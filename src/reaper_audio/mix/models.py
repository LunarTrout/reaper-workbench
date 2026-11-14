"""Mix diagnosis contracts: scope, observation, claim, hypothesis, verification.

The chain is fixed and one-directional::

    MixObservation -> MixClaim -> IssueHypothesis -> PostChangeCheck

An observation says what was measured, heard or derived, and on which audio.
A claim turns observations into a statement that is either *detected*
(``mix_detected``) or *inferred* (``mix_inferred``); an inferred claim must
carry at least two live alternatives, because a diagnosis that names one cause
and hides the others is a guess wearing a lab coat.  A hypothesis names a
mechanism and must be falsifiable: either it ships a discriminating test or it
declares itself ``not_testable``.  A post-change check says what should move,
records what did move, and keeps the expected direction next to the measured
value.

Two things this module deliberately does not have:

* a ``quality``/``score`` field on claims — a mix is not a number, and a single
  scalar invites thresholds nobody calibrated;
* severity thresholds — ``Severity`` carries a level, the anchor that justifies
  it and an interval, never a cut-off table.  Thresholds are policy data and
  belong to the layer that owns them.

Wave A keeps this file to types, invariants and serialization.  Measurement,
inference, severity calibration and verification live elsewhere.  ``Stage``,
``ChannelView``, ``SignalWindow`` and ``RevisionRef`` are imported from the
shared context contracts and redefined nowhere: one class per concept.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn

from ..context.audio import ChannelView, SignalWindow, Stage
from ..context.errors import ContractValidationError
from ..context.schema import RevisionRef, validate_revision

#: Code carried by structural mix-contract violations.
MIX_INVARIANT_CODE = "mix_invariant"

#: Code required when a hypothesis is neither tested nor declared untestable.
HYPOTHESIS_NOT_TESTABLE_CODE = "hypothesis_not_testable"

_JSON_DUMPS_OPTIONS: dict[str, Any] = {
    "sort_keys": True,
    "ensure_ascii": False,
    "allow_nan": False,
}

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ID_BODY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ASSET_PREFIX = "ast_sha256_"

OBSERVATION_STATUSES = frozenset(
    {"measured", "derived", "model_observation", "unavailable"}
)
TRACK_ROLES = frozenset({"track", "folder", "bus", "master"})
SECTION_LEVELS = frozenset({"riff", "phrase", "section", "unknown"})
LABEL_SOURCES = frozenset({"human", "marker", "model", "null"})
ROLE_SOURCES = frozenset({"human", "config", "model", "null"})
LEVEL_MODES = frozenset({"absolute", "matched", "unknown"})
CONFIDENCE_BASES = frozenset({"interval", "rank", "agreement", "none"})
SEVERITY_LEVELS = frozenset({"none", "mild", "moderate", "severe"})
SEVERITY_ANCHORS = frozenset({"calibration", "heuristic", "none"})
INTENT_SOURCES = frozenset({"owner", "model", "config"})
EXPECTED_DIRECTIONS = frozenset({"increase", "decrease", "unchanged"})
POST_CHANGE_STATUSES = frozenset(
    {"planned", "applied", "verified", "failed", "inconclusive", "rolled_back"}
)


class IssueClass(StrEnum):
    """The audible problem a claim or hypothesis is about."""

    dry = "dry"
    muddy = "muddy"
    harsh = "harsh"
    masked = "masked"
    weak_attack = "weak_attack"
    boomy = "boomy"
    narrow = "narrow"
    phasey = "phasey"
    distant = "distant"
    crushed = "crushed"
    clipping = "clipping"
    unbalanced = "unbalanced"
    unclear = "unclear"
    other = "other"


class ObservationKind(StrEnum):
    """How an observation came to exist."""

    measurement = "measurement"
    derived = "derived"
    listener = "listener"


class ClaimClass(StrEnum):
    """Whether a claim reports a signal or proposes a cause."""

    mix_detected = "mix_detected"
    mix_inferred = "mix_inferred"


class ClaimStatus(StrEnum):
    """Lifecycle of a claim in the ledger."""

    asserted = "asserted"
    contested = "contested"
    confirmed = "confirmed"
    refuted = "refuted"
    unverified = "unverified"
    retracted = "retracted"


class EvidenceGrade(StrEnum):
    """Durability of the evidence behind a statement.

    ``A`` is a measured DSP result, ``B`` a listener report whose delivery was
    verified, ``C`` a listener report that was not verified, ``D`` a value
    derived from other measurements, ``E`` an annotation and ``F`` nothing at
    all.  The ladder only ever runs downward: evidence never improves by being
    restated.
    """

    A_measured_dsp = "A_measured_dsp"
    B_listener_verified = "B_listener_verified"
    C_listener_unverified = "C_listener_unverified"
    D_derived = "D_derived"
    E_annotation = "E_annotation"
    F_unsupported = "F_unsupported"


class CauseFamily(StrEnum):
    """The family a causal hypothesis belongs to, never a specific cause."""

    performance = "performance"
    arrangement = "arrangement"
    source_tone = "source_tone"
    masking = "masking"
    eq = "eq"
    compression = "compression"
    saturation = "saturation"
    phase = "phase"
    pan = "pan"
    reverb = "reverb"
    routing = "routing"
    automation = "automation"
    monitoring = "monitoring"
    reference_mismatch = "reference_mismatch"
    unknown = "unknown"


# --------------------------------------------------------------------------- #
# scalar helpers
# --------------------------------------------------------------------------- #


def _fail(
    message: str,
    *,
    field: str,
    next_action: str | None = None,
    code: str | None = None,
) -> NoReturn:
    error = ContractValidationError(message, field=field, next_action=next_action)
    error.code = code or MIX_INVARIANT_CODE
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


def _optional_finite(value: Any, field: str) -> float | None:
    if value is None:
        return None
    return _finite(value, field)


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{field} must be a bool", field=field)
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{field} must be an integer", field=field)
    return value


def _enum(value: Any, enum_cls: Any, field: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        allowed = ", ".join(str(member) for member in enum_cls)
        _fail(f"{field} must be one of {allowed}, got {value!r}", field=field)


def _choice(value: Any, allowed: frozenset[str], field: str) -> str:
    text = _text(value, field)
    if text not in allowed:
        _fail(
            f"{field} must be one of {', '.join(sorted(allowed))}, got {text!r}",
            field=field,
        )
    return text


def _hex64(value: Any, field: str) -> str:
    text = _text(value, field).strip().lower()
    if not _HEX64.match(text):
        _fail(f"{field} must be 64 lowercase hex characters", field=field)
    return text


def _optional_hex64(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _hex64(value, field)


def _prefixed(value: Any, prefix: str, field: str) -> str:
    """Require ``prefix_<body>``; the body's shape is the minter's business."""
    text = _text(value, field).strip()
    head, separator, body = text.partition("_")
    if separator != "_" or head != prefix or not _ID_BODY.match(body):
        _fail(
            f"{field} must be a {prefix}_ identifier",
            field=field,
            next_action=f"use {prefix}_",
        )
    return text


def _text_tuple(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple)):
        _fail(f"{field} must be a sequence of strings", field=field)
    return tuple(_text(item, f"{field}[{index}]") for index, item in enumerate(value))


def _unique(
    items: tuple[Any, ...], field: str, *, attr: str | None = None
) -> tuple[Any, ...]:
    seen: set[Any] = set()
    for item in items:
        key = item if attr is None else getattr(item, attr)
        if key in seen:
            _fail(f"{field} must be unique", field=field)
        seen.add(key)
    return items


def _ordered_pair(value: Any, field: str) -> tuple[float, float]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, (list, tuple))
        or len(value) != 2
    ):
        _fail(f"{field} must be a (low, high) pair", field=field)
    low = _finite(value[0], f"{field}[0]")
    high = _finite(value[1], f"{field}[1]")
    if low > high:
        _fail(f"{field} low must be <= high", field=field)
    return (low, high)


def _record(value: Any, cls: type, field: str) -> Any:
    if isinstance(value, cls):
        return value
    if isinstance(value, Mapping):
        return cls.from_dict(value)
    _fail(f"{field} must be a {cls.__name__}", field=field)


def _optional_record(value: Any, cls: type, field: str) -> Any:
    if value is None:
        return None
    return _record(value, cls, field)


def _records(value: Any, cls: type, field: str) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (cls, Mapping)):
        value = (value,)
    elif isinstance(value, str) or not isinstance(value, (list, tuple)):
        _fail(f"{field} must be a sequence of {cls.__name__}", field=field)
    return tuple(_record(item, cls, field) for item in value)


def _window_value(value: Any, field: str) -> SignalWindow | None:
    if value is None:
        return None
    if isinstance(value, SignalWindow):
        return value
    if isinstance(value, Mapping):
        return SignalWindow.from_dict(value)
    _fail(f"{field} must be a SignalWindow or None", field=field)


def _required_window(value: Any, field: str) -> SignalWindow:
    if value is None:
        _fail(
            "a mix scope requires the signal window it covers",
            field=field,
            next_action="capture_window",
        )
    window = _window_value(value, field)
    assert window is not None
    return window


class _JsonRecord:
    """Mixin giving every mix record strict, deterministic JSON."""

    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def to_json(self) -> str:
        """Serialize with ``allow_nan=False``; non-finite values are refused."""
        return json.dumps(self.to_dict(), **_JSON_DUMPS_OPTIONS)


# --------------------------------------------------------------------------- #
# scope
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SectionRef(_JsonRecord):
    """The musical stretch a scope covers, with honest label provenance."""

    section_id: str
    label: str | None = None
    label_source: str = "null"
    level: str = "unknown"

    def __post_init__(self) -> None:
        object.__setattr__(self, "section_id", _text(self.section_id, "section_id"))
        object.__setattr__(self, "label", _optional_text(self.label, "label"))
        source = self.label_source
        if source is None:
            source = "null"
        object.__setattr__(self, "label_source", _choice(source, LABEL_SOURCES, "label_source"))
        object.__setattr__(self, "level", _choice(self.level, SECTION_LEVELS, "level"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "label": self.label,
            "label_source": self.label_source,
            "level": self.level,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "SectionRef") -> "SectionRef":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail("section must be a mapping or SectionRef", field="section")
        return cls(
            section_id=data.get("section_id", ""),
            label=data.get("label"),
            label_source=data.get("label_source", "null"),
            level=data.get("level", "unknown"),
        )


@dataclass(frozen=True, slots=True)
class RoleRef(_JsonRecord):
    """A track's role in the mix, and where that role came from."""

    value: str
    source: str = "null"
    confidence: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _text(self.value, "value"))
        source = self.source
        if source is None:
            source = "null"
        object.__setattr__(self, "source", _choice(source, ROLE_SOURCES, "source"))
        confidence = _optional_finite(self.confidence, "confidence")
        if confidence is not None and not 0.0 <= confidence <= 1.0:
            _fail("confidence must be within 0..1", field="confidence")
        object.__setattr__(self, "confidence", confidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "source": self.source,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "RoleRef") -> "RoleRef":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail("role must be a mapping or RoleRef", field="role")
        return cls(
            value=data.get("value", ""),
            source=data.get("source", "null"),
            confidence=data.get("confidence"),
        )


