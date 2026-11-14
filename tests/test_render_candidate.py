from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from reaper_audio.daw.render_candidate import MAX_CANDIDATE_BYTES
from reaper_audio.daw.session import EditOperation, EditPlan, ProjectRevision, ProjectSession
from reaper_audio.errors import (
    ArtifactCleanupError,
    RenderBudgetExceeded,
    RenderCandidateMismatch,
    RenderProvenanceIncomplete,
    StaleContext,
    ValidationError,
)
from reaper_audio.models import Section


def _revision(path: Path, change_count: int = 5) -> ProjectRevision:
    return ProjectRevision(project_id="reaper:candidate-test", change_count=change_count,
                           path=str(path), instance_id="bridge-candidate",
                           epoch=1, epoch_basis="bridge_session_project_generation")


def _digest(*, change_count: int = 5, fx_coverage: bool = True) -> dict:
    return {
        "project_id": "reaper:candidate-test",
        "path": "C:/candidate.rpp",
        "change_count": change_count,
        "digest_version": 1,
        "coverage": {"tracks": True, "items": False, "takes": False,
                     "fx": fx_coverage, "sends": True, "tempo": False,
                     "master": True, "automation": False, "envelopes": False,
                     "project": False, "fx_state": True},
        "missing": [] if fx_coverage else ["fx"],
        "fields": {
            "tracks": [{
                "guid": "{TRACK}", "main_send": True, "folder_depth": 0,
                "sends": [{"index": 0, "dest_guid": "{MASTER}", "mode": 0,
                           "mute": False, "volume": 1.0, "pan": 0.0,
                           "source_channels": 0, "destination_channels": 0}],
                "fx": [{"index": 0, "guid": "{FX-TRACK}", "name": "Track EQ",
                        "enabled": True, "offline": False, "parameter_count": 3}],
            }],
            "master": {
                "guid": "{MASTER}", "main_send": True,
                "sends": [],
                "fx": [{"index": 0, "guid": "{FX-MASTER}", "name": "Master Limiter",
                        "enabled": True, "offline": False, "parameter_count": 2}],
            },
            "fx_state": {"complete": True, "tracks": [
                {"track_guid": "{TRACK}", "is_master": False, "fx_count": 1,
                 "chunk": "<FXCHAIN>", "complete": True},
                {"track_guid": "{MASTER}", "is_master": True, "fx_count": 1,
                 "chunk": "<MASTERFXLIST>", "complete": True},
            ]},
            "project": {"settings": {"render_settings": 0}, "unread": [],
                        "complete": True},
        },
    }


class CandidateBridge:
    def __init__(self, path: Path, *, digest: dict | None = None,
                 fail_render: bool = False, restore_report: dict | None = None,
                 post_digest: dict | None = None) -> None:
        self.revision = {"project_id": "reaper:candidate-test", "change_count": 5,
                         "path": str(path), "instance_id": "bridge-candidate",
                         "epoch": 1,
                         "epoch_basis": "bridge_session_project_generation"}
        self.digest_value = digest or _digest()
        self.post_digest = post_digest
        self.restore_report = restore_report
        self.fail_render = fail_render
        self.render_calls: list[dict] = []

    def project_context(self, project_id: str | None = None, **_: object) -> dict:
        return {"revision": dict(self.revision)}

    def project_digest(self, project_id: str | None = None) -> dict:
        return dict(self.digest_value)

    def capture_render(self, **kwargs: object) -> dict:
        self.render_calls.append(dict(kwargs))
        if self.fail_render:
            raise RuntimeError("simulated render failure")
        directory = Path(str(kwargs["output_dir"]))
        job_id = str(kwargs["job_id"])
        master = directory / f"rali-{job_id}-00-MASTER.wav"
        sf.write(master, np.zeros(8_000 * 2, dtype=np.float64).reshape(-1, 2),
                 8_000, subtype="FLOAT")
        before = dict(self.revision)
        after = {**self.revision, "change_count": 6}
        self.revision = after
        if self.post_digest is not None:
            self.digest_value = self.post_digest
        else:
            self.digest_value = {**self.digest_value, "change_count": 6}
        restore = self.restore_report or {
            "restored": True, "failures": [],
            "dirty_before": 0, "dirty_after": 0,
        }
        return {
            "project_id": before["project_id"],
            "revision": before,
            "revision_after": after,
            "capture_point": "native_render_master_only",
            "render_action": "File: Render project",
            "render_settings_used": {
                "RENDER_SETTINGS": 0.0, "RENDER_BOUNDSFLAG": 2.0,
                "RENDER_CHANNELS": 2.0, "RENDER_TAILMS": 0.0,
                "RENDER_ADDTOPROJ": 0.0,
            },
            "restore_report": restore,
            "channels": 2,
            "sample_rate": 8_000,
            "tail_s": 0.0,
            "files": [{"source_guid": "{MASTER}", "name": "MASTER",
                       "track_index": -1, "is_master": True,
                       "path": str(master), "bytes": master.stat().st_size}],
        }


