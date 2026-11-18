"""Decision classes and server-side escalation — Context Contract v2 §4.5/§4.13.

The caller declares what it is deciding (``local``, ``pairwise`` or
``global``).  The server may **raise** the class and never lowers it:

* a target that is the master, a bus (folder parent), or a routing hub makes
  the decision global, because every stem passes through it;
* more than two distinct sources cannot be a pairwise decision;
* an explicit ``global`` stays global.

A full pass is then a *server* decision with four declared triggers:

1. the (possibly raised) class is ``global``;
2. the operation is a save/export/final verdict;
3. no complete coverage passport can be minted for the requested class;
4. the share of stale sources exceeds a threshold from configuration.

There is no default for (4): if the configuration value is absent, the rule is
inert and the reason is reported as such.  Nothing here knows about bars,
sections or song structure.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

CLASSES = ("local", "pairwise", "global")
_RANK = {name: index for index, name in enumerate(CLASSES)}

#: How many distinct sources a declared class may touch before the server
#: raises it.  This is vocabulary, not taste: ``pairwise`` means exactly two.
CLASS_SOURCE_LIMIT = {"local": 1, "pairwise": 2}


def stale_fraction_threshold() -> float | None:
    raw = os.environ.get("REAPER_V2_STALE_FRACTION_THRESHOLD")
    if raw is None or raw.strip() == "":
        return None
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        raise ValueError("REAPER_V2_STALE_FRACTION_THRESHOLD must be within [0, 1]")
    return value


def _raise_class(current: str, candidate: str) -> str:
    return candidate if _RANK[candidate] > _RANK[current] else current


def resolve_class(declared: str | None, *,
                  sources: Iterable[str],
                  touches_master: bool = False,
                  touches_bus: bool = False) -> dict[str, Any]:
    """Return the effective class and the reasons the server raised it."""
    if declared is None:
        declared = "local"
    if declared not in CLASSES:
        raise ValueError(f"decision_class must be one of {CLASSES}")
    effective = declared
    reasons: list[str] = []
    if touches_master:
        effective = _raise_class(effective, "global")
        reasons.append("targets_master_chain")
    if touches_bus:
        effective = _raise_class(effective, "global")
        reasons.append("targets_bus_or_routing_hub")
    source_set = sorted({str(item) for item in sources if str(item)})
    limit = CLASS_SOURCE_LIMIT.get(declared)
    if limit is not None and len(source_set) > limit:
        effective = _raise_class(effective, "global")
        reasons.append(f"{len(source_set)}_sources_exceed_{declared}_scope")
    return {
        "declared": declared,
        "effective": effective,
        "raised": effective != declared,
        "reasons": reasons,
        "sources": source_set,
        "source_count": len(source_set),
    }


def full_pass_reasons(*, effective_class: str,
                      is_save: bool = False,
                      passport_complete: bool | None = None,
                      stale_fraction: float | None = None) -> dict[str, Any]:
    """Server decision: must this operation be preceded by a full pass?"""
    reasons: list[str] = []
    if effective_class == "global":
        reasons.append("decision_class_global")
    if is_save:
        reasons.append("save_or_export")
    if passport_complete is False:
        reasons.append("coverage_passport_cannot_be_minted")
    threshold = stale_fraction_threshold()
    threshold_applied = None
    if threshold is not None and stale_fraction is not None:
        threshold_applied = threshold
        if stale_fraction > threshold:
            reasons.append("stale_fraction_above_configured_threshold")
    return {
        "required": bool(reasons),
        "reasons": reasons,
        "stale_fraction": stale_fraction,
        "stale_threshold": threshold_applied,
        "threshold_configured": threshold is not None,
        "effective_class": effective_class,
    }


__all__ = ["CLASSES", "CLASS_SOURCE_LIMIT", "full_pass_reasons",
           "resolve_class", "stale_fraction_threshold"]
