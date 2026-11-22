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
from ..analysis.timing_review import review_page, review_plan
from ..daw.midi import TempoMap
from ..errors import ValidationError
from ..models import Section

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


def inspect_timing_waveform(session: Any, *, project_id: str | None,
                            item_guid: str | None, track_guid: str | None,
                            start_s: float | None, end_s: float | None,
                            analysis_id: str | None = None, plan_id: str | None = None,
                            bins: int = 256, subdivisions: Sequence[int] | None = None,
                            grid_phase_s: float | None = None) -> dict[str, Any]:
    """Read a continuous pre-FX window from the original 1x take."""
    if (start_s is None or end_s is None or not np.isfinite([start_s, end_s]).all()
            or not 0 < end_s - start_s <= 4.0):
        raise ValidationError("Waveform needs a project-time window of at most four seconds")
    if isinstance(bins, bool) or not isinstance(bins, int) or not 1 <= bins <= 1024:
        raise ValidationError("waveform_bins must be between 1 and 1024")
    if plan_id is not None:
        plan = PLANS.get(plan_id)
        if not plan or not plan.get("analysis_id"):
            raise ValidationError("Waveform needs a live timing plan")
        if analysis_id is not None and analysis_id != plan["analysis_id"]:
            raise ValidationError("The plan and analysis do not refer to the same input")
        analysis_id = plan["analysis_id"]
    else:
        plan = None
    if item_guid is None and analysis_id is None:
        raise ValidationError("Waveform needs item_guid, analysis_id or plan_id")

    snapshot = session.snapshot(project_id=project_id, include_items=True)
    analysis = validate_rhythm_analysis(analysis_id, snapshot) if analysis_id else None
    if analysis:
        if ((item_guid is not None and item_guid != analysis["item_guid"])
                or (track_guid is not None and track_guid != analysis["track_guid"])):
            raise ValidationError("The selected item or track differs from the timing analysis")
        item_guid, track_guid = analysis["item_guid"], analysis["track_guid"]
    track, item = _find_item(snapshot, item_guid, track_guid)
    take = item.get("active_take") or {}
    source_path = take.get("source_path") or take.get("source") or item.get("source_path")
    playrate = float(take.get("playrate", item.get("playrate", 1.0)))
    if (not source_path or abs(playrate - 1.0) > 1e-6 or take.get("stretch_markers")
            or item.get("stretch_marker_positions") or item.get("source_length_is_qn")
            or item.get("reverse") or take.get("reverse") or item.get("source_reversed")):
        raise ValidationError("Waveform requires a forward file-backed 1x take without stretch markers")
    position = float(item["position_s"])
    offset = float(item.get("start_offset_s") or 0.0)
    low, high = _source_window(item, start_s, end_s)
    audio = load_audio(source_path, Section(offset + (low - position),
                                            offset + (high - position)))
    sample_rate = audio.sample_rate
    first_sample = round(audio.start_s * sample_rate)
    count = min(bins, len(audio.samples))
    boundaries = np.linspace(0, len(audio.samples), count + 1, dtype=int)
    levels = []
    for left, right in zip(boundaries, boundaries[1:]):
        span = audio.samples[left:right]
        levels.append([round(float(span.min()), 6), round(float(span.max()), 6),
                       round(float(np.sqrt(np.mean(span ** 2))), 6)])

    tempo_context = snapshot["tempo"]
    if plan and plan.get("tempo_context") != tempo_context:
        raise ValidationError("Project tempo changed since planning; prepare a fresh plan")
    tempo = TempoMap.from_context(tempo_context)
    phase = grid_phase_s if grid_phase_s is not None else (plan or {}).get("grid_phase_s", 0.0)
    divisions = tuple(dict.fromkeys(subdivisions if subdivisions is not None
                                    else (1, (plan or {}).get("subdivision") or 4)))
    if (not np.isfinite(phase) or not divisions
            or any(isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 64
                   for n in divisions)):
        raise ValidationError("Pass a finite grid phase and divisions between 1 and 64")
    first_qn, last_qn = tempo.time_to_qn(low - phase), tempo.time_to_qn(high - phase)
    if (not np.isfinite([first_qn, last_qn]).all()
            or np.spacing(max(abs(first_qn), abs(last_qn))) >= 1 / max(divisions)):
        raise ValidationError("Grid lies outside the supported numeric range")
    if sum(divisions) * (last_qn - first_qn) + len(divisions) > 1024:
        raise ValidationError("Grid is too dense for this window; zoom in or use fewer divisions")
    grid = [{"qn": row["qn"], "project_s": round(row["time_s"] + phase, 7),
             "subdivision": row["subdivision"]}
            for row in grid_times(tempo, low - phase, high - phase, divisions)]
    if plan:
        candidates = [row for row in plan["review_rows"] if low <= row["observed_s"] < high]
    else:
        candidates = [{**event, "observed_s": event["project_s"]}
                      for event in (analysis or {}).get("events", ())
                      if low <= event["project_s"] < high]
    return {
        "schema": "reaper-workbench.timing-waveform/1",
        "revision": snapshot.get("revision"), "track_guid": track["guid"],
        "item_guid": item["guid"], "take_guid": take.get("guid"), "source_path": str(source_path),
        "analysis_id": analysis_id, "plan_id": plan_id, "signal": "source_pre_fx",
        "project_window_s": [low, high], "sample_rate": sample_rate,
        "source_sample_boundaries": (boundaries + first_sample).tolist(),
        "item_position_s": position, "source_offset_s": offset,
        "level_columns": ["minimum", "maximum", "rms"], "levels": levels,
        "grid": grid, "grid_phase_s": phase,
        "candidates": candidates[:128], "candidate_count": len(candidates),
    }


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


