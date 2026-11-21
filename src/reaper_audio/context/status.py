"""One project read, many status sections — pure shaping, no host access.

The bridge already returns everything a mixing decision needs, but it returns
it in one wide payload whose cost depends on what was asked for.  This module
turns that payload into the single ``session.status`` answer: a sectioned view
whose size is controlled by ``detail`` and ``sections``.

Design rules:

* no host calls and no filesystem reads live here — the caller supplies the
  ``project.context`` payload plus the optional digest-derived rows, so the
  shape is fully testable without REAPER;
* nothing is hardcoded about the project: track names, region boundaries,
  tempo, routing and values all come from the payload.  The only constants are
  REAPER's own documented vocabularies (play-state bits, send modes,
  automation modes);
* ``summary`` keeps identities, counts and names; ``full`` adds the rows
  (sends, envelopes, items, FX chains).  A caller that asks for a section gets
  that section, never a silently empty stand-in.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import isfinite
from typing import Any

from ..errors import ValidationError
from .state_reads import state_block, state_evidence_section

STATUS_SCHEMA = "reaper-workbench.session-status/1"
PREFLIGHT_SCHEMA = "reaper-workbench.session-preflight/1"

#: Sections a status answer may contain.  ``revision`` and ``project`` are
#: always present; the list below is what callers can additionally select.
STATUS_SECTIONS: tuple[str, ...] = (
    "runtime", "transport", "revision", "tracks", "sends", "envelopes",
    "regions", "tempo", "items", "media", "master", "receipts", "evidence",
)
DETAIL_LEVELS: tuple[str, ...] = ("summary", "full")

#: REAPER's documented ``I_SENDMODE`` values.
SEND_MODES: Mapping[int, str] = {0: "post_fader", 1: "pre_fx", 2: "pre_fader"}

#: REAPER's documented track automation modes (``GetTrackAutomationMode``).
AUTOMATION_MODES: Mapping[int, str] = {
    0: "trim_read", 1: "read", 2: "touch", 3: "write", 4: "latch",
    5: "latch_preview",
}

#: REAPER's documented envelope scaling modes (``GetEnvelopeScalingMode``).
ENVELOPE_SCALES: Mapping[int, str] = {0: "amplitude", 1: "fader"}

#: REAPER's documented ``GetPlayStateEx`` bit flags.
PLAY_STATE_BITS: tuple[tuple[int, str], ...] = (
    (1, "playing"), (2, "paused"), (4, "recording"), (8, "recording_paused"),
)


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) else None


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    number = _finite(value)
    return bool(number) if number is not None else False


def _db(value: object) -> float | None:
    """Linear gain -> dB.  Zero/negative gain has no finite dB value."""
    gain = _finite(value)
    if gain is None or gain <= 0:
        return None
    from math import log10
    return 20.0 * log10(gain)


def _play_state(value: object) -> tuple[int, str]:
    """Decode ``GetPlayStateEx`` into its raw value and a stable label."""
    raw = _integer(value)
    if raw is None or raw < 0:
        raw = 0
    labels = [label for bit, label in PLAY_STATE_BITS if raw & bit]
    return raw, "+".join(labels) if labels else "stopped"


def _send_mode(value: object) -> tuple[int | None, str]:
    mode = _integer(value)
    if mode is None:
        return None, "unknown"
    return mode, SEND_MODES.get(mode, f"mode:{mode}")


def normalize_status_request(
    *, detail: str = "summary", sections: object = None
) -> tuple[str, tuple[str, ...]]:
    """Validate ``detail``/``sections`` and return a canonical request.

    ``sections=None`` means every section.  A single string is accepted for
    convenience.  Unknown names are refused with the allowed list; silently
    dropping a requested section would make an incomplete answer look complete.
    """
    if detail not in DETAIL_LEVELS:
        raise ValidationError(
            "detail must be 'summary' or 'full'",
            details={"detail": detail, "allowed": list(DETAIL_LEVELS)},
        )
    if sections is None:
        return detail, STATUS_SECTIONS
    if isinstance(sections, str):
        requested: Sequence[object] = (sections,)
    elif isinstance(sections, Sequence) and not isinstance(sections, (bytes, bytearray)):
        requested = sections
    else:
        raise ValidationError("sections must be a string, a list of strings, or None")
    selected: list[str] = []
    for name in requested:
        if not isinstance(name, str) or name not in STATUS_SECTIONS:
            raise ValidationError(
                "Unknown status section",
                details={"section": name,
                         "allowed": list(STATUS_SECTIONS)},
            )
        if name not in selected:
            selected.append(name)
    return detail, tuple(selected)


def _transport_row(context: Mapping[str, Any]) -> dict[str, Any]:
    transport = context.get("transport")
    transport = transport if isinstance(transport, Mapping) else {}
    raw, label = _play_state(transport.get("play_state"))
    return {
        "play_state": raw,
        "state": label,
        "playing": bool(raw & 1),
        "recording": bool(raw & 4),
        "cursor_s": _finite(transport.get("cursor_s")),
        "edit_cursor_s": _finite(transport.get("edit_cursor_s")),
        "play_position_s": _finite(transport.get("play_position_s")),
        "play_position_kind": _text(transport.get("play_position_kind")),
    }


def _send_count(track: Mapping[str, Any]) -> int:
    sends = track.get("sends")
    if isinstance(sends, list):
        return len(sends)
    count = _integer(sends)
    return count if count is not None and count >= 0 else 0


def _fx_row(fx: Mapping[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "index": _integer(fx.get("index")),
        "guid": str(fx.get("guid") or ""),
        "name": str(fx.get("name") or ""),
        "enabled": _flag(fx.get("enabled")),
        "offline": _flag(fx.get("offline")),
        "parameter_count": _integer(fx.get("parameter_count")),
    }
    parameters = fx.get("parameters")
    if isinstance(parameters, list):
        row["parameters"] = [
            {
                "index": _integer(item.get("index")),
                "name": str(item.get("name") or ""),
                "normalized": _finite(item.get("normalized")),
                "formatted": str(item.get("formatted") or ""),
            }
            for item in parameters if isinstance(item, Mapping)
        ]
    summary = fx.get("summary")
    if isinstance(summary, Mapping):
        row["summary"] = dict(summary)
    return row


def _track_row(track: Mapping[str, Any], *, detail: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "guid": str(track.get("guid") or ""),
        "index": _integer(track.get("index")),
        "name": str(track.get("name") or ""),
        "volume_db": _db(track.get("volume")),
        "pan": _finite(track.get("pan")),
        "mute": _flag(track.get("mute")),
        "solo": _integer(track.get("solo")) or 0,
        "main_send": _flag(track.get("main_send")),
        "folder_depth": _integer(track.get("folder_depth")) or 0,
        "fx_count": len(track.get("fx") or []),
        "fx_names": [str(item.get("name") or "") for item in track.get("fx") or []
                     if isinstance(item, Mapping)],
        "send_count": _send_count(track),
        "item_count": len(track.get("items") or []),
    }
    if detail == "full":
        row.update({
            "volume": _finite(track.get("volume")),
            "record_arm": _integer(track.get("record_arm")),
            "channel_count": _integer(track.get("channel_count")),
            "receives": _integer(track.get("receives")),
            "hardware_sends": _integer(track.get("hardware_sends")),
            "automation_mode": _integer(track.get("automation_mode")),
            "fx": [_fx_row(fx) for fx in track.get("fx") or []
                   if isinstance(fx, Mapping)],
        })
    return row


def _owner_rows(context: Mapping[str, Any]) -> list[tuple[Mapping[str, Any], bool]]:
    owners: list[tuple[Mapping[str, Any], bool]] = [
        (track, False) for track in context.get("tracks") or []
        if isinstance(track, Mapping)
    ]
    master = context.get("master")
    if isinstance(master, Mapping):
        owners.append((master, True))
    return owners


def _send_rows(context: Mapping[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Flatten every track's sends; the flag says whether rows were readable.

    ``project.context`` alone only carries a count.  The session layer merges
    the digest's send rows into ``track["sends"]``; when that did not happen
    the caller still gets truthful counts instead of invented routes.
    """
    rows: list[dict[str, Any]] = []
    complete = True
    for owner, is_master in _owner_rows(context):
        sends = owner.get("sends")
        if not isinstance(sends, list):
            if _send_count(owner):
                complete = False
            continue
        for send in sends:
            if not isinstance(send, Mapping):
                continue
            mode, mode_name = _send_mode(send.get("mode"))
            volume = _finite(send.get("volume"))
            rows.append({
                "source_guid": str(owner.get("guid") or ""),
                "source_name": "master" if is_master else str(owner.get("name") or ""),
                "is_master_source": is_master,
                "index": _integer(send.get("index")),
                "dest_guid": str(send.get("dest_guid") or ""),
                "dest_name": str(send.get("dest_name") or ""),
                "volume": volume,
                "volume_db": _db(volume),
                "pan": _finite(send.get("pan")),
                "mode": mode,
                "pre_post": mode_name,
                "enabled": not _flag(send.get("mute")),
                "mute": _flag(send.get("mute")),
                "phase": _flag(send.get("phase")),
                "mono": _flag(send.get("mono")),
            })
    return rows, complete


