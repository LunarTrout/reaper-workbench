from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from reaper_audio.daw.session import (
    EditOperation,
    ProjectRevision,
    ProjectSession,
    _load_rendered_audio,
)
from reaper_audio.errors import BridgeUnavailable, StaleContext, ValidationError
from reaper_audio.models import AudioBuffer, Section


class FakeBridge:
    def __init__(self, *, malformed: bool = False, markers: list | None = None,
                 tempo_point_overrides: dict | None = None,
                 tempo_source: str = "test_tempo_map"):
        self.calls: list[tuple[str, dict]] = []
        self.malformed = malformed
        self.tempo_point_overrides = tempo_point_overrides or {}
        self.tempo_source = tempo_source
        self.revision = {
            "project_id": "reaper:test-project",
            "change_count": 4,
            "path": "C:/test.rpp",
        }
        self.markers = markers if markers is not None else [
            {"time_s": 0.0, "bpm": 120.0, "numerator": 4,
             "denominator": 4, "linear": False}
        ]

    def project_context(self, project_id: str | None = None, **_: object) -> dict:
        return {
            "revision": dict(self.revision),
            "tempo": {"bpm": 120.0, "numerator": 4, "denominator": 4,
                      "markers": self.markers},
        }

    def call(self, operation: str, arguments: dict) -> dict:
        self.calls.append((operation, arguments))
        points = [{"time_s": time_s, "bpm": 120.0, "numerator": 4,
                   "denominator": 4, "effective_quarter_bpm": 120.0,
                   **self.tempo_point_overrides}
                  for time_s in arguments["times_s"]]
        if self.malformed and points:
            points.pop()
        return {"project_id": arguments["project_id"],
                "revision": dict(self.revision), "points": points,
                "source": self.tempo_source}


class FakeTransactionBridge:
    def __init__(self, *, malformed_preview: bool = False):
        self.revision = {
            "project_id": "reaper:test-project",
            "change_count": 4,
            "path": "C:/test.rpp",
        }
        self.calls: list[tuple[str, dict]] = []
        self.malformed_preview = malformed_preview

    def project_context(self, project_id: str | None = None, **_: object) -> dict:
        return {"revision": dict(self.revision)}

    def call(self, operation: str, arguments: dict) -> dict:
        self.calls.append((operation, arguments))
        if operation == "transaction.preview":
            if self.malformed_preview:
                return {"plan_id": ""}
            return {"plan_id": "bridge-plan-1",
                    "revision": dict(self.revision), "before": []}
        if operation == "transaction.apply":
            dry_run = arguments["dry_run"]
            return {"plan_id": arguments["plan"]["plan_id"],
                    "applied": not dry_run, "dry_run": dry_run}
        if operation == "transaction.undo":
            return {"undone": True,
                    "revision_before": arguments["expected_revision"]}
        raise AssertionError(operation)


def test_project_session_reads_many_exact_tempo_map_points_in_one_call() -> None:
    bridge = FakeBridge()
    session = ProjectSession(bridge, project_id="reaper:test-project")

    result = session.tempo_at([0.0, 1.25, 8.5])

    assert len(result["points"]) == 3
    assert result["points"][1] == {
        "time_s": 1.25, "bpm": 120.0, "numerator": 4, "denominator": 4,
        "effective_quarter_bpm": 120.0,
    }
    assert bridge.calls == [("project.tempo_at", {
        "project_id": "reaper:test-project", "times_s": [0.0, 1.25, 8.5],
    })]


def test_project_session_uses_project_base_tempo_when_map_has_no_markers() -> None:
    bridge = FakeBridge(markers=[])

    result = ProjectSession(bridge, project_id="reaper:test-project").tempo_at(
        [0.0, 0.1, 120.0]
    )

    assert result["source"] == "project.base_tempo_no_markers"
    assert result["points"] == [
        {"time_s": 0.0, "bpm": 120.0, "numerator": 4, "denominator": 4,
         "effective_quarter_bpm": 120.0},
        {"time_s": 0.1, "bpm": 120.0, "numerator": 4, "denominator": 4,
         "effective_quarter_bpm": 120.0},
        {"time_s": 120.0, "bpm": 120.0, "numerator": 4, "denominator": 4,
         "effective_quarter_bpm": 120.0},
    ]
    assert bridge.calls == []


