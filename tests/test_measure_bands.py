"""Metric selection and band aggregation: shape and arithmetic, no targets."""

from __future__ import annotations

import math

import pytest

from reaper_audio.analysis.metrics import BAND_CENTERS_HZ, DB_FLOOR
from reaper_audio.daw.measure_table import (BANDS_KEY, OCTAVE_CENTERS_HZ,
                                            THIRD_OCTAVE_CENTERS_HZ,
                                            aggregate_bands, build_cell,
                                            parse_bands, parse_metrics,
                                            summarize_rows)
from reaper_audio.errors import ValidationError


def test_the_default_selection_carries_scalars_but_not_the_band_array() -> None:
    selection = parse_metrics(None)
    assert selection.bands is False
    assert "integrated_lufs" in selection.keys
    assert BANDS_KEY not in selection.keys


def test_aliases_expand_to_the_report_keys() -> None:
    selection = parse_metrics(["lufs", "peak", "crest", "side_mid"])
    assert selection.keys == ("integrated_lufs", "peak_dbfs", "crest_db", "side_mid_db")


def test_bands_is_a_selector_not_a_metric_key() -> None:
    selection = parse_metrics(["lufs", "bands"])
    assert selection.bands is True
    assert selection.keys == ("integrated_lufs",)
    assert "band_level_dbfs" not in selection.keys


def test_all_includes_bands() -> None:
    selection = parse_metrics("all")
    assert selection.bands is True
    assert "integrated_lufs" in selection.keys


def test_an_unknown_metric_is_refused_with_the_accepted_names() -> None:
    with pytest.raises(ValidationError) as error:
        parse_metrics(["loudness_target"])
    details = error.value.details
    assert "lufs" in details["aliases"]
    assert "integrated_lufs" in details["metrics"]


def test_octave_and_third_plans_use_standard_grids() -> None:
    octave = parse_bands("octave")
    assert octave.mode == "octave"
    assert len(octave.bands) == len(OCTAVE_CENTERS_HZ)
    third = parse_bands("third")
    assert third.mode == "third"
    assert len(third.bands) == len(THIRD_OCTAVE_CENTERS_HZ)
    assert parse_bands(None).mode == "none"


def test_explicit_band_edges_are_kept() -> None:
    plan = parse_bands([[30.0, 60.0], {"lo_hz": 60.0, "hi_hz": 120.0,
                                       "label": "low mid"}])
    assert plan.mode == "explicit"
    assert [edge[:2] for edge in plan.bands] == [(30.0, 60.0), (60.0, 120.0)]
    assert plan.bands[1][2] == "low mid"


def test_bad_band_edges_are_refused() -> None:
    for spec in ([], [[0.0, 100.0]], [[100.0, 100.0], [200.0, 300.0]], "1/12"):
        with pytest.raises(ValidationError):
            parse_bands(spec)


def test_octave_aggregation_sums_power_not_decibels() -> None:
    plan = parse_bands("octave")
    levels = [DB_FLOOR] * len(BAND_CENTERS_HZ)
    # Two measured bands inside the 1 kHz octave (800, 1000, 1250 Hz) carry
    # energy; the third stays at the floor and contributes nothing.
    index_1k = BAND_CENTERS_HZ.index(1000.0)
    levels[index_1k] = -20.0
    levels[index_1k - 1] = -26.0
    result = aggregate_bands(list(BAND_CENTERS_HZ), levels, plan)
    center_index = list(result["centers_hz"]).index(1000.0)
    expected = 10.0 * math.log10(10 ** (-20.0 / 10.0) + 10 ** (-26.0 / 10.0))
    assert result["level_dbfs"][center_index] == pytest.approx(expected, abs=0.01)
    assert result["member_counts"][center_index] == 3
    assert all(value is not None for value in result["level_dbfs"]), \
        "every octave band is backed by the measured 1/3-octave grid"


