"""Transport, project, settings, application and revision-wait control.

Everything here rides on REAPER's documented ReaScript API through the
existing bridge mailbox: no new bridge operation is invented, and no project
name, section, fader level or loudness target is assumed.  Project-bound calls
carry a typed ``ReaperHandle`` built from the exact revision that was just
read, so a stale revision fails closed instead of editing a different tab.

``app`` is the only part that touches the host process; its launcher,
terminator and health probe are injectable so the state machine can be tested
without starting REAPER.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from math import isfinite
from pathlib import Path
from typing import Any

from ..errors import ApiUnavailable, BridgeUnavailable, TransactionFailed, ValidationError
from ..reaper.handles import ReaperHandle
from ..reaper.window import focus_reaper_window
from .session import ProjectRevision

#: REAPER's documented transport command IDs (Main_OnCommandEx section 0).
TRANSPORT_COMMANDS: Mapping[str, int] = {"play": 1007, "stop": 1016, "pause": 1008}

#: Documented ``GetSetProjectInfo`` descriptors read by ``settings(read)``.
DEFAULT_SETTING_FIELDS: tuple[str, ...] = (
    "RENDER_SETTINGS", "RENDER_BOUNDSFLAG", "RENDER_STARTPOS", "RENDER_ENDPOS",
    "RENDER_CHANNELS", "RENDER_SRATE", "RENDER_TAILFLAG", "RENDER_TAILMS",
    "RENDER_ADDTOPROJ", "RENDER_DITHER", "RENDER_NORMALIZE",
    "PROJECT_SRATE", "PROJECT_SRATE_USE", "PROJECT_TIMEBASE",
    "PROJECT_TIMEBASE_FLAGS", "PLAYBACK_STOP", "DIRTY", "READONLY",
)

#: Descriptors ``settings(write)`` accepts.  They are REAPER's own setting
#: names, not values chosen for this project.
WRITABLE_SETTING_FIELDS: frozenset[str] = frozenset({
    "RENDER_SETTINGS", "RENDER_BOUNDSFLAG", "RENDER_STARTPOS", "RENDER_ENDPOS",
    "RENDER_CHANNELS", "RENDER_SRATE", "RENDER_TAILFLAG", "RENDER_TAILMS",
    "RENDER_ADDTOPROJ", "RENDER_DITHER", "RENDER_NORMALIZE",
    "RENDER_NORMALIZE_TARGET", "RENDER_BRICKWALL",
    "PROJECT_SRATE", "PROJECT_SRATE_USE", "PROJECT_TIMEBASE", "PLAYBACK_STOP",
})

_WATCH_KINDS = ("revision", "transport")


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) else None


def _require_finite(value: object, name: str) -> float:
    number = _finite_number(value)
    if number is None:
        raise ValidationError(f"{name} must be a finite number")
    return number


def _api_scalar(result: Any, *, name: str, index: int = 0) -> Any:
    returns = getattr(result, "returns", ())
    if len(returns) <= index:
        raise BridgeUnavailable(f"REAPER returned no value for {name}")
    return returns[index]


def _handle(revision: ProjectRevision, *, kind: str = "ReaProject",
            stable_id: str | None = None) -> ReaperHandle:
    if not revision.project_id or revision.change_count < 0:
        raise BridgeUnavailable("REAPER returned an incomplete project revision")
    return ReaperHandle(kind=kind, project_id=revision.project_id,
                        change_count=revision.change_count,
                        stable_id=stable_id or revision.project_id)


def _revision_from_state(state: object) -> ProjectRevision:
    revision = (dict(state.get("revision") or {})
                if isinstance(state, Mapping) else {})
    result = ProjectRevision.from_dict(revision)
    if not result.project_id or result.change_count < 0:
        raise BridgeUnavailable("REAPER returned an incomplete project revision")
    return result


def _read_revision(session: Any) -> ProjectRevision:
    """Read the active revision and dirty flag without the track payload."""
    state = session.bridge.call("project.render_settings.read",
                                {"project_id": session.project_id})
    return _revision_from_state(state)


def _read_handle(session: Any) -> tuple[ReaperHandle, ProjectRevision]:
    revision = _read_revision(session)
    return _handle(revision), revision


def transport_control(session: Any, action: str, *,
                      position_s: float | None = None) -> dict[str, Any]:
    """Play, stop or pause the exact project that was read, then prove the state.

    ``position_s`` seeks first (and seeks playback when the action is ``play``).
    The read-back is REAPER's own ``GetPlayStateEx``/``GetCursorPositionEx``:
    a command that was accepted but did not change the transport raises.
    """
    if action not in TRANSPORT_COMMANDS:
        raise ValidationError(
            "transport action must be play, stop or pause",
            details={"action": action, "allowed": list(TRANSPORT_COMMANDS)})
    seek_s = _require_finite(position_s, "position_s") if position_s is not None else None

    context = session.snapshot()
    revision = _revision_from_state(context)
    handle = _handle(revision)
    before = _transport_row(context.get("transport"))
    commands: list[dict[str, Any]] = []

    if seek_s is not None:
        session.bridge.call_api("SetEditCurPos2", handle, seek_s, True,
                                action == "play")
        commands.append({"api": "SetEditCurPos2", "position_s": seek_s})
    command = TRANSPORT_COMMANDS[action]
    session.bridge.call_api("Main_OnCommandEx", command, 0, handle)
    commands.append({"api": "Main_OnCommandEx", "command_id": command})

    raw_state = _api_scalar(session.bridge.call_api("GetPlayStateEx", handle),
                            name="GetPlayStateEx")
    cursor = _api_scalar(session.bridge.call_api("GetCursorPositionEx", handle),
                         name="GetCursorPositionEx")
    play_position = _api_scalar(session.bridge.call_api("GetPlayPositionEx", handle),
                                name="GetPlayPositionEx")
    if isinstance(raw_state, bool) or not isinstance(raw_state, int):
        raise BridgeUnavailable("REAPER returned an invalid play state")
    if _finite_number(cursor) is None or _finite_number(play_position) is None:
        raise BridgeUnavailable("REAPER returned an invalid transport position")
    after = {"play_state": raw_state, "playing": bool(raw_state & 1),
             "paused": bool(raw_state & 2), "recording": bool(raw_state & 4),
             "cursor_s": float(cursor), "play_position_s": float(play_position)}
    if action == "play" and not after["playing"]:
        raise BridgeUnavailable("REAPER did not start playback",
                                details={"before": before, "after": after})
    if action in {"stop", "pause"} and after["playing"]:
        raise BridgeUnavailable("REAPER did not stop playback",
                                details={"action": action, "before": before,
                                         "after": after})
    if seek_s is not None and abs(after["cursor_s"] - seek_s) > 1e-6:
        raise BridgeUnavailable("REAPER did not move the edit cursor to the requested position",
                                details={"requested_position_s": seek_s,
                                         "cursor_s": after["cursor_s"]})
    return {
        "action": action,
        "position_s": seek_s,
        "revision": revision.to_dict(),
        "before": before,
        "after": after,
        "verified": True,
        "commands": commands,
    }


def _transport_row(transport: object) -> dict[str, Any]:
    data = transport if isinstance(transport, Mapping) else {}
    play_state = data.get("play_state")
    if isinstance(play_state, bool) or not isinstance(play_state, int):
        play_state = 0
    return {
        "play_state": play_state,
        "playing": bool(play_state & 1),
        "paused": bool(play_state & 2),
        "recording": bool(play_state & 4),
        "cursor_s": _finite_number(data.get("cursor_s")),
        "play_position_s": _finite_number(data.get("play_position_s")),
    }


def _absolute_rpp(path: object) -> Path:
    if not isinstance(path, (str, os.PathLike)):
        raise ValidationError("path must be a string")
    target = Path(path).expanduser()
    if not target.is_absolute():
        raise ValidationError("path must be absolute", details={"path": str(target)})
    if target.suffix.lower() != ".rpp":
        raise ValidationError("path must end in .RPP", details={"path": str(target)})
    return target.resolve(strict=False)


def _norm_path(path: object) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def project_action(session: Any, action: str, *, path: object = None,
                   overwrite_existing: bool = False) -> dict[str, Any]:
    """Open or save the project; a save-as never overwrites without consent.

    The overwrite refusal happens before any bridge call, so an unconfirmed
    target cannot even be touched.  ``open`` verifies that the active project's
    path is the requested one and rebinds ``session.project_id``; a REAPER that
    kept the old tab open is reported instead of silently editing it.
    """
    if action not in {"open", "save", "save_as"}:
        raise ValidationError("project action must be open, save or save_as",
                              details={"action": action})
    if not isinstance(overwrite_existing, bool):
        raise ValidationError("overwrite_existing must be a boolean")
    if action == "open":
        if path is None:
            raise ValidationError("project(open) requires path")
        target = _absolute_rpp(path)
        if not target.is_file():
            raise ValidationError("Project file does not exist",
                                  details={"path": str(target)})
        session.bridge.call_api("Main_openProject", str(target))
        opened = session.bridge.project_context(None)
        revision = _revision_from_state(opened)
        opened_path = opened.get("path") or revision.path
        if _norm_path(opened_path) != _norm_path(target):
            raise BridgeUnavailable(
                "REAPER did not make the requested project active",
                details={"requested_path": str(target),
                         "active_path": str(opened_path or "")})
        session.project_id = revision.project_id
        return {"action": "open", "path": str(target),
                "project_id": revision.project_id,
                "revision": revision.to_dict(),
                "verified": "active_project_path"}

    if action == "save":
        if path is not None or overwrite_existing:
            raise ValidationError("path and overwrite_existing are only valid for save_as")
        result = session.save_current()
        if not isinstance(result, Mapping) or result.get("saved") is not True:
            raise BridgeUnavailable("REAPER did not confirm the project save",
                                    details={"result": result})
        return {"action": "save", **dict(result)}

    if path is None:
        raise ValidationError("project(save_as) requires path")
    target = _absolute_rpp(path)
    existed = target.exists()
    if existed and not overwrite_existing:
        raise ValidationError(
            "Save target already exists; pass overwrite_existing=True to replace it",
            details={"path": str(target), "overwrite_existing": False})
    result = session.save_as(str(target), overwrite_existing=overwrite_existing)
    return {"action": "save_as", "overwritten": existed,
            "path": str(target), **dict(result)}


def _read_setting(session: Any, handle: ReaperHandle, field: str) -> float:
    result = session.bridge.call_api("GetSetProjectInfo", handle, field, 0.0, False)
    value = _finite_number(_api_scalar(result, name="GetSetProjectInfo"))
    if value is None:
        raise BridgeUnavailable("REAPER returned a non-numeric project setting",
                                details={"field": field})
    return value


def _normalize_fields(fields: object) -> tuple[str, ...]:
    if fields is None:
        return DEFAULT_SETTING_FIELDS
    if isinstance(fields, str):
        fields = (fields,)
    if not isinstance(fields, Sequence) or isinstance(fields, (bytes, bytearray)):
        raise ValidationError("fields must be a string or a list of strings")
    result: list[str] = []
    for field in fields:
        if not isinstance(field, str) or not field:
            raise ValidationError("settings field names must be non-empty strings")
        if field not in DEFAULT_SETTING_FIELDS:
            raise ValidationError(
                "Unknown project setting field",
                details={"field": field, "allowed": list(DEFAULT_SETTING_FIELDS)})
        result.append(field)
    if not result:
        raise ValidationError("fields must not be empty")
    return tuple(result)


def settings_action(session: Any, action: str = "read", *, fields: object = None,
                    values: object = None) -> dict[str, Any]:
    """Read or write documented project settings with a read-back proof.

    Reads accept any subset of :data:`DEFAULT_SETTING_FIELDS`.  Writes accept
    only :data:`WRITABLE_SETTING_FIELDS`; each value is read before, written,
    read again, and the whole call raises ``transaction_failed`` if REAPER did
    not apply every requested value.
    """
    if action not in {"read", "write"}:
        raise ValidationError("settings action must be read or write")
    if action == "read":
        if values is not None:
            raise ValidationError("values are only valid for settings(write)")
        chosen = _normalize_fields(fields)
        handle, revision = _read_handle(session)
        settings = {field: _read_setting(session, handle, field) for field in chosen}
        return {"action": "read", "fields": list(chosen), "settings": settings,
                "revision": revision.to_dict()}

    if fields is not None:
        raise ValidationError("fields are only valid for settings(read)")
    if not isinstance(values, Mapping) or not values:
        raise ValidationError("settings(write) requires a non-empty values object")
    requested: dict[str, float] = {}
    for field, value in values.items():
        if not isinstance(field, str) or field not in WRITABLE_SETTING_FIELDS:
            raise ValidationError(
                "Setting is not writable through this operation",
                details={"field": field, "allowed": sorted(WRITABLE_SETTING_FIELDS)})
        number = _finite_number(value)
        if number is None:
            raise ValidationError("Setting values must be finite numbers",
                                  details={"field": field, "value": value})
        requested[field] = number

    before: dict[str, float] = {}
    after: dict[str, float] = {}
    mismatched: list[dict[str, Any]] = []
    revision = _read_revision(session)
    for field, value in requested.items():
        handle = _handle(revision)
        before[field] = _read_setting(session, handle, field)
        session.bridge.call_api("GetSetProjectInfo", handle, field, value, True)
        revision = _read_revision(session)
        after[field] = _read_setting(session, _handle(revision), field)
        if abs(after[field] - value) > 1e-6:
            mismatched.append({"field": field, "requested": value,
                               "actual": after[field]})
    report = {"action": "write", "fields": list(requested), "before": before,
              "after": after, "matched": not mismatched,
              "mismatched": mismatched, "revision": revision.to_dict()}
    if mismatched:
        raise TransactionFailed("REAPER did not apply every project setting",
                                details=report)
    return report


def reaper_window_focused() -> bool | None:
    """Whether a REAPER window currently owns the foreground, if knowable."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        user32 = ctypes.windll.user32
        handle = user32.GetForegroundWindow()
        if not handle:
            return False
        length = user32.GetWindowTextLengthW(handle)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        return "REAPER" in buffer.value
    except Exception:  # noqa: BLE001 - focus is descriptive on this host
        return None


