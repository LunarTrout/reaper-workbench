"""Exact measured delta between two audio analyses.

An *operation* delta answers "what did I change"; this module answers the other
question: "which measured values actually differ between these two captured
audio states".  They are deliberately separate — one never substitutes for the
other.

Every number here is taken from the stored analysis report of its own capture:

* scalar metrics are compared value-for-value with their stored unit;
* temporal series are compared cell-for-cell on the **stored time axis**, not on
  the panorama's declared summary grid and not on any resampled or averaged
  grid;
* a changed run of cells is reported as exact changed intervals plus every exact
  cell row; a large delta is delivered as complete pages rather than truncated;
* when the two captures do not share a comparable time axis or measurement
  window, the tool refuses a pointwise delta and says why.  It never invents an
  alignment.

The exact source rows remain in each analysis record and are addressable by
``reaper://analyses/{analysis_id}/report``; this delta is a comparison, not a
replacement archive.  No overall quality score is produced.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import ValidationError

AUDIO_DELTA_SCHEMA = "reaper-workbench.audio-delta/1"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _metric_cell(value: Any) -> tuple[float | None, str | None, Any]:
    """Return (value, unit, raw) for a stored metric cell or scalar."""
    if isinstance(value, Mapping):
        raw = value.get("value")
        return _number(raw), (str(value.get("unit")) if value.get("unit") else None), value
    return _number(value), None, value


def _sources(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    sources = report.get("sources")
    if not isinstance(sources, Mapping):
        raise ValidationError("the analysis report has no source map")
    return {str(guid): row for guid, row in sources.items() if isinstance(row, Mapping)}


def _window(report: Mapping[str, Any]) -> dict[str, Any] | None:
    section = report.get("section")
    if not isinstance(section, Mapping):
        return None
    start = _number(section.get("start_s"))
    end = _number(section.get("end_s"))
    if start is None or end is None:
        return None
    return {"start_s": start, "end_s": end}


def _series_payload(report: Mapping[str, Any], name: str) -> tuple[list[float], list[Any], str | None]:
    """Return (times, values, unit) for one exact stored series."""
    curves = report.get("curves")
    if not isinstance(curves, Mapping):
        raise ValidationError("the analysis report has no curves")
    if name == "rms_400ms":
        times = [_number(value) for value in curves.get("rms_400ms_time_s") or []]
        return ([float(value) for value in times if value is not None],
                list(curves.get("rms_400ms_dbfs") or []), "dBFS")
    if name == "band_level":
        times = [_number(value) for value in curves.get("spectrogram_time_s") or []]
        return ([float(value) for value in times if value is not None],
                [list(row) if isinstance(row, (list, tuple)) else row
                 for row in curves.get("band_level_time_dbfs") or []], "dBFS")
    if name == "temporal_profile":
        profile = curves.get("temporal_profile")
        profile = profile if isinstance(profile, Mapping) else {}
        times = [_number(value) for value in profile.get("times") or []]
        return ([float(value) for value in times if value is not None],
                [dict(cell) for cell in profile.get("cells") or []
                 if isinstance(cell, Mapping)], None)
    raise ValidationError("unknown analysis series", details={"series": name})


def _same_axis(before: Sequence[float], after: Sequence[float]) -> bool:
    """A pointwise delta needs the identical stored axis, not a close one."""
    return len(before) == len(after) and list(before) == list(after)


def _continuity_identity(record: Mapping[str, Any]) -> dict[str, Any]:
    """The fields that must agree before two captures are one project history.

    ``project_id`` alone is not continuity: a reopened project, another bridge
    process or a regenerated project is a different measurement generation.
    """
    revision = record.get("revision")
    if not isinstance(revision, Mapping):
        raise ValidationError("an analysis delta needs each analysis revision")
    project_id = revision.get("project_id")
    path = revision.get("path")
    instance_id = revision.get("instance_id")
    epoch = revision.get("epoch")
    if not isinstance(project_id, str) or not project_id:
        raise ValidationError("the base/target revision has no project_id")
    if not isinstance(path, str) or not path:
        raise ValidationError("the base/target revision has no project path")
    if not isinstance(instance_id, str) or not instance_id:
        raise ValidationError(
            "the base/target revision has no bridge instance id; continuity is unproven",
            details={"project_id": project_id})
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise ValidationError(
            "the base/target revision has no positive project generation (epoch); "
            "continuity is unproven",
            details={"project_id": project_id, "epoch": epoch})
    return {"project_id": project_id, "path": path,
            "instance_id": instance_id, "epoch": epoch}


def _changed_intervals(times: Sequence[float], changed: Sequence[int],
                       window: Mapping[str, Any] | None) -> tuple[list[dict[str, Any]], int]:
    """Build half-open intervals only from stored cell starts and the window end.

    A cell start is a measured boundary.  A run that ends before the last cell
    can therefore close at the next cell's exact start.  The final cell has no
    stored end; its run closes at the measurement window end when that end is
    known and later than the cell, and otherwise no interval is invented.  No
    width is inferred from a median step.
    """
    if not changed:
        return [], 0
    runs: list[tuple[int, int]] = []
    start_index = previous = changed[0]
    for index in list(changed[1:]) + [None]:
        if index is not None and index == previous + 1:
            previous = index
            continue
        runs.append((start_index, previous))
        if index is None:
            break
        start_index = previous = index
    intervals: list[dict[str, Any]] = []
    unbounded_cells = 0
    for first, last in runs:
        start = times[first]
        end: float | None = None
        basis: str | None = None
        if last + 1 < len(times) and times[last + 1] > start:
            end, basis = times[last + 1], "next_stored_cell_start"
        elif (last + 1 == len(times) and window is not None and
              window.get("end_s") is not None and
              float(window["end_s"]) > start):
            end, basis = float(window["end_s"]), "measurement_window_end"
        if end is None:
            unbounded_cells += last - first + 1
            continue
        intervals.append({
            "start_s": start,
            "end_s": end,
            "cells": last - first + 1,
            "first_cell": first,
            "last_cell": last,
            "end_basis": basis,
        })
    return intervals, unbounded_cells


def _scalar_stats(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "max_abs": None, "mean_abs": None, "rms": None,
                "min": None, "max": None}
    absolute = [abs(value) for value in values]
    return {
        "count": len(values),
        "max_abs": max(absolute),
        "mean_abs": sum(absolute) / len(absolute),
        "rms": math.sqrt(sum(value * value for value in values) / len(values)),
        "min": min(values),
        "max": max(values),
    }


def _flatten(value: Any) -> list[float]:
    if isinstance(value, Mapping):
        return [number for key in sorted(value) for number in _flatten(value[key])]
    if isinstance(value, (list, tuple)):
        return [number for item in value for number in _flatten(item)]
    number = _number(value)
    return [number] if number is not None else []


def _series_delta(before_report: Mapping[str, Any], after_report: Mapping[str, Any],
                  name: str, *, interval: Mapping[str, Any] | None,
                  window: Mapping[str, Any] | None,
                  max_cells: int | None = None) -> dict[str, Any]:
    before_times, before_values, unit = _series_payload(before_report, name)
    after_times, after_values, after_unit = _series_payload(after_report, name)
    if unit != after_unit:
        return {"series": name, "status": "incompatible",
                "reason": "unit_mismatch", "before_unit": unit,
                "after_unit": after_unit}
    if not _same_axis(before_times, after_times):
        return {
            "series": name,
            "status": "incompatible",
            "reason": "time_axis_differs",
            "before": {"count": len(before_times),
                       "start_s": before_times[0] if before_times else None,
                       "end_s": before_times[-1] if before_times else None},
            "after": {"count": len(after_times),
                      "start_s": after_times[0] if after_times else None,
                      "end_s": after_times[-1] if after_times else None},
            "note": ("the two captures do not share the same stored time axis, so a "
                     "pointwise delta would be fictitious; compare per-source summaries "
                     "or align the captures explicitly"),
        }
    if len(before_values) != len(before_times) or len(after_values) != len(after_times):
        return {"series": name, "status": "incompatible",
                "reason": "value_count_differs",
                "before": {"times": len(before_times), "values": len(before_values)},
                "after": {"times": len(after_times), "values": len(after_values)}}
    low = _number((interval or {}).get("start_s"))
    high = _number((interval or {}).get("end_s"))
    indices = [index for index, time_s in enumerate(before_times)
               if (low is None or time_s >= low) and (high is None or time_s < high)]
    changed: list[int] = []
    scalars: list[float] = []
    structured = False
    cells: list[dict[str, Any]] = []
    for index in indices:
        before_value = before_values[index]
        after_value = after_values[index]
        if before_value == after_value:
            continue
        changed.append(index)
        if isinstance(before_value, Mapping) or isinstance(after_value, Mapping):
            structured = True
            delta_value: Any = None
            magnitude: list[float] = []
        elif isinstance(before_value, (list, tuple)) or isinstance(after_value, (list, tuple)):
            before_flat = _flatten(before_value)
            after_flat = _flatten(after_value)
            magnitude = [after_flat[i] - before_flat[i] for i in range(
                min(len(before_flat), len(after_flat)))]
            scalars.extend(magnitude)
            delta_value = list(magnitude)
        else:
            before_number = _number(before_value)
            after_number = _number(after_value)
            delta_value = (after_number - before_number
                           if before_number is not None and after_number is not None
                           else None)
            if delta_value is not None:
                scalars.append(delta_value)
        cells.append({
            "index": index,
            "start_s": before_times[index],
            "before": before_value,
            "after": after_value,
            "delta": delta_value,
        })
    intervals, unbounded_cells = _changed_intervals(before_times, changed, window)
    return {
        "series": name,
        "status": "compared",
        "unit": unit,
        "time_basis": "project_seconds, exact stored cells",
        "axis": {
            "count": len(before_times),
            "start_s": before_times[0] if before_times else None,
            "last_cell_start_s": before_times[-1] if before_times else None,
            "window": window,
            "boundary_basis": ("cell starts are stored values; a run closes at the "
                               "next stored cell start, and the final run only at "
                               "the measurement window end. No width is inferred "
                               "from a median step."),
        },
        "compared_cells": len(indices),
        "changed_cells": len(changed),
        "unchanged_cells": len(indices) - len(changed),
        "changed_intervals": intervals,
        "changed_intervals_unbounded_cells": unbounded_cells,
        "magnitude": ({"kind": "structured_cells", "delta": None}
                      if structured else _scalar_stats(scalars)),
        "cells": cells,
        "cells_complete": True,
        "cells_omitted": 0,
        "exact_query": (
            "reaper_panorama_query data=['series'] objects=[source id] "
            f"series=['{name}'] interval={{start_s,end_s}} returns the stored "
            "time axis and values unrounded"),
        "note": ("every changed cell is listed with its exact stored timestamp; "
                 "the full cells and intervals are delivered inline or as complete "
                 "pages, never truncated by an internal cell cap"),
    }


def build_audio_delta(base: Mapping[str, Any], target: Mapping[str, Any], *,
                      interval: Mapping[str, Any] | None = None,
                      sources: Sequence[str] | None = None,
                      series: Sequence[str] | None = None,
                      metrics: Sequence[str] | None = None,
                      max_changed_cells: int | None = None,
                      current_capture_id: str | None = None) -> dict[str, Any]:
    """Compare two stored analysis records exactly.

    ``base`` and ``target`` are records as returned by the analysis store (a
    ``report``, an ``analysis_id`` and their capture/revision binding).  The
    result never claims a quality verdict and never substitutes for either
    record.  ``max_changed_cells`` is accepted only as a legacy argument and no
    longer bounds the result: every changed cell is returned, and the MCP layer
    pages a large delta rather than hiding cells.
    """
    if max_changed_cells is not None and (
            isinstance(max_changed_cells, bool) or not isinstance(max_changed_cells, int)
            or max_changed_cells < 0):
        raise ValidationError("max_changed_cells must be a non-negative integer or null")
    base_report = base.get("report")
    target_report = target.get("report")
    if not isinstance(base_report, Mapping) or not isinstance(target_report, Mapping):
        raise ValidationError("both analyses need a stored report")
    base_project = str(base.get("project_id") or
                       (base_report.get("project") or {}).get("project_id") or "")
    target_project = str(target.get("project_id") or
                         (target_report.get("project") or {}).get("project_id") or "")
    if base_project and target_project and base_project != target_project:
        raise ValidationError("the two analyses belong to different projects",
                              details={"base_project_id": base_project,
                                       "target_project_id": target_project})
    base_identity = _continuity_identity(base)
    target_identity = _continuity_identity(target)
    if base_identity != target_identity:
        raise ValidationError(
            "the two analyses are not one continuous project history",
            details={"base_identity": base_identity,
                     "target_identity": target_identity,
                     "fields_compared": ["project_id", "path", "instance_id", "epoch"]})
    base_window = _window(base_report)
    target_window = _window(target_report)
    if base_window != target_window:
        raise ValidationError(
            "the two analyses measured different windows; compare equal windows",
            details={"base_window": base_window, "target_window": target_window})
    base_sources = _sources(base_report)
    target_sources = _sources(target_report)
    wanted = {str(value) for value in sources} if sources else None
    selected = sorted(set(base_sources) & set(target_sources))
    if wanted is not None:
        unknown = sorted(wanted - set(base_sources) - set(target_sources))
        if unknown:
            raise ValidationError("unknown source in selection",
                                  details={"unknown": unknown})
        selected = [guid for guid in selected if guid in wanted]
    added = sorted(set(target_sources) - set(base_sources))
    removed = sorted(set(base_sources) - set(target_sources))

    metric_rows: list[dict[str, Any]] = []
    metric_missing: list[dict[str, Any]] = []
    metric_incompatible: list[dict[str, Any]] = []
    unchanged_metrics = 0
    wanted_metrics = {str(name) for name in metrics} if metrics else None
    for guid in selected:
        before_metrics = base_sources[guid].get("metrics")
        after_metrics = target_sources[guid].get("metrics")
        before_metrics = before_metrics if isinstance(before_metrics, Mapping) else {}
        after_metrics = after_metrics if isinstance(after_metrics, Mapping) else {}
        names = sorted(set(before_metrics) | set(after_metrics))
        if wanted_metrics is not None:
            names = [name for name in names if name in wanted_metrics]
        for name in names:
            before_value, before_unit, _ = _metric_cell(before_metrics.get(name))
            after_value, after_unit, _ = _metric_cell(after_metrics.get(name))
            if before_value is None or after_value is None:
                if before_value != after_value:
                    metric_missing.append({
                        "source": guid, "metric": name,
                        "before": before_metrics.get(name),
                        "after": after_metrics.get(name),
                    })
                continue
            if before_unit != after_unit:
                metric_incompatible.append({
                    "source": guid, "metric": name,
                    "before_unit": before_unit, "after_unit": after_unit,
                })
                continue
            if before_value == after_value:
                unchanged_metrics += 1
                continue
            metric_rows.append({
                "source": guid, "metric": name, "unit": before_unit,
                "before": before_value, "after": after_value,
                "delta": after_value - before_value,
            })

    names = list(series or ("rms_400ms",))
    series_rows: list[dict[str, Any]] = []
    for guid in selected:
        for name in names:
            try:
                row = _series_delta(base_sources[guid], target_sources[guid], name,
                                    interval=interval, window=base_window,
                                    max_cells=max_changed_cells)
            except ValidationError as exc:
                row = {"series": name, "status": "unavailable",
                       "reason": getattr(exc, "message", str(exc))}
            series_rows.append({"source": guid, **row})

    freshness = "unknown"
    reason = None
    target_capture = target.get("capture_id")
    if current_capture_id and target_capture:
        if str(current_capture_id) != str(target_capture):
            freshness = "stale_target"
            reason = ("the target analysis is not the current capture; its measured "
                      "values describe an older audio state")
        else:
            freshness = "current"
    elif current_capture_id and not target_capture:
        freshness = "unbound_target"
        reason = "the target analysis has no capture id"

    return {
        "schema_version": AUDIO_DELTA_SCHEMA,
        "kind": "audio_snapshot_delta",
        "project_id": base_project or target_project or None,
        "measurement": {
            "window": base_window,
            "base": {
                "analysis_id": base.get("analysis_id"),
                "capture_id": base.get("capture_id"),
                "revision": base.get("revision"),
                "fact_root": (base.get("input_fingerprint") or {}).get(
                    "capture_fact_root") if isinstance(base.get("input_fingerprint"),
                                                       Mapping) else None,
            },
            "target": {
                "analysis_id": target.get("analysis_id"),
                "capture_id": target.get("capture_id"),
                "revision": target.get("revision"),
                "fact_root": (target.get("input_fingerprint") or {}).get(
                    "capture_fact_root") if isinstance(target.get("input_fingerprint"),
                                                       Mapping) else None,
            },
        },
        "freshness": {"state": freshness, "reason": reason,
                      "current_capture_id": current_capture_id},
        "scope": {
            "sources": selected,
            "added_sources": added,
            "removed_sources": removed,
            "interval": dict(interval) if interval else None,
            "series": names,
            "metrics": "all" if wanted_metrics is None else sorted(wanted_metrics),
        },
        "metrics": {
            "columns": ["source", "metric", "unit", "before", "after", "delta"],
            "rows": metric_rows,
            "changed": len(metric_rows),
            "unchanged": unchanged_metrics,
            "missing": metric_missing,
            "incompatible": metric_incompatible,
            "note": ("exact values from each stored report; a null on either side is "
                     "listed as missing, never treated as zero"),
        },
        "series": {
            "rows": series_rows,
            "compared": sum(1 for row in series_rows if row.get("status") == "compared"),
            "incompatible": sum(1 for row in series_rows
                                if row.get("status") == "incompatible"),
            "changed_cells": sum(int(row.get("changed_cells") or 0)
                                 for row in series_rows),
            "note": ("cell-for-cell on the stored time axis; a run of changed cells is "
                     "reported as exact [start_s,end_s) intervals"),
        },
        "guarantees": {
            "exact": "values are the stored measurements of each analysis",
            "no_resampling": "no averaged or decimated grid is used for a delta",
            "no_quality_score": "numeric deltas only; no better/worse verdict",
            "original_records": ("both analyses remain addressable at "
                                 "reaper://analyses/{analysis_id}/report"),
        },
        "interpretation": ("A measured difference between two captures. It does not "
                           "identify a cause and does not replace the operation delta "
                           "returned by the edit that produced the change."),
    }


__all__ = [
    "AUDIO_DELTA_SCHEMA",
    "build_audio_delta",
]