def _explicit_targets(references, analysis, tempo, phase_s, tolerance, *, allow_empty=False):
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
    if not targets and not allow_empty:
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
    choices, keep = {}, []
    user_ranges = list(protected_ranges)
    if rhythm_id:
        reference_events, keep, choices = _select_rhythm_targets(
            rhythm_id, rhythm_choices or {}, analysis_id, tempo_context, grid_phase_s)
        protected_ranges = [*protected_ranges, *keep]
    targets = (_explicit_targets(reference_events, analysis, tempo, grid_phase_s, tolerance_s,
                                 allow_empty=bool(rhythm_id))
               if reference_events is not None else None)
    grid = ([row["time_s"] + grid_phase_s for row in grid_times(
        tempo, position-grid_phase_s, position+length-grid_phase_s, subdivision)]
            if targets is None else [])
    result = plan_crossfades(
        load_audio(source), analysis["events"], grid, position=position, offset=offset,
        length=length, tolerance=tolerance_s, strength=strength, profile=analysis["profile"],
        protected_ranges=protected_ranges, explicit_targets=targets,
        edge_fades=analysis["edge_fades_s"])
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
                   "strength": strength, "tolerance_s": tolerance_s,
                   "user_protected_ranges": user_ranges, "rhythm_keep_ranges": keep,
                   "protected_ranges": list(protected_ranges)})
    return _finish_crossfade_plan(result, analysis, tempo)


def _finish_crossfade_plan(plan, analysis, tempo):
    from .timing_apply import check_coverage, check_protected_ranges, normalize_clips

    position, offset = plan["item_position_s"], plan["source_offset_s"]
    origin = position-offset
    clips = normalize_clips(plan["blocks"], project_start_s=origin)
    item = {"position_s": position, "start_offset_s": offset, "length_s": plan["item_length_s"]}
    plan["coverage_check"] = check_coverage(clips, item, crossfade=True)
    plan["protected_checks"] = check_protected_ranges(
        clips, item, plan["protected_ranges"], project_start_s=origin)
    review_plan(plan, analysis, tempo, RHYTHMS.get(plan.get("rhythm_id")))
    PLANS[plan["plan_id"]] = plan
    return plan


def register_timing_refinement(plan):
    analysis = ANALYSES.get(plan.get("analysis_id"))
    if analysis:
        review_plan(plan, analysis, TempoMap.from_context(plan["tempo_context"]),
                    RHYTHMS.get(plan.get("rhythm_id")))
    PLANS[plan["plan_id"]] = plan


