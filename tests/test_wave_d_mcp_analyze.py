"""Wave D: MCP `reaper_analyze` render options must reach `session.analyze`."""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import soundfile as sf

pytest.importorskip("mcp")

from reaper_audio import mcp_server
from reaper_audio.daw.session import ProjectSession
from reaper_audio.errors import BridgeUnavailable, ValidationError
from reaper_audio.models import Section

REVISION = {"project_id": "project-wave-d", "change_count": 3,
            "path": "C:/music/wave-d.RPP"}
TAIL_MS = 500
TAIL_S = TAIL_MS / 1000.0


class FakeSession:
    """A project session that records the exact analyze request."""

    def __init__(self) -> None:
        self.bridge = SimpleNamespace(bridge_session_id="bridge-wave-d")
        self.calls: list[dict] = []

    def snapshot(self, **kwargs: object) -> dict:
        return {"revision": dict(REVISION), "tracks": [], "master": {},
                "name": "Wave D fixture"}

    def analyze(self, artifact_root: str, **kwargs: Any) -> dict:
        self.calls.append({"artifact_root": artifact_root, **kwargs})
        section = kwargs.get("section")
        tail_ms = kwargs.get("tail_ms", 0)
        # The rendered file starts at local zero, so its duration is the
        # requested section duration plus the tail (not the absolute project
        # end plus the tail).
        file_end = (section.duration_s + tail_ms / 1000.0
                    if section is not None else 1.0 + tail_ms / 1000.0)
        return {
            "project": {"revision": dict(REVISION),
                        "capture_revision": dict(REVISION)},
            "section": (None if section is None else
                        {"start_s": section.start_s, "end_s": section.end_s}),
            "capture": {"artifact_job_id": "job-wave-d",
                        "tail_s": tail_ms / 1000.0},
            "capture_time_window": {
                "project_start_s": 0.0 if section is None else section.start_s,
                "project_end_s": 1.0 if section is None else section.end_s,
                "file_start_s": 0.0,
                "file_end_s": file_end,
                "tail_s": tail_ms / 1000.0,
            },
            "tail_s": tail_ms / 1000.0,
            "sources": {},
            "audio_files": {},
            "coverage": {},
            "project_tempo": {},
        }


