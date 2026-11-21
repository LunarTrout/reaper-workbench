from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from reaper_audio.analysis.timing_seams import plan_crossfades
from reaper_audio.daw.timing_service import (
    inspect_timing_review, plan_timing_crossfades, refine_timing,
)
from reaper_audio.errors import ValidationError

TEMPO = {"bpm": 120, "numerator": 4, "denominator": 4, "markers": []}


def _events(times, position=0.):
    return [{"id": f"e{i}", "source_s": time-position, "project_s": time,
             "confidence": .97, "energy_jump": 1.2} for i, time in enumerate(times)]


def test_item_boundaries_do_not_pin_the_first_and_last_attacks():
    audio = SimpleNamespace(samples=np.zeros(16_000), sample_rate=8000)
    events = _events([.25, 1.1, 1.65])
    plan = plan_crossfades(audio, events, [], position=0., offset=0., length=2.,
                          tolerance=.046, strength=1., profile="picked",
                          explicit_targets={"e0": .227, "e1": 1.1, "e2": 1.69})
    assert plan["blocks"][0]["position_s"] == 0.
    assert plan["blocks"][0]["source_start_s"] == 0.
    assert plan["blocks"][-1]["position_s"] + plan["blocks"][-1]["length_s"] == 2.
    assert plan["anchors"][0]["achieved_shift_s"] == pytest.approx(-.023)
    assert plan["anchors"][-1]["achieved_shift_s"] == pytest.approx(.04)


@pytest.fixture
def source(tmp_path, monkeypatch):
    path = tmp_path / "take.wav"
    sf.write(path, np.zeros(16_000), 8000)
    stat = path.stat()
    item = {"guid": "item", "position_s": .5005, "length_s": 2., "start_offset_s": 0.,
            "active_take": {"guid": "take", "source": str(path)}}
    events = _events([.517, .743, .990, 1.12, 1.275, 1.49, 1.759, 1.969, 2.241], .5005)
    analysis = {
        "method": "crossfade", "project_id": "project", "track_guid": "track",
        "item_guid": "item", "take_guid": "take", "position_s": .5005,
        "source_offset_s": 0., "item_length_s": 2., "edge_fades_s": [0., 0.],
        "source_fingerprint": [stat.st_size, stat.st_mtime_ns], "sample_rate": 8000,
        "source_path": str(path), "duration_s": 2., "events": events,
        "project_window": {"start_s": .5005, "end_s": 2.5005}, "profile": "picked",
        "take_count": 1, "take_fx_count": 0,
    }
    snapshot = {"revision": {"project_id": "project"}, "tempo": TEMPO,
                "tracks": [{"guid": "track", "items": [item]}]}
    monkeypatch.setattr("reaper_audio.daw.timing_service.ANALYSES", {"analysis": analysis})
    monkeypatch.setattr("reaper_audio.daw.timing_service.PLANS", {})
    monkeypatch.setattr("reaper_audio.daw.timing_service.RHYTHMS", {})
    return snapshot


def _plan(**kwargs):
    return plan_timing_crossfades("analysis", tempo_context=TEMPO, subdivision=2,
                                 tolerance_s=.046, strength=1., **kwargs)


def test_review_includes_a_front_whose_grid_point_falls_outside_the_item(source):
    plan = _plan()
    page = inspect_timing_review(plan["plan_id"], kind="unassigned",
                                 start_s=.50, end_s=.55, limit=1)
    assert page["total"] == 1
    row = page["rows"][0]
    assert row["event_id"] == "e0"
    assert row["reason"] == "nearest_grid_outside_item"
    assert .5 in row["candidate_targets_s"]
    assert plan["review"]["state"] == "needs_review"
    assert plan["review"]["assigned_count"] + plan["review"]["unassigned_count"] == 9


def test_refine_can_assign_a_missed_front_and_keep_surrounding_blocks(source):
    plan = _plan(reference_events=[
        {"event_id": f"e{i}", "target_s": target}
        for i, target in [(1, .75), (2, 1.), (4, 1.25), (5, 1.5),
                          (6, 1.75), (7, 2.), (8, 2.25)]
    ])
    refined = refine_timing(plan["plan_id"], snapshot=source, start_s=.98, end_s=1.32,
                            reference_events=[{"event_id": "e3", "target_s": 1.125}])
    patch = refined["refinement"]
    assert patch["unchanged_outside_count"] > 0
    first, last = patch["editable_window_s"]
    for block in plan["blocks"]:
        if block["position_s"] < first or block["position_s"]+block["length_s"] > last:
            assert block in refined["blocks"]
    selected = next(a for a in refined["anchors"] if a["id"] == "e3")
    assert selected["residual_s"] == pytest.approx(0, abs=1e-7)
    assert refined["coverage_check"]["target_gap_count"] == 0
    with pytest.raises(ValidationError, match="widen"):
        refine_timing(plan["plan_id"], snapshot=source, start_s=.98, end_s=1.32,
                      reference_events=[{"event_id": "e8", "target_s": 2.25}])


def test_default_mcp_plan_creates_and_exposes_a_rhythm_map(source, monkeypatch):
    from reaper_audio import mcp_server

    session = SimpleNamespace(snapshot=lambda **_: source)
    monkeypatch.setattr(mcp_server, "_session", lambda *_: session)
    result = mcp_server.reaper_timing(action="plan", analysis_id="analysis", tolerance_s=.046)
    assert result["ok"], result
    assert result["target_kind"] == "rhythm_map"
    assert result["rhythm_id"]
    assert "blocks" not in result
    detail = mcp_server.reaper_timing(action="inspect", plan_id=result["plan_id"])
    assert len(detail["plan"]["blocks"]) == result["block_count"]
