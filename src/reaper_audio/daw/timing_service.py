"""Project-facing timing analysis: resolve a take, measure, match, plan.

Pure DSP and geometry live in :mod:`reaper_audio.analysis.timing`; this module
adds project coordinates, a tempo map and a deterministic analysis registry.
It never edits the project.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from typing import Any, Mapping, Sequence
from uuid import uuid4

from ..analysis.audio import load_audio
from ..analysis.timing import (build_timing_plan, detect_timing_events,
                               grid_times, match_events)
from ..analysis.timing_context import plan_phrase
from ..daw.midi import TempoMap
from ..errors import ValidationError

ANALYSES: dict[str, dict[str, Any]] = {}
PLANS: dict[str, dict[str, Any]] = {}


def _find_item(snapshot: Mapping[str, Any], item_guid: str | None,
               track_guid: str | None) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    for track in snapshot.get("tracks") or []:
        if not isinstance(track, Mapping):
            continue
        if track_guid and track.get("guid") != track_guid:
            continue
        for item in track.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            if item_guid and item.get("guid") != item_guid:
                continue
            if item.get("active_take") and item["active_take"].get("is_midi"):
                continue
            return track, item
    raise ValidationError("No audio item matches the requested GUIDs",
                          details={"track_guid": track_guid, "item_guid": item_guid})


def _source_window(item: Mapping[str, Any], start_s: float | None,
                   end_s: float | None) -> tuple[float, float]:
    position = float(item.get("position_s") or 0.0)
    length = float(item.get("length_s") or 0.0)
    start = position if start_s is None else max(position, float(start_s))
    end = position + length if end_s is None else min(position + length, float(end_s))
    if end <= start:
        raise ValidationError("Requested range does not intersect the item",
                              details={"position_s": position, "length_s": length,
                                       "start_s": start_s, "end_s": end_s})
    return start, end


def analyze_timing(session: Any, *, project_id: str | None,
                   track_guid: str | None, item_guid: str | None,
                   start_s: float | None = None, end_s: float | None = None,
                   profile: str = "auto", sensitivity: float = 1.8,
                   min_gap_s: float = 0.03) -> dict[str, Any]:
    """Measure attack candidates in one audio take and map them to project time."""
    snapshot = session.snapshot(project_id=project_id, include_items=True)
    track, item = _find_item(snapshot, item_guid, track_guid)
    take = item.get("active_take") or {}
    source_path = take.get("source_path") or item.get("source_path")
    if not source_path:
        raise ValidationError("The selected item has no file-backed active take",
                              details={"item_guid": item.get("guid")})
    position = float(item.get("position_s") or 0.0)
    offset = float(item.get("start_offset_s") or 0.0)
    playrate = float(item.get("playrate") or 1.0)
    if abs(playrate - 1.0) > 1e-6:
        raise ValidationError("Timing analysis currently supports 1x takes only",
                              details={"playrate": playrate})
    window_start, window_end = _source_window(item, start_s, end_s)
    source_start = offset + (window_start - position)
    source_end = source_start + (window_end - window_start)
    audio = load_audio(source_path)
    analyzed = detect_timing_events(audio, sensitivity=sensitivity,
                                    min_gap_s=min_gap_s, profile=profile)
    events = []
    for row in analyzed["events"]:
        source_s = float(row["source_s"])
        if source_start - 1e-9 <= source_s <= source_end + 1e-9:
            event = dict(row)
            event["source_s"] = round(source_s, 7)
            event["project_s"] = round(position + (source_s - offset), 7)
            events.append(event)
    analysis_id = f"tan_{uuid4().hex[:16]}"
    result = {
        **analyzed,
        "analysis_id": analysis_id,
        "project_id": snapshot.get("project", {}).get("project_id", project_id),
        "track_guid": track.get("guid"),
        "track_name": track.get("name"),
        "item_guid": item.get("guid"),
        "take_guid": take.get("guid"),
        "source_path": str(source_path),
        "source_offset_s": round(offset, 7),
        "position_s": round(position, 7),
        "project_window": {"start_s": round(window_start, 7),
                           "end_s": round(window_end, 7)},
        "events": events,
        "event_count": len(events),
    }
    ANALYSES[analysis_id] = result
    return result


def _choose_grid(events: Sequence[Mapping[str, Any]], tempo: TempoMap,
                 start_s: float, end_s: float, tolerance_s: float,
                 subdivisions: int | Sequence[int]) -> tuple[dict[str, Any],
                                                             list[dict[str, float]],
                                                             int]:
    candidates = ([4] if isinstance(subdivisions, int) else list(subdivisions))
    best = None
    for division in candidates:
        grid = grid_times(tempo, start_s, end_s, division)
        matched = match_events(events, grid, tolerance_s=tolerance_s)
        score = (matched["matched"], -(matched["median_abs_offset_s"] or 0.0))
        if best is None or score > best[0]:
            best = (score, matched, grid, int(division))
    assert best is not None
    _score, matched, grid, division = best
    return matched, grid, division


def plan_timing(analysis_id: str, *, tempo_context: Mapping[str, Any],
                subdivisions: int | Sequence[int] = (4, 3, 6, 8),
                tolerance_s: float = 0.080, strength: float = 1.0,
                group_gap_s: float = 0.25,
                group_shift_s: float = 0.012,
                protected_ranges: Sequence[Mapping[str, Any]] = (),
                reference_events: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Match one stored analysis to the project grid and return a plan."""
    analysis = ANALYSES.get(analysis_id)
    if analysis is None:
        raise ValidationError("Unknown timing analysis_id",
                              details={"analysis_id": analysis_id})
    tempo = TempoMap.from_context(tempo_context)
    events = analysis["events"]
    if not events:
        raise ValidationError("The analysis contains no events")
    start_s = float(analysis["project_window"]["start_s"])
    end_s = float(analysis["project_window"]["end_s"])
    project_events = [{**event, "source_s": event["project_s"]}
                      for event in events]
    if reference_events:
        assignments = []
        for index, reference in enumerate(reference_events):
            if index >= len(project_events):
                break
            event = project_events[index]
            target = reference.get("target_s")
            if target is None and reference.get("qn") is not None:
                target = tempo.qn_to_time(float(reference["qn"]))
            if target is None:
                continue
            assignments.append({"event_index": index,
                                "grid_index": index,
                                "event_s": float(event["source_s"]),
                                "target_s": float(target),
                                "qn": float(tempo.time_to_qn(float(target))),
                                "offset_s": float(target) - float(event["source_s"])})
        matched = {"assignments": assignments, "matched": len(assignments),
                   "skipped_events": [], "skipped_grid": [],
                   "median_abs_offset_s": (float(np.median(
                       [abs(row["offset_s"]) for row in assignments]))
                       if assignments else None)}
        grid = list(reference_events)
        division = 0
    else:
        matched, grid, division = _choose_grid(project_events, tempo, start_s, end_s,
                                               tolerance_s, subdivisions)
    position = float(analysis["position_s"])
    offset = float(analysis["source_offset_s"])
    source_assignments = []
    for row in matched["assignments"]:
        source_target = offset + (float(row["target_s"]) - position)
        event_s = float(events[int(row["event_index"])]["source_s"])
        source_assignments.append({**row, "event_s": event_s,
                                   "target_s": source_target,
                                   "offset_s": source_target - event_s})
    plan = build_timing_plan(
        events, source_assignments, analysis_id=analysis_id,
        strength=strength, duration_s=float(analysis["duration_s"]),
        group_gap_s=group_gap_s, group_shift_s=group_shift_s,
        protected_ranges=protected_ranges,
    )
    # Source geometry is relative to the take; expose project positions too.
    for block in plan["blocks"]:
        block["project_source_start_s"] = round(position + block["start_s"] - offset, 7)
        block["project_target_start_s"] = round(
            position + block["target_start_s"] - offset, 7)
        block["project_target_end_s"] = round(
            position + block["target_end_s"] - offset, 7)
    # Keep the losing options visible for phrase-level decisions.
    alternatives = []
    for index, block in enumerate(plan["blocks"], start=1):
        ids = set(block["event_ids"])
        rows = [event for event in events if str(event.get("id")) in ids]
        targets = {}
        for row in matched["assignments"]:
            event = events[int(row["event_index"])]
            if event.get("id") in ids:
                targets[str(event["id"])] = offset + (float(row["target_s"]) - position)
        if not rows:
            continue
        phrase_events = [{
            "id": row["id"], "phrase": f"block_{index}",
            "start_s": float(row["source_s"]),
            "end_s": min(float(analysis["duration_s"]),
                         float(row["source_s"]) + 0.25),
            "boundary_end_s": block["end_s"],
        } for row in rows]
        options = plan_phrase(phrase_events, targets,
                              phrase_key="phrase", strength=strength,
                              preserve_duration=True,
                              protected_ranges=protected_ranges)
        if not options.get("selected"):
            options["selected"] = [{"event_id": row["id"], "shift_s": 0.0,
                                    "start_s": float(row["source_s"])}
                                   for row in rows]
            options.setdefault("issues", []).append({
                "code": "leave_selected",
                "phrase": f"block_{index}",
                "next_action": "audition the unshifted phrase or lower strength",
            })
        alternatives.append({"block": index, "options": options})
        for issue in options.get("issues", []):
            if issue.get("code") == "duration_preserved_by_smaller_shift":
                block["duration_preserved_by_smaller_shift"] = True
    plan.update({
        "alternatives": alternatives,
        "plan_id": f"tpl_{uuid4().hex[:16]}",
        "tempo_map_source": getattr(tempo, "source", "project"),
        "subdivision": division,
        "target_kind": "reference_events" if reference_events else "project_grid",
        "grid_points": len(grid),
        "match": matched,
        "project_id": analysis.get("project_id"),
        "track_guid": analysis.get("track_guid"),
        "item_guid": analysis.get("item_guid"),
    })
    PLANS[plan["plan_id"]] = plan
    return plan


def inspect_timing(analysis_id: str | None = None,
                   plan_id: str | None = None) -> dict[str, Any]:
    if plan_id:
        plan = PLANS.get(plan_id)
        if plan is None:
            raise ValidationError("Unknown timing plan_id", details={"plan_id": plan_id})
        return {"schema": "reaper-workbench.timing-inspect/1", "plan": plan}
    if analysis_id:
        analysis = ANALYSES.get(analysis_id)
        if analysis is None:
            raise ValidationError("Unknown timing analysis_id",
                                  details={"analysis_id": analysis_id})
        return {"schema": "reaper-workbench.timing-inspect/1", "analysis": analysis}
    raise ValidationError("Pass analysis_id or plan_id")
