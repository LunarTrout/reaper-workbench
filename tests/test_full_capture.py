from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import MappingProxyType

import numpy as np
import pytest
import soundfile as sf

from reaper_audio.context.full_capture import (
    CaptureArchive,
    FullCaptureRecord,
    REQUIRED_MAP_KEYS,
)
from reaper_audio.errors import ValidationError
from reaper_audio.mcp_server import _jsonable


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record(tmp_path: Path, capture_id: str = "cap_test") -> tuple[FullCaptureRecord, dict[str, Path]]:
    track_guid = "{TRACK-1}"
    master_guid = "{MASTER}"
    wav_track = tmp_path / "track.wav"
    wav_master = tmp_path / "master.wav"
    sf.write(wav_track, np.zeros((480, 1)), 48000, subtype="PCM_24")
    sf.write(wav_master, np.zeros((480, 2)), 48000, subtype="PCM_24")
    revision = {"project_id": "reaper:test", "change_count": 2, "path": "test.rpp"}
    coverage = {name: True for name in REQUIRED_MAP_KEYS - {"coverage"} |
                {"audio", "analysis"}}
    project_map = {
        "tracks": [{"guid": track_guid, "name": "Track 1", "items": [],
                    "fx": [], "mute": False, "solo": 0, "volume": 1.0,
                    "pan": 0.0}],
        "master": {"guid": master_guid, "name": "", "items": [], "fx": [],
                   "mute": False, "solo": 0, "volume": 1.0, "pan": 0.0},
        "items": [], "takes": [], "midi": [], "fx": [],
        "routing": [], "envelopes": [], "automation": [],
        "tempo": {"bpm": 120}, "time_signatures": [],
        "markers": [], "regions": [],
        "selection": {"track_guids": [], "time_start_s": 0, "time_end_s": 0},
        "transport": {"play_state": 0, "cursor_s": 0, "play_position_s": 0},
        "coverage": {name: True for name in REQUIRED_MAP_KEYS - {"coverage"}},
        "digest": {"coverage": dict(coverage), "fields": {}},
    }
    audio = []
    files = {}
    for guid, path, is_master, stage in (
        (track_guid, wav_track, False, "track_via_master_post_fx"),
        (master_guid, wav_master, True, "master_mix_post_fx"),
    ):
        info = sf.info(path)
        digest = _sha_file(path)
        audio.append({
            "asset_id": f"ast_sha256_{digest}",
            "source_guid": guid,
            "name": path.stem,
            "is_master": is_master,
            "stage": stage,
            "capture_method": "native_render",
            "sample_rate": info.samplerate,
            "channels": info.channels,
            "frames": info.frames,
            "duration_s": info.frames / info.samplerate,
            "bytes": path.stat().st_size,
            "sha256": digest,
            "signal_window": {"project_start_s": 0.0, "project_end_s": 1.0,
                              "file_start_s": 0.0, "file_end_s": 1.0},
            "capture_revision": revision,
            "status": "leased",
            "lease_id": "job-test",
        })
        files[guid] = path
    record = FullCaptureRecord(
        capture_id=capture_id,
        project_id="reaper:test",
        project_path="test.rpp",
        revision_before=revision,
        revision_after=revision,
        time_window={"project_start_s": 0.0, "project_end_s": 1.0,
                     "file_start_s": 0.0, "file_end_s": 1.0},
        project_map=project_map,
        audio=audio,
        analysis={guid: {"metrics": {"integrated_lufs": {"value": -20.0}}}
                  for guid in files},
        coverage=coverage,
        missing=[],
        created_at="2026-01-01T00:00:00+00:00",
        producer="test",
        restore_report={"no_media_items_added": True},
    )
    return record, files


