from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from reaper_audio.context.full_capture import CaptureArchive
from reaper_audio.daw.full_capture import FullCaptureService
from reaper_audio.daw.session import ProjectRevision
from reaper_audio.errors import BridgeUnavailable


def _wav(path: Path, frequency: float) -> None:
    rate = 48000
    frames = 48000
    t = np.arange(frames, dtype=np.float64) / rate
    samples = np.column_stack((
        0.1 * np.sin(2 * np.pi * frequency * t),
        0.1 * np.sin(2 * np.pi * frequency * t + 0.2),
    ))
    sf.write(path, samples, rate, subtype="PCM_24")


class _FakeBridge:
    def __init__(self, tracks: list[dict[str, Any]], master: dict[str, Any],
                 digest: dict[str, Any]) -> None:
        self.tracks = tracks
        self.master = master
        self.digest = digest
        self.revision = {"project_id": "reaper:overlap-test",
                         "change_count": 2, "path": "overlap.rpp"}
        self.render_calls: list[dict[str, Any]] = []
        self.api_calls: list[tuple[str, Any, str, float]] = []
        self.fail_track_batch: int | None = None
        self.extra_master_in_track_batch = False
        self.mute_after_render: str | None = None
        self._track_batches = 0

    def project_digest(self, project_id: str | None = None) -> dict[str, Any]:
        return json.loads(json.dumps(self.digest))

    def capture_render(self, **kwargs: Any) -> dict[str, Any]:
        self.render_calls.append(json.loads(json.dumps(
            {key: value for key, value in kwargs.items()
             if key not in {"expected_revision", "output_dir"}})))
        output = Path(kwargs["output_dir"])
        job = str(kwargs["job_id"])
        master_only = bool(kwargs.get("master_only"))
        subset = kwargs.get("track_guids")
        if master_only:
            path = output / f"rali-{job}-master.wav"
            _wav(path, 110.0)
            files = [{
                "source_guid": self.master["guid"], "name": "MASTER",
                "track_index": -1, "is_master": True,
                "path": str(path), "bytes": path.stat().st_size,
            }]
        else:
            self._track_batches += 1
            if self.fail_track_batch == self._track_batches:
                raise BridgeUnavailable("simulated second batch failure")
            wanted = set(subset) if subset is not None else {
                track["guid"] for track in self.tracks}
            files = []
            for track in self.tracks:
                if track["guid"] not in wanted:
                    continue
                path = output / f"rali-{job}-{track['index']}.wav"
                _wav(path, 180.0 + track["index"] * 20)
                files.append({
                    "source_guid": track["guid"], "name": track["name"],
                    "track_index": track["index"], "is_master": False,
                    "path": str(path), "bytes": path.stat().st_size,
                })
            if self.extra_master_in_track_batch:
                # REAPER emitted an extra master target for a stems-only batch:
                # 6 files for 5 planned tracks. The capture must refuse it.
                extra = output / f"rali-{job}-0-MASTER.wav"
                _wav(extra, 90.0)
                files.append({
                    "source_guid": self.master["guid"], "name": "MASTER",
                    "track_index": -1, "is_master": True,
                    "path": str(extra), "bytes": extra.stat().st_size,
                })
            if self.mute_after_render:
                for track in self.tracks:
                    if track["guid"] == self.mute_after_render:
                        track["mute"] = True
        self.revision = dict(self.revision,
                             change_count=self.revision["change_count"] + 1)
        return {
            "project_id": self.revision["project_id"],
            "files": files,
            "revision": dict(self.revision, change_count=self.revision["change_count"] - 1),
            "revision_after": dict(self.revision),
            "capture_point": ("native_render_master_only" if master_only else
                              "native_render_selected_tracks_via_master"),
            "render_action": "File: Render project, using the most recent settings",
            "render_settings_used": {"RENDER_SETTINGS": 0 if master_only else 128},
            "target_count": len(files),
            "master_included": master_only,
            "track_guids": subset,
            "channels": 2,
            "sample_rate": 48000,
            "channels_requested": kwargs.get("channels"),
            "sample_rate_requested": kwargs.get("sample_rate"),
            "tail_ms_requested": kwargs.get("tail_ms"),
            "restore_report": {"restored": True, "failures": [],
                               "dirty_before": 0, "dirty_after": 0},
        }

    def call_api(self, name: str, handle: Any, parameter: str,
                 value: float) -> SimpleNamespace:
        self.api_calls.append((name, handle, parameter, value))
        if parameter == "B_MUTE":
            for track in self.tracks:
                if handle is not None and track["guid"] == getattr(handle, "stable_id", None):
                    track["mute"] = bool(value)
        return SimpleNamespace(project_after=dict(self.revision))


class _FakeSession:
    def __init__(self, bridge: _FakeBridge, context: dict[str, Any]) -> None:
        self.bridge = bridge
        self.context = context

    def snapshot(self, **kwargs: Any) -> dict[str, Any]:
        return json.loads(json.dumps(self.context))