@dataclass(frozen=True, slots=True)
class ScopeEntity(_JsonRecord):
    """One object a scope is about: a track, a folder, a bus or the master."""

    entity_id: str
    track_role: str
    role: RoleRef | None = None
    asset_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "entity_id", _text(self.entity_id, "entity_id"))
        object.__setattr__(
            self, "track_role", _choice(self.track_role, TRACK_ROLES, "track_role")
        )
        object.__setattr__(self, "role", _optional_record(self.role, RoleRef, "role"))
        asset_id = self.asset_id
        if asset_id is not None:
            asset_id = _text(asset_id, "asset_id")
            if not asset_id.startswith(_ASSET_PREFIX):
                _fail("asset_id must start with ast_sha256_", field="asset_id")
        object.__setattr__(self, "asset_id", asset_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "track_role": self.track_role,
            "role": None if self.role is None else self.role.to_dict(),
            "asset_id": self.asset_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "ScopeEntity") -> "ScopeEntity":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail("entity must be a mapping or ScopeEntity", field="entities")
        return cls(
            entity_id=data.get("entity_id", ""),
            track_role=data.get("track_role", ""),
            role=data.get("role"),
            asset_id=data.get("asset_id"),
        )


@dataclass(frozen=True, slots=True)
class SignalPathRefs(_JsonRecord):
    """Stable pointers to the routing a scope was observed through."""

    track_guids: tuple[str, ...] = ()
    send_ids: tuple[str, ...] = ()
    fx_guids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "track_guids", _text_tuple(self.track_guids, "track_guids")
        )
        object.__setattr__(self, "send_ids", _text_tuple(self.send_ids, "send_ids"))
        object.__setattr__(self, "fx_guids", _text_tuple(self.fx_guids, "fx_guids"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_guids": list(self.track_guids),
            "send_ids": list(self.send_ids),
            "fx_guids": list(self.fx_guids),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "SignalPathRefs") -> "SignalPathRefs":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail(
                "signal_path_refs must be a mapping or SignalPathRefs",
                field="signal_path_refs",
            )
        return cls(
            track_guids=data.get("track_guids", ()),
            send_ids=data.get("send_ids", ()),
            fx_guids=data.get("fx_guids", ()),
        )


@dataclass(frozen=True, slots=True)
class LevelReference(_JsonRecord):
    """What the observed level was judged against.

    ``matched`` means a reference was gain-matched to this scope, so the match
    window is not optional: without it "matched" names no comparison at all.
    """

    mode: str = "unknown"
    gain_db: float | None = None
    match_window: SignalWindow | None = None
    method: str | None = None
    absolute_lufs: float | None = None

    def __post_init__(self) -> None:
        mode = _choice(self.mode, LEVEL_MODES, "mode")
        gain_db = _optional_finite(self.gain_db, "gain_db")
        match_window = _window_value(self.match_window, "match_window")
        method = _optional_text(self.method, "method")
        absolute_lufs = _optional_finite(self.absolute_lufs, "absolute_lufs")
        if mode == "matched" and match_window is None:
            _fail(
                "mode 'matched' requires the match_window it was matched over",
                field="match_window",
                next_action="provide_match_window",
            )
        if mode == "absolute" and absolute_lufs is None:
            _fail(
                "mode 'absolute' requires absolute_lufs",
                field="absolute_lufs",
                next_action="measure_program_loudness",
            )
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "gain_db", gain_db)
        object.__setattr__(self, "match_window", match_window)
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "absolute_lufs", absolute_lufs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "gain_db": self.gain_db,
            "match_window": (
                None if self.match_window is None else self.match_window.to_dict()
            ),
            "method": self.method,
            "absolute_lufs": self.absolute_lufs,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "LevelReference") -> "LevelReference":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail(
                "level_reference must be a mapping or LevelReference",
                field="level_reference",
            )
        return cls(
            mode=data.get("mode", "unknown"),
            gain_db=data.get("gain_db"),
            match_window=data.get("match_window"),
            method=data.get("method"),
            absolute_lufs=data.get("absolute_lufs"),
        )


