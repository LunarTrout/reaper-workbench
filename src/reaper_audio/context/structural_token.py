"""Structural tokens — the cheap half of token separation (Stage B3).

An edit does not need audio: it needs proof that the caller saw this exact
project map.  A structural token binds

    project_id + instance + epoch + change_count + digest of the project map
    (tracks, routing, FX, automation, tempo)

and is minted from the live project in one cheap call — no capture, no render,
no audio.  The panorama receipt stays accepted for compatibility, but it is no
longer the only way to authorise a mutation.

Tokens are signed with a per-process secret and kept in memory: a token minted
by another process, or a forged one, is rejected instead of being trusted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from ..errors import StaleContext, ValidationError
from .revision import digest_evidence

_TOKEN_PREFIX = "st_"
_MAX_TOKENS = 512
# A measurement renders through a temporary project configuration and restores
# it; REAPER still advances its state counter.  Those exact jumps are recorded
# here so a token minted before a measure stays valid after it, while any other
# change still invalidates it.
_SELF_NOISE: dict[str, list[tuple[int, int]]] = {}
_SECRET = os.urandom(32)
_TOKENS: dict[str, dict[str, Any]] = {}


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(payload), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


_NON_STRUCTURAL_FIELDS = frozenset({"project", "fx_state"})
# REAPER re-derives beat-attached item positions when render bounds move, so a
# measurement can shift an item by a few milliseconds.  Rounding keeps the token
# blind to that drift while a real move (>= 0.1 s) still changes the hash.
_POSITION_DECIMALS = 1


def _stable(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, _POSITION_DECIMALS)
    if isinstance(value, dict):
        return {key: _stable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def _structural_map_hash(digest: Mapping[str, Any]) -> str:
    """Hash the map a mutation depends on, not the render configuration.

    ``fields.project`` carries render and playback settings, which a measurement
    legitimately changes and restores; including them made every measure look
    like a structural edit.  Tracks, routing, FX, automation, items and tempo
    remain part of the hash, so a real change still invalidates the token.
    """
    fields = dict(digest.get("fields") or {})
    for name in _NON_STRUCTURAL_FIELDS:
        fields.pop(name, None)
    payload = {"digest_version": digest.get("digest_version"),
               "fields": _stable(fields),
               "coverage": dict(digest.get("coverage") or {})}
    return "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()


def _revision_bits(session: Any, project_id: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    digest = session.digest(project_id)
    context = session.snapshot(include_items=False)
    revision = dict(context.get("revision") or {})
    map_sha256 = _structural_map_hash(digest)
    return digest, revision, map_sha256


def mint(session: Any, project_id: str) -> dict[str, Any]:
    """Mint a structural token from the current project map."""
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValidationError("project_id must be a non-empty string")
    digest, revision, map_sha256 = _revision_bits(session, project_id)
    if not map_sha256:
        raise ValidationError("the bridge returned no project-map digest")
    payload = {
        "project_id": project_id,
        "instance_id": revision.get("instance_id") or digest.get("instance_id"),
        "epoch": revision.get("epoch") or digest.get("epoch"),
        "change_count": int(revision.get("change_count")
                            or digest.get("change_count") or 0),
        "map_sha256": map_sha256,
        "minted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    token = _TOKEN_PREFIX + hmac.new(_SECRET, _canonical(payload),
                                     hashlib.sha256).hexdigest()
    if len(_TOKENS) >= _MAX_TOKENS:
        oldest = sorted(_TOKENS, key=lambda key: _TOKENS[key].get("minted_at") or "")[:64]
        for stale in oldest:
            _TOKENS.pop(stale, None)
    _TOKENS[token] = payload
    return {
        "structural_token": token,
        "fingerprint": {key: payload[key] for key in
                        ("project_id", "instance_id", "epoch", "change_count",
                         "map_sha256")},
        "next_call": {
            "tool": "reaper_edit",
            "note": ("pass structural_token with expected_revision; preview needs no "
                     "token, apply and undo do. It authorises structure, not audio "
                     "claims — measurements still need their own evidence."),
        },
    }


def register_self_noise(project_id: str, before: int, after: int) -> None:
    """Remember one revision jump the library itself caused (a measurement)."""
    if not project_id or after <= before:
        return
    jumps = _SELF_NOISE.setdefault(project_id, [])
    jumps.append((int(before), int(after)))
    del jumps[:-8]


def _explained_by_self_noise(project_id: str, minted: int, current: int) -> bool:
    return any(before == minted and after == current
               for before, after in _SELF_NOISE.get(project_id, []))


def require(session: Any, project_id: str, token: str | None) -> dict[str, Any]:
    """Fail closed unless the token was minted here for this exact map."""
    if not token:
        raise ValidationError("structural_token is required for this operation")
    payload = _TOKENS.get(str(token))
    if payload is None:
        raise StaleContext(
            "This structural token was not minted by this server (or the server restarted)",
            details={"gate": "structural_token",
                     "next_action": "call reaper_structural_token for this project"})
    if payload.get("project_id") != project_id:
        raise StaleContext(
            "The structural token belongs to another project",
            details={"gate": "structural_token",
                     "token_project_id": payload.get("project_id")})
    digest, revision, current_map = _revision_bits(session, project_id)
    if current_map != payload.get("map_sha256"):
        raise StaleContext(
            "The project map changed since this token was minted",
            details={"gate": "structural_token",
                     "minted_change_count": payload.get("change_count"),
                     "current_change_count": revision.get("change_count"),
                     "next_action": "mint a fresh token, then retry"})
    current_count = revision.get("change_count")
    if (current_count is not None
            and int(current_count) != int(payload.get("change_count") or 0)
            and not _explained_by_self_noise(project_id,
                                             int(payload.get("change_count") or 0),
                                             int(current_count))):
        raise StaleContext(
            "The revision moved since this token was minted",
            details={"gate": "structural_token",
                     "minted_change_count": payload.get("change_count"),
                     "current_change_count": current_count,
                     "next_action": "mint a fresh token, then retry"})
    for key in ("instance_id", "epoch"):
        expected = payload.get(key)
        current = revision.get(key) or digest.get(key)
        if expected is not None and current is not None and str(expected) != str(current):
            raise StaleContext(
                f"The token was minted against a different {key}",
                details={"gate": "structural_token", "expected": expected,
                         "current": current})
    return payload


def seconds_since_mint(token: str) -> float | None:
    """Diagnostic only: how old a token is, in seconds."""
    payload = _TOKENS.get(token)
    if not payload:
        return None
    try:
        minted = datetime.fromisoformat(str(payload["minted_at"]))
    except (KeyError, ValueError):
        return None
    return max(0.0, time.time() - minted.timestamp())


__all__ = ["mint", "require", "seconds_since_mint"]
