import json

import numpy as np
import pytest

from reaper_audio.analysis.audio import slice_audio
from reaper_audio.analysis.metrics import measure_audio
from reaper_audio.errors import ValidationError
from reaper_audio.models import AudioBuffer, Section


def test_slice_audio_uses_absolute_timeline() -> None:
    audio = AudioBuffer(np.arange(10), sample_rate=10, start_s=10.0)

    result = slice_audio(audio, Section(10.2, 10.5))

    np.testing.assert_array_equal(result.samples[:, 0], [2.0, 3.0, 4.0])
    assert result.start_s == pytest.approx(10.2)
    assert result.duration_s == pytest.approx(0.3)


def test_slice_audio_rejects_uncovered_section() -> None:
    audio = AudioBuffer(np.zeros(10), sample_rate=10, start_s=10.0)

    with pytest.raises(ValidationError, match="not fully covered"):
        slice_audio(audio, Section(9.9, 10.2))


def test_measure_audio_applies_section_and_reports_its_timeline() -> None:
    sample_rate = 8_000
    first = np.full(sample_rate, 0.1)
    second = np.full(sample_rate, 0.25)
    audio = AudioBuffer(np.concatenate([first, second]), sample_rate=sample_rate,
                        start_s=5.0, source="synthetic")

    report = measure_audio(audio, section=Section(6.0, 7.0), frame_size=512,
                           hop_size=128)

    assert report.duration_s == pytest.approx(1.0)
    assert report.metrics["peak_dbfs"].value == pytest.approx(20 * np.log10(0.25))
    assert report.provenance["measured_section_s"] == {"start_s": 6.0, "end_s": 7.0}
    assert report.curves["spectrogram_time_s"][0] >= 6.0


def test_measure_audio_includes_the_full_time_local_feature_profile() -> None:
    sample_rate = 48_000
    time = np.arange(sample_rate, dtype=np.float64) / sample_rate
    audio = AudioBuffer(0.2 * np.sin(2 * np.pi * 220 * time), sample_rate,
                        source="A440-test")

    report = measure_audio(audio)

    profile = report.curves["temporal_profile"]
    first = profile["cells"][0]
    assert len(profile["fc"]) == 28
    assert len(first["bands"]) == 28
    assert len(first["erb"]) == 24
    assert first["f0_hz"] == pytest.approx(220.0, abs=2.0)
    assert len(first["chroma"]) == 12
    # Mono input has no inter-channel evidence: spatial fields must stay None
    # instead of inventing a pan/width/coherence pair from a duplicated channel.
    assert first["coherence"] is None
    assert first["corr"] is None
    assert first["width"] is None
    assert first["pan"] is None
    stereo = AudioBuffer(np.column_stack([0.2 * np.sin(2 * np.pi * 220 * time),
                                          0.2 * np.sin(2 * np.pi * 221 * time)]),
                         sample_rate, source="stereo-test")
    stereo_first = measure_audio(stereo).curves["temporal_profile"]["cells"][0]
    assert len(stereo_first["coherence"]) == 3
    assert all("tempo_bpm" in cell for cell in profile["cells"])
    assert all("event_summary_2ms" in cell for cell in profile["cells"])
    assert "events_2ms" in report.curves
    assert report.coverage["temporal_profile"] == "complete"
    json.dumps(report.to_dict(), allow_nan=False)