@dataclass(frozen=True, slots=True)
class MixScope(_JsonRecord):
    """What a statement is about: objects, stage, window, channels, level.

    A scope without a window or a channel view is not evidence, it is a mood.
    ``stage_pair`` exists only for comparisons and must name two different
    stages; ``stage`` remains the primary stage of the scope.
    """

    scope_id: str
    project_id: str
    revision: RevisionRef
    entities: tuple[ScopeEntity, ...]
    stage: Stage
    window: SignalWindow
    channel_view: ChannelView
    level_reference: LevelReference
    signal_path_refs: SignalPathRefs = SignalPathRefs()
    stage_pair: tuple[Stage, Stage] | None = None
    section: SectionRef | None = None

    def __post_init__(self) -> None:
        scope_id = _prefixed(self.scope_id, "sc", "scope_id")
        project_id = _text(self.project_id, "project_id")
        revision = validate_revision(self.revision)

        entities = _records(self.entities, ScopeEntity, "entities")
        if not entities:
            _fail(
                "a mix scope requires at least one entity",
                field="entities",
                next_action="name_track_bus_or_master",
            )
        _unique(entities, "entities", attr="entity_id")

        stage = _enum(self.stage, Stage, "stage")
        window = _required_window(self.window, "window")
        channel_view = _enum(self.channel_view, ChannelView, "channel_view")
        level_reference = _record(
            self.level_reference, LevelReference, "level_reference"
        )
        signal_path_refs = _record(
            self.signal_path_refs, SignalPathRefs, "signal_path_refs"
        )

        pair = self.stage_pair
        if pair is not None:
            if isinstance(pair, Stage) or isinstance(pair, str) or not isinstance(
                pair, (list, tuple)
            ):
                _fail(
                    "stage_pair must be a pair of two stages",
                    field="stage_pair",
                    next_action="use_two_stages",
                )
            if len(pair) != 2:
                _fail(
                    "stage_pair must hold exactly two stages",
                    field="stage_pair",
                    next_action="use_two_stages",
                )
            first = _enum(pair[0], Stage, "stage_pair[0]")
            second = _enum(pair[1], Stage, "stage_pair[1]")
            if first is second:
                _fail(
                    "stage_pair must compare two different stages",
                    field="stage_pair",
                    next_action="choose_two_stages",
                )
            pair = (first, second)

        section = self.section
        if section is not None:
            section = _record(section, SectionRef, "section")

        object.__setattr__(self, "scope_id", scope_id)
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "entities", entities)
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "window", window)
        object.__setattr__(self, "channel_view", channel_view)
        object.__setattr__(self, "level_reference", level_reference)
        object.__setattr__(self, "signal_path_refs", signal_path_refs)
        object.__setattr__(self, "stage_pair", pair)
        object.__setattr__(self, "section", section)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "project_id": self.project_id,
            "revision": self.revision.to_dict(),
            "entities": [entity.to_dict() for entity in self.entities],
            "stage": str(self.stage),
            "stage_pair": (
                None
                if self.stage_pair is None
                else [str(self.stage_pair[0]), str(self.stage_pair[1])]
            ),
            "window": self.window.to_dict(),
            "channel_view": str(self.channel_view),
            "level_reference": self.level_reference.to_dict(),
            "signal_path_refs": self.signal_path_refs.to_dict(),
            "section": None if self.section is None else self.section.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "MixScope") -> "MixScope":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail("scope must be a mapping or MixScope", field="scope")
        return cls(
            scope_id=data.get("scope_id", ""),
            project_id=data.get("project_id", ""),
            revision=data.get("revision", {}),
            entities=data.get("entities", ()),
            stage=data.get("stage", ""),
            window=data.get("window"),
            channel_view=data.get("channel_view", ""),
            level_reference=data.get("level_reference", {}),
            signal_path_refs=data.get("signal_path_refs", {}),
            stage_pair=data.get("stage_pair"),
            section=data.get("section"),
        )


