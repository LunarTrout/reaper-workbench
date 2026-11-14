"""Wave C: capture-side windows, factual restore reports and artifact ownership.

These tests are hermetic: a fake bridge writes real WAV files (stdlib ``wave``)
into the job directory, so the session's own window bookkeeping, hashing,
manifest and cleanup paths run for real. No REAPER instance is contacted.
"""

from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path

import numpy as np
import pytest

from reaper_audio.context.audio import CaptureMethod, Stage
from reaper_audio.daw.session import ProjectSession
from reaper_audio.errors import ArtifactCleanupError, BridgeUnavailable
from reaper_audio.models import Section

PROJECT_ID = "reaper:wave-c-project"
PROJECT_PATH = "C:/wave-c/project.rpp"
SAMPLE_RATE = 8_000
TRACK_A = "{TRACK-A}"
TRACK_B = "{TRACK-B}"
MASTER_GUID = "{MASTER}"
ITEM_A = "{ITEM-A}"
ITEM_B = "{ITEM-B}"

_OMIT = object()


def _write_wav(path: Path, *, duration_s: float, sample_rate: int = SAMPLE_RATE,
               channels: int = 2, tone_hz: float = 220.0,
               amplitude: float = 0.2) -> None:
    """Write a real 16-bit PCM WAV through the standard-library writer."""
    frames = int(round(duration_s * sample_rate))
    time_s = np.arange(frames, dtype=np.float64) / sample_rate
    signal = amplitude * np.sin(2.0 * np.pi * tone_hz * time_s)
    pcm = np.clip(np.round(signal * 32767.0), -32768, 32767).astype("<i2")
    if channels > 1:
        pcm = np.repeat(pcm[:, None], channels, axis=1)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())


