"""Deterministic cross-source relation candidates for the unified panorama.

This module computes *evidence of a relationship*, never a verdict.  It has no
knowledge of captures, revisions or archives: callers bind every candidate to
one capture/revision window and turn it into a typed ``RelationRecord`` in
``context.relations``.

The four producers are deliberately narrow:

* band dominance between two sources over the same active cells;
* pairwise phase/cancellation evidence already measured by ``analysis.phase``;
* a source-to-master level/band relation with an explicit arithmetic share;
* a project-to-reference metric difference on a matched stage and section.

Every candidate carries raw metrics, units, coverage, an explicit uncertainty
record and at least two live alternatives.  Missing data produces an explicit
unavailable reason on the returned batch, never a zero or an empty claim.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

BAND_DOMINANCE_METHOD = "analysis.relations.band_dominance"
PHASE_METHOD = "analysis.phase.compare_track_phase"
TRACK_MASTER_METHOD = "analysis.relations.track_master_level_relation"
REFERENCE_METHOD = "analysis.relations.reference_comparison"
METHOD_VERSION = "1"

BAND_FLOOR_DB = -80.0
DEFAULT_BAND_MIN_DB = 10.0
DEFAULT_BAND_TOP = 8

BAND_ALTERNATIVES = (
    "intentional doubling or arrangement weight",
    "performance or take difference",
    "mono-downmix or phase artefact",
)
PHASE_ALTERNATIVES = (
    "intentional width or polarity choice",
    "delay or polarity error",
    "different performances or takes",
)
MASTER_ALTERNATIVES = (
    "intentional balance between the source and the rest of the mix",
    "master-bus processing (compression, limiting, EQ, saturation)",
    "solo-track render missing returns, room or parallel processing",
)
REFERENCE_ALTERNATIVES = (
    "different arrangement, performance or section",
    "different processing chain or master",
    "stage, loudness or window mismatch in the comparison",
)

BAND_TABLE_COLUMNS = (
    "from_id",
    "to_id",
    "status",
    "best_band_index",
    "best_band_hz",
    "best_positive_db",
    "best_negative_db",
    "active_fraction",
    "pan_relation",
    "cell_count",
    "series_ref",
    "reason",
)

BAND_TEMPORAL_MODEL = {
    "representation": "pointer_to_aligned_source_band_level_time",
    "source_series": "#/analysis/<source_id>/curves/band_level_time_dbfs",
    "time_axis": "#/analysis/<source_id>/curves/spectrogram_time_s",
    "band_axis": "#/analysis/<source_id>/curves/band_centers_hz",
    "transform": (
        "per-cell broadband-mean subtraction on each source, then "
        "from-source minus to-source"
    ),
    "alignment": "shared_frame_count=min(frames_from, frames_to); no resampling",
    "row_series_ref": "band_pair:<from_id>|<to_id>",
    "row_series_resolution": (
        "series_ref identifies the pair; resolve both endpoint arrays through "
        "source_series, align on the first shared_frame_count cells, then apply transform"
    ),
    "units": {"band_difference": "dB"},
    "summary_only_fields": ["best_positive_db", "best_negative_db", "active_fraction"],
    "median_is_complete_temporal_relation": False,
    "retrieval": "panorama_slice indexes the named source series on the same grid",
}


@dataclass(frozen=True, slots=True)
class RelationCandidate:
    """One measured relationship before it is bound to a capture."""

    kind: str
    from_id: str
    to_id: str
    metrics: Mapping[str, float]
    units: Mapping[str, str]
    attributes: Mapping[str, Any]
    coverage: Mapping[str, Any]
    uncertainty: Mapping[str, Any]
    alternatives: tuple[str, ...]
    interpretation: str
    method: str
    method_version: str = METHOD_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "from_id": self.from_id,
            "to_id": self.to_id,
            "metrics": dict(self.metrics),
            "units": dict(self.units),
            "attributes": dict(self.attributes),
            "coverage": dict(self.coverage),
            "uncertainty": dict(self.uncertainty),
            "alternatives": list(self.alternatives),
            "interpretation": self.interpretation,
            "method": self.method,
            "method_version": self.method_version,
        }


@dataclass(frozen=True, slots=True)
class RelationBatch:
    """Canonical relation evidence plus completeness accounting.

    ``expected`` is the number of units the producer was asked to evaluate.
    ``evaluated`` counts units examined, including ``no_dominant_band``.
    ``observed`` counts emitted relations.  A complete batch therefore requires
    ``evaluated == expected`` and no unavailable unit, not ``observed == expected``.
    """

    candidates: tuple[RelationCandidate, ...] = ()
    expected: Mapping[str, int] = field(default_factory=dict)
    evaluated: Mapping[str, int] = field(default_factory=dict)
    observed: Mapping[str, int] = field(default_factory=dict)
    unavailable: Mapping[str, str] = field(default_factory=dict)
    tables: Mapping[str, Any] = field(default_factory=dict)
    truncated: Mapping[str, Any] = field(default_factory=dict)
    complete: bool = True


def merge_batches(*batches: RelationBatch) -> RelationBatch:
    """Combine producer outputs without hiding any unavailable reason."""

    candidates: list[RelationCandidate] = []
    expected: dict[str, int] = {}
    evaluated: dict[str, int] = {}
    observed: dict[str, int] = {}
    unavailable: dict[str, str] = {}
    tables: dict[str, Any] = {}
    truncated: dict[str, Any] = {}
    complete = True
    for batch in batches:
        candidates.extend(batch.candidates)
        for kind, count in batch.expected.items():
            expected[kind] = expected.get(kind, 0) + int(count)
        for kind, count in batch.evaluated.items():
            evaluated[kind] = evaluated.get(kind, 0) + int(count)
        for kind, count in batch.observed.items():
            observed[kind] = observed.get(kind, 0) + int(count)
        unavailable.update(batch.unavailable)
        tables.update(batch.tables)
        truncated.update(batch.truncated)
        complete = complete and batch.complete
    return RelationBatch(
        candidates=tuple(candidates),
        expected=expected,
        evaluated=evaluated,
        observed=observed,
        unavailable=unavailable,
        tables=tables,
        truncated=truncated,
        complete=complete,
    )


def top_band_dominance(batch: RelationBatch, *, limit: int = DEFAULT_BAND_TOP) -> RelationBatch:
    """Return a bounded convenience view; never a complete batch.

    The canonical batch keeps every evaluated pair.  This view is only a
    ranked convenience and marks itself incomplete with an explicit dropped
    count, so it cannot be mistaken for, or acknowledged as, complete evidence.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    ordered = sorted(
        batch.candidates, key=lambda item: item.metrics["band_delta_db"], reverse=True
    )
    selected = tuple(ordered[:limit])
    dropped = max(0, len(ordered) - len(selected))
    return RelationBatch(
        candidates=selected,
        expected=dict(batch.expected),
        evaluated=dict(batch.evaluated),
        observed=dict(batch.observed),
        unavailable=dict(batch.unavailable),
        tables={},
        truncated={"rule": "top_k_by_band_delta_db", "dropped": dropped},
        complete=False,
    )


