"""Canonical time values, intervals and maps.

A ``TimeRef`` is never a bare float: the basis says which clock the value
belongs to and the unit is fixed by that basis.  Wave A deliberately supports
no cross-basis conversion; callers that need one must obtain a verified
``TimeMap`` in a later wave and get ``MappingUnavailableError`` until then.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from .context.errors import (
    ContractValidationError,
    MappingUnavailableError,
    MissingTimeBasisError,
)


class TimeBasis(StrEnum):
    """Clock a time value is expressed in."""

    sample_index = "sample_index"
    file_s = "file_s"
    source_s = "source_s"
    take_s = "take_s"
    project_s = "project_s"
    musical_qn = "musical_qn"
    bar_beat = "bar_beat"
    performed_beat = "performed_beat"
    capture_s = "capture_s"


class MappingStatus(StrEnum):
    """How far a time value or map may be trusted."""

    verified = "verified"
    approx = "approx"
    unverified = "unverified"
    unsupported = "unsupported"


class TimeUnit(StrEnum):
    """Physical or notational unit of a time value."""

    sample = "sample"
    s = "s"
    qn = "qn"
    bar_beat = "bar_beat"
    beat = "beat"


_UNIT_FOR_BASIS: dict[TimeBasis, TimeUnit] = {
    TimeBasis.sample_index: TimeUnit.sample,
    TimeBasis.file_s: TimeUnit.s,
    TimeBasis.source_s: TimeUnit.s,
    TimeBasis.take_s: TimeUnit.s,
    TimeBasis.project_s: TimeUnit.s,
    TimeBasis.musical_qn: TimeUnit.qn,
    TimeBasis.bar_beat: TimeUnit.bar_beat,
    TimeBasis.performed_beat: TimeUnit.beat,
    TimeBasis.capture_s: TimeUnit.s,
}

_MAP_REQUIRED = frozenset(
    {
        TimeBasis.source_s,
        TimeBasis.take_s,
        TimeBasis.musical_qn,
        TimeBasis.bar_beat,
    }
)

_MAP_FORBIDDEN = frozenset(
    {
        TimeBasis.sample_index,
        TimeBasis.file_s,
        TimeBasis.project_s,
        TimeBasis.capture_s,
    }
)

INTERVAL_ANCHORS = frozenset(
    {
        "attack_start",
        "energy_peak",
        "detector_frame",
        "window_bounds",
        "event_boundary",
    }
)

_SAMPLE_BASES = frozenset(
    {
        TimeBasis.sample_index,
        TimeBasis.file_s,
        TimeBasis.project_s,
    }
)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field} must be a non-empty string", field=field)
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractValidationError(f"{field} must be a number", field=field)
    number = float(value)
    if not math.isfinite(number):
        raise ContractValidationError(f"{field} must be finite", field=field)
    return number


def _coerce_basis(value: Any) -> TimeBasis:
    if value is None or value == "":
        raise MissingTimeBasisError("time value requires a basis", field="basis")
    if isinstance(value, TimeBasis):
        return value
    try:
        return TimeBasis(value)
    except (ValueError, TypeError) as exc:
        raise ContractValidationError(f"unknown time basis: {value!r}", field="basis") from exc


def _coerce_unit(value: Any) -> TimeUnit:
    if value is None or value == "":
        raise ContractValidationError("time value requires a unit", field="unit")
    if isinstance(value, TimeUnit):
        return value
    try:
        return TimeUnit(value)
    except (ValueError, TypeError) as exc:
        raise ContractValidationError(f"unknown time unit: {value!r}", field="unit") from exc


def _coerce_mapping_status(value: Any) -> MappingStatus:
    if isinstance(value, MappingStatus):
        return value
    try:
        return MappingStatus(value)
    except (ValueError, TypeError) as exc:
        raise ContractValidationError(
            f"unknown mapping status: {value!r}", field="mapping_status"
        ) from exc


@dataclass(frozen=True, slots=True)
class TimeRef:
    """One point on a named clock, with its uncertainty and provenance."""

    basis: TimeBasis
    value: float
    unit: TimeUnit
    uncertainty_ms: float | None = None
    map_id: str | None = None
    mapping_status: MappingStatus = MappingStatus.unverified
    hypothesis_id: str | None = None

    def __post_init__(self) -> None:
        basis = _coerce_basis(self.basis)
        unit = _coerce_unit(self.unit)
        value = _finite_number(self.value, "value")
        status = _coerce_mapping_status(self.mapping_status)
        map_id = _optional_text(self.map_id, "map_id")
        hypothesis_id = _optional_text(self.hypothesis_id, "hypothesis_id")
        uncertainty = self.uncertainty_ms
        if uncertainty is not None:
            uncertainty = _finite_number(uncertainty, "uncertainty_ms")
            if uncertainty < 0:
                raise ContractValidationError(
                    "uncertainty_ms must be >= 0", field="uncertainty_ms"
                )

        expected_unit = _UNIT_FOR_BASIS[basis]
        if unit is not expected_unit:
            raise ContractValidationError(
                f"basis {basis} requires unit {expected_unit}, got {unit}",
                field="unit",
            )
        if basis in _MAP_REQUIRED and map_id is None:
            raise MissingTimeBasisError(
                f"basis {basis} requires map_id", field="map_id"
            )
        if basis in _MAP_FORBIDDEN and map_id is not None:
            raise ContractValidationError(
                f"basis {basis} does not accept map_id", field="map_id"
            )
        if basis is TimeBasis.performed_beat and hypothesis_id is None:
            raise MissingTimeBasisError(
                "basis performed_beat requires hypothesis_id", field="hypothesis_id"
            )

        object.__setattr__(self, "basis", basis)
        object.__setattr__(self, "unit", unit)
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "mapping_status", status)
        object.__setattr__(self, "map_id", map_id)
        object.__setattr__(self, "hypothesis_id", hypothesis_id)
        object.__setattr__(self, "uncertainty_ms", uncertainty)

    def to_dict(self) -> dict[str, Any]:
        return {
            "basis": str(self.basis),
            "value": self.value,
            "unit": str(self.unit),
            "uncertainty_ms": self.uncertainty_ms,
            "map_id": self.map_id,
            "mapping_status": str(self.mapping_status),
            "hypothesis_id": self.hypothesis_id,
        }


@dataclass(frozen=True, slots=True)
class Interval:
    """A half-open span on one clock; ``anchor`` belongs here, not on TimeRef."""

    start: TimeRef
    end: TimeRef | None = None
    anchor: str = "window_bounds"

    def __post_init__(self) -> None:
        start = self.start
        if isinstance(start, Mapping):
            start = time_ref_from_dict(start)
            object.__setattr__(self, "start", start)
        if not isinstance(start, TimeRef):
            raise ContractValidationError("interval start must be a TimeRef", field="start")
        validate_time_ref(start)

        end = self.end
        if isinstance(end, Mapping):
            end = time_ref_from_dict(end)
            object.__setattr__(self, "end", end)
        if end is not None:
            if not isinstance(end, TimeRef):
                raise ContractValidationError(
                    "interval end must be a TimeRef or None", field="end"
                )
            validate_time_ref(end)
            if end.basis is not start.basis or end.unit is not start.unit:
                raise ContractValidationError(
                    "interval start and end must share basis and unit", field="end"
                )
            if end.value < start.value:
                raise ContractValidationError(
                    "interval end must be >= start", field="end"
                )

        if self.anchor not in INTERVAL_ANCHORS:
            raise ContractValidationError(
                f"unknown interval anchor: {self.anchor!r}", field="anchor"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.to_dict(),
            "end": None if self.end is None else self.end.to_dict(),
            "anchor": self.anchor,
        }


@dataclass(frozen=True, slots=True)
class TimeMap:
    """A versioned, reviewable mapping between two clocks."""

    map_id: str
    project_id: str
    version: int
    method: str
    max_residual_ms: float
    linear_model: str = "bpm_linear"
    mapping_status: MappingStatus = MappingStatus.approx

    def __post_init__(self) -> None:
        _text(self.map_id, "map_id")
        _text(self.project_id, "project_id")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise ContractValidationError("version must be an integer", field="version")
        if self.version < 1:
            raise ContractValidationError("version must be >= 1", field="version")
        _text(self.method, "method")
        residual = _finite_number(self.max_residual_ms, "max_residual_ms")
        if residual < 0:
            raise ContractValidationError(
                "max_residual_ms must be >= 0", field="max_residual_ms"
            )
        _text(self.linear_model, "linear_model")
        object.__setattr__(self, "max_residual_ms", residual)
        object.__setattr__(self, "mapping_status", _coerce_mapping_status(self.mapping_status))

    def to_dict(self) -> dict[str, Any]:
        return {
            "map_id": self.map_id,
            "project_id": self.project_id,
            "version": self.version,
            "method": self.method,
            "max_residual_ms": self.max_residual_ms,
            "linear_model": self.linear_model,
            "mapping_status": str(self.mapping_status),
        }


def validate_time_ref(ref: TimeRef) -> None:
    """Raise a contract error unless ``ref`` is a valid ``TimeRef``."""
    if not isinstance(ref, TimeRef):
        raise ContractValidationError("time value must be a TimeRef", field="time")


def time_ref_to_dict(ref: TimeRef) -> dict[str, Any]:
    validate_time_ref(ref)
    return ref.to_dict()


def time_ref_from_dict(data: Mapping[str, Any] | TimeRef) -> TimeRef:
    """Parse a time ref, tolerating dicts written before optional fields existed."""
    if isinstance(data, TimeRef):
        return data
    if not isinstance(data, Mapping):
        raise ContractValidationError("time value must be a mapping", field="time")
    return TimeRef(
        basis=cast(TimeBasis, data.get("basis")),
        value=cast(float, data.get("value")),
        unit=cast(TimeUnit, data.get("unit")),
        uncertainty_ms=data.get("uncertainty_ms"),
        map_id=data.get("map_id"),
        mapping_status=data.get("mapping_status", MappingStatus.unverified),
        hypothesis_id=data.get("hypothesis_id"),
    )


def compose(
    ref: TimeRef,
    target_basis: TimeBasis | str,
    time_map: TimeMap | None = None,
) -> TimeRef:
    """Return ``ref`` in ``target_basis``.

    Wave A only supports identity composition.  Every cross-basis request is a
    typed refusal that names the map the caller must obtain; no conversion is
    invented here.
    """
    validate_time_ref(ref)
    target = _coerce_basis(target_basis)
    if ref.basis is not target:
        raise MappingUnavailableError(
            f"no composition path from {ref.basis} to {target} in Wave A",
            field="target_basis",
            next_action="project_time_map",
        )
    if time_map is not None and not isinstance(time_map, TimeMap):
        raise ContractValidationError("time_map must be a TimeMap or None", field="time_map")
    return TimeRef(
        basis=ref.basis,
        value=ref.value,
        unit=ref.unit,
        uncertainty_ms=ref.uncertainty_ms,
        map_id=ref.map_id,
        mapping_status=ref.mapping_status,
        hypothesis_id=ref.hypothesis_id,
    )


def to_sample_index(ref: TimeRef, sample_rate: int | float, *, is_end: bool = False) -> int:
    """Convert a sample/file/project time to an integer sample index.

    Starts use ``floor`` and ends use ``ceil`` so a window never loses a sample.
    """
    validate_time_ref(ref)
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, (int, float)):
        raise ContractValidationError("sample_rate must be a number", field="sample_rate")
    rate = float(sample_rate)
    if not math.isfinite(rate) or rate <= 0:
        raise ContractValidationError("sample_rate must be > 0", field="sample_rate")
    if ref.basis not in _SAMPLE_BASES:
        raise ContractValidationError(
            f"to_sample_index does not accept basis {ref.basis}", field="basis"
        )
    position = float(ref.value) if ref.basis is TimeBasis.sample_index else ref.value * rate
    return math.ceil(position) if is_end else math.floor(position)


__all__ = [
    "TimeBasis",
    "MappingStatus",
    "TimeUnit",
    "TimeRef",
    "Interval",
    "TimeMap",
    "INTERVAL_ANCHORS",
    "validate_time_ref",
    "time_ref_to_dict",
    "time_ref_from_dict",
    "compose",
    "to_sample_index",
]