def compare_timing_reviews(plan_id, compare_plan_id, *, start_s, end_s, offset, limit):
    first = inspect_timing_review(plan_id, kind="all", start_s=start_s, end_s=end_s,
                                  offset=offset, limit=limit)
    left, right = PLANS[plan_id], PLANS.get(compare_plan_id)
    if not right or left.get("analysis_id") != right.get("analysis_id"):
        raise ValidationError("Compare plans made from the same source analysis")
    by_id = {row["event_id"]: row for row in right["review_rows"]}
    base_ids = {row["event_id"] for row in left["review_rows"]}
    right_only = [row for row in right["review_rows"] if row["event_id"] not in base_ids
                  and (start_s is None or row["observed_s"] >= start_s)
                  and (end_s is None or row["observed_s"] < end_s)]
    rows = [{"event_id": row["event_id"], "left": row, "right": by_id.get(row["event_id"])}
            for row in first["rows"]]
    return {**first, "compare_plan_id": compare_plan_id, "rows": rows,
            "right_only_count": len(right_only), "right_only": right_only[:limit]}


def crossfade_plan_view(plan, *, detail="summary"):
    if plan.get("method") != "crossfade":
        return plan
    excluded = {"review_rows"}
    if detail != "full":
        excluded.update({"blocks", "seams", "anchors", "phase", "diagnostics",
                         "protected_checks", "protected_ranges",
                         "user_protected_ranges", "rhythm_keep_ranges", "rhythm_choices"})
    return {**{k: v for k, v in plan.items() if k not in excluded},
            "block_count": len(plan["blocks"]), "seam_count": len(plan["seams"]),
            "protected_range_count": len(plan.get("protected_ranges", []))}


def inspect_timing_review(plan_id, *, kind, start_s=None, end_s=None, offset=0, limit=12):
    plan = PLANS.get(plan_id)
    if not plan or "review" not in plan:
        raise ValidationError("Review requires a crossfade timing plan_id")
    if (kind not in {"all", "residual", "unassigned"} or not 0 <= offset
            or not 1 <= limit <= 32 or (start_s is not None and not np.isfinite(start_s))
            or (end_s is not None and not np.isfinite(end_s))
            or (start_s is not None and end_s is not None and start_s >= end_s)):
        raise ValidationError("Invalid timing review page or interval")
    return {"schema": "reaper-workbench.timing-review/1", "plan_id": plan_id,
            "review": plan["review"],
            **review_page(plan, kind=kind, start_s=start_s, end_s=end_s,
                          offset=offset, limit=limit)}


def _outside_window(ranges, start, end):
    kept = []
    for row in ranges:
        lo, hi = row["start_s"], row["end_s"]
        if lo < start:
            kept.append({"start_s": lo, "end_s": min(start, hi)})
        if hi > end:
            kept.append({"start_s": max(lo, end), "end_s": hi})
    return kept


def _same_geometry(left, right):
    fields = ("position_s", "source_start_s", "length_s", "fade_in_s", "fade_out_s")
    return len(left) == len(right) and all(abs(a[k]-b[k]) <= 1e-7
                                        for a, b in zip(left, right) for k in fields)


