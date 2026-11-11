"""Third-octave levels and Zwicker/ISO 532-1 perceptual loudness."""

from __future__ import annotations

import numpy as np

THIRD_OCTAVE_CENTERS_HZ = np.asarray([
    25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500,
    630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300,
    8000, 10000, 12500,
], dtype=np.float64)
BARK_CENTERS = np.arange(240, dtype=np.float64) * 0.1 + 0.05
BARK_REGIONS = (
    ("sub", 0.0, 2.0), ("bass", 2.0, 5.0), ("lowmid", 5.0, 9.0),
    ("mid", 9.0, 14.0), ("upmid", 14.0, 18.0), ("pres", 18.0, 21.0),
    ("air", 21.0, 24.0),
)


def _third_octave_matrix(sample_rate: int, n_fft: int) -> tuple[np.ndarray, np.ndarray]:
    frequencies = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)
    ratio = 10 ** (1.0 / 20.0)
    matrix = np.zeros((len(THIRD_OCTAVE_CENTERS_HZ), len(frequencies)), dtype=np.float64)
    for index, center in enumerate(THIRD_OCTAVE_CENTERS_HZ):
        if center >= sample_rate / 2:
            continue
        mask = ((frequencies >= center / ratio) & (frequencies < center * ratio))
        if not np.any(mask):
            mask[np.argmin(np.abs(frequencies - center))] = True
        matrix[index, mask] = 1.0
    return matrix, frequencies


def third_octave_levels(
    samples: np.ndarray,
    sample_rate: int,
    *,
    hop_s: float = 0.05,
    n_fft: int = 4096,
) -> tuple[np.ndarray, np.ndarray]:
    """Return time-local third-octave levels in dB SPL-equivalent units.

    PCM full scale is not an acoustic calibration. Callers should calibrate the
    resulting levels to a declared playback reference before comparing loudness.
    """
    x = np.asarray(samples, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    if x.ndim != 2 or x.shape[0] == 0 or x.shape[1] == 0:
        raise ValueError("samples must be a non-empty frames-by-channels array")
    if sample_rate <= 0 or hop_s <= 0 or n_fft < 256:
        raise ValueError("sample_rate/hop_s must be positive and n_fft >= 256")

    hop = max(1, int(round(hop_s * sample_rate)))
    frame_count = max(1, 1 + max(0, len(x) - n_fft) // hop)
    padded = np.pad(x, ((0, max(0, n_fft - len(x))), (0, 0)))
    starts = np.arange(frame_count) * hop
    indices = starts[:, None] + np.arange(n_fft)[None, :]
    frames = padded[indices]
    window = np.hanning(n_fft)
    window_correction = max((window.sum() / 2.0) ** 2, 1e-24)
    spectrum = np.fft.rfft(frames * window[None, :, None], axis=1)
    channel_power = np.sum(np.abs(spectrum) ** 2, axis=2) * 2.0 / window_correction
    matrix, _ = _third_octave_matrix(sample_rate, n_fft)
    band_power = channel_power @ matrix.T
    levels = 10.0 * np.log10(np.maximum(band_power, 1e-30) / (20e-6) ** 2)
    times = (starts + n_fft / 2) / sample_rate
    return levels, times


def calibrate_levels(levels_db_spl: np.ndarray, target_level_db_spl: float = 90.0) -> np.ndarray:
    """Shift all bands so each frame's total level reaches a chosen reference."""
    levels = np.asarray(levels_db_spl, dtype=np.float64)
    if levels.ndim != 2 or levels.shape[0] == 0:
        raise ValueError("levels_db_spl must be a non-empty frames-by-bands matrix")
    if not np.isfinite(target_level_db_spl):
        raise ValueError("target_level_db_spl must be finite")
    total = 10.0 * np.log10(np.maximum(np.sum(10.0 ** (levels / 10.0), axis=1,
                                                keepdims=True), 1e-30))
    return levels + (float(target_level_db_spl) - total)


def zwicker_series(levels_db_spl: np.ndarray) -> tuple[np.ndarray, np.ndarray,
                                                       np.ndarray, np.ndarray]:
    """Compute ISO 532-1 Zwicker sones, phons, specific loudness and sharpness."""
    from phonometry import psychoacoustics

    levels = np.atleast_2d(np.asarray(levels_db_spl, dtype=np.float64))
    if levels.ndim != 2 or levels.shape[1] != len(THIRD_OCTAVE_CENTERS_HZ):
        raise ValueError("Zwicker input must have 28 third-octave levels per frame")
    sones = np.empty(len(levels), dtype=np.float64)
    phons = np.empty(len(levels), dtype=np.float64)
    specific = np.empty((len(levels), len(BARK_CENTERS)), dtype=np.float64)
    sharpness = np.empty(len(levels), dtype=np.float64)
    for index, row in enumerate(levels):
        result = psychoacoustics.loudness_zwicker_from_spectrum(row)
        sones[index] = result.loudness
        phons[index] = result.loudness_level
        specific[index] = result.specific
        sharpness[index] = psychoacoustics.sharpness_din_from_specific(
            result.specific, method="din"
        )
    return sones, phons, specific, sharpness
