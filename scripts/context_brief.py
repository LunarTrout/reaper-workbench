"""Context brief: everything the library measured, as text, then one model pass over it.

The point is not "what do you hear" but "here are the numbers, the reference deltas, the
per-window stereo behaviour and the audio - now analyse". The model gets:

  * our section: loudness, PLR, crest, dynamism, band shares, centroid, width, correlation;
  * the same numbers for every reference (mean + per file), and the delta;
  * a 30 s stereo timeline of the whole track (correlation, balance, side/mid);
  * the audio itself, as one clip per model request.

Arrangement is out of scope by standing rule: the brief never asks what to add, remove or
reorder, only what is off in tone, level, width, dynamics and space, and by how much.

    python scripts/context_brief.py --ours work/preview_full_master.wav \
        --ref "C:/.../GRIMA.wav" --ref "C:/.../Wintersun.wav" --section 195:225 --ask
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LIB / "src"))
sys.path.insert(0, str(LIB / "scripts"))

import reference_delta as rd            # noqa: E402
from reaper_audio.analysis import ossicle as os_   # noqa: E402
from reaper_audio.analysis import spatial_timeline as st   # noqa: E402

SYSTEM = """You are analysing a mix against reference records. You get measured numbers with a \
measured audio clip. Rules, in order of importance:
1. Use the numbers as the baseline; the audio confirms or contradicts them, it does not replace them.
2. Every claim must name the number it rests on, and the direction of the change (how many dB, \
how much wider). No adjectives without a number.
3. Stay inside tone, level, width, dynamics and space. Do not discuss arrangement, structure, \
what to add or remove, or songwriting.
4. When a delta is inside a sensible tolerance, say "fine" and move on; do not invent work.
5. End with at most three changes, ordered by expected effect, each with the element and the amount."""


def section(path: Path, forced):
    x, sr, span = rd.window(path, forced)
    return rd.measure(x, sr), span


def timeline_text(path: Path) -> list[str]:
    report = st.timeline(str(path), window_s=30.0, hop_s=30.0)
    rows = []
    for w in report.get("windows", []):
        span = w.get("window_s") or [0.0, 0.0]
        rows.append(f"  {span[0]:.0f}-{span[1]:.0f}s corr {float(w.get('correlation') or 0):+.2f} "
                    f"bal {float(w.get('balance_db') or 0):+.1f} dB "
                    f"side/mid {float(w.get('side_mid_db') or 0):+.1f} dB")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ours", required=True)
    ap.add_argument("--ref", action="append", default=[])
    ap.add_argument("--section", default=None, metavar="START:END")
    ap.add_argument("--ref-section", default=None, metavar="START:END")
    ap.add_argument("--ask", action="store_true", help="send the brief + audio to the model")
    ap.add_argument("--describe", action="store_true",
                    help="ask the model to describe ours and every reference separately")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    def parse(w):
        return None if not w else tuple(float(v) for v in w.split(":"))

    ours, span = section(Path(args.ours), parse(args.section))
    refs = []
    for i, ref in enumerate(args.ref):
        m, rspan = section(Path(ref), parse(args.ref_section))
        refs.append({"name": Path(ref).stem, "span": rspan, "metrics": m})

    keys = [k for k in ours]
    mean_ref = {k: round(float(sum(r["metrics"][k] for r in refs) / len(refs)), 2) for k in keys} if refs else {}

    lines = [f"OUR SECTION: {args.ours}  {span[0]:.0f}-{span[1]:.0f}s"]
    lines += [f"  {k}: {ours[k]}" for k in keys]
    for r in refs:
        lines.append(f"REFERENCE {r['name']}  {r['span'][0]:.0f}-{r['span'][1]:.0f}s")
        lines += [f"  {k}: {r['metrics'][k]}" for k in keys]
    if mean_ref:
        lines.append("DELTA (ours minus mean of references)")
        lines += [f"  {k}: {round(ours[k] - mean_ref[k], 2):+}" for k in keys]
    lines.append("STEREO TIMELINE OF OUR TRACK (30 s windows)")
    lines += timeline_text(Path(args.ours))

    brief = "\n".join(lines)
    print(brief)

    out = {"ours": {"path": args.ours, "span": span, "metrics": ours},
           "references": refs, "mean_reference": mean_ref,
           "delta": {k: round(ours[k] - mean_ref[k], 2) for k in keys} if mean_ref else {},
           "brief": brief}

    if args.ask:
        question = (brief + "\n\nNow analyse: what is off against the references, by how much, "
                           "which element causes it, and the three changes that would close the gap "
                           "most. Use the numbers above in every sentence.")
        report = os_.analyze([{"wav": args.ours, "start_s": span[0], "end_s": span[1],
                               "question": question, "max_new_tokens": 600}], dtype="float16")
        out["answer"] = report["answers"][0]["text"]
        out["runtime"] = report["runtime"]
        print("\n=== MODEL ANSWER ===\n" + out["answer"])

    if args.describe:
        # Numbers stay in our layer: the model is asked to listen, per clip, and the two
        # descriptions are shown side by side with the measured deltas next to them.
        jobs = [{"wav": args.ours, "start_s": span[0], "end_s": span[1],
                 "question": os_.DEFAULT_QUESTION, "max_new_tokens": 400}]
        for ref in args.ref:
            rspan = parse(args.ref_section) or (0.0, 30.0)
            jobs.append({"wav": ref, "start_s": rspan[0], "end_s": rspan[1],
                         "question": os_.DEFAULT_QUESTION, "max_new_tokens": 400})
        described = os_.analyze(jobs, dtype="float16")

        def fields(text: str) -> dict[str, str]:
            found = {}
            for line in text.splitlines():
                line = line.strip()
                if len(line) > 3 and line[0].isdigit() and line[1] in ".)":
                    found[line.split(".")[0]] = line[2:].strip()
            return found

        cols = [{"name": "ours", "span": span, "fields": fields(described["answers"][0]["text"])}]
        for ref, answer in zip(args.ref, described["answers"][1:]):
            cols.append({"name": Path(ref).stem, "span": None, "fields": fields(answer["text"])})
        out["descriptions"] = cols
        print("\n=== DESCRIPTIONS (model) vs DELTAS (measured) ===")
        if out.get("delta"):
            print("measured delta: " + ", ".join(f"{k} {out['delta'][k]:+}" for k in
                                                ("lowmid_ratio", "presence_ratio", "air_ratio")))
        for key in sorted({k for c in cols for k in c["fields"]}, key=lambda s: int(s)):
            print(f"\n{key}.")
            for c in cols:
                print(f"   [{c['name']}] {c['fields'].get(key, '-')[:220]}")

    path = Path(args.out or (Path(args.ours).parent / "context_brief.json"))
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\nwritten:", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
