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
from ..analysis.timing_seams import attack_candidates, plan_crossfades
from ..analysis.timing_rhythm import DEFAULT_DIVISIONS, infer_rhythm
from ..daw.midi import TempoMap
from ..errors import ValidationError

ANALYSES: dict[str, dict[str, Any]] = {}
PLANS: dict[str, dict[str, Any]] = {}
RHYTHMS: dict[str, dict[str, Any]] = {}


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
                   min_gap_s: float = 0.03,
                   method: str = "legacy") -> dict[str, Any]:
    """Measure attack candidates in one audio take and map them to project time."""
    snapshot = session.snapshot(project_id=project_id, include_items=True)
    track, item = _find_item(snapshot, item_guid, track_guid)
    take = item.get("active_take") or {}
    source_path = take.get("source_path") or take.get("source") or item.get("source_path")
    if not source_path:
        raise ValidationError("The selected item has no file-backed active take",
                              details={"item_guid": item.get("guid")})
    position = float(item.get("position_s") or 0.0)
    offset = float(item.get("start_offset_s") or 0.0)
    playrate = float(take.get("playrate", item.get("playrate", 1.0)))
    if abs(playrate - 1.0) > 1e-6:
        raise ValidationError("Timing analysis currently supports 1x takes only",
                              details={"playrate": playrate})
    window_start, window_end = _source_window(item, start_s, end_s)
    source_start = offset + (window_start - position)
    source_end = source_start + (window_end - window_start)
    audio = load_audio(source_path)
    if method == "crossfade":
        candidates = attack_candidates(audio, profile, sensitivity, min_gap_s)
        analyzed = {"schema": "reaper-workbench.timing-analysis/1",
                    "profile": profile, "sample_rate": audio.sample_rate,
                    "duration_s": len(audio.samples) / audio.sample_rate,
                    "events": candidates, "coverage": "available" if candidates else "no_events"}
    else:
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
        "method": method,
        "item_length_s": item.get("length_s"),
        "edge_fades_s": [item.get("fade_in_s") or 0., item.get("fade_out_s") or 0.],
        "take_count": item.get("take_count", 1),
        "take_fx_count": take.get("fx_count", 0),
        "source_fingerprint": list(_file_fingerprint(Path(source_path))),
        "project_id": (snapshot.get("revision", {}).get("project_id")
                       or snapshot.get("project", {}).get("project_id") or project_id),
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
    candidates = ([subdivisions] if isinstance(subdivisions, int) else list(subdivisions))
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