@dataclass(frozen=True, slots=True)
class BandSource:
    """A source's time-by-band level matrix, in dB."""

    source_id: str
    cells: tuple[tuple[float, ...], ...]
    band_centers_hz: tuple[float, ...] | None = None
    pan: float | None = None


@dataclass(frozen=True, slots=True)
class SourceLevels:
    """The level facts needed for a source-to-master relation."""

    source_id: str
    rms_dbfs: float | None
    integrated_lufs: float | None = None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _unknown(reason: str) -> dict[str, Any]:
    return {"status": "unknown", "reason": reason}


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}


def _coerce_band_source(source_id: str, value: Any) -> BandSource:
    if isinstance(value, BandSource):
        return value
    if not isinstance(value, Mapping):
        raise ValueError(f"source {source_id!r} must be a mapping or BandSource")

    centers = value.get("band_centers_hz") or value.get("band_centers")
    if centers is not None:
        centers = tuple(float(item) for item in centers)

    rows: Any = value.get("bands")
    if rows is None:
        cells = value.get("cells")
        if isinstance(cells, Sequence) and not isinstance(cells, (str, bytes)):
            rows = [
                cell.get("bands") if isinstance(cell, Mapping) else cell
                for cell in cells
            ]
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
        raise ValueError(f"source {source_id!r} has no band cell matrix")

    matrix: list[tuple[float, ...]] = []
    width: int | None = None
    for row in rows:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or not row:
            raise ValueError(f"source {source_id!r} has an empty band row")
        values = tuple(float(item) for item in row)
        if width is None:
            width = len(values)
        elif len(values) != width:
            raise ValueError(f"source {source_id!r} has uneven band rows")
        matrix.append(values)
    if not matrix or width is None or width < 2:
        raise ValueError(f"source {source_id!r} needs at least two bands")

    pan = _finite(value.get("pan"))
    return BandSource(
        source_id=source_id,
        cells=tuple(matrix),
        band_centers_hz=centers,
        pan=pan,
    )


