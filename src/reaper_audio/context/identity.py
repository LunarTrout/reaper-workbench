"""Project identity completeness for the context graph.

A revision triple (``project_id``, ``change_count``, ``path``) cannot prove
continuity: two REAPER runs can present the same path and the same change
counter, and a project reloaded from disk starts a new counter generation.
Continuity therefore also requires a *running-instance* identity and an
*epoch* (generation) that the bridge assigns and can prove.

This module is the single place that decides whether those components are
known.  A missing component is never defaulted into "same": it is reported as
a gap and every reuse decision fails closed at the caller.

Wire shape (inside any revision object)::

    {"project_id": ..., "instance_id": ..., "epoch": 3,
     "epoch_basis": "bridge_session_project_generation", "change_count": ...}

``epoch`` is a positive generation counter.  ``0``/``None`` means *unknown*
and can never prove continuity; the bridge starts its counter at 1.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import ContractValidationError

__all__ = [
    "EPOCH_BASIS_BRIDGE",
    "normalize_project_id",
    "IDENTITY_SCHEMA",
    "ProjectIdentity",
    "identity_complete",
    "identity_from_mapping",
    "identity_gaps",
    "require_same_identity",
    "same_identity_values",
]

def normalize_project_id(value: Any) -> str:
    """Separator- and case-insensitive form of a project id, for matching only.

    REAPER reports Windows paths with backslashes while archives written on
    other platforms (or by older capture code) may carry forward slashes.  The
    stored id is never rewritten; comparisons use this canonical form so the
    same open project resolves regardless of which side supplied the id.
    """
    return str(value).replace("\\", "/").lower()


IDENTITY_SCHEMA = "reaper-workbench.project-identity/1"

#: Basis string the resident bridge reports for its per-project generation.
EPOCH_BASIS_BRIDGE = "bridge_session_project_generation"


def _text_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _epoch_or_none(value: Any) -> int | None:
    """Return a positive epoch, or ``None`` when the value proves nothing."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 1 else None


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    """The identity components needed before a revision may be reused.

    ``epoch_basis`` documents *why* the epoch is trusted.  It is descriptive:
    the gate checks presence, not the words.
    """

    project_id: str
    instance_id: str | None
    epoch: int | None
    epoch_basis: str | None = None

    @property
    def complete(self) -> bool:
        return identity_complete(self)

    def gaps(self) -> tuple[str, ...]:
        return identity_gaps(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": IDENTITY_SCHEMA,
            "project_id": self.project_id,
            "instance_id": self.instance_id,
            "epoch": self.epoch,
            "epoch_basis": self.epoch_basis,
            "complete": self.complete,
        }


def identity_from_mapping(value: Any) -> ProjectIdentity:
    """Read identity components from a revision, binding or identity object.

    Nested ``{"identity": {...}}`` is accepted because capture bindings wrap a
    revision and a bridge may add the block incrementally.  Values are never
    coerced: a string epoch or a zero epoch becomes ``None`` rather than
    "generation zero".
    """
    if isinstance(value, ProjectIdentity):
        return value
    if not isinstance(value, Mapping):
        return ProjectIdentity("", None, None, None)
    nested = value.get("identity")
    source: Mapping[str, Any] = nested if isinstance(nested, Mapping) else value
    project_id = _text_or_none(source.get("project_id")) or ""
    instance_id = _text_or_none(source.get("instance_id"))
    epoch = _epoch_or_none(source.get("epoch"))
    basis = _text_or_none(source.get("epoch_basis"))
    return ProjectIdentity(project_id, instance_id, epoch, basis)


def identity_gaps(identity: ProjectIdentity) -> tuple[str, ...]:
    """Names of the identity components that cannot prove continuity."""
    gaps: list[str] = []
    if not identity.project_id:
        gaps.append("project_id")
    if identity.instance_id is None:
        gaps.append("instance_id")
    if identity.epoch is None:
        gaps.append("epoch")
    return tuple(gaps)


def identity_complete(identity: ProjectIdentity) -> bool:
    return not identity_gaps(identity)


def same_identity_values(left: ProjectIdentity, right: ProjectIdentity) -> bool:
    """True only when both identities are complete and every component agrees.

    Unknown components never compare equal: ``same_identity_values`` is the
    predicate a reuse decision uses, so tolerating a missing instance would
    silently accept a restarted REAPER that reused a path and counter.
    """
    if not identity_complete(left) or not identity_complete(right):
        return False
    return (left.project_id == right.project_id and
            left.instance_id == right.instance_id and
            left.epoch == right.epoch)


def require_same_identity(left: ProjectIdentity, right: ProjectIdentity,
                          *, field: str = "identity") -> None:
    """Raise a contract error naming every missing or mismatched component."""
    if same_identity_values(left, right):
        return
    details = {
        "left": left.to_dict(),
        "right": right.to_dict(),
        "gaps": sorted(set(identity_gaps(left)) | set(identity_gaps(right))),
        "next_action": "refresh-context",
    }
    raise _identity_error(details, field)


def _identity_error(details: dict[str, Any], field: str) -> ContractValidationError:
    error = ContractValidationError(
        "project identity is incomplete or belongs to another instance/epoch",
        field=field,
        next_action="refresh-context",
    )
    # ContractValidationError has no details slot; attach the structured facts
    # the caller needs without changing its stable public shape.
    error.identity_details = details  # type: ignore[attr-defined]
    return error
