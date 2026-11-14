"""Plan-bound candidate renders with a fail-closed provenance contract.

A candidate render answers one question only: *what does the applied edit sound
like at this exact project revision and window, through the real master path?*
It is deliberately not a capture, not a stem collection and not a second
analysis pipeline.

Native REAPER rendering temporarily rewrites render settings, the time
selection and the dirty flag, then restores them.  This module verifies the
restoration report and compares the project digest and important state before
and after the render.  The monotonic change counter may advance; the returned
``revision_after_render`` is explicit and is the next valid revision.  The module
never reloads, saves or resets the project.  The product call path runs on the
project that owns the applied plan; the unattended live test harness is
restricted to an operator-verified disposable fixture, never the user's active
project.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Mapping
from math import ceil, isfinite
from pathlib import Path
from typing import Any

import soundfile as sf

from ..analysis.artifacts import AudioArtifactWorkspace
from ..context.revision import digest_evidence
from ..errors import (ArtifactCleanupError, BridgeUnavailable,
                      RenderBudgetExceeded, RenderCandidateMismatch,
                      RenderProvenanceIncomplete, RenderUnsupported,
                      StaleContext, ValidationError)
from ..models import Section

MAX_CANDIDATE_SECONDS = 120.0
MAX_CANDIDATE_BYTES = 64 * 1024 * 1024
STAGE = "master_output_post_track_bus_master_fx"
NODE = "master"
RENDER_PATH = "native_reaper_master_render_42230"
MAX_PROVENANCE_ROWS = 64


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False,
                      default=str).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()




def _revision_view(revision: Any) -> dict[str, Any]:
    if hasattr(revision, "to_dict"):
        value = revision.to_dict()
    elif isinstance(revision, Mapping):
        value = dict(revision)
    else:
        raise ValidationError(
            "revision_after must be a project revision",
            details={"field": "revision", "received_type": type(revision).__name__},
        )
    if not isinstance(value, Mapping):
        raise ValidationError(
            "revision_after must serialize to a mapping",
            details={"field": "revision", "received_type": type(value).__name__},
        )
    value = dict(value)
    for name in ("project_id", "change_count", "path"):
        if name not in value or value.get(name) is None or value.get(name) == "":
            raise ValidationError(
                "revision_after is missing a required identity field",
                details={"field": name, "received": "missing",
                         "received_type": type(value.get(name)).__name__},
            )
    if not isinstance(value["project_id"], str):
        raise ValidationError(
            "revision_after has a malformed project_id",
            details={"field": "project_id",
                     "received_type": type(value["project_id"]).__name__},
        )
    if (isinstance(value["change_count"], bool) or
            not isinstance(value["change_count"], int) or value["change_count"] < 0):
        raise ValidationError(
            "revision_after has a malformed change_count",
            details={"field": "change_count",
                     "received_type": type(value["change_count"]).__name__},
        )
    if not isinstance(value["path"], str):
        raise ValidationError(
            "revision_after has a malformed path",
            details={"field": "path", "received_type": type(value["path"]).__name__},
        )
    return value


def _plan_base_revision(plan: Any) -> dict[str, Any]:
    """Validate the base revision carried by the previewed plan."""
    raw = getattr(plan, "revision", None)
    if raw is None:
        raise ValidationError(
            "The plan has no base revision",
            details={"field": "plan.revision", "received": "missing",
                     "received_type": "NoneType",
                     "plan_id": getattr(plan, "plan_id", None),
                     "next_action": "Preview the edit plan again before rendering."},
        )
    try:
        return _revision_view(raw)
    except ValidationError as exc:
        raise ValidationError(
            "The plan base revision is malformed",
            details={"field": "plan.revision",
                     "received_type": type(raw).__name__,
                     "reason": str(exc),
                     "validation_details": exc.details,
                     "plan_id": getattr(plan, "plan_id", None),
                     "next_action": "Preview the edit plan again before rendering."},
        ) from exc



def _same_revision(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Compare the identity fields the bridge actually promises.

    ``instance_id``/``epoch`` are compared only when both sides carry them;
    older bridges omit them and a missing continuity component is not evidence
    of equality.  The caller separately records the identity gaps.
    """
    if (left.get("project_id") != right.get("project_id") or
            left.get("change_count") != right.get("change_count") or
            left.get("path") != right.get("path")):
        return False
    for name in ("instance_id", "epoch"):
        a, b = left.get(name), right.get(name)
        if a is not None and b is not None and a != b:
            return False
    return True


