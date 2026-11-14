"""Wave C: ``analyze`` keeps a rendered tail out of every measured window.

The fake bridge writes a real 1.25 s WAV with ``wave`` from the standard
library: a quiet project section followed by a loud tail. If the tail leaked
into the measurement, the reported peak would jump by ~33 dB, so the window
bookkeeping is checked by measured numbers, not only by metadata.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from reaper_audio.analysis.metrics import measure_audio
from reaper_audio.daw import session as session_module
from reaper_audio.daw.session import ProjectSession, _load_rendered_audio
from reaper_audio.errors import BridgeUnavailable, ValidationError
from reaper_audio.models import MeasurementReport, Section
from _digest_payloads import FULL_COVERAGE, full_extra_fields

PROJECT_ID = "reaper:wave-c-analyze"
PROJECT_PATH = "C:/wave-c/analyze.rpp"
SAMPLE_RATE = 8_000
TRACK_GUID = "{TRACK-A}"
MASTER_GUID = "{MASTER}"
ITEM_GUID = "{ITEM-A}"
SECTION = Section(0.0, 1.0)
TAIL_MS = 250
TAIL_S = 0.25
QUIET_AMPLITUDE = 0.02
LOUD_AMPLITUDE = 0.9
QUIET_PEAK_DBFS = 20.0 * float(np.log10(QUIET_AMPLITUDE))  # ~ -33.98 dBFS


def _write_tail_wav(path: Path, *, section_end_s: float = 1.0,
                    tail_s: float = TAIL_S, sample_rate: int = SAMPLE_RATE,
                    channels: int = 2) -> None:
    """Write a WAV whose section is quiet and whose tail is deliberately loud."""
    section_frames = int(round(section_end_s * sample_rate))
    tail_frames = int(round(tail_s * sample_rate))
    frames = section_frames + tail_frames
    time_s = np.arange(frames, dtype=np.float64) / sample_rate
    signal = QUIET_AMPLITUDE * np.sin(2.0 * np.pi * 220.0 * time_s)
    signal[section_frames:] = (LOUD_AMPLITUDE
                               * np.sin(2.0 * np.pi * 660.0 * time_s[section_frames:]))
    pcm = np.clip(np.round(signal * 32767.0), -32768, 32767).astype("<i2")
    if channels > 1:
        pcm = np.repeat(pcm[:, None], channels, axis=1)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())


class FakeAnalyzeBridge:
    """Minimal bridge for one capture of one track plus the master."""

    def __init__(self, *, restore_report: object = None,
                 file_tail_s: float | None = None) -> None:
        self.restore_report = ({"restored": True, "failures": []}
                               if restore_report is None else restore_report)
        self.file_tail_s = file_tail_s
        self.calls: list[tuple[str, dict]] = []
        self.render_args: dict | None = None
        self.revision = {"project_id": PROJECT_ID, "change_count": 4,
                         "path": PROJECT_PATH}
        self.track = {"guid": TRACK_GUID, "index": 0, "name": "Guitar",
                      "volume": 0.75, "pan": -0.2, "mute": False, "solo": 0,
                      "fx": [{"guid": "{FX}", "name": "EQ"}],
                      "items": [{"guid": ITEM_GUID, "position_s": 0.0,
                                 "length_s": 1.0}]}
        self.master = {"guid": MASTER_GUID, "index": -1, "name": "MASTER",
                       "volume": 1.0, "pan": 0.0, "mute": False, "solo": 0,
                       "fx": []}

    def project_context(self, project_id: str | None = None, **_: object) -> dict:
        return {
            "revision": dict(self.revision),
            "path": self.revision["path"],
            "name": "Wave C Analyze",
            "track_count": 1,
            "tracks": [dict(self.track, items=list(self.track["items"]))],
            "master": dict(self.master),
            "selection": {"track_guids": []},
            "tempo": {"bpm": 120.0, "numerator": 4, "denominator": 4,
                      "markers": []},
            "transport": {"play_state": 0, "position_s": 0.0},
            "length_s": 1.0,
        }

    def project_digest(self, project_id: str | None = None) -> dict:
        return {
            "project_id": PROJECT_ID,
            "path": PROJECT_PATH,
            "change_count": self.revision["change_count"],
            "digest_version": 1,
            "coverage": {name: True for name in FULL_COVERAGE},
            "missing": [],
            "fields": {
                "tracks": [{"guid": TRACK_GUID, "fx": [], "sends": []}],
                "items": [{"guid": ITEM_GUID, "take_guid": ""}],
                "tempo": [{"time_s": 0.0, "bpm": 120.0, "numerator": 4,
                           "denominator": 4}],
                **full_extra_fields([TRACK_GUID], MASTER_GUID),
            },
        }

    def capture_render(self, *, expected_revision: dict, output_dir: str,
                       job_id: str, expected_track_count: int,
                       timeout_s: float = 600.0,
                       section_start_s: float | None = None,
                       section_end_s: float | None = None,
                       master_only: bool = False, channels: str = "stereo",
                       sample_rate: int | None = None,
                       tail_ms: int = 0) -> dict:
        self.render_args = {
            "expected_revision": dict(expected_revision),
            "section_start_s": section_start_s, "section_end_s": section_end_s,
            "channels": channels, "sample_rate": sample_rate, "tail_ms": tail_ms,
            "master_only": master_only,
        }
        before = dict(self.revision)
        after = {**self.revision, "change_count": self.revision["change_count"] + 1}
        self.revision = after
        rate = sample_rate or SAMPLE_RATE
        channel_count = 1 if channels == "mono" else 2
        base = 1.0 if section_start_s is None else section_end_s - section_start_s
        written_tail = (self.file_tail_s if self.file_tail_s is not None
                        else tail_ms / 1000.0)
        directory = Path(output_dir)
        rendered_tracks = [] if master_only else [self.track]
        files: list[dict] = []
        for index, track in enumerate(rendered_tracks):
            path = directory / f"rali-{job_id}-{index + 1:02d}-{track['name']}.wav"
            _write_tail_wav(path, section_end_s=base, tail_s=written_tail,
                            sample_rate=rate, channels=channel_count)
            files.append({"source_guid": track["guid"], "name": track["name"],
                          "track_index": index, "is_master": False,
                          "path": str(path), "bytes": path.stat().st_size})
        master_path = directory / f"rali-{job_id}-00-MASTER.wav"
        _write_tail_wav(master_path, section_end_s=base, tail_s=written_tail,
                        sample_rate=rate, channels=channel_count)
        files.append({"source_guid": MASTER_GUID, "name": "MASTER",
                      "track_index": -1, "is_master": True,
                      "path": str(master_path),
                      "bytes": master_path.stat().st_size})
        result = {
            "project_id": PROJECT_ID,
            "revision": before,
            "revision_after": after,
            "capture_point": ("native_render_master_only" if master_only
                              else "native_render_track_stems_plus_master"),
            "render_action": "File: Render project",
            "render_settings_used": {"RENDER_CHANNELS": float(channel_count),
                                     "RENDER_TAILMS": float(tail_ms)},
            "channels": channel_count,
            "sample_rate": rate,
            "tail_s": tail_ms / 1000.0,
            "files": files,
        }
        if self.restore_report is not None:
            result["restore_report"] = self.restore_report
        return result

    def call(self, operation: str, arguments: dict | None = None, **_: object) -> dict:
        self.calls.append((operation, dict(arguments or {})))
        if operation == "project.render_settings.read":
            return {"project_id": self.revision["project_id"],
                    "revision": dict(self.revision),
                    "render_settings": {"format": "wav"},
                    "render_targets": "C:/renders/old.wav", "dirty": 0}
        raise AssertionError(f"unexpected bridge operation: {operation}")


class RecordingBridge:
    """Records any call; used to prove a rejection happens before the bridge."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def project_context(self, *args: object, **kwargs: object) -> dict:
        self.calls.append("project_context")
        raise AssertionError("project_context must not be called")

    def capture_render(self, *args: object, **kwargs: object) -> dict:
        self.calls.append("capture_render")
        raise AssertionError("capture_render must not be called")

    def call(self, *args: object, **kwargs: object) -> dict:
        self.calls.append("call")
        raise AssertionError("bridge.call must not be called")


