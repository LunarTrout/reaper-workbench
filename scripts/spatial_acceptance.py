"""Acceptance harness for the Ossicle-4B backend.

Stage 1 (runs now, no GPU, no model): build known binaural scenes and print the measured
facts, proving the harness itself carries real L/R channels and that swapping the
channels mirrors the scene.

Stage 2 (GPU only): ask the model where the source is for each scene, then repeat with
L/R swapped. Pass = the answer flips sides for the panned scenes and stays centred for
the centred one.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 16000
OUT = Path(os.environ.get(
    "REAPER_WORKBENCH_SCENE_DIR") or
    Path(__file__).resolve().parents[1] / "work" / "spatial_scenes")


def tone(freq: float, seconds: float, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def noise(seconds: float, amp: float = 0.08, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (amp * rng.standard_normal(int(seconds * SR))).astype(np.float32)


def scene(name: str, left: np.ndarray, right: np.ndarray) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.wav"
    sf.write(str(path), np.stack([left, right], axis=1), SR, subtype="FLOAT")
    swapped = OUT / f"{name}_swapped.wav"
    sf.write(str(swapped), np.stack([right, left], axis=1), SR, subtype="FLOAT")
    return path


def build(seconds: float = 8.0) -> dict[str, dict]:
    n = int(seconds * SR)
    silence = np.zeros(n, dtype=np.float32)
    scenes = {
        "left_only": scene("left_only", tone(440, seconds), silence),
        "right_only": scene("right_only", silence, tone(440, seconds)),
        "center": scene("center", tone(500, seconds, 0.4), tone(500, seconds, 0.4)),
        "three_sources": scene(
            "three_sources",
            tone(220, seconds, 0.3) + noise(seconds, 0.05, 1),
            tone(880, seconds, 0.3) + noise(seconds, 0.05, 2)),
    }
    return scenes


def facts(path: Path) -> dict:
    from reaper_audio.analysis import ossicle as ms
    x, sr = ms.load_binaural(path, allow_mono=False)
    f = ms.stereo_facts(x)
    return {"shape": list(x.shape), "sr": sr, **f}


def project_case(start_s: float, end_s: float, question: str) -> dict:
    """Render a real project range as stereo and compare the model with REAPER's pan/level.

    GPU-only: the backend refuses before any render when the card is unavailable.
    """
    import tempfile
    from pathlib import Path

    import reaper_audio.mcp_server as m
    from reaper_audio.analysis import ossicle as ms

    ms.require_gpu()                      # fail fast, never render without a GPU
    session = m._session(None, None)
    ctx = session.snapshot(include_fx_params=False)
    tmp = Path(tempfile.mkdtemp(prefix="spatial-project-"))
    try:
        session.bridge.capture_render(expected_revision=ctx["revision"], output_dir=str(tmp),
                                      job_id="spatial", expected_track_count=ctx["track_count"],
                                      timeout_s=1800.0, section_start_s=start_s,
                                      section_end_s=end_s, master_only=True)
        wav = next(tmp.glob("*.wav"))
        answer = ms.analyze([{"wav": str(wav), "question": question}])
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    tracks = [{"name": t["name"], "pan": round(float(t.get("pan", 0.0)), 3),
               "volume": round(float(t.get("volume", 1.0)), 3),
               "mute": bool(t.get("mute"))} for t in ctx.get("tracks", [])]
    return {"ok": True, "range": [start_s, end_s], "model": answer,
            "reaper": {"tracks": tracks,
                       "note": "pan -1..1 and fader values straight from the project"},
            "caveat": ("A dense mix is outside the model's training distribution; compare its "
                       "claims against these measured values, never treat them as truth.")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-model", action="store_true",
                   help="stage 2: requires the V100; otherwise only DSP facts are printed")
    ap.add_argument("--question", default="Where is the sound source located: left, right or centre?")
    ap.add_argument("--project", nargs=2, type=float, metavar=("START_S", "END_S"),
                    help="GPU-only: analyse a real project range and print REAPER pan/level")
    args = ap.parse_args()

    if args.project:
        print(json.dumps(project_case(args.project[0], args.project[1], args.question),
                         indent=2))
        return

    scenes = build()
    report = {}
    for name, path in scenes.items():
        report[name] = {"path": str(path), "facts": facts(path),
                        "swapped_facts": facts(OUT / f"{name}_swapped.wav")}
    print(json.dumps(report, indent=2))

    if not args.run_model:
        print("\nstage 1 only: no model was loaded (GPU not required for this part)")
        return

    # One model load for every scene, swapped and normal: analyse() takes a job list.
    from reaper_audio.analysis import ossicle as ms
    jobs, names = [], []
    for name, path in scenes.items():
        jobs.append({"wav": str(path), "question": args.question})
        names.append(name)
        jobs.append({"wav": str(OUT / f"{name}_swapped.wav"), "question": args.question})
        names.append(name + "_swapped")
    reply = ms.analyze(jobs)
    answers = {"answers": dict(zip(names, reply["answers"])),
               "gpu": reply["gpu"], "runtime": reply["runtime"],
               "caveat": reply["limits"]["caveat"]}
    print(json.dumps(answers, indent=2))


if __name__ == "__main__":
    main()
