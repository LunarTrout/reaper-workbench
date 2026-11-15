"""Durable storage for derived analysis reports.

An analysis is a *derived* document.  Its value depends on exactly one archived
capture (capture id, the complete revision identity, and the capture's
immutable fact root), one project section, a set of reference files with their
content hashes and windows, and the analysis options.  Every one of those inputs
is immutable, yet recomputing the document costs minutes to hours, so the report
and its input fingerprint are written next to the capture archive and re-read
after a process restart.

The stored record is metadata only: no stem audio is copied or retained here.
The capture archive already owns the hash-addressed WAVs, and a live analysis
keeps its rendered audio in the OS temp root under its own lease, which expires
or is released independently of this report.

Layout::

    <archive>/analyses/<analysis_id>/record.json

``record.json`` is replaced atomically (temp file, fsync, ``os.replace``) and is
never rewritten with different content for the same analysis id.  A small
pointer under the user profile only records *where* an analysis id lives; it is
advisory and can never substitute a report, because every load re-verifies the
schema version, the analysis id, the fingerprint digest and the identity fields
it claims, and the caller re-verifies the capture's fact root.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from ..errors import ValidationError

ANALYSIS_STORE_SCHEMA_VERSION = "reaper-workbench.analysis-record/1"
FINGERPRINT_VERSION = 1
ANALYSES_DIR_NAME = "analyses"
ANALYSIS_RECORD_FILE_NAME = "record.json"
ANALYSIS_INDEX_DIR_ENV = "REAPER_ANALYSIS_INDEX"

# ``available``   - the capture's immutable panorama document was resolved and
#                   its fact root is part of the fingerprint.
# ``unresolved``  - the capture is archived but its document could not be built.
# ``not_archived``- a live render that was never archived as a capture.
FACT_ROOT_STATES = frozenset({"available", "unresolved", "not_archived"})

OPTION_KEYS = (
    "include_pairwise",
    "phase_pairs",
    "include_partial_loudness",
    "parallel",
    "tail_ms",
    "channels",
    "sample_rate",
    "artifact_role",
    "detail",
)

_ANALYSIS_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def jsonable(value: Any) -> Any:
    """Normalise a report into JSON scalars without losing numbers."""
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, float) and (value != value or abs(value) == float("inf")):
        return None
    if type(value).__module__.startswith("numpy"):
        item = getattr(value, "item", None)
        if callable(item):
            return jsonable(item())
    return value


def canonical_json(value: Any) -> str:
    """One deterministic encoding, so a fingerprint digest is reproducible."""
    return json.dumps(jsonable(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def fingerprint_digest(fingerprint: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(fingerprint).encode("utf-8")).hexdigest()


def revision_identity(revision: Any) -> dict[str, Any] | None:
    """Return the identity fields a capture cannot change, or ``None``.

    A missing instance id or a non-positive epoch is *no proof of continuity*
    (the same rule ``ProjectRevision`` uses), so such a revision is not a
    complete identity and never enters a fingerprint.
    """
    if not isinstance(revision, Mapping):
        return None
    project_id = revision.get("project_id")
    path = revision.get("path")
    instance_id = revision.get("instance_id")
    epoch = revision.get("epoch")
    raw_count = revision.get("change_count")
    if not isinstance(project_id, str) or not project_id:
        return None
    if not isinstance(path, str) or not path:
        return None
    if not isinstance(instance_id, str) or not instance_id:
        return None
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        return None
    if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0:
        return None
    return {"project_id": project_id, "path": path, "instance_id": instance_id,
            "epoch": epoch, "change_count": raw_count}


def reference_file_fingerprint(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Hash one reference file so a different file can never reuse a report."""
    target = Path(path).expanduser()
    try:
        resolved = target.resolve(strict=True)
    except OSError as exc:
        raise ValidationError("Reference audio is no longer readable",
                              details={"path": str(target)}) from exc
    if not resolved.is_file():
        raise ValidationError("Reference audio is not a regular file",
                              details={"path": str(resolved)})
    digest = hashlib.sha256()
    size = 0
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"path": str(resolved), "sha256": digest.hexdigest(), "bytes": size}


