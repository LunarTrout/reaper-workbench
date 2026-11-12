"""Limiter search logic without REAPER: parameter lookup and bisection."""

from __future__ import annotations

import pytest

from reaper_audio.daw import limiter
from reaper_audio.daw.limiter import LimiterReport, _index, _search_threshold


class _Session:
    def __init__(self):
        self.value = 0.5

    def plan(self, operations, *, reason):
        return operations

    def apply(self, plan, *, dry_run=False):
        self.value = float(plan[0].values["normalized"])
        return {"applied": True}


def test_index_is_case_insensitive():
    params = [{"index": 4, "name": "Ceiling"}, {"index": 5, "name": "Threshold"}]
    assert _index(params, "ceiling") == 4
    assert _index(params, "THRESHOLD") == 5


def test_index_reports_a_missing_parameter():
    with pytest.raises(KeyError):
        _index([{"index": 0, "name": "Threshold"}], "Ceiling")


def _report(lufs, tp, target=-10.0, ceiling=-1.0):
    return LimiterReport(lufs_before=-15.0, lufs_after=lufs, true_peak_after=tp,
                         plr_before=13.0, plr_after=9.0, target_lufs=target,
                         ceiling_dbtp=ceiling, threshold_db=0.0)


def test_ok_requires_loudness_and_peak():
    assert _report(-10.1, -1.2).ok
    assert not _report(-11.0, -1.2).ok
    assert not _report(-10.1, -0.5).ok


def test_search_converges_on_a_monotone_plugin(monkeypatch):
    session = _Session()

    def fake_measure(_render, _job):
        # Higher normalised threshold = louder, up to -4 LUFS.
        return {"lufs": -60.0 + 56.0 * session.value, "true_peak_dbtp": -1.0,
                "plr_db": 10.0}

    monkeypatch.setattr(limiter, "_measure", fake_measure)
    best, norm = _search_threshold(session, "track", "fx", 0, target_lufs=-10.0,
                                   render=lambda job: None, tolerance_lu=0.5,
                                   max_iterations=9)
    assert abs(best["lufs"] - (-10.0)) <= 1.0
    assert 0.0 <= norm <= 1.0
