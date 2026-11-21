"""Append-only decision journal: why every edit happened, one JSONL per project.

The edit layer already produces a receipt with the operations it applied, the
values it read back and whether verification matched.  A receipt answers *what*
changed; it cannot answer *why* the change was made.  This module is the
durable, human-readable trajectory: each ``apply`` and each ``undo`` appends one
JSON object carrying the mandatory reason, optional references to the facts,
measurements, receipts or state that justified it, and the receipt payloads the
library attached.

Measurement is deliberately absent.  Measuring does not change the project, so
journaling it would make the trajectory look like a series of edits.

Layout and durability
---------------------

::

    <archive>/projects/<safe_project_key>/journal.jsonl

``<safe_project_key>`` is the same readable slug plus SHA-256 suffix that
:mod:`reaper_audio.context.project_state` uses, so a project's state and journal
always live in the same directory.  One JSON object per line, UTF-8, compact.
Appending writes the newline-terminated line with a single ``os.write`` on an
``O_APPEND`` descriptor, then ``fsync``-es before returning.  A reader therefore
sees either the previous complete lines or the new complete line; only a crash
in the middle of the write can leave a torn final line, which the reader skips
and reports as ``torn_line`` instead of failing the whole file.

There is no file lock.  One process/session owns one project's journal at a
time (the same single-writer rule as the project state).  In-process appends are
serialised with a module lock; two concurrent processes appending to one file
would still each write whole lines, but the sequence numbers they choose could
collide.  A future multi-process writer must take an inter-process lock around
*read head + append*, not inside this module.

Line schema (``reaper-workbench.decision-journal/1``, frozen)
-------------------------------------------------------------

::

    {
      "seq": 7,                          # 1-based, monotonic per project
      "at": "2026-11-21T12:34:56.789Z",  # UTC ISO-8601, milliseconds
      "project_id": "reaper:{GUID}:/songs/song.rpp",
      "kind": "apply" | "undo",
      "receipt": "op_...",               # the receipt this entry belongs to
      "why": "tame the lead so the vocal sits forward",
      "basis": [{"kind": "measurement", "id": "fact_..."}],
      "expected": {"volume_db": -2.0} | null,
      "operations": [{"kind": "track.set", "target_guid": "...",
                      "values": {...}}],
      "readback": {...} | null,
      "verify": {...} | null,
      "revision_before": {...} | null,
      "revision_after": {...} | null,
      "fx_edits": [...],
      "undoes": "op_..." | null
    }

``why`` is mandatory and non-empty: an edit without a stated reason is refused
with ``journal_required``.  ``basis`` entries are ``{kind, id}`` with ``kind``
in ``fact``/``measurement``/``receipt``/``state``; they are references, never
copies, so the trajectory can be followed back to the exact fact or receipt.
``expected`` is the caller's claim about the outcome; ``readback`` and
``verify`` are the library's evidence.  An ``undo`` names the receipt it
reverses in ``undoes``; its own ``receipt`` is the new receipt produced by the
undo transaction.

Invariants
----------

* Lines are append-only.  This module never rewrites, reorders or deletes one.
* ``seq`` is assigned by :meth:`JournalStore.append` as ``head + 1`` where
  ``head`` is the highest sequence of a *complete* line.  A torn final line is
  invisible to the counter, so its number may be reused; it was never readable.
* Appending to a file whose last byte is not a newline first writes a newline,
  so the torn fragment becomes one (unreadable) line and the new record stays
  separate.  The fragment is retained, not silently erased.
* A record is refused, before any I/O, when it carries a secret-bearing key
  (password, api key, bearer token, ...) or raw audio (samples, PCM, bytes).
  Reasons and measurements are text and numbers; audio belongs in the capture
  archive, not in a decision log.
* ``read`` filters after parsing, never changes the file, and reports
  ``torn_line`` separately from the entries it could decode.  ``next_seq`` is
  the cursor to pass back as ``since_seq``: the last returned sequence when the
  ``limit`` cut the result, otherwise the journal head.  Polling with
  ``since_seq=next_seq`` therefore neither skips nor repeats an entry.

Nothing here reads the clock beyond stamping a new entry, talks to REAPER or
knows a project name, threshold or track count.
"""

