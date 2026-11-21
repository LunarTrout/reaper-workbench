"""One agent-facing project context: map, state health, facts and pointers.

A model turn used to need three separate answers before it could act: the
project map (tracks, FX, routing, tempo), the current state (which measurements
exist, which are stale) and the panorama summary.  Read together they are large
and partly redundant, and read separately they can describe different revisions.

This module builds the single object that replaces those reads.  It is pure
shaping: every value comes from the supplied :class:`ProjectState` document, the
optional project map and the optional FX summary.  There is no bridge call, no
filesystem access and no clock in this module, so the same inputs always produce
the same output.

Sections (fixed order):

``project``
    ``project_id``, ``path``, ``revision`` and the state ``state_token``.  Path
    and revision come from the map when it carries them; they are ``None``
    rather than invented when it does not.
``map``
    A compact projection of the project map: per-track ``guid``/``name``/
    ``fx_names``/``send_count``/``item_summary`` (counts and time extrema only),
    master FX names, routes, tempo, markers and regions.  Full item and take
    rows are never copied into the context; the compact item summary is an
    inventory, and exact rows stay behind the panorama query pointer.
``health``
    Fact counts by status, the sorted sources with a stale fact, and explicit
    ``present`` flags for the media and runtime blocks when the map carries
    them.  A missing block is ``{"present": false}``, never a clean result.
``facts``
    One row per logical fact (``id``, ``source_id``, ``stage``, ``window``,
    ``metrics_available``, ``status``), capped by ``max_facts`` with ``total``
    and a ``next`` pointer for the rest.  ``detail="summary"`` never includes
    measured values; ``detail="full"`` adds ``values`` for the returned rows.
``fx``
    The caller's plugin-parameter summary, passed through unchanged inside a
    ``present``/``count``/``rows`` envelope.
``changes``
    A delta from ``state.query.changes_since`` when ``since_token`` is given,
    otherwise the most recent entries of the document's change log (bounded by
    :data:`RECENT_CHANGES_SHOWN`).
``audio``
    ``source_id -> {sha256, stage}`` references only, from an ``audio`` or
    ``audio_refs`` collection on the map document.  No asset payloads.
``next``
    Static detail pointers: ``state.query`` for a deeper fact read,
    ``fx.parameters`` for plugin values and ``since=state_token`` for a delta.

Size policy: a real capture view is tens of kilobytes, so the context is a
summary with pointers, not a capture dump.  :func:`estimated_tokens` reports
``ceil(UTF-8 bytes / 2.2)`` -- dividing by 2.2 rather than the usual ~4
deliberately over-counts, so a caller budgeting by it does not under-estimate.
:func:`project_context_pages` packs the top-level sections greedily, in the
fixed order above, into deterministic pages of at most ``page_bytes`` bytes
whenever a section fits in one page; a section is never split.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import ValidationError
from .state_query import availability, changes_since, query_facts

CONTEXT_SCHEMA = "reaper-workbench.project-context/1"

#: Section order of the context and of every page's section list.
SECTIONS: tuple[str, ...] = (
    "project", "map", "health", "facts", "fx", "changes", "audio", "next",
)

DETAIL_LEVELS: tuple[str, ...] = ("summary", "full")

#: Conservative bytes-per-token divisor: real text is closer to 4 bytes per
#: token, so dividing by 2.2 over-counts rather than under-counts.
TOKEN_BYTES_ESTIMATE = 2.2

#: Default page budget.  Below the 48,000-byte panorama policy default and the
#: 50,000-character host inline threshold, leaving room for the envelope.
DEFAULT_PAGE_BYTES = 46_000

#: Presentation bound for the no-token change log read.  This is not a project
#: threshold: it only stops the raw log from crowding the context, and the
#: omitted count says exactly how many entries were left out.
RECENT_CHANGES_SHOWN = 20

#: Static detail pointers carried by every context.
NEXT_POINTER: dict[str, str] = {
    "detail": "state.query",
    "parameters": "fx.parameters",
    "delta": "since=state_token",
}


# --------------------------------------------------------------------------- #
# scalar and JSON helpers
# --------------------------------------------------------------------------- #


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _first_number(row: Mapping[str, Any], *names: str) -> float | None:
    for name in names:
        number = _number(row.get(name))
        if number is not None:
            return number
    return None


def _json_safe(value: object) -> Any:
    """Deterministic JSON-shaped copy with sorted keys and no NaN/Inf.

    A non-finite float becomes ``None`` (it was read but is not encodable); an
    object that is not JSON-shaped is described by type instead of stringified,
    so two different objects cannot collapse into the same text.
    """
    if isinstance(value, Mapping):
        return {str(key): _json_safe(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"$bytes": bytes(value).hex()}
    return {"$type": type(value).__name__, "$repr": repr(value)}


def _encoded(value: object) -> bytes:
    """Canonical UTF-8 JSON for a JSON-safe value."""
    return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #


def _document(state: object) -> dict[str, Any] | None:
    """The project state document from a ``ProjectState`` or a mapping.

    ``None`` means no state was loaded and is preserved as absence; anything
    else that is not a state mapping is a caller error, not an empty state.
    """
    if state is None:
        return None
    if isinstance(state, Mapping):
        return dict(state)
    inner = getattr(state, "state", None)
    if isinstance(inner, Mapping):
        return dict(inner)
    raise ValidationError(
        "state must be a project state mapping, a ProjectState or None",
        details={"type": type(state).__name__},
    )


def _token(document: Mapping[str, Any] | None) -> int | None:
    if document is None:
        return None
    token = document.get("token")
    if isinstance(token, bool) or not isinstance(token, int) or token < 0:
        return None
    return token


def _mapping_arg(value: object, name: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError(f"{name} must be a mapping or None",
                              details={name: type(value).__name__})
    return value


def _map_object(container: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The map itself, unwrapping a full-capture record when one was passed.

    ``build_project_context`` documents ``map_document`` as the project map.  A
    capture record carries the same map under ``project_map`` plus the audio
    list at the top level, so accepting that envelope keeps the audio
    references usable without a second argument.  A ``project_map`` key of the
    wrong type is a malformed envelope, not an empty map.
    """
    if container is None:
        return None
    nested = container.get("project_map")
    if nested is None:
        return container
    if not isinstance(nested, Mapping):
        raise ValidationError("map_document.project_map must be a mapping")
    return nested