def _context(tracks: list[dict[str, Any]], master: dict[str, Any]) -> dict[str, Any]:
    return {
        "revision": {"project_id": "reaper:overlap-test", "change_count": 2,
                     "path": "overlap.rpp"},
        "tracks": tracks,
        "master": master,
        "selection": {"track_guids": [], "time_start_s": 0.0, "time_end_s": 0.0},
        "transport": {"play_state": 0, "cursor_s": 0.0, "edit_cursor_s": 0.0,
                      "play_position_s": 0.0},
        "tempo": {"bpm": 120}, "length_s": 1.0,
        "markers": [], "regions": [],
    }


def _tracks() -> list[dict[str, Any]]:
    tracks = []
    topology = [(1, "FOLDER A"), (0, "A child 1"), (-1, "A child 2"),
                (0, "standalone 1"), (1, "FOLDER B"), (0, "B child 1"),
                (-1, "B child 2"), (0, "standalone 2")]
    for index, (depth, name) in enumerate(topology):
        tracks.append({
            "guid": f"{{TRACK-{index}}}", "index": index, "name": name,
            "folder_depth": depth, "items": [], "fx": [],
            "mute": False, "solo": 0, "volume": 1.0, "pan": 0.0,
            "sends": 0, "receives": 0, "hardware_sends": 0,
            "selected": False, "is_master": False,
        })
    return tracks


def _digest(tracks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "coverage": {name: True for name in (
            "tracks", "items", "automation", "fx_state", "sends",
            "project_settings", "tempo", "markers")},
        "fields": {"tracks": [
            {"guid": track["guid"], "folder_depth": track["folder_depth"],
             "sends": [], "hardware_outputs": []}
            for track in tracks
        ]},
    }


def test_capture_renders_master_first_overlaps_batches_and_maps_analysis(
        tmp_path: Path) -> None:
    tracks = _tracks()
    master = {"guid": "{MASTER}", "index": -1, "name": "", "is_master": True,
              "folder_depth": 0, "items": [], "fx": [], "mute": False,
              "solo": 0, "volume": 0.85, "pan": 0.0,
              "sends": 0, "receives": 0, "hardware_sends": 1, "selected": False}
    digest = _digest(tracks)
    bridge = _FakeBridge(tracks, master, digest)
    service = FullCaptureService(
        _FakeSession(bridge, _context(tracks, master)),  # type: ignore[arg-type]
        tmp_path / "archive",
    )
    result = service.capture(section=(0.0, 1.0), analysis_workers=2)

    assert result["ok"] is True
    assert result["capture_status"] == "valid"
    assert len(result["master_attempts"]) == 1
    assert result["master_attempts"][0]["selected"] is True
    assert result["master_attempts"][0]["source_activity"]["audible_count"] == 8

    calls = bridge.render_calls
    assert len(calls) == 3
    assert calls[0]["master_only"] is True
    assert calls[1]["track_guids"] == ["{TRACK-0}", "{TRACK-1}",
                                       "{TRACK-2}", "{TRACK-3}"]
    assert calls[2]["track_guids"] == ["{TRACK-4}", "{TRACK-5}",
                                       "{TRACK-6}", "{TRACK-7}"]
    assert calls[1]["expected_track_count"] == 4
    assert calls[2]["expected_track_count"] == 4

    timings = result["timings"]
    assert list(timings["batch_plan"]["batch_sizes"]) == [4, 4]
    assert timings["batch_plan"]["fallback_reason"] is None
    assert timings["master_first"] is True
    assert timings["first_analysis_submit_s"] is not None
    assert timings["analysis_render_overlap_s"] is not None
    assert timings["analysis_pool"]["peak_in_flight"] <= 2
    assert timings["analysis_workers"] == 2
    assert timings["analysis_engine"] == "spawn_process_pool"
    assert timings["batch_target_count"] == 8
    assert timings["batch_file_count"] == 8

    stored = CaptureArchive(tmp_path / "archive").read(result["capture_id"])
    assert len(stored.audio) == 9
    assert len(stored.analysis) == 9
    assert stored.audio[-1]["is_master"] is True
    assert [row["source_guid"] for row in stored.audio[:-1]] == [
        track["guid"] for track in tracks]
    assert stored.coverage["audio"] is True
    assert stored.coverage["analysis"] is True
    assert stored.relations["schema_version"] == 1
    relation_coverage = stored.relations["coverage"]["band_dominance"]
    assert relation_coverage["expected"] == 28
    assert relation_coverage["evaluated"] == 28
    assert stored.relations["coverage"]["phase_pair"]["status"] == "unavailable"
    assert stored.relations["reference_status"] == "not_supplied"
    pair_table = stored.relations["evaluations"]["band_dominance_pairs"]
    assert len(pair_table["rows"]) == 28
    assert list(pair_table["columns"])[-2:] == ["relation_id", "dep_set"]
    id_index = pair_table["columns"].index("relation_id")
    assert all(str(row[id_index]).startswith("rel_") for row in pair_table["rows"])
    assert stored.timings["relations_s"] >= 0.0
    assert result["panorama"]["relations"]["content_hash"] == (
        stored.relations["content_hash"])
    assert stored.timings["master_attempts"][0]["sha256"] == (
        result["master_attempts"][0]["sha256"])

    # The restore is a state guarantee, not a rewrite: a mute that already
    # matches the pre-capture value is not written again, because every write
    # advances the project counter the restore handle is bound to.
    assert bridge.api_calls == []
    assert all(track["mute"] is False for track in tracks)
    assert all(call[3] == 0.0 for call in bridge.api_calls)
    digest_after = bridge.digest
    assert digest_after == digest


