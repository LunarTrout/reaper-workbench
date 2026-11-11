"""Apply agent-chosen cut times to REAPER through the transactional API.

The agent decides *which* cuts are musical. This module only resolves the
targets, builds one revision-bound plan, and applies it in an order that keeps
each original item GUID valid (latest cut first, on every track).
"""
from __future__ import annotations

from typing import Any, Iterable

from ..analysis.edit_map import _group
from ..errors import ValidationError
from .session import EditOperation, ProjectSession

MAX_CUTS_PER_TRANSACTION = 200


def _item_containing(items: Iterable[dict], time_s: float) -> dict | None:
    for item in items:
        position = float(item.get("position_s", 0.0))
        length = float(item.get("length_s", 0.0))
        if position < time_s < position + length:
            return item
    return None


def cut_plan(
    session: ProjectSession,
    cuts: Iterable[dict],
    *,
    reason: str,
) -> tuple[list[EditOperation], list[dict]]:
    """Resolve cut descriptors into item.split operations.

    Each cut is ``{"time_s": float, "group": "drums"}`` or
    ``{"time_s": float, "track_guids": [guid, ...]}``. Cuts are applied from
    the latest time backwards, so the original item GUID remains the target for
    earlier splits on the same track.
    """
    context = session.snapshot(include_items=True)
    tracks = {track.get("guid"): track for track in context.get("tracks", [])}
    if not tracks:
        raise ValidationError("The project has no tracks to cut")

    resolved: dict[tuple[str, float], dict] = {}
    skipped: list[dict] = []
    for cut in cuts:
        time_s = float(cut.get("time_s", -1))
        if time_s < 0:
            skipped.append({"cut": cut, "reason": "invalid time"})
            continue
        guids = cut.get("track_guids")
        if not guids:
            group = cut.get("group")
            if not group:
                skipped.append({"cut": cut, "reason": "no group or track_guids"})
                continue
            guids = [guid for guid, track in tracks.items()
                     if _group(str(track.get("name", ""))) == group]
        for guid in guids:
            track = tracks.get(guid)
            if not track:
                skipped.append({"cut": cut, "reason": "unknown track"})
                continue
            item = _item_containing(track.get("items", []), time_s)
            if not item:
                skipped.append({"cut": cut, "reason": "no item at time",
                                "track": track.get("name")})
                continue
            key = (guid, round(time_s, 6))
            resolved[key] = {"guid": guid, "time_s": time_s,
                             "item_guid": item["guid"],
                             "track_name": track.get("name", ""),
                             "fade_s": cut.get("fade_s", 0.02),
                             "tail_s": cut.get("tail_s", 0.0),
                             "right_trim_s": cut.get("right_trim_s", 0.0),
                             "move_s": cut.get("move_s", 0.0)}

    ordered = sorted(resolved.values(), key=lambda entry: -entry["time_s"])
    if len(ordered) > MAX_CUTS_PER_TRANSACTION:
        raise ValidationError(
            "Too many cuts for one transaction; send fewer cuts per call",
            details={"cuts": len(ordered), "limit": MAX_CUTS_PER_TRANSACTION})
    operations = [
        EditOperation(kind="item.split", target_guid=entry["item_guid"],
                      values={"position_s": entry["time_s"],
                              "fade_s": entry.get("fade_s", 0.02),
                              "tail_s": entry.get("tail_s", 0.0),
                              "right_trim_s": entry.get("right_trim_s", 0.0),
                              "move_s": entry.get("move_s", 0.0)})
        for entry in ordered
    ]
    return operations, skipped


def apply_cut_times(
    session: ProjectSession,
    cuts: Iterable[dict],
    *,
    reason: str,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan and apply agent-chosen cut times. Returns the transaction result."""
    operations, skipped = cut_plan(session, cuts, reason=reason)
    if not operations:
        return {"applied": False, "no_op": True, "operations": 0,
                "skipped": skipped}
    plan = session.plan(operations, reason=reason)
    result = session.apply(plan, dry_run=dry_run)
    result["operations_requested"] = len(operations)
    result["skipped"] = skipped
    return result
