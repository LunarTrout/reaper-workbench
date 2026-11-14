from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import soundfile as sf

pytest.importorskip("mcp")

from reaper_audio import mcp_server
from reaper_audio.analysis.artifacts import AudioArtifactWorkspace
from reaper_audio.daw.session import EditOperation, EditPlan, ProjectRevision

PROJECT_ID = "reaper:candidate-mcp"
MASTER = "{MASTER}"


def _revision(change_count: int = 5) -> dict:
    return {"project_id": PROJECT_ID, "change_count": change_count,
            "path": "C:/candidate.rpp", "instance_id": "bridge-candidate",
            "epoch": 1, "epoch_basis": "bridge_session_project_generation"}


def _plan() -> EditPlan:
    return EditPlan(revision=ProjectRevision.from_dict(_revision(5)),
                    operations=(EditOperation("track.set", "{TRACK}", {"volume": 0.5}),),
                    reason="candidate transport test", plan_id="plan-candidate-mcp")


class CandidateSession:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.revision = _revision(5)
        self.calls: list[dict] = []
        self.bridge = SimpleNamespace(bridge_session_id="bridge-candidate")

    def snapshot(self, **_: object) -> dict:
        return {"revision": dict(self.revision)}

    def render_candidate(self, plan: EditPlan, *, revision_after, section,
                         artifact_root, channels="stereo", sample_rate=None,
                         tail_ms=0, max_duration_s=120.0):
        self.calls.append({"plan_id": plan.plan_id, "section": section,
                           "channels": channels, "sample_rate": sample_rate,
                           "tail_ms": tail_ms, "revision_after": revision_after})
        workspace = AudioArtifactWorkspace(artifact_root, job_id="candidatejob1")
        path = workspace.directory / "candidate-master.wav"
        rate = sample_rate or 8_000
        frames = int(round(section.duration_s * rate))
        sf.write(path, np.zeros((frames, 2)), rate, subtype="FLOAT")
        artifact = workspace.register_asset(path)
        directory = workspace.directory
        workspace.retain()
        audio = {
            "path": str(artifact.path), "artifact_name": artifact.filename,
            "sha256": artifact.sha256, "bytes": artifact.bytes,
            "sample_rate": rate, "channels": 2, "frames": frames,
            "duration_s": frames / rate, "format": "WAV",
            "file_window": {"start_s": 0.0, "end_s": frames / rate},
            "project_window": {"start_s": section.start_s, "end_s": section.end_s},
            "measured_window": {"start_s": section.start_s, "end_s": section.end_s},
            "tail_s": tail_ms / 1000.0,
            "stage": "master_output_post_track_bus_master_fx",
            "node": "master", "channel_view": channels,
            "source_kind": "rendered_master_mix",
            "audio_accessor_used": False,
        }
        return {
            "plan_id": plan.plan_id,
            "project_id": PROJECT_ID,
            "revision_before": _revision(5),
            "revision_after": _revision(5),
            "revision_after_render": _revision(6),
            "render_key": "rk-candidate",
            "section": {"start_s": section.start_s, "end_s": section.end_s},
            "duration_s": section.duration_s,
            "render_s": 0.1,
            "audio": audio,
            "provenance": {"master_guid": MASTER,
                           "render_settings_used": {"RENDER_SETTINGS": 0.0},
                           "routing_summary": {"track_count": 1},
                           "fingerprints": {"project_digest_scope": "full"}},
            "project_state": {"counter_changed_by_render": True,
                              "counter_delta_by_render": 1,
                              "content_unchanged": True,
                              "restoration_verified": True,
                              "revision_after_render_is_current": True,
                              "content_comparison": {"equal": True,
                                                     "excluded_fields": ["change_count"],
                                                     "changed_fields": []}},
            "artifact_job": {"job_id": "candidatejob1", "directory": str(directory)},
        }


