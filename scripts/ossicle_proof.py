"""Live proof for the Ossicle-4B backend: known-answer pairs plus a real master render.

Runs three jobs in a single worker call so the 9.9 GB load is paid once:

  1. two calibration variants with a known axis (brightness_0 vs brightness_2),
  2. the hard-left / hard-right acceptance scenes (orientation must be named),
  3. a real section from the supplied master render, with the default battery.

Writes work/ossicle_proof.json next to the measured facts, so a claim can be checked
against the measurement instead of believed.

    PYTHONPATH=src python scripts/ossicle_proof.py --master path/to/stereo-master.wav
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LIB / "src"))

from reaper_audio.analysis import ossicle as os_   # noqa: E402

CAL = LIB / "work" / "spatial_calibration"
SCENES = Path(os.environ.get(
    "REAPER_WORKBENCH_SCENE_DIR") or LIB / "work" / "spatial_scenes")
OUT = LIB / "work" / "ossicle_proof.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", type=Path, required=True,
                        help="stereo master render for the real-audio check")
    parser.add_argument("--calibration-dir", type=Path, default=CAL)
    parser.add_argument("--scenes-dir", type=Path, default=SCENES)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    status = os_.status()
    print(json.dumps({"status_ok": status["ok"],
                      "engine": status["engine"],
                      "gpu": [d["name"] for d in status.get("v100", [])],
                      "transformers": status["python"].get("transformers"),
                      "qwen3_5": status["python"].get("qwen3_5"),
                      "weights_ok": status["weights"]["ok"]}, indent=2), flush=True)
    if not status["ok"]:
        print("status not ok - stopping before any inference")
        return 1

    jobs = [
        {"wav": str(args.calibration_dir / "brightness_0.wav"),
         "wav2": str(args.calibration_dir / "brightness_2.wav"),
         "question": ("Compare Audio 1 and Audio 2. Which one has more energy in the high "
                      "frequencies? Answer Audio 1 or Audio 2, then how much, then what makes "
                      "it so.")},
        {"wav": str(args.scenes_dir / "left_only.wav"),
         "wav2": str(args.scenes_dir / "right_only.wav"),
         "question": ("Audio 1 and Audio 2 are the same tone panned differently. For each "
                      "clip say whether the sound sits on the left, on the right or in the "
                      "centre, and name Audio 1 and Audio 2 explicitly.")},
        {"wav": str(args.master),
         "question": os_.DEFAULT_QUESTION},
    ]
    report = os_.analyze(jobs, dtype="float16", quantize="none", max_new_tokens=260)
    report["status"] = {k: status[k] for k in ("engine", "model", "v100")}
    report["status"]["weights_ok"] = status["weights"]["ok"]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str),
                        encoding="utf-8")
    print(json.dumps({"runtime": report["runtime"],
                      "answers": [{"question": a["question"][:60], "text": a["text"]}
                                  for a in report["answers"]]}, indent=2, ensure_ascii=False))
    print("written:", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
