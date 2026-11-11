"""Ossicle-4B backend: audio-language model on the real L/R channels, GPU-only.

Replaces MiDashengLM-Spatial as the library's listening model. Reasons for the swap:
the spatial checkpoint publishes no quantised weights anywhere and needs 33.5 GB of
fp32 shards on a 16 GB card, while Ossicle-4B is 9.9 GB (3.6 GB with its own int4),
carries a stereo adapter, and answers questions about music, sound and speech.

What is kept from the old contract, because it is the honest part:
  * the two channels fed to the model are the real, synchronous L/R channels of the
    source audio - never summed, never concatenated; mono input is refused unless the
    caller explicitly allows it, and a mono signal carries no direction at all;
  * the model is only ever asked for comparisons, never for absolute adjectives, and
    every answer is returned next to the measured stereo facts of the same clip;
  * inference is GPU-only. No CPU path, no disk offload, no silent fallback.

Limits worth stating every time: the checkpoint was trained on clips of at most 30 s;
clips over 120 s are streamed through the model, and stereo cues are not used for those
long clips, so this module refuses anything above 120 s instead of quietly dropping the
direction information the analysis was asked for.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from ..errors import ReaperAudioError

_LIB_ROOT = Path(__file__).resolve().parents[3]
MODEL_DIR = Path(os.environ.get("OSSICLE_MODEL_DIR",
                                _LIB_ROOT / "work" / "models" / "ossicle-4b"))
REVISION = "d4def766b7465b31ee93853acd38964f699d652e"
MANIFEST = MODEL_DIR / "manifest.json"
WORKER = Path(__file__).resolve().parents[3] / "scripts" / "ossicle_worker.py"
PYTHON = os.environ.get(
    "OSSICLE_PYTHON",
    r"C:/Users/epicc/mcp/flstudio-mcp/.venv-ossicle/Scripts/python.exe")
SR = 16000
MIN_S = 1.0
MAX_S = 120.0        # beyond this the model streams pieces and stops using stereo cues
TRAINED_S = 30.0     # clips the checkpoint was actually trained on

# Ask for comparisons, never for adjectives. The model answers in a couple of hundred
# tokens, so the battery is ordered by what is most useful to a mixer.
DEFAULT_QUESTION = """Listen to this excerpt and answer in short labelled lines. Compare, do not label: words like bright, dark or wide mean nothing without a reference.
SOURCES: the distinct sources you can pick out, most dominant first.
SIDES: which sources sit left, which right, which in the centre; say if that changes during the excerpt.
MASKING: name pairs, "A hides B", and which of the two loses.
LOW END: kick against bass, and the low end against everything else - which is louder.
MOVEMENT: anything that grows or shrinks relative to the rest, and where.
WIDTH: which element is widest in the stereo image and which is narrowest.
PROBLEMS: for each, the direction of the fix (raise/lower/narrow/widen X relative to Y), most important first.
CONFIDENCE: low, medium or high, and what you could not judge."""


class OssicleError(ReaperAudioError):
    """Anything that stops the audio model, with a machine-readable code."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": self.message,
                                       "details": self.details}}


class GpuRequired(OssicleError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__("GPU_REQUIRED", message, details)


def manifest() -> dict[str, Any]:
    if not MANIFEST.exists():
        raise OssicleError("MODEL_MANIFEST_MISSING",
                           "manifest.json with sizes and sha256 is missing",
                           {"path": str(MANIFEST)})
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def verify_weights() -> dict[str, Any]:
    """Size (and sha256 where the Hub publishes one) of every root file."""
    import hashlib

    man = manifest()
    rows, bad, checked = [], [], 0
    for name, meta in sorted(man["files"].items()):
        path = MODEL_DIR / name
        row = {"file": name, "expected": meta.get("size"), "exists": path.exists()}
        if path.exists():
            row["size"] = path.stat().st_size
            row["complete"] = row["size"] == row["expected"]
            if row["complete"] and meta.get("sha256"):
                h = hashlib.sha256()
                with path.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 22), b""):
                        h.update(chunk)
                row["sha256_match"] = h.hexdigest() == meta["sha256"]
                checked += 1
        if not row.get("complete") or row.get("sha256_match") is False:
            bad.append(name)
        rows.append(row)
    return {"ok": not bad, "revision": REVISION, "license": man.get("license"),
            "files_checked": len(rows), "sha256_checked": checked,
            "incomplete": bad, "files": rows}


