"""Asynchronous capture jobs: durable progress, per-source rendering, cancel.

The bridge and session are fakes, but everything below the bridge is the real
capture path: the worker renders one source per call, then hands the files to
``FullCaptureService.capture(pre_rendered=...)`` so the analysis, state fill and
content-addressed archive are exercised exactly as in production.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from reaper_audio.context.full_capture import CaptureArchive
from reaper_audio.daw.analysis_policy import AnalysisWorkerPlan
from reaper_audio.daw.capture_jobs import (
    JOB_SCHEMA,
    TERMINAL_STATES,
    cancel_capture_job,
    capture_job_result,
    capture_job_status,
    start_capture_job,
)
from reaper_audio.errors import BridgeUnavailable, ValidationError

PROJECT = "reaper:async-capture-test"
MASTER = "{MASTER-ASYNC}"


def _wav(path: Path, frequency: float, *, seconds: float = 0.2) -> None:
    rate = 48000
    frames = int(rate * seconds)
    t = np.arange(frames, dtype=np.float64) / rate
    samples = np.column_stack((
        0.1 * np.sin(2 * np.pi * frequency * t),
        0.1 * np.sin(2 * np.pi * frequency * t + 0.2),
    ))
    sf.write(path, samples, rate, subtype="PCM_24")


def _tracks(count: int = 3) -> list[dict[str, Any]]:
    return [{
        "guid": f"{{TRACK-{index}}}",
        "index": index,
        "name": f"Source {index}",
        "folder_depth": 0,
        "items": [],
        "fx": [],
        "mute": False,
        "solo": 0,
        "volume": 1.0,
        "pan": 0.0,
        "sends": 0,
        "receives": 0,
        "hardware_sends": 0,
        "selected": False,
        "is_master": False,
    } for index in range(count)]


def _master() -> dict[str, Any]:
    return {"guid": MASTER, "index": -1, "name": "", "is_master": True,
            "folder_depth": 0, "items": [], "fx": [], "mute": False,
            "solo": 0, "volume": 0.85, "pan": 0.0,
            "sends": 0, "receives": 0, "hardware_sends": 1, "selected": False}


def _context(tracks: list[dict[str, Any]], master: dict[str, Any]) -> dict[str, Any]:
    return {
        "revision": {"project_id": PROJECT, "change_count": 2,
                     "path": "async.rpp"},
        "tracks": copy.deepcopy(tracks),
        "master": copy.deepcopy(master),
        "selection": {"track_guids": [], "time_start_s": 0.0, "time_end_s": 0.0},
        "transport": {"play_state": 0, "cursor_s": 0.0, "edit_cursor_s": 0.0,
                      "play_position_s": 0.0},
        "tempo": {"bpm": 120}, "length_s": 0.2,
        "markers": [], "regions": [],
    }


def _digest(tracks: list[dict[str, Any]]) -> dict[str, Any]:
    from reaper_audio.context.revision import (DIGEST_REQUIRED_FIELDS,
                                               DIGEST_VERSION)
    return {
        "digest_version": DIGEST_VERSION,
        "change_count": 1,
        "missing": [],
        "coverage": {name: True for name in DIGEST_REQUIRED_FIELDS},
        "fields": {"tracks": [
            {"guid": track["guid"], "folder_depth": track["folder_depth"],
             "sends": [], "hardware_outputs": []}
            for track in tracks
        ]},
    }


class _FakeCaptureBridge:
    """One resident bridge, rendered one source per call.

    ``gate`` lets a test hold the first render open long enough to observe the
    running job document, request cancellation or release it.
    """

    def __init__(self, tracks: list[dict[str, Any]], master: dict[str, Any],
                 digest: dict[str, Any], archive_root: Path) -> None:
        self.tracks = tracks
        self.master = master
        self.digest = digest
        self.archive_root = Path(archive_root)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.job_documents: list[dict[str, Any]] = []
        self.rendered_rows: list[dict[str, Any]] = []
        self.gate = threading.Event()
        self.gate.set()
        self.first_render_started = threading.Event()
        self.fail_at_index: int | None = None
        self.revision = {"project_id": PROJECT, "change_count": 2,
                         "path": "async.rpp"}
        self.job_id: str | None = None
        self.output_dir: Path | None = None

    # -- reads ----------------------------------------------------------- #
    def project_digest(self, project_id: str | None = None) -> dict[str, Any]:
        return copy.deepcopy(self.digest)

    # -- capture job contract -------------------------------------------- #
    def capture_begin(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("begin", dict(kwargs)))
        self.job_id = str(kwargs["job_id"])
        self.output_dir = Path(kwargs["output_dir"])
        sources = [{"index": track["index"], "guid": track["guid"],
                    "name": track["name"], "is_master": False}
                   for track in self.tracks]
        sources.append({"index": -1, "guid": MASTER, "name": "MASTER",
                        "is_master": True})
        return {
            "job": self.job_id,
            "project_id": PROJECT,
            "revision": copy.deepcopy(self.revision),
            "revision_after": copy.deepcopy(self.revision),
            "sources": sources,
            "total": len(sources),
            "channels": 2 if kwargs.get("channels") != "mono" else 1,
            "sample_rate": int(kwargs.get("sample_rate") or 48000),
            "tail_s": int(kwargs.get("tail_ms") or 0) / 1000.0,
            "render_settings_used": {
                "RENDER_SETTINGS": float(128 + (8 << 16)), "RENDER_BOUNDSFLAG": 2.0,
                "RENDER_CHANNELS": 2.0, "RENDER_SRATE": 48000.0,
                "RENDER_TAILFLAG": 0.0, "RENDER_TAILMS": 0.0,
                "RENDER_ADDTOPROJ": 0.0,
            },
        }

    def capture_render_one(self, *, job: str, index: int,
                           output_dir: Any, timeout_s: float) -> dict[str, Any]:
        self.calls.append(("render_one", {"job": job, "index": index,
                                          "timeout_s": timeout_s}))
        if index == self.tracks[0]["index"]:
            self.first_render_started.set()
            assert self.gate.wait(timeout=30), "test gate was never released"
        # Read the durable job document at the moment of the render: every
        # previous source must already be recorded, and the file must parse.
        self.job_documents.append(json.loads(
            (self.archive_root / "jobs" / f"{job}.json").read_text(encoding="utf-8")))
        if self.fail_at_index is not None and index == self.fail_at_index:
            raise BridgeUnavailable("simulated bridge loss")
        output = Path(output_dir)
        is_master = index == -1
        if is_master:
            path = output / f"rali-{job}-MASTER.wav"
            _wav(path, 110.0)
        else:
            path = output / f"rali-{job}-{index + 1}-Source {index}.wav"
            _wav(path, 180.0 + index * 30)
        row = {
            "source_guid": MASTER if is_master else self.tracks[index]["guid"],
            "name": "MASTER" if is_master else self.tracks[index]["name"],
            "track_index": -1 if is_master else index,
            "is_master": is_master,
            "path": str(path),
            "bytes": path.stat().st_size,
        }
        self.rendered_rows.append(row)
        return {
            "job": job,
            "project_id": PROJECT,
            "revision": copy.deepcopy(self.revision),
            "revision_after": copy.deepcopy(self.revision),
            "capture_point": "native_render_async_job_source",
            "files": [dict(row)],
            "render_settings_requested": 0 if is_master else (128 + (8 << 16)),
            "render_settings_used": {
                "RENDER_SETTINGS": float(0 if is_master else (128 + (8 << 16))),
                "RENDER_BOUNDSFLAG": 2.0, "RENDER_CHANNELS": 2.0,
                "RENDER_SRATE": 48000.0, "RENDER_TAILFLAG": 0.0,
                "RENDER_TAILMS": 0.0, "RENDER_ADDTOPROJ": 0.0,
            },
            "channels": 2,
            "sample_rate": 48000,
            "tail_s": 0.0,
            "index": index,
            "is_master": is_master,
            "rendered_total": len(self.rendered_rows),
        }

    def capture_end(self, *, job: str, timeout_s: float) -> dict[str, Any]:
        self.calls.append(("end", {"job": job, "timeout_s": timeout_s}))
        return {
            "job": job,
            "project_id": PROJECT,
            "revision": copy.deepcopy(self.revision),
            "revision_after": copy.deepcopy(self.revision),
            "files": [dict(row) for row in self.rendered_rows],
            "rendered_count": len(self.rendered_rows),
            "channels": 2,
            "sample_rate": 48000,
            "tail_s": 0.0,
            "render_settings_used": {
                "RENDER_SETTINGS": float(128 + (8 << 16)), "RENDER_BOUNDSFLAG": 2.0,
                "RENDER_CHANNELS": 2.0, "RENDER_SRATE": 48000.0,
                "RENDER_TAILFLAG": 0.0, "RENDER_TAILMS": 0.0,
                "RENDER_ADDTOPROJ": 0.0,
            },
            "restore_report": {"restored": True, "failures": [],
                               "dirty_before": 0, "dirty_after": 0},
            "capture_point": "native_render_async_job",
        }


class _FakeSession:
    def __init__(self, bridge: _FakeCaptureBridge, context: dict[str, Any]) -> None:
        self.bridge = bridge
        self.context = context
        self.project_id: str | None = None
        self.archive_root: str | None = None

    def snapshot(self, **kwargs: Any) -> dict[str, Any]:
        return copy.deepcopy(self.context)

    def digest(self, project_id: str | None = None) -> dict[str, Any]:
        return copy.deepcopy(self.bridge.digest)


def _serial_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the capture path serial: no process pool in a unit test."""
    def plan(*, asset_count: int, window_s: float | None = None,
             requested: int | None = None, **_: Any) -> AnalysisWorkerPlan:
        return AnalysisWorkerPlan(
            workers=1 if asset_count else 0, requested=requested,
            asset_count=asset_count, window_s=window_s,
            window_class="short", per_worker_envelope_mb=1.0,
            memory_budget_mb=None, memory_limit_workers=None, reason="test")

    monkeypatch.setattr("reaper_audio.daw.full_capture.resolve_analysis_workers", plan)


