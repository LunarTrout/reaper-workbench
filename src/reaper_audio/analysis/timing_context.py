"""Phrase-level timing decisions with explicit alternatives.

A grid match is not a plan.  One measured phrase can fit several annotated
reference readings, can be moved as a block, can be moved less, or can be left
where it is.  This module scores those alternatives on the musical context the
caller supplied and keeps the losing options in the output so an agent can
choose another one.

The module is deliberately pure: it knows nothing about tempo maps, REAPER,
genres or grids.  Every time, tolerance, strength and ambiguity number is a
caller input, and all coordinates stay in the caller's coordinate system
(source seconds or project seconds; the module does not convert between them).

Event fields
------------

``id``
    Stable identifier used by ``targets`` and ``pinned``.
``start_s`` / ``end_s``
    Measured start and sounding end of the event.
``vowel_s``, ``consonant_start_s``
    Vocal syllable model.  When ``vowel_s`` is present the default anchor is the
    vowel, so aligning the vowel moves the whole syllable and the consonant
    keeps its lead.  The consonant is exposed in ``protected``.
``anchor``
    Optional ``"start"``, ``"vowel"`` or ``"consonant_start"`` override.
``vowel_target_s`` / ``target_s``
    Per-event fallback target for the declared anchor.  An explicit
    ``targets`` mapping always wins.
``boundary_end_s``
    Last allowed end of the sounding event.  A later shift that would pass it
    is reported as lost tail time.
``pause_after_s`` / ``pause_before_s``
    Measured pauses the phrase must not shorten beyond ``duration_tolerance_s``.
    A positive shift eats ``pause_after_s``; a negative shift eats
    ``pause_before_s``.  Gaps inferred from neighbouring events are reported as
    context but are not protected unless the caller annotates the pause.
``next_start_s``
    Alternative to ``pause_after_s``; the gap to this time is protected.
``phrase`` (or ``phrase_key``)
    Phrase membership.  Shifts are phrase-uniform by design.

Target sets
-----------

``targets`` is one mapping ``event id -> target seconds``.  ``target_sets``
declares several annotated readings of the same signal (and replaces
``targets`` when supplied)::

    [{"id": "straight", "targets": {"a": 0.50, "b": 0.75}},
     {"id": "pushed",   "targets": {"a": 0.54, "b": 0.79}}]

An optional ``cost_s`` (or ``prior_s``) adds the caller's prior to an
interpretation's score.  Each phrase is planned independently, so a pin in one
phrase cannot change the reading of another phrase.

When two interpretations score within ``ambiguity_s`` of each other, both are
returned in ``candidates`` with their targets, residuals and shifts, the phrase
is marked ``ambiguous`` and its chosen rows are kept out of ``selected`` (they
are listed under ``unresolved``).  A caller can then pin one anchor, pass a
narrower band, or supply a prior.

Preserving duration
-------------------

With ``preserve_duration=True`` a candidate that would cut a tail, shorten a
pause, or newly overlap a protected range is kept as a rejected alternative
with exact ``lost_tail_ms`` / ``lost_pause_ms`` numbers.  Eligible alternatives
are ``leave`` (zero shift), ``block`` (the phrase-wide shift) and ``smaller``
(the largest shift toward the target that keeps the protected material).  The
chosen option is the lowest-score eligible one, so a grid hit that eats a tail
is replaced by a reduced or zero shift rather than applied silently.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "reaper-workbench.timing-phrase-plan/2"

_ANCHOR_FIELDS = {
    "start": "start_s",
    "vowel": "vowel_s",
    "consonant_start": "consonant_start_s",
}
_ANCHOR_TARGET_FIELDS = {
    "start": "target_s",
    "vowel": "vowel_target_s",
    "consonant_start": "consonant_target_s",
}
_SHIFTED_FIELDS = ("end_s", "vowel_s", "vowel_end_s",
                   "consonant_start_s", "consonant_end_s")
_SET_METADATA = frozenset({"id", "name", "label", "cost_s", "prior_s", "source"})
_EPS = 1e-9


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _optional_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _rounded(value: float) -> float:
    return round(float(value), 9)


def _ms(value: float) -> float:
    return round(float(value) * 1000.0, 6)


def _zone_bounds(zone: Mapping[str, Any]) -> tuple[float | None, float | None]:
    start = _optional_number(zone.get("start_s"))
    if start is None:
        start = _optional_number(zone.get("start"))
    end = _optional_number(zone.get("end_s"))
    if end is None:
        end = _optional_number(zone.get("end"))
    if start is None or end is None:
        return None, None
    if end < start:
        start, end = end, start
    return start, end


def _overlap_amount(start_s: float, end_s: float,
                    zones: Sequence[Mapping[str, Any]]) -> float:
    total = 0.0
    for zone in zones:
        zone_start, zone_end = _zone_bounds(zone)
        if zone_start is None or zone_end is None:
            continue
        total += max(0.0, min(end_s, zone_end) - max(start_s, zone_start))
    return total


def _anchor_name(event: Mapping[str, Any]) -> str:
    declared = event.get("anchor")
    if declared in _ANCHOR_FIELDS:
        return str(declared)
    return "vowel" if event.get("vowel_s") is not None else "start"


def _anchor_time(event: Mapping[str, Any], anchor: str) -> float:
    value = _optional_number(event.get(_ANCHOR_FIELDS[anchor]))
    if value is None:
        return _number(event.get("start_s"))
    return value


def _target_value(raw: Any) -> float | None:
    if isinstance(raw, Mapping):
        for key in ("target_s", "time_s", "target", "value"):
            value = _optional_number(raw.get(key))
            if value is not None:
                return value
        return None
    return _optional_number(raw)


def _target_for(event: Mapping[str, Any],
                targets: Mapping[str, Any]) -> float | None:
    event_id = event.get("_id")
    if event_id is None:
        event_id = event.get("id")
    raw = targets.get(str(event_id))
    if raw is not None:
        value = _target_value(raw)
        if value is not None:
            return value
    return _optional_number(event.get(_ANCHOR_TARGET_FIELDS[_anchor_name(event)]))


def _normalise_target_sets(targets: Mapping[str, Any] | None,
                           target_sets: Sequence[Mapping[str, Any]] | None
                           ) -> list[dict[str, Any]]:
    if target_sets is None:
        if targets is not None and not isinstance(targets, Mapping):
            raise ValueError("targets must map event ids to target seconds")
        return [{"id": "targets", "targets": dict(targets or {}),
                 "cost_s": 0.0, "source": "caller"}]
    if isinstance(target_sets, (str, bytes)):
        raise ValueError("target_sets must be a sequence of mappings")
    sets: list[dict[str, Any]] = []
    used: set[str] = set()
    for index, raw in enumerate(target_sets):
        if not isinstance(raw, Mapping):
            raise ValueError("each target set must be a mapping")
        set_id = str(raw.get("id") or raw.get("name") or f"set_{index + 1}")
        if set_id in used:
            set_id = f"{set_id}#{index + 1}"
        used.add(set_id)
        if "targets" in raw:
            mapping = raw.get("targets") or {}
            if not isinstance(mapping, Mapping):
                raise ValueError(f"target set {set_id!r} must map event ids to seconds")
            mapping = dict(mapping)
        else:
            mapping = {str(key): value for key, value in raw.items()
                       if key not in _SET_METADATA}
        cost = _optional_number(raw.get("cost_s", raw.get("prior_s")))
        sets.append({"id": set_id, "targets": mapping,
                     "cost_s": 0.0 if cost is None else cost,
                     "source": str(raw.get("source") or "caller")})
    if not sets:
        return [{"id": "targets", "targets": {}, "cost_s": 0.0,
                 "source": "caller"}]
    return sets


def _prepare_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            raise ValueError("events must be mappings")
        raw_id = event.get("id")
        event_id = (str(raw_id) if raw_id not in (None, "")
                    else f"event_{index + 1:04d}")
        if event_id in seen:
            raise ValueError(f"duplicate event id {event_id!r}")
        seen.add(event_id)
        prepared.append({**event, "_id": event_id})
    return prepared


def _next_external_start(event: Mapping[str, Any], phrase_ids: set[str],
                         ordered_all: Sequence[Mapping[str, Any]]) -> float | None:
    end = _optional_number(event.get("end_s"))
    if end is None:
        end = _number(event.get("start_s"))
    for other in ordered_all:
        if other["_id"] in phrase_ids:
            continue
        other_start = _number(other.get("start_s"))
        if other_start >= end - _EPS:
            return other_start
    return None


def _previous_external_end(event: Mapping[str, Any], phrase_ids: set[str],
                           ordered_all: Sequence[Mapping[str, Any]]) -> float | None:
    start = _number(event.get("start_s"))
    best: float | None = None
    for other in ordered_all:
        if other["_id"] in phrase_ids:
            continue
        other_end = _optional_number(other.get("end_s"))
        if other_end is None:
            other_end = _number(other.get("start_s"))
        if other_end <= start + _EPS and (best is None or other_end > best):
            best = other_end
    return best


def _build_pause_info(events: Sequence[Mapping[str, Any]], *,
                      phrase_ids: set[str],
                      ordered_all: Sequence[Mapping[str, Any]]
                      ) -> dict[str, dict[str, Any]]:
    info: dict[str, dict[str, Any]] = {}
    for index, event in enumerate(events):
        event_id = event["_id"]
        start = _number(event.get("start_s"))
        end = _optional_number(event.get("end_s"))

        after: dict[str, Any] | None = None
        explicit_after = _optional_number(event.get("pause_after_s"))
        if explicit_after is not None:
            after = {"s": max(0.0, explicit_after), "source": "pause_after_s",
                     "protected": True}
        elif end is not None:
            next_start = _optional_number(event.get("next_start_s"))
            if next_start is not None:
                after = {"s": max(0.0, next_start - end),
                         "source": "next_start_s", "protected": True}
            elif index == len(events) - 1:
                external = _next_external_start(event, phrase_ids, ordered_all)
                if external is not None:
                    after = {"s": max(0.0, external - end),
                             "source": "next_phrase_event", "protected": False}

        before: dict[str, Any] | None = None
        explicit_before = _optional_number(event.get("pause_before_s"))
        if explicit_before is not None:
            before = {"s": max(0.0, explicit_before),
                      "source": "pause_before_s", "protected": True}
        elif index == 0:
            external_end = _previous_external_end(event, phrase_ids, ordered_all)
            if external_end is not None:
                before = {"s": max(0.0, start - external_end),
                          "source": "previous_phrase_event", "protected": False}

        info[event_id] = {"after": after, "before": before}
    return info


def _duration_caps(events: Sequence[Mapping[str, Any]], *,
                   pause_info: Mapping[str, Mapping[str, Any]],
                   zones: Sequence[Mapping[str, Any]],
                   tolerance_s: float = 0.0
                   ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return signed shift limits for positive and negative movement.

    Positive caps are the largest right shift that keeps each tail/pause within
    the caller's duration tolerance; a negative cap is the most negative shift
    that keeps a left-side pause.  Only caller-declared pauses are caps:
    automatic gaps to neighbouring phrases stay visible as context but are not
    protected unless the caller marks them.

    A ``boundary_end_s`` that the measured end already exceeds by more than the
    tolerance is not a usable tail limit (the input is inconsistent or the
    boundary describes a different edit), so it does not produce a cap.
    """
    positive: list[dict[str, Any]] = []
    negative: list[dict[str, Any]] = []
    for event in events:
        event_id = event["_id"]
        start = _number(event.get("start_s"))
        end = _optional_number(event.get("end_s"))
        boundary = _optional_number(event.get("boundary_end_s"))
        if end is not None and boundary is not None:
            limit = boundary - end + tolerance_s
            if limit >= -_EPS:
                positive.append({"code": "tail", "event_id": event_id,
                                 "limit_s": _rounded(limit),
                                 "boundary_end_s": _rounded(boundary),
                                 "end_s": _rounded(end)})
        after = pause_info[event_id]["after"]
        if after is not None and after.get("protected"):
            positive.append({"code": "pause", "side": "after",
                             "event_id": event_id,
                             "pause_source": after["source"],
                             "limit_s": _rounded(min(after["s"], tolerance_s))})
        before = pause_info[event_id]["before"]
        if before is not None and before.get("protected"):
            negative.append({"code": "pause", "side": "before",
                             "event_id": event_id,
                             "pause_source": before["source"],
                             "limit_s": _rounded(-min(before["s"], tolerance_s))})
        right_edge = end if end is not None else start
        for zone_index, zone in enumerate(zones):
            zone_start, zone_end = _zone_bounds(zone)
            if zone_start is None or zone_end is None:
                continue
            if zone_start >= right_edge - _EPS:
                positive.append({"code": "protected_range",
                                 "event_id": event_id,
                                 "zone_index": zone_index,
                                 "limit_s": _rounded(zone_start - right_edge)})
            if zone_end <= start + _EPS:
                negative.append({"code": "protected_range",
                                 "event_id": event_id,
                                 "zone_index": zone_index,
                                 "limit_s": _rounded(zone_end - start)})
    return positive, negative


