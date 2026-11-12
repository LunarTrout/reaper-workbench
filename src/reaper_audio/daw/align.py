"""Transient-anchored alignment between two takes.

The offset is estimated around detected attacks, not on the whole file: a
global cross-correlation is dominated by sustained material and by level, while
an attack is exactly where a human sees and hears the misalignment.

Convention: a positive ``lag_samples`` means the target is *late* relative to
the reference; shifting the target by ``-lag_samples`` aligns it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import correlate, find_peaks

from ..analysis.transients import detect_transients
from ..models import AudioBuffer


@dataclass(frozen=True)
class Offset:
    time_s: float
    lag_samples: int
    lag_ms: float
    correlation: float


@dataclass(frozen=True)
class Alignment:
    offsets: tuple[Offset, ...]
    median_lag: int
    mad_samples: float
    median_correlation: float
    slope_samples_per_s: float = 0.0
    slope_stderr: float = 0.0
    span_s: float = 0.0

    @property
    def lag_ms(self) -> float:
        return 0.0 if not self.offsets else float(np.median([o.lag_ms for o in self.offsets]))

    def to_dict(self) -> dict:
        return {
            "median_lag_samples": self.median_lag,
            "median_lag_ms": self.lag_ms,
            "mad_samples": self.mad_samples,
            "median_correlation": self.median_correlation,
            "slope_samples_per_s": self.slope_samples_per_s,
            "slope_stderr": self.slope_stderr,
            "span_s": self.span_s,
            "offsets": [{"time_s": o.time_s, "lag_samples": o.lag_samples,
                         "lag_ms": o.lag_ms, "correlation": o.correlation}
                        for o in self.offsets],
        }


def _slope(times: np.ndarray, lags: np.ndarray) -> tuple[float, float]:
    """Least-squares drift in samples per second, with its standard error.

    A constant offset has slope 0; a take that slowly runs late or early has a
    significant slope, and the median alone would hide exactly that.
    """
    n = times.size
    if n < 3 or np.ptp(times) <= 0:
        return 0.0, 0.0
    design = np.stack([times, np.ones_like(times)], axis=1)
    coef, *_ = np.linalg.lstsq(design, lags, rcond=None)
    residual = lags - design @ coef
    dof = max(n - 2, 1)
    sigma2 = float(residual @ residual) / dof
    cov = sigma2 * np.linalg.inv(design.T @ design)
    return float(coef[0]), float(np.sqrt(max(cov[0, 0], 0.0)))


def _mono(audio: AudioBuffer) -> np.ndarray:
    samples = audio.samples
    return samples.mean(axis=1) if samples.ndim == 2 and samples.shape[1] > 1 else samples[:, 0]


def estimate_alignment(reference: AudioBuffer, target: AudioBuffer, *,
                       search_ms: float = 50.0, pre_s: float = 0.02,
                       post_s: float = 0.12, max_onsets: int = 400) -> Alignment:
    """Estimate per-attack offsets and their robust aggregate."""
    if reference.sample_rate != target.sample_rate:
        raise ValueError("both takes must share a sample rate")
    sr = reference.sample_rate
    ref = _mono(reference)
    tgt = _mono(target)
    events = detect_transients(reference).get("events", [])
    times = [float(e["time_s"]) for e in events][:max_onsets]
    pre, post = int(pre_s * sr), int(post_s * sr)
    search = max(1, int(search_ms * sr / 1000.0))
    offsets: list[Offset] = []
    for time_s in times:
        a = max(0, int(time_s * sr) - pre)
        b = min(ref.shape[0], int(time_s * sr) + post)
        segment = ref[a:b]
        if segment.shape[0] < 16:
            continue
        ta = max(0, a - search)
        tb = min(tgt.shape[0], b + search)
        window = tgt[ta:tb]
        if window.shape[0] < segment.shape[0]:
            continue
        numerator = correlate(window, segment, mode="valid", method="fft")
        energy = np.convolve(window ** 2, np.ones(segment.shape[0]), mode="valid")
        denominator = np.sqrt(np.maximum(energy, 1e-20)) * np.sqrt(np.sum(segment ** 2))
        normalized = numerator / np.maximum(denominator, 1e-20)
        index = int(np.argmax(normalized))
        offsets.append(Offset(time_s=time_s, lag_samples=int(ta + index - a),
                              lag_ms=float((ta + index - a) / sr * 1000.0),
                              correlation=float(normalized[index])))
    if not offsets:
        return Alignment(offsets=(), median_lag=0, mad_samples=0.0, median_correlation=0.0)
    lags = np.array([o.lag_samples for o in offsets], dtype=float)
    times = np.array([o.time_s for o in offsets], dtype=float)
    median = int(np.median(lags))
    mad = float(np.median(np.abs(lags - median)))
    corr = float(np.median([o.correlation for o in offsets]))
    slope, stderr = _slope(times, lags)
    return Alignment(offsets=tuple(offsets), median_lag=median, mad_samples=mad,
                     median_correlation=corr, slope_samples_per_s=slope,
                     slope_stderr=stderr, span_s=float(times.max() - times.min()))


def segment_offsets(result: Alignment, *, segment_s: float) -> list[dict]:
    """Group attack offsets into time segments.

    A single median hides drift; this shows, per segment, how far the take is
    from the reference.  No stretching is implied — the caller decides whether
    to move items or to leave a performance that genuinely varies alone.
    """
    if segment_s <= 0:
        raise ValueError("segment_s must be positive")
    buckets: dict[int, list[Offset]] = {}
    for offset in result.offsets:
        buckets.setdefault(int(offset.time_s // segment_s), []).append(offset)
    out = []
    for index in sorted(buckets):
        items = buckets[index]
        lags = [o.lag_samples for o in items]
        out.append({
            "start_s": index * segment_s,
            "end_s": (index + 1) * segment_s,
            "onsets": len(items),
            "median_lag_samples": int(np.median(lags)),
            "mad_samples": float(np.median(np.abs(np.array(lags) - np.median(lags)))),
            "median_correlation": float(np.median([o.correlation for o in items])),
        })
    return out


def apply_offset(audio: AudioBuffer, lag_samples: int) -> AudioBuffer:
    """Shift a take by ``-lag_samples`` so that it aligns with the reference."""
    shift = -int(lag_samples)
    samples = np.roll(audio.samples, shift, axis=0)
    if shift > 0:
        samples[:shift] = 0.0
    elif shift < 0:
        samples[shift:] = 0.0
    return AudioBuffer(samples=samples, sample_rate=audio.sample_rate)


def correlation(a: AudioBuffer, b: AudioBuffer) -> float:
    x, y = _mono(a), _mono(b)
    n = min(x.shape[0], y.shape[0])
    if n == 0:
        return 0.0
    x, y = x[:n], y[:n]
    denom = float(np.sqrt(np.sum(x ** 2) * np.sum(y ** 2)))
    return float(np.sum(x * y) / denom) if denom > 0 else 0.0


def align_group(reference: AudioBuffer, targets: dict[str, AudioBuffer], **kwargs) -> dict[str, Alignment]:
    """Estimate one alignment per take, keyed by the caller's names."""
    return {name: estimate_alignment(reference, target, **kwargs)
            for name, target in targets.items()}


