"""Local per-event refine actions for a crossfade timing plan.

Every action takes the plan dict produced by
:mod:`reaper_audio.daw.timing_service`, returns ``(child_plan, changes)`` and
never touches the project or the source audio.  The input plan is deep-copied
before any edit, so the caller keeps its own plan even when an action is
rejected.  ``changes`` names every event id, block index or seam index that
actually moved, together with the reason and the residual the caller must
expect.  Geometry outside the touched event or seam is copied verbatim.

Plan contract used here: times are seconds; anchors are attacks; numeric seams
are cut points where the following block starts; blocks are the clips written
to the project.  A scored seam mapping with ``start_s`` and ``end_s`` (the
planner's native output) is moved as a whole so its width survives.  Protected
ranges are source intervals unless they declare ``basis="project_s"``.

``changes["applied"]`` is false when the request was already satisfied; in
that case the returned plan is an unmodified copy and keeps its ``plan_id``.
An applied action gets a new ``plan_id``, a ``parent_plan_id`` and one compact
entry in ``plan["refinements"]``.
"""

from __future__ import annotations

import copy
import math
from typing import Any, MutableMapping, Mapping, cast
from uuid import uuid4

from ..errors import ValidationError

MIN_CLIP_S = 0.004
ATTACK_CLEARANCE_S = 0.004

__all__ = [
    "MIN_CLIP_S",
    "ATTACK_CLEARANCE_S",
    "pin_attack",
    "manual_attack",
    "exclude_candidate",
    "choose_rhythm_option",
    "protect_interval",
    "recompute_seam",
]


def _number(value: Any, field: str) -> float:
    """Return one finite float or reject the value at the plan boundary."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a finite number",
                              details={"field": field, "value": value})
    number = float(value)
    if not math.isfinite(number):
        raise ValidationError(f"{field} must be a finite number",
                              details={"field": field, "value": value})
    return number


def _copy_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-copy a crossfade plan and make sure its collections are lists."""
    if not isinstance(plan, Mapping):
        raise ValidationError("A timing plan must be a mapping")
    if plan.get("method") != "crossfade":
        raise ValidationError("Local refine actions need a crossfade timing plan",
                              details={"method": plan.get("method")})
    copied = copy.deepcopy(plan)
    child: dict[str, Any] = copied if isinstance(copied, dict) else dict(copied)
    for key in ("blocks", "seams", "anchors"):
        value = child.get(key)
        if value is None:
            child[key] = []
        elif not isinstance(value, list):
            raise ValidationError(f"The plan {key} must be a list",
                                  details={"field": key})
    return child


def _stamp(child: dict[str, Any], parent: Mapping[str, Any], action: str,
           changes: Mapping[str, Any]) -> dict[str, Any]:
    """Give an edited child plan its own id and a compact refinement ledger."""
    child["parent_plan_id"] = parent.get("plan_id")
    child["plan_id"] = f"tpl_{uuid4().hex[:16]}"
    ledger = list(child.get("refinements") or [])
    ledger.append({"action": action, "changes": copy.deepcopy(dict(changes))})
    child["refinements"] = ledger
    return child


def _find_anchor(plan: Mapping[str, Any], event_id: Any) -> dict[str, Any]:
    """Return the one anchor for an event id, or reject an unknown id."""
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValidationError("event_id must be a non-empty string",
                              details={"event_id": event_id})
    anchors = plan.get("anchors") or []
    anchor = next((row for row in anchors
                   if isinstance(row, Mapping) and row.get("id") == event_id), None)
    if anchor is None:
        known = [str(row.get("id")) for row in anchors
                 if isinstance(row, Mapping) and row.get("id") is not None]
        raise ValidationError("Unknown timing event id",
                              details={"event_id": event_id, "known_ids": known[:32]})
    return cast(dict[str, Any], anchor)


def _anchor_time(anchor: Mapping[str, Any], field: str = "project_s") -> float:
    return _number(anchor.get(field, anchor.get("project_s")), f"anchor.{field}")


def _block_ids(block: Mapping[str, Any]) -> list[str]:
    ids = block.get("event_ids") or []
    if not isinstance(ids, list):
        raise ValidationError("A block event_ids field must be a list",
                              details={"position_s": block.get("position_s")})
    return [str(value) for value in ids]