# --------------------------------------------------------------------------- #
# measurements and evidence
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MixMethod(_JsonRecord):
    """The algorithm behind a reading, pinned by name and version."""

    name: str
    version: str
    params_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "name"))
        object.__setattr__(self, "version", _text(self.version, "version"))
        object.__setattr__(
            self, "params_hash", _optional_text(self.params_hash, "params_hash")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "params_hash": self.params_hash,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "MixMethod") -> "MixMethod":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail("method must be a mapping or MixMethod", field="method")
        return cls(
            name=data.get("name", ""),
            version=data.get("version", ""),
            params_hash=data.get("params_hash"),
        )


@dataclass(frozen=True, slots=True)
class MetricReading(_JsonRecord):
    """One number with its unit, method, coverage and uncertainty."""

    name: str
    family: str
    value: float
    unit: str
    method: MixMethod
    coverage: float | None = None
    uncertainty: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "name"))
        object.__setattr__(self, "family", _text(self.family, "family"))
        object.__setattr__(self, "value", _finite(self.value, "value"))
        object.__setattr__(self, "unit", _text(self.unit, "unit"))
        object.__setattr__(self, "method", _record(self.method, MixMethod, "method"))
        coverage = _optional_finite(self.coverage, "coverage")
        if coverage is not None and not 0.0 <= coverage <= 1.0:
            _fail("coverage must be within 0..1", field="coverage")
        uncertainty = _optional_finite(self.uncertainty, "uncertainty")
        if uncertainty is not None and uncertainty < 0:
            _fail("uncertainty must be >= 0", field="uncertainty")
        object.__setattr__(self, "coverage", coverage)
        object.__setattr__(self, "uncertainty", uncertainty)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "family": self.family,
            "value": self.value,
            "unit": self.unit,
            "method": self.method.to_dict(),
            "coverage": self.coverage,
            "uncertainty": self.uncertainty,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "MetricReading") -> "MetricReading":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail("metric must be a mapping or MetricReading", field="metric")
        return cls(
            name=data.get("name", ""),
            family=data.get("family", ""),
            value=data.get("value"),
            unit=data.get("unit", ""),
            method=data.get("method", {}),
            coverage=data.get("coverage"),
            uncertainty=data.get("uncertainty"),
        )


@dataclass(frozen=True, slots=True)
class AssetRef(_JsonRecord):
    """A pointer to one content-addressed asset, with its digest."""

    asset_id: str
    sha256: str
    capture_id: str | None = None
    delivery_ref: str | None = None

    def __post_init__(self) -> None:
        sha256 = _hex64(self.sha256, "sha256")
        asset_id = _text(self.asset_id, "asset_id").strip()
        if not asset_id.startswith(_ASSET_PREFIX):
            _fail("asset_id must start with ast_sha256_", field="asset_id")
        body = asset_id[len(_ASSET_PREFIX) :].lower()
        if not _HEX64.match(body):
            _fail("asset_id body must be 64 hex characters", field="asset_id")
        if body != sha256:
            _fail(
                "asset_id must embed sha256 of the asset bytes",
                field="asset_id",
            )
        object.__setattr__(self, "asset_id", f"{_ASSET_PREFIX}{body}")
        object.__setattr__(self, "sha256", sha256)
        object.__setattr__(
            self, "capture_id", _optional_text(self.capture_id, "capture_id")
        )
        object.__setattr__(
            self, "delivery_ref", _optional_text(self.delivery_ref, "delivery_ref")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "sha256": self.sha256,
            "capture_id": self.capture_id,
            "delivery_ref": self.delivery_ref,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "AssetRef") -> "AssetRef":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail("asset must be a mapping or AssetRef", field="asset")
        return cls(
            asset_id=data.get("asset_id", ""),
            sha256=data.get("sha256", ""),
            capture_id=data.get("capture_id"),
            delivery_ref=data.get("delivery_ref"),
        )


