"""Search seam proportions against the actual source audio.

The musical question is not "cut at the beat"; it is "how much of the left
tail and the right head do I keep, and over what overlap". Those proportions
change per note. This module renders candidate overlaps in memory, measures
them, and returns ranked evidence. The agent chooses the variant and applies
it through REAPER operations.
"""
from __future__ import annotations

from itertools import product

import numpy as np

from .seam import seam_metrics
from .tail import estimate_tail


def _metrics_at_boundary(audio: np.ndarray, position: int,
                         sample_rate: int) -> dict:
    window = max(2, int(round(0.05 * sample_rate)))
    before = audio[max(0, position - window):position]
    after = audio[position:min(len(audio), position + window)]
    channels = audio.shape[1]
    if channels == 1:
        return seam_metrics(before[:, 0], after[:, 0], sample_rate)

    per_channel = [seam_metrics(before[:, index], after[:, index], sample_rate)
                   for index in range(channels)]
    around = audio[max(0, position - window):
                   min(len(audio), position + window)]
    left_channel, right_channel = around[:, 0], around[:, 1]
    denominator = float(np.linalg.norm(left_channel) *
                        np.linalg.norm(right_channel))
    correlation = (float(np.dot(left_channel, right_channel)) / denominator
                   if denominator > 0 else 0.0)
    return {
        "level_before_dbfs": round(float(np.mean(
            [item["level_before_dbfs"] for item in per_channel])), 2),
        "level_after_dbfs": round(float(np.mean(
            [item["level_after_dbfs"] for item in per_channel])), 2),
        "level_delta_db": round(float(np.mean(
            [item["level_delta_db"] for item in per_channel])), 2),
        "max_sample_step": round(max(item["max_sample_step"]
                                      for item in per_channel), 6),
        "spectral_flux": round(max(item["spectral_flux"]
                                    for item in per_channel), 4),
        "tail_energy_ratio_db": round(float(np.mean(
            [item["tail_energy_ratio_db"] for item in per_channel])), 2),
        "phase_correlation": round(correlation, 4),
        "channels": per_channel,
    }


def _aggregate_boundary_metrics(start: dict, end: dict) -> dict:
    """Keep flat metrics while ranking by the worse overlap edge."""
    level_delta = max((start["level_delta_db"], end["level_delta_db"]),
                      key=abs)
    metrics = dict(start)
    metrics.update({
        "level_delta_db": level_delta,
        "max_sample_step": max(start["max_sample_step"],
                                end["max_sample_step"]),
        "spectral_flux": max(start["spectral_flux"], end["spectral_flux"]),
        "tail_energy_ratio_db": level_delta,
        "boundaries": {"overlap_start": start, "overlap_end": end},
    })
    if "channels" in end:
        metrics["end_channels"] = end["channels"]
    return metrics