def analysis_input_fingerprint(*, capture_id: str | None,
                               project_id: str,
                               revision: Mapping[str, Any],
                               revision_after: Mapping[str, Any] | None,
                               capture_fact_root: str | None,
                               capture_fact_root_state: str,
                               section: Mapping[str, Any] | None,
                               references: Sequence[Mapping[str, Any]],
                               options: Mapping[str, Any]) -> dict[str, Any]:
    """Build the complete, immutable input identity of one analysis."""
    identity = revision_identity(revision)
    if identity is None:
        raise ValidationError(
            "Analysis inputs need a complete project identity "
            "(project_id, path, instance_id, positive epoch, change_count)")
    if identity["project_id"] != project_id:
        raise ValidationError("Analysis revision belongs to another project",
                              details={"project_id": project_id,
                                       "revision_project_id": identity["project_id"]})
    after = None
    if revision_after is not None:
        after = revision_identity(revision_after)
        if after is None:
            raise ValidationError("The post-analysis revision identity is incomplete")
    fingerprint = {
        "fingerprint_version": FINGERPRINT_VERSION,
        "capture_id": capture_id,
        "project": identity,
        "revision_after": after,
        "capture_fact_root": capture_fact_root,
        "capture_fact_root_state": capture_fact_root_state,
        "section": (_section(section) if section is not None else None),
        "references": [dict(reference) for reference in references],
        "options": dict(options),
    }
    return _checked_fingerprint(fingerprint)


def _section(value: Mapping[str, Any]) -> dict[str, float]:
    start = value.get("start_s")
    end = value.get("end_s")
    if (isinstance(start, bool) or not isinstance(start, (int, float)) or
            isinstance(end, bool) or not isinstance(end, (int, float))):
        raise ValidationError("Analysis section needs numeric start_s and end_s")
    if float(end) < float(start):
        raise ValidationError("Analysis section ends before it starts")
    return {"start_s": float(start), "end_s": float(end)}


def _checked_analysis_id(analysis_id: Any) -> str:
    if not isinstance(analysis_id, str) or not _ANALYSIS_ID.fullmatch(analysis_id):
        raise ValidationError("analysis_id is not a valid identifier",
                              details={"analysis_id": analysis_id})
    return analysis_id


