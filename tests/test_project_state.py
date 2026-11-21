"""Tests for the one-mutable-project-state document (schema 1).

No REAPER, no bridge, no clock: every row is a plain mapping shaped like the
status/digest projections, and every timestamp is supplied by the test.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from reaper_audio.context.project_state import (
    FACT_STATUSES,
    FINGERPRINT_FIELDS,
    SCHEMA,
    ProjectState,
    StateStore,
    diff_fingerprints,
    empty_state,
    fact_key,
    fingerprint_master,
    fingerprint_routing,
    fingerprint_track,
    safe_project_key,
)
from reaper_audio.errors import ValidationError

PROJECT = "reaper:{6A1B2C3D-0000-4000-8000-000000000001}:/songs/song.rpp"
OTHER_PROJECT = "reaper:{6A1B2C3D-0000-4000-8000-000000000002}:/songs/other.rpp"

TRACK_A = "{AAAAAAAA-0000-4000-8000-00000000000A}"
TRACK_B = "{BBBBBBBB-0000-4000-8000-00000000000B}"
MASTER = "{MASTER00-0000-4000-8000-00000000000M}"


def track_row(guid: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "guid": guid,
        "volume": 1.0,
        "pan": 0.0,
        "mute": False,
        "solo": 0,
        "channel_count": 2,
        "fx": [],
        "sends": [],
        "envelopes": [],
        "items": [],
    }
    row.update(overrides)
    return row


def master_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "guid": MASTER,
        "volume": 1.0,
        "pan": 0.0,
        "mute": False,
        "solo": 0,
        "channel_count": 2,
        "fx": [],
        "sends": [],
    }
    row.update(overrides)
    return row


def send_row(source: str, destination: str, *, index: int = 0,
             volume: float = 1.0, pan: float = 0.0, mode: int = 0) -> dict[str, object]:
    return {"source_guid": source, "dest_guid": destination, "index": index,
            "volume": volume, "pan": pan, "mode": mode}


def source_fingerprint(guid: str) -> dict[str, object]:
    fields = fingerprint_track(track_row(guid))["fields"]
    return {"kind": "track", "guid": guid, "fields": fields, "sha256": "sha256:source"}


def master_fingerprint() -> dict[str, object]:
    fields = fingerprint_master(master_row())["fields"]
    return {"kind": "master", "guid": MASTER, "fields": fields, "sha256": "sha256:master"}


def routing_fingerprint() -> dict[str, object]:
    return fingerprint_routing([send_row(TRACK_A, TRACK_B)])


def add_fact(state: ProjectState, *, source_id: str, stage: str,
             start: float = 0.0, end: float = 10.0,
             metric_name: str = "lufs", metric_value: float = -14.0,
             source_guid: str | None = None,
             master: bool = True) -> dict[str, object]:
    return state.upsert_fact(
        source_id=source_id,
        stage=stage,
        window=[start, end],
        metrics={metric_name: metric_value},
        fingerprints={
            "source": source_fingerprint(source_guid) if source_guid else None,
            "master": master_fingerprint() if master else None,
            "routing": routing_fingerprint(),
        },
        revision={"counter": 7, "fingerprint": "sha256:revision"},
        measured_at="2026-01-02T03:04:05Z",
    )


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #


def test_absent_project_loads_an_empty_skeleton_without_creating_files(tmp_path: Path) -> None:
    store = StateStore(tmp_path)

    state = store.load(PROJECT)

    assert state["schema"] == SCHEMA
    assert state["project_id"] == PROJECT
    assert state["token"] == 0
    assert state["objects"] == {"tracks": {}, "master": None, "routing": None}
    assert state["facts"] == {}
    assert state["history"] == []
    assert state["changes"] == []
    assert not (tmp_path / "projects").exists()


def test_save_and_load_round_trip_is_atomic(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    state = ProjectState(store, PROJECT)
    state.upsert_objects([track_row(TRACK_A)], master_row(),
                         [send_row(TRACK_A, TRACK_B)])
    add_fact(state, source_id=f"track:{TRACK_A}", stage="via_master",
             source_guid=TRACK_A)
    token = state.state_token
    assert token > 0

    path = state.save()
    expected = store.projects_root / safe_project_key(PROJECT) / "state.json"
    assert path == expected
    assert path.is_file()
    # No temporary file survives the atomic replace.
    assert [entry.name for entry in path.parent.iterdir()] == ["state.json"]

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == state.state
    assert on_disk["schema"] == SCHEMA
    assert list(on_disk["objects"]) == ["master", "routing", "tracks"]

    reloaded = ProjectState(store, PROJECT)
    assert reloaded.state_token == token
    assert reloaded.state == state.state
    assert reloaded.facts_for() == state.facts_for()


def test_state_token_is_stable_across_saves(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    state = ProjectState(store, PROJECT)
    state.upsert_objects([track_row(TRACK_A)], master_row(),
                         [send_row(TRACK_A, TRACK_B)])
    first = state.save()
    token = state.state_token
    raw_first = first.read_text(encoding="utf-8")

    state.save()
    reloaded = ProjectState(store, PROJECT)

    assert reloaded.state_token == token
    assert first.read_text(encoding="utf-8") == raw_first


def test_foreign_or_broken_state_is_refused_not_reset(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    path = store.path_for(PROJECT)
    path.parent.mkdir(parents=True)

    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValidationError):
        store.load(PROJECT)

    path.write_text(json.dumps(empty_state(OTHER_PROJECT)), encoding="utf-8")
    with pytest.raises(ValidationError):
        store.load(PROJECT)


def test_safe_project_key_is_deterministic_and_separates_similar_ids() -> None:
    first = safe_project_key(PROJECT)
    assert first == safe_project_key(PROJECT)
    assert first != safe_project_key(PROJECT.lower())
    assert first != safe_project_key(PROJECT.replace("song.rpp", "song-2.rpp"))
    assert first != safe_project_key(OTHER_PROJECT)
    assert "/" not in first and "\\" not in first and ":" not in first


# --------------------------------------------------------------------------- #
# fingerprints
# --------------------------------------------------------------------------- #


def test_track_fingerprint_records_missing_fields_as_null() -> None:
    fingerprint = fingerprint_track({"guid": TRACK_A, "volume_db": -3.0})

    assert set(fingerprint["fields"]) == set(FINGERPRINT_FIELDS)
    fields = fingerprint["fields"]
    assert fields["volume_db"] == -3.0
    assert fields["volume"] is None
    assert fields["channel_count"] is None
    assert fields["fx"] is None
    assert fields["sends"] is None
    assert fields["envelopes"] is None
    assert fields["items"] is None
    assert fields["mute"] is None
    assert fields["solo"] is None


def test_track_fingerprint_changes_for_fx_enable_parameter_fader_and_items() -> None:
    base = track_row(
        TRACK_A,
        fx=[{"index": 0, "name": "EQ", "enabled": True, "offline": False,
             "params": [{"index": 0, "normalized": 0.5}]}],
        items=[{"guid": "item-1", "position_s": 0.0, "length_s": 4.0,
                "active_take": {"guid": "take-1", "source": "/audio/kick.wav"}}],
    )
    baseline = fingerprint_track(base)
    assert baseline == fingerprint_track(copy.deepcopy(base))

    bypassed = copy.deepcopy(base)
    bypassed["fx"][0]["enabled"] = False
    assert fingerprint_track(bypassed)["sha256"] != baseline["sha256"]

    parameter = copy.deepcopy(base)
    parameter["fx"][0]["params"][0]["normalized"] = 0.75
    assert fingerprint_track(parameter)["sha256"] != baseline["sha256"]

    fader = copy.deepcopy(base)
    fader["volume"] = 0.5
    assert fingerprint_track(fader)["sha256"] != baseline["sha256"]

    longer = copy.deepcopy(base)
    longer["items"][0]["length_s"] = 8.0
    assert fingerprint_track(longer)["sha256"] != baseline["sha256"]


def test_track_fingerprint_is_unaffected_by_an_unrelated_track() -> None:
    first = fingerprint_track(track_row(TRACK_A))
    second = fingerprint_track(track_row(TRACK_B, volume=0.25))

    assert first != second
    assert fingerprint_track(track_row(TRACK_A)) == first


def test_master_and_routing_fingerprints_change_with_their_own_fields() -> None:
    baseline = fingerprint_master(master_row())
    louder = fingerprint_master(master_row(volume=0.5))
    assert louder["sha256"] != baseline["sha256"]

    routing = fingerprint_routing([send_row(TRACK_A, TRACK_B)])
    moved = fingerprint_routing([send_row(TRACK_A, TRACK_B, volume=0.5)])
    destination = fingerprint_routing([send_row(TRACK_A, MASTER)])
    mode = fingerprint_routing([send_row(TRACK_A, TRACK_B, mode=1)])

    assert routing["sha256"] != moved["sha256"]
    assert routing["sha256"] != destination["sha256"]
    assert routing["sha256"] != mode["sha256"]
    # Flat send rows and owner rows carrying `sends` describe the same graph.
    owner_rows = [{"guid": TRACK_A, "sends": [
        {"index": 0, "dest_guid": TRACK_B, "volume": 1.0, "pan": 0.0, "mode": 0},
    ]}]
    assert fingerprint_routing(owner_rows)["sha256"] == routing["sha256"]


def test_diff_fingerprints_names_only_moved_fields() -> None:
    previous = fingerprint_track(track_row(TRACK_A))
    changed = fingerprint_track(track_row(TRACK_A, volume=0.5, mute=True))

    assert diff_fingerprints(previous, changed) == ["mute", "volume"]


# --------------------------------------------------------------------------- #
# objects
# --------------------------------------------------------------------------- #


def test_upsert_objects_reports_only_the_changed_track(tmp_path: Path) -> None:
    state = ProjectState(StateStore(tmp_path), PROJECT)
    first = state.upsert_objects(
        [track_row(TRACK_A), track_row(TRACK_B)],
        master_row(),
        [send_row(TRACK_A, TRACK_B)],
    )
    assert first["tracks"]["added"] == [TRACK_A, TRACK_B]
    assert first["changed"] == {}

    changed_a = track_row(TRACK_A, volume=0.5)
    second = state.upsert_objects(
        [changed_a, track_row(TRACK_B)],
        master_row(),
        [send_row(TRACK_A, TRACK_B)],
    )

    assert second["tracks"]["updated"] == [TRACK_A]
    assert second["tracks"]["unchanged"] == [TRACK_B]
    assert second["changed"] == {TRACK_A: ["volume"]}
    assert TRACK_B not in second["changed"]


def test_upsert_objects_changes_routing_flag_only_when_edges_move(tmp_path: Path) -> None:
    state = ProjectState(StateStore(tmp_path), PROJECT)
    state.upsert_objects([track_row(TRACK_A), track_row(TRACK_B)], master_row(),
                         [send_row(TRACK_A, TRACK_B)])

    same = state.upsert_objects(routing=[send_row(TRACK_A, TRACK_B)])
    assert same["routing"]["unchanged"] is True
    assert same["routing_changed"] is False

    moved = state.upsert_objects(routing=[send_row(TRACK_A, TRACK_B, volume=0.25)])
    assert moved["routing"]["updated"] is True
    assert moved["routing_changed"] is True


# --------------------------------------------------------------------------- #
# facts
# --------------------------------------------------------------------------- #


def test_same_key_upsert_supersedes_and_keeps_history(tmp_path: Path) -> None:
    state = ProjectState(StateStore(tmp_path), PROJECT)
    first = add_fact(state, source_id=f"track:{TRACK_A}", stage="via_master",
                     source_guid=TRACK_A, metric_value=-14.0)
    second = add_fact(state, source_id=f"track:{TRACK_A}", stage="via_master",
                      source_guid=TRACK_A, metric_value=-12.0)

    assert first["key"] == second["key"]
    assert first["id"] != second["id"]

    facts = {fact["id"]: fact for fact in state.facts_for()}
    assert facts[first["id"]]["status"] == "superseded"
    assert facts[first["id"]]["superseded_by"] == second["id"]
    assert facts[second["id"]]["status"] == "current"
    assert facts[second["id"]]["metrics"] == {"lufs": -12.0}
    assert facts[second["id"]]["supersedes"] == [first["id"]]
    assert facts[second["id"]]["overlaps"] == []

    assert len(state.state["history"]) == 1
    entry = state.state["history"][0]
    assert entry["kind"] == "fact_superseded"
    assert entry["fact_id"] == first["id"]
    assert entry["superseded_by"] == second["id"]

    assert [fact["id"] for fact in state.facts_for(status="current")] == [second["id"]]


def test_partial_overlap_keeps_both_facts_and_flags_the_new_one(tmp_path: Path) -> None:
    state = ProjectState(StateStore(tmp_path), PROJECT)
    first = add_fact(state, source_id=f"track:{TRACK_A}", stage="via_master",
                     source_guid=TRACK_A, start=0.0, end=10.0)
    second = add_fact(state, source_id=f"track:{TRACK_A}", stage="via_master",
                      source_guid=TRACK_A, start=5.0, end=15.0)

    assert first["key"] != second["key"]
    stored = {fact["id"]: fact for fact in state.facts_for()}
    assert stored[first["id"]]["status"] == "current"
    assert stored[second["id"]]["status"] == "current"
    assert stored[second["id"]]["overlaps"] == [first["key"]]
    assert stored[first["id"]]["overlaps"] == []


def test_identical_window_with_a_different_metric_set_is_not_partial(
    tmp_path: Path,
) -> None:
    state = ProjectState(StateStore(tmp_path), PROJECT)
    add_fact(state, source_id=f"track:{TRACK_A}", stage="via_master",
             source_guid=TRACK_A, metric_name="lufs")
    other = add_fact(state, source_id=f"track:{TRACK_A}", stage="via_master",
                     source_guid=TRACK_A, metric_name="true_peak")

    assert other["overlaps"] == []
    assert len(state.facts_for(status="current")) == 2


def test_fact_key_sorts_metric_names_and_normalises_the_window() -> None:
    key = fact_key("track:x", "via_master", (0, 2.5), {"b": 1, "a": 2})

    assert key == {"source_id": "track:x", "stage": "via_master",
                   "window": [0.0, 2.5], "metrics": ["a", "b"]}


def test_upsert_fact_rejects_unknown_fingerprint_names_and_bad_windows(tmp_path: Path) -> None:
    state = ProjectState(StateStore(tmp_path), PROJECT)
    with pytest.raises(ValidationError):
        state.upsert_fact(source_id="track:x", stage="via_master", window=[0, 1],
                          metrics={"lufs": -14}, fingerprints={"track": {}})
    with pytest.raises(ValidationError):
        state.upsert_fact(source_id="track:x", stage="via_master", window=[5, 1],
                          metrics={"lufs": -14})


# --------------------------------------------------------------------------- #
# stale marking
# --------------------------------------------------------------------------- #


def test_master_change_stales_via_master_and_master_facts_only(tmp_path: Path) -> None:
    state = ProjectState(StateStore(tmp_path), PROJECT)
    via_master = add_fact(state, source_id=f"track:{TRACK_A}", stage="via_master",
                          source_guid=TRACK_A)
    master_stage = add_fact(state, source_id=f"master:{MASTER}",
                            stage="master_mix_post_fx", source_guid=MASTER)
    source_stage_a = add_fact(state, source_id=f"track:{TRACK_A}",
                              stage="track_stem_post_fx", source_guid=TRACK_A)
    source_stage_b = add_fact(state, source_id=f"track:{TRACK_B}",
                              stage="track_stem_post_fx", source_guid=TRACK_B)

    stale = state.mark_stale({"master": ["fx"]})

    assert set(stale) == {via_master["id"], master_stage["id"]}
    assert "master:fx" in stale[via_master["id"]]
    assert source_stage_a["id"] not in stale
    assert source_stage_b["id"] not in stale
    statuses = {fact["id"]: fact["status"] for fact in state.facts_for()}
    assert statuses[via_master["id"]] == "stale"
    assert statuses[master_stage["id"]] == "stale"
    assert statuses[source_stage_a["id"]] == "current"
    assert statuses[source_stage_b["id"]] == "current"


def test_source_change_stales_that_source_only(tmp_path: Path) -> None:
    state = ProjectState(StateStore(tmp_path), PROJECT)
    fact_a = add_fact(state, source_id=f"track:{TRACK_A}", stage="via_master",
                      source_guid=TRACK_A)
    fact_b = add_fact(state, source_id=f"track:{TRACK_B}", stage="via_master",
                      source_guid=TRACK_B)

    stale = state.mark_stale({TRACK_A: ["fx", "volume"]})

    assert set(stale) == {fact_a["id"]}
    assert stale[fact_a["id"]] == "source:fx,volume"
    assert fact_b["id"] not in stale


def test_routing_change_stales_via_master_facts_only(tmp_path: Path) -> None:
    state = ProjectState(StateStore(tmp_path), PROJECT)
    via_master = add_fact(state, source_id=f"track:{TRACK_A}", stage="via_master",
                          source_guid=TRACK_A)
    master_stage = add_fact(state, source_id=f"master:{MASTER}",
                            stage="master_mix_post_fx", source_guid=MASTER)
    source_stage = add_fact(state, source_id=f"track:{TRACK_A}",
                            stage="track_stem_post_fx", source_guid=TRACK_A)

    stale = state.mark_stale({}, routing_changed=True)

    assert set(stale) == {via_master["id"]}
    assert stale[via_master["id"]] == "routing"
    assert master_stage["id"] not in stale
    assert source_stage["id"] not in stale


def test_source_change_matches_a_track_guid_source_id_without_fingerprints(tmp_path: Path) -> None:
    state = ProjectState(StateStore(tmp_path), PROJECT)
    fact = state.upsert_fact(source_id=f"track:{TRACK_A}", stage="via_master",
                             window=[0, 10], metrics={"lufs": -14.0})

    stale = state.mark_stale({TRACK_A: ["pan"]})

    assert stale == {fact["id"]: "source:pan"}


def test_mark_stale_is_idempotent_and_ignores_superseded_facts(tmp_path: Path) -> None:
    state = ProjectState(StateStore(tmp_path), PROJECT)
    first = add_fact(state, source_id=f"track:{TRACK_A}", stage="via_master",
                     source_guid=TRACK_A)
    second = add_fact(state, source_id=f"track:{TRACK_A}", stage="via_master",
                      source_guid=TRACK_A, metric_value=-10.0)

    assert set(state.mark_stale({TRACK_A: ["pan"]})) == {second["id"]}
    assert state.mark_stale({TRACK_A: ["pan"]}) == {}
    statuses = {fact["id"]: fact["status"] for fact in state.facts_for()}
    assert statuses[first["id"]] == "superseded"


# --------------------------------------------------------------------------- #
# change feed and queries
# --------------------------------------------------------------------------- #


def test_changes_since_returns_only_newer_entries(tmp_path: Path) -> None:
    state = ProjectState(StateStore(tmp_path), PROJECT)
    baseline = state.state_token
    state.upsert_objects([track_row(TRACK_A)], master_row(),
                         [send_row(TRACK_A, TRACK_B)])
    after_objects = state.state_token
    fact = add_fact(state, source_id=f"track:{TRACK_A}", stage="via_master",
                    source_guid=TRACK_A)

    everything = state.changes_since(baseline)
    assert everything["count"] == 2
    assert [entry["kind"] for entry in everything["changes"]] == ["objects", "fact"]
    assert [entry["seq"] for entry in everything["changes"]] == [after_objects, state.state_token]

    newer = state.changes_since(after_objects)
    assert newer["count"] == 1
    assert newer["changes"][0]["fact_id"] == fact["id"]

    assert state.changes_since(state.state_token)["changes"] == []
    assert state.changes_since(state.state_token)["token"] == state.state_token


def test_changes_since_a_future_token_reports_lost_lineage(tmp_path: Path) -> None:
    state = ProjectState(StateStore(tmp_path), PROJECT)
    state.upsert_objects([track_row(TRACK_A)])

    feed = state.changes_since(state.state_token + 5)

    assert feed["reset"] is True
    assert feed["count"] == len(state.state["changes"])


def test_facts_for_filters_and_coverage_summary_counts(tmp_path: Path) -> None:
    state = ProjectState(StateStore(tmp_path), PROJECT)
    add_fact(state, source_id=f"track:{TRACK_A}", stage="via_master",
             source_guid=TRACK_A)
    add_fact(state, source_id=f"track:{TRACK_B}", stage="via_master",
             source_guid=TRACK_B)
    state.mark_stale({TRACK_B: ["fx"]})

    assert len(state.facts_for(source_id=f"track:{TRACK_A}")) == 1
    assert state.facts_for(status="stale")[0]["source_id"] == f"track:{TRACK_B}"
    assert state.facts_for(source_id="track:missing") == []

    summary = state.coverage_summary()
    assert summary["total"] == 2
    assert summary["by_status"] == {"current": 1, "stale": 1, "superseded": 0}
    assert summary["by_object"][f"track:{TRACK_B}"]["stale"] == 1
    assert summary["objects"] == {"tracks": 0, "master": False, "routing": False}

    for status in summary["by_status"]:
        assert status in FACT_STATUSES