@pytest.fixture
def fake_session(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    session = FakeSession()
    monkeypatch.setattr(mcp_server, "_prune_analysis_state", lambda: None)
    monkeypatch.setattr(mcp_server, "_session", lambda *args, **kwargs: session)
    monkeypatch.setattr(mcp_server, "_analysis_records", {})
    monkeypatch.setattr(mcp_server, "_analysis_roles", {})
    monkeypatch.setattr(
        mcp_server, "_require_interpretation_receipt",
        lambda *args, **kwargs: {"complete": True, "receipt": "ct_test"},
    )
    return session


def _write_reference(path: Path) -> None:
    sample_rate = 8_000
    time = np.arange(2 * sample_rate, dtype=np.float64) / sample_rate
    sf.write(path, 0.1 * np.sin(2 * np.pi * 440 * time), sample_rate,
             subtype="FLOAT")


def test_schema_exposes_render_options_with_defaults() -> None:
    tool = next(tool for tool in asyncio.run(mcp_server.server.list_tools())
                if tool.name == "reaper_analyze")
    properties = tool.input_schema["properties"]

    assert properties["tail_ms"]["default"] == 0
    assert properties["tail_ms"]["type"] == "integer"
    assert properties["channels"]["default"] == "stereo"
    assert properties["channels"]["type"] == "string"
    assert {"type": "integer"} in properties["sample_rate"]["anyOf"]
    assert {"type": "null"} in properties["sample_rate"]["anyOf"]


def test_render_options_are_passed_through_and_reference_window_is_unchanged(
        fake_session: FakeSession, tmp_path: Path) -> None:
    reference = tmp_path / "reference.wav"
    _write_reference(reference)

    result = mcp_server.reaper_analyze(
        start_s=1.0, end_s=2.0, tail_ms=TAIL_MS, channels="mono",
        sample_rate=48_000,
        reference_paths=[str(reference)],
        reference_windows=[{"path": str(reference), "start_s": 0.5, "end_s": 1.5}],
    )

    assert result["ok"] is True
    call = fake_session.calls[0]
    assert call["tail_ms"] == TAIL_MS
    assert call["channels"] == "mono"
    assert call["sample_rate"] == 48_000
    assert (call["section"].start_s, call["section"].end_s) == (1.0, 2.0)

    # The tail belongs to the project capture only: the reference section is
    # exactly the source-file window the caller requested.
    reference_section = next(iter(call["reference_sections"].values()))
    assert (reference_section.start_s, reference_section.end_s) == (0.5, 1.5)
    # Full-file references keep the whole 2 s file; no tail is appended.
    assert sorted(buffer.duration_s for buffer in call["references"].values()) == \
        pytest.approx([1.0, 2.0])
    windows = {item["selection"]: item["window"]
               for item in result["reference_inputs"]}
    assert windows["full_file"] is None
    assert windows["source_file_window"] == {"start_s": 0.5, "end_s": 1.5}

    assert result["analysis_windows"]["project_section"] == {
        "start_s": 1.0, "end_s": 2.0,
    }
    assert result["analysis_windows"]["measured"] == {
        "start_s": 1.0, "end_s": 2.0,
    }
    assert result["analysis_windows"]["captured_file"] == {
        "start_s": 0.0, "end_s": 1.0 + TAIL_S,
    }
    assert result["analysis_windows"]["tail_s"] == pytest.approx(TAIL_S)
    assert result["analysis_windows"]["tail_included_in_measurement"] is False
    assert result["capture"]["tail_s"] == pytest.approx(TAIL_S)


def test_default_render_options_do_not_change_an_old_call(
        fake_session: FakeSession) -> None:
    result = mcp_server.reaper_analyze(start_s=0.0, end_s=1.0)

    assert result["ok"] is True
    call = fake_session.calls[0]
    assert call["tail_ms"] == 0
    assert call["channels"] == "stereo"
    assert call["sample_rate"] is None
    assert result["analysis_windows"]["captured_file"] == {
        "start_s": 0.0, "end_s": 1.0,
    }
    assert result["analysis_windows"]["tail_s"] == 0.0


@pytest.mark.parametrize("arguments", [{"tail_ms": True}, {"sample_rate": True}])
def test_mcp_wire_rejects_boolean_render_options(
        arguments: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable(*args: object, **kwargs: object) -> None:
        raise AssertionError("a rejected request must not reach the REAPER session")

    monkeypatch.setattr(mcp_server, "_session", unreachable)

    with pytest.raises(Exception) as excinfo:
        asyncio.run(mcp_server.server.call_tool("reaper_analyze", arguments))

    message = str(excinfo.value)
    assert "valid integer" in message
    assert "bool" in message


@pytest.mark.parametrize("options", [
    {"tail_ms": -1},
    {"tail_ms": True},
    {"tail_ms": 1.5},
    {"channels": "quad"},
    {"channels": ["stereo"]},
    {"sample_rate": 0},
    {"sample_rate": -48_000},
    {"sample_rate": True},
])
def test_invalid_render_options_are_rejected(options: dict) -> None:
    raw_analyze = getattr(mcp_server.reaper_analyze, "__wrapped__")

    with pytest.raises(ValidationError):
        raw_analyze(**options)


# ---------------------------------------------------------------------------
# Full-project render tail: the file may keep the tail, the measurement may not.
# ---------------------------------------------------------------------------

FULL_PROJECT_ID = "reaper:wave-d-full-project"
FULL_PROJECT_PATH = "C:/wave-d/full-project.rpp"
FULL_SAMPLE_RATE = 8_000
QUIET_AMPLITUDE = 0.02
LOUD_AMPLITUDE = 0.9
QUIET_PEAK_DBFS = 20.0 * float(np.log10(QUIET_AMPLITUDE))


def _write_project_tail_wav(path: Path, *, project_frames: int, total_frames: int,
                            sample_rate: int) -> None:
    """Quiet project audio followed by a deliberately loud render tail."""
    frames = max(1, total_frames)
    time_s = np.arange(frames, dtype=np.float64) / sample_rate
    signal = QUIET_AMPLITUDE * np.sin(2.0 * np.pi * 220.0 * time_s)
    split = min(max(0, project_frames), frames)
    signal[split:] = (LOUD_AMPLITUDE
                      * np.sin(2.0 * np.pi * 660.0 * time_s[split:]))
    pcm = np.clip(np.round(signal * 32767.0), -32768, 32767).astype("<i2")
    pcm = np.repeat(pcm[:, None], 2, axis=1)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())


class FullProjectBridge:
    """Render two tracks plus the master for a short full-project fixture."""

    def __init__(self, *, project_length: object = 1.0,
                 file_frames: int | None = None,
                 tail_echo_s: float | None = None,
                 tail_setting_ms: float | None = None,
                 include_length: bool = True) -> None:
        self.project_length = project_length
        self.file_frames = file_frames
        self.tail_echo_s = tail_echo_s
        self.tail_setting_ms = tail_setting_ms
        self.include_length = include_length
        self.revision = {"project_id": FULL_PROJECT_ID, "change_count": 3,
                         "path": FULL_PROJECT_PATH}
        self.tracks = [
            {"guid": "{GUITAR}", "index": 0, "name": "Guitar", "volume": 0.7,
             "pan": -1.0, "mute": False, "solo": 0, "fx": []},
            {"guid": "{BASS}", "index": 1, "name": "Bass", "volume": 0.8,
             "pan": 0.0, "mute": False, "solo": 0, "fx": []},
        ]
        self.master = {"guid": "{MASTER}", "index": -1, "name": "MASTER",
                       "volume": 1.0, "pan": 0.0, "mute": False, "solo": 0,
                       "fx": []}
        self.render_args: dict | None = None

    def project_context(self, project_id: str | None = None, **_: object) -> dict:
        context = {
            "revision": dict(self.revision),
            "path": FULL_PROJECT_PATH,
            "name": "Wave D full project",
            "track_count": len(self.tracks),
            "tracks": [dict(track) for track in self.tracks],
            "master": dict(self.master),
            "selection": {"track_guids": [], "time_start_s": 0.0,
                          "time_end_s": 0.0},
            "tempo": {"bpm": 120.0, "numerator": 4, "denominator": 4,
                      "markers": []},
            "transport": {"play_state": 0, "position_s": 0.0},
        }
        if self.include_length:
            context["length_s"] = self.project_length
        return context

    def call(self, operation: str, arguments: dict | None = None, **_: object) -> dict:
        if operation == "project.render_settings.read":
            return {"project_id": self.revision["project_id"],
                    "revision": dict(self.revision), "render_settings": 0,
                    "render_targets": "C:/renders/old.wav", "dirty": 0}
        raise AssertionError(f"unexpected bridge operation: {operation}")

    def capture_render(self, *, expected_revision: dict, output_dir: str,
                       job_id: str, expected_track_count: int,
                       timeout_s: float = 600.0,
                       section_start_s: float | None = None,
                       section_end_s: float | None = None,
                       master_only: bool = False, channels: str = "stereo",
                       sample_rate: int | None = None,
                       tail_ms: int = 0) -> dict:
        self.render_args = {
            "section_start_s": section_start_s, "section_end_s": section_end_s,
            "tail_ms": tail_ms, "channels": channels,
            "sample_rate": sample_rate, "master_only": master_only,
        }
        before = dict(self.revision)
        after = {**self.revision, "change_count": self.revision["change_count"] + 1}
        self.revision = after
        rate = sample_rate or FULL_SAMPLE_RATE
        if section_start_s is None:
            length = self.project_length
            usable = (isinstance(length, (int, float))
                      and not isinstance(length, bool)
                      and np.isfinite(length) and length > 0)
            base_frames = int(round(float(length) * rate)) if usable else 8_000
        else:
            base_frames = int(round((section_end_s - section_start_s) * rate))
        tail_frames = int(round(tail_ms / 1000.0 * rate))
        total_frames = (base_frames + tail_frames if self.file_frames is None
                        else self.file_frames)
        directory = Path(output_dir)
        rendered = self.tracks if not master_only else []
        files: list[dict[str, Any]] = []
        for index, track in enumerate(rendered):
            path = directory / f"rali-{job_id}-{index + 1:02d}-{track['name']}.wav"
            _write_project_tail_wav(path, project_frames=base_frames,
                                    total_frames=total_frames, sample_rate=rate)
            files.append({"source_guid": track["guid"], "name": track["name"],
                          "track_index": index, "is_master": False,
                          "path": str(path), "bytes": path.stat().st_size})
        master_path = directory / f"rali-{job_id}-00-MASTER.wav"
        _write_project_tail_wav(master_path, project_frames=base_frames,
                                total_frames=total_frames, sample_rate=rate)
        files.append({"source_guid": self.master["guid"], "name": "MASTER",
                      "track_index": -1, "is_master": True,
                      "path": str(master_path),
                      "bytes": master_path.stat().st_size})
        return {
            "project_id": self.revision["project_id"],
            "revision": before,
            "revision_after": dict(after),
            "capture_point": "native_render_track_stems_plus_master",
            "render_action": "File: Render project",
            "render_settings_used": {
                "RENDER_CHANNELS": float(2 if channels == "stereo" else 1),
                "RENDER_TAILMS": float(tail_ms if self.tail_setting_ms is None
                                       else self.tail_setting_ms),
                "RENDER_ADDTOPROJ": 0.0,
            },
            "channels": 2 if channels == "stereo" else 1,
            "sample_rate": rate,
            "tail_s": (tail_ms / 1000.0 if self.tail_echo_s is None
                       else self.tail_echo_s),
            "restore_report": {"restored": True, "failures": []},
            "files": files,
        }


def _analyze_full_project(bridge: FullProjectBridge, tmp_path: Path,
                          **kwargs: Any) -> dict:
    return ProjectSession(bridge).analyze(
        tmp_path / "artifacts", parallel=False, include_fx_params=False, **kwargs)


def test_full_project_without_a_tail_measures_the_whole_file(tmp_path: Path) -> None:
    report = _analyze_full_project(FullProjectBridge(), tmp_path)

    window = report["capture_time_window"]
    assert window["project_start_s"] == 0.0
    assert window["project_end_s"] == pytest.approx(1.0)
    assert window["file_end_s"] == pytest.approx(1.0)
    assert window["tail_s"] == 0.0
    assert window["project_end_source"] == "project_length_and_rendered_file"
    assert report["section"] is None
    for source in report["sources"].values():
        analysis = source["analysis_window"]
        assert analysis["measured_end_s"] == pytest.approx(1.0)
        assert analysis["file_end_s"] == pytest.approx(1.0)
        assert analysis["tail_included_in_measurement"] is False
        assert analysis["coverage"] == "full_file"


def test_full_project_tail_widens_the_file_not_the_measured_interval(
        tmp_path: Path) -> None:
    report = _analyze_full_project(FullProjectBridge(), tmp_path, tail_ms=250)

    window = report["capture_time_window"]
    assert window["project_start_s"] == 0.0
    assert window["project_end_s"] == pytest.approx(1.0)
    assert window["file_end_s"] == pytest.approx(1.25)
    assert window["tail_s"] == pytest.approx(0.25)
    assert window["project_end_source"] == "project_length_and_render_tail"
    assert window["project_end_evidence"]["project_frames"] == 8_000
    assert window["project_end_evidence"]["tail_frames"] == 2_000
    assert report["section"] is None

    assert set(report["sources"]) == {"{GUITAR}", "{BASS}", "{MASTER}"}
    for source in report["sources"].values():
        analysis = source["analysis_window"]
        assert analysis["project_end_s"] == pytest.approx(1.0)
        assert analysis["measured_start_s"] == 0.0
        assert analysis["measured_end_s"] == pytest.approx(1.0)
        assert analysis["file_end_s"] == pytest.approx(1.25)
        assert analysis["tail_s"] == pytest.approx(0.25)
        assert analysis["tail_included_in_measurement"] is False
        assert analysis["coverage"] == "project_window"

        # The WAV really is 1.25 s and still carries the loud tail...
        audio_format = source["capture"]["audio_format"]
        assert audio_format["frames"] == 10_000
        assert audio_format["duration_s"] == pytest.approx(1.25)
        assert source["capture"]["time_window"]["file_end_s"] == pytest.approx(1.25)

        # ...but the measured numbers describe the quiet 0..1 s project only.
        assert source["duration_s"] == pytest.approx(1.0)
        peak_dbfs = source["metrics"]["peak_dbfs"]["value"]
        assert peak_dbfs == pytest.approx(QUIET_PEAK_DBFS, abs=1.0)
        times = source["curves"]["temporal_profile"]["times"]
        assert times and max(times) <= 1.0


def test_section_with_tail_keeps_the_section_as_the_measured_interval(
        tmp_path: Path) -> None:
    report = _analyze_full_project(FullProjectBridge(), tmp_path,
                                   section=Section(0.25, 0.75), tail_ms=250)

    window = report["capture_time_window"]
    assert window["project_start_s"] == pytest.approx(0.25)
    assert window["project_end_s"] == pytest.approx(0.75)
    assert window["file_end_s"] == pytest.approx(0.75)
    assert window["tail_s"] == pytest.approx(0.25)
    assert window["project_end_source"] == "requested_section"
    assert report["section"] == {"start_s": 0.25, "end_s": 0.75}
    for source in report["sources"].values():
        analysis = source["analysis_window"]
        assert analysis["measured_end_s"] == pytest.approx(0.75)
        assert analysis["file_end_s"] == pytest.approx(0.75)
        assert analysis["tail_included_in_measurement"] is False
        assert analysis["coverage"] == "project_section"
        assert source["capture"]["audio_format"]["frames"] == 6_000
        assert source["duration_s"] == pytest.approx(0.5)


def test_full_project_boundary_rounds_at_sample_frame_precision(
        tmp_path: Path) -> None:
    # 1.0001 s at 8 kHz is 8000.8 frames; the render holds 8001 project frames.
    report = _analyze_full_project(
        FullProjectBridge(project_length=1.0001), tmp_path, tail_ms=250,
    )

    window = report["capture_time_window"]
    expected_end = 8_001 / FULL_SAMPLE_RATE
    assert window["project_end_s"] == pytest.approx(expected_end)
    assert abs(window["project_end_s"] - 1.0001) <= 1.0 / FULL_SAMPLE_RATE
    assert window["file_end_s"] == pytest.approx(10_001 / FULL_SAMPLE_RATE)
    for source in report["sources"].values():
        measured_end = source["analysis_window"]["measured_end_s"]
        assert measured_end == pytest.approx(expected_end)
        assert abs(measured_end - 1.0001) <= 1.0 / FULL_SAMPLE_RATE
        assert source["duration_s"] == pytest.approx(expected_end)


def test_render_shorter_than_the_requested_tail_fails_closed(tmp_path: Path) -> None:
    bridge = FullProjectBridge(file_frames=1_600)  # 0.2 s, tail needs 0.25 s

    with pytest.raises(BridgeUnavailable,
                       match="not longer than its requested tail") as raised:
        _analyze_full_project(bridge, tmp_path, tail_ms=250)

    assert raised.value.details["boundary"] == "unknown"
    assert not list((tmp_path / "artifacts").glob("job-*/rali-*.wav"))


def test_render_with_extra_audio_fails_closed(tmp_path: Path) -> None:
    bridge = FullProjectBridge(file_frames=12_000)  # 1.5 s vs 1.0 + 0.25

    with pytest.raises(BridgeUnavailable,
                       match="disagrees with REAPER's project length") as raised:
        _analyze_full_project(bridge, tmp_path, tail_ms=250)

    assert raised.value.details["boundary"] == "unknown"


def test_tail_without_project_length_fails_closed(tmp_path: Path) -> None:
    bridge = FullProjectBridge(include_length=False)

    with pytest.raises(BridgeUnavailable,
                       match="cannot be separated from project audio") as raised:
        _analyze_full_project(bridge, tmp_path, tail_ms=250)

    assert raised.value.details["boundary"] == "unknown"


def test_tail_echo_that_does_not_match_the_request_fails_closed(
        tmp_path: Path) -> None:
    bridge = FullProjectBridge(tail_echo_s=0.0, tail_setting_ms=0.0)

    with pytest.raises(BridgeUnavailable,
                       match="different tail than requested") as raised:
        _analyze_full_project(bridge, tmp_path, tail_ms=250)

    assert raised.value.details["boundary"] == "unknown"


@pytest.mark.parametrize("length", [float("nan"), float("inf"), True, "1.0", 0.0])
def test_invalid_project_length_fails_closed(tmp_path: Path,
                                             length: object) -> None:
    bridge = FullProjectBridge(project_length=length)

    with pytest.raises(BridgeUnavailable,
                       match="invalid project length") as raised:
        _analyze_full_project(bridge, tmp_path, tail_ms=250)

    assert raised.value.details["boundary"] == "unknown"


def test_mcp_full_project_tail_is_never_marked_as_measured(
        fake_session: FakeSession, tmp_path: Path) -> None:
    result = mcp_server.reaper_analyze(tail_ms=TAIL_MS)

    assert result["ok"] is True
    windows = result["analysis_windows"]
    assert windows["project_section"] is None
    assert windows["measured"] == {"start_s": 0.0, "end_s": 1.0}
    assert windows["captured_file"] == {"start_s": 0.0, "end_s": 1.0 + TAIL_S}
    assert windows["tail_s"] == pytest.approx(TAIL_S)
    assert windows["tail_included_in_measurement"] is False
    assert fake_session.calls[0]["section"] is None


def test_mcp_fails_closed_when_a_tail_has_no_measured_boundary(
        monkeypatch: pytest.MonkeyPatch) -> None:
    cleaned: list[str] = []

    class UnknownBoundarySession(FakeSession):
        def analyze(self, artifact_root: str, **kwargs: Any) -> dict:
            self.calls.append({"artifact_root": artifact_root, **kwargs})
            return {
                "project": {"revision": dict(REVISION),
                            "capture_revision": dict(REVISION)},
                "section": None,
                "capture": {"artifact_job_id": "job-unknown-boundary",
                            "tail_s": TAIL_S},
                "capture_time_window": {
                    "project_start_s": 0.0,
                    "file_start_s": 0.0,
                    "file_end_s": 1.0 + TAIL_S,
                    "tail_s": TAIL_S,
                },
                "tail_s": TAIL_S,
                "sources": {},
                "audio_files": {"{MASTER}": "temporary-master.wav"},
                "coverage": {},
                "project_tempo": {},
            }

    monkeypatch.setattr(mcp_server, "_prune_analysis_state", lambda: None)
    monkeypatch.setattr(mcp_server, "_session",
                        lambda *args, **kwargs: UnknownBoundarySession())
    monkeypatch.setattr(mcp_server, "_analysis_records", {})
    monkeypatch.setattr(mcp_server, "_analysis_roles", {})
    monkeypatch.setattr(
        mcp_server, "_require_interpretation_receipt",
        lambda *args, **kwargs: {"complete": True, "receipt": "ct_test"},
    )
    monkeypatch.setattr(mcp_server, "cleanup_audio_job",
                        lambda root, job_id: cleaned.append(job_id))

    result = mcp_server.reaper_analyze(tail_ms=TAIL_MS)

    assert result["ok"] is False
    assert result["error"]["code"] == "bridge_unavailable"
    assert "measurable project boundary" in result["error"]["message"]
    assert result["error"]["details"]["boundary"] == "unknown"
    assert cleaned == ["job-unknown-boundary"]
