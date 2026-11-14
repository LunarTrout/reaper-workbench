from __future__ import annotations

import hashlib
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from reaper_audio.analysis.artifacts import AudioArtifactWorkspace
from reaper_audio.daw.analysis_pipeline import BoundedAnalysisPool
from reaper_audio.daw.full_capture import (
    FullCaptureService,
    _apply_master_classification,
    _master_attempt_entry,
    _overlap_timing,
    _wave_master_validation,
)
from reaper_audio.errors import BridgeUnavailable, ValidationError


class _FakeFuture:
    def __init__(self) -> None:
        self._done = False
        self._value: Any = None
        self._exception: BaseException | None = None
        self.cancelled = False

    def done(self) -> bool:
        return self._done

    def result(self) -> Any:
        if self._exception is not None:
            raise self._exception
        if not self._done:
            raise RuntimeError("future is not done")
        return self._value

    def cancel(self) -> None:
        self.cancelled = True
        self._done = True

    def complete(self, value: Any) -> None:
        self._value = value
        self._done = True

    def fail(self, error: BaseException) -> None:
        self._exception = error
        self._done = True


class _FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, tuple[str, str], _FakeFuture]] = []
        self.shutdown_calls: list[dict[str, Any]] = []

    def submit(self, function: Any, payload: tuple[str, str]) -> _FakeFuture:
        future = _FakeFuture()
        self.calls.append((function, payload, future))
        return future

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        self.shutdown_calls.append({"wait": wait, "cancel_futures": cancel_futures})


def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before the deadline")


def _result(guid: str) -> dict[str, Any]:
    return {"source_guid": guid, "analysis": {"guid": guid},
            "sample_rate": 48000, "channels": 2, "frames": 100,
            "duration_s": 100 / 48000, "decode_s": 0.01, "dsp_s": 0.02}


def test_pool_never_exceeds_the_validated_in_flight_bound() -> None:
    executor = _FakeExecutor()
    pool = BoundedAnalysisPool(2, executor_factory=lambda _workers: executor)
    try:
        pool.submit([(f"{{G{i}}}", f"p{i}") for i in range(10)])
        assert [call[1] for call in executor.calls] == [
            ("{G0}", "p0"), ("{G1}", "p1")]
        assert len(pool._futures) == 2
        assert len(pool._ready) == 8
        assert pool.stats()["peak_in_flight"] == 2

        payload = executor.calls[0][1]
        executor.calls[0][2].complete(_result(payload[0]))
        _wait_until(lambda: len(executor.calls) >= 3)
        assert pool.stats()["peak_in_flight"] <= 2
        assert len(pool._ready) == 7
    finally:
        pool.abort()


def test_pool_maps_results_by_guid_and_preserves_requested_order() -> None:
    executor = _FakeExecutor()
    pool = BoundedAnalysisPool(3, executor_factory=lambda _workers: executor)
    try:
        pool.submit([("{A}", "a"), ("{B}", "b"), ("{C}", "c")])
        _wait_until(lambda: len(executor.calls) == 3)
        # Complete out of submission order.
        for payload, future in (
            (executor.calls[2][1], executor.calls[2][2]),
            (executor.calls[0][1], executor.calls[0][2]),
            (executor.calls[1][1], executor.calls[1][2]),
        ):
            future.complete(_result(payload[0]))
        pool.finish()
        results = pool.wait_all()
        assert set(results) == {"{A}", "{B}", "{C}"}
        ordered = pool.result_list(["{B}", "{A}", "{C}"])
        assert [item["source_guid"] for item in ordered] == ["{B}", "{A}", "{C}"]
    finally:
        pool.close(wait_workers=False)


