"""Typed detail references for the capture panorama.

The panorama inlines the whole-project map and one navigational summary per
linked domain (MIDI take, automation envelope, analysis series).  The exact
rows stay in the canonical capture record and are addressed by a typed
reference that names the owner, schema, units, time basis, interval, row count,
completeness and content hash.

A reference is a retrieval address, never a replacement for the evidence: the
resolver in this module returns the exact stored rows and verifies their hash
against the reference before returning them.  A stale or mismatched reference
fails closed instead of returning partial data.

Nothing here interprets music, infers a role, scores a mix or asserts a cause.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import sys
from array import array
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

from ..errors import PanoramaBindingError, ValidationError

DETAILS_SCHEMA = "reaper-workbench.panorama-details/1"
DETAIL_FETCH_TOOL = "reaper_panorama_query"
DETAIL_INTERVAL = "[start_s,end_s)"
DEFAULT_DETAIL_BYTES = 48_000
MAX_DETAIL_POINTS = 20_000

DETAIL_KIND_SCHEMA = {
    "midi_notes": "reaper-workbench.midi-notes/1",
    "envelope_points": "reaper-workbench.envelope-points/1",
    "analysis_series": "reaper-workbench.analysis-series/1",
}
DETAIL_KINDS = frozenset(DETAIL_KIND_SCHEMA)


def _json_default(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return dict(value)
    if isinstance(value, tuple):
        return list(value)
    if type(value).__module__.startswith("numpy") and hasattr(value, "item"):
        return value.item()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False,
                      default=_json_default).encode("utf-8")


def content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def revision_fingerprint(record: Any) -> str:
    revision = (getattr(record, "revision_after", None) or
                getattr(record, "revision_before", None))
    if isinstance(record, Mapping):
        revision = record.get("revision_after") or record.get("revision_before")
    if revision is None:
        return content_sha256({"capture_id": getattr(record, "capture_id", "")})
    return content_sha256(revision)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _interval(start: Any, end: Any) -> dict[str, Any]:
    first = _number(start)
    last = _number(end)
    if first is None or last is None:
        return {"start_s": None, "end_s": None, "available": False,
                "interval": DETAIL_INTERVAL, "units": "s"}
    return {"start_s": first, "end_s": last, "duration_s": max(0.0, last - first),
            "available": True, "interval": DETAIL_INTERVAL, "units": "s"}


def _fetch(request: Mapping[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in request.items() if value is not None}
    return {"tool": DETAIL_FETCH_TOOL, "request": payload}


def _binding(*, capture_id: str, project_id: str,
             revision: str, integrity: str | None, record_path: str | None) -> dict[str, Any]:
    return {
        "capture_id": capture_id,
        "project_id": project_id,
        "revision_fingerprint": revision,
        "record_integrity_sha256": integrity,
        "record_path": record_path,
    }


def midi_reference(*, capture_id: str, project_id: str, revision: str,
                   integrity: str | None, record_path: str | None,
                   take_guid: str, take_id: str, item_id: str | None,
                   track_id: str | None, notes: Sequence[Mapping[str, Any]],
                   note_count: int | None, returned_count: int | None,
                   truncated: bool, time_basis: Mapping[str, Any]) -> dict[str, Any]:
    """One navigational reference to the exact notes of one MIDI take."""

    rows = [dict(note) for note in notes if isinstance(note, Mapping)]
    starts = [v for v in (_number(note.get("start_s")) for note in rows) if v is not None]
    ends = [v for v in (_number(note.get("end_s")) for note in rows) if v is not None]
    pitches = [v for v in (_integer(note.get("pitch")) for note in rows) if v is not None]
    velocities = [v for v in (_integer(note.get("velocity")) for note in rows) if v is not None]
    channels = sorted({v for v in (_integer(note.get("channel")) for note in rows)
                       if v is not None})
    occupied = _interval(min(starts) if starts else None,
                         max(ends) if ends else None)
    reference = {
        "ref": f"midi:{take_guid}",
        "kind": "midi_notes",
        "schema": DETAIL_KIND_SCHEMA["midi_notes"],
        "owner_id": take_id,
        "owner_kind": "take",
        "item_id": item_id,
        "track_id": track_id,
        "time_basis": dict(time_basis),
        "interval": occupied,
        "count": int(note_count if note_count is not None else len(rows)),
        "returned_count": int(returned_count if returned_count is not None else len(rows)),
        "truncated": bool(truncated),
        "occupied_interval": occupied,
        "pitch_range": {"min": min(pitches) if pitches else None,
                        "max": max(pitches) if pitches else None},
        "velocity_range": {"min": min(velocities) if velocities else None,
                           "max": max(velocities) if velocities else None},
        "channels": channels,
        "content_sha256": content_sha256(rows),
        "fetch": _fetch({"kind": "midi_notes", "id": take_id}),
        **_binding(capture_id=capture_id, project_id=project_id,
                   revision=revision, integrity=integrity, record_path=record_path),
    }
    return reference


def envelope_reference(*, capture_id: str, project_id: str, revision: str,
                       integrity: str | None, record_path: str | None,
                       envelope_id: str, domain: str, owner_id: str,
                       owner_guid: str | None, envelope_guid: str | None,
                       name: str | None, points: Sequence[Mapping[str, Any]],
                       point_count: int | None, active: bool,
                       time_basis: Mapping[str, Any],
                       units: str | None = None) -> dict[str, Any]:
    """One navigational reference to the exact points of one envelope."""

    rows = [dict(point) for point in points if isinstance(point, Mapping)]
    times = [v for v in (_number(point.get("time_s")) for point in rows) if v is not None]
    values = [v for v in (_number(point.get("value")) for point in rows) if v is not None]
    shapes = sorted({v for v in (_integer(point.get("shape")) for point in rows)
                     if v is not None})
    reference = {
        "ref": envelope_id,
        "kind": "envelope_points",
        "schema": DETAIL_KIND_SCHEMA["envelope_points"],
        "owner_id": owner_id,
        "owner_kind": "track",
        "envelope_id": envelope_id,
        "name": name,
        "target": {
            "parameter": name,
            "units": units,
            "units_basis": ("reported_by_reaper" if units is not None
                            else "not_reported_by_reaper"),
        },
        "active": bool(active),
        "time_basis": dict(time_basis),
        "interval": _interval(min(times) if times else None,
                              max(times) if times else None),
        "count": int(point_count if point_count is not None else len(rows)),
        "returned_count": len(rows),
        "value_range": {"min": min(values) if values else None,
                        "max": max(values) if values else None},
        "shape_kinds": shapes,
        "content_sha256": content_sha256(rows),
        "fetch": _fetch({"kind": "envelope_points", "id": envelope_id}),
        **_binding(capture_id=capture_id, project_id=project_id,
                   revision=revision, integrity=integrity, record_path=record_path),
        "locator": {"domain": domain, "owner_guid": owner_guid,
                    "envelope_guid": envelope_guid, "name": name},
    }
    return reference


def series_reference(*, capture_id: str, project_id: str, revision: str,
                     integrity: str | None, record_path: str | None,
                     analysis_id: str, source_id: str | None,
                     series: Mapping[str, Any], interval: Mapping[str, Any],
                     time_basis: Mapping[str, Any]) -> dict[str, Any]:
    """One reference covering every exact series stored for one analysis."""

    return {
        "ref": f"series:{analysis_id}",
        "kind": "analysis_series",
        "schema": DETAIL_KIND_SCHEMA["analysis_series"],
        "owner_id": analysis_id,
        "owner_kind": "analysis",
        "source_id": source_id,
        "series": dict(series),
        "time_basis": dict(time_basis),
        "interval": dict(interval),
        "count": sum(int(item.get("count") or 0) for item in series.values()
                     if isinstance(item, Mapping)),
        "truncated": False,
        "content_sha256": content_sha256({str(key): dict(value)
                                          for key, value in series.items()
                                          if isinstance(value, Mapping)}),
        "fetch": _fetch({"kind": "analysis_series", "id": analysis_id}),
        **_binding(capture_id=capture_id, project_id=project_id,
                   revision=revision, integrity=integrity, record_path=record_path),
    }


def build_details_document(entries: Sequence[Mapping[str, Any]], *,
                           capture_id: str, project_id: str, revision: str,
                           integrity: str | None, window: Mapping[str, Any]) -> dict[str, Any]:
    ordered = sorted((dict(entry) for entry in entries), key=lambda row: str(row.get("ref")))
    counts: dict[str, int] = {}
    for entry in ordered:
        kind = str(entry.get("kind"))
        counts[kind] = counts.get(kind, 0) + 1
    return {
        "schema": DETAILS_SCHEMA,
        "fetch_tool": DETAIL_FETCH_TOOL,
        "contract": (
            "Each entry is an address for exact rows held in the canonical capture "
            "record: ref names the owner, schema, units, time basis, interval, row "
            "count, completeness and content hash. A reference is never a permission "
            "to drop evidence; reaper_panorama_query returns the exact stored rows, "
            "verifies this content hash and fails closed when the record, revision or "
            "hash does not match."
        ),
        "request_schema": {
            "kind": sorted(DETAIL_KINDS),
            "id": "the ref's owner_id (or its bare source/take guid)",
            "start_s": "optional interval start on the project timeline",
            "end_s": "optional interval end on the project timeline",
            "series": "analysis_series only: list of series names; default all",
            "resolution_s": "analysis_series only: aggregate exact cells into buckets",
        },
        "interval": DETAIL_INTERVAL,
        "capture_id": capture_id,
        "project_id": project_id,
        "revision_fingerprint": revision,
        "record_integrity_sha256": integrity,
        "window": dict(window),
        "counts": {"refs": len(ordered), **{f"by_kind_{k}": v for k, v in sorted(counts.items())}},
        "entries": ordered,
    }


# -- resolution ---------------------------------------------------------------


def _notes_of(record: Any, take_guid: str) -> list[dict[str, Any]]:
    project_map = getattr(record, "project_map", None) or {}
    for entry in project_map.get("midi") or []:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("take_guid") or "") != take_guid:
            continue
        events = entry.get("events")
        events = events if isinstance(events, Mapping) else {}
        notes = events.get("notes")
        return [dict(note) for note in notes or [] if isinstance(note, Mapping)]
    return []


def _envelopes_of(record: Any, domain: str) -> list[Mapping[str, Any]]:
    project_map = getattr(record, "project_map", None) or {}
    raw = project_map.get(domain)
    if not isinstance(raw, Mapping):
        return []
    rows: list[Mapping[str, Any]] = []
    for key in ("master", "fx", "items"):
        for entry in raw.get(key) or []:
            if isinstance(entry, Mapping):
                rows.append(entry)
    for entry in raw.get("tracks") or []:
        if not isinstance(entry, Mapping):
            continue
        for envelope in entry.get("envelopes") or []:
            if isinstance(envelope, Mapping):
                rows.append({"track_guid": entry.get("track_guid"), "envelope": envelope})
    return rows


def _find_envelope(record: Any, locator: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Locate one envelope with the same traversal the panorama uses."""
    domain = str(locator.get("domain") or "")
    guid = locator.get("envelope_guid")
    name = locator.get("name")
    owner = locator.get("owner_guid")
    project_map = getattr(record, "project_map", None) or {}
    raw = project_map.get(domain)
    if not isinstance(raw, Mapping):
        return None
    master_guid = None
    master = project_map.get("master")
    if isinstance(master, Mapping):
        master_guid = master.get("guid")

    def match(envelope: Any, owner_guid: Any) -> bool:
        if not isinstance(envelope, Mapping):
            return False
        if owner is not None and str(owner_guid or "") != str(owner):
            return False
        if guid is not None:
            return str(envelope.get("guid") or "") == str(guid)
        return name is not None and envelope.get("name") == name

    for envelope in raw.get("master") or []:
        if match(envelope, master_guid):
            return envelope
    for entry in raw.get("fx") or []:
        if not isinstance(entry, Mapping):
            continue
        owner_guid = entry.get("track_guid") or entry.get("owner_guid")
        for envelope in entry.get("envelopes") or []:
            if match(envelope, owner_guid):
                return envelope
    for entry in raw.get("items") or []:
        if not isinstance(entry, Mapping):
            continue
        for envelope in entry.get("envelopes") or []:
            if match(envelope, None):
                return envelope
    for entry in raw.get("tracks") or []:
        if not isinstance(entry, Mapping):
            continue
        for envelope in entry.get("envelopes") or []:
            if match(envelope, entry.get("track_guid")):
                return envelope
    return None


