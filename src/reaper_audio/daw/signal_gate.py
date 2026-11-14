"""Sample-content gates for captured audio.

The bridge can prove that a WAV is structurally valid and still hand back a
file containing exact zeros.  This module turns the rendered samples into a
small, explicit signal verdict so callers can distinguish a deliberately
silent capture (an empty section, muted tracks, a zeroed/muted master) from an
unexpected silent render.  It is deliberately independent of the analysis
pipeline so the capture gate does not depend on optional analysis extras.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

# Same floor as the full-capture master gate: a peak/RMS at or below this is
# treated as silence for acceptance purposes.
SILENCE_DBFS = -120.0


def dbfs(value: float) -> float:
    """Return dBFS with the library's finite silence floor."""
    if value <= 0.0:
        return -160.0
    return float(max(-160.0, 20.0 * math.log10(value)))


def measure_wave(path: str | Path) -> dict[str, Any]:
    """Measure one WAV without silently normalising or truncating it."""
    peak = 0.0
    sum_square = 0.0
    sample_count = 0
    with sf.SoundFile(str(path)) as stream:
        frames = int(stream.frames)
        sample_rate = int(stream.samplerate)
        channels = int(stream.channels)
        while True:
            block = stream.read(frames=65_536, dtype="float64", always_2d=True)
            if block.size == 0:
                break
            peak = max(peak, float(np.max(np.abs(block))))
            sum_square += float(np.sum(np.square(block)))
            sample_count += int(block.size)
    rms = float(np.sqrt(sum_square / sample_count)) if sample_count else 0.0
    peak_dbfs = dbfs(peak)
    rms_dbfs = dbfs(rms)
    silent = peak_dbfs <= SILENCE_DBFS and rms_dbfs <= SILENCE_DBFS
    return {
        "frames": frames,
        "sample_rate": sample_rate,
        "channels": channels,
        "peak_dbfs": peak_dbfs,
        "rms_dbfs": rms_dbfs,
        "silent": silent,
        "audible": not silent,
    }


def source_activity(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarise rendered source stems for the master-silence decision.

    ``rows`` carry at least ``source_guid``, optional ``mute`` and the measured
    content fields from :func:`measure_wave`.
    """
    tracks: list[dict[str, Any]] = []
    audible: list[str] = []
    for row in rows:
        guid = str(row.get("source_guid") or "")
        content = row.get("content")
        if not isinstance(content, Mapping):
            continue
        record = {
            "source_guid": guid,
            "name": row.get("name"),
            "mute": bool(row.get("mute")),
            "peak_dbfs": content.get("peak_dbfs"),
            "rms_dbfs": content.get("rms_dbfs"),
            "audible": bool(content.get("audible")),
            "silent": bool(content.get("silent")),
        }
        tracks.append(record)
        if record["audible"]:
            audible.append(guid)
    return {
        "tracks": tracks,
        "audible_guids": audible,
        "audible_count": len(audible),
        "all_silent": not audible,
    }


def classify_master_silence(master: Mapping[str, Any],
                            activity: Mapping[str, Any],
                            master_state: Mapping[str, Any]) -> dict[str, Any]:
    """Say whether a silent master is evidence-based expected silence.

    A silent render is expected only when there is a concrete reason: every
    track is muted, the master is muted/zeroed, or the capture window contains
    no active audio items.  "Every rendered stem is silent" is *not* enough by
    itself: that is exactly what an unresolved source or a broken render looks
    like.  When active audio items exist in the window and the render is still
    silent, the result is unexplained and must be rejected by the caller.
    """
    silent = bool(master.get("silent"))
    if not silent:
        return {"expected_silence": False, "silence_reason": None,
                "unexpected_silence": False}

    tracks = activity.get("tracks")
    rows = [item for item in tracks if isinstance(item, Mapping)] \
        if isinstance(tracks, (list, tuple)) else []
    all_sources_silent = not bool(activity.get("audible_guids"))
    mute_values = [bool(item.get("mute")) for item in rows if "mute" in item]
    all_tracks_muted = bool(rows) and len(mute_values) == len(rows) and all(mute_values)
    active_audio_items = activity.get("active_audio_items")
    has_active_audio = (isinstance(active_audio_items, int) and
                        not isinstance(active_audio_items, bool) and
                        active_audio_items > 0)

    master_muted = bool(master_state.get("mute"))
    volume = master_state.get("volume")
    master_zero = (isinstance(volume, (int, float)) and
                   not isinstance(volume, bool) and float(volume) <= 1e-6)
    reasons: list[str] = []
    if all_sources_silent and not has_active_audio:
        # No active audio source in the window (or only muted items): the
        # silence is explainable by project structure, not by a failed render.
        reasons.append("no_active_audio_sources")
    if all_tracks_muted:
        reasons.append("all_project_tracks_muted")
    if master_muted:
        reasons.append("master_muted")
    if master_zero:
        reasons.append("master_zero_volume")
    expected = bool(reasons)
    return {
        "expected_silence": expected,
        "silence_reason": "+".join(reasons) if reasons else None,
        "unexpected_silence": not expected,
    }