def _pan_relation(left: float | None, right: float | None) -> str:
    if left is None or right is None:
        return "unknown"
    if abs(left) < 0.5 or abs(right) < 0.5:
        return "unknown"
    return "same_side" if (left < 0) == (right < 0) else "opposite_side"


def _band_candidate(
    *,
    kind: str,
    from_id: str,
    to_id: str,
    band_index: int,
    band_hz: float | None,
    band_delta_db: float,
    broadband_delta_db: float,
    active_fraction: float,
    active_cells: int,
    cell_count: int,
    pan_relation: str,
    method: str,
) -> RelationCandidate:
    status = "complete" if active_cells == cell_count else "partial"
    reasons = [] if status == "complete" else ["inactive_or_silent_cells_excluded"]
    attributes: dict[str, Any] = {
        "band_index": int(band_index),
        "pan_relation": pan_relation,
        "is_psychoacoustic_threshold": False,
        "active_cells": int(active_cells),
        "cell_count": int(cell_count),
        "endpoint_stages": {
            "from": "source_render_post_fx",
            "to": "source_render_post_fx",
        },
        "stage_relation": "same_stage",
        "temporal_evidence": {
            "representation": "pointer_to_aligned_source_band_level_time",
            "from_series_ref": f"#/analysis/{from_id}/curves/band_level_time_dbfs",
            "to_series_ref": f"#/analysis/{to_id}/curves/band_level_time_dbfs",
            "time_ref": f"#/analysis/{from_id}/curves/spectrogram_time_s",
            "band_ref": f"#/analysis/{from_id}/curves/band_centers_hz",
            "transform": (
                "per-cell broadband-mean subtraction on each source, then "
                "from-source minus to-source"
            ),
            "median_is_complete_temporal_relation": False,
        },
    }
    if band_hz is not None:
        attributes["band_hz"] = float(band_hz)
    return RelationCandidate(
        kind=kind,
        from_id=from_id,
        to_id=to_id,
        metrics={
            "band_delta_db": float(band_delta_db),
            "broadband_delta_db": float(broadband_delta_db),
            "active_fraction": float(active_fraction),
        },
        units={"band_delta_db": "dB", "broadband_delta_db": "dB", "active_fraction": "ratio"},
        attributes=attributes,
        coverage={
            "status": status,
            "expected_cells": int(cell_count),
            "observed_cells": int(active_cells),
            "fraction": float(active_cells / cell_count),
            "reasons": reasons,
        },
        uncertainty=_unknown(
            "no calibrated audibility model for dense distorted material; "
            "the value is a same-cell level difference, not a masking threshold"
        ),
        alternatives=BAND_ALTERNATIVES,
        interpretation=(
            "Band-dominance locator: the from-source sits above the to-source in one "
            "band over the shared active cells. This is not audibility, not masking, "
            "and not an instruction to change either source."
        ),
        method=method,
    )