@dataclass(frozen=True, slots=True)
class ListenerInfo(_JsonRecord):
    """Which listener produced an observation, and what it cannot do."""

    engine: str
    model_revision: str | None = None
    weights_sha256: str | None = None
    prompt_sha256: str | None = None
    context_injected: bool = False
    limits: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "engine", _text(self.engine, "engine"))
        object.__setattr__(
            self, "model_revision", _optional_text(self.model_revision, "model_revision")
        )
        object.__setattr__(
            self, "weights_sha256", _optional_hex64(self.weights_sha256, "weights_sha256")
        )
        object.__setattr__(
            self, "prompt_sha256", _optional_hex64(self.prompt_sha256, "prompt_sha256")
        )
        object.__setattr__(
            self, "context_injected", _bool(self.context_injected, "context_injected")
        )
        object.__setattr__(self, "limits", _text_tuple(self.limits, "limits"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "model_revision": self.model_revision,
            "weights_sha256": self.weights_sha256,
            "prompt_sha256": self.prompt_sha256,
            "context_injected": self.context_injected,
            "limits": list(self.limits),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "ListenerInfo") -> "ListenerInfo":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail("listener must be a mapping or ListenerInfo", field="listener")
        return cls(
            engine=data.get("engine", ""),
            model_revision=data.get("model_revision"),
            weights_sha256=data.get("weights_sha256"),
            prompt_sha256=data.get("prompt_sha256"),
            context_injected=data.get("context_injected", False),
            limits=data.get("limits", ()),
        )


# --------------------------------------------------------------------------- #
# observations
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MixObservation(_JsonRecord):
    """A measured, derived or heard fact about one scope.

    A listener observation is graded ``C`` until the delivery of the audio it
    describes is verified; ``B`` also demands a pinned listener identity, since
    "a model said so" is not a fact about a model nobody can name.
    """

    observation_id: str
    kind: ObservationKind
    scope: MixScope
    construct_ids: tuple[str, ...]
    evidence_grade: EvidenceGrade
    status: str
    metric: MetricReading | None = None
    asset: AssetRef | None = None
    listener: ListenerInfo | None = None
    support_window: SignalWindow | None = None
    limitations: tuple[str, ...] = ()
    freshness: str = "as_of_rev"

    def __post_init__(self) -> None:
        observation_id = _prefixed(self.observation_id, "mo", "observation_id")
        kind = _enum(self.kind, ObservationKind, "kind")
        scope = _record(self.scope, MixScope, "scope")
        construct_ids = _text_tuple(self.construct_ids, "construct_ids")
        if not construct_ids:
            _fail(
                "an observation must name at least one construct",
                field="construct_ids",
                next_action="reference_construct",
            )
        _unique(construct_ids, "construct_ids")
        grade = _enum(self.evidence_grade, EvidenceGrade, "evidence_grade")
        status = _choice(self.status, OBSERVATION_STATUSES, "status")

        metric = _optional_record(self.metric, MetricReading, "metric")
        asset = _optional_record(self.asset, AssetRef, "asset")
        listener = _optional_record(self.listener, ListenerInfo, "listener")
        support_window = _window_value(self.support_window, "support_window")
        limitations = _text_tuple(self.limitations, "limitations")
        freshness = _text(self.freshness, "freshness")

        if kind is ObservationKind.listener:
            if listener is None:
                _fail(
                    "a listener observation requires listener info",
                    field="listener",
                    next_action="attach_listener_info",
                )
            if grade not in (
                EvidenceGrade.B_listener_verified,
                EvidenceGrade.C_listener_unverified,
            ):
                _fail(
                    "a listener observation must be graded B (verified) "
                    "or C (unverified)",
                    field="evidence_grade",
                    next_action="grade_listener_evidence",
                )
            if grade is EvidenceGrade.B_listener_verified and (
                listener.model_revision is None or listener.weights_sha256 is None
            ):
                _fail(
                    "grade B requires a pinned listener identity "
                    "(model_revision and weights_sha256)",
                    field="listener",
                    next_action="pin_listener_identity",
                )
        elif kind is ObservationKind.measurement:
            if grade not in (
                EvidenceGrade.A_measured_dsp,
                EvidenceGrade.D_derived,
            ):
                _fail(
                    "a measurement observation must be graded A (measured) "
                    "or D (derived)",
                    field="evidence_grade",
                )
            if metric is None and status != "unavailable":
                _fail(
                    "a measurement observation requires a metric reading",
                    field="metric",
                    next_action="attach_metric",
                )

        object.__setattr__(self, "observation_id", observation_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "construct_ids", construct_ids)
        object.__setattr__(self, "evidence_grade", grade)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "metric", metric)
        object.__setattr__(self, "asset", asset)
        object.__setattr__(self, "listener", listener)
        object.__setattr__(self, "support_window", support_window)
        object.__setattr__(self, "limitations", limitations)
        object.__setattr__(self, "freshness", freshness)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "kind": str(self.kind),
            "scope": self.scope.to_dict(),
            "construct_ids": list(self.construct_ids),
            "evidence_grade": str(self.evidence_grade),
            "status": self.status,
            "metric": None if self.metric is None else self.metric.to_dict(),
            "asset": None if self.asset is None else self.asset.to_dict(),
            "listener": None if self.listener is None else self.listener.to_dict(),
            "support_window": (
                None if self.support_window is None else self.support_window.to_dict()
            ),
            "limitations": list(self.limitations),
            "freshness": self.freshness,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "MixObservation") -> "MixObservation":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail("observation must be a mapping or MixObservation", field="observation")
        return cls(
            observation_id=data.get("observation_id", ""),
            kind=data.get("kind", ""),
            scope=data.get("scope", {}),
            construct_ids=data.get("construct_ids", ()),
            evidence_grade=data.get("evidence_grade", ""),
            status=data.get("status", ""),
            metric=data.get("metric"),
            asset=data.get("asset"),
            listener=data.get("listener"),
            support_window=data.get("support_window"),
            limitations=data.get("limitations", ()),
            freshness=data.get("freshness", "as_of_rev"),
        )


# --------------------------------------------------------------------------- #
# claims
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AlternativeGroup(_JsonRecord):
    """Rival hypotheses that were live at the moment a claim was inferred.

    Inferred claims carry at least two members: one explanation plus a
    straw man is not a differential.
    """

    group_id: str
    members: tuple[str, ...]

    def __post_init__(self) -> None:
        group_id = _prefixed(self.group_id, "hypgrp", "group_id")
        if isinstance(self.members, str) or not isinstance(self.members, (list, tuple)):
            _fail(
                "members must be a sequence of hyp_ identifiers",
                field="members",
            )
        members = tuple(
            _prefixed(item, "hyp", f"members[{index}]")
            for index, item in enumerate(self.members)
        )
        if len(members) < 2:
            _fail(
                "an alternative group needs at least two members",
                field="members",
                next_action="add_alternative",
            )
        _unique(members, "members")
        object.__setattr__(self, "group_id", group_id)
        object.__setattr__(self, "members", members)

    def to_dict(self) -> dict[str, Any]:
        return {"group_id": self.group_id, "members": list(self.members)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "AlternativeGroup") -> "AlternativeGroup":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail(
                "alternatives must be a mapping or AlternativeGroup",
                field="alternatives",
            )
        return cls(
            group_id=data.get("group_id", ""),
            members=data.get("members", ()),
        )


