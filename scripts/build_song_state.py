"""Full-track map: every section of the song in every layer we have.

One pass over the whole render, in 30 s sections (matching what the model was trained on):

  * measured: LUFS, PLR, crest, dynamism, band shares, centroid, width, correlation;
  * reference: the mean of the reference records, and the delta for every section;
  * heard: the model's own reading of that section, in its words;
  * timeline: side/mid and balance per section, so width drift is visible.

Writes work/song_state.json and prints one line per section. Roles are attached later, when
the project is open and each track can be rendered as its own stem - the schema already has
the slot for them.

    python scripts/build_song_state.py --render work/preview_full_master.wav \
        --ref "C:/.../GRIMA.wav" --ref "C:/.../Wintersun.wav" --ask
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

LIB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LIB / "src"))
sys.path.insert(0, str(LIB / "scripts"))

import reference_delta as rd                     # noqa: E402
from reaper_audio.analysis import ossicle as os_  # noqa: E402

KEYS = ["lufs_i", "plr_db", "crest_db", "dynamism_db", "centroid_hz", "side_mid_db",
        "correlation", "low_ratio", "lowmid_ratio", "mid_ratio", "presence_ratio", "air_ratio"]


def sections(duration: float, length: float = 30.0) -> list[tuple[float, float]]:
    out, t = [], 0.0
    while t < duration - 5.0:
        out.append((round(t, 1), round(min(t + length, duration), 1)))
        t += length
    return out


def model_readings(path: str, spans, max_new_tokens: int = 320) -> list[dict]:
    jobs = [{"wav": path, "start_s": a, "end_s": b, "question": os_.DEFAULT_QUESTION,
             "max_new_tokens": max_new_tokens} for a, b in spans]
    report = os_.analyze(jobs, dtype="float16")
    return report["answers"], report["runtime"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--render", required=True)
    ap.add_argument("--ref", action="append", default=[])
    ap.add_argument("--ask", action="store_true", help="let the model read every section")
    ap.add_argument("--section-s", type=float, default=30.0)
    ap.add_argument("--out", default=str(LIB / "work" / "song_state.json"))
    args = ap.parse_args()

    import soundfile as sf
    duration = sf.info(args.render).frames / sf.info(args.render).samplerate
    spans = sections(duration, args.section_s)

    ref_metrics = []
    for ref in args.ref:
        # references are sampled on their own 30 s grid; the mean is the target line
        ref_spans = sections(sf.info(ref).frames / sf.info(ref).samplerate, args.section_s)
        for a, b in ref_spans:
            x, sr, _ = rd.window(Path(ref), (a, b))
            ref_metrics.append(rd.measure(x, sr))
    ref_mean = {k: round(float(np.mean([m[k] for m in ref_metrics])), 2) for k in KEYS} if ref_metrics else {}

    rows = []
    for a, b in spans:
        x, sr, _ = rd.window(Path(args.render), (a, b))
        rows.append({"span": [a, b], "metrics": rd.measure(x, sr)})

    runtime = None
    if args.ask:
        answers, runtime = model_readings(args.render, spans)
        for row, ans in zip(rows, answers):
            row["heard"] = ans["text"]
            row["seconds"] = ans["seconds"]

    state = {"render": args.render, "duration_s": round(duration, 2),
             "references": args.ref, "reference_mean": ref_mean, "runtime": runtime,
             "sections": rows}
    Path(args.out).write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    head = f"{'section':>15} {'LUFS':>7} {'PLR':>6} {'dyn':>6} {'lowmid':>7} {'pres':>7} {'air':>7} {'s/m':>6}"
    if ref_mean:
        head += f" {'dlowmid':>8} {'dpres':>7} {'dair':>6}"
    print(head)
    for row in rows:
        m = row["metrics"]
        line = (f"{row['span'][0]:>7.0f}-{row['span'][1]:<7.0f}{m['lufs_i']:>7.1f}{m['plr_db']:>6.1f}"
                f"{m['dynamism_db']:>6.1f}{m['lowmid_ratio']:>7.1f}{m['presence_ratio']:>7.1f}"
                f"{m['air_ratio']:>7.1f}{m['side_mid_db']:>6.1f}")
        if ref_mean:
            line += (f"{m['lowmid_ratio'] - ref_mean['lowmid_ratio']:>+8.1f}"
                     f"{m['presence_ratio'] - ref_mean['presence_ratio']:>+7.1f}"
                     f"{m['air_ratio'] - ref_mean['air_ratio']:>+6.1f}")
        print(line)
    print("written:", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
