from reaper_audio.analysis.edit_map_report import (build_edit_map_from_report,
                                                   tempo_points_from_report)


def _report():
    return {"sources": {"g": {
        "track_context": {"name": "guitar"},
        "curves": {
            "temporal_profile": {
                "times": [0.0, 0.1],
                "cells": [{"project_tempo": {"bpm": 120.0}},
                          {"project_tempo": {"bpm": 140.0}}],
            },
            "events_2ms": {"events": [
                {"t": 0.05, "kind": "onset", "level_db": -20.0,
                 "band": "mid", "centroid_hz": 1000.0, "dur_ms": 200.0},
            ]},
        },
    }}}


def test_tempo_points_come_from_profile_times_not_cells():
    points = tempo_points_from_report(_report())
    assert points == [{"time_s": 0.0, "bpm": 120.0},
                      {"time_s": 0.1, "bpm": 140.0}]


def test_tail_evidence_uses_measured_event_duration():
    edit_map = build_edit_map_from_report(_report(), intent="natural")
    assert edit_map["source_tail_s"]["guitar"] == 0.2
    assert edit_map["candidates"][0]["tail_s"] == 0.2
    assert "tail ≈200 ms" in edit_map["candidates"][0]["reason"]
