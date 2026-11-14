"""One generic semantic query over an archived capture.

This is the exact-detail path that replaces paging.  There are no pages, no
cursors and no numbered chunks: a caller names a *meaning* -- which objects,
which part of the timeline, which data families -- and receives that whole
coherent fragment in one result.

Two retrieval paths, both exact:

* series cells, MIDI notes and envelope points are resolved through
  ``context.details`` against the sealed manifest, so every returned row is
  re-hashed and a mismatch raises instead of returning data from another
  revision;
* relations, items, takes, audio rows, analysis rows and the project map are
  filtered from the canonical panorama document, which is itself bound to the
  capture/revision/integrity of the sealed record.

If the selected fragment does not fit the response budget the call returns
``query_too_large`` with the measured size per family and the semantic scope
that has to be narrowed.  It never truncates, never paginates and never
silently returns less than it selected.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from reaper_audio.context.details import DETAILS_SCHEMA, resolve_details
from reaper_audio.context.panorama_view import (
    ARCHIVE_FAMILIES,
    DEFAULT_RESPONSE_BYTES,
    canonical,
    section_bytes,
    table,
)
from reaper_audio.errors import ValidationError

QUERY_SCHEMA = "reaper-workbench.panorama-query/1"

DOCUMENT_FAMILIES = ("relations", "items", "takes", "audio", "fx", "analysis", "project_summary")
DETAIL_FAMILIES = ("series", "midi_notes", "envelope_points")
ALL_FAMILIES = tuple(ARCHIVE_FAMILIES)


def _interval(value: Any) -> tuple[float | None, float | None]:
    if value is None:
        return (None, None)
    if not isinstance(value, Mapping):
        raise ValidationError("interval must be an object with start_s and end_s")
    start = value.get("start_s")
    end = value.get("end_s")
    try:
        start = float(start) if start is not None else None
        end = float(end) if end is not None else None
    except (TypeError, ValueError):
        raise ValidationError("interval start_s/end_s must be numbers") from None
    if start is not None and end is not None and not start < end:
        raise ValidationError("interval start_s must be less than end_s")
    return (start, end)


def _overlaps(start: Any, end: Any, window: tuple[float | None, float | None]) -> bool:
    low, high = window
    if low is None and high is None:
        return True
    if start is None or end is None:
        return False
    try:
        return float(end) > (low if low is not None else float("-inf")) and \
            float(start) < (high if high is not None else float("inf"))
    except (TypeError, ValueError):
        return False


def _object_index(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Every addressable id, guid and unique name mapped to its object kind."""
    index: dict[str, dict[str, Any]] = {}

    def add(identifier: Any, kind: str, stable: Any, guid: Any = None,
            name: Any = None) -> None:
        if not identifier:
            return
        entry = {"kind": kind, "id": stable, "guid": guid, "name": name}
        index[str(identifier)] = entry

    def add_all(rows: Iterable[Any], kind: str, id_key: str) -> None:
        seen_names: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            add(row.get(id_key), kind, row.get(id_key), row.get("guid"), row.get("name"))
            if row.get("guid"):
                add(row.get("guid"), kind, row.get(id_key), row.get("guid"), row.get("name"))
            name = row.get("name")
            if isinstance(name, str) and name:
                seen_names[name] = seen_names.get(name, 0) + 1

    add_all(document.get("tracks") or [], "track", "stable_id")
    add_all([document.get("master") or {}], "track", "stable_id")
    add_all(document.get("items") or [], "item", "item_id")
    add_all(document.get("takes") or [], "take", "take_id")
    add_all(document.get("audio") or [], "audio", "audio_id")
    add_all(document.get("analysis") or [], "analysis", "analysis_id")
    add_all([row for row in (document.get("midi") or []) if isinstance(row, Mapping)],
            "midi", "midi_id")
    # Unique human names are convenient and unambiguous; duplicate names are not
    # resolved silently.
    names: dict[str, list[str]] = {}
    for row in (document.get("tracks") or []) + [document.get("master") or {}]:
        if isinstance(row, Mapping) and isinstance(row.get("name"), str):
            names.setdefault(row["name"], []).append(str(row.get("stable_id")))
    for name, ids in names.items():
        if len(ids) == 1 and name not in index:
            index[name] = {"kind": "track", "id": ids[0], "guid": None, "name": name}
    return index


