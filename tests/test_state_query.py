"""Tests for the state read API — pure document queries, no REAPER, no clock.

Facts are built by the production ``ProjectState`` so the read side is pinned
against the same document the capture/measure layers write.  A handful of
tests mutate a loaded document by hand to prove the read side still derives
stale reasons and history correctly when a field is missing.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from reaper_audio.context.project_state import (
    ProjectState,
    StateStore,
    empty_state,
    fingerprint_master,
    fingerprint_routing,
    fingerprint_track,
)
from reaper_audio.context.state_query import (
    availability,
    changes_since,
    query_facts,
)
from reaper_audio.errors import ValidationError

PROJECT = "reaper:{STATE-Q-0000-4000-8000-000000000001}:/songs/state-q.rpp"
TRACK_A = "{AAAAAAAA-0000-4000-8000-00000000000A}"
TRACK_B = "{BBBBBBBB-0000-4000-8000-00000000000B}"
MASTER = "{MASTER00-0000-4000-8000-00000000000M}"

TRACK_STAGE = "selected_track_via_master_parallel_batch_post_fx"
MASTER_STAGE = "master_mix_post_fx"
STEM_STAGE = "track_stem_post_fx"


# --------------------------------------------------------------------------- #
# fixtures and builders
# --------------------------------------------------------------------------- #


def track_row(guid: str, *, volume: float = 1.0) -> dict[str, object]:
    return {
        "guid": guid,
        "volume": volume,
        "pan": 0.0,
        "mute": False,
        "solo": 0,
        "channel_count": 2,
        "fx": [],
        "sends": [],
        "envelopes": [],
        "items": [],
    }


def master_row(*, volume: float = 1.0) -> dict[str, object]:
    return {
        "guid": MASTER,
        "volume": volume,
        "pan": 0.0,
        "mute": False,
        "solo": 0,
        "channel_count": 2,
        "fx": [],
        "sends": [],
    }


def send_row(*, volume: float = 1.0) -> dict[str, object]:
    return {"source_guid": TRACK_A, "dest_guid": MASTER, "index": 0,
            "volume": volume, "pan": 0.0, "mode": 0}


def make_state(tmp_path: Path) -> ProjectState:
    """A project with objects and therefore a non-zero starting token."""
    state = ProjectState(StateStore(tmp_path / "archive"), PROJECT)
    state.upsert_objects([track_row(TRACK_A), track_row(TRACK_B)],
                         master_row(), [send_row()])
    return state


def add_fact(
    state: ProjectState,
    *,
    source: str = f"track:{TRACK_A}",
    stage: str = TRACK_STAGE,
    start: float | None = 0.0,
    end: float | None = 10.0,
    metrics: dict[str, object] | None = None,
    guid: str = TRACK_A,
    status: str = "current",
    measured_at: str = "2026-01-02T03:04:05Z",
) -> dict[str, object]:
    return state.upsert_fact(
        source_id=source,
        stage=stage,
        window=[start, end],
        metrics=dict(metrics) if metrics is not None else {"lufs": -14.0},
        fingerprints={
            "source": fingerprint_track(track_row(guid)),
            "master": fingerprint_master(master_row()),
            "routing": fingerprint_routing([send_row()]),
        },
        revision={"counter": state.state_token + 1},
        measured_at=measured_at,
        status=status,
    )


# --------------------------------------------------------------------------- #
# filters
# --------------------------------------------------------------------------- #


def test_query_filters_exactly_by_source_and_stage(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    fact_a = add_fact(state, source=f"track:{TRACK_A}", stage=TRACK_STAGE)
    fact_b = add_fact(state, source=f"track:{TRACK_B}", stage=TRACK_STAGE,
                      guid=TRACK_B)
    fact_master = add_fact(state, source=f"master:{MASTER}", stage=MASTER_STAGE,
                           guid=MASTER)

    by_source = query_facts(state.state, source_id=f"track:{TRACK_A}")
    assert [row["id"] for row in by_source] == [fact_a["id"]]

    by_stage = query_facts(state.state, stage=MASTER_STAGE)
    assert [row["id"] for row in by_stage] == [fact_master["id"]]

    both = query_facts(state.state, source_id=f"track:{TRACK_B}",
                       stage=TRACK_STAGE)
    assert [row["id"] for row in both] == [fact_b["id"]]

    assert query_facts(state.state, source_id="track:missing") == []


def test_window_filter_selects_overlaps_and_orders_exact_first(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path)
    early = add_fact(state, start=0.0, end=10.0)
    late = add_fact(state, start=5.0, end=15.0)
    exact = add_fact(state, start=5.0, end=10.0)
    disjoint = add_fact(state, start=20.0, end=30.0)

    rows = query_facts(state.state, source_id=f"track:{TRACK_A}",
                       stage=TRACK_STAGE, window=[5.0, 10.0])

    assert [row["id"] for row in rows] == [exact["id"], early["id"], late["id"]]
    assert disjoint["id"] not in {row["id"] for row in rows}
    assert query_facts(state.state, window=[40.0, 50.0]) == []
    # Closed intervals: the late window touches the requested start at 15.0.
    touching = query_facts(state.state, window=[15.0, 19.0])
    assert [row["id"] for row in touching] == [late["id"]]
    # A zero-length window is a point probe.
    point = query_facts(state.state, window=[20.0, 20.0])
    assert [row["id"] for row in point] == [disjoint["id"]]


def test_window_filter_keeps_open_ended_windows_open(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    open_start = add_fact(state, start=None, end=10.0)

    rows = query_facts(state.state, window=[5.0, 6.0])

    assert [row["id"] for row in rows] == [open_start["id"]]
    assert rows[0]["window"] == [None, 10.0]


def test_metrics_filter_intersects_the_measured_metric_set(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    lufs = add_fact(state, metrics={"lufs": -14.0}, start=0.0, end=1.0)
    peak = add_fact(state, metrics={"true_peak": -1.0}, start=0.0, end=1.0)
    both = add_fact(state, metrics={"lufs": -13.0, "true_peak": -1.2},
                    start=0.0, end=1.0)
    other = add_fact(state, metrics={"correlation": 0.5}, start=0.0, end=1.0)

    rows = query_facts(state.state, metrics=["true_peak"])

    assert {row["id"] for row in rows} == {peak["id"], both["id"]}
    assert other["id"] not in {row["id"] for row in rows}
    assert query_facts(state.state, metrics=[]) != []          # empty = no filter
    assert query_facts(state.state, metrics=["missing"]) == []
    lufs_rows = query_facts(state.state, metrics=["lufs"])
    assert {row["id"] for row in lufs_rows} == {lufs["id"], both["id"]}


def test_status_filter_separates_current_from_history(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    first = add_fact(state, metrics={"lufs": -14.0})
    second = add_fact(state, metrics={"lufs": -12.0})

    current = query_facts(state.state, status="current")
    assert [row["id"] for row in current] == [second["id"]]

    history = query_facts(state.state, status="superseded",
                         latest_per_key=False)
    assert [row["id"] for row in history] == [first["id"]]
    assert history[0]["superseded_by"] == second["id"]

    assert query_facts(state.state, status="stale") == []


# --------------------------------------------------------------------------- #
# latest-per-key and history
# --------------------------------------------------------------------------- #


def test_latest_per_key_returns_newest_and_names_the_older_records(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path)
    first = add_fact(state, metrics={"lufs": -14.0})
    second = add_fact(state, metrics={"lufs": -12.0})
    third = add_fact(state, metrics={"lufs": -10.0})

    rows = query_facts(state.state)

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == third["id"]
    assert row["values"] == {"lufs": -10.0}
    assert row["status"] == "current"
    assert row["superseded_by"] is None
    assert set(row["supersedes"]) == {first["id"], second["id"]}


def test_full_history_keeps_the_forward_supersession_links(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    first = add_fact(state, metrics={"lufs": -14.0})
    second = add_fact(state, metrics={"lufs": -12.0})
    third = add_fact(state, metrics={"lufs": -10.0})

    rows = query_facts(state.state, latest_per_key=False)

    assert [row["id"] for row in rows] == [first["id"], second["id"], third["id"]]
    assert [row["status"] for row in rows] == ["superseded", "superseded",
                                              "current"]
    assert rows[0]["superseded_by"] == second["id"]
    assert rows[1]["superseded_by"] == third["id"]
    assert rows[2]["superseded_by"] is None


def test_latest_per_key_keeps_distinct_metric_sets_apart(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    lufs = add_fact(state, metrics={"lufs": -14.0})
    peak = add_fact(state, metrics={"true_peak": -1.0})

    rows = query_facts(state.state)

    assert {row["id"] for row in rows} == {lufs["id"], peak["id"]}


# --------------------------------------------------------------------------- #
# stale reasons and values
# --------------------------------------------------------------------------- #


def test_stale_reason_is_surfaced_from_the_fact_record(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    add_fact(state, source=f"track:{TRACK_A}", guid=TRACK_A)
    state.mark_stale({TRACK_A: ["volume"]})

    rows = query_facts(state.state)

    assert len(rows) == 1
    assert rows[0]["status"] == "stale"
    assert rows[0]["stale_reason"] == "source:volume"


def test_stale_reason_falls_back_to_the_recorded_change_history(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path)
    fact = add_fact(state, source=f"track:{TRACK_A}", guid=TRACK_A)
    state.mark_stale({TRACK_A: ["volume"]})
    # Simulate a document whose fact row lost the reason but whose change log
    # still records why it went stale.
    state.state["facts"][fact["id"]]["stale_reason"] = None

    rows = query_facts(state.state)

    assert rows[0]["stale_reason"] == "source:volume"


def test_values_keep_bands_as_mappings_and_never_invent_zero(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path)
    fact = add_fact(state, metrics={
        "lufs": -14.0,
        "bands": {"band_level_dbfs": [-20.0, -18.5]},
        "unmeasured": None,
    })

    row = query_facts(state.state)[0]

    assert fact["id"] == row["id"]
    assert row["values"]["lufs"] == -14.0
    assert row["values"]["bands"] == {"band_level_dbfs": [-20.0, -18.5]}
    assert "unmeasured" not in row["values"]
    assert "unmeasured" not in json.dumps(row["values"])
    # The metric name was part of the measured set even though its value is
    # missing; the row reports the set, not a fabricated value.
    assert row["metrics_requested"] == ["bands", "lufs", "unmeasured"]


# --------------------------------------------------------------------------- #
# changes_since
# --------------------------------------------------------------------------- #


def test_changes_since_a_new_measure_lists_new_facts(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    baseline = state.state_token
    fact = add_fact(state)

    feed = changes_since(state.state, baseline)

    assert feed["state_token"] == state.state_token
    assert [row["id"] for row in feed["new_or_updated"]] == [fact["id"]]
    assert feed["now_stale"] == []
    assert feed["superseded"] == []
    assert feed["changed_objects"] == {
        "tracks": {},
        "added_tracks": [],
        "removed_tracks": [],
        "master": [],
        "routing_changed": False,
    }


def test_changes_since_a_stale_mark_reports_only_new_stale_facts(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path)
    fact = add_fact(state, source=f"track:{TRACK_A}", guid=TRACK_A)
    token = state.state_token
    state.mark_stale({TRACK_A: ["volume"]})

    feed = changes_since(state.state, token)

    assert feed["new_or_updated"] == []
    assert [row["id"] for row in feed["now_stale"]] == [fact["id"]]
    assert feed["now_stale"][0]["stale_reason"] == "source:volume"
    assert changes_since(state.state, state.state_token)["now_stale"] == []


def test_changes_since_reports_supersession_and_the_new_fact(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    baseline = state.state_token
    first = add_fact(state, metrics={"lufs": -14.0})
    token = state.state_token
    second = add_fact(state, metrics={"lufs": -12.0})

    feed = changes_since(state.state, token)

    assert [row["id"] for row in feed["new_or_updated"]] == [second["id"]]
    assert [row["id"] for row in feed["superseded"]] == [first["id"]]
    assert feed["superseded"][0]["superseded_by"] == second["id"]

    # From before both measures: the transient first record is superseded
    # history, never counted as a still-new fact.
    whole = changes_since(state.state, baseline)
    assert [row["id"] for row in whole["new_or_updated"]] == [second["id"]]
    assert [row["id"] for row in whole["superseded"]] == [first["id"]]


def test_changes_since_merges_object_fingerprint_changes(tmp_path: Path) -> None:
    state = ProjectState(StateStore(tmp_path / "archive"), PROJECT)
    baseline = state.state_token
    state.upsert_objects([track_row(TRACK_A)], master_row(), [send_row()])
    added_token = state.state_token

    added = changes_since(state.state, baseline)
    assert added["changed_objects"]["added_tracks"] == [TRACK_A]
    assert added["changed_objects"]["tracks"] == {}

    state.upsert_objects([track_row(TRACK_A, volume=0.5),
                          track_row(TRACK_B)])
    state.upsert_objects(master=master_row(volume=0.5))
    changed = changes_since(state.state, added_token)

    assert changed["changed_objects"]["tracks"] == {TRACK_A: ["volume"]}
    assert changed["changed_objects"]["added_tracks"] == [TRACK_B]
    assert changed["changed_objects"]["master"] == ["volume"]
    assert changed["changed_objects"]["routing_changed"] is False


def test_changes_since_a_future_token_reports_a_reset(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    fact = add_fact(state)

    feed = changes_since(state.state, state.state_token + 5)

    assert feed["reset"] is True
    assert [row["id"] for row in feed["new_or_updated"]] == [fact["id"]]
    assert feed["state_token"] == state.state_token


# --------------------------------------------------------------------------- #
# availability and empty state
# --------------------------------------------------------------------------- #


def test_availability_counts_statuses_and_the_token(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    superseded = add_fact(state, source=f"track:{TRACK_A}", guid=TRACK_A)
    current = add_fact(state, source=f"track:{TRACK_A}", guid=TRACK_A)
    stale = add_fact(state, source=f"track:{TRACK_B}", guid=TRACK_B)
    state.mark_stale({TRACK_B: ["fx"]})

    result = availability(state.state)

    assert result == {
        "present": True,
        "facts": 3,
        "current": 1,
        "stale": 1,
        "superseded": 1,
        "state_token": state.state_token,
    }
    # The named ids really are the rows the counts describe.
    assert query_facts(state.state, status="current",
                       latest_per_key=False)[0]["id"] == current["id"]
    assert query_facts(state.state, status="stale")[0]["id"] == stale["id"]
    assert query_facts(state.state, status="superseded")[0]["id"] == superseded["id"]


def test_availability_without_a_document_is_explicitly_absent() -> None:
    assert availability(None) == {
        "present": False,
        "facts": 0,
        "current": 0,
        "stale": 0,
        "superseded": 0,
        "state_token": None,
    }


def test_empty_state_answers_every_read_without_inventing_rows() -> None:
    state = empty_state(PROJECT)

    assert query_facts(state) == []
    assert query_facts(state, source_id="track:any",
                       status="current") == []
    assert changes_since(state, 0) == {
        "state_token": 0,
        "new_or_updated": [],
        "now_stale": [],
        "superseded": [],
        "changed_objects": {
            "tracks": {},
            "added_tracks": [],
            "removed_tracks": [],
            "master": [],
            "routing_changed": False,
        },
    }
    assert availability(state) == {
        "present": True,
        "facts": 0,
        "current": 0,
        "stale": 0,
        "superseded": 0,
        "state_token": 0,
    }


# --------------------------------------------------------------------------- #
# ordering, purity and validation
# --------------------------------------------------------------------------- #


def test_reads_work_on_a_document_reloaded_from_disk(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    add_fact(state, metrics={"lufs": -14.0})
    token = state.state_token
    add_fact(state, metrics={"lufs": -12.0})
    state.save()

    loaded = StateStore(tmp_path / "archive").load(PROJECT)

    assert query_facts(loaded) == query_facts(state.state)
    assert query_facts(loaded, latest_per_key=False) == query_facts(
        state.state, latest_per_key=False)
    feed = changes_since(loaded, token)
    assert len(feed["new_or_updated"]) == 1
    assert len(feed["superseded"]) == 1
    assert feed["state_token"] == state.state_token


def test_output_is_deterministic_json_safe_and_does_not_mutate_state(
    tmp_path: Path,
) -> None:
    state = make_state(tmp_path)
    add_fact(state, source=f"track:{TRACK_B}", stage=MASTER_STAGE, guid=TRACK_B,
             start=5.0, end=15.0)
    add_fact(state, source=f"track:{TRACK_A}", stage=STEM_STAGE, guid=TRACK_A,
             start=0.0, end=4.0)
    add_fact(state, source=f"track:{TRACK_A}", stage=TRACK_STAGE, guid=TRACK_A,
             start=2.0, end=8.0, metrics={"bands": {"band_level_dbfs": [-22.0]}})
    before = copy.deepcopy(state.state)

    rows = query_facts(state.state, latest_per_key=False)

    assert state.state == before
    assert query_facts(state.state, latest_per_key=False) == rows
    assert json.loads(json.dumps(rows)) == rows
    order = [(row["source_id"], row["stage"], row["window"][0],
              json.dumps(row["key"], sort_keys=True, separators=(",", ":")))
             for row in rows]
    assert order == sorted(order)


def test_query_rejects_malformed_filters_and_documents(tmp_path: Path) -> None:
    state = make_state(tmp_path)

    with pytest.raises(ValidationError):
        query_facts(state.state, status="unknown")
    with pytest.raises(ValidationError):
        query_facts(state.state, window=[5.0, 1.0])
    with pytest.raises(ValidationError):
        query_facts(state.state, window=[0.0, "x"])  # type: ignore[list-item]
    with pytest.raises(ValidationError):
        query_facts(state.state, metrics=[1])  # type: ignore[list-item]
    with pytest.raises(ValidationError):
        query_facts(state.state, latest_per_key="yes")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        query_facts([])  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        query_facts({"facts": []})  # type: ignore[dict-item]
    with pytest.raises(ValidationError):
        changes_since(state.state, -1)
    with pytest.raises(ValidationError):
        changes_since(state.state, True)
