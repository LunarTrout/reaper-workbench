"""Role-level stems: discovery, reconstruction and per-role matrices."""

from __future__ import annotations

import numpy as np
import soundfile as sf

from reaper_audio.corpus.roles import discover, reconstruction, role_matrix


def _write(path, audio, sr=32000):
    sf.write(str(path), audio, sr, subtype="FLOAT")


def test_discover_maps_role_to_files(tmp_path):
    for source in ("track1", "track2"):
        for role in ("drums", "bass"):
            d = tmp_path / "htdemucs" / source
            d.mkdir(parents=True, exist_ok=True)
            _write(d / f"{role}.wav", np.zeros((32000, 2)))
    roles = discover(tmp_path)
    assert set(roles) == {"drums", "bass"}
    assert len(roles["drums"]) == 2


def test_reconstruction_of_exact_stems_is_perfect(tmp_path):
    rng = np.random.default_rng(0)
    sr = 32000
    a = rng.normal(0, 0.05, (sr, 2))
    b = rng.normal(0, 0.05, (sr, 2))
    mix = a + b
    _write(tmp_path / "mix.wav", mix, sr)
    _write(tmp_path / "a.wav", a, sr)
    _write(tmp_path / "b.wav", b, sr)
    result = reconstruction(tmp_path / "mix.wav",
                            {"a": tmp_path / "a.wav", "b": tmp_path / "b.wav"})
    assert result.correlation > 0.999
    assert result.residual_db < -100


def test_reconstruction_reports_a_bad_stem_set(tmp_path):
    rng = np.random.default_rng(1)
    sr = 32000
    mix = rng.normal(0, 0.05, (sr, 2))
    _write(tmp_path / "mix.wav", mix, sr)
    _write(tmp_path / "a.wav", mix * 0.5, sr)
    result = reconstruction(tmp_path / "mix.wav", {"a": tmp_path / "a.wav"})
    assert result.residual_db > -12


def test_silent_windows_are_skipped(tmp_path):
    sr = 32000
    silent_then_signal = np.zeros((sr * 4, 2))
    silent_then_signal[sr:] = np.random.default_rng(3).normal(0, 0.05, (sr * 3, 2))
    _write(tmp_path / "vocals.wav", silent_then_signal, sr)
    matrix = role_matrix([tmp_path / "vocals.wav"], window_s=1.0)
    assert matrix.shape[0] == 3


def test_silent_signal_still_gives_finite_features():
    from reaper_audio.corpus import measure_signal
    values = measure_signal(np.zeros((32000, 2)), 32000).values
    assert np.all(np.isfinite(values))


def test_model_rejects_non_finite_matrix():
    import pytest
    from reaper_audio.corpus import CorpusModel
    matrix = np.zeros((4, 3))
    matrix[1, 1] = np.nan
    with pytest.raises(ValueError):
        CorpusModel.fit(matrix)


def test_role_matrix_shape(tmp_path):
    sr = 32000
    _write(tmp_path / "drums.wav", np.random.default_rng(2).normal(0, 0.05, (sr * 5, 2)), sr)
    matrix = role_matrix([tmp_path / "drums.wav"], window_s=1.0)
    assert matrix.shape[0] == 5
