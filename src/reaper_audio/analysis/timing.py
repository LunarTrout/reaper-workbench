"""Grid timing analysis and a conservative block planner for audio takes.

The module measures attacks in one audio take, matches them monotonically to a
musical grid, groups events that want the same shift, and returns clip
geometry at 1x.  It does not touch REAPER: the caller applies the geometry as
one typed edit transaction after previewing it.

Coordinates used here:

* ``source_s`` — seconds in the take's audio file;
* ``project_s`` — seconds on the project timeline;
* ``qn`` — quarter notes on the project tempo map.

The take offset is a parameter, never inferred from a name or a grid.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from scipy.ndimage import uniform_filter1d

from ..models import AudioBuffer
from .transients import detect_transients

SCHEMA = "reaper-workbench.timing-analysis/1"
PLAN_SCHEMA = "reaper-workbench.timing-plan/1"
EVENT_KINDS = frozenset({"attack", "note", "syllable", "tail", "unclear"})


@dataclass(frozen=True, slots=True)
class TimingEvent:
    id: str
    source_s: float
    sample: int
    kind: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "source_s": round(self.source_s, 7),
                "sample": self.sample, "kind": self.kind,
                "confidence": round(self.confidence, 4)}


def _mono(audio: AudioBuffer) -> np.ndarray:
    samples = np.asarray(audio.samples)
    if samples.ndim == 2 and samples.shape[1] > 1:
        return samples.mean(axis=1)
    return samples[:, 0] if samples.ndim == 2 else samples


def _refine_attack(mono: np.ndarray, peak_s: float, rate: int, *,
                   before_s: float = 0.04, after_s: float = 0.008) -> tuple[float, int]:
    """Move a spectral-flux peak back to the first rise of its attack."""
    peak = min(len(mono) - 1, max(0, int(round(peak_s * rate))))
    lo = max(0, peak - int(before_s * rate))
    hi = min(len(mono), peak + int(after_s * rate))
    if hi - lo < 8:
        return peak / rate, peak
    segment = np.abs(mono[lo:hi]).astype(np.float64)
    width = max(1, int(0.001 * rate))
    envelope = uniform_filter1d(segment, width, mode="nearest")
    head = envelope[:max(1, (hi - lo) // 4)]
    floor = float(np.percentile(head, 20)) if head.size else 0.0
    top = float(np.max(envelope))
    if top <= floor * 1.5 + 1e-12:
        return peak / rate, peak
    threshold = floor + 0.25 * (top - floor)
    index = int(np.argmax(envelope >= threshold))
    return (lo + index) / rate, lo + index


def detect_timing_events(audio: AudioBuffer, *, sensitivity: float = 2.8,
                         min_gap_s: float = 0.035, max_events: int = 2000,
                         profile: str = "auto") -> dict[str, Any]:
    """Detect attack candidates and refine each to its sample-domain onset.

    ``profile`` is a declared intent (auto/picked/vocal/sustained/percussion).
    Detection itself stays signal-driven; the profile only names the kind for
    the caller and does not inject a grid or a tempo.
    """
    if profile not in {"auto", "picked", "vocal", "sustained", "percussion"}:
        raise ValueError("unknown timing profile")
    raw = detect_transients(audio, sensitivity=sensitivity, min_gap_s=min_gap_s,
                            max_events=max_events)
    mono = _mono(audio)
    rate = audio.sample_rate
    events: list[TimingEvent] = []
    for index, row in enumerate(raw.get("events") or [], start=1):
        peak_s = float(row.get("time_s", -1))
        if peak_s < 0:
            continue
        refined_s, sample = _refine_attack(mono, peak_s, rate)
        kind = "attack" if profile in {"auto", "picked", "percussion"} else "note"
        events.append(TimingEvent(
            id=f"evt_{index:05d}", source_s=refined_s, sample=sample,
            kind=kind, confidence=float(row.get("confidence") or 0.0),
        ))
    events.sort(key=lambda event: event.source_s)
    for index, event in enumerate(events, start=1):
        if event.id != f"evt_{index:05d}":
            events[index - 1] = TimingEvent(f"evt_{index:05d}", event.source_s,
                                            event.sample, event.kind,
                                            event.confidence)
    return {
        "schema": SCHEMA,
        "profile": profile,
        "sample_rate": rate,
        "duration_s": round(len(mono) / rate, 6),
        "events": [event.to_dict() for event in events],
        "event_count": len(events),
        "coverage": "available" if events else "no_events",
    }


def grid_times(tempo: Any, start_s: float, end_s: float,
               subdivisions: int | Sequence[int] = 4) -> list[dict[str, float]]:
    """Return grid points as ``{qn, time_s, subdivision}`` in the window.

    ``tempo`` is the project :class:`TempoMap` (``qn_to_time``/``time_to_qn``).
    ``subdivisions`` is a division count or an explicit tuple, e.g. ``(3, 4)``.
    """
    if end_s <= start_s:
        return []
    divisions = (subdivisions,) if isinstance(subdivisions, int) else tuple(subdivisions)
    if not divisions or any(int(value) < 1 for value in divisions):
        raise ValueError("subdivisions must be positive integers")
    qn_start = float(tempo.time_to_qn(start_s))
    qn_end = float(tempo.time_to_qn(end_s))
    rows: list[dict[str, float]] = []
    for division in divisions:
        step = 1.0 / int(division)
        first = np.ceil(qn_start / step) * step
        qn = float(first)
        while qn <= qn_end + 1e-9:
            time_s = float(tempo.qn_to_time(qn))
            if start_s - 1e-9 <= time_s <= end_s + 1e-9:
                rows.append({"qn": qn, "time_s": time_s,
                             "subdivision": int(division)})
            qn += step
    rows.sort(key=lambda row: (row["time_s"], row["qn"]))
    return rows


def match_events(events: Sequence[Mapping[str, Any]],
                 grid: Sequence[Mapping[str, Any]], *,
                 tolerance_s: float = 0.080,
                 max_shift_s: float | None = None) -> dict[str, Any]:
    """Monotonic event-to-grid matching with skips on both sides.

    A skipped event stays in the source but receives no grid target; a skipped
    grid cell is a rest or an ornament.  The DP maximizes matched events and
    then minimizes absolute offset, so it never moves an event onto a denser
    grid merely to match more lines.
    """
    if tolerance_s <= 0:
        raise ValueError("tolerance_s must be positive")
    limit = tolerance_s if max_shift_s is None else min(tolerance_s, max_shift_s)
    n, m = len(events), len(grid)
    if not n or not m:
        return {"assignments": [], "matched": 0, "skipped_events": list(range(n)),
                "skipped_grid": list(range(m)), "median_abs_offset_s": None}
    neg = -1e18
    dp = [[neg] * (m + 1) for _ in range(n + 1)]
    for j in range(m + 1):
        dp[0][j] = 0.0
    for i in range(1, n + 1):
        dp[i][0] = 0.0
    choice: list[list[str]] = [[""] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        event_s = float(events[i - 1]["source_s"])
        for j in range(1, m + 1):
            best = dp[i - 1][j]
            step = "skip_event"
            if dp[i][j - 1] > best + 1e-12:
                best, step = dp[i][j - 1], "skip_grid"
            offset = abs(float(grid[j - 1]["time_s"]) - event_s)
            if offset <= limit:
                score = dp[i - 1][j - 1] + 1.0 - 0.05 * (offset / limit)
                if score > best + 1e-12:
                    best, step = score, "match"
            dp[i][j] = best
            choice[i][j] = step
    assignments: list[dict[str, Any]] = []
    i, j = n, m
    while i > 0 and j > 0:
        step = choice[i][j]
        if step == "match":
            event_s = float(events[i - 1]["source_s"])
            grid_row = grid[j - 1]
            assignments.append({
                "event_index": i - 1,
                "grid_index": j - 1,
                "event_s": event_s,
                "target_s": float(grid_row["time_s"]),
                "qn": float(grid_row["qn"]),
                "offset_s": float(grid_row["time_s"]) - event_s,
            })
            i -= 1
            j -= 1
        elif step == "skip_event":
            i -= 1
        else:
            j -= 1
    assignments.reverse()
    matched = {row["event_index"] for row in assignments}
    matched_grid = {row["grid_index"] for row in assignments}
    offsets = [abs(row["offset_s"]) for row in assignments]
    return {
        "assignments": assignments,
        "matched": len(assignments),
        "skipped_events": [index for index in range(n) if index not in matched],
        "skipped_grid": [index for index in range(m) if index not in matched_grid],
        "median_abs_offset_s": float(np.median(offsets)) if offsets else None,
    }


def _overlaps(start_s: float, end_s: float,
              ranges: Iterable[Mapping[str, Any]]) -> bool:
    for row in ranges:
        lo = float(row.get("start_s", row.get("start", 0.0)))
        hi = float(row.get("end_s", row.get("end", 0.0)))
        if start_s < hi and end_s > lo:
            return True
    return False


def build_timing_plan(events: Sequence[Mapping[str, Any]],
                      assignments: Sequence[Mapping[str, Any]], *,
                      analysis_id: str = "analysis",
                      strength: float = 1.0,
                      duration_s: float,
                      group_gap_s: float = 0.25,
                      group_shift_s: float = 0.012,
                      protected_ranges: Iterable[Mapping[str, Any]] = (),
                      fade_s: float = 0.0045) -> dict[str, Any]:
    """Group matched events into move blocks and return 1x clip geometry.

    A block is a contiguous run of events whose required shifts differ by less
    than ``group_shift_s`` and whose gap is below ``group_gap_s``.  The block
    gets one median shift; its boundaries sit halfway between neighbouring
    block events, so no attack is cut.  A block that overlaps a protected
    range is reported, not silently moved.
    """
    if not 0.0 <= strength <= 1.0:
        raise ValueError("strength must be between 0 and 1")
    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    ordered = sorted(events, key=lambda row: float(row["source_s"]))
    by_index = {int(row["event_index"]): row for row in assignments}
    matched = [(index, by_index[index]) for index in range(len(ordered))
               if index in by_index]
    if not matched:
        return {"schema": PLAN_SCHEMA, "analysis_id": analysis_id,
                "blocks": [], "issues": [{"code": "no_matched_events",
                                          "next_action": "widen tolerance or pass a longer window"}],
                "coverage": "empty"}
    groups: list[list[tuple[int, Mapping[str, Any]]]] = []
    for item in matched:
        index, assignment = item
        event_s = float(ordered[index]["source_s"])
        shift = float(assignment["offset_s"]) * strength
        if not groups:
            groups.append([item])
            continue
        previous_index, previous_assignment = groups[-1][-1]
        previous_s = float(ordered[previous_index]["source_s"])
        previous_shift = float(previous_assignment["offset_s"]) * strength
        if (event_s - previous_s <= group_gap_s and
                abs(shift - previous_shift) <= group_shift_s):
            groups[-1].append(item)
        else:
            groups.append([item])
    blocks: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for group in groups:
        first_index, first_assignment = group[0]
        last_index, last_assignment = group[-1]
        first_s = float(ordered[first_index]["source_s"])
        last_s = float(ordered[last_index]["source_s"])
        shifts = [float(assignment["offset_s"]) * strength
                  for _, assignment in group]
        shift = float(np.median(shifts))
        start_s = 0.0 if first_index == 0 else (
            float(ordered[first_index - 1]["source_s"]) + first_s) / 2.0
        end_s = duration_s if last_index == len(ordered) - 1 else (
            last_s + float(ordered[last_index + 1]["source_s"])) / 2.0
        end_s = min(duration_s, max(end_s, last_s + 1e-4))
        target_start = start_s + shift
        target_end = end_s + shift
        block = {
            "start_s": round(start_s, 7), "end_s": round(end_s, 7),
            "source_offset_s": round(start_s, 7),
            "target_start_s": round(target_start, 7),
            "target_end_s": round(target_end, 7),
            "shift_s": round(shift, 7),
            "rate": 1.0, "fade_in_s": fade_s, "fade_out_s": fade_s,
            "event_ids": [str(ordered[index]["id"]) for index, _ in group],
            "event_count": len(group),
        }
        blocks.append(block)
        if target_start < 0 or target_end > duration_s:
            issues.append({"code": "source_handles_unavailable",
                           "block": block,
                           "next_action": "reduce the shift or move the item boundary"})
        if _overlaps(start_s, end_s, protected_ranges):
            issues.append({"code": "protected_range", "block": block,
                           "next_action": "keep this block or lower strength"})
    for left, right in zip(blocks, blocks[1:]):
        delta = float(right["shift_s"]) - float(left["shift_s"])
        if abs(delta) >= 0.030:
            issues.append({"code": "adjacent_shift_jump",
                           "left": left["event_ids"][-1],
                           "right": right["event_ids"][0],
                           "delta_s": round(delta, 7),
                           "next_action": "audition this seam or protect the pause"})
    matched_offsets = [abs(float(row["offset_s"])) for _, row in matched]
    residual = []
    for index, assignment in matched:
        shift = float(assignment["offset_s"]) * strength
        residual.append(abs(float(ordered[index]["source_s"]) + shift -
                            float(assignment["target_s"])))
    return {
        "schema": PLAN_SCHEMA,
        "analysis_id": analysis_id,
        "strength": strength,
        "event_count": len(ordered),
        "matched_count": len(matched),
        "coverage": "partial" if issues else "complete",
        "median_error_before_s": round(float(np.median(matched_offsets)), 7),
        "median_error_after_s": round(float(np.median(residual)), 7),
        "blocks": blocks,
        "issues": issues,
    }
