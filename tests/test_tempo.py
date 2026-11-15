from __future__ import annotations

import numpy as np

from reaper_audio.analysis.metrics import measure_audio
from reaper_audio.models import AudioBuffer
from reaper_audio.analysis.tempo import _quantile_linear, estimate_tempo_curve


def _assert_quantile_parity(values: np.ndarray, quantiles: list[float]) -> None:
    actual = _quantile_linear(values, quantiles)
    expected = np.quantile(values, quantiles)
    # Float64 equivalence, not a bit-identical claim: the partition path does
    # the same linear interpolation but may round a different number of times.
    np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=1e-12)


def test_partition_quantiles_match_numpy_for_odd_and_even_lengths() -> None:
    rng = np.random.default_rng(41)
    for size in list(range(1, 20)) + [32, 33, 64, 127, 128, 255, 1024]:
        values = rng.normal(0.0, 1.0, size)
        _assert_quantile_parity(values, [0.0, 0.25, 0.5, 0.75, 1.0])
        _assert_quantile_parity(values, [0.1, 0.9])


def test_partition_quantiles_match_numpy_for_sorted_and_duplicate_values() -> None:
    rng = np.random.default_rng(43)
    for values in (
        np.sort(rng.normal(0.0, 1.0, 257)),
        np.asarray([-3.5, -3.5, 0.0, 0.0, 0.0, 2.25, 9.0]),
        np.full(17, 0.25),
        np.asarray([1e-6, 2e-6, 0.5, 1.0, 2.0]),
    ):
        _assert_quantile_parity(values, [0.0, 0.25, 0.5, 0.75, 1.0])


def test_partition_quantiles_handle_extreme_quantiles_and_endpoints() -> None:
    rng = np.random.default_rng(47)
    values = rng.uniform(0.001, 2.0, 129)
    actual = _quantile_linear(values, [0.0, 1e-12, 0.5, 1.0 - 1e-12, 1.0])
    expected = np.quantile(values, [0.0, 1e-12, 0.5, 1.0 - 1e-12, 1.0])
    assert actual[0] == expected[0] == values.min()
    assert actual[-1] == expected[-1] == values.max()
    np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=1e-15)


def test_partition_quantiles_stay_within_float64_tolerance() -> None:
    rng = np.random.default_rng(53)
    values = rng.normal(0.0, 1e9, 128)
    quantiles = [1e-12, 0.25, 0.5, 0.75, 1.0 - 1e-12]
    actual = _quantile_linear(values, quantiles)
    expected = np.quantile(values, quantiles)
    residue = float(np.max(np.abs(actual - expected)))
    # The residue may be exactly zero for a given draw; the contract is the
    # tolerance, not a claim that every draw is bit-identical.
    assert residue <= 1e-6  # relative ~1e-15 at this magnitude
    np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=1e-6)


def test_partition_quantiles_of_an_empty_array_are_empty() -> None:
    result = _quantile_linear(np.asarray([], dtype=np.float64), [0.25, 0.5])
    assert result.size == 0


def test_local_tempo_curve_reports_bpm_and_metrical_alternatives_per_cell() -> None:
    onsets = np.arange(0.25, 16.0, 0.5)
    cell_times = np.arange(0.0, 16.0, 0.1)

    result = estimate_tempo_curve(onsets, cell_times)

    assert len(result["points"]) == len(cell_times)
    point = result["points"][80]
    assert point["tempo_bpm"] == 120.0
    assert point["confidence"] > 0.9
    assert any(item["tempo_bpm"] == 240.0 for item in point["alternatives"])
    assert point["window_s"]["start"] < point["time_s"] < point["window_s"]["end"]


def test_local_tempo_is_explicitly_unknown_without_repeated_events() -> None:
    result = estimate_tempo_curve([1.0, 2.0], [0.0, 0.1, 0.2])

    assert [point["tempo_bpm"] for point in result["points"]] == [None, None, None]
    assert all(point["confidence"] == 0.0 for point in result["points"])
    assert all(point["reason"] == "insufficient repeated events"
               for point in result["points"])


def test_full_measurement_returns_per_cell_tempo_for_a_120_bpm_signal() -> None:
    sample_rate = 8_000
    samples = np.zeros(sample_rate * 12, dtype=np.float64)
    pulse_time = np.arange(80, dtype=np.float64) / sample_rate
    pulse = 0.8 * np.sin(2 * np.pi * 1_000 * pulse_time) * np.hanning(80)
    for start_s in np.arange(0.25, 12.0, 0.5):
        start = int(start_s * sample_rate)
        samples[start:start + len(pulse)] = pulse

    report = measure_audio(AudioBuffer(samples, sample_rate))

    profile = report.curves["temporal_profile"]
    assert report.curves["events_2ms"]["onsets"] == 24
    assert all(cell["tempo_bpm"] == 120.0 for cell in profile["cells"])
    assert all(cell["tempo_alternatives"][0]["tempo_bpm"] == 240.0
               for cell in profile["cells"])