def _checked_fingerprint(value: Any) -> dict[str, Any]:
    """Reject any fingerprint that is not complete and internally consistent."""
    if not isinstance(value, Mapping):
        raise ValidationError("An analysis fingerprint is required")
    if value.get("fingerprint_version") != FINGERPRINT_VERSION:
        raise ValidationError("Unknown analysis fingerprint version",
                              details={"fingerprint_version": value.get("fingerprint_version")})
    state = value.get("capture_fact_root_state")
    if state not in FACT_ROOT_STATES:
        raise ValidationError("Unknown capture fact-root state",
                              details={"capture_fact_root_state": state})
    root = value.get("capture_fact_root")
    if state == "available":
        if not isinstance(root, str) or not _SHA256.fullmatch(root):
            raise ValidationError("An available capture fact root must be a sha256 digest",
                                  details={"capture_fact_root": root})
    elif root is not None:
        raise ValidationError("Only an available capture can carry a fact root",
                              details={"capture_fact_root_state": state,
                                       "capture_fact_root": root})
    capture_id = value.get("capture_id")
    if capture_id is not None and (not isinstance(capture_id, str) or not capture_id):
        raise ValidationError("capture_id must be a non-empty string or null")
    project = value.get("project")
    identity = revision_identity(project)
    if (not isinstance(project, Mapping) or identity is None or
            identity != dict(project)):
        raise ValidationError("The analysis fingerprint has an incomplete project identity")
    after = value.get("revision_after")
    if after is not None:
        if revision_identity(after) != dict(after):
            raise ValidationError("The analysis fingerprint has an incomplete post-analysis identity")
        for key in ("project_id", "path", "instance_id", "epoch"):
            if after.get(key) != identity.get(key):
                raise ValidationError("The post-analysis identity belongs to another project instance",
                                      details={"field": key})
        if after["change_count"] < identity["change_count"]:
            raise ValidationError("The post-analysis counter cannot move backwards")
    section = value.get("section")
    if section is not None:
        if not isinstance(section, Mapping):
            raise ValidationError("The analysis fingerprint section must be a mapping or null")
        _section(section)
    references = value.get("references")
    if not isinstance(references, (list, tuple)):
        raise ValidationError("The analysis fingerprint needs a reference list")
    for entry in references:
        if not isinstance(entry, Mapping):
            raise ValidationError("A reference fingerprint must be a mapping")
        if not isinstance(entry.get("reference_id"), str) or not entry["reference_id"]:
            raise ValidationError("A reference fingerprint needs a reference_id")
        if not isinstance(entry.get("path"), str) or not entry["path"]:
            raise ValidationError("A reference fingerprint needs its resolved path")
        if not isinstance(entry.get("sha256"), str) or not _SHA256.fullmatch(entry["sha256"]):
            raise ValidationError("A reference fingerprint needs a sha256 digest")
        size = entry.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValidationError("A reference fingerprint needs a byte count")
        window = entry.get("window")
        if window is not None:
            if not isinstance(window, Mapping):
                raise ValidationError("A reference window must be a mapping or null")
            _section(window)
    options = value.get("options")
    if not isinstance(options, Mapping) or set(options) != set(OPTION_KEYS):
        raise ValidationError("The analysis fingerprint needs every analysis option",
                              details={"required": list(OPTION_KEYS)})
    if not isinstance(options["include_pairwise"], bool):
        raise ValidationError("include_pairwise must be boolean")
    if not isinstance(options["include_partial_loudness"], bool):
        raise ValidationError("include_partial_loudness must be boolean")
    if not isinstance(options["parallel"], bool):
        raise ValidationError("parallel must be boolean")
    tail_ms = options["tail_ms"]
    if isinstance(tail_ms, bool) or not isinstance(tail_ms, int) or tail_ms < 0:
        raise ValidationError("tail_ms must be a non-negative integer")
    if options["channels"] not in {"stereo", "mono"}:
        raise ValidationError("channels must be stereo or mono")
    sample_rate = options["sample_rate"]
    if sample_rate is not None and (isinstance(sample_rate, bool) or
                                    not isinstance(sample_rate, int) or sample_rate <= 0):
        raise ValidationError("sample_rate must be a positive integer or null")
    if options["artifact_role"] not in {"temporary", "baseline", "final"}:
        raise ValidationError("artifact_role must be temporary, baseline, or final")
    if options["detail"] not in {"summary", "full"}:
        raise ValidationError("detail must be summary or full")
    phase_pairs = options["phase_pairs"]
    if phase_pairs is not None and phase_pairs != "all":
        if (not isinstance(phase_pairs, (list, tuple)) or
                any(not isinstance(pair, (list, tuple)) or len(pair) != 2 or
                    any(not isinstance(source, str) or not source for source in pair)
                    for pair in phase_pairs)):
            raise ValidationError("phase_pairs must be 'all' or a non-empty list of source pairs")
    return dict(value)


def analysis_store_root(archive_root: str | os.PathLike[str]) -> Path:
    return Path(archive_root).expanduser().absolute()


def analysis_record_path(archive_root: str | os.PathLike[str],
                         analysis_id: str) -> Path:
    return (analysis_store_root(archive_root) / ANALYSES_DIR_NAME /
            _checked_analysis_id(analysis_id) / ANALYSIS_RECORD_FILE_NAME)


def _link_free(path: Path) -> bool:
    return not (path.is_symlink() or getattr(path, "is_junction", lambda: False)())


def _archive_audio_files(record: Mapping[str, Any], root_real: Path) -> dict[str, str]:
    """Keep only hash-addressed archive references; drop temporary lease paths.

    The temp root holds the live-render lease and is deliberately not persisted:
    that lease expires or is released on its own schedule, and a report must not
    revive it.
    """
    files = record.get("audio_files")
    if not isinstance(files, Mapping):
        return {}
    kept: dict[str, str] = {}
    for guid, path_text in files.items():
        if not isinstance(guid, str) or not isinstance(path_text, str):
            continue
        try:
            relative = Path(path_text).absolute().relative_to(root_real)
        except ValueError:
            continue
        if relative.parts[:2] == ("audio", "sha256") and len(relative.parts) == 3:
            kept[guid] = path_text
    return kept


