import copy
import json

import pytest

from reaper_audio.daw.timing_refine_actions import (
    insert_seam,
    MIN_CLIP_S, choose_rhythm_option, exclude_candidate, manual_attack,
    pin_attack, protect_interval, recompute_seam,
)
from reaper_audio.errors import ValidationError


def _plan():
    """Three clips, two seams, five attacks; 0.5 s item at 0.5 s, 2 s source."""
    return {
        "schema": "reaper-workbench.timing-plan/1",
        "method": "crossfade",
        "plan_id": "tpl_parent",
        "item_position_s": 0.5,
        "source_offset_s": 0.0,
        "item_length_s": 2.0,
        "duration_s": 2.0,
        "tolerance_s": 0.08,
        "blocks": [
            {"position_s": 0.5, "source_start_s": 0.0, "length_s": 0.6,
             "fade_in_s": 0.004, "fade_out_s": 0.008, "event_ids": ["e1", "e2"]},
            {"position_s": 1.1, "source_start_s": 0.6, "length_s": 0.7,
             "fade_in_s": 0.008, "fade_out_s": 0.008, "event_ids": ["e3", "e4"]},
            {"position_s": 1.8, "source_start_s": 1.3, "length_s": 0.7,
             "fade_in_s": 0.008, "fade_out_s": 0.004, "event_ids": ["e5"]},
        ],
        "seams": [1.1, 1.8],
        "anchors": [
            {"id": "e1", "source_s": 0.12, "project_s": 0.62, "target_s": 0.62,
             "desired_shift_s": 0.0, "achieved_shift_s": 0.0, "confidence": 0.9},
            {"id": "e2", "source_s": 0.40, "project_s": 0.90, "target_s": 0.88,
             "desired_shift_s": -0.02, "achieved_shift_s": -0.02, "confidence": 0.8},
            {"id": "e3", "source_s": 0.70, "project_s": 1.20, "target_s": 1.20,
             "desired_shift_s": 0.0, "achieved_shift_s": 0.0, "confidence": 0.95},
            {"id": "e4", "source_s": 1.00, "project_s": 1.50, "target_s": 1.50,
             "desired_shift_s": 0.0, "achieved_shift_s": 0.0, "confidence": 0.9},
            {"id": "e5", "source_s": 1.40, "project_s": 1.90, "target_s": 1.90,
             "desired_shift_s": 0.0, "achieved_shift_s": 0.0, "confidence": 0.85},
        ],
        "protected_ranges": [
            {"start_s": 1.6, "end_s": 1.7, "basis": "project_s"},
            {"start_s": 0.2, "end_s": 0.4, "basis": "source_s"},
            {"start_s": 0.35, "end_s": 0.5, "basis": "source_s"},
        ],
        "rhythm_choices": {"seg_1": "keep"},
    }


def _simple(blocks, seams, anchors, *, duration=2.0):
    return {
        "method": "crossfade", "plan_id": "tpl_simple",
        "item_position_s": 0.0, "source_offset_s": 0.0,
        "item_length_s": 2.0, "duration_s": duration, "tolerance_s": 0.046,
        "blocks": blocks, "seams": seams, "anchors": anchors,
        "protected_ranges": [], "rhythm_choices": {},
    }


def _attack(event_id, project_s, target_s=None):
    return {"id": event_id, "source_s": project_s, "project_s": project_s,
            "target_s": project_s if target_s is None else target_s,
            "desired_shift_s": 0.0, "achieved_shift_s": 0.0, "confidence": 0.9}


