"""Edit receipts: the exact facts needed to undo one edit, precisely.

REAPER's own undo stack is global and only reaches the newest action; a later
user edit makes an older transaction unreachable.  A receipt records what this
edit changed *and* what the affected values were, so undo can be rebuilt as the
inverse operations against the current project and never touches anything the
receipt did not change.

The receipt is plain JSON data plus a store.  Inverting a receipt is done by
:func:`build_inverse_operations`, which refuses rather than guesses when a
value has changed since the edit or when an operation cannot be reconstructed.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from ..context.ids import new_id
from ..errors import (ReceiptConflict, ReceiptNotUndoable, ValidationError)
from .edit_context import EditSnapshot, EnvelopePoint, SendRow, evaluate_envelope
from .session import EditOperation

RECEIPT_SCHEMA_VERSION = 1

#: Operations this module can invert precisely.  Anything else is refused by
#: name instead of being approximated.
INVERTIBLE_KINDS = frozenset({"track.set", "send.set", "send.create",
                              "send.delete", "envelope.set"})


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _values_match(expected: object, actual: object, *, atol: float,
                  rtol: float) -> bool:
    expected_number = _finite(expected)
    actual_number = _finite(actual)
    if expected_number is not None and actual_number is not None:
        return abs(expected_number - actual_number) <= max(
            atol, rtol * max(abs(expected_number), abs(actual_number)))
    return expected == actual


@dataclass(frozen=True, slots=True)
class EditReceipt:
    """One applied (or failed) library edit, with enough facts to invert it."""

    receipt_id: str
    project_id: str
    plan_id: str
    reason: str
    status: str
    operations: tuple[dict[str, Any], ...]
    revision_before: dict[str, Any]
    revision_after: dict[str, Any] | None
    before: tuple[dict[str, Any], ...] = ()
    after: tuple[dict[str, Any], ...] = ()
    apply_results: tuple[Any, ...] = ()
    baseline: dict[str, Any] = field(default_factory=dict)
    rebased: int = 0
    created_at: float = field(default_factory=time.time)
    verify: dict[str, Any] | None = None
    readback: dict[str, Any] | None = None
    undo_available: bool = False
    undo_block_reason: str | None = None
    undoes: str | None = None
    error: dict[str, Any] | None = None
    schema_version: int = RECEIPT_SCHEMA_VERSION

    @property
    def kind(self) -> str:
        return "undo" if self.undoes else "edit"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "kind": self.kind,
            "project_id": self.project_id,
            "plan_id": self.plan_id,
            "reason": self.reason,
            "status": self.status,
            "created_at": self.created_at,
            "undoes": self.undoes,
            "operations": [dict(item) for item in self.operations],
            "revision_before": dict(self.revision_before),
            "revision_after": (dict(self.revision_after)
                               if self.revision_after is not None else None),
            "before": [dict(item) for item in self.before],
            "after": [dict(item) for item in self.after],
            "apply_results": list(self.apply_results),
            "baseline": json.loads(json.dumps(self.baseline)),
            "rebased": self.rebased,
            "verify": self.verify,
            "readback": self.readback,
            "undo_available": self.undo_available,
            "undo_block_reason": self.undo_block_reason,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EditReceipt":
        if not isinstance(value, Mapping):
            raise ValidationError("A receipt must be a JSON object")
        required = ("receipt_id", "project_id", "plan_id", "reason", "status",
                    "operations", "revision_before")
        missing = [name for name in required if name not in value]
        if missing:
            raise ValidationError("A receipt is missing required fields",
                                  details={"missing": missing})
        if not isinstance(value["operations"], list):
            raise ValidationError("A receipt operations field must be a list")
        if not isinstance(value["revision_before"], Mapping):
            raise ValidationError("A receipt revision_before field must be an object")
        return cls(
            receipt_id=str(value["receipt_id"]),
            project_id=str(value["project_id"]),
            plan_id=str(value["plan_id"]),
            reason=str(value["reason"]),
            status=str(value["status"]),
            operations=tuple(dict(item) for item in value["operations"]),
            revision_before=dict(value["revision_before"]),
            revision_after=(dict(value["revision_after"])
                            if isinstance(value.get("revision_after"), Mapping)
                            else None),
            before=tuple(dict(item) for item in value.get("before") or ()
                         if isinstance(item, Mapping)),
            after=tuple(dict(item) for item in value.get("after") or ()
                        if isinstance(item, Mapping)),
            apply_results=tuple(value.get("apply_results") or ()),
            baseline=dict(value.get("baseline") or {}),
            rebased=int(value.get("rebased") or 0),
            created_at=float(value.get("created_at") or 0.0),
            verify=(dict(value["verify"]) if isinstance(value.get("verify"), Mapping)
                    else None),
            readback=(dict(value["readback"])
                      if isinstance(value.get("readback"), Mapping) else None),
            undo_available=bool(value.get("undo_available")),
            undo_block_reason=(str(value["undo_block_reason"])
                               if value.get("undo_block_reason") else None),
            undoes=(str(value["undoes"]) if value.get("undoes") else None),
            error=(dict(value["error"]) if isinstance(value.get("error"), Mapping)
                   else None),
            schema_version=int(value.get("schema_version") or RECEIPT_SCHEMA_VERSION),
        )


def new_receipt_id() -> str:
    """Mint one receipt identifier from the registered ``op`` prefix."""
    return new_id("op")


class ReceiptStore(Protocol):
    """Minimal persistence surface used by the edit layer."""

    def put(self, receipt: EditReceipt) -> None: ...

    def get(self, receipt_id: str) -> EditReceipt | None: ...


class MemoryReceiptStore:
    """Process-local receipt store (tests and short-lived sessions)."""

    def __init__(self) -> None:
        self._items: dict[str, EditReceipt] = {}
        self._lock = threading.Lock()

    def put(self, receipt: EditReceipt) -> None:
        with self._lock:
            self._items[receipt.receipt_id] = receipt

    def get(self, receipt_id: str) -> EditReceipt | None:
        with self._lock:
            return self._items.get(receipt_id)

    def list_recent(self, limit: int = 20) -> list[EditReceipt]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValidationError("limit must be a positive integer")
        with self._lock:
            ordered = sorted(self._items.values(),
                             key=lambda receipt: receipt.created_at, reverse=True)
        return ordered[:limit]


class FileReceiptStore:
    """One JSON file per receipt under an archive directory."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, receipt_id: str) -> Path:
        if not isinstance(receipt_id, str) or not receipt_id or \
                any(part in receipt_id for part in ("/", "\\", "..")):
            raise ValidationError("receipt_id is not a safe file name",
                                  details={"receipt_id": receipt_id})
        return self.root / f"{receipt_id}.json"

    def put(self, receipt: EditReceipt) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path(receipt.receipt_id)
        temp = target.with_suffix(".json.tmp")
        temp.write_text(json.dumps(receipt.to_dict(), ensure_ascii=False,
                                   separators=(",", ":")), encoding="utf-8")
        os.replace(temp, target)

    def get(self, receipt_id: str) -> EditReceipt | None:
        path = self._path(receipt_id)
        if not path.is_file():
            return None
        return EditReceipt.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_recent(self, limit: int = 20) -> list[EditReceipt]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValidationError("limit must be a positive integer")
        receipts: list[EditReceipt] = []
        if not self.root.is_dir():
            return receipts
        for path in self.root.glob("*.json"):
            try:
                receipts.append(EditReceipt.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, ValidationError):
                continue
        receipts.sort(key=lambda receipt: receipt.created_at, reverse=True)
        return receipts[:limit]