def _default_launch(project_path: str | None) -> None:
    executable = Path(os.environ.get("REAPER_EXECUTABLE",
                                     r"C:/Program Files/REAPER (x64)/reaper.exe"))
    if not executable.is_file():
        raise ValidationError("REAPER executable not found",
                              details={"path": str(executable),
                                       "env": "REAPER_EXECUTABLE"})
    command = [str(executable)]
    if project_path:
        command.append(str(project_path))
    subprocess.Popen(command)


def _default_terminate() -> None:
    if os.name != "nt":
        raise ApiUnavailable("Stopping REAPER is only implemented on Windows")
    result = subprocess.run(["taskkill", "/IM", "reaper.exe", "/F"],
                            capture_output=True, text=True, check=False)
    if result.returncode not in (0, 128):  # 128: not running
        raise ApiUnavailable("taskkill could not stop REAPER",
                             details={"returncode": result.returncode,
                                      "stderr": (result.stderr or "")[:500]})


def _live(probe: Callable[[], Mapping[str, Any] | None]) -> Mapping[str, Any] | None:
    try:
        value = probe()
    except Exception:  # noqa: BLE001 - "not answering" is the signal we need
        return None
    return value if isinstance(value, Mapping) else None


def app_action(bridge: Any, action: str, *, project_path: object = None,
               timeout_s: float = 30.0, force: bool = False,
               health: Callable[[], Mapping[str, Any] | None] | None = None,
               launch: Callable[[str | None], None] | None = None,
               terminate: Callable[[], None] | None = None,
               focus: Callable[[], bool] | None = None,
               sleep: Callable[[float], None] = time.sleep,
               clock: Callable[[], float] = time.monotonic,
               poll_s: float = 0.5) -> dict[str, Any]:
    """Ensure REAPER/bridge is up, restart it, or focus its window.

    ``restart`` refuses to discard unsaved work unless ``force=True``.  It
    proves both halves of the cycle: the old session stops answering and a
    *different* bridge session answers afterwards.  The launcher, terminator
    and probe are parameters so this can be unit-tested without a host.
    """
    if action not in {"ensure", "restart", "focus"}:
        raise ValidationError("app action must be ensure, restart or focus",
                              details={"action": action})
    timeout = _require_finite(timeout_s, "timeout_s")
    if timeout <= 0:
        raise ValidationError("timeout_s must be positive")
    interval = _require_finite(poll_s, "poll_s")
    if interval <= 0:
        raise ValidationError("poll_s must be positive")
    if not isinstance(force, bool):
        raise ValidationError("force must be a boolean")
    if project_path is not None and not isinstance(project_path, (str, os.PathLike)):
        raise ValidationError("project_path must be a string or None")

    if focus is not None and action != "focus":
        raise ValidationError("focus is only valid for app(focus)")
    if action == "focus":
        focus_fn = focus or focus_reaper_window
        focused = bool(focus_fn())
        return {"ok": focused, "action": "focus", "focused": focused}

    probe = health or (lambda: bridge.health(timeout_s=3.0))
    launch_fn = launch or _default_launch
    path = str(project_path) if project_path is not None else None
    live = _live(probe)

    if action == "ensure":
        if live:
            return _running("ensure", live, started=False)
        if not path:
            raise ValidationError(
                "ensure needs project_path when no bridge is answering",
                details={"next_action": "pass project_path so REAPER can open it"})
        launch_fn(path)
        deadline = clock() + timeout
        while clock() < deadline:
            sleep(interval)
            live = _live(probe)
            if live:
                return _running("ensure", live, started=True)
        raise BridgeUnavailable("REAPER did not expose a bridge before the deadline",
                                details={"project_path": path})

    previous = live.get("bridge_session") if live else None
    project = live.get("project") if isinstance(live, Mapping) else None
    project = project if isinstance(project, Mapping) else {}
    if live and not force:
        project_id = project.get("project_id")
        if isinstance(project_id, str) and project_id:
            state = bridge.call("project.render_settings.read",
                                {"project_id": project_id})
            if isinstance(state, Mapping) and state.get("dirty"):
                raise ValidationError(
                    "The project has unsaved changes; save it or pass force=True",
                    details={"project_id": project_id,
                             "project_path": project.get("path")})
    if path is None:
        path = project.get("path") if isinstance(project.get("path"), str) else None
    if live and not path:
        # Refuse before stopping anything: an unsaved project has no path to
        # reopen, and killing REAPER first would make the error destructive.
        raise ValidationError(
            "restart needs project_path when the open project has no saved path",
            details={"project_id": project.get("project_id")})

    deadline = clock() + timeout
    if live:
        terminate_fn = terminate or _default_terminate
        terminate_fn()
        while clock() < deadline:
            sleep(interval)
            if _live(probe) is None:
                break
        else:
            raise BridgeUnavailable("REAPER did not stop before the deadline")
    if not path:
        raise ValidationError(
            "restart needs project_path when no open project path is known")
    launch_fn(path)
    while clock() < deadline:
        sleep(interval)
        live = _live(probe)
        if live and live.get("bridge_session") != previous:
            return _running("restart", live, started=True,
                            previous_session=previous)
    raise BridgeUnavailable("REAPER did not expose a new bridge session before the deadline",
                            details={"project_path": path,
                                     "previous_session": previous})