class FakeCaptureBridge:
    """A bridge that behaves like the live one for one capture of two tracks."""

    def __init__(self, *, restore_report: object = _OMIT,
                 drift: str | None = None,
                 add_to_project: float = 0.0) -> None:
        self.restore_report = ({"restored": True, "failures": []}
                               if restore_report is _OMIT else restore_report)
        self.drift = drift
        self.add_to_project = add_to_project
        self.context_calls = 0
        self.calls: list[tuple[str, dict]] = []
        self.render_args: dict | None = None
        self.sampled_args: dict | None = None
        self.revision = {"project_id": PROJECT_ID, "change_count": 4,
                         "path": PROJECT_PATH}
        self.tracks = [
            {"guid": TRACK_A, "index": 0, "name": "Guitar", "volume": 0.75,
             "pan": -0.2, "mute": False, "solo": 0,
             "fx": [{"guid": "{FX-A}", "name": "EQ"}],
             "items": [{"guid": ITEM_A, "position_s": 0.0, "length_s": 1.0}]},
            {"guid": TRACK_B, "index": 1, "name": "Bass", "volume": 0.8,
             "pan": 0.1, "mute": False, "solo": 0, "fx": [],
             "items": [{"guid": ITEM_B, "position_s": 0.0, "length_s": 1.0}]},
        ]
        self.master = {"guid": MASTER_GUID, "index": -1, "name": "MASTER",
                       "volume": 1.0, "pan": 0.0, "mute": False, "solo": 0,
                       "fx": [{"guid": "{FX-M}", "name": "Limiter"}]}

    def project_context(self, project_id: str | None = None, **_: object) -> dict:
        self.context_calls += 1
        if self.context_calls == 2 and self.drift == "track":
            self.tracks = [*self.tracks, {
                "guid": "{TRACK-C}", "index": 2, "name": "Vox", "volume": 1.0,
                "pan": 0.0, "mute": False, "solo": 0, "fx": [], "items": [],
            }]
        return {
            "revision": dict(self.revision),
            "path": self.revision["path"],
            "name": "Wave C Fixture",
            "track_count": len(self.tracks),
            "tracks": [dict(track, items=list(track["items"]))
                       for track in self.tracks],
            "master": dict(self.master),
            "selection": {"track_guids": []},
            "tempo": {"bpm": 120.0, "numerator": 4, "denominator": 4,
                      "markers": []},
            "transport": {"play_state": 0, "position_s": 0.0},
            "length_s": 1.0,
        }

    def project_digest(self, project_id: str | None = None) -> dict:
        return {
            "project_id": PROJECT_ID,
            "path": PROJECT_PATH,
            "change_count": self.revision["change_count"],
            "digest_version": 1,
            "coverage": {name: True for name in
                         ("tracks", "items", "takes", "fx", "sends", "tempo")},
            "missing": [],
            "fields": {
                "tracks": [{"guid": track["guid"]} for track in self.tracks],
                "items": [{"guid": item["guid"]} for track in self.tracks
                          for item in track["items"]],
                "tempo": [{"time_s": 0.0, "bpm": 120.0, "numerator": 4,
                           "denominator": 4}],
            },
        }

    def capture_render(self, *, expected_revision: dict, output_dir: str,
                       job_id: str, expected_track_count: int,
                       timeout_s: float = 600.0,
                       section_start_s: float | None = None,
                       section_end_s: float | None = None,
                       master_only: bool = False, channels: str = "stereo",
                       sample_rate: int | None = None,
                       tail_ms: int = 0) -> dict:
        self.render_args = {
            "expected_revision": dict(expected_revision),
            "output_dir": str(output_dir), "job_id": job_id,
            "expected_track_count": expected_track_count,
            "section_start_s": section_start_s, "section_end_s": section_end_s,
            "master_only": master_only, "channels": channels,
            "sample_rate": sample_rate, "tail_ms": tail_ms,
        }
        before = dict(self.revision)
        after = {**self.revision, "change_count": self.revision["change_count"] + 1}
        self.revision = after
        base = 1.0 if section_start_s is None else section_end_s - section_start_s
        duration = base + tail_ms / 1000.0
        rate = sample_rate or SAMPLE_RATE
        channel_count = 1 if channels == "mono" else 2
        directory = Path(output_dir)
        files: list[dict] = []
        rendered_tracks = [] if master_only else list(self.tracks)
        for index, track in enumerate(rendered_tracks):
            path = directory / f"rali-{job_id}-{index + 1:02d}-{track['name']}.wav"
            _write_wav(path, duration_s=duration, sample_rate=rate,
                       channels=channel_count, tone_hz=180.0 + 60.0 * index)
            files.append({"source_guid": track["guid"], "name": track["name"],
                          "track_index": index, "is_master": False,
                          "path": str(path), "bytes": path.stat().st_size})
        master_path = directory / f"rali-{job_id}-00-MASTER.wav"
        _write_wav(master_path, duration_s=duration, sample_rate=rate,
                   channels=channel_count, tone_hz=440.0)
        files.append({"source_guid": MASTER_GUID, "name": "MASTER",
                      "track_index": -1, "is_master": True,
                      "path": str(master_path),
                      "bytes": master_path.stat().st_size})
        result = {
            "project_id": PROJECT_ID,
            "revision": before,
            "revision_after": after,
            "capture_point": ("native_render_master_only" if master_only
                              else "native_render_track_stems_plus_master"),
            "render_action": "File: Render project",
            "render_settings_used": {"RENDER_CHANNELS": float(channel_count),
                                     "RENDER_TAILMS": float(tail_ms),
                                     "RENDER_ADDTOPROJ": self.add_to_project},
            "channels": channel_count,
            "sample_rate": rate,
            "tail_s": tail_ms / 1000.0,
            "files": files,
        }
        # ``restore_report`` is only present when the bridge actually reported
        # one; ``None`` models a bridge that said nothing.
        if self.restore_report is not None:
            result["restore_report"] = self.restore_report
        return result

    def sample_tracks(self, *, project_id: str, output_dir: str, job_id: str,
                      start_s: float, end_s: float,
                      sample_rate: int = 48_000,
                      timeout_s: float = 300.0) -> dict:
        self.sampled_args = {"project_id": project_id, "output_dir": str(output_dir),
                             "job_id": job_id, "start_s": start_s,
                             "end_s": end_s}
        directory = Path(output_dir)
        files: list[dict] = []
        for index, track in enumerate(self.tracks):
            path = directory / f"rali-{job_id}-{index + 1:02d}-{track['name']}.wav"
            _write_wav(path, duration_s=end_s - start_s, sample_rate=sample_rate,
                       channels=2, tone_hz=180.0 + 60.0 * index)
            files.append({"source_guid": track["guid"], "name": track["name"],
                          "track_index": index, "is_master": False,
                          "path": str(path), "bytes": path.stat().st_size})
        return {"project_id": PROJECT_ID, "revision": dict(self.revision),
                "files": files,
                "capture_point": "native_audio_accessor_tracks"}

    def call(self, operation: str, arguments: dict | None = None, **_: object) -> dict:
        self.calls.append((operation, dict(arguments or {})))
        if operation == "project.render_settings.read":
            return {"project_id": self.revision["project_id"],
                    "revision": dict(self.revision),
                    "render_settings": {"format": "wav", "bounds": "time"},
                    "render_targets": "C:/renders/old-target.wav", "dirty": 0}
        raise AssertionError(f"unexpected bridge operation: {operation}")


