"""Read facts, changes and availability from one project state document.

:mod:`reaper_audio.context.project_state` owns the document; this module is the
read API an agent uses so it never has to measure or render again just to see
what is already known.  Everything here is pure: the functions take the loaded
document (the mapping :meth:`StateStore.load` returns), never read a file, never
talk to REAPER, never render and never consult a clock.  They do not mutate the
document and every result is deterministic and JSON-safe.

Three questions
---------------

* :func:`query_facts` — which measurements exist, filtered by source, stage,
  window, metric name or status.  By default the newest fact per logical key is
  returned and the older records are collapsed into its ``supersedes`` list;
  ``latest_per_key=False`` returns the full retained history.
* :func:`changes_since` — what happened after a state token: newly recorded
  facts, facts that went stale, facts that were superseded and object
  fingerprint changes.  Entries are only ever derived from the document's fact
  records, ``changes``/``history`` log and stored fingerprints; a category with
  no evidence stays empty instead of being guessed.
* :func:`availability` — whether a document exists and how many facts it holds
  per status.

Filter semantics
----------------

* ``source_id`` and ``stage`` are exact matches.
* ``window`` selects facts whose ``[start_s, end_s]`` intersects the requested
  window.  Intervals are closed, so a zero-length window is a point probe and
  touching endpoints intersect.  A ``None`` endpoint is open-ended (``-inf`` /
  ``+inf``), matching how the store records a partially known window.  Facts
  whose window equals the request exactly sort before merely overlapping ones.
* ``metrics`` selects facts whose measured metric-name set intersects the
  requested names.  An empty sequence is no filter.
* ``status`` is one of ``current``, ``stale`` or ``superseded``.

Logical key and supersession
----------------------------

A fact's logical key is ``(source_id, stage, window, sorted metric names)`` —
the same identity the store upserts by, not the stored ``key_sha256`` string.
With ``latest_per_key=True`` the newest record of each key is returned.  Older
records are not dropped silently: their ids appear in the newest row's
``supersedes`` list (the stored ``supersedes`` plus every older member of the
same logical key).  ``superseded_by`` on each row is the store's forward link,
so it is ``None`` for the newest record and names the newer id in full history.

Honesty rules
-------------

* A metric that is absent or ``None`` is omitted from ``values``; it is never
  rendered as ``0``.  A non-finite number is omitted too.  Band payloads are
  copied as mappings/lists exactly as measured, never flattened or averaged.
* ``stale_reason`` comes from the fact record when it carries one; otherwise it
  is read from the documented stale change (``changes`` kind ``stale``) or a
  stale history entry.  If neither names the fact, the answer is ``None``, not
  an invented reason.
* A fact whose creation sequence cannot be established (no token suffix and no
  change-log entry) is treated as pre-existing; it is never reported as new.
* ``changes_since`` on a token newer than the document (a reset/replaced state
  file) reports ``reset=True`` and returns the whole retained picture rather
  than an empty delta that would look current.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import ValidationError
from .project_state import (
    FACT_STATUSES,
    STATUS_CURRENT,
    STATUS_STALE,
    STATUS_SUPERSEDED,
)

__all__ = ["availability", "changes_since", "query_facts"]


# --------------------------------------------------------------------------- #
# document and scalar helpers
# --------------------------------------------------------------------------- #


def _require_state(state: object) -> Mapping[str, Any]:
    """Validate the envelope this read API needs; refuse rather than repair.

    ``facts``, when absent, means an empty state (the same convention as
    :func:`empty_state`).  A section of the wrong type is refused: reading it
    as empty would turn a broken document into a falsely clean answer.
    """
    if not isinstance(state, Mapping):
        raise ValidationError("state must be a project state mapping",
                              details={"type": type(state).__name__})
    facts = state.get("facts", {})
    if not isinstance(facts, Mapping):
        raise ValidationError("project state facts must be a mapping")
    objects = state.get("objects", {})
    if not isinstance(objects, Mapping):
        raise ValidationError("project state objects must be a mapping")
    for name in ("changes", "history"):
        section = state.get(name, [])
        if not isinstance(section, list):
            raise ValidationError(f"project state {name} must be a list")
    return state


def _number(value: object) -> float | None:
    """Finite float or ``None``; booleans are not numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _integer(value: object) -> int | None:
    """Integer or ``None``; booleans are not integers here."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _token_or_zero(document: Mapping[str, Any]) -> int:
    token = document.get("token")
    if isinstance(token, bool) or not isinstance(token, int) or token < 0:
        return 0
    return token


def _token_or_none(document: Mapping[str, Any]) -> int | None:
    token = document.get("token")
    if isinstance(token, bool) or not isinstance(token, int) or token < 0:
        return None
    return token


def _json_safe(value: object) -> Any:
    """Deterministic JSON-shaped copy: sorted mapping keys, no NaN/Inf.

    A non-finite float becomes ``None`` inside a structure (it was read but is
    not an encodable measurement).  An object that is not JSON-shaped is
    described instead of stringified ambiguously, so two different values
    cannot collapse to the same text.
    """
    if isinstance(value, Mapping):
        return {str(key): _json_safe(value[key])
                for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"$bytes": bytes(value).hex()}
    return {"$type": type(value).__name__, "$repr": repr(value)}


def _canonical(value: object) -> str:
    """Canonical JSON for ordering and grouping; always encodable."""
    return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _fact_rows(document: Mapping[str, Any]) -> list[tuple[int, Mapping[str, Any]]]:
    """Every fact record with its document position, in stored order."""
    facts = document.get("facts")
    if not isinstance(facts, Mapping):
        return []
    return [(position, fact) for position, fact in enumerate(facts.values())
            if isinstance(fact, Mapping)]


# --------------------------------------------------------------------------- #
# windows
# --------------------------------------------------------------------------- #


def _window_pair(value: object) -> tuple[float | None, float | None] | None:
    """``(start_s, end_s)`` from a pair or a ``{start_s, end_s}`` mapping.

    ``None`` endpoints stay ``None`` (open-ended).  A malformed shape, a
    non-numeric endpoint or ``start > end`` yields ``None``: the caller must
    not turn an unreadable window into ``[0, 0]``.
    """
    if isinstance(value, Mapping):
        start_raw = value.get("start_s", value.get("start"))
        end_raw = value.get("end_s", value.get("end"))
    elif (isinstance(value, Sequence) and not isinstance(value, (str, bytes))
          and len(value) == 2):
        start_raw, end_raw = value[0], value[1]
    else:
        return None
    start = None if start_raw is None else _number(start_raw)
    end = None if end_raw is None else _number(end_raw)
    if start_raw is not None and start is None:
        return None
    if end_raw is not None and end is None:
        return None
    if start is not None and end is not None and start > end:
        return None
    return start, end


def _requested_window(value: object) -> tuple[float | None, float | None] | None:
    """Validate a query window; ``None`` means no window filter."""
    if value is None:
        return None
    pair = _window_pair(value)
    if pair is None:
        raise ValidationError(
            "window must be {start_s, end_s} or a two-item sequence of finite "
            "numbers (null endpoints are open-ended)",
            details={"window": value},
        )
    return pair


def _windows_overlap(fact: tuple[float | None, float | None],
                     requested: tuple[float | None, float | None]) -> bool:
    """Closed-interval intersection; open ends are infinite.

    Closed means touching endpoints intersect (``[0, 5]`` touches ``[5, 10]``)
    and a zero-length request is a point probe.  That is the useful reading for
    "what measurements cover this span?".
    """
    fact_start, fact_end = fact
    query_start, query_end = requested
    low_fact = float("-inf") if fact_start is None else fact_start
    high_fact = float("inf") if fact_end is None else fact_end
    low_query = float("-inf") if query_start is None else query_start
    high_query = float("inf") if query_end is None else query_end
    return low_fact <= high_query and high_fact >= low_query


# --------------------------------------------------------------------------- #
# fact identity and values
# --------------------------------------------------------------------------- #


def _requested_metrics(value: object) -> frozenset[str] | None:
    """Validate the metric-name filter; empty means no filter."""
    if value is None:
        return None
    if isinstance(value, str):
        names: list[object] = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        names = list(value)
    else:
        raise ValidationError("metrics must be a sequence of metric names",
                              details={"metrics": value})
    result: list[str] = []
    for name in names:
        if not isinstance(name, str) or not name:
            raise ValidationError("metric names must be non-empty strings",
                                  details={"name": name})
        result.append(name)
    return frozenset(result) if result else None


def _key_of(fact: Mapping[str, Any]) -> dict[str, Any]:
    """The structured logical key of a fact, normalised for comparison.

    The stored ``key`` wins; a hand-built or older record falls back to the
    flat ``source_id``/``stage``/``window`` fields.  A window that cannot be
    read stays ``None`` rather than defaulting to zero.
    """
    raw_key = fact.get("key")
    key = raw_key if isinstance(raw_key, Mapping) else {}

    source = key.get("source_id") or fact.get("source_id")
    stage = key.get("stage") or fact.get("stage")

    raw_window = key.get("window")
    if raw_window is None:
        raw_window = fact.get("window")
    window = _window_pair(raw_window)

    names: list[str] = []
    raw_names = key.get("metrics")
    if isinstance(raw_names, Sequence) and not isinstance(raw_names, (str, bytes)):
        names = sorted({str(name) for name in raw_names
                        if isinstance(name, str) and name})
    if not names:
        stored = fact.get("metrics")
        if isinstance(stored, Mapping):
            names = sorted(str(name) for name in stored)

    return {
        "source_id": source if isinstance(source, str) else None,
        "stage": stage if isinstance(stage, str) else None,
        "window": [window[0], window[1]] if window is not None else None,
        "metrics": names,
    }


def _logical_key(key: Mapping[str, Any]) -> str:
    """Stable hash of ``(source, stage, window, metrics)``.

    Numbers are normalised to floats so ``0`` and ``0.0`` from a hand-built
    record and a stored record still describe one identity.  The stored
    ``key_sha256`` is deliberately not used: the logical key is the schema
    definition, not a digest that could have been computed over a different
    shape.
    """
    window = key.get("window")
    payload = {
        "source_id": key.get("source_id"),
        "stage": key.get("stage"),
        "window": ([None if item is None else float(item) for item in window]
                   if isinstance(window, Sequence) else None),
        "metrics": list(key.get("metrics") or []),
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _metric_values(fact: Mapping[str, Any]) -> dict[str, Any]:
    """Measured values only: numbers, text and band structures as stored.

    A metric present with ``None`` is not a measurement and is omitted; so is
    a non-finite number.  Nothing is coerced to ``0`` and a band mapping is
    copied whole.
    """
    metrics = fact.get("metrics")
    if not isinstance(metrics, Mapping):
        return {}
    values: dict[str, Any] = {}
    for name in sorted(metrics, key=str):
        value = metrics[name]
        if value is None:
            continue
        safe = _json_safe(value)
        if safe is None:
            continue
        values[str(name)] = safe
    return values


def _string_list(value: object) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    return sorted({item for item in value if isinstance(item, str) and item})


# --------------------------------------------------------------------------- #
# change/history indexes
# --------------------------------------------------------------------------- #


def _creation_index(document: Mapping[str, Any]) -> dict[str, int]:
    """Fact id -> the token that created it, from the change log.

    ``history`` entries for ``fact_superseded`` name the *new* fact with the
    supersession token, which is also its creation token; they are a fallback
    when the change log was trimmed.
    """
    index: dict[str, int] = {}
    for entry in document.get("changes") or []:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("kind") not in (None, "fact"):
            continue
        seq = _integer(entry.get("seq"))
        fact_id = entry.get("fact_id")
        if seq is not None and isinstance(fact_id, str) and fact_id:
            index[fact_id] = max(index.get(fact_id, seq), seq)
    for entry in document.get("history") or []:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("kind") != "fact_superseded":
            continue
        seq = _integer(entry.get("seq"))
        new_id = entry.get("superseded_by")
        if seq is not None and isinstance(new_id, str) and new_id:
            index.setdefault(new_id, seq)
    return index


def _fact_sequence(fact: Mapping[str, Any], index: Mapping[str, int]) -> int:
    """Creation token of a fact; ``-1`` when it cannot be established."""
    fact_id = fact.get("id")
    if not isinstance(fact_id, str) or not fact_id:
        return -1
    suffix = fact_id.rsplit("#", 1)[-1]
    if suffix.isdigit():
        return int(suffix)
    return index.get(fact_id, -1)


def _stale_index(document: Mapping[str, Any]) -> dict[str, tuple[int, str]]:
    """Fact id -> ``(transition token, reason)`` for stored stale marks."""
    index: dict[str, tuple[int, str]] = {}

    def add(fact_id: object, seq: object, reason: object) -> None:
        token = _integer(seq)
        if not isinstance(fact_id, str) or not fact_id or token is None:
            return
        text = reason if isinstance(reason, str) else ""
        previous = index.get(fact_id)
        if previous is None or token >= previous[0]:
            index[fact_id] = (token, text)

    for section in (document.get("changes") or [], document.get("history") or []):
        for entry in section:
            if not isinstance(entry, Mapping):
                continue
            seq = entry.get("seq")
            kind = entry.get("kind")
            if kind == "stale":
                facts = entry.get("facts")
                if isinstance(facts, Mapping):
                    for fact_id, reason in facts.items():
                        add(fact_id, seq, reason)
                continue
            if kind in ("fact_stale", "stale_fact", "fact_marked_stale"):
                add(entry.get("fact_id"), seq,
                    entry.get("stale_reason") or entry.get("reason"))
                continue
            if isinstance(entry.get("fact_id"), str) \
                    and isinstance(entry.get("stale_reason"), str):
                add(entry.get("fact_id"), seq, entry.get("stale_reason"))
    return index


def _supersede_index(document: Mapping[str, Any]) -> dict[str, int]:
    """Old fact id -> the token at which it was superseded."""
    index: dict[str, int] = {}

    def add(fact_id: object, seq: object) -> None:
        token = _integer(seq)
        if isinstance(fact_id, str) and fact_id and token is not None:
            index[fact_id] = max(index.get(fact_id, token), token)

    for entry in document.get("history") or []:
        if isinstance(entry, Mapping) and entry.get("kind") == "fact_superseded":
            add(entry.get("fact_id"), entry.get("seq"))
    for entry in document.get("changes") or []:
        if not isinstance(entry, Mapping) or entry.get("kind") != "fact":
            continue
        superseded = entry.get("superseded")
        if isinstance(superseded, Sequence) and not isinstance(superseded, (str, bytes)):
            for fact_id in superseded:
                add(fact_id, entry.get("seq"))
    return index


# --------------------------------------------------------------------------- #
# row assembly and ordering
# --------------------------------------------------------------------------- #


def _status(fact: Mapping[str, Any]) -> str | None:
    value = fact.get("status")
    return value if isinstance(value, str) and value else None


def _stale_reason(fact: Mapping[str, Any],
                  index: Mapping[str, tuple[int, str]]) -> str | None:
    """Stored reason, else the reason recorded by the stale transition."""
    reason = fact.get("stale_reason")
    if isinstance(reason, str) and reason:
        return reason
    fact_id = fact.get("id")
    if isinstance(fact_id, str):
        entry = index.get(fact_id)
        if entry is not None and entry[1]:
            return entry[1]
    return None


def _key_sha256(fact: Mapping[str, Any], key: Mapping[str, Any]) -> str:
    stored = fact.get("key_sha256")
    if isinstance(stored, str) and stored:
        return stored
    return _logical_key(key)


def _row(fact: Mapping[str, Any],
         stale_index: Mapping[str, tuple[int, str]]) -> dict[str, Any]:
    """One JSON-safe row; see the module docstring for the field rules."""
    key = _key_of(fact)
    fact_id = fact.get("id")
    superseded_by = fact.get("superseded_by")
    fingerprint = fact.get("fingerprints")
    overlaps = fact.get("overlaps")
    supersedes = fact.get("supersedes")
    measured_at = fact.get("measured_at")
    return {
        "id": fact_id if isinstance(fact_id, str) and fact_id else None,
        "key": _json_safe(key),
        "key_sha256": _key_sha256(fact, key),
        "source_id": key["source_id"],
        "stage": key["stage"],
        "window": _json_safe(key["window"]),
        "metrics_requested": list(key["metrics"]),
        "values": _metric_values(fact),
        "status": _status(fact),
        "stale_reason": _stale_reason(fact, stale_index),
        "fingerprints": (_json_safe(fingerprint)
                         if isinstance(fingerprint, Mapping) else None),
        "revision": _json_safe(fact.get("revision")),
        "measured_at": measured_at if isinstance(measured_at, str) else None,
        "superseded_by": (superseded_by
                          if isinstance(superseded_by, str) and superseded_by
                          else None),
        "overlaps": (_json_safe(overlaps)
                     if isinstance(overlaps, (list, tuple)) else None),
        "supersedes": (_string_list(supersedes)),
    }


def _text_sort(value: object) -> str:
    return value if isinstance(value, str) else ""


def _entry_sort_key(entry: tuple[int, Mapping[str, Any], Mapping[str, Any]],
                    requested: tuple[float | None, float | None] | None,
                    creation: Mapping[str, int]) -> tuple[Any, ...]:
    """Deterministic order: source, stage, exact window, start, key, lineage.

    The exact-match rank sits after ``stage`` so an exact window match leads
    its source/stage group; without a window filter every rank is equal and the
    base order is exactly ``source_id, stage, window start, key``.
    """
    position, fact, key = entry
    window = key.get("window")
    exact = 1
    if requested is not None and isinstance(window, Sequence):
        exact = 0 if (window[0], window[1]) == requested else 1
    start = window[0] if isinstance(window, Sequence) else None
    return (
        _text_sort(key.get("source_id")),
        _text_sort(key.get("stage")),
        exact,
        float("-inf") if start is None else float(start),
        _canonical(key),
        _fact_sequence(fact, creation),
        position,
        _text_sort(fact.get("id")),
    )


def _collapse_latest(entries: list[tuple[int, Mapping[str, Any], Mapping[str, Any]]],
                     stale_index: Mapping[str, tuple[int, str]],
                     creation: Mapping[str, int]) -> list[dict[str, Any]]:
    """Newest row per logical key, with older member ids in ``supersedes``.

    Group order follows the already-sorted ``entries`` (the first member of
    each logical key decides where the group sits), so the exact-window rank
    computed by :func:`_entry_sort_key` survives the collapse.
    """
    groups: dict[str, list[tuple[int, Mapping[str, Any], Mapping[str, Any]]]] = {}
    for entry in entries:
        groups.setdefault(_logical_key(entry[2]), []).append(entry)
    rows: list[dict[str, Any]] = []
    for members in groups.values():
        newest = max(members,
                     key=lambda entry: (_fact_sequence(entry[1], creation),
                                        entry[0]))
        row = _row(newest[1], stale_index)
        newest_id = row["id"]
        older: list[str] = []
        for _position, fact, _key in members:
            fact_id = fact.get("id")
            if isinstance(fact_id, str) and fact_id and fact_id != newest_id:
                older.append(fact_id)
        row["supersedes"] = sorted(set(row["supersedes"]) | set(older))
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# query
# --------------------------------------------------------------------------- #




def public_stage(stage: Any) -> str:
    """Collapse internal render stage names to the documented three.

    Writers keep the exact render provenance; readers speak via_master,
    master or external_file so an agent does not have to know bridge internals.
    """
    text = str(stage or "")
    if "external" in text:
        return "external_file"
    if "via_master" in text or "selected_track" in text:
        return "via_master"
    if text.startswith("master"):
        return "master"
    return text


def query_facts(
    state: Mapping[str, Any],
    *,
    source_id: str | None = None,
    stage: str | None = None,
    window: Sequence[float] | None = None,
    metrics: Sequence[str] | None = None,
    status: str | None = None,
    latest_per_key: bool = True,
) -> list[dict[str, Any]]:
    """Return the measured facts the document holds, optionally filtered.

    ``state`` is the loaded document (``StateStore.load(...)``), not a
    :class:`ProjectState`.  Filters are combined with AND:

    * ``source_id`` / ``stage`` — exact;
    * ``window`` — facts whose closed ``[start_s, end_s]`` intersects the
      requested window (``None`` endpoints are open-ended); exact matches sort
      before partial overlaps;
    * ``metrics`` — facts whose measured metric-name set intersects the names;
    * ``status`` — ``current``, ``stale`` or ``superseded``.

    With ``latest_per_key=True`` (the default) only the newest fact of each
    logical ``(source, stage, window, metrics)`` key is returned; older records
    are named in ``supersedes`` on that row and their forward link is still in
    each record's ``superseded_by``.  ``latest_per_key=False`` returns every
    retained record, including the superseded history.

    Rows are deterministic and JSON-safe.  ``values`` omits an absent or
    ``None`` metric instead of reporting ``0``; band payloads stay mappings.
    Output order is ``source_id``, ``stage``, window start, key, with exact
    window matches first when a window filter is supplied.
    """
    document = _require_state(state)

    if source_id is not None and not isinstance(source_id, str):
        raise ValidationError("source_id must be a string or None",
                              details={"source_id": source_id})
    if stage is not None and not isinstance(stage, str):
        raise ValidationError("stage must be a string or None",
                              details={"stage": stage})
    if status is not None and status not in FACT_STATUSES:
        raise ValidationError("unknown fact status",
                              details={"status": status,
                                       "allowed": list(FACT_STATUSES)})
    if not isinstance(latest_per_key, bool):
        raise ValidationError("latest_per_key must be a boolean",
                              details={"latest_per_key": latest_per_key})
    requested_window = _requested_window(window)
    requested_metrics = _requested_metrics(metrics)

    creation = _creation_index(document)
    stale_index = _stale_index(document)

    selected: list[tuple[int, Mapping[str, Any], Mapping[str, Any]]] = []
    for position, fact in _fact_rows(document):
        key = _key_of(fact)
        if source_id is not None and key["source_id"] != source_id:
            continue
        if (stage is not None and key["stage"] != stage and
                public_stage(key["stage"]) != stage):
            continue
        if status is not None and _status(fact) != status:
            continue
        if requested_metrics is not None:
            if not requested_metrics.intersection(key["metrics"]):
                continue
        if requested_window is not None:
            window_pair = _window_pair(key["window"])
            if window_pair is None or not _windows_overlap(window_pair,
                                                           requested_window):
                continue
        selected.append((position, fact, key))

    selected.sort(key=lambda entry: _entry_sort_key(entry, requested_window,
                                                    creation))

    if not latest_per_key:
        return [_row(fact, stale_index) for _position, fact, _key in selected]
    return _collapse_latest(selected, stale_index, creation)


# --------------------------------------------------------------------------- #
# changes since a token
# --------------------------------------------------------------------------- #


def _merge_fields(target: list[str], names: list[str]) -> list[str]:
    merged = sorted(set(target) | set(names))
    return merged


def _changed_objects(document: Mapping[str, Any],
                     effective_token: int) -> dict[str, Any]:
    """Object fingerprint changes recorded after ``effective_token``.

    Only ``changes`` entries of kind ``objects`` are evidence: the document
    stores no previous-vs-current fingerprint pair, so with no log entry the
    honest answer is "no recorded change", never a comparison against an
    invented baseline.
    """
    tracks: dict[str, list[str]] = {}
    added: set[str] = set()
    removed: set[str] = set()
    master: list[str] = []
    routing_changed = False

    for entry in document.get("changes") or []:
        if not isinstance(entry, Mapping) or entry.get("kind") != "objects":
            continue
        seq = _integer(entry.get("seq"))
        if seq is None or seq <= effective_token:
            continue
        changed = entry.get("changed")
        if isinstance(changed, Mapping):
            for guid, fields in changed.items():
                name = str(guid)
                names = _string_list(fields)
                if name == "master":
                    master = _merge_fields(master, names)
                else:
                    tracks[name] = _merge_fields(tracks.get(name, []), names)
        for guid in _string_list(entry.get("tracks_updated")):
            if guid not in tracks:
                # The log says the object changed but not which fields moved.
                tracks[guid] = []
        added.update(_string_list(entry.get("tracks_added")))
        removed.update(_string_list(entry.get("tracks_removed")))
        if entry.get("routing_changed") is True:
            routing_changed = True

    return {
        "tracks": {guid: tracks[guid] for guid in sorted(tracks)},
        "added_tracks": sorted(added),
        "removed_tracks": sorted(removed),
        "master": master,
        "routing_changed": routing_changed,
    }


def changes_since(state: Mapping[str, Any], token: int) -> dict[str, Any]:
    """What the document records after ``token``, split by kind.

    ``token`` is a state token previously read from the document.  The result
    has:

    * ``state_token`` — the document's current token;
    * ``new_or_updated`` — rows for fact records created after ``token`` that
      are still current or stale (a re-measurement is a new record);
    * ``now_stale`` — rows for facts that transitioned from current to stale
      after ``token`` (already-stale facts are not re-reported);
    * ``superseded`` — rows for facts that were replaced after ``token``;
    * ``changed_objects`` — ``{tracks: {guid: [fields]}, added_tracks: [...],
      removed_tracks: [...], master: [fields], routing_changed: bool}``
      merged from the ``objects`` change entries after ``token``.

    If ``token`` is newer than the document (the state file was reset or
    replaced), ``reset`` is ``True`` and every retained change is returned, so
    the caller sees a full refresh instead of an empty delta that looks
    current.  Facts with no derivable creation token are never called new.
    """
    document = _require_state(state)
    if isinstance(token, bool) or not isinstance(token, int) or token < 0:
        raise ValidationError("token must be a non-negative integer",
                              details={"token": token})

    current = _token_or_zero(document)
    reset = token > current
    effective = -1 if reset else token

    creation = _creation_index(document)
    stale_index = _stale_index(document)
    supersede_index = _supersede_index(document)
    entries = _fact_rows(document)
    sequences: dict[str, int] = {}
    for _position, fact in entries:
        fact_id = fact.get("id")
        if isinstance(fact_id, str) and fact_id:
            sequences[fact_id] = _fact_sequence(fact, creation)

    new_entries: list[tuple[int, Mapping[str, Any], Mapping[str, Any]]] = []
    stale_entries: list[tuple[int, Mapping[str, Any], Mapping[str, Any]]] = []
    superseded_entries: list[tuple[int, Mapping[str, Any], Mapping[str, Any]]] = []

    for position, fact in entries:
        fact_id = fact.get("id")
        created = sequences.get(fact_id, -1) if isinstance(fact_id, str) else -1
        status = _status(fact)
        entry = (position, fact, _key_of(fact))
        if status == STATUS_SUPERSEDED:
            when = supersede_index.get(fact_id) \
                if isinstance(fact_id, str) else None
            if when is None and created > effective:
                # Created after the token and already superseded: the
                # replacement necessarily also happened after the token.
                when = created
            if when is not None and when > effective:
                superseded_entries.append(entry)
            continue
        if created > effective:
            new_entries.append(entry)
            continue
        if status == STATUS_STALE:
            transition = stale_index.get(fact_id) \
                if isinstance(fact_id, str) else None
            if transition is not None and transition[0] > effective:
                stale_entries.append(entry)

    def order(rows: list[tuple[int, Mapping[str, Any], Mapping[str, Any]]]
              ) -> list[tuple[int, Mapping[str, Any], Mapping[str, Any]]]:
        return sorted(rows,
                      key=lambda entry: _entry_sort_key(entry, None, creation))

    result: dict[str, Any] = {
        "state_token": current,
        "new_or_updated": [_row(fact, stale_index)
                           for _position, fact, _key in order(new_entries)],
        "now_stale": [_row(fact, stale_index)
                      for _position, fact, _key in order(stale_entries)],
        "superseded": [_row(fact, stale_index)
                       for _position, fact, _key in order(superseded_entries)],
        "changed_objects": _changed_objects(document, effective),
    }
    if reset:
        result["reset"] = True
    return result


# --------------------------------------------------------------------------- #
# availability
# --------------------------------------------------------------------------- #


def availability(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Whether a state document exists and its fact counts by status.

    ``None`` means no document was loaded (``present=False``, token ``None``).
    An empty document is still ``present=True`` with zero facts: presence is a
    fact about the read, not about how much was measured.  ``state_token`` is
    ``None`` when the document carries no valid token; it is never invented as
    ``0``.
    """
    if state is None:
        return {
            "present": False,
            "facts": 0,
            "current": 0,
            "stale": 0,
            "superseded": 0,
            "state_token": None,
        }
    document = _require_state(state)
    counts = {name: 0 for name in FACT_STATUSES}
    total = 0
    for _position, fact in _fact_rows(document):
        total += 1
        status = _status(fact)
        if status in counts:
            counts[status] += 1
    return {
        "present": True,
        "facts": total,
        "current": counts[STATUS_CURRENT],
        "stale": counts[STATUS_STALE],
        "superseded": counts[STATUS_SUPERSEDED],
        "state_token": _token_or_none(document),
    }
