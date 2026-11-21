"""Acceptance tests for phrase-level timing decisions.

These tests are deliberately synthetic and deterministic.  Each one encodes a
case that a nearest-grid or per-peak snap gets wrong:

* the same measured signal must follow two different annotated reference sets;
* a phrase must move as one block instead of snapping every peak;
* close reference readings must both survive as candidates;
* a pin must freeze only the phrase that contains it;
* preserve-duration must reject a tail or pause loss and report exact ms;
* a vocal syllable must move its consonant with the vowel, not snap the start.
"""

from __future__ import annotations

import json
from typing import Any

from reaper_audio.analysis.timing_context import plan_phrase


def _event(event_id: str, phrase: str, start_s: float,
           end_s: float | None = None, **extra: object) -> dict[str, Any]:
    row: dict[str, Any] = {"id": event_id, "phrase": phrase,
                           "start_s": start_s}
    if end_s is not None:
        row["end_s"] = end_s
    row.update(extra)
    return row


def _rows(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["event_id"]: row for row in result["selected"]}


def _phrase(result: dict[str, Any], index: int = 0) -> dict[str, Any]:
    return result["phrases"][index]


def _options(phrase: dict[str, Any]) -> list[dict[str, Any]]:
    return [phrase["chosen"], *phrase["alternatives"]]


def _option_with_shift(phrase: dict[str, Any],
                       shift_s: float) -> dict[str, Any]:
    for option in _options(phrase):
        if abs(float(option["shift_s"]) - shift_s) < 1e-9:
            return option
    raise AssertionError(f"no option with shift {shift_s}: "
                         f"{[option['shift_s'] for option in _options(phrase)]}")


def _issues(result: dict[str, Any], code: str) -> list[dict[str, Any]]:
    return [issue for issue in result["issues"] if issue["code"] == code]


def test_two_reference_target_sets_choose_different_targets() -> None:
    events = [
        _event("a", "p1", 0.50, 0.70),
        _event("b", "p1", 0.75, 0.95),
        _event("c", "p2", 1.50, 1.70),
    ]
    straight = {"a": 0.50, "b": 0.75, "c": 1.50}
    pushed = {"a": 0.56, "b": 0.81, "c": 1.56}

    first = _rows(plan_phrase(events, straight))
    second = _rows(plan_phrase(events, pushed))

    assert first["a"]["start_s"] == 0.50
    assert first["b"]["start_s"] == 0.75
    assert first["c"]["start_s"] == 1.50
    # 0.56 and 0.81 are not on a 120 BPM sixteenth grid (0.125 s steps), so a
    # nearest-grid snap cannot produce the requested reference positions.
    assert second["a"]["start_s"] == 0.56
    assert second["b"]["start_s"] == 0.81
    assert second["c"]["start_s"] == 1.56
    assert second["a"]["start_s"] != first["a"]["start_s"]


def test_phrase_moves_as_one_block_not_as_independent_peak_snaps() -> None:
    events = [
        _event("a", "p", 0.50, 0.70),
        _event("b", "p", 0.75, 0.95),
        _event("c", "p", 1.00, 1.20),
    ]
    targets = {"a": 0.54, "b": 0.80, "c": 1.06}

    result = plan_phrase(events, targets)
    rows = _rows(result)
    chosen = _phrase(result)["chosen"]  # type: ignore[index]

    # Per-peak snapping would yield 0.54 / 0.80 / 1.06 and change the
    # inter-onset intervals.  The phrase keeps its measured .25 s spacing.
    assert [rows[key]["start_s"] for key in ("a", "b", "c")] == [0.55, 0.80, 1.05]
    assert rows["b"]["start_s"] - rows["a"]["start_s"] == 0.25
    assert rows["c"]["start_s"] - rows["b"]["start_s"] == 0.25
    assert chosen["shift_s"] == 0.05
    assert "phrase_consensus" in chosen["sources"]


