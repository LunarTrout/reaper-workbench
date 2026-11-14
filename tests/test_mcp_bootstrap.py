"""Bootstrap and interpretation-gate contract tests (no REAPER, no DSP)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from reaper_audio import mcp_server
from reaper_audio.errors import ValidationError

REVISION = {"project_id": "project-gate", "change_count": 4,
            "path": "C:/music/gate.RPP"}


class _FakeSession:
    def __init__(self) -> None:
        self.bridge = SimpleNamespace(bridge_session_id="bridge-gate")

    def snapshot(self, **kwargs: Any) -> dict[str, Any]:
        return {"revision": dict(REVISION), "tracks": [], "master": {},
                "name": "Gate fixture"}


def test_interpretation_gate_refuses_an_absent_receipt() -> None:
    with pytest.raises(ValidationError) as raised:
        mcp_server._require_interpretation_receipt(
            None, REVISION["project_id"], REVISION
        )
    assert raised.value.details["gate"] == "panorama_interpretation"


def test_project_read_is_bootstrap_structural_not_a_complete_panorama(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "_session",
                        lambda *args, **kwargs: _FakeSession())
    result = mcp_server.reaper_project(project_id=REVISION["project_id"])
    assert result["panorama_gate"]["complete"] is False
    assert result["panorama_gate"]["bootstrap_structural_read"] is True
    assert result["panorama_gate"]["interpretation_allowed"] is False


def test_project_analysis_is_refused_before_a_complete_receipt(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "_session",
                        lambda *args, **kwargs: _FakeSession())
    result = mcp_server.reaper_analyze(
        project_id=REVISION["project_id"], expected_revision=dict(REVISION)
    )
    assert result["ok"] is False
    assert result["error"]["details"]["gate"] == "panorama_interpretation"


def test_bootstrap_capture_is_allowed_without_a_prior_receipt(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _FakeCaptureService:
        def __init__(self, session: Any, archive_root: str) -> None:
            captured["archive_root"] = archive_root

        def capture(self, **kwargs: Any) -> dict[str, Any]:
            captured["capture_kwargs"] = kwargs
            return {"ok": True, "capture_id": "cap_bootstrap", "timings": {},
                    "panorama": {"raw": "must-not-be-returned"}}

    monkeypatch.setattr(mcp_server, "_session",
                        lambda *args, **kwargs: _FakeSession())
    monkeypatch.setattr(mcp_server, "_current_context",
                        lambda session, expected=None: {"revision": dict(REVISION)})
    monkeypatch.setattr(mcp_server, "FullCaptureService", _FakeCaptureService)
    result = mcp_server.reaper_capture_snapshot(
        project_id=REVISION["project_id"],
        expected_revision=dict(REVISION),
        archive_root=str(tmp_path / "archive"),
        start_s=0.0, end_s=1.0,
    )
    assert result["ok"] is True
    assert result["capture_id"] == "cap_bootstrap"
    assert "panorama" not in result
    assert result["panorama_delivery"]["view_delivered"] is False
    assert result["panorama_delivery"]["interpretation_allowed"] is False
    assert captured["archive_root"].endswith("archive")
    assert captured["capture_kwargs"]["section"] == (0.0, 1.0)
