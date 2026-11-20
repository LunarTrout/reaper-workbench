"""Intervals are data: explicit windows, project regions, or the bar grid."""

from __future__ import annotations

import math

import pytest

from reaper_audio.daw.measure_intervals import (bar_boundaries, bar_intervals,
                                                overlapping_pairs,
                                                parse_intervals, region_intervals,
                                                tempo_segments, union_span)
from reaper_audio.errors import ValidationError


def tempo(bpm: float, *, numerator: int = 4, denominator: int = 4,
          markers: list[dict] | None = None) -> dict:
    return {"bpm": bpm, "numerator": numerator, "denominator": denominator,
            "markers": list(markers or [])}


def test_all_and_explicit_windows_are_kept_as_given() -> None:
    everything = parse_intervals("all", project_length_s=30.0)
    assert [(row.start_s, row.end_s) for row in everything] == [(0.0, 30.0)]

    explicit = parse_intervals([[2.0, 6.0], {"start_s": 5.0, "end_s": 9.0,
                                             "label": "second"}],
                               project_length_s=30.0)
    assert [(row.start_s, row.end_s) for row in explicit] == [(2.0, 6.0), (5.0, 9.0)]
    assert explicit[1].label == "second"
    assert explicit[0].origin == "explicit"


def test_an_interval_beyond_the_project_is_refused() -> None:
    with pytest.raises(ValidationError) as error:
        parse_intervals([[0.0, 31.0]], project_length_s=30.0)
    assert "project length" in error.value.message
    assert error.value.details["project_length_s"] == 30.0


def test_overlaps_are_reported_as_facts_not_merged() -> None:
    rows = parse_intervals([[0.0, 6.0], [4.0, 10.0], [10.0, 12.0]],
                           project_length_s=30.0)
    assert len(rows) == 3
    pairs = overlapping_pairs(rows)
    assert [(pair["a"], pair["b"]) for pair in pairs] == [(0, 1)]
    assert pairs[0]["shared_audio_s"] == 2.0
    assert pairs[0]["a_interval"] == {"start_s": 0.0, "end_s": 6.0}
    assert pairs[0]["b_interval"] == {"start_s": 4.0, "end_s": 10.0}
    assert union_span(rows) == (0.0, 12.0)


def test_regions_are_used_in_project_order() -> None:
    rows = parse_intervals(
        "regions", project_length_s=30.0,
        regions=[{"position_s": 10.0, "end_s": 20.0, "name": "B"},
                 {"position_s": 0.0, "end_s": 10.0, "name": "A"}],
        markers=[])
    assert [(row.start_s, row.end_s, row.label) for row in rows] == [
        (10.0, 20.0, "B"), (0.0, 10.0, "A")]
    assert {row.origin for row in rows} == {"region"}


def test_markers_become_spans_only_when_no_region_exists() -> None:
    rows = parse_intervals("regions", project_length_s=20.0, regions=[],
                           markers=[{"position_s": 0.0}, {"position_s": 7.5},
                                    {"position_s": 15.0}])
    assert [(row.start_s, row.end_s) for row in rows] == [
        (0.0, 7.5), (7.5, 15.0), (15.0, 20.0)]
    assert {row.origin for row in rows} == {"marker_span"}


def test_no_regions_and_no_markers_is_a_clear_error_not_an_empty_table() -> None:
    with pytest.raises(ValidationError) as error:
        region_intervals([], [], project_length_s=20.0)
    message = error.value.message.lower()
    assert "region" in message and "marker" in message
    assert "bars:" in error.value.details["next_action"]
    assert error.value.details["regions"] == 0


def test_bar_grid_follows_the_tempo_map() -> None:
    # 120 BPM in 4/4 is one bar every 2 s; at 8 s the tempo halves.
    chart = tempo(120.0, markers=[{"time_s": 8.0, "bpm": 60.0,
                                   "numerator": 4, "denominator": 4,
                                   "linear": False}])
    boundaries = bar_boundaries(chart, project_length_s=24.0)
    assert boundaries == pytest.approx([0.0, 2.0, 4.0, 6.0, 8.0, 12.0, 16.0, 20.0, 24.0])

    cells = bar_intervals(chart, project_length_s=24.0, bars_per_cell=2)
    assert [(row.start_s, row.end_s) for row in cells] == [
        (0.0, 4.0), (4.0, 8.0), (8.0, 16.0), (16.0, 24.0)]
    assert cells[0].label == "bars 1-2"


def test_bar_length_uses_the_time_signature() -> None:
    chart = tempo(120.0, numerator=3, denominator=4)
    # Three quarter notes at 120 BPM = 1.5 s per bar.
    assert bar_boundaries(chart, project_length_s=6.0) == pytest.approx(
        [0.0, 1.5, 3.0, 4.5, 6.0])


def test_a_linear_tempo_ramp_is_integrated_not_stepped() -> None:
    chart = tempo(120.0, markers=[{"time_s": 0.0, "bpm": 120.0, "linear": True,
                                   "numerator": 4, "denominator": 4},
                                  {"time_s": 60.0, "bpm": 60.0,
                                   "numerator": 4, "denominator": 4}])
    segments = tempo_segments(chart, project_length_s=60.0)
    assert segments[0].linear is True
    # Quarter notes elapsed over the ramp: integral of (120 + (60-120)*t/60)/60.
    assert segments[0].quarters_between(0.0, 60.0) == pytest.approx(90.0)

    # First bar line solves 120*t - 0.5*t^2 = 4*60.
    expected_first = (120.0 - math.sqrt(120.0 ** 2 - 2.0 * 240.0))
    boundaries = bar_boundaries(chart, project_length_s=60.0)
    assert boundaries[1] == pytest.approx(expected_first, abs=1e-6)
    assert boundaries == sorted(boundaries)
    assert boundaries[-1] == pytest.approx(60.0)


def test_a_time_signature_only_marker_inherits_the_tempo() -> None:
    chart = tempo(90.0, markers=[{"time_s": 4.0, "bpm": 0.0, "numerator": 2,
                                  "denominator": 4, "linear": False}])
    segments = tempo_segments(chart, project_length_s=12.0)
    assert segments[0].bpm0 == pytest.approx(90.0)
    assert segments[-1].numerator == 2


def test_bars_spec_requires_a_positive_count() -> None:
    for spec in ("bars:0", "bars:", "bars:x"):
        with pytest.raises(ValidationError):
            parse_intervals(spec, project_length_s=10.0, tempo=tempo(120.0))


def test_a_tempo_map_without_a_base_tempo_is_refused() -> None:
    with pytest.raises(ValidationError):
        bar_boundaries({"bpm": 0, "numerator": 4, "denominator": 4, "markers": []},
                       project_length_s=10.0)
