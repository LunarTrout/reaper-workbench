"""Transport/project/settings/app/wait tests against a fake bridge, no REAPER."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from reaper_audio.daw.session import ProjectSession
from reaper_audio.daw.session_control import app_action, wait_for_change
from reaper_audio.errors import (
    BridgeUnavailable,
    TransactionFailed,
    ValidationError,
)
from reaper_audio.reaper.api_catalog import ApiCallResult

FIXTURES = Path(__file__).parent / "fixtures"


def _context() -> dict:
    return json.loads((FIXTURES / "session_context.json").read_text(encoding="utf-8"))


def _digest() -> dict:
    return json.loads((FIXTURES / "session_digest.json").read_text(encoding="utf-8"))


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


class ControlBridge:
    def __init__(self, *, ignore_writes: bool = False,
                 ignore_transport: bool = False, ignore_open: bool = False):
        self.context = copy.deepcopy(_context())
        self.digest_payload = copy.deepcopy(_digest())
        self.revision = dict(self.context["revision"])
        self.revision_sequence: list[int] | None = None
        self.revision_reads = 0
        self.cursor_sequence: list[float] | None = None
        self.snapshot_reads = 0
        self.calls: list[tuple[str, dict]] = []
        self.api_calls: list[tuple[str, tuple]] = []
        self.save_calls: list[dict] = []
        self.settings: dict[str, float] = {
            "RENDER_SETTINGS": 0.0, "RENDER_TAILMS": 0.0,
            "PROJECT_SRATE": 48000.0, "PROJECT_TIMEBASE": 1.0,
        }
        self.ignore_writes = ignore_writes
        self.ignore_transport = ignore_transport
        self.ignore_open = ignore_open
        self.play_state = 0
        self.cursor = 0.0
        self.play_position = 0.0
        self.active_path = self.context["path"]
        self.project_id = self.revision["project_id"]

    # -- bridge surface -------------------------------------------------
    def project_context(self, project_id=None, **options):
        self.calls.append(("project.context", {"project_id": project_id, **options}))
        context = copy.deepcopy(self.context)
        context["revision"] = dict(self.revision)
        context["path"] = self.active_path
        context["transport"] = {"play_state": self.play_state,
                                "cursor_s": self.cursor,
                                "edit_cursor_s": self.cursor,
                                "play_position_s": self.play_position}
        if self.cursor_sequence is not None:
            index = min(self.snapshot_reads, len(self.cursor_sequence) - 1)
            context["transport"]["cursor_s"] = self.cursor_sequence[index]
            self.snapshot_reads += 1
        return context

    def project_digest(self, project_id=None):
        return copy.deepcopy(self.digest_payload)

    def health(self, timeout_s: float = 1.0):
        return {"bridge_session": "6a0b1c2d-0001", "bridge_build": "test-build",
                "project": {"project_id": self.project_id,
                            "change_count": self.revision["change_count"],
                            "path": self.active_path}}

    def call(self, operation: str, arguments: dict | None = None,
             timeout_s: float = 10.0):
        arguments = dict(arguments or {})
        self.calls.append((operation, arguments))
        if operation == "project.render_settings.read":
            self.revision_reads += 1
            if self.revision_sequence:
                index = min(self.revision_reads - 1,
                            len(self.revision_sequence) - 1)
                self.revision["change_count"] = self.revision_sequence[index]
            return {"project_id": self.project_id,
                    "revision": dict(self.revision), "dirty": 0}
        raise AssertionError(f"unexpected bridge operation: {operation}")

    def call_api(self, name: str, *values, timeout_s: float = 10.0):
        self.api_calls.append((name, values))
        if name == "SetEditCurPos2":
            self.cursor = float(values[1])
            return ApiCallResult(name, (), {})
        if name == "Main_OnCommandEx":
            if not self.ignore_transport:
                command = int(values[0])
                self.play_state = {1007: 1, 1016: 0, 1008: 2}[command]
            return ApiCallResult(name, (), {})
        if name == "GetPlayStateEx":
            return ApiCallResult(name, (self.play_state,), {})
        if name == "GetCursorPositionEx":
            return ApiCallResult(name, (self.cursor,), {})
        if name == "GetPlayPositionEx":
            return ApiCallResult(name, (self.play_position,), {})
        if name == "GetSetProjectInfo":
            field = str(values[1])
            is_set = bool(values[3])
            if is_set and not self.ignore_writes:
                self.settings[field] = float(values[2])
            return ApiCallResult(name, (self.settings.get(field, 0.0),), {})
        if name == "Main_openProject":
            if not self.ignore_open:
                self.active_path = str(values[0])
            return ApiCallResult(name, (), {})
        if name == "MIDI_CountEvts":
            return ApiCallResult(name, (True, 64, 0, 0), {})
        raise AssertionError(f"unexpected API call: {name}")

    def save_project(self, mode: str, *, project_id=None, path=None,
                     overwrite_existing: bool = False, timeout_s: float = 30.0):
        self.save_calls.append({"mode": mode, "path": path,
                                "overwrite_existing": overwrite_existing})
        if mode == "save_as" and path is not None:
            self.active_path = str(path)
        return {"saved": True, "mode": mode, "path": path or self.active_path,
                "project_id_after": self.project_id}


def _session(bridge: ControlBridge) -> ProjectSession:
    return ProjectSession(bridge, project_id=bridge.project_id)


def test_transport_play_seeks_then_verifies_the_read_back() -> None:
    bridge = ControlBridge()
    result = _session(bridge).transport("play", position_s=12.0)

    assert result["action"] == "play"
    assert result["before"]["playing"] is False
    assert result["after"] == {"play_state": 1, "playing": True, "paused": False,
                               "recording": False, "cursor_s": 12.0,
                               "play_position_s": 0.0}
    assert result["verified"] is True
    names = [name for name, _ in bridge.api_calls]
    assert names[:2] == ["SetEditCurPos2", "Main_OnCommandEx"]
    assert bridge.api_calls[0][1][1] == 12.0
    assert bridge.api_calls[1][1][0] == 1007


def test_transport_stop_reports_a_stopped_state() -> None:
    bridge = ControlBridge()
    bridge.play_state = 1
    result = _session(bridge).transport("stop")

    assert result["after"]["playing"] is False
    assert result["after"]["play_state"] == 0


def test_transport_raises_when_reaper_did_not_start_playback() -> None:
    bridge = ControlBridge(ignore_transport=True)
    with pytest.raises(BridgeUnavailable):
        _session(bridge).transport("play")


def test_transport_rejects_an_unknown_action() -> None:
    with pytest.raises(ValidationError):
        _session(ControlBridge()).transport("record")


def test_save_as_without_overwrite_consent_refuses_before_any_bridge_call(
        tmp_path) -> None:
    bridge = ControlBridge()
    target = tmp_path / "mix.rpp"
    target.write_text("existing", encoding="utf-8")

    with pytest.raises(ValidationError) as error:
        _session(bridge).project("save_as", path=str(target))

    assert "overwrite_existing=True" in str(error.value)
    assert bridge.save_calls == []


def test_save_as_overwrites_when_explicitly_authorized(tmp_path) -> None:
    bridge = ControlBridge()
    target = tmp_path / "mix.rpp"
    target.write_text("existing", encoding="utf-8")

    result = _session(bridge).project("save_as", path=str(target),
                                      overwrite_existing=True)

    assert result["saved"] is True
    assert result["overwritten"] is True
    assert bridge.save_calls == [{"mode": "save_as", "path": str(target),
                                  "overwrite_existing": True}]


def test_save_as_accepts_a_new_path_without_consent(tmp_path) -> None:
    bridge = ControlBridge()
    target = tmp_path / "new_mix.rpp"

    result = _session(bridge).project("save_as", path=str(target))

    assert result["overwritten"] is False
    assert bridge.save_calls[0]["overwrite_existing"] is False


def test_project_rejects_relative_and_non_rpp_paths(tmp_path) -> None:
    bridge = ControlBridge()
    with pytest.raises(ValidationError):
        _session(bridge).project("save_as", path="relative/mix.rpp")
    with pytest.raises(ValidationError):
        _session(bridge).project("save_as", path=str(tmp_path / "mix.wav"))


def test_project_open_binds_the_active_project_and_verifies_its_path(tmp_path) -> None:
    bridge = ControlBridge()
    target = tmp_path / "opened.rpp"
    target.write_text("<REAPER_PROJECT 0.1", encoding="utf-8")

    result = _session(bridge).project("open", path=str(target))

    assert result["verified"] == "active_project_path"
    assert result["project_id"] == bridge.project_id
    assert bridge.api_calls[0] == ("Main_openProject", (str(target),))


def test_project_open_refuses_when_reaper_kept_another_tab_active(tmp_path) -> None:
    bridge = ControlBridge(ignore_open=True)
    target = tmp_path / "opened.rpp"
    target.write_text("<REAPER_PROJECT 0.1", encoding="utf-8")

    with pytest.raises(BridgeUnavailable):
        _session(bridge).project("open", path=str(target))


def test_settings_read_returns_the_documented_fields() -> None:
    result = _session(ControlBridge()).settings("read",
                                                fields=["RENDER_TAILMS", "PROJECT_SRATE"])

    assert result["action"] == "read"
    assert result["settings"] == {"RENDER_TAILMS": 0.0, "PROJECT_SRATE": 48000.0}
    assert result["revision"]["project_id"] == "reaper:demo-test"


def test_settings_write_reports_before_and_after_with_a_match() -> None:
    bridge = ControlBridge()
    result = _session(bridge).settings("write", values={"RENDER_TAILMS": 500})

    assert result["before"] == {"RENDER_TAILMS": 0.0}
    assert result["after"] == {"RENDER_TAILMS": 500.0}
    assert result["matched"] is True
    assert result["revision"]["change_count"] == 41


def test_settings_write_raises_when_reaper_ignored_the_value() -> None:
    bridge = ControlBridge(ignore_writes=True)
    with pytest.raises(TransactionFailed) as error:
        _session(bridge).settings("write", values={"RENDER_TAILMS": 500})

    assert error.value.details["mismatched"] == [
        {"field": "RENDER_TAILMS", "requested": 500.0, "actual": 0.0}]


def test_settings_rejects_unknown_fields_and_read_only_writes() -> None:
    session = _session(ControlBridge())
    with pytest.raises(ValidationError):
        session.settings("read", fields=["NOT_A_SETTING"])
    with pytest.raises(ValidationError):
        session.settings("write", values={"DIRTY": 0})


def test_wait_for_change_returns_on_the_next_revision() -> None:
    clock = FakeClock()
    bridge = ControlBridge()
    bridge.revision_sequence = [41, 41, 42]

    result = wait_for_change(
        _session(bridge),
        {"project_id": bridge.project_id, "change_count": 41},
        timeout_s=2.0, poll_s=0.5, watch=("revision",),
        clock=clock.monotonic, sleep=clock.sleep)

    assert result["changed"] is True
    assert result["reason"] == "revision"
    assert result["revision"]["change_count"] == 42
    assert result["polls"] == 2
    assert result["elapsed_s"] == 1.0


def test_wait_for_change_returns_on_a_transport_move() -> None:
    clock = FakeClock()
    bridge = ControlBridge()
    bridge.cursor_sequence = [12.5, 12.5, 40.0]

    result = wait_for_change(
        _session(bridge),
        {"project_id": bridge.project_id, "change_count": 41},
        timeout_s=2.0, poll_s=0.5, watch=("revision", "transport"),
        clock=clock.monotonic, sleep=clock.sleep)

    assert result["changed"] is True
    assert result["reason"] == "transport"
    assert result["transport"]["cursor_s"] == 40.0
    assert result["polls"] == 2


def test_wait_for_change_times_out_without_raising() -> None:
    clock = FakeClock()
    bridge = ControlBridge()
    bridge.revision_sequence = [41]

    result = wait_for_change(
        _session(bridge),
        {"project_id": bridge.project_id, "change_count": 41},
        timeout_s=1.0, poll_s=0.25, watch=("revision",),
        clock=clock.monotonic, sleep=clock.sleep)

    assert result["changed"] is False
    assert result["reason"] == "timeout"
    assert result["polls"] == 4
    assert result["revision"]["change_count"] == 41


def test_wait_for_change_validates_since_and_timeout() -> None:
    session = _session(ControlBridge())
    with pytest.raises(ValidationError):
        session.wait_for_change({"change_count": 41}, timeout_s=1.0)
    with pytest.raises(ValidationError):
        session.wait_for_change({"project_id": "p", "change_count": 1},
                                timeout_s=0)


class AppBridge:
    def __init__(self, *, running: bool = True, dirty: int = 0):
        self.running = running
        self.session = "session-1"
        self.dirty = dirty
        self.path = "C:/Projects/demo/demo.rpp"
        self.terminated = False
        self.launch_paths: list[str | None] = []

    def probe(self):
        if not self.running:
            return None
        return {"bridge_session": self.session, "bridge_build": "b1",
                "reaper_version": "7.85",
                "project": {"project_id": "reaper:demo-test",
                            "change_count": 7, "path": self.path}}

    def health(self, timeout_s: float = 1.0):
        state = self.probe()
        if state is None:
            raise BridgeUnavailable("not running")
        return state

    def call(self, operation: str, arguments: dict | None = None,
             timeout_s: float = 10.0):
        if operation == "project.render_settings.read":
            return {"dirty": self.dirty,
                    "revision": {"project_id": "reaper:demo-test",
                                 "change_count": 7, "path": self.path}}
        raise AssertionError(operation)

    def launch(self, path=None):
        self.launch_paths.append(path)
        self.running = True
        self.session = "session-2"

    def terminate(self):
        self.terminated = True
        self.running = False


def test_app_ensure_launches_reaper_when_no_bridge_answers() -> None:
    clock = FakeClock()
    bridge = AppBridge(running=False)

    result = app_action(bridge, "ensure", project_path="C:/Projects/demo/demo.rpp",
                        timeout_s=2.0, health=bridge.probe, launch=bridge.launch,
                        clock=clock.monotonic, sleep=clock.sleep, poll_s=0.5)

    assert result["ok"] is True
    assert result["started"] is True
    assert result["bridge_session_id"] == "session-2"
    assert bridge.launch_paths == ["C:/Projects/demo/demo.rpp"]


def test_app_ensure_reports_an_already_running_bridge() -> None:
    bridge = AppBridge(running=True)
    result = app_action(bridge, "ensure", health=bridge.probe, launch=bridge.launch)

    assert result["started"] is False
    assert result["bridge_session_id"] == "session-1"
    assert bridge.launch_paths == []


def test_app_restart_refuses_to_discard_unsaved_work() -> None:
    bridge = AppBridge(running=True, dirty=1)

    with pytest.raises(ValidationError) as error:
        app_action(bridge, "restart", health=bridge.probe,
                   terminate=bridge.terminate, launch=bridge.launch, force=False)

    assert "unsaved changes" in str(error.value)
    assert bridge.terminated is False


def test_app_restart_waits_for_a_different_session() -> None:
    clock = FakeClock()
    bridge = AppBridge(running=True, dirty=0)

    result = app_action(bridge, "restart", timeout_s=3.0, health=bridge.probe,
                        terminate=bridge.terminate, launch=bridge.launch,
                        clock=clock.monotonic, sleep=clock.sleep, poll_s=0.5)

    assert result["started"] is True
    assert result["previous_session_id"] == "session-1"
    assert result["bridge_session_id"] == "session-2"
    assert bridge.terminated is True
    assert bridge.launch_paths == [bridge.path]


def test_app_focus_uses_the_injected_focus_probe() -> None:
    result = app_action(AppBridge(), "focus", focus=lambda: True)
    assert result == {"ok": True, "action": "focus", "focused": True}


def test_app_restart_needs_a_path_when_no_project_is_open() -> None:
    bridge = AppBridge(running=False)
    with pytest.raises(ValidationError):
        app_action(bridge, "restart", health=bridge.probe,
                   terminate=bridge.terminate, launch=bridge.launch)
