"""Whole-track pass as 30 s windows: what happens where, one question per window.

The stereo adapter is not used past 120 s, so a 5-minute master cannot be asked as one
clip without losing exactly the information we care about. Instead the track is cut into
30 s windows (the length the checkpoint was trained on) and every window is a separate
question in a single worker call, so the model loads once.

    PYTHONPATH=src python scripts/ossicle_timeline.py work/preview_full_master.wav
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LIB / "src"))

from reaper_audio.analysis import ossicle as os_   # noqa: E402

QUESTION = ("This is one 30-second section of a black metal song. Answer in short lines: "
            "which sources are present (drums, bass, guitars, vocals, keys), which dominates, "
            "does anything change inside the section (entry, drop, tempo, density), and where "
            "does the guitar sit in the stereo image.")


def main() -> int:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else LIB / "work" / "preview_full_master.wav")
    import soundfile as sf

    info = sf.info(str(src))
    total = info.frames / info.samplerate
    window, hop = 30.0, 30.0
    jobs, spans = [], []
    t = 0.0
    while t < total - 3.0:
        end = min(t + window, total)
        jobs.append({"wav": str(src), "start_s": t, "end_s": end, "question": QUESTION})
        spans.append([round(t, 1), round(end, 1)])
        t += hop

    report = os_.analyze(jobs, dtype="float16", max_new_tokens=180)
    out = {"source": str(src), "duration_s": round(total, 2),
           "runtime": report["runtime"], "gpu": report["gpu"], "windows": []}
    for span, answer in zip(spans, report["answers"]):
        out["windows"].append({"span": span, "facts": answer["stereo_facts"],
                               "text": answer["text"], "seconds": answer["seconds"]})
    path = LIB / "work" / "ossicle_timeline.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    for row in out["windows"]:
        print(f"\n[{row['span'][0]:>6.1f}-{row['span'][1]:>6.1f}] corr {row['facts']['correlation']:+.3f} "
              f"bal {row['facts']['balance_db']:+.1f} dB")
        print("   ", row["text"].replace("\n", " ")[:400])
    print("\nwritten:", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