def test_pin_freezes_one_target_and_leaves_every_block_and_other_anchor():
    plan = _plan()
    child, changes = pin_attack(plan, "e2")

    assert changes["event_id"] == "e2"
    assert changes["previous_target_s"] == pytest.approx(0.88)
    assert changes["target_s"] == pytest.approx(0.90)
    assert changes["residual_s"] == pytest.approx(-0.02)
    assert changes["requires_replan"] is True
    assert changes["applied"] is True

    pinned = next(row for row in child["anchors"] if row["id"] == "e2")
    assert pinned["target_s"] == pytest.approx(0.90)
    assert pinned["desired_shift_s"] == 0.0
    assert pinned["pinned"] is True
    assert pinned["pin_shift_s"] == 0.0

    original = {row["id"]: row for row in plan["anchors"]}
    for row in child["anchors"]:
        if row["id"] != "e2":
            assert row == original[row["id"]]
    for before, after in zip(plan["blocks"], child["blocks"]):
        assert after == before
    assert child["seams"] == plan["seams"]
    assert child["parent_plan_id"] == "tpl_parent"
    assert child["plan_id"] != plan["plan_id"]
    assert plan["anchors"][1]["target_s"] == pytest.approx(0.88)
    assert "pinned" not in plan["anchors"][1]

    again, repeat = pin_attack(child, "e2")
    assert repeat["applied"] is False
    assert again["plan_id"] == child["plan_id"]


def test_manual_attack_gets_a_stable_id_and_joins_the_owning_block():
    plan = _plan()
    child, changes = manual_attack(plan, source_s=0.75, project_s=1.25)

    assert changes["event_id"] == "manual_1"
    assert changes["block_index"] == 1
    assert changes["event_ids_index"] == 1
    assert changes["mapping_residual_s"] == pytest.approx(0.0)
    assert child["blocks"][1]["event_ids"] == ["e3", "manual_1", "e4"]
    assert [row["id"] for row in child["anchors"]] == [
        "e1", "e2", "e3", "manual_1", "e4", "e5"]
    added = next(row for row in child["anchors"] if row["id"] == "manual_1")
    assert added["project_s"] == pytest.approx(1.25)
    assert added["target_s"] == pytest.approx(1.25)
    assert added["source_s"] == pytest.approx(0.75)
    assert added["manual"] is True
    assert child["blocks"][0] == plan["blocks"][0]
    assert child["blocks"][2] == plan["blocks"][2]

    second, again = manual_attack(child, source_s=0.3, project_s=0.8)
    assert again["event_id"] == "manual_2"
    named, named_changes = manual_attack(plan, source_s=0.3, project_s=0.8,
                                         event_id="pickup")
    assert named_changes["event_id"] == "pickup"
    assert "pickup" in named["blocks"][0]["event_ids"]
    assert second["plan_id"] != child["plan_id"]


def test_manual_attack_reports_a_mapping_deviation_and_rejects_bad_input():
    plan = _plan()
    child, changes = manual_attack(plan, source_s=0.75, project_s=1.24)
    assert changes["mapping_residual_s"] == pytest.approx(0.01)
    assert changes["reason"]
    added = next(row for row in child["anchors"] if row["id"] == "manual_1")
    assert added["target_s"] == pytest.approx(1.24)

    with pytest.raises(ValidationError, match="already used"):
        manual_attack(plan, source_s=0.75, project_s=1.25, event_id="e3")
    with pytest.raises(ValidationError, match="inside the item"):
        manual_attack(plan, source_s=2.5, project_s=2.6)
    with pytest.raises(ValidationError, match="inside the item"):
        manual_attack(plan, source_s=0.75, project_s=3.0)
    with pytest.raises(ValidationError):
        manual_attack(plan, source_s=float("nan"), project_s=1.2)


def test_exclude_keeps_the_neighbouring_attack_and_both_seams():
    plan = _plan()
    child, changes = exclude_candidate(plan, "e3", reason="false positive: breath")

    assert changes["event_id"] == "e3"
    assert changes["reason"] == "false positive: breath"
    assert changes["block_indexes"] == [1]
    assert changes["neighbours"] == ["e2", "e4"]
    assert changes["remaining_anchor_count"] == 4
    assert "e3" not in [row["id"] for row in child["anchors"]]
    for event_id in ("e2", "e4"):
        before = next(row for row in plan["anchors"] if row["id"] == event_id)
        after = next(row for row in child["anchors"] if row["id"] == event_id)
        assert after == before
    assert child["seams"] == plan["seams"]
    assert child["blocks"][0] == plan["blocks"][0]
    assert child["blocks"][2] == plan["blocks"][2]
    touched = child["blocks"][1]
    assert touched["event_ids"] == ["e4"]
    for field in ("position_s", "source_start_s", "length_s", "fade_in_s", "fade_out_s"):
        assert touched[field] == plan["blocks"][1][field]
    assert child["excluded_events"][0]["event_id"] == "e3"

    with pytest.raises(ValidationError, match="Unknown timing event"):
        exclude_candidate(plan, "missing", reason="nope")
    with pytest.raises(ValidationError, match="non-empty reason"):
        exclude_candidate(plan, "e3", reason="  ")