def _asset_by_guid(captured: dict) -> dict:
    return {asset.source_guid: asset for asset in captured["assets"]}


def test_capture_keeps_tail_in_the_file_window_and_out_of_the_project_window(
        tmp_path: Path) -> None:
    bridge = FakeCaptureBridge()
    session = ProjectSession(bridge)

    with session.capture_audio(tmp_path / "artifacts", section=Section(0.0, 1.0),
                               tail_ms=250) as captured:
        capture = captured["capture"]
        assert capture.status == "complete"
        assert capture.missing == ()
        assert capture.tail_s == pytest.approx(0.25)
        assert capture.restore_report == {"restored": True, "failures": []}
        assert capture.coverage["digest_missing"] == []
        assert capture.coverage["expected"] == 3
        assert capture.coverage["assets"] == 3

        window = captured["time_window"]
        assert window["tail_s"] == pytest.approx(0.25)
        assert window["project_start_s"] == 0.0
        assert window["project_end_s"] == pytest.approx(1.0)
        assert window["file_start_s"] == 0.0
        assert window["file_end_s"] == pytest.approx(1.25)
        assert window["requested_section"] == {"start_s": 0.0, "end_s": 1.0}

        assert bridge.render_args["tail_ms"] == 250
        assert bridge.render_args["section_start_s"] == 0.0
        assert bridge.render_args["section_end_s"] == 1.0

        for guid, asset in _asset_by_guid(captured).items():
            assert asset.duration_s == pytest.approx(1.25)
            assert asset.window is not None
            # The project window is the section, never section + tail.
            assert asset.window.project_start_s == 0.0
            assert asset.window.project_end_s == pytest.approx(1.0)
            assert asset.window.file_start_s == 0.0
            assert asset.window.file_end_s == pytest.approx(1.25)

        for entry in [*captured["tracks"], captured["master"]]:
            capture_context = entry["capture"]
            assert capture_context["time_window"] == window
            assert capture_context["audio_format"]["duration_s"] == pytest.approx(1.25)
            assert capture_context["audio_format"]["file_start_s"] == 0.0


