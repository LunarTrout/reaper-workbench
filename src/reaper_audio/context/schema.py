"""Canonical record envelope and revision identity for the context graph.

``schema_version`` is frozen at 1 for Wave A.  Additive changes are allowed
within a major version; any other version is rejected with
``SchemaVersionError`` instead of being guessed at.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .errors import (
    ContractValidationError,
    SchemaVersionError,
)

SCHEMA_VERSION = 1

FINGERPRINT_SCOPES = frozenset({"full", "coarse"})


class RecordKind(StrEnum):
    """Kinds of records that can appear in the append-only journal."""

    project = "project"
    revision = "revision"
    capture = "capture"
    asset = "asset"
    measurement = "measurement"
    observation = "observation"
    annotation = "annotation"
    event = "event"
    occurrence = "occurrence"
    gesture = "gesture"
    score = "score"
    time_map = "time_map"
    rhythm_map = "rhythm_map"
    section = "section"
    riff = "riff"
    claim = "claim"
    hypothesis = "hypothesis"
    intent = "intent"
    plan = "plan"
    operation = "operation"
    change = "change"
    verification = "verification"
    delivery = "delivery"


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field} must be a non-empty string", field=field)
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractValidationError(f"{field} must be an integer", field=field)
    if value < 0:
        raise ContractValidationError(f"{field} must be >= 0", field=field)
    return value


@dataclass(frozen=True, slots=True)
class RevisionRef:
    """Identity and content fingerprint of one project revision.

    ``counter`` is diagnostic only; freshness gates compare ``project_id``,
    ``instance_id`` and ``epoch`` for identity and ``fingerprint`` for content.
    """

    project_id: str
    instance_id: str | None
    epoch: int
    counter: int
    fingerprint: str
    fingerprint_scope: str = "full"

    def __post_init__(self) -> None:
        _text(self.project_id, "project_id")
        _optional_text(self.instance_id, "instance_id")
        _non_negative_int(self.epoch, "epoch")
        _non_negative_int(self.counter, "counter")
        _text(self.fingerprint, "fingerprint")
        if self.fingerprint_scope not in FINGERPRINT_SCOPES:
            raise ContractValidationError(
                "fingerprint_scope must be 'full' or 'coarse'",
                field="fingerprint_scope",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "instance_id": self.instance_id,
            "epoch": self.epoch,
            "counter": self.counter,
            "fingerprint": self.fingerprint,
            "fingerprint_scope": self.fingerprint_scope,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "RevisionRef") -> "RevisionRef":
        """Parse a revision dict.

        Accepts dicts written before ``instance_id``/``epoch``/
        ``fingerprint_scope`` existed: those default to ``None``/``0``/
        ``"full"``.  ``fingerprint`` is mandatory.
        """
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            raise ContractValidationError(
                "revision must be a mapping or RevisionRef", field="revision"
            )
        try:
            epoch = int(data.get("epoch", 0))
            counter = int(data.get("counter", 0))
        except (TypeError, ValueError) as exc:
            raise ContractValidationError(
                "epoch and counter must be integers", field="epoch"
            ) from exc
        return cls(
            project_id=data.get("project_id", ""),
            instance_id=data.get("instance_id"),
            epoch=epoch,
            counter=counter,
            fingerprint=data.get("fingerprint", ""),
            fingerprint_scope=data.get("fingerprint_scope", "full"),
        )


@dataclass(frozen=True, slots=True)
class RecordEnvelope:
    """Outer envelope of one journal record."""

    record_kind: RecordKind
    record_id: str
    schema_version: int = SCHEMA_VERSION
    revision: RevisionRef | None = None
    created_at: str | None = None
    producer: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.record_kind, RecordKind):
            if isinstance(self.record_kind, str):
                try:
                    object.__setattr__(self, "record_kind", RecordKind(self.record_kind))
                except ValueError as exc:
                    raise ContractValidationError(
                        f"unknown record_kind: {self.record_kind!r}", field="record_kind"
                    ) from exc
            else:
                raise ContractValidationError(
                    "record_kind must be a RecordKind", field="record_kind"
                )
        _text(self.record_id, "record_id")
        if self.schema_version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"schema_version {self.schema_version} is not supported",
                field="schema_version",
            )
        _optional_text(self.created_at, "created_at")
        _optional_text(self.producer, "producer")
        if self.revision is not None and not isinstance(self.revision, RevisionRef):
            object.__setattr__(self, "revision", validate_revision(self.revision))

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_kind": str(self.record_kind),
            "record_id": self.record_id,
            "schema_version": self.schema_version,
            "revision": None if self.revision is None else self.revision.to_dict(),
            "created_at": self.created_at,
            "producer": self.producer,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RecordEnvelope":
        if not isinstance(data, Mapping):
            raise ContractValidationError("record must be a mapping", field="record")
        raw_version = data.get("schema_version", SCHEMA_VERSION)
        try:
            schema_version = int(raw_version)
        except (TypeError, ValueError) as exc:
            raise SchemaVersionError(
                f"schema_version {raw_version!r} is not supported",
                field="schema_version",
            ) from exc
        if schema_version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"schema_version {schema_version} is not supported",
                field="schema_version",
            )
        revision = data.get("revision")
        return cls(
            record_kind=data.get("record_kind", ""),
            record_id=data.get("record_id", ""),
            schema_version=schema_version,
            revision=None if revision is None else validate_revision(revision),
            created_at=data.get("created_at"),
            producer=data.get("producer"),
        )


def validate_revision(data_or_revision: Mapping[str, Any] | RevisionRef) -> RevisionRef:
    """Return a ``RevisionRef`` from either a mapping or an existing instance."""
    if isinstance(data_or_revision, RevisionRef):
        return data_or_revision
    if isinstance(data_or_revision, Mapping):
        return RevisionRef.from_dict(data_or_revision)
    raise ContractValidationError(
        "revision must be a mapping or RevisionRef", field="revision"
    )


def dumps_record(envelope: RecordEnvelope) -> str:
    """Serialize an envelope as strict, deterministic JSON."""
    if not isinstance(envelope, RecordEnvelope):
        raise ContractValidationError("envelope must be a RecordEnvelope", field="record")
    return json.dumps(
        envelope.to_dict(),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    )


def loads_record(text: str) -> RecordEnvelope:
    """Parse a JSON envelope, rejecting unknown schema versions."""
    if not isinstance(text, str):
        raise ContractValidationError("record text must be a string", field="record")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractValidationError("record text is not valid JSON", field="record") from exc
    return RecordEnvelope.from_dict(data)


__all__ = [
    "SCHEMA_VERSION",
    "FINGERPRINT_SCOPES",
    "RecordKind",
    "RevisionRef",
    "RecordEnvelope",
    "validate_revision",
    "dumps_record",
    "loads_record",
]
