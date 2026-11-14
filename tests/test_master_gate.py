from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf

from reaper_audio.analysis.artifacts import AudioArtifactWorkspace
from reaper_audio.context.full_capture import CaptureArchive
from reaper_audio.daw.full_capture import (FullCaptureService,
                                           _track_activity,
                                           _wave_master_validation)
from reaper_audio.daw.session import ProjectRevision
from reaper_audio.errors import CaptureMasterInvalid


def _wav(path: Path, *, signal: bool, frames: int = 2048) -> None:
    if signal:
        t = np.arange(frames) / 48000
        samples = 0.2 * np.sin(2 * np.pi * 220 * t)
    else:
        samples = np.zeros(frames)
    sf.write(path, samples, 48000, subtype="PCM_24")


def _rendered(path: Path, revision: int) -> dict:
    return {
        "files": [{"path": str(path)}],
        "revision_after": {"project_id": "p", "change_count": revision,
                           "path": "p.rpp"},
        "render_settings_used": {"RENDER_SETTINGS": 0},
        "_elapsed_s": 0.1,
    }


def _service(tmp_path: Path) -> FullCaptureService:
    return FullCaptureService(
        SimpleNamespace(bridge=SimpleNamespace()), tmp_path / "archive")


def test_master_gate_retains_failed_attempt_and_selects_signal(tmp_path: Path) -> None:
    service = _service(tmp_path)
    workspace = AudioArtifactWorkspace(tmp_path / "incoming")
    try:
        silent = workspace.directory / "master-silent.wav"
        signal = workspace.directory / "master-signal.wav"
        _wav(silent, signal=False)
        _wav(signal, signal=True)
        renders = iter([_rendered(silent, 3), _rendered(signal, 4)])
        service._render_master_only = lambda *args, **kwargs: next(renders)  # type: ignore[method-assign]
        item, render, attempts, extra = service._render_master_with_gate(
            revision=ProjectRevision.from_dict(
                {"project_id": "p", "change_count": 2, "path": "p.rpp"}),
            workspace=workspace,
            master_guid="{MASTER}",
            start_s=10.0,
            end_s=30.0,
            channels="stereo",
            sample_rate=48000,
            tail_ms=0,
            project_length_s=300.0,
            source_activity={"tracks": [{"mute": False}],
                             "audible_guids": ["{T}"], "audible_count": 1,
                             "all_silent": False},
            master_state={"mute": False, "volume": 1.0},
        )
        assert item["path"] == str(signal)
        assert render["revision_after"]["change_count"] == 4
        assert [attempt["status"] for attempt in attempts] == ["silent", "signal_present"]
        assert attempts[0]["valid"] is False
        assert attempts[1]["valid"] is True
        assert attempts[0]["selected"] is False
        assert attempts[1]["selected"] is True
        assert list(extra) == [attempts[0]["sha256"]]
        assert extra[attempts[0]["sha256"]] == str(silent)
    finally:
        workspace.close()


def test_master_gate_persistent_silence_keeps_last_attempt_and_evidence(tmp_path: Path) -> None:
    service = _service(tmp_path)
    workspace = AudioArtifactWorkspace(tmp_path / "incoming")
    try:
        first = workspace.directory / "master-silent-1.wav"
        second = workspace.directory / "master-silent-2.wav"
        _wav(first, signal=False, frames=2048)
        _wav(second, signal=False, frames=1024)
        renders = iter([_rendered(first, 3), _rendered(second, 4)])
        service._render_master_only = lambda *args, **kwargs: next(renders)  # type: ignore[method-assign]
        item, _, attempts, extra = service._render_master_with_gate(
            revision=ProjectRevision.from_dict(
                {"project_id": "p", "change_count": 2, "path": "p.rpp"}),
            workspace=workspace,
            master_guid="{MASTER}",
            start_s=10.0,
            end_s=30.0,
            channels="stereo",
            sample_rate=48000,
            tail_ms=0,
            project_length_s=300.0,
            source_activity={"tracks": [{"mute": False}],
                             "audible_guids": ["{T}"], "audible_count": 1,
                             "all_silent": False},
            master_state={"mute": False, "volume": 1.0},
        )
        assert item["path"] == str(second)
        assert [attempt["selected"] for attempt in attempts] == [False, True]
        assert all(attempt["silent"] for attempt in attempts)
        assert len(extra) == 2
        assert set(extra) == {attempts[0]["sha256"], attempts[1]["sha256"]}
    finally:
        workspace.close()


def test_wave_master_validation_reports_silence_without_masking(tmp_path: Path) -> None:
    silent = tmp_path / "silent.wav"
    signal = tmp_path / "signal.wav"
    _wav(silent, signal=False)
    _wav(signal, signal=True)
    silent_result = _wave_master_validation(silent, "{MASTER}")
    signal_result = _wave_master_validation(signal, "{MASTER}")
    assert silent_result["status"] == "silent"
    assert silent_result["silent"] is True
    assert silent_result["valid"] is False
    assert signal_result["status"] == "signal_present"
    assert signal_result["silent"] is False
    assert signal_result["valid"] is True


