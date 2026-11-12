"""Transient-anchored alignment on synthetic takes with known offsets."""

from __future__ import annotations

import numpy as np

from reaper_audio.daw.align import (Alignment, apply_offset, alignment_report,
                                    correlation, estimate_alignment)
from reaper_audio.models import AudioBuffer

SR = 48000


def _clicks(delay: int = 0, noise: float = 1e-4, seed: int = 0) -> AudioBuffer:
    rng = np.random.default_rng(seed)
    n = int(2.0 * SR)
    x = np.zeros(n)
    t = np.arange(int(0.05 * SR)) / SR
    burst = np.exp(-t * 60) * np.sin(2 * np.pi * 900 * t)
    for start in (0.2, 0.5, 0.9, 1.4, 1.7):
        i = int(start * SR)
        x[i:i + burst.shape[0]] += burst[:min(burst.shape[0], n - i)]
    x += noise * rng.normal(size=n)
    y = np.roll(x, delay)
    if delay > 0:
        y[:delay] = 0.0
    elif delay < 0:
        y[delay:] = 0.0
    return AudioBuffer(samples=np.stack([y, y], axis=1), sample_rate=SR)


def test_offset_is_recovered_within_two_samples():
    reference = _clicks()
    target = _clicks(delay=37)
    result = estimate_alignment(reference, target, search_ms=10)
    assert abs(result.median_lag - 37) <= 2
    assert result.median_correlation > 0.8


def test_no_offset_is_zero():
    reference = _clicks()
    result = estimate_alignment(reference, _clicks(), search_ms=10)
    assert abs(result.median_lag) <= 1


def test_shift_aligns_and_improves_correlation():
    reference = _clicks()
    target = _clicks(delay=37)
    before = correlation(reference, target)
    aligned = apply_offset(target, 37)
    after = correlation(reference, aligned)
    assert after > before + 0.1
    residual = estimate_alignment(reference, aligned, search_ms=5)
    assert abs(residual.median_lag) <= 2


def test_unrelated_material_has_low_confidence():
    reference = _clicks()
    rng = np.random.default_rng(1)
    noise = AudioBuffer(samples=rng.normal(0, 0.1, (int(2.0 * SR), 2)), sample_rate=SR)
    result = estimate_alignment(reference, noise, search_ms=10)
    assert result.median_correlation < 0.6


def test_report_contains_before_and_after():
    report = alignment_report(_clicks(), _clicks(delay=12), search_ms=10)
    assert set(report) >= {"median_lag_samples", "correlation_before",
                           "correlation_after", "offsets"}
    assert report["correlation_after"] >= report["correlation_before"]
