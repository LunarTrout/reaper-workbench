"""Portable panorama lifecycle: open, pages, completion receipts, deltas, refresh, slice.

This module is deliberately independent of any MCP client or host context machinery.
It turns an immutable capture archive into one logical panorama document and exposes
the transport primitives required by the cross-client contract:

* ``open``      -> page 0 plus a manifest, handle and document hash
* ``page``      -> one deterministic page, recorded server-side
* ``ack``       -> verifies every page hash and mints a complete-view receipt
* ``status``    -> current revision/root and page progress
* ``changes_since`` -> factual delta from an exact base capture/root
* ``refresh``   -> a complete current document (normal operation)
* ``slice``     -> whole-project structure plus one interval's evidence

Nothing here edits a REAPER project. Mutation tools must call ``require_receipt``
before applying a change.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..analysis.relations import (RelationBatch, band_dominance_interval,
                                  merge_batches)
from ..errors import StaleContext, ValidationError
from .changes import (DELTA_SCHEMA, build_structural_delta, delta_hash as _delta_hash,
                     digest_facts, next_view_root)
from .full_capture import CaptureArchive, FullCaptureRecord
from .identity import (identity_from_mapping, normalize_project_id,
                       same_identity_values)
from .details import DEFAULT_DETAIL_BYTES, resolve_details
from .panorama import (PANORAMA_SCHEMA_VERSION, build_panorama, load_panorama,
                       panorama_path_for_record)
from .query import run_query
from .panorama_view import (DEFAULT_RESPONSE_BYTES, LEAN_OVERVIEW_BYTES, budget_report,
                            build_minimum_view, build_view, canonical as view_canonical,
                            view_bytes)
from .render_dependencies import compare_render_manifests
from .relations import RelationBinding, RelationWindow, build_relation_set

# One logical document per capture, keyed by the archive files' identity.  A
# a capture record can be hundreds of megabytes, so rebuilding a document for
# every page or detail call would be prohibitive; the cache stores the derived
# projection only and is invalidated when either file changes on disk.
_DOCUMENT_CACHE: "OrderedDict[tuple, dict[str, Any]]" = OrderedDict()
_DOCUMENT_CACHE_LIMIT = 3
# Parsing a capture record is the expensive step for detail fetches.  Cache the
# frozen record only when it is small enough to hold comfortably; a multi-hundred
# megabyte record is re-read instead of pinned in memory.
_RECORD_CACHE: "OrderedDict[tuple, Any]" = OrderedDict()
_RECORD_CACHE_MAX_BYTES = 64 * 1024 * 1024


def _document_cache_key(capture_dir: Path) -> tuple:
    def stamp(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    return (str(capture_dir), stamp(capture_dir / "record.json"),
            stamp(capture_dir / "panorama.json"))


def _cache_get(key: tuple) -> dict[str, Any] | None:
    document = _DOCUMENT_CACHE.get(key)
    if document is not None:
        _DOCUMENT_CACHE.move_to_end(key)
    return document


def _cache_put(key: tuple, document: dict[str, Any]) -> None:
    _DOCUMENT_CACHE[key] = document
    _DOCUMENT_CACHE.move_to_end(key)
    while len(_DOCUMENT_CACHE) > _DOCUMENT_CACHE_LIMIT:
        _DOCUMENT_CACHE.popitem(last=False)


LIFECYCLE_SCHEMA = "reaper-workbench.panorama-lifecycle/1"
DEFAULT_PAGE_BYTES = 24_576
MAX_PAGE_BYTES = 262_144
# Paged delivery of the model view.  A page is a set of whole top-level view
# sections; the target is the measured response size, not a payload estimate.
# There is no page-count limit: a larger project simply yields more pages, as
# long as every single page stays inside the client's response cap.
VIEW_PAGE_TARGET_BYTES = 30_000
VIEW_PAGE_MAX_BYTES = 46_000
# Page sizing uses the character count of this deterministic indented JSON
# rendering. It is not a UTF-8 wire-byte count or the complete MCP transport
# envelope; clients and hosts may apply additional framing or limits.
_PAGE_HEADER_ALLOWANCE = 3_200


def _delivered_bytes(value: Any) -> int:
    """Character count of the server's indented JSON rendering of a result."""
    return len(json.dumps(value, indent=2, ensure_ascii=False))
MAX_DELTA_CHAIN = 20
MAX_DELTA_OPS = 4_000
HANDLE_TTL_SECONDS = 24 * 60 * 60
RECEIPT_TTL_SECONDS = 24 * 60 * 60

_EPHEMERAL_TOP_LEVEL = frozenset({
    "document_sha256",
    "capture_binding",
    "provenance",
    "analysis_archive",
    "reading_contract",
    "legend",
    "stable_id_legend_contract",
    "crosslink_contract",
    "audio_capture_revisions",
    "audio_capture_revision_contract",
    "analysis_axis",
    "render_manifest",
    "evidence_freshness",
    "structural_facts",
})

_ALIAS_FIELDS = frozenset({
    "stable_id", "audio_id", "analysis_id", "item_id", "take_id", "midi_id",
    "track_id", "source_id", "active_take_id", "owner_id", "destination_id",
})


def _json_default(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (list, tuple)):
        return list(value)
    raise TypeError(f"not JSON-serialisable: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False,
                      default=_json_default).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _deep_copy(value: Any) -> Any:
    return json.loads(canonical_bytes(value))


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _split_text(data: bytes, page_bytes: int) -> list[str]:
    """Split UTF-8 bytes at character boundaries into pages of <= page_bytes."""
    if page_bytes <= 0:
        raise ValidationError("page_bytes must be positive")
    text = data.decode("utf-8")
    pages: list[str] = []
    start = 0
    size = 0
    for index, character in enumerate(text):
        width = len(character.encode("utf-8"))
        if size and size + width > page_bytes:
            pages.append(text[start:index])
            start = index
            size = 0
        size += width
    pages.append(text[start:])
    return pages


