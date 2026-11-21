"""Read status evidence, freshness and coverage from the project state.

:mod:`reaper_audio.context.project_state` is the single source of truth for
what the library knows about one project: the objects it last read and the
measurement facts recorded on them.  This module is the read side of that
document.  It never measures, renders, writes or talks to REAPER; it turns a
state mapping into the three answers callers used to assemble from capture
archives and evidence units:

* :func:`state_evidence_section` — the ``evidence`` status section
  (state/reason/unit counts plus a by-status ``counts`` breakdown);
* :func:`state_freshness` — per-source, per-stage freshness from the
  fingerprints stored *with each fact* compared against a live read;
* :func:`state_coverage` — fact counters by source, stage and status, plus the
  distinct windows those facts cover.

Honesty rules
-------------

* **No state is stated, never implied.**  Every function accepts ``None`` and
  returns an explicit ``no_state``/``unknown`` answer with zero counts.  It
  never falls back to invented coverage, and an empty state is reported as
  ``empty`` rather than ``current``.
* **Missing evidence is not clean evidence.**  A field the live read cannot
  provide makes the answer ``unknown`` (reason ``fingerprint_incomplete``), not
  ``current``.  A field the fact never recorded is not compared at all: the
  fingerprint could not prove anything about it in the first place.
* **Comparison uses the stored fact fingerprint**, not the state's newest
  object fingerprint.  A fact measured two edits ago must go stale even when
  the state objects have since moved on.  :mod:`state_diff` supplies the
  field-level comparison and its dependency vocabulary.

No project name, track list, metric name or threshold lives here.  Stage names
are classified structurally from the same markers the state store uses
(``via_master``, ``master``, ``premaster``, ``external``).
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import ValidationError
from . import state_diff
from .freshness import (FRESHNESS_SCHEMA, STATE_CURRENT, STATE_STALE,
                        STATE_UNKNOWN, summarise)
from .project_state import (
    FACT_STATUSES,
    FINGERPRINT_FIELDS,
    MASTER_STAGE_PREFIX,
    SCHEMA,
    STATUS_CURRENT,
    STATUS_STALE,
    STATUS_SUPERSEDED,
    VIA_MASTER_MARKER,
    ProjectState,
    StateStore,
    fingerprint_master,
    fingerprint_routing,
    fingerprint_track,
)

STATE_COVERAGE_SCHEMA = "reaper-workbench.state-coverage/1"

_DETAIL_LEVELS: tuple[str, ...] = ("summary", "full")

#: Fields the state fingerprints that :mod:`state_diff` does not compare
#: (today only ``volume_db``, which a summary row carries instead of a linear
#: fader value).  They are still part of the stored fingerprint and must not be
#: silently ignored when a fact recorded one.
_STATE_ONLY_FIELDS: tuple[str, ...] = tuple(
    name for name in FINGERPRINT_FIELDS if name not in state_diff.FINGERPRINT_FIELDS
)

#: Worst-first ordering used when one source/stage has several facts.
_STATE_ORDER = {STATE_CURRENT: 0, STATE_STALE: 1, STATE_UNKNOWN: 2}

__all__ = [
    "STATE_COVERAGE_SCHEMA",
    "read_state",
    "state_block",
    "state_coverage",
    "state_evidence_section",
    "state_freshness",
]


# --------------------------------------------------------------------------- #
# document helpers
# --------------------------------------------------------------------------- #


def _document(state: object) -> dict[str, Any] | None:
    """Return the state document for a mapping or :class:`ProjectState`.

    ``None`` means "this project has no state".  A mapping must carry
    ``facts``/``objects`` mappings; anything else is refused instead of being
    read as an empty, falsely clean state.
    """
    if state is None:
        return None
    if isinstance(state, ProjectState):
        return state.state
    if not isinstance(state, Mapping):
        raise ValidationError("state must be a ProjectState, a mapping or None",
                              details={"type": type(state).__name__})
    document = dict(state)
    if not isinstance(document.get("facts"), Mapping):
        raise ValidationError("project state facts must be a mapping")
    if not isinstance(document.get("objects"), Mapping):
        raise ValidationError("project state objects must be a mapping")
    return document


def _token(document: Mapping[str, Any]) -> int:
    token = document.get("token")
    if isinstance(token, bool) or not isinstance(token, int) or token < 0:
        return 0
    return token


def _facts(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every fact record, in deterministic id order."""
    facts = document.get("facts") or {}
    rows = [fact for fact in facts.values() if isinstance(fact, Mapping)]
    rows.sort(key=lambda fact: str(fact.get("id") or ""))
    return rows


