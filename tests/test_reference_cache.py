"""A reference is measured once per content/window/analyzer, not per call."""

from __future__ import annotations

import numpy as np
import soundfile as sf

from reaper_audio.analysis import metrics
from reaper_audio.analysis.audio import load_audio
from reaper_audio.daw.reference import cached_reference_report


def _write(path, frequency: float) -> None:
    rate = 48_000
    time = np.arange(rate, dtype=np.float64) / rate
    samples = 0.1 * np.sin(2 * np.pi * frequency * time)
    sf.write(path, np.column_stack([samples, samples]), rate, subtype="FLOAT")


def test_reference_report_is_cached_by_content(tmp_path, monkeypatch) -> None:
    source = tmp_path / "reference.wav"
    _write(source, 220.0)
    calls = 0
    original = metrics.measure_audio

    def counted(audio, **kwargs):
        nonlocal calls
        calls += 1
        return original(audio, **kwargs)

    monkeypatch.setattr(metrics, "measure_audio", counted)
    root = tmp_path / "archive"
    first = cached_reference_report(load_audio(source), source, None, root)
    second = cached_reference_report(load_audio(source), source, None, root)

    assert calls == 1
    assert first.metrics["integrated_lufs"].value == second.metrics["integrated_lufs"].value
    assert list((root / "measurement-cache").rglob("*.json"))

    _write(source, 330.0)
    third = cached_reference_report(load_audio(source), source, None, root)
    assert calls == 2
    assert third.metrics["spectral_centroid_hz"].value != first.metrics["spectral_centroid_hz"].value
