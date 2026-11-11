"""Timeline profile of a stereo master: measured width/spatial facts per window.

The spatial model only ever sees one 30-60 s clip, so "what happens across the whole
song" cannot come from the model. This module produces the measured half of that
picture — per-window balance, correlation, side/mid ratio and per-band correlation —
which a model caption can later sit next to. No model, no GPU, no inference here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

BANDS = [(20, 120), (120, 500), (500, 2000), (2000, 8000), (8000, 16000)]


def _db(x: float) -> float:
    return float(20 * np.log10(max(float(x), 1e-12)))


def _band(x: np.ndarray, sr: int, lo: float, hi: float) -> np.ndarray:
    from scipy.signal import butter, sosfiltfilt
    nyq = sr / 2.0
    hi = min(hi, nyq * 0.99)
    if lo <= 0:
        sos = butter(4, hi / nyq, btype="low", output="sos")
    else:
        sos = butter(4, [lo / nyq, hi / nyq], btype="band", output="sos")
    return sosfiltfilt(sos, x)


def window_metrics(block: np.ndarray, sr: int) -> dict[str, Any]:
    """block is (2, T)."""
    left, right = block[0].astype(np.float64), block[1].astype(np.float64)
    mid, side = (left + right) / 2.0, (left - right) / 2.0
    rms_l, rms_r = np.sqrt(np.mean(left ** 2)), np.sqrt(np.mean(right ** 2))
    rms_mid, rms_side = np.sqrt(np.mean(mid ** 2)), np.sqrt(np.mean(side ** 2))
    denom = (np.linalg.norm(left) * np.linalg.norm(right)) + 1e-12
    bands = {}
    for lo, hi in BANDS:
        l, r = _band(left, sr, lo, hi), _band(right, sr, lo, hi)
        d = (np.linalg.norm(l) * np.linalg.norm(r)) + 1e-12
        bands[f"{lo}-{hi}"] = round(float(np.dot(l, r) / d), 3)
    return {
        "rms_dbfs": [round(_db(rms_l), 2), round(_db(rms_r), 2)],
        "balance_db": round(_db(rms_l) - _db(rms_r), 2),
        "correlation": round(float(np.dot(left, right) / denom), 3),
        "side_mid_db": round(_db(rms_side) - _db(rms_mid), 2),
        "width": round(float(rms_side / (rms_mid + 1e-12)), 3),
        "crest_db": round(_db(np.max(np.abs(block))) - _db(np.sqrt(np.mean(block ** 2))), 2),
        "band_correlation": bands,
    }


def timeline(path: str | Path, window_s: float = 30.0, hop_s: float | None = None,
             start_s: float = 0.0, end_s: float | None = None) -> dict[str, Any]:
    import soundfile as sf

    info = sf.info(str(path))
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if data.shape[1] < 2:
        raise ValueError("a timeline profile needs a stereo file")
    data = data[:, :2].T
    if end_s is not None:
        data = data[:, : int(end_s * sr)]
    if start_s:
        data = data[:, int(start_s * sr):]
    hop = hop_s or window_s / 2.0
    total = data.shape[1] / sr

    rows = []
    t = 0.0
    while t < total:
        a, b = int(t * sr), min(int((t + window_s) * sr), data.shape[1])
        if b - a < int(0.2 * sr):
            break
        m = window_metrics(data[:, a:b], sr)
        m["window_s"] = [round(start_s + t, 2), round(start_s + b / sr, 2)]
        rows.append(m)
        t += hop

    def stat(key):
        vals = [r[key] for r in rows]
        return {"min": min(vals), "max": max(vals),
                "at_min": rows[vals.index(min(vals))]["window_s"],
                "at_max": rows[vals.index(max(vals))]["window_s"]}

    flagged = []
    for r in rows:
        if r["correlation"] >= 0.9:
            flagged.append({"window_s": r["window_s"], "what": "near-mono",
                            "correlation": r["correlation"]})
        elif r["correlation"] <= 0.2:
            flagged.append({"window_s": r["window_s"], "what": "very wide / phase risk",
                            "correlation": r["correlation"]})
        if abs(r["balance_db"]) >= 1.0:
            flagged.append({"window_s": r["window_s"], "what": "level leaned to one side",
                            "balance_db": r["balance_db"]})
    return {"path": str(path), "sample_rate": sr, "duration_s": round(total, 2),
            "window_s": window_s, "hop_s": hop,
            "windows": rows,
            "aggregate": {"correlation": stat("correlation"), "width": stat("width"),
                          "balance_db": stat("balance_db"), "side_mid_db": stat("side_mid_db")},
            "flags": flagged,
            "note": ("Measured facts only. Captions from a spatial model, if any, must be "
                     "compared against these numbers, never used in their place.")}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--window", type=float, default=30.0)
    ap.add_argument("--hop", type=float, default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    report = timeline(args.wav, window_s=args.window, hop_s=args.hop)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"written: {args.out} ({len(report['windows'])} windows)")
    else:
        print(text)


if __name__ == "__main__":
    main()
