"""Measurement noise must not kill a token; value targets calibrate themselves."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from reaper_audio import mcp_server
from reaper_audio.context import structural_token as tokens
from reaper_audio.context.plugin_state_store import PluginStateStore
from reaper_audio.errors import StaleContext

PROJECT_ID = "reaper:{MASTER}:C:/Projects/demo.RPP"


def test_a_measurement_jump_no_longer_invalidates_a_token(monkeypatch) -> None:
    state = {"revision": {"project_id": PROJECT_ID, "instance_id": "inst",
                          "epoch": 1, "change_count": 10},
             "map": "sha256:map"}
    monkeypatch.setattr(tokens, "_revision_bits",
                        lambda session, project_id: ({}, dict(state["revision"]),
                                                     state["map"]))
    token = tokens.mint(None, PROJECT_ID)["structural_token"]

    state["revision"]["change_count"] = 76
    with pytest.raises(StaleContext):
        tokens.require(None, PROJECT_ID, token)

    tokens.register_self_noise(PROJECT_ID, 10, 76)
    accepted = tokens.require(None, PROJECT_ID, token)
    assert accepted["map_sha256"] == "sha256:map"

    state["revision"]["change_count"] = 77
    with pytest.raises(StaleContext):
        tokens.require(None, PROJECT_ID, token)


def test_a_real_map_change_still_invalidates_a_token(monkeypatch) -> None:
    state = {"revision": {"project_id": PROJECT_ID, "instance_id": "inst",
                          "epoch": 1, "change_count": 10},
             "map": "sha256:map"}
    monkeypatch.setattr(tokens, "_revision_bits",
                        lambda session, project_id: ({}, dict(state["revision"]),
                                                     state["map"]))
    token = tokens.mint(None, PROJECT_ID)["structural_token"]
    state["map"] = "sha256:other"
    with pytest.raises(StaleContext):
        tokens.require(None, PROJECT_ID, token)


class FakeBridge:
    def __init__(self) -> None:
        self.value = 0.5
        self.calls: list[tuple] = []

    def health(self, timeout_s: float = 2.0) -> dict:
        return {"project": {"change_count": 1}}

    def project_context(self, project_id: str, **kwargs) -> dict:
        return {"tracks": [{"guid": "{TRACK}",
                            "fx": [{"guid": "{FX}", "index": 0}]}]}

    def call(self, operation: str, arguments: dict, timeout_s: float = 10.0) -> dict:
        assert operation == "fx.list_params"
        return {"parameters": [{"index": 2, "name": "Ratio"}]}

    def call_api(self, name: str, handle, *args):
        self.calls.append((name, args))
        if name == "TrackFX_GetParamNormalized":
            return SimpleNamespace(returns=[self.value])
        if name == "TrackFX_GetFormattedParamValue":
            return SimpleNamespace(returns=[True, f"{2.0 + self.value * 4.0:.2f}:1"])
        if name == "TrackFX_SetParamNormalized":
            self.value = float(args[-1])
            return SimpleNamespace(returns=[True])
        raise AssertionError(name)


def test_a_value_target_calibrates_itself_on_first_use(tmp_path) -> None:
    bridge = FakeBridge()
    resolved, note = mcp_server._resolve_parameter_targets(
        project_id=PROJECT_ID, track_guid="{TRACK}",
        values={"fx_guid": "{FX}", "parameter_index": 2, "target": "3:1"},
        archive_root=str(tmp_path), bridge=bridge)

    assert resolved["normalized"] == pytest.approx(0.25, abs=0.06)
    assert note and note["cached"] is True
    stored = PluginStateStore(tmp_path).get_calibration(PROJECT_ID, "{TRACK}",
                                                        "{FX}", 2)
    assert stored and len(stored["table"]) >= 3
    # the sweep must leave the parameter where it found it
    assert bridge.value == pytest.approx(0.5)


def test_a_second_value_target_reuses_the_cached_table(tmp_path) -> None:
    bridge = FakeBridge()
    mcp_server._resolve_parameter_targets(
        project_id=PROJECT_ID, track_guid="{TRACK}",
        values={"fx_guid": "{FX}", "parameter_index": 2, "target": "3:1"},
        archive_root=str(tmp_path), bridge=bridge)
    writes_before = len([call for call in bridge.calls
                         if call[0] == "TrackFX_SetParamNormalized"])
    _, note = mcp_server._resolve_parameter_targets(
        project_id=PROJECT_ID, track_guid="{TRACK}",
        values={"fx_guid": "{FX}", "parameter_index": 2, "target": "3:1"},
        archive_root=str(tmp_path), bridge=bridge)
    writes_after = len([call for call in bridge.calls
                        if call[0] == "TrackFX_SetParamNormalized"])
    assert note is None
    assert writes_after == writes_before