def test_rhythm_choice_is_recorded_without_moving_attacks():
    plan = _plan()
    child, changes = choose_rhythm_option(plan, segment_id="seg_1",
                                          option_id="swing_8")

    assert changes["segment_id"] == "seg_1"
    assert changes["option_id"] == "swing_8"
    assert changes["previous_option_id"] == "keep"
    assert changes["attacks_moved"] == 0
    assert child["rhythm_choices"] == {"seg_1": "swing_8"}
    assert plan["rhythm_choices"] == {"seg_1": "keep"}
    assert child["anchors"] == plan["anchors"]
    assert child["blocks"] == plan["blocks"]
    assert child["seams"] == plan["seams"]

    same, repeat = choose_rhythm_option(child, segment_id="seg_1",
                                        option_id="swing_8")
    assert repeat["applied"] is False
    assert same["plan_id"] == child["plan_id"]
    with pytest.raises(ValidationError):
        choose_rhythm_option(plan, segment_id="seg_1", option_id="")


def test_protect_interval_merges_overlapping_source_ranges():
    plan = _plan()
    child, changes = protect_interval(plan, start_s=0.45, end_s=0.8)

    assert changes["added"] == {"start_s": 0.45, "end_s": 0.8}
    assert changes["merged"] == [{"start_s": 0.2, "end_s": 0.8}]
    assert changes["source_ranges_before"] == [
        {"start_s": 0.2, "end_s": 0.4}, {"start_s": 0.35, "end_s": 0.5}]
    assert changes["protected_range_count_before"] == 3
    assert changes["protected_range_count_after"] == 2
    assert child["protected_ranges"][0] == plan["protected_ranges"][0]
    assert child["protected_ranges"][1] == {
        "start_s": 0.2, "end_s": 0.8, "basis": "source_s"}
    assert child["anchors"] == plan["anchors"]
    assert child["blocks"] == plan["blocks"]
    assert child["seams"] == plan["seams"]

    covered, repeat = protect_interval(child, start_s=0.3, end_s=0.6)
    assert repeat["applied"] is False
    assert repeat["already_protected"] is True
    assert covered["protected_ranges"] == child["protected_ranges"]
    assert covered["plan_id"] == child["plan_id"]

    with pytest.raises(ValidationError, match="end after it starts"):
        protect_interval(plan, start_s=0.8, end_s=0.8)
    with pytest.raises(ValidationError, match="below zero"):
        protect_interval(plan, start_s=-0.1, end_s=0.2)
    with pytest.raises(ValidationError, match="exceeds the source"):
        protect_interval(plan, start_s=0.5, end_s=2.5)


def test_protect_interval_treats_unlabelled_ranges_as_source():
    plan = _plan()
    plan["protected_ranges"] = [
        {"start_s": 0.10, "end_s": 0.30},
        {"start_s": 0.20, "end_s": 0.40},
    ]
    child, changes = protect_interval(plan, start_s=0.35, end_s=0.5)

    assert child["protected_ranges"] == [
        {"start_s": 0.10, "end_s": 0.5, "basis": "source_s"}]
    assert changes["merged"] == [{"start_s": 0.10, "end_s": 0.5}]
    assert changes["protected_range_count_after"] == 1


