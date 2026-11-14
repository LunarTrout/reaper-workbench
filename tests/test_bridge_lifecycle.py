"""Contract tests for the bridge lifecycle activation (mock transport only).

These tests never touch REAPER: they encode the guarantees the live bridge
promises through ``bridge.lifecycle`` so a regression shows up here first.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from reaper_audio.errors import BridgeUnavailable, ValidationError
from reaper_audio.reaper.bridge_lifecycle import (
    BUILD_PATTERN,
    activate_bridge,
    bridge_script_path,
    script_build,
)

PROJECT_ID = "reaper:{TEST}:C:/fixtures/project.rpp"
OLD = "6af00000-000000"
NEW = "6af11111-111111"
BUILD = "b2-2026-11-14"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += float(seconds)


def _health(build: str | None, session: str, project_id: str = PROJECT_ID) -> dict[str, Any]:
    row: dict[str, Any] = {
        "service": "reaper-workbench",
        "protocol": 1,
        "bridge_session": session,
        "project": {"project_id": project_id},
    }
    if build is not None:
        row["bridge_build"] = build
    return row


def test_already_current_is_a_no_op() -> None:
    calls: list[str] = []

    result = activate_bridge(
        health=lambda session: _health(BUILD, OLD),
        discover=lambda: [{"bridge_session_id": OLD}],
        request_reload=lambda expected: calls.append(expected) or {"accepted": True},
        build=BUILD,
    )

    assert result["reloaded"] is False
    assert result["reason"] == "already_current"
    assert result["session"] == OLD
    assert calls == [], "a bridge already running the disk build must not reload"


def test_activation_reports_the_new_session_and_build() -> None:
    clock = _Clock()
    reloads: list[str] = []
    sessions = {"live": [OLD]}

    def request_reload(expected: str) -> dict[str, Any]:
        reloads.append(expected)
        sessions["live"] = [OLD, NEW]
        return {"accepted": True, "running_build": expected}

    def health(session: str | None) -> dict[str, Any]:
        if session == NEW:
            return _health(BUILD, NEW)
        return _health("b1-old", OLD)

    def discover() -> list[dict[str, Any]]:
        live = list(sessions["live"])
        # The old loop dies on its next tick, so it is visible only once.
        if sessions["live"] == [OLD, NEW]:
            sessions["live"] = [NEW]
        return [{"bridge_session_id": sid} for sid in live]

    result = activate_bridge(health=health, discover=discover,
                             request_reload=request_reload, build=BUILD,
                             timeout_s=5.0, clock=clock, sleep=clock.sleep)

    assert reloads == ["b1-old"]
    assert result["reloaded"] is True
    assert result["previous_session"] == OLD
    assert result["previous_build"] == "b1-old"
    assert result["session"] == NEW
    assert result["build"] == BUILD
    assert result["project_id"] == PROJECT_ID


def test_missing_new_session_fails_closed() -> None:
    clock = _Clock()
    result = pytest.raises(BridgeUnavailable)
    with result as raised:
        activate_bridge(
            health=lambda session: _health("b1-old", OLD),
            discover=lambda: [{"bridge_session_id": OLD}],
            request_reload=lambda expected: {"accepted": True},
            build=BUILD, timeout_s=2.0, poll_interval_s=0.5,
            clock=clock, sleep=clock.sleep)
    assert raised.value.details["reason"] == "new_session_missing"


def test_duplicate_sessions_fail_closed() -> None:
    clock = _Clock()
    def health(session: str | None) -> dict[str, Any]:
        if session is None:
            return _health("b1-old", OLD)
        return _health(BUILD, session)

    with pytest.raises(BridgeUnavailable) as raised:
        activate_bridge(
            health=health,
            discover=lambda: [{"bridge_session_id": OLD},
                              {"bridge_session_id": NEW},
                              {"bridge_session_id": "6af22222-222222"}],
            request_reload=lambda expected: {"accepted": True},
            build=BUILD, timeout_s=2.0, clock=clock, sleep=clock.sleep)
    assert raised.value.details["reason"] == "duplicate_sessions"


def test_project_change_fails_closed() -> None:
    clock = _Clock()
    other = "reaper:{OTHER}:C:/fixtures/other.rpp"
    with pytest.raises(BridgeUnavailable) as raised:
        activate_bridge(
            health=lambda session: (_health(BUILD, NEW, other) if session == NEW
                                    else _health("b1-old", OLD)),
            discover=lambda: [{"bridge_session_id": NEW}],
            request_reload=lambda expected: {"accepted": True},
            build=BUILD, expected_project_id=PROJECT_ID,
            timeout_s=2.0, clock=clock, sleep=clock.sleep)
    assert raised.value.details["reason"] == "project_changed"


def test_project_identity_is_pinned_even_without_caller_override() -> None:
    clock = _Clock()
    other = "reaper:{OTHER}:C:/fixtures/other.rpp"

    def health(session: str | None) -> dict[str, Any]:
        return _health(BUILD, NEW, other) if session == NEW else _health("b1-old", OLD)

    with pytest.raises(BridgeUnavailable) as raised:
        activate_bridge(
            health=health,
            discover=lambda: [{"bridge_session_id": NEW}],
            request_reload=lambda expected: {"accepted": True},
            build=BUILD, timeout_s=2.0, clock=clock, sleep=clock.sleep)
    assert raised.value.details["reason"] == "project_changed"
    assert raised.value.details["expected"] == PROJECT_ID


def test_legacy_bridge_requires_one_boot() -> None:
    """A bridge without the build constant cannot be activated from the client."""
    called: list[str] = []
    with pytest.raises(BridgeUnavailable) as raised:
        activate_bridge(
            health=lambda session: _health(None, OLD),
            discover=lambda: [{"bridge_session_id": OLD}],
            request_reload=lambda expected: called.append(expected) or {"accepted": True},
            build=BUILD)
    assert raised.value.details["reason"] == "bootstrap_required"
    assert called == [], "a legacy bridge must not be asked to reload"


def test_refusal_is_surfaced_with_the_bridge_code() -> None:
    with pytest.raises(BridgeUnavailable) as raised:
        activate_bridge(
            health=lambda session: _health("b1-old", OLD),
            discover=lambda: [{"bridge_session_id": OLD}],
            request_reload=lambda expected: {
                "accepted": False, "error": "reload_rejected",
                "details": {"reason": "compile_error"}},
            build=BUILD)
    assert raised.value.details["reason"] == "reload_refused"


def test_missing_disk_build_is_refused() -> None:
    with pytest.raises(BridgeUnavailable) as raised:
        activate_bridge(
            health=lambda session: _health("b1-old", OLD),
            discover=lambda: [{"bridge_session_id": OLD}],
            request_reload=lambda expected: {"accepted": True},
            build=None)
    assert raised.value.details["reason"] == "disk_build_missing"


def test_timeout_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        activate_bridge(
            health=lambda session: _health(BUILD, OLD),
            discover=lambda: [],
            request_reload=lambda expected: {"accepted": True},
            build=BUILD, timeout_s=0)


def test_script_build_round_trip(tmp_path: Path) -> None:
    script = tmp_path / "bridge.lua"
    script.write_text('-- header\nlocal bridge_build = "b9-test"\nreturn\n',
                      encoding="utf-8")
    assert script_build(script) == "b9-test"
    script.write_text('local other = "x"\n', encoding="utf-8")
    assert script_build(script) is None
    assert script_build(tmp_path / "missing.lua") is None


def test_resident_lua_declares_the_lifecycle_contract() -> None:
    """Static guard: the resident script must keep the v2 lifecycle surface."""
    text = bridge_script_path().read_text(encoding="utf-8", errors="replace")
    assert BUILD_PATTERN.search(text), "scripts/reaper_bridge.lua declares no bridge_build"
    assert '["bridge.lifecycle"] = bridge_lifecycle' in text
    assert "loadfile(script_path)" in text, "reload must compile before it stops the loop"
    assert "_G.reaper_bridge_supersede" in text, "a second boot must supersede the first"
    assert re.search(r"if _G\.reaper_bridge_supersede ~= supersede_token then", text), \
        "the loop must stop when a newer boot owns the mailbox"


def test_legacy_bridge_requires_operator_managed_upgrade() -> None:
    calls: list[str] = []
    with pytest.raises(BridgeUnavailable) as raised:
        activate_bridge(
            health=lambda session: _health(None, OLD),
            discover=lambda: [{"bridge_session_id": OLD}],
            request_reload=lambda expected: calls.append(expected) or {"accepted": True},
            build=BUILD)
    assert raised.value.details["reason"] == "bootstrap_required"
    assert calls == []


def test_mcp_tool_does_not_expose_action_bootstrap() -> None:
    """The MCP lifecycle surface must not expose the failed legacy action path."""
    import inspect
    from pathlib import Path

    import reaper_audio.mcp_server as mcp_server

    signature = inspect.signature(mcp_server.reaper_bridge_reload)
    assert "allow_action_bootstrap" not in signature.parameters
    source = Path(mcp_server.__file__).read_text(encoding="utf-8")
    assert "bootstrap=_native_bootstrap" not in source


def test_resident_lua_does_not_defer_arbitrary_actions() -> None:
    """Action invocation is synchronous; lifecycle handoff has its own operation."""
    text = bridge_script_path().read_text(encoding="utf-8", errors="replace")
    assert "local pending_action = nil" not in text
    assert 'if arguments.defer == true then' not in text