def test_close_reference_readings_are_both_exposed_with_evidence() -> None:
    events = [
        _event("a", "p1", 0.50, 0.70),
        _event("b", "p1", 0.75, 0.95),
    ]
    target_sets = [
        {"id": "straight", "targets": {"a": 0.56, "b": 0.81}},
        {"id": "near", "targets": {"a": 0.535, "b": 0.79}},
    ]

    wide = plan_phrase(events, target_sets=target_sets, ambiguity_s=0.005)
    phrase = _phrase(wide)
    assert phrase["ambiguous"] is True
    assert phrase["needs_decision"] is True
    assert phrase["applied"] is False
    assert wide["selected"] == []
    assert [candidate["interpretation"] for candidate in phrase["candidates"]] == [
        "straight", "near"]
    assert {candidate["shift_s"] for candidate in phrase["candidates"]} == {0.06, 0.035}
    evidence = {report["interpretation"]: report
                for report in phrase["interpretations"]}
    assert evidence["straight"]["residuals_s"] == {"a": 0.0, "b": 0.0}
    assert evidence["near"]["targets"] == {"a": 0.535, "b": 0.79}
    assert evidence["near"]["residuals_s"]["b"] == 0.005
    assert wide["unresolved"][0]["phrase"] == "p1"
    assert _issues(wide, "ambiguous_grid")

    narrow = plan_phrase(events, target_sets=target_sets, ambiguity_s=0.001)
    assert narrow["phrases"][0]["ambiguous"] is False
    assert narrow["phrases"][0]["chosen"]["interpretation"] == "straight"
    assert _rows(narrow)["b"]["start_s"] == 0.81


def test_pinning_one_anchor_changes_only_its_phrase() -> None:
    events = [
        _event("a", "p1", 0.50, 0.70),
        _event("b", "p1", 0.75, 0.95),
        _event("c", "p2", 1.50, 1.70),
        _event("d", "p2", 1.75, 1.95),
        _event("e", "p3", 2.50, 2.70),
    ]
    targets = {"a": 0.56, "b": 0.81, "c": 1.56, "d": 1.81, "e": 2.56}

    free = _rows(plan_phrase(events, targets))
    pinned_result = plan_phrase(events, targets, pinned={"c"})
    pinned = _rows(pinned_result)

    for event_id in ("a", "b", "e"):
        assert pinned[event_id]["start_s"] == free[event_id]["start_s"]
    # The pinned phrase p2 is frozen as a block; the other phrases are not.
    assert pinned["c"]["start_s"] == 1.50
    assert pinned["d"]["start_s"] == 1.75
    assert free["d"]["start_s"] == 1.81
    reports = {report["phrase"]: report for report in pinned_result["phrases"]}
    assert reports["p1"]["chosen"]["shift_s"] == 0.06
    assert reports["p3"]["chosen"]["shift_s"] == 0.06
    assert reports["p2"]["resolution"] == "pinned"
    assert reports["p2"]["pinned_events"] == ["c"]


def test_pinning_under_two_readings_does_not_resolve_other_phrases() -> None:
    events = [
        _event("a", "p1", 0.50, 0.70),
        _event("b", "p1", 0.75, 0.95),
        _event("c", "p2", 1.50, 1.70),
    ]
    target_sets = [
        {"id": "straight", "targets": {"a": 0.56, "b": 0.81, "c": 1.56}},
        {"id": "near", "targets": {"a": 0.535, "b": 0.79, "c": 1.56}},
    ]

    free = plan_phrase(events, target_sets=target_sets, ambiguity_s=0.005)
    pinned = plan_phrase(events, target_sets=target_sets, ambiguity_s=0.005,
                         pinned={"c"})

    assert pinned["phrases"][0]["ambiguous"] is True
    assert ([candidate["shift_s"] for candidate in pinned["phrases"][0]["candidates"]]
            == [candidate["shift_s"] for candidate in free["phrases"][0]["candidates"]])
    assert pinned["phrases"][1]["resolution"] == "pinned"
    assert _rows(pinned)["c"]["start_s"] == 1.50


