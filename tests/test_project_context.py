"""Single project-context builder: summary/full, caps, deltas, paging.

Every state here is written by the production ``ProjectState`` API, so the
context is pinned against the same document the capture layer stores.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from reaper_audio.context.project_context import (
    DEFAULT_PAGE_BYTES,
    SECTIONS,
    build_project_context,
    estimated_tokens,
    project_context_pages,
)
from reaper_audio.context.project_state import (
    ProjectState,
    StateStore,
    empty_state,
)
from reaper_audio.errors import ValidationError

PROJECT = "reaper:{PROJECT-0000-4000-8000-000000000001}:/songs/context.rpp"
TRACK_A = "{AAAAAAAA-0000-4000-8000-00000000000A}"
TRACK_B = "{BBBBBBBB-0000-4000-8000-00000000000B}"
MASTER = "{MASTER00-0000-4000-8000-00000000000M}"


def _track(guid: str, name: str) -> dict[str, object]:
    return {
        "guid": guid,
        "name": name,
        "volume": 1.0,
        "pan": 0.0,
        "mute": False,
        "solo": 0,
        "channel_count": 2,
        "fx": [],
        "sends": [],
        "items": [],
    }


def _state(tmp_path: Path) -> ProjectState:
    state = ProjectState(StateStore(tmp_path), PROJECT)
    state.upsert_objects(
        tracks=[_track(TRACK_A, "Drums"), _track(TRACK_B, "Bass")],
        master={"guid": MASTER, "name": "Master", "volume": 1.0, "fx": []},
        routing=[],
    )
    state.upsert_fact(
        source_id=f"track:{TRACK_A}",
        stage="track_post_fx",
        window=[0.0, 2.0],
        metrics={"rms_dbfs": -18.0, "lufs_i": -16.5},
        measured_at="2026-01-01T00:00:00Z",
    )
    state.upsert_fact(
        source_id=f"track:{TRACK_B}",
        stage="track_post_fx",
        window=[0.0, 2.0],
        metrics={"rms_dbfs": -21.0},
    )
    return state


def _map_document() -> dict[str, object]:
    return {
        "tracks": [
            {
                "guid": TRACK_A,
                "name": "Drums",
                "fx": [{"name": "ReaEQ"}, {"name": "ReaComp"}],
                "sends": [{"to": TRACK_B}],
                "items": [
                    {"guid": "{ITEM-1}", "position_s": 0.0, "length_s": 2.0},
                    {"guid": "{ITEM-2}", "position_s": 2.0, "length_s": 1.5},
                ],
            },
            {
                "guid": TRACK_B,
                "name": "Bass",
                "fx_names": ["ReaTune"],
                "send_count": 0,
                "item_count": 4,
            },
        ],
        "master": {"guid": MASTER, "name": "Master",
                   "fx_names": ["ReaLimit"]},
        "routing": [
            {"source_id": TRACK_A, "destination_id": TRACK_B,
             "send_index": 0, "kind": "send", "volume": 0.5, "pan": 0.0,
             "mode": "post_fader", "mute": False},
        ],
        "tempo": {"bpm": 120.0},
        "markers": [{"time_s": 4.0, "name": "verse"}],
        "regions": [{"interval": {"start_s": 4.0, "end_s": 8.0},
                     "name": "A"}],
        "media": {"offline": [], "missing": [{"source": "kick.wav"}]},
        "runtime": {"state": "current", "instance_id": "bridge-1"},
        "audio": [
            {"source_guid": TRACK_A, "is_master": False,
             "sha256": "a" * 64, "stage": "track_via_master_post_fx"},
            {"source_guid": MASTER, "is_master": True,
             "sha256": "b" * 64, "stage": "master_mix_post_fx"},
        ],
    }


def _page_size(page: object) -> int:
    return len(json.dumps(page, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8"))


# --------------------------------------------------------------------------- #
# summary vs full
# --------------------------------------------------------------------------- #


def test_summary_rows_have_no_values_and_full_rows_do(tmp_path: Path) -> None:
    state = _state(tmp_path)

    summary = build_project_context(state)
    rows = summary["facts"]["rows"]
    assert len(rows) == 2
    assert summary["facts"]["detail"] == "summary"
    for row in rows:
        assert "values" not in row
        assert set(row) == {"id", "source_id", "stage", "window",
                            "metrics_available", "status"}
    assert rows[0]["metrics_available"] == ["lufs_i", "rms_dbfs"]

    full = build_project_context(state, detail="full")
    full_rows = full["facts"]["rows"]
    assert full["facts"]["detail"] == "full"
    assert all("values" in row for row in full_rows)
    drums = next(row for row in full_rows if row["source_id"] == f"track:{TRACK_A}")
    assert drums["values"]["rms_dbfs"] == -18.0
    assert drums["values"]["lufs_i"] == -16.5


# --------------------------------------------------------------------------- #
# fact cap
# --------------------------------------------------------------------------- #


def test_max_facts_caps_rows_and_reports_next_pointer(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.upsert_fact(
        source_id=f"master:{MASTER}",
        stage="master_post_fx",
        window=[0.0, 2.0],
        metrics={"lufs_i": -14.0},
    )

    context = build_project_context(state, max_facts=1)
    facts = context["facts"]
    assert facts["total"] == 3
    assert facts["returned"] == 1
    assert len(facts["rows"]) == 1
    assert facts["next"] == {"detail": "state.query", "remaining": 2}

    none = build_project_context(state, max_facts=0)
    assert none["facts"]["returned"] == 0
    assert none["facts"]["rows"] == []
    assert none["facts"]["next"]["remaining"] == 3

    all_rows = build_project_context(state, max_facts=None)
    assert all_rows["facts"]["returned"] == 3
    assert all_rows["facts"]["next"]["remaining"] == 0


# --------------------------------------------------------------------------- #
# changes delta
# --------------------------------------------------------------------------- #


def test_since_token_returns_the_delta_not_the_recent_log(tmp_path: Path) -> None:
    state = _state(tmp_path)
    token = state.state_token
    state.upsert_fact(
        source_id=f"track:{TRACK_B}",
        stage="track_post_fx",
        window=[2.0, 4.0],
        metrics={"rms_dbfs": -20.0},
    )

    delta = build_project_context(state, since_token=token)["changes"]
    assert delta["mode"] == "since"
    assert delta["present"] is True
    assert delta["since"] == token
    assert delta["state_token"] == state.state_token
    assert [row["window"] for row in delta["new_or_updated"]] == [[2.0, 4.0]]
    assert delta["now_stale"] == []
    assert delta["superseded"] == []

    recent = build_project_context(state)["changes"]
    assert recent["mode"] == "recent"
    assert recent["state_token"] == state.state_token
    assert recent["total"] >= 4
    assert recent["returned"] == min(recent["total"], 20)
    assert recent["omitted"] == recent["total"] - recent["returned"]
    assert recent["entries"][-1]["seq"] == state.state_token

    reset = build_project_context(state, since_token=state.state_token + 10)["changes"]
    assert reset["reset"] is True
    assert reset["since"] == state.state_token + 10


# --------------------------------------------------------------------------- #
# fx passthrough
# --------------------------------------------------------------------------- #


def test_fx_summary_is_passed_through(tmp_path: Path) -> None:
    state = _state(tmp_path)
    rows = [
        {"track": TRACK_A, "slot": 0, "plugin": "ReaEQ",
         "set_params_count": 2, "available_params_count": 12,
         "reference": "fx:{A}:0", "last_edit_at": "2026-01-01T00:00:00Z"},
        {"track": TRACK_B, "slot": 1, "plugin": "ReaComp",
         "set_params_count": 0, "available_params_count": 8,
         "reference": "fx:{B}:1", "last_edit_at": None},
    ]
    context = build_project_context(state, fx_summary=rows)
    assert context["fx"]["present"] is True
    assert context["fx"]["count"] == 2
    assert context["fx"]["rows"] == rows

    absent = build_project_context(state)
    assert absent["fx"] == {"present": False, "count": 0, "rows": []}


# --------------------------------------------------------------------------- #
# map compaction
# --------------------------------------------------------------------------- #


def test_map_is_compact_and_audio_is_reference_only(tmp_path: Path) -> None:
    state = _state(tmp_path)
    context = build_project_context(state, map_document=_map_document())
    project_map = context["map"]

    assert project_map["present"] is True
    assert project_map["track_count"] == 2
    drums = project_map["tracks"][0]
    assert drums["guid"] == TRACK_A
    assert drums["name"] == "Drums"
    assert drums["fx_names"] == ["ReaEQ", "ReaComp"]
    assert drums["send_count"] == 1
    assert drums["item_summary"]["items_count"] == 2
    assert drums["item_summary"]["timed_count"] == 2
    assert drums["item_summary"]["start_min_s"] == 0.0
    assert drums["item_summary"]["end_max_s"] == 3.5
    # No full item row leaks: only the compact summary is present.
    assert "items" not in drums
    assert "takes" not in drums
    assert project_map["tracks"][1]["item_summary"] == {
        "items_count": 4, "timed_count": None, "start_min_s": None,
        "start_max_s": None, "end_min_s": None, "end_max_s": None,
    }

    assert project_map["master"] == {
        "guid": MASTER, "name": "Master", "fx_names": ["ReaLimit"],
    }
    assert project_map["routes"] == [{
        "from": TRACK_A, "to": TRACK_B, "send": 0, "kind": "send",
        "mode": "post_fader", "volume": 0.5, "pan": 0.0, "mute": False,
    }]
    assert project_map["tempo"] == {"bpm": 120.0}
    assert project_map["markers"] == [[4.0, "verse"]]
    assert project_map["regions"] == [[4.0, 8.0, "A"]]

    audio = context["audio"]
    assert audio["present"] is True
    assert audio["count"] == 2
    assert audio["sources"][f"track:{TRACK_A}"] == {
        "sha256": "a" * 64, "stage": "track_via_master_post_fx",
    }
    assert audio["sources"][f"master:{MASTER}"] == {
        "sha256": "b" * 64, "stage": "master_mix_post_fx",
    }
    assert "audio" not in project_map


def test_health_carries_counts_stale_sources_and_optional_blocks(
        tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.mark_stale({TRACK_A: ["volume"]})

    context = build_project_context(state, map_document=_map_document())
    health = context["health"]
    assert health["facts"] == {
        "present": True, "total": 2, "current": 1, "stale": 1,
        "superseded": 0,
    }
    assert health["stale_sources"] == [f"track:{TRACK_A}"]
    assert health["media"] == {
        "present": True, "offline_count": 0, "missing_count": 1, "ok": False,
    }
    assert health["runtime"]["present"] is True
    assert health["runtime"]["details"]["state"] == "current"

    bare = build_project_context(state)["health"]
    assert bare["media"] == {"present": False}
    assert bare["runtime"] == {"present": False}


# --------------------------------------------------------------------------- #
# paging
# --------------------------------------------------------------------------- #


def test_pages_are_deterministic_and_cover_every_section_once(
        tmp_path: Path) -> None:
    state = _state(tmp_path)
    context = build_project_context(
        state, map_document=_map_document(),
        fx_summary=[{"track": TRACK_A, "slot": 0, "plugin": "ReaEQ"}],
    )
    solo = project_context_pages(context, page_bytes=1)
    assert [page["sections"] for page in solo] == [[name] for name in SECTIONS]
    limit = max(_page_size(page) for page in solo) + 64

    pages = project_context_pages(context, page_bytes=limit)
    assert pages == project_context_pages(context, page_bytes=limit)
    assert len(pages) >= 2
    for page in pages:
        assert _page_size(page) <= limit
        assert page["schema"] == "reaper-workbench.project-context/1"
    flattened = [name for page in pages for name in page["sections"]]
    assert flattened == list(SECTIONS)
    assert len(flattened) == len(set(flattened))


def test_pages_never_split_a_section_and_allow_an_oversized_one(
        tmp_path: Path) -> None:
    state = _state(tmp_path)
    context = build_project_context(state, map_document=_map_document())
    pages = project_context_pages(context, page_bytes=1)
    assert len(pages) == len(SECTIONS)
    assert all(len(page["sections"]) == 1 for page in pages)
    for page in pages:
        name = page["sections"][0]
        assert set(page["context"]) == {name}


def test_default_page_budget_is_the_documented_policy() -> None:
    assert DEFAULT_PAGE_BYTES == 46_000
    empty_pages = project_context_pages({})
    assert len(empty_pages) == 1
    assert empty_pages[0]["sections"] == []
    assert empty_pages[0]["context"] == {}


# --------------------------------------------------------------------------- #
# token estimate
# --------------------------------------------------------------------------- #


def test_token_estimate_uses_utf8_bytes_over_two_point_two(tmp_path: Path) -> None:
    context = build_project_context(_state(tmp_path),
                                    map_document=_map_document())
    encoded = json.dumps(context, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    assert estimated_tokens(context) == max(1, math.ceil(len(encoded) / 2.2))
    # Conservative relative to a four-bytes-per-token rule of thumb.
    assert estimated_tokens(context) >= math.ceil(len(encoded) / 4)
    assert estimated_tokens({}) >= 1


# --------------------------------------------------------------------------- #
# empty and absent inputs
# --------------------------------------------------------------------------- #


def test_empty_state_is_an_explicit_empty_context() -> None:
    context = build_project_context(empty_state(PROJECT))
    assert context["project"] == {
        "project_id": PROJECT, "path": None, "revision": None,
        "state_token": 0,
    }
    assert context["map"] == {"present": False}
    assert context["health"]["facts"] == {
        "present": True, "total": 0, "current": 0, "stale": 0,
        "superseded": 0,
    }
    assert context["health"]["stale_sources"] == []
    assert context["facts"] == {
        "present": True, "detail": "summary", "total": 0, "returned": 0,
        "rows": [], "next": {"detail": "state.query", "remaining": 0},
    }
    assert context["changes"]["present"] is True
    assert context["changes"]["entries"] == []
    assert context["audio"] == {"present": False, "count": 0, "sources": {}}
    assert context["next"] == {
        "detail": "state.query", "parameters": "fx.parameters",
        "delta": "since=state_token",
    }
    # JSON-safe end to end.
    json.dumps(context, allow_nan=False)


def test_absent_state_is_absent_not_invented() -> None:
    context = build_project_context(None)
    assert context["project"]["project_id"] is None
    assert context["project"]["state_token"] is None
    assert context["health"]["facts"]["present"] is False
    assert context["facts"]["present"] is False
    assert context["facts"]["rows"] == []
    assert context["changes"]["present"] is False
    json.dumps(context, allow_nan=False)


def test_capture_record_envelope_supplies_map_and_audio(tmp_path: Path) -> None:
    state = _state(tmp_path)
    record = {"project_path": "C:/songs/context.rpp",
              "revision": {"change_count": 7},
              "project_map": _map_document(),
              "audio": _map_document()["audio"]}
    context = build_project_context(state, map_document=record)
    assert context["project"]["path"] == "C:/songs/context.rpp"
    assert context["project"]["revision"] == {"change_count": 7}
    assert context["map"]["track_count"] == 2
    assert context["audio"]["count"] == 2


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("detail", ["brief", "", None])
def test_unknown_detail_is_refused(tmp_path: Path, detail: object) -> None:
    with pytest.raises(ValidationError):
        build_project_context(_state(tmp_path), detail=detail)  # type: ignore[arg-type]


@pytest.mark.parametrize("max_facts", [-1, 1.5, True, "10"])
def test_bad_max_facts_is_refused(tmp_path: Path, max_facts: object) -> None:
    with pytest.raises(ValidationError):
        build_project_context(_state(tmp_path), max_facts=max_facts)  # type: ignore[arg-type]


def test_bad_since_token_and_map_document_are_refused(tmp_path: Path) -> None:
    state = _state(tmp_path)
    with pytest.raises(ValidationError):
        build_project_context(state, since_token=-2)
    with pytest.raises(ValidationError):
        build_project_context(state, map_document=["not", "a", "map"])
    with pytest.raises(ValidationError):
        build_project_context(state, map_document={"tracks": "nope"})


def test_bad_page_bytes_and_fx_rows_are_refused(tmp_path: Path) -> None:
    context = build_project_context(_state(tmp_path))
    with pytest.raises(ValidationError):
        project_context_pages(context, page_bytes=0)
    with pytest.raises(ValidationError):
        project_context_pages(context, page_bytes=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        build_project_context(_state(tmp_path), fx_summary=["not a row"])