def test_capture_audio_binds_processed_track_and_master_to_metadata_and_cleans(
    tmp_path: Path,
) -> None:
    class CaptureBridge(FakeBridge):
        def __init__(self) -> None:
            super().__init__(markers=[])
            self.render_args = None

        def project_context(self, project_id: str | None = None, **kwargs: object) -> dict:
            self.context_options = kwargs
            return {
                "revision": dict(self.revision),
                "path": "C:/test.rpp",
                "name": "Test",
                "track_count": 1,
                "tracks": [{"guid": "{TRACK}", "index": 0, "name": "Guitar",
                            "volume": 0.75, "pan": -0.2, "mute": False,
                            "solo": 0, "fx": [{"guid": "{FX}", "name": "EQ"}]}],
                "master": {"guid": "{MASTER}", "index": -1, "name": "MASTER",
                           "volume": 1.0, "pan": 0.0, "mute": False,
                           "solo": 0, "fx": []},
                "tempo": {"bpm": 123.0, "numerator": 7, "denominator": 8,
                          "markers": []},
            }

        def capture_render(self, **kwargs: object) -> dict:
            self.render_args = kwargs
            revision_before = dict(self.revision)
            revision_after = {**self.revision,
                             "change_count": self.revision["change_count"] + 22}
            self.revision = revision_after
            directory = Path(kwargs["output_dir"])
            job_id = kwargs["job_id"]
            track_path = directory / f"rali-{job_id}-01-Guitar.wav"
            master_path = directory / f"rali-{job_id}-00-MASTER.wav"
            sf.write(track_path, np.zeros(1024), 8_000, subtype="FLOAT")
            sf.write(master_path, np.zeros(1024), 8_000, subtype="FLOAT")
            return {
                "project_id": self.revision["project_id"],
                "revision": revision_before,
                "revision_after": dict(revision_after),
                "capture_point": "native_render_track_stems_plus_master",
                "render_action": "File: Render project",
                "files": [
                    {"source_guid": "{TRACK}", "name": "Guitar", "track_index": 0,
                     "is_master": False, "path": str(track_path),
                     "bytes": track_path.stat().st_size},
                    {"source_guid": "{MASTER}", "name": "MASTER", "track_index": -1,
                     "is_master": True, "path": str(master_path),
                     "bytes": master_path.stat().st_size},
                ],
            }

        def call(self, operation: str, arguments: dict) -> dict:
            if operation == "project.render_settings.read":
                return {"project_id": self.revision["project_id"],
                        "revision": dict(self.revision), "render_settings": 0,
                        "render_targets": "C:/renders/old.wav", "dirty": 0}
            raise AssertionError(operation)

    bridge = CaptureBridge()
    artifact_root = tmp_path / "captures"
    session = ProjectSession(bridge)

    with session.capture_audio(artifact_root) as captured:
        track = captured["tracks"][0]
        assert track["pan"] == -0.2
        assert track["volume"] == 0.75
        assert track["fx"][0]["name"] == "EQ"
        assert Path(track["capture"]["path"]).exists()
        assert captured["master"]["capture"]["is_master"] is True
        assert track["capture"]["capture_method"] == "reaper_native_offline_render"
        assert track["capture"]["signal_stage"] == "reaper_rendered_track_stem"
        assert captured["master"]["capture"]["signal_stage"] == "reaper_rendered_master_mix"
        assert captured["project"]["tempo"]["bpm"] == 123.0
        assert captured["time_window"]["timeline"] == "project_seconds"
        assert captured["time_window"]["project_start_s"] == 0.0
        assert captured["tracks"][0]["capture"]["audio_format"]["frames"] == 1024
        job_directory = Path(captured["artifact_job"]["directory"])
        assert bridge.render_args["expected_track_count"] == 1
        assert bridge.render_args["expected_revision"]["change_count"] == 4
        assert captured["project"]["capture_revision"]["change_count"] == 4
        assert captured["project"]["revision"]["change_count"] == 26

    assert not job_directory.exists()
    assert captured["artifact_job"]["cleanup"]["removed_job_directory"] is True
    assert len(captured["artifact_job"]["cleanup"]["removed_files"]) == 2


