"""Estimate the real decay tail of an onset from audio.

The 2 ms event detector caps durations (often at 150 ms), which is not enough
for a sustained guitar. This measures how long the energy stays within a
caller-chosen drop below the onset peak. The caller chooses the drop (for
example 20 dB for a ring-out, 40 dB for silence); the result is evidence.
"""
from __future__ import annotations

import numpy as np


def _mono(source: np.ndarray) -> np.ndarray:
    samples = np.asarray(source, dtype=np.float64)
    if samples.ndim == 2:
        samples = samples.mean(axis=1)
    if samples.ndim != 1:
        raise ValueError("source must be mono or (samples, channels)")
    return samples


def _rms_db(window: np.ndarray, floor_db: float = -120.0) -> float:
    if window.size == 0:
        return floor_db
    value = float(np.sqrt(np.mean(np.square(window))))
    return max(floor_db, 20.0 * float(np.log10(value))) if value > 0 else floor_db


def estimate_tail(source: np.ndarray, onset_s: float, sample_rate: int, *,
                  drop_db: float = 20.0, max_tail_s: float = 2.0,
                  hop_s: float = 0.01) -> dict:
    """Return the time until the onset energy falls ``drop_db`` below peak."""
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if drop_db <= 0 or max_tail_s <= 0 or hop_s <= 0:
        raise ValueError("drop_db, max_tail_s and hop_s must be positive")
    samples = _mono(source)
    onset = int(round(onset_s * sample_rate))
    if onset < 0 or onset >= len(samples):
        raise ValueError("onset is outside the source")
    hop = max(1, int(round(hop_s * sample_rate)))
    max_windows = max(1, int(round(max_tail_s / hop_s)))
    levels = []
    for index in range(max_windows):
        start = onset + index * hop
        end = min(len(samples), start + hop)
        if start >= len(samples):
            break
        levels.append(_rms_db(samples[start:end]))
    if not levels:
        raise ValueError("no audio after the onset")
    peak_index = int(np.argmax(levels))
    peak = levels[peak_index]
    threshold = peak - float(drop_db)
    tail_windows = len(levels) - peak_index
    for index in range(peak_index, len(levels)):
        if levels[index] < threshold:
            # A later hit must not extend the tail: stop at the first
            # sustained drop below the threshold after the peak.
            if index + 1 >= len(levels) or levels[index + 1] < threshold:
                tail_windows = index - peak_index + 1
                break
    tail_s = min(max_tail_s, tail_windows * hop_s)
    return {
        "tail_s": round(float(tail_s), 4),
        "drop_db": float(drop_db),
        "peak_dbfs": round(float(peak), 2),
        "threshold_dbfs": round(float(threshold), 2),
        "capped": tail_s >= max_tail_s - 1e-9,
        "windows": len(levels),
    }