@pytest.mark.parametrize(
    ("restore_report", "message"),
    [
        (None, "did not report whether the capture render was restored"),
        ({"restored": False, "failures": []}, "did not confirm"),
        ({"restored": "yes", "failures": []}, "did not confirm"),
        ({"restored": 1, "failures": []}, "did not confirm"),
        ({"restored": True, "failures": None}, "invalid capture restore report"),
        ({"restored": True, "failures": ["RENDER_TAILFLAG"]}, "could not restore"),
    ],
)
def test_capture_refuses_an_absent_or_failing_restore_report(
        tmp_path: Path, restore_report: object, message: str) -> None:
    root = tmp_path / "artifacts"
    bridge = FakeCaptureBridge(restore_report=restore_report)

    with pytest.raises(BridgeUnavailable, match=message) as raised:
        with ProjectSession(bridge).capture_audio(root, section=Section(0.0, 1.0)):
            pytest.fail("capture must not yield without a factual restore report")

    assert raised.value.details["field"] in {"restore_report", "failures"}
    # The renders were registered before the check, so the failed capture
    # still removes its own temporary job instead of leaking WAVs.
    assert not list(root.glob("job-*"))


def test_capture_preserves_unregistered_files_and_never_glob_cleans(
        tmp_path: Path) -> None:
    bridge = FakeCaptureBridge()
    root = tmp_path / "artifacts"
    captured: dict | None = None

    with pytest.raises(ArtifactCleanupError):
        with ProjectSession(bridge).capture_audio(root) as captured:
            job_directory = Path(captured["artifact_job"]["directory"])
            stray = job_directory / "another-writer.wav"
            stray.write_bytes(b"not owned by this manifest" + bytes(64))
            # Same shape as a REAPER render and the same job-id prefix: only
            # stale-job reclamation may ever touch it, never immediate cleanup.
            orphan = job_directory / f"rali-{bridge.render_args['job_id']}-99-orphan.wav"
            _write_wav(orphan, duration_s=0.1)
            orphan_bytes = orphan.read_bytes()

    assert captured is not None
    cleanup = captured["artifact_job"]["cleanup"]
    assert cleanup["removed_job_directory"] is False
    assert cleanup["retained"] is False
    assert {Path(path).name for path in cleanup["preserved_paths"]} == {
        "another-writer.wav", orphan.name}
    assert len(cleanup["removed_files"]) == 3
    assert stray.read_bytes() == b"not owned by this manifest" + bytes(64)
    assert orphan.read_bytes() == orphan_bytes
    assert sorted(path.name for path in job_directory.iterdir()) == [
        ".reaper-audio-job.json", "another-writer.wav", orphan.name]


def test_capture_hashes_every_asset_and_records_it_in_the_manifest(
        tmp_path: Path) -> None:
    bridge = FakeCaptureBridge()

    with ProjectSession(bridge).capture_audio(tmp_path / "artifacts") as captured:
        job_directory = Path(captured["artifact_job"]["directory"])
        manifest = json.loads((job_directory / ".reaper-audio-job.json")
                              .read_text(encoding="utf-8"))
        assert manifest["status"] == "active"
        owned = {entry["name"]: entry for entry in manifest["assets"]}
        assets = _asset_by_guid(captured)
        filename_by_guid = {
            entry["guid"]: Path(entry["capture"]["path"]).name
            for entry in [*captured["tracks"], captured["master"]]
        }

        assert len(owned) == 3
        assert set(owned) == set(filename_by_guid.values())
        for guid, asset in assets.items():
            filename = filename_by_guid[guid]
            path = job_directory / filename
            measured = hashlib.sha256(path.read_bytes()).hexdigest()
            assert asset.sha256 == measured
            assert asset.asset_id == f"ast_sha256_{measured}"
            assert asset.bytes == path.stat().st_size
            assert owned[filename]["sha256"] == measured
            assert owned[filename]["bytes"] == asset.bytes
        # Two stems of identical duration but different content must not
        # collapse to one content identity.
        assert len({asset.sha256 for asset in assets.values()}) == 3


