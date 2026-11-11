"""Reliability calibration for Ossicle-4B's relative judgements.

Absolute adjectives are useless without a reference, so the backend asks pairwise
questions. That only helps if the model's pairwise answers track reality. This script
builds variants of one real clip with a known change on a single axis, then asks the model
to compare adjacent steps:

    brightness:  high shelf  +0 / +3 / +6 dB @ 4 kHz
    width:       side gain   0.5x / 1.0x / 1.6x   (M/S)
    level:       0 / +2 / +4 dB
    distance:    reverb wet  0.0 / 0.35 / 0.7

For each axis, three pairwise questions (1v2, 2v3, 1v3) with a known correct answer.
Accuracy on an axis is what tells us whether to trust that axis on a real mix.

Stage 1 (no GPU): build and measure the variants, print measured deltas.
Stage 2 (--run-model): ask the model and score its answers against those deltas.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import soundfile as sf

OUT = Path(__file__).resolve().parents[1] / "work" / "spatial_calibration"
SR = 16000


def load_source(path: str | Path, seconds: float = 12.0) -> np.ndarray:
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    x = data[:, :2].T
    if sr != SR:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(SR, sr)
        x = np.stack([resample_poly(ch, SR // g, sr // g) for ch in x]).astype(np.float32)
    n = int(seconds * SR)
    if x.shape[1] < n:
        x = np.pad(x, ((0, 0), (0, n - x.shape[1])))
    return x[:, :n]


def high_shelf(x: np.ndarray, gain_db: float, freq: float = 4000.0) -> np.ndarray:
    from scipy.signal import sosfiltfilt, butter
    # crude but measurable shelf: split at freq, boost the high band
    sos = butter(4, freq / (SR / 2), btype="high", output="sos")
    high = sosfiltfilt(sos, x, axis=1)
    return (x + high * (10 ** (gain_db / 20) - 1)).astype(np.float32)


def side_gain(x: np.ndarray, gain: float) -> np.ndarray:
    mid = (x[0] + x[1]) / 2.0
    side = (x[0] - x[1]) / 2.0 * gain
    return np.stack([mid + side, mid - side]).astype(np.float32)


def reverb_wet(x: np.ndarray, wet: float, decay: float = 0.6) -> np.ndarray:
    n = int(0.8 * SR)
    t = np.arange(n) / SR
    rng = np.random.default_rng(7)
    tail = (rng.standard_normal(n) * np.exp(-t / decay)).astype(np.float32)
    tail /= np.max(np.abs(tail)) + 1e-9
    out = x.copy()
    dry_rms = np.sqrt(np.mean(x ** 2)) + 1e-12
    for ch in range(2):
        wet_sig = np.convolve(x[ch], tail)[: x.shape[1]].astype(np.float32)
        wet_sig *= dry_rms / (np.sqrt(np.mean(wet_sig ** 2)) + 1e-12)   # equal-power, level stays put
        out[ch] = (1 - wet) * x[ch] + wet * wet_sig
    return out.astype(np.float32)


def measure(x: np.ndarray) -> dict:
    from scipy.signal import butter, sosfiltfilt
    def band(lo, hi):
        sos = butter(4, [lo / (SR / 2), hi / (SR / 2)], btype="band", output="sos")
        return sosfiltfilt(sos, x, axis=1)
    low, high = band(60, 250), band(4000, 7000)
    mid, side = (x[0] + x[1]) / 2, (x[0] - x[1]) / 2
    return {
        "rms_dbfs": round(float(20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-12)), 2),
        "high_low_db": round(float(20 * np.log10((np.sqrt(np.mean(high ** 2)) + 1e-12) /
                                                (np.sqrt(np.mean(low ** 2)) + 1e-12))), 2),
        "width": round(float(np.sqrt(np.mean(side ** 2)) / (np.sqrt(np.mean(mid ** 2)) + 1e-12)), 3),
        "correlation": round(float(np.dot(x[0], x[1]) /
                                   ((np.linalg.norm(x[0]) * np.linalg.norm(x[1])) + 1e-12)), 3),
    }


def match_rms(clip: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """Same loudness as the reference, so only the intended axis changes."""
    ref_rms = np.sqrt(np.mean(ref ** 2)) + 1e-12
    cur = np.sqrt(np.mean(clip ** 2)) + 1e-12
    return (clip * (ref_rms / cur)).astype(np.float32)


def build(source: np.ndarray) -> dict[str, list[Path]]:
    OUT.mkdir(parents=True, exist_ok=True)
    variants: dict[str, list[Path]] = {}
    plan = {
        "brightness": [match_rms(high_shelf(source, g), source) for g in (0.0, 3.0, 6.0)],
        "width": [match_rms(side_gain(source, g), source) for g in (0.5, 1.0, 1.6)],
        "level": [(source * 10 ** (g / 20)).astype(np.float32) for g in (0.0, 2.0, 4.0)],
        "distance": [match_rms(reverb_wet(source, w), source) for w in (0.0, 0.35, 0.7)],
    }
    for axis, clips in plan.items():
        variants[axis] = []
        for i, clip in enumerate(clips):
            path = OUT / f"{axis}_{i}.wav"
            sf.write(str(path), clip.T, SR, subtype="FLOAT")
            variants[axis].append(path)
    return variants


QUESTIONS = {
    "brightness": "Compare Audio 1 and Audio 2. Which one has more energy in the high frequencies? Answer Audio 1 or Audio 2, then how much, then what makes it so.",
    "width": "Compare Audio 1 and Audio 2. Which one sounds wider in the stereo image? Answer Audio 1 or Audio 2, then how much, then what makes it so.",
    "level": "Compare Audio 1 and Audio 2. Which one is louder overall? Answer Audio 1 or Audio 2, then how much, then what makes it so.",
    "distance": "Compare Audio 1 and Audio 2. Which one sounds further away? Answer Audio 1 or Audio 2, then how much, then what makes it so.",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="stereo wav used as the base material")
    ap.add_argument("--run-model", action="store_true")
    args = ap.parse_args()

    source = load_source(args.source)
    variants = build(source)
    facts = {axis: [measure(sf.read(str(p), dtype="float32", always_2d=True)[0].T)
                    for p in paths] for axis, paths in variants.items()}
    print(json.dumps({"variants": {a: [str(p) for p in ps] for a, ps in variants.items()},
                      "measured": facts}, indent=2))

    if not args.run_model:
        print("\nstage 1 only: variants built and measured, no model run")
        return

    # One model load for every pair: analyse() takes a job list, each job carries both
    # clips. Presentation order is shuffled per pair, otherwise a model that always
    # answers "Audio 1" (or always "Audio 2") scores 100% without discriminating
    # anything - which is exactly what the first run of this harness showed.
    import random
    from reaper_audio.analysis import ossicle as ms
    rng = random.Random(1234)
    jobs, keys = [], []
    for axis, paths in variants.items():
        for i, j in ((0, 1), (1, 2), (0, 2)):
            swapped = rng.random() < 0.5
            a, b = (paths[j], paths[i]) if swapped else (paths[i], paths[j])
            jobs.append({"wav": str(a), "wav2": str(b), "question": QUESTIONS[axis]})
            keys.append((axis, i, j, swapped))
    reply = ms.analyze(jobs)
    results: dict[str, dict] = {}
    for (axis, i, j, swapped), row in zip(keys, reply["answers"]):
        results.setdefault(axis, {"pairs": [], "measured": facts[axis]})
        results[axis]["pairs"].append({"pair": [i, j], "presented": "swapped" if swapped else "in order",
                                      "text": row["text"], "seconds": row["seconds"]})
    # Score every pair against the axis direction that was built into the variants:
    # index 1 carries more of the axis than index 0, index 2 more than index 1.
    for axis, block in results.items():
        if not isinstance(block, dict) or "pairs" not in block:
            continue
        hits, scored, said_one, said_two = 0, 0, 0, 0
        for row in block["pairs"]:
            i, j = row["pair"]
            expected = "audio 1" if row["presented"] == "swapped" else "audio 2"
            text = row["text"].lower()
            first = min([(text.find(t), t) for t in ("audio 1", "audio 2") if text.find(t) >= 0],
                        default=(None, None))[1]
            if first is None:
                row["verdict"] = "no choice named"
                continue
            said_one += first == "audio 1"
            said_two += first == "audio 2"
            scored += 1
            row["expected"] = expected
            row["verdict"] = "hit" if first == expected else "miss"
            hits += row["verdict"] == "hit"
        block["accuracy"] = f"{hits}/{scored}" if scored else "0/0"
        block["said"] = {"audio 1": said_one, "audio 2": said_two}
    results["runtime"] = reply["runtime"]
    results["gpu"] = reply["gpu"]
    out = OUT / "calibration.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
