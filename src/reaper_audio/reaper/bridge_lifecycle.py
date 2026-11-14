"""Activate a bridge build that is already on disk, without touching the project.

The resident ReaScript bridge is the only component the Python side cannot
restart: it lives inside REAPER's Lua state. This module implements the narrow
lifecycle contract that makes an activation safe and provable:

* the running copy reports ``bridge_build`` in ``health`` and in its registry
  entry, so "which copy is active" is an observed fact, never an assumption;
* the on-disk script declares the same constant, so the caller can state the
  build it expects to activate before asking for it;
* the bridge refuses the reload when its file does not compile, and it compiles
  before its own loop stops, so a broken edit leaves the running copy alive;
* a supersede token stored in the shared Lua global table makes the newest boot
  own the mailbox, so a reload leaves exactly one resident loop;
* this client fails closed on every unproven outcome: no new session, more than
  one live session, a build mismatch or a different project id are all errors,
  never a "probably fine" result.

Nothing here saves, closes, reloads or edits a project. The operation is a
process-local script activation.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from ..errors import BridgeUnavailable, ValidationError

# The constant the resident bridge reports. Kept in one regex so both this
# module and the contract test read the same declaration.
BUILD_PATTERN = re.compile(r'^local bridge_build = "([^"]+)"', re.MULTILINE)

#: Env override for the script the caller wants to activate.
SCRIPT_ENV = "REAPER_BRIDGE_SCRIPT"


def bridge_script_path() -> Path:
    """Return the bridge script that owns the running runtime.

    ``REAPER_BRIDGE_SCRIPT`` wins so a test or a second checkout can point the
    activation at its own file; otherwise the library's own script is used.
    """
    override = os.environ.get(SCRIPT_ENV, "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "scripts" / "reaper_bridge.lua"


def script_build(path: Path | str) -> str | None:
    """Read the build constant from a bridge script, or None when absent."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = BUILD_PATTERN.search(text)
    return match.group(1) if match else None


def _field(instance: Mapping[str, Any], key: str) -> Any:
    return instance.get(key)


