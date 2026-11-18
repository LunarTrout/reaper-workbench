"""Decision gates — Context Contract v2 §4.13 / Stage C2–C4, E4.

The caller declares what it is doing (``local``, ``pairwise``, ``global``) and
which stored evidence it relies on (``basis``).  The server checks both against
the archive it minted:

* class: the evidence must cover what the class claims.  ``local`` needs the
  target source, ``pairwise`` needs both sides, ``global`` needs a complete
  coverage passport over the whole project *and* intervals that cover the
  claimed time span;
* promotion only: an operation that touches the master, a folder bus or the
  whole project is *raised* to ``global`` by the server, never lowered; a stale
  share above the configured threshold raises it too, but softly — it asks for a
  full pass without invalidating a local claim its own evidence backs;
* basis: every cited ``evidence_id`` must exist and carry exactly the value the
  caller quoted (float comparison at representation tolerance, which is not a
  quality threshold);
* blockers: at most three, each verifiable and removable with one action.

Nothing here measures audio, guesses intent or scores a mix.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .contract_v2 import stale_fraction_threshold as configured_stale_threshold

DECISION_CLASSES = ("local", "pairwise", "global")
_RANK = {"local": 0, "pairwise": 1, "global": 2}
MAX_BLOCKERS = 3
# Float representation tolerance when comparing a quoted value with the stored
# one.  It exists because 44.1 and 44.10000000000001 are the same measurement,
# not because the server tolerates a "close enough" citation.
VALUE_REL_TOL = 1e-9
VALUE_ABS_TOL = 1e-12
_USE_CONFIG = object()


def _blocker(code: str, message: str, resolution: str,
             **evidence: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "resolution": resolution,
            "evidence": {key: value for key, value in evidence.items()
                         if value is not None}}


def _same_value(quoted: Any, stored: Any) -> bool:
    if isinstance(quoted, bool) or isinstance(stored, bool):
        return quoted == stored
    if isinstance(quoted, (int, float)) and isinstance(stored, (int, float)):
        return math.isclose(float(quoted), float(stored),
                            rel_tol=VALUE_REL_TOL, abs_tol=VALUE_ABS_TOL)
    return quoted == stored


def raise_class(decision_class: str, reasons: Sequence[str]) -> tuple[str, list[str]]:
    """Apply server-side promotions; returns (class, promoted_reasons)."""
    if decision_class not in DECISION_CLASSES:
        raise ValueError(f"decision_class must be one of {DECISION_CLASSES}")
    if not reasons:
        return decision_class, []
    target = decision_class
    for reason in reasons:
        needed = _RANK[reason] if reason in _RANK else _RANK["global"]
        if needed > _RANK[target]:
            target = reason
    promoted = ([reason for reason in reasons
                 if _RANK.get(reason, _RANK["global"]) > _RANK[decision_class]]
                if target != decision_class else [])
    return target, promoted


def verify_basis(declared: Sequence[Mapping[str, Any]],
                 units: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Every quoted evidence value must match the stored unit exactly."""
    blockers: list[dict[str, Any]] = []
    for item in declared:
        evidence_id = str(item.get("evidence_id") or "")
        if not evidence_id:
            blockers.append(_blocker(
                "basis_malformed", "a basis entry has no evidence_id",
                "cite evidence_id plus the metric/value you read from it"))
            continue
        unit = units.get(evidence_id)
        if unit is None:
            blockers.append(_blocker(
                "basis_unknown", f"evidence {evidence_id} is not in this archive",
                "re-measure that source or drop the citation"))
            continue
        if "value" in item:
            if not _same_value(item.get("value"), unit.get("value")):
                blockers.append(_blocker(
                    "basis_mismatch",
                    f"quoted {item.get('value')!r} but the stored value of "
                    f"{evidence_id} is {unit.get('value')!r}",
                    "re-read the value from the evidence unit and cite it exactly",
                    evidence_id=evidence_id, quoted=item.get("value"),
                    stored=unit.get("value")))
            continue
        metric = item.get("metric")
        if metric is not None and unit.get("metric") != metric:
            blockers.append(_blocker(
                "basis_mismatch",
                f"evidence {evidence_id} measures {unit.get('metric')!r}, not {metric!r}",
                "cite the evidence unit that carries the metric you name",
                evidence_id=evidence_id, stored_metric=unit.get("metric")))
    return blockers