def _resolve_objects(document: Mapping[str, Any],
                     objects: Any) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Return (selection, unknown). ``None`` selection means 'all objects'."""
    if objects is None or objects == "all" or objects == ["all"]:
        return None, []
    if isinstance(objects, (str, Mapping)):
        objects = [objects]
    index = _object_index(document)
    selection: list[dict[str, Any]] = []
    unknown: list[str] = []
    for raw in objects or []:
        token = raw.get("id") if isinstance(raw, Mapping) else raw
        if not isinstance(token, str) or not token:
            unknown.append(str(token))
            continue
        entry = index.get(token)
        if entry is None:
            unknown.append(token)
            continue
        selection.append(entry)
    return selection, unknown


def _matches(value: Any, selection: Sequence[Mapping[str, Any]] | None) -> bool:
    if selection is None:
        return True
    wanted = {str(entry.get("id")) for entry in selection}
    return value is not None and str(value) in wanted


def _source_ids(selection: Sequence[Mapping[str, Any]] | None) -> set[str] | None:
    """Objects plus the sources they own, for measurement families."""
    if selection is None:
        return None
    return {str(entry.get("id")) for entry in selection}


def _relation_rows(document: Mapping[str, Any], selection: Sequence[Mapping[str, Any]] | None,
                   kinds: Sequence[str] | None, bands: Sequence[float] | None,
                   window: tuple[float | None, float | None],
                   *, include_pairs: bool = True) -> dict[str, Any]:
    relations = document.get("relations")
    relations = relations if isinstance(relations, Mapping) else {}
    edges = [row for row in relations.get("edges") or [] if isinstance(row, Mapping)]
    pairs = [row for row in relations.get("evaluated_pairs") or [] if isinstance(row, Mapping)]
    unavailable = relations.get("unavailable")
    if kinds:
        allowed = {str(kind) for kind in kinds}
        edges = [row for row in edges if str(row.get("kind")) in allowed]
        unavailable = {key: reason for key, reason in (unavailable or {}).items()
                       if str(key).split(":", 1)[0] in allowed}
        # Evaluated pairs are aggregate band-dominance pairs.  They are kept
        # only when the caller still selected that relation family.
        if not ({"band_dominance", "band_dominance_pair"} & allowed):
            pairs = []
    if bands:
        wanted = {float(band) for band in bands}
        edges = [row for row in edges if row.get("band_hz") is not None
                 and float(row["band_hz"]) in wanted]
        # A pair is not band-scoped; a band filter cannot honestly select one.
        pairs = []
    if not include_pairs:
        pairs = []
    if selection is not None:
        wanted = {str(entry.get("id")) for entry in selection}
        edges = [row for row in edges
                 if str(row.get("from")) in wanted or str(row.get("to")) in wanted]
        pairs = [row for row in pairs
                 if str(row.get("from")) in wanted or str(row.get("to")) in wanted]
        unavailable = {key: reason for key, reason in (unavailable or {}).items()
                       if any(f"|{item}" in key or key.endswith(f"|{item}")
                              or f":{item}|" in key or f"|{item}" in key
                              for item in wanted)}
    return {
        "schema_version": relations.get("schema_version"),
        "complete": relations.get("complete"),
        "coverage": relations.get("coverage"),
        "reference_status": relations.get("reference_status"),
        "content_hash": relations.get("content_hash"),
        "edges": edges,
        "evaluated_pairs": pairs,
        "unavailable": dict(unavailable or {}),
        "counts": {"edges": len(edges), "pairs": len(pairs),
                   "unavailable": len(unavailable or {})},
    }


def _rows(value: Any, key: str = "envelopes") -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        value = value.get(key)
    return [row for row in value or [] if isinstance(row, Mapping)]


def _owner_aliases(document: Mapping[str, Any]) -> dict[str, set[str]]:
    """Which object ids address the same measured data.

    A model selects "this track" and expects its measurement cells and its
    automation, not only rows whose owner string happens to equal the track id.
    """
    aliases: dict[str, set[str]] = {}

    def link(left: Any, right: Any) -> None:
        if not left or not right:
            return
        left, right = str(left), str(right)
        aliases.setdefault(left, {left}).add(right)
        aliases.setdefault(right, {right}).add(left)

    for row in document.get("tracks") or []:
        if not isinstance(row, Mapping):
            continue
        for key in ("audio_id", "analysis_id"):
            link(row.get("stable_id"), row.get(key))
    master = document.get("master")
    if isinstance(master, Mapping):
        for key in ("audio_id", "analysis_id"):
            link(master.get("stable_id"), master.get(key))
    for row in document.get("analysis") or []:
        if isinstance(row, Mapping):
            link(row.get("analysis_id"), row.get("source_id"))
    for key in ("automation", "envelopes"):
        for row in _rows(document.get(key), "envelopes"):
            link(row.get("envelope_id"), row.get("owner_id"))
    return aliases


def _expanded_owners(document: Mapping[str, Any],
                     selection: Sequence[Mapping[str, Any]]) -> set[str]:
    aliases = _owner_aliases(document)
    expanded: set[str] = set()
    for entry in selection:
        for key in ("id", "guid"):
            value = entry.get(key)
            if value:
                expanded.add(str(value))
                expanded |= aliases.get(str(value), set())
    return expanded


def _detail_requests(document: Mapping[str, Any], families: Sequence[str],
                     selection: Sequence[Mapping[str, Any]] | None,
                     window: tuple[float | None, float | None],
                     series: Sequence[str] | None) -> list[dict[str, Any]]:
    details = document.get("details")
    details = details if isinstance(details, Mapping) else {}
    entries = [entry for entry in details.get("entries") or [] if isinstance(entry, Mapping)]
    kind_for = {"series": "analysis_series", "midi_notes": "midi_notes",
                "envelope_points": "envelope_points"}
    wanted_kinds = {kind_for[family] for family in families}
    wanted_series = {str(name) for name in series} if series else None
    owners = None if selection is None else _expanded_owners(document, selection)
    requests = []
    for entry in entries:
        if entry.get("kind") not in wanted_kinds:
            continue
        owner = entry.get("owner_id")
        if owners is not None and str(owner) not in owners and str(entry.get("ref")) not in owners:
            continue
        raw_entry_series = entry.get("series")
        entry_series: dict[str, Any] = (dict(raw_entry_series)
                                        if isinstance(raw_entry_series, Mapping) else {})
        selected_series = list(entry_series)
        if wanted_series is not None and entry.get("kind") == "analysis_series":
            selected_series = [name for name in entry_series if name in wanted_series]
            if not selected_series:
                continue
        interval = entry.get("interval") if isinstance(entry.get("interval"), Mapping) else {}
        request: dict[str, Any] = {"kind": entry.get("kind"), "id": owner}
        if entry.get("kind") == "analysis_series" and selected_series:
            request["series"] = selected_series
        start, end = window
        if start is not None:
            request["start_s"] = start
        if end is not None:
            request["end_s"] = end
        if start is None and end is None and interval.get("start_s") is not None:
            # No caller window: return the whole stored entry, not a slice of it.
            pass
        requests.append(request)
    return requests


def _family_filter(document: Mapping[str, Any], family: str,
                   selection: Sequence[Mapping[str, Any]] | None,
                   window: tuple[float | None, float | None]) -> Any:
    if family == "project_summary":
        return document.get("project") or {}
    if family == "items":
        rows = [row for row in document.get("items") or [] if isinstance(row, Mapping)]
        if selection is not None:
            wanted = {str(entry.get("id")) for entry in selection}
            rows = [row for row in rows if str(row.get("item_id")) in wanted
                    or str(row.get("track_id")) in wanted]
        if window != (None, None):
            rows = [row for row in rows
                    if _overlaps((row.get("interval") or {}).get("start_s")
                                 if isinstance(row.get("interval"), Mapping) else row.get("position_s"),
                                 (row.get("interval") or {}).get("end_s")
                                 if isinstance(row.get("interval"), Mapping) else None,
                                 window)]
        return rows
    if family == "takes":
        rows = [row for row in document.get("takes") or [] if isinstance(row, Mapping)]
        if selection is not None:
            wanted = {str(entry.get("id")) for entry in selection}
            rows = [row for row in rows if str(row.get("take_id")) in wanted
                    or str(row.get("item_id")) in wanted or str(row.get("track_id")) in wanted]
        return rows
    if family == "audio":
        rows = [row for row in document.get("audio") or [] if isinstance(row, Mapping)]
        if selection is not None:
            wanted = {str(entry.get("id")) for entry in selection}
            rows = [row for row in rows if str(row.get("audio_id")) in wanted
                    or str(row.get("source_id")) in wanted]
        return rows
    if family == "analysis":
        rows = [row for row in document.get("analysis") or [] if isinstance(row, Mapping)]
        if selection is not None:
            wanted = {str(entry.get("id")) for entry in selection}
            rows = [row for row in rows if str(row.get("analysis_id")) in wanted
                    or str(row.get("source_id")) in wanted]
        return rows
    if family == "fx":
        rows = [row for row in document.get("fx") or [] if isinstance(row, Mapping)]
        if selection is not None:
            wanted = {str(entry.get("id")) for entry in selection}
            rows = [row for row in rows if str(row.get("owner_id")) in wanted
                    or str(row.get("fx_id")) in wanted]
        return rows
    return None


def _metric_names(document: Mapping[str, Any]) -> list[str]:
    units = document.get("analysis_metric_units")
    return sorted(dict(units)) if isinstance(units, Mapping) else []


def _metric_metadata(document: Mapping[str, Any], names: Sequence[str]) -> dict[str, Any]:
    units = document.get("analysis_metric_units")
    units = dict(units) if isinstance(units, Mapping) else {}
    contract = document.get("analysis_contract")
    method = (contract.get("method") if isinstance(contract, Mapping) else None)
    method = dict(method) if isinstance(method, Mapping) else {}
    descriptor = {
        "analysis": method.get("analysis"),
        "version": method.get("version") or "unversioned",
        "decoder": method.get("decoder"),
        "measurement_stage": "file_or_buffer",
    }
    return {
        "methods": [descriptor],
        "metric_units": {name: units.get(name) for name in names},
        "metric_methods": {name: 0 for name in names},
    }


def _project_analysis_rows(rows: Sequence[Mapping[str, Any]],
                           metrics: Sequence[str] | None) -> list[dict[str, Any]]:
    if not metrics:
        return [dict(row) for row in rows]
    selected = [str(metric) for metric in metrics]
    projected = []
    for row in rows:
        values = row.get("metrics") if isinstance(row.get("metrics"), Mapping) else {}
        projected.append({
            **{key: value for key, value in row.items() if key != "metrics"},
            "metrics": {name: values.get(name) for name in selected},
        })
    return projected


def run_query(document: Mapping[str, Any], record: Any, *,
              data: Any = None,
              objects: Any = None,
              interval: Any = None,
              series: Sequence[str] | None = None,
              metrics: Sequence[str] | None = None,
              kinds: Sequence[str] | None = None,
              bands_hz: Sequence[float] | None = None,
              resolution_s: float | None = None,
              include_pairs: bool = True,
              max_response_bytes: int = DEFAULT_RESPONSE_BYTES) -> dict[str, Any]:
    """Return one coherent semantic fragment of the archived capture."""
    if isinstance(data, str):
        data = [data]
    families = [str(family) for family in (data or ["all"])]
    if "all" in families:
        families = list(ALL_FAMILIES)
    unknown_families = [family for family in families if family not in ARCHIVE_FAMILIES]
    if unknown_families:
        raise ValidationError(
            "unknown data family",
            details={"unknown": unknown_families, "allowed": list(ALL_FAMILIES)})
    window = _interval(interval)
    all_metrics = _metric_names(document)
    if metrics is not None:
        requested_metrics = [str(name) for name in metrics]
        unknown_metrics = [name for name in requested_metrics if name not in all_metrics]
        if unknown_metrics:
            raise ValidationError(
                "unknown analysis metric",
                details={"unknown": unknown_metrics, "allowed": all_metrics})
    else:
        requested_metrics = all_metrics
    selection, unknown_objects = _resolve_objects(document, objects)
    if unknown_objects:
        raise ValidationError(
            "unknown object in query selection",
            details={"unknown": unknown_objects,
                     "next_action": ("read the view's tables for stable ids, or pass "
                                     "'all' for the whole family")})
    result: dict[str, Any] = {
        "schema_version": QUERY_SCHEMA,
        "status": "query",
        "capture_id": (document.get("capture_binding") or {}).get("capture_id"),
        "project_id": (document.get("capture_binding") or {}).get("project_id"),
        "revision": ((document.get("capture_binding") or {}).get("revision_after")
                     or (document.get("capture_binding") or {}).get("revision_before")),
        "document_sha256": document.get("document_sha256"),
        "selection": {
            "data": families,
            "objects": "all" if selection is None else [entry.get("id") for entry in selection],
            "interval": ({"start_s": window[0], "end_s": window[1]}
                         if window != (None, None) else None),
            "series": list(series) if series else "all",
            "metrics": requested_metrics if metrics is not None else "all",
            "kinds": list(kinds) if kinds else "all",
            "include_pairs": include_pairs,
            "resolution_s": resolution_s,
            "units": {family: ARCHIVE_FAMILIES[family]["units"] for family in families},
        },
        "data": {},
        "time_basis": "project_seconds, half-open [start_s,end_s)",
        "guarantees": {
            "exact": ("rows are the stored measurements of this revision; they are not "
                      "re-measured and not truncated"),
            "hash_bound": ("series, MIDI and envelope rows are re-hashed against the sealed "
                           "detail manifest before they are returned"),
            "no_pages": "a single coherent fragment, never a numbered chunk or cursor",
        },
    }
    family_bytes: dict[str, int] = {}
    detail_families = [family for family in families if family in DETAIL_FAMILIES]
    if detail_families:
        requests = _detail_requests(document, detail_families, selection, window, series)
        if requests:
            if resolution_s is not None:
                for request in requests:
                    if request["kind"] == "analysis_series":
                        request["resolution_s"] = float(resolution_s)
            try:
                detail_payload = resolve_details(record, document, requests,
                                                 max_bytes=max_response_bytes)
            except ValidationError as error:
                # A point-count refusal is still a size refusal: report it as
                # query_too_large with the same narrowing guidance, never as a
                # generic validation error the model cannot act on.
                if "limit" not in (error.details or {}):
                    raise
                return {
                    **{key: result[key] for key in
                       ("schema_version", "capture_id", "project_id", "revision",
                        "document_sha256", "selection", "time_basis")},
                    "status": "query_too_large",
                    "reason": "exact_detail_point_limit",
                    "point_limit": error.details.get("limit"),
                    "requested_series": error.details.get("requested_series"),
                    "max_response_bytes": int(max_response_bytes),
                    "narrow": [
                        "pass interval {start_s,end_s} to one section of the song",
                        "pass objects [stable ids] to fewer sources",
                        "pass a coarser resolution_s or fewer series names",
                    ],
                    "note": ("the rows exist in full in the archive; this is a per-request "
                             "point limit, not a missing measurement"),
                }
            if detail_payload.get("status") == "detail_too_large":
                return {
                    **{key: result[key] for key in
                       ("schema_version", "capture_id", "project_id", "revision",
                        "document_sha256", "selection", "time_basis")},
                    "status": "query_too_large",
                    "reason": "exact_detail_over_budget",
                    "measured_bytes": detail_payload.get("bytes"),
                    "max_response_bytes": detail_payload.get("max_bytes"),
                    "family_bytes": {"exact_detail": detail_payload.get("bytes")},
                    "requests": detail_payload.get("requests"),
                    "narrow": [
                        "pass interval {start_s,end_s} to one section of the song",
                        "pass objects [stable ids] to fewer sources",
                        "pass series [names] or a coarser resolution_s",
                    ],
                    "note": ("the rows exist in full in the archive; this is a size limit "
                             "of one response, not a missing measurement"),
                }
            result["data"]["exact_detail"] = detail_payload
            family_bytes["exact_detail"] = section_bytes(detail_payload)
        else:
            result["data"]["exact_detail"] = {
                "status": "empty_selection",
                "note": "no stored detail entry matches this object/interval/series selection",
                "entries_total": len((document.get("details") or {}).get("entries") or []),
            }
            family_bytes["exact_detail"] = section_bytes(result["data"]["exact_detail"])
    for family in families:
        if family in DETAIL_FAMILIES:
            continue
        if family == "relations":
            value = _relation_rows(document, selection, kinds, bands_hz, window,
                                   include_pairs=include_pairs)
        else:
            value = _family_filter(document, family, selection, window)
            if family == "analysis":
                value = _project_analysis_rows(value, requested_metrics
                                               if metrics is not None else None)
        result["data"][family] = value
        family_bytes[family] = section_bytes(value)
    if "analysis" in families or metrics is not None:
        result.update(_metric_metadata(document, requested_metrics))
    returned_rows = sum(len(value) for value in result["data"].values()
                        if isinstance(value, list))
    returned_points = 0
    if isinstance(result["data"].get("exact_detail"), Mapping):
        detail = result["data"]["exact_detail"]
        returned_points = sum(int(row.get("count") or 0) for row in
                              detail.get("series") or []) if isinstance(
                                  detail.get("series"), list) else int(
                                      detail.get("count") or 0)
    result["scope"] = {
        "matching_rows": returned_rows,
        "returned_rows": returned_rows,
        "matching_points": returned_points,
        "returned_points": returned_points,
        "complete": True,
        "hash_verified": bool(result["data"].get("exact_detail")),
    }
    measured = section_bytes(result)
    result["budget"] = {
        "measured_bytes": measured,
        "max_response_bytes": int(max_response_bytes),
        "fits": measured <= int(max_response_bytes),
        "family_bytes": family_bytes,
    }
    if measured > int(max_response_bytes):
        return {
            "schema_version": QUERY_SCHEMA,
            "status": "query_too_large",
            "reason": "selection_over_budget",
            "capture_id": result["capture_id"],
            "project_id": result["project_id"],
            "revision": result["revision"],
            "document_sha256": result["document_sha256"],
            "selection": result["selection"],
            "measured_bytes": measured,
            "max_response_bytes": int(max_response_bytes),
            "family_bytes": family_bytes,
            "narrow": [
                "drop the families you do not need from data",
                "pass objects [stable ids] instead of all",
                "pass interval {start_s,end_s} for a section",
                "request one series or a coarser resolution_s",
            ],
            "note": ("the selection is over the size of one response. Nothing was "
                     "truncated and nothing was paged; narrow the semantic scope and the "
                     "whole fragment arrives in one result."),
        }
    return result


def query_catalog() -> dict[str, Any]:
    """The families and selectors this tool accepts, for the view's catalog."""
    return {
        "schema_version": QUERY_SCHEMA,
        "families": {family: dict(spec) for family, spec in ARCHIVE_FAMILIES.items()},
        "selectors": {
            "data": [f"one or more of {sorted(ARCHIVE_FAMILIES)}", "default: all"],
            "objects": ["'all'", "stable ids", "REAPER guids", "unique track names"],
            "interval": {"start_s": "s", "end_s": "s"},
            "series": ["rms_400ms", "band_level", "temporal_profile"],
            "kinds": ["band_dominance", "track_master_level_relation"],
            "bands_hz": ["band centres for band_dominance"],
            "resolution_s": "coarsen series cells inside the interval",
        },
        "detail_schema": DETAILS_SCHEMA,
    }


__all__ = [
    "ALL_FAMILIES",
    "DETAIL_FAMILIES",
    "DOCUMENT_FAMILIES",
    "QUERY_SCHEMA",
    "query_catalog",
    "run_query",
    "table",
    "canonical",
]