@dataclass(frozen=True, slots=True)
class ClaimConfidence(_JsonRecord):
    """Confidence in a claim, with the shape of the evidence behind it.

    A probability with no basis, no interval and no calibration is a number
    with no history; ``basis="none"`` is the honest way to say so.
    """

    basis: str
    value: float | None = None
    interval: tuple[float, float] | None = None
    calibration_id: str | None = None
    n: int | None = None

    def __post_init__(self) -> None:
        basis = _choice(self.basis, CONFIDENCE_BASES, "basis")
        value = _optional_finite(self.value, "value")
        interval = self.interval
        if interval is not None:
            interval = _ordered_pair(interval, "interval")
        if basis == "interval" and interval is None:
            _fail(
                "basis 'interval' requires an interval",
                field="interval",
                next_action="provide_interval",
            )
        calibration_id = _optional_text(self.calibration_id, "calibration_id")
        n = self.n
        if n is not None:
            n = _integer(n, "n")
            if n < 0:
                _fail("n must be >= 0", field="n")
        object.__setattr__(self, "basis", basis)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "interval", interval)
        object.__setattr__(self, "calibration_id", calibration_id)
        object.__setattr__(self, "n", n)

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis": self.basis,
            "value": self.value,
            "interval": None if self.interval is None else list(self.interval),
            "calibration_id": self.calibration_id,
            "n": self.n,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "ClaimConfidence") -> "ClaimConfidence":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail("confidence must be a mapping or ClaimConfidence", field="confidence")
        return cls(
            basis=data.get("basis", "none"),
            value=data.get("value"),
            interval=data.get("interval"),
            calibration_id=data.get("calibration_id"),
            n=data.get("n"),
        )


@dataclass(frozen=True, slots=True)
class Severity(_JsonRecord):
    """How bad it is, and what licensed that judgement.

    There are no thresholds here on purpose.  ``anchored_by`` says whether a
    calibration backed the level, a heuristic did, or nothing did; the module
    never converts a level into a score.
    """

    level: str = "none"
    anchored_by: str = "none"
    value: float | None = None
    ci: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        level = _choice(self.level, SEVERITY_LEVELS, "level")
        anchored_by = _choice(self.anchored_by, SEVERITY_ANCHORS, "anchored_by")
        value = _optional_finite(self.value, "value")
        ci = self.ci
        if ci is not None:
            ci = _ordered_pair(ci, "ci")
        object.__setattr__(self, "level", level)
        object.__setattr__(self, "anchored_by", anchored_by)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "ci", ci)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "anchored_by": self.anchored_by,
            "value": self.value,
            "ci": None if self.ci is None else list(self.ci),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "Severity") -> "Severity":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail("severity must be a mapping or Severity", field="severity")
        return cls(
            level=data.get("level", "none"),
            anchored_by=data.get("anchored_by", "none"),
            value=data.get("value"),
            ci=data.get("ci"),
        )


@dataclass(frozen=True, slots=True)
class IntentRef(_JsonRecord):
    """The owner's stated goal, so a claim can be judged against intent."""

    goal_id: str | None
    statement: str | None
    source: str
    constraints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "goal_id", _optional_text(self.goal_id, "goal_id"))
        object.__setattr__(
            self, "statement", _optional_text(self.statement, "statement")
        )
        object.__setattr__(self, "source", _choice(self.source, INTENT_SOURCES, "source"))
        object.__setattr__(
            self, "constraints", _text_tuple(self.constraints, "constraints")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "statement": self.statement,
            "source": self.source,
            "constraints": list(self.constraints),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "IntentRef") -> "IntentRef":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail("intent must be a mapping or IntentRef", field="intent")
        return cls(
            goal_id=data.get("goal_id"),
            statement=data.get("statement"),
            source=data.get("source", ""),
            constraints=data.get("constraints", ()),
        )


@dataclass(frozen=True, slots=True)
class MixClaim(_JsonRecord):
    """One statement about the mix, with the evidence that licenses it.

    ``mix_inferred`` means a cause is being proposed, so at least two
    alternatives must still be on the table.  There is no ``quality`` and no
    ``score`` field: a claim is a sentence with evidence, not a ranking.
    """

    claim_id: str
    statement_template: str
    claim_class: ClaimClass
    construct_ids: tuple[str, ...]
    scope: MixScope
    evidence_ids: tuple[str, ...]
    evidence_grade: EvidenceGrade
    method: MixMethod
    confidence: ClaimConfidence
    reference_ids: tuple[str, ...] = ()
    alternatives: AlternativeGroup | None = None
    intent: IntentRef | None = None
    severity: Severity | None = None
    status: ClaimStatus = ClaimStatus.asserted
    not_checked: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        claim_id = _prefixed(self.claim_id, "clm", "claim_id")
        statement_template = _text(self.statement_template, "statement_template")
        claim_class = _enum(self.claim_class, ClaimClass, "claim_class")
        construct_ids = _text_tuple(self.construct_ids, "construct_ids")
        if not construct_ids:
            _fail(
                "a claim must name at least one construct",
                field="construct_ids",
                next_action="reference_construct",
            )
        _unique(construct_ids, "construct_ids")
        scope = _record(self.scope, MixScope, "scope")
        evidence_ids = _text_tuple(self.evidence_ids, "evidence_ids")
        if not evidence_ids:
            _fail(
                "a claim requires at least one piece of evidence",
                field="evidence_ids",
                next_action="attach_evidence",
            )
        _unique(evidence_ids, "evidence_ids")
        grade = _enum(self.evidence_grade, EvidenceGrade, "evidence_grade")
        method = _record(self.method, MixMethod, "method")
        confidence = _record(self.confidence, ClaimConfidence, "confidence")
        reference_ids = _text_tuple(self.reference_ids, "reference_ids")
        _unique(reference_ids, "reference_ids")
        alternatives = _optional_record(
            self.alternatives, AlternativeGroup, "alternatives"
        )
        intent = _optional_record(self.intent, IntentRef, "intent")
        severity = _optional_record(self.severity, Severity, "severity")
        status = _enum(self.status, ClaimStatus, "status")
        not_checked = _text_tuple(self.not_checked, "not_checked")

        if claim_class is ClaimClass.mix_inferred and (
            alternatives is None or len(alternatives.members) < 2
        ):
            _fail(
                "an inferred claim requires an alternative group with >= 2 members",
                field="alternatives",
                next_action="add_alternatives",
            )

        object.__setattr__(self, "claim_id", claim_id)
        object.__setattr__(self, "statement_template", statement_template)
        object.__setattr__(self, "claim_class", claim_class)
        object.__setattr__(self, "construct_ids", construct_ids)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "evidence_ids", evidence_ids)
        object.__setattr__(self, "evidence_grade", grade)
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "reference_ids", reference_ids)
        object.__setattr__(self, "alternatives", alternatives)
        object.__setattr__(self, "intent", intent)
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "not_checked", not_checked)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "statement_template": self.statement_template,
            "claim_class": str(self.claim_class),
            "construct_ids": list(self.construct_ids),
            "scope": self.scope.to_dict(),
            "evidence_ids": list(self.evidence_ids),
            "evidence_grade": str(self.evidence_grade),
            "method": self.method.to_dict(),
            "confidence": self.confidence.to_dict(),
            "reference_ids": list(self.reference_ids),
            "alternatives": (
                None if self.alternatives is None else self.alternatives.to_dict()
            ),
            "intent": None if self.intent is None else self.intent.to_dict(),
            "severity": None if self.severity is None else self.severity.to_dict(),
            "status": str(self.status),
            "not_checked": list(self.not_checked),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "MixClaim") -> "MixClaim":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail("claim must be a mapping or MixClaim", field="claim")
        return cls(
            claim_id=data.get("claim_id", ""),
            statement_template=data.get("statement_template", ""),
            claim_class=data.get("claim_class", ""),
            construct_ids=data.get("construct_ids", ()),
            scope=data.get("scope", {}),
            evidence_ids=data.get("evidence_ids", ()),
            evidence_grade=data.get("evidence_grade", ""),
            method=data.get("method", {}),
            confidence=data.get("confidence", {}),
            reference_ids=data.get("reference_ids", ()),
            alternatives=data.get("alternatives"),
            intent=data.get("intent"),
            severity=data.get("severity"),
            status=data.get("status", ClaimStatus.asserted),
            not_checked=data.get("not_checked", ()),
        )


