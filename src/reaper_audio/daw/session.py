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
from pathlib import Path
from typing import Any, Iterator, Literal

import soundfile as sf

from ..analysis.artifacts import AudioArtifactWorkspace, RegisteredArtifact
from ..analysis.audio import load_audio
from ..analysis.bundle import analyze_bundle
from ..analysis.metrics import measure_audio
from ..context.audio import (AudioAsset, Capture, CaptureMethod, ChannelView,
                             SignalWindow, SourceKind, Stage)
from ..context.errors import ContractValidationError
from ..context.ids import new_id
from ..context.revision import from_project_revision, revision_from_digest
from ..errors import (ArtifactCleanupError, BridgeUnavailable,
                      CaptureMasterInvalid, MediaUnresolved, StaleContext,
                      ValidationError)
from ..models import AudioBuffer, Section
from ..reaper.bridge_client import ReaperBridge
from ..timebase import MappingStatus
from .analysis_policy import (resolve_analysis_workers,
                              validate_analysis_worker_request)
from .signal_gate import (classify_master_silence, measure_wave,
                          source_activity)


def _main_module_can_spawn() -> bool:
    """multiprocessing spawn re-imports __main__; stdin/notebooks have no file."""
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None)
    return bool(path) and not str(path).startswith("<") and os.path.exists(path)


def _unresolved_source_rows(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return unmuted audio take sources REAPER reports as unresolved.

    The bridge adds ``source_online`` only when it can measure the source.  A
    missing field is a legacy bridge contract and is left to the bridge's own
    preflight; an explicit false or zero length/rate/channel count is a
    fail-closed media state.  Empty tracks and muted items are intentionally
    silent and are not failures.
    """
    rows: list[dict[str, Any]] = []
    for track in tracks:
        if not isinstance(track, Mapping) or bool(track.get("mute")):
            continue
        for item in track.get("items") or []:
            if not isinstance(item, Mapping) or bool(item.get("mute")):
                continue
            take = item.get("active_take")
            if isinstance(take, Mapping) and take.get("is_midi"):
                continue
            if "source_online" not in item:
                continue
            values = (item.get("source_online"),
                      item.get("source_length_s"),
                      item.get("source_sample_rate"),
                      item.get("source_channels"))
            if not all(value not in (None, 0, 0.0, False) for value in values):
                rows.append({
                    "track_guid": track.get("guid"),
                    "track_index": track.get("index"),
                    "track_name": track.get("name"),
                    "item_guid": item.get("guid"),
                    "source": (take or {}).get("source") if isinstance(take, Mapping) else None,
                    "source_online": item.get("source_online"),
                    "source_length_s": item.get("source_length_s"),
                    "source_sample_rate": item.get("source_sample_rate"),
                    "source_channels": item.get("source_channels"),
                })
    return rows


def _active_audio_item_count(tracks: list[dict[str, Any]], section: Section | None,
                             tail_s: float) -> int:
    """Count unmuted audio items overlapping the captured project window."""
    if section is None:
        window_start, window_end = None, None
    else:
        window_start = float(section.start_s)
        window_end = float(section.end_s) + max(0.0, float(tail_s))
    count = 0
    for track in tracks:
        if not isinstance(track, Mapping) or bool(track.get("mute")):
            continue
        for item in track.get("items") or []:
            if not isinstance(item, Mapping) or bool(item.get("mute")):
                continue
            take = item.get("active_take")
            if isinstance(take, Mapping) and take.get("is_midi"):
                continue
            if window_start is not None and window_end is not None:
                position = item.get("position_s")
                length = item.get("length_s")
                if (isinstance(position, (int, float)) and
                        not isinstance(position, bool) and
                        isinstance(length, (int, float)) and
                        not isinstance(length, bool)):
                    item_end = float(position) + float(length)
                    if item_end <= window_start or float(position) >= window_end:
                        continue
            count += 1
    return count


@dataclass(frozen=True, slots=True, eq=False)
class ProjectRevision:
    """One observed project revision, including the continuity identity.

    ``instance_id`` is the running REAPER bridge instance; ``epoch`` is the
    bridge's per-project generation, bumped when the bridge observes a
    discontinuity (new project object handle, project identity change or a
    change counter that moved backwards, i.e. a reload).  A ``None``/zero
    component is unknown and can never prove continuity.
    """

    project_id: str
    change_count: int
    path: str
    instance_id: str | None = None
    epoch: int | None = None
    epoch_basis: str | None = None

    @classmethod
    def from_dict(cls, value: dict) -> "ProjectRevision":
        raw_epoch = value.get("epoch")
        epoch = (raw_epoch if isinstance(raw_epoch, int) and not isinstance(raw_epoch, bool)
                 and raw_epoch >= 1 else None)
        instance_id = value.get("instance_id")
        basis = value.get("epoch_basis")
        return cls(project_id=str(value.get("project_id", "")),
                   change_count=int(value.get("change_count", -1)),
                   path=str(value.get("path", "")),
                   instance_id=(instance_id if isinstance(instance_id, str) and instance_id
                                else None),
                   epoch=epoch,
                   epoch_basis=(basis if isinstance(basis, str) and basis else None))

    def to_dict(self) -> dict:
        value = {"project_id": self.project_id, "change_count": self.change_count,
                 "path": self.path}
        # Identity components travel only when they are known.  A legacy or
        # coarse revision keeps its exact old wire shape, so no caller can read
        # a null as "generation zero" continuity.
        if self.instance_id is not None:
            value["instance_id"] = self.instance_id
        if self.epoch is not None:
            value["epoch"] = self.epoch
        if self.epoch_basis is not None:
            value["epoch_basis"] = self.epoch_basis
        return value

    @property
    def identity_complete(self) -> bool:
        return bool(self.project_id and self.instance_id and self.epoch)

    def _continuity_key(self) -> tuple:
        # epoch_basis is provenance, not identity: two observations of the same
        # project generation match even if one omitted the explanatory label.
        return (self.project_id, self.change_count, self.path,
                self.instance_id, self.epoch)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ProjectRevision):
            return NotImplemented
        return self._continuity_key() == other._continuity_key()

    def __hash__(self) -> int:
        return hash(self._continuity_key())

    def identity_gaps(self) -> tuple[str, ...]:
        gaps: list[str] = []
        if not self.project_id:
            gaps.append("project_id")
        if not self.instance_id:
            gaps.append("instance_id")
        if not self.epoch:
            gaps.append("epoch")
        return tuple(gaps)


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


def _load_rendered_audio(path: str, section: Section | None,
                         tail_s: float = 0.0,
                         *, project_end_s: float | None = None) -> AudioBuffer:
    """Map a render file's local zero to its project-time selection.

    ``tail_s`` is the extra audio REAPER wrote past the requested section so
    that reverb decays are not cut off.  It widens only the *file* window: the
    buffer still starts at ``section.start_s`` on the project clock, so
    ``measure_audio(..., section=section)`` trims the tail away and every
    measurement describes the project section alone.  The tail is therefore
    available to later analysis but is never presented as measured.

    Without a section, ``project_end_s`` is the verified end of the musical
    project inside the file.  It is required whenever a tail was rendered: the
    returned buffer is then trimmed to that boundary while its provenance keeps
    the longer file window, so a full-project capture cannot silently measure
    the tail.
    """
    if (isinstance(tail_s, bool) or not isinstance(tail_s, (int, float)) or
            not isfinite(float(tail_s)) or tail_s < 0):
        raise ValidationError("tail_s must be a finite non-negative number")
    tail_s = float(tail_s)
    decoded = load_audio(path)
    if section is None:
        file_end_s = decoded.duration_s
        if project_end_s is None:
            if tail_s > 0:
                raise BridgeUnavailable(
                    "Rendered audio has a tail but no verified project boundary",
                    details={"boundary": "unknown", "path": str(path),
                             "file_end_s": file_end_s, "tail_s": tail_s},
                )
            # No section and no tail: the file itself is the project window.
            # ``tail_s`` is still recorded so a reader can tell that the file
            # may extend past the musical content the caller cared about.
            return AudioBuffer(
                samples=decoded.samples, sample_rate=decoded.sample_rate,
                source=decoded.source, start_s=0.0,
                provenance={**decoded.provenance,
                            "timeline": "project_seconds",
                            "render_window": {"start_s": 0.0,
                                              "end_s": decoded.duration_s},
                            "project_window": {"start_s": 0.0,
                                               "end_s": decoded.duration_s},
                            "file_window": {"start_s": 0.0,
                                            "end_s": decoded.duration_s},
                            "tail_s": tail_s},
            )
        boundary = _finite_or_none(project_end_s)
        if boundary is None or boundary <= 0:
            raise BridgeUnavailable(
                "Rendered audio has an invalid project boundary",
                details={"boundary": "unknown", "path": str(path),
                         "project_end_s": project_end_s, "tail_s": tail_s},
            )
        sample_tolerance_s = 1.0 / decoded.sample_rate
        if boundary > file_end_s + 0.5 * sample_tolerance_s:
            raise BridgeUnavailable(
                "Rendered project boundary lies past the captured file",
                details={"boundary": "unknown", "path": str(path),
                         "project_end_s": boundary, "file_end_s": file_end_s},
            )
        expected_file_end_s = boundary + tail_s
        if abs(file_end_s - expected_file_end_s) > sample_tolerance_s:
            raise BridgeUnavailable(
                "Rendered file duration does not match the project boundary plus tail",
                details={"boundary": "unknown", "path": str(path),
                         "project_end_s": boundary, "tail_s": tail_s,
                         "expected_file_end_s": expected_file_end_s,
                         "actual_file_end_s": file_end_s,
                         "sample_rate": decoded.sample_rate},
            )
        frames = min(decoded.frames,
                     int(round(boundary * decoded.sample_rate)))
        if frames <= 0:
            raise BridgeUnavailable(
                "Rendered project boundary contains no audio samples",
                details={"boundary": "unknown", "path": str(path),
                         "project_end_s": boundary},
            )
        measured_end_s = frames / decoded.sample_rate
        return AudioBuffer(
            samples=decoded.samples[:frames], sample_rate=decoded.sample_rate,
            source=decoded.source, start_s=0.0,
            provenance={**decoded.provenance,
                        "timeline": "project_seconds",
                        "render_window": {"start_s": 0.0, "end_s": measured_end_s},
                        "project_window": {"start_s": 0.0, "end_s": measured_end_s},
                        "file_window": {"start_s": 0.0, "end_s": file_end_s},
                        "tail_s": tail_s},
        )

    tolerance_s = 1.0 / decoded.sample_rate
    expected_duration_s = section.duration_s + tail_s
    if abs(decoded.duration_s - expected_duration_s) > tolerance_s:
        raise BridgeUnavailable(
            "Rendered audio duration does not match the requested project section",
            details={"path": path,
                     "requested_project_section": {
                         "start_s": section.start_s, "end_s": section.end_s},
                     "requested_tail_s": tail_s,
                     "expected_duration_s": expected_duration_s,
                     "actual_duration_s": decoded.duration_s,
                     "sample_rate": decoded.sample_rate},
        )
    return AudioBuffer(
        samples=decoded.samples, sample_rate=decoded.sample_rate,
        source=decoded.source, start_s=section.start_s,
        provenance={**decoded.provenance,
                    "timeline": "project_seconds",
                    # The render window is the project selection; the file
                    # window is everything REAPER wrote, tail included.
                    "render_window": {"start_s": section.start_s,
                                      "end_s": section.end_s},
                    "project_window": {"start_s": section.start_s,
                                       "end_s": section.end_s},
                    "file_window": {"start_s": 0.0,
                                    "end_s": decoded.duration_s},
                    "tail_s": tail_s},
    )


def _measure_source_job(payload: tuple[str, Section | None, float, float | None]):
    """Worker: measure one source, trimming a full-project render at its end."""
    path, section, tail_s, project_end_s = payload
    return measure_audio(
        _load_rendered_audio(path, section, tail_s,
                             project_end_s=project_end_s),
        section=section,
    )


def _finite_or_none(value: object) -> float | None:
    """Return a finite float for a numeric window bound, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) else None


