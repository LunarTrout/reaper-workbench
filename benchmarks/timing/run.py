"""Run the exact-truth synthetic timing benchmark and write a JSON report.

The synthetic case is deliberately small and deterministic.  It is a harness
test, not evidence about a real mix; real holdout results belong in a separate
report with the manifest entries.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from reaper_audio.analysis.timing import detect_timing_events

from .baselines import global_shift, nearest_grid, no_op
from .metrics import event_scores, geometry_metrics, match_points, timing_error
from .synthetic import synthetic_case


def _detected(case: dict[str, Any]) -> list[float]:
    analysis = detect_timing_events(case["corrupted"], sensitivity=2.0,
                                    min_gap_s=0.03)
    return [row["source_s"] for row in analysis["events"]]


def score_case(name: str, events: Sequence[float], shifts: Sequence[float], *,
               duration_s: float, sample_rate: int = 48_000,
               tolerance_s: float = 0.020) -> dict[str, Any]:
    case = synthetic_case(events, shifts, duration_s=duration_s,
                          sample_rate=sample_rate)
    detected = _detected(case)
    targets = case["targets"]
    scores = event_scores(targets, detected, tolerance_s)
    matched = match_points(targets, detected, tolerance_s)["pairs"]
    aligned_events = [row["predicted_s"] for row in matched]
    aligned_targets = [row["truth_s"] for row in matched]
    reported: dict[str, Any] = {
        "case": name,
        "event_scores": scores,
        "targets": targets,
        "detected": [round(value, 7) for value in detected],
        "baselines": {},
    }
    for baseline_name, planner in (("noop", no_op),
                                   ("global_shift", global_shift),
                                   ("nearest_grid", nearest_grid)):
        segments = planner(aligned_events, aligned_targets)
        predicted_targets = [row["target_start_s"] for row in segments]
        result = timing_error(aligned_targets, predicted_targets,
                              tolerance_s)
        result["geometry"] = geometry_metrics(
            case["truth_segments"][:len(segments)], segments)
        reported["baselines"][baseline_name] = result
    return reported


def main() -> None:
    events = [0.20, 0.45, 0.70, 0.95, 1.20]
    shifts = [0.010, -0.020, 0.015, 0.030, -0.012]
    report = {
        "schema": "reaper-workbench.timing-synthetic-report/1",
        "note": "exact-truth harness only; not a real-mix quality claim",
        "cases": [score_case("clicks_mixed_shifts", events, shifts,
                             duration_s=1.5)],
    }
    out = Path(__file__).with_name("report.json")
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    print(json.dumps(report["cases"][0]["baselines"], indent=2))


if __name__ == "__main__":
    main()
