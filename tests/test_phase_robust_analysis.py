from __future__ import annotations

import numpy as np
import pytest

from reaper_audio.analysis.events import detect_events
from reaper_audio.analysis.phase import compare_track_phase
from reaper_audio.analysis.peaks import estimate_true_peak
from reaper_audio.analysis.temporal import analyze as analyze_temporal_profile
from reaper_audio.analysis.transients import detect_transients
from reaper_audio.models import AudioBuffer, Section


def _burst_signal(sample_rate: int = 48_000, duration_s: float = 1.2) -> np.ndarray:
    samples = np.zeros(round(sample_rate * duration_s), dtype=np.float64)
    burst_frames = round(0.035 * sample_rate)
    tone = np.sin(2 * np.pi * 1_100 * np.arange(burst_frames) / sample_rate)
    for start_s in (0.1, 0.3, 0.5, 0.7, 0.9, 1.1):
        start = round(start_s * sample_rate)
        samples[start:start + burst_frames] = 0.35 * tone
    return samples


def test_event_detector_does_not_lose_antiphase_stereo_attacks() -> None:
    mono = _burst_signal()
    in_phase = np.column_stack((mono, mono))
    anti_phase = np.column_stack((mono, -mono))

    expected = detect_events(in_phase, fs=48_000)
    actual = detect_events(anti_phase, fs=48_000)

    assert expected["onsets"] > 0
    assert actual["onsets"] == expected["onsets"]
    assert actual["count"] == expected["count"]


def test_clip_detection_checks_each_channel_before_any_fold_down() -> None:
    mono = np.zeros(48_000, dtype=np.float64)
    mono[12_000:12_200] = 1.05
    anti_phase = np.column_stack((mono, -mono))

    report = detect_events(anti_phase, fs=48_000)

    clip_events = [event for event in report["events"] if event["kind"] == "clip"]
    assert len(clip_events) == 1
    assert clip_events[0]["count"] == 400


def test_transient_detector_is_phase_invariant_for_stereo() -> None:
    mono = _burst_signal()
    in_phase = AudioBuffer(np.column_stack((mono, mono)), 48_000)
    anti_phase = AudioBuffer(np.column_stack((mono, -mono)), 48_000)

    expected = detect_transients(in_phase, frame_size=1_024, hop_size=256)
    actual = detect_transients(anti_phase, frame_size=1_024, hop_size=256)

    assert expected["event_count"] > 0
    assert actual["event_count"] == expected["event_count"]


def test_temporal_spectrum_uses_channel_power_not_cancelling_mid_signal() -> None:
    sample_rate = 8_000
    time = np.arange(sample_rate, dtype=np.float64) / sample_rate
    mono = 0.2 * np.sin(2 * np.pi * 440 * time)

    in_phase = analyze_temporal_profile(np.column_stack((mono, mono)), fs=sample_rate,
                                        hop_s=0.1, nfft=1_024)
    anti_phase = analyze_temporal_profile(np.column_stack((mono, -mono)), fs=sample_rate,
                                          hop_s=0.1, nfft=1_024)

    for expected, actual in zip(in_phase["cells"], anti_phase["cells"]):
        np.testing.assert_allclose(actual["bands"], expected["bands"], atol=1e-9)
        assert actual["rms_db"] == expected["rms_db"]
        assert actual["f0_hz"] == expected["f0_hz"]


def test_mono_temporal_profile_does_not_fabricate_stereo_fields() -> None:
    sample_rate = 8_000
    time = np.arange(sample_rate, dtype=np.float64) / sample_rate
    mono = 0.2 * np.sin(2 * np.pi * 440 * time)

    profile = analyze_temporal_profile(mono, fs=sample_rate, hop_s=0.1, nfft=1_024)

    assert profile["cells"]
    assert all(cell["pan"] is None for cell in profile["cells"])
    assert all(cell["corr"] is None for cell in profile["cells"])


