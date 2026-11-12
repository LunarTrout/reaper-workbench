"""Assess one audio file against a reference corpus.

Prints the distance, the empirical percentile (leave-one-out null), the
features that carry the distance, and the defect gates.  No score is invented.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reaper_audio.corpus.measure import FEATURE_NAMES  # noqa: E402
from reaper_audio.corpus.model import CorpusModel  # noqa: E402
from reaper_audio.corpus.manifest import Manifest  # noqa: E402
from reaper_audio.corpus.report import (assess_file, corpus_matrix_from_dir,  # noqa: E402
                                        distance_timeline)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus")
    ap.add_argument("--features", help="cached .npz matrix (x) instead of --corpus")
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out")
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--policy", help="JSON file with gate thresholds")
    ap.add_argument("--manifest", help="manifest JSON with stage/source facts")
    ap.add_argument("--stage", help="use only entries with this stage")
    ap.add_argument("--timeline", type=float, metavar="WINDOW_S",
                    help="also print the distance timeline with this window")
    args = ap.parse_args()

    if args.features:
        import numpy as np
        matrix = np.load(args.features)["x"]
    elif args.corpus:
        manifest = Manifest.load(args.manifest) if args.manifest else None
        matrix = corpus_matrix_from_dir(args.corpus, window_s=args.window,
                                        manifest=manifest, stage=args.stage)
    else:
        ap.error("either --corpus or --features is required")
    model = CorpusModel.fit(matrix, FEATURE_NAMES)
    policy = json.loads(Path(args.policy).read_text()) if args.policy else {}
    report = assess_file(model, matrix, args.audio, policy=policy)

    print("corpus:", model.summary())
    print(f"distance: {report.distance:.3f}  percentile: {report.percentile:.2f}")
    print("extras:", {k: round(v, 2) if isinstance(v, float) else v
                      for k, v in report.extras.items()})
    print("top contributions:")
    for name, z, share in report.contributions[:8]:
        print(f"  {name:<22} z {z:+.2f}  share {share:+.2f}")
    print("gates:", {k: round(v, 3) for k, v in report.gates.items()})
    print("violations:", report.violations or "none")
    if args.timeline:
        import soundfile as sf
        info = sf.info(args.audio)
        audio, sr = sf.read(args.audio, dtype="float64", always_2d=True)
        timeline = distance_timeline(model, matrix, audio, sr, window_s=args.timeline)
        worst = sorted(timeline, key=lambda row: -row["distance"])[:5]
        print("worst windows:")
        for row in worst:
            print(f"  {row['start_s']:6.1f}-{row['end_s']:6.1f}s  {row['distance']:6.2f}"
                  f"  pct {row['percentile']:.2f}")

    if args.out:
        Path(args.out).write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        print("written:", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