def _wait_for(archive_root: Path, job_id: str, predicate: Any,
              timeout_s: float = 60.0) -> dict[str, Any]:
    """Bounded poll; never a busy wait longer than the deadline."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        document = capture_job_status(archive_root, job_id)
        if predicate(document):
            return document
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not reach the expected state: "
                         f"{capture_job_status(archive_root, job_id)}")


def test_capture_job_renders_one_source_per_call_and_archives(tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    _serial_workers(monkeypatch)
    tracks = _tracks(3)
    master = _master()
    archive = tmp_path / "archive"
    bridge = _FakeCaptureBridge(tracks, master, _digest(tracks), archive)
    bridge.gate.clear()  # hold the first source open
    session = _FakeSession(bridge, _context(tracks, master))

    started = start_capture_job(session, archive, section=(0.0, 0.2))
    job_id = started["job_id"]
    assert started["state"] == "queued"
    assert bridge.first_render_started.wait(timeout=30)

    running = capture_job_status(archive, job_id)
    assert running["state"] == "running"
    assert running["progress"]["total"] == len(tracks) + 1
    assert running["progress"]["done"] == 0
    assert [row["guid"] for row in running["sources"]] == [
        *(track["guid"] for track in tracks), MASTER]
    assert running["bridge"]["job_handle"] == job_id

    bridge.gate.set()
    document = _wait_for(archive, job_id,
                         lambda row: row["state"] in TERMINAL_STATES)
    assert document["state"] == "done", document["error"]
    assert document["capture_id"]
    assert document["progress"]["done"] == document["progress"]["total"]
    assert document["state_fill"]["ok"] is True
    assert document["error"] is None

    # Exactly one begin, then one render per source in index order, then end.
    operations = [name for name, _ in bridge.calls]
    assert operations == ["begin", *(["render_one"] * (len(tracks) + 1)), "end"]
    assert [payload["index"] for name, payload in bridge.calls
            if name == "render_one"] == [*range(len(tracks)), -1]
    # The bridge job is ended exactly once and the worker never uses the
    # synchronous all-at-once operation.
    assert operations.count("end") == 1

    # Every document read from disk during a render parsed and carried the
    # progress of the sources rendered before it.
    assert [row["progress"]["done"] for row in bridge.job_documents] == (
        list(range(len(tracks) + 1)))
    for row in bridge.job_documents:
        assert row["schema"] == JOB_SCHEMA
        assert row["state"] == "running"
        assert len(row["rendered"]) == row["progress"]["done"]

    result = capture_job_result(archive, job_id)
    assert result["state"] == "done"
    assert result["capture_id"] == document["capture_id"]
    assert result["state_fill"]["ok"] is True
    assert result["error"] is None

    stored = CaptureArchive(archive).read(document["capture_id"])
    assert len(stored.audio) == len(tracks) + 1
    assert len(stored.analysis) == len(tracks) + 1
    assert stored.audio[-1]["is_master"] is True
    assert [row["source_guid"] for row in stored.audio[:-1]] == [
        track["guid"] for track in tracks]
    # The async render contract is visible in the record timings.
    assert stored.timings["async_render"]["scheduling"] == "per_source_bridge_job"
    assert stored.timings["async_render"]["sources_rendered"] == len(tracks) + 1
    # One selected track through the master keeps the production render mode.
    assert stored.timings["batch_render_settings"]["RENDER_SETTINGS"] == float(
        128 + (8 << 16))
    assert stored.timings["master_render_settings"]["RENDER_SETTINGS"] == 0.0

    # Atomic writes leave no temporary documents behind.
    assert sorted(path.name for path in (archive / "jobs").glob("*.tmp")) == []


def test_capture_job_cancel_restores_and_marks_cancelled(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    _serial_workers(monkeypatch)
    tracks = _tracks(2)
    master = _master()
    archive = tmp_path / "archive"
    bridge = _FakeCaptureBridge(tracks, master, _digest(tracks), archive)
    bridge.gate.clear()
    session = _FakeSession(bridge, _context(tracks, master))

    job_id = start_capture_job(session, archive, section=(0.0, 0.2))["job_id"]
    assert bridge.first_render_started.wait(timeout=30)

    cancelled = cancel_capture_job(archive, job_id)
    assert cancelled["cancel_requested"] is True
    assert cancelled["state"] == "running"  # one render is already in flight

    bridge.gate.set()
    document = _wait_for(archive, job_id,
                         lambda row: row["state"] in TERMINAL_STATES)
    assert document["state"] == "cancelled"
    assert document["capture_id"] is None
    assert document["progress"]["done"] == 1
    assert len(document["rendered"]) == 1
    # Capture end still ran, so the project state was restored despite cancel.
    assert [name for name, _ in bridge.calls].count("end") == 1
    assert [payload["index"] for name, payload in bridge.calls
            if name == "render_one"] == [0]
    # Cancelling a terminal job is a no-op.
    again = cancel_capture_job(archive, job_id)
    assert again["state"] == "cancelled"
    assert again["cancel_requested"] is True


def test_capture_job_bridge_failure_keeps_progress_and_marks_failed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _serial_workers(monkeypatch)
    tracks = _tracks(3)
    master = _master()
    archive = tmp_path / "archive"
    bridge = _FakeCaptureBridge(tracks, master, _digest(tracks), archive)
    bridge.fail_at_index = 1
    session = _FakeSession(bridge, _context(tracks, master))

    job_id = start_capture_job(session, archive, section=(0.0, 0.2))["job_id"]
    document = _wait_for(archive, job_id,
                         lambda row: row["state"] in TERMINAL_STATES)
    assert document["state"] == "failed"
    assert document["error"]["code"] == "bridge_unavailable"
    assert "simulated bridge loss" in document["error"]["message"]
    # Partial progress is preserved rather than reset.
    assert document["progress"]["done"] == 1
    assert document["progress"]["total"] == len(tracks) + 1
    assert len(document["rendered"]) == 1
    assert document["capture_id"] is None
    # The failure path tries to restore through capture_end.
    assert [name for name, _ in bridge.calls].count("end") == 1
    assert CaptureArchive(archive).list_index() == []
    # Partial renders are reclaimed; a failed job leaves no artifact directory.
    assert not list((archive / ".incoming").glob("job-*"))
    result = capture_job_result(archive, job_id)
    assert result == {"state": "failed", "capture_id": None, "state_fill": None,
                      "error": document["error"]}


def test_capture_job_validates_the_request_before_creating_a_job(tmp_path: Path) -> None:
    session = _FakeSession(_FakeCaptureBridge([], _master(), {}, tmp_path / "unused"),
                           {})
    with pytest.raises(ValidationError):
        start_capture_job(session, tmp_path / "archive", channels="quad")
    with pytest.raises(ValidationError):
        start_capture_job(session, tmp_path / "archive", section=(2.0, 1.0))
    with pytest.raises(ValidationError):
        start_capture_job(session, tmp_path / "archive", tail_ms=-1)
    with pytest.raises(ValidationError):
        capture_job_status(tmp_path / "archive", "../escape")
    assert not (tmp_path / "archive" / "jobs").exists()


def test_bridge_client_async_capture_ops_validate_the_wire_contract(tmp_path: Path) -> None:
    """The client refuses a job/source/file that does not match its request."""
    from reaper_audio.reaper.bridge_client import ReaperBridge

    output = tmp_path / "out"
    output.mkdir()
    track_wav = output / "track.wav"
    master_wav = output / "master.wav"
    _wav(track_wav, 200.0)
    _wav(master_wav, 100.0)
    revision = {"project_id": PROJECT, "change_count": 2, "path": "async.rpp"}
    settings = {"RENDER_SETTINGS": float(128 + (8 << 16)), "RENDER_BOUNDSFLAG": 2.0,
                "RENDER_CHANNELS": 2.0, "RENDER_SRATE": 48000.0,
                "RENDER_TAILFLAG": 0.0, "RENDER_TAILMS": 0.0,
                "RENDER_ADDTOPROJ": 0.0}
    track_row = {"source_guid": "{TRACK-0}", "name": "Source 0",
                 "track_index": 0, "is_master": False,
                 "path": str(track_wav), "bytes": track_wav.stat().st_size}
    master_row = {"source_guid": MASTER, "name": "MASTER", "track_index": -1,
                  "is_master": True, "path": str(master_wav),
                  "bytes": master_wav.stat().st_size}
    responses: dict[str, Any] = {
        "project.capture_begin": {
            "job": "job-1", "project_id": PROJECT,
            "revision": copy.deepcopy(revision),
            "revision_after": copy.deepcopy(revision),
            "sources": [{"index": 0, "guid": "{TRACK-0}", "name": "Source 0",
                         "is_master": False},
                        {"index": -1, "guid": MASTER, "name": "MASTER",
                         "is_master": True}],
            "total": 2, "channels": 2, "sample_rate": 48000, "tail_s": 0.0,
            "render_settings_used": dict(settings),
        },
        "project.capture_render_one": {
            "job": "job-1", "project_id": PROJECT,
            "revision": copy.deepcopy(revision),
            "revision_after": copy.deepcopy(revision),
            "capture_point": "native_render_async_job_source",
            "files": [dict(track_row)],
            "render_settings_requested": 128,
            "render_settings_used": dict(settings),
            "channels": 2, "sample_rate": 48000, "tail_s": 0.0,
            "index": 0, "is_master": False, "rendered_total": 1,
        },
    }
    responses["project.capture_end"] = {
        "job": "job-1", "project_id": PROJECT,
        "revision": copy.deepcopy(revision),
        "revision_after": copy.deepcopy(revision),
        "files": [dict(track_row), dict(master_row)], "rendered_count": 2,
        "channels": 2, "sample_rate": 48000, "tail_s": 0.0,
        "render_settings_used": dict(settings),
        "restore_report": {"restored": True, "failures": []},
        "capture_point": "native_render_async_job",
    }
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_call(operation: str, arguments: dict[str, Any] | None = None,
                  *, timeout_s: float = 0.0) -> Any:
        calls.append((operation, dict(arguments or {})))
        return copy.deepcopy(responses[operation])

    bridge = ReaperBridge(runtime_dir=tmp_path / "runtime")
    bridge.call = fake_call  # type: ignore[method-assign]

    begin = bridge.capture_begin(expected_revision=revision, output_dir=output,
                                 job_id="job-1", section_start_s=0.0,
                                 section_end_s=0.2)
    assert begin["total"] == 2
    assert calls[-1][1]["sample_rate"] == 0  # null means project rate
    assert calls[-1][1]["output_dir"] == str(output.resolve())

    one = bridge.capture_render_one(job="job-1", index=0, output_dir=output)
    assert one["files"][0]["source_guid"] == "{TRACK-0}"
    assert calls[-1] == ("project.capture_render_one", {"job": "job-1", "index": 0})

    # A file for another source must not be accepted as this index's render.
    responses["project.capture_render_one"]["files"][0]["track_index"] = 1
    with pytest.raises(BridgeUnavailable):
        bridge.capture_render_one(job="job-1", index=0, output_dir=output)
    responses["project.capture_render_one"]["files"][0]["track_index"] = 0

    end = bridge.capture_end(job="job-1")
    assert end["files"][0]["source_guid"] == "{TRACK-0}"
    assert end["restore_report"]["restored"] is True


def test_resident_lua_declares_the_async_capture_contract() -> None:
    """Static guard: the additive job operations and their yield survive edits."""
    from reaper_audio.reaper.bridge_lifecycle import bridge_script_path
    text = bridge_script_path().read_text(encoding="utf-8", errors="replace")
    assert '"b27-2026-11-21"' in text
    # The per-source track render must keep the production parallel-via-master
    # mode; the non-parallel 128 mode is not the archived production audio.
    assert "capture_job_track_render_settings = 128 + (8 << 16)" in text
    for operation in ("project.capture_begin", "project.capture_render_one",
                      "project.capture_end"):
        assert f'["{operation}"]' in text, operation
    # The synchronous all-at-once operation must stay registered and untouched.
    assert '["project.capture_render"] = project_capture_render' in text
    # capture_render_one must yield to REAPER before returning.
    render_one = text.split("local function capture_render_one", 1)[1]
    render_one = render_one.split("local function capture_end", 1)[0]
    assert "reaper.defer(function() end)" in render_one
    begin = text.split("local function capture_begin", 1)[1].split(
        "local function capture_render_one", 1)[0]
    end = text.split("local function capture_end", 1)[1].split(
        "local function projects_list", 1)[0]
    assert "capture_job_save_state" in begin
    assert "capture_job_restore_state" in end