def _canonical_ids(value: Any, legend: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return legend.get(value, value)
    if isinstance(value, list):
        return [_canonical_ids(item, legend) for item in value]
    if isinstance(value, Mapping):
        return {key: _canonical_ids(item, legend) for key, item in value.items()}
    return value


def _relation_fact_view(relations: Any) -> None:
    """Strip capture-local identity from relation evidence for fact comparison.

    Measurements, statuses, coverage and pair rows remain.  Capture ids,
    revision keys, derived relation ids, asset hashes and pointer strings are
    removed so the fact root is comparable across a re-capture.  The full
    documents still carry all of them and are covered by the document hash.
    """

    if not isinstance(relations, dict):
        return
    for key in ("binding", "content_hash", "page_contract", "contract",
                "sources_note", "canonical_schema_version"):
        relations.pop(key, None)
    sources = relations.get("sources")
    if isinstance(sources, dict):
        for row in sources.values():
            if isinstance(row, dict):
                row.pop("asset_sha256", None)
    for edge in relations.get("edges") or []:
        if isinstance(edge, dict):
            edge.pop("id", None)
    for pair in relations.get("evaluated_pairs") or []:
        if isinstance(pair, dict):
            pair.pop("id", None)
    # Legacy v1 shape: keep the same stripping for archived documents.
    for relation in relations.get("relations") or []:
        if not isinstance(relation, dict):
            continue
        for field in ("relation_id", "provenance", "dep_set"):
            relation.pop(field, None)
        attributes = relation.get("attributes")
        if isinstance(attributes, dict):
            temporal = attributes.get("temporal_evidence")
            if isinstance(temporal, dict):
                for field in ("from_series_ref", "to_series_ref", "time_ref", "band_ref"):
                    temporal.pop(field, None)
    evaluations = relations.get("evaluations")
    if isinstance(evaluations, dict):
        for table in evaluations.values():
            if not isinstance(table, dict):
                continue
            for field in ("capture_id", "revision_key"):
                table.pop(field, None)
            columns = list(table.get("columns") or [])
            drop = [index for index, name in enumerate(columns)
                    if name in {"relation_id", "dep_set"}]
            if not drop:
                continue
            table["columns"] = [name for index, name in enumerate(columns)
                                if index not in drop]
            table["rows"] = [
                [value for index, value in enumerate(row) if index not in drop]
                for row in table.get("rows") or []
            ]


def stable_fact_view(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return the capture-independent factual view used for the fact root and deltas.

    Capture ids, record paths, capture timestamps, per-render audio hashes and
    document-local aliases are removed. Everything else, including every numeric
    value and both temporal levels, is retained.
    """
    view = _deep_copy(document)
    legend = view.get("stable_id_legend")
    if not isinstance(legend, Mapping):
        legend = {}
    view = _canonical_ids(view, {str(k): str(v) for k, v in legend.items()})
    for key in _EPHEMERAL_TOP_LEVEL:
        view.pop(key, None)
    view.pop("stable_id_legend", None)
    # Detail entries carry capture-local ids, pointers and locators. Keep only
    # the content identity so a delta can name a linked payload that changed,
    # without making the fact root depend on this capture's address book.
    details = view.get("details")
    if isinstance(details, Mapping):
        view["details"] = {
            "schema": details.get("schema"),
            "entries": [
                {
                    "ref": entry.get("ref"),
                    "kind": entry.get("kind"),
                    "count": entry.get("count"),
                    "truncated": entry.get("truncated"),
                    "content_sha256": entry.get("content_sha256"),
                }
                for entry in sorted(details.get("entries") or [],
                                    key=lambda row: str(row.get("ref")))
                if isinstance(entry, Mapping)
            ],
        }
    else:
        view.pop("details", None)

    for track in view.get("tracks") or []:
        if isinstance(track, dict):
            for field in ("stable_id", "audio_id", "analysis_id"):
                track.pop(field, None)

    for item in view.get("items") or []:
        if isinstance(item, dict):
            for field in ("item_id", "active_take_id", "midi_id", "track_id"):
                item.pop(field, None)

    for take in view.get("takes") or []:
        if isinstance(take, dict):
            for field in ("take_id", "item_id", "midi_id", "track_id"):
                take.pop(field, None)

    for fx in view.get("fx") or []:
        if isinstance(fx, dict):
            for field in ("fx_id", "owner_id"):
                fx.pop(field, None)

    for route in view.get("routes") or []:
        if isinstance(route, dict):
            for field in ("route_id", "source_id", "destination_id"):
                route.pop(field, None)

    for audio in view.get("audio") or []:
        if not isinstance(audio, dict):
            continue
        for field in ("audio_id", "analysis_id", "source_id", "sha256", "archive_audio",
                      "capture_revision", "render_key", "render_key_complete",
                      "render_dependencies", "mapping"):
            audio.pop(field, None)
        provenance = audio.get("render_provenance")
        if isinstance(provenance, dict):
            provenance.pop("capture_revision_change_count", None)
            provenance.pop("capture_revision_sha256", None)

    for analysis in view.get("analysis") or []:
        if isinstance(analysis, dict):
            analysis.pop("analysis_id", None)
            analysis.pop("source_guid", None)

    temporal = view.get("analysis_temporal")
    if isinstance(temporal, dict):
        temporal.pop("record_path", None)
        temporal.pop("record_integrity_sha256", None)

    _relation_fact_view(view.get("relations"))
    summary = view.get("relations_summary")
    if isinstance(summary, dict):
        summary.pop("content_hash", None)

    return view


def fact_root(document: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_bytes(stable_fact_view(document)))


def document_hash(document: Mapping[str, Any]) -> str:
    """The sealed document hash.

    ``build_panorama`` hashes every field except ``document_sha256`` and stores
    the result there; ``validate_binding`` recomputes it on load.  Returning the
    stored value keeps receipts, tokens and queries bound to the *archived*
    identity instead of a second, differently-defined hash of the same document.
    """
    sealed = document.get("document_sha256")
    if isinstance(sealed, str) and sealed:
        return sealed
    return sha256_bytes(canonical_bytes(document))


def _normalise_revision(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"project_id": "", "change_count": -1, "path": "",
                "instance_id": None, "epoch": None}
    raw_epoch = value.get("epoch")
    epoch = (raw_epoch if isinstance(raw_epoch, int) and not isinstance(raw_epoch, bool)
             and raw_epoch >= 1 else None)
    instance = value.get("instance_id")
    raw_count = value.get("change_count", -1)
    try:
        change_count = int(raw_count)
    except (TypeError, ValueError):
        change_count = -1
    return {
        "project_id": str(value.get("project_id", "")),
        "change_count": change_count,
        "path": str(value.get("path", "")),
        "instance_id": instance if isinstance(instance, str) and instance else None,
        "epoch": epoch,
    }


def revisions_match(left: Mapping[str, Any] | None,
                    right: Mapping[str, Any] | None) -> bool:
    """Compare revisions including the continuity identity when either side has it.

    A side that never measured ``instance_id``/``epoch`` cannot prove continuity.
    If the other side did measure them, the comparison is a mismatch rather than
    a silent pass: the old lenient behaviour let a restarted REAPER reuse a path
    and change counter.  Revisions that both predate the identity fields keep
    comparing by project/counter/path for backwards-compatible archive reads.
    """
    a = _normalise_revision(left)
    b = _normalise_revision(right)
    if (normalize_project_id(a["project_id"]) != normalize_project_id(b["project_id"]) or
            a["change_count"] != b["change_count"] or
            normalize_project_id(a["path"]) != normalize_project_id(b["path"])):
        return False
    for key in ("instance_id", "epoch"):
        if a[key] is not None or b[key] is not None:
            if a[key] != b[key]:
                return False
    return True


def _diff_values(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    if before == after:
        return []
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        operations: list[dict[str, Any]] = []
        for key in sorted(set(before) | set(after)):
            child = f"{path}/{key}"
            if key not in before:
                operations.append({"op": "add", "path": child, "after": after[key]})
            elif key not in after:
                operations.append({"op": "remove", "path": child, "before": before[key]})
            else:
                operations.extend(_diff_values(before[key], after[key], child))
        return operations
    if isinstance(before, list) and isinstance(after, list):
        operations = []
        for index in range(max(len(before), len(after))):
            child = f"{path}/{index}"
            if index >= len(before):
                operations.append({"op": "add", "path": child, "after": after[index]})
            elif index >= len(after):
                operations.append({"op": "remove", "path": child, "before": before[index]})
            else:
                operations.extend(_diff_values(before[index], after[index], child))
        return operations
    return [{"op": "replace", "path": path, "before": before, "after": after}]


def _domain_counts(view: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for key, value in view.items():
        if isinstance(value, list):
            result[key] = {"count": len(value), "hash": sha256_bytes(canonical_bytes(value))}
        elif isinstance(value, Mapping):
            result[key] = {"count": len(value), "hash": sha256_bytes(canonical_bytes(value))}
    return result


_AUDIO_AFFECTING_FIELDS = frozenset({
    "volume", "pan", "mute", "solo", "channel_count", "pan_mode",
    "fx", "sends", "routes", "automation", "envelopes", "folder_depth",
})
_AUDIO_AFFECTING_ROOTS = frozenset({
    "master", "routes", "sends", "automation", "envelopes", "fx",
    "items", "takes", "midi",
})


def _invalidations(operations: Sequence[Mapping[str, Any]],
                   target_document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Mark rendered evidence stale when its dependencies changed.

    A delta is factual about project state; it cannot silently keep old audio or
    old measurements as if the signal path were unchanged. This is deliberately
    conservative: processing/arrangement edits invalidate all rendered evidence,
    while name/colour/selection edits do not.
    """
    dependency_paths: list[str] = []
    for operation in operations:
        path = str(operation.get("path") or "")
        parts = [part for part in path.split("/") if part]
        if not parts:
            continue
        root = parts[0]
        if root in {"analysis", "analysis_temporal", "coverage", "truncation",
                    "crosslinks", "per_track"}:
            continue
        if root == "project" or root == "time_map" or root == "tempo":
            continue
        if root in _AUDIO_AFFECTING_ROOTS:
            if root == "tracks" and len(parts) >= 3 and parts[2] not in _AUDIO_AFFECTING_FIELDS:
                continue
            dependency_paths.append(path)
            continue
        if root == "tracks" and len(parts) >= 2:
            if len(parts) == 2:
                dependency_paths.append(path)
            elif parts[2] in _AUDIO_AFFECTING_FIELDS:
                dependency_paths.append(path)
            elif parts[2] == "mixer" and len(parts) >= 4 and parts[3] in _AUDIO_AFFECTING_FIELDS:
                dependency_paths.append(path)
    if not dependency_paths:
        return []
    sources: list[str] = []
    for row in target_document.get("audio") or []:
        if isinstance(row, Mapping):
            source = row.get("source_id") or row.get("source_guid") or row.get("audio_id")
            if source:
                sources.append(str(source))
    return [{
        "kind": "audio_and_analysis",
        "scope": "all_rendered_sources",
        "source_count": len(sources),
        "sources": sources,
        "reason": "processing_or_arrangement_dependency_changed",
        "dependency_paths": sorted(set(dependency_paths)),
        "requires_recapture": True,
        "note": ("Old audio and measurements remain historical capture facts, but they no "
                 "longer describe the changed project revision; recapture before comparing."),
    }]


# The public status is an allowlist: a handle file may still carry legacy page
# bookkeeping (page_count, received pages, mode="pages") from older archives, and
# none of that may reach a model-facing tool. Only these delivery facts are
# reported, and only the documented handle fields are serialized.
DELIVERY_STATUS_FIELDS = ("view_schema", "view_sha256", "view_bytes", "response_bytes",
                          "measured_bytes", "max_response_bytes", "fits")
_PAGED_STATUS_FIELDS = ("page_count", "pages_received", "pages_missing", "complete")


def delivery_status(state: Mapping[str, Any]) -> dict[str, Any]:
    """Sanitized delivery facts for one stored handle.

    Reads whatever the handle file happens to contain and returns only the
    allowlisted keys, so a legacy page handle cannot smuggle ``mode``,
    ``page_count``, page/ack or cursor fields through the public status.  A
    handle created by the *current* paged contract (``mode == "view_pages"``)
    legitimately reports its page progress, because resuming an interrupted
    receipt needs it.
    """
    raw = state.get("delivery")
    raw = raw if isinstance(raw, Mapping) else {}
    out = {key: raw[key] for key in DELIVERY_STATUS_FIELDS if key in raw}
    if str(state.get("mode")) == "view_pages":
        pages = list(state.get("view_pages") or [])
        received = sorted(set(int(item) for item in state.get("received") or []))
        out.update({
            "page_count": len(pages),
            "pages_received": [pages[i]["page_id"] for i in received],
            "pages_missing": [pages[i]["page_id"] for i in range(len(pages))
                              if i not in set(received)],
            "complete": bool(state.get("complete") and state.get("receipt")),
        })
    return out


def _largest_list(value: Any, prefix: tuple[str, ...] = ()) -> tuple[int, tuple[str, ...]] | None:
    best: tuple[int, tuple[str, ...]] | None = None
    if isinstance(value, Mapping):
        for name, item in value.items():
            if isinstance(item, list) and item:
                size = _delivered_bytes({name: item})
                if best is None or size > best[0]:
                    best = (size, prefix + (str(name),))
            elif isinstance(item, Mapping):
                found = _largest_list(item, prefix + (str(name),))
                if found and (best is None or found[0] > best[0]):
                    best = found
    return best


def _replace_at(value: Any, path: tuple[str, ...], chunk: Any) -> Any:
    if not path:
        return chunk
    if isinstance(value, Mapping):
        clone = dict(value)
        clone[path[0]] = _replace_at(value[path[0]], path[1:], chunk)
        return clone
    raise ValidationError("View part path is not a mapping",
                          details={"path": list(path)})


def _split_section(key: str, value: Any, ceiling: int) -> list[tuple[Any, list[str] | None]]:
    """Split one oversized section into parts that each fit a page envelope.

    The largest list anywhere in the section (top-level rows, a nested
    ``rows.rows`` table, an item list) is split on element boundaries; every
    other field is repeated in every part.  Merging concatenates exactly that
    list, so the reassembled section is byte-for-byte the original.
    """
    def fits(part: Any) -> bool:
        return _delivered_bytes({"payload": {key: part}}) + _PAGE_HEADER_ALLOWANCE <= ceiling

    if isinstance(value, list):
        path: tuple[str, ...] = ()
        items = list(value)
    else:
        found = _largest_list(value)
        if found is None:
            raise ValidationError(
                "One view section does not fit a single page",
                details={"section": key, "section_bytes": _delivered_bytes({key: value}),
                         "page_max_bytes": ceiling,
                         "next_action": "raise max_page_bytes; the view is never truncated"})
        _size, path = found
        items = value
        for step in path:
            items = items[step]
        items = list(items)
    parts: list[tuple[Any, list[str] | None]] = []
    start = 0
    while start < len(items):
        end = start + 1
        while end < len(items):
            candidate = _replace_at(value, path, items[start:end + 1]) if path else items[start:end + 1]
            if not fits(candidate):
                break
            end += 1
        chunk = items[start:end]
        part = _replace_at(value, path, chunk) if path else chunk
        if not fits(part):
            raise ValidationError(
                "One view entry does not fit a single page",
                details={"section": key, "path": list(path), "index": start,
                         "entry_bytes": _delivered_bytes({key: items[start]}),
                         "page_max_bytes": ceiling})
        parts.append((part, list(path) if path else None))
        start = end
    return parts


def _pack_view_sections(view: Mapping[str, Any], *,
                        payload_budget: int,
                        max_page_bytes: int = VIEW_PAGE_MAX_BYTES
                        ) -> list[list[tuple[str, Any]]]:
    """Greedily pack the view into pages of whole sections or section parts.

    A section that fits one page is never split.  Only a section larger than a
    page is split, and then only at list-element boundaries with every head
    field repeated; ``_merge_view_pages`` concatenates the parts back in page
    order, so the merged view is byte-for-byte the built view.
    """
    pages: list[list[tuple[str, Any]]] = []
    current: list[tuple[str, Any]] = []
    current_keys: set[str] = set()
    size = 0
    for key, value in view.items():
        section_bytes = _delivered_bytes({key: value})
        units: list[tuple[Any, list[str] | None]]
        if section_bytes + _PAGE_HEADER_ALLOWANCE <= max_page_bytes:
            units = [(value, None)]
        else:
            units = _split_section(str(key), value, max_page_bytes)
        split = len(units) > 1
        for index, (unit, path) in enumerate(units):
            unit_bytes = _delivered_bytes({key: unit})
            if current and (size + unit_bytes > payload_budget or key in current_keys):
                pages.append(current)
                current, current_keys, size = [], set(), 0
            current.append((key, unit))
            current_keys.add(str(key))
            size += unit_bytes
            if split:
                meta = {str(key): {"path": path, "part": index + 1, "total": len(units)}}
                for position, entry in enumerate(current):
                    if entry[0] == "__parts__":
                        current[position] = ("__parts__", {**entry[1], **meta})
                        break
                else:
                    current.append(("__parts__", meta))
    if current:
        pages.append(current)
    return pages


def _merge_view_pages(pages: Sequence[Mapping[str, Any]]) -> OrderedDict[str, Any]:
    """Reassemble the view from page payloads, concatenating split parts."""
    merged: OrderedDict[str, Any] = OrderedDict()
    for page in pages:
        parts = (page.get("payload") or {}).get("__parts__") or {}
        for key, value in (page.get("payload") or {}).items():
            if key == "__parts__":
                continue
            if key not in merged:
                merged[key] = value
                continue
            path = (parts.get(key) or {}).get("path") or []
            target = merged[key]
            for step in path[:-1]:
                target = target[step]
            if path:
                target[path[-1]] = list(target.get(path[-1]) or []) + list(
                    (value if not path[:-1] else value) if False else _read_at(value, path))
            elif isinstance(merged[key], list) and isinstance(value, list):
                merged[key] = list(merged[key]) + list(value)
            else:
                raise ValidationError("Page fragments cannot be merged",
                                      details={"section": key})
    return merged


def _read_at(value: Any, path: Sequence[str]) -> Any:
    for step in path:
        value = value[step]
    return value


class PanoramaLifecycle:
    """Server-side lifecycle store for one capture archive."""

    def __init__(self, archive_root: str | os.PathLike[str],
                 *, page_bytes: int = DEFAULT_PAGE_BYTES,
                 state_root: str | os.PathLike[str] | None = None) -> None:
        if isinstance(page_bytes, bool) or not isinstance(page_bytes, int) or page_bytes <= 0:
            raise ValidationError("page_bytes must be a positive integer")
        self.page_bytes = min(page_bytes, MAX_PAGE_BYTES)
        self.archive = CaptureArchive(archive_root)
        self.state_dir = Path(state_root or (self.archive.root / "lifecycle"))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.handles_dir = self.state_dir / "handles"
        self.receipts_dir = self.state_dir / "receipts"
        self.contexts_dir = self.state_dir / "contexts"
        self.deltas_dir = self.state_dir / "deltas"
        self.handles_dir.mkdir(exist_ok=True)
        self.receipts_dir.mkdir(exist_ok=True)
        self.contexts_dir.mkdir(exist_ok=True)
        self.deltas_dir.mkdir(exist_ok=True)

    # -- capture / document loading -----------------------------------------

    def _index_row(self, capture_id: str) -> dict[str, Any] | None:
        for row in self.archive.list_index():
            if str(row.get("capture_id")) == capture_id:
                return row
        return None

    def _record(self, capture_id: str) -> FullCaptureRecord:
        path = self.archive.captures / capture_id / "record.json"
        if not path.is_file():
            raise ValidationError("Capture is not in the archive",
                                  details={"capture_id": capture_id})
        key = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        cached = _RECORD_CACHE.get(key)
        if cached is not None:
            _RECORD_CACHE.move_to_end(key)
            return cached
        record = FullCaptureRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        if key[2] <= _RECORD_CACHE_MAX_BYTES:
            _RECORD_CACHE[key] = record
            _RECORD_CACHE.move_to_end(key)
            while len(_RECORD_CACHE) > 1:
                _RECORD_CACHE.popitem(last=False)
        return record

    def _binding(self, capture_id: str, document: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Return the capture binding without parsing a 460 MB record.

        The panorama sidecar is sealed against the record's binding. Only that
        small binding is needed to validate the sidecar; project_map/analysis
        are deliberately not loaded here.
        """
        if document is None:
            record_path = self.archive.captures / capture_id / "record.json"
            sidecar = panorama_path_for_record(record_path)
            if not sidecar.is_file():
                raise ValidationError("Capture has no panorama sidecar",
                                      details={"capture_id": capture_id})
            document = json.loads(sidecar.read_text(encoding="utf-8"))
        binding = document.get("capture_binding")
        if not isinstance(binding, Mapping):
            raise ValidationError("Panorama has no capture binding")
        row = self._index_row(capture_id) or {}
        return {
            "capture_id": str(binding.get("capture_id") or capture_id),
            "project_id": str(binding.get("project_id") or row.get("project_id") or ""),
            "integrity_sha256": binding.get("record_integrity_sha256"),
            "revision_before": binding.get("revision_before"),
            "revision_after": binding.get("revision_after"),
            "time_window": binding.get("time_window"),
            "project_path": row.get("project_path"),
            "project_map": {},
            "analysis": {},
        }

    def _document(self, capture_id: str) -> dict[str, Any]:
        """Return the current-schema document without mutating the archive.

        A sealed sidecar written under an older panorama schema is read,
        verified against its own binding, and then re-projected in memory from
        the canonical record.  The archived sidecar is never rewritten.
        """
        record_path = self.archive.captures / capture_id / "record.json"
        key = _document_cache_key(record_path.parent)
        cached = _cache_get(key)
        if cached is not None:
            return cached
        sidecar = panorama_path_for_record(record_path)
        if sidecar.is_file():
            raw = json.loads(sidecar.read_text(encoding="utf-8"))
            document = load_panorama(sidecar, self._binding(capture_id, raw),
                                     max_bytes=None, max_tokens=None)
            if document.get("schema_version") == PANORAMA_SCHEMA_VERSION:
                _cache_put(key, document)
                return document
            record = self._record(capture_id)
            upgraded = build_panorama(record, record_path=record_path.name,
                                      max_bytes=10 ** 12, max_tokens=10 ** 12)
            _cache_put(key, upgraded)
            return upgraded
        record = self._record(capture_id)
        document = build_panorama(record, record_path=record_path.name,
                                  max_bytes=10 ** 12, max_tokens=10 ** 12)
        _cache_put(key, document)
        return document

    def _revision(self, binding: Mapping[str, Any], document: Mapping[str, Any],
                  capture_id: str) -> dict[str, Any]:
        revision = (binding.get("revision_after") or binding.get("revision_before")
                    if isinstance(binding, Mapping) else None)
        if not isinstance(revision, Mapping):
            capture_binding = document.get("capture_binding")
            if isinstance(capture_binding, Mapping):
                revision = (capture_binding.get("revision_after") or
                            capture_binding.get("revision_before"))
        value = _normalise_revision(revision)
        value.update({
            "capture_id": capture_id,
            "fact_root": fact_root(document),
            "document_hash": document_hash(document),
            "schema_version": str(document.get("schema_version") or ""),
        })
        return value

    def latest_capture(self, project_id: str) -> dict[str, Any] | None:
        rows = [row for row in self.archive.list_index()
                if normalize_project_id(row.get("project_id")) == normalize_project_id(project_id) and row.get("capture_id")]
        if not rows:
            return None
        rows.sort(key=lambda row: str(row["capture_id"]), reverse=True)
        return rows[0]

    def _capture_by_revision(self, project_id: str,
                             revision: Mapping[str, Any] | None) -> str | None:
        for row in self.archive.list_index():
            if normalize_project_id(row.get("project_id")) != normalize_project_id(project_id):
                continue
            for key in ("revision_before", "revision_after"):
                candidate = row.get(key)
                if isinstance(candidate, Mapping) and revisions_match(candidate, revision):
                    return str(row["capture_id"])
        return None

    # -- handle persistence --------------------------------------------------

    def _handle_path(self, handle: str) -> Path:
        return self.handles_dir / f"{handle}.json"

    def _load_handle(self, handle: str) -> dict[str, Any]:
        path = self._handle_path(handle)
        if not path.is_file():
            raise StaleContext("Panorama handle is unknown or expired; call panorama_refresh",
                               details={"handle": handle})
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise StaleContext("Panorama handle is corrupt; call panorama_refresh")
        if time.time() - float(data.get("created_at", 0)) > HANDLE_TTL_SECONDS:
            raise StaleContext("Panorama handle has expired; call panorama_refresh",
                               details={"handle": handle})
        return data

    def _save_handle(self, handle: str, state: Mapping[str, Any]) -> None:
        _atomic_write(self._handle_path(handle), canonical_bytes(state))

    def _new_handle(self, project_id: str, capture_id: str, revision: Mapping[str, Any],
                    document: Mapping[str, Any], page_bytes: int,
                    *, split_pages: bool = True) -> dict[str, Any]:
        raw = canonical_bytes(document)
        pages = _split_text(raw, page_bytes) if split_pages else []
        handle = "pan_" + secrets.token_urlsafe(18)
        state = {
            "schema_version": LIFECYCLE_SCHEMA,
            "handle": handle,
            "project_id": project_id,
            "capture_id": capture_id,
            "revision": dict(revision),
            "fact_root": fact_root(document),
            "document_hash": document_hash(document),
            "document_bytes": len(raw),
            "mode": "pages" if split_pages else "single",
            "page_bytes": page_bytes,
            "page_hashes": [sha256_bytes(page.encode("utf-8")) for page in pages],
            "page_count": len(pages),
            "received": [],
            "complete": False,
            "receipt": None,
            "created_at": time.time(),
        }
        self._save_handle(handle, state)
        return state

    def _new_read_scope(self, project_id: str, capture_id: str,
                        revision: Mapping[str, Any], document_sha: str,
                        root: str, budget: int) -> dict[str, Any]:
        """Issue a read-only binding for exact queries after a budget refusal.

        It is deliberately not a handle, receipt or context token: it cannot
        complete a panorama and cannot authorize a mutation.  It only lets the
        exact semantic query address the same capture/revision/fact root.
        """
        scope_id = "rs_" + secrets.token_urlsafe(20)
        expires_at = time.time() + HANDLE_TTL_SECONDS
        state = {
            "schema_version": LIFECYCLE_SCHEMA,
            "handle": scope_id,
            "mode": "read_scope",
            "permissions": ["read"],
            "allowed": ["reaper_panorama_query"],
            "project_id": project_id,
            "capture_id": capture_id,
            "revision": dict(revision),
            "fact_root": root,
            "document_hash": document_sha,
            "complete": False,
            "receipt": None,
            "context_token": None,
            "max_response_bytes": int(budget),
            "created_at": time.time(),
            "expires_at": expires_at,
        }
        _atomic_write(self._handle_path(scope_id), canonical_bytes(state))
        return {
            "id": scope_id,
            "permissions": ["read"],
            "allowed": ["reaper_panorama_query"],
            "project_id": project_id,
            "capture_id": capture_id,
            "revision": dict(revision),
            "fact_root": root,
            "document_hash": document_sha,
            "max_response_bytes": int(budget),
            "expires_at": expires_at,
            "note": ("read-only exact queries only; not a receipt, context token or "
                     "mutation authorization"),
        }

    def _mark_received(self, state: dict[str, Any], page_index: int) -> None:
        received = set(int(item) for item in state.get("received") or [])
        received.add(page_index)
        state["received"] = sorted(received)
        self._save_handle(str(state["handle"]), state)

    def _page_envelope(self, state: Mapping[str, Any], page_index: int,
                       pages: Sequence[str]) -> dict[str, Any]:
        if page_index < 0 or page_index >= len(pages):
            raise ValidationError("page_index is outside the document",
                                  details={"page_index": page_index,
                                           "page_count": len(pages)})
        payload = pages[page_index]
        page_hash = sha256_bytes(payload.encode("utf-8"))
        expected = list(state.get("page_hashes") or [])
        if page_index < len(expected) and expected[page_index] != page_hash:
            raise StaleContext("Stored page hash does not match the document")
        received = set(int(item) for item in state.get("received") or [])
        missing = [index for index in range(len(pages)) if index not in received]
        return {
            "schema_version": LIFECYCLE_SCHEMA,
            "status": "page",
            "project_id": state["project_id"],
            "capture_id": state["capture_id"],
            "revision": state["revision"],
            "fact_root": state["fact_root"],
            "document_hash": state["document_hash"],
            "document_bytes": state["document_bytes"],
            "handle": state["handle"],
            "page_index": page_index,
            "page_count": len(pages),
            "page_bytes": state["page_bytes"],
            "page_hash": page_hash,
            "payload": payload,
            "delivery": {
                "complete": len(received) == len(pages) and bool(state.get("receipt")),
                "pages_received": len(received),
                "pages_missing": missing,
                "receipt": state.get("receipt"),
            },
        }

    # -- continuity tokens ---------------------------------------------------

    def _capture_audio_evidence(self, capture_id: str,
                                revision: Mapping[str, Any]) -> dict[str, Any]:
        """Describe the audio evidence a fresh capture token carries.

        A just-acknowledged capture is current by construction.  Coverage says
        how precisely a *later* delta can narrow invalidation, not whether the
        capture describes its own revision.
        """
        document = self._document(capture_id)
        manifest = document.get("render_manifest")
        manifest = manifest if isinstance(manifest, Mapping) else {}
        id_by_guid: dict[str, str] = {}
        for row in document.get("tracks") or []:
            if isinstance(row, Mapping) and row.get("guid") and row.get("stable_id"):
                id_by_guid[str(row["guid"])] = str(row["stable_id"])
        master_row = document.get("master")
        if (isinstance(master_row, Mapping) and master_row.get("guid")
                and master_row.get("stable_id")):
            id_by_guid[str(master_row["guid"])] = str(master_row["stable_id"])
        sources = []
        for row in manifest.get("sources") or []:
            if not isinstance(row, Mapping):
                continue
            source_id = row.get("source_id")
            if not source_id and row.get("source_guid"):
                source_id = id_by_guid.get(str(row["source_guid"]))
            if source_id:
                sources.append(str(source_id))
        complete = bool(manifest.get("sources")) and bool(manifest.get("complete"))
        return {
            "state": "current",
            "base_capture_id": capture_id,
            "invalidated_sources": [],
            "fresh_sources": sorted(sources),
            "reasons": [],
            "coverage": {
                "render_manifest": "complete" if complete else "incomplete_or_absent",
                "authoritative": complete,
                "cross_revision_authoritative": bool(
                    manifest.get("cross_revision_authoritative", False)),
                "unverified": list(manifest.get("unverified") or []),
            },
            "revision": _normalise_revision(revision),
        }

    def _context_path(self, token: str) -> Path:
        return self.contexts_dir / f"{token}.json"

    def _load_context(self, token: str) -> dict[str, Any]:
        if not isinstance(token, str) or not token.startswith("ct_"):
            raise StaleContext("A panorama context token is required",
                               details={"token": token})
        path = self._context_path(token)
        if not path.is_file():
            raise StaleContext("Panorama context token is unknown or expired; refresh",
                               details={"token": token})
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise StaleContext("Panorama context token is corrupt; refresh")
        if time.time() - float(data.get("created_at", 0)) > RECEIPT_TTL_SECONDS:
            raise StaleContext("Panorama context token has expired; refresh",
                               details={"token": token})
        return data

    def _mint_context(self, project_id: str, capture_id: str,
                      revision: Mapping[str, Any], fact_root_value: str,
                      document_hash_value: str, *, kind: str,
                      predecessor: str | None = None,
                      delta_id: str | None = None,
                      view_root: str | None = None,
                      delta_chain: Sequence[Mapping[str, Any]] | None = None,
                      audio_evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
        token = "ct_" + secrets.token_urlsafe(24)
        evidence = dict(audio_evidence or {})
        evidence.setdefault("state", "current")
        evidence.setdefault("invalidated_sources", [])
        evidence.setdefault("fresh_sources", [])
        evidence.setdefault("reasons", [])
        data = {
            "schema_version": LIFECYCLE_SCHEMA,
            "token": token,
            "kind": kind,
            "project_id": project_id,
            "capture_id": capture_id,
            "revision": dict(revision),
            "fact_root": fact_root_value,
            "document_hash": document_hash_value,
            "view_root": view_root or document_hash_value,
            "delta_chain": [dict(entry) for entry in (delta_chain or [])],
            "audio_evidence": evidence,
            "predecessor": predecessor,
            "delta_id": delta_id,
            "created_at": time.time(),
        }
        _atomic_write(self._context_path(token), canonical_bytes(data))
        return data

    # -- public lifecycle ----------------------------------------------------

    def open(self, project_id: str, *, expected_revision: Mapping[str, Any] | None = None,
             page_bytes: int | None = None,
             capture_id: str | None = None) -> dict[str, Any]:
        latest = ({"capture_id": capture_id} if capture_id else
                  self.latest_capture(project_id))
        if latest is None:
            raise ValidationError("No capture exists for this project",
                                  details={"project_id": project_id})
        capture_id = str(latest["capture_id"])
        capture_row = self._index_row(capture_id)
        if capture_row is None or normalize_project_id(capture_row.get("project_id")) != normalize_project_id(project_id):
            raise ValidationError("Requested capture is not in this project archive",
                                  details={"capture_id": capture_id})
        document = self._document(capture_id)
        binding = self._binding(capture_id, document)
        revision = self._revision(binding, document, capture_id)
        if expected_revision is not None and not revisions_match(expected_revision, revision):
            raise StaleContext("The requested revision is not the current captured revision",
                               details={"requested": _normalise_revision(expected_revision),
                                        "current": revision})
        selected_page = min(int(page_bytes or self.page_bytes), MAX_PAGE_BYTES)
        if selected_page <= 0:
            raise ValidationError("page_bytes must be positive")
        state = self._new_handle(project_id, capture_id, revision, document, selected_page)
        pages = _split_text(canonical_bytes(document), selected_page)
        self._mark_received(state, 0)
        envelope = self._page_envelope(state, 0, pages)
        envelope["manifest"] = {
            "page_count": len(pages),
            "page_bytes": selected_page,
            "document_bytes": state["document_bytes"],
            "page_hashes": state["page_hashes"],
            "coverage": document.get("coverage"),
            "truncation": document.get("truncation"),
            "identity": document.get("identity"),
            "evidence": document.get("evidence_freshness"),
            "render_manifest": {
                "present": bool((document.get("render_manifest") or {}).get("sources")),
                "complete": bool((document.get("render_manifest") or {}).get("complete")),
                "unverified": list((document.get("render_manifest") or {}).get("unverified") or []),
            },
            "next_page": 1 if len(pages) > 1 else None,
        }
        envelope["next_call"] = {
            "tool": "reaper_panorama_page",
            "handle": state["handle"],
            "revision": state["revision"],
            "page_index": 1 if len(pages) > 1 else 0,
            "then": "Fetch every page, call reaper_panorama_ack with all page hashes, and only then mutate or claim the whole project.",
        }
        return envelope

    def page(self, handle: str, revision: Mapping[str, Any],
             page_index: int) -> dict[str, Any]:
        state = self._load_handle(handle)
        if not revisions_match(revision, state["revision"]):
            raise StaleContext("Page request is for another revision",
                               details={"requested": _normalise_revision(revision),
                                        "current": state["revision"]})
        document = self._document(str(state["capture_id"]))
        pages = _split_text(canonical_bytes(document), int(state["page_bytes"]))
        envelope = self._page_envelope(state, int(page_index), pages)
        self._mark_received(state, int(page_index))
        envelope["delivery"] = {
            "complete": len(set(state["received"])) == len(pages) and bool(state.get("receipt")),
            "pages_received": len(set(state["received"])),
            "pages_missing": [index for index in range(len(pages))
                              if index not in set(state["received"])],
            "receipt": state.get("receipt"),
        }
        return envelope

    def ack(self, handle: str, revision: Mapping[str, Any],
            page_hashes: Sequence[str]) -> dict[str, Any]:
        state = self._load_handle(handle)
        if not revisions_match(revision, state["revision"]):
            raise StaleContext("Acknowledgement is for another revision",
                               details={"requested": _normalise_revision(revision),
                                        "current": state["revision"]})
        expected = list(state.get("page_hashes") or [])
        supplied = [str(item) for item in page_hashes]
        if len(supplied) != len(expected):
            raise ValidationError("Acknowledgement must contain every page hash",
                                  details={"expected": len(expected),
                                           "received": len(supplied)})
        missing = [index for index in range(len(expected)) if index not in set(state.get("received") or [])]
        if missing:
            return {
                "schema_version": LIFECYCLE_SCHEMA,
                "status": "incomplete",
                "complete": False,
                "missing_pages": missing,
                "pages_received": sorted(set(state.get("received") or [])),
                "page_count": len(expected),
                "document_hash": state["document_hash"],
                "next_call": {"tool": "reaper_panorama_page", "handle": handle,
                              "revision": state["revision"]},
            }
        if supplied != expected:
            raise StaleContext("Page hashes do not match the document manifest",
                               details={"document_hash": state["document_hash"]})
        if state.get("complete") and state.get("receipt"):
            return {
                "schema_version": LIFECYCLE_SCHEMA,
                "status": "complete",
                "complete": True,
                "receipt": state["receipt"],
                "context_token": state.get("context_token"),
                "handle": handle,
                "project_id": state["project_id"],
                "capture_id": state["capture_id"],
                "revision": state["revision"],
                "fact_root": state["fact_root"],
                "document_hash": state["document_hash"],
                "page_count": len(expected),
                "pages_received": sorted(set(state.get("received") or [])),
                "idempotent": True,
            }
        receipt = "pr_" + secrets.token_urlsafe(24)
        state["complete"] = True
        state["receipt"] = receipt
        state["completed_at"] = time.time()
        context = self._mint_context(
            str(state["project_id"]), str(state["capture_id"]),
            state["revision"], str(state["fact_root"]), str(state["document_hash"]),
            kind="full", predecessor=None, delta_id=None,
            view_root=str(state["document_hash"]),
            delta_chain=[],
            audio_evidence=self._capture_audio_evidence(
                str(state["capture_id"]), state["revision"]))
        state["context_token"] = context["token"]
        self._save_handle(handle, state)
        _atomic_write(self.receipts_dir / f"{receipt}.json", canonical_bytes({
            "schema_version": LIFECYCLE_SCHEMA,
            "receipt": receipt,
            "handle": handle,
            "project_id": state["project_id"],
            "capture_id": state["capture_id"],
            "revision": state["revision"],
            "fact_root": state["fact_root"],
            "document_hash": state["document_hash"],
            "view_root": state["document_hash"],
            "delta_chain": [],
            "audio_evidence": context.get("audio_evidence"),
            "context_token": context["token"],
            "page_count": len(expected),
            "complete": True,
            "created_at": time.time(),
        }))
        return {
            "schema_version": LIFECYCLE_SCHEMA,
            "status": "complete",
            "complete": True,
            "receipt": receipt,
            "context_token": context["token"],
            "handle": handle,
            "project_id": state["project_id"],
            "capture_id": state["capture_id"],
            "revision": state["revision"],
            "fact_root": state["fact_root"],
            "document_hash": state["document_hash"],
            "page_count": len(expected),
            "pages_received": sorted(set(state.get("received") or [])),
        }

    def status(self, project_id: str, handle: str | None = None) -> dict[str, Any]:
        latest = self.latest_capture(project_id)
        result: dict[str, Any] = {"project_id": project_id, "latest": None}
        if latest is not None:
            capture_id = str(latest["capture_id"])
            document = self._document(capture_id)
            binding = self._binding(capture_id, document)
            result["latest"] = self._revision(binding, document, capture_id)
        if handle:
            state = self._load_handle(handle)
            delivery = delivery_status(state)
            result["handle"] = {
                "handle": handle,
                "capture_id": state["capture_id"],
                "revision": state["revision"],
                "fact_root": state["fact_root"],
                "document_hash": state["document_hash"],
                "delivered": bool(state.get("complete") and state.get("receipt")),
                "receipt": state.get("receipt"),
                "context_token": state.get("context_token"),
                "view_sha256": delivery.get("view_sha256") or state.get("view_sha256"),
                "delivery": delivery,
            }
        return result

    def refresh(self, project_id: str, *, expected_revision: Mapping[str, Any] | None = None,
                max_response_bytes: int | None = None) -> dict[str, Any]:
        """Re-deliver the current whole view (the documented recovery path)."""
        return self.single(project_id, expected_revision=expected_revision,
                           max_response_bytes=max_response_bytes)

    # -- single-response delivery --------------------------------------------

    def _capture_document(self, project_id: str, capture_id: str | None,
                          expected_revision: Mapping[str, Any] | None
                          ) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]:
        latest = ({"capture_id": capture_id} if capture_id else
                  self.latest_capture(project_id))
        if latest is None:
            raise ValidationError("No capture exists for this project",
                                  details={"project_id": project_id})
        capture = str(latest["capture_id"])
        row = self._index_row(capture)
        if row is None or normalize_project_id(row.get("project_id")) != normalize_project_id(project_id):
            raise ValidationError("Requested capture is not in this project archive",
                                  details={"capture_id": capture})
        document = self._document(capture)
        binding = self._binding(capture, document)
        revision = self._revision(binding, document, capture)
        if expected_revision is not None and not revisions_match(expected_revision, revision):
            raise StaleContext("The requested revision is not the current captured revision",
                               details={"requested": _normalise_revision(expected_revision),
                                        "current": revision})
        return capture, document, binding, revision

    def single(self, project_id: str, *,
               expected_revision: Mapping[str, Any] | None = None,
               capture_id: str | None = None,
               max_response_bytes: int | None = None) -> dict[str, Any]:
        """Deliver the whole project view, its receipt and its token in one result.

        This is the normal path.  The server builds the compact view, measures
        the serialised response, and only mints a receipt when the measured
        response fits the declared budget.  It never pages, never truncates and
        never claims the model read anything: the receipt guarantees delivery of
        one exact view (document hash, revision, fact root), nothing more.

        If the measured response is over budget the call returns
        ``response_over_budget`` with the exact size per section and the
        semantic scopes that would fit.  No handle, receipt or token is minted
        for a view that was not delivered.
        """
        budget = int(max_response_bytes or DEFAULT_RESPONSE_BYTES)
        if budget <= 0:
            raise ValidationError("max_response_bytes must be positive")
        capture, document, _binding, revision = self._capture_document(
            project_id, capture_id, expected_revision)
        record_path = f"captures/{capture}/record.json"
        view = build_view(document, record_path=record_path, max_response_bytes=budget)
        if view_bytes(view) > LEAN_OVERVIEW_BYTES:
            lean_view = build_view(document, record_path=record_path,
                                   max_response_bytes=budget, lean=True)
            if view_bytes(lean_view) < view_bytes(view):
                # Keep all tracks/master and all 24 metric columns; only
                # high-cardinality item/take rows become exact query families.
                view = lean_view
        report = budget_report(view, max_response_bytes=budget)
        document_sha = document_hash(document)
        # Placeholders have exactly the length of the minted strings, so the
        # measurement below is the size of the real response, not an estimate.
        placeholder = "x" * 32
        envelope: dict[str, Any] = {
            "schema_version": LIFECYCLE_SCHEMA,
            "status": "complete",
            "project_id": project_id,
            "capture_id": capture,
            "revision": revision,
            "fact_root": fact_root(document),
            "document_hash": document_sha,
            "delivery": {
                "mode": "single_response",
                "view_schema": view.get("schema_version"),
                "view_sha256": view.get("view_sha256"),
                "view_bytes": 999999,
                "response_bytes": 999999,
                "max_response_bytes": budget,
                "fits": True,
                "pages": False,
                "requires_client_reassembly": False,
                "receipt_guarantee": ("delivered exactly this view (document hash, revision, "
                                      "fact root); not a claim that it was read"),
            },
            "view": view,
            "handle": "pan_" + "x" * 24,
            "receipt": "pr_" + placeholder,
            "context_token": "ct_" + placeholder,
            "audio_evidence": {"state": "current"},
            "next_action": "exact rows: reaper_panorama_query; edits: this context_token",
        }
        measured_response = len(view_canonical(envelope))
        delivery = envelope["delivery"]
        delivery["view_bytes"] = int(report["measured_bytes"])
        delivery["response_bytes"] = measured_response
        # The numbers just written may be shorter than the six-digit placeholders;
        # re-measure so the reported size is the real one and re-check the budget.
        for _ in range(3):
            actual = len(view_canonical(envelope))
            if actual == delivery["response_bytes"]:
                break
            delivery["response_bytes"] = actual
        measured_response = len(view_canonical(envelope))
        if not report["fits"] or measured_response > budget:
            # Before refusing, try a declared minimum core: tracks, all scalar
            # metrics at exact precision, relation counts and the exact-family
            # catalogue.  It is explicitly not the whole view and mints no
            # receipt, token or handle.  If even that does not fit, the honest
            # measured refusal remains the only answer.
            minimum_view = build_minimum_view(
                document, record_path=record_path, max_response_bytes=budget)
            minimum_envelope = _minimum_envelope(
                project_id, capture, revision, document_sha,
                str(envelope["fact_root"]), minimum_view, budget,
                audio_evidence=self._capture_audio_evidence(capture, revision))
            scope = self._new_read_scope(
                project_id, capture, revision, document_sha,
                str(envelope["fact_root"]), budget)
            minimum_envelope["read_scope"] = dict(scope)
            minimum_response = len(view_canonical(minimum_envelope))
            minimum_envelope["delivery"]["response_bytes"] = minimum_response
            minimum_envelope["budget"]["response_bytes"] = minimum_response
            minimum_response = len(view_canonical(minimum_envelope))
            minimum_envelope["delivery"]["response_bytes"] = minimum_response
            minimum_envelope["budget"]["response_bytes"] = minimum_response
            if minimum_response <= budget:
                return minimum_envelope
            scope = self._new_read_scope(
                project_id, capture, revision, document_sha,
                str(envelope["fact_root"]), budget)
            return _over_budget(envelope, report, budget, measured_response,
                                read_scope=scope)
        state = self._new_handle(project_id, capture, revision, document,
                                 int(self.page_bytes), split_pages=False)
        receipt = "pr_" + secrets.token_urlsafe(24)
        context = self._mint_context(
            project_id, capture, revision, str(envelope["fact_root"]), document_sha,
            kind="full", predecessor=None, delta_id=None, view_root=document_sha,
            delta_chain=[],
            audio_evidence=self._capture_audio_evidence(capture, revision))
        state["complete"] = True
        state["receipt"] = receipt
        state["context_token"] = context["token"]
        state["completed_at"] = time.time()
        state["delivery"] = {"mode": "single_response",
                             "bytes": report["measured_bytes"],
                             "view_sha256": view.get("view_sha256")}
        self._save_handle(str(state["handle"]), state)
        _atomic_write(self.receipts_dir / f"{receipt}.json", canonical_bytes({
            "schema_version": LIFECYCLE_SCHEMA,
            "receipt": receipt,
            "handle": state["handle"],
            "project_id": project_id,
            "capture_id": capture,
            "revision": revision,
            "fact_root": envelope["fact_root"],
            "document_hash": document_sha,
            "view_root": document_sha,
            "view_sha256": view.get("view_sha256"),
            "delta_chain": [],
            "audio_evidence": context.get("audio_evidence"),
            "context_token": context["token"],
            "delivery": "single_response",
            "page_count": None,
            "complete": True,
            "created_at": time.time(),
        }))
        envelope["handle"] = state["handle"]
        envelope["receipt"] = receipt
        envelope["context_token"] = context["token"]
        envelope["audio_evidence"] = {"state": (context.get("audio_evidence") or {}).get("state")}
        final_bytes = len(view_canonical(envelope))
        if final_bytes > budget:
            # The measured placeholder envelope was exact; this is a hard guard
            # for an unforeseen longer token, and it still never truncates.
            scope = self._new_read_scope(
                project_id, capture, revision, document_sha,
                str(envelope["fact_root"]), budget)
            return _over_budget(envelope, report, budget, final_bytes,
                                read_scope=scope)
        return envelope

    # -- paged delivery of the model view ------------------------------------

    def open_view(self, project_id: str, *,
                  expected_revision: Mapping[str, Any] | None = None,
                  capture_id: str | None = None,
                  page_target_bytes: int | None = None,
                  max_page_bytes: int | None = None) -> dict[str, Any]:
        """Deliver the model view as pages and page 1's manifest.

        The view is built exactly as the single-response path builds it (no
        lean fallback, no new rounding), then split on whole top-level sections.
        Every page is a valid JSON object with its own header, stable
        ``page_id``, payload hash and the overall ``view_sha256``.  Merging the
        payloads in page order reproduces the view byte-for-byte under
        ``view_canonical``.

        The page count is not capped: N grows until every page's *measured*
        serialised envelope fits ``max_page_bytes``.  A section that cannot fit a
        single page raises instead of being truncated or split silently.
        """
        target = int(page_target_bytes or VIEW_PAGE_TARGET_BYTES)
        ceiling = int(max_page_bytes or VIEW_PAGE_MAX_BYTES)
        if target <= 0 or ceiling <= 0:
            raise ValidationError("page_target_bytes and max_page_bytes must be positive",
                                  details={"page_target_bytes": target,
                                           "max_page_bytes": ceiling})
        if ceiling < 4_000:
            raise ValidationError("max_page_bytes is too small for a page header",
                                  details={"max_page_bytes": ceiling})
        target = min(target, ceiling)
        capture, document, _binding, revision = self._capture_document(
            project_id, capture_id, expected_revision)
        view = build_view(document, record_path=f"captures/{capture}/record.json",
                          lean=False)
        total_bytes = len(view_canonical(view))
        budget = max(2_000, target - _PAGE_HEADER_ALLOWANCE)
        pack_ceiling = ceiling
        for _ in range(16):
            packed = _pack_view_sections(view, payload_budget=budget,
                                         max_page_bytes=pack_ceiling)
            state = self._new_view_pages_state(
                project_id, capture, revision, document, view, packed,
                total_bytes=total_bytes, target=target, max_page_bytes=ceiling)
            envelopes = [self._view_page_envelope(state, index)
                         for index in range(len(state["view_pages"]))]
            sizes = [_delivered_bytes(envelope) for envelope in envelopes]
            worst = max(sizes)
            if worst <= ceiling:
                self._mark_received(state, 0)
                envelope = self._view_page_envelope(state, 0)
                envelope["manifest"] = {
                    "page_count": len(state["view_pages"]),
                    "page_ids": [page["page_id"] for page in state["view_pages"]],
                    "page_hashes": [page["page_hash"] for page in state["view_pages"]],
                    "page_bytes": list(state["page_bytes"]),
                    "response_bytes": sizes,
                    "measurement": "delivered characters (json.dumps indent=2, ensure_ascii=False)",
                    "max_page_bytes": ceiling,
                    "view_bytes": total_bytes,
                    "view_sha256": state["view_sha256"],
                    "schema": view.get("schema_version"),
                    "complete": False,
                    "next_call": {
                        "tool": "reaper_panorama_page",
                        "then": ("fetch every page_id in this order, then call "
                                 "reaper_panorama_ack with every page_hash; only a "
                                 "complete receipt allows analysis or edits"),
                    },
                }
                return envelope
            if budget <= 2_000:
                raise ValidationError(
                    "A panorama page cannot fit the response cap",
                    details={"page_bytes": sizes, "max_page_bytes": ceiling,
                             "pages": len(sizes),
                             "next_action": ("raise max_page_bytes if the host really "
                                             "accepts a larger result; the view is never "
                                             "truncated")})
            pack_ceiling = max(4_000, pack_ceiling - (worst - ceiling) - 256)
            budget = max(2_000, min(budget, pack_ceiling - _PAGE_HEADER_ALLOWANCE))
        raise ValidationError("The panorama could not be packed into pages")

    def _new_view_pages_state(self, project_id: str, capture_id: str,
                              revision: Mapping[str, Any],
                              document: Mapping[str, Any],
                              view: Mapping[str, Any],
                              packed: Sequence[Sequence[tuple[str, Any]]], *,
                              total_bytes: int, target: int,
                              max_page_bytes: int) -> dict[str, Any]:
        handle = "pan_" + secrets.token_urlsafe(18)
        page_count = len(packed)
        pages: list[dict[str, Any]] = []
        section_page: dict[str, str] = {}
        for index, sections in enumerate(packed):
            page_id = f"pg_{index + 1}_of_{page_count}"
            payload = OrderedDict(sections)
            payload_bytes = view_canonical(payload)
            page_hash = sha256_bytes(payload_bytes)
            pages.append({
                "page_id": page_id,
                "page_index": index,
                "sections": [key for key, _value in sections],
                "payload": {key: value for key, value in sections},
                "page_hash": page_hash,
                "page_bytes": len(payload_bytes),
            })
            for key, _value in sections:
                section_page[str(key)] = page_id
        state = {
            "schema_version": LIFECYCLE_SCHEMA,
            "handle": handle,
            "mode": "view_pages",
            "project_id": project_id,
            "capture_id": capture_id,
            "revision": dict(revision),
            "fact_root": fact_root(document),
            "document_hash": document_hash(document),
            "view_schema": view.get("schema_version"),
            "view_sha256": view.get("view_sha256"),
            "view_bytes": total_bytes,
            "page_target_bytes": int(target),
            "max_page_bytes": int(max_page_bytes),
            "page_count": page_count,
            "view_pages": pages,
            "view_section_page": section_page,
            "page_hashes": [page["page_hash"] for page in pages],
            "page_bytes": [page["page_bytes"] for page in pages],
            "received": [],
            "complete": False,
            "receipt": None,
            "context_token": None,
            "created_at": time.time(),
            "expires_at": time.time() + HANDLE_TTL_SECONDS,
        }
        self._save_handle(handle, state)
        return state

    def _view_page_envelope(self, state: Mapping[str, Any],
                            index: int) -> dict[str, Any]:
        pages = list(state["view_pages"])
        if index < 0 or index >= len(pages):
            raise ValidationError("page index is outside the view",
                                  details={"page_index": index,
                                           "page_count": len(pages)})
        page = pages[index]
        received = sorted(set(int(item) for item in state.get("received") or []))
        missing = [pages[i]["page_id"] for i in range(len(pages))
                   if i not in set(received)]
        section_page = dict(state.get("view_section_page") or {})
        where = {name: section_page[name] for name in
                 ("entities", "tracks", "analysis", "relations", "archive")
                 if name in section_page}
        complete = bool(state.get("complete") and state.get("receipt"))
        envelope: dict[str, Any] = {
            "schema_version": LIFECYCLE_SCHEMA,
            "status": "page",
            "project_id": state["project_id"],
            "capture_id": state["capture_id"],
            "revision": state["revision"],
            "fact_root": state["fact_root"],
            "document_hash": state["document_hash"],
            "handle": state["handle"],
            "view_schema": state["view_schema"],
            "view_sha256": state["view_sha256"],
            "page_id": page["page_id"],
            "page_index": index,
            "page_count": len(pages),
            "page_hash": page["page_hash"],
            "page_bytes": page["page_bytes"],
            "sections": list(page["sections"]),
            "where": where,
            "units": ("section-local: analysis.metric_units, units, audio_contract, "
                      "archive.catalog"),
            "payload": page["payload"],
            "delivery": {
                "pages_received": len(received),
                "pages_missing": missing,
                "complete": complete,
                "receipt": state.get("receipt"),
            },
        }
        if complete:
            envelope["delivery"]["context_token"] = state.get("context_token")
        elif index + 1 < len(pages):
            envelope["next"] = {
                "tool": "reaper_panorama_page",
                "handle": state["handle"],
                "revision": state["revision"],
                "page_id": pages[index + 1]["page_id"],
            }
        else:
            envelope["next"] = {
                "tool": "reaper_panorama_ack",
                "handle": state["handle"],
                "revision": state["revision"],
                "page_hashes": "every page_hash in page order",
            }
        return envelope

    def view_page(self, handle: str, revision: Mapping[str, Any],
                  page_id: str) -> dict[str, Any]:
        state = self._load_handle(handle)
        if str(state.get("mode")) != "view_pages":
            raise ValidationError("This handle is not a paged panorama view",
                                  details={"handle": handle})
        if not revisions_match(revision, state["revision"]):
            raise StaleContext("Page request is for another revision",
                               details={"requested": _normalise_revision(revision),
                                        "current": state["revision"]})
        ids = [page["page_id"] for page in state["view_pages"]]
        if page_id not in ids:
            raise ValidationError("Unknown page_id for this panorama",
                                  details={"page_id": page_id, "page_ids": ids})
        index = ids.index(page_id)
        self._mark_received(state, index)
        return self._view_page_envelope(state, index)

    def ack_pages(self, handle: str, revision: Mapping[str, Any],
                  page_hashes: Sequence[str]) -> dict[str, Any]:
        state = self._load_handle(handle)
        if str(state.get("mode")) != "view_pages":
            raise ValidationError("This handle is not a paged panorama view",
                                  details={"handle": handle})
        if not revisions_match(revision, state["revision"]):
            raise StaleContext("Acknowledgement is for another revision",
                               details={"requested": _normalise_revision(revision),
                                        "current": state["revision"]})
        pages = list(state["view_pages"])
        expected = [page["page_hash"] for page in pages]
        supplied = [str(item) for item in page_hashes]
        received = set(int(item) for item in state.get("received") or [])
        missing = [pages[i]["page_id"] for i in range(len(pages)) if i not in received]
        if missing:
            return {
                "schema_version": LIFECYCLE_SCHEMA,
                "status": "incomplete",
                "complete": False,
                "missing_pages": missing,
                "pages_received": [pages[i]["page_id"] for i in sorted(received)],
                "page_count": len(pages),
                "view_sha256": state["view_sha256"],
                "next_call": {"tool": "reaper_panorama_page", "handle": handle,
                              "revision": state["revision"],
                              "page_id": missing[0]},
            }
        if supplied != expected:
            raise StaleContext(
                "Page hashes do not match the served panorama",
                details={"expected_count": len(expected), "received_count": len(supplied),
                         "next_action": "refetch every page and pass their page_hash in order"})
        merged = _merge_view_pages(pages)
        reconstructed = sha256_bytes(view_canonical(
            {key: value for key, value in merged.items() if key != "view_sha256"}))
        if reconstructed != str(state["view_sha256"]):
            raise StaleContext(
                "Pages do not reconstruct the delivered view",
                details={"expected_sha256": state["view_sha256"],
                         "reconstructed_sha256": reconstructed})
        if state.get("complete") and state.get("receipt"):
            return self._ack_view_response(state, idempotent=True)
        receipt = "pr_" + secrets.token_urlsafe(24)
        state["complete"] = True
        state["receipt"] = receipt
        state["completed_at"] = time.time()
        context = self._mint_context(
            str(state["project_id"]), str(state["capture_id"]),
            state["revision"], str(state["fact_root"]), str(state["document_hash"]),
            kind="full", predecessor=None, delta_id=None,
            view_root=str(state["view_sha256"]),
            delta_chain=[],
            audio_evidence=self._capture_audio_evidence(
                str(state["capture_id"]), state["revision"]))
        state["context_token"] = context["token"]
        self._save_handle(handle, state)
        _atomic_write(self.receipts_dir / f"{receipt}.json", canonical_bytes({
            "schema_version": LIFECYCLE_SCHEMA,
            "receipt": receipt,
            "handle": handle,
            "project_id": state["project_id"],
            "capture_id": state["capture_id"],
            "revision": state["revision"],
            "fact_root": state["fact_root"],
            "document_hash": state["document_hash"],
            "view_root": state["view_sha256"],
            "view_sha256": state["view_sha256"],
            "delta_chain": [],
            "audio_evidence": context.get("audio_evidence"),
            "context_token": context["token"],
            "page_count": len(pages),
            "page_ids": [page["page_id"] for page in pages],
            "page_hashes": expected,
            "complete": True,
            "created_at": time.time(),
        }))
        return self._ack_view_response(state, idempotent=False)

    def _ack_view_response(self, state: Mapping[str, Any], *,
                           idempotent: bool) -> dict[str, Any]:
        pages = list(state["view_pages"])
        return {
            "schema_version": LIFECYCLE_SCHEMA,
            "status": "complete",
            "complete": True,
            "receipt": state["receipt"],
            "context_token": state.get("context_token"),
            "handle": state["handle"],
            "project_id": state["project_id"],
            "capture_id": state["capture_id"],
            "revision": state["revision"],
            "fact_root": state["fact_root"],
            "document_hash": state["document_hash"],
            "view_sha256": state["view_sha256"],
            "view_bytes": state["view_bytes"],
            "page_count": len(pages),
            "page_ids": [page["page_id"] for page in pages],
            "reconstructed": True,
            "idempotent": idempotent,
            "receipt_guarantee": ("these exact pages (view_sha256, page hashes) were "
                                  "delivered and acknowledged; not a claim that they "
                                  "were read or understood"),
        }

    def query(self, handle: str, revision: Mapping[str, Any], *,
                  data: Any = None,
                  objects: Any = None,
                  interval: Any = None,
                  series: Sequence[str] | None = None,
                  metrics: Sequence[str] | None = None,
                  kinds: Sequence[str] | None = None,
                  bands_hz: Sequence[float] | None = None,
                  resolution_s: float | None = None,
                  include_pairs: bool = True,
                  max_response_bytes: int | None = None,
                  read_scope: str | None = None) -> dict[str, Any]:
        """Return one coherent semantic fragment of the acknowledged capture.

        The normal path uses a complete handle plus its full-view receipt.  A
        ``read_scope`` issued by an over-budget refusal is accepted for this
        read-only query only; it has no receipt, cannot complete a panorama and
        cannot authorize a mutation.  The fragment is returned whole or as
        ``query_too_large`` with its measured size; it is never paged.
        """
        if read_scope:
            state = self._load_handle(read_scope)
            if state.get("mode") != "read_scope" or "read" not in (
                    state.get("permissions") or []):
                raise StaleContext(
                    "The read-only scope is not usable for exact queries",
                    details={"read_scope": read_scope})
            handle = read_scope
        else:
            state = self._load_handle(handle)
            if not state.get("complete") or not state.get("receipt"):
                raise StaleContext(
                    "Open the panorama before querying exact rows",
                    details={"handle": handle,
                             "next_action": "call reaper_panorama_open"})
        if not revisions_match(revision, state["revision"]):
            raise StaleContext("Query is for another revision",
                               details={"requested": _normalise_revision(revision),
                                        "current": state["revision"]})
        for key in ("capture_id", "fact_root", "document_hash"):
            if revision.get(key) and str(revision[key]) != str(state[key]):
                raise StaleContext(f"Query is for another {key}",
                                   details={"requested": str(revision[key]),
                                            "current": str(state[key])})
        document = self._document(str(state["capture_id"]))
        if document_hash(document) != str(state["document_hash"]):
            raise StaleContext(
                "The captured panorama changed since this handle was opened",
                details={"next_action": "call reaper_panorama_open again"})
        needed = (data if isinstance(data, list)
                  else ([data] if isinstance(data, str) else ["all"]))
        needs_record = ("all" in needed or any(
            family in {"series", "midi_notes", "envelope_points"} for family in needed))
        record = self._record(str(state["capture_id"])) if needs_record else None
        budget = int(max_response_bytes or DEFAULT_RESPONSE_BYTES)
        result = run_query(document, record, data=data, objects=objects, interval=interval,
                           series=series, metrics=metrics, kinds=kinds, bands_hz=bands_hz,
                           resolution_s=resolution_s, include_pairs=include_pairs,
                           max_response_bytes=budget)
        result.update({
            "project_id": state["project_id"],
            "capture_id": state["capture_id"],
            "revision": state["revision"],
            "fact_root": state["fact_root"],
            "document_hash": state["document_hash"],
            "handle": handle,
            "context_token": state.get("context_token"),
            "read_scope": read_scope,
            "read_only": bool(read_scope),
        })
        return result

    def changes_since(self, project_id: str, *,
                      base_capture_id: str | None = None,
                      base_revision: Mapping[str, Any] | None = None,
                      base_fact_root: str | None = None,
                      context_token: str | None = None) -> dict[str, Any]:
        latest = self.latest_capture(project_id)
        if latest is None:
            raise ValidationError("No capture exists for this project",
                                  details={"project_id": project_id})
        context_record: dict[str, Any] | None = None
        if context_token is not None:
            context_record = self._load_context(context_token)
            if str(context_record.get("project_id")) != project_id:
                return {"status": "stale_base", "reason": "wrong_context_project",
                        "project_id": project_id, "context_token": context_token}
            if base_capture_id is None:
                base_capture_id = str(context_record.get("capture_id") or "") or None
            if base_fact_root is None:
                base_fact_root = str(context_record.get("fact_root") or "") or None
        if base_capture_id is None and base_revision is not None:
            base_capture_id = base_revision.get("capture_id")  # type: ignore[assignment]
        if base_capture_id is None and base_revision is not None:
            base_capture_id = self._capture_by_revision(project_id, base_revision)
        if not base_capture_id:
            return {"status": "stale_base", "reason": "unknown_base",
                    "project_id": project_id, "to_capture": latest["capture_id"]}
        base_capture_id = str(base_capture_id)
        base_row = self._index_row(base_capture_id)
        if base_row is None:
            return {"status": "stale_base", "reason": "base_not_in_archive",
                    "project_id": project_id, "base_capture_id": base_capture_id,
                    "to_capture": latest["capture_id"],
                    "next_call": {"tool": "reaper_panorama_refresh",
                                  "project_id": project_id},
                    "note": ("base and target must be in the same capture archive; "
                             "use a base from this archive or refresh in full")}
        if str(base_row.get("project_id")) != project_id:
            return {"status": "stale_base", "reason": "wrong_project",
                    "project_id": project_id}
        base_document = self._document(base_capture_id)
        base_binding = self._binding(base_capture_id, base_document)
        base_revision_value = self._revision(base_binding, base_document, base_capture_id)
        base_root = fact_root(base_document)
        if context_record is not None:
            if (str(context_record.get("capture_id") or "") != base_capture_id or
                    str(context_record.get("fact_root") or "") != base_root):
                return {"status": "stale_base", "reason": "wrong_context_base",
                        "project_id": project_id, "context_token": context_token,
                        "base_capture_id": base_capture_id}
        if base_fact_root is not None and base_fact_root != base_root:
            return {"status": "stale_base", "reason": "root_mismatch",
                    "project_id": project_id, "supplied_root": base_fact_root,
                    "actual_root": base_root}
        target_capture_id = str(latest["capture_id"])
        if target_capture_id == base_capture_id:
            token_revision = (context_record.get("revision")
                              if context_record is not None else None)
            if (context_record is not None and
                    not revisions_match(token_revision, base_revision_value)):
                return {"status": "stale_base",
                        "reason": "context_ahead_of_capture_archive",
                        "project_id": project_id,
                        "base_capture_id": base_capture_id,
                        "context_revision": _normalise_revision(token_revision or {}),
                        "capture_revision": base_revision_value,
                        "next_call": {"tool": "reaper_capture_snapshot",
                                      "project_id": project_id},
                        "note": ("the current context is already ahead of the newest "
                                 "capture, so a capture-to-capture delta cannot describe "
                                 "the acknowledged revision; capture again")}
            return {"status": "not_modified", "project_id": project_id,
                    "revision": base_revision_value, "fact_root": base_root}
        rows = [row for row in self.archive.list_index()
                if normalize_project_id(row.get("project_id")) == normalize_project_id(project_id) and row.get("capture_id")]
        rows.sort(key=lambda row: str(row["capture_id"]))
        ids = [str(row["capture_id"]) for row in rows]
        if base_capture_id not in ids or target_capture_id not in ids:
            return {"status": "stale_base", "reason": "unknown_base",
                    "project_id": project_id, "to_capture": target_capture_id}
        if ids.index(target_capture_id) - ids.index(base_capture_id) > MAX_DELTA_CHAIN:
            return {"status": "refresh_required", "reason": "chain_too_long",
                    "project_id": project_id, "from_capture": base_capture_id,
                    "to_capture": target_capture_id,
                    "next_call": {"tool": "reaper_panorama_refresh",
                                  "project_id": project_id}}
        target_document = self._document(target_capture_id)
        target_binding = self._binding(target_capture_id, target_document)
        target_revision = self._revision(target_binding, target_document, target_capture_id)
        base_identity = identity_from_mapping(base_revision_value)
        target_identity = identity_from_mapping(target_revision)
        if base_identity.gaps() or target_identity.gaps():
            return {"status": "stale_base", "reason": "identity_unknown",
                    "project_id": project_id,
                    "base_identity": base_identity.to_dict(),
                    "target_identity": target_identity.to_dict()}
        if not same_identity_values(base_identity, target_identity):
            return {"status": "stale_base", "reason": "identity_discontinuity",
                    "project_id": project_id,
                    "base_identity": base_identity.to_dict(),
                    "target_identity": target_identity.to_dict(),
                    "note": ("the captures were taken in different REAPER instances or "
                             "project generations; continuity cannot be inferred from the "
                             "path or change counter")}
        base_view = stable_fact_view(base_document)
        target_view = stable_fact_view(target_document)
        operations = _diff_values(base_view, target_view)
        if len(operations) > MAX_DELTA_OPS:
            return {"status": "refresh_required", "reason": "delta_too_large",
                    "project_id": project_id, "from_capture": base_capture_id,
                    "to_capture": target_capture_id,
                    "next_call": {"tool": "reaper_panorama_refresh",
                                  "project_id": project_id}}
        render_comparison = compare_render_manifests(
            base_document.get("render_manifest") or {},
            target_document.get("render_manifest") or {})
        if render_comparison.get("authoritative"):
            invalidated = sorted(set(render_comparison.get("changed") or []) |
                                 set(render_comparison.get("removed") or []))
            invalidations = [{
                "kind": "audio_and_analysis",
                "scope": "named_sources",
                "sources": invalidated,
                "reason": "render_dependency_changed" if invalidated else "unchanged_render_keys",
                "component_changes": render_comparison.get("component_changes") or {},
                "complete": True,
                "requires_recapture": bool(invalidated),
                "note": ("known render keys prove which captured nodes survived while no "
                         "unresolved dependency class is declared; old audio remains a "
                         "historical capture fact"),
            }]
        elif render_comparison.get("unverified"):
            invalidations = [{
                "kind": "audio_and_analysis",
                "scope": "unknown",
                "sources": None,
                "reason": "unverified_cross_capture_dependency",
                "unverified": list(render_comparison.get("unverified") or []),
                "complete": False,
                "requires_recapture": True,
                "note": ("the target capture is fresh evidence for its own revision, but "
                         "unverified dependency classes prevent an authoritative "
                         "unchanged verdict between captures"),
            }]
        else:
            invalidations = _invalidations(operations, target_document)
        delta_payload = {
            "status": "delta",
            "project_id": project_id,
            "from_capture": base_capture_id,
            "to_capture": target_capture_id,
            "from_revision": base_revision_value,
            "to_revision": target_revision,
            "from_root": base_root,
            "to_root": fact_root(target_document),
            "document_hash": document_hash(target_document),
            "ops": operations,
            "unchanged": _domain_counts({
                key: value for key, value in target_view.items()
                if key in base_view and base_view[key] == value
            }),
            "invalidations": invalidations,
            "render": {
                "scope": render_comparison.get("scope"),
                "authoritative": bool(render_comparison.get("authoritative")),
                "changed": render_comparison.get("changed"),
                "unchanged": render_comparison.get("unchanged"),
                "added": render_comparison.get("added"),
                "removed": render_comparison.get("removed"),
                "reasons": render_comparison.get("reasons"),
                "unverified": list(render_comparison.get("unverified") or []),
                "freshness": (render_comparison.get("freshness")
                              or ("known" if render_comparison.get("authoritative")
                                  else "unknown")),
            },
            "attribution": "capture",
            "compare": {"project": "same", "verdict": "comparable"},
        }
        if context_record is None:
            return {**delta_payload, "continuity": False,
                    "note": ("delta is informational; pass a context_token to make it "
                             "advanceable and usable for the mutation gate")}
        delta_hash = sha256_bytes(canonical_bytes(delta_payload))
        delta_id = "dl_" + secrets.token_urlsafe(24)
        _atomic_write(self.deltas_dir / f"{delta_id}.json", canonical_bytes({
            "schema_version": LIFECYCLE_SCHEMA,
            "delta_id": delta_id,
            "delta_hash": delta_hash,
            "base_context_token": context_token,
            "project_id": project_id,
            "base_capture_id": base_capture_id,
            "base_revision": base_revision_value,
            "base_root": base_root,
            "target_capture_id": target_capture_id,
            "target_revision": target_revision,
            "target_root": fact_root(target_document),
            "target_document_hash": document_hash(target_document),
            "created_at": time.time(),
            "acked": False,
            "successor_token": None,
        }))
        result = {
            **delta_payload,
            "continuity": True,
            "context_token": context_token,
            "delta_id": delta_id,
            "delta_hash": delta_hash,
            "next_call": {"tool": "reaper_panorama_status",
                          "delta_id": delta_id, "delta_hash": delta_hash,
                          "context_token": context_token},
        }
        # The delta and its successor token are delivered in the same response:
        # the server already knows it served this exact delta hash on this base,
        # so no separate acknowledgement round trip is required to continue.
        # The guarantee is delivery of that exact delta, not that it was read.
        if context_token:
            try:
                advanced = self.delta_ack(delta_id, delta_hash, context_token)
            except (StaleContext, ValidationError) as exc:
                result["advance"] = {"advanced": False, "reason": str(exc),
                                     "next_call": result["next_call"]}
            else:
                result["advance"] = {
                    "advanced": True,
                    "predecessor_token": context_token,
                    "context_token": advanced.get("context_token"),
                    "revision": advanced.get("revision"),
                    "view_root": advanced.get("view_root"),
                    "audio_evidence": advanced.get("audio_evidence"),
                    "guarantee": ("this exact delta (delta_hash) was delivered with this "
                                  "response on this base; the successor token advances to "
                                  "the target revision without a second round trip"),
                }
                result["context_token"] = advanced.get("context_token")
                result["next_call"] = {
                    "tool": "reaper_panorama_status",
                    "optional": True,
                    "note": ("only needed to re-acknowledge the same delta "
                             "idempotently; the successor token is already in "
                             "advance.context_token"),
                }
        return result

    def _load_change(self, delta_id: str) -> dict[str, Any]:
        path = self.deltas_dir / f"{delta_id}.json"
        if not path.is_file():
            raise StaleContext("Change record is unknown or expired; request it again",
                               details={"delta_id": delta_id})
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise StaleContext("Change record is corrupt", details={"delta_id": delta_id})
        payload = record.get("payload")
        if (isinstance(payload, dict) and
                _delta_hash(payload) != str(record.get("delta_hash") or "")):
            raise StaleContext("Change record payload no longer matches its hash",
                               details={"delta_id": delta_id})
        return record

    def _resolve_context_token(self, receipt: str) -> str:
        """Accept a receipt or a context token and return an existing token id."""
        if isinstance(receipt, str) and receipt.startswith("ct_"):
            return receipt
        if not isinstance(receipt, str) or not receipt.startswith("pr_"):
            raise StaleContext("A context token is required to build a mutation delta")
        path = self.receipts_dir / f"{receipt}.json"
        if not path.is_file():
            raise StaleContext("Panorama receipt is unknown or expired",
                               details={"receipt": receipt})
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise StaleContext("Panorama receipt is corrupt")
        token = data.get("context_token")
        if isinstance(token, str) and token.startswith("ct_"):
            self._load_context(token)
            return token
        # Legacy receipt without a token: mint the equivalent capture token
        # from its own proven binding and remember it for later calls.
        minted = self._mint_context(
            str(data.get("project_id")), str(data.get("capture_id")),
            data.get("revision") or {}, str(data.get("fact_root")),
            str(data.get("document_hash")), kind="full",
            view_root=str(data.get("document_hash")), delta_chain=[],
            audio_evidence=self._capture_audio_evidence(
                str(data.get("capture_id")), data.get("revision") or {}))
        data["context_token"] = minted["token"]
        _atomic_write(path, canonical_bytes(data))
        return minted["token"]

    def record_change(self, *, kind: str, context_token: str, project_id: str,
                      base_revision: Mapping[str, Any],
                      target_revision: Mapping[str, Any],
                      after_digest: Mapping[str, Any] | None,
                      scope: str,
                      operation_facts: Sequence[Mapping[str, Any]] | None,
                      readback_complete: bool,
                      readback_missing: Sequence[str] = (),
                      target_digest_evidence: Mapping[str, Any] | None = None
                      ) -> dict[str, Any]:
        """Build, store and return one factual delta for a completed mutation.

        The base is the exact acknowledged token, not "whatever capture is
        latest": a delta that cannot be tied to the model's current context is
        refused rather than guessed.
        """
        resolved_token = self._resolve_context_token(context_token)
        token = self._load_context(resolved_token)
        self._require_context_identity(token, project_id)
        if str(token.get("project_id")) != project_id:
            raise StaleContext("The context token belongs to another project",
                               details={"token_project": token.get("project_id"),
                                        "project_id": project_id})
        token_revision = token.get("revision")
        if not isinstance(token_revision, Mapping):
            raise StaleContext("The context token carries no revision")
        if not (revisions_match(base_revision, token_revision) and
                revisions_match(token_revision, base_revision)):
            raise StaleContext("The mutation did not start from the acknowledged context",
                               details={"acknowledged": _normalise_revision(token_revision),
                                        "reported_base": _normalise_revision(base_revision)})
        base_identity = identity_from_mapping(base_revision)
        target_identity = identity_from_mapping(target_revision)
        if (base_identity.gaps() or target_identity.gaps() or
                not same_identity_values(base_identity, target_identity)):
            raise StaleContext(
                "The mutation crossed a project identity boundary; no delta can "
                "advance the old context",
                details={"base_identity": base_identity.to_dict(),
                         "target_identity": target_identity.to_dict(),
                         "next_action": "capture again and acknowledge the new panorama"})
        capture_id = str(token.get("capture_id") or "")
        base_document = self._document(capture_id)
        facts = base_document.get("structural_facts")
        if not isinstance(facts, Mapping) or facts.get("available") is not True:
            readback_complete = False
            readback_missing = list(readback_missing) + ["base_structural_facts_absent"]
            before_facts: Mapping[str, Any] = digest_facts(None)
        else:
            before_facts = facts
        base = {
            "context_token": resolved_token,
            "capture_id": capture_id,
            "revision": _normalise_revision(token_revision),
            "fact_root": str(token.get("fact_root") or ""),
            "view_root": str(token.get("view_root") or token.get("document_hash") or ""),
            "document_hash": str(token.get("document_hash") or ""),
            "delta_chain_length": len(token.get("delta_chain") or []),
        }
        target = {
            "revision": _normalise_revision(target_revision),
            "digest_fingerprint": (target_digest_evidence or {}).get("fingerprint"),
            "digest_scope": (target_digest_evidence or {}).get("scope"),
        }
        payload = build_structural_delta(
            kind=kind, project_id=project_id, base=base, target=target,
            base_document=base_document, before_facts=before_facts,
            after_facts=digest_facts(after_digest), after_digest=after_digest,
            scope=scope, operation_facts=operation_facts,
            readback_complete=readback_complete,
            readback_missing=readback_missing,
            target_digest_evidence=target_digest_evidence,
        )
        payload_hash = _delta_hash(payload)
        delta_id = "chg_" + secrets.token_urlsafe(24)
        record = {
            "schema_version": DELTA_SCHEMA,
            "delta_id": delta_id,
            "delta_hash": payload_hash,
            "kind": kind,
            "project_id": project_id,
            "base": dict(payload["base"]),
            "target": dict(payload["target"]),
            "payload": payload,
            "created_at": time.time(),
            "acked": False,
            "successor_token": None,
        }
        _atomic_write(self.deltas_dir / f"{delta_id}.json", canonical_bytes(record))
        response = {
            **payload,
            "delta_id": delta_id,
            "delta_hash": payload_hash,
            "continuity": True,
            "context_token": resolved_token,
            "acknowledgeable": bool(payload.get("acknowledgeable")),
        }
        if payload.get("acknowledgeable"):
            response["next_call"] = {
                "tool": "reaper_panorama_status",
                "delta_id": delta_id,
                "delta_hash": payload_hash,
                "context_token": resolved_token,
                "then": ("Structural edits may continue against the advanced token. "
                         "Project-audio interpretation requires a fresh capture."),
            }
            # Deliver the successor with the delta itself: the delta was served in
            # this response, so a second round trip would only re-transmit the
            # same hash.  The guarantee is delivery, not comprehension.
            try:
                advanced = self.delta_ack(delta_id, payload_hash, resolved_token)
            except (StaleContext, ValidationError) as exc:
                response["advance"] = {"advanced": False, "reason": str(exc)}
            else:
                response["advance"] = {
                    "advanced": True,
                    "predecessor_token": resolved_token,
                    "context_token": advanced.get("context_token"),
                    "revision": advanced.get("revision"),
                    "view_root": advanced.get("view_root"),
                    "audio_evidence": advanced.get("audio_evidence"),
                    "guarantee": ("this exact mutation delta was delivered with this "
                                  "response; the successor token is already valid for "
                                  "structural edits"),
                }
                response["context_token"] = advanced.get("context_token")
                response["next_call"]["optional"] = True
        else:
            response["refresh_required"] = True
            response["next_call"] = {
                "tool": "reaper_capture_snapshot",
                "project_id": project_id,
                "then": ("The readback was partial; no context advancement is possible. "
                         "Capture again and acknowledge the new panorama."),
            }
        return response

    def delta_ack(self, delta_id: str, delta_hash: str,
                  context_token: str) -> dict[str, Any]:
        """Verify a served delta and mint the successor continuity token.

        Two delta kinds share this entry point: a capture-to-capture delta
        produced by ``changes_since`` and a mutation delta produced by
        ``record_change``.  A capture delta hands over a complete new document,
        so its successor token is a fresh capture view.  A mutation delta
        proves one fact set and chains onto the model's current view; it can
        advance structural edits, and its ``audio_evidence`` says which audio
        claims did not survive.
        """
        path = self.deltas_dir / f"{delta_id}.json"
        if not path.is_file():
            raise StaleContext("Delta is unknown or expired; request it again",
                               details={"delta_id": delta_id})
        context_token = self._resolve_context_token(context_token)
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise StaleContext("Delta is corrupt; refresh")
        if str(record.get("delta_hash")) != str(delta_hash):
            raise StaleContext("Delta hash does not match the served delta",
                               details={"delta_id": delta_id})
        payload = record.get("payload")
        if isinstance(payload, Mapping) and _delta_hash(payload) != str(record.get("delta_hash")):
            raise StaleContext("Delta payload no longer matches its stored hash",
                               details={"delta_id": delta_id})
        is_change = isinstance(record.get("payload"), Mapping)
        base_token = (str((record.get("base") or {}).get("context_token"))
                      if is_change else str(record.get("base_context_token")))
        if base_token != str(context_token):
            raise StaleContext("Delta does not belong to this context token",
                               details={"delta_id": delta_id})
        if record.get("acked") and record.get("successor_token"):
            try:
                existing = self._load_context(str(record["successor_token"]))
                return {
                    "schema_version": LIFECYCLE_SCHEMA,
                    "status": "complete",
                    "complete": True,
                    "idempotent": True,
                    "token": existing["token"],
                    "context_token": existing["token"],
                    "kind": existing["kind"],
                    "revision": existing["revision"],
                    "fact_root": existing["fact_root"],
                    "document_hash": existing["document_hash"],
                    "view_root": existing.get("view_root"),
                    "audio_evidence": existing.get("audio_evidence"),
                    "delta_id": delta_id,
                    "delta_chain_length": len(existing.get("delta_chain") or []),
                }
            except StaleContext:
                pass
        context = self._load_context(context_token)
        if str(context.get("project_id")) != str(record.get("project_id")):
            raise StaleContext("Delta context belongs to another project")
        if is_change:
            payload = record.get("payload")
            if not isinstance(payload, Mapping) or not payload.get("acknowledgeable"):
                raise StaleContext(
                    "This delta is not acknowledgeable; its readback was partial",
                    details={"delta_id": delta_id,
                             "readback": payload.get("readback") if isinstance(payload, Mapping)
                             else None,
                             "next_action": ("capture again and acknowledge the new "
                                             "panorama; do not reuse a partial delta")})
            raw_base = record.get("base")
            base: Mapping[str, Any] = raw_base if isinstance(raw_base, Mapping) else {}
            raw_target = record.get("target")
            target: Mapping[str, Any] = raw_target if isinstance(raw_target, Mapping) else {}
            if str(base.get("view_root")) != str(context.get("view_root")
                                                 or context.get("document_hash")):
                raise StaleContext("Delta was built from another structural view",
                                   details={"delta_id": delta_id})
            if not (revisions_match(context.get("revision"), base.get("revision")) and
                    revisions_match(base.get("revision"), context.get("revision"))):
                raise StaleContext("Delta was built from another acknowledged revision",
                                   details={"delta_id": delta_id})
            chain = list(context.get("delta_chain") or [])
            chain.append({"delta_id": delta_id, "delta_hash": delta_hash,
                          "revision": target.get("revision")})
            successor = self._mint_context(
                str(record["project_id"]), str(context.get("capture_id")),
                target.get("revision") or {}, str(context.get("fact_root") or ""),
                str(context.get("document_hash") or ""), kind="delta",
                predecessor=context_token, delta_id=delta_id,
                view_root=next_view_root(str(base.get("view_root")), str(delta_hash)),
                delta_chain=chain,
                audio_evidence=payload.get("audio_evidence"))
        else:
            target_capture_id = str(record["target_capture_id"])
            target_document = self._document(target_capture_id)
            if document_hash(target_document) != str(record.get("target_document_hash")):
                raise StaleContext("Capture delta target document no longer matches its hash",
                                   details={"delta_id": delta_id,
                                            "capture_id": target_capture_id})
            if fact_root(target_document) != str(record.get("target_root")):
                raise StaleContext("Capture delta target fact root no longer matches",
                                   details={"delta_id": delta_id,
                                            "capture_id": target_capture_id})
            successor = self._mint_context(
                str(record["project_id"]), str(record["target_capture_id"]),
                record["target_revision"], str(record["target_root"]),
                str(record["target_document_hash"]), kind="delta",
                predecessor=context_token, delta_id=delta_id,
                view_root=str(record["target_document_hash"]), delta_chain=[],
                audio_evidence=self._capture_audio_evidence(
                    str(record["target_capture_id"]), record["target_revision"]))
        record["acked"] = True
        record["successor_token"] = successor["token"]
        record["acked_at"] = time.time()
        _atomic_write(path, canonical_bytes(record))
        return {
            "schema_version": LIFECYCLE_SCHEMA,
            "status": "complete",
            "complete": True,
            "idempotent": False,
            "token": successor["token"],
            "context_token": successor["token"],
            "kind": successor["kind"],
            "predecessor": context_token,
            "delta_id": delta_id,
            "revision": successor["revision"],
            "fact_root": successor["fact_root"],
            "document_hash": successor["document_hash"],
            "view_root": successor.get("view_root"),
            "delta_chain_length": len(successor.get("delta_chain") or []),
            "audio_evidence": successor.get("audio_evidence"),
            "capture_id": successor["capture_id"],
        }

    def _interval_relations(self, record: FullCaptureRecord,
                            document: Mapping[str, Any],
                            state: Mapping[str, Any],
                            start: float, end: float) -> dict[str, Any]:
        """Recompute band-dominance evidence for one interval of this capture.

        Only the captured per-source band-level series are used.  Source-master,
        phase and reference relations are not silently carried over: they need
        data or a stage that a band series cannot provide, so they are emitted as
        explicit unavailable coverage for this interval.
        """

        project_map = record.project_map if isinstance(record.project_map, Mapping) else {}
        master = project_map.get("master") if isinstance(project_map, Mapping) else {}
        master_guid = str((master or {}).get("guid") or "")
        master_id = f"master:{master_guid or 'unknown'}"
        pan_by_guid = {
            str(track.get("guid")): track.get("pan")
            for track in (document.get("tracks") or [])
            if isinstance(track, Mapping) and track.get("guid")
        }
        source_payloads: dict[str, Any] = {}
        for guid, report in (record.analysis or {}).items():
            if str(guid) == master_guid or not isinstance(report, Mapping):
                continue
            curves = report.get("curves")
            if not isinstance(curves, Mapping):
                source_payloads[str(guid)] = {"bands": []}
                continue
            source_payloads[str(guid)] = {
                "bands": curves.get("band_level_time_dbfs"),
                "times": curves.get("spectrogram_time_s"),
                "band_centers_hz": curves.get("band_centers_hz"),
                "pan": pan_by_guid.get(str(guid)),
            }
        interval_batch = band_dominance_interval(
            source_payloads, start_s=start, end_s=end
        )
        batch = merge_batches(
            interval_batch,
            RelationBatch(
                unavailable={
                    "track_master_level_relation":
                        "not_recomputed_for_interval: source-master RMS is whole-window",
                    "phase_pair":
                        "not_recomputed_for_interval: phase requires waveform comparison",
                    "reference_comparison":
                        "not_recomputed_for_interval: no aligned reference stage supplied",
                }
            ),
        )
        source_assets: dict[str, str] = {}
        for asset in record.audio or []:
            if not isinstance(asset, Mapping):
                continue
            guid = str(asset.get("source_guid") or "")
            if guid:
                source_assets[guid] = str(asset.get("sha256") or asset.get("asset_id") or guid)
        if master_guid:
            source_assets[master_id] = source_assets.get(master_guid, master_id)
        sample_rate = next(
            (int(asset["sample_rate"]) for asset in record.audio or []
             if isinstance(asset, Mapping) and isinstance(asset.get("sample_rate"), int)),
            None,
        )
        revision = record.revision_before if isinstance(record.revision_before, Mapping) else {}
        binding = RelationBinding(
            capture_id=record.capture_id,
            project_id=record.project_id,
            revision_fingerprint=sha256_bytes(canonical_bytes(revision)),
            fact_root=str(state.get("fact_root") or ""),
            window=RelationWindow(start_s=start, end_s=end,
                                  sample_rate_hz=sample_rate),
            stage="post_fx",
            node="capture",
            channel_view="stereo",
            render_key=None,
            producer="reaper-lifecycle.interval-relations",
        )
        relation_set = build_relation_set(
            binding,
            batch,
            source_assets=source_assets,
            known_ids=sorted(set(source_payloads) | ({master_id} if master_guid else set())),
            master_id=master_id if master_guid else None,
        )
        domain = relation_set.to_dict()
        full_hash = domain.pop("content_hash", None)
        table = (domain.get("evaluations") or {}).get("band_dominance_pairs")
        if isinstance(table, Mapping) and isinstance(table.get("rows"), list):
            columns = list(table.get("columns") or [])
            status_index = columns.index("status") if "status" in columns else 2
            rows = [list(row) for row in table["rows"]]
            counts: dict[str, int] = {}
            for row in rows:
                status = str(row[status_index]) if status_index < len(row) else "unknown"
                counts[status] = counts.get(status, 0) + 1
            kept = [row for row in rows
                    if (str(row[status_index]) if status_index < len(row)
                        else "unknown") != "no_dominant_band"]
            domain["evaluations"] = {
                "band_dominance_pairs": {
                    "columns": columns,
                    "rows": kept,
                    "expected_pairs": table.get("expected_pairs"),
                    "evaluated_pairs": table.get("evaluated_pairs"),
                    "relations_emitted": table.get("relations_emitted"),
                    "status_counts": counts,
                    "rows_included": len(kept),
                    "rows_omitted_no_dominant": len(rows) - len(kept),
                    "temporal_model": table.get("temporal_model"),
                    "interval": table.get("interval"),
                    "compact_interval_view": True,
                    "canonical": False,
                }
            }
        domain["scope"] = "interval"
        domain["interval"] = {"start_s": start, "end_s": end}
        domain["canonical_full_content_hash"] = full_hash
        domain["interval_view"] = {
            "scope": "interval",
            "start_s": start,
            "end_s": end,
            "evaluations_complete": False,
            "rows_omitted_no_dominant": (
                (domain.get("evaluations") or {}).get("band_dominance_pairs") or {}
            ).get("rows_omitted_no_dominant", 0),
            "note": (
                "interval-local band dominance; no whole-capture aggregate is "
                "reused. Source-master, phase and reference relations are "
                "unavailable for this interval with explicit reasons."
            ),
        }
        domain["relations_summary"] = {
            "schema_version": domain.get("schema_version"),
            "scope": "interval",
            "complete": bool(domain.get("complete")),
            "coverage": domain.get("coverage"),
            "unavailable": domain.get("unavailable"),
            "counts": {
                "relations": len(domain.get("relations") or []),
                "pairs": (
                    (domain.get("evaluations") or {}).get("band_dominance_pairs") or {}
                ).get("evaluated_pairs", 0),
            },
            "canonical_full_content_hash": full_hash,
        }
        return domain

    def slice(self, handle: str, revision: Mapping[str, Any], *,
              window_start_s: float, window_end_s: float) -> dict[str, Any]:
        state = self._load_handle(handle)
        if not state.get("complete") or not state.get("receipt"):
            raise StaleContext(
                "Acknowledge every panorama page before slicing project evidence",
                details={"handle": handle,
                         "next_action": ("fetch every page with reaper_panorama_page, "
                                         "then call reaper_panorama_ack")})
        if not revisions_match(revision, state["revision"]):
            raise StaleContext("Slice request is for another revision",
                               details={"requested": _normalise_revision(revision),
                                        "current": state["revision"]})
        if revision.get("capture_id") and str(revision["capture_id"]) != str(state["capture_id"]):
            raise StaleContext("Slice request is for another capture",
                               details={"requested_capture": revision.get("capture_id"),
                                        "current_capture": state["capture_id"]})
        if revision.get("fact_root") and str(revision["fact_root"]) != str(state["fact_root"]):
            raise StaleContext("Slice request is for another fact root",
                               details={"requested_root": revision.get("fact_root"),
                                        "current_root": state["fact_root"]})
        if (revision.get("document_hash")
                and str(revision["document_hash"]) != str(state["document_hash"])):
            raise StaleContext("Slice request is for another panorama document",
                               details={"requested_document": revision.get("document_hash"),
                                        "current_document": state["document_hash"]})
        document = self._document(str(state["capture_id"]))
        start = float(window_start_s)
        end = float(window_end_s)
        if not start < end:
            raise ValidationError("window_start_s must be less than window_end_s")
        try:
            record = self._record(str(state["capture_id"]))
            interval_relations = self._interval_relations(
                record, document, state, start, end
            )
        except (ValidationError, OSError, TypeError, ValueError) as exc:
            interval_relations = {
                "scope": "interval",
                "complete": False,
                "interval": {"start_s": start, "end_s": end},
                "unavailable": {"band_dominance": "capture_record_unavailable"},
                "error": str(exc),
                "note": (
                    "The capture has no readable canonical record for interval "
                    "recomputation; no whole-capture relation is substituted."
                ),
            }
        structural_keys = (
            "project", "tracks", "master", "items", "takes", "midi", "fx",
            "routes", "automation", "envelopes", "time_map", "time_signatures",
            "tempo", "markers", "regions", "selection", "transport", "per_track",
            "crosslinks", "coverage", "truncation", "analysis_metric_units",
            "analysis_contract", "audio_time_contract", "relations_summary",
        )
        structural = {key: document.get(key) for key in structural_keys if key in document}
        evidence_items = []
        for item in document.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            interval = item.get("interval")
            if isinstance(interval, Mapping):
                item_start = interval.get("start_s")
                item_end = interval.get("end_s")
            else:
                item_start = item.get("position_s")
                item_end = (float(item_start) + float(item.get("length_s") or 0)
                            if item_start is not None else None)
            if item_start is None or item_end is None:
                continue
            if float(item_end) > start and float(item_start) < end:
                evidence_items.append(item)
        temporal_evidence: dict[str, Any] | None = None
        temporal = document.get("analysis_temporal")
        if isinstance(temporal, Mapping):
            buckets = []
            for bucket in temporal.get("buckets") or []:
                if not isinstance(bucket, Mapping):
                    continue
                row_start = bucket.get("start_s")
                row_end = bucket.get("end_s")
                if row_start is None or row_end is None:
                    continue
                if float(row_end) > start and float(row_start) < end:
                    buckets.append(bucket)
            if buckets:
                temporal_evidence = {
                    "representation": temporal.get("representation"),
                    "units": temporal.get("units"),
                    "aggregation": temporal.get("aggregation"),
                    "bucket_width_s": temporal.get("bucket_width_s"),
                    "sources": temporal.get("sources"),
                    "buckets": buckets,
                }
        details = document.get("details")
        details_summary = None
        if isinstance(details, Mapping):
            details_summary = {
                "schema": details.get("schema"),
                "fetch_tool": details.get("fetch_tool"),
                "counts": details.get("counts"),
                "interval": details.get("interval"),
                "request_schema": details.get("request_schema"),
                "note": ("the full entries live in the panorama pages; this slice "
                         "repeats only the manifest summary"),
            }
        result = {
            "schema_version": LIFECYCLE_SCHEMA,
            "status": "slice",
            "project_id": state["project_id"],
            "capture_id": state["capture_id"],
            "revision": state["revision"],
            "fact_root": state["fact_root"],
            "document_hash": state["document_hash"],
            "state": {"scope": "project", **structural,
                      "details_manifest": details_summary},
            "evidence": {
                "scope": {"kind": "interval", "start_s": start, "end_s": end},
                "items": evidence_items,
                "temporal": temporal_evidence,
                "relations": interval_relations,
                "relations_scope": "interval",
            },
            "coverage": {
                "not_in_scope": [
                    "whole-project relations_summary remains project-scoped",
                    "whole-project relation measurements are not repeated in the interval slice",
                    "whole-song measurement tables are not repeated; use panorama_page for the full document",
                    "whole-song audio evidence remains in the capture archive",
                ],
                "window": {"start_s": start, "end_s": end},
            },
        }
        encoded = canonical_bytes(result)
        if len(encoded) > 48_000:
            return {
                "status": "slice_too_large",
                "bytes": len(encoded),
                "project_id": state["project_id"],
                "revision": state["revision"],
                "next_call": {"tool": "reaper_panorama_page", "handle": handle,
                              "revision": state["revision"], "page_index": 0},
            }
        return result

    def detail(self, handle: str, revision: Mapping[str, Any], *,
               requests: Sequence[Mapping[str, Any]],
               max_bytes: int = DEFAULT_DETAIL_BYTES) -> dict[str, Any]:
        """Return exact linked rows for a batch of typed detail requests.

        The same completion receipt, revision, capture and document hash that
        unlock ``slice`` are required here.  Each request is checked against the
        panorama's detail manifest and every returned payload is hashed against
        the manifest entry, so a stale or mismatched reference fails closed.
        """
        state = self._load_handle(handle)
        if not state.get("complete") or not state.get("receipt"):
            raise StaleContext(
                "Acknowledge every panorama page before fetching linked detail",
                details={"handle": handle,
                         "next_action": ("fetch every page with reaper_panorama_page, "
                                         "then call reaper_panorama_ack")})
        if not revisions_match(revision, state["revision"]):
            raise StaleContext("Detail request is for another revision",
                               details={"requested": _normalise_revision(revision),
                                        "current": state["revision"]})
        for key in ("capture_id", "fact_root", "document_hash"):
            if revision.get(key) and str(revision[key]) != str(state[key]):
                raise StaleContext(f"Detail request is for another {key}",
                                   details={"requested": str(revision[key]),
                                            "current": str(state[key])})
        document = self._document(str(state["capture_id"]))
        if document_hash(document) != str(state["document_hash"]):
            raise StaleContext(
                "The captured panorama changed since this handle was opened",
                details={"next_action": "call reaper_panorama_refresh"})
        record = self._record(str(state["capture_id"]))
        result = resolve_details(record, document, requests, max_bytes=max_bytes)
        if result.get("status") == "detail_too_large":
            result.update({"project_id": state["project_id"],
                           "capture_id": state["capture_id"],
                           "revision": state["revision"],
                           "handle": handle})
            return result
        return {
            "schema_version": result.get("schema_version"),
            "status": "detail",
            "project_id": state["project_id"],
            "capture_id": state["capture_id"],
            "revision": state["revision"],
            "fact_root": state["fact_root"],
            "document_hash": state["document_hash"],
            "state": {
                "scope": "project",
                "relations_summary": document.get("relations_summary"),
                "coverage": document.get("coverage"),
            },
            "evidence": result,
            "coverage": {
                "not_in_scope": [
                    "the linked rows are exact stored values for this capture window",
                    "they are not re-measured and do not extend beyond the capture window",
                ],
            },
        }

    # -- mutation gate -------------------------------------------------------

    def _require_context_identity(self, data: Mapping[str, Any], project_id: str,
                                  *, require_complete: bool = True) -> None:
        identity = identity_from_mapping(data.get("revision"))
        gaps = identity.gaps()
        if require_complete and gaps:
            raise StaleContext(
                "Project identity is incomplete; continuity cannot be proven",
                details={"project_id": project_id, "gaps": list(gaps),
                         "next_action": ("refresh the panorama from a bridge that reports "
                                         "instance_id and epoch")})
        if identity.project_id != project_id:
            raise StaleContext("Panorama token belongs to another project",
                               details={"token_project": identity.project_id,
                                        "project_id": project_id})

    def _verify_delta_chain(self, data: Mapping[str, Any]) -> None:
        """Recompute the view root from the capture hash plus every stored delta.

        A token that claims an advanced view must prove the whole chain.  Each
        link must exist, hash to what the link names, start from the previous
        view root and carry the same project identity.  Any gap is a stale
        context, never a best-effort view.
        """
        chain = data.get("delta_chain")
        if not isinstance(chain, list) or not chain:
            # A full capture token may carry a view root that differs from the
            # capture document hash: the view is a projection, so its sheet
            # hash is not the document hash.  Anchor the token on the capture
            # document instead of comparing two different kinds of root.
            capture_id = str(data.get("capture_id") or "")
            if capture_id and self._index_row(capture_id) is not None:
                if str(data.get("document_hash") or "") != document_hash(
                        self._document(capture_id)):
                    raise StaleContext("Panorama view root does not match its capture")
            return
        # The chain is rooted in one immutable capture.  Recompute its document
        # hash instead of trusting the token's copy: a replaced or truncated
        # base document must break the chain, not silently become the base.
        capture_id = str(data.get("capture_id") or "")
        if not capture_id or self._index_row(capture_id) is None:
            raise StaleContext("Delta context base capture is not in the archive",
                               details={"capture_id": capture_id})
        base_document = self._document(capture_id)
        if str(data.get("document_hash") or "") != document_hash(base_document):
            raise StaleContext("Delta context base document no longer matches its hash",
                               details={"capture_id": capture_id})
        first = chain[0] if isinstance(chain[0], Mapping) else {}
        first_id = str(first.get("delta_id") or "")
        first_record = self._load_change(first_id) if first_id else {}
        first_base = first_record.get("base")
        anchor = first_base if isinstance(first_base, Mapping) else {}
        if str(anchor.get("document_hash") or "") != str(data.get("document_hash")):
            raise StaleContext("Panorama delta chain starts from another capture",
                               details={"delta_id": first_id})
        current = str(anchor.get("view_root") or data.get("document_hash"))
        previous_revision: Mapping[str, Any] | None = None
        for entry in chain:
            if not isinstance(entry, Mapping):
                raise StaleContext("Panorama delta chain is corrupt")
            delta_id = str(entry.get("delta_id") or "")
            named_hash = str(entry.get("delta_hash") or "")
            record = self._load_change(delta_id)
            if str(record.get("delta_hash")) != named_hash:
                raise StaleContext("Panorama delta chain hash does not match the stored delta",
                                   details={"delta_id": delta_id})
            if str(record.get("project_id")) != str(data.get("project_id")):
                raise StaleContext("Panorama delta chain crosses projects",
                                   details={"delta_id": delta_id})
            raw_base = record.get("base")
            base: Mapping[str, Any] = raw_base if isinstance(raw_base, Mapping) else {}
            if str(base.get("view_root")) != current:
                raise StaleContext("Panorama delta chain is not continuous",
                                   details={"delta_id": delta_id})
            if previous_revision is not None and not revisions_match(
                    previous_revision, base.get("revision")):
                raise StaleContext("Panorama delta chain revisions do not line up",
                                   details={"delta_id": delta_id})
            raw_target = record.get("target")
            target: Mapping[str, Any] = raw_target if isinstance(raw_target, Mapping) else {}
            entry_revision = entry.get("revision")
            if isinstance(entry_revision, Mapping) and not revisions_match(
                    entry_revision, target.get("revision")):
                raise StaleContext("Panorama delta chain target revision drifted",
                                   details={"delta_id": delta_id})
            current = next_view_root(current, named_hash)
            previous_revision = target.get("revision") if isinstance(
                target.get("revision"), Mapping) else None
        if current != str(data.get("view_root")):
            raise StaleContext("Panorama view root does not match its verified delta chain",
                               details={"token_root": data.get("view_root"),
                                        "recomputed_root": current})

    def _require_audio_fresh(self, data: Mapping[str, Any]) -> None:
        evidence = data.get("audio_evidence")
        evidence = evidence if isinstance(evidence, Mapping) else {}
        state = str(evidence.get("state") or "unknown")
        if state == "current":
            return
        raise StaleContext(
            "Project audio evidence is stale for this revision; capture before interpreting it",
            details={
                "audio_state": state,
                "base_capture_id": evidence.get("base_capture_id") or data.get("capture_id"),
                "invalidated_sources": list(evidence.get("invalidated_sources") or []),
                "fresh_sources": list(evidence.get("fresh_sources") or []),
                "reasons": list(evidence.get("reasons") or []),
                "next_action": ("call reaper_capture_snapshot, then reaper_panorama_open; "
                                "the view and its receipt arrive in one result"),
                "gate": "audio_evidence_fresh",
            },
        )

    def require_receipt(self, receipt: str, project_id: str,
                        expected_revision: Mapping[str, Any] | None,
                        expected_fact_root: str | None = None,
                        *,
                        expected_capture_id: str | None = None,
                        require_audio_fresh: bool = False) -> dict[str, Any]:
        def _check_root(value: Mapping[str, Any], *, is_delta: bool) -> None:
            if expected_capture_id is not None:
                if is_delta:
                    raise StaleContext(
                        "A delta-advanced context cannot authorize an exact capture read",
                        details={"capture_id": expected_capture_id,
                                 "next_action": ("open and acknowledge the exact capture you "
                                                 "want to read")})
                capture_id = str(expected_capture_id)
                row = self._index_row(capture_id)
                if row is None:
                    raise ValidationError(
                        "Capture is not in the archive",
                        details={"capture_id": capture_id},
                    )
                if str(row.get("project_id") or "") != project_id:
                    raise StaleContext(
                        "Capture belongs to another project",
                        details={"capture_project": row.get("project_id"),
                                 "project_id": project_id},
                    )
                document = self._document(capture_id)
                current_root = fact_root(document)
                current_hash = document_hash(document)
                if str(value.get("capture_id")) != capture_id:
                    raise StaleContext(
                        "Panorama receipt is for another capture",
                        details={"receipt_capture": value.get("capture_id"),
                                 "current_capture": capture_id},
                    )
                if str(value.get("fact_root")) != current_root:
                    raise StaleContext(
                        "Panorama receipt is for another fact root",
                        details={"receipt_root": value.get("fact_root"),
                                 "current_root": current_root},
                    )
                if str(value.get("document_hash") or "") != current_hash:
                    raise StaleContext(
                        "Panorama receipt is for another panorama document",
                        details={"receipt_document": value.get("document_hash"),
                                 "current_document": current_hash},
                    )
                if expected_fact_root is not None and expected_fact_root != current_root:
                    raise StaleContext(
                        "Expected fact root does not match the captured root",
                        details={"expected_root": expected_fact_root,
                                 "current_root": current_root},
                    )
                return
            if is_delta:
                # A delta view is anchored to its base capture, not to the
                # latest capture (there may be none for this revision).
                base_capture_id = str(value.get("capture_id") or "")
                if not base_capture_id or self._index_row(base_capture_id) is None:
                    raise StaleContext("Delta context base capture is not in the archive",
                                       details={"capture_id": base_capture_id})
                document = self._document(base_capture_id)
                current_root = fact_root(document)
                if str(value.get("fact_root")) != current_root:
                    raise StaleContext(
                        "Delta context was built from another base fact root",
                        details={"token_root": value.get("fact_root"),
                                 "current_root": current_root})
                if expected_fact_root is not None and expected_fact_root != current_root:
                    raise StaleContext(
                        "Expected fact root does not match the base capture root",
                        details={"expected_root": expected_fact_root,
                                 "current_root": current_root})
                return
            latest = self.latest_capture(project_id)
            if latest is None:
                raise StaleContext("No capture exists for this project",
                                   details={"project_id": project_id})
            capture_id = str(latest["capture_id"])
            document = self._document(capture_id)
            current_root = fact_root(document)
            if str(value.get("capture_id")) != capture_id:
                raise StaleContext(
                    "Panorama receipt is for another capture",
                    details={"receipt_capture": value.get("capture_id"),
                             "current_capture": capture_id},
                )
            if str(value.get("fact_root")) != current_root:
                raise StaleContext(
                    "Panorama receipt is for another fact root",
                    details={"receipt_root": value.get("fact_root"),
                             "current_root": current_root},
                )
            if expected_fact_root is not None and expected_fact_root != current_root:
                raise StaleContext(
                    "Expected fact root does not match the current captured root",
                    details={"expected_root": expected_fact_root,
                             "current_root": current_root},
                )

        if isinstance(receipt, str) and receipt.startswith("ct_"):
            data = self._load_context(receipt)
            self._require_context_identity(
                data, project_id, require_complete=expected_capture_id is None)
            if expected_revision is not None and not revisions_match(expected_revision,
                                                                     data.get("revision")):
                raise StaleContext("Panorama context token does not match the expected revision",
                                   details={"token_revision": data.get("revision"),
                                            "expected_revision": _normalise_revision(expected_revision)})
            is_delta = str(data.get("kind")) == "delta"
            if is_delta:
                if expected_capture_id is not None:
                    raise StaleContext(
                        "A delta-advanced context cannot authorize an exact capture read",
                        details={"capture_id": expected_capture_id,
                                 "next_action": ("open and acknowledge the exact capture you "
                                                 "want to read")})
                self._verify_delta_chain(data)
            else:
                _check_root(data, is_delta=False)
            if require_audio_fresh:
                self._require_audio_fresh(data)
            return {**data, "complete": True, "receipt": receipt,
                    "require_audio_fresh": require_audio_fresh}
        if not isinstance(receipt, str) or not receipt.startswith("pr_"):
            raise ValidationError("A complete panorama receipt or context token is required before mutation",
                                  details={"next_action": ("call reaper_panorama_open; it returns the whole "
                                                           "view with its receipt and context token in one "
                                                           "result. After a delta use advance.context_token")})
        path = self.receipts_dir / f"{receipt}.json"
        if not path.is_file():
            raise StaleContext("Panorama receipt is unknown or expired; refresh the panorama",
                               details={"receipt": receipt})
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise StaleContext("Panorama receipt is corrupt; refresh the panorama")
        if time.time() - float(data.get("created_at", 0)) > RECEIPT_TTL_SECONDS:
            raise StaleContext("Panorama receipt has expired; refresh the panorama",
                               details={"receipt": receipt})
        if str(data.get("project_id")) != project_id:
            raise StaleContext("Panorama receipt belongs to another project",
                               details={"receipt_project": data.get("project_id"),
                                        "project_id": project_id})
        self._require_context_identity(
            data, project_id, require_complete=expected_capture_id is None)
        if expected_revision is not None and not revisions_match(expected_revision,
                                                                 data.get("revision")):
            raise StaleContext("Panorama receipt does not match the expected revision",
                               details={"receipt_revision": data.get("revision"),
                                        "expected_revision": _normalise_revision(expected_revision)})
        _check_root(data, is_delta=False)
        if require_audio_fresh:
            # A capture receipt is current by construction; the check exists so
            # an old receipt whose evidence was later marked stale cannot slip
            # through the same call path.
            evidence = data.get("audio_evidence")
            if isinstance(evidence, Mapping) and str(evidence.get("state")) != "current":
                self._require_audio_fresh(data)
        return data


def _minimum_envelope(project_id: str, capture: str, revision: Mapping[str, Any],
                      document_sha: str, fact_root_value: str,
                      view: Mapping[str, Any], budget: int,
                      audio_evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A declared core response: useful, non-complete, and deliberately receipt-free."""
    measured_view = view_bytes(view)
    envelope: dict[str, Any] = {
        "schema_version": LIFECYCLE_SCHEMA,
        "status": "minimum",
        "project_id": project_id,
        "capture_id": capture,
        "revision": dict(revision),
        "fact_root": fact_root_value,
        "document_hash": document_sha,
        "delivery": {
            "mode": "minimum",
            "view_schema": view.get("schema_version"),
            "view_sha256": view.get("view_sha256"),
            "view_bytes": measured_view,
            "response_bytes": 0,
            "max_response_bytes": budget,
            "fits": True,
            "pages": False,
            "requires_client_reassembly": False,
            "receipt_guarantee": (
                "delivered this declared minimum core; it is not the whole-project view "
                "and mints no receipt, context token or handle"),
        },
        "view": dict(view),
        "audio_evidence": dict(audio_evidence) if audio_evidence is not None else {"state": "current"},
        "next_action": (
            "all scalar metrics are exact in this minimum; catalogued item/take/FX/routing/"
            "audio/series families use reaper_panorama_query with read_scope and return "
            "whole scopes or measured refusals"),
        "budget": {
            "max_response_bytes": budget,
            "view_bytes": measured_view,
            "response_bytes": 0,
        },
        "no_hidden_fallback": (
            "minimum_core is a declared, non-complete projection; no receipt, context token "
            "or handle was minted; no page or cursor exists"),
    }
    # Re-measure until the reported size is the actual canonical size.  The
    # response has no token placeholders, so this normally converges at once.
    for _ in range(3):
        actual = len(view_canonical(envelope))
        if envelope["delivery"]["response_bytes"] == actual and envelope["budget"]["response_bytes"] == actual:
            break
        envelope["delivery"]["response_bytes"] = actual
        envelope["budget"]["response_bytes"] = actual
    return envelope


def _over_budget(envelope: Mapping[str, Any], report: Mapping[str, Any],
                 budget: int, measured_with_tokens: int,
                 read_scope: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Honest refusal: no view, no receipt, exact sizes and scopes.

    A view that cannot be delivered in one response is not delivered at all.
    Nothing is truncated, paged or replaced by a summary, and no receipt is
    minted for content the model did not receive.
    """
    result = {key: value for key, value in envelope.items() if key != "view"}
    # Nothing was minted: drop the delivery placeholders so no caller can mistake
    # them for a usable receipt or handle.
    for key in ("receipt", "context_token", "handle"):
        result.pop(key, None)
    result["status"] = "response_over_budget"
    delivery = dict(result.get("delivery") or {})
    delivery.update({
        "fits": False,
        "response_bytes": measured_with_tokens,
        "reason": "single_response_over_budget",
        "pages": False,
        "requires_client_reassembly": False,
    })
    result["delivery"] = delivery
    if read_scope is not None:
        result["read_scope"] = dict(read_scope)
    result["budget"] = {"sections_bytes": report.get("sections_bytes"),
                        "exact_detail_tool": report.get("exact_detail_tool")}
    view = envelope.get("view") or {}
    archive = view.get("archive") or {}
    catalog = archive.get("catalog") or {}
    catalog_rows = [row for row in (catalog.get("rows") or [])
                    if isinstance(row, (list, tuple))]
    result["over_limit"] = {
        "measured_bytes": measured_with_tokens,
        "view_bytes": report.get("measured_bytes"),
        "max_response_bytes": budget,
        "over_by_bytes": measured_with_tokens - budget,
        "sections_bytes": report.get("sections_bytes"),
        "host_basis": (
            "the budget is the size of one standard MCP tool result: below the Hermes "
            "50,000-char inline/spill threshold and inside the Codex ~10,000-token output "
            "budget at ~4 bytes/token. A host with a different, known cap passes "
            "max_response_bytes explicitly."),
        "what_is_blocking": (
            "this project's structure plus every measured family serialises larger than "
            "one tool result. The limit is the host's per-result budget, not missing "
            "data: the archive holds everything."),
        "families_bytes": {str(row[0]): row[3] for row in catalog_rows},
        "read_scope": dict(read_scope) if read_scope is not None else None,
        "options": [
            {"option": "raise the budget",
             "call": "reaper_panorama_open(max_response_bytes=<your host's real cap>)",
             "note": "only if the host really accepts a result that large"},
            {"option": "read-only exact query with the issued read_scope",
             "call": ("reaper_panorama_query(handle=\"\", revision=<revision>, "
                      "read_scope=<read_scope.id>, data=[...], objects=[...], interval={...})"),
             "note": ("one coherent fragment per call; read-only, not a full-view receipt, "
                      "not mutation authorization, no pages and no cursor")},
            {"option": "capture a shorter section",
             "call": "reaper_capture_snapshot(start_s=..., end_s=...)",
             "note": ("fewer measurement cells per source; the project structure itself "
                      "does not shrink")},
        ],
        "no_hidden_fallback": (
            "nothing was truncated, paged, summarised or delivered outside this response; "
            "no full-view handle, receipt or context token was minted; only the read-only "
            "query scope above was issued"),
    }
    return result


__all__ = [
    "LIFECYCLE_SCHEMA",
    "DEFAULT_PAGE_BYTES",
    "MAX_PAGE_BYTES",
    "PanoramaLifecycle",
    "canonical_bytes",
    "document_hash",
    "fact_root",
    "stable_fact_view",
    "revisions_match",
]
