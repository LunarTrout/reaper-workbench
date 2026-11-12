"""Run one measured EQ cycle on a track of the open project.

The corpus matrix is measured once (or reused from an .npz cache); the cycle
itself renders the section through REAPER and keeps only moves that reduce the
measured distance.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reaper_audio.corpus.measure import FEATURE_NAMES  # noqa: E402
from reaper_audio.corpus.model import CorpusModel  # noqa: E402
from reaper_audio.corpus.report import corpus_matrix_from_dir  # noqa: E402
from reaper_audio.daw.cycle import run_eq_cycle  # noqa: E402
from reaper_audio.daw.session import ProjectSession  # noqa: E402
from reaper_audio.reaper.bridge_client import ReaperBridge  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", help="directory with reference excerpts")
    ap.add_argument("--features", help="cached .npz matrix (x) instead of --corpus")
    ap.add_argument("--track", required=True, help="track GUID or unique name")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--out", default="work/cycle")
    ap.add_argument("--plugin", default="VST: ReaEQ (Cockos)")
    ap.add_argument("--eq-count", type=int, default=2)
    ap.add_argument("--max-moves", type=int, default=None)
    ap.add_argument("--policy", help="JSON file with gate thresholds")
    args = ap.parse_args()

    if args.features:
        matrix = np.load(args.features)["x"]
    elif args.corpus:
        matrix = corpus_matrix_from_dir(args.corpus)
    else:
        ap.error("either --corpus or --features is required")
    model = CorpusModel.fit(matrix, FEATURE_NAMES)

    session = ProjectSession(ReaperBridge())
    tracks = session.snapshot()["tracks"]
    if args.track.startswith("{"):
        guid = args.track
    else:
        matches = [t for t in tracks if t["name"] == args.track]
        if len(matches) != 1:
            names = ", ".join(t["name"] for t in tracks)
            ap.error(f"track name {args.track!r} matches {len(matches)} tracks; have: {names}")
        guid = matches[0]["guid"]

    result = run_eq_cycle(session, track_guid=guid, model=model, output_dir=args.out,
                          section_start_s=args.start, section_end_s=args.end,
                          plugin=args.plugin, eq_count=args.eq_count,
                          max_moves=args.max_moves,
                          policy=json.loads(Path(args.policy).read_text()) if args.policy else None)
    print(f"before: {result.lufs_before:.2f} LUFS, distance {result.distance_before:.3f}")
    for move in result.accepted:
        print(f"  accepted {move['freq_hz']:.0f} Hz {move['gain_db']:+.1f} dB "
              f"-> {move['distance']:.3f}")
    for move in result.rejected:
        print(f"  rejected {move['freq_hz']:.0f} Hz {move['gain_db']:+.1f} dB "
              f"-> {move['distance']:.3f}")
    print(f"after:  {result.lufs_after:.2f} LUFS, distance {result.distance_after:.3f} "
          f"(ratio {result.distance_after / result.distance_before:.3f})")
    print("remaining:", [(n, round(z, 2)) for n, z, _ in result.contributions[:5]])
    if result.gate_violations:
        print("gate violations:", result.gate_violations)

    out = Path(args.out) / "cycle.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    print("written:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