def _tail_loss(end_s: float | None, shift_s: float,
               boundary_s: float | None) -> float:
    if end_s is None or boundary_s is None:
        return 0.0
    baseline = max(0.0, end_s - boundary_s)
    moved = max(0.0, end_s + shift_s - boundary_s)
    return max(0.0, moved - baseline)


def _pause_loss(pause: Mapping[str, Any] | None,
                shift_s: float) -> tuple[float, float]:
    if pause is None:
        return 0.0, 0.0
    if shift_s > 0:
        return min(shift_s, pause["s"]), max(0.0, shift_s - pause["s"])
    if shift_s < 0:
        amount = -shift_s
        return min(amount, pause["s"]), max(0.0, amount - pause["s"])
    return 0.0, 0.0


def _measure_shift(shift_s: float, *,
                   phrase_id: str,
                   events: Sequence[Mapping[str, Any]],
                   desired: Mapping[str, Mapping[str, Any]],
                   pause_info: Mapping[str, Mapping[str, Any]],
                   zones: Sequence[Mapping[str, Any]],
                   tolerance_s: float = 0.0) -> dict[str, Any]:
    """Apply one phrase shift and return rows plus duration accounting."""
    rows: list[dict[str, Any]] = []
    timing_error = 0.0
    targeted = 0
    lost_tail = 0.0
    lost_pause_after = 0.0
    lost_pause_before = 0.0
    overlap_after = 0.0
    overlap_before = 0.0
    protected_overlap = 0.0
    duration_events: list[dict[str, Any]] = []

    for event in events:
        event_id = event["_id"]
        start = _number(event.get("start_s"))
        end = _optional_number(event.get("end_s"))
        boundary = _optional_number(event.get("boundary_end_s"))
        entry = desired.get(event_id)

        row: dict[str, Any] = {
            "event_id": event_id,
            "phrase": phrase_id,
            "anchor": entry["anchor"] if entry is not None else _anchor_name(event),
            "targeted": entry is not None,
            "shift_s": _rounded(shift_s),
            "start_s": _rounded(start + shift_s),
        }
        for field in _SHIFTED_FIELDS:
            value = _optional_number(event.get(field))
            if value is not None:
                row[field] = _rounded(value + shift_s)

        vowel = _optional_number(event.get("vowel_s"))
        consonant = _optional_number(event.get("consonant_start_s"))
        consonant_source = "consonant_start_s"
        if consonant is None:
            consonant = start
            consonant_source = "start_s"
        if vowel is not None and vowel - consonant > _EPS:
            lead = vowel - consonant
            row["articulation"] = {
                "anchor": "vowel",
                "consonant_source": consonant_source,
                "consonant_lead_s": _rounded(lead),
                "consonant_lead_after_s": _rounded(
                    (vowel + shift_s) - (consonant + shift_s)),
                "consonant_preserved": True,
            }
            row["protected"] = [{
                "part": "consonant",
                "start_s": _rounded(consonant + shift_s),
                "end_s": _rounded(vowel + shift_s),
                "duration_s": _rounded(lead),
                "reason": "moves_with_vowel",
                "protected": True,
            }]

        if entry is not None:
            anchor_after = _anchor_time(event, entry["anchor"]) + shift_s
            residual = entry["target"] - anchor_after
            row.update({
                "target_s": _rounded(entry["target"]),
                "anchor_before_s": _rounded(entry["anchor_s"]),
                "anchor_after_s": _rounded(anchor_after),
                "residual_s": _rounded(residual),
            })
            timing_error += abs(residual)
            targeted += 1

        tail_boundary = (boundary if (end is not None and boundary is not None
                                      and boundary >= end - tolerance_s - _EPS)
                         else None)
        tail = _tail_loss(end, shift_s, tail_boundary)
        pause_after = pause_info[event_id]["after"]
        pause_before = pause_info[event_id]["before"]
        protected_after = pause_after if (pause_after is not None
                                          and pause_after.get("protected")) else None
        protected_before = pause_before if (pause_before is not None
                                            and pause_before.get("protected")) else None
        after_loss, after_overlap = (_pause_loss(protected_after, shift_s)
                                     if shift_s > 0 else (0.0, 0.0))
        before_loss, before_overlap = (_pause_loss(protected_before, shift_s)
                                       if shift_s < 0 else (0.0, 0.0))

        base_start = start
        base_end = end if end is not None else start
        if base_end < base_start:
            base_start, base_end = base_end, base_start
        moved_start = base_start + shift_s
        moved_end = base_end + shift_s
        baseline_overlap = _overlap_amount(base_start, base_end, zones)
        moved_overlap = _overlap_amount(moved_start, moved_end, zones)
        range_overlap = max(0.0, moved_overlap - baseline_overlap)

        lost_tail += tail
        lost_pause_after += after_loss
        lost_pause_before += before_loss
        overlap_after += after_overlap
        overlap_before += before_overlap
        protected_overlap += range_overlap

        row["duration"] = {
            "lost_tail_s": _rounded(tail),
            "lost_tail_ms": _ms(tail),
            "lost_pause_s": _rounded(after_loss + before_loss),
            "lost_pause_ms": _ms(after_loss + before_loss),
            "lost_pause_after_ms": _ms(after_loss),
            "lost_pause_before_ms": _ms(before_loss),
            "overlap_ms": _ms(after_overlap + before_overlap),
            "protected_overlap_ms": _ms(range_overlap),
            "pause_after_s": (_rounded(pause_after["s"])
                              if pause_after is not None else None),
            "pause_after_source": (pause_after["source"]
                                   if pause_after is not None else None),
            "pause_before_s": (_rounded(pause_before["s"])
                               if pause_before is not None else None),
            "pause_before_source": (pause_before["source"]
                                    if pause_before is not None else None),
        }
        if (tail or after_loss or before_loss or after_overlap
                or before_overlap or range_overlap):
            duration_events.append({
                "event_id": event_id,
                "lost_tail_ms": _ms(tail),
                "lost_pause_ms": _ms(after_loss + before_loss),
                "lost_pause_after_ms": _ms(after_loss),
                "lost_pause_before_ms": _ms(before_loss),
                "overlap_ms": _ms(after_overlap + before_overlap),
                "protected_overlap_ms": _ms(range_overlap),
            })
        rows.append(row)

    lost_pause = lost_pause_after + lost_pause_before
    return {
        "events": rows,
        "targeted": targeted,
        "timing_error_s": _rounded(timing_error),
        "mean_abs_error_s": (_rounded(timing_error / targeted)
                             if targeted else 0.0),
        "lost_tail_s": _rounded(lost_tail),
        "lost_tail_ms": _ms(lost_tail),
        "lost_pause_s": _rounded(lost_pause),
        "lost_pause_ms": _ms(lost_pause),
        "lost_pause_after_ms": _ms(lost_pause_after),
        "lost_pause_before_ms": _ms(lost_pause_before),
        "overlap_ms": _ms(overlap_after + overlap_before),
        "protected_overlap_s": _rounded(protected_overlap),
        "protected_overlap_ms": _ms(protected_overlap),
        "duration_events": duration_events,
    }


