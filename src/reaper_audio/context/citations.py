"""Proof-of-citation — Context Contract v2 §4.7.

An edit or a claim that rests on measurements must carry ``basis``: ids of
evidence units, each with the exact value it is citing.  The server looks the
unit up and compares the number.  Citing something that was never measured is
impossible; citing a number the server did not produce is refused.

The check is about *possession of data*, not comprehension: it proves that the
cited numbers were available and are the stored ones.  It never claims the
caller understood or used them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .evidence import EvidenceStore


def _same_number(left: Any, right: Any, tolerance: float) -> bool:
    try:
        return abs(float(left) - float(right)) <= tolerance
    except (TypeError, ValueError):
        return False


def verify_citations(store: EvidenceStore,
                     basis: Sequence[Mapping[str, Any]],
                     *, tolerance: float = 0.0) -> dict[str, Any]:
    """Verify every cited (unit id, metric, value) against the store.

    ``tolerance`` is passed by the caller; the default 0.0 means exact
    equality of the stored float.  A non-zero tolerance is an explicit choice
    of the caller and is echoed in the result.
    """
    if not isinstance(basis, Sequence) or isinstance(basis, (str, bytes)):
        raise ValueError("basis must be a list of citations")
    checked: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for index, citation in enumerate(basis):
        if not isinstance(citation, Mapping):
            failed.append({"index": index, "reason": "citation_is_not_an_object"})
            continue
        identifier = citation.get("unit_id") or citation.get("id")
        if not isinstance(identifier, str) or not identifier:
            failed.append({"index": index, "reason": "missing_unit_id"})
            continue
        unit = store.get(identifier)
        if unit is None:
            failed.append({"index": index, "unit_id": identifier,
                           "reason": "unit_not_found"})
            continue
        metric = citation.get("metric") or unit.get("metric")
        if metric != unit.get("metric"):
            failed.append({"index": index, "unit_id": identifier,
                           "reason": "metric_mismatch",
                           "cited": metric, "stored": unit.get("metric")})
            continue
        value = citation.get("value")
        if value is None:
            checked.append({"unit_id": identifier, "metric": metric,
                            "value": unit.get("value"), "compared": False})
            continue
        if not _same_number(value, unit.get("value"), tolerance):
            failed.append({"index": index, "unit_id": identifier,
                           "reason": "value_mismatch",
                           "cited": value, "stored": unit.get("value")})
            continue
        checked.append({"unit_id": identifier, "metric": metric,
                        "value": unit.get("value"), "compared": True,
                        "source_id": unit.get("source_id"),
                        "stage": unit.get("stage"),
                        "interval": unit.get("interval")})
    return {
        "ok": not failed,
        "checked": checked,
        "failed": failed,
        "tolerance": tolerance,
        "basis_size": len(basis),
    }


__all__ = ["verify_citations"]
