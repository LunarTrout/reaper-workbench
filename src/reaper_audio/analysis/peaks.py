"""Sample-peak and oversampled true-peak estimates."""

from __future__ import annotations

import numpy as np
from scipy.signal import firwin, resample_poly

DB_FLOOR = -160.0
DEFAULT_OVERSAMPLE = 4
DEFAULT_CHUNK_FRAMES = 65_536


def _dbfs(value: float) -> float | None:
    if not np.isfinite(value) or value < 0:
        raise ValueError("peak amplitude must be finite and non-negative")
    if value == 0:
        return None
    return float(max(DB_FLOOR, 20.0 * np.log10(value)))


def _channel_true_peak(channel: np.ndarray, oversample: int, taps: np.ndarray,
                       chunk_frames: int) -> float:
    if len(channel) <= 1:
        return float(np.max(np.abs(channel)))

    context = int(np.ceil(((len(taps) - 1) / 2) / oversample)) + 2
    padded = np.pad(channel, (context, context), mode="reflect")
    peak = 0.0
    for start in range(0, len(channel), chunk_frames):
        end = min(len(channel), start + chunk_frames)
        core_start = context + start
        core_end = context + end
        segment_start = max(0, core_start - context)
        segment_end = min(len(padded), core_end + context)
        interpolated = resample_poly(
            padded[segment_start:segment_end], oversample, 1, window=taps,
        )
        local_start = (core_start - segment_start) * oversample
        local_end = (core_end - segment_start) * oversample
        peak = max(peak, float(np.max(np.abs(interpolated[local_start:local_end]))))
    return peak


def estimate_true_peak(samples: np.ndarray, *, oversample: int = DEFAULT_OVERSAMPLE,
                       chunk_frames: int = DEFAULT_CHUNK_FRAMES) -> dict:
    """Estimate inter-sample peak using chunked polyphase FIR interpolation.

    This is an engineering estimate, not a standards-compliance declaration.
    Validate it against the current ITU/EBU true-peak test signals before using
    it for delivery compliance decisions.
    """
    x = np.asarray(samples, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    if x.ndim != 2 or not x.shape[0] or not x.shape[1]:
        raise ValueError("samples must be a non-empty frames-by-channels array")
    if not np.isfinite(x).all():
        raise ValueError("samples must contain only finite values")
    if isinstance(oversample, bool) or not isinstance(oversample, int) or oversample < 2:
        raise ValueError("oversample must be an integer >= 2")
    if isinstance(chunk_frames, bool) or not isinstance(chunk_frames, int) or chunk_frames < 1:
        raise ValueError("chunk_frames must be a positive integer")

    taps = firwin(32 * oversample + 1, 1.0 / oversample,
                  window=("kaiser", 8.6))
    per_channel_sample = np.max(np.abs(x), axis=0)
    per_channel_true = np.asarray([
        _channel_true_peak(x[:, channel], oversample, taps, chunk_frames)
        for channel in range(x.shape[1])
    ], dtype=np.float64)
    sample_peak = float(np.max(per_channel_sample))
    true_peak = float(np.max(per_channel_true))
    return {
        "sample_peak_linear": sample_peak,
        "sample_peak_dbfs": _dbfs(sample_peak),
        "true_peak_linear": true_peak,
        "true_peak_dbfs_estimate": _dbfs(true_peak),
        "per_channel_sample_peak_linear": per_channel_sample.tolist(),
        "per_channel_sample_peak_dbfs": [
            _dbfs(float(value)) for value in per_channel_sample
        ],
        "per_channel_true_peak_linear": per_channel_true.tolist(),
        "per_channel_true_peak_dbfs_estimate": [
            _dbfs(float(value)) for value in per_channel_true
        ],
        "oversample_factor": oversample,
        "filter": {"type": "polyphase FIR", "window": "Kaiser", "beta": 8.6,
                   "tap_count": len(taps)},
        "coverage": ("estimated; not validated for standards compliance; "
                     "validate against official test signals before compliance use"),
    }
