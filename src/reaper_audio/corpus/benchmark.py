"""Leave-one-track-out benchmark: does the model react to production changes?

Protocol
--------
For each excerpt (a track):

1. Fit a :class:`CorpusModel` on windows of *all other* tracks.
2. Take windows of this track, keep the original, and build degraded copies
   with :data:`reaper_audio.corpus.degrade.DEGRADATIONS`.
3. Compare distances to the model: a production degradation must move the copy
   *away* from the corpus; a pure level change must not.

The corpus never contains the track being scored, so identity cannot explain
the result.  No threshold is applied: the benchmark reports the fraction of
windows where the original is closer, and the median distance ratio.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import numpy as np
import soundfile as sf

from .degrade import DEGRADATIONS, degrade
from .measure import FEATURE_NAMES, measure_signal, windows
from .model import CorpusModel


def _load(path: str | Path, max_seconds: float) -> tuple[np.ndarray, int]:
    info = sf.info(str(path))
    frames = min(info.frames, int(max_seconds * info.samplerate))
    x, sr = sf.read(str(path), frames=frames, dtype="float64", always_2d=True)
    return x, int(sr)


def run_benchmark(
    paths: list[str | Path],
    *,
    window_s: float = 10.0,
    max_seconds: float = 60.0,
    include_invariant: bool = True,
    only: list[str] | None = None,
    limit_files: int | None = None,
) -> dict:
    if len(paths) < 3:
        raise ValueError("the leave-one-track-out protocol needs at least three excerpts")

    if limit_files is not None:
        paths = paths[:limit_files]
    loaded = []
    for p in paths:
        x, sr = _load(p, max_seconds)
        ws = list(windows(x, sr, window_s))
        feats = np.stack([measure_signal(w, sr).values for w in ws]) if ws else np.empty((0, len(FEATURE_NAMES)))
        group = Path(p).stem.split("_")[0]
        loaded.append({"path": str(p), "group": group, "sr": sr, "windows": ws,
                       "features": feats})

    results: dict[str, dict] = {}
    for deg in DEGRADATIONS:
        if only is not None and deg.name not in only:
            continue
        if deg.expect == "invariant" and not include_invariant:
            continue
        wins, margins, ratios, orig_d, deg_d = 0, [], [], [], []
        for i, item in enumerate(loaded):
            others = [o["features"] for o in loaded
                      if o["group"] != item["group"] and len(o["features"])]
            if not others:
                continue
            model = CorpusModel.fit(np.vstack(others), FEATURE_NAMES)
            for w in item["windows"]:
                y, _label = deg.fn(w, item["sr"], **deg.kwargs)
                d0 = model.distance(measure_signal(w, item["sr"]).values)[0]
                d1 = model.distance(measure_signal(y, item["sr"]).values)[0]
                orig_d.append(float(d0))
                deg_d.append(float(d1))
                margins.append((d1 - d0) / max(d0, 1e-9))
                ratios.append(d1 / max(d0, 1e-9))
                wins += 1
        if not wins:
            continue
        margins_a = np.asarray(margins)
        results[deg.name] = {
            "expect": deg.expect,
            "windows": wins,
            "closer_original": float(np.mean(margins_a > 0)),
            "median_margin": float(np.median(margins_a)),
            "median_ratio": float(np.median(ratios)),
            "median_distance_original": float(np.median(orig_d)),
            "median_distance_degraded": float(np.median(deg_d)),
        }

    # Level invariance is reported as an absolute margin: it must be small
    # compared with the production degradations, and its sign is meaningless.
    per_track = []
    for i, item in enumerate(loaded):
        others = [o["features"] for o in loaded
                  if o["group"] != item["group"] and len(o["features"])]
        if not others:
            continue
        model = CorpusModel.fit(np.vstack(others), FEATURE_NAMES)
        d = model.distance(item["features"])
        per_track.append({
            "path": item["path"],
            "windows": int(len(item["features"])),
            "distance_median": float(np.median(d)) if len(d) else float("nan"),
            "distance_iqr": float(np.subtract(*np.percentile(d, [75, 25]))) if len(d) else float("nan"),
            "model": model.summary(),
        })

    return {
        "protocol": {
            "window_s": window_s,
            "max_seconds": max_seconds,
            "excerpts": len(loaded),
            "groups": len({item["group"] for item in loaded}),
            "features": list(FEATURE_NAMES),
        },
        "degradations": results,
        "per_track": per_track,
    }
