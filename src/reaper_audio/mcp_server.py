"""LLM-oriented MCP interface for the native REAPER audio library.

Run with ``python -m reaper_audio.mcp_server`` using stdio. The server speaks
only to the resident ReaScript bridge; it does not inspect or modify REAPER
process memory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass, replace
from functools import wraps
from math import isfinite
from pathlib import Path
from typing import Any, NotRequired, TypedDict
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from pydantic import StrictInt

from .analysis.artifacts import cleanup_audio_job, cleanup_stale_audio_jobs
from .analysis.audio import load_audio
from .analysis.bundle import analyze_bundle
from .analysis.edit_map_report import (build_edit_map_from_report,
                                       tempo_points_from_report)
from .analysis.mix_evidence import build_mix_evidence
from .analysis.metrics import measure_audio
from .daw.session import EditOperation, EditPlan, ProjectRevision, ProjectSession
from .daw.full_capture import FullCaptureService
from .context.full_capture import CaptureArchive, FullCaptureRecord
from .context.lifecycle import PanoramaLifecycle, document_hash
from .context.panorama import (DEFAULT_MAX_BYTES, DEFAULT_MAX_TOKENS,
                               PANORAMA_SCHEMA_VERSION, build_panorama, load_panorama,
                               panorama_path_for_record)
from .context.panorama_view import DEFAULT_RESPONSE_BYTES, build_view
from .context.revision import digest_evidence
from .context.identity import identity_from_mapping
from .errors import (BridgeUnavailable, PanoramaBindingError,
                     PanoramaCapacityError, ReaperAudioError, StaleContext,
                     ValidationError)
from .models import AudioBuffer, MeasurementReport, MetricValue, Section
from .reaper.api_catalog import get_api_catalog
from .daw.checkpoint import checkpoint_root, save_checkpoint
from .reaper.bridge_client import ReaperBridge
from .reaper.bridge_lifecycle import (
    activate_bridge,
    bridge_script_path,
    script_build,
)
from .reaper.handles import ReaperHandle

_LOG = logging.getLogger("reaper_audio.mcp")
_ANALYSIS_TTL_S = 60 * 60
_PLAN_TTL_S = 10 * 60
_MAX_ANALYSES_PER_ROLE = 1
_MAX_PLAN_CACHE = 128
_MAX_REPORT_BYTES = 32 * 1024 * 1024
_MAX_AUDIO_BYTES = 512 * 1024 * 1024
_TRANSACTION_OPERATIONS = (
    "track.set", "track.create", "track.delete", "track.reorder",
    "item.set", "item.create", "item.delete", "item.split", "item.duplicate",
    "take.set", "midi.create", "fx.set_enabled", "fx.set_parameter",
    "fx.insert", "fx.delete", "fx.set_preset", "fx.move", "fx.offline",
    "send.set", "send.create", "send.delete", "marker.create",
    "region.delete", "envelope.set", "tempo.set",
)
_AUDIO_ROOT = Path(tempfile.gettempdir()) / "reaper-audio-library" / "mcp-analysis"
_LOCK = threading.RLock()
_analysis_records: dict[str, dict[str, Any]] = {}
_analysis_roles: dict[tuple[str, str], str] = {}
_edit_plans: dict[str, dict[str, Any]] = {}
_PANORAMA_LIFECYCLES: dict[str, PanoramaLifecycle] = {}
_LAST_PANORAMA_ROOT: str | None = None
_API_GATE_BYPASS = threading.local()



def _focus_reaper_window() -> bool:
    """Bring REAPER to the foreground before a native render.

    REAPER may close media files while its main window is inactive; the bridge
    then reads zero-length sources and refuses to render silence.  Focusing the
    host reopens them.  Best effort: never raises, never blocks a render.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _collect(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if "REAPER" in buffer.value:
                    found.append(hwnd)
            return True

        user32.EnumWindows(_collect, 0)
        for hwnd in found:
            user32.ShowWindow(hwnd, 9)
            if user32.SetForegroundWindow(hwnd):
                time.sleep(0.35)
                return True
    except Exception:
        return False
    return bool(found)

def _panorama_lifecycle(archive_root: str | None = None,
                        page_bytes: int | None = None) -> PanoramaLifecycle:
    """Resolve the lifecycle store for an archive; keep one per absolute root."""
    global _LAST_PANORAMA_ROOT
    root = str(Path(archive_root or _LAST_PANORAMA_ROOT or
                    os.environ.get("REAPER_CAPTURE_ARCHIVE", "capture-archive")).resolve())
    lifecycle = _PANORAMA_LIFECYCLES.get(root)
    if lifecycle is None or (page_bytes is not None and lifecycle.page_bytes != page_bytes):
        lifecycle = PanoramaLifecycle(root, page_bytes=page_bytes or 24_576)
        _PANORAMA_LIFECYCLES[root] = lifecycle
    _LAST_PANORAMA_ROOT = root
    return lifecycle


def _require_panorama_receipt(receipt: str | None, project_id: str,
                              expected_revision: Mapping[str, Any] | None,
                              archive_root: str | None = None,
                              expected_fact_root: str | None = None,
                              *, require_audio_fresh: bool = False,
                              expected_capture_id: str | None = None) -> dict[str, Any]:
    """Fail closed unless a complete, revision-matched panorama receipt exists."""
    if not receipt:
        raise ValidationError(
            "A complete panorama is required before any project mutation",
            details={"next_action": ("call reaper_panorama_open, fetch every page with "
                                     "reaper_panorama_page, then reaper_panorama_ack with "
                                     "all page hashes in order; edits use the receipt's "
                                     "context_token"),
                     "gate": "panorama_completion"})
    lifecycle = _panorama_lifecycle(archive_root)
    return lifecycle.require_receipt(receipt, project_id, expected_revision,
                                     expected_fact_root=expected_fact_root,
                                     require_audio_fresh=require_audio_fresh,
                                     expected_capture_id=expected_capture_id)


def _require_interpretation_receipt(receipt: str | None, project_id: str,
                                    expected_revision: Mapping[str, Any] | None,
                                    archive_root: str | None = None,
                                    expected_fact_root: str | None = None,
                                    *,
                                    expected_capture_id: str | None = None) -> dict[str, Any]:
    """Gate model-facing interpretation, not the bootstrap capture itself.

    The first full capture creates the evidence document and is deliberately
    allowed without a receipt. Every project-target analysis/interpretation
    call that follows must present one complete, delivered receipt for that
    same project, revision and fact root *and* its audio evidence must still be
    current. A structural delta can authorize edits but never audio
    interpretation: only a fresh capture does that. The server cannot observe
    private model reasoning; this gates tool access, not thought.

    ``expected_capture_id`` binds the receipt to the capture the evidence was
    measured from. Without it a receipt is checked against the latest capture
    in the archive, which invalidates an older analysis as soon as any newer
    capture is written, even though its revision and fact root still match.
    """
    if not receipt:
        raise ValidationError(
            "A complete, delivered panorama is required before interpreting "
            "project audio or project measurements",
            details={"next_action": ("check reaper_panorama_status first; reuse and open a complete capture "
                                     "for this exact revision if one exists, otherwise capture once, "
                                     "open it, fetch every page, and acknowledge all hashes"),
                     "gate": "panorama_interpretation"})
    return _require_panorama_receipt(receipt, project_id, expected_revision,
                                     archive_root, expected_fact_root,
                                     require_audio_fresh=True,
                                     expected_capture_id=expected_capture_id)


def _require_capture_read_receipt(receipt: str | None, project_id: str,
                                  expected_revision: Mapping[str, Any] | None,
                                  capture_id: str,
                                  archive_root: str | None = None) -> dict[str, Any]:
    """Gate a model-facing archive read for one exact capture.

    Unlike a mutation gate, this must validate the receipt against the capture
    being read, not the latest capture in the project archive.  A receipt for a
    different capture, document hash, revision, project or fact root must never
    unlock the panorama payload.
    """
    if not receipt:
        raise ValidationError(
            "A complete, delivered panorama is required before reading a "
            "capture through the MCP surface",
            details={"capture_id": capture_id,
                     "archive_root": archive_root,
                     "next_action": ("call reaper_panorama_open for this capture, fetch "
                                     "every page with reaper_panorama_page, then "
                                     "reaper_panorama_ack with all page hashes"),
                     "next_action_args": {
                         "tool": "reaper_panorama_open",
                         "capture_id": capture_id,
                         "archive_root": archive_root,
                     },
                     "gate": "panorama_capture_read"})
    lifecycle = _panorama_lifecycle(archive_root)
    return lifecycle.require_receipt(
        receipt, project_id, expected_revision,
        expected_capture_id=capture_id,
    )


class ReferenceWindow(TypedDict):
    path: str
    start_s: float
    end_s: float
    name: NotRequired[str]

server = MCPServer(
    "reaper-workbench",
    title="REAPER Workbench",
    version="0.3.0",
    description=("Native REAPER control and audio analysis through the documented "
                 "ReaScript API and resident local bridge, with one revision-bound "
                 "project panorama delivered in measured pages and exact semantic queries."),
    instructions=(
        "REAPER Workbench. Establish the project, its identity and exact revision with "
        "reaper_session and reaper_project, then reaper_panorama_status. reaper_panorama_open "
        "returns page 1 of the revision-bound project panorama plus a manifest; fetch every page "
        "with reaper_panorama_page, then reaper_panorama_ack with all page hashes in order. "
        "That receipt proves delivery of every page of this bounded projection, not delivery of every raw audio point, "
        "not that the model understood it, and not that the model heard the audio. The panorama is a project-wide map, "
        "not full-resolution audio evidence: temporal maps use declared aggregation, and exact series and relation rows "
        "are fetched separately. Check coverage, point counts, resolution, aggregation, units, stage, and provenance. "
        "Before a conclusion or edit depends on fine timing, frequency, transient, or relation evidence, fetch the exact "
        "relevant source(s) and interval with reaper_panorama_query; narrow the interval if the result is over budget. "
        "Never treat a missing or summarized value as proof that the event or feature is absent. A URI alone does not "
        "mean an audio-capable client delivered sound to the model. Only that complete receipt unlocks analysis, exact "
        "queries and edits. A partial set returns the missing page ids; a stale revision needs a new open. "
        "Create a capture with reaper_capture_snapshot only when no capture matches the "
        "revision. Do not interpret project audio or edit from a partial panorama. Exact "
        "high-frequency rows "
        "(relation edges, series cells, MIDI notes, envelope points) come from "
        "reaper_panorama_query by semantic selection. "
        "Every successful mutation (reaper_edit apply/undo, "
        "reaper_api_call, reaper_action) returns a factual delta bound to the acknowledged "
        "base, together with its successor context token in the same response; use "
        "advance.context_token for the next structural edit and continue editing at the new "
        "revision without another full capture. A "
        "delta never makes stale audio current: its audio_evidence names every invalidated "
        "source, and project-target analysis refuses until a fresh capture is acknowledged. "
        "Raw API calls and Actions have unknown mutation scope and invalidate project audio "
        "conservatively. Use reaper_capabilities and reaper_api_search to discover "
        "operations; do not assume a fixed musical workflow. Project edits are batched as "
        "preview, then apply, then read back; every write is tied to project_id and "
        "expected_revision. The generic API and Actions remain available for documented "
        "REAPER functions beyond convenience edits. Audio analysis uses native REAPER render "
        "capture; raw AudioAccessor samples are pre-FX and are not processed mix stems. "
        "Temporary audio is outside the project and has a bounded lease. "
        "Operational anti-patterns: keep project_id, bridge/instance identity, epoch, "
        "change_count, capture_id and its receipt/context_token together; never combine "
        "tokens from different identities or revisions. After a mutation, use the returned "
        "successor token for structural work; if audio_evidence is stale, do not analyze old "
        "audio with it. For reaper_analyze, evidence_revision binds the measured WAV; "
        "revision_after_capture may include render-restoration counter churn and is not the "
        "audio revision. On stale_context/audio_evidence_fresh, inspect details and "
        "next_action, check whether an exact-revision capture already exists, then make at "
        "most one fresh capture/open/fetch-all-pages/ack cycle and retry the refused call "
        "once. If it still fails, stop and report the exact blocker; never loop captures or "
        "launch duplicate synchronous analysis. render_candidate_mismatch is failed "
        "verification: do not retry blindly or call an FX-chunk difference harmless. Read "
        "the affected project and FX parameters through MCP; without semantic before/after "
        "evidence, leave verification unresolved. A saved checkpoint proves persistence, "
        "not audio quality: distinguish saved, read back, measured after the last edit, "
        "and auditioned. Use MCP for all project reads, edits and analysis; never bypass "
        "its gates with direct bridge calls, scripts, GUI operations or hand-edited RPP files."
    ),
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, ReaperHandle):
        return value.to_wire()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, float) and (value != value or abs(value) == float("inf")):
        return None
    if type(value).__module__.startswith("numpy") and hasattr(value, "item"):
        return _jsonable(value.item())
    return value


def _next_action(code: str) -> str:
    return {
        "stale_context": ("Read the recovery contract and current project state; reuse an exact matching capture "
                          "before creating one. Refresh once and retry once; stop if it is still stale."),
        "bridge_unavailable": "Check that REAPER is open and its resident ReaScript bridge is running.",
        "bridge_timeout": "Check the REAPER bridge status before retrying; the operation may still have completed.",
        "validation_error": "Correct the named field or use capability/API search to confirm the signature.",
        "api_unavailable": "Check the REAPER version and discover an available alternative in the API catalog.",
        "transaction_failed": "Read the project state and undo status before attempting another edit.",
        "analysis_unavailable": "Inspect capture and analysis details, then retry a smaller time range if appropriate.",
        "capture_master_invalid": "The capture is archived as invalid evidence; inspect master_attempts and re-render after fixing the master signal path.",
        "media_unresolved": "Inspect the named take source paths and online/offline state; bring the media online before capturing rather than accepting silence.",
        "panorama_capacity_exceeded": "Request a smaller capture section or raise the explicit panorama capacity; the canonical record remains archived.",
        "panorama_binding_invalid": "Do not use this sidecar; rebuild the panorama from the verified canonical record.",
        "artifact_cleanup_failed": "Do not delete the directory manually; inspect the manifest and preserved paths.",
        "render_budget_exceeded": "Choose a shorter interval or a lower sample rate; the candidate is not delivered over budget.",
        "render_provenance_incomplete": "Do not use this candidate; fix the missing render settings, FX state or routing provenance first.",
        "render_candidate_mismatch": ("Do not retry the render. Read back the affected project and FX parameters "
                                       "through MCP; keep verification unresolved unless semantic state matches."),
        "render_unsupported": "Update the resident bridge so it supports the native master-only render contract.",
        "render_candidate_refused": "Inspect the typed refusal details; no candidate audio was delivered.",
    }.get(code, "Inspect the structured error details and verify current REAPER state before retrying.")


def _error_recovery(code: str, details: Mapping[str, Any]) -> dict[str, Any] | None:
    """Add a bounded recovery contract to context and candidate-render failures.

    This tells the model how to recover without weakening fail-closed gates.
    It never authorizes a retry or makes stale audio evidence current.
    """
    gate = str(details.get("gate") or "")
    if code == "render_candidate_mismatch":
        return {
            "contract": "reaper_mcp_recovery/1",
            "policy": "preserve_failed_verification",
            "retry_limit": 0,
            "required": [
                "Do not run another candidate render for this mismatch.",
                "Read back affected FX chains and parameter values through MCP.",
                "If semantic before/after state cannot be established, report audio verification as unresolved.",
            ],
        }
    if code != "stale_context" and gate not in {
            "panorama_interpretation", "panorama_completion", "panorama_capture_read"}:
        return None
    audio_stale = (details.get("audio_state") == "stale" or
                   details.get("audio_evidence") == "stale" or
                   gate == "audio_evidence_fresh")
    return {
        "contract": "reaper_mcp_recovery/1",
        "policy": "bounded_context_recovery",
        "failure_scope": "audio_evidence" if audio_stale else "project_context_or_receipt",
        "steps": [
            "Read this error's details and next_action; do not replay rejected arguments with an old token.",
            "Read the current project identity/revision and panorama status through MCP.",
            "Reuse or finish a complete capture only if it matches the exact current project identity and revision.",
            "If audio evidence is stale and no matching capture exists, create one capture, deliver every page, and acknowledge it.",
            "Retry the originally refused operation at most once; if refused again, stop and report the exact blocker.",
        ],
        "retry_limit": 1,
        "stop_after_repeat": True,
        "do_not": [
            "Do not start duplicate synchronous capture/analyze calls while one is running.",
            "Do not create a new capture merely because a render advanced change_count if evidence_revision still matches.",
            "Do not use a structural successor token to claim audio evidence is fresh.",
        ],
    }


def _error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ReaperAudioError):
        code = exc.code
        message = exc.message
        details = exc.details
    elif isinstance(exc, (TypeError, ValueError)):
        code = "validation_error"
        message = str(exc)
        details = {}
    else:
        _LOG.exception("Unhandled MCP tool failure")
        code = "internal_error"
        message = "The MCP tool failed unexpectedly. No success is being reported."
        details = {"exception_type": type(exc).__name__}
    error = {"code": code, "message": message,
             "details": _jsonable(details),
             "next_action": _next_action(code)}
    recovery = _error_recovery(code, details)
    if recovery is not None:
        error["recovery"] = recovery
    return {"ok": False, "error": error}


