"""One mutable project state per project, stored in the archive.

The library used to keep immutable capture snapshots, an evidence store and
separate freshness/coverage stores.  This module is the single place where the
*current* state of one project lives: objects (tracks, master, routing), what
was measured on them (facts), and which measurements a later project change
invalidated.  Captures fill it; measurements upsert into it; fingerprints decide
what went stale when the owner changes FX, a fader or automation behind the
agent's back.

Layout and durability
---------------------

::

    <archive>/projects/<safe_project_key>/state.json

``<safe_project_key>`` is a readable ASCII slug of the project id plus a short
SHA-256 suffix, so two project ids that sanitise alike still get separate
files.  Writes are atomic: a temporary file in the same directory is written,
flushed, ``fsync``-ed and moved over ``state.json`` with ``os.replace``.  A
reader therefore sees either the previous complete document or the next
complete document, never a torn one.  An unreadable or foreign state file is
refused instead of being silently replaced: losing state quietly is worse than
a loud validation error.

Single-writer semantics (lock-free)
-----------------------------------

There is deliberately no file lock.  One process/session owns one project's
state file at a time; the archive is not shared between two live writers for
the same project.  ``os.replace`` keeps every individual write atomic, but two
concurrent writers can still lose one another's updates (last writer wins).
That is acceptable because state is derived from the project, not authored by
the user: the next project read re-establishes objects and a re-measure
re-establishes facts.  A future multi-process writer must add locking *around
load/save*, not inside this module.

Schema (``reaper-workbench.project-state/1``, frozen)
-----------------------------------------------------

::

    {
      "schema": "reaper-workbench.project-state/1",
      "project_id": "reaper:{GUID}:/songs/song.rpp",
      "token": 17,
      "map": null,
      "objects": {
        "tracks": {
          "<track guid>": {"guid": "...", "fingerprint": {...}}
        },
        "master": {"guid": "...", "fingerprint": {...}} | null,
        "routing": {"fingerprint": {...}} | null
      },
      "facts": {
        "<fact id>": {
          "id": "...", "source_id": "...", "stage": "...",
          "window": [start_s, end_s],
          "key": {"source_id": "...", "stage": "...",
                  "window": [start_s, end_s],
                  "metrics": ["sorted", "metric", "names"]},
          "key_sha256": "...", "metrics": {"name": value},
          "fingerprints": {"source": {...}|null, "master": {...}|null,
                           "routing": {...}|null},
          "revision": ...|null, "measured_at": "...|null",
          "status": "current" | "stale" | "superseded",
          "stale_reason": "...|null",
          "overlaps": [key, ...], "supersedes": ["..."],
          "superseded_by": "..."|null
        }
      },
      "history": [{"seq": n, "kind": "fact_superseded", ...}],
      "changes": [{"seq": n, "kind": "objects"|"fact"|"stale", ...}]
    }

Invariants
----------

* ``token`` is a monotonically increasing integer.  Every change to objects or
  facts increments it and appends exactly one entry to ``changes`` with that
  sequence number.  ``changes_since(token)`` returns the entries after that
  token.  Timestamps are never part of the token, so re-saving an unchanged
  document does not look like a change.
* A fact is identified by ``{source_id, stage, window, metrics}`` where
  ``metrics`` is the *sorted list of measured metric names*, not their values.
  Upserting the same identity supersedes the previous record (its status becomes
  ``"superseded"`` and a compact entry is appended to ``history``); the previous
  record itself is retained, never deleted.
* A fact whose window partially overlaps an existing fact of the same source and
  stage is stored too and gets ``overlaps`` naming the other keys.  Analysis is
  never discarded because two windows disagree; both results stay queryable.
* Facts are never rewritten by a stale check: only ``status`` and
  ``stale_reason`` move to ``"stale"``.  A fresh measurement of the same identity
  supersedes the stale record.
* Facts are not mutated by object upserts.  ``upsert_objects`` reports which
  fingerprint fields moved; the caller decides whether that invalidates facts by
  calling :meth:`ProjectState.mark_stale`.  Keeping the two steps apart lets a
  capture fill objects without declaring the measurements it is about to take
  stale.

Dependencies between a fact and project objects
-----------------------------------------------

``mark_stale`` follows the frozen rules:

* a *via-master* fact (stage name carries ``via_master``) depends on its source
  object, the master object and routing;
* a *master-stage* fact (stage is ``master`` or starts with ``master_``)
  depends on the master object only;
* every other stage is a source-stage fact and depends on its source object
  only.  The stage vocabulary comes from the capture/measurement layers
  (``via_master``, ``master``, ``master_mix_post_fx``,
  ``selected_track_via_master_parallel_batch_post_fx``, ...); unknown names
  degrade to the smallest honest claim instead of guessing master involvement.

Fingerprint rules
-----------------

Fingerprints use only fields that the status projection
(:mod:`reaper_audio.context.status`) or the bridge digest actually carries:
FX name/enabled/offline/slot order and a hash of normalised parameter values
when parameters were read, fader volume and/or dB, pan, mute, solo, sends
(destination, volume, pan, mode), envelopes (name, point count, points hash),
an item summary (count, start/end, take labels, active take ids) and channel
count.  A field that the supplied row does not carry is recorded as ``null``,
never derived from a different read or invented.  Because empty and absent are
different facts, feed fingerprints from a consistent row shape: a status
``summary`` row cannot prove routing, automation or parameters, so it
fingerprints to ``null`` there and must not be compared with a ``full`` row or
a digest row for the same object.

Nested structures (FX rows, sends, envelopes, items, routing edges) are hashed
as canonical JSON (sorted keys, compact separators, ASCII escaping) with
SHA-256, so equal content always hashes equally regardless of key order.

Nothing in this module reads the clock, talks to REAPER or knows a project
name, threshold or track count.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..errors import ValidationError

__all__ = [
    "FACT_STATUSES",
    "FINGERPRINT_FIELDS",
    "MASTER_STAGE_PREFIX",
    "SCHEMA",
    "STATUS_CURRENT",
    "STATUS_STALE",
    "STATUS_SUPERSEDED",
    "VIA_MASTER_MARKER",
    "ProjectState",
    "StateStore",
    "diff_fingerprints",
    "empty_state",
    "fact_key",
    "fingerprint_master",
    "fingerprint_routing",
    "fingerprint_track",
    "safe_project_key",
]

#: Schema id of the state document.  Frozen: a different shape needs a new id.
SCHEMA = "reaper-workbench.project-state/1"

#: Archive sub-directory holding one state file per project.
PROJECTS_DIRNAME = "projects"

#: File name inside a project's state directory.
STATE_FILENAME = "state.json"

STATUS_CURRENT = "current"
STATUS_STALE = "stale"
STATUS_SUPERSEDED = "superseded"

#: Every status a fact record may carry.  ``current`` is measured and trusted,
#: ``stale`` was measured but a dependency changed since, ``superseded`` was
#: replaced by a newer measurement of the same identity.
FACT_STATUSES: tuple[str, ...] = (STATUS_CURRENT, STATUS_STALE, STATUS_SUPERSEDED)

#: Marker inside a stage name that means "measured through the master bus".
VIA_MASTER_MARKER = "via_master"

#: Prefix of a master-bus stage name (``master``, ``master_post_fx``, ...).
MASTER_STAGE_PREFIX = "master"

#: Fingerprint field names, in a stable reporting order.  This is the schema
#: of ``fingerprint_*`` field maps, not a project-specific metric list.
FINGERPRINT_FIELDS: tuple[str, ...] = (
    "channel_count",
    "envelopes",
    "fx",
    "items",
    "mute",
    "pan",
    "sends",
    "solo",
    "volume",
    "volume_db",
)

_FINGERPRINT_KINDS = ("track", "master", "routing")
_FACT_FINGERPRINT_NAMES = ("source", "master", "routing")

_SAFE_SLUG_LIMIT = 40
_SAFE_KEY_HASH_CHARS = 16
_TAKE_PATH_SPLIT = re.compile(r"[\\/]+")
_UNSAFE_SLUG_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


# --------------------------------------------------------------------------- #
# small value normalisers
# --------------------------------------------------------------------------- #


def _finite(value: object) -> float | None:
    """Finite float or ``None``; booleans and non-numbers are not numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _numeric(value: object) -> int | float | None:
    """JSON number without coercing a boolean into ``0``/``1``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return _finite(value)


def _flag(value: object) -> bool | None:
    """Boolean when the value says one; ``None`` when it cannot be read."""
    if isinstance(value, bool):
        return value
    number = _finite(value)
    return bool(number) if number is not None else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _non_empty_text(value: object) -> str | None:
    text = _text(value)
    return text if text else None


def _optional_number(value: object, field: str) -> float | None:
    """``None`` stays ``None``; anything else must decode to a finite number."""
    if value is None:
        return None
    number = _finite(value)
    if number is None:
        raise ValidationError(f"{field} must be a finite number or null",
                              details={"field": field, "value": value})
    return number


def _canonical(value: object) -> str:
    """Canonical JSON used for every nested hash in this module."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:  # pragma: no cover - guarded inputs
        raise ValidationError(
            "value is not canonically encodable",
            details={"reason": str(exc)},
        ) from exc