def test_expected_silence_is_not_rejected() -> None:
    from reaper_audio.daw.full_capture import _classify_master_silence

    silent_master = {"silent": True}
    case_silent_section = _classify_master_silence(
        silent_master,
        {"tracks": [{"mute": False}], "audible_guids": [],
         "audible_count": 0, "all_silent": True},
        {"mute": False, "volume": 1.0})
    assert case_silent_section == {
        "expected_silence": True,
        "silence_reason": "all_rendered_sources_silent",
        "unexpected_silence": False,
    }
    case_master_muted = _classify_master_silence(
        silent_master,
        {"tracks": [{"mute": False}], "audible_guids": ["{T}"],
         "audible_count": 1, "all_silent": False},
        {"mute": True, "volume": 1.0})
    assert case_master_muted["expected_silence"] is True
    assert case_master_muted["silence_reason"] == "master_muted"
    case_master_zero = _classify_master_silence(
        silent_master,
        {"tracks": [{"mute": False}], "audible_guids": ["{T}"],
         "audible_count": 1, "all_silent": False},
        {"mute": False, "volume": 0.0})
    assert case_master_zero["expected_silence"] is True
    assert case_master_zero["silence_reason"] == "master_zero_volume"
    case_all_tracks_muted = _classify_master_silence(
        silent_master,
        {"tracks": [{"mute": True}, {"mute": True}], "audible_guids": [],
         "audible_count": 0, "all_silent": True},
        {"mute": False, "volume": 1.0})
    assert case_all_tracks_muted["expected_silence"] is True


def test_track_activity_uses_rendered_stems_and_mute_state(tmp_path: Path) -> None:
    silent = tmp_path / "track-silent.wav"
    active = tmp_path / "track-active.wav"
    _wav(silent, signal=False)
    _wav(active, signal=True)
    activity = _track_activity([
        ("{SILENT}", "silent", str(silent), False),
        ("{ACTIVE}", "active", str(active), True),
    ])
    assert activity["all_silent"] is False
    assert activity["audible_guids"] == ["{ACTIVE}"]
    rows = {row["source_guid"]: row for row in activity["tracks"]}
    assert rows["{SILENT}"]["audible"] is False
    assert rows["{ACTIVE}"]["audible"] is True
    assert rows["{ACTIVE}"]["mute"] is True


def test_unexpected_silent_master_is_retried(tmp_path: Path) -> None:
    service = _service(tmp_path)
    workspace = AudioArtifactWorkspace(tmp_path / "incoming")
    try:
        first = workspace.directory / "master-unexpected-1.wav"
        second = workspace.directory / "master-unexpected-2.wav"
        _wav(first, signal=False, frames=2048)
        _wav(second, signal=False, frames=1024)
        renders = iter([_rendered(first, 3), _rendered(second, 4)])
        service._render_master_only = lambda *args, **kwargs: next(renders)  # type: ignore[method-assign]
        item, _, attempts, extra = service._render_master_with_gate(
            revision=ProjectRevision.from_dict(
                {"project_id": "p", "change_count": 2, "path": "p.rpp"}),
            workspace=workspace,
            master_guid="{MASTER}",
            start_s=10.0, end_s=30.0, channels="stereo", sample_rate=48000,
            tail_ms=0, project_length_s=300.0,
            source_activity={"tracks": [{"mute": False}],
                             "audible_guids": ["{T}"], "audible_count": 1,
                             "all_silent": False},
            master_state={"mute": False, "volume": 1.0},
        )
        assert item["path"] == str(second)
        assert [attempt["expected_silence"] for attempt in attempts] == [False, False]
        assert [attempt["unexpected_silence"] for attempt in attempts] == [True, True]
        assert len(extra) == 2
    finally:
        workspace.close()


def test_expected_silence_selects_first_attempt_without_retry(tmp_path: Path) -> None:
    service = _service(tmp_path)
    workspace = AudioArtifactWorkspace(tmp_path / "incoming")
    try:
        silent = workspace.directory / "master-silent.wav"
        _wav(silent, signal=False)
        renders = iter([_rendered(silent, 3)])
        service._render_master_only = lambda *args, **kwargs: next(renders)  # type: ignore[method-assign]
        item, _, attempts, extra = service._render_master_with_gate(
            revision=ProjectRevision.from_dict(
                {"project_id": "p", "change_count": 2, "path": "p.rpp"}),
            workspace=workspace,
            master_guid="{MASTER}",
            start_s=10.0, end_s=30.0, channels="stereo", sample_rate=48000,
            tail_ms=0, project_length_s=300.0,
            source_activity={"tracks": [{"mute": False}], "audible_guids": [],
                             "audible_count": 0, "all_silent": True},
            master_state={"mute": False, "volume": 1.0},
        )
        assert item["path"] == str(silent)
        assert len(attempts) == 1
        assert attempts[0]["status"] == "expected_silence"
        assert attempts[0]["valid"] is True
        assert attempts[0]["selected"] is True
        assert extra == {}
    finally:
        workspace.close()