def _analysis_of(record: Any, source_guid: str) -> Mapping[str, Any] | None:
    analysis = getattr(record, "analysis", None) or {}
    report = analysis.get(source_guid)
    return report if isinstance(report, Mapping) else None


def _in_window(time_s: Any, start: float | None, end: float | None) -> bool:
    value = _number(time_s)
    if value is None:
        return False
    if start is not None and value < start:
        return False
    if end is not None and value >= end:
        return False
    return True


def _bucketise(times: Sequence[float], values: Sequence[float], width: float) -> list[dict[str, Any]]:
    if not times:
        return []
    start, end = float(times[0]), float(times[-1])
    out: list[dict[str, Any]] = []
    cursor = start
    while cursor < end + 1e-12:
        stop = min(end, cursor + width)
        picked = sorted(float(v) for t, v in zip(times, values)
                        if cursor <= t < stop or
                        (stop >= end and abs(t - end) <= 1e-12))
        if picked:
            n = len(picked)
            out.append({"start_s": cursor, "end_s": stop, "count": n,
                        "min": picked[0], "max": picked[-1],
                        "mean": sum(picked) / n,
                        "p50": picked[max(0, int(round(0.5 * (n - 1))))]})
        cursor = stop
        if stop >= end:
            break
    return out


def series_payload(report: Mapping[str, Any], name: str) -> tuple[Any, dict[str, Any]]:
    raw_curves = report.get("curves")
    curves: Mapping[str, Any] = raw_curves if isinstance(raw_curves, Mapping) else {}
    if name == "rms_400ms":
        times = curves.get("rms_400ms_time_s")
        values = curves.get("rms_400ms_dbfs")
        return ({"times_s": list(times or []), "values": list(values or [])},
                {"units": "dBFS", "value_kind": "scalar_per_cell"})
    if name == "band_level":
        times = curves.get("spectrogram_time_s")
        values = curves.get("band_level_time_dbfs")
        rows = [list(row) for row in values or [] if isinstance(row, (list, tuple))]
        return ({"times_s": list(times or []), "values": rows},
                {"units": "dBFS", "value_kind": "band_vector_per_cell",
                 "band_centers_hz": list(curves.get("band_centers_hz") or [])})
    if name == "temporal_profile":
        raw_profile = curves.get("temporal_profile")
        profile: Mapping[str, Any] = raw_profile if isinstance(raw_profile, Mapping) else {}
        raw_cells = profile.get("cells") or []
        return ({"times_s": list(profile.get("times") or []),
                 "values": [dict(cell) for cell in raw_cells if isinstance(cell, Mapping)]},
                {"units": None, "value_kind": "cell_record_per_cell",
                 "cell_fields": sorted({key for cell in raw_cells
                                        if isinstance(cell, Mapping) for key in cell})})
    raise ValidationError("unknown analysis series", details={"series": name})


