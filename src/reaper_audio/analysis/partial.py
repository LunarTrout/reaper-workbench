"""Per-source partial loudness behind an explicit reconstruction gate.

The leave-one-source-out arithmetic in this module subtracts a source's band
power from the declared node's band power. That subtraction is only meaningful
when the supplied source renders actually reconstruct the declared node render
at the same sample timeline, window, channel view and processing stage. The
module therefore measures that reconstruction first and emits per-source values
only when the measurement, the provenance and the declaration all agree.

When the gate does not pass, the payload carries ``status="unavailable"``, an
explicit reason, the gate's inputs and (when the buffers allow it) the measured
residual. It emits no share, no ratio, no "too quiet"/"exposed" label and no
substitute estimate.

Shares that the gate does admit are raw outputs of the model arithmetic. They
are not audibility, they are relative to the supplied sources' summed modelled
partial loudness (not to the mix), and no threshold or classification is
applied to them here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import numpy as np
from scipy.signal import butter, sosfilt

from ..errors import ValidationError
from ..models import AudioBuffer, Section
from .audio import slice_audio
from .perceptual import (BARK_CENTERS, BARK_REGIONS, THIRD_OCTAVE_CENTERS_HZ,
                         third_octave_levels, zwicker_series)

PARTIAL_LOUDNESS_SCHEMA_VERSION = 2
GATE_METHOD = "sum_of_source_renders_vs_declared_node_band_power"
GATE_METHOD_VERSION = 1
DEFAULT_MAX_MEDIAN_ERROR_DB = 1.0
DEFAULT_MAX_P95_ERROR_DB = 3.0
DECLARATION_FIELDS = ("node", "stage", "channel_view")

THIRD_OCTAVE_29_HZ = np.asarray([
    25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400,
    500, 630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000,
    6300, 8000, 10000, 12500, 16000,
], dtype=np.float64)
FREQUENCY_REGIONS_HZ = (
    ("sub", 0.0, 60.0), ("bass", 60.0, 250.0), ("lowmid", 250.0, 560.0),
    ("mid", 560.0, 1278.0), ("upmid", 1278.0, 3000.0),
    ("pres", 3000.0, 6000.0), ("air", 6000.0, 30000.0),
)
DB_FLOOR = -60.0
# Levels at or below this value are the "band not measured" sentinel written by
# _bands29, not a quiet band. Cells where both sides are unmeasured carry no
# error and are excluded from the gate statistics.
UNMEASURED_LEVEL_DB = -299.0
EPSILON_POWER = 1e-30


def _bands29(samples: np.ndarray, sample_rate: int, hop_s: float) -> tuple[np.ndarray, np.ndarray]:
    x = np.asarray(samples, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    if x.ndim != 2 or x.shape[0] == 0 or x.shape[1] == 0:
        raise ValidationError("Audio for partial loudness must be non-empty")
    hop = max(1, int(round(hop_s * sample_rate)))
    frame_count = max(1, 1 + (len(x) - hop) // hop)
    padded_frames = frame_count * hop
    if padded_frames > len(x):
        x = np.pad(x, ((0, padded_frames - len(x)), (0, 0)))
    frames = x[:padded_frames].reshape(frame_count, hop, x.shape[1])
    levels = np.full((frame_count, len(THIRD_OCTAVE_29_HZ)), -300.0, dtype=np.float64)
    nyquist = sample_rate / 2.0
    for band_index, center_hz in enumerate(THIRD_OCTAVE_29_HZ):
        low_hz = center_hz / 2 ** (1 / 6)
        high_hz = center_hz * 2 ** (1 / 6)
        if low_hz >= nyquist or center_hz >= nyquist:
            continue
        upper = min(high_hz, nyquist * 0.99)
        lower = max(low_hz, 10.0)
        if lower >= upper:
            continue
        sos = butter(4, [lower, upper], btype="bandpass", fs=sample_rate, output="sos")
        filtered = sosfilt(sos, x, axis=0)
        filtered_frames = filtered[:padded_frames].reshape(frame_count, hop, x.shape[1])
        mean_square = np.sum(np.mean(filtered_frames ** 2, axis=1), axis=1)
        levels[:, band_index] = 10.0 * np.log10(
            np.maximum(mean_square, 1e-30) / (20e-6) ** 2
        )
    return levels, np.arange(frame_count, dtype=np.float64) * hop_s


def _moore_glasberg(levels: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    from phonometry import psychoacoustics

    sones = np.empty(len(levels), dtype=np.float64)
    specific = np.empty((len(levels), 372), dtype=np.float64)
    centers = None
    for index, row in enumerate(levels):
        result = psychoacoustics.loudness_moore_glasberg_from_third_octave(row)
        sones[index] = float(result.loudness)
        specific[index] = np.asarray(result.specific, dtype=np.float64)
        if centers is None:
            centers = np.asarray(result.centre_frequencies, dtype=np.float64)
    if centers is None:
        raise ValidationError("Perceptual model returned no frequency centers")
    return sones, specific, centers


def _model_series(levels: np.ndarray, model: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if model == "iso532-2":
        return _moore_glasberg(levels)
    if model == "iso532-1":
        sones, _phons, specific, _sharpness = zwicker_series(levels)
        return sones, specific, BARK_CENTERS
    raise ValidationError("model must be 'iso532-1' or 'iso532-2'")


def _region_masks(centers: np.ndarray, model: str) -> dict[str, np.ndarray]:
    if model == "iso532-1":
        regions = BARK_REGIONS
        return {name: (centers >= low) & (centers < high)
                for name, low, high in regions}
    return {
        name: (centers >= low) & (centers < high)
        for name, low, high in FREQUENCY_REGIONS_HZ
    }


def _band_centers(model: str) -> np.ndarray:
    return THIRD_OCTAVE_CENTERS_HZ if model == "iso532-1" else THIRD_OCTAVE_29_HZ


def _source_levels(samples: np.ndarray, sample_rate: int, hop_s: float,
                   model: str) -> tuple[np.ndarray, np.ndarray]:
    """Band levels from the same decomposition the selected model consumes."""
    if model == "iso532-2":
        return _bands29(samples, sample_rate, hop_s)
    return third_octave_levels(samples, sample_rate, hop_s=hop_s)


def _label(value: Any) -> str | None:
    """Normalise a declared render label; anything unusable becomes missing."""
    if isinstance(value, str):
        return value.strip() or None
    return None


def _declared_window(entry: Mapping) -> dict | None:
    window = entry.get("window")
    if window is None:
        return None
    if not isinstance(window, Mapping):
        return None
    try:
        start_s = float(window["start_s"])
        end_s = float(window["end_s"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (np.isfinite(start_s) and np.isfinite(end_s)) or end_s <= start_s:
        return None
    return {"start_s": start_s, "end_s": end_s}


def _validate_buffers(mix: AudioBuffer, stems: Mapping[str, AudioBuffer],
                      section: Section | None) -> tuple[AudioBuffer, dict[str, AudioBuffer]]:
    if not isinstance(mix, AudioBuffer):
        raise ValidationError("mix must be an AudioBuffer")
    if not isinstance(stems, Mapping) or not stems:
        raise ValidationError("stems must map source IDs to AudioBuffer values")
    if any(not isinstance(key, str) or not key.strip() for key in stems):
        raise ValidationError("Every source needs a non-empty string ID")
    if any(not isinstance(audio, AudioBuffer) for audio in stems.values()):
        raise ValidationError("Every source value must be an AudioBuffer")

    return (
        slice_audio(mix, section) if section is not None else mix,
        {key: slice_audio(audio, section) if section is not None else audio
         for key, audio in stems.items()},
    )


def _alignment_block(measured_mix: AudioBuffer, measured_stems: Mapping[str, AudioBuffer],
                     window: dict) -> dict:
    """Check the sample timeline, window and channel geometry of the renders."""
    problems: list[dict] = []
    for source_id, source in measured_stems.items():
        if source.sample_rate != measured_mix.sample_rate:
            problems.append({
                "reason": "sample_rate_mismatch", "source_ids": [source_id],
                "detail": {"mix_sample_rate": measured_mix.sample_rate,
                           "source_sample_rate": source.sample_rate},
            })
        if abs(source.start_s - measured_mix.start_s) > 1e-9:
            problems.append({
                "reason": "timeline_mismatch", "source_ids": [source_id],
                "detail": {"mix_start_s": measured_mix.start_s,
                           "source_start_s": source.start_s},
            })
        if source.frames != measured_mix.frames:
            problems.append({
                "reason": "frame_count_mismatch", "source_ids": [source_id],
                "detail": {"mix_frames": measured_mix.frames,
                           "source_frames": source.frames},
            })
        if source.channels != measured_mix.channels:
            problems.append({
                "reason": "channel_count_mismatch", "source_ids": [source_id],
                "detail": {"mix_channels": measured_mix.channels,
                           "source_channels": source.channels},
            })
    return {
        "status": "mismatch" if problems else "ok",
        "sample_rate": int(measured_mix.sample_rate),
        "channels": int(measured_mix.channels),
        "frames": int(measured_mix.frames),
        "timeline_start_s": float(measured_mix.start_s),
        "window": dict(window),
        "checks": ["same_sample_rate", "same_timeline_start", "same_frame_count",
                   "same_channel_count"],
        "problems": problems,
    }


def _declaration_block(mix_labels: Mapping[str, str | None],
                       measured_stems: Mapping[str, AudioBuffer],
                       source_provenance: Mapping[str, Any] | None,
                       window: dict, sample_rate: int, mix_source: str) -> dict:
    """Check that every render declares the node/stage/channel view it came from."""
    problems: list[dict] = []
    missing_mix = [field for field in DECLARATION_FIELDS if mix_labels.get(field) is None]
    if missing_mix:
        problems.append({
            "reason": "missing_provenance", "source_ids": [],
            "detail": {"scope": "declared_node_render", "missing_declarations": missing_mix},
        })

    declared_sources: dict[str, dict] = {}
    missing_sources: list[dict] = []
    stage_mismatch: list[dict] = []
    view_mismatch: list[dict] = []
    node_mismatch: list[dict] = []
    window_mismatch: list[dict] = []
    seen_nodes: dict[str, str] = {}
    tolerance_s = 0.5 / sample_rate

    for source_id in measured_stems:
        entry = (source_provenance or {}).get(source_id)
        if not isinstance(entry, Mapping):
            missing_sources.append({"source_id": source_id,
                                    "missing_declarations": list(DECLARATION_FIELDS)})
            continue
        labels = {field: _label(entry.get(field)) for field in DECLARATION_FIELDS}
        declared_window = _declared_window(entry)
        if entry.get("window") is not None and declared_window is None:
            missing_sources.append({"source_id": source_id,
                                    "missing_declarations": ["window"]})
            continue
        absent = [field for field in DECLARATION_FIELDS if labels[field] is None]
        if absent:
            missing_sources.append({"source_id": source_id,
                                    "missing_declarations": absent})
            continue
        declared_sources[source_id] = {
            "node": labels["node"], "stage": labels["stage"],
            "channel_view": labels["channel_view"], "window": declared_window,
        }
        if mix_labels.get("stage") is not None and labels["stage"] != mix_labels["stage"]:
            stage_mismatch.append({"source_id": source_id, "source_stage": labels["stage"],
                                   "declared_stage": mix_labels["stage"]})
        if (mix_labels.get("channel_view") is not None
                and labels["channel_view"] != mix_labels["channel_view"]):
            view_mismatch.append({"source_id": source_id,
                                  "source_channel_view": labels["channel_view"],
                                  "declared_channel_view": mix_labels["channel_view"]})
        node = labels["node"]
        if mix_labels.get("node") is not None and node == mix_labels["node"]:
            node_mismatch.append({"source_id": source_id, "node": node,
                                  "detail": "same node as the declared mix render"})
        elif node in seen_nodes:
            node_mismatch.append({"source_id": source_id, "node": node,
                                  "detail": f"node already declared by {seen_nodes[node]}"})
        else:
            seen_nodes[node] = source_id
        if declared_window is not None:
            if (abs(declared_window["start_s"] - window["start_s"]) > tolerance_s
                    or abs(declared_window["end_s"] - window["end_s"]) > tolerance_s):
                window_mismatch.append({
                    "source_id": source_id, "declared_window": declared_window,
                    "measured_window": dict(window),
                })

    if missing_sources:
        problems.append({
            "reason": "missing_provenance",
            "source_ids": [item["source_id"] for item in missing_sources],
            "detail": {"scope": "source_renders", "sources": missing_sources},
        })
    if stage_mismatch:
        problems.append({"reason": "stage_mismatch",
                         "source_ids": [item["source_id"] for item in stage_mismatch],
                         "detail": {"sources": stage_mismatch}})
    if view_mismatch:
        problems.append({"reason": "channel_view_mismatch",
                         "source_ids": [item["source_id"] for item in view_mismatch],
                         "detail": {"sources": view_mismatch}})
    if node_mismatch:
        problems.append({"reason": "node_mismatch",
                         "source_ids": [item["source_id"] for item in node_mismatch],
                         "detail": {"sources": node_mismatch}})
    if window_mismatch:
        problems.append({"reason": "window_mismatch",
                         "source_ids": [item["source_id"] for item in window_mismatch],
                         "detail": {"sources": window_mismatch}})

    return {
        "mix_source": mix_source,
        "node": mix_labels.get("node"),
        "stage": mix_labels.get("stage"),
        "channel_view": mix_labels.get("channel_view"),
        "window": dict(window),
        "sources": declared_sources,
        "problems": problems,
    }


def _band_error_measurement(reconstructed_levels: np.ndarray, mix_levels: np.ndarray,
                            centers: np.ndarray) -> dict:
    """Absolute per-band error between a reconstruction and the declared render."""
    reconstructed_power = 10.0 ** (reconstructed_levels / 10.0)
    mix_power = 10.0 ** (mix_levels / 10.0)
    ratio_db = 10.0 * np.log10(
        (reconstructed_power + EPSILON_POWER) / (mix_power + EPSILON_POWER)
    )
    evaluated = ~((reconstructed_levels <= UNMEASURED_LEVEL_DB)
                  & (mix_levels <= UNMEASURED_LEVEL_DB))
    absolute = np.abs(ratio_db[evaluated]) if np.any(evaluated) else np.zeros(1)
    per_band: dict[str, float] = {}
    for index, center in enumerate(centers):
        column = evaluated[:, index]
        if not np.any(column):
            continue
        per_band[f"{float(center):g}"] = float(np.median(np.abs(ratio_db[column, index])))
    return {
        "median_abs_db": float(np.median(absolute)),
        "p95_abs_db": float(np.percentile(absolute, 95)),
        "max_abs_db": float(np.max(absolute)),
        "cells_evaluated": int(absolute.size),
        "frames": int(mix_levels.shape[0]),
        "bands": int(mix_levels.shape[1]),
        "per_band_median_abs_db": per_band,
    }


def _power_additivity_measurement(source_levels: list[np.ndarray],
                                  mix_levels: np.ndarray) -> dict:
    """How far incoherent source powers depart from the node's band powers.

    A positive error means the sources add to more power than the node holds,
    which is what phase cancellation or a node render taken after nonlinear
    processing looks like. It is reported, never thresholded: the model's
    subtraction assumes the powers add, and this is the size of that assumption.
    """
    total_source_power = np.zeros_like(mix_levels)
    for levels in source_levels:
        total_source_power = total_source_power + 10.0 ** (levels / 10.0)
    mix_power = 10.0 ** (mix_levels / 10.0)
    ratio_db = 10.0 * np.log10(
        (total_source_power + EPSILON_POWER) / (mix_power + EPSILON_POWER)
    )
    evaluated = mix_levels > UNMEASURED_LEVEL_DB
    absolute = np.abs(ratio_db[evaluated]) if np.any(evaluated) else np.zeros(1)
    signed_median = float(np.median(ratio_db[evaluated])) if np.any(evaluated) else 0.0
    return {
        "median_abs_db": float(np.median(absolute)),
        "p95_abs_db": float(np.percentile(absolute, 95)),
        "positive_median_db": signed_median,
        "note": ("Difference between the summed source band powers and the node's band "
                 "powers. The subtraction arithmetic assumes this is near zero; it is "
                 "reported, not thresholded."),
    }


def _gate_block(*, status: str, reasons: list[str], alignment: dict, declaration: dict,
                tolerances: dict, result: dict | None) -> dict:
    return {
        "method": GATE_METHOD,
        "method_version": GATE_METHOD_VERSION,
        "status": status,
        "reasons": reasons,
        "alignment": alignment,
        "declaration": declaration,
        "tolerances_db": tolerances,
        "result": result,
    }


def _unavailable_payload(*, reason: str, reasons: list[str], gate: dict,
                         model: str, calibration_level_db_spl: float, hop_s: float,
                         measured_mix: AudioBuffer | None,
                         window: dict, source_ids: list[str]) -> dict:
    return {
        "schema_version": PARTIAL_LOUDNESS_SCHEMA_VERSION,
        "status": "unavailable",
        "reason": reason,
        "reasons": reasons,
        "method": "leave_one_source_out_band_power_difference",
        "model": model,
        "calibration_level_db_spl": float(calibration_level_db_spl),
        "sample_rate": None if measured_mix is None else measured_mix.sample_rate,
        "hop_s": hop_s,
        "duration_s": None if measured_mix is None else measured_mix.duration_s,
        "timeline_start_s": None if measured_mix is None else measured_mix.start_s,
        "node": gate["declaration"]["node"],
        "stage": gate["declaration"]["stage"],
        "channel_view": gate["declaration"]["channel_view"],
        "window": dict(window),
        "n_sources": 0,
        "equal_share_baseline_pct": None,
        "gate": gate,
        "sources": {},
        "limitations": [
            "Per-source values are withheld because the reconstruction gate did not pass.",
            "No share, ratio, audibility label, too-quiet or exposed classification is "
            "emitted, and no substitute estimate is produced.",
        ],
        "coverage": {"sources": 0, "gate": gate["status"], "shares_emitted": False,
                     "source_ids": list(source_ids)},
    }


def _section_summary(
    *,
    start_index: int,
    stop_index: int,
    times: np.ndarray,
    mix_sones: np.ndarray,
    rest_sones: np.ndarray,
    solo_sones: np.ndarray,
    mix_specific: np.ndarray,
    rest_specific: np.ndarray,
    solo_specific: np.ndarray,
    masks: dict[str, np.ndarray],
) -> dict:
    selection = slice(start_index, stop_index)
    partial = np.maximum(mix_sones[selection] - rest_sones[selection], 0.0)
    solo = solo_sones[selection]
    row = {
        "start_s": float(times[start_index]),
        "end_s": float(times[stop_index - 1]),
        "partial_sones": float(np.mean(partial)),
        "solo_sones": float(np.mean(solo)),
        "partial_to_solo_ratio": float(np.clip(np.mean(partial) /
                                               max(float(np.mean(solo)), 1e-9), 0.0, 1.0)),
        "regions": {},
    }
    for name, mask in masks.items():
        if not np.any(mask):
            continue
        solo_region = solo_specific[selection][:, mask].sum(axis=1)
        partial_region = np.maximum(
            mix_specific[selection][:, mask].sum(axis=1)
            - rest_specific[selection][:, mask].sum(axis=1), 0.0
        )
        row["regions"][name] = {
            "partial_to_solo_ratio": float(np.clip(
                np.mean(partial_region) / max(float(np.mean(solo_region)), 1e-9), 0.0, 1.0
            )),
            "partial_sones": float(np.mean(partial_region)),
            "solo_sones": float(np.mean(solo_region)),
        }
    return row


def attribute_partial_loudness(
    mix: AudioBuffer,
    stems: Mapping[str, AudioBuffer],
    *,
    section: Section | None = None,
    model: str = "iso532-2",
    calibration_level_db_spl: float = 90.0,
    hop_s: float = 0.2,
    sensitivity_step_db: float = 1.0,
    section_s: float | None = None,
    node: str | None = None,
    stage: str | None = None,
    channel_view: str | None = None,
    source_provenance: Mapping[str, Mapping[str, Any]] | None = None,
    max_median_error_db: float = DEFAULT_MAX_MEDIAN_ERROR_DB,
    max_p95_error_db: float = DEFAULT_MAX_P95_ERROR_DB,
) -> dict:
    """Attribute modeled partial loudness only after a reconstruction gate.

    Before any per-source value is derived, the supplied source renders are
    summed and compared with the declared node render band by band over the
    measured window. The gate passes only when every declaration is present and
    consistent (``node``, ``stage``, ``channel_view`` for the mix and for every
    source; distinct source nodes; the declared window) and the absolute per-band
    error is at most ``max_median_error_db`` at the median and
    ``max_p95_error_db`` at p95.

    ``node``/``stage``/``channel_view`` describe the render being attributed;
    ``source_provenance`` maps each source ID to its own ``node``, ``stage``,
    ``channel_view`` and optional ``window`` (``{"start_s", "end_s"}``). Missing
    or inconsistent provenance, a different sample timeline/frame count/channel
    count, or a failed reconstruction returns ``status="unavailable"`` with the
    gate's inputs and measured residual; ``sources`` is then empty.

    Passing the gate admits the model's raw arithmetic only. Shares are relative
    to the summed modelled partial loudness of the supplied sources, are not
    audibility, and carry no classification.
    """
    if model not in {"iso532-1", "iso532-2"}:
        raise ValidationError("model must be 'iso532-1' or 'iso532-2'")
    if not np.isfinite(calibration_level_db_spl):
        raise ValidationError("calibration_level_db_spl must be finite")
    if not np.isfinite(hop_s) or hop_s <= 0:
        raise ValidationError("hop_s must be finite and positive")
    if not np.isfinite(sensitivity_step_db) or sensitivity_step_db <= 0:
        raise ValidationError("sensitivity_step_db must be finite and positive")
    if section_s is not None and (not np.isfinite(section_s) or section_s <= 0):
        raise ValidationError("section_s must be finite and positive")
    if not np.isfinite(max_median_error_db) or max_median_error_db <= 0:
        raise ValidationError("max_median_error_db must be finite and positive")
    if not np.isfinite(max_p95_error_db) or max_p95_error_db <= 0:
        raise ValidationError("max_p95_error_db must be finite and positive")
    if source_provenance is not None and not isinstance(source_provenance, Mapping):
        raise ValidationError("source_provenance must map source IDs to declarations")
    if isinstance(source_provenance, Mapping):
        unknown = set(source_provenance) - set(stems)
        if unknown:
            raise ValidationError("source_provenance must only name supplied sources",
                                  details={"unknown_source_ids": sorted(unknown)})

    measured_mix, measured_stems = _validate_buffers(mix, stems, section)
    fs = measured_mix.sample_rate
    window = {"start_s": float(measured_mix.start_s),
              "end_s": float(measured_mix.start_s + measured_mix.duration_s)}
    tolerances = {"median_abs": float(max_median_error_db),
                  "p95_abs": float(max_p95_error_db), "unit": "dB"}
    mix_labels = {"node": _label(node), "stage": _label(stage),
                  "channel_view": _label(channel_view)}
    alignment = _alignment_block(measured_mix, measured_stems, window)
    declaration = _declaration_block(mix_labels, measured_stems, source_provenance,
                                     window, fs, measured_mix.source)
    source_ids = sorted(measured_stems)

    declaration_reasons = [problem["reason"] for problem in declaration["problems"]]
    if alignment["status"] != "ok":
        reasons = [problem["reason"] for problem in alignment["problems"]]
        reasons += [reason for reason in declaration_reasons if reason not in reasons]
        gate = _gate_block(status="not_evaluated", reasons=reasons, alignment=alignment,
                           declaration=declaration, tolerances=tolerances, result=None)
        return _unavailable_payload(
            reason=reasons[0], reasons=reasons, gate=gate, model=model,
            calibration_level_db_spl=calibration_level_db_spl, hop_s=hop_s,
            measured_mix=measured_mix, window=window, source_ids=source_ids,
        )

    # Band levels on the model's own decomposition, so the gate guards exactly
    # the inputs the subtraction will use. The reconstruction is summed in the
    # time domain: correlated and anti-correlated renders sum exactly, which is
    # what makes this a reconstruction test rather than a power test.
    mix_levels_raw, times = _source_levels(measured_mix.samples, fs, hop_s, model)
    summed_samples = np.zeros_like(measured_mix.samples)
    for source in measured_stems.values():
        summed_samples = summed_samples + source.samples
    summed_levels_raw, summed_times = _source_levels(summed_samples, fs, hop_s, model)
    if (len(summed_levels_raw) != len(mix_levels_raw)
            or not np.allclose(summed_times, times)):
        reasons = ["frame_count_mismatch"]
        gate = _gate_block(status="not_evaluated", reasons=reasons, alignment=alignment,
                           declaration=declaration, tolerances=tolerances, result=None)
        return _unavailable_payload(
            reason=reasons[0], reasons=reasons, gate=gate, model=model,
            calibration_level_db_spl=calibration_level_db_spl, hop_s=hop_s,
            measured_mix=measured_mix, window=window, source_ids=source_ids,
        )

    source_levels_raw: dict[str, np.ndarray] = {}
    for source_id, source in measured_stems.items():
        levels, level_times = _source_levels(source.samples, fs, hop_s, model)
        if len(levels) != len(mix_levels_raw) or not np.allclose(level_times, times):
            raise ValidationError("Mix and source analysis frames do not align",
                                  details={"source_id": source_id})
        source_levels_raw[source_id] = levels

    centers = _band_centers(model)
    reconstruction = _band_error_measurement(summed_levels_raw, mix_levels_raw, centers)
    reconstruction["incoherent_power_sum"] = _power_additivity_measurement(
        list(source_levels_raw.values()), mix_levels_raw
    )

    gate_reasons: list[str] = list(declaration_reasons)
    if (reconstruction["median_abs_db"] > max_median_error_db
            or reconstruction["p95_abs_db"] > max_p95_error_db):
        gate_reasons.append("reconstruction_gate_failed")
    if gate_reasons:
        gate = _gate_block(status="fail", reasons=gate_reasons, alignment=alignment,
                           declaration=declaration, tolerances=tolerances,
                           result=reconstruction)
        return _unavailable_payload(
            reason=gate_reasons[0], reasons=gate_reasons, gate=gate, model=model,
            calibration_level_db_spl=calibration_level_db_spl, hop_s=hop_s,
            measured_mix=measured_mix, window=window, source_ids=source_ids,
        )

    total_db = 10.0 * np.log10(np.maximum(
        np.sum(10.0 ** (mix_levels_raw / 10.0), axis=1, keepdims=True), 1e-30
    ))
    offset_db = float(calibration_level_db_spl - np.median(total_db))
    mix_levels = mix_levels_raw + offset_db
    mix_power = 10.0 ** (mix_levels / 10.0)
    mix_sones, mix_specific, model_centers = _model_series(mix_levels, model)
    region_masks = _region_masks(model_centers, model)
    by_source: dict[str, dict] = {}

    for source_id, source in measured_stems.items():
        source_levels = source_levels_raw[source_id] + offset_db
        source_power = 10.0 ** (source_levels / 10.0)
        source_sones, source_specific, _ = _model_series(source_levels, model)
        rest_power = np.maximum(mix_power - source_power, 10.0 ** (DB_FLOOR / 10.0))
        rest_levels = 10.0 * np.log10(np.maximum(rest_power, 1e-30))
        rest_sones, rest_specific, _ = _model_series(rest_levels, model)
        partial_sones = np.maximum(mix_sones - rest_sones, 0.0)
        plus_power = mix_power + source_power * (10.0 ** (sensitivity_step_db / 10.0) - 1.0)
        plus_sones, _plus_specific, _ = _model_series(
            10.0 * np.log10(np.maximum(plus_power, 1e-30)), model
        )
        mean_partial = float(np.mean(partial_sones))
        mean_solo = float(np.mean(source_sones))

        regions: dict[str, dict] = {}
        for region_name, mask in region_masks.items():
            if not np.any(mask):
                continue
            solo_region = source_specific[:, mask].sum(axis=1)
            partial_region = np.maximum(
                mix_specific[:, mask].sum(axis=1) - rest_specific[:, mask].sum(axis=1), 0.0
            )
            regions[region_name] = {
                "partial_to_solo_ratio": float(np.clip(
                    np.mean(partial_region) / max(float(np.mean(solo_region)), 1e-9),
                    0.0, 1.0,
                )),
                "solo_sones": float(np.mean(solo_region)),
                "partial_sones": float(np.mean(partial_region)),
                "time_series": {
                    "source_solo_sones": solo_region.tolist(),
                    "source_partial_sones": partial_region.tolist(),
                    "partial_to_solo_ratio": np.clip(
                        partial_region / np.maximum(solo_region, 1e-9), 0.0, 1.0
                    ).tolist(),
                },
            }

        frame_ratio = np.clip(partial_sones / np.maximum(source_sones, 1e-9), 0.0, 1.0)
        times_abs = times + measured_mix.start_s
        source_report = {
            "solo_sones": mean_solo,
            "partial_sones": mean_partial,
            "partial_to_solo_ratio": float(np.clip(mean_partial / max(mean_solo, 1e-9),
                                                   0.0, 1.0)),
            "partial_to_solo_ratio_p10": float(np.percentile(frame_ratio, 10)),
            "sensitivity_sones_per_db": float(
                np.mean(plus_sones - mix_sones) / sensitivity_step_db
            ),
            "share_of_partial_loudness_pct": 0.0,
            "regions": regions,
            "time_series": {
                "time_s": times_abs.tolist(),
                "mix_sones": mix_sones.tolist(),
                "source_solo_sones": source_sones.tolist(),
                "source_partial_sones": partial_sones.tolist(),
                "partial_to_solo_ratio": frame_ratio.tolist(),
            },
        }
        if section_s is not None:
            frames_per_section = max(1, int(round(section_s / hop_s)))
            sections = []
            for start in range(0, len(times), frames_per_section):
                stop = min(len(times), start + frames_per_section)
                if stop - start < 2:
                    continue
                row = _section_summary(
                    start_index=start, stop_index=stop,
                    times=times_abs, mix_sones=mix_sones, rest_sones=rest_sones,
                    solo_sones=source_sones, mix_specific=mix_specific,
                    rest_specific=rest_specific, solo_specific=source_specific,
                    masks=region_masks,
                )
                sections.append(row)
            source_report["sections"] = sections
        by_source[source_id] = source_report

    total_partial = sum(item["partial_sones"] for item in by_source.values())
    for item in by_source.values():
        item["share_of_partial_loudness_pct"] = (
            100.0 * item["partial_sones"] / total_partial if total_partial > 1e-12 else 0.0
        )

    gate = _gate_block(status="pass", reasons=[], alignment=alignment,
                       declaration=declaration, tolerances=tolerances,
                       result=reconstruction)
    return {
        "schema_version": PARTIAL_LOUDNESS_SCHEMA_VERSION,
        "status": "available",
        "reason": None,
        "reasons": [],
        "model": model,
        "method": "leave_one_source_out_band_power_difference",
        "calibration_level_db_spl": float(calibration_level_db_spl),
        "sample_rate": fs,
        "hop_s": hop_s,
        "duration_s": measured_mix.duration_s,
        "timeline_start_s": measured_mix.start_s,
        "node": gate["declaration"]["node"],
        "stage": gate["declaration"]["stage"],
        "channel_view": gate["declaration"]["channel_view"],
        "window": dict(window),
        "n_sources": len(by_source),
        "equal_share_baseline_pct": (100.0 / len(by_source)) if by_source else None,
        "share_denominator": "sum_of_source_partial_sones",
        "gate": gate,
        "sources": by_source,
        "limitations": [
            "Shares are relative to the summed modelled partial loudness of the supplied "
            "sources, not to the mix, and are not audibility.",
            "Leave-one-source-out band-power subtraction assumes source band powers add "
            "incoherently; gate.result.incoherent_power_sum reports the measured departure.",
            "No threshold, too-quiet/exposed classification or quality verdict is applied.",
            "Reverb/delay returns and master nonlinearity are represented only if the "
            "declared renders contain them at the declared stage.",
            "equal_share_baseline_pct is 100/n for description only; it is not a gate.",
        ],
        "coverage": {"sources": len(by_source), "gate": gate["status"],
                     "shares_emitted": True, "source_ids": sorted(by_source)},
    }
