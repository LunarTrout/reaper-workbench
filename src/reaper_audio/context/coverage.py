"""Coverage passport — Context Contract v2 §4.6.

A passport can only be minted *from facts*: which sources were measured, over
which intervals, at which revisions, and what is missing.  It is the object a
``global`` claim must present; the server re-derives it and compares.

The module deliberately has no notion of a "good" coverage: it reports numbers
and gaps.  Whether 12/42 is enough is not its question — the contract says a
global claim needs full coverage, and a partial one is refused with the counts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

PASSPORT_SCHEMA = "reaper-workbench.coverage-passport/1"


def build_passport(*, scope: str,
                   measured: Iterable[str],
                   total: Iterable[str],
                   intervals: Mapping[str, Sequence[Mapping[str, float]]],
                   revisions: Mapping[str, Any],
                   gaps: Sequence[Mapping[str, Any]] | None = None,
                   gaps_declared_by: str = "server") -> dict[str, Any]:
    """Assemble a passport from measured facts.

    ``measured``/``total`` are source ids; ``intervals`` is what each measured
    source actually covers.  A source listed in ``measured`` with no interval
    is a gap, not a covered source.
    """
    measured_list = sorted({str(item) for item in measured})
    total_list = sorted({str(item) for item in total})
    covered = [source for source in measured_list
               if intervals.get(source)]
    missing = [source for source in total_list if source not in covered]
    declared_gaps = list(gaps or [])
    for source in measured_list:
        if source in covered:
            continue
        declared_gaps.append({"source_id": source, "reason": "no_interval_measured"})
    for source in missing:
        if source in measured_list:
            continue
        declared_gaps.append({"source_id": source, "reason": "not_measured"})
    return {
        "schema": PASSPORT_SCHEMA,
        "scope": str(scope),
        "sources_total": len(total_list),
        "sources_measured": len(covered),
        "coverage_fraction": (len(covered) / len(total_list)) if total_list else None,
        "sources": {"measured": covered, "missing": missing},
        "intervals": {source: [dict(row) for row in intervals.get(source) or []]
                      for source in covered},
        "revisions": dict(revisions),
        "gaps": declared_gaps,
        "gaps_declared_by": str(gaps_declared_by),
        "complete": not declared_gaps,
    }


def require_complete(passport: Mapping[str, Any], *,
                     minimum_sources: int | None = None) -> None:
    """Raise ``CoverageIncomplete`` unless the passport covers its scope.

    ``minimum_sources`` lets a caller require more than "all listed sources"
    (for example, a global claim needs every project source); it is a declared
    argument, never a hidden threshold.
    """
    from ..errors import ValidationError

    measured = int(passport.get("sources_measured") or 0)
    total = int(passport.get("sources_total") or 0)
    gaps = list(passport.get("gaps") or [])
    if gaps or measured != total or total == 0:
        raise ValidationError(
            "The coverage passport does not cover the requested scope",
            details={"sources_measured": measured, "sources_total": total,
                     "gaps": gaps[:8], "gap_count": len(gaps),
                     "scope": passport.get("scope")})
    if minimum_sources is not None and measured < int(minimum_sources):
        raise ValidationError(
            "The coverage passport covers fewer sources than the claim requires",
            details={"sources_measured": measured,
                     "required": int(minimum_sources)})


#: Spans closer than this are treated as touching.  It absorbs float
#: representation (14.999999999 vs 15.0), not a real audio gap.
SPAN_EPSILON_S = 1e-6


def _merge_spans(spans: Sequence[Mapping[str, float]]) -> list[tuple[float, float]]:
    ordered = sorted((float(span.get("start_s", 0.0)), float(span.get("end_s", 0.0)))
                     for span in spans if float(span.get("end_s", 0.0)) > float(span.get("start_s", 0.0)))
    merged: list[tuple[float, float]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1] + SPAN_EPSILON_S:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def uncovered_spans(intervals: Sequence[Mapping[str, float]],
                    required: Sequence[Mapping[str, float]]) -> list[dict[str, float]]:
    """Parts of ``required`` that ``intervals`` do not cover, in seconds."""
    merged = _merge_spans(intervals)
    gaps: list[dict[str, float]] = []
    for region in required:
        start = float(region.get("start_s", 0.0))
        end = float(region.get("end_s", 0.0))
        if end <= start:
            continue
        cursor = start
        for span_start, span_end in merged:
            if span_end <= cursor + SPAN_EPSILON_S:
                continue
            if span_start >= end - SPAN_EPSILON_S:
                break
            if span_start > cursor + SPAN_EPSILON_S:
                gaps.append({"start_s": round(cursor, 6),
                             "end_s": round(min(span_start, end), 6)})
            cursor = max(cursor, span_end)
            if cursor >= end - SPAN_EPSILON_S:
                break
        if cursor < end - SPAN_EPSILON_S:
            gaps.append({"start_s": round(cursor, 6), "end_s": round(end, 6)})
    return gaps


def length_coverage(units: Iterable[Mapping[str, Any]], *,
                    project_id: str,
                    required: Sequence[Mapping[str, float]],
                    sources: Sequence[str]) -> dict[str, Any]:
    """Time coverage per source from every stored unit, whatever its revision.

    Used to explain a length gap in the clearest terms: the passport may count
    zero *current* sources (a short-window render key never equals the
    full-length one), yet the caller still needs to be told "you measured 0-5 s
    of 15 s" rather than only "0/8 sources".
    """
    intervals: dict[str, list[dict[str, float]]] = {}
    for unit in units:
        if _normalise_project_id(unit.get("project_id")) != _normalise_project_id(project_id):
            continue
        source = str(unit.get("source_id") or "")
        interval = unit.get("interval") or {}
        if not source or not interval:
            continue
        row = {"start_s": float(interval.get("start_s", 0.0)),
               "end_s": float(interval.get("end_s", 0.0))}
        intervals.setdefault(source, [])
        if row not in intervals[source]:
            intervals[source].append(row)
    uncovered: list[dict[str, Any]] = []
    for source in sources:
        gaps = uncovered_spans(intervals.get(source) or [], required)
        if gaps:
            uncovered.append({"source_id": source, "gaps": gaps[:6],
                              "gap_count": len(gaps)})
    required_s = sum(float(row.get("end_s", 0.0)) - float(row.get("start_s", 0.0))
                     for row in required)
    return {"required": [dict(row) for row in required],
            "required_s": round(required_s, 6),
            "uncovered": uncovered, "complete": not uncovered}


def interval_coverage(passport: Mapping[str, Any], *,
                      required: Sequence[Mapping[str, float]],
                      sources: Sequence[str] | None = None) -> dict[str, Any]:
    """Do the measured intervals cover the claimed time span?

    The passport's ``complete`` flag only says every source has *some* interval.
    A ``global`` or final claim also has to cover the declared region: a 15 s
    window on a 12-minute project is not a project-wide verdict, however many
    sources it touched.
    """
    intervals = passport.get("intervals") or {}
    measured = list(passport.get("sources", {}).get("measured") or [])
    targets = list(sources) if sources is not None else measured
    uncovered: list[dict[str, Any]] = []
    for source in targets:
        gaps = uncovered_spans(intervals.get(source) or [], required)
        if gaps:
            covered_s = sum(end - start for start, end
                            in _merge_spans(intervals.get(source) or []))
            uncovered.append({
                "source_id": source,
                "gaps": gaps[:6],
                "gap_count": len(gaps),
                "measured_s": round(covered_s, 6),
            })
    required_s = sum(float(row.get("end_s", 0.0)) - float(row.get("start_s", 0.0))
                     for row in required)
    return {
        "required": [dict(row) for row in required],
        "required_s": round(required_s, 6),
        "uncovered": uncovered,
        "complete": not uncovered,
    }


def _normalise_project_id(value: Any) -> str:
    """Project ids embed a path; the bridge and the archive may disagree on
    separators, which must never turn a measured source into a gap."""
    return str(value or "").replace("\\", "/")


def passport_from_units(units: Iterable[Mapping[str, Any]], *,
                        scope: str,
                        project_id: str,
                        total_sources: Iterable[str],
                        revision: Mapping[str, Any],
                        current_fingerprints: Mapping[str, Mapping[str, str]] | None = None
                        ) -> dict[str, Any]:
    """Build a passport from stored evidence units of one project revision.

    A unit counts as measured when its ``project_id`` matches and either

    * ``current_fingerprints`` is supplied and the unit's render fingerprint is
      one of that source's current fingerprints (the honest test — a measure
      call bumps ``change_count`` by itself, so revision equality would make a
      complete passport impossible), or
    * otherwise its revision fields equal the requested revision.

    Everything else is a gap, named.
    """
    wanted = {key: revision.get(key) for key in ("change_count", "epoch", "instance_id")}
    measured: dict[str, list[dict[str, float]]] = {}
    for unit in units:
        if _normalise_project_id(unit.get("project_id")) != _normalise_project_id(project_id):
            continue
        source = str(unit.get("source_id") or "")
        if current_fingerprints is not None:
            expected = set((current_fingerprints.get(source) or {}).values())
            if not expected or unit.get("fingerprint") not in expected:
                continue
        else:
            unit_revision = unit.get("revision") or {}
            if any(unit_revision.get(key) != value for key, value in wanted.items()):
                continue
        interval = unit.get("interval") or {}
        if not source or not interval:
            continue
        row = {"start_s": float(interval.get("start_s", 0.0)),
               "end_s": float(interval.get("end_s", 0.0))}
        if row not in measured.setdefault(source, []):
            measured[source].append(row)
    passport = build_passport(scope=scope, measured=list(measured),
                              total=total_sources, intervals=measured,
                              revisions=dict(wanted), gaps_declared_by="server")
    passport["source"] = "legacy"
    return passport


def passport_from_state(state: object, *, scope: str, project_id: str,
                        total_sources: Sequence[str] | None = None) -> dict[str, Any]:
    """Build a coverage passport from the facts stored in project state.

    ``measured`` sources are those with at least one ``current`` fact carrying a
    real window; ``total_sources`` is the caller's live source list, defaulting
    to the sources the state objects know about.  The result is the legacy
    passport shape plus three additive keys: ``source`` (``"state"``),
    ``state_token`` and ``counters`` (the full :func:`state_coverage` answer).
    A state with no facts or no objects is marked incomplete with an explicit
    gap; it never reads as a complete passport by having nothing to check.
    """
    from .state_reads import state_coverage

    counters = state_coverage(state)
    intervals = {source: [dict(row) for row in rows]
                 for source, rows in (counters.get("intervals") or {}).items()
                 if rows}
    measured = sorted(intervals)
    if total_sources is not None:
        total = sorted({str(item) for item in total_sources})
    else:
        total = list((counters.get("sources") or {}).get("all") or [])
    gaps: list[dict[str, Any]] = []
    if not counters.get("facts_total"):
        gaps.append({"reason": "state_has_no_facts"})
    if not total:
        gaps.append({"reason": "state_has_no_objects"})
    passport = build_passport(scope=scope, measured=measured, total=total,
                              intervals=intervals,
                              revisions={"state_token": counters.get("state_token")},
                              gaps=gaps, gaps_declared_by="state")
    passport["source"] = "state"
    passport["project_id"] = str(project_id)
    passport["state_token"] = counters.get("state_token")
    passport["counters"] = counters
    return passport


def coverage_report(*, state: object = None,
                    units: Iterable[Mapping[str, Any]] = (),
                    scope: str = "global",
                    project_id: str = "",
                    total_sources: Sequence[str] | None = None,
                    revision: Mapping[str, Any] | None = None,
                    current_fingerprints: Mapping[str, Mapping[str, str]] | None = None
                    ) -> dict[str, Any]:
    """Coverage passport, preferring the project state when one exists.

    With ``state`` the answer comes from stored facts and is marked
    ``"source": "state"``.  Without it the legacy evidence-unit passport is
    built and marked ``"source": "legacy"``.  ``units``/``revision`` are only
    read on the legacy path; nothing is invented when both are empty (every
    listed source becomes a named gap).
    """
    if state is not None:
        return passport_from_state(state, scope=scope, project_id=project_id,
                                   total_sources=total_sources)
    passport = passport_from_units(units, scope=scope, project_id=project_id,
                                   total_sources=total_sources or (),
                                   revision=dict(revision or {}),
                                   current_fingerprints=current_fingerprints)
    passport["source"] = "legacy"
    return passport


__all__ = [
    "SPAN_EPSILON_S",
    "interval_coverage",
    "uncovered_spans", "PASSPORT_SCHEMA", "build_passport", "coverage_report",
    "passport_from_state", "passport_from_units", "require_complete"]