def test_recompute_seam_moves_one_cut_and_keeps_everything_else():
    plan = _plan()
    before = copy.deepcopy(plan)
    child, changes = recompute_seam(plan, seam_index=0, max_shift_s=0.08)

    assert changes["shift_s"] == pytest.approx(-0.05)
    assert changes["seam_before_s"] == pytest.approx(1.1)
    assert changes["seam_s"] == pytest.approx(1.05)
    assert changes["conflict"] is None
    assert changes["clamped"] is False
    assert changes["residual_s"] == pytest.approx(0.0, abs=1e-12)

    assert child["blocks"][0]["length_s"] == pytest.approx(0.55)
    assert child["blocks"][1]["position_s"] == pytest.approx(1.05)
    assert child["blocks"][1]["source_start_s"] == pytest.approx(0.55)
    assert child["blocks"][1]["length_s"] == pytest.approx(0.75)
    assert child["seams"] == pytest.approx([1.05, 1.8])
    assert child["blocks"][2] == plan["blocks"][2]
    assert child["anchors"] == plan["anchors"]
    assert child["blocks"][0]["position_s"] == pytest.approx(0.5)
    assert child["blocks"][0]["fade_in_s"] == plan["blocks"][0]["fade_in_s"]
    assert child["blocks"][0]["fade_out_s"] == plan["blocks"][0]["fade_out_s"]
    assert child["blocks"][0]["event_ids"] == plan["blocks"][0]["event_ids"]
    assert plan == before


def test_recompute_seam_keeps_geometry_outside_the_touched_window_verbatim():
    plan = _plan()
    child, changes = recompute_seam(plan, seam_index=1, max_shift_s=0.02)

    assert changes["clamped"] is True
    assert changes["shift_s"] == pytest.approx(-0.02)
    assert changes["requested_shift_s"] == pytest.approx(-0.10)
    assert changes["residual_s"] == pytest.approx(-0.08)
    assert "cap" in changes["reason"]
    assert child["blocks"][0] == plan["blocks"][0]
    assert json.dumps(child["blocks"][0], sort_keys=True) == \
        json.dumps(plan["blocks"][0], sort_keys=True)
    assert child["seams"][0] == plan["seams"][0]
    assert child["anchors"] == plan["anchors"]
    assert child["blocks"][1]["length_s"] == pytest.approx(0.68)
    assert child["blocks"][2]["position_s"] == pytest.approx(1.78)
    assert child["blocks"][2]["source_start_s"] == pytest.approx(1.28)
    assert child["blocks"][2]["length_s"] == pytest.approx(0.72)
    assert child["seams"][1] == pytest.approx(1.78)


def test_recompute_seam_handles_scored_seam_mappings():
    plan = _plan()
    plan["blocks"][0]["length_s"] = 0.61
    plan["seams"][0] = {"start_s": 1.1, "end_s": 1.11, "score": 2.5}
    child, changes = recompute_seam(plan, seam_index=0, max_shift_s=0.08)

    assert changes["shift_s"] == pytest.approx(-0.05)
    assert child["seams"][0]["start_s"] == pytest.approx(1.05)
    assert child["seams"][0]["end_s"] == pytest.approx(1.06)
    assert child["seams"][0]["score"] == 2.5
    assert child["blocks"][0]["length_s"] == pytest.approx(0.56)
    assert child["blocks"][1]["position_s"] == pytest.approx(1.05)
    assert child["seams"][1] == 1.8


def test_recompute_seam_conflict_when_the_source_runs_out():
    plan = _simple(
        [{"position_s": 0.0, "source_start_s": 0.9, "length_s": 1.1,
          "fade_in_s": 0.0, "fade_out_s": 0.008, "event_ids": ["a1"]},
         {"position_s": 1.1, "source_start_s": 1.0, "length_s": 0.9,
          "fade_in_s": 0.008, "fade_out_s": 0.0, "event_ids": ["a2"]}],
        [1.1], [_attack("a1", 0.8), _attack("a2", 1.5)])
    child, changes = recompute_seam(plan, seam_index=0, max_shift_s=0.05)

    assert changes["applied"] is False
    assert changes["shift_s"] == 0.0
    assert changes["requested_shift_s"] == pytest.approx(0.05)
    assert changes["conflict"]["code"] == "insufficient_source"
    assert changes["conflict"]["available_shift_s"] == pytest.approx(0.0)
    assert "source" in changes["conflict"]["reason"]
    assert json.loads(json.dumps(changes))["conflict"]["code"] == "insufficient_source"
    assert changes["blocks_changed"] == []
    assert child == plan
    assert child["plan_id"] == plan["plan_id"]


