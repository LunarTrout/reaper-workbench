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
        self._counter = 0
        self.bridge = FakeBridge(self)

    def snapshot(self, **_: object) -> dict:
        return {"revision": dict(self.revision)}

    def plan(self, operations, *, reason, expected_revision=None) -> EditPlan:
        self._counter += 1
        revision = (expected_revision if isinstance(expected_revision, ProjectRevision)
                    else ProjectRevision.from_dict(self.revision))
        return EditPlan(revision=revision, operations=tuple(operations),
                        reason=reason, plan_id=f"plan_{self._counter}", before=[])

    def apply(self, plan: EditPlan, *, dry_run: bool = True) -> dict:
        assert dry_run is False
        before = {"pan": 0.0}
        after = {"pan": -1.0}
        self.revision = dict(self.target_revision)
        self.digest_value = dict(self.digest_value_after)
        return {
            "plan_id": plan.plan_id, "dry_run": False, "applied": True,
            "no_op": False,
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

    def undo(self, *, expected_revision: ProjectRevision | None = None) -> dict:
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
    delta = applied["delta"]
    assert delta["acknowledgeable"] is True
    assert delta["audio_evidence"]["state"] == "stale"
    assert delta["audio_evidence"]["invalidated_sources"] == ["mst_m", "trk_a", "trk_b"]
    assert delta["audio_evidence"]["fresh_sources"] == []
    assert delta["invalidations"][0]["reason"] == "unverified_cross_capture_dependency"
    assert delta["advance"]["advanced"] is True

    acked = PanoramaLifecycle(root).delta_ack(
        delta["delta_id"], delta["delta_hash"], token)
    assert acked["complete"] is True
    advanced = acked["context_token"]

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
    assert applied_two["delta"]["acknowledgeable"] is True
    assert applied_two["delta"]["base"]["context_token"] == advanced


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
    acked = PanoramaLifecycle(root).delta_ack(
        applied["delta"]["delta_id"], applied["delta"]["delta_hash"], token)
    advanced = acked["context_token"]

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

    result = mcp_server.reaper_api_call(
        "GetAppVersion", [], PROJECT_ID, _revision(1),
        panorama_receipt=token, archive_root=str(root))
    delta = result["delta"]
    assert delta["scope"] == "unknown"
    assert delta["render"]["authoritative"] is False
    assert delta["render"]["changed"] == []
    assert delta["render"]["unchanged"] == []
    assert delta["audio_evidence"]["state"] == "stale"
    assert delta["audio_evidence"]["invalidated_sources"] == ["mst_m", "trk_a", "trk_b"]
    assert delta["invalidations"][0]["scope"] == "all_project_audio"
    assert delta["invalidations"][0]["sources"] is None
    assert delta["acknowledgeable"] is True

    acked = PanoramaLifecycle(root).delta_ack(
        delta["delta_id"], delta["delta_hash"], token)
    assert acked["complete"] is True
    assert acked["audio_evidence"]["state"] == "stale"


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

    result = mcp_server.reaper_action(
        "invoke", PROJECT_ID, _revision(1), command_id=40044,
        panorama_receipt=token, archive_root=str(root))
    assert result["state_changed"] is True
    delta = result["delta"]
    assert delta["scope"] == "unknown"
    assert delta["kind"] == "action"
    assert delta["audio_evidence"]["state"] == "stale"
    assert delta["audio_evidence"]["invalidated_sources"] == ["mst_m", "trk_a", "trk_b"]


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
    acked = PanoramaLifecycle(root).delta_ack(
        applied["delta"]["delta_id"], applied["delta"]["delta_hash"], token)

    # Undo restores pan but changes the take volume: a new forward revision
    # whose delta must name the volume dependency, not erase the edit history.
    session.target_revision = _revision(3)
    session.digest_value_after = _digest(change_count=3, pan_a=0.0, volume_a=0.8)
    undone = mcp_server.reaper_edit(
        "undo", PROJECT_ID, _revision(2),
        plan_id=preview["plan"]["plan_id"], panorama_receipt=acked["context_token"],
        archive_root=str(root))
    delta = undone["delta"]
    assert undone["mode"] == "undo"
    assert delta["kind"] == "undo"
    assert delta["base"]["revision"]["change_count"] == 2
    assert delta["target"]["revision"]["change_count"] == 3
    assert delta["acknowledgeable"] is True
    assert delta["audio_evidence"]["state"] == "stale"
    assert delta["audio_evidence"]["invalidated_sources"] == ["mst_m", "trk_a", "trk_b"]
    assert delta["audio_evidence"]["fresh_sources"] == []
