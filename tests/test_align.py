"""Transient-anchored alignment on synthetic takes with known offsets."""

from __future__ import annotations

import numpy as np

import pytest

from reaper_audio.daw.align import (Alignment, align_group, apply_offset,
                                    alignment_report, correlation, estimate_alignment,
                                    plan_group_alignment)
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


def test_group_alignment_is_per_take():
    reference = _clicks()
    result = align_group(reference, {"late37": _clicks(delay=37),
                                     "late7": _clicks(delay=7)}, search_ms=10)
    assert abs(result["late37"].median_lag - 37) <= 2
    assert abs(result["late7"].median_lag - 7) <= 2


class _FakeSession:
    def __init__(self, positions):
        self._positions = positions

    def snapshot(self, **_):
        return {"tracks": [{"guid": "t1", "items": [
            {"guid": guid, "position_s": pos} for guid, pos in self._positions.items()]}]}

    def plan(self, operations, *, reason):
        self.operations = operations
        return operations


def test_group_plan_moves_each_item_by_minus_lag():
    session = _FakeSession({"a": 0.5, "b": 0.25})
    plan_group_alignment(session, moves={"a": 480, "b": 0}, sample_rate=48000,
                         reason="test")
    assert len(session.operations) == 1
    assert session.operations[0].target_guid == "a"
    assert session.operations[0].values["position_s"] == 0.49


def test_group_plan_rejects_moving_before_zero():
    import pytest
    session = _FakeSession({"a": 0.0})
    with pytest.raises(ValueError):
        plan_group_alignment(session, moves={"a": 4800}, sample_rate=48000, reason="test")


def _saturated(x: AudioBuffer, delay: int, seed: int = 2) -> AudioBuffer:
    """Same performance, different timbre and noise — a realistic double."""
    rng = np.random.default_rng(seed)
    y = np.tanh(x.samples * 3.0) * 0.5
    y = y + 0.002 * rng.normal(size=y.shape)
    y = np.roll(y, delay, axis=0)
    if delay > 0:
        y[:delay] = 0.0
    return AudioBuffer(samples=y, sample_rate=x.sample_rate)


def test_alignment_survives_a_different_timbre_and_noise():
    reference = _clicks()
    target = _saturated(reference, 64)
    result = estimate_alignment(reference, target, search_ms=10)
    assert abs(result.median_lag - 64) <= 3
    assert abs(result.slope_samples_per_s) < 5.0


def test_drift_is_reported_as_a_slope():
    reference = _clicks()
    samples = reference.samples.copy()
    rate = 20.0  # samples per second
    for start in range(0, samples.shape[0], int(0.25 * SR)):
        block = samples[start:start + int(0.25 * SR)].copy()
        shift = int(rate * (start / SR))
        samples[start:start + block.shape[0]] = np.roll(block, shift, axis=0)
    drifting = AudioBuffer(samples=samples, sample_rate=SR)
    result = estimate_alignment(reference, drifting, search_ms=40)
    assert result.slope_samples_per_s == pytest.approx(rate, rel=0.35)
    assert result.slope_stderr < abs(result.slope_samples_per_s)


def test_report_makes_no_decision_without_a_policy():
    report = alignment_report(_clicks(), _saturated(_clicks(), 64), search_ms=10)
    assert "action" not in report


def test_policy_decides_between_shift_segments_and_refusal():
    reference = _clicks()
    shifted = alignment_report(reference, _saturated(reference, 64), search_ms=10,
                               policy={"alignment.max_slope_sigma": 3.0,
                                       "alignment.min_correlation": 0.8})
    unrelated = alignment_report(reference, _clicks(delay=0, seed=99), search_ms=10,
                                 policy={"alignment.min_correlation": 0.8})
    assert shifted["action"] == "global_shift"
    assert unrelated["action"] in ("global_shift", "refuse")


def test_segment_offsets_show_where_the_take_drifts():
    from reaper_audio.daw.align import segment_offsets
    reference = _clicks()
    samples = reference.samples.copy()
    for start in range(0, samples.shape[0], int(0.5 * SR)):
        shift = int(10 * start / SR)
        block = samples[start:start + int(0.5 * SR)].copy()
        samples[start:start + block.shape[0]] = np.roll(block, shift, axis=0)
    drifting = AudioBuffer(samples=samples, sample_rate=SR)
    result = estimate_alignment(reference, drifting, search_ms=40)
    segments = segment_offsets(result, segment_s=0.5)
    medians = [s["median_lag_samples"] for s in segments]
    assert medians == sorted(medians)
    assert medians[-1] > medians[0]
    assert result.slope_samples_per_s > 0


def test_report_contains_before_and_after():
    report = alignment_report(_clicks(), _clicks(delay=12), search_ms=10)
    assert set(report) >= {"median_lag_samples", "correlation_before",
                           "correlation_after_global_shift", "offsets", "segments"}
    assert report["correlation_after_global_shift"] >= report["correlation_before"]
