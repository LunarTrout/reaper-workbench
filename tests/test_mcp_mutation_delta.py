"""MCP tool path: apply -> factual delta -> ack -> second apply, no recapture.

The tools are exercised with a fake project session, but the lifecycle, the
delta builder, the gates and the archive are the real implementations.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")

from reaper_audio import mcp_server
from reaper_audio.context.lifecycle import PanoramaLifecycle
from reaper_audio.daw.session import EditOperation, EditPlan, ProjectRevision
from reaper_audio.errors import ValidationError

from test_identity_and_delta import (PROJECT_ID, TRACK_A, _archive, _capture,
                                     _digest, _revision)


class FakeBridge:
    """A bridge call that advances the fake project from the base revision."""

    def __init__(self, session: "FakeSession") -> None:
        self.session = session

    def call_api(self, name: str, *_: object, **__: object) -> SimpleNamespace:
        before = dict(self.session.revision)
        self.session.revision = dict(self.session.target_revision)
        self.session.digest_value = dict(self.session.digest_value_after)
        return SimpleNamespace(name=name, returns=(), arrays={},
                               project_before=before,
                               project_after=dict(self.session.revision))


class FakeSession:
    """One project session whose state advances on apply."""

    def __init__(self, *, revision: dict, digest: dict) -> None:
        self.revision = dict(revision)
        self.digest_value = dict(digest)
        self.target_revision: dict = dict(revision)
        self.digest_value_after: dict = dict(digest)
        self.plans: list[EditPlan] = []
        self.undo_plan_ids: list[str] = []
        self.no_op = False
        self._counter = 0
        self.bridge = FakeBridge(self)

    def snapshot(self, **_: object) -> dict:
        return {"revision": dict(self.revision)}

    def plan(self, operations, *, reason, expected_revision=None) -> EditPlan:
        self._counter += 1
        revision = (expected_revision if isinstance(expected_revision, ProjectRevision)
                    else ProjectRevision.from_dict(self.revision))
        return EditPlan(revision=revision, operations=tuple(operations),
                        reason=reason, plan_id=f"plan_{self._counter}",
                        before=[{"operation_index": index + 1,
                                 "kind": operation.kind,
                                 "target_guid": operation.target_guid,
                                 "values": {"pan": 0.0},
                                 "changes_state": True}
                                for index, operation in enumerate(operations)])

    def apply(self, plan: EditPlan, *, dry_run: bool = True) -> dict:
        assert dry_run is False
        before = {"pan": 0.0}
        after = {"pan": -1.0}
        self.revision = dict(self.target_revision)
        self.digest_value = dict(self.digest_value_after)
        return {
            "plan_id": plan.plan_id, "dry_run": False, "applied": True,
            "no_op": self.no_op,
            "revision_before": plan.revision.to_dict(),
            "revision_after": dict(self.revision),
            "before": [{"operation_index": 1, "kind": "track.set",
                        "target_guid": TRACK_A, "values": before,
                        "changes_state": True}],
            "after": [{"operation_index": 1, "kind": "track.set",
                       "target_guid": TRACK_A, "values": after}],
        }

    def digest(self, project_id: str | None = None) -> dict:
        return dict(self.digest_value)

    def undo(self, *, expected_revision: ProjectRevision | None = None,
             plan_id: str | None = None) -> dict:
        self.undo_plan_ids.append(plan_id)
        before = dict(self.revision)
        self.revision = dict(self.target_revision)
        self.digest_value = dict(self.digest_value_after)
        return {"plan_id": None, "dry_run": False, "applied": True,
                "undone": True, "revision_before": before,
                "revision_after": dict(self.revision),
                "before": [], "after": []}


def _acknowledge(root: Path) -> str:
    lifecycle = PanoramaLifecycle(root, page_bytes=4096)
    opened = lifecycle.open(PROJECT_ID, page_bytes=4096)
    hashes = [opened["page_hash"]]
    for index in range(1, opened["page_count"]):
        hashes.append(lifecycle.page(opened["handle"], opened["revision"], index)["page_hash"])
    ack = lifecycle.ack(opened["handle"], opened["revision"], hashes)
    assert ack["complete"] is True
    return ack["context_token"]


def test_apply_delta_ack_and_second_apply_without_recapture(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", _revision(1), _digest(change_count=1))
    token = _acknowledge(root)

    session = FakeSession(revision=_revision(1), digest=_digest(change_count=1))
    session.target_revision = _revision(2)
    session.digest_value_after = _digest(change_count=2, pan_a=-1.0)
    monkeypatch.setattr(mcp_server, "_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(mcp_server, "_edit_plans", {})
    monkeypatch.setattr(mcp_server, "_PANORAMA_LIFECYCLES", {})

    preview = mcp_server.reaper_edit(
        "preview", PROJECT_ID, _revision(1),
        operations=[{"kind": "track.set", "target_guid": TRACK_A,
                     "values": {"pan": -1.0}}],
        reason="pan a double hard left")
    assert preview["mode"] == "preview"
    plan_id = preview["plan"]["plan_id"]

    applied = mcp_server.reaper_edit(
        "apply", PROJECT_ID, _revision(1), plan_id=plan_id,
        panorama_receipt=token, archive_root=str(root))
    assert applied["mode"] == "apply"
    assert "delta" not in applied
    update = applied["update"]
    assert update["status"] == "change_update"
    assert "from_revision" not in update and "to_revision" not in update
    assert update["revision"]["change_count"] == 2
    assert update["measurements"]["state"] == "stale"
    assert update["measurements"]["invalidated_sources"] == ["mst_m", "trk_a", "trk_b"]
    assert update["measurements"]["fresh_sources"] == []


    changed = {row["id"]: row for row in update["changed"]}
    assert changed[f"track:{TRACK_A}"]["fields"] == [{"field": "pan", "value": -1.0}]
    advanced = applied["next_call"]["context_token"]
    assert advanced and advanced != token

    # A second edit is authorized by the advanced token, with no new capture.
    session.target_revision = _revision(3)
    session.digest_value_after = _digest(change_count=3, pan_a=-1.0, volume_a=0.5)
    preview_two = mcp_server.reaper_edit(
        "preview", PROJECT_ID, _revision(2),
        operations=[{"kind": "track.set", "target_guid": TRACK_A,
                     "values": {"volume": 0.5}}],
        reason="pull the double down")
    applied_two = mcp_server.reaper_edit(
        "apply", PROJECT_ID, _revision(2),
        plan_id=preview_two["plan"]["plan_id"],
        panorama_receipt=advanced, archive_root=str(root))
    assert applied_two["mode"] == "apply"
    assert "delta" not in applied_two
    assert applied_two["update"]["status"] == "change_update"
    assert applied_two["update"]["revision"]["change_count"] == 3
    assert applied_two["next_call"]["context_token"]

def test_no_op_apply_cannot_undo_the_previous_transaction(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A plan that changed nothing must never rewind an earlier transaction."""
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", _revision(1), _digest(change_count=1))
    token = _acknowledge(root)
    session = FakeSession(revision=_revision(1), digest=_digest(change_count=1))
    monkeypatch.setattr(mcp_server, "_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(mcp_server, "_edit_plans", {})
    monkeypatch.setattr(mcp_server, "_PANORAMA_LIFECYCLES", {})
    monkeypatch.setattr(mcp_server, "_record_mutation_delta",
                        lambda **kwargs: {"status": "unavailable",
                                          "acknowledgeable": False,
                                          "reason": "no_op", "next_call": None})

    preview = mcp_server.reaper_edit(
        "preview", PROJECT_ID, _revision(1),
        operations=[{"kind": "fx.move", "target_guid": TRACK_A,
                     "values": {"fx_guid": "{FX-A}", "to_index": 0}}],
        reason="same-slot move")
    session.no_op = True
    applied = mcp_server.reaper_edit(
        "apply", PROJECT_ID, _revision(1),
        plan_id=preview["plan"]["plan_id"], panorama_receipt=token,
        archive_root=str(root))
    assert applied["mode"] == "apply"
    assert applied["no_op"] is True
    assert applied["undo_available"] is False

    refused = mcp_server.reaper_edit(
        "undo", PROJECT_ID, _revision(1),
        plan_id=preview["plan"]["plan_id"], panorama_receipt=token,
        archive_root=str(root))
    assert refused["ok"] is False
    assert "nothing to undo" in refused["error"]["message"]
    assert session.undo_plan_ids == []
def test_stale_audio_blocks_project_analysis_after_an_edit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", _revision(1), _digest(change_count=1))
    token = _acknowledge(root)
    session = FakeSession(revision=_revision(1), digest=_digest(change_count=1))
    session.target_revision = _revision(2)
    session.digest_value_after = _digest(change_count=2, pan_a=-1.0)
    monkeypatch.setattr(mcp_server, "_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(mcp_server, "_edit_plans", {})
    monkeypatch.setattr(mcp_server, "_PANORAMA_LIFECYCLES", {})

    preview = mcp_server.reaper_edit(
        "preview", PROJECT_ID, _revision(1),
        operations=[{"kind": "track.set", "target_guid": TRACK_A,
                     "values": {"pan": -1.0}}], reason="pan")
    applied = mcp_server.reaper_edit(
        "apply", PROJECT_ID, _revision(1),
        plan_id=preview["plan"]["plan_id"], panorama_receipt=token,
        archive_root=str(root))
    advanced = applied["next_call"]["context_token"]
    assert advanced

    # The live project is at revision 2; the analysis gate runs before any render.
    session.revision = _revision(2)
    refused = mcp_server.reaper_analyze(
        project_id=PROJECT_ID, expected_revision=_revision(2),
        panorama_receipt=advanced, archive_root=str(root))
    assert refused["ok"] is False
    assert refused["error"]["code"] == "stale_context"
    assert refused["error"]["details"]["audio_state"] == "stale"
    assert "reaper_capture_snapshot" in refused["error"]["details"]["next_action"]


def test_raw_api_mutation_declares_unknown_scope_and_invalidates_all_audio(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", _revision(1), _digest(change_count=1))
    token = _acknowledge(root)
    session = FakeSession(revision=_revision(1), digest=_digest(change_count=1))
    session.target_revision = _revision(2)
    session.digest_value_after = _digest(change_count=2)
    monkeypatch.setattr(mcp_server, "_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(mcp_server, "_PANORAMA_LIFECYCLES", {})
    sealed: dict = {}
    real = mcp_server._record_mutation_delta

    def spy(**kwargs):
        record = real(**kwargs)
        sealed.update(record)
        return record

    monkeypatch.setattr(mcp_server, "_record_mutation_delta", spy)

    result = mcp_server.reaper_api_call(
        "GetAppVersion", [], PROJECT_ID, _revision(1),
        panorama_receipt=token, archive_root=str(root))

    for forbidden in ("delta", "project_before", "project_after", "expected_revision"):
        assert forbidden not in result
    assert sealed["scope"] == "unknown"
    assert sealed["kind"] == "api_call"
    assert sealed["render"]["authoritative"] is False
    assert sealed["audio_evidence"]["state"] == "stale"
    assert sealed["audio_evidence"]["invalidated_sources"] == ["mst_m", "trk_a", "trk_b"]
    assert sealed["invalidations"][0]["scope"] == "all_project_audio"
    assert result["update"]["schema_version"] == "reaper-workbench.change-update/1"
    assert result["update"]["revision"]["change_count"] == 2
    assert result["update"]["measurements"]["state"] == "stale"
    assert result["update"]["context_token"].startswith("ct_")


def test_action_invocation_declares_unknown_scope(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", _revision(1), _digest(change_count=1))
    token = _acknowledge(root)
    session = FakeSession(revision=_revision(1), digest=_digest(change_count=1))
    session.target_revision = _revision(2)
    session.digest_value_after = _digest(change_count=2)
    monkeypatch.setattr(mcp_server, "_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(mcp_server, "_PANORAMA_LIFECYCLES", {})
    sealed: dict = {}
    real = mcp_server._record_mutation_delta

    def spy(**kwargs):
        record = real(**kwargs)
        sealed.update(record)
        return record

    monkeypatch.setattr(mcp_server, "_record_mutation_delta", spy)

    result = mcp_server.reaper_action(
        "invoke", PROJECT_ID, _revision(1), command_id=40044,
        panorama_receipt=token, archive_root=str(root))
    assert result["state_changed"] is True
    for forbidden in ("delta", "project_before", "project_after"):
        assert forbidden not in result
    assert sealed["scope"] == "unknown"
    assert sealed["kind"] == "action"
    assert sealed["audio_evidence"]["state"] == "stale"
    assert sealed["audio_evidence"]["invalidated_sources"] == ["mst_m", "trk_a", "trk_b"]
    assert result["update"]["measurements"]["state"] == "stale"
    assert result["update"]["context_token"].startswith("ct_")


def test_undo_is_its_own_forward_revision_with_a_factual_delta(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", _revision(1), _digest(change_count=1))
    token = _acknowledge(root)
    session = FakeSession(revision=_revision(1), digest=_digest(change_count=1))
    session.target_revision = _revision(2)
    session.digest_value_after = _digest(change_count=2, pan_a=-1.0)
    monkeypatch.setattr(mcp_server, "_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(mcp_server, "_edit_plans", {})
    monkeypatch.setattr(mcp_server, "_PANORAMA_LIFECYCLES", {})

    preview = mcp_server.reaper_edit(
        "preview", PROJECT_ID, _revision(1),
        operations=[{"kind": "track.set", "target_guid": TRACK_A,
                     "values": {"pan": -1.0}}], reason="pan")
    applied = mcp_server.reaper_edit(
        "apply", PROJECT_ID, _revision(1),
        plan_id=preview["plan"]["plan_id"], panorama_receipt=token,
        archive_root=str(root))
    advanced = applied["next_call"]["context_token"]

    # Undo restores pan but changes the take volume: a new forward revision
    # whose delta must name the volume dependency, not erase the edit history.
    session.target_revision = _revision(3)
    session.digest_value_after = _digest(change_count=3, pan_a=0.0, volume_a=0.8)
    undone = mcp_server.reaper_edit(
        "undo", PROJECT_ID, _revision(2),
        plan_id=preview["plan"]["plan_id"], panorama_receipt=advanced,
        archive_root=str(root))
    assert undone["mode"] == "undo"
    assert "delta" not in undone
    update = undone["update"]
    assert update["status"] == "change_update"
    assert update["revision"]["change_count"] == 3
    assert "from_revision" not in update and "to_revision" not in update
    changed = {row["id"]: row for row in update["changed"]}
    fields = {field["field"]: field["value"]
              for field in changed[f"track:{TRACK_A}"]["fields"]}
    assert fields["volume"] == 0.8
    assert all(set(field) == {"field", "value"}
               for row in update["changed"] for field in row["fields"])
    assert update["measurements"]["state"] == "stale"
    assert update["measurements"]["invalidated_sources"] == ["mst_m", "trk_a", "trk_b"]
    assert update["measurements"]["fresh_sources"] == []
    assert session.undo_plan_ids == [preview["plan"]["plan_id"]]


def test_no_op_apply_cannot_undo_the_previous_transaction(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A plan that changed nothing must never rewind an earlier transaction."""
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", _revision(1), _digest(change_count=1))
    token = _acknowledge(root)
    session = FakeSession(revision=_revision(1), digest=_digest(change_count=1))
    monkeypatch.setattr(mcp_server, "_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(mcp_server, "_edit_plans", {})
    monkeypatch.setattr(mcp_server, "_PANORAMA_LIFECYCLES", {})
    monkeypatch.setattr(mcp_server, "_record_mutation_delta",
                        lambda **kwargs: {"status": "unavailable",
                                          "acknowledgeable": False,
                                          "reason": "no_op", "next_call": None})

    preview = mcp_server.reaper_edit(
        "preview", PROJECT_ID, _revision(1),
        operations=[{"kind": "fx.move", "target_guid": TRACK_A,
                     "values": {"fx_guid": "{FX-A}", "to_index": 0}}],
        reason="same-slot move")
    session.no_op = True
    applied = mcp_server.reaper_edit(
        "apply", PROJECT_ID, _revision(1),
        plan_id=preview["plan"]["plan_id"], panorama_receipt=token,
        archive_root=str(root))
    assert applied["mode"] == "apply"
    assert applied["no_op"] is True
    assert applied["undo_available"] is False

    refused = mcp_server.reaper_edit(
        "undo", PROJECT_ID, _revision(1),
        plan_id=preview["plan"]["plan_id"], panorama_receipt=token,
        archive_root=str(root))
    assert refused["ok"] is False
    assert "nothing to undo" in refused["error"]["message"]
    assert session.undo_plan_ids == []


INTERNAL_RECORD_KEYS = {
    "delta", "delta_id", "delta_hash", "mutation_delta",
    "project_before", "project_after",
    "from_revision", "to_revision",
    "revision_before", "revision_after",
    "base_revision", "target_revision", "base", "target",
}


def _assert_no_internal_record(value: object, path: str = "$") -> None:
    from collections.abc import Mapping as MappingABC
    if isinstance(value, MappingABC):
        for key, item in value.items():
            assert str(key) not in INTERNAL_RECORD_KEYS, f"internal record key at {path}.{key}"
            _assert_no_internal_record(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_internal_record(item, f"{path}[{index}]")


def test_model_facing_mutations_never_expose_an_internal_record(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Edit, raw API, Action, save and render advance all return updates only."""
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", _revision(1), _digest(change_count=1))
    token = _acknowledge(root)
    session = FakeSession(revision=_revision(1), digest=_digest(change_count=1))
    session.target_revision = _revision(2)
    session.digest_value_after = _digest(change_count=2)
    monkeypatch.setattr(mcp_server, "_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(mcp_server, "_edit_plans", {})
    monkeypatch.setattr(mcp_server, "_PANORAMA_LIFECYCLES", {})

    preview = mcp_server.reaper_edit(
        "preview", PROJECT_ID, _revision(1),
        operations=[{"kind": "track.set", "target_guid": TRACK_A,
                     "values": {"pan": -1.0}}], reason="audit")
    applied = mcp_server.reaper_edit(
        "apply", PROJECT_ID, _revision(1),
        plan_id=preview["plan"]["plan_id"], panorama_receipt=token,
        archive_root=str(root))
    _assert_no_internal_record(preview)
    _assert_no_internal_record(applied)

    session.target_revision = _revision(3)
    session.digest_value_after = _digest(change_count=3)
    api = mcp_server.reaper_api_call(
        "GetAppVersion", [], PROJECT_ID, _revision(2),
        panorama_receipt=applied["next_call"]["context_token"], archive_root=str(root))
    _assert_no_internal_record(api)

    session.target_revision = _revision(4)
    session.digest_value_after = _digest(change_count=4)
    action = mcp_server.reaper_action(
        "invoke", PROJECT_ID, _revision(3), command_id=40044,
        panorama_receipt=api["next_call"]["context_token"], archive_root=str(root))
    _assert_no_internal_record(action)

    session.save_current = lambda: {"saved": True, "path": PROJECT_ID}
    saved = mcp_server.reaper_save(
        "save_current", PROJECT_ID, _revision(4),
        panorama_receipt=action["next_call"]["context_token"], archive_root=str(root))
    _assert_no_internal_record(saved)

    advance = mcp_server._advance_candidate_context(
        cached={}, project_id=PROJECT_ID,
        applied=ProjectRevision.from_dict(_revision(4)),
        post_revision=ProjectRevision.from_dict(_revision(5)), session=None)
    _assert_no_internal_record(advance)
