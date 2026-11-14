"""Full-capture snapshot contract and append-only archive.

A full capture is not a folder of WAVs.  It is one immutable record that links a
complete project map, every source/master audio asset, the analysis of each
asset, and the exact project revision/time window they came from.  Missing
coverage is an error, never an empty placeholder.

The archive is deliberately outside MCP's temporary audio lease: records are
append-only and old captures are never removed by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import ValidationError
from .ids import new_id
from .panorama import (DEFAULT_MAX_BYTES, PANORAMA_FILE_NAME,
                       build_panorama, write_panorama_atomic)


class _FullCaptureError(ValidationError):
    """Validation error that keeps the field/detail context used here."""

    def __init__(self, message: str, *, field: str | None = None,
                 details: dict | None = None) -> None:
        merged = dict(details or {})
        if field is not None:
            merged["field"] = field
        super().__init__(message, details=merged)

SCHEMA_VERSION = "reaper-workbench.full-capture/1"
ARCHIVE_SCHEMA_VERSION = "reaper-workbench.capture-archive/1"

#: Every group must be represented in ``project_map``.  Empty is allowed only
#: when the project really has no objects of that kind; ``coverage`` says which
#: groups were read.
REQUIRED_MAP_KEYS = frozenset(
    {
        "tracks",
        "master",
        "items",
        "takes",
        "midi",
        "fx",
        "routing",
        "envelopes",
        "automation",
        "tempo",
        "time_signatures",
        "markers",
        "regions",
        "selection",
        "transport",
        "coverage",
    }
)

REQUIRED_ASSET_KEYS = frozenset(
    {
        "asset_id",
        "source_guid",
        "name",
        "is_master",
        "stage",
        "capture_method",
        "sample_rate",
        "channels",
        "frames",
        "duration_s",
        "bytes",
        "sha256",
        "signal_window",
        "capture_revision",
        "status",
    }
)

_CAPTURE_ID = re.compile(r"^cap_[A-Za-z0-9._-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _FullCaptureError(f"{field} must be a non-empty string", field=field)
    return value


def _sha(value: Any, field: str) -> str:
    text = _text(value, field).lower()
    if not _SHA256.fullmatch(text):
        raise _FullCaptureError(f"{field} must be 64 lowercase hex", field=field)
    return text


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _FullCaptureError(f"{field} must be a mapping", field=field)
    return dict(value)


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise _FullCaptureError(f"{field} must be a sequence", field=field)
    return list(value)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _json_frozen(value: Any) -> Any:
    """Serialize a sealed record without thawing its analysis payload first."""
    if isinstance(value, MappingProxyType):
        return dict(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def canonical_payload(record: Mapping[str, Any]) -> bytes:
    """Canonical JSON bytes covered by the record integrity hash.

    Sealed records hold mapping proxies and tuples.  Supplying a converter here
    lets the hash and archive write traverse the frozen structure directly,
    rather than making a 460-MB thawed copy of the analysis data.
    """
    payload = {key: value for key, value in record.items() if key != "integrity_sha256"}
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_frozen,
    ).encode("utf-8")


@dataclass(slots=True)
class FullCaptureRecord:
    """One complete, linked capture.

    The integrity hash covers every field except ``integrity_sha256`` itself.
    Archive paths are part of the sealed payload, so a record copied to another
    archive cannot silently look like the original.
    """

    capture_id: str
    project_id: str
    project_path: str
    revision_before: dict[str, Any]
    revision_after: dict[str, Any] | None
    time_window: dict[str, Any]
    project_map: dict[str, Any]
    audio: list[dict[str, Any]]
    analysis: dict[str, Any]
    coverage: dict[str, bool]
    missing: list[str]
    created_at: str
    producer: str = "hermes"
    restore_report: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, Any] = field(default_factory=dict)
    relations: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION
    integrity_sha256: str | None = None
    _sealed: bool = field(default=False, init=False, repr=False)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise TypeError("FullCaptureRecord is sealed and immutable")
        object.__setattr__(self, name, value)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise _FullCaptureError(
                f"unsupported full-capture schema: {self.schema_version!r}",
                field="schema_version",
            )
        if not _CAPTURE_ID.fullmatch(_text(self.capture_id, "capture_id")):
            raise _FullCaptureError("capture_id must be cap_<id>", field="capture_id")
        _text(self.project_id, "project_id")
        _text(self.project_path, "project_path")
        self.revision_before = _mapping(self.revision_before, "revision_before")
        if self.revision_after is not None:
            self.revision_after = _mapping(self.revision_after, "revision_after")
        self.time_window = _mapping(self.time_window, "time_window")
        for key in ("project_start_s", "project_end_s"):
            value = self.time_window.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise _FullCaptureError(
                    f"time_window.{key} must be a number", field="time_window"
                )
        if float(self.time_window["project_end_s"]) <= float(self.time_window["project_start_s"]):
            raise _FullCaptureError(
                "time_window.project_end_s must be greater than project_start_s",
                field="time_window",
            )
        self.project_map = _mapping(self.project_map, "project_map")
        absent = REQUIRED_MAP_KEYS - set(self.project_map)
        if absent:
            raise _FullCaptureError(
                "project_map is missing required groups",
                field="project_map",
                details={"missing": sorted(absent)},
            )
        if not isinstance(self.project_map["tracks"], list) or not self.project_map["tracks"]:
            raise _FullCaptureError("project_map.tracks must be non-empty", field="project_map")
        if not isinstance(self.project_map["master"], Mapping):
            raise _FullCaptureError("project_map.master must be a mapping", field="project_map")
        self.coverage = _mapping(self.coverage, "coverage")
        self.restore_report = _mapping(self.restore_report, "restore_report")
        self.timings = _mapping(self.timings, "timings")
        self.relations = _mapping(self.relations, "relations")
        if self.relations:
            if self.relations.get("schema_version") != 1:
                raise _FullCaptureError(
                    "relations must use the supported relation schema",
                    field="relations.schema_version",
                )
            if not isinstance(self.relations.get("binding"), Mapping):
                raise _FullCaptureError(
                    "relations must carry a capture binding",
                    field="relations.binding",
                )
            binding = self.relations["binding"]
            if str(binding.get("capture_id")) != self.capture_id:
                raise _FullCaptureError(
                    "relations binding capture_id does not match the record",
                    field="relations.binding.capture_id",
                )
            if str(binding.get("project_id")) != self.project_id:
                raise _FullCaptureError(
                    "relations binding project_id does not match the record",
                    field="relations.binding.project_id",
                )
            window = binding.get("window")
            if isinstance(window, Mapping):
                start_s = window.get("start_s")
                end_s = window.get("end_s")
                record_start = self.time_window.get("project_start_s")
                record_end = self.time_window.get("project_end_s")
                numeric = all(
                    isinstance(value, (int, float)) and not isinstance(value, bool)
                    for value in (start_s, end_s, record_start, record_end)
                )
                if (not numeric
                        or start_s != record_start
                        or end_s != record_end):
                    raise _FullCaptureError(
                        "relations binding window does not match the record",
                        field="relations.binding.window",
                    )
        if set(self.coverage) != set(REQUIRED_MAP_KEYS) - {"coverage"} | {"audio", "analysis"}:
            # Keep this strict: a caller cannot omit a coverage dimension.
            raise _FullCaptureError(
                "coverage must name every project-map and audio dimension",
                field="coverage",
                details={"provided": sorted(self.coverage),
                         "expected": sorted((REQUIRED_MAP_KEYS - {"coverage"}) |
                                            {"audio", "analysis"})},
            )
        if any(not isinstance(value, bool) or not value for value in self.coverage.values()):
            raise _FullCaptureError(
                "a full capture requires every coverage dimension to be true",
                field="coverage",
            )
        if self.missing:
            raise _FullCaptureError(
                "a full capture cannot report missing groups", field="missing"
            )
        # A true coverage flag is not evidence.  Every required group must have
        # its actual payload present and of the right shape.
        for key in REQUIRED_MAP_KEYS - {"coverage"}:
            if key not in self.project_map or self.project_map[key] is None:
                raise _FullCaptureError(
                    "coverage=true has no matching project-map payload",
                    field="coverage",
                    details={"missing_payload": key},
                )
        if not isinstance(self.project_map["master"], Mapping):
            raise _FullCaptureError(
                "project_map.master must be a mapping", field="project_map"
            )
        if not isinstance(self.project_map["tempo"], Mapping):
            raise _FullCaptureError(
                "project_map.tempo must be a mapping", field="project_map"
            )
        if not isinstance(self.project_map["selection"], Mapping) or not isinstance(
                self.project_map["transport"], Mapping):
            raise _FullCaptureError(
                "selection and transport payloads must be mappings", field="project_map"
            )

        audio = _list(self.audio, "audio")
        if not audio:
            raise _FullCaptureError("audio must not be empty", field="audio")
        seen_guids: set[str] = set()
        masters = 0
        for index, item in enumerate(audio):
            asset = _mapping(item, f"audio[{index}]")
            absent = REQUIRED_ASSET_KEYS - set(asset)
            if absent:
                raise _FullCaptureError(
                    f"audio[{index}] is missing fields",
                    field="audio",
                    details={"missing": sorted(absent)},
                )
            guid = _text(asset["source_guid"], f"audio[{index}].source_guid")
            if guid in seen_guids:
                raise _FullCaptureError(
                    "audio source_guid values must be unique", field="audio",
                    details={"source_guid": guid},
                )
            seen_guids.add(guid)
            _sha(asset["sha256"], f"audio[{index}].sha256")
            if not isinstance(asset.get("capture_revision"), Mapping):
                raise _FullCaptureError(
                    "every asset needs its capture revision",
                    field="audio",
                    details={"source_guid": guid},
                )
            if asset["capture_revision"] != self.revision_before:
                raise _FullCaptureError(
                    "all assets must share the record revision",
                    field="audio",
                    details={"source_guid": guid},
                )
            window = asset.get("signal_window")
            if not isinstance(window, Mapping):
                raise _FullCaptureError(
                    "every asset needs a signal window",
                    field="audio",
                    details={"source_guid": guid},
                )
            for win_key in ("project_start_s", "project_end_s"):
                if (not isinstance(window.get(win_key), (int, float)) or
                        abs(float(window[win_key]) - float(self.time_window[win_key])) > 1e-9):
                    raise _FullCaptureError(
                        "all assets must share the capture time window",
                        field="audio",
                        details={"source_guid": guid, "window": win_key},
                    )
            if asset.get("status") not in {"leased", "available", "expired", "stored"}:
                raise _FullCaptureError(
                    "audio status must be leased, available, expired or stored",
                    field="audio",
                    details={"source_guid": guid},
                )
            if asset["is_master"] is True:
                masters += 1
            elif asset["is_master"] is not False:
                raise _FullCaptureError(
                    "audio is_master must be boolean", field="audio"
                )
        if masters != 1:
            raise _FullCaptureError(
                "a full capture needs exactly one master asset", field="audio",
                details={"masters": masters},
            )
        track_guids = {
            track.get("guid") for track in self.project_map["tracks"]
            if isinstance(track, Mapping) and isinstance(track.get("guid"), str)
        }
        if not track_guids:
            raise _FullCaptureError(
                "project_map.tracks has no stable GUIDs", field="project_map"
            )
        master_guid = self.project_map["master"].get("guid")
        if not isinstance(master_guid, str) or not master_guid:
            raise _FullCaptureError(
                "project_map.master has no stable GUID", field="project_map"
            )
        expected_guids = track_guids | {master_guid}
        if seen_guids != expected_guids:
            raise _FullCaptureError(
                "audio GUIDs must match every project track plus exactly one master",
                field="audio",
                details={"missing_guids": sorted(expected_guids - seen_guids),
                         "extra_guids": sorted(seen_guids - expected_guids)},
            )
        self.audio = audio
        self.analysis = _mapping(self.analysis, "analysis")
        absent_analysis = seen_guids - set(self.analysis)
        if absent_analysis:
            raise _FullCaptureError(
                "every captured asset needs an analysis record",
                field="analysis",
                details={"missing_guids": sorted(absent_analysis)},
            )
        self.missing = list(self.missing)
        _text(self.created_at, "created_at")
        _text(self.producer, "producer")
        object.__setattr__(self, "revision_before", _freeze(self.revision_before))
        object.__setattr__(self, "revision_after", _freeze(self.revision_after))
        object.__setattr__(self, "time_window", _freeze(self.time_window))
        object.__setattr__(self, "project_map", _freeze(self.project_map))
        object.__setattr__(self, "audio", _freeze(tuple(self.audio)))
        object.__setattr__(self, "analysis", _freeze(self.analysis))
        object.__setattr__(self, "coverage", _freeze(self.coverage))
        object.__setattr__(self, "restore_report", _freeze(self.restore_report))
        object.__setattr__(self, "timings", _freeze(self.timings))
        object.__setattr__(self, "relations", _freeze(self.relations))
        object.__setattr__(self, "missing", tuple(self.missing))
        if self.integrity_sha256 is None:
            self.integrity_sha256 = hashlib.sha256(
                canonical_payload(self.to_mapping())).hexdigest()
        else:
            _sha(self.integrity_sha256, "integrity_sha256")
            expected = hashlib.sha256(
                canonical_payload(self.to_mapping())).hexdigest()
            if expected != self.integrity_sha256:
                raise _FullCaptureError(
                    "full-capture integrity hash does not match its payload",
                    field="integrity_sha256",
                )
        object.__setattr__(self, "_sealed", True)

    def to_mapping(self, *, deep: bool = False) -> dict[str, Any]:
        """Return the record fields without thawing huge analysis data.

        The sealed record is already JSON-compatible through
        :func:`canonical_payload`; callers that only need a mapping for a hash,
        archive append, or panorama build should not pay for a 460-MB deep copy.
        ``audio`` entries are copied because the archive legitimately annotates
        them with durable paths; all other groups stay immutable and shared.
        Set ``deep=True`` only for an explicitly editable public dictionary.
        """
        thaw = _thaw if deep else (lambda value: value)
        result = {
            "schema_version": self.schema_version,
            "capture_id": self.capture_id,
            "project_id": self.project_id,
            "project_path": self.project_path,
            "revision_before": _thaw(self.revision_before),
            "revision_after": _thaw(self.revision_after),
            "time_window": _thaw(self.time_window),
            # The project map is small relative to analysis; thaw it so the
            # existing list-based helpers and archive index see ordinary JSON
            # data.  The 460-MB analysis payload remains immutable.
            "project_map": _thaw(self.project_map),
            "audio": ([dict(item) for item in self.audio] if not deep
                      else _thaw(self.audio)),
            "analysis": thaw(self.analysis),
            "coverage": _thaw(self.coverage),
            "missing": list(self.missing),
            "created_at": self.created_at,
            "producer": self.producer,
            "restore_report": _thaw(self.restore_report),
            "timings": _thaw(self.timings),
            "integrity_sha256": self.integrity_sha256,
        }
        # A legacy record without relations must keep its original canonical
        # bytes, so the key is omitted rather than serialized as an empty map.
        if self.relations:
            result["relations"] = thaw(self.relations)
        return result

    def to_dict(self) -> dict[str, Any]:
        """Explicit deep, editable dictionary (legacy/public API)."""
        return self.to_mapping(deep=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FullCaptureRecord":
        if not isinstance(data, Mapping):
            raise _FullCaptureError("full capture must be a mapping", field="capture")
        return cls(**dict(data))

    def seal(self) -> "SealedFullCapture":
        return _seal_mapping(self.to_mapping())


@dataclass(frozen=True, slots=True)
class SealedFullCapture:
    """Immutable payload handed to the archive.

    The mutable builder (``FullCaptureRecord``) is validated once and then
    serialized.  The archive never receives the builder's lists/dicts, so no
    caller can change a captured record after it has been appended.
    """

    capture_id: str
    payload_json: str
    integrity_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self.payload_json)


def _seal_mapping(data: Mapping[str, Any]) -> SealedFullCapture:
    """Seal an already-validated mapping without a second deep freeze/thaw.

    ``CaptureArchive.append`` starts from a validated ``FullCaptureRecord`` and
    only adds archive paths/status to audio entries.  Rebuilding a second
    immutable record there costs several full deep traversals of the analysis
    payload.  This helper preserves the exact canonical bytes and integrity
    hash while avoiding that redundant validation pass; ``CaptureArchive.read``
    still performs the full integrity check when the record is loaded.
    """
    clean = dict(data)
    clean["integrity_sha256"] = None
    integrity = hashlib.sha256(canonical_payload(clean)).hexdigest()
    clean["integrity_sha256"] = integrity
    payload = json.dumps(clean, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False,
                         default=_json_frozen)
    return SealedFullCapture(capture_id=str(clean.get("capture_id")),
                             payload_json=payload,
                             integrity_sha256=integrity)


@dataclass(frozen=True, slots=True)
class ArchivedCapture:
    capture_id: str
    record_path: str
    audio_directory: str
    integrity_sha256: str


class CaptureArchive:
    """Append-only, content-addressed full-capture archive.

    Layout::

        <root>/index.jsonl
        <root>/retention.jsonl
        <root>/captures/<capture_id>/record.json
        <root>/audio/sha256/<sha256>.wav

    ``append`` copies every audio asset into the content-addressed store,
    verifies its SHA-256 before and after the copy, and rewrites the sealed
    record so each audio entry points at its durable ``archive_audio`` path.
    Identical assets (same SHA-256) are stored once and referenced by every
    matching GUID. Successful captures are never expired; only an unsuccessful
    call may leave temporary workspace files, which the caller removes.
    """

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).expanduser().absolute()
        if self.root.exists() and (self.root.is_symlink() or
                                   getattr(self.root, "is_junction", lambda: False)()):
            raise _FullCaptureError("Capture archive root cannot be a link")
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise _FullCaptureError("Capture archive root must be a directory")
        self.root_real = self.root.resolve(strict=True)
        self.captures = self.root_real / "captures"
        self.captures.mkdir(exist_ok=True)
        self.index_path = self.root_real / "index.jsonl"
        if not self.index_path.exists():
            self.index_path.write_text("", encoding="utf-8")

    def append(self, record: FullCaptureRecord | SealedFullCapture,
               audio_files: Mapping[str, str | os.PathLike[str]],
               *, extra_files: Mapping[str, str | os.PathLike[str]] | None = None) -> ArchivedCapture:
        """Seal one record and retain every audio asset durably.

        ``audio_files`` is the required one-file-per-source map.
        ``extra_files`` is an optional content-addressed evidence map keyed by
        SHA-256 (for example failed master-render attempts).  Extra files are
        copied and hash-verified but never promoted to ``record.audio``.
        """
        if isinstance(record, FullCaptureRecord):
            data = record.to_mapping()
        elif isinstance(record, SealedFullCapture):
            data = record.to_dict()
        else:
            raise _FullCaptureError("record must be a FullCaptureRecord")
        capture_id = _text(data.get("capture_id"), "capture_id")
        destination = self.captures / capture_id
        if destination.exists():
            raise _FullCaptureError(
                "Capture id already exists in the archive",
                details={"capture_id": capture_id, "path": str(destination)},
            )
        audio_items = data.get("audio")
        if not isinstance(audio_items, list):
            raise _FullCaptureError("sealed record has no audio list")
        guids = {str(item["source_guid"]) for item in audio_items}
        if set(audio_files) != guids:
            raise _FullCaptureError(
                "audio_files must contain exactly one file per captured source",
                details={"missing": sorted(guids - set(audio_files)),
                         "extra": sorted(set(audio_files) - guids)},
            )
        content_root = self.root_real / "audio" / "sha256"
        content_root.mkdir(parents=True, exist_ok=True)
        audio_status: dict[str, dict[str, Any]] = {}
        created_blobs: list[Path] = []
        try:
            for item in audio_items:
                guid = str(item["source_guid"])
                source = Path(audio_files[guid]).expanduser()
                if source.is_symlink() or getattr(source, "is_junction", lambda: False)():
                    raise _FullCaptureError("Capture audio cannot be a link",
                                            details={"path": str(source)})
                if not source.is_file():
                    raise _FullCaptureError("Capture audio is missing",
                                            details={"path": str(source)})
                expected = str(item["sha256"]).lower()
                actual = _sha256_file(source)
                if actual != expected:
                    raise _FullCaptureError(
                        "Capture audio hash differs from the record",
                        details={"source_guid": guid, "expected": expected, "actual": actual},
                    )
                relative = f"audio/sha256/{expected}.wav"
                target = self.root_real / relative
                if target.exists():
                    if _sha256_file(target) != expected:
                        raise _FullCaptureError(
                            "Content-addressed audio has a conflicting hash",
                            details={"source_guid": guid, "path": str(target)},
                        )
                else:
                    temporary = content_root / f".{expected}.{os.getpid()}.{id(item)}.tmp"
                    shutil.copyfile(source, temporary)
                    if _sha256_file(temporary) != expected:
                        temporary.unlink(missing_ok=True)
                        raise _FullCaptureError(
                            "Archived audio copy failed its hash check",
                            details={"source_guid": guid, "expected": expected},
                        )
                    os.replace(temporary, target)
                    created_blobs.append(target)
                item["archive_audio"] = relative
                item["status"] = "stored"
                audio_status[guid] = {
                    "sha256": expected,
                    "status": "stored",
                    "archive_audio": relative,
                }
            if extra_files:
                for raw_sha, raw_source in extra_files.items():
                    expected = _sha(str(raw_sha), "extra_files.sha256")
                    source = Path(raw_source).expanduser()
                    if source.is_symlink() or getattr(source, "is_junction", lambda: False)():
                        raise _FullCaptureError("Extra capture evidence cannot be a link",
                                                details={"path": str(source)})
                    if not source.is_file():
                        raise _FullCaptureError("Extra capture evidence is missing",
                                                details={"path": str(source)})
                    if _sha256_file(source) != expected:
                        raise _FullCaptureError(
                            "Extra capture evidence hash differs from its key",
                            details={"path": str(source), "expected": expected})
                    target = self.root_real / f"audio/sha256/{expected}.wav"
                    if target.exists():
                        if _sha256_file(target) != expected:
                            raise _FullCaptureError(
                                "Content-addressed evidence has a conflicting hash",
                                details={"path": str(target)})
                    else:
                        temporary = content_root / f".{expected}.{os.getpid()}.extra.tmp"
                        shutil.copyfile(source, temporary)
                        if _sha256_file(temporary) != expected:
                            temporary.unlink(missing_ok=True)
                            raise _FullCaptureError(
                                "Archived evidence copy failed its hash check",
                                details={"path": str(source)})
                        os.replace(temporary, target)
                        created_blobs.append(target)
            data["integrity_sha256"] = None
            sealed = _seal_mapping(data)
            data["integrity_sha256"] = sealed.integrity_sha256
            staging_root = self.root_real / ".staging"
            staging_root.mkdir(exist_ok=True)
            staging = staging_root / f"{capture_id}-{os.getpid()}-{id(sealed)}"
            if staging.exists():
                raise _FullCaptureError("Archive staging path already exists")
            staging.mkdir()
            try:
                staged_record = staging / "record.json"
                with staged_record.open("w", encoding="utf-8") as handle:
                    handle.write(sealed.payload_json)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(staging, destination)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        except Exception:
            for blob in created_blobs:
                blob.unlink(missing_ok=True)
            raise
        record_path = destination / "record.json"
        index_entry = {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "capture_id": capture_id,
            "project_id": data.get("project_id"),
            "project_path": data.get("project_path"),
            "revision_before": data.get("revision_before"),
            "time_window": data.get("time_window"),
            "record": str(record_path.relative_to(self.root_real)).replace("\\", "/"),
            "integrity_sha256": sealed.integrity_sha256,
            "audio": audio_status,
            "retention": "durable content-addressed audio; no expiry",
        }
        try:
            self._append_index_atomic(index_entry)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            for blob in created_blobs:
                blob.unlink(missing_ok=True)
            raise
        # The canonical record is committed and is never rewritten.  Build the
        # bounded LLM panorama next to it; it is a derived sidecar, not part of
        # the record integrity hash.
        # Build from the in-memory mapping that already contains the archive
        # paths.  Do not parse the just-written 440-MB record back from disk.
        panorama = build_panorama(
            data,
            record_path=str(record_path.relative_to(self.root_real)).replace("\\", "/"),
            max_bytes=DEFAULT_MAX_BYTES,
        )
        write_panorama_atomic(destination / PANORAMA_FILE_NAME, panorama)
        return ArchivedCapture(
            capture_id=capture_id,
            record_path=str(record_path),
            audio_directory=str(content_root),
            integrity_sha256=sealed.integrity_sha256,
        )

    def _append_index_atomic(self, entry: Mapping[str, Any]) -> None:
        line = json.dumps(entry, sort_keys=True, ensure_ascii=False,
                          allow_nan=False) + "\n"
        old = self.index_path.read_text(encoding="utf-8")
        temp = self.root_real / "index.jsonl.tmp"
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(old)
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self.index_path)

    def recover(self) -> list[str]:
        """Repair missing index rows from committed capture directories."""
        indexed = {row.get("capture_id") for row in self.list_index()}
        repaired: list[str] = []
        for capture_dir in sorted(self.captures.iterdir()):
            if not capture_dir.is_dir() or capture_dir.name.startswith("."):
                continue
            record_path = capture_dir / "record.json"
            if not record_path.is_file() or capture_dir.name in indexed:
                continue
            data = json.loads(record_path.read_text(encoding="utf-8"))
            audio = data.get("audio") if isinstance(data.get("audio"), list) else []
            entry = {
                "schema_version": ARCHIVE_SCHEMA_VERSION,
                "capture_id": capture_dir.name,
                "project_id": data.get("project_id"),
                "project_path": data.get("project_path"),
                "revision_before": data.get("revision_before"),
                "time_window": data.get("time_window"),
                "record": str(record_path.relative_to(self.root_real)).replace("\\", "/"),
                "integrity_sha256": data.get("integrity_sha256"),
                "audio": {
                    str(item.get("source_guid")): {
                        "sha256": item.get("sha256"),
                        "status": item.get("status", "expired"),
                        "lease_id": item.get("lease_id"),
                    }
                    for item in audio if isinstance(item, Mapping)
                },
                "retention": "recovered from committed record",
            }
            self._append_index_atomic(entry)
            repaired.append(capture_dir.name)
        return repaired

    def expire(self, capture_id: str, *, reason: str = "lease_elapsed") -> None:
        """Append an expiry event for a legacy lease-only capture.

        Durable captures with stored audio are never expired.  The sealed
        record itself is never rewritten.
        """
        capture_id = _text(capture_id, "capture_id")
        rows = self.list_index()
        row = next((entry for entry in rows if entry.get("capture_id") == capture_id), None)
        if row is None:
            raise _FullCaptureError("Capture is not in the archive",
                                    details={"capture_id": capture_id})
        audio = row.get("audio")
        if isinstance(audio, Mapping) and any(
                isinstance(item, Mapping) and item.get("status") == "stored"
                for item in audio.values()):
            raise _FullCaptureError("Durable capture audio must not be expired",
                                    details={"capture_id": capture_id})
        event = {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "capture_id": capture_id,
            "status": "expired",
            "reason": _text(reason, "reason"),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        with (self.root_real / "retention.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    def status(self, capture_id: str) -> dict[str, Any]:
        """Return the latest retention status for one capture."""
        capture_id = _text(capture_id, "capture_id")
        rows = self.list_index()
        row = next((entry for entry in rows if entry.get("capture_id") == capture_id), None)
        if row is None:
            raise _FullCaptureError("Capture is not in the archive",
                                    details={"capture_id": capture_id})
        audio = row.get("audio")
        if isinstance(audio, Mapping) and any(
                isinstance(item, Mapping) and item.get("status") == "stored"
                for item in audio.values()):
            return {"capture_id": capture_id, "retention_status": "stored"}
        status = "leased"
        retention_path = self.root_real / "retention.jsonl"
        if retention_path.is_file():
            for line in retention_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("capture_id") == capture_id:
                    status = str(event.get("status", status))
        return {"capture_id": capture_id, "retention_status": status}

    def read(self, capture_id: str) -> FullCaptureRecord:
        capture_id = _text(capture_id, "capture_id")
        path = self.captures / capture_id / "record.json"
        if not path.is_file():
            raise _FullCaptureError("Capture is not in the archive",
                                  details={"capture_id": capture_id})
        data = json.loads(path.read_text(encoding="utf-8"))
        record = FullCaptureRecord.from_dict(data)
        expected = hashlib.sha256(canonical_payload(record.to_mapping())).hexdigest()
        if record.integrity_sha256 != expected:
            raise _FullCaptureError("Archived capture failed integrity verification",
                                          field="integrity_sha256")
        for index, asset in enumerate(record.audio):
            if not isinstance(asset, Mapping):
                continue
            relative = asset.get("archive_audio")
            if relative is None:
                continue
            if not isinstance(relative, str) or not relative.startswith("audio/sha256/"):
                raise _FullCaptureError("Archived audio path is invalid",
                                        field="audio",
                                        details={"index": index})
            blob = (self.root_real / relative).resolve()
            try:
                blob.relative_to(self.root_real)
            except ValueError as exc:
                raise _FullCaptureError("Archived audio escaped the archive",
                                        field="audio",
                                        details={"path": str(blob)}) from exc
            if not blob.is_file():
                raise _FullCaptureError("Archived audio is missing",
                                        field="audio",
                                        details={"source_guid": asset.get("source_guid"),
                                                 "path": str(blob)})
            if _sha256_file(blob) != str(asset.get("sha256")).lower():
                raise _FullCaptureError("Archived audio hash mismatch",
                                        field="audio",
                                        details={"source_guid": asset.get("source_guid")})
        attempts = record.timings.get("master_attempts")
        if isinstance(attempts, (list, tuple)):
            for index, attempt in enumerate(attempts):
                if not isinstance(attempt, Mapping):
                    continue
                relative = attempt.get("archive_audio")
                if relative is None:
                    continue
                if not isinstance(relative, str) or not relative.startswith("audio/sha256/"):
                    raise _FullCaptureError("Archived evidence path is invalid",
                                            field="timings.master_attempts",
                                            details={"index": index})
                evidence = (self.root_real / relative).resolve()
                try:
                    evidence.relative_to(self.root_real)
                except ValueError as exc:
                    raise _FullCaptureError("Archived evidence escaped the archive",
                                            field="timings.master_attempts",
                                            details={"path": str(evidence)}) from exc
                if not evidence.is_file():
                    raise _FullCaptureError("Archived evidence is missing",
                                            field="timings.master_attempts",
                                            details={"index": index})
                if _sha256_file(evidence) != str(attempt.get("sha256")).lower():
                    raise _FullCaptureError("Archived evidence hash mismatch",
                                            field="timings.master_attempts",
                                            details={"index": index})
        return record

    def list_index(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for line in self.index_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "SCHEMA_VERSION",
    "ARCHIVE_SCHEMA_VERSION",
    "REQUIRED_MAP_KEYS",
    "REQUIRED_ASSET_KEYS",
    "FullCaptureRecord",
    "ArchivedCapture",
    "CaptureArchive",
    "canonical_payload",
]