# --------------------------------------------------------------------------- #
# hypotheses
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Prediction(_JsonRecord):
    """What the mix would do if a mechanism were true, and if it were not."""

    if_true: str
    if_false: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "if_true", _text(self.if_true, "if_true"))
        object.__setattr__(self, "if_false", _text(self.if_false, "if_false"))

    def to_dict(self) -> dict[str, Any]:
        return {"if_true": self.if_true, "if_false": self.if_false}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "Prediction") -> "Prediction":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail("prediction must be a mapping or Prediction", field="predictions")
        return cls(if_true=data.get("if_true", ""), if_false=data.get("if_false", ""))


@dataclass(frozen=True, slots=True)
class DiscriminatingTest(_JsonRecord):
    """An action whose two outcomes separate the hypothesis from its rivals."""

    test_id: str
    description: str
    expected_if_true: str
    expected_if_false: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "test_id", _text(self.test_id, "test_id"))
        object.__setattr__(
            self, "description", _text(self.description, "description")
        )
        object.__setattr__(
            self, "expected_if_true", _text(self.expected_if_true, "expected_if_true")
        )
        object.__setattr__(
            self,
            "expected_if_false",
            _text(self.expected_if_false, "expected_if_false"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "description": self.description,
            "expected_if_true": self.expected_if_true,
            "expected_if_false": self.expected_if_false,
        }

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any] | "DiscriminatingTest"
    ) -> "DiscriminatingTest":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail(
                "discriminating test must be a mapping or DiscriminatingTest",
                field="discriminating_tests",
            )
        return cls(
            test_id=data.get("test_id", ""),
            description=data.get("description", ""),
            expected_if_true=data.get("expected_if_true", ""),
            expected_if_false=data.get("expected_if_false", ""),
        )