def _digest(value: object) -> str:
    """``sha256:<hex>`` over :func:`_canonical`."""
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _clone(value: object) -> Any:
    """Deep copy plain JSON-shaped data so callers cannot mutate the state."""
    return copy.deepcopy(value)


# --------------------------------------------------------------------------- #
# fingerprints
# --------------------------------------------------------------------------- #


def _fx_entries(row: Mapping[str, Any]) -> list[dict[str, Any] | None] | None:
    """FX slots in chain order, or ``None`` when the row carries no FX rows.

    A status ``summary`` row carries only ``fx_names``: names and order are
    real fields, so they are fingerprinted while enabled/offline/parameters stay
    ``null``.  Parameter values are hashed by slot index; ``parameter_count`` is
    kept separately because a truncated parameter read must not look like a
    different chain.
    """
    fx = row.get("fx")
    if isinstance(fx, list):
        entries: list[dict[str, Any] | None] = []
        for position, item in enumerate(fx):
            if not isinstance(item, Mapping):
                entries.append(None)
                continue
            order = _integer(item.get("index"))
            if order is None:
                order = position
            parameters = item.get("parameters")
            if not isinstance(parameters, list):
                parameters = item.get("params")
            parameter_hash: str | None = None
            parameter_count = _integer(item.get("parameter_count"))
            if isinstance(parameters, list):
                values: list[dict[str, Any]] = []
                for parameter_position, parameter in enumerate(parameters):
                    if not isinstance(parameter, Mapping):
                        continue
                    index = _integer(parameter.get("index"))
                    values.append({
                        "index": index if index is not None else parameter_position,
                        "normalized": _finite(parameter.get("normalized")),
                    })
                values.sort(key=lambda entry: (entry["index"], _canonical(entry)))
                parameter_hash = _digest(values)
                if parameter_count is None:
                    parameter_count = len(values)
            entries.append({
                "order": order,
                "name": _text(item.get("name")),
                "enabled": _flag(item.get("enabled")),
                "offline": _flag(item.get("offline")),
                "parameter_count": parameter_count,
                "parameters_sha256": parameter_hash,
            })
        return entries
    names = row.get("fx_names")
    if isinstance(names, list):
        return [{
            "order": position,
            "name": _text(name),
            "enabled": None,
            "offline": None,
            "parameter_count": None,
            "parameters_sha256": None,
        } for position, name in enumerate(names)]
    return None


