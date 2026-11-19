"""Usability surface: named sends, evidence tokens, bridge cleanup, MIDI notes."""

from __future__ import annotations

import asyncio

import pytest

from reaper_audio import mcp_server
from reaper_audio.context.evidence import EvidenceStore
from reaper_audio.errors import ValidationError
from reaper_audio.reaper.bridge_client import ReaperBridge

PROJECT_ID = "reaper:{MASTER}:C:/Projects/demo.RPP"


def test_project_context_names_send_destinations(monkeypatch) -> None:
    payload = {
        "revision": {"project_id": PROJECT_ID, "change_count": 3},
        "tracks": [
            {"guid": "{A}", "name": "Аня 1",
             "sends": [{"index": 0, "dest_guid": "{BUS}", "volume": 1.0}]},
            {"guid": "{BUS}", "name": "FEMALE", "sends": []},
        ],
    }
    monkeypatch.setattr(ReaperBridge, "call",
                        lambda self, operation, arguments, timeout_s=10.0: payload)
    context = ReaperBridge().project_context(PROJECT_ID)
    send = context["tracks"][0]["sends"][0]
    assert send["dest_name"] == "FEMALE"
    assert context["tracks"][1]["sends"] == []


def test_evidence_tool_returns_the_units_behind_a_token(tmp_path) -> None:
    store = EvidenceStore(tmp_path)
    units = [{"metric": "integrated_lufs", "value": -18.0, "unit": "LUFS",
              "interval": {"start_s": 0.0, "end_s": 10.0},
              "source_id": "track:{A}"},
             {"metric": "peak_dbfs", "value": -1.0, "unit": "dBFS",
              "interval": {"start_s": 0.0, "end_s": 10.0},
              "source_id": "track:{A}"}]
    ids = store.put_many(units)
    token = store.mint_token(project_id=PROJECT_ID,
                             revision={"project_id": PROJECT_ID},
                             units=ids)
    result = mcp_server.reaper_evidence(token=token["token"], archive_root=str(tmp_path))
    assert result["unit_count"] == 2
    assert {unit["metric"] for unit in result["units"]} == {"integrated_lufs",
                                                            "peak_dbfs"}


def test_evidence_tool_refuses_an_unknown_token(tmp_path) -> None:
    result = mcp_server.reaper_evidence(token="et_missing", archive_root=str(tmp_path))
    assert result["ok"] is False
    assert result["error"]["code"] == "validation_error"


def test_bridge_cleanup_retires_every_duplicate(monkeypatch) -> None:
    alive = {"one": PROJECT_ID, "two": "reaper:{OTHER}", "three": PROJECT_ID}

    monkeypatch.setattr(ReaperBridge, "discover_instances",
                        lambda self: [{"bridge_session_id": key} for key in alive])
    monkeypatch.setattr(mcp_server, "_bridge_health",
                        lambda session_id, timeout_s=2.0: {
                            "bridge_session_id": session_id,
                            "project_id": alive.get(session_id)})
    retired = []

    def fake_call(self, operation, arguments, timeout_s=10.0):
        retired.append((self.bridge_session_id, operation, arguments))
        return {"accepted": True}

    monkeypatch.setattr(ReaperBridge, "call", fake_call)
    result = mcp_server.reaper_bridge(action="cleanup", project_id=PROJECT_ID)
    assert result["kept"] == "one"
    assert sorted(result["retired"]) == ["three", "two"]
    assert all(operation == "bridge.lifecycle" and
               arguments == {"action": "retire"}
               for _, operation, arguments in retired)


def test_bridge_status_lists_projects(monkeypatch) -> None:
    monkeypatch.setattr(ReaperBridge, "discover_instances",
                        lambda self: [{"bridge_session_id": "one"},
                                      {"bridge_session_id": "ghost"}])
    monkeypatch.setattr(mcp_server, "_bridge_health",
                        lambda session_id, timeout_s=2.0: (
                            {"bridge_session_id": session_id,
                             "project_id": PROJECT_ID} if session_id == "one" else None))
    result = mcp_server.reaper_bridge(action="status")
    assert [row["bridge_session_id"] for row in result["bridges"]] == ["one", "ghost"]
    assert result["bridges"][1]["state"] == "registered_but_silent"


def test_midi_notes_reports_pitch_statistics(monkeypatch) -> None:
    notes = [{"pitch": 35, "velocity": 100, "start_s": 0.0},
             {"pitch": 35, "velocity": 110, "start_s": 0.5},
             {"pitch": 38, "velocity": 120, "start_s": 1.0}]
    context = {"revision": {"project_id": PROJECT_ID}, "tracks": [
        {"name": "Superior Drummer 3", "items": [
            {"guid": "{ITEM}", "active_take": {"midi": {"notes": notes}}}]}]}
    monkeypatch.setattr(ReaperBridge, "health",
                        lambda self, timeout_s=5.0: {"project": {"project_id": PROJECT_ID}})
    monkeypatch.setattr(ReaperBridge, "project_context",
                        lambda self, project_id=None, **kwargs: context)
    result = mcp_server.reaper_midi(action="notes", project_id=PROJECT_ID)
    pitches = {row["pitch"]: row for row in result["items"][0]["pitches"]}
    assert pitches[35]["notes"] == 2
    assert pitches[35]["velocity_mean"] == 105.0
    assert pitches[38]["velocity_max"] == 120
