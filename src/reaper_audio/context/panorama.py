"""Self-describing, bounded panorama for one full capture.

The canonical ``record.json`` remains the source of truth.  This module builds a
deterministic, LLM-readable projection that joins the project map, capture
audio, analysis, revisions and cross-object links in one document.  Dense
analysis series are represented as multi-resolution summaries; the original
series stays in the archive and is addressed by an explicit pointer and hash.

The projection never invents a musical role, genre, quality verdict, or causal
explanation.  Those are separate epistemic layers and are empty unless another
producer supplied them.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ..errors import PanoramaBindingError, PanoramaCapacityError, ValidationError
from ..analysis.relations import (
    band_dominance_candidates,
    merge_batches,
    reference_candidates,
    track_master_candidates,
    RelationBatch,
)
from .details import (
    build_details_document,
    envelope_reference,
    midi_reference,
    revision_fingerprint,
    series_entry,
    series_reference,
)
from .relations import RelationBinding, RelationWindow, build_relation_set
from .identity import identity_from_mapping
from .render_dependencies import project_render_manifest

PANORAMA_SCHEMA_VERSION = "reaper-workbench.capture-panorama/2"
# Captures written before the detail-reference representation keep their sealed
# sidecar untouched; readers upgrade them in memory from the canonical record.
PANORAMA_LEGACY_SCHEMA_VERSIONS = frozenset({"reaper-workbench.capture-panorama/1"})
PANORAMA_FILE_NAME = "panorama.json"
# One-shot model context budget.  The canonical record remains complete on
# disk; this is the bounded derived projection returned to the model.
DEFAULT_MAX_TOKENS = 256_000
# Deliberately conservative: a real BPE measured ~3.07 bytes/token on compact
# panorama JSON; 2.2 bytes/token is kept as a safety margin.
TOKEN_BYTES_ESTIMATE = 2.2
DEFAULT_MAX_BYTES = 700_000
MIDI_NOTE_LIMIT = 10000
FILE_DURATION_TOLERANCE_S = 0.001
# Display budget for the inline song map.  It is a presentation bound, not a
# taste constant: at most this many buckets per source, never more buckets than
# source cells.  Exact cells at any resolution come from the detail reference.
INLINE_MAP_BUCKETS = 24
ANALYSIS_METRIC_KEYS = frozenset({
    "integrated_lufs", "peak_dbfs", "rms_dbfs", "crest_db",
    "true_peak_estimate_dbfs", "spectral_centroid_hz", "stereo_correlation",
    "transient_density_per_s", "loudness_range_lu",
    # The remaining scalar measurements of ``measure_audio`` were measured but
    # never reached the document, so no reader could see them.  A measured value
    # with no path to the model is a dropped domain; keep every one of them.
    "clipped_sample_ratio", "transient_count", "side_mid_db", "mono_fold_loss_db",
    "modulation_rate_hz", "modulation_depth", "pitch_jitter_pct",
    "coarse_speech_band_ratio", "dc_offset", "clipped_sample_count",
    "two_ms_event_count", "onset_count", "spike_count", "clipping_event_count",
    "local_tempo_median_bpm",
})
INTERVAL_CONVENTION = "[start_s,end_s)"
ANALYSIS_METRIC_COLUMNS = tuple(sorted(ANALYSIS_METRIC_KEYS))
ANALYSIS_METRIC_UNITS = {
    "integrated_lufs": "LUFS", "peak_dbfs": "dBFS", "rms_dbfs": "dBFS",
    "crest_db": "dB", "true_peak_estimate_dbfs": "dBTP",
    "spectral_centroid_hz": "Hz", "stereo_correlation": "correlation",
    "transient_density_per_s": "events/s", "loudness_range_lu": "LU",
    "clipped_sample_ratio": "ratio", "transient_count": "count",
    "side_mid_db": "dB", "mono_fold_loss_db": "dB",
    "modulation_rate_hz": "Hz", "modulation_depth": "ratio",
    "pitch_jitter_pct": "%", "coarse_speech_band_ratio": "ratio",
    "dc_offset": "FS", "clipped_sample_count": "samples",
    "two_ms_event_count": "events", "onset_count": "events",
    "spike_count": "events", "clipping_event_count": "events",
    "local_tempo_median_bpm": "BPM",
}
TIME_BASIS_PROJECT = {
    "start_s": "project_seconds",
    "end_s": "project_seconds",
    "ppq": "ppq_from_reaper_midi_get_note",
    "interval": INTERVAL_CONVENTION,
}


def _json_default(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return dict(value)
    if isinstance(value, tuple):
        return list(value)
    if type(value).__module__.startswith("numpy") and hasattr(value, "item"):
        return value.item()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_json_default,
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def estimate_tokens(text: str | bytes) -> int:
    """Conservative token estimate used before an actual tokenizer is called."""
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return max(1, math.ceil(len(raw) / TOKEN_BYTES_ESTIMATE))


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
    a = _number(start)
    b = _number(end)
    if a is None or b is None:
        return {"start_s": None, "end_s": None, "duration_s": None,
                "interval": INTERVAL_CONVENTION, "units": "s",
                "available": False}
    return {
        "start_s": a,
        "end_s": b,
        "duration_s": max(0.0, b - a),
        "interval": INTERVAL_CONVENTION,
        "units": "s",
        "available": True,
    }


def _point(time_s: Any) -> dict[str, Any]:
    value = _number(time_s)
    return {"time_s": value, "units": "s", "basis": "project_seconds"}


def _stable(prefix: str, *parts: Any) -> str:
    raw = ":".join(str(part) for part in parts)
    return f"{prefix}:{raw}"


def _record_dict(record: Any) -> dict[str, Any]:
    """Return a shallow record mapping without thawing 460 MB of analysis.

    ``FullCaptureRecord.to_mapping`` keeps the sealed analysis and project map
    as immutable shared structures.  The builder only reads them; the archive
    annotates a private list of audio entries.  A plain mapping (including the
    in-memory archive mapping) is accepted as-is.
    """
    if isinstance(record, Mapping):
        data = dict(record)
    elif hasattr(record, "to_mapping"):
        data = record.to_mapping()
    elif hasattr(record, "to_dict"):
        data = record.to_dict()
    else:
        raise ValidationError("panorama requires a full-capture record")
    if not isinstance(data, Mapping):
        raise ValidationError("full-capture record did not serialize to a mapping")
    return dict(data)


def _project_map(data: Mapping[str, Any]) -> Mapping[str, Any]:
    value = data.get("project_map")
    if not isinstance(value, Mapping):
        raise ValidationError("full-capture record has no project_map")
    return value


def _audio_items(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    value = data.get("audio")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _analysis_map(data: Mapping[str, Any]) -> Mapping[str, Any]:
    value = data.get("analysis")
    return value if isinstance(value, Mapping) else {}


def _project_settings(project_map: Mapping[str, Any]) -> Mapping[str, Any]:
    digest = project_map.get("digest")
    fields = digest.get("fields") if isinstance(digest, Mapping) else None
    project = fields.get("project") if isinstance(fields, Mapping) else None
    settings = project.get("settings") if isinstance(project, Mapping) else None
    return settings if isinstance(settings, Mapping) else {}


def _item_views(data: Mapping[str, Any], project_map: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    tracks = project_map.get("tracks") if isinstance(project_map.get("tracks"), list) else []
    track_of_item: dict[str, str] = {}
    for track in tracks:
        if not isinstance(track, Mapping):
            continue
        track_guid = track.get("guid")
        if not isinstance(track_guid, str):
            continue
        for item in track.get("items") or []:
            if isinstance(item, Mapping) and item.get("guid"):
                track_of_item[str(item["guid"])] = track_guid
    for raw in project_map.get("items") or []:
        if not isinstance(raw, Mapping):
            continue
        guid = str(raw.get("guid") or "")
        if not guid or guid in seen:
            continue
        seen.add(guid)
        track_guid = raw.get("track_guid") or track_of_item.get(guid)
        position = _number(raw.get("position_s"))
        length = _number(raw.get("length_s"))
        item_id = _stable("item", guid)
        markers = []
        for marker in raw.get("stretch_marker_positions") or []:
            if isinstance(marker, Mapping):
                markers.append({
                    "pos_s": _number(marker.get("pos")),
                    "source_pos_s": _number(marker.get("srcpos")),
                })
        view = {
            "item_id": item_id,
            "guid": guid,
            "track_id": _stable("track", track_guid) if track_guid else None,
            "position_s": position,
            "length_s": length,
            # Edit-critical geometry: a pan, offset, playrate, fade or
            # channel-mode edit must be visible and comparable in the model's
            # own document, not only in the full record.
            "start_offset_s": _number(raw.get("start_offset_s")),
            "playrate": _number(raw.get("playrate")),
            "mute": bool(raw.get("mute")),
            "fade_in_s": _number(raw.get("fade_in_s")),
            "fade_out_s": _number(raw.get("fade_out_s")),
            "channel_mode": _integer(raw.get("channel_mode")),
            "group_id": _integer(raw.get("group_id")),
            "loop_source": bool(raw.get("loop_source")),
            "beat_attach_mode": _integer(raw.get("beat_attach_mode")),
            "stretch_marker_count": len(markers),
            "stretch_markers": markers,
            "item_duration_s": length,
            "item_duration_basis": "project_item_length_s",
            "interval": _interval(position, (position + length)
                                  if position is not None and length is not None else None),
            "take_count": _integer(raw.get("take_count")) or 0,
            "active_take_id": None,
            "midi_id": None,
        }
        active = raw.get("active_take")
        if isinstance(active, Mapping) and active.get("guid"):
            view["active_take_id"] = _stable("take", str(active["guid"]))
        rows.append(view)
    return rows


def _take_views(items: list[Mapping[str, Any]],
                project_map: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        # The item view does not carry the full nested active take; recover it
        # from the project-map items by GUID.
        pass
    raw_items = project_map.get("items") if isinstance(project_map.get("items"), list) else []
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        active = raw.get("active_take")
        if not isinstance(active, Mapping):
            continue
        guid = str(active.get("guid") or "")
        if not guid or guid in seen:
            continue
        seen.add(guid)
        item_id = _stable("item", str(raw.get("guid") or ""))
        track_guid = raw.get("track_guid")
        if track_guid:
            track_id = _stable("track", track_guid)
        else:
            # Recover through the item view when the flattened project map
            # omitted track_guid. Item views already carry the canonical alias.
            track_id = next((view.get("track_id") for view in items
                             if view.get("item_id") == item_id), None)
        markers = active.get("stretch_markers")
        marker_count = len(markers) if isinstance(markers, (list, tuple)) else _integer(markers) or 0
        rows.append({
            "take_id": _stable("take", guid),
            "item_id": item_id,
            "track_id": track_id,
            "name": active.get("name"),
            "is_midi": bool(active.get("is_midi")),
            "source_media_path": active.get("source"),
            "playrate": _number(active.get("playrate")),
            "preserve_pitch": bool(active.get("preserve_pitch")),
            "stretch_marker_count": marker_count,
            "fx_count": _integer(active.get("fx_count")) or 0,
            "channel_mode": _integer(raw.get("channel_mode")),
            "group_id": _integer(raw.get("group_id")),
            "midi_id": None,
        })
    return rows


def _midi_views(items: list[Mapping[str, Any]], takes: list[Mapping[str, Any]],
                project_map: Mapping[str, Any], *,
                detail_ctx: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One navigational summary per MIDI take, plus a typed reference to notes.

    Notes themselves stay in the canonical record.  The summary keeps the owner
    chain, exact time basis, population, occupied interval, pitch/velocity
    bounds and truncation state so the model knows whether a reference is worth
    fetching and what it can contain.
    """
    take_ids = {str(t["guid"]): str(t["take_id"]) for t in takes
                if isinstance(t, Mapping) and t.get("guid")}
    item_by_take: dict[str, str] = {}
    for raw in project_map.get("items") or []:
        if not isinstance(raw, Mapping):
            continue
        active = raw.get("active_take")
        if isinstance(active, Mapping) and active.get("guid"):
            item_by_take[str(active["guid"])] = _stable("item", str(raw.get("guid") or ""))
    rows: list[dict[str, Any]] = []
    raw_midi = project_map.get("midi") if isinstance(project_map.get("midi"), list) else []
    for entry in raw_midi:
        if not isinstance(entry, Mapping):
            continue
        take_guid = str(entry.get("take_guid") or "")
        if not take_guid:
            continue
        events = entry.get("events")
        if not isinstance(events, Mapping):
            events = {}
        notes_raw = events.get("notes") if isinstance(events.get("notes"), list) else []
        notes = [note for note in notes_raw if isinstance(note, Mapping)]
        returned = _integer(events.get("returned_note_count"))
        note_count = _integer(events.get("note_count"))
        truncated = bool(events.get("truncated"))
        if returned is None:
            returned = len(notes)
        if note_count is None:
            note_count = returned
        midi_id = _stable("midi", take_guid)
        take_id = take_ids.get(take_guid) or _stable("take", take_guid)
        item_id = item_by_take.get(take_guid)
        track_id = next((str(t.get("track_id")) for t in takes
                         if isinstance(t, Mapping) and str(t.get("guid")) == take_guid
                         and t.get("track_id")), None)
        reference = midi_reference(
            capture_id=str(detail_ctx["capture_id"]),
            project_id=str(detail_ctx["project_id"]),
            revision=str(detail_ctx["revision"]),
            integrity=detail_ctx["integrity"],
            record_path=detail_ctx["record_path"],
            take_guid=take_guid, take_id=take_id, item_id=item_id,
            track_id=track_id, notes=notes,
            note_count=note_count, returned_count=returned,
            truncated=truncated or note_count > returned,
            time_basis=TIME_BASIS_PROJECT,
        )
        detail_ctx["refs"].append(reference)
        occupied = reference["occupied_interval"]
        duration = occupied.get("duration_s") or 0.0
        rows.append({
            "midi_id": midi_id,
            "take_id": take_id,
            "item_id": item_id,
            "track_id": track_id,
            "fact_class": "project_fact",
            "source": "reaper_project_map",
            "time_basis": TIME_BASIS_PROJECT,
            "note_count": note_count,
            "returned_note_count": returned,
            "truncated": bool(truncated or note_count > returned),
            "midi_note_limit": MIDI_NOTE_LIMIT,
            "occupied_interval": occupied,
            "pitch_range": reference["pitch_range"],
            "velocity_range": reference["velocity_range"],
            "channels": reference["channels"],
            "density_per_s": (round(len(notes) / duration, 6) if duration > 0 else None),
            "density_basis": "returned_notes_over_occupied_interval",
            "detail": {
                "ref": reference["ref"],
                "kind": reference["kind"],
                "schema": reference["schema"],
                "count": reference["count"],
                "truncated": reference["truncated"],
                "fetch": reference["fetch"],
            },
        })
    return rows