def _running(action: str, live: Mapping[str, Any], *, started: bool,
             previous_session: str | None = None) -> dict[str, Any]:
    project = live.get("project")
    project = project if isinstance(project, Mapping) else {}
    result = {
        "ok": True,
        "action": action,
        "started": started,
        "bridge_session_id": live.get("bridge_session"),
        "bridge_build": live.get("bridge_build"),
        "project_id": project.get("project_id"),
        "path": project.get("path"),
        "reaper_version": live.get("reaper_version"),
    }
    if previous_session is not None:
        result["previous_session_id"] = previous_session
    return result


def _split_since(since: object) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if isinstance(since, ProjectRevision):
        return since.to_dict(), None
    if not isinstance(since, Mapping):
        raise ValidationError("since must be a revision object or ProjectRevision")
    if "revision" in since:
        revision = since.get("revision")
        transport = since.get("transport")
    else:
        revision = since
        transport = None
    if not isinstance(revision, Mapping):
        raise ValidationError("since.revision must be an object")
    normalized = dict(revision)
    project_id = normalized.get("project_id")
    change_count = normalized.get("change_count")
    if not isinstance(project_id, str) or not project_id:
        raise ValidationError("since revision needs a project_id")
    if isinstance(change_count, bool) or not isinstance(change_count, int) or change_count < 0:
        raise ValidationError("since revision needs a non-negative change_count")
    if transport is not None and not isinstance(transport, Mapping):
        raise ValidationError("since.transport must be an object")
    return normalized, dict(transport) if isinstance(transport, Mapping) else None