def test_recompute_seam_conflict_when_a_clip_would_fall_below_four_ms():
    plan = _simple(
        [{"position_s": 0.0, "source_start_s": 0.0, "length_s": 1.0,
          "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": ["a1"]},
         {"position_s": 1.0, "source_start_s": 1.0, "length_s": MIN_CLIP_S,
          "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": ["a2"]}],
        [1.0], [_attack("a1", 0.9), _attack("a2", 1.4)])
    child, changes = recompute_seam(plan, seam_index=0, max_shift_s=0.2)

    assert changes["conflict"]["code"] == "clip_too_short"
    assert changes["conflict"]["available_shift_s"] == pytest.approx(0.0)
    assert child == plan


def test_recompute_seam_conflict_when_attacks_leave_no_gap():
    plan = _simple(
        [{"position_s": 0.0, "source_start_s": 0.0, "length_s": 1.0,
          "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": ["a1"]},
         {"position_s": 1.0, "source_start_s": 1.0, "length_s": 1.0,
          "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": ["a2"]}],
        [1.0], [_attack("a1", 0.998), _attack("a2", 1.002)])
    child, changes = recompute_seam(plan, seam_index=0, max_shift_s=0.1)

    assert changes["conflict"]["code"] == "neighbouring_attack_overlap"
    assert "attacks at" in changes["conflict"]["reason"]
    assert child == plan


def test_recompute_seam_rejects_unknown_indices_and_bad_limits():
    plan = _plan()
    with pytest.raises(ValidationError, match="Unknown seam_index"):
        recompute_seam(plan, seam_index=-1, max_shift_s=0.05)
    with pytest.raises(ValidationError, match="Unknown seam_index"):
        recompute_seam(plan, seam_index=2, max_shift_s=0.05)
    with pytest.raises(ValidationError, match="integer"):
        recompute_seam(plan, seam_index=True, max_shift_s=0.05)
    with pytest.raises(ValidationError, match="greater than zero"):
        recompute_seam(plan, seam_index=0, max_shift_s=0.0)
    with pytest.raises(ValidationError, match="Unknown timing event"):
        pin_attack(plan, "missing")
    with pytest.raises(ValidationError, match="crossfade"):
        pin_attack({"method": "legacy", "blocks": [], "seams": [],
                    "anchors": []}, "e1")


def test_actions_never_mutate_the_caller_plan():
    plan = _plan()
    snapshot = copy.deepcopy(plan)
    pin_attack(plan, "e3")
    manual_attack(plan, source_s=0.75, project_s=1.25)
    exclude_candidate(plan, "e4", reason="double trigger")
    choose_rhythm_option(plan, segment_id="seg_2", option_id="straight")
    protect_interval(plan, start_s=0.6, end_s=0.9)
    recompute_seam(plan, seam_index=0, max_shift_s=0.05)
    recompute_seam(plan, seam_index=1, max_shift_s=0.05)
    assert plan == snapshot

def test_recompute_seam_slips_a_late_attack_onto_its_target():
    # The incoming clip starts at 1.0 s from source 1.0 s, so its interior
    # attack keeps a +20 ms residual until the clip slips in time.  Slipping
    # must move that attack and leave the outgoing clip's attacks alone.
    plan = _simple(
        blocks=[
            {"position_s": 0.0, "source_start_s": 0.0, "length_s": 1.0,
             "fade_in_s": 0.0, "fade_out_s": 0.004, "event_ids": ["a1"]},
            {"position_s": 1.0, "source_start_s": 1.0, "length_s": 0.9,
             "fade_in_s": 0.004, "fade_out_s": 0.0, "event_ids": ["a2"]},
        ],
        seams=[1.0],
        anchors=[_attack("a1", 0.50), _attack("a2", 1.40, 1.38)],
    )
    child, changes = recompute_seam(plan, seam_index=0, max_shift_s=0.04)

    assert changes["mode"] == "slip"
    assert changes["shift_s"] == pytest.approx(-0.020, abs=1e-6)
    moved = {row["event_id"]: row for row in changes["anchors_moved"]}
    assert moved["a2"]["project_s"] == pytest.approx(1.380, abs=1e-6)
    assert "a1" not in moved
    anchor = next(item for item in child["anchors"] if item["id"] == "a2")
    assert anchor["project_s"] == pytest.approx(1.380, abs=1e-6)
    outgoing = next(item for item in child["anchors"] if item["id"] == "a1")
    assert outgoing["project_s"] == pytest.approx(0.50)
    # The clip ends where it did, so it grew by the same 20 ms.
    assert child["blocks"][1]["length_s"] == pytest.approx(0.920, abs=1e-6)
    assert child["blocks"][0]["length_s"] == pytest.approx(0.980, abs=1e-6)
    assert plan["anchors"][1]["project_s"] == pytest.approx(1.40)


