# Music Flamingo provider

Local audio-understanding model for the REAPER MCP. It lets the agent ask musical
questions about the master, a time range, or an external reference file, and get an
answer grounded in the same project revision that was rendered.

## Weights are not vendored

Music Flamingo weights are **not** part of this repository and must never be committed.
They are licensed under **NVIDIA OneWay Noncommercial Research**; review that licence
before any use beyond research.

| file | size (bytes) | sha256 |
|---|---|---|
| `music-flamingo-Q5_1.gguf` | 5 754 852 544 | `0742a216d86e28c5ecde1bf3bce6261d509edd540e2512a056684ddcc31051a6` |
| `mmproj-music-flamingo-bf16.gguf` | 1 324 506 912 | `d4be69ed65f25dae97062febd44f9f41c0f6b14178f1cfb530fd894d595a4f94` |

Pinned revision: `a059053433697011c6928b1962110040f4bcb4d0` of `henry1477/music-flamingo-gguf`.

Fetch (resumes partial files, then verifies both hashes):

```bash
scripts/fetch_flamingo.sh /path/to/model-store/music-flamingo-q5_1
```

Environment:

* `FLAMINGO_MODEL_DIR` — directory with the two files (default: the path above in this repo's docs).
* `FLAMINGO_LLAMA_BIN` — `llama-mtmd-cli` with Music Flamingo support (build after llama.cpp PR b7593, CUDA enabled).

## GPU policy

Inference is **GPU-only**. Every entry point calls `require_gpu()`, which:

1. runs `nvidia-smi` and requires a device whose name contains `V100`,
2. verifies both model files exist,
3. verifies the llama.cpp binary is on PATH.

Any failure raises `GPU_REQUIRED` (missing/incorrect GPU) or `MODEL_MISSING` /
`BACKEND_MISSING`, and **no inference runs**. There is no CPU fallback and no partial
offload: `-ngl 99` is always passed.

## Stereo is carried explicitly

The model consumes one mono 16 kHz signal (Whisper feature extractor), so it cannot
hear a panorama. Each analysis therefore returns:

* `representations` — separate runs for `left`, `right`, `mid`, `side`, `mono_sum`,
  each labelled with the transform used and the gain applied (none),
* `stereo_facts` — measured on the original stereo: L/R correlation, side/mid in dB,
  and per-band correlation (20–120, 120–500, 500–2k, 2k–8k, 8k–16k Hz).

Model text and measured facts are returned in separate fields; never treat model
wording as a measurement.

## Long material

Windows are 30 s. A request longer than 600 s fails with `TOO_LONG` instead of being
silently truncated. Pass `start_s`/`end_s` for a bounded range.

## MCP tools

* `reaper_flamingo_status` — GPU, model files, binary, window limits.
* `reaper_flamingo_describe(target, start_s, end_s, representations, reference_path, prompt)`
  — `target="master"` renders the master through its FX to a temporary file outside the
  project, analyses it, deletes the render and returns `project.revision`, tempo map,
  representations, facts and the model output. `target="file:<path>"` analyses any WAV
  (for example a reference).
* `reaper_flamingo_ask(question, ...)` — same pipeline with a free-form question.

References are analysed in a **separate** run; the comparison is between structured
results (DSP facts first), because the engine cannot take two audios in one request.

## Read-only guarantee

Analysis never writes to the project: the master is rendered to a temporary directory
outside the project media folder, and the directory is removed in a `finally` block.
Temporary files never appear on the timeline.

## Verification checklist

1. `reaper_flamingo_status` → `ok: true`, device contains `V100`, both files `exists: true`.
2. `reaper_flamingo_ask("что звучит и что мешает?", start_s=0, end_s=30)` returns text
   per representation and `stereo_facts`.
3. Same call with `target="file:<reference>"` returns a separate report.
4. A 700 s request returns `TOO_LONG`, not a truncated answer.
5. With `nvidia-smi` blocked, every call returns `GPU_REQUIRED` and nothing runs.
6. After a run, the temporary directory is gone and the project revision is unchanged.
