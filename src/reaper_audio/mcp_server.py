"""LLM-oriented MCP interface for the native REAPER audio library.

Run with ``python -m reaper_audio.mcp_server`` using stdio. The server speaks
only to the resident ReaScript bridge; it does not inspect or modify REAPER
process memory.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, is_dataclass
from functools import wraps
from pathlib import Path
from typing import Any, NotRequired, TypedDict
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer

from .analysis.artifacts import cleanup_audio_job, cleanup_stale_audio_jobs
from .analysis.audio import load_audio
from .analysis.edit_map_report import (build_edit_map_from_report,
                                       tempo_points_from_report)
from .analysis.mix_evidence import build_mix_evidence
from .daw.session import EditOperation, EditPlan, ProjectRevision, ProjectSession
from .errors import (BridgeUnavailable, ReaperAudioError, StaleContext,
                     ValidationError)
from .models import Section
from .reaper.api_catalog import get_api_catalog
from .reaper.bridge_client import ReaperBridge
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


class ReferenceWindow(TypedDict):
    path: str
    start_s: float
    end_s: float
    name: NotRequired[str]

server = MCPServer(
    "reaper-workbench",
    title="REAPER Workbench",
    version="0.2.0",
    description=("Native REAPER control and audio analysis through the documented "
                 "ReaScript API and resident local bridge."),
    instructions=(
        "Start with reaper_session(action='health') and reaper_project to establish "
        "the project and revision. Use reaper_capabilities and reaper_api_search to "
        "discover operations; do not assume a fixed musical workflow. Analyze all "
        "processed tracks and the master together with reaper_analyze. Project edits "
        "are batched as preview, then apply, then read back; every write is tied to "
        "project_id and expected_revision. The generic API and Actions remain "
        "available for documented REAPER functions beyond convenience edits. "
        "Audio analysis uses native REAPER render capture; raw AudioAccessor samples "
        "are pre-FX and are not processed mix stems. Temporary audio is outside the "
        "project and has a bounded lease."
    ),
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, ReaperHandle):
        return value.to_wire()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
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
        "stale_context": "Refresh the project context, inspect what changed, then prepare a new plan.",
        "bridge_unavailable": "Check that REAPER is open and its resident ReaScript bridge is running.",
        "bridge_timeout": "Check the REAPER bridge status before retrying; the operation may still have completed.",
        "validation_error": "Correct the named field or use capability/API search to confirm the signature.",
        "api_unavailable": "Check the REAPER version and discover an available alternative in the API catalog.",
        "transaction_failed": "Read the project state and undo status before attempting another edit.",
        "analysis_unavailable": "Inspect capture and analysis details, then retry a smaller time range if appropriate.",
        "artifact_cleanup_failed": "Do not delete the directory manually; inspect the manifest and preserved paths.",
    }.get(code, "Inspect the structured error details and verify current REAPER state before retrying.")


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
    return {"ok": False, "error": {"code": code, "message": message,
                                     "details": _jsonable(details),
                                     "next_action": _next_action(code)}}


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


def _report_without_local_paths(report: dict[str, Any]) -> dict[str, Any]:
    safe = dict(report)
    safe.pop("audio_files", None)
    return safe


@server.tool()
@_guard
def reaper_session(action: str = "health",
                   bridge_session_id: str | None = None) -> dict[str, Any]:
    """Inspect the REAPER connection or list live instances/projects.

    action: health, list_instances, or list_projects. When several REAPER
    instances are live, pass bridge_session_id to bind subsequent calls.
    """
    if action not in {"health", "list_instances", "list_projects"}:
        raise ValidationError("action must be health, list_instances, or list_projects")
    bridge = ReaperBridge(bridge_session_id=bridge_session_id)
    if action == "list_instances":
        return {"instances": bridge.discover_instances()}
    if action == "list_projects":
        return bridge.list_projects()
    return bridge.health(timeout_s=3.0)


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
                   expected_revision: dict[str, Any] | None = None) -> dict[str, Any]:
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
                   detail: str = "summary") -> dict[str, Any]:
    """Run the complete available audio suite on all tracks and the rendered master.

    This is one batch operation. It returns a concise report plus temporary
    audio-resource URIs for listening. Capture is a processed REAPER render;
    no project items/stems are added. reference_paths analyzes complete files;
    reference_windows selects source-file-local windows independently from the
    project section. artifact_role holds one lease each for temporary, baseline,
    or final; replacing a role releases its previous temporary audio files.
    """
    if (start_s is None) != (end_s is None):
        raise ValidationError("start_s and end_s must be supplied together")
    section = Section(start_s, end_s) if start_s is not None else None
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
    session = _session(project_id, bridge_session_id)
    expected = (_revision(expected_revision, project_id=project_id)
                if expected_revision is not None else None)
    project_before = _current_context(session, expected)
    selected_project_id = project_before["revision"]["project_id"]
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

    analysis_id = f"a-{int(time.time())}-{os.urandom(6).hex()}"
    record = {"analysis_id": analysis_id, "project_id": selected_project_id,
              "revision": revision, "role": artifact_role,
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
        "section": report.get("section"),
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
                    end_s: float | None = None) -> dict[str, Any]:
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
def reaper_mix_evidence(analysis_id: str) -> dict[str, Any]:
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
    evidence = build_mix_evidence(report)
    return {
        "ok": True,
        "analysis_id": analysis_id,
        "revision": record.get("revision"),
        "mix_evidence": evidence,
    }


@server.tool()
@_guard
def reaper_edit(mode: str,
                project_id: str,
                expected_revision: dict[str, Any],
                operations: list[dict[str, Any]] | None = None,
                reason: str | None = None,
                plan_id: str | None = None,
                bridge_session_id: str | None = None) -> dict[str, Any]:
    """Preview, apply, or undo one revision-bound batch of project edits.

    preview: supply operations and reason; nothing changes. apply: use the
    returned plan_id and the same expected_revision. undo: use that plan_id
    and the post-apply revision returned by apply. Each batch may contain up
    to 256 library-supported operations; generic API calls and Actions remain
    available for operations outside this transaction set.
    """
    if mode not in {"preview", "apply", "undo"}:
        raise ValidationError("mode must be preview, apply, or undo")
    expected = _revision(expected_revision, project_id=project_id)
    session = _session(project_id, bridge_session_id)
    _current_context(session, expected)
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
    if cached["project_id"] != project_id or plan.revision != expected:
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
        cached["applied_revision"] = ProjectRevision.from_dict(applied_revision)
        return {"mode": "apply", "plan_id": plan_id,
                "result": result,
                "next_call": {"tool": "reaper_project", "project_id": project_id,
                              "expected_revision": applied_revision,
                              "then": "Read back touched objects; use reaper_edit(mode='undo') only if this plan is still the newest undo record."}}
    applied_revision = cached.get("applied_revision")
    if not isinstance(applied_revision, ProjectRevision) or applied_revision != expected:
        raise StaleContext("Undo needs the exact revision returned by this plan's apply",
                           details={"applied_revision": (applied_revision.to_dict()
                                                          if isinstance(applied_revision, ProjectRevision)
                                                          else None),
                                    "provided_revision": expected.to_dict()})
    result = session.undo(expected_revision=expected)
    with _LOCK:
        _edit_plans.pop(plan_id, None)
    return {"mode": "undo", "plan_id": plan_id, "result": result}


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
                    timeout_s: float = 30.0) -> dict[str, Any]:
    """Call any function in the official REAPER ReaScript catalog.

    values are positional and must match the catalog signature exactly.
    Represent host objects as {"$reaper_ref":{"kind":"MediaTrack","guid":"..."}}
    or pass a live $reaper_handle returned by an earlier call. Null is accepted
    only for optional parameters. Arrays use ordinary numeric JSON lists.
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
    return {"name": result.name,
            "returns": list(result.returns),
            "arrays": {str(index): list(items) for index, items in result.arrays.items()},
            "project_before": result.project_before,
            "project_after": result.project_after,
            "expected_revision": expected.to_dict(),
            "note": "Re-read project context after any API call that may mutate state."
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
                  bridge_session_id: str | None = None) -> dict[str, Any]:
    """Search a REAPER Action or invoke it against an identified revision.

    Search requires query_object and query_operation. Main-section invocation
    uses revision-bound Main_OnCommandEx. MIDI-editor section 32060 uses the
    active MIDI editor and also requires expected_revision.
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
    return {"command_id": command_id, "section_id": section_id,
            "returns": list(result.returns),
            "project_before": expected.to_dict(),
            "project_after": after.to_dict(),
            "state_changed": after != expected,
            "verification": "Read the affected project objects and audio; Actions are not assumed to be undoable by this server."}


@server.tool()
@_guard
def reaper_save(mode: str,
                project_id: str,
                expected_revision: dict[str, Any],
                path: str | None = None,
                overwrite_existing: bool = False,
                bridge_session_id: str | None = None) -> dict[str, Any]:
    """Save through REAPER. save_as is absolute .RPP and refuses overwrite by default."""
    if mode not in {"save_current", "save_as"}:
        raise ValidationError("mode must be save_current or save_as")
    if not isinstance(overwrite_existing, bool):
        raise ValidationError("overwrite_existing must be boolean")
    expected = _revision(expected_revision, project_id=project_id)
    session = _session(project_id, bridge_session_id)
    before = _current_context(session, expected)
    if mode == "save_current":
        if path is not None or overwrite_existing:
            raise ValidationError("path and overwrite_existing only apply to save_as")
        result = session.save_current()
    else:
        if not path:
            raise ValidationError("save_as requires path")
        result = session.save_as(path, overwrite_existing=overwrite_existing)
    after = session.snapshot()
    return {"result": result,
            "revision_before": before.get("revision"),
            "revision_after": after.get("revision"),
            "saved": result.get("saved") is True}


@server.tool()
@_guard
def reaper_release(analysis_id: str) -> dict[str, Any]:
    """Release one analysis report and its manifest-owned temporary WAVs now."""
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
    return _analysis_artifact_path(record, source_guid).read_bytes()


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
    return reaper_api_call(name=name, values=values,
                           project_id=ctx["revision"]["project_id"],
                           expected_revision=ctx["revision"])["returns"]


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
                             bridge_session_id: str | None = None) -> dict[str, Any]:
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
                        bridge_session_id: str | None = None) -> dict[str, Any]:
    """Ask Music Flamingo a free-form question about the master or a file, optionally bounded."""
    if not isinstance(question, str) or not question.strip():
        raise ValidationError("question must be a non-empty string")
    return reaper_flamingo_describe(target=target, start_s=start_s, end_s=end_s,
                                    representations=representations,
                                    reference_path=reference_path,
                                    prompt=question.strip(),
                                    project_id=project_id,
                                    bridge_session_id=bridge_session_id)


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
                           bridge_session_id: str | None = None) -> dict[str, Any]:
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
                            bridge_session_id: str | None = None) -> dict[str, Any]:
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
            handle = reaper_api_call(name="GetTrack", values=[proj, t["index"]],
                                     project_id=ctx["revision"]["project_id"],
                                     expected_revision=ctx["revision"])["returns"][0]
            count = int(reaper_api_call(name="CountTrackMediaItems", values=[handle],
                                        project_id=ctx["revision"]["project_id"],
                                        expected_revision=ctx["revision"])["returns"][0])
        except Exception:                       # noqa: BLE001
            continue
        for i in range(min(count, 20)):
            try:
                item = reaper_api_call(name="GetTrackMediaItem", values=[handle, i],
                                       project_id=ctx["revision"]["project_id"],
                                       expected_revision=ctx["revision"])["returns"][0]
                pos = float(reaper_api_call(name="GetMediaItemInfo_Value",
                                            values=[item, "D_POSITION"],
                                            project_id=ctx["revision"]["project_id"],
                                            expected_revision=ctx["revision"])["returns"][0])
                length = float(reaper_api_call(name="GetMediaItemInfo_Value",
                                               values=[item, "D_LENGTH"],
                                               project_id=ctx["revision"]["project_id"],
                                               expected_revision=ctx["revision"])["returns"][0])
            except Exception:                   # noqa: BLE001
                continue
            if start_s is not None and (pos >= end_s or pos + length <= start_s):
                continue
            items.append({"track": t["name"], "start": round(pos, 2),
                          "end": round(pos + length, 2)})
    markers: list[dict[str, Any]] = []
    try:
        total = int(reaper_api_call(name="CountProjectMarkers", values=[proj],
                                    project_id=ctx["revision"]["project_id"],
                                    expected_revision=ctx["revision"])["returns"][0])
        for i in range(min(total, 20)):
            row = reaper_api_call(name="EnumProjectMarkers3", values=[proj, i],
                                  project_id=ctx["revision"]["project_id"],
                                  expected_revision=ctx["revision"])["returns"]
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
                            bridge_session_id: str | None = None) -> dict[str, Any]:
    """Arrangement context as text: tempo map, bar numbers, items, markers, pan/level.

    This is the "timeline" that can be handed to an audio model together with the request.
    No model and no GPU are involved; it only reads the open project.
    """
    from .analysis import timeline_context as tc
    session = _session(project_id, bridge_session_id)
    ctx = session.snapshot(include_fx_params=False)
    items, markers = _timeline_items_markers(ctx, start_s, end_s)
    text = tc.build(ctx, start_s, end_s, items=items, markers=markers)
    return {"ok": True, "context": text,
            "project": {"name": ctx.get("name"), "revision": ctx.get("revision")},
            "counts": {"items": len(items), "markers": len(markers)}}


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("REAPER_AUDIO_LOG_LEVEL", "WARNING"))
    server.run(transport="stdio")