def test_recompute_seam_keeps_boundaries_when_no_attack_is_late():
    plan = _simple(
        blocks=[
            {"position_s": 0.0, "source_start_s": 0.0, "length_s": 1.0,
             "fade_in_s": 0.0, "fade_out_s": 0.004, "event_ids": ["a1"]},
            {"position_s": 1.0, "source_start_s": 1.0, "length_s": 0.9,
             "fade_in_s": 0.004, "fade_out_s": 0.0, "event_ids": ["a2"]},
        ],
        seams=[1.0],
        anchors=[_attack("a1", 0.50), _attack("a2", 1.40)],
    )
    child, changes = recompute_seam(plan, seam_index=0, max_shift_s=0.04)

    assert changes.get("mode") != "slip"
    assert changes["anchors_moved"] == []
    assert [item["project_s"] for item in child["anchors"]] == [0.50, 1.40]

def test_insert_seam_places_one_late_attack_and_keeps_its_neighbours():
    # a2 is 20 ms late inside a long clip that also holds a3.  Cutting before
    # a2 and restoring the mapping before a3 must move a2 only.
    plan = _simple(
        blocks=[
            {"position_s": 0.0, "source_start_s": 0.0, "length_s": 0.5,
             "fade_in_s": 0.0, "fade_out_s": 0.004, "event_ids": ["a1"]},
            {"position_s": 0.5, "source_start_s": 0.5, "length_s": 1.4,
             "fade_in_s": 0.004, "fade_out_s": 0.0, "event_ids": ["a2", "a3"]},
        ],
        seams=[0.5],
        anchors=[_attack("a1", 0.20), _attack("a2", 1.10, 1.08),
                 _attack("a3", 1.50)],
    )
    child, changes = insert_seam(plan, event_id="a2", lead_s=0.008,
                                 max_shift_s=0.04)

    assert changes["applied"] is True
    assert changes["shift_s"] == pytest.approx(-0.020, abs=1e-6)
    assert {row["event_id"] for row in changes["anchors_moved"]} == {"a2"}
    times = {item["id"]: item["project_s"] for item in child["anchors"]}
    assert times["a2"] == pytest.approx(1.080, abs=1e-6)
    assert times["a1"] == pytest.approx(0.20)
    assert times["a3"] == pytest.approx(1.50)
    assert len(child["blocks"]) == 4
    assert len(child["seams"]) == 3
    # The crossfade must be over before the attack starts, otherwise the attack
    # sounds in both clips at once.
    cut = changes["cut_project_s"]; shift = changes["shift_s"]
    crossfade = min(abs(shift), 0.025)
    assert cut + shift + crossfade <= 1.080 - 0.005