def test_capture_default_short_window_uses_shared_policy(tmp_path: Path) -> None:
    tracks = _tracks()
    master = {"guid": "{MASTER}", "index": -1, "name": "", "is_master": True,
              "folder_depth": 0, "items": [], "fx": [], "mute": False,
              "solo": 0, "volume": 0.85, "pan": 0.0,
              "sends": 0, "receives": 0, "hardware_sends": 1, "selected": False}
    bridge = _FakeBridge(tracks, master, _digest(tracks))
    service = FullCaptureService(
        _FakeSession(bridge, _context(tracks, master)),  # type: ignore[arg-type]
        tmp_path / "archive",
    )
    result = service.capture(section=(0.0, 1.0))

    assert result["ok"] is True
    assert result["timings"]["analysis_workers"] == 8
    assert result["timings"]["analysis_worker_policy"]["window_class"] == "short"
    assert result["timings"]["analysis_worker_policy"]["workers"] == 8


def test_capture_failure_cancels_analysis_and_cleans_the_incoming_job(
        tmp_path: Path) -> None:
    tracks = _tracks()
    master = {"guid": "{MASTER}", "index": -1, "name": "", "is_master": True,
              "folder_depth": 0, "items": [], "fx": [], "mute": False,
              "solo": 0, "volume": 0.85, "pan": 0.0,
              "sends": 0, "receives": 0, "hardware_sends": 1, "selected": False}
    bridge = _FakeBridge(tracks, master, _digest(tracks))
    bridge.fail_track_batch = 2
    bridge.mute_after_render = "{TRACK-3}"
    service = FullCaptureService(
        _FakeSession(bridge, _context(tracks, master)),  # type: ignore[arg-type]
        tmp_path / "archive",
    )
    try:
        service.capture(section=(0.0, 1.0), analysis_workers=2)
    except BridgeUnavailable as exc:
        assert "simulated second batch failure" in str(exc)
    else:
        raise AssertionError("the capture should have failed")

    archive = CaptureArchive(tmp_path / "archive")
    assert archive.list_index() == []
    incoming = tmp_path / "archive" / ".incoming"
    assert not list(incoming.glob("job-*"))
    # The failure path restores the mute the isolation sweep left behind, and
    # writes nothing else: the live state already matches for every other track.
    assert len(bridge.api_calls) == 1
    assert bridge.api_calls[0][2] == "B_MUTE"
    assert bridge.api_calls[0][3] == 0.0
    assert all(track["mute"] is False for track in tracks)


def test_six_targets_for_five_planned_tracks_fail_closed(tmp_path: Path) -> None:
    """REAPER returning a master target inside a stems-only batch is fatal.

    Six rendered files for five planned tracks must abort the capture before any
    record is committed, and the finally-path must still restore the mute that
    the isolation sweep left behind.
    """
    tracks = _tracks()
    master = {"guid": "{MASTER}", "index": -1, "name": "", "is_master": True,
              "folder_depth": 0, "items": [], "fx": [], "mute": False,
              "solo": 0, "volume": 0.85, "pan": 0.0,
              "sends": 0, "receives": 0, "hardware_sends": 1, "selected": False}
    bridge = _FakeBridge(tracks, master, _digest(tracks))
    bridge.extra_master_in_track_batch = True
    bridge.mute_after_render = "{TRACK-0}"
    service = FullCaptureService(
        _FakeSession(bridge, _context(tracks, master)),  # type: ignore[arg-type]
        tmp_path / "archive",
    )

    with pytest.raises(BridgeUnavailable) as raised:
        service.capture(section=(0.0, 1.0), analysis_workers=2)

    assert "master file" in str(raised.value).lower(), str(raised.value)
    # Nothing committed: no index record, no capture directory, no leftover job.
    archive = CaptureArchive(tmp_path / "archive")
    assert archive.list_index() == []
    assert not list((tmp_path / "archive" / "captures").glob("*"))
    # The isolation mute was restored despite the failure.
    assert bridge.api_calls == [
        call for call in bridge.api_calls if call[2] == "B_MUTE" and call[3] == 0.0]
    assert len(bridge.api_calls) == 1
    assert all(track["mute"] is False for track in tracks)