def _candidate_shifts(desired: Sequence[Mapping[str, Any]],
                      positive_caps: Sequence[Mapping[str, Any]],
                      negative_caps: Sequence[Mapping[str, Any]]
                      ) -> list[dict[str, Any]]:
    records: dict[float, dict[str, Any]] = {}

    def add(shift_s: float, kind: str, source: str, *,
            requested: float | None = None,
            limited_by: Iterable[str] = ()) -> None:
        key = _rounded(shift_s)
        record = records.setdefault(key, {
            "shift_s": key,
            "kinds": [],
            "sources": [],
            "requested_shifts": [],
            "limited_by": [],
        })
        if kind not in record["kinds"]:
            record["kinds"].append(kind)
        if source not in record["sources"]:
            record["sources"].append(source)
        if requested is not None:
            value = _rounded(requested)
            if value not in record["requested_shifts"]:
                record["requested_shifts"].append(value)
        for code in limited_by:
            if code not in record["limited_by"]:
                record["limited_by"].append(code)

    add(0.0, "leave", "measured")
    ordered = sorted(entry["shift_s"] for entry in desired)
    add(ordered[0], "block", "event_min")
    if len(ordered) > 1:
        add(ordered[-1], "block", "event_max")
        middle = len(ordered) // 2
        median = (ordered[middle] if len(ordered) % 2
                  else (ordered[middle - 1] + ordered[middle]) / 2.0)
        add(median, "block", "phrase_median")
        add(sum(ordered) / len(ordered), "block", "phrase_consensus")

    max_positive = (min(cap["limit_s"] for cap in positive_caps)
                    if positive_caps else None)
    max_negative = (max(cap["limit_s"] for cap in negative_caps)
                    if negative_caps else None)
    for entry in desired:
        shift = entry["shift_s"]
        if shift > _EPS and max_positive is not None and max_positive < shift - _EPS:
            safe = max(0.0, max_positive)
            binding = [cap["code"] for cap in positive_caps
                       if abs(cap["limit_s"] - max_positive) <= _EPS]
            add(safe, "smaller", f"reduced:{entry['event_id']}",
                requested=shift, limited_by=binding)
        elif shift < -_EPS and max_negative is not None and max_negative > shift + _EPS:
            safe = min(0.0, max_negative)
            binding = [cap["code"] for cap in negative_caps
                       if abs(cap["limit_s"] - max_negative) <= _EPS]
            add(safe, "smaller", f"reduced:{entry['event_id']}",
                requested=shift, limited_by=binding)
    return list(records.values())