@dataclass(frozen=True, slots=True)
class RenderedProjectBoundary:
    """Verified end of the musical project inside one rendered capture."""

    project_end_s: float
    source: str
    file_frames: int
    sample_rate: int
    tail_s: float
    tail_frames: int
    project_frames: int
    project_length_s: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_end_s": self.project_end_s,
            "project_end_source": self.source,
            "file_frames": self.file_frames,
            "sample_rate": self.sample_rate,
            "tail_s": self.tail_s,
            "tail_frames": self.tail_frames,
            "project_frames": self.project_frames,
            "project_length_s": self.project_length_s,
        }


def _verified_render_tail(rendered: Mapping[str, Any], *, tail_ms: int) -> float:
    """Return the tail REAPER says it applied, or fail with an unknown boundary.

    The request alone is not evidence: a bridge that ignored ``tail_ms`` would
    otherwise let us subtract an imaginary tail from a real file.  Both the
    top-level ``tail_s`` echo and the read-back ``RENDER_TAILMS`` setting are
    checked against the request, and a missing echo is only tolerated when no
    tail was requested.
    """
    requested_tail_s = tail_ms / 1000.0
    reported: list[tuple[str, float]] = []
    raw_tail = rendered.get("tail_s")
    if raw_tail is not None:
        value = _finite_or_none(raw_tail)
        if value is None or value < 0:
            raise BridgeUnavailable(
                "REAPER reported an invalid render tail",
                details={"field": "tail_s", "value": raw_tail,
                         "tail_ms": tail_ms},
            )
        reported.append(("tail_s", value))
    settings = rendered.get("render_settings_used")
    if isinstance(settings, Mapping) and "RENDER_TAILMS" in settings:
        raw_ms = _finite_or_none(settings.get("RENDER_TAILMS"))
        if raw_ms is None or raw_ms < 0:
            raise BridgeUnavailable(
                "REAPER reported an invalid render tail setting",
                details={"field": "render_settings_used.RENDER_TAILMS",
                         "value": settings.get("RENDER_TAILMS")},
            )
        reported.append(("render_settings_used.RENDER_TAILMS", raw_ms / 1000.0))
    if not reported:
        if tail_ms == 0:
            return 0.0
        raise BridgeUnavailable(
            "REAPER did not report the render tail, so the measured project "
            "boundary is unknown",
            details={"boundary": "unknown", "tail_ms": tail_ms,
                     "reason": "missing_tail_echo"},
        )
    for field, value in reported:
        if abs(value - requested_tail_s) > 1e-9:
            raise BridgeUnavailable(
                "REAPER rendered a different tail than requested, so the "
                "measured project boundary is unknown",
                details={"boundary": "unknown", "field": field,
                         "requested_tail_s": requested_tail_s,
                         "reported_tail_s": value},
            )
    return requested_tail_s


