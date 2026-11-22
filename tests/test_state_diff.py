"""The cheap fingerprint diff: which object changed, which field, which fact.

These tests pin the contract the state store relies on:

* a change is reported at field level, not as one opaque "project changed";
* a fader move on one track does not invalidate another track's facts;
* master changes reach ``via_master`` facts but never ``premaster`` ones;
* routing changes reach mix and ``via_master`` facts;
* a removed source is ``source_missing``, not a silent pass;
* equal fingerprints produce no change at all;
* fields outside ``FINGERPRINT_FIELDS`` never make a fingerprint look dirty.

The module is pure: every input here is a plain dictionary.
"""

from __future__ import annotations

import copy

from reaper_audio.context.state_diff import (
    FINGERPRINT_FIELDS,
    REASON_MASTER_CHANGED,
    REASON_ROUTING_CHANGED,
    REASON_SOURCE_MISSING,
    diff_fingerprints,
    stale_fact_keys,
    stale_facts,
    state_changes,
)

TRACK_A = "{A-0001}"
TRACK_B = "{B-0002}"
MASTER = "{M-0003}"


def _fx(guid: str, *, name: str = "ReaEQ", enabled: bool = True,
        offline: bool = False, params: str = "params-1") -> dict:
    return {
        "guid": guid,
        "name": name,
        "enabled": enabled,
        "offline": offline,
        "params_sha256": params,
    }


def _track(*, fx: list[dict] | None = None, volume: float = 0.5,
           pan: float = 0.0, mute: bool = False, solo: int = 0,
           channel_count: int = 2, sends: list[dict] | None = None,
           envelopes: list[dict] | None = None,
           items: list[dict] | None = None) -> dict:
    return {
        "fx": [_fx("fx-1")] if fx is None else fx,
        "volume": volume,
        "pan": pan,
        "mute": mute,
        "solo": solo,
        "channel_count": channel_count,
        "sends": [] if sends is None else sends,
        "envelopes": [] if envelopes is None else envelopes,
        "items": [] if items is None else items,
    }


def _master(*, volume: float = 1.0) -> dict:
    return {"guid": MASTER, "fx": [], "volume": volume, "pan": 0.0,
            "mute": False, "solo": 0, "channel_count": 2, "sends": [],
            "envelopes": [], "items": []}


def _fingerprint(tracks: dict[str, dict] | None = None,
                 master: dict | None = None,
                 routing: dict | None = None) -> dict:
    result = {
        "tracks": tracks if tracks is not None else {
            TRACK_A: _track(), TRACK_B: _track()},
        "master": _master() if master is None else master,
    }
    if routing is not None:
        result["routing"] = routing
    return result


def _facts() -> list[dict]:
    return [
        {"key": "fact:pre:a", "stage": "premaster", "source_id": f"track:{TRACK_A}"},
        {"key": "fact:vm:a", "stage": "via_master", "source_id": f"track:{TRACK_A}"},
        {"key": "fact:pre:b", "stage": "premaster", "source_id": f"track:{TRACK_B}"},
        {"key": "fact:vm:b", "stage": "via_master", "source_id": f"track:{TRACK_B}"},
        {"key": "fact:master", "stage": "master", "source_id": f"master:{MASTER}"},
        {"key": "fact:external", "stage": "external_file", "source": "C:/ref/target.wav"},
    ]


# --------------------------------------------------------------------------- #
# FX
# --------------------------------------------------------------------------- #


def test_fx_reorder_is_reported_as_a_chain_reorder() -> None:
    before = _fingerprint({TRACK_A: _track(fx=[_fx("fx-1"), _fx("fx-2", name="Comp")])})
    after = _fingerprint({TRACK_A: _track(fx=[_fx("fx-2", name="Comp"), _fx("fx-1")])})

    changes = diff_fingerprints(before, after)

    assert changes["changed"] is True
    entry = changes["tracks"][TRACK_A]
    assert entry["fields"] == ["fx"]
    detail = entry["details"]["fx"]
    assert "fx_chain_reordered" in detail["reasons"]
    assert detail["order_before"] == ["guid:fx-1", "guid:fx-2"]
    assert detail["order_after"] == ["guid:fx-2", "guid:fx-1"]
    assert detail["added"] == []
    assert detail["removed"] == []


