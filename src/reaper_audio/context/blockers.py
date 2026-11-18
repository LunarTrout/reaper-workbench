"""Blockers instead of a wall — Context Contract v2 §4.8.

A blocker is something the *server* verified and can name a single remedy for.
The list is capped at three: a caller who needs more than three independent
fixes is not in a state where one more sentence helps.

No item here is a suggestion, a style note or a score.  If the server cannot
check it, it does not belong in the list.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

MAX_BLOCKERS = 3


def blocker(code: str, *, reason: str, remedy: str, details: Mapping[str, Any] | None = None,
            ) -> dict[str, Any]:
    return {"code": str(code), "reason": str(reason), "remedy": str(remedy),
            "details": dict(details or {})}


def collect(checks: Iterable[Mapping[str, Any]], *, limit: int = MAX_BLOCKERS,
            ) -> dict[str, Any]:
    """Keep the first ``limit`` verified blockers and count the rest.

    ``checks`` entries carry ``blocking: bool`` plus the fields of
    :func:`blocker`.  Ordering is the caller's: it encodes what must be fixed
    first.  The response never hides how many were dropped: ``suppressed``
    counts them, so "three shown" is never mistaken for "three exist".
    """
    blockers: list[dict[str, Any]] = []
    suppressed = 0
    for check in checks:
        if not isinstance(check, Mapping) or not check.get("blocking"):
            continue
        if len(blockers) >= max(1, int(limit)):
            suppressed += 1
            continue
        blockers.append(blocker(str(check.get("code") or "unspecified"),
                                reason=str(check.get("reason") or ""),
                                remedy=str(check.get("remedy") or ""),
                                details=check.get("details")))
    return {"blockers": blockers, "count": len(blockers),
            "suppressed": suppressed, "limit": int(limit)}


__all__ = ["MAX_BLOCKERS", "blocker", "collect"]
