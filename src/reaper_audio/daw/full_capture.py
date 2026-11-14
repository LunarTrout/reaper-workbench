"""Full post-FX capture: project map + every source/master asset + analysis.

The render path deliberately uses one proven master-only render per track with
that track temporarily soloed.  REAPER's multi-stem bitmask is not assumed to be
correct until a live target/GUID proof exists; this path has a one-file/one-GUID
contract per render and restores solo state in ``finally``.
"""

from __future__ import annotations

import json
import math
import multiprocessing
import os
import tempfile
import time
from collections.abc import Mapping
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from ..analysis.artifacts import AudioArtifactWorkspace
from ..analysis.audio import load_audio
from ..analysis.metrics import measure_audio
from ..context.full_capture import CaptureArchive, FullCaptureRecord, REQUIRED_MAP_KEYS
from ..context.ids import new_id
from ..context.panorama import (build_panorama, build_relation_domain, load_panorama,
                                panorama_path_for_record)
from ..context.render_dependencies import build_render_manifest
from ..context.changes import digest_facts
from ..errors import BridgeUnavailable, CaptureMasterInvalid, ValidationError
from ..reaper.handles import ReaperHandle
from .analysis_pipeline import BoundedAnalysisPool
from .analysis_policy import (MAX_ANALYSIS_WORKERS,
                              resolve_analysis_workers,
                              validate_analysis_worker_request)
from .analysis_workers import analyze_asset_job
from .capture_batching import CaptureBatchPlan, plan_capture_batches
from .session import ProjectRevision, ProjectSession

# Long/unknown-duration captures default to four workers; short sections may
# use up to eight through the shared policy.  Twelve and sixteen are not a
# production mode.
DEFAULT_ANALYSIS_WORKERS = 4
MAX_CAPTURE_OVERLAP_WORKERS = MAX_ANALYSIS_WORKERS
MASTER_RENDER_MAX_ATTEMPTS = 2
MASTER_SILENCE_DBFS = -120.0


def _archived_record_binding(record: FullCaptureRecord, archived: Any) -> dict[str, Any]:
    """Return the small binding view for the record that the archive sealed.

    ``CaptureArchive.append`` adds durable audio paths/status and recalculates
    the record integrity hash.  The original in-memory record therefore has a
    stale integrity hash after append.  Build the panorama validator's minimal
    identity from the archive result without reloading the very large record.
    """
    return {
        "capture_id": archived.capture_id,
        "project_id": record.project_id,
        "integrity_sha256": archived.integrity_sha256,
        "revision_before": record.revision_before,
        "revision_after": record.revision_after,
        "time_window": record.time_window,
    }


def _dbfs(value: float) -> float:
    if value <= 0:
        return -160.0
    return float(max(-160.0, 20.0 * math.log10(value)))


def _wave_activity(path: Path) -> dict[str, Any]:
    info = load_audio(path)
    samples = np.asarray(info.samples, dtype=np.float64)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    rms = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
    peak_dbfs = _dbfs(peak)
    rms_dbfs = _dbfs(rms)
    return {
        "frames": int(info.frames),
        "sample_rate": int(info.sample_rate),
        "channels": int(info.channels),
        "peak_dbfs": peak_dbfs,
        "rms_dbfs": rms_dbfs,
        "audible": peak_dbfs > MASTER_SILENCE_DBFS or rms_dbfs > MASTER_SILENCE_DBFS,
    }


def _track_activity(track_files: list[tuple[str, str, str, bool]]) -> dict[str, Any]:
    """Classify rendered source stems with the same signal floor as master."""
    rows = []
    audible = []
    for item in track_files:
        guid, name, path = item[0], item[1], item[2]
        mute = bool(item[3]) if len(item) > 3 else False
        activity = _wave_activity(Path(path))
        row = {"source_guid": guid, "name": name, "mute": mute, **activity}
        rows.append(row)
        if activity["audible"]:
            audible.append(guid)
    return {
        "tracks": rows,
        "audible_guids": audible,
        "audible_count": len(audible),
        "all_silent": not audible,
    }


def _classify_master_silence(validation: Mapping[str, Any],
                             source_activity: Mapping[str, Any],
                             master_state: Mapping[str, Any]) -> dict[str, Any]:
    """Classify a silent master using rendered sources and captured state.

    A silent master is not a failure when the requested section contains no
    audible source, when every source is muted, or when the master itself is
    intentionally muted/zeroed.  It is unexpected only when at least one
    rendered source is audible and the captured master state does not explain
    the silence.
    """
    silent = bool(validation.get("silent"))
    if not silent:
        return {"expected_silence": False, "silence_reason": None,
                "unexpected_silence": False}
    tracks = source_activity.get("tracks")
    if isinstance(tracks, (list, tuple)):
        all_sources_silent = not bool(source_activity.get("audible_guids"))
        mute_values = [
            bool(item.get("mute")) for item in tracks
            if isinstance(item, Mapping) and "mute" in item
        ]
        all_tracks_muted = (
            bool(tracks) and len(mute_values) == len(tracks) and
            all(mute_values)
        )
    else:
        all_sources_silent = bool(source_activity.get("all_silent"))
        all_tracks_muted = False
    master_muted = bool(master_state.get("mute"))
    volume = master_state.get("volume")
    master_zero = (isinstance(volume, (int, float)) and
                   not isinstance(volume, bool) and float(volume) <= 1e-6)
    reasons = []
    if all_sources_silent:
        reasons.append("all_rendered_sources_silent")
    if all_tracks_muted:
        reasons.append("all_project_tracks_muted")
    if master_muted:
        reasons.append("master_muted")
    if master_zero:
        reasons.append("master_zero_volume")
    expected = bool(reasons)
    return {
        "expected_silence": expected,
        "silence_reason": "+".join(reasons) if reasons else None,
        "unexpected_silence": not expected,
    }


def _final_master_validation(validation: Mapping[str, Any],
                             attempts: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None) -> dict[str, Any]:
    """Apply the expected-silence decision to the full analysis validation."""
    result = dict(validation)
    selected = None
    if attempts:
        selected = next((item for item in attempts
                         if isinstance(item, Mapping) and item.get("selected")), None)
        if selected is None:
            selected = next((item for item in reversed(attempts)
                             if isinstance(item, Mapping)), None)
    if result.get("silent") and isinstance(selected, Mapping) and selected.get("expected_silence"):
        result.update({
            "status": "expected_silence",
            "valid": True,
            "expected_silence": True,
            "unexpected_silence": False,
            "silence_reason": selected.get("silence_reason"),
            "source_activity": selected.get("source_activity"),
        })
    else:
        result.update({
            "expected_silence": False,
            "unexpected_silence": bool(result.get("silent")),
            "silence_reason": None,
        })
    return result


def _wave_master_validation(path: Path, master_guid: str) -> dict[str, Any]:
    """Validate a master-only render before it is accepted into a capture.

    This is deliberately a signal gate, not a substitute for the full
    measurement report.  It uses the same peak/RMS floor as
    ``_master_validation`` and records the first-attempt evidence verbatim.
    """
    info = load_audio(path)
    samples = np.asarray(info.samples, dtype=np.float64)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    rms = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
    peak_dbfs = _dbfs(peak)
    rms_dbfs = _dbfs(rms)
    silent = peak_dbfs <= MASTER_SILENCE_DBFS and rms_dbfs <= MASTER_SILENCE_DBFS
    return {
        "asset_guid": master_guid,
        "peak_dbfs": peak_dbfs,
        "rms_dbfs": rms_dbfs,
        "status": "silent" if silent else "signal_present",
        "silent": silent,
        "valid": not silent,
        "frames": int(info.frames),
        "sample_rate": int(info.sample_rate),
        "channels": int(info.channels),
    }


def _analysis_worker_count(requested: int | None, asset_count: int,
                           window_s: float | None = None,
                           *, available_memory_bytes: int | None = None) -> int:
    """Return the shared policy's worker count for one analysis pass."""
    return resolve_analysis_workers(
        asset_count=asset_count, window_s=window_s, requested=requested,
        available_memory_bytes=available_memory_bytes,
    ).workers


def _master_validation(analysis: Mapping[str, Any], master_guid: str) -> dict[str, Any]:
    """Describe the archived master signal without masking a silent render."""
    report = analysis.get(master_guid)
    metrics = report.get("metrics") if isinstance(report, Mapping) else None
    peak = None
    rms = None
    if isinstance(metrics, Mapping):
        peak_cell = metrics.get("peak_dbfs")
        rms_cell = metrics.get("rms_dbfs")
        if isinstance(peak_cell, Mapping):
            peak = peak_cell.get("value")
        if isinstance(rms_cell, Mapping):
            rms = rms_cell.get("value")
    numeric = [value for value in (peak, rms)
               if isinstance(value, (int, float)) and not isinstance(value, bool)]
    silent = bool(numeric) and all(float(value) <= -120.0 for value in numeric)
    status = "missing" if report is None else ("silent" if silent else "signal_present")
    return {
        "asset_guid": master_guid,
        "peak_dbfs": peak,
        "rms_dbfs": rms,
        "status": status,
        "silent": silent,
        "valid": status == "signal_present",
    }


