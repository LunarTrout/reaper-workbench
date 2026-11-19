"""Leveling curves must never mute, and profiles must find real phrases."""

from __future__ import annotations

import numpy as np
import pytest

from reaper_audio.daw.leveling import plan_curve
from reaper_audio.daw.profile import build_profile
from reaper_audio.errors import ValidationError

SEGMENTS = [{"start_s": 0.0, "end_s": 30.0, "level_db": -26.94},
            {"start_s": 35.0, "end_s": 80.0, "level_db": -22.46},
            {"start_s": 120.0, "end_s": 230.0, "level_db": -23.33}]


def test_curve_never_writes_zero_and_respects_the_limit() -> None:
    points, moves = plan_curve(SEGMENTS, target_db=-23.33, max_gain_db=6.0,
                               smooth_s=0.15)
    assert all(point["value"] > 0.4 for point in points)
    assert [round(move["gain_db"], 2) for move in moves] == [3.61, -0.87, 0.0]
    times = [point["time_s"] for point in points]
    assert times == sorted(times)
    # a ramp point precedes every segment edge
    assert any(abs(t - (0.0)) < 1e-6 for t in times)


def test_curve_clamps_gain_instead_of_following_a_bad_measurement() -> None:
    points, moves = plan_curve([{"start_s": 10.0, "end_s": 20.0, "level_db": -30.0}],
                               target_db=-23.0, max_gain_db=6.0)
    assert moves[0]["gain_db"] == 6.0
    assert max(point["value"] for point in points) == pytest.approx(2.0, abs=0.01)


def test_curve_refuses_empty_or_overlapping_segments() -> None:
    with pytest.raises(ValidationError):
        plan_curve([], target_db=-20.0)
    with pytest.raises(ValidationError):
        plan_curve([{"start_s": 0, "end_s": 10, "level_db": -20},
                    {"start_s": 5, "end_s": 15, "level_db": -20}], target_db=-20)


def test_profile_finds_the_loud_region_and_ignores_the_gap() -> None:
    rate = 8000
    silence = np.zeros(rate * 2)
    tone = 0.5 * np.sin(2 * np.pi * 220 * np.arange(rate * 3) / rate)
    samples = np.concatenate([silence, tone, silence]).reshape(-1, 1)
    profile = build_profile(samples, rate, step_s=0.5)
    assert profile["segments"], "the tone must be found"
    assert profile["segments"][0]["start_s"] == pytest.approx(2.0, abs=0.6)
    assert profile["target_db"] == pytest.approx(-9.0, abs=1.0)