def test_true_peak_estimate_detects_intersample_overshoot() -> None:
    sample_rate = 48_000
    frequency = 0.25 * sample_rate
    sample_indices = np.arange(2_048, dtype=np.float64)
    # At this frequency, place each sinusoid maximum halfway between samples.
    phase = np.pi / 2 - np.pi * frequency / sample_rate
    samples = 0.99 * np.sin(2 * np.pi * frequency * sample_indices / sample_rate + phase)

    result = estimate_true_peak(samples)

    assert result["true_peak_linear"] > result["sample_peak_linear"]
    assert result["true_peak_dbfs_estimate"] > result["sample_peak_dbfs"]
    assert "not validated for standards compliance" in result["coverage"]


def test_true_peak_estimate_is_stable_across_chunk_boundaries() -> None:
    sample_rate = 48_000
    sample_indices = np.arange(12_003, dtype=np.float64)
    samples = 0.8 * np.sin(2 * np.pi * 17_333 * sample_indices / sample_rate + 0.17)

    large_chunk = estimate_true_peak(samples, chunk_frames=65_536)
    small_chunks = estimate_true_peak(samples, chunk_frames=127)

    np.testing.assert_allclose(
        small_chunks["per_channel_true_peak_linear"],
        large_chunk["per_channel_true_peak_linear"], atol=1e-10,
    )


def test_pairwise_phase_reports_antiphase_energy_and_spectral_evidence() -> None:
    sample_rate = 8_000
    rng = np.random.default_rng(2026)
    source = rng.normal(0.0, 0.2, sample_rate * 2)
    a = AudioBuffer(source, sample_rate, source="mic-a")
    b = AudioBuffer(-source, sample_rate, source="mic-b")

    result = compare_track_phase(a, b)
    channel = result["channel_pairs"][0]

    assert result["coverage"]["status"] == "available"
    assert channel["zero_lag_correlation"] == pytest.approx(-1.0, abs=1e-6)
    assert channel["power_change_vs_incoherent_sum_db"] <= -100.0
    valid_bands = [band for band in channel["frequency_bands"]
                   if band["status"] == "available"]
    assert valid_bands
    assert all(band["mean_squared_coherence"] > 0.99 for band in valid_bands)
    assert all(abs(abs(band["mean_cross_spectral_phase_rad"]) - np.pi) < 1e-6
               for band in valid_bands)


def test_pairwise_phase_delay_sign_and_time_alignment_are_explicit() -> None:
    sample_rate = 8_000
    rng = np.random.default_rng(42)
    source = rng.normal(0.0, 0.2, sample_rate * 3)
    delay_frames = 24
    delayed = np.concatenate((np.zeros(delay_frames), source[:-delay_frames]))
    result = compare_track_phase(
        AudioBuffer(source, sample_rate, source="direct"),
        AudioBuffer(delayed, sample_rate, source="mic"),
    )

    estimate = result["channel_pairs"][0]["relative_delay"]
    assert result["coverage"]["status"] == "available"
    assert estimate["median_ms"] == pytest.approx(3.0, abs=0.3)
    assert estimate["sign_convention"].startswith("positive means source B")

    fractional = compare_track_phase(
        AudioBuffer(source, sample_rate, source="a"),
        AudioBuffer(source, sample_rate, source="b", start_s=0.0000625),
    )
    assert fractional["coverage"]["status"] == "unavailable"
    assert fractional["coverage"]["reason"] == "start_times_not_sample_aligned"


def test_pairwise_phase_resamples_to_common_rate_and_limits_section() -> None:
    sample_rate_a = 44_100
    sample_rate_b = 48_000
    time_a = np.arange(sample_rate_a) / sample_rate_a
    time_b = np.arange(sample_rate_b) / sample_rate_b
    a = AudioBuffer(0.2 * np.sin(2 * np.pi * 440 * time_a),
                    sample_rate_a, source="a")
    b = AudioBuffer(0.2 * np.sin(2 * np.pi * 440 * time_b),
                    sample_rate_b, source="b")

    result = compare_track_phase(a, b, section=Section(0.2, 0.8))

    assert result["coverage"]["status"] == "available"
    assert result["sample_rate_hz"] == sample_rate_a
    assert result["resampled"] == {"a": False, "b": True}
    assert result["time_range_s"]["start"] == pytest.approx(0.2, abs=1 / sample_rate_a)
    assert result["duration_s"] == pytest.approx(0.6, abs=2 / sample_rate_a)
