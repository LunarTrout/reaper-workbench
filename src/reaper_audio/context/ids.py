"""Identifier registry for context records.

Two kinds of identifiers exist and they are never interchangeable:

``new_id`` — authored or session records (captures, observations, plans, …).
The body is a ULID: a 48-bit millisecond timestamp plus 80 bits of entropy,
encoded as 26 Crockford base32 characters.  ULIDs are monotonic inside one
process, so a later identifier never sorts before an earlier one.

``derive_id`` — derived records (events, occurrences, gestures, measurements).
The body is a truncated SHA-256 over the canonical natural key, the method
version and the revision fingerprint.  Re-analysing the same revision with the
same parameters reproduces the same identifier; changing any input yields a
new one.

A prefix is only valid if it is registered in ``PREFIXES``.  ``ges`` is
deliberately absent: the gesture prefix is ``peg`` and the retired spelling is
rejected with ``next_action="use peg_"`` instead of being silently accepted.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import threading
import time
from collections.abc import Mapping
from typing import Any, NoReturn

from .errors import ContractValidationError, UnknownPrefixError

CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ID_BODY_LENGTH = 26
GESTURE_PREFIX = "peg"

#: Bumped whenever the derivation payload changes shape, so old and new IDs
#: can never collide on the same natural key.
_ID_SCHEME = "reaper_audio.id/1"

#: Every registered prefix.  ``ges`` is intentionally not here.
PREFIXES: frozenset[str] = frozenset(
    {
        "prj",
        "pri",
        "rev",
        "trk",
        "trk_master",
        "itm",
        "tak",
        "src",
        "fx",
        "snd",
        "env",
        "sco",
        "mrk",
        "rgn",
        "tm",
        "rmp",
        "cor",
        "sec",
        "rif",
        "phr",
        "peg",
        "occ",
        "evt",
        "prf",
        "mea",
        "ast_sha256",
        "cap",
        "obs",
        "ann",
        "clm",
        "hyp",
        "hypgrp",
        "int",
        "pln",
        "op",
        "chg",
        "ver",
        "mo",
        "ref",
        "rel",
        "sc",
        "dlv",
    }
)

#: Retired prefixes.  Accepted by neither ``new_id`` nor ``parse_id``.
DEPRECATED_PREFIXES: frozenset[str] = frozenset({"ges"})

#: Prefixes whose identity must be reproducible from content.
DERIVED_PREFIXES: frozenset[str] = frozenset(
    {"evt", "prf", "occ", "peg", "sco", "mea", "ast_sha256", "rmp", "cor", "tm", "rev", "rel"}
)

#: Prefixes minted once per authored record or session action.
AUTHORED_PREFIXES: frozenset[str] = frozenset(
    {
        "cap",
        "obs",
        "clm",
        "hyp",
        "hypgrp",
        "int",
        "pln",
        "op",
        "chg",
        "ver",
        "prj",
        "pri",
        "ann",
        "mo",
        "ref",
        "dlv",
    }
)

_CROCKFORD_INDEX = {char: index for index, char in enumerate(CROCKFORD_ALPHABET)}
_PREFIXES_LONGEST_FIRST = tuple(sorted(PREFIXES, key=lambda name: (-len(name), name)))

_MAX_TIMESTAMP_MS = (1 << 48) - 1
_MAX_RANDOM = (1 << 80) - 1

_ulid_lock = threading.Lock()
_last_timestamp_ms = -1
_last_random = 0


def _invalid_id(
    message: str,
    *,
    field: str = "id",
    next_action: str | None = None,
) -> ContractValidationError:
    """Build the typed error used for malformed identifiers."""
    error = ContractValidationError(message, field=field, next_action=next_action)
    error.code = "invalid_id"
    return error


def _fail(message: str, *, field: str, next_action: str | None = None) -> NoReturn:
    raise _invalid_id(message, field=field, next_action=next_action)


def _encode_fixed(value: int, chars: int) -> str:
    """Encode ``value`` as ``chars`` big-endian Crockford base32 characters."""
    return "".join(
        CROCKFORD_ALPHABET[(value >> (5 * (chars - 1 - index))) & 0x1F]
        for index in range(chars)
    )


def _next_ulid() -> str:
    """Return one monotonic 26-character ULID body."""
    global _last_timestamp_ms, _last_random
    with _ulid_lock:
        now_ms = time.time_ns() // 1_000_000
        if now_ms > _last_timestamp_ms:
            _last_timestamp_ms = now_ms
            _last_random = secrets.randbits(80)
        else:
            # Same millisecond or a backwards clock step: keep the sequence
            # monotonic instead of repeating or sorting out of order.
            if _last_random >= _MAX_RANDOM:
                _last_timestamp_ms = min(_last_timestamp_ms + 1, _MAX_TIMESTAMP_MS)
                _last_random = 0
            else:
                _last_random += 1
        timestamp = min(_last_timestamp_ms, _MAX_TIMESTAMP_MS)
        return _encode_fixed(timestamp, 10) + _encode_fixed(_last_random, 16)


def _normalize_prefix(prefix: str) -> str:
    if not isinstance(prefix, str):
        _fail("id prefix must be a string", field="prefix")
    name = prefix.strip().rstrip("_")
    if not name:
        _fail("id prefix must not be empty", field="prefix", next_action="use_registered_prefix")
    return name


def _require_registered(prefix: str, *, field: str = "prefix") -> str:
    if prefix in PREFIXES:
        return prefix
    if prefix in DEPRECATED_PREFIXES:
        replacement = "peg_" if prefix == "ges" else None
        raise UnknownPrefixError(
            f"id prefix {prefix!r} is retired; gesture ids use {GESTURE_PREFIX!r}",
            field=field,
            next_action=f"use {replacement}" if replacement else None,
        )
    raise UnknownPrefixError(
        f"unknown id prefix: {prefix!r}",
        field=field,
        next_action="register_prefix",
    )


def new_id(prefix: str) -> str:
    """Mint a fresh ``prefix_<26>`` identifier.

    The trailing underscore is optional in ``prefix``; exactly one separator is
    always emitted.  Unregistered prefixes raise ``UnknownPrefixError`` before
    any random data is drawn.
    """
    name = _require_registered(_normalize_prefix(prefix))
    if name == "ast_sha256":
        _fail(
            "asset ids are content hashes, not minted ids",
            field="prefix",
            next_action="use ast_sha256_<64 lowercase hex>",
        )
    return f"{name}_{_next_ulid()}"


def _canonical(value: Any, field: str) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail(f"{field} must be finite", field=field)
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                _fail(f"{field} keys must be non-empty strings", field=field)
            result[key] = _canonical(item, f"{field}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical(item, f"{field}[]") for item in value]
    _fail(
        f"{field} must contain only JSON scalars, mappings and sequences, "
        f"got {type(value).__name__}",
        field=field,
        next_action="use_json_scalars",
    )


def canonical_key(natural_key: Mapping[str, object]) -> str:
    """Serialize a natural key as canonical JSON.

    Mapping order and tuple/list choices never change the result; values must
    stay JSON-representable so the identifier can be recomputed from a journal
    record alone.
    """
    if not isinstance(natural_key, Mapping):
        _fail("natural_key must be a mapping", field="natural_key")
    canonical = _canonical(natural_key, "natural_key")
    try:
        return json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:  # pragma: no cover - guarded above
        _fail(f"natural_key is not canonical JSON: {exc}", field="natural_key")


def derive_id(
    prefix: str,
    natural_key: Mapping[str, object],
    method_version: str,
    revision_fingerprint: str,
) -> str:
    """Derive a reproducible ``prefix_<26>`` identifier.

    Same ``natural_key``, ``method_version`` and ``revision_fingerprint`` always
    give the same identifier; a different value for any of them gives another.
    """
    name = _require_registered(_normalize_prefix(prefix))
    if name == "ast_sha256":
        _fail(
            "asset ids are content hashes, not derived ids",
            field="prefix",
            next_action="hash the asset bytes into ast_sha256_<64 hex>",
        )
    if not isinstance(natural_key, Mapping):
        _fail("natural_key must be a mapping", field="natural_key")
    if not isinstance(method_version, str) or not method_version.strip():
        _fail("method_version must be a non-empty string", field="method_version")
    if not isinstance(revision_fingerprint, str) or not revision_fingerprint.strip():
        _fail("revision_fingerprint must be a non-empty string", field="revision_fingerprint")
    payload = {
        "scheme": _ID_SCHEME,
        "prefix": name,
        "natural_key": _canonical(natural_key, "natural_key"),
        "method_version": method_version,
        "revision_fingerprint": revision_fingerprint,
    }
    blob = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(blob).digest()[:16]
    body = _encode_fixed(int.from_bytes(digest, "big") << 2, ID_BODY_LENGTH)
    return f"{name}_{body}"


def _match_prefix(value: str) -> str | None:
    for prefix in _PREFIXES_LONGEST_FIRST:
        if value.startswith(prefix + "_"):
            return prefix
    return None


def parse_id(value: str) -> tuple[str, str]:
    """Split an identifier into ``(prefix, body)``.

    Prefix matching is longest-first, so ``trk_master_…`` never parses as
    ``trk``.  The body must be exactly 26 Crockford base32 characters.
    """
    if not isinstance(value, str) or not value:
        _fail("id must be a non-empty string", field="id")
    prefix = _match_prefix(value)
    if prefix is None:
        for retired in sorted(DEPRECATED_PREFIXES, key=len, reverse=True):
            if value.startswith(retired + "_"):
                replacement = f"use {GESTURE_PREFIX}_" if retired == "ges" else None
                raise UnknownPrefixError(
                    f"id prefix {retired!r} is retired",
                    field="id",
                    next_action=replacement,
                )
        raise UnknownPrefixError(
            f"unknown id prefix in {value!r}", field="id", next_action="register_prefix"
        )
    body = value[len(prefix) + 1 :]
    if prefix == "ast_sha256":
        if len(body) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in body):
            _fail(
                "asset id body must be 64 hex characters",
                field="id",
                next_action="hash the asset bytes",
            )
        return prefix, body.lower()
    if len(body) != ID_BODY_LENGTH:
        _fail(
            f"id body must be {ID_BODY_LENGTH} characters, got {len(body)}",
            field="id",
            next_action="use_ulid_or_derive_id",
        )
    upper = body.upper()
    for char in upper:
        if char not in _CROCKFORD_INDEX:
            _fail(
                f"id body contains an invalid Crockford base32 character: {char!r}",
                field="id",
            )
    return prefix, upper


def validate_id(value: str, expected_prefix: str | None = None) -> str:
    """Return the canonical identifier or raise a typed contract error.

    ``expected_prefix`` may be given with or without its trailing underscore.
    """
    prefix, body = parse_id(value)
    if expected_prefix is not None:
        expected = _normalize_prefix(expected_prefix)
        if prefix != expected:
            _fail(
                f"id {value!r} uses prefix {prefix!r}, expected {expected!r}",
                field="id",
                next_action=f"use {expected}_",
            )
    return f"{prefix}_{body}"


def is_valid_id(value: object) -> bool:
    """True when ``value`` parses as a registered identifier."""
    try:
        parse_id(value)  # type: ignore[arg-type]
    except ValueError:
        return False
    return True


def is_derived_prefix(prefix: str) -> bool:
    """True when records with this prefix must be content-addressed."""
    if not isinstance(prefix, str):
        return False
    return prefix.strip().rstrip("_") in DERIVED_PREFIXES


def is_authored_prefix(prefix: str) -> bool:
    """True when records with this prefix are minted once per authored record."""
    if not isinstance(prefix, str):
        return False
    return prefix.strip().rstrip("_") in AUTHORED_PREFIXES


__all__ = [
    "CROCKFORD_ALPHABET",
    "ID_BODY_LENGTH",
    "GESTURE_PREFIX",
    "PREFIXES",
    "DEPRECATED_PREFIXES",
    "DERIVED_PREFIXES",
    "AUTHORED_PREFIXES",
    "new_id",
    "derive_id",
    "parse_id",
    "validate_id",
    "is_valid_id",
    "is_derived_prefix",
    "is_authored_prefix",
    "canonical_key",
]
