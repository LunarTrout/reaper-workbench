"""Timeline-context tempo reporting from the project tempo map."""

from __future__ import annotations

from reaper_audio.analysis.timeline_context import build


def _ctx(tempo: dict) -> dict:
    return {"name": "scratch.rpp", "track_count": 1, "length_s": 3.0,
            "tracks": [], "tempo": tempo}


def test_static_project_reports_its_own_bpm_and_bars() -> None:
    ctx = _ctx({"bpm": 120.0, "numerator": 4, "denominator": 4, "markers": []})
    text = build(ctx, 0.0, 3.0)
    assert "at 120 BPM" in text
    assert "bars 0.0-1.5" in text
    assert " at 0 BPM" not in text and " 0 BPM " not in text


def test_tempo_map_uses_the_marker_active_at_the_focus_start() -> None:
    ctx = _ctx({"bpm": 120.0, "numerator": 4, "denominator": 4, "markers": [
        {"time_s": 0.0, "bpm": 90.0, "numerator": 4, "denominator": 4, "measure": 0},
        {"time_s": 2.0, "bpm": 140.0, "numerator": 4, "denominator": 4, "measure": 1},
    ]})
    text = build(ctx, 2.0, 2.5)
    assert "TEMPO MAP:" in text
    assert "90 BPM" in text and "140 BPM" in text
    assert "at 140 BPM" in text
    assert "bars 1.0-1.3" in text


def test_unknown_tempo_is_reported_as_unknown_not_zero() -> None:
    ctx = _ctx({"bpm": None, "numerator": 4, "denominator": 4, "markers": []})
    text = build(ctx, 0.0, 3.0)
    assert "at unknown tempo" in text
    assert "bars unknown" in text
    assert " at 0 BPM" not in text and " 0 BPM " not in text


def test_marker_without_bpm_falls_back_to_the_project_bpm() -> None:
    ctx = _ctx({"bpm": 100.0, "numerator": 4, "denominator": 4, "markers": [
        {"time_s": 0.0, "numerator": 4, "denominator": 4, "measure": 0},
    ]})
    text = build(ctx, 0.0, 1.0)
    assert "at 100 BPM" in text
