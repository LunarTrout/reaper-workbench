import json
import time
from pathlib import Path
from threading import Thread

import pytest

from reaper_audio.errors import BridgeTimeout, BridgeUnavailable, ValidationError
from reaper_audio.reaper.bridge_client import ReaperBridge
from reaper_audio.reaper.handles import ReaperHandle


def test_bridge_round_trip_uses_request_id_and_atomic_mailbox(tmp_path) -> None:
    bridge = ReaperBridge(tmp_path, poll_s=0.005)

    def fake_reaper() -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            request_path = tmp_path / "request.json"
            if request_path.is_file():
                request = json.loads(request_path.read_text(encoding="utf-8"))
                request_path.unlink()
                response_path = tmp_path / "responses" / f"{request['id']}.json"
                response_path.write_text(json.dumps({
                    "id": request["id"], "ok": True,
                    "result": {"operation": request["operation"],
                               "arguments": request["arguments"]},
                }), encoding="utf-8")
                return
            time.sleep(0.005)
        raise AssertionError("test responder did not receive a request")

    responder = Thread(target=fake_reaper)
    responder.start()
    result = bridge.call("health", {"probe": "связь"}, timeout_s=1.0)
    responder.join(timeout=1.0)

    assert result == {"operation": "health", "arguments": {"probe": "связь"}}
    assert not (tmp_path / "request.json").exists()
    assert not (tmp_path / "client.lock").exists()


def test_bridge_timeout_removes_unconsumed_request(tmp_path) -> None:
    bridge = ReaperBridge(tmp_path, poll_s=0.005)

    with pytest.raises(BridgeTimeout):
        bridge.call("health", timeout_s=0.05)

    assert not (tmp_path / "request.json").exists()
    assert not (tmp_path / "client.lock").exists()


def test_call_api_sends_catalog_types_and_decodes_host_handle(tmp_path, monkeypatch) -> None:
    bridge = ReaperBridge(tmp_path)
    project = ReaperHandle(kind="ReaProject", project_id="project-1", change_count=7,
                           stable_id="project-1")
    observed = {}

    def fake_call(operation, arguments=None, *, timeout_s=10.0):
        observed["operation"] = operation
        observed["arguments"] = arguments
        return {
            "returns": [{"$reaper_handle": {
                "kind": "MediaTrack", "project_id": "project-1",
                "change_count": 7, "stable_id": "{TRACK-GUID}",
            }}],
            "arrays": {},
            "project_before": {"project_id": "project-1", "change_count": 7},
            "project_after": {"project_id": "project-1", "change_count": 7},
        }

    monkeypatch.setattr(bridge, "call", fake_call)
    result = bridge.call_api("GetTrack", project, 0)

    assert observed["operation"] == "api.call"
    assert observed["arguments"]["parameter_types"] == [
        {"type": "ReaProject", "host_handle": True, "optional": False},
        {"type": "integer", "host_handle": False, "optional": False},
    ]
    assert observed["arguments"]["args"][0] == project.to_wire()
    assert isinstance(result.returns[0], ReaperHandle)
    assert result.returns[0].kind == "MediaTrack"
    assert result.returns[0].stable_id == "{TRACK-GUID}"


