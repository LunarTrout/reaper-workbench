"""Phrase-level timing decisions with explicit alternatives.

A grid match is not a plan: the same attack can be moved alone, with its
phrase, weakened, or left alone.  This module scores those alternatives on the
musical context the caller supplied (targets, phrase boundaries, protected
tails, consonants) and keeps the losing options in the output so an agent can
choose another one.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _target_for(event: Mapping[str, Any], targets: Mapping[str, float],
                key: str) -> float | None:
    value = targets.get(str(event.get("id")))
    if value is not None:
        return float(value)
    if key == "vowel" and event.get("vowel_target_s") is not None:
        return float(event["vowel_target_s"])
    return None


def _shift_for(event: Mapping[str, Any], target_s: float,
               key: str) -> float:
    anchor = event.get("vowel_s") if key == "vowel" else event.get("start_s")
    if anchor is None:
        anchor = event.get("start_s", 0.0)
    return float(target_s) - float(anchor)


def _apply(event: Mapping[str, Any], shift_s: float,
           key: str) -> dict[str, Any]:
    start = _number(event.get("start_s"))
    row = {
        "event_id": event.get("id"),
        "shift_s": round(shift_s, 7),
        "start_s": round(start + shift_s, 7),
    }
    if event.get("end_s") is not None:
        row["end_s"] = round(_number(event["end_s"]) + shift_s, 7)
    if event.get("vowel_s") is not None:
        row["vowel_s"] = round(_number(event["vowel_s"]) + shift_s, 7)
    if event.get("consonant_start_s") is not None:
        row["consonant_start_s"] = round(
            _number(event["consonant_start_s"]) + shift_s, 7)
    return row


def _duration_loss(event: Mapping[str, Any], shift_s: float,
                   boundary_s: float | None) -> float:
    if boundary_s is None or event.get("end_s") is None:
        return 0.0
    return max(0.0, _number(event["end_s"]) + shift_s - boundary_s)


def plan_phrase(events: Sequence[Mapping[str, Any]],
                targets: Mapping[str, float], *,
                phrase_key: str = "phrase",
                strength: float = 1.0,
                preserve_duration: bool = True,
                protected_ranges: Sequence[Mapping[str, Any]] = (),
                pinned: Iterable[str] = ()) -> dict[str, Any]:
    """Return selected shifts plus every credible alternative.

    ``events`` need ``id``, ``start_s``, ``end_s``; optional ``vowel_s``,
    ``consonant_start_s``, ``boundary_end_s`` and ``phrase``.  ``targets`` maps
    event id to a target anchor time; for vocal events the anchor is
    ``vowel_s``, otherwise ``start_s``.
    """
    if not 0.0 <= strength <= 1.0:
        raise ValueError("strength must be between 0 and 1")
    pinned_ids = {str(value) for value in pinned}
    phrases: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        phrases.setdefault(str(event.get(phrase_key, "phrase")), []).append(event)
    selected: list[dict[str, Any]] = []
    alternatives: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for phrase_id, phrase_events in phrases.items():
        candidates = [0.0]
        desired = []
        for event in phrase_events:
            event_id = str(event.get("id"))
            target = _target_for(event, targets, "vowel" if event.get("vowel_s") is not None else "start")
            if target is None:
                continue
            key = "vowel" if event.get("vowel_s") is not None else "start"
            desired.append((event, target, _shift_for(event, target, key)))
        if not desired:
            continue
        phrase_pinned = any(str(event.get("id")) in pinned_ids
                            for event, _, _ in desired)
        if phrase_pinned:
            # The anchor fixes this phrase: keep its events where they are.
            candidates = [0.0]
        else:
            candidates.extend(shift * strength for _, _, shift in desired)
            if len(desired) > 1:
                candidates.append(sum(shift for _, _, shift in desired)
                                  / len(desired) * strength)
        rows = []
        for shift in sorted(set(round(value, 7) for value in candidates)):
            loss = sum(_duration_loss(event, shift, event.get("boundary_end_s"))
                       for event, _, _ in desired)
            if preserve_duration and loss > 1e-9:
                continue
            blocks = [_apply(event, shift, "vowel" if event.get("vowel_s") is not None else "start")
                      for event, _, _ in desired]
            overlap = any(
                any(row.get("start_s", 0) < float(zone.get("end_s", zone.get("end", 0))) and
                    row.get("end_s", row.get("start_s", 0)) > float(zone.get("start_s", zone.get("start", 0)))
                    for zone in protected_ranges)
                for row in blocks
            )
            if overlap:
                continue
            error = sum(abs(target - (
                block.get("vowel_s", block["start_s"]) if event.get("vowel_s") is not None
                else block["start_s"])) for (event, target, _), block in zip(desired, blocks))
            rows.append({
                "phrase": phrase_id,
                "kind": "leave" if shift == 0 else ("block" if abs(shift) == abs(desired[0][2] * strength) and len(desired) > 1 else "event"),
                "shift_s": shift,
                "timing_error_s": round(error, 7),
                "duration_loss_s": round(loss, 7),
                "events": blocks,
            })
        if not rows:
            issues.append({"code": "no_safe_alternative", "phrase": phrase_id,
                           "next_action": "protect the tail/pause or lower strength"})
            continue
        rows.sort(key=lambda row: (row["timing_error_s"], row["duration_loss_s"],
                                   abs(row["shift_s"])))
        chosen = rows[0]
        selected.extend(chosen["events"])
        alternatives.append({"phrase": phrase_id, "chosen": chosen,
                             "alternatives": rows[1:]})
        if preserve_duration and any(
                _duration_loss(event, shift, event.get("boundary_end_s")) > 1e-9
                for event, _, shift in desired for shift in (max(0.0, desired[0][2]),)):
            issues.append({"code": "duration_preserved_by_smaller_shift",
                           "phrase": phrase_id,
                           "next_action": "audition the smaller shift or leave the phrase"})
    return {
        "schema": "reaper-workbench.timing-phrase-plan/1",
        "selected": selected,
        "alternatives": alternatives,
        "issues": issues,
    }
