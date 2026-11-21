"""Simple, deliberately unsophisticated timing baselines.

Every function receives the same measured event times and target times.  The
output is a list of segments in the same shape as the truth so the metrics can
compare them directly.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import numpy as np


def _segments(events_s: Sequence[float], shifts_s: Sequence[float],
              *, length_s: float) -> list[dict[str, float]]:
    return [{"source_start_s": float(event),
             "source_end_s": float(event) + length_s,
             "target_start_s": float(event) + float(shift),
             "target_end_s": float(event) + length_s + float(shift),
             "shift_s": float(shift)}
            for event, shift in zip(events_s, shifts_s)]


def no_op(events_s: Sequence[float], targets_s: Sequence[float], *,
          length_s: float = 0.08) -> list[dict[str, float]]:
    return _segments(events_s, [0.0] * len(events_s), length_s=length_s)


def global_shift(events_s: Sequence[float], targets_s: Sequence[float], *,
                 length_s: float = 0.08) -> list[dict[str, float]]:
    if not events_s:
        return []
    shift = float(np.median(np.asarray(targets_s) - np.asarray(events_s)))
    return _segments(events_s, [shift] * len(events_s), length_s=length_s)


def nearest_grid(events_s: Sequence[float], targets_s: Sequence[float], *,
                 length_s: float = 0.08) -> list[dict[str, float]]:
    shifts = [float(target) - float(event)
              for event, target in zip(events_s, targets_s)]
    return _segments(events_s, shifts, length_s=length_s)


def uniform_slices(duration_s: float, slices: int, *,
                   length_s: float = 0.08) -> list[dict[str, float]]:
    """Even slicing without a musical target; the control for edit density."""
    if slices < 1:
        raise ValueError("slices must be positive")
    step = duration_s / slices
    return [{"source_start_s": index * step,
             "source_end_s": (index + 1) * step,
             "target_start_s": index * step,
             "target_end_s": (index + 1) * step,
             "shift_s": 0.0} for index in range(slices)]


def time_stretch(duration_s: float, target_duration_s: float) -> dict[str, Any]:
    """Declared baseline, not silently substituted by editing.

    A real stretch implementation must be supplied by the caller; this function
    exists so a report can say "not run" instead of confusing it with editing.
    """
    return {"available": False,
            "reason": "time-stretch backend not installed",
            "requested_duration_s": float(duration_s),
            "target_duration_s": float(target_duration_s)}