def test_pool_propagates_a_worker_failure_and_cancels_the_rest() -> None:
    executor = _FakeExecutor()
    pool = BoundedAnalysisPool(2, executor_factory=lambda _workers: executor)
    try:
        pool.submit([(f"{{G{i}}}", f"p{i}") for i in range(4)])
        executor.calls[0][2].fail(RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            pool.wait_all()
        assert pool._error is not None
    finally:
        pool.abort()


def test_pool_abort_cancels_pending_futures_without_waiting() -> None:
    executor = _FakeExecutor()
    pool = BoundedAnalysisPool(2, executor_factory=lambda _workers: executor)
    pool.submit([(f"{{G{i}}}", f"p{i}") for i in range(6)])
    assert len(executor.calls) == 2
    pool.abort()
    assert all(call[2].cancelled for call in executor.calls)
    assert executor.shutdown_calls[-1] == {"wait": False, "cancel_futures": True}


def test_pool_rejects_an_invalid_worker_count() -> None:
    with pytest.raises(ValidationError):
        BoundedAnalysisPool(0)
    with pytest.raises(ValidationError):
        BoundedAnalysisPool(True)


def test_pool_real_workers_match_serial_analysis(tmp_path: Path) -> None:
    payloads: list[tuple[str, str]] = []
    for index, frequency in enumerate((220.0, 330.0, 440.0)):
        path = tmp_path / f"asset-{index}.wav"
        rate = 48000
        t = np.arange(rate // 2, dtype=np.float64) / rate
        samples = np.column_stack((
            np.sin(2 * np.pi * frequency * t),
            np.sin(2 * np.pi * frequency * t + 0.1),
        ))
        sf.write(path, samples, rate, subtype="PCM_24")
        payloads.append((f"{{TRACK-{index}}}", str(path)))

    from reaper_audio.daw.full_capture import _analyze_rendered_files
    serial = _analyze_rendered_files(payloads, 1)
    pool = BoundedAnalysisPool(2)
    try:
        pool.submit(payloads)
        pool.finish()
        pool.wait_all()
        results = pool.result_list([guid for guid, _ in payloads])
    finally:
        pool.close(wait_workers=False)
    assert [item["source_guid"] for item in results] == [
        item["source_guid"] for item in serial]
    assert [item["analysis"] for item in results] == [
        item["analysis"] for item in serial]


def _service(tmp_path: Path) -> FullCaptureService:
    service = FullCaptureService(SimpleNamespace(bridge=SimpleNamespace()),
                                 tmp_path / "archive")
    service._profile = {"admit_s": 0.0}
    return service


def _wav(path: Path, *, channels: int = 2, frames: int = 480) -> None:
    t = np.arange(frames, dtype=np.float64) / 48000
    samples = np.zeros((frames, channels))
    for channel in range(channels):
        samples[:, channel] = 0.2 * np.sin(2 * np.pi * (200 + channel * 40) * t)
    sf.write(path, samples, 48000, subtype="PCM_24")


def _rendered_files(workspace: AudioArtifactWorkspace,
                    guids: list[str], *, channels: int = 2
                    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    files: list[dict[str, Any]] = []
    tracks: dict[str, dict[str, Any]] = {}
    for index, guid in enumerate(guids):
        path = workspace.directory / f"track-{index}.wav"
        _wav(path, channels=channels)
        files.append({
            "source_guid": guid,
            "path": str(path),
            "bytes": path.stat().st_size,
            "is_master": False,
            "name": f"bridge-{index}",
            "track_index": index,
        })
        tracks[guid] = {"guid": guid, "index": index + 10, "name": f"snapshot-{index}"}
    rendered = {"files": files, "channels": channels, "sample_rate": 48000,
                "_elapsed_s": 0.25}
    return rendered, tracks


def test_admission_registers_exact_planned_guids_in_plan_order(tmp_path: Path) -> None:
    workspace = AudioArtifactWorkspace(tmp_path / "incoming")
    try:
        rendered, tracks = _rendered_files(workspace, ["{B}", "{A}"])
        service = _service(tmp_path)
        items = service._admit_track_batch(
            rendered, ["{A}", "{B}"], tracks, workspace)
        assert [item["source_guid"] for item in items] == ["{A}", "{B}"]
        assert [item["track_index"] for item in items] == [11, 10]
        assert [item["name"] for item in items] == ["snapshot-1", "snapshot-0"]
        for item in items:
            assert item["sha256"] == hashlib.sha256(
                Path(item["path"]).read_bytes()).hexdigest()
            assert item["bytes"] == Path(item["path"]).stat().st_size
    finally:
        workspace.close()


def test_admission_rejects_missing_duplicate_and_master_rows(tmp_path: Path) -> None:
    workspace = AudioArtifactWorkspace(tmp_path / "incoming")
    try:
        service = _service(tmp_path)
        rendered, tracks = _rendered_files(workspace, ["{A}", "{B}"])
        with pytest.raises(BridgeUnavailable, match="planned tracks"):
            service._admit_track_batch(rendered, ["{A}"], tracks, workspace)

        duplicate = {"files": [dict(rendered["files"][0]),
                               dict(rendered["files"][0])],
                     "channels": 2, "sample_rate": 48000}
        with pytest.raises(BridgeUnavailable, match="duplicate"):
            service._admit_track_batch(duplicate, ["{A}"], tracks, workspace)

        master = {"files": [dict(rendered["files"][0], is_master=True)],
                  "channels": 2, "sample_rate": 48000}
        with pytest.raises(BridgeUnavailable, match="master"):
            service._admit_track_batch(master, ["{A}"], tracks, workspace)
    finally:
        workspace.close()


def test_admission_checks_declared_size_and_wav_shape(tmp_path: Path) -> None:
    workspace = AudioArtifactWorkspace(tmp_path / "incoming")
    try:
        service = _service(tmp_path)
        rendered, tracks = _rendered_files(workspace, ["{A}"])
        wrong_size = {"files": [dict(rendered["files"][0],
                                     bytes=rendered["files"][0]["bytes"] + 1)],
                      "channels": 2, "sample_rate": 48000}
        with pytest.raises(BridgeUnavailable, match="size"):
            service._admit_track_batch(wrong_size, ["{A}"], tracks, workspace)

        wrong_shape = dict(rendered, channels=1)
        with pytest.raises(BridgeUnavailable, match="channel"):
            service._admit_track_batch(wrong_shape, ["{A}"], tracks, workspace)
    finally:
        workspace.close()


def test_admission_rejects_a_file_outside_the_workspace(tmp_path: Path) -> None:
    workspace = AudioArtifactWorkspace(tmp_path / "incoming")
    try:
        service = _service(tmp_path)
        outside = tmp_path / "outside.wav"
        _wav(outside)
        rendered = {
            "files": [{"source_guid": "{A}", "path": str(outside),
                       "bytes": outside.stat().st_size, "is_master": False}],
            "channels": 2, "sample_rate": 48000,
        }
        with pytest.raises(Exception):
            service._admit_track_batch(
                rendered, ["{A}"],
                {"{A}": {"guid": "{A}", "index": 0, "name": "a"}}, workspace)
    finally:
        workspace.close()


def test_master_entry_and_classification_preserve_evidence_shape(tmp_path: Path) -> None:
    signal = tmp_path / "master.wav"
    _wav(signal)
    validation = _wave_master_validation(signal, "{MASTER}")
    entry = _master_attempt_entry(
        attempt=1, item={"path": str(signal)}, path=signal,
        validation=validation, rendered={"channels": 2, "sample_rate": 48000,
                                         "_elapsed_s": 0.2,
                                         "render_settings_used": {"RENDER_SETTINGS": 0}},
        start_s=0.0, end_s=2.0, project_length_s=3.0,
    )
    assert entry["valid"] is True
    assert entry["selected"] is False
    assert entry["source_activity"] is None
    assert entry["bytes"] == signal.stat().st_size

    _apply_master_classification(entry, validation, {
        "expected_silence": False, "unexpected_silence": False,
        "silence_reason": None,
    }, {
        "audible_count": 2, "audible_guids": ["{A}", "{B}"],
        "all_silent": False,
    })
    assert entry["status"] == "signal_present"
    assert entry["valid"] is True
    assert entry["source_activity"]["audible_count"] == 2
    assert entry["source_activity"]["audible_guids"] == ["{A}", "{B}"]

    silent = tmp_path / "silent.wav"
    sf.write(silent, np.zeros((480, 2)), 48000, subtype="PCM_24")
    silent_validation = _wave_master_validation(silent, "{MASTER}")
    silent_entry = _master_attempt_entry(
        attempt=2, item={"path": str(silent)}, path=silent,
        validation=silent_validation, rendered={"channels": 2, "sample_rate": 48000},
        start_s=0.0, end_s=2.0, project_length_s=3.0,
    )
    _apply_master_classification(silent_entry, silent_validation, {
        "expected_silence": True, "unexpected_silence": False,
        "silence_reason": "all_rendered_sources_silent",
    }, {
        "audible_count": 0, "audible_guids": [], "all_silent": True,
    })
    assert silent_entry["status"] == "expected_silence"
    assert silent_entry["valid"] is True
    assert silent_entry["silence_reason"] == "all_rendered_sources_silent"


def test_overlap_timing_reports_only_measured_values() -> None:
    timing = _overlap_timing(
        started=100.0,
        first_submit_at=110.0,
        pool_created_at=109.0,
        last_render_end=140.0,
        analysis_end=200.0,
        render_ends=[105.0, 120.0, 140.0],
        pipeline_stats={"completion_times_s": {}},
        admission_times=[{"batch": 1, "tracks": 10, "at_s": 11.0}],
        batch_plan=None,
    )
    assert timing["first_analysis_submit_s"] == 10.0
    assert timing["last_render_end_s"] == 40.0
    assert timing["analysis_end_s"] == 100.0
    assert timing["analysis_render_overlap_s"] == 30.0
    assert timing["results_before_render_end"] == 0
    assert timing["renders_finished_before_analysis_end"] == 3