def refine_timing(plan_id, *, snapshot, start_s, end_s, reference_events):
    base = PLANS.get(plan_id)
    if not base or base.get("method") != "crossfade":
        raise ValidationError("Refine requires a crossfade timing plan_id")
    if start_s is None or end_s is None or not (
            np.isfinite(start_s) and np.isfinite(end_s) and start_s < end_s):
        raise ValidationError("Refine takes a project-time interval with start_s and end_s")
    analysis = validate_rhythm_analysis(base["analysis_id"], snapshot)
    tempo_context = snapshot["tempo"]
    if tempo_context != base["tempo_context"]:
        raise ValidationError("Project tempo changed; build a new timing plan")
    tempo = TempoMap.from_context(tempo_context)
    targets = _explicit_targets(reference_events or (), analysis, tempo, base["grid_phase_s"],
                                base["tolerance_s"], allow_empty=True)
    original = base["blocks"]
    indexes = [i for i, block in enumerate(original)
               if block["position_s"] < end_s and
               block["position_s"]+block["length_s"] > start_s]
    if not indexes:
        raise ValidationError("Refine interval does not overlap the plan")
    lo, hi = indexes[0], indexes[-1]+1
    left, right = original[lo], original[hi-1]
    position, offset = base["item_position_s"], base["source_offset_s"]
    origin = position-offset
    first, last = left["position_s"], right["position_s"]+right["length_s"]
    first_shift = first-left["source_start_s"]-origin
    last_shift = right["position_s"]-right["source_start_s"]-origin
    fade_in, fade_out = left["fade_in_s"], right["fade_out_s"]
    low_source = left["source_start_s"]+fade_in
    high_source = right["source_start_s"]+right["length_s"]-fade_out
    interior = {event["id"] for event in analysis["events"]
                if low_source <= event["source_s"] <= high_source}
    if set(targets)-interior:
        raise ValidationError("Requested attacks touch a retained seam; widen the refine interval",
                              details={"event_ids": sorted(set(targets)-interior)})
    old_anchors = {a["id"]: a for a in base["anchors"]}
    local_goals = {i: a["target_s"] for i, a in old_anchors.items() if i in interior}
    local_goals.update(targets)
    if not local_goals:
        raise ValidationError("No assigned targets in this window; pass reference_events")
    ordered_goals = [local_goals[e["id"]] for e in analysis["events"] if e["id"] in local_goals]
    if any(b <= a for a, b in zip(ordered_goals, ordered_goals[1:])):
        raise ValidationError("Refined targets must preserve the attack order")
    release_start = max(low_source+origin, start_s-first_shift)
    release_end = min(high_source+origin, end_s-last_shift)
    if targets and release_end > release_start:
        keep = _outside_window(base["rhythm_keep_ranges"], release_start, release_end)
    else:
        keep = base["rhythm_keep_ranges"]
    protected = [*base["user_protected_ranges"], *keep]
    if any(not first+fade_in <= t <= last-fade_out for t in targets.values()):
        raise ValidationError("A requested target touches a retained fade; widen the interval")
    try:
        local = plan_crossfades(
            load_audio(base["source_path"]), analysis["events"], [], position=position,
            offset=offset, length=base["item_length_s"], tolerance=base["tolerance_s"],
            strength=base["strength"], profile=analysis["profile"], protected_ranges=protected,
            explicit_targets=local_goals, clip_window=(first, last, first_shift, last_shift),
            edge_fades=(fade_in, fade_out), fixed_targets=targets)
    except ValidationError as exc:
        raise ValidationError("Refinement cannot satisfy the requested targets and retain the seams",
                              details={"cause": str(exc), "parent_plan_id": plan_id,
                                       "editable_window_s": [first, last],
                                       "requested_event_ids": list(targets)}) from exc
    original_ids = {i for b in original[lo:hi] for i in b["event_ids"]}
    anchors = [a for a in base["anchors"] if a["id"] not in original_ids | interior]
    anchors.extend(local["anchors"])
    local_anchors = {a["id"]: a for a in local["anchors"]}
    comparison = []
    events_by_id = {e["id"]: e for e in analysis["events"]}
    for event_id, wanted in local_goals.items():
        old = old_anchors.get(event_id)
        new = local_anchors.get(event_id)
        event = events_by_id[event_id]
        source = event["source_s"]
        positions = [b["position_s"]+source-b["source_start_s"] for b in original[lo:hi]
                     if b["source_start_s"] <= source < b["source_start_s"]+b["length_s"]]
        previous = (old["project_s"]+old["achieved_shift_s"] if old else
                    positions[0] if len(positions) == 1 else None)
        comparison.append({"event_id": event_id, "observed_s": event["project_s"],
                           "target_s": wanted,
                           "old_error_s": previous-wanted if previous is not None else None,
                           "new_error_s": new["residual_s"] if new else None})
    plan = {**base, "plan_id": f"tpl_{uuid4().hex[:16]}", "parent_plan_id": plan_id,
            "blocks": [*original[:lo], *local["blocks"], *original[hi:]],
            "anchors": sorted(anchors, key=lambda a: a["project_s"]),
            "seams": [*base["seams"][:lo], *local["seams"], *base["seams"][hi-1:]],
            "diagnostics": [d for d in base["diagnostics"]
                            if not first <= d.get("position_s", -1) <= last]+local["diagnostics"],
            "rhythm_keep_ranges": keep, "protected_ranges": protected,
            "refinement": {"requested_window_s": [start_s, end_s],
                           "editable_window_s": [first, last],
                           "unchanged_outside_count": len(original)-(hi-lo),
                           "state": ("unchanged" if _same_geometry(local["blocks"], original[lo:hi])
                                     else "candidate"),
                           "events": comparison}}
    return _finish_crossfade_plan(plan, analysis, tempo)


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
        return {"schema": "reaper-workbench.timing-inspect/1",
                "plan": crossfade_plan_view(plan, detail="full")}
    if analysis_id:
        analysis = ANALYSES.get(analysis_id)
        if analysis is None:
            raise ValidationError("Unknown timing analysis_id",
                                  details={"analysis_id": analysis_id})
        return {"schema": "reaper-workbench.timing-inspect/1", "analysis": analysis}
    raise ValidationError("Pass analysis_id or plan_id")

