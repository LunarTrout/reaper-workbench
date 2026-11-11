"""Live proof for the Ossicle-4B backend: one model load, known-answer pairs + a real mix.

Runs three jobs in a single worker call so the 9.9 GB load is paid once:

  1. two calibration variants with a known axis (brightness_0 vs brightness_2),
  2. the hard-left / hard-right acceptance scenes (orientation must be named),
  3. a real section of the user's master, with the default battery.

Writes work/ossicle_proof.json next to the measured facts, so a claim can be checked
against the measurement instead of believed.

    PYTHONPATH=src python scripts/ossicle_proof.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LIB / "src"))

from reaper_audio.analysis import ossicle as os_   # noqa: E402

CAL = LIB / "work" / "spatial_calibration"
SCENES = Path(r"C:/Users/epicc/AppData/Local/hermes/cache/scratch/spatial_scenes")
MASTER = LIB / "work" / "mixcheck_master_1_05_1_30.wav"
OUT = LIB / "work" / "ossicle_proof.json"


def main() -> int:
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
        {"wav": str(CAL / "brightness_0.wav"), "wav2": str(CAL / "brightness_2.wav"),
         "question": ("Compare Audio 1 and Audio 2. Which one has more energy in the high "
                      "frequencies? Answer Audio 1 or Audio 2, then how much, then what makes "
                      "it so.")},
        {"wav": str(SCENES / "left_only.wav"), "wav2": str(SCENES / "right_only.wav"),
         "question": ("Audio 1 and Audio 2 are the same tone panned differently. For each "
                      "clip say whether the sound sits on the left, on the right or in the "
                      "centre, and name Audio 1 and Audio 2 explicitly.")},
        {"wav": str(MASTER),
         "question": os_.DEFAULT_QUESTION},
    ]
    report = os_.analyze(jobs, dtype="float16", quantize="none", max_new_tokens=260)
    report["status"] = {k: status[k] for k in ("engine", "model", "v100")}
    report["status"]["weights_ok"] = status["weights"]["ok"]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str),
                   encoding="utf-8")
    print(json.dumps({"runtime": report["runtime"],
                      "answers": [{"question": a["question"][:60], "text": a["text"]}
                                  for a in report["answers"]]}, indent=2, ensure_ascii=False))
    print("written:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