def _hash_floats(values: Sequence[Any], digest: Any) -> None:
    numeric = [float(value) for value in values]
    digest.update(struct.pack("<Q", len(numeric)))
    buffer = array("d", numeric)
    if sys.byteorder != "little":
        buffer.byteswap()
    digest.update(buffer.tobytes())


def series_content_sha256(payload: Mapping[str, Any]) -> str:
    """Fast deterministic hash of an exact series payload.

    JSON canonicalisation of a band matrix (millions of floats) dominates the
    build time of a long capture, so series are hashed as fixed-width floats.
    The build and the resolver use this same function, so the check stays exact.
    """
    rows = payload.get("values") or []
    first = rows[0] if rows else None
    if isinstance(first, Mapping) or (rows and not isinstance(first, (list, tuple, int, float))):
        # Structured cells (for example a temporal profile) are small; keep the
        # canonical JSON hash so every field stays covered.
        return content_sha256({"times_s": payload.get("times_s") or [],
                               "values": rows})
    digest = hashlib.sha256()
    digest.update(b"series/v1")
    _hash_floats(payload.get("times_s") or [], digest)
    if isinstance(first, (list, tuple)):
        digest.update(b"matrix")
        digest.update(struct.pack("<Q", len(rows)))
        for row in rows:
            _hash_floats(row, digest)
    else:
        digest.update(b"values")
        _hash_floats(rows, digest)
    return digest.hexdigest()


