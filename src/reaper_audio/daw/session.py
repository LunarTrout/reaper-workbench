"""Project snapshots and revision-bound edit plans."""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Iterator, Literal

import soundfile as sf

from ..analysis.artifacts import AudioArtifactWorkspace
from ..analysis.audio import load_audio
from ..analysis.bundle import analyze_bundle
from ..analysis.metrics import measure_audio
from ..errors import (ArtifactCleanupError, BridgeUnavailable, StaleContext,
                      ValidationError)
from ..models import AudioBuffer, Section
from ..reaper.bridge_client import ReaperBridge


def _main_module_can_spawn() -> bool:
    """multiprocessing spawn re-imports __main__; stdin/notebooks have no file."""
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None)
    return bool(path) and not str(path).startswith("<") and os.path.exists(path)


@dataclass(frozen=True, slots=True)
class ProjectRevision:
    project_id: str
    change_count: int
    path: str

    @classmethod
    def from_dict(cls, value: dict) -> "ProjectRevision":
        return cls(project_id=str(value.get("project_id", "")),
                   change_count=int(value.get("change_count", -1)),
                   path=str(value.get("path", "")))

    def to_dict(self) -> dict:
        return {"project_id": self.project_id, "change_count": self.change_count,
                "path": self.path}


@dataclass(frozen=True, slots=True)
class EditOperation:
    kind: str
    target_guid: str
    values: dict[str, Any]

    def to_dict(self) -> dict:
        return {"kind": self.kind, "target_guid": self.target_guid, "values": self.values}


@dataclass(frozen=True, slots=True)
class EditPlan:
    revision: ProjectRevision
    operations: tuple[EditOperation, ...]
    reason: str
    plan_id: str
    before: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"plan_id": self.plan_id, "revision": self.revision.to_dict(),
                "reason": self.reason,
                "operations": [item.to_dict() for item in self.operations],
                "before": self.before}


def _load_rendered_audio(path: str, section: Section | None) -> AudioBuffer:
    """Map a render file's local zero to its project-time selection."""
    decoded = load_audio(path)
    if section is None:
        return AudioBuffer(
            samples=decoded.samples, sample_rate=decoded.sample_rate,
            source=decoded.source, start_s=0.0,
            provenance={**decoded.provenance,
                        "timeline": "project_seconds",
                        "render_window": {"start_s": 0.0,
                                          "end_s": decoded.duration_s},
                        "file_window": {"start_s": 0.0,
                                        "end_s": decoded.duration_s}},
        )

    tolerance_s = 1.0 / decoded.sample_rate
    if abs(decoded.duration_s - section.duration_s) > tolerance_s:
        raise BridgeUnavailable(
            "Rendered audio duration does not match the requested project section",
            details={"path": path,
                     "requested_project_section": {
                         "start_s": section.start_s, "end_s": section.end_s},
                     "expected_duration_s": section.duration_s,
                     "actual_duration_s": decoded.duration_s,
                     "sample_rate": decoded.sample_rate},
        )
    return AudioBuffer(
        samples=decoded.samples, sample_rate=decoded.sample_rate,
        source=decoded.source, start_s=section.start_s,
        provenance={**decoded.provenance,
                    "timeline": "project_seconds",
                    "render_window": {"start_s": section.start_s,
                                      "end_s": section.start_s + decoded.duration_s},
                    "file_window": {"start_s": 0.0,
                                    "end_s": decoded.duration_s}},
    )


def _measure_source_job(payload: tuple[str, Section | None]):
    """Worker: load a rendered source and run the full measurement suite."""
    path, section = payload
    return measure_audio(_load_rendered_audio(path, section), section=section)


