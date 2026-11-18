"""Server work modes — Context Contract v2 §4.14 / Stage E1–E2.

The user picks the rules at session start; the model never changes them on its
own.  Three modes:

* ``strict`` (default) — structural token for edits, evidence for audio claims,
  a passport for ``global`` claims, at most three blockers;
* ``advisory`` — the same checks run and are returned, but they do not block:
  the action proceeds marked ``unverified`` with the list of what is missing;
* ``off`` — no evidence context at all; only project/instance/epoch/revision
  identity is checked so nothing is written into a foreign or stale project.

State lives in one JSON file outside the server process so a restart cannot
silently reset the user's rules, and ``locked`` can only be cleared by editing
that file (or the CLI), never through MCP.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MODES = ("strict", "advisory", "off")
DEFAULT_MODE = "strict"
DEFAULT_STALE_PCT = 50
JOURNAL_SCHEMA = "reaper-workbench.mode-journal/1"


class ModeError(ValueError):
    """An invalid mode request (bad value, not a user request, or locked)."""


@dataclass(frozen=True)
class ModeState:
    mode: str = DEFAULT_MODE
    stale_threshold_pct: int = DEFAULT_STALE_PCT
    locked: bool = False
    updated_at: str = ""
    updated_by: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _state_dir() -> Path:
    override = os.environ.get("REAPER_MCP_MODE_DIR")
    if override and override.strip():
        return Path(override).expanduser().absolute()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "REAPER-Audio-Library" / "runtime"
    return Path.home() / ".reaper-audio-library"


def state_path() -> Path:
    return _state_dir() / "mode.json"


def journal_path() -> Path:
    return _state_dir() / "mode-journal.jsonl"


def _validate(mode: str, pct: int) -> tuple[str, int]:
    if mode not in MODES:
        raise ModeError(f"mode must be one of {MODES}")
    if isinstance(pct, bool) or not isinstance(pct, int) or not 0 <= pct <= 100:
        raise ModeError("stale_threshold_pct must be an integer 0-100")
    return mode, pct


def load_mode() -> ModeState:
    """Read the user's rules.  A malformed file fails closed to ``strict``."""
    path = state_path()
    if not path.is_file():
        return ModeState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ModeState()
    if not isinstance(raw, Mapping):
        return ModeState()
    mode = raw.get("mode", DEFAULT_MODE)
    pct = raw.get("stale_threshold_pct", DEFAULT_STALE_PCT)
    try:
        mode, pct = _validate(str(mode), int(pct))
    except (ModeError, TypeError, ValueError):
        return ModeState()
    return ModeState(mode=mode, stale_threshold_pct=pct,
                     locked=bool(raw.get("locked", False)),
                     updated_at=str(raw.get("updated_at") or ""),
                     updated_by=str(raw.get("updated_by") or ""),
                     reason=str(raw.get("reason") or ""))


def _write_state(state: ModeState) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix="mode-", suffix=".tmp", delete=False) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _append_journal(entry: Mapping[str, Any]) -> None:
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(entry), ensure_ascii=False, sort_keys=True) + "\n")


def set_mode(*, mode: str | None = None, stale_threshold_pct: int | None = None,
             reason: str, requested_by: str, external: bool = False) -> ModeState:
    """Change the rules.  Only a direct user request may call this.

    ``external`` is reserved for a CLI/config path outside MCP: it is the only
    way to change state while ``locked`` is set.
    """
    current = load_mode()
    if current.locked and not external:
        raise ModeError("mode is locked; it can only be changed outside MCP")
    if not isinstance(reason, str) or not reason.strip():
        raise ModeError("a mode change needs a stated reason")
    if not isinstance(requested_by, str) or not requested_by.strip():
        raise ModeError("a mode change needs to name who asked for it")
    new_mode, new_pct = _validate(current.mode if mode is None else mode,
                                  current.stale_threshold_pct
                                  if stale_threshold_pct is None else stale_threshold_pct)
    updated = replace(current, mode=new_mode, stale_threshold_pct=new_pct,
                      updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      updated_by=requested_by, reason=reason.strip())
    _write_state(updated)
    _append_journal({
        "schema": JOURNAL_SCHEMA,
        "at": updated.updated_at,
        "from": {"mode": current.mode, "stale_threshold_pct": current.stale_threshold_pct,
                 "locked": current.locked},
        "to": {"mode": updated.mode, "stale_threshold_pct": updated.stale_threshold_pct,
               "locked": updated.locked},
        "requested_by": updated.updated_by,
        "reason": updated.reason,
    })
    return updated


def set_locked(locked: bool, *, reason: str, requested_by: str,
               external: bool = False) -> ModeState:
    """Lock or unlock the rules.  Unlocking from inside MCP is refused."""
    current = load_mode()
    if not locked and not external:
        raise ModeError("unlocking is only possible outside MCP (config, file, CLI)")
    if current.locked and not external and locked:
        raise ModeError("mode is locked; it can only be changed outside MCP")
    updated = replace(current, locked=locked,
                      updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      updated_by=requested_by, reason=reason.strip())
    _write_state(updated)
    _append_journal({
        "schema": JOURNAL_SCHEMA,
        "at": updated.updated_at,
        "from": {"mode": current.mode, "locked": current.locked},
        "to": {"mode": updated.mode, "locked": updated.locked},
        "requested_by": requested_by,
        "reason": reason.strip(),
    })
    return updated


def mode_brief() -> dict[str, Any]:
    """The one-line view attached to every response."""
    state = load_mode()
    return {"mode": state.mode, "stale_threshold_pct": state.stale_threshold_pct,
            "locked": state.locked}


def stale_threshold_fraction(state: ModeState | None = None) -> float | None:
    """The share as a 0-1 fraction; ``100`` disables the trigger."""
    state = state or load_mode()
    if state.stale_threshold_pct >= 100:
        return None
    return state.stale_threshold_pct / 100.0


__all__ = ["DEFAULT_MODE", "DEFAULT_STALE_PCT", "JOURNAL_SCHEMA", "MODES", "ModeError",
           "ModeState", "journal_path", "load_mode", "mode_brief", "set_locked",
           "set_mode", "stale_threshold_fraction", "state_path"]