def _guard(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return _jsonable(function(*args, **kwargs))
        except Exception as exc:  # MCP clients need a typed tool result, not a traceback.
            return _error_payload(exc)
    return wrapped


def _session(project_id: str | None = None,
             bridge_session_id: str | None = None) -> ProjectSession:
    if project_id is not None and (not isinstance(project_id, str) or not project_id.strip()):
        raise ValidationError("project_id must be a non-empty string")
    if bridge_session_id is not None and (
        not isinstance(bridge_session_id, str) or not bridge_session_id.strip()
    ):
        raise ValidationError("bridge_session_id must be a non-empty string")
    return ProjectSession(ReaperBridge(bridge_session_id=bridge_session_id),
                          project_id=project_id)


def _revision(value: dict[str, Any], *, project_id: str | None = None) -> ProjectRevision:
    if not isinstance(value, dict):
        raise ValidationError("expected_revision must be a revision object returned by reaper_project")
    revision = ProjectRevision.from_dict(value)
    if (not revision.project_id or revision.change_count < 0 or
            not isinstance(value.get("path"), str)):
        raise ValidationError("expected_revision needs project_id, change_count, and path")
    if project_id is not None and revision.project_id != project_id:
        raise ValidationError("expected_revision belongs to a different project",
                              details={"requested_project_id": project_id,
                                       "revision_project_id": revision.project_id})
    return revision


def _reference_requests(
        reference_paths: list[str] | None,
        reference_windows: list[ReferenceWindow] | None,
        ) -> list[dict[str, Any]]:
    """Normalize independent full-file and reference-local audio windows."""
    if reference_paths is not None and (
        not isinstance(reference_paths, list) or len(reference_paths) > 32 or
        any(not isinstance(path, str) or not path.strip() for path in reference_paths)
    ):
        raise ValidationError("reference_paths must contain up to 32 file paths")
    if reference_windows is not None and (
        not isinstance(reference_windows, list) or len(reference_windows) > 32
    ):
        raise ValidationError("reference_windows must contain up to 32 windows")

    requests = [
        {"path": path, "name": Path(path).stem or f"reference_{index}",
         "section": None, "selection": "full_file"}
        for index, path in enumerate(reference_paths or [], start=1)
    ]
    for index, window in enumerate(reference_windows or [], start=1):
        if not isinstance(window, dict):
            raise ValidationError("Each reference window must be an object")
        required = {"path", "start_s", "end_s"}
        allowed = required | {"name"}
        if required - set(window) or set(window) - allowed:
            raise ValidationError(
                "A reference window needs path, start_s, and end_s; name is optional"
            )
        path = window["path"]
        name = window.get("name")
        start_s = window["start_s"]
        end_s = window["end_s"]
        if not isinstance(path, str) or not path.strip():
            raise ValidationError("Reference window path must be a non-empty string")
        if (name is not None and
                (not isinstance(name, str) or not name.strip() or len(name) > 120)):
            raise ValidationError("Reference window name must be a short non-empty string")
        if (isinstance(start_s, bool) or not isinstance(start_s, (int, float)) or
                isinstance(end_s, bool) or not isinstance(end_s, (int, float))):
            raise ValidationError("Reference window times must be numbers")
        section = Section(float(start_s), float(end_s))
        base_name = Path(path).stem or f"reference_{index}"
        requests.append({
            "path": path,
            "name": name or f"{base_name} "
                              f"[{section.start_s:g}-{section.end_s:g}s]",
            "section": section,
            "selection": "source_file_window",
        })
    if len(requests) > 32:
        raise ValidationError("A combined maximum of 32 reference inputs is supported")
    return requests


def _current_context(session: ProjectSession,
                     expected: ProjectRevision | None = None) -> dict[str, Any]:
    context = session.snapshot()
    current = ProjectRevision.from_dict(context.get("revision", {}))
    if not current.project_id or current.change_count < 0:
        raise BridgeUnavailable("REAPER returned an incomplete project revision")
    if expected is not None and current != expected:
        raise StaleContext("The project changed since the supplied context was read",
                           details={"expected": expected.to_dict(),
                                    "current": current.to_dict()})
    return context


def _mutation_readback(session: ProjectSession, project_id: str) -> dict[str, Any]:
    """Read the strongest post-mutation structural facts available.

    The revision comes from a full project-context read; the digest proves the
    render-affecting content.  A digest whose ``change_count`` disagrees with
    the observed revision, a digest that fails verification, or a missing
    subgroup is a partial readback and is reported as such — never as "no
    change".
    """
    missing: list[str] = []
    revision: ProjectRevision | None = None
    digest: dict[str, Any] | None = None
    evidence: Mapping[str, Any] | None = None
    try:
        context = session.snapshot()
        revision = ProjectRevision.from_dict(context.get("revision", {}))
        if not revision.project_id or revision.change_count < 0:
            missing.append("revision_unreadable")
            revision = None
    except Exception as exc:  # noqa: BLE001 - reported as a coverage fact
        missing.append(f"context_unreadable:{type(exc).__name__}")
    try:
        digest = session.digest(project_id)
    except Exception as exc:  # noqa: BLE001
        missing.append(f"digest_unavailable:{type(exc).__name__}")
    if isinstance(digest, Mapping):
        if str(digest.get("project_id") or "") != project_id:
            missing.append("digest_project_mismatch")
        if revision is not None and digest.get("change_count") != revision.change_count:
            missing.append("digest_revision_mismatch")
        try:
            resolved = digest_evidence(project_id, dict(digest))
            if resolved.scope != "full":
                missing.extend(f"digest_missing:{name}" for name in resolved.missing)
            evidence = {"fingerprint": resolved.fingerprint,
                        "scope": resolved.scope,
                        "missing": list(resolved.missing)}
        except Exception:  # noqa: BLE001
            missing.append("digest_unverifiable")
    complete = revision is not None and isinstance(digest, Mapping) and not missing
    identity = identity_from_mapping(revision.to_dict()) if revision is not None else None
    if identity is not None and identity.gaps():
        missing.extend(f"identity_missing:{gap}" for gap in identity.gaps())
        complete = False
    return {
        "revision": revision,
        "digest": digest,
        "evidence": evidence,
        "complete": complete,
        "missing": missing,
    }


def _record_mutation_delta(*, lifecycle: PanoramaLifecycle, kind: str,
                           receipt: str, project_id: str,
                           base_revision: ProjectRevision,
                           session: ProjectSession, scope: str,
                           operation_facts: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Attach one factual delta to a successful mutation, or say why not.

    A mutation has already happened when this runs.  A delta failure therefore
    cannot be reported as a mutation failure; it is returned as an explicit
    non-advanceable record with ``refresh_required``.
    """
    readback = _mutation_readback(session, project_id)
    revision = readback["revision"]
    if revision is None or not isinstance(readback["digest"], Mapping):
        return {
            "status": "unavailable",
            "acknowledgeable": False,
            "refresh_required": True,
            "reason": "post_mutation_readback_incomplete",
            "readback": {"complete": False, "missing": readback["missing"]},
            "next_call": {"tool": "reaper_capture_snapshot", "project_id": project_id,
                          "then": ("No structural delta can be minted from this readback; "
                                   "capture again and acknowledge the panorama.")},
        }
    try:
        delta = lifecycle.record_change(
            kind=kind, context_token=receipt, project_id=project_id,
            base_revision=base_revision.to_dict(),
            target_revision=revision.to_dict(),
            after_digest=readback["digest"],
            scope=scope,
            operation_facts=operation_facts,
            readback_complete=bool(readback["complete"]),
            readback_missing=readback["missing"],
            target_digest_evidence=readback["evidence"],
        )
    except Exception as exc:  # noqa: BLE001 - mutation already applied
        return {
            "status": "unavailable",
            "acknowledgeable": False,
            "refresh_required": True,
            "reason": f"delta_build_failed:{type(exc).__name__}",
            "error": str(exc),
            "readback": {"complete": bool(readback["complete"]),
                         "missing": readback["missing"]},
            "next_call": {"tool": "reaper_capture_snapshot", "project_id": project_id,
                          "then": ("The mutation applied but its context cannot be "
                                   "proven; capture again before interpreting or "
                                   "continuing structural edits.")},
        }
    return delta


def _classify_api(name: str) -> str:
    lowered = name.casefold()
    if lowered.startswith(("midi_", "midi", "midieditor", "midi_editor")):
        return "midi"
    if "fx" in lowered or "effect" in lowered or "plugin" in lowered:
        return "fx_plugins"
    if any(word in lowered for word in ("send", "routing", "sidechain", "recmode")):
        return "routing_recording"
    if any(word in lowered for word in ("envelope", "automation", "automode")):
        return "automation_envelopes"
    if any(word in lowered for word in ("tempo", "timemap", "measure", "grid", "beat")):
        return "tempo_grid_meter"
    if any(word in lowered for word in ("marker", "region")):
        return "markers_regions"
    if any(word in lowered for word in ("take", "mediaitem", "item_", "iteminfo")):
        return "items_takes_sources"
    if any(word in lowered for word in ("track", "folder", "master")):
        return "tracks"
    if any(word in lowered for word in ("audioaccessor", "audiodevice", "audio", "peak", "pcm_")):
        return "audio_capture_measurement"
    if any(word in lowered for word in ("project", "undo", "save", "config", "extstate")):
        return "project_state_persistence"
    if any(word in lowered for word in ("play", "stop", "pause", "record", "transport", "cursor")):
        return "transport"
    if any(word in lowered for word in ("kbd", "action", "shortcut", "command")):
        return "actions_keyboard"
    if any(word in lowered for word in ("gfx", "window", "dialog", "show", "ui", "js_")):
        return "ui_windows"
    if any(word in lowered for word in ("controlsurface", "control_surface", "joystick", "device")):
        return "devices_control_surfaces"
    return "other_documented_api"


def _api_domains() -> dict[str, list[str]]:
    catalog = get_api_catalog()
    domains: dict[str, list[str]] = {}
    for name in catalog.functions:
        domains.setdefault(_classify_api(name), []).append(name)
    for names in domains.values():
        names.sort(key=str.casefold)
    return dict(sorted(domains.items()))


def _capability_summary() -> dict[str, Any]:
    catalog = get_api_catalog()
    domains = _api_domains()
    return {
        "schema_version": 1,
        "api_source": "bundled official REAPER ReaScript signature catalog",
        "documentation_version": catalog.documentation_version,
        "documented_api_count": len(catalog.functions),
        "domains": [{"id": name, "function_count": len(functions),
                     "resource_uri": f"reaper://capabilities/{name}"}
                    for name, functions in domains.items()],
        "additional_capability_families": [
            {"id": "actions", "description": "REAPER action lists, searchable by section; invoke via typed API handles."},
            {"id": "project_context", "description": "Tracks, master, FX, sends, items, tempo, selection, and revisions."},
            {"id": "audio_analysis", "description": "All project tracks and rendered master, measurements, comparisons, temporal tape, and audio resources."},
            {"id": "transactions", "description": "Batch preview/apply/undo for library-supported project operations.",
             "supported_operations": list(_TRANSACTION_OPERATIONS)},
            {"id": "candidate_render", "description": "Plan-bound native master render on an exact interval, with verified restoration, explicit post-render revision, stage/FX/routing provenance and a leased audio resource."},
            {"id": "native_bridge_operations", "description": "Library operations such as render capture, save, tempo-map queries, and installed-FX inspection."},
        ],
        "runtime_claim": "Catalog entries are documented; availability and behavior depend on the installed REAPER version and are checked at call time.",
    }


def _api_resource(name: str) -> dict[str, Any]:
    signature = get_api_catalog().get(name)
    result = signature.to_dict(detail="full")
    result["runtime_note"] = "Documentation entry only; use reaper_api_call to check this REAPER instance."
    return result


def _analysis_artifact_path(record: dict[str, Any], source_guid: str) -> Path:
    path_text = record.get("audio_files", {}).get(source_guid)
    job_id = record.get("artifact_job_id")
    archive_root_text = record.get("archive_root")
    if isinstance(archive_root_text, str) and archive_root_text:
        expected = record.get("archive_audio_hashes", {}).get(source_guid)
        if not isinstance(expected, str) or len(expected) != 64:
            raise ValidationError("Archived audio resource has no verified content hash",
                                  details={"source_guid": source_guid})
        if not isinstance(path_text, str):
            raise ValidationError("The requested source has no retained audio resource",
                                  details={"source_guid": source_guid})
        root = Path(archive_root_text).expanduser().absolute()
        if (root.is_symlink() or getattr(root, "is_junction", lambda: False)() or
                not root.is_dir()):
            raise ValidationError("The capture archive root is unavailable or unsafe")
        root_real = root.resolve(strict=True)
        path = Path(path_text)
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)() or not path.is_file():
            raise ValidationError("The archived audio resource is no longer a regular file")
        resolved = path.resolve(strict=True)
        try:
            relative = resolved.relative_to(root_real)
        except ValueError as exc:
            raise ValidationError("Archived audio resource is outside its capture archive") from exc
        if (relative.parts[:2] != ("audio", "sha256") or len(relative.parts) != 3 or
                relative.name != f"{expected}.wav"):
            raise ValidationError("Archived audio resource does not match its content address",
                                  details={"source_guid": source_guid})
        if resolved.stat().st_size > _MAX_AUDIO_BYTES:
            raise ValidationError("Audio resource exceeds the per-resource transfer limit",
                                  details={"limit_bytes": _MAX_AUDIO_BYTES})
        return resolved
    if not isinstance(path_text, str) or not isinstance(job_id, str):
        raise ValidationError("The requested source has no retained audio resource",
                              details={"source_guid": source_guid})
    root = _AUDIO_ROOT.expanduser().absolute()
    if (not root.is_dir() or root.is_symlink() or
            getattr(root, "is_junction", lambda: False)()):
        raise ValidationError("The managed audio-resource root is unavailable")
    directory = root / f"job-{job_id}"
    if (not directory.is_dir() or directory.is_symlink() or
            getattr(directory, "is_junction", lambda: False)()):
        raise ValidationError("The audio lease directory is no longer available")
    root_real = root.resolve(strict=True)
    directory_real = directory.resolve(strict=True)
    path = Path(path_text)
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)() or not path.is_file():
        raise ValidationError("The leased audio resource is no longer a regular file")
    resolved = path.resolve(strict=True)
    if resolved.parent != directory_real or directory_real.parent != root_real:
        raise ValidationError("Audio resource path is outside its managed lease")
    manifest_path = directory / ".reaper-audio-job.json"
    if (manifest_path.is_symlink() or
            getattr(manifest_path, "is_junction", lambda: False)() or
            not manifest_path.is_file()):
        raise ValidationError("Audio lease manifest is missing or unsafe")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("owner") != "reaper-audio-library" or
            manifest.get("job_id") != job_id or
            resolved.name not in manifest.get("artifact_names", [])):
        raise ValidationError("Audio resource is not registered in its lease manifest")
    if resolved.stat().st_size > _MAX_AUDIO_BYTES:
        raise ValidationError("Audio resource exceeds the per-resource transfer limit",
                              details={"limit_bytes": _MAX_AUDIO_BYTES})
    return resolved


def _cleanup_analysis_record(analysis_id: str) -> bool:
    with _LOCK:
        record = _analysis_records.pop(analysis_id, None)
        if record is None:
            return False
        role_key = (record["project_id"], record["role"])
        if _analysis_roles.get(role_key) == analysis_id:
            _analysis_roles.pop(role_key, None)
    job_id = record.get("artifact_job_id")
    if isinstance(job_id, str):
        cleanup_audio_job(_AUDIO_ROOT, job_id)
    return True


def _prune_analysis_state() -> None:
    now = time.time()
    with _LOCK:
        expired = [key for key, value in _analysis_records.items()
                   if value["expires_at"] <= now]
    for analysis_id in expired:
        _cleanup_analysis_record(analysis_id)
    cleanup_stale_audio_jobs(_AUDIO_ROOT, max_age_s=_ANALYSIS_TTL_S, now_epoch=now)


def _record_capture_id(record: Mapping[str, Any]) -> str | None:
    """Return the capture an analysis record measured, when it names one.

    Analysis is bound to the revision before its own render churn; that
    revision carries the capture id.  Interpretation gates must compare the
    receipt to this capture, never to whatever capture was archived later.
    """
    revision = record.get("revision")
    if isinstance(revision, Mapping):
        value = revision.get("capture_id")
        if isinstance(value, str) and value:
            return value
    return None


def _report_without_local_paths(report: dict[str, Any]) -> dict[str, Any]:
    safe = dict(report)
    safe.pop("audio_files", None)
    return safe


def _analysis_windows(report: dict[str, Any], section: Section | None,
                      tail_ms: int) -> dict[str, Any]:
    """Separate what the capture wrote from what the analysis measured.

    ``session.analyze`` already reports ``capture_time_window`` and top-level
    ``tail_s``.  The MCP response keeps that evidence, but states the three
    spans explicitly so a client cannot mistake a rendered tail for measured
    material.
    """
    window = report.get("capture_time_window")
    window = window if isinstance(window, Mapping) else {}
    raw_tail = report.get("tail_s")
    tail_s = (float(raw_tail) if isinstance(raw_tail, (int, float)) and
              not isinstance(raw_tail, bool) else tail_ms / 1000.0)
    project_start = window.get("project_start_s")
    project_end = window.get("project_end_s")
    file_start = window.get("file_start_s")
    file_end = window.get("file_end_s")
    if section is not None:
        project_start, project_end = section.start_s, section.end_s
        file_start = 0.0 if file_start is None else file_start
        file_end = (section.duration_s + tail_s) if file_end is None else file_end
    measured = ({"start_s": project_start, "end_s": project_end}
                if isinstance(project_start, (int, float)) and
                isinstance(project_end, (int, float)) else None)
    captured = ({"start_s": file_start, "end_s": file_end}
                if isinstance(file_start, (int, float)) and
                isinstance(file_end, (int, float)) else None)
    return {
        "project_section": (None if section is None else
                            {"start_s": section.start_s, "end_s": section.end_s}),
        "measured": measured,
        "captured_file": captured,
        "tail_s": tail_s,
        # A valid capture resolves the project boundary before analysis, so a
        # rendered tail is never part of the measured span.  Only an unknown
        # boundary leaves this undecidable, and that capture never reaches here.
        "tail_included_in_measurement": (False if measured is not None or
                                         tail_s == 0 else None),
    }


def _capture_audio_path(archive: CaptureArchive,
                        asset: Mapping[str, Any]) -> Path:
    relative = asset.get("archive_audio")
    if not isinstance(relative, str) or not relative.startswith("audio/sha256/"):
        raise ValidationError("Capture audio is not stored in the content-addressed archive",
                              details={"source_guid": asset.get("source_guid")})
    path = (archive.root_real / relative).resolve(strict=True)
    try:
        relative_path = path.relative_to(archive.root_real)
    except ValueError as exc:
        raise ValidationError("Capture audio escaped its archive",
                              details={"source_guid": asset.get("source_guid")}) from exc
    if (len(relative_path.parts) != 3 or relative_path.parts[:2] != ("audio", "sha256") or
            path.is_symlink() or getattr(path, "is_junction", lambda: False)()):
        raise ValidationError("Capture audio path is not a regular content-addressed asset",
                              details={"source_guid": asset.get("source_guid")})
    return path


def _capture_measurement_report(source_guid: str, raw: Mapping[str, Any], *,
                                capture_id: str, asset_sha256: str,
                                project_start_s: float,
                                project_end_s: float) -> MeasurementReport:
    """Decode one archived JSON report and align its file-relative time axis."""
    raw_metrics = raw.get("metrics")
    raw_curves = raw.get("curves")
    raw_provenance = raw.get("provenance")
    if (not isinstance(raw_metrics, Mapping) or not isinstance(raw_curves, Mapping) or
            not isinstance(raw_provenance, Mapping)):
        raise ValidationError("Archived source analysis is incomplete",
                              details={"capture_id": capture_id,
                                       "source_guid": source_guid})
    sample_rate = raw.get("sample_rate")
    channels = raw.get("channels")
    duration_s = raw.get("duration_s")
    if (isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate <= 0 or
            isinstance(channels, bool) or not isinstance(channels, int) or channels <= 0 or
            isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)) or
            not isfinite(float(duration_s)) or duration_s <= 0):
        raise ValidationError("Archived source analysis has an invalid audio format",
                              details={"capture_id": capture_id,
                                       "source_guid": source_guid})

    metrics: dict[str, MetricValue] = {}
    for name, cell in raw_metrics.items():
        if not isinstance(cell, Mapping) or not isinstance(cell.get("unit"), str):
            raise ValidationError("Archived metric lacks its value/unit record",
                                  details={"capture_id": capture_id,
                                           "source_guid": source_guid,
                                           "metric": str(name)})
        value = cell.get("value")
        confidence = cell.get("confidence")
        if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float)) or
                not isfinite(float(value))):
            raise ValidationError("Archived metric value is not finite numeric data",
                                  details={"capture_id": capture_id,
                                           "source_guid": source_guid,
                                           "metric": str(name)})
        if confidence is not None and (
                isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or
                not isfinite(float(confidence))):
            raise ValidationError("Archived metric confidence is invalid",
                                  details={"capture_id": capture_id,
                                           "source_guid": source_guid,
                                           "metric": str(name)})
        note = cell.get("note")
        metrics[str(name)] = MetricValue(
            value=None if value is None else float(value),
            unit=cell["unit"],
            confidence=None if confidence is None else float(confidence),
            note=note if isinstance(note, str) else None,
        )

    provenance = _jsonable(raw_provenance)
    measured = provenance.get("measured_section_s")
    if not isinstance(measured, Mapping):
        raw_section = raw.get("section")
        measured = (raw_section.get("measured_section_s")
                    if isinstance(raw_section, Mapping) else None)
    measured_start = (measured.get("start_s")
                      if isinstance(measured, Mapping) else 0.0)
    if (isinstance(measured_start, bool) or
            not isinstance(measured_start, (int, float)) or
            not isfinite(float(measured_start))):
        measured_start = 0.0
    shift_s = project_start_s - float(measured_start)
    curves = _jsonable(raw_curves)

    def shift_axis(container: dict[str, Any], field: str) -> None:
        values = container.get(field)
        if isinstance(values, list):
            container[field] = [float(value) + shift_s
                                if isinstance(value, (int, float)) and
                                not isinstance(value, bool) else value
                                for value in values]

    for field in ("rms_400ms_time_s", "spectrogram_time_s"):
        shift_axis(curves, field)
    temporal = curves.get("temporal_profile")
    if isinstance(temporal, dict):
        shift_axis(temporal, "times")
        tempo = temporal.get("tempo")
        if isinstance(tempo, dict):
            for point in tempo.get("points") or []:
                if isinstance(point, dict):
                    value = point.get("time_s")
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        point["time_s"] = float(value) + shift_s
                    window = point.get("window_s")
                    if isinstance(window, dict):
                        for field in ("start", "end"):
                            value = window.get(field)
                            if isinstance(value, (int, float)) and not isinstance(value, bool):
                                window[field] = float(value) + shift_s

    events = curves.get("events_2ms")
    if isinstance(events, dict):
        for event in events.get("events") or []:
            if isinstance(event, dict):
                for field in ("t", "time_s"):
                    value = event.get(field)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        event[field] = float(value) + shift_s

    provenance["source_capture_id"] = capture_id
    provenance["asset_sha256"] = asset_sha256
    provenance["start_s"] = project_start_s
    provenance["measured_section_s"] = {
        "start_s": project_start_s, "end_s": project_end_s,
    }
    return MeasurementReport(
        source=source_guid,
        sample_rate=sample_rate,
        channels=channels,
        duration_s=float(duration_s),
        section=Section(project_start_s, project_end_s),
        metrics=metrics,
        curves=curves,
        provenance=provenance,
        coverage=_jsonable(raw.get("coverage") or {}),
    )


def _capture_audio_buffer(archive: CaptureArchive, asset: Mapping[str, Any], *,
                          project_start_s: float, project_end_s: float,
                          capture_start_s: float, capture_id: str) -> AudioBuffer:
    source_guid = str(asset.get("source_guid") or "")
    path = _capture_audio_path(archive, asset)
    local_section = Section(project_start_s - capture_start_s,
                            project_end_s - capture_start_s)
    decoded = load_audio(path, local_section)
    provenance = {
        **decoded.provenance,
        "capture_id": capture_id,
        "asset_sha256": asset.get("sha256"),
        "node": source_guid,
        "stage": asset.get("stage"),
        "channel_view": f"{decoded.channels}_channels",
        "window": {"start_s": project_start_s, "end_s": project_end_s},
    }
    return AudioBuffer(samples=decoded.samples, sample_rate=decoded.sample_rate,
                       source=source_guid, start_s=project_start_s,
                       provenance=provenance)


@server.tool()
@_guard
def reaper_session(action: str = "health",
                   bridge_session_id: str | None = None,
                   project_id: str | None = None) -> dict[str, Any]:
    """Inspect the REAPER connection or list live instances/projects.

    action: health, list_instances, list_projects, or dirty. When several
    REAPER instances are live, pass bridge_session_id to bind subsequent calls.
    ``dirty`` is a read-only gate: it reports whether the active project holds
    unsaved changes, which is what a controlled restart must not discard.
    """
    if action not in {"health", "list_instances", "list_projects", "dirty"}:
        raise ValidationError(
            "action must be health, list_instances, list_projects, or dirty")
    bridge = ReaperBridge(bridge_session_id=bridge_session_id)
    if action == "list_instances":
        return {"instances": bridge.discover_instances()}
    if action == "list_projects":
        return bridge.list_projects()
    if action == "dirty":
        # One tab when project_id is given, every open tab otherwise: a
        # controlled restart must not discard unsaved state from ANY of them.
        if project_id:
            state = bridge.call("project.render_settings.read",
                                {"project_id": project_id}, timeout_s=5.0)
            dirty = state.get("dirty") if isinstance(state, dict) else None
            if not isinstance(dirty, (int, float)) or isinstance(dirty, bool):
                raise BridgeUnavailable("REAPER returned no project dirty state",
                                        details={"project_id": project_id, "state": state})
            return {"dirty": bool(dirty), "project_id": project_id,
                    "revision": state.get("revision"),
                    "render_targets": state.get("render_targets")}
        listing = bridge.list_projects()
        rows: list[dict[str, Any]] = []
        for entry in listing.get("projects") or []:
            revision = entry.get("revision") or {}
            pid = revision.get("project_id")
            if not isinstance(pid, str) or not pid:
                raise BridgeUnavailable("REAPER returned a project without an id",
                                        details={"entry": entry})
            state = bridge.call("project.render_settings.read",
                                {"project_id": pid}, timeout_s=5.0)
            dirty = state.get("dirty") if isinstance(state, dict) else None
            if not isinstance(dirty, (int, float)) or isinstance(dirty, bool):
                raise BridgeUnavailable("REAPER returned no project dirty state",
                                        details={"project_id": pid, "state": state})
            rows.append({"project_id": pid, "name": entry.get("name"),
                         "active": bool(entry.get("is_active")),
                         "change_count": revision.get("change_count"),
                         "dirty": bool(dirty)})
        return {"projects": rows,
                "clean": all(not row["dirty"] for row in rows),
                "dirty_count": sum(1 for row in rows if row["dirty"])}
    return bridge.health(timeout_s=3.0)


@server.tool()
@_guard
def reaper_checkpoint_save(path: str, project_id: str,
                           expected_revision: dict[str, Any],
                           verify_media: bool = True,
                           bridge_session_id: str | None = None) -> dict[str, Any]:
    """Save the active project to a new .RPP checkpoint and verify the result.

    Narrow by design: save-as only, an existing file is refused (never
    overwritten), and the call only accepts the active project at the exact
    revision the caller read. This is not a substitute for the capture gate; it
    stores unsaved state before a controlled restart. The answer is verified —
    new path, rebound project id, ``dirty == 0`` and every audio source online —
    and any mismatch raises even though the file was written.
    """
    bridge = ReaperBridge(bridge_session_id=bridge_session_id)
    return save_checkpoint(
        path=path,
        project_id=project_id,
        root=checkpoint_root(),
        expected_revision=expected_revision,
        call=bridge.call,
        health=lambda: bridge.health(timeout_s=3.0),
        context=lambda: bridge.call(
            "project.context",
            {"include_items": True, "include_fx_params": False},
            timeout_s=120.0),
        verify_media=verify_media,
    )


@server.tool()
@_guard
def reaper_bridge_reload(timeout_s: float = 45.0,
                         bridge_session_id: str | None = None) -> dict[str, Any]:
    """Activate the bridge build that is already on disk, without touching the project.

    This is a lifecycle call, not a project edit: it compiles the on-disk bridge
    script first (a broken file is refused and the running copy is left alone),
    then asks the resident loop to hand the mailbox to the new copy. It never
    saves, closes or reloads a project. The result names the previous and the
    new session and their builds, and it fails closed when exactly one new
    session with the expected build cannot be proven. A legacy bridge without
    this lifecycle contract is refused; this tool never attempts action bootstrap.
    """
    path = bridge_script_path()
    build = script_build(path)
    if not build:
        raise ValidationError(
            "The on-disk bridge script declares no build; refusing to activate it",
            details={"path": str(path)})
    bridge = ReaperBridge(bridge_session_id=bridge_session_id)

    initial = bridge.health(timeout_s=3.0)
    project = initial.get("project") if isinstance(initial, Mapping) else None
    expected_project_id = (project.get("project_id")
                           if isinstance(project, Mapping) else None)
    result = activate_bridge(
        health=lambda session: ReaperBridge(
            bridge_session_id=session or bridge_session_id).health(timeout_s=3.0),
        discover=bridge.discover_instances,
        request_reload=lambda expected: bridge.call(
            "bridge.lifecycle",
            {"action": "reload", "expected_build": expected},
            timeout_s=10.0),
        expected_project_id=expected_project_id,
        build=build,
        timeout_s=timeout_s,
    )
    result["script_path"] = str(path)
    return result


@server.tool()
@_guard
def reaper_capabilities(mode: str = "summary", domain: str | None = None,
                        offset: int = 0, limit: int = 60,
                        detail: str = "names") -> dict[str, Any]:
    """Browse the full documented API by domain without flooding the model.

    mode=summary returns the index. mode=domain pages through one domain.
    detail may be names, summary, or full; summary/full include signatures.
    """
    if mode not in {"summary", "domain"}:
        raise ValidationError("mode must be summary or domain")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValidationError("offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValidationError("limit must be from 1 to 100")
    if detail not in {"names", "summary", "full"}:
        raise ValidationError("detail must be names, summary, or full")
    if mode == "summary":
        return _capability_summary()
    if not isinstance(domain, str) or not domain:
        raise ValidationError("domain is required when mode=domain")
    domains = _api_domains()
    if domain not in domains:
        raise ValidationError("Unknown API domain",
                              details={"domain": domain,
                                       "available_domains": list(domains)})
    names = domains[domain]
    selected = names[offset:offset + limit]
    catalog = get_api_catalog()
    if detail == "names":
        functions = [{"name": name} for name in selected]
    else:
        functions = [catalog.get(name).to_dict(detail=detail) for name in selected]
    return {"domain": domain, "documentation_version": catalog.documentation_version,
            "documented_function_count": len(names), "offset": offset,
            "returned_count": len(functions),
            "next_offset": offset + len(functions) if offset + len(functions) < len(names) else None,
            "functions": functions,
            "runtime_note": "This is the documented catalog. Check actual availability with reaper_api_call."}


@server.tool()
@_guard
def reaper_api_search(object: str, operation: str,
                      qualifiers: list[str] | None = None,
                      kind: str = "api", detail: str = "summary",
                      limit: int = 8, section_id: int = 0,
                      bridge_session_id: str | None = None) -> dict[str, Any]:
    """Find documented ReaScript APIs or REAPER Actions from structured intent.

    Supply one object and one operation, with short qualifiers. Do not pass the
    whole user request as a search sentence. Search hits are candidates, not a
    recommendation to execute the first result.
    """
    if not isinstance(object, str) or not object.strip() or len(object) > 80:
        raise ValidationError("object must be 1 to 80 characters")
    if not isinstance(operation, str) or not operation.strip() or len(operation) > 80:
        raise ValidationError("operation must be 1 to 80 characters")
    qualifiers = qualifiers or []
    if (not isinstance(qualifiers, list) or len(qualifiers) > 8 or
            any(not isinstance(item, str) or not item.strip() or len(item) > 80
                for item in qualifiers)):
        raise ValidationError("qualifiers must contain at most 8 short strings")
    if kind not in {"api", "actions", "both"}:
        raise ValidationError("kind must be api, actions, or both")
    if detail not in {"names", "summary", "full"}:
        raise ValidationError("detail must be names, summary, or full")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValidationError("limit must be from 1 to 20")
    if isinstance(section_id, bool) or not isinstance(section_id, int):
        raise ValidationError("section_id must be an integer")
    result: dict[str, Any] = {"structured_query": {"object": object,
                                                    "operation": operation,
                                                    "qualifiers": qualifiers}}
    if kind in {"api", "both"}:
        hits = get_api_catalog().search_card(
            object.strip(), operation.strip(),
            qualifiers=[item.strip() for item in qualifiers],
            limit=limit, detail=detail)
        result["api"] = {"documentation_version": get_api_catalog().documentation_version,
                         "results": [hit.to_dict(detail=detail) for hit in hits]}
    if kind in {"actions", "both"}:
        bridge = ReaperBridge(bridge_session_id=bridge_session_id)
        translations = {
            "дорожка": "track", "дорожки": "track", "трек": "track",
            "айтем": "item", "клип": "item", "медиа айтем": "item",
            "разрезать": "split", "нарезать": "split", "разделить": "split",
            "переместить": "move", "сдвинуть": "move", "удалить": "delete",
            "создать": "insert", "добавить": "insert", "изменить": "set",
            "получить": "get", "темп": "tempo", "панорама": "pan",
            "эффект": "fx", "плагин": "fx", "дорожек": "track",
        }
        words: list[str] = []
        for field in (object, operation, *qualifiers):
            normalized = field.strip().casefold()
            expanded = translations.get(normalized, normalized)
            words.extend(part for part in expanded.replace("_", " ").split() if len(part) > 1)
        terms = list(dict.fromkeys(words))
        operation_terms = set(translations.get(operation.strip().casefold(), operation.casefold()).split())
        scores: dict[int, dict[str, Any]] = {}
        for term in terms:
            found = bridge.call("actions.list", {"section_id": section_id,
                                                  "query": term,
                                                  "offset": 0,
                                                  "limit": 5000})
            for action in found.get("actions", []):
                command_id = action.get("command_id")
                name = str(action.get("name", ""))
                if isinstance(command_id, int):
                    hit = scores.setdefault(command_id, {"command_id": command_id,
                                                         "name": name, "matched_terms": set()})
                    hit["matched_terms"].add(term)
        action_hits = []
        for hit in scores.values():
            matched = hit["matched_terms"]
            operation_match = len(matched & operation_terms)
            if operation_match == 0:
                continue
            action_hits.append({"command_id": hit["command_id"], "name": hit["name"],
                                "matched_terms": sorted(matched),
                                "score": operation_match * 4 + len(matched)})
        action_hits.sort(key=lambda item: (-item["score"], item["name"].casefold()))
        result["actions"] = {"section_id": section_id,
                             "results": action_hits[:limit],
                             "note": "Action search ranks names only; inspect the target project before invocation."}
    return result


@server.tool()
@_guard
def reaper_project(project_id: str | None = None,
                   bridge_session_id: str | None = None,
                   include_items: bool = False,
                   include_fx_params: bool = False,
                   include_midi_notes: bool = False,
                   track_guids: list[str] | None = None,
                   item_guids: list[str] | None = None,
                   expected_revision: dict[str, Any] | None = None,
                   panorama_receipt: str | None = None,
                   archive_root: str | None = None) -> dict[str, Any]:
    """Read one project's current context, including master, routing and tempo.

    Request items, FX parameters, or MIDI note data only when relevant. GUID
    filters trim returned tracks/items; the master is always included.
    """
    session = _session(project_id, bridge_session_id)
    expected = (_revision(expected_revision, project_id=project_id)
                if expected_revision is not None else None)
    context = session.snapshot(include_items=include_items,
                               include_fx_params=include_fx_params,
                               include_midi_notes=include_midi_notes)
    revision = ProjectRevision.from_dict(context.get("revision", {}))
    if expected is not None and expected != revision:
        raise StaleContext("The project changed since expected_revision was read",
                           details={"expected": expected.to_dict(),
                                    "current": revision.to_dict()})
    if panorama_receipt:
        gate = _require_panorama_receipt(panorama_receipt, revision.project_id,
                                         revision.to_dict(), archive_root)
        evidence = gate.get("audio_evidence")
        evidence = evidence if isinstance(evidence, Mapping) else {}
        fresh = str(evidence.get("state") or "unknown") == "current"
        context["panorama_gate"] = {
            "complete": True,
            "receipt_valid": True,
            "interpretation_allowed": fresh,
            "audio_evidence": dict(evidence),
            "note": ("A structural delta advances edits only; project-audio "
                     "interpretation needs evidence.state == 'current'."),
        }
    else:
        context["panorama_gate"] = {
            "complete": False,
            "receipt_valid": False,
            "interpretation_allowed": False,
            "bootstrap_structural_read": True,
            "next_call": {
                "tool": "reaper_capture_snapshot",
                "then": ["reaper_panorama_open"],
            },
            "note": ("This structural read is allowed to establish the project "
                     "and revision for the first capture. Do not treat it as a "
                     "complete panorama or interpret project audio until "
                     "reaper_panorama_open has delivered the view and its receipt."),
        }
    tracks = context.get("tracks", [])
    if track_guids is not None:
        if not isinstance(track_guids, list) or any(not isinstance(g, str) for g in track_guids):
            raise ValidationError("track_guids must be a list of GUID strings")
        wanted = set(track_guids)
        context["tracks"] = [track for track in tracks if track.get("guid") in wanted]
        context["requested_track_guids_not_found"] = sorted(
            wanted - {track.get("guid") for track in tracks})
    if item_guids is not None:
        if not isinstance(item_guids, list) or any(not isinstance(g, str) for g in item_guids):
            raise ValidationError("item_guids must be a list of GUID strings")
        wanted_items = set(item_guids)
        found_items: set[str] = set()
        for track in context.get("tracks", []):
            items = track.get("items", [])
            track["items"] = [item for item in items
                              if item.get("guid") in wanted_items]
            found_items.update(item.get("guid") for item in track["items"])
        context["requested_item_guids_not_found"] = sorted(wanted_items - found_items)
    bridge_id = session.bridge.bridge_session_id
    context["context_uri"] = (
        f"reaper://projects/{quote(bridge_id or '', safe='')}/"
        f"{quote(revision.project_id, safe='')}/context/{revision.change_count}"
    )
    return context


def _analyze_archived_capture(*, capture_id: str, project_id: str | None,
                              expected_revision: dict[str, Any] | None,
                              start_s: float | None, end_s: float | None,
                              include_pairwise: bool,
                              phase_pairs: list[tuple[str, str]] | str | None,
                              include_partial_loudness: bool,
                              reference_requests: list[dict[str, Any]],
                              artifact_role: str, detail: str,
                              tail_ms: int, channels: str,
                              sample_rate: int | None,
                              panorama_receipt: str | None,
                              archive_root: str | None) -> dict[str, Any]:
    """Compare references to one exact archived capture without a REAPER render."""
    if not archive_root:
        raise ValidationError("archive_root is required when capture_id is supplied")
    if tail_ms:
        raise ValidationError("tail_ms cannot be added to archived capture audio")
    if channels != "stereo":
        raise ValidationError("channels is a live-render option; archived analysis preserves captured channels")

    archive = CaptureArchive(archive_root)
    captured = archive.read(capture_id)
    if project_id is not None and project_id != captured.project_id:
        raise StaleContext("Requested project_id does not match the archived capture",
                           details={"project_id": project_id,
                                    "capture_project_id": captured.project_id,
                                    "capture_id": capture_id})
    if expected_revision is not None:
        expected = ProjectRevision.from_dict(expected_revision)
        allowed_revisions = [captured.revision_before]
        if captured.revision_after is not None:
            allowed_revisions.append(captured.revision_after)
        if not any(ProjectRevision.from_dict(dict(value)) == expected
                   for value in allowed_revisions):
            raise StaleContext("expected_revision does not belong to this archived capture",
                               details={"capture_id": capture_id,
                                        "expected": expected.to_dict(),
                                        "capture_revision_before": captured.revision_before,
                                        "capture_revision_after": captured.revision_after})

    panorama_revision = captured.revision_after or captured.revision_before
    _require_capture_read_receipt(
        panorama_receipt, captured.project_id, panorama_revision,
        captured.capture_id, archive_root,
    )
    window = captured.time_window
    capture_start = float(window["project_start_s"])
    capture_end = float(window["project_end_s"])
    selected_start = capture_start if start_s is None else float(start_s)
    selected_end = capture_end if end_s is None else float(end_s)
    if selected_start < capture_start or selected_end > capture_end:
        raise ValidationError("Requested interval is outside the archived capture window",
                              details={"capture_id": capture_id,
                                       "capture_window": {"start_s": capture_start,
                                                          "end_s": capture_end},
                                       "requested_window": {"start_s": selected_start,
                                                            "end_s": selected_end}})
    selected_section = Section(selected_start, selected_end)
    capture_duration = capture_end - capture_start
    selected_duration = selected_end - selected_start

    assets = [dict(row) for row in captured.audio]
    assets_by_guid = {str(row["source_guid"]): row for row in assets}
    if len(assets_by_guid) != len(assets):
        raise ValidationError("Archived capture has duplicate source identities",
                              details={"capture_id": capture_id})
    master_ids = [guid for guid, row in assets_by_guid.items()
                  if row.get("is_master") is True]
    if len(master_ids) != 1:
        raise ValidationError("Archived capture must contain exactly one master audio asset",
                              details={"capture_id": capture_id,
                                       "master_count": len(master_ids)})
    master_id = master_ids[0]
    if sample_rate is not None and any(
            int(row.get("sample_rate") or 0) != sample_rate for row in assets):
        raise ValidationError("sample_rate differs from the archived capture; it cannot be resampled without a new render",
                              details={"requested_sample_rate": sample_rate,
                                       "captured_sample_rates": sorted({
                                           int(row.get("sample_rate") or 0) for row in assets})})
    raw_analysis = captured.analysis
    if not isinstance(raw_analysis, Mapping) or set(raw_analysis) != set(assets_by_guid):
        raise ValidationError("Archived capture analysis does not cover every audio source",
                              details={"capture_id": capture_id,
                                       "audio_sources": len(assets_by_guid),
                                       "analysis_sources": (len(raw_analysis)
                                                            if isinstance(raw_analysis, Mapping)
                                                            else 0)})

    phase_source_ids: set[str] = set()
    if phase_pairs == "all":
        phase_source_ids = set(assets_by_guid)
    elif isinstance(phase_pairs, list):
        for pair in phase_pairs:
            phase_source_ids.update(pair)
    raw_source_ids = set(phase_source_ids)
    if include_partial_loudness:
        raw_source_ids.update(assets_by_guid)
    if raw_source_ids - set(assets_by_guid):
        raise ValidationError("Requested phase source is not part of the archived capture",
                              details={"unknown_source_ids": sorted(
                                  raw_source_ids - set(assets_by_guid))})

    reference_audio: dict[str, AudioBuffer] = {}
    reference_sections: dict[str, Section] = {}
    reference_inputs: list[dict[str, Any]] = []
    for request in reference_requests:
        reference_section = request["section"]
        loaded = load_audio(request["path"], reference_section)
        key_base = request["name"]
        key = key_base
        suffix = 2
        while key in reference_audio:
            key = f"{key_base}_{suffix}"
            suffix += 1
        reference_audio[key] = loaded
        if reference_section is not None:
            reference_sections[key] = reference_section
        reference_inputs.append({
            "reference_id": key,
            "selection": request["selection"],
            "window": None if reference_section is None else {
                "start_s": reference_section.start_s,
                "end_s": reference_section.end_s,
            },
        })

    capture_tail_s = max(
        0.0,
        max(float(row.get("duration_s") or 0.0) for row in assets) - capture_duration,
    )
    exact_full_window = (
        abs(selected_start - capture_start) <= 1e-9 and
        abs(selected_end - capture_end) <= 1e-9
    )
    precomputed_usable = exact_full_window and capture_tail_s <= 1e-6
    if precomputed_usable:
        shared_band_count: int | None = None
        for guid, raw in raw_analysis.items():
            duration = raw.get("duration_s") if isinstance(raw, Mapping) else None
            rate = assets_by_guid[guid].get("sample_rate")
            tolerance = 1.0 / float(rate) if isinstance(rate, int) and rate > 0 else 1e-6
            if (not isinstance(duration, (int, float)) or isinstance(duration, bool) or
                    abs(float(duration) - capture_duration) > tolerance):
                precomputed_usable = False
                break
            curves = raw.get("curves")
            profile = curves.get("temporal_profile") if isinstance(curves, Mapping) else None
            cells = profile.get("cells") if isinstance(profile, Mapping) else None
            if not isinstance(cells, (list, tuple)) or not cells:
                precomputed_usable = False
                break
            for cell in cells:
                bands = cell.get("bands") if isinstance(cell, Mapping) else None
                if not isinstance(bands, (list, tuple)) or not bands:
                    precomputed_usable = False
                    break
                if any(isinstance(value, bool) or
                       not isinstance(value, (int, float)) or
                       not isfinite(float(value)) for value in bands):
                    precomputed_usable = False
                    break
                if shared_band_count is None:
                    shared_band_count = len(bands)
                elif len(bands) != shared_band_count:
                    precomputed_usable = False
                    break
            if not precomputed_usable:
                break

    from .daw.analysis_policy import available_memory_bytes

    decoded_bytes = 0
    for guid in raw_source_ids:
        asset = assets_by_guid[guid]
        frames = int(asset.get("frames") or 0)
        channels_count = int(asset.get("channels") or 0)
        asset_duration = float(asset.get("duration_s") or 0.0)
        if frames > 0 and channels_count > 0 and asset_duration > 0:
            selected_frames = min(frames, int(round(frames * selected_duration / asset_duration)))
            decoded_bytes += selected_frames * channels_count * 8
    available = available_memory_bytes() if raw_source_ids else None
    estimated_working_bytes = decoded_bytes * 2
    memory_budget = None if available is None else int(available * 0.70)
    if memory_budget is not None and estimated_working_bytes > memory_budget:
        raise ValidationError("Archived raw-audio analysis exceeds the memory-safe budget",
                              details={"capture_id": capture_id,
                                       "selected_sources": sorted(raw_source_ids),
                                       "decoded_audio_bytes": decoded_bytes,
                                       "estimated_working_bytes": estimated_working_bytes,
                                       "memory_budget_bytes": memory_budget,
                                       "next_action": "select fewer phase pairs or omit partial loudness"})

    source_reports: dict[str, MeasurementReport] = {}
    raw_buffers: dict[str, AudioBuffer | None] = {guid: None for guid in assets_by_guid}
    for guid, asset in assets_by_guid.items():
        if precomputed_usable:
            source_reports[guid] = _capture_measurement_report(
                guid, raw_analysis[guid], capture_id=capture_id,
                asset_sha256=str(asset["sha256"]),
                project_start_s=capture_start, project_end_s=capture_end,
            )
            if guid in raw_source_ids:
                raw_buffers[guid] = _capture_audio_buffer(
                    archive, asset, project_start_s=selected_start,
                    project_end_s=selected_end, capture_start_s=capture_start,
                    capture_id=capture_id,
                )
        else:
            audio_buffer = _capture_audio_buffer(
                archive, asset, project_start_s=selected_start,
                project_end_s=selected_end, capture_start_s=capture_start,
                capture_id=capture_id,
            )
            source_reports[guid] = replace(
                measure_audio(audio_buffer), section=selected_section, source=guid)
            if guid in raw_source_ids:
                raw_buffers[guid] = audio_buffer

    report = analyze_bundle(
        {guid: raw_buffers[guid] for guid in assets_by_guid},
        references=reference_audio or None,
        reference_sections=reference_sections or None,
        include_pairwise=include_pairwise,
        phase_pairs=phase_pairs,
        anchor_key=master_id,
        mix_key=master_id if include_partial_loudness else None,
        precomputed_reports=source_reports,
    )

    track_map = {
        str(row.get("guid")): row for row in captured.project_map.get("tracks", [])
        if isinstance(row, Mapping) and row.get("guid")
    }
    master_context = captured.project_map.get("master")
    if not isinstance(master_context, Mapping):
        master_context = {}
    source_rows: list[dict[str, Any]] = []
    audio_files: dict[str, str] = {}
    audio_hashes: dict[str, str] = {}
    for guid, source_report in report["sources"].items():
        asset = assets_by_guid[guid]
        audio_files[guid] = str(_capture_audio_path(archive, asset))
        audio_hashes[guid] = str(asset["sha256"])
        context = master_context if asset.get("is_master") else track_map.get(guid, {})
        track_context = {
            "guid": guid,
            "name": asset.get("name") or context.get("name", ""),
            "volume": context.get("volume"),
            "pan": context.get("pan"),
            "mute": context.get("mute"),
            "solo": context.get("solo"),
            "is_master": bool(asset.get("is_master")),
        }
        asset_context = {
            key: asset.get(key) for key in (
                "asset_id", "source_guid", "name", "is_master", "stage",
                "capture_method", "sample_rate", "channels", "frames",
                "duration_s", "bytes", "sha256", "signal_window",
                "capture_revision", "render_key", "render_key_complete",
                "render_dependencies",
            ) if key in asset
        }
        source_report["track_context"] = track_context
        source_report["capture"] = asset_context
        source_rows.append({
            "source_guid": guid,
            "name": track_context["name"],
            "is_master": track_context["is_master"],
            "volume": track_context["volume"],
            "pan": track_context["pan"],
            "mute": track_context["mute"],
            "capture": asset_context,
            "metrics": source_report.get("metrics", {}),
        })

    analysis_revision = dict(captured.revision_before)
    analysis_revision["capture_id"] = capture_id
    revision_after = dict(captured.revision_after or captured.revision_before)
    revision_after["capture_id"] = capture_id
    capture_info = {
        "capture_id": capture_id,
        "integrity_sha256": captured.integrity_sha256,
        "audio_sources": len(assets),
        "analysis_reused": precomputed_usable,
        "render_performed": False,
    }
    analysis_windows = {
        "project_section": {"start_s": selected_start, "end_s": selected_end},
        "measured": {"start_s": selected_start, "end_s": selected_end},
        "captured_file": {"start_s": 0.0,
                           "end_s": max(float(row.get("duration_s") or 0.0)
                                        for row in assets)},
        "tail_s": capture_tail_s,
        "tail_included_in_measurement": False,
    }
    report["project"] = {
        "project_id": captured.project_id,
        "path": captured.project_path,
        "revision": analysis_revision,
        "capture_id": capture_id,
    }
    report["capture"] = capture_info
    report["capture_binding"] = {
        "project_id": captured.project_id,
        "capture_id": capture_id,
        "evidence_revision": analysis_revision,
        "revision_after_capture": revision_after,
        "time_window": {"start_s": capture_start, "end_s": capture_end},
        "asset_sha256": audio_hashes,
    }
    report["analysis_windows"] = analysis_windows
    report["reference_inputs"] = reference_inputs
    report["execution"] = {
        "project_audio_source": "verified_archived_capture",
        "reaper_render_performed": False,
        "source_metrics": "reused" if precomputed_usable else "remeasured_from_archived_wav",
        "project_time_origin_s": capture_start,
    }
    project_tempo = captured.project_map.get("tempo", {})
    report["project_tempo"] = project_tempo

    analysis_id = f"a-{int(time.time())}-{os.urandom(6).hex()}"
    record = {
        "analysis_id": analysis_id,
        "project_id": captured.project_id,
        "revision": analysis_revision,
        "revision_after": revision_after,
        "role": artifact_role,
        "artifact_job_id": None,
        "audio_files": audio_files,
        "archive_audio_hashes": audio_hashes,
        "archive_root": str(archive.root_real),
        "capture_id": capture_id,
        "report": report,
        "expires_at": time.time() + _ANALYSIS_TTL_S,
    }
    role_key = (captured.project_id, artifact_role)
    with _LOCK:
        previous_id = _analysis_roles.get(role_key)
        _analysis_records[analysis_id] = record
        _analysis_roles[role_key] = analysis_id
    if previous_id:
        _cleanup_analysis_record(previous_id)

    for row in source_rows:
        guid = row["source_guid"]
        row["audio_uri"] = (f"reaper://analyses/{analysis_id}/audio/"
                            f"{quote(guid, safe='')}")
    result = {
        "ok": True,
        "analysis_id": analysis_id,
        "artifact_role": artifact_role,
        "lease_expires_in_seconds": _ANALYSIS_TTL_S,
        "project": report["project"],
        "initial_context_revision": analysis_revision,
        "evidence_revision": analysis_revision,
        "revision_after_capture": revision_after,
        "capture_id": capture_id,
        "section": {"start_s": selected_start, "end_s": selected_end},
        "analysis_windows": analysis_windows,
        "reference_inputs": reference_inputs,
        "source_count_including_master": len(source_rows),
        "sources": source_rows,
        "coverage": report.get("coverage", {}),
        "project_tempo": project_tempo,
        "capture": capture_info,
        "execution": report["execution"],
        "report_uri": f"reaper://analyses/{analysis_id}/report",
        "resource_note": ("Audio resources point to hash-verified durable capture WAVs. "
                          "Releasing this analysis lease never deletes the archived capture."),
    }
    if detail == "full":
        result["report"] = report
    return result


@server.tool()
@_guard
def reaper_analyze(project_id: str | None = None,
                   bridge_session_id: str | None = None,
                   expected_revision: dict[str, Any] | None = None,
                   start_s: float | None = None,
                   end_s: float | None = None,
                   include_pairwise: bool = False,
                   phase_pairs: list[list[str]] | str | None = None,
                   include_partial_loudness: bool = False,
                   reference_paths: list[str] | None = None,
                   reference_windows: list[ReferenceWindow] | None = None,
                   parallel: bool = True,
                   artifact_role: str = "temporary",
                   detail: str = "summary",
                   tail_ms: StrictInt = 0,
                   channels: str = "stereo",
                   sample_rate: StrictInt | None = None,
                   panorama_receipt: str | None = None,
                   archive_root: str | None = None,
                   capture_id: str | None = None) -> dict[str, Any]:
    """Run the complete available audio suite on all tracks and the rendered master.

    With no capture_id, this is one batch operation that renders and analyzes
    the current project. With capture_id, it reuses one exact, receipt-verified
    archived capture and does not call the REAPER bridge or render the project.
    It returns a concise report plus analysis-scoped audio-resource URIs.
    reference_paths analyzes complete files; reference_windows selects
    source-file-local windows independently from the project section.
    artifact_role holds one lease each for temporary, baseline, or final.

    tail_ms asks REAPER to render extra audio past the project section so
    reverb decays survive; the extra tail is never measured. channels and
    sample_rate are the render request. All three options affect only the
    project capture: reference files and reference windows are read unchanged.
    """
    if (start_s is None) != (end_s is None):
        raise ValidationError("start_s and end_s must be supplied together")
    section = Section(start_s, end_s) if start_s is not None else None
    if isinstance(tail_ms, bool) or not isinstance(tail_ms, int) or tail_ms < 0:
        raise ValidationError("tail_ms must be a non-negative integer")
    if not isinstance(channels, str) or channels not in {"stereo", "mono"}:
        raise ValidationError("channels must be 'stereo' or 'mono'")
    if sample_rate is not None and (
            isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or
            sample_rate <= 0):
        raise ValidationError("sample_rate must be a positive integer or None")
    if not isinstance(include_pairwise, bool) or not isinstance(include_partial_loudness, bool):
        raise ValidationError("analysis options must be boolean")
    if not isinstance(parallel, bool):
        raise ValidationError("parallel must be boolean")
    if artifact_role not in {"temporary", "baseline", "final"}:
        raise ValidationError("artifact_role must be temporary, baseline, or final")
    if detail not in {"summary", "full"}:
        raise ValidationError("detail must be summary or full")
    if phase_pairs is not None and phase_pairs != "all":
        if (not isinstance(phase_pairs, list) or
                any(not isinstance(pair, list) or len(pair) != 2 or
                    any(not isinstance(source, str) for source in pair)
                    for pair in phase_pairs)):
            raise ValidationError("phase_pairs must be 'all' or pairs of source GUIDs")
        phase_pairs = [tuple(pair) for pair in phase_pairs]
    reference_requests = _reference_requests(reference_paths, reference_windows)

    _prune_analysis_state()
    if capture_id is not None:
        if not isinstance(capture_id, str) or not capture_id.strip():
            raise ValidationError("capture_id must be a non-empty string or null")
        return _analyze_archived_capture(
            capture_id=capture_id,
            project_id=project_id,
            expected_revision=expected_revision,
            start_s=start_s,
            end_s=end_s,
            include_pairwise=include_pairwise,
            phase_pairs=phase_pairs,
            include_partial_loudness=include_partial_loudness,
            reference_requests=reference_requests,
            artifact_role=artifact_role,
            detail=detail,
            tail_ms=tail_ms,
            channels=channels,
            sample_rate=sample_rate,
            panorama_receipt=panorama_receipt,
            archive_root=archive_root,
        )

    _focus_reaper_window()
    session = _session(project_id, bridge_session_id)
    expected = (_revision(expected_revision, project_id=project_id)
                if expected_revision is not None else None)
    project_before = _current_context(session, expected)
    selected_project_id = project_before["revision"]["project_id"]
    _require_interpretation_receipt(
        panorama_receipt, selected_project_id, project_before["revision"],
        archive_root,
    )
    references: dict[str, Any] = {}
    reference_sections: dict[str, Section] = {}
    reference_inputs: list[dict[str, Any]] = []
    for request in reference_requests:
        path = request["path"]
        reference_section = request["section"]
        reference_audio = load_audio(path, reference_section)
        key_base = request["name"]
        key = key_base
        suffix = 2
        while key in references:
            key = f"{key_base}_{suffix}"
            suffix += 1
        references[key] = reference_audio
        if reference_section is not None:
            reference_sections[key] = reference_section
        reference_inputs.append({
            "reference_id": key,
            "selection": request["selection"],
            "window": None if reference_section is None else {
                "start_s": reference_section.start_s,
                "end_s": reference_section.end_s,
            },
        })
    report = session.analyze(
        str(_AUDIO_ROOT), section=section, references=references or None,
        reference_sections=reference_sections or None,
        include_pairwise=include_pairwise, phase_pairs=phase_pairs,
        parallel=parallel, include_partial_loudness=include_partial_loudness,
        capture_mode="render", retain_audio=True, timeout_s=1800.0,
        tail_ms=tail_ms, channels=channels, sample_rate=sample_rate,
    )
    revision = report.get("project", {}).get("revision", {})
    capture_revision = report.get("project", {}).get("capture_revision", {})
    if expected is not None and ProjectRevision.from_dict(capture_revision) != expected:
        job_id = report.get("capture", {}).get("artifact_job_id")
        if isinstance(job_id, str):
            cleanup_audio_job(_AUDIO_ROOT, job_id)
        raise StaleContext("Project revision changed before audio capture began",
                           details={"expected": expected.to_dict(),
                                    "captured": capture_revision})
    if revision.get("project_id") != selected_project_id:
        job_id = report.get("capture", {}).get("artifact_job_id")
        if isinstance(job_id, str):
            cleanup_audio_job(_AUDIO_ROOT, job_id)
        raise StaleContext("The active project changed while analysis was running",
                           details={"started_project_id": selected_project_id,
                                    "captured_project_id": revision.get("project_id")})
    artifact_job_id = report.get("capture", {}).get("artifact_job_id")
    audio_files = report.get("audio_files")
    if not isinstance(artifact_job_id, str) or not isinstance(audio_files, dict):
        raise BridgeUnavailable("Audio analysis returned no managed temporary resources")
    analysis_windows = _analysis_windows(report, section, tail_ms)
    if analysis_windows["tail_s"] > 0 and analysis_windows["measured"] is None:
        cleanup_audio_job(_AUDIO_ROOT, artifact_job_id)
        raise BridgeUnavailable(
            "Audio analysis did not report a measurable project boundary for "
            "the rendered tail",
            details={"boundary": "unknown",
                     "capture_time_window": report.get("capture_time_window"),
                     "tail_s": analysis_windows["tail_s"]},
        )

    analysis_id = f"a-{int(time.time())}-{os.urandom(6).hex()}"
    # The measured audio is bound to the pre-capture revision; the post-capture
    # revision differs by the capture's own render-settings churn. Gates for
    # consuming this analysis must use the evidence binding, not that churn.
    analysis_revision = (capture_revision if isinstance(capture_revision, Mapping)
                         and capture_revision.get("project_id") else revision)
    record = {"analysis_id": analysis_id, "project_id": selected_project_id,
              "revision": analysis_revision, "revision_after": revision,
              "role": artifact_role,
              "artifact_job_id": artifact_job_id,
              "audio_files": audio_files,
              "report": _report_without_local_paths(report),
              "expires_at": time.time() + _ANALYSIS_TTL_S}
    role_key = (selected_project_id, artifact_role)
    with _LOCK:
        previous_id = _analysis_roles.get(role_key)
        _analysis_records[analysis_id] = record
        _analysis_roles[role_key] = analysis_id
    if previous_id:
        _cleanup_analysis_record(previous_id)

    source_rows = []
    for guid, source in report.get("sources", {}).items():
        track_context = source.get("track_context", {})
        metric_values = source.get("metrics", {})
        source_rows.append({
            "source_guid": guid,
            "name": track_context.get("name", ""),
            "is_master": bool(source.get("capture", {}).get("is_master",
                                                            track_context.get("is_master", False))),
            "volume": track_context.get("volume"),
            "pan": track_context.get("pan"),
            "mute": track_context.get("mute"),
            "capture": source.get("capture", {}),
            "metrics": metric_values,
            "audio_uri": (f"reaper://analyses/{analysis_id}/audio/"
                          f"{quote(guid, safe='')}") if guid in audio_files else None,
        })
    result: dict[str, Any] = {
        "ok": True,
        "analysis_id": analysis_id,
        "artifact_role": artifact_role,
        "lease_expires_in_seconds": _ANALYSIS_TTL_S,
        "project": report.get("project"),
        "initial_context_revision": project_before.get("revision"),
        "evidence_revision": analysis_revision,
        "revision_after_capture": revision,
        "section": report.get("section"),
        "analysis_windows": analysis_windows,
        "reference_inputs": reference_inputs,
        "source_count_including_master": len(source_rows),
        "sources": source_rows,
        "coverage": report.get("coverage", {}),
        "project_tempo": report.get("project_tempo", {}),
        "capture": report.get("capture", {}),
        "report_uri": f"reaper://analyses/{analysis_id}/report",
        "resource_note": "Audio WAVs are outside the RPP, manifest-managed, replaced per role, and deleted after the lease or explicit release.",
    }
    if detail == "full":
        result["report"] = record["report"]
    return result


@server.tool()
@_guard
def reaper_edit_map(analysis_id: str,
                    intent: str | None = None,
                    decide: bool = False,
                    source_groups: dict[str, str] | None = None,
                    start_s: float | None = None,
                    end_s: float | None = None,
                    panorama_receipt: str | None = None,
                    archive_root: str | None = None) -> dict[str, Any]:
    """Build cut/seam evidence from an existing analysis, without applying it.

    This is deliberately evidence-only by default: no tight/natural preset,
    no cut/keep decision, no group guessing. ``decide=True`` is an explicit
    request for the library's heuristic labels; groups come only from
    ``source_groups`` supplied by the caller.
    """
    if not isinstance(analysis_id, str) or not analysis_id:
        raise ValidationError("analysis_id is required")
    if not isinstance(decide, bool):
        raise ValidationError("decide must be a boolean")
    if intent is not None and intent not in {"tight", "natural", "preserve"}:
        raise ValidationError("intent must be tight, natural, or preserve")
    if source_groups is not None and (
        not isinstance(source_groups, dict) or
        any(not isinstance(key, str) or not isinstance(value, str)
            for key, value in source_groups.items())
    ):
        raise ValidationError("source_groups must map source names to group names")
    if (start_s is None) != (end_s is None):
        raise ValidationError("start_s and end_s must be supplied together")
    _prune_analysis_state()
    record = _analysis_records.get(analysis_id)
    if record is None or record["expires_at"] <= time.time():
        raise ValidationError("analysis_id is unknown or expired")
    report = record.get("report")
    if not isinstance(report, dict):
        raise ValidationError("analysis has no report")
    _require_interpretation_receipt(
        panorama_receipt, str(record.get("project_id") or ""),
        record.get("revision"), archive_root,
        expected_capture_id=_record_capture_id(record),
    )
    points = tempo_points_from_report(report)
    edit_map = build_edit_map_from_report(
        report, points, intent=intent, decide=decide,
        source_groups=source_groups, start_s=start_s, end_s=end_s)
    return {
        "ok": True,
        "analysis_id": analysis_id,
        "revision": record.get("revision"),
        "tempo_point_count": len(points),
        "edit_map": edit_map,
    }


@server.tool()
@_guard
def reaper_mix_evidence(analysis_id: str,
                        panorama_receipt: str | None = None,
                        archive_root: str | None = None) -> dict[str, Any]:
    """Return measured mix issues/questions from an existing analysis.

    No plugin, EQ frequency, or gain recipe is chosen here. The agent reads the
    evidence (band dominance, reference deltas, partial audibility, phase) and
    decides the processing.
    """
    if not isinstance(analysis_id, str) or not analysis_id:
        raise ValidationError("analysis_id is required")
    _prune_analysis_state()
    record = _analysis_records.get(analysis_id)
    if record is None or record["expires_at"] <= time.time():
        raise ValidationError("analysis_id is unknown or expired")
    report = record.get("report")
    if not isinstance(report, dict):
        raise ValidationError("analysis has no report")
    _require_interpretation_receipt(
        panorama_receipt, str(record.get("project_id") or ""),
        record.get("revision"), archive_root,
        expected_capture_id=_record_capture_id(record),
    )
    evidence = build_mix_evidence(report)
    return {
        "ok": True,
        "analysis_id": analysis_id,
        "revision": record.get("revision"),
        "mix_evidence": evidence,
    }


def _edit_operation_facts(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Pair the per-operation before/after readback into one factual list."""
    before = result.get("before")
    after = result.get("after")
    before_rows = before if isinstance(before, list) else []
    after_rows = after if isinstance(after, list) else []
    facts: list[dict[str, Any]] = []
    for index, entry in enumerate(before_rows):
        if not isinstance(entry, Mapping):
            continue
        paired = after_rows[index] if index < len(after_rows) else {}
        paired = paired if isinstance(paired, Mapping) else {}
        facts.append({
            "operation_index": entry.get("operation_index"),
            "kind": entry.get("kind"),
            "target_guid": entry.get("target_guid"),
            "before": entry.get("values"),
            "after": paired.get("values"),
            "changes_state": entry.get("changes_state"),
        })
    return facts


@server.tool()
@_guard
def reaper_edit(mode: str,
                project_id: str,
                expected_revision: dict[str, Any],
                operations: list[dict[str, Any]] | None = None,
                reason: str | None = None,
                plan_id: str | None = None,
                bridge_session_id: str | None = None,
                panorama_receipt: str | None = None,
                archive_root: str | None = None) -> dict[str, Any]:
    """Preview, apply, or undo one revision-bound batch of project edits.

    preview: supply operations and reason; nothing changes. apply: use the
    returned plan_id and the same expected_revision; the response carries a
    factual delta bound to the acknowledged panorama token. undo: use that
    plan_id and the post-apply revision returned by apply; undo is a new
    forward revision with its own delta. Each batch may contain up to 256
    library-supported operations; generic API calls and Actions remain
    available for operations outside this transaction set.
    """
    if mode not in {"preview", "apply", "undo"}:
        raise ValidationError("mode must be preview, apply, or undo")
    expected = _revision(expected_revision, project_id=project_id)
    session = _session(project_id, bridge_session_id)
    _current_context(session, expected)
    if mode != "preview":
        _require_panorama_receipt(panorama_receipt, project_id, expected.to_dict(),
                                  archive_root)
    if mode == "preview":
        if not isinstance(operations, list) or not operations:
            raise ValidationError("preview requires a non-empty operations list")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("preview requires a short reason")
        parsed = []
        for index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                raise ValidationError("Each operation must be an object",
                                      details={"operation_index": index})
            if set(operation) - {"kind", "target_guid", "values"}:
                raise ValidationError("Operation contains unsupported top-level fields",
                                      details={"operation_index": index})
            parsed.append(EditOperation(kind=str(operation.get("kind", "")),
                                        target_guid=str(operation.get("target_guid", "")),
                                        values=operation.get("values", {})))
        plan = session.plan(parsed, reason=reason, expected_revision=expected)
        with _LOCK:
            _edit_plans[plan.plan_id] = {"plan": plan, "project_id": project_id,
                                        "bridge_session_id": bridge_session_id,
                                        "created_at": time.time(),
                                        "applied_revision": None}
            if len(_edit_plans) > _MAX_PLAN_CACHE:
                oldest = sorted(_edit_plans.items(), key=lambda pair: pair[1]["created_at"])
                for expired_id, _ in oldest[:len(_edit_plans) - _MAX_PLAN_CACHE]:
                    _edit_plans.pop(expired_id, None)
        return {"mode": "preview", "changed": False,
                "plan": plan.to_dict(),
                "next_call": {"tool": "reaper_edit", "mode": "apply",
                              "plan_id": plan.plan_id,
                              "project_id": project_id,
                              "expected_revision": plan.revision.to_dict()}}
    if not isinstance(plan_id, str) or not plan_id:
        raise ValidationError(f"{mode} requires plan_id returned by preview")
    with _LOCK:
        cached = _edit_plans.get(plan_id)
    if cached is None:
        raise ValidationError("plan_id is unknown or expired; preview the change again",
                              details={"plan_id": plan_id})
    plan: EditPlan = cached["plan"]
    if cached["project_id"] != project_id:
        raise StaleContext("The edit plan does not match this project revision",
                           details={"plan_project_id": cached["project_id"],
                                    "plan_revision": plan.revision.to_dict(),
                                    "provided_revision": expected.to_dict()})
    # Apply starts from the plan's base revision.  Undo starts from the exact
    # revision that apply produced, which is not the plan's base revision; the
    # applied-revision check below still requires that exact value.
    if mode != "undo" and plan.revision != expected:
        raise StaleContext("The edit plan does not match this project revision",
                           details={"plan_project_id": cached["project_id"],
                                    "plan_revision": plan.revision.to_dict(),
                                    "provided_revision": expected.to_dict()})
    if cached["bridge_session_id"] != bridge_session_id:
        raise StaleContext("The edit plan belongs to a different REAPER bridge instance")
    if mode == "apply":
        if cached["applied_revision"] is not None:
            raise ValidationError("This edit plan was already applied")
        result = session.apply(plan, dry_run=False)
        applied_revision = result.get("revision_after")
        if not isinstance(applied_revision, dict):
            raise BridgeUnavailable("REAPER did not return the applied project revision")
        applied = ProjectRevision.from_dict(applied_revision)
        cached["applied_revision"] = applied
        delta = _record_mutation_delta(
            lifecycle=_panorama_lifecycle(archive_root), kind="edit",
            receipt=str(panorama_receipt), project_id=project_id,
            base_revision=expected, session=session, scope="known",
            operation_facts=_edit_operation_facts(result))
        advance = delta.get("advance") if isinstance(delta, Mapping) else None
        if isinstance(advance, Mapping) and advance.get("context_token"):
            cached["applied_context_token"] = str(advance["context_token"])
        cached["applied_archive_root"] = archive_root
        if not delta.get("delta_id") or not delta.get("acknowledgeable"):
            return {"mode": "apply", "plan_id": plan_id, "result": result,
                    "delta": delta, "next_call": delta.get("next_call")}
        return {"mode": "apply", "plan_id": plan_id,
                "result": result,
                "delta": delta,
                "next_call": {
                    "tool": None,
                    "context_token": (delta.get("advance") or {}).get("context_token"),
                    "note": ("the successor token is already in delta.advance.context_token; "
                             "no acknowledgement call is required to continue structural "
                             "edits. Project-audio interpretation still needs a fresh "
                             "capture.")}}
    applied_revision = cached.get("applied_revision")
    if not isinstance(applied_revision, ProjectRevision) or applied_revision != expected:
        raise StaleContext("Undo needs the exact revision returned by this plan's apply",
                           details={"applied_revision": (applied_revision.to_dict()
                                                          if isinstance(applied_revision, ProjectRevision)
                                                          else None),
                                    "provided_revision": expected.to_dict()})
    result = session.undo(expected_revision=expected)
    undone_revision = result.get("revision_after")
    delta = _record_mutation_delta(
        lifecycle=_panorama_lifecycle(archive_root), kind="undo",
        receipt=str(panorama_receipt), project_id=project_id,
        base_revision=expected, session=session, scope="known",
        operation_facts=None)
    with _LOCK:
        _edit_plans.pop(plan_id, None)
    response = {"mode": "undo", "plan_id": plan_id, "result": result,
                "revision_after": undone_revision, "delta": delta}
    if delta.get("delta_id") and delta.get("acknowledgeable"):
        response["next_call"] = {
            "tool": None,
            "context_token": (delta.get("advance") or {}).get("context_token"),
            "note": ("the successor token is already in delta.advance.context_token; the "
                     "earlier edit records remain history and no ack call is required"),
        }
    else:
        response["next_call"] = delta.get("next_call")
    return response


def _advance_candidate_context(*, cached: dict[str, Any], project_id: str,
                               applied: ProjectRevision,
                               post_revision: ProjectRevision,
                               session: ProjectSession) -> dict[str, Any]:
    """Make the post-render revision the next valid lifecycle revision/token.

    A native REAPER render can advance the monotonic change counter while the
    content digest and restored settings remain identical.  That is not an edit
    and must not invalidate audio evidence, but the old context token is bound
    to the pre-render revision.  Record a content-neutral delta from the
    applied revision to the post-render revision using the token returned by
    apply, so the model receives the next valid token instead of a stale one.
    """
    token = cached.get("applied_context_token")
    if not token:
        return {
            "status": "unavailable",
            "reason": "no_applied_context_token",
            "from_revision": applied.to_dict(),
            "to_revision": post_revision.to_dict(),
            "revision_after_render": post_revision.to_dict(),
            "next_call": {
                "tool": "reaper_panorama_refresh",
                "project_id": project_id,
                "then": ("The render advanced the counter but no apply context "
                         "token was cached; refresh the panorama before the next edit."),
            },
        }
    lifecycle = _panorama_lifecycle(cached.get("applied_archive_root"))
    delta = _record_mutation_delta(
        lifecycle=lifecycle, kind="render_candidate", receipt=str(token),
        project_id=project_id, base_revision=applied, session=session,
        scope="known", operation_facts=[])
    advance = delta.get("advance") if isinstance(delta, Mapping) else None
    next_token = advance.get("context_token") if isinstance(advance, Mapping) else None
    if not next_token:
        return {
            "status": "unavailable",
            "reason": str(delta.get("reason") or "delta_not_advanceable"),
            "from_revision": applied.to_dict(),
            "to_revision": post_revision.to_dict(),
            "revision_after_render": post_revision.to_dict(),
            "delta": delta,
            "next_call": delta.get("next_call") or {
                "tool": "reaper_panorama_refresh",
                "project_id": project_id,
                "then": "Refresh the panorama before the next structural edit.",
            },
        }
    return {
        "status": "advanced",
        "from_revision": applied.to_dict(),
        "to_revision": post_revision.to_dict(),
        "revision_after_render": post_revision.to_dict(),
        "context_token": str(next_token),
        "delta": delta,
        "next_call": delta.get("next_call"),
    }


@server.tool()
@_guard
def reaper_render_candidate(
        plan_id: str,
        project_id: str,
        revision_after: dict[str, Any],
        start_s: float,
        end_s: float,
        bridge_session_id: str | None = None,
        channels: str = "stereo",
        sample_rate: StrictInt | None = None,
        tail_ms: StrictInt = 0,
        max_duration_s: float = 120.0) -> dict[str, Any]:
    """Render the applied edit's processed master output on an exact interval.

    This is the verification primitive for an iterative mix: it binds one
    already-applied ``reaper_edit`` plan to a short, explicitly selected
    interval and returns the processed master WAV (track, bus and master FX
    plus routing) with its SHA-256, exact project/file bounds, render stage,
    FX/routing provenance, render key and a lease-managed audio resource.

    The native REAPER render temporarily rewrites render settings and the time
    selection, then restores them. Restoration is verified by comparing the
    project digest and important render state before/after; the monotonic
    counter may advance and is returned explicitly as ``revision_after_render``.
    No project reload, save, reset or hidden counter rollback is performed.
    The candidate file is outside the .rpp and is replaced on the next
    candidate for this project; release it with
    ``reaper_release(analysis_id=candidate_id)`` or let its lease expire.
    """
    _focus_reaper_window()
    if not isinstance(plan_id, str) or not plan_id:
        raise ValidationError("plan_id must be the id returned by reaper_edit preview/apply")
    if not isinstance(project_id, str) or not project_id:
        raise ValidationError("project_id must be a non-empty string")
    if (isinstance(start_s, bool) or not isinstance(start_s, (int, float)) or
            isinstance(end_s, bool) or not isinstance(end_s, (int, float)) or
            not isfinite(float(start_s)) or not isfinite(float(end_s))):
        raise ValidationError("start_s and end_s must be finite numbers")
    section = Section(float(start_s), float(end_s))
    if (isinstance(max_duration_s, bool) or
            not isinstance(max_duration_s, (int, float)) or
            not isfinite(float(max_duration_s)) or max_duration_s <= 0):
        raise ValidationError("max_duration_s must be a positive finite number")
    if isinstance(tail_ms, bool) or not isinstance(tail_ms, int) or tail_ms < 0:
        raise ValidationError("tail_ms must be a non-negative integer")
    if channels not in {"stereo", "mono"}:
        raise ValidationError("channels must be 'stereo' or 'mono'")
    if sample_rate is not None and (isinstance(sample_rate, bool) or sample_rate <= 0):
        raise ValidationError("sample_rate must be a positive integer or None")
    requested = _revision(revision_after, project_id=project_id)
    with _LOCK:
        cached = _edit_plans.get(plan_id)
    if cached is None:
        raise ValidationError("plan_id is unknown or expired; preview/apply the edit again",
                              details={"plan_id": plan_id})
    if cached.get("project_id") != project_id:
        raise StaleContext("The candidate plan belongs to a different project",
                           details={"plan_project_id": cached.get("project_id"),
                                    "project_id": project_id})
    if cached.get("bridge_session_id") != bridge_session_id:
        raise StaleContext("The candidate plan belongs to a different REAPER bridge instance")
    created_at = cached.get("created_at")
    if (isinstance(created_at, (int, float)) and
            time.time() - float(created_at) > _PLAN_TTL_S):
        raise StaleContext("The candidate plan has expired; preview/apply the edit again",
                           details={"plan_id": plan_id, "ttl_s": _PLAN_TTL_S})
    applied = cached.get("applied_revision")
    if not isinstance(applied, ProjectRevision):
        raise ValidationError("The candidate plan has not been applied",
                              details={"plan_id": plan_id,
                                       "next_action": "apply the previewed plan first"})
    if (applied.project_id != requested.project_id or
            applied.change_count != requested.change_count or
            applied.path != requested.path or
            (applied.instance_id is not None and requested.instance_id is not None and
             applied.instance_id != requested.instance_id) or
            (applied.epoch is not None and requested.epoch is not None and
             applied.epoch != requested.epoch)):
        raise StaleContext("revision_after is not the revision returned by this plan's apply",
                           details={"plan_applied_revision": applied.to_dict(),
                                    "revision_after": requested.to_dict()})
    plan = cached.get("plan")
    if plan is None or getattr(plan, "plan_id", None) != plan_id:
        raise ValidationError("The cached candidate plan is invalid")
    session = _session(project_id, bridge_session_id)
    _current_context(session, requested)
    _prune_analysis_state()
    result = session.render_candidate(
        plan, revision_after=applied, section=section,
        artifact_root=_AUDIO_ROOT, channels=channels,
        sample_rate=sample_rate, tail_ms=tail_ms,
        max_duration_s=float(max_duration_s),
    )
    result_map: dict[str, Any] = result if isinstance(result, dict) else {}
    provenance_raw = result_map.get("provenance")
    provenance: dict[str, Any] = (dict(provenance_raw)
                                  if isinstance(provenance_raw, Mapping) else {})
    master_guid = str(provenance.get("master_guid") or "")
    job_raw = result_map.get("artifact_job")
    job_map: dict[str, Any] = dict(job_raw) if isinstance(job_raw, Mapping) else {}
    job_id = str(job_map.get("job_id") or "")
    audio_raw = result_map.get("audio")
    audio: dict[str, Any] = dict(audio_raw) if isinstance(audio_raw, Mapping) else {}
    audio_path = audio.get("path")
    if not master_guid or not job_id or not isinstance(audio_path, str) or not audio_path:
        if job_id:
            cleanup_audio_job(_AUDIO_ROOT, job_id)
        raise BridgeUnavailable("The candidate render returned incomplete lease provenance")
    post_raw = result_map.get("revision_after_render")
    if not isinstance(post_raw, Mapping):
        cleanup_audio_job(_AUDIO_ROOT, job_id)
        raise BridgeUnavailable("The candidate render did not return a post-render revision")
    try:
        post_revision = ProjectRevision.from_dict(dict(post_raw))
    except Exception as exc:
        cleanup_audio_job(_AUDIO_ROOT, job_id)
        raise BridgeUnavailable(
            "The candidate render returned an invalid post-render revision",
            details={"reason": str(exc), "revision_after_render": post_raw},
        ) from exc
    if post_revision == applied:
        context_advance: dict[str, Any] = {
            "status": "not_required",
            "from_revision": applied.to_dict(),
            "to_revision": post_revision.to_dict(),
            "revision_after_render": post_revision.to_dict(),
            "context_token": cached.get("applied_context_token"),
        }
    else:
        try:
            context_advance = _advance_candidate_context(
                cached=cached, project_id=project_id, applied=applied,
                post_revision=post_revision, session=session)
        except Exception as exc:  # noqa: BLE001 - audio is already valid
            context_advance = {
                "status": "unavailable",
                "reason": f"context_advance_failed:{type(exc).__name__}",
                "from_revision": applied.to_dict(),
                "to_revision": post_revision.to_dict(),
                "revision_after_render": post_revision.to_dict(),
                "next_call": {
                    "tool": "reaper_panorama_refresh",
                    "project_id": project_id,
                    "then": "Refresh the panorama before the next structural edit.",
                },
            }
    with _LOCK:
        cached["applied_revision"] = post_revision
        if context_advance.get("context_token"):
            cached["applied_context_token"] = str(context_advance["context_token"])
    candidate_id = f"rc-{int(time.time())}-{os.urandom(6).hex()}"
    role_key = (project_id, "candidate")
    with _LOCK:
        previous_id = _analysis_roles.get(role_key)
    if previous_id:
        try:
            _cleanup_analysis_record(previous_id)
        except Exception:
            cleanup_audio_job(_AUDIO_ROOT, job_id)
            raise
    record = {
        "analysis_id": candidate_id,
        "project_id": project_id,
        "revision": result_map.get("revision_after"),
        "revision_after": result_map.get("revision_after_render"),
        "role": "candidate",
        "artifact_job_id": job_id,
        "audio_files": {master_guid: audio_path},
        "report": {
            "kind": "render_candidate",
            "plan_id": plan_id,
            "project_id": project_id,
            "revision_after": result_map.get("revision_after"),
            "provenance": provenance,
            "audio": {key: value for key, value in audio.items() if key != "path"},
        },
        "expires_at": time.time() + _ANALYSIS_TTL_S,
    }
    with _LOCK:
        _analysis_records[candidate_id] = record
        _analysis_roles[role_key] = candidate_id
    audio_uri = (f"reaper://analyses/{candidate_id}/audio/"
                 f"{quote(master_guid, safe='')}")
    project_state_raw = result_map.get("project_state")
    project_state: dict[str, Any] = (dict(project_state_raw)
                                     if isinstance(project_state_raw, Mapping) else {})
    response = {
        "ok": True,
        "mode": "render_candidate",
        "candidate_id": candidate_id,
        "plan_id": plan_id,
        "project_id": project_id,
        "revision_before": result_map.get("revision_before"),
        "revision_after": result_map.get("revision_after"),
        "revision_after_render": result_map.get("revision_after_render"),
        "render_key": result_map.get("render_key"),
        "section": result_map.get("section"),
        "duration_s": result_map.get("duration_s"),
        "render_s": result_map.get("render_s"),
        "audio": {
            "resource_uri": audio_uri,
            "sha256": audio.get("sha256"),
            "bytes": audio.get("bytes"),
            "sample_rate": audio.get("sample_rate"),
            "channels": audio.get("channels"),
            "frames": audio.get("frames"),
            "duration_s": audio.get("duration_s"),
            "format": audio.get("format"),
            "stage": audio.get("stage"),
            "node": audio.get("node"),
            "channel_view": audio.get("channel_view"),
            "source_kind": audio.get("source_kind"),
            "audio_accessor_used": audio.get("audio_accessor_used"),
            "file_window": audio.get("file_window"),
            "project_window": audio.get("project_window"),
            "measured_window": audio.get("measured_window"),
            "tail_s": audio.get("tail_s"),
        },
        "provenance": provenance,
        "project_state": project_state,
        "context_advance": context_advance,
        "completeness": {
            "plan_bound": True,
            "revision_bound": True,
            "audio_delivered": bool(audio.get("sha256") and audio.get("bytes")),
            "stage_known": bool(audio.get("stage") and audio.get("node")),
            "fx_provenance": bool(provenance.get("fingerprints")),
            "routing_provenance": bool(provenance.get("routing_summary")),
            "render_settings_echo": bool(provenance.get("render_settings_used")),
            "restoration_verified": bool(project_state.get("restoration_verified")),
            "project_content_unchanged": bool(project_state.get("content_unchanged")),
            "content_evidence_equal": bool(
                (project_state.get("content_comparison") or {}).get("equal")),
            "post_render_revision_current": bool(
                project_state.get("revision_after_render_is_current")),
            "counter_advance_reported": bool(
                project_state.get("counter_changed_by_render")),
            "lifecycle_token_current": bool(context_advance.get("context_token")),
        },
        "lease": {
            "kind": "render_candidate",
            "id": candidate_id,
            "expires_at": record["expires_at"],
            "ttl_s": _ANALYSIS_TTL_S,
            "release_tool": "reaper_release",
            "release_argument": {"analysis_id": candidate_id},
            "cleanup": "manifest-owned job + TTL; replaced by the next candidate for this project",
        },
        "warnings": [],
    }
    if project_state.get("counter_changed_by_render"):
        response["warnings"].append("render_counter_advanced")
    if context_advance.get("status") == "unavailable":
        response["warnings"].append("candidate_context_token_unavailable")
    if ((provenance.get("fingerprints") if isinstance(provenance, Mapping) else {})
            or {}).get("project_digest_scope") != "full":
        response["warnings"].append("project_digest_scope_coarse")
    if result_map.get("revision_after_render") != result_map.get("revision_after"):
        response["warnings"].append("render_advanced_revision_counter")
    return response


def _handle_argument(value: Any, *, kind: str, revision: ProjectRevision,
                     bridge_session_id: str | None) -> Any:
    if isinstance(value, dict) and "$reaper_ref" in value:
        reference = value.get("$reaper_ref")
        if not isinstance(reference, dict) or reference.get("kind") != kind:
            raise ValidationError("$reaper_ref kind must match the API parameter",
                                  details={"expected_kind": kind})
        if kind == "ReaProject":
            stable_id = revision.project_id
        else:
            stable_id = reference.get("guid")
            if not isinstance(stable_id, str) or not stable_id:
                raise ValidationError("Object references require a non-empty guid",
                                      details={"kind": kind})
        return ReaperHandle(kind=kind, project_id=revision.project_id,
                            change_count=revision.change_count, stable_id=stable_id)
    if isinstance(value, dict) and "$reaper_handle" in value:
        handle = ReaperHandle.from_wire(value)
        if handle.kind != kind:
            raise ValidationError("REAPER handle type does not match API parameter",
                                  details={"expected_kind": kind, "received_kind": handle.kind})
        if (handle.project_id != revision.project_id or
                handle.change_count != revision.change_count):
            raise StaleContext("The supplied API handle is not for the current project revision",
                               details={"handle": handle.to_wire(),
                                        "current_revision": revision.to_dict()})
        if handle.bridge_session and bridge_session_id and handle.bridge_session != bridge_session_id:
            raise StaleContext("Opaque API handles cannot be transferred across REAPER instances")
        return handle
    raise ValidationError("Host-object API parameters need a typed handle or $reaper_ref",
                          details={"expected_kind": kind,
                                   "example": {"$reaper_ref": {"kind": kind,
                                                                 "guid": "object GUID"}}})


@server.tool()
@_guard
def reaper_api_call(name: str,
                    values: list[Any],
                    project_id: str,
                    expected_revision: dict[str, Any],
                    bridge_session_id: str | None = None,
                    timeout_s: float = 30.0,
                    panorama_receipt: str | None = None,
                    archive_root: str | None = None) -> dict[str, Any]:
    """Call any function in the official REAPER ReaScript catalog.

    values are positional and must match the catalog signature exactly.
    Represent host objects as {"$reaper_ref":{"kind":"MediaTrack","guid":"..."}}
    or pass a live $reaper_handle returned by an earlier call. Null is accepted
    only for optional parameters. Arrays use ordinary numeric JSON lists.

    A mutating call returns a delta whose scope is explicitly ``unknown``: the
    server does not pretend to know which tracks a raw API call touched, so all
    project audio evidence is invalidated conservatively.
    """
    if not isinstance(values, list):
        raise ValidationError("values must be a positional JSON array")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
        raise ValidationError("timeout_s must be positive")
    catalog = get_api_catalog()
    signature = catalog.get(name)
    expected = _revision(expected_revision, project_id=project_id)
    session = _session(project_id, bridge_session_id)
    _current_context(session, expected)
    if not getattr(_API_GATE_BYPASS, "enabled", False):
        _require_panorama_receipt(panorama_receipt, project_id, expected.to_dict(),
                                  archive_root)
    if len(values) != len(signature.parameters):
        raise ValidationError("Wrong number of API arguments",
                              details={"name": name,
                                       "expected_count": len(signature.parameters),
                                       "received_count": len(values),
                                       "signature": signature.raw_lua_signature})
    prepared = []
    for parameter, value in zip(signature.parameters, values):
        if parameter.value_type.is_host_handle:
            value = _handle_argument(value, kind=parameter.value_type.name,
                                     revision=expected,
                                     bridge_session_id=bridge_session_id)
        elif isinstance(value, dict) and ("$reaper_ref" in value or "$reaper_handle" in value):
            raise ValidationError("A host handle was supplied for a scalar API parameter",
                                  details={"name": name, "parameter": parameter.name})
        prepared.append(value)
    result = session.bridge.call_api(name, *prepared, timeout_s=float(timeout_s))
    before_value = result.project_before if isinstance(result.project_before, Mapping) else None
    after_value = result.project_after if isinstance(result.project_after, Mapping) else None
    delta: dict[str, Any] | None = None
    if isinstance(after_value, Mapping):
        after_revision = ProjectRevision.from_dict(dict(after_value))
        if after_revision != expected:
            delta = _record_mutation_delta(
                lifecycle=_panorama_lifecycle(archive_root), kind="api_call",
                receipt=str(panorama_receipt), project_id=project_id,
                base_revision=expected, session=session, scope="unknown",
                operation_facts=[{
                    "operation_index": 0, "kind": name,
                    "target_guid": None,
                    "before": {"change_count": expected.change_count},
                    "after": {"change_count": after_revision.change_count},
                    "changes_state": True,
                }])
        else:
            delta = {"status": "not_modified", "acknowledgeable": False,
                     "project_id": project_id,
                     "revision": after_revision.to_dict(),
                     "note": "the API call did not change the observed project revision"}
    return {"name": result.name,
            "returns": list(result.returns),
            "arrays": {str(index): list(items) for index, items in result.arrays.items()},
            "project_before": before_value,
            "project_after": after_value,
            "expected_revision": expected.to_dict(),
            "delta": delta,
            "note": ("A raw API call has unknown mutation scope: the delta invalidates "
                     "project audio conservatively and never claims a precise untouched "
                     "subset. Re-read the project context before claims."),
            }


@server.tool()
@_guard
def reaper_action(mode: str,
                  project_id: str | None = None,
                  expected_revision: dict[str, Any] | None = None,
                  section_id: int = 0,
                  query_object: str | None = None,
                  query_operation: str | None = None,
                  command_id: int | None = None,
                  bridge_session_id: str | None = None,
                  panorama_receipt: str | None = None,
                  archive_root: str | None = None) -> dict[str, Any]:
    """Search a REAPER Action or invoke it against an identified revision.

    Search requires query_object and query_operation. Main-section invocation
    uses revision-bound Main_OnCommandEx. MIDI-editor section 32060 uses the
    active MIDI editor and also requires expected_revision. An invocation that
    changes state returns a delta with scope ``unknown`` and conservative audio
    invalidation.
    """
    if mode not in {"search", "invoke"}:
        raise ValidationError("mode must be search or invoke")
    if isinstance(section_id, bool) or not isinstance(section_id, int):
        raise ValidationError("section_id must be an integer")
    bridge = ReaperBridge(bridge_session_id=bridge_session_id)
    if mode == "search":
        if not isinstance(query_object, str) or not query_object.strip() or not isinstance(query_operation, str) or not query_operation.strip():
            raise ValidationError("Action search needs query_object and query_operation")
        return reaper_api_search(object=query_object, operation=query_operation,
                                 kind="actions", section_id=section_id,
                                 bridge_session_id=bridge_session_id)
    if project_id is None or expected_revision is None:
        raise ValidationError("invoke requires project_id and expected_revision")
    if isinstance(command_id, bool) or not isinstance(command_id, int) or command_id <= 0:
        raise ValidationError("invoke requires a positive numeric command_id")
    expected = _revision(expected_revision, project_id=project_id)
    session = _session(project_id, bridge_session_id)
    _current_context(session, expected)
    _require_panorama_receipt(panorama_receipt, project_id, expected.to_dict(),
                              archive_root)
    project_handle = ReaperHandle(kind="ReaProject", project_id=project_id,
                                  change_count=expected.change_count,
                                  stable_id=project_id)
    if section_id == 0:
        result = session.bridge.call_api("Main_OnCommandEx", command_id, 0,
                                         project_handle)
    elif section_id == 32060:
        editor = session.bridge.call_api("MIDIEditor_GetActive")
        if not editor.returns or not isinstance(editor.returns[0], ReaperHandle):
            raise ValidationError("No active MIDI editor is available")
        if editor.returns[0].project_id != project_id or editor.returns[0].change_count != expected.change_count:
            raise StaleContext("The active MIDI editor is not bound to the requested revision")
        result = session.bridge.call_api("MIDIEditor_OnCommand", editor.returns[0], command_id)
    else:
        raise ValidationError("Action invocation supports section 0 or MIDI Editor section 32060",
                              details={"supported_sections": [0, 32060]})
    after = ProjectRevision.from_dict(session.snapshot().get("revision", {}))
    delta: dict[str, Any] | None = None
    if after != expected:
        delta = _record_mutation_delta(
            lifecycle=_panorama_lifecycle(archive_root), kind="action",
            receipt=str(panorama_receipt), project_id=project_id,
            base_revision=expected, session=session, scope="unknown",
            operation_facts=[{
                "operation_index": 0, "kind": f"action:{command_id}",
                "target_guid": None,
                "before": {"change_count": expected.change_count},
                "after": {"change_count": after.change_count},
                "changes_state": True,
            }])
    else:
        delta = {"status": "not_modified", "acknowledgeable": False,
                 "project_id": project_id, "revision": after.to_dict(),
                 "note": "the Action did not change the observed project revision"}
    return {"command_id": command_id, "section_id": section_id,
            "returns": list(result.returns),
            "project_before": expected.to_dict(),
            "project_after": after.to_dict(),
            "state_changed": after != expected,
            "delta": delta,
            "verification": ("Actions have unknown mutation scope: the delta invalidates "
                             "project audio conservatively; re-read the affected project "
                             "objects and capture before interpreting audio. Actions are "
                             "not assumed to be undoable by this server.")}


@server.tool()
@_guard
def reaper_save(mode: str,
                project_id: str,
                expected_revision: dict[str, Any],
                path: str | None = None,
                overwrite_existing: bool = False,
                bridge_session_id: str | None = None,
                panorama_receipt: str | None = None,
                archive_root: str | None = None) -> dict[str, Any]:
    """Save through REAPER. save_as is absolute .RPP and refuses overwrite by default.

    A save that moves the revision or path sets ``context_invalidated`` and
    returns no delta: the previous token cannot be reused, and no facts about a
    new document are invented.
    """
    if mode not in {"save_current", "save_as"}:
        raise ValidationError("mode must be save_current or save_as")
    if not isinstance(overwrite_existing, bool):
        raise ValidationError("overwrite_existing must be boolean")
    expected = _revision(expected_revision, project_id=project_id)
    session = _session(project_id, bridge_session_id)
    before = _current_context(session, expected)
    _require_panorama_receipt(panorama_receipt, project_id, expected.to_dict(),
                              archive_root)
    if mode == "save_current":
        if path is not None or overwrite_existing:
            raise ValidationError("path and overwrite_existing only apply to save_as")
        result = session.save_current()
    else:
        if not path:
            raise ValidationError("save_as requires path")
        result = session.save_as(path, overwrite_existing=overwrite_existing)
    after = session.snapshot()
    after_revision = after.get("revision") or {}
    changed_identity = (ProjectRevision.from_dict(after_revision) != expected)
    result_value = {
        "result": result,
        "revision_before": before.get("revision"),
        "revision_after": after_revision,
        "saved": result.get("saved") is True,
        "context_invalidated": changed_identity,
    }
    if changed_identity:
        result_value["delta"] = None
        result_value["next_call"] = {
            "tool": "reaper_capture_snapshot",
            "project_id": str(after_revision.get("project_id") or project_id),
            "then": ("Saving moved the project revision or path; the current context "
                     "token no longer matches. Capture again or refresh the structural "
                     "context before editing. No factual delta is invented for a save."),
        }
    return result_value


@server.tool()
@_guard
def reaper_release(analysis_id: str) -> dict[str, Any]:
    """Release one analysis or render-candidate lease and its temporary WAVs now."""
    if not isinstance(analysis_id, str) or not analysis_id:
        raise ValidationError("analysis_id must be a non-empty string")
    return {"analysis_id": analysis_id,
            "released": _cleanup_analysis_record(analysis_id)}


@server.resource("reaper://capabilities/index", name="REAPER capability index",
                 description="Compact domain map for the full documented API and native library features.",
                 mime_type="application/json")
def capability_index_resource() -> str:
    return json.dumps(_capability_summary(), ensure_ascii=False, separators=(",", ":"))


@server.resource("reaper://capabilities/{domain}", name="REAPER API domain",
                 description="Pageless complete list of documented function names in one domain.",
                 mime_type="application/json")
def capability_domain_resource(domain: str) -> str:
    domains = _api_domains()
    if domain not in domains:
        raise ValidationError("Unknown API domain", details={"domain": domain})
    catalog = get_api_catalog()
    return json.dumps({"domain": domain,
                       "documentation_version": catalog.documentation_version,
                       "functions": [catalog.get(name).to_dict(detail="summary")
                                     for name in domains[domain]]},
                      ensure_ascii=False, separators=(",", ":"))


@server.resource("reaper://api/{name}", name="REAPER API signature",
                 description="Full official catalog entry for one ReaScript function.",
                 mime_type="application/json")
def api_signature_resource(name: str) -> str:
    return json.dumps(_api_resource(name), ensure_ascii=False, separators=(",", ":"))


@server.resource("reaper://projects/{bridge_session_id}/{project_id}/context/{change_count}",
                 name="Current REAPER project context",
                 description="Re-read a project snapshot only if it still has the requested state-change count.",
                 mime_type="application/json")
def project_context_resource(bridge_session_id: str, project_id: str,
                             change_count: str) -> str:
    if not change_count.isdigit():
        raise ValidationError("change_count must be a non-negative integer")
    session = _session(project_id, bridge_session_id)
    context = session.snapshot()
    revision = ProjectRevision.from_dict(context.get("revision", {}))
    if revision.project_id != project_id or revision.change_count != int(change_count):
        raise StaleContext("Project context resource refers to an older revision",
                           details={"requested_project_id": project_id,
                                    "requested_change_count": int(change_count),
                                    "current_revision": revision.to_dict()})
    return json.dumps(_jsonable(context), ensure_ascii=False, separators=(",", ":"))


@server.resource("reaper://analyses/{analysis_id}/report", name="Audio analysis report",
                 description="Full measurement and comparison report for one leased analysis.",
                 mime_type="application/json")
def analysis_report_resource(analysis_id: str) -> str:
    with _LOCK:
        record = _analysis_records.get(analysis_id)
    if record is None or record["expires_at"] <= time.time():
        _cleanup_analysis_record(analysis_id)
        raise ValidationError("Analysis report lease has expired or was released",
                              details={"analysis_id": analysis_id})
    body = json.dumps(_jsonable(record["report"]), ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)
    if len(body.encode("utf-8")) > _MAX_REPORT_BYTES:
        raise ValidationError("Analysis report exceeds the resource transfer limit",
                              details={"limit_bytes": _MAX_REPORT_BYTES})
    return body


@server.resource("reaper://analyses/{analysis_id}/audio/{source_guid}",
                 name="Temporary REAPER analysis audio",
                 description="Processed rendered WAV for one track or the master; outside the project and lease-managed.",
                 mime_type="audio/wav")
def analysis_audio_resource(analysis_id: str, source_guid: str) -> bytes:
    with _LOCK:
        record = _analysis_records.get(analysis_id)
    if record is None or record["expires_at"] <= time.time():
        _cleanup_analysis_record(analysis_id)
        raise ValidationError("Audio resource lease has expired or was released",
                              details={"analysis_id": analysis_id})
    audio = _analysis_artifact_path(record, source_guid).read_bytes()
    expected = record.get("archive_audio_hashes", {}).get(source_guid)
    if isinstance(expected, str) and hashlib.sha256(audio).hexdigest() != expected:
        raise ValidationError("Archived audio resource failed its content hash check",
                              details={"source_guid": source_guid,
                                       "capture_id": record.get("capture_id")})
    return audio


@server.tool()
@_guard
def reaper_flamingo_status() -> dict[str, Any]:
    """Report whether Music Flamingo can run here: GPU, model files, llama.cpp binary."""
    from .analysis import flamingo as fl
    st = fl.gpu_status()
    return {
        "ok": bool(st.get("ok")),
        "gpu": st,
        "cuda": fl.cuda_devices(),
        "model": {"path": str(fl.MODEL_FILE), "exists": fl.MODEL_FILE.exists()},
        "mmproj": {"path": str(fl.MMPROJ_FILE), "exists": fl.MMPROJ_FILE.exists()},
        "binary": {"name": fl.LLAMA_BIN,
                   "resolved": fl.llama_binary()},
        "windows": {"window_s": fl.WINDOW_S, "max_total_s": fl.MAX_TOTAL_S},
    }


def _api_call(ctx, name, values):
    previous = getattr(_API_GATE_BYPASS, "enabled", False)
    _API_GATE_BYPASS.enabled = True
    try:
        return reaper_api_call(name=name, values=values,
                               project_id=ctx["revision"]["project_id"],
                               expected_revision=ctx["revision"])["returns"]
    finally:
        _API_GATE_BYPASS.enabled = previous


def _track_send_destinations(ctx, track_guid):
    """Destination GUIDs of a track's normal sends (routing, not names)."""
    ref = {"$reaper_ref": {"kind": "MediaTrack", "guid": track_guid}}
    count = _api_call(ctx, "GetTrackNumSends", [ref, 0])[0]
    out = []
    for index in range(int(count)):
        dest = _api_call(ctx, "GetTrackSendInfo_Value", [ref, 0, index, "P_DESTTRACK"])[0]
        guid = _api_call(ctx, "GetTrackGUID", [dest])[0]
        out.append(guid)
    return out


def _render_isolated_track(session, ctx, key, start_s, end_s):
    """Render one track alone, decided by actual routing.

    A track is kept audible together with every track it feeds (transitively), so a
    bus or a folder parent is never muted out from under the target. Every other track
    is muted, then the exact pre-existing mute/solo state is restored and verified.
    """
    tracks = ctx["tracks"]
    target = next((t for t in tracks if t["name"] == key or t["guid"] == key), None)
    if target is None:
        raise ValidationError("unknown track", details={"key": key,
                                                        "tracks": [t["name"] for t in tracks]})

    by_guid = {t["guid"]: t for t in tracks}
    keep = {target["guid"]}
    frontier = [target["guid"]]
    seen = set()
    while frontier:
        guid = frontier.pop()
        if guid in seen:
            continue
        seen.add(guid)
        for dest in _track_send_destinations(ctx, guid):
            if dest in by_guid and dest not in keep:
                keep.add(dest)
                frontier.append(dest)

    mute_ops, mute_restore = [], []
    solo_ops, solo_restore = [], []
    snapshot = {}
    for t in tracks:
        snapshot[t["guid"]] = {"mute": bool(t.get("mute")), "solo": int(t.get("solo") or 0)}
        want_mute = t["guid"] not in keep
        if bool(t.get("mute")) != want_mute:
            mute_ops.append({"kind": "track.set", "target_guid": t["guid"],
                             "values": {"mute": want_mute}})
        mute_restore.append({"kind": "track.set", "target_guid": t["guid"],
                             "values": {"mute": snapshot[t["guid"]]["mute"]}})
        if int(t.get("solo") or 0) != 0:
            solo_ops.append({"kind": "track.set", "target_guid": t["guid"],
                             "values": {"solo": 0}})
            solo_restore.append({"kind": "track.set", "target_guid": t["guid"],
                                 "values": {"solo": snapshot[t["guid"]]["solo"]}})

    def apply(ops, reason):
        if not ops:
            return
        ctx_now = session.snapshot(include_fx_params=False)
        prev = reaper_edit(mode="preview", project_id=ctx_now["revision"]["project_id"],
                           expected_revision=ctx_now["revision"], operations=ops, reason=reason)
        if not prev.get("ok", True):
            raise ValidationError(f"{reason} failed", details=prev)
        reaper_edit(mode="apply", project_id=ctx_now["revision"]["project_id"],
                    expected_revision=ctx_now["revision"],
                    plan_id=prev["plan"]["plan_id"], reason=reason)

    apply(solo_ops, "clear solo for isolation")
    apply(mute_ops, "temporary track isolation")
    try:
        fresh = session.snapshot(include_fx_params=False)
        return _flamingo_render(session, fresh, start_s, end_s), target
    finally:
        apply(mute_restore + solo_restore, "restore mute/solo after isolation")
        check = session.snapshot(include_fx_params=False)
        drifted = []
        for t in check["tracks"]:
            was = snapshot.get(t["guid"])
            if was is None:
                continue
            if bool(t.get("mute")) != was["mute"] or int(t.get("solo") or 0) != was["solo"]:
                drifted.append(t["name"])
        if drifted:
            from .analysis.flamingo import FlamingoError
            raise FlamingoError("ISOLATION_RESTORE_FAILED",
                                "mute/solo state did not return to its original values",
                                {"tracks": drifted})


def _flamingo_render(session, ctx, start_s, end_s):
    import tempfile
    from pathlib import Path
    tmp = tempfile.mkdtemp(prefix="flamingo-render-")
    session.bridge.capture_render(expected_revision=ctx["revision"], output_dir=tmp,
                                  job_id="flamingo", expected_track_count=ctx["track_count"],
                                  timeout_s=1800.0,
                                  section_start_s=start_s, section_end_s=end_s,
                                  master_only=True)
    wavs = sorted([p for p in Path(tmp).glob("*.wav")], key=lambda p: p.stat().st_mtime)
    return wavs[-1]


def _project_summary(ctx: dict[str, Any]) -> dict[str, Any]:
    """Compact, explicit project state bound to the analysed audio."""
    tracks = []
    for t in ctx.get("tracks", []):
        tracks.append({
            "index": t.get("index"), "name": t.get("name"),
            "volume": t.get("volume"), "pan": t.get("pan"),
            "mute": bool(t.get("mute")), "solo": t.get("solo"),
            "sends": t.get("sends"), "receives": t.get("receives"),
            "fx": [f.get("name") for f in (t.get("fx") or [])],
            "item_count": len(t.get("items") or []),
        })
    master = ctx.get("master") or {}
    return {"name": ctx.get("name"), "path": ctx.get("path"),
            "revision": ctx.get("revision"), "tempo": ctx.get("tempo"),
            "track_count": ctx.get("track_count"), "tracks": tracks,
            "master_fx": [f.get("name") for f in (master.get("fx") or [])]}


@server.tool()
@_guard
def reaper_flamingo_describe(target: str = "master",
                             start_s: float | None = None,
                             end_s: float | None = None,
                             representations: list[str] | None = None,
                             reference_path: str | None = None,
                             prompt: str | None = None,
                             project_id: str | None = None,
                             bridge_session_id: str | None = None,
                             panorama_receipt: str | None = None,
                             archive_root: str | None = None) -> dict[str, Any]:
    """Describe audio with Music Flamingo (GPU only).

    target: "master" renders the project master through its FX; "file:<path>" uses an
    existing audio file (for example a reference). The model hears one mono 16 kHz
    signal per run; L/R/Mid/Side are separate labelled runs and stereo facts come from
    the DSP measurements, not from the model.
    """
    import shutil
    from .analysis import flamingo as fl
    fl.require_gpu()          # fail fast: never render or touch the project without a GPU
    if (start_s is None) != (end_s is None):
        raise ValidationError("start_s and end_s must be supplied together")
    if target.startswith("file:"):
        path = target[5:]
        report = fl.describe(path, prompt=prompt, start_s=start_s or 0.0, end_s=end_s,
                             representations=representations,
                             reference_path=reference_path)
        return {"ok": True, "report": report}
    if target != "master" and not target.startswith("track:"):
        raise ValidationError("target must be 'master', 'track:<name|guid>' or 'file:<path>'")
    session = _session(project_id, bridge_session_id)
    ctx = session.snapshot(include_fx_params=False)
    _require_interpretation_receipt(
        panorama_receipt, str(ctx.get("revision", {}).get("project_id") or ""),
        ctx.get("revision"), archive_root,
    )

    if target.startswith("track:"):
        wav, tinfo = _render_isolated_track(session, ctx, target[6:], start_s, end_s)
        try:
            dsp = fl.quick_dsp(wav)
            report = fl.describe(wav, prompt=prompt, start_s=0.0, end_s=None,
                                 representations=representations,
                                 reference_path=reference_path)
        finally:
            shutil.rmtree(wav.parent, ignore_errors=True)
        report["target"] = {"track": tinfo.get("name"), "guid": tinfo.get("guid"),
                            "volume": tinfo.get("volume"), "pan": tinfo.get("pan"),
                            "fx": [f.get("name") for f in (tinfo.get("fx") or [])]}
        report["dsp"] = dsp
        report["project"] = {"name": ctx.get("name"), "path": ctx.get("path"),
                             "revision": ctx.get("revision"), "tempo": ctx.get("tempo")}
        report["note"] = ("Track rendered alone by temporarily muting the rest; mutes were "
                          "restored immediately. " + report.get("note", ""))
        return {"ok": True, "report": report}

    wav = _flamingo_render(session, ctx, start_s, end_s)
    try:
        dsp = fl.quick_dsp(wav)
        report = fl.describe(wav, prompt=prompt, start_s=0.0, end_s=None,
                             representations=representations,
                             reference_path=reference_path)
    finally:
        shutil.rmtree(wav.parent, ignore_errors=True)
    report["target"] = "master"
    report["dsp"] = dsp
    report["project"] = {"name": ctx.get("name"), "path": ctx.get("path"),
                         "revision": ctx.get("revision"), "tempo": ctx.get("tempo")}
    report["project_context"] = _project_summary(ctx)
    report["note"] = ("Temporary render removed after analysis; the project was not modified. "
                      + report.get("note", ""))
    return {"ok": True, "report": report}


@server.tool()
@_guard
def reaper_flamingo_ask(question: str,
                        target: str = "master",
                        start_s: float | None = None,
                        end_s: float | None = None,
                        representations: list[str] | None = None,
                        reference_path: str | None = None,
                        project_id: str | None = None,
                        bridge_session_id: str | None = None,
                        panorama_receipt: str | None = None,
                        archive_root: str | None = None) -> dict[str, Any]:
    """Ask Music Flamingo a free-form question about the master or a file, optionally bounded."""
    if not isinstance(question, str) or not question.strip():
        raise ValidationError("question must be a non-empty string")
    return reaper_flamingo_describe(target=target, start_s=start_s, end_s=end_s,
                                    representations=representations,
                                    reference_path=reference_path,
                                    prompt=question.strip(),
                                    project_id=project_id,
                                    bridge_session_id=bridge_session_id,
                                    panorama_receipt=panorama_receipt,
                                    archive_root=archive_root)


@server.tool()
@_guard
def reaper_spatial_status() -> dict[str, Any]:
    """Ossicle-4B backend: weights, revision, licence, interpreter and GPU.

    Reports whether every file of the checkpoint matches its pinned size/sha256, whether
    the model interpreter sees a Tesla V100, and whether its transformers knows the
    qwen3_5 text model this checkpoint needs. Nothing is loaded or executed here.
    """
    from .analysis import ossicle as ms
    return ms.status()


@server.tool()
@_guard
def reaper_spatial_analyze(question: str | None = None,
                           target: str = "master",
                           start_s: float | None = None,
                           end_s: float | None = None,
                           allow_mono: bool = False,
                           quantize: str = "none",
                           with_timeline: bool = True,
                           project_id: str | None = None,
                           bridge_session_id: str | None = None,
                           panorama_receipt: str | None = None,
                           archive_root: str | None = None) -> dict[str, Any]:
    """Ask Ossicle-4B about the real L/R channels of a stereo source (GPU only).

    target: "master" renders the master through its FX as stereo; "track:<name>" renders
    that track alone (its pan and level included); "file:<path>" uses a stereo file.
    The two channels are fed as a genuine (2, T) binaural signal - never summed, never
    concatenated. Mono sources are refused unless allow_mono is set, and a mono render
    carries no directional information even then. Sections are capped at 120 s: past that
    the model streams the clip and stops using its stereo adapter.

    quantize: "none" keeps fp16 weights (9.9 GB), "int4" uses the checkpoint's own
    group-wise 4-bit layers (3.6 GB) - useful when a long section needs the VRAM.
    """
    import shutil
    from .analysis import ossicle as ms
    ms.require_gpu()          # fail fast: no render, no project touch without a GPU
    question = (question or ms.DEFAULT_QUESTION).strip()
    if (start_s is None) != (end_s is None):
        raise ValidationError("start_s and end_s must be supplied together")

    if target.startswith("file:"):
        result = ms.analyze([{"wav": target[5:], "question": question.strip(),
                              "start_s": start_s or 0.0, "end_s": end_s,
                              "allow_mono": bool(allow_mono)}], quantize=quantize)
        return {"ok": True, "report": result}

    if target != "master" and not target.startswith("track:"):
        raise ValidationError("target must be 'master', 'track:<name|guid>' or 'file:<path>'")
    session = _session(project_id, bridge_session_id)
    ctx = session.snapshot(include_fx_params=False)
    _require_interpretation_receipt(
        panorama_receipt, str(ctx.get("revision", {}).get("project_id") or ""),
        ctx.get("revision"), archive_root,
    )
    timeline_text = None
    if with_timeline:
        from .analysis import timeline_context as tc
        items, markers = _timeline_items_markers(ctx, start_s, end_s)
        timeline_text = tc.build(ctx, start_s, end_s, items=items, markers=markers)
        question = timeline_text + "\n\nQUESTION: " + question.strip()
    meta: dict[str, Any] = {"target": target}
    if target.startswith("track:"):
        wav, tinfo = _render_isolated_track(session, ctx, target[6:], start_s, end_s)
        meta["track"] = {"name": tinfo.get("name"), "guid": tinfo.get("guid"),
                         "volume": tinfo.get("volume"), "pan": tinfo.get("pan"),
                         "fx": [f.get("name") for f in (tinfo.get("fx") or [])]}
    else:
        wav = _flamingo_render(session, ctx, start_s, end_s)
    try:
        result = ms.analyze([{"wav": str(wav), "question": question.strip(),
                              "start_s": 0.0, "end_s": None,
                              "allow_mono": bool(allow_mono)}], quantize=quantize)
    finally:
        shutil.rmtree(wav.parent, ignore_errors=True)
    result["target"] = meta
    result["project"] = {"name": ctx.get("name"), "path": ctx.get("path"),
                         "revision": ctx.get("revision"), "tempo": ctx.get("tempo")}
    result["timeline_context"] = timeline_text
    result["note"] = ("Temporary stereo render removed after analysis; the project was not "
                      "modified. Spatial answers are model statements, not measurements: "
                      "compare them with the pan/level values above.")
    return {"ok": True, "report": result}


@server.tool()
@_guard
def reaper_spatial_timeline(target: str = "master",
                            start_s: float | None = None,
                            end_s: float | None = None,
                            window_s: float = 30.0,
                            hop_s: float | None = None,
                            project_id: str | None = None,
                            bridge_session_id: str | None = None,
                            panorama_receipt: str | None = None,
                            archive_root: str | None = None) -> dict[str, Any]:
    """Measured width/balance profile across the timeline (no model, no GPU).

    Stereo only: renders the master (or a track, or a file) and reports per-window RMS,
    balance, correlation, side/mid and per-band correlation. This is the measured half
    of "what happens over time"; a spatial-model caption can be compared against it later.
    """
    import shutil
    from .analysis import spatial_timeline as st
    if target.startswith("file:"):
        return {"ok": True, "report": st.timeline(target[5:], window_s=window_s,
                                                  hop_s=hop_s, start_s=start_s or 0.0,
                                                  end_s=end_s)}
    if target != "master" and not target.startswith("track:"):
        raise ValidationError("target must be 'master', 'track:<name|guid>' or 'file:<path>'")
    session = _session(project_id, bridge_session_id)
    ctx = session.snapshot(include_fx_params=False)
    _require_interpretation_receipt(
        panorama_receipt, str(ctx.get("revision", {}).get("project_id") or ""),
        ctx.get("revision"), archive_root,
    )
    if target.startswith("track:"):
        wav, tinfo = _render_isolated_track(session, ctx, target[6:], start_s, end_s)
    else:
        wav = _flamingo_render(session, ctx, start_s, end_s)
        tinfo = None
    try:
        report = st.timeline(wav, window_s=window_s, hop_s=hop_s)
    finally:
        shutil.rmtree(wav.parent, ignore_errors=True)
    report["target"] = {"kind": "track", "track": tinfo.get("name")} if tinfo else "master"
    report["project"] = {"name": ctx.get("name"), "revision": ctx.get("revision")}
    report["note"] = ("Measured from a temporary stereo render that was deleted afterwards; "
                      "the project was not modified. " + report.get("note", ""))
    return {"ok": True, "report": report}


def _timeline_items_markers(ctx: dict[str, Any], start_s: float | None,
                            end_s: float | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Items overlapping the range and project markers, straight from the bridge."""
    proj = {"$reaper_ref": {"kind": "ReaProject", "guid": ctx["revision"]["project_id"]}}
    items: list[dict[str, Any]] = []
    for t in ctx.get("tracks", []):
        try:
            handle = _api_call(ctx, "GetTrack", [proj, t["index"]])[0]
            count = int(_api_call(ctx, "CountTrackMediaItems", [handle])[0])
        except Exception:                       # noqa: BLE001
            continue
        for i in range(min(count, 20)):
            try:
                item = _api_call(ctx, "GetTrackMediaItem", [handle, i])[0]
                pos = float(_api_call(ctx, "GetMediaItemInfo_Value", [item, "D_POSITION"])[0])
                length = float(_api_call(ctx, "GetMediaItemInfo_Value", [item, "D_LENGTH"])[0])
            except Exception:                   # noqa: BLE001
                continue
            if start_s is not None and (pos >= end_s or pos + length <= start_s):
                continue
            items.append({"track": t["name"], "start": round(pos, 2),
                          "end": round(pos + length, 2)})
    markers: list[dict[str, Any]] = []
    try:
        total = int(_api_call(ctx, "CountProjectMarkers", [proj])[0])
        for i in range(min(total, 20)):
            row = _api_call(ctx, "EnumProjectMarkers3", [proj, i])
            if isinstance(row, list) and len(row) >= 5:
                markers.append({"time_s": float(row[2]), "name": row[4]})
    except Exception:                           # noqa: BLE001
        pass
    return items, markers


@server.tool()
@_guard
def reaper_timeline_context(start_s: float | None = None,
                            end_s: float | None = None,
                            project_id: str | None = None,
                            bridge_session_id: str | None = None,
                            panorama_receipt: str | None = None,
                            archive_root: str | None = None) -> dict[str, Any]:
    """Arrangement context as text: tempo map, bar numbers, items, markers, pan/level.

    This is the "timeline" that can be handed to an audio model together with the request.
    No model and no GPU are involved; it only reads the open project.
    """
    from .analysis import timeline_context as tc
    session = _session(project_id, bridge_session_id)
    ctx = session.snapshot(include_fx_params=False)
    _require_interpretation_receipt(
        panorama_receipt, str(ctx.get("revision", {}).get("project_id") or ""),
        ctx.get("revision"), archive_root,
    )
    items, markers = _timeline_items_markers(ctx, start_s, end_s)
    text = tc.build(ctx, start_s, end_s, items=items, markers=markers)
    return {"ok": True, "context": text,
            "project": {"name": ctx.get("name"), "revision": ctx.get("revision")},
            "counts": {"items": len(items), "markers": len(markers)}}


@server.tool()
@_guard
def reaper_capture_snapshot(project_id: str,
                            expected_revision: dict[str, Any],
                            archive_root: str,
                            start_s: float | None = None,
                            end_s: float | None = None,
                            channels: str = "stereo",
                            sample_rate: int | None = None,
                            tail_ms: int = 0,
                            analysis_workers: int | None = None,
                            bridge_session_id: str | None = None) -> dict[str, Any]:
    """Capture a complete project snapshot and append it to a durable archive.

    The capture contains the full project map (tracks/buses/master, items/takes,
    MIDI, FX and parameters, routing, automation/envelopes, tempo/time signatures,
    markers/regions, selection/transport), one post-FX audio asset per project
    track and one master, and an analysis record for every asset. Temporary
    WAVs are copied into a content-addressed SHA-256 archive store and verified
    before the capture is committed; identical assets share one stored blob.

    Master renders are fail-closed: the master signal is checked byte-level
    from peak/RMS; one bounded retry is allowed, every failed attempt is
    preserved as archive evidence, and persistent silence returns
    ``capture_master_invalid`` instead of an ``ok=true`` result.

    The bootstrap call returns capture/archive metadata only.  It deliberately
    does not return the panorama payload: call ``reaper_panorama_open`` for the
    new capture, fetch every page it announces with ``reaper_panorama_page``,
    then ``reaper_panorama_ack`` with every page hash before interpreting.
    """
    _focus_reaper_window()
    expected = _revision(expected_revision, project_id=project_id)
    session = _session(project_id, bridge_session_id)
    _current_context(session, expected)
    if (start_s is None) != (end_s is None):
        raise ValidationError("start_s and end_s must both be provided or both be null")
    section = None if start_s is None else (float(start_s), float(end_s))
    service = FullCaptureService(session, archive_root)
    result = service.capture(section=section, channels=channels,
                             sample_rate=sample_rate, tail_ms=tail_ms,
                             analysis_workers=analysis_workers)
    serialization_start = time.perf_counter()
    safe = _jsonable(result)
    # The bootstrap capture creates the document, but it must not become a
    # second raw reading path.  The model opens the exact document and receives
    # the whole view with its receipt; only capture/archive metadata is returned.
    safe.pop("panorama", None)
    safe["panorama_delivery"] = {
        "view_delivered": False,
        "interpretation_allowed": False,
        "complete_view_required": True,
        "reason": ("bootstrap capture returns metadata only; open the panorama to receive "
                   "page 1 and the manifest, fetch every page, then acknowledge them"),
        "next_action": {
            "tool": "reaper_panorama_open",
            "project_id": project_id,
            "archive_root": archive_root,
            "capture_id": safe.get("capture_id"),
            "returns": ["page_1", "manifest"],
            "then": ("fetch every page_id with reaper_panorama_page, then call "
                     "reaper_panorama_ack with every page_hash in order"),
        },
    }
    serialization_end = time.perf_counter()
    timings = safe.setdefault("timings", {})
    timings["mcp_serialization_s"] = round(serialization_end - serialization_start, 3)
    return safe


def _read_capture_panorama(capture_id: str, archive_root: str,
                           record: FullCaptureRecord | None = None,
                           *, delivered_receipt: str | None = None) -> dict[str, Any]:
    """Internal archive reader used by tests/archive tooling.

    This is deliberately not an MCP tool.  The model-facing
    ``reaper_capture_read`` wrapper requires an exact-capture receipt before it
    calls this helper.
    """
    archive = CaptureArchive(archive_root)
    if record is None:
        record = archive.read(capture_id)
    status = archive.status(capture_id)
    path = panorama_path_for_record(
        archive.captures / capture_id / "record.json")
    if path.is_file():
        try:
            panorama = load_panorama(path, record, max_bytes=DEFAULT_MAX_BYTES,
                                     max_tokens=DEFAULT_MAX_TOKENS)
            audio_rows = panorama.get("audio")
            has_render_provenance = (
                isinstance(audio_rows, list) and bool(audio_rows) and
                all(isinstance(row, Mapping) and
                    isinstance(row.get("render_provenance"), Mapping) and
                    row["render_provenance"].get("complete") is True
                    for row in audio_rows)
            )
            if panorama.get("schema_version") != PANORAMA_SCHEMA_VERSION:
                # A sealed older sidecar is never rewritten; project the same
                # canonical record into the current schema for this read.
                panorama = build_panorama(
                    record,
                    record_path=str((archive.captures / capture_id / "record.json").relative_to(
                        archive.root)),
                    max_bytes=DEFAULT_MAX_BYTES,
                    max_tokens=DEFAULT_MAX_TOKENS,
                )
                source = "read_time_schema_upgrade"
            elif has_render_provenance:
                source = "sidecar"
            else:
                # A valid pre-contract sidecar may still bind to the right
                # capture while lacking the per-asset render stage/revision.
                # Rebuild it from the sealed record in memory; do not mutate
                # an existing archive during a read.
                panorama = build_panorama(
                    record,
                    record_path=str((archive.captures / capture_id / "record.json").relative_to(
                        archive.root)),
                    max_bytes=DEFAULT_MAX_BYTES,
                    max_tokens=DEFAULT_MAX_TOKENS,
                )
                source = "read_time_stale_sidecar_contract"
        except PanoramaCapacityError:
            # A legacy sidecar may have been written before the one-shot token
            # budget existed.  Project it compactly in memory; never rewrite or
            # truncate the canonical archive on read.
            panorama = build_panorama(
                record,
                record_path=str((archive.captures / capture_id / "record.json").relative_to(
                    archive.root)),
                max_bytes=DEFAULT_MAX_BYTES,
                max_tokens=DEFAULT_MAX_TOKENS,
            )
            source = "read_time_legacy_sidecar_budget"
    else:
        panorama = build_panorama(
            record,
            record_path=str((archive.captures / capture_id / "record.json").relative_to(
                archive.root)),
            max_bytes=DEFAULT_MAX_BYTES,
            max_tokens=DEFAULT_MAX_TOKENS,
        )
        source = "read_time"
    master_validation = (record.timings.get("master_validation")
                         if isinstance(record.timings, Mapping) else None)
    if not isinstance(master_validation, Mapping):
        master_validation = {}
    invalid_reasons: list[str] = []
    if master_validation.get("valid") is False:
        invalid_reasons.append("master_signal_invalid")
    if record.missing:
        invalid_reasons.append("capture_missing_fields")
    if not all(bool(value) for value in record.coverage.values()):
        invalid_reasons.append("coverage_incomplete")
    delivery = {
        "view_delivered": bool(delivered_receipt),
        "interpretation_allowed": bool(delivered_receipt),
        "complete_view_required": True,
        "receipt": delivered_receipt,
        "capture_id": record.capture_id,
        "document_hash": document_hash(panorama),
    }
    return {
        "ok": True,
        "panorama": panorama,
        "panorama_source": source,
        "delivery": delivery,
        "capture": {
            "capture_id": record.capture_id,
            "project_id": record.project_id,
            "project_path": record.project_path,
            "schema_version": record.schema_version,
            "integrity_sha256": record.integrity_sha256,
            "revision_before": record.revision_before,
            "revision_after": record.revision_after,
            "time_window": record.time_window,
            "capture_status": "invalid" if invalid_reasons else "complete",
            "invalid_reasons": invalid_reasons,
            "master_validation": dict(master_validation),
            "coverage": dict(record.coverage),
            "missing": list(record.missing),
            "record_path": str(archive.captures / capture_id / "record.json"),
        },
        "retention_status": status["retention_status"],
        "archive": {"capture_id": capture_id, "root": str(archive.root)},
    }



@server.tool()
@_guard
def reaper_capture_read(capture_id: str, archive_root: str,
                        panorama_receipt: str | None = None) -> dict[str, Any]:
    """Read one capture only after an exact-capture complete-view receipt.

    A receipt for the latest capture is not sufficient for a historical capture.
    Without a matching project, capture, revision, fact root and document hash
    this tool fails closed and returns a ``next_action`` pointing to
    ``reaper_panorama_open``. The bootstrap ``reaper_capture_snapshot`` remains
    the only no-receipt capture operation and returns metadata only.
    """
    if not panorama_receipt:
        _require_capture_read_receipt(panorama_receipt, "", None, capture_id,
                                      archive_root)
    archive = CaptureArchive(archive_root)
    record = archive.read(capture_id)
    _require_capture_read_receipt(
        panorama_receipt, record.project_id,
        record.revision_after or record.revision_before,
        record.capture_id, archive_root,
    )
    return _read_capture_panorama(capture_id, archive_root, record=record,
                                  delivered_receipt=panorama_receipt)


@server.tool()
@_guard
def reaper_panorama_open(project_id: str,
                         archive_root: str,
                         expected_revision: dict[str, Any] | None = None,
                         capture_id: str | None = None,
                         page_target_bytes: int | None = None,
                         max_page_bytes: int | None = None) -> dict[str, Any]:
    """Open the whole-project view as pages and return page 1 with the manifest.

    The panorama is fixed to one capture, revision and ``view_sha256``.  The
    manifest lists every ``page_id``, its payload hash and the measured response
    size of each page.  Fetch every page with ``reaper_panorama_page``, then call
    ``reaper_panorama_ack`` with all page hashes in order; only the resulting
    receipt (and its ``context_token``) unlocks analysis and mutations.

    The page count is adaptive: each page contains whole view sections or an
    exact list-boundary split, and the pages reconstruct this bounded panorama
    without further truncation. The panorama itself is a derived projection:
    dense time series may be summarized and linked to exact archived detail.
    Page sizing measures this server's indented-JSON character count, not the
    host's complete MCP transport envelope. A page that cannot fit raises
    instead of being silently truncated.

    ``page_target_bytes`` is the preferred page size (default 30,000) and
    ``max_page_bytes`` is the configured per-page limit (default 46,000).  The
    current measurement is the character count of an indented JSON rendering,
    not UTF-8 wire bytes or the complete MCP transport envelope.  Validate the
    host's actual result limit before raising this setting.
    """
    lifecycle = _panorama_lifecycle(archive_root)
    return lifecycle.open_view(project_id, expected_revision=expected_revision,
                               capture_id=capture_id,
                               page_target_bytes=page_target_bytes,
                               max_page_bytes=max_page_bytes)


@server.tool()
@_guard
def reaper_panorama_page(handle: str, revision: dict[str, Any], archive_root: str,
                         page_id: str) -> dict[str, Any]:
    """Fetch one page of the opened panorama by its stable ``page_id``.

    Pages may be fetched in any order but every page_id in the manifest must be
    fetched before ``reaper_panorama_ack``.  Repeated requests are idempotent and
    return the same page hash.  A page request for another revision fails with
    ``stale_context``; an unknown page_id fails and lists the valid ids.
    """
    lifecycle = _panorama_lifecycle(archive_root)
    return lifecycle.view_page(handle, revision, page_id)


@server.tool()
@_guard
def reaper_panorama_ack(handle: str, revision: dict[str, Any], archive_root: str,
                        page_hashes: list[str]) -> dict[str, Any]:
    """Verify every delivered page and mint the complete-view receipt.

    The server checks that every page was actually fetched on this handle, that
    the supplied hashes match the served pages in order, and that the pages
    reconstruct the exact ``view_sha256``.  A partial page set returns
    ``incomplete`` with the missing page ids and mints no receipt.  The receipt
    proves delivery of these exact pages, not that the model read or understood
    them.
    """
    lifecycle = _panorama_lifecycle(archive_root)
    return lifecycle.ack_pages(handle, revision, page_hashes)


@server.tool()
@_guard
def reaper_panorama_query(revision: dict[str, Any],
                          archive_root: str,
                          handle: str | None = None,
                          data: list[str] | None = None,
                          objects: list[str] | str | None = None,
                          interval: dict[str, float] | None = None,
                          series: list[str] | None = None,
                          metrics: list[str] | None = None,
                          kinds: list[str] | None = None,
                          bands_hz: list[float] | None = None,
                          resolution_s: float | None = None,
                          include_pairs: bool = True,
                          max_response_bytes: int | None = None,
                          panorama_receipt: str | None = None,
                          read_scope: str | None = None) -> dict[str, Any]:
    """Return exact high-frequency rows for a semantic selection, in one result.

    This tool never pages: a selection either arrives whole or returns
    ``query_too_large`` with its measured size.  Paging applies only to the
    project panorama (``reaper_panorama_open`` → ``reaper_panorama_page`` →
    ``reaper_panorama_ack``); acknowledge the panorama first, then name what you
    want here by meaning and the whole coherent fragment arrives at once:

    * ``data``: ``relations`` | ``series`` | ``midi_notes`` | ``envelope_points`` |
      ``items`` | ``takes`` | ``audio`` | ``fx`` | ``analysis`` | ``project_summary``
      (default all);
    * ``objects``: ``"all"``, stable ids (``track:<guid>``, ``analysis:<guid>``, ...),
      raw REAPER GUIDs, or unique track names;
    * ``interval``: ``{start_s, end_s}`` for measured rows inside a section;
    * ``series`` / ``resolution_s``: which exact measurement cells, at what resolution;
    * ``metrics``: analysis column filter; metadata rows/source stay intact;
    * ``kinds`` / ``bands_hz``: relation kinds and band centres; ``include_pairs``
      controls aggregate relation-pair cost.

    Series, MIDI and envelope rows are re-hashed against the sealed detail
    manifest, so a stale or mismatched reference fails closed.  If the selection
    is larger than one response, the call returns ``query_too_large`` with the
    exact size per family and the scope to narrow — never a truncated or paged
    answer.

    Authorization: ``revision`` + ``archive_root`` + ``handle`` +
    ``panorama_receipt`` from the acknowledged panorama. A query without that
    receipt fails closed. The legacy ``read_scope`` argument is accepted only for
    older single-response flows; it is not a receipt, cannot complete a panorama
    and never authorizes an edit/API/action/save call. If a selected fragment is
    still too large, ``query_too_large`` is returned.
    """
    lifecycle = _panorama_lifecycle(archive_root)
    if read_scope:
        if handle or panorama_receipt:
            raise ValidationError(
                "read_scope is a read-only route and cannot be combined with a handle or receipt",
                details={"next_action": ("use either revision+archive_root+read_scope, or "
                                         "revision+archive_root+handle+panorama_receipt")})
        # A budget refusal issues this read-only binding; it is never a
        # full-view receipt and never unlocks an edit/API/action/save call.
        return lifecycle.query(
            "", revision, data=data, objects=objects, interval=interval,
            series=series, metrics=metrics, kinds=kinds, bands_hz=bands_hz,
            resolution_s=resolution_s, include_pairs=include_pairs,
            max_response_bytes=max_response_bytes, read_scope=read_scope)
    if not handle or not panorama_receipt:
        raise ValidationError(
            "A panorama query requires handle and panorama_receipt from an "
            "acknowledged panorama",
            details={"next_action": ("open the panorama, fetch every page and call "
                                     "reaper_panorama_ack, then pass its handle and receipt; "
                                     "the legacy read_scope is accepted only for older flows")})
    project_id = str(revision.get("project_id") or "")
    capture_id = str(revision.get("capture_id") or "")
    _require_capture_read_receipt(
        panorama_receipt, project_id, revision, capture_id, archive_root)
    result = lifecycle.query(
        handle, revision, data=data, objects=objects, interval=interval,
        series=series, metrics=metrics, kinds=kinds, bands_hz=bands_hz,
        resolution_s=resolution_s, include_pairs=include_pairs,
        max_response_bytes=max_response_bytes)
    return result


@server.tool()
@_guard
def reaper_panorama_status(project_id: str,
                           archive_root: str,
                           handle: str | None = None) -> dict[str, Any]:
    """Report the latest captured revision/root and one handle's delivery state."""
    return _panorama_lifecycle(archive_root).status(project_id, handle)


@server.tool()
@_guard
def reaper_panorama_changes_since(project_id: str,
                                  archive_root: str,
                                  base_capture_id: str | None = None,
                                  base_revision: dict[str, Any] | None = None,
                                  base_fact_root: str | None = None,
                                  context_token: str | None = None) -> dict[str, Any]:
    """Return a revision-bound delta from an exact base capture/root.

    Pass the latest ``context_token`` to make the delta advanceable. A delta
    without a token is informational only and cannot unlock mutation. A wrong
    or missing base returns ``stale_base``; a chain longer than the bounded delta
    horizon returns ``refresh_required``. The successor token is returned in the
    same response (``advance.context_token``); no acknowledgement round trip is
    required to continue.
    """
    return _panorama_lifecycle(archive_root).changes_since(
        project_id, base_capture_id=base_capture_id, base_revision=base_revision,
        base_fact_root=base_fact_root, context_token=context_token)


@server.tool()
@_guard
def reaper_panorama_refresh(project_id: str,
                            archive_root: str,
                            expected_revision: dict[str, Any] | None = None,
                            page_target_bytes: int | None = None,
                            max_page_bytes: int | None = None) -> dict[str, Any]:
    """Re-open the current panorama from page 1 after loss or staleness.

    Refresh is a normal operation, not an error path: it returns a new handle,
    page 1 and a fresh manifest.  The old handle, pages and context token must
    not be used to mutate.
    """
    lifecycle = _panorama_lifecycle(archive_root)
    return lifecycle.open_view(project_id, expected_revision=expected_revision,
                               page_target_bytes=page_target_bytes,
                               max_page_bytes=max_page_bytes)


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("REAPER_AUDIO_LOG_LEVEL", "WARNING"))
    server.run(transport="stdio")