def _send_entries(row: Mapping[str, Any]) -> list[dict[str, Any] | None] | None:
    """Send edges owned by one row, or ``None`` when send rows are absent."""
    sends = row.get("sends")
    if not isinstance(sends, list):
        return None
    entries: list[dict[str, Any] | None] = []
    for position, send in enumerate(sends):
        if not isinstance(send, Mapping):
            entries.append(None)
            continue
        order = _integer(send.get("index"))
        if order is None:
            order = position
        entries.append({
            "order": order,
            "destination": _non_empty_text(send.get("dest_guid")),
            "volume": _finite(send.get("volume")),
            "pan": _finite(send.get("pan")),
            "mode": _numeric(send.get("mode")),
        })
    return entries


def _envelope_entries(row: Mapping[str, Any]) -> list[dict[str, Any] | None] | None:
    """Envelope name/point summary, sorted so enumeration order is irrelevant."""
    envelopes = row.get("envelopes")
    if not isinstance(envelopes, list):
        return None
    entries: list[dict[str, Any] | None] = []
    for envelope in envelopes:
        if not isinstance(envelope, Mapping):
            entries.append(None)
            continue
        points = envelope.get("points")
        points_hash: str | None = None
        if isinstance(points, list):
            normalized: list[dict[str, Any] | None] = []
            for point in points:
                if not isinstance(point, Mapping):
                    normalized.append(None)
                    continue
                normalized.append({
                    "time_s": _finite(point.get("time_s")),
                    "value": _finite(point.get("value")),
                    "shape": _numeric(point.get("shape")),
                    "tension": _finite(point.get("tension")),
                })
            points_hash = _digest(normalized)
        point_count = _integer(envelope.get("point_count"))
        if point_count is None and isinstance(points, list):
            point_count = len(points)
        entries.append({
            "name": _text(envelope.get("name")),
            "point_count": point_count,
            "points_sha256": points_hash,
        })
    entries.sort(key=_canonical)
    return entries


def _take_label(value: object) -> str | None:
    """Last path segment of a take source; a name, never a full path."""
    text = _non_empty_text(value)
    if text is None:
        return None
    segments = [segment for segment in _TAKE_PATH_SPLIT.split(text) if segment]
    return segments[-1] if segments else None