def _file_fingerprint(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def validate_rhythm_analysis(analysis_id, snapshot):
    analysis = ANALYSES.get(analysis_id)
    project = (snapshot.get("revision") or {}).get("project_id")
    if not analysis or analysis.get("method") != "crossfade" or analysis["project_id"] != project:
        raise ValidationError("Analyze the take with method=crossfade in this project first",
                              details={"analysis_id": analysis_id})
    _, item = _find_item(snapshot, analysis["item_guid"], analysis["track_guid"])
    take = item.get("active_take") or {}
    if (take.get("guid") != analysis["take_guid"] or
            Path(take.get("source_path") or take.get("source") or "") !=
            Path(analysis["source_path"]) or
            abs(float(take.get("playrate", item.get("playrate", 1.0)))-1) > 1e-6 or
            (take.get("stretch_markers") or item.get("stretch_marker_positions")) or
            any(abs(float(item[field])-float(analysis[key])) > 1e-6
                for field, key in (("position_s", "position_s"),
                                   ("start_offset_s", "source_offset_s"),
                                   ("length_s", "item_length_s"))) or
            list(_file_fingerprint(Path(analysis["source_path"]))) != analysis["source_fingerprint"]):
        raise ValidationError("Take or source changed since analysis; analyze the current item",
                              details={"analysis_id": analysis_id})
    return analysis


def build_timing_rhythm(analysis_id, *, snapshot, subdivisions=None, tolerance_s=.046,
                        grid_phase_s=0., context_analysis_ids=()):
    """Store rhythm evidence against live source geometry and the native tempo map."""
    analysis = validate_rhythm_analysis(analysis_id, snapshot)
    tempo_context = snapshot["tempo"]
    tempo = TempoMap.from_context(tempo_context)
    if len(context_analysis_ids) > 8 or analysis_id in context_analysis_ids:
        raise ValidationError("Use up to eight separate context analyses")
    context_events = []
    for aid in dict.fromkeys(context_analysis_ids):
        context = validate_rhythm_analysis(aid, snapshot)
        if context["item_guid"] == analysis["item_guid"]:
            raise ValidationError("Use a separate item for rhythm context")
        context_events.extend(context["events"])
    window = analysis["project_window"]
    segments = infer_rhythm(
        load_audio(analysis["source_path"]), analysis["events"], tempo,
        start_s=window["start_s"], end_s=window["end_s"],
        source_origin=analysis["position_s"]-analysis["source_offset_s"],
        subdivisions=DEFAULT_DIVISIONS if subdivisions is None else subdivisions,
        phase_s=grid_phase_s, tolerance_s=tolerance_s, profile=analysis["profile"],
        context_events=context_events)
    rhythm_id = f"try_{uuid4().hex[:16]}"
    RHYTHMS[rhythm_id] = {"schema": "reaper-workbench.timing-rhythm/1",
                          "rhythm_id": rhythm_id, "analysis_id": analysis_id,
                          "project_id": analysis["project_id"], "item_guid": analysis["item_guid"],
                          "window": window, "grid_phase_s": grid_phase_s,
                          "tolerance_s": tolerance_s, "tempo_context": dict(tempo_context),
                          "context_analysis_ids": list(context_analysis_ids), "segments": segments}
    return rhythm_id


def read_timing_rhythm(rhythm_id, *, offset=0, limit=8):
    rhythm = RHYTHMS.get(rhythm_id)
    if rhythm is None:
        raise ValidationError("Unknown rhythm_id", details={"rhythm_id": rhythm_id})
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 32:
        raise ValidationError("Rhythm page needs offset >= 0 and limit between 1 and 32")
    segments = rhythm["segments"]
    return {**rhythm, "segments": segments[offset:offset+limit], "segment_count": len(segments),
            "offset": offset, "next_offset": offset+limit if offset+limit < len(segments) else None,
            "status_counts": {status: sum(s["status"] == status for s in segments)
                              for status in ("supported", "ambiguous", "unresolved")}}


def _select_rhythm_targets(rhythm_id, choices, analysis_id, tempo_context, phase_s):
    rhythm = RHYTHMS.get(rhythm_id)
    if rhythm is None or rhythm["analysis_id"] != analysis_id:
        raise ValidationError("rhythm_id must belong to the requested timing analysis")
    if rhythm["tempo_context"] != tempo_context or rhythm["grid_phase_s"] != phase_s:
        raise ValidationError("Tempo map or phase differs from the rhythm map")
    known = {s["id"] for s in rhythm["segments"]}
    if set(choices)-known:
        raise ValidationError("Unknown segment in rhythm_choices",
                              details={"unknown": sorted(set(choices)-known)})
    targets, keep, selected = [], [], {}
    for segment in rhythm["segments"]:
        choice = choices.get(segment["id"], segment["recommended_option"] or "keep")
        selected[segment["id"]] = choice
        if choice == "keep":
            start, end = segment["start_s"], segment["end_s"]
            if keep and abs(keep[-1]["end_s"]-start) <= 1e-8:
                keep[-1]["end_s"] = end
            else:
                keep.append({"start_s": start, "end_s": end})
            continue
        option = next((o for o in segment["options"] if o["id"] == choice), None)
        if option is None:
            raise ValidationError("Unknown rhythm option",
                                  details={"segment_id": segment["id"], "option": choice})
        targets.extend({"event_id": a["event_id"], "target_s": a["target_s"]}
                       for a in option["anchors"])
    return targets, keep, selected


def _explicit_targets(references, analysis, tempo, phase_s, tolerance):
    events = {e["id"]: e for e in analysis["events"]}
    targets = {}
    window = analysis["project_window"]
    for row in references:
        event_id = row.get("event_id")
        if not isinstance(event_id, str) or event_id not in events or event_id in targets:
            raise ValidationError("Each explicit target needs a distinct measured event_id")
        keys = [key for key in ("target_s", "qn") if key in row]
        if len(keys) != 1:
            raise ValidationError("Pass exactly one target_s or qn for each event")
        key = keys[0]
        raw = row[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not np.isfinite(raw):
            raise ValidationError("Explicit target must be a finite number")
        target = float(raw) if key == "target_s" else tempo.qn_to_time(float(raw))+phase_s
        if (not window["start_s"]-1e-8 <= target < window["end_s"] or
                abs(events[event_id]["project_s"]-target) > tolerance+1e-8):
            raise ValidationError("Explicit target exceeds the item window or timing tolerance",
                                  details={"event_id": event_id, "target_s": target})
        targets[event_id] = target
    ordered = [targets[e["id"]] for e in analysis["events"] if e["id"] in targets]
    if any(b <= a for a, b in zip(ordered, ordered[1:])):
        raise ValidationError("Explicit targets must preserve attack order and be distinct")
    if not targets:
        raise ValidationError("No selected rhythm anchors; keep this item or choose an option")
    return targets


def plan_timing_crossfades(analysis_id: str, *, tempo_context: Mapping[str, Any],
                           subdivision: int, tolerance_s: float, strength: float,
                           grid_phase_s: float | None = None,
                           protected_ranges: Sequence[Mapping[str, Any]] = (),
                           project_id: str | None = None,
                           rhythm_id: str | None = None,
                           rhythm_choices: Mapping[str, str] | None = None,
                           reference_events: Sequence[Mapping[str, Any]] | None = None
                           ) -> dict[str, Any]:
    analysis = ANALYSES.get(analysis_id)
    if (analysis is None or analysis.get("method") != "crossfade" or
            (project_id and analysis["project_id"] != project_id)):
        raise ValidationError("Analyze the source item with method=crossfade first")
    if not 1 <= subdivision <= 32 or not 0 < tolerance_s <= .25 or not 0 <= strength <= 1:
        raise ValidationError("Invalid subdivision, tolerance or strength")
    if grid_phase_s is None:
        grid_phase_s = (RHYTHMS.get(rhythm_id or "") or {}).get("grid_phase_s", 0.)
    if not np.isfinite(grid_phase_s):
        raise ValidationError("grid_phase_s must be finite")
    position, offset = analysis["position_s"], analysis["source_offset_s"]
    length = float(analysis["item_length_s"])
    window = analysis["project_window"]
    if abs(window["start_s"]-position) > 1e-6 or abs(window["end_s"]-position-length) > 1e-6:
        raise ValidationError("Analyze the entire item before planning a replacement")
    source = Path(analysis["source_path"])
    if list(_file_fingerprint(source)) != analysis["source_fingerprint"]:
        raise ValidationError("Source audio changed since timing analysis")
    if analysis["take_count"] != 1 or analysis["take_fx_count"]:
        raise ValidationError("Crossfade replacement requires a single take without take FX")
    if offset < -1e-6 or offset+length > analysis["duration_s"]+1e-6:
        raise ValidationError("The item window exceeds the source audio",
                              details={"source_window_s": [offset, offset+length],
                                       "source_duration_s": analysis["duration_s"]})
    tempo = TempoMap.from_context(tempo_context)
    if rhythm_id and reference_events is not None:
        raise ValidationError("Choose a rhythm map or explicit references for this plan")
    if rhythm_choices and not rhythm_id:
        raise ValidationError("rhythm_choices requires rhythm_id")
    choices = {}
    if rhythm_id:
        reference_events, keep, choices = _select_rhythm_targets(
            rhythm_id, rhythm_choices or {}, analysis_id, tempo_context, grid_phase_s)
        protected_ranges = [*protected_ranges, *keep]
    targets = (_explicit_targets(reference_events, analysis, tempo, grid_phase_s, tolerance_s)
               if reference_events is not None else None)
    grid = ([row["time_s"] + grid_phase_s for row in grid_times(
        tempo, position-grid_phase_s, position+length-grid_phase_s, subdivision)]
            if targets is None else [])
    result = plan_crossfades(
        load_audio(source), analysis["events"], grid, position=position, offset=offset,
        length=length, tolerance=tolerance_s, strength=strength, profile=analysis["profile"],
        protected_ranges=protected_ranges, explicit_targets=targets)
    first, last = result["blocks"][0], result["blocks"][-1]
    edge_in, edge_out = analysis["edge_fades_s"]
    first["fade_in_s"] = min(edge_in, first["length_s"]-first["fade_out_s"])
    last["fade_out_s"] = min(edge_out, last["length_s"]-last["fade_in_s"])
    result.update({"schema": "reaper-workbench.timing-plan/1", "method": "crossfade",
                   "tempo_context": dict(tempo_context),
                   "plan_id": f"tpl_{uuid4().hex[:16]}", "analysis_id": analysis_id,
                   "project_id": analysis["project_id"], "track_guid": analysis["track_guid"],
                   "item_guid": analysis["item_guid"], "take_guid": analysis["take_guid"],
                   "source_path": str(source), "source_fingerprint": analysis["source_fingerprint"],
                   "item_position_s": position, "source_offset_s": offset, "item_length_s": length,
                   "duration_s": analysis["duration_s"],
                   "subdivision": subdivision if targets is None else None,
                   "grid_phase_s": grid_phase_s,
                   "target_kind": ("rhythm_map" if rhythm_id else "reference_events"
                                   if targets is not None else "project_grid"),
                   "rhythm_id": rhythm_id, "rhythm_choices": choices,
                   "protected_ranges": list(protected_ranges)})
    if targets is not None:
        # Reject unsafe geometry while the agent can still revise the choice.
        from .timing_apply import check_coverage, check_protected_ranges, normalize_clips
        origin = position-offset
        clips = normalize_clips(result["blocks"], project_start_s=origin)
        item = {"position_s": position, "start_offset_s": offset, "length_s": length}
        result["coverage_check"] = check_coverage(clips, item, crossfade=True)
        result["protected_checks"] = check_protected_ranges(
            clips, item, protected_ranges, project_start_s=origin)
    PLANS[result["plan_id"]] = result
    return result


def measure_timing_clock(session: Any, *, project_id: str | None, item_guid: str,
                         start_s: float | None, end_s: float | None) -> dict[str, Any]:
    """Compare short isolated click attacks with the native project beat grid."""
    snap = session.snapshot(project_id=project_id, include_items=True)
    _, item = _find_item(snap, item_guid, None)
    start, end = _source_window(item, start_s, end_s)
    if end-start > 30.:
        raise ValidationError("Measure a click-only window of up to 30 seconds")
    take = item.get("active_take") or {}
    if abs(float(take.get("playrate", 1.))-1.) > 1e-6:
        raise ValidationError("Clock measurement expects a 1x click take")
    audio = load_audio(take.get("source") or take.get("source_path"))
    rate = audio.sample_rate
    origin = item["position_s"]-item["start_offset_s"]
    grid = grid_times(TempoMap.from_context(snap["tempo"]), start, end, 1)
    samples = np.asarray(audio.samples)
    energy = np.max(np.abs(samples), axis=1) if samples.ndim == 2 else np.abs(samples)
    rows = []
    for beat in grid:
        time = beat["time_s"]
        lo = max(0, round((time-origin-.11)*rate))
        hi = min(len(energy), round((time-origin+.11)*rate))
        values = energy[lo:hi]
        if not len(values):
            continue
        peak = float(np.max(values))
        baseline = float(np.percentile(values, 25))
        if peak <= max(1e-5, baseline*8):
            continue
        on = float(origin+(lo+np.argmax(values >= .12*peak))/rate)
        rows.append({"beat_s": time, "attack_s": on, "offset_s": on-time, "peak": peak})
    offsets = [r["offset_s"] for r in rows]
    return {"schema": "reaper-workbench.timing-clock/1",
            "measurement_id": f"tclock_{uuid4().hex[:16]}", "beats": rows,
            "median_phase_s": float(np.median(offsets)) if offsets else None,
            "offset_range_s": [min(offsets), max(offsets)] if offsets else None}


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
