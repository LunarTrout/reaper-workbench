"""Transient-anchored alignment between two takes.

The offset is estimated around detected attacks, not on the whole file: a
global cross-correlation is dominated by sustained material and by level, while
an attack is exactly where a human sees and hears the misalignment.

Convention: a positive ``lag_samples`` means the target is *late* relative to
the reference; shifting the target by ``-lag_samples`` aligns it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import correlate, find_peaks

from ..analysis.transients import detect_transients
from ..models import AudioBuffer


@dataclass(frozen=True)
class Offset:
    time_s: float
    lag_samples: int
    lag_ms: float
    correlation: float


@dataclass(frozen=True)
class Alignment:
    offsets: tuple[Offset, ...]
    median_lag: int
    mad_samples: float
    median_correlation: float

    @property
    def lag_ms(self) -> float:
        return 0.0 if not self.offsets else float(np.median([o.lag_ms for o in self.offsets]))

    def to_dict(self) -> dict:
        return {
            "median_lag_samples": self.median_lag,
            "median_lag_ms": self.lag_ms,
            "mad_samples": self.mad_samples,
            "median_correlation": self.median_correlation,
            "offsets": [{"time_s": o.time_s, "lag_samples": o.lag_samples,
                         "lag_ms": o.lag_ms, "correlation": o.correlation}
                        for o in self.offsets],
        }


def _mono(audio: AudioBuffer) -> np.ndarray:
    samples = audio.samples
    return samples.mean(axis=1) if samples.ndim == 2 and samples.shape[1] > 1 else samples[:, 0]


def estimate_alignment(reference: AudioBuffer, target: AudioBuffer, *,
                       search_ms: float = 50.0, pre_s: float = 0.02,
                       post_s: float = 0.12, max_onsets: int = 400) -> Alignment:
    """Estimate per-attack offsets and their robust aggregate."""
    if reference.sample_rate != target.sample_rate:
        raise ValueError("both takes must share a sample rate")
    sr = reference.sample_rate
    ref = _mono(reference)
    tgt = _mono(target)
    events = detect_transients(reference).get("events", [])
    times = [float(e["time_s"]) for e in events][:max_onsets]
    pre, post = int(pre_s * sr), int(post_s * sr)
    search = max(1, int(search_ms * sr / 1000.0))
    offsets: list[Offset] = []
    for time_s in times:
        a = max(0, int(time_s * sr) - pre)
        b = min(ref.shape[0], int(time_s * sr) + post)
        segment = ref[a:b]
        if segment.shape[0] < 16:
            continue
        ta = max(0, a - search)
        tb = min(tgt.shape[0], b + search)
        window = tgt[ta:tb]
        if window.shape[0] < segment.shape[0]:
            continue
        numerator = correlate(window, segment, mode="valid", method="fft")
        energy = np.convolve(window ** 2, np.ones(segment.shape[0]), mode="valid")
        denominator = np.sqrt(np.maximum(energy, 1e-20)) * np.sqrt(np.sum(segment ** 2))
        normalized = numerator / np.maximum(denominator, 1e-20)
        index = int(np.argmax(normalized))
        offsets.append(Offset(time_s=time_s, lag_samples=int(ta + index - a),
                              lag_ms=float((ta + index - a) / sr * 1000.0),
                              correlation=float(normalized[index])))
    if not offsets:
        return Alignment(offsets=(), median_lag=0, mad_samples=0.0, median_correlation=0.0)
    lags = np.array([o.lag_samples for o in offsets])
    median = int(np.median(lags))
    mad = float(np.median(np.abs(lags - median)))
    corr = float(np.median([o.correlation for o in offsets]))
    return Alignment(offsets=tuple(offsets), median_lag=median, mad_samples=mad,
                     median_correlation=corr)


def apply_offset(audio: AudioBuffer, lag_samples: int) -> AudioBuffer:
    """Shift a take by ``-lag_samples`` so that it aligns with the reference."""
    shift = -int(lag_samples)
    samples = np.roll(audio.samples, shift, axis=0)
    if shift > 0:
        samples[:shift] = 0.0
    elif shift < 0:
        samples[shift:] = 0.0
    return AudioBuffer(samples=samples, sample_rate=audio.sample_rate)


def correlation(a: AudioBuffer, b: AudioBuffer) -> float:
    x, y = _mono(a), _mono(b)
    n = min(x.shape[0], y.shape[0])
    if n == 0:
        return 0.0
    x, y = x[:n], y[:n]
    denom = float(np.sqrt(np.sum(x ** 2) * np.sum(y ** 2)))
    return float(np.sum(x * y) / denom) if denom > 0 else 0.0


def alignment_report(reference: AudioBuffer, target: AudioBuffer, **kwargs) -> dict:
    """Estimate, apply, and report the correlation before and after."""
    result = estimate_alignment(reference, target, **kwargs)
    before = correlation(reference, target)
    aligned = apply_offset(target, result.median_lag)
    after = correlation(reference, aligned)
    report = result.to_dict()
    report.update({"correlation_before": before, "correlation_after": after,
                   "improved": after >= before})
    return report