def test_analyze_with_tail_reports_the_project_section_as_measured(
        tmp_path: Path) -> None:
    bridge = FakeAnalyzeBridge()

    report = ProjectSession(bridge).analyze(
        tmp_path / "artifacts", section=SECTION, tail_ms=TAIL_MS, parallel=False,
    )

    assert bridge.render_args["tail_ms"] == TAIL_MS
    assert bridge.render_args["section_start_s"] == 0.0
    assert bridge.render_args["section_end_s"] == 1.0
    assert report["section"] == {"start_s": 0.0, "end_s": 1.0}
    assert report["tail_s"] == pytest.approx(TAIL_S)

    capture = report["capture"]
    assert capture["tail_s"] == pytest.approx(TAIL_S)
    assert capture["capture_id"].startswith("cap_")
    assert capture["method"] == "native_render"
    assert capture["stages"] == ["track_stem_post_fx", "master_mix_post_fx"]
    assert capture["revision_before"]["counter"] == 4
    assert capture["revision_after"]["counter"] == 5
    assert capture["coverage"]["assets"] == 2
    assert capture["coverage"]["digest_missing"] == []
    assert capture["warnings"] == []
    assert capture["time_window_project"] == {"start_s": 0.0, "end_s": 1.0}
    assert capture["time_window_file"] == {"start_s": 0.0, "end_s": 1.25}
    assert capture["cleanup"]["removed_job_directory"] is True
    assert capture["cleanup"]["removed_file_count"] == 2

    window = report["capture_time_window"]
    assert window["project_end_s"] == pytest.approx(1.0)
    assert window["file_end_s"] == pytest.approx(1.25)
    assert window["tail_s"] == pytest.approx(TAIL_S)

    assert set(report["sources"]) == {TRACK_GUID, MASTER_GUID}
    for source_id, source in report["sources"].items():
        analysis_window = source["analysis_window"]
        assert analysis_window["project_start_s"] == 0.0
        assert analysis_window["project_end_s"] == pytest.approx(1.0)
        assert analysis_window["file_start_s"] == 0.0
        assert analysis_window["file_end_s"] == pytest.approx(1.25)
        assert analysis_window["measured_start_s"] == 0.0
        assert analysis_window["measured_end_s"] == pytest.approx(1.0)
        assert analysis_window["tail_s"] == pytest.approx(TAIL_S)
        assert analysis_window["tail_included_in_measurement"] is False
        assert analysis_window["coverage"] == "project_section"

        # The per-file capture context still carries the real 1.25 s window.
        assert source["capture"]["time_window"]["file_end_s"] == pytest.approx(1.25)
        assert source["capture"]["asset_id"].startswith("ast_sha256_")

        # Measured numbers describe 0..1 only: a 1.25 s measurement would
        # report the -0.9 dBFS tail instead of the -34 dBFS section.
        assert source["duration_s"] == pytest.approx(1.0)
        peak_dbfs = source["metrics"]["peak_dbfs"]["value"]
        assert peak_dbfs == pytest.approx(QUIET_PEAK_DBFS, abs=1.0)
        times = source["curves"]["temporal_profile"]["times"]
        assert times and max(times) <= 1.0