def _fx_views(tracks: Sequence[Mapping[str, Any]],
              master: Mapping[str, Any], master_guid: str | None,
              master_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def add(owner_id: str, owner_guid: str | None, fx_list: Any,
            is_master: bool, owner_index: int | None = None) -> None:
        if not isinstance(fx_list, Sequence) or isinstance(fx_list, (str, bytes)):
            return
        for raw in fx_list:
            if not isinstance(raw, Mapping):
                continue
            slot = _integer(raw.get("index"))
            fx_guid = raw.get("guid")
            fx_id = _stable("fx", owner_guid or owner_id, slot, fx_guid or "")
            params = raw.get("parameters")
            if not isinstance(params, list):
                params = raw.get("params") if isinstance(raw.get("params"), list) else []
            pointer = (f"#/project_map/tracks/{owner_index}/fx/{slot}/parameters"
                       if not is_master else
                       f"#/project_map/master/fx/{slot}/parameters")
            parameter_rows = []
            for param in params:
                if not isinstance(param, Mapping):
                    continue
                parameter_rows.append({
                    "index": _integer(param.get("index")),
                    "name": param.get("name"),
                    "normalized": _number(param.get("normalized")),
                    "formatted": param.get("formatted"),
                })
            rows.append({
                "fx_id": fx_id,
                "owner_id": owner_id,
                "is_master": is_master,
                "slot_index": slot,
                "slot_order": slot,
                "guid": fx_guid,
                "name": raw.get("name"),
                "enabled": bool(raw.get("enabled")),
                "offline": bool(raw.get("offline")),
                "parameter_count": _integer(raw.get("parameter_count")) or len(params),
                "parameters": parameter_rows,
                "raw_parameters_pointer": pointer,
            })

    for track_index, track in enumerate(tracks):
        add(str(track.get("stable_id") or ""), str(track.get("guid") or ""),
            track.get("_raw_fx"), False,
            _integer(track.get("index")) if track.get("index") is not None else track_index)
    add(master_id, master_guid, master.get("_raw_fx"), True)
    return rows


def _digest_track_rows(project_map: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    digest = project_map.get("digest")
    fields = digest.get("fields") if isinstance(digest, Mapping) else None
    rows = fields.get("tracks") if isinstance(fields, Mapping) else None
    result: dict[str, Mapping[str, Any]] = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, Mapping) and isinstance(row.get("guid"), str):
                result[str(row["guid"])] = row
    return result


def _route_views(tracks: Sequence[Mapping[str, Any]],
                 master: Mapping[str, Any], master_id: str,
                 project_map: Mapping[str, Any]) -> list[dict[str, Any]]:
    digest_rows = _digest_track_rows(project_map)
    rows: list[dict[str, Any]] = []
    for track in tracks:
        guid = track.get("guid")
        sends = track.get("sends")
        if not isinstance(sends, Sequence) or isinstance(sends, (str, bytes)):
            source_row = digest_rows.get(str(guid)) if guid else None
            sends = source_row.get("sends") if isinstance(source_row, Mapping) else None
        if not isinstance(sends, Sequence) or isinstance(sends, (str, bytes)):
            continue
        for send in sends:
            if not isinstance(send, Mapping):
                continue
            index = _integer(send.get("index"))
            dest_guid = send.get("dest_guid")
            if index is None:
                continue
            rows.append({
                "route_id": _stable("route", guid or "", dest_guid or "", index),
                "source_id": track.get("stable_id"),
                "destination_id": (_stable("master", dest_guid)
                                   if str(dest_guid) == str(master.get("guid"))
                                   else _stable("track", dest_guid)),
                "send_index": index,
                "volume": _number(send.get("volume")),
                "pan": _number(send.get("pan")),
                "mute": bool(send.get("mute")),
            })
    return rows


def _envelope_rows(raw: Any, kind: str, tracks: Sequence[Mapping[str, Any]],
                   master_id: str, master_guid: str | None, *,
                   detail_ctx: Mapping[str, Any],
                   domain_key: str) -> list[dict[str, Any]]:
    """One navigational summary per envelope, plus a typed reference to points.

    Exact points stay in the canonical record.  The summary names the owner and
    target, the time basis, the active interval, the value range, the point
    count and the truncation state, so a model can decide whether to fetch the
    exact lane without reading it.
    """
    rows: list[dict[str, Any]] = []
    track_ids = {str(t.get("guid")): str(t.get("stable_id")) for t in tracks}
    if not isinstance(raw, Mapping):
        return rows

    def add(owner_id: str, owner_guid: str | None, envelope: Any,
            is_master: bool = False) -> None:
        if not isinstance(envelope, Mapping):
            return
        guid = envelope.get("guid")
        name = envelope.get("name")
        points = envelope.get("points") if isinstance(envelope.get("points"), list) else []
        point_rows = [point for point in points if isinstance(point, Mapping)]
        env_id = _stable(kind, owner_id, guid or name or len(rows))
        chunk = envelope.get("chunk")
        reference = envelope_reference(
            capture_id=str(detail_ctx["capture_id"]),
            project_id=str(detail_ctx["project_id"]),
            revision=str(detail_ctx["revision"]),
            integrity=detail_ctx["integrity"],
            record_path=detail_ctx["record_path"],
            envelope_id=env_id, domain=domain_key, owner_id=owner_id,
            owner_guid=owner_guid, envelope_guid=(str(guid) if guid else None),
            name=name, points=point_rows,
            point_count=_integer(envelope.get("point_count")) or len(point_rows),
            active=bool(envelope.get("active")),
            time_basis=TIME_BASIS_PROJECT,
        )
        detail_ctx["refs"].append(reference)
        rows.append({
            "envelope_id": env_id,
            "kind": kind,
            "owner_id": owner_id,
            "owner_guid": owner_guid,
            "is_master": is_master,
            "guid": guid,
            "name": name,
            "target": reference["target"],
            "active": bool(envelope.get("active")),
            "armed": bool(envelope.get("armed")),
            "visible": bool(envelope.get("visible")),
            "time_basis": TIME_BASIS_PROJECT,
            "point_count": reference["count"],
            "returned_point_count": reference["returned_count"],
            "interval": reference["interval"],
            "value_range": reference["value_range"],
            "shape_kinds": reference["shape_kinds"],
            "automation_item_count": _integer(envelope.get("automation_item_count")) or 0,
            "automation_item_interval": (
                _interval(
                    min((_number(item.get("start_s")) for item in
                         envelope.get("automation_items") or []
                         if isinstance(item, Mapping) and _number(item.get("start_s")) is not None),
                        default=None),
                    max((_number(item.get("end_s")) for item in
                         envelope.get("automation_items") or []
                         if isinstance(item, Mapping) and _number(item.get("end_s")) is not None),
                        default=None))
                if envelope.get("automation_items") else _interval(None, None)),
            "raw_chunk_included": False,
            "raw_chunk_sha256": _hash(chunk) if chunk is not None else None,
            "fact_class": "project_fact",
            "source": "reaper_project_map",
            "detail": {
                "ref": reference["ref"],
                "kind": reference["kind"],
                "schema": reference["schema"],
                "count": reference["count"],
                "truncated": False,
                "fetch": reference["fetch"],
            },
        })

    master_rows = raw.get("master") if isinstance(raw.get("master"), list) else []
    for env in master_rows:
        add(master_id, master_guid, env, True)
    for item in raw.get("tracks") or []:
        if not isinstance(item, Mapping):
            continue
        track_guid = item.get("track_guid")
        owner_id = track_ids.get(str(track_guid), _stable("track", track_guid or ""))
        envs = item.get("envelopes") if isinstance(item.get("envelopes"), list) else []
        for env in envs:
            add(owner_id, str(track_guid) if track_guid else None, env)
    for item in raw.get("fx") or []:
        if isinstance(item, Mapping):
            # FX envelopes are owned by their parent object when the bridge
            # does not provide a GUID.  Keep the raw owner reference.
            owner = str(item.get("track_guid") or item.get("owner_guid") or "")
            for env in item.get("envelopes") or []:
                add(_stable("track", owner) if owner else "fx", owner or None, env)
    for item in raw.get("items") or []:
        if isinstance(item, Mapping):
            item_guid = item.get("item_guid")
            for env in item.get("envelopes") or []:
                add(_stable("item", item_guid or ""), None, env)
    return rows


def _curve_levels(duration_s: float | None) -> list[float]:
    if duration_s is None or duration_s < 0:
        return [1.0, 5.0, 30.0]
    if duration_s <= 60.0:
        return [1.0, 5.0, 30.0]
    return [10.0, 60.0]


def _stats(values: Sequence[float]) -> dict[str, Any]:
    clean = [float(v) for v in values if isinstance(v, (int, float))
             and not isinstance(v, bool) and math.isfinite(float(v))]
    if not clean:
        return {"count": 0, "min": None, "max": None, "mean": None,
                "median": None, "p05": None, "p95": None, "units": None}
    ordered = sorted(clean)
    n = len(ordered)
    return {
        "count": n,
        "min": ordered[0],
        "max": ordered[-1],
        "mean": sum(ordered) / n,
        "median": ordered[n // 2] if n % 2 else
        (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0,
        "p05": ordered[max(0, int(round(0.05 * (n - 1))))],
        "p95": ordered[max(0, int(round(0.95 * (n - 1))))],
        "units": None,
    }


def _buckets(times: Sequence[float], values: Sequence[float],
             level_s: float) -> list[dict[str, Any]]:
    if not times or not values:
        return []
    start = float(times[0])
    end = float(times[-1])
    if end <= start:
        end = start + level_s
    result: list[dict[str, Any]] = []
    bucket_start = start
    while bucket_start < end + 1e-12:
        bucket_end = min(end, bucket_start + level_s)
        selected = [float(value) for time_s, value in zip(times, values)
                    if isinstance(time_s, (int, float)) and
                    isinstance(value, (int, float)) and
                    bucket_start <= float(time_s) < bucket_end]
        if selected:
            result.append({
                "start_s": bucket_start,
                "end_s": bucket_end,
                "duration_s": bucket_end - bucket_start,
                "interval": INTERVAL_CONVENTION,
                "units": "s",
                "value": sum(selected) / len(selected),
                "aggregation": "mean",
                "source_point_count": len(selected),
            })
        bucket_start = bucket_end
        if bucket_end >= end:
            break
    return result


def _time_summary(times: Any, values: Any, *, units: str,
                  pointer: str, record_integrity: str | None,
                  record_path: str | None,
                  levels: Sequence[float] | None = None) -> dict[str, Any]:
    times_list = ([float(v) for v in times]
                 if isinstance(times, (list, tuple)) else [])
    values_list = ([float(v) for v in values]
                   if isinstance(values, (list, tuple)) else [])
    n = min(len(times_list), len(values_list))
    times_list, values_list = times_list[:n], values_list[:n]
    duration = (times_list[-1] - times_list[0]) if n else None
    requested_levels = list(levels) if levels else _curve_levels(duration)
    result_levels: list[dict[str, Any]] = []
    for level in requested_levels:
        result_levels.append({
            "bucket_width_s": level,
            "aggregation": "mean",
            "buckets": _buckets(times_list, values_list, level),
        })
    raw = {"times_s": times_list, "values": values_list}
    return {
        "representation": "multi_resolution_time_summary",
        "units": units,
        "source_point_count": n,
        "source_time_range": _interval(times_list[0], times_list[-1])
        if n else _interval(None, None),
        "levels": result_levels,
        "raw_series_included": False,
        "raw_series_sha256": _hash(raw),
        "raw_series_pointer": pointer,
        "record_integrity_sha256": record_integrity,
    }


def _quantile_buckets(times: Sequence[float], values: Sequence[float],
                      level_s: float) -> list[dict[str, Any]]:
    """One compact quantile/summary record per deterministic time bin."""
    if not times or not values:
        return []
    start, end = float(times[0]), float(times[-1])
    if end <= start:
        end = start + level_s
    result: list[dict[str, Any]] = []
    bucket_start = start
    while bucket_start < end + 1e-12:
        bucket_end = min(end, bucket_start + level_s)
        selected = sorted(float(v) for t, v in zip(times, values)
                          if isinstance(t, (int, float)) and
                          isinstance(v, (int, float)) and
                          (bucket_start <= float(t) < bucket_end or
                           (bucket_end >= end and abs(float(t) - end) <= 1e-12)))
        if selected:
            n = len(selected)
            result.append({
                "start_s": bucket_start,
                "end_s": bucket_end,
                "count": n,
                "min": selected[0],
                "max": selected[-1],
                "mean": sum(selected) / n,
                "p50": selected[max(0, int(round(0.50 * (n - 1))))],
            })
        bucket_start = bucket_end
        if bucket_end >= end:
            break
    return result


def _quantile_time_summary(times: Any, values: Any, *, units: str,
                           pointer: str, record_integrity: str | None,
                           record_path: str | None,
                           levels: Sequence[float]) -> dict[str, Any]:
    """Aggressive deterministic multi-resolution quantile time projection.

    Per-sample metadata is intentionally kept once at the curve level, not
    repeated in every bin.  Every bucket has explicit half-open boundaries and
    mean/p05/p50/p95 summaries; exact points and their hash stay in the archive.
    """
    times_list = ([float(v) for v in times]
                  if isinstance(times, (list, tuple)) else [])
    values_list = ([float(v) for v in values]
                   if isinstance(values, (list, tuple)) else [])
    n = min(len(times_list), len(values_list))
    times_list, values_list = times_list[:n], values_list[:n]
    duration = (times_list[-1] - times_list[0]) if n else None
    result_levels = []
    for level in levels:
        result_levels.append({
            "bucket_width_s": level,
            "aggregation": "mean_and_quantiles",
            "buckets": _quantile_buckets(times_list, values_list, level),
        })
    raw = {"times_s": times_list, "values": values_list}
    return {
        "representation": "quantile_time_bins",
        "units": units,
        "bucket_interval": INTERVAL_CONVENTION,
        "levels": result_levels,
        "quantiles": [0.5],
        "source_point_count": n,
        "source_time_range": _interval(times_list[0], times_list[-1])
        if n else _interval(None, None),
        "raw_series_included": False,
        "raw_series_sha256": _hash(raw),
        "raw_series_pointer": pointer,
        "record_integrity_sha256": record_integrity,
    }


def _activity_grid(analyses: Mapping[str, Any],
                   analysis_views: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Cross-source RMS map: one row per bucket, named source ids.

    Exact cells are not reprinted here.  The grid declares its aggregation,
    resolution, time basis and covered interval; every source keeps an exact
    series reference in ``analysis[].detail``.
    """
    prepared: list[tuple[str, str, list[float], list[float]]] = []
    for guid, report in analyses.items():
        curves = report.get("curves") if isinstance(report, Mapping) else None
        if not isinstance(curves, Mapping):
            continue
        times = [float(value) for value in curves.get("rms_400ms_time_s") or []
                 if isinstance(value, (int, float)) and not isinstance(value, bool)]
        values = [float(value) for value in curves.get("rms_400ms_dbfs") or []
                  if isinstance(value, (int, float)) and not isinstance(value, bool)]
        count = min(len(times), len(values))
        if count < 2:
            continue
        view = analysis_views.get(guid) or {}
        prepared.append((str(view.get("source_id") or _stable("analysis", guid)),
                         str(view.get("analysis_id") or ""),
                         times[:count], values[:count]))
    if not prepared:
        return {
            "representation": "cross_source_activity_grid",
            "available": False,
            "reason": "no_source_rms_series",
            "units": "dBFS",
            "sources": [],
            "buckets": [],
        }
    widest = max(times[-1] - times[0] for _sid, _aid, times, _values in prepared)
    buckets_count = max(1, min(INLINE_MAP_BUCKETS, min(len(times) for _s, _a, times, _v in prepared) - 1))
    width = widest / buckets_count
    rows: dict[tuple[float, float], dict[str, Any]] = {}
    for source_id, _analysis_id, times, values in prepared:
        for bucket in _quantile_buckets(times, values, width):
            key = (bucket["start_s"], bucket["end_s"])
            row = rows.setdefault(key, {"start_s": bucket["start_s"],
                                        "end_s": bucket["end_s"],
                                        "rms_dbfs_by_source": {}})
            row["rms_dbfs_by_source"][source_id] = bucket["mean"]
    ordered = [rows[key] for key in sorted(rows)]
    sources = [{"source_id": source_id,
                "analysis_id": analysis_id,
                "series_ref": f"series:{analysis_id}" if analysis_id else None}
               for source_id, analysis_id, _times, _values in prepared]
    observed = {row["source_id"] for row in sources}
    missing = sorted({str(view.get("source_id") or key)
                      for key, view in analysis_views.items()} - observed)
    return {
        "representation": "cross_source_activity_grid",
        "available": True,
        "units": "dBFS",
        "value": "mean rms_400ms per bucket per source",
        "aggregation": "mean_per_bucket",
        "bucket_width_s": width,
        "bucket_count": len(ordered),
        "anchor": "first_source_cell_start_s",
        "time_basis": {"start_s": "project_seconds",
                       "interval": INTERVAL_CONVENTION},
        "covered_interval": _interval(min(row["start_s"] for row in ordered),
                                      max(row["end_s"] for row in ordered)),
        "source_count": len(sources),
        "sources": sources,
        "buckets": ordered,
        "coverage": {
            "status": ("complete" if len(sources) == len(analysis_views) else "partial"),
            "expected_sources": len(analysis_views),
            "observed_sources": len(sources),
            "sources_without_rms_cells": missing,
            "note": ("a source without RMS cells is absent from the grid, not zero; "
                     "exact cells are available through analysis[].detail"),
        },
    }


def _analysis_view(report: Any, guid: str, *,
                   source_id: str | None,
                   detail_ctx: dict[str, Any]) -> dict[str, Any]:
    """One per-source measurement row with named metrics and a series reference."""
    if not isinstance(report, Mapping):
        return {
            "analysis_id": _stable("analysis", guid),
            "source_guid": guid,
            "available": False,
            "metrics": {},
        }
    raw_metrics = report.get("metrics") if isinstance(report.get("metrics"), Mapping) else {}
    metrics: dict[str, Any] = {}
    for name in ANALYSIS_METRIC_COLUMNS:
        item = raw_metrics.get(name)
        value = item.get("value") if isinstance(item, Mapping) else item
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metrics[name] = float(value)
    provenance = report.get("provenance") if isinstance(report.get("provenance"), Mapping) else {}
    section = report.get("section")
    measured = section.get("measured_section_s") if isinstance(section, Mapping) else None
    if not isinstance(measured, Mapping):
        measured = provenance.get("measured_section_s")
    measured = measured if isinstance(measured, Mapping) else None
    channels = _integer(report.get("channels"))
    series_full: dict[str, Any] = {}
    series_row: dict[str, Any] = {}
    for name in ("rms_400ms", "band_level", "temporal_profile"):
        entry = series_entry(report, name)
        if int(entry.get("count") or 0):
            series_full[name] = entry
            series_row[name] = {key: entry[key] for key in
                                ("count", "units", "value_kind", "band_count")
                                if key in entry}
    raw_curves = report.get("curves") if isinstance(report.get("curves"), Mapping) else {}
    rms_values = [float(value) for value in raw_curves.get("rms_400ms_dbfs") or []
                  if isinstance(value, (int, float)) and not isinstance(value, bool)]
    interval = (_interval(measured.get("start_s"), measured.get("end_s"))
                if measured is not None else _interval(None, None))
    reference = series_reference(
        capture_id=str(detail_ctx["capture_id"]),
        project_id=str(detail_ctx["project_id"]),
        revision=str(detail_ctx["revision"]),
        integrity=detail_ctx["integrity"],
        record_path=detail_ctx["record_path"],
        analysis_id=_stable("analysis", guid), source_id=source_id,
        series=series_full, interval=interval,
        time_basis={"start_s": "project_seconds", "interval": INTERVAL_CONVENTION},
    )
    detail_ctx["refs"].append(reference)
    view: dict[str, Any] = {
        "analysis_id": _stable("analysis", guid),
        "source_guid": guid,
        "source_id": source_id,
        "available": True,
        "fact_class": "signal_measurement",
        "metrics": metrics,
        "measured_window": interval,
        "measured_window_basis": ("measured_section" if measured is not None
                                  else "unavailable"),
        "stage": provenance.get("stage"),
        "channel_view": (f"{channels}_channels" if channels else None),
        "rms_stats": {**_stats(rms_values), "units": "dBFS",
                      "basis": "rms_400ms_cells_over_measured_window"},
        "series": series_row,
        "uncertainty": {
            "notes": [
                {"metric": name, "note": item.get("note")}
                for name, item in (raw_metrics or {}).items()
                if isinstance(item, Mapping) and item.get("note")
            ],
            "confidence": {
                name: item.get("confidence")
                for name, item in (raw_metrics or {}).items()
                if isinstance(item, Mapping) and item.get("confidence") is not None
            },
            "basis": "per_metric_analysis_record",
        },
        "detail": {
            "ref": reference["ref"],
            "kind": reference["kind"],
            "schema": reference["schema"],
            "count": reference["count"],
            "fetch": reference["fetch"],
        },
    }
    return view


def _audio_view(asset: Mapping[str, Any], *, project_start_s: float,
                capture_end_s: float, track_id: str | None, master_id: str | None,
                record_integrity: str | None, record_path: str | None,
                analysis_id: str | None, is_master: bool) -> dict[str, Any]:
    """One audio asset row.

    The project/file/sample mapping formulas live once in ``audio_contract``;
    this row keeps only the values that differ per asset plus exact format,
    provenance and identity.
    """
    frames = _integer(asset.get("frames")) or 0
    sample_rate = _integer(asset.get("sample_rate")) or 0
    channels = _integer(asset.get("channels")) or 0
    reported_duration = _number(asset.get("duration_s"))
    derived_duration = frames / sample_rate if frames and sample_rate else None
    tolerance = max(FILE_DURATION_TOLERANCE_S, 1.0 / sample_rate if sample_rate else 0.0)
    if (reported_duration is not None and derived_duration is not None
            and abs(reported_duration - derived_duration) > tolerance):
        raise ValidationError(
            "audio duration does not match frames / sample_rate",
            details={
                "source_guid": asset.get("source_guid"),
                "frames": frames,
                "sample_rate_hz": sample_rate,
                "reported_duration_s": reported_duration,
                "derived_duration_s": derived_duration,
                "tolerance_s": tolerance,
            },
        )
    duration = derived_duration if derived_duration is not None else (reported_duration or 0.0)
    requested_duration = max(0.0, capture_end_s - project_start_s)
    mapped_file_end = min(duration, requested_duration)
    mapped_samples = int(round(mapped_file_end * sample_rate)) if sample_rate else 0
    tail_samples = max(0, frames - mapped_samples)
    tail_start = capture_end_s
    tail_end = project_start_s + duration
    tail_present = (tail_end - tail_start) > 1e-9
    signal_window = asset.get("signal_window")
    if not isinstance(signal_window, Mapping):
        signal_window = {}
    capture_revision = asset.get("capture_revision")
    if not isinstance(capture_revision, Mapping):
        capture_revision = {}
    capture_revision_sha256 = _hash(capture_revision) if capture_revision else None
    capture_method = asset.get("capture_method")
    capture_stage = asset.get("stage")
    return {
        "audio_id": _stable("audio", asset.get("source_guid") or ""),
        "source_id": master_id if is_master else track_id,
        "name": asset.get("name"),
        "is_master": bool(is_master),
        "render_provenance": {
            "capture_method": capture_method,
            "stage": capture_stage,
            "capture_revision_change_count": _integer(
                capture_revision.get("change_count")),
            "capture_revision_sha256": capture_revision_sha256,
            "soloed_guid": asset.get("soloed_guid"),
            "complete": bool(
                capture_method and capture_stage and capture_revision_sha256 and
                capture_revision.get("change_count") is not None and
                "soloed_guid" in asset),
        },
        "format": {
            "frame_count": frames,
            "sample_rate_hz": sample_rate,
            "channel_count": channels,
            "duration_s": duration,
            "duration_consistent": (reported_duration is not None and
                                    derived_duration is not None and
                                    abs(reported_duration - derived_duration) <= tolerance),
        },
        "mapping": {
            "file_start_s": 0.0,
            "file_end_s": duration,
            "mapped_file_end_s": mapped_file_end,
            "valid_end_sample_exclusive": mapped_samples,
            "tail_start_sample": mapped_samples,
            "tail_end_sample_exclusive": frames,
            "tail_sample_count": tail_samples,
            "tail_present": tail_present,
            "tail_start_s": tail_start if tail_present else None,
            "tail_end_s": tail_end if tail_present else None,
            "tail_maps_to_project": False,
            "signal_window": dict(signal_window),
        },
        "sha256": asset.get("sha256"),
        "archive_audio": asset.get("archive_audio"),
        "analysis_id": analysis_id,
    }


def _project_extent(project_map: Mapping[str, Any]) -> dict[str, Any]:
    settings = _project_settings(project_map)
    # Prefer a field that REAPER reports as the project extent.  The render
    # end position is not the same concept; it can be shorter or longer than
    # the project and must be labelled as a derived fallback.
    exact_candidates = (
        ("project_extent_s", project_map.get("project_extent_s")),
        ("project_length_s", project_map.get("project_length_s")),
        ("length_s", project_map.get("length_s")),
        ("project_extent_s", settings.get("project_extent_s")),
        ("project_length_s", settings.get("project_length_s")),
    )
    for field_name, candidate in exact_candidates:
        end = _number(candidate)
        if end is not None and end >= 0:
            return {
                "start_s": 0.0,
                "end_s": end,
                "duration_s": end,
                "interval": INTERVAL_CONVENTION,
                "units": "s",
                "basis": "reaper_reported_project_extent",
                "source_field": field_name,
                "reported_by_reaper": True,
                "derived": False,
            }
    end = _number(settings.get("render_endpos"))
    if end is not None and end > 0:
        return {
            "start_s": 0.0,
            "end_s": end,
            "duration_s": end,
            "interval": INTERVAL_CONVENTION,
            "units": "s",
            "basis": "derived_from_reaper_render_endpos_not_project_extent",
            "source_field": "render_endpos",
            "reported_by_reaper": False,
            "derived": True,
            "warning": "render_endpos is a render setting, not an exact project extent",
        }
    end = 0.0
    for item in project_map.get("items") or []:
        if isinstance(item, Mapping):
            position = _number(item.get("position_s"))
            length = _number(item.get("length_s"))
            if position is not None and length is not None:
                end = max(end, position + length)
    return {
        "start_s": 0.0,
        "end_s": end,
        "duration_s": end,
        "interval": INTERVAL_CONVENTION,
        "units": "s",
        "basis": "derived_from_item_ends_fallback",
        "source_field": "items.position_s+length_s",
        "reported_by_reaper": False,
        "derived": True,
        "warning": "project extent was not reported by REAPER; value is derived from items",
    }


def _content_extent(project_map: Mapping[str, Any]) -> dict[str, Any]:
    starts: list[float] = []
    ends: list[float] = []
    for item in project_map.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        position = _number(item.get("position_s"))
        length = _number(item.get("length_s"))
        if position is not None and length is not None:
            starts.append(position)
            ends.append(position + length)
    if not starts:
        return {"start_s": None, "end_s": None, "duration_s": None,
                "interval": INTERVAL_CONVENTION, "units": "s",
                "basis": "no_project_items", "available": False}
    start, end = min(starts), max(ends)
    return {
        "start_s": start,
        "end_s": end,
        "duration_s": max(0.0, end - start),
        "interval": INTERVAL_CONVENTION,
        "units": "s",
        "basis": "derived_from_all_project_item_intervals",
        "item_count": len(starts),
        "available": True,
    }


def _crosslink(links: list[dict[str, Any]], from_id: str, relation: str,
               to_id: str, **qualifiers: Any) -> None:
    if not from_id or not to_id:
        return
    link = {
        "from": from_id,
        "relation": relation,
        "to": to_id,
    }
    if qualifiers:
        link["qualifiers"] = qualifiers
    links.append(link)


def _report_complete(raw: Any) -> bool:
    if isinstance(raw, Mapping):
        return bool(raw.get("complete", True)) and not bool(raw.get("gaps"))
    return True


def _coverage(project_map: Mapping[str, Any], data: Mapping[str, Any],
              tracks: list[Mapping[str, Any]], master: Mapping[str, Any],
              items: list[Mapping[str, Any]], takes: list[Mapping[str, Any]],
              midi: list[Mapping[str, Any]], fx: list[Mapping[str, Any]],
              routes: list[Mapping[str, Any]],
              automation: list[Mapping[str, Any]],
              envelopes: list[Mapping[str, Any]],
              audio: list[Mapping[str, Any]],
              analysis: list[Mapping[str, Any]]) -> dict[str, Any]:
    raw_tracks = project_map.get("tracks") if isinstance(project_map.get("tracks"), list) else []
    raw_master = project_map.get("master") if isinstance(project_map.get("master"), Mapping) else {}
    raw_items = project_map.get("items") if isinstance(project_map.get("items"), list) else []
    raw_takes = project_map.get("takes") if isinstance(project_map.get("takes"), list) else []
    raw_midi = project_map.get("midi") if isinstance(project_map.get("midi"), list) else []
    raw_fx = project_map.get("fx") if isinstance(project_map.get("fx"), list) else []
    expected_takes = 0
    for item in raw_items:
        if isinstance(item, Mapping):
            expected_takes += max(1, _integer(item.get("take_count")) or 1)
    expected_midi_notes = 0
    midi_truncated = False
    for entry in raw_midi:
        if not isinstance(entry, Mapping):
            continue
        events = entry.get("events")
        if isinstance(events, Mapping):
            raw_count = _integer(events.get("note_count")) or 0
            returned = _integer(events.get("returned_note_count")) or 0
            expected_midi_notes += raw_count
            if (bool(events.get("truncated")) or raw_count > returned or
                    raw_count > MIDI_NOTE_LIMIT):
                midi_truncated = True
    expected_fx = sum(len(track.get("fx") or []) for track in raw_tracks
                      if isinstance(track, Mapping))
    if isinstance(raw_master.get("fx"), list):
        expected_fx += len(raw_master["fx"])
    expected_routes = 0
    for track in raw_tracks:
        if isinstance(track, Mapping):
            sends = track.get("sends")
            if isinstance(sends, list):
                expected_routes += len(sends)
            else:
                expected_routes += _integer(sends) or 0
    audio_expected = len(raw_tracks) + (1 if raw_master else 0)
    analysis_expected = len(audio)
    statuses: dict[str, dict[str, Any]] = {
        "tracks": {"expected_count": len(raw_tracks), "observed_count": len(tracks),
                   "status": "complete" if len(raw_tracks) == len(tracks) else "partial"},
        "items": {"expected_count": len(raw_items), "observed_count": len(items),
                  "status": "complete" if len(raw_items) == len(items) else "partial"},
        "takes": {"expected_count": expected_takes, "observed_count": len(takes),
                  "status": "complete" if expected_takes == len(takes) else "partial",
                  "active_take_only": expected_takes > len(takes),
                  "missing_count": max(0, expected_takes - len(takes))},
        "midi": {"expected_count": expected_midi_notes, "observed_count": sum(
            int(row.get("returned_note_count") or 0) for row in midi),
            "status": ("truncated" if midi_truncated else
                       "complete" if expected_midi_notes == sum(
                           int(row.get("returned_note_count") or 0) for row in midi)
                       else "partial"),
            "midi_note_limit": MIDI_NOTE_LIMIT,
            "returned_note_count_basis": "details.midi_notes.returned_count",
            "truncated": midi_truncated},
        "fx": {"expected_count": expected_fx, "observed_count": len(fx),
               "status": "complete" if expected_fx == len(fx) else "partial"},
        "routes": {"expected_count": expected_routes, "observed_count": len(routes),
                   "status": "complete" if expected_routes == len(routes) else "partial"},
        "automation": {"expected_count": None, "observed_count": len(automation),
                       "status": "complete" if _report_complete(
                           project_map.get("automation")) else "partial"},
        "envelopes": {"expected_count": None, "observed_count": len(envelopes),
                      "status": "complete" if _report_complete(
                          project_map.get("envelopes")) else "partial"},
        "audio": {"expected_count": audio_expected, "observed_count": len(audio),
                  "status": "complete" if audio_expected == len(audio) else "partial"},
        "analysis": {"expected_count": analysis_expected, "observed_count": len(analysis),
                     "status": "complete" if analysis_expected == len(analysis) else "partial"},
    }
    raw_coverage = data.get("coverage")
    if isinstance(raw_coverage, Mapping):
        for name, value in raw_coverage.items():
            if name in statuses and value is not True:
                statuses[name]["status"] = "missing"
    missing: list[str] = []
    truncated: list[str] = []
    if statuses["takes"]["active_take_only"]:
        truncated.append("takes.active_take_only")
    if statuses["midi"]["truncated"]:
        truncated.append("midi.note_limit")
    for name, status in statuses.items():
        if status["status"] != "complete":
            missing.append(name)
    raw_missing = data.get("missing") if isinstance(data.get("missing"), list) else []
    if raw_missing:
        missing.extend(str(item) for item in raw_missing)
    return {
        "domains": statuses,
        "complete": all(status["status"] == "complete" for status in statuses.values()),
        "missing": sorted(set(missing)),
        "truncated": sorted(set(truncated)),
        "raw_record_coverage": dict(data.get("coverage") or {}),
        "coverage_rule": "a domain is complete only when expected == observed and no truncation flag is set",
    }


KIND_MEANING_NOTE = (
    "Each kind block states the shared meaning, method, units, caveats and "
    "alternatives once. Every row below it carries only its own endpoints, "
    "measurement values, band, coverage and stable id; the set-level window, "
    "stage, node, channel view and provenance apply to every row."
)


def _relations_view(domain: Mapping[str, Any], *,
                    source_meta: Mapping[str, Mapping[str, Any]],
                    id_for_source: Mapping[str, str]) -> dict[str, Any]:
    """Factor a canonical relation set into readable blocks plus exact rows.

    Canonical relation endpoint ids are REAPER source GUIDs; the panorama maps
    them to the same stable ids used by tracks, audio and analysis so one
    identity join works across the whole document.
    """

    def stable(endpoint: Any) -> Any:
        text = str(endpoint)
        return id_for_source.get(text, endpoint)
    relations = [row for row in domain.get("relations") or [] if isinstance(row, Mapping)]
    binding = domain.get("binding") if isinstance(domain.get("binding"), Mapping) else {}
    kinds: dict[str, Any] = {}
    edges: list[dict[str, Any]] = []
    for relation in relations:
        kind = str(relation.get("kind"))
        attributes = (relation.get("attributes")
                      if isinstance(relation.get("attributes"), Mapping) else {})
        if kind not in kinds:
            temporal = (attributes.get("temporal_evidence")
                        if isinstance(attributes.get("temporal_evidence"), Mapping) else {})
            kinds[kind] = {
                "meaning": relation.get("interpretation"),
                "is_not": (
                    ["audibility", "psychoacoustic masking", "causation",
                     "an instruction to change anything"]
                    if kind == "band_dominance" else
                    ["a fader", "a causal contribution through master FX or PDC",
                     "a quality judgment"] if kind == "track_master_level_relation" else
                    ["a settled conclusion"]
                ),
                "method": relation.get("method"),
                "method_version": relation.get("method_version"),
                "units": dict(relation.get("units") or {}),
                "endpoint_stages": dict(attributes.get("endpoint_stages") or {}),
                "stage_relation": attributes.get("stage_relation"),
                "is_psychoacoustic_threshold": attributes.get("is_psychoacoustic_threshold"),
                "uncertainty": dict(relation.get("uncertainty") or {}),
                "alternatives": list(relation.get("alternatives") or []),
                "coverage_rule": ("a cell is active when both sources exceed the analysis "
                                  "floor; active_cells vs cell_count is reported per row"),
                "evidence": {key: temporal.get(key) for key in
                             ("representation", "transform",
                              "median_is_complete_temporal_relation")
                             if key in temporal} or None,
                "evidence_series": ("the analysis[] row for each endpoint: detail.fetch with "
                                    "kind=analysis_series, series=band_level, cells on "
                                    "spectrogram_time_s"),
            }
        coverage = (relation.get("coverage")
                    if isinstance(relation.get("coverage"), Mapping) else {})
        row: dict[str, Any] = {
            "id": relation.get("relation_id"),
            "kind": kind,
            "from": stable(relation.get("from_id")),
            "to": stable(relation.get("to_id")),
            "metrics": dict(relation.get("metrics") or {}),
            "coverage": {
                "status": coverage.get("status"),
                "active_cells": coverage.get("observed_cells"),
                "cell_count": coverage.get("expected_cells"),
                "fraction": coverage.get("fraction"),
                "reasons": list(coverage.get("reasons") or []),
            },
        }
        for key in ("band_hz", "band_index", "pan_relation"):
            if key in attributes:
                row[key] = attributes[key]
        edges.append(row)
    edges.sort(key=lambda row: (str(row.get("kind")), str(row.get("from")),
                                str(row.get("to")), str(row.get("id"))))

    pairs: list[dict[str, Any]] = []
    evaluations = domain.get("evaluations")
    table = (evaluations.get("band_dominance_pairs")
             if isinstance(evaluations, Mapping) else None)
    if isinstance(table, Mapping):
        columns = [str(name) for name in table.get("columns") or []]
        for raw in table.get("rows") or []:
            if not isinstance(raw, (list, tuple)):
                continue
            mapping = dict(zip(columns, raw))
            row = {
                "id": mapping.get("relation_id"),
                "from": stable(mapping.get("from_id")),
                "to": stable(mapping.get("to_id")),
                "result": mapping.get("status"),
                "band_index": mapping.get("best_band_index"),
                "band_hz": mapping.get("best_band_hz"),
                "positive_db": mapping.get("best_positive_db"),
                "negative_db": mapping.get("best_negative_db"),
                "active_fraction": mapping.get("active_fraction"),
                "pan_relation": mapping.get("pan_relation"),
                "cell_count": mapping.get("cell_count"),
                "reason": mapping.get("reason") or None,
            }
            pairs.append({key: value for key, value in row.items() if value is not None})
        pairs.sort(key=lambda row: (str(row.get("from")), str(row.get("to"))))
    coverage = domain.get("coverage") if isinstance(domain.get("coverage"), Mapping) else {}
    emitted = {kind: 0 for kind in kinds}
    for row in edges:
        emitted[str(row.get("kind"))] = emitted.get(str(row.get("kind")), 0) + 1
    return {
        "schema_version": 2,
        "canonical_schema_version": domain.get("schema_version"),
        "contract": KIND_MEANING_NOTE,
        "complete": bool(domain.get("complete")),
        "reference_status": domain.get("reference_status"),
        "binding": {
            "capture_id": binding.get("capture_id"),
            "project_id": binding.get("project_id"),
            "revision_fingerprint": binding.get("revision_fingerprint"),
            "fact_root": binding.get("fact_root"),
            "window": binding.get("window"),
            "stage": binding.get("stage"),
            "node": binding.get("node"),
            "channel_view": binding.get("channel_view"),
            "producer": binding.get("producer"),
            "created_at": binding.get("created_at"),
            "render_key": binding.get("render_key"),
        },
        "sources": {str(key): dict(value) for key, value in source_meta.items()},
        "sources_note": ("every row's asset dependency is the binding above plus this "
                         "map's asset_sha256 for its two endpoints"),
        "kinds": kinds,
        "kinds_note": ("a kind with no emitted row is absent here; completeness "
                       "still reports its evaluation"),
        "edges": edges,
        "evaluated_pairs": pairs,
        "completeness": {
            "kinds": {
                kind: {
                    "expected": int(value.get("expected", 0)),
                    "evaluated": int(value.get("evaluated", 0)),
                    "emitted": int(emitted.get(kind, 0)),
                    "status": value.get("status"),
                }
                for kind, value in sorted(coverage.items())
                if isinstance(value, Mapping)
            },
            "pairs_evaluated": len(pairs),
            "pairs_complete": (bool(table.get("canonical"))
                               if isinstance(table, Mapping) else None),
            "display_cap": None,
            "edges_omitted_by_display_cap": 0,
            "top_k_is_not_complete": True,
        },
        "coverage": dict(coverage),
        "unavailable": dict(domain.get("unavailable") or {}),
        "truncated": dict(domain.get("truncated") or {}),
        "content_hash": domain.get("content_hash"),
        "page_contract": {
            "canonical": True,
            "top_k_is_not_complete": True,
            "bounded_projection_must_declare_partial": True,
            "paging": ("the relation domain is part of the paged panorama; no "
                       "bounded projection is emitted by this builder"),
        },
    }


def build_relation_domain(record: Any, *, integrity: str | None = None) -> dict[str, Any]:
    """Build the same canonical relation domain from a full-capture record.

    This is the pre-seal entry point used by the capture builder.  It reads only
    fields already present in the record payload; it does not render or analyse
    audio again.  The panorama builder calls the same implementation when a
    legacy record has no ``relations`` field.
    """

    data = _record_dict(record)
    project_map = _project_map(data)
    raw_tracks = project_map.get("tracks")
    tracks = raw_tracks if isinstance(raw_tracks, list) else []
    master = project_map.get("master")
    master_map = master if isinstance(master, Mapping) else {}
    master_guid = master_map.get("guid")
    master_id = _stable("master", master_guid or "unknown")
    track_shells = [
        {
            "guid": track.get("guid"),
            "facts": {"pan": _number(track.get("pan"))},
        }
        for track in tracks
        if isinstance(track, Mapping) and track.get("guid")
    ]
    raw_assets = _audio_items(data)
    audio_by_source = {
        str(asset.get("source_guid")): {"audio_id": asset.get("asset_id")}
        for asset in raw_assets
        if asset.get("source_guid")
    }
    time_window = data.get("time_window")
    if not isinstance(time_window, Mapping):
        raise ValidationError("full-capture record has no time_window")
    start = _number(time_window.get("project_start_s"))
    end = _number(time_window.get("project_end_s"))
    if start is None or end is None:
        raise ValidationError("full-capture record has an invalid time_window")
    return _relations_domain(
        data=data,
        analyses=_analysis_map(data),
        track_shells=track_shells,
        audio_by_source=audio_by_source,
        raw_assets=raw_assets,
        master_guid=str(master_guid) if master_guid else None,
        master_id=master_id,
        project_id=str(data.get("project_id") or data.get("project_path") or "unknown"),
        project_start=start,
        project_end=end,
        integrity=integrity,
    )


def _relations_domain(
    *,
    data: Mapping[str, Any],
    analyses: Mapping[str, Any],
    track_shells: Sequence[Mapping[str, Any]],
    audio_by_source: Mapping[str, Mapping[str, Any]],
    raw_assets: Sequence[Mapping[str, Any]],
    master_guid: str | None,
    master_id: str,
    project_id: str,
    project_start: float,
    project_end: float,
    integrity: str | None,
) -> dict[str, Any]:
    """Build the canonical source-source and source-master relation domain.

    Every expected pair is evaluated and retained in a columnar table; emitted
    relations are the thresholded subset.  A top-K list is not produced here:
    the domain is the canonical evidence, and a caller may request a bounded
    projection separately.
    """

    capture_id = str(data.get("capture_id") or "")
    revision = data.get("revision_before") or data.get("revision_after")
    revision_fingerprint = _hash(revision) if revision is not None else _hash(capture_id)
    fact_root = integrity if isinstance(integrity, str) and integrity else _hash({
        "capture_id": capture_id,
        "revision": revision,
    })
    sample_rate = next(
        (rate for rate in (_integer(asset.get("sample_rate")) for asset in raw_assets)
         if rate), None
    )
    window = RelationWindow(
        start_s=project_start,
        end_s=project_end,
        start_sample=(int(round(project_start * sample_rate)) if sample_rate else None),
        end_sample=(int(round(project_end * sample_rate)) if sample_rate else None),
        sample_rate_hz=sample_rate,
    )
    pan_by_guid = {
        str(shell.get("guid")): _number((shell.get("facts") or {}).get("pan"))
        for shell in track_shells
        if shell.get("guid")
    }
    band_sources: dict[str, Any] = {}
    for guid, report in analyses.items():
        if guid == str(master_guid):
            continue
        curves = report.get("curves") if isinstance(report, Mapping) else None
        bands = curves.get("band_level_time_dbfs") if isinstance(curves, Mapping) else None
        if not isinstance(bands, (list, tuple)) or not bands:
            continue
        band_sources[str(guid)] = {
            "bands": bands,
            "band_centers_hz": (curves.get("band_centers_hz")
                                if isinstance(curves, Mapping) else None),
            "pan": pan_by_guid.get(str(guid)),
        }
    band_batch = band_dominance_candidates(band_sources)
    master_report = analyses.get(str(master_guid)) if master_guid else None
    source_reports = {
        str(guid): report for guid, report in analyses.items()
        if guid != str(master_guid)
    }
    if isinstance(master_report, Mapping):
        master_batch = track_master_candidates(
            source_reports, master_report, master_id=master_id
        )
        reference_batch = reference_candidates(
            master_report, [], stage="master_post_fx"
        )
    else:
        master_batch = RelationBatch(
            unavailable={"track_master_level_relation": "master_analysis_unavailable"}
        )
        reference_batch = RelationBatch(
            unavailable={"reference_comparison": "master_analysis_unavailable"}
        )
    phase_batch = RelationBatch(
        unavailable={"phase_pair": "phase_pair_not_measured_in_capture"}
    )
    batch = merge_batches(band_batch, master_batch, reference_batch, phase_batch)
    assets: dict[str, str] = {}
    for asset in raw_assets:
        guid = str(asset.get("source_guid") or "")
        if not guid:
            continue
        view = audio_by_source.get(guid) or {}
        assets[guid] = str(asset.get("sha256") or view.get("audio_id") or guid)
    known = set(analyses)
    if master_guid:
        known.add(str(master_guid))
    known.add(master_id)
    if master_guid and str(master_guid) in assets:
        assets[master_id] = assets[str(master_guid)]
    binding = RelationBinding(
        capture_id=capture_id,
        project_id=project_id,
        revision_fingerprint=revision_fingerprint,
        fact_root=fact_root,
        window=window,
        stage="post_fx",
        node="capture",
        channel_view="stereo",
        render_key=None,
        producer="reaper-workbench/context/panorama.py",
        created_at=data.get("created_at"),
    )
    relation_set = build_relation_set(
        binding,
        batch,
        source_assets=assets,
        known_ids=sorted(known),
        master_id=master_id,
    )
    domain = relation_set.to_dict()
    domain["reference_status"] = (
        "supplied" if reference_batch.observed.get("reference_comparison", 0) else
        "not_supplied"
    )
    domain["page_contract"] = {
        "canonical": True,
        "top_k_is_not_complete": True,
        "bounded_projection_must_declare_partial": True,
        "paging": (
            "not_implemented; the canonical table is included whole and capacity "
            "is enforced by the panorama budget"
        ),
        "evaluated_pair_rows": len(
            (domain.get("evaluations", {}).get("band_dominance_pairs") or {}).get("rows", [])
        ),
    }
    return domain


def build_panorama(record: Any, *, record_path: str | Path | None = None,
                   max_bytes: int = DEFAULT_MAX_BYTES,
                   max_tokens: int = DEFAULT_MAX_TOKENS,
                   compact: bool = True,
                   force_columnar_temporal: bool = False) -> dict[str, Any]:
    """Build a deterministic, bounded, self-describing capture panorama.

    Shared semantics, units, method, caveats, window and set-level provenance
    appear once (per kind, per domain or per capture).  Every row keeps its own
    exact values, endpoints, coverage and stable id.  MIDI notes, envelope
    points and exact time-series cells stay in the canonical record and are
    addressed by typed entries in ``details``.
    """
    data = _record_dict(record)
    project_map = _project_map(data)
    capture_id = str(data.get("capture_id") or "")
    if not capture_id:
        raise ValidationError("full-capture record has no capture_id")
    integrity = data.get("integrity_sha256")
    project_path = str(data.get("project_path") or "")
    project_key = str(data.get("project_id") or project_path or "unknown")
    revision = revision_fingerprint(data)
    relative_record_path = (str(record_path).replace("\\", "/")
                            if record_path is not None else
                            f"captures/{capture_id}/record.json")
    tracks_raw = [track for track in project_map.get("tracks") or []
                  if isinstance(track, Mapping)]
    master_raw = project_map.get("master") if isinstance(project_map.get("master"), Mapping) else {}
    master_guid = master_raw.get("guid")
    master_id = _stable("master", master_guid or "unknown")
    track_ids = {str(track.get("guid")): _stable("track", track.get("guid"))
                 for track in tracks_raw if track.get("guid")}
    detail_ctx: dict[str, Any] = {
        "capture_id": capture_id,
        "project_id": project_key,
        "revision": revision,
        "integrity": integrity if isinstance(integrity, str) else None,
        "record_path": relative_record_path,
        "refs": [],
    }
    # Build track shells first so FX/route/envelope views can use stable IDs.
    track_shells: list[dict[str, Any]] = []
    for index, track in enumerate(tracks_raw):
        guid = track.get("guid")
        track_shells.append({
            "stable_id": _stable("track", guid or index),
            "guid": guid,
            "index": index,
            "name": track.get("name"),
            "is_master": False,
            "fact_class": "project_fact",
            "source": "reaper_project_map",
            "facts": {
                "channel_count": _integer(track.get("channel_count")),
                "folder_depth": _integer(track.get("folder_depth")),
                "volume": _number(track.get("volume")),
                "pan": _number(track.get("pan")),
                "mute": bool(track.get("mute")),
                "solo": track.get("solo"),
                "selected": bool(track.get("selected")),
                "record_arm": _integer(track.get("record_arm")),
                "record_mode": _integer(track.get("record_mode")),
                "record_mode_flags": _integer(track.get("record_mode_flags")),
                "send_count": _integer(track.get("sends")) or 0,
                "receive_count": _integer(track.get("receives")) or 0,
                "hardware_send_count": _integer(track.get("hardware_sends")) or 0,
                "fx_count": len(track.get("fx") or []),
                "item_count": len(track.get("items") or []),
            },
            "semantic_role": None,
            "semantic_role_source": "not_provided_by_reaper",
            "_raw_fx": track.get("fx") or [],
        })
    master_shell = {
        "stable_id": master_id,
        "index": _integer(master_raw.get("index")),
        "name": master_raw.get("name") or "MASTER",
        "is_master": True,
        "fact_class": "project_fact",
        "source": "reaper_project_map",
        "facts": {
            "channel_count": _integer(master_raw.get("channel_count")),
            "folder_depth": _integer(master_raw.get("folder_depth")),
            "volume": _number(master_raw.get("volume")),
            "pan": _number(master_raw.get("pan")),
            "mute": bool(master_raw.get("mute")),
            "solo": master_raw.get("solo"),
            "selected": bool(master_raw.get("selected")),
            "fx_count": len(master_raw.get("fx") or []),
            "send_count": _integer(master_raw.get("sends")) or 0,
            "receive_count": _integer(master_raw.get("receives")) or 0,
            "hardware_send_count": _integer(master_raw.get("hardware_sends")) or 0,
        },
        "semantic_role": None,
        "semantic_role_source": "not_provided_by_reaper",
        "_raw_fx": master_raw.get("fx") or [],
    }
    # Never attach IDs to the canonical project maps.  Downstream helpers use
    # the immutable shell list and the raw GUIDs instead.
    items = _item_views(data, project_map)
    takes = _take_views(items, project_map)
    midi = _midi_views(items, takes, project_map, detail_ctx=detail_ctx)
    fx = _fx_views(track_shells, master_shell, str(master_guid) if master_guid else None,
                   master_id)
    routes = _route_views(track_shells, master_shell, master_id, project_map)
    automation_rows = _envelope_rows(project_map.get("automation"), "automation",
                                     track_shells, master_id, master_guid,
                                     detail_ctx=detail_ctx, domain_key="automation")
    envelope_rows = _envelope_rows(project_map.get("envelopes"), "envelope",
                                   track_shells, master_id, master_guid,
                                   detail_ctx=detail_ctx, domain_key="envelopes")
    project_extent = _project_extent(project_map)
    content_extent = _content_extent(project_map)
    time_window = data.get("time_window") if isinstance(data.get("time_window"), Mapping) else {}
    project_start = _number(time_window.get("project_start_s")) or 0.0
    project_end = _number(time_window.get("project_end_s"))
    if project_end is None:
        project_end = project_extent["end_s"]
    capture_window = {
        "project_start_s": project_start,
        "project_end_s": project_end,
        "duration_s": max(0.0, project_end - project_start),
        "interval": INTERVAL_CONVENTION,
        "units": "s",
        "basis": "capture_time_window",
    }
    assets = _audio_items(data)
    analyses = _analysis_map(data)
    max_file_duration = max((_number(item.get("duration_s")) or 0.0 for item in assets),
                            default=0.0)
    requested_tail_ms = None
    timings = data.get("timings") if isinstance(data.get("timings"), Mapping) else {}
    settings = timings.get("batch_render_settings") if isinstance(timings, Mapping) else None
    if isinstance(settings, Mapping):
        requested_tail_ms = _integer(settings.get("RENDER_TAILMS"))
    top_tail_end = project_start + max_file_duration
    top_tail = _interval(project_end, top_tail_end)
    top_tail.update({
        "present": top_tail_end > project_end + 1e-9,
        "maps_to_project": False,
        "requested_tail_ms": requested_tail_ms,
        "basis": "max_audio_duration_minus_capture_window",
    })
    audio_views: list[dict[str, Any]] = []
    analysis_views: dict[str, dict[str, Any]] = {}
    audio_by_source: dict[str, dict[str, Any]] = {}
    for asset in assets:
        guid = str(asset.get("source_guid") or "")
        is_master = bool(asset.get("is_master")) or guid == str(master_guid)
        track_id = track_ids.get(guid)
        if is_master:
            track_id = None
        analysis = analyses.get(guid)
        analysis_id = _stable("analysis", guid) if isinstance(analysis, Mapping) else None
        view = _audio_view(
            asset, project_start_s=project_start, capture_end_s=project_end,
            track_id=track_id, master_id=master_id,
            record_integrity=integrity if isinstance(integrity, str) else None,
            record_path=relative_record_path, analysis_id=analysis_id,
            is_master=is_master,
        )
        view["render_key"] = asset.get("render_key")
        view["render_key_complete"] = bool(asset.get("render_key_complete"))
        dependencies = asset.get("render_dependencies")
        view["render_dependencies"] = {
            "manifest_schema": (dependencies or {}).get("manifest_schema"),
            "components_sha256": dict((dependencies or {}).get("components_sha256") or {}),
            "unknown": list((dependencies or {}).get("unknown") or []),
            "unverified": list((dependencies or {}).get("unverified") or []),
        }
        audio_views.append(view)
        audio_by_source[guid] = view
        if isinstance(analysis, Mapping):
            analysis_views[guid] = _analysis_view(
                analysis, guid,
                source_id=(master_id if is_master else track_id),
                detail_ctx=detail_ctx)
    analysis_temporal = _activity_grid(analyses, analysis_views)
    analysis_axis = None
    analysis_contract = {
        "metrics": {
            "representation": "named_scalar_object_per_source",
            "units": dict(ANALYSIS_METRIC_UNITS),
            "fact_class": "signal_measurement",
            "absent_metric": "a metric key is omitted, never written as zero or null-as-value",
        },
        "rms_400ms_dbfs": {
            "representation": "quantile_time_bins",
            "aggregation": "mean_min_max_p50",
            "units": "dBFS",
            "time_basis": "project_seconds",
            "interval": INTERVAL_CONVENTION,
            "inline_buckets_per_source": INLINE_MAP_BUCKETS,
            "exact_cells": ("the analysis[] row for a source: detail.fetch with "
                            "kind=analysis_series, series=rms_400ms"),
        },
        "band_level": {
            "representation": "exact_series_only",
            "units": "dBFS",
            "axis": "analysis_axis.values",
            "exact_cells": ("the analysis[] row for a source: detail.fetch with "
                            "kind=analysis_series, series=band_level"),
            "not_inline": "band cells are not reprinted inline; the relation kinds reference them",
        },
        "measured_window": "per source in analysis[].measured_window",
        "stage": "per source in analysis[].stage",
        "channel_view": "per source in analysis[].channel_view",
    }
    first_provenance: Mapping[str, Any] = {}
    for report in analyses.values():
        if isinstance(report, Mapping) and isinstance(report.get("provenance"), Mapping):
            first_provenance = report["provenance"]
            break
    analysis_contract["method"] = {
        "analysis": first_provenance.get("analysis"),
        "version": first_provenance.get("analysis_version"),
        "decoder": first_provenance.get("decoder"),
        "basis": "analysis[].provenance (one producer for this capture)",
    }
    analysis_contract["stages"] = sorted({
        str(view.get("stage")) for view in analysis_views.values() if view.get("stage")
    })
    # Producer-wide metric notes are identical for every source; state them once
    # only when that is demonstrably true, otherwise keep the per-source copy.
    note_sets = {_hash((view.get("uncertainty") or {}).get("notes") or [])
                 for view in analysis_views.values()}
    if len(note_sets) == 1 and analysis_views:
        shared_notes = next(iter(analysis_views.values())).get("uncertainty", {}).get("notes") or []
        analysis_contract["metric_notes"] = list(shared_notes)
        analysis_contract["metric_notes_scope"] = (
            "identical for every source in this capture; see analysis[].uncertainty.confidence "
            "for per-source confidence values")
        for view in analysis_views.values():
            uncertainty = view.get("uncertainty")
            if isinstance(uncertainty, dict):
                uncertainty.pop("notes", None)
                uncertainty["notes_ref"] = "analysis_contract.metric_notes"
    else:
        analysis_contract["metric_notes_scope"] = "per source in analysis[].uncertainty.notes"
    for axis_guid, axis_report in analyses.items():
        axis_curves = axis_report.get("curves") if isinstance(axis_report, Mapping) else None
        axis_values = axis_curves.get("band_centers_hz") if isinstance(axis_curves, Mapping) else None
        if isinstance(axis_values, (list, tuple)) and axis_values:
            analysis_axis = {
                "units": "Hz",
                "values": list(axis_values),
                "source_point_count": len(axis_values),
                "raw_series_included": False,
                "note": "shared band-frequency axis for every analysis band_level series",
            }
            break
    # Complete track/master views with links and exact source slots.
    tracks: list[dict[str, Any]] = []
    for shell, raw in zip(track_shells, tracks_raw):
        guid = str(shell.get("guid") or "")
        item_views = [item for item in items if item.get("track_id") == shell["stable_id"]]
        item_ids = [item["item_id"] for item in item_views]
        take_ids = [take["take_id"] for take in takes
                    if take.get("track_id") == shell["stable_id"]]
        midi_ids = [row["midi_id"] for row in midi
                    if row.get("item_id") in set(item_ids)]
        audio = audio_by_source.get(guid)
        analysis = analysis_views.get(guid)
        tracks.append({
            "stable_id": shell["stable_id"],
            "guid": guid,
            "index": shell["index"],
            "name": shell["name"],
            "is_master": False,
            "mixer": shell["facts"],
            "audio_id": audio.get("audio_id") if audio else None,
            "analysis_id": analysis.get("analysis_id") if analysis else None,
            "item_ids": item_ids,
            "take_ids": take_ids,
            "midi_ids": midi_ids,
        })
    master_item_ids = [item["item_id"] for item in items
                       if item.get("track_guid") == str(master_guid)]
    master_take_ids = [take["take_id"] for take in takes
                       if take.get("track_id") == master_id]
    master_audio = audio_by_source.get(str(master_guid))
    master_analysis = analysis_views.get(str(master_guid))
    master_view = {
        "stable_id": master_id,
        "guid": master_guid,
        "index": _integer(master_raw.get("index")),
        "name": master_raw.get("name") or "MASTER",
        "is_master": True,
        "fact_class": "project_fact",
        "source": "reaper_project_map",
        "mixer": master_shell["facts"],
        "audio_id": master_audio.get("audio_id") if master_audio else None,
        "analysis_id": master_analysis.get("analysis_id") if master_analysis else None,
        "item_ids": master_item_ids,
        "take_ids": master_take_ids,
    }
    # Cross-links over every included object, with named relation kinds.
    project_full_id = project_key
    project_id = _stable(
        "project", hashlib.sha256(project_full_id.encode("utf-8")).hexdigest()[:16])
    capture_node = _stable("capture", capture_id)
    links: list[dict[str, Any]] = []
    _crosslink(links, capture_node, "captures_project", project_id)
    for track in tracks:
        _crosslink(links, project_id, "contains_track", track["stable_id"])
    _crosslink(links, project_id, "contains_master", master_id)
    for item in items:
        if item.get("track_id"):
            _crosslink(links, item["track_id"], "contains_item", item["item_id"])
        if item.get("active_take_id"):
            _crosslink(links, item["item_id"], "has_active_take",
                       item["active_take_id"])
    for take in takes:
        if take.get("item_id"):
            _crosslink(links, take["item_id"], "has_take", take["take_id"])
    for midi_row in midi:
        _crosslink(links, midi_row["take_id"], "has_midi", midi_row["midi_id"])
    for row in fx:
        _crosslink(links, row["owner_id"], "inserts_fx", row["fx_id"],
                   slot_index=row["slot_index"], slot_order=row["slot_order"])
    for route in routes:
        _crosslink(links, route["source_id"], "routes_to",
                   route["destination_id"], send_index=route["send_index"])
    for row in automation_rows:
        _crosslink(links, row["owner_id"], "has_automation", row["envelope_id"])
    for row in envelope_rows:
        _crosslink(links, row["owner_id"], "has_envelope", row["envelope_id"])
    for audio in audio_views:
        if audio.get("source_id"):
            _crosslink(links, audio["source_id"], "has_audio", audio["audio_id"])
        if audio.get("analysis_id"):
            _crosslink(links, audio["audio_id"], "measured_by", audio["analysis_id"])
    for analysis in analysis_views.values():
        _crosslink(links, analysis["analysis_id"], "belongs_to_capture", capture_node)
    # Time map.  Only the revision-bound REAPER map is used; no beat grid is
    # inferred from a single base BPM.  The identity is content-derived, so an
    # unchanged tempo map keeps the same ID across captures.
    tempo_raw = project_map.get("tempo") if isinstance(project_map.get("tempo"), Mapping) else {}
    tempo_markers = tempo_raw.get("markers") if isinstance(tempo_raw.get("markers"), list) else []
    signature_raw = project_map.get("time_signatures")
    signatures = signature_raw if isinstance(signature_raw, list) else []
    time_map_id = "time_map:" + _hash({"tempo_markers": tempo_markers,
                                       "time_signatures": signatures})[:16]
    _crosslink(links, project_id, "has_time_map", time_map_id)
    time_map = {
        "time_map_id": time_map_id,
        "revision_bound": True,
        "revision": data.get("revision_before"),
        "map_source": "reaper_project_map.tempo.markers and time_signatures",
        "tempo_points": [
            {
                "time_s": _number(marker.get("time_s")),
                "bpm": _number(marker.get("bpm")),
                "numerator": _integer(marker.get("numerator")),
                "denominator": _integer(marker.get("denominator")),
                "measure": _integer(marker.get("measure")),
                "beat": _number(marker.get("beat")),
                "linear": bool(marker.get("linear")),
                "units": {"time_s": "s", "bpm": "BPM", "beat": "beats"},
                "fact_class": "project_fact",
            }
            for marker in tempo_markers if isinstance(marker, Mapping)
        ],
        "time_signatures": [
            {
                "time_s": _number(marker.get("time_s")),
                "numerator": _integer(marker.get("numerator")),
                "denominator": _integer(marker.get("denominator")),
                "measure": _integer(marker.get("measure")),
                "beat": _number(marker.get("beat")),
                "fact_class": "project_fact",
            }
            for marker in signatures if isinstance(marker, Mapping)
        ],
        "base_bpm_not_used_for_musical_coordinates": True,
        "musical_coordinates": {
            "available": True,
            "basis": "revision_bound_reaper_tempo_and_time_signature_map",
            "computed_from_single_bpm": False,
        },
    }
    markers = [
        {
            "marker_id": _stable("marker", index),
            "time_s": _number(marker.get("time_s")),
            "name": marker.get("name"),
            "color": marker.get("color"),
            "fact_class": "project_fact",
        }
        for index, marker in enumerate(project_map.get("markers") or [])
        if isinstance(marker, Mapping)
    ]
    for marker in markers:
        _crosslink(links, project_id, "has_marker", marker["marker_id"])
    regions = []
    for index, region in enumerate(project_map.get("regions") or []):
        if isinstance(region, Mapping):
            start = region.get("start_s", region.get("position_s"))
            end = region.get("end_s")
            regions.append({
                "region_id": _stable("region", index),
                "name": region.get("name"),
                "interval": _interval(start, end),
                "render": region.get("render"),
                "fact_class": "project_fact",
            })
    for region in regions:
        _crosslink(links, project_id, "has_region", region["region_id"])
    coverage = _coverage(project_map, data, tracks, master_view, items, takes, midi,
                         fx, routes, automation_rows, envelope_rows, audio_views,
                         list(analysis_views.values()))
    selected = project_map.get("selection") if isinstance(project_map.get("selection"), Mapping) else {}
    transport = project_map.get("transport") if isinstance(project_map.get("transport"), Mapping) else {}
    from collections import Counter
    item_counts = Counter(str(row.get("track_id")) for row in items)
    take_counts = Counter(str(row.get("track_id")) for row in takes)
    take_track = {str(row.get("take_id")): str(row.get("track_id"))
                  for row in takes}
    midi_counts = Counter(take_track.get(str(row.get("take_id"))) for row in midi)
    fx_counts = Counter(str(row.get("owner_id")) for row in fx)
    route_counts = Counter(str(row.get("source_id")) for row in routes)
    automation_counts = Counter(str(row.get("owner_id")) for row in automation_rows)
    automation_meta = project_map.get("automation") if isinstance(project_map.get("automation"), Mapping) else {}
    envelope_meta = project_map.get("envelopes") if isinstance(project_map.get("envelopes"), Mapping) else {}
    per_track = [
        {
            "track_id": track["stable_id"],
            "index": track["index"],
            "name": track["name"],
            "counts": {
                "items": item_counts.get(track["stable_id"], 0),
                "takes": take_counts.get(track["stable_id"], 0),
                "midi": midi_counts.get(track["stable_id"], 0),
                "fx": fx_counts.get(track["stable_id"], 0),
                "routes": route_counts.get(track["stable_id"], 0),
                "automation": automation_counts.get(track["stable_id"], 0),
            },
            "audio_id": track["audio_id"],
            "analysis_id": track["analysis_id"],
        }
        for track in tracks
    ]
    audio_capture_revisions: dict[str, Any] = {}
    for asset in data.get("audio") or []:
        if not isinstance(asset, Mapping):
            continue
        revision_value = asset.get("capture_revision")
        if isinstance(revision_value, Mapping):
            audio_capture_revisions[_hash(revision_value)] = dict(revision_value)
    precomputed_relations = data.get("relations")
    if (
        isinstance(precomputed_relations, Mapping)
        and precomputed_relations.get("schema_version") == 1
        and isinstance(precomputed_relations.get("binding"), Mapping)
    ):
        canonical_relations = dict(precomputed_relations)
    else:
        canonical_relations = _relations_domain(
            data=data, analyses=analyses, track_shells=track_shells,
            audio_by_source=audio_by_source, raw_assets=assets,
            master_guid=str(master_guid) if master_guid else None,
            master_id=master_id, project_id=project_key,
            project_start=project_start, project_end=project_end,
            integrity=integrity if isinstance(integrity, str) else None,
        )
    id_for_guid = dict(track_ids)
    if master_guid:
        id_for_guid[str(master_guid)] = master_id
    source_meta: dict[str, dict[str, Any]] = {}
    for track in tracks_raw:
        guid = str(track.get("guid") or "")
        if not guid:
            continue
        asset = next((row for row in assets
                      if str(row.get("source_guid")) == guid), None)
        source_meta[_stable("track", guid)] = {
            "name": track.get("name"),
            "role": "track",
            "stage": "source_render_post_fx",
            "asset_sha256": (asset or {}).get("sha256"),
        }
    if master_guid:
        master_asset = next((row for row in assets
                             if str(row.get("source_guid")) == str(master_guid)), None)
        source_meta[master_id] = {
            "name": master_raw.get("name") or "MASTER",
            "role": "master",
            "stage": "master_render_post_fx",
            "asset_sha256": (master_asset or {}).get("sha256"),
        }
    relations_domain = _relations_view(canonical_relations, source_meta=source_meta,
                                       id_for_source=dict(id_for_guid))
    relations_summary = {
        "schema_version": relations_domain.get("schema_version"),
        "scope": "project",
        "complete": bool(relations_domain.get("complete")),
        "coverage": relations_domain.get("coverage"),
        "unavailable": relations_domain.get("unavailable"),
        "reference_status": relations_domain.get("reference_status"),
        "content_hash": relations_domain.get("content_hash"),
        "counts": {
            "relations": len(relations_domain.get("edges") or []),
            "pairs": len(relations_domain.get("evaluated_pairs") or []),
        },
    }
    identity = identity_from_mapping(
        data.get("revision_after") if isinstance(data.get("revision_after"), Mapping)
        else data.get("revision_before"))
    raw_manifest = project_map.get("render_manifest")
    if isinstance(raw_manifest, Mapping):
        manifest_full = project_render_manifest(raw_manifest, id_for_guid)
        render_manifest_view = {key: value for key, value in manifest_full.items()
                                if key != "by_source"}
        render_manifest_view["by_source_note"] = (
            "the per-source index is derivable from sources[].source_id and is not "
            "repeated here; the canonical manifest keeps it")
        render_manifest_status = "present"
    else:
        render_manifest_view = {
            "schema": "reaper-workbench.render-manifest/1",
            "version": 1,
            "sources": [],
            "complete": False,
            "missing_reason": ("this capture predates the render-dependency manifest; "
                               "audio identity cannot be proven"),
        }
        render_manifest_status = "absent"
    evidence_freshness = {
        "state": "captured",
        "revision": data.get("revision_after") or data.get("revision_before"),
        "identity": identity.to_dict(),
        "render_manifest": render_manifest_status,
        "complete": bool(render_manifest_view.get("complete")),
        "cross_revision_authoritative": bool(
            render_manifest_view.get("cross_revision_authoritative")),
        "note": ("audio measured in this capture describes this exact revision; a "
                 "later mutation delta marks the affected sources stale and a new "
                 "capture is required before those sources may be interpreted again"),
    }
    raw_structural_facts = project_map.get("digest_facts")
    if isinstance(raw_structural_facts, Mapping):
        structural_facts = dict(raw_structural_facts)
        # The delta contract compares these keys with live bridge digests, which
        # are raw REAPER GUIDs.  Say so instead of presenting a second silent
        # id space.
        structural_facts["id_basis"] = "reaper_guid"
        structural_facts["join_to_stable_ids"] = (
            "tracks[].guid is the same GUID; tracks[].stable_id is the panorama id")
    else:
        structural_facts = {
            "schema": "reaper-workbench.digest-facts/1",
            "available": False,
            "reason": "capture_predates_structural_facts",
            "tracks": {}, "items": {}, "master": None, "automation": [],
            "id_basis": "reaper_guid",
        }
    details_document = build_details_document(
        detail_ctx["refs"], capture_id=capture_id, project_id=project_key,
        revision=revision, integrity=detail_ctx["integrity"],
        window=capture_window)
    document: dict[str, Any] = {
        "schema_version": PANORAMA_SCHEMA_VERSION,
        "document_type": "capture_panorama",
        "capture_binding": {
            "capture_id": capture_id,
            "project_id": data.get("project_id"),
            "project_path": project_path,
            "record_path": relative_record_path,
            "record_schema_version": data.get("schema_version"),
            "record_integrity_sha256": integrity,
            "revision_before": data.get("revision_before"),
            "revision_after": data.get("revision_after"),
            "time_window": dict(time_window),
            "schema_version": "reaper-workbench.full-capture/1",
        },
        "reading_contract": {
            "self_describing": True,
            "full_domains_included": True,
            "requires_external_tool_call": False,
            "requires_pagination": False,
            "requires_detail_fetch_for_exact_rows": True,
            "raw_capture_is_authoritative": True,
            "raw_record_reprinted": False,
            "inline": (
                "project map, tracks, master, items, takes, FX, routes, tempo, "
                "markers, regions, audio identity, per-source measurements, "
                "automation summaries, MIDI summaries, relation kinds and rows, "
                "the activity grid and the detail manifest"
            ),
            "linked_exact": (
                "MIDI notes, envelope/automation points, per-cell analysis series "
                "(rms, band levels, temporal profile) and the render-dependency "
                "component map; each is addressed by a typed entry in details and "
                "fetched with reaper_panorama_query"
            ),
            "truly_lossy": (
                "nothing in this document drops a measured value. Inline temporal "
                "buckets are a declared aggregation of exact cells (aggregation, "
                "width, anchor and count are stated per level); exact cells remain "
                "retrievable at any resolution through the series reference"
            ),
            "response_budget": {
                "max_tokens": max_tokens,
                "max_bytes": max_bytes,
                "token_estimate_method": ("utf8_bytes/2.2 conservative fallback; "
                                          "verify with a real tokenizer when available"),
                "compact_projection": compact,
            },
            "dense_series": {
                "representation": "declared_buckets_plus_exact_reference",
                "raw_series_included": False,
                "raw_series_available_from": "details entry for the analysis (kind analysis_series)",
            },
            "facts_vs_measurements_vs_interpretations_vs_hypotheses": {
                "project_facts": "Reaper project state under project_map and plan-audio provenance",
                "signal_measurements": "DSP metrics and summaries produced by analysis",
                "model_interpretations": "empty; no role, genre, quality, or preference inferred",
                "causal_hypotheses": "empty; no cause is asserted by this builder",
            },
        },
        "provenance": {
            "builder": "reaper-workbench/context/panorama.py",
            "builder_version": PANORAMA_SCHEMA_VERSION,
            "captured_at": data.get("created_at"),
            "producer": data.get("producer"),
            "record_path": relative_record_path,
            "record_integrity_sha256": integrity,
            "archive_audio_directory": "audio/sha256",
            "raw_capture": {
                "authoritative": True,
                "record_path": relative_record_path,
                "record_integrity_sha256": integrity,
                "raw_record_included": False,
            },
        },
        "project": {
            "project_id": data.get("project_id"),
            "stable_id": project_id,
            "stable_id_basis": "sha256(project_id)[:16]",
            "path": project_path,
            "name": Path(project_path).name if project_path else None,
            "revision_before": data.get("revision_before"),
            "revision_after": data.get("revision_after"),
            "project_extent": project_extent,
            "content_extent": content_extent,
            "capture_window": capture_window,
            "render_tail": top_tail,
            "counts": {
                "tracks": len(tracks),
                "master": 1 if master_raw else 0,
                "items": len(items),
                "takes_captured": len(takes),
                "takes_expected": coverage["domains"]["takes"]["expected_count"],
                "midi_reports": len(midi),
                "midi_notes_observed": coverage["domains"]["midi"]["observed_count"],
                "fx_units": len(fx),
                "routes": len(routes),
                "automation_envelopes": len(automation_rows),
                "envelopes": len(envelope_rows),
                "markers": len(markers),
                "regions": len(regions),
                "audio_assets": len(audio_views),
                "analysis_reports": len(analysis_views),
            },
        },
        "time_map": time_map,
        "tracks": tracks,
        "master": master_view,
        "items": items,
        "takes": takes,
        "midi": midi,
        "fx": fx,
        "routes": routes,
        "automation": {
            "complete": _report_complete(project_map.get("automation")),
            "items_scanned": automation_meta.get("items_scanned"),
            "gaps": (automation_meta.get("gaps") or []),
            "envelopes": automation_rows,
        },
        "envelopes": {
            "complete": _report_complete(project_map.get("envelopes")),
            "items_scanned": envelope_meta.get("items_scanned"),
            "gaps": (envelope_meta.get("gaps") or []),
            "envelopes": envelope_rows,
        },
        "tempo": tempo_markers,
        "time_signatures": signatures,
        "markers": markers,
        "regions": regions,
        "selection": dict(selected),
        "transport": dict(transport),
        "audio": audio_views,

        "identity": identity.to_dict(),
        "render_manifest": render_manifest_view,
        "evidence_freshness": evidence_freshness,
        "structural_facts": structural_facts,
        "analysis_axis": analysis_axis,
        "analysis_contract": analysis_contract,
        "analysis_metric_units": ANALYSIS_METRIC_UNITS,
        "analysis_temporal": analysis_temporal,
        "audio_capture_revisions": audio_capture_revisions,
        "audio_capture_revision_contract": (
            "Every audio.render_provenance.capture_revision_sha256 resolves to "
            "the exact capture_revision object in audio_capture_revisions. "
            "stage and capture_method are per audio_id; source_id identifies the "
            "track/master source."),
        "analysis_archive": {
            "record_pointer": "#/analysis/{source_guid}",
            "raw_series_included": False,
            "raw_notes_pointer": "#/analysis/{source_guid}/metrics/*/note",
            "record_integrity_sha256": integrity if isinstance(integrity, str) else None,
            "record_path": relative_record_path,
        },
        "analysis": list(analysis_views.values()),
        "relations": relations_domain,
        "relations_summary": relations_summary,
        "crosslinks": links,
        "per_track": per_track,
        "coverage": coverage,
        "domain_counts": {
            "tracks": len(tracks), "items": len(items), "takes": len(takes),
            "midi": len(midi), "fx": len(fx), "routes": len(routes),
            "automation": len(automation_rows), "envelopes": len(envelope_rows),
            "audio": len(audio_views), "analysis": len(analysis_views),
            "relations": len(relations_domain.get("edges") or []),
            "relation_evaluated_pairs": len(relations_domain.get("evaluated_pairs") or []),
            "crosslinks": len(links),
            "temporal_sources": int(analysis_temporal.get("source_count") or 0),
            "detail_refs": len(details_document.get("entries") or []),
        },
        "truncation": {
            "truncated": coverage["truncated"],
            "missing": coverage["missing"],
            "silent_drop": False,
            "note": ("all canonical track/domain counts are explicit; MIDI notes and "
                     "envelope points are linked in full through details, and the "
                     "activity grid states its aggregation and resolution"),
        },
        "epistemic_layers": {
            "project_facts": {
                "included": True,
                "sources": ["project_map", "audio", "record_revision"],
                "object_counts": {"tracks": len(tracks), "items": len(items),
                                  "takes": len(takes), "fx": len(fx),
                                  "routes": len(routes), "audio": len(audio_views)},
            },
            "signal_measurements": {
                "included": True,
                "sources": ["analysis.metrics", "analysis.curves", "relations"],
                "analysis_count": len(analysis_views),
                "metric_count": sum(len(view.get("metrics") or {})
                                    for view in analysis_views.values()),
                "raw_series_included": False,
            },
            "model_interpretations": {
                "included": False,
                "items": [],
                "note": "The panorama does not infer roles, genre, quality, or preference.",
            },
            "causal_hypotheses": {
                "included": False,
                "items": [],
                "note": "The panorama records facts and measurements; it asserts no cause.",
            },
            "uncertainty": {
                "measurement_notes": sorted(
                    {
                        (str(note.get("metric")), str(note.get("note")))
                        for note in (
                            analysis_contract.get("metric_notes")
                            or [note
                                for view in analysis_views.values()
                                for note in (view.get("uncertainty") or {}).get("notes", [])]
                        )
                        if isinstance(note, Mapping) and note.get("note")
                    }
                )[:24],
                "measurement_notes_location": ("analysis_contract.metric_notes"
                                               if analysis_contract.get("metric_notes")
                                               else "analysis[].uncertainty.notes"),
                "measurement_notes_are_deduplicated": True,
                "full_notes_in_canonical_record": True,
                "raw_note_pointer": "#/analysis/*/metrics/*/note",
                "truncated_domains": coverage["truncated"],
                "missing_domains": coverage["missing"],
            },
        },
        "crosslink_contract": {
            "fields": ["from", "relation", "to"],
            "relation_names": "relation is the literal link kind; no positional legend",
            "ids": "every from/to is a stable object ID documented by legend.stable_ids",
            "qualifiers": "optional object only where slot or send context is material",
            "complete": True,
        },
        "audio_time_contract": {
            "project_window": capture_window,
            "file_to_project": ("project_time_s = audio_file_time_s + "
                                "audio_time_contract.project_window.project_start_s; "
                                "valid only inside mapping.mapped_file_end_s"),
            "project_to_file": ("audio_file_time_s = project_time_s - "
                                "audio_time_contract.project_window.project_start_s"),
            "sample_to_file": "audio_file_time_s = sample_index / format.sample_rate_hz",
            "file_to_sample": "sample_index = round(audio_file_time_s * format.sample_rate_hz)",
            "tail_rule": ("samples at or after mapping.tail_start_sample are render tail "
                          "and never map to project time"),
            "interval": INTERVAL_CONVENTION,
            "units": {"frame_count": "samples", "sample_rate_hz": "Hz",
                      "channel_count": "channels", "duration_s": "s"},
            "sample_intervals_are_half_open": True,
        },
        "details": details_document,
        "legend": {
            "time": {
                "interval": INTERVAL_CONVENTION,
                "project_fields": "fields ending in _s are seconds on the project timeline unless marked file-local",
                "file_fields": "audio.format and audio.mapping describe one WAV's local timeline",
                "sample_fields": "frame_count and sample indexes are exact, half-open sample intervals",
            },
            "stable_ids": {
                "project": "project:<sha16(project_id)>",
                "track": "track:<reaper_guid>",
                "master": "master:<reaper_guid>",
                "item": "item:<reaper_guid>",
                "take": "take:<reaper_guid>",
                "midi": "midi:<take_guid>",
                "audio": "audio:<reaper_source_guid>",
                "analysis": "analysis:<reaper_source_guid>",
                "fx": "fx:<owner_id>:<slot>",
                "route": "route:<source_id>:<send_index>",
                "envelope": "envelope:<owner_id>:<guid-or-name>",
            },
            "coverage": "Expected/observed counts and explicit partial/truncated status.",
            "epistemic_layers": "Project facts, signal measurements, interpretations and hypotheses are separated.",
            "detail_refs": ("details.entries[].fetch is the exact MCP request for the "
                            "linked rows; a ref is an address, never a replacement"),
        },
    }
    if project_end is not None:
        document["project"]["capture_window"]["project_end_s"] = project_end
    # A deterministic document hash detects sidecar tampering and accidental
    # edits.  It is computed over everything except itself.
    document["document_sha256"] = _hash(document)
    encoded = _canonical(document)
    estimated_tokens = estimate_tokens(encoded)
    too_many_bytes = max_bytes is not None and len(encoded) > max_bytes
    too_many_tokens = max_tokens is not None and estimated_tokens > max_tokens
    if too_many_bytes or too_many_tokens:
        raise PanoramaCapacityError(
            "Capture panorama exceeds the configured one-shot context budget",
            details={
                "capture_id": capture_id,
                "estimated_bytes": len(encoded),
                "max_bytes": max_bytes,
                "estimated_tokens": estimated_tokens,
                "max_tokens": max_tokens,
                "token_estimate_method": "utf8_bytes/2.2 conservative fallback",
                "tokenizer_verified": False,
                "domains": {
                    "tracks": len(tracks),
                    "items": len(items),
                    "takes": len(takes),
                    "fx": len(fx),
                    "audio": len(audio_views),
                    "analysis": len(analysis_views),
                    "detail_refs": len(details_document.get("entries") or []),
                },
                "raw_series_included": False,
                "next_action": ("use reaper_panorama_query for exact rows, or capture a "
                                "shorter section; the full canonical record remains "
                                "archived and is never truncated."),
            },
        )
    return document


def serialize_panorama(panorama: Mapping[str, Any], *, indent: int = 2) -> bytes:
    return (json.dumps(panorama, sort_keys=True, ensure_ascii=False,
                       allow_nan=False, indent=indent,
                       default=_json_default) + "\n").encode("utf-8")


def write_panorama_atomic(path: str | Path, panorama: Mapping[str, Any]) -> Path:
    """Atomically write one panorama next to its canonical capture record."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = serialize_panorama(panorama)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    return target


def load_panorama(path: str | Path, record: Any,
                  *, max_bytes: int | None = DEFAULT_MAX_BYTES,
                  max_tokens: int | None = DEFAULT_MAX_TOKENS) -> dict[str, Any]:
    """Load and verify a sidecar; raise if its binding or budget is invalid."""
    target = Path(path)
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise PanoramaBindingError("panorama sidecar is not a mapping",
                                   details={"path": str(target)})
    validate_binding(data, record)
    encoded = _canonical(data)
    estimated_tokens = estimate_tokens(encoded)
    too_many_bytes = max_bytes is not None and len(encoded) > max_bytes
    too_many_tokens = max_tokens is not None and estimated_tokens > max_tokens
    if too_many_bytes or too_many_tokens:
        raise PanoramaCapacityError(
            "Capture panorama exceeds the configured one-shot context budget",
            details={"estimated_bytes": len(encoded), "max_bytes": max_bytes,
                     "estimated_tokens": estimated_tokens,
                     "max_tokens": max_tokens, "path": str(target),
                     "raw_series_included": False},
        )
    return dict(data)


def validate_binding(panorama: Mapping[str, Any], record: Any) -> None:
    data = _record_dict(record)
    binding = panorama.get("capture_binding")
    if not isinstance(binding, Mapping):
        raise PanoramaBindingError("panorama has no capture_binding",
                                   details={"path": "capture_binding"})
    expected = {
        "capture_id": data.get("capture_id"),
        "project_id": data.get("project_id"),
        "record_integrity_sha256": data.get("integrity_sha256"),
        "revision_before": data.get("revision_before"),
        "revision_after": data.get("revision_after"),
        "time_window": data.get("time_window"),
    }
    actual = {key: binding.get(key) for key in expected}
    if actual != expected:
        raise PanoramaBindingError(
            "panorama capture binding does not match the raw record",
            details={"expected": expected, "actual": actual},
        )
    document_hash = panorama.get("document_sha256")
    if document_hash:
        payload = {key: value for key, value in panorama.items()
                   if key != "document_sha256"}
        if _hash(payload) != document_hash:
            raise PanoramaBindingError(
                "panorama document hash does not match its contents",
                details={"expected": document_hash,
                         "actual": _hash(payload)},
            )
    version = panorama.get("schema_version")
    if version != PANORAMA_SCHEMA_VERSION and version not in PANORAMA_LEGACY_SCHEMA_VERSIONS:
        raise PanoramaBindingError(
            "unsupported panorama schema version",
            details={"schema_version": version,
                     "supported": [PANORAMA_SCHEMA_VERSION,
                                   *sorted(PANORAMA_LEGACY_SCHEMA_VERSIONS)]},
        )


def panorama_path_for_record(record_path: str | Path) -> Path:
    return Path(record_path).parent / PANORAMA_FILE_NAME


__all__ = [
    "PANORAMA_SCHEMA_VERSION",
    "PANORAMA_LEGACY_SCHEMA_VERSIONS",
    "PANORAMA_FILE_NAME",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_TOKENS",
    "TOKEN_BYTES_ESTIMATE",
    "INLINE_MAP_BUCKETS",
    "estimate_tokens",
    "MIDI_NOTE_LIMIT",
    "build_panorama",
    "serialize_panorama",
    "write_panorama_atomic",
    "load_panorama",
    "validate_binding",
    "panorama_path_for_record",
]