def _resolve_rendered_project_end(*, length_s: object, frames: object,
                                  sample_rate: object,
                                  tail_s: object) -> RenderedProjectBoundary:
    """Derive the project end from the render tail, sample count and length.

    A full-project render writes ``project_length + tail``.  The boundary is
    therefore the file minus the *verified* tail, rounded to whole samples, and
    it must agree with REAPER's own project length within one sample frame.
    When any of those values is missing, malformed or inconsistent, the
    boundary is unknown and the measurement fails closed.
    """
    if isinstance(frames, bool) or not isinstance(frames, int) or frames <= 0:
        raise BridgeUnavailable(
            "Rendered audio has no usable sample count, so the project "
            "boundary is unknown",
            details={"boundary": "unknown", "frames": frames},
        )
    if (isinstance(sample_rate, bool) or not isinstance(sample_rate, int)
            or sample_rate <= 0):
        raise BridgeUnavailable(
            "Rendered audio has no usable sample rate, so the project "
            "boundary is unknown",
            details={"boundary": "unknown", "sample_rate": sample_rate},
        )
    tail = _finite_or_none(tail_s)
    if tail is None or tail < 0:
        raise BridgeUnavailable(
            "Rendered audio has an invalid tail length, so the project "
            "boundary is unknown",
            details={"boundary": "unknown", "tail_s": tail_s},
        )
    length = None
    if length_s is not None:
        length = _finite_or_none(length_s)
        if length is None or length <= 0:
            raise BridgeUnavailable(
                "REAPER reported an invalid project length, so the project "
                "boundary is unknown",
                details={"boundary": "unknown", "project_length_s": length_s},
            )

    tolerance_s = 1.0 / sample_rate
    if tail > 0:
        tail_frames = int(round(tail * sample_rate))
        project_frames = frames - tail_frames
        if project_frames <= 0:
            raise BridgeUnavailable(
                "The rendered audio is not longer than its requested tail, so "
                "the project boundary is unknown",
                details={"boundary": "unknown", "frames": frames,
                         "tail_s": tail, "tail_frames": tail_frames,
                         "sample_rate": sample_rate},
            )
        project_end_s = project_frames / sample_rate
        if length is None:
            raise BridgeUnavailable(
                "REAPER did not report a project length, so the rendered tail "
                "cannot be separated from project audio",
                details={"boundary": "unknown", "frames": frames,
                         "tail_s": tail, "sample_rate": sample_rate},
            )
        if abs(project_end_s - length) > tolerance_s:
            raise BridgeUnavailable(
                "The projected render boundary disagrees with REAPER's project "
                "length, so the project boundary is unknown",
                details={"boundary": "unknown", "frames": frames,
                         "tail_s": tail, "sample_rate": sample_rate,
                         "project_length_s": length,
                         "implied_project_end_s": project_end_s,
                         "tolerance_s": tolerance_s},
            )
        return RenderedProjectBoundary(
            project_end_s=project_end_s, source="project_length_and_render_tail",
            file_frames=frames, sample_rate=sample_rate, tail_s=tail,
            tail_frames=tail_frames, project_frames=project_frames,
            project_length_s=length,
        )

    project_end_s = frames / sample_rate
    if length is not None and abs(project_end_s - length) > tolerance_s:
        raise BridgeUnavailable(
            "The rendered file boundary disagrees with REAPER's project length, "
            "so the project boundary is unknown",
            details={"boundary": "unknown", "frames": frames,
                     "sample_rate": sample_rate, "project_length_s": length,
                     "implied_project_end_s": project_end_s,
                     "tolerance_s": tolerance_s},
        )
    return RenderedProjectBoundary(
        project_end_s=project_end_s,
        source=("project_length_and_rendered_file" if length is not None
                else "rendered_file_duration"),
        file_frames=frames, sample_rate=sample_rate, tail_s=0.0,
        tail_frames=0, project_frames=frames, project_length_s=length,
    )


def _analysis_window_duration_s(section: Section | None,
                                time_window: object) -> float | None:
    """Measured project window used by the shared worker policy."""
    if section is not None:
        return section.duration_s
    if isinstance(time_window, Mapping):
        start = _finite_or_none(time_window.get("project_start_s"))
        end = _finite_or_none(time_window.get("project_end_s"))
        if start is not None and end is not None and end > start:
            return end - start
    return None


def _bundle_analysis_workers(asset_count: int, *, section: Section | None,
                             time_window: object,
                             requested: int | None = None,
                             available_memory_bytes: int | None = None) -> int:
    """The bundle path's single call into the shared analysis policy."""
    return resolve_analysis_workers(
        asset_count=asset_count,
        window_s=_analysis_window_duration_s(section, time_window),
        requested=requested,
        available_memory_bytes=available_memory_bytes,
    ).workers


def _source_analysis_window(time_window: object, *, section: Section | None,
                            tail_s: float) -> dict[str, Any]:
    """Separate what a capture *wrote* from what the analysis *measured*.

    ``section`` is the project selection the caller asked to measure; the
    captured file may extend ``tail_s`` past it.  Reporting one window for both
    would let a reverb tail pass as measured material, so the three spans are
    always emitted separately: project, file and measured.  Without an explicit
    section the measured span is the verified full-project boundary, which can
    be shorter than the file.
    """
    file_start = file_end = project_start = project_end = None
    if isinstance(time_window, Mapping):
        file_start = _finite_or_none(time_window.get("file_start_s"))
        file_end = _finite_or_none(time_window.get("file_end_s"))
        project_start = _finite_or_none(time_window.get("project_start_s"))
        project_end = _finite_or_none(time_window.get("project_end_s"))
    if section is not None:
        project_start, project_end = section.start_s, section.end_s
        file_start = 0.0 if file_start is None else file_start
        file_end = (section.duration_s + tail_s) if file_end is None else file_end
    elif project_start is not None and project_end is not None:
        file_start = 0.0 if file_start is None else file_start
        file_end = (project_end + tail_s) if file_end is None else file_end
    measured_start = measured_end = None
    if project_start is not None and project_end is not None:
        measured_start, measured_end = project_start, project_end
    return {
        "project_start_s": project_start,
        "project_end_s": project_end,
        "file_start_s": file_start,
        "file_end_s": file_end,
        "measured_start_s": measured_start,
        "measured_end_s": measured_end,
        "tail_s": tail_s,
        # The project window is always the measured span.  A valid capture
        # resolves the full-project boundary before analysis, so the tail is
        # never counted as measured material.
        "tail_included_in_measurement": False,
        "coverage": ("project_section" if section is not None
                     else "full_file" if (project_start is not None and
                                          file_end is not None and
                                          project_end is not None and
                                          abs(file_end - project_end) <= 1e-9)
                     else "project_window" if project_start is not None
                     else "unknown"),
    }


