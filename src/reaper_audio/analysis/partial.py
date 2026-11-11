"""Per-source partial loudness and audibility evidence for aligned mixes."""

from __future__ import annotations

from collections.abc import Mapping
from math import gcd

import numpy as np
from scipy.signal import butter, resample_poly, sosfilt

from ..errors import ValidationError
from ..models import AudioBuffer, Section
from .audio import slice_audio
from .perceptual import (BARK_CENTERS, BARK_REGIONS, third_octave_levels,
                         zwicker_series)

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


def _validate_sources(mix: AudioBuffer, stems: Mapping[str, AudioBuffer],
                      section: Section | None) -> tuple[AudioBuffer, dict[str, AudioBuffer]]:
    if not isinstance(mix, AudioBuffer):
        raise ValidationError("mix must be an AudioBuffer")
    if not isinstance(stems, Mapping) or not stems:
        raise ValidationError("stems must map source IDs to AudioBuffer values")
    if any(not isinstance(key, str) or not key.strip() for key in stems):
        raise ValidationError("Every source needs a non-empty string ID")
    if any(not isinstance(audio, AudioBuffer) for audio in stems.values()):
        raise ValidationError("Every source value must be an AudioBuffer")

    measured_mix = slice_audio(mix, section) if section is not None else mix
    measured_stems = {
        key: slice_audio(audio, section) if section is not None else audio
        for key, audio in stems.items()
    }
    for key, source in measured_stems.items():
        if source.sample_rate != measured_mix.sample_rate:
            raise ValidationError("Mix and source sample rates must match before attribution",
                                  details={"source_id": key,
                                           "mix_sample_rate": measured_mix.sample_rate,
                                           "source_sample_rate": source.sample_rate})
        if abs(source.start_s - measured_mix.start_s) > 1e-9:
            raise ValidationError("Mix and source timelines must have the same start time",
                                  details={"source_id": key,
                                           "mix_start_s": measured_mix.start_s,
                                           "source_start_s": source.start_s})
        if source.frames != measured_mix.frames:
            raise ValidationError("Mix and source frame counts must match for attribution",
                                  details={"source_id": key,
                                           "mix_frames": measured_mix.frames,
                                           "source_frames": source.frames})
    return measured_mix, measured_stems


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
        "relative_audibility": float(np.clip(np.mean(partial) /
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
            "relative_audibility": float(np.clip(
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
) -> dict:
    """Estimate each aligned source's contribution to modeled mix loudness.

    Source and mix buffers must share a sample timeline and duration. The
    leave-one-out calculation subtracts band powers, as in the source model;
    it is an estimate and does not prove perceptual masking or account for
    nonlinear bus/master processing.
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

    measured_mix, measured_stems = _validate_sources(mix, stems, section)
    fs = measured_mix.sample_rate
    samples = measured_mix.samples

    if model == "iso532-2":
        mix_levels_raw, times = _bands29(samples, fs, hop_s)
        total_db = 10.0 * np.log10(np.maximum(
            np.sum(10.0 ** (mix_levels_raw / 10.0), axis=1, keepdims=True), 1e-30
        ))
        offset_db = float(calibration_level_db_spl - np.median(total_db))
        mix_levels = mix_levels_raw + offset_db
    else:
        mix_levels_raw, times = third_octave_levels(samples, fs, hop_s=hop_s)
        total_db = 10.0 * np.log10(np.maximum(
            np.sum(10.0 ** (mix_levels_raw / 10.0), axis=1, keepdims=True), 1e-30
        ))
        offset_db = float(calibration_level_db_spl - np.median(total_db))
        mix_levels = mix_levels_raw + offset_db

    mix_power = 10.0 ** (mix_levels / 10.0)
    mix_sones, mix_specific, centers = _model_series(mix_levels, model)
    region_masks = _region_masks(centers, model)
    by_source: dict[str, dict] = {}

    for source_id, source in measured_stems.items():
        if model == "iso532-2":
            source_levels_raw, source_times = _bands29(source.samples, fs, hop_s)
            source_levels = source_levels_raw + offset_db
        else:
            source_levels_raw, source_times = third_octave_levels(
                source.samples, fs, hop_s=hop_s
            )
            source_levels = source_levels_raw + offset_db
        if len(source_levels) != len(mix_levels) or not np.allclose(source_times, times):
            raise ValidationError("Mix and source analysis frames do not align",
                                  details={"source_id": source_id})

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
                "relative_audibility": float(np.clip(
                    np.mean(partial_region) / max(float(np.mean(solo_region)), 1e-9),
                    0.0, 1.0,
                )),
                "solo_sones": float(np.mean(solo_region)),
                "partial_sones": float(np.mean(partial_region)),
                "time_series": {
                    "source_solo_sones": solo_region.tolist(),
                    "source_partial_sones": partial_region.tolist(),
                    "relative_audibility": np.clip(
                        partial_region / np.maximum(solo_region, 1e-9), 0.0, 1.0
                    ).tolist(),
                },
            }

        frame_audibility = np.clip(partial_sones / np.maximum(source_sones, 1e-9), 0.0, 1.0)
        times_abs = times + measured_mix.start_s
        source_report = {
            "solo_sones": mean_solo,
            "partial_sones": mean_partial,
            "relative_audibility": float(np.clip(mean_partial / max(mean_solo, 1e-9),
                                                 0.0, 1.0)),
            "relative_audibility_p10": float(np.percentile(frame_audibility, 10)),
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
                "relative_audibility": frame_audibility.tolist(),
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

    return {
        "model": model,
        "method": "leave_one_source_out_band_power_difference",
        "calibration_level_db_spl": float(calibration_level_db_spl),
        "sample_rate": fs,
        "hop_s": hop_s,
        "duration_s": measured_mix.duration_s,
        "timeline_start_s": measured_mix.start_s,
        "sources": by_source,
        "limitations": [
            "Requires mix and sources to be aligned to the same timeline and processing stage.",
            "Leave-one-out band-power subtraction approximates masking and may be invalid for nonlinear bus/master processing.",
            "A bus and its child tracks should not both be interpreted as independent additive sources.",
        ],
        "coverage": {"sources": len(by_source), "model": model,
                     "source_alignment": "validated_equal_timeline_and_frames"},
    }
