"""Run the leave-one-track-out corpus benchmark.

Usage:
    PYTHONPATH=src python scripts/corpus_benchmark.py --corpus <dir> --out work/corpus_benchmark.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reaper_audio.corpus import run_benchmark  # noqa: E402

AUDIO_SUFFIXES = {".wav", ".flac", ".aiff", ".aif", ".mp3", ".ogg"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True, help="directory with reference excerpts")
    ap.add_argument("--out", default="work/corpus_benchmark.json")
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--only", help="comma-separated degradation names")
    ap.add_argument("--limit-files", type=int)
    args = ap.parse_args()

    paths = sorted(p for p in Path(args.corpus).iterdir() if p.suffix.lower() in AUDIO_SUFFIXES)
    if len(paths) < 3:
        print(f"need at least three excerpts, found {len(paths)}", file=sys.stderr)
        return 2

    only = [name.strip() for name in args.only.split(",")] if args.only else None
    report = run_benchmark(paths, window_s=args.window, max_seconds=args.seconds,
                           only=only, limit_files=args.limit_files)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"{'degradation':<16}{'expect':<10}{'n':>4}{'orig closer':>13}{'median margin':>15}{'ratio':>8}")
    for name, r in report["degradations"].items():
        print(f"{name:<16}{r['expect']:<10}{r['windows']:>4}{r['closer_original']:>13.2f}"
              f"{r['median_margin']:>+15.2f}{r['median_ratio']:>8.2f}")
    print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