# --------------------------------------------------------------------------- #
# project
# --------------------------------------------------------------------------- #


def _project_section(document: Mapping[str, Any] | None,
                     container: Mapping[str, Any] | None) -> dict[str, Any]:
    nested = container.get("project") if container is not None else None
    nested = nested if isinstance(nested, Mapping) else {}
    project_id = None
    if document is not None:
        project_id = _text(document.get("project_id"))
    if project_id is None and container is not None:
        project_id = (_text(container.get("project_id"))
                      or _text(nested.get("project_id")))
    path = None
    if container is not None:
        path = (_text(container.get("path"))
                or _text(container.get("project_path"))
                or _text(nested.get("path")))
    revision = None
    if container is not None:
        revision = container.get("revision")
        if revision is None:
            revision = nested.get("revision")
    return {
        "project_id": project_id,
        "path": path,
        "revision": _json_safe(revision) if revision is not None else None,
        "state_token": _token(document),
    }


# --------------------------------------------------------------------------- #
# map
# --------------------------------------------------------------------------- #


def _fx_names(row: Mapping[str, Any]) -> list[str | None] | None:
    names = row.get("fx_names")
    if isinstance(names, list):
        return [name if isinstance(name, str) else None for name in names]
    fx = row.get("fx")
    if isinstance(fx, list):
        return [(_text(item.get("name")) if isinstance(item, Mapping) else None)
                for item in fx]
    return None


