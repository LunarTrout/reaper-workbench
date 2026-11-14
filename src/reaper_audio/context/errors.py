"""Structured contract errors for context records and time values.

Every error carries a stable machine-readable ``code`` plus the field that
failed and, where one exists, the next action a caller can take.  Codes are
part of the public contract and must never change.
"""

from __future__ import annotations

from typing import Any


class ContractError(ValueError):
    """Base class for contract violations at a module boundary."""

    code = "contract_error"

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        next_action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = type(self).code
        self.field = field
        self.next_action = next_action

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "next_action": self.next_action,
        }

    def __str__(self) -> str:
        parts = [self.message]
        if self.field:
            parts.append(f"field={self.field}")
        return " ".join(parts)


class UnknownPrefixError(ContractError):
    """An identifier prefix is not registered."""

    code = "unknown_id_prefix"


class MissingTimeBasisError(ContractError):
    """A time value cannot be interpreted without a basis or a map."""

    code = "missing_time_basis"


class InvalidStatusError(ContractError):
    """A status value is not valid for its record kind."""

    code = "invalid_status"


class RevisionMismatchError(ContractError):
    """The record was produced against a different project revision."""

    code = "revision_mismatch"


class MappingUnavailableError(ContractError):
    """No verified mapping exists between the requested time bases."""

    code = "mapping_unavailable"


class SchemaVersionError(ContractError):
    """The record's schema version is not supported by this reader."""

    code = "schema_version_unsupported"


class ContractValidationError(ContractError):
    """A field failed a structural or range invariant."""

    code = "contract_validation"


__all__ = [
    "ContractError",
    "UnknownPrefixError",
    "MissingTimeBasisError",
    "InvalidStatusError",
    "RevisionMismatchError",
    "MappingUnavailableError",
    "SchemaVersionError",
    "ContractValidationError",
]
