"""Short-term level profile of one rendered source.

A measurement answers "how loud is this window".  A profile answers "where in the
track is this source, how loud is each phrase, and where is it silent" — the
view a mix decision actually needs, computed from one render instead of dozens
of separate windows.
"""

from __future__ import annotations

import numpy as np

from ..errors import ValidationError

FLOOR_PERCENTILE = 20.0
ACTIVE_MARGIN_DB = 6.0


def _split_run(points: list[dict], levels: np.ndarray, start: int, end: int,
               split_db: float, min_s: float, step_s: float) -> list[tuple[int, int]]:
    """Split one active run where its level moves by more than ``split_db``.

    Silence alone does not separate phrases: a quiet line inside a loud passage
    is still active, and averaging it into the run hides exactly the drop a
    listener hears. The run is walked left to right; when the running mean drifts
    past the threshold for at least ``min_s`` seconds, a new segment starts.
    """
    min_steps = max(1, int(round(min_s / step_s)))
    spans: list[tuple[int, int]] = []
    first = start
    total = 0.0
    count = 0
    recent: list[float] = []
    for index in range(start, end + 1):
        recent.append(float(levels[index]))
        if len(recent) > min_steps:
            recent.pop(0)
        total += float(levels[index])
        count += 1
        mean = total / count
        if (count >= 2 * min_steps and len(recent) == min_steps
                and abs(sum(recent) / len(recent) - mean) > split_db):
            spans.append((first, index - 1))
            first = index
            total = float(levels[index])
            count = 1
            recent = [float(levels[index])]
    spans.append((first, end))
    return spans


def build_profile(samples: np.ndarray, sample_rate: int, *,
                  step_s: float, offset_s: float = 0.0,
                  active_margin_db: float = ACTIVE_MARGIN_DB,
                  split_db: float | None = None,
                  min_segment_s: float = 2.0) -> dict:
    if samples.ndim == 1:
        mono = samples.astype(np.float64)
    else:
        mono = samples.mean(axis=1).astype(np.float64)
    if sample_rate <= 0:
        raise ValidationError("sample_rate must be positive")
    if step_s <= 0:
        raise ValidationError("step_s must be positive")
    step = max(1, int(round(sample_rate * float(step_s))))
    if len(mono) < step:
        raise ValidationError("the rendered source is shorter than one profile step",
                              details={"samples": int(len(mono)), "step": step})
    points = []
    for start in range(0, len(mono) - step + 1, step):
        block = mono[start:start + step]
        rms = float(np.sqrt(np.mean(block ** 2)) + 1e-12)
        points.append({"time_s": round(offset_s + start / sample_rate, 3),
                       "level_db": round(20 * np.log10(rms), 2)})
    levels = np.array([row["level_db"] for row in points])
    floor = float(np.percentile(levels, FLOOR_PERCENTILE))
    active = levels > floor + float(active_margin_db)
    segments = []
    index = 0
    while index < len(points):
        if not active[index]:
            index += 1
            continue
        start = index
        while index + 1 < len(points) and active[index + 1]:
            index += 1
        end = index
        spans = ([(start, end)] if split_db is None
                 else _split_run(points, levels, start, end, float(split_db),
                                 float(min_segment_s), float(step_s)))
        for first, last in spans:
            block_levels = levels[first:last + 1]
            power = float(np.mean(10 ** (block_levels / 10.0)))
            segments.append({"start_s": points[first]["time_s"],
                             "end_s": round(points[last]["time_s"] + step_s, 3),
                             "level_db": round(10 * np.log10(power), 2)})
        index += 1
    target = float(np.median([segment["level_db"] for segment in segments])) if segments else None
    return {"step_s": float(step_s), "floor_db": round(floor, 2),
            "active_margin_db": float(active_margin_db),
            "active_share": round(float(active.mean()), 4),
            "target_db": round(target, 2) if target is not None else None,
            "segments": segments, "points": points}