def test_selected_render_maps_file_zero_to_absolute_project_time(
        tmp_path: Path) -> None:
    path = tmp_path / "selected-render.wav"
    sample_rate = 8_000
    time = np.arange(2 * sample_rate) / sample_rate
    sf.write(path, 0.2 * np.sin(2 * np.pi * 440 * time),
             sample_rate, subtype="FLOAT")

    audio = _load_rendered_audio(str(path), Section(37.5, 39.5))

    assert audio.duration_s == pytest.approx(2.0)
    assert audio.start_s == pytest.approx(37.5)
    assert audio.provenance["render_window"] == {
        "start_s": 37.5, "end_s": 39.5,
    }
    assert audio.provenance["file_window"] == {
        "start_s": 0.0, "end_s": 2.0,
    }


def test_selected_render_rejects_wrong_duration(tmp_path: Path) -> None:
    path = tmp_path / "wrong-render.wav"
    sf.write(path, np.zeros(16_000), 8_000, subtype="FLOAT")

    with pytest.raises(BridgeUnavailable, match="duration does not match"):
        _load_rendered_audio(str(path), Section(37.5, 40.0))


def test_project_session_analyze_bundles_capture_context_tempo_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = {"project_id": "reaper:test-project", "change_count": 26,
                "path": "C:/test.rpp"}
    captured = {
        "capture_point": "native_render_track_stems_plus_master",
        "render_action": "File: Render project",
        "time_window": {"timeline": "project_seconds", "project_start_s": 40.0,
                        "project_end_s": 48.0, "file_start_s": 0.0,
                        "file_end_s": 8.0},
        "project": {"capture_revision": {**revision, "change_count": 4},
                    "revision": revision, "path": revision["path"],
                    "name": "Test", "tempo": {"bpm": 120.0}},
        "tracks": [{"guid": "{TRACK}", "name": "Guitar", "volume": 0.75,
                    "pan": -0.2, "fx": [{"name": "EQ"}],
                    "capture": {"path": "temporary-track.wav", "bytes": 100,
                                "source_guid": "{TRACK}", "is_master": False,
                                "signal_stage": "reaper_rendered_track_stem"}}],
        "master": {"guid": "{MASTER}", "name": "MASTER", "volume": 1.0,
                   "pan": 0.0, "fx": [],
                   "capture": {"path": "temporary-master.wav", "bytes": 100,
                               "source_guid": "{MASTER}", "is_master": True,
                               "signal_stage": "reaper_rendered_master_mix"}},
        "artifact_job": {"job_id": "job-1", "directory": "temporary-dir",
                         "cleanup": None},
    }

    @contextmanager
    def fake_capture_audio(self: ProjectSession, *_args: object, **_kwargs: object):
        yield captured
        captured["artifact_job"]["cleanup"] = {
            "removed_files": ["track.wav", "master.wav"],
            "preserved_paths": [], "removed_job_directory": True,
            "retained": bool(_kwargs.get("preserve_artifacts", False)),
        }

    loaded_paths: list[str] = []

    def fake_load_audio(path: str, section: object = None) -> AudioBuffer:
        loaded_paths.append(path)
        return AudioBuffer(np.zeros(8_000), 8_000, source=path)

    analysis_call: dict = {}

    def fake_analyze_bundle(sources: dict, **kwargs: object) -> dict:
        analysis_call["sources"] = sources
        analysis_call["kwargs"] = kwargs
        return {
            "sources": {
                key: {"curves": {"temporal_profile": {
                    "times": [0.0, 0.1], "cells": [{}, {}],
                }}}
                for key in sources
            },
        }

    monkeypatch.setattr(ProjectSession, "capture_audio", fake_capture_audio)
    monkeypatch.setattr("reaper_audio.daw.session.load_audio", fake_load_audio)
    monkeypatch.setattr("reaper_audio.daw.session.analyze_bundle", fake_analyze_bundle)
    session = ProjectSession(FakeBridge(), project_id=revision["project_id"])

    def fake_tempo_at(times_s: list[float]) -> dict:
        return {
            "project_id": revision["project_id"], "revision": revision,
            "source": "test_tempo_map",
            "points": [{"time_s": time_s, "bpm": 123.0, "numerator": 7,
                        "denominator": 8, "effective_quarter_bpm": 61.5}
                       for time_s in times_s],
        }

    monkeypatch.setattr(session, "tempo_at", fake_tempo_at)
    result = session.analyze("temporary-root", parallel=False,
                             include_partial_loudness=True)

    assert loaded_paths == ["temporary-track.wav", "temporary-master.wav"]
    assert set(analysis_call["sources"]) == {"{TRACK}", "{MASTER}"}
    assert analysis_call["kwargs"]["mix_key"] == "{MASTER}"
    assert analysis_call["kwargs"]["anchor_key"] == "{MASTER}"
    assert result["sources"]["{TRACK}"]["track_context"]["pan"] == -0.2
    assert result["sources"]["{TRACK}"]["track_context"]["volume"] == 0.75
    assert result["sources"]["{TRACK}"]["capture"]["signal_stage"] == "reaper_rendered_track_stem"
    assert result["sources"]["{MASTER}"]["capture"]["signal_stage"] == "reaper_rendered_master_mix"
    assert "path" not in result["sources"]["{TRACK}"]["capture"]
    assert result["sources"]["{MASTER}"]["curves"]["temporal_profile"][
        "cells"][1]["project_tempo"]["bpm"] == 123.0
    assert result["project_tempo"]["points_attached_to_tape_cells"] == 2
    assert result["capture"]["source_count_including_master"] == 2
    assert result["capture"]["capture_revision"] == {**revision, "change_count": 4}
    assert result["capture"]["time_window"] == captured["time_window"]
    assert result["capture"]["cleanup"] == {
        "removed_file_count": 2, "removed_job_directory": True,
        "preserved_artifact_count": 0,
        "retained_for_resource_lease": False,
    }


