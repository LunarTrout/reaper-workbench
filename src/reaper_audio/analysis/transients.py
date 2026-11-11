"""Broadband and band-limited onset evidence for percussion and pick attacks."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import find_peaks, stft

from ..models import AudioBuffer


@dataclass(frozen=True, slots=True)
class TransientEvent:
    time_s: float
    strength: float
    confidence: float
    band_strength: dict[str, float]

    def to_dict(self) -> dict:
        return {"time_s": self.time_s, "strength": self.strength,
                "confidence": self.confidence, "band_strength": self.band_strength}


def _band_energy_flux(magnitude: np.ndarray, frequencies: np.ndarray) -> dict[str, np.ndarray]:
    ranges = {"low": (20.0, 200.0), "mid": (200.0, 2000.0),
              "high": (2000.0, float(frequencies[-1]))}
    result: dict[str, np.ndarray] = {}
    log_mag = np.log1p(magnitude)
    for name, (lo, hi) in ranges.items():
        mask = (frequencies >= lo) & (frequencies < hi)
        if not np.any(mask):
            result[name] = np.zeros(max(0, magnitude.shape[1] - 1))
            continue
        delta = np.maximum(0.0, np.diff(log_mag[mask], axis=1))
        result[name] = delta.sum(axis=0)
    return result


def detect_transients(
    audio: AudioBuffer,
    *,
    frame_size: int = 2048,
    hop_size: int = 256,
    min_gap_s: float = 0.035,
    sensitivity: float = 2.8,
    max_events: int = 20_000,
) -> dict:
    """Detect spectral-change peaks; return candidates rather than asserted beats.

    Parameters are signal-analysis controls. They do not encode instrument,
    genre, tempo or a preferred grid.
    """
    if frame_size < 128 or hop_size < 1 or hop_size > frame_size:
        raise ValueError("frame_size/hop_size are outside valid bounds")
    if min_gap_s < 0 or sensitivity <= 0 or max_events < 1:
        raise ValueError("min_gap_s, sensitivity and max_events must be positive")

    per_channel_power = []
    frequencies = None
    times = None
    samples = audio.samples
    if len(samples) < frame_size:
        samples = np.pad(samples, ((0, frame_size - len(samples)), (0, 0)))
    for channel in range(audio.channels):
        frequencies, times, spectrum = stft(
            samples[:, channel], fs=audio.sample_rate, window="hann",
            nperseg=frame_size, noverlap=frame_size - hop_size,
            boundary="zeros", padded=True,
        )
        per_channel_power.append(np.abs(spectrum) ** 2)
    # Average power, rather than averaging waveforms, so anti-phase stereo
    # cannot erase the very attacks this detector is meant to find.
    magnitude = np.sqrt(np.mean(np.stack(per_channel_power, axis=0), axis=0))
    assert frequencies is not None and times is not None
    flux_by_band = _band_energy_flux(magnitude, frequencies)
    if not flux_by_band or not len(next(iter(flux_by_band.values()))):
        return {"events": [], "event_count": 0, "density_per_s": 0.0,
                "hop_s": hop_size / audio.sample_rate, "coverage": "empty"}

    broadband = np.sum(np.stack(list(flux_by_band.values())), axis=0)
    smooth = np.convolve(broadband, np.ones(3) / 3.0, mode="same")
    baseline = float(np.median(smooth))
    mad = float(np.median(np.abs(smooth - baseline)))
    robust_sigma = max(1.4826 * mad, float(np.percentile(smooth, 90)) * 0.01, 1e-12)
    threshold = baseline + sensitivity * robust_sigma
    distance = max(1, round(min_gap_s * audio.sample_rate / hop_size))
    candidates, props = find_peaks(smooth, height=threshold, distance=distance,
                                   prominence=max(robust_sigma, 1e-12))
    if len(candidates) > max_events:
        order = np.argsort(smooth[candidates])[-max_events:]
        candidates = np.sort(candidates[order])

    high_reference = max(float(np.percentile(smooth, 99.5)), threshold + 1e-12)
    denom = max(high_reference - threshold, 1e-12)
    events: list[dict] = []
    for idx in candidates:
        strength = float(smooth[idx])
        confidence = float(np.clip((strength - threshold) / denom, 0.0, 1.0))
        event_time = float(times[min(idx + 1, len(times) - 1)])
        bands = {name: float(values[idx]) for name, values in flux_by_band.items()}
        events.append(TransientEvent(event_time + audio.start_s, strength,
                                     confidence, bands).to_dict())
    duration = max(audio.duration_s, 1e-12)
    return {
        "events": events,
        "event_count": len(events),
        "density_per_s": len(events) / duration,
        "threshold": threshold,
        "baseline": baseline,
        "robust_sigma": robust_sigma,
        "hop_s": hop_size / audio.sample_rate,
        "frame_size": frame_size,
        "min_gap_s": min_gap_s,
        "sensitivity": sensitivity,
        "sample_rate": audio.sample_rate,
        "channels_used": audio.channels,
        "coverage": "complete",
    }
