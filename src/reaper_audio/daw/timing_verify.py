"""Verify a timing plan against the signal it will actually produce.

An ``apply`` receipt only proves that clips were written.  It does not prove
that a planned attack moved, that the cut did not add a click, or that the
planner asked for source material the file does not contain.  This module
answers those questions from the block geometry alone, using an in-memory
mono signal instead of REAPER.

The model
---------
The plan's blocks are exactly the clips the bridge writes: block *i* plays at
``position_s``, reads the source from ``source_start_s`` for ``length_s``
seconds, and applies a linear fade over the first ``fade_in_s`` and the last
``fade_out_s`` seconds.  :func:`render_plan` concatenates those slices on one
timeline, mixes overlapping blocks additively and leaves silence wherever no
block covers the timeline.  The buffer starts at the plan's timeline origin
(``item_position_s``, or the earliest block if one starts earlier), so project
time ``t`` is buffer index ``round((t - origin) * sample_rate)``.

What is checked
---------------
* :func:`measure_attacks` follows every anchor and reports where its attack
  actually landed, using a simple 1 ms RMS energy-rise rule (documented on the
  function).  It also notices a second front close to the measured one and a
  front that sits on an unexpected block boundary.
* :func:`verify_plan` renders the plan once and returns the attack rows, one
  row per block boundary and a summary count.
* :func:`conflict_report` lists the geometry that cannot work: source read
  past the end of the file, a crossfade overlap longer than the earlier
  block's source tail, an attack buried under a fade, a block shorter than
  4 ms and uncovered holes in the item window.

Nothing here edits the project, reads files or talks to REAPER.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

import math

import numpy as np

from ..errors import ValidationError

#: RMS frame and hop used by the energy-rise detector, in seconds.
FRAME_S = 0.001
#: A second front this close to the measured one is a duplicate.
DUPLICATE_RADIUS_S = 0.040
#: A second front counts as a separate attack only across a valley this deep.
DIP_DB = 6.0
#: A front this close to a block boundary is a seam front.
SEAM_RADIUS_S = 0.003
#: How far around a block boundary the seam scan looks, in seconds.
SEAM_SCAN_S = 0.015
#: Blocks shorter than this cannot carry a usable fade, in seconds.
MIN_BLOCK_S = 0.004
#: Summed block gain below this counts as an attack sitting under a fade.
UNITY_GAIN_TOL = 1e-3
#: dB floor used by the seam level delta.
LEVEL_FLOOR = 1e-9


class _Block(NamedTuple):
    block_index: int
    position_s: float
    source_start_s: float
    length_s: float
    fade_in_s: float
    fade_out_s: float

    @property
    def end_s(self) -> float:
        return self.position_s + self.length_s

    @property
    def source_end_s(self) -> float:
        return self.source_start_s + self.length_s


# ---------------------------------------------------------------------------
# Inputs and block geometry
# ---------------------------------------------------------------------------


def _finite(value: Any, field: str, *, index: int | None = None) -> float:
    if isinstance(value, bool) or not isinstance(
            value, (int, float, np.integer, np.floating)):
        raise ValidationError(f"{field} must be a finite number",
                              details={"field": field, "block_index": index,
                                       "value": value})
    number = float(value)
    if not np.isfinite(number):
        raise ValidationError(f"{field} must be a finite number",
                              details={"field": field, "block_index": index,
                                       "value": value})
    return number


def _rate(sample_rate: Any) -> int:
    if isinstance(sample_rate, bool) or not isinstance(
            sample_rate, (int, float, np.integer, np.floating)):
        raise ValidationError("sample_rate must be a positive number")
    rate = int(round(float(sample_rate)))
    if rate <= 0:
        raise ValidationError("sample_rate must be a positive number")
    return rate


def _mono(source: Any) -> np.ndarray:
    samples = np.asarray(source, dtype=np.float64)
    if samples.ndim == 2:
        samples = samples.mean(axis=1)
    if samples.ndim != 1:
        raise ValidationError("source must be a mono sample array",
                              details={"shape": list(samples.shape)})
    return samples


def _block_rows(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if not isinstance(plan, Mapping):
        raise ValidationError("timing verification needs a plan mapping")
    rows = plan.get("blocks")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValidationError("timing plan needs a blocks list")
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValidationError("each timing block must be a mapping")
    return list(rows)


def _blocks(plan: Mapping[str, Any]) -> list[_Block]:
    blocks = []
    for index, row in enumerate(_block_rows(plan)):
        length = _finite(row.get("length_s"), "length_s", index=index)
        if length < 0.0:
            raise ValidationError("block length_s cannot be negative",
                                  details={"block_index": index, "length_s": length})
        blocks.append(_Block(
            block_index=index,
            position_s=_finite(row.get("position_s"), "position_s", index=index),
            source_start_s=_finite(row.get("source_start_s", 0.0),
                                   "source_start_s", index=index),
            length_s=length,
            fade_in_s=max(0.0, _finite(row.get("fade_in_s") or 0.0,
                                       "fade_in_s", index=index)),
            fade_out_s=max(0.0, _finite(row.get("fade_out_s") or 0.0,
                                        "fade_out_s", index=index)),
        ))
    return blocks


def _anchors(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = plan.get("anchors") or []
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValidationError("timing plan anchors must be a list")
    anchors = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValidationError("each timing anchor must be a mapping")
        anchors.append(row)
    return anchors


def _anchor_target(anchor: Mapping[str, Any]) -> float:
    for key in ("target_s", "project_s"):
        value = anchor.get(key)
        if value is not None:
            return _finite(value, key)
    raise ValidationError("anchor needs target_s or project_s",
                          details={"anchor": dict(anchor)})


def _timeline(plan: Mapping[str, Any], blocks: Sequence[_Block]) -> tuple[float, float]:
    """Return the project-time span the rendered buffer covers."""
    position = _finite(plan.get("item_position_s") or 0.0, "item_position_s")
    length = _finite(plan.get("item_length_s") or 0.0, "item_length_s")
    if length < 0.0:
        raise ValidationError("item_length_s cannot be negative")
    start, end = position, position + length
    for block in blocks:
        start = min(start, block.position_s)
        end = max(end, block.end_s)
    return start, end


def _item_window(plan: Mapping[str, Any], blocks: Sequence[_Block]) -> tuple[float, float] | None:
    position = _finite(plan.get("item_position_s") or 0.0, "item_position_s")
    length = _finite(plan.get("item_length_s") or 0.0, "item_length_s")
    if length > 0.0:
        return position, position + length
    if blocks:
        return (min(block.position_s for block in blocks),
                max(block.end_s for block in blocks))
    return None


def _seam_times(plan: Mapping[str, Any], blocks: Sequence[_Block]) -> list[float]:
    """Block boundaries as project times; plan seams win when they exist."""
    rows = plan.get("seams") or []
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValidationError("timing plan seams must be a list")
    values = []
    for row in rows:
        value = (row.get("position_s", row.get("start_s"))
                 if isinstance(row, Mapping) else row)
        if value is None:
            continue
        values.append(_finite(value, "seams"))
    if not values and len(blocks) > 1:
        values = [block.position_s for block in sorted(blocks, key=lambda b: b.position_s)[1:]]
    unique: list[float] = []
    for value in sorted(values):
        if not unique or abs(value - unique[-1]) > 1e-6:
            unique.append(value)
    return unique


def _gain_at(block: _Block, offset_s: float) -> float:
    """Linear item gain at *offset_s* into the block (fades multiply)."""
    gain = 1.0
    if block.fade_in_s > 0.0 and offset_s < block.fade_in_s:
        gain *= max(0.0, offset_s / block.fade_in_s)
    tail = block.length_s - offset_s
    if block.fade_out_s > 0.0 and tail < block.fade_out_s:
        gain *= max(0.0, min(1.0, tail / block.fade_out_s))
    return max(0.0, gain)


def _fade_curve(count: int, fade_in_s: float, fade_out_s: float, rate: int) -> np.ndarray:
    """Linear fade curve, identical to the dry timing preview."""
    gain = np.ones(count, dtype=np.float64)
    head = min(count, max(0, int(round(fade_in_s * rate))))
    tail = min(count, max(0, int(round(fade_out_s * rate))))
    if head:
        gain[:head] *= np.arange(head) / head
    if tail:
        gain[-tail:] *= 1.0 - np.arange(tail) / tail
    return gain


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_plan(plan: Mapping[str, Any], source: Any, *,
                sample_rate: int) -> tuple[np.ndarray, int]:
    """Render the plan's blocks as the buffer REAPER would play.

    Every block is placed at ``round((position_s - origin) * rate)`` and reads
    ``round(source_start_s * rate)`` for ``round(length_s * rate)`` samples.
    Reads past either end of the source become silence (they cannot be
    invented); :func:`conflict_report` names those blocks.  Linear fades use
    the same curve as the dry preview: a fade-in starts at zero and reaches
    unity one sample after ``fade_in_s``; overlapping blocks are summed.

    Returns ``(buffer, sample_rate)``; buffer index 0 is the plan timeline
    origin, normally ``item_position_s``.
    """
    rate = _rate(sample_rate)
    samples = _mono(source)
    blocks = _blocks(plan)
    start_s, end_s = _timeline(plan, blocks)
    length = max(0, int(round((end_s - start_s) * rate)))
    buffer = np.zeros(length, dtype=np.float64)
    for block in blocks:
        if block.length_s <= 0.0:
            continue
        count = int(round(block.length_s * rate))
        if count <= 0:
            continue
        target = int(round((block.position_s - start_s) * rate))
        origin = int(round(block.source_start_s * rate))
        piece = np.zeros(count, dtype=np.float64)
        read_lo = max(0, -origin)
        read_hi = min(count, len(samples) - origin)
        if read_hi > read_lo:
            piece[read_lo:read_hi] = samples[origin + read_lo:origin + read_hi]
        piece *= _fade_curve(count, block.fade_in_s, block.fade_out_s, rate)
        write_lo = max(0, -target)
        write_hi = min(count, len(buffer) - target)
        if write_hi > write_lo:
            buffer[target + write_lo:target + write_hi] += piece[write_lo:write_hi]
    return buffer, rate


# ---------------------------------------------------------------------------
# Energy-rise attack detection
# ---------------------------------------------------------------------------


def _frame_envelope(samples: np.ndarray, rate: int) -> tuple[np.ndarray, int]:
    hop = max(1, int(round(rate * FRAME_S)))
    frames = len(samples) // hop
    if frames <= 0:
        return np.zeros(0, dtype=np.float64), hop
    body = samples[:frames * hop].reshape(frames, hop)
    return np.sqrt(np.mean(body * body, axis=1)), hop


def _valley_depth_db(envelope: np.ndarray, hop: int, rate: int,
                     first_s: float, second_s: float) -> float:
    """Return how far the envelope falls before the second front, in dB.

    Two attacks are separated by a real decay; one attack with a long 30-75 ms
    rise is not.  The dip is measured from the loudest frame between the two
    fronts down to the quietest frame that follows it and precedes the second
    front, so dense material with slow rises does not register as a double hit.
    """
    first = int(np.floor(first_s * rate / hop))
    last = int(np.ceil(second_s * rate / hop))
    first = max(0, first)
    last = min(len(envelope) - 1, last)
    if last - first < 3:
        return 0.0
    segment = envelope[first:last + 1]
    peak_index = int(np.argmax(segment))
    if peak_index >= len(segment) - 1:
        return 0.0
    peak = float(segment[peak_index])
    floor = float(segment[peak_index:].min())
    if peak <= LEVEL_FLOOR or floor <= LEVEL_FLOOR:
        return 0.0
    return 20.0 * math.log10(peak / floor)


def _fronts(samples: np.ndarray, envelope: np.ndarray, hop: int, rate: int,
            lo_s: float, hi_s: float, min_rise: float = 0.0) -> list[float]:
    """Return front times (seconds, buffer-relative) inside ``[lo_s, hi_s]``.

    See :func:`measure_attacks` for the rule.  ``min_rise`` requires the
    crossing frame to jump by that fraction of the local peak-to-baseline
    range, which rejects ripple in a decaying tail.  Fronts closer than one
    frame are the same front and are merged.
    """
    if hi_s <= lo_s or not len(envelope):
        return []
    first = max(0, int(np.floor(lo_s * rate / hop)))
    last = min(len(envelope), int(np.ceil(hi_s * rate / hop)) + 1)
    if last - first < 2:
        return []
    window = envelope[first:last]
    baseline = float(np.percentile(window, 20))
    peak = float(window.max())
    if not np.isfinite(peak) or peak <= max(baseline * 3.0, LEVEL_FLOOR):
        return []
    threshold = baseline + 0.30 * (peak - baseline)
    span = peak - baseline
    found: list[float] = []
    for offset in range(1, len(window)):
        if window[offset] < threshold or window[offset - 1] >= threshold:
            continue
        if min_rise > 0.0 and window[offset] - window[offset - 1] < min_rise * span:
            continue
        index = first + offset
        start = index * hop
        frame = np.abs(samples[start:start + hop])
        target = 0.5 * float(envelope[index])
        hits = np.flatnonzero(frame >= target)
        sample = start if not len(hits) else start + int(hits[0])
        time = sample / rate
        if lo_s - FRAME_S <= time <= hi_s + FRAME_S:
            found.append(min(max(time, lo_s), hi_s))
    merged: list[float] = []
    for time in sorted(found):
        if merged and time - merged[-1] < FRAME_S:
            continue
        merged.append(time)
    return merged


# ---------------------------------------------------------------------------
# Attack measurement
# ---------------------------------------------------------------------------


def measure_attacks(plan: Mapping[str, Any], buffer: Any, *,
                    sample_rate: int, search_s: float = 0.060) -> list[dict[str, Any]]:
    """Locate every anchor's attack in the rendered buffer.

    Detection rule
    --------------
    The buffer is cut into non-overlapping 1 ms RMS frames
    (``round(sample_rate * 0.001)`` samples).  A scan window is usable only if
    its loudest frame is at least three times the window's 20th percentile
    (steady material never qualifies); the threshold is then
    ``baseline + 0.30 * (peak - baseline)`` and a front is the first frame
    that rises through it.  The reported time is refined inside that frame to
    the first sample at or above half the frame RMS, so a click that starts
    mid-frame is reported on its own sample.  Fronts less than 1 ms apart are
    one front.

    Each row reports the front nearest to the anchor's target inside
    ``search_s`` (or ``None``), the signed error in milliseconds, whether a
    second front exists within 40 ms (that scan requires an attack-sized rise,
    at least 20% of the local peak-to-baseline range, so decay ripple does not
    count), and whether the measured front sits within 3 ms of a block
    boundary that no anchor expects.
    """
    rate = _rate(sample_rate)
    samples = _mono(buffer)
    blocks = _blocks(plan)
    start_s, _end_s = _timeline(plan, blocks)
    envelope, hop = _frame_envelope(samples, rate)
    search = _finite(search_s, "search_s")
    if search <= 0.0:
        raise ValidationError("search_s must be positive")

    anchors = _anchors(plan)
    targets = [_anchor_target(anchor) for anchor in anchors]
    seams = [seam for seam in _seam_times(plan, blocks)
             if not any(abs(seam - target) <= SEAM_RADIUS_S for target in targets)]

    rows: list[dict[str, Any]] = []
    for anchor, expected in zip(anchors, targets):
        local = expected - start_s
        fronts = _fronts(samples, envelope, hop, rate, local - search, local + search)
        measured = min(fronts, key=lambda time: abs(time - local)) if fronts else None

        duplicate = False
        seam_front = False
        if measured is not None:
            # A duplicate must be an attack-sized rise, not ripple in the decay.
            near = _fronts(samples, envelope, hop, rate,
                           measured - DUPLICATE_RADIUS_S,
                           measured + DUPLICATE_RADIUS_S, min_rise=0.20)
            duplicate = any(
                abs(time - measured) > FRAME_S
                and _valley_depth_db(envelope, hop, rate, measured, time) >= DIP_DB
                for time in near
            )
            project = measured + start_s
            seam_front = any(abs(project - seam) <= SEAM_RADIUS_S for seam in seams)

        measured_s = None if measured is None else round(measured + start_s, 7)
        rows.append({
            "event_id": anchor.get("id"),
            "expected_s": round(float(expected), 7),
            "measured_s": measured_s,
            "error_ms": (None if measured_s is None
                         else round((measured_s - expected) * 1000.0, 3)),
            "found": measured is not None,
            "duplicate_nearby": bool(duplicate),
            "seam_front": bool(seam_front),
        })
    return rows


# ---------------------------------------------------------------------------
# Full verification
# ---------------------------------------------------------------------------


def _level_delta(samples: np.ndarray, rate: int, time_s: float) -> float:
    """RMS level after a boundary minus the level before it, in dB."""
    width = max(1, int(round(0.010 * rate)))
    centre = int(round(time_s * rate))
    before = samples[max(0, centre - width):max(0, centre)]
    after = samples[max(0, centre):min(len(samples), centre + width)]
    if not len(before) or not len(after):
        return 0.0
    left = float(np.sqrt(np.mean(before * before)))
    right = float(np.sqrt(np.mean(after * after)))
    return round(float(20.0 * np.log10((right + LEVEL_FLOOR) / (left + LEVEL_FLOOR))), 2)


def _duplicate_by_geometry(blocks: list[_Block],
                           anchors: list[dict[str, Any]]) -> int:
    """Count attacks whose source position is covered by two or more blocks.

    This is exact: it reads the plan geometry, not the audio, so it answers
    "will this attack play twice" without a detector threshold.  The signal
    based ``duplicate_nearby`` stays informational, because on dense clipped
    material a 30-75 ms rise produces fronts that are not separate notes.
    """
    count = 0
    for anchor in anchors:
        source_s = anchor.get("source_s")
        if source_s is None:
            continue
        covering = 0
        for block in blocks:
            start = float(block.source_start_s)
            length = float(block.length_s)
            if start <= float(source_s) < start + length and _gain_at(block, float(source_s) - start) > 0:
                covering += 1
        if covering > 1:
            count += 1
    return count


def verify_plan(plan: Mapping[str, Any], source: Any, *,
                sample_rate: int) -> dict[str, Any]:
    """Render the plan and report every anchor attack and every seam.

    ``seams`` carries one row per block boundary with the level step across
    it and whether a front exists there that no anchor expects.  The summary
    counts found and missing attacks, duplicate fronts and seam fronts.
    """
    buffer, rate = render_plan(plan, source, sample_rate=sample_rate)
    attacks = measure_attacks(plan, buffer, sample_rate=rate)

    blocks = _blocks(plan)
    start_s, _end_s = _timeline(plan, blocks)
    envelope, hop = _frame_envelope(buffer, rate)
    anchors = _anchors(plan)
    targets = [_anchor_target(anchor) for anchor in anchors]
    local_targets = [target - start_s for target in targets]

    seams = []
    for seam in _seam_times(plan, blocks):
        local = seam - start_s
        near = _fronts(buffer, envelope, hop, rate, local - SEAM_SCAN_S, local + SEAM_SCAN_S)
        front = any(
            abs(time - local) <= SEAM_RADIUS_S and
            all(abs(time - target) > SEAM_RADIUS_S for target in local_targets)
            for time in near)
        seams.append({
            "position_s": round(seam, 7),
            "front_detected": bool(front),
            "level_db_delta": _level_delta(buffer, rate, local),
        })

    return {
        "attacks": attacks,
        "seams": seams,
        "summary": {
            "found": sum(row["found"] for row in attacks),
            "missing": sum(not row["found"] for row in attacks),
            "duplicate_geometry": _duplicate_by_geometry(blocks, anchors),
            "signal_echo": sum(row["duplicate_nearby"] for row in attacks),
            "seam_fronts": sum(row["front_detected"] for row in seams),
        },
    }


# ---------------------------------------------------------------------------
# Geometry conflicts
# ---------------------------------------------------------------------------


def conflict_report(plan: Mapping[str, Any], source_duration_s: float) -> list[dict[str, Any]]:
    """List the plan geometry that cannot be played as written.

    Codes:
    ``source_past_end`` (block reads beyond the file),
    ``source_before_start`` (block reads before sample 0),
    ``overlap_exceeds_tail`` (neighbouring blocks overlap by more than the
    earlier block, or its remaining source, can supply),
    ``attack_under_fade`` (the summed gain at an anchor is below unity, with
    the attenuation in dB),
    ``block_too_short`` (block shorter than 4 ms),
    ``fade_longer_than_block`` (a fade cannot complete inside its block),
    ``attack_uncovered`` (no block covers an anchor) and
    ``gap`` (a hole inside the item window, reported in seconds).
    """
    duration = _finite(source_duration_s, "source_duration_s")
    if duration < 0.0:
        raise ValidationError("source_duration_s cannot be negative")
    blocks = _blocks(plan)
    conflicts: list[dict[str, Any]] = []

    for block in blocks:
        if block.length_s < MIN_BLOCK_S - 1e-12:
            conflicts.append({
                "code": "block_too_short", "block_index": block.block_index,
                "position_s": block.position_s, "length_s": round(block.length_s, 9),
                "minimum_s": MIN_BLOCK_S,
            })
        if block.source_start_s < -1e-9:
            conflicts.append({
                "code": "source_before_start", "block_index": block.block_index,
                "position_s": block.position_s,
                "source_start_s": round(block.source_start_s, 9),
            })
        if block.source_end_s > duration + 1e-9:
            conflicts.append({
                "code": "source_past_end", "block_index": block.block_index,
                "position_s": block.position_s,
                "source_start_s": round(block.source_start_s, 9),
                "source_end_s": round(block.source_end_s, 9),
                "source_duration_s": round(duration, 9),
                "excess_s": round(block.source_end_s - duration, 9),
            })
        if max(block.fade_in_s, block.fade_out_s) > block.length_s + 1e-9:
            conflicts.append({
                "code": "fade_longer_than_block", "block_index": block.block_index,
                "length_s": round(block.length_s, 9),
                "fade_in_s": round(block.fade_in_s, 9),
                "fade_out_s": round(block.fade_out_s, 9),
            })

    ordered = sorted(blocks, key=lambda block: (block.position_s, block.block_index))
    for left, right in zip(ordered, ordered[1:]):
        overlap = left.end_s - right.position_s
        if overlap <= 1e-9:
            continue
        tail = max(0.0, duration - left.source_start_s)
        if overlap > min(left.length_s, tail) + 1e-9:
            conflicts.append({
                "code": "overlap_exceeds_tail",
                "block_index": left.block_index, "next_block_index": right.block_index,
                "overlap_s": round(overlap, 9),
                "block_length_s": round(left.length_s, 9),
                "source_tail_s": round(tail, 9),
            })

    for anchor in _anchors(plan):
        target = _anchor_target(anchor)
        gain = 0.0
        covering = []
        for block in blocks:
            if block.length_s <= 0.0:
                continue
            if block.position_s <= target < block.end_s:
                value = _gain_at(block, target - block.position_s)
                gain += value
                covering.append({"block_index": block.block_index, "gain": round(value, 6)})
        if not covering:
            conflicts.append({"code": "attack_uncovered", "event_id": anchor.get("id"),
                              "project_s": round(target, 9)})
        elif gain < 1.0 - UNITY_GAIN_TOL:
            conflicts.append({
                "code": "attack_under_fade", "event_id": anchor.get("id"),
                "project_s": round(target, 9), "gain": round(gain, 6),
                "attenuation_db": round(float(-20.0 * np.log10(max(gain, LEVEL_FLOOR))), 3),
                "blocks": covering,
            })

    window = _item_window(plan, blocks)
    if window is not None:
        lo, hi = window
        cursor = lo
        intervals = sorted((block.position_s, block.end_s)
                           for block in blocks if block.length_s > 0.0)
        for start, end in intervals:
            if end <= cursor + 1e-9:
                continue
            if start > cursor + 1e-6 and cursor < hi:
                conflicts.append({"code": "gap", "start_s": round(cursor, 9),
                                  "end_s": round(min(start, hi), 9),
                                  "duration_s": round(min(start, hi) - cursor, 9)})
            cursor = max(cursor, end)
            if cursor >= hi:
                break
        if cursor < hi - 1e-6:
            conflicts.append({"code": "gap", "start_s": round(cursor, 9),
                              "end_s": round(hi, 9), "duration_s": round(hi - cursor, 9)})
    return conflicts
