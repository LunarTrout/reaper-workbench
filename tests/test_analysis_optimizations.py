from __future__ import annotations

import json

import numpy as np
import pyloudnorm as pyln

from reaper_audio.analysis import events, temporal
from reaper_audio.analysis.metrics import _fast_loudness_profile, measure_audio
from reaper_audio.models import AudioBuffer


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def test_quarter_decay_and_coherence_match_separate_helpers() -> None:
    rng = np.random.default_rng(7)
    left = rng.normal(0, 0.1, 4_800)
    right = rng.normal(0, 0.08, 4_800)
    expected_decay = temporal._band_decay(left, right, 48_000)
    expected_coherence = temporal._coherence(left, right, 48_000)
    actual_decay, actual_coherence = temporal._decay_and_coherence(
        left, right, 48_000)
    assert actual_decay == expected_decay
    assert actual_coherence == expected_coherence


def test_vectorized_envelope_matches_legacy_loop() -> None:
    rng = np.random.default_rng(11)
    samples = rng.normal(0, 0.2, (48_000, 2))
    times, peaks, rmss = events._envelope(samples, 48_000)
    hop = 96
    win = 192
    n = max(1, (len(samples) - win) // hop + 1)
    old_peaks = []
    old_rmss = []
    for i in range(n):
        segment = samples[i * hop:i * hop + win]
        old_peaks.append(float(np.max(np.abs(segment))))
        old_rmss.append(float(np.sqrt(np.mean(segment ** 2))))
    assert len(times) == n
    assert np.array_equal(peaks, np.asarray(old_peaks))
    assert np.array_equal(rmss, np.asarray(old_rmss))


def test_window_rms_matches_legacy_loop() -> None:
    rng = np.random.default_rng(19)
    x = rng.normal(0, 0.1, 20_000)
    actual = temporal._window_rms(x, 960, 240)
    expected = np.asarray([
        float(np.sqrt(np.mean(x[start:start + 960] ** 2)))
        for start in range(0, max(1, len(x) - 960), 240)
    ])
    assert np.array_equal(actual, expected)


def test_temporal_reuses_loudness_without_changing_report() -> None:
    import pyloudnorm as pyln

    sample_rate = 8_000
    t = np.arange(round(3.25 * sample_rate), dtype=np.float64) / sample_rate
    samples = np.column_stack((0.12 * np.sin(2 * np.pi * 220 * t),
                               0.09 * np.sin(2 * np.pi * 330 * t + 0.2)))
    meter = pyln.Meter(sample_rate)
    lufs = float(meter.integrated_loudness(samples))
    momentary = np.asarray(meter.blockwise_loudness, dtype=np.float64)
    lra = float(meter.loudness_range(samples))
    reused = temporal.analyze(
        samples, fs=sample_rate,
        loudness={"lufs": lufs, "lra": lra, "momentary": momentary})
    independent = temporal.analyze(samples, fs=sample_rate)
    assert _canonical(reused) == _canonical(independent)


def test_measure_audio_keeps_all_reused_intermediates_visible() -> None:
    sample_rate = 8_000
    t = np.arange(4 * sample_rate, dtype=np.float64) / sample_rate
    samples = np.column_stack((0.2 * np.sin(2 * np.pi * 100 * t),
                               0.15 * np.sin(2 * np.pi * 150 * t)))
    report = measure_audio(AudioBuffer(samples, sample_rate))
    profile = report.curves["temporal_profile"]
    for key in ("lufs", "lra", "loudness_coverage", "cells", "tempo",
                "events_2ms", "band_level_time_dbfs", "rms_400ms_dbfs",
                "spectrogram_time_s", "peak_estimate"):
        assert key in profile or key in report.curves
    assert "momentary_lufs" in profile["cells"][0]
    assert "short_term_lufs" in profile["cells"][0]
    assert report.metrics["integrated_lufs"].value is not None
    assert report.metrics["loudness_range_lu"].value is not None


def test_near_event_lookup_matches_linear_scan() -> None:
    rng = np.random.default_rng(23)
    times = np.sort(rng.uniform(0.0, 20.0, 200))
    candidates = rng.uniform(0.0, 20.0, 10_000)
    for candidate in candidates:
        expected = any(abs(candidate - value) < 0.05 for value in times)
        assert events._has_near_event(times.tolist(), float(candidate)) is expected


def test_linear_slope_matches_polyfit() -> None:
    rng = np.random.default_rng(29)
    times = np.arange(4, dtype=np.float64) * 0.023
    values = rng.normal(0, 2, 4)
    expected = float(np.polyfit(times, values, 1)[0])
    assert abs(temporal._linear_slope(times, values) - expected) < 1e-12


def _direct_short_term(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """pyloudnorm's direct 3 s blockwise timeline, the pre-optimisation baseline."""
    meter = pyln.Meter(sample_rate, block_size=3.0, overlap=0.97)
    meter.integrated_loudness(samples)
    return np.asarray(meter.blockwise_loudness, dtype=np.float64)


def _published(levels: np.ndarray) -> np.ndarray:
    """The profile's published convention: only non-finite values become sentinels."""
    return np.nan_to_num(levels, nan=-200.0, neginf=-200.0, posinf=0.0)


def _dense_zero_denormal_tail(sample_rate: int = 8_000,
                              duration_s: float = 90.0) -> np.ndarray:
    """Dense audio, an exact-zero gap, denormal-scale energy, a long silent tail.

    The denormal section drives the K-weighted squares into the subnormal range
    (``1e-320``), where a prefix-sum difference either underflows to zero or
    loses the value entirely, while pyloudnorm's direct block sum still returns
    a finite level near -3200 LUFS.
    """
    t = np.arange(int(round(duration_s * sample_rate)), dtype=np.float64) / sample_rate
    samples = np.zeros((len(t), 2), dtype=np.float64)
    dense = t < 10.0
    denormal = (t >= 25.0) & (t < 30.0)
    samples[dense, 0] = 0.08 * np.sin(2 * np.pi * 220 * t[dense])
    samples[dense, 1] = 0.05 * np.sin(2 * np.pi * 330 * t[dense] + 0.1)
    samples[denormal, 0] = 1e-160 * np.sin(2 * np.pi * 220 * t[denormal])
    samples[denormal, 1] = 1e-160 * np.cos(2 * np.pi * 180 * t[denormal])
    return samples


def _k_weighted(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    meter = pyln.Meter(sample_rate)
    filtered = np.array(samples, dtype=np.float64, copy=True)
    for stage in meter._filters.values():
        for channel in range(filtered.shape[1]):
            filtered[:, channel] = stage.apply_filter(filtered[:, channel])
    return filtered


def test_short_term_timeline_matches_direct_blockwise_reference() -> None:
    """The whole 3 s timeline must equal pyloudnorm's direct block sums.

    Dense blocks stay on the prefix path; every block whose running-sum
    difference underflowed must be restored to the direct value, with the same
    block starts, the same ``1 / (3 s * fs)`` denominator and the same
    per-channel summation as the baseline.  The whole-series tolerance is
    1e-9 dB: far below the 0.1 dB publication quantum and still more than two
    orders above the measured float-reassociation residue of active blocks.
    """
    from reaper_audio.analysis import metrics

    sample_rate = 8_000
    samples = _dense_zero_denormal_tail(sample_rate)
    expected = _published(_direct_short_term(samples, sample_rate))
    actual = _fast_loudness_profile(samples, sample_rate)["short_term"]

    assert actual.shape == expected.shape
    assert np.array_equal(np.isfinite(actual), np.isfinite(expected))
    delta = np.abs(actual - expected)
    assert int(np.count_nonzero(delta > 1e-9)) == 0
    assert float(np.max(delta)) <= 1e-9

    deep = np.isfinite(expected) & (expected < -200.0)
    assert deep.sum() >= 50
    assert np.array_equal(actual[deep], expected[deep])

    silent = expected == -200.0
    assert silent.sum() >= 100
    assert np.array_equal(actual[silent], expected[silent])

    # The fixture must fail without the refinement: publish the same prefix
    # sums through the same sentinel convention and the deep blocks collapse.
    filtered = _k_weighted(samples, sample_rate)
    starts, ends = metrics._block_grid(len(filtered), sample_rate, 3.0, 0.97)
    window = 3.0 * sample_rate
    prefix = np.concatenate((np.zeros((1, filtered.shape[1]), dtype=np.float64),
                             np.cumsum(np.square(filtered), axis=0)), axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        prefix_levels = -0.691 + 10.0 * np.log10(
            np.sum(((prefix[ends] - prefix[starts]) / window).T, axis=0))
    unrefined = _published(prefix_levels)
    assert int(np.count_nonzero((unrefined == -200.0) & deep)) >= 50
    assert float(np.max(np.abs(unrefined - expected))) > 1_000.0


def test_short_term_deep_blocks_keep_finite_reference_values() -> None:
    """A finite reference level must never be clamped to the -200 sentinel."""
    sample_rate = 8_000
    samples = _dense_zero_denormal_tail(sample_rate)
    expected = _published(_direct_short_term(samples, sample_rate))
    actual = _fast_loudness_profile(samples, sample_rate)["short_term"]

    assert expected.min() < -3_000.0
    assert actual.min() == expected.min()
    deep = np.isfinite(expected) & (expected < -200.0)
    assert deep.sum() >= 50
    assert np.all(np.isfinite(actual[deep]))
    assert np.array_equal(actual[deep], expected[deep])
    assert not np.any((actual == -200.0) & (expected < -200.0))


def test_refinement_restores_every_block_the_running_sum_lost() -> None:
    """Blocks the prefix difference cannot represent get the direct sum."""
    from reaper_audio.analysis import metrics

    sample_rate = 8_000
    samples = _dense_zero_denormal_tail(sample_rate, duration_s=45.0)
    filtered = _k_weighted(samples, sample_rate)
    starts, ends = metrics._block_grid(len(filtered), sample_rate, 3.0, 0.97)
    prefix = np.concatenate((np.zeros((1, 2), dtype=np.float64),
                             np.cumsum(np.square(filtered), axis=0)), axis=0)
    window = 3.0 * sample_rate
    naive_z = ((prefix[ends] - prefix[starts]) / window).T
    scale = prefix[ends] + prefix[starts]
    with np.errstate(divide="ignore", invalid="ignore"):
        naive_levels = -0.691 + 10.0 * np.log10(np.sum(naive_z, axis=0))

    levels = naive_levels.copy()
    z = naive_z.copy()
    refined = metrics._recompute_underflowed_blocks(
        filtered, sample_rate, 3.0, levels, z, starts, ends, scale)

    assert refined.size > 0
    lost = np.flatnonzero(~np.isfinite(naive_levels) | (np.sum(naive_z, axis=0) <= 0.0))
    assert lost.size > 0
    assert set(lost.tolist()) <= set(refined.tolist())

    for index in refined:
        direct = sum(
            (1.0 / window) * np.sum(np.square(filtered[starts[index]:ends[index], channel]))
            for channel in range(2))
        assert z[0, index] + z[1, index] == direct
        if direct > 0.0:
            assert levels[index] == -0.691 + 10.0 * np.log10(direct)
        else:
            assert levels[index] == -np.inf


def test_measure_audio_publishes_direct_reference_short_term_cells() -> None:
    """Published ``short_term_lufs`` cells must equal the baseline after rounding.

    This is the field that drifted by up to 3033.8 dB across 2,824 capture cells:
    a silent tail was published as -200.0 where the direct block sum is finite.
    """
    sample_rate = 8_000
    dense_s = 8.0
    t = np.arange(int(round(24.0 * sample_rate)), dtype=np.float64) / sample_rate
    samples = np.zeros((len(t), 2), dtype=np.float64)
    dense = t < dense_s
    denormal = (t >= 12.0) & (t < 18.0)
    samples[dense, 0] = 0.09 * np.sin(2 * np.pi * 220 * t[dense])
    samples[dense, 1] = 0.06 * np.sin(2 * np.pi * 330 * t[dense] + 0.1)
    samples[denormal, 0] = 1e-160 * np.sin(2 * np.pi * 220 * t[denormal])
    samples[denormal, 1] = 1e-160 * np.cos(2 * np.pi * 180 * t[denormal])

    expected = _published(_direct_short_term(samples, sample_rate))
    report = measure_audio(AudioBuffer(samples, sample_rate))
    cells = report.curves["temporal_profile"]["cells"]
    assert len(cells) >= len(expected)
    compared = 0
    for index, cell in enumerate(cells[: len(expected)]):
        if "short_term_lufs" not in cell:
            continue
        assert cell["short_term_lufs"] == round(float(expected[index]), 1), index
        compared += 1
    assert compared == len(expected)
    published = [cell["short_term_lufs"] for cell in cells if "short_term_lufs" in cell]
    assert min(published) < -3_000.0


def test_refinement_envelope_scales_with_the_running_sum() -> None:
    """The underflow test must follow the running sum, not a fixed floor.

    A block whose power is far above any fixed absolute threshold can still be
    a cancellation residual when the cumulative sum is large; the envelope
    ``k * eps * S`` is what decides.  Here block 1 (7e-11) is inside the
    envelope of a 1e6-energy running sum and must be restored, while the loud
    block 0 must stay on the prefix path.
    """
    from reaper_audio.analysis import metrics

    sample_rate = 8_000
    filtered = np.zeros((120_000, 2), dtype=np.float64)
    starts = np.asarray([0, 24_000, 72_000], dtype=np.int64)
    ends = np.asarray([24_000, 48_000, 100_000], dtype=np.int64)
    z = np.asarray([[1e-3, 7e-11, 1e-30], [1e-3, 7e-11, 1e-30]], dtype=np.float64)
    scale = np.full((3, 2), 2e6, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        levels = -0.691 + 10.0 * np.log10(np.sum(z, axis=0))

    refined = metrics._recompute_underflowed_blocks(
        filtered, sample_rate, 3.0, levels, z.copy(), starts, ends, scale)
    assert refined.tolist() == [1, 2]


def test_negligible_energy_mask_never_hides_energy() -> None:
    """The skip shortcut is sound: true only when every square underflows."""
    from reaper_audio.analysis import metrics

    limit = metrics._SQUARE_UNDERFLOW_LIMIT
    filtered = np.zeros((200_000, 2), dtype=np.float64)
    filtered[60_000:120_000] = 1e-200     # squares underflow to +0.0
    filtered[120_000:160_000] = 1e-160    # squares are subnormal, not zero
    filtered[160_000:, 0] = 0.5
    starts, ends = metrics._block_grid(len(filtered), 8_000, 3.0, 0.97)
    indices = np.arange(starts.size)
    mask = metrics._negligible_energy_mask(filtered, starts, ends, indices)
    for index in indices:
        block = filtered[starts[index]:ends[index]]
        if mask[index]:
            assert np.max(np.abs(block)) <= limit
            assert np.sum(np.square(block)) == 0.0
    assert mask.sum() > 0
    assert not mask.all()
    subnormal = np.flatnonzero(starts >= 120_000)
    assert subnormal.size > 0 and not np.any(mask[subnormal])


def test_fast_loudness_matches_pyloudnorm_with_documented_tolerances() -> None:
    sample_rate = 8_000
    t = np.arange(8 * sample_rate, dtype=np.float64) / sample_rate
    envelope = 0.06 + 0.04 * np.sin(2 * np.pi * 0.7 * t) ** 2
    samples = np.column_stack((
        envelope * np.sin(2 * np.pi * 110 * t),
        0.8 * envelope * np.sin(2 * np.pi * 220 * t + 0.2),
    ))
    expected_meter = pyln.Meter(sample_rate)
    expected_lufs = float(expected_meter.integrated_loudness(samples))
    expected_momentary = np.asarray(expected_meter.blockwise_loudness,
                                    dtype=np.float64)
    expected_lra = float(expected_meter.loudness_range(samples))
    expected_short_meter = pyln.Meter(sample_rate, block_size=3.0, overlap=0.97)
    expected_short_meter.integrated_loudness(samples)
    expected_short = np.asarray(expected_short_meter.blockwise_loudness,
                                dtype=np.float64)
    actual = _fast_loudness_profile(samples, sample_rate)
    assert abs(actual["lufs"] - expected_lufs) < 1e-9
    # The prefix-sum 3 s LRA path is not refined (all its blocks are gated at
    # -70 LUFS), and a short-window LRA can shift by ~0.0011 LU.
    assert abs(actual["lra"] - expected_lra) < 0.01
    assert np.max(np.abs(actual["momentary"] - expected_momentary)) < 1e-9
    assert np.max(np.abs(actual["short_term"] - _published(expected_short))) <= 1e-9