from __future__ import annotations

import copy
import json
import math
import os
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import ValidationError
from .project_state import PROJECTS_DIRNAME, safe_project_key

__all__ = [
    "BASIS_KINDS",
    "ENTRY_KINDS",
    "JOURNAL_FILENAME",
    "MAX_FX_EDITS",
    "MAX_OPERATIONS",
    "RAW_AUDIO_KEYS",
    "SCHEMA",
    "SECRET_KEYS",
    "JournalRequired",
    "JournalStore",
    "make_entry",
    "utc_now",
]

#: Document id of the line schema.  A different shape needs a new id.
SCHEMA = "reaper-workbench.decision-journal/1"

#: File name inside ``<archive>/projects/<safe_project_key>/``.
JOURNAL_FILENAME = "journal.jsonl"

#: Reference kinds accepted in ``basis``.
BASIS_KINDS: tuple[str, ...] = ("fact", "measurement", "receipt", "state")

#: Entry kinds.  ``undo`` entries also carry ``undoes``.
ENTRY_KINDS: tuple[str, ...] = ("apply", "undo")

#: Mirrors the edit layer's per-batch operation cap.
MAX_OPERATIONS = 256

#: A defensive cap; an apply touches a handful of FX parameters, not thousands.
MAX_FX_EDITS = 1024

#: Keys whose presence means a credential is about to be written.  Normalised
#: to lower case with ``-``/spaces as ``_``.  ``token`` alone is deliberately
#: absent: the project-state token is ordinary data.
SECRET_KEYS = frozenset({
    "api_key",
    "api_secret",
    "apikey",
    "authorization",
    "bearer",
    "bearer_token",
    "client_secret",
    "cookie",
    "credentials",
    "credential",
    "access_token",
    "auth_token",
    "password",
    "passphrase",
    "passwd",
    "private_key",
    "refresh_token",
    "secret",
    "session_cookie",
    "session_key",
    "signing_key",
    "x_api_key",
})

#: Keys whose presence means raw audio is about to be written.  Binary values
#: are refused under any key; these names catch the list-of-floats form.
RAW_AUDIO_KEYS = frozenset({
    "audio_bytes",
    "audio_data",
    "audio_samples",
    "buffer",
    "buffers",
    "float_samples",
    "interleaved_samples",
    "pcm",
    "pcm_data",
    "raw_audio",
    "raw_samples",
    "sample_data",
    "sample_frames",
    "samples",
    "waveform",
    "waveform_data",
})

_ENTRY_FIELDS: tuple[str, ...] = (
    "seq",
    "at",
    "project_id",
    "kind",
    "receipt",
    "why",
    "basis",
    "expected",
    "operations",
    "readback",
    "verify",
    "revision_before",
    "revision_after",
    "fx_edits",
    "undoes",
)

_REQUIRED_ENTRY_FIELDS = ("project_id", "receipt", "why", "operations")

_APPEND_LOCK = threading.RLock()


class JournalRequired(ValidationError):
    """An apply or undo arrived without the human reason it needs."""

    code = "journal_required"


# --------------------------------------------------------------------------- #
# small value normalisers
# --------------------------------------------------------------------------- #


def utc_now() -> str:
    """Current UTC instant as ISO-8601 with milliseconds and ``Z``."""
    return (datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"))


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string",
                              details={"field": field, "type": type(value).__name__})
    return value.strip()


def _require_why(value: object) -> str:
    """The one field the owner will not let an edit omit."""
    if not isinstance(value, str) or not value.strip():
        raise JournalRequired(
            "every apply and every undo needs a non-empty why",
            details={"field": "why", "hint": ("state the audible or structural "
                                              "reason for this change")},
        )
    return value.strip()