def _option_reasons(*,
                    shift_s: float,
                    kinds: Sequence[str],
                    measured: Mapping[str, Any],
                    meta: Mapping[str, Any],
                    context: Mapping[str, Any],
                    preserve_duration: bool,
                    tolerance_s: float) -> tuple[bool, list[dict[str, Any]]]:
    rejected = False
    reasons: list[dict[str, Any]] = []

    if measured["lost_tail_s"] > tolerance_s + _EPS:
        if preserve_duration:
            rejected = True
        reasons.append({
            "code": "protected_tail" if preserve_duration else "tail_loss_allowed",
            "message": (f"shift {shift_s:+.6f} s would cut "
                        f"{measured['lost_tail_ms']:.3f} ms of tail"),
            "lost_tail_s": measured["lost_tail_s"],
            "lost_tail_ms": measured["lost_tail_ms"],
            "events": [row["event_id"] for row in measured["duration_events"]
                       if row["lost_tail_ms"] > 0],
        })
    if measured["lost_pause_s"] > tolerance_s + _EPS:
        if preserve_duration:
            rejected = True
        reasons.append({
            "code": "protected_pause" if preserve_duration else "pause_loss_allowed",
            "message": (f"shift {shift_s:+.6f} s would shorten "
                        f"{measured['lost_pause_ms']:.3f} ms of pause"),
            "lost_pause_s": measured["lost_pause_s"],
            "lost_pause_ms": measured["lost_pause_ms"],
            "lost_pause_after_ms": measured["lost_pause_after_ms"],
            "lost_pause_before_ms": measured["lost_pause_before_ms"],
            "events": [row["event_id"] for row in measured["duration_events"]
                       if row["lost_pause_ms"] > 0],
        })
    if measured["protected_overlap_s"] > _EPS:
        rejected = True
        reasons.append({
            "code": "protected_range",
            "message": (f"shift {shift_s:+.6f} s would newly overlap "
                        f"{measured['protected_overlap_ms']:.3f} ms of protected material"),
            "protected_overlap_s": measured["protected_overlap_s"],
            "protected_overlap_ms": measured["protected_overlap_ms"],
        })
    if measured["overlap_ms"] > _EPS:
        rejected = True
        reasons.append({
            "code": "event_overlap",
            "message": (f"shift {shift_s:+.6f} s would run into "
                        f"{measured['overlap_ms']:.3f} ms of protected following material"),
            "overlap_ms": measured["overlap_ms"],
        })

    if "smaller" in kinds:
        requested_values = list(meta.get("requested_shifts") or ())
        if requested_values:
            requested = max(requested_values, key=abs)
            at_requested = _measure_shift(requested, **context)
            reasons.append({
                "code": "reduced_shift",
                "message": "shift reduced to keep protected duration and pauses",
                "requested_shift_s": _rounded(requested),
                "applied_shift_s": _rounded(shift_s),
                "avoided_tail_ms": at_requested["lost_tail_ms"],
                "avoided_pause_ms": at_requested["lost_pause_ms"],
                "avoided_protected_overlap_ms": at_requested["protected_overlap_ms"],
                "limited_by": list(meta.get("limited_by") or ()),
            })
    elif shift_s == 0:
        reasons.append({
            "code": "leave_phrase",
            "message": "keep the measured phrase where it is",
        })
    else:
        reasons.append({
            "code": "phrase_block_shift",
            "message": "move the whole phrase by one block shift",
            "event_count": len(measured["events"]),
        })

    if any("articulation" in row for row in measured["events"]):
        lead = next(row["articulation"]["consonant_lead_s"]
                    for row in measured["events"] if "articulation" in row)
        reasons.append({
            "code": "vowel_anchor",
            "message": "the vowel target moves the whole syllable; the consonant keeps its lead",
            "consonant_lead_s": lead,
            "consonant_preserved": True,
        })
    if "pinned" in (meta.get("sources") or ()):
        reasons.append({
            "code": "pinned_anchor",
            "message": "the phrase is pinned at its measured timing",
        })
    return rejected, reasons