def _analysis_timing_fields(*, analysis_wall_s: float,
                            analysis_pool_wall_s: float,
                            dsp_worker_elapsed_sum_s: float,
                            wav_decode_worker_elapsed_sum_s: float) -> dict[str, Any]:
    """Name timing units explicitly; never label a worker sum as elapsed wall."""
    return {
        "timings_schema": "capture-timings/2",
        "analysis_wall_s": round(analysis_wall_s, 3),
        "analysis_pool_wall_s": round(analysis_pool_wall_s, 3),
        "dsp_worker_elapsed_sum_s": round(dsp_worker_elapsed_sum_s, 3),
        "wav_decode_worker_elapsed_sum_s": round(
            wav_decode_worker_elapsed_sum_s, 3),
        # Legacy key read by older consumers. It used to hold a worker sum;
        # it now always means elapsed pool wall time, never DSP-only time.
        "dsp_measure_s": round(analysis_pool_wall_s, 3),
        # Legacy alias retained as a wall-clock synonym.
        "analysis_s": round(analysis_wall_s, 3),
        "analysis_pool_s": round(analysis_pool_wall_s, 3),
    }


def _analyze_rendered_files(payloads: list[tuple[str, str]],
                            workers: int) -> list[dict[str, Any]]:
    """Measure assets in deterministic input order with a fixed worker pool."""
    if not payloads:
        return []
    if workers <= 1:
        return [analyze_asset_job(payload) for payload in payloads]
    context = multiprocessing.get_context("spawn")
    executor = ProcessPoolExecutor(max_workers=workers, mp_context=context)
    futures = [executor.submit(analyze_asset_job, payload) for payload in payloads]
    try:
        results = [future.result() for future in futures]
    except BaseException:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    executor.shutdown(wait=True)
    return results



def _master_attempt_entry(*, attempt: int, item: Mapping[str, Any], path: Path,
                          validation: Mapping[str, Any],
                          rendered: Mapping[str, Any],
                          start_s: float | None, end_s: float | None,
                          project_length_s: float) -> dict[str, Any]:
    """Build the durable evidence row for one master render attempt.

    Silence classification is deliberately filled in only after the rendered
    sources exist; ``_apply_master_classification`` supplies the same fields
    the serial gate produced.
    """
    sha = _sha256_file(path)
    return {
        "attempt": attempt,
        "status": str(validation.get("status") or "unknown"),
        "silent": bool(validation.get("silent")),
        "valid": not bool(validation.get("silent")),
        "expected_silence": False,
        "unexpected_silence": bool(validation.get("silent")),
        "silence_reason": None,
        "source_activity": None,
        "peak_dbfs": validation.get("peak_dbfs"),
        "rms_dbfs": validation.get("rms_dbfs"),
        "frames": validation.get("frames"),
        "sample_rate": validation.get("sample_rate"),
        "channels": validation.get("channels"),
        "sha256": sha,
        "bytes": path.stat().st_size,
        "archive_audio": f"audio/sha256/{sha}.wav",
        "render_s": rendered.get("_elapsed_s"),
        "render_settings": dict(rendered.get("render_settings_used") or {}),
        "time_window": {
            "project_start_s": (start_s if start_s is not None else 0.0),
            "project_end_s": (end_s if end_s is not None else project_length_s),
        },
        "selected": False,
    }


def _apply_master_classification(entry: dict[str, Any],
                                 validation: Mapping[str, Any],
                                 classification: Mapping[str, Any],
                                 source_activity: Mapping[str, Any]) -> None:
    entry.update({
        "status": ("expected_silence" if classification.get("expected_silence")
                   else str(validation.get("status") or "unknown")),
        "valid": (not bool(validation.get("silent")) or
                  bool(classification.get("expected_silence"))),
        "expected_silence": bool(classification.get("expected_silence")),
        "unexpected_silence": bool(classification.get("unexpected_silence")),
        "silence_reason": classification.get("silence_reason"),
        "source_activity": {
            "audible_count": source_activity.get("audible_count"),
            "audible_guids": list(source_activity.get("audible_guids") or []),
            "all_silent": source_activity.get("all_silent"),
        },
    })


def _overlap_timing(*, started: float,
                    first_submit_at: float | None,
                    pool_created_at: float | None,
                    last_render_end: float | None,
                    analysis_end: float | None,
                    render_ends: list[float],
                    pipeline_stats: Mapping[str, Any] | None,
                    admission_times: list[dict[str, Any]],
                    batch_plan: CaptureBatchPlan | None) -> dict[str, Any]:
    """Describe measured overlap without turning it into a speed claim."""
    def relative(value: float | None) -> float | None:
        return None if value is None else round(value - started, 3)

    overlap_s = None
    if (first_submit_at is not None and last_render_end is not None and
            analysis_end is not None):
        overlap_s = round(max(0.0, min(last_render_end, analysis_end) -
                              first_submit_at), 3)
    completed_before_render_end = None
    completions = (pipeline_stats or {}).get("completion_times_s")
    if (isinstance(completions, Mapping) and last_render_end is not None and
            pool_created_at is not None):
        completed_before_render_end = sum(
            1 for value in completions.values()
            if isinstance(value, (int, float)) and
            pool_created_at + float(value) <= last_render_end)
    return {
        "master_first": True,
        "batch_plan": batch_plan.summary() if batch_plan is not None else None,
        "batch_admissions": list(admission_times),
        "batch_render_ends_s": [relative(value) for value in render_ends],
        "renders_finished_before_analysis_end": (
            None if analysis_end is None else
            sum(1 for value in render_ends if value <= analysis_end)),
        "first_analysis_submit_s": relative(first_submit_at),
        "last_render_end_s": relative(last_render_end),
        "analysis_end_s": relative(analysis_end),
        "analysis_render_overlap_s": overlap_s,
        "results_before_render_end": completed_before_render_end,
        "analysis_pool": dict(pipeline_stats) if pipeline_stats else None,
    }


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValidationError(f"{field} must be finite")
    return number