def _send_count(row: Mapping[str, Any]) -> int | None:
    mixer = row.get("mixer")
    for holder in (row, mixer if isinstance(mixer, Mapping) else {}):
        count = _integer(holder.get("send_count"))
        if count is not None and count >= 0:
            return count
    sends = row.get("sends")
    if isinstance(sends, list):
        return len(sends)
    count = _integer(sends)
    return count if count is not None and count >= 0 else None


def _bounds(item: Mapping[str, Any]) -> tuple[float | None, float | None]:
    interval = item.get("interval")
    if isinstance(interval, Mapping):
        start = _first_number(interval, "start_s", "start")
        end = _first_number(interval, "end_s", "end")
    else:
        start = _first_number(item, "position_s", "start_s")
        end = _first_number(item, "end_s")
    if start is not None and end is None:
        length = _number(item.get("length_s"))
        if length is not None:
            end = start + length
    return start, end


def _item_summary(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Counts and time extrema only -- never item ids, names or full rows."""
    summary = row.get("item_summary")
    if isinstance(summary, Mapping):
        return {
            "items_count": _integer(summary.get("items_count",
                                                summary.get("count"))),
            "timed_count": _integer(summary.get("timed_count")),
            "start_min_s": _number(summary.get("start_min_s")),
            "start_max_s": _number(summary.get("start_max_s")),
            "end_min_s": _number(summary.get("end_min_s")),
            "end_max_s": _number(summary.get("end_max_s")),
        }
    items = row.get("items")
    if isinstance(items, list):
        starts: list[float] = []
        ends: list[float] = []
        count = 0
        for item in items:
            if not isinstance(item, Mapping):
                continue
            count += 1
            start, end = _bounds(item)
            if start is not None:
                starts.append(start)
            if start is not None and end is not None:
                ends.append(end)
        return {
            "items_count": count,
            "timed_count": len(ends),
            "start_min_s": min(starts) if starts else None,
            "start_max_s": max(starts) if starts else None,
            "end_min_s": min(ends) if ends else None,
            "end_max_s": max(ends) if ends else None,
        }
    count = _integer(row.get("item_count"))
    if count is not None:
        return {
            "items_count": count,
            "timed_count": None,
            "start_min_s": None,
            "start_max_s": None,
            "end_min_s": None,
            "end_max_s": None,
        }
    return None


def _track_row(row: Mapping[str, Any], guid: str | None) -> dict[str, Any]:
    return {
        "guid": _text(row.get("guid")) or guid,
        "name": _text(row.get("name")),
        "fx_names": _fx_names(row),
        "send_count": _send_count(row),
        "item_summary": _item_summary(row),
    }


def _master_row(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError("map_document.master must be a mapping or null")
    return {
        "guid": _text(value.get("guid")),
        "name": _text(value.get("name")),
        "fx_names": _fx_names(value),
    }


def _route_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One compact route: from/to plus the fields the map actually carries."""
    result: dict[str, Any] = {}
    source = (_text(row.get("source_id")) or _text(row.get("source"))
              or _text(row.get("from")))
    target = (_text(row.get("destination_id")) or _text(row.get("destination"))
              or _text(row.get("to")))
    if source is not None:
        result["from"] = source
    if target is not None:
        result["to"] = target
    send = _integer(row.get("send_index"))
    if send is None:
        send = _integer(row.get("send"))
    if send is None:
        send = _integer(row.get("index"))
    if send is not None:
        result["send"] = send
    for name in ("kind", "mode"):
        value = _text(row.get(name))
        if value is not None:
            result[name] = value
    for name in ("volume", "pan"):
        value = _number(row.get(name))
        if value is not None:
            result[name] = value
    mute = row.get("mute")
    if isinstance(mute, bool):
        result["mute"] = mute
    else:
        number = _number(mute)
        if number is not None:
            result["mute"] = bool(number)
    return result


def _routes(map_object: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    if "routes" in map_object:
        raw = map_object.get("routes")
    elif "routing" in map_object:
        raw = map_object.get("routing")
    else:
        return None
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValidationError("map_document routes must be a list")
    return [_route_row(row) for row in raw if isinstance(row, Mapping)]


def _tempo(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    number = _number(value)
    if number is not None:
        return {"bpm": number}
    if not isinstance(value, Mapping):
        raise ValidationError("map_document.tempo must be a mapping or a number")
    result: dict[str, Any] = {}
    bpm = _number(value.get("bpm"))
    if bpm is not None:
        result["bpm"] = bpm
    signature = value.get("time_signature")
    if isinstance(signature, Mapping):
        result["time_signature"] = _json_safe(signature)
    elif signature is not None:
        result["time_signature"] = _json_safe(signature)
    points = value.get("points")
    if isinstance(points, list):
        rows: list[list[Any]] = []
        for point in points:
            if isinstance(point, Mapping):
                rows.append([
                    _first_number(point, "time_s", "time", "position_s"),
                    _number(point.get("bpm")),
                    _integer(point.get("numerator")),
                    _integer(point.get("denominator")),
                ])
            elif (isinstance(point, Sequence) and not isinstance(point, (str, bytes))
                  and len(point) >= 2):
                rows.append([_number(point[0]), _number(point[1])])
        result["points"] = rows
    if result:
        return result
    return _json_safe(dict(value))


def _markers(value: object) -> list[list[Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValidationError("map_document.markers must be a list")
    rows: list[list[Any]] = []
    for marker in value:
        if isinstance(marker, Mapping):
            rows.append([
                _first_number(marker, "time_s", "position_s", "pos"),
                _text(marker.get("name")),
            ])
        elif (isinstance(marker, Sequence) and not isinstance(marker, (str, bytes))
              and len(marker) >= 2):
            rows.append([_number(marker[0]),
                         marker[1] if isinstance(marker[1], str) else None])
    return rows


def _regions(value: object) -> list[list[Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValidationError("map_document.regions must be a list")
    rows: list[list[Any]] = []
    for region in value:
        if isinstance(region, Mapping):
            interval = region.get("interval")
            interval = interval if isinstance(interval, Mapping) else {}
            start = _first_number(interval, "start_s", "start")
            end = _first_number(interval, "end_s", "end")
            if start is None:
                start = _first_number(region, "start_s", "start")
            if end is None:
                end = _first_number(region, "end_s", "end")
            rows.append([start, end, _text(region.get("name"))])
        elif (isinstance(region, Sequence) and not isinstance(region, (str, bytes))
              and len(region) >= 3):
            rows.append([_number(region[0]), _number(region[1]),
                         region[2] if isinstance(region[2], str) else None])
    return rows


def _tracks(map_object: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    if "tracks" not in map_object:
        return None
    raw = map_object.get("tracks")
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        rows: list[dict[str, Any]] = []
        for guid in sorted(raw, key=str):
            row = raw[guid]
            if isinstance(row, Mapping):
                rows.append(_track_row(row, _text(guid)))
        return rows
    if not isinstance(raw, list):
        raise ValidationError("map_document.tracks must be a list or a mapping")
    return [_track_row(row, None) for row in raw if isinstance(row, Mapping)]


def _map_section(map_object: Mapping[str, Any] | None) -> dict[str, Any]:
    if map_object is None:
        return {"present": False}
    tracks = _tracks(map_object)
    return {
        "present": True,
        "track_count": len(tracks) if tracks is not None else None,
        "tracks": tracks,
        "master": _master_row(map_object.get("master")),
        "routes": _routes(map_object),
        "tempo": _tempo(map_object.get("tempo")),
        "markers": _markers(map_object.get("markers")),
        "regions": _regions(map_object.get("regions")),
    }


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #


def _count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, list):
        return len(value)
    return None


def _first_block(*containers: Mapping[str, Any] | None,
                 name: str) -> object:
    for container in containers:
        if container is None:
            continue
        if name in container and container.get(name) is not None:
            return container.get(name)
    return None


def _media_health(*containers: Mapping[str, Any] | None) -> dict[str, Any]:
    value = _first_block(*containers, name="media")
    if value is None:
        return {"present": False}
    if isinstance(value, Mapping):
        offline = _count(value.get("offline"))
        if offline is None:
            offline = _count(value.get("offline_count"))
        missing = _count(value.get("missing"))
        if missing is None:
            missing = _count(value.get("missing_count"))
        ok = None
        if offline is not None and missing is not None:
            ok = offline == 0 and missing == 0
        return {"present": True, "offline_count": offline,
                "missing_count": missing, "ok": ok}
    if isinstance(value, bool):
        return {"present": True, "offline_count": None,
                "missing_count": None, "ok": value}
    return {"present": True, "offline_count": None, "missing_count": None,
            "ok": None}


def _runtime_health(*containers: Mapping[str, Any] | None) -> dict[str, Any]:
    value = _first_block(*containers, name="runtime")
    if value is None:
        return {"present": False}
    if isinstance(value, Mapping):
        return {"present": True, "details": _json_safe(dict(value))}
    return {"present": True, "details": _json_safe(value)}


def _health_section(document: Mapping[str, Any] | None,
                    container: Mapping[str, Any] | None,
                    map_object: Mapping[str, Any] | None) -> dict[str, Any]:
    counts = availability(document)
    stale_sources: set[str] = set()
    facts = document.get("facts") if document is not None else None
    if isinstance(facts, Mapping):
        for fact in facts.values():
            if (isinstance(fact, Mapping)
                    and fact.get("status") == "stale"
                    and isinstance(fact.get("source_id"), str)
                    and fact["source_id"]):
                stale_sources.add(fact["source_id"])
    return {
        "facts": {
            "present": counts["present"],
            "total": counts["facts"],
            "current": counts["current"],
            "stale": counts["stale"],
            "superseded": counts["superseded"],
        },
        "stale_sources": sorted(stale_sources),
        "media": _media_health(container, map_object),
        "runtime": _runtime_health(container, map_object),
    }


# --------------------------------------------------------------------------- #
# facts
# --------------------------------------------------------------------------- #


def _facts_section(document: Mapping[str, Any] | None, *, detail: str,
                   max_facts: int | None) -> dict[str, Any]:
    if document is None:
        return {
            "present": False,
            "detail": detail,
            "total": 0,
            "returned": 0,
            "rows": [],
            "next": None,
        }
    rows = query_facts(document)
    total = len(rows)
    selected = rows if max_facts is None else rows[:max_facts]
    result: list[dict[str, Any]] = []
    for row in selected:
        item: dict[str, Any] = {
            "id": row.get("id"),
            "source_id": row.get("source_id"),
            "stage": row.get("stage"),
            "window": _json_safe(row.get("window")),
            "metrics_available": list(row.get("metrics_requested") or []),
            "status": row.get("status"),
        }
        if detail == "full":
            item["values"] = _json_safe(row.get("values") or {})
        result.append(item)
    return {
        "present": True,
        "detail": detail,
        "total": total,
        "returned": len(result),
        "rows": result,
        "next": {"detail": "state.query",
                 "remaining": max(0, total - len(result))},
    }


# --------------------------------------------------------------------------- #
# fx, changes, audio
# --------------------------------------------------------------------------- #


def _fx_section(fx_summary: object) -> dict[str, Any]:
    if fx_summary is None:
        return {"present": False, "count": 0, "rows": []}
    if (isinstance(fx_summary, (str, bytes))
            or not isinstance(fx_summary, Sequence)):
        raise ValidationError("fx_summary must be a sequence of mappings or None")
    rows: list[dict[str, Any]] = []
    for row in fx_summary:
        if not isinstance(row, Mapping):
            raise ValidationError("fx_summary rows must be mappings")
        rows.append(_json_safe(dict(row)))
    return {"present": True, "count": len(rows), "rows": rows}


def _changes_section(document: Mapping[str, Any] | None,
                     since_token: int | None) -> dict[str, Any]:
    mode = "since" if since_token is not None else "recent"
    if document is None:
        return {"present": False, "mode": mode, "state_token": None,
                "total": 0, "returned": 0, "entries": []}
    if since_token is not None:
        result = changes_since(document, since_token)
        result["present"] = True
        result["mode"] = "since"
        result["since"] = since_token
        return result
    entries: list[dict[str, Any]] = []
    raw = document.get("changes")
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, Mapping):
                entries.append(_json_safe(dict(entry)))
    total = len(entries)
    shown = entries[-RECENT_CHANGES_SHOWN:] if total > RECENT_CHANGES_SHOWN \
        else entries
    return {
        "present": True,
        "mode": "recent",
        "state_token": _token(document),
        "total": total,
        "returned": len(shown),
        "omitted": total - len(shown),
        "entries": shown,
    }


def _audio_section(container: Mapping[str, Any] | None,
                   map_object: Mapping[str, Any] | None) -> dict[str, Any]:
    raw: object = None
    found = False
    for holder in (container, map_object):
        if holder is not None and holder.get("audio") is not None:
            raw = holder.get("audio")
            found = True
            break
    refs: object = None
    for holder in (container, map_object):
        if holder is not None and holder.get("audio_refs") is not None:
            refs = holder.get("audio_refs")
            found = True
            break

    sources: dict[str, dict[str, Any]] = {}
    if isinstance(raw, list):
        for asset in raw:
            if not isinstance(asset, Mapping):
                continue
            source_id = _text(asset.get("source_id"))
            if source_id is None:
                guid = _text(asset.get("source_guid")) or _text(asset.get("guid"))
                if guid is not None:
                    owner = "master" if asset.get("is_master") is True else "track"
                    source_id = f"{owner}:{guid}"
            if source_id is None:
                continue
            sources[source_id] = {
                "sha256": _text(asset.get("sha256")),
                "stage": _text(asset.get("stage")),
            }
    elif isinstance(raw, Mapping):
        refs = raw
    if isinstance(refs, Mapping):
        for key, ref in refs.items():
            if not isinstance(key, str) or not key:
                continue
            if isinstance(ref, Mapping):
                sources[key] = {
                    "sha256": _text(ref.get("sha256")),
                    "stage": _text(ref.get("stage")),
                }
            elif isinstance(ref, str):
                sources[key] = {"sha256": ref, "stage": None}
    return {
        "present": found,
        "count": len(sources),
        "sources": {key: sources[key] for key in sorted(sources)},
    }


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #


def build_project_context(
    state: object,
    *,
    map_document: object = None,
    fx_summary: object = None,
    since_token: int | None = None,
    detail: str = "summary",
    max_facts: int | None = 50,
) -> dict[str, Any]:
    """Build the one project context object from state and optional extras.

    ``state`` is a :class:`ProjectState`, its document mapping, or ``None`` when
    no state has been loaded.  ``map_document`` is the project map (or a capture
    record carrying one under ``project_map``); ``fx_summary`` is the plugin
    parameter summary; ``since_token`` switches the changes section to a delta.

    ``detail`` is ``summary`` (no measured values in the fact rows) or ``full``
    (values for the returned rows only).  ``max_facts`` caps the fact rows;
    ``None`` removes the cap.  Missing map and facts are explicit
    ``present: false``; no value is invented for an absent input.

    The result is JSON-safe and deterministic: mappings are copied with sorted
    keys, non-finite numbers become ``None`` and nothing depends on iteration
    order of the inputs except the project's own track/route order.
    """
    if detail not in DETAIL_LEVELS:
        raise ValidationError("detail must be 'summary' or 'full'",
                              details={"detail": detail,
                                       "allowed": list(DETAIL_LEVELS)})
    if max_facts is not None:
        if isinstance(max_facts, bool) or not isinstance(max_facts, int) \
                or max_facts < 0:
            raise ValidationError("max_facts must be a non-negative integer or None",
                                  details={"max_facts": max_facts})
    if since_token is not None:
        if isinstance(since_token, bool) or not isinstance(since_token, int) \
                or since_token < 0:
            raise ValidationError("since_token must be a non-negative integer",
                                  details={"since_token": since_token})

    document = _document(state)
    container = _mapping_arg(map_document, "map_document")
    map_object = _map_object(container)

    return {
        "project": _project_section(document, container),
        "map": _map_section(map_object),
        "health": _health_section(document, container, map_object),
        "facts": _facts_section(document, detail=detail, max_facts=max_facts),
        "fx": _fx_section(fx_summary),
        "changes": _changes_section(document, since_token),
        "audio": _audio_section(container, map_object),
        "next": dict(NEXT_POINTER),
    }


def estimated_tokens(context: object) -> int:
    """Conservative token estimate for one context payload.

    ``ceil(UTF-8 bytes of canonical JSON / 2.2)``, minimum 1.  Real English and
    JSON tokenise closer to four bytes per token, so this deliberately
    over-counts; it is a budget guard, not a tokenizer claim.
    """
    if not isinstance(context, Mapping):
        raise ValidationError("estimated_tokens needs a context mapping",
                              details={"type": type(context).__name__})
    return max(1, math.ceil(len(_encoded(context)) / TOKEN_BYTES_ESTIMATE))


def project_context_pages(context: object, *,
                          page_bytes: int = DEFAULT_PAGE_BYTES
                          ) -> list[dict[str, Any]]:
    """Pack the context's top-level sections into deterministic pages.

    Sections are taken in :data:`SECTIONS` order (unknown top-level keys follow
    in sorted order) and packed greedily: a page takes the next section when
    the resulting payload still fits in ``page_bytes``.  A section is atomic --
    it is never split -- so a section larger than one page occupies a page of
    its own and that page exceeds the budget; every page whose sections
    individually fit is within ``page_bytes``.  Each page payload is
    ``{"schema", "sections", "context"}``; page order is list order.

    Determinism: the same context and ``page_bytes`` always yield byte-identical
    pages, because section order and every value are deterministic.
    """
    if isinstance(page_bytes, bool) or not isinstance(page_bytes, int) \
            or page_bytes <= 0:
        raise ValidationError("page_bytes must be a positive integer",
                              details={"page_bytes": page_bytes})
    if not isinstance(context, Mapping):
        raise ValidationError("project_context_pages needs a context mapping",
                              details={"type": type(context).__name__})

    known = [name for name in SECTIONS if name in context]
    unknown = [name for name in context
               if name not in SECTIONS and not isinstance(name, str)]
    if unknown:
        raise ValidationError("context section names must be strings",
                              details={"names": [type(name).__name__
                                                 for name in unknown]})
    extra = sorted(str(name) for name in context if name not in SECTIONS)
    order = known + extra

    groups: list[list[str]] = []
    current: list[str] = []
    for name in order:
        candidate = current + [name]
        if current and len(_encoded(_page(candidate, context))) > page_bytes:
            groups.append(current)
            current = [name]
        else:
            current = candidate
    if current:
        groups.append(current)
    if not groups:
        groups = [[]]
    return [_page(group, context) for group in groups]


def _page(names: list[str], context: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": CONTEXT_SCHEMA,
        "sections": list(names),
        "context": {name: _json_safe(context[name]) for name in names},
    }


__all__ = [
    "CONTEXT_SCHEMA",
    "DEFAULT_PAGE_BYTES",
    "DETAIL_LEVELS",
    "NEXT_POINTER",
    "RECENT_CHANGES_SHOWN",
    "SECTIONS",
    "TOKEN_BYTES_ESTIMATE",
    "build_project_context",
    "estimated_tokens",
    "project_context_pages",
]
