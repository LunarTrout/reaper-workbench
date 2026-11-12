"""Delivery: status, master trim maths and the measured trim loop."""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from reaper_audio.daw.delivery import (measure_delivery, plan_master_trim, status,
                                       trim_to_target)


def _sine(path, amplitude, sr=32000, seconds=2.0):
    t = np.arange(int(seconds * sr)) / sr
    x = amplitude * np.sin(2 * np.pi * 997 * t)
    sf.write(str(path), np.stack([x, x], axis=1), sr, subtype="FLOAT")
    return path


class _Session:
    def __init__(self, volume=1.0):
        self.volume = volume
        self.operations = []

    def snapshot(self, **_):
        return {"master": {"guid": "{M}", "volume": self.volume}}

    def plan(self, operations, *, reason):
        self.operations = operations
        return operations

    def apply(self, plan, *, dry_run=False):
        self.volume = plan[0].values["volume"]
        return {"applied": True}


def test_status_flags_loudness_and_peak():
    ok, issues = status(-12.0, 0.5, {"target_lufs": -14.0, "max_true_peak_dbtp": -1.0,
                                     "max_lufs_error": 0.2})
    assert not ok
    assert any("loudness" in i for i in issues)
    assert any("true peak" in i for i in issues)


def test_status_accepts_a_master_inside_the_policy():
    ok, issues = status(-14.0, -1.2, {"target_lufs": -14.0, "max_true_peak_dbtp": -1.0})
    assert ok and issues == []


def test_master_trim_is_linear_gain():
    session = _Session(volume=0.5)
    plan_master_trim(session, master_guid="{M}", delta_db=6.0, reason="test")
    assert session.operations[0].values["volume"] == pytest.approx(0.5 * 10 ** (6 / 20))


def test_trim_loop_stops_when_the_target_is_reached(tmp_path):
    session = _Session()
    first = _sine(tmp_path / "first.wav", 0.2)
    second = _sine(tmp_path / "second.wav", 0.1)
    target = measure_delivery(second)["lufs"]
    sequence = [first, second]

    def render(_job):
        return sequence[min(len(session.operations), 1)]

    report = trim_to_target(session, master_guid="{M}",
                            policy={"target_lufs": target, "max_lufs_error": 0.2},
                            render=render)
    assert len(session.operations) == 1
    assert report.ok
    assert report.lufs == pytest.approx(target, abs=0.2)
    assert len(report.history) >= 2


def test_trim_loop_reports_that_limiting_is_needed(tmp_path):
    session = _Session()
    hot = _sine(tmp_path / "hot.wav", 0.9)

    def render(_job):
        return hot

    report = trim_to_target(session, master_guid="{M}",
                            policy={"target_lufs": measure_delivery(hot)["lufs"],
                                    "max_true_peak_dbtp": -6.0},
                            render=render)
    assert not report.ok
    assert any("needs limiting" in i for i in report.issues)
