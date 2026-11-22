"""Dry timing preview: render the plan's 1x geometry without editing REAPER.

The preview is deliberately a simulation of the same block geometry the bridge
applies.  It is useful for auditioning a suspicious seam or a large adjacent
shift before a transaction touches the project.  It does not run REAPER FX and
does not claim to be the native A/B render.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import soundfile as sf

from ..analysis.audio import load_audio
from ..errors import ValidationError
from .midi import TempoMap

PREVIEW_SCHEMA = "reaper-workbench.timing-preview/1"


def _fade(length: int, fade_in: float, fade_out: float, rate: int) -> np.ndarray:
    window = np.ones(length, dtype=np.float64)
    head = min(length, max(0, round(fade_in * rate)))
    tail = min(length, max(0, round(fade_out * rate)))
    if head:
        window[:head] *= np.arange(head) / head
    if tail:
        window[-tail:] *= 1 - np.arange(tail) / tail
    return window


def _render_aligned(source: np.ndarray, rate: int, blocks: list[Mapping[str, Any]],
                    duration_s: float) -> tuple[np.ndarray, list[dict[str, Any]]]:
    samples = np.asarray(source, dtype=np.float64)
    if samples.ndim == 1:
        samples = samples[:, None]
    base_length = max(len(samples), int(round(duration_s * rate)))
    pieces: list[tuple[int, np.ndarray]] = []
    geometry: list[dict[str, Any]] = []
    for block in blocks:
        start = max(0.0, float(block.get("start_s", 0.0)))
        end = min(float(duration_s), float(block.get("end_s", start)))
        if end <= start:
            continue
        a = int(round(start * rate))
        b = int(round(end * rate))
        if b <= a or a >= len(samples):
            continue
        piece = samples[a:b].copy()
        piece *= _fade(len(piece), float(block.get("fade_in_s", 0.0)),
                       float(block.get("fade_out_s", 0.0)), rate)[:, None]
        target = int(round(float(block.get("target_start_s", start)) * rate))
        # Keep a negative target inside the output; the bridge's preflight
        # rejects those before a real apply, so this is preview-only clipping.
        if target < 0:
            piece = piece[-target:]
            target = 0
        pieces.append((target, piece))
        geometry.append({"block_index": len(geometry),
                         "source_start_s": round(a / rate, 7),
                         "source_end_s": round(b / rate, 7),
                         "target_start_s": round(target / rate, 7),
                         "target_end_s": round((target + len(piece)) / rate, 7),
                         "shift_s": round(target / rate - a / rate, 7)})
    length = max([base_length] + [target + len(piece) for target, piece in pieces])
    out = np.zeros((length, samples.shape[1]), dtype=np.float64)
    for target, piece in pieces:
        out[target:target + len(piece)] += piece
    # The preview must not silently extend the source past its own duration;
    # trim to the take length while keeping the declared pre-roll.
    if len(out) > base_length:
        out = out[:base_length]
    return out, geometry


def _click_beats(tempo: TempoMap, start_s: float, stop_s: float, phase_s: float):
    start_qn = max(0.0, tempo.time_to_qn(start_s - phase_s))
    stop_qn = tempo.time_to_qn(stop_s - phase_s)
    bar, offset = tempo.qn_to_bar_beat(start_qn)
    while tempo.bar_start_qn(bar) <= stop_qn:
        origin = tempo.bar_start_qn(bar)
        length = tempo.bar_length_qn(bar)
        numerator, _, beat_qn, _ = tempo.meter_at_bar(bar)
        for beat in range(numerator):
            delta = beat * beat_qn
            if delta >= length or delta < offset - 1e-9:
                continue
            time_s = tempo.qn_to_time(origin + delta) + phase_s
            if start_s - 1e-9 <= time_s < stop_s:
                yield max(0.0, time_s - start_s), beat == 0
        bar += 1
        offset = 0.0


def render_timing_preview(plan: Mapping[str, Any], *, out_dir: str | Path,
                          bpm: float | None = None,
                          click: bool = True) -> dict[str, Any]:
    """Write ``A_original.wav``, ``B_aligned.wav`` and an optional click."""
    if not isinstance(plan, Mapping) or not isinstance(plan.get("blocks"), list):
        raise ValidationError("preview needs a timing plan with blocks")
    source_path = plan.get("source_path")
    if not source_path:
        analysis_id = plan.get("analysis_id")
        if analysis_id:
            from .timing_service import ANALYSES
            source_path = (ANALYSES.get(str(analysis_id)) or {}).get("source_path")
    if not source_path:
        raise ValidationError("preview needs the source WAV path",
                              details={"code": "source_path_missing"})
    audio = load_audio(str(source_path))
    rate = audio.sample_rate
    samples = np.asarray(audio.samples, dtype=np.float64)
    if samples.ndim == 1:
        samples = samples[:, None]
    duration = float(plan.get("duration_s") or len(samples) / rate)
    blocks = plan["blocks"]
    project_start = 0.
    if plan.get("method") == "crossfade":
        project_start = float(plan["item_position_s"])
        offset = float(plan["source_offset_s"])
        duration = float(plan["item_length_s"])
        begin = round(offset * rate)
        samples = samples[begin:begin + round(duration * rate)]
        blocks = [{**b, "start_s": b["source_start_s"]-offset,
                   "end_s": b["source_start_s"]-offset+b["length_s"],
                   "target_start_s": b["position_s"]-project_start} for b in blocks]
    aligned, geometry = _render_aligned(samples, rate, blocks, duration)
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(
        repr((str(source_path), plan.get("blocks"), plan.get("tempo_context"),
              plan.get("grid_phase_s"), bpm, click)).encode("utf-8")).hexdigest()[:16]
    prefix = root / f"timing-preview-{digest}"
    original_path = prefix.with_name(prefix.name + "-A-original.wav")
    aligned_path = prefix.with_name(prefix.name + "-B-aligned.wav")
    sf.write(original_path, samples, rate, subtype="PCM_24")
    sf.write(aligned_path, aligned, rate, subtype="PCM_24")
    click_path = None
    tempo_context = plan.get("tempo_context")
    if click and (tempo_context or (bpm and bpm > 0)):
        click_path = prefix.with_name(prefix.name + "-click.wav")
        total = max(len(samples), len(aligned))
        click_samples = np.zeros((total, 2), dtype=np.float64)
        tempo = TempoMap.from_context(tempo_context or
                                     {"bpm": bpm, "numerator": 4, "denominator": 4})
        phase = float(plan.get("grid_phase_s", 0.))
        for time_s, accent in _click_beats(tempo, project_start,
                                           project_start + total / rate, phase):
            at = int(round(time_s * rate))
            length = max(1, int(0.025 * rate))
            env = np.exp(-np.linspace(0, 6, length))
            tone = 0.6 * env * np.sin(2 * np.pi * (1500 if accent else 1000) *
                                      np.arange(length) / rate)
            click_samples[at:at + length, :] += tone[:total-at, None]
        sf.write(click_path, click_samples, rate, subtype="PCM_24")
    return {
        "schema": PREVIEW_SCHEMA,
        "source_path": str(source_path),
        "duration_s": round(duration, 7),
        "sample_rate": rate,
        "original_uri": original_path.as_posix(),
        "aligned_uri": aligned_path.as_posix(),
        "click_uri": click_path.as_posix() if click_path else None,
        "geometry": geometry,
        "block_count": len(geometry),
        "native_render": False,
        "note": "Dry geometry simulation; REAPER FX/native chain is not included.",
    }