def band_dominance_candidates(
    sources: Mapping[str, Any],
    *,
    min_db: float = DEFAULT_BAND_MIN_DB,
    floor_db: float = BAND_FLOOR_DB,
    source_errors: Mapping[str, str] | None = None,
) -> RelationBatch:
    """Evaluate every source pair; emit relations only above ``min_db``.

    Every pair produces a columnar row with status ``evaluated``,
    ``no_dominant_band`` or ``unavailable`` (with a reason), so a complete
    snapshot never turns an unremarkable pair into a missing one.

    ``source_errors`` lets an interval caller mark a source unavailable before
    parsing (missing time axis, no frames, shape mismatch) without dropping its
    pairs from the canonical table.
    """

    if not math.isfinite(min_db) or min_db < 0:
        raise ValueError("min_db must be finite and non-negative")
    if isinstance(floor_db, bool) or not math.isfinite(floor_db):
        raise ValueError("floor_db must be finite")
    errors = dict(source_errors or {})

    source_count = len(sources)
    expected = source_count * (source_count - 1) // 2
    if expected == 0:
        return RelationBatch(
            expected={"band_dominance": 0},
            evaluated={"band_dominance": 0},
            observed={"band_dominance": 0},
            unavailable={"band_dominance": "need_at_least_two_sources"},
            tables={"band_dominance_pairs": {
                "columns": list(BAND_TABLE_COLUMNS), "rows": [],
                "expected_pairs": 0, "evaluated_pairs": 0, "relations_emitted": 0,
            }},
            complete=False,
        )

    parsed: dict[str, BandSource] = {}
    unavailable: dict[str, str] = {}
    for source_id, value in sources.items():
        if source_id in errors:
            unavailable[f"band_dominance:{source_id}"] = errors[source_id]
            continue
        try:
            parsed[source_id] = _coerce_band_source(source_id, value)
        except ValueError as exc:
            unavailable[f"band_dominance:{source_id}"] = str(exc)

    ids = sorted(sources)
    prepared: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for source_id, source in parsed.items():
        raw = np.asarray(source.cells, dtype=np.float64)
        filled = np.where(np.isfinite(raw), raw, floor_db)
        normalized = filled - filled.mean(axis=1, keepdims=True)
        prepared[source_id] = (filled, normalized, filled > floor_db)
    candidates: list[RelationCandidate] = []
    rows: list[list[Any]] = []
    evaluated_pairs = 0
    observed_pairs = 0

    for left_index, left_id in enumerate(ids):
        left = parsed.get(left_id)
        for right_id in ids[left_index + 1 :]:
            right = parsed.get(right_id)
            key = f"band_dominance:{left_id}|{right_id}"
            pair_ref = f"band_pair:{left_id}|{right_id}"
            if left is None or right is None:
                reason = (
                    errors.get(left_id) if left is None
                    else errors.get(right_id)
                ) or (
                    f"source_unavailable:{left_id}" if left is None
                    else f"source_unavailable:{right_id}"
                )
                unavailable[key] = reason
                rows.append([
                    left_id, right_id, "unavailable", None, None, None, None,
                    0.0, "unknown", 0, pair_ref, reason,
                ])
                continue
            left_raw, left_norm, left_active = prepared[left_id]
            right_raw, right_norm, right_active = prepared[right_id]
            if left_raw.shape[1] != right_raw.shape[1]:
                unavailable[key] = "band_count_mismatch"
                rows.append([left_id, right_id, "unavailable", None, None, None, None,
                             0.0, _pan_relation(left.pan, right.pan), 0,
                             pair_ref, "band_count_mismatch"])
                continue
            count = min(left_raw.shape[0], right_raw.shape[0])
            if count < 1:
                unavailable[key] = "no_shared_cells"
                rows.append([left_id, right_id, "unavailable", None, None, None, None,
                             0.0, _pan_relation(left.pan, right.pan), 0,
                             pair_ref, "no_shared_cells"])
                continue
            evaluated_pairs += 1
            a_raw = left_raw[:count]
            b_raw = right_raw[:count]
            diff = left_norm[:count] - right_norm[:count]
            active = left_active[:count] & right_active[:count]
            band_count = diff.shape[1]
            counts = active.sum(axis=0).astype(int)
            medians = np.full(band_count, np.nan)
            populated = np.flatnonzero(counts)
            if populated.size:
                masked = np.where(active[:, populated], diff[:, populated], np.nan)
                medians[populated] = np.nanmedian(masked, axis=0)

            pan = _pan_relation(left.pan, right.pan)
            if not np.any(np.isfinite(medians)):
                rows.append([left_id, right_id, "unavailable", None, None, None, None,
                             0.0, pan, int(count), pair_ref, "no_active_band_cells"])
                unavailable[key] = "no_active_band_cells"
                continue

            active_any = active.any(axis=1)
            broadband = float(
                np.median(a_raw[active_any].mean(axis=1) - b_raw[active_any].mean(axis=1))
            ) if np.any(active_any) else float(np.nanmedian(diff))
            positive = int(np.nanargmax(medians))
            negative = int(np.nanargmin(medians))
            positive_db = float(medians[positive])
            negative_db = float(-medians[negative])
            dominant = positive_db >= min_db or negative_db >= min_db
            best_band = positive if positive_db >= negative_db else negative
            best_band_hz = (
                float(left.band_centers_hz[best_band])
                if left.band_centers_hz is not None and best_band < len(left.band_centers_hz)
                else None
            )
            rows.append([
                left_id, right_id, "evaluated" if dominant else "no_dominant_band",
                int(best_band), best_band_hz, positive_db, negative_db,
                float(counts[best_band] / count) if count else 0.0,
                pan, int(count), pair_ref, "",
            ])

            if positive_db >= min_db:
                active_fraction = float(counts[positive] / count)
                candidates.append(
                    _band_candidate(
                        kind="band_dominance",
                        from_id=left_id,
                        to_id=right_id,
                        band_index=positive,
                        band_hz=(left.band_centers_hz[positive]
                                 if left.band_centers_hz is not None
                                 and positive < len(left.band_centers_hz) else None),
                        band_delta_db=positive_db,
                        broadband_delta_db=broadband,
                        active_fraction=active_fraction,
                        active_cells=int(counts[positive]),
                        cell_count=int(count),
                        pan_relation=pan,
                        method=BAND_DOMINANCE_METHOD,
                    )
                )
            if negative_db >= min_db:
                active_fraction = float(counts[negative] / count)
                candidates.append(
                    _band_candidate(
                        kind="band_dominance",
                        from_id=right_id,
                        to_id=left_id,
                        band_index=negative,
                        band_hz=(left.band_centers_hz[negative]
                                 if left.band_centers_hz is not None
                                 and negative < len(left.band_centers_hz) else None),
                        band_delta_db=negative_db,
                        broadband_delta_db=float(-broadband),
                        active_fraction=active_fraction,
                        active_cells=int(counts[negative]),
                        cell_count=int(count),
                        pan_relation=pan,
                        method=BAND_DOMINANCE_METHOD,
                    )
                )
            if dominant:
                observed_pairs += 1

    table = {
        "columns": list(BAND_TABLE_COLUMNS),
        "rows": sorted(rows, key=lambda row: (str(row[0]), str(row[1]))),
        "temporal_model": dict(BAND_TEMPORAL_MODEL),
        "units": {
            "best_band_hz": "Hz",
            "best_positive_db": "dB",
            "best_negative_db": "dB",
            "active_fraction": "ratio",
            "cell_count": "cells",
        },
        "expected_pairs": expected,
        "evaluated_pairs": evaluated_pairs,
        "relations_emitted": len(candidates),
    }
    complete = (
        len(parsed) == source_count
        and evaluated_pairs == expected
        and not unavailable
    )
    return RelationBatch(
        candidates=tuple(candidates),
        expected={"band_dominance": expected},
        evaluated={"band_dominance": evaluated_pairs},
        observed={"band_dominance": observed_pairs},
        unavailable=unavailable,
        tables={"band_dominance_pairs": table},
        complete=complete,
    )