def _probe_python() -> dict[str, Any]:
    """Ask the model interpreter what torch sees. Read-only, no inference."""
    exe = Path(PYTHON)
    if not exe.exists():
        return {"ok": False, "reason": f"interpreter missing: {exe}", "python": str(exe)}
    code = (
        "import json, torch\n"
        "info={'ok': True, 'torch': torch.__version__, 'cuda_build': torch.version.cuda,\n"
        "      'available': bool(torch.cuda.is_available()), 'devices': [],\n"
        "      'bf16': bool(torch.cuda.is_available() and torch.cuda.is_bf16_supported())}\n"
        "if info['available']:\n"
        "    for i in range(torch.cuda.device_count()):\n"
        "        p = torch.cuda.get_device_properties(i)\n"
        "        info['devices'].append({'index': i, 'name': p.name,\n"
        "                                'cc': f'{p.major}.{p.minor}',\n"
        "                                'vram_mb': int(p.total_memory / 1048576)})\n"
        "try:\n"
        "    import transformers\n"
        "    info['transformers'] = transformers.__version__\n"
        "    info['qwen3_5'] = True\n"
        "    assert __import__('transformers.models.qwen3_5.modeling_qwen3_5') is not None\n"
        "except Exception as exc:\n"
        "    info['qwen3_5'] = False\n"
        "    info['transformers_error'] = f'{type(exc).__name__}: {exc}'[:300]\n"
        "print(json.dumps(info))\n")
    try:
        out = subprocess.run([str(exe), "-c", code], capture_output=True, text=True, timeout=300)
    except Exception as exc:                    # noqa: BLE001
        return {"ok": False, "reason": str(exc), "python": str(exe)}
    if out.returncode != 0:
        return {"ok": False, "reason": (out.stderr or out.stdout).strip()[-400:],
                "python": str(exe)}
    try:
        info = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:                           # noqa: BLE001
        return {"ok": False, "reason": "unparsable probe output", "raw": out.stdout[-400:]}
    info["python"] = str(exe)
    return info


def status() -> dict[str, Any]:
    man = manifest()
    probe = _probe_python()
    v100 = [d for d in probe.get("devices", []) if "V100" in d.get("name", "").upper()]
    return {
        "ok": bool(probe.get("ok") and probe.get("available") and v100
                   and probe.get("qwen3_5")),
        "engine": "Ossicle-4B",
        "model": {"dir": str(MODEL_DIR), "revision": REVISION, "license": man.get("license"),
                  "total_gb": round(man.get("total_bytes", 0) / 1e9, 2),
                  "files_present": sum((MODEL_DIR / n).exists() for n in man["files"])},
        "weights": verify_weights(),
        "python": probe,
        "v100": v100,
        "limits": {"sample_rate": SR, "clip_s": [MIN_S, MAX_S], "trained_clip_s": TRAINED_S,
                   "note": ("stereo adapter used only up to 120 s; dense multi-source metal "
                            "mixes are outside the model's training distribution, so its "
                            "statements are hints to check against measured facts")},
    }


def require_gpu() -> dict[str, Any]:
    probe = _probe_python()
    if not probe.get("ok"):
        raise GpuRequired("the model interpreter cannot import torch", details=probe)
    if not probe.get("available"):
        raise GpuRequired("CUDA is not available to torch in the model interpreter "
                          "(no CPU inference allowed)", details=probe)
    v100 = [d for d in probe.get("devices", []) if "V100" in d.get("name", "").upper()]
    if not v100:
        raise GpuRequired("no Tesla V100 visible to torch", details=probe)
    if not probe.get("qwen3_5"):
        raise GpuRequired("transformers in the model interpreter has no qwen3_5 model "
                          "(Ossicle-4B needs transformers >= 5)", details=probe)
    return {"device": v100[0], "probe": probe}