class FullCaptureService:
    """Capture a complete, archiveable project snapshot through one session."""

    def __init__(self, session: ProjectSession, archive_root: str | os.PathLike[str]):
        self.session = session
        self.bridge = session.bridge
        self.archive_root = Path(archive_root).expanduser().absolute()

    # ------------------------------------------------------------------ #
    # public
    # ------------------------------------------------------------------ #
    def capture(
        self,
        *,
        section: tuple[float, float] | None = None,
        channels: str = "stereo",
        sample_rate: int | None = None,
        tail_ms: int = 0,
        include_midi_notes: bool = True,
        analysis_workers: int | None = None,
        producer: str = "hermes",
    ) -> dict[str, Any]:
        if channels not in {"stereo", "mono"}:
            raise ValidationError("channels must be stereo or mono")
        if sample_rate is not None and (
            isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate <= 0
        ):
            raise ValidationError("sample_rate must be a positive integer or null")
        if isinstance(tail_ms, bool) or not isinstance(tail_ms, int) or tail_ms < 0:
            raise ValidationError("tail_ms must be a non-negative integer")
        if analysis_workers is not None:
            validate_analysis_worker_request(analysis_workers)
        start_s = end_s = None
        if section is not None:
            if (not isinstance(section, (tuple, list)) or len(section) != 2):
                raise ValidationError("section must be [start_s, end_s]")
            start_s, end_s = (_number(section[0], "section.start_s"),
                              _number(section[1], "section.end_s"))
            if end_s <= start_s:
                raise ValidationError("section.end_s must be greater than start_s")

        started = time.perf_counter()
        self._profile = {"mute_rpc_s": 0.0, "mute_rpc_calls": 0,
                         "mute_bridge_calls": 0, "mute_track_mutations": 0,
                         "render_s": 0.0, "render_calls": 0,
                         "render_times": {}, "render_batch_s": 0.0,
                         "admit_s": 0.0, "decode_s": 0.0, "dsp_s": 0.0}
        snapshot_start = time.perf_counter()
        context = self.session.snapshot(
            include_items=True,
            include_fx_params=True,
            include_midi_notes=include_midi_notes,
        )
        revision = ProjectRevision.from_dict(context.get("revision", {}))
        if not revision.project_id or not revision.path:
            raise BridgeUnavailable("REAPER returned an incomplete project identity")
        tracks = context.get("tracks")
        master = context.get("master")
        if not isinstance(tracks, list) or not tracks or not isinstance(master, dict):
            raise BridgeUnavailable("REAPER returned an incomplete project map")
        transport = context.get("transport")
        if not isinstance(transport, Mapping) or transport.get("play_state") not in (0, 0.0):
            raise ValidationError(
                "Stop playback before a full capture; capture never starts or stops it",
                details={"transport": dict(transport) if isinstance(transport, Mapping) else transport},
            )
        snapshot_end = time.perf_counter()
        digest_start = time.perf_counter()
        digest = self.bridge.project_digest(revision.project_id)
        digest_end = time.perf_counter()
        digest_coverage = digest.get("coverage") if isinstance(digest, dict) else None
        if (not isinstance(digest_coverage, Mapping) or
                not digest_coverage or any(value is not True for value in digest_coverage.values())):
            raise BridgeUnavailable(
                "A full capture requires a fully covered project digest",
                details={"coverage": digest_coverage},
            )

        workspace_root = self.archive_root / ".incoming"
        workspace_root.mkdir(parents=True, exist_ok=True)
        archive = CaptureArchive(self.archive_root)
        capture_id = new_id("cap")
        created_at = datetime.now(timezone.utc).isoformat()
        original_mute = {
            str(track.get("guid")): int(bool(track.get("mute")))
            for track in tracks if isinstance(track, Mapping) and track.get("guid")
        }
        routing = self._routing_graph(tracks, digest)
        rendered_files: list[dict[str, Any]] = []
        track_items: list[dict[str, Any]] = []
        master_item: dict[str, Any] | None = None
        master_rendered: dict[str, Any] | None = None
        master_attempts: list[dict[str, Any]] = []
        extra_master_files: dict[str, str] = {}
        current = revision
        workspace: AudioArtifactWorkspace | None = None
        record: FullCaptureRecord | None = None
        stored: FullCaptureRecord | None = None
        pipeline: BoundedAnalysisPool | None = None
        pipeline_closed = False
        pool_created_at: float | None = None
        first_submit_at: float | None = None
        admission_times: list[dict[str, Any]] = []
        render_ends: list[float] = []
        batch_plan: CaptureBatchPlan | None = None
        batch_renders: list[dict[str, Any]] = []
        last_render: dict[str, Any] | None = None
        last_render_end: float | None = None
        analysis_end: float | None = None
        pool_end: float | None = None
        analysis_timing: dict[str, Any] | None = None
        pipeline_stats: dict[str, Any] | None = None
        results: list[dict[str, Any]] | None = None
        try:
            workspace = AudioArtifactWorkspace(workspace_root)
            tracks_by_guid = {
                str(track.get("guid")): track
                for track in tracks
                if isinstance(track, Mapping) and track.get("guid")
            }
            all_guids = [str(track.get("guid")) for track in tracks]
            master_guid = str(master.get("guid"))
            project_length_s = float(context.get("length_s") or 0.0)
            window_s: float | None
            if section is not None:
                start_value = float(start_s) if start_s is not None else 0.0
                end_value = float(end_s) if end_s is not None else 0.0
                window_s = end_value - start_value
            else:
                window_s = project_length_s if project_length_s > 0 else None
            worker_plan = resolve_analysis_workers(
                asset_count=len(tracks) + 1,
                window_s=window_s,
                requested=analysis_workers,
            )
            workers = worker_plan.workers

            # The master is rendered and validated before any track batch.
            # A silent master is kept as an attempt and classified only after
            # the rendered sources exist; the historical two-attempt gate and
            # its evidence contract are unchanged.
            master_rendered = self._render_master_only(
                current, workspace, f"{workspace.job_id}-master-1",
                start_s, end_s, channels, sample_rate, tail_ms,
            )
            current = ProjectRevision.from_dict(master_rendered["revision_after"])
            master_item = self._one_file(master_rendered, workspace)
            master_path = Path(str(master_item["path"]))
            self._check_rendered_wav(master_path, master_rendered)
            master_validation = _wave_master_validation(master_path, master_guid)
            master_entry = _master_attempt_entry(
                attempt=1, item=master_item, path=master_path,
                validation=master_validation, rendered=master_rendered,
                start_s=start_s, end_s=end_s,
                project_length_s=project_length_s,
            )
            master_attempts.append(master_entry)
            master_is_silent = bool(master_validation["silent"])
            master_submitted = False
            if not master_is_silent:
                master_entry.update({
                    "valid": True, "selected": True,
                    "expected_silence": False, "unexpected_silence": False,
                })
            last_render = master_rendered
            last_render_end = time.perf_counter()
            render_ends.append(last_render_end)

            if workers > 1:
                pool_created_at = time.perf_counter()
                pipeline = BoundedAnalysisPool(workers)
                if not master_is_silent:
                    pipeline.submit([(master_guid, str(master_path))])
                    master_submitted = True
                    first_submit_at = time.perf_counter()

            batch_plan = plan_capture_batches(tracks, digest)
            full_project_render = (batch_plan.fallback_reason is not None or
                                   len(batch_plan.batches) <= 1)
            if full_project_render:
                planned_batches: list[tuple[list[str], int | None]] = [
                    (all_guids, None)]
            else:
                planned_batches = [
                    (list(batch.guids), index)
                    for index, batch in enumerate(batch_plan.batches, 1)
                ]
            for batch_guids, batch_number in planned_batches:
                job_suffix = ("batch" if batch_number is None
                              else f"batch-{batch_number}")
                rendered = self._render_batch(
                    current, workspace, f"{workspace.job_id}-{job_suffix}",
                    len(batch_guids), start_s, end_s, channels, sample_rate,
                    tail_ms,
                    track_guids=(None if full_project_render else batch_guids),
                )
                current = ProjectRevision.from_dict(rendered["revision_after"])
                admitted = self._admit_track_batch(
                    rendered, batch_guids, tracks_by_guid, workspace)
                track_items.extend(admitted)
                batch_renders.append(rendered)
                last_render = rendered
                last_render_end = time.perf_counter()
                render_ends.append(last_render_end)
                payloads = [(str(item["source_guid"]), str(item["path"]))
                            for item in admitted]
                if pipeline is not None:
                    pipeline.submit(payloads)
                    if first_submit_at is None:
                        first_submit_at = time.perf_counter()
                    admission_times.append({
                        "batch": batch_number if batch_number is not None else 1,
                        "tracks": len(payloads),
                        "at_s": round(time.perf_counter() - started, 3),
                    })

            track_items.sort(key=lambda item: int(item.get("track_index", -1)))
            if (len(track_items) != len(tracks) or
                    {str(item["source_guid"]) for item in track_items} != set(all_guids)):
                raise BridgeUnavailable(
                    "The rendered track batches did not cover every project track exactly once",
                    details={"expected": len(tracks), "returned": len(track_items),
                             "batch_plan": batch_plan.summary() if batch_plan else None},
                )
            source_activity_started = time.perf_counter()
            track_mute = {
                str(track.get("guid")): bool(track.get("mute"))
                for track in tracks if isinstance(track, Mapping) and track.get("guid")
            }
            source_activity = _track_activity([
                (str(item["source_guid"]), str(item.get("name") or ""),
                 str(item["path"]), track_mute.get(str(item["source_guid"]), False))
                for item in track_items if item.get("path")
            ])
            source_activity_s = time.perf_counter() - source_activity_started

            master_classification = _classify_master_silence(
                master_validation, source_activity, master)
            _apply_master_classification(
                master_entry, master_validation, master_classification,
                source_activity)
            if master_is_silent and not master_classification["expected_silence"]:
                extra_master_files[str(master_entry["sha256"])] = str(master_path)
                rendered = self._render_master_only(
                    current, workspace, f"{workspace.job_id}-master-2",
                    start_s, end_s, channels, sample_rate, tail_ms,
                )
                current = ProjectRevision.from_dict(rendered["revision_after"])
                item = self._one_file(rendered, workspace)
                path = Path(str(item["path"]))
                self._check_rendered_wav(path, rendered)
                validation = _wave_master_validation(path, master_guid)
                entry = _master_attempt_entry(
                    attempt=2, item=item, path=path,
                    validation=validation, rendered=rendered,
                    start_s=start_s, end_s=end_s,
                    project_length_s=project_length_s,
                )
                classification = _classify_master_silence(
                    validation, source_activity, master)
                _apply_master_classification(
                    entry, validation, classification, source_activity)
                entry["selected"] = True
                master_attempts.append(entry)
                master_item, master_rendered = item, rendered
                master_path, master_validation = path, validation
                master_is_silent = bool(validation["silent"])
                last_render = rendered
                last_render_end = time.perf_counter()
                render_ends.append(last_render_end)
                if master_is_silent and not classification["expected_silence"]:
                    extra_master_files[str(entry["sha256"])] = str(path)
            else:
                master_entry["selected"] = True

            master_item.update({
                "render_s": master_rendered.get("_elapsed_s"),
                "source_guid": master_guid,
                "name": "MASTER",
                "track_index": -1,
                "is_master": True,
                "stage": "master_mix_post_fx",
            })
            rendered_files = list(track_items)
            rendered_files.append(master_item)
            payload_guids = [str(item["source_guid"]) for item in rendered_files]
            if pipeline is not None:
                if not master_submitted:
                    pipeline.submit([(master_guid, str(master_path))])
                    master_submitted = True
                    if first_submit_at is None:
                        first_submit_at = time.perf_counter()
                pipeline.finish()
            # Both render paths restore render settings, selection and visible
            # transport before returning.
            render_end = time.perf_counter()
            restore_start = time.perf_counter()
            after_context = self.session.snapshot(
                include_items=True, include_fx_params=True,
                include_midi_notes=include_midi_notes,
            )
            after_digest = self.bridge.project_digest(revision.project_id)
            restore_report = self._restore_report(
                before=context, after=after_context,
                before_digest=digest, after_digest=after_digest,
                revision_before=revision, revision_after=current,
                last_render=last_render or master_rendered,
            )
            restore_end = time.perf_counter()

            if pipeline is not None:
                results_map = pipeline.wait_all()
                analysis_end = time.perf_counter()
                pipeline_stats = pipeline.stats()
                pipeline.close(wait_workers=True)
                pipeline_closed = True
                pool_end = time.perf_counter()
                missing = [guid for guid in payload_guids
                           if guid not in results_map]
                if missing:
                    raise RuntimeError(
                        "Analysis results are incomplete: "
                        + ", ".join(sorted(missing)[:8]))
                results = [results_map[guid] for guid in payload_guids]
                analysis_timing = {
                    "analysis_wall_s": analysis_end - (first_submit_at or started),
                    "analysis_pool_wall_s": pool_end - (pool_created_at or started),
                }

            batch_summary = {
                "files": list(track_items),
                "render_settings_used": (
                    dict(batch_renders[0].get("render_settings_used") or {})
                    if batch_renders else {}),
                "target_count": sum(int(rendered.get("target_count") or 0)
                                    for rendered in batch_renders),
                "_elapsed_s": round(sum(
                    float(rendered.get("_elapsed_s") or 0.0)
                    for rendered in batch_renders), 3),
                "revision_after": current.to_dict(),
                "restore_report": (last_render or {}).get("restore_report"),
            }
            overlap_timing = _overlap_timing(
                started=started,
                first_submit_at=first_submit_at,
                pool_created_at=pool_created_at,
                last_render_end=last_render_end,
                analysis_end=analysis_end,
                render_ends=render_ends,
                pipeline_stats=pipeline_stats,
                admission_times=admission_times,
                batch_plan=batch_plan,
            )
            record = self._build_record(
                capture_id=capture_id,
                created_at=created_at,
                producer=producer,
                context=context,
                digest=digest,
                revision=revision,
                revision_after=current,
                section=(start_s, end_s),
                rendered_files=rendered_files,
                workspace=workspace,
                restore_report=restore_report,
                analysis_workers=workers,
                master_attempts=master_attempts,
                timings={
                    "capture_render_s": round(render_end - started, 3),
                    "snapshot_s": round(snapshot_end - snapshot_start, 3),
                    "digest_s": round(digest_end - digest_start, 3),
                    "mute_rpc_s": round(self._profile["mute_rpc_s"], 3),
                    "mute_rpc_calls": self._profile["mute_rpc_calls"],
                    "mute_bridge_calls": self._profile["mute_bridge_calls"],
                    "mute_track_mutations": self._profile["mute_track_mutations"],
                    "render_s": round(self._profile["render_s"], 3),
                    "render_batch_s": round(self._profile.get("render_batch_s", 0.0), 3),
                    "source_activity_s": round(source_activity_s, 3),
                    "render_master_s": master_rendered.get("_elapsed_s"),
                    "render_calls": self._profile["render_calls"],
                    "render_per_track_s": dict(self._profile.get("render_times", {})),
                    "batch_render_settings": dict(
                        batch_summary.get("render_settings_used") or {}),
                    "batch_target_count": batch_summary.get("target_count"),
                    "batch_file_count": len(track_items),
                    "analysis_worker_policy": worker_plan.to_dict(),
                    "master_render_settings": dict(
                        master_rendered.get("render_settings_used") or {}),
                    "master_target_count": master_rendered.get("target_count"),
                    "master_file_count": len(master_rendered.get("files") or []),
                    "restore_verification_s": round(restore_end - restore_start, 3),
                    **overlap_timing,
                },
                analysis_results=results,
                analysis_timing=analysis_timing,
            )
            archive_start = time.perf_counter()
            archived = archive.append(
                record,
                {item["source_guid"]: item["path"] for item in rendered_files},
                extra_files=extra_master_files,
            )
            archive_end = time.perf_counter()
            # The archive append has already written the canonical record and
            # its panorama.  Keep using the in-memory sealed builder: re-reading
            # the 440-MB record here would duplicate work and memory for no
            # validation benefit.
            stored = record
            panorama_path = panorama_path_for_record(archived.record_path)
            if panorama_path.is_file():
                panorama = load_panorama(
                    panorama_path, _archived_record_binding(stored, archived))
            else:
                # Older archives may not have the sidecar; build the same
                # deterministic projection without touching the canonical record.
                panorama = build_panorama(
                    stored,
                    record_path=str(Path(archived.record_path).relative_to(
                        Path(archive.root).resolve())).replace("\\", "/"),
                )
            stored_attempts = stored.timings.get("master_attempts", [])

            final_master_validation = _final_master_validation(
                _master_validation(stored.analysis, str(master.get("guid"))),
                stored_attempts,
            )
            if not final_master_validation.get("valid"):
                raise CaptureMasterInvalid(
                    "Capture archived, but every master render attempt was silent",
                    details={
                        "status": "invalid_master",
                        "capture_id": stored.capture_id,
                        "record_path": archived.record_path,
                        "master_validation": final_master_validation,
                        "master_attempts": stored_attempts,
                    },
                )
            return {
                "ok": True,
                "capture_status": (
                    "valid_after_retry" if any(
                        not item.get("valid") for item in master_attempts)
                    else record.timings.get(
                        "capture_status", "valid")),
                "capture_id": stored.capture_id,
                "project_id": stored.project_id,
                "revision": record.revision_after,
                "time_window": record.time_window,
                "raw_capture": {
                    "capture_id": stored.capture_id,
                    "record_path": archived.record_path,
                    "integrity_sha256": archived.integrity_sha256,
                    "schema_version": stored.schema_version,
                    "raw_record_included": False,
                },
                "archive": {
                    "capture_id": archived.capture_id,
                    "record_path": archived.record_path,
                    "audio_directory": archived.audio_directory,
                    "integrity_sha256": archived.integrity_sha256,
                },
                "panorama": panorama,
                "panorama_sidecar": {
                    "path": str(panorama_path) if panorama_path.is_file() else None,
                    "stored": panorama_path.is_file(),
                    "schema_version": panorama.get("schema_version"),
                },
                "master_validation": final_master_validation,
                "master_attempts": stored_attempts,
                "timings": {
                    **record.timings,
                    "archive_s": round(archive_end - archive_start, 3),
                    "total_s": round(time.perf_counter() - started, 3),
                },
            }
        finally:
            # A failed restore must surface, never leave a silent solo state.
            try:
                current = self._restore_original_mutes(current, original_mute)
            finally:
                if pipeline is not None and not pipeline_closed:
                    try:
                        pipeline.abort()
                    except Exception:
                        pass
                if workspace is not None:
                    workspace.close()
                if record is not None and stored is None:
                    try:
                        archive.expire(record.capture_id,
                                      reason="capture_failed_before_durable_commit")
                    except Exception:
                        pass

    # ------------------------------------------------------------------ #
    # REAPER operations
    # ------------------------------------------------------------------ #
    def _set_mutes(self, revision: ProjectRevision,
                    mutes: Mapping[str, bool]) -> ProjectRevision:
        """Apply a GUID->mute map through one bridge transaction."""
        if not mutes:
            return revision
        rpc_started = time.perf_counter()
        result = self.bridge.set_track_mutes(
            project_id=revision.project_id,
            expected_revision=revision.to_dict(),
            mutes=dict(mutes),
        )
        self._profile["mute_rpc_s"] += time.perf_counter() - rpc_started
        self._profile["mute_bridge_calls"] += 1
        self._profile["mute_track_mutations"] += len(mutes)
        after = result.get("project_after")
        if not isinstance(after, Mapping):
            raise BridgeUnavailable("REAPER did not report the batched mute revision")
        for entry in result.get("mutes", []):
            if not isinstance(entry, Mapping):
                raise BridgeUnavailable("REAPER returned an invalid batched mute entry")
            guid = entry.get("guid")
            if guid in mutes and bool(entry.get("mute")) != bool(mutes[str(guid)]):
                raise BridgeUnavailable("REAPER did not apply a batched mute",
                                       details={"guid": guid, "expected": mutes[str(guid)],
                                                "actual": entry.get("mute")})
        return ProjectRevision.from_dict(dict(after))

    def _restore_original_mutes(self, current: ProjectRevision,
                                original_mute: Mapping[str, int]) -> ProjectRevision:
        """Restore the pre-capture mute map, or fail; never skip silently.

        The capture's own isolation sweep advances the project change counter,
        so a revision carried from before the sweep is stale by exactly two
        counts per touched track and the bridge rejects every restore handle
        built from it. The live revision is therefore re-read here, and only the
        mutes that actually differ are written.
        """
        current, live_mute = self._live_mute_state(current, original_mute)
        pending = {guid: value for guid, value in original_mute.items()
                   if bool(live_mute[guid]) != bool(value)}
        if pending:
            current = self._set_every_mute(current, pending, None)
        return current

    def _live_mute_state(self, fallback: ProjectRevision,
                         required: Mapping[str, int]
                         ) -> tuple[ProjectRevision, dict[str, int]]:
        """Read the live revision and GUID->mute map without mutating anything.

        Fail-closed: an unreadable snapshot, a revision that does not match the
        captured project, or a track whose mute state is missing must raise. A
        partial map would make ``live_mute[guid]`` look already restored and
        leave the project muted while the capture reports success.
        """
        try:
            context = self.session.snapshot(include_items=False,
                                            include_fx_params=False)
        except Exception as error:
            raise BridgeUnavailable(
                "Cannot read the live mute state before restoring the capture",
                details={"reason": "snapshot_failed", "error": str(error)}) from error
        if not isinstance(context, Mapping):
            raise BridgeUnavailable(
                "REAPER returned no project context before the mute restore",
                details={"reason": "context_missing"})
        revision_row = context.get("revision")
        if not isinstance(revision_row, Mapping):
            raise BridgeUnavailable(
                "REAPER returned no revision before the mute restore",
                details={"reason": "revision_missing"})
        revision = ProjectRevision.from_dict(dict(revision_row))
        if not revision.project_id or not revision.path:
            raise BridgeUnavailable(
                "REAPER returned an incomplete revision before the mute restore",
                details={"reason": "revision_incomplete",
                         "project_id": revision.project_id, "path": revision.path})
        if revision.project_id != fallback.project_id:
            raise BridgeUnavailable(
                "The active project changed before the mute restore",
                details={"reason": "project_mismatch",
                         "expected": fallback.project_id,
                         "current": revision.project_id})
        mutes: dict[str, int] = {}
        for track in context.get("tracks") or []:
            if not isinstance(track, Mapping):
                continue
            guid = track.get("guid")
            if not isinstance(guid, str) or not guid:
                continue
            if "mute" not in track:
                raise BridgeUnavailable(
                    "A track has no mute state before the mute restore",
                    details={"reason": "mute_missing", "guid": guid})
            mutes[guid] = 1 if track.get("mute") else 0
        missing = sorted(set(required) - set(mutes))
        if missing:
            raise BridgeUnavailable(
                "The mute state of a captured track could not be read",
                details={"reason": "mute_state_incomplete",
                         "missing_count": len(missing), "guids": missing[:8]})
        return revision, mutes

    def _set_mute(self, revision: ProjectRevision, guid: str, value: int | None) -> ProjectRevision:
        if value is None:
            raise ValidationError("mute restore requires an explicit value")
        handle = ReaperHandle(
            kind="MediaTrack",
            project_id=revision.project_id,
            change_count=revision.change_count,
            stable_id=guid,
        )
        rpc_started = time.perf_counter()
        result = self.bridge.call_api(
            "SetMediaTrackInfo_Value", handle, "B_MUTE", float(value)
        )
        self._profile["mute_rpc_s"] += time.perf_counter() - rpc_started
        self._profile["mute_rpc_calls"] += 1
        self._profile["mute_bridge_calls"] += 1
        self._profile["mute_track_mutations"] += 1
        after = result.project_after
        if not isinstance(after, Mapping):
            raise BridgeUnavailable("REAPER did not report the mute revision")
        return ProjectRevision.from_dict(dict(after))

    def _set_every_mute(self, revision: ProjectRevision, original: Mapping[str, int],
                        value: int | None) -> ProjectRevision:
        if value is not None:
            for guid in original:
                revision = self._set_mute(revision, guid, value)
            return revision
        for guid, mute in original.items():
            revision = self._set_mute(revision, guid, mute)
        return revision

    @staticmethod
    def _routing_graph(tracks: list[Any], digest: Mapping[str, Any]) -> dict[str, list[str]]:
        rows = digest.get("fields", {}).get("tracks", [])
        graph: dict[str, list[str]] = {}
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                guid = row.get("guid")
                if not isinstance(guid, str):
                    continue
                sends = row.get("sends") or []
                graph[guid] = [str(send.get("dest_guid")) for send in sends
                               if isinstance(send, Mapping) and send.get("dest_guid")]
        for track in tracks:
            if isinstance(track, Mapping) and isinstance(track.get("guid"), str):
                graph.setdefault(track["guid"], [])
        return graph

    @staticmethod
    def _isolation_set(target_guid: str, tracks: list[Any],
                       graph: Mapping[str, list[str]]) -> list[str]:
        by_guid = {str(t["guid"]): t for t in tracks
                   if isinstance(t, Mapping) and t.get("guid")}
        target = by_guid.get(target_guid)
        if target is None:
            raise BridgeUnavailable("Capture target GUID is not in the project map")
        has_items = bool(target.get("items"))
        if has_items:
            # Source/audio track: keep the target and its destination buses open.
            selected = {target_guid}
            pending = [target_guid]
            while pending:
                guid = pending.pop()
                for dest in graph.get(guid, []):
                    if dest in by_guid and dest not in selected:
                        selected.add(dest)
                        pending.append(dest)
            return sorted(selected)
        # Aux bus: its input is the reverse closure of sources that can reach it.
        selected = {target_guid}
        pending = [target_guid]
        while pending:
            dest = pending.pop()
            for source, destinations in graph.items():
                if dest in destinations and source not in selected:
                    selected.add(source)
                    pending.append(source)
        return sorted(selected)

    def _check_rendered_wav(self, path: Path, rendered: Mapping[str, Any]) -> Any:
        """Prove one rendered target is a readable WAV with the requested shape.

        This runs after the render RPC returned, so the writer is closed.  The
        caller hashes the file separately; this check is header-only and never
        retains decoded samples.
        """
        try:
            info = sf.info(str(path))
        except Exception as exc:
            raise BridgeUnavailable(
                "A rendered target is not a readable WAV",
                details={"path": str(path), "reason": str(exc)}) from exc
        if info.frames <= 0 or info.samplerate <= 0:
            raise BridgeUnavailable(
                "A rendered target contains no samples",
                details={"path": str(path), "frames": int(info.frames),
                         "sample_rate": int(info.samplerate)})
        channels = rendered.get("channels")
        if isinstance(channels, int) and not isinstance(channels, bool):
            if int(info.channels) != int(channels):
                raise BridgeUnavailable(
                    "A rendered target has the wrong channel count",
                    details={"path": str(path), "expected": channels,
                             "actual": int(info.channels)})
        rate = rendered.get("sample_rate")
        if (isinstance(rate, (int, float)) and not isinstance(rate, bool) and
                float(rate) > 0 and int(info.samplerate) != int(rate)):
            raise BridgeUnavailable(
                "A rendered target has the wrong sample rate",
                details={"path": str(path), "expected": int(rate),
                         "actual": int(info.samplerate)})
        return info

    def _admit_track_batch(self, rendered: Mapping[str, Any],
                           expected_guids: list[str],
                           tracks_by_guid: Mapping[str, Mapping[str, Any]],
                           workspace: AudioArtifactWorkspace) -> list[dict[str, Any]]:
        """Admit one closed batch: exact GUID set, header and content hashes."""
        admit_started = time.perf_counter()
        files = rendered.get("files")
        if not isinstance(files, list) or not files:
            raise BridgeUnavailable("A render batch returned no files")
        by_guid: dict[str, Mapping[str, Any]] = {}
        for file in files:
            if not isinstance(file, Mapping):
                raise BridgeUnavailable("A render batch returned an invalid file row")
            if file.get("is_master"):
                raise BridgeUnavailable(
                    "A track batch unexpectedly returned a master file")
            guid = file.get("source_guid")
            if not isinstance(guid, str) or not guid:
                raise BridgeUnavailable("A render batch file has no track GUID")
            if guid in by_guid:
                raise BridgeUnavailable(
                    "A render batch returned a duplicate track GUID",
                    details={"source_guid": guid})
            by_guid[guid] = file
        expected = set(expected_guids)
        if set(by_guid) != expected:
            raise BridgeUnavailable(
                "A render batch did not return exactly the planned tracks",
                details={"missing": sorted(expected - set(by_guid)),
                         "extra": sorted(set(by_guid) - expected)})
        paths: list[str | os.PathLike[str]] = []
        for guid in expected_guids:
            path = by_guid[guid].get("path")
            declared = by_guid[guid].get("bytes")
            if not isinstance(path, str):
                raise BridgeUnavailable(
                    "A render batch file has no path", details={"source_guid": guid})
            try:
                actual = Path(path).stat().st_size
            except OSError as exc:
                raise BridgeUnavailable(
                    "A rendered batch file is missing",
                    details={"source_guid": guid, "path": path,
                             "reason": str(exc)}) from exc
            if not isinstance(declared, int) or isinstance(declared, bool):
                raise BridgeUnavailable(
                    "A render batch file has no declared byte size",
                    details={"source_guid": guid, "path": path})
            if int(declared) != int(actual):
                raise BridgeUnavailable(
                    "A rendered batch file changed size before admission",
                    details={"source_guid": guid, "declared": declared,
                             "actual": int(actual)})
            if actual <= 44:
                raise BridgeUnavailable(
                    "A rendered batch file is empty",
                    details={"source_guid": guid, "bytes": int(actual)})
            paths.append(path)
        artifacts = workspace.register_assets(paths)
        items: list[dict[str, Any]] = []
        for guid, artifact in zip(expected_guids, artifacts, strict=True):
            file = by_guid[guid]
            info = self._check_rendered_wav(Path(artifact.path), rendered)
            if int(artifact.bytes) != int(file.get("bytes", -1)):
                raise BridgeUnavailable(
                    "A rendered batch file changed size during hashing",
                    details={"source_guid": guid,
                             "registered": int(artifact.bytes),
                             "declared": file.get("bytes")})
            if Path(artifact.path).stat().st_size != int(artifact.bytes):
                raise BridgeUnavailable(
                    "A rendered batch file is still growing after hashing",
                    details={"source_guid": guid, "path": str(artifact.path)})
            track = tracks_by_guid.get(guid)
            if track is None:
                raise BridgeUnavailable(
                    "A rendered track is not in the project snapshot",
                    details={"source_guid": guid})
            item = dict(file)
            item.update({
                "source_guid": guid,
                "name": str(track.get("name") or f"track {track.get('index')}"),
                "track_index": int(track.get("index", -1)),
                "is_master": False,
                "stage": "selected_track_via_master_parallel_batch_post_fx",
                "soloed_guid": None,
                "batch_render_s": rendered.get("_elapsed_s"),
                "sha256": artifact.sha256,
                "bytes": int(artifact.bytes),
                "frames": int(info.frames),
                "sample_rate": int(info.samplerate),
                "channels": int(info.channels),
            })
            items.append(item)
        self._profile["admit_s"] += time.perf_counter() - admit_started
        return items

    def _render_batch(self, revision: ProjectRevision,
                      workspace: AudioArtifactWorkspace, job_id: str,
                      track_count: int,
                      start_s: float | None, end_s: float | None,
                      channels: str, sample_rate: int | None,
                      tail_ms: int,
                      track_guids: list[str] | None = None) -> dict[str, Any]:
        render_started = time.perf_counter()
        result = self.bridge.capture_render(
            expected_revision=revision.to_dict(),
            output_dir=str(workspace.directory),
            job_id=job_id,
            expected_track_count=track_count,
            timeout_s=600.0,
            section_start_s=start_s,
            section_end_s=end_s,
            master_only=False,
            selected_tracks_via_master=True,
            parallel_via_master=True,
            track_guids=track_guids,
            render_settings=128,
            channels=channels,
            sample_rate=sample_rate,
            tail_ms=tail_ms,
        )
        elapsed = time.perf_counter() - render_started
        self._profile["render_s"] += elapsed
        self._profile["render_batch_s"] = self._profile.get("render_batch_s", 0.0) + elapsed
        self._profile["render_calls"] += 1
        result["_elapsed_s"] = round(elapsed, 3)
        return result

    def _render_master_only(self, revision: ProjectRevision, workspace: AudioArtifactWorkspace,
                            job_id: str, start_s: float | None, end_s: float | None,
                            channels: str, sample_rate: int | None,
                            tail_ms: int) -> dict[str, Any]:
        render_started = time.perf_counter()
        result = self.bridge.capture_render(
            expected_revision=revision.to_dict(),
            output_dir=str(workspace.directory),
            job_id=job_id,
            expected_track_count=0,
            timeout_s=600.0,
            section_start_s=start_s,
            section_end_s=end_s,
            master_only=True,
            channels=channels,
            sample_rate=sample_rate,
            tail_ms=tail_ms,
        )
        elapsed = time.perf_counter() - render_started
        self._profile["render_s"] += elapsed
        self._profile["render_calls"] += 1
        self._profile.setdefault("render_times", {})[job_id] = round(elapsed, 3)
        result["_elapsed_s"] = round(elapsed, 3)
        return result

    def _render_master_with_gate(self, *, revision: ProjectRevision,
                                 workspace: AudioArtifactWorkspace,
                                 master_guid: str,
                                 start_s: float | None, end_s: float | None,
                                 channels: str, sample_rate: int | None,
                                 tail_ms: int, project_length_s: float,
                                 source_activity: Mapping[str, Any],
                                 master_state: Mapping[str, Any]
                                 ) -> tuple[dict[str, Any], dict[str, Any],
                                            list[dict[str, Any]], dict[str, str]]:
        """Render master attempts, preserve failed evidence and select the gate winner."""
        attempts: list[dict[str, Any]] = []
        extra_files: dict[str, str] = {}
        selected_item: dict[str, Any] | None = None
        selected_render: dict[str, Any] | None = None
        last_item: dict[str, Any] | None = None
        last_render: dict[str, Any] | None = None
        for attempt_index in range(1, MASTER_RENDER_MAX_ATTEMPTS + 1):
            rendered = self._render_master_only(
                revision, workspace, f"{workspace.job_id}-master-{attempt_index}",
                start_s, end_s, channels, sample_rate, tail_ms,
            )
            item = self._one_file(rendered, workspace)
            path = Path(str(item["path"]))
            sha = _sha256_file(path)
            validation = _wave_master_validation(path, master_guid)
            classification = _classify_master_silence(
                validation, source_activity, master_state)
            gate_valid = not validation["silent"] or classification["expected_silence"]
            entry = {
                "attempt": attempt_index,
                "status": ("expected_silence" if classification["expected_silence"]
                           else validation["status"]),
                "silent": validation["silent"],
                "valid": gate_valid,
                "expected_silence": classification["expected_silence"],
                "unexpected_silence": classification["unexpected_silence"],
                "silence_reason": classification["silence_reason"],
                "source_activity": {
                    "audible_count": source_activity.get("audible_count"),
                    "audible_guids": list(source_activity.get("audible_guids") or []),
                    "all_silent": source_activity.get("all_silent"),
                },
                "peak_dbfs": validation["peak_dbfs"],
                "rms_dbfs": validation["rms_dbfs"],
                "frames": validation["frames"],
                "sample_rate": validation["sample_rate"],
                "channels": validation["channels"],
                "sha256": sha,
                "bytes": path.stat().st_size,
                "archive_audio": f"audio/sha256/{sha}.wav",
                "render_s": rendered.get("_elapsed_s"),
                "render_settings": dict(rendered.get("render_settings_used") or {}),
                "time_window": {
                    "project_start_s": (start_s if start_s is not None else 0.0),
                    "project_end_s": (end_s if end_s is not None else project_length_s),
                },
                "selected": gate_valid,
            }
            if validation["silent"] and not classification["expected_silence"]:
                # Keep only unexpected failed renders as evidence; intentional
                # silence is a valid result, not a failure.
                extra_files[sha] = str(path)
            attempts.append(entry)
            revision = ProjectRevision.from_dict(rendered["revision_after"])
            last_item, last_render = item, rendered
            if gate_valid:
                selected_item, selected_render = item, rendered
                break
        if selected_item is None or selected_render is None:
            assert last_item is not None and last_render is not None
            selected_item, selected_render = last_item, last_render
            for entry in attempts:
                entry["selected"] = entry["attempt"] == attempts[-1]["attempt"]
        return selected_item, selected_render, attempts, extra_files

    @staticmethod
    def _one_file(rendered: Mapping[str, Any], workspace: AudioArtifactWorkspace) -> dict[str, Any]:
        files = rendered.get("files")
        if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
            raise BridgeUnavailable("A solo render did not return exactly one file")
        item = dict(files[0])
        path = item.get("path")
        if not isinstance(path, str):
            raise BridgeUnavailable("A solo render has no file path")
        workspace.register_asset(path)
        return item

    # ------------------------------------------------------------------ #
    # record construction
    # ------------------------------------------------------------------ #
    @staticmethod
    def _restore_report(*, before: Mapping[str, Any], after: Mapping[str, Any],
                        before_digest: Mapping[str, Any], after_digest: Mapping[str, Any],
                        revision_before: ProjectRevision,
                        revision_after: ProjectRevision,
                        last_render: Mapping[str, Any]) -> dict[str, Any]:
        before_tracks = {str(t.get("guid")): t for t in before.get("tracks", [])
                         if isinstance(t, Mapping) and t.get("guid")}
        after_tracks = {str(t.get("guid")): t for t in after.get("tracks", [])
                        if isinstance(t, Mapping) and t.get("guid")}
        def field_map(rows, key):
            return {guid: row.get(key) for guid, row in rows.items()}
        before_items = {str(item.get("guid")) for track in before_tracks.values()
                        for item in track.get("items", []) if isinstance(item, Mapping)}
        after_items = {str(item.get("guid")) for track in after_tracks.values()
                       for item in track.get("items", []) if isinstance(item, Mapping)}
        before_fields = before_digest.get("fields") if isinstance(before_digest, Mapping) else None
        after_fields = after_digest.get("fields") if isinstance(after_digest, Mapping) else None
        try:
            digest_equal = json.dumps(before_fields, sort_keys=True,
                                      ensure_ascii=False, allow_nan=False) == json.dumps(
                after_fields, sort_keys=True, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            digest_equal = False
        restore = last_render.get("restore_report")
        before_transport = before.get("transport") or {}
        if not isinstance(before_transport, Mapping):
            before_transport = {}
        after_transport = after.get("transport") or {}
        if not isinstance(after_transport, Mapping):
            after_transport = {}
        before_cursor = before_transport.get("edit_cursor_s",
                                             before_transport.get("cursor_s"))
        after_cursor = after_transport.get("edit_cursor_s",
                                           after_transport.get("cursor_s"))
        before_play_state = before_transport.get("play_state")
        after_play_state = after_transport.get("play_state")

        def same_number(left: Any, right: Any, tolerance: float = 1e-6) -> bool:
            return (isinstance(left, (int, float)) and not isinstance(left, bool) and
                    isinstance(right, (int, float)) and not isinstance(right, bool) and
                    math.isfinite(float(left)) and math.isfinite(float(right)) and
                    abs(float(left) - float(right)) <= tolerance)

        transport_position_restored = same_number(before_cursor, after_cursor)
        transport_play_state_restored = (
            before_play_state is not None and before_play_state == after_play_state)
        stopped_before_and_after = before_play_state == 0 and after_play_state == 0
        play_position_before = before_transport.get("play_position_s")
        play_position_after = after_transport.get("play_position_s")
        play_position_changed = not same_number(play_position_before, play_position_after)
        before_selection = before.get("selection") or {}
        if not isinstance(before_selection, Mapping):
            before_selection = {}
        after_selection = after.get("selection") or {}
        if not isinstance(after_selection, Mapping):
            after_selection = {}
        render_restore = dict(restore) if isinstance(restore, Mapping) else {}
        return {
            "initial_change_count": revision_before.change_count,
            "final_change_count": revision_after.change_count,
            "counter_delta": revision_after.change_count - revision_before.change_count,
            "counter_note": ("REAPER increments its monotonic change counter for "
                             "each temporary mute/render-setting transaction; the "
                             "counter is not a content fingerprint."),
            "track_count_before": len(before_tracks),
            "track_count_after": len(after_tracks),
            "item_guids_before": sorted(before_items),
            "item_guids_after": sorted(after_items),
            "no_media_items_added": before_items == after_items,
            "mute_restored": field_map(before_tracks, "mute") == field_map(after_tracks, "mute"),
            "solo_restored": field_map(before_tracks, "solo") == field_map(after_tracks, "solo"),
            "volume_pan_restored": (
                field_map(before_tracks, "volume") == field_map(after_tracks, "volume") and
                field_map(before_tracks, "pan") == field_map(after_tracks, "pan")),
            "selection_restored": before.get("selection") == after.get("selection"),
            "time_selection_restored": (
                before_selection.get("time_start_s") == after_selection.get("time_start_s") and
                before_selection.get("time_end_s") == after_selection.get("time_end_s")),
            "transport_restored": (
                transport_position_restored and transport_play_state_restored),
            "transport_restore_basis": "edit_cursor_s+play_state",
            "transport_position_before_s": before_cursor,
            "transport_position_after_s": after_cursor,
            "transport_position_restored": transport_position_restored,
            "transport_play_state_restored": transport_play_state_restored,
            "transport_before": dict(before_transport),
            "transport_after": dict(after_transport),
            "play_position_before_s": play_position_before,
            "play_position_observed_s": play_position_after,
            "play_position_matches": (
                None if stopped_before_and_after else
                not play_position_changed),
            "play_position_restorable": False,
            "play_position_semantics": (
                "observed_playback_position_not_project_cursor; "
                "not used as a restore failure while transport is stopped"),
            "play_position_observed_changed": play_position_changed,
            "master_restored": before.get("master") == after.get("master"),
            "digest_fields_equal": digest_equal,
            "render_restore_report": render_restore,
            "dirty_before": render_restore.get("dirty_before"),
            "dirty_after": render_restore.get("dirty_after"),
            "dirty_restored": (
                render_restore.get("dirty_before") == render_restore.get("dirty_after")),
        }

    def _build_record(self, *, capture_id: str, created_at: str, producer: str,
                      context: Mapping[str, Any], digest: Mapping[str, Any],
                      revision: ProjectRevision, revision_after: ProjectRevision,
                      section: tuple[float | None, float | None],
                      rendered_files: list[dict[str, Any]],
                      workspace: AudioArtifactWorkspace,
                      restore_report: Mapping[str, Any],
                      analysis_workers: int | None = None,
                      master_attempts: list[dict[str, Any]] | None = None,
                      timings: Mapping[str, Any],
                      analysis_results: list[dict[str, Any]] | None = None,
                      analysis_timing: Mapping[str, Any] | None = None) -> FullCaptureRecord:
        analysis_started = time.perf_counter()
        tracks = list(context.get("tracks") or [])
        master = dict(context.get("master") or {})
        items: list[Any] = []
        takes: list[Any] = []
        midi: list[Any] = []
        fx: list[Any] = []
        for track in tracks:
            items.extend(track.get("items") or [])
            fx.extend(track.get("fx") or [])
            for item in track.get("items") or []:
                take = item.get("active_take") if isinstance(item, Mapping) else None
                if isinstance(take, Mapping):
                    takes.append(take)
                    if take.get("midi") is not None:
                        midi.append({"take_guid": take.get("guid"),
                                     "events": take.get("midi")})
        fx.extend(master.get("fx") or [])
        digest_fields = digest.get("fields")
        fields: Mapping[str, Any] = digest_fields if isinstance(digest_fields, Mapping) else {}
        automation = fields.get("automation") or []
        envelopes = fields.get("envelopes") or automation
        sends = fields.get("sends") or []
        map_coverage = {name: True for name in REQUIRED_MAP_KEYS - {"coverage"}}
        project_map = {
            "tracks": tracks,
            "master": master,
            "items": items,
            "takes": takes,
            "midi": midi,
            "fx": fx,
            "routing": sends,
            "envelopes": envelopes,
            "automation": automation,
            "tempo": context.get("tempo") or {},
            "time_signatures": (context.get("tempo") or {}).get("markers") or [],
            "markers": context.get("markers") or [],
            "regions": context.get("regions") or [],
            "selection": context.get("selection") or {},
            "transport": context.get("transport") or {},
            "coverage": map_coverage,
            "digest": dict(digest),
        }
        audio: list[dict[str, Any]] = []
        analysis: dict[str, Any] = {}
        payloads = [(str(item["source_guid"]), str(item["path"]))
                    for item in rendered_files]
        window_s = None
        if section[0] is not None and section[1] is not None:
            window_s = float(section[1]) - float(section[0])
        else:
            length_s = float(context.get("length_s") or 0.0)
            window_s = length_s if length_s > 0 else None
        if analysis_results is None:
            worker_plan = resolve_analysis_workers(
                asset_count=len(payloads), window_s=window_s,
                requested=analysis_workers)
            workers = worker_plan.workers
            pool_started = time.perf_counter()
            results = _analyze_rendered_files(payloads, workers)
            pool_elapsed = time.perf_counter() - pool_started
        else:
            results = list(analysis_results)
            if analysis_workers is not None:
                workers = int(analysis_workers)
            else:
                workers = resolve_analysis_workers(
                    asset_count=len(payloads), window_s=window_s).workers
            pool_elapsed = (float(analysis_timing.get("analysis_pool_wall_s", 0.0))
                            if isinstance(analysis_timing, Mapping) else 0.0)
        by_guid = {str(result["source_guid"]): result for result in results}
        if (len(by_guid) != len(payloads) or
                set(by_guid) != {guid for guid, _ in payloads}):
            raise RuntimeError("DSP analysis returned a duplicate or missing source GUID")
        for item in rendered_files:
            path = Path(str(item["path"]))
            guid = str(item["source_guid"])
            result = by_guid.get(guid)
            if result is None:
                raise RuntimeError(f"DSP analysis returned no result for {guid}")
            self._profile["decode_s"] += float(result.get("decode_s", 0.0))
            self._profile["dsp_s"] += float(result.get("dsp_s", 0.0))
            report = result["analysis"]
            audio.append({
                "asset_id": f"ast_sha256_{item.get('sha256') or _sha256_file(path)}",
                "source_guid": guid,
                "name": str(item.get("name") or ""),
                "is_master": bool(item.get("is_master")),
                "stage": str(item.get("stage") or ""),
                "capture_method": "native_render",
                "sample_rate": int(result["sample_rate"]),
                "channels": int(result["channels"]),
                "frames": int(result["frames"]),
                "duration_s": float(result["duration_s"]),
                "bytes": path.stat().st_size,
                "sha256": str(item.get("sha256") or _sha256_file(path)),
                "signal_window": {
                    "project_start_s": (section[0] if section[0] is not None else 0.0),
                    "project_end_s": (section[1] if section[1] is not None else float(context.get("length_s") or 0.0)),
                    "file_start_s": 0.0,
                    "file_end_s": float(result["duration_s"]),
                },
                "capture_revision": revision.to_dict(),
                "status": "leased",
                "lease_id": workspace.job_id,
                "render_s": item.get("render_s"),
                "soloed_guid": item.get("soloed_guid"),
            })
            analysis[guid] = report
        coverage = {name: True for name in REQUIRED_MAP_KEYS - {"coverage"} | {"audio", "analysis"}}
        time_window = {
            "project_start_s": 0.0 if section[0] is None else float(section[0]),
            "project_end_s": float(context.get("length_s") or 0.0) if section[1] is None else float(section[1]),
            "file_start_s": 0.0,
            "file_end_s": max(float(item["duration_s"]) for item in audio),
            "timeline": "project_seconds",
        }
        render_started = time.perf_counter()
        render_contract = {
            "capture": "native_render",
            "track_stage": "selected_track_via_master_parallel_batch_post_fx",
            "master_stage": "master_mix_post_fx",
            "track_signal_includes_master_processing": True,
        }
        try:
            render_manifest = build_render_manifest(
                project_id=revision.project_id,
                digest=digest,
                sources=[
                    {
                        "source_guid": str(asset.get("source_guid") or ""),
                        "scope": "master" if asset.get("is_master") else "source",
                        "stage": str(asset.get("stage") or ""),
                        "channels": asset.get("channels"),
                        "sample_rate": asset.get("sample_rate"),
                    }
                    for asset in audio
                ],
                time_window=time_window,
                render_contract=render_contract,
            )
        except Exception as exc:  # noqa: BLE001 - a coarse digest is a coverage fact
            # A capture whose digest cannot prove the full dependency surface is
            # still archived, but its audio identity is explicitly unknown and
            # every later delta invalidates it conservatively.
            render_manifest = {
                "schema": "reaper-workbench.render-manifest/1",
                "version": 1,
                "project_id": revision.project_id,
                "window": {
                    "project_start_s": time_window["project_start_s"],
                    "project_end_s": time_window["project_end_s"],
                },
                "render_contract": render_contract,
                "sources": [],
                "complete": False,
                "cross_revision_authoritative": False,
                "unavailable": {
                    "reason": "digest_does_not_support_render_identity",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                "unverified": [],
            }
        render_elapsed_s = time.perf_counter() - render_started
        keys_by_guid = {
            str(row.get("source_guid")): row
            for row in render_manifest.get("sources") or []
            if isinstance(row, Mapping) and row.get("source_guid")
        }
        for asset in audio:
            row = keys_by_guid.get(str(asset.get("source_guid") or ""))
            if row is not None:
                asset["render_key"] = row.get("render_key")
                asset["render_key_complete"] = bool(row.get("complete"))
                asset["render_dependencies"] = {
                    "manifest_schema": render_manifest.get("schema"),
                    "dependency_guids": list(row.get("dependency_guids") or []),
                    "components_sha256": dict(row.get("components") or {}),
                    "unknown": list(row.get("unknown") or []),
                    "unverified": list(render_manifest.get("unverified") or []),
                }
        project_map["render_manifest"] = render_manifest
        project_map["digest_facts"] = digest_facts(digest)
        relation_started = time.perf_counter()
        relations = build_relation_domain({
            "capture_id": capture_id,
            "project_id": revision.project_id,
            "project_path": revision.path,
            "revision_before": revision.to_dict(),
            "revision_after": revision_after.to_dict(),
            "time_window": time_window,
            "project_map": project_map,
            "audio": audio,
            "analysis": analysis,
        })
        relations_elapsed_s = time.perf_counter() - relation_started
        timings = dict(timings)
        timings["render_manifest_s"] = round(render_elapsed_s, 3)
        timings["render_manifest"] = {
            "schema": render_manifest.get("schema"),
            "complete": bool(render_manifest.get("complete")),
            "sources": len(render_manifest.get("sources") or []),
        }
        timings["relations_s"] = round(relations_elapsed_s, 3)
        timings["relations"] = {
            "schema_version": relations.get("schema_version"),
            "complete": bool(relations.get("complete")),
            "pair_table": (
                (relations.get("evaluations") or {}).get("band_dominance_pairs") or {}
            ).get("evaluated_pairs", 0),
        }
        if isinstance(analysis_timing, Mapping):
            analysis_wall_s = float(analysis_timing.get(
                "analysis_wall_s", time.perf_counter() - analysis_started))
        else:
            analysis_wall_s = time.perf_counter() - analysis_started
        timings.update(_analysis_timing_fields(
            analysis_wall_s=analysis_wall_s,
            analysis_pool_wall_s=pool_elapsed,
            dsp_worker_elapsed_sum_s=self._profile["dsp_s"],
            wav_decode_worker_elapsed_sum_s=self._profile["decode_s"],
        ))
        timings["analysis_workers"] = workers
        timings["analysis_engine"] = (
            "serial" if workers <= 1 else "spawn_process_pool")
        timings["analysis_scheduling"] = (
            "render_analysis_overlap" if isinstance(analysis_timing, Mapping)
            else "render_then_analysis")
        timings["analysis_window"] = (
            "first_admitted_payload_to_last_result"
            if isinstance(analysis_timing, Mapping)
            else "analysis_call_start_to_last_result")
        timings["master_attempts"] = list(master_attempts or [])
        master_validation = _final_master_validation(
            _master_validation(analysis, str(master.get("guid"))),
            master_attempts,
        )
        timings["master_validation"] = master_validation
        timings["capture_status"] = (
            "valid_expected_silence" if master_validation.get("expected_silence")
            else "valid" if master_validation.get("valid")
            else "invalid_master")
        return FullCaptureRecord(
            capture_id=capture_id,
            project_id=revision.project_id,
            project_path=revision.path,
            revision_before=revision.to_dict(),
            revision_after=revision_after.to_dict(),
            time_window=time_window,
            project_map=project_map,
            audio=audio,
            analysis=analysis,
            coverage=coverage,
            missing=[],
            created_at=created_at,
            producer=producer,
            restore_report=dict(restore_report),
            timings=dict(timings),
            relations=relations,
        )


def _sha256_file(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["FullCaptureService"]
