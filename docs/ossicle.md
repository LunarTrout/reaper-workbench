# Ossicle-4B backend (stereo / audio understanding)

The library's listening model. It answers questions about the real left and right
channels of a clip — which sources sit where, what masks what, how the low end is
balanced, what moves — and every answer is returned next to the measured stereo facts
of the same clip, so a claim can be checked instead of believed.

It replaced **MiDashengLM-Spatial** (see the last section for why).

## What it is

| | |
|---|---|
| Checkpoint | `SILVERTHRONE/Ossicle-4B`, revision `d4def766b7465b31ee93853acd38964f699d652e` |
| Licence | Apache-2.0 (base models keep their own) |
| Size | 4.9 B params — 0.65 B audio encoder (Qwen3-Omni AuT) + 4.2 B Qwen3.5-4B; 9.75 GB bf16, 3.6 GB with its own int4 |
| Audio in | mono or stereo, any sample rate, any length; stereo goes through a `StereoAdapter` fed with 72 interaural level/phase cues per token |
| Token rate | 12.5 audio tokens per second |
| Local path | `<library>/work/models/ossicle-4b` |
| Interpreter | `OSSICLE_PYTHON` (defaults to the active library interpreter; set it to a separate CUDA environment only when the model dependencies require one) |

## Files

| File | Role |
|---|---|
| `scripts/fetch_model.py` | parallel range download (the CDN throttles one connection to ~0.5 MB/s), manifest with size + sha256, resume, re-fetch on a checksum mismatch. Defaults to this checkpoint; `--repo/--revision/--dest` fetch any other |
| `src/reaper_audio/analysis/ossicle.py` | backend: weight verification, GPU gate, stereo loading, measured facts, worker call, `compare()` |
| `scripts/ossicle_worker.py` | one model load per call, N questions, hard GPU assertions, fp16 on V100 |
| `scripts/spatial_acceptance.py` | known-scene harness: hard left / right / centre / three sources, each with swapped L-R |
| `scripts/spatial_calibration.py` | known-answer variants per axis (brightness, width, level, distance) to measure which judgements are trustworthy |
| `tests/test_ossicle_stereo.py` | proves the stereo path keeps channels, mirrors on swap, refuses mono, caps at 120 s |

## Environment

The model folder is loaded with `trust_remote_code`; `modeling_ossicle.py`,
`configuration_ossicle.py`, the chat template and the tokenizer live in the checkpoint
itself, so a shards-only download is not loadable — `fetch_ossicle.py` takes every root
file and pins all of them.

Two hard requirements the probe checks before any render or write:

* `transformers` must know `qwen3_5` (the text half of the checkpoint). 4.57.6 does not;
  5.x does, and 5.x refuses to run on torch 2.5 (its `is_torch_available()` gates on
  `torch >= 2.6`, and the import then dies with `NameError: name 'nn' is not defined`),
  so the interpreter carries `torch 2.6.0+cu124`.
* The card must be the Tesla V100. `torch.cuda.is_bf16_supported()` returns **True** on a
  V100 because of the emulation branch, so it is not a usable signal here: the worker
  selects fp16 whenever the device capability is below 8.0, and the checkpoint's
  `_keep_in_fp32_modules_strict = ["head", "stereo"]` keeps the projection head and the
  stereo adapter in fp32.

Nothing runs on the CPU. The worker refuses when `torch.cuda.is_available()` is false,
when the device name is not a V100, when any parameter is still on the CPU, or when the
allocated VRAM is below 2.5 GB.

## Contract

* **Two real channels, never summed.** `load_binaural()` returns `(2, T)` float32 at
  16 kHz, channels first. A mono file is refused with `NOT_STEREO` unless the caller sets
  `allow_mono`, and then it carries no direction anyway.
* **120 s ceiling.** Up to 120 s the stereo adapter is used; beyond that the model
  streams the clip in 64-second pieces and, by its own card, stops using stereo cues.
  The backend refuses longer clips with `TOO_LONG` instead of silently dropping the
  direction information the analysis was asked for.
* **Comparisons, not adjectives.** The default battery asks for sources ranked, sides,
  masking pairs, low-end order, movement, width and a direction for each fix — and every
  answer ships with `stereo_facts` (correlation, per-channel RMS, balance, first mid/side
  samples) measured from the same clip.
* **A/B in one request.** `compare(a, b, question)` sends both clips in one call; the
  model labels them `Audio 1:` / `Audio 2:` in its own prompt format, so the answer is a
  pairwise judgement instead of an absolute one.

## MCP surface

`reaper_spatial_status()` reports files, sha256, interpreter, torch/CUDA, the V100 and
whether `qwen3_5` is importable. `reaper_spatial_analyze(target="master"|"track:<name>"
|"file:<path>", start_s, end_s, question, quantize="none"|"int4")` renders the target as
stereo, optionally prefixes the arrangement text from `reaper_timeline_context`, runs the
model and attaches the measured facts plus the project's revision. `reaper_spatial_timeline`
stays model-free DSP.

