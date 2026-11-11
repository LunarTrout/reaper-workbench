"""Mastering arithmetic from measured loudness and true-peak evidence.

No target is invented here: the caller (agent or user) supplies the loudness
goal and ceiling. This module computes the gain, the predicted peak, and the
amount of limiting needed; it never chooses a plugin or an EQ move.
"""
from __future__ import annotations

from typing import Any, Mapping


def _value(source: Mapping, name: str) -> float | None:
    cell = (source.get("metrics") or {}).get(name)
    if isinstance(cell, dict):
        value = cell.get("value")
    else:
        value = getattr(cell, "value", cell)
    return float(value) if isinstance(value, (int, float)) else None


def _master_source(report: Mapping) -> Mapping | None:
    anchor = report.get("anchor_key")
    sources = report.get("sources") or {}
    if anchor and anchor in sources:
        return sources[anchor]
    for source in sources.values():
        if (source.get("track_context") or {}).get("is_master"):
            return source
    return None


def plan_mastering(report: Mapping, *, target_lufs: float,
                   ceiling_dbtp: float = -1.0) -> dict[str, Any]:
    """Compute gain and limiting needed to reach a caller-supplied target.

    ``target_lufs`` and ``ceiling_dbtp`` are decisions, not defaults of this
    module. A negative ceiling is normal. The result reports the trade-off when
    the target cannot be reached without more limiting.
    """
    if not isinstance(target_lufs, (int, float)):
        raise ValueError("target_lufs must be a number")
    if not isinstance(ceiling_dbtp, (int, float)) or ceiling_dbtp >= 0:
        raise ValueError("ceiling_dbtp must be negative")
    master = _master_source(report)
    if master is None:
        raise ValueError("report has no master source")
    lufs = _value(master, "integrated_lufs")
    true_peak = _value(master, "true_peak_estimate_dbfs")
    sample_peak = _value(master, "peak_dbfs")
    if lufs is None or true_peak is None:
        raise ValueError("master loudness/true-peak measurement is unavailable")
    gain_db = float(target_lufs) - lufs
    predicted_peak = true_peak + gain_db
    limiting_db = max(0.0, predicted_peak - float(ceiling_dbtp))
    reachable = limiting_db <= 0.0
    return {
        "measured": {"integrated_lufs": lufs,
                     "true_peak_estimate_dbfs": true_peak,
                     "sample_peak_dbfs": sample_peak},
        "target": {"integrated_lufs": float(target_lufs),
                   "ceiling_dbtp": float(ceiling_dbtp)},
        "gain_db": round(gain_db, 3),
        "predicted_true_peak_dbfs": round(predicted_peak, 3),
        "predicted_integrated_lufs": round(float(target_lufs), 3),
        "limiter": {
            "needed": limiting_db > 0.0,
            "gain_reduction_db": round(limiting_db, 3),
            "threshold_dbtp": float(ceiling_dbtp),
            "note": ("Apply the gain first, then limit only by the reported "
                     "amount; verify with a fresh true-peak measurement."),
        },
        "reachable_without_limiting": reachable,
        "warning": (None if reachable else
                    "The loudness target exceeds the ceiling on peaks; the "
                    "limiter must remove the reported gain reduction, or the "
                    "target must be lowered."),
    }