def test_parallel_analyze_reports_capture_sources_without_decoding_buffers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = {"project_id": "reaper:test-project", "change_count": 26,
                "path": "C:/test.rpp"}
    time_window = {"timeline": "project_seconds", "project_start_s": 40.0,
                   "project_end_s": 48.0, "file_start_s": 0.0,
                   "file_end_s": 8.0}
    captured = {
        "capture_point": "native_render_track_stems_plus_master",
        "render_action": "File: Render project",
        "time_window": time_window,
        "project": {"capture_revision": revision, "revision": revision,
                    "path": revision["path"], "name": "Test", "tempo": {}},
        "tracks": [{"guid": "{TRACK}", "name": "Guitar", "capture": {
            "path": "temporary-track.wav", "signal_stage": "reaper_rendered_track_stem",
        }}],
        "master": {"guid": "{MASTER}", "name": "MASTER", "capture": {
            "path": "temporary-master.wav", "signal_stage": "reaper_rendered_master_mix",
        }},
        "artifact_job": {"job_id": "job-parallel", "directory": "temporary-dir",
                         "cleanup": None},
    }

    @contextmanager
    def fake_capture_audio(self: ProjectSession, *_args: object, **_kwargs: object):
        yield captured
        captured["artifact_job"]["cleanup"] = {
            "removed_files": ["track.wav", "master.wav"],
            "preserved_paths": [], "removed_job_directory": True,
            "retained": False,
        }

    class InlinePool:
        def __init__(self, max_workers: int):
            self.max_workers = max_workers

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def map(self, function, jobs):
            return [function(job) for job in jobs]

    monkeypatch.setattr(ProjectSession, "capture_audio", fake_capture_audio)
    monkeypatch.setattr("reaper_audio.daw.session._main_module_can_spawn", lambda: True)
    monkeypatch.setattr("reaper_audio.daw.session.ProcessPoolExecutor", InlinePool)
    monkeypatch.setattr("reaper_audio.daw.session._measure_source_job",
                        lambda job: {"path": job[0]})
    monkeypatch.setattr("reaper_audio.daw.session.analyze_bundle", lambda sources, **_: {
        "sources": {key: {"curves": {"temporal_profile": {
            "times": [], "cells": [],
        }}} for key in sources},
    })

    result = ProjectSession(FakeBridge(markers=[])).analyze(
        "temporary-root", parallel=True,
    )

    assert result["capture"]["source_count_including_master"] == 2
    assert result["capture"]["capture_revision"] == revision
    assert result["capture"]["time_window"] == time_window
    assert result["sources"]["{TRACK}"]["capture"]["signal_stage"] == "reaper_rendered_track_stem"
    assert result["sources"]["{MASTER}"]["capture"]["signal_stage"] == "reaper_rendered_master_mix"


