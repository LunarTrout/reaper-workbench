"""Typed values crossing analysis, DAW and integration boundaries."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import isfinite
from typing import Any

import numpy as np

from .errors import ValidationError


@dataclass(frozen=True, slots=True)
class Section:
    start_s: float
    end_s: float

    def __post_init__(self) -> None:
        if not isfinite(self.start_s) or not isfinite(self.end_s):
            raise ValidationError("Section bounds must be finite")
        if self.start_s < 0 or self.end_s <= self.start_s:
            raise ValidationError("Section must satisfy 0 <= start_s < end_s",
                                  details={"start_s": self.start_s,
                                           "end_s": self.end_s})

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


@dataclass(frozen=True, slots=True)
class AudioBuffer:
    samples: np.ndarray
    sample_rate: int
    source: str = ""
    start_s: float = 0.0
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        data = np.asarray(self.samples)
        if data.ndim == 1:
            data = data[:, None]
        if data.ndim != 2 or data.shape[0] == 0 or data.shape[1] == 0:
            raise ValidationError("Audio must be a non-empty frames-by-channels array")
        if self.sample_rate <= 0:
            raise ValidationError("sample_rate must be positive")
        if not isfinite(self.start_s) or self.start_s < 0:
            raise ValidationError("start_s must be finite and non-negative")
        if not np.isfinite(data).all():
            raise ValidationError("Audio contains NaN or infinite samples")
        object.__setattr__(self, "samples", np.ascontiguousarray(data, dtype=np.float64))

    @property
    def frames(self) -> int:
        return int(self.samples.shape[0])

    @property
    def channels(self) -> int:
        return int(self.samples.shape[1])

    @property
    def duration_s(self) -> float:
        return self.frames / self.sample_rate


@dataclass(frozen=True, slots=True)
class MetricValue:
    value: float | None
    unit: str
    confidence: float | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class MeasurementReport:
    source: str
    sample_rate: int
    channels: int
    duration_s: float
    section: Section | None
    metrics: dict[str, MetricValue]
    curves: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