def test_capture_master_invalid_is_structured() -> None:
    error = CaptureMasterInvalid("silent", details={"capture_id": "cap_x"})
    assert error.code == "capture_master_invalid"
    assert error.as_dict() == {
        "ok": False, "error": "capture_master_invalid", "message": "silent",
        "details": {"capture_id": "cap_x"},
    }


def test_build_record_marks_silent_master_invalid(tmp_path: Path) -> None:
    service = _service(tmp_path)
    workspace = AudioArtifactWorkspace(tmp_path / "incoming")
    try:
        track = workspace.directory / "track.wav"
        master = workspace.directory / "master.wav"
        _wav(track, signal=True, frames=48000)
        _wav(master, signal=False, frames=48000)
        track_sha = hashlib.sha256(track.read_bytes()).hexdigest()
        master_sha = hashlib.sha256(master.read_bytes()).hexdigest()
        revision = ProjectRevision.from_dict(
            {"project_id": "p", "change_count": 2, "path": "p.rpp"})
        service._profile = {"decode_s": 0.0, "dsp_s": 0.0}
        context = {
            "tracks": [{"guid": "{T}", "name": "track", "items": [], "fx": []}],
            "master": {"guid": "{M}", "name": "MASTER", "items": [], "fx": []},
            "selection": {"track_guids": [], "time_start_s": 0, "time_end_s": 0},
            "transport": {"play_state": 0, "cursor_s": 0, "edit_cursor_s": 0,
                          "play_position_s": 0},
            "tempo": {"bpm": 120}, "length_s": 1.0,
        }
        rendered = [
            {"path": str(track), "source_guid": "{T}", "name": "track",
             "is_master": False, "stage": "track", "sha256": track_sha},
            {"path": str(master), "source_guid": "{M}", "name": "MASTER",
             "is_master": True, "stage": "master_mix_post_fx",
             "sha256": master_sha},
        ]
        record = service._build_record(
            capture_id="cap_master_status", created_at="2026-01-01T00:00:00+00:00",
            producer="test", context=context, digest={"fields": {}},
            revision=revision, revision_after=revision, section=(0.0, 1.0),
            rendered_files=rendered, workspace=workspace,
            restore_report={}, analysis_workers=1,
            master_attempts=[{"attempt": 1, "valid": False, "silent": True}],
            timings={},
        )
        assert record.timings["capture_status"] == "invalid_master"
        assert record.timings["master_validation"]["valid"] is False
    finally:
        workspace.close()


def test_expected_silence_record_is_valid_and_archived(tmp_path: Path) -> None:
    service = _service(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    workspace = AudioArtifactWorkspace(tmp_path / "incoming")
    try:
        track = workspace.directory / "track.wav"
        master = workspace.directory / "master.wav"
        _wav(track, signal=True, frames=48000)
        _wav(master, signal=False, frames=48000)
        track_sha = hashlib.sha256(track.read_bytes()).hexdigest()
        master_sha = hashlib.sha256(master.read_bytes()).hexdigest()
        revision = ProjectRevision.from_dict(
            {"project_id": "p", "change_count": 2, "path": "p.rpp"})
        service._profile = {"decode_s": 0.0, "dsp_s": 0.0}
        context = {
            "tracks": [{"guid": "{T}", "name": "track", "items": [], "fx": []}],
            "master": {"guid": "{M}", "name": "MASTER", "items": [], "fx": [],
                       "mute": True, "volume": 1.0},
            "selection": {"track_guids": [], "time_start_s": 0, "time_end_s": 0},
            "transport": {"play_state": 0, "cursor_s": 0, "edit_cursor_s": 0,
                          "play_position_s": 0},
            "tempo": {"bpm": 120}, "length_s": 1.0,
        }
        rendered = [
            {"path": str(track), "source_guid": "{T}", "name": "track",
             "is_master": False, "stage": "track", "sha256": track_sha},
            {"path": str(master), "source_guid": "{M}", "name": "MASTER",
             "is_master": True, "stage": "master_mix_post_fx",
             "sha256": master_sha},
        ]
        record = service._build_record(
            capture_id="cap_expected_silence", created_at="2026-01-01T00:00:00+00:00",
            producer="test", context=context, digest={"fields": {}},
            revision=revision, revision_after=revision, section=(0.0, 1.0),
            rendered_files=rendered, workspace=workspace,
            restore_report={}, analysis_workers=1,
            master_attempts=[{"attempt": 1, "valid": True, "silent": True,
                              "expected_silence": True,
                              "silence_reason": "master_muted",
                              "selected": True}],
            timings={},
        )
        assert record.timings["capture_status"] == "valid_expected_silence"
        assert record.timings["master_validation"]["status"] == "expected_silence"
        assert record.timings["master_validation"]["valid"] is True
        sealed = archive.append(record, {"{T}": track, "{M}": master})
        stored = archive.read(sealed.capture_id)
        assert stored.timings["capture_status"] == "valid_expected_silence"
        assert stored.timings["master_validation"]["expected_silence"] is True
    finally:
        workspace.close()