def _item_summary(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """Count, placement extent, take labels and active-take ids.

    ``items`` rows are preferred; a bare ``item_count`` records the count and
    leaves the unread parts ``null``.  ``takes``/``active_takes`` are empty when
    item rows were read but carried no take identity, which is different from
    ``null`` (item rows were never supplied).
    """
    items = row.get("items")
    if isinstance(items, list):
        starts: list[float] = []
        ends: list[float] = []
        takes: list[str] = []
        active: list[str] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            position = _finite(item.get("position_s"))
            if position is None:
                position = _finite(item.get("start_s"))
            length = _finite(item.get("length_s"))
            if position is not None:
                starts.append(position)
                if length is not None:
                    ends.append(position + length)
            take = item.get("active_take")
            take = take if isinstance(take, Mapping) else {}
            label = _take_label(take.get("source"))
            if label is None:
                label = _take_label(item.get("source_path"))
            if label is None:
                label = _take_label(item.get("source"))
            if label is not None:
                takes.append(label)
            take_guid = _non_empty_text(take.get("guid"))
            if take_guid is None:
                take_guid = _non_empty_text(item.get("take_guid"))
            if take_guid is not None:
                active.append(take_guid)
        return {
            "count": len(items),
            "start_s": min(starts) if starts else None,
            "end_s": max(ends) if ends else None,
            "takes": sorted(takes),
            "active_takes": sorted(active),
        }
    item_count = _integer(row.get("item_count"))
    if item_count is not None:
        return {
            "count": item_count,
            "start_s": None,
            "end_s": None,
            "takes": None,
            "active_takes": None,
        }
    return None


def _row_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    """The fingerprinted fields of one track/master row, missing ones null."""
    return {
        "channel_count": _integer(row.get("channel_count")),
        "envelopes": _envelope_entries(row),
        "fx": _fx_entries(row),
        "items": _item_summary(row),
        "mute": _flag(row.get("mute")),
        "pan": _finite(row.get("pan")),
        "sends": _send_entries(row),
        "solo": _numeric(row.get("solo")),
        "volume": _finite(row.get("volume")),
        "volume_db": _finite(row.get("volume_db")),
    }


def _fingerprint(kind: str, guid: str | None, fields: Mapping[str, Any]) -> dict[str, Any]:
    """Assemble a fingerprint record; the hash covers kind + fields only."""
    return {
        "kind": kind,
        "guid": guid or None,
        "fields": dict(fields),
        "sha256": _digest({"kind": kind, "fields": dict(fields)}),
    }


def fingerprint_track(track_row: Mapping[str, Any]) -> dict[str, Any]:
    """Fingerprint one track row from a status or digest read.

    Every field in :data:`FINGERPRINT_FIELDS` is present in the result; a field
    the row does not carry is ``null``.  The caller should fingerprint the
    richest row it has (digest track row or status ``detail="full"``) and keep
    using the same shape: a summary row cannot prove sends, automation, items or
    FX bypass, so its hash deliberately differs from a full row's.
    """
    if not isinstance(track_row, Mapping):
        raise ValidationError("track_row must be a mapping",
                              details={"type": type(track_row).__name__})
    guid = _non_empty_text(track_row.get("guid"))
    return _fingerprint("track", guid, _row_fields(track_row))


def fingerprint_master(master_row: Mapping[str, Any]) -> dict[str, Any]:
    """Fingerprint the master row; same field set as a track minus items."""
    if not isinstance(master_row, Mapping):
        raise ValidationError("master_row must be a mapping",
                              details={"type": type(master_row).__name__})
    return _fingerprint("master", _non_empty_text(master_row.get("guid")),
                        _row_fields(master_row))


def _routing_edges(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Flatten send rows *or* owner rows carrying ``sends`` into edges."""
    edges: list[dict[str, Any]] = []
    for position, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        if "source_guid" in row or "dest_guid" in row:
            order = _integer(row.get("index"))
            edges.append({
                "source": _non_empty_text(row.get("source_guid")),
                "destination": _non_empty_text(row.get("dest_guid")),
                "order": order if order is not None else position,
                "volume": _finite(row.get("volume")),
                "pan": _finite(row.get("pan")),
                "mode": _numeric(row.get("mode")),
            })
            continue
        sends = row.get("sends")
        if not isinstance(sends, list):
            continue
        source = _non_empty_text(row.get("guid"))
        for send_position, send in enumerate(sends):
            if not isinstance(send, Mapping):
                continue
            order = _integer(send.get("index"))
            edges.append({
                "source": source,
                "destination": _non_empty_text(send.get("dest_guid")),
                "order": order if order is not None else send_position,
                "volume": _finite(send.get("volume")),
                "pan": _finite(send.get("pan")),
                "mode": _numeric(send.get("mode")),
            })
    edges.sort(key=_canonical)
    return edges


def fingerprint_routing(rows: object) -> dict[str, Any]:
    """Fingerprint the routing graph from send rows or per-owner rows.

    Each row may be a flat send row (``source_guid``/``dest_guid``, as produced
    by the status send section) or an owner row carrying a ``sends`` list (as in
    a digest track row).  Owner rows without a ``sends`` list contribute
    nothing, so routing must be fingerprinted from full/digest rows; a summary
    row's ``send_count`` is a count, not evidence of an unchanged route.
    """
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Iterable):
        raise ValidationError("routing rows must be an iterable of mappings")
    edges = _routing_edges(rows)
    return {
        "kind": "routing",
        "guid": None,
        "fields": {"edges": edges},
        "sha256": _digest({"kind": "routing", "fields": {"edges": edges}}),
    }


def _fields_of(fingerprint: object) -> Mapping[str, Any]:
    if isinstance(fingerprint, Mapping):
        fields = fingerprint.get("fields")
        if isinstance(fields, Mapping):
            return fields
        return fingerprint
    return {}


def diff_fingerprints(previous: object, current: object) -> list[str]:
    """Field names whose fingerprinted value differs, in reporting order.

    Both arguments may be fingerprint records or bare field maps.  ``fx`` covers
    every FX attribute (including parameter values), so a bypass toggle and a
    parameter move both report ``"fx"``.
    """
    old = _fields_of(previous)
    new = _fields_of(current)
    names = [name for name in FINGERPRINT_FIELDS if name in old or name in new]
    names.extend(sorted((set(old) | set(new)) - set(FINGERPRINT_FIELDS)))
    return [name for name in names if old.get(name) != new.get(name)]


# --------------------------------------------------------------------------- #
# project keys and the empty document
# --------------------------------------------------------------------------- #


def safe_project_key(project_id: str) -> str:
    """Deterministic, path-safe directory name for one project id.

    The readable slug keeps state directories inspectable; the hash suffix
    prevents two ids that sanitise to the same slug (or a case-only difference)
    from sharing a state file.
    """
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValidationError("project_id must be a non-empty string",
                              details={"project_id": project_id})
    text = project_id.strip()
    slug = _UNSAFE_SLUG_CHARS.sub("-", text).strip("-._").lower()
    slug = slug[:_SAFE_SLUG_LIMIT].strip("-._")
    suffix = hashlib.sha256(text.encode("utf-8")).hexdigest()[:_SAFE_KEY_HASH_CHARS]
    return f"{slug}-{suffix}" if slug else suffix


def empty_state(project_id: str) -> dict[str, Any]:
    """The document used for a project that has never been saved."""
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValidationError("project_id must be a non-empty string",
                              details={"project_id": project_id})
    return {
        "schema": SCHEMA,
        "project_id": project_id,
        "token": 0,
        "objects": {"tracks": {}, "master": None, "routing": None},
        "facts": {},
        "history": [],
        "changes": [],
    }


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #


def _require_document(state: object, *, project_id: str | None = None) -> dict[str, Any]:
    """Validate the frozen envelope; refuse rather than repair."""
    if not isinstance(state, Mapping):
        raise ValidationError("project state must be a mapping",
                              details={"type": type(state).__name__})
    document = dict(state)
    if document.get("schema") != SCHEMA:
        raise ValidationError(
            "unsupported project state schema",
            details={"schema": document.get("schema"), "expected": SCHEMA},
        )
    stored_id = document.get("project_id")
    if not isinstance(stored_id, str) or not stored_id:
        raise ValidationError("project state has no project_id")
    if project_id is not None and stored_id != project_id:
        raise ValidationError(
            "project state belongs to a different project",
            details={"expected": project_id, "stored": stored_id},
        )
    token = document.get("token")
    if isinstance(token, bool) or not isinstance(token, int) or token < 0:
        raise ValidationError("project state token must be a non-negative integer",
                              details={"token": token})
    objects = document.get("objects")
    if not isinstance(objects, Mapping):
        raise ValidationError("project state objects must be a mapping")
    for key in ("tracks", "master", "routing"):
        if key not in objects:
            raise ValidationError(f"project state objects.{key} is missing")
    if not isinstance(objects.get("tracks"), Mapping):
        raise ValidationError("project state objects.tracks must be a mapping")
    if not isinstance(document.get("facts"), Mapping):
        raise ValidationError("project state facts must be a mapping")
    for key in ("history", "changes"):
        if not isinstance(document.get(key), list):
            raise ValidationError(f"project state {key} must be a list")
    return document


class StateStore:
    """Path layout plus atomic read/write of one state document per project.

    ``load`` never creates a file and never repairs a bad one: an absent file
    yields :func:`empty_state`, while an unreadable, foreign or wrong-schema
    file raises ``ValidationError`` so no caller silently starts from zero.
    ``save`` is the only writer and is atomic.  See the module docstring for the
    lock-free single-writer rule.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        if not isinstance(root, (str, os.PathLike)):
            raise ValidationError("archive root must be a path")
        self.root = Path(root).expanduser().absolute()
        self.projects_root = self.root / PROJECTS_DIRNAME

    def path_for(self, project_id: str) -> Path:
        """``<root>/projects/<safe_project_key>/state.json``."""
        return self.projects_root / safe_project_key(project_id) / STATE_FILENAME

    def load(self, project_id: str) -> dict[str, Any]:
        """Read one project's document, or a fresh skeleton when absent."""
        path = self.path_for(project_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return empty_state(project_id)
        except OSError as exc:
            raise ValidationError(
                "project state file is unreadable",
                details={"path": str(path), "reason": str(exc)},
            ) from exc
        except UnicodeError as exc:
            raise ValidationError(
                "project state file is not UTF-8 text",
                details={"path": str(path), "reason": str(exc)},
            ) from exc
        try:
            decoded = json.loads(raw)
        except ValueError as exc:
            raise ValidationError(
                "project state file is not valid JSON",
                details={"path": str(path), "reason": str(exc)},
            ) from exc
        return _require_document(decoded, project_id=project_id)

    def save(self, state: Mapping[str, Any]) -> Path:
        """Validate and atomically replace one project's state file."""
        document = _require_document(state)
        path = self.path_for(document["project_id"])
        try:
            payload = json.dumps(document, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "project state is not JSON-encodable",
                details={"reason": str(exc)},
            ) from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent),
            prefix=f".{path.stem}.", suffix=".tmp", delete=False,
        )
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, path)
        finally:
            try:
                if os.path.exists(handle.name):
                    os.unlink(handle.name)
            except OSError:  # pragma: no cover - best-effort cleanup
                pass
        return path