def _plan(path: Path, *, base_change: int = 5) -> EditPlan:
    return EditPlan(
        revision=_revision(path, base_change),
        operations=(EditOperation("track.set", "{TRACK}", {"volume": 0.5}),),
        reason="candidate test", plan_id="plan-candidate-1",
    )


def test_render_candidate_rejects_missing_plan_revision_before_render(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "fixture.rpp"
    copy.write_text("<REAPER_PROJECT>", encoding="utf-8")
    bridge = CandidateBridge(copy)
    session = ProjectSession(bridge, project_id="reaper:candidate-test")
    bad_plan = SimpleNamespace(plan_id="plan-bad-revision", revision=None)

    with pytest.raises(ValidationError) as excinfo:
        session.render_candidate(
            bad_plan, revision_after=_revision(copy, 5),
            section=Section(1.0, 2.0), artifact_root=tmp_path / "artifacts",
            sample_rate=8_000,
        )
    assert excinfo.value.details["field"] == "plan.revision"
    assert excinfo.value.details["received"] == "missing"
    assert bridge.render_calls == []


def test_render_candidate_rejects_malformed_plan_revision_before_render(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "fixture.rpp"
    copy.write_text("<REAPER_PROJECT>", encoding="utf-8")
    bridge = CandidateBridge(copy)
    session = ProjectSession(bridge, project_id="reaper:candidate-test")
    bad_plan = SimpleNamespace(
        plan_id="plan-bad-revision",
        revision={"project_id": "reaper:candidate-test", "change_count": 5},
    )

    with pytest.raises(ValidationError) as excinfo:
        session.render_candidate(
            bad_plan, revision_after=_revision(copy, 5),
            section=Section(1.0, 2.0), artifact_root=tmp_path / "artifacts",
            sample_rate=8_000,
        )
    assert excinfo.value.details["field"] == "plan.revision"
    assert excinfo.value.details["validation_details"]["field"] == "path"
    assert "malformed" in str(excinfo.value).lower()
    assert bridge.render_calls == []


def test_render_candidate_binds_plan_revision_interval_and_master_provenance(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "fixture.rpp"
    copy.write_text("<REAPER_PROJECT>", encoding="utf-8")
    bridge = CandidateBridge(copy)
    session = ProjectSession(bridge, project_id="reaper:candidate-test")

    result = session.render_candidate(
        _plan(copy), revision_after=_revision(copy, 5), section=Section(1.0, 2.0),
        artifact_root=tmp_path / "artifacts", sample_rate=8_000,
    )

    assert result["plan_id"] == "plan-candidate-1"
    assert result["section"] == {"start_s": 1.0, "end_s": 2.0}
    assert result["audio"]["sample_rate"] == 8_000
    assert result["audio"]["channels"] == 2
    assert result["audio"]["duration_s"] == pytest.approx(1.0)
    assert result["audio"]["file_window"] == {"start_s": 0.0, "end_s": 1.0}
    assert result["audio"]["project_window"] == {"start_s": 1.0, "end_s": 2.0}
    assert result["audio"]["stage"] == "master_output_post_track_bus_master_fx"
    assert result["audio"]["node"] == "master"
    assert result["audio"]["sha256"]
    assert result["provenance"]["master_fx"][0]["name"] == "Master Limiter"
    assert result["provenance"]["routing_summary"]["main_send_count"] == 1
    assert result["provenance"]["routing_summary"]["send_edge_count"] == 1
    assert result["provenance"]["fingerprints"]["routing_sha256"]
    assert result["project_state"]["content_unchanged"] is True
    assert result["project_state"]["restoration_verified"] is True
    assert result["project_state"]["content_comparison"]["equal"] is True
    assert result["project_state"]["content_comparison"]["excluded_fields"] == ["change_count"]
    assert result["project_state"]["counter_changed_by_render"] is True
    assert result["revision_after_render"]["change_count"] == 6
    assert result["render_key"]
    assert Path(result["audio"]["path"]).exists()
    assert bridge.render_calls[0]["master_only"] is True
    assert bridge.render_calls[0]["render_settings"] == 0
    assert bridge.render_calls[0]["section_start_s"] == 1.0
    assert bridge.render_calls[0]["section_end_s"] == 2.0


def test_render_candidate_refuses_plan_revision_mismatch_before_render(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "fixture.rpp"
    copy.write_text("<REAPER_PROJECT>", encoding="utf-8")
    bridge = CandidateBridge(copy)
    session = ProjectSession(bridge, project_id="reaper:candidate-test")

    with pytest.raises(RenderCandidateMismatch):
        session.render_candidate(
            _plan(copy, base_change=6), revision_after=_revision(copy, 5),
            section=Section(1.0, 2.0), artifact_root=tmp_path / "artifacts",
            sample_rate=8_000,
        )
    assert bridge.render_calls == []


def test_render_candidate_refuses_stale_project_before_render(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "fixture.rpp"
    copy.write_text("<REAPER_PROJECT>", encoding="utf-8")
    bridge = CandidateBridge(copy)
    bridge.revision["change_count"] = 6
    session = ProjectSession(bridge, project_id="reaper:candidate-test")

    with pytest.raises(StaleContext):
        session.render_candidate(
            _plan(copy), revision_after=_revision(copy, 5),
            section=Section(1.0, 2.0), artifact_root=tmp_path / "artifacts",
            sample_rate=8_000,
        )
    assert bridge.render_calls == []


def test_render_candidate_works_without_disposable_env_declaration(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REAPER_WORKBENCH_RENDER_COPY", raising=False)
    monkeypatch.delenv("REAPER_WORKBENCH_FIXTURE", raising=False)
    copy = tmp_path / "fixture.rpp"
    copy.write_text("<REAPER_PROJECT>", encoding="utf-8")
    bridge = CandidateBridge(copy)
    session = ProjectSession(bridge, project_id="reaper:candidate-test")

    result = session.render_candidate(
        _plan(copy), revision_after=_revision(copy, 5),
        section=Section(1.0, 2.0), artifact_root=tmp_path / "artifacts",
        sample_rate=8_000,
    )
    assert result["revision_after_render"]["change_count"] == 6
    assert result["project_state"]["restoration_verified"] is True


def test_render_candidate_treats_counter_only_digest_change_as_content_equal(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "fixture.rpp"
    copy.write_text("<REAPER_PROJECT>", encoding="utf-8")
    counter_only = _digest()
    counter_only["change_count"] = 6
    bridge = CandidateBridge(copy, post_digest=counter_only)
    session = ProjectSession(bridge, project_id="reaper:candidate-test")

    result = session.render_candidate(
        _plan(copy), revision_after=_revision(copy, 5),
        section=Section(1.0, 2.0), artifact_root=tmp_path / "artifacts",
        sample_rate=8_000,
    )
    comparison = result["project_state"]["content_comparison"]
    assert comparison["equal"] is True
    assert comparison["excluded_fields"] == ["change_count"]
    assert comparison["changed_fields"] == []
    assert comparison["compared_digest_sha256_before"] == comparison[
        "compared_digest_sha256_after"]
    assert result["project_state"]["counter_changed_by_render"] is True


def test_render_candidate_refuses_over_budget_window_before_render(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "fixture.rpp"
    copy.write_text("<REAPER_PROJECT>", encoding="utf-8")
    bridge = CandidateBridge(copy)
    session = ProjectSession(bridge, project_id="reaper:candidate-test")

    with pytest.raises(RenderBudgetExceeded):
        session.render_candidate(
            _plan(copy), revision_after=_revision(copy, 5),
            section=Section(0.0, 2.0), artifact_root=tmp_path / "artifacts",
            sample_rate=8_000, max_duration_s=1.0,
        )
    assert bridge.render_calls == []


def test_render_candidate_refuses_incomplete_provenance(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "fixture.rpp"
    copy.write_text("<REAPER_PROJECT>", encoding="utf-8")
    bridge = CandidateBridge(copy, digest=_digest(fx_coverage=False))
    session = ProjectSession(bridge, project_id="reaper:candidate-test")

    with pytest.raises(RenderProvenanceIncomplete):
        session.render_candidate(
            _plan(copy), revision_after=_revision(copy, 5),
            section=Section(1.0, 2.0), artifact_root=tmp_path / "artifacts",
            sample_rate=8_000,
        )
    assert bridge.render_calls == []


def test_render_candidate_cleans_workspace_when_render_fails(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "fixture.rpp"
    copy.write_text("<REAPER_PROJECT>", encoding="utf-8")
    root = tmp_path / "artifacts"
    bridge = CandidateBridge(copy, fail_render=True)
    session = ProjectSession(bridge, project_id="reaper:candidate-test")

    with pytest.raises(RuntimeError, match="simulated render failure"):
        session.render_candidate(
            _plan(copy), revision_after=_revision(copy, 5),
            section=Section(1.0, 2.0), artifact_root=root, sample_rate=8_000,
        )
    assert not any(root.glob("job-*"))


def test_render_candidate_rejects_oversized_actual_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "fixture.rpp"
    copy.write_text("<REAPER_PROJECT>", encoding="utf-8")
    bridge = CandidateBridge(copy)
    session = ProjectSession(bridge, project_id="reaper:candidate-test")

    # A one-second, 8 kHz stereo float WAV is ~64 kB; this budget is below it.
    with pytest.raises(RenderBudgetExceeded):
        session.render_candidate(
            _plan(copy), revision_after=_revision(copy, 5),
            section=Section(1.0, 2.0), artifact_root=tmp_path / "artifacts",
            sample_rate=8_000, max_bytes=1_000,
        )


def test_render_candidate_reports_cleanup_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "fixture.rpp"
    copy.write_text("<REAPER_PROJECT>", encoding="utf-8")
    bridge = CandidateBridge(copy, fail_render=True)
    session = ProjectSession(bridge, project_id="reaper:candidate-test")

    from reaper_audio.analysis import artifacts as artifact_module

    def broken_close(self, **_: object):
        raise RuntimeError("simulated cleanup failure")

    monkeypatch.setattr(artifact_module.AudioArtifactWorkspace, "close", broken_close)

    with pytest.raises(ArtifactCleanupError) as excinfo:
        session.render_candidate(
            _plan(copy), revision_after=_revision(copy, 5),
            section=Section(1.0, 2.0), artifact_root=tmp_path / "artifacts",
            sample_rate=8_000,
        )
    assert excinfo.value.details["original_error"] == "RuntimeError"
    assert excinfo.value.details["cleanup_error"] == "RuntimeError"


def test_render_candidate_fails_closed_when_restoration_fails(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "fixture.rpp"
    copy.write_text("<REAPER_PROJECT>", encoding="utf-8")
    root = tmp_path / "artifacts"
    bridge = CandidateBridge(copy, restore_report={
        "restored": False, "failures": ["render_settings"],
        "dirty_before": 0, "dirty_after": 1,
    })
    session = ProjectSession(bridge, project_id="reaper:candidate-test")

    with pytest.raises(RenderProvenanceIncomplete) as excinfo:
        session.render_candidate(
            _plan(copy), revision_after=_revision(copy, 5),
            section=Section(1.0, 2.0), artifact_root=root, sample_rate=8_000,
        )
    assert "restore" in str(excinfo.value).lower()
    assert "render_settings" in str(excinfo.value.details)
    assert not any(root.glob("job-*"))


def test_render_candidate_fails_closed_when_content_digest_changes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "fixture.rpp"
    copy.write_text("<REAPER_PROJECT>", encoding="utf-8")
    changed = _digest()
    changed["change_count"] = 6
    changed["fields"]["master"]["fx"][0]["name"] = "Unexpected Master Limiter"
    bridge = CandidateBridge(copy, post_digest=changed)
    session = ProjectSession(bridge, project_id="reaper:candidate-test")

    with pytest.raises(RenderCandidateMismatch) as excinfo:
        session.render_candidate(
            _plan(copy), revision_after=_revision(copy, 5),
            section=Section(1.0, 2.0), artifact_root=tmp_path / "artifacts",
            sample_rate=8_000,
        )
    assert "content" in str(excinfo.value).lower() or "digest" in str(excinfo.value).lower()


def test_render_candidate_refuses_a_bridge_without_master_render(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "fixture.rpp"
    copy.write_text("<REAPER_PROJECT>", encoding="utf-8")

    class NoRenderBridge:
        def __init__(self) -> None:
            self.revision = {"project_id": "reaper:candidate-test",
                             "change_count": 5, "path": str(copy)}

        def project_context(self, project_id: str | None = None, **_: object) -> dict:
            return {"revision": dict(self.revision)}

        def project_digest(self, project_id: str | None = None) -> dict:
            return _digest()

    session = ProjectSession(NoRenderBridge(), project_id="reaper:candidate-test")
    from reaper_audio.errors import RenderUnsupported

    with pytest.raises(RenderUnsupported):
        session.render_candidate(
            _plan(copy), revision_after=_revision(copy, 5),
            section=Section(1.0, 2.0), artifact_root=tmp_path / "artifacts",
            sample_rate=8_000,
        )