def _length_blocker(length: Mapping[str, Any]) -> dict[str, Any]:
    uncovered = list(length.get("uncovered") or [])
    example = uncovered[0] if uncovered else {}
    gaps = list(example.get("gaps") or [])
    missing = (f"{gaps[0]['start_s']}-{gaps[0]['end_s']} s" if gaps
               else "part of the region")
    return _blocker(
        "coverage_incomplete_length",
        (f"a global claim needs the measured intervals to cover "
         f"{length.get('required_s')} s, but {len(uncovered)} source(s) miss "
         f"e.g. {missing}"),
        ("measure the missing span, or narrow the claim to the measured "
         "intervals and stop calling it final"),
        required_s=length.get("required_s"),
        uncovered_sources=len(uncovered), example=example)


def coverage_blockers(decision_class: str,
                      target_sources: Sequence[str],
                      coverage: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Does the passport cover what the declared class claims?"""
    coverage = coverage or {}
    measured = {str(item) for item in coverage.get("measured_sources") or []}
    total = coverage.get("total_sources")
    sources_measured = coverage.get("sources_measured")
    blockers: list[dict[str, Any]] = []
    if decision_class == "global":
        length = coverage.get("length") or {}
        if length and not length.get("complete", True):
            blockers.append(_length_blocker(length))
        if not coverage.get("complete"):
            blockers.append(_blocker(
                "coverage_incomplete",
                (f"a global claim needs every source measured; this passport has "
                 f"{sources_measured}/{total}"),
                "run a full-length capture of the remaining sources",
                sources_measured=sources_measured, total_sources=total,
                missing=list(coverage.get("missing_sources") or [])[:8]))
        return blockers
    missing = [source for source in target_sources if source not in measured]
    if missing:
        blockers.append(_blocker(
            "coverage_missing_target",
            (f"{decision_class} claim names sources that this passport does not "
             f"measure: {', '.join(missing[:4])}"),
            "measure those sources over the interval you are claiming",
            missing=missing[:8], sources_measured=sources_measured, total_sources=total))
    return blockers


def evaluate(*, requested_class: str,
             target_sources: Sequence[str] = (),
             master_touched: bool = False,
             bus_touched: bool = False,
             whole_project: bool = False,
             basis: Sequence[Mapping[str, Any]] | None = None,
             units: Mapping[str, Mapping[str, Any]] | None = None,
             coverage: Mapping[str, Any] | None = None,
             stale_fraction: float | None = None,
             stale_fraction_threshold: float | None | object = _USE_CONFIG) -> dict[str, Any]:
    """Decide whether the declared class is backed by the archive."""
    raw_threshold = (configured_stale_threshold()
                     if stale_fraction_threshold is _USE_CONFIG
                     else stale_fraction_threshold)
    threshold: float | None = (float(raw_threshold)
                               if isinstance(raw_threshold, (int, float)) else None)
    hard_global = (requested_class == "global" or master_touched
                   or bus_touched or whole_project)
    promotions: list[str] = []
    if master_touched:
        promotions.append("global")
    if bus_touched:
        promotions.append("global")
    if whole_project:
        promotions.append("global")
    if (stale_fraction is not None and threshold is not None
            and stale_fraction > threshold):
        promotions.append("global")
    decision_class, promoted = raise_class(requested_class, promotions)

    # A share-driven promotion asks for a full pass; it does not retroactively
    # invalidate a local claim that its own evidence backs.  Only a claim that
    # is global by construction (declared global, master, bus, whole project)
    # has to present a complete passport and length coverage.
    coverage_class = "global" if hard_global else requested_class
    blockers: list[dict[str, Any]] = []
    blockers.extend(verify_basis(list(basis or []), dict(units or {})))
    blockers.extend(coverage_blockers(coverage_class, list(target_sources), coverage))
    return {
        "schema": "reaper-workbench.decision-gate/1",
        "requested_class": requested_class,
        "decision_class": decision_class,
        "promoted": promoted,
        "requires_full_pass": ("global" in promoted and not hard_global),
        "stale_fraction": stale_fraction,
        "stale_fraction_threshold": threshold,
        "blockers": blockers[:MAX_BLOCKERS],
        "blockers_truncated": len(blockers) > MAX_BLOCKERS,
        "ok": not blockers,
    }


__all__ = ["DECISION_CLASSES", "MAX_BLOCKERS", "coverage_blockers", "evaluate",
           "raise_class", "verify_basis"]
