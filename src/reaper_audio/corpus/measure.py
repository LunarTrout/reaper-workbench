"""Measurements for corpus statistics.

Every function here is a *measurement definition*: a standard (BS.1770-5 for
loudness, Annex 2 for true peak) or an explicit, level-invariant signal
description.  None of them is a quality score, and none carries a threshold.

The feature vector is intentionally compositional where it describes tone:
band levels are expressed as centred log-ratios (CLR) so that the vector cannot
be dominated by overall level and the covariance is not singular because the
parts sum to one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfiltfilt, stft

from phonometry.broadcast import program_loudness

#: Third-octave centres from 25 Hz to 16 kHz.  A measurement grid, not a tuning.
BAND_CENTERS = np.array(
    [25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500,
     630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000,
     10000, 12500, 16000],
    dtype=float,
)

#: Coarse bands used for per-band stereo and crest descriptions.
COARSE_BANDS = ((20.0, 200.0), (200.0, 2000.0), (2000.0, 16000.0))

_TILT_RANGE = (100.0, 10000.0)
_EPS = 1e-20


def _db(x: float) -> float:
    return float(10.0 * np.log10(max(float(x), _EPS)))


def _to_mono(x: np.ndarray) -> np.ndarray:
    return x.mean(axis=1) if x.ndim == 2 else x


def _band_power(freqs: np.ndarray, power: np.ndarray, lo: float, hi: float) -> float:
    mask = (freqs >= lo) & (freqs < hi)
    if not np.any(mask):
        return 0.0
    return float(power[mask].mean())


def _third_octave_power(x: np.ndarray, sr: int) -> np.ndarray:
    """Mean power per third-octave band, averaged over channels and time."""
    mono = _to_mono(x)
    n_fft = 8192 if sr >= 32000 else 4096
    freqs, _, z = stft(mono, fs=sr, window="hann", nperseg=n_fft,
                       noverlap=n_fft // 2, boundary="zeros", padded=True)
    power = np.mean(np.abs(z) ** 2, axis=1)
    out = np.empty(len(BAND_CENTERS))
    for i, fc in enumerate(BAND_CENTERS):
        lo, hi = fc / 2 ** (1 / 6), fc * 2 ** (1 / 6)
        out[i] = _band_power(freqs, power, lo, hi)
    return out


def _band_signal(x: np.ndarray, sr: int, lo: float, hi: float) -> np.ndarray:
    nyq = sr / 2.0
    hi = min(hi, nyq * 0.99)
    sos = butter(4, [lo / nyq, hi / nyq], btype="band", output="sos")
    return sosfiltfilt(sos, x, axis=0)


def _side_mid(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if x.ndim == 1:
        return np.zeros_like(x), x
    return (x[:, 0] - x[:, 1]) / 2.0, (x[:, 0] + x[:, 1]) / 2.0


def _band_crest(x: np.ndarray, sr: int, lo: float, hi: float) -> float:
    band = _band_signal(x, sr, lo, hi)
    peak = float(np.max(np.abs(band))) + _EPS
    rms = float(np.sqrt(np.mean(band ** 2))) + _EPS
    return float(20.0 * np.log10(peak / rms))


def _onset_density(x: np.ndarray, sr: int) -> float:
    """Spectral-flux onsets per second, threshold relative to the flux spread."""
    mono = _to_mono(x)
    n_fft = 2048 if sr >= 32000 else 1024
    hop = n_fft // 4
    _, _, z = stft(mono, fs=sr, window="hann", nperseg=n_fft, noverlap=n_fft - hop,
                   boundary="zeros", padded=True)
    mag = np.abs(z)
    flux = np.maximum(0.0, np.diff(mag, axis=1)).sum(axis=0)
    if flux.size < 4:
        return 0.0
    med = np.median(flux)
    mad = np.median(np.abs(flux - med)) + _EPS
    thresh = med + 3.0 * 1.4826 * mad
    peaks = (flux[1:-1] > flux[:-2]) & (flux[1:-1] >= flux[2:]) & (flux[1:-1] > thresh)
    seconds = x.shape[0] / sr
    return float(peaks.sum() / seconds) if seconds > 0 else 0.0


def _short_crest(x: np.ndarray, sr: int, *, frame_s: float = 0.1) -> tuple[float, float]:
    """Crest distribution over short frames: p50 and p95, in dB.

    A single crest value over ten seconds hides compression: the loud frames
    lose their peaks while the quiet ones keep theirs.  The distribution is the
    level-invariant description of that loss.
    """
    mono = _to_mono(x)
    hop = max(1, int(sr * frame_s))
    usable = (mono.shape[0] // hop) * hop
    if usable < hop:
        return (0.0, 0.0)
    frames = mono[:usable].reshape(-1, hop)
    peak = np.max(np.abs(frames), axis=1) + _EPS
    rms = np.sqrt(np.mean(frames ** 2, axis=1)) + _EPS
    crest = 20.0 * np.log10(peak / rms)
    return (float(np.percentile(crest, 50)), float(np.percentile(crest, 95)))


def _loudness(x: np.ndarray, sr: int):
    # phonometry follows the channel-first convention used by the standard.
    data = x.T if x.ndim == 2 else x
    return program_loudness(data, float(sr))


@dataclass(frozen=True)
class Measurement:
    values: np.ndarray
    names: tuple[str, ...]
    extras: dict


def feature_names() -> tuple[str, ...]:
    names: list[str] = []
    names += [f"clr_{int(c)}" for c in BAND_CENTERS]
    names += ["tilt", "side_mid", "correlation", "mono_loss_db"]
    for lo, hi in COARSE_BANDS:
        names += [f"sm_{int(lo)}_{int(hi)}", f"crest_{int(lo)}_{int(hi)}"]
    names += ["onset_density", "crest_short_p50", "crest_short_p95"]
    return tuple(names)


FEATURE_NAMES = feature_names()


def measure_signal(x: np.ndarray, sr: int) -> Measurement:
    """Measure one excerpt.  ``x`` is (samples, channels) or (samples,)."""
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    x = x[:, :2]

    band_power = _third_octave_power(x, sr)
    log_power = np.log(band_power + _EPS)
    clr = log_power - log_power.mean()

    freqs = BAND_CENTERS
    mask = (freqs >= _TILT_RANGE[0]) & (freqs <= _TILT_RANGE[1])
    tilt = float(np.polyfit(np.log2(freqs[mask]), 10.0 * log_power[mask] / np.log(10.0), 1)[0])

    side, mid = _side_mid(x)
    side_p = float(np.mean(side ** 2)) + _EPS
    mid_p = float(np.mean(mid ** 2)) + _EPS
    side_mid = float(10.0 * np.log10(side_p / mid_p))

    if x.shape[1] == 2 and np.std(x[:, 0]) > 0 and np.std(x[:, 1]) > 0:
        correlation = float(np.corrcoef(x[:, 0], x[:, 1])[0, 1])
    else:
        correlation = 1.0

    loud_stereo = _loudness(x, sr)
    loud_mono = _loudness(_to_mono(x), sr)
    # A silent stem has no loudness at all (-inf); the width loss is undefined,
    # not enormous.  Keep the feature finite and let the caller skip silence.
    if np.isfinite(loud_mono.integrated) and np.isfinite(loud_stereo.integrated):
        mono_loss = float(loud_mono.integrated - loud_stereo.integrated)
    else:
        mono_loss = 0.0

    coarse: list[float] = []
    for lo, hi in COARSE_BANDS:
        b = _band_signal(x, sr, lo, hi)
        s, m = _side_mid(b)
        coarse.append(float(10.0 * np.log10((np.mean(s ** 2) + _EPS) / (np.mean(m ** 2) + _EPS))))

    crests = [_band_crest(x, sr, lo, hi) for lo, hi in COARSE_BANDS]
    onset = _onset_density(x, sr)
    short = _short_crest(x, sr)

    values = np.concatenate([clr, [tilt, side_mid, correlation, mono_loss], coarse,
                             crests, [onset, short[0], short[1]]])
    extras = {
        "integrated_lufs": float(loud_stereo.integrated),
        "lra": float(getattr(loud_stereo, "loudness_range", float("nan"))),
        "true_peak_dbtp": float(getattr(loud_stereo, "true_peak", float("nan"))),
        "max_short_term": float(getattr(loud_stereo, "max_short_term", float("nan"))),
        "plr_db": float(getattr(loud_stereo, "true_peak", float("nan")) - loud_stereo.integrated),
        "psr_db": float(getattr(loud_stereo, "true_peak", float("nan"))
                        - getattr(loud_stereo, "max_short_term", float("nan"))),
        "duration_s": float(x.shape[0] / sr),
    }
    return Measurement(values=values.astype(np.float64), names=FEATURE_NAMES, extras=extras)


def measure_file(path: str | Path, *, max_seconds: float | None = None) -> Measurement:
    info = sf.info(str(path))
    frames = info.frames if max_seconds is None else min(info.frames, int(max_seconds * info.samplerate))
    x, sr = sf.read(str(path), frames=frames, dtype="float64", always_2d=True)
    return measure_signal(x, sr)


def windows(x: np.ndarray, sr: int, seconds: float) -> Iterable[np.ndarray]:
    """Split a signal into consecutive windows of ``seconds`` (last partial dropped)."""
    step = int(round(seconds * sr))
    if step <= 0:
        raise ValueError("seconds must be positive")
    for start in range(0, x.shape[0] - step + 1, step):
        yield x[start:start + step]
