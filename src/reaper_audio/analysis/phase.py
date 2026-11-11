"""Pairwise, time-aligned phase and cancellation evidence for audio sources."""

from __future__ import annotations

from math import ceil, floor, gcd, isfinite

import numpy as np
from scipy import signal

from ..errors import ValidationError
from ..models import AudioBuffer, Section
from .audio import slice_audio

_DB_FLOOR = -160.0
_BANDS_HZ = ((20.0, 200.0), (200.0, 2_000.0),
             (2_000.0, 8_000.0), (8_000.0, 20_000.0))


def _ratio_db(numerator: float, denominator: float) -> float | None:
    if denominator <= 0 or not isfinite(denominator):
        return None
    if numerator <= 0:
        return _DB_FLOOR
    return float(max(_DB_FLOOR, 10.0 * np.log10(numerator / denominator)))


def _resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return samples
    divisor = gcd(source_rate, target_rate)
    return signal.resample_poly(
        samples, target_rate // divisor, source_rate // divisor,
        axis=0, window=("kaiser", 8.6),
    )


def _aligned_overlap(a: AudioBuffer, b: AudioBuffer, sample_rate: int
                     ) -> tuple[np.ndarray, np.ndarray, float, str | None]:
    a_samples = _resample(a.samples, a.sample_rate, sample_rate)
    b_samples = _resample(b.samples, b.sample_rate, sample_rate)
    start_s = max(a.start_s, b.start_s)
    end_s = min(a.start_s + a.duration_s, b.start_s + b.duration_s)
    if end_s <= start_s:
        return a_samples[:0], b_samples[:0], start_s, "no_time_overlap"

    a_start_exact = (start_s - a.start_s) * sample_rate
    b_start_exact = (start_s - b.start_s) * sample_rate
    a_start, b_start = round(a_start_exact), round(b_start_exact)
    if (abs(a_start_exact - a_start) > 0.25
            or abs(b_start_exact - b_start) > 0.25):
        return a_samples[:0], b_samples[:0], start_s, "start_times_not_sample_aligned"

    frame_count = min(
        floor((end_s - start_s) * sample_rate + 1e-9),
        len(a_samples) - a_start,
        len(b_samples) - b_start,
    )
    if frame_count <= 0:
        return a_samples[:0], b_samples[:0], start_s, "no_sample_aligned_overlap"
    return (a_samples[a_start:a_start + frame_count],
            b_samples[b_start:b_start + frame_count], start_s, None)