def _pair_key(key: Any) -> tuple[str, str] | None:
    if isinstance(key, tuple) and len(key) == 2:
        left, right = key
        if isinstance(left, str) and isinstance(right, str):
            return (left, right) if left <= right else (right, left)
    if isinstance(key, str) and "|" in key:
        left, right = key.split("|", 1)
        if left and right:
            return (left, right) if left <= right else (right, left)
    return None


def phase_candidates(phase_results: Mapping[Any, Mapping[str, Any]]) -> RelationBatch:
    """Normalize ``compare_track_phase`` results into relation candidates."""

    candidates: list[RelationCandidate] = []
    unavailable: dict[str, str] = {}
    expected = 0
    evaluated = 0
    observed = 0
    for raw_key, result in phase_results.items():
        key = _pair_key(raw_key)
        if key is None or not isinstance(result, Mapping):
            continue
        expected += 1
        evaluated += 1
        left, right = key
        coverage = result.get("coverage") if isinstance(result.get("coverage"), Mapping) else {}
        if coverage.get("status") != "available":
            unavailable[f"phase_pair:{left}|{right}"] = str(
                coverage.get("reason") or "phase_unavailable"
            )
            continue
        channel_pairs = result.get("channel_pairs")
        if not isinstance(channel_pairs, Sequence) or not channel_pairs:
            unavailable[f"phase_pair:{left}|{right}"] = "no_channel_pairs"
            continue
        correlations = [
            value for pair in channel_pairs
            if isinstance(pair, Mapping)
            for value in [_finite(pair.get("zero_lag_correlation"))]
            if value is not None
        ]
        sum_changes = [
            value for pair in channel_pairs
            if isinstance(pair, Mapping)
            for value in [_finite(pair.get("power_change_vs_incoherent_sum_db"))]
            if value is not None
        ]
        if not correlations:
            unavailable[f"phase_pair:{left}|{right}"] = "correlation_unavailable"
            continue
        metrics: dict[str, float] = {
            "zero_lag_correlation": float(np.mean(correlations)),
        }
        units: dict[str, str] = {"zero_lag_correlation": "correlation"}
        if sum_changes:
            metrics["sum_change_db"] = float(np.mean(sum_changes))
            units["sum_change_db"] = "dB"
        observed += 1
        candidates.append(
            RelationCandidate(
                kind="phase_pair",
                from_id=left,
                to_id=right,
                metrics=metrics,
                units=units,
                attributes={
                    "channel_pair_count": len(channel_pairs),
                    "resampled": dict(result.get("resampled") or {}),
                },
                coverage={
                    "status": "complete" if correlations else "partial",
                    "expected_channel_pairs": len(channel_pairs),
                    "observed_channel_pairs": len(correlations),
                    "reasons": [] if len(correlations) == len(channel_pairs)
                    else ["some_channel_pairs_unavailable"],
                },
                uncertainty=_unknown(
                    "signal relationship only; correlation and summed-power change "
                    "do not establish an audible or unintended problem"
                ),
                alternatives=PHASE_ALTERNATIVES,
                interpretation=(
                    "Pairwise phase evidence on the aligned overlap. Low correlation "
                    "may be intended width; high correlation may be intended mono weight."
                ),
                method=PHASE_METHOD,
            )
        )

    if expected == 0:
        unavailable["phase_pair"] = "no_phase_results_supplied"
    return RelationBatch(
        candidates=tuple(candidates),
        expected={"phase_pair": expected},
        evaluated={"phase_pair": evaluated},
        observed={"phase_pair": observed},
        unavailable=unavailable,
        complete=evaluated == expected and not unavailable and expected > 0,
    )