## Why it replaced MiDashengLM-Spatial

* No quantised spatial checkpoint exists anywhere (HF, ModelScope, OpenCSG, Ollama,
  GitHub releases): the only weights are 33.53 GB of fp32 shards for a 16 GB card, and
  GPTQ/AWQ kernels target SM75+, which a V100 is not. Conversion to NF4 needed the whole
  33.5 GB source online first.
* Ossicle-4B ships 4-bit weights, a stereo adapter and a documented GPU-only path, and its
  own card reports 46.5 on MMAU-Pro's spatial category (33–34 for the quantised vLLM
  builds with the adapter, 24.0 without it).
* The old backend's 60-second cap and ~3-source training domain are kept as caveats, not
  as an excuse: the model's statements are hints to be checked against the measured
  numbers, never ground truth about a dense black-metal mix.

## Proven

*Weights*: 20 root files, 9.75 GB, every file verified by size and (where the Hub publishes
one) sha256; `incomplete: []`. Fetched with 12 parallel range connections — the CDN throttles
a single connection to ~0.5 MB/s, twelve gave 7–11 MB/s.

*Load and answers* (`work/ossicle_proof.json`, Tesla V100-SXM2-16GB): fp16, weights 9.83 GB,
peak VRAM 9 593 MB of 16 249, **load 11.2 s**, three questions in **1.8 / 5.6 / 7.5 s**.
No CPU, no offload; the worker asserts it before answering.

*Known-answer checks*:

| Check | Ground truth | Model | Verdict |
|---|---|---|---|
| hard-left vs hard-right tone | left / right | "Audio 1 sits on the left, Audio 2 on the right" | **correct** — the stereo adapter works |
| brightness_0 vs brightness_2 (+6 dB high shelf, loudness matched) | Audio 2 is +5.2 dB brighter in HF | "Audio 1 has more energy in the high frequencies" | **wrong** — and it invented a "high-pitched guitar riff" for a synthetic variant |

*Real material* (`work/mixcheck_master_1_05_1_30.wav`, 25 s of the project master): named drums
as dominant, guitars and bass spread left/right, kick+bass low end, bass masking; called the
drums the widest element, which contradicts a four-hard-panned-guitar arrangement, and reported
"high confidence" on all of it.

Conclusion: use it for orientation and element identification on real material, and never as
the only judge of a tonal change — its brightness/level comparisons can invert, and its
confidence does not track its accuracy. `scripts/spatial_calibration.py` measures each axis
before anything is trusted.

### Pairwise questions are position bias, not judgement

`scripts/spatial_calibration.py --run-model` builds three variants per axis (brightness by
high shelf, width by side gain, level by dB, distance by wet ratio), presents all three pairs
with the order shuffled, and scores the answer against the variant that carries more of the
axis. Result on a 12 s excerpt of the project master (fp16, shuffled order):

| Axis | Accuracy | What it actually said |
|---|---|---|
| brightness | 2/3 — but "Audio 1" in 3/3 pairs | constant |
| width | 0/3 | "Audio 1" in 3/3 |
| level | 1/3 | "Audio 1" in 3/3 |
| distance | 1/3 | "Audio 2" in 3/3 |

Decisive check: the **same file** as Audio 1 and as Audio 2. Answer: "Audio 1 is louder
overall … a heavier, more distorted bass guitar" and "Audio 1 has more energy in the high
frequencies" — a confident comparison of two identical signals, with invented reasoning.

Rules that follow:

### The model reads the numbers and inverts their direction

`scripts/context_brief.py --ask` hands the model the measured table (our values, the references,
the delta) together with the audio and asks for an analysis. Result on the 195–225 s section:
the brief said `lufs_i −4.89` (ours quieter) and `centroid_hz −96.5` (ours darker); the answer
was "the track is off by **+4.89 dB** in loudness and **−96.5 Hz** in centroid … the loudness
and centroid are higher … the volume should be reduced". The numbers are taken from the table,
the direction is flipped, and the recommendation follows the flipped sign.

So the split is fixed: measurements decide, the model only listens. Feed it audio and ask what
it hears; never ask it to reason over a numeric table, and never let its "analysis" sentence
carry a delta.

- **Never ask "which of the two is more X".** The answer is decided by slot order, not audio.
* Compare two clips by asking about each one separately, then compare the *measured facts*
  (`stereo_facts`) and your own numbers — not the model's words.
* Descriptive questions about one clip (what is there, who masks whom, where things sit,
  what would you change) are not affected by this bias.
* Any claim in a report must be paired with the measurement it is supposed to explain.


