"""Envelope reads and guarded writes.

An envelope write can create silence or a large gain step.  This module reads
points in a requested unit (amplitude or dB), evaluates every check the caller
asked for *before* anything is planned, and refuses with the exact offending
times and values.

Nothing here assumes an envelope name, a track name, an interval or a
threshold.  The owner, name, interval, mode, scaling and every guard limit are
parameters of the call; ``guard=None`` means "no limits", and each guard key
that is omitted is not checked.  There are no implicit dB defaults.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import EnvelopeGuardRejected, ValidationError
from .edit_context import (EditSnapshot, EnvelopePoint, EnvelopeRow,
                           automation_mode_name, db_to_linear,
                           evaluate_envelope, linear_to_db, parse_automation_mode,
                           read_edit_context)
from .session import EditOperation

#: Units a caller may use for envelope values.  ``amplitude`` is the linear
#: value REAPER stores; ``db`` is a level in decibels.
SCALINGS = ("amplitude", "db")

_GUARD_KEYS = ("min_gain_db", "max_delta_db", "forbid_silence")


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _normalize_scaling(scaling: object) -> str:
    if not isinstance(scaling, str) or scaling not in SCALINGS:
        raise ValidationError("scaling must be 'amplitude' or 'db'",
                              details={"scaling": scaling, "accepted": list(SCALINGS)})
    return scaling


def _interval(value: object, *, field: str) -> tuple[float, float] | None:
    """Validate an optional ``[start_s, end_s]`` project interval."""
    if value is None:
        return None
    if (not isinstance(value, (list, tuple)) or len(value) != 2):
        raise ValidationError(f"{field} must be [start_s, end_s]",
                              details={field: value})
    start, end = _finite(value[0]), _finite(value[1])
    if start is None or end is None or start < 0 or end <= start:
        raise ValidationError(
            f"{field} must be two finite numbers with 0 <= start < end",
            details={field: value},
        )
    return start, end


def _to_amplitude(value: float, scaling: str) -> float:
    if scaling == "db":
        return db_to_linear(value)
    return float(value)


def _from_amplitude(value: float, scaling: str) -> float | None:
    if scaling == "db":
        return linear_to_db(value)
    return float(value)


def _point_value(point: Mapping[str, Any], scaling: str, index: int) -> tuple[float, float]:
    """Return ``(time_s, amplitude)`` for one requested point."""
    time_s = _finite(point.get("time_s"))
    value = _finite(point.get("value"))
    if time_s is None or time_s < 0:
        raise ValidationError(
            "Every envelope point needs a finite non-negative time_s",
            details={"point_index": index, "point": dict(point)},
        )
    if value is None:
        raise ValidationError(
            "Every envelope point needs a finite value",
            details={"point_index": index, "point": dict(point)},
        )
    amplitude = _to_amplitude(value, scaling)
    if not math.isfinite(amplitude) or amplitude < 0:
        raise ValidationError(
            "Envelope point value is outside a usable amplitude range",
            details={"point_index": index, "value": value, "scaling": scaling},
        )
    return time_s, amplitude


def _shape_and_tension(point: Mapping[str, Any], index: int) -> tuple[int, float]:
    shape = point.get("shape", 0)
    tension = point.get("tension", 0.0)
    if (isinstance(shape, bool) or not isinstance(shape, int) or not 0 <= shape <= 5):
        raise ValidationError("Envelope point shape must be an integer from 0 to 5",
                              details={"point_index": index, "shape": shape})
    if (isinstance(tension, bool) or not isinstance(tension, (int, float)) or
            not math.isfinite(float(tension)) or not -1 <= float(tension) <= 1):
        raise ValidationError("Envelope point tension must be between -1 and 1",
                              details={"point_index": index, "tension": tension})
    return shape, float(tension)


def _snapshot(value: EditSnapshot | Any) -> EditSnapshot:
    """Accept either a prepared snapshot or a live project session."""
    if isinstance(value, EditSnapshot):
        return value
    return read_edit_context(value)


def envelope_points(snapshot: EditSnapshot | Any, owner_guid: str, name: str, *,
                    interval: tuple[float, float] | None = None,
                    scaling: str = "amplitude") -> dict[str, Any]:
    """Return one envelope's points, optionally restricted to an interval.

    ``scaling='amplitude'`` reports the linear value REAPER stores;
    ``scaling='db'`` reports it in decibels (``None`` for a non-positive
    amplitude, i.e. actual silence).  The bridge's native scaling is always
    reported separately, so a fader-scaled volume envelope is never mistaken
    for an amplitude one.
    """
    scaling = _normalize_scaling(scaling)
    window = _interval(interval, field="interval")
    snapshot = _snapshot(snapshot)
    owner = snapshot.require_track(owner_guid)
    row = snapshot.require_envelope(owner_guid, name)
    points: list[dict[str, Any]] = []
    for point in row.points:
        if window is not None and not (window[0] <= point.time_s <= window[1]):
            continue
        value = _from_amplitude(point.value, scaling)
        entry = point.to_dict()
        entry["value"] = value
        points.append(entry)
    values = [point.value for point in row.points
              if window is None or window[0] <= point.time_s <= window[1]]
    db_values: list[float] = []
    for value in values:
        db_value = linear_to_db(value)
        if db_value is not None:
            db_values.append(db_value)
    return {
        "owner_guid": row.owner_guid,
        "owner_name": owner.name,
        "owner_is_master": owner.is_master,
        "automation_mode": owner.automation_mode,
        "mode_name": automation_mode_name(owner.automation_mode),
        "name": row.name,
        "envelope_guid": row.guid,
        "scaling": scaling,
        "scaling_mode": row.scaling_mode,
        "native_scaling": row.scaling_name,
        "active": row.active,
        "visible": row.visible,
        "armed": row.armed,
        "point_count": len(row.points),
        "returned_point_count": len(points),
        "complete": snapshot.automation_complete,
        "interval": list(window) if window is not None else None,
        "points": points,
        "min_value": min(values) if values else None,
        "max_value": max(values) if values else None,
        "min_db": min(db_values) if db_values else None,
        "max_db": max(db_values) if db_values else None,
    }


@dataclass(frozen=True, slots=True)
class _GuardViolation:
    code: str
    time_s: float
    value_db: float | None
    limit_db: float | None = None
    before_db: float | None = None
    delta_db: float | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "time_s": self.time_s,
                                 "value_db": self.value_db}
        if self.limit_db is not None:
            value["limit_db"] = self.limit_db
        if self.before_db is not None:
            value["before_db"] = self.before_db
        if self.delta_db is not None:
            value["delta_db"] = self.delta_db
        return value


def _validate_guard(guard: object) -> dict[str, Any]:
    if guard is None:
        return {}
    if not isinstance(guard, Mapping):
        raise ValidationError("guard must be an object",
                              details={"guard": guard, "accepted": list(_GUARD_KEYS)})
    unknown = sorted(set(guard) - set(_GUARD_KEYS))
    if unknown:
        raise ValidationError("guard contains unsupported keys",
                              details={"unsupported": unknown,
                                       "accepted": list(_GUARD_KEYS)})
    validated: dict[str, Any] = {}
    for key in ("min_gain_db", "max_delta_db"):
        if key not in guard:
            continue
        value = _finite(guard[key])
        if value is None:
            raise ValidationError(f"guard.{key} must be a finite number",
                                  details={"guard": dict(guard)})
        validated[key] = value
    if "forbid_silence" in guard:
        if not isinstance(guard["forbid_silence"], bool):
            raise ValidationError("guard.forbid_silence must be a boolean",
                                  details={"guard": dict(guard)})
        validated["forbid_silence"] = guard["forbid_silence"]
    return validated


def _guard_checks(points: Sequence[tuple[float, float]], row: EnvelopeRow,
                  guard: Mapping[str, Any]) -> list[_GuardViolation]:
    """Compare proposed amplitudes with the guard and the current envelope."""
    violations: list[_GuardViolation] = []
    if guard.get("min_gain_db") is not None:
        limit = float(guard["min_gain_db"])
        for time_s, amplitude in points:
            value_db = linear_to_db(amplitude)
            if value_db is None or value_db < limit:
                violations.append(_GuardViolation(
                    code="min_gain_db", time_s=time_s, value_db=value_db,
                    limit_db=limit))
    if guard.get("max_delta_db") is not None:
        limit = float(guard["max_delta_db"])
        for time_s, amplitude in points:
            before = evaluate_envelope(row.points, time_s)
            if before is None:
                continue
            before_db = linear_to_db(before)
            after_db = linear_to_db(amplitude)
            if before_db is None and after_db is None:
                # silence to silence: the level did not move
                continue
            if before_db is None or after_db is None:
                violations.append(_GuardViolation(
                    code="max_delta_db", time_s=time_s, value_db=after_db,
                    limit_db=limit, before_db=before_db))
                continue
            delta = after_db - before_db
            if abs(delta) > limit + 1e-12:
                violations.append(_GuardViolation(
                    code="max_delta_db", time_s=time_s, value_db=after_db,
                    limit_db=limit, before_db=before_db, delta_db=delta))
    if guard.get("forbid_silence"):
        for time_s, amplitude in points:
            if amplitude <= 0:
                violations.append(_GuardViolation(
                    code="forbid_silence", time_s=time_s, value_db=None))
    return violations


def plan_envelope_set(
    snapshot: EditSnapshot | Any,
    owner_guid: str,
    name: str,
    points: Sequence[Mapping[str, Any]],
    *,
    scaling: str = "amplitude",
    mode: object = None,
    active: bool | None = None,
    clear: bool = False,
    guard: Mapping[str, Any] | None = None,
    interval: tuple[float, float] | None = None,
    point_atol: float = 1e-9,
) -> tuple[list[EditOperation], dict[str, Any]]:
    """Build ``envelope.set`` (and, when requested, ``track.set``) operations.

    Parameters are the whole contract: which envelope, which points, which
    unit, which automation mode, which interval the caller intends to affect
    and which guard (if any) the write must satisfy.

    Guard keys are independent: ``min_gain_db`` refuses a point below that
    level, ``max_delta_db`` refuses a point that moves further than that from
    the current curve at the same time, and ``forbid_silence`` refuses a point
    whose amplitude is zero.  An omitted key is not checked; ``guard=None``
    applies no limits.  No dB value has a default here.

    A guard violation raises :class:`EnvelopeGuardRejected` carrying every
    offending point; nothing is sent to REAPER.
    """
    scaling = _normalize_scaling(scaling)
    window = _interval(interval, field="interval")
    snapshot = _snapshot(snapshot)
    guard_values = _validate_guard(guard)
    if active is not None and not isinstance(active, bool):
        raise ValidationError("active must be a boolean", details={"active": active})
    if not isinstance(clear, bool):
        raise ValidationError("clear must be a boolean", details={"clear": clear})
    if isinstance(point_atol, bool) or not isinstance(point_atol, (int, float)) or \
            not math.isfinite(float(point_atol)) or point_atol < 0:
        raise ValidationError("point_atol must be a finite non-negative number")
    owner = snapshot.require_track(owner_guid)
    row = snapshot.require_envelope(owner_guid, name)
    if not isinstance(points, Sequence) or isinstance(points, (str, bytes)):
        raise ValidationError("points must be a list of envelope points")
    if len(points) > 10_000:
        raise ValidationError("points is limited to 10000 entries",
                              details={"points": len(points)})

    parsed: list[tuple[float, float, int, float]] = []
    for index, point in enumerate(points):
        if not isinstance(point, Mapping):
            raise ValidationError("Every envelope point must be an object",
                                  details={"point_index": index})
        time_s, amplitude = _point_value(point, scaling, index)
        if window is not None and not (window[0] - point_atol <= time_s
                                       <= window[1] + point_atol):
            raise ValidationError(
                "An envelope point lies outside the declared interval",
                details={"point_index": index, "time_s": time_s,
                         "interval": list(window)},
            )
        shape, tension = _shape_and_tension(point, index)
        parsed.append((time_s, amplitude, shape, tension))
    parsed.sort(key=lambda entry: entry[0])
    for left, right in zip(parsed, parsed[1:]):
        if right[0] - left[0] <= point_atol:
            raise ValidationError(
                "Two envelope points share the same time",
                details={"time_s": left[0], "point_atol": point_atol},
            )

    violations = _guard_checks([(time_s, amplitude) for time_s, amplitude, _, _ in parsed],
                               row, guard_values)
    if violations:
        raise EnvelopeGuardRejected(
            "The envelope write violates the requested guard",
            details={
                "envelope": name,
                "owner_guid": owner_guid,
                "guard": dict(guard_values),
                "violations": [violation.to_dict() for violation in violations],
                "violation_count": len(violations),
                "proposed_points": len(parsed),
                "next_action": ("change the points or send explicit wider guard "
                                "limits; nothing was planned"),
            },
        )

    operations: list[EditOperation] = []
    mode_value = parse_automation_mode(mode)
    if mode_value is not None:
        operations.append(EditOperation(
            kind="track.set", target_guid=owner_guid,
            values={"automation_mode": mode_value},
        ))
        if owner.automation_mode is not None and owner.automation_mode == mode_value:
            # Still emitted so the plan is uniform; the bridge reports no_op.
            pass
    values: dict[str, Any] = {
        "envelope": name,
        "points": [{"time_s": time_s, "value": amplitude, "shape": shape,
                    "tension": tension}
                   for time_s, amplitude, shape, tension in parsed],
        "clear": clear,
    }
    if active is not None:
        values["active"] = active
    operations.append(EditOperation(kind="envelope.set", target_guid=owner_guid,
                                    values=values))
    report = {
        "envelope": name,
        "owner_guid": owner_guid,
        "owner_name": owner.name,
        "owner_is_master": owner.is_master,
        "scaling": scaling,
        "native_scaling": row.scaling_name,
        "scaling_mode": row.scaling_mode,
        "points": len(parsed),
        "cleared": clear,
        "active": active,
        "automation_mode": mode_value,
        "mode_name": automation_mode_name(mode_value),
        "interval": list(window) if window is not None else None,
        "baseline_point_count": len(row.points),
        "guard": {
            "requested": dict(guard_values),
            "checked": sorted(guard_values),
            "violations": [],
        },
    }
    return operations, report


__all__ = ["SCALINGS", "envelope_points", "plan_envelope_set"]
