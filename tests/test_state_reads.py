"""State-backed status evidence, freshness and coverage — no REAPER, no clock.

Every fact and fingerprint here is built by the production state store, so the
tests pin the read side against the same document the capture layer writes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reaper_audio.context.coverage import coverage_report
from reaper_audio.context.freshness import build_freshness
from reaper_audio.context.project_state import (
    ProjectState,
    StateStore,
    fingerprint_master,
    fingerprint_routing,
    fingerprint_track,
)
from reaper_audio.context.state_reads import (
    read_state,
    state_block,
    state_coverage,
    state_evidence_section,
    state_freshness,
)
from reaper_audio.context.status import build_status
from reaper_audio.errors import ValidationError

PROJECT = "reaper:{STATE-L-0000-4000-8000-000000000001}:/songs/state-l.rpp"
TRACK_A = "{AAAAAAAA-0000-4000-8000-00000000000A}"
TRACK_B = "{BBBBBBBB-0000-4000-8000-00000000000B}"
MASTER = "{MASTER00-0000-4000-8000-00000000000M}"

TRACK_STAGE = "selected_track_via_master_parallel_batch_post_fx"
MASTER_STAGE = "master_mix_post_fx"


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


def live_read(*, volume: float = 1.0,
              track: dict[str, object] | None = None) -> dict[str, object]:
    """A live project read in the shape a digest/status caller would supply."""
    return {
        "tracks": {TRACK_A: track if track is not None else track_row(TRACK_A,
                                                                     volume=volume)},
        "master": master_row(),
        "routing": [send_row()],
    }


def add_track_fact(state: ProjectState, *, start: float = 0.0, end: float = 10.0,
                   volume: float = 1.0,
                   metric_value: float = -14.0) -> dict[str, object]:
    return state.upsert_fact(
        source_id=f"track:{TRACK_A}",
        stage=TRACK_STAGE,
        window=[start, end],
        metrics={"lufs": metric_value},
        fingerprints={
            "source": fingerprint_track(track_row(TRACK_A, volume=volume)),
            "master": fingerprint_master(master_row()),
            "routing": fingerprint_routing([send_row()]),
        },
        revision={"change_count": 7},
        measured_at="2026-01-02T03:04:05Z",
    )


def add_master_fact(state: ProjectState, *, start: float = 0.0,
                    end: float = 10.0) -> dict[str, object]:
    return state.upsert_fact(
        source_id=f"master:{MASTER}",
        stage=MASTER_STAGE,
        window=[start, end],
        metrics={"lufs": -12.0},
        fingerprints={"master": fingerprint_master(master_row())},
        revision={"change_count": 7},
        measured_at="2026-01-02T03:04:06Z",
    )


def make_state(tmp_path: Path) -> ProjectState:
    state = ProjectState(StateStore(tmp_path / "archive"), PROJECT)
    state.upsert_objects([track_row(TRACK_A)], master_row(), [send_row()])
    add_track_fact(state)
    add_master_fact(state)
    return state


# --------------------------------------------------------------------------- #
# evidence
# --------------------------------------------------------------------------- #


def test_state_evidence_counts_fact_statuses(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    section = state_evidence_section(state.state)

    assert section["state"] == "current"
    assert section["reason"] is None
    assert section["units_total"] == 2
    assert section["units_current"] == 2
    assert section["units_stale"] == 0
    assert section["units_superseded"] == 0
    assert section["counts"] == {"current": 2, "stale": 0, "superseded": 0}
    assert section["state_token"] == state.state_token


def test_state_evidence_full_reports_groups_and_stale_rows(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    state.mark_stale({TRACK_A: ["volume"]})
    section = state_evidence_section(state.state, detail="full")

    assert section["state"] == "stale"
    assert section["reason"] == "facts_stale"
    assert section["units_stale"] == 2
    assert section["by_source"][f"track:{TRACK_A}"]["stale"] == 1
    assert section["by_stage"][TRACK_STAGE]["stale"] == 1
    assert {row["source_id"] for row in section["stale_facts"]} == {
        f"track:{TRACK_A}", f"master:{MASTER}"}


def test_status_uses_state_evidence_and_adds_a_state_block(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    legacy = {"state": "stale", "units_total": 99}

    result = build_status(
        context={"tracks": [], "revision": {"project_id": PROJECT}},
        detail="full", sections=["evidence"], evidence=legacy,
        state=state.state, state_since=0,
    )

    evidence = result["evidence"]
    assert evidence["units_total"] == 2  # state wins over the legacy answer
    assert evidence["counts"]["current"] == 2
    block = result["state"]
    assert block["present"] is True
    assert block["state_token"] == state.state_token
    assert block["facts"] == 2
    assert block["current"] == 2
    assert block["stale"] == 0
    assert block["superseded"] == 0
    assert block["changes_since"]["since"] == 0
    assert block["changes_since"]["count"] >= 2
    json.dumps(result)  # the additive block stays JSON-encodable


def test_status_without_state_keeps_the_legacy_evidence_and_no_block() -> None:
    result = build_status(
        context={"tracks": [], "revision": {}}, detail="summary",
        sections=["evidence"],
        evidence={"state": "stale", "units_total": 3, "units_current": 1,
                  "units_stale": 2},
    )

    assert result["evidence"] == {
        "state": "stale", "reason": None, "units_total": 3,
        "units_current": 1, "units_stale": 2,
    }
    assert "state" not in result


def test_state_block_names_no_facts_without_pretending() -> None:
    empty = state_block(None)
    assert empty["present"] is False
    assert empty["facts"] == 0
    assert empty["state_token"] == 0


# --------------------------------------------------------------------------- #
# freshness
# --------------------------------------------------------------------------- #


def test_state_freshness_current_when_stored_fingerprints_match(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    report = state_freshness(state.state, live_read())

    assert report["source"] == "state"
    assert report["state"] == "ok"
    assert report["facts_total"] == 2
    assert report["facts_evaluated"] == 2
    assert report["summary"]["sources_current"] == 2
    assert report["summary"]["sources_stale"] == 0
    assert report["summary"]["sources_unknown"] == 0
    track = report["sources"][f"track:{TRACK_A}"][TRACK_STAGE]
    assert track["state"] == "current"
    assert track["reason"] is None
    assert track["complete"] is True
    assert report["changes"] == []


def test_state_freshness_is_stale_after_a_fingerprint_change(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    report = state_freshness(state.state, live_read(volume=0.5))

    track = report["sources"][f"track:{TRACK_A}"][TRACK_STAGE]
    assert track["state"] == "stale"
    assert track["reason"] == "fingerprint_changed"
    assert track["changed_fields"] == ["volume"]
    assert track["windows"][0]["changes"] == [
        {"object": "source", "guid": TRACK_A, "fields": ["volume"],
         "unverified": []},
    ]
    assert report["sources"][f"master:{MASTER}"][MASTER_STAGE]["state"] == "stale"
    assert report["summary"]["sources_stale"] == 2
    assert report["summary"]["sources_current"] == 0
    assert {row["source_id"] for row in report["changes"]} == {
        f"track:{TRACK_A}", f"master:{MASTER}"}
    assert report["changes"][0]["objects"][0]["fields"] == ["volume"]


def test_state_freshness_unknown_is_never_treated_as_clean(tmp_path: Path) -> None:
    state = make_state(tmp_path)

    unreadable = state_freshness(state.state, None)
    assert unreadable["summary"]["sources_current"] == 0
    assert unreadable["summary"]["sources_unknown"] == 2
    reasons = {entry["reason"] for stages in unreadable["sources"].values()
               for entry in stages.values()}
    assert reasons == {"current_state_unreadable"}

    # A summary-shaped live row cannot prove FX, sends or items: unknown.
    partial = state_freshness(state.state, {"tracks": {
        TRACK_A: {"guid": TRACK_A, "volume": 1.0, "volume_db": 0.0,
                  "pan": 0.0, "mute": False, "solo": 0}},
        "master": master_row(), "routing": [send_row()]})
    track = partial["sources"][f"track:{TRACK_A}"][TRACK_STAGE]
    assert track["state"] == "unknown"
    assert track["reason"] == "fingerprint_incomplete"
    assert track["complete"] is False
    assert partial["summary"]["sources_stale"] == 0


def test_state_freshness_marks_a_superseded_fact_as_superseded(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    add_track_fact(state, metric_value=-12.0)  # same identity -> supersedes

    report = state_freshness(state.state, live_read())

    assert report["facts_total"] == 3
    assert report["facts_superseded"] == 1
    assert report["facts_evaluated"] == 2
    assert report["summary"]["sources_current"] == 2


def test_build_freshness_prefers_state_and_marks_the_answer(tmp_path: Path) -> None:
    state = make_state(tmp_path)

    report = build_freshness(state=state.state, live_fingerprints=live_read())
    assert report["source"] == "state"
    assert report["summary"]["sources_current"] == 2

    record = {
        "capture_id": "cap-1",
        "revision_after": {"change_count": 7},
        "project_map": {"render_manifest": {"sources": [{
            "source_id": f"track:{TRACK_A}", "source_guid": TRACK_A,
            "scope": "source", "stage": TRACK_STAGE,
            "render_key": "rk1:aaa", "complete": True,
        }]}},
    }
    legacy = build_freshness(record, current_fingerprints={
        f"track:{TRACK_A}": {"via_master": "rk1:aaa"}})
    assert legacy["source"] == "legacy"
    assert legacy["summary"]["sources_current"] == 1


# --------------------------------------------------------------------------- #
# coverage
# --------------------------------------------------------------------------- #


def test_state_coverage_counts_by_source_stage_and_status(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    add_track_fact(state, start=10.0, end=20.0)        # second window
    add_track_fact(state, metric_value=-12.0)          # supersedes window 0-10

    coverage = state_coverage(state.state)

    assert coverage["facts_total"] == 4
    assert coverage["by_status"] == {"current": 3, "stale": 0, "superseded": 1}
    assert coverage["windows_total"] == 3
    assert coverage["windows_current"] == 3
    track = coverage["by_source"][f"track:{TRACK_A}"]
    assert track["current"] == 2
    assert track["superseded"] == 1
    assert track["facts"] == 3
    assert track["windows"] == 2
    stage = coverage["by_stage"][TRACK_STAGE]
    assert stage["current"] == 2 and stage["superseded"] == 1
    assert coverage["by_source_stage"][f"track:{TRACK_A}"][TRACK_STAGE]["facts"] == 3
    assert coverage["intervals"][f"track:{TRACK_A}"] == [
        {"start_s": 0.0, "end_s": 10.0},
        {"start_s": 10.0, "end_s": 20.0},
    ]
    assert coverage["objects"] == {"tracks": 1, "master": True, "routing": True}
    assert coverage["sources"]["all"] == [f"master:{MASTER}", f"track:{TRACK_A}"]


def test_coverage_report_prefers_state_and_marks_the_answer(tmp_path: Path) -> None:
    state = make_state(tmp_path)

    passport = coverage_report(state=state.state, scope="project", project_id=PROJECT)

    assert passport["source"] == "state"
    assert passport["project_id"] == PROJECT
    assert passport["state_token"] == state.state_token
    assert passport["sources_total"] == 2
    assert passport["sources_measured"] == 2
    assert passport["complete"] is True
    assert passport["counters"]["facts_total"] == 2


def test_coverage_report_falls_back_to_legacy_units() -> None:
    passport = coverage_report(
        units=[], scope="global", project_id="p",
        total_sources=[f"track:{TRACK_A}"], revision={"change_count": 1},
    )

    assert passport["source"] == "legacy"
    assert passport["sources_measured"] == 0
    assert passport["sources_total"] == 1
    assert passport["complete"] is False
    assert passport["gaps"] == [{"source_id": f"track:{TRACK_A}",
                                 "reason": "not_measured"}]


# --------------------------------------------------------------------------- #
# explicit no-state paths
# --------------------------------------------------------------------------- #


def test_no_state_is_reported_explicitly_everywhere() -> None:
    evidence = state_evidence_section(None)
    assert evidence["state"] == "unavailable"
    assert evidence["reason"] == "no_state"
    assert evidence["units_total"] == 0
    assert evidence["counts"] == {"current": 0, "stale": 0, "superseded": 0}

    freshness = state_freshness(None, None)
    assert freshness["state"] == "no_state"
    assert freshness["reason"] == "no_state"
    assert freshness["sources"] == {}
    assert freshness["summary"]["sources_total"] == 0
    assert freshness["summary"]["sources_current"] == 0

    coverage = state_coverage(None)
    assert coverage["state"] == "unknown"
    assert coverage["reason"] == "no_state"
    assert coverage["facts_total"] == 0
    assert coverage["windows_total"] == 0
    assert coverage["sources"]["all"] == []

    legacy = build_freshness(None)
    assert legacy["source"] == "legacy"
    assert legacy["reason"] == "no_capture_record"
    assert legacy["summary"]["sources_total"] == 0


def test_read_state_is_none_when_absent_and_loads_after_save(tmp_path: Path) -> None:
    assert read_state(tmp_path / "archive", PROJECT) is None

    state = make_state(tmp_path)
    state.save()
    document = read_state(tmp_path / "archive", PROJECT)

    assert document is not None
    assert document["project_id"] == PROJECT
    assert len(document["facts"]) == 2
    assert read_state(None, PROJECT) is None


def test_project_status_reads_state_and_keeps_the_legacy_fallback(
        tmp_path: Path) -> None:
    from reaper_audio.daw.session_status import project_status

    class Session:
        archive_root = str(tmp_path / "archive")

        def snapshot(self, **options: object) -> dict[str, object]:
            return {"tracks": [], "master": {},
                    "revision": {"project_id": PROJECT, "change_count": 7,
                                 "path": "C:/songs/state-l.rpp"},
                    "length_s": 10.0}

    legacy = project_status(Session(), sections=["evidence"])
    assert "state" not in legacy
    assert legacy["evidence"]["state"] == "empty"

    make_state(tmp_path).save()
    state_backed = project_status(Session(), sections=["evidence"])
    assert state_backed["evidence"]["units_total"] == 2
    assert state_backed["evidence"]["state"] == "current"
    assert state_backed["state"]["facts"] == 2
    assert state_backed["state"]["present"] is True


def test_read_state_refuses_a_broken_document(tmp_path: Path) -> None:
    path = StateStore(tmp_path / "archive").path_for(PROJECT)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValidationError):
        read_state(tmp_path / "archive", PROJECT)

def test_middle_item_edit_invalidates_both_source_and_master(tmp_path):
    from copy import deepcopy

    track = track_row(TRACK_A)
    track["items"] = [
        {"guid": f"item-{index}", "position_s": float(index), "length_s": 1.0,
         "active_take": {"guid": f"take-{index}", "source": "di.wav"}}
        for index in range(3)
    ]
    state = ProjectState(StateStore(tmp_path), PROJECT)
    state.upsert_objects([track], master_row(), [send_row()])
    state.upsert_fact(source_id=f"track:{TRACK_A}", stage=TRACK_STAGE, window=[0, 3],
                      metrics={"lufs": -14.0},
                      fingerprints={"source": fingerprint_track(track),
                                    "master": fingerprint_master(master_row()),
                                    "routing": fingerprint_routing([send_row()])})
    add_master_fact(state)
    edited = deepcopy(track)
    edited["items"][1].update(position_s=1.2, fade_in_s=0.05, start_offset_s=0.1)
    report = state_freshness(state.state, live_read(track=edited))
    assert report["sources"][f"track:{TRACK_A}"][TRACK_STAGE]["changed_fields"] == ["items"]
    assert report["sources"][f"master:{MASTER}"][MASTER_STAGE]["state"] == "stale"