def capture_baseline(snapshot: EditSnapshot,
                     operations: Sequence[EditOperation]) -> dict[str, Any]:
    """Record the editable state each operation depends on.

    Only objects the batch touches are recorded, and sends are recorded as the
    whole list for their source: an index alone cannot restore a deleted send,
    and a destination alone cannot prove the index still means the same thing.
    """
    tracks: dict[str, Any] = {}
    sends: dict[str, Any] = {}
    envelopes: list[dict[str, Any]] = []
    for operation in operations:
        track = snapshot.track(operation.target_guid)
        if track is not None and track.guid not in tracks:
            tracks[track.guid] = track.baseline_values()
        if operation.kind.startswith("send."):
            source = snapshot.track(operation.target_guid)
            if source is not None and source.guid not in sends:
                sends[source.guid] = [row.to_dict()
                                      for row in snapshot.sends_from(source.guid)]
        if operation.kind == "envelope.set":
            name = operation.values.get("envelope")
            row = snapshot.envelope(operation.target_guid, str(name))
            if row is not None and not any(
                    entry["owner_guid"] == row.owner_guid and entry["name"] == row.name
                    for entry in envelopes):
                entry = row.baseline_values()
                entry.update({"owner_guid": row.owner_guid, "name": row.name})
                if track is not None:
                    entry["automation_mode"] = track.automation_mode
                envelopes.append(entry)
    return {"tracks": tracks, "sends": sends, "envelopes": envelopes}