def _revision_changed(before: Mapping[str, Any],
                      after: Mapping[str, Any]) -> bool:
    if str(before.get("project_id")) != str(after.get("project_id")):
        return True
    if before.get("change_count") != after.get("change_count"):
        return True
    for key in ("path", "instance_id", "epoch"):
        # A legacy ``since`` may not know the component; then it cannot be
        # used to claim continuity, and it is not used to claim a change either.
        if before.get(key) is None:
            continue
        if before.get(key) != after.get(key):
            return True
    return False


def _transport_changed(before: Mapping[str, Any] | None,
                       after: Mapping[str, Any] | None) -> bool:
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return False
    for key in ("play_state", "cursor_s", "play_position_s"):
        if before.get(key) != after.get(key):
            return True
    return False


def _watch_set(watch: object) -> tuple[str, ...]:
    if isinstance(watch, str):
        watch = (watch,)
    if not isinstance(watch, Sequence) or isinstance(watch, (bytes, bytearray)):
        raise ValidationError("watch must be a string or a list of strings")
    selected: list[str] = []
    for kind in watch:
        if kind not in _WATCH_KINDS:
            raise ValidationError("watch entries must be revision or transport",
                                  details={"kind": kind, "allowed": list(_WATCH_KINDS)})
        if kind not in selected:
            selected.append(kind)
    if not selected:
        raise ValidationError("watch must contain at least one kind")
    return tuple(selected)


