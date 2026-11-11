"""Compact arrangement context for model prompts ("лента" as text).

The spatial/audio models have no idea where a clip sits in the song: no tempo, no bar
numbers, no track names, no pan values. This module turns the live REAPER project into a
short factual block that can be prepended to the question, so an answer can be grounded
in the arrangement instead of guessing from 30 seconds of audio alone.

It is deliberately text, not a second input modality: the models were not trained on DAW
objects, and pretending otherwise would produce confident nonsense.
"""

from __future__ import annotations

from typing import Any


def _db(volume: float) -> float:
    import math
    return round(20 * math.log10(max(float(volume), 1e-6)), 2)


def _bar_at(t: float, tempo: dict[str, Any]) -> float:
    """Fractional bar number at time t, from the project's tempo map."""
    marks = sorted(tempo.get("markers") or [], key=lambda m: m.get("time_s") or 0.0)
    if not marks:
        return 0.0
    cur = marks[0]
    for m in marks:
        if (m.get("time_s") or 0.0) <= t + 1e-9:
            cur = m
        else:
            break
    bpm = float(cur.get("bpm") or 120.0)
    num = float(cur.get("numerator") or 4)
    measure = float(cur.get("measure") or 0)
    return measure + (t - float(cur.get("time_s") or 0.0)) * bpm / (60.0 * num)


def build(ctx: dict[str, Any], start_s: float | None = None, end_s: float | None = None,
          items: list[dict[str, Any]] | None = None,
          markers: list[dict[str, Any]] | None = None,
          max_tracks: int = 40, max_items: int = 60) -> str:
    tempo = ctx.get("tempo") or {}
    length = ctx.get("length_s")
    length = f"{float(length):.2f} s" if isinstance(length, (int, float)) else "unknown"
    lines = [f"PROJECT: {ctx.get('name')} | {ctx.get('track_count')} tracks | length {length}"]

    marks = sorted(tempo.get("markers") or [], key=lambda m: m.get("time_s") or 0.0)
    if marks:
        parts = [f"bar {int(m.get('measure', 0)) + 1} ({float(m.get('time_s') or 0):.1f}s) "
                 f"{float(m.get('bpm') or 0):g} BPM {int(m.get('numerator') or 4)}/{int(m.get('denominator') or 4)}"
                 for m in marks]
        lines.append("TEMPO MAP: " + "; ".join(parts))

    if start_s is not None and end_s is not None:
        b0, b1 = _bar_at(start_s, tempo), _bar_at(end_s, tempo)
        bpm = float((tempo.get("markers") or [{}])[0].get("bpm") or 0) if tempo else 0
        for m in marks:
            if (m.get("time_s") or 0.0) <= start_s:
                bpm = float(m.get("bpm") or bpm)
        lines.append(f"FOCUS: {start_s:.2f}-{end_s:.2f} s = bars {b0:.1f}-{b1:.1f} "
                     f"({end_s - start_s:.2f} s at {bpm:g} BPM)")

    if items:
        rows = []
        for it in items[:max_items]:
            rows.append(f"{it.get('track')} @ {it.get('start', 0):.1f}-{it.get('end', 0):.1f}s"
                        + (f" ({it['name']})" if it.get("name") else ""))
        lines.append(f"ITEMS IN RANGE ({min(len(items), max_items)} of {len(items)}): " + "; ".join(rows))

    if markers:
        rows = [f"{m.get('name') or 'marker'} @ {float(m.get('time_s') or 0):.1f}s" for m in markers[:20]]
        lines.append("MARKERS: " + "; ".join(rows))

    tracks = [t for t in ctx.get("tracks", []) if not t.get("is_master")]
    if items:
        active = {it.get("track") for it in items}
        tracks = [t for t in tracks if t.get("name") in active] + \
                 [t for t in tracks if t.get("name") not in active]
    tracks = tracks[:max_tracks]
    lines.append("TRACKS (pan -1..1, fader dB):")
    for t in tracks:
        bits = [f"{int(t.get('index', 0)) + 1}. {t.get('name')}",
                f"pan {float(t.get('pan') or 0):+.2f}",
                f"{_db(t.get('volume') or 1.0):+.1f} dB"]
        if t.get("mute"):
            bits.append("MUTED")
        if t.get("solo"):
            bits.append("SOLO")
        fx = [f.get("name") for f in (t.get("fx") or []) if f.get("enabled")]
        if fx:
            bits.append("FX: " + ", ".join(fx[:3]))
        sends = int(t.get("sends") or 0) if isinstance(t.get("sends"), int) else len(t.get("sends") or [])
        if sends:
            bits.append(f"{sends} send(s)")
        lines.append("  " + " | ".join(bits))

    text = "\n".join(lines)
    if len(text) > 2200:
        text = text[:2200].rsplit("\n", 1)[0] + "\n  ... (truncated)"
    return text
