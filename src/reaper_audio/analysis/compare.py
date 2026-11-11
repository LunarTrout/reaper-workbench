"""Explicit, unit-aware comparisons without a hidden mix-quality score."""

from __future__ import annotations

from statistics import median
from typing import Any

import numpy as np

from ..errors import ValidationError
from ..models import MeasurementReport


def _report_dict(report: MeasurementReport | dict) -> dict:
    if isinstance(report, MeasurementReport):
        return report.to_dict()
    return report


def compare_reports(a: MeasurementReport | dict, b: MeasurementReport | dict) -> dict:
    left, right = _report_dict(a), _report_dict(b)
    lm, rm = left.get("metrics", {}), right.get("metrics", {})
    deltas: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for name in sorted(set(lm) | set(rm)):
        av, bv = lm.get(name), rm.get(name)
        if av is None or bv is None:
            missing.append(name)
            continue
        if hasattr(av, "__dict__"):
            av = av.__dict__
        if hasattr(bv, "__dict__"):
            bv = bv.__dict__
        avalue = av.get("value") if isinstance(av, dict) else av
        bvalue = bv.get("value") if isinstance(bv, dict) else bv
        aunit = av.get("unit") if isinstance(av, dict) else None
        bunit = bv.get("unit") if isinstance(bv, dict) else None
        if avalue is None or bvalue is None or aunit != bunit:
            missing.append(name)
            continue
        deltas[name] = {"a": float(avalue), "b": float(bvalue),
                        "delta": float(bvalue) - float(avalue), "unit": aunit}
    coverage_a = left.get("coverage", {})
    coverage_b = right.get("coverage", {})
    return {
        "ok": True,
        "a": left.get("source"),
        "b": right.get("source"),
        "deltas": deltas,
        "coverage": {"compared": sorted(deltas), "missing_or_incompatible": missing,
                     "a": coverage_a, "b": coverage_b},
        "interpretation": "Numeric deltas only; no overall quality score is inferred.",
    }


def reference_corridor(
    mine: MeasurementReport | dict,
    references: list[MeasurementReport | dict],
) -> dict:
    if not references:
        raise ValidationError("At least one reference is required")
    target = _report_dict(mine)
    ref_dicts = [_report_dict(item) for item in references]
    names = set(target.get("metrics", {}))
    for ref in ref_dicts:
        names &= set(ref.get("metrics", {}))
    rows: dict[str, dict[str, Any]] = {}
    skipped: dict[str, str] = {}
    for name in sorted(names):
        vals: list[float] = []
        units: set[str | None] = set()
        for ref in ref_dicts:
            cell = ref["metrics"][name]
            val = cell.get("value") if isinstance(cell, dict) else cell
            unit = cell.get("unit") if isinstance(cell, dict) else None
            if val is None:
                continue
            vals.append(float(val))
            units.add(unit)
        mine_cell = target["metrics"][name]
        mine_value = mine_cell.get("value") if isinstance(mine_cell, dict) else mine_cell
        mine_unit = mine_cell.get("unit") if isinstance(mine_cell, dict) else None
        if mine_value is None or len(units) != 1 or mine_unit not in units or not vals:
            skipped[name] = "missing value or inconsistent units"
            continue
        center = float(median(vals))
        mad = float(median([abs(value - center) for value in vals]))
        rows[name] = {"mine": float(mine_value), "median": center,
                      "mad": mad, "delta": float(mine_value) - center,
                      "unit": mine_unit, "references": len(vals)}
    return {"rows": rows, "skipped": skipped, "reference_count": len(ref_dicts),
            "interpretation": "Median and median absolute deviation; not a target recipe."}


def evaluate_goals(
    before: MeasurementReport | dict,
    after: MeasurementReport | dict,
    goals: dict[str, dict[str, float | str]],
) -> dict:
    """Evaluate caller-supplied constraints; the library invents no target values."""
    comparison = compare_reports(before, after)
    deltas = comparison["deltas"]
    rows: dict[str, dict] = {}
    failures: list[str] = []
    missing: list[str] = []
    for name, goal in goals.items():
        cell = deltas.get(name)
        if cell is None:
            missing.append(name)
            continue
        value = cell["b"]
        kind = str(goal.get("kind", ""))
        if kind == "range":
            ok = float(goal["min"]) <= value <= float(goal["max"])
        elif kind == "minimum":
            ok = value >= float(goal["value"])
        elif kind == "maximum":
            ok = value <= float(goal["value"])
        else:
            raise ValidationError("Unsupported goal kind",
                                  details={"metric": name, "kind": kind})
        rows[name] = {"value": value, "unit": cell["unit"], "ok": ok, "goal": goal}
        if not ok:
            failures.append(name)
    decision = "insufficient_data" if missing else ("reject" if failures else "accept")
    return {"decision": decision, "checks": rows, "failed": failures,
            "missing": missing, "comparison": comparison}


def simulate_gain(report: MeasurementReport | dict, gain_db: float) -> dict:
    """Predict only mathematically linear level metrics; mark all else unknown."""
    source = _report_dict(report)
    linear_names = {"peak_dbfs", "true_peak_estimate_dbfs", "rms_dbfs", "integrated_lufs"}
    predicted: dict[str, dict[str, Any]] = {}
    for name, cell in source.get("metrics", {}).items():
        value = cell.get("value") if isinstance(cell, dict) else cell
        unit = cell.get("unit") if isinstance(cell, dict) else None
        if value is None:
            predicted[name] = {"value": None, "unit": unit, "reason": "no baseline value"}
        elif name in linear_names and unit in {"dBFS", "dBTP", "LUFS"}:
            predicted[name] = {"value": float(value) + gain_db, "unit": unit,
                               "model": "linear gain"}
        else:
            predicted[name] = {"value": None, "unit": unit,
                               "reason": "must be remeasured after processing"}
    return {"gain_db": gain_db, "metrics": predicted,
            "warning": "This is not a render prediction for nonlinear or time-varying FX."}
