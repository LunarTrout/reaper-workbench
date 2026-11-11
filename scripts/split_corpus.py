"""Split albums into tracks and cut one excerpt per track for the corpus.

Silence detection is adaptive: the floor comes from the recording's own quiet
frames, the gap length and excerpt length are task settings, not method
constants.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

FLOOR_PERCENTILE = 5.0
GAP_DB = 6.0
MIN_GAP_S = 1.0
MIN_TRACK_S = 60.0
EXCERPT_S = 45.0


def frame_db(x: np.ndarray, sr: int, hop_s: float = 0.1) -> np.ndarray:
    hop = int(sr * hop_s)
    usable = (x.shape[0] // hop) * hop
    frames = x[:usable].reshape(-1, hop)
    rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-20)
    return 20 * np.log10(rms)


def split_points(db: np.ndarray, sr: int, hop_s: float = 0.1) -> list[int]:
    floor = float(np.percentile(db, FLOOR_PERCENTILE))
    threshold = floor + GAP_DB
    silent = db < threshold
    runs, start = [], None
    for i, s in enumerate(silent):
        if s and start is None:
            start = i
        elif not s and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(silent)))
    min_frames = int(MIN_GAP_S / hop_s)
    points = []
    for a, b in runs:
        if b - a >= min_frames:
            points.append(((a + b) // 2) * int(sr * hop_s))
    return points


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="C:/Users/epicc/Desktop/PROJECTS/vers1/_refs/corpus")
    ap.add_argument("--out", default="work/corpus_tracks")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*.wav"):
        stale.unlink()

    total = 0
    for path in sorted(Path(args.corpus).glob("*.mp3")):
        group = path.stem.split("_")[0][:14]
        x, sr = sf.read(str(path), dtype="float64", always_2d=True)
        mono = x.mean(axis=1)
        db = frame_db(mono, sr)
        cuts = [0] + split_points(db, sr) + [x.shape[0]]
        index = 0
        for a, b in zip(cuts, cuts[1:]):
            if (b - a) / sr < MIN_TRACK_S:
                continue
            index += 1
            mid = (a + b) // 2
            half = int(EXCERPT_S * sr / 2)
            start = max(a, mid - half)
            stop = min(b, start + int(EXCERPT_S * sr))
            excerpt = x[start:stop]
            if excerpt.shape[0] < int(EXCERPT_S * sr * 0.8):
                continue
            name = f"{group}_{index:02d}.wav"
            sf.write(str(out / name), excerpt.astype(np.float32), sr, subtype="FLOAT")
            total += 1
        print(f"{path.name}: {index} segments")
    print("written", total, "excerpts to", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
