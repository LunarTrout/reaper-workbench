"""Measure one declared window of one rendered or external file.

The render of a measure call covers the union of every requested interval
(one render per source, not one per cell).  Each cell is then decoded from that
file over exactly its own window and measured with the same analyzer a single
call would use, so a cell and a standalone measurement of the same window are
the same number.

The job function is module level and takes only plain values: it is used both
in-process and through a spawn pool.
"""

from __future__ import annotations

import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Sequence

from ..analysis.audio import load_audio
from ..analysis.metrics import measure_audio
from ..errors import ValidationError
from ..models import AudioBuffer, Section

#: ``(source_key, asset_path, window_start_in_asset_s, interval_start_s, interval_end_s)``
WindowPayload = tuple[str, str, float, float, float]


def analyze_window_job(payload: WindowPayload) -> dict[str, Any]:
    """Decode and measure one window of one asset inside a worker."""
    key, path, offset_s, start_s, end_s = payload
    length_s = float(end_s) - float(start_s)
    if length_s <= 0:
        raise ValidationError("A measurement window must be positive",
                              details={"source": key, "start_s": start_s,
                                       "end_s": end_s})
    decode_started = time.perf_counter()
    try:
        audio = load_audio(path, Section(float(offset_s), float(offset_s) + length_s))
    except ValidationError as exc:
        raise ValidationError(
            "The measurement window is not covered by the source audio",
            details={"source": key, "path": str(path),
                     "window_in_asset_s": {"start_s": float(offset_s),
                                           "end_s": float(offset_s) + length_s},
                     "interval_s": {"start_s": float(start_s),
                                    "end_s": float(end_s)},
                     "reason": exc.message}) from exc
    decode_s = time.perf_counter() - decode_started
    # Report times in project coordinates: the same interval the caller asked
    # for, whether the asset is a render or an external file.
    buffer = AudioBuffer(
        samples=audio.samples, sample_rate=audio.sample_rate,
        source=audio.source, start_s=float(start_s),
        provenance={**audio.provenance,
                    "asset_window_s": {"start_s": float(offset_s),
                                       "end_s": float(offset_s) + length_s},
                    "interval_s": {"start_s": float(start_s),
                                   "end_s": float(end_s)}},
    )
    dsp_started = time.perf_counter()
    report = measure_audio(buffer).to_dict()
    dsp_s = time.perf_counter() - dsp_started
    report["provenance"]["measured_section_s"] = {
        "start_s": float(start_s), "end_s": float(end_s)}
    report["section"] = {"start_s": float(start_s), "end_s": float(end_s)}
    return {
        "source_guid": key,
        "analysis": report,
        "sample_rate": int(buffer.sample_rate),
        "channels": int(buffer.channels),
        "frames": int(buffer.frames),
        "duration_s": float(buffer.duration_s),
        "decode_s": round(decode_s, 6),
        "dsp_s": round(dsp_s, 6),
    }


def analyze_windows(payloads: Sequence[WindowPayload],
                    workers: int) -> list[dict[str, Any]]:
    """Measure windows in input order, in-process when a pool would not help."""
    if not payloads:
        return []
    if workers <= 1 or len(payloads) <= 1:
        return [analyze_window_job(payload) for payload in payloads]
    context = multiprocessing.get_context("spawn")
    executor = ProcessPoolExecutor(max_workers=workers, mp_context=context)
    futures = [executor.submit(analyze_window_job, payload) for payload in payloads]
    try:
        results = [future.result() for future in futures]
    except BaseException:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    executor.shutdown(wait=True)
    return results


__all__ = ["WindowPayload", "analyze_window_job", "analyze_windows"]