@dataclass(frozen=True, slots=True)
class IssueHypothesis(_JsonRecord):
    """A candidate cause, stated so that it can lose.

    A cause is never a fact.  The hypothesis must either carry at least one
    discriminating test or say plainly that it is not testable; otherwise it is
    a story, and it fails with ``hypothesis_not_testable``.
    """

    hypothesis_id: str
    issue_class: IssueClass
    construct_ids: tuple[str, ...]
    scope: MixScope
    proposition: str
    cause_family: CauseFamily
    mechanism: str
    predictions: tuple[Prediction, ...] = ()
    supports: tuple[str, ...] = ()
    against: tuple[str, ...] = ()
    discriminating_tests: tuple[DiscriminatingTest, ...] = ()
    not_testable: bool = False

    def __post_init__(self) -> None:
        hypothesis_id = _prefixed(self.hypothesis_id, "hyp", "hypothesis_id")
        issue_class = _enum(self.issue_class, IssueClass, "issue_class")
        construct_ids = _text_tuple(self.construct_ids, "construct_ids")
        if not construct_ids:
            _fail(
                "a hypothesis must name at least one construct",
                field="construct_ids",
                next_action="reference_construct",
            )
        _unique(construct_ids, "construct_ids")
        scope = _record(self.scope, MixScope, "scope")
        proposition = _text(self.proposition, "proposition")
        cause_family = _enum(self.cause_family, CauseFamily, "cause_family")
        mechanism = _text(self.mechanism, "mechanism")
        predictions = _records(self.predictions, Prediction, "predictions")
        supports = _text_tuple(self.supports, "supports")
        against = _text_tuple(self.against, "against")
        tests = _records(
            self.discriminating_tests, DiscriminatingTest, "discriminating_tests"
        )
        _unique(tests, "discriminating_tests", attr="test_id")
        not_testable = _bool(self.not_testable, "not_testable")

        if not tests and not not_testable:
            _fail(
                "a hypothesis needs a discriminating test or not_testable=True",
                field="discriminating_tests",
                next_action="add_discriminating_test_or_mark_untestable",
                code=HYPOTHESIS_NOT_TESTABLE_CODE,
            )

        object.__setattr__(self, "hypothesis_id", hypothesis_id)
        object.__setattr__(self, "issue_class", issue_class)
        object.__setattr__(self, "construct_ids", construct_ids)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "proposition", proposition)
        object.__setattr__(self, "cause_family", cause_family)
        object.__setattr__(self, "mechanism", mechanism)
        object.__setattr__(self, "predictions", predictions)
        object.__setattr__(self, "supports", supports)
        object.__setattr__(self, "against", against)
        object.__setattr__(self, "discriminating_tests", tests)
        object.__setattr__(self, "not_testable", not_testable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "issue_class": str(self.issue_class),
            "construct_ids": list(self.construct_ids),
            "scope": self.scope.to_dict(),
            "proposition": self.proposition,
            "cause_family": str(self.cause_family),
            "mechanism": self.mechanism,
            "predictions": [item.to_dict() for item in self.predictions],
            "supports": list(self.supports),
            "against": list(self.against),
            "discriminating_tests": [item.to_dict() for item in self.discriminating_tests],
            "not_testable": self.not_testable,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "IssueHypothesis") -> "IssueHypothesis":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail("hypothesis must be a mapping or IssueHypothesis", field="hypothesis")
        return cls(
            hypothesis_id=data.get("hypothesis_id", ""),
            issue_class=data.get("issue_class", ""),
            construct_ids=data.get("construct_ids", ()),
            scope=data.get("scope", {}),
            proposition=data.get("proposition", ""),
            cause_family=data.get("cause_family", ""),
            mechanism=data.get("mechanism", ""),
            predictions=data.get("predictions", ()),
            supports=data.get("supports", ()),
            against=data.get("against", ()),
            discriminating_tests=data.get("discriminating_tests", ()),
            not_testable=data.get("not_testable", False),
        )


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PostChangeCheck(_JsonRecord):
    """What a change was supposed to move, and what actually moved.

    ``verified`` and ``failed`` are measurements, not opinions: both require
    the observed reading that decided the outcome.
    """

    check_id: str
    claim_id: str
    scope: MixScope
    target_metric: MetricReading
    expected_direction: str
    status: str
    observed: MetricReading | None = None
    evidence_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        check_id = _prefixed(self.check_id, "ver", "check_id")
        claim_id = _prefixed(self.claim_id, "clm", "claim_id")
        scope = _record(self.scope, MixScope, "scope")
        target_metric = _record(self.target_metric, MetricReading, "target_metric")
        direction = _choice(
            self.expected_direction, EXPECTED_DIRECTIONS, "expected_direction"
        )
        status = _choice(self.status, POST_CHANGE_STATUSES, "status")
        observed = _optional_record(self.observed, MetricReading, "observed")
        evidence_ids = _text_tuple(self.evidence_ids, "evidence_ids")
        _unique(evidence_ids, "evidence_ids")
        notes = _text_tuple(self.notes, "notes")

        if status in {"verified", "failed"} and observed is None:
            _fail(
                f"status {status!r} requires the observed metric that decided it",
                field="observed",
                next_action="attach_observed_metric",
            )

        object.__setattr__(self, "check_id", check_id)
        object.__setattr__(self, "claim_id", claim_id)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "target_metric", target_metric)
        object.__setattr__(self, "expected_direction", direction)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "observed", observed)
        object.__setattr__(self, "evidence_ids", evidence_ids)
        object.__setattr__(self, "notes", notes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "claim_id": self.claim_id,
            "scope": self.scope.to_dict(),
            "target_metric": self.target_metric.to_dict(),
            "expected_direction": self.expected_direction,
            "status": self.status,
            "observed": None if self.observed is None else self.observed.to_dict(),
            "evidence_ids": list(self.evidence_ids),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "PostChangeCheck") -> "PostChangeCheck":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            _fail("check must be a mapping or PostChangeCheck", field="check")
        return cls(
            check_id=data.get("check_id", ""),
            claim_id=data.get("claim_id", ""),
            scope=data.get("scope", {}),
            target_metric=data.get("target_metric", {}),
            expected_direction=data.get("expected_direction", ""),
            status=data.get("status", ""),
            observed=data.get("observed"),
            evidence_ids=data.get("evidence_ids", ()),
            notes=data.get("notes", ()),
        )


# --------------------------------------------------------------------------- #
# serialization
# --------------------------------------------------------------------------- #


def dumps_mix_record(record: Any) -> str:
    """Serialize a mix record as strict, deterministic JSON."""
    to_dict = getattr(record, "to_dict", None)
    if not callable(to_dict):
        _fail("mix record must expose to_dict()", field="record")
    return json.dumps(to_dict(), **_JSON_DUMPS_OPTIONS)


def loads_mix_record(cls: type, text: str) -> Any:
    """Parse JSON produced by :func:`dumps_mix_record` back into ``cls``."""
    if not isinstance(text, str):
        _fail("record text must be a string", field="record")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        _fail("record text is not valid JSON", field="record")
    if not isinstance(data, Mapping):
        _fail("record must be a JSON object", field="record")
    return cls.from_dict(data)


__all__ = [
    "MIX_INVARIANT_CODE",
    "HYPOTHESIS_NOT_TESTABLE_CODE",
    "OBSERVATION_STATUSES",
    "TRACK_ROLES",
    "SECTION_LEVELS",
    "LABEL_SOURCES",
    "ROLE_SOURCES",
    "LEVEL_MODES",
    "CONFIDENCE_BASES",
    "SEVERITY_LEVELS",
    "SEVERITY_ANCHORS",
    "INTENT_SOURCES",
    "EXPECTED_DIRECTIONS",
    "POST_CHANGE_STATUSES",
    "IssueClass",
    "ObservationKind",
    "ClaimClass",
    "ClaimStatus",
    "EvidenceGrade",
    "CauseFamily",
    "SectionRef",
    "RoleRef",
    "ScopeEntity",
    "SignalPathRefs",
    "LevelReference",
    "MixScope",
    "MixMethod",
    "MetricReading",
    "AssetRef",
    "ListenerInfo",
    "MixObservation",
    "AlternativeGroup",
    "ClaimConfidence",
    "Severity",
    "IntentRef",
    "MixClaim",
    "Prediction",
    "DiscriminatingTest",
    "IssueHypothesis",
    "PostChangeCheck",
    "dumps_mix_record",
    "loads_mix_record",
]