def series_entry(report: Mapping[str, Any], name: str) -> dict[str, Any]:
    """Describe one exact series without returning it (count, hash, units)."""
    payload, meta = series_payload(report, name)
    raw_curves = report.get("curves")
    curves = raw_curves if isinstance(raw_curves, Mapping) else {}
    times = payload.get("times_s") or []
    entry: dict[str, Any] = {
        "count": len(times),
        "content_sha256": series_content_sha256(payload),
        "units": meta.get("units"),
        "value_kind": meta.get("value_kind"),
    }
    if name == "band_level":
        entry["band_count"] = len(meta.get("band_centers_hz") or [])
    series_meta = curves.get("series_meta")
    if isinstance(series_meta, Mapping) and isinstance(series_meta.get(name), Mapping):
        entry.update({key: value for key, value in series_meta[name].items()
                      if key in ("window_s", "hop_s", "decimation_step", "cell_fields")})
    return entry


def _locator(entry: Mapping[str, Any]) -> dict[str, Any]:
    raw = entry.get("locator")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _resolve_midi(record: Any, entry: Mapping[str, Any], request: Mapping[str, Any],
                  start: float | None, end: float | None) -> dict[str, Any]:
    take_guid = str(entry.get("owner_id") or "").split(":", 1)[-1]
    rows = _notes_of(record, take_guid)
    actual = content_sha256(rows)
    if actual != entry.get("content_sha256"):
        raise PanoramaBindingError(
            "detail reference does not match the canonical record",
            details={"ref": entry.get("ref"), "expected": entry.get("content_sha256"),
                     "actual": actual})
    selected = []
    for note in rows:
        if not _in_window(note.get("start_s"), start, end):
            continue
        row = dict(note)
        note_start = _number(note.get("start_s"))
        note_end = _number(note.get("end_s"))
        row["note_duration_s"] = (note_end - note_start
                                  if note_start is not None and note_end is not None
                                  else None)
        start_ppq = _number(note.get("start_ppq"))
        end_ppq = _number(note.get("end_ppq"))
        row["note_duration_qn"] = (end_ppq - start_ppq
                                   if start_ppq is not None and end_ppq is not None
                                   else None)
        selected.append(row)
    return {
        "ref": entry.get("ref"), "kind": "midi_notes", "schema": entry.get("schema"),
        "derived_fields": ["note_duration_s", "note_duration_qn"],
        "derived_basis": ("note_duration_s = end_s - start_s; note_duration_qn = "
                          "end_ppq - start_ppq; both are computed from the stored rows"),
        "owner_id": entry.get("owner_id"), "item_id": entry.get("item_id"),
        "track_id": entry.get("track_id"),
        "time_basis": entry.get("time_basis"),
        "interval": _interval(start, end) if start is not None or end is not None
        else entry.get("interval"),
        "count": entry.get("count"), "returned": len(selected),
        "truncated": bool(entry.get("truncated")),
        "content_sha256": actual, "matches_manifest": True,
        "notes": selected,
    }