def test_an_explicit_plan_aggregates_by_centre_frequency() -> None:
    plan = parse_bands([[30.0, 60.0], [60.0, 120.0]])
    levels = [-30.0 if 30.0 <= center < 60.0 else -40.0
              for center in BAND_CENTERS_HZ]
    result = aggregate_bands(list(BAND_CENTERS_HZ), levels, plan)
    power_low = sum(10 ** (-30.0 / 10.0) for center in BAND_CENTERS_HZ
                    if 30.0 <= center < 60.0)
    power_high = sum(10 ** (-40.0 / 10.0) for center in BAND_CENTERS_HZ
                     if 60.0 <= center < 120.0)
    assert result["level_dbfs"][0] == pytest.approx(10 * math.log10(power_low), abs=0.01)
    assert result["level_dbfs"][1] == pytest.approx(10 * math.log10(power_high), abs=0.01)
    # 25 Hz is outside both requested bands and is not silently folded in.
    assert result["member_counts"] == [3, 3]


def test_a_requested_band_with_no_measured_members_stays_null() -> None:
    plan = parse_bands([[16000.0, 20000.0]])
    result = aggregate_bands(list(BAND_CENTERS_HZ),
                             [DB_FLOOR] * len(BAND_CENTERS_HZ), plan)
    assert result["level_dbfs"] == [None]
    assert result["member_counts"] == [0]


def test_all_floor_members_stay_at_the_floor() -> None:
    plan = parse_bands([[30.0, 60.0]])
    result = aggregate_bands([40.0, 50.0], [DB_FLOOR, DB_FLOOR], plan)
    assert result["level_dbfs"][0] == DB_FLOOR


def _report() -> dict:
    return {
        "metrics": {"integrated_lufs": {"value": -14.25, "unit": "LUFS"},
                    "peak_dbfs": {"value": -1.5, "unit": "dBFS"},
                    "crest_db": {"value": None, "unit": "dB"}},
        "curves": {"band_centers_hz": list(BAND_CENTERS_HZ),
                   "band_level_dbfs": [-30.0] * len(BAND_CENTERS_HZ)},
    }


def test_a_cell_contains_only_the_requested_metrics() -> None:
    cell, units = build_cell(_report(), parse_metrics(["lufs", "peak"]), parse_bands(None))
    assert cell == {"integrated_lufs": -14.25, "peak_dbfs": -1.5}
    assert units["integrated_lufs"] == "LUFS"
    assert "crest_db" not in cell


def test_bands_add_the_measured_array_and_the_aggregate_on_request() -> None:
    cell, units = build_cell(_report(), parse_metrics(["lufs", "bands"]),
                             parse_bands("octave"))
    assert len(cell[BANDS_KEY]) == len(BAND_CENTERS_HZ)
    assert len(cell["band_aggregate"]["level_dbfs"]) == len(OCTAVE_CENTERS_HZ)
    assert "band_aggregate" not in build_cell(
        _report(), parse_metrics(["lufs", "bands"]), parse_bands(None))[0]
    assert units[BANDS_KEY] == "dBFS"


def test_summaries_describe_the_table_without_a_target() -> None:
    rows = [{"index": 0, "start_s": 0.0, "end_s": 1.0},
            {"index": 1, "start_s": 1.0, "end_s": 2.0}]
    columns = [{"source_id": "a"}, {"source_id": "b"}]
    cells = [{"a": {"integrated_lufs": -10.0}, "b": {"integrated_lufs": -14.0}},
             {"a": {"integrated_lufs": -12.0}, "b": {"integrated_lufs": None}}]
    summary = summarize_rows(rows, columns, cells, parse_metrics(["lufs"]))
    assert summary["rows"][0]["metrics"]["integrated_lufs"] == {
        "count": 2, "mean": -12.0, "min": -14.0, "max": -10.0}
    assert summary["columns"][0]["metrics"]["integrated_lufs"]["count"] == 2
    assert summary["columns"][1]["metrics"]["integrated_lufs"]["count"] == 1
    assert "no target" in summary["criterion"]
