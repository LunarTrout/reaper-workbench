"""Music Flamingo provider: GPU-only audio understanding for REAPER projects.

Design rules (from the project brief):
* Inference runs only on the NVIDIA Tesla V100. No CPU fallback: if the GPU or the
  CUDA backend is not confirmed, every entry point raises ``GpuRequired``.
* The model consumes mono 16 kHz audio, so stereo is carried as explicitly labelled
  representations (L, R, Mid, Side) plus DSP facts measured on the original stereo.
* Long material is processed in 30 s windows and reported; nothing is truncated
  silently.
* Analysis is read-only: no project edits, no stems on the timeline, temporary WAVs
  live outside the project media folder and are removed afterwards.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..errors import ReaperAudioError

_LIB_ROOT = Path(__file__).resolve().parents[3]
MODEL_DIR = Path(os.environ.get(
    "FLAMINGO_MODEL_DIR") or _LIB_ROOT / "work" / "models" / "music-flamingo-q5_1")
MODEL_FILE = MODEL_DIR / "music-flamingo-Q5_1.gguf"
MMPROJ_FILE = MODEL_DIR / "mmproj-music-flamingo-bf16.gguf"
LLAMA_BIN = os.environ.get("FLAMINGO_LLAMA_BIN", "llama-mtmd-cli")
_LLAMA_FALLBACK_DIR = Path(os.environ.get(
    "FLAMINGO_LLAMA_DIR") or _LIB_ROOT / "work" / "tools" / "llama.cpp")
WINDOW_S = 30.0
MAX_TOTAL_S = 600.0          # documented ceiling; longer material is reported, not cut
SR = 16000


class FlamingoError(ReaperAudioError):
    """Structured error carrying a stable code for MCP clients."""

    code = "flamingo_error"

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message, details=details)
        self.code = code


class GpuRequired(FlamingoError):
    code = "GPU_REQUIRED"

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__("GPU_REQUIRED", message, details)


# --------------------------------------------------------------------------- GPU

def gpu_status() -> dict[str, Any]:
    """Probe the GPU through nvidia-smi and report exactly what is confirmed."""
    info: dict[str, Any] = {"ok": False, "device": None, "vram_mb": None,
                            "cuda_backend": None, "reason": None}
    exe = shutil.which("nvidia-smi")
    if not exe:
        info["reason"] = "nvidia-smi not found on PATH"
        return info
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total,driver_version",
                              "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=20)
    except Exception as exc:                       # noqa: BLE001
        info["reason"] = f"nvidia-smi failed: {exc}"
        return info
    if out.returncode != 0:
        info["reason"] = (out.stderr or out.stdout or "nvidia-smi returned non-zero").strip()[:400]
        return info
    line = (out.stdout or "").strip().splitlines()
    if not line:
        info["reason"] = "nvidia-smi returned no devices"
        return info
    parts = [p.strip() for p in line[0].split(",")]
    name = parts[0] if parts else ""
    vram = parts[1] if len(parts) > 1 else ""
    info.update(device=name, vram_mb=vram, driver=parts[2] if len(parts) > 2 else None)
    if "V100" not in name.upper():
        info["reason"] = f"unexpected device: {name}"
        return info
    info["cuda_backend"] = "nvidia-smi ok"
    info["ok"] = True
    return info


def llama_binary() -> str | None:
    """Resolve llama-mtmd-cli: env override, PATH, then the bundled CUDA build."""
    if os.sep in LLAMA_BIN or "/" in LLAMA_BIN:
        return LLAMA_BIN if Path(LLAMA_BIN).exists() else None
    found = shutil.which(LLAMA_BIN)
    if found:
        return found
    for name in (LLAMA_BIN + ".exe", LLAMA_BIN):
        candidate = _LLAMA_FALLBACK_DIR / name
        if candidate.exists():
            return str(candidate)
    return None


def cuda_devices() -> dict[str, Any]:
    """Ask the llama.cpp binary which devices its CUDA backend actually enumerates.

    ``-ngl 99`` only requests offload; it proves nothing on its own. This probe parses
    the ``Available devices:`` block only, rejects ``(none)`` and CUDA init failures,
    and fails closed on any doubt.
    """
    binary = llama_binary()
    if binary is None:
        return {"ok": False, "devices": [], "raw": "", "reason": "binary not found"}
    try:
        out = subprocess.run([binary, "--list-devices"], capture_output=True,
                             text=True, timeout=60)
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "devices": [], "raw": "", "reason": str(exc)}
    raw = ((out.stdout or "") + (out.stderr or "")).strip()
    devices: list[str] = []
    in_block = False
    saw_none = False
    for line in raw.splitlines():
        s = line.strip()
        if s.lower().startswith("available devices"):
            in_block = True
            continue
        if not in_block or not s:
            continue
        if s.startswith("(") or s.lower().startswith(("none", "no devices")):
            saw_none = True
            continue
        # a real entry looks like "CUDA0: NVIDIA Tesla V100-SXM2-16GB (16384 MiB, 16000 MiB free)"
        if re.match(r"^(CUDA|ROCm|Metal|Vulkan|SYCL)\d+\s*:", s, re.IGNORECASE):
            devices.append(s)
    init_failed = "failed to initialize cuda" in raw.lower()
    ok = (out.returncode == 0) and bool(devices) and not saw_none and not init_failed
    reason = None
    if not ok:
        reason = ("CUDA init failed" if init_failed else
                  "no device enumerated" if (saw_none or not devices) else
                  f"llama.cpp exited {out.returncode}")
    return {"ok": ok, "devices": devices, "saw_none": saw_none,
            "init_failed": init_failed, "returncode": out.returncode,
            "raw": raw[:800], "reason": reason}


def require_gpu() -> dict[str, Any]:
    status = gpu_status()
    if not status["ok"]:
        raise GpuRequired(
            "Music Flamingo needs the Tesla V100 and a working CUDA backend; "
            "no CPU inference is allowed.",
            details=status)
    for path in (MODEL_FILE, MMPROJ_FILE):
        if not path.exists():
            raise FlamingoError("MODEL_MISSING", f"model file is missing: {path}",
                                {"path": str(path)})
    if llama_binary() is None:
        raise FlamingoError(
            "BACKEND_MISSING",
            f"llama.cpp multimodal binary not found: {LLAMA_BIN}",
            {"binary": LLAMA_BIN, "searched": str(_LLAMA_FALLBACK_DIR),
             "hint": "build llama.cpp with Music Flamingo (PR after b7593) and set FLAMINGO_LLAMA_BIN"})
    cuda = cuda_devices()
    if not cuda["ok"]:
        raise GpuRequired(
            "The llama.cpp CUDA backend enumerates no device, so no layer could be "
            "offloaded to the V100; refusing to run inference.",
            details={"cuda": cuda, "gpu": status})
    status["cuda_devices"] = cuda["devices"]
    return status


# ------------------------------------------------------------------ audio views

@dataclass
class AudioView:
    label: str
    samples: np.ndarray            # mono float32
    gain_db: float = 0.0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "gain_db": round(self.gain_db, 3), "note": self.note}


def _mono16k(data: np.ndarray, sr: int) -> np.ndarray:
    """Average to mono and resample to 16 kHz (polyphase, no gain normalisation)."""
    mono = data.mean(axis=1) if data.ndim > 1 else data
    if sr == SR:
        return mono.astype(np.float32)
    from math import gcd
    up, down = SR, sr
    g = gcd(up, down)
    up, down = up // g, down // g
    from scipy.signal import resample_poly
    return resample_poly(mono, up, down).astype(np.float32)


def stereo_views(data: np.ndarray, sr: int) -> list[AudioView]:
    """Build the labelled mono representations used by the model."""
    if data.ndim == 1:
        data = np.stack([data, data], axis=1)
    left, right = data[:, 0], data[:, 1]
    mid = (left + right) / 2.0
    side = (left - right) / 2.0
    return [
        AudioView("left", _mono16k(left, sr), note="channel L only, no gain applied"),
        AudioView("right", _mono16k(right, sr), note="channel R only, no gain applied"),
        AudioView("mid", _mono16k(mid, sr), note="(L+R)/2, centre content"),
        AudioView("side", _mono16k(side, sr), note="(L-R)/2, stereo difference; silent when the mix is mono"),
        AudioView("mono_sum", _mono16k(data, sr), note="L+R averaged to mono (same as mid), what the model actually hears"),
    ]


def stereo_facts(data: np.ndarray, sr: int) -> dict[str, Any]:
    """Measured stereo facts (not model impressions)."""
    if data.ndim == 1 or data.shape[1] < 2:
        return {"channels": 1, "note": "mono source"}
    left, right = data[:, 0], data[:, 1]
    denom = (np.linalg.norm(left) * np.linalg.norm(right)) + 1e-12
    corr = float(np.dot(left, right) / denom)
    mid = (left + right) / 2.0
    side = (left - right) / 2.0
    sm = 20 * np.log10((np.sqrt(np.mean(side ** 2)) + 1e-12) /
                       (np.sqrt(np.mean(mid ** 2)) + 1e-12))
    bands = [(20, 120), (120, 500), (500, 2000), (2000, 8000), (8000, 16000)]
    per_band = {}
    for lo, hi in bands:
        seg_l = _band(left, sr, lo, hi)
        seg_r = _band(right, sr, lo, hi)
        d = (np.linalg.norm(seg_l) * np.linalg.norm(seg_r)) + 1e-12
        per_band[f"{lo}-{hi}"] = round(float(np.dot(seg_l, seg_r) / d), 3)
    return {"channels": int(data.shape[1]), "correlation": round(corr, 3),
            "side_mid_db": round(float(sm), 2), "band_correlation": per_band}


def _band(x: np.ndarray, sr: int, lo: float, hi: float) -> np.ndarray:
    n = len(x)
    if n == 0:
        return x
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    mask = (freqs >= lo) & (freqs < hi)
    spec[~mask] = 0
    return np.fft.irfft(spec, n)


def load_audio(path: str | Path, start_s: float = 0.0, end_s: float | None = None):
    import soundfile as sf
    info = sf.info(str(path))
    start = int(max(0.0, start_s) * info.samplerate)
    frames = -1 if end_s is None else int(max(0.0, end_s - start_s) * info.samplerate)
    data, sr = sf.read(str(path), start=start, frames=frames, dtype="float32", always_2d=True)
    return data, sr


def quick_dsp(path: str | Path) -> dict[str, Any]:
    """Cheap measured facts about a rendered file (kept separate from model text)."""
    import soundfile as sf
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if data.size == 0:
        return {"frames": 0}
    peak = 20 * np.log10(np.max(np.abs(data)) + 1e-12)
    rms = 20 * np.log10(np.sqrt(np.mean(data ** 2)) + 1e-12)
    corr = float(np.corrcoef(data[:, 0], data[:, 1])[0, 1]) if data.shape[1] > 1 else None
    return {"peak_dbfs": round(float(peak), 2), "rms_dbfs": round(float(rms), 2),
            "crest_db": round(float(peak - rms), 2),
            "correlation": None if corr is None else round(corr, 3),
            "frames": int(len(data)), "sample_rate": int(sr)}


# ------------------------------------------------------------------- inference

def _write_wav(path: Path, samples: np.ndarray, sr: int = SR) -> None:
    import soundfile as sf
    sf.write(str(path), samples, sr, subtype="PCM_16")


def _run_llama(wav: Path, prompt: str, *, temp: float = 0.2,
               max_tokens: int = 512) -> str:
    binary = llama_binary()
    if binary is None:
        raise FlamingoError("BACKEND_MISSING", "llama-mtmd-cli is not available")
    cmd = [binary, "-m", str(MODEL_FILE), "--mmproj", str(MMPROJ_FILE),
           "-ngl", "99", "--no-warmup", "-p", prompt, "--audio", str(wav),
           "--temp", str(temp), "-n", str(max_tokens)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    log = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0:
        raise FlamingoError("INFERENCE_FAILED", log.strip()[-800:], {"cmd": cmd})
    # Fail closed unless llama.cpp confirms it offloaded layers to the GPU.
    offload = re.search(r"offloaded\s+(\d+)\s*/\s*(\d+)\s+layers\s+to\s+GPU", log, re.IGNORECASE)
    if not offload or int(offload.group(1)) == 0:
        raise GpuRequired(
            "llama.cpp did not report any layer offloaded to the GPU; refusing to "
            "accept a CPU-only run.",
            details={"offload_match": offload.group(0) if offload else None,
                     "log_tail": log.strip()[-600:]})
    return (proc.stdout or "").strip()


def _windows(start_s: float, end_s: float) -> list[tuple[float, float]]:
    out, pos = [], start_s
    while pos < end_s - 1e-3:
        out.append((pos, min(end_s, pos + WINDOW_S)))
        pos += WINDOW_S
    return out


SERVER_BIN = Path(LLAMA_BIN).with_name("llama-server.exe")
SERVER_HOST = "127.0.0.1"


def llama_server_binary() -> Path | None:
    for candidate in (SERVER_BIN, _LLAMA_FALLBACK_DIR / "llama-server.exe"):
        if candidate.exists():
            return candidate
    found = shutil.which("llama-server") or shutil.which("llama-server.exe")
    return Path(found) if found else None


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind((SERVER_HOST, 0))
        return int(s.getsockname()[1])


class _ServerRunner:
    """One resident llama-server process: model loaded once, one request per window.

    Spawning llama-mtmd-cli per window reloads ~7 GB of weights every time; a resident
    server removes that cost while keeping every representation. Fails closed: the
    server is only accepted when its log proves CUDA offload, and it is killed on exit.
    """

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.log_path: Path | None = None

    def _log(self) -> str:
        if self.log_path and self.log_path.exists():
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        return ""

    def __enter__(self) -> "_ServerRunner":
        binary = llama_server_binary()
        if binary is None:
            raise FlamingoError("BACKEND_MISSING", "llama-server binary not found",
                                {"searched": str(SERVER_BIN)})
        self.port = _free_port()
        log = tempfile.NamedTemporaryFile(prefix="flamingo-server-", suffix=".log", delete=False)
        log.close()
        self.log_path = Path(log.name)
        cmd = [str(binary), "-m", str(MODEL_FILE), "--mmproj", str(MMPROJ_FILE), "-ngl", "99",
               "-c", "8192", "--host", SERVER_HOST, "--port", str(self.port),
               "--log-file", str(self.log_path)]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.time() + 900
        while time.time() < deadline:
            if self.proc.poll() is not None:
                text = self._log()
                if "failed to initialize cuda" in text.lower() or "no cuda-capable device" in text.lower():
                    raise GpuRequired("llama-server could not initialise CUDA; refusing CPU inference",
                                      details={"log_tail": text[-800:]})
                raise FlamingoError("SERVER_EXITED", "llama-server exited before becoming ready",
                                    {"returncode": self.proc.returncode, "log_tail": text[-800:]})
            if self._healthy():
                break
            time.sleep(1.0)
        else:
            raise FlamingoError("SERVER_TIMEOUT", "llama-server did not become ready",
                                {"port": self.port, "log_tail": self._log()[-800:]})
        text = self._log()
        offload = re.search(r"offloaded\s+(\d+)\s*/\s*(\d+)\s+layers\s+to\s+GPU", text, re.IGNORECASE)
        if not offload or int(offload.group(1)) == 0:
            raise GpuRequired("llama-server reported no GPU layer offload; refusing CPU inference",
                              details={"offload_match": offload.group(0) if offload else None,
                                       "log_tail": text[-800:]})
        self._offload = offload.group(0)
        return self

    def _healthy(self) -> bool:
        import urllib.request
        try:
            with urllib.request.urlopen(
                    f"http://{SERVER_HOST}:{self.port}/health", timeout=2) as rsp:
                return rsp.status == 200
        except Exception:                       # noqa: BLE001 - not up yet
            return False

    def ask(self, wav_path: Path, question: str) -> str:
        import base64
        import json as _json
        import urllib.request
        payload = {
            "temperature": 0.2,
            "max_tokens": 512,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "input_audio",
                     "input_audio": {"data": base64.b64encode(wav_path.read_bytes()).decode(),
                                     "format": "wav"}},
                ],
            }],
        }
        req = urllib.request.Request(
            f"http://{SERVER_HOST}:{self.port}/v1/chat/completions",
            data=_json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as rsp:
            body = _json.loads(rsp.read().decode("utf-8", errors="replace"))
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise FlamingoError("BAD_SERVER_RESPONSE", "llama-server returned no text",
                                {"error": str(exc), "body": body}) from exc
        return content.strip() if isinstance(content, str) else str(content)

    def __exit__(self, *exc_info) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=30)
        if self.log_path and self.log_path.exists():
            try:
                self.log_path.unlink()
            except OSError:
                pass


def _answers(jobs: list[tuple[str, float, float, Path]], question: str):
    """Produce the model answer for every (representation, window) job."""
    if os.environ.get("FLAMINGO_BACKEND", "cli").strip().lower() == "server":
        with _ServerRunner() as server:
            for label, w0, w1, wav in jobs:
                yield label, w0, w1, server.ask(wav, _labelled(question, label, w0, w1))
        return
    for label, w0, w1, wav in jobs:
        yield label, w0, w1, _run_llama(wav, _labelled(question, label, w0, w1))


def _labelled(question: str, label: str, w0: float, w1: float) -> str:
    return f"[representation: {label}; window {w0:.1f}-{w1:.1f}s] {question}"


def describe(audio_path: str | Path, *, prompt: str | None = None,
             start_s: float = 0.0, end_s: float | None = None,
             representations: list[str] | None = None,
             reference_path: str | Path | None = None) -> dict[str, Any]:
    """Run Music Flamingo over the audio, per representation and per 30 s window."""
    gpu = require_gpu()
    data, sr = load_audio(audio_path, start_s, end_s)
    duration = len(data) / sr
    if end_s is None:
        end_s = start_s + duration
    if duration > MAX_TOTAL_S + 1e-6:
        raise FlamingoError(
            "TOO_LONG",
            f"{duration:.1f} s exceeds the {MAX_TOTAL_S:.0f} s ceiling; "
            "request a bounded range instead of silently truncating",
            {"duration_s": duration, "max_s": MAX_TOTAL_S})
    views = stereo_views(data, sr)
    if representations is None:
        # One view per window keeps the process count (and the model reload cost)
        # at its minimum; every other view is one explicit argument away.
        representations = ["mono_sum"]
    views = [v for v in views if v.label in representations]
    if not views:
        raise FlamingoError("UNKNOWN_REPRESENTATION", "no such audio representation",
                            {"requested": representations,
                             "available": [v.label for v in stereo_views(data, sr)]})

    question = prompt or (
        "Describe what you hear in this audio: the instruments and voices present, how they "
        "balance against each other, tonal character, dynamic movement, space, and anything "
        "that sounds wrong or unclear. Be specific and brief.")

    results: list[dict[str, Any]] = []
    jobs: list[tuple[str, float, float, Path]] = []
    with tempfile.TemporaryDirectory(prefix="flamingo-") as tmp:
        for view in views:
            for k, (w0, w1) in enumerate(_windows(start_s, end_s)):
                a = int((w0 - start_s) * SR)
                b = int((w1 - start_s) * SR)
                chunk = view.samples[a:b]
                if chunk.size == 0:
                    continue
                wav = Path(tmp) / f"{view.label}_{k:03d}.wav"
                _write_wav(wav, chunk)
                jobs.append((view.label, w0, w1, wav))
        for label, w0, w1, text in _answers(jobs, question):
            results.append({"representation": label, "window": [round(w0, 2), round(w1, 2)],
                            "text": text})

    out: dict[str, Any] = {
        "engine": "music-flamingo-Q5_1",
        "gpu": gpu,
        "audio": {"path": str(audio_path), "start_s": start_s, "end_s": end_s,
                  "duration_s": round(duration, 3), "sample_rate": sr},
        "representations": [v.to_dict() for v in views],
        "stereo_facts": stereo_facts(data, sr),
        "windows": [[round(a, 2), round(b, 2)] for a, b in _windows(start_s, end_s)],
        "results": results,
        "note": ("The model hears one mono 16 kHz signal per run; stereo facts are "
                 "measured separately from the original stereo."),
    }
    if reference_path:
        ref = describe(reference_path, prompt=question, representations=representations)
        out["reference"] = {"audio": ref["audio"], "representations": ref["representations"],
                            "stereo_facts": ref["stereo_facts"], "windows": ref["windows"],
                            "results": ref["results"]}
        out["comparison"] = _compare_records(out, out["reference"])
    return out


def _facts(x: dict[str, Any]) -> dict[str, Any]:
    f = x.get("stereo_facts") or {}
    return {"correlation": f.get("correlation"), "side_mid_db": f.get("side_mid_db")}


def _compare_records(target: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    """Pair the two runs window by window so the caller can compare like with like."""
    def index(rec):
        return {(r["representation"], tuple(r["window"])): r["text"] for r in rec.get("results", [])}

    a, b = index(target), index(reference)
    rows = []
    for key in sorted(set(a) | set(b)):
        rows.append({"representation": key[0], "window": list(key[1]),
                     "target_text": a.get(key), "reference_text": b.get(key)})
    return {"rows": rows,
            "target_facts": _facts(target), "reference_facts": _facts(reference),
            "note": ("The two recordings were analysed in separate runs with the same "
                     "representations and window grid; compare measured facts first.")}