@pytest.fixture
def candidate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CandidateSession:
    root = tmp_path / "leases"
    session = CandidateSession(root)
    monkeypatch.setattr(mcp_server, "_AUDIO_ROOT", root)
    monkeypatch.setattr(mcp_server, "_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(mcp_server, "_prune_analysis_state", lambda: None)
    monkeypatch.setattr(mcp_server, "_analysis_records", {})
    monkeypatch.setattr(mcp_server, "_analysis_roles", {})
    monkeypatch.setattr(mcp_server, "_edit_plans", {
        "plan-candidate-mcp": {
            "plan": _plan(), "project_id": PROJECT_ID,
            "bridge_session_id": "bridge-candidate",
            "created_at": time.time(),
            "applied_revision": ProjectRevision.from_dict(_revision(5)),
        }
    })
    return session


def test_render_candidate_tool_schema_is_transport_visible() -> None:
    tools = asyncio.run(mcp_server.server.list_tools())
    tool = next(tool for tool in tools if tool.name == "reaper_render_candidate")
    properties = tool.input_schema["properties"]

    assert set(properties) >= {"plan_id", "project_id", "revision_after",
                               "start_s", "end_s", "channels", "sample_rate",
                               "tail_ms", "max_duration_s"}
    assert properties["tail_ms"]["default"] == 0
    assert properties["channels"]["default"] == "stereo"
    assert len(tools) == 29


def test_render_candidate_returns_plan_bound_audio_and_releases_lease(
        candidate_env: CandidateSession) -> None:
    result = mcp_server.reaper_render_candidate(
        plan_id="plan-candidate-mcp", project_id=PROJECT_ID,
        revision_after=_revision(5), start_s=1.0, end_s=2.0,
        bridge_session_id="bridge-candidate", sample_rate=8_000,
    )

    assert result["ok"] is True
    assert result["plan_id"] == "plan-candidate-mcp"
    assert result["audio"]["stage"] == "master_output_post_track_bus_master_fx"
    assert result["audio"]["node"] == "master"
    assert result["audio"]["resource_uri"].startswith("reaper://analyses/")
    assert result["audio"]["sha256"]
    assert result["lease"]["release_tool"] == "reaper_release"
    assert result["project_state"]["counter_changed_by_render"] is True
    assert "render_counter_advanced" in result["warnings"]
    assert candidate_env.calls[0]["section"].start_s == 1.0
    assert candidate_env.calls[0]["section"].end_s == 2.0
    assert candidate_env.calls[0]["sample_rate"] == 8_000

    candidate_id = result["candidate_id"]
    audio = mcp_server.analysis_audio_resource(candidate_id, MASTER)
    assert audio[:4] == b"RIFF"
    lease_dir = candidate_env.root / "job-candidatejob1"
    assert lease_dir.exists()

    released = mcp_server.reaper_release(candidate_id)
    assert released["released"] is True
    assert not lease_dir.exists()
    with pytest.raises(Exception):
        mcp_server.analysis_audio_resource(candidate_id, MASTER)


def test_counter_advance_returns_post_render_revision_and_token(
        candidate_env: CandidateSession, monkeypatch: pytest.MonkeyPatch) -> None:
    cached = mcp_server._edit_plans["plan-candidate-mcp"]
    cached["applied_context_token"] = "token-before-render"
    cached["applied_archive_root"] = "/tmp/candidate-archive"
    monkeypatch.setattr(mcp_server, "_panorama_lifecycle",
                        lambda root: SimpleNamespace(root=root))
    seen: list[dict] = []

    def fake_delta(**kwargs: Any) -> dict:
        seen.append(kwargs)
        return {"advance": {"context_token": "token-after-render"},
                "next_call": {"tool": "reaper_panorama_ack"}}

    monkeypatch.setattr(mcp_server, "_record_mutation_delta", fake_delta)

    result = mcp_server.reaper_render_candidate(
        plan_id="plan-candidate-mcp", project_id=PROJECT_ID,
        revision_after=_revision(5), start_s=1.0, end_s=2.0,
        bridge_session_id="bridge-candidate", sample_rate=8_000,
    )

    assert result["revision_after"]["change_count"] == 5
    assert result["revision_after_render"]["change_count"] == 6
    assert result["context_advance"]["status"] == "advanced"
    assert result["context_advance"]["context_token"] == "token-after-render"
    assert result["completeness"]["post_render_revision_current"] is True
    assert result["completeness"]["lifecycle_token_current"] is True
    assert seen and seen[0]["kind"] == "render_candidate"
    assert seen[0]["base_revision"].change_count == 5
    assert cached["applied_revision"].change_count == 6
    assert cached["applied_context_token"] == "token-after-render"


def test_render_candidate_transport_returns_structured_content(
        candidate_env: CandidateSession) -> None:
    call: Any = asyncio.run(mcp_server.server.call_tool(
        "reaper_render_candidate",
        {"plan_id": "plan-candidate-mcp", "project_id": PROJECT_ID,
         "revision_after": _revision(5), "start_s": 1.0, "end_s": 2.0,
         "bridge_session_id": "bridge-candidate", "sample_rate": 8_000},
    ))

    assert call.is_error is False
    assert call.structured_content is not None
    assert call.structured_content["ok"] is True
    assert call.structured_content["audio"]["resource_uri"].startswith("reaper://analyses/")
    assert call.content and "master_output_post_track_bus_master_fx" in call.content[0].text


def test_render_candidate_refuses_revision_not_returned_by_apply(
        candidate_env: CandidateSession) -> None:
    result = mcp_server.reaper_render_candidate(
        plan_id="plan-candidate-mcp", project_id=PROJECT_ID,
        revision_after=_revision(4), start_s=1.0, end_s=2.0,
        bridge_session_id="bridge-candidate",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "stale_context"
    assert candidate_env.calls == []


def test_render_candidate_refuses_unknown_plan_before_session(
        candidate_env: CandidateSession) -> None:
    result = mcp_server.reaper_render_candidate(
        plan_id="unknown", project_id=PROJECT_ID,
        revision_after=_revision(5), start_s=1.0, end_s=2.0,
        bridge_session_id="bridge-candidate",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "validation_error"
    assert candidate_env.calls == []


def test_render_candidate_wire_rejects_boolean_tail(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable(*args: object, **kwargs: object) -> None:
        raise AssertionError("a schema-rejected request must not reach the session")

    monkeypatch.setattr(mcp_server, "_session", unreachable)
    with pytest.raises(Exception) as excinfo:
        asyncio.run(mcp_server.server.call_tool(
            "reaper_render_candidate",
            {"plan_id": "p", "project_id": PROJECT_ID,
             "revision_after": _revision(5), "start_s": 1.0, "end_s": 2.0,
             "tail_ms": True},
        ))
    assert "valid integer" in str(excinfo.value)
