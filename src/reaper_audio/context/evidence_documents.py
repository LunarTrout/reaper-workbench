"""Per-source evidence documents and a thin capture manifest — §4.12.

A sealed capture record holds every source in one document.  That document is
already 4.8 MB for a seven-track bench project and grows linearly with the
track count, so re-reading one source means parsing all of them, and adding a
source means rewriting the whole file.

This module splits a *committed* record into:

* ``documents/<capture_id>/<source>.json`` — one self-contained document per
  source (its analysis block, audio entry, project rows and relation edges),
  written once and never rewritten;
* ``captures/<capture_id>/manifest.json`` — the thin index the contract asks for:
  ``source -> {doc, sha256, bytes, render_key, interval, revision}``.

The canonical ``record.json`` is never modified: it stays the integrity anchor,
so old readers keep working and new readers can resolve a single source without
loading the whole capture.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = "reaper-workbench.capture-manifest/1"
DOCUMENT_SCHEMA = "reaper-workbench.source-document/1"
DOCUMENTS_DIR = "documents"


class EvidenceDocumentError(RuntimeError):
    """The archive cannot serve a per-source document request."""


def _plain(value: Any) -> Any:
    """Thaw read-only mappings so a document can be serialised."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _source_id(entry: Mapping[str, Any]) -> str:
    guid = str(entry.get("source_guid") or entry.get("guid") or "")
    scope = "master" if entry.get("is_master") else "track"
    return f"{scope}:{guid}"


def _project_rows(data: Mapping[str, Any], guid: str) -> dict[str, Any]:
    """Pull this source's rows out of the record's ``project_map`` sections."""
    rows: dict[str, Any] = {}
    project_map = data.get("project_map") or {}
    for section, block in project_map.items():
        if not isinstance(block, Mapping):
            continue
        selected: dict[str, Any] = {}
        for key in ("tracks", "master"):
            value = block.get(key)
            if isinstance(value, list):
                matches = [row for row in value
                           if isinstance(row, Mapping)
                           and str(row.get("guid") or "") == guid]
                if matches:
                    selected[key] = matches
            elif isinstance(value, Mapping) and str(value.get("guid") or "") == guid:
                selected[key] = value
        if selected:
            rows[section] = selected
    return rows


def _relations_for(data: Mapping[str, Any], guid: str) -> list[Any]:
    relations = data.get("relations") or {}
    edges = relations.get("edges") if isinstance(relations, Mapping) else None
    if not isinstance(edges, list):
        return []
    return [edge for edge in edges
            if isinstance(edge, Mapping)
            and guid in {str(edge.get("from") or ""), str(edge.get("to") or "")}]


def build_source_document(data: Mapping[str, Any], source_id: str) -> dict[str, Any]:
    """One source's evidence, self-contained and JSON-ready."""
    scope, _, guid = source_id.partition(":")
    audio_entries = [entry for entry in (data.get("audio") or [])
                     if _source_id(entry) == source_id]
    analysis = (data.get("analysis") or {}).get(guid)
    if not audio_entries and analysis is None:
        raise EvidenceDocumentError(f"source {source_id} is not in this capture")
    audio = _plain(audio_entries[0]) if audio_entries else {}
    return {
        "schema": DOCUMENT_SCHEMA,
        "capture_id": data.get("capture_id"),
        "project_id": data.get("project_id"),
        "source_id": source_id,
        "is_master": scope == "master",
        "revision": data.get("revision_before"),
        "time_window": data.get("time_window"),
        "fingerprint": {
            "render_key": audio.get("render_key"),
            "complete": bool(audio.get("render_key_complete")),
            "capture_method": audio.get("capture_method"),
        },
        "audio": audio,
        "analysis": _plain(analysis),
        "project": _plain(_project_rows(data, guid)),
        "relations": _plain(_relations_for(data, guid)),
        "record_sha256": data.get("integrity_sha256"),
    }


