from __future__ import annotations

from benchmarks.timing.baselines import nearest_grid, no_op
from benchmarks.timing.metrics import event_scores, geometry_metrics, timing_error
from benchmarks.timing.run import score_case
from benchmarks.timing.synthetic import click_track, move_segments, synthetic_case


def test_segment_move_records_exact_source_and_target_geometry() -> None:
    audio = click_track([0.20, 0.50], duration_s=1.0)
    moved, truth = move_segments(audio, [
        {"source_start_s": 0.20, "source_end_s": 0.28, "shift_s": 0.015},
        {"source_start_s": 0.50, "source_end_s": 0.58, "shift_s": -0.020},
    ])
    assert truth[0].target_start_s == 0.215
    assert truth[1].target_start_s == 0.480
    assert abs((truth[0].source_end_s - truth[0].source_start_s) - 0.08) < 1e-9
    assert moved.sample_rate == audio.sample_rate


def test_event_metrics_count_one_to_one_matches() -> None:
    score = event_scores([0.2, 0.4], [0.205, 0.6], tolerance_s=0.02)
    assert score["matched"] == 1
    assert score["precision"] == 0.5
    assert score["recall"] == 0.5
    assert timing_error([0.2, 0.4], [0.205, 0.6], 0.02)["mae_s"] == 0.005


def test_a_naive_noop_loses_to_a_grid_baseline() -> None:
    case = synthetic_case([0.2, 0.45, 0.7], [0.012, -0.018, 0.025],
                          duration_s=1.2)
    truth = [row["target_start_s"] for row in case["truth_segments"]]
    events = [row["source_start_s"] for row in case["truth_segments"]]
    no_op_score = timing_error(truth, [row["target_start_s"] for row in no_op(events, truth)], 0.05)
    grid_score = timing_error(truth, [row["target_start_s"] for row in nearest_grid(events, truth)], 0.05)
    assert no_op_score["mae_s"] > grid_score["mae_s"]


def test_geometry_metrics_report_duplicates_and_length_error() -> None:
    truth = [{"target_start_s": 0.2, "target_end_s": 0.28},
             {"target_start_s": 0.5, "target_end_s": 0.58}]
    predicted = [{"target_start_s": 0.2, "target_end_s": 0.30},
                 {"target_start_s": 0.5, "target_end_s": 0.58},
                 {"target_start_s": 0.9, "target_end_s": 0.98}]
    result = geometry_metrics(truth, predicted)
    assert result["matched"] == 2
    assert result["duplicated"] == 1
    assert abs(result["length_mae_s"] - 0.01) < 1e-9


def test_synthetic_harness_runs_and_exposes_baseline_scores() -> None:
    result = score_case("unit", [0.2, 0.45, 0.7], [0.01, -0.02, 0.015],
                        duration_s=1.2)
    assert result["event_scores"]["truth_count"] == 3
    assert set(result["baselines"]) == {"noop", "global_shift", "nearest_grid"}
