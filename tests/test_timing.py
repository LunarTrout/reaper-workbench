from __future__ import annotations

import numpy as np

from reaper_audio.analysis.timing import (build_timing_plan, detect_timing_events,
                                          grid_times, match_events)
from reaper_audio.models import AudioBuffer


class _Tempo:
    """120 BPM, constant: one quarter note = 0.5 s."""

    def time_to_qn(self, time_s: float) -> float:
        return float(time_s) / 0.5

    def qn_to_time(self, qn: float) -> float:
        return float(qn) * 0.5


def _clicks(times, rate=48_000, duration=2.0, amplitude=0.8):
    samples = np.zeros((int(duration * rate), 2), dtype=np.float64)
    for time_s in times:
        index = int(round(time_s * rate))
        length = min(int(0.002 * rate), samples.shape[0] - index)
        if length > 0:
            samples[index:index + length, :] = amplitude
    return AudioBuffer(samples, rate)


def _click_events(times):
    return [{"id": f"evt_{index:05d}", "source_s": time_s, "sample": 0,
             "kind": "attack", "confidence": 1.0}
            for index, time_s in enumerate(times, start=1)]


def test_detects_attacks_and_improves_their_median_grid_error() -> None:
    truth = np.arange(0.25, 1.75, 0.25)
    jitter = np.array([0.004, -0.006, 0.008, -0.003, 0.005, -0.007])
    audio = _clicks(truth + jitter)
    analysis = detect_timing_events(audio, sensitivity=2.0, min_gap_s=0.05)
    detected = [row["source_s"] for row in analysis["events"]]

    assert len(detected) == len(truth)
    assert np.median(np.abs(np.array(detected) - truth)) < 0.010

    grid = grid_times(_Tempo(), 0.0, 2.0, subdivisions=4)
    matched = match_events(analysis["events"], grid, tolerance_s=0.040)
    assert matched["matched"] >= len(truth) - 1
    plan = build_timing_plan(analysis["events"], matched["assignments"],
                             strength=1.0, duration_s=2.0)
    assert plan["median_error_after_s"] < plan["median_error_before_s"]
    assert plan["blocks"]


def test_a_32ms_shift_jump_between_blocks_is_reported() -> None:
    times = [0.20, 0.45, 0.70, 0.95]
    events = _click_events(times)
    assignments = [
        {"event_index": 0, "grid_index": 0, "event_s": 0.20, "target_s": 0.200, "qn": 0.4, "offset_s": 0.0},
        {"event_index": 1, "grid_index": 1, "event_s": 0.45, "target_s": 0.450, "qn": 0.9, "offset_s": 0.0},
        {"event_index": 2, "grid_index": 2, "event_s": 0.70, "target_s": 0.732, "qn": 1.464, "offset_s": 0.032},
        {"event_index": 3, "grid_index": 3, "event_s": 0.95, "target_s": 0.982, "qn": 1.964, "offset_s": 0.032},
    ]
    plan = build_timing_plan(events, assignments, strength=1.0,
                             duration_s=1.2, group_gap_s=0.60,
                             group_shift_s=0.012)
    assert [issue["code"] for issue in plan["issues"]].count("adjacent_shift_jump") == 1
    jump = next(issue for issue in plan["issues"] if issue["code"] == "adjacent_shift_jump")
    assert jump["delta_s"] == 0.032


def test_protected_pause_is_not_moved_silently() -> None:
    events = _click_events([0.20, 0.45])
    assignments = [
        {"event_index": 0, "grid_index": 0, "event_s": 0.20, "target_s": 0.220, "qn": 0.4, "offset_s": 0.020},
        {"event_index": 1, "grid_index": 1, "event_s": 0.45, "target_s": 0.470, "qn": 0.9, "offset_s": 0.020},
    ]
    plan = build_timing_plan(events, assignments, duration_s=1.0,
                             protected_ranges=[{"start_s": 0.30, "end_s": 0.40}])
    assert any(issue["code"] == "protected_range" for issue in plan["issues"])
