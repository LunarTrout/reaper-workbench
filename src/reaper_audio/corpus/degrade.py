"""Controlled degradations used to test the corpus model.

Each degradation is a *known* production change with a known direction.  The
benchmark asks a simple question: does the model place the original closer to
a corpus of other tracks than its degraded copy?  Parameters here are the
design of the experiment, not constants of the method.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
from scipy.signal import butter, sosfilt, sosfiltfilt


def _as_stereo(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 1:
        x = np.stack([x, x], axis=1)
    return x


def _rbj_peaking(sr: int, fc: float, gain_db: float, q: float = 1.0):
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * fc / sr
    alpha = np.sin(w0) / (2 * q)
    b = np.array([1 + alpha * A, -2 * np.cos(w0), 1 - alpha * A])
    a = np.array([1 + alpha / A, -2 * np.cos(w0), 1 - alpha / A])
    return b / a[0], a / a[0]


def _apply_biquad(x: np.ndarray, b: np.ndarray, a: np.ndarray) -> np.ndarray:
    from scipy.signal import lfilter
    return lfilter(b, a, x, axis=0)


def gain(x: np.ndarray, sr: int, db: float) -> tuple[np.ndarray, str]:
    return _as_stereo(x) * (10 ** (db / 20.0)), f"gain {db:+.1f} dB"


def tilt(x: np.ndarray, sr: int, db_per_oct: float) -> tuple[np.ndarray, str]:
    """Linear-phase spectral tilt around 1 kHz, applied in the FFT domain."""
    y = _as_stereo(x)
    n = y.shape[0]
    freqs = np.fft.rfftfreq(n, 1 / sr)
    with np.errstate(divide="ignore"):
        octaves = np.log2(np.maximum(freqs, 1.0) / 1000.0)
    shape = 10 ** (db_per_oct * octaves / 20.0)
    spec = np.fft.rfft(y, axis=0) * shape[:, None]
    return np.fft.irfft(spec, n=n, axis=0), f"tilt {db_per_oct:+.1f} dB/oct"


def peaking(x: np.ndarray, sr: int, fc: float, db: float, q: float = 1.0) -> tuple[np.ndarray, str]:
    b, a = _rbj_peaking(sr, fc, db, q)
    return _apply_biquad(_as_stereo(x), b, a), f"peak {fc:.0f} Hz {db:+.1f} dB"


def lowpass(x: np.ndarray, sr: int, fc: float, order: int = 8) -> tuple[np.ndarray, str]:
    sos = butter(order, fc / (sr / 2), btype="low", output="sos")
    return sosfiltfilt(sos, _as_stereo(x), axis=0), f"lowpass {fc:.0f} Hz"


def compress(x: np.ndarray, sr: int, ratio: float = 6.0, threshold_db: float = -24.0,
             attack_ms: float = 3.0, release_ms: float = 80.0) -> tuple[np.ndarray, str]:
    """Feed-forward RMS compressor with a fixed detector; a known production move."""
    y = _as_stereo(x)
    env = np.sqrt(np.mean(y ** 2, axis=1) + 1e-20)
    att = np.exp(-1.0 / (attack_ms * 1e-3 * sr))
    rel = np.exp(-1.0 / (release_ms * 1e-3 * sr))
    smoothed = np.empty_like(env)
    acc = env[0]
    for i, v in enumerate(env):
        coeff = att if v > acc else rel
        acc = coeff * acc + (1 - coeff) * v
        smoothed[i] = acc
    level_db = 20 * np.log10(smoothed + 1e-20)
    over = np.maximum(0.0, level_db - threshold_db)
    gain_db = -over * (1 - 1 / ratio)
    return y * (10 ** (gain_db / 20.0))[:, None], f"compress {ratio:.0f}:1 @ {threshold_db:.0f} dB"


def narrow(x: np.ndarray, sr: int, factor: float = 0.4) -> tuple[np.ndarray, str]:
    y = _as_stereo(x)
    mid = (y[:, 0] + y[:, 1]) / 2
    side = (y[:, 0] - y[:, 1]) / 2 * factor
    return np.stack([mid + side, mid - side], axis=1), f"narrow side x{factor:.2f}"


def mono(x: np.ndarray, sr: int, **_) -> tuple[np.ndarray, str]:
    y = _as_stereo(x)
    m = (y[:, 0] + y[:, 1]) / 2
    return np.stack([m, m], axis=1), "mono"


def clip(x: np.ndarray, sr: int, level: float = 0.5) -> tuple[np.ndarray, str]:
    return np.clip(_as_stereo(x), -level, level), f"clip at {level:.2f} FS"


@dataclass(frozen=True)
class Degradation:
    name: str
    fn: Callable[..., tuple[np.ndarray, str]]
    kwargs: dict
    expect: str  # "better" (original closer), "invariant" (level-only)


DEGRADATIONS: tuple[Degradation, ...] = (
    Degradation("gain", gain, {"db": -6.0}, "invariant"),
    Degradation("bright", tilt, {"db_per_oct": 3.0}, "better"),
    Degradation("dark", tilt, {"db_per_oct": -3.0}, "better"),
    Degradation("mud", peaking, {"fc": 250.0, "db": 5.0, "q": 0.9}, "better"),
    Degradation("presence_cut", peaking, {"fc": 3000.0, "db": -5.0, "q": 0.9}, "better"),
    Degradation("air_cut", lowpass, {"fc": 8000.0}, "better"),
    Degradation("compressed", compress, {}, "better"),
    Degradation("narrow", narrow, {"factor": 0.4}, "better"),
    Degradation("mono", mono, {}, "better"),
    Degradation("clipped", clip, {"level": 0.5}, "better"),
)


def degrade(name: str, x: np.ndarray, sr: int) -> tuple[np.ndarray, str]:
    for d in DEGRADATIONS:
        if d.name == name:
            return d.fn(x, sr, **d.kwargs)
    raise KeyError(name)
