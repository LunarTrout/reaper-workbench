from __future__ import annotations

import numpy as np

from reaper_audio.analysis.metrics import measure_audio
from reaper_audio.models import AudioBuffer
from reaper_audio.analysis.tempo import estimate_tempo_curve


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
