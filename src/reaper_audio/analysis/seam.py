"""Measure a seam, not guess it.

A cut is only half the edit. The other half is the overlap: how far the left
slice is extended, how much of the right slice is used, and how long the
crossfade is. Those proportions depend on the note, so this module reports
measurable seam defects (level dip, click/step, spectral jump, tail loss) and
lets the caller choose the variant. It does not pick a plugin, a ratio or a
"correct" extension.
"""
from __future__ import annotations

import numpy as np


def rms_db(samples: np.ndarray, floor_db: float = -120.0) -> float:
    if samples.size == 0:
        return floor_db
    value = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    if value <= 0.0:
        return floor_db
    return max(floor_db, 20.0 * float(np.log10(value)))


def crossfade(left: np.ndarray, right: np.ndarray,
              crossfade_s: float, sample_rate: int,
              curve: str = "linear") -> np.ndarray:
    """Overlap the end of ``left`` with the start of ``right``.

    ``linear`` is correct when both sides come from the same continuous take
    (a split guitar): the waveform is correlated, so a linear fade preserves
    level. ``equal_power`` is for uncorrelated material; on a correlated split
    it can cancel or bump the level.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if curve not in {"linear", "equal_power"}:
        raise ValueError("curve must be linear or equal_power")
    count = min(len(left), len(right), int(round(crossfade_s * sample_rate)))
    if count <= 0:
        return np.concatenate([left, right]).astype(np.float32, copy=False)
    phase = np.linspace(0.0, 1.0, count, dtype=np.float64)
    if curve == "linear":
        blend = (left[-count:].astype(np.float64) * (1.0 - phase) +
                 right[:count].astype(np.float64) * phase)
    else:
        angle = phase * (np.pi / 2.0)
        blend = (left[-count:].astype(np.float64) * np.cos(angle) +
                 right[:count].astype(np.float64) * np.sin(angle))
    return np.concatenate([
        left[:-count].astype(np.float32, copy=False),
        blend.astype(np.float32),
        right[count:].astype(np.float32, copy=False),
    ])


def equal_power_crossfade(left: np.ndarray, right: np.ndarray,
                          crossfade_s: float, sample_rate: int) -> np.ndarray:
    return crossfade(left, right, crossfade_s, sample_rate, curve="equal_power")


def seam_metrics(before: np.ndarray, after: np.ndarray,
                 sample_rate: int) -> dict:
    """Describe the defect at a seam using a short window on each side."""
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    before = np.asarray(before, dtype=np.float64).reshape(-1)
    after = np.asarray(after, dtype=np.float64).reshape(-1)
    if before.size < 2 or after.size < 2:
        raise ValueError("need at least two samples on each side")
    level_before = rms_db(before)
    level_after = rms_db(after)
    step = float(np.max(np.abs(np.diff(np.concatenate([before[-64:], after[:64]])))))
    window = max(16, min(before.size, after.size, int(0.05 * sample_rate)))
    spectrum_before = np.abs(np.fft.rfft(before[-window:]))
    spectrum_after = np.abs(np.fft.rfft(after[:window]))
    denom = float(np.linalg.norm(spectrum_before) * np.linalg.norm(spectrum_after))
    flux = 1.0 - (float(np.dot(spectrum_before, spectrum_after)) / denom
                   if denom > 0 else 0.0)
    return {
        "level_before_dbfs": round(level_before, 2),
        "level_after_dbfs": round(level_after, 2),
        "level_delta_db": round(level_after - level_before, 2),
        "max_sample_step": round(step, 6),
        "spectral_flux": round(max(0.0, min(2.0, flux)), 4),
        "tail_energy_ratio_db": round(level_after - level_before, 2),
        "interpretation": ("Large negative level_delta_db means a gap/dip; "
                           "large max_sample_step means a click; high "
                           "spectral_flux means a timbre jump. These are "
                           "measurements, not a verdict."),
    }
