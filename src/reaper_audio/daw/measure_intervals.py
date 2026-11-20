"""Caller-declared measurement intervals.

An interval is never guessed here: it is either supplied by the caller, taken
from the regions/markers the project already carries, or derived from the
project's own tempo and time-signature map (``bars:<n>``).  The module knows
nothing about sections, choruses or "good" windows.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, isfinite
from typing import Any, Mapping, Sequence

from ..errors import ValidationError

#: Tolerance used only for float comparisons of caller-supplied edges.
_EPS = 1e-9

BARS_PREFIX = "bars:"


@dataclass(frozen=True, slots=True)
class MeasureInterval:
    """One measurement window on the project timeline."""

    start_s: float
    end_s: float
    label: str = ""
    origin: str = "explicit"

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    def to_dict(self, *, index: int | None = None) -> dict[str, Any]:
        row: dict[str, Any] = {
            "start_s": round(self.start_s, 6),
            "end_s": round(self.end_s, 6),
            "duration_s": round(self.duration_s, 6),
            "label": self.label,
            "origin": self.origin,
        }
        if index is not None:
            row = {"index": index, **row}
        return row


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a number", details={"field": field})
    number = float(value)
    if not isfinite(number):
        raise ValidationError(f"{field} must be finite", details={"field": field})
    return number


def _interval(start: Any, end: Any, *, project_length_s: float,
              label: str, origin: str) -> MeasureInterval:
    start_s = _number(start, f"{origin}.start_s")
    end_s = _number(end, f"{origin}.end_s")
    if end_s <= start_s:
        raise ValidationError("Interval end_s must be greater than start_s",
                              details={"start_s": start_s, "end_s": end_s,
                                       "origin": origin})
    if start_s < -_EPS:
        raise ValidationError("Interval start_s must not be negative",
                              details={"start_s": start_s, "origin": origin})
    start_s = max(0.0, start_s)
    if project_length_s > 0 and end_s > project_length_s + _EPS:
        raise ValidationError(
            "Interval extends beyond the project length",
            details={"start_s": start_s, "end_s": end_s,
                     "project_length_s": project_length_s, "origin": origin,
                     "next_action": ("measure a window inside the project, or "
                                     "measure an external file source instead")})
    if project_length_s > 0:
        end_s = min(end_s, project_length_s)
    return MeasureInterval(start_s=round(start_s, 6), end_s=round(end_s, 6),
                           label=label, origin=origin)


# --------------------------------------------------------------------- #
# Regions and markers
# --------------------------------------------------------------------- #
def _region_rows(regions: Any) -> list[MeasureInterval]:
    rows: list[MeasureInterval] = []
    for index, region in enumerate(regions or []):
        if not isinstance(region, Mapping):
            continue
        start = region.get("position_s", region.get("start_s"))
        end = region.get("end_s")
        if start is None or end is None:
            continue
        try:
            start_s, end_s = float(start), float(end)
        except (TypeError, ValueError):
            continue
        if not (isfinite(start_s) and isfinite(end_s)) or end_s <= start_s:
            continue
        name = str(region.get("name") or "").strip()
        rows.append(MeasureInterval(start_s=start_s, end_s=end_s,
                                    label=name or f"region {index + 1}",
                                    origin="region"))
    return rows


def _marker_span_rows(markers: Any, *, project_length_s: float) -> list[MeasureInterval]:
    positions: list[float] = []
    for marker in markers or []:
        if not isinstance(marker, Mapping):
            continue
        raw = marker.get("position_s")
        try:
            position = float(raw)
        except (TypeError, ValueError):
            continue
        if isfinite(position) and 0.0 <= position < project_length_s:
            positions.append(position)
    positions = sorted(set(round(value, 6) for value in positions))
    rows: list[MeasureInterval] = []
    for index, start in enumerate(positions):
        end = positions[index + 1] if index + 1 < len(positions) else project_length_s
        if end - start <= _EPS:
            continue
        rows.append(MeasureInterval(start_s=start, end_s=end,
                                    label=f"marker {index + 1}", origin="marker_span"))
    return rows


def region_intervals(regions: Any, markers: Any, *,
                     project_length_s: float) -> list[MeasureInterval]:
    """Regions as intervals; markers become spans only when no region exists."""
    rows = _region_rows(regions)
    if rows:
        return rows
    rows = _marker_span_rows(markers, project_length_s=project_length_s)
    if rows:
        return rows
    raise ValidationError(
        "The project has no regions or markers to use as intervals",
        details={"regions": 0, "markers": 0,
                 "next_action": ("add regions/markers in REAPER, pass explicit "
                                 "[[start_s, end_s], ...] intervals, or use "
                                 "intervals='bars:<n>'")})


# --------------------------------------------------------------------- #
# Bar grid from the project tempo map
# --------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _TempoSegment:
    t0: float
    t1: float
    bpm0: float
    bpm1: float
    numerator: int
    denominator: int
    linear: bool

    @property
    def bar_quarters(self) -> float:
        return self.numerator * 4.0 / self.denominator

    def quarters_between(self, t0: float, t1: float) -> float:
        """Quarter notes elapsed between two absolute times inside the segment."""
        t0 = min(max(t0, self.t0), self.t1)
        t1 = min(max(t1, self.t0), self.t1)
        if t1 <= t0:
            return 0.0
        if not self.linear or abs(self.bpm1 - self.bpm0) < _EPS:
            return (t1 - t0) * self.bpm0 / 60.0
        span = self.t1 - self.t0
        slope = (self.bpm1 - self.bpm0) / span
        u0, u1 = t0 - self.t0, t1 - self.t0
        return (self.bpm0 * (u1 - u0) + 0.5 * slope * (u1 * u1 - u0 * u0)) / 60.0

    def time_for_quarters(self, t0: float, quarters: float) -> float:
        """Time at which ``quarters`` more quarter notes elapse from ``t0``."""
        t0 = min(max(t0, self.t0), self.t1)
        if quarters <= 0:
            return t0
        if not self.linear or abs(self.bpm1 - self.bpm0) < _EPS:
            return min(self.t1, t0 + quarters * 60.0 / self.bpm0)
        span = self.t1 - self.t0
        slope = (self.bpm1 - self.bpm0) / span
        u0 = t0 - self.t0
        # bpm0*(u-u0) + slope/2*(u^2-u0^2) = 60*quarters
        c = 0.5 * slope * u0 * u0 + self.bpm0 * u0 + 60.0 * quarters
        a = 0.5 * slope
        b = self.bpm0
        discriminant = b * b - 4.0 * a * (-c)
        if discriminant < 0:
            return self.t1
        root = (-b + discriminant ** 0.5) / (2.0 * a)
        if not isfinite(root):
            return self.t1
        return min(self.t1, max(t0, self.t0 + root))


def _tempo_points(tempo: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    tempo = tempo or {}
    try:
        base_bpm = float(tempo.get("bpm") or 0.0)
        base_num = int(tempo.get("numerator") or 0)
        base_den = int(tempo.get("denominator") or 0)
    except (TypeError, ValueError) as exc:
        raise ValidationError("The project tempo map is unreadable",
                              details={"tempo": dict(tempo)}) from exc
    if not isfinite(base_bpm) or base_bpm <= 0:
        raise ValidationError("The project tempo map has no base tempo",
                              details={"bpm": tempo.get("bpm")})
    if base_num <= 0 or base_den <= 0:
        raise ValidationError("The project time signature is unreadable",
                              details={"numerator": tempo.get("numerator"),
                                       "denominator": tempo.get("denominator")})
    points: list[dict[str, Any]] = [{"t": 0.0, "bpm": base_bpm, "num": base_num,
                                     "den": base_den, "linear": False}]
    for raw in tempo.get("markers") or []:
        if not isinstance(raw, Mapping):
            continue
        try:
            at = float(raw.get("time_s"))
        except (TypeError, ValueError):
            continue
        if not isfinite(at):
            continue
        try:
            bpm = float(raw.get("bpm") or 0.0)
        except (TypeError, ValueError):
            bpm = 0.0
        try:
            num = int(raw.get("numerator") or 0)
            den = int(raw.get("denominator") or 0)
        except (TypeError, ValueError):
            num = den = 0
        linear = bool(raw.get("linear"))
        previous = points[-1]
        resolved = {
            "t": max(0.0, at),
            "bpm": bpm if isfinite(bpm) and bpm > 0 else previous["bpm"],
            "num": num if num > 0 else previous["num"],
            "den": den if den > 0 else previous["den"],
            "linear": linear,
        }
        if resolved["t"] <= _EPS:
            points[0] = {**resolved, "t": 0.0}
        elif abs(resolved["t"] - previous["t"]) <= _EPS:
            points[-1] = resolved
        else:
            points.append(resolved)
    return points


def tempo_segments(tempo: Mapping[str, Any] | None, *,
                   project_length_s: float) -> list[_TempoSegment]:
    """Piecewise tempo/time-signature map covering the whole project."""
    points = _tempo_points(tempo)
    segments: list[_TempoSegment] = []
    for index, point in enumerate(points):
        following = points[index + 1] if index + 1 < len(points) else None
        t0 = float(point["t"])
        t1 = float(following["t"]) if following is not None else max(project_length_s, t0)
        if t1 <= t0 + _EPS:
            continue
        linear = bool(point["linear"]) and following is not None
        bpm1 = float(following["bpm"]) if (linear and following is not None) \
            else float(point["bpm"])
        segments.append(_TempoSegment(
            t0=t0, t1=t1,
            bpm0=float(point["bpm"]), bpm1=bpm1,
            numerator=int(point["num"]), denominator=int(point["den"]),
            linear=linear))
    if not segments:
        raise ValidationError("The project tempo map produced no segments")
    # The last segment always reaches the project length so the walk terminates.
    last = segments[-1]
    if last.t1 < project_length_s:
        segments.append(_TempoSegment(
            t0=last.t1, t1=project_length_s, bpm0=last.bpm1, bpm1=last.bpm1,
            numerator=last.numerator, denominator=last.denominator, linear=False))
    return segments


def bar_boundaries(tempo: Mapping[str, Any] | None, *,
                   project_length_s: float) -> list[float]:
    """Times of every bar line from 0 to the project length."""
    if project_length_s <= 0:
        raise ValidationError("The project length is unknown; a bar grid needs it")
    segments = tempo_segments(tempo, project_length_s=project_length_s)
    boundaries: list[float] = [0.0]
    t = 0.0
    segment_index = 0
    bar_quarters = segments[0].bar_quarters
    quarters_left = bar_quarters
    max_bpm = max(max(segment.bpm0, segment.bpm1) for segment in segments)
    min_bar = min(segment.bar_quarters for segment in segments)
    # Data-derived loop bound: even at the fastest tempo the project cannot hold
    # more bars than this.
    guard = int(ceil(project_length_s * max_bpm / 60.0 / min_bar)) + 8
    for _ in range(guard):
        if t >= project_length_s - _EPS:
            break
        while segment_index < len(segments) - 1 and segments[segment_index].t1 <= t + _EPS:
            segment_index += 1
            bar_quarters = segments[segment_index].bar_quarters
        segment = segments[segment_index]
        available = segment.quarters_between(t, segment.t1)
        if available >= quarters_left - _EPS:
            t = segment.time_for_quarters(t, quarters_left)
            boundaries.append(round(t, 6))
            quarters_left = bar_quarters
        else:
            quarters_left -= available
            t = segment.t1
            segment_index = min(segment_index + 1, len(segments) - 1)
    if boundaries[-1] < project_length_s - _EPS:
        boundaries.append(project_length_s)
    return boundaries


def bar_intervals(tempo: Mapping[str, Any] | None, *, project_length_s: float,
                  bars_per_cell: int) -> list[MeasureInterval]:
    """Cells of ``bars_per_cell`` consecutive bars, anchored at bar 1."""
    boundaries = bar_boundaries(tempo, project_length_s=project_length_s)
    if len(boundaries) < 2:
        raise ValidationError("The project is shorter than one bar",
                              details={"project_length_s": project_length_s})
    rows: list[MeasureInterval] = []
    first_bar = 0
    while first_bar < len(boundaries) - 1:
        last_bar = min(first_bar + bars_per_cell, len(boundaries) - 1)
        start = boundaries[first_bar]
        end = min(boundaries[last_bar], project_length_s)
        if end - start > _EPS:
            rows.append(MeasureInterval(
                start_s=round(start, 6), end_s=round(end, 6),
                label=f"bars {first_bar + 1}-{last_bar}", origin="bars"))
        first_bar = last_bar
    if not rows:
        raise ValidationError("The bar grid produced no intervals",
                              details={"project_length_s": project_length_s,
                                       "bars_per_cell": bars_per_cell})
    return rows


# --------------------------------------------------------------------- #
# Spec parsing
# --------------------------------------------------------------------- #
def parse_bars_spec(text: str) -> int:
    raw = text[len(BARS_PREFIX):].strip()
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError("intervals='bars:<n>' needs a positive integer bar count",
                              details={"intervals": text}) from exc
    if value <= 0:
        raise ValidationError("intervals='bars:<n>' needs a positive integer bar count",
                              details={"intervals": text, "bars": value})
    return value


def parse_intervals(spec: Any, *, project_length_s: float,
                    regions: Any = None, markers: Any = None,
                    tempo: Mapping[str, Any] | None = None) -> list[MeasureInterval]:
    """Turn the ``intervals`` parameter into concrete windows.

    Accepted forms: ``"all"``, ``"regions"``, ``"bars:<n>"``, a sequence of
    ``[start_s, end_s]`` pairs (optionally with a label), or a mapping with
    ``kind``.  ``None`` returns an empty list: the caller then uses its legacy
    single window.
    """
    if spec is None:
        return []
    if project_length_s <= 0:
        raise ValidationError("REAPER returned no project length; intervals need it")
    if isinstance(spec, str):
        text = spec.strip()
        lowered = text.lower()
        if lowered == "all":
            return [MeasureInterval(0.0, project_length_s, "all", "all")]
        if lowered == "regions":
            return region_intervals(regions, markers, project_length_s=project_length_s)
        if lowered.startswith(BARS_PREFIX):
            return bar_intervals(tempo, project_length_s=project_length_s,
                                 bars_per_cell=parse_bars_spec(text))
        raise ValidationError(
            "intervals must be 'all', 'regions', 'bars:<n>' or a list of [start_s, end_s]",
            details={"intervals": spec})
    if isinstance(spec, Mapping):
        kind = str(spec.get("kind") or "").strip().lower()
        if kind == "all":
            return [MeasureInterval(0.0, project_length_s, "all", "all")]
        if kind == "regions":
            return region_intervals(regions, markers, project_length_s=project_length_s)
        if kind == "bars":
            try:
                count = int(spec.get("bars") or spec.get("n") or 0)
            except (TypeError, ValueError) as exc:
                raise ValidationError("intervals={kind: bars} needs a positive 'bars'",
                                      details={"intervals": spec}) from exc
            if count <= 0:
                raise ValidationError("intervals={kind: bars} needs a positive 'bars'",
                                      details={"intervals": spec})
            return bar_intervals(tempo, project_length_s=project_length_s,
                                 bars_per_cell=count)
        if "start_s" in spec or "end_s" in spec:
            return [_interval(spec.get("start_s"), spec.get("end_s"),
                              project_length_s=project_length_s,
                              label=str(spec.get("label") or spec.get("name") or ""),
                              origin="explicit")]
        raise ValidationError("Interval mapping needs kind or start_s/end_s",
                              details={"intervals": spec})
    if isinstance(spec, Sequence) and not isinstance(spec, (str, bytes)):
        rows: list[MeasureInterval] = []
        for entry in spec:
            if isinstance(entry, Mapping):
                rows.append(_interval(entry.get("start_s"), entry.get("end_s"),
                                      project_length_s=project_length_s,
                                      label=str(entry.get("label")
                                                or entry.get("name") or ""),
                                      origin="explicit"))
            elif isinstance(entry, Sequence) and not isinstance(entry, (str, bytes)):
                if len(entry) != 2:
                    raise ValidationError("Every interval must be [start_s, end_s]",
                                          details={"interval": list(entry)})
                rows.append(_interval(entry[0], entry[1],
                                      project_length_s=project_length_s,
                                      label="", origin="explicit"))
            else:
                raise ValidationError("Every interval must be [start_s, end_s] or a mapping",
                                      details={"interval": entry})
        if not rows:
            raise ValidationError("intervals must not be empty")
        return rows
    raise ValidationError("intervals must be 'all', 'regions', 'bars:<n>' or a list",
                          details={"intervals": spec})


def union_span(intervals: Sequence[MeasureInterval]) -> tuple[float, float]:
    """One render span that covers every requested interval."""
    if not intervals:
        raise ValidationError("No intervals to measure")
    start = min(row.start_s for row in intervals)
    end = max(row.end_s for row in intervals)
    return start, end


def overlapping_pairs(intervals: Sequence[MeasureInterval]) -> list[dict[str, Any]]:
    """Pairs of intervals that share audio, in declaration order.

    A fact, not a verdict: the caller decides whether shared audio between two
    requested windows is intended.
    """
    pairs: list[dict[str, Any]] = []
    for left in range(len(intervals)):
        for right in range(left + 1, len(intervals)):
            first, second = intervals[left], intervals[right]
            shared = min(first.end_s, second.end_s) - max(first.start_s, second.start_s)
            if shared > _EPS:
                pairs.append({
                    "a": left, "b": right,
                    "a_interval": {"start_s": round(first.start_s, 6),
                                   "end_s": round(first.end_s, 6)},
                    "b_interval": {"start_s": round(second.start_s, 6),
                                   "end_s": round(second.end_s, 6)},
                    "shared_audio_s": round(shared, 6),
                })
    return pairs


__all__ = [
    "BARS_PREFIX",
    "MeasureInterval",
    "bar_boundaries",
    "bar_intervals",
    "overlapping_pairs",
    "parse_bars_spec",
    "parse_intervals",
    "region_intervals",
    "tempo_segments",
    "union_span",
]