def _normalise_at(value: object) -> str:
    """A caller-supplied timestamp is accepted only as an aware ISO instant."""
    if value is None:
        return utc_now()
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("at must be an ISO-8601 string",
                              details={"field": "at"})
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("at is not a valid ISO-8601 instant",
                              details={"field": "at", "value": text}) from exc
    if parsed.tzinfo is None:
        raise ValidationError("at must carry a UTC offset",
                              details={"field": "at", "value": text})
    return (parsed.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"))


def _receipt_id(value: object) -> str:
    """Accept a receipt id, a receipt mapping, or an ``EditReceipt`` object."""
    if isinstance(value, str):
        return _require_text(value, "receipt")
    if isinstance(value, Mapping):
        candidate = value.get("receipt_id")
    else:
        candidate = getattr(value, "receipt_id", None)
    if candidate is None:
        raise ValidationError(
            "receipt must be a receipt id, a receipt object or a JSON object "
            "carrying receipt_id",
            details={"field": "receipt", "type": type(value).__name__},
        )
    return _require_text(candidate, "receipt.receipt_id")


def _receipt_payload(receipt: object, name: str) -> Mapping[str, Any] | None:
    """One evidence payload already attached to a receipt object/mapping."""
    if isinstance(receipt, Mapping):
        value = receipt.get(name)
    else:
        value = getattr(receipt, name, None)
    return value if isinstance(value, Mapping) else None


def _optional_object(value: object, field: str) -> dict[str, Any] | None:
    """A JSON object or ``None``; lists, scalars and bytes are refused."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError(f"{field} must be a JSON object or null",
                              details={"field": field, "type": type(value).__name__})
    return copy.deepcopy(dict(value))


def _normalise_basis(value: object) -> list[dict[str, str]]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValidationError("basis must be a list of {kind, id} references",
                              details={"field": "basis", "type": type(value).__name__})
    references: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValidationError("each basis entry must be a {kind, id} object",
                                  details={"field": f"basis[{index}]"})
        unknown = set(item) - {"kind", "id"}
        if unknown:
            raise ValidationError(
                "a basis entry carries only kind and id",
                details={"field": f"basis[{index}]",
                         "unknown": sorted(str(name) for name in unknown)},
            )
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in BASIS_KINDS:
            raise ValidationError(
                "basis kind must be fact, measurement, receipt or state",
                details={"field": f"basis[{index}].kind", "value": kind,
                         "allowed": list(BASIS_KINDS)},
            )
        references.append({"kind": kind,
                           "id": _require_text(item.get("id"),
                                               f"basis[{index}].id")})
    return references


def _normalise_operations(value: object) -> list[dict[str, Any]]:
    """The operations the receipt applied; each names its target object."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValidationError("operations must be a list of operation objects",
                              details={"field": "operations",
                                       "type": type(value).__name__})
    if not value:
        raise ValidationError("operations must contain at least one operation",
                              details={"field": "operations"})
    if len(value) > MAX_OPERATIONS:
        raise ValidationError("operations exceeds the journal limit",
                              details={"field": "operations", "count": len(value),
                                       "limit": MAX_OPERATIONS})
    operations: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValidationError("each operation must be a JSON object",
                                  details={"field": f"operations[{index}]"})
        kind = _require_text(item.get("kind"), f"operations[{index}].kind")
        target = _require_text(item.get("target_guid"),
                               f"operations[{index}].target_guid")
        operation: dict[str, Any] = {"kind": kind, "target_guid": target}
        values = item.get("values")
        if values is not None:
            if not isinstance(values, Mapping):
                raise ValidationError(
                    "operation values must be a JSON object",
                    details={"field": f"operations[{index}].values"},
                )
            operation["values"] = copy.deepcopy(dict(values))
        for key, item_value in item.items():
            if key in operation or key in {"kind", "target_guid", "values"}:
                continue
            operation[key] = copy.deepcopy(item_value)
        operations.append(operation)
    return operations


def _normalise_fx_edits(value: object) -> list[dict[str, Any]]:
    """Optional per-FX edits: a list of objects, never a bare mapping."""
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValidationError("fx_edits must be a list of JSON objects",
                              details={"field": "fx_edits",
                                       "type": type(value).__name__})
    if len(value) > MAX_FX_EDITS:
        raise ValidationError("fx_edits exceeds the journal limit",
                              details={"field": "fx_edits", "count": len(value),
                                       "limit": MAX_FX_EDITS})
    edits: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValidationError("each fx_edits entry must be a JSON object",
                                  details={"field": f"fx_edits[{index}]"})
        edits.append(copy.deepcopy(dict(item)))
    return edits


