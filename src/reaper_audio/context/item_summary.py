"""Compact per-track representation of captured media items and takes.

The canonical capture record keeps every item and take row at full field
precision.  A model-facing map does not need 315 near-identical rows to answer
"what is on this track and where"; it needs a per-track inventory plus a stable
address for the exact row when it does.  This module builds that inventory from
the exact rows and recognises the already-summarised shape on read.

Two representations exist and both are read:

* ``per_track_summary`` -- the current shape: one row per track with counts,
  item time extrema, short names, stable item ids, take names and the active
  take relation;
* a plain list of full item/take rows -- every sidecar written before this
  shape.  The reader projects it into the summary at read time and never
  rewrites the archive.

No value is invented: a missing start/end is excluded from the extrema and the
`timed_count` column says how many rows actually contributed; a name is only
inlined when it is present and short, and the exact row remains one query away.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, Sequence
from typing import Any

ITEM_SUMMARY_SCHEMA = "reaper-workbench.track-item-summary/1"
ITEM_SUMMARY_REPRESENTATION = "per_track_summary"

#: Only short names are inlined; longer names stay in the exact row.  This is a
#: presentation bound, never a claim that the item/take has no name.
INLINE_NAME_CHARS = 48
#: A pathological single track must not make the map unbounded.  Beyond this
#: many item ids/names the section states how many ids were omitted; query by
#: track id still returns every exact item row.
MAX_INLINE_ITEM_IDS = 512
MAX_INLINE_NAMES = MAX_INLINE_ITEM_IDS

ITEM_COLUMNS = ("track_id", "items_count", "timed_count", "start_min_s",
                "start_max_s", "end_min_s", "end_max_s", "item_names",
                "item_ids", "item_ids_omitted")
TAKE_COLUMNS = ("track_id", "takes_count", "take_names", "active_takes")

ITEM_NOTE = (
    "one row per track/master; start_*_s/end_*_s are the extrema over the "
    "timed_count items that carry an interval (each item's half-open "
    "[start_s,end_s)); item_names is positional with item_ids (null = no short "
    "captured name; the exact name stays in the query row); item_ids are the "
    "stable 'item:<guid>' addresses; names and ids share a 512-per-track inline "
    "cap and item_ids_omitted counts the rest. Exact item rows are not duplicated "
    "here: reaper_panorama_query data=['items'] with objects=[track id or item id] "
    "returns them whole.")
TAKE_NOTE = (
    "one row per track/master; take_names is positional with the captured "
    "active takes (null = no short captured name; the exact name stays in the "
    "query row) and is cut at the same 512-per-track inline cap as item names; "
    "takes_count states the total. Capture stores the active take only; "
    "coverage.domains.takes states active_take_only and the exact expected "
    "count. active_takes='all' means take row i is the active take of item row "
    "i; otherwise it is a list of [item_position, take_name_or_null]. Exact "
    "take rows: reaper_panorama_query data=['takes'] objects=[track/item/take id].")


def _name(value: Any) -> str | None:
    if isinstance(value, str) and value and len(value) <= INLINE_NAME_CHARS:
        return value
    return None


def _bounds(row: Mapping[str, Any]) -> tuple[float | None, float | None]:
    interval = row.get("interval")
    if isinstance(interval, Mapping):
        start = interval.get("start_s")
        end = interval.get("end_s")
    else:
        start = row.get("position_s")
        end = None
    try:
        start = float(start) if start is not None else None
    except (TypeError, ValueError):
        start = None
    if end is None and start is not None:
        try:
            length = row.get("length_s")
            if length is not None:
                end = start + float(length)
        except (TypeError, ValueError):
            end = None
    try:
        end = float(end) if end is not None else None
    except (TypeError, ValueError):
        end = None
    return start, end


def _buckets(track_ids: Sequence[str]) -> "OrderedDict[str, dict[str, Any]]":
    return OrderedDict((str(track_id), {
        "items": [], "starts": [], "ends": [], "names": [],
        "takes": [], "take_names": [],
    }) for track_id in track_ids)


def item_summary(items: Sequence[Mapping[str, Any]],
                 track_ids: Sequence[str],
                 *, record_path: str | None = None) -> dict[str, Any]:
    """Project full item rows into one summary row per track."""
    buckets = _buckets(track_ids)
    for row in items:
        if not isinstance(row, Mapping):
            continue
        key = str(row.get("track_id") or "")
        bucket = buckets.setdefault(key, {
            "items": [], "starts": [], "ends": [], "names": [],
            "takes": [], "take_names": []})
        bucket["items"].append(row)
        start, end = _bounds(row)
        if start is not None and end is not None:
            bucket["starts"].append(start)
            bucket["ends"].append(end)
        # Positional, so a null says "no short captured name", not "renamed".
        bucket["names"].append(_name(row.get("name")))
    rows = []
    for track_id, bucket in buckets.items():
        ids = [str(row.get("item_id")) for row in bucket["items"] if row.get("item_id")]
        omitted = max(0, len(ids) - MAX_INLINE_ITEM_IDS)
        starts, ends = bucket["starts"], bucket["ends"]
        names = bucket["names"][:MAX_INLINE_NAMES]
        rows.append([
            track_id,
            len(bucket["items"]),
            len(starts),
            round(min(starts), 6) if starts else None,
            round(max(starts), 6) if starts else None,
            round(min(ends), 6) if ends else None,
            round(max(ends), 6) if ends else None,
            names if any(name is not None for name in names) else None,
            ids[:MAX_INLINE_ITEM_IDS] or None,
            omitted or None,
        ])
    return {
        "schema": ITEM_SUMMARY_SCHEMA,
        "representation": ITEM_SUMMARY_REPRESENTATION,
        "count": len([row for row in items if isinstance(row, Mapping)]),
        "columns": list(ITEM_COLUMNS),
        "rows": rows,
        "detail_index": {
            "canonical_record": record_path,
            "items": {"record_pointer": "#/project_map/items",
                      "id_field": "guid", "stable_id": "item:<guid>"},
            "query": {
                "tool": "reaper_panorama_query",
                "data": ["items"],
                "objects": ["<track stable id>", "<item stable id>", "<item guid>"],
                "interval": "optional {start_s,end_s}",
                "note": ("exact rows come from the canonical record binding of "
                         "this capture, never from a re-derived project state"),
            },
        },
        "note": ITEM_NOTE,
    }


def take_summary(items: Sequence[Mapping[str, Any]],
                 takes: Sequence[Mapping[str, Any]],
                 track_ids: Sequence[str],
                 *, record_path: str | None = None) -> dict[str, Any]:
    """Project full take rows into one summary row per track."""
    buckets = _buckets(track_ids)
    for row in items:
        if not isinstance(row, Mapping):
            continue
        key = str(row.get("track_id") or "")
        bucket = buckets.setdefault(key, {
            "items": [], "starts": [], "ends": [], "names": [],
            "takes": [], "take_names": [], "item_ids": []})
        bucket["items"].append(row)
    positions: dict[str, dict[str, int]] = {}
    for track_id, bucket in buckets.items():
        positions[track_id] = {
            str(row.get("item_id")): index
            for index, row in enumerate(bucket["items"]) if row.get("item_id")}
    # Item order per track is the anchor for the active-take alignment claim.
    for track_id, bucket in buckets.items():
        bucket["item_ids"] = [str(row.get("item_id")) for row in bucket["items"]
                              if row.get("item_id")]
    for row in takes:
        if not isinstance(row, Mapping):
            continue
        key = str(row.get("track_id") or "")
        bucket = buckets.setdefault(key, {
            "items": [], "starts": [], "ends": [], "names": [],
            "takes": [], "take_names": [], "item_ids": []})
        bucket["takes"].append(row)
        bucket["take_names"].append(_name(row.get("name")))
    rows = []
    for track_id, bucket in buckets.items():
        item_ids = bucket.get("item_ids") or []
        take_rows = bucket["takes"]
        # Alignment: take row i is the active take of item row i, for every item.
        aligned = False
        if item_ids and len(take_rows) == len(item_ids):
            order = [positions.get(track_id, {}).get(str(row.get("item_id")))
                     for row in take_rows]
            aligned = order == list(range(len(item_ids)))
        if aligned:
            active: Any = "all"
        else:
            by_position: dict[int, str | None] = {}
            for row in take_rows:
                position = positions.get(track_id, {}).get(str(row.get("item_id")))
                if position is None:
                    continue
                by_position[position] = _name(row.get("name"))
            active = [[position, by_position.get(position)]
                      for position in range(len(item_ids))] or None
        rows.append([
            track_id,
            len(take_rows),
            (bucket["take_names"][:MAX_INLINE_NAMES]
             if any(name is not None for name in bucket["take_names"]) else None),
            active,
        ])
    return {
        "schema": ITEM_SUMMARY_SCHEMA,
        "representation": ITEM_SUMMARY_REPRESENTATION,
        "count": len([row for row in takes if isinstance(row, Mapping)]),
        "columns": list(TAKE_COLUMNS),
        "rows": rows,
        "detail_index": {
            "canonical_record": record_path,
            "takes": {"record_pointer": "#/project_map/items/*/active_take",
                      "id_field": "guid", "stable_id": "take:<guid>"},
            "query": {
                "tool": "reaper_panorama_query",
                "data": ["takes"],
                "objects": ["<track stable id>", "<item stable id>", "<take stable id>"],
                "note": ("only the active take of each item is captured; "
                         "coverage.domains.takes states the expected total"),
            },
        },
        "note": TAKE_NOTE,
    }


def is_summary(value: Any) -> bool:
    return (isinstance(value, Mapping)
            and value.get("representation") == ITEM_SUMMARY_REPRESENTATION
            and isinstance(value.get("columns"), (list, tuple))
            and isinstance(value.get("rows"), (list, tuple)))


def exact_rows(value: Any) -> list[Mapping[str, Any]] | None:
    """The full row list when the section is the legacy list shape."""
    if isinstance(value, (list, tuple)):
        return [row for row in value if isinstance(row, Mapping)]
    return None


def summary_count(value: Any) -> int | None:
    if is_summary(value):
        count = value.get("count")
        if isinstance(count, int) and not isinstance(count, bool):
            return count
    rows = exact_rows(value)
    return len(rows) if rows is not None else None


def summary_entries(value: Any) -> list[dict[str, Any]] | None:
    """Rows of a summary section as dictionaries (columns -> value)."""
    if not is_summary(value):
        return None
    columns = [str(name) for name in value.get("columns") or []]
    entries = []
    for row in value.get("rows") or []:
        if isinstance(row, (list, tuple)) and len(row) == len(columns):
            entries.append(dict(zip(columns, row)))
    return entries


__all__ = [
    "INLINE_NAME_CHARS",
    "ITEM_COLUMNS",
    "ITEM_NOTE",
    "ITEM_SUMMARY_REPRESENTATION",
    "ITEM_SUMMARY_SCHEMA",
    "MAX_INLINE_ITEM_IDS",
    "TAKE_COLUMNS",
    "TAKE_NOTE",
    "exact_rows",
    "is_summary",
    "item_summary",
    "summary_count",
    "summary_entries",
    "take_summary",
]
