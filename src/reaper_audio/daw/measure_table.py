"""Table shape for one ``measure`` call: metrics, band aggregation, aggregates.

The renderer and the analyzer already produce one report per source and
window.  This module only decides *what is shown*: which metrics leave the
library, how the 28 measured band levels are aggregated, and the descriptive
per-row/per-column summaries.  It never recomputes a measurement and never
applies a target.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, log10
from typing import Any, Mapping, Sequence

import numpy as np

from ..errors import ValidationError
from ..analysis.metrics import DB_FLOOR

#: Standard octave-band centres (IEC 61260 base-2 grid).  A grid, not a mix
#: decision: every measured band is assigned to its nearest centre.
OCTAVE_CENTERS_HZ: tuple[float, ...] = (
    31.5, 63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0, 16000.0,
)
#: Standard one-third-octave centres; identical to the analyzer's own band grid.
THIRD_OCTAVE_CENTERS_HZ: tuple[float, ...] = (
    25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0, 160.0, 200.0,
    250.0, 315.0, 400.0, 500.0, 630.0, 800.0, 1000.0, 1250.0, 1600.0, 2000.0,
    2500.0, 3150.0, 4000.0, 5000.0, 6300.0, 8000.0, 10000.0, 12500.0,
)

#: Canonical metric keys produced by ``analysis.metrics.measure_audio``.
MEASURABLE_METRICS: tuple[str, ...] = (
    "peak_dbfs",
    "true_peak_estimate_dbfs",
    "rms_dbfs",
    "crest_db",
    "integrated_lufs",
    "loudness_range_lu",
    "clipped_sample_ratio",
    "spectral_centroid_hz",
    "transient_density_per_s",
    "transient_count",
    "stereo_correlation",
    "side_mid_db",
    "mono_fold_loss_db",
    "modulation_rate_hz",
    "modulation_depth",
    "pitch_jitter_pct",
    "coarse_speech_band_ratio",
    "dc_offset",
    "clipped_sample_count",
    "two_ms_event_count",
    "onset_count",
    "spike_count",
    "clipping_event_count",
    "local_tempo_median_bpm",
)

#: Short names a caller may use instead of the canonical report keys.
METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "lufs": ("integrated_lufs",),
    "loudness": ("integrated_lufs",),
    "lra": ("loudness_range_lu",),
    "peak": ("peak_dbfs",),
    "true_peak": ("true_peak_estimate_dbfs",),
    "rms": ("rms_dbfs",),
    "crest": ("crest_db",),
    "centroid": ("spectral_centroid_hz",),
    "side_mid": ("side_mid_db",),
    "stereo": ("stereo_correlation", "side_mid_db", "mono_fold_loss_db"),
    "clips": ("clipped_sample_count", "clipped_sample_ratio",
              "clipping_event_count"),
    "transients": ("transient_count", "transient_density_per_s"),
    "events": ("two_ms_event_count", "onset_count", "spike_count"),
    "tempo": ("local_tempo_median_bpm",),
    "dc": ("dc_offset",),
    "modulation": ("modulation_rate_hz", "modulation_depth"),
}

BANDS_KEY = "band_level_dbfs"

BAND_MODE_NONE = "none"
BAND_MODE_OCTAVE = "octave"
BAND_MODE_THIRD = "third"
BAND_MODE_EXPLICIT = "explicit"
BAND_MODES = (BAND_MODE_NONE, BAND_MODE_OCTAVE, BAND_MODE_THIRD, BAND_MODE_EXPLICIT)


@dataclass(frozen=True, slots=True)
class MetricSelection:
    """Which metrics leave the library and under which names."""

    requested: tuple[str, ...]
    keys: tuple[str, ...]
    bands: bool

    def to_dict(self) -> dict[str, Any]:
        return {"requested": list(self.requested), "keys": list(self.keys),
                "bands": self.bands, "all": self.requested == ("all",)}


@dataclass(frozen=True, slots=True)
class BandPlan:
    """How the measured 1/3-octave bands are aggregated."""

    mode: str = BAND_MODE_NONE
    bands: tuple[tuple[float, float, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        if self.mode == BAND_MODE_NONE:
            return {"mode": BAND_MODE_NONE}
        return {"mode": self.mode,
                "centers_hz": [round((low * high) ** 0.5, 3)
                               for low, high, _label in self.bands],
                "edges_hz": [[round(low, 3), round(high, 3)]
                             for low, high, _label in self.bands]}


def parse_metrics(spec: Any) -> MetricSelection:
    """Resolve ``metrics`` into canonical report keys.

    ``None`` keeps every scalar metric but not the band array; ``"all"`` (or
    ``["*"]``) also includes the measured bands.  Unknown names are refused
    with the accepted list instead of silently returning ``null``.
    """
    if spec is None:
        return MetricSelection(requested=(), keys=MEASURABLE_METRICS, bands=False)
    if isinstance(spec, str):
        if spec.strip().lower() in {"all", "*"}:
            return MetricSelection(requested=("all",), keys=MEASURABLE_METRICS,
                                   bands=True)
        spec = [spec]
    if not isinstance(spec, Sequence) or isinstance(spec, (str, bytes)):
        raise ValidationError("metrics must be a list of metric names",
                              details={"metrics": spec})
    requested: list[str] = []
    keys: list[str] = []
    bands = False
    for raw in spec:
        if not isinstance(raw, str) or not raw.strip():
            raise ValidationError("Every metric name must be a non-empty string",
                                  details={"metric": raw})
        name = raw.strip().lower()
        if name in requested:
            continue
        requested.append(name)
        if name in {"all", "*"}:
            bands = True
            for key in MEASURABLE_METRICS:
                if key not in keys:
                    keys.append(key)
            continue
        if name == "bands":
            bands = True
            continue
        expanded = METRIC_ALIASES.get(name)
        if expanded is None:
            if name not in MEASURABLE_METRICS:
                raise ValidationError(
                    "Unknown metric name",
                    details={"metric": raw,
                             "aliases": sorted(METRIC_ALIASES),
                             "metrics": list(MEASURABLE_METRICS),
                             "next_action": ("use an alias such as 'lufs', "
                                             "'peak', 'crest', 'bands', or a "
                                             "canonical metric key")})
            expanded = (name,)
        for key in expanded:
            if key not in keys:
                keys.append(key)
    if not keys and not bands:
        raise ValidationError("metrics must select at least one metric")
    return MetricSelection(requested=tuple(requested), keys=tuple(keys), bands=bands)


def _band_edges_for(centers: Sequence[float], width_octaves: float
                    ) -> tuple[tuple[float, float, str], ...]:
    half = 2.0 ** (width_octaves / 2.0)
    return tuple((center / half, center * half, f"{center:g} Hz") for center in centers)


def parse_bands(spec: Any) -> BandPlan:
    """Resolve ``bands`` into a plan; ``None`` keeps the raw band array only."""
    if spec is None:
        return BandPlan()
    if isinstance(spec, str):
        name = spec.strip().lower()
        if name in {"none", "off", "raw", "measured"}:
            return BandPlan()
        if name in {"octave", "oct", "1/1"}:
            return BandPlan(mode=BAND_MODE_OCTAVE,
                            bands=_band_edges_for(OCTAVE_CENTERS_HZ, 1.0))
        if name in {"third", "third_octave", "1/3"}:
            return BandPlan(mode=BAND_MODE_THIRD,
                            bands=_band_edges_for(THIRD_OCTAVE_CENTERS_HZ, 1.0 / 3.0))
        raise ValidationError(
            "bands must be 'octave', 'third', [[lo_hz, hi_hz], ...] or null",
            details={"bands": spec})
    if isinstance(spec, Mapping):
        kind = str(spec.get("kind") or spec.get("mode") or "").strip().lower()
        if kind in {"octave", "third"}:
            return parse_bands(kind)
        if "bands" in spec:
            return parse_bands(spec["bands"])
        raise ValidationError("bands mapping needs kind='octave'|'third' or bands=[...]",
                              details={"bands": spec})
    if isinstance(spec, Sequence) and not isinstance(spec, (str, bytes)):
        rows: list[tuple[float, float, str]] = []
        for entry in spec:
            if isinstance(entry, Mapping):
                low_raw = entry.get("lo_hz", entry.get("low_hz", entry.get("start_hz")))
                high_raw = entry.get("hi_hz", entry.get("high_hz", entry.get("end_hz")))
                label = str(entry.get("label") or "")
            elif isinstance(entry, Sequence) and not isinstance(entry, (str, bytes)):
                if len(entry) != 2:
                    raise ValidationError("Every band must be [lo_hz, hi_hz]",
                                          details={"band": list(entry)})
                low_raw, high_raw = entry
                label = ""
            else:
                raise ValidationError("Every band must be [lo_hz, hi_hz] or a mapping",
                                      details={"band": entry})
            try:
                low, high = float(low_raw), float(high_raw)
            except (TypeError, ValueError) as exc:
                raise ValidationError("Band edges must be numbers in Hz",
                                      details={"band": entry}) from exc
            if not (isfinite(low) and isfinite(high)) or low <= 0 or high <= low:
                raise ValidationError("Band must satisfy 0 < lo_hz < hi_hz",
                                      details={"band": entry})
            rows.append((low, high, label or f"{low:g}-{high:g} Hz"))
        if not rows:
            raise ValidationError("bands must not be empty")
        return BandPlan(mode=BAND_MODE_EXPLICIT, bands=tuple(rows))
    raise ValidationError("bands must be 'octave', 'third', [[lo_hz, hi_hz], ...] or null",
                          details={"bands": spec})


def _db_from_power(power: float) -> float:
    if not isfinite(power) or power <= 0.0:
        return DB_FLOOR
    return float(max(DB_FLOOR, 10.0 * log10(power)))


def aggregate_bands(centers: Sequence[float], levels: Sequence[float],
                    plan: BandPlan) -> dict[str, Any]:
    """Power-sum the measured bands into the planned bands.

    Every measured band goes to the planned band whose edge range contains its
    centre; a plan band that receives nothing reports ``null`` instead of a
    substitute.  Levels are in dBFS per band, so they are converted to power,
    summed and converted back.
    """
    pairs: list[tuple[float, float]] = []
    for center, level in zip(centers, levels):
        try:
            center_f, level_f = float(center), float(level)
        except (TypeError, ValueError):
            continue
        if not isfinite(center_f) or not isfinite(level_f):
            continue
        pairs.append((center_f, level_f))
    aggregated: list[float | None] = []
    used: list[int] = []
    for low, high, _label in plan.bands:
        members = [level for center, level in pairs if low <= center < high]
        if not members:
            aggregated.append(None)
            used.append(0)
            continue
        if all(level <= DB_FLOOR for level in members):
            aggregated.append(DB_FLOOR)
        else:
            power = sum(10.0 ** (level / 10.0) for level in members
                        if level > DB_FLOOR)
            aggregated.append(_db_from_power(power))
        used.append(len(members))
    return {
        "mode": plan.mode,
        "centers_hz": [round((low * high) ** 0.5, 3) for low, high, _l in plan.bands],
        "edges_hz": [[round(low, 3), round(high, 3)] for low, high, _l in plan.bands],
        "labels": [label for _low, _high, label in plan.bands],
        "level_dbfs": [None if value is None else round(value, 2)
                       for value in aggregated],
        "member_counts": used,
        "source_band_count": len(pairs),
    }


def _metric_value(report_metrics: Mapping[str, Any], key: str
                  ) -> tuple[float | None, str]:
    raw = report_metrics.get(key)
    if isinstance(raw, Mapping):
        value = raw.get("value")
        unit = str(raw.get("unit") or "")
    else:
        value = raw
        unit = ""
    if value is None or isinstance(value, bool):
        return None, unit
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, unit
    if not isfinite(number):
        return None, unit
    return number, unit


def build_cell(report: Mapping[str, Any], selection: MetricSelection,
               band_plan: BandPlan) -> tuple[dict[str, Any], dict[str, str]]:
    """One table cell plus the units of the metrics it carries."""
    metrics = report.get("metrics") or {}
    curves = report.get("curves") or {}
    cell: dict[str, Any] = {}
    units: dict[str, str] = {}
    for key in selection.keys:
        value, unit = _metric_value(metrics, key)
        cell[key] = None if value is None else round(value, 4)
        if unit:
            units[key] = unit
    if selection.bands:
        levels = curves.get(BANDS_KEY)
        cell[BANDS_KEY] = list(levels) if isinstance(levels, Sequence) else None
        centers = curves.get("band_centers_hz") or []
        if band_plan.mode != BAND_MODE_NONE:
            cell["band_aggregate"] = aggregate_bands(centers, levels or [], band_plan)
            cell["band_aggregate"].pop("source_band_count", None)
        units[BANDS_KEY] = "dBFS"
    return cell, units


def _summary(values: Sequence[float | None]) -> dict[str, Any] | None:
    numbers = np.asarray([value for value in values if value is not None], dtype=float)
    if numbers.size == 0:
        return None
    return {"count": int(numbers.size),
            "mean": round(float(numbers.mean()), 4),
            "min": round(float(numbers.min()), 4),
            "max": round(float(numbers.max()), 4)}


def summarize_rows(rows: Sequence[Mapping[str, Any]],
                   columns: Sequence[Mapping[str, Any]],
                   cells: Sequence[Mapping[str, Mapping[str, Any]]],
                   selection: MetricSelection) -> dict[str, Any]:
    """Descriptive mean/min/max per row across sources and per column across rows.

    No target, no verdict: a summary of the numbers already in the table.  Only
    scalar metrics take part; the band array is not averaged.
    """
    row_stats: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        per_metric: dict[str, Any] = {}
        for key in selection.keys:
            summary = _summary([cells[index].get(column["source_id"], {}).get(key)
                                for column in columns])
            if summary is not None:
                per_metric[key] = summary
        row_stats.append({"index": row.get("index", index),
                          "interval": {"start_s": row.get("start_s"),
                                       "end_s": row.get("end_s")},
                          "label": row.get("label", ""),
                          "metrics": per_metric})
    column_stats: list[dict[str, Any]] = []
    for column_index, column in enumerate(columns):
        source_id = column["source_id"]
        per_metric = {}
        for key in selection.keys:
            summary = _summary([cells[row_index].get(source_id, {}).get(key)
                                for row_index in range(len(rows))])
            if summary is not None:
                per_metric[key] = summary
        column_stats.append({"source_id": source_id,
                             "name": column.get("name", ""),
                             "metrics": per_metric})
    return {"criterion": "mean/min/max of the table values; no target or verdict",
            "rows": row_stats, "columns": column_stats}


__all__ = [
    "BAND_MODES",
    "BANDS_KEY",
    "BandPlan",
    "METRIC_ALIASES",
    "MEASURABLE_METRICS",
    "MetricSelection",
    "OCTAVE_CENTERS_HZ",
    "THIRD_OCTAVE_CENTERS_HZ",
    "aggregate_bands",
    "build_cell",
    "parse_bands",
    "parse_metrics",
    "summarize_rows",
]