def _build_option(*, phrase_id: str,
                  interpretation: Mapping[str, Any],
                  meta: Mapping[str, Any],
                  measured: Mapping[str, Any],
                  context: Mapping[str, Any],
                  preserve_duration: bool,
                  tolerance_s: float,
                  shift_cost_per_s: float,
                  duration_cost_per_s: float) -> dict[str, Any]:
    shift_s = float(meta["shift_s"])
    kinds = list(meta["kinds"])
    if abs(shift_s) <= _EPS:
        kind = "leave"
    elif "smaller" in kinds and meta.get("requested_shifts"):
        kind = "smaller"
    else:
        kind = "block"

    rejected, reasons = _option_reasons(
        shift_s=shift_s, kinds=kinds, measured=measured, meta=meta,
        context=context, preserve_duration=preserve_duration,
        tolerance_s=tolerance_s)

    duration_loss_s = measured["lost_tail_s"] + measured["lost_pause_s"]
    score_s = (measured["mean_abs_error_s"] + interpretation["cost_s"]
               + shift_cost_per_s * abs(shift_s)
               + duration_cost_per_s * duration_loss_s)
    return {
        "option_id": f"{phrase_id}:{interpretation['id']}:{shift_s:+.9f}",
        "phrase": phrase_id,
        "interpretation": interpretation["id"],
        "kind": kind,
        "kinds": sorted(set(kinds)),
        "shift_s": _rounded(shift_s),
        "sources": list(meta["sources"]),
        "requested_shifts_s": list(meta.get("requested_shifts") or ()),
        "limited_by": list(meta.get("limited_by") or ()),
        "timing_error_s": measured["timing_error_s"],
        "mean_abs_error_s": measured["mean_abs_error_s"],
        "lost_tail_s": measured["lost_tail_s"],
        "lost_tail_ms": measured["lost_tail_ms"],
        "lost_pause_s": measured["lost_pause_s"],
        "lost_pause_ms": measured["lost_pause_ms"],
        "lost_pause_after_ms": measured["lost_pause_after_ms"],
        "lost_pause_before_ms": measured["lost_pause_before_ms"],
        "overlap_ms": measured["overlap_ms"],
        "protected_overlap_s": measured["protected_overlap_s"],
        "protected_overlap_ms": measured["protected_overlap_ms"],
        "duration_loss_s": _rounded(duration_loss_s),
        "duration_loss_ms": _ms(duration_loss_s),
        "duration_events": measured["duration_events"],
        "cost_s": interpretation["cost_s"],
        "score_s": _rounded(score_s),
        "rejected": rejected,
        "reasons": reasons,
        "events": measured["events"],
    }


