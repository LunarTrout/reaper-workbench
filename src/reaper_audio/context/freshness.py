"""Freshness per (source, stage) — Context Contract v2 §4.3 / §4.3a.

Freshness is a *derived* map, never an independent scalar:

    state[source_id][stage] = {
        state: "current" | "stale" | "unknown",
        fingerprint, capture_id, revision, intervals, reason,
    }

Rules that this module enforces, and nothing else:

* ``current`` requires an equal, *complete* fingerprint and a capture that
  still belongs to the same project revision lineage;
* a changed fingerprint is ``stale`` with the old and new fingerprints kept;
* an incomplete/absent fingerprint is ``unknown`` — never a hit, never "no
  change" (R3);
* a stage that was not rendered is declared ``unknown`` with reason
  ``stage_not_rendered``: absence is stated, never rendered as zero (§2.3);
* ``premaster`` and ``via_master`` are never compared to each other; a
  measurement on one stage says nothing about the other (§4.3a).

No threshold, no time window and no musical knowledge lives here: intervals
come from the caller and are carried through untouched.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

STAGE_PREMASTER = "premaster"
STAGE_VIA_MASTER = "via_master"
STAGES = (STAGE_PREMASTER, STAGE_VIA_MASTER)

#: Schema id of the freshness answer (legacy and state-backed alike).
FRESHNESS_SCHEMA = "reaper-workbench.freshness/1"

STATE_CURRENT = "current"
STATE_STALE = "stale"
STATE_UNKNOWN = "unknown"

_ABSENT = object()


def _sources_from_manifest(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    project_map = record.get("project_map")
    if not isinstance(project_map, Mapping):
        return []
    manifest = project_map.get("render_manifest")
    if not isinstance(manifest, Mapping):
        return []
    rows = manifest.get("sources")
    if not isinstance(rows, list):
        return []
    normalized: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        item = dict(row)
        if not item.get("source_id"):
            guid = item.get("source_guid")
            scope = str(item.get("scope") or "source")
            if isinstance(guid, str) and guid:
                item["source_id"] = (f"master:{guid}" if scope == "master"
                                     else f"track:{guid}")
        normalized.append(item)
    return normalized


def stage_of(row: Mapping[str, Any]) -> str:
    """Map a captured signal stage onto the contract's two-stage vocabulary."""
    raw = str(row.get("stage") or "")
    if raw == "master_mix_post_fx" or raw.endswith("via_master"):
        return STAGE_VIA_MASTER
    if raw == "premaster" or raw.endswith("premaster"):
        return STAGE_PREMASTER
    # Every other captured stage today is "selected track via master".
    return STAGE_VIA_MASTER