def test_record_is_sealed_and_rejects_nested_mutation(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    with pytest.raises(TypeError):
        record.project_map = {}
    with pytest.raises(TypeError):
        record.audio[0]["status"] = "expired"
    assert record.to_dict()["audio"][0]["status"] == "leased"


def test_coverage_true_requires_payload() -> None:
    # The constructor is the only place that can turn a coverage claim into a
    # sealed record; a missing payload must fail before any archive write.
    with pytest.raises(ValidationError):
        FullCaptureRecord(
            capture_id="cap_bad", project_id="p", project_path="p",
            revision_before={"project_id": "p", "change_count": 0, "path": "p"},
            revision_after=None,
            time_window={"project_start_s": 0, "project_end_s": 1},
            project_map={"tracks": [{"guid": "x"}], "master": {"guid": "m"}},
            audio=[], analysis={}, coverage={}, missing=[],
            created_at="2026-01-01T00:00:00+00:00",
        )


def test_archive_retains_content_addressed_audio(tmp_path: Path) -> None:
    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archived = archive.append(record, files)
    capture_dir = Path(archived.record_path).parent
    assert (capture_dir / "record.json").is_file()
    assert all(path.is_file() for path in files.values())
    rows = archive.list_index()
    assert len(rows) == 1
    assert rows[0]["capture_id"] == "cap_test"
    assert all(item["status"] == "stored" for item in rows[0]["audio"].values())
    stored = archive.read("cap_test")
    assert all(item["status"] == "stored" and item.get("archive_audio")
               for item in stored.audio)
    blob_paths = sorted((tmp_path / "archive" / "audio" / "sha256").glob("*.wav"))
    assert len(blob_paths) == 2
    assert all(_sha_file(path) == path.stem for path in blob_paths)
    assert archive.status("cap_test")["retention_status"] == "stored"
    assert not list((tmp_path / "archive" / ".staging").glob("*"))

    # The same content under a different capture id must not create a second
    # copy of either WAV.
    second, second_files = _record(tmp_path, "cap_test_2")
    archive.append(second, second_files)
    assert len(list((tmp_path / "archive" / "audio" / "sha256").glob("*.wav"))) == 2

    with pytest.raises(ValidationError, match="must not be expired"):
        archive.expire("cap_test", reason="lease_elapsed")


def test_archive_read_rejects_missing_or_corrupt_audio(tmp_path: Path) -> None:
    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)
    stored = archive.read("cap_test")
    archive_audio = Path(stored.audio[0]["archive_audio"])
    blob = tmp_path / "archive" / archive_audio
    blob.write_bytes(b"corrupt")
    with pytest.raises(ValidationError, match="hash mismatch"):
        archive.read("cap_test")


def test_archive_retains_extra_master_attempt_evidence(tmp_path: Path) -> None:
    record, files = _record(tmp_path)
    extra = tmp_path / "failed-master.wav"
    sf.write(extra, np.zeros(480), 48000, subtype="PCM_24")
    extra_sha = _sha_file(extra)
    data = record.to_dict()
    data["timings"] = {
        "master_attempts": [{
            "attempt": 1,
            "status": "silent",
            "silent": True,
            "valid": False,
            "sha256": extra_sha,
            "archive_audio": f"audio/sha256/{extra_sha}.wav",
        }]
    }
    data["integrity_sha256"] = None
    record_with_attempt = FullCaptureRecord.from_dict(data)
    archive = CaptureArchive(tmp_path / "archive")
    archived = archive.append(record_with_attempt, files,
                              extra_files={extra_sha: str(extra)})
    stored = archive.read(archived.capture_id)
    attempt = stored.timings["master_attempts"][0]
    assert attempt["sha256"] == extra_sha
    assert attempt["archive_audio"] == f"audio/sha256/{extra_sha}.wav"
    assert (tmp_path / "archive" / attempt["archive_audio"]).is_file()
    assert not list((tmp_path / "archive" / ".staging").glob("*"))


def test_archive_read_rejects_corrupt_extra_evidence(tmp_path: Path) -> None:
    record, files = _record(tmp_path)
    extra = tmp_path / "failed-master.wav"
    sf.write(extra, np.zeros(481), 48000, subtype="PCM_24")
    extra_sha = _sha_file(extra)
    data = record.to_dict()
    data["timings"] = {
        "master_attempts": [{
            "attempt": 1, "status": "silent", "silent": True, "valid": False,
            "sha256": extra_sha,
            "archive_audio": f"audio/sha256/{extra_sha}.wav",
        }]
    }
    data["integrity_sha256"] = None
    archive = CaptureArchive(tmp_path / "archive")
    archived = archive.append(FullCaptureRecord.from_dict(data), files,
                              extra_files={extra_sha: str(extra)})
    evidence = tmp_path / "archive" / f"audio/sha256/{extra_sha}.wav"
    evidence.write_bytes(b"corrupt")
    with pytest.raises(ValidationError, match="evidence hash mismatch"):
        archive.read(archived.capture_id)


def test_archive_rejects_duplicate_capture_id(tmp_path: Path) -> None:
    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)
    with pytest.raises(ValidationError):
        archive.append(record, files)


def test_jsonable_handles_mappingproxy() -> None:
    value = MappingProxyType({"a": MappingProxyType({"b": 1}), "c": (2, 3)})
    assert _jsonable(value) == {"a": {"b": 1}, "c": [2, 3]}
    json.dumps(_jsonable(value), allow_nan=False)
