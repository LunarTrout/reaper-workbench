"""Parameter-name parsing and calibration curves for EQ moves."""

from __future__ import annotations

import math

import pytest

from reaper_audio.daw.eq import (Calibration, band_type, classify, is_filter_band,
                                 layout, plan_moves)


def test_classify_splits_prefix_and_band():
    assert classify("Freq-Band 2") == ("freq", "band 2")
    assert classify("Gain-Low Shelf") == ("gain", "low shelf")
    assert classify("BW-High Pass 5") == ("bw", "high pass 5")
    assert classify("Global Gain") is None


def test_band_type_and_filter_detection():
    assert band_type("band 2") == "peaking"
    assert band_type("high shelf 4") == "shelf"
    assert band_type("high pass 5") == "filter"
    assert is_filter_band("low pass 3")


def test_layout_groups_parameters_by_band():
    params = [
        {"index": 0, "name": "Freq-Low Shelf"},
        {"index": 1, "name": "Gain-Low Shelf"},
        {"index": 3, "name": "Freq-Band 2"},
        {"index": 4, "name": "Gain-Band 2"},
        {"index": 15, "name": "Global Gain"},
    ]
    bands = layout(params)
    assert set(bands) == {"low shelf", "band 2"}
    assert bands["band 2"]["freq"].index == 3
    assert bands["band 2"]["gain"].index == 4


def test_calibration_interpolates_in_log_space_for_frequency():
    cal = Calibration(index=3, samples=[(0.0, 20.0), (0.5, 1000.0), (1.0, 20000.0)], log_y=True)
    assert cal.to_normalized(1000.0) == pytest.approx(0.5, abs=0.02)
    assert cal.to_normalized(20.0) == pytest.approx(0.0, abs=0.02)
    assert cal.to_normalized(20000.0) == pytest.approx(1.0, abs=0.02)
    assert cal.to_normalized(200.0) < 0.5


def test_gain_calibration_is_linear():
    cal = Calibration(index=4, samples=[(0.0, -12.0), (0.5, 0.0), (1.0, 12.0)], log_y=False)
    assert cal.to_normalized(0.0) == pytest.approx(0.5)
    assert cal.to_normalized(-6.0) == pytest.approx(0.25)
    assert cal.to_normalized(6.0) == pytest.approx(0.75)


def _fake_bands():
    return {
        "band 2": {kind: object() for kind in ("freq", "gain")},
        "band 3": {kind: object() for kind in ("freq", "gain")},
        "low shelf": {kind: object() for kind in ("freq", "gain")},
        "high pass 5": {kind: object() for kind in ("freq", "gain")},
    }


class _NoSession:
    def plan(self, *args, **kwargs):
        raise AssertionError("plan must not be reached with an invalid value")


def test_non_finite_calibration_is_rejected_before_any_write():
    from reaper_audio.daw.eq import apply_moves
    from reaper_audio.errors import ValidationError
    nan = float("nan")
    calibration = {
        "band 2": {
            "freq": Calibration(3, [(0.0, 100.0), (1.0, 100.0)], log_y=True),
            "gain": Calibration(4, [(0.0, 0.0), (1.0, nan)], log_y=False),
        }
    }
    with pytest.raises(ValidationError):
        apply_moves(_NoSession(), "track", "fx", calibration,
                    [{"band": "band 2", "freq_hz": 100.0, "gain_db": 3.0}],
                    reason="test")


def test_plan_moves_skips_shelves_and_filters():
    # plan_moves reads frequencies from calibration values; give it real ones.
    cal = {
        "band 2": {"freq": Calibration(3, [(0.0, 100.0), (1.0, 100.0)], True), "gain": None},
        "band 3": {"freq": Calibration(6, [(0.0, 10000.0), (1.0, 10000.0)], True), "gain": None},
        "low shelf": {"freq": Calibration(0, [(0.0, 50.0), (1.0, 50.0)], True), "gain": None},
        "high pass 5": {"freq": Calibration(12, [(0.0, 30.0), (1.0, 30.0)], True), "gain": None},
    }
    moves = plan_moves({120.0: -4.0, 8000.0: 2.0}, cal, max_bands=4)
    bands = {m["band"] for m in moves}
    assert bands <= {"band 2", "band 3"}
    assert len(moves) == 2
