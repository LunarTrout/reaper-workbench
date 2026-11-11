"""Local tempo evidence derived from repeated signal events."""

from __future__ import annotations

import numpy as np


def estimate_tempo_curve(
    onset_times_s: list[float] | np.ndarray,
    cell_times_s: list[float] | np.ndarray,
    *,
    window_s: float = 8.0,
    min_bpm: float = 20.0,
    max_bpm: float = 300.0,
    max_alternatives: int = 3,
) -> dict:
    """Estimate a local pulse rate around every supplied time point.

    The result reports metrical alternatives because an onset sequence alone
    cannot always distinguish a beat from a subdivision or a missing beat.
    """
    onsets = np.asarray(onset_times_s, dtype=np.float64)
    centers = np.asarray(cell_times_s, dtype=np.float64)
    if onsets.ndim != 1 or centers.ndim != 1:
        raise ValueError("onset_times_s and cell_times_s must be one-dimensional")
    if not np.isfinite(onsets).all() or not np.isfinite(centers).all():
        raise ValueError("tempo times must be finite")
    if (not np.isfinite(window_s) or window_s <= 0 or
            not np.isfinite(min_bpm) or not np.isfinite(max_bpm) or
            min_bpm <= 0 or max_bpm <= min_bpm or max_alternatives < 1):
        raise ValueError("tempo search settings are outside valid ranges")
    onsets = np.sort(onsets)
    if onsets.size and np.any(np.diff(onsets) <= 0):
        onsets = np.unique(onsets)

    points: list[dict] = []
    half_window = window_s / 2.0
    candidate_factors = (0.25, 1 / 3, 0.5, 1.0, 2.0, 3.0, 4.0)
    min_period = 60.0 / max_bpm
    max_period = 60.0 / min_bpm

    for center in centers:
        start = float(center - half_window)
        end = float(center + half_window)
        left = int(np.searchsorted(onsets, start, side="left"))
        right = int(np.searchsorted(onsets, end, side="right"))
        local = onsets[left:right]
        if local.size < 3:
            points.append({"time_s": float(center), "tempo_bpm": None,
                           "confidence": 0.0, "alternatives": [],
                           "onsets_used": int(local.size),
                           "window_s": {"start": start, "end": end},
                           "reason": "insufficient repeated events"})
            continue

        intervals = np.diff(local)
        intervals = intervals[np.isfinite(intervals) & (intervals > 0)]
        if intervals.size < 2:
            points.append({"time_s": float(center), "tempo_bpm": None,
                           "confidence": 0.0, "alternatives": [],
                           "onsets_used": int(local.size),
                           "window_s": {"start": start, "end": end},
                           "reason": "insufficient event intervals"})
            continue

        anchors = np.quantile(intervals, [0.25, 0.5, 0.75])
        periods = sorted({float(anchor * factor)
                          for anchor in anchors for factor in candidate_factors
                          if min_period <= anchor * factor <= max_period})
        if not periods:
            points.append({"time_s": float(center), "tempo_bpm": None,
                           "confidence": 0.0, "alternatives": [],
                           "onsets_used": int(local.size),
                           "window_s": {"start": start, "end": end},
                           "reason": "no candidate in configured BPM range"})
            continue

        scores: list[dict] = []
        for period in periods:
            phase = np.exp((2j * np.pi / period) * (local - local[0]))
            phase_coherence = float(np.abs(np.mean(phase)))
            residual_cycles = np.abs(np.diff(local) / period)
            subdivision_distance = np.abs(residual_cycles * 4.0 -
                                          np.rint(residual_cycles * 4.0)) / 4.0
            timing_consistency = float(np.exp(-4.0 * np.median(subdivision_distance)))
            evidence = min(1.0, max(0.0, (local.size - 2) / 8.0))
            score = (0.55 * phase_coherence + 0.30 * timing_consistency +
                     0.15 * evidence)
            scores.append({"bpm": 60.0 / period, "score": float(score),
                           "phase_coherence": phase_coherence,
                           "timing_consistency": timing_consistency})

        scores.sort(key=lambda item: (-item["score"], item["bpm"]))
        best = scores[0]
        # Keep near-equal metrical levels visible instead of pretending the
        # onset stream proves a unique beat/subdivision interpretation.
        alternatives = [item for item in scores[1:]
                        if best["score"] - item["score"] <= 0.04][:max_alternatives - 1]
        selected = [best, *alternatives]
        points.append({
            "time_s": float(center),
            "tempo_bpm": round(float(best["bpm"]), 2),
            "confidence": round(float(best["score"]), 3),
            "alternatives": [{"tempo_bpm": round(float(item["bpm"]), 2),
                              "confidence": round(float(item["score"]), 3)}
                             for item in selected[1:]],
            "candidates": [{"tempo_bpm": round(float(item["bpm"]), 2),
                            "score": round(float(item["score"]), 3),
                            "phase_coherence": round(float(item["phase_coherence"]), 3),
                            "timing_consistency": round(float(item["timing_consistency"]), 3)}
                           for item in selected],
            "onsets_used": int(local.size),
            "window_s": {"start": start, "end": end},
            "reason": "local onset periodicity; metrical level may be ambiguous",
        })

    return {
        "method": "local_onset_periodicity",
        "window_s": window_s,
        "search_range_bpm": {"min": min_bpm, "max": max_bpm},
        "resolution_bpm": "candidate set from local inter-onset intervals",
        "points": points,
        "interpretation": (
            "Tempo candidates are inferred from event periodicity. The onset stream "
            "may represent beats, subdivisions, or a pattern with missing beats."
        ),
    }
