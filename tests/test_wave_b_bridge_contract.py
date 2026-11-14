"""Wave B lane 6: the bridge response contract, validated without REAPER.

``ReaperBridge.capture_render`` and ``ReaperBridge.project_digest`` are the two
places where an untrusted bridge response becomes evidence.  These tests drive
them through a monkeypatched ``call`` (the same seam ``tests/test_bridge_client.py``
uses) and pin the rejection of every field Wave B added: the render-settings
echo, the channel count, the measured tail, the restore report, and a render
that would be added to the project.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reaper_audio.errors import BridgeUnavailable
from reaper_audio.reaper.bridge_client import ReaperBridge

REVISION = {"project_id": "project-1", "change_count": 8, "path": "C:/mix.rpp"}


def _settings(**overrides: float) -> dict:
    settings = {
        "RENDER_SETTINGS": 1.0,
        "RENDER_BOUNDSFLAG": 1.0,
        "RENDER_CHANNELS": 2.0,
        "RENDER_SRATE": 0.0,
        "RENDER_TAILFLAG": 0.0,
        "RENDER_TAILMS": 0.0,
        "RENDER_ADDTOPROJ": 0.0,
    }
    settings.update(overrides)
    return settings


def _response(tmp_path: Path) -> dict:
    track_path = tmp_path / "track.wav"
    master_path = tmp_path / "master.wav"
    track_path.write_bytes(b"track render" + bytes(64))
    master_path.write_bytes(b"master render" + bytes(64))
    return {
        "project_id": "project-1",
        "revision": {**REVISION,
                     "identity_basis": "master_track_guid_and_project_path"},
        "revision_after": {**REVISION, "change_count": 9},
        "capture_point": "native_render_track_stems_plus_master",
        "render_action": "File: Render project",
        "render_settings_used": _settings(),
        "channels": 2,
        "sample_rate": 48_000,
        "tail_s": 0.0,
        "restore_report": {"restored": True, "failures": []},
        "files": [
            {"source_guid": "{TRACK}", "name": "Guitar", "track_index": 0,
             "is_master": False, "path": str(track_path),
             "bytes": track_path.stat().st_size},
            {"source_guid": "{MASTER}", "name": "MASTER", "track_index": -1,
             "is_master": True, "path": str(master_path),
             "bytes": master_path.stat().st_size},
        ],
    }


def _digest() -> dict:
    return {
        "project_id": "project-1",
        "path": "C:/mix.rpp",
        "change_count": 4,
        "digest_version": 1,
        "coverage": {"tracks": True, "items": True, "takes": True,
                     "fx": True, "sends": True, "tempo": True,
                     "envelopes": False},
        "missing": ["envelopes"],
        "fields": {"tracks": [], "items": [], "tempo": []},
    }


def _install(monkeypatch: pytest.MonkeyPatch, bridge: ReaperBridge,
             payload: object) -> None:
    monkeypatch.setattr(bridge, "call", lambda *args, **kwargs: payload)


def test_capture_render_accepts_a_complete_response_and_forwards_the_request(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = ReaperBridge(tmp_path)
    payload = _response(tmp_path)
    observed: dict = {}

    def fake_call(operation, arguments=None, *, timeout_s=10.0):
        observed["operation"] = operation
        observed["arguments"] = arguments
        return payload

    monkeypatch.setattr(bridge, "call", fake_call)
    result = bridge.capture_render(expected_revision=REVISION,
                                   output_dir=tmp_path, job_id="wave-b-job",
                                   expected_track_count=1, channels="stereo",
                                   sample_rate=None, tail_ms=0)

    assert result is payload
    assert observed["operation"] == "project.capture_render"
    arguments = observed["arguments"]
    assert arguments["project_id"] == "project-1"
    assert arguments["expected_revision"] == REVISION
    assert arguments["output_dir"] == str(tmp_path.resolve())
    assert arguments["job_id"] == "wave-b-job"
    assert arguments["section_start_s"] is None
    assert arguments["section_end_s"] is None
    assert arguments["master_only"] is False
    assert arguments["channels"] == "stereo"
    assert arguments["sample_rate"] == 0
    assert arguments["tail_ms"] == 0


@pytest.mark.parametrize("field", ["render_settings_used", "channels",
                                   "sample_rate", "tail_s", "restore_report"])
def test_capture_render_refuses_a_response_without_a_contract_field(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    bridge = ReaperBridge(tmp_path)
    payload = _response(tmp_path)
    payload.pop(field)
    _install(monkeypatch, bridge, payload)

    with pytest.raises(BridgeUnavailable,
                       match="render contract fields") as raised:
        bridge.capture_render(expected_revision=REVISION, output_dir=tmp_path,
                              job_id="wave-b-job", expected_track_count=1)

    assert raised.value.details["missing_fields"] == [field]


@pytest.mark.parametrize(("requested", "reported"), [("stereo", 1),
                                                     ("mono", 2)])
def test_capture_render_refuses_a_different_channel_layout(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requested: str,
        reported: int) -> None:
    bridge = ReaperBridge(tmp_path)
    payload = _response(tmp_path)
    payload["channels"] = reported
    payload["render_settings_used"] = _settings(
        RENDER_CHANNELS=float(reported))
    _install(monkeypatch, bridge, payload)

    with pytest.raises(BridgeUnavailable, match="different channel layout"):
        bridge.capture_render(expected_revision=REVISION, output_dir=tmp_path,
                              job_id="wave-b-job", expected_track_count=1,
                              channels=requested)


def test_capture_render_refuses_a_tail_the_bridge_did_not_apply(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = ReaperBridge(tmp_path)
    payload = _response(tmp_path)
    payload["tail_s"] = 0.0
    payload["render_settings_used"] = _settings(RENDER_TAILFLAG=1.0,
                                                RENDER_TAILMS=250.0)
    _install(monkeypatch, bridge, payload)

    with pytest.raises(BridgeUnavailable, match="different render tail"):
        bridge.capture_render(expected_revision=REVISION, output_dir=tmp_path,
                              job_id="wave-b-job", expected_track_count=1,
                              tail_ms=250)


@pytest.mark.parametrize(("report", "fragment"), [
    pytest.param({"restored": False, "failures": []},
                 "did not confirm", id="not-restored"),
    pytest.param({"restored": True, "failures": ["RENDER_SRATE"]},
                 "could not restore", id="restore-failures"),
])
def test_capture_render_refuses_an_unrestored_project(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, report: dict,
        fragment: str) -> None:
    bridge = ReaperBridge(tmp_path)
    payload = _response(tmp_path)
    payload["restore_report"] = report
    _install(monkeypatch, bridge, payload)

    with pytest.raises(BridgeUnavailable, match=fragment):
        bridge.capture_render(expected_revision=REVISION, output_dir=tmp_path,
                              job_id="wave-b-job", expected_track_count=1)


def test_capture_render_refuses_a_render_added_to_the_project(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = ReaperBridge(tmp_path)
    payload = _response(tmp_path)
    payload["render_settings_used"] = _settings(RENDER_ADDTOPROJ=1.0)
    _install(monkeypatch, bridge, payload)

    with pytest.raises(BridgeUnavailable,
                       match="applied different render settings") as raised:
        bridge.capture_render(expected_revision=REVISION, output_dir=tmp_path,
                              job_id="wave-b-job", expected_track_count=1)

    assert "RENDER_ADDTOPROJ" in raised.value.details["deviations"]


def test_capture_render_refuses_the_wrong_render_bounds_mode(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = ReaperBridge(tmp_path)
    payload = _response(tmp_path)
    # The request names a time selection, but the bridge echoed project bounds.
    payload["render_settings_used"] = _settings(RENDER_BOUNDSFLAG=1.0)
    _install(monkeypatch, bridge, payload)

    with pytest.raises(BridgeUnavailable,
                       match="applied different render settings") as raised:
        bridge.capture_render(expected_revision=REVISION, output_dir=tmp_path,
                              job_id="wave-b-job", expected_track_count=1,
                              section_start_s=1.0, section_end_s=2.0)

    assert "RENDER_BOUNDSFLAG" in raised.value.details["deviations"]


def test_capture_render_accepts_the_section_bounds_it_requested(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = ReaperBridge(tmp_path)
    payload = _response(tmp_path)
    payload["render_settings_used"] = _settings(RENDER_BOUNDSFLAG=2.0)
    _install(monkeypatch, bridge, payload)

    result = bridge.capture_render(expected_revision=REVISION,
                                   output_dir=tmp_path, job_id="wave-b-job",
                                   expected_track_count=1,
                                   section_start_s=1.0, section_end_s=2.0)

    assert result["capture_point"] == "native_render_track_stems_plus_master"


def test_project_digest_accepts_the_frozen_shape(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = ReaperBridge(tmp_path)
    payload = _digest()
    observed: dict = {}

    def fake_call(operation, arguments=None, *, timeout_s=10.0):
        observed["operation"] = operation
        observed["arguments"] = arguments
        return payload

    monkeypatch.setattr(bridge, "call", fake_call)
    result = bridge.project_digest("project-1")

    assert result is payload
    assert observed["operation"] == "project.digest"
    assert observed["arguments"] == {"project_id": "project-1"}


@pytest.mark.parametrize(("mutate", "fragment"), [
    pytest.param(lambda d: ["not", "a", "digest"],
                 "invalid project digest", id="not-a-dict"),
    pytest.param(lambda d: d.pop("missing") and d, "required fields",
                 id="absent-key"),
    pytest.param(lambda d: d.__setitem__("project_id", ""),
                 "without a project identity", id="empty-project-id"),
    pytest.param(lambda d: d.__setitem__("path", 7), "non-string path",
                 id="non-string-path"),
    pytest.param(lambda d: d.__setitem__("change_count", -1),
                 "invalid change count", id="negative-change-count"),
    pytest.param(lambda d: d.__setitem__("change_count", True),
                 "invalid change count", id="boolean-change-count"),
    pytest.param(lambda d: d.__setitem__("digest_version", 2),
                 "unsupported schema version", id="future-version"),
    pytest.param(lambda d: d.__setitem__("coverage", []),
                 "invalid coverage map", id="coverage-list"),
    pytest.param(lambda d: d["coverage"].__setitem__("tracks", 1),
                 "non-boolean coverage flag", id="coverage-int"),
    pytest.param(lambda d: d.__setitem__("missing", "envelopes"),
                 "invalid missing list", id="missing-bare-string"),
    pytest.param(lambda d: d.__setitem__("missing", [""]),
                 "unnamed missing entry", id="missing-empty-name"),
    pytest.param(lambda d: d.__setitem__("fields", []),
                 "invalid field map", id="fields-list"),
    pytest.param(lambda d: d["fields"].__setitem__("tracks", {}),
                 "invalid field group", id="field-group-not-list"),
])
def test_project_digest_rejects_a_malformed_shape(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate,
        fragment: str) -> None:
    bridge = ReaperBridge(tmp_path)
    payload = _digest()
    replacement = mutate(payload)
    if replacement is not None:
        payload = replacement
    _install(monkeypatch, bridge, payload)

    with pytest.raises(BridgeUnavailable, match=fragment):
        bridge.project_digest("project-1")