def _lag_windows(a: np.ndarray, b: np.ndarray, sample_rate: int,
                 max_lag_ms: float) -> dict:
    """Estimate B's relative delay in overlapping, band-limited blocks."""
    decimation = max(1, ceil(sample_rate / 8_000))
    lag_rate = sample_rate / decimation
    left = signal.resample_poly(a, 1, decimation) if decimation > 1 else a
    right = signal.resample_poly(b, 1, decimation) if decimation > 1 else b
    max_lag = min(round(max_lag_ms * lag_rate / 1_000), max(0, len(left) // 4))
    frame_size = min(max(1, round(lag_rate)), len(left))
    hop = max(1, frame_size // 2)
    estimates: list[tuple[float, float]] = []

    for start in range(0, max(1, len(left) - frame_size + 1), hop):
        x = np.asarray(left[start:start + frame_size], dtype=np.float64)
        y = np.asarray(right[start:start + frame_size], dtype=np.float64)
        if len(x) < 32 or np.std(x) <= 1e-12 or np.std(y) <= 1e-12:
            continue
        x = x - np.mean(x)
        y = y - np.mean(y)
        corr = signal.correlate(y, x, mode="full", method="fft")
        lags = signal.correlation_lags(len(y), len(x), mode="full")
        keep = np.abs(lags) <= max_lag
        candidate_lags = lags[keep]
        candidate_corr = corr[keep]
        norm = float(np.sqrt(np.sum(x * x) * np.sum(y * y)))
        if norm <= 0:
            continue
        scores = candidate_corr / norm
        best = int(np.argmax(np.abs(scores)))
        estimates.append((float(candidate_lags[best] / lag_rate * 1_000),
                          float(np.clip(scores[best], -1.0, 1.0))))

    if not estimates:
        return {"median_ms": None, "p10_ms": None, "p90_ms": None,
                "median_abs_correlation": None, "windows_analyzed": 0,
                "lag_search_limit_ms": max_lag_ms,
                "lag_estimate_sample_rate_hz": lag_rate}
    lags_ms = np.asarray([item[0] for item in estimates])
    scores = np.asarray([item[1] for item in estimates])
    return {
        "median_ms": float(np.median(lags_ms)),
        "p10_ms": float(np.percentile(lags_ms, 10)),
        "p90_ms": float(np.percentile(lags_ms, 90)),
        "median_abs_correlation": float(np.median(np.abs(scores))),
        "windows_analyzed": len(estimates),
        "lag_search_limit_ms": max_lag_ms,
        "lag_estimate_sample_rate_hz": lag_rate,
        "sign_convention": "positive means source B is delayed relative to source A",
    }


def _channel_metrics(a: np.ndarray, b: np.ndarray, sample_rate: int,
                     max_lag_ms: float) -> dict:
    a_centered = a - np.mean(a)
    b_centered = b - np.mean(b)
    a_power = float(np.mean(a_centered ** 2))
    b_power = float(np.mean(b_centered ** 2))
    if a_power > 0 and b_power > 0:
        zero_lag_correlation: float | None = float(
            np.mean(a_centered * b_centered) / np.sqrt(a_power * b_power)
        )
        zero_lag_correlation = float(np.clip(zero_lag_correlation, -1.0, 1.0))
    else:
        zero_lag_correlation = None

    nperseg = min(2_048, 2 ** int(np.floor(np.log2(len(a)))))
    if nperseg < 32:
        bands = []
    else:
        noverlap = nperseg // 2
        kwargs = {"fs": sample_rate, "nperseg": nperseg,
                  "noverlap": noverlap, "detrend": "constant"}
        frequencies, pxx = signal.welch(a, **kwargs)
        _, pyy = signal.welch(b, **kwargs)
        _, pxy = signal.csd(a, b, **kwargs)
        coherence = np.divide(
            np.abs(pxy) ** 2, pxx * pyy,
            out=np.zeros_like(pxx), where=(pxx * pyy) > 0,
        )
        coherence = np.clip(coherence, 0.0, 1.0)
        nyquist = sample_rate / 2.0
        bands = []
        for low_hz, requested_high_hz in _BANDS_HZ:
            high_hz = min(requested_high_hz, nyquist)
            mask = ((frequencies >= low_hz) & (frequencies < high_hz)
                    if high_hz > low_hz else np.zeros_like(frequencies, dtype=bool))
            if np.count_nonzero(mask) < 2:
                bands.append({
                    "low_hz": low_hz, "high_hz": high_hz,
                    "status": "insufficient_frequency_bins",
                    "frequency_bins": int(np.count_nonzero(mask)),
                })
                continue
            cross = pxy[mask]
            cross_sum = np.sum(cross)
            denominator = float(np.sum(pxx[mask] + pyy[mask]))
            mix_power = float(np.sum(pxx[mask] + pyy[mask] + 2.0 * np.real(cross)))
            bands.append({
                "low_hz": low_hz, "high_hz": high_hz,
                "status": "available",
                "frequency_bins": int(np.count_nonzero(mask)),
                "mean_squared_coherence": float(np.mean(coherence[mask])),
                "mean_cross_spectral_phase_rad": (
                    float(np.angle(cross_sum))
                    if abs(cross_sum) > 1e-20 else None
                ),
                "power_change_vs_incoherent_sum_db": _ratio_db(mix_power, denominator),
            })

    return {
        "zero_lag_correlation": zero_lag_correlation,
        "power_change_vs_incoherent_sum_db": _ratio_db(
            float(np.mean((a + b - np.mean(a + b)) ** 2)), a_power + b_power
        ),
        "relative_delay": _lag_windows(a, b, sample_rate, max_lag_ms),
        "frequency_bands": bands,
    }


def compare_track_phase(
    a: AudioBuffer,
    b: AudioBuffer,
    *,
    max_lag_ms: float = 10.0,
    section: Section | None = None,
) -> dict:
    """Measure pairwise phase/cancellation evidence without changing either source.

    Time-domain correlation and summed-power change describe the actual aligned
    signals. Cross-spectral bands expose frequency-dependent relation. Delay is
    estimated independently in short, downsampled windows. None of these values
    decides whether a relationship is artistically intended or erroneous.
    """
    if not isinstance(a, AudioBuffer) or not isinstance(b, AudioBuffer):
        raise ValidationError("a and b must be AudioBuffer instances")
    if (isinstance(max_lag_ms, (bool, np.bool_))
            or not isinstance(max_lag_ms, (int, float, np.integer, np.floating))
            or not isfinite(float(max_lag_ms)) or max_lag_ms < 0):
        raise ValidationError("max_lag_ms must be finite and non-negative")
    max_lag_ms = float(max_lag_ms)
    if section is not None:
        a = slice_audio(a, section)
        b = slice_audio(b, section)

    sample_rate = min(a.sample_rate, b.sample_rate)
    left, right, start_s, alignment_error = _aligned_overlap(a, b, sample_rate)
    duration_s = len(left) / sample_rate
    base = {
        "source_a": a.source,
        "source_b": b.source,
        "time_range_s": {"start": start_s, "end": start_s + duration_s},
        "duration_s": duration_s,
        "sample_rate_hz": sample_rate,
        "resampled": {"a": a.sample_rate != sample_rate,
                      "b": b.sample_rate != sample_rate},
        "coverage": {"status": "unavailable", "reason": alignment_error},
        "channel_pairs": [],
        "interpretation": (
            "Signal evidence only. Correlation, coherence, delay, or fold-down "
            "change alone does not establish an audible or unwanted problem."
        ),
    }
    if alignment_error is not None:
        return base
    if duration_s < 0.01:
        base["coverage"]["reason"] = "overlap_too_short"
        return base

    if a.channels == b.channels:
        channel_pairs = [(index, index) for index in range(a.channels)]
    elif a.channels == 1:
        channel_pairs = [(0, index) for index in range(b.channels)]
    elif b.channels == 1:
        channel_pairs = [(index, 0) for index in range(a.channels)]
    else:
        base["coverage"]["reason"] = "ambiguous_channel_mapping"
        return base

    for channel_a, channel_b in channel_pairs:
        metrics = _channel_metrics(left[:, channel_a], right[:, channel_b],
                                   sample_rate, max_lag_ms)
        base["channel_pairs"].append({
            "channel_a": channel_a,
            "channel_b": channel_b,
            **metrics,
        })
    base["coverage"] = {
        "status": "available",
        "overlap_frames": len(left),
        "channel_pair_count": len(channel_pairs),
        "resampling_method": "polyphase Kaiser FIR to lower input sample rate",
    }
    return base
