"""Signal-content tests: intentional silence is not a silent-render failure."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from reaper_audio.daw.signal_gate import (classify_master_silence, dbfs,
                                          measure_wave, source_activity)


def _write(path: Path, *, amplitude: float, seconds: float = 0.25,
           rate: int = 48_000) -> Path:
    frames = int(round(seconds * rate))
    t = np.arange(frames, dtype=np.float64) / rate
    samples = (amplitude * np.sin(2.0 * np.pi * 440.0 * t))[:, None]
    samples = np.repeat(samples, 2, axis=1)
    sf.write(str(path), samples, rate, subtype="PCM_16")
    return path


def test_measure_wave_distinguishes_signal_from_digital_silence(tmp_path: Path) -> None:
    audible = measure_wave(_write(tmp_path / "tone.wav", amplitude=0.25))
    silent = measure_wave(_write(tmp_path / "silence.wav", amplitude=0.0))

    assert audible["audible"] is True
    assert audible["silent"] is False
    assert audible["peak_dbfs"] > -20.0
    assert audible["rms_dbfs"] > -30.0
    assert silent["silent"] is True
    assert silent["peak_dbfs"] == -160.0
    assert dbfs(0.0) == -160.0


def test_silent_master_with_no_audible_sources_is_expected() -> None:
    content = {"peak_dbfs": -160.0, "rms_dbfs": -160.0, "silent": True,
               "audible": False}
    activity = source_activity([
        {"source_guid": "track-a", "name": "empty", "mute": False,
         "content": content},
    ])
    activity["active_audio_items"] = 0
    verdict = classify_master_silence(content, activity,
                                      {"mute": False, "volume": 1.0})

    assert verdict["expected_silence"] is True
    assert verdict["unexpected_silence"] is False
    assert "no_active_audio_sources" in verdict["silence_reason"]


def test_silent_master_with_active_audio_items_is_unexpected() -> None:
    silent = {"peak_dbfs": -160.0, "rms_dbfs": -160.0, "silent": True,
              "audible": False}
    activity = source_activity([
        {"source_guid": "track-a", "name": "guitar", "mute": False,
         "content": silent},
    ])
    activity["active_audio_items"] = 1
    verdict = classify_master_silence(silent, activity,
                                      {"mute": False, "volume": 1.0})

    assert verdict["expected_silence"] is False
    assert verdict["unexpected_silence"] is True
    assert verdict["silence_reason"] is None


def test_silent_master_with_an_audible_source_is_unexpected() -> None:
    silent = {"peak_dbfs": -160.0, "rms_dbfs": -160.0, "silent": True,
              "audible": False}
    audible = {"peak_dbfs": -12.0, "rms_dbfs": -15.0, "silent": False,
               "audible": True}
    activity = source_activity([
        {"source_guid": "track-a", "name": "guitar", "mute": False,
         "content": audible},
        {"source_guid": "track-b", "name": "empty", "mute": False,
         "content": silent},
    ])
    verdict = classify_master_silence(silent, activity,
                                      {"mute": False, "volume": 1.0})

    assert verdict["expected_silence"] is False
    assert verdict["unexpected_silence"] is True
    assert verdict["silence_reason"] is None


def test_muted_master_or_all_muted_tracks_explain_silence() -> None:
    silent = {"peak_dbfs": -160.0, "rms_dbfs": -160.0, "silent": True,
              "audible": False}
    audible = {"peak_dbfs": -12.0, "rms_dbfs": -15.0, "silent": False,
               "audible": True}
    rows = [{"source_guid": "track-a", "name": "guitar", "mute": True,
             "content": audible}]
    activity = source_activity(rows)

    muted_master = classify_master_silence(
        silent, activity, {"mute": True, "volume": 1.0})
    muted_tracks = classify_master_silence(
        silent, activity, {"mute": False, "volume": 1.0})
    zero_master = classify_master_silence(
        silent, source_activity([{"source_guid": "track-a", "name": "guitar",
                                  "mute": False, "content": audible}]),
        {"mute": False, "volume": 0.0})

    assert muted_master["expected_silence"] is True
    assert "master_muted" in muted_master["silence_reason"]
    assert muted_tracks["expected_silence"] is True
    assert "all_project_tracks_muted" in muted_tracks["silence_reason"]
    assert zero_master["expected_silence"] is True
    assert "master_zero_volume" in zero_master["silence_reason"]
