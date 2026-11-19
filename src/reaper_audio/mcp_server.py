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
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass, replace
from functools import wraps
from math import isfinite
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict
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
from .daw.leveling import plan_curve
from .daw.session import EditOperation, EditPlan, ProjectRevision, ProjectSession
from .daw.full_capture import FullCaptureService
from .daw.measure import MeasureService
from .context.decision_gates import evaluate
from .context.mode import load_mode, mode_brief, set_locked, set_mode
from .context.structural_token import mint as mint_structural_token
from .context.structural_token import register_self_noise
from .context.structural_token import require as require_structural_token
from .context.mutation_journal import diff_digests
from .context.evidence import EvidenceStore
from .context.coverage import length_coverage, passport_from_units
from .context.freshness_service import current_fingerprints, freshness_report
from .context.full_capture import CaptureArchive, FullCaptureRecord, _thaw
from .context.analysis_store import (analysis_input_fingerprint,
                                     analysis_locations, fingerprint_digest,
                                     load_analysis_record,
                                     persist_analysis_record,
                                     reference_file_fingerprint)
from .context.analyze_summary import project_analyze_summary
from .context.audio_delta import build_audio_delta
from .context.lifecycle import (PanoramaLifecycle, _delivered_bytes,
                                change_update_view, document_hash, fact_root)
from .context.panorama import (DEFAULT_MAX_BYTES, DEFAULT_MAX_TOKENS,
                               PANORAMA_SCHEMA_VERSION, build_panorama, load_panorama,
                               panorama_path_for_record)
from .daw.param_targets import resolve as resolve_parameter_target
from .context.plugin_state_store import PluginStateStore, chain_fingerprint
from .context.plugin_summary import PluginDescriptor, summarize
from .context.panorama_view import DEFAULT_RESPONSE_BYTES
from .context.revision import digest_evidence
from .context.identity import identity_from_mapping
from .errors import (BridgeUnavailable, PanoramaCapacityError, ReaperAudioError,
                     StaleContext, ValidationError)
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
#: The logical model-facing surface: stable task groups, not one tool per
#: ReaScript function or metric.  ``reaper_capabilities(mode="tools")`` serves
#: this map so a model can find the task first and the exact function later.
#: Stable analyzer IDs for the batched scoped-analysis task.  Cost classes are
#: relative declarations, not measured seconds; a caller can use the class to
#: choose a bundle without pretending it is an exact runtime prediction.
ANALYZER_CATALOG: dict[str, dict[str, Any]] = {
    "metrics": {
        "description": "Core per-source measurements: level, loudness, peaks, spectrum bands, stereo and temporal summaries.",
        "scope": ["source", "interval"],
        "returns": "metrics, curves and per-source measurement context",
        "prerequisites": ["archived capture audio"],
        "cost": {"class": "medium", "basis": "declared relative class; scales with selected source count and interval length"},
        "status": "available",
    },
    "temporal_profile": {
        "description": "Stored temporal profile cells and their exact source/time coordinates.",
        "scope": ["source", "interval"],
        "returns": "temporal profile cells with source and time axis",
        "prerequisites": ["metrics"],
        "cost": {"class": "medium", "basis": "declared relative class; cell count scales with duration"},
        "status": "available",
    },
    "masking": {
        "description": "Pairwise band-dominance evidence from the measured temporal profiles.",
        "scope": ["source_pair", "interval"],
        "returns": "masking_evidence pairs; descriptive, not a psychoacoustic verdict",
        "prerequisites": ["metrics", "at least two selected sources"],
        "cost": {"class": "heavy", "basis": "declared relative class; pairwise growth with selected source count"},
        "status": "available",
    },
    "band_balance": {
        "description": "Band-level balance timeline at aligned measured timestamps.",
        "scope": ["sources", "interval"],
        "returns": "aligned band levels and coverage; no interpolation",
        "prerequisites": ["metrics", "matching time axes"],
        "cost": {"class": "heavy", "basis": "declared relative class; scales with aligned ticks and sources"},
        "status": "available",
    },
    "phase": {
        "description": "Measured pairwise phase correlation and lag for explicitly requested source pairs.",
        "scope": ["source_pair", "interval"],
        "returns": "phase comparison rows and coverage",
        "prerequisites": ["metrics", "explicit source pairs"],
        "cost": {"class": "heavy", "basis": "declared relative class; per pair and interval"},
        "status": "available",
    },
    "partial_loudness": {
        "description": "Measured partial-loudness shares when a mix and source stems are selected.",
        "scope": ["sources", "interval"],
        "returns": "partial loudness shares with its reconstruction gate",
        "prerequisites": ["metrics", "mix source and at least one stem"],
        "cost": {"class": "heavy", "basis": "declared relative class; model/cell count dependent"},
        "status": "available",
    },
    "mix_evidence": {
        "description": "Evidence and questions derived from measured sources and references; never a recipe or score.",
        "scope": ["sources", "interval"],
        "returns": "evidence rows, questions, coverage and source levels",
        "prerequisites": ["metrics"],
        "cost": {"class": "light", "basis": "declared relative class; derived from already measured metrics"},
        "status": "available",
    },
}


TOOL_SURFACE: dict[str, dict[str, Any]] = {
    "session": {
        "purpose": "open, verify, checkpoint or release the REAPER connection",
        "use_when": "at the start of a session, before a risky operation, or on a broken bridge",
        "context": "none (bootstrap); returns the project identity and revision",
        "tools": ["reaper_session", "reaper_checkpoint_save", "reaper_bridge_reload",
                  "reaper_bridge", "reaper_mode", "reaper_structural_token"],
        "not_for": "reading or changing project content",
    },
    "project": {
        "purpose": "read the full project context or save the project",
        "use_when": "you need master, tracks, routing, tempo, items, FX or MIDI before a capture",
        "context": "project_id or bridge_session_id",
        "tools": ["reaper_project", "reaper_timeline_context", "reaper_save"],
        "not_for": "audio measurement or structural edits",
    },
    "capture": {
        "purpose": "materialize one immutable, revision-bound capture of the project audio",
        "use_when": "no archived capture matches the current revision, or a historical capture must be read",
        "context": "exact project revision; reading a capture needs its panorama receipt",
        "tools": ["reaper_capture_snapshot", "reaper_capture_read"],
        "not_for": "interpreting audio; use panorama or analysis",
    },
    "panorama": {
        "purpose": "one whole-project view, exact rows by meaning, and revision-bound change updates",
        "use_when": "before analysis, edits, or any whole-project conclusion",
        "context": "capture_id + archive_root; mutations need the panorama receipt/context token",
        "tools": ["reaper_panorama_open", "reaper_panorama_page", "reaper_panorama_ack",
                  "reaper_panorama_query", "reaper_panorama_status",
                  "reaper_panorama_changes_since", "reaper_panorama_refresh"],
        "not_for": "changing the project or replacing exact audio measurements",
    },
    "analysis": {
        "purpose": "measure captured audio, run a scoped analyzer batch, inspect measured evidence, compare two analyses, map edits",
        "use_when": "the exact capture/revision is acknowledged and a measured answer is needed",
        "context": "panorama receipt; capture_id + analyzers + source_ids + intervals for a scoped batch; analysis_id for evidence; two analysis ids for a comparison",
        "tools": ["reaper_analyze", "reaper_mix_evidence", "reaper_edit_map",
                  "reaper_render_candidate", "reaper_release"],
        "not_for": "choosing a genre recipe or a quality score; a scoped result is not whole-project coverage",
    },
    "measure": {
        "purpose": ("measure only the requested sources over a caller-declared interval, "
                    "read per-source freshness, and inspect coverage from stored evidence"),
        "use_when": ("after an edit you want to hear again without a full capture, when you "
                     "need to know which sources a capture still describes, or before a "
                     "global claim"),
        "context": ("project_id + archive_root; start_s/end_s are the caller's interval; "
                    "stage is via_master or master"),
        "tools": ["reaper_measure", "reaper_freshness", "reaper_coverage"],
        "not_for": ("choosing how long to measure, inferring musical sections, or replacing "
                    "a whole-project capture when a global verdict is required"),
    },
    "inspect": {
        "purpose": ("quote an exact evidence token, calibrate a plug-in parameter, "
                    "or read and reshape a MIDI take"),
        "use_when": ("a stored measurement must be quoted without rescanning the archive, "
                     "a value target needs a measured mapping, or velocities must change"),
        "context": "project_id for MIDI/calibration; archive_root for evidence and calibration",
        "tools": ["reaper_evidence", "reaper_calibrate", "reaper_midi"],
        "not_for": "replacing a fresh measurement or editing audio items",
    },
    "edit": {
        "purpose": "preview, apply or undo a revision-bound mutation",
        "use_when": "a measured finding justifies a concrete parameter or object change",
        "context": "panorama receipt + context token + exact expected_revision",
        "tools": ["reaper_edit"],
        "not_for": "bulk unknown mutations; use reaper_api_search first",
    },
    "discovery": {
        "purpose": "find rare or low-level ReaScript functions and Actions without a flat tool per function",
        "use_when": "the task groups above do not express the operation",
        "context": "object + operation query; then reaper_api_call with a typed handle",
        "tools": ["reaper_capabilities", "reaper_api_search", "reaper_api_call",
                  "reaper_action"],
        "not_for": "guessing a function; results are candidates, not recommendations",
    },
    "research": {
        "purpose": "optional model-specific listening and spatial research",
        "use_when": "a question explicitly needs a model or spatial rendering",
        "context": "the model/runtime status and the exact audio target",
        "tools": ["reaper_flamingo_status", "reaper_flamingo_describe",
                  "reaper_flamingo_ask", "reaper_spatial_status",
                  "reaper_spatial_analyze", "reaper_spatial_timeline"],
        "not_for": "routine mix decisions; results are evidence, not a verdict",
    },
}
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


def _access_note(access: Mapping[str, Any] | None) -> dict[str, Any]:
    """Advisory mode reports what was missing instead of blocking."""
    if not access or not access.get("unverified"):
        return {}
    return {"unverified": True, "access": access}