def pin_attack(plan: Mapping[str, Any], event_id: str
               ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Freeze one attack at its measured project time for later passes.

    The anchor target becomes ``anchor["project_s"]`` and the pin is written as
    ``pinned=True`` plus ``pin_shift_s=0.0``, which the seam grouping already
    treats as a fixed group.  Blocks are not rewritten here; if the current
    geometry still carries a block shift, ``changes["residual_s"]`` reports how
    far the planned position is from the new target and a later pass must honour
    the pin.
    """
    child = _copy_plan(plan)
    anchor = _find_anchor(child, event_id)
    project = _anchor_time(anchor, "project_s")
    previous_target = _anchor_time(anchor, "target_s")
    desired = anchor.get("desired_shift_s")
    desired = 0.0 if desired is None else _number(desired, "anchor.desired_shift_s")
    achieved = anchor.get("achieved_shift_s")
    achieved = 0.0 if achieved is None else _number(achieved, "anchor.achieved_shift_s")
    applied = (anchor.get("pinned") is not True or
               abs(previous_target - project) > 1e-12 or
               abs(desired) > 1e-12 or
               anchor.get("pin_shift_s") not in (0, 0.0))
    changes = {
        "action": "pin_attack",
        "event_id": event_id,
        "applied": applied,
        "previous_target_s": previous_target,
        "target_s": project,
        "project_s": project,
        "residual_s": achieved,
        "pinned": True,
        "pin_shift_s": 0.0,
        "requires_replan": abs(achieved) > 1e-12,
        "reason": (None if abs(achieved) <= 1e-12 else
                   "the current geometry still carries a block shift; "
                   "the pin is honoured on the next planning pass"),
    }
    if not applied:
        return child, changes
    anchor["target_s"] = project
    anchor["desired_shift_s"] = 0.0
    anchor["residual_s"] = achieved
    anchor["pinned"] = True
    anchor["pin_shift_s"] = 0.0
    return _stamp(child, plan, "pin_attack", changes), changes


def manual_attack(plan: Mapping[str, Any], *, source_s: float, project_s: float,
                  event_id: str | None = None, target_s: float | None = None,
                  attack_end_s: float | None = None
                  ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Add an attack the detector missed at an explicit source and project time.

    The id is the caller's ``event_id`` or the first free ``manual_<n>``.  The
    new anchor starts unpinned with the given project time as its target, and
    its id is inserted into the block that owns the source position so later
    planning keeps the clip association.  A source/project pair that does not
    agree with the block mapping is not rejected: the difference is reported as
    ``mapping_residual_s``.
    """
    child = _copy_plan(plan)
    source = _number(source_s, "source_s")
    project = _number(project_s, "project_s")
    position = _number(child.get("item_position_s"), "item_position_s")
    offset = _number(child.get("source_offset_s"), "source_offset_s")
    length = _number(child.get("item_length_s"), "item_length_s")
    if not position - 1e-9 <= project <= position + length + 1e-9:
        raise ValidationError("A manual attack must fall inside the item",
                              details={"project_s": project,
                                       "item_window_s": [position, position + length]})
    if not offset - 1e-9 <= source <= offset + length + 1e-9:
        raise ValidationError("A manual attack must fall inside the item source window",
                              details={"source_s": source,
                                       "source_window_s": [offset, offset + length]})
    duration = child.get("duration_s")
    if duration is not None and source > _number(duration, "duration_s") + 1e-9:
        raise ValidationError("A manual attack cannot lie past the source audio",
                              details={"source_s": source, "duration_s": duration})
    anchors = child["anchors"]
    used = {str(row.get("id")) for row in anchors
            if isinstance(row, Mapping) and row.get("id") is not None}
    used.update(str(value) for block in child["blocks"]
                if isinstance(block, Mapping) for value in _block_ids(block))
    excluded = {str(row.get("event_id")) for row in (child.get("excluded_events") or [])
                if isinstance(row, Mapping)}
    if event_id is None:
        index = 1
        while f"manual_{index}" in used:
            index += 1
        new_id = f"manual_{index}"
    elif not isinstance(event_id, str) or not event_id.strip():
        raise ValidationError("event_id must be a non-empty string",
                              details={"event_id": event_id})
    elif event_id in used:
        raise ValidationError("The manual event_id is already used by this plan",
                              details={"event_id": event_id})
    elif event_id in excluded:
        raise ValidationError("The manual event_id belongs to an excluded event",
                              details={"event_id": event_id})
    else:
        new_id = event_id

    target = project if target_s is None else _number(target_s, "target_s")
    attack_end = source if attack_end_s is None else _number(attack_end_s, "attack_end_s")
    if not position <= target < position + length or not source <= attack_end <= offset + length:
        raise ValidationError("The manual target and attack interval must fit inside the item")
    candidates = []
    for index, block in enumerate(child["blocks"]):
        if not isinstance(block, Mapping):
            raise ValidationError("Timing plan blocks must be objects",
                                  details={"block_index": index})
        start = _number(block.get("source_start_s"), "block.source_start_s")
        clip_length = _number(block.get("length_s"), "block.length_s")
        last = index == len(child["blocks"]) - 1
        inside = (start - 1e-9 <= source <= start + clip_length + 1e-9
                  if last else start - 1e-9 <= source < start + clip_length - 1e-12)
        if inside:
            candidates.append(index)
    if not candidates:
        raise ValidationError("The manual attack lies outside every plan block",
                              details={"source_s": source, "project_s": project})
    owners = [index for index in candidates
              if abs(child["blocks"][index]["position_s"] + source
                     - child["blocks"][index]["source_start_s"] - project) <= 1e-6]
    if len(candidates) > 1 and len(owners) != 1:
        raise ValidationError("The manual attack appears in several blocks; pass its observed project time",
                              details={"source_s": source, "block_indexes": candidates})
    block_index = owners[0] if owners else candidates[0]
    block = child["blocks"][block_index]
    block_position = _number(block.get("position_s"), "block.position_s")
    block_source = _number(block.get("source_start_s"), "block.source_start_s")
    mapping_residual = (block_position + source - block_source) - project

    ids = _block_ids(block)
    anchor_times = {str(row.get("id")): _anchor_time(row, "project_s")
                    for row in anchors if isinstance(row, Mapping)}
    insert_at = len(ids)
    for index, existing in enumerate(ids):
        if anchor_times.get(existing, math.inf) > project:
            insert_at = index
            break
    ids.insert(insert_at, new_id)
    block["event_ids"] = ids

    new_anchor = {
        "id": new_id, "source_s": source, "project_s": project,
        "target_s": target, "desired_shift_s": target - project, "confidence": 1.0,
        "attack_end_s": attack_end, "achieved_shift_s": mapping_residual,
        "residual_s": project + mapping_residual - target,
        "kind": "attack", "manual": True,
    }
    anchor_index = len(anchors)
    for index, row in enumerate(anchors):
        if isinstance(row, Mapping) and _anchor_time(row, "project_s") > project:
            anchor_index = index
            break
    anchors.insert(anchor_index, new_anchor)

    changes = {
        "action": "manual_attack",
        "event_id": new_id,
        "applied": True,
        "source_s": source,
        "project_s": project,
        "target_s": target,
        "residual_s": project + mapping_residual - target,
        "mapping_residual_s": mapping_residual,
        "block_index": block_index,
        "event_ids_index": insert_at,
        "anchor_index": anchor_index,
        "reason": (None if abs(mapping_residual) <= 1e-6 else
                   "the supplied project time differs from the block source mapping"),
    }
    return _stamp(child, plan, "manual_attack", changes), changes


def exclude_candidate(plan: Mapping[str, Any], event_id: str, *, reason: str
                      ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Drop one false-positive event without touching its neighbours.

    The anchor disappears, its id leaves every block that listed it, and the
    exclusion is recorded in ``plan["excluded_events"]``.  Seams and block
    geometry stay exactly as they were.
    """
    child = _copy_plan(plan)
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationError("An exclusion needs a non-empty reason",
                              details={"event_id": event_id, "reason": reason})
    anchor = _find_anchor(child, event_id)
    anchors = child["anchors"]
    neighbours = []
    for index, row in enumerate(anchors):
        if row is anchor:
            if index:
                neighbours.append(str(anchors[index - 1].get("id")))
            if index + 1 < len(anchors):
                neighbours.append(str(anchors[index + 1].get("id")))
            break
    block_indexes = []
    for index, block in enumerate(child["blocks"]):
        if not isinstance(block, Mapping):
            raise ValidationError("Timing plan blocks must be objects",
                                  details={"block_index": index})
        ids = _block_ids(block)
        if event_id in ids:
            cast(dict[str, Any], block)["event_ids"] = [
                value for value in ids if value != event_id]
            block_indexes.append(index)
    child["anchors"] = [row for row in anchors if row is not anchor]
    excluded = list(child.get("excluded_events") or [])
    excluded.append({
        "event_id": event_id,
        "reason": reason,
        "source_s": anchor.get("source_s"),
        "project_s": _anchor_time(anchor, "project_s"),
        "target_s": _anchor_time(anchor, "target_s"),
        "block_indexes": block_indexes,
    })
    child["excluded_events"] = excluded
    changes = {
        "action": "exclude_candidate",
        "event_id": event_id,
        "applied": True,
        "reason": reason,
        "source_s": anchor.get("source_s"),
        "project_s": _anchor_time(anchor, "project_s"),
        "target_s": _anchor_time(anchor, "target_s"),
        "block_indexes": block_indexes,
        "neighbours": neighbours,
        "remaining_anchor_count": len(child["anchors"]),
    }
    return _stamp(child, plan, "exclude_candidate", changes), changes


def choose_rhythm_option(plan: Mapping[str, Any], *, segment_id: str, option_id: str
                         ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Record the musical choice for one rhythm segment without moving attacks."""
    child = _copy_plan(plan)
    if not isinstance(segment_id, str) or not segment_id.strip():
        raise ValidationError("segment_id must be a non-empty string",
                              details={"segment_id": segment_id})
    if not isinstance(option_id, str) or not option_id.strip():
        raise ValidationError("option_id must be a non-empty string",
                              details={"segment_id": segment_id, "option_id": option_id})
    choices = child.get("rhythm_choices")
    if choices is None:
        choices = {}
    elif not isinstance(choices, Mapping):
        raise ValidationError("rhythm_choices must be an object",
                              details={"segment_id": segment_id})
    previous = choices.get(segment_id)
    applied = previous != option_id
    changes = {
        "action": "choose_rhythm_option",
        "segment_id": segment_id,
        "option_id": option_id,
        "previous_option_id": previous,
        "applied": applied,
        "attacks_moved": 0,
        "reason": None if applied else "the segment already uses this option",
    }
    if not applied:
        return child, changes
    updated = dict(choices)
    updated[segment_id] = option_id
    child["rhythm_choices"] = updated
    return _stamp(child, plan, "choose_rhythm_option", changes), changes


def _source_span(row: Any) -> tuple[float, float] | None:
    """Return one protected range in source seconds, or None for project rows."""
    if not isinstance(row, Mapping):
        raise ValidationError("protected_ranges entries must be objects",
                              details={"range": row})
    basis = row.get("basis")
    if basis not in (None, "source_s", "project_s"):
        raise ValidationError("A protected range basis must be source_s or project_s",
                              details={"basis": basis})
    if "source_start_s" in row:
        start = _number(row.get("source_start_s"), "protected_range.source_start_s")
        end = _number(row.get("source_end_s"), "protected_range.source_end_s")
    elif basis == "project_s":
        return None
    else:
        start = _number(row.get("start_s"), "protected_range.start_s")
        end = _number(row.get("end_s"), "protected_range.end_s")
    if end <= start:
        raise ValidationError("A protected range ends before it starts",
                              details={"start_s": start, "end_s": end})
    return start, end


def protect_interval(plan: Mapping[str, Any], *, start_s: float, end_s: float
                     ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Add one protected source interval and merge it with overlapping ones.

    New ranges are stored as ``{"start_s", "end_s", "basis": "source_s"}``.
    Rows without a basis are source ranges in this plan shape; only rows that
    declare ``basis="project_s"`` are project ranges and are copied verbatim.
    Merged rows take the place of the first source range so unrelated entries
    keep their order.
    """
    child = _copy_plan(plan)
    start = _number(start_s, "start_s")
    end = _number(end_s, "end_s")
    if end <= start:
        raise ValidationError("A protected interval must end after it starts",
                              details={"start_s": start, "end_s": end})
    if start < -1e-9:
        raise ValidationError("A protected source interval cannot start below zero",
                              details={"start_s": start})
    duration = child.get("duration_s")
    if duration is not None:
        duration = _number(duration, "duration_s")
        if end > duration + 1e-9:
            raise ValidationError("A protected source interval exceeds the source audio",
                                  details={"end_s": end, "duration_s": duration})
    rows = child.get("protected_ranges")
    if rows is None:
        rows = []
    elif not isinstance(rows, list):
        raise ValidationError("protected_ranges must be a list")
    spans = []
    for index, row in enumerate(rows):
        span = _source_span(row)
        if span is not None:
            spans.append((span[0], span[1], index))
    before = list(spans)
    spans.append((start, end, None))
    spans.sort(key=lambda item: (item[0], item[1]))
    merged: list[list[Any]] = []
    for low, high, index in spans:
        if merged and low <= merged[-1][1] + 1e-9:
            merged[-1][1] = max(merged[-1][1], high)
            if index is not None:
                merged[-1][2].append(index)
        else:
            merged.append([low, high, [] if index is None else [index]])
    merged_rows = [{"start_s": low, "end_s": high, "basis": "source_s"}
                   for low, high, _ in merged]
    combined = []
    inserted = False
    for row in rows:
        if _source_span(row) is None:
            combined.append(row)
        elif not inserted:
            combined.extend(merged_rows)
            inserted = True
    if not inserted:
        combined.extend(merged_rows)
    applied = combined != rows
    changes = {
        "action": "protect_interval",
        "applied": applied,
        "added": {"start_s": start, "end_s": end},
        "merged": [{"start_s": low, "end_s": high} for low, high, _ in merged],
        "source_ranges_before": [{"start_s": low, "end_s": high}
                                 for low, high, _ in before],
        "protected_range_count_before": len(rows),
        "protected_range_count_after": len(combined),
        "already_protected": not applied,
        "reason": None if applied else "the interval is already protected",
    }
    if not applied:
        return child, changes
    child["protected_ranges"] = combined
    return _stamp(child, plan, "protect_interval", changes), changes


def _seam_time(value: Any) -> float:
    """Read the project time of a numeric seam or a scored seam mapping."""
    if isinstance(value, Mapping):
        for key in ("project_s", "start_s", "time_s"):
            if value.get(key) is not None:
                return _number(value[key], f"seam.{key}")
        raise ValidationError("Seam entry has no project time",
                              details={"seam": dict(value)})
    return _number(value, "seam")


def _shift_seam(value: Any, delta: float) -> Any:
    """Move a seam entry by delta while keeping a scored seam's width."""
    if isinstance(value, Mapping):
        moved = copy.deepcopy(dict(value))
        for key in ("start_s", "end_s", "project_s", "time_s"):
            if key in moved:
                moved[key] = _number(moved[key], f"seam.{key}") + delta
        return moved
    return _number(value, "seam") + delta


def _seam_conflict(child: dict[str, Any], seam_index: int, seam_s: float,
                   requested: float, code: str, reason: str,
                   available: float | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the untouched child plan with a concrete, machine-readable refusal."""
    conflict = {
        "code": code,
        "reason": reason,
        "seam_index": seam_index,
        "seam_s": seam_s,
        "requested_shift_s": requested,
        "available_shift_s": available,
    }
    changes = {
        "action": "recompute_seam",
        "seam_index": seam_index,
        "applied": False,
        "clamped": False,
        "seam_before_s": seam_s,
        "seam_s": seam_s,
        "requested_shift_s": requested,
        "shift_s": 0.0,
        "available_shift_s": available,
        "residual_s": requested,
        "reason": reason,
        "conflict": conflict,
        "blocks_changed": [],
        "seam_changed": None,
        "anchors_moved": [],
    }
    return child, changes


SLIP_TRIGGER_S = 0.004


def _slip_seam(child: dict[str, Any], seam_index: int, delta_s: float,
               max_shift_s: float, duration: float | None
               ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Move the incoming clip in time, keep its source, report moved attacks.

    ``delta_s`` is how far the attacks inside the incoming clip must travel.
    The clip's end stays put, so its length changes by ``-delta_s`` and the
    outgoing clip's length changes by ``delta_s``.  The result carries the
    bounds that limited the move, so a caller can see why it was clamped.
    """
    blocks = child["blocks"]
    seams = child["seams"]
    left = dict(blocks[seam_index])
    right = dict(blocks[seam_index + 1])
    left_position = float(left["position_s"])
    left_length = float(left["length_s"])
    right_position = float(right["position_s"])
    right_source = float(right["source_start_s"])
    right_length = float(right["length_s"])
    seam_s = right_position

    bounds = []
    if max_shift_s > 0.0:
        bounds.append((max_shift_s, "max_shift_s"))
        bounds.append((-max_shift_s, "max_shift_s"))
    bounds.append((left_length - MIN_CLIP_S, "clip_too_short"))
    bounds.append((MIN_CLIP_S - right_length, "clip_too_short"))
    bounds.append((right_position - left_position - MIN_CLIP_S, "clip_too_short"))
    if duration is not None:
        bounds.append((duration - right_source - right_length, "insufficient_source"))
    low = max((value for value, _ in bounds if value < 0.0), default=0.0)
    high = min((value for value, _ in bounds if value > 0.0), default=0.0)
    shift = min(max(delta_s, low), high)
    limited_by = None
    for value, code in bounds:
        if abs(shift - value) <= 1e-9 and abs(delta_s - value) > 1e-9:
            limited_by = code
            break
    if abs(shift) < 1e-6:
        return _seam_conflict(child, seam_index, seam_s, shift,
                              limited_by or "insufficient_source",
                              "the seam cannot move without breaking a limit",
                              high)

    right["position_s"] = right_position + shift
    right["length_s"] = right_length - shift
    left["length_s"] = left_length + shift
    blocks[seam_index] = left
    blocks[seam_index + 1] = right
    seams[seam_index] = right["position_s"]

    moved = []
    right_end = right_position + right_length
    for anchor in child["anchors"]:
        if not isinstance(anchor, Mapping):
            continue
        time_s = _anchor_time(anchor, "project_s")
        if right_position - 1e-9 <= time_s < right_end - 1e-9:
            moved.append({"event_id": anchor.get("id"),
                          "previous_project_s": round(time_s, 6),
                          "project_s": round(time_s + shift, 6)})
            if isinstance(anchor, MutableMapping):
                anchor["project_s"] = time_s + shift

    changes = {
        "action": "recompute_seam",
        "mode": "slip",
        "seam_index": seam_index,
        "applied": True,
        "clamped": limited_by is not None,
        "seam_before_s": round(seam_s, 6),
        "seam_s": round(right["position_s"], 6),
        "requested_shift_s": round(delta_s, 6),
        "shift_s": round(shift, 6),
        "limited_by": limited_by,
        "seam_placement_residual_s": round(delta_s - shift, 6),
        "conflict": None,
        "blocks_changed": [
            {"index": seam_index, "length_s": round(left["length_s"], 6),
             "previous_length_s": round(left_length, 6)},
            {"index": seam_index + 1, "position_s": round(right["position_s"], 6),
             "previous_position_s": round(right_position, 6),
             "source_start_s": round(right_source, 6),
             "previous_source_start_s": round(right_source, 6),
             "length_s": round(right["length_s"], 6),
             "previous_length_s": round(right_length, 6)},
        ],
        "seam_changed": {"index": seam_index,
                         "previous_s": round(seam_s, 6),
                         "seam_s": round(right["position_s"], 6)},
        "anchors_moved": moved,
    }
    return child, changes


def recompute_seam(plan: Mapping[str, Any], *, seam_index: int, max_shift_s: float,
                   mode: str = "auto"
                   ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Re-place one seam between its neighbouring attacks.

    The target position is the midpoint of the gap left by the closest attack
    on each side, minus the clearance a cut needs.  The move is capped by
    ``max_shift_s``, by the remaining source audio after the outgoing clip, by
    the minimum clip length of both neighbours and by the attacks themselves.
    A partial move is applied and reported as ``clamped`` with the residual.
    When no move in the requested direction is legal, the plan comes back
    unchanged and ``changes["conflict"]`` names the blocking limit.

    ``mode`` decides what a seam move means:

    ``"boundary"`` keeps every attack where it is: the incoming clip's position
    and its source start move by the same amount, so its interior mapping is
    unchanged.  This is right when the seam is merely badly placed.

    ``"slip"`` moves the incoming clip in time and keeps its source start, so
    every attack inside it moves by the same delta.  The outgoing clip shortens
    and the incoming one lengthens by that delta, which is the only way to place
    a late attack without touching its neighbours.

    ``"auto"`` slips when the incoming clip holds an attack whose residual
    exceeds ``SLIP_TRIGGER_S`` and the move fits the limits, otherwise it only
    re-places the boundary.
    """
    child = _copy_plan(plan)
    if isinstance(seam_index, bool) or not isinstance(seam_index, int):
        raise ValidationError("seam_index must be an integer",
                              details={"seam_index": seam_index})
    blocks = child["blocks"]
    seams = child["seams"]
    if len(blocks) < 2 or len(seams) != len(blocks) - 1:
        raise ValidationError("A crossfade plan needs one seam between each pair of blocks",
                              details={"block_count": len(blocks),
                                       "seam_count": len(seams)})
    if not 0 <= seam_index < len(seams):
        raise ValidationError("Unknown seam_index",
                              details={"seam_index": seam_index,
                                       "seam_count": len(seams)})
    max_shift = _number(max_shift_s, "max_shift_s")
    if max_shift <= 0.0:
        raise ValidationError("max_shift_s must be greater than zero",
                              details={"max_shift_s": max_shift})

    left = blocks[seam_index]
    right = blocks[seam_index + 1]
    for index, block in ((seam_index, left), (seam_index + 1, right)):
        if not isinstance(block, Mapping):
            raise ValidationError("Timing plan blocks must be objects",
                                  details={"block_index": index})
    left_position = _number(left.get("position_s"), "block.position_s")
    left_source = _number(left.get("source_start_s"), "block.source_start_s")
    left_length = _number(left.get("length_s"), "block.length_s")
    right_position = _number(right.get("position_s"), "block.position_s")
    right_source = _number(right.get("source_start_s"), "block.source_start_s")
    right_length = _number(right.get("length_s"), "block.length_s")
    if left_length <= 0.0 or right_length <= 0.0:
        raise ValidationError("Both blocks around a seam need a positive length",
                              details={"seam_index": seam_index})
    seam_s = _seam_time(seams[seam_index])
    if abs(seam_s - right_position) > 1e-6:
        raise ValidationError("Seam time does not match the following block",
                              details={"seam_index": seam_index, "seam_s": seam_s,
                                       "block_position_s": right_position})
    if not left_position < seam_s <= left_position + left_length + 1e-6:
        raise ValidationError("Seam time does not fall inside the preceding block",
                              details={"seam_index": seam_index, "seam_s": seam_s,
                                       "block_span_s": [left_position,
                                                        left_position + left_length]})
    duration = child.get("duration_s")
    duration = None if duration is None else _number(duration, "duration_s")

    if mode not in ("auto", "boundary", "slip"):
        raise ValidationError("Unknown seam mode",
                              details={"mode": mode,
                                       "accepted": ["auto", "boundary", "slip"]})

    incoming_end = right_position + right_length
    late = []
    for anchor in child["anchors"]:
        if not isinstance(anchor, Mapping):
            continue
        time_s = _anchor_time(anchor, "project_s")
        if right_position - 1e-9 <= time_s < incoming_end - 1e-9:
            target = anchor.get("target_s")
            if isinstance(target, (int, float)):
                delta = float(target) - time_s
                if abs(delta) > SLIP_TRIGGER_S:
                    late.append((abs(delta), delta))
    if late and mode in ("auto", "slip"):
        late.sort(reverse=True)
        return _slip_seam(child, seam_index, late[0][1], max_shift, duration)

    attack_times = []
    for anchor in child["anchors"]:
        if isinstance(anchor, Mapping) and anchor.get("id") is not None:
            attack_times.append(_anchor_time(anchor, "project_s"))
    previous_attacks = [value for value in attack_times if value < seam_s - 1e-12]
    next_attacks = [value for value in attack_times if value >= seam_s - 1e-12]
    previous_attack = max(previous_attacks) if previous_attacks else None
    next_attack = min(next_attacks) if next_attacks else None
    low = (None if previous_attack is None
           else previous_attack + ATTACK_CLEARANCE_S)
    high = (None if next_attack is None
            else next_attack - ATTACK_CLEARANCE_S)

    if low is not None and high is not None and high < low:
        reason = (f"attacks at {previous_attack:.6f} s and {next_attack:.6f} s "
                  f"leave less than the {2 * ATTACK_CLEARANCE_S:.3f} s a seam needs")
        aim = (low + high) / 2.0
        return _seam_conflict(child, seam_index, seam_s, aim - seam_s,
                              "neighbouring_attack_overlap", reason,
                              high - seam_s)

    if low is not None and high is not None:
        aim = (low + high) / 2.0
    elif low is not None:
        aim = max(seam_s, low)
    elif high is not None:
        aim = min(seam_s, high)
    else:
        aim = seam_s
    requested = aim - seam_s

    later = [(max_shift, "max_shift_s",
              f"the caller cap is {max_shift:.6f} s")]
    earlier = [(-max_shift, "max_shift_s",
                f"the caller cap is {max_shift:.6f} s")]
    later.append((right_length - MIN_CLIP_S, "clip_too_short",
                  f"block {seam_index + 1} would be shorter than {MIN_CLIP_S:.3f} s"))
    earlier.append((MIN_CLIP_S - left_length, "clip_too_short",
                    f"block {seam_index} would be shorter than {MIN_CLIP_S:.3f} s"))
    if duration is not None:
        later.append((duration - (left_source + left_length), "insufficient_source",
                      f"block {seam_index} reaches {left_source + left_length:.6f} s "
                      f"of a {duration:.6f} s source"))
        earlier.append((-right_source, "insufficient_source",
                        f"block {seam_index + 1} starts at {right_source:.6f} s "
                        "of the source"))
    if low is not None:
        earlier.append((low - seam_s, "neighbouring_attack_overlap",
                        f"the previous attack is at {previous_attack:.6f} s"))
    if high is not None:
        later.append((high - seam_s, "neighbouring_attack_overlap",
                      f"the next attack is at {next_attack:.6f} s"))
    latest = min(value for value, _, _ in later)
    earliest = max(value for value, _, _ in earlier)
    if earliest > latest + 1e-12:
        reason = ("the neighbouring clips and attacks leave no legal seam position")
        return _seam_conflict(child, seam_index, seam_s, requested,
                              "clip_too_short", reason, latest)

    if abs(requested) <= 1e-12:
        changes = {
            "action": "recompute_seam",
            "seam_index": seam_index,
            "applied": False,
            "clamped": False,
            "seam_before_s": seam_s,
            "seam_s": seam_s,
            "requested_shift_s": 0.0,
            "shift_s": 0.0,
            "available_shift_s": 0.0,
            "residual_s": 0.0,
            "reason": ("the seam already sits midway between its neighbouring attacks"
                       if low is not None and high is not None else
                       "no neighbouring attack asks for a different cut"),
            "conflict": None,
            "blocks_changed": [],
            "seam_changed": None,
            "anchors_moved": [],
        }
        return child, changes

    if requested > 0.0:
        _, code, detail = min(later, key=lambda item: item[0])
        available = latest
        if latest <= 1e-12:
            return _seam_conflict(child, seam_index, seam_s, requested, code, detail,
                                  available)
        shift = min(requested, latest)
    else:
        _, code, detail = max(earlier, key=lambda item: item[0])
        available = earliest
        if earliest >= -1e-12:
            return _seam_conflict(child, seam_index, seam_s, requested, code, detail,
                                  available)
        shift = max(requested, earliest)

    new_seam = seam_s + shift
    if low is not None and new_seam < low - 1e-9:
        return _seam_conflict(
            child, seam_index, seam_s, requested, "neighbouring_attack_overlap",
            f"moving {shift:+.6f} s would still overlap the attack at "
            f"{previous_attack:.6f} s", shift)
    if high is not None and new_seam > high + 1e-9:
        return _seam_conflict(
            child, seam_index, seam_s, requested, "neighbouring_attack_overlap",
            f"moving {shift:+.6f} s would still overlap the attack at "
            f"{next_attack:.6f} s", shift)
    if left_length + shift < MIN_CLIP_S - 1e-9 or right_length - shift < MIN_CLIP_S - 1e-9:
        return _seam_conflict(
            child, seam_index, seam_s, requested, "clip_too_short",
            f"the move would leave less than {MIN_CLIP_S:.3f} s of clip", shift)
    if duration is not None and left_source + left_length + shift > duration + 1e-9:
        return _seam_conflict(
            child, seam_index, seam_s, requested, "insufficient_source",
            f"block {seam_index} would pass the {duration:.6f} s source end", shift)
    if right_source + shift < -1e-9:
        return _seam_conflict(
            child, seam_index, seam_s, requested, "insufficient_source",
            f"block {seam_index + 1} would start before the source", shift)

    clamped = abs(shift - requested) > 1e-9
    reason = None
    if clamped:
        reason = (f"{detail}; applied {shift:+.6f} s of the requested "
                  f"{requested:+.6f} s")
    changes = {
        "action": "recompute_seam",
        "seam_index": seam_index,
        "applied": abs(shift) > 1e-12,
        "clamped": clamped,
        "seam_before_s": seam_s,
        "seam_s": new_seam,
        "requested_shift_s": requested,
        "shift_s": shift,
        "available_shift_s": available,
        "residual_s": requested - shift,
        "reason": reason,
        "conflict": None,
        "blocks_changed": [
            {"index": seam_index,
             "length_s": left_length + shift,
             "previous_length_s": left_length},
            {"index": seam_index + 1,
             "position_s": right_position + shift,
             "previous_position_s": right_position,
             "source_start_s": right_source + shift,
             "previous_source_start_s": right_source,
             "length_s": right_length - shift,
             "previous_length_s": right_length},
        ],
        "seam_changed": {"index": seam_index, "previous_s": seam_s,
                         "seam_s": new_seam},
        "anchors_moved": [],
    }
    if not changes["applied"]:
        return child, changes
    left["length_s"] = left_length + shift
    right["position_s"] = right_position + shift
    right["source_start_s"] = right_source + shift
    right["length_s"] = right_length - shift
    child["seams"][seam_index] = _shift_seam(child["seams"][seam_index], shift)
    return _stamp(child, plan, "recompute_seam", changes), changes

def insert_seam(plan: Mapping[str, Any], *, event_id: str, lead_s: float = 0.008,
                max_shift_s: float = 0.04
                ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Give one late attack its own clip, leaving the rest of the block alone.

    Slipping a whole clip moves every attack inside it, so one late note is
    placed by cutting the clip in the quiet window before it, playing the new
    clip so the attack lands on its target, and cutting again before the next
    attack so the tail block resumes the original mapping.  Only the attack
    that needed the move changes time.

    ``lead_s`` is how far before an attack a cut may go.  Both new clips read
    the same source continuously: nothing is stretched and no audio is invented.
    The move is capped by ``max_shift_s``.
    """
    child = _copy_plan(plan)
    if not isinstance(event_id, str) or not event_id:
        raise ValidationError("event_id must be a non-empty string",
                              details={"event_id": event_id})
    lead = _number(lead_s, "lead_s")
    cap = _number(max_shift_s, "max_shift_s")
    if lead <= 0.0 or cap <= 0.0:
        raise ValidationError("lead_s and max_shift_s must be positive",
                              details={"lead_s": lead, "max_shift_s": cap})

    anchor = None
    for candidate in child["anchors"]:
        if isinstance(candidate, Mapping) and candidate.get("id") == event_id:
            anchor = candidate
            break
    if anchor is None:
        raise ValidationError("Unknown event id", details={"event_id": event_id})
    observed_s = _anchor_time(anchor, "project_s")
    attack_source = anchor.get("source_s")
    if not isinstance(attack_source, (int, float)):
        raise ValidationError("The attack has no source coordinate",
                              details={"event_id": event_id})
    target = anchor.get("target_s")
    if not isinstance(target, (int, float)):
        raise ValidationError("The attack has no numeric target",
                              details={"event_id": event_id})
    target = float(target)
    blocks = child["blocks"]
    seams = child["seams"]
    # Several blocks may read the same source sample (crossfades overlap in
    # source).  The one that actually plays it is the one whose mapping lands
    # inside the item, so bind by that instead of taking the first match.
    item_start = child.get("item_position_s")
    item_length = child.get("item_length_s")
    item_end = (float(item_start) + float(item_length)
                if item_start is not None and item_length is not None else None)
    candidates = []
    for position, block in enumerate(blocks):
        start = _number(block.get("position_s"), "block.position_s")
        length = _number(block.get("length_s"), "block.length_s")
        source = _number(block.get("source_start_s"), "block.source_start_s")
        if source - 1e-9 <= attack_source < source + length - 1e-9:
            echo = start + (attack_source - source)
            inside = item_end is None or (float(item_start) - 1e-9 <= echo <= item_end + 1e-9)
            candidates.append({"index": position, "echo_s": round(echo, 6),
                               "inside_item": inside})
    bound = [c for c in candidates if c["inside_item"]]
    if not bound:
        raise ValidationError("No block plays the attack inside the item",
                              details={"event_id": event_id, "source_s": attack_source,
                                       "item_window_s": None if item_end is None
                                       else [round(float(item_start), 6), round(item_end, 6)],
                                       "candidates": candidates})
    owners = [c for c in bound if event_id in _block_ids(blocks[c["index"]])]
    if len(owners) == 1:
        bound = owners
    if len(bound) > 1:
        raise ValidationError("Several blocks could play the attack",
                              details={"event_id": event_id, "source_s": attack_source,
                                       "candidates": bound})
    index = bound[0]["index"]

    block = dict(blocks[index])
    start = float(block["position_s"])
    source = float(block["source_start_s"])
    length = float(block["length_s"])
    end = start + length
    duration = child.get("duration_s")
    duration = None if duration is None else _number(duration, "duration_s")

    # Where this sample sounds today, through the block that holds it.
    echo_project = start + (attack_source - source)

    attack_s = echo_project
    wanted = target - attack_s
    delta = max(min(wanted, cap), -cap)
    # Moving the clip earlier makes the incoming and outgoing clips overlap by
    # exactly |delta| at the cut, and a crossfade may not run past the attack it
    # is meant to clear.  The cut therefore sits at least one crossfade plus a
    # margin before the attack.
    crossfade_guess = min(abs(delta), 0.025)
    lead = max(lead, abs(delta) + crossfade_guess + 0.006)
    if abs(delta) < 1e-6:
        return child, {"action": "insert_seam", "event_id": event_id,
                       "applied": False, "reason": "attack is already on target",
                       "shift_s": 0.0, "conflict": None, "anchors_moved": []}

    if delta > 0.0:
        # Moving an attack later: the outgoing clip must fade to silence exactly
        # where it would otherwise repeat the attack, and the incoming clip must
        # start before that point reading its own source, so the pick plays once.
        crossfade = min(max(lead_s, 0.004), 0.025)
        cut_project = echo_project - crossfade
        new_position = cut_project
        right_source = attack_source - (echo_project + delta - new_position)
        left_length = echo_project - start
        if left_length < MIN_CLIP_S:
            return _seam_conflict(child, max(index - 1, 0), cut_project, 0.0,
                                  "clip_too_short",
                                  f"the outgoing half would be {left_length:.6f} s", 0.0)
        from ..analysis.timing_seams import MAX_SOURCE_SKIP

        attack_end = float(anchor.get("attack_end_s", attack_source))
        shifted_end = echo_project + delta + attack_end - attack_source
        later_times = [start + float(row["source_s"]) - source
                       for row in child["anchors"] if row.get("source_s") is not None
                       and row["id"] != event_id
                       and attack_end < float(row["source_s"]) < source + length]
        tail_fade = max(crossfade, delta - MAX_SOURCE_SKIP + 1e-6)
        original_fade_out = float(block.get("fade_out_s") or 0.0)
        tail_cut = min([end - max(MIN_CLIP_S, original_fade_out + tail_fade),
                        *(time - tail_fade for time in later_times)])
        right_end = tail_cut + tail_fade
        if (right_source < 0 or shifted_end > tail_cut or tail_cut < echo_project
                or right_end - new_position < MIN_CLIP_S):
            return _seam_conflict(child, max(index - 1, 0), cut_project, delta,
                                  "no_safe_tail_seam",
                                  "the shifted attack and its next neighbour need more room", 0.0)

        left = dict(block, length_s=round(left_length, 6), fade_out_s=round(crossfade, 6))
        left["event_ids"] = [key for key in _block_ids(left) if key != event_id]
        right = {"position_s": round(new_position, 6),
                 "source_start_s": round(right_source, 6),
                 "length_s": round(right_end - new_position, 6),
                 "fade_in_s": round(crossfade, 6),
                 "fade_out_s": round(tail_fade, 6), "event_ids": [event_id]}
        tail = dict(block, position_s=round(tail_cut, 6),
                    source_start_s=round(source + tail_cut - start, 6),
                    length_s=round(end - tail_cut, 6), fade_in_s=round(tail_fade, 6))
        tail_ids = {row["id"] for row in child["anchors"] if row.get("source_s") is not None
                    and source + tail_cut - start <= row["source_s"] < source + length}
        tail["event_ids"] = [key for key in left["event_ids"] if key in tail_ids]
        left["event_ids"] = [key for key in left["event_ids"] if key not in tail_ids]
        moved = [{"event_id": event_id, "previous_project_s": round(echo_project, 6),
                  "project_s": round(echo_project + delta, 6)}]
        blocks[index:index + 1] = [left, right, tail]
        seams[index:index] = [round(new_position, 6), round(tail_cut, 6)]
        changes = {"action": "insert_seam", "event_id": event_id, "applied": True,
                   "mode": "later", "shift_s": round(delta, 6),
                   "cut_project_s": round(cut_project, 6), "cut_source_s": round(right_source, 6),
                   "crossfade_s": round(crossfade, 6), "anchors_moved": moved, "conflict": None}
        anchor["project_s"] = observed_s
        anchor["achieved_shift_s"] = echo_project + delta - observed_s
        anchor["residual_s"] = echo_project + delta - target
        return _stamp(child, plan, "insert_seam", changes), changes

    cut_project = attack_s - lead
    new_position = cut_project + delta
    crossfade = min(abs(delta), 0.025)
    left_length = cut_project - start
    if delta > 0.0:
        left_length += delta + crossfade
    if left_length < MIN_CLIP_S or cut_project <= start + 1e-9:
        return _seam_conflict(child, max(index - 1, 0), cut_project, 0.0,
                              "clip_too_short",
                              f"the cut at {cut_project:.6f} s leaves "
                              f"{left_length:.6f} s in front of it", 0.0)

    later = []
    for item in child["anchors"]:
        if not isinstance(item, Mapping):
            continue
        time_s = _anchor_time(item, "project_s")
        if attack_s + 1e-9 < time_s < end - 1e-9:
            later.append(time_s)
    tail_cut = (min(later) - lead) if later else None
    # A tail shorter than one clip cannot be written, so the new clip simply
    # carries the rest of the block and the source stays contiguous.
    if tail_cut is not None and (end - tail_cut) < MIN_CLIP_S:
        tail_cut = None
    cut_source = source + (cut_project - start)
    tail_length = (end - tail_cut) if tail_cut is not None else 0.0
    tail_source = (source + (tail_cut - start)) if tail_cut is not None else None
    # Source must stay contiguous: the new clip reads to the tail's first
    # sample, so the timeline overlap (delta) lives in the crossfade, not in a
    # second read of the same audio.
    # Geometry that the apply() validator accepts for either direction:
    #   timeline overlap at the cut  == both fades            (crossfade)
    #   source overlap at the cut    == delta + crossfade     (<= MAX_SEAM_OVERLAP_S)
    #   source skip at the tail      == delta - tail_fade     (<= MAX_SOURCE_SKIP)
    tail_fade = 0.004 if tail_cut is not None else 0.0
    if delta > 0.0:
        tail_fade = max(tail_fade, min(delta - 0.020, 0.030))
    if tail_cut is not None:
        right_length = (tail_cut + tail_fade) - new_position
    else:
        right_length = end - new_position
    # Old-coordinate end of the region the new clip carries: everything before
    # it moves with the clip, everything after it belongs to the tail.
    right_end = cut_project + right_length
    if right_length < MIN_CLIP_S:
        return _seam_conflict(child, max(index - 1, 0), cut_project, delta,
                              "clip_too_short",
                              f"the new clip would be {right_length:.6f} s", 0.0)
    cut_source = source + (cut_project - start)
    tail_length = (end - tail_cut) if tail_cut is not None else 0.0
    if duration is not None:
        for label, start_source, span in (("new clip", cut_source, right_length),
                                          ("tail", tail_source, tail_length)):
            if start_source is None:
                continue
            if start_source + span > duration + 1e-9:
                return _seam_conflict(child, max(index - 1, 0), cut_project, delta,
                                      "insufficient_source",
                                      f"the {label} would read to "
                                      f"{start_source + span:.6f} s of {duration:.6f} s", 0.0)

    left = dict(block)
    left["length_s"] = round(left_length, 6)
    left["fade_out_s"] = round(crossfade, 6)
    right = {"position_s": round(new_position, 6),
             "source_start_s": round(cut_source, 6),
             "length_s": round(right_length, 6),
             "fade_in_s": round(crossfade, 6),
             "fade_out_s": round(tail_fade, 6), "event_ids": []}
    pieces = [left, right]
    new_seams = [round(new_position, 6)]
    if tail_cut is not None and tail_length >= MIN_CLIP_S:
        tail = {"position_s": round(tail_cut, 6),
                "source_start_s": round(tail_source, 6),
                "length_s": round(tail_length, 6),
                "fade_in_s": round(tail_fade, 6),
                "fade_out_s": block.get("fade_out_s") or 0.0,
                "event_ids": []}
        pieces.append(tail)
        new_seams.append(round(tail_cut, 6))

    moved = []
    for item in child["anchors"]:
        if not isinstance(item, Mapping):
            continue
        time_s = _anchor_time(item, "project_s")
        if cut_project - 1e-9 <= time_s < right_end - 1e-9:
            moved.append({"event_id": item.get("id"),
                          "previous_project_s": round(time_s, 6),
                          "project_s": round(time_s + delta, 6)})
            if isinstance(item, MutableMapping):
                item["project_s"] = time_s + delta
            right["event_ids"].append(item.get("id"))
        elif right_end - 1e-9 <= time_s < end - 1e-9 and len(pieces) == 3:
            pieces[2]["event_ids"].append(item.get("id"))

    blocks[index:index + 1] = pieces
    for offset, value in enumerate(new_seams):
        seams.insert(index + offset, value)

    changes = {
        "action": "insert_seam",
        "event_id": event_id,
        "applied": True,
        "clamped": abs(delta - wanted) > 1e-9,
        "shift_s": round(delta, 6),
        "cut_project_s": round(cut_project, 6),
        "cut_source_s": round(cut_source, 6),
        "tail_cut_s": None if tail_cut is None else round(tail_cut, 6),
        "lead_s": round(lead, 6),
        "block_count": len(blocks),
        "blocks_changed": [
            {"index": index, "length_s": left["length_s"],
             "previous_length_s": round(length, 6)},
            {"index": index + 1, "position_s": right["position_s"],
             "source_start_s": right["source_start_s"],
             "length_s": right["length_s"]},
        ] + ([{"index": index + 2, "position_s": pieces[2]["position_s"],
               "source_start_s": pieces[2]["source_start_s"],
               "length_s": pieces[2]["length_s"]}] if len(pieces) == 3 else []),
        "anchors_moved": moved,
        "conflict": None,
    }
    anchor["project_s"] = observed_s
    anchor["achieved_shift_s"] = echo_project + delta - observed_s
    anchor["residual_s"] = echo_project + delta - target
    return _stamp(child, plan, "insert_seam", changes), changes
