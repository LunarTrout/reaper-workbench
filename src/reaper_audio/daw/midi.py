"""Musical-grid MIDI operations: generate, check, preview, replace.

The whole point of this module is that a note's position is decided in quarter
notes (QN) and only then converted to project seconds through the project's
tempo map.  Multiplying ``60 / bpm`` across a tempo change is exactly the bug
that put ride and fill notes off the grid at 155.5 -> 160 -> 80 -> 72.5, so no
such arithmetic exists here.

What is *not* here: no drum pattern, no genre, no fixed section, no default
subdivision, no track names, no "five seconds".  A pattern is caller data
``[{beat, pitch, velocity}, ...]``; subdivision, accents and humanisation are
parameters.  Bars and the tempo map come from the project.

Positions have two clocks:

* **project QN** — musical position on the project timeline (bar 1 starts at
  QN 0).  This is what the caller reasons about.
* **source QN / take PPQ** — the position inside the take.  It differs from
  project QN by the item position and the take start offset (and playback
  rate), which is why a trimmed item is not generated as if it started at its
  first visible sample.

Note times sent to the bridge (``midi.replace``) are **absolute project
seconds**, the same clock ``midi.notes`` returns.  REAPER converts them to PPQ
with ``MIDI_GetPPQPosFromProjTime``, so the take offset is applied by REAPER
itself; ``start_qn`` on each generated note is the source PPQ the note will
have, and makes the offset visible to the caller and to tests.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace as dc_replace
from typing import Any, Protocol

from ..errors import BridgeUnavailable, ValidationError

_QN_EPS = 1e-9
_TIME_EPS = 1e-6
_MAX_PHRASE_NOTES = 100_000
_MAX_PATTERN_NOTES = 10_000

__all__ = [
    "TempoMap",
    "TempoPoint",
    "MidiItem",
    "Note",
    "GeneratedPhrase",
    "GridReport",
    "MidiService",
    "FragmentRenderer",
    "generate",
    "grid_check",
    "replace_notes",
    "item_statistics",
]


# ---------------------------------------------------------------------------
# small validators
# ---------------------------------------------------------------------------

def _number(value: Any, field: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValidationError(f"{field} must be finite")
    if minimum is not None and number < minimum:
        raise ValidationError(f"{field} must be >= {minimum}")
    return number


def _integer(value: Any, field: str, *, minimum: int | None = None,
             maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{field} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValidationError(f"{field} must be <= {maximum}")
    return value


def _positive_int(value: Any, field: str) -> int:
    return _integer(value, field, minimum=1)


# ---------------------------------------------------------------------------
# tempo map
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TempoPoint:
    """One effective tempo/time-signature position on the project timeline."""

    time_s: float
    qn: float
    bpm: float
    numerator: int
    denominator: int
    linear: bool
    measure: int
    beat: float


@dataclass(frozen=True, slots=True)
class _MeterSpan:
    """A run of bars sharing one time signature, anchored at a bar start."""

    start_bar: int  # zero-based measure number
    start_qn: float
    beat_qn: float
    measure_qn: float
    numerator: int
    denominator: int


@dataclass(frozen=True, slots=True)
class TempoMap:
    """Piecewise project time map in QN, built from ``project.tempo``.

    Points are REAPER tempo/time-signature markers.  A marker with ``bpm`` 0
    means "tempo unchanged" in REAPER, and a marker with numerator/denominator
    0 means "time signature unchanged"; both are inherited here instead of
    being treated as real values.  ``beat`` is a quarter-note offset inside the
    measure (REAPER's ``beatpos``), not a meter-dependent beat count.  Linear
    markers ramp BPM linearly in time to the next marker (REAPER's "gradually
    transition tempo").
    """

    points: tuple[TempoPoint, ...]
    meters: tuple[_MeterSpan, ...]
    source: str
    warnings: tuple[dict[str, Any], ...] = ()

    # -- construction -------------------------------------------------------

    @classmethod
    def from_context(cls, tempo: Mapping[str, Any] | None) -> "TempoMap":
        if not isinstance(tempo, Mapping):
            raise BridgeUnavailable("REAPER returned no project tempo context")
        base_bpm = _number(tempo.get("bpm"), "tempo.bpm", minimum=1e-9)
        base_num = _integer(tempo.get("numerator"), "tempo.numerator", minimum=1)
        base_den = _integer(tempo.get("denominator"), "tempo.denominator", minimum=1)
        raw_markers = tempo.get("markers") or []
        if not isinstance(raw_markers, list):
            raise BridgeUnavailable("REAPER returned an invalid tempo-marker list")

        markers = sorted(
            (marker for marker in raw_markers if isinstance(marker, Mapping)),
            key=lambda marker: float(marker.get("time_s") or 0.0),
        )
        if len(markers) != len(raw_markers):
            raise BridgeUnavailable("REAPER returned a non-object tempo marker")

        points: list[TempoPoint] = []
        effective_bpm = base_bpm
        effective_num, effective_den = base_num, base_den
        anchor: TempoPoint | None = None

        for index, marker in enumerate(markers):
            time_s = _number(marker.get("time_s"), f"tempo.markers[{index}].time_s")
            raw_measure = marker.get("measure")
            raw_beat = marker.get("beat")
            measure = _integer(raw_measure, f"tempo.markers[{index}].measure") \
                if raw_measure is not None else -1
            beat = _number(raw_beat, f"tempo.markers[{index}].beat") \
                if raw_beat is not None else -1.0
            free_position = measure < 0 or beat < 0
            marker_bpm = marker.get("bpm")
            if marker_bpm is not None:
                marker_bpm = _number(marker_bpm, f"tempo.markers[{index}].bpm")
                if marker_bpm > 0:
                    effective_bpm = marker_bpm
            marker_num = marker.get("numerator")
            marker_den = marker.get("denominator")
            if marker_num is not None and marker_den is not None:
                marker_num = _integer(marker_num, f"tempo.markers[{index}].numerator")
                marker_den = _integer(marker_den, f"tempo.markers[{index}].denominator")
                if marker_num > 0 and marker_den > 0:
                    effective_num, effective_den = marker_num, marker_den

            if free_position:
                # REAPER allows a tempo marker without a measure/beat position
                # (SetTempoTimeSigMarker accepts -1).  Its QN comes from the
                # tempo integral, not from a bar number.  A time-signature
                # change at such a position cannot be placed on the bar grid,
                # so it is refused instead of guessed.
                if points and (points[-1].numerator != effective_num or
                               points[-1].denominator != effective_den):
                    raise BridgeUnavailable(
                        "A time-signature change sits at a tempo marker without "
                        "a bar position",
                        details={"marker_index": index, "time_s": time_s},
                    )
                if not points:
                    qn = time_s * base_bpm / 60.0
                else:
                    previous = points[-1]
                    delta_t = time_s - previous.time_s
                    if delta_t < -_TIME_EPS:
                        raise BridgeUnavailable(
                            "REAPER returned tempo markers in decreasing time order",
                            details={"marker_index": index, "time_s": time_s,
                                     "previous_time_s": previous.time_s},
                        )
                    ramp_bpm = (0.5 * (previous.bpm + effective_bpm)
                                if previous.linear else previous.bpm)
                    qn = previous.qn + max(0.0, delta_t) * ramp_bpm / 60.0
            elif anchor is not None:
                if (measure < anchor.measure or
                        (measure == anchor.measure and beat < anchor.beat)):
                    raise BridgeUnavailable(
                        "REAPER returned tempo markers out of musical order",
                        details={"marker_index": index, "measure": measure,
                                 "beat": beat, "previous_measure": anchor.measure,
                                 "previous_beat": anchor.beat},
                    )
                qn = (anchor.qn
                      + (measure - anchor.measure) * anchor.numerator * 4.0 / anchor.denominator
                      + (beat - anchor.beat) * 4.0 / anchor.denominator)
            else:
                qn = (measure * base_num * 4.0 / base_den
                      + beat * 4.0 / base_den)

            point = TempoPoint(
                time_s=time_s, qn=qn, bpm=effective_bpm,
                numerator=effective_num, denominator=effective_den,
                linear=bool(marker.get("linear")), measure=measure, beat=beat,
            )
            points.append(point)
            if not free_position:
                anchor = point

        if not points:
            points = [TempoPoint(time_s=0.0, qn=0.0, bpm=base_bpm,
                                 numerator=base_num, denominator=base_den,
                                 linear=False, measure=0, beat=0.0)]
            source = "project.base_tempo_no_markers"
        else:
            source = "project.tempo_markers"
            if points[0].time_s > _TIME_EPS:
                expected = points[0].time_s * base_bpm / 60.0
                if abs(points[0].qn - expected) > _qn_tolerance(points[0].qn):
                    raise BridgeUnavailable(
                        "The first tempo marker disagrees with the project base tempo",
                        details={"marker_qn": points[0].qn,
                                 "base_expected_qn": expected,
                                 "marker_time_s": points[0].time_s,
                                 "base_bpm": base_bpm},
                    )
        integration_warnings = _check_tempo_integration(points)

        meters: list[_MeterSpan] = [_MeterSpan(
            start_bar=0, start_qn=0.0, beat_qn=4.0 / base_den,
            measure_qn=base_num * 4.0 / base_den,
            numerator=base_num, denominator=base_den,
        )]
        for index, point in enumerate(points):
            if point.measure < 0 or point.beat < 0:
                continue
            if point.time_s == 0.0 and point.measure == 0 and point.beat == 0.0 and index == 0:
                continue
            start_qn = point.qn - point.beat
            last = meters[-1]
            if point.measure < last.start_bar or start_qn < last.start_qn - _QN_EPS:
                raise BridgeUnavailable(
                    "The tempo map's bar positions are not monotonic",
                    details={"marker_index": index,
                             "measure": point.measure,
                             "bar_start_qn": start_qn,
                             "previous_bar_start_qn": last.start_qn},
                )
            meters.append(_MeterSpan(
                start_bar=point.measure, start_qn=start_qn,
                beat_qn=4.0 / point.denominator,
                measure_qn=point.numerator * 4.0 / point.denominator,
                numerator=point.numerator, denominator=point.denominator,
            ))
        # A later span for the same measure is a partial-measure marker; the
        # later start wins for bar lookups, so keep only the last one.
        deduped: list[_MeterSpan] = []
        for span in meters:
            if deduped and deduped[-1].start_bar == span.start_bar:
                deduped[-1] = span
            else:
                deduped.append(span)
        return cls(points=tuple(points), meters=tuple(deduped), source=source,
                   warnings=tuple(integration_warnings))

    # -- basic lookup -------------------------------------------------------

    def _point_index_at_qn(self, qn: float) -> int:
        index = 0
        for position, point in enumerate(self.points):
            if point.qn <= qn + _QN_EPS:
                index = position
            else:
                break
        return index

    def _point_index_at_time(self, time_s: float) -> int:
        index = 0
        for position, point in enumerate(self.points):
            if point.time_s <= time_s + _TIME_EPS:
                index = position
            else:
                break
        return index

    def _span_at_qn(self, qn: float) -> _MeterSpan:
        span = self.meters[0]
        for candidate in self.meters:
            if candidate.start_qn <= qn + _QN_EPS:
                span = candidate
            else:
                break
        return span

    # -- time <-> QN --------------------------------------------------------

    def qn_to_time(self, qn: float) -> float:
        """Project time of a project QN position, exact inside each segment."""
        qn = _number(qn, "qn")
        index = self._point_index_at_qn(qn)
        point = self.points[index]
        if qn <= point.qn:
            return point.time_s + (qn - point.qn) * 60.0 / point.bpm
        if index + 1 >= len(self.points):
            return point.time_s + (qn - point.qn) * 60.0 / point.bpm
        nxt = self.points[index + 1]
        delta_qn = nxt.qn - point.qn
        delta_t = nxt.time_s - point.time_s
        if delta_qn <= _QN_EPS:
            return point.time_s
        if not point.linear or abs(nxt.bpm - point.bpm) <= 1e-12:
            return point.time_s + (qn - point.qn) * 60.0 / point.bpm
        return point.time_s + delta_t * _linear_fraction(
            (qn - point.qn) / delta_qn, point.bpm, nxt.bpm)

    def time_to_qn(self, time_s: float) -> float:
        """Project QN of a project time, exact inside each segment."""
        time_s = _number(time_s, "time_s")
        index = self._point_index_at_time(time_s)
        point = self.points[index]
        if time_s <= point.time_s:
            return point.qn + (time_s - point.time_s) * point.bpm / 60.0
        if index + 1 >= len(self.points):
            return point.qn + (time_s - point.time_s) * point.bpm / 60.0
        nxt = self.points[index + 1]
        delta_t = nxt.time_s - point.time_s
        if delta_t <= _TIME_EPS:
            return point.qn
        fraction = (time_s - point.time_s) / delta_t
        if not point.linear or abs(nxt.bpm - point.bpm) <= 1e-12:
            return point.qn + fraction * (nxt.qn - point.qn)
        return point.qn + delta_t / 60.0 * (
            point.bpm * fraction + 0.5 * (nxt.bpm - point.bpm) * fraction * fraction)

    # -- bars ---------------------------------------------------------------

    def bar_start_qn(self, bar: int) -> float:
        """QN of the first beat of a 1-based bar."""
        bar = _positive_int(bar, "bar")
        measure = bar - 1
        span = self.meters[0]
        for candidate in self.meters:
            if candidate.start_bar <= measure:
                span = candidate
            else:
                break
        return span.start_qn + (measure - span.start_bar) * span.measure_qn

    def bar_length_qn(self, bar: int) -> float:
        bar = _positive_int(bar, "bar")
        return self.bar_start_qn(bar + 1) - self.bar_start_qn(bar)

    def meter_at_bar(self, bar: int) -> tuple[int, int, float, float]:
        """``(numerator, denominator, beat_qn, measure_qn)`` for a bar."""
        bar = _positive_int(bar, "bar")
        measure = bar - 1
        span = self.meters[0]
        for candidate in self.meters:
            if candidate.start_bar <= measure:
                span = candidate
            else:
                break
        return span.numerator, span.denominator, span.beat_qn, span.measure_qn

    def qn_to_bar_beat(self, qn: float) -> tuple[int, float]:
        """``(1-based bar, QN offset inside the bar)`` for a project QN."""
        qn = _number(qn, "qn")
        span = self._span_at_qn(qn)
        offset = qn - span.start_qn
        if span.measure_qn <= _QN_EPS:
            return span.start_bar + 1, 0.0
        bar = span.start_bar + int(math.floor(offset / span.measure_qn + 1e-12))
        bar_start = span.start_qn + (bar - span.start_bar) * span.measure_qn
        return bar + 1, qn - bar_start

    def nearest_grid_qn(self, qn: float, subdivision: float) -> float:
        """Nearest point of the bar-anchored ``1/subdivision`` QN grid."""
        qn = _number(qn, "qn")
        subdivision = _number(subdivision, "subdivision", minimum=1e-9)
        step = 1.0 / subdivision
        bar, _ = self.qn_to_bar_beat(qn)
        start = self.bar_start_qn(bar)
        end = self.bar_start_qn(bar + 1)
        k = math.floor((qn - start) / step)
        candidates = [start + (k + shift) * step for shift in (-1, 0, 1, 2)]
        candidates.append(end)
        return min(candidates, key=lambda candidate: abs(candidate - qn))


def _qn_tolerance(value: float, bpm: float | None = None) -> float:
    """Float slack plus a real-world marker rounding allowance.

    REAPER stores tempo-marker times rounded to the edit-cursor precision, so a
    musically exact marker can differ by a few milliseconds. The allowance is a
    time tolerance converted through the local tempo, not a musical constant.
    """
    base = max(1e-6, abs(value) * 1e-9)
    if bpm:
        base = max(base, 0.005 * float(bpm) / 60.0)
    return base


def _linear_fraction(normalized_qn: float, bpm_start: float, bpm_end: float) -> float:
    """Invert the quadratic beat count of a linear-in-time tempo ramp."""
    if bpm_end == bpm_start:
        return normalized_qn
    slope = bpm_end - bpm_start
    mean = 0.5 * (bpm_start + bpm_end)
    discriminant = bpm_start * bpm_start + 2.0 * slope * normalized_qn * mean
    if discriminant <= 0:
        return normalized_qn
    root = (-bpm_start + math.sqrt(discriminant)) / slope
    if root < -1e-9 or root > 1.0 + 1e-9:
        other = (-bpm_start - math.sqrt(discriminant)) / slope
        if 0.0 <= other <= 1.0:
            return other
    return min(1.0, max(0.0, root))


def _check_tempo_integration(points: list[TempoPoint]) -> list[dict[str, Any]]:
    """Report positions whose measure grid disagrees with the tempo integral.

    Real projects carry rounded marker times and meter edits; a disagreement is
    a fact about the project, not a reason to refuse every MIDI operation.
    Decreasing time order stays fatal because it cannot be interpreted.
    """
    warnings: list[dict[str, Any]] = []
    for index in range(len(points) - 1):
        current, nxt = points[index], points[index + 1]
        delta_t = nxt.time_s - current.time_s
        delta_qn = nxt.qn - current.qn
        if delta_t < -_TIME_EPS:
            raise BridgeUnavailable(
                "REAPER returned tempo markers in decreasing time order",
                details={"marker_index": index + 1, "time_s": nxt.time_s,
                         "previous_time_s": current.time_s},
            )
        if delta_t <= _TIME_EPS:
            if abs(delta_qn) > _qn_tolerance(delta_qn):
                raise BridgeUnavailable(
                    "Two tempo markers share a time but not a musical position",
                    details={"marker_index": index + 1, "delta_qn": delta_qn},
                )
            continue
        integrated_bpm = (0.5 * (current.bpm + nxt.bpm)
                          if current.linear else current.bpm)
        expected_qn = delta_t * integrated_bpm / 60.0
        tolerance = max(_qn_tolerance(expected_qn, integrated_bpm), abs(delta_qn) * 1e-6)
        if abs(expected_qn - delta_qn) > tolerance:
            warnings.append({
                "code": "tempo_map_measure_mismatch",
                "marker_index": index + 1,
                "time_s": nxt.time_s,
                "measure": nxt.measure,
                "beat": nxt.beat,
                "musical_delta_qn": delta_qn,
                "tempo_delta_qn": expected_qn,
                "bpm": current.bpm,
                "next_bpm": nxt.bpm,
            })
    return warnings


# ---------------------------------------------------------------------------
# item geometry and notes
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Note:
    """One MIDI note with both source PPQ and absolute project seconds."""

    pitch: int
    velocity: int
    start_qn: float
    end_qn: float
    start_s: float
    end_s: float
    bar: int
    beat: float
    channel: int = 0
    muted: bool = False

    def __post_init__(self) -> None:
        _integer(self.pitch, "pitch", minimum=0, maximum=127)
        _integer(self.velocity, "velocity", minimum=1, maximum=127)
        _integer(self.channel, "channel", minimum=0, maximum=15)
        if not isinstance(self.muted, bool):
            raise ValidationError("muted must be a boolean")
        _number(self.start_qn, "start_qn")
        _number(self.end_qn, "end_qn")
        if float(self.end_qn) < float(self.start_qn):
            raise ValidationError("A note's end_qn must be >= start_qn")
        if _number(self.end_s, "end_s") <= _number(self.start_s, "start_s"):
            raise ValidationError("A note must end after it starts")

    def to_dict(self) -> dict[str, Any]:
        return {
            "pitch": self.pitch,
            "velocity": self.velocity,
            "start_qn": self.start_qn,
            "end_qn": self.end_qn,
            "start_s": self.start_s,
            "end_s": self.end_s,
            "bar": self.bar,
            "beat": self.beat,
            "channel": self.channel,
            "muted": self.muted,
        }


@dataclass(frozen=True, slots=True)
class MidiItem:
    """A MIDI item and the two clocks it lives on."""

    guid: str
    position_s: float
    length_s: float
    take_offset_qn: float
    playrate: float
    tempo: TempoMap
    notes: tuple[Note, ...] = ()

    @property
    def end_s(self) -> float:
        return self.position_s + self.length_s

    @property
    def position_qn(self) -> float:
        return self.tempo.time_to_qn(self.position_s)

    @property
    def end_qn(self) -> float:
        return self.tempo.time_to_qn(self.end_s)

    def source_qn(self, project_qn: float) -> float:
        """Take PPQ for a project QN, including take offset and playrate."""
        return (self.take_offset_qn
                + (project_qn - self.position_qn) * self.playrate)

    def project_qn(self, source_qn: float) -> float:
        return self.position_qn + (source_qn - self.take_offset_qn) / self.playrate

    def contains(self, start_s: float, end_s: float) -> bool:
        return (start_s >= self.position_s - _TIME_EPS
                and end_s <= self.end_s + _TIME_EPS)

    def source_window_s(self) -> tuple[float, float]:
        """Project-time window whose notes have a non-negative take PPQ.

        A positive take offset exposes source audio before the item's visible
        start; those notes are hidden but still part of the take, so a replace
        must be allowed to preserve them.  A negative offset means the source
        starts after the item start and PPQ below zero is invalid.
        """
        return self.tempo.qn_to_time(self.project_qn(0.0)), self.end_s

    @classmethod
    def from_context(cls, item: Mapping[str, Any], tempo: TempoMap) -> "MidiItem":
        guid = item.get("guid")
        if not isinstance(guid, str) or not guid:
            raise ValidationError("A MIDI item needs a non-empty GUID")
        position_s = _number(item.get("position_s"), "item.position_s", minimum=0.0)
        length_s = _number(item.get("length_s"), "item.length_s", minimum=1e-9)
        playrate = item.get("playrate")
        playrate = 1.0 if playrate in (None, 0) else _number(
            playrate, "item.playrate", minimum=1e-9)
        start_offset_s = item.get("start_offset_s")
        start_offset_s = 0.0 if start_offset_s is None else _number(
            start_offset_s, "item.start_offset_s")
        raw_notes = _raw_take_notes(item)

        explicit_offset = item.get("take_offset_qn", item.get("start_offset_qn"))
        if explicit_offset is not None:
            take_offset_qn = _number(explicit_offset, "item.take_offset_qn")
        elif start_offset_s > 0:
            # REAPER already states the trim; measuring backwards from the item
            # start is exact for the active tempo span and needs no guessing.
            take_offset_qn = (tempo.time_to_qn(position_s)
                              - tempo.time_to_qn(position_s - start_offset_s))
        else:
            # REAPER reports no trim: the take starts at the item start.  Do not
            # reconstruct an offset from note PPQ — a note list written through
            # midi.replace carries project seconds, and combining that with the
            # take PPQ grid produces a false disagreement.
            take_offset_qn = 0.0
        base = cls(guid=guid, position_s=position_s, length_s=length_s,
                   take_offset_qn=take_offset_qn, playrate=playrate,
                   tempo=tempo, notes=())
        notes = tuple(_note_from_take(raw, base, index)
                      for index, raw in enumerate(raw_notes))
        return dc_replace(base, notes=notes)


def _raw_take_notes(item: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    take = item.get("active_take") or {}
    if isinstance(take, Mapping) and take.get("is_midi") is False:
        raise ValidationError("The item's active take is not MIDI")
    midi = (take or {}).get("midi") if isinstance(take, Mapping) else None
    if isinstance(midi, Mapping) and midi.get("error"):
        raise BridgeUnavailable(
            "REAPER could not read the MIDI take",
            details={"item_guid": item.get("guid"), "error": str(midi["error"])},
        )
    if isinstance(midi, Mapping) and midi.get("truncated") is True:
        raise BridgeUnavailable(
            "The MIDI take's note list is truncated; refusing a partial view",
            details={"item_guid": item.get("guid"),
                     "note_count": midi.get("note_count"),
                     "returned_note_count": midi.get("returned_note_count")},
        )
    raw = (midi or {}).get("notes") if isinstance(midi, Mapping) else None
    if raw is None:
        raw = item.get("notes")
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(note, Mapping) for note in raw):
        raise BridgeUnavailable("REAPER returned an invalid MIDI note list")
    return raw


def _infer_take_offset(notes: Sequence[Mapping[str, Any]], position_s: float,
                       playrate: float, tempo: TempoMap) -> float | None:
    guesses: list[float] = []
    position_qn = tempo.time_to_qn(position_s)
    for note in notes:
        ppq = note.get("start_ppq")
        start_s = note.get("start_s")
        if not isinstance(ppq, (int, float)) or isinstance(ppq, bool):
            continue
        if not isinstance(start_s, (int, float)) or isinstance(start_s, bool):
            continue
        project_qn = tempo.time_to_qn(float(start_s))
        guesses.append(float(ppq) - (project_qn - position_qn) * playrate)
    if not guesses:
        return None
    low, high = min(guesses), max(guesses)
    if high - low > 1e-3:
        raise BridgeUnavailable(
            "The take's notes disagree about its start offset",
            details={"min_offset_qn": low, "max_offset_qn": high},
        )
    return sum(guesses) / len(guesses)


def _note_from_take(raw: Mapping[str, Any], item: MidiItem, index: int) -> Note:
    start_s = _number(raw.get("start_s"), f"notes[{index}].start_s")
    end_s = _number(raw.get("end_s"), f"notes[{index}].end_s")
    project_qn = item.tempo.time_to_qn(start_s)
    start_qn = raw.get("start_ppq")
    if isinstance(start_qn, (int, float)) and not isinstance(start_qn, bool):
        start_qn = float(start_qn)
    else:
        start_qn = item.source_qn(project_qn)
    end_qn = raw.get("end_ppq")
    if isinstance(end_qn, (int, float)) and not isinstance(end_qn, bool):
        end_qn = float(end_qn)
    else:
        end_qn = item.source_qn(item.tempo.time_to_qn(end_s))
    bar, beat = item.tempo.qn_to_bar_beat(project_qn)
    return Note(pitch=_integer(raw.get("pitch"), f"notes[{index}].pitch",
                               minimum=0, maximum=127),
                velocity=_integer(raw.get("velocity"), f"notes[{index}].velocity",
                                  minimum=1, maximum=127),
                start_qn=start_qn, end_qn=end_qn, start_s=start_s, end_s=end_s,
                bar=bar, beat=beat,
                channel=_integer(raw.get("channel", 0), f"notes[{index}].channel",
                                 minimum=0, maximum=15),
                muted=bool(raw.get("muted", False)))


# ---------------------------------------------------------------------------
# pattern, accents, humanisation
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _PatternNote:
    beat: float
    pitch: int
    velocity: int
    duration: float | None
    channel: int
    muted: bool


@dataclass(frozen=True, slots=True)
class _Accent:
    absolute: int | None = None
    delta: int | None = None


@dataclass(frozen=True, slots=True)
class _Humanize:
    time_ms: float = 0.0
    velocity: int = 0
    seed: int = 0

    @property
    def active(self) -> bool:
        return self.time_ms > 0 or self.velocity > 0


def _parse_pattern(pattern: Any, subdivision: int, tempo: TempoMap,
                   from_bar: int, range_qn: float) -> tuple[list[_PatternNote], float]:
    """Parse caller data; never invent a period for a bare note list."""
    if isinstance(pattern, Mapping):
        raw_notes = pattern.get("notes")
        length_beats = pattern.get("length_beats")
        if length_beats is None and pattern.get("bars") is not None:
            bars = _positive_int(pattern.get("bars"), "pattern.bars")
            length_beats = bars * tempo.meter_at_bar(from_bar)[3]
    elif isinstance(pattern, Sequence) and not isinstance(pattern, (str, bytes)):
        raw_notes = pattern
        length_beats = None
    else:
        raise ValidationError("pattern must be a note list or a mapping with notes")
    if not isinstance(raw_notes, Sequence) or isinstance(raw_notes, (str, bytes)):
        raise ValidationError("pattern notes must be a sequence")
    if not raw_notes:
        raise ValidationError("pattern must contain at least one note")
    if len(raw_notes) > _MAX_PATTERN_NOTES:
        raise ValidationError(
            "pattern has too many notes",
            details={"notes": len(raw_notes), "limit": _MAX_PATTERN_NOTES})

    notes: list[_PatternNote] = []
    grid_tolerance = 1e-5
    for index, raw in enumerate(raw_notes):
        if not isinstance(raw, Mapping):
            raise ValidationError(f"pattern.notes[{index}] must be an object")
        beat = _number(raw.get("beat"), f"pattern.notes[{index}].beat", minimum=0.0)
        on_grid = beat * subdivision
        if abs(on_grid - round(on_grid)) > grid_tolerance:
            raise ValidationError(
                "A pattern beat is not on the requested subdivision grid",
                details={"note_index": index, "beat": beat,
                         "subdivision": subdivision},
            )
        duration = raw.get("duration")
        duration = None if duration is None else _number(
            duration, f"pattern.notes[{index}].duration", minimum=1e-9)
        notes.append(_PatternNote(
            beat=beat,
            pitch=_integer(raw.get("pitch"), f"pattern.notes[{index}].pitch",
                           minimum=0, maximum=127),
            velocity=_integer(raw.get("velocity"), f"pattern.notes[{index}].velocity",
                              minimum=1, maximum=127),
            duration=duration,
            channel=_integer(raw.get("channel", 0),
                             f"pattern.notes[{index}].channel", minimum=0, maximum=15),
            muted=bool(raw.get("muted", False)),
        ))

    if length_beats is None:
        # A bare onset list carries no period.  Applying a "one bar" or "whole
        # bars" rule here would be a musical assumption, so a bare list is one
        # non-repeating statement over the requested bars.  To repeat a motif,
        # the caller passes {"notes": [...], "length_beats": ...} explicitly.
        length_beats = range_qn
    else:
        length_beats = _number(length_beats, "pattern.length_beats", minimum=1e-9)
    for index, note in enumerate(notes):
        if note.beat >= length_beats - _QN_EPS:
            raise ValidationError(
                "A pattern note starts outside the pattern period",
                details={"note_index": index, "beat": note.beat,
                         "length_beats": length_beats},
            )
    return notes, float(length_beats)


def _accent_key(beat: float, subdivision: int) -> int:
    return int(round(beat * subdivision))


def _parse_accents(accents: Any, subdivision: int) -> dict[int, _Accent]:
    if accents is None:
        return {}
    entries: list[Mapping[str, Any]] = []
    if isinstance(accents, Mapping):
        beats = accents.get("beats")
        if beats is None:
            entries = [accents]
        else:
            if not isinstance(beats, Sequence) or isinstance(beats, (str, bytes)):
                raise ValidationError("accents.beats must be a sequence")
            template = {key: value for key, value in accents.items() if key != "beats"}
            for beat in beats:
                entries.append({**template, "beat": beat})
    elif isinstance(accents, Sequence) and not isinstance(accents, (str, bytes)):
        for entry in accents:
            if not isinstance(entry, Mapping):
                raise ValidationError(
                    "accents entries must carry a beat and a velocity change")
            entries.append(entry)
    else:
        raise ValidationError(
            "accents must be a mapping or a sequence of beat/velocity entries")

    result: dict[int, _Accent] = {}
    for index, entry in enumerate(entries):
        beat = _number(entry.get("beat"), f"accents[{index}].beat", minimum=0.0)
        on_grid = beat * subdivision
        if abs(on_grid - round(on_grid)) > 1e-5:
            raise ValidationError(
                "An accent beat is not on the requested subdivision grid",
                details={"accent_index": index, "beat": beat,
                         "subdivision": subdivision},
            )
        has_absolute = entry.get("velocity") is not None
        has_delta = entry.get("velocity_delta") is not None
        if has_absolute == has_delta:
            raise ValidationError(
                "An accent needs exactly one of velocity or velocity_delta",
                details={"accent_index": index},
            )
        accent = (_Accent(absolute=_integer(entry.get("velocity"),
                                            f"accents[{index}].velocity",
                                            minimum=1, maximum=127))
                  if has_absolute else
                  _Accent(delta=_integer(entry.get("velocity_delta"),
                                         f"accents[{index}].velocity_delta")))
        key = _accent_key(beat, subdivision)
        if key in result and result[key] != accent:
            raise ValidationError(
                "Conflicting accents for the same beat",
                details={"accent_index": index, "beat": beat},
            )
        result[key] = accent
    return result


def _parse_humanize(humanize: Any) -> _Humanize:
    if humanize is None:
        return _Humanize()
    if isinstance(humanize, Mapping):
        unknown = set(humanize) - {"time_ms", "velocity", "seed"}
        if unknown:
            raise ValidationError(
                "Unknown humanize fields", details={"fields": sorted(unknown)})
        return _Humanize(
            time_ms=_number(humanize.get("time_ms", 0.0), "humanize.time_ms",
                            minimum=0.0),
            velocity=_integer(humanize.get("velocity", 0), "humanize.velocity",
                              minimum=0, maximum=127),
            seed=_integer(humanize.get("seed", 0), "humanize.seed"),
        )
    return _Humanize(time_ms=_number(humanize, "humanize", minimum=0.0))


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class GeneratedPhrase:
    item_guid: str
    from_bar: int
    to_bar: int
    subdivision: int
    pattern_length_qn: float
    range_start_s: float
    range_end_s: float
    take_offset_qn: float
    playrate: float
    notes: tuple[Note, ...]
    collisions_clipped: int
    humanize_time_ms: float
    humanize_velocity: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_guid": self.item_guid,
            "from_bar": self.from_bar,
            "to_bar": self.to_bar,
            "subdivision": self.subdivision,
            "pattern_length_qn": self.pattern_length_qn,
            "range": {"start_s": self.range_start_s, "end_s": self.range_end_s},
            "take_offset_qn": self.take_offset_qn,
            "playrate": self.playrate,
            "note_count": len(self.notes),
            "collisions_clipped": self.collisions_clipped,
            "humanize": {"time_ms": self.humanize_time_ms,
                         "velocity": self.humanize_velocity},
            "notes": [note.to_dict() for note in self.notes],
        }


def _coerce_item(item: MidiItem | Mapping[str, Any],
                 tempo: TempoMap | None = None) -> MidiItem:
    if isinstance(item, MidiItem):
        return item
    if isinstance(item, Mapping):
        if tempo is None:
            raise ValidationError("A raw item needs the project TempoMap")
        return MidiItem.from_context(item, tempo)
    raise ValidationError("item must be a MidiItem or an item context mapping")


def generate(item: MidiItem | Mapping[str, Any], pattern: Any, from_bar: int,
             to_bar: int, subdivision: int, accents: Any = None,
             humanize: Any = None, *, tempo: TempoMap | None = None) -> GeneratedPhrase:
    """Generate notes on the musical grid between two inclusive bars.

    ``pattern`` is caller data and nothing else.  A bare note list is one
    non-repeating statement over the requested bars; to repeat a motif, pass
    ``{"notes": [...], "length_beats": ...}`` (or ``bars``).  Note durations
    default to one subdivision step.  Notes of the same pitch that would
    overlap are clipped at the next onset; exact duplicate onsets are a data
    error.  ``accents`` and ``humanize`` default to ``None`` (no accent change,
    no humanisation).  ``subdivision`` has no default: it is a musical choice
    and must come from the caller.

    The requested bars must lie inside the item: generating outside the take
    is refused instead of writing notes nobody asked to hear.
    """
    item = _coerce_item(item, tempo)
    from_bar = _positive_int(from_bar, "from_bar")
    to_bar = _positive_int(to_bar, "to_bar")
    if to_bar < from_bar:
        raise ValidationError("to_bar must be >= from_bar")
    subdivision = _positive_int(subdivision, "subdivision")

    range_start_qn = item.tempo.bar_start_qn(from_bar)
    range_end_qn = item.tempo.bar_start_qn(to_bar + 1)
    range_start_s = item.tempo.qn_to_time(range_start_qn)
    range_end_s = item.tempo.qn_to_time(range_end_qn)
    if not item.contains(range_start_s, range_end_s):
        raise ValidationError(
            "The requested bars are not inside the item",
            details={"from_bar": from_bar, "to_bar": to_bar,
                     "requested_s": [range_start_s, range_end_s],
                     "item_s": [item.position_s, item.end_s]},
        )

    pattern_notes, length_qn = _parse_pattern(pattern, subdivision, item.tempo,
                                              from_bar, range_end_qn - range_start_qn)
    accent_map = _parse_accents(accents, subdivision)
    unknown_accents = sorted(set(accent_map) - {_accent_key(note.beat, subdivision)
                                                for note in pattern_notes})
    if unknown_accents:
        raise ValidationError(
            "Accents name beats the pattern does not contain",
            details={"subdivision_keys": unknown_accents,
                     "subdivision": subdivision},
        )
    human = _parse_humanize(humanize)
    step = 1.0 / subdivision
    rng = random.Random(human.seed) if human.active else None

    span_qn = range_end_qn - range_start_qn
    repetitions = int(math.floor((span_qn - _QN_EPS) / length_qn)) + 1
    generated: list[Note] = []
    if repetitions * len(pattern_notes) > _MAX_PHRASE_NOTES:
        raise ValidationError(
            "The requested phrase is too large",
            details={"generated_notes": repetitions * len(pattern_notes),
                     "limit": _MAX_PHRASE_NOTES},
        )

    for repetition in range(repetitions):
        origin_qn = range_start_qn + repetition * length_qn
        if origin_qn >= range_end_qn - _QN_EPS:
            break
        for spec in pattern_notes:
            start_qn = origin_qn + spec.beat
            if start_qn >= range_end_qn - _QN_EPS:
                continue
            duration = spec.duration if spec.duration is not None else step
            end_qn = min(start_qn + duration, range_end_qn)
            if end_qn <= start_qn + _QN_EPS:
                continue

            velocity = spec.velocity
            accent = accent_map.get(_accent_key(spec.beat, subdivision))
            if accent is not None:
                velocity = (accent.absolute if accent.absolute is not None
                            else velocity + int(accent.delta or 0))
            shift_s = 0.0
            if rng is not None and human.time_ms > 0:
                shift_s = rng.uniform(-human.time_ms, human.time_ms) / 1000.0
            if rng is not None and human.velocity > 0:
                velocity += rng.randint(-human.velocity, human.velocity)
            velocity = max(1, min(127, velocity))

            start_s = item.tempo.qn_to_time(start_qn) + shift_s
            end_s = item.tempo.qn_to_time(end_qn) + shift_s
            # Humanisation must not push a note outside the take: clamp at the
            # item edges instead of producing an unapplicable phrase.
            start_s = max(item.position_s, min(item.end_s, start_s))
            end_s = max(item.position_s, min(item.end_s, end_s))
            if end_s <= start_s + _TIME_EPS:
                continue
            source_start_qn = item.source_qn(item.tempo.time_to_qn(start_s))
            source_end_qn = item.source_qn(item.tempo.time_to_qn(end_s))
            if source_end_qn <= source_start_qn:
                source_end_qn = source_start_qn + _QN_EPS
            bar, beat = item.tempo.qn_to_bar_beat(start_qn)
            generated.append(Note(
                pitch=spec.pitch, velocity=velocity,
                start_qn=source_start_qn, end_qn=source_end_qn,
                start_s=start_s, end_s=end_s, bar=bar, beat=beat,
                channel=spec.channel, muted=spec.muted,
            ))

    generated.sort(key=lambda note: (note.pitch, note.start_s, note.start_qn))
    clipped = 0
    resolved: list[Note] = []
    for note in generated:
        if not resolved or resolved[-1].pitch != note.pitch:
            resolved.append(note)
            continue
        previous = resolved[-1]
        if abs(note.start_s - previous.start_s) <= _TIME_EPS:
            raise ValidationError(
                "The pattern produces two notes of the same pitch at one position",
                details={"pitch": note.pitch, "start_qn": note.start_qn,
                         "start_s": note.start_s},
            )
        if note.start_s < previous.end_s - _TIME_EPS:
            end_s = note.start_s
            end_qn = max(previous.start_qn + _QN_EPS,
                         item.source_qn(item.tempo.time_to_qn(end_s)))
            resolved[-1] = dc_replace(previous, end_qn=end_qn, end_s=end_s)
            clipped += 1
        resolved.append(note)
    resolved.sort(key=lambda note: (note.start_s, note.pitch))

    return GeneratedPhrase(
        item_guid=item.guid, from_bar=from_bar, to_bar=to_bar,
        subdivision=subdivision, pattern_length_qn=length_qn,
        range_start_s=range_start_s, range_end_s=range_end_s,
        take_offset_qn=item.take_offset_qn, playrate=item.playrate,
        notes=tuple(resolved), collisions_clipped=clipped,
        humanize_time_ms=human.time_ms, humanize_velocity=human.velocity,
    )


# ---------------------------------------------------------------------------
# grid check
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class GridReport:
    """Measured distances from the grid; classification is the caller's job."""

    item_guid: str
    tolerance_ms: float
    subdivision: int | None
    note_count: int
    notes_within_tolerance: int
    notes_beyond_tolerance: int
    max_abs_deviation_ms: float
    deviations: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_guid": self.item_guid,
            "tolerance_ms": self.tolerance_ms,
            "subdivision": self.subdivision,
            "note_count": self.note_count,
            "notes_within_tolerance": self.notes_within_tolerance,
            "notes_beyond_tolerance": self.notes_beyond_tolerance,
            "max_abs_deviation_ms": self.max_abs_deviation_ms,
            "deviations": list(self.deviations),
        }


def grid_check(item: MidiItem | Mapping[str, Any], tolerance_ms: float = 0.0,
               subdivision: int | None = None, *,
               tempo: TempoMap | None = None) -> GridReport:
    """Measure every note's distance from the bar-anchored QN grid.

    No pass/fail verdict is returned.  Each deviation is in project
    milliseconds against the tempo map, not against an averaged BPM, so a
    tempo change inside the fragment cannot hide drift.  ``subdivision=None``
    means the quarter-note grid; ``tolerance_ms`` defaults to 0 and is merely
    echoed in the numeric counts, which the caller interprets.
    """
    item = _coerce_item(item, tempo)
    tolerance_ms = _number(tolerance_ms, "tolerance_ms", minimum=0.0)
    if subdivision is not None:
        subdivision = _positive_int(subdivision, "subdivision")
    grid_division = subdivision if subdivision is not None else 1
    deviations: list[dict[str, Any]] = []
    beyond = 0
    maximum = 0.0
    for index, note in enumerate(item.notes):
        project_qn = item.tempo.time_to_qn(note.start_s)
        grid_qn = item.tempo.nearest_grid_qn(project_qn, grid_division)
        deviation_ms = (note.start_s - item.tempo.qn_to_time(grid_qn)) * 1000.0
        absolute = abs(deviation_ms)
        maximum = max(maximum, absolute)
        if absolute > tolerance_ms + 1e-9:
            beyond += 1
        grid_bar, grid_beat = item.tempo.qn_to_bar_beat(grid_qn)
        deviations.append({
            "index": index,
            "pitch": note.pitch,
            "time_s": note.start_s,
            "project_qn": project_qn,
            "bar": note.bar,
            "beat": note.beat,
            "grid_qn": grid_qn,
            "grid_bar": grid_bar,
            "grid_beat": grid_beat,
            "deviation_ms": deviation_ms,
            "abs_deviation_ms": absolute,
        })

    return GridReport(
        item_guid=item.guid, tolerance_ms=tolerance_ms,
        subdivision=subdivision, note_count=len(item.notes),
        notes_within_tolerance=len(item.notes) - beyond,
        notes_beyond_tolerance=beyond,
        max_abs_deviation_ms=maximum,
        deviations=tuple(deviations),
    )


# ---------------------------------------------------------------------------
# replace validation
# ---------------------------------------------------------------------------

_ALLOWED_NOTE_KEYS = {
    "start_s", "end_s", "time_s", "start_qn", "end_qn",
    "pitch", "velocity", "channel", "muted", "bar", "beat",
}


def _replace_payload(item: MidiItem, notes: Sequence[Any]) -> list[dict[str, Any]]:
    if not isinstance(notes, Sequence) or isinstance(notes, (str, bytes)):
        raise ValidationError("notes must be a sequence")
    payload: list[dict[str, Any]] = []
    low, high = item.source_window_s()
    for index, raw in enumerate(notes):
        if isinstance(raw, Note):
            note = raw
        elif isinstance(raw, Mapping):
            unknown = set(raw) - _ALLOWED_NOTE_KEYS
            if unknown:
                raise ValidationError(
                    "Unknown MIDI note field",
                    details={"note_index": index, "fields": sorted(unknown)})
            start_s = raw.get("start_s", raw.get("time_s"))
            end_s = raw.get("end_s")
            if start_s is None and raw.get("start_qn") is not None:
                start_s = item.tempo.qn_to_time(item.project_qn(
                    _number(raw.get("start_qn"), f"notes[{index}].start_qn")))
            if end_s is None and raw.get("end_qn") is not None:
                end_s = item.tempo.qn_to_time(item.project_qn(
                    _number(raw.get("end_qn"), f"notes[{index}].end_qn")))
            if start_s is None or end_s is None:
                raise ValidationError(
                    "Every note needs start_s/time_s and end_s",
                    details={"note_index": index})
            start_s = _number(start_s, f"notes[{index}].start_s")
            end_s = _number(end_s, f"notes[{index}].end_s")
            if end_s <= start_s:
                raise ValidationError("A note must end after it starts",
                                      details={"note_index": index})
            project_qn = item.tempo.time_to_qn(start_s)
            bar, beat = item.tempo.qn_to_bar_beat(project_qn)
            note = Note(
                pitch=_integer(raw.get("pitch"), f"notes[{index}].pitch",
                               minimum=0, maximum=127),
                velocity=_integer(raw.get("velocity"), f"notes[{index}].velocity",
                                  minimum=1, maximum=127),
                start_qn=item.source_qn(project_qn),
                end_qn=item.source_qn(item.tempo.time_to_qn(end_s)),
                start_s=start_s, end_s=end_s, bar=bar, beat=beat,
                channel=_integer(raw.get("channel", 0), f"notes[{index}].channel",
                                 minimum=0, maximum=15),
                muted=bool(raw.get("muted", False)),
            )
        else:
            raise ValidationError("Each note must be a Note or an object",
                                  details={"note_index": index})

        if not (note.start_s >= low - _TIME_EPS
                and note.end_s <= high + _TIME_EPS):
            raise ValidationError(
                "A note lies outside the item's take",
                details={"note_index": index,
                         "note_s": [note.start_s, note.end_s],
                         "item_s": [item.position_s, item.end_s],
                         "take_window_s": [low, high]},
            )
        payload_note = {"time_s": note.start_s, "end_s": note.end_s,
                        "pitch": note.pitch, "velocity": note.velocity}
        if note.channel:
            payload_note["channel"] = note.channel
        if note.muted:
            payload_note["muted"] = True
        payload.append(payload_note)

    ordered = sorted(range(len(payload)),
                     key=lambda position: (payload[position]["pitch"],
                                           payload[position]["time_s"]))
    for previous_index, index in zip(ordered, ordered[1:]):
        previous, current = payload[previous_index], payload[index]
        if previous["pitch"] != current["pitch"]:
            continue
        if current["time_s"] < previous["end_s"] - _TIME_EPS:
            if abs(current["time_s"] - previous["time_s"]) <= _TIME_EPS:
                reason = "duplicate_onset"
            else:
                reason = "overlapping_same_pitch"
            raise ValidationError(
                "MIDI notes of one pitch collide",
                details={"reason": reason, "pitch": current["pitch"],
                         "first_time_s": previous["time_s"],
                         "second_time_s": current["time_s"],
                         "first_end_s": previous["end_s"]},
            )
    return payload


def replace_notes(item: MidiItem, notes: Sequence[Any]) -> list[dict[str, Any]]:
    """Validate absolute-project-second notes for a replace, or raise."""
    return _replace_payload(item, notes)


# ---------------------------------------------------------------------------
# statistics (the pre-existing per-pitch rows, unchanged)
# ---------------------------------------------------------------------------

def item_statistics(item: MidiItem, *, track_name: str | None = None) -> dict[str, Any] | None:
    """Per-pitch counts and velocity ranges; ``None`` for a note-less item."""
    if not item.notes:
        return None
    by_pitch: dict[int, list[int]] = {}
    for note in item.notes:
        by_pitch.setdefault(note.pitch, []).append(note.velocity)
    return {
        "track_name": track_name,
        "item_guid": item.guid,
        "note_count": len(item.notes),
        "pitches": [
            {
                "pitch": pitch,
                "notes": len(values),
                "velocity_min": min(values),
                "velocity_mean": round(sum(values) / len(values), 1),
                "velocity_max": max(values),
            }
            for pitch, values in sorted(by_pitch.items())
        ],
    }


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------

class FragmentRenderer(Protocol):
    """Injectable fragment render used by ``preview`` before any apply.

    ``proposed_notes`` is ``None`` for a render of the take as it is, or the
    validated absolute-second notes of a candidate phrase.  A renderer that
    supports auditioning a change stages those notes in a scratch take/project
    and renders exactly ``[start_s, end_s)``; the service never applies them
    itself before a preview.
    """

    def __call__(self, *, item_guid: str, start_s: float, end_s: float,
                 bar_range: Mapping[str, int],
                 proposed_notes: Sequence[Mapping[str, Any]] | None) -> Any: ...


class MidiService:
    """Bridge-facing MIDI operations around the pure musical-grid core.

    ``session`` is any object with ``snapshot(include_items=...,
    include_midi_notes=...)`` and a ``bridge.call(op, payload)`` (the
    ``ProjectSession`` used elsewhere in this package).  ``renderer`` is
    injected: preview must not invent a render path, and tests use a fake.
    """

    def __init__(self, session: Any, *, renderer: FragmentRenderer | None = None):
        self.session = session
        self.renderer = renderer

    # -- reads --------------------------------------------------------------

    def _context(self) -> dict[str, Any]:
        context = self.session.snapshot(include_items=True, include_midi_notes=True)
        if not isinstance(context, Mapping):
            raise BridgeUnavailable("REAPER returned an invalid project context")
        return dict(context)

    def _find_item(self, item_guid: str) -> tuple[dict[str, Any], dict[str, Any], MidiItem]:
        if not isinstance(item_guid, str) or not item_guid:
            raise ValidationError("item_guid must be a non-empty REAPER item GUID")
        context = self._context()
        tempo = TempoMap.from_context(context.get("tempo"))
        for track in context.get("tracks") or []:
            for item in track.get("items") or []:
                if item.get("guid") == item_guid:
                    return context, track, MidiItem.from_context(item, tempo)
        raise ValidationError("The item is not in the active project",
                              details={"item_guid": item_guid})

    def statistics(self) -> dict[str, Any]:
        """Per-pitch statistics over every MIDI item, in the old response shape."""
        context = self._context()
        tempo = TempoMap.from_context(context.get("tempo"))
        rows: list[dict[str, Any]] = []
        for track in context.get("tracks") or []:
            for item in track.get("items") or []:
                if not isinstance(item, Mapping):
                    continue
                take = item.get("active_take") or {}
                if isinstance(take, Mapping) and take.get("is_midi") is False:
                    continue
                midi_item = MidiItem.from_context(item, tempo)
                row = item_statistics(midi_item, track_name=track.get("name"))
                if row is not None:
                    rows.append(row)
        return {"ok": True, "action": "notes", "items": rows}

    def grid_check(self, item_guid: str, tolerance_ms: float = 0.0,
                   subdivision: int | None = None) -> dict[str, Any]:
        _, track, item = self._find_item(item_guid)
        report = grid_check(item, tolerance_ms, subdivision)
        return {"action": "grid_check", "track_name": track.get("name"),
                **report.to_dict()}

    # -- writes -------------------------------------------------------------

    def generate(self, item_guid: str, pattern: Any, from_bar: int, to_bar: int,
                 subdivision: int, accents: Any = None, humanize: Any = None,
                 *, apply: bool = False) -> dict[str, Any]:
        """Generate a phrase; only ``apply=True`` writes it to the take.

        Applying keeps every existing note outside the requested bars, so a
        phrase is added to a part instead of silently replacing the rest of it.
        """
        _, track, item = self._find_item(item_guid)
        phrase = generate(item, pattern, from_bar, to_bar, subdivision,
                          accents, humanize)
        result: dict[str, Any] = {"ok": True, "action": "generate",
                                  "track_name": track.get("name"),
                                  **phrase.to_dict()}
        if not apply:
            return result

        kept = [note for note in item.notes
                if note.start_s < phrase.range_start_s - _TIME_EPS
                or note.start_s >= phrase.range_end_s - _TIME_EPS]
        merged = sorted(kept + list(phrase.notes),
                        key=lambda note: (note.start_s, note.pitch))
        applied = self._call_replace(item, merged)
        result["applied"] = applied
        result["kept_existing"] = len(kept)
        result["merged_note_count"] = len(merged)
        return result

    def replace(self, item_guid: str, notes: Sequence[Any]) -> dict[str, Any]:
        """Replace every note in the take with validated absolute-second notes."""
        _, track, item = self._find_item(item_guid)
        result = {"ok": True, "action": "replace",
                  "track_name": track.get("name"), "item_guid": item.guid}
        result.update(self._call_replace(item, notes))
        return result

    def _call_replace(self, item: MidiItem, notes: Sequence[Any]) -> dict[str, Any]:
        payload = _replace_payload(item, notes)
        response = self.session.bridge.call(
            "midi.replace", {"item_guid": item.guid, "notes": payload})
        if not isinstance(response, Mapping):
            raise BridgeUnavailable("REAPER returned an invalid midi.replace result")
        inserted = response.get("inserted")
        removed = response.get("removed")
        if (isinstance(inserted, bool) or not isinstance(inserted, int) or
                isinstance(removed, bool) or not isinstance(removed, int) or
                inserted < 0 or removed < 0):
            raise BridgeUnavailable(
                "midi.replace did not report inserted/removed counts",
                details={"response": dict(response)},
            )
        if inserted != len(payload):
            raise BridgeUnavailable(
                "midi.replace inserted a different number of notes than requested",
                details={"requested": len(payload), "inserted": inserted,
                         "removed": removed},
            )
        return {"inserted": inserted, "removed": removed,
                "note_count": len(payload)}

    # -- preview ------------------------------------------------------------

    def preview(self, item_guid: str, bars: Any,
                notes: Sequence[Any] | None = None) -> dict[str, Any]:
        """Render a bar fragment through the injected renderer, before apply.

        ``bars`` is either a count from the item's first bar or an inclusive
        ``[from_bar, to_bar]`` pair.  The rendered range is clipped to the
        item and named in the answer together with the renderer's result.
        Pass ``notes`` (for example ``generate(...).notes``) to audition a
        candidate phrase instead of the take as it currently sounds.
        """
        if self.renderer is None:
            raise ValidationError("preview requires an injected renderer")
        _, track, item = self._find_item(item_guid)
        proposed = None if notes is None else _replace_payload(item, notes)
        from_bar, to_bar = self._preview_bars(item, bars)
        start_s = max(item.position_s, item.tempo.qn_to_time(item.tempo.bar_start_qn(from_bar)))
        end_s = min(item.end_s, item.tempo.qn_to_time(item.tempo.bar_start_qn(to_bar + 1)))
        if end_s <= start_s + _TIME_EPS:
            raise ValidationError(
                "The preview bars do not overlap the item",
                details={"from_bar": from_bar, "to_bar": to_bar,
                         "item_s": [item.position_s, item.end_s]},
            )
        rendered = self.renderer(item_guid=item.guid, start_s=start_s,
                                 end_s=end_s,
                                 bar_range={"from_bar": from_bar, "to_bar": to_bar},
                                 proposed_notes=proposed)
        return {
            "ok": True,
            "action": "preview",
            "track_name": track.get("name"),
            "item_guid": item.guid,
            "from_bar": from_bar,
            "to_bar": to_bar,
            "start_s": start_s,
            "end_s": end_s,
            "duration_s": end_s - start_s,
            "proposed_note_count": 0 if proposed is None else len(proposed),
            "clipped": start_s > item.tempo.qn_to_time(
                item.tempo.bar_start_qn(from_bar)) + _TIME_EPS
                or end_s < item.tempo.qn_to_time(
                    item.tempo.bar_start_qn(to_bar + 1)) - _TIME_EPS,
            "render": rendered,
        }

    @staticmethod
    def _preview_bars(item: MidiItem, bars: Any) -> tuple[int, int]:
        if isinstance(bars, int) and not isinstance(bars, bool):
            count = _positive_int(bars, "bars")
            first, _ = item.tempo.qn_to_bar_beat(item.position_qn)
            return first, first + count - 1
        if (isinstance(bars, Sequence) and not isinstance(bars, (str, bytes))
                and len(bars) == 2):
            from_bar = _positive_int(bars[0], "bars[0]")
            to_bar = _positive_int(bars[1], "bars[1]")
            if to_bar < from_bar:
                raise ValidationError("preview bars must be [from_bar, to_bar]")
            return from_bar, to_bar
        raise ValidationError("bars must be a count or an inclusive [from, to] pair")