class ProjectSession:
    def __init__(self, bridge: ReaperBridge, *, project_id: str | None = None):
        self.bridge = bridge
        self.project_id = project_id

    def snapshot(self, *, project_id: str | None = None, include_items: bool = False,
                 include_fx_params: bool = False,
                 include_midi_notes: bool = False) -> dict:
        selected_id = project_id if project_id is not None else self.project_id
        return self.bridge.project_context(selected_id, include_items=include_items,
                                           include_fx_params=include_fx_params,
                                           include_midi_notes=include_midi_notes)

    @contextmanager
    def capture_audio(
        self, artifact_root: str, *, include_fx_params: bool = True,
        timeout_s: float = 600.0,
        section: Section | None = None,
        capture_mode: str = "render",
        preserve_artifacts: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Capture every processed track and master into a temporary job folder.

        Rendered files remain available only inside the ``with`` block and are
        removed on exit. The returned snapshot binds each file to the exact
        project revision plus its track fader, pan, mute/solo, and FX context.
        """
        if not isinstance(include_fx_params, bool):
            raise ValidationError("include_fx_params must be a boolean")
        if not isinstance(preserve_artifacts, bool):
            raise ValidationError("preserve_artifacts must be a boolean")
        if not isinstance(capture_mode, str) or capture_mode not in {"render", "accessor"}:
            raise ValidationError("capture_mode must be 'render' or 'accessor'")
        workspace = AudioArtifactWorkspace(artifact_root)
        captured: dict[str, Any] | None = None
        try:
            context = self.snapshot(include_fx_params=include_fx_params)
            revision = ProjectRevision.from_dict(context.get("revision", {}))
            tracks = context.get("tracks")
            master = context.get("master")
            if (not revision.project_id or revision.change_count < 0 or
                    not isinstance(tracks, list) or not isinstance(master, dict)):
                raise BridgeUnavailable("REAPER returned an incomplete audio capture context")
            if len(tracks) != context.get("track_count"):
                raise BridgeUnavailable("REAPER track count does not match its context snapshot")
            render_settings_before = self.bridge.call(
                "project.render_settings.read", {"project_id": revision.project_id}
            )
            if (not isinstance(render_settings_before, dict) or
                    render_settings_before.get("project_id") != revision.project_id or
                    not isinstance(render_settings_before.get("dirty"), (int, float))):
                raise BridgeUnavailable("REAPER returned invalid initial render settings")

            if capture_mode == "accessor":
                if section is not None:
                    sample_start, sample_end = section.start_s, section.end_s
                else:
                    sample_start = 0.0
                    sample_end = float(context.get("length_s") or 0.0)
                if sample_end <= sample_start:
                    raise BridgeUnavailable(
                        "Project length is unavailable for accessor capture",
                        details={"length_s": context.get("length_s")})
                sampled = self.bridge.sample_tracks(
                    project_id=revision.project_id,
                    output_dir=workspace.directory,
                    job_id=workspace.job_id,
                    start_s=sample_start,
                    end_s=sample_end,
                )
                master_rendered = self.bridge.capture_render(
                    expected_revision=revision.to_dict(),
                    output_dir=workspace.directory,
                    job_id=workspace.job_id,
                    expected_track_count=len(tracks),
                    timeout_s=timeout_s,
                    section_start_s=None if section is None else section.start_s,
                    section_end_s=None if section is None else section.end_s,
                    master_only=True,
                )
                rendered = {
                    **master_rendered,
                    "files": [*sampled.get("files", []),
                              *master_rendered.get("files", [])],
                    "capture_point":
                        "native_audio_accessor_pre_fx_tracks_plus_master_render",
                }
            else:
                rendered = self.bridge.capture_render(
                    expected_revision=revision.to_dict(),
                    output_dir=workspace.directory,
                    job_id=workspace.job_id,
                    expected_track_count=len(tracks),
                    timeout_s=timeout_s,
                    section_start_s=None if section is None else section.start_s,
                    section_end_s=None if section is None else section.end_s,
                )
            rendered_files = rendered.get("files")
            if not isinstance(rendered_files, list):
                raise BridgeUnavailable("REAPER returned an invalid rendered file list")
            registered = workspace.register_existing_files(
                [item.get("path") for item in rendered_files
                 if isinstance(item, dict)]
            )
            audio_metadata_by_guid: dict[str, dict[str, Any]] = {}
            durations: list[float] = []
            tolerances: list[float] = []
            for item, path in zip(rendered_files, registered):
                if not isinstance(item, dict):
                    raise BridgeUnavailable("REAPER returned an invalid rendered audio entry")
                guid = item.get("source_guid")
                try:
                    info = sf.info(path)
                except Exception as exc:
                    raise BridgeUnavailable(
                        "REAPER render is not a readable audio file",
                        details={"source_guid": guid, "path": path,
                                 "reason": str(exc)},
                    ) from exc
                if info.frames <= 0 or info.samplerate <= 0 or info.channels <= 0:
                    raise BridgeUnavailable(
                        "REAPER returned an empty or invalid audio file",
                        details={"source_guid": guid, "path": path,
                                 "frames": info.frames,
                                 "sample_rate": info.samplerate,
                                 "channels": info.channels},
                    )
                duration_s = info.frames / info.samplerate
                if section is not None and abs(duration_s - section.duration_s) > 1.0 / info.samplerate:
                    raise BridgeUnavailable(
                        "REAPER render does not cover the requested project section",
                        details={"source_guid": guid,
                                 "requested_project_section": {
                                     "start_s": section.start_s,
                                     "end_s": section.end_s},
                                 "actual_file_duration_s": duration_s,
                                 "sample_rate": info.samplerate},
                    )
                audio_metadata_by_guid[str(guid)] = {
                    "frames": int(info.frames),
                    "sample_rate": int(info.samplerate),
                    "channels": int(info.channels),
                    "duration_s": duration_s,
                    "file_start_s": 0.0,
                }
                durations.append(duration_s)
                tolerances.append(1.0 / info.samplerate)
            if durations and max(durations) - min(durations) > max(tolerances):
                raise BridgeUnavailable(
                    "REAPER returned track and master renders with different time windows",
                    details={"durations_s": durations,
                             "sample_rates": [item["sample_rate"]
                                              for item in audio_metadata_by_guid.values()]},
                )
            rendered_duration = durations[0] if durations else 0.0
            time_window = {
                "timeline": "project_seconds",
                "project_start_s": section.start_s if section is not None else 0.0,
                "project_end_s": ((section.start_s if section is not None else 0.0)
                                  + rendered_duration),
                "file_start_s": 0.0,
                "file_end_s": rendered_duration,
                "requested_section": None if section is None else {
                    "start_s": section.start_s, "end_s": section.end_s,
                },
            }
            context_after = self.snapshot(include_fx_params=include_fx_params)
            render_settings_after = self.bridge.call(
                "project.render_settings.read", {"project_id": revision.project_id}
            )
            render_revision_after = ProjectRevision.from_dict(
                rendered.get("revision_after", {})
            )
            current_revision = ProjectRevision.from_dict(
                context_after.get("revision", {})
            )
            # REAPER can publish the post-render change count slightly after
            # the render action returns. Poll the live revision instead of
            # assuming the first snapshot already sees the final number.
            for _ in range(50):
                if current_revision == render_revision_after:
                    break
                time.sleep(0.05)
                context_after = self.snapshot(include_fx_params=include_fx_params)
                current_revision = ProjectRevision.from_dict(
                    context_after.get("revision", {})
                )
            if current_revision != render_revision_after:
                raise StaleContext("Project changed while the audio capture was being verified",
                                   details={"capture_revision_after":
                                            render_revision_after.to_dict(),
                                            "current_revision": current_revision.to_dict()})
            context_keys = ("path", "name", "track_count", "tracks", "master",
                            "selection", "tempo")
            changed_context = [key for key in context_keys
                               if context.get(key) != context_after.get(key)]
            # Play position and cursor drift while REAPER renders; only the
            # transport's play state is part of the project state we must
            # preserve.
            before_transport = context.get("transport") or {}
            after_transport = context_after.get("transport") or {}
            if (not isinstance(before_transport, dict)
                    or not isinstance(after_transport, dict)
                    or before_transport.get("play_state") !=
                    after_transport.get("play_state")):
                changed_context.append("transport")
            render_keys = ("render_settings", "render_targets", "dirty")
            changed_render_settings = [
                key for key in render_keys
                if render_settings_before.get(key) != render_settings_after.get(key)
            ]
            if changed_context or changed_render_settings:
                raise BridgeUnavailable(
                    "REAPER project state was not fully restored after audio capture",
                    details={"changed_context": changed_context,
                             "changed_render_settings": changed_render_settings},
                )
            path_by_guid = {
                item["source_guid"]: str(path)
                for item, path in zip(rendered_files, registered)
            }
            capture_by_guid = {
                item["source_guid"]: item for item in rendered_files
                if isinstance(item, dict)
            }

            def describe_track(track: dict[str, Any]) -> dict[str, Any]:
                guid = track.get("guid")
                capture = capture_by_guid.get(guid)
                path = path_by_guid.get(guid)
                if not isinstance(guid, str) or capture is None or path is None:
                    raise BridgeUnavailable("A project track has no matching rendered WAV",
                                            details={"track_guid": guid})
                capture_context = {key: value for key, value in capture.items()
                                   if key != "path"} | {
                                       "path": path,
                                       "time_window": time_window,
                                       "audio_format": audio_metadata_by_guid[guid],
                                   }
                is_master = capture.get("is_master") is True
                if capture_mode == "accessor" and not is_master:
                    capture_context.update({
                        "capture_method": "reaper_audio_accessor",
                        "signal_stage": "immediately_pre_fx",
                        "track_fx_included": False,
                    })
                else:
                    capture_context.update({
                        "capture_method": "reaper_native_offline_render",
                        "signal_stage": (
                            "reaper_rendered_master_mix" if is_master
                            else "reaper_rendered_track_stem"
                        ),
                    })
                return {
                    **track,
                    "capture": capture_context,
                }

            captured = {
                "schema_version": 1,
                "capture_point": rendered.get("capture_point"),
                "render_action": rendered.get("render_action"),
                "time_window": time_window,
                "project": {
                    "capture_revision": revision.to_dict(),
                    "revision": current_revision.to_dict(),
                    "path": context.get("path"),
                    "name": context.get("name"),
                    "tempo": context.get("tempo"),
                },
                "tracks": [describe_track(track) for track in tracks],
                "master": describe_track(master),
                "artifact_job": {
                    "job_id": workspace.job_id,
                    "directory": str(workspace.directory),
                    "cleanup": None,
                },
            }
            yield captured
        finally:
            active_exception = sys.exc_info()[0] is not None
            retained = preserve_artifacts and captured is not None and not active_exception
            if retained:
                workspace.retain()
                cleanup_report = {
                    "removed_files": [],
                    "preserved_paths": [],
                    "removed_job_directory": False,
                    "retained": True,
                }
            else:
                report = workspace.close()
                cleanup_report = {
                    "removed_files": list(report.removed_files),
                    "preserved_paths": list(report.preserved_paths),
                    "removed_job_directory": report.removed_job_directory,
                    "retained": False,
                }
            if captured is not None:
                captured["artifact_job"]["cleanup"] = cleanup_report
            if cleanup_report["preserved_paths"] and not active_exception:
                raise ArtifactCleanupError(
                    "Temporary audio capture could not be fully cleaned",
                    details={"job_id": workspace.job_id,
                             "preserved_paths": cleanup_report["preserved_paths"]},
                )

    def analyze(
        self,
        artifact_root: str,
        *,
        section: Section | None = None,
        references: Mapping[str, AudioBuffer] | None = None,
        reference_sections: Mapping[str, Section] | None = None,
        include_fx_params: bool = True,
        include_pairwise: bool = False,
        phase_pairs: list[tuple[str, str]] | Literal["all"] | None = None,
        phase_max_lag_ms: float = 10.0,
        timeout_s: float = 600.0,
        parallel: bool = True,
        max_workers: int | None = None,
        include_partial_loudness: bool = False,
        capture_mode: str = "render",
        retain_audio: bool = False,
    ) -> dict:
        """Capture, analyze, and return all project audio with matching context.

        Every non-master track and the rendered master are analyzed in one
        bundle. Temporary WAVs are deleted by ``capture_audio`` before return;
        returned reports use stable source GUIDs rather than temporary paths.
        Project-tempo values are attached to each temporal-profile cell.

        REAPER's AudioAccessor returns track samples immediately pre-FX. It can
        be used through ``capture_audio`` for raw-source extraction, but cannot
        stand in for the processed tracks required for a mix analysis.
        """
        if not isinstance(include_pairwise, bool):
            raise ValidationError("include_pairwise must be a boolean")
        if parallel and not _main_module_can_spawn():
            # Running from stdin/REPL: spawn cannot re-import __main__.
            parallel = False
        if not isinstance(include_fx_params, bool):
            raise ValidationError("include_fx_params must be a boolean")
        if not isinstance(retain_audio, bool):
            raise ValidationError("retain_audio must be a boolean")
        if reference_sections is not None:
            if (not isinstance(reference_sections, Mapping) or
                    any(not isinstance(key, str) or not key.strip() or
                        not isinstance(value, Section)
                        for key, value in reference_sections.items())):
                raise ValidationError(
                    "reference_sections must map non-empty reference IDs to Section values"
                )
            if (not isinstance(references, Mapping) or
                    set(reference_sections) - set(references)):
                raise ValidationError(
                    "Every reference section must identify a supplied reference"
                )
        if not isinstance(capture_mode, str) or capture_mode not in {"render", "accessor"}:
            raise ValidationError("capture_mode must be 'render' or 'accessor'")
        if capture_mode == "accessor":
            raise ValidationError(
                "AudioAccessor samples are pre-FX and are not processed mix stems; "
                "use capture_audio(..., capture_mode='accessor') for raw-source capture"
            )

        result: dict[str, Any] | None = None
        captured: dict[str, Any] | None = None
        with self.capture_audio(
            artifact_root, include_fx_params=include_fx_params,
            timeout_s=timeout_s, section=section, capture_mode=capture_mode,
            preserve_artifacts=retain_audio,
        ) as captured:
            tracks = captured.get("tracks")
            master = captured.get("master")
            project = captured.get("project")
            if (not isinstance(tracks, list) or not isinstance(master, dict)
                    or not isinstance(project, dict)):
                raise BridgeUnavailable("Audio capture returned an incomplete project snapshot")
            revision = ProjectRevision.from_dict(project.get("revision", {}))
            if not revision.project_id or revision.change_count < 0:
                raise BridgeUnavailable("Audio capture returned an invalid project revision")

            source_paths: dict[str, str] = {}
            track_context: dict[str, dict[str, Any]] = {}
            capture_files: dict[str, dict[str, Any]] = {}
            for entry in [*tracks, master]:
                if not isinstance(entry, dict):
                    raise BridgeUnavailable("Audio capture returned an invalid track context")
                guid = entry.get("guid")
                capture_file = entry.get("capture")
                path = capture_file.get("path") if isinstance(capture_file, dict) else None
                if (not isinstance(guid, str) or not guid or guid in source_paths
                        or not isinstance(path, str) or not path):
                    raise BridgeUnavailable(
                        "Audio capture returned an invalid or duplicate source GUID",
                        details={"source_guid": guid},
                    )
                source_paths[guid] = path
                track_context[guid] = {
                    key: value for key, value in entry.items() if key != "capture"
                }
                capture_files[guid] = {
                    key: value for key, value in capture_file.items() if key != "path"
                }

            master_guid = master.get("guid")
            if not isinstance(master_guid, str) or master_guid not in source_paths:
                raise BridgeUnavailable("Rendered master has no matching source buffer")

            needs_buffers = ((not parallel) or phase_pairs is not None
                             or include_partial_loudness)
            source_audio: dict[str, AudioBuffer] = {}
            if needs_buffers:
                for guid, path in source_paths.items():
                    decoded = _load_rendered_audio(path, section)
                    source_audio[guid] = AudioBuffer(
                        samples=decoded.samples,
                        sample_rate=decoded.sample_rate,
                        source=guid,
                        start_s=decoded.start_s,
                        provenance={**decoded.provenance,
                            "capture_point": captured.get("capture_point"),
                            "project_id": revision.project_id,
                            "project_revision": revision.to_dict(),
                            "source_guid": guid,
                            "track_name": track_context[guid].get("name", ""),
                            "stage": "native_reaper_track_render",
                        },
                    )

            precomputed = None
            if parallel and len(source_paths) > 1:
                workers = max_workers or min(len(source_paths), os.cpu_count() or 1)
                workers = max(1, min(workers, len(source_paths)))
                with ProcessPoolExecutor(max_workers=workers) as pool:
                    precomputed = dict(zip(
                        source_paths,
                        pool.map(_measure_source_job,
                                 [(path, section) for path in source_paths.values()]),
                    ))
            sources_arg = source_audio if source_audio else {
                guid: None for guid in source_paths
            }
            master_key = master_guid if (tracks and include_partial_loudness) else None
            report = analyze_bundle(
                sources_arg,
                references=references,
                reference_sections=reference_sections,
                section=section,
                anchor_key=master_guid,
                mix_key=master_key,
                include_pairwise=include_pairwise,
                phase_pairs=phase_pairs,
                phase_max_lag_ms=phase_max_lag_ms,
                precomputed_reports=precomputed,
            )

            for source_id, source_report in report["sources"].items():
                source_report["track_context"] = track_context[source_id]
                source_report["capture"] = capture_files[source_id]

            temporal_times: set[float] = set()
            for source_report in report["sources"].values():
                profile = source_report.get("curves", {}).get("temporal_profile", {})
                temporal_times.update(float(value) for value in profile.get("times", []))
            ordered_times = sorted(temporal_times)
            tempo_points: dict[float, dict[str, Any]] = {}
            tempo_source: str | None = None
            for batch_start in range(0, len(ordered_times), 50_000):
                batch = ordered_times[batch_start:batch_start + 50_000]
                tempo_result = self.tempo_at(batch)
                tempo_revision = ProjectRevision.from_dict(
                    tempo_result.get("revision", {})
                )
                if (tempo_result.get("project_id") != revision.project_id
                        or tempo_revision != revision):
                    raise StaleContext(
                        "Project changed after capture and before tempo annotation",
                        details={"capture_revision": revision.to_dict(),
                                 "tempo_revision": tempo_revision.to_dict()},
                    )
                points = tempo_result.get("points")
                if not isinstance(points, list) or len(points) != len(batch):
                    raise BridgeUnavailable("REAPER returned incomplete tempo annotations")
                tempo_source = tempo_result.get("source")
                for expected_time, point in zip(batch, points):
                    if (not isinstance(point, dict)
                            or not isinstance(point.get("bpm"), (int, float))
                            or not isfinite(float(point["bpm"]))
                            or point["bpm"] <= 0
                            or not isinstance(point.get("numerator"), int)
                            or not isinstance(point.get("denominator"), int)
                            or point["numerator"] <= 0 or point["denominator"] <= 0
                            or abs(float(point.get("time_s", -1)) - expected_time) > 1e-9):
                        raise BridgeUnavailable("REAPER returned an invalid project tempo point")
                    tempo_points[round(expected_time, 9)] = {
                        "time_s": float(expected_time),
                        "bpm": float(point["bpm"]),
                        "numerator": point["numerator"],
                        "denominator": point["denominator"],
                        "effective_quarter_bpm": float(
                            point.get("effective_quarter_bpm",
                                      float(point["bpm"]) * 4 / point["denominator"])
                        ),
                    }

            for source_report in report["sources"].values():
                profile = source_report.get("curves", {}).get("temporal_profile", {})
                times = profile.get("times", [])
                cells = profile.get("cells", [])
                if len(times) != len(cells):
                    raise BridgeUnavailable("Analysis tape has mismatched time and cell counts")
                for time_s, cell in zip(times, cells):
                    point = tempo_points.get(round(float(time_s), 9))
                    if point is None:
                        raise BridgeUnavailable(
                            "Project tempo is missing for an audio-tape cell",
                            details={"time_s": float(time_s)},
                        )
                    cell["project_tempo"] = dict(point)

            report["project"] = {
                key: value for key, value in project.items()
                if key not in {"capture_revision", "revision"}
            } | {
                "capture_revision": project.get("capture_revision"),
                "revision": project.get("revision"),
            }
            report["project_tempo"] = {
                "source": tempo_source,
                "revision": revision.to_dict(),
                "points_attached_to_tape_cells": len(tempo_points),
                "coverage": "available" if ordered_times else
                    "not_applicable: no temporal-profile cells",
            }
            report["capture"] = {
                "capture_point": captured.get("capture_point"),
                "render_action": captured.get("render_action"),
                "source_count_including_master": len(source_paths),
                "capture_revision": project.get("capture_revision"),
                "time_window": captured.get("time_window"),
                "temporary_audio": (
                    "retained for a bounded resource lease" if retain_audio
                    else "removed after analysis"
                ),
                "cleanup": None,
            }
            result = report

        if result is None or captured is None:
            raise BridgeUnavailable("Project analysis ended without a result")
        cleanup = captured.get("artifact_job", {}).get("cleanup", {})
        result["capture"]["cleanup"] = {
            "removed_file_count": len(cleanup.get("removed_files", [])),
            "removed_job_directory": cleanup.get("removed_job_directory", False),
            "preserved_artifact_count": len(cleanup.get("preserved_paths", [])),
            "retained_for_resource_lease": cleanup.get("retained", False),
        }
        if retain_audio:
            result["capture"]["artifact_job_id"] = captured.get("artifact_job", {}).get(
                "job_id"
            )
        if retain_audio:
            retained_audio: dict[str, str] = {}
            for entry in [*captured.get("tracks", []), captured.get("master", {})]:
                guid = entry.get("guid") if isinstance(entry, dict) else None
                capture_info = entry.get("capture") if isinstance(entry, dict) else None
                path = capture_info.get("path") if isinstance(capture_info, dict) else None
                if isinstance(guid, str) and isinstance(path, str):
                    retained_audio[guid] = path
            if len(retained_audio) != len(result.get("sources", {})):
                raise BridgeUnavailable(
                    "Not every analyzed source has a retained audio artifact",
                    details={"retained_source_count": len(retained_audio),
                             "analyzed_source_count": len(result.get("sources", {}))},
                )
            result["audio_files"] = retained_audio
        return result

    def tempo_at(self, times_s: list[float]) -> dict:
        """Read the exact effective REAPER tempo at many project-time positions."""
        if not isinstance(times_s, list) or not times_s:
            raise ValidationError("times_s must be a non-empty list")
        if len(times_s) > 50_000:
            raise ValidationError("times_s is limited to 50000 positions")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
               not isfinite(float(value)) for value in times_s):
            raise ValidationError("Every project time must be a finite number")
        context = self.snapshot()
        revision = ProjectRevision.from_dict(context.get("revision", {}))
        tempo = context.get("tempo")
        if not isinstance(tempo, dict):
            raise BridgeUnavailable("REAPER returned an invalid project tempo context")
        markers = tempo.get("markers", [])
        if not isinstance(markers, list):
            raise BridgeUnavailable("REAPER returned an invalid tempo-marker list")

        # With no tempo/time-signature markers, REAPER's project tempo and
        # meter are constant. Use the project snapshot for every requested
        # position instead of repeatedly querying its time map near zero,
        # which can stall in REAPER 7.84 on this class of project.
        if not markers:
            bpm = tempo.get("bpm")
            numerator = tempo.get("numerator")
            denominator = tempo.get("denominator")
            if (not isinstance(bpm, (int, float)) or isinstance(bpm, bool) or
                    not isfinite(float(bpm)) or bpm <= 0 or
                    not isinstance(numerator, int) or isinstance(numerator, bool) or
                    numerator <= 0 or not isinstance(denominator, int) or
                    isinstance(denominator, bool) or denominator <= 0 or
                    not revision.project_id or revision.change_count < 0):
                raise BridgeUnavailable("REAPER returned an invalid constant tempo")
            points = [{"time_s": float(time_s), "bpm": float(bpm),
                       "numerator": numerator, "denominator": denominator,
                       "effective_quarter_bpm": float(bpm) * 4 / denominator}
                      for time_s in times_s]
            return {"project_id": revision.project_id, "revision": revision.to_dict(),
                    "points": points, "source": "project.base_tempo_no_markers"}

        result = self.bridge.call("project.tempo_at", {
            "project_id": revision.project_id,
            "times_s": [float(value) for value in times_s],
        })
        points = result.get("points") if isinstance(result, dict) else None
        if (not isinstance(points, list) or len(points) != len(times_s) or
                result.get("project_id") != revision.project_id or
                ProjectRevision.from_dict(result.get("revision", {})) != revision or
                not isinstance(result.get("source"), str) or
                not result["source"].strip()):
            raise BridgeUnavailable("REAPER returned an incomplete tempo-map response")
        for expected, point in zip(times_s, points):
            if not isinstance(point, dict):
                raise BridgeUnavailable("REAPER returned an invalid tempo-map point")
            time_s = point.get("time_s")
            bpm = point.get("bpm")
            numerator = point.get("numerator")
            denominator = point.get("denominator")
            quarter_bpm = point.get("effective_quarter_bpm")
            if (isinstance(time_s, bool) or not isinstance(time_s, (int, float)) or
                    not isfinite(float(time_s)) or
                    abs(float(time_s) - float(expected)) > 1e-9 or
                    isinstance(bpm, bool) or not isinstance(bpm, (int, float)) or
                    not isfinite(float(bpm)) or bpm <= 0 or
                    isinstance(numerator, bool) or not isinstance(numerator, int) or
                    numerator <= 0 or isinstance(denominator, bool) or
                    not isinstance(denominator, int) or denominator <= 0 or
                    isinstance(quarter_bpm, bool) or
                    not isinstance(quarter_bpm, (int, float)) or
                    not isfinite(float(quarter_bpm)) or quarter_bpm <= 0):
                raise BridgeUnavailable("REAPER returned an invalid tempo-map point")
        return result

    def save_current(self) -> dict:
        return self.bridge.save_project("current", project_id=self.project_id)

    def save_as(self, path: str, *, overwrite_existing: bool = False) -> dict:
        result = self.bridge.save_project("save_as", project_id=self.project_id, path=path,
                                          overwrite_existing=overwrite_existing)
        new_project_id = result.get("project_id_after")
        if isinstance(new_project_id, str) and new_project_id:
            self.project_id = new_project_id
        return result

    def plan(
        self, operations: list[EditOperation], *, reason: str,
        expected_revision: ProjectRevision | dict[str, Any] | None = None,
    ) -> EditPlan:
        if not isinstance(operations, list) or not operations or len(operations) > 256:
            raise ValidationError("An edit plan must contain 1 to 256 operations")
        if any(not isinstance(item, EditOperation) or not item.kind or
               not item.target_guid or not isinstance(item.values, dict) or
               not item.values for item in operations):
            raise ValidationError("Every edit operation needs a kind, target GUID, and values")
        if (not isinstance(reason, str) or not reason.strip() or len(reason) > 300):
            raise ValidationError("reason must contain 1 to 300 characters")
        context = self.snapshot()
        revision = ProjectRevision.from_dict(context["revision"])
        if not revision.project_id or revision.change_count < 0:
            raise BridgeUnavailable("REAPER returned an invalid project revision")
        if expected_revision is not None:
            expected = (expected_revision if isinstance(expected_revision, ProjectRevision)
                        else ProjectRevision.from_dict(expected_revision))
            if expected != revision:
                raise StaleContext("Project changed before the edit plan was previewed",
                                   details={"expected": expected.to_dict(),
                                            "current": revision.to_dict()})
        preview = self.bridge.call("transaction.preview", {
            "revision": revision.to_dict(),
            "operations": [item.to_dict() for item in operations],
        })
        if (not isinstance(preview, dict) or
                not isinstance(preview.get("plan_id"), str) or
                not preview["plan_id"] or
                not isinstance(preview.get("revision"), dict) or
                ProjectRevision.from_dict(preview["revision"]) != revision or
                not isinstance(preview.get("before"), list)):
            raise BridgeUnavailable("REAPER returned an invalid edit-plan preview")
        return EditPlan(revision=revision, operations=tuple(operations), reason=reason,
                        plan_id=preview["plan_id"],
                        before=preview.get("before", {}))

    def create_midi_item(self, track_guid: str, *, position_s: float, length_s: float,
                         notes: list[dict[str, Any]], reason: str,
                         dry_run: bool = True) -> dict[str, Any]:
        """Plan or create a MIDI item with arbitrary note events on one track.

        Note start/end times are seconds relative to the new item's start. This
        operation creates only the requested events; it does not impose a chord,
        drum, tempo, or genre recipe. A dry run is returned by default.
        """
        if not isinstance(track_guid, str) or not track_guid:
            raise ValidationError("track_guid must be a non-empty REAPER track GUID")
        if isinstance(position_s, bool) or not isinstance(position_s, (int, float)) or \
                not isfinite(float(position_s)):
            raise ValidationError("position_s must be a finite number")
        if isinstance(length_s, bool) or not isinstance(length_s, (int, float)) or \
                not isfinite(float(length_s)) or length_s <= 0:
            raise ValidationError("length_s must be a finite positive number")
        if not isinstance(notes, list) or len(notes) > 10000 or \
                any(not isinstance(note, dict) for note in notes):
            raise ValidationError("notes must be a list of at most 10000 note objects")
        if not isinstance(dry_run, bool):
            raise ValidationError("dry_run must be a boolean")
        operation = EditOperation(
            kind="midi.create", target_guid=track_guid,
            values={"position_s": float(position_s), "length_s": float(length_s),
                    "notes": notes},
        )
        plan = self.plan([operation], reason=reason)
        applied = self.apply(plan, dry_run=dry_run)
        return {"plan": plan, "result": applied}

    def apply(self, plan: EditPlan, *, dry_run: bool = True) -> dict:
        if not isinstance(plan, EditPlan) or not plan.plan_id:
            raise ValidationError("apply requires a valid previewed EditPlan")
        if not isinstance(dry_run, bool):
            raise ValidationError("dry_run must be a boolean")
        current = self.snapshot()
        revision = ProjectRevision.from_dict(current["revision"])
        if revision != plan.revision:
            raise StaleContext("Project changed after the plan was prepared",
                               details={"planned": plan.revision.to_dict(),
                                        "current": revision.to_dict()})
        result = self.bridge.call("transaction.apply", {
            "plan": plan.to_dict(),
            "dry_run": dry_run,
        })
        if (not isinstance(result, dict) or result.get("plan_id") != plan.plan_id or
                result.get("dry_run") is not dry_run or
                result.get("applied") is not (not dry_run or result.get("no_op") is True)):
            raise BridgeUnavailable("REAPER returned an invalid transaction result")
        return result

    def undo(self, *, expected_revision: ProjectRevision) -> dict:
        if not isinstance(expected_revision, ProjectRevision):
            raise ValidationError("undo requires the revision returned by the applied transaction")
        current = self.snapshot()
        revision = ProjectRevision.from_dict(current["revision"])
        if revision != expected_revision:
            raise StaleContext("Project changed after the target transaction",
                               details={"expected": expected_revision.to_dict(),
                                        "current": revision.to_dict()})
        result = self.bridge.call("transaction.undo",
                                  {"expected_revision": expected_revision.to_dict()})
        if (not isinstance(result, dict) or result.get("undone") is not True or
                not isinstance(result.get("revision_before"), dict) or
                ProjectRevision.from_dict(result["revision_before"]) != expected_revision):
            raise BridgeUnavailable("REAPER returned an invalid transaction undo result")
        return result