def assemble_variant(source: np.ndarray, cut_s: float, sample_rate: int, *,
                     left_extend_s: float, move_s: float = 0.0,
                     right_trim_s: float = 0.0,
                     curve: str = "linear") -> dict:
    """Assemble one candidate with real item positions.

    - left item: source up to ``cut + left_extend``;
    - right item: starts at ``cut + move + right_trim`` and plays source from
      ``cut`` (moving an item does not change its take offset);
    - overlap = ``left_extend - move - right_trim``; the crossfade lasts the
      whole overlap, so the two copies do not sum above unity.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if left_extend_s < 0 or right_trim_s < 0:
        raise ValueError("extensions must be non-negative")
    if curve not in {"linear", "equal_power"}:
        raise ValueError("curve must be linear or equal_power")
    cut = int(round(cut_s * sample_rate))
    left_end = cut + int(round(left_extend_s * sample_rate))
    right_start = (cut + int(round(move_s * sample_rate)) +
                   int(round(right_trim_s * sample_rate)))
    if cut < 0 or left_end > len(source):
        raise ValueError("left extension extends beyond the source")
    if right_start < 0:
        raise ValueError("right item starts before zero")
    overlap = left_end - right_start
    if overlap <= 0:
        raise ValueError("right item starts after the left item ends (gap)")
    samples = np.asarray(source, dtype=np.float32)
    mono_input = samples.ndim == 1
    if mono_input:
        samples = samples[:, None]
    if samples.ndim != 2:
        raise ValueError("source must be mono or (samples, channels)")
    if overlap > len(samples) - cut:
        raise ValueError("overlap exceeds available right-side source audio")
    channels = samples.shape[1]
    total = max(left_end, right_start + len(samples) - cut)
    assembled = np.zeros((total, channels), dtype=np.float32)

    fade = np.linspace(0.0, 1.0, overlap, dtype=np.float32)[:, None]
    left = samples[:left_end].astype(np.float32, copy=True)
    if curve == "linear":
        left[-overlap:] *= (1.0 - fade)
    else:
        left[-overlap:] *= np.cos(fade * (np.pi / 2.0)).astype(np.float32)
    assembled[:left_end] += left

    right_copy = samples[cut:].astype(np.float32, copy=True)
    if curve == "linear":
        right_copy[:overlap] *= fade
    else:
        right_copy[:overlap] *= np.sin(fade * (np.pi / 2.0)).astype(np.float32)
    assembled[right_start:right_start + len(right_copy)] += right_copy

    start_metrics = _metrics_at_boundary(assembled, right_start, sample_rate)
    end_metrics = _metrics_at_boundary(assembled, left_end, sample_rate)
    metrics = _aggregate_boundary_metrics(start_metrics, end_metrics)
    return {
        "left_extend_s": round(left_extend_s, 4),
        "move_s": round(move_s, 4),
        "right_trim_s": round(right_trim_s, 4),
        "overlap_s": round(overlap / sample_rate, 4),
        "seam_position_s": round(right_start / sample_rate, 4),
        "crossfade_end_s": round(left_end / sample_rate, 4),
        "curve": curve,
        "metrics": metrics,
        "audio": assembled[:, 0] if mono_input else assembled,
    }


def search_variants(source: np.ndarray, cut_s: float, sample_rate: int, *,
                    max_left_extend_s: float = 0.3,
                    max_move_s: float = 0.2,
                    max_right_trim_s: float = 0.1,
                    steps: int = 5,
                    weights: dict | None = None,
                    curve: str = "linear") -> dict:
    """Rank assembled candidates by measured defects (lower is better)."""
    if steps < 2:
        raise ValueError("steps must be at least 2")
    weights = {"level": 1.0, "step": 1.0, "flux": 1.0} | (weights or {})
    left_grid = np.linspace(0.01, max_left_extend_s, steps)
    move_grid = np.linspace(-max_move_s, max_move_s, steps)
    trim_grid = np.linspace(0.0, max_right_trim_s, steps)
    rows = []
    for left_extend, move, right_trim in product(
            left_grid, move_grid, trim_grid):
        try:
            variant = assemble_variant(
                source, cut_s, sample_rate,
                left_extend_s=float(left_extend), move_s=float(move),
                right_trim_s=float(right_trim), curve=curve)
        except ValueError:
            continue
        metrics = variant["metrics"]
        score = (weights["level"] * abs(metrics["level_delta_db"]) / 6.0 +
                 weights["step"] * metrics["max_sample_step"] / 0.1 +
                 weights["flux"] * metrics["spectral_flux"])
        rows.append({key: value for key, value in variant.items()
                     if key != "audio"} | {"score": round(float(score), 4)})
    rows.sort(key=lambda row: row["score"])
    return {
        "cut_s": cut_s,
        "curve": curve,
        "weights": weights,
        "variants": rows,
        "best": rows[0] if rows else None,
        "decision": ("Measured candidates only. Choose the acceptable seam "
                     "for the musical goal; no variant is applied here."),
    }


def search_with_tail(source: np.ndarray, onset_s: float, sample_rate: int, *,
                     drop_db: float = 20.0,
                     max_tail_s: float = 1.0,
                     max_left_extend_cap_s: float = 0.5,
                     steps: int = 5,
                     weights: dict | None = None,
                     curve: str = "linear") -> dict:
    """Measure the real tail at this onset and bound the seam search by it.

    A 150 ms detector duration is not a tail. The caller still chooses the
    acceptable variant; this only stops the search from extending a short
    drum hit for a second, or from truncating a long guitar ring.
    """
    tail = estimate_tail(source, onset_s, sample_rate, drop_db=drop_db,
                         max_tail_s=max_tail_s)
    max_left = min(float(max_left_extend_cap_s),
                   max(0.02, float(tail["tail_s"])))
    search = search_variants(
        source, onset_s, sample_rate,
        max_left_extend_s=max_left, steps=steps,
        weights=weights, curve=curve)
    search["tail_evidence"] = tail
    search["max_left_extend_s"] = round(max_left, 4)
    return search