def _option_key(option: Mapping[str, Any]) -> tuple[Any, ...]:
    return (option["score_s"], option["timing_error_s"],
            option["duration_loss_s"], abs(option["shift_s"]),
            option["option_id"])


def _issue_key(code: str, phrase_id: str, shift_s: float) -> tuple[Any, ...]:
    return (code, phrase_id, _rounded(shift_s))


def plan_phrase(events: Sequence[Mapping[str, Any]],
                targets: Mapping[str, Any] | None = None, *,
                target_sets: Sequence[Mapping[str, Any]] | None = None,
                ambiguity_s: float = 0.0,
                phrase_key: str = "phrase",
                strength: float = 1.0,
                preserve_duration: bool = True,
                protected_ranges: Sequence[Mapping[str, Any]] = (),
                pinned: Iterable[str] = (),
                duration_tolerance_s: float = 0.0,
                shift_cost_per_s: float = 0.0,
                duration_cost_per_s: float = 0.0) -> dict[str, Any]:
    """Return selected phrase shifts plus every credible alternative.

    ``events`` need ``id`` and ``start_s``; optional fields are described in the
    module docstring.  ``targets`` maps event id to a target anchor time; for a
    vocal event the anchor is ``vowel_s`` unless ``anchor`` says otherwise.
    ``target_sets`` replaces ``targets`` with several annotated readings of the
    same signal.  ``ambiguity_s`` is the caller's score band: interpretations
    closer than that are both returned and the phrase is left unresolved.

    ``preserve_duration`` rejects shifts that would cut a tail, shorten a pause,
    or newly overlap a protected range; rejected candidates stay in
    ``alternatives`` with exact millisecond losses.  ``duration_tolerance_s``
    lets the caller accept a small annotated loss.  ``shift_cost_per_s`` and
    ``duration_cost_per_s`` add caller-declared costs to the timing score.

    A pinned event fixes its own phrase at the measured timing; phrases without
    a pin are planned independently, so pinning one anchor cannot change
    another phrase.
    """
    if not 0.0 <= strength <= 1.0:
        raise ValueError("strength must be between 0 and 1")
    for name, value in (("ambiguity_s", ambiguity_s),
                        ("duration_tolerance_s", duration_tolerance_s),
                        ("shift_cost_per_s", shift_cost_per_s),
                        ("duration_cost_per_s", duration_cost_per_s)):
        if value < 0:
            raise ValueError(f"{name} must not be negative")

    interpretations = _normalise_target_sets(targets, target_sets)
    prepared = _prepare_events(events)
    pinned_ids = {str(value) for value in (pinned or ())}
    zones = [zone for zone in (protected_ranges or ())
             if isinstance(zone, Mapping)]

    groups: dict[str, list[dict[str, Any]]] = {}
    for event in prepared:
        phrase_id = str(event.get(phrase_key, "phrase"))
        groups.setdefault(phrase_id, []).append(event)
    for members in groups.values():
        members.sort(key=lambda event: (_number(event.get("start_s")),
                                        event["_id"]))
    ordered_all = sorted(prepared, key=lambda event: (
        _number(event.get("start_s")), event["_id"]))

    selected: list[dict[str, Any]] = []
    phrase_reports: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    legacy_alternatives: list[dict[str, Any]] = []
    issue_map: dict[tuple[Any, ...], dict[str, Any]] = {}

    def add_issue(key: tuple[Any, ...], payload: dict[str, Any]) -> None:
        existing = issue_map.get(key)
        if existing is None:
            issue_map[key] = payload
            return
        for field in ("lost_tail_ms", "lost_pause_ms", "overlap_ms",
                      "protected_overlap_ms"):
            if payload.get(field, 0.0) > existing.get(field, 0.0):
                existing[field] = payload[field]

    for phrase_id, phrase_events in groups.items():
        phrase_ids = {event["_id"] for event in phrase_events}
        pinned_events = [event["_id"] for event in phrase_events
                         if event["_id"] in pinned_ids]
        pause_info = _build_pause_info(phrase_events, phrase_ids=phrase_ids,
                                       ordered_all=ordered_all)
        positive_caps, negative_caps = _duration_caps(
            phrase_events, pause_info=pause_info, zones=zones,
            tolerance_s=duration_tolerance_s)

        options_by_interpretation: dict[str, list[dict[str, Any]]] = {}
        contexts_by_interpretation: dict[str, dict[str, Any]] = {}
        interpretation_reports: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []

        for interpretation in interpretations:
            desired_entries: list[dict[str, Any]] = []
            for event in phrase_events:
                target = _target_for(event, interpretation["targets"])
                if target is None:
                    continue
                anchor = _anchor_name(event)
                anchor_s = _anchor_time(event, anchor)
                desired_entries.append({
                    "event_id": event["_id"],
                    "target": target,
                    "anchor": anchor,
                    "anchor_s": anchor_s,
                    "raw_shift_s": _rounded(target - anchor_s),
                    "shift_s": _rounded((target - anchor_s) * strength),
                })
            desired = {entry["event_id"]: entry for entry in desired_entries}
            report: dict[str, Any] = {
                "interpretation": interpretation["id"],
                "cost_s": interpretation["cost_s"],
                "applicable": bool(desired_entries),
                "target_count": len(desired_entries),
                "targets": {entry["event_id"]: _rounded(entry["target"])
                            for entry in desired_entries},
                "anchors": {entry["event_id"]: entry["anchor"]
                            for entry in desired_entries},
                "desired_shifts_s": {entry["event_id"]: entry["shift_s"]
                                     for entry in desired_entries},
                "raw_desired_shifts_s": {entry["event_id"]: entry["raw_shift_s"]
                                         for entry in desired_entries},
            }
            if not desired_entries:
                report.update({"reason": "no_targets_for_phrase",
                               "plausible": False, "selected": False})
                interpretation_reports.append(report)
                continue

            context = {
                "phrase_id": phrase_id,
                "events": phrase_events,
                "desired": desired,
                "pause_info": pause_info,
                "zones": zones,
                "tolerance_s": duration_tolerance_s,
            }
            contexts_by_interpretation[interpretation["id"]] = context
            if pinned_events:
                metas: list[dict[str, Any]] = [{
                    "shift_s": 0.0,
                    "kinds": ["leave"],
                    "sources": ["pinned"],
                    "requested_shifts": [],
                    "limited_by": [],
                }]
            else:
                metas = _candidate_shifts(desired_entries, positive_caps,
                                          negative_caps)

            options: list[dict[str, Any]] = []
            for meta in metas:
                measured = _measure_shift(meta["shift_s"], **context)
                options.append(_build_option(
                    phrase_id=phrase_id,
                    interpretation=interpretation,
                    meta=meta,
                    measured=measured,
                    context=context,
                    preserve_duration=preserve_duration,
                    tolerance_s=duration_tolerance_s,
                    shift_cost_per_s=shift_cost_per_s,
                    duration_cost_per_s=duration_cost_per_s,
                ))
            options_by_interpretation[interpretation["id"]] = options
            eligible = [option for option in options if not option["rejected"]]
            best = min(eligible, key=_option_key)
            residuals = {row["event_id"]: row["residual_s"]
                         for row in best["events"] if row["targeted"]}
            report.update({
                "best_option_id": best["option_id"],
                "best_shift_s": best["shift_s"],
                "score_s": best["score_s"],
                "mean_abs_error_s": best["mean_abs_error_s"],
                "timing_error_s": best["timing_error_s"],
                "residuals_s": residuals,
                "plausible": False,
                "selected": False,
            })
            interpretation_reports.append(report)
            candidates.append(best)

        if not candidates:
            phrase_reports.append({
                "phrase": phrase_id,
                "resolution": "no_targets",
                "applied": False,
                "ambiguous": False,
                "needs_decision": True,
                "pinned_events": pinned_events,
                "chosen": None,
                "candidates": [],
                "alternatives": [],
                "interpretations": interpretation_reports,
                "reasons": [{
                    "code": "no_reference_targets",
                    "message": "no reference target covers this phrase",
                }],
            })
            add_issue(("no_reference_targets", phrase_id, 0.0), {
                "code": "no_reference_targets",
                "phrase": phrase_id,
                "next_action": "add a reference target or leave this phrase out of the plan",
            })
            legacy_alternatives.append({"phrase": phrase_id, "chosen": None,
                                        "alternatives": []})
            continue

        candidates.sort(key=_option_key)
        chosen = candidates[0]
        best_score = chosen["score_s"]
        plausible = [candidate for candidate in candidates
                     if candidate["score_s"] <= best_score + ambiguity_s + _EPS]
        gap = (candidates[1]["score_s"] - best_score
               if len(candidates) > 1 else 0.0)
        ambiguous = len(plausible) > 1 and not pinned_events
        for candidate in candidates:
            candidate["plausible"] = candidate in plausible
            candidate["selected"] = candidate is chosen
        for report in interpretation_reports:
            if not report.get("applicable"):
                continue
            option = next((candidate for candidate in candidates
                           if candidate["interpretation"] == report["interpretation"]),
                          None)
            if option is not None:
                report["plausible"] = option["plausible"]
                report["selected"] = option["selected"]

        chosen_options = options_by_interpretation[chosen["interpretation"]]
        alternatives = [option for option in chosen_options
                        if option["option_id"] != chosen["option_id"]]
        for candidate in candidates:
            if (candidate["interpretation"] != chosen["interpretation"]
                    and all(option["option_id"] != candidate["option_id"]
                            for option in alternatives)):
                alternatives.append(candidate)
        alternatives.sort(key=lambda option: (
            option["rejected"], option["score_s"], option["timing_error_s"],
            abs(option["shift_s"]), option["option_id"]))

        resolution = ("ambiguous" if ambiguous
                      else "pinned" if pinned_events else "selected")
        phrase_reasons: list[dict[str, Any]] = []
        if pinned_events:
            phrase_reasons.append({
                "code": "pinned_anchor",
                "message": "the pinned anchor fixes this phrase at measured timing",
                "events": pinned_events,
            })
            add_issue(("pinned_anchor", phrase_id, 0.0), {
                "code": "pinned_anchor",
                "phrase": phrase_id,
                "events": pinned_events,
                "next_action": "unpin the anchor to let this phrase move",
            })
        if ambiguous:
            phrase_reasons.append({
                "code": "ambiguous_grid",
                "message": "several reference readings fit within the ambiguity band",
                "interpretations": [candidate["interpretation"]
                                    for candidate in plausible],
                "ambiguity_gap_s": _rounded(gap),
                "ambiguity_band_s": _rounded(ambiguity_s),
                "next_action": "pin one anchor or pass a narrower reference target set",
            })
            add_issue(("ambiguous_grid", phrase_id, 0.0), {
                "code": "ambiguous_grid",
                "phrase": phrase_id,
                "interpretations": [candidate["interpretation"]
                                    for candidate in plausible],
                "ambiguity_gap_s": _rounded(gap),
                "ambiguity_band_s": _rounded(ambiguity_s),
                "candidates": [{
                    "interpretation": candidate["interpretation"],
                    "shift_s": candidate["shift_s"],
                    "score_s": candidate["score_s"],
                    "mean_abs_error_s": candidate["mean_abs_error_s"],
                } for candidate in candidates],
                "next_action": "pin one anchor or pass a narrower reference target set",
            })
        phrase_reasons.append({
            "code": "lowest_score",
            "message": "chosen for the lowest timing score among eligible alternatives",
            "interpretation": chosen["interpretation"],
            "score_s": chosen["score_s"],
            "timing_error_s": chosen["timing_error_s"],
        })

        for option in options_by_interpretation[chosen["interpretation"]]:
            if not option["rejected"]:
                continue
            if option["lost_tail_ms"] > 0:
                add_issue(_issue_key("protected_tail", phrase_id, option["shift_s"]), {
                    "code": "protected_tail",
                    "phrase": phrase_id,
                    "interpretation": option["interpretation"],
                    "option_id": option["option_id"],
                    "shift_s": option["shift_s"],
                    "lost_tail_ms": option["lost_tail_ms"],
                    "lost_pause_ms": option["lost_pause_ms"],
                    "next_action": "audition the smaller shift or leave the phrase",
                })
            if option["lost_pause_ms"] > 0:
                add_issue(_issue_key("protected_pause", phrase_id, option["shift_s"]), {
                    "code": "protected_pause",
                    "phrase": phrase_id,
                    "interpretation": option["interpretation"],
                    "option_id": option["option_id"],
                    "shift_s": option["shift_s"],
                    "lost_pause_ms": option["lost_pause_ms"],
                    "lost_pause_after_ms": option["lost_pause_after_ms"],
                    "lost_pause_before_ms": option["lost_pause_before_ms"],
                    "next_action": "audition the smaller shift or leave the phrase",
                })
            if option["protected_overlap_ms"] > 0:
                add_issue(_issue_key("protected_range", phrase_id, option["shift_s"]), {
                    "code": "protected_range",
                    "phrase": phrase_id,
                    "interpretation": option["interpretation"],
                    "option_id": option["option_id"],
                    "shift_s": option["shift_s"],
                    "protected_overlap_ms": option["protected_overlap_ms"],
                    "next_action": "keep the measured phrase or move the protected range",
                })

        if "smaller" in chosen["kinds"] and chosen["requested_shifts_s"]:
            requested = max(chosen["requested_shifts_s"], key=abs)
            requested_measured = _measure_shift(
                requested, **contexts_by_interpretation[chosen["interpretation"]])
            phrase_reasons.append({
                "code": "duration_preserved_by_smaller_shift",
                "message": (f"the full grid hit would lose "
                            f"{requested_measured['lost_tail_ms']:.3f} ms of tail, "
                            f"{requested_measured['lost_pause_ms']:.3f} ms of pause and "
                            f"overlap {requested_measured['protected_overlap_ms']:.3f} ms "
                            f"of protected material"),
                "requested_shift_s": _rounded(requested),
                "applied_shift_s": chosen["shift_s"],
                "next_action": "audition the smaller shift or leave the phrase",
            })
            add_issue(_issue_key("duration_preserved_by_smaller_shift",
                                 phrase_id, chosen["shift_s"]), {
                "code": "duration_preserved_by_smaller_shift",
                "phrase": phrase_id,
                "interpretation": chosen["interpretation"],
                "requested_shift_s": _rounded(requested),
                "applied_shift_s": chosen["shift_s"],
                "limited_by": chosen["limited_by"],
                "lost_tail_ms": requested_measured["lost_tail_ms"],
                "lost_pause_ms": requested_measured["lost_pause_ms"],
                "protected_overlap_ms": requested_measured["protected_overlap_ms"],
                "next_action": "audition the smaller shift or leave the phrase",
            })

        applied = not ambiguous
        if applied:
            selected.extend(chosen["events"])
        else:
            unresolved.append({
                "phrase": phrase_id,
                "reason": "ambiguous_reference",
                "candidates": [{
                    "interpretation": candidate["interpretation"],
                    "shift_s": candidate["shift_s"],
                    "score_s": candidate["score_s"],
                    "mean_abs_error_s": candidate["mean_abs_error_s"],
                } for candidate in plausible],
                "next_action": "pin one anchor or pass a narrower reference target set",
            })

        phrase_reports.append({
            "phrase": phrase_id,
            "resolution": resolution,
            "applied": applied,
            "ambiguous": ambiguous,
            "needs_decision": ambiguous,
            "pinned_events": pinned_events,
            "ambiguity_gap_s": _rounded(gap),
            "chosen": chosen,
            "candidates": candidates,
            "alternatives": alternatives,
            "interpretations": interpretation_reports,
            "reasons": phrase_reasons,
        })
        legacy_alternatives.append({
            "phrase": phrase_id,
            "chosen": chosen,
            "alternatives": alternatives,
        })

    issues = list(issue_map.values())
    decided = sum(1 for report in phrase_reports
                  if report["chosen"] is not None and report["applied"])
    if not phrase_reports or decided == 0:
        coverage = "empty"
    elif unresolved or decided < len(phrase_reports):
        coverage = "partial"
    else:
        coverage = "complete"
    return {
        "schema": SCHEMA,
        "strength": strength,
        "preserve_duration": preserve_duration,
        "ambiguity_band_s": _rounded(ambiguity_s),
        "phrase_count": len(phrase_reports),
        "decided_phrase_count": decided,
        "selected_event_count": len(selected),
        "coverage": coverage,
        "selected": selected,
        "unresolved": unresolved,
        "phrases": phrase_reports,
        "alternatives": legacy_alternatives,
        "issues": issues,
    }