def _compact_fx(rows: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        result.append({
            "index": row.get("index"),
            "guid": row.get("guid"),
            "name": row.get("name"),
            "enabled": bool(row.get("enabled")),
            "offline": bool(row.get("offline")),
            "parameter_count": row.get("parameter_count"),
        })
    return result


def _compact_sends(rows: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        result.append({
            "index": row.get("index"),
            "dest_guid": row.get("dest_guid"),
            "mode": row.get("mode"),
            "mute": bool(row.get("mute")),
            "volume": row.get("volume"),
            "pan": row.get("pan"),
            "source_channels": row.get("source_channels"),
            "destination_channels": row.get("destination_channels"),
        })
    return result


def _digest_content(digest: Mapping[str, Any]) -> dict[str, Any]:
    """Return the persistent evidence view with the monotonic counter removed."""
    return {key: value for key, value in digest.items() if key != "change_count"}


def _digest_differences(before: Any, after: Any, *,
                       path: str = "", limit: int = 64) -> list[dict[str, Any]]:
    """Enumerate content-evidence differences without comparing the counter.

    ``change_count`` is the only documented monotonic field the native render
    transaction may advance.  It is excluded at the top level; every other
    digest field (including restored render settings and persistent project
    facts) must compare equal.
    """
    differences: list[dict[str, Any]] = []
    if path.count(".") > 12 or len(differences) >= limit:
        return differences
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        keys = sorted(set(before) | set(after), key=str)
        for key in keys:
            child = f"{path}.{key}" if path else str(key)
            if not path and key == "change_count":
                continue
            if key not in before:
                differences.append({"field": child, "kind": "added"})
            elif key not in after:
                differences.append({"field": child, "kind": "removed"})
            else:
                differences.extend(_digest_differences(before[key], after[key],
                                                       path=child, limit=limit - len(differences)))
            if len(differences) >= limit:
                break
        return differences[:limit]
    if isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            differences.append({"field": path or "$", "kind": "length",
                                "before": len(before), "after": len(after)})
            return differences
        for index, (left, right) in enumerate(zip(before, after)):
            differences.extend(_digest_differences(left, right,
                                                   path=f"{path}[{index}]",
                                                   limit=limit - len(differences)))
            if len(differences) >= limit:
                break
        return differences[:limit]
    if before != after:
        differences.append({"field": path or "$", "kind": "value"})
    return differences[:limit]


def _require_digest_provenance(digest: Mapping[str, Any], project_id: str,
                               revision: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(digest, Mapping):
        raise RenderProvenanceIncomplete("REAPER did not return a project digest")
    if digest.get("project_id") != project_id:
        raise RenderCandidateMismatch("The digest belongs to a different project",
                                      details={"digest_project_id": digest.get("project_id"),
                                               "project_id": project_id})
    if digest.get("change_count") != revision.get("change_count"):
        raise StaleContext("The project digest is not the applied revision",
                           details={"digest_change_count": digest.get("change_count"),
                                    "revision_change_count": revision.get("change_count")})
    coverage_raw = digest.get("coverage")
    coverage: Mapping[str, Any] = coverage_raw if isinstance(coverage_raw, Mapping) else {}
    missing = set(digest.get("missing") or [])
    required = ("fx", "sends", "master", "fx_state")
    bad = [name for name in required
           if coverage.get(name) is not True or name in missing]
    fields_raw = digest.get("fields")
    fields: Mapping[str, Any] = fields_raw if isinstance(fields_raw, Mapping) else {}
    if bad or not isinstance(fields.get("tracks"), list):
        raise RenderProvenanceIncomplete(
            "The project digest cannot prove FX and routing provenance",
            details={"required": list(required), "uncovered": bad,
                     "digest_missing": sorted(missing),
                     "digest_coverage": dict(coverage)},
        )
    master = fields.get("master")
    fx_state = fields.get("fx_state")
    if not isinstance(master, Mapping):
        raise RenderProvenanceIncomplete("The project digest has no master row")
    if not isinstance(fx_state, Mapping) or fx_state.get("complete") is not True:
        raise RenderProvenanceIncomplete(
            "The project digest has no complete serialized FX state",
            details={"fx_state": (dict(fx_state) if isinstance(fx_state, Mapping)
                                  else fx_state)},
        )
    try:
        evidence = digest_evidence(project_id, digest)
    except Exception as exc:  # noqa: BLE001 - malformed digest is a refusal
        raise RenderProvenanceIncomplete(
            "The project digest does not satisfy the digest contract",
            details={"reason": type(exc).__name__, "message": str(exc)},
        ) from exc
    return {"digest": digest, "fields": fields, "master": master,
            "fx_state": fx_state, "evidence": evidence}


def _provenance(provenance: Mapping[str, Any], *, settings: Mapping[str, Any],
                render_action: str, channel_view: str) -> dict[str, Any]:
    fields = provenance["fields"]
    master = provenance["master"]
    tracks = [row for row in fields.get("tracks") or [] if isinstance(row, Mapping)]
    master_guid = str(master.get("guid") or "")
    track_rows: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    fx_tracks: list[dict[str, Any]] = []
    fx_instances = 0
    bypassed = 0
    offline = 0
    for track in tracks:
        guid = str(track.get("guid") or "")
        sends = _compact_sends(track.get("sends"))
        for send in sends:
            edges.append({"from": guid, "to": send.get("dest_guid"),
                          "mode": send.get("mode"), "mute": send.get("mute"),
                          "volume": send.get("volume"), "pan": send.get("pan")})
        fx = _compact_fx(track.get("fx"))
        if fx:
            fx_tracks.append({"track_guid": guid, "fx": fx})
        fx_instances += len(fx)
        bypassed += sum(1 for row in fx if not row["enabled"])
        offline += sum(1 for row in fx if row["offline"])
        track_rows.append({
            "guid": guid,
            "main_send": bool(track.get("main_send")),
            "folder_depth": track.get("folder_depth"),
            "sends": sends,
        })
    master_fx = _compact_fx(master.get("fx"))
    master_sends = _compact_sends(master.get("sends"))
    for send in master_sends:
        edges.append({"from": master_guid, "to": send.get("dest_guid"),
                      "mode": send.get("mode"), "mute": send.get("mute"),
                      "volume": send.get("volume"), "pan": send.get("pan")})
    track_fx_instances = fx_instances
    fx_instances += len(master_fx)
    bypassed += sum(1 for row in master_fx if not row["enabled"])
    offline += sum(1 for row in master_fx if row["offline"])
    master_send_edges = sum(1 for edge in edges
                            if edge["to"] == master_guid or edge["from"] == master_guid)
    routing_payload = {
        "master_guid": master_guid,
        "tracks": track_rows,
        "master": {"guid": master_guid, "main_send": bool(master.get("main_send")),
                   "sends": master_sends},
    }
    fx_payload = {
        "tracks": fx_tracks,
        "master": master_fx,
        "fx_state": provenance["fx_state"],
    }
    routing_sha = _sha256(routing_payload)
    fx_sha = _sha256(fx_payload)
    return {
        "stage": STAGE,
        "node": NODE,
        "channel_view": channel_view,
        "signal_path": "tracks -> sends/folders -> master -> master FX -> output",
        "master_included": True,
        "track_fx_included": True,
        "master_fx_included": True,
        "routing_included": True,
        "render_path": RENDER_PATH,
        "render_action": render_action,
        "render_settings_used": dict(settings),
        "master_guid": master_guid,
        "master_fx": master_fx,
        "master_fx_count": len(master_fx),
        "track_fx_chain_count": len(fx_tracks),
        "fx_instance_count": fx_instances,
        "track_fx_instance_count": track_fx_instances,
        "bypassed_fx_count": bypassed,
        "offline_fx_count": offline,
        "track_fx": fx_tracks[:MAX_PROVENANCE_ROWS],
        "track_fx_omitted": max(0, len(fx_tracks) - MAX_PROVENANCE_ROWS),
        "routing_summary": {
            "track_count": len(tracks),
            "main_send_count": sum(1 for row in track_rows if row["main_send"]),
            "muted_from_master_count": sum(1 for row in track_rows
                                           if not row["main_send"]),
            "folder_track_count": sum(1 for row in track_rows
                                      if row.get("folder_depth") not in (None, 0)),
            "send_edge_count": len(edges),
            "master_send_edge_count": master_send_edges,
        },
        "routing_edges": edges[:MAX_PROVENANCE_ROWS],
        "routing_edges_omitted": max(0, len(edges) - MAX_PROVENANCE_ROWS),
        "fingerprints": {
            "routing_sha256": routing_sha,
            "fx_sha256": fx_sha,
            "project_digest_sha256": provenance["evidence"].fingerprint,
            "project_digest_scope": provenance["evidence"].scope,
            "project_digest_missing": list(provenance["evidence"].missing),
        },
    }


def _require_render_response(rendered: Mapping[str, Any], *,
                             revision: Mapping[str, Any],
                             channels: int, sample_rate: int | None,
                             tail_ms: int,
                             master_guid: str) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if not isinstance(rendered, Mapping):
        raise RenderUnsupported("The bridge returned no render response")
    if rendered.get("capture_point") != "native_render_master_only":
        raise RenderCandidateMismatch(
            "The bridge did not use the native master-only render path",
            details={"capture_point": rendered.get("capture_point"),
                     "required": "native_render_master_only"},
        )
    before = rendered.get("revision")
    after = rendered.get("revision_after")
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        raise RenderProvenanceIncomplete("The render has no revision binding")
    if (not before.get("instance_id") or not before.get("epoch") or
            not after.get("instance_id") or not after.get("epoch")):
        raise RenderProvenanceIncomplete(
            "The render did not carry the continuity identity required for a candidate",
            details={"render_before": dict(before), "render_after": dict(after)},
        )
    if not _same_revision(before, revision):
        raise StaleContext("The project changed before the candidate render",
                           details={"requested": dict(revision),
                                    "render_before": dict(before)})
    if (after.get("project_id") != revision.get("project_id") or
            after.get("path") != revision.get("path") or
            not isinstance(after.get("change_count"), int) or
            after["change_count"] < revision.get("change_count", 0)):
        raise RenderCandidateMismatch("The render returned an invalid successor revision",
                                      details={"requested": dict(revision),
                                               "render_after": dict(after)})
    settings = rendered.get("render_settings_used")
    if not isinstance(settings, Mapping):
        raise RenderProvenanceIncomplete("The render did not echo its settings")
    required_settings = ("RENDER_SETTINGS", "RENDER_BOUNDSFLAG", "RENDER_CHANNELS",
                         "RENDER_TAILMS", "RENDER_ADDTOPROJ")
    absent = [name for name in required_settings if name not in settings]
    if absent:
        raise RenderProvenanceIncomplete("The render settings echo is incomplete",
                                         details={"missing": absent})
    if int(settings["RENDER_SETTINGS"]) != 0:
        raise RenderCandidateMismatch("The render was not the master mix",
                                      details={"RENDER_SETTINGS": settings["RENDER_SETTINGS"]})
    if int(settings["RENDER_CHANNELS"]) != channels:
        raise RenderCandidateMismatch("The render used a different channel layout",
                                      details={"expected": channels,
                                               "actual": settings["RENDER_CHANNELS"]})
    if int(settings["RENDER_TAILMS"]) != tail_ms:
        raise RenderCandidateMismatch("The render used a different tail",
                                      details={"expected": tail_ms,
                                               "actual": settings["RENDER_TAILMS"]})
    if int(settings["RENDER_ADDTOPROJ"]) != 0:
        raise RenderCandidateMismatch("The render was added to the project")
    expected_bounds = 2  # the caller requires an explicit section
    if int(settings["RENDER_BOUNDSFLAG"]) != expected_bounds:
        raise RenderCandidateMismatch("The render did not use the requested time selection",
                                      details={"expected": 2,
                                               "actual": settings["RENDER_BOUNDSFLAG"]})
    restore = rendered.get("restore_report")
    if (not isinstance(restore, Mapping) or restore.get("restored") is not True or
            restore.get("failures")):
        raise RenderProvenanceIncomplete("REAPER did not restore render state",
                                         details={"restore_report": restore,
                                                  "revision_after": rendered.get("revision_after")})
    if not isinstance(rendered.get("render_action"), str) or not rendered["render_action"]:
        raise RenderProvenanceIncomplete("The render did not report its REAPER action")
    if (restore.get("dirty_before") is not None and
            restore.get("dirty_after") is not None and
            restore.get("dirty_before") != restore.get("dirty_after")):
        raise RenderProvenanceIncomplete("REAPER did not restore the dirty flag",
                                         details={"dirty_before": restore.get("dirty_before"),
                                                  "dirty_after": restore.get("dirty_after")})
    files = rendered.get("files")
    if not isinstance(files, list) or len(files) != 1:
        raise RenderProvenanceIncomplete("A master candidate must return exactly one file",
                                         details={"file_count": len(files or [])})
    file = files[0]
    if not isinstance(file, Mapping) or file.get("is_master") is not True:
        raise RenderCandidateMismatch("The returned file is not the master mix")
    if file.get("source_guid") != master_guid:
        raise RenderCandidateMismatch("The master file has the wrong source GUID",
                                      details={"expected": master_guid,
                                               "actual": file.get("source_guid")})
    if sample_rate is not None and int(rendered.get("sample_rate") or 0) != sample_rate:
        raise RenderCandidateMismatch("The render used a different sample rate",
                                      details={"expected": sample_rate,
                                               "actual": rendered.get("sample_rate")})
    return before, after


def _cleanup_after_failure(workspace: AudioArtifactWorkspace, error: Exception) -> None:
    try:
        # The bridge may have written a render before a later provenance check
        # failed; those files are not registered as assets yet, so a normal
        # close would preserve them by design.  This is a dedicated temporary
        # job, so reclaim its orphaned direct children as part of the refusal.
        workspace.close(reclaim_orphans=True)
    except Exception as cleanup_error:  # noqa: BLE001 - preserve both refusals
        raise ArtifactCleanupError(
            "Candidate render failed and its temporary files could not be cleaned",
            details={"original_error": type(error).__name__,
                     "original_message": str(error),
                     "cleanup_error": type(cleanup_error).__name__,
                     "cleanup_message": str(cleanup_error)},
        ) from cleanup_error


def render_candidate(session: Any, plan: Any, *, revision_after: Any,
                     section: Section, artifact_root: str | Path,
                     channels: str = "stereo", sample_rate: int | None = None,
                     tail_ms: int = 0,
                     max_duration_s: float = MAX_CANDIDATE_SECONDS,
                     max_bytes: int = MAX_CANDIDATE_BYTES) -> dict[str, Any]:
    """Render one applied plan's native master output on an exact interval."""
    from .session import ProjectRevision

    if not isinstance(section, Section):
        raise ValidationError("render_candidate requires an explicit Section")
    if isinstance(tail_ms, bool) or not isinstance(tail_ms, int) or tail_ms < 0:
        raise ValidationError("tail_ms must be a non-negative integer")
    if channels not in {"stereo", "mono"}:
        raise ValidationError("channels must be 'stereo' or 'mono'")
    if sample_rate is not None and (isinstance(sample_rate, bool) or
                                    not isinstance(sample_rate, int) or sample_rate <= 0):
        raise ValidationError("sample_rate must be a positive integer or None")
    if (isinstance(max_duration_s, bool) or
            not isinstance(max_duration_s, (int, float)) or
            not isfinite(float(max_duration_s)) or max_duration_s <= 0):
        raise ValidationError("max_duration_s must be a positive finite number")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValidationError("max_bytes must be a positive integer")
    if not getattr(plan, "plan_id", None):
        raise ValidationError("render_candidate requires a previewed EditPlan")
    revision = _revision_view(revision_after)
    identity_gaps = [name for name in ("instance_id", "epoch")
                     if not revision.get(name)]
    if identity_gaps:
        raise RenderProvenanceIncomplete(
            "The applied revision lacks the continuity identity required for a candidate",
            details={"missing": identity_gaps,
                     "next_action": "Refresh the project context with a bridge that reports instance_id and epoch."},
        )
    request = {
        "plan_id": str(plan.plan_id),
        "project_id": revision["project_id"],
        "revision_after": revision,
        "section": {"start_s": section.start_s, "end_s": section.end_s},
    }
    base = _plan_base_revision(plan)
    if (base["project_id"] != revision["project_id"] or
            base["path"] != revision["path"] or
            revision["change_count"] < base["change_count"]):
        raise RenderCandidateMismatch("The plan and applied revision do not match",
                                      details={"plan_revision": base,
                                               "revision_after": revision})
    duration = section.duration_s
    if duration > float(max_duration_s):
        raise RenderBudgetExceeded(
            "The requested candidate window is longer than the render budget",
            details={"duration_s": duration, "max_duration_s": max_duration_s,
                     "next_action": "Choose a shorter interval or raise the explicit budget."},
        )
    estimate_rate = sample_rate or 48_000
    estimated_bytes = ceil((duration + tail_ms / 1000.0) *
                           estimate_rate * (2 if channels == "stereo" else 1) * 4) + 44
    if estimated_bytes > max_bytes:
        raise RenderBudgetExceeded(
            "The requested candidate render would exceed the audio budget",
            details={"estimated_bytes": estimated_bytes, "max_bytes": max_bytes,
                     "duration_s": duration, "sample_rate": estimate_rate,
                     "next_action": "Choose a shorter interval or a lower sample rate."},
        )
    bridge = getattr(session, "bridge", None)
    render = getattr(bridge, "capture_render", None)
    if not callable(render):
        raise RenderUnsupported("The bridge has no capture_render implementation")
    snapshot = session.snapshot()
    current = ProjectRevision.from_dict((snapshot or {}).get("revision") or {})
    if not _same_revision(current.to_dict(), revision):
        raise StaleContext("The project changed after the applied plan",
                           details={"requested": revision,
                                    "current": current.to_dict()})
    try:
        digest_before = session.digest(revision["project_id"])
    except AttributeError as exc:
        raise RenderUnsupported(
            "The bridge has no project digest, so render provenance cannot be proved",
            details={"reason": str(exc)},
        ) from exc
    provenance = _require_digest_provenance(digest_before, revision["project_id"], revision)
    master_guid = str(provenance["master"].get("guid") or "")
    if not master_guid:
        raise RenderProvenanceIncomplete("The digest has no master GUID")

    job_id = f"rc-{uuid.uuid4().hex[:20]}"
    workspace = AudioArtifactWorkspace(artifact_root, job_id=job_id)
    started = time.perf_counter()
    try:
        try:
            rendered = render(
                expected_revision=revision,
                output_dir=str(workspace.directory),
                job_id=job_id,
                expected_track_count=0,
                timeout_s=600.0,
                section_start_s=section.start_s,
                section_end_s=section.end_s,
                master_only=True,
                render_settings=0,
                channels=channels,
                sample_rate=sample_rate,
                tail_ms=tail_ms,
            )
        except TypeError as exc:
            if "unexpected keyword argument" in str(exc):
                raise RenderUnsupported(
                    "The installed bridge does not support the master render contract",
                    details={"reason": str(exc)},
                ) from exc
            raise
        if not isinstance(rendered, Mapping):
            raise RenderUnsupported("The bridge returned an invalid render response")
        _, after = _require_render_response(
            rendered, revision=revision,
            channels=2 if channels == "stereo" else 1,
            sample_rate=sample_rate, tail_ms=tail_ms,
            master_guid=master_guid,
        )
        files = rendered.get("files") or []
        path = Path(str(files[0].get("path") or ""))
        if not path.is_file():
            raise RenderProvenanceIncomplete("The rendered master file is missing",
                                             details={"path": str(path)})
        info = sf.info(str(path))
        if info.frames <= 0 or info.samplerate <= 0 or info.channels <= 0:
            raise RenderProvenanceIncomplete("The rendered master file has no samples")
        reported_rate = rendered.get("sample_rate")
        if (isinstance(reported_rate, (int, float)) and
                not isinstance(reported_rate, bool) and int(reported_rate) > 0 and
                int(info.samplerate) != int(reported_rate)):
            raise RenderCandidateMismatch("The rendered file sample rate disagrees with the render echo",
                                          details={"echo": int(reported_rate),
                                                   "file": int(info.samplerate)})
        expected_duration = section.duration_s + tail_ms / 1000.0
        actual_duration = info.frames / info.samplerate
        tolerance = max(1.5 / info.samplerate, 0.001)
        if abs(actual_duration - expected_duration) > tolerance:
            raise RenderCandidateMismatch(
                "The rendered file duration does not match the requested interval",
                details={"expected_s": expected_duration, "actual_s": actual_duration,
                         "tolerance_s": tolerance},
            )
        expected_channels = 2 if channels == "stereo" else 1
        if info.channels != expected_channels:
            raise RenderCandidateMismatch("The rendered file has the wrong channel count",
                                          details={"expected": expected_channels,
                                                   "actual": info.channels})
        artifact = workspace.register_asset(path)
        if artifact.bytes > max_bytes:
            raise RenderBudgetExceeded(
                "The rendered candidate exceeds the audio budget",
                details={"bytes": artifact.bytes, "max_bytes": max_bytes},
            )
        digest_after = session.digest(revision["project_id"])
        after_evidence = _require_digest_provenance(digest_after, revision["project_id"], after)
        content_differences = _digest_differences(digest_before, digest_after)
        if content_differences:
            raise RenderCandidateMismatch(
                "The candidate render changed persistent project content or restored settings",
                details={"changed_fields": content_differences,
                         "excluded_fields": ["change_count"],
                         "before_fingerprint": _sha256(digest_before),
                         "after_fingerprint": _sha256(digest_after)},
            )
        snapshot_after = session.snapshot()
        current_after = ProjectRevision.from_dict(
            (snapshot_after or {}).get("revision") or {})
        if (current_after.project_id != after.get("project_id") or
                current_after.path != after.get("path") or
                current_after.change_count != after.get("change_count")):
            raise RenderCandidateMismatch(
                "The project advanced past the candidate render before it was read back",
                details={"render_after": dict(after),
                         "current": current_after.to_dict()},
            )
        elapsed = round(time.perf_counter() - started, 3)
        after_revision = ProjectRevision.from_dict(dict(after)).to_dict()
        counter_delta = int(after.get("change_count", 0)) - int(revision["change_count"])
        content_comparison = {
            "equal": True,
            "excluded_fields": ["change_count"],
            "changed_fields": [],
            "compared_digest_sha256_before": _sha256(_digest_content(digest_before)),
            "compared_digest_sha256_after": _sha256(_digest_content(digest_after)),
            "after_digest_scope": after_evidence["evidence"].scope,
            "note": ("Only the monotonic REAPER change counter is excluded. "
                     "Render settings and every other digest field were restored "
                     "and compared; any difference would have refused the WAV."),
        }
        provenance_block = _provenance(
            provenance, settings=rendered.get("render_settings_used") or {},
            render_action=str(rendered.get("render_action") or ""),
            channel_view=channels,
        )
        render_key = _sha256({
            "plan_id": str(plan.plan_id),
            "plan_sha256": _sha256(plan.to_dict() if hasattr(plan, "to_dict") else {}),
            "project_id": revision["project_id"],
            "revision_after": revision,
            "section": request["section"],
            "channels": channels,
            "sample_rate": sample_rate,
            "tail_ms": tail_ms,
            "render_settings": dict(rendered.get("render_settings_used") or {}),
            "render_action": rendered.get("render_action"),
            "routing_sha256": provenance_block["fingerprints"]["routing_sha256"],
            "fx_sha256": provenance_block["fingerprints"]["fx_sha256"],
            "project_digest_sha256": provenance_block["fingerprints"]["project_digest_sha256"],
        })
        result = {
            "plan_id": str(plan.plan_id),
            "project_id": revision["project_id"],
            "revision_before": base,
            "revision_after": revision,
            "revision_after_render": after_revision,
            "render_key": render_key,
            "render_s": elapsed,
            "section": request["section"],
            "duration_s": section.duration_s,
            "audio": {
                "path": str(path),
                "artifact_name": artifact.filename,
                "sha256": artifact.sha256,
                "bytes": artifact.bytes,
                "sample_rate": int(info.samplerate),
                "channels": int(info.channels),
                "frames": int(info.frames),
                "duration_s": actual_duration,
                "format": "WAV",
                "file_window": {"start_s": 0.0, "end_s": actual_duration},
                "project_window": {"start_s": section.start_s, "end_s": section.end_s},
                "measured_window": {"start_s": section.start_s, "end_s": section.end_s},
                "tail_s": tail_ms / 1000.0,
                "stage": STAGE,
                "node": NODE,
                "channel_view": channels,
                "source_kind": "rendered_master_mix",
                "audio_accessor_used": False,
            },
            "provenance": provenance_block,
            "project_state": {
                "render_mode": "native_reaper_master_render",
                "restoration_verified": True,
                "content_unchanged": True,
                "counter_changed_by_render": counter_delta != 0,
                "counter_delta_by_render": counter_delta,
                "revision_after_render_is_current": True,
                "digest_before_sha256": _sha256(digest_before.get("fields")),
                "digest_after_sha256": _sha256(digest_after.get("fields")),
                "content_comparison": content_comparison,
                "note": ("The native render may advance REAPER's monotonic change "
                         "counter even after restoring settings; the content "
                         "digest is compared before and after and is not hidden. "
                         "revision_after_render is the current revision."),
            },
            "artifact_job": {
                "job_id": job_id,
                "directory": str(workspace.directory),
            },
        }
        workspace.retain()
        return result
    except Exception as exc:
        _cleanup_after_failure(workspace, exc)
        raise