def _baseline_track(baseline: Mapping[str, Any], guid: str) -> Mapping[str, Any] | None:
    tracks = baseline.get("tracks")
    if isinstance(tracks, Mapping) and isinstance(tracks.get(guid), Mapping):
        return tracks[guid]
    return None


def _baseline_sends(baseline: Mapping[str, Any], source_guid: str) -> list[dict[str, Any]]:
    sends = baseline.get("sends")
    if isinstance(sends, Mapping) and isinstance(sends.get(source_guid), list):
        return [dict(row) for row in sends[source_guid] if isinstance(row, Mapping)]
    return []


def _baseline_envelope(baseline: Mapping[str, Any], owner_guid: str,
                       name: str) -> Mapping[str, Any] | None:
    envelopes = baseline.get("envelopes")
    if not isinstance(envelopes, list):
        return None
    for entry in envelopes:
        if (isinstance(entry, Mapping) and entry.get("owner_guid") == owner_guid
                and entry.get("name") == name):
            return entry
    return None


def _after_values(receipt: EditReceipt, index: int) -> Mapping[str, Any]:
    if index < len(receipt.after) and isinstance(receipt.after[index], Mapping):
        values = receipt.after[index].get("values")
        if isinstance(values, Mapping):
            return values
    return {}


def _apply_result(receipt: EditReceipt, index: int) -> Mapping[str, Any]:
    if index < len(receipt.apply_results) and isinstance(
            receipt.apply_results[index], Mapping):
        return receipt.apply_results[index]
    return {}


def _current_send_at(snapshot: EditSnapshot, source_guid: str,
                     send_index: int) -> SendRow | None:
    return next((row for row in snapshot.sends_from(source_guid)
                 if row.send_index == send_index), None)


def _current_send_to(snapshot: EditSnapshot, source_guid: str,
                     destination_guid: str) -> list[SendRow]:
    return [row for row in snapshot.sends_from(source_guid)
            if row.destination_guid == destination_guid]


def expected_envelope_points(baseline_points: Sequence[Mapping[str, Any]],
                             operation: Mapping[str, Any]) -> list[EnvelopePoint]:
    """Reconstruct the point list the applied edit was supposed to leave."""
    points = [] if operation.get("clear") else [
        EnvelopePoint(index=0, time_s=float(point["time_s"]),
                      value=float(point["value"]),
                      shape=int(point.get("shape") or 0),
                      tension=float(point.get("tension") or 0.0))
        for point in baseline_points
    ]
    proposed = []
    for entry in operation.get("points") or ():
        proposed.append(EnvelopePoint(
            index=0, time_s=float(entry["time_s"]), value=float(entry["value"]),
            shape=int(entry.get("shape") or 0),
            tension=float(entry.get("tension") or 0.0),
        ))
    merged: list[EnvelopePoint] = []
    for point in [*points, *proposed]:
        merged = [existing for existing in merged
                  if abs(existing.time_s - point.time_s) > 1e-9]
        merged.append(point)
    merged.sort(key=lambda point: point.time_s)
    return merged


def envelope_curves_match(current_points: Sequence[EnvelopePoint],
                          expected: Sequence[EnvelopePoint], *,
                          atol: float) -> bool:
    """Compare two envelopes by their value at every point time of either.

    REAPER may keep two points at one time after an insert, so comparing the
    raw lists would report a mismatch for a curve that evaluates identically.
    Values are compared at the union of both point times; the shape between
    points is linear in both cases.
    """
    if not current_points and not expected:
        return True
    if not current_points or not expected:
        return False
    times = sorted({point.time_s for point in [*current_points, *expected]})
    for time_s in times:
        left = evaluate_envelope(current_points, time_s)
        right = evaluate_envelope(expected, time_s)
        if left is None or right is None:
            return False
        if abs(left - right) > atol:
            return False
    return True