def _status_counts(facts: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Counts for every documented status; unknown statuses keep their name."""
    counts = {name: 0 for name in FACT_STATUSES}
    for fact in facts:
        status = str(fact.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _window_pair(fact: Mapping[str, Any]) -> tuple[float | None, float | None]:
    """``(start_s, end_s)`` of a fact, ``(None, None)`` when unusable."""
    window = fact.get("window")
    if isinstance(window, Mapping):
        start_raw = window.get("start_s", window.get("start"))
        end_raw = window.get("end_s", window.get("end"))
    elif (isinstance(window, Sequence) and not isinstance(window, (str, bytes))
          and len(window) == 2):
        start_raw, end_raw = window[0], window[1]
    else:
        return None, None
    return _number(start_raw), _number(end_raw)


def _window_dict(fact: Mapping[str, Any]) -> dict[str, float] | None:
    start, end = _window_pair(fact)
    if start is None or end is None or end < start:
        return None
    return {"start_s": start, "end_s": end}


def _window_key(fact: Mapping[str, Any]) -> tuple[str, str, float | None, float | None]:
    start, end = _window_pair(fact)
    return (str(fact.get("source_id") or ""), str(fact.get("stage") or ""),
            start, end)


def _objects(document: Mapping[str, Any]) -> Mapping[str, Any]:
    objects = document.get("objects")
    return objects if isinstance(objects, Mapping) else {}


def _state_sources(document: Mapping[str, Any]) -> dict[str, Any]:
    """Source ids implied by the state objects (tracks + master)."""
    objects = _objects(document)
    tracks = objects.get("tracks")
    track_guids = sorted(str(guid) for guid in tracks) if isinstance(tracks, Mapping) else []
    master = objects.get("master")
    master_guid = None
    if isinstance(master, Mapping):
        guid = master.get("guid")
        if isinstance(guid, str) and guid:
            master_guid = guid
    all_sources = [f"track:{guid}" for guid in track_guids]
    if master_guid:
        all_sources.append(f"master:{master_guid}")
    return {"tracks": track_guids,
            "master": master_guid,
            "all": sorted(all_sources)}


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #


def read_state(archive_root: str | os.PathLike[str] | None,
               project_id: str) -> dict[str, Any] | None:
    """Load one project's state document, or ``None`` when none exists.

    A missing archive root or a missing ``state.json`` is *no state*: the
    caller must take its legacy path and say so.  A present but unreadable or
    foreign file raises ``ValidationError`` from :class:`StateStore` — losing
    state quietly is worse than a loud failure.
    """
    if not archive_root or not isinstance(project_id, str) or not project_id:
        return None
    store = StateStore(archive_root)
    if not store.path_for(project_id).is_file():
        return None
    return store.load(project_id)


# --------------------------------------------------------------------------- #
# evidence section
# --------------------------------------------------------------------------- #


def _group_counts(facts: Sequence[Mapping[str, Any]],
                  key: str) -> dict[str, dict[str, int]]:
    """Status counters per value of ``key`` (``source_id`` or ``stage``)."""
    groups: dict[str, dict[str, int]] = {}
    windows: dict[str, set[tuple[str, str, float | None, float | None]]] = {}
    for fact in facts:
        name = str(fact.get(key) or "")
        row = groups.setdefault(name, {status: 0 for status in FACT_STATUSES})
        row["facts"] = row.get("facts", 0) + 1
        status = str(fact.get("status") or "")
        row[status] = row.get(status, 0) + 1
        windows.setdefault(name, set()).add(_window_key(fact))
    for name, row in groups.items():
        row["windows"] = len(windows.get(name) or ())
    return groups


def state_evidence_section(state: object, *, detail: str = "summary") -> dict[str, Any]:
    """The status ``evidence`` section derived from one project state.

    ``state`` may be a :class:`ProjectState` or its document; ``None`` yields an
    explicit ``unavailable``/``no_state`` answer with zero counts.  The verdict
    is ``current`` (every fact current), ``stale`` (any stale fact),
    ``superseded`` (only superseded facts remain), ``empty`` (no facts) or
    ``unavailable`` (no state).  ``detail`` is ``summary`` (the shape the status
    answer has always used, plus ``counts``) or ``full`` (adds per-source/
    per-stage counters, the window total and the compact stale-fact rows).
    """
    if detail not in _DETAIL_LEVELS:
        raise ValidationError("detail must be 'summary' or 'full'",
                              details={"detail": detail,
                                       "allowed": list(_DETAIL_LEVELS)})
    document = _document(state)
    if document is None:
        return {
            "state": "unavailable",
            "reason": "no_state",
            "units_total": 0,
            "units_current": 0,
            "units_stale": 0,
            "units_superseded": 0,
            "counts": {status: 0 for status in FACT_STATUSES},
            "state_token": 0,
        }

    facts = _facts(document)
    counts = _status_counts(facts)
    total = len(facts)
    if total == 0:
        label, reason = "empty", "no_facts"
    elif counts[STATUS_STALE]:
        label, reason = "stale", "facts_stale"
    elif counts[STATUS_CURRENT] == 0:
        label, reason = "superseded", "no_current_facts"
    else:
        label, reason = "current", None

    result: dict[str, Any] = {
        "state": label,
        "reason": reason,
        "units_total": total,
        "units_current": counts[STATUS_CURRENT],
        "units_stale": counts[STATUS_STALE],
        "units_superseded": counts[STATUS_SUPERSEDED],
        "counts": counts,
        "state_token": _token(document),
    }
    if detail == "full":
        windows = {_window_key(fact) for fact in facts}
        result["by_source"] = _group_counts(facts, "source_id")
        result["by_stage"] = _group_counts(facts, "stage")
        result["windows_total"] = len(windows)
        result["stale_facts"] = [
            {
                "id": fact.get("id"),
                "source_id": fact.get("source_id"),
                "stage": fact.get("stage"),
                "window": fact.get("window"),
                "reason": fact.get("stale_reason"),
            }
            for fact in facts
            if fact.get("status") == STATUS_STALE
        ]
    return result


def _changes_since(document: Mapping[str, Any], token: int) -> dict[str, Any]:
    """Same shape/rule as :meth:`ProjectState.changes_since`, read-only."""
    if isinstance(token, bool) or not isinstance(token, int) or token < 0:
        raise ValidationError("since must be a non-negative integer",
                              details={"since": token})
    current = _token(document)
    reset = token > current
    changes = [
        dict(entry) for entry in document.get("changes") or []
        if isinstance(entry, Mapping)
        and (reset or int(entry.get("seq") or 0) > token)
    ]
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "project_id": str(document.get("project_id") or ""),
        "since": token,
        "token": current,
        "changes": changes,
        "count": len(changes),
    }
    if reset:
        result["reset"] = True
    return result


def state_block(state: object, *, since: int | None = None) -> dict[str, Any]:
    """The additive ``state`` block carried next to the evidence section.

    ``{state_token, facts, current, stale, superseded, present}`` plus
    ``changes_since`` when the caller supplies the token it last saw.  It is
    deliberately separate from ``evidence["state"]``, which stays the evidence
    verdict string.
    """
    document = _document(state)
    if document is None:
        return {"present": False, "state_token": 0, "facts": 0,
                "current": 0, "stale": 0, "superseded": 0}
    facts = _facts(document)
    counts = _status_counts(facts)
    block: dict[str, Any] = {
        "present": True,
        "state_token": _token(document),
        "facts": len(facts),
        "current": counts[STATUS_CURRENT],
        "stale": counts[STATUS_STALE],
        "superseded": counts[STATUS_SUPERSEDED],
    }
    if since is not None:
        block["changes_since"] = _changes_since(document, since)
    return block


# --------------------------------------------------------------------------- #
# freshness
# --------------------------------------------------------------------------- #


def _fields_of(value: object) -> Mapping[str, Any] | None:
    """Fingerprint field map from a fingerprint record or a raw row."""
    if not isinstance(value, Mapping):
        return None
    fields = value.get("fields")
    if isinstance(fields, Mapping):
        return fields
    return value


def _live_object_fields(value: object, kind: str) -> Mapping[str, Any] | None:
    """Field map for a live track/master read.

    A ``fingerprint_*`` record (or a bare field map) is used as-is.  A raw
    status/digest row is passed through the store's own fingerprint function, so
    both sides have the same normalised shape (FX slots, sends, items).  That
    also keeps shape differences honest: a summary row that cannot carry FX
    rows fingerprints those fields as ``null`` and later reads ``unknown``.
    """
    if not isinstance(value, Mapping):
        return None
    fields = value.get("fields")
    if isinstance(fields, Mapping):
        return fields
    if any(key not in FINGERPRINT_FIELDS for key in value):
        normalized = (fingerprint_master(value) if kind == "master"
                      else fingerprint_track(value))
        return normalized["fields"]
    return value


def _routing_fields(value: object) -> Mapping[str, Any] | None:
    """Routing fingerprint as normalised ``{"edges": [...]}``.

    Accepts a stored fingerprint record, already-normalised edges, or raw send /
    owner rows (which go through :func:`fingerprint_routing`).  Normalising both
    sides is what keeps an unchanged route from looking like every send moved.
    """
    if value is None:
        return None
    normalized_edge_keys = {"source", "destination", "order", "volume", "pan", "mode"}
    if isinstance(value, Mapping):
        fields = value.get("fields")
        if isinstance(fields, Mapping):
            return {"edges": list(fields.get("edges") or [])}
        edges = value.get("edges")
        if isinstance(edges, (list, tuple)):
            raw = any(isinstance(row, Mapping)
                      and any(key not in normalized_edge_keys for key in row)
                      for row in edges)
            if raw:
                return {"edges": list(fingerprint_routing(edges)["fields"]["edges"])}
            return {"edges": list(edges)}
        return dict(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return {"edges": list(fingerprint_routing(value)["fields"]["edges"])}
    return None


def _guid_from_source_id(source_id: str) -> str | None:
    """``track:{GUID}`` / ``master:{GUID}`` -> ``{GUID}``; else ``None``."""
    _, separator, remainder = source_id.partition(":")
    if separator and len(remainder) > 2 and remainder.startswith("{") \
            and remainder.endswith("}"):
        return remainder
    return None


def _source_guid(fact: Mapping[str, Any]) -> str | None:
    fingerprints = fact.get("fingerprints")
    if isinstance(fingerprints, Mapping):
        source = fingerprints.get("source")
        if isinstance(source, Mapping):
            guid = source.get("guid")
            if isinstance(guid, str) and guid:
                return guid
    return _guid_from_source_id(str(fact.get("source_id") or ""))


def _stage_dependency(stage: str, *, has_source: bool) -> str:
    """``external``/``via_master``/``master``/``source``/``unknown``.

    Mirrors the state store's dependency vocabulary.  A stage with a source but
    no recognisable marker defaults to ``via_master``: every measurement this
    library produces runs through the master, and under-invalidating is the one
    failure a freshness reader must not have.
    """
    name = stage.strip().lower()
    if not name:
        return "via_master" if has_source else "unknown"
    if "external" in name:
        return "external"
    if VIA_MASTER_MARKER in name:
        return "via_master"
    if name == "premaster" or "premaster" in name:
        return "source"
    if (name == MASTER_STAGE_PREFIX or name.startswith(f"{MASTER_STAGE_PREFIX}_")
            or "master" in name):
        return "master"
    return "via_master" if has_source else "unknown"


def _normalise_live(value: object) -> dict[str, Any] | None:
    """Normalise a live fingerprint read into a comparable project shape.

    Accepted forms::

        {"tracks": {guid: row} | [row, ...], "master": row, "routing": rows}
        {"track:<guid>": row, "master": row}
        {"objects": {...}}                     # a stored-state wrapper

    Rows may be raw status/digest rows or ``fingerprint_*`` records; both are
    reduced to their field maps.  Anything else raises rather than being read
    as "no change".
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError("live_fingerprints must be a mapping or None",
                              details={"type": type(value).__name__})
    nested = value.get("objects")
    if isinstance(nested, Mapping) and not any(
            key in value for key in ("tracks", "master", "routing")):
        value = nested

    tracks: dict[str, Mapping[str, Any]] = {}
    master: Mapping[str, Any] | None = None
    routing: Mapping[str, Any] | None = None
    tracks_present = "tracks" in value and value.get("tracks") is not None
    master_present = isinstance(value.get("master"), Mapping)
    routing_present = "routing" in value and value.get("routing") is not None

    if tracks_present:
        raw_tracks = value.get("tracks")
        if isinstance(raw_tracks, Mapping):
            for guid, row in raw_tracks.items():
                fields = _live_object_fields(row, "track")
                if isinstance(guid, str) and guid and isinstance(fields, Mapping):
                    tracks[guid] = fields
        elif isinstance(raw_tracks, Sequence) and not isinstance(raw_tracks, (str, bytes)):
            for row in raw_tracks:
                if not isinstance(row, Mapping):
                    continue
                guid = row.get("guid")
                fields = _live_object_fields(row, "track")
                if isinstance(guid, str) and guid and isinstance(fields, Mapping):
                    tracks[guid] = fields
        else:
            raise ValidationError("live tracks must be a mapping or a sequence of rows")
    else:
        for key, row in value.items():
            if not isinstance(key, str):
                continue
            if key.startswith("track:"):
                fields = _live_object_fields(row, "track")
                if isinstance(fields, Mapping):
                    tracks[key[len("track:"):]] = fields
                    tracks_present = True
            elif key == "master" or key.startswith("master:"):
                fields = _live_object_fields(row, "master")
                if isinstance(fields, Mapping):
                    master = fields
                    master_present = True

    if master_present and master is None:
        master = _live_object_fields(value.get("master"), "master")
    if routing_present:
        routing = _routing_fields(value.get("routing"))

    return {
        "tracks": tracks,
        "master": master,
        "routing": routing,
        "present": {"tracks": tracks_present, "master": master_present,
                    "routing": routing_present},
    }


def _same_scalar(left: object, right: object) -> bool:
    """Numeric equality without treating ``True`` as ``1``."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return left == right


def _diff_fields(kind: str, stored: Mapping[str, Any],
                 live: Mapping[str, Any], guid: str | None) -> list[str]:
    """Field names changed between two comparable subsets, via ``state_diff``."""
    if kind == "track":
        diff = state_diff.diff_fingerprints({"tracks": {guid: stored}},
                                            {"tracks": {guid: live}})
        entry = diff["tracks"].get(guid or "")
        return list(entry.get("fields") or []) if entry else []
    if kind == "master":
        diff = state_diff.diff_fingerprints({"master": stored}, {"master": live})
        return list(diff["master"].get("fields") or [])
    diff = state_diff.diff_fingerprints({"routing": stored}, {"routing": live})
    return list(diff["routing"].get("fields") or [])


def _compare_fields(stored: Mapping[str, Any], live: Mapping[str, Any],
                    *, kind: str, guid: str | None = None
                    ) -> tuple[list[str], list[str]]:
    """``(changed fields, unverifiable fields)`` for one object.

    Only fields the fact recorded with a non-null value are candidates: a field
    the measurement never read cannot go stale.  A candidate the live read does
    not carry is *unverifiable* (the caller turns that into ``unknown``), never
    silently equal.
    """
    names = list(dict.fromkeys((*FINGERPRINT_FIELDS, *state_diff.FINGERPRINT_FIELDS)))
    comparable: list[str] = []
    unverified: list[str] = []
    for name in names:
        stored_value = stored.get(name)
        if stored_value is None:
            continue
        if name in live and live.get(name) is not None:
            comparable.append(name)
        else:
            unverified.append(name)
    if not comparable:
        return [], (unverified or ["fingerprint_fields_unreadable"])

    changed: list[str] = []
    shared = [name for name in comparable if name in state_diff.FINGERPRINT_FIELDS]
    if shared:
        changed.extend(_diff_fields(kind,
                                    {name: stored[name] for name in shared},
                                    {name: live[name] for name in shared},
                                    guid))
    for name in comparable:
        if name in _STATE_ONLY_FIELDS and not _same_scalar(stored[name], live[name]):
            changed.append(name)
    return list(dict.fromkeys(changed)), unverified


def _compare_routing(stored: Mapping[str, Any],
                     live: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    stored_edges = stored.get("edges")
    live_edges = live.get("edges")
    if stored_edges is None:
        return [], ["routing"]
    if live_edges is None:
        return [], ["routing"]
    return _diff_fields("routing", stored, live, None), []


def _evaluate_fact(fact: Mapping[str, Any],
                   live: dict[str, Any] | None) -> dict[str, Any]:
    """Compare one fact's stored fingerprints with the live project shape."""
    status = str(fact.get("status") or "")
    if status == STATUS_STALE:
        return {"state": STATE_STALE,
                "reason": str(fact.get("stale_reason") or "stored_stale"),
                "changes": [], "unverified": []}

    fingerprints = fact.get("fingerprints")
    fingerprints = fingerprints if isinstance(fingerprints, Mapping) else {}
    source = _fields_of(fingerprints.get("source"))
    master = _fields_of(fingerprints.get("master"))
    routing = _routing_fields(fingerprints.get("routing"))
    guid = _source_guid(fact)
    stage = str(fact.get("stage") or "")
    dependency = _stage_dependency(stage, has_source=bool(guid))

    if dependency == "external":
        return {"state": STATE_UNKNOWN,
                "reason": "external_dependency_unverified",
                "changes": [], "unverified": []}
    if dependency == "unknown":
        return {"state": STATE_UNKNOWN, "reason": "fingerprint_scope_unknown",
                "changes": [], "unverified": []}
    if live is None:
        return {"state": STATE_UNKNOWN, "reason": "current_state_unreadable",
                "changes": [], "unverified": []}

    objects: list[dict[str, Any]] = []
    if dependency in {"source", "via_master"}:
        if not source or not guid:
            return {"state": STATE_UNKNOWN, "reason": "fingerprint_incomplete",
                    "changes": [], "unverified": ["source"]}
        if not live["present"]["tracks"]:
            return {"state": STATE_UNKNOWN,
                    "reason": "current_fingerprint_missing",
                    "changes": [], "unverified": ["source"]}
        live_source = live["tracks"].get(guid)
        if live_source is None:
            return {"state": STATE_STALE, "reason": "source_missing",
                    "changes": [], "unverified": []}
        changed, unverified = _compare_fields(source, live_source,
                                              kind="track", guid=guid)
        objects.append({"object": "source", "guid": guid,
                        "fields": changed, "unverified": unverified})
    if dependency in {"master", "via_master"}:
        if not master:
            return {"state": STATE_UNKNOWN, "reason": "fingerprint_incomplete",
                    "changes": [], "unverified": ["master"]}
        if live["master"] is None:
            return {"state": STATE_UNKNOWN,
                    "reason": "current_fingerprint_missing",
                    "changes": [], "unverified": ["master"]}
        changed, unverified = _compare_fields(master, live["master"], kind="master")
        objects.append({"object": "master", "guid": master.get("guid"),
                        "fields": changed, "unverified": unverified})
    if dependency == "via_master":
        if routing is None:
            return {"state": STATE_UNKNOWN, "reason": "fingerprint_incomplete",
                    "changes": [], "unverified": ["routing"]}
        if live["routing"] is None:
            return {"state": STATE_UNKNOWN,
                    "reason": "current_fingerprint_missing",
                    "changes": [], "unverified": ["routing"]}
        changed, unverified = _compare_routing(routing, live["routing"])
        objects.append({"object": "routing", "guid": None,
                        "fields": changed, "unverified": unverified})

    changed_objects = [row for row in objects if row["fields"]]
    if changed_objects:
        return {"state": STATE_STALE, "reason": "fingerprint_changed",
                "changes": changed_objects, "unverified": []}
    unverified_objects = [row for row in objects if row["unverified"]]
    if unverified_objects:
        return {"state": STATE_UNKNOWN, "reason": "fingerprint_incomplete",
                "changes": [], "unverified": unverified_objects}
    return {"state": STATE_CURRENT, "reason": None,
            "changes": [], "unverified": []}


def _new_group() -> dict[str, Any]:
    return {
        "state": STATE_CURRENT,
        "reason": None,
        "facts": 0,
        "windows": [],
        "intervals": [],
        "fingerprint": None,
        "current_fingerprint": None,
        "changes": [],
        "changed_fields": [],
        "unverified": [],
        "complete": True,
        "capture_id": None,
        "revision": None,
    }


def state_freshness(state: object,
                    live_fingerprints: Mapping[str, Any] | None) -> dict[str, Any]:
    """Per-source, per-stage freshness from stored fact fingerprints.

    ``live_fingerprints`` is the current project read: tracks/master/routing
    rows or ``fingerprint_*`` records (see :func:`_normalise_live`).  ``None``
    means it could not be read at all, so every source is ``unknown`` with
    reason ``current_state_unreadable``.  A source/stage with several facts
    aggregates to the worst state (``unknown`` > ``stale`` > ``current``);
    each window keeps its own verdict under ``windows``.

    ``unknown`` is never folded into ``current``: an unverifiable dependency
    leaves the answer unknown instead of calling it clean.
    """
    document = _document(state)
    if document is None:
        return {
            "schema": FRESHNESS_SCHEMA,
            "source": "state",
            "state": "no_state",
            "reason": "no_state",
            "state_token": 0,
            "facts_total": 0,
            "facts_evaluated": 0,
            "facts_superseded": 0,
            "stages": [],
            "sources": {},
            "summary": summarise({}),
            "changes": [],
        }

    live = _normalise_live(live_fingerprints) if live_fingerprints is not None else None
    facts = _facts(document)
    sources: dict[str, dict[str, dict[str, Any]]] = {}
    changes: list[dict[str, Any]] = []
    evaluated = 0
    superseded = 0
    stages: set[str] = set()

    for fact in facts:
        if fact.get("status") == STATUS_SUPERSEDED:
            superseded += 1
            continue
        evaluated += 1
        source_id = str(fact.get("source_id") or "")
        stage = str(fact.get("stage") or "unknown")
        stages.add(stage)
        verdict = _evaluate_fact(fact, live)
        group = sources.setdefault(source_id, {}).setdefault(stage, _new_group())
        group["facts"] += 1
        if _STATE_ORDER.get(verdict["state"], 2) > _STATE_ORDER.get(group["state"], 2):
            group["state"] = verdict["state"]
            group["reason"] = verdict["reason"]
        window = _window_dict(fact)
        interval = window if window is not None else None
        group["windows"].append({
            "fact_id": fact.get("id"),
            "window": fact.get("window"),
            "state": verdict["state"],
            "reason": verdict["reason"],
            "changes": verdict["changes"],
            "unverified": verdict["unverified"],
            "measured_at": fact.get("measured_at"),
        })
        if interval is not None and interval not in group["intervals"]:
            group["intervals"].append(interval)
        if group["fingerprint"] is None:
            fingerprint = ((fact.get("fingerprints") or {}).get("source")
                           or (fact.get("fingerprints") or {}).get("master"))
            if isinstance(fingerprint, Mapping):
                group["fingerprint"] = fingerprint.get("sha256")
        group["revision"] = group["revision"] or fact.get("revision")
        group["complete"] = group["complete"] and not verdict["unverified"]
        for row in verdict["changes"]:
            group["changes"].append({**row, "fact_id": fact.get("id")})
            for name in row["fields"]:
                if name not in group["changed_fields"]:
                    group["changed_fields"].append(name)
        for row in verdict["unverified"]:
            if row not in group["unverified"]:
                group["unverified"].append(row)
        if verdict["changes"]:
            changes.append({
                "fact_id": fact.get("id"),
                "source_id": source_id,
                "stage": stage,
                "window": fact.get("window"),
                "objects": verdict["changes"],
            })

    return {
        "schema": FRESHNESS_SCHEMA,
        "source": "state",
        "state": "ok" if facts else "empty",
        "reason": None if facts else "no_facts",
        "state_token": _token(document),
        "facts_total": len(facts),
        "facts_evaluated": evaluated,
        "facts_superseded": superseded,
        "stages": sorted(stages),
        "sources": sources,
        "summary": summarise(sources),
        "changes": changes,
    }


# --------------------------------------------------------------------------- #
# coverage
# --------------------------------------------------------------------------- #


def _coverage_groups(facts: Sequence[Mapping[str, Any]]) -> tuple[
        dict[str, dict[str, int]],
        dict[str, dict[str, int]],
        dict[str, dict[str, dict[str, int]]],
        dict[str, list[dict[str, float]]]]:
    """Counters by source, by stage, by source/stage and current intervals."""
    by_source: dict[str, dict[str, int]] = {}
    by_stage: dict[str, dict[str, int]] = {}
    by_source_stage: dict[str, dict[str, dict[str, int]]] = {}
    intervals: dict[str, list[dict[str, float]]] = {}
    windows_by_source: dict[str, set[tuple[str, str, float | None, float | None]]] = {}
    windows_by_stage: dict[str, set[tuple[str, str, float | None, float | None]]] = {}
    current_by_source: dict[str, set[tuple[str, str, float | None, float | None]]] = {}
    current_by_stage: dict[str, set[tuple[str, str, float | None, float | None]]] = {}
    pair_windows: dict[tuple[str, str], set[tuple[str, str, float | None, float | None]]] = {}
    pair_current: dict[tuple[str, str], set[tuple[str, str, float | None, float | None]]] = {}

    def bump(group: dict[str, dict[str, int]], name: str,
             status: str) -> dict[str, int]:
        row = group.setdefault(name, {state: 0 for state in FACT_STATUSES})
        row["facts"] = row.get("facts", 0) + 1
        row[status] = row.get(status, 0) + 1
        return row

    for fact in facts:
        source = str(fact.get("source_id") or "")
        stage = str(fact.get("stage") or "")
        status = str(fact.get("status") or "")
        bump(by_source, source, status)
        bump(by_stage, stage, status)
        bump(by_source_stage.setdefault(source, {}), stage, status)
        key = _window_key(fact)
        pair_key = (source, stage)
        windows_by_source.setdefault(source, set()).add(key)
        windows_by_stage.setdefault(stage, set()).add(key)
        pair_windows.setdefault(pair_key, set()).add(key)
        if fact.get("status") == STATUS_CURRENT:
            current_by_source.setdefault(source, set()).add(key)
            current_by_stage.setdefault(stage, set()).add(key)
            pair_current.setdefault(pair_key, set()).add(key)
            interval = _window_dict(fact)
            if interval is not None:
                rows = intervals.setdefault(source, [])
                if interval not in rows:
                    rows.append(interval)

    for name, row in by_source.items():
        row["windows"] = len(windows_by_source.get(name, ()))
        row["current_windows"] = len(current_by_source.get(name, ()))
    for name, row in by_stage.items():
        row["windows"] = len(windows_by_stage.get(name, ()))
        row["current_windows"] = len(current_by_stage.get(name, ()))
    for source, stages in by_source_stage.items():
        for stage, row in stages.items():
            pair_key = (source, stage)
            row["windows"] = len(pair_windows.get(pair_key, ()))
            row["current_windows"] = len(pair_current.get(pair_key, ()))
    return by_source, by_stage, by_source_stage, intervals


def state_coverage(state: object) -> dict[str, Any]:
    """Coverage counters from one project state, no passport judgement.

    Reports fact counts by status, by source, by stage and by source/stage,
    the distinct windows recorded (``windows_total``) and the subset measured
    ``current`` (``windows_current``), plus the real intervals per source that
    a passport can use.  ``state`` may be a :class:`ProjectState` or its
    document; ``None`` yields an explicit ``unknown``/``no_state`` answer.
    """
    document = _document(state)
    if document is None:
        return {
            "schema": STATE_COVERAGE_SCHEMA,
            "state": "unknown",
            "reason": "no_state",
            "state_token": 0,
            "facts_total": 0,
            "windows_total": 0,
            "windows_current": 0,
            "by_status": {status: 0 for status in FACT_STATUSES},
            "by_source": {},
            "by_stage": {},
            "by_source_stage": {},
            "intervals": {},
            "sources": {"tracks": [], "master": None, "all": []},
            "objects": {"tracks": 0, "master": False, "routing": False},
        }

    facts = _facts(document)
    by_source, by_stage, by_source_stage, intervals = _coverage_groups(facts)
    windows = {_window_key(fact) for fact in facts}
    windows_current = {_window_key(fact) for fact in facts
                       if fact.get("status") == STATUS_CURRENT}
    objects = _objects(document)
    tracks = objects.get("tracks")
    return {
        "schema": STATE_COVERAGE_SCHEMA,
        "state": "ok" if facts else "empty",
        "reason": None if facts else "no_facts",
        "state_token": _token(document),
        "facts_total": len(facts),
        "windows_total": len(windows),
        "windows_current": len(windows_current),
        "by_status": _status_counts(facts),
        "by_source": by_source,
        "by_stage": by_stage,
        "by_source_stage": by_source_stage,
        "intervals": intervals,
        "sources": _state_sources(document),
        "objects": {
            "tracks": len(tracks) if isinstance(tracks, Mapping) else 0,
            "master": isinstance(objects.get("master"), Mapping),
            "routing": isinstance(objects.get("routing"), Mapping),
        },
    }
