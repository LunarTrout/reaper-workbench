from __future__ import annotations

from typing import Any

import pytest

from reaper_audio.daw.capture_batching import (
    DEFAULT_CAPTURE_BATCH_TARGET,
    MAX_CAPTURE_BATCH_TARGET,
    plan_capture_batches,
)
from reaper_audio.errors import ValidationError


def _track(index: int, guid: str | None = None, depth: int = 0,
           sends: int = 0, name: str = "") -> dict[str, Any]:
    return {
        "guid": guid if guid is not None else f"{{TRACK-{index}}}",
        "index": index,
        "name": name or f"track {index}",
        "folder_depth": depth,
        "sends": sends,
    }


def _digest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"fields": {"tracks": rows}}


def _row(guid: str, depth: int = 0,
         sends: list[str] | None = None,
         hardware: int = 0) -> dict[str, Any]:
    return {
        "guid": guid,
        "folder_depth": depth,
        "sends": [{"dest_guid": dest} for dest in (sends or [])],
        "hardware_outputs": [{}] * hardware,
    }


def _flat(count: int) -> list[dict[str, Any]]:
    return [_track(index) for index in range(count)]


def _guids(batches: list[tuple[str, ...]]) -> set[str]:
    return {guid for batch in batches for guid in batch}


def test_multi_track_project_batches_stay_multi_track_and_complete() -> None:
    tracks = _flat(42)
    plan = plan_capture_batches(tracks, _digest([_row(track["guid"]) for track in tracks]))
    assert plan.fallback_reason is None
    sizes = [len(batch.guids) for batch in plan.batches]
    assert len(plan.batches) == DEFAULT_CAPTURE_BATCH_TARGET
    assert all(size >= 2 for size in sizes)
    assert sum(sizes) == 42
    assert _guids([batch.guids for batch in plan.batches]) == {
        track["guid"] for track in tracks}
    indexes = [index for batch in plan.batches for index in batch.track_indexes]
    assert indexes == list(range(42))


def test_singleton_remainder_is_merged_into_a_neighbour() -> None:
    tracks = _flat(10)
    # Four target batches would greedily produce 3/3/3/1 for ten tracks.
    plan = plan_capture_batches(tracks, _digest([_row(track["guid"]) for track in tracks]),
                                target_batches=4)
    sizes = [len(batch.guids) for batch in plan.batches]
    assert sizes == [3, 3, 4]
    assert all(size >= 2 for size in sizes)


def test_two_track_project_is_never_a_one_track_batch() -> None:
    tracks = _flat(2)
    plan = plan_capture_batches(tracks, _digest([_row(track["guid"]) for track in tracks]))
    assert plan.fallback_reason is None
    assert [len(batch.guids) for batch in plan.batches] == [2]


def test_one_track_project_is_one_full_batch() -> None:
    tracks = _flat(1)
    plan = plan_capture_batches(tracks, _digest([_row(tracks[0]["guid"])]))
    assert plan.fallback_reason is None
    assert [batch.guids for batch in plan.batches] == [(tracks[0]["guid"],)]


def test_nested_folder_subtree_is_atomic() -> None:
    tracks = [
        _track(0, "{A}", depth=1),
        _track(1, "{B}", depth=1),
        _track(2, "{C}", depth=-2),
        _track(3, "{D}", depth=0),
        _track(4, "{E}", depth=1),
        _track(5, "{F}", depth=-1),
    ]
    digest = _digest([
        _row("{A}", 1), _row("{B}", 1), _row("{C}", -2),
        _row("{D}", 0), _row("{E}", 1), _row("{F}", -1),
    ])
    plan = plan_capture_batches(tracks, digest, target_batches=4)
    assert plan.fallback_reason is None
    assert plan.units == (("{A}", "{B}", "{C}"), ("{D}",), ("{E}", "{F}"))
    location = {guid: index for index, batch in enumerate(plan.batches)
                for guid in batch.guids}
    assert location["{A}"] == location["{B}"] == location["{C}"]
    assert location["{E}"] == location["{F}"]


