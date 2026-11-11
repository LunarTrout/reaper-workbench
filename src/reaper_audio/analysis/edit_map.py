"""Analysis-driven edit map: where to cut, nudge or leave the performance alone.

This module does not cut audio. It turns measured evidence into a reviewable
map that an agent can act on with the transactional edit API:

* per-source onset events (already produced by ``analysis.events``),
* cross-source clusters with confidence (a cut is real when several mics agree),
* the project tempo grid and each cluster's distance from it,
* structural sections from a self-similarity matrix and Foote novelty,
* a small set of recommended cuts, plus an explicit review list.

The intent parameter is the decision layer: ``tight`` favours quantisation,
``natural`` keeps human timing unless it is clearly off, ``preserve`` only
fixes large errors. The agent chooses the intent and confirms the cuts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

import numpy as np
from scipy.signal import find_peaks

from ..models import AudioBuffer
from .events import detect_events

CLUSTER_TOLERANCE_S = 0.030
FRAME_S = 1.0
FRAME_HOP_S = 0.5


@dataclass(frozen=True)
class Onset:
    time_s: float
    source: str
    level_db: float
    band: str
    centroid_hz: float


def _mono(buffer: AudioBuffer) -> np.ndarray:
    data = np.asarray(buffer.samples, dtype=np.float64)
    if data.ndim == 2:
        return data.mean(axis=1)
    return data


def _source_onsets(source: str, buffer: AudioBuffer) -> list[Onset]:
    report = detect_events(np.asarray(buffer.samples, dtype=np.float64),
                           fs=buffer.sample_rate)
    result: list[Onset] = []
    for event in report.get("events", []):
        if event.get("kind") != "onset":
            continue
        result.append(Onset(
            time_s=float(event["t"]),
            source=source,
            level_db=float(event.get("level_db", -120.0)),
            band=str(event.get("band", "?")),
            centroid_hz=float(event.get("centroid_hz", 0.0)),
        ))
    return result


def _cluster(onsets: Iterable[Onset], tolerance_s: float = CLUSTER_TOLERANCE_S) -> list[dict]:
    ordered = sorted(onsets, key=lambda item: item.time_s)
    clusters: list[dict] = []
    for onset in ordered:
        if clusters and onset.time_s - clusters[-1]["end_s"] <= tolerance_s:
            cluster = clusters[-1]
            cluster["items"].append(onset)
            cluster["end_s"] = onset.time_s
            cluster["time_s"] = float(np.mean([item.time_s for item in cluster["items"]]))
        else:
            clusters.append({"time_s": onset.time_s, "end_s": onset.time_s,
                             "items": [onset]})
    return clusters


def _confidence(cluster: dict, source_count: int) -> tuple[float, list[str]]:
    items: list[Onset] = cluster["items"]
    sources = sorted({item.source for item in items})
    levels = np.array([item.level_db for item in items], dtype=float)
    strength = float(np.clip((np.max(levels) + 60.0) / 60.0, 0.0, 1.0))
    agreement = min(1.0, len(sources) / max(3, source_count / 2))
    low_band = sum(item.band == "low" for item in items) / len(items)
    score = 0.55 * agreement + 0.30 * strength + 0.15 * low_band
    return round(float(np.clip(score, 0.0, 1.0)), 3), sources


def tempo_grid(points: list[dict] | None, end_s: float) -> list[dict]:
    """Return beat positions from a piecewise-constant tempo map.

    ``points`` is the list returned by ``ProjectSession.tempo_at``:
    ``[{"time_s": ..., "bpm": ...}, ...]``. A missing map means 120 BPM.
    """
    if not points:
        points = [{"time_s": 0.0, "bpm": 120.0}]
    ordered = sorted(({"time_s": float(p["time_s"]), "bpm": float(p["bpm"])}
                      for p in points if float(p.get("bpm", 0)) > 0),
                     key=lambda item: item["time_s"])
    if not ordered:
        ordered = [{"time_s": 0.0, "bpm": 120.0}]
    if ordered[0]["time_s"] > 0:
        ordered.insert(0, {"time_s": 0.0, "bpm": ordered[0]["bpm"]})
    beats: list[float] = []
    cursor = 0.0
    index = 0
    while cursor <= end_s + 1e-9:
        while index + 1 < len(ordered) and ordered[index + 1]["time_s"] <= cursor:
            index += 1
        period = 60.0 / ordered[index]["bpm"]
        beats.append(cursor)
        cursor += period
    return [{"time_s": round(value, 6)} for value in beats]


def _nearest_beat(time_s: float, beats: list[dict]) -> tuple[float, float]:
    if not beats:
        return time_s, 0.0
    times = np.array([beat["time_s"] for beat in beats], dtype=float)
    index = int(np.argmin(np.abs(times - time_s)))
    return float(times[index]), float((time_s - times[index]) * 1000.0)


def _features(buffer: AudioBuffer) -> tuple[np.ndarray, np.ndarray]:
    """Frame-wise timbre/energy features for structural segmentation."""
    mono = _mono(buffer)
    fs = buffer.sample_rate
    hop = max(1, int(FRAME_HOP_S * fs))
    size = max(hop, int(FRAME_S * fs))
    frames = max(0, (len(mono) - size) // hop + 1)
    rows: list[list[float]] = []
    times: list[float] = []
    for index in range(frames):
        segment = mono[index * hop:index * hop + size]
        if len(segment) < 256:
            continue
        window = np.hanning(len(segment))
        spectrum = np.abs(np.fft.rfft(segment * window)) ** 2
        freqs = np.fft.rfftfreq(len(segment), 1.0 / fs)
        total = float(spectrum.sum()) + 1e-20
        centroid = float((freqs * spectrum).sum() / total)
        bands = []
        for low, high in ((20, 120), (120, 400), (400, 1200), (1200, 3000),
                          (3000, 6000), (6000, 12000), (12000, 20000)):
            bands.append(float(np.log10(spectrum[(freqs >= low) & (freqs < high)].sum()
                                       / total + 1e-12)))
        chroma = np.zeros(12)
        mask = (freqs >= 40) & (freqs <= 5000)
        midi = 69 + 12 * np.log2(np.maximum(freqs[mask], 1e-9) / 440.0)
        for value, power in zip(midi, spectrum[mask]):
            chroma[int(round(value)) % 12] += power
        chroma = chroma / (chroma.sum() + 1e-20)
        rows.append([float(np.sqrt(np.mean(segment ** 2))), centroid,
                     *bands, *chroma.tolist()])
        times.append(index * hop / fs)
    return np.array(rows, dtype=float), np.array(times, dtype=float)


def _sections(sources: Mapping[str, AudioBuffer]) -> list[dict]:
    """Foote novelty over a self-similarity matrix of the summed features."""
    if not sources:
        return []
    length = min(len(_features(buffer)[1]) for buffer in sources.values())
    if length < 8:
        return []
    matrix = []
    for buffer in sources.values():
        features, times = _features(buffer)
        matrix.append(features[:length])
    features = np.mean(matrix, axis=0)
    features = (features - features.mean(axis=0)) / (features.std(axis=0) + 1e-9)
    similarity = features @ features.T
    kernel_size = min(8, max(2, length // 8))
    novelty = np.zeros(length)
    for anchor in range(kernel_size, length - kernel_size):
        before = similarity[anchor - kernel_size:anchor, anchor - kernel_size:anchor]
        after = similarity[anchor:anchor + kernel_size, anchor:anchor + kernel_size]
        cross = similarity[anchor - kernel_size:anchor, anchor:anchor + kernel_size]
        novelty[anchor] = float(before.mean() + after.mean() - 2 * cross.mean())
    peaks, _ = find_peaks(novelty, distance=max(2, int(4.0 / FRAME_HOP_S)),
                          prominence=max(1e-9, float(np.std(novelty)) * 0.5))
    boundaries = [0.0] + [float(times[index]) for index in peaks] + [float(times[-1])]
    sections = []
    for index, (start, end) in enumerate(zip(boundaries[:-1], boundaries[1:])):
        if end - start < 0.5:
            continue
        sections.append({"index": index, "start_s": round(start, 3),
                         "end_s": round(end, 3),
                         "novelty": round(float(novelty[min(len(novelty) - 1,
                                                              int(start / FRAME_HOP_S))]), 4)})
    return sections


def _group(source: str) -> str:
    name = source.lower()
    if any(word in name for word in ("kick", "snare", "overhead", "tom", "hat",
                                     "ride", "drum", "cymbal")):
        return "drums"
    if any(word in name for word in ("guitar", "gtr", "amp")):
        return "guitars"
    if "bass" in name:
        return "bass"
    if any(word in name for word in ("vocal", "vox", "voice")):
        return "vocals"
    return "other"


def build_edit_map(
    sources: Mapping[str, AudioBuffer],
    tempo_points: list[dict] | None = None,
    *,
    intent: str = "natural",
    min_confidence: float | None = None,
) -> dict:
    """Build a reviewable cut map from per-source audio and the tempo map.

    intent: ``tight`` (quantise almost everything), ``natural`` (default,
    only clear timing errors), ``preserve`` (fix only large errors).
    """
    if intent not in {"tight", "natural", "preserve"}:
        raise ValueError("intent must be tight, natural or preserve")
    if not sources:
        raise ValueError("sources must contain at least one AudioBuffer")
    end_s = max(len(_mono(buffer)) / buffer.sample_rate
                for buffer in sources.values())
    beats = tempo_grid(tempo_points, end_s)

    events: list[Onset] = []
    for source, buffer in sources.items():
        events.extend(_source_onsets(source, buffer))
    clusters = _cluster(events)

    thresholds = {"tight": (0.50, 10.0, 1.0),
                  "natural": (0.65, 20.0, 0.7),
                  "preserve": (0.80, 35.0, 0.4)}
    confidence_floor, offset_floor_ms, strength = thresholds[intent]
    if min_confidence is not None:
        confidence_floor = float(min_confidence)

    recommended: list[dict] = []
    review: list[dict] = []
    rows: list[dict] = []
    groups: dict[str, list[dict]] = {}
    for cluster in clusters:
        confidence, sources_in_cluster = _confidence(cluster, len(sources))
        grid_time, offset_ms = _nearest_beat(cluster["time_s"], beats)
        action = "keep"
        if confidence >= confidence_floor and abs(offset_ms) >= offset_floor_ms:
            action = "cut"
        elif confidence < confidence_floor and abs(offset_ms) >= offset_floor_ms:
            action = "review"
        row = {
            "time_s": round(float(cluster["time_s"]), 4),
            "sources": sources_in_cluster,
            "group": _group(sources_in_cluster[0]) if sources_in_cluster else "other",
            "confidence": confidence,
            "grid_time_s": round(grid_time, 4),
            "offset_ms": round(offset_ms, 1),
            "action": action,
            "bands": sorted({item.band for item in cluster["items"]}),
            "reason": (f"{len(sources_in_cluster)} source(s) agree; "
                       f"{offset_ms:+.0f} ms from the beat; "
                       f"confidence {confidence:.2f}"),
        }
        rows.append(row)
        groups.setdefault(row["group"], []).append(
            {"time_s": row["time_s"], "action": action,
             "confidence": confidence, "offset_ms": row["offset_ms"]})
        if action == "cut":
            recommended.append(row)
        elif action == "review":
            review.append(row)

    return {
        "schema_version": 1,
        "intent": intent,
        "quantize_strength": strength,
        "duration_s": round(float(end_s), 3),
        "tempo_beats": len(beats),
        "sections": _sections(sources),
        "onsets": {source: [round(item.time_s, 4) for item in events
                            if item.source == source] for source in sources},
        "clusters": rows,
        "groups": groups,
        "recommended_cuts": recommended,
        "review": review,
        "note": ("Cuts are candidates, not decisions. Apply them through the "
                 "transactional edit API and verify phase before/after."),
    }
