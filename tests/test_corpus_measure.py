"""Measurements must be level-invariant where they describe production, and
must follow the standard where they describe loudness."""

from __future__ import annotations

import numpy as np
import pytest

from reaper_audio.corpus import FEATURE_NAMES, measure_signal
from reaper_audio.corpus.measure import measure_file, windows


def _noise(seconds: float = 4.0, sr: int = 32000, channels: int = 2, seed: int = 0):
    rng = np.random.default_rng(seed)
    x = rng.normal(0.0, 0.1, size=(int(seconds * sr), channels))
    return x, sr


def test_feature_names_match_vector():
    x, sr = _noise()
    m = measure_signal(x, sr)
    assert m.values.shape == (len(FEATURE_NAMES),)
    assert m.names == FEATURE_NAMES


def test_gain_does_not_change_production_features():
    x, sr = _noise()
    a = measure_signal(x, sr).values
    b = measure_signal(x * 0.5, sr).values
    # CLR, tilt, side/mid, correlation, mono loss, per-band crest, onset density
    # are all defined to be independent of overall gain.
    assert np.allclose(a, b, atol=1e-6, equal_nan=True)


def test_stereo_widening_moves_side_mid_only():
    x, sr = _noise()
    width_before = measure_signal(x, sr).values[FEATURE_NAMES.index("side_mid")]
    mid = (x[:, 0] + x[:, 1]) / 2
    side = (x[:, 0] - x[:, 1]) / 2
    y = np.stack([mid + 2 * side, mid - 2 * side], axis=1)
    width_after = measure_signal(y, sr).values[FEATURE_NAMES.index("side_mid")]
    assert width_after > width_before + 5.0


def test_mono_signal_has_zero_width():
    x, sr = _noise(channels=1)
    m = measure_signal(x, sr).values
    assert m[FEATURE_NAMES.index("side_mid")] < -100.0


def test_integrated_loudness_of_anchor_signal_matches_standard():
    sr = 48000
    t = np.arange(sr, dtype=np.float64) / sr
    sine = np.sin(2 * np.pi * 997 * t)  # 0 dBFS peak
    # Two identical channels: the standard's 0 dBFS one-channel anchor
    # (-3.01 LKFS) plus the +3 dB channel sum.
    stereo = measure_signal(np.stack([sine, sine], axis=1), sr)
    assert stereo.extras["integrated_lufs"] == pytest.approx(0.0, abs=0.02)


def test_windows_cover_signal_without_partial_tail():
    x, sr = _noise(seconds=10.0)
    parts = list(windows(x, sr, 3.0))
    assert len(parts) == 3
    assert all(p.shape[0] == 3 * sr for p in parts)


def test_measure_file_reads_excerpt(tmp_path):
    import soundfile as sf
    x, sr = _noise(seconds=2.0, sr=44100)
    path = tmp_path / "excerpt.wav"
    sf.write(str(path), x, sr)
    m = measure_file(path, max_seconds=1.0)
    assert m.extras["duration_s"] == pytest.approx(1.0, abs=0.01)
