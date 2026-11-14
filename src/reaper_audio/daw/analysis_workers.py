"""Spawn-safe DSP worker used by full capture.

The process pool is created once per capture.  This module is imported by each
spawned child before ``numpy``/``scipy`` are imported, so the thread limits
actually apply to the child's BLAS and OpenMP runtimes.  The worker returns
plain JSON-safe values; the parent owns GUID mapping and ordering.
"""
from __future__ import annotations

import os
import time

for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
              "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_name, "1")

from ..analysis.audio import load_audio
from ..analysis.metrics import measure_audio


def analyze_asset_job(payload: tuple[str, str]) -> dict:
    """Load and measure one rendered asset inside a spawned worker.

    ``payload`` is ``(source_guid, path)``.  Only plain values cross the
    process boundary; the parent never receives the sample array.
    """
    guid, path = payload
    decode_started = time.perf_counter()
    info = load_audio(path)
    decode_s = time.perf_counter() - decode_started
    dsp_started = time.perf_counter()
    report = measure_audio(info).to_dict()
    dsp_s = time.perf_counter() - dsp_started
    return {
        "source_guid": guid,
        "analysis": report,
        "sample_rate": int(info.sample_rate),
        "channels": int(info.samples.shape[1]),
        "frames": int(info.frames),
        "duration_s": float(info.duration_s),
        "decode_s": round(decode_s, 6),
        "dsp_s": round(dsp_s, 6),
    }