def test_insert_seam_geometry_satisfies_the_apply_validator():
    # apply() refuses a plan unless the timeline overlap at every seam equals
    # both fades and the source gap stays inside the allowed handle overlap.
    from reaper_audio.daw.timing_apply import MAX_SEAM_OVERLAP_S

    plan = _simple(
        blocks=[
            {"position_s": 0.0, "source_start_s": 0.0, "length_s": 0.5,
             "fade_in_s": 0.0, "fade_out_s": 0.004, "event_ids": ["a1"]},
            {"position_s": 0.5, "source_start_s": 0.5, "length_s": 1.4,
             "fade_in_s": 0.004, "fade_out_s": 0.0, "event_ids": ["a2", "a3"]},
        ],
        seams=[0.5],
        anchors=[_attack("a1", 0.20), _attack("a2", 1.10, 1.08),
                 _attack("a3", 1.50)],
    )
    child, _changes = insert_seam(plan, event_id="a2", lead_s=0.008,
                                  max_shift_s=0.04)

    blocks = sorted(child["blocks"], key=lambda block: block["position_s"])
    for previous, current in zip(blocks, blocks[1:]):
        end = previous["position_s"] + previous["length_s"]
        overlap = end - current["position_s"]
        if overlap <= 0:
            continue
        assert overlap == pytest.approx(previous["fade_out_s"], abs=1e-5)
        assert overlap == pytest.approx(current["fade_in_s"], abs=1e-5)
        gap = current["source_start_s"] - (previous["source_start_s"] + previous["length_s"])
        assert gap <= 1e-6
        assert gap >= -MAX_SEAM_OVERLAP_S - 1e-6
    for block in child["blocks"]:
        assert block["source_start_s"] + block["length_s"] <= 2.0 + 1e-6

def _riffb_plan(two_inside=False):
    """An item at 64.4252 s reading source 13.0788 s, like the real Riff B take."""
    return {
        "method": "crossfade", "plan_id": "tpl_riffb",
        "item_position_s": 64.4252, "source_offset_s": 13.0788,
        "item_length_s": 24.1258, "duration_s": 37.55, "tolerance_s": 0.046,
        "blocks": [
            {"position_s": 64.4252, "source_start_s": 13.0788, "length_s": 3.8401,
             "fade_in_s": 0.0, "fade_out_s": 0.004, "event_ids": []},
            {"position_s": 68.2653, "source_start_s": 16.9068, "length_s": 1.4544,
             "fade_in_s": 0.004, "fade_out_s": 0.011, "event_ids": []},
            # Same source sample again, but its mapping lands far outside the item.
            {"position_s": 115.9000, "source_start_s": 16.9068, "length_s": 1.4544,
             "fade_in_s": 0.004, "fade_out_s": 0.011, "event_ids": []},
        ],
        "seams": [64.4252 + 3.8401, 68.2653, 115.9000],
        "anchors": [{"id": "front", "source_s": 17.1955, "project_s": 68.5540,
                     "target_s": 68.5852, "desired_shift_s": 0.0312,
                     "achieved_shift_s": 0.0, "confidence": 1.0}],
        "protected_ranges": [], "rhythm_choices": {},
    }


def test_insert_seam_binds_the_block_that_plays_the_attack_inside_the_item():
    plan = _riffb_plan()
    child, changes = insert_seam(plan, event_id="front", lead_s=0.008, max_shift_s=0.06)

    assert changes.get("conflict") or changes.get("applied") is not False
    # The chosen block is the one at 68.2653, not the copy at 115.9.
    assert changes["cut_project_s"] < 70.0
    assert abs(changes["shift_s"]) < 0.10
    assert 64.4252 <= changes["cut_project_s"] <= 88.551


def test_insert_seam_reports_candidates_when_the_source_sample_is_ambiguous():
    plan = _riffb_plan()
    # Force both copies to map inside the item window.
    plan["blocks"][2]["position_s"] = 70.5000
    plan["seams"][2] = 70.5000
    with pytest.raises(ValidationError) as excinfo:
        insert_seam(plan, event_id="front", lead_s=0.008, max_shift_s=0.06)
    details = excinfo.value.details
    assert details["source_s"] == 17.1955
    assert len(details["candidates"]) == 2


def _gain_at(block, t):
    """Linear fade gain of a block at time t on the timeline."""
    start = block["position_s"]
    end = start + block["length_s"]
    if not (start - 1e-9 <= t <= end + 1e-9):
        return 0.0
    gain = 1.0
    fi = block.get("fade_in_s") or 0.0
    fo = block.get("fade_out_s") or 0.0
    if fi > 0.0 and t < start + fi:
        gain *= max(0.0, (t - start) / fi)
    if fo > 0.0 and t > end - fo:
        gain *= max(0.0, (end - t) / fo)
    return gain