def build_freshness(record: Mapping[str, Any] | None = None, *,
                    current_fingerprints: Mapping[str, Mapping[str, str]] | None = None,
                    revision: Mapping[str, Any] | None = None,
                    capture_id: str | None = None,
                    intervals: Mapping[str, Sequence[Mapping[str, float]]] | None = None,
                    state: object = None,
                    live_fingerprints: Mapping[str, Any] | None = None,
                    ) -> dict[str, Any]:
    """Build the per-source, per-stage freshness map.

    Two answer paths, both marked by the top-level ``source`` key:

    * ``state`` — when a project state document is supplied, the stored fact
      fingerprints are compared with ``live_fingerprints`` by
      :func:`reaper_audio.context.state_reads.state_freshness`.  ``record`` is
      then unused.
    * ``legacy`` — without state, the capture record's render manifest is
      compared with ``current_fingerprints`` as before.

    ``current_fingerprints`` is ``{source_id: {stage: render_key}}`` computed
    from the *current* project digest.  ``None`` means the current state could
    not be read at all: every source becomes ``unknown``.  With neither a state
    nor a record the answer is an explicit empty ``legacy`` report with reason
    ``no_capture_record`` — never a silent "clean" result.
    """
    if state is not None:
        from .state_reads import state_freshness

        return state_freshness(state, live_fingerprints)
    if record is None:
        return {
            "schema": FRESHNESS_SCHEMA,
            "source": "legacy",
            "capture_id": None,
            "revision": {},
            "stages": list(STAGES),
            "sources": {},
            "summary": summarise({}),
            "reason": "no_capture_record",
        }
    capture_id = capture_id or str(record.get("capture_id") or "")
    revision_row = dict(revision or record.get("revision_after") or {})
    recorded = {row.get("source_id"): row for row in _sources_from_manifest(record)
                if isinstance(row.get("source_id"), str)}

    sources: dict[str, dict[str, Any]] = {}
    for source_id, row in recorded.items():
        if not isinstance(source_id, str) or not source_id:
            continue
        stage = stage_of(row)
        recorded_key = row.get("render_key")
        complete = bool(row.get("complete"))
        entry: dict[str, Any] = {
            "fingerprint": recorded_key,
            "complete": complete,
            "capture_id": capture_id,
            "revision": {key: revision_row.get(key)
                         for key in ("change_count", "epoch", "instance_id")},
            "intervals": list((intervals or {}).get(source_id) or []),
        }
        if not complete or not isinstance(recorded_key, str) or not recorded_key:
            entry.update(state=STATE_UNKNOWN, reason="fingerprint_incomplete")
        elif current_fingerprints is None:
            entry.update(state=STATE_UNKNOWN, reason="current_state_unreadable")
        else:
            current_key = (current_fingerprints.get(source_id) or {}).get(stage)
            if not isinstance(current_key, str) or not current_key:
                entry.update(state=STATE_UNKNOWN, reason="current_fingerprint_missing")
            elif current_key == recorded_key:
                entry.update(state=STATE_CURRENT, reason=None)
            else:
                entry.update(state=STATE_STALE, reason="fingerprint_changed",
                             previous_fingerprint=recorded_key,
                             current_fingerprint=current_key)
        sources[source_id] = {stage: entry}

    return {
        "schema": FRESHNESS_SCHEMA,
        "source": "legacy",
        "capture_id": capture_id,
        "revision": revision_row,
        "stages": list(STAGES),
        "sources": sources,
        "summary": summarise(sources),
    }


def summarise(sources: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> dict[str, Any]:
    """Derive the global picture from the map; never a standalone field."""
    counts = {STATE_CURRENT: 0, STATE_STALE: 0, STATE_UNKNOWN: 0}
    stale_sources: list[str] = []
    unknown_sources: list[str] = []
    for source_id, stages in sources.items():
        states = [row.get("state") for row in stages.values()]
        if STATE_STALE in states:
            counts[STATE_STALE] += 1
            stale_sources.append(source_id)
        elif STATE_UNKNOWN in states or not states:
            counts[STATE_UNKNOWN] += 1
            unknown_sources.append(source_id)
        else:
            counts[STATE_CURRENT] += 1
    total = sum(counts.values())
    return {
        "sources_total": total,
        "sources_current": counts[STATE_CURRENT],
        "sources_stale": counts[STATE_STALE],
        "sources_unknown": counts[STATE_UNKNOWN],
        "stale_sources": stale_sources,
        "unknown_sources": unknown_sources,
        "stale_fraction": (counts[STATE_STALE] / total) if total else None,
    }


def states_by_source(freshness: Mapping[str, Any]) -> dict[str, str]:
    """One state per source: the worst state across its stages."""
    order = {STATE_CURRENT: 0, STATE_STALE: 1, STATE_UNKNOWN: 2}
    result: dict[str, str] = {}
    for source_id, stages in (freshness.get("sources") or {}).items():
        worst = STATE_CURRENT
        for row in stages.values():
            state = row.get("state")
            if order.get(state, 2) > order[worst]:
                worst = state if state in order else STATE_UNKNOWN
        result[source_id] = worst
    return result


def current_sources(freshness: Mapping[str, Any], *, stage: str | None = None) -> list[str]:
    """Sources that are ``current`` (optionally for one stage only)."""
    out: list[str] = []
    for source_id, stages in (freshness.get("sources") or {}).items():
        rows = [stages[stage]] if stage in stages else list(stages.values())
        if rows and all(row.get("state") == STATE_CURRENT for row in rows):
            out.append(source_id)
    return sorted(out)


__all__ = [
    "FRESHNESS_SCHEMA",
    "STAGES",
    "STAGE_PREMASTER",
    "STAGE_VIA_MASTER",
    "STATE_CURRENT",
    "STATE_STALE",
    "STATE_UNKNOWN",
    "build_freshness",
    "current_sources",
    "stage_of",
    "states_by_source",
    "summarise",
]
