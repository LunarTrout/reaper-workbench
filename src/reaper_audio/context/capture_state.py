"""Fill one project's mutable state from a completed capture.

A capture already computed everything this module needs: live project rows (a
digest, a status answer or a raw context), one analysis report per rendered
source, and the interval those reports describe.  This adapter joins those
facts into :class:`~reaper_audio.context.project_state.ProjectState`; it does
not measure, aggregate, round or rename anything.

The write order is the contract:

1. read the live project rows **once** (``session.digest()``, then
   ``session.status(detail="full")``, then a raw snapshot) and fingerprint
   tracks, master and routing with the state module's own functions;
2. compare those fingerprints with the document's stored ones through
   :func:`~reaper_audio.context.state_diff.diff_fingerprints`, so a changed
   fader, FX chain, send or automation curve is named before anything is
   written;
3. call :meth:`ProjectState.mark_stale` with exactly those changes, so facts
   measured against the previous project state stop being ``current`` *before*
   a fresh fact can supersede them;
4. upsert one fact per captured source and analysis interval, carrying the
   record's own stage, window, revision and metric values unchanged.

Only rows the project read actually carries are fingerprinted.  A field that
was not read stays ``null`` in the fingerprint, exactly as
:mod:`reaper_audio.context.project_state` documents; this module never fills a
missing field from a second read.  External-file sources are measured against
no project object at all, so their facts carry three ``null`` fingerprints.

Evidence ids follow the library's existing source-id convention
(``track:{guid}``, ``master:{guid}``, ``file:{sha256}``).  A record that carries
an explicit evidence id wins; otherwise the id is derived from the source row
and its ``is_master``/kind flag, which is the same identity the measure layer
records for the same asset.

Nothing here reads REAPER directly, knows a project name, or owns a metric
vocabulary: the metric names come from the analysis report that the record
already contains.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import ValidationError
from .project_state import (
    ProjectState,
    fingerprint_master,
    fingerprint_routing,
    fingerprint_track,
)
from .state_diff import STAGE_EXTERNAL_FILE, diff_fingerprints

__all__ = ["fill_state_from_capture"]

_TRACK_EVIDENCE_PREFIX = "track:"
_MASTER_EVIDENCE_PREFIX = "master:"
_FILE_EVIDENCE_PREFIX = "file:"

#: Source-row ``kind`` values that mean "a file outside the project".
_EXTERNAL_KINDS = frozenset({"file", "external", "path"})

#: The per-band level vector is one measured value in the record's curves.
#: It is copied whole: averaging bands here would invent a measurement.
_BAND_LEVEL_KEY = "band_level_dbfs"


# --------------------------------------------------------------------------- #
# small, total readers
# --------------------------------------------------------------------------- #


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _number_list(value: Any) -> list[float] | None:
    """The exact finite numbers of a measured vector, or ``None`` if partial."""
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        return None
    numbers: list[float] = []
    for item in value:
        number = _finite(item)
        if number is None:
            return None
        numbers.append(number)
    return numbers


def _plain_json(value: Any) -> Any:
    """Deep-copy a sealed record value into plain JSON containers.

    Capture records freeze their mappings (``MappingProxyType``) and tuples;
    the state document stores ordinary JSON, so a revision read from a sealed
    record is thawed before it is attached to a fact.
    """
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _metric_value(cell: Any) -> Any:
    """The value a report cell already carries, without coercion beyond JSON.

    A metric cell is either ``{"value": ...}`` (the analyzer's shape) or the
    value itself.  Missing, boolean and non-finite values are not measurements
    and are omitted; finite numbers, text and finite numeric vectors are copied
    exactly.
    """
    value = cell.get("value") if isinstance(cell, Mapping) else cell
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value
    numbers = _number_list(value)
    return numbers or None


def _window_pair(value: Any) -> list[float | None] | None:
    """Normalise a record window to ``[start_s, end_s]`` or ``None``.

    Both the capture asset shape (``project_start_s``/``project_end_s``) and
    the analysis/measurement shape (``start_s``/``end_s``) are accepted.  A
    mapping with neither pair is not a window.
    """
    if not isinstance(value, Mapping):
        return None
    start = value.get("start_s")
    if start is None:
        start = value.get("project_start_s")
    end = value.get("end_s")
    if end is None:
        end = value.get("project_end_s")
    if start is None and end is None:
        return None
    start_number = _finite(start)
    end_number = _finite(end)
    if start_number is None and start is not None:
        return None
    if end_number is None and end is not None:
        return None
    if start_number is not None and end_number is not None and start_number > end_number:
        return None
    return [start_number, end_number]


def _section_window(section: Any) -> list[float | None] | None:
    if isinstance(section, Mapping):
        return _window_pair(section)
    if (isinstance(section, Sequence) and not isinstance(section, (str, bytes))
            and len(section) == 2):
        start, end = _finite(section[0]), _finite(section[1])
        if start is None and end is None:
            return None
        if start is not None and end is not None and start > end:
            return None
        return [start, end]
    return None


def _record_view(record: Any) -> Mapping[str, Any]:
    """Read a record's capture fields without thawing its full analysis map.

    ``FullCaptureRecord.to_mapping()`` deep-copies the analysis payload, which
    can be hundreds of megabytes.  Attribute access on the sealed record is
    cheap and is what the capture service already holds; plain mappings are
    accepted for tests and for JSON records read from an archive.
    """
    if isinstance(record, Mapping):
        return record
    return {
        "project_id": getattr(record, "project_id", None),
        "analysis": getattr(record, "analysis", None),
        "audio": getattr(record, "audio", None),
        "sources": getattr(record, "sources", None),
        "project_map": getattr(record, "project_map", None),
        "time_window": getattr(record, "time_window", None),
        "revision_before": getattr(record, "revision_before", None),
        "revision_after": getattr(record, "revision_after", None),
    }


# --------------------------------------------------------------------------- #
# one live project read
# --------------------------------------------------------------------------- #


def _read_session_rows(session: Any) -> tuple[list[dict[str, Any]],
                                              Mapping[str, Any] | None,
                                              list[dict[str, Any]] | None]:
    """Read the live project rows once: digest, status or raw snapshot.

    Each branch is a complete project read; mixing two branches would let a
    fingerprint compare fields read at different moments.  ``routing`` is
    ``None`` when the read could not prove the routing graph, so an unreadable
    send section never looks like an empty one.
    """
    digest_reader = getattr(session, "digest", None)
    if callable(digest_reader):
        digest = digest_reader()
        if isinstance(digest, Mapping):
            fields = digest.get("fields")
            if (isinstance(fields, Mapping)
                    and ("tracks" in fields or "master" in fields)):
                return _digest_rows(digest)

    status_reader = getattr(session, "status", None)
    if callable(status_reader):
        try:
            status = status_reader(detail="full")
        except TypeError:
            status = status_reader()
        if isinstance(status, Mapping) and isinstance(status.get("tracks"), (list, tuple)):
            return _status_rows(status)

    snapshot_reader = getattr(session, "snapshot", None)
    if callable(snapshot_reader):
        context = snapshot_reader(include_items=True, include_fx_params=True)
        if isinstance(context, Mapping):
            return _snapshot_rows(context)

    return [], None, None


def _automation_envelopes(
    automation: Any, master_guid: str | None
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]] | None]:
    """Group digest automation rows by owner; ``None`` means "not read".

    The digest stores automation in four places: per-track plain envelopes,
    FX envelopes, take envelopes and the master list.  Every row names its
    owner in ``track_guid``; the master rows are either flagged ``is_master``
    or carry the master GUID.
    """
    if not isinstance(automation, (Mapping, list, tuple)):
        return {}, None
    by_track: dict[str, list[dict[str, Any]]] = {}
    master: list[dict[str, Any]] = []

    def add(row: Any, owner: str | None) -> None:
        if not isinstance(row, Mapping):
            return
        item = dict(row)
        guid = _text(item.get("track_guid")) or owner
        if guid is None:
            return
        if item.get("is_master") is True or (master_guid is not None
                                             and guid == master_guid):
            master.append(item)
        else:
            by_track.setdefault(guid, []).append(item)

    if isinstance(automation, Mapping):
        for group in _rows(automation.get("tracks")):
            if not isinstance(group, Mapping):
                continue
            owner = _text(group.get("track_guid"))
            for envelope in _rows(group.get("envelopes")):
                add(envelope, owner)
        for key in ("fx", "items", "master"):
            for row in _rows(automation.get(key)):
                add(row, None)
    else:
        for row in automation:
            add(row, None)
    return by_track, master


def _digest_rows(digest: Mapping[str, Any]
                 ) -> tuple[list[dict[str, Any]], Mapping[str, Any] | None,
                            list[dict[str, Any]] | None]:
    fields = _mapping(digest.get("fields"))
    tracks = [dict(row) for row in _rows(fields.get("tracks"))
              if isinstance(row, Mapping) and _text(row.get("guid"))]
    master_row = fields.get("master")
    master = dict(master_row) if isinstance(master_row, Mapping) else None
    master_guid = _text((master or {}).get("guid"))

    items_group = fields.get("items")
    if isinstance(items_group, list):
        items_by_track: dict[str, list[dict[str, Any]]] = {}
        for item in items_group:
            if not isinstance(item, Mapping):
                continue
            owner = _text(item.get("track_guid"))
            if owner is not None:
                items_by_track.setdefault(owner, []).append(dict(item))
        for row in tracks:
            row["items"] = items_by_track.get(str(row.get("guid")), [])

    envelopes_by_track, master_envelopes = _automation_envelopes(
        fields.get("automation"), master_guid)
    for row in tracks:
        guid = str(row.get("guid"))
        if guid in envelopes_by_track:
            row["envelopes"] = envelopes_by_track[guid]
    if master is not None and master_envelopes is not None:
        master["envelopes"] = master_envelopes

    coverage = _mapping(digest.get("coverage"))
    routing: list[dict[str, Any]] | None = None
    owners = [row for row in tracks if isinstance(row.get("sends"), list)]
    if master is not None and isinstance(master.get("sends"), list):
        owners.append(master)
    if owners and coverage.get("sends") is not False:
        routing = owners
    return tracks, master, routing


def _status_rows(status: Mapping[str, Any]
                 ) -> tuple[list[dict[str, Any]], Mapping[str, Any] | None,
                            list[dict[str, Any]] | None]:
    tracks = [dict(row) for row in _rows(status.get("tracks"))
              if isinstance(row, Mapping) and _text(row.get("guid"))]
    master_row = status.get("master")
    master = dict(master_row) if isinstance(master_row, Mapping) else None
    master_guid = _text((master or {}).get("guid"))

    sends_section = _mapping(status.get("sends"))
    send_rows = [dict(row) for row in _rows(sends_section.get("rows"))
                 if isinstance(row, Mapping)]
    sends_complete = sends_section.get("complete") is True

    envelopes_section = _mapping(status.get("envelopes"))
    envelope_rows = [dict(row) for row in _rows(envelopes_section.get("rows"))
                     if isinstance(row, Mapping)]

    items_section = _mapping(status.get("items"))
    item_rows = [dict(row) for row in _rows(items_section.get("rows"))
                 if isinstance(row, Mapping)]

    if envelope_rows:
        by_owner: dict[str, list[dict[str, Any]]] = {}
        for envelope in envelope_rows:
            owner = _text(envelope.get("owner_guid"))
            if owner is not None:
                by_owner.setdefault(owner, []).append(envelope)
        for row in tracks:
            guid = str(row.get("guid"))
            if guid in by_owner:
                row["envelopes"] = by_owner[guid]
        if master is not None and master_guid in by_owner:
            master["envelopes"] = by_owner[master_guid]

    if item_rows:
        by_track: dict[str, list[dict[str, Any]]] = {}
        for item in item_rows:
            owner = _text(item.get("track_guid"))
            if owner is not None:
                by_track.setdefault(owner, []).append(item)
        for row in tracks:
            guid = str(row.get("guid"))
            if guid in by_track:
                row["items"] = by_track[guid]

    routing: list[dict[str, Any]] | None = None
    if sends_complete:
        # A complete send section proves the whole routing graph, including
        # "no sends"; an empty owner list is a measurement, not missing data.
        by_source: dict[str, list[dict[str, Any]]] = {}
        for send in send_rows:
            owner = _text(send.get("source_guid"))
            if owner is not None:
                by_source.setdefault(owner, []).append(send)
        for row in tracks:
            row["sends"] = by_source.get(str(row.get("guid")), [])
        if master is not None:
            master["sends"] = by_source.get(master_guid or "", [])
        routing = send_rows
    else:
        owners = [row for row in tracks if isinstance(row.get("sends"), list)]
        if master is not None and isinstance(master.get("sends"), list):
            owners.append(master)
        routing = owners or None
    return tracks, master, routing


def _snapshot_rows(context: Mapping[str, Any]
                   ) -> tuple[list[dict[str, Any]], Mapping[str, Any] | None,
                              list[dict[str, Any]] | None]:
    """A raw ``project.context`` read has the same owner rows as a digest."""
    tracks = [dict(row) for row in _rows(context.get("tracks"))
              if isinstance(row, Mapping) and _text(row.get("guid"))]
    master_row = context.get("master")
    master = dict(master_row) if isinstance(master_row, Mapping) else None
    owners = [row for row in tracks if isinstance(row.get("sends"), list)]
    if master is not None and isinstance(master.get("sends"), list):
        owners.append(master)
    return tracks, master, owners or None


# --------------------------------------------------------------------------- #
# fingerprints and the object diff
# --------------------------------------------------------------------------- #


def _fingerprint_fields(fingerprint: Any) -> dict[str, Any]:
    record = _mapping(fingerprint)
    fields = record.get("fields")
    return dict(fields) if isinstance(fields, Mapping) else {}


def _stored_shape(state: ProjectState) -> dict[str, Any]:
    """The document's fingerprints as plain field maps for the diff."""
    objects = _mapping(state.objects)
    tracks: dict[str, dict[str, Any]] = {}
    for guid, record in _mapping(objects.get("tracks")).items():
        tracks[str(guid)] = _fingerprint_fields(_mapping(record).get("fingerprint"))
    master = _fingerprint_fields(_mapping(objects.get("master")).get("fingerprint"))
    routing_record = _mapping(objects.get("routing"))
    routing = routing_record.get("fingerprint")
    return {
        "tracks": tracks,
        "master": master,
        "routing": dict(routing) if isinstance(routing, Mapping) else None,
    }


def _merge_fields(left: list[str], right: Sequence[str]) -> list[str]:
    return sorted(set(left) | {str(field) for field in right})


def _changed_from_diff(diff: Mapping[str, Any],
                       objects: Mapping[str, Any]) -> tuple[dict[str, list[str]], bool]:
    """Object changes for ``mark_stale``, plus the routing flag.

    The diff is the primary evidence.  ``upsert_objects`` adds identity facts a
    field diff cannot see (a new master GUID, a new object), so its ``changed``
    map is merged in rather than replaced.
    """
    changed: dict[str, list[str]] = {}
    for guid in _rows(diff.get("added_tracks")) + _rows(diff.get("removed_tracks")):
        changed[str(guid)] = []
    for guid, entry in _mapping(diff.get("tracks")).items():
        fields = entry.get("fields") if isinstance(entry, Mapping) else None
        changed[str(guid)] = [str(field) for field in _rows(fields)]
    master_fields = _rows(_mapping(diff.get("master")).get("fields"))
    if master_fields:
        changed["master"] = [str(field) for field in master_fields]

    reported = _mapping(objects.get("changed"))
    for guid, fields in reported.items():
        key = str(guid)
        changed[key] = _merge_fields(changed.get(key, []), _rows(fields))
    routing_changed = (bool(_mapping(diff.get("routing")).get("changed"))
                       or bool(objects.get("routing_changed")))
    return changed, routing_changed


# --------------------------------------------------------------------------- #
# capture sources and their analysis
# --------------------------------------------------------------------------- #


def _source_metadata(row: Any) -> dict[str, Any]:
    return dict(row) if isinstance(row, Mapping) else {}


def _source_rows(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One row per source that has an analysis report, in evidence-id order.

    Audio assets carry the rendered stage and signal window; a measure-style
    ``sources`` row carries the same identity for external files.  When both
    describe one source, the audio row supplies the capture fields and the
    source row supplies whatever only it knows (an explicit evidence id).
    """
    analysis = _mapping(record.get("analysis"))
    by_key: dict[str, dict[str, Any]] = {}
    for row in _rows(record.get("sources")):
        if not isinstance(row, Mapping):
            continue
        key = _text(row.get("source_id")) or _text(row.get("source_guid"))
        if key is not None:
            by_key[key] = _source_metadata(row)
    for row in _rows(record.get("audio")):
        if not isinstance(row, Mapping):
            continue
        key = _text(row.get("source_guid"))
        if key is not None:
            merged = dict(by_key.get(key, {}))
            merged.update(_source_metadata(row))
            by_key[key] = merged

    project_map = _mapping(record.get("project_map"))
    track_guids = {
        str(track.get("guid"))
        for track in _rows(project_map.get("tracks"))
        if isinstance(track, Mapping) and _text(track.get("guid"))
    }
    master_guid = _text(_mapping(project_map.get("master")).get("guid"))
    if master_guid is None:
        master_guid = _text(_mapping(record.get("master")).get("guid"))

    rows: list[dict[str, Any]] = []
    for raw_key in sorted(analysis, key=str):
        key = str(raw_key)
        metadata = dict(by_key.get(key, {}))
        metadata.setdefault("source_id", key)
        metadata["_analysis_key"] = key
        metadata["_project_kind"] = (
            "master" if master_guid is not None and key == master_guid
            else "track" if key in track_guids
            else "unknown"
        )
        rows.append(metadata)
    rows.sort(key=lambda row: (
        _evidence_id(row), str(row.get("stage") or ""),
        str(row.get("source_id") or ""),
    ))
    return rows


def _is_external(row: Mapping[str, Any]) -> bool:
    kind = str(row.get("kind") or "").strip().lower()
    if kind in _EXTERNAL_KINDS or row.get("is_external") is True:
        return True
    source_id = str(row.get("source_id") or "")
    key = str(row.get("_analysis_key") or source_id)
    if source_id.startswith(_FILE_EVIDENCE_PREFIX) or key.startswith(_FILE_EVIDENCE_PREFIX):
        return True
    stage = str(row.get("stage") or "").lower()
    return "external" in stage


def _evidence_id(row: Mapping[str, Any]) -> str:
    """The record's evidence id, or the library convention for its source."""
    explicit = _text(row.get("evidence_id")) or _text(row.get("evidence_source_id"))
    if explicit is not None:
        return explicit
    source_id = _text(row.get("source_id"))
    if source_id is not None and source_id.startswith(
            (_TRACK_EVIDENCE_PREFIX, _MASTER_EVIDENCE_PREFIX, _FILE_EVIDENCE_PREFIX)):
        return source_id
    guid = _text(row.get("source_guid")) or source_id or str(row.get("_analysis_key") or "")
    if _is_external(row):
        digest = _text(row.get("sha256"))
        if digest is not None:
            return f"{_FILE_EVIDENCE_PREFIX}{digest.lower()}"
        key = str(row.get("_analysis_key") or guid)
        return key if key.startswith(_FILE_EVIDENCE_PREFIX) else f"{_FILE_EVIDENCE_PREFIX}{key}"
    if (row.get("is_master") is True or row.get("_project_kind") == "master"
            or str(row.get("stage") or "").startswith("master")):
        return f"{_MASTER_EVIDENCE_PREFIX}{guid}"
    return f"{_TRACK_EVIDENCE_PREFIX}{guid}"


def _source_stage(row: Mapping[str, Any], report: Mapping[str, Any] | None) -> str | None:
    stage = _text(row.get("stage"))
    if stage is not None:
        return stage
    provenance = _mapping((report or {}).get("provenance"))
    stage = _text(provenance.get("stage"))
    if stage is not None:
        return stage
    return STAGE_EXTERNAL_FILE if _is_external(row) else None


def _analysis_entries(report: Any) -> list[Mapping[str, Any]]:
    """Reports for one source: one per captured interval when the record says so."""
    if isinstance(report, Mapping):
        return [report]
    return [entry for entry in _rows(report) if isinstance(entry, Mapping)]


def _report_window(report: Mapping[str, Any]) -> list[float | None] | None:
    provenance = _mapping(report.get("provenance"))
    measured = provenance.get("measured_section_s")
    if measured is None:
        measured = _mapping(report.get("section")).get("measured_section_s")
    window = _window_pair(measured)
    if window is not None:
        return window
    return _window_pair(report.get("section"))


def _source_window(row: Mapping[str, Any]) -> list[float | None] | None:
    for key in ("signal_window", "window", "interval"):
        window = _window_pair(row.get(key))
        if window is not None:
            return window
    return None


def _fact_window(row: Mapping[str, Any], report: Mapping[str, Any],
                 section: list[float | None] | None,
                 record_window: list[float | None] | None,
                 *, report_first: bool = False
                 ) -> list[float | None] | None:
    """The captured interval: source, then report, then the capture request.

    A record that carries one report per interval already names each interval
    in that report; ``report_first`` keeps those distinct instead of collapsing
    every fact onto the source's full render window.
    """
    if report_first:
        return (_report_window(report) or _source_window(row)
                or section or record_window)
    return (_source_window(row) or _report_window(report)
            or section or record_window)


def _analysis_metrics(report: Mapping[str, Any]) -> dict[str, Any]:
    """Every measured value the report carries, copied without aggregation."""
    metrics: dict[str, Any] = {}
    for name in sorted(_mapping(report.get("metrics")), key=str):
        value = _metric_value(_mapping(report.get("metrics"))[name])
        if value is not None:
            metrics[str(name)] = value
    curves = _mapping(report.get("curves"))
    bands = _number_list(curves.get(_BAND_LEVEL_KEY))
    if bands:
        metrics[_BAND_LEVEL_KEY] = bands
    return metrics


# --------------------------------------------------------------------------- #
# public adapter
# --------------------------------------------------------------------------- #


def _route_is_master(source_id: str, master_fingerprint: Mapping[str, Any] | None) -> bool:
    """Whether a fact's source id names the master object.

    The evidence id already carries the owner kind; the master fingerprint is
    checked as a fallback for a record that supplied a bare GUID.
    """
    if source_id.startswith(_MASTER_EVIDENCE_PREFIX):
        return True
    guid = (master_fingerprint or {}).get("guid")
    return bool(guid) and source_id == guid




def _compact_map(tracks, master, routing) -> dict:
    """Structure-only map: no item rows, no parameter values."""
    def track(row):
        items = row.get("items") if isinstance(row.get("items"), list) else []
        return {
            "guid": row.get("guid"),
            "name": row.get("name"),
            "fx_names": [str(fx.get("name")) for fx in (row.get("fx") or [])
                         if isinstance(fx, Mapping)],
            "send_count": len(row.get("sends") or []) if isinstance(
                row.get("sends"), list) else row.get("send_count"),
            "item_summary": {"count": len(items)} if items else {
                "count": row.get("item_count") or 0},
            "volume_db": row.get("volume_db"),
            "pan": row.get("pan"),
            "folder_depth": row.get("folder_depth"),
        }
    master_row = None
    if isinstance(master, Mapping):
        master_row = {
            "guid": master.get("guid"),
            "name": master.get("name") or "master",
            "fx_names": [str(fx.get("name")) for fx in (master.get("fx") or [])
                         if isinstance(fx, Mapping)],
        }
    return {
        "tracks": [track(row) for row in tracks],
        "master": master_row,
        "routes": list(routing or []),
    }

def fill_state_from_capture(
    state: ProjectState,
    session: Any,
    record: Any,
    *,
    section: Any = None,
    revision: Any = None,
) -> dict[str, Any]:
    """Fill ``state`` with one capture's objects, changes and facts.

    ``state`` is the live :class:`~reaper_audio.context.project_state.ProjectState`
    document for the captured project (the caller saves it).  ``session`` must
    expose at least one of ``digest()``, ``status(detail="full")`` or
    ``snapshot(include_items=True)``; exactly one of them is read.
    ``record`` is a :class:`~reaper_audio.context.full_capture.FullCaptureRecord`
    or any mapping/object carrying ``analysis``, ``audio`` (or ``sources``),
    ``project_map`` and ``time_window``.  ``section`` is the capture request's
    ``(start_s, end_s)`` fallback window and ``revision`` the revision stored on
    the facts (the capture record's ``revision_before`` when omitted).

    Returns ``{"facts_written": n, "objects": {...}, "changes": {...},
    "state_token": int}``.  ``objects`` is the
    :meth:`ProjectState.upsert_objects` report; ``changes`` carries the object
    diff, the guids/fields that moved, the ``stale_facts`` map produced before
    the new facts, and any sources that were skipped with a reason.
    """
    view = _record_view(record)
    project_id = _text(view.get("project_id")) or state.project_id
    if project_id != state.project_id:
        raise ValidationError(
            "capture belongs to a different project",
            details={"capture": project_id, "state": state.project_id})

    tracks, master, routing = _read_session_rows(session)
    state.upsert_map(_compact_map(tracks, master, routing))
    stored_shape = _stored_shape(state)

    track_fingerprints = {
        str(row["guid"]): fingerprint_track(row)
        for row in tracks
    }
    master_fingerprint = fingerprint_master(master) if master is not None else None
    routing_fingerprint = (fingerprint_routing(routing)
                           if routing is not None else None)

    current_shape: dict[str, Any] = {
        "tracks": {
            guid: dict(fingerprint["fields"])
            for guid, fingerprint in track_fingerprints.items()
        },
        "master": (dict(master_fingerprint["fields"])
                   if master_fingerprint is not None
                   else stored_shape["master"]),
        "routing": (routing_fingerprint if routing_fingerprint is not None
                    else stored_shape["routing"]),
    }
    diff = diff_fingerprints(stored_shape, current_shape)

    object_kwargs: dict[str, Any] = {}
    if tracks:
        object_kwargs["tracks"] = tracks
    if master is not None:
        object_kwargs["master"] = master
    if routing is not None:
        object_kwargs["routing"] = routing
    if object_kwargs:
        objects = state.upsert_objects(**object_kwargs)
    else:
        objects = {
            "tracks": {"added": [], "updated": [], "unchanged": []},
            "master": {"added": False, "updated": False, "unchanged": False},
            "routing": {"added": False, "updated": False, "unchanged": False},
            "changed": {},
            "routing_changed": False,
            "token": state.state_token,
        }

    changed, routing_changed = _changed_from_diff(diff, objects)
    stale_facts: dict[str, str] = {}
    if changed or routing_changed:
        stale_facts = state.mark_stale(changed, routing_changed=routing_changed)

    record_window = _window_pair(view.get("time_window"))
    section_window = _section_window(section)
    analysis = _mapping(view.get("analysis"))
    fact_revision = revision
    if fact_revision is None:
        fact_revision = view.get("revision_before") or view.get("revision_after")
    if fact_revision is not None:
        fact_revision = _plain_json(fact_revision)
    facts_written = 0
    skipped: list[dict[str, str]] = []

    for row in _source_rows(view):
        key = str(row.get("_analysis_key") or row.get("source_id") or "")
        report = analysis.get(key)
        entries = _analysis_entries(report)
        if not entries:
            skipped.append({"source_id": _evidence_id(row),
                            "reason": "no_analysis_report"})
            continue
        stage = _source_stage(row, entries[0])
        if stage is None:
            skipped.append({"source_id": _evidence_id(row),
                            "reason": "no_recorded_stage"})
            continue

        source_id = _evidence_id(row)
        external = _is_external(row)
        if external:
            fingerprints: dict[str, Any] = {}
        else:
            source_fingerprint = (
                master_fingerprint
                if _route_is_master(source_id, master_fingerprint)
                else track_fingerprints.get(str(row.get("source_guid") or key))
            )
            fingerprints = {
                "source": source_fingerprint,
                "master": master_fingerprint,
                "routing": routing_fingerprint,
            }

        for entry in entries:
            window = _fact_window(row, entry, section_window, record_window,
                                  report_first=len(entries) > 1)
            if window is None:
                skipped.append({"source_id": source_id,
                                "reason": "no_captured_interval"})
                continue
            metrics = _analysis_metrics(entry)
            if not metrics:
                skipped.append({"source_id": source_id,
                                "reason": "no_measured_values"})
                continue
            state.upsert_fact(
                source_id=source_id,
                stage=stage,
                window=window,
                metrics=metrics,
                fingerprints=fingerprints,
                revision=fact_revision,
                status="current",
            )
            facts_written += 1

    changes = {
        "changed": bool(diff.get("changed")),
        "objects": {
            "tracks": dict(diff.get("tracks") or {}),
            "master": dict(diff.get("master") or {}),
            "routing": dict(diff.get("routing") or {}),
            "added_tracks": list(diff.get("added_tracks") or []),
            "removed_tracks": list(diff.get("removed_tracks") or []),
            "unchanged_tracks": list(diff.get("unchanged_tracks") or []),
            "master_guid": diff.get("master_guid"),
        },
        "tracks": dict(diff.get("tracks") or {}),
        "master": dict(diff.get("master") or {}),
        "routing": dict(diff.get("routing") or {}),
        "added_tracks": list(diff.get("added_tracks") or []),
        "removed_tracks": list(diff.get("removed_tracks") or []),
        "unchanged_tracks": list(diff.get("unchanged_tracks") or []),
        "changed_objects": {guid: list(fields) for guid, fields in sorted(changed.items())},
        "routing_changed": routing_changed,
        "stale_facts": dict(sorted(stale_facts.items())),
        "skipped": sorted(skipped, key=lambda entry: (entry["source_id"],
                                                      entry["reason"])),
    }
    return {
        "facts_written": facts_written,
        "objects": objects,
        "changes": changes,
        "state_token": state.state_token,
    }