def load_binaural(path: str | Path, start_s: float = 0.0, end_s: float | None = None,
                  allow_mono: bool = False) -> tuple[np.ndarray, int]:
    """Return (2, T) float32 at 16 kHz with the real L/R channels preserved."""
    import soundfile as sf

    info = sf.info(str(path))
    if info.channels != 2 and not allow_mono:
        raise OssicleError(
            "NOT_STEREO",
            "the audio model needs two real channels; mono input carries no direction",
            {"path": str(path), "channels": info.channels})
    data, sr = sf.read(str(path), dtype="float32", always_2d=True,
                       start=int(start_s * info.samplerate),
                       stop=None if end_s is None else int(end_s * info.samplerate))
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    x = data[:, :2].T.copy()                       # (2, T), channels first, no summing
    if sr != SR:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(SR, sr)
        x = np.stack([resample_poly(ch, SR // g, sr // g) for ch in x]).astype(np.float32)
    if x.shape[1] < int(MIN_S * SR):
        raise OssicleError("TOO_SHORT", "clip shorter than the model minimum",
                           {"samples": int(x.shape[1]), "min_s": MIN_S})
    if x.shape[1] > int(MAX_S * SR):
        raise OssicleError("TOO_LONG",
                           "clip longer than the 120 s stereo ceiling; ask for a shorter "
                           "section instead of losing the stereo cues",
                           {"samples": int(x.shape[1]), "max_s": MAX_S})
    return x, SR


def stereo_facts(x: np.ndarray) -> dict[str, Any]:
    """Measured facts about the two channels (not model impressions)."""
    left, right = x[0], x[1]
    denom = (np.linalg.norm(left) * np.linalg.norm(right)) + 1e-12
    corr = float(np.dot(left, right) / denom)
    rms = [float(np.sqrt(np.mean(ch ** 2))) for ch in x]
    return {"correlation": round(corr, 4),
            "rms_dbfs": [round(20 * np.log10(r + 1e-12), 2) for r in rms],
            "balance_db": round(20 * np.log10((rms[0] + 1e-12) / (rms[1] + 1e-12)), 2),
            "mid_side": {"mid": [round(float(v), 6) for v in ((left + right) / 2)[:3]],
                         "side": [round(float(v), 6) for v in ((left - right) / 2)[:3]]}}


def analyze(jobs: Iterable[dict[str, Any]], device: int = 0,
            dtype: str | None = None, quantize: str = "none",
            max_new_tokens: int = 220) -> dict[str, Any]:
    """Run the audio model on one or more stereo clips in a single model load.

    Each job: {"wav": path, "wav2": optional second clip, "question": str,
               "start_s": float, "end_s": float, "allow_mono": bool}.
    The model process is started once and answers every job; that is what keeps a
    second question from reloading 9.9 GB of weights.
    """
    gpu = require_gpu()
    weights = verify_weights()
    if not weights["ok"]:
        raise OssicleError("WEIGHTS_INCOMPLETE",
                           "model files are missing or do not match their pinned size/sha256",
                           {"incomplete": weights["incomplete"]})

    prepared, facts = [], []
    tmp = tempfile.TemporaryDirectory(prefix="ossicle-")
    try:
        import soundfile as sf

        for i, job in enumerate(jobs):
            x, sr = load_binaural(job["wav"], job.get("start_s", 0.0), job.get("end_s"),
                                  allow_mono=bool(job.get("allow_mono")))
            facts.append(stereo_facts(x))
            out = Path(tmp.name) / f"clip_{i:03d}.wav"
            sf.write(str(out), x.T, sr, subtype="FLOAT")   # (T, 2) written back as stereo
            audios = [str(out)]
            if job.get("wav2"):
                x2, _ = load_binaural(job["wav2"], job.get("start_s", 0.0), job.get("end_s"),
                                      allow_mono=bool(job.get("allow_mono")))
                facts.append(stereo_facts(x2))
                out2 = Path(tmp.name) / f"clip_{i:03d}_b.wav"
                sf.write(str(out2), x2.T, sr, subtype="FLOAT")
                audios.append(str(out2))
            prepared.append({"audios": audios,
                             "question": job.get("question") or DEFAULT_QUESTION,
                             "max_new_tokens": int(job.get("max_new_tokens", max_new_tokens))})
        payload = {"model_dir": str(MODEL_DIR), "device": int(device), "dtype": dtype,
                   "quantize": quantize, "revision": REVISION, "jobs": prepared}
        proc = subprocess.run([PYTHON, str(WORKER)], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=3600)
    finally:
        tmp.cleanup()
    if proc.returncode != 0:
        raise OssicleError("INFERENCE_FAILED", (proc.stderr or proc.stdout).strip()[-800:],
                           {"returncode": proc.returncode})
    try:
        reply = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:                    # noqa: BLE001
        raise OssicleError("BAD_WORKER_OUTPUT", str(exc),
                           {"stdout": proc.stdout[-500:], "stderr": proc.stderr[-500:]})
    if not reply.get("ok"):
        err = reply.get("error") or {}
        raise OssicleError(err.get("code", "INFERENCE_FAILED"),
                           err.get("message", "worker failed"), err.get("details"))
    rows = reply["answers"]
    fact_iter = iter(facts)
    for row in rows:
        count = max(1, len(row.get("audios") or []))
        row_facts = [next(fact_iter, None) for _ in range(count)]
        row["stereo_facts"] = row_facts[0] if count == 1 else row_facts
    return {"ok": True, "engine": "Ossicle-4B", "revision": REVISION,
            "gpu": gpu["device"],
            "runtime": {k: reply.get(k) for k in
                        ("dtype", "quantize", "vram_peak_mb", "vram_weights_gb", "load_seconds")},
            "answers": rows,
            "limits": {"clip_s": [MIN_S, MAX_S], "sample_rate": SR,
                       "trained_clip_s": TRAINED_S,
                       "caveat": "hints from a 4.9B audio model, not measurements; dense "
                                 "multi-source mixes are out of its training distribution"}}


def compare(source_a: str | Path, source_b: str | Path, question: str | None = None,
            start_s: float = 0.0, end_s: float | None = None,
            quantize: str = "none") -> dict[str, Any]:
    """Ask the model to compare two stereo clips in one request (A/B, not absolutes).

    Both clips are prepared as real (2, T) binaural signals and sent together, which is
    the model's own "Audio 1: / Audio 2:" mode - a pairwise judgement, checkable against
    the measured facts returned next to it.
    """
    default = ("Compare Audio 1 and Audio 2. Which one is {axis}? Answer A or B, then how "
               "much (a little / clearly / a lot), then what in the audio makes it so. "
               "If they are equal, say equal and why.")
    question = question or default.format(axis="more balanced across the stereo image")
    return analyze([{"wav": source_a, "wav2": source_b, "question": question,
                     "start_s": start_s, "end_s": end_s}], quantize=quantize)