def test_fx_bypass_is_reported_without_claiming_a_parameter_change() -> None:
    before = _fingerprint({TRACK_A: _track(fx=[_fx("fx-1", enabled=True)])})
    after = _fingerprint({TRACK_A: _track(fx=[_fx("fx-1", enabled=False)])})

    detail = diff_fingerprints(before, after)["tracks"][TRACK_A]["details"]["fx"]

    assert "fx_bypass_changed" in detail["reasons"]
    assert "fx_parameters_changed" not in detail["reasons"]
    assert detail["slots"]["guid:fx-1"]["enabled"]["before"] is True
    assert detail["slots"]["guid:fx-1"]["enabled"]["after"] is False


def test_fx_parameter_hash_change_is_detected() -> None:
    before = _fingerprint({TRACK_A: _track(fx=[_fx("fx-1", params="params-1")])})
    after = _fingerprint({TRACK_A: _track(fx=[_fx("fx-1", params="params-2")])})

    detail = diff_fingerprints(before, after)["tracks"][TRACK_A]["details"]["fx"]

    assert detail["reasons"] == ["fx_parameters_changed"]
    slot = detail["slots"]["guid:fx-1"]
    assert slot["parameters"]["before"] == "params-1"
    assert slot["parameters"]["after"] == "params-2"
    assert "fx_bypass_changed" not in detail["reasons"]
    assert "fx_added" not in detail["reasons"]


def test_missing_parameter_hash_is_not_compared() -> None:
    before = _fingerprint({TRACK_A: _track(fx=[_fx("fx-1", params="params-1")])})
    without_hash = _fx("fx-1")
    without_hash.pop("params_sha256")
    after = _fingerprint({TRACK_A: _track(fx=[without_hash])})

    changes = diff_fingerprints(before, after)

    assert changes["changed"] is False
    assert TRACK_A not in changes["tracks"]


# --------------------------------------------------------------------------- #
# one field, one track
# --------------------------------------------------------------------------- #


def test_fader_change_affects_only_that_track() -> None:
    before = _fingerprint()
    after = copy.deepcopy(before)
    after["tracks"][TRACK_A]["volume"] = 0.62

    changes = diff_fingerprints(before, after)

    assert changes["changed"] is True
    assert set(changes["tracks"]) == {TRACK_A}
    assert changes["tracks"][TRACK_A]["fields"] == ["volume"]
    assert changes["tracks"][TRACK_A]["details"]["volume"]["before"] == 0.5
    assert changes["tracks"][TRACK_A]["details"]["volume"]["after"] == 0.62
    assert changes["master"]["fields"] == []
    assert changes["routing"]["changed"] is False
    assert TRACK_B in changes["unchanged_tracks"]
    assert changes["added_tracks"] == []
    assert changes["removed_tracks"] == []
    # A fader move on A invalidates its own measurements and the summed master.
    assert stale_fact_keys(_facts(), changes) == ["fact:master", "fact:pre:a", "fact:vm:a"]
    assert {row["key"]: row["reason"] for row in stale_facts(_facts(), changes)} == {
        "fact:master": "master_inputs_changed",
        "fact:pre:a": "source_changed",
        "fact:vm:a": "source_changed",
    }


def test_null_composite_is_unreadable_not_an_empty_chain() -> None:
    before = _fingerprint({TRACK_A: _track(fx=[_fx("fx-1")])})
    after = copy.deepcopy(before)
    after["tracks"][TRACK_A]["fx"] = None

    detail = diff_fingerprints(before, after)["tracks"][TRACK_A]["details"]["fx"]

    assert detail["reason"] == "unreadable"
    assert detail["before_present"] is True
    assert detail["after_present"] is True
    assert "fx_removed" not in detail


def test_unknown_fields_are_ignored_deterministically() -> None:
    before = _fingerprint()
    after = copy.deepcopy(before)
    after["tracks"][TRACK_A]["colour"] = "red"
    after["tracks"][TRACK_A]["name"] = "renamed but not compared"
    after["tracks"][TRACK_A]["some_future_metric"] = 42
    after["master"]["notes"] = "a comment"
    # Reverse insertion order: the diff must not depend on dict order.
    reordered = {key: after[key] for key in reversed(list(after))}

    assert diff_fingerprints(before, after)["changed"] is False
    assert diff_fingerprints(before, reordered)["changed"] is False