def _read_wait_state(session: Any, watch: tuple[str, ...]) -> dict[str, Any]:
    revision = _read_revision(session)
    state: dict[str, Any] = {"revision": revision.to_dict()}
    if "transport" in watch:
        context = session.snapshot()
        state["transport"] = dict(context.get("transport") or {})
    return state


def wait_for_change(session: Any, since: object, timeout_s: float = 30.0, *,
                    poll_s: float = 0.25, watch: object = ("revision", "transport"),
                    clock: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], None] = time.sleep,
                    read_state: Callable[[tuple[str, ...]], Mapping[str, Any]] | None = None
                    ) -> dict[str, Any]:
    """Block until the revision (or transport) differs from ``since``.

    One long poll instead of a caller-side health loop.  ``since`` is either a
    status ``revision`` object, a ``ProjectRevision``, or
    ``{"revision": ..., "transport": ...}``.  A revision that cannot be
    compared is refused; a timeout returns ``changed: False`` with the last
    observed state, never an exception.
    """
    timeout = _require_finite(timeout_s, "timeout_s")
    if timeout <= 0:
        raise ValidationError("timeout_s must be positive")
    interval = _require_finite(poll_s, "poll_s")
    if interval <= 0:
        raise ValidationError("poll_s must be positive")
    watched = _watch_set(watch)
    baseline_revision, baseline_transport = _split_since(since)
    reader = read_state or (lambda kinds: _read_wait_state(session, kinds))

    start = clock()
    state = reader(watched)
    current_revision = state.get("revision") if isinstance(state, Mapping) else None
    current_transport = state.get("transport") if isinstance(state, Mapping) else None
    if not isinstance(current_revision, Mapping):
        raise BridgeUnavailable("REAPER returned no revision while waiting for a change")
    if baseline_transport is None and "transport" in watched:
        baseline_transport = (dict(current_transport)
                              if isinstance(current_transport, Mapping) else None)
    polls = 0

    def outcome(changed: bool, reason: str) -> dict[str, Any]:
        return {
            "changed": changed,
            "reason": reason,
            "since": {"revision": baseline_revision, "transport": baseline_transport},
            "revision": dict(current_revision) if isinstance(current_revision, Mapping) else None,
            "transport": (dict(current_transport)
                          if isinstance(current_transport, Mapping) else None),
            "elapsed_s": clock() - start,
            "polls": polls,
            "timeout_s": timeout,
            "watch": list(watched),
        }

    revision_moved = _revision_changed(baseline_revision, current_revision)
    transport_moved = ("transport" in watched and
                       _transport_changed(baseline_transport, current_transport))
    if revision_moved or transport_moved:
        return outcome(True, _reason(revision_moved, transport_moved))

    deadline = start + timeout
    while clock() < deadline:
        sleep(min(interval, max(0.0, deadline - clock())))
        polls += 1
        state = reader(watched)
        current_revision = state.get("revision") if isinstance(state, Mapping) else None
        current_transport = state.get("transport") if isinstance(state, Mapping) else None
        if not isinstance(current_revision, Mapping):
            raise BridgeUnavailable("REAPER returned no revision while waiting for a change")
        revision_moved = _revision_changed(baseline_revision, current_revision)
        transport_moved = ("transport" in watched and
                           _transport_changed(baseline_transport, current_transport))
        if revision_moved or transport_moved:
            return outcome(True, _reason(revision_moved, transport_moved))
    return outcome(False, "timeout")


def _reason(revision_moved: bool, transport_moved: bool) -> str:
    if revision_moved and transport_moved:
        return "revision+transport"
    return "revision" if revision_moved else "transport"


__all__ = [
    "DEFAULT_SETTING_FIELDS",
    "TRANSPORT_COMMANDS",
    "WRITABLE_SETTING_FIELDS",
    "app_action",
    "project_action",
    "reaper_window_focused",
    "settings_action",
    "transport_control",
    "wait_for_change",
]
