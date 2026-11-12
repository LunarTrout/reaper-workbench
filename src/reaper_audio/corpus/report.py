"""Assessment report: one audio file against a corpus, with defects.

The report is a vector plus a rank, not a score.  ``percentile`` answers "how
far is this from the corpus compared with the corpus members themselves"; it is
computed from the leave-one-out distances, so it has an empirical null.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .gates import check, gate_values
from .measure import FEATURE_NAMES, measure_signal
from .model import CorpusModel


@dataclass
class Assessment:
    distance: float
    percentile: float
    contributions: list[tuple[str, float, float]]
    gates: dict[str, float]
    violations: tuple[str, ...]
    extras: dict

    def to_dict(self) -> dict:
        return {
            "distance": self.distance,
            "percentile": self.percentile,
            "contributions": [{"feature": n, "z": z, "share": s}
                              for n, z, s in self.contributions],
            "gates": self.gates,
            "violations": list(self.violations),
            "extras": self.extras,
        }


def assess(model: CorpusModel, matrix: np.ndarray, x: np.ndarray, sr: int, *,
           policy: dict[str, float] | None = None) -> Assessment:
    measurement = measure_signal(x, sr)
    distance = float(model.distance(measurement.values)[0])
    loo = model.loo_distances(matrix)
    percentile = float(np.mean(loo <= distance))
    gate = check(x, sr, policy or {})
    return Assessment(
        distance=distance,
        percentile=percentile,
        contributions=model.contributions(measurement.values),
        gates=gate.values,
        violations=gate.violations,
        extras=measurement.extras,
    )


def assess_file(model: CorpusModel, matrix: np.ndarray, path: str | Path, *,
                max_seconds: float | None = None,
                policy: dict[str, float] | None = None) -> Assessment:
    info = sf.info(str(path))
    frames = info.frames if max_seconds is None else min(info.frames,
                                                         int(max_seconds * info.samplerate))
    x, sr = sf.read(str(path), frames=frames, dtype="float64", always_2d=True)
    return assess(model, matrix, x, sr, policy=policy)


def distance_timeline(model: CorpusModel, matrix: np.ndarray, x: np.ndarray, sr: int,
                      *, window_s: float = 10.0) -> list[dict]:
    """Distance per window: where along the track the mix leaves the corpus.

    Sections are not labelled here; the timeline only says *where* the
    deviation is, leaving the interpretation to the project map.
    """
    loo = model.loo_distances(matrix)
    step = int(window_s * sr)
    out = []
    for start in range(0, x.shape[0] - step + 1, step):
        values = measure_signal(x[start:start + step], sr).values
        distance = float(model.distance(values)[0])
        out.append({
            "start_s": start / sr,
            "end_s": (start + step) / sr,
            "distance": distance,
            "percentile": float(np.mean(loo <= distance)),
        })
    return out


def corpus_matrix_from_dir(directory: str | Path, *, window_s: float = 10.0,
                           max_seconds: float | None = None,
                           manifest=None, stage: str | None = None) -> np.ndarray:
    """Measure every excerpt in a directory.

    When a manifest and a stage are given, only excerpts whose manifest entry
    has that stage are used: a distance over masters and rough mixes at once is
    an average of two different targets, not a target.
    """
    allowed = None
    if manifest is not None and stage is not None:
        allowed = {Path(e.file).name for e in manifest.by_stage(stage)}
    rows = []
    for path in sorted(Path(directory).glob("*.wav")):
        if allowed is not None and path.name not in allowed:
            continue
        info = sf.info(str(path))
        frames = info.frames if max_seconds is None else min(info.frames,
                                                             int(max_seconds * info.samplerate))
        x, sr = sf.read(str(path), frames=frames, dtype="float64", always_2d=True)
        step = int(window_s * sr)
        for start in range(0, x.shape[0] - step + 1, step):
            rows.append(measure_signal(x[start:start + step], sr).values)
    if not rows:
        raise ValueError("no audio windows found")
    return np.stack(rows)