def _resolve_envelope(record: Any, entry: Mapping[str, Any], request: Mapping[str, Any],
                      start: float | None, end: float | None) -> dict[str, Any]:
    locator = _locator(entry)
    envelope = _find_envelope(record, locator)
    if envelope is None:
        raise PanoramaBindingError(
            "envelope detail reference is not present in the canonical record",
            details={"ref": entry.get("ref")})
    rows = [dict(point) for point in envelope.get("points") or []
            if isinstance(point, Mapping)]
    actual = content_sha256(rows)
    if actual != entry.get("content_sha256"):
        raise PanoramaBindingError(
            "detail reference does not match the canonical record",
            details={"ref": entry.get("ref"), "expected": entry.get("content_sha256"),
                     "actual": actual})
    selected = [point for point in rows if _in_window(point.get("time_s"), start, end)]
    return {
        "ref": entry.get("ref"), "kind": "envelope_points", "schema": entry.get("schema"),
        "owner_id": entry.get("owner_id"), "envelope_id": entry.get("envelope_id"),
        "name": entry.get("name"), "target": entry.get("target"),
        "time_basis": entry.get("time_basis"),
        "interval": _interval(start, end) if start is not None or end is not None
        else entry.get("interval"),
        "count": entry.get("count"), "returned": len(selected),
        "truncated": False, "content_sha256": actual, "matches_manifest": True,
        "points": selected,
    }


