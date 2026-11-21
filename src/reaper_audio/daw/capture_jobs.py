"""Durable asynchronous full-capture jobs.

The synchronous ``FullCaptureService.capture`` renders every source in one
bridge call.  On a large project that call holds the single-threaded resident
ReaScript for minutes, so its heartbeat stops and the client reports
``bridge_timeout``.  This module splits the same capture over three additive
bridge operations:

``project.capture_begin``
    Save render settings, selection and transport once and enumerate sources.
``project.capture_render_one``
    Render exactly one source and return; the bridge yields between calls.
``project.capture_end``
    Restore the saved project state exactly once and return every file.

The durable job document lives at ``<archive_root>/jobs/<job_id>.json`` and is
rewritten atomically after every source, so a crash leaves an accurate
progress record and a retry can see how far the job got.  The rendered files
are then handed to the *same* ``FullCaptureService`` used by the synchronous
path through :class:`~reaper_audio.daw.full_capture.PreRenderedCapture`; this
module never grows a second capture/analysis/archive implementation.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..analysis.artifacts import AudioArtifactWorkspace
from ..errors import (BridgeTimeout, BridgeUnavailable, PanoramaCapacityError,
                      ReaperAudioError, ValidationError)
from ..reaper.bridge_client import ReaperBridge
from .full_capture import FullCaptureService, PreRenderedCapture
from .session import ProjectRevision, ProjectSession

#: Schema marker of one durable job document.
JOB_SCHEMA = "reaper-workbench.capture-job/1"
JOB_STATES = ("queued", "running", "done", "failed", "cancelled")
TERMINAL_STATES = frozenset({"done", "failed", "cancelled"})

#: One render per source; a single source never needs the whole-capture budget
#: but a long section can still take minutes, so keep the historical 600 s bound.
BEGIN_TIMEOUT_S = 60.0
RENDER_ONE_TIMEOUT_S = 600.0
END_TIMEOUT_S = 120.0

JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


# --------------------------------------------------------------------------- #
# durable job document
# --------------------------------------------------------------------------- #

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jobs_root(archive_root: str | os.PathLike[str]) -> Path:
    return Path(archive_root).expanduser().absolute() / "jobs"


def _job_path(archive_root: str | os.PathLike[str], job_id: str) -> Path:
    if not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None:
        raise ValidationError("job_id contains unsupported characters",
                              details={"job_id": _safe(job_id)})
    return _jobs_root(archive_root) / f"{job_id}.json"


def _safe(value: Any) -> Any:
    """Describe an arbitrary value without leaking a non-JSON object."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _safe(item) for key, item in value.items()}
    return f"<{type(value).__name__}>"


def _job_lock(path: Path) -> threading.RLock:
    """One re-entrant lock per job document, shared by every updater."""
    key = str(path.resolve(strict=False))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _validate_document(document: Mapping[str, Any], *, job_id: str) -> None:
    """Refuse a document that violates the schema instead of trusting it."""
    if (not isinstance(document, Mapping) or
            document.get("schema") != JOB_SCHEMA or
            document.get("job_id") != job_id):
        raise ValidationError("capture job document does not match its schema",
                              details={"job_id": job_id})
    if document.get("state") not in JOB_STATES:
        raise ValidationError("capture job document has an invalid state",
                              details={"job_id": job_id,
                                       "state": _safe(document.get("state"))})
    progress = document.get("progress")
    if (not isinstance(progress, Mapping) or
            isinstance(progress.get("done"), bool) or
            not isinstance(progress.get("done"), int) or progress["done"] < 0 or
            isinstance(progress.get("total"), bool) or
            not isinstance(progress.get("total"), int) or progress["total"] < 0 or
            progress["done"] > progress["total"]):
        raise ValidationError("capture job document has invalid progress",
                              details={"job_id": job_id})
    sources = document.get("sources")
    if not isinstance(sources, list):
        raise ValidationError("capture job document has an invalid source list",
                              details={"job_id": job_id})
    rendered = document.get("rendered")
    if not isinstance(rendered, list):
        raise ValidationError("capture job document has an invalid rendered list",
                              details={"job_id": job_id})
    error = document.get("error")
    if error is not None and (
            not isinstance(error, Mapping) or
            not isinstance(error.get("code"), str) or not error["code"] or
            not isinstance(error.get("message"), str)):
        raise ValidationError("capture job document has an invalid error",
                              details={"job_id": job_id})
    if document.get("capture_id") is not None and not isinstance(
            document.get("capture_id"), str):
        raise ValidationError("capture job document has an invalid capture id",
                              details={"job_id": job_id})