def test_an_already_passed_boundary_is_not_used_as_a_tail_cap() -> None:
    # The measured end is already beyond this boundary, so it describes a
    # different edit and cannot be a tail limit for this event.
    event = _event("note", "p", 0.50, 0.80, boundary_end_s=0.60)
    result = plan_phrase([event], {"note": 0.55})
    phrase = _phrase(result)

    assert phrase["chosen"]["shift_s"] == 0.05
    assert phrase["chosen"]["lost_tail_ms"] == 0.0
    assert not _issues(result, "duration_preserved_by_smaller_shift")


def test_preserve_duration_rejects_tail_cut_and_offers_smaller_block_leave() -> None:
    event = _event("note", "p", 0.50, 0.80, boundary_end_s=0.83)
    result = plan_phrase([event], {"note": 0.60})
    phrase = _phrase(result)
    chosen = phrase["chosen"]  # type: ignore[index]

    assert chosen["shift_s"] == 0.03
    assert chosen["kind"] == "smaller"
    assert chosen["events"][0]["end_s"] == 0.83
    assert chosen["lost_tail_ms"] == 0.0
    assert _rows(result)["note"]["end_s"] == 0.83

    grid_hit = _option_with_shift(phrase, 0.10)
    assert grid_hit["rejected"] is True
    assert "block" in grid_hit["kinds"]
    assert grid_hit["lost_tail_ms"] == 70.0
    assert any(reason["code"] == "protected_tail"
               for reason in grid_hit["reasons"])
    assert "leave" in _option_with_shift(phrase, 0.0)["kinds"]

    tail_issue = _issues(result, "protected_tail")[0]
    assert tail_issue["shift_s"] == 0.10
    assert tail_issue["lost_tail_ms"] == 70.0
    smaller_issue = _issues(result, "duration_preserved_by_smaller_shift")[0]
    assert smaller_issue["requested_shift_s"] == 0.10
    assert smaller_issue["applied_shift_s"] == 0.03
    assert smaller_issue["lost_tail_ms"] == 70.0


def test_preserve_duration_rejects_pause_loss_and_reports_exact_ms() -> None:
    event = _event("note", "p", 0.50, 0.80, pause_after_s=0.03)
    result = plan_phrase([event], {"note": 0.60})
    phrase = _phrase(result)
    chosen = phrase["chosen"]  # type: ignore[index]

    assert chosen["shift_s"] == 0.0
    assert "smaller" in chosen["kinds"]
    assert chosen["requested_shifts_s"] == [0.10]

    grid_hit = _option_with_shift(phrase, 0.10)
    assert grid_hit["rejected"] is True
    assert grid_hit["lost_pause_ms"] == 30.0
    assert grid_hit["overlap_ms"] == 70.0
    assert any(reason["code"] == "protected_pause"
               for reason in grid_hit["reasons"])

    pause_issue = _issues(result, "protected_pause")[0]
    assert pause_issue["lost_pause_ms"] == 30.0
    smaller_issue = _issues(result, "duration_preserved_by_smaller_shift")[0]
    assert smaller_issue["requested_shift_s"] == 0.10
    assert smaller_issue["applied_shift_s"] == 0.0
    assert smaller_issue["lost_pause_ms"] == 30.0


def test_preserve_duration_reports_tail_and_pause_together() -> None:
    event = _event("note", "p", 0.50, 0.80, boundary_end_s=0.83,
                   pause_after_s=0.03)
    result = plan_phrase([event], {"note": 0.60})
    phrase = _phrase(result)

    assert phrase["chosen"]["shift_s"] == 0.0
    grid_hit = _option_with_shift(phrase, 0.10)
    assert grid_hit["lost_tail_ms"] == 70.0
    assert grid_hit["lost_pause_ms"] == 30.0
    smaller = _issues(result, "duration_preserved_by_smaller_shift")[0]
    assert (smaller["lost_tail_ms"], smaller["lost_pause_ms"]) == (70.0, 30.0)