def _audible_occurrences(blocks, source_s, threshold=0.05):
    out = []
    for block in blocks:
        src_start = block["source_start_s"]
        if src_start <= source_s <= src_start + block["length_s"]:
            t = block["position_s"] + (source_s - src_start)
            gain = _gain_at(block, t)
            if gain > threshold:
                out.append((round(t, 6), round(gain, 3)))
    return sorted(out)


def _riff_b_plan():
    """The confirmed Riff B case: the pick at 17.1955 s plays at 68.5540 s."""
    return {
        "method": "crossfade", "plan_id": "tpl_riffB_regression",
        "item_position_s": 64.4252, "source_offset_s": 13.0788,
        "item_length_s": 24.1258, "duration_s": 37.55, "tolerance_s": 0.046,
        "blocks": [
            {"position_s": 64.4252, "source_start_s": 13.0788, "length_s": 3.8401,
             "fade_in_s": 0.0, "fade_out_s": 0.004, "event_ids": []},
            {"position_s": 68.2653, "source_start_s": 16.9068, "length_s": 1.4544,
             "fade_in_s": 0.004, "fade_out_s": 0.011, "event_ids": []},
            {"position_s": 69.7197, "source_start_s": 18.3612, "length_s": 1.0000,
             "fade_in_s": 0.011, "fade_out_s": 0.004, "event_ids": []},
        ],
        "seams": [68.2653, 69.7197],
        "anchors": [
            {"id": "pick", "source_s": 17.1955, "project_s": 68.5540,
             "target_s": 68.5852, "desired_shift_s": 0.0312,
             "achieved_shift_s": 0.0, "confidence": 1.0},
            {"id": "next", "source_s": 18.4000, "project_s": 69.7585,
             "target_s": 69.7585, "desired_shift_s": 0.0,
             "achieved_shift_s": 0.0, "confidence": 0.9},
        ],
        "protected_ranges": [], "rhythm_choices": {},
    }


def test_manual_path_moves_one_pick_once_with_no_hole_and_keeps_neighbours():
    plan = _riff_b_plan()
    before = _audible_occurrences(plan["blocks"], 17.1955)
    assert len(before) == 1 and before[0][0] == pytest.approx(68.5540, abs=1e-4)

    child, _ = manual_attack(plan, source_s=17.1955, project_s=68.5540,
                             event_id="manual_pick")
    child["anchors"] = [dict(a) for a in child["anchors"]]
    next(a for a in child["anchors"] if a["id"] == "manual_pick")["target_s"] = 68.5852
    child2, changes = insert_seam(child, event_id="manual_pick", lead_s=0.008,
                                  max_shift_s=0.06)

    assert changes["applied"] is True
    assert changes["shift_s"] == pytest.approx(0.0312, abs=1e-4)

    blocks = child2["blocks"]
    assert all(block["length_s"] > 0.004 for block in blocks)

    # The pick is audible exactly once, on the target.
    after = _audible_occurrences(blocks, 17.1955)
    assert [row[0] for row in after] == [pytest.approx(68.5852, abs=1e-4)]

    # The old copy still exists in the outgoing clip but is fully faded out there.
    muted = []
    for block in blocks:
        src_start = block["source_start_s"]
        if src_start <= 17.1955 <= src_start + block["length_s"]:
            t = block["position_s"] + (17.1955 - src_start)
            if abs(t - 68.5852) > 1e-3:
                muted.append((round(t, 6), round(_gain_at(block, t), 3)))
    assert muted and all(gain == 0.0 for _, gain in muted)

    # No hole on the timeline: every adjacent pair overlaps by exactly its fades.
    ordered = sorted(blocks, key=lambda b: b["position_s"])
    for prev, cur in zip(ordered, ordered[1:]):
        overlap = prev["position_s"] + prev["length_s"] - cur["position_s"]
        if overlap <= 1e-6:
            continue
        assert overlap == pytest.approx(prev.get("fade_out_s") or 0.0, abs=1e-5)
        assert overlap == pytest.approx(cur.get("fade_in_s") or 0.0, abs=1e-5)

    # The neighbouring attack keeps its time.
    assert next(a for a in child2["anchors"] if a["id"] == "next")["project_s"] == pytest.approx(69.7585)
