"""Host-window helpers that keep REAPER's media sources open.

REAPER may release media files while its main window is inactive: sources then
report zero length, zero sample rate and ``source_online=false`` even though the
files are on disk.  Any render made in that state would be silent.  Bringing the
window to the foreground reopens those sources, so every render path focuses the
host first and re-checks the reported source state afterwards.
"""

from __future__ import annotations

import os
import time


def focus_reaper_window() -> bool:
    """Bring a REAPER main window to the foreground.  Best effort, never raises."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _collect(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if "REAPER" in buffer.value:
                    found.append(hwnd)
            return True

        user32.EnumWindows(_collect, 0)
        for hwnd in found:
            user32.ShowWindow(hwnd, 9)
            if user32.SetForegroundWindow(hwnd):
                time.sleep(0.35)
                return True
    except Exception:  # noqa: BLE001 - focus is opportunistic
        return False
    return bool(found)