def test_project_session_analyze_can_return_temporary_audio_resource_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = {"project_id": "reaper:test-project", "change_count": 26,
                "path": "C:/test.rpp"}
    captured = {
        "capture_point": "native_render_track_stems_plus_master",
        "render_action": "File: Render project",
        "project": {"capture_revision": revision, "revision": revision,
                    "path": revision["path"], "name": "Test", "tempo": {}},
        "tracks": [{"guid": "{TRACK}", "name": "Guitar", "capture": {
            "path": "retained-track.wav", "bytes": 12,
        }}],
        "master": {"guid": "{MASTER}", "name": "MASTER", "capture": {
            "path": "retained-master.wav", "bytes": 13,
        }},
        "artifact_job": {"job_id": "job-2", "directory": "retained-dir",
                         "cleanup": None},
    }

    @contextmanager
    def fake_capture_audio(self: ProjectSession, *_args: object, **kwargs: object):
        assert kwargs["preserve_artifacts"] is True
        yield captured
        captured["artifact_job"]["cleanup"] = {
            "removed_files": [], "preserved_paths": [],
            "removed_job_directory": False, "retained": True,
        }

    monkeypatch.setattr(ProjectSession, "capture_audio", fake_capture_audio)
    monkeypatch.setattr("reaper_audio.daw.session.load_audio", lambda *_: AudioBuffer(
        np.zeros(8_000), 8_000))
    monkeypatch.setattr("reaper_audio.daw.session.analyze_bundle", lambda sources, **_: {
        "sources": {key: {} for key in sources},
    })
    session = ProjectSession(FakeBridge(markers=[]), project_id=revision["project_id"])
    monkeypatch.setattr(session, "tempo_at", lambda times: {
        "project_id": revision["project_id"], "revision": revision,
        "source": "test", "points": [],
    })

    result = session.analyze("temporary-root", parallel=False, retain_audio=True)

    assert result["audio_files"] == {
        "{TRACK}": "retained-track.wav", "{MASTER}": "retained-master.wav",
    }
    assert result["capture"]["cleanup"]["retained_for_resource_lease"] is True


@pytest.mark.parametrize("times", [[], [True], [float("nan")], [float("inf")]])
def test_project_session_rejects_invalid_tempo_query_positions(times: list) -> None:
    with pytest.raises(ValidationError):
        ProjectSession(FakeBridge()).tempo_at(times)


def test_project_session_rejects_incomplete_tempo_map_response() -> None:
    with pytest.raises(BridgeUnavailable, match="incomplete"):
        ProjectSession(FakeBridge(malformed=True)).tempo_at([0.0, 1.0])