def test_missing_field_is_not_null() -> None:
    before = _fingerprint({TRACK_A: _track()})
    after = copy.deepcopy(before)
    after["tracks"][TRACK_A].pop("volume")
    after["tracks"][TRACK_A]["pan"] = None

    changes = diff_fingerprints(before, after)
    fields = changes["tracks"][TRACK_A]["fields"]

    assert fields == ["volume", "pan"]
    volume = changes["tracks"][TRACK_A]["details"]["volume"]
    assert volume["reason"] == "missing_after"
    assert volume["before_present"] is True
    assert volume["after_present"] is False
    pan = changes["tracks"][TRACK_A]["details"]["pan"]
    assert pan["reason"] == "value_changed"
    assert pan["before_present"] is True
    assert pan["after_present"] is True
    assert pan["after"] is None


# --------------------------------------------------------------------------- #
# master and routing dependencies
# --------------------------------------------------------------------------- #


def test_master_change_stales_via_master_and_master_facts_only() -> None:
    before = _fingerprint()
    after = _fingerprint(master=_master(volume=0.8))

    changes = diff_fingerprints(before, after)
    stale = stale_facts(_facts(), changes)

    assert changes["master"]["fields"] == ["volume"]
    assert {row["key"] for row in stale} == {
        "fact:vm:a", "fact:vm:b", "fact:master"}
    assert {row["reason"] for row in stale} == {REASON_MASTER_CHANGED}
    assert [row["key"] for row in stale] == sorted(row["key"] for row in stale)


def test_routing_change_stales_master_and_via_master_facts() -> None:
    before = _fingerprint()
    after = copy.deepcopy(before)
    after["tracks"][TRACK_B]["sends"] = [
        {"index": 0, "dest_guid": "{BUS}", "dest_name": "BUS", "volume": 0.5,
         "mode": 0, "mute": False},
    ]

    changes = diff_fingerprints(before, after)
    stale = stale_facts(_facts(), changes)

    assert changes["routing"]["changed"] is True
    assert changes["routing"]["fields"] == ["sends"]
    assert changes["routing"]["details"]["sends"]["sources"] == [TRACK_B]
    assert set(changes["tracks"]) == {TRACK_B}
    assert changes["tracks"][TRACK_B]["fields"] == ["sends"]
    assert {row["key"]: row["reason"] for row in stale} == {
        "fact:master": REASON_ROUTING_CHANGED,
        "fact:vm:a": REASON_ROUTING_CHANGED,
        "fact:vm:b": REASON_ROUTING_CHANGED,
    }
    assert "fact:pre:a" not in {row["key"] for row in stale}
    # B's own premaster signal did not change: only its outgoing send did.
    assert "fact:pre:b" not in {row["key"] for row in stale}


def test_capture_stage_names_are_classified_structurally() -> None:
    facts = [
        {"key": "via:long", "source_id": f"track:{TRACK_A}",
         "stage": "selected_track_via_master_parallel_batch_post_fx"},
        {"key": "via:short", "source_id": f"track:{TRACK_A}", "stage": "via_master"},
        {"key": "master:mix", "source_id": f"master:{MASTER}",
         "stage": "master_mix_post_fx"},
        {"key": "master:short", "source_id": f"master:{MASTER}", "stage": "master"},
        {"key": "pre:long", "source_id": f"track:{TRACK_A}", "stage": "premaster"},
        {"key": "no:stage", "source_id": f"track:{TRACK_A}"},
    ]
    master_change = diff_fingerprints(
        _fingerprint(), _fingerprint(master=_master(volume=0.9)))
    routing_change = diff_fingerprints(
        _fingerprint(),
        _fingerprint({TRACK_A: _track(), TRACK_B: _track(sends=[
            {"index": 0, "dest_guid": "{BUS}", "volume": 0.5}])}),
    )

    assert stale_fact_keys(facts, master_change) == [
        "master:mix", "master:short", "no:stage", "via:long", "via:short"]
    # Routing reaches the summed master and via-master forms.
    assert stale_fact_keys(facts, routing_change) == [
        "master:mix", "master:short", "no:stage", "via:long", "via:short"]


