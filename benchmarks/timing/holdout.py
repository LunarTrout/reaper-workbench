"""Measure real before/after pairs without inventing ground truth.

The source files are downloaded locally and are never committed.  For each
before-event we estimate the local displacement to the after file by
normalised cross-correlation of a short onset envelope.  That is a measured
shift, not a human annotation: the report says so and never presents these
numbers as exact edit truth.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from reaper_audio.analysis.timing import detect_timing_events
from reaper_audio.models import AudioBuffer


def _mono(path: Path, rate: int = 44_100) -> np.ndarray:
    audio, sample_rate = sf.read(str(path), always_2d=True)
    audio = audio.astype(np.float64).mean(axis=1)
    if sample_rate != rate:
        # Linear resampling is sufficient for an onset-envelope probe; the
        # report records the source rate separately.
        target = max(1, int(round(len(audio) * rate / sample_rate)))
        audio = np.interp(np.linspace(0, len(audio) - 1, target),
                          np.arange(len(audio)), audio)
    return audio


def _envelope(audio: np.ndarray, hop: int = 128) -> np.ndarray:
    diff = np.abs(np.diff(audio, prepend=audio[:1]))
    frames = len(diff) // hop
    if frames == 0:
        return np.zeros(1, dtype=np.float64)
    return diff[:frames * hop].reshape(frames, hop).max(axis=1)


def _local_shift(before: np.ndarray, after: np.ndarray, event_s: float,
                 *, rate: int, hop: int = 128,
                 half_window_s: float = 0.060,
                 context_s: float = 0.035) -> dict[str, Any] | None:
    frame_rate = rate / hop
    centre = int(round(event_s * frame_rate))
    context = max(2, int(round(context_s * frame_rate)))
    radius = max(1, int(round(half_window_s * frame_rate)))
    if centre - context - radius < 0 or centre + context + radius >= len(before):
        return None
    template = before[centre - context:centre + context]
    template = template - float(template.mean())
    norm = float(np.linalg.norm(template))
    if norm <= 1e-12:
        return None
    best = None
    for lag in range(-radius, radius + 1):
        candidate = after[centre + lag - context:centre + lag + context]
        candidate = candidate - float(candidate.mean())
        denom = float(np.linalg.norm(candidate))
        if denom <= 1e-12:
            continue
        score = float(np.dot(template, candidate) / (norm * denom))
        if best is None or score > best[0]:
            best = (score, lag)
    if best is None or best[0] < 0.35:
        return None
    if abs(best[1]) == radius:
        return None
    return {"shift_s": best[1] / frame_rate, "correlation": round(best[0], 4)}


def measure_pair(root: Path, row: dict[str, Any]) -> dict[str, Any]:
    before_path = root / row["before"]
    after_path = root / row["after"]
    result: dict[str, Any] = {
        "id": row["id"], "instrument": row.get("instrument"),
        "truth": row.get("truth"), "source": row.get("source"),
        "status": "ok", "files": {"before": row["before"], "after": row["after"]},
    }
    if not before_path.is_file() or not after_path.is_file():
        result.update(status="missing_audio",
                      next="download the named files from the source page")
        return result
    before = _mono(before_path)
    after = _mono(after_path)
    before_env = _envelope(before)
    after_env = _envelope(after)
    # detect on a small AudioBuffer so the real detector is exercised
    detected = detect_timing_events(
        AudioBuffer(before[:, None], 44_100), sensitivity=1.8, min_gap_s=0.03)
    events = [float(item["source_s"]) for item in detected["events"]]
    shifts: list[float] = []
    correlations: list[float] = []
    for event in events:
        found = _local_shift(before_env, after_env, event, rate=44_100)
        if found is None:
            continue
        shifts.append(found["shift_s"])
        correlations.append(found["correlation"])
    result.update(
        duration_before_s=round(len(before) / 44_100, 4),
        duration_after_s=round(len(after) / 44_100, 4),
        detected_events=len(events),
        matched_events=len(shifts),
        match_rate=round(len(shifts) / len(events), 4) if events else 0.0,
        shift_mean_s=round(float(np.mean(shifts)), 5) if shifts else None,
        shift_median_s=round(float(np.median(shifts)), 5) if shifts else None,
        shift_mae_s=round(float(np.mean(np.abs(shifts))), 5) if shifts else None,
        shift_p90_s=round(float(np.percentile(np.abs(shifts), 90)), 5) if shifts else None,
        shift_min_s=round(float(np.min(shifts)), 5) if shifts else None,
        shift_max_s=round(float(np.max(shifts)), 5) if shifts else None,
        mean_correlation=round(float(np.mean(correlations)), 4) if correlations else None,
        note=row.get("note"),
    )
    if row.get("truth") == "perceptual_ab":
        result["next"] = "perceptual A/B only; do not report an edit MAE"
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=os.environ.get("TIMING_HOLDOUT_DIR", "."))
    parser.add_argument("--pairs", default=str(Path(__file__).with_name("holdout_pairs.json")))
    parser.add_argument("--out", default=str(Path(__file__).with_name("holdout_report.json")))
    args = parser.parse_args()
    root = Path(args.root)
    pairs = json.loads(Path(args.pairs).read_text(encoding="utf-8"))
    rows = [measure_pair(root, row) for row in pairs]
    report = {
        "schema": "reaper-workbench.timing-holdout-report/1",
        "note": "Local measurements only. The audio is not redistributed; "
                "measured_local_shift is a cross-correlation estimate, not human truth.",
        "root_label": "TIMING_HOLDOUT_DIR",
        "pairs": rows,
        "available": sum(row["status"] == "ok" for row in rows),
        "planned": len(rows),
    }
    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8")
    print(json.dumps({"available": report["available"], "planned": report["planned"],
                      "ids": [(row["id"], row["status"]) for row in rows]}, indent=2))


if __name__ == "__main__":
    main()