def test_analyze_refuses_a_render_that_is_longer_than_the_declared_tail(
        tmp_path: Path) -> None:
    bridge = FakeAnalyzeBridge(file_tail_s=TAIL_S)

    with pytest.raises(BridgeUnavailable,
                       match="does not cover the requested project section"):
        ProjectSession(bridge).analyze(
            tmp_path / "artifacts", section=SECTION, tail_ms=0, parallel=False,
        )


def test_analyze_rejects_accessor_tail_before_any_bridge_or_workspace(
        tmp_path: Path) -> None:
    bridge = RecordingBridge()
    root = tmp_path / "artifacts"

    with pytest.raises(ValidationError, match="tail"):
        ProjectSession(bridge).analyze(root, capture_mode="accessor",
                                       tail_ms=TAIL_MS)

    assert bridge.calls == []
    assert not root.exists()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"tail_ms": -1}, "tail_ms"),
        ({"tail_ms": True}, "tail_ms"),
        ({"tail_ms": 1.5}, "tail_ms"),
        ({"channels": "quad"}, "channels"),
        ({"channels": None}, "channels"),
        ({"sample_rate": 0}, "sample_rate"),
        ({"sample_rate": True}, "sample_rate"),
    ],
)
def test_analyze_rejects_invalid_render_parameters_before_capture(
        tmp_path: Path, kwargs: dict, message: str) -> None:
    bridge = RecordingBridge()

    with pytest.raises(ValidationError, match=message):
        ProjectSession(bridge).analyze(tmp_path / "artifacts", **kwargs)

    assert bridge.calls == []


