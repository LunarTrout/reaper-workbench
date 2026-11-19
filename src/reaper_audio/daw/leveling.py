"""Turn measured segment levels into a safe volume curve.

The point of this module is to stop hand-built envelope points: a caller passes
the segments a profile measured, a target level and a gain limit, and gets a
curve that only touches regions with signal, never writes silence, and reports
every move it makes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import ValidationError

MAX_GAIN_DB = 6.0
EDGE_S = 0.05


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def plan_curve(segments: Sequence[Mapping[str, Any]], *,
               target_db: float, max_gain_db: float = MAX_GAIN_DB,
               smooth_s: float = 0.15,
               base_value: float = 1.0) -> tuple[list[dict[str, float]],
                                                  list[dict[str, Any]]]:
    """Return (points, table) for one measured set of active segments."""
    if not isinstance(target_db, (int, float)):
        raise ValidationError("target_db must be a number")
    if not segments:
        raise ValidationError("leveling needs at least one measured segment")
    if smooth_s < 0:
        raise ValidationError("smooth_s must not be negative")
    ramp = max(EDGE_S, float(smooth_s))
    ordered = sorted(segments, key=lambda row: float(row["start_s"]))
    points: list[dict[str, float]] = []
    table: list[dict[str, Any]] = []
    previous_end: float | None = None
    for row in ordered:
        start = float(row["start_s"])
        end = float(row["end_s"])
        level = float(row["level_db"])
        if end <= start:
            raise ValidationError("segment end_s must be greater than start_s",
                                  details={"segment": dict(row)})
        if previous_end is not None and start < previous_end:
            raise ValidationError("leveling segments must not overlap",
                                  details={"segment": dict(row)})
        gain_db = _clamp(target_db - level, float(max_gain_db))
        factor = round(10 ** (gain_db / 20.0), 6)
        # A one-second tail or a fade is not a phrase: boosting it would lift
        # breaths and reverb, so it stays at unity instead of being levelled.
        if end - start < 1.0 or level < float(target_db) - 10.0:
            if previous_end is not None and start - previous_end > 2 * ramp:
                points.append({"time_s": round(previous_end + ramp, 6),
                               "value": base_value})
                points.append({"time_s": round(start - ramp, 6),
                               "value": base_value})
            previous_end = max(previous_end or 0.0, end)
            continue
        if previous_end is None or start - previous_end > 2 * ramp:
            if previous_end is not None:
                points.append({"time_s": round(previous_end + ramp, 6),
                               "value": base_value})
            points.append({"time_s": round(max(0.0, start - ramp), 6),
                           "value": base_value})
        # Adjacent segments meet directly: no unity point between them, or the
        # curve would saw back to 0 dB at every phrase boundary.
        points.append({"time_s": round(start, 6), "value": factor})
        points.append({"time_s": round(end, 6), "value": factor})
        table.append({"start_s": round(start, 3), "end_s": round(end, 3),
                      "level_db": round(level, 2), "gain_db": round(gain_db, 2),
                      "factor": factor})
        previous_end = end
    if previous_end is not None:
        points.append({"time_s": round(previous_end + ramp, 6),
                       "value": base_value})
    points.sort(key=lambda point: point["time_s"])
    deduped: list[dict[str, float]] = []
    for point in points:
        if deduped and abs(deduped[-1]["time_s"] - point["time_s"]) < 1e-6:
            deduped[-1] = point
        else:
            deduped.append(point)
    return deduped, table
