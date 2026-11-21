from __future__ import annotations

from bisect import bisect_left
from math import isclose

from .timing import grid_times
from .timing_seams import timing_errors


def _nearby(times, value):
    index = bisect_left(times, value)
    return times[max(0, index-1):index+1]


def review_plan(plan, analysis, tempo, rhythm=None):
    events = analysis["events"]
    blocks = plan["blocks"]
    anchors = {a["id"]: a for a in plan["anchors"]}
    phase = plan["grid_phase_s"]
    division = plan["subdivision"]
    tolerance = plan["tolerance_s"]
    start, end = plan["item_position_s"], plan["item_position_s"]+plan["item_length_s"]
    grid = ([q["time_s"]+phase for q in grid_times(
        tempo, start-phase-tolerance, end-phase+tolerance, division)] if division else [])
    rhythm_events = {}
    for segment in rhythm["segments"] if rhythm else ():
        for event in segment["observations"]:
            rhythm_events[event["event_id"]] = (segment["id"], segment["status"], [])
        for option in segment["options"]:
            for anchor in option["anchors"]:
                rhythm_events[anchor["event_id"]][2].append({
                    "option_id": option["id"], "target_s": anchor["target_s"],
                    "target_qn": anchor["target_qn"]})
    rows = []
    geometry_epsilon = 1 / analysis.get("sample_rate", 44100)
    changes = {"improved": 0, "unchanged": 0, "worsened": 0}
    for event in events:
        event_id = event["id"]
        observed, source = event["project_s"], event["source_s"]
        hits = [b["position_s"] + source-b["source_start_s"] for b in blocks
                if b["source_start_s"] <= source < b["source_start_s"]+b["length_s"]]
        anchor = anchors.get(event_id)
        nearby_grid = _nearby(grid, observed)
        row = {
            "event_id": event_id, "observed_s": observed, "source_s": source,
            "confidence": event["confidence"],
            "candidate_targets_s": nearby_grid,
            "planned_source_occurrences_s": sorted(hits),
        }
        segment, state, options = rhythm_events.get(event_id, (None, None, []))
        if segment:
            row.update({"rhythm_segment_id": segment, "rhythm_status": state,
                        "rhythm_options": options})
        if anchor:
            before = abs(anchor["desired_shift_s"])
            after = abs(anchor["residual_s"])
            difference = before-after
            state = ("unchanged" if abs(difference) <= geometry_epsilon else
                     "improved" if difference > 0 else "worsened")
            changes[state] += 1
            row.update({"assignment": "selected", "target_s": anchor["target_s"],
                        "planned_s": observed + anchor["achieved_shift_s"],
                        "error_before_s": observed-anchor["target_s"],
                        "error_after_s": anchor["residual_s"],
                        "change": state, "fixed_by": anchor.get("fixed_by", [])})
        else:
            closest = min(nearby_grid, key=lambda t: abs(t-observed), default=None)
            if closest is not None and not start <= closest < end:
                cause = "nearest_grid_outside_item"
            elif segment and plan.get("rhythm_choices", {}).get(segment) == "keep":
                cause = "rhythm_interval_kept"
            else:
                cause = "no_selected_target"
            row.update({"assignment": "unassigned", "reason": cause})
        rows.append(row)
    assigned = [r for r in rows if r["assignment"] == "selected"]
    remaining = [r for r in assigned if abs(r["error_after_s"]) > geometry_epsilon]
    geometry_changed = any(not isclose(b["position_s"]-b["source_start_s"],
                        start-plan["source_offset_s"], rel_tol=0., abs_tol=1e-7)
                           for b in blocks)
    plan.update(timing_errors(list(anchors.values())))
    plan["review_rows"] = rows
    plan["review"] = {
        "state": ("no_timing_change" if not geometry_changed else "needs_review"
                  if len(rows) != len(assigned) or remaining else "assigned_and_aligned"),
        "candidate_count": len(rows), "assigned_count": len(assigned),
        "unassigned_count": len(rows)-len(assigned), "residual_count": len(remaining),
        "candidate_motion": changes, "comparison_resolution_s": geometry_epsilon,
        "max_assigned_error_after_s": max((abs(r["error_after_s"]) for r in assigned),
                                         default=None),
    }
    for kind in ("residual", "unassigned"):
        plan["review"][kind] = review_page(plan, kind=kind, limit=4)
    return plan


def review_page(plan, *, kind="all", start_s=None, end_s=None, offset=0, limit=12):
    rows = [r for r in plan["review_rows"]
            if (start_s is None or r["observed_s"] >= start_s)
            and (end_s is None or r["observed_s"] < end_s)]
    if kind == "residual":
        rows = sorted((r for r in rows if r["assignment"] == "selected" and
                       abs(r["error_after_s"]) > plan["review"]["comparison_resolution_s"]),
                      key=lambda r: -abs(r["error_after_s"]))
    if kind == "unassigned":
        rows = sorted((r for r in rows if r["assignment"] == "unassigned"),
                      key=lambda r: -r["confidence"])
    return {"kind": kind, "total": len(rows), "offset": offset,
            "next_offset": offset+limit if offset+limit < len(rows) else None,
            "rows": rows[offset:offset+limit]}
