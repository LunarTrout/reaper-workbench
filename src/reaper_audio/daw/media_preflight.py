"""Pre-render media preflight.

REAPER unloads media while its window is inactive, and the bridge then reports
``source_online=false`` with zero length/rate/channels.  A render started in
that state would be silent, so the capture path used to fail with
``media_unresolved`` even though every file was on disk.

This module performs the repair the host needs (focus + "Set all media online")
and proves it worked by re-reading the project, instead of telling the caller to
fix it by hand.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from ..reaper.window import focus_reaper_window

SET_ALL_MEDIA_ONLINE = 40101


def unresolved_rows(tracks: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Same rule as the capture guard: unmuted audio takes with no media."""
    rows: list[dict[str, Any]] = []
    for track in tracks:
        if not isinstance(track, Mapping) or bool(track.get("mute")):
            continue
        for item in track.get("items") or []:
            if not isinstance(item, Mapping) or bool(item.get("mute")):
                continue
            take = item.get("active_take")
            if isinstance(take, Mapping) and take.get("is_midi"):
                continue
            if "source_online" not in item:
                continue
            values = (item.get("source_online"), item.get("source_length_s"),
                      item.get("source_sample_rate"), item.get("source_channels"))
            if not all(value not in (None, 0, 0.0, False) for value in values):
                rows.append({
                    "track_guid": track.get("guid"),
                    "track_index": track.get("index"),
                    "track_name": track.get("name"),
                    "item_guid": item.get("guid"),
                    "source": (take or {}).get("source") if isinstance(take, Mapping) else None,
                })
    return rows


def ensure_media_online(session: Any, *, attempts: int = 3,
                        wait_s: float = 1.2) -> dict[str, Any]:
    """Focus REAPER and reopen media until the project reports online sources.

    Returns ``{"ok": bool, "recovered": bool, "attempts": int, "unresolved": [...]}``.
    Never raises: a caller that still sees unresolved sources must fail closed.
    """
    context = _snapshot(session)
    rows = unresolved_rows(context.get("tracks") or [])
    if not rows:
        return {"ok": True, "recovered": False, "attempts": 0, "unresolved": []}

    for attempt in range(1, attempts + 1):
        focus_reaper_window()
        try:
            session.bridge.call_api("Main_OnCommand", SET_ALL_MEDIA_ONLINE, 0,
                                    timeout_s=10.0)
        except Exception:  # noqa: BLE001 - the recheck below is the proof
            pass
        time.sleep(wait_s)
        context = _snapshot(session)
        rows = unresolved_rows(context.get("tracks") or [])
        if not rows:
            return {"ok": True, "recovered": True, "attempts": attempt,
                    "unresolved": []}
    return {"ok": False, "recovered": False, "attempts": attempts,
            "unresolved": rows}


def _snapshot(session: Any) -> Mapping[str, Any]:
    try:
        return session.snapshot(include_items=True, include_fx_params=False)
    except TypeError:
        return session.snapshot()