def persist_analysis_record(archive_root: str | os.PathLike[str],
                            record: Mapping[str, Any],
                            fingerprint: Mapping[str, Any]) -> Path:
    """Atomically write one derived report and its input fingerprint."""
    root = analysis_store_root(archive_root)
    if not root.is_dir() or not _link_free(root):
        raise ValidationError("The capture archive root must be a plain directory",
                              details={"archive_root": str(root)})
    root_real = root.resolve(strict=True)
    analysis_id = _checked_analysis_id(record.get("analysis_id"))
    checked = _checked_fingerprint(fingerprint)
    report = record.get("report")
    if not isinstance(report, Mapping):
        raise ValidationError("An analysis record needs a report mapping",
                              details={"analysis_id": analysis_id})
    revision = revision_identity(record.get("revision"))
    if revision is None or revision != dict(checked["project"]):
        raise ValidationError("The analysis record revision disagrees with its fingerprint",
                              details={"analysis_id": analysis_id})
    after = record.get("revision_after")
    after_identity = None if after is None else revision_identity(after)
    if checked.get("revision_after") != after_identity:
        raise ValidationError("The post-analysis revision disagrees with its fingerprint",
                              details={"analysis_id": analysis_id})
    if checked.get("capture_id") != record.get("capture_id"):
        raise ValidationError("The analysis capture id disagrees with its fingerprint",
                              details={"analysis_id": analysis_id})
    if checked["project"]["project_id"] != record.get("project_id"):
        raise ValidationError("The analysis project id disagrees with its fingerprint",
                              details={"analysis_id": analysis_id})
    payload = {
        "schema_version": ANALYSIS_STORE_SCHEMA_VERSION,
        "analysis_id": analysis_id,
        "project_id": record.get("project_id"),
        "revision": dict(record.get("revision") or {}),
        "revision_after": dict(after) if isinstance(after, Mapping) else None,
        "role": record.get("role"),
        "capture_id": record.get("capture_id"),
        "capture_archive_root": (str(record["archive_root"])
                                 if isinstance(record.get("archive_root"), (str, Path))
                                 else None),
        "archive_audio_hashes": dict(record.get("archive_audio_hashes") or {}),
        "archive_audio_files": _archive_audio_files(record, root_real),
        "report": jsonable(report),
        "input_fingerprint": checked,
        "input_fingerprint_sha256": fingerprint_digest(checked),
    }
    directory = root_real / ANALYSES_DIR_NAME / analysis_id
    if directory.exists() and not _link_free(directory):
        raise ValidationError("The analysis store entry cannot be a link",
                              details={"path": str(directory)})
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ANALYSIS_RECORD_FILE_NAME
    data = canonical_json(payload).encode("utf-8")
    temporary = directory / f".{ANALYSIS_RECORD_FILE_NAME}.{os.getpid()}.tmp"
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, target)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    remember_analysis_location(analysis_id, root_real,
                               payload["input_fingerprint_sha256"])
    return target