def plan_group_alignment(session, *, moves: dict[str, int], sample_rate: int,
                         reason: str):
    """Plan item moves for several takes in one transaction.

    ``moves`` maps item GUID to the estimated lag of that take; every take is
    moved by ``-lag`` with a plain position change, so nothing is stretched.
    """
    from .session import EditOperation
    context = session.snapshot(include_items=True)
    positions = {item["guid"]: float(item["position_s"])
                 for track in context["tracks"] for item in track.get("items", [])}
    ops = []
    for item_guid, lag in moves.items():
        if not lag:
            continue
        if item_guid not in positions:
            raise ValueError(f"item {item_guid} is not in the project")
        position = positions[item_guid] - lag / float(sample_rate)
        if position < 0:
            # Sub-sample rounding can land a few microseconds below zero when
            # the item already starts exactly at the alignment offset.
            if position > -1.0 / sample_rate:
                position = 0.0
            else:
                raise ValueError("a shift would move an item before the project start")
        ops.append(EditOperation(kind="item.set", target_guid=item_guid,
                                 values={"position_s": round(position, 6)}))
    if not ops:
        raise ValueError("no non-zero moves to apply")
    return session.plan(ops, reason=reason)


def item_alignment_plan(session, *, track_guid: str, item_guid: str,
                        lag_samples: int, sample_rate: int, reason: str):
    """Plan moving an item by ``-lag_samples`` without stretching it.

    The item's own position is read from the live project; the edit is a plain
    ``item.set position_s`` so the audio content is untouched.
    """
    from .session import EditOperation
    context = session.snapshot(include_items=True)
    track = next((t for t in context["tracks"] if t["guid"] == track_guid), None)
    if track is None:
        raise ValueError("track_guid is not in the project")
    item = next((i for i in track.get("items", []) if i["guid"] == item_guid), None)
    if item is None:
        raise ValueError("item_guid is not on that track")
    position = float(item["position_s"]) - lag_samples / float(sample_rate)
    if position < 0:
        raise ValueError("the shift would move the item before the project start")
    return session.plan([EditOperation(kind="item.set", target_guid=item_guid,
                                       values={"position_s": round(position, 6)})],
                        reason=reason)


def alignment_report(reference: AudioBuffer, target: AudioBuffer, *,
                     policy: dict | None = None, **kwargs) -> dict:
    """Measure offsets and report them; never classify without a policy.

    The report always contains the slope with its standard error, the per-onset
    offsets, the per-segment offsets, and the correlation of the global shift.
    Whether a slope means "drifting" and whether a correlation is too low to
    trust is a project setting: pass ``policy`` with
    ``alignment.max_slope_sigma`` and ``alignment.min_correlation`` to obtain
    an ``action`` field.  Without a policy no decision is made here.
    """
    result = estimate_alignment(reference, target, **kwargs)
    before = correlation(reference, target)
    shifted = apply_offset(target, result.median_lag)
    after = correlation(reference, shifted)
    report = result.to_dict()
    report["segments"] = segment_offsets(result, segment_s=max(result.span_s / 4.0, 0.5))
    report["correlation_before"] = before
    report["correlation_after_global_shift"] = after
    if policy:
        sigma = policy.get("alignment.max_slope_sigma")
        min_corr = policy.get("alignment.min_correlation")
        drifting = (sigma is not None and result.slope_stderr > 0.0 and
                    abs(result.slope_samples_per_s) > float(sigma) * result.slope_stderr)
        unreliable = (min_corr is not None and
                      result.median_correlation < float(min_corr))
        report["action"] = ("refuse" if unreliable else
                            "per_segment" if drifting else "global_shift")
    return report
