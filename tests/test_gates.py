"""Defect gates must fire on the defect and stay silent on clean audio."""

from __future__ import annotations

import numpy as np
import pytest

from reaper_audio.corpus.gates import check, clipping, dc_offset, dropouts, level_jumps


def _music(seconds: float = 4.0, sr: int = 32000, seed: int = 0):
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * sr)) / sr
    tone = 0.2 * np.sin(2 * np.pi * 220 * t) + 0.05 * rng.normal(size=t.size)
    return np.stack([tone, tone * 0.9], axis=1), sr


def test_clean_signal_has_no_clipping():
    x, sr = _music()
    assert clipping(x)["clip_runs"] == 0.0


def test_clipped_signal_is_detected():
    x, sr = _music()
    y = np.clip(x * 5, -0.5, 0.5)
    values = clipping(y, full_scale=0.5)
    assert values["clip_runs"] > 0
    assert values["clip_fraction"] > 0


def test_dc_offset_is_detected():
    x, sr = _music()
    y = x + 0.2
    assert dc_offset(y)["dc_max"] == pytest.approx(0.2, abs=0.01)


def test_dropout_run_is_detected():
    x, sr = _music(seconds=4.0)
    y = x.copy()
    y[sr:sr * 2] = 0.0
    values = dropouts(y, sr)
    assert values["silence_longest_s"] >= 0.9


def test_level_jump_is_detected():
    x, sr = _music(seconds=4.0)
    y = x.copy()
    y[sr * 2:] *= 0.05
    values = level_jumps(y, sr)
    assert values["level_jump_db"] > 20.0


def test_policy_turns_measurements_into_violations():
    x, sr = _music()
    clipped = np.clip(x * 8, -0.3, 0.3)
    result = check(clipped, sr, {"max_clip_runs": 0})
    assert "clipping" in result.violations
    clean = check(x, sr, {"max_clip_runs": 0})
    assert clean.violations == ()