def _report_metrics(report: Any) -> Mapping[str, Any]:
    if report is None:
        return {}
    if isinstance(report, Mapping):
        metrics = report.get("metrics")
        if isinstance(metrics, Mapping):
            return metrics
        return report
    metrics = getattr(report, "metrics", None)
    return metrics if isinstance(metrics, Mapping) else {}


def _metric_value(report: Any, name: str) -> float | None:
    cell = _report_metrics(report).get(name)
    if isinstance(cell, Mapping):
        value = cell.get("value")
    else:
        value = getattr(cell, "value", cell)
    return _finite(value)


def _metric_unit(report: Any, name: str) -> str | None:
    cell = _report_metrics(report).get(name)
    if isinstance(cell, Mapping):
        unit = cell.get("unit")
    else:
        unit = getattr(cell, "unit", None)
    return str(unit) if isinstance(unit, str) and unit else None


def _source_levels(source_id: str, value: Any) -> SourceLevels:
    if isinstance(value, SourceLevels):
        return value
    if isinstance(value, Mapping) and "rms_dbfs" in value and "metrics" not in value:
        return SourceLevels(
            source_id=source_id,
            rms_dbfs=_finite(value.get("rms_dbfs")),
            integrated_lufs=_finite(value.get("integrated_lufs")),
        )
    return SourceLevels(
        source_id=source_id,
        rms_dbfs=_metric_value(value, "rms_dbfs"),
        integrated_lufs=_metric_value(value, "integrated_lufs"),
    )


def band_dominance_interval(
    sources: Mapping[str, Any],
    *,
    start_s: float,
    end_s: float,
    min_db: float = DEFAULT_BAND_MIN_DB,
    floor_db: float = BAND_FLOOR_DB,
) -> RelationBatch:
    """Evaluate band dominance only on captured cells inside one interval.

    Frames are selected by their stored project-time coordinate with a
    half-open rule ``start_s <= t < end_s``.  The function never resamples and
    never reuses a whole-window median.  A frame whose centre lies inside the
    interval is used as-is; if a boundary frame exists outside the interval,
    the source records ``partial_start``/``partial_end`` rather than silently
    treating the partial bucket as complete.
    """

    if (isinstance(start_s, bool) or isinstance(end_s, bool)
            or not math.isfinite(float(start_s)) or not math.isfinite(float(end_s))
            or float(end_s) <= float(start_s)):
        raise ValueError("start_s and end_s must be finite and start_s < end_s")
    start = float(start_s)
    end = float(end_s)
    sliced: dict[str, Any] = {}
    errors: dict[str, str] = {}
    windows: dict[str, Any] = {}

    for source_id, value in sources.items():
        if not isinstance(value, Mapping):
            errors[source_id] = "source_payload_unavailable"
            sliced[source_id] = {"bands": []}
            continue
        bands = value.get("bands")
        times = value.get("times")
        if not isinstance(bands, (list, tuple)) or not isinstance(times, (list, tuple)):
            errors[source_id] = "time_axis_or_band_series_unavailable"
            sliced[source_id] = {"bands": []}
            continue
        if len(bands) != len(times):
            errors[source_id] = "time_axis_length_mismatch"
            sliced[source_id] = {"bands": []}
            continue
        parsed_times: list[float] = []
        for item in times:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                parsed_times = []
                break
            parsed_times.append(float(item))
        if not parsed_times:
            errors[source_id] = "time_axis_not_numeric"
            sliced[source_id] = {"bands": []}
            continue
        selected = [index for index, timestamp in enumerate(parsed_times)
                    if start <= timestamp < end]
        if not selected:
            errors[source_id] = "no_frames_in_interval"
            sliced[source_id] = {"bands": []}
            continue
        first, last = selected[0], selected[-1]
        windows[source_id] = {
            "selected_frames": len(selected),
            "first_frame_index": int(first),
            "last_frame_index": int(last),
            "first_time_s": parsed_times[first],
            "last_time_s": parsed_times[last],
            "partial_start": bool(first > 0 and parsed_times[first] > start),
            "partial_end": bool(last + 1 < len(parsed_times)
                                and parsed_times[last + 1] >= end),
            "time_basis": "stored_frame_center_project_s",
            "selection": "start_s <= frame_time_s < end_s",
        }
        sliced[source_id] = {
            "bands": [list(bands[index]) for index in selected],
            "band_centers_hz": value.get("band_centers_hz"),
            "pan": value.get("pan"),
        }

    batch = band_dominance_candidates(
        sliced, min_db=min_db, floor_db=floor_db, source_errors=errors
    )
    table = batch.tables.get("band_dominance_pairs")
    if isinstance(table, dict):
        table["interval"] = {
            "start_s": start,
            "end_s": end,
            "selection": "start_s <= stored_frame_center_project_s < end_s",
            "resampling": "none",
            "partial_bucket_policy": (
                "frame centres inside the interval are used; a boundary frame "
                "outside the interval is reported as partial_start/partial_end, "
                "never interpolated or counted as complete"
            ),
            "sources": windows,
        }
    return batch