# --------------------------------------------------------------------------- #
# facts
# --------------------------------------------------------------------------- #


def _normalise_window(window: object) -> list[float | None]:
    """``[start_s, end_s]`` as floats (or nulls) from a mapping or a pair."""
    if isinstance(window, Mapping):
        start_raw = window.get("start_s", window.get("start"))
        end_raw = window.get("end_s", window.get("end"))
    elif (isinstance(window, Sequence) and not isinstance(window, (str, bytes))
          and len(window) == 2):
        start_raw, end_raw = window[0], window[1]
    else:
        raise ValidationError(
            "window must be {start_s, end_s} or a two-item sequence",
            details={"window": window},
        )
    start = _optional_number(start_raw, "window.start_s")
    end = _optional_number(end_raw, "window.end_s")
    if start is not None and end is not None and start > end:
        raise ValidationError("window start_s must not exceed end_s",
                              details={"window": [start, end]})
    return [start, end]


def fact_key(source_id: str, stage: str, window: object,
             metrics: Mapping[str, Any]) -> dict[str, Any]:
    """The identity of one fact: source, stage, window, sorted metric names."""
    if not isinstance(source_id, str) or not source_id:
        raise ValidationError("source_id must be a non-empty string")
    if not isinstance(stage, str) or not stage:
        raise ValidationError("stage must be a non-empty string")
    if not isinstance(metrics, Mapping):
        raise ValidationError("metrics must be a mapping",
                              details={"type": type(metrics).__name__})
    names: list[str] = []
    for name in metrics:
        if not isinstance(name, str) or not name:
            raise ValidationError("metric names must be non-empty strings",
                                  details={"name": name})
        names.append(name)
    return {
        "source_id": source_id,
        "stage": stage,
        "window": _normalise_window(window),
        "metrics": sorted(names),
    }


def _key_hash(key: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(dict(key)).encode("utf-8")).hexdigest()


def _stage_dependency(stage: str) -> str:
    """``via_master``, ``master`` or ``source`` — the frozen dependency rule."""
    if VIA_MASTER_MARKER in stage:
        return "via_master"
    if stage == MASTER_STAGE_PREFIX or stage.startswith(f"{MASTER_STAGE_PREFIX}_"):
        return "master"
    return "source"


def _windows_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """True when two windows partly intersect (same window is not partial)."""
    if list(left.get("window") or []) == list(right.get("window") or []):
        return False
    left_start, left_end = (left.get("window") or [None, None])[:2]
    right_start, right_end = (right.get("window") or [None, None])[:2]
    left_low = float("-inf") if left_start is None else float(left_start)
    left_high = float("inf") if left_end is None else float(left_end)
    right_low = float("-inf") if right_start is None else float(right_start)
    right_high = float("inf") if right_end is None else float(right_end)
    return left_low < right_high and left_high > right_low


def _normalise_fingerprints(fingerprints: object) -> dict[str, Any]:
    """Exactly ``source``/``master``/``routing``; missing values are null."""
    if fingerprints is None:
        return {name: None for name in _FACT_FINGERPRINT_NAMES}
    if not isinstance(fingerprints, Mapping):
        raise ValidationError("fingerprints must be a mapping")
    unknown = set(fingerprints) - set(_FACT_FINGERPRINT_NAMES)
    if unknown:
        raise ValidationError(
            "unknown fact fingerprint name",
            details={"unknown": sorted(str(name) for name in unknown),
                     "allowed": list(_FACT_FINGERPRINT_NAMES)},
        )
    result: dict[str, Any] = {}
    for name in _FACT_FINGERPRINT_NAMES:
        value = fingerprints.get(name)
        if value is None:
            result[name] = None
        elif isinstance(value, Mapping):
            result[name] = _clone(dict(value))
        else:
            raise ValidationError(
                f"fingerprints.{name} must be a mapping or null",
                details={"name": name, "type": type(value).__name__},
            )
    return result


def _source_fingerprint_guid(fact: Mapping[str, Any]) -> str | None:
    fingerprint = (fact.get("fingerprints") or {}).get("source")
    if isinstance(fingerprint, Mapping):
        return _non_empty_text(fingerprint.get("guid"))
    return None