def activate_bridge(*,
                    health: Callable[[str | None], Mapping[str, Any]],
                    discover: Callable[[], list[dict[str, Any]]],
                    request_reload: Callable[[str], Mapping[str, Any]] | None,
                    build: str | None,
                    expected_project_id: str | None = None,
                    timeout_s: float = 45.0,
                    poll_interval_s: float = 0.25,
                    clock: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Activate the on-disk build and prove it is the one now running.

    ``health(session_id)`` must return the live health of that session (None
    means the client's default session), ``discover()`` the live instances and
    ``request_reload(expected_build)`` the bridge's lifecycle response.
    """
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
        raise ValidationError("timeout_s must be a positive number")
    if isinstance(poll_interval_s, bool) or not isinstance(poll_interval_s, (int, float)) \
            or poll_interval_s <= 0:
        raise ValidationError("poll_interval_s must be a positive number")
    if not isinstance(build, str) or not build:
        raise BridgeUnavailable(
            "The bridge script on disk declares no build; refusing to activate it",
            details={"reason": "disk_build_missing"})

    running = health(None)
    running_build = _field(running, "bridge_build")
    running_session = _field(running, "bridge_session")
    if not isinstance(running_session, str) or not running_session:
        raise BridgeUnavailable(
            "The resident bridge did not report its session",
            details={"reason": "session_missing"})
    project = running.get("project")
    live_project_id = (project.get("project_id")
                       if isinstance(project, Mapping) else None)
    if not isinstance(live_project_id, str) or not live_project_id:
        raise BridgeUnavailable(
            "The resident bridge did not report the active project identity",
            details={"reason": "project_missing"})
    if expected_project_id is not None and live_project_id != expected_project_id:
        raise BridgeUnavailable(
            "The active project does not match the requested bridge activation context",
            details={"reason": "project_changed", "expected": expected_project_id,
                     "current": live_project_id})
    # Always pin the live project read before a reload, even when the caller
    # did not supply a project id. A successful activation must preserve it.
    expected_project_id = live_project_id
    if not isinstance(running_build, str) or not running_build:
        raise BridgeUnavailable(
            "The running bridge predates the lifecycle contract; refusing an "
            "action-based bootstrap",
            details={"reason": "bootstrap_required",
                     "running_session": running_session,
                     "expected_build": build})

    if running_build == build:
        return {"reloaded": False, "reason": "already_current",
                "session": running_session, "build": running_build,
                "project_id": (running.get("project") or {}).get("project_id")
                if isinstance(running.get("project"), Mapping) else None}

    if request_reload is None:
        raise ValidationError(
            "a bridge that reports a build requires the lifecycle reload call")
    started = clock()
    answer = request_reload(running_build)
    if not isinstance(answer, Mapping) or answer.get("accepted") is not True:
        raise BridgeUnavailable(
            "The resident bridge refused the reload",
            details={"reason": "reload_refused", "answer": dict(answer or {})})

    return _await_booted_session(
        health=health, discover=discover, previous_session=running_session,
        build=build, expected_project_id=expected_project_id, started=started,
        timeout_s=float(timeout_s), poll_interval_s=float(poll_interval_s),
        clock=clock, sleep=sleep, previous_build=running_build)


def _await_booted_session(*, health: Callable[[str | None], Mapping[str, Any]],
                          discover: Callable[[], list[dict[str, Any]]],
                          previous_session: str, build: str,
                          expected_project_id: str | None, started: float,
                          timeout_s: float, poll_interval_s: float,
                          clock: Callable[[], float],
                          sleep: Callable[[float], None],
                          previous_build: str | None = "unknown") -> dict[str, Any]:
    """Wait for a new session with the expected build and the old session's death."""
    deadline = started + timeout_s
    last_seen: list[str] = []
    saw_new = False
    while clock() < deadline:
        live = [row for row in discover()
                if isinstance(row, Mapping) and row.get("bridge_session_id")]
        last_seen = [str(row.get("bridge_session_id")) for row in live]
        others = [row for row in live
                  if str(row.get("bridge_session_id")) != previous_session]
        old_alive = any(str(row.get("bridge_session_id")) == previous_session
                        for row in live)
        if len(others) > 1:
            raise BridgeUnavailable(
                "More than one bridge session is alive after the activation; exactly "
                "one resident loop is required",
                details={"reason": "duplicate_sessions", "sessions": last_seen})
        if len(others) == 1:
            candidate = str(others[0].get("bridge_session_id"))
            info = health(candidate)
            if _field(info, "bridge_build") == build:
                saw_new = True
                if not old_alive:
                    project = info.get("project")
                    project_id = (project.get("project_id")
                                  if isinstance(project, Mapping) else None)
                    if expected_project_id is not None and project_id != expected_project_id:
                        raise BridgeUnavailable(
                            "The active project changed during the bridge activation",
                            details={"reason": "project_changed",
                                     "expected": expected_project_id,
                                     "current": project_id})
                    result = {"reloaded": True, "reason": "activated",
                              "previous_session": previous_session,
                              "session": candidate, "build": build,
                              "project_id": project_id,
                              "elapsed_s": round(clock() - started, 3)}
                    if previous_build is not None:
                        result["previous_build"] = previous_build
                    return result
        sleep(poll_interval_s)

    if saw_new:
        raise BridgeUnavailable(
            "The new bridge answered, but the previous session is still alive; the "
            "old resident loop was not replaced",
            details={"reason": "previous_session_alive",
                     "previous_session": previous_session,
                     "sessions_seen": last_seen,
                     "timeout_s": timeout_s})
    raise BridgeUnavailable(
        "No new bridge session reported the expected build before the timeout",
        details={"reason": "new_session_missing", "expected_build": build,
                 "previous_session": previous_session,
                 "sessions_seen": last_seen, "timeout_s": timeout_s})