def _normalise_undoes(value: object) -> str | None:
    if value is None:
        return None
    return _receipt_id(value)


def _normalised_key(key: str) -> str:
    return key.strip().lower().replace("-", "_").replace(" ", "_")


def _assert_safe(value: object, *, path: str = "entry") -> None:
    """Refuse credentials, raw audio and binary payloads anywhere in an entry."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ValidationError(
            "journal entries must not contain binary or raw audio data",
            details={"field": path, "type": type(value).__name__},
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValidationError("journal keys must be strings",
                                      details={"field": path})
            name = _normalised_key(key)
            if name in SECRET_KEYS:
                raise ValidationError(
                    "journal entries must not contain secrets",
                    details={"field": f"{path}.{key}", "reason": "secret_key"},
                )
            if name in RAW_AUDIO_KEYS:
                raise ValidationError(
                    "journal entries must not contain raw audio",
                    details={"field": f"{path}.{key}", "reason": "raw_audio_key"},
                )
            _assert_safe(item, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_safe(item, path=f"{path}[{index}]")
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError("journal entries must not contain NaN or infinity",
                              details={"field": path})


def _assert_json_encodable(value: Mapping[str, Any]) -> None:
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False)
        payload.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValidationError("journal entry is not JSON-encodable",
                              details={"reason": str(exc)}) from exc


# --------------------------------------------------------------------------- #
# entries
# --------------------------------------------------------------------------- #


def make_entry(
    *,
    project_id: str,
    receipt: object,
    operations: Sequence[Mapping[str, Any]],
    why: str,
    basis: Sequence[Mapping[str, Any]] | None = None,
    expected: Mapping[str, Any] | None = None,
    readback: Mapping[str, Any] | None = None,
    verify: Mapping[str, Any] | None = None,
    revision_before: Mapping[str, Any] | None = None,
    revision_after: Mapping[str, Any] | None = None,
    fx_edits: Sequence[Mapping[str, Any]] | None = None,
    undoes: object = None,
) -> dict[str, Any]:
    """Build one validated journal entry, without a sequence number.

    ``receipt`` may be the receipt id or an object/mapping carrying
    ``receipt_id``; only the id is stored, because the receipt itself already
    lives in the receipt store.  When that object carries ``readback``,
    ``verify``, ``revision_before`` or ``revision_after`` and the caller leaves
    the corresponding argument as ``None``, the payloads are attached from the
    receipt automatically.  ``undoes`` names the earlier receipt this entry
    reverses and makes the entry an ``undo``; without it the entry is an
    ``apply``.  The sequence is assigned by :meth:`JournalStore.append`, the
    only writer.

    Raises :class:`JournalRequired` (a ``ValidationError`` with code
    ``journal_required``) when ``why`` is missing, blank or not a string, and
    :class:`ValidationError` for malformed basis/expected/operations or for any
    secret-bearing or raw-audio field.
    """
    project = _require_text(project_id, "project_id")
    reason = _require_why(why)
    receipt_id = _receipt_id(receipt)
    # The library attaches the evidence: when the caller passes the receipt
    # object and leaves readback/verify/revisions out, take them from it
    # rather than recording a change with no evidence.
    if readback is None:
        readback = _receipt_payload(receipt, "readback")
    if verify is None:
        verify = _receipt_payload(receipt, "verify")
    if revision_before is None:
        revision_before = _receipt_payload(receipt, "revision_before")
    if revision_after is None:
        revision_after = _receipt_payload(receipt, "revision_after")
    operation_list = _normalise_operations(operations)
    basis_list = _normalise_basis(basis)
    expected_obj = _optional_object(expected, "expected")
    readback_obj = _optional_object(readback, "readback")
    verify_obj = _optional_object(verify, "verify")
    before_obj = _optional_object(revision_before, "revision_before")
    after_obj = _optional_object(revision_after, "revision_after")
    fx_list = _normalise_fx_edits(fx_edits)
    undo_id = _normalise_undoes(undoes)
    if undo_id is not None and undo_id == receipt_id:
        raise ValidationError(
            "an undo entry must reference the receipt it reverses, not its own",
            details={"field": "undoes", "receipt": receipt_id},
        )
    kind = "undo" if undo_id is not None else "apply"
    entry: dict[str, Any] = {
        "at": utc_now(),
        "project_id": project,
        "kind": kind,
        "receipt": receipt_id,
        "why": reason,
        "basis": basis_list,
        "expected": expected_obj,
        "operations": operation_list,
        "readback": readback_obj,
        "verify": verify_obj,
        "revision_before": before_obj,
        "revision_after": after_obj,
        "fx_edits": fx_list,
        "undoes": undo_id,
    }
    _assert_safe(entry)
    _assert_json_encodable(entry)
    return entry


def _entry_fields(entry: object) -> dict[str, Any]:
    """Validate a stored-shaped entry and return its normalised fields."""
    if not isinstance(entry, Mapping):
        raise ValidationError("a journal entry must be a JSON object",
                              details={"type": type(entry).__name__})
    unknown = set(entry) - set(_ENTRY_FIELDS)
    if unknown:
        raise ValidationError(
            "journal entry contains fields outside the line schema",
            details={"unknown": sorted(str(name) for name in unknown),
                     "allowed": list(_ENTRY_FIELDS)},
        )
    missing = [name for name in _REQUIRED_ENTRY_FIELDS
               if name not in entry and name != "why"]
    if missing:
        raise ValidationError("journal entry is missing required fields",
                              details={"missing": missing})
    undo_id = _normalise_undoes(entry.get("undoes"))
    kind = entry.get("kind")
    if kind is None:
        kind = "undo" if undo_id is not None else "apply"
    if kind not in ENTRY_KINDS:
        raise ValidationError("kind must be apply or undo",
                              details={"kind": kind, "allowed": list(ENTRY_KINDS)})
    if kind == "undo" and undo_id is None:
        raise ValidationError("an undo entry must name the receipt it undoes",
                              details={"field": "undoes"})
    if kind == "apply" and undo_id is not None:
        raise ValidationError("an apply entry must not carry undoes",
                              details={"field": "undoes", "value": undo_id})
    fields: dict[str, Any] = {
        "at": _normalise_at(entry.get("at")),
        "project_id": _require_text(entry.get("project_id"), "project_id"),
        "kind": kind,
        "receipt": _receipt_id(entry.get("receipt")),
        "why": _require_why(entry.get("why")),
        "basis": _normalise_basis(entry.get("basis")),
        "expected": _optional_object(entry.get("expected"), "expected"),
        "operations": _normalise_operations(entry.get("operations")),
        "readback": _optional_object(entry.get("readback"), "readback"),
        "verify": _optional_object(entry.get("verify"), "verify"),
        "revision_before": _optional_object(entry.get("revision_before"),
                                            "revision_before"),
        "revision_after": _optional_object(entry.get("revision_after"),
                                           "revision_after"),
        "fx_edits": _normalise_fx_edits(entry.get("fx_edits")),
        "undoes": undo_id,
    }
    _assert_safe(fields)
    _assert_json_encodable(fields)
    return fields


def _compose(seq: int, fields: Mapping[str, Any]) -> dict[str, Any]:
    """One stored record in the frozen field order, sequence first."""
    return {
        "seq": seq,
        "at": fields["at"],
        "project_id": fields["project_id"],
        "kind": fields["kind"],
        "receipt": fields["receipt"],
        "why": fields["why"],
        "basis": copy.deepcopy(fields["basis"]),
        "expected": copy.deepcopy(fields["expected"]),
        "operations": copy.deepcopy(fields["operations"]),
        "readback": copy.deepcopy(fields["readback"]),
        "verify": copy.deepcopy(fields["verify"]),
        "revision_before": copy.deepcopy(fields["revision_before"]),
        "revision_after": copy.deepcopy(fields["revision_after"]),
        "fx_edits": copy.deepcopy(fields["fx_edits"]),
        "undoes": fields["undoes"],
    }


# --------------------------------------------------------------------------- #
# file helpers
# --------------------------------------------------------------------------- #


def _head_seq(path: Path, *, window: int = 65536) -> int:
    """Highest sequence among complete lines; 0 when there is none.

    The window grows until it covers the file or yields a parseable sequence.
    An entry longer than the window, or a torn fragment hiding the long line
    before it, therefore never causes a sequence to be reused: the scan keeps
    expanding instead of trusting an unparsable tail.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size == 0:
                return 0
            current = max(1, window)
            while True:
                start = max(0, size - current)
                handle.seek(start)
                payload = handle.read(size - start)
                first_is_partial = False
                if start > 0:
                    handle.seek(start - 1)
                    first_is_partial = handle.read(1) != b"\n"
                if first_is_partial:
                    # The first segment is the tail of a line cut by the window.
                    _, separator, rest = payload.partition(b"\n")
                    payload = rest if separator else b""
                segments = payload.split(b"\n")
                if segments and segments[-1] == b"":
                    segments.pop()
                for segment in reversed(segments):
                    text = segment.strip()
                    if not text:
                        continue
                    try:
                        value = json.loads(text.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        continue
                    seq = value.get("seq") if isinstance(value, dict) else None
                    if (isinstance(seq, int) and not isinstance(seq, bool)
                            and seq >= 0):
                        return seq
                if start == 0:
                    return 0
                current *= 2
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise ValidationError("journal is unreadable",
                              details={"path": str(path),
                                       "reason": str(exc)}) from exc


def _needs_separator(path: Path) -> bool:
    """True when the file exists, is non-empty, and lacks a final newline."""
    try:
        if path.stat().st_size == 0:
            return False
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b"\n"
    except FileNotFoundError:
        return False


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # pragma: no cover - defensive
            raise OSError("journal write returned no progress")
        view = view[written:]


def _append_line(path: Path, payload: bytes) -> None:
    """Append one newline-terminated line, durable before returning."""
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o644)
    try:
        if _needs_separator(path):
            _write_all(descriptor, b"\n")
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if created:
        # Best effort: make the directory entry durable on POSIX.  Windows
        # cannot open a directory for fsync, which is why this is guarded.
        directory = None
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory = None
        if directory is not None:
            try:
                os.fsync(directory)
            except OSError:
                pass
            finally:
                os.close(directory)


