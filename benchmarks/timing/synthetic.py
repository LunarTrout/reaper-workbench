"""Exact-truth synthetic timing cases for the editing method.

The generator moves source segments by known seconds without rate or pitch
change.  It records every source/target boundary, the applied shift and the
crossfade length, so a planner can be scored against the actual edit rather
than a waveform similarity heuristic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from reaper_audio.models import AudioBuffer


@dataclass(frozen=True, slots=True)
class MovedSegment:
    source_start_s: float
    source_end_s: float
    target_start_s: float
    target_end_s: float
    shift_s: float
    fade_in_s: float
    fade_out_s: float

    def to_dict(self) -> dict[str, float]:
        return {
            "source_start_s": round(self.source_start_s, 7),
            "source_end_s": round(self.source_end_s, 7),
            "target_start_s": round(self.target_start_s, 7),
            "target_end_s": round(self.target_end_s, 7),
            "shift_s": round(self.shift_s, 7),
            "fade_in_s": self.fade_in_s,
            "fade_out_s": self.fade_out_s,
        }


def _fade_window(length: int, fade_samples: int) -> np.ndarray:
    window = np.ones(length, dtype=np.float64)
    if fade_samples <= 0 or length == 0:
        return window
    edge = min(fade_samples, length // 2)
    if edge <= 0:
        return window
    ramp = np.linspace(0.0, 1.0, edge, endpoint=False, dtype=np.float64)
    window[:edge] *= ramp
    window[-edge:] *= ramp[::-1]
    return window


def move_segments(audio: AudioBuffer,
                  segments: Sequence[Mapping[str, Any]], *,
                  fade_s: float = 0.0045) -> tuple[AudioBuffer, list[MovedSegment]]:
    """Return a copy with each source segment moved by its ``shift_s``.

    Segments must be non-overlapping in source time.  The output is long enough
    for the furthest target; overlaps at targets are summed, and the truth keeps
    the exact source and target boundaries.
    """
    rate = audio.sample_rate
    source = np.asarray(audio.samples, dtype=np.float64)
    if source.ndim == 1:
        source = source[:, None]
    ordered = sorted((dict(row) for row in segments),
                     key=lambda row: float(row["source_start_s"]))
    for left, right in zip(ordered, ordered[1:]):
        if float(left["source_end_s"]) > float(right["source_start_s"]) + 1e-9:
            raise ValueError("source segments overlap")
    moved: list[MovedSegment] = []
    pieces: list[tuple[int, np.ndarray]] = []
    for row in ordered:
        source_start = float(row["source_start_s"])
        source_end = float(row["source_end_s"])
        shift = float(row["shift_s"])
        if source_start < 0 or source_end <= source_start or source_end > len(source) / rate + 1e-9:
            raise ValueError("segment is outside the source audio")
        a = int(round(source_start * rate))
        b = int(round(source_end * rate))
        piece = source[a:b].copy()
        target = int(round((source_start + shift) * rate))
        fade = max(0, int(round(fade_s * rate)))
        piece *= _fade_window(len(piece), fade)[:, None]
        pieces.append((target, piece))
        moved.append(MovedSegment(
            source_start_s=a / rate, source_end_s=b / rate,
            target_start_s=target / rate,
            target_end_s=(target + len(piece)) / rate,
            shift_s=shift, fade_in_s=fade_s, fade_out_s=fade_s,
        ))
    end = max([len(source)] + [target + len(piece) for target, piece in pieces])
    output = np.zeros((end, source.shape[1]), dtype=np.float64)
    for target, piece in pieces:
        output[target:target + len(piece)] += piece
    return AudioBuffer(output, rate, source=audio.source), moved


def click_track(times: Iterable[float], *, duration_s: float,
                sample_rate: int = 48_000, amplitude: float = 0.8,
                length_s: float = 0.002) -> AudioBuffer:
    """Generate a deterministic two-channel click track for event tests."""
    samples = np.zeros((int(duration_s * sample_rate), 2), dtype=np.float64)
    length = max(1, int(length_s * sample_rate))
    for time_s in times:
        index = int(round(float(time_s) * sample_rate))
        if 0 <= index < len(samples):
            samples[index:index + length, :] = amplitude
    return AudioBuffer(samples, sample_rate)


def synthetic_case(events_s: Sequence[float], shifts_s: Sequence[float], *,
                   duration_s: float, sample_rate: int = 48_000,
                   amplitude: float = 0.8, fade_s: float = 0.0045) -> dict[str, Any]:
    """Build a click track plus a shifted version with exact truth.

    Each event is a short source segment; its target is ``event + shift``.
    The returned truth contains the source/target geometry and the target list
    that a planner should reach.
    """
    if len(events_s) != len(shifts_s):
        raise ValueError("events and shifts must have the same length")
    segment_length = 0.004
    original = click_track(events_s, duration_s=duration_s,
                           sample_rate=sample_rate, amplitude=amplitude,
                           length_s=segment_length)
    segments = [{"source_start_s": float(event),
                 "source_end_s": float(event) + segment_length,
                 "shift_s": float(shift)}
                for event, shift in zip(events_s, shifts_s)]
    corrupted, moved = move_segments(original, segments, fade_s=fade_s)
    # Truth is the correction back to the original performance, not the
    # corruption itself.  The corrupted event sits at ``event + shift`` and the
    # desired target is ``event``.
    correction = [{
        "source_start_s": float(event) + float(shift),
        "source_end_s": float(event) + segment_length + float(shift),
        "target_start_s": float(event),
        "target_end_s": float(event) + segment_length,
        "shift_s": -float(shift),
        "fade_in_s": fade_s,
        "fade_out_s": fade_s,
    } for event, shift in zip(events_s, shifts_s)]
    return {
        "original": original,
        "corrupted": corrupted,
        "truth_segments": correction,
        "corruption_segments": [row.to_dict() for row in moved],
        "targets": [float(value) for value in events_s],
        "corrupted_events_s": [float(event) + float(shift)
                               for event, shift in zip(events_s, shifts_s)],
    }