def build_inverse_operations(
    receipt: EditReceipt,
    snapshot: EditSnapshot,
    *,
    allow_overwrite: bool = False,
    value_atol: float = 1e-9,
    value_rtol: float = 1e-6,
    envelope_atol: float = 2e-3,
) -> tuple[list[EditOperation], list[str]]:
    """Rebuild the operations that return exactly the receipt's targets.

    Operations are inverted in reverse order, so two edits to one object unwind
    in the order they were applied.  Any affected value that no longer matches
    what the receipt produced is a conflict (unless ``allow_overwrite``);
    operations that cannot be reconstructed precisely are refused by kind.
    """
    if not isinstance(receipt, EditReceipt):
        raise ValidationError("build_inverse_operations requires an EditReceipt")
    operations = [dict(item) for item in receipt.operations]
    inverse: list[EditOperation] = []
    notes: list[str] = []
    for index in range(len(operations) - 1, -1, -1):
        operation = operations[index]
        kind = str(operation.get("kind") or "")
        target = str(operation.get("target_guid") or "")
        values = operation.get("values")
        values = dict(values) if isinstance(values, Mapping) else {}
        after = _after_values(receipt, index)
        result = _apply_result(receipt, index)
        if kind not in INVERTIBLE_KINDS:
            raise ReceiptNotUndoable(
                "This receipt contains an operation that cannot be inverted precisely",
                details={"receipt_id": receipt.receipt_id, "kind": kind,
                         "operation_index": index,
                         "invertible_kinds": sorted(INVERTIBLE_KINDS),
                         "next_action": ("use a checkpoint or the REAPER undo stack "
                                         "for this kind; the receipt stays as history")},
            )
        if kind.startswith("send.") and not snapshot.sends_complete:
            raise ReceiptConflict(
                "The send list is incomplete, so this undo cannot prove the route state",
                details={"receipt_id": receipt.receipt_id, "kind": kind,
                         "missing": list(snapshot.missing)},
            )
        if kind == "envelope.set" and not snapshot.automation_complete:
            raise ReceiptConflict(
                "The envelope list is incomplete, so this undo cannot prove the curve state",
                details={"receipt_id": receipt.receipt_id, "kind": kind,
                         "missing": list(snapshot.missing)},
            )

        if kind == "track.set":
            baseline = _baseline_track(receipt.baseline, target)
            if baseline is None:
                raise ReceiptNotUndoable(
                    "The receipt has no baseline for this track",
                    details={"receipt_id": receipt.receipt_id, "track_guid": target},
                )
            current = snapshot.track(target)
            if current is None:
                raise ReceiptConflict(
                    "The track changed since the receipt was applied",
                    details={"receipt_id": receipt.receipt_id, "track_guid": target,
                             "reason": "track no longer exists"},
                )
            current_values = current.baseline_values()
            for key in values:
                if key not in baseline:
                    continue
                expected = after.get(key, values[key])
                if not _values_match(expected, current_values.get(key),
                                     atol=value_atol, rtol=value_rtol):
                    if not allow_overwrite:
                        raise ReceiptConflict(
                            "A value changed after this receipt was applied",
                            details={"receipt_id": receipt.receipt_id,
                                     "track_guid": target, "field": key,
                                     "expected": expected,
                                     "current": current_values.get(key),
                                     "next_action": ("re-apply the intended value, or "
                                                     "pass allow_overwrite=true")},
                        )
                    notes.append(f"{target}.{key} was changed after the edit; overwritten")
            restore = {key: baseline[key] for key in values if key in baseline}
            if restore:
                inverse.append(EditOperation("track.set", target, restore))

        elif kind == "send.set":
            send_index = values.get("send_index")
            if isinstance(send_index, bool) or not isinstance(send_index, int):
                raise ReceiptNotUndoable(
                    "The receipt has no send index to restore",
                    details={"receipt_id": receipt.receipt_id, "operation_index": index},
                )
            baseline_rows = _baseline_sends(receipt.baseline, target)
            baseline = next((row for row in baseline_rows
                             if row.get("send_index") == send_index), None)
            current = _current_send_at(snapshot, target, send_index)
            if baseline is None or current is None:
                raise ReceiptConflict(
                    "The send changed since the receipt was applied",
                    details={"receipt_id": receipt.receipt_id, "source_guid": target,
                             "send_index": send_index,
                             "reason": "send no longer exists at this index"},
                )
            if current.destination_guid != baseline.get("destination_guid"):
                raise ReceiptConflict(
                    "The send index now points at a different destination",
                    details={"receipt_id": receipt.receipt_id, "source_guid": target,
                             "send_index": send_index,
                             "expected_destination": baseline.get("destination_guid"),
                             "current_destination": current.destination_guid},
                )
            for key in ("volume", "pan", "mute"):
                if key not in values:
                    continue
                expected = after.get(key, values[key])
                if not _values_match(expected, getattr(current, key),
                                     atol=value_atol, rtol=value_rtol):
                    if not allow_overwrite:
                        raise ReceiptConflict(
                            "A send value changed after this receipt was applied",
                            details={"receipt_id": receipt.receipt_id,
                                     "source_guid": target, "send_index": send_index,
                                     "field": key, "expected": expected,
                                     "current": getattr(current, key)},
                        )
                    notes.append(f"send {send_index}.{key} was changed after the edit; overwritten")
            inverse.append(EditOperation("send.set", target, {
                "send_index": send_index,
                "volume": baseline.get("volume", current.volume),
                "pan": baseline.get("pan", current.pan),
                "mute": bool(baseline.get("mute", current.mute)),
            }))

        elif kind == "send.create":
            created_index = result.get("send_index")
            if isinstance(created_index, bool) or not isinstance(created_index, int):
                raise ReceiptNotUndoable(
                    "The receipt does not know which send this operation created",
                    details={"receipt_id": receipt.receipt_id, "operation_index": index},
                )
            current = _current_send_at(snapshot, target, created_index)
            destination = values.get("destination_guid")
            if current is None:
                raise ReceiptConflict(
                    "The created send no longer exists at its recorded index",
                    details={"receipt_id": receipt.receipt_id, "source_guid": target,
                             "send_index": created_index},
                )
            if current.destination_guid != destination:
                raise ReceiptConflict(
                    "The send index now points at a different destination",
                    details={"receipt_id": receipt.receipt_id, "source_guid": target,
                             "send_index": created_index,
                             "expected_destination": destination,
                             "current_destination": current.destination_guid},
                )
            inverse.append(EditOperation("send.delete", target,
                                         {"send_index": created_index}))

        elif kind == "send.delete":
            send_index = values.get("send_index")
            if isinstance(send_index, bool) or not isinstance(send_index, int):
                raise ReceiptNotUndoable(
                    "The receipt has no send index for the deleted send",
                    details={"receipt_id": receipt.receipt_id, "operation_index": index},
                )
            baseline_rows = _baseline_sends(receipt.baseline, target)
            baseline = next((row for row in baseline_rows
                             if row.get("send_index") == send_index), None)
            if baseline is None:
                raise ReceiptNotUndoable(
                    "The receipt has no routing facts for the deleted send",
                    details={"receipt_id": receipt.receipt_id, "source_guid": target,
                             "send_index": send_index},
                )
            if _current_send_to(snapshot, target, str(baseline["destination_guid"])):
                raise ReceiptConflict(
                    "A send to this destination already exists; recreating it "
                    "would duplicate routing",
                    details={"receipt_id": receipt.receipt_id, "source_guid": target,
                             "destination_guid": baseline["destination_guid"]},
                )
            inverse.append(EditOperation("send.create", target, {
                "destination_guid": baseline["destination_guid"],
                "volume": baseline.get("volume", 1.0),
                "pan": baseline.get("pan", 0.0),
                "mute": bool(baseline.get("mute", False)),
            }))

        elif kind == "envelope.set":
            name = str(values.get("envelope") or "")
            baseline = _baseline_envelope(receipt.baseline, target, name)
            if baseline is None:
                raise ReceiptNotUndoable(
                    "The receipt has no baseline points for this envelope",
                    details={"receipt_id": receipt.receipt_id, "owner_guid": target,
                             "envelope": name},
                )
            current_row = snapshot.envelope(target, name)
            if current_row is None:
                raise ReceiptConflict(
                    "The envelope no longer exists",
                    details={"receipt_id": receipt.receipt_id, "owner_guid": target,
                             "envelope": name},
                )
            expected_points = expected_envelope_points(
                list(baseline.get("points") or []), values)
            if not envelope_curves_match(current_row.points, expected_points,
                                         atol=envelope_atol):
                if not allow_overwrite:
                    raise ReceiptConflict(
                        "The envelope changed after this receipt was applied",
                        details={"receipt_id": receipt.receipt_id,
                                 "owner_guid": target, "envelope": name,
                                 "expected_point_count": len(expected_points),
                                 "current_point_count": len(current_row.points),
                                 "next_action": ("re-apply the intended curve, or pass "
                                                 "allow_overwrite=true")},
                    )
                notes.append(f"envelope {name} was changed after the edit; overwritten")
            restore_points = [{"time_s": float(point["time_s"]),
                               "value": float(point["value"]),
                               "shape": int(point.get("shape") or 0),
                               "tension": float(point.get("tension") or 0.0)}
                              for point in baseline.get("points") or []]
            restore_values: dict[str, Any] = {
                "envelope": name, "points": restore_points, "clear": True,
            }
            if baseline.get("active") is not None:
                restore_values["active"] = bool(baseline["active"])
            inverse.append(EditOperation("envelope.set", target, restore_values))
            baseline_mode = baseline.get("automation_mode")
            current_track = snapshot.track(target)
            mode_was_set = any(
                entry.get("kind") == "track.set"
                and entry.get("target_guid") == target
                and isinstance(entry.get("values"), Mapping)
                and "automation_mode" in entry["values"]
                for entry in receipt.operations)
            if isinstance(baseline_mode, int) and mode_was_set:
                after_mode = None
                for mode_index, entry in enumerate(receipt.operations):
                    if (entry.get("kind") == "track.set"
                            and entry.get("target_guid") == target
                            and isinstance(entry.get("values"), Mapping)
                            and "automation_mode" in entry["values"]):
                        facts = _after_values(receipt, mode_index)
                        candidate = facts.get("automation_mode_after",
                                              facts.get("automation_mode"))
                        if isinstance(candidate, int) and not isinstance(candidate, bool):
                            after_mode = candidate
                current_mode = (current_track.automation_mode
                                if current_track is not None else None)
                if (current_mode is not None and after_mode is not None
                        and current_mode != after_mode and not allow_overwrite):
                    raise ReceiptConflict(
                        "The automation mode changed after this receipt was applied",
                        details={"receipt_id": receipt.receipt_id,
                                 "owner_guid": target, "field": "automation_mode",
                                 "expected": after_mode, "current": current_mode},
                    )
                inverse.append(EditOperation("track.set", target,
                                             {"automation_mode": baseline_mode}))
                if current_mode is None or after_mode is None:
                    notes.append(
                        f"automation mode of {target} restored from the receipt; "
                        f"the current mode could not be compared")

    if not inverse:
        raise ReceiptNotUndoable(
            "The receipt produced no inverse operations",
            details={"receipt_id": receipt.receipt_id,
                     "next_action": "nothing in this receipt can be restored automatically"},
        )
    return inverse, notes


def receipt_block_reason(operations: Sequence[Mapping[str, Any]]) -> str | None:
    """Return why a batch has no precise undo, or ``None`` when it has one."""
    blockers = [str(operation.get("kind"))
                for operation in operations
                if str(operation.get("kind")) not in INVERTIBLE_KINDS]
    if not blockers:
        return None
    return ("no precise inverse for: " + ", ".join(sorted(set(blockers)))
            + "; use a checkpoint for these operations")


def mark_undone(receipt: EditReceipt, *, undo_receipt_id: str) -> EditReceipt:
    """Return a copy of ``receipt`` whose status records the targeted undo."""
    return replace(receipt, status="undone",
                   verify={**(receipt.verify or {}), "undone_by": undo_receipt_id})


__all__ = [
    "INVERTIBLE_KINDS",
    "RECEIPT_SCHEMA_VERSION",
    "EditReceipt",
    "FileReceiptStore",
    "MemoryReceiptStore",
    "ReceiptStore",
    "build_inverse_operations",
    "capture_baseline",
    "envelope_curves_match",
    "expected_envelope_points",
    "mark_undone",
    "new_receipt_id",
    "receipt_block_reason",
]