def _rejects_render_contract(exc: TypeError, contract: Mapping[str, Any]) -> bool:
    """True only when ``exc`` says this bridge refuses one of the new keywords."""
    text = str(exc)
    if "unexpected keyword argument" not in text:
        return False
    return any(f"'{name}'" in text for name in contract)


def _capture_render(
    bridge: Any, *, render_contract: Mapping[str, Any], **request: Any
) -> tuple[dict[str, Any], bool]:
    """Render through the bridge and report whether it accepted the render contract.

    ``channels``, ``sample_rate`` and ``tail_ms`` are part of the current bridge
    contract.  A legacy fake bridge that cannot accept them is retried without
    them so the old session tests still exercise the same code path; the second
    element is then ``False`` and the capture is marked legacy instead of being
    presented as a verified render.  Any other ``TypeError`` is a real failure
    and is never downgraded.
    """
    try:
        result = bridge.capture_render(**request, **render_contract)
    except TypeError as exc:
        if not _rejects_render_contract(exc, render_contract):
            raise
        result = bridge.capture_render(**request)
        accepted = False
    else:
        accepted = True
    if not isinstance(result, Mapping):
        raise BridgeUnavailable(
            "REAPER returned an invalid analysis render response",
            details={"result_type": type(result).__name__},
        )
    return dict(result), accepted


def _capture_restore_report(rendered: Mapping[str, Any]) -> dict[str, Any]:
    """Return REAPER's factual restore report, or refuse the capture.

    A capture temporarily rewrites render settings and restores them afterwards.
    A default ``{"restored": True}`` would turn an unreported project state into
    silent corruption, so a missing, malformed or failing report is a hard
    failure instead of a synthesized success.
    """
    report = rendered.get("restore_report")
    if not isinstance(report, Mapping):
        raise BridgeUnavailable(
            "REAPER did not report whether the capture render was restored",
            details={"field": "restore_report",
                     "render_action": rendered.get("render_action"),
                     "result_type": type(report).__name__},
        )
    report = dict(report)
    if report.get("restored") is not True:
        raise BridgeUnavailable(
            "REAPER did not confirm that the capture render was restored",
            details={"field": "restore_report", "restore_report": report},
        )
    failures = report.get("failures")
    if not isinstance(failures, (list, tuple)):
        raise BridgeUnavailable(
            "REAPER returned an invalid capture restore report",
            details={"field": "failures", "restore_report": report},
        )
    if failures:
        raise BridgeUnavailable(
            "REAPER could not restore every capture render setting",
            details={"field": "failures", "restore_report": report,
                     "failures": list(failures)},
        )
    return report


def _bridge_project_digest(bridge: Any, project_id: str) -> dict[str, Any] | None:
    """Read one structural project digest, or ``None`` when the bridge has none.

    A bridge that exposes ``project_digest`` but fails is never treated as a
    legacy bridge: an unreadable digest is a capture failure, and silently
    falling back would let an unmeasured revision pass for a measured one.
    """
    reader = getattr(bridge, "project_digest", None)
    if not callable(reader):
        return None
    try:
        digest = reader(project_id)
    except BridgeUnavailable:
        raise
    except Exception as exc:
        raise BridgeUnavailable(
            "REAPER could not read a project digest for the audio capture",
            details={"project_id": project_id, "operation": "project.digest",
                     "reason": str(exc), "error": type(exc).__name__},
        ) from exc
    if not isinstance(digest, Mapping):
        raise BridgeUnavailable(
            "REAPER returned an invalid project digest",
            details={"project_id": project_id,
                     "result_type": type(digest).__name__},
        )
    digest = dict(digest)
    if digest.get("project_id") != project_id:
        raise BridgeUnavailable(
            "REAPER returned a project digest for a different project",
            details={"project_id": project_id,
                     "digest_project_id": digest.get("project_id")},
        )
    return digest


def _capture_revision(project_id: str, path: str, change_count: int,
                      digest: Mapping[str, Any] | None,
                      instance_id: str | None = None,
                      epoch: int | None = None):
    """Bind the pre-capture revision to the measured digest, or to a placeholder.

    The identity components travel with the digest: a capture that dropped them
    could never be compared for continuity, only for fingerprint equality.
    """
    if digest is None:
        return from_project_revision(project_id, path, change_count,
                                     instance_id=instance_id,
                                     epoch=epoch or 0)
    try:
        return revision_from_digest(project_id, change_count, digest,
                                    instance_id=instance_id,
                                    epoch=epoch or 0)
    except ContractValidationError as exc:
        raise BridgeUnavailable(
            "REAPER returned a project digest that cannot bind a capture revision",
            details={"project_id": project_id,
                     "digest_version": digest.get("digest_version"),
                     "reason": str(exc)},
        ) from exc


def _digest_object_guids(digest: Mapping[str, Any] | None,
                         group: str) -> tuple[str, ...] | None:
    """Stable GUID list of one digest field group, or ``None`` when unmeasured."""
    if not isinstance(digest, Mapping):
        return None
    fields = digest.get("fields")
    if not isinstance(fields, Mapping):
        return None
    values = fields.get(group)
    if not isinstance(values, list):
        return None
    guids: list[str] = []
    for entry in values:
        if not isinstance(entry, Mapping):
            return None
        guid = entry.get("guid")
        if not isinstance(guid, str) or not guid:
            return None
        guids.append(guid)
    return tuple(guids)


def _structure_evidence(context: Mapping[str, Any],
                        digest: Mapping[str, Any] | None) -> dict[str, Any]:
    """Object identities and counts visible in one snapshot plus its digest."""
    tracks = context.get("tracks")
    track_count = context.get("track_count")
    if isinstance(track_count, bool) or not isinstance(track_count, int):
        track_count = len(tracks) if isinstance(tracks, list) else None
    track_guids: tuple[str, ...] | None = None
    item_count: int | None = None
    item_guids = _digest_object_guids(digest, "items")
    if isinstance(tracks, list):
        raw_guids = [track.get("guid") if isinstance(track, Mapping) else None
                     for track in tracks]
        if raw_guids and all(isinstance(guid, str) and guid for guid in raw_guids):
            track_guids = tuple(str(guid) for guid in raw_guids)
        observed: list[list[Any]] = []
        for track in tracks:
            if not isinstance(track, Mapping):
                continue
            items = track.get("items")
            if isinstance(items, list):
                observed.append(items)
        if observed:
            item_count = sum(len(items) for items in observed)
    if item_guids is not None:
        item_count = len(item_guids)
    return {"track_count": track_count, "track_guids": track_guids,
            "item_count": item_count, "item_guids": item_guids}