def _resolve_series(record: Any, entry: Mapping[str, Any], request: Mapping[str, Any],
                    start: float | None, end: float | None) -> dict[str, Any]:
    source_guid = str(entry.get("owner_id") or "").split(":", 1)[-1]
    report = _analysis_of(record, source_guid)
    if report is None:
        raise PanoramaBindingError(
            "analysis detail reference is not present in the canonical record",
            details={"ref": entry.get("ref")})
    available_series = entry.get("series")
    available: dict[str, Any] = ({str(key): value for key, value in available_series.items()
                                  if isinstance(value, Mapping)}
                                 if isinstance(available_series, Mapping) else {})
    requested = request.get("series")
    if requested is None:
        names = list(available)
    elif isinstance(requested, list) and all(isinstance(item, str) for item in requested):
        names = requested
    else:
        raise ValidationError("series must be a list of series names")
    resolution = _number(request.get("resolution_s"))
    if resolution is not None and resolution <= 0:
        raise ValidationError("resolution_s must be positive")

    output: dict[str, Any] = {}
    total_points = 0
    for name in names:
        if name not in available:
            raise ValidationError("series is not present for this source",
                                  details={"series": name,
                                           "available": sorted(available)})
        payload, meta = series_payload(report, name)
        times = [float(value) for value in payload.get("times_s") or []]
        values = payload.get("values") or []
        pairs = list(zip(times, values))
        if start is not None or end is not None:
            pairs = [(t, v) for t, v in pairs if _in_window(t, start, end)]
        selected_times = [t for t, _ in pairs]
        selected_values = [v for _, v in pairs]
        actual = series_content_sha256(payload)
        if actual != available[name].get("content_sha256"):
            raise PanoramaBindingError(
                "series detail reference does not match the canonical record",
                details={"ref": name, "expected": available[name].get("content_sha256"),
                         "actual": actual})
        total_points += len(selected_times)
        if total_points > MAX_DETAIL_POINTS:
            raise ValidationError(
                "detail request exceeds the point budget; pass resolution_s or a narrower interval",
                details={"limit": MAX_DETAIL_POINTS, "requested_series": names,
                         "next_action": "retry with resolution_s or a smaller [start_s,end_s)"})
        item: dict[str, Any] = {
            "name": name, "units": meta.get("units"),
            "value_kind": meta.get("value_kind"),
            "count": len(selected_times), "content_sha256": actual,
            "matches_manifest": True, "time_basis": entry.get("time_basis"),
        }
        if name == "band_level":
            item["band_centers_hz"] = meta.get("band_centers_hz")
        if name == "temporal_profile":
            item["cell_fields"] = meta.get("cell_fields")
        if resolution is not None and name in {"rms_400ms", "band_level"}:
            item["resolution_s"] = resolution
            item["aggregation"] = "mean_min_max_p50" if name == "rms_400ms" else "mean"
            if name == "rms_400ms":
                item["buckets"] = _bucketise(selected_times,
                                             [float(v) for v in selected_values], resolution)
            else:
                width = resolution
                buckets = []
                cursor = selected_times[0] if selected_times else 0.0
                stop_at = selected_times[-1] if selected_times else 0.0
                while cursor < stop_at + 1e-12 and selected_values:
                    stop = min(stop_at, cursor + width)
                    cells = [row for t, row in zip(selected_times, selected_values)
                             if cursor <= t < stop or
                             (stop >= stop_at and abs(t - stop_at) <= 1e-12)]
                    if cells:
                        width_bands = min(len(row) for row in cells)
                        buckets.append({
                            "start_s": cursor, "end_s": stop, "count": len(cells),
                            "mean": [sum(float(row[b]) for row in cells) / len(cells)
                                     for b in range(width_bands)],
                        })
                    cursor = stop
                    if stop >= stop_at:
                        break
                item["buckets"] = buckets
        else:
            item["times_s"] = selected_times
            item["values"] = selected_values
        output[name] = item
    return {
        "ref": entry.get("ref"), "kind": "analysis_series", "schema": entry.get("schema"),
        "owner_id": entry.get("owner_id"), "source_id": entry.get("source_id"),
        "interval": _interval(start, end) if start is not None or end is not None
        else entry.get("interval"),
        "series": output, "matches_manifest": True,
    }


