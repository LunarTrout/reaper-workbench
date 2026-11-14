from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from reaper_audio.daw.full_capture import (
    MAX_ANALYSIS_WORKERS,
    FullCaptureService,
    _analysis_timing_fields,
    _analysis_worker_count,
    _analyze_rendered_files,
    _master_validation,
)
from reaper_audio.daw.session import ProjectRevision


def _assets(tmp_path: Path) -> list[tuple[str, str]]:
    payloads: list[tuple[str, str]] = []
    for index, frequency in enumerate((220.0, 440.0, 660.0)):
        path = tmp_path / f"asset-{index}.wav"
        sr = 48000
        t = np.arange(sr, dtype=np.float64) / sr
        samples = np.column_stack((np.sin(2 * np.pi * frequency * t),
                                   np.sin(2 * np.pi * frequency * t + 0.2)))
        sf.write(path, samples, sr, subtype="PCM_24")
        payloads.append((f"{{TRACK-{index}}}", str(path)))
    return payloads


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def test_analysis_parallel_is_byte_and_order_identical(tmp_path: Path) -> None:
    payloads = _assets(tmp_path)
    serial = _analyze_rendered_files(payloads, 1)
    parallel = _analyze_rendered_files(payloads, 3)
    assert [item["source_guid"] for item in parallel] == [
        item["source_guid"] for item in serial]
    assert [_canonical(item["analysis"]) for item in parallel] == [
        _canonical(item["analysis"]) for item in serial]
    assert [(item["sample_rate"], item["channels"], item["frames"],
             item["duration_s"]) for item in parallel] == [
        (item["sample_rate"], item["channels"], item["frames"],
         item["duration_s"]) for item in serial]


def test_analysis_worker_count_is_bounded() -> None:
    assert MAX_ANALYSIS_WORKERS == 8
    assert _analysis_worker_count(None, 42, window_s=20.0) == 8
    assert _analysis_worker_count(None, 42, window_s=300.0) == 4
    assert _analysis_worker_count(None, 42) == 4
    assert _analysis_worker_count(None, 3, window_s=20.0) == 3
    assert _analysis_worker_count(4, 42, window_s=300.0) == 4
    with pytest.raises(Exception, match="analysis_workers"):
        _analysis_worker_count(0, 3)
    with pytest.raises(Exception, match="analysis_workers"):
        _analysis_worker_count(MAX_ANALYSIS_WORKERS + 1, 42)


def test_analysis_timing_fields_do_not_label_sum_as_dsp_elapsed() -> None:
    fields = _analysis_timing_fields(
        analysis_wall_s=50.0,
        analysis_pool_wall_s=48.0,
        dsp_worker_elapsed_sum_s=160.0,
        wav_decode_worker_elapsed_sum_s=0.5,
    )
    assert fields["timings_schema"] == "capture-timings/2"
    assert fields["analysis_wall_s"] == 50.0
    assert fields["analysis_pool_wall_s"] == 48.0
    assert fields["dsp_worker_elapsed_sum_s"] == 160.0
    assert fields["wav_decode_worker_elapsed_sum_s"] == 0.5
    assert fields["dsp_measure_s"] == 48.0
    assert fields["dsp_measure_s"] != fields["dsp_worker_elapsed_sum_s"]


def test_analysis_worker_failure_is_propagated(tmp_path: Path) -> None:
    missing = tmp_path / "missing.wav"
    with pytest.raises(Exception):
        _analyze_rendered_files([("{TRACK-BAD}", str(missing))], 2)


def test_master_validation_does_not_hide_silence() -> None:
    silent = _master_validation({
        "{MASTER}": {"metrics": {
            "peak_dbfs": {"value": -160.0},
            "rms_dbfs": {"value": -160.0},
        }}
    }, "{MASTER}")
    assert silent["status"] == "silent"
    assert silent["silent"] is True
    assert silent["valid"] is False

    present = _master_validation({
        "{MASTER}": {"metrics": {
            "peak_dbfs": {"value": -1.0},
            "rms_dbfs": {"value": -12.0},
        }}
    }, "{MASTER}")
    assert present["status"] == "signal_present"
    assert present["valid"] is True


def _restore_context(*, cursor: float, play_position: float) -> dict:
    return {
        "tracks": [{"guid": "{TRACK}", "mute": False, "solo": 0,
                    "volume": 1.0, "pan": 0.0, "items": []}],
        "master": {"guid": "{MASTER}", "mute": False, "solo": 0,
                   "volume": 1.0, "pan": 0.0, "items": []},
        "selection": {"track_guids": [], "time_start_s": 0, "time_end_s": 0},
        "transport": {"play_state": 0, "cursor_s": cursor,
                      "edit_cursor_s": cursor, "play_position_s": play_position},
    }


def test_restore_report_verifies_edit_cursor_not_observed_play_position() -> None:
    revision = ProjectRevision.from_dict(
        {"project_id": "p", "change_count": 2, "path": "p.rpp"})
    report = FullCaptureService._restore_report(
        before=_restore_context(cursor=33.6, play_position=12.020136),
        after=_restore_context(cursor=33.6, play_position=29.992381),
        before_digest={"fields": {}}, after_digest={"fields": {}},
        revision_before=revision, revision_after=revision,
        last_render={"restore_report": {"dirty_before": 0, "dirty_after": 0}},
    )
    assert report["transport_position_restored"] is True
    assert report["transport_restored"] is True
    assert report["play_position_matches"] is None
    assert report["play_position_restorable"] is False
    assert report["play_position_observed_changed"] is True
    assert report["play_position_before_s"] == 12.020136
    assert report["play_position_observed_s"] == 29.992381


def test_restore_report_fails_if_edit_cursor_moved() -> None:
    revision = ProjectRevision.from_dict(
        {"project_id": "p", "change_count": 2, "path": "p.rpp"})
    report = FullCaptureService._restore_report(
        before=_restore_context(cursor=33.6, play_position=12.0),
        after=_restore_context(cursor=34.0, play_position=12.0),
        before_digest={"fields": {}}, after_digest={"fields": {}},
        revision_before=revision, revision_after=revision,
        last_render={"restore_report": {"dirty_before": 0, "dirty_after": 0}},
    )
    assert report["transport_position_restored"] is False
    assert report["transport_restored"] is False
