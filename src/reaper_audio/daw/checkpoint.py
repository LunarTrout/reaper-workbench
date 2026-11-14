"""Checkpoint save: persist the active project once, then prove the result.

This is deliberately *not* a general save path. The capture gate exists because
an edit needs a measured panorama behind it; a checkpoint is the opposite case —
the project already holds unsaved state and the only safe next step is to write
it to a new file before anything restarts. So the surface is one call, with
constraints that make it impossible to use as a silent overwrite or as a save of
some other project:

* the target must be an absolute ``.RPP`` path in an existing directory;
* the file must not exist yet — overwrite is refused, never optional;
* ``project_id`` must be the active project, and ``expected_revision`` its live
  revision, so a caller cannot checkpoint a project it did not just read;
* the save itself is REAPER's ``Main_SaveProjectEx`` through the bridge's
  ``project.save`` operation with ``save_as``;
* the result is verified: the new path, the rebound project id, ``dirty == 0``
  and every audio item's source still online. Any mismatch raises, even though
  the file was already written — the caller must know it cannot trust it.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Mapping

from ..errors import BridgeUnavailable, ValidationError

#: Portable default. Set ``REAPER_CHECKPOINT_ROOT`` to a project-specific
#: version directory when checkpoints should live beside a particular project.
DEFAULT_CHECKPOINT_ROOT = Path.home() / "Documents" / "REAPER Workbench" / "versions"
ROOT_ENV = "REAPER_CHECKPOINT_ROOT"


def checkpoint_root() -> Path:
    override = os.environ.get(ROOT_ENV, "").strip()
    return Path(override).expanduser() if override else DEFAULT_CHECKPOINT_ROOT


def _folded(path: Path) -> str:
    """Case-insensitive, separator-insensitive form for containment checks."""
    return str(path).replace("\\", "/").casefold()


def _inside(target: Path, root: Path) -> bool:
    root_text = _folded(root).rstrip("/")
    return _folded(target) == root_text or _folded(target).startswith(root_text + "/")


def checkpoint_path(path: str | Path, *, root: Path | str | None = None) -> Path:
    """Validate a checkpoint target and return its canonical path.

    The target is confined to one directory: absolute, ``.RPP``, no ``..``
    segments, and — after resolving every symlink/junction — still inside the
    root. Comparison is case-insensitive because the project lives on a Windows
    volume where ``Versions`` and ``versions`` are the same directory.
    """
    if not isinstance(path, (str, Path)):
        raise ValidationError("path must be a string")
    root_path = Path(root).resolve() if root is not None else checkpoint_root().resolve()
    if not root_path.is_dir():
        raise ValidationError("checkpoint root does not exist",
                              details={"root": str(root_path)})
    target = Path(path)
    if not target.is_absolute():
        raise ValidationError("checkpoint path must be absolute",
                              details={"path": str(target)})
    if target.suffix.lower() != ".rpp":
        raise ValidationError("checkpoint path must end in .RPP",
                              details={"path": str(target)})
    if any(part == ".." for part in target.parts):
        raise ValidationError("checkpoint path must not contain '..' segments",
                              details={"path": str(target)})
    # Resolve the parent so a symlinked or junctioned directory cannot smuggle
    # the write outside the root, then keep the caller's file name.
    parent = target.parent.resolve()
    resolved = parent / target.name
    if not _inside(resolved, root_path):
        raise ValidationError(
            "checkpoint path is outside the allowed directory",
            details={"path": str(resolved), "root": str(root_path)})
    if not parent.is_dir():
        raise ValidationError("checkpoint directory does not exist",
                              details={"directory": str(parent)})
    if resolved.exists() or resolved.is_symlink():
        raise ValidationError("checkpoint target already exists; overwrite is refused",
                              details={"path": str(resolved)})
    return resolved


def _audio_rows(context: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for track in context.get("tracks") or []:
        if not isinstance(track, Mapping):
            continue
        for item in track.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            take = item.get("active_take") or {}
            if isinstance(take, Mapping) and take.get("is_midi"):
                continue
            rows.append(item)
    return rows


def save_checkpoint(*, path: str | Path,
                    project_id: str,
                    expected_revision: Mapping[str, Any],
                    call: Callable[[str, dict[str, Any]], Any],
                    health: Callable[[], Mapping[str, Any]],
                    context: Callable[[], Mapping[str, Any]] | None = None,
                    verify_media: bool = True,
                    root: Path | str | None = None) -> dict[str, Any]:
    """Save the active project to a new file and verify the result."""
    if not isinstance(project_id, str) or not project_id:
        raise ValidationError("project_id is required")
    if not isinstance(expected_revision, Mapping):
        raise ValidationError("expected_revision is required")
    expected_count = expected_revision.get("change_count")
    if isinstance(expected_count, bool) or not isinstance(expected_count, int):
        raise ValidationError(
            "expected_revision.change_count must be an integer",
            details={"expected_revision": dict(expected_revision)})
    target = checkpoint_path(path, root=root)

    running = health()
    current = running.get("project") if isinstance(running, Mapping) else None
    if not isinstance(current, Mapping):
        raise BridgeUnavailable("REAPER returned no active project",
                                details={"reason": "project_missing"})
    if current.get("project_id") != project_id:
        raise ValidationError(
            "project_id does not name the active project",
            details={"expected": project_id, "active": current.get("project_id")})
    have = current.get("change_count")
    if expected_count != have:
        raise BridgeUnavailable(
            "The project changed after the caller read it; refresh before saving",
            details={"reason": "stale_context", "expected": expected_count,
                     "current": have})

    saved = call("project.save", {"mode": "save_as", "path": str(target),
                                  "overwrite_existing": False})
    if not isinstance(saved, Mapping) or saved.get("saved") is not True:
        raise BridgeUnavailable("REAPER did not confirm the checkpoint save",
                                details={"reason": "save_unconfirmed",
                                         "answer": dict(saved or {})})
    reported = saved.get("path")
    if not isinstance(reported, str) or Path(reported).resolve() != target.resolve():
        raise BridgeUnavailable("REAPER saved to a different path than requested",
                                details={"reason": "path_mismatch",
                                         "requested": str(target), "reported": reported})

    after = health()
    rebound = after.get("project") if isinstance(after, Mapping) else None
    if not isinstance(rebound, Mapping):
        raise BridgeUnavailable("REAPER returned no project after the checkpoint",
                                details={"reason": "project_missing_after_save"})
    after_id = rebound.get("project_id")
    if not isinstance(after_id, str) or after_id == project_id or \
            target.name not in after_id:
        raise BridgeUnavailable(
            "The active project did not rebind to the checkpoint path",
            details={"reason": "project_not_rebound", "project_id": after_id,
                     "requested": str(target)})

    render_state = call("project.render_settings.read", {})
    dirty = render_state.get("dirty") if isinstance(render_state, Mapping) else None
    if isinstance(dirty, bool) or not isinstance(dirty, (int, float)):
        raise BridgeUnavailable("REAPER returned no numeric dirty state after the checkpoint",
                                details={"reason": "dirty_unreadable", "dirty": dirty})
    if dirty != 0:
        raise BridgeUnavailable(
            "REAPER reports unsaved changes after the checkpoint save",
            details={"reason": "dirty_after_save", "dirty": dirty,
                     "path": str(target)})

    media: dict[str, Any] | None = None
    if verify_media:
        if context is None:
            raise ValidationError("verify_media requires a context reader")
        rows = _audio_rows(context())
        online = [row for row in rows
                  if row.get("source_online") and (row.get("source_length_s") or 0) > 0]
        media = {"audio_items": len(rows), "online": len(online),
                 "offline": len(rows) - len(online)}
        if not rows:
            raise BridgeUnavailable("The checkpoint has no audio items to verify",
                                    details={"reason": "no_audio_items",
                                             "path": str(target)})
        if len(online) != len(rows):
            offline = [str(row.get("active_take", {}).get("source"))[:120]
                       for row in rows if row not in online][:5]
            raise BridgeUnavailable(
                "The checkpoint did not resolve every media source; it must not be trusted",
                details={"reason": "media_unresolved", "path": str(target),
                         "media": media, "offline_sample": offline})

    return {"saved": True, "path": str(target), "project_id": after_id,
            "previous_project_id": project_id, "dirty": int(dirty),
            "revision": rebound.get("change_count"), "media": media}
