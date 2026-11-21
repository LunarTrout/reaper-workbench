"""Wake REAPER's media before any render, without the owner touching the window.

REAPER resolves offline media when its main window is activated.  A bridge
call does not activate it, so a background instance can sit with every item
offline and render silence.  Every operation that needs project audio calls
``ensure_media_online`` first: it reads the media section, and only when
something is offline does it raise the REAPER window and wait for the items to
come online.  If they do not, the operation fails with ``media_unresolved``
instead of rendering silence.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any

from ..errors import MediaUnresolved

SW_RESTORE = 9
SW_SHOW = 5


def _reaper_windows() -> list[tuple[int, str]]:
    user32 = ctypes.windll.user32
    handles: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):  # pragma: no cover - Windows only
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value
        if "REAPER" in title.upper():
            handles.append((int(hwnd), title))
        return True

    user32.EnumWindows(callback, 0)
    return handles


def _focus_window(project_path: str | None) -> str | None:
    """Bring the REAPER window that owns this project to the foreground."""
    windows = _reaper_windows()
    if not windows:
        return None
    wanted = (project_path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    stem = wanted.rsplit(".", 1)[0]
    reaper_only = [(hwnd, title) for hwnd, title in windows
                   if "chrome" not in title.lower() and "firefox" not in title.lower()]
    pool = reaper_only or windows
    chosen = next(((hwnd, title) for hwnd, title in pool
                   if (wanted and wanted in title.lower()) or
                      (stem and stem in title.lower())), pool[0])
    hwnd, title = chosen
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    return title


def media_state(session: Any) -> dict[str, Any]:
    reader = getattr(session, "status", None)
    if not callable(reader):
        # Lightweight/fake sessions used by callers that render files directly
        # have no project media section; nothing can be offline for them.
        return {"offline_count": 0, "available": False}
    status = reader(detail="summary", sections=["media"])
    return dict(status.get("media") or {})


def ensure_media_online(session: Any, *, timeout_s: float = 30.0,
                        poll_s: float = 1.0) -> dict[str, Any]:
    """Return the media state, waking REAPER first if anything is offline."""
    media = media_state(session)
    offline = int(media.get("offline_count") or 0)
    if offline <= 0:
        return {"woke": False, "waited_s": 0.0, "media": media}

    project_path = str((session.snapshot(include_items=False).get("revision") or {})
                       .get("path") or session.project_id or "")
    title = _focus_window(project_path)
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        time.sleep(poll_s)
        media = media_state(session)
        if int(media.get("offline_count") or 0) <= 0:
            return {"woke": True, "waited_s": round(time.monotonic() - started, 2),
                    "window": title, "media": media}
    raise MediaUnresolved(
        "Media stayed offline after waking REAPER; refusing to render silence",
        details={"offline_count": media.get("offline_count"),
                 "offline_tracks": media.get("offline_tracks"),
                 "window": title,
                 "next_action": ("bring the REAPER window forward once, or reload the "
                                 "project; the library never measures offline media")})


__all__ = ["ensure_media_online", "media_state"]
