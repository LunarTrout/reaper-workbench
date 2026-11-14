"""Event, occurrence, gesture and score records.

Identity and time come from the shared contracts: ``TimeRef``/``Interval`` are
imported from :mod:`reaper_audio.timebase` (never redefined here) and every
identifier is validated against :mod:`reaper_audio.context.ids`.  A record that
violates a cross-field invariant — a ``prf_`` event without its ``occ_``, a
``note`` without pitch and duration, a chord of one note — fails with
``ContractValidationError(code="event_invariant")`` rather than being repaired
silently.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from ..context.errors import ContractError, ContractValidationError
from ..context.ids import parse_id
from ..timebase import Interval, TimeRef

EVENT_INVARIANT_CODE = "event_invariant"

CONFIDENCE_BASES = frozenset({"interval", "rank", "agreement", "none"})
PERFORMANCE_EVENT_KINDS = frozenset({"pick_attack", "drum_strike", "note", "chord"})
SCORE_ORIGINS = frozenset({"project_midi", "transcription", "composed"})
GESTURE_MEMBER_PREFIXES = frozenset({"evt", "prf"})

_JSON_DUMPS_OPTIONS: dict[str, Any] = {
    "sort_keys": True,
    "ensure_ascii": False,
    "allow_nan": False,
}


class SignalKind(StrEnum):
    """What a measured signal event represents."""

    onset = "onset"
    transient = "transient"
    spike = "spike"
    clip_run = "clip_run"
    silence = "silence"
    decay = "decay"


class EventStatus(StrEnum):
    """Review state shared by signal and performance events."""

    candidate = "candidate"
    transcribed = "transcribed"
    reviewed = "reviewed"
    rejected = "rejected"


def _fail(message: str, *, field: str, next_action: str | None = None) -> NoReturn:
    error = ContractValidationError(message, field=field, next_action=next_action)
    error.code = EVENT_INVARIANT_CODE
    raise error


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field} must be a non-empty string", field=field)
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{field} must be a number", field=field)
    number = float(value)
    if not math.isfinite(number):
        _fail(f"{field} must be finite", field=field)
    return number


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{field} must be an integer", field=field)
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{field} must be a boolean", field=field)
    return value


def _enum(enum_type: type[StrEnum], value: Any, field: str) -> Any:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (ValueError, TypeError):
        allowed = ", ".join(str(member) for member in enum_type)
        _fail(f"{field} must be one of {allowed}, got {value!r}", field=field)


def _require_id(value: Any, prefix: str, field: str) -> str:
    """Require a registered identifier whose prefix matches ``prefix``."""
    try:
        found, _body = parse_id(value)
    except ContractError as exc:
        _fail(
            f"{field} must be a valid {prefix}_ identifier: {exc.message}",
            field=field,
            next_action=f"use {prefix}_",
        )
    if found != prefix:
        _fail(
            f"{field} must use prefix {prefix!r}, got {found!r}",
            field=field,
            next_action=f"use {prefix}_",
        )
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(f"{field} must be a mapping", field=field)
    return value


def _required(data: Mapping[str, Any], key: str) -> Any:
    if key not in data:
        _fail(f"missing required field {key!r}", field=key)
    return data[key]


def _tuple_of_text(value: Any, field: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        _fail(f"{field} must be a sequence of strings", field=field)
    items = tuple(value)
    for item in items:
        _text(item, field)
    return items


class _JsonRecord:
    """Mixin giving every record strict, deterministic JSON."""

    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def to_json(self) -> str:
        """Serialize with ``allow_nan=False``; non-finite values are refused."""
        return json.dumps(self.to_dict(), **_JSON_DUMPS_OPTIONS)


@dataclass(frozen=True, slots=True)
class UsageScope(_JsonRecord):
    """Which uses the evidence behind a record actually licenses."""

    timing: bool = True
    pitch: bool = False
    duration: bool = False
    midi: bool = False
    destructive_cut: bool = False

    def __post_init__(self) -> None:
        for name in ("timing", "pitch", "duration", "midi", "destructive_cut"):
            object.__setattr__(self, name, _boolean(getattr(self, name), name))

    def to_dict(self) -> dict[str, Any]:
        return {
            "timing": self.timing,
            "pitch": self.pitch,
            "duration": self.duration,
            "midi": self.midi,
            "destructive_cut": self.destructive_cut,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "UsageScope") -> "UsageScope":
        if isinstance(data, cls):
            return data
        source = _mapping(data, "usage_scope")
        defaults = cls()
        return cls(
            timing=source.get("timing", defaults.timing),
            pitch=source.get("pitch", defaults.pitch),
            duration=source.get("duration", defaults.duration),
            midi=source.get("midi", defaults.midi),
            destructive_cut=source.get("destructive_cut", defaults.destructive_cut),
        )


@dataclass(frozen=True, slots=True)
class Confidence(_JsonRecord):
    """How strongly a measurement is believed, and against what."""

    basis: str
    value: float | None = None
    calibration_id: str | None = None
    n: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.basis, str) or self.basis not in CONFIDENCE_BASES:
            allowed = ", ".join(sorted(CONFIDENCE_BASES))
            _fail(f"confidence basis must be one of {allowed}", field="basis")
        if self.value is not None:
            value = _finite(self.value, "value")
            if not 0.0 <= value <= 1.0:
                _fail("confidence value must be within 0..1", field="value")
            object.__setattr__(self, "value", value)
        object.__setattr__(
            self, "calibration_id", _optional_text(self.calibration_id, "calibration_id")
        )
        if self.n is not None:
            count = _integer(self.n, "n")
            if count < 0:
                _fail("confidence n must be >= 0", field="n")

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis": self.basis,
            "value": self.value,
            "calibration_id": self.calibration_id,
            "n": self.n,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "Confidence") -> "Confidence":
        if isinstance(data, cls):
            return data
        source = _mapping(data, "confidence")
        return cls(
            basis=_required(source, "basis"),
            value=source.get("value"),
            calibration_id=source.get("calibration_id"),
            n=source.get("n"),
        )


@dataclass(frozen=True, slots=True)
class MethodRef(_JsonRecord):
    """The algorithm that produced a record, pinned by version and params."""

    name: str
    version: str
    params_hash: str | None = None

    def __post_init__(self) -> None:
        _text(self.name, "name")
        _text(self.version, "version")
        object.__setattr__(self, "params_hash", _optional_text(self.params_hash, "params_hash"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "params_hash": self.params_hash,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "MethodRef") -> "MethodRef":
        if isinstance(data, cls):
            return data
        source = _mapping(data, "method")
        return cls(
            name=_required(source, "name"),
            version=_required(source, "version"),
            params_hash=source.get("params_hash"),
        )


@dataclass(frozen=True, slots=True)
class EvidenceRef(_JsonRecord):
    """Pointer to another journal record used as evidence."""

    record_id: str
    record_kind: str

    def __post_init__(self) -> None:
        _text(self.record_id, "record_id")
        _text(self.record_kind, "record_kind")

    def to_dict(self) -> dict[str, Any]:
        return {"record_id": self.record_id, "record_kind": self.record_kind}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "EvidenceRef") -> "EvidenceRef":
        if isinstance(data, cls):
            return data
        source = _mapping(data, "evidence")
        return cls(
            record_id=_required(source, "record_id"),
            record_kind=_required(source, "record_kind"),
        )


def _midi_note(value: Any, field: str) -> int:
    note = _integer(value, field)
    if not 0 <= note <= 127:
        _fail(f"{field} must be within 0..127", field=field)
    return note


@dataclass(frozen=True, slots=True)
class Pitch(_JsonRecord):
    """One pitch hypothesis, with alternatives kept rather than collapsed."""

    midi: int
    cents_offset: float = 0.0
    pitch_class: int | None = None
    alternatives: tuple[tuple[int, float], ...] = ()
    confidence: Confidence | None = None

    def __post_init__(self) -> None:
        midi = _midi_note(self.midi, "midi")
        cents = _finite(self.cents_offset, "cents_offset")
        if abs(cents) > 100.0:
            _fail("cents_offset must stay within +/-100 cents", field="cents_offset")

        pitch_class = self.pitch_class
        if pitch_class is None:
            pitch_class = midi % 12
        else:
            pitch_class = _integer(pitch_class, "pitch_class")
            if not 0 <= pitch_class <= 11:
                _fail("pitch_class must be within 0..11", field="pitch_class")

        alternatives = self.alternatives
        if isinstance(alternatives, str) or not isinstance(alternatives, (list, tuple)):
            _fail("alternatives must be a sequence of (midi, weight) pairs", field="alternatives")
        pairs: list[tuple[int, float]] = []
        for index, pair in enumerate(alternatives):
            if isinstance(pair, str) or not isinstance(pair, (list, tuple)) or len(pair) != 2:
                _fail(
                    f"alternatives[{index}] must be a (midi, weight) pair",
                    field="alternatives",
                )
            pairs.append((_midi_note(pair[0], "alternatives"), _finite(pair[1], "alternatives")))

        if self.confidence is not None and not isinstance(self.confidence, Confidence):
            _fail("confidence must be a Confidence or None", field="confidence")

        object.__setattr__(self, "midi", midi)
        object.__setattr__(self, "cents_offset", cents)
        object.__setattr__(self, "pitch_class", pitch_class)
        object.__setattr__(self, "alternatives", tuple(pairs))

    def to_dict(self) -> dict[str, Any]:
        return {
            "midi": self.midi,
            "cents_offset": self.cents_offset,
            "pitch_class": self.pitch_class,
            "alternatives": [[midi, weight] for midi, weight in self.alternatives],
            "confidence": None if self.confidence is None else self.confidence.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "Pitch") -> "Pitch":
        if isinstance(data, cls):
            return data
        source = _mapping(data, "pitch")
        confidence = source.get("confidence")
        return cls(
            midi=_required(source, "midi"),
            cents_offset=source.get("cents_offset", 0.0),
            pitch_class=source.get("pitch_class"),
            alternatives=tuple(
                tuple(pair) for pair in source.get("alternatives", ())  # type: ignore[misc]
            ),
            confidence=None if confidence is None else Confidence.from_dict(confidence),
        )


@dataclass(frozen=True, slots=True)
class EventDuration(_JsonRecord):
    """Measured extent of an event, separate from its onset interval."""

    start_s: float
    end_s: float
    end_confidence: Confidence | None = None

    def __post_init__(self) -> None:
        start = _finite(self.start_s, "start_s")
        end = _finite(self.end_s, "end_s")
        if end < start:
            _fail("end_s must be >= start_s", field="end_s")
        if self.end_confidence is not None and not isinstance(self.end_confidence, Confidence):
            _fail("end_confidence must be a Confidence or None", field="end_confidence")
        object.__setattr__(self, "start_s", start)
        object.__setattr__(self, "end_s", end)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_s": self.start_s,
            "end_s": self.end_s,
            "end_confidence": (
                None if self.end_confidence is None else self.end_confidence.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "EventDuration") -> "EventDuration":
        if isinstance(data, cls):
            return data
        source = _mapping(data, "duration")
        confidence = source.get("end_confidence")
        return cls(
            start_s=_required(source, "start_s"),
            end_s=_required(source, "end_s"),
            end_confidence=None if confidence is None else Confidence.from_dict(confidence),
        )


def _interval(value: Any, field: str) -> Interval:
    if isinstance(value, Interval):
        return value
    if isinstance(value, Mapping):
        try:
            return Interval(
                start=cast(TimeRef, value.get("start")),
                end=value.get("end"),
                anchor=value.get("anchor", "window_bounds"),
            )
        except ContractError as exc:
            _fail(f"{field} is not a valid interval: {exc.message}", field=field)
    _fail(f"{field} must be an Interval or an interval mapping", field=field)


def _evidence(value: Any, field: str) -> tuple[EvidenceRef, ...]:
    if isinstance(value, EvidenceRef):
        return (value,)
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        _fail(f"{field} must be a sequence of EvidenceRef", field=field)
    items: list[EvidenceRef] = []
    for item in value:
        if isinstance(item, EvidenceRef):
            items.append(item)
        elif isinstance(item, Mapping):
            items.append(EvidenceRef.from_dict(item))
        else:
            _fail(f"{field} entries must be EvidenceRef", field=field)
    return tuple(items)


@dataclass(frozen=True, slots=True)
class SignalEvent(_JsonRecord):
    """A measured signal feature on one asset."""

    event_id: str
    asset_id: str
    interval: Interval
    kind: SignalKind
    strength: float
    method: MethodRef
    status: EventStatus = EventStatus.candidate
    occurrence_id: str | None = None
    evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        _require_id(self.event_id, "evt", "event_id")
        _require_id(self.asset_id, "ast_sha256", "asset_id")
        if not isinstance(self.interval, Interval):
            _fail("interval must be an Interval", field="interval")
        object.__setattr__(self, "kind", _enum(SignalKind, self.kind, "kind"))
        object.__setattr__(self, "strength", _finite(self.strength, "strength"))
        if not isinstance(self.method, MethodRef):
            _fail("method must be a MethodRef", field="method")
        object.__setattr__(self, "status", _enum(EventStatus, self.status, "status"))
        if self.occurrence_id is not None:
            _require_id(self.occurrence_id, "occ", "occurrence_id")
        object.__setattr__(self, "evidence", _evidence(self.evidence, "evidence"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "asset_id": self.asset_id,
            "interval": self.interval.to_dict(),
            "kind": str(self.kind),
            "strength": self.strength,
            "method": self.method.to_dict(),
            "status": str(self.status),
            "occurrence_id": self.occurrence_id,
            "evidence": [item.to_dict() for item in self.evidence],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "SignalEvent") -> "SignalEvent":
        if isinstance(data, cls):
            return data
        source = _mapping(data, "signal_event")
        return cls(
            event_id=_required(source, "event_id"),
            asset_id=_required(source, "asset_id"),
            interval=_interval(_required(source, "interval"), "interval"),
            kind=_required(source, "kind"),
            strength=_required(source, "strength"),
            method=MethodRef.from_dict(_required(source, "method")),
            status=source.get("status", EventStatus.candidate),
            occurrence_id=source.get("occurrence_id"),
            evidence=_evidence(source.get("evidence", ()), "evidence"),
        )


@dataclass(frozen=True, slots=True)
class PerformanceOccurrence(_JsonRecord):
    """One realised pass of a take through an item (loop iteration included)."""

    occurrence_id: str
    project_instance_id: str
    item_guid: str
    take_guid: str
    loop_iteration: int = 0
    source_interval: Interval | None = None
    status: str = "partial"
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_id(self.occurrence_id, "occ", "occurrence_id")
        _text(self.project_instance_id, "project_instance_id")
        _text(self.item_guid, "item_guid")
        _text(self.take_guid, "take_guid")
        iteration = _integer(self.loop_iteration, "loop_iteration")
        if iteration < 0:
            _fail("loop_iteration must be >= 0", field="loop_iteration")
        if self.source_interval is not None and not isinstance(self.source_interval, Interval):
            _fail("source_interval must be an Interval or None", field="source_interval")
        _text(self.status, "status")
        object.__setattr__(self, "limitations", _tuple_of_text(self.limitations, "limitations"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "occurrence_id": self.occurrence_id,
            "project_instance_id": self.project_instance_id,
            "item_guid": self.item_guid,
            "take_guid": self.take_guid,
            "loop_iteration": self.loop_iteration,
            "source_interval": (
                None if self.source_interval is None else self.source_interval.to_dict()
            ),
            "status": self.status,
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any] | "PerformanceOccurrence"
    ) -> "PerformanceOccurrence":
        if isinstance(data, cls):
            return data
        source = _mapping(data, "occurrence")
        source_interval = source.get("source_interval")
        return cls(
            occurrence_id=_required(source, "occurrence_id"),
            project_instance_id=_required(source, "project_instance_id"),
            item_guid=_required(source, "item_guid"),
            take_guid=_required(source, "take_guid"),
            loop_iteration=source.get("loop_iteration", 0),
            source_interval=(
                None if source_interval is None else _interval(source_interval, "source_interval")
            ),
            status=source.get("status", "partial"),
            limitations=source.get("limitations", ()),
        )


@dataclass(frozen=True, slots=True)
class PerformanceEvent(_JsonRecord):
    """What was performed at one point of an occurrence.

    ``interval`` is the performed position; ``duration`` is the measured
    extent, kept apart so a cut never has to be inferred from a window bound.
    """

    event_id: str
    occurrence_id: str
    kind: str
    interval: Interval
    method: MethodRef
    usage_scope: UsageScope
    pitch: Pitch | None = None
    chord: tuple[int, ...] | None = None
    duration: EventDuration | None = None
    status: EventStatus = EventStatus.candidate
    confidence: Confidence | None = None
    alternatives: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_id(self.event_id, "prf", "event_id")
        _require_id(self.occurrence_id, "occ", "occurrence_id")
        if not isinstance(self.kind, str) or self.kind not in PERFORMANCE_EVENT_KINDS:
            allowed = ", ".join(sorted(PERFORMANCE_EVENT_KINDS))
            _fail(f"kind must be one of {allowed}, got {self.kind!r}", field="kind")
        if not isinstance(self.interval, Interval):
            _fail("interval must be an Interval", field="interval")
        if not isinstance(self.method, MethodRef):
            _fail("method must be a MethodRef", field="method")
        if not isinstance(self.usage_scope, UsageScope):
            _fail("usage_scope must be a UsageScope", field="usage_scope")

        if self.pitch is not None and not isinstance(self.pitch, Pitch):
            _fail("pitch must be a Pitch or None", field="pitch")
        if self.duration is not None and not isinstance(self.duration, EventDuration):
            _fail("duration must be an EventDuration or None", field="duration")
        if self.confidence is not None and not isinstance(self.confidence, Confidence):
            _fail("confidence must be a Confidence or None", field="confidence")

        chord = self.chord
        if chord is not None:
            if isinstance(chord, str) or not isinstance(chord, (list, tuple)):
                _fail("chord must be a sequence of MIDI notes", field="chord")
            notes = tuple(_midi_note(note, "chord") for note in chord)
            if len(notes) < 2:
                _fail("chord must contain at least two MIDI notes", field="chord")
            object.__setattr__(self, "chord", notes)

        if self.kind == "note":
            if self.pitch is None:
                _fail("kind 'note' requires a pitch", field="pitch", next_action="set_pitch")
            if self.duration is None:
                _fail(
                    "kind 'note' requires a duration",
                    field="duration",
                    next_action="set_duration",
                )
        if self.kind == "chord" and self.chord is None:
            _fail("kind 'chord' requires a chord", field="chord", next_action="set_chord")
        if self.pitch is not None and self.chord is not None:
            _fail(
                "a performance event carries either pitch or chord, not both",
                field="chord",
            )

        object.__setattr__(self, "status", _enum(EventStatus, self.status, "status"))
        object.__setattr__(self, "alternatives", _tuple_of_text(self.alternatives, "alternatives"))
        evidence_ids = _tuple_of_text(self.evidence_ids, "evidence_ids")
        for evidence_id in evidence_ids:
            try:
                parse_id(evidence_id)
            except ContractError as exc:
                _fail(
                    f"evidence_ids must be valid ids: {exc.message}", field="evidence_ids"
                )
        object.__setattr__(self, "evidence_ids", evidence_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "occurrence_id": self.occurrence_id,
            "kind": self.kind,
            "interval": self.interval.to_dict(),
            "method": self.method.to_dict(),
            "usage_scope": self.usage_scope.to_dict(),
            "pitch": None if self.pitch is None else self.pitch.to_dict(),
            "chord": None if self.chord is None else list(self.chord),
            "duration": None if self.duration is None else self.duration.to_dict(),
            "status": str(self.status),
            "confidence": None if self.confidence is None else self.confidence.to_dict(),
            "alternatives": list(self.alternatives),
            "evidence_ids": list(self.evidence_ids),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "PerformanceEvent") -> "PerformanceEvent":
        if isinstance(data, cls):
            return data
        source = _mapping(data, "performance_event")
        pitch = source.get("pitch")
        duration = source.get("duration")
        confidence = source.get("confidence")
        chord = source.get("chord")
        return cls(
            event_id=_required(source, "event_id"),
            occurrence_id=_required(source, "occurrence_id"),
            kind=_required(source, "kind"),
            interval=_interval(_required(source, "interval"), "interval"),
            method=MethodRef.from_dict(_required(source, "method")),
            usage_scope=UsageScope.from_dict(_required(source, "usage_scope")),
            pitch=None if pitch is None else Pitch.from_dict(pitch),
            chord=None if chord is None else tuple(chord),
            duration=None if duration is None else EventDuration.from_dict(duration),
            status=source.get("status", EventStatus.candidate),
            confidence=None if confidence is None else Confidence.from_dict(confidence),
            alternatives=source.get("alternatives", ()),
            evidence_ids=source.get("evidence_ids", ()),
        )


@dataclass(frozen=True, slots=True)
class PerceivedGesture(_JsonRecord):
    """A heard gesture over one or more signal or performance events."""

    gesture_id: str
    member_event_ids: tuple[str, ...]
    kind: str
    interval: Interval
    confidence: Confidence | None = None

    def __post_init__(self) -> None:
        _require_id(self.gesture_id, "peg", "gesture_id")
        members = _tuple_of_text(self.member_event_ids, "member_event_ids")
        if not members:
            _fail("gesture requires at least one member event", field="member_event_ids")
        if len(set(members)) != len(members):
            _fail("member_event_ids must be unique", field="member_event_ids")
        for member in members:
            try:
                prefix, _body = parse_id(member)
            except ContractError as exc:
                _fail(
                    f"member_event_ids must be valid ids: {exc.message}",
                    field="member_event_ids",
                )
            if prefix not in GESTURE_MEMBER_PREFIXES:
                _fail(
                    "member_event_ids must reference evt_ or prf_ events",
                    field="member_event_ids",
                    next_action="use evt_ or prf_",
                )
        _text(self.kind, "kind")
        if not isinstance(self.interval, Interval):
            _fail("interval must be an Interval", field="interval")
        if self.confidence is not None and not isinstance(self.confidence, Confidence):
            _fail("confidence must be a Confidence or None", field="confidence")
        object.__setattr__(self, "member_event_ids", members)

    def to_dict(self) -> dict[str, Any]:
        return {
            "gesture_id": self.gesture_id,
            "member_event_ids": list(self.member_event_ids),
            "kind": self.kind,
            "interval": self.interval.to_dict(),
            "confidence": None if self.confidence is None else self.confidence.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "PerceivedGesture") -> "PerceivedGesture":
        if isinstance(data, cls):
            return data
        source = _mapping(data, "gesture")
        confidence = source.get("confidence")
        return cls(
            gesture_id=_required(source, "gesture_id"),
            member_event_ids=source.get("member_event_ids", ()),
            kind=_required(source, "kind"),
            interval=_interval(_required(source, "interval"), "interval"),
            confidence=None if confidence is None else Confidence.from_dict(confidence),
        )


@dataclass(frozen=True, slots=True)
class ScoreObject(_JsonRecord):
    """One transcribed or composed score event on a take."""

    score_id: str
    take_guid: str
    ppq: int
    pitch: int
    channel: int
    duration_ppq: int
    method: MethodRef
    origin: str = "project_midi"

    def __post_init__(self) -> None:
        _require_id(self.score_id, "sco", "score_id")
        _text(self.take_guid, "take_guid")
        position = _integer(self.ppq, "ppq")
        if position < 0:
            _fail("ppq must be >= 0", field="ppq")
        note = _midi_note(self.pitch, "pitch")
        channel = _integer(self.channel, "channel")
        if not 0 <= channel <= 15:
            _fail("channel must be within 0..15", field="channel")
        length = _integer(self.duration_ppq, "duration_ppq")
        if length < 0:
            _fail("duration_ppq must be >= 0", field="duration_ppq")
        if not isinstance(self.method, MethodRef):
            _fail("method must be a MethodRef", field="method")
        if not isinstance(self.origin, str) or self.origin not in SCORE_ORIGINS:
            allowed = ", ".join(sorted(SCORE_ORIGINS))
            _fail(f"origin must be one of {allowed}", field="origin")
        object.__setattr__(self, "ppq", position)
        object.__setattr__(self, "pitch", note)
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "duration_ppq", length)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score_id": self.score_id,
            "take_guid": self.take_guid,
            "ppq": self.ppq,
            "pitch": self.pitch,
            "channel": self.channel,
            "duration_ppq": self.duration_ppq,
            "method": self.method.to_dict(),
            "origin": self.origin,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "ScoreObject") -> "ScoreObject":
        if isinstance(data, cls):
            return data
        source = _mapping(data, "score")
        return cls(
            score_id=_required(source, "score_id"),
            take_guid=_required(source, "take_guid"),
            ppq=_required(source, "ppq"),
            pitch=_required(source, "pitch"),
            channel=_required(source, "channel"),
            duration_ppq=_required(source, "duration_ppq"),
            method=MethodRef.from_dict(_required(source, "method")),
            origin=source.get("origin", "project_midi"),
        )


__all__ = [
    "EVENT_INVARIANT_CODE",
    "CONFIDENCE_BASES",
    "PERFORMANCE_EVENT_KINDS",
    "SCORE_ORIGINS",
    "GESTURE_MEMBER_PREFIXES",
    "SignalKind",
    "EventStatus",
    "UsageScope",
    "Confidence",
    "MethodRef",
    "EvidenceRef",
    "Pitch",
    "EventDuration",
    "SignalEvent",
    "PerformanceOccurrence",
    "PerformanceEvent",
    "PerceivedGesture",
    "ScoreObject",
    "TimeRef",
    "Interval",
]