def test_capture_cleanup_failure_preserves_the_locked_file_and_reports_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = FakeCaptureBridge()
    root = tmp_path / "artifacts"
    captured: dict | None = None
    real_unlink = Path.unlink

    def flaky_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self.name.endswith("-01-Guitar.wav"):
            raise PermissionError("file is held open by another process")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    with pytest.raises(ArtifactCleanupError) as raised:
        with ProjectSession(bridge).capture_audio(root) as captured:
            job_directory = Path(captured["artifact_job"]["directory"])
            locked = job_directory / f"rali-{bridge.render_args['job_id']}-01-Guitar.wav"
            assert locked.is_file()

    assert captured is not None
    cleanup = captured["artifact_job"]["cleanup"]
    assert cleanup["preserved_paths"] == [str(locked)]
    assert locked.exists()
    assert raised.value.details["preserved_paths"] == [str(locked)]
    assert len(cleanup["removed_files"]) == 2
    assert not (job_directory / f"rali-{bridge.render_args['job_id']}-00-MASTER.wav").exists()


def test_typed_capture_method_is_consistent_with_the_legacy_string(
        tmp_path: Path) -> None:
    legacy_names = {
        CaptureMethod.native_render: "reaper_native_offline_render",
        CaptureMethod.audio_accessor: "reaper_audio_accessor",
    }
    bridge = FakeCaptureBridge()

    with ProjectSession(bridge).capture_audio(
            tmp_path / "render", section=Section(0.0, 1.0)) as rendered:
        for entry in [*rendered["tracks"], rendered["master"]]:
            context = entry["capture"]
            typed = CaptureMethod(context["capture_method_typed"])
            assert context["capture_method"] == legacy_names[typed]
            for key in ("asset_id", "sha256", "stage", "capture_method_typed",
                        "source_kind", "channel_view", "lease_id"):
                assert context[key]
            assert context["stage"] == str(
                next(asset.stage for asset in rendered["assets"]
                     if asset.source_guid == entry["guid"])
            )

    with ProjectSession(bridge).capture_audio(
            tmp_path / "accessor", section=Section(0.0, 1.0),
            capture_mode="accessor") as sampled:
        by_guid = {entry["guid"]: entry for entry in
                   [*sampled["tracks"], sampled["master"]]}
        assert by_guid[TRACK_A]["capture"]["capture_method_typed"] == "audio_accessor"
        assert by_guid[TRACK_A]["capture"]["capture_method"] == "reaper_audio_accessor"
        assert by_guid[MASTER_GUID]["capture"]["capture_method_typed"] == "native_render"
        assert (by_guid[MASTER_GUID]["capture"]["capture_method"]
                == "reaper_native_offline_render")
        assets = _asset_by_guid(sampled)
        assert assets[TRACK_A].capture_method is CaptureMethod.audio_accessor
        assert assets[TRACK_A].stage is Stage.take_sum_pre_fx
        assert assets[MASTER_GUID].capture_method is CaptureMethod.native_render
        assert assets[MASTER_GUID].stage is Stage.master_mix_post_fx


def test_capture_refuses_project_growth_while_a_tail_is_requested(
        tmp_path: Path) -> None:
    bridge = FakeCaptureBridge(drift="track")
    root = tmp_path / "artifacts"

    with pytest.raises(BridgeUnavailable, match="added or removed project objects"):
        with ProjectSession(bridge).capture_audio(
                root, section=Section(0.0, 1.0), tail_ms=250):
            pass

    assert not list(root.glob("job-*"))


def test_capture_refuses_a_render_that_adds_stems_to_the_playlist(
        tmp_path: Path) -> None:
    bridge = FakeCaptureBridge(add_to_project=1.0)
    root = tmp_path / "artifacts"

    with pytest.raises(BridgeUnavailable, match="project playlist") as raised:
        with ProjectSession(bridge).capture_audio(root, section=Section(0.0, 1.0)):
            pass

    assert raised.value.details["RENDER_ADDTOPROJ"] == 1.0
    assert not list(root.glob("job-*"))