def _require_unchanged_structure(before: Mapping[str, Any],
                                 after: Mapping[str, Any]) -> None:
    """Refuse a capture that added, removed or replaced tracks or media items.

    A capture only ever renders and restores; new project objects mean the
    restore failed or the project changed under the render.  Either way the
    returned audio no longer describes the snapshot it is bound to.
    """
    changed = [key for key in ("track_count", "track_guids", "item_count",
                               "item_guids")
               if before.get(key) is not None and after.get(key) is not None
               and before.get(key) != after.get(key)]
    if not changed:
        return
    raise BridgeUnavailable(
        "REAPER added or removed project objects during audio capture",
        details={"changed": changed,
                 "structure": {key: {"before": before.get(key),
                                     "after": after.get(key)}
                               for key in changed}},
    )


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

    def digest(self, project_id: str | None = None) -> dict[str, Any]:
        """Read the canonical project-state digest the render keys are built from."""
        selected_id = project_id if project_id is not None else self.project_id
        digest = self.bridge.project_digest(selected_id)
        if not isinstance(digest, Mapping):
            raise BridgeUnavailable("REAPER returned an invalid project digest")
        return dict(digest)

    @contextmanager
    def capture_audio(
        self, artifact_root: str, *, include_fx_params: bool = True,
        timeout_s: float = 600.0,
        section: Section | None = None,
        capture_mode: str = "render",
        preserve_artifacts: bool = False,
        tail_ms: int = 0,
        channels: str = "stereo",
        sample_rate: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Capture every processed track and master into a temporary job folder.

        Rendered files remain available only inside the ``with`` block and are
        removed on exit. The returned snapshot binds each file to the exact
        project revision plus its track fader, pan, mute/solo, and FX context,
        and carries a typed ``Capture`` whose ``AudioAsset`` records name the
        stage, method, channel view, content hash and signal window of every
        file.

        ``channels``, ``sample_rate`` and ``tail_ms`` are REAPER render requests:
        they are echoed back by the bridge and refused when they were not
        applied. ``tail_ms`` extends the rendered file past the requested end
        without widening the project window. With no section, the project
        window comes from REAPER's project length cross-checked against the
        rendered sample count minus the applied tail; if those values cannot
        be reconciled the capture fails with an unknown boundary. A bridge
        without a project digest, or one that cannot accept the render
        contract, still yields the legacy dict but is marked
        ``legacy_bridge_contract`` instead of being reported as verified.
        """
        if not isinstance(include_fx_params, bool):
            raise ValidationError("include_fx_params must be a boolean")
        if not isinstance(preserve_artifacts, bool):
            raise ValidationError("preserve_artifacts must be a boolean")
        if not isinstance(capture_mode, str) or capture_mode not in {"render", "accessor"}:
            raise ValidationError("capture_mode must be 'render' or 'accessor'")
        if isinstance(tail_ms, bool) or not isinstance(tail_ms, int) or tail_ms < 0:
            raise ValidationError("tail_ms must be a non-negative integer")
        if not isinstance(channels, str) or channels not in {"stereo", "mono"}:
            raise ValidationError("channels must be 'stereo' or 'mono'")
        if sample_rate is not None and (isinstance(sample_rate, bool) or
                                        not isinstance(sample_rate, int) or
                                        sample_rate <= 0):
            raise ValidationError("sample_rate must be a positive integer or None")
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
            unresolved_sources = _unresolved_source_rows(tracks)
            if unresolved_sources:
                raise MediaUnresolved(
                    "REAPER reports unmuted take sources with no decodable media",
                    details={"sources": unresolved_sources},
                )
            digest_before = _bridge_project_digest(self.bridge, revision.project_id)
            capture_revision = _capture_revision(
                revision.project_id, revision.path, revision.change_count,
                digest_before, revision.instance_id, revision.epoch,
            )
            legacy_contract = digest_before is None
            render_contract = {"channels": channels, "sample_rate": sample_rate,
                               "tail_ms": tail_ms}
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
                # The master render includes the requested tail; sample the
                # same window from the accessor so every captured asset has a
                # common file window.
                sample_end += tail_ms / 1000.0
                sampled = self.bridge.sample_tracks(
                    project_id=revision.project_id,
                    output_dir=workspace.directory,
                    job_id=workspace.job_id,
                    start_s=sample_start,
                    end_s=sample_end,
                )
                # Bind the master render to the revision the bridge observed
                # after accessor sampling. Sampling itself does not mutate take
                # sources; unresolved media is rejected before this point.
                master_revision = sampled.get("revision_after")
                if not isinstance(master_revision, Mapping):
                    master_revision = revision.to_dict()
                master_rendered, accepted = _capture_render(
                    self.bridge,
                    render_contract=render_contract,
                    expected_revision=master_revision,
                    output_dir=workspace.directory,
                    job_id=workspace.job_id,
                    expected_track_count=len(tracks),
                    timeout_s=timeout_s,
                    section_start_s=None if section is None else section.start_s,
                    section_end_s=None if section is None else section.end_s,
                    master_only=True,
                )
                legacy_contract = legacy_contract or not accepted
                rendered = {
                    **master_rendered,
                    "files": [*sampled.get("files", []),
                              *master_rendered.get("files", [])],
                    "capture_point":
                        "native_audio_accessor_pre_fx_tracks_plus_master_render",
                }
            else:
                rendered, accepted = _capture_render(
                    self.bridge,
                    render_contract=render_contract,
                    expected_revision=revision.to_dict(),
                    output_dir=workspace.directory,
                    job_id=workspace.job_id,
                    expected_track_count=len(tracks),
                    timeout_s=timeout_s,
                    section_start_s=None if section is None else section.start_s,
                    section_end_s=None if section is None else section.end_s,
                )
                legacy_contract = legacy_contract or not accepted
            rendered_files = rendered.get("files")
            if not isinstance(rendered_files, list):
                raise BridgeUnavailable("REAPER returned an invalid rendered file list")
            rendered_entries = [item for item in rendered_files
                                if isinstance(item, dict)]
            if len(rendered_entries) != len(rendered_files):
                raise BridgeUnavailable("REAPER returned an invalid rendered audio entry")
            artifacts = workspace.register_assets(
                [item.get("path") for item in rendered_entries]
            )
            # Registered first so the temporary renders are manifest-owned and
            # still get cleaned when the restore report is unusable.
            restore_report = _capture_restore_report(rendered)
            registered_paths = tuple(artifact.path for artifact in artifacts)
            audio_metadata_by_guid: dict[str, dict[str, Any]] = {}
            artifacts_by_guid: dict[str, RegisteredArtifact] = {}
            capture_by_guid: dict[str, dict[str, Any]] = {}
            durations: list[float] = []
            tolerances: list[float] = []
            # The applied tail is read back from the render response, never
            # assumed from the request: it is one of the three values the
            # full-project boundary is derived from.
            tail_s = _verified_render_tail(rendered, tail_ms=tail_ms)
            for item, path, artifact in zip(rendered_entries, registered_paths,
                                            artifacts):
                guid = item.get("source_guid")
                if not isinstance(guid, str) or not guid.strip():
                    raise BridgeUnavailable("REAPER returned an invalid rendered audio entry")
                if guid in capture_by_guid:
                    raise BridgeUnavailable(
                        "REAPER returned a duplicate rendered source GUID",
                        details={"source_guid": guid},
                    )
                try:
                    info = sf.info(path)
                except Exception as exc:
                    raise BridgeUnavailable(
                        "REAPER render is not a readable audio file",
                        details={"source_guid": guid, "path": str(path),
                                 "reason": str(exc)},
                    ) from exc
                if info.frames <= 0 or info.samplerate <= 0 or info.channels <= 0:
                    raise BridgeUnavailable(
                        "REAPER returned an empty or invalid audio file",
                        details={"source_guid": guid, "path": str(path),
                                 "frames": info.frames,
                                 "sample_rate": info.samplerate,
                                 "channels": info.channels},
                    )
                if info.channels not in (1, 2):
                    # The render contract only ever asks for mono or stereo; a
                    # wider file cannot be described by ChannelView.
                    raise BridgeUnavailable(
                        "REAPER render has an unsupported channel count",
                        details={"source_guid": guid, "path": str(path),
                                 "channels": info.channels},
                    )
                duration_s = info.frames / info.samplerate
                if section is not None:
                    expected_duration_s = section.duration_s + tail_s
                    if abs(duration_s - expected_duration_s) > 1.0 / info.samplerate:
                        raise BridgeUnavailable(
                            "REAPER render does not cover the requested project section",
                            details={"source_guid": guid,
                                     "requested_project_section": {
                                         "start_s": section.start_s,
                                         "end_s": section.end_s},
                                     "requested_tail_s": tail_s,
                                     "expected_duration_s": expected_duration_s,
                                     "actual_file_duration_s": duration_s,
                                     "sample_rate": info.samplerate},
                        )
                audio_metadata_by_guid[guid] = {
                    "frames": int(info.frames),
                    "sample_rate": int(info.samplerate),
                    "channels": int(info.channels),
                    "duration_s": duration_s,
                    "file_start_s": 0.0,
                }
                artifacts_by_guid[guid] = artifact
                capture_by_guid[guid] = item
                durations.append(duration_s)
                tolerances.append(1.0 / info.samplerate)
            if durations and max(durations) - min(durations) > max(tolerances):
                raise BridgeUnavailable(
                    "REAPER returned track and master renders with different time windows",
                    details={"durations_s": durations,
                             "sample_rates": [item["sample_rate"]
                                              for item in audio_metadata_by_guid.values()]},
                )
            if not durations:
                raise BridgeUnavailable(
                    "REAPER returned no rendered audio for the capture",
                    details={"files": len(rendered_entries)},
                )
            # A valid WAV header is not evidence of audio.  Measure every
            # rendered source before the capture is accepted, then let the
            # explicit expected-silence rules distinguish a deliberately
            # silent section from a silent render that contradicts audible
            # source tracks.
            audio_content = {
                guid: measure_wave(artifacts_by_guid[guid].path)
                for guid in capture_by_guid
            }
            content_master_guid = master.get("guid")
            if (not isinstance(content_master_guid, str) or
                    content_master_guid not in audio_content):
                raise BridgeUnavailable(
                    "REAPER returned no measurable master audio for the capture",
                    details={"master_guid": content_master_guid,
                             "measured_guids": sorted(audio_content)},
                )
            source_rows = []
            for track in tracks:
                guid = track.get("guid")
                if not isinstance(guid, str) or guid not in audio_content:
                    continue
                source_rows.append({
                    "source_guid": guid,
                    "name": track.get("name"),
                    "mute": bool(track.get("mute")),
                    "content": audio_content[guid],
                })
            activity = source_activity(source_rows)
            activity["active_audio_items"] = _active_audio_item_count(
                tracks, section, tail_s)
            master_validation = {
                "asset_guid": content_master_guid,
                **audio_content[content_master_guid],
                "status": ("silent" if audio_content[content_master_guid]["silent"]
                           else "signal_present"),
                "valid": not audio_content[content_master_guid]["silent"],
            }
            silence = classify_master_silence(
                audio_content[content_master_guid], activity,
                {"mute": bool(master.get("mute")),
                 "volume": master.get("volume")},
            )
            master_validation.update(silence)
            if silence["unexpected_silence"]:
                raise CaptureMasterInvalid(
                    "REAPER returned a silent master while rendered source tracks were audible",
                    details={"master_validation": master_validation,
                             "source_activity": activity},
                )
            # The master is always a native render, so it carries the full
            # render window (and any tail) at the render sample rate. Accessor
            # track files may use another rate but are duration-matched above.
            master_guid = master.get("guid") if isinstance(master, Mapping) else None
            if not isinstance(master_guid, str) or not master_guid:
                raise BridgeUnavailable(
                    "Project context has no master GUID for the capture",
                    details={"master_guid": master_guid},
                )
            master_meta = audio_metadata_by_guid.get(master_guid)
            if master_meta is None:
                raise BridgeUnavailable(
                    "Rendered master has no audio metadata for the capture",
                    details={"master_guid": master_guid},
                )
            render_frames = master_meta["frames"]
            render_sample_rate = master_meta["sample_rate"]
            rendered_duration = master_meta["duration_s"]
            project_start_s = section.start_s if section is not None else 0.0
            # The project window is the requested section, or the verified end
            # of the musical project; the file window is what REAPER actually
            # wrote, which includes any requested tail.
            project_end_evidence = None
            if section is not None:
                project_end_s = section.end_s
                project_end_source = "requested_section"
            else:
                boundary = _resolve_rendered_project_end(
                    length_s=context.get("length_s"),
                    frames=render_frames,
                    sample_rate=render_sample_rate,
                    tail_s=tail_s,
                )
                project_end_s = boundary.project_end_s
                project_end_source = boundary.source
                project_end_evidence = boundary.to_dict()
            time_window = {
                "timeline": "project_seconds",
                "project_start_s": project_start_s,
                "project_end_s": project_end_s,
                "file_start_s": 0.0,
                "file_end_s": rendered_duration,
                "tail_s": tail_s,
                "project_end_source": project_end_source,
                "project_end_evidence": project_end_evidence,
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
            digest_after = _bridge_project_digest(self.bridge, revision.project_id)
            legacy_contract = legacy_contract or digest_after is None
            # A capture only renders and restores. A new track or media item
            # means the restore failed or the project moved under the render,
            # so the returned audio no longer describes the bound snapshot.
            _require_unchanged_structure(
                _structure_evidence(context, digest_before),
                _structure_evidence(context_after, digest_after),
            )
            legacy_contract = legacy_contract or any(
                name not in rendered
                for name in ("render_settings_used", "tail_s")
            )
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
                for item, path in zip(rendered_entries, registered_paths)
            }
            capture_id = new_id("cap")
            request_id = new_id("cap")
            assets: list[AudioAsset] = []
            for guid, capture in capture_by_guid.items():
                metadata = audio_metadata_by_guid[guid]
                artifact = artifacts_by_guid[guid]
                is_master = capture.get("is_master") is True
                if is_master:
                    # The master is always a native render, including during an
                    # accessor capture: the accessor only ever exposes take sums.
                    stage = Stage.master_mix_post_fx
                    asset_method = CaptureMethod.native_render
                elif capture_mode == "accessor":
                    stage = Stage.take_sum_pre_fx
                    asset_method = CaptureMethod.audio_accessor
                else:
                    stage = Stage.track_stem_post_fx
                    asset_method = CaptureMethod.native_render
                assets.append(AudioAsset(
                    asset_id=f"ast_sha256_{artifact.sha256}",
                    capture_id=capture_id,
                    source_guid=guid,
                    stage=stage,
                    capture_method=asset_method,
                    source_kind=SourceKind.unknown,
                    channel_view=(ChannelView.native if metadata["channels"] == 2
                                  else ChannelView.mono_sum),
                    channel_pair=None,
                    sample_rate=metadata["sample_rate"],
                    channels=metadata["channels"],
                    frames=metadata["frames"],
                    duration_s=metadata["duration_s"],
                    bytes=artifact.bytes,
                    sha256=artifact.sha256,
                    uri=None,
                    lease_id=workspace.job_id,
                    lease_expires_at=None,
                    status="available",
                    window=SignalWindow(
                        project_start_s=project_start_s,
                        project_end_s=project_end_s,
                        file_start_s=0.0,
                        file_end_s=metadata["duration_s"],
                        boundary="window_bounds",
                        mapping_status=MappingStatus.unverified,
                        latency_offset_ms=None,
                        map_id=None,
                        uncertainty_ms=None,
                    ),
                    derivation=None,
                ))
            asset_by_guid = {asset.source_guid: asset for asset in assets}

            def describe_track(track: dict[str, Any]) -> dict[str, Any]:
                guid = track.get("guid")
                capture = capture_by_guid.get(guid)
                path = path_by_guid.get(guid)
                asset = asset_by_guid.get(guid) if isinstance(guid, str) else None
                if (not isinstance(guid, str) or capture is None or path is None
                        or asset is None):
                    raise BridgeUnavailable("A project track has no matching rendered WAV",
                                            details={"track_guid": guid})
                capture_context = {key: value for key, value in capture.items()
                                   if key != "path"} | {
                                       "path": path,
                                       "time_window": time_window,
                                       "audio_format": audio_metadata_by_guid[guid],
                                       "asset_id": asset.asset_id,
                                       "sha256": asset.sha256,
                                       "stage": str(asset.stage),
                                       # ``capture_method`` (a free-form string)
                                       # predates the typed contract and exists
                                       # only for readers that still parse it.
                                       # ``capture_method_typed`` is the value to
                                       # read; the two never diverge:
                                       #   native_render -> "reaper_native_offline_render"
                                       #   audio_accessor -> "reaper_audio_accessor"
                                       # A master is always a native render, even
                                       # during an accessor capture.
                                       "capture_method_typed": str(asset.capture_method),
                                       "source_kind": str(asset.source_kind),
                                       "channel_view": str(asset.channel_view),
                                       "lease_id": asset.lease_id,
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

            expected_assets = len(tracks) + 1
            digest_missing = (tuple(digest_before.get("missing") or ())
                              if digest_before is not None else ())
            solo_active = any(
                isinstance(track, Mapping)
                and track.get("solo") not in (None, 0, False)
                for track in tracks
            )
            warnings: list[str] = []
            if solo_active:
                warnings.append("solo_active")
            if capture_revision.fingerprint_scope != "full":
                warnings.append("revision_digest_partial")
            if legacy_contract:
                warnings.append("legacy_bridge_contract")
            capture_complete = (
                len(assets) == expected_assets
                and all(
                    isinstance(guid, str) and guid in asset_by_guid
                    for guid in [*(track.get("guid") for track in tracks
                                   if isinstance(track, Mapping)),
                                 master.get("guid")]
                )
            )
            render_settings_used = rendered.get("render_settings_used")
            if isinstance(render_settings_used, Mapping):
                # A capture renders to files; it must never place stems on the
                # project playlist. ``RENDER_ADDTOPROJ`` is the setting that
                # would do exactly that, so a bridge that reports it as on
                # fails the capture instead of silently growing the project.
                add_to_project = render_settings_used.get("RENDER_ADDTOPROJ")
                if add_to_project not in (None, 0, 0.0, False):
                    raise BridgeUnavailable(
                        "REAPER added the capture stems to the project playlist",
                        details={"render_settings_used": dict(render_settings_used),
                                 "RENDER_ADDTOPROJ": add_to_project},
                    )
            revision_after_ref = _capture_revision(
                revision.project_id, current_revision.path,
                current_revision.change_count, digest_after,
                current_revision.instance_id, current_revision.epoch,
            )
            capture_obj = Capture(
                capture_id=capture_id,
                request_id=request_id,
                method=(CaptureMethod.audio_accessor if capture_mode == "accessor"
                        else CaptureMethod.native_render),
                stages=tuple(dict.fromkeys(asset.stage for asset in assets)),
                assets=tuple(assets),
                revision_before=capture_revision,
                revision_after=revision_after_ref,
                render_settings=(render_settings_used
                                 if isinstance(render_settings_used, Mapping) else {}),
                restore_report=restore_report,
                solo_active=solo_active,
                tail_s=tail_s,
                coverage={
                    "expected": expected_assets,
                    "assets": len(assets),
                    "digest": (dict(digest_before.get("coverage") or {})
                               if digest_before is not None else {}),
                    # Digest gaps are reported here, because ``missing`` below
                    # speaks about captured assets only.
                    "digest_missing": list(digest_missing),
                },
                missing=(() if capture_complete else digest_missing),
                warnings=tuple(warnings),
                status="complete" if capture_complete else "partial",
            )

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
                "capture": capture_obj,
                "assets": tuple(assets),
                "audio_content": audio_content,
                "source_activity": activity,
                "master_validation": master_validation,
                "capture_revision": capture_revision.to_dict(),
                "revision_after_ref": revision_after_ref.to_dict(),
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
        tail_ms: int = 0,
        channels: str = "stereo",
        sample_rate: int | None = None,
    ) -> dict:
        """Capture, analyze, and return all project audio with matching context.

        Every non-master track and the rendered master are analyzed in one
        bundle. Temporary WAVs are deleted by ``capture_audio`` before return;
        returned reports use stable source GUIDs rather than temporary paths.
        Project-tempo values are attached to each temporal-profile cell.

        ``tail_ms`` asks REAPER to write extra audio past the requested
        ``section`` so reverb decays survive the render.  It widens the captured
        file only: every metric, curve and report window still describes the
        project section, and each source reports the measured and file windows
        separately.  Without a section, the measured interval is the verified
        end of the whole project, which excludes the rendered tail.
        ``channels`` and ``sample_rate`` are the render request echoed and
        verified by the bridge.

        REAPER's AudioAccessor returns track samples immediately pre-FX. It can
        be used through ``capture_audio`` for raw-source extraction, but cannot
        stand in for the processed tracks required for a mix analysis.
        """
        if not isinstance(include_pairwise, bool):
            raise ValidationError("include_pairwise must be a boolean")
        if parallel and not _main_module_can_spawn():
            # Running from stdin/REPL: spawn cannot re-import __main__.
            parallel = False
        if max_workers is not None:
            validate_analysis_worker_request(max_workers)
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
        if isinstance(tail_ms, bool) or not isinstance(tail_ms, int) or tail_ms < 0:
            raise ValidationError("tail_ms must be a non-negative integer")
        if not isinstance(channels, str) or channels not in {"stereo", "mono"}:
            raise ValidationError("channels must be 'stereo' or 'mono'")
        if sample_rate is not None and (isinstance(sample_rate, bool) or
                                        not isinstance(sample_rate, int) or
                                        sample_rate <= 0):
            raise ValidationError("sample_rate must be a positive integer or None")
        if capture_mode == "accessor":
            # The accessor writes exactly ``end_s - start_s`` seconds and has no
            # tail control, so a tail request is rejected before any workspace,
            # snapshot or render is attempted.
            if tail_ms > 0:
                raise ValidationError(
                    "AudioAccessor capture cannot render a tail; use "
                    "capture_mode='render' for tail_ms > 0"
                )
            raise ValidationError(
                "AudioAccessor samples are pre-FX and are not processed mix stems; "
                "use capture_audio(..., capture_mode='accessor') for raw-source capture"
            )
        tail_s = tail_ms / 1000.0

        result: dict[str, Any] | None = None
        captured: dict[str, Any] | None = None
        with self.capture_audio(
            artifact_root, include_fx_params=include_fx_params,
            timeout_s=timeout_s, section=section, capture_mode=capture_mode,
            preserve_artifacts=retain_audio, tail_ms=tail_ms,
            channels=channels, sample_rate=sample_rate,
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

            time_window = captured.get("time_window")
            window = time_window if isinstance(time_window, Mapping) else {}
            reported_tail_s = _finite_or_none(window.get("tail_s"))
            if reported_tail_s is not None and reported_tail_s >= 0:
                tail_s = reported_tail_s
            project_end_s = (None if section is not None
                             else _finite_or_none(window.get("project_end_s")))
            if section is None and tail_s > 0 and project_end_s is None:
                # The capture must have resolved the full-project boundary
                # before a tail can be measured; otherwise the tail would leak
                # into the analysis window.
                raise BridgeUnavailable(
                    "Audio capture did not verify the full-project boundary, so "
                    "the tail cannot be separated from measured audio",
                    details={"boundary": "unknown", "tail_s": tail_s,
                             "time_window": time_window},
                )

            needs_buffers = ((not parallel) or phase_pairs is not None
                             or include_partial_loudness)
            source_audio: dict[str, AudioBuffer] = {}
            if needs_buffers:
                for guid, path in source_paths.items():
                    decoded = _load_rendered_audio(
                        path, section, tail_s, project_end_s=project_end_s)
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
                workers = _bundle_analysis_workers(
                    len(source_paths), section=section,
                    time_window=captured.get("time_window"),
                    requested=max_workers)
                with ProcessPoolExecutor(max_workers=workers) as pool:
                    precomputed = dict(zip(
                        source_paths,
                        pool.map(_measure_source_job,
                                 [(path, section, tail_s, project_end_s)
                                  for path in source_paths.values()]),
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

            analysis_window = _source_analysis_window(
                captured.get("time_window"), section=section, tail_s=tail_s
            )
            for source_id, source_report in report["sources"].items():
                source_report["track_context"] = track_context[source_id]
                source_report["capture"] = capture_files[source_id]
                # Metrics above were measured over the project section only.
                # This window keeps the captured-but-unmeasured tail visible
                # instead of folding it into the measured span.
                source_report["analysis_window"] = dict(analysis_window)

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
                # change_count drifts by a few units because capturing stems
                # temporarily reassigns routing and restore bumps the revision.
                # The project identity and path are what matter for tempo data.
                if (tempo_result.get("project_id") != revision.project_id
                        or tempo_revision.path != revision.path):
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
            capture_obj = captured.get("capture")
            capture_is_typed = isinstance(capture_obj, Capture)
            capture_window = captured.get("time_window")
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
                "capture_id": capture_obj.capture_id if capture_is_typed else None,
                "method": str(capture_obj.method) if capture_is_typed else None,
                "stages": ([str(stage) for stage in capture_obj.stages]
                           if capture_is_typed else []),
                "tail_s": tail_s,
                "time_window_project": {
                    "start_s": analysis_window["project_start_s"],
                    "end_s": analysis_window["project_end_s"],
                },
                "time_window_file": {
                    "start_s": analysis_window["file_start_s"],
                    "end_s": analysis_window["file_end_s"],
                },
                "revision_before": (capture_obj.revision_before.to_dict()
                                    if capture_is_typed else None),
                "revision_after": (
                    capture_obj.revision_after.to_dict()
                    if capture_is_typed and capture_obj.revision_after is not None
                    else None
                ),
                "coverage": (dict(capture_obj.coverage)
                             if capture_is_typed else None),
                "warnings": ([str(warning) for warning in capture_obj.warnings]
                             if capture_is_typed else []),
                "cleanup": None,
            }
            # Mirrored at the top level so a reader never has to guess whether
            # the analysis window it is looking at includes a rendered tail.
            report["tail_s"] = tail_s
            report["capture_time_window"] = (dict(capture_window)
                                             if isinstance(capture_window, Mapping)
                                             else {})
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

    def render_candidate(
        self,
        plan: EditPlan,
        *,
        revision_after: ProjectRevision | dict[str, Any],
        section: Section,
        artifact_root: str | Path,
        channels: str = "stereo",
        sample_rate: int | None = None,
        tail_ms: int = 0,
        max_duration_s: float = 120.0,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> dict[str, Any]:
        """Render the applied plan's processed master output.

        The native REAPER render path is used (master mix, track/bus/master FX
        and routing included). It is not a capture. Native settings/selection
        are restored and verified; a counter advance is returned explicitly as
        ``revision_after_render`` rather than refused or hidden. Temporary audio
        is retained only for the caller's lease and can be removed by the
        normal release path.
        """
        from .render_candidate import render_candidate

        revision = (revision_after if isinstance(revision_after, ProjectRevision)
                    else ProjectRevision.from_dict(dict(revision_after)))
        return render_candidate(
            self, plan, revision_after=revision, section=section,
            artifact_root=artifact_root, channels=channels,
            sample_rate=sample_rate, tail_ms=tail_ms,
            max_duration_s=max_duration_s, max_bytes=max_bytes,
        )

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
