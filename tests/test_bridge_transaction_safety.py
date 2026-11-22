"""Safety contract for bridge transactions.

Two rules keep a plan-scoped undo from touching anything it did not apply:

* a same-slot ``fx.move`` is a true no-op (``TrackFX_CopyToTrack`` on the slot a
  plugin already occupies can drop it), and
* ``transaction.undo`` is bound to the plan it claims to undo, so a plan that
  changed nothing can never rewind an earlier transaction.

The bridge is a resident ReaScript with no unit harness, so the two rules are
asserted at the source level here. The behavioural checks live in the opt-in
tests at the bottom; they need a disposable project copy and are skipped unless
``REAPER_WORKBENCH_LIVE=1`` and ``REAPER_WORKBENCH_SAFETY_COPY`` are set. This
module never opens, saves, switches or closes a project.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from reaper_audio.daw.session import EditOperation, ProjectRevision, ProjectSession
from reaper_audio.errors import StaleContext
from reaper_audio.reaper.bridge_client import ReaperBridge

BRIDGE = Path(__file__).resolve().parents[1] / "scripts" / "reaper_bridge.lua"
LIVE = os.environ.get("REAPER_WORKBENCH_LIVE") == "1"
COPY = os.environ.get("REAPER_WORKBENCH_SAFETY_COPY")
live_only = pytest.mark.skipif(
    not (LIVE and COPY),
    reason="transaction safety gate: set REAPER_WORKBENCH_LIVE=1 and "
           "REAPER_WORKBENCH_SAFETY_COPY to a disposable project copy",
)


def _function_body(name: str) -> str:
    text = BRIDGE.read_text(encoding="utf-8")
    start = text.index(f"local function {name}(")
    following = text.find("\nlocal function ", start + 1)
    return text[start:following if following != -1 else len(text)]


def _branch(body: str, marker: str) -> str:
    start = body.index(marker)
    following = body.find("elseif", start)
    return body[start:following if following != -1 else len(body)]


def test_fx_move_to_the_current_slot_is_not_marked_as_a_change() -> None:
    branch = _branch(_function_body("inspect_edit"), 'normalized.kind == "fx.move"')
    assert "changed = true" not in branch
    assert "before.fx_index" in branch
    assert "before.fx_count" in branch
    assert "desired.to_index" in branch


def test_fx_move_state_reads_the_current_slot_and_chain_length() -> None:
    branch = _branch(_function_body("read_edit_state"), 'kind == "fx.move"')
    assert "state.fx_index" in branch
    assert "state.fx_count" in branch


def test_bridge_undo_is_bound_to_the_requested_plan() -> None:
    body = _function_body("transaction_undo")
    assert "arguments.plan_id" in body
    assert "last.plan_id ~= requested_plan" in body
    assert "Undo_DoUndo2" in body


# --------------------------------------------------------------------------
# Opt-in behavioural gate (disposable copy only)
# --------------------------------------------------------------------------

def _normalized(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _live_session() -> ProjectSession:
    assert COPY, "REAPER_WORKBENCH_SAFETY_COPY is required"
    bridge = ReaperBridge()
    wanted = _normalized(COPY)
    for entry in bridge.list_projects(timeout_s=5.0)["projects"]:
        revision = entry.get("revision") or {}
        path = revision.get("path")
        if isinstance(path, str) and path and _normalized(path) == wanted:
            return ProjectSession(bridge, project_id=str(revision["project_id"]))
    pytest.fail(
        "The declared disposable copy is not open in this bridge; open exactly "
        f"{COPY} in a dedicated REAPER instance first."
    )


def _revision(session: ProjectSession) -> ProjectRevision:
    return ProjectRevision.from_dict(session.snapshot()["revision"])


def _fx_guids(session: ProjectSession) -> list[str]:
    tracks = session.snapshot(include_items=False).get("tracks") or []
    return [str(fx["guid"]) for fx in (tracks[0].get("fx") or [])]


@live_only
def test_live_same_slot_fx_move_is_a_true_no_op() -> None:
    session = _live_session()
    before = _revision(session)
    context = session.snapshot(include_items=False)
    track_guid = str(context["tracks"][0]["guid"])
    fx = (context["tracks"][0].get("fx") or [])
    assert fx, "the disposable copy needs at least one FX for this gate"
    fx_guid = str(fx[0]["guid"])

    plan = session.plan(
        [EditOperation("fx.move", track_guid,
                       {"fx_guid": fx_guid, "to_index": 0})],
        reason="safety gate: same-slot move",
    )
    applied = session.apply(plan, dry_run=False)

    assert applied["no_op"] is True
    assert applied["revision"]["change_count"] == before.change_count
    assert fx_guid in _fx_guids(session)


@live_only
def test_live_no_op_apply_cannot_undo_the_previous_transaction() -> None:
    session = _live_session()
    context = session.snapshot(include_items=False)
    track_guid = str(context["tracks"][0]["guid"])
    original = float(context["tracks"][0]["volume"])
    target = 0.5 if abs(original - 0.5) > 1e-6 else 0.55

    real_plan = session.plan(
        [EditOperation("track.set", track_guid, {"volume": target})],
        reason="safety gate: a real edit",
    )
    session.apply(real_plan, dry_run=False)
    after_real = _revision(session)

    noop_plan = session.plan(
        [EditOperation("track.set", track_guid, {"volume": target})],
        reason="safety gate: same value again",
    )
    applied = session.apply(noop_plan, dry_run=False)
    assert applied["no_op"] is True

    with pytest.raises(StaleContext):
        session.undo(expected_revision=_revision(session),
                     plan_id=noop_plan.plan_id)

    # The real edit must still stand: the refused undo changed nothing.
    current = session.snapshot(include_items=False)["tracks"][0]["volume"]
    assert abs(float(current) - target) < 1e-6

    session.undo(expected_revision=_revision(session), plan_id=real_plan.plan_id)
    restored = session.snapshot(include_items=False)["tracks"][0]["volume"]
    assert abs(float(restored) - original) < 1e-6
from pathlib import Path

def test_bridge_offsets_are_validated_read_and_written():
    bridge = (Path(__file__).resolve().parents[1] / "scripts" / "reaper_bridge.lua").read_text(encoding="utf8")
    item_validator = bridge.split("local function validate_item_values(values)", 1)[1].split("local function ", 1)[0]
    take_validator = bridge.split("local function validate_take_values(values)", 1)[1].split("local function ", 1)[0]
    item_reader = bridge.split('elseif kind == "item.set" then', 2)[2].split('elseif kind == "item.timing_apply"', 1)[0]
    item_writer = bridge.split('elseif kind == "item.set" then', 3)[3].split('elseif kind == "fx.insert"', 1)[0]
    assert "start_offset_s = true" in item_validator
    assert "start_offset_s = true" in take_validator
    assert 'reaper.GetMediaItemTakeInfo_Value(take, "D_STARTOFFS")' in item_reader
    assert 'checked_call(reaper.SetMediaItemTakeInfo_Value, take, "D_STARTOFFS", numeric)' in item_writer
    assert 'playrate = "D_PLAYRATE", start_offset_s = "D_STARTOFFS"' in bridge
    # Extending a forward 1x take left keeps any original source sample at its old time.
    assert 183.0 + (31.5 - 31.054621) == pytest.approx(182.6 + (31.5 - 30.654621))
