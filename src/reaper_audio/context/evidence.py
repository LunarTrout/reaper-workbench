"""Evidence units and evidence tokens — Context Contract v2 §4.1 / §4.4.

An evidence unit is the atom the contract reasons about:

    (source_id, interval, method, method_version, stage, sample_rate, channels)
        -> metric: value + unit + fingerprint + capture_id + revision

Units are content-addressed: ``id = sha256(canonical unit)``.  The same
measurement produced twice maps to the same id, so a citation is stable across
process restarts and across captures with the same numbers.

An ``evidence_token`` is server-side state that names a set of unit ids and the
revision they were measured at.  It is minted only from stored units; nothing
can be cited that was never written.  Tokens are persisted under
``<archive>/state/v2_tokens/`` so a gateway restart does not silently invalidate
them (R10): an unknown or expired token fails closed with an explicit reason.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

TOKEN_SCHEMA = "reaper-workbench.evidence-token/1"
UNIT_SCHEMA = "reaper-workbench.evidence-unit/1"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unit_id(unit: Mapping[str, Any]) -> str:
    """Content address of a unit, scoped to project and revision.

    Revision is part of the identity: the same number measured at two different
    revisions is two different units, and a later revision can never overwrite
    the evidence of an earlier one.
    """
    revision = unit.get("revision") or {}
    body = {key: unit.get(key) for key in
            ("source_id", "interval", "metric", "method", "method_version",
             "stage", "sample_rate", "channels", "capture_id", "value",
             "project_id")}
    body["revision"] = {key: revision.get(key) for key in
                        ("change_count", "epoch", "instance_id")}
    return "eu_" + _sha(_canonical(body))[:40]


class EvidenceStore:
    """Append-only evidence units and server-side evidence tokens."""

    def __init__(self, archive_root: str | os.PathLike[str]) -> None:
        self.root = Path(archive_root).expanduser().absolute() / "state"
        self.units_path = self.root / "evidence_units.json"
        self.tokens_dir = self.root / "v2_tokens"
        self._units: dict[str, dict[str, Any]] | None = None
        self._units_mtime: float | None = None

    # ------------------------------------------------------------------ #
    def _load_units(self) -> dict[str, dict[str, Any]]:
        """Load units, refreshing when another process appended to the file."""
        try:
            mtime = self.units_path.stat().st_mtime_ns
        except OSError:
            mtime = None
        if self._units is not None and mtime == self._units_mtime:
            return self._units
        try:
            raw = json.loads(self.units_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError):
            # A torn store is not evidence: start empty rather than guess.
            raw = {}
        self._units = {str(key): dict(value) for key, value in raw.items()
                       if isinstance(value, Mapping)} if isinstance(raw, Mapping) else {}
        self._units_mtime = mtime
        return self._units

    def all_units(self) -> list[dict[str, Any]]:
        """Every stored unit, freshly read from disk."""
        return [dict(unit) for unit in self._load_units().values()]

    def _save_units(self) -> None:
        assert self._units is not None
        self.root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._units, ensure_ascii=False, sort_keys=True,
                             indent=0)
        self._atomic_write(self.units_path, payload)

    @staticmethod
    def _atomic_write(path: Path, payload: str) -> None:
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8",
                                             dir=str(path.parent),
                                             prefix=f".{path.name}.",
                                             suffix=".tmp", delete=False)
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, path)
        finally:
            try:
                if os.path.exists(handle.name):
                    os.unlink(handle.name)
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    def put(self, unit: Mapping[str, Any]) -> str:
        """Store one unit (idempotent) and return its id."""
        return self.put_many([unit])[0]

    def put_many(self, units: Iterable[Mapping[str, Any]]) -> list[str]:
        """Store many units with one atomic write; returns ids in order."""
        identifiers: list[str] = []
        stored = self._load_units()
        changed = False
        for unit in units:
            body = dict(unit)
            body["schema"] = UNIT_SCHEMA
            identifier = unit_id(body)
            body["id"] = identifier
            identifiers.append(identifier)
            if stored.get(identifier) != body:
                stored[identifier] = body
                changed = True
        if changed:
            self._save_units()
            try:
                self._units_mtime = self.units_path.stat().st_mtime_ns
            except OSError:
                self._units_mtime = None
        return identifiers

    def get(self, identifier: str) -> dict[str, Any] | None:
        entry = self._load_units().get(identifier)
        return dict(entry) if entry is not None else None

    def get_many(self, identifiers: Sequence[str]) -> list[dict[str, Any]]:
        return [row for identifier in identifiers
                if (row := self.get(identifier)) is not None]

    # ------------------------------------------------------------------ #
    def mint_token(self, *, project_id: str, revision: Mapping[str, Any],
                   units: Sequence[str], ttl_s: int | None = None) -> dict[str, Any]:
        stored = self.get_many(units)
        if len(stored) != len(units):
            missing = sorted(set(units) - {row["id"] for row in stored})
            raise KeyError(f"unknown evidence units: {missing[:5]}")
        token_id = "et_" + _sha(_canonical({"units": list(units),
                                            "revision": dict(revision),
                                            "nonce": time.time_ns()}))[:32]
        token = {
            "schema": TOKEN_SCHEMA,
            "token": token_id,
            "kind": "evidence",
            "project_id": project_id,
            "revision": dict(revision),
            "units": list(units),
            "unit_count": len(units),
            "minted_at": time.time(),
            "expires_at": (time.time() + ttl_s) if ttl_s else None,
        }
        self.tokens_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self.tokens_dir / f"{token_id}.json",
                           json.dumps(token, ensure_ascii=False, indent=0))
        return token

    def load_token(self, token_id: str) -> dict[str, Any]:
        path = self.tokens_dir / f"{token_id}.json"
        try:
            token = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KeyError(f"unknown token: {token_id}") from None
        except (OSError, ValueError) as exc:
            raise KeyError(f"unreadable token: {token_id}") from exc
        expires = token.get("expires_at")
        if isinstance(expires, (int, float)) and expires < time.time():
            raise KeyError(f"expired token: {token_id}")
        return token


__all__ = ["EvidenceStore", "TOKEN_SCHEMA", "UNIT_SCHEMA", "unit_id"]