def _require_structural_access(*, session: ProjectSession, project_id: str,
                               expected: ProjectRevision,
                               archive_root: str | None,
                               structural_token: str | None = None,
                               panorama_receipt: str | None = None) -> dict[str, Any]:
    """Authorise a mutation by a cheap structural token, or a legacy receipt.

    The token proves the caller saw this exact project map (project, instance,
    epoch, revision, map digest) and costs one digest read — no capture, no
    audio.  A panorama receipt is still accepted for older workflows.
    """
    if structural_token:
        payload = require_structural_token(session, project_id, structural_token)
        return {"accepted": "structural_token", "fingerprint": payload}
    state = load_mode()
    if state.mode == "off":
        # Mode off keeps only identity/revision checks, which the caller ran
        # before reaching this point.
        return {"accepted": "mode_off"}
    if state.mode == "advisory":
        try:
            return _require_panorama_receipt(panorama_receipt, project_id,
                                             expected.to_dict(), archive_root)
        except Exception as error:  # noqa: BLE001 - advisory reports, never blocks
            return {"accepted": "advisory", "unverified": True,
                    "missing": [("panorama_receipt_or_structural_token"
                                 if panorama_receipt is None else "valid_token")],
                    "detail": str(error)[:400]}
    return _require_panorama_receipt(panorama_receipt, project_id,
                                     expected.to_dict(), archive_root)


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
            details={"next_action": ("mint a cheap structural token with "
                                     "reaper_structural_token and pass it as structural_token; "
                                     "a full panorama (open → every page → ack) is needed only "
                                     "for audio claims and global verdicts"),
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
    current. A structural change update can authorize edits but never audio
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


def _revision_identity(revision: Any) -> tuple[Any, ...] | None:
    """Return the identity fields a capture's own render pass never changes.

    ``instance_id`` and a positive ``epoch`` are required: ProjectRevision
    treats a missing instance/epoch as *no proof of continuity*, so a counter
    increase without both cannot be classified as render churn.
    """
    if not isinstance(revision, Mapping):
        return None
    project_id = str(revision.get("project_id") or "")
    path = str(revision.get("path") or "")
    instance_id = revision.get("instance_id")
    epoch = revision.get("epoch")
    if not project_id or not path:
        return None
    if not isinstance(instance_id, str) or not instance_id:
        return None
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        return None
    return (project_id, path, instance_id, epoch)


def _is_render_restoration_of(evidence: Any, after: Any) -> bool:
    """True only when two revisions are one state separated by render churn.

    A capture mutes and restores tracks for its isolation renders, and REAPER
    counts that as project changes.  Stored audio evidence keeps the
    pre-capture counter while its panorama receipt is minted at the
    post-capture counter, so the two can never compare equal.  Accepting the
    difference is safe only when project identity, path, bridge instance and
    epoch are identical and the counter merely advanced: a structural edit,
    another project, or another bridge instance must still fail closed.
    """
    left, right = _revision_identity(evidence), _revision_identity(after)
    if left is None or right is None or left != right:
        return False
    try:
        before_count = int(evidence.get("change_count"))  # type: ignore[union-attr]
        after_count = int(after.get("change_count"))  # type: ignore[union-attr]
    except (TypeError, ValueError):
        return False
    return after_count > before_count


def _capture_document_root(archive_root: str | None, capture_id: str) -> str | None:
    """Return the immutable fact root of one archived capture, if provable.

    The panorama document is built from the sealed ``FullCaptureRecord``; its
    ``integrity_sha256`` covers every stored field, so this root identifies the
    exact project_map and audio hashes the analysis measured.  Anything that
    cannot be resolved returns ``None`` and the caller must fail closed.
    """
    if not archive_root or not capture_id:
        return None
    try:
        document = _panorama_lifecycle(archive_root)._document(capture_id)  # noqa: SLF001
        return fact_root(document)
    except Exception:  # noqa: BLE001 - unprovable root must never authorize
        return None


def _require_analysis_receipt(receipt: str | None, record: Mapping[str, Any],
                              archive_root: str | None = None) -> dict[str, Any]:
    """Resolve a panorama receipt for one stored analysis record.

    The evidence revision is tried first.  The capture's post-render revision is
    tried only for a proven render-restoration counter change of the same
    project, path, bridge instance and epoch *and* only when the archived
    capture's immutable fact root can be resolved; that root is then required to
    match the receipt's, together with the exact capture id.  A receipt minted
    for any other capture, panorama document or project therefore stays stale,
    and so does a counter change whose structural state cannot be proven equal.
    """
    project_id = str(record.get("project_id") or "")
    capture_id = record.get("capture_id") or _record_capture_id(record)
    evidence = record.get("revision")
    try:
        return _require_interpretation_receipt(
            receipt, project_id, evidence, archive_root,
            expected_capture_id=capture_id,
        )
    except StaleContext:
        after = record.get("revision_after")
        if not capture_id or not _is_render_restoration_of(evidence, after):
            raise
        capture_root = _capture_document_root(archive_root, capture_id)
        if not capture_root:
            raise StaleContext(
                "The capture's immutable fact root cannot be resolved; the "
                "counter difference cannot be proven to be render churn",
                details={"capture_id": capture_id,
                         "evidence_revision": evidence,
                         "revision_after_capture": after},
            )
        return _require_interpretation_receipt(
            receipt, project_id, after, archive_root, capture_root,
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
                 "project panorama delivered whole when it fits or as a complete "
                 "lossless page set when it does not, exact semantic queries, and "
                 "factual change updates that are themselves pageable."),
    instructions=(
        "REAPER Workbench. Find the task with reaper_capabilities(mode=\"tools\") — "
        "the surface is grouped by session, project, capture, panorama, analysis, edit, "
        "discovery and research, not one tool per ReaScript function. " "The work mode, the stale-share threshold and the lock are set by the user at "
        "session start. Change them only at the user's explicit request — never on your "
        "own, not to optimise, and not to get past a gate; if the mode blocks the work, "
        "ask instead of switching. reaper_mode reads them and, on that request, changes "
        "them; every response repeats the current mode. Strict means the checks block, "
        "advisory means they are reported and the action proceeds marked unverified, off "
        "means only project/instance/revision identity is checked. An edit requires a "
        "current structural token, not a full panorama; a full panorama is needed only "
        "for audio claims and global verdicts. ""Establish the "
        "project, its identity and exact revision with reaper_session and reaper_project, "
        "then reaper_panorama_status. Start the full project context with "
        "reaper_panorama_open() and no mode argument: the default is size-independent "
        "auto. It returns the complete full view with its receipt and context token "
        "in one measured result when that fits the declared budget; otherwise it returns "
        "page 1 plus a manifest of every page. Pages are the complete path, not a legacy "
        "fallback: a whole-project claim needs every declared page acknowledged. A "
        "structural edit does not: it needs a current structural token from "
        "reaper_structural_token (project, instance, epoch, revision and the project-map "
        "digest), which costs one cheap read, no capture and no audio. Do not choose a "
        "subset of pages, and do not ask "
        "the model to guess single vs pages; an explicit mode=\"single\" over budget also "
        "falls back to pages. If manifest.page_ids is present, fetch every id in order; if "
        "it is omitted for a very large set, enumerate the declared page_id_pattern for "
        "1..page_count. Then call reaper_panorama_ack with every page_hash in order, or "
        "with manifest_sha256 alone, which commits to the same ordered page set. A partial "
        "set returns the missing page ids and mints no receipt or context token. "
        "If a page is replaced by a host spillover notice, it was not read: reopen the "
        "same capture with smaller page sizes and fetch the full page text. A receipt "
        "proves the server served these exact pages, not that every text was visible, "
        "that you understood it, or that you heard audio. "
        "The guide resource reaper://guide/model states this protocol in one place. "
        "Bridge, evidence and MIDI helpers are first-class: reaper_bridge(action=status|cleanup|ensure) "
        "lists live bridges, retires duplicates and starts REAPER when none answers; exactly one "
        "resident loop survives per process. reaper_measure reopens offline media itself and returns "
        "a unique measurement_id, so two identical windows remain two distinct facts. A value target "
        "(\"25 ms\", \"3:1\") calibrates itself on first use and is cached. reaper_midi reads per-pitch "
        "statistics, edits velocities and quantises note starts. reaper_evidence returns the units "
        "behind an evidence token. A measurement no longer invalidates a structural token: its map "
        "hash ignores render configuration and quantises positions to 0.1 s. "
        "The panorama is a project-wide map, "
        "not full-resolution audio evidence: temporal maps use declared aggregation, and exact series and relation rows "
        "are fetched separately. Check coverage, point counts, resolution, aggregation, units, stage, and provenance. "
        "Before a conclusion or edit depends on fine timing, frequency, transient, or relation evidence, fetch the exact "
        "relevant source(s) and interval with reaper_panorama_query; narrow the interval if the result is over budget. "
        "Never treat a missing or summarized value as proof that the event or feature is absent. A URI alone does not "
        "mean an audio-capable client delivered sound to the model. A complete receipt unlocks analysis and exact "
        "queries; an edit needs a structural token, and a stale revision needs either a fresh token or a new open. "
        "Create a capture with reaper_capture_snapshot only when no capture matches the "
        "revision. Do not interpret project audio from a partial panorama. If "
        "reaper_capture_snapshot returns panorama_capacity_exceeded with details.capture_id "
        "and says the canonical record remains archived, only one-shot delivery failed: do "
        "not capture again. Open that exact capture with reaper_panorama_open using the same "
        "project_id/archive_root, fetch every page and acknowledge all hashes, then use "
        "archive-mode reaper_analyze for that capture and receipt. Its measurements apply "
        "only to the archived revision. Exact "
        "high-frequency rows "
        "(relation edges, series cells, MIDI notes, envelope points) come from "
        "reaper_panorama_query by semantic selection. If a query, audio comparison or scoped "
        "analysis result is too large for one measured response, it is delivered as "
        "complete result_page pages; fetch every page and acknowledge the hashes or "
        "manifest hash. Result-page acknowledgement proves reconstruction only and mints "
        "no receipt, context token or edit permission. "
        "Use reaper_analyze with capture_id, analyzers, source_ids and intervals for one "
        "scoped batch; the analyzer IDs, scope, prerequisites and relative cost classes are "
        "in reaper_capabilities(mode=\"tools\"). A scoped result is not whole-project "
        "coverage. A full panorama is not an automatic next step after edits or scoped "
        "analysis: keep working from the acknowledged view, the change journal and local "
        "evidence while they support the next decision; request a new full panorama only "
        "when the next consequential global decision needs fresh whole-project evidence, "
        "the user asks for a complete overview, or before a final whole-project conclusion. "
        "The latest context_token is the model's current known project state. For routine "
        "continuity, call reaper_panorama_changes_since with that token; do not restart "
        "from the first capture. An explicitly named pair of archived captures is a "
        "separate version comparison, not the normal current-state update. "
        "Every successful mutation (reaper_edit apply/undo, "
        "reaper_api_call, reaper_action, reaper_render_candidate) returns an update: the "
        "stable ids it touched with only their new current values, the current revision "
        "and the successor context token. Unchanged facts are not resent and no internal "
        "mutation record is exposed. An update too large for one measured result returns "
        "status=change_page with page 1, a manifest and a handle. Fetch every declared "
        "change page, then acknowledge all page hashes or the manifest hash; only that "
        "complete acknowledgement advances the token exactly once. Use the returned "
        "context_token for the next structural edit and continue editing at the new "
        "revision without another full capture. An update never makes stale audio "
        "current: its audio_evidence names every invalidated source, and project-target "
        "analysis refuses until a fresh capture is acknowledged. Updates address objects "
        "by stable IDs and semantic paths, not array positions; a track insertion or FX "
        "move must not be read as a change to every later object. "
        "A measurement-heavy update may carry its exact metric ops once in "
        "metric_table (integer-indexed columns plus exact dictionaries and a "
        "path_pattern); expand every row and do not treat it as a summary. "
        "Comparability is factual: if method/version, unit, stage, window or grid evidence "
        "does not match, the comparison says unknown or incomparable and still shows each "
        "side's exact measured values; it never emits a better/worse verdict. "
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


def _summarize_analyze_result(result: dict[str, Any], detail: str) -> dict[str, Any]:
    """Apply the versioned lossless summary projection to a model-facing result.

    Only ``detail="summary"`` is normalized.  ``detail="full"`` keeps the
    complete per-source report, and the durable record is written before this
    runs, so the archive never depends on the projection.  The projection runs
    on the JSON-normalized payload, so numpy scalars cannot reach it.
    """
    if detail != "summary":
        return result
    return project_analyze_summary(_jsonable(result))


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
        "panorama_capacity_exceeded": ("If details include capture_id and say the canonical record remains archived, "
                                         "do not repeat reaper_capture_snapshot: open that exact capture with "
                                         "reaper_panorama_open using the same project_id/archive_root, fetch every "
                                         "page, acknowledge all hashes, then use archive-mode reaper_analyze for that "
                                         "capture revision. Otherwise inspect the full error before deciding whether "
                                         "a new capture is needed."),
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
            result = _jsonable(function(*args, **kwargs))
            if isinstance(result, dict):
                result.setdefault("mode", mode_brief())
            return result
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


def _structural_fingerprint(access: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """Return the proven fingerprint when a structural token authorised the call."""
    if isinstance(access, Mapping) and access.get("accepted") == "structural_token":
        fingerprint = access.get("fingerprint")
        if isinstance(fingerprint, Mapping):
            return fingerprint
    return None


def _record_mutation_delta(*, lifecycle: PanoramaLifecycle, kind: str,
                           receipt: str, project_id: str,
                           base_revision: ProjectRevision,
                           session: ProjectSession, scope: str,
                           operation_facts: list[dict[str, Any]] | None,
                           structural_fingerprint: Mapping[str, Any] | None = None
                           ) -> dict[str, Any]:
    """Attach one internal mutation record to a successful mutation, or say why not.

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
                          "then": ("No structural change update can be minted from this readback; "
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
            structural_fingerprint=structural_fingerprint,
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
            "next_call": ({"tool": "reaper_structural_token", "project_id": project_id,
                           "then": ("The mutation applied and the revision advanced. "
                                    "Mint a fresh structural token to continue editing; "
                                    "audio evidence stays unknown until a capture.")}
                          if structural_fingerprint is not None else
                          {"tool": "reaper_capture_snapshot", "project_id": project_id,
                           "then": ("The mutation applied but its context cannot be "
                                    "proven; capture again before interpreting or "
                                    "continuing structural edits.")}),
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


def _analysis_store_roots(archive_root: str | None = None,
                          analysis_id: str | None = None) -> list[Path]:
    """Ordered archive roots that may own a durable analysis report.

    A location pointer written next to the user profile answers this after a
    restart; the explicit argument, the last panorama root, the environment and
    the historical default are the fallbacks.  Every candidate is still verified
    by the stored record before it is believed.
    """
    candidates: list[Any] = []
    if analysis_id:
        candidates.extend(analysis_locations(analysis_id))
    candidates.extend([archive_root, _LAST_PANORAMA_ROOT,
                       os.environ.get("REAPER_CAPTURE_ARCHIVE"),
                       "capture-archive"])
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, (str, os.PathLike)) or not str(candidate).strip():
            continue
        try:
            root = Path(candidate).expanduser().absolute()
        except (OSError, ValueError):
            continue
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    return roots


def _persist_analysis_report(record: dict[str, Any], *,
                             archive_root: str | None,
                             fingerprint: Mapping[str, Any]) -> dict[str, Any]:
    """Write the derived report under the archive and report what happened.

    The measurement already succeeded, so a storage failure is returned as
    state instead of raised: the caller can then say plainly that this analysis
    is only lease-bound rather than silently believing it is durable.
    """
    for root in _analysis_store_roots(archive_root):
        if not root.is_dir():
            continue
        try:
            path = persist_analysis_record(root, record, fingerprint)
        except (OSError, ValidationError) as exc:
            _LOG.warning("Analysis report was not persisted under %s: %s", root, exc)
            return {"persisted": False, "archive_root": str(root),
                    "reason": getattr(exc, "message", None) or str(exc)}
        return {"persisted": True, "archive_root": str(root), "record": str(path),
                "input_fingerprint_sha256": record.get("input_fingerprint_sha256")}
    return {"persisted": False,
            "reason": "no capture archive root is available"}


def _verify_persisted_analysis(record: Mapping[str, Any], root: Path) -> None:
    """Re-prove a rehydrated report still belongs to its exact capture.

    The stored fingerprint is self-consistent by construction; this adds the one
    fact that only the archive can answer — the capture's immutable fact root.
    A report persisted for another capture, another input configuration or an
    archive that has since lost its audio fails closed here.
    """
    analysis_id = record.get("analysis_id")
    fingerprint = record.get("input_fingerprint")
    fingerprint = fingerprint if isinstance(fingerprint, Mapping) else {}
    state = fingerprint.get("capture_fact_root_state")
    capture_id = fingerprint.get("capture_id")
    if state == "available":
        current = _capture_document_root(str(root), str(capture_id or ""))
        if not current or current != fingerprint.get("capture_fact_root"):
            raise ValidationError(
                "The persisted analysis no longer matches its capture's immutable fact root",
                details={"analysis_id": analysis_id,
                         "capture_id": capture_id,
                         "fingerprint_root": fingerprint.get("capture_fact_root"),
                         "current_root": current},
            )
    elif state not in {"unresolved", "not_archived"}:
        raise ValidationError("The persisted analysis has no usable capture binding",
                              details={"analysis_id": analysis_id})
    audio_root = record.get("archive_root")
    if isinstance(audio_root, str) and audio_root:
        try:
            stored = Path(audio_root).expanduser().resolve()
            expected = root.resolve()
        except OSError:
            stored, expected = Path(audio_root), root
        if stored != expected:
            raise ValidationError(
                "The persisted analysis points at audio outside this archive",
                details={"analysis_id": analysis_id,
                         "record_archive_root": str(stored),
                         "lookup_archive_root": str(expected)},
            )


def _analysis_record(analysis_id: str,
                     archive_root: str | None = None) -> dict[str, Any]:
    """Return one analysis record, rehydrating it from durable storage.

    The process-local map is only a cache: an MCP restart drops it while the
    derived report and its complete input fingerprint stay in the capture
    archive.  A rehydrated record is re-bound to its capture's immutable fact
    root before use and raises when it cannot prove that binding, so a report
    for another capture or input configuration is never substituted.
    """
    now = time.time()
    with _LOCK:
        record = _analysis_records.get(analysis_id)
    if isinstance(record, dict) and record.get("expires_at", 0.0) > now:
        return record
    matches: list[tuple[Path, dict[str, Any]]] = []
    for root in _analysis_store_roots(archive_root, analysis_id):
        if not root.is_dir():
            continue
        loaded = load_analysis_record(root, analysis_id)
        if loaded is None:
            continue
        _verify_persisted_analysis(loaded, root)
        matches.append((root, loaded))
    if not matches:
        raise ValidationError("analysis_id is unknown or expired",
                              details={"analysis_id": analysis_id})
    digests = {str(item.get("input_fingerprint_sha256")) for _, item in matches}
    if len(digests) > 1:
        raise ValidationError("analysis_id resolves to more than one durable report",
                              details={"analysis_id": analysis_id,
                                       "archive_roots": [str(root) for root, _ in matches]})
    root, loaded = matches[0]
    loaded["expires_at"] = now + _ANALYSIS_TTL_S
    with _LOCK:
        _analysis_records[analysis_id] = loaded
    _LOG.info("Rehydrated analysis %s from %s", analysis_id, root)
    return loaded


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
    health = bridge.health(timeout_s=3.0)
    try:
        pid = (health.get("project") or {}).get("project_id")
        if pid:
            state = bridge.call("project.render_settings.read",
                                {"project_id": pid}, timeout_s=5.0)
            dirty = state.get("dirty") if isinstance(state, dict) else None
            if isinstance(dirty, (int, float)) and not isinstance(dirty, bool):
                health.setdefault("project", {})["dirty"] = bool(dirty)
    except Exception:  # noqa: BLE001 - health must not fail over a hint
        pass
    return health


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


def _bridge_health(session_id: str, timeout_s: float = 2.0) -> dict[str, Any] | None:
    try:
        health = ReaperBridge(bridge_session_id=session_id).health(timeout_s=timeout_s)
    except Exception:
        return None
    project = health.get("project") if isinstance(health, Mapping) else None
    return {"bridge_session_id": session_id,
            "bridge_build": health.get("bridge_build"),
            "project_id": (project or {}).get("project_id"),
            "path": (project or {}).get("path"),
            "change_count": (project or {}).get("change_count")}


@server.tool()
@_guard
def reaper_bridge(action: str = "status", project_id: str | None = None,
                  bridge_session_id: str | None = None,
                  project_path: str | None = None,
                  timeout_s: float = 30.0) -> dict[str, Any]:
    """Discover, tidy up or start the resident REAPER bridge from one place.

    ``status`` lists every live bridge with the project it serves. ``cleanup``
    retires every duplicate except the one that matches ``project_id`` (or the
    explicitly named session), which is what makes a second stray copy stop
    fighting the first. ``ensure`` starts REAPER with ``project_path`` when no
    bridge answers and waits for one, so a caller never has to remember a
    startup script.
    """
    if action not in {"status", "cleanup", "ensure"}:
        raise ValidationError("action must be status, cleanup or ensure")
    if action == "status":
        instances = []
        for descriptor in ReaperBridge().discover_instances():
            session_id = str(descriptor.get("bridge_session_id"))
            health = _bridge_health(session_id)
            instances.append(health or {"bridge_session_id": session_id,
                                        "state": "registered_but_silent"})
        return {"ok": True, "action": "status", "count": len(instances),
                "bridges": instances}

    if action == "cleanup":
        instances = ReaperBridge().discover_instances()
        keep = bridge_session_id
        if keep is None and project_id:
            for descriptor in instances:
                session_id = str(descriptor.get("bridge_session_id"))
                health = _bridge_health(session_id) or {}
                if health.get("project_id") == project_id:
                    keep = session_id
                    break
        if keep is None and instances:
            keep = str(instances[0].get("bridge_session_id"))
        retired, failed = [], []
        for descriptor in instances:
            session_id = str(descriptor.get("bridge_session_id"))
            if session_id == keep:
                continue
            try:
                ReaperBridge(bridge_session_id=session_id).call(
                    "bridge.lifecycle", {"action": "retire"}, timeout_s=5.0)
                retired.append(session_id)
            except Exception as error:  # report, never crash cleanup
                failed.append({"bridge_session_id": session_id,
                               "error": str(error)})
        remaining = []
        for descriptor in ReaperBridge().discover_instances():
            session_id = str(descriptor.get("bridge_session_id"))
            remaining.append(_bridge_health(session_id)
                             or {"bridge_session_id": session_id,
                                 "state": "registered_but_silent"})
        return {"ok": not failed, "action": "cleanup", "kept": keep,
                "retired": retired, "failed": failed, "remaining": remaining}

    live = [descriptor for descriptor in ReaperBridge().discover_instances()
            if _bridge_health(str(descriptor.get("bridge_session_id")))]
    if live:
        return {"ok": True, "action": "ensure", "started": False,
                "bridges": [descriptor.get("bridge_session_id") for descriptor in live]}
    executable = Path(os.environ.get("REAPER_EXECUTABLE",
                                     r"C:/Program Files/REAPER (x64)/reaper.exe"))
    if not project_path:
        raise ValidationError(
            "ensure needs project_path when no bridge is running",
            details={"next_action": "pass project_path so REAPER can be opened with it"})
    if not executable.is_file():
        raise ValidationError("REAPER executable not found",
                              details={"path": str(executable)})
    subprocess.Popen([str(executable), str(project_path)])
    deadline = time.monotonic() + max(5.0, float(timeout_s))
    while time.monotonic() < deadline:
        for descriptor in ReaperBridge().discover_instances():
            session_id = str(descriptor.get("bridge_session_id"))
            health = _bridge_health(session_id)
            if health:
                return {"ok": True, "action": "ensure", "started": True,
                        "bridge_session_id": session_id,
                        "project_id": health.get("project_id")}
        time.sleep(0.5)
    raise BridgeUnavailable(
        "REAPER did not expose a bridge before the deadline",
        details={"project_path": project_path,
                 "next_action": "check that REAPER started and scripts are enabled"})


@server.tool()
@_guard
def reaper_evidence(token: str, archive_root: str) -> dict[str, Any]:
    """Read the units behind an evidence token instead of scanning the archive.

    A token names an exact set of measurements; this returns those units with
    their metric, value, interval and source, so a caller can quote LUFS or a
    peak without loading every unit ever stored.
    """
    store = EvidenceStore(archive_root)
    try:
        record = store.load_token(token)
    except KeyError as error:
        raise ValidationError(str(error), details={"token": token,
                                                   "next_action": "run a measure first"})
    units = store.get_many(list(record.get("units") or []))
    return {"ok": True, "token": token, "project_id": record.get("project_id"),
            "revision": record.get("revision"),
            "unit_count": len(units),
            "units": [{"id": unit.get("id"), "metric": unit.get("metric"),
                       "value": unit.get("value"), "unit": unit.get("unit"),
                       "interval": unit.get("interval"),
                       "source_id": unit.get("source_id")}
                      for unit in units]}


@server.tool()
@_guard
def reaper_calibrate(track_guid: str, fx_guid: str, archive_root: str,
                     project_id: str | None = None,
                     parameters: list[int] | None = None,
                     points: int = 11) -> dict[str, Any]:
    """Measure a plug-in parameter so a value target ("25 ms", "3:1") is exact.

    Sweeps normalized positions, records what the plug-in displays, restores the
    original value and stores the table next to the plug-in summaries. Every
    later ``fx.set_parameter`` with ``target`` resolves through this table, so
    nobody has to guess what 0.27 means.
    """
    bridge = ReaperBridge()
    health = bridge.health(timeout_s=5.0)
    project = health.get("project") or {}
    project_id = project_id or str(project.get("project_id") or "")
    if not project_id:
        raise BridgeUnavailable("REAPER returned no project identity")
    context = bridge.project_context(project_id, include_items=False,
                                     include_fx_params=False)
    track = next((row for row in context.get("tracks") or []
                  if row.get("guid") == track_guid), None)
    if track is None:
        raise ValidationError("track not found", details={"track_guid": track_guid})
    fx_row = next((row for row in track.get("fx") or []
                   if row.get("guid") == fx_guid), None)
    if fx_row is None:
        raise ValidationError("plug-in not found on the track",
                              details={"fx_guid": fx_guid})
    listing = bridge.call("fx.list_params",
                          {"track_guid": track_guid, "fx_guid": fx_guid})
    names = {int(row["index"]): str(row.get("name"))
             for row in listing.get("parameters") or []}
    wanted = parameters or sorted(names)[:1]
    store = PluginStateStore(archive_root)
    measured = []
    for index in wanted:
        index = int(index)
        if index not in names:
            continue

        def read() -> tuple[float, str]:
            change_count = bridge.health()["project"]["change_count"]
            handle = ReaperHandle(kind="MediaTrack", project_id=project_id,
                                  change_count=change_count, stable_id=track_guid)
            normalized = float(bridge.call_api("TrackFX_GetParamNormalized", handle,
                                               int(fx_row["index"]), index).returns[0])
            formatted = bridge.call_api("TrackFX_GetFormattedParamValue", handle,
                                        int(fx_row["index"]), index)
            text = str(formatted.returns[1]) if len(formatted.returns) > 1 else ""
            return normalized, text

        def write(value: float) -> None:
            change_count = bridge.health()["project"]["change_count"]
            handle = ReaperHandle(kind="MediaTrack", project_id=project_id,
                                  change_count=change_count, stable_id=track_guid)
            bridge.call_api("TrackFX_SetParamNormalized", handle,
                            int(fx_row["index"]), index, float(value))

        original, original_text = read()
        table = []
        steps = max(3, int(points))
        for step in range(steps):
            normalized = step / (steps - 1)
            write(normalized)
            _, text = read()
            table.append({"normalized": round(normalized, 6), "formatted": text})
        write(original)
        store.put_calibration(project_id, track_guid, fx_guid, index,
                              name=names[index], table=table)
        measured.append({"parameter_index": index, "name": names[index],
                         "was": original_text, "points": len(table)})
    return {"ok": True, "track_guid": track_guid, "fx_guid": fx_guid,
            "measured": measured}


@server.tool()
@_guard
def reaper_midi(action: str = "notes", item_guid: str | None = None,
                project_id: str | None = None,
                pitch: int | None = None,
                start_s: float | None = None, end_s: float | None = None,
                factor: float = 1.0, offset: float = 0.0,
                jitter: float = 0.0,
                grid_s: float | None = None,
                strength: float = 1.0) -> dict[str, Any]:
    """Read a MIDI take or reshape its velocities without leaving MCP.

    ``notes`` returns per-pitch counts and velocity ranges so a caller can see
    what the kit is doing. ``velocity_scale`` applies a measured factor, offset
    and deterministic jitter to the named pitch and time window, then sorts the
    take; it answers with how many notes changed. ``quantize`` snaps note starts
    to ``grid_s`` seconds with partial ``strength`` (0-1) and keeps lengths.
    """
    bridge = ReaperBridge()
    health = bridge.health(timeout_s=5.0)
    project = health.get("project") or {}
    project_id = project_id or str(project.get("project_id") or "")
    context = bridge.project_context(project_id, include_items=True,
                                     include_midi_notes=True,
                                     include_fx_params=False)
    if action == "notes":
        rows = []
        for track in context.get("tracks") or []:
            for item in track.get("items") or []:
                take = item.get("active_take") or {}
                notes = ((take.get("midi") or {}).get("notes")) or []
                if not notes:
                    continue
                by_pitch: dict[int, list[float]] = {}
                for note in notes:
                    by_pitch.setdefault(int(note.get("pitch")), []).append(
                        float(note.get("velocity") or 0))
                rows.append({"track_name": track.get("name"),
                             "item_guid": item.get("guid"),
                             "note_count": len(notes),
                             "pitches": [{"pitch": key,
                                          "notes": len(values),
                                          "velocity_min": min(values),
                                          "velocity_mean": round(sum(values) / len(values), 1),
                                          "velocity_max": max(values)}
                                         for key, values in sorted(by_pitch.items())]})
        return {"ok": True, "action": "notes", "items": rows}
    if action == "quantize":
        if not item_guid:
            raise ValidationError("quantize needs item_guid")
        if grid_s is None or float(grid_s) <= 0:
            raise ValidationError("quantize needs a positive grid_s")
        payload: dict[str, Any] = {"item_guid": item_guid,
                                   "grid_s": float(grid_s),
                                   "strength": float(strength)}
        if pitch is not None:
            payload["pitch"] = int(pitch)
        result = bridge.call("midi.quantize", payload)
        return {"ok": True, "action": "quantize", **result}
    if action != "velocity_scale":
        raise ValidationError("action must be notes, velocity_scale or quantize")
    if not item_guid:
        raise ValidationError("velocity_scale needs item_guid")
    payload: dict[str, Any] = {"item_guid": item_guid, "factor": float(factor),
                               "offset": float(offset), "jitter": float(jitter)}
    if pitch is not None:
        payload["pitch"] = int(pitch)
    if start_s is not None:
        payload["start_s"] = float(start_s)
    if end_s is not None:
        payload["end_s"] = float(end_s)
    result = bridge.call("midi.velocity_scale", payload)
    return {"ok": True, "action": "velocity_scale", **result}


@server.tool()
@_guard
def reaper_capabilities(mode: Literal["summary", "domain", "tools"] = "summary",
                        domain: str | None = None,
                        offset: int = 0, limit: int = 60,
                        detail: Literal["names", "summary", "full"] = "names"
                        ) -> dict[str, Any]:
    """Browse the documented API by domain without flooding the model.

    mode=summary returns the API index. mode=domain pages through one domain.
    mode=tools returns the logical MCP task surface: stable groups with purpose,
    required context and the tools for each task. Use mode=tools first; use
    mode=domain or reaper_api_search only for a rare low-level operation.

    detail may be names, summary, or full; summary/full include signatures.
    """
    if mode not in {"summary", "domain", "tools"}:
        raise ValidationError("mode must be summary, domain, or tools")
    if mode == "tools":
        return {
            "schema_version": 1,
            "surface": "logical_task_groups",
            "rule": ("Find the task group first. A new MCP tool is added only when no "
                     "existing group can express the task with its current arguments. "
                     "Rare or per-function operations use reaper_capabilities(mode=domain) "
                     "and reaper_api_search, then reaper_api_call — never one tool per "
                     "ReaScript function or metric."),
            "groups": {name: dict(value) for name, value in TOOL_SURFACE.items()},
            "analyzers": {name: dict(value) for name, value in ANALYZER_CATALOG.items()},
            "analyzer_rule": ("Pass analyzer IDs to reaper_analyze(analyzers=[...]) with "
                              "capture_id, source_ids and intervals for one scoped batch. "
                              "Cost classes are relative declarations, not measured seconds."),
            "context_rule": ("Every mutation and every audio interpretation is bound to a "
                             "capture revision and a panorama receipt/context token; a "
                             "tool outside the session/project/discovery groups does not "
                             "accept a bare project path as authority."),
        }
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


def _plugin_state_store(archive_root: str | None = None) -> PluginStateStore:
    root = Path(archive_root or _LAST_PANORAMA_ROOT or
                os.environ.get("REAPER_CAPTURE_ARCHIVE", "capture-archive")).resolve()
    return PluginStateStore(root)


def _attach_send_routing(session, context: dict[str, Any]) -> dict[str, Any]:
    """Replace each track's send count with the digest rows, names included.

    ``project.context`` can only say how many sends a track has; the routing
    itself (destination, level, mode) lives in the digest. Reading it once here
    turns "sends: 1" into "1 send to FEMALE at 0 dB", which is what a mixer
    actually needs.
    """
    revision = context.get("revision") or {}
    project_id = str(revision.get("project_id") or "")
    if not project_id or not context.get("tracks"):
        return context
    try:
        digest = session.bridge.project_digest(project_id)
    except Exception:
        return context
    rows = {str(track.get("guid")): track.get("sends")
            for track in (digest.get("fields") or {}).get("tracks") or []}
    for track in context["tracks"]:
        sends = rows.get(str(track.get("guid")))
        if isinstance(sends, list):
            track["send_count"] = track.get("sends")
            track["sends"] = sends
    return context


def _attach_fx_summaries(*, session: ProjectSession, revision: ProjectRevision,
                         context: dict[str, Any], archive_root: str | None,
                         refresh: bool) -> dict[str, Any]:
    """Attach compact plug-in summaries to every FX row of the selected tracks.

    A cached summary is reused only while the track's serialized FX chain hash
    matches the digest; otherwise the row says ``stale`` (or ``unverified`` for a
    chain that cannot be hashed) instead of showing an older state.  ``refresh``
    reads parameters once, rebuilds the summaries and stores them.
    """
    tracks = context.get("tracks") or []
    if not isinstance(tracks, list) or not tracks:
        return context
    store = _plugin_state_store(archive_root)
    digest = session.digest(revision.project_id) if refresh else None
    chains: dict[str, tuple[str | None, bool]] = {}
    if isinstance(digest, Mapping):
        for row in ((digest.get("fields") or {}).get("fx_state") or {}).get("tracks") or []:
            if isinstance(row, Mapping):
                chains[str(row.get("track_guid"))] = (
                    str(row.get("chunk") or "") or None,
                    bool(row.get("unstable")))
    elif not refresh:
        digest = session.digest(revision.project_id)
        for row in ((digest.get("fields") or {}).get("fx_state") or {}).get("tracks") or []:
            if isinstance(row, Mapping):
                chains[str(row.get("track_guid"))] = (
                    str(row.get("chunk") or "") or None,
                    bool(row.get("unstable")))

    def _params(track_guid: str, fx_guid: str) -> list[dict[str, Any]]:
        """Read one plug-in's parameters; the full-project dump is never needed."""
        response = session.bridge.call("fx.list_params",
                                       {"track_guid": track_guid,
                                        "fx_guid": fx_guid})
        return [dict(row) for row in (response.get("parameters") or [])]

    for track in tracks:
        track_guid = str(track.get("guid"))
        chunk, unstable = chains.get(track_guid, (None, False))
        current = None if unstable else chain_fingerprint(chunk)
        for fx in track.get("fx") or []:
            if not isinstance(fx, dict):
                continue
            fx_guid = str(fx.get("guid"))
            if refresh:
                try:
                    parameters = _params(track_guid, fx_guid)
                except Exception as error:  # noqa: BLE001 - reported, not raised
                    fx["summary_state"] = "missing"
                    fx["summary_error"] = f"{type(error).__name__}: {error}"[:200]
                    continue
                summary = summarize(PluginDescriptor(name=str(fx.get("name")),
                                                     active=bool(fx.get("enabled")),
                                                     parameters=parameters))
                store.put(revision.project_id, track_guid, fx_guid,
                          summary=summary, chain_fingerprint=current)
                fx["summary"] = summary
                fx["summary_state"] = ("unverified" if current is None
                                       else "current")
                continue
            summary, state = store.status(revision.project_id, track_guid,
                                          fx_guid, current)
            if summary is not None:
                fx["summary"] = summary
            fx["summary_state"] = state
    context["fx_summary"] = {
        "schema": "reaper-workbench.plugin-state-store/1",
        "mode": "refresh" if refresh else "cache",
        "note": ("summary_state=current means the chain hash matches the digest; "
                 "stale/missing means re-read with refresh_fx_summaries=true"),
    }
    return context


def _calibrate_mapping(bridge, *, project_id: str, track_guid: str,
                       fx_index: int, index: int, points: int = 7
                       ) -> tuple[list[dict[str, Any]], str]:
    """Sweep one parameter and return its normalized -> displayed table."""
    def read() -> tuple[float, str]:
        change_count = bridge.health()["project"]["change_count"]
        handle = ReaperHandle(kind="MediaTrack", project_id=project_id,
                              change_count=change_count, stable_id=track_guid)
        normalized = float(bridge.call_api("TrackFX_GetParamNormalized", handle,
                                           int(fx_index), index).returns[0])
        formatted = bridge.call_api("TrackFX_GetFormattedParamValue", handle,
                                    int(fx_index), index)
        text = str(formatted.returns[1]) if len(formatted.returns) > 1 else ""
        return normalized, text

    def write(value: float) -> None:
        change_count = bridge.health()["project"]["change_count"]
        handle = ReaperHandle(kind="MediaTrack", project_id=project_id,
                              change_count=change_count, stable_id=track_guid)
        bridge.call_api("TrackFX_SetParamNormalized", handle,
                        int(fx_index), index, float(value))

    original, original_text = read()
    table: list[dict[str, Any]] = []
    steps = max(3, int(points))
    for step in range(steps):
        normalized = step / (steps - 1)
        write(normalized)
        _, text = read()
        table.append({"normalized": round(normalized, 6), "formatted": text})
    write(original)
    return table, original_text


def _resolve_parameter_targets(*, project_id: str, track_guid: str,
                               values: Mapping[str, Any],
                               archive_root: str | None,
                               bridge: Any | None = None
                               ) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Rewrite ``target="25 ms"`` into the normalized value the plug-in expects.

    The mapping comes from a measured calibration table.  When the table is
    missing and a bridge is available, the parameter is calibrated once here and
    cached, so the caller never has to know that value targets need a sweep.
    """
    target = values.get("target")
    fx_guid = values.get("fx_guid")
    index = values.get("parameter_index")
    if not isinstance(target, str) or not isinstance(fx_guid, str) or index is None:
        raise ValidationError("fx.set_parameter target requires fx_guid and parameter_index")
    index = int(index)
    store = _plugin_state_store(archive_root)
    calibration = store.get_calibration(project_id, track_guid, fx_guid, index)
    note = None
    if not calibration and bridge is not None:
        context = bridge.project_context(project_id, include_items=False,
                                         include_fx_params=False)
        track = next((row for row in context.get("tracks") or []
                      if row.get("guid") == track_guid), None)
        fx_row = next((row for row in (track or {}).get("fx") or []
                       if row.get("guid") == fx_guid), None)
        if track is None or fx_row is None:
            raise ValidationError("track or plug-in not found for the value target",
                                  details={"track_guid": track_guid,
                                           "fx_guid": fx_guid})
        listing = bridge.call("fx.list_params",
                              {"track_guid": track_guid, "fx_guid": fx_guid})
        name = next((str(row.get("name")) for row in listing.get("parameters") or []
                     if int(row.get("index", -1)) == index), f"parameter {index}")
        table, was = _calibrate_mapping(bridge, project_id=project_id,
                                        track_guid=track_guid,
                                        fx_index=int(fx_row["index"]),
                                        index=index)
        store.put_calibration(project_id, track_guid, fx_guid, index,
                              name=name, table=table)
        calibration = store.get_calibration(project_id, track_guid, fx_guid, index)
        note = {"parameter_index": index, "name": name, "was": was,
                "points": len(table), "cached": True}
    if not calibration:
        raise ValidationError(
            "No calibration for this parameter; measure it before using a value target",
            details={"next_action": ("call reaper_calibrate for this track, plug-in "
                                     "and parameter index, then retry"),
                     "fx_guid": fx_guid, "parameter_index": index})
    try:
        normalized = resolve_parameter_target(calibration["table"], target)
    except ValueError as error:
        raise ValidationError(str(error), details={"target": target,
                                                   "parameter": calibration.get("name")})
    resolved = {"fx_guid": fx_guid, "parameter_index": index,
                "normalized": normalized}
    return resolved, note


@server.tool()
@_guard
def reaper_project(project_id: str | None = None,
                   bridge_session_id: str | None = None,
                   include_items: bool = False,
                   include_fx_params: bool = False,
                   include_midi_notes: bool = False,
                   include_fx_summary: bool = True,
                   refresh_fx_summaries: bool = False,
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
            "note": ("A structural change update advances edits only; project-audio "
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
    context = _attach_send_routing(session, context)
    if include_fx_summary or refresh_fx_summaries:
        context = _attach_fx_summaries(session=session, revision=revision,
                                       context=context, archive_root=archive_root,
                                       refresh=refresh_fx_summaries)
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
                              parallel: bool,
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
    reference_fingerprints: list[dict[str, Any]] = []
    reference_hashes: dict[str, dict[str, Any]] = {}
    for request in reference_requests:
        reference_section = request["section"]
        loaded = load_audio(request["path"], reference_section)
        file_fingerprint = reference_hashes.get(str(request["path"]))
        if file_fingerprint is None:
            file_fingerprint = reference_file_fingerprint(request["path"])
            reference_hashes[str(request["path"])] = file_fingerprint
        key_base = request["name"]
        key = key_base
        suffix = 2
        while key in reference_audio:
            key = f"{key_base}_{suffix}"
            suffix += 1
        reference_audio[key] = loaded
        if reference_section is not None:
            reference_sections[key] = reference_section
        window = None if reference_section is None else {
            "start_s": reference_section.start_s,
            "end_s": reference_section.end_s,
        }
        reference_inputs.append({
            "reference_id": key,
            "selection": request["selection"],
            "window": window,
        })
        reference_fingerprints.append({
            "reference_id": key,
            "selection": request["selection"],
            "window": window,
            **file_fingerprint,
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
    capture_fact_root = _capture_document_root(str(archive.root_real),
                                              capture_id)
    try:
        fingerprint = analysis_input_fingerprint(
            capture_id=capture_id,
            project_id=captured.project_id,
            revision=analysis_revision,
            revision_after=revision_after,
            capture_fact_root=capture_fact_root,
            capture_fact_root_state=("available" if capture_fact_root
                                     else "unresolved"),
            section=analysis_windows["measured"],
            references=reference_fingerprints,
        options={
            "include_pairwise": include_pairwise,
            "phase_pairs": phase_pairs,
            "include_partial_loudness": include_partial_loudness,
            "parallel": parallel,
            "tail_ms": tail_ms,
            "channels": channels,
            "sample_rate": sample_rate,
            "artifact_role": artifact_role,
            "detail": detail,
        },
        )
    except ValidationError as exc:
        _LOG.warning("Analysis inputs are not fingerprintable: %s", exc)
        persistence = {"persisted": False,
                       "reason": getattr(exc, "message", str(exc))}
    else:
        record["input_fingerprint"] = fingerprint
        record["input_fingerprint_sha256"] = fingerprint_digest(fingerprint)
        persistence = _persist_analysis_report(
            record, archive_root=str(archive.root_real), fingerprint=fingerprint)
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
        "analysis_persistence": persistence,
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
    return _summarize_analyze_result(result, detail)


def _scoped_analyze_archived(*, capture_id: str,
                             project_id: str | None,
                             expected_revision: dict[str, Any] | None,
                             analyzers: Sequence[str],
                             source_ids: Sequence[str] | None,
                             intervals: Sequence[Mapping[str, Any]],
                             include_pairwise: bool,
                             phase_pairs: list[tuple[str, str]] | str | None,
                             include_partial_loudness: bool,
                             parallel: bool,
                             reference_requests: list[dict[str, Any]],
                             artifact_role: str,
                             archive_root: str | None,
                             panorama_receipt: str | None) -> dict[str, Any]:
    """Run one scoped batch over an archived capture.

    The archived capture path is used because it preserves exact source and
    interval identity without rendering the live project.  Every requested
    analyzer reports whether it ran; a missing prerequisite is an explicit
    per-analyzer status, not an invented value.
    """
    requested = list(dict.fromkeys(str(name) for name in analyzers))
    unknown = [name for name in requested if name not in ANALYZER_CATALOG]
    if unknown:
        raise ValidationError("unknown analyzer id",
                              details={"unknown": unknown,
                                       "allowed": sorted(ANALYZER_CATALOG)})
    if not intervals:
        raise ValidationError("scoped analysis needs at least one interval")
    selected_sources = None if not source_ids else {str(item) for item in source_ids}
    runs: list[dict[str, Any]] = []
    all_source_guids: set[str] = set()
    first: dict[str, Any] | None = None
    for interval in intervals:
        start_s = interval.get("start_s")
        end_s = interval.get("end_s")
        if start_s is None or end_s is None:
            raise ValidationError("each scoped interval needs start_s and end_s")
        full = _analyze_archived_capture(
            capture_id=capture_id, project_id=project_id,
            expected_revision=expected_revision,
            start_s=float(start_s), end_s=float(end_s),
            include_pairwise=bool(include_pairwise or "phase" in requested),
            phase_pairs=(phase_pairs if phase_pairs is not None else
                         ("all" if "phase" in requested else None)),
            include_partial_loudness=bool(include_partial_loudness or
                                          "partial_loudness" in requested),
            parallel=parallel,
            reference_requests=reference_requests,
            artifact_role=artifact_role, detail="full",
            tail_ms=0, channels="stereo", sample_rate=None,
            panorama_receipt=panorama_receipt, archive_root=archive_root)
        if first is None:
            first = full
        report = full.get("report") if isinstance(full.get("report"), Mapping) else {}
        rows = [row for row in (full.get("sources") or [])
                if isinstance(row, Mapping)]
        available_guids = {str(row.get("source_guid")) for row in rows}
        all_source_guids.update(available_guids)
        if selected_sources is not None:
            aliases = set(available_guids)
            aliases.update(f"track:{guid}" for guid in available_guids)
            aliases.update(f"master:{guid}" for guid in available_guids)
            aliases.update(str(row.get("source_id")) for row in rows
                           if row.get("source_id"))
            unknown_sources = sorted(selected_sources - aliases)
            if unknown_sources:
                raise ValidationError("unknown source id for this capture",
                                      details={"unknown": unknown_sources,
                                               "available": sorted(aliases)})
            rows = [row for row in rows
                    if str(row.get("source_guid")) in selected_sources or
                    str(row.get("source_id")) in selected_sources or
                    f"track:{row.get('source_guid')}" in selected_sources or
                    f"master:{row.get('source_guid')}" in selected_sources]
        selected_guids = {str(row.get("source_guid")) for row in rows}
        analyzer_results: dict[str, Any] = {}
        for name in requested:
            if name == "metrics":
                analyzer_results[name] = {
                    "status": "available", "sources": rows,
                    "coverage": full.get("coverage") or {},
                }
            elif name == "temporal_profile":
                profiles = {}
                for guid in selected_guids:
                    source_report = (report.get("sources") or {}).get(guid)
                    curves = source_report.get("curves") if isinstance(source_report, Mapping) else None
                    if isinstance(curves, Mapping):
                        profiles[guid] = curves.get("temporal_profile")
                analyzer_results[name] = {
                    "status": "available" if profiles else "unavailable",
                    "reason": None if profiles else "no temporal profile in this capture",
                    "sources": profiles,
                }
            elif name == "masking":
                value = report.get("masking_evidence")
                analyzer_results[name] = {
                    "status": "available" if value is not None else "unavailable",
                    "reason": None if value is not None else "requires at least two selected sources",
                    "result": value,
                }
            elif name == "band_balance":
                value = report.get("cross_source_band_timeline")
                analyzer_results[name] = {
                    "status": "available" if value is not None else "unavailable",
                    "reason": None if value is not None else "requires aligned source time axes",
                    "result": value,
                }
            elif name == "phase":
                value = report.get("pairwise_phase")
                analyzer_results[name] = {
                    "status": "available" if value else "unavailable",
                    "reason": None if value else "requires explicit source pairs with sufficient overlap",
                    "result": value,
                }
            elif name == "partial_loudness":
                value = report.get("partial_loudness")
                analyzer_results[name] = {
                    "status": "available" if value is not None else "unavailable",
                    "reason": None if value is not None else "requires a mix source and at least one stem",
                    "result": value,
                }
            elif name == "mix_evidence":
                analyzer_results[name] = {
                    "status": "available",
                    "result": build_mix_evidence(report or {}, max_issues=10**9),
                }
        runs.append({
            "interval": {"start_s": float(start_s), "end_s": float(end_s)},
            "analysis_id": full.get("analysis_id"),
            "revision": full.get("evidence_revision") or full.get("initial_context_revision"),
            "capture_id": capture_id,
            "requested_analyzers": requested,
            "analyzers": analyzer_results,
            "coverage": {
                "requested_sources": None if selected_sources is None else sorted(selected_sources),
                "available_sources": sorted(available_guids),
                "returned_sources": sorted(selected_guids),
                "source_count_including_master": len(rows),
                "scoped": True,
            },
            "provenance": full.get("execution") or {},
            "capture": full.get("capture") or {},
            "windows": full.get("analysis_windows") or {},
            "report_uri": full.get("report_uri"),
        })
    first = first or {}
    result = {
        "ok": True,
        "mode": "scoped",
        "status": "scoped_analysis",
        "capture_id": capture_id,
        "project_id": first.get("project", {}).get("project_id") if isinstance(
            first.get("project"), Mapping) else project_id,
        "revision": first.get("evidence_revision") or first.get("initial_context_revision"),
        "scope": {
            "kind": "scoped",
            "whole_project": False,
            "note": ("These facts are scoped to the named capture, interval(s) and "
                     "sources. They do not prove whole-project coverage, and they are "
                     "not current after a later project revision unless re-measured."),
            "source_count": len(all_source_guids),
        },
        "requested_analyzers": requested,
        "analyzer_catalog": {name: dict(ANALYZER_CATALOG[name]) for name in requested},
        "execution": {
            "mode": "archived_capture_measurement",
            "source_selection": "returned_rows_filtered",
            "note": ("the archived capture measurement is the exact source report; "
                     "source_ids filter which source rows are returned, not a re-measurement"),
        },
        "runs": runs,
        "next_step": ("Use these scoped facts for the local decision. A full panorama is "
                      "not an automatic next step after edits or micro-analysis; request "
                      "one only when the next consequential global decision needs fresh "
                      "whole-project evidence, when the user asks for a complete overview, "
                      "or before a final whole-project conclusion."),
        "no_quality_verdict": True,
    }
    # An archived capture is served frozen (mapping proxies and tuples); the
    # scoped result must be plain JSON before it is measured or paged.
    result = _thaw(result)
    if _delivered_bytes(result) <= DEFAULT_RESPONSE_BYTES:
        return result
    page = _panorama_lifecycle(archive_root).open_result_pages(
        result, result_kind="scoped_analysis",
        project_id=str(result.get("project_id") or ""),
        capture_id=capture_id,
        revision=result.get("revision") or {},
        fact_root_value=str((first.get("capture") or {}).get("fact_root") or ""),
        document_hash_value=str((first.get("capture") or {}).get("integrity_sha256") or ""),
        source_handle=str(first.get("analysis_id") or ""),
    )
    page.update({"ok": True, "mode": "scoped", "audio_analysis_page": True})
    return page


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
                   capture_id: str | None = None,
                   analyzers: list[str] | None = None,
                   source_ids: list[str] | None = None,
                   intervals: list[dict[str, float]] | None = None) -> dict[str, Any]:
    """Run the complete available audio suite on all tracks and the rendered master.

    With no capture_id, this is one batch operation that renders and analyzes
    the current project. With capture_id, it reuses one exact, receipt-verified
    archived capture and does not call the REAPER bridge or render the project.
    It returns a concise report plus analysis-scoped audio-resource URIs.
    reference_paths analyzes complete files; reference_windows selects
    source-file-local windows independently from the project section.
    artifact_role holds one lease each for temporary, baseline, or final.

    With capture_id plus analyzers/source_ids/intervals this becomes one scoped
    batch: several existing analyzer IDs run against named sources and exact
    song intervals, returning what ran, what was unavailable and a scope marker.
    A scoped result never claims whole-project coverage.  Large scoped results
    use the same lossless result-page contract as queries and audio comparisons.
    A full panorama is not an automatic next step after edits or micro-analysis;
    request one only when the next consequential global decision needs fresh
    whole-project evidence, when the user asks for a complete overview, or
    before a final whole-project conclusion.

    detail="summary" returns one lossless projection of those rows: every source
    is in "sources" (joined by "source_id") with every metric value, metric
    definitions appear once in "metric_registry" (unit/confidence/note), capture
    facts identical in every source appear once in "shared_capture", a dependency
    hash repeated across sources is stored once in "component_hashes" with each
    row linking to it by component id, and source-specific facts stay on their
    own row. "summary_projection.merge" states the exact merge, and no source,
    metric, null, note or provenance hash is sampled, rounded, paged or omitted.
    Completeness is not a size guarantee: a project with many sources can still
    exceed a client's inline budget, and the result is then longer, never
    shorter. detail="full" keeps the complete per-source report, and
    reaper://analyses/{analysis_id}/report always serves the archived report
    unchanged. Both carry the same measurement; only the layout differs.

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
    scoped_requested = analyzers is not None or source_ids is not None or intervals is not None
    if scoped_requested:
        if capture_id is None:
            raise ValidationError(
                "Scoped batched analysis requires capture_id and archived capture audio",
                details={"next_action": "capture the project, then pass capture_id, analyzers, source_ids and intervals"})
        if intervals is not None and (start_s is not None or end_s is not None):
            raise ValidationError("use either start_s/end_s or intervals, not both")
        if analyzers is None:
            analyzers = ["metrics"]
        if (not isinstance(analyzers, list) or not analyzers or
                any(not isinstance(name, str) or not name for name in analyzers)):
            raise ValidationError("analyzers must be a non-empty list of stable analyzer ids")
        if source_ids is not None and (
                not isinstance(source_ids, list) or
                any(not isinstance(item, str) or not item for item in source_ids)):
            raise ValidationError("source_ids must be a list of stable source or track ids")
        if intervals is None:
            intervals = ([{"start_s": start_s, "end_s": end_s}]
                         if start_s is not None and end_s is not None else None)
        if not isinstance(intervals, list) or not intervals:
            raise ValidationError("intervals must be a non-empty list of {start_s, end_s}")
        for interval in intervals:
            if (not isinstance(interval, dict) or not {"start_s", "end_s"} <= set(interval) or
                    set(interval) - {"start_s", "end_s"}):
                raise ValidationError("each interval must be exactly {start_s, end_s}")

    _prune_analysis_state()
    if scoped_requested:
        return _scoped_analyze_archived(
            capture_id=str(capture_id), project_id=project_id,
            expected_revision=expected_revision,
            analyzers=list(analyzers or ["metrics"]), source_ids=source_ids,
            intervals=[dict(item) for item in (intervals or [])],
            include_pairwise=include_pairwise,
            phase_pairs=(phase_pairs if not isinstance(phase_pairs, list)
                         else [tuple(pair) for pair in phase_pairs]),  # type: ignore[arg-type]
            include_partial_loudness=include_partial_loudness, parallel=parallel,
            reference_requests=reference_requests, artifact_role=artifact_role,
            archive_root=archive_root, panorama_receipt=panorama_receipt)
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
            parallel=parallel,
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
    reference_fingerprints: list[dict[str, Any]] = []
    reference_hashes: dict[str, dict[str, Any]] = {}
    for request in reference_requests:
        path = request["path"]
        reference_section = request["section"]
        reference_audio = load_audio(path, reference_section)
        file_fingerprint = reference_hashes.get(str(path))
        if file_fingerprint is None:
            file_fingerprint = reference_file_fingerprint(path)
            reference_hashes[str(path)] = file_fingerprint
        key_base = request["name"]
        key = key_base
        suffix = 2
        while key in references:
            key = f"{key_base}_{suffix}"
            suffix += 1
        references[key] = reference_audio
        if reference_section is not None:
            reference_sections[key] = reference_section
        window = None if reference_section is None else {
            "start_s": reference_section.start_s,
            "end_s": reference_section.end_s,
        }
        reference_inputs.append({
            "reference_id": key,
            "selection": request["selection"],
            "window": window,
        })
        reference_fingerprints.append({
            "reference_id": key,
            "selection": request["selection"],
            "window": window,
            **file_fingerprint,
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
    live_capture = report.get("capture")
    live_capture_id = (live_capture.get("capture_id")
                       if isinstance(live_capture, Mapping) else None)
    if not isinstance(live_capture_id, str) or not live_capture_id:
        live_capture_id = None
    live_fact_root = None
    if live_capture_id:
        for live_root in _analysis_store_roots(archive_root):
            if live_root.is_dir():
                live_fact_root = _capture_document_root(str(live_root),
                                                        live_capture_id)
                break
    try:
        fingerprint = analysis_input_fingerprint(
            capture_id=live_capture_id,
            project_id=selected_project_id,
            revision=analysis_revision,
            revision_after=revision,
            capture_fact_root=live_fact_root,
            capture_fact_root_state=("available" if live_fact_root
                                     else "not_archived"),
            section=analysis_windows["measured"],
            references=reference_fingerprints,
        options={
            "include_pairwise": include_pairwise,
            "phase_pairs": phase_pairs,
            "include_partial_loudness": include_partial_loudness,
            "parallel": parallel,
            "tail_ms": tail_ms,
            "channels": channels,
            "sample_rate": sample_rate,
            "artifact_role": artifact_role,
            "detail": detail,
        },
        )
    except ValidationError as exc:
        _LOG.warning("Analysis inputs are not fingerprintable: %s", exc)
        persistence = {"persisted": False,
                       "reason": getattr(exc, "message", str(exc))}
    else:
        record["input_fingerprint"] = fingerprint
        record["input_fingerprint_sha256"] = fingerprint_digest(fingerprint)
        persistence = _persist_analysis_report(
            record, archive_root=archive_root, fingerprint=fingerprint)
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
        "analysis_persistence": persistence,
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
    return _summarize_analyze_result(result, detail)


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
    record = _analysis_record(analysis_id, archive_root)
    report = record.get("report")
    if not isinstance(report, dict):
        raise ValidationError("analysis has no report")
    _require_analysis_receipt(panorama_receipt, record, archive_root)
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
                        archive_root: str | None = None,
                        compare_analysis_id: str | None = None,
                        interval: dict[str, float] | None = None,
                        sources: list[str] | None = None,
                        series: list[str] | None = None,
                        metrics: list[str] | None = None,
                        max_changed_cells: int | None = None) -> dict[str, Any]:
    """Return measured evidence from an existing analysis, or compare two.

    With no ``compare_analysis_id`` this summarizes measured mix issues and
    questions; no plugin, EQ frequency, or gain recipe is chosen here.

    With ``compare_analysis_id`` it returns the exact measured difference
    between the two stored analyses: scalar metrics with units, and series
    compared cell-for-cell on the stored time axis.  Every changed cell is
    returned; there is no internal 2000-cell truncation.  This is a measurement
    comparison, not a mutation record.  A delta that does not
    fit one measured result is delivered as complete ``result_page`` pages over
    ``reaper_panorama_page`` and verified with ``reaper_panorama_ack``.  The
    acknowledgement proves exact reconstruction only; it mints no receipt or
    edit token and does not change the capture context.

    A run of changed cells is reported as exact half-open intervals built only
    from stored cell starts and the measurement window end, plus the exact
    changed timestamps/indices.  If the analyses are not one continuous project
    history (project, path, bridge instance, epoch) or do not share a
    window/time axis, the call refuses instead of inventing a comparison.
    ``max_changed_cells`` is accepted as a legacy argument but no longer bounds
    the returned rows.
    """
    if not isinstance(analysis_id, str) or not analysis_id:
        raise ValidationError("analysis_id is required")
    if interval is not None:
        if not isinstance(interval, dict) or set(interval) - {"start_s", "end_s"}:
            raise ValidationError("interval must be {start_s, end_s}")
    _prune_analysis_state()
    record = _analysis_record(analysis_id, archive_root)
    report = record.get("report")
    if not isinstance(report, dict):
        raise ValidationError("analysis has no report")
    _require_analysis_receipt(panorama_receipt, record, archive_root)
    if compare_analysis_id:
        if not isinstance(compare_analysis_id, str) or not compare_analysis_id:
            raise ValidationError("compare_analysis_id must be a non-empty string")
        if compare_analysis_id == analysis_id:
            raise ValidationError("compare_analysis_id must name the other analysis")
        target = _analysis_record(compare_analysis_id, archive_root)
        project_id = str(record.get("project_id") or "")
        current_capture_id: str | None = None
        for root in _analysis_store_roots(archive_root):
            if not root.is_dir():
                continue
            latest = _panorama_lifecycle(str(root)).latest_capture(project_id)
            if latest is not None:
                current_capture_id = str(latest.get("capture_id") or "") or None
                break
        delta = build_audio_delta(
            record, target, interval=interval, sources=sources, series=series,
            metrics=metrics, max_changed_cells=max_changed_cells,
            current_capture_id=current_capture_id,
        )
        response = {
            "ok": True,
            "mode": "compare",
            "analysis_id": analysis_id,
            "compare_analysis_id": compare_analysis_id,
            "revision": record.get("revision"),
            "comparison_revision": target.get("revision"),
            "audio_delta": delta,
        }
        if _delivered_bytes(response) <= DEFAULT_RESPONSE_BYTES:
            return response
        page = _panorama_lifecycle(archive_root or record.get("archive_root")).open_result_pages(
            delta, result_kind="audio_delta",
            project_id=str(record.get("project_id") or ""),
            capture_id=str(record.get("capture_id") or ""),
            revision=target.get("revision") or record.get("revision") or {},
            fact_root_value=str(record.get("capture_fact_root") or ""),
            document_hash_value=str(record.get("capture_document_sha256") or ""),
            source_handle=analysis_id,
        )
        page.update({"ok": True, "mode": "compare",
                     "analysis_id": analysis_id,
                     "compare_analysis_id": compare_analysis_id,
                     "comparison_page": True})
        return page
    evidence = build_mix_evidence(report)
    return {
        "ok": True,
        "mode": "single",
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


def _undo_operation_facts(plan: EditPlan) -> list[dict[str, Any]]:
    """Reverse the preview facts for an undo that the bridge reports bare.

    Undo restores the preview's before values, so its factual before/after is
    exactly the inverse of the applied plan.  This keeps the undo update factual
    even when REAPER returns only revisions for the undo call.
    """
    facts: list[dict[str, Any]] = []
    before_rows = plan.before if isinstance(plan.before, list) else []
    for index, operation in enumerate(plan.operations):
        original = before_rows[index] if index < len(before_rows) else {}
        original = original if isinstance(original, Mapping) else {}
        facts.append({
            "operation_index": original.get("operation_index", index + 1),
            "kind": operation.kind,
            "target_guid": operation.target_guid,
            "before": dict(operation.values),
            "after": original.get("values"),
            "changes_state": original.get("changes_state", True),
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
                archive_root: str | None = None,
                decision_class: str | None = None,
                basis: list[dict[str, Any]] | None = None,
                target_sources: list[str] | None = None,
                structural_token: str | None = None) -> dict[str, Any]:
    """Preview, apply, or undo one revision-bound batch of project edits.

    preview: supply operations and reason; nothing changes. apply: use the
    returned plan_id and the same expected_revision; the response carries an
    incremental update with only the changed current values, the current
    revision and the context token. undo: use that plan_id and the post-apply
    revision returned by apply; undo is a new forward revision with its own
    update. Each batch may contain up to 256 library-supported operations;
    generic API calls and Actions remain available for operations outside this
    transaction set.
    """
    if mode not in {"preview", "apply", "undo"}:
        raise ValidationError("mode must be preview, apply, or undo")
    expected = _revision(expected_revision, project_id=project_id)
    session = _session(project_id, bridge_session_id)
    _current_context(session, expected)
    decision_gate = None
    state = load_mode()
    if decision_class is not None and state.mode != "off":
        gate = _edit_decision_gate(
            session=session, project_id=project_id, expected=expected,
            operations=operations or [], decision_class=decision_class,
            basis=basis, target_sources=target_sources,
            archive_root=archive_root)
        gate["mode"] = state.mode
        decision_gate = gate
        if not gate["ok"] and state.mode == "strict":
            return {"mode": mode, "changed": False, "blocked": True,
                    "decision_gate": gate, "project_id": project_id,
                    "revision": expected.to_dict()}
    access: dict[str, Any] | None = None
    if mode != "preview":
        access = _require_structural_access(session=session, project_id=project_id,
                                   expected=expected, archive_root=archive_root,
                                   structural_token=structural_token,
                                   panorama_receipt=panorama_receipt)
    if mode == "preview":
        if not isinstance(operations, list) or not operations:
            raise ValidationError("preview requires a non-empty operations list")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("preview requires a short reason")
        parsed = []
        auto_calibrated: list[dict[str, Any]] = []
        leveling: list[dict[str, Any]] = []
        for index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                raise ValidationError("Each operation must be an object",
                                      details={"operation_index": index})
            if set(operation) - {"kind", "target_guid", "values"}:
                raise ValidationError("Operation contains unsupported top-level fields",
                                      details={"operation_index": index})
            kind = str(operation.get("kind", ""))
            target_guid = str(operation.get("target_guid", ""))
            values = operation.get("values", {})
            if kind == "envelope.level":
                if not isinstance(values, Mapping):
                    raise ValidationError("envelope.level needs an object of values")
                segments = values.get("segments") or []
                target_db = values.get("target_db")
                if target_db is None:
                    raise ValidationError("envelope.level needs target_db "
                                          "(take it from the measured profile)")
                points, moves = plan_curve(
                    segments, target_db=float(target_db),
                    max_gain_db=float(values.get("max_gain_db", 6.0)),
                    smooth_s=float(values.get("smooth_s", 0.15)))
                leveling.append({"target_db": round(float(target_db), 2),
                                 "max_gain_db": float(values.get("max_gain_db", 6.0)),
                                 "moves": moves})
                values = {"envelope": str(values.get("envelope", "Volume")),
                          "points": points, "clear": values.get("clear", True),
                          "active": True}
                kind = "envelope.set"
            if (kind == "fx.set_parameter" and isinstance(values, Mapping)
                    and "target" in values):
                values, calibration_note = _resolve_parameter_targets(
                    project_id=expected.project_id, track_guid=target_guid,
                    values=values, archive_root=archive_root,
                    bridge=session.bridge)
                if calibration_note:
                    auto_calibrated.append(calibration_note)
            parsed.append(EditOperation(kind=kind, target_guid=target_guid,
                                        values=values))
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
        response = {"mode": "preview", "changed": False,
                    "plan": plan.to_dict(),
                    "decision_gate": decision_gate,
                "next_call": {"tool": "reaper_edit", "mode": "apply",
                              "plan_id": plan.plan_id,
                              "project_id": project_id,
                              "expected_revision": plan.revision.to_dict()}}
        if auto_calibrated:
            response["auto_calibrated"] = auto_calibrated
        if leveling:
            response["leveling"] = leveling
        return response
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
        cached["applied_no_op"] = bool(result.get("no_op"))
        if cached["applied_no_op"]:
            return {"mode": "apply", "plan_id": plan_id, "no_op": True,
                    "undo_available": False,
                    "revision": applied.to_dict(),
                    "next_call": {
                        "tool": None,
                        "note": ("the plan changed no project fact; nothing was "
                                 "applied and there is nothing to undo")}}
        delta = _record_mutation_delta(
            lifecycle=_panorama_lifecycle(archive_root), kind="edit",
            receipt=str(panorama_receipt), project_id=project_id,
            base_revision=expected, session=session, scope="known",
            operation_facts=_edit_operation_facts(result),
            structural_fingerprint=_structural_fingerprint(access))
        advance = delta.get("advance") if isinstance(delta, Mapping) else None
        token = (str(advance["context_token"])
                 if isinstance(advance, Mapping) and advance.get("context_token") else "")
        if token:
            cached["applied_context_token"] = token
        cached["applied_archive_root"] = archive_root
        if not delta.get("delta_id") or not delta.get("acknowledgeable"):
            next_token: str | None = None
            if _structural_fingerprint(access) is not None:
                # The edit was proven by a cheap structural token; the revision
                # advanced, so mint its successor here instead of sending the
                # caller back to a capture it does not need.
                try:
                    next_token = str(mint_structural_token(session, project_id)
                                     .get("structural_token") or "") or None
                except Exception:  # noqa: BLE001 - the edit itself succeeded
                    next_token = None
            next_call = delta.get("next_call")
            if next_token:
                next_call = {"tool": "reaper_edit", "mode": "apply",
                             "structural_token": next_token,
                             "note": ("continue against this token; project audio "
                                      "stays unknown until a capture")}
            result = {"mode": "apply", "plan_id": plan_id, **_access_note(access),
                      "status": str(delta.get("status") or "unavailable"),
                      "reason": delta.get("reason"),
                      "next_call": next_call}
            if next_token:
                result["structural_token"] = next_token
            return result
        update = change_update_view(_panorama_lifecycle(archive_root), delta,
                                    context_token=token, project_id=project_id,
                                    revision=applied.to_dict())
        return {"mode": "apply", "plan_id": plan_id,
                **_access_note(access),
                "update": update,
                "next_call": {
                    "tool": None,
                    "context_token": token,
                    "note": ("the update replaces the known fields it names and carries "
                             "the current context token; no acknowledgement call is "
                             "required to continue structural edits. Project-audio "
                             "interpretation still needs a fresh capture.")}}
    applied_revision = cached.get("applied_revision")
    if not isinstance(applied_revision, ProjectRevision) or applied_revision != expected:
        raise StaleContext("Undo needs the exact revision returned by this plan's apply",
                           details={"applied_revision": (applied_revision.to_dict()
                                                          if isinstance(applied_revision, ProjectRevision)
                                                          else None),
                                    "provided_revision": expected.to_dict()})
    if cached.get("applied_no_op"):
        raise ValidationError(
            "This plan applied no change; there is nothing to undo",
            details={"plan_id": plan_id,
                     "reason": "a no-op apply records no library transaction, so "
                               "an undo could only target an earlier edit"})
    result = session.undo(expected_revision=expected, plan_id=plan_id)
    undone_revision = result.get("revision_after")
    undo_facts = _edit_operation_facts(result) or _undo_operation_facts(plan)
    delta = _record_mutation_delta(
        lifecycle=_panorama_lifecycle(archive_root), kind="undo",
        receipt=str(panorama_receipt), project_id=project_id,
        base_revision=expected, session=session, scope="known",
        operation_facts=undo_facts,
        structural_fingerprint=_structural_fingerprint(access))
    with _LOCK:
        _edit_plans.pop(plan_id, None)
    advance = delta.get("advance") if isinstance(delta, Mapping) else None
    token = (str(advance["context_token"])
             if isinstance(advance, Mapping) and advance.get("context_token") else "")
    if not delta.get("delta_id") or not delta.get("acknowledgeable"):
        return {"mode": "undo", "plan_id": plan_id, **_access_note(access),
                "status": str(delta.get("status") or "unavailable"),
                "reason": delta.get("reason"),
                "next_call": delta.get("next_call")}
    update = change_update_view(
        _panorama_lifecycle(archive_root), delta,
        context_token=token, project_id=project_id,
        revision=(undone_revision if isinstance(undone_revision, dict)
                  else expected.to_dict()))
    return {"mode": "undo", "plan_id": plan_id, **_access_note(access),
            "update": update,
            "next_call": {
                "tool": None,
                "context_token": token,
                "note": ("the update replaces the known fields it names and carries the "
                         "current context token; the earlier edit records remain history "
                         "and no acknowledgement call is required"),
            }}


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
            "revision": post_revision.to_dict(),
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
            "reason": str(delta.get("reason") or "change_not_advanceable"),
            "revision": post_revision.to_dict(),
            "next_call": delta.get("next_call") or {
                "tool": "reaper_panorama_refresh",
                "project_id": project_id,
                "then": "Refresh the panorama before the next structural edit.",
            },
        }
    update = change_update_view(lifecycle, delta, context_token=str(next_token),
                                project_id=project_id,
                                revision=post_revision.to_dict())
    return {
        "status": "advanced",
        "update": update,
        "revision": post_revision.to_dict(),
        "context_token": str(next_token),
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
            "revision": post_revision.to_dict(),
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
                "revision": post_revision.to_dict(),
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
        "revision": (result_map.get("revision_after_render")
                     or result_map.get("revision_after")),
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


def _post_hoc_change_update(session: ProjectSession, project_id: str,
                            before_digest: Mapping[str, Any]) -> dict[str, Any]:
    """Stage D3: name the known components a generic mutation actually moved."""
    context = session.snapshot(include_items=False)
    tracks = context.get("tracks") or []
    master = context.get("master") or {}
    after_digest = session.digest(project_id)
    return diff_digests(
        project_id=project_id,
        before_digest=before_digest,
        after_digest=after_digest,
        track_guids=[str(track["guid"]) for track in tracks if track.get("guid")],
        master_guid=str(master.get("guid") or ""),
        time_window={"project_start_s": 0.0,
                     "project_end_s": float(context.get("length_s") or 0.0)},
        render_contract={
            "capture": "native_render",
            "track_stage": "selected_track_via_master_parallel_batch_post_fx",
            "master_stage": "master_mix_post_fx",
            "track_signal_includes_master_processing": True,
        },
    )


def _evidence_context(*, session: ProjectSession, project_id: str,
                      expected: ProjectRevision,
                      archive_root: str | None) -> dict[str, Any]:
    """Facts every gate needs: coverage passport, evidence units, track depth."""
    context = session.snapshot(include_items=False)
    tracks = context.get("tracks") or []
    master_guid = str((context.get("master") or {}).get("guid") or "")
    depth = {str(track.get("guid")): int(track.get("folder_depth") or 0)
             for track in tracks if track.get("guid")}
    total_sources = [f"track:{guid}" for guid in depth]
    if master_guid:
        total_sources.append(f"master:{master_guid}")
    units = EvidenceStore(archive_root).all_units() if archive_root else []
    coverage: dict[str, Any] | None = None
    if archive_root:
        try:
            fingerprints = current_fingerprints(session, project_id)
        except Exception:  # noqa: BLE001 - unreadable state means "unknown", not "clean"
            fingerprints = None
        passport = passport_from_units(
            units, scope="project", project_id=project_id,
            total_sources=total_sources, revision=expected.to_dict(),
            current_fingerprints=fingerprints)
        length_s = float(context.get("length_s") or 0.0)
        required = [{"start_s": 0.0, "end_s": length_s}] if length_s > 0 else []
        coverage = {
            "length": (length_coverage(units, project_id=project_id,
                                       required=required, sources=total_sources)
                       if required else None),
            "complete": bool(passport.get("complete")),
            "sources_measured": passport.get("sources_measured"),
            "total_sources": passport.get("sources_total"),
            "measured_sources": list((passport.get("sources") or {}).get("measured") or []),
            "missing_sources": [str(gap.get("source_id")) for gap in
                                (passport.get("gaps") or [])
                                if isinstance(gap, Mapping)][:8],
        }
    stale_fraction = None
    if coverage and coverage.get("total_sources"):
        measured = coverage.get("sources_measured") or 0
        stale_fraction = round(1.0 - (measured / coverage["total_sources"]), 6)
    return {"coverage": coverage, "units": units, "depth": depth,
            "master_guid": master_guid, "stale_fraction": stale_fraction}


def _coverage_gate_report(*, session: ProjectSession, project_id: str,
                          expected: ProjectRevision,
                          archive_root: str | None,
                          requested_class: str = "global") -> dict[str, Any]:
    """Coverage check shared by save and generic mutations (Stage C1/C2)."""
    context = _evidence_context(session=session, project_id=project_id,
                                expected=expected, archive_root=archive_root)
    return evaluate(requested_class=requested_class, coverage=context["coverage"],
                    stale_fraction=context["stale_fraction"])


def _edit_decision_gate(*, session: ProjectSession, project_id: str,
                        expected: ProjectRevision,
                        operations: Sequence[Mapping[str, Any]],
                        decision_class: str,
                        basis: Sequence[Mapping[str, Any]] | None,
                        target_sources: Sequence[str] | None,
                        archive_root: str | None) -> dict[str, Any]:
    """Stage C2-C4: verify the declared class against stored evidence.

    The server, not the caller, decides that an edit touches the master or a
    folder bus; the resulting class can only go up.
    """
    context = _evidence_context(session=session, project_id=project_id,
                                expected=expected, archive_root=archive_root)
    touched = {str(op.get("target_guid") or "") for op in operations}
    return evaluate(
        requested_class=decision_class,
        target_sources=list(target_sources or []),
        master_touched=bool(context["master_guid"]) and context["master_guid"] in touched,
        bus_touched=any(context["depth"].get(guid, 0) > 0 for guid in touched if guid),
        basis=list(basis or []),
        units={str(unit.get("id")): unit for unit in context["units"] if unit.get("id")},
        coverage=context["coverage"],
        stale_fraction=context["stale_fraction"])


def _unknown_scope_response(*, delta: Mapping[str, Any] | None,
                            after: ProjectRevision, project_id: str,
                            archive_root: str | None,
                            extra: dict[str, Any]) -> dict[str, Any]:
    """Model-facing answer for an operation whose change scope is unknown.

    The sealed delta stays internal: the model only sees the new values that
    replace its known fields, the current revision and the context token.
    """
    body: dict[str, Any] = dict(extra)
    advance = delta.get("advance") if isinstance(delta, Mapping) else None
    token = (str(advance["context_token"])
             if isinstance(advance, Mapping) and advance.get("context_token") else "")
    if isinstance(delta, Mapping) and delta.get("delta_id") and delta.get("acknowledgeable"):
        body["update"] = change_update_view(_panorama_lifecycle(archive_root), delta,
                                          context_token=token, project_id=project_id,
                                          revision=after.to_dict())
        body["next_call"] = {
            "tool": None, "context_token": token,
            "note": ("the update replaces the known fields it names; this operation's "
                     "scope is unknown, so audio evidence remains stale")}
        return body
    body["status"] = str((delta or {}).get("status") or "not_modified")
    body["revision"] = after.to_dict()
    body["next_call"] = (delta or {}).get("next_call") or {
        "tool": "reaper_panorama_status",
        "note": "no project fact changed; re-read the project context"}
    return body


@server.tool()
@_guard
def reaper_api_call(name: str,
                    values: list[Any],
                    project_id: str,
                    expected_revision: dict[str, Any],
                    bridge_session_id: str | None = None,
                    timeout_s: float = 30.0,
                    panorama_receipt: str | None = None,
                    archive_root: str | None = None,
                    decision_class: str | None = None,
                    structural_token: str | None = None) -> dict[str, Any]:
    """Call any function in the official REAPER ReaScript catalog.

    values are positional and must match the catalog signature exactly.
    Represent host objects as {"$reaper_ref":{"kind":"MediaTrack","guid":"..."}}
    or pass a live $reaper_handle returned by an earlier call. Null is accepted
    only for optional parameters. Arrays use ordinary numeric JSON lists.

    A mutating call returns a change update whose scope is explicitly
    ``unknown``: the server does not pretend to know which tracks a raw API call
    touched, so all project audio evidence is invalidated conservatively.
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
    state = load_mode()
    if decision_class is not None and state.mode != "off":
        gate = _coverage_gate_report(session=session, project_id=project_id,
                                     expected=expected, archive_root=archive_root,
                                     requested_class=decision_class)
        gate["mode"] = state.mode
        if not gate["ok"] and state.mode == "strict":
            return {"ok": False, "blocked": True, "decision_gate": gate,
                    "project_id": project_id, "revision": expected.to_dict()}
    access: Mapping[str, Any] | None = None
    if not getattr(_API_GATE_BYPASS, "enabled", False):
        access = _require_structural_access(session=session, project_id=project_id,
                                            expected=expected, archive_root=archive_root,
                                            structural_token=structural_token,
                                            panorama_receipt=panorama_receipt)
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
    digest_before = session.digest(project_id)
    result = session.bridge.call_api(name, *prepared, timeout_s=float(timeout_s))
    after_value = result.project_after if isinstance(result.project_after, Mapping) else None
    after_final = (ProjectRevision.from_dict(dict(after_value))
                   if isinstance(after_value, Mapping) else expected)
    delta: dict[str, Any] | None = None
    if isinstance(after_value, Mapping):
        after_revision = after_final
        if after_revision != expected:
            delta = _record_mutation_delta(
                lifecycle=_panorama_lifecycle(archive_root), kind="api_call",
                receipt=str(panorama_receipt), project_id=project_id,
                base_revision=expected, session=session, scope="unknown",
                structural_fingerprint=_structural_fingerprint(access),
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
    change_update = (_post_hoc_change_update(session, project_id, digest_before)
                     if delta is not None and delta.get("status") != "not_modified"
                     else None)
    return _unknown_scope_response(
        delta=delta, after=after_final, project_id=project_id,
        archive_root=archive_root,
        extra={"name": result.name,
               "change_update": change_update,
               "returns": list(result.returns),
               "arrays": {str(index): list(items)
                          for index, items in result.arrays.items()}})


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
                  archive_root: str | None = None,
                  structural_token: str | None = None) -> dict[str, Any]:
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
    access = _require_structural_access(session=session, project_id=project_id,
                                        expected=expected, archive_root=archive_root,
                                        structural_token=structural_token,
                                        panorama_receipt=panorama_receipt)
    project_handle = ReaperHandle(kind="ReaProject", project_id=project_id,
                                  change_count=expected.change_count,
                                  stable_id=project_id)
    digest_before_action = session.digest(project_id)
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
            structural_fingerprint=_structural_fingerprint(access),
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
    change_update = (_post_hoc_change_update(session, project_id, digest_before_action)
                     if after != expected else None)
    return _unknown_scope_response(
        delta=delta, after=after, project_id=project_id, archive_root=archive_root,
        extra={"change_update": change_update,
               "command_id": command_id, "section_id": section_id,
               "state_changed": after != expected,
               "returns": list(result.returns)})


@server.tool()
@_guard
def reaper_structural_token(project_id: str,
                            bridge_session_id: str | None = None) -> dict[str, Any]:
    """Mint a structural token for an edit: cheap, no capture and no audio.

    The token binds project id, instance, epoch, revision and the digest of the
    project map (tracks, routing, FX, automation).  Pass it to ``reaper_edit``
    apply/undo, ``reaper_api_call``, ``reaper_action`` or ``reaper_save``.
    It authorises structure only: audio claims still need measurements.
    """
    session = _session(project_id, bridge_session_id)
    return mint_structural_token(session, project_id)


@server.tool()
@_guard
def reaper_mode(action: str = "get",
                mode: str | None = None,
                stale_threshold_pct: int | None = None,
                reason: str | None = None,
                requested_by: str = "user",
                lock: bool | None = None) -> dict[str, Any]:
    """Read or change the server work mode (strict / advisory / off).

    Changing the mode is a user decision.  Call ``action="set"`` only when the
    user asked for it in this conversation, name them in ``requested_by`` and
    state why in ``reason``.  The model never changes the mode on its own and
    never to get past a gate: if the mode blocks the work, ask the user.
    ``lock=true`` freezes the rules; unlocking is only possible outside MCP.
    """
    if action == "get":
        return {"mode_state": load_mode().as_dict()}
    if action != "set":
        raise ValidationError("action must be get or set")
    if not reason or not str(reason).strip():
        raise ValidationError("changing the mode needs a stated reason")
    state = set_mode(mode=mode, stale_threshold_pct=stale_threshold_pct,
                     reason=str(reason), requested_by=requested_by)
    if lock is not None:
        state = set_locked(bool(lock), reason=str(reason), requested_by=requested_by)
    return {"mode_state": state.as_dict()}


@server.tool()
@_guard
def reaper_save(mode: str,
                project_id: str,
                expected_revision: dict[str, Any],
                path: str | None = None,
                overwrite_existing: bool = False,
                bridge_session_id: str | None = None,
                panorama_receipt: str | None = None,
                archive_root: str | None = None,
                structural_token: str | None = None) -> dict[str, Any]:
    """Save through REAPER. save_as is absolute .RPP and refuses overwrite by default.

    A save is not an assertion: it checks project/instance/epoch/revision and a
    path conflict, never coverage or audio evidence. ``panorama_receipt`` is a
    deprecated compatibility parameter — when supplied it is still verified, so
    a caller cannot believe it was checked when it was not; new callers should
    pass ``structural_token`` or nothing.

    A save that moves the revision or path sets ``context_invalidated`` and
    returns no changed-value payload: the previous token cannot be reused, and no
    facts about a new document are invented.
    """
    if mode not in {"save_current", "save_as"}:
        raise ValidationError("mode must be save_current or save_as")
    if not isinstance(overwrite_existing, bool):
        raise ValidationError("overwrite_existing must be boolean")
    expected = _revision(expected_revision, project_id=project_id)
    session = _session(project_id, bridge_session_id)
    _current_context(session, expected)
    if structural_token:
        require_structural_token(session, project_id, structural_token)
    if panorama_receipt:
        # Deprecated, but never silently ignored: if a caller presents one, it
        # is checked rather than believed validated.
        _require_panorama_receipt(panorama_receipt, project_id,
                                  expected.to_dict(), archive_root)
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
    new_project_id = str(after_revision.get("project_id") or project_id)
    if new_project_id != project_id:
        # Summaries are keyed by project_id; a save-as must not throw them away.
        try:
            _plugin_state_store(archive_root).migrate(project_id, new_project_id)
        except Exception:  # noqa: BLE001 - a summary cache must never fail a save
            pass
    result_value = {
        "result": result,
        "revision": after_revision,
        "saved": result.get("saved") is True,
        "context_invalidated": changed_identity,
    }
    if changed_identity:
        result_value["next_call"] = {
            "tool": "reaper_capture_snapshot",
            "project_id": str(after_revision.get("project_id") or project_id),
            "then": ("Saving moved the project revision or path; the current context "
                     "token no longer matches. Capture again or refresh the structural "
                     "context before editing. No changed values are invented for a save."),
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


MODEL_GUIDE: dict[str, Any] = {
    "schema": "reaper-workbench.model-guide/1",

    "modes": ("The work mode (strict / advisory / off), the stale-share threshold and the "
              "lock belong to the user: change them only at the user's explicit request "
              "through reaper_mode, never on your own and never to pass a gate; if the "
              "mode blocks the work, ask. Every response repeats it."),
    "tokens": ("An edit needs a current structural_token from reaper_structural_token "
               "(project, instance, epoch, revision and the project-map digest) - one cheap "
               "read, no capture, no audio. A full panorama is needed only for audio claims "
               "and global verdicts; a panorama receipt is still accepted for compatibility."),    "purpose": ("Canonical navigation contract for a model using REAPER Workbench. "
                "It names the complete panorama path, current-state updates, exact query, "
                "operation discovery and verification; it does not prescribe a genre "
                "or a fixed mixing recipe."),
    "project_context": {
        "start": "reaper_panorama_open() with no mode argument (default auto).",
        "auto": ("returns the complete full view with receipt/context_token when it fits "
                 "the measured budget; otherwise page 1 plus a manifest. An explicit "
                 "mode='single' over budget also falls back to pages."),
        "pages": ("pages are the complete path. If manifest.page_ids is present, fetch "
                  "every id in order. If it is omitted for a very large set, enumerate "
                  "manifest.page_id_pattern for 1..page_count. Do not skip a page."),
        "ack": ("reaper_panorama_ack with every page_hash in order, or with "
                "manifest_sha256 alone. A partial set returns missing page ids and mints "
                "no receipt or context token."),
        "continuity": ("The first complete panorama establishes the model's known "
                       "project state. Keep the latest context_token as the identity of "
                       "that state. For routine updates, call "
                       "reaper_panorama_changes_since with this token; do not repeatedly "
                       "re-anchor to the first capture or request the whole panorama "
                       "after each edit. Merge returned changed facts into the known "
                       "state. A new chat or an unprovable/lost token starts from a full "
                       "panorama."),
        "stale_or_lost": ("a stale revision needs a new open; reaper_panorama_refresh "
                          "re-opens the current capture. A handle survives a server "
                          "restart but expires after its declared TTL."),
        "claim_limit": ("the receipt proves these exact pages were served, not that the "
                        "model read them and not that it heard audio."),
    },
    "exact_rows": {
        "tool": "reaper_panorama_query",
        "selectors": ["data", "objects", "interval", "series", "metrics", "kinds",
                      "bands_hz", "resolution_s"],
        "rule": ("one coherent selection per call; use it for relation edges, series "
                 "cells, MIDI notes and envelope points. A selection too large for one "
                 "result becomes complete result_page pages; fetch every page and ack. "
                 "Result-page ack proves exact reconstruction only and mints no receipt, "
                 "context token or edit permission. Never treat a missing or aggregated "
                 "value as proof of absence."),
    },
    "scoped_analysis": {
        "tool": "reaper_analyze",
        "arguments": ["capture_id", "analyzers", "source_ids", "intervals"],
        "catalog": "reaper_capabilities(mode='tools').analyzers",
        "rule": ("one batch runs several stable analyzer IDs against named sources and "
                 "exact song intervals. The result is explicitly scoped, not whole-project "
                 "coverage, and is bound to its capture/revision. Cost classes are relative, "
                 "not measured seconds. Large scoped results use complete result pages."),
        "no_loop": ("a full panorama is not an automatic next step after edits or scoped "
                    "analysis. Keep working from the acknowledged view, change journal and "
                    "local evidence while they support the next decision; request a new "
                    "full panorama only when the next consequential global decision needs "
                    "fresh whole-project evidence, the user asks for a complete overview, "
                    "or before a final whole-project conclusion."),
    },
    "updates": {
        "capture": ("For normal continuity, reaper_panorama_changes_since uses the "
                    "latest context_token and returns not_modified or only the net "
                    "current facts newer than that token. An explicit base_capture_id / "
                    "base_fact_root without a token is an informational archived-capture "
                    "comparison, not the normal model-state update."),
        "mutation": ("reaper_edit / reaper_api_call / reaper_action / "
                     "reaper_render_candidate return an update: stable object ids and "
                     "only the new current values, the current revision and the "
                     "successor token. No internal mutation record, base/target "
                     "revision or before/after pair is exposed."),
        "inline": "a small update carries context_token in the same response.",
        "paged": ("an update too large for one result returns status='change_page' with "
                  "page 1, a manifest and a handle. Fetch every declared change page, "
                  "then ack all hashes or the manifest hash; only that ack advances the "
                  "token, exactly once."),
        "identity": ("ops address stable object ids and semantic paths, not array "
                     "positions; an insertion or FX move must not rename later objects."),
        "metric_table": ("measurement-heavy updates may carry the same exact metric ops "
                         "once in metric_table: integer-indexed columns plus "
                         "exact dictionaries and path_pattern. Expand every row; no "
                         "changed cell is omitted."),
        "comparability": ("computed only from identity, method/version, metric, unit, "
                          "stage, channel/window and grid criteria. Missing evidence is "
                          "unknown; a stated mismatch is incomparable; each side's exact "
                          "measured values are still reported in an explicit comparison. "
                          "A normal state update is not a numeric metric difference. "
                          "No better/worse verdict."),
    },
    "discovery": {
        "tasks": "reaper_capabilities(mode='tools')",
        "api_index": "reaper_capabilities(mode='summary') then mode='domain'.",
        "search": "reaper_api_search(query) returns candidates, not recommendations.",
        "dispatch": ("reaper_api_call with a typed handle; reaper_action for a named "
                     "Action. Neither is a separate tool per ReaScript function."),
    },
    "edits": {
        "flow": ("preview a plan (no token needed), apply with expected_revision plus the "
                 "structural_token, read back the affected "
                 "objects and the returned update. Save only when requested."),
        "authority": ("only a complete panorama receipt/context token unlocks a project "
                      "edit; a read_scope is read-only."),
    },
    "verification": [
        "keep project_id, instance/epoch, change_count, capture_id and receipt/token together",
        "check coverage, counts, units, method/version, stage, window and provenance before a conclusion",
        "distinguish saved, read back, measured and auditioned",
        "a render mismatch is failed verification; do not retry blindly",
    ],
}


@server.resource("reaper://guide/model", name="Model navigation guide",
                 description=("Canonical, always-current MCP navigation contract: full "
                              "panorama, page ack, current-state updates, exact query, "
                              "discovery and verification."),
                 mime_type="application/json")
def model_guide_resource() -> str:
    return json.dumps(MODEL_GUIDE, ensure_ascii=False, separators=(",", ":"))


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
    record = _analysis_record(analysis_id)
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
def reaper_measure(archive_root: str,
                   project_id: str | None = None,
                   sources: list[str] | str = "all",
                   start_s: float | None = None,
                   end_s: float | None = None,
                   stage: str = "via_master",
                   channels: str = "stereo",
                   sample_rate: int | None = None,
                   tail_ms: int = 0,
                   analysis_workers: int | None = None,
                   step_s: float | None = None,
                   bridge_session_id: str | None = None) -> dict[str, Any]:
    """Measure only the requested sources over a caller-declared interval.

    This is the edit -> listen unit.  It renders the named sources (or the
    master alone with ``stage="master"``), measures them, stores evidence units
    and mints an evidence token.  It does **not** create a project capture and
    does not mint a global receipt.

    The interval is a parameter: pass ``start_s``/``end_s`` for the exact
    window you want measured (both or neither).  The library has no notion of
    bars, sections or "enough" — it only reports what it measured there.

    ``stage="premaster"`` is refused with ``stage_not_rendered``: this renderer
    produces only via-master stems and the master mix, and it never substitutes
    one stage for another.
    """
    if (start_s is None) != (end_s is None):
        raise ValidationError("start_s and end_s must both be provided or both be null")
    section = None
    if start_s is not None and end_s is not None:
        section = (float(start_s), float(end_s))
    session = _session(project_id, bridge_session_id)
    before = session.snapshot(include_items=False).get("revision") or {}
    service = MeasureService(session, archive_root)
    result = service.measure(sources=sources, section=section, stage=stage,
                             channels=channels, sample_rate=sample_rate,
                             tail_ms=tail_ms, analysis_workers=analysis_workers,
                             step_s=step_s)
    after = (result.get("revision") or {}) if isinstance(result, dict) else {}
    assert_project = str(before.get("project_id") or after.get("project_id") or "")
    if before.get("change_count") is not None and after.get("change_count") is not None:
        register_self_noise(assert_project, int(before["change_count"]),
                            int(after["change_count"]))
    if isinstance(result, dict):
        # Two identical calls produce identical bytes; the id keeps each
        # measurement an individually addressable fact for the client.
        result.setdefault("measurement_id", uuid.uuid4().hex)
    return result


@server.tool()
@_guard
def reaper_freshness(archive_root: str,
                     project_id: str | None = None,
                     capture_id: str | None = None,
                     bridge_session_id: str | None = None) -> dict[str, Any]:
    """Per-source, per-stage freshness of a capture against the live project.

    ``sources[source_id][stage]`` is ``current``/``stale``/``unknown`` with the
    reason and both fingerprints.  A track edit makes that track and the master
    stale; a master/bus edit makes every source under it stale.  Unknown is
    never treated as clean.
    """
    session = _session(project_id, bridge_session_id)
    selected = project_id or session.project_id
    if not selected:
        raise ValidationError("project_id is required when no project is active")
    report = freshness_report(session, archive_root, project_id=selected,
                              capture_id=capture_id)
    return {"ok": True, "freshness": report}


@server.tool()
@_guard
def reaper_coverage(archive_root: str,
                    project_id: str | None = None,
                    scope: str = "global",
                    bridge_session_id: str | None = None) -> dict[str, Any]:
    """Coverage passport built from stored evidence units of this project.

    Reports measured/total sources, the intervals each source actually covers,
    the revision and every gap.  A global claim without a complete passport is
    refused by the gates; this tool is how a caller sees the numbers first.
    """
    session = _session(project_id, bridge_session_id)
    context = session.snapshot(include_items=False)
    project = project_id or str((context.get("revision") or {}).get("project_id"))
    tracks = context.get("tracks") or []
    master = context.get("master") or {}
    total = [f"track:{track.get('guid')}" for track in tracks if track.get("guid")]
    if master.get("guid"):
        total.append(f"master:{master.get('guid')}")
    revision = context.get("revision") or {}
    store = EvidenceStore(archive_root)
    passport = passport_from_units(store.all_units(), scope=scope,
                                   project_id=project, total_sources=total,
                                   revision=revision)
    return {"ok": True, "passport": passport}


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
                         max_page_bytes: int | None = None,
                         mode: Literal["auto", "pages", "single"] = "auto",
                         max_response_bytes: int | None = None) -> dict[str, Any]:
    """Open the whole-project view. The default is size-independent.

    ``mode="auto"`` (default) tries one measured full-view response and, if the
    declared budget cannot hold it, delivers the same complete view as pages
    with a manifest.  ``mode="pages"`` always pages.  ``mode="single"`` asks for
    the fast path but still falls back to pages when the measured single
    response is over budget, so no caller is refused a full panorama because of
    a transport limit.  The pages are the complete path: fetch every declared
    page, then acknowledge all hashes (or the manifest hash).

    The panorama is fixed to one capture, revision and ``view_sha256``.  Every
    mode measures the response it actually serves.
    """
    if mode not in {"auto", "pages", "single"}:
        raise ValidationError("mode must be 'auto', 'pages' or 'single'",
                              details={"mode": mode})
    lifecycle = _panorama_lifecycle(archive_root)
    if mode == "pages":
        return lifecycle.open_view(project_id, expected_revision=expected_revision,
                                   capture_id=capture_id,
                                   page_target_bytes=page_target_bytes,
                                   max_page_bytes=max_page_bytes,
                                   requested_mode="pages")
    if mode == "single":
        return lifecycle.open_auto(project_id, expected_revision=expected_revision,
                                   capture_id=capture_id,
                                   page_target_bytes=page_target_bytes,
                                   max_page_bytes=max_page_bytes,
                                   max_response_bytes=max_response_bytes,
                                   requested_mode="single")
    return lifecycle.open_auto(project_id, expected_revision=expected_revision,
                               capture_id=capture_id,
                               page_target_bytes=page_target_bytes,
                               max_page_bytes=max_page_bytes,
                               max_response_bytes=max_response_bytes,
                               requested_mode="auto")


@server.tool()
@_guard
def reaper_panorama_page(handle: str, revision: dict[str, Any], archive_root: str,
                         page_id: str) -> dict[str, Any]:
    """Fetch one page of a panorama view, delta, query or audio-delta result.

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
                        page_hashes: list[str] | None = None,
                        page_manifest_sha256: str | None = None) -> dict[str, Any]:
    """Verify every delivered page and mint the complete-view receipt.

    The server checks that every page was actually fetched on this handle, that
    the supplied hashes match the served pages in order, and that the pages
    reconstruct the exact served payload hash.  For a large page set, pass the
    single ``manifest_sha256`` instead of a hash list; it commits to the same
    ordered page set.  A query or audio-delta result-page set returns a complete
    reconstruction receipt with no context token and no mutation permission; a
    partial page set returns ``incomplete`` and mints nothing.  The receipt
    proves delivery of these exact pages, not that the model read or understood
    them.
    """
    lifecycle = _panorama_lifecycle(archive_root)
    return lifecycle.ack_pages(handle, revision, list(page_hashes or []),
                               page_manifest_sha256=page_manifest_sha256)


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
    """Return exact high-frequency rows for a semantic selection, paged if large.

    The selection is exact: series, MIDI, envelope, relation and metric rows are
    returned from the acknowledged capture, never truncated or narrowed because
    of transport size.  If the result does not fit one measured response, the
    same payload is delivered as complete ``result_page`` pages through
    ``reaper_panorama_page`` and verified with ``reaper_panorama_ack``.  That
    acknowledgement proves exact reconstruction only; it mints no receipt or
    edit token and does not change the capture context.  Name what you want:

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
    manifest, so a stale or mismatched reference fails closed.

    Authorization: ``revision`` + ``archive_root`` + ``handle`` +
    ``panorama_receipt`` from the acknowledged panorama. A query without that
    receipt fails closed. The legacy ``read_scope`` argument is accepted only for
    older single-response flows; it is not a receipt, cannot complete a panorama
    and never authorizes an edit/API/action/save call.  Scoped results are not
    whole-project coverage.
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
        return _thaw(lifecycle.query(
            "", revision, data=data, objects=objects, interval=interval,
            series=series, metrics=metrics, kinds=kinds, bands_hz=bands_hz,
            resolution_s=resolution_s, include_pairs=include_pairs,
            max_response_bytes=max_response_bytes, read_scope=read_scope))
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
    return _thaw(result)


@server.tool()
@_guard
def reaper_panorama_status(project_id: str,
                           archive_root: str,
                           handle: str | None = None) -> dict[str, Any]:
    """Report the latest captured revision/root and one handle's delivery state.

    The handle may be a panorama page handle, a paged change update, or a
    paged exact query/audio/scoped result handle.  Every paged handle reports
    its ``payload_sha256``; a result handle also reports ``result_kind``; all
    report page count, received/missing pages and completion without repeating
    the payload.
    """
    return _panorama_lifecycle(archive_root).status(project_id, handle)


@server.tool()
@_guard
def reaper_panorama_changes_since(project_id: str,
                                  archive_root: str,
                                  base_capture_id: str | None = None,
                                  base_revision: dict[str, Any] | None = None,
                                  base_fact_root: str | None = None,
                                  context_token: str | None = None,
                                  bridge_session_id: str | None = None) -> dict[str, Any]:
    """Return only the facts newer than the supplied context token.

    The token is the model's known state: a token already at the newest known
    revision returns ``not_modified`` and never replays earlier changes. An
    older token receives the net current values of every sealed mutation after
    its revision, newest value per field, with the token left un-advanced.
    Without a token the call compares two exact captures and returns
    ``status="change_set"``. A wrong or broken base returns ``stale_base``.
    A large change set is returned as ``status="change_page"`` with a handle and
    manifest; fetch every declared change page and acknowledge all hashes (or
    the manifest hash). The update never omits a change or invents a
    better/worse verdict.
    """
    live_revision: Mapping[str, Any] | None = None
    if context_token:
        try:
            live_revision = _session(project_id, bridge_session_id).snapshot().get("revision")
        except Exception:  # noqa: BLE001 - archive-only answer, marked as such
            live_revision = None
    return _panorama_lifecycle(archive_root).changes_since(
        project_id, base_capture_id=base_capture_id, base_revision=base_revision,
        base_fact_root=base_fact_root, context_token=context_token,
        current_revision=live_revision)


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
