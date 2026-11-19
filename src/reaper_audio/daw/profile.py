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


def build_profile(samples: np.ndarray, sample_rate: int, *,
                  step_s: float, offset_s: float = 0.0,
                  active_margin_db: float = ACTIVE_MARGIN_DB) -> dict:
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
        block_levels = levels[start:end + 1]
        power = float(np.mean(10 ** (block_levels / 10.0)))
        segments.append({"start_s": points[start]["time_s"],
                         "end_s": round(points[end]["time_s"] + step_s, 3),
                         "level_db": round(10 * np.log10(power), 2)})
        index += 1
    target = float(np.median([segment["level_db"] for segment in segments])) if segments else None
    return {"step_s": float(step_s), "floor_db": round(floor, 2),
            "active_margin_db": float(active_margin_db),
            "active_share": round(float(active.mean()), 4),
            "target_db": round(target, 2) if target is not None else None,
            "segments": segments, "points": points}