def _send_section(context: Mapping[str, Any], *, detail: str) -> dict[str, Any]:
    rows, complete = _send_rows(context)
    counted = sum(_send_count(owner) for owner, _ in _owner_rows(context))
    count = len(rows) if rows else counted
    sources: dict[str, int] = {}
    for row in rows:
        key = row["source_name"] or row["source_guid"]
        sources[key] = sources.get(key, 0) + 1
    if detail == "summary":
        return {
            "count": count,
            "complete": complete and (not counted or len(rows) == counted),
            "by_source": sources,
            "destinations": sorted({row["dest_name"] for row in rows
                                    if row["dest_name"]}),
        }
    return {"count": count,
            "complete": complete and (not counted or len(rows) == counted),
            "rows": rows}


def envelope_range(points: object) -> tuple[float | None, float | None]:
    """Min/max of recorded envelope point values; ``None`` when unreadable."""
    if not isinstance(points, list):
        return None, None
    values = [_finite(point.get("value")) for point in points
              if isinstance(point, Mapping)]
    numbers = [value for value in values if value is not None]
    if not numbers:
        return None, None
    return min(numbers), max(numbers)


def _envelope_row(owner: Mapping[str, Any], envelope: Mapping[str, Any],
                  *, is_master: bool) -> dict[str, Any]:
    mode = _integer(owner.get("automation_mode"))
    scale = _integer(envelope.get("scaling_mode"))
    low, high = envelope_range(envelope.get("points"))
    return {
        "owner_guid": str(owner.get("guid") or ""),
        "owner_name": "master" if is_master else str(owner.get("name") or ""),
        "is_master": is_master,
        "guid": str(envelope.get("guid") or ""),
        "name": str(envelope.get("name") or ""),
        "automation_mode": mode,
        "automation_mode_name": AUTOMATION_MODES.get(mode) if mode is not None else None,
        "active": _flag(envelope.get("active")),
        "armed": _flag(envelope.get("armed")),
        "visible": _flag(envelope.get("visible")),
        "scale_mode": scale,
        "scale": ENVELOPE_SCALES.get(scale, f"mode:{scale}") if scale is not None else None,
        "point_count": _integer(envelope.get("point_count")) or 0,
        "automation_item_count": _integer(envelope.get("automation_item_count")) or 0,
        "min": low,
        "max": high,
    }


