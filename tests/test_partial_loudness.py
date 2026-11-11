from __future__ import annotations

import json

import numpy as np
import pytest

from reaper_audio.analysis.partial import attribute_partial_loudness
from reaper_audio.errors import ValidationError
from reaper_audio.models import AudioBuffer


def _tone(sample_rate: int, duration_s: float, frequency_hz: float,
          amplitude: float = 0.05) -> np.ndarray:
    time = np.arange(round(sample_rate * duration_s), dtype=np.float64) / sample_rate
    return amplitude * np.sin(2.0 * np.pi * frequency_hz * time)


def test_single_source_partial_loudness_matches_its_solo_loudness() -> None:
    sample_rate = 48_000
    samples = _tone(sample_rate, 2.0, 110.0)
    mix = AudioBuffer(samples, sample_rate, start_s=2.0, source="mix")
    stem = AudioBuffer(samples, sample_rate, start_s=2.0, source="stem")

    result = attribute_partial_loudness(mix, {"bass": stem})

    bass = result["sources"]["bass"]
    assert bass["partial_sones"] == pytest.approx(bass["solo_sones"], rel=1e-8)
    assert bass["relative_audibility"] == pytest.approx(1.0)
    assert bass["share_of_partial_loudness_pct"] == pytest.approx(100.0)
    assert len(bass["time_series"]["time_s"]) == 10
    json.dumps(result, allow_nan=False)


def test_multi_source_partial_report_has_temporal_and_section_results() -> None:
    sample_rate = 48_000
    bass_samples = _tone(sample_rate, 2.0, 110.0)
    lead_samples = _tone(sample_rate, 2.0, 880.0, amplitude=0.03)
    mix_samples = bass_samples + lead_samples
    mix = AudioBuffer(mix_samples, sample_rate, source="master")
    stems = {
        "bass-guid": AudioBuffer(bass_samples, sample_rate, source="bass-guid"),
        "lead-guid": AudioBuffer(lead_samples, sample_rate, source="lead-guid"),
    }

    result = attribute_partial_loudness(mix, stems, section_s=1.0)

    assert sum(source["share_of_partial_loudness_pct"]
               for source in result["sources"].values()) == pytest.approx(100.0)
    assert all(len(source["sections"]) == 2 for source in result["sources"].values())
    assert all("source_partial_sones" in source["time_series"]
               for source in result["sources"].values())
    json.dumps(result, allow_nan=False)


def test_partial_loudness_rejects_unaligned_sources() -> None:
    sample_rate = 48_000
    mix = AudioBuffer(_tone(sample_rate, 1.0, 110.0), sample_rate, start_s=0.0)
    stem = AudioBuffer(_tone(sample_rate, 1.0, 110.0), sample_rate, start_s=0.1)

    with pytest.raises(ValidationError, match="same start time"):
        attribute_partial_loudness(mix, {"misaligned": stem})
