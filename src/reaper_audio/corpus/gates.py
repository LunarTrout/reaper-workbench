"""Defect gates: things that are wrong regardless of taste.

The corpus distance describes *where* a mix sits; it cannot see a clipped
sample, a DC offset or a drop-out.  These measurements are deliberately
separate from the tonal model and are compared against a delivery policy that
the caller supplies (project settings), never against constants baked here.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_EPS = 1e-12


@dataclass(frozen=True)
class GateMeasurement:
    values: dict[str, float]
    violations: tuple[str, ...]


def clipping(x: np.ndarray, *, full_scale: float = 1.0) -> dict[str, float]:
    """Count sample runs sitting at full scale (hard clipping)."""
    mag = np.max(np.abs(x), axis=1) if x.ndim == 2 else np.abs(x)
    hit = mag >= full_scale * (1.0 - 1e-6)
    runs = 0
    longest = 0
    current = 0
    for flag in hit:
        if flag:
            current += 1
            longest = max(longest, current)
        elif current:
            runs += 1
            current = 0
    if current:
        runs += 1
    # Flat tops: consecutive samples with exactly the same magnitude, close to
    # the signal maximum.  This catches clipping that never reaches 1.0 FS
    # (a limiter at -0.3 dBFS leaves thousands of such runs).
    flat_runs = 0
    flat_longest = 0
    if mag.size:
        peak = float(mag.max())
        if peak > 0:
            near = mag >= 0.8 * peak
            same = np.zeros(mag.shape[0], dtype=bool)
            same[1:] = (mag[1:] == mag[:-1]) & near[1:] & near[:-1]
            current = 0
            for flag in same:
                if flag:
                    current += 1
                    flat_longest = max(flat_longest, current + 1)
                elif current:
                    flat_runs += 1
                    current = 0
            if current:
                flat_runs += 1
    return {"clip_runs": float(runs), "clip_longest": float(longest),
            "clip_flat_runs": float(flat_runs), "clip_flat_longest": float(flat_longest),
            "clip_fraction": float(hit.mean()) if hit.size else 0.0}


def dc_offset(x: np.ndarray) -> dict[str, float]:
    data = x if x.ndim == 2 else x[:, None]
    means = np.abs(np.mean(data, axis=0))
    return {"dc_max": float(means.max()) if means.size else 0.0}


def dropouts(x: np.ndarray, sr: int, *, block_s: float = 0.05,
             floor_db: float = -70.0) -> dict[str, float]:
    """Runs of near-silence inside otherwise active material."""
    mono = x.mean(axis=1) if x.ndim == 2 else x
    hop = max(1, int(sr * block_s))
    usable = (mono.shape[0] // hop) * hop
    if usable == 0:
        return {"silence_runs": 0.0, "silence_longest_s": 0.0}
    frames = mono[:usable].reshape(-1, hop)
    rms = np.sqrt(np.mean(frames ** 2, axis=1) + _EPS)
    db = 20 * np.log10(rms)
    silent = db < floor_db
    runs = 0
    longest = 0
    current = 0
    for flag in silent:
        if flag:
            current += 1
            longest = max(longest, current)
        elif current:
            runs += 1
            current = 0
    if current:
        runs += 1
    return {"silence_runs": float(runs),
            "silence_longest_s": float(longest * block_s)}


def level_jumps(x: np.ndarray, sr: int, *, window_s: float = 1.0) -> dict[str, float]:
    """Largest step between consecutive short-window RMS values, in dB."""
    mono = x.mean(axis=1) if x.ndim == 2 else x
    hop = max(1, int(sr * window_s))
    usable = (mono.shape[0] // hop) * hop
    if usable < 2 * hop:
        return {"level_jump_db": 0.0, "level_range_db": 0.0}
    frames = mono[:usable].reshape(-1, hop)
    rms = np.sqrt(np.mean(frames ** 2, axis=1) + _EPS)
    db = 20 * np.log10(rms)
    return {"level_jump_db": float(np.max(np.abs(np.diff(db)))),
            "level_range_db": float(db.max() - db.min())}


def gate_values(x: np.ndarray, sr: int) -> dict[str, float]:
    values: dict[str, float] = {}
    values.update(clipping(x))
    values.update(dc_offset(x))
    values.update(dropouts(x, sr))
    values.update(level_jumps(x, sr))
    return values


#: Policy keys are the caller's; the values here are only the comparison rule.
def violations(values: dict[str, float], policy: dict[str, float]) -> tuple[str, ...]:
    out = []
    clip_total = values.get("clip_runs", 0.0) + values.get("clip_flat_runs", 0.0)
    if clip_total > policy.get("max_clip_runs", float("inf")):
        out.append("clipping")
    if values.get("dc_max", 0.0) > policy.get("max_dc", float("inf")):
        out.append("dc_offset")
    if values.get("silence_longest_s", 0.0) > policy.get("max_silence_s", float("inf")):
        out.append("dropout")
    if values.get("level_jump_db", 0.0) > policy.get("max_level_jump_db", float("inf")):
        out.append("level_jump")
    return tuple(out)


def check(x: np.ndarray, sr: int, policy: dict[str, float]) -> GateMeasurement:
    values = gate_values(x, sr)
    return GateMeasurement(values=values, violations=violations(values, policy))