def track_master_candidates(
    sources: Mapping[str, Any],
    master: Any,
    *,
    master_id: str = "master",
) -> RelationBatch:
    """Relate each source render to the master by level and an arithmetic share."""

    master_rms = _metric_value(master, "rms_dbfs")
    master_lufs = _metric_value(master, "integrated_lufs")
    if master_rms is None and master_lufs is None:
        return RelationBatch(
            expected={"track_master_level_relation": len(sources)},
            evaluated={"track_master_level_relation": 0},
            observed={"track_master_level_relation": 0},
            unavailable={"track_master_level_relation": "master_level_unavailable"},
            complete=False,
        )

    levels = {source_id: _source_levels(source_id, value)
              for source_id, value in sources.items()}
    usable = {source_id: level for source_id, level in levels.items()
              if level.rms_dbfs is not None}
    if not usable:
        return RelationBatch(
            expected={"track_master_level_relation": len(sources)},
            evaluated={"track_master_level_relation": 0},
            observed={"track_master_level_relation": 0},
            unavailable={"track_master_level_relation": "no_source_rms_available"},
            complete=False,
        )

    powers = {source_id: 10.0 ** (level.rms_dbfs / 10.0)
              for source_id, level in usable.items()}
    total_power = float(sum(powers.values()))
    summed_db = 10.0 * math.log10(total_power) if total_power > 0 else None
    residual = (summed_db - master_rms
                if summed_db is not None and master_rms is not None else None)

    candidates: list[RelationCandidate] = []
    for source_id in sorted(usable):
        level = usable[source_id]
        metrics: dict[str, float] = {
            "share_of_summed_power_pct": float(100.0 * powers[source_id] / total_power)
            if total_power > 0 else 0.0,
        }
        units = {"share_of_summed_power_pct": "%"}
        if master_rms is not None and level.rms_dbfs is not None:
            metrics["source_minus_master_rms_db"] = float(level.rms_dbfs - master_rms)
            units["source_minus_master_rms_db"] = "dB"
        if master_lufs is not None and level.integrated_lufs is not None:
            metrics["source_minus_master_lufs_db"] = float(
                level.integrated_lufs - master_lufs
            )
            units["source_minus_master_lufs_db"] = "dB"
        if residual is not None:
            metrics["summed_sources_minus_master_db"] = float(residual)
            units["summed_sources_minus_master_db"] = "dB"
        candidates.append(
            RelationCandidate(
                kind="track_master_level_relation",
                from_id=source_id,
                to_id=master_id,
                metrics=metrics,
                units=units,
                attributes={
                    "metric_family": "summed_source_power_share",
                    "causal_contribution": False,
                    "master_bus_nonlinearity": "not_modelled",
                    "returns_and_room": "absent_from_solo_renders",
                    "pdc": "not_modelled",
                    "endpoint_stages": {
                        "from": "source_render_post_fx",
                        "to": "master_render_post_fx",
                    },
                    "stage_relation": "cross_stage",
                    "share_basis": "arithmetic_power_of_summed_source_renders",
                    "master_id": master_id,
                    "caveat": (
                        "share is a ratio of summed source renders, not a measured "
                        "contribution through master-bus processing, returns or PDC"
                    ),
                },
                coverage={
                    "status": "complete",
                    "source_count": len(usable),
                    "reasons": [],
                },
                uncertainty=_unknown(
                    "solo renders omit returns, room and parallel paths; the master "
                    "may be nonlinear, so the share is arithmetic evidence only"
                ),
                alternatives=MASTER_ALTERNATIVES,
                interpretation=(
                    "Source-to-master level relation. The share is arithmetic over the "
                    "supplied solo renders; it is not a fader, not a causal contribution "
                    "through master FX or PDC, and not a quality judgment."
                ),
                method=TRACK_MASTER_METHOD,
            )
        )

    return RelationBatch(
        candidates=tuple(candidates),
        expected={"track_master_level_relation": len(sources)},
        evaluated={"track_master_level_relation": len(usable)},
        observed={"track_master_level_relation": len(candidates)},
        unavailable={} if len(usable) == len(sources) else {
            "track_master_level_relation": "some_sources_missing_rms"
        },
        complete=len(usable) == len(sources),
    )