def envelope_rows(context: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every readable envelope row, owned by a track or the master."""
    rows: list[dict[str, Any]] = []
    for owner, is_master in _owner_rows(context):
        envelopes = owner.get("envelopes")
        if not isinstance(envelopes, list):
            continue
        rows.extend(_envelope_row(owner, envelope, is_master=is_master)
                    if isinstance(envelope, Mapping) else {}
                    for envelope in envelopes)
    return [row for row in rows if row]


def _envelope_section(context: Mapping[str, Any], *, detail: str) -> dict[str, Any]:
    rows = envelope_rows(context)
    if detail == "summary":
        owners: dict[str, int] = {}
        for row in rows:
            key = row["owner_name"] or row["owner_guid"]
            owners[key] = owners.get(key, 0) + 1
        return {"count": len(rows), "by_owner": owners,
                "point_total": sum(row["point_count"] for row in rows)}
    return {"count": len(rows), "rows": rows}


def _regions(context: Mapping[str, Any], *, detail: str) -> list[dict[str, Any]]:
    rows = [row for row in context.get("regions") or [] if isinstance(row, Mapping)]
    if detail == "summary":
        return [{"name": str(row.get("name") or ""),
                 "start_s": _finite(row.get("position_s")),
                 "end_s": _finite(row.get("end_s"))} for row in rows]
    return [{
        "index": _integer(row.get("index")),
        "number": _finite(row.get("number")),
        "name": str(row.get("name") or ""),
        "start_s": _finite(row.get("position_s")),
        "end_s": _finite(row.get("end_s")),
        "color": _finite(row.get("color")),
    } for row in rows]


def _markers(context: Mapping[str, Any], *, detail: str) -> dict[str, Any]:
    rows = [row for row in context.get("markers") or [] if isinstance(row, Mapping)]
    if detail == "summary":
        return {"count": len(rows),
                "names": [str(row.get("name") or "") for row in rows]}
    return {"count": len(rows), "rows": [{
        "index": _integer(row.get("index")),
        "number": _finite(row.get("number")),
        "name": str(row.get("name") or ""),
        "position_s": _finite(row.get("position_s")),
        "color": _finite(row.get("color")),
    } for row in rows]}


def _tempo(context: Mapping[str, Any], *, detail: str) -> dict[str, Any]:
    tempo = context.get("tempo")
    tempo = tempo if isinstance(tempo, Mapping) else {}
    markers = [row for row in tempo.get("markers") or []
               if isinstance(row, Mapping)]
    result: dict[str, Any] = {
        "bpm": _finite(tempo.get("bpm")),
        "numerator": _integer(tempo.get("numerator")),
        "denominator": _integer(tempo.get("denominator")),
        "marker_count": len(markers),
    }
    if detail == "full":
        result["markers"] = [{
            "time_s": _finite(row.get("time_s")),
            "measure": _finite(row.get("measure")),
            "beat": _finite(row.get("beat")),
            "bpm": _finite(row.get("bpm")),
            "numerator": _integer(row.get("numerator")),
            "denominator": _integer(row.get("denominator")),
            "linear": _flag(row.get("linear")),
        } for row in markers]
    return result


def _item_row(track: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
    take = item.get("active_take")
    take = take if isinstance(take, Mapping) else {}
    midi = take.get("midi")
    note_count = item.get("midi_note_count")
    if note_count is None and isinstance(midi, Mapping):
        note_count = midi.get("note_count")
    source_online = item.get("source_online")
    return {
        "track_guid": str(track.get("guid") or ""),
        "track_name": str(track.get("name") or ""),
        "guid": str(item.get("guid") or ""),
        "position_s": _finite(item.get("position_s")),
        "length_s": _finite(item.get("length_s")),
        "start_offset_s": _finite(item.get("start_offset_s")),
        "playrate": _finite(item.get("playrate")),
        "mute": _flag(item.get("mute")),
        "selected": _flag(item.get("selected")),
        "take_count": _integer(item.get("take_count")) or 0,
        "fade_in_s": _finite(item.get("fade_in_s")),
        "fade_out_s": _finite(item.get("fade_out_s")),
        "source": str(take.get("source") or ""),
        "is_midi": _flag(take.get("is_midi")),
        "source_online": (_flag(source_online)
                          if "source_online" in item else None),
        "midi_note_count": _integer(note_count),
    }


def _items(context: Mapping[str, Any], *, detail: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for track in context.get("tracks") or []:
        if not isinstance(track, Mapping):
            continue
        for item in track.get("items") or []:
            if isinstance(item, Mapping):
                rows.append(_item_row(track, item))
    counts = {
        "count": len(rows),
        "audio_count": sum(1 for row in rows if not row["is_midi"]),
        "midi_count": sum(1 for row in rows if row["is_midi"]),
        "muted_count": sum(1 for row in rows if row["mute"]),
        "offline_count": sum(1 for row in rows if row["source_online"] is False),
    }
    if detail == "summary":
        return counts
    return {**counts, "rows": rows}


def _media(offline: Sequence[Mapping[str, Any]] | None,
           missing: Sequence[Mapping[str, Any]] | None, *, detail: str) -> dict[str, Any]:
    rows = [dict(row) for row in offline or [] if isinstance(row, Mapping)]
    missing_rows = [dict(row) for row in missing or [] if isinstance(row, Mapping)]
    if detail == "summary":
        return {
            "offline_count": len(rows),
            "missing_count": len(missing_rows),
            "offline_tracks": sorted({str(row.get("track_name") or "")
                                      for row in rows}),
            "missing_sources": [str(row.get("source") or "") for row in missing_rows],
        }
    return {
        "offline_count": len(rows),
        "missing_count": len(missing_rows),
        "offline": rows,
        "missing": missing_rows,
    }


def _master_row(context: Mapping[str, Any], *, detail: str) -> dict[str, Any]:
    master = context.get("master")
    master = master if isinstance(master, Mapping) else {}
    fx = [fx for fx in master.get("fx") or [] if isinstance(fx, Mapping)]
    envelopes = [row for row in master.get("envelopes") or []
                 if isinstance(row, Mapping)]
    row: dict[str, Any] = {
        "guid": str(master.get("guid") or ""),
        "name": str(master.get("name") or "") or "master",
        "volume_db": _db(master.get("volume")),
        "pan": _finite(master.get("pan")),
        "mute": _flag(master.get("mute")),
        "main_send": _flag(master.get("main_send")),
        "fx_count": len(fx),
        "fx_names": [str(item.get("name") or "") for item in fx],
        "envelope_count": len(envelopes),
    }
    if detail == "full":
        row.update({
            "volume": _finite(master.get("volume")),
            "channel_count": _integer(master.get("channel_count")),
            "fx": [_fx_row(item) for item in fx],
            "sends": _send_rows({"tracks": [], "master": master})[0],
        })
    return row


def _receipts_section(receipts: Mapping[str, Any] | None,
                      *, detail: str) -> dict[str, Any]:
    data = dict(receipts or {})
    records = [dict(row) for row in data.get("records") or []
               if isinstance(row, Mapping)]
    total = data.get("count", len(records))
    if detail == "summary":
        return {
            "state": data.get("state", "unknown"),
            "count": total,
            "returned": len(records),
            "latest": records[-1] if records else None,
            "source": data.get("source"),
        }
    return {"state": data.get("state", "unknown"), "count": total,
            "returned": len(records), "source": data.get("source"),
            "records": records}


def _evidence_section(evidence: Mapping[str, Any] | None,
                      *, detail: str) -> dict[str, Any]:
    data = dict(evidence or {})
    if detail == "summary":
        return {
            "state": data.get("state", "unknown"),
            "reason": data.get("reason"),
            "units_total": data.get("units_total", 0),
            "units_current": data.get("units_current", 0),
            "units_stale": data.get("units_stale", 0),
        }
    result = dict(data)
    result.setdefault("state", "unknown")
    return result


def build_status(
    *,
    context: Mapping[str, Any],
    detail: str = "summary",
    sections: object = None,
    runtime: Mapping[str, Any] | None = None,
    offline: Sequence[Mapping[str, Any]] | None = None,
    missing: Sequence[Mapping[str, Any]] | None = None,
    receipts: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
    state: Mapping[str, Any] | None = None,
    state_since: int | None = None,
) -> dict[str, Any]:
    """Shape one ``project.context`` read into the sectioned status answer.

    ``context`` may be enriched by the session layer: ``track["sends"]`` as
    digest rows, ``track["envelopes"]`` / ``master["envelopes"]`` as digest
    automation rows, and ``item["midi_note_count"]`` where it was countable.
    Missing enrichment degrades to counts, never to invented values.

    ``state`` is the project's state document when one exists.  It then answers
    the ``evidence`` section from stored facts and adds an additive top-level
    ``state`` block (``state_token``/``facts``/``current``/``stale``/
    ``superseded``, plus ``changes_since`` when ``state_since`` is supplied).
    Without state the section is the caller-supplied legacy evidence answer.
    The block is top-level because ``evidence["state"]`` already carries the
    evidence verdict string; nesting it there would replace that field.
    """
    detail, selected = normalize_status_request(detail=detail, sections=sections)
    tracks = [track for track in context.get("tracks") or []
              if isinstance(track, Mapping)]
    revision = context.get("revision")
    revision = dict(revision) if isinstance(revision, Mapping) else {}
    result: dict[str, Any] = {
        "schema": STATUS_SCHEMA,
        "detail": detail,
        "sections": list(selected),
        "project": {
            "project_id": str(revision.get("project_id") or ""),
            "name": str(context.get("name") or ""),
            "path": str(context.get("path") or revision.get("path") or ""),
            "length_s": _finite(context.get("length_s")),
            "track_count": (_integer(context.get("track_count"))
                            if _integer(context.get("track_count")) is not None
                            else len(tracks)),
        },
        "revision": revision,
    }
    if "runtime" in selected:
        result["runtime"] = dict(runtime) if isinstance(runtime, Mapping) else {
            "state": "unknown", "reason": "runtime_not_read"}
    if "transport" in selected:
        result["transport"] = _transport_row(context)
    if "tracks" in selected:
        result["tracks"] = [_track_row(track, detail=detail) for track in tracks]
        result["soloed"] = [row["name"] or row["guid"] for row in result["tracks"]
                            if row["solo"]]
        result["muted"] = [row["name"] or row["guid"] for row in result["tracks"]
                           if row["mute"]]
    if "sends" in selected:
        result["sends"] = _send_section(context, detail=detail)
    if "envelopes" in selected:
        result["envelopes"] = _envelope_section(context, detail=detail)
    if "regions" in selected:
        result["regions"] = _regions(context, detail=detail)
        result["markers"] = _markers(context, detail=detail)
    if "tempo" in selected:
        result["tempo"] = _tempo(context, detail=detail)
    if "items" in selected:
        result["items"] = _items(context, detail=detail)
    if "media" in selected:
        result["media"] = _media(offline, missing, detail=detail)
    if "master" in selected:
        result["master"] = _master_row(context, detail=detail)
    if "receipts" in selected:
        result["receipts"] = _receipts_section(receipts, detail=detail)
    if "evidence" in selected:
        if state is not None:
            result["evidence"] = state_evidence_section(state, detail=detail)
            result["state"] = state_block(state, since=state_since)
        else:
            result["evidence"] = _evidence_section(evidence, detail=detail)
    return result


def observation_codes(*collections: Sequence[Mapping[str, Any]]) -> list[str]:
    """Sorted unique observation codes from blocker/warning rows.

    A code states what was observed (a solo, offline media, a muted master),
    never a quality judgement: the caller decides what to do about it.
    """
    codes = {str(row.get("code")) for rows in collections for row in rows
             if isinstance(row, Mapping) and row.get("code")}
    return sorted(codes)


__all__ = [
    "AUTOMATION_MODES",
    "DETAIL_LEVELS",
    "ENVELOPE_SCALES",
    "PREFLIGHT_SCHEMA",
    "PLAY_STATE_BITS",
    "SEND_MODES",
    "STATUS_SCHEMA",
    "STATUS_SECTIONS",
    "build_status",
    "envelope_range",
    "envelope_rows",
    "normalize_status_request",
    "observation_codes",
]