def test_analyze_refuses_a_capture_without_a_restore_report(
        tmp_path: Path) -> None:
    bridge = FakeAnalyzeBridge(restore_report={"restored": False, "failures": []})
    root = tmp_path / "artifacts"

    with pytest.raises(BridgeUnavailable, match="did not confirm") as raised:
        ProjectSession(bridge).analyze(root, section=SECTION, tail_ms=TAIL_MS,
                                       parallel=False)

    assert raised.value.details["field"] == "restore_report"
    assert not list(root.glob("job-*"))


def test_rendered_buffer_maps_tail_to_the_file_window_only(
        tmp_path: Path) -> None:
    path = tmp_path / "tail-render.wav"
    _write_tail_wav(path)

    audio = _load_rendered_audio(str(path), SECTION, TAIL_S)

    assert audio.start_s == 0.0
    assert audio.duration_s == pytest.approx(1.25)
    assert audio.provenance["render_window"] == {"start_s": 0.0, "end_s": 1.0}
    assert audio.provenance["project_window"] == {"start_s": 0.0, "end_s": 1.0}
    assert audio.provenance["file_window"] == {"start_s": 0.0, "end_s": 1.25}
    assert audio.provenance["tail_s"] == pytest.approx(TAIL_S)

    measured = measure_audio(audio, section=SECTION)
    assert measured.duration_s == pytest.approx(1.0)
    assert measured.metrics["peak_dbfs"].value == pytest.approx(
        QUIET_PEAK_DBFS, abs=1.0)

    with pytest.raises(BridgeUnavailable, match="duration does not match"):
        _load_rendered_audio(str(path), SECTION)


def test_parallel_analyze_passes_section_and_tail_to_every_worker(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = FakeAnalyzeBridge()
    payloads: list[tuple] = []

    class InlinePool:
        def __init__(self, max_workers: int):
            self.max_workers = max_workers

        def __enter__(self) -> "InlinePool":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def map(self, function, jobs):
            return [function(job) for job in jobs]

    def fake_measure(payload: tuple) -> MeasurementReport:
        payloads.append(payload)
        path, section, tail_s, project_end_s = payload
        assert tail_s == pytest.approx(TAIL_S)
        assert project_end_s is None
        return MeasurementReport(
            source=path, sample_rate=SAMPLE_RATE, channels=2,
            duration_s=section.duration_s, section=section, metrics={},
            curves={"temporal_profile": {"times": [], "cells": []}},
        )

    monkeypatch.setattr(session_module, "_main_module_can_spawn", lambda: True)
    monkeypatch.setattr(session_module, "ProcessPoolExecutor", InlinePool)
    monkeypatch.setattr(session_module, "_measure_source_job", fake_measure)

    report = ProjectSession(bridge).analyze(
        tmp_path / "artifacts", section=SECTION, tail_ms=TAIL_MS, parallel=True,
    )

    assert len(payloads) == 2
    assert [payload[1] for payload in payloads] == [SECTION, SECTION]
    assert all(payload[2] == pytest.approx(TAIL_S) for payload in payloads)
    for source in report["sources"].values():
        assert source["analysis_window"]["file_end_s"] == pytest.approx(1.25)
        assert source["analysis_window"]["measured_end_s"] == pytest.approx(1.0)
