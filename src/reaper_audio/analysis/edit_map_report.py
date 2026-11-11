"""Build an evidence map from a live ``ProjectSession.analyze`` report.

The map deliberately contains measurements and candidates, not a musical
decision. The agent reads it, chooses which cuts (if any) make sense for the
song and the user's intent, and applies them through the transactional API.
"""
from __future__ import annotations

from typing import Mapping

from .edit_map import (Onset, _cluster, _confidence, _group, _nearest_beat,
                       tempo_grid)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def tempo_points_from_report(report: Mapping) -> list[dict]:
    """Collect the project tempo annotation that ``analyze`` attached to cells."""
    for source in report.get("sources", {}).values():
        profile = (source.get("curves", {})
                   .get("temporal_profile", {}))
        cells = profile.get("cells", [])
        times = profile.get("times", [])
        points: dict[float, float] = {}
        for time_s, cell in zip(times, cells):
            tempo = cell.get("project_tempo") or {}
            bpm = tempo.get("bpm")
            if isinstance(time_s, (int, float)) and isinstance(bpm, (int, float)):
                points[round(float(time_s), 6)] = float(bpm)
        if points:
            return [{"time_s": time_s, "bpm": bpm}
                    for time_s, bpm in sorted(points.items())]
    return []


def build_edit_map_from_report(
    report: Mapping,
    tempo_points: list[dict] | None = None,
    *,
    intent: str | None = None,
    decide: bool = False,
    source_groups: Mapping[str, str] | None = None,
    min_confidence: float | None = None,
    start_s: float | None = None,
    end_s: float | None = None,
) -> dict:
    """Turn a live analysis report into cut candidates with measured evidence."""
    if intent is not None and intent not in {"tight", "natural", "preserve"}:
        raise ValueError("intent must be tight, natural or preserve")
    if decide and intent is None:
        intent = "natural"
    sources = report.get("sources") or {}
    if not sources:
        raise ValueError("report has no analyzed sources")
    if tempo_points is None:
        tempo_points = tempo_points_from_report(report)

    events: list[Onset] = []
    names: list[str] = []
    durations_by_name: dict[str, list[float]] = {}
    capped_by_name: dict[str, bool] = {}
    for guid, source in sources.items():
        name = (source.get("track_context") or {}).get("name") or str(guid)
        names.append(name)
        raw = ((source.get("curves") or {}).get("events_2ms") or {}).get("events", [])
        for event in raw:
            if event.get("kind") != "onset":
                continue
            time_s = float(event.get("t", -1))
            if time_s < 0:
                continue
            if start_s is not None and time_s < start_s:
                continue
            if end_s is not None and time_s >= end_s:
                continue
            duration_ms = event.get("dur_ms")
            if isinstance(duration_ms, (int, float)):
                durations_by_name.setdefault(name, []).append(float(duration_ms))
            if event.get("dur_capped") is True:
                capped_by_name[name] = True
            events.append(Onset(
                time_s=time_s, source=name,
                level_db=float(event.get("level_db", -120.0)),
                band=str(event.get("band", "?")),
                centroid_hz=float(event.get("centroid_hz", 0.0)),
            ))
    duration = end_s if end_s is not None else (
        max((event.time_s for event in events), default=0.0) + 0.5)
    tail_by_source = {
        name: round(min(0.5, max(0.02, _percentile(values, 0.75) / 1000.0)), 3)
        for name, values in durations_by_name.items()
    }
    beats = tempo_grid(tempo_points, duration)
    clusters = _cluster(events)

    thresholds = {"tight": (0.50, 10.0, 1.0),
                  "natural": (0.65, 20.0, 0.7),
                  "preserve": (0.80, 35.0, 0.4)}
    if decide:
        confidence_floor, offset_floor_ms, strength = thresholds[intent]
    else:
        confidence_floor, offset_floor_ms, strength = 1.0, 0.0, 0.0
    if min_confidence is not None:
        confidence_floor = float(min_confidence)

    rows: list[dict] = []
    groups: dict[str, list[dict]] = {}
    for cluster in clusters:
        confidence, cluster_sources = _confidence(cluster, len(sources))
        grid_time, offset_ms = _nearest_beat(cluster["time_s"], beats)
        if decide:
            suggested = ("cut" if confidence >= confidence_floor and
                         abs(offset_ms) >= offset_floor_ms else "keep")
        else:
            suggested = "unreviewed"
        source_tails = [tail_by_source.get(source, 0.05)
                        for source in cluster_sources]
        tail_s = round(_percentile(source_tails, 0.5), 3)
        tail_capped = any(capped_by_name.get(source, False)
                          for source in cluster_sources)
        first_source = cluster_sources[0] if cluster_sources else ""
        group = ((source_groups or {}).get(first_source) or "unassigned")
        row = {
            "time_s": round(float(cluster["time_s"]), 4),
            "sources": cluster_sources,
            "group": group,
            "confidence": confidence,
            "grid_time_s": round(grid_time, 4),
            "offset_ms": round(offset_ms, 1),
            "tail_s": tail_s,
            "tail_capped": tail_capped,
            "suggested": suggested,
            "bands": sorted({item.band for item in cluster["items"]}),
            "reason": (f"{len(cluster_sources)} source(s) agree; "
                       f"{offset_ms:+.0f} ms from the beat; "
                       f"confidence {confidence:.2f}; "
                       f"tail {'≥' if tail_capped else '≈'}"
                       f"{tail_s * 1000:.0f} ms"),
        }
        rows.append(row)
        groups.setdefault(row["group"], []).append(
            {"time_s": row["time_s"], "suggested": suggested,
             "confidence": confidence, "offset_ms": row["offset_ms"]})
    return {
        "schema_version": 1,
        "intent": intent,
        "quantize_strength": strength,
        "duration_s": round(float(duration), 3),
        "tempo_beats": len(beats),
        "sections": [],
        "source_tail_s": dict(sorted(tail_by_source.items())),
        "source_tail_capped": dict(sorted(capped_by_name.items())),
        "onsets": {name: [round(item.time_s, 4) for item in events
                          if item.source == name] for name in names},
        "candidates": rows,
        "groups": groups,
        "suggested_cuts": [row for row in rows if row["suggested"] == "cut"],
        "decision": ("These are measured candidates. The agent chooses cuts, "
                     "groups and strength; no cut is applied by this function."),
    }