def test_preserve_duration_offers_a_reduced_block_shift_for_the_phrase() -> None:
    events = [
        _event("a", "p", 0.50, 0.80, boundary_end_s=0.83),
        _event("b", "p", 0.90, 1.10, boundary_end_s=1.13),
    ]
    result = plan_phrase(events, {"a": 0.55, "b": 0.95})
    phrase = _phrase(result)
    chosen = phrase["chosen"]  # type: ignore[index]

    assert chosen["shift_s"] == 0.03
    assert chosen["kind"] == "smaller"
    assert [row["event_id"] for row in chosen["events"]] == ["a", "b"]
    assert [row["end_s"] for row in chosen["events"]] == [0.83, 1.13]

    block_hit = _option_with_shift(phrase, 0.05)
    assert block_hit["rejected"] is True
    assert block_hit["lost_tail_ms"] == 40.0
    assert "leave" in _option_with_shift(phrase, 0.0)["kinds"]


def test_without_preserve_duration_the_grid_hit_is_used_and_loss_reported() -> None:
    event = _event("note", "p", 0.50, 0.80, boundary_end_s=0.83)
    result = plan_phrase([event], {"note": 0.60}, preserve_duration=False)
    chosen = _phrase(result)["chosen"]  # type: ignore[index]

    assert chosen["shift_s"] == 0.10
    assert chosen["rejected"] is False
    assert chosen["lost_tail_ms"] == 70.0
    assert any(reason["code"] == "tail_loss_allowed"
               for reason in chosen["reasons"])


def test_vocal_vowel_target_moves_the_syllable_and_preserves_the_consonant() -> None:
    syllable = _event("syl", "v", 0.10, 0.40,
                      consonant_start_s=0.10, vowel_s=0.16, vowel_target_s=0.21)
    result = plan_phrase([syllable], {})
    row = _rows(result)["syl"]

    # A start/nearest-grid snap would put the consonant at 0.21; moving only
    # the vowel would leave the consonant at 0.10.  The syllable moves 50 ms.
    assert row["anchor"] == "vowel"
    assert row["shift_s"] == 0.05
    assert row["start_s"] == 0.15
    assert row["consonant_start_s"] == 0.15
    assert row["vowel_s"] == 0.21
    assert row["end_s"] == 0.45
    assert row["articulation"]["consonant_lead_s"] == 0.06
    assert row["articulation"]["consonant_lead_after_s"] == 0.06
    assert row["articulation"]["consonant_preserved"] is True
    assert row["protected"] == [{
        "part": "consonant",
        "start_s": 0.15,
        "end_s": 0.21,
        "duration_s": 0.06,
        "reason": "moves_with_vowel",
        "protected": True,
    }]


def test_vocal_targets_mapping_is_read_as_the_vowel_anchor() -> None:
    syllable = _event("syl", "v", 0.10, 0.40,
                      consonant_start_s=0.10, vowel_s=0.16)
    row = _rows(plan_phrase([syllable], {"syl": 0.21}))["syl"]

    assert row["consonant_start_s"] == 0.15
    assert row["vowel_s"] == 0.21
    assert row["articulation"]["consonant_lead_after_s"] == 0.06


def test_vocal_consonant_still_moves_with_a_duration_limited_vowel() -> None:
    syllable = _event("syl", "v", 0.10, 0.40, boundary_end_s=0.42,
                      consonant_start_s=0.10, vowel_s=0.16, vowel_target_s=0.26)
    result = plan_phrase([syllable], {})
    row = _rows(result)["syl"]

    assert row["shift_s"] == 0.02
    assert row["consonant_start_s"] == 0.12
    assert row["vowel_s"] == 0.18
    assert row["end_s"] == 0.42
    assert row["articulation"]["consonant_lead_s"] == 0.06
    assert row["protected"][0]["duration_s"] == 0.06


def test_output_is_json_friendly_and_deterministic() -> None:
    events = [
        _event("a", "p1", 0.50, 0.80, boundary_end_s=0.83),
        _event("syl", "p2", 0.10, 0.40, consonant_start_s=0.10,
               vowel_s=0.16, vowel_target_s=0.21),
    ]
    calls = dict(
        target_sets=[
            {"id": "straight", "targets": {"a": 0.55}},
            {"id": "near", "targets": {"a": 0.56}},
        ],
        ambiguity_s=0.01,
    )
    first = plan_phrase(events, **calls)
    second = plan_phrase(events, **calls)

    assert json.loads(json.dumps(first)) == first
    assert second == first