def test_call_api_rejects_wrong_handle_and_scalar_types_before_bridge_call(tmp_path,
                                                                           monkeypatch) -> None:
    bridge = ReaperBridge(tmp_path)
    invoked = False

    def forbidden_call(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("invalid call must be rejected locally")

    monkeypatch.setattr(bridge, "call", forbidden_call)
    track = ReaperHandle(kind="MediaTrack", project_id="project-1", change_count=7,
                         stable_id="{TRACK-GUID}")

    with pytest.raises(ValidationError, match="handle type"):
        bridge.call_api("GetTrack", track, 0)
    with pytest.raises(ValidationError, match="integer argument"):
        bridge.call_api("GetTrack", ReaperHandle(
            kind="ReaProject", project_id="project-1", change_count=7,
            stable_id="project-1"), True)

    assert not invoked


def test_project_context_targets_explicit_project(tmp_path, monkeypatch) -> None:
    bridge = ReaperBridge(tmp_path)
    observed = {}

    def fake_call(operation, arguments=None, *, timeout_s=10.0):
        observed["operation"] = operation
        observed["arguments"] = arguments
        return {"revision": {"project_id": "project-2", "change_count": 3}}

    monkeypatch.setattr(bridge, "call", fake_call)
    context = bridge.project_context("project-2", include_items=True)

    assert context["revision"]["project_id"] == "project-2"
    assert observed["operation"] == "project.context"
    assert observed["arguments"]["project_id"] == "project-2"
    assert observed["arguments"]["include_items"] is True


def test_capture_render_validates_track_and_master_files(tmp_path, monkeypatch) -> None:
    bridge = ReaperBridge(tmp_path)
    revision = {"project_id": "project-1", "change_count": 8, "path": "C:/mix.rpp"}
    track_path = tmp_path / "track.wav"
    master_path = tmp_path / "master.wav"
    track_path.write_bytes(b"track render" + bytes(64))
    master_path.write_bytes(b"master render" + bytes(64))
    observed = {}

    def fake_call(operation, arguments=None, *, timeout_s=10.0):
        observed["operation"] = operation
        observed["arguments"] = arguments
        return {
            "project_id": "project-1",
            "revision": {**revision, "identity_basis": "master_track_guid_and_project_path"},
            "revision_after": revision,
            "capture_point": "native_render_track_stems_plus_master",
            "files": [
                {"source_guid": "{TRACK}", "name": "Guitar", "track_index": 0,
                 "is_master": False, "path": str(track_path),
                 "bytes": track_path.stat().st_size},
                {"source_guid": "{MASTER}", "name": "MASTER", "track_index": -1,
                 "is_master": True, "path": str(master_path),
                 "bytes": master_path.stat().st_size},
            ],
        }

    monkeypatch.setattr(bridge, "call", fake_call)
    result = bridge.capture_render(expected_revision=revision, output_dir=tmp_path,
                                   job_id="job-1", expected_track_count=1)

    assert len(result["files"]) == 2
    assert observed["operation"] == "project.capture_render"
    assert observed["arguments"]["job_id"] == "job-1"
    assert observed["arguments"]["output_dir"] == str(tmp_path.resolve())


def test_capture_render_rejects_incomplete_render_set(tmp_path, monkeypatch) -> None:
    bridge = ReaperBridge(tmp_path)
    revision = {"project_id": "project-1", "change_count": 8, "path": "C:/mix.rpp"}
    audio_path = tmp_path / "master.wav"
    audio_path.write_bytes(b"master render" + bytes(64))
    monkeypatch.setattr(bridge, "call", lambda *args, **kwargs: {
        "project_id": "project-1", "revision": revision,
        "revision_after": revision,
        "capture_point": "native_render_track_stems_plus_master",
        "files": [{"source_guid": "{MASTER}", "name": "MASTER", "track_index": -1,
                   "is_master": True, "path": str(audio_path),
                   "bytes": audio_path.stat().st_size}],
    })

    with pytest.raises(BridgeUnavailable, match="incomplete track and master"):
        bridge.capture_render(expected_revision=revision, output_dir=tmp_path,
                              job_id="job-2", expected_track_count=1)


def test_save_as_requires_explicit_noncolliding_destination(tmp_path, monkeypatch) -> None:
    bridge = ReaperBridge(tmp_path)
    target = tmp_path / "new-project.rpp"
    observed = {}

    def fake_call(operation, arguments=None, *, timeout_s=10.0):
        observed["operation"] = operation
        observed["arguments"] = arguments
        if Path(arguments["path"]).exists() and not arguments["overwrite_existing"]:
            raise ValidationError("Save target already exists; overwrite was not authorized")
        return {"saved": True, "path": arguments["path"]}

    monkeypatch.setattr(bridge, "call", fake_call)
    result = bridge.save_project("save_as", path=target)

    assert result["saved"] is True
    assert observed["operation"] == "project.save"
    assert observed["arguments"]["path"] == str(target.resolve())
    assert observed["arguments"]["overwrite_existing"] is False

    target.write_text("protected", encoding="utf-8")
    with pytest.raises(ValidationError, match="already exists"):
        bridge.save_project("save_as", path=target)
    assert target.read_text(encoding="utf-8") == "protected"


def test_project_save_current_rejects_save_as_options(tmp_path) -> None:
    bridge = ReaperBridge(tmp_path)
    with pytest.raises(ValidationError, match="only valid for save_as"):
        bridge.save_project("current", path=tmp_path / "ignored.rpp")


def test_default_bridge_discovers_single_fresh_reaper_instance(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    root = tmp_path / "REAPER-Audio-Library" / "runtime"
    instances = root / "instances"
    instances.mkdir(parents=True)
    session_id = "abc123-456"
    session_dir = root / "sessions" / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "heartbeat").write_text("0000000000", encoding="ascii")
    (instances / f"{session_id}.json").write_text(json.dumps({
        "protocol": 1,
        "bridge_session_id": session_id,
        "last_seen_epoch": time.time(),
        "reaper_version": "7.84",
        "active_project": {"project_id": "project-1"},
    }), encoding="utf-8")

    bridge = ReaperBridge()
    assert bridge.discover_instances()[0]["bridge_session_id"] == session_id
    assert bridge._resolve_runtime_dir() == root / "sessions" / session_id


def test_default_bridge_requires_explicit_choice_for_multiple_instances(tmp_path,
                                                                       monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    instances = tmp_path / "REAPER-Audio-Library" / "runtime" / "instances"
    instances.mkdir(parents=True)
    for session_id in ("a11-b11", "a22-b22"):
        session_dir = instances.parent / "sessions" / session_id
        session_dir.mkdir(parents=True)
        (session_dir / "heartbeat").write_text("0000000000", encoding="ascii")
        (instances / f"{session_id}.json").write_text(json.dumps({
            "protocol": 1,
            "bridge_session_id": session_id,
            "last_seen_epoch": time.time(),
        }), encoding="utf-8")

    with pytest.raises(ValidationError, match="Multiple REAPER instances"):
        ReaperBridge()._resolve_runtime_dir()