def _parse_records(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """All readable entries plus whether the final line was torn."""
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return [], False
    except OSError as exc:
        raise ValidationError("journal is unreadable",
                              details={"path": str(path),
                                       "reason": str(exc)}) from exc
    if not payload:
        return [], False
    ends_with_newline = payload.endswith(b"\n")
    segments = payload.split(b"\n")
    if ends_with_newline:
        segments = segments[:-1]
    records: list[dict[str, Any]] = []
    torn = False
    last_index = len(segments) - 1
    for index, segment in enumerate(segments):
        text = segment.strip()
        if not text:
            continue
        try:
            value = json.loads(text.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            if index == last_index and not ends_with_newline:
                torn = True
            continue
        if not isinstance(value, dict):
            if index == last_index and not ends_with_newline:
                torn = True
            continue
        seq = value.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            continue
        records.append(value)
    records.sort(key=lambda record: record["seq"])
    return records, torn


def _mentions_track(entry: Mapping[str, Any], track: str) -> bool:
    operations = entry.get("operations")
    if not isinstance(operations, list):
        return False
    return any(isinstance(operation, Mapping)
               and operation.get("target_guid") == track
               for operation in operations)


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #


class JournalStore:
    """Path layout plus append/read for per-project decision journals.

    ``JournalStore(archive_root)`` is an archive-level handle: :meth:`append`
    derives the project from the entry, and :meth:`read` uses the project given
    at construction, the project this store last appended to, or the archive's
    only journal (in that order).  With several journals present, an unbound
    ``read`` refuses instead of guessing; pass ``project_id=`` or use
    :meth:`for_project`.  See the module docstring for the single-writer rule.
    """

    def __init__(self, root: str | os.PathLike[str],
                 project_id: str | None = None) -> None:
        if not isinstance(root, (str, os.PathLike)):
            raise ValidationError("archive root must be a path")
        self.root = Path(root).expanduser().absolute()
        self.projects_root = self.root / PROJECTS_DIRNAME
        self._project_id = (_require_text(project_id, "project_id")
                            if project_id is not None else None)
        self._last_project_id: str | None = None

    def for_project(self, project_id: str) -> "JournalStore":
        """A handle bound to one project, sharing this archive root."""
        return JournalStore(self.root, project_id=project_id)

    def path_for(self, project_id: str) -> Path:
        """``<root>/projects/<safe_project_key>/journal.jsonl``."""
        return self.projects_root / safe_project_key(project_id) / JOURNAL_FILENAME

    # -- write -------------------------------------------------------------- #

    def append(self, entry: object, *, project_id: str | None = None) -> dict[str, Any]:
        """Append one validated entry, assign its sequence and return it.

        The sequence is ``head + 1`` where ``head`` is the last complete line.
        The write is one newline-terminated line on an ``O_APPEND`` descriptor,
        followed by ``fsync``; a torn final fragment is separated by a newline
        and left in place rather than erased.  Nothing is written when the
        entry fails validation.
        """
        fields = _entry_fields(entry)
        entry_project = fields["project_id"]
        if project_id is not None:
            requested = _require_text(project_id, "project_id")
            if requested != entry_project:
                raise ValidationError(
                    "entry belongs to a different project",
                    details={"expected": requested, "entry": entry_project},
                )
        if self._project_id is not None and self._project_id != entry_project:
            raise ValidationError(
                "entry belongs to a different project than this journal handle",
                details={"expected": self._project_id, "entry": entry_project},
            )
        path = self.path_for(entry_project)
        with _APPEND_LOCK:
            seq = _head_seq(path) + 1
            record = _compose(seq, fields)
            payload = json.dumps(record, ensure_ascii=False,
                                 separators=(",", ":"), allow_nan=False) + "\n"
            _append_line(path, payload.encode("utf-8"))
        self._last_project_id = entry_project
        return copy.deepcopy(record)

    # -- read --------------------------------------------------------------- #

    def read(
        self,
        project_id: str | None = None,
        *,
        receipt: str | None = None,
        track: str | None = None,
        since_seq: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Read entries with filters applied; the file is never rewritten.

        ``receipt`` matches the entry's receipt id; ``track`` matches any
        operation whose ``target_guid`` is that object; ``since_seq`` returns
        only entries with a greater sequence.  ``limit`` caps the page.
        ``next_seq`` is the cursor for the next call (the last returned
        sequence when more matching entries remain, else the journal head).
        A torn final line is skipped and reported in ``torn_line``; it never
        hides the complete lines before it.
        """
        if isinstance(since_seq, bool) or not isinstance(since_seq, int) or since_seq < 0:
            raise ValidationError("since_seq must be a non-negative integer",
                                  details={"since_seq": since_seq})
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValidationError("limit must be a positive integer",
                                  details={"limit": limit})
        if receipt is not None:
            receipt = _require_text(receipt, "receipt")
        if track is not None:
            track = _require_text(track, "track")

        path, _resolved = self._read_path(project_id)
        if path is None:
            return {"entries": [], "next_seq": since_seq, "torn_line": False}
        records, torn = _parse_records(path)
        matches = [
            record for record in records
            if record["seq"] > since_seq
            and (receipt is None or record.get("receipt") == receipt)
            and (track is None or _mentions_track(record, track))
        ]
        head = records[-1]["seq"] if records else 0
        page = matches[:limit]
        if len(matches) > len(page):
            next_seq = page[-1]["seq"]
        else:
            next_seq = max(since_seq, head)
        return {"entries": page, "next_seq": next_seq, "torn_line": torn}

    def _read_path(self, project_id: str | None) -> tuple[Path | None, str | None]:
        """Resolve which journal to read; ``(None, None)`` means empty archive."""
        if project_id is not None:
            resolved = _require_text(project_id, "project_id")
            return self.path_for(resolved), resolved
        if self._project_id is not None:
            return self.path_for(self._project_id), self._project_id
        if self._last_project_id is not None:
            return self.path_for(self._last_project_id), self._last_project_id
        candidates = sorted(self.projects_root.glob(f"*/{JOURNAL_FILENAME}"))
        if not candidates:
            return None, None
        if len(candidates) > 1:
            raise ValidationError(
                "read needs a project_id: this archive holds several journals",
                details={"journals": [path.parent.name for path in candidates]},
            )
        return candidates[0], None
