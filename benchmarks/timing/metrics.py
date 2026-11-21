"""Metrics for the three timing tasks: events, targets and edit geometry."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence


def _times(rows: Iterable[Mapping[str, Any]], key: str = "time_s") -> list[float]:
    return [float(row[key]) for row in rows if row.get(key) is not None]


def match_points(truth: Sequence[float], predicted: Sequence[float],
                 tolerance_s: float) -> dict[str, Any]:
    """Greedy one-to-one nearest matching inside a tolerance."""
    used: set[int] = set()
    pairs: list[dict[str, float]] = []
    for target in truth:
        best = None
        for index, value in enumerate(predicted):
            if index in used:
                continue
            error = abs(value - target)
            if error <= tolerance_s and (best is None or error < best[0]):
                best = (error, index, value)
        if best is not None:
            used.add(best[1])
            pairs.append({"truth_s": float(target), "predicted_s": float(best[2]),
                          "error_s": float(best[0])})
    return {"pairs": pairs, "matched": len(pairs),
            "unmatched_truth": len(truth) - len(pairs),
            "unmatched_predicted": len(predicted) - len(pairs)}


def event_scores(truth: Sequence[float], predicted: Sequence[float],
                 tolerance_s: float) -> dict[str, Any]:
    matched = match_points(truth, predicted, tolerance_s)["matched"]
    precision = matched / len(predicted) if predicted else 0.0
    recall = matched / len(truth) if truth else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": round(precision, 6), "recall": round(recall, 6),
            "f1": round(f1, 6), "matched": matched,
            "truth_count": len(truth), "predicted_count": len(predicted)}


def timing_error(truth: Sequence[float], predicted: Sequence[float],
                 tolerance_s: float) -> dict[str, Any]:
    import numpy as np
    pairs = match_points(truth, predicted, tolerance_s)["pairs"]
    errors = [abs(pair["error_s"]) for pair in pairs]
    if not errors:
        return {"count": 0, "mae_s": None, "p90_s": None}
    return {"count": len(errors),
            "mae_s": round(float(np.mean(errors)), 7),
            "p90_s": round(float(np.percentile(errors, 90)), 7)}


def geometry_metrics(truth: Sequence[Mapping[str, Any]],
                     predicted: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare target starts and lengths without assuming identical counts."""
    import numpy as np
    truth_rows = sorted(truth, key=lambda row: float(row["target_start_s"]))
    predicted_rows = sorted(predicted, key=lambda row: float(row["target_start_s"]))
    used: set[int] = set()
    pairs = []
    for row in truth_rows:
        target = float(row["target_start_s"])
        best = None
        for index, candidate in enumerate(predicted_rows):
            if index in used:
                continue
            error = abs(float(candidate["target_start_s"]) - target)
            if best is None or error < best[0]:
                best = (error, index, candidate)
        if best is None:
            continue
        used.add(best[1])
        candidate = best[2]
        truth_length = float(row["target_end_s"]) - target
        pred_length = float(candidate["target_end_s"]) - float(candidate["target_start_s"])
        pairs.append({"truth_start_s": target,
                      "predicted_start_s": float(candidate["target_start_s"]),
                      "start_error_s": best[0],
                      "truth_length_s": truth_length,
                      "predicted_length_s": pred_length,
                      "length_error_s": abs(pred_length - truth_length)})
    start_errors = [pair["start_error_s"] for pair in pairs]
    length_errors = [pair["length_error_s"] for pair in pairs]
    return {
        "matched": len(pairs),
        "missing": len(truth_rows) - len(pairs),
        "duplicated": len(predicted_rows) - len(pairs),
        "start_mae_s": round(float(np.mean(start_errors)), 7) if start_errors else None,
        "start_p90_s": round(float(np.percentile(start_errors, 90)), 7) if start_errors else None,
        "length_mae_s": round(float(np.mean(length_errors)), 7) if length_errors else None,
        "pairs": pairs,
    }


def pause_loss(truth: Sequence[Mapping[str, Any]],
               predicted: Sequence[Mapping[str, Any]]) -> list[dict[str, float]]:
    """Per adjacent truth pair, how much pause the predicted geometry loses."""
    truth_rows = sorted(truth, key=lambda row: float(row["target_start_s"]))
    pred_rows = sorted(predicted, key=lambda row: float(row["target_start_s"]))
    losses = []
    for index in range(len(truth_rows) - 1):
        truth_gap = (float(truth_rows[index + 1]["target_start_s"]) -
                     float(truth_rows[index]["target_end_s"]))
        if index + 1 >= len(pred_rows):
            break
        pred_gap = (float(pred_rows[index + 1]["target_start_s"]) -
                    float(pred_rows[index]["target_end_s"]))
        losses.append({"index": index,
                       "truth_pause_s": round(truth_gap, 7),
                       "predicted_pause_s": round(pred_gap, 7),
                       "loss_s": round(max(0.0, truth_gap - pred_gap), 7)})
    return losses