def _normalise_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise ValidationError("each detail request must be an object")
    kind = request.get("kind")
    if kind not in DETAIL_KINDS:
        raise ValidationError("unknown detail kind",
                              details={"kind": kind, "allowed": sorted(DETAIL_KINDS)})
    identifier = request.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise ValidationError("detail request needs a non-empty id")
    start = _number(request.get("start_s"))
    end = _number(request.get("end_s"))
    if request.get("start_s") is not None and start is None:
        raise ValidationError("start_s must be a finite number")
    if request.get("end_s") is not None and end is None:
        raise ValidationError("end_s must be a finite number")
    if start is not None and end is not None and not start < end:
        raise ValidationError("start_s must be less than end_s")
    return {"kind": kind, "id": identifier, "start_s": start, "end_s": end,
            "series": request.get("series"), "resolution_s": request.get("resolution_s")}


def _match_entry(entries: Sequence[Mapping[str, Any]], kind: str,
                 identifier: str) -> Mapping[str, Any] | None:
    for entry in entries:
        if entry.get("kind") != kind:
            continue
        owner = entry.get("owner_id")
        if identifier == owner or identifier == entry.get("ref"):
            return entry
        locator = _locator(entry)
        for key in ("take_guid", "source_guid", "envelope_guid"):
            if locator.get(key) and str(locator[key]) == identifier:
                return entry
    return None


def resolve_details(record: Any, document: Mapping[str, Any],
                    requests: Sequence[Any], *, max_bytes: int = DEFAULT_DETAIL_BYTES) -> dict[str, Any]:
    """Return exact stored rows for a batch of typed detail requests.

    Every request is checked against the panorama's detail manifest and the
    returned rows are hashed against the manifest entry.  A mismatch raises
    rather than returning data the model could mistake for the captured facts.
    """

    details = document.get("details")
    if not isinstance(details, Mapping):
        raise ValidationError("this panorama has no detail manifest")
    entries = [entry for entry in details.get("entries") or [] if isinstance(entry, Mapping)]
    if isinstance(requests, Mapping):
        requests = [requests]
    normalised = [_normalise_request(request) for request in requests or []]
    if not normalised:
        raise ValidationError("at least one detail request is required")
    results: list[dict[str, Any]] = []
    for request in normalised:
        entry = _match_entry(entries, str(request["kind"]), str(request["id"]))
        if entry is None:
            raise ValidationError(
                "unknown detail reference",
                details={"kind": request["kind"], "id": request["id"],
                         "next_action": ("read details.entries in the panorama and use an "
                                         "entry's owner_id or ref")})
        if request["kind"] == "midi_notes":
            results.append(_resolve_midi(record, entry, request,
                                         request["start_s"], request["end_s"]))
        elif request["kind"] == "envelope_points":
            results.append(_resolve_envelope(record, entry, request,
                                             request["start_s"], request["end_s"]))
        else:
            results.append(_resolve_series(record, entry, request,
                                           request["start_s"], request["end_s"]))
    payload = {
        "schema_version": DETAILS_SCHEMA,
        "status": "detail",
        "capture_id": document.get("capture_binding", {}).get("capture_id")
        if isinstance(document.get("capture_binding"), Mapping) else None,
        "project_id": details.get("project_id"),
        "revision_fingerprint": details.get("revision_fingerprint"),
        "document_sha256": document.get("document_sha256"),
        "scope": {"kind": "typed_ids", "requests": len(results)},
        "results": results,
        "coverage": {
            "not_in_scope": [
                "the linked rows are exact captures of this revision; they are not a "
                "re-measurement and they do not extend beyond the capture window",
            ],
            "interval": details.get("interval"),
        },
    }
    encoded = canonical(payload)
    if len(encoded) > max_bytes:
        return {
            "schema_version": DETAILS_SCHEMA,
            "status": "detail_too_large",
            "bytes": len(encoded),
            "max_bytes": max_bytes,
            "requests": [{"kind": request["kind"], "id": request["id"]}
                         for request in normalised],
            "next_call": {"tool": DETAIL_FETCH_TOOL,
                          "hint": "request fewer ids, a narrower interval, or a coarser resolution_s"},
        }
    return payload


__all__ = [
    "DETAILS_SCHEMA",
    "DETAIL_FETCH_TOOL",
    "DETAIL_KIND_SCHEMA",
    "DETAIL_KINDS",
    "DEFAULT_DETAIL_BYTES",
    "MAX_DETAIL_POINTS",
    "canonical",
    "content_sha256",
    "revision_fingerprint",
    "build_details_document",
    "series_payload",
    "series_entry",
    "series_content_sha256",
    "midi_reference",
    "envelope_reference",
    "series_reference",
    "resolve_details",
]