def _atomic_write(path: Path, document: Mapping[str, Any]) -> None:
    """Write one job document atomically, with deterministic JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, sort_keys=True, ensure_ascii=False,
                         allow_nan=False, separators=(",", ":"), indent=2)
    temp = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _read_job(archive_root: str | os.PathLike[str], job_id: str) -> dict[str, Any]:
    path = _job_path(archive_root, job_id)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError("capture job not found",
                              details={"job_id": job_id}) from exc
    except (OSError, ValueError) as exc:
        raise ValidationError("capture job document is unreadable",
                              details={"job_id": job_id,
                                       "reason": str(exc)}) from exc
    _validate_document(document, job_id=job_id)
    return document


def _update_job(archive_root: str | os.PathLike[str], job_id: str,
                mutate: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """Read-modify-write one job document under its lock.

    Every writer goes through here, so a progress update can never drop a
    concurrent ``cancel_requested`` flag written by the same process.
    """
    path = _job_path(archive_root, job_id)
    with _job_lock(path):
        document = _read_job(archive_root, job_id)
        updated = mutate(copy.deepcopy(document))
        if not isinstance(updated, dict):
            raise ValidationError("capture job update did not return a document",
                                  details={"job_id": job_id})
        updated["updated_at"] = _utc_now()
        _validate_document(updated, job_id=job_id)
        _atomic_write(path, updated)
        return copy.deepcopy(updated)


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #

def start_capture_job(
    session: ProjectSession,
    archive_root: str | os.PathLike[str],
    *,
    section: tuple[float, float] | list[float] | None = None,
    channels: str = "stereo",
    sample_rate: int | None = None,
    tail_ms: int = 0,
    bridge_session_id: str | None = None,
) -> dict[str, Any]:
    """Create a durable capture job and start its worker thread.

    The request is validated before the job document exists, so a bad section
    or channel layout is an immediate ``validation_error`` rather than a failed
    job.  The worker renders one source per bridge call and then archives the
    record through the synchronous capture service.
    """
    if channels not in {"stereo", "mono"}:
        raise ValidationError("channels must be stereo or mono")
    if sample_rate is not None and (
            isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or
            sample_rate <= 0):
        raise ValidationError("sample_rate must be a positive integer or null")
    if isinstance(tail_ms, bool) or not isinstance(tail_ms, int) or tail_ms < 0:
        raise ValidationError("tail_ms must be a non-negative integer")
    if bridge_session_id is not None and (
            not isinstance(bridge_session_id, str) or not bridge_session_id.strip()):
        raise ValidationError("bridge_session_id must be a non-empty string or null")
    start_s = end_s = None
    if section is not None:
        if not isinstance(section, (tuple, list)) or len(section) != 2:
            raise ValidationError("section must be [start_s, end_s]")
        start_s = _finite(section[0], "section.start_s")
        end_s = _finite(section[1], "section.end_s")
        if end_s <= start_s:
            raise ValidationError("section.end_s must be greater than start_s")

    root = Path(archive_root).expanduser().absolute()
    job_id = f"capjob-{uuid.uuid4().hex}"
    now = _utc_now()
    document = {
        "schema": JOB_SCHEMA,
        "job_id": job_id,
        "state": "queued",
        "created_at": now,
        "updated_at": now,
        "phase": "queued",
        "request": {
            "section": None if start_s is None else [start_s, end_s],
            "channels": channels,
            "sample_rate": sample_rate,
            "tail_ms": tail_ms,
        },
        "bridge": {
            "project_id": None,
            "bridge_session_id": bridge_session_id,
            "output_dir": None,
            "job_handle": None,
        },
        "sources": [],
        "progress": {"done": 0, "total": 0, "current": None},
        "rendered": [],
        "capture_id": None,
        "state_fill": None,
        "error": None,
        "cancel_requested": False,
        "worker": {"pid": os.getpid(), "thread": f"capture-job-{job_id}"},
    }
    _validate_document(document, job_id=job_id)
    _atomic_write(_job_path(root, job_id), document)
    thread = threading.Thread(
        target=_run_job,
        args=(session, root, job_id, start_s, end_s, channels, sample_rate,
              tail_ms, bridge_session_id),
        name=f"capture-job-{job_id}",
        daemon=True,
    )
    thread.start()
    return {"job_id": job_id, "state": "queued"}


def capture_job_status(archive_root: str | os.PathLike[str], job_id: str) -> dict[str, Any]:
    """Return the current durable job document."""
    return _read_job(archive_root, job_id)


def capture_job_result(archive_root: str | os.PathLike[str],
                       job_id: str) -> dict[str, Any]:
    """Return the terminal-facing result view of one job."""
    document = _read_job(archive_root, job_id)
    return {
        "state": document["state"],
        "capture_id": document.get("capture_id"),
        "state_fill": document.get("state_fill"),
        "error": document.get("error"),
    }


def cancel_capture_job(archive_root: str | os.PathLike[str],
                       job_id: str) -> dict[str, Any]:
    """Request cancellation between sources and return the updated document.

    A queued job is cancelled immediately.  A running job is never interrupted
    inside one REAPER render; the worker checks this flag before every source,
    calls ``capture_end`` so the project state is restored, and only then marks
    the job ``cancelled``.
    """
    def mutate(document: dict[str, Any]) -> dict[str, Any]:
        if document["state"] in TERMINAL_STATES:
            return document
        document["cancel_requested"] = True
        if document["state"] == "queued":
            document["state"] = "cancelled"
            document["phase"] = "cancelled"
            document["progress"]["current"] = None
        return document

    return _update_job(archive_root, job_id, mutate)


# --------------------------------------------------------------------------- #
# worker
# --------------------------------------------------------------------------- #

def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValidationError(f"{field} must be finite")
    return number


def _worker_session(session: ProjectSession, bridge_session_id: str | None,
                    archive_root: Path) -> ProjectSession:
    """Use a dedicated bridge client when a session id was supplied.

    The caller may keep using its own session while the job runs; a separate
    client with the same ``bridge_session_id`` has its own mailbox state and
    cannot interleave with the worker's request files.
    """
    if bridge_session_id is None:
        return session
    bridge = ReaperBridge(bridge_session_id=bridge_session_id)
    return ProjectSession(bridge, project_id=getattr(session, "project_id", None),
                          archive_root=str(archive_root))


def _job_mutator(**fields: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def mutate(document: dict[str, Any]) -> dict[str, Any]:
        for key, value in fields.items():
            if key == "progress":
                document["progress"].update(value)
            else:
                document[key] = value
        return document
    return mutate


def _error_row(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, (BridgeTimeout, BridgeUnavailable)):
        code = "bridge_unavailable"
    elif isinstance(exc, ReaperAudioError):
        code = exc.code
    else:
        code = "capture_failed"
    message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    row: dict[str, Any] = {"code": code, "message": str(message)}
    details = getattr(exc, "details", None)
    if isinstance(details, Mapping) and details:
        row["details"] = _safe(details)
    return row


def _run_job(session: ProjectSession, archive_root: Path, job_id: str,
             start_s: float | None, end_s: float | None, channels: str,
             sample_rate: int | None, tail_ms: int,
             bridge_session_id: str | None) -> None:
    workspace: AudioArtifactWorkspace | None = None
    job_session: ProjectSession | None = None
    bridge_job_open = False
    finished = False
    try:
        # A job cancelled while queued must not adopt the bridge at all, and a
        # late-starting worker must never overwrite a terminal document.
        def start_running(document: dict[str, Any]) -> dict[str, Any]:
            if document["state"] in TERMINAL_STATES:
                return document
            document["state"] = "running"
            document["phase"] = "snapshot"
            return document

        started = _update_job(archive_root, job_id, start_running)
        if started["state"] in TERMINAL_STATES:
            finished = True
            return
        job_session = _worker_session(session, bridge_session_id, archive_root)
        missing_ops = [name for name in ("capture_begin", "capture_render_one",
                                         "capture_end")
                       if not callable(getattr(job_session.bridge, name, None))]
        if missing_ops:
            raise BridgeUnavailable(
                "The bridge client does not expose the asynchronous capture operations",
                details={"missing_operations": missing_ops})
        context = job_session.snapshot(include_items=False, include_fx_params=False)
        revision = ProjectRevision.from_dict(context.get("revision", {}))
        if not revision.project_id or not revision.path:
            raise BridgeUnavailable("REAPER returned an incomplete project identity")
        transport = context.get("transport")
        if (not isinstance(transport, Mapping) or
                transport.get("play_state") not in (0, 0.0)):
            raise ValidationError(
                "Stop playback before a full capture; capture never starts or stops it",
                details={"transport": dict(transport)
                         if isinstance(transport, Mapping) else _safe(transport)},
            )

        workspace = AudioArtifactWorkspace(archive_root / ".incoming", job_id=job_id)
        # Assume the bridge job exists before the request is sent: a lost
        # response (timeout after REAPER applied the settings) still deserves a
        # best-effort capture_end in the failure path.  An unknown job is a
        # cheap, swallowed error there.
        bridge_job_open = True
        try:
            begin = job_session.bridge.capture_begin(
                expected_revision=revision.to_dict(),
                output_dir=workspace.directory,
                job_id=job_id,
                timeout_s=BEGIN_TIMEOUT_S,
                section_start_s=start_s,
                section_end_s=end_s,
                channels=channels,
                sample_rate=sample_rate,
                tail_ms=tail_ms,
            )
        except ReaperAudioError as exc:
            # An older resident bridge answers the new operation with
            # "Unsupported operation"; that is a stale bridge, not a bad job.
            if "unsupported operation" in str(exc).lower():
                raise BridgeUnavailable(
                    "The resident bridge does not support asynchronous capture; "
                    "reload the current bridge script",
                    details={"reason": "bridge_build_too_old"},
                ) from exc
            raise
        sources = _job_sources(begin)
        _update_job(archive_root, job_id, _job_mutator(
            phase="rendering",
            sources=[{key: source[key] for key in ("index", "guid", "name", "is_master")}
                     for source in sources],
            progress={"done": 0, "total": len(sources), "current": None},
            bridge={
                "project_id": begin.get("project_id"),
                "bridge_session_id": bridge_session_id,
                "output_dir": str(workspace.directory),
                "job_handle": job_id,
                "revision_before": revision.to_dict(),
            },
        ))

        render_times: list[dict[str, Any]] = []
        render_settings: dict[int, dict[str, Any]] = {}
        cancelled = False
        for source in sources:
            if _cancel_requested(archive_root, job_id):
                cancelled = True
                break
            _update_job(archive_root, job_id, _job_mutator(
                phase="rendering",
                progress={"done": len(render_times), "total": len(sources),
                          "current": {"index": source["index"],
                                      "guid": source["guid"],
                                      "name": source["name"]}},
            ))
            started = time.perf_counter()
            rendered = job_session.bridge.capture_render_one(
                job=job_id,
                index=source["index"],
                output_dir=workspace.directory,
                timeout_s=RENDER_ONE_TIMEOUT_S,
            )
            elapsed = round(time.perf_counter() - started, 3)
            files = rendered.get("files")
            if not isinstance(files, list) or len(files) != 1:
                raise BridgeUnavailable(
                    "A capture source render did not return exactly one file",
                    details={"index": source["index"]})
            row = dict(files[0])
            settings_used = rendered.get("render_settings_used")
            if isinstance(settings_used, Mapping):
                render_settings[source["index"]] = dict(settings_used)
            render_times.append({
                "index": source["index"],
                "guid": source["guid"],
                "is_master": bool(source["is_master"]),
                "elapsed_s": elapsed,
            })
            # The job document is rewritten after every source: a crash here
            # leaves exactly the files it actually rendered on disk.
            def record_rendered(document: dict[str, Any]) -> dict[str, Any]:
                document["phase"] = "rendering"
                document["progress"].update({
                    "done": len(render_times), "total": len(sources),
                    "current": None,
                })
                document["rendered"].append({
                    "index": source["index"],
                    "guid": source["guid"],
                    "is_master": bool(source["is_master"]),
                    "path": row.get("path"),
                    "bytes": row.get("bytes"),
                    "elapsed_s": elapsed,
                })
                return document

            _update_job(archive_root, job_id, record_rendered)

        end = job_session.bridge.capture_end(job=job_id, timeout_s=END_TIMEOUT_S)
        bridge_job_open = False
        if cancelled or _cancel_requested(archive_root, job_id):
            _update_job(archive_root, job_id, _job_mutator(
                state="cancelled", phase="cancelled",
                progress={"current": None},
                state_fill=None,
            ))
            finished = True
            return

        pre_rendered = _pre_rendered(begin=begin, end=end, workspace=workspace,
                                     render_times=render_times,
                                     render_settings=render_settings,
                                     job_id=job_id)
        _update_job(archive_root, job_id, _job_mutator(phase="assembling"))
        service = FullCaptureService(job_session, archive_root)
        capture_section: tuple[float, float] | None = None
        if start_s is not None and end_s is not None:
            capture_section = (float(start_s), float(end_s))
        result = service.capture(
            section=capture_section,
            channels=channels,
            sample_rate=sample_rate,
            tail_ms=tail_ms,
            pre_rendered=pre_rendered,
        )
        _update_job(archive_root, job_id, _job_mutator(
            state="done",
            phase="done",
            capture_id=result.get("capture_id"),
            state_fill=result.get("state"),
            progress={"done": len(sources), "total": len(sources),
                      "current": None},
            error=None,
        ))
        finished = True
    except BaseException as exc:  # noqa: BLE001 - a job must record every failure
        if bridge_job_open and job_session is not None:
            # Best effort: the bridge died mid-job in the common failure case,
            # but when only the assembly failed the project still needs its
            # settings back.  A second failure is already reported by ``exc``.
            try:
                job_session.bridge.capture_end(job=job_id,
                                               timeout_s=END_TIMEOUT_S)
            except Exception:
                pass
        try:
            details = getattr(exc, "details", None)
            capture_id = (details.get("capture_id")
                          if isinstance(details, Mapping) else None)
            if isinstance(exc, PanoramaCapacityError):
                # The canonical capture and the project state are already
                # archived; only the one-shot view exceeded its budget.  The
                # job succeeded and the caller opens pages instead.
                _update_job(archive_root, job_id, _job_mutator(
                    state="done",
                    phase="done",
                    capture_id=capture_id if isinstance(capture_id, str) else None,
                    error=None,
                    progress={"done": len(sources), "total": len(sources),
                              "current": None},
                ))
            else:
                _update_job(archive_root, job_id, _job_mutator(
                    state="failed",
                    phase="failed",
                    error=_error_row(exc),
                    capture_id=capture_id if isinstance(capture_id, str) else None,
                    progress={"current": None},
                ))
        except Exception:
            # The job file itself is unwritable; nothing else can be reported.
            pass
        finished = True
    finally:
        if not finished:
            try:
                _update_job(archive_root, job_id, _job_mutator(
                    state="failed", phase="failed",
                    error={"code": "capture_failed",
                           "message": "capture worker stopped unexpectedly"},
                ))
            except Exception:
                pass
        if workspace is not None:
            try:
                # Reclaim any render this job wrote before it could register the
                # file (a bridge failure at source 2 leaves sources 0-1 on
                # disk).  Orphan reclaim is limited to this job's prefix.
                workspace.close(reclaim_orphans=True)
            except Exception:
                pass


def _cancel_requested(archive_root: Path, job_id: str) -> bool:
    try:
        return bool(_read_job(archive_root, job_id).get("cancel_requested"))
    except ValidationError:
        return False


def _job_sources(begin: Mapping[str, Any]) -> list[dict[str, Any]]:
    sources = begin.get("sources")
    if not isinstance(sources, list) or not sources:
        raise BridgeUnavailable("REAPER returned a capture job with no sources")
    rows: list[dict[str, Any]] = []
    for item in sources:
        if not isinstance(item, Mapping):
            raise BridgeUnavailable("REAPER returned an invalid capture source entry")
        index, guid, name = item.get("index"), item.get("guid"), item.get("name")
        is_master = item.get("is_master")
        if (isinstance(index, bool) or not isinstance(index, int) or
                not isinstance(guid, str) or not guid or
                not isinstance(name, str) or not isinstance(is_master, bool)):
            raise BridgeUnavailable("REAPER returned invalid capture source metadata",
                                    details={"source": _safe(item)})
        rows.append({"index": index, "guid": guid, "name": name,
                     "is_master": is_master})
    if sum(1 for row in rows if row["is_master"]) != 1:
        raise BridgeUnavailable(
            "REAPER returned a capture source list without exactly one master",
            details={"sources": len(rows)})
    return rows


def _pre_rendered(*, begin: Mapping[str, Any], end: Mapping[str, Any],
                  workspace: AudioArtifactWorkspace,
                  render_times: list[dict[str, Any]],
                  render_settings: Mapping[int, Mapping[str, Any]],
                  job_id: str) -> PreRenderedCapture:
    files = end.get("files")
    if not isinstance(files, list) or not files:
        raise BridgeUnavailable("REAPER ended the capture job without files",
                                details={"job": job_id})
    master_files = [dict(row) for row in files
                    if isinstance(row, Mapping) and row.get("is_master")]
    track_files = [dict(row) for row in files
                   if isinstance(row, Mapping) and not row.get("is_master")]
    if len(master_files) != 1 or len(track_files) != len(files) - 1:
        raise BridgeUnavailable(
            "REAPER returned an invalid capture job file set",
            details={"files": len(files), "masters": len(master_files)})
    settings = dict(begin.get("render_settings_used") or {})
    channels = begin.get("channels")
    sample_rate = begin.get("sample_rate")
    if (isinstance(channels, bool) or not isinstance(channels, int) or
            channels not in (1, 2) or isinstance(sample_rate, bool) or
            not isinstance(sample_rate, int) or sample_rate <= 0):
        raise BridgeUnavailable(
            "REAPER returned a capture job without a usable render format",
            details={"channels": _safe(channels), "sample_rate": _safe(sample_rate)})
    restore_report = dict(end.get("restore_report") or {})
    revision_after = end.get("revision_after") or begin.get("revision_after") or {}
    if not isinstance(revision_after, Mapping):
        raise BridgeUnavailable("REAPER ended the capture job without a revision")

    master_elapsed = sum(float(row.get("elapsed_s") or 0.0)
                         for row in render_times if row.get("is_master"))
    track_elapsed = sum(float(row.get("elapsed_s") or 0.0)
                        for row in render_times if not row.get("is_master"))

    def render_row(rows: list[dict[str, Any]], elapsed: float,
                   settings_for_source: Mapping[str, Any] | None = None
                   ) -> dict[str, Any]:
        return {
            "project_id": begin.get("project_id"),
            "files": rows,
            "revision": begin.get("revision"),
            "revision_after": dict(revision_after),
            "capture_point": "native_render_async_job_source",
            "render_settings_used": dict(settings_for_source or settings),
            "channels": channels,
            "sample_rate": sample_rate,
            "tail_s": begin.get("tail_s"),
            "target_count": len(rows),
            "_elapsed_s": round(elapsed, 3),
            "restore_report": restore_report,
            "render_action": "asynchronous capture job",
        }

    master_settings = render_settings.get(-1)
    first_track_settings: Mapping[str, Any] | None = None
    for row in track_files:
        track_index = row.get("track_index")
        if (isinstance(track_index, int) and not isinstance(track_index, bool) and
                track_index in render_settings):
            first_track_settings = render_settings[track_index]
            break
    return PreRenderedCapture(
        workspace=workspace,
        master_render=render_row(master_files, master_elapsed, master_settings),
        tracks_render=render_row(track_files, track_elapsed, first_track_settings),
        revision_after=dict(revision_after),
        restore_report=restore_report,
        render_times=tuple(dict(row) for row in render_times),
        bridge_job_id=job_id,
    )


__all__ = [
    "JOB_SCHEMA",
    "JOB_STATES",
    "TERMINAL_STATES",
    "cancel_capture_job",
    "capture_job_result",
    "capture_job_status",
    "start_capture_job",
]