def test_removed_track_marks_its_facts_source_missing() -> None:
    before = _fingerprint()
    after = _fingerprint({TRACK_A: _track()})

    changes = diff_fingerprints(before, after)
    stale = stale_facts(_facts(), changes)

    assert changes["removed_tracks"] == [TRACK_B]
    assert changes["added_tracks"] == []
    assert TRACK_B not in changes["tracks"]
    assert {row["key"]: row["reason"] for row in stale} == {
        "fact:master": "master_inputs_changed",
        "fact:pre:b": REASON_SOURCE_MISSING,
        "fact:vm:b": REASON_SOURCE_MISSING,
    }
    assert stale_fact_keys(_facts(), changes) == ["fact:master", "fact:pre:b", "fact:vm:b"]
    assert "fact:vm:a" not in stale_fact_keys(_facts(), changes)


# --------------------------------------------------------------------------- #
# nothing changed
# --------------------------------------------------------------------------- #


def test_identical_fingerprints_produce_no_changes() -> None:
    before = _fingerprint()
    after = copy.deepcopy(before)

    changes = diff_fingerprints(before, after)

    assert changes["changed"] is False
    assert changes["tracks"] == {}
    assert changes["master"] == {"fields": [], "details": {}}
    assert changes["routing"] == {"changed": False, "fields": [], "details": {}}
    assert changes["added_tracks"] == []
    assert changes["removed_tracks"] == []
    assert set(changes["unchanged_tracks"]) == {TRACK_A, TRACK_B}
    assert stale_fact_keys(_facts(), changes) == []


# --------------------------------------------------------------------------- #
# other fingerprint fields
# --------------------------------------------------------------------------- #


def test_envelope_and_item_changes_are_field_level() -> None:
    before = _fingerprint({TRACK_A: _track(
        envelopes=[{"guid": "env-1", "name": "Volume", "sha256": "a" * 64,
                    "point_count": 4}])})
    after = _fingerprint({TRACK_A: _track(
        envelopes=[{"guid": "env-1", "name": "Volume", "sha256": "b" * 64,
                    "point_count": 5}],
        items=[{"guid": "item-1", "position_s": 0.0, "length_s": 4.0}])})

    changes = diff_fingerprints(before, after)
    entry = changes["tracks"][TRACK_A]

    assert entry["fields"] == ["envelopes", "items"]
    assert "envelope_changed" in entry["details"]["envelopes"]["reasons"]
    assert "item_added" in entry["details"]["items"]["reasons"]


def test_flat_object_map_is_accepted() -> None:
    before = {f"track:{TRACK_A}": _track(), "master": _master()}
    after = copy.deepcopy(before)
    after[f"track:{TRACK_A}"]["solo"] = 1

    changes = diff_fingerprints(before, after)

    assert changes["tracks"][TRACK_A]["fields"] == ["solo"]


def test_fingerprint_field_list_is_the_public_contract() -> None:
    assert FINGERPRINT_FIELDS == (
        "fx", "volume", "pan", "mute", "solo", "sends", "envelopes",
        "items", "channel_count")


# --------------------------------------------------------------------------- #
# convenience wrapper
# --------------------------------------------------------------------------- #


def test_state_changes_wraps_diff_and_stale_facts() -> None:
    before = _fingerprint()
    after = _fingerprint(master=_master(volume=0.7))

    result = state_changes(
        {"fingerprint": before, "facts": _facts()},
        {"fingerprint": after},
    )

    assert set(result) == {"changed", "objects", "stale_facts"}
    assert result["changed"] is True
    assert result["objects"]["master"]["fields"] == ["volume"]
    assert set(result["objects"]) == {
        "tracks", "master", "routing", "added_tracks", "removed_tracks",
        "unchanged_tracks", "master_guid"}
    assert {row["key"] for row in result["stale_facts"]} == {
        "fact:vm:a", "fact:vm:b", "fact:master"}
    assert all(row["reason"] == REASON_MASTER_CHANGED
               for row in result["stale_facts"])
