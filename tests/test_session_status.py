"""Status/preflight tests on a recorded project payload — no live REAPER."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from reaper_audio.context.evidence import EvidenceStore
from reaper_audio.daw.session import ProjectSession
from reaper_audio.daw.session_status import (
    collect_runtime,
    media_report,
    read_receipts,
)
from reaper_audio.errors import BridgeUnavailable, ValidationError
from reaper_audio.reaper.api_catalog import ApiCallResult

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class RecordedBridge:
    """Replays the recorded context/digest and answers the few extra reads."""

    def __init__(self, *, context: dict | None = None, digest: dict | None = None,
                 health: dict | None = None, instances: list | None = None,
                 midi_counts: dict | None = None):
        self.context = copy.deepcopy(context or _load("session_context.json"))
        self.digest_payload = copy.deepcopy(digest if digest is not None
                                            else _load("session_digest.json"))
        self.health_payload = health or {
            "service": "reaper-workbench",
            "protocol": 1,
            "bridge_session": "6a0b1c2d-0001",
            "bridge_build": "test-build",
            "reaper_version": "7.85",
            "project": {"project_id": "reaper:demo-test",
                        "change_count": 41,
                        "path": "C:/Projects/demo/demo.rpp"},
        }
        self.instance_rows = list(instances or [])
        self.midi_counts = midi_counts if midi_counts is not None else {
            "{TAKE-ACCORDION-1}": 128}
        self.calls: list[tuple[str, dict]] = []
        self.api_calls: list[tuple[str, tuple]] = []
        self.project_id = "reaper:demo-test"

    def project_context(self, project_id=None, **options):
        self.calls.append(("project.context", {"project_id": project_id, **options}))
        return copy.deepcopy(self.context)

    def project_digest(self, project_id=None):
        self.calls.append(("project.digest", {"project_id": project_id}))
        if self.digest_payload is None:
            raise BridgeUnavailable("digest unavailable")
        return copy.deepcopy(self.digest_payload)

    def health(self, timeout_s: float = 1.0):
        return dict(self.health_payload)

    def discover_instances(self):
        return list(self.instance_rows)

    def call_api(self, name: str, *values, timeout_s: float = 10.0):
        self.api_calls.append((name, values))
        if name == "MIDI_CountEvts":
            take = values[0]
            count = self.midi_counts.get(take.stable_id)
            if count is None:
                return ApiCallResult(name, (False, 0, 0, 0), {})
            return ApiCallResult(name, (True, count, 0, 0), {})
        raise AssertionError(f"unexpected API call: {name}")


def _session(bridge: RecordedBridge) -> ProjectSession:
    return ProjectSession(bridge, project_id="reaper:demo-test")


def test_status_summary_is_smaller_than_full_and_keeps_the_same_identity() -> None:
    bridge = RecordedBridge()
    session = _session(bridge)

    summary = session.status()
    full = session.status(detail="full")

    assert summary["detail"] == "summary"
    assert full["detail"] == "full"
    assert summary["revision"] == full["revision"]
    assert summary["project"]["project_id"] == "reaper:demo-test"
    assert summary["soloed"] == ["Rhythm Gtr L"]
    assert summary["muted"] == []
    assert len(json.dumps(summary)) < len(json.dumps(full))

    # Summary keeps names and counts; full keeps rows.
    assert summary["sends"]["count"] == 2
    assert "rows" not in summary["sends"]
    assert full["sends"]["count"] == 2
    assert {row["dest_name"] for row in full["sends"]["rows"]} == {"GTR BUS", "FX BUS"}
    assert summary["items"]["count"] == 5
    assert "rows" not in summary["items"]
    assert len(full["items"]["rows"]) == 5
    assert summary["envelopes"]["count"] == 3
    assert "rows" not in summary["envelopes"]
    assert len(full["envelopes"]["rows"]) == 3


def test_status_full_describes_routing_envelopes_and_midi_counts() -> None:
    bridge = RecordedBridge()
    full = _session(bridge).status(detail="full")

    vocal_send = next(row for row in full["sends"]["rows"]
                      if row["dest_name"] == "FX BUS")
    assert vocal_send["pre_post"] == "pre_fader"
    assert vocal_send["enabled"] is True
    assert vocal_send["source_name"] == "Scream Vox"

    volume = next(row for row in full["envelopes"]["rows"]
                  if row["guid"] == "{ENV-RHYTHM-VOL}")
    assert volume["owner_name"] == "Rhythm Gtr L"
    assert volume["point_count"] == 3
    assert volume["scale"] == "amplitude"
    assert volume["automation_mode_name"] == "read"
    assert (volume["min"], volume["max"]) == (0.25, 1.0)

    accordion = next(row for row in full["items"]["rows"]
                     if row["guid"] == "{ITEM-ACCORDION-1}")
    assert accordion["is_midi"] is True
    assert accordion["midi_note_count"] == 128
    assert any(name == "MIDI_CountEvts" for name, _ in bridge.api_calls)

    assert full["master"]["fx_count"] == 2
    assert full["master"]["fx"][1]["name"] == "ReaLimit"
    assert full["tempo"]["marker_count"] == 5
    assert full["regions"][0]["name"] == "Intro"
    assert full["markers"]["count"] == 1


def test_status_sections_limits_the_answer() -> None:
    result = _session(RecordedBridge()).status(sections=["tracks", "tempo"])

    assert set(result) == {"schema", "detail", "sections", "project", "revision",
                           "tracks", "soloed", "muted", "tempo"}
    assert result["sections"] == ["tracks", "tempo"]


def test_status_rejects_unknown_detail_and_section() -> None:
    session = _session(RecordedBridge())
    with pytest.raises(ValidationError):
        session.status(detail="everything")
    with pytest.raises(ValidationError):
        session.status(sections=["tracks", "nonsense"])


def test_status_degrades_to_counts_when_the_digest_is_unavailable() -> None:
    bridge = RecordedBridge()
    bridge.digest_payload = None
    result = _session(bridge).status(detail="full")

    assert result["gaps"] == ["digest_unavailable"]
    assert result["sends"]["count"] == 2
    assert result["sends"]["rows"] == []
    assert result["sends"]["complete"] is False


def test_status_media_reports_offline_and_missing_separately() -> None:
    bridge = RecordedBridge()
    session = _session(bridge)
    media = session.status(sections=["media"])["media"]

    assert media["offline_count"] == 1
    assert media["missing_count"] == 1
    assert media["missing_sources"] == [
        "C:/Projects/demo/audio/bass_di_missing.wav"]

    offline, missing = media_report(bridge.context["tracks"])
    assert offline[0]["track_name"] == "Bass DI"
    assert offline[0]["file_exists"] is False
    assert missing == offline


def test_preflight_catches_solo_and_offline_media_by_name() -> None:
    result = _session(RecordedBridge()).preflight()

    codes = {row["code"] for row in result["blockers"]}
    assert result["ok"] is False
    assert result["state"] == "blocked"
    assert {"solo_active", "media_offline", "media_missing"} <= codes
    solo = next(row for row in result["blockers"] if row["code"] == "solo_active")
    assert solo["names"] == ["Rhythm Gtr L"]
    offline = next(row for row in result["blockers"] if row["code"] == "media_offline")
    assert offline["names"] == ["Bass DI"]
    assert "C:/Projects/demo/audio/bass_di_missing.wav" in offline["sources"]

    # The same observations are also available as flat, judgement-free facts.
    assert result["soloed"] == ["Rhythm Gtr L"]
    assert result["muted"] == []
    assert result["offline"] == ["Bass DI"]
    assert result["missing_sources"] == [
        "C:/Projects/demo/audio/bass_di_missing.wav"]
    assert result["transport"]["playing"] is False
    assert result["runtime"]["state"] in {"current", "stale_runtime", "unknown"}


def test_preflight_reports_envelope_silence_without_a_judgement_code() -> None:
    bridge = RecordedBridge()
    for point in bridge.digest_payload["fields"]["automation"]["tracks"][0][
            "envelopes"][0]["points"]:
        point["value"] = 0.0

    result = _session(bridge).preflight()

    assert result["checks"]["envelopes"]["readable"] is True
    assert result["checks"]["envelopes"]["source"] == "project.digest"
    assert [row["name"] for row in result["checks"]["envelopes"]["with_silence"]] == [
        "Volume"]
    assert "envelope_silence" not in result["codes"]


def test_preflight_reports_window_focus_as_a_fact(monkeypatch) -> None:
    monkeypatch.setattr("reaper_audio.daw.session_control.reaper_window_focused",
                        lambda: False)

    result = _session(RecordedBridge()).preflight(check_focus=True)

    assert result["focused"] is False
    assert result["checks"]["focus"] == {"checked": True, "focused": False}
    assert "window_not_focused" in result["codes"]


def test_preflight_allows_an_explicitly_named_solo() -> None:
    bridge = RecordedBridge()
    bridge.context["tracks"][1]["items"][0]["source_online"] = True
    bridge.context["tracks"][1]["items"][0]["source_length_s"] = 150.0
    bridge.context["tracks"][1]["items"][0]["source_sample_rate"] = 48000
    bridge.context["tracks"][1]["items"][0]["source_channels"] = 2

    result = _session(bridge).preflight(allow_solo=["Rhythm Gtr L"])

    assert {row["code"] for row in result["blockers"]} == set()
    assert result["ok"] is True
    assert result["checks"]["solo"]["active"] == ["Rhythm Gtr L"]
    assert result["checks"]["solo"]["blocked"] == []


def test_preflight_reports_muted_tracks_and_master_fx_as_non_blocking() -> None:
    bridge = RecordedBridge()
    bridge.context["tracks"][0]["solo"] = 0
    bridge.context["tracks"][2]["mute"] = True
    bridge.context["master"]["fx"][0]["enabled"] = False

    result = _session(bridge).preflight()

    assert result["ok"] is False  # media offline still blocks
    assert {row["code"] for row in result["info"]} >= {"tracks_muted"}
    assert "master_fx_bypassed_or_offline" in result["codes"]
    assert "master_fx_bypassed_or_offline" not in {
        row["code"] for row in result["blockers"]}


def test_runtime_reports_stale_runtime_on_a_build_mismatch(tmp_path) -> None:
    script = tmp_path / "reaper_bridge.lua"
    script.write_text('local bridge_build = "new-build"\n', encoding="utf-8")
    started = int(script.stat().st_mtime) - 100
    bridge = RecordedBridge(
        health={"bridge_session": "6a0b1c2d-0001", "bridge_build": "old-build",
                "script_path": str(script), "reaper_version": "7.85",
                "protocol": 1, "service": "reaper-workbench"},
        instances=[{"bridge_session_id": "6a0b1c2d-0001",
                    "bridge_build": "old-build", "started_epoch": started}],
    )

    runtime = collect_runtime(ProjectSession(bridge))

    assert runtime["state"] == "stale_runtime"
    assert runtime["stale"] is True
    assert runtime["build_matches"] is False
    assert runtime["on_disk_build"] == "new-build"
    assert runtime["bridge_build"] == "old-build"
    assert runtime["process_started_epoch"] == started
    assert runtime["code_mtime_epoch"] >= started
    assert runtime["runtime_age_s"] == runtime["code_mtime_epoch"] - started


def test_runtime_marks_a_code_file_newer_than_the_session_as_stale(tmp_path) -> None:
    script = tmp_path / "reaper_bridge.lua"
    script.write_text('local bridge_build = "same-build"\n', encoding="utf-8")
    started = int(script.stat().st_mtime) - 100
    bridge = RecordedBridge(
        health={"bridge_session": "6a0b1c2d-0001", "bridge_build": "same-build",
                "script_path": str(script), "reaper_version": "7.85"},
        instances=[{"bridge_session_id": "6a0b1c2d-0001",
                    "started_epoch": started}],
    )

    runtime = collect_runtime(ProjectSession(bridge))

    assert runtime["build_matches"] is True
    assert runtime["code_newer_than_process"] is True
    assert runtime["runtime_age_s"] > 0
    assert runtime["state"] == "stale_runtime"
    assert runtime["reason"] == "code_newer_than_process"


def test_status_runtime_section_contains_the_version_and_freshness(tmp_path) -> None:
    script = tmp_path / "reaper_bridge.lua"
    script.write_text('local bridge_build = "test-build"\n', encoding="utf-8")
    bridge = RecordedBridge(
        health={"bridge_session": "6a0b1c2d-0001", "bridge_build": "test-build",
                "script_path": str(script), "reaper_version": "7.85",
                "protocol": 1, "service": "reaper-workbench"},
        instances=[{"bridge_session_id": "6a0b1c2d-0001",
                    "started_epoch": int(script.stat().st_mtime) + 100}],
    )

    runtime = _session(bridge).status(sections=["runtime"])["runtime"]

    assert runtime["state"] == "current"
    assert runtime["build_matches"] is True
    assert runtime["workbench_version"]
    assert runtime["process_started_at"]


def _unit(change_count: int, metric: str) -> dict:
    return {
        "project_id": "reaper:demo-test",
        "revision": {"project_id": "reaper:demo-test", "change_count": change_count},
        "source_id": "track:{TRACK-RHYTHM-L}",
        "interval": {"start_s": 0.0, "end_s": 76.0},
        "metric": metric,
        "method": "test",
        "method_version": 1,
        "stage": "premaster",
        "sample_rate": 48000,
        "channels": 2,
        "capture_id": "cap-1",
        "value": 1.0,
    }


def test_receipts_and_evidence_are_read_without_writing(tmp_path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    journal = state / "edit_receipts.jsonl"
    journal.write_text(
        json.dumps({"receipt": "r1", "status": "applied"}) + "\n" +
        json.dumps({"receipt": "r2", "status": "rebased"}) + "\n" +
        "{broken\n",
        encoding="utf-8")
    EvidenceStore(tmp_path).put_many([_unit(41, "lufs"), _unit(40, "lufs")])

    receipts = read_receipts(tmp_path)
    assert receipts["count"] == 2
    assert receipts["malformed_count"] == 1
    assert receipts["records"][-1]["receipt"] == "r2"

    result = _session(RecordedBridge()).status(
        sections=["receipts", "evidence"], archive_root=str(tmp_path))
    assert result["receipts"]["count"] == 2
    assert result["receipts"]["latest"]["status"] == "rebased"
    assert result["evidence"]["units_total"] == 2
    assert result["evidence"]["units_current"] == 1
    assert result["evidence"]["units_stale"] == 1
    assert result["evidence"]["state"] == "stale"


def test_media_report_uses_the_capture_rule_for_offline(tmp_path) -> None:
    source = tmp_path / "online.wav"
    source.write_bytes(b"RIFF")
    tracks = [{
        "guid": "{T}",
        "index": 0,
        "name": "Track",
        "mute": False,
        "items": [{
            "guid": "{I}",
            "mute": False,
            "source_online": True,
            "source_length_s": 1.0,
            "source_sample_rate": 48000,
            "source_channels": 2,
            "active_take": {"is_midi": False, "source": str(source)},
        }],
    }]
    offline, missing = media_report(tracks)
    assert offline == [] and missing == []


def test_media_report_flags_a_missing_file_even_when_source_online(tmp_path) -> None:
    tracks = [{
        "guid": "{T}", "index": 0, "name": "Track", "mute": False,
        "items": [{
            "guid": "{I}", "mute": False,
            "source_online": False, "source_length_s": 0,
            "source_sample_rate": 0, "source_channels": 0,
            "active_take": {"is_midi": False,
                            "source": str(tmp_path / "gone.wav")},
        }],
    }]
    offline, missing = media_report(tracks)
    assert len(offline) == 1
    assert len(missing) == 1
    assert offline[0]["file_exists"] is False