def load_analysis_record(archive_root: str | os.PathLike[str],
                         analysis_id: str) -> dict[str, Any] | None:
    """Read one persisted record, or ``None`` when this archive has no such id.

    A present but unreadable, foreign, downgraded or self-inconsistent record
    raises: it must never be silently replaced by a different report.
    """
    target = analysis_record_path(archive_root, analysis_id)
    if not target.exists():
        return None
    if not _link_free(target) or not target.is_file():
        raise ValidationError("The persisted analysis record is not a regular file",
                              details={"path": str(target)})
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValidationError("The persisted analysis record is unreadable",
                              details={"path": str(target)}) from exc
    if not isinstance(payload, Mapping):
        raise ValidationError("The persisted analysis record is not a mapping",
                              details={"path": str(target)})
    if payload.get("schema_version") != ANALYSIS_STORE_SCHEMA_VERSION:
        raise ValidationError("The persisted analysis record has an unknown schema version",
                              details={"schema_version": payload.get("schema_version"),
                                       "expected": ANALYSIS_STORE_SCHEMA_VERSION})
    if payload.get("analysis_id") != analysis_id:
        raise ValidationError("The persisted analysis record belongs to another analysis id",
                              details={"stored": payload.get("analysis_id"),
                                       "requested": analysis_id})
    checked = _checked_fingerprint(payload.get("input_fingerprint"))
    digest = payload.get("input_fingerprint_sha256")
    if digest != fingerprint_digest(checked):
        raise ValidationError("The persisted analysis fingerprint does not match its digest",
                              details={"analysis_id": analysis_id})
    if checked.get("capture_id") != payload.get("capture_id"):
        raise ValidationError("The persisted analysis capture id disagrees with its fingerprint",
                              details={"analysis_id": analysis_id})
    if checked["project"]["project_id"] != payload.get("project_id"):
        raise ValidationError("The persisted analysis project id disagrees with its fingerprint",
                              details={"analysis_id": analysis_id})
    identity = revision_identity(payload.get("revision"))
    if identity is None or identity != checked["project"]:
        raise ValidationError("The persisted analysis revision disagrees with its fingerprint",
                              details={"analysis_id": analysis_id})
    stored_after = payload.get("revision_after")
    stored_after_identity = None if stored_after is None else revision_identity(stored_after)
    if checked.get("revision_after") != stored_after_identity:
        raise ValidationError("The persisted post-analysis revision disagrees with its fingerprint",
                              details={"analysis_id": analysis_id})
    report = payload.get("report")
    if not isinstance(report, Mapping):
        raise ValidationError("The persisted analysis record has no report",
                              details={"analysis_id": analysis_id})
    audio_files = payload.get("archive_audio_files")
    hashes = payload.get("archive_audio_hashes")
    record: dict[str, Any] = {
        "analysis_id": analysis_id,
        "project_id": payload.get("project_id"),
        "revision": dict(payload.get("revision") or {}),
        "revision_after": (dict(stored_after) if isinstance(stored_after, Mapping) else None),
        "role": payload.get("role"),
        # A persisted report never revives a temporary audio lease.
        "artifact_job_id": None,
        "audio_files": dict(audio_files) if isinstance(audio_files, Mapping) else {},
        "archive_audio_hashes": dict(hashes) if isinstance(hashes, Mapping) else {},
        "capture_id": payload.get("capture_id"),
        "report": dict(report),
        "input_fingerprint": checked,
        "input_fingerprint_sha256": digest,
        "durable": True,
    }
    archive_root_text = payload.get("capture_archive_root")
    if isinstance(archive_root_text, str) and archive_root_text:
        record["archive_root"] = archive_root_text
    return record


def analysis_index_dir() -> Path:
    """Where analysis-id location pointers live (outside the archive)."""
    override = os.environ.get(ANALYSIS_INDEX_DIR_ENV)
    if isinstance(override, str) and override.strip():
        return Path(override).expanduser().absolute()
    return Path.home() / ".reaper-audio-library" / "analysis-index"


def remember_analysis_location(analysis_id: str, archive_root: Path,
                               fingerprint_sha256: str) -> bool:
    """Record where an analysis lives so a restarted server can find it.

    Best effort by contract: the pointer is advisory and carries no authority.
    Losing it only means the caller must name ``archive_root`` explicitly.
    """
    try:
        directory = analysis_index_dir()
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": ANALYSIS_STORE_SCHEMA_VERSION,
            "analysis_id": _checked_analysis_id(analysis_id),
            "archive_root": str(archive_root),
            "input_fingerprint_sha256": fingerprint_sha256,
        }
        target = directory / f"{analysis_id}.json"
        temporary = directory / f".{analysis_id}.{os.getpid()}.tmp"
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(canonical_json(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return True
    except OSError:
        return False


def analysis_locations(analysis_id: str) -> list[Path]:
    """Return the archive roots a pointer names for this analysis id."""
    try:
        target = analysis_index_dir() / f"{analysis_id}.json"
        if not target.is_file():
            return []
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError):
        return []
    if not isinstance(payload, Mapping) or payload.get("analysis_id") != analysis_id:
        return []
    root = payload.get("archive_root")
    if not isinstance(root, str) or not root:
        return []
    return [Path(root).expanduser().absolute()]


__all__ = [
    "ANALYSIS_STORE_SCHEMA_VERSION",
    "FINGERPRINT_VERSION",
    "ANALYSES_DIR_NAME",
    "ANALYSIS_RECORD_FILE_NAME",
    "FACT_ROOT_STATES",
    "OPTION_KEYS",
    "analysis_input_fingerprint",
    "analysis_locations",
    "analysis_record_path",
    "analysis_store_root",
    "canonical_json",
    "fingerprint_digest",
    "jsonable",
    "load_analysis_record",
    "persist_analysis_record",
    "reference_file_fingerprint",
    "remember_analysis_location",
    "revision_identity",
]