def reference_candidates(
    project: Any,
    references: Sequence[Mapping[str, Any]],
    *,
    stage: str,
    section: str | None = None,
    metric_names: Sequence[str] | None = None,
) -> RelationBatch:
    """Emit project-to-reference metric differences on a matched stage."""

    project_metrics = _report_metrics(project)
    if not project_metrics:
        return RelationBatch(
            unavailable={"reference_comparison": "project_metrics_unavailable"}
        )
    allowed = set(metric_names) if metric_names is not None else None
    candidates: list[RelationCandidate] = []
    unavailable: dict[str, str] = {}
    evaluated = 0
    observed = 0

    for reference in references:
        if not isinstance(reference, Mapping):
            continue
        reference_id = reference.get("reference_id")
        if not isinstance(reference_id, str) or not reference_id:
            continue
        evaluated += 1
        reference_stage = reference.get("stage")
        if reference_stage != stage:
            unavailable[f"reference_comparison:{reference_id}"] = "stage_mismatch"
            continue
        reference_metrics = _report_metrics(reference)
        names = sorted(set(project_metrics) & set(reference_metrics))
        if allowed is not None:
            names = [name for name in names if name in allowed]
        metrics: dict[str, float] = {}
        units: dict[str, str] = {}
        for name in names:
            mine = _metric_value(project, name)
            theirs = _metric_value(reference, name)
            if mine is None or theirs is None:
                continue
            unit = _metric_unit(project, name) or "unknown"
            metrics[f"delta_{name}"] = float(mine - theirs)
            units[f"delta_{name}"] = unit
        if not metrics:
            unavailable[f"reference_comparison:{reference_id}"] = "no_comparable_metrics"
            continue
        observed += 1
        candidates.append(
            RelationCandidate(
                kind="reference_comparison",
                from_id="project",
                to_id=reference_id,
                metrics=metrics,
                units=units,
                attributes={
                    "reference_id": reference_id,
                    "reference_stage": reference_stage,
                    "section": section,
                    "shared_metric_count": len(metrics),
                    "not_a_score": True,
                },
                coverage={
                    "status": "complete" if len(metrics) == len(names) else "partial",
                    "shared_metric_count": len(metrics),
                    "candidate_metric_count": len(names),
                    "reasons": [] if len(metrics) == len(names)
                    else ["some_shared_metrics_missing_values"],
                },
                uncertainty=_unknown(
                    "reference material and processing differ; metric deltas are "
                    "descriptive and do not identify a cause or a target"
                ),
                alternatives=REFERENCE_ALTERNATIVES,
                interpretation=(
                    "Project-to-reference metric difference on the declared stage and "
                    "section. Descriptive only; not a score, rank or instruction."
                ),
                method=REFERENCE_METHOD,
            )
        )

    if not references:
        unavailable["reference_comparison"] = "no_reference_supplied"
    return RelationBatch(
        candidates=tuple(candidates),
        expected={"reference_comparison": len(references)},
        evaluated={"reference_comparison": evaluated},
        observed={"reference_comparison": observed},
        unavailable=unavailable,
        complete=evaluated == len(references) and not unavailable and len(references) > 0,
    )


__all__ = [
    "BAND_DOMINANCE_METHOD",
    "BAND_FLOOR_DB",
    "BAND_TABLE_COLUMNS",
    "BAND_TEMPORAL_MODEL",
    "DEFAULT_BAND_MIN_DB",
    "DEFAULT_BAND_TOP",
    "METHOD_VERSION",
    "PHASE_METHOD",
    "REFERENCE_METHOD",
    "TRACK_MASTER_METHOD",
    "BandSource",
    "RelationBatch",
    "RelationCandidate",
    "SourceLevels",
    "band_dominance_candidates",
    "band_dominance_interval",
    "merge_batches",
    "phase_candidates",
    "reference_candidates",
    "top_band_dominance",
    "track_master_candidates",
]
