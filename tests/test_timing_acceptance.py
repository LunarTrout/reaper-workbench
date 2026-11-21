from __future__ import annotations

from reaper_audio.analysis.timing_context import plan_phrase


def _events():
    return [
        {"id": "a", "phrase": "p1", "start_s": 0.50, "end_s": 0.70},
        {"id": "b", "phrase": "p1", "start_s": 0.75, "end_s": 0.95},
        {"id": "c", "phrase": "p2", "start_s": 1.50, "end_s": 1.70},
    ]


def test_same_signal_two_references_choose_different_targets() -> None:
    straight = {"a": 0.50, "b": 0.75, "c": 1.50}
    pushed = {"a": 0.56, "b": 0.81, "c": 1.50}

    one = plan_phrase(_events(), straight, phrase_key="phrase")
    two = plan_phrase(_events(), pushed, phrase_key="phrase")

    first = {row["event_id"]: row for row in one["selected"]}
    second = {row["event_id"]: row for row in two["selected"]}
    assert first["a"]["start_s"] == 0.50
    assert second["a"]["start_s"] == 0.56
    assert first["c"]["start_s"] == second["c"]["start_s"] == 1.50


def test_pinned_anchor_changes_only_its_phrase() -> None:
    targets = {"a": 0.56, "b": 0.81, "c": 1.56}
    free = plan_phrase(_events(), targets, phrase_key="phrase")
    pinned = plan_phrase(_events(), targets, phrase_key="phrase", pinned={"c"})

    free_rows = {row["event_id"]: row for row in free["selected"]}
    pinned_rows = {row["event_id"]: row for row in pinned["selected"]}
    assert free_rows["a"]["start_s"] == pinned_rows["a"]["start_s"] == 0.56
    assert pinned_rows["c"]["start_s"] == 1.50
    assert free_rows["c"]["start_s"] == 1.56


def test_preserving_duration_rejects_the_grid_hit_that_eats_the_tail() -> None:
    event = {"id": "note", "phrase": "p", "start_s": 0.50,
             "end_s": 0.80, "boundary_end_s": 0.80}
    result = plan_phrase([event], {"note": 0.55}, phrase_key="phrase",
                         preserve_duration=True)
    chosen = result["selected"][0]
    assert chosen["shift_s"] == 0.0
    assert chosen["end_s"] == 0.80
    assert any(item["code"] == "duration_preserved_by_smaller_shift"
               for item in result["issues"])


def test_vocal_syllable_moves_as_one_piece_with_its_consonant() -> None:
    syllable = {"id": "syl", "phrase": "v", "start_s": 0.10,
                "consonant_start_s": 0.10, "vowel_s": 0.16,
                "end_s": 0.40, "vowel_target_s": 0.20}
    result = plan_phrase([syllable], {}, phrase_key="phrase",
                         preserve_duration=False)
    chosen = result["selected"][0]
    assert chosen["consonant_start_s"] == 0.14
    assert chosen["vowel_s"] == 0.20
    assert chosen["end_s"] == 0.44