def _write_once(path: Path, payload: bytes) -> str:
    """Write a document atomically, refusing to rewrite different content."""
    if path.exists():
        existing = path.read_bytes()
        if existing != payload:
            raise EvidenceDocumentError(
                f"refusing to rewrite a sealed document: {path.name}")
        return _sha256_bytes(existing)
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(path.suffix + ".tmp")
    with staging.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(staging, path)
    return _sha256_bytes(payload)


def write_capture_documents(*, archive_root: str | os.PathLike[str],
                            capture_id: str,
                            data: Mapping[str, Any],
                            record_bytes: int) -> dict[str, Any]:
    """Split one committed record into per-source documents + a manifest.

    Idempotent: an existing document with identical bytes is left untouched, a
    differing one is refused rather than overwritten.
    """
    root = Path(archive_root).expanduser().absolute()
    capture_dir = root / "captures" / capture_id
    doc_dir = capture_dir / DOCUMENTS_DIR
    sources: dict[str, Any] = {}
    total_bytes = 0
    for entry in data.get("audio") or []:
        source_id = _source_id(entry)
        document = build_source_document(data, source_id)
        payload = json.dumps(document, ensure_ascii=False, sort_keys=True,
                             indent=0).encode("utf-8")
        name = source_id.replace(":", "-").replace("{", "").replace("}", "") + ".json"
        digest = _write_once(doc_dir / name, payload)
        total_bytes += len(payload)
        sources[source_id] = {
            "doc": f"captures/{capture_id}/{DOCUMENTS_DIR}/{name}",
            "sha256": digest,
            "bytes": len(payload),
            "render_key": document["fingerprint"]["render_key"],
            "complete": document["fingerprint"]["complete"],
            "is_master": document["is_master"],
            "interval": data.get("time_window"),
            "revision": data.get("revision_before"),
        }
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "capture_id": capture_id,
        "project_id": data.get("project_id"),
        "revision": data.get("revision_before"),
        "time_window": data.get("time_window"),
        "record": {"path": f"captures/{capture_id}/record.json",
                   "bytes": record_bytes,
                   "integrity_sha256": data.get("integrity_sha256")},
        "sources": sources,
        "manifest_bytes": None,  # filled below, after serialisation
    }
    encoded = json.dumps({**manifest, "manifest_bytes": None}, ensure_ascii=False,
                         sort_keys=True, indent=0).encode("utf-8")
    manifest["manifest_bytes"] = None
    manifest_path = capture_dir / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing.get("sources") != manifest["sources"]:
            raise EvidenceDocumentError("refusing to rewrite a sealed manifest")
    else:
        staging = manifest_path.with_suffix(".json.tmp")
        staging.write_bytes(encoded)
        os.replace(staging, manifest_path)
    return manifest


def manifest_for(archive_root: str | os.PathLike[str],
                 capture_id: str) -> dict[str, Any] | None:
    """Read a capture manifest, or ``None`` for captures that predate it."""
    path = Path(archive_root).expanduser().absolute() / "captures" / capture_id / "manifest.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def read_source_document(archive_root: str | os.PathLike[str],
                         capture_id: str, source_id: str) -> dict[str, Any]:
    """Load exactly one source's document, verifying its recorded hash."""
    manifest = manifest_for(archive_root, capture_id)
    if manifest is None:
        raise EvidenceDocumentError(
            f"capture {capture_id} has no manifest; read record.json instead")
    entry = (manifest.get("sources") or {}).get(source_id)
    if not isinstance(entry, Mapping):
        raise EvidenceDocumentError(f"{source_id} is not in this manifest")
    root = Path(archive_root).expanduser().absolute()
    path = root / str(entry["doc"])
    payload = path.read_bytes()
    if _sha256_bytes(payload) != entry.get("sha256"):
        raise EvidenceDocumentError(f"document {path.name} does not match its hash")
    return json.loads(payload)


__all__ = ["DOCUMENT_SCHEMA", "MANIFEST_SCHEMA", "EvidenceDocumentError",
           "build_source_document", "manifest_for", "read_source_document",
           "write_capture_documents"]