def test_folder_and_send_grouping_keeps_subtrees_whole() -> None:
    tracks = [
        _track(0, "{OUTER}", depth=1),
        _track(1, "{INNER}", depth=1),
        _track(2, "{TONE-A}", depth=-2),
        _track(3, "{PLAIN}", depth=0),
        _track(4, "{AUX}", depth=0),
    ]
    digest = _digest([
        _row("{OUTER}", 1, ["{AUX}"]),
        _row("{INNER}", 1),
        _row("{TONE-A}", -2, ["{AUX}"]),
        _row("{PLAIN}", 0, ["{INNER}"]),
        _row("{AUX}", 0),
    ])
    plan = plan_capture_batches(tracks, digest, target_batches=3)
    assert plan.fallback_reason is None
    assert plan.units[0] == ("{OUTER}", "{INNER}", "{TONE-A}")
    location = {guid: index for index, batch in enumerate(plan.batches)
                for guid in batch.guids}
    assert location["{OUTER}"] == location["{INNER}"] == location["{TONE-A}"]
    assert plan.cross_batch_sends >= 0
    assert plan.unresolved_send_destinations == 0


def test_cross_batch_sends_are_reported_from_digest() -> None:
    tracks = _flat(4)
    guids = [track["guid"] for track in tracks]
    digest = _digest([
        _row(guids[0], sends=[guids[2], guids[1]]),
        _row(guids[1], sends=[guids[3]]),
        _row(guids[2], sends=[]),
        _row(guids[3], sends=[]),
    ])
    plan = plan_capture_batches(tracks, digest, target_batches=2)
    assert [batch.guids for batch in plan.batches] == [
        (guids[0], guids[1]), (guids[2], guids[3])]
    # 0->2 and 1->3 cross; 0->1 is internal. A send to the non-target master
    # GUID is counted separately.
    assert plan.cross_batch_sends == 2
    assert plan.master_sends == 0


def test_send_to_master_is_not_a_cross_batch_edge() -> None:
    tracks = _flat(4)
    guids = [track["guid"] for track in tracks]
    digest = _digest([
        _row(guids[0], sends=["{MASTER}"]),
        _row(guids[1]), _row(guids[2]), _row(guids[3]),
    ])
    plan = plan_capture_batches(tracks, digest, target_batches=2)
    assert plan.master_sends == 1
    assert plan.cross_batch_sends == 0


def test_unresolved_send_counts_are_kept_when_digest_rows_are_missing() -> None:
    tracks = [_track(index, sends=1) for index in range(4)]
    plan = plan_capture_batches(tracks, None, target_batches=2)
    assert plan.fallback_reason is None
    assert plan.topology_source == "snapshot_only"
    assert plan.unresolved_send_destinations == 4
    assert plan.cross_batch_sends == 0


def test_hardware_outputs_are_reported() -> None:
    tracks = _flat(4)
    guids = [track["guid"] for track in tracks]
    digest = _digest([
        _row(guids[0], hardware=1), _row(guids[1]),
        _row(guids[2]), _row(guids[3]),
    ])
    plan = plan_capture_batches(tracks, digest)
    assert plan.hardware_output_guids == (guids[0],)
    assert plan.fallback_reason is None


def test_unbalanced_or_unstable_topology_falls_back() -> None:
    close_first = [_track(0, depth=-1), _track(1, depth=0)]
    plan = plan_capture_batches(close_first, _digest([
        _row("{TRACK-0}", -1), _row("{TRACK-1}", 0)]))
    assert plan.batches == ()
    assert plan.fallback_reason == "unbalanced_folder_close"

    open_at_end = [_track(0, depth=1), _track(1, depth=0)]
    plan = plan_capture_batches(open_at_end, _digest([
        _row("{TRACK-0}", 1), _row("{TRACK-1}", 0)]))
    assert plan.batches == ()
    assert plan.fallback_reason == "unbalanced_folder_open"

    duplicate = [_track(0, "{SAME}"), _track(1, "{SAME}")]
    plan = plan_capture_batches(duplicate)
    assert plan.batches == ()
    assert plan.fallback_reason == "unstable_track_identity"

    missing = [{"index": 0, "folder_depth": 0}]
    plan = plan_capture_batches(missing)
    assert plan.batches == ()
    assert plan.fallback_reason == "unstable_track_identity"


def test_planner_does_not_depend_on_track_names() -> None:
    tracks = _flat(8)
    for index, track in enumerate(tracks):
        track["name"] = "same-name" if index % 2 else f"other-{index}"
    plan = plan_capture_batches(tracks, _digest([_row(track["guid"]) for track in tracks]))
    assert _guids([batch.guids for batch in plan.batches]) == {
        track["guid"] for track in tracks}


def test_target_batches_is_validated() -> None:
    with pytest.raises(ValidationError):
        plan_capture_batches(_flat(4), target_batches=0)
    with pytest.raises(ValidationError):
        plan_capture_batches(_flat(4), target_batches=MAX_CAPTURE_BATCH_TARGET + 1)
    with pytest.raises(ValidationError):
        plan_capture_batches(_flat(4), target_batches=True)