def review_window(plan, *, start_s, end_s, context_beats=2.0):
    """Phrase-level view of one plan: every attack and every seam it touches.

    One event id survives analysis, planning and verification, so the caller can
    find the same attack in the source, in the plan and in the result without
    guessing from a rounded timecode.

    Each attack keeps the three time domains apart (source_s, project_s,
    target_s) and adds what a single median hides: every clip that plays the
    attack, its per-clip fade attenuation and the protected ranges that pinned
    it. Seam rows report the source cut, both fades and whether the outgoing
    clip still has source audio past its edge. When the plan names a readable
    source_path, every in-window attack also carries a short RMS envelope.
    """
    if not isinstance(plan, Mapping) or plan.get("method") != "crossfade":
        raise ValidationError("Window review needs a crossfade timing plan")
    lo, hi = float(start_s), float(end_s)
    if not np.isfinite(lo) or not np.isfinite(hi) or lo >= hi:
        raise ValidationError("Window review needs start_s < end_s")
    context = float(context_beats)
    if not np.isfinite(context) or context < 0.0:
        raise ValidationError("Window review needs a finite, non-negative context")
    position = float(plan["item_position_s"])
    offset = float(plan["source_offset_s"])
    item_length = float(plan["item_length_s"])
    source_total = float(plan.get("duration_s") or 0.0)
    blocks = list(plan.get("blocks") or [])
    anchors = list(plan.get("anchors") or [])

    def seam_seconds(value):
        if isinstance(value, Mapping):
            for key in ("project_s", "start_s", "time_s"):
                if value.get(key) is not None:
                    return float(value[key])
            raise ValidationError("Seam entry has no project time",
                                  details={"seam": dict(value)})
        return float(value)

    seams = sorted(seam_seconds(value) for value in (plan.get("seams") or []))
    ordered_blocks = sorted(blocks, key=lambda block: float(block["position_s"]))

    blockers: dict[Any, list[Any]] = {}
    review = plan.get("review")
    residual_page = review.get("residual") if isinstance(review, Mapping) else None
    for entry in ((residual_page.get("rows") if isinstance(residual_page, Mapping) else None)
                  or []):
        if not isinstance(entry, Mapping) or entry.get("event_id") is None:
            continue
        fixed = entry.get("fixed_by") or []
        if not isinstance(fixed, (list, tuple)):
            fixed = [fixed]
        bucket = blockers.setdefault(entry["event_id"], [])
        for range_id in fixed:
            if range_id not in bucket:
                bucket.append(range_id)

    samples = None
    sample_rate = 44100
    source_path = plan.get("source_path")
    if source_path:
        try:
            audio = load_audio(source_path)
            sample_rate = audio.sample_rate
            samples = audio.samples.mean(axis=1)
        except ValidationError:
            samples = None

    def attack_envelope(source_s):
        """RMS in dBFS every 2 ms across +/-40 ms around one source attack."""
        step, half = 0.002, 0.001
        frames = []
        for index in range(41):
            center = source_s - 0.040 + index * step
            low = int(round((center - half) * sample_rate))
            high = int(round((center + half) * sample_rate))
            low, high = max(0, min(len(samples), low)), max(0, min(len(samples), high))
            if high <= low:
                frames.append(-120.0)
                continue
            power = float(np.mean(samples[low:high] ** 2))
            frames.append(round(20.0 * float(np.log10(max(np.sqrt(power), 1e-6))), 2))
        return frames

    rows = []
    for anchor in anchors:
        project_s = float(anchor["project_s"])
        if not (lo - context <= project_s <= hi + context):
            continue
        target_s = float(anchor.get("target_s", project_s))
        source_s = float(anchor.get("source_s", offset + (project_s - position)))
        left = max([seam for seam in seams if seam <= project_s], default=position)
        right = min([seam for seam in seams if seam > project_s],
                    default=position + item_length)
        occurrences = []
        for index, clip in enumerate(blocks):
            start = float(clip["source_start_s"])
            end = start + float(clip["length_s"])
            if not start <= source_s < end:
                continue
            fade = 0.0
            fade_in = float(clip.get("fade_in_s") or 0.0)
            if fade_in > 0.0 and source_s < start + fade_in:
                fade = 1.0 - (source_s - start) / fade_in
            fade_out = float(clip.get("fade_out_s") or 0.0)
            if fade_out > 0.0 and source_s > end - fade_out:
                fade = max(fade, 1.0 - (end - source_s) / fade_out)
            occurrences.append({"block_index": index,
                                "project_s": round(float(clip["position_s"]) + source_s - start, 7),
                                "fade_attenuation": round(float(min(max(fade, 0.0), 1.0)), 4)})
        row = {
            "event_id": anchor["id"],
            "source_s": round(source_s, 6),
            "project_s": round(project_s, 6),
            "target_s": round(target_s, 6),
            "planned_s": round(project_s + float(anchor.get("achieved_shift_s") or 0.0), 7),
            "residual_s": round(float(anchor.get("residual_s", project_s - target_s)), 6),
            "in_window": bool(lo <= project_s <= hi),
            "confidence": anchor.get("confidence"),
            "seam_left_s": round(left, 6),
            "seam_right_s": round(right, 6),
            "occurrences": occurrences,
            "duplicated": len(occurrences) > 1,
            "under_fade": any(item["fade_attenuation"] > 0.0 for item in occurrences),
            "blockers": list(blockers.get(anchor["id"], [])),
        }
        if row["in_window"]:
            if samples is None:
                row["envelope_unavailable"] = True
            else:
                row["envelope_db"] = attack_envelope(source_s)
        rows.append(row)

    seam_rows = []
    for global_index, project_cut in enumerate(seams):
        if not (lo - context <= project_cut <= hi + context):
            continue
        incoming = next((block for block in ordered_blocks
                         if float(block["position_s"]) >= project_cut), None)
        previous = None
        for block in ordered_blocks:
            if float(block["position_s"]) < project_cut:
                previous = block
            else:
                break
        remaining = None
        exhausted = False
        if previous is not None and source_total > 0.0:
            left_audio = source_total - (float(previous["source_start_s"]) +
                                         float(previous["length_s"]))
            if left_audio <= 0.0:
                remaining, exhausted = 0.0, True
            else:
                remaining = round(left_audio, 6)
        seam_rows.append({
            "seam_index": global_index,
            "project_s": round(project_cut, 6),
            "cut_source_s": round(offset + (project_cut - position), 6),
            "fade_out_s": None if previous is None else previous.get("fade_out_s"),
            "fade_in_s": None if incoming is None else incoming.get("fade_in_s"),
            "source_audio_left_s": remaining,
            "edge_source_exhausted": exhausted,
        })

    return {"schema": "reaper-workbench.timing-window/1",
            "window_s": [lo, hi],
            "item": {"position_s": position, "source_offset_s": offset,
                     "length_s": item_length, "source_duration_s": source_total},
            "attack_count": len(rows),
            "attacks": rows,
            "seam_count": len(seam_rows),
            "seams": seam_rows}