@pytest.mark.parametrize("overrides", [
    {"bpm": True},
    {"numerator": 0},
    {"numerator": True},
    {"denominator": 0},
    {"denominator": True},
    {"effective_quarter_bpm": float("nan")},
])
def test_project_session_rejects_invalid_tempo_map_values(overrides: dict) -> None:
    with pytest.raises(BridgeUnavailable, match="invalid tempo-map point"):
        ProjectSession(FakeBridge(tempo_point_overrides=overrides)).tempo_at([0.0])


def test_project_session_rejects_tempo_map_without_source_metadata() -> None:
    with pytest.raises(BridgeUnavailable, match="incomplete"):
        ProjectSession(FakeBridge(tempo_source=" ")).tempo_at([0.0])


def test_project_session_previews_and_applies_revision_bound_edit() -> None:
    bridge = FakeTransactionBridge()
    session = ProjectSession(bridge, project_id="reaper:test-project")
    operation = EditOperation("track.set", "{TRACK-GUID}", {"pan": 0.25})

    plan = session.plan([operation], reason="Move guitar slightly right")
    preview = session.apply(plan, dry_run=True)
    applied = session.apply(plan, dry_run=False)

    assert plan.plan_id == "bridge-plan-1"
    assert preview["applied"] is False
    assert applied["applied"] is True


def test_project_session_refuses_to_preview_against_a_stale_revision() -> None:
    bridge = FakeTransactionBridge()
    session = ProjectSession(bridge, project_id="reaper:test-project")
    expected = dict(bridge.revision, change_count=bridge.revision["change_count"] - 1)

    with pytest.raises(StaleContext, match="changed before the edit plan"):
        session.plan([EditOperation("track.set", "{TRACK-GUID}", {"pan": 0.2})],
                     reason="Move track", expected_revision=expected)

    assert bridge.calls == []


def test_project_session_blocks_stale_transaction_before_bridge_apply() -> None:
    bridge = FakeTransactionBridge()
    session = ProjectSession(bridge)
    plan = session.plan(
        [EditOperation("track.set", "{TRACK-GUID}", {"mute": True})],
        reason="Mute track",
    )
    bridge.revision["change_count"] += 1

    with pytest.raises(StaleContext):
        session.apply(plan, dry_run=False)
    assert [call[0] for call in bridge.calls] == ["transaction.preview"]


def test_project_session_rejects_malformed_preview_and_invalid_plan_inputs() -> None:
    session = ProjectSession(FakeTransactionBridge(malformed_preview=True))
    operation = EditOperation("track.set", "{TRACK-GUID}", {"volume": 1.0})

    with pytest.raises(BridgeUnavailable, match="invalid edit-plan preview"):
        session.plan([operation], reason="Set gain")
    with pytest.raises(ValidationError):
        ProjectSession(FakeTransactionBridge()).plan([], reason="empty")
    with pytest.raises(ValidationError):
        ProjectSession(FakeTransactionBridge()).plan([operation], reason="")


def test_project_session_undo_requires_and_checks_project_revision() -> None:
    bridge = FakeTransactionBridge()
    session = ProjectSession(bridge)
    revision = ProjectRevision.from_dict(bridge.revision)

    result = session.undo(expected_revision=revision)

    assert result["undone"] is True
    assert bridge.calls == [("transaction.undo", {
        "expected_revision": bridge.revision,
    })]


@pytest.mark.parametrize(
    ("capture_mode", "message"),
    [("accessor", "pre-FX"), (None, "capture_mode"), ([], "capture_mode"),
     ("unknown", "capture_mode")],
)
def test_analyze_rejects_raw_accessor_as_processed_mix(
    tmp_path: Path, capture_mode: object, message: str,
) -> None:
    session = ProjectSession(object())

    with pytest.raises(ValidationError, match=message):
        session.analyze(str(tmp_path), capture_mode=capture_mode)