def _guid_from_source_id(source_id: str) -> str | None:
    """``track:{GUID}`` / ``master:{GUID}`` -> ``{GUID}``, else ``None``.

    The library's source ids prefix a REAPER GUID with an owner kind; using that
    convention is a fallback only, never a replacement for the fingerprint.
    """
    _, separator, remainder = source_id.partition(":")
    if not separator:
        return None
    if len(remainder) > 2 and remainder.startswith("{") and remainder.endswith("}"):
        return remainder
    return None


class ProjectState:
    """Mutable, in-memory view of one project's state document.

    ``ProjectState(store, project_id)`` loads the document (or starts from
    :func:`empty_state`).  Every mutating helper updates the in-memory document
    and the token/change log; persistence happens only when :meth:`save` is
    called, so a caller can build a capture's objects and facts as one unit.
    """

    def __init__(self, store: StateStore, project_id: str) -> None:
        if not isinstance(store, StateStore):
            raise ValidationError("store must be a StateStore")
        self._store = store
        self._project_id = project_id
        self._state = store.load(project_id)

    # -- identity ---------------------------------------------------------- #

    @property
    def project_id(self) -> str:
        return str(self._state["project_id"])

    @property
    def state(self) -> dict[str, Any]:
        """The live document.  Mutating it by hand bypasses token accounting."""
        return self._state

    @property
    def state_token(self) -> int:
        """Monotonic change token; stable across save/load."""
        return int(self._state["token"])

    @property
    def objects(self) -> Mapping[str, Any]:
        return self._state["objects"]

    def compact(self) -> dict[str, Any]:
        """Drop values that no reader needs: superseded facts keep identity only.

        A superseded fact is history, not a value the agent may quote; its
        metrics and fingerprints are the bulk of the document.  Status, key,
        revision, timestamps and who replaced it stay, so the lineage is intact.
        """
        removed = 0
        for fact in self._state.get("facts", {}).values():
            if fact.get("status") != STATUS_SUPERSEDED:
                continue
            if fact.get("metrics"):
                fact["metrics"] = {}
                removed += 1
            fact["fingerprints"] = {}
            fact["compacted"] = True
        return {"compacted": removed}

    def save(self) -> Path:
        self.compact()
        return self._store.save(self._state)

    def upsert_map(self, mapping: Mapping[str, Any]) -> dict[str, Any]:
        """Store the compact project map that agent context is built from.

        The map is structure, not evidence: tracks/master/routes/tempo/markers
        with item and FX summaries, never exact item/take rows or parameter
        values.  Replacing it bumps the state token so context deltas notice.
        """
        if not isinstance(mapping, Mapping):
            raise ValidationError("project map must be a mapping")
        if self._state.get("map") == dict(mapping):
            return {"token": int(self._state["token"]), "changed": False,
                    "sections": list(mapping)}
        self._state["map"] = dict(mapping)
        token = self._begin_change()
        self._append_change({"seq": token, "kind": "map", "fields": list(mapping)})
        return {"token": token, "sections": list(mapping)}

    # -- token/change plumbing --------------------------------------------- #

    def _begin_change(self) -> int:
        token = int(self._state["token"]) + 1
        self._state["token"] = token
        return token

    def _append_change(self, entry: dict[str, Any]) -> None:
        self._state["changes"].append(entry)

    # -- objects ------------------------------------------------------------ #

    def upsert_objects(
        self,
        tracks: object = None,
        master: Mapping[str, Any] | None = None,
        routing: object = None,
    ) -> dict[str, Any]:
        """Fingerprint and store the current tracks, master and routing.

        ``tracks`` is a sequence of track rows or a ``{guid: row}`` mapping;
        rows must carry a non-empty ``guid``.  Only supplied objects are
        touched: a partial read cannot erase the rest of the state, and object
        removal is deliberately outside schema v1 (the integration stream can
        report a removal through :meth:`mark_stale`).

        Returns ``{"tracks": {"added", "updated", "unchanged"},
        "master": {...}, "routing": {"added", "updated", "unchanged"},
        "changed": {guid: [fingerprint fields]}, "routing_changed": bool,
        "token": n}``.  ``changed`` contains only objects whose fingerprinted
        fields moved; a new object is reported as added, not changed.
        """
        if tracks is None and master is None and routing is None:
            raise ValidationError("upsert_objects needs tracks, master or routing")
        objects = self._state["objects"]
        changed: dict[str, list[str]] = {}
        added_tracks: list[str] = []
        updated_tracks: list[str] = []
        unchanged_tracks: list[str] = []
        master_result = {"added": False, "updated": False, "unchanged": False}
        routing_result = {"added": False, "updated": False, "unchanged": False}
        routing_changed = False
        touched = False

        if tracks is not None:
            rows = self._track_rows(tracks)
            touched = touched or bool(rows)
            for guid in sorted(rows):
                fingerprint = fingerprint_track(rows[guid])
                previous = objects["tracks"].get(guid)
                if previous is None:
                    added_tracks.append(guid)
                    objects["tracks"][guid] = {"guid": guid,
                                               "fingerprint": fingerprint}
                    continue
                fields = diff_fingerprints(previous.get("fingerprint"), fingerprint)
                objects["tracks"][guid] = {"guid": guid, "fingerprint": fingerprint}
                if fields:
                    updated_tracks.append(guid)
                    changed[guid] = fields
                else:
                    unchanged_tracks.append(guid)

        if master is not None:
            touched = True
            fingerprint = fingerprint_master(master)
            previous = objects.get("master")
            if previous is None:
                master_result["added"] = True
                objects["master"] = {"guid": fingerprint["guid"],
                                     "fingerprint": fingerprint}
            else:
                fields = diff_fingerprints(previous.get("fingerprint"), fingerprint)
                if previous.get("guid") != fingerprint["guid"] and "guid" not in fields:
                    fields.insert(0, "guid")
                objects["master"] = {"guid": fingerprint["guid"],
                                     "fingerprint": fingerprint}
                if fields:
                    master_result["updated"] = True
                    changed["master"] = fields
                else:
                    master_result["unchanged"] = True

        if routing is not None:
            touched = True
            fingerprint = fingerprint_routing(routing)
            previous = objects.get("routing")
            if previous is None:
                routing_result["added"] = True
                objects["routing"] = {"fingerprint": fingerprint}
            else:
                previous_sha = (previous.get("fingerprint") or {}).get("sha256")
                objects["routing"] = {"fingerprint": fingerprint}
                if previous_sha != fingerprint["sha256"]:
                    routing_result["updated"] = True
                    routing_changed = True
                else:
                    routing_result["unchanged"] = True

        token = self.state_token
        if touched and (added_tracks or updated_tracks or routing_result["added"]
                        or routing_result["updated"]
                        or master_result["added"] or master_result["updated"]):
            token = self._begin_change()
            self._append_change({
                "seq": token,
                "kind": "objects",
                "tracks_added": added_tracks,
                "tracks_updated": updated_tracks,
                "changed": _clone(changed),
                "routing_changed": routing_changed,
            })
        return {
            "tracks": {"added": added_tracks, "updated": updated_tracks,
                       "unchanged": unchanged_tracks},
            "master": master_result,
            "routing": routing_result,
            "changed": changed,
            "routing_changed": routing_changed,
            "token": token,
        }

    @staticmethod
    def _track_rows(tracks: object) -> dict[str, Mapping[str, Any]]:
        rows: dict[str, Mapping[str, Any]] = {}
        if isinstance(tracks, Mapping):
            for key, row in tracks.items():
                if not isinstance(row, Mapping):
                    raise ValidationError("track rows must be mappings",
                                          details={"guid": key})
                item = dict(row)
                item.setdefault("guid", key)
                guid = _non_empty_text(item.get("guid"))
                if guid is None:
                    raise ValidationError("track row has no guid",
                                          details={"guid": key})
                rows[guid] = item
            return rows
        if isinstance(tracks, (str, bytes)) or not isinstance(tracks, Iterable):
            raise ValidationError("tracks must be an iterable of mappings")
        for row in tracks:
            if not isinstance(row, Mapping):
                raise ValidationError("track rows must be mappings")
            guid = _non_empty_text(row.get("guid"))
            if guid is None:
                raise ValidationError("track row has no guid")
            rows[guid] = row
        return rows

    # -- facts -------------------------------------------------------------- #

    def upsert_fact(
        self,
        *,
        source_id: str,
        stage: str,
        window: object,
        metrics: Mapping[str, Any],
        fingerprints: Mapping[str, Any] | None = None,
        revision: object = None,
        measured_at: str | None = None,
        status: str = STATUS_CURRENT,
    ) -> dict[str, Any]:
        """Insert one measurement fact, superseding the same identity.

        The identity is ``{source_id, stage, window, metrics}`` with ``metrics``
        being the sorted metric *names*; a previous non-superseded fact with the
        same identity keeps its record but becomes ``superseded`` (with
        ``superseded_by``) and gets a compact entry in ``history``.  A fact whose
        window partly overlaps another fact of the same source and stage is kept
        and reported in its ``overlaps`` list.
        """
        if status not in FACT_STATUSES:
            raise ValidationError("unknown fact status",
                                  details={"status": status,
                                           "allowed": list(FACT_STATUSES)})
        if measured_at is not None and not isinstance(measured_at, str):
            raise ValidationError("measured_at must be a string or null")
        if not isinstance(metrics, Mapping):
            raise ValidationError("metrics must be a mapping")
        key = fact_key(source_id, stage, window, metrics)
        key_hash = _key_hash(key)
        stored_metrics = _clone(dict(metrics))
        stored_fingerprints = _normalise_fingerprints(fingerprints)
        stored_revision = _clone(revision)

        facts = self._state["facts"]
        previous_ids = [
            fact_id for fact_id, fact in facts.items()
            if fact.get("key_sha256") == key_hash
            and fact.get("status") != STATUS_SUPERSEDED
        ]
        previous_ids.sort()

        token = self._begin_change()
        fact_id = f"{key_hash}#{token}"
        superseded: list[str] = []
        for previous_id in previous_ids:
            previous = facts.get(previous_id)
            if not isinstance(previous, dict):
                continue
            previous["status"] = STATUS_SUPERSEDED
            previous["superseded_by"] = fact_id
            superseded.append(previous_id)
            self._state["history"].append({
                "seq": token,
                "kind": "fact_superseded",
                "fact_id": previous_id,
                "key": _clone(previous.get("key")),
                "revision": _clone(previous.get("revision")),
                "measured_at": previous.get("measured_at"),
                "superseded_by": fact_id,
            })

        overlaps = [
            _clone(fact.get("key")) for fact_id, fact in sorted(facts.items())
            if fact_id not in previous_ids
            and fact.get("status") != STATUS_SUPERSEDED
            and fact.get("source_id") == source_id
            and fact.get("stage") == stage
            and _windows_overlap(key, fact)
        ]

        record = {
            "id": fact_id,
            "source_id": source_id,
            "stage": stage,
            "window": _clone(key["window"]),
            "key": key,
            "key_sha256": key_hash,
            "metrics": stored_metrics,
            "fingerprints": stored_fingerprints,
            "revision": stored_revision,
            "measured_at": measured_at,
            "status": status,
            "stale_reason": None,
            "overlaps": overlaps,
            "supersedes": superseded,
            "superseded_by": None,
        }
        facts[fact_id] = record
        self._append_change({
            "seq": token,
            "kind": "fact",
            "fact_id": fact_id,
            "source_id": source_id,
            "stage": stage,
            "window": _clone(key["window"]),
            "metrics": _clone(key["metrics"]),
            "status": status,
            "superseded": superseded,
            "overlaps": _clone(overlaps),
        })
        return _clone(record)

    def mark_stale(
        self,
        changed: Mapping[str, Sequence[str]],
        routing_changed: bool = False,
    ) -> dict[str, str]:
        """Mark current facts stale whose dependencies the change touches.

        ``changed`` maps a track guid to the fingerprint fields that moved, plus
        the special key ``"master"`` for the master object.  An empty field list
        is a change whose fields are unknown and is still treated as a change.
        ``routing_changed`` must be the ``routing_changed`` flag returned by
        :meth:`upsert_objects`.

        Dependency rule (frozen): via-master facts depend on source + master +
        routing; master-stage facts on master only; source-stage facts on their
        source only.  Only ``current`` facts transition; already-stale or
        superseded facts are left alone, so a repeated call reports nothing.
        Returns ``{fact_id: reason}`` for the facts that went stale.
        """
        if not isinstance(routing_changed, bool):
            raise ValidationError("routing_changed must be a boolean")
        if not isinstance(changed, Mapping):
            raise ValidationError("changed must be a mapping of guid -> fields")
        normalised: dict[str, list[str]] = {}
        for guid, fields in changed.items():
            if not isinstance(guid, str) or not guid:
                raise ValidationError("changed keys must be non-empty strings")
            if isinstance(fields, (str, bytes)) or not isinstance(fields, Sequence):
                raise ValidationError("changed fields must be a sequence of names",
                                      details={"guid": guid})
            names = sorted({str(field) for field in fields})
            normalised[guid] = names
        master_fields = normalised.pop(MASTER_STAGE_PREFIX, [])

        stale: dict[str, str] = {}
        for fact_id in sorted(self._state["facts"]):
            fact = self._state["facts"][fact_id]
            if not isinstance(fact, dict) or fact.get("status") != STATUS_CURRENT:
                continue
            dependency = _stage_dependency(str(fact.get("stage") or ""))
            source_guid = _source_fingerprint_guid(fact)
            matched_guid: str | None = None
            if source_guid is not None:
                if source_guid in normalised:
                    matched_guid = source_guid
            else:
                source_id = str(fact.get("source_id") or "")
                if source_id in normalised:
                    matched_guid = source_id
                else:
                    derived = _guid_from_source_id(source_id)
                    if derived is not None and derived in normalised:
                        matched_guid = derived
            source_changed = matched_guid is not None
            reasons: list[str] = []
            source_fields = normalised.get(matched_guid or "", [])
            if dependency == "master":
                if master_fields:
                    reasons.append(f"master:{','.join(master_fields) or 'unknown'}")
            elif dependency == "via_master":
                if source_changed:
                    reasons.append(f"source:{','.join(source_fields) or 'unknown'}")
                if master_fields:
                    reasons.append(f"master:{','.join(master_fields) or 'unknown'}")
                if routing_changed:
                    reasons.append("routing")
            else:
                if source_changed:
                    reasons.append(f"source:{','.join(source_fields) or 'unknown'}")
            if not reasons:
                continue
            reason = "; ".join(reasons)
            fact["status"] = STATUS_STALE
            fact["stale_reason"] = reason
            stale[fact_id] = reason

        if stale:
            token = self._begin_change()
            self._append_change({"seq": token, "kind": "stale",
                                 "facts": _clone(stale)})
        return stale

    # -- queries ------------------------------------------------------------ #

    def facts_for(
        self,
        source_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Facts filtered by source and/or status, in deterministic key order."""
        if status is not None and status not in FACT_STATUSES:
            raise ValidationError("unknown fact status",
                                  details={"status": status,
                                           "allowed": list(FACT_STATUSES)})
        selected = []
        for fact in self._state["facts"].values():
            if not isinstance(fact, Mapping):
                continue
            if source_id is not None and fact.get("source_id") != source_id:
                continue
            if status is not None and fact.get("status") != status:
                continue
            selected.append(fact)
        selected.sort(key=lambda fact: (_canonical(fact.get("key")), str(fact.get("id"))))
        return _clone(selected)

    def coverage_summary(self) -> dict[str, Any]:
        """Fact counts by status and by source object, plus object coverage."""
        by_status = {name: 0 for name in FACT_STATUSES}
        by_object: dict[str, dict[str, int]] = {}
        for fact in self._state["facts"].values():
            if not isinstance(fact, Mapping):
                continue
            status = str(fact.get("status"))
            if status not in by_status:
                by_status[status] = 0
            by_status[status] += 1
            source = str(fact.get("source_id") or "")
            counts = by_object.setdefault(source, {name: 0 for name in FACT_STATUSES})
            counts[status] = counts.get(status, 0) + 1
        objects = self._state["objects"]
        return {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "by_object": by_object,
            "objects": {
                "tracks": len(objects.get("tracks") or {}),
                "master": objects.get("master") is not None,
                "routing": objects.get("routing") is not None,
            },
            "token": self.state_token,
        }

    def changes_since(self, token: int) -> dict[str, Any]:
        """Change-log entries after ``token``; a newer token means lost lineage.

        ``token > state_token`` can only happen when this state file was reset or
        replaced after the caller read it.  Instead of returning an empty delta
        that looks current, the whole retained log is returned with
        ``"reset": true`` so the caller can refresh.
        """
        if isinstance(token, bool) or not isinstance(token, int) or token < 0:
            raise ValidationError("token must be a non-negative integer",
                                  details={"token": token})
        current = self.state_token
        reset = token > current
        changes = [
            _clone(entry) for entry in self._state["changes"]
            if isinstance(entry, Mapping)
            and (reset or int(entry.get("seq") or 0) > token)
        ]
        result = {
            "schema": SCHEMA,
            "project_id": self.project_id,
            "since": token,
            "token": current,
            "changes": changes,
            "count": len(changes),
        }
        if reset:
            result["reset"] = True
        return result
