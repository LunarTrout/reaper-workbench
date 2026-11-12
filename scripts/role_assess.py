"""Role-level assessment: our stems against reference stems, same separator.

Prints the reconstruction error first — without it the role numbers are not
evidence — then one line per role with the corpus distance, the empirical
percentile and the three features that carry the distance.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reaper_audio.corpus.measure import measure_signal  # noqa: E402
from reaper_audio.corpus.roles import (discover, reconstruction, role_matrix,  # noqa: E402
                                       role_models)
from reaper_audio.corpus.model import CorpusModel  # noqa: E402
from reaper_audio.corpus.measure import FEATURE_NAMES  # noqa: E402

import soundfile as sf  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refs", required=True, help="root with <model>/<source>/<role>.wav")
    ap.add_argument("--ours", required=True, help="directory with our stems")
    ap.add_argument("--mix", required=True, help="the mix our stems came from")
    ap.add_argument("--window", type=float, default=10.0)
    ap.add_argument("--out")
    args = ap.parse_args()

    our_paths = discover(args.ours)
    refs = discover(args.refs)
    roles = sorted(set(our_paths) & set(refs))
    if not roles:
        ap.error("no common roles between the two stem sets")

    recon_paths = {role: our_paths[role][0] for role in roles}
    recon = reconstruction(args.mix, recon_paths) if len(recon_paths) > 1 else None
    print("reconstruction:", recon.to_dict() if recon else "not enough stems")

    models = role_models({role: refs[role] for role in roles}, window_s=args.window)
    report = {"reconstruction": recon.to_dict() if recon else None, "roles": {}}
    print(f"\n{'role':<12}{'refs':>5}{'windows':>8}{'distance':>10}{'pct':>6}   top")
    for role in roles:
        model = models[role]
        x, sr = sf.read(str(our_paths[role][0]), dtype="float64", always_2d=True)
        step = int(args.window * sr)
        rows = [measure_signal(x[s:s + step], sr).values
                for s in range(0, x.shape[0] - step + 1, step)]
        if not rows:
            continue
        matrix = np.stack(rows)
        distances = model.distance(matrix)
        loo = model.loo_distances(getattr(model, "matrix"))
        percentile = float(np.mean(loo <= float(np.median(distances))))
        contributions = model.contributions(rows[len(rows) // 2])[:3]
        top = ", ".join(f"{n} {z:+.1f}" for n, z, _ in contributions)
        print(f"{role:<12}{len(refs[role]):>5}{len(rows):>8}{np.median(distances):>10.2f}"
              f"{percentile:>6.2f}   {top}")
        report["roles"][role] = {
            "references": len(refs[role]),
            "windows": len(rows),
            "distance_median": float(np.median(distances)),
            "percentile": percentile,
            "contributions": [{"feature": n, "z": z, "share": s}
                              for n, z, s in contributions],
        }

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("written:", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
