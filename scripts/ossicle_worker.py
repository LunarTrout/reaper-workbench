"""Ossicle-4B worker: one model load, N stereo questions, GPU only.

Reads a JSON request on stdin:
  {"model_dir": ..., "device": 0, "dtype": "float16", "quantize": "none"|"int4",
   "revision": ..., "jobs": [{"audios": [wav, ...], "question": str,
                              "max_new_tokens": int}, ...]}
Writes one JSON line on stdout. Any sign of CPU execution is a hard failure.

Why fp16 by default: the checkpoint ships bf16, but the only GPU here is a Tesla
V100 (SM 7.0), which has no bf16 matrix-multiply path - bf16 weights either fall
back to an emulation path or fail. fp16 is native there, and the module's own
`_keep_in_fp32_modules_strict` keeps the projection head and the stereo adapter in
fp32, so only the encoder and the language model run in half precision.
"""

from __future__ import annotations

import json
import sys
import time
import traceback


def fail(code: str, message: str, details: dict | None = None) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "details": details or {}}}


def main() -> int:
    request = json.loads(sys.stdin.read())
    device_index = int(request.get("device", 0))

    import torch

    if not torch.cuda.is_available():
        print(json.dumps(fail("GPU_REQUIRED", "torch.cuda.is_available() is False")))
        return 0
    name = torch.cuda.get_device_name(device_index)
    if "V100" not in name.upper():
        print(json.dumps(fail("GPU_REQUIRED", f"device {device_index} is {name!r}, not a Tesla V100")))
        return 0

    dtype_name = request.get("dtype") or ("bfloat16" if torch.cuda.is_bf16_supported() else "float16")
    dtype = {"float16": torch.float16, "bfloat16": torch.bfloat16,
             "float32": torch.float32}[dtype_name]
    quantize = (request.get("quantize") or "none").lower()

    from transformers import AutoModel

    t0 = time.time()
    load_kwargs = dict(trust_remote_code=True, low_cpu_mem_usage=True)
    try:
        model = AutoModel.from_pretrained(request["model_dir"], dtype=dtype, **load_kwargs)
    except TypeError:                          # older signature name
        model = AutoModel.from_pretrained(request["model_dir"], torch_dtype=dtype, **load_kwargs)
    model = model.to(f"cuda:{device_index}")
    if quantize == "int4":
        model.to_4bit()                        # group-wise 4-bit, in place on the current device
    model.eval()
    load_seconds = round(time.time() - t0, 2)

    # No CPU, no disk: every parameter must sit on the requested GPU.
    on_cpu = [n for n, p in model.named_parameters() if not p.is_cuda]
    if on_cpu:
        print(json.dumps(fail("GPU_REQUIRED", "some parameters stayed on CPU; refusing",
                              {"count": len(on_cpu), "examples": on_cpu[:5]})))
        return 0
    allocated_gb = torch.cuda.memory_allocated(device_index) / 1e9
    if allocated_gb < 2.5:
        print(json.dumps(fail("GPU_REQUIRED", "too little VRAM allocated for the weights; "
                              "the model is probably not resident on the GPU",
                              {"allocated_gb": round(allocated_gb, 2)})))
        return 0

    torch.cuda.reset_peak_memory_stats(device_index)
    answers = []
    for job in request["jobs"]:
        audios = job.get("audios") or [job["wav"]]
        t1 = time.time()
        with torch.no_grad():
            text = model.ask(audios if len(audios) > 1 else audios[0],
                             job["question"], max_new_tokens=int(job.get("max_new_tokens", 220)))
        answers.append({"audios": audios, "question": job["question"],
                        "text": str(text).strip(), "seconds": round(time.time() - t1, 2)})

    peak_mb = int(torch.cuda.max_memory_allocated(device_index) / 1048576)
    print(json.dumps({"ok": True, "device": name, "dtype": dtype_name,
                      "quantize": quantize, "load_seconds": load_seconds,
                      "vram_peak_mb": peak_mb,
                      "vram_weights_gb": round(allocated_gb, 2), "answers": answers}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:                            # noqa: BLE001
        print(json.dumps(fail("INFERENCE_FAILED", "worker crashed",
                              {"traceback": traceback.format_exc()[-1800:]})))
        raise SystemExit(0)
