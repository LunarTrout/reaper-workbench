# Performance pipeline report B — end-to-end throughput, DSP, scheduling and packaging

**Date:** 2026-11-13 · **Scope:** Workstream B (analysis/packaging/whole-pipeline throughput) ·
**No production code, project file, `work/` artifact or capture archive was modified.**

> **Historical note:** the 8-worker recommendation in this report predates the
> memory-aware production policy. Current production default is 4 workers for
> full/unknown-duration captures and up to 8 for windows <= 30 s, with a hard
> cap of 8; 12/16 are benchmark-only. See `docs/FULL-CAPTURE.md` and
> `src/reaper_audio/daw/analysis_policy.py`.

Evidence labels used throughout: **[MEASURED]** = read from an artifact or timed on this machine during
this study; **[SOURCED]** = external document (section c); **[INFERENCE]** = derived by arithmetic or
code reading from the two above, with its basis stated. Every number that is not labelled is a
restatement of a labelled number.

Machine under test: AMD Ryzen 7 5700G, 8 cores / 16 threads, 48 GiB installed (47 GiB budget), 2×1 TB
SATA SSD + 1 TB HDD, one Tesla V100 16 GB, Windows 11, Python 3.14.9 (target 3.13), numpy 2.5.4,
scipy 1.18.1, soundfile 0.14.0 **[MEASURED]**. A capture-parity audit was running in another Hermes
task for part of this study, so all wall-clock probes below are reported as ranges and were observed
under contention (section h).

---

## (a) Question and scope

This report answers one question: **given the current full-capture implementation, where does the
703-second iteration actually go, which parts of that time can be removed or overlapped without
changing a single measured value, and what does the resulting iteration cost?**

It covers:

1. A stage model of `FullCaptureService.capture` built from the sealed record's own timers plus a
   reconciliation of the 149.9 s that those timers do not explain.
2. The analysis worker pool: per-asset DSP cost, per-worker memory, parallel efficiency, scheduling
   tail, spawn cost, result-transfer cost.
3. Packaging: record sealing, JSON serialization, archive copy/hashing, panorama, validation, and
   what the 445 MB record costs per iteration and per read.
4. Bounded overlap of the existing pool with render, master validation, project-context capture and
   archive work, including the memory ceiling.
5. Exact (non-approximating) DSP optimizations, ranked, with quantified stage shares.
6. GPU feasibility on the V100, with transfer/precision arithmetic and an explicit rejection of
   unmeasured "GPU is faster" claims.
7. A falsifiable benchmark and parity protocol.

It does **not** cover: REAPER's render-completion semantics, `Main_OnCommandEx(42230)` blocking
behaviour, `RENDER_TARGETS`/`RENDER_STATS`, or whether a target file can be proven closed inside a
render batch. Those are Workstream A. Where B's design depends on them, the dependency is listed in
"Cross-workstream questions for A" with a working assumption.

Non-goals, per the brief: caching, dropping metrics, sample-rate reduction, lower-resolution
analysis, track subsets, or changing the stage/alignment/window of any measurement. No approximation
appears in any recommended default.

Method: static reading of the worktree; independent parsing of the sealed record
`cap_01M7A7ZV8VWK4SA8FHGRC9Q04S` (445,001,421 bytes, never `json.load`-ed whole; targeted
byte-span extraction only); field-level comparison against `cap_01M79J9HAR188THDEQ5WK8GTTD`; and
scratch-copy probes of the packaging primitives with the repository's own code imported. No REAPER
process was started, no MCP tool was invoked, no `work/` path was written.

---

## (b) Current code observations

### b.1 The real serial order

`capture()` (`src/reaper_audio/daw/full_capture.py:298`) executes the following, in this order, with
no overlap between any two steps:

| # | Stage | Code | Measured (s) | Notes |
|---|---|---|---|---|
| 1 | Before-snapshot (items, FX params, MIDI) | `:335` → `session.snapshot` | 0.073 | `snapshot_s` |
| 2 | Project digest + coverage gate | `:355` | 0.073 | `digest_s` |
| 3 | **Track batch render** (42 targets, one bridge RPC, 600 s timeout) | `_render_batch` `:714` | **98.470** | `render_batch_s`; Python blocks in `ReaperBridge.call` polling a mailbox at 20 ms (`bridge_client.py:287`) |
| 4 | Per-file manifest registration (**full SHA-256 per WAV**) | `:420` `workspace.register_asset` | ~2.7 (inside row 7) | 3.42 GB at 1.25 GB/s **[MEASURED rate]**; `artifacts.py:126` → `_stream_sha256` |
| 5 | **Source-activity gate** (full float64 decode of all 42 WAVs in the parent) | `_track_activity` `:66`, called `:427` | **17.767** | `source_activity_s`; 0.42 s/asset |
| 6 | Master render | `_render_master_only` `:744` | **9.505** | `render_master_s` |
| 7 | Registration hashes + master gate: SHA-256 + **second full decode** + peak/RMS classification | `:420`, `:793-797` | **8.96** (derived: ≈2.7 registration + ≈6.2 gate) | not timed separately; derived as `capture_render_s − (rows 1+2+3+5+6)` |
| 8 | After-snapshot + after-digest + restore report | `:461-472` | 0.15 | `restore_verification_s`; digest equality is recorded, not enforced |
| 9 | **Analysis pool** (8 spawn workers, 43 tasks) | `_analyze_rendered_files` `:256`, called from `_build_record` `:1026` | **408.274** | `analysis_pool_wall_s` |
| 10 | Per-asset record loop: 2× `_sha256_file` per asset, dict assembly | `:1031-1065` | **9.903** | `analysis_wall_s − analysis_pool_wall_s` |
| 11 | **Record sealing**: `_freeze` of the whole payload + `canonical_payload` dump/encode/hash | `FullCaptureRecord.__post_init__` (`context/full_capture.py:194-396`), constructed at `:1095` | **40–84** (derived) | *no timer exists*; probe below |
| 12 | `CaptureArchive.append`: 43 × (source SHA + copy + temp SHA), 2 × 445 MB dumps, 445 MB write + fsync, index rewrite, panorama | `context/full_capture.py:527-696` | **30–45** (derived) | `archive_s` is only reported in the response, never in the record |
| 13 | Panorama load + `validate_binding`, master validation, response assembly | `full_capture.py:523-587` | ~2 | sidecar 395,400 B |
| 14 | `finally`: **42 sequential no-op mute RPCs** + workspace close (delete 43 WAVs) | `:591`, `_set_every_mute` `:654` | 3–21 (derived, in residual) | `mute_bridge_calls=0` in the record: nothing was muted, so these restore calls cannot change anything |
| 15 | MCP wrapper `_jsonable` + `mcp_serialization_s` | `mcp_server.py:1721-1729` | 0.007 | measured on a 20 s capture |

**Critical path:** snapshot → digest → **render (98.5 s)** → activity (17.8 s) → master (9.5 s) →
**pool (408.3 s)** → record loop (9.9 s) → **seal + archive (70–130 s)** → response. Everything is
serial; the only concurrency in the process is inside the pool.

### b.2 Timers that do not mean what their names suggest

- `analysis_wall_s` (418.177) is **not** the pool: it spans `_build_record` from line 977, so it
  includes project-map assembly, the pool, the per-asset SHA/dict loop and stops *before* the record
  constructor. `analysis_pool_wall_s` (408.274) is the pool only. The 9.903 s difference is real work.
- `capture_render_s` (134.851) includes the 17.767 s activity gate, ~2.7 s of registration hashing and
  the ~6.2 s master gate, which are analysis/packaging-side work, not render.
- `dsp_worker_elapsed_sum_s` (3011.645) is a sum of per-task `measure_audio` timers
  (`analysis_workers.py:31-33`); it is **not** wall time and excludes decode. The record says so
  (`timings_schema: capture-timings/2`), but downstream readers must not average it.
- `total_s` and `archive_s` are added to the **response only** (`:582-586`). The sealed record cannot
  answer "how long did packaging take", which is why the 149.9 s tail had to be reconstructed here.
- The `finally` block runs *after* `total_s` is computed, so `total_s` excludes the 42 mute RPCs and
  the deletion of 3.42 GB of temp WAVs.

### b.3 Duplicated work (exact inventory)

| Work | Where | Count | Cost basis |
|---|---|---|---|
| Full WAV decode per asset | activity gate `:73`, DSP worker `analysis_workers.py:29`, master gate `:172` | 2 per track (activity + DSP), 2 for master (gate + DSP) | decode 480 MB/s cached **[MEASURED]**; 3.42 GB → ~7 s per pass |
| SHA-256 passes per WAV | `register_asset` `:420`; `_sha256_file` **twice** in the record loop (`:1041` and `:1052` — the first result is discarded because `item` is never updated); archive source `:575`; archive temp `:592` | **5** | 17.1 GB hashed per capture; 1.25 GB/s → ~14 s |
| Full 445 MB JSON dump | `__post_init__` canonical (1), `_seal_mapping` canonical (2), `_seal_mapping` payload (3) | **3** | 11–13 s each **[MEASURED]** |
| Deep freeze of the payload | `_freeze` on 9 fields, one of which is 444.76 MB | 1 | 38–52 s at 515 MB **[MEASURED]** |
| Pairwise correlation over the whole buffer | `metrics.py:292` (`np.corrcoef(left,right)`) and `temporal.extras` (`:781`) | 2 | ~0.2 s/asset |
| `np.abs(x) >= 0.999` scan | `metrics.py:273`, `temporal.extras:778`, `events.py:168` | 3 | ~0.1 s/asset each |
| Master mute restore RPCs | `finally` → `_set_every_mute` | 42 | 1 RPC ≈ 20–60 ms + Lua snapshot |

### b.4 Worker pool, memory and scheduling

- `_analyze_rendered_files` (`:256`) creates **one `ProcessPoolExecutor` per capture** with the
  `spawn` context, submits all 43 tasks immediately, then calls `future.result()` **in submission
  order**. No `chunksize` is used (so no chunksize straggler), no longest-first ordering, and the
  master — almost certainly the most expensive asset — is submitted **last**.
- `analysis_workers.py` sets `OMP/OPENBLAS/MKL/NUMEXPR/VECLIB` thread limits **before** importing
  numpy (line 13-18), so each worker is single-threaded inside BLAS. Correct.
- `_analysis_worker_count` (`:192`) caps at `MAX_ANALYSIS_WORKERS = 16`, i.e. 16 on this 16-thread
  host. With a measured per-worker peak of **3.66 GB** (`(29,560 MB − parent)/8` from
  `worker-scaling.json` **[MEASURED]**), 16 busy workers need **≈59 GB** — above the 47 GiB budget.
  **The current cap can OOM the host.** 12 workers ≈ 44 GB leaves ≈3 GB of headroom.
- Measured scaling on eight identical 300 s assets: 8/12/16 workers = **95.817 / 95.122 / 91.636 s**
  with peak tree RSS **29.56 / 29.19 / 29.97 GB** (`worker-scaling.json` **[MEASURED]**). 16 workers
  are 4.4 % faster than 8 while needing (at 16 busy tasks) twice the memory: the workload is
  bandwidth-bound, not core-bound.
- In the sealed capture the pool ran 43 tasks in 408.274 s with Σ DSP = 3011.645 s → mean busy
  workers **7.38 of 8 (92.2 %)** and an ideal-balance wall of 376.5 s, i.e. a **31.8 s tail (7.8 %)**.
  Of that, some is genuine load imbalance and some is the parent unpickling ~445 MB of result dicts
  in one thread **[INFERENCE: 43 × 10.3 MB results]**.
- CPU utilisation of the whole iteration: 3011.6 DSP-seconds / 703.07 s = **4.28 cores** of 16
  logical (26.8 %) **[INFERENCE]**. The machine is idle most of the time; the pipeline is serial, not
  saturated.

### b.5 Rate and format facts that the design must respect

- All 43 assets are 44.1 kHz, stereo, 24-bit PCM, 13,252,932 frames, 300.520 s, 79,518,282 bytes
  **[MEASURED from `audio[]`]**. Total WAV bytes **3,419,286,126** (3.42 GB).
- The panorama sidecar on disk is 395,400 B **[MEASURED]**; the brief quotes 395,429 B. The 29-byte
  difference was not investigated (it does not affect any conclusion here).
- Both the batch and the master used `RENDER_SRATE: 0` (project rate) **[MEASURED]**, so there is no
  track/master rate mismatch in this capture. If a caller passes `sample_rate`, both paths receive
  the same value (`:735`, `:760`); analysis cost then scales ~linearly with the new rate
  (48 kHz vs 44.1 kHz = +8.8 % samples → ~+9 % DSP) **[INFERENCE]**.
- The record is 445,001,421 B of which the `analysis` object is **444,758,064 B (99.95 %)**; the
  project map, audio table, timings and restore report together are ~243 KB. Per asset the analysis
  is 10.3 MB on average and 12.6 MB for the densest track, dominated by three arrays: per-band time
  levels 6.42 MB (51 %), temporal cells 3.67 MB (29 %), 2 ms events 2.18 MB (17 %) **[MEASURED]**.

### b.6 Why the successful-looking capture returned an error

`optimized-capture.json` records `ok=false`, `elapsed_wall_s=703.072` and a `RuntimeError("capture
did not return a valid capture_id")` raised by the harness itself (`optimized_capture_run.py:57-58`),
**not** by the service. The harness calls `reaper_capture_snapshot` in-process and tests
`result.get("ok") is true and result.get("capture_id")`. The tool is wrapped by `_guard`
(`mcp_server.py:147-154`), which converts **any** exception into `_error_payload(exc)`; that payload
has no top-level `capture_id`, so the harness reports "did not return a valid capture_id" even though
`capture()` had already committed a valid, readable, master-passed record.

Two code paths can raise *after* the archive commit and destroy the response:
`load_panorama(...)`/`validate_binding` at `:525`, and the `finally` block's 42 sequential
`SetMediaTrackInfo_Value` RPCs at `:591` (each with the default 10 s timeout and a required
`project_after` snapshot). The record's own `restore_report` shows `mute_restored=true`,
`solo_restored=true`, `digest_fields_equal=true`, `dirty_restored=true` and no mute was ever set
(`mute_bridge_calls=0`), so the restore *did* end in the correct state — meaning a late RPC failure
in the same block is the most plausible response-killer **[INFERENCE; the response body was not
preserved, so the exact exception is unproven]**.

**Success contract that a tool response must satisfy** (an archived record alone is not success):
`ok is true`, a valid `capture_id`, `raw_capture.record_path` readable through `reaper_capture_read`
with `integrity_sha256` verifying and the panorama binding valid, `master_validation.valid is true`
(or `expected_silence` with a reason), `restore_report` showing mute/solo/selection/transport/dirty
restored, and `timings.total_s` present. The wrapper should also be made failure-atomic: a
post-commit exception should return `ok=true` plus a `warnings[]` entry, not an error payload that
hides a committed capture.

---

## (c) External sources

All accessed **2026-11-13**. Quoted sentences are verbatim from the linked document.

| # | Claim it supports | Source | Label | Quote / note |
|---|---|---|---|---|
| S1 | `spawn` starts a fresh interpreter and is the slow start method; it is the Windows default | [Python docs, `multiprocessing` — contexts and start methods](https://docs.python.org/3/library/multiprocessing.html) | [OFFICIAL] | "The parent process starts a fresh Python interpreter process. The child process will only inherit those resources necessary to run the process object's run() method. … Starting a process using this method is rather slow compared to using fork or forkserver." and "The default on Windows and macOS." |
| S2 | Spawned tasks/results must be picklable; the pool is process-based | [Python docs, `concurrent.futures.ProcessPoolExecutor`](https://docs.python.org/3/library/concurrent.futures.html) | [OFFICIAL] | "only picklable objects can be executed and returned." |
| S3 | `chunksize` defaults to 1 and matters for long iterables; `max_tasks_per_child` controls worker lifetime | same as S2 | [OFFICIAL] | "For very long iterables, using a large value for chunksize can significantly improve performance compared to the default size of 1." / "max_tasks_per_child … the maximum number of tasks a single process can execute before it will exit and be replaced with a fresh worker process." |
| S4 | `json.dump` writes a stream; `json.dumps` returns one string | [Python docs, `json`](https://docs.python.org/3/library/json.html) | [OFFICIAL] | "Serialize obj as a JSON formatted stream to fp (a .write()-supporting file-like object)" (dump) vs "Serialize obj to a JSON formatted str" (dumps). |
| S5 | Gating algorithm, block size and thresholds that `_fast_loudness_profile` must preserve | [ITU-R BS.1770-4 (10/2015)](https://www.itu.int/dms_pubrec/itu-r/rec/bs/R-REC-BS.1770-4-201510-S!!PDF-E.pdf) | [OFFICIAL] | "gating of 400 ms blocks (overlapping by 75%), where two thresholds are used: – the first at −70 LKFS; – the second at −10 dB relative to the level measured after application of the first threshold." |
| S6 | 3 s window, ≥10 Hz sampling and 10th/95th percentile for LRA | [EBU Tech 3342 (2023)](https://tech.ebu.ch/docs/tech/tech3342.pdf) | [OFFICIAL] | "a sliding analysis-window of length 3 seconds … A minimum block overlap of 2.9 s between consecutive analysis windows (i.e. ≥10 Hz sampling of the loudness level) is required; the exact amount of overlap is implementation-dependent." |
| S7 | Amdahl bound; the serial fraction dominates | [Amdahl, *Validity of the Single Processor Approach…*, AFIPS 1967 (reprint)](https://www3.cs.stonybrook.edu/~rezaul/Spring-2019/CSE613/reading/Amdahl-1967.pdf) | [PRIMARY RESEARCH] | "the effort expended on achieving high parallel processing rates is wasted unless it is accompanied by achievements in sequential processing rates of very nearly the same magnitude." (The original paper has no equations; the algebraic form S(n)=1/((1−p)+p/n) is the standard later formulation.) |
| S8 | V100 FP64/FP32 rates, HBM2 bandwidth, PCIe generation | [NVIDIA Tesla V100 datasheet](https://images.nvidia.com/content/technologies/volta/pdf/tesla-volta-v100-datasheet-letter-fnl-web.pdf) | [OFFICIAL] | "Double-Precision 7 TFLOPS … Single-Precision 14 TFLOPS" (PCIe), "16GB … HBM2", "900GB/sec", "PCIe Gen3". |
| S9 | Batched transforms exist and are faster; lower precision is faster | [NVIDIA cuFFT documentation](https://docs.nvidia.com/cuda/cufft/index.html) | [OFFICIAL] | "Execution of multiple 1D, 2D and 3D transforms simultaneously. These batched transforms have higher performance than single transforms." / "Half-precision …, single-precision … and double-precision … Transforms of lower precision have higher performance." |
| S10 | Memory bandwidth, not FLOPs, limits streaming kernels (basis for reading the 8-worker plateau) | [McCalpin, STREAM benchmark reference, Univ. of Virginia](https://www.cs.virginia.edu/stream/ref.html) | [PRIMARY RESEARCH] | "Computer cpus are getting faster much more quickly than computer memory systems. As this progresses, more and more programs will be limited in performance by the memory bandwidth of the system, rather than by the computational performance of the cpu." Used qualitatively only; the plateau itself is measured here (M14, M15). |

Numbers in this report that come from vendors or standards are used only for arithmetic
(transfer time, FLOP estimates, gating rules); no vendor performance claim is used as evidence that
this pipeline would be faster.

---

## (d) Measured facts vs inferences

### d.1 Measured facts

| # | Fact | Value | Artifact |
|---|---|---|---|
| M1 | Full capture wall time | 703.072 s | `perf/optimized-capture.json` |
| M2 | `capture_render_s` | 134.851 s | sealed record `timings` |
| M3 | `render_batch_s` | 98.470 s | sealed record |
| M4 | `render_master_s` | 9.505 s | sealed record |
| M5 | `source_activity_s` | 17.767 s | sealed record |
| M6 | `snapshot_s` / `digest_s` / `restore_verification_s` | 0.073 / 0.073 / 0.150 s | sealed record |
| M7 | `analysis_pool_wall_s` | 408.274 s | sealed record |
| M8 | `analysis_wall_s` | 418.177 s | sealed record |
| M9 | `dsp_worker_elapsed_sum_s` | 3011.645 s | sealed record |
| M10 | `wav_decode_worker_elapsed_sum_s` | 16.552 s | sealed record |
| M11 | workers / assets / tracks+master | 8 / 43 / 42+1 | sealed record |
| M12 | record / analysis / panorama bytes | 445,001,421 / 444,758,064 / 395,400 | direct `stat` |
| M13 | total WAV bytes | 3,419,286,126 | `audio[]` sum |
| M14 | 8/12/16 worker scaling (8 identical assets) | 95.817 / 95.122 / 91.636 s | `perf/worker-scaling.json` |
| M15 | peak tree RSS at 8/12/16 workers | 29.56 / 29.19 / 29.97 GB | same |
| M16 | Σ DSP in that scaling run | 673.588 / 667.166 / 651.034 s | same |
| M17 | archive append on a 20 s capture (42 assets, 242 MB) | 3.5162 s | `analysis/ARCHIVE-PROFILE-AFTER.json` |
| M18 | SHA-256 and copy throughput on stored assets | 1255.4 / 1586.8 MB/s | `analysis/ARCHIVE-HASH-PROBE.json` |
| M19 | record `json.dumps` compact / write / load (repo harness) | 10.253 / 11.338 / 10.384 s | `perf/PERF-WORKLOG.md` |
| M20 | probe: 515 MB payload — dumps 12.7 s, canonical 11.9–26.1 s, write+fsync 14.8 s, `json.loads` 10.9 s, `_freeze` 38.5–52.5 s, `FullCaptureRecord.__post_init__` **97.0 s**, `build_panorama` 1.8 s | see values | this study, scratch, contended |
| M21 | probe: WAV decode float64 0.166 s per 79.5 MB (480 MB/s); peak+RMS 0.415 s per asset | — | this study |
| M22 | probe: `import numpy, scipy.signal, soundfile` in a fresh interpreter | 1.7 s warm / 3.8 s cold | this study |
| M23 | probe: numpy 256 MB copy 8.6 GB/s, sum 2.7 GB/s | — | this study |
| M24 | parity: 129,215 cells compared; 2,824 cells differ in `short_term_lufs` (max abs(delta) 3033.8); 10 further **master** cells differ in `peak` (1) and `f0_hz` (9); band-time curve differs only for the master (339,793 values, max 2.17e-4 dB); `rms_400ms` differs only for the master (2,929 values, max 4.55e-8); event lists identical 43/43; `rms_dbfs` bit-identical 42/43 | — | this study on both sealed records |
| M25 | parity: bytes/frames/duration equal 43/43, container SHA-256 equal **0/43** | — | both records |
| M26 | parity: 11 project-map field diffs — `change_count` (412→86, 580→254), 8 × VST chunk containing an extra `Program 1` token, `transport.play_position_s` (300.536→150.000) | — | both records |
| M27 | 20 s reference capture: total 61.697 = render 18.253 + analysis 28.888 + archive 7.306 + 7.25 other | — | `analysis/capture_optimized_live.json` |

### d.2 Inferences and their basis

| # | Inference | Value / range | Basis | Confidence |
|---|---|---|---|---|
| I1 | Registration hashes + master gate (hash + full decode + classify) | 8.96 s (≈2.7 + 6.2) | M2 − (M6 + M3 + M5 + M4) | high |
| I2 | Work inside `_build_record` outside the pool | 9.903 s | M8 − M7 | high (measured difference) |
| I3 | **Unaccounted tail** = total − (render + restore + analysis_wall) | **149.9 s** | M1 − (M2 + 0.15 + M8) | high |
| I4 | Tail split: record seal 40–84 s, archive append 30–45 s, panorama/validation/response/mute-restore/cleanup 3–21 s | 73–150 s | M20 scaled by 445/515, M17 scaled by 3.42 GB/0.242 GB, M18, M19 | medium (two derived rows, one synthetic payload) |
| I5 | Per-asset DSP in the capture | 70.04 s | M9 / 43 | high |
| I6 | Per-asset DSP under 8-way concurrency in the scaling run | 84.2 s | M16 / 8 | high |
| I7 | Pool efficiency / tail | 92.2 % / 31.8 s | M9 / (M7 × 8); M7 − M9/8 | high |
| I8 | Mean CPU use of the iteration | 4.28 of 16 logical cores | M9 / M1 | high |
| I9 | WAV I/O per iteration | 34.2 GB (8 read passes + 2 write passes over 3.42 GB) | code paths in b.3 | high |
| I10 | 16 workers with ≥16 assets need ≈59 GB | 16 × 3.66 GB | M15 / 8 workers | medium-high (linear RSS assumption) |
| I11 | DSP bandwidth-bound, not core-bound | 8→16 workers = +4.4 % for 2× memory | M14, M15 | high |
| I12 | Packaging can be reduced to ~22–26 s without changing a byte of meaning | save ~105–125 s | M19/M20 minus one streaming dump (~13 s) + one hash pass (~3 s) + copy (~3 s) + panorama (1.8 s) | medium |
| I13 | Render can be fully hidden under DSP if per-file admission is available | save ~92 s | render 98.5 s vs pool 408 s, first file ≈ 2–3 s | medium (depends on A) |
| I14 | Batch-level pipelining (no per-file invariant) hides all but the first batch's render | save ~70–75 s with 4 batches | same arithmetic at batch granularity | medium |
| I15 | GPU, if fully vectorized in FP64, could remove ~30–45 % of DSP time; end-to-end gain ≤1.25× | — | stage shares below + Amdahl | low-medium |

### d.3 The 149.9 s tail, reconciled

`703.072 − (134.851 + 0.150 + 418.177) = 149.894 s`. This is not a rounding residual: it is the
record seal, the archive write and the response. Confirmed components:

- **Seal.** `FullCaptureRecord.__post_init__` deep-freezes `analysis` into `MappingProxyType`/tuples
  and then serialises the payload once for the integrity hash. On a 515 MB synthetic payload built
  from one real analysis member replicated 43×, the constructor took **97.0 s**; the freeze alone
  took 38.5–52.5 s and the canonical dump 11.9–26.1 s (variance caused by the concurrent audit).
  Scaled to 445 MB: **40–84 s**.
- **Archive.** `append` re-dumps the canonical payload (445 MB) for the new hash, dumps it a second
  time into `payload_json`, then writes 445 MB with `fsync` (M19/M20: 11–15 s), and performs 43 ×
  (source SHA + `copyfile` + temp SHA). At M18's rates that is 3.42 GB copied and 6.84 GB hashed →
  ~8 s, plus 24–28 s of dumping and 11–15 s of writing → **30–45 s**.
- **Rest.** Panorama build 1.8 s, index rewrite (index.jsonl is 297 KB), `validate_binding`,
  `_jsonable`, response, 42 mute RPCs, deletion of 43 WAVs → **3–21 s**.

Sum 73–150 s brackets the measured 149.9 s. The remaining uncertainty is concentrated in the two
stages that **have no timer at all**, which is itself a finding: the most expensive packaging work in
the pipeline is invisible in the artifact.

### d.4 Parity: four different kinds of "difference"

`perf/capture-parity.json` reports `analysis_numeric_diff_count = 337,134` and
`analysis_max_numeric_delta = 3033.8`, which reads as catastrophic drift. Parsed field by field, the
differences fall into four disjoint classes:

| Class | Findings | Verdict |
|---|---|---|
| **(a) Path / serialization** | 43 × `/source` (the temp job directory differs); `transport.play_position_s`; `change_count` 412→86 and 580→254; 8 × FX chunk carrying an extra `Program 1` token | Not a measurement difference. The FX chunk diff is byte-identical base64 apart from the inserted `\x00Program 1` string (`AAAQAAAA` vs `AFByb2dyYW0gMQAQAAAA`), i.e. plugin state serialization, not parameters. `change_count` is session provenance. `play_position` is the observed play position after rendering, which the code explicitly does not restore (`play_position_restorable=false`). |
| **(b1) Documented float reassociation** | `integrated_lufs` and LRA differ for 42/43 assets by ≤2.7e-11 (mostly 1e-15); `lra` mirrors it | Matches the intended one-pass K-weighting + prefix-sum change. Acceptable with a stated tolerance. |
| **(b2) Undocumented floor change in `short_term_lufs`** | **2,824 cells in 23/43 assets** differ; the baseline carries a denormal ring-down (−133 → −3233 dB across a silent tail) while the optimised path clamps to **−200.0**; max Δ 3033.8 | **Not 0.1 dB and not one cell.** The worklog's "one `short_term_lufs` cell at 0.1 dB" holds for the single dense 48 kHz test file, not for a 43-asset capture with silent tails. Gating is unaffected (−70 LUFS is far above both), but the published field changed. Fix: extend `_refine_low_power_short_term_blocks` (`metrics.py:145`) to treat **underflowed** blocks (prefix-sum power below the smallest normal, not just <1e-11) with the direct summation, so a block whose true energy is zero yields the same value as the reference. |
| **(b3) Master PCM not bit-identical** | The master's band-time curve differs in 339,793 of 362,432 values (max 2.17e-4 dB), `rms_400ms` in 2,929 values (4.55e-8), `rms_dbfs` 2.6e-9, centroid 6.3e-8 Hz, `peak` in 1 cell, `f0_hz` in 9 cells (up to 7.3 Hz), `pitch_jitter_pct` 430.77→430.78 | Consistent with the previously reported ~1.19e-7 FS master PCM difference. Everything derived from master samples moves at the expected magnitude. The f0 estimator itself is out of range on this material (cells report 4.8 kHz … 166 kHz at 44.1 kHz sample rate), so `f0_hz` and `pitch_jitter_pct` (430 %) must not be used as parity gates — they are a metric-quality defect independent of this comparison. |
| **(c) Container hash only** | bytes/frames/duration/SR equal 43/43; SHA-256 equal **0/43** | The WAV containers differ while the payload length is identical. **Not yet proven** metadata-only: no chunk-level inventory or PCM hash exists for all 43. |

**What is still missing for a real equivalence claim:** (1) a chunk-level inventory of all 43 WAV
pairs (RIFF/`fmt `/`data`/`bext`/`LIST`/`iXML` offsets and sizes, plus the hash of the raw `data`
chunk bytes); (2) a decoded-PCM SHA-256 for all 43 pairs (not 3 spot checks); (3) a repeated-render
test in one session to decide whether the master's 1.19e-7 is render nondeterminism; (4) a decision
on whether `short_term_lufs` is restored exactly or re-baselined with a documented floor; (5) the
`Program 1` chunk accepted as serialization with a recorded example. Until (1) and (2) exist, **the
runs are not equivalent** — they are "structurally identical, numerically identical within stated
tolerances for 42 tracks, and different in the master PCM".

---

## (e) Candidate designs

All designs keep: every track, the full master, every metric, the same stage/alignment/window, the
same lease/cleanup policy, and a bounded queue. Nothing below is an approximation.

### D0 — current pipeline, for reference

703 s. Serial: 98.5 render → 17.8 activity → 9.5+9.0 master+gate → 408.3 pool → 9.9 loop → 70–130
seal/archive → ~10 response/restore.

### D1 — Packaging only (no dependency on A, no semantic change)

Replace the seal/append path with a **single streaming write**:

1. **Workers emit canonical JSON fragments.** `analyze_asset_job` returns `(path, sha256, bytes,
   timings)` for an `analysis/<guid>.json` fragment written with `sort_keys=True,
   separators=(",",":")` — the same encoder settings the record uses. The parent never holds 43
   analysis dicts (removes ~445 MB of pickle traffic and ~2–4 GB of parent RSS).
2. **Stream the record.** Write `{"analysis":{...}` by concatenating fragments in GUID order through
   a hashing writer; emit the other top-level keys in sorted order. Reserve the 64-char
   `integrity_sha256` field, hash the two segments **around** it (the hash excludes that key, exactly
   as `canonical_payload` does today), then seek back and overwrite the placeholder. One pass, one
   hash, no 445 MB string, no `_freeze`.
3. **Copy assets before sealing.** The content-addressed blob path is `audio/sha256/<sha>.wav`, and
   the SHA is already known from registration, so `archive_audio`/`status` can be final **before**
   the record is written. That removes the second canonical dump and the second hash.
4. **One SHA pass per WAV.** Capture `register_asset()`'s return value (`RegisteredArtifact.sha256`)
   into `item["sha256"]`; delete the duplicated `_sha256_file` calls at `:1041`/`:1052`; let
   `append` verify only the temp copy it wrote.
5. **Skip the 42 no-op mute RPCs** when `mute_bridge_calls == 0` and the after-snapshot proves
   `mute_restored`.

Expected: **590–620 s** (central 596). Saves ~105 s of the 149.9 s tail (seal 40–84 → ~0, two dumps
→ one streaming dump, 5 SHA passes → 2, 42 RPCs → 0). Memory: parent peak falls from ~3–4 GB to
~1 GB during packaging. Risk: low — the record bytes are verifiable byte-for-byte against a
re-generated record (same encoder settings, same key order); the parity gate in (g) proves it.

### D2 — Bounded overlap (needs only batch granularity; full benefit needs A)

**D2a — batch pipelining (no new REAPER signal required).** Split the 42-track batch into 4 bounded
batches (e.g. 11/11/10/10). Submit batch *n* to the pool, then immediately issue the render RPC for
batch *n+1*; the pool runs in worker processes while the calling thread blocks in the render RPC.
Collect results only after the last render. Each batch's render is hidden behind the previous
batch's DSP (~100 s), except the first (~25 s). Expected save **~70 s**.

**D2b — per-file streaming (needs A's completion invariant).** Admit each WAV to the pool as soon as
it is provably closed and complete. The render is then fully hidden (save **~92 s**), and DSP starts
~2–3 s after the first track.

**D2c — activity and gate work into the pool.** `_track_activity` currently decodes 42 WAVs serially
in the parent (17.8 s) *and* the DSP pool decodes them again. Run the activity gate as 42 small pool
tasks submitted before the master render; they finish in ~3 s wall and overlap the 9.5 s master
render, so the 17.8 s serial block disappears (its work is unchanged and the numbers it produces are
byte-identical because it is the same `load_audio` + `np.max`/`np.mean` code). Let the master gate's
hash + decode overlap the pool. Expected save **~15 s + ~8 s**.

**D2d — scheduling.** Submit tasks longest-first (master first, then by `bytes` descending — all
assets are equal length here, but the master and dense stems are known-heavier), keep
`chunksize=1`, keep one pool per capture. Raise nothing: 8 workers, with a memory-derived cap
`W = min(16, floor((RSS_budget − parent) / 3.7 GB))` = **10** on 47 GiB. Expected save **~25–30 s**.

**D2e — render rate limiting.** While a REAPER render RPC is in flight, run at most `W−2` DSP workers
(the render itself is multi-core and streams 3.4 GB); ramp to `W` after it returns. This protects the
render from memory-bandwidth contention. Cost: a few seconds of DSP; benefit: render stays at 98 s
instead of stretching.

Expected D2 total: **440–480 s** (central 455). Memory ceiling unchanged (29.6 GB at 8 workers);
risk: medium (depends on A's invariant for D2b; D2a is safe without it but changes the render batch
count, which A must confirm is semantically neutral for per-track isolation and master FX).

### D3 — Exact DSP work (no approximation)

Ranked by measured share of the 34.9 s single-asset profile (`PERF-WORKLOG.md`, after existing
optimizations) and by risk:

| Stage | Share of profile | Exact fix | Expected |
|---|---|---|---|
| Temporal cells (3,005 Python iterations, 36 features each) | 31.1 s of 72.4 s in the 42-asset stage profile; 10.3 s of `analyze_cell` in the cProfile | Batch the 3,005 windows into one (3005, 4096, 2) rfft; vectorise the per-cell scalars (`rms`, `peak`, `crest`, `zcr`, spectral moments, ERB/band matrices, chroma) as array ops; keep per-cell rounding exactly (`round(x, 1)` on the same float64 inputs). `analyze_cell`'s FFT is 6010 rffts of 4096 ≈ 1.5 GFLOP **[INFERENCE: 5N log2 N]**. | 2–4× on this stage |
| Loudness (17.9 s) | 25 % | Already one K-weighting pass; remove the redundant `pyloudnorm` fallback when `loudness` is supplied; keep the direct-sum refinement for underflowed blocks (also fixes the b2 parity defect). | 1.2–1.5× |
| Transients (9.0 s) | 13 % | Single STFT already; replace `np.log1p` + `np.diff` over the full magnitude with a band-limited flux computed on the fly; blockwise windows to cut temporaries. | 1.3–1.8× |
| True peak (7.5 s) | 10 % | Polyphase FIR is already vectorised; process in blocks to avoid 2× full-length temporaries; consider a shorter verified filter only with a parity proof. | 1.2× |
| `_spectrogram` (3.4 s) | 5 % | Reuse one rfft for both channels via a (frames, 4096, 2) batched transform; avoid `np.stack` of per-channel powers. | 1.5× |
| `detect_events` (3.1 s) | 4 % | `_envelope` computes `np.abs(windows)` and `windows**2` as full temporaries; block the window loop (per-window results are bit-identical). | 1.5–2× |
| Full-buffer passes | — | One `abs()` materialisation reused for peak, clip count and clip events; one `corrcoef`. | ~0.4 s/asset |

Combined exact expectation: **1.4–1.6× on DSP** (3011 → 1900–2150 s), which at 8 workers is a pool
of 240–270 s. This is consistent with the measured 1.72× on the single file and the 1.30× actually
observed in the live capture (536.8 → 418.2 s), because the live mix of assets contains more silent
and sparse material where the earlier wins are smaller.

Expected D3 total (on top of D2): **285–330 s** (central 305).

### D4 — Schema v2 array sidecars (later), and GPU (research only)

**Schema v2.** Keep every field, but move the dense arrays (`band_level_time_dbfs` 6.42 MB,
`events_2ms` 2.18 MB, `rms_400ms_dbfs` and `spectrogram_time_s` 0.32 MB per asset) into
length-prefixed float64/int32 sidecars referenced by `{offset, length, dtype, sha256}`. Values stay
bit-identical (JSON float repr is an exact round-trip; float64 binary is the same number). With those
four fields out, the record falls from 445 MB to **~60 MB** (the per-cell scalar block, ~1.4 MB per
asset, stays inline); also sidecar-ing the cell arrays would reach ~25 MB but changes more of the
reader. Write 13 s → ~2 s, `read` 26 s → ~4 s, archive 15 GB → ~6 GB. Requires a v1 reader and a
migration; ~2–3 days. Only worth it after D1, whose streaming writer already removes the memory spike.

**GPU.** Stage shape on the V100:

| Stage | GPU-shaped? | Why | Transfer arithmetic |
|---|---|---|---|
| STFT / spectrogram / transient flux | **Yes** | batched rfft, 25,880 + 103,520 transforms of 4096/2048 | 212 MB per asset in (float64) at PCIe Gen3 ≈ 13 GB/s → **~17 ms**; VRAM for one asset's 2048/256 STFT ≈ 0.85 GB |
| True peak (polyphase FIR) | Yes | regular convolution | same buffer |
| Temporal cells | **Hostile as written** | 3,005 tiny 4096-point transforms + ~50 small numpy calls per cell; launch-bound and Python-bound | needs a full vectorised rewrite first (D3), then the batched FFT is ~1.5 GFLOP → <10 ms |
| K-weighting + block gating | No | sequential IIR + prefix sums over 13.25 M samples; bandwidth-bound | 212 MB, but a scan-based IIR is a rewrite with new numerics |
| Event scan / bisect | No | 2 ms envelope, Python loop over 150 k frames | — |

Conclusion: the GPU can plausibly remove the FFT/FIR subset (≈40 % of DSP) **only after** D3
vectorises the cell pipeline, and it must run **FP64** to keep the metrics contract (S9: "transforms
of lower precision have higher performance", so FP64 gives up half the V100's FP32 rate). One V100
shared by 8 worker processes needs 8 CUDA contexts (~0.3–0.5 GB each) plus 1–2 GB of per-asset
arrays — feasible on 16 GB but fragile, and the same GPU already backs the Flamingo/Ossicle tools in
this repo. Arithmetic ceiling **on top of D3**: the FFT/FIR work that D3 has not already vectorised is
at most ~20–25 % of the remaining DSP, so the pool would go from ~250 s to ~190 s and the iteration
from ~305 s to **~230 s** — a 1.3× gain for a full rewrite of the cell pipeline plus a FP64 GPU
backend, against 2.3× for D1–D3 at much lower risk. Note also that D3 and the GPU attack the **same**
FFT work, so their gains are not additive.
**Recommendation: do not put GPU on the critical path.** Run the spike in (g) only if D3 stalls.

### Decision matrix

| Design | Wall (s) | Δ vs 703 | Effort | Correctness risk | Peak RSS | Depends on A | Evidence confidence |
|---|---|---|---|---|---|---|---|
| D1 packaging | 590–620 | −85…−115 | 1–2 d | Low (byte-identical gate) | ↓ ~2 GB | no | Medium-high |
| D2a batch pipelining | 500–530 | −175…−200 | 2 d | Medium (render batch semantics) | unchanged | partly | Medium |
| D2b per-file streaming | 440–480 | −225…−260 | 3 d | Medium | unchanged | **yes** | Medium |
| D2 + D3 | **285–330** | **−375…−420** | 6–9 d | Medium (per-field parity gate) | ↓ | yes (D2b) | Medium |
| D4 schema v2 | −10…−20 more (after D1) | — | 2–3 d | Medium (v1 reader) | ↓↓ | no | High (mechanical) |
| GPU spike | unknown; ≈ −70…−80 only on top of D3 | — | 5–10 d | High (FP64 parity) | VRAM risk | no | Low |

**Recommended architecture:** D1 + D2c/d (no dependency), then D2a, then D2b when A's invariant
lands, then D3. Expected steady state **285–330 s per complete iteration** (2.1–2.5× today), with a
serial floor of **~230–250 s** if DSP reaches 2× and the record is streamed.

### Why the floor is what it is

After D1–D3 the irreducible serial work is: snapshot + digest 0.15, record loop ~10, streaming
write + hash + copy ~22, panorama + validation + response ~3, mute restore 0–1 → **~36 s**. DSP at
1.5–2× is 1900–2150 s of CPU work; at 8 memory-safe workers that is 240–270 s (1.5×) or 190–215 s
(2×). Render (98.5 s) is hidden. So the bound is **DSP + 36 s ≈ 230–305 s**, and every further gain
must come from making the DSP itself cheaper per sample, not from more parallelism — 8 workers
already deliver 92 % of the pool's ideal and 16 workers buy 4 %.

---

## (f) Failure modes

| Failure | Mechanism in the current code | Consequence | Mitigation / invariant |
|---|---|---|---|
| **RAM blow-up** | 16-worker cap × 3.66 GB/worker = 59 GB on a 47 GiB host | OOM or swap; a killed worker becomes `BrokenProcessPool` and the capture fails after 3.4 GB of renders | Worker count derived from measured per-worker peak and `psutil.virtual_memory().available`; hard ceiling 10; refuse to start if free RAM < `W × 3.7 GB + 2 GB` |
| **Unbounded queue** | 43 submission tuples (paths only) — RAM-safe, but 43 × 79.5 MB = 3.42 GB of WAVs must exist simultaneously, and the archive adds 3.42 GB | Disk exhaustion on a long session (24 captures = 15 GB of blobs already) | Pre-flight free-space check (`3 × total_wav_bytes`); bounded admission (≤ W admitted, ≤ 2W queued); lease cleanup as today |
| **Worker starvation** | 42 sequential no-op mute RPCs in `finally`, each up to 10 s | Up to 7 min of dead time on a slow bridge; the response is computed before this, so it is invisible | Skip when `mute_bridge_calls == 0`; otherwise one batched `set_track_mutes` call instead of 42 |
| **Spawn storm** | One pool per capture (8 × 1.7 s imports) | ~2 s wall per capture; a per-batch pool design would pay 4–6× | Keep one pool per capture; if batching, submit batches to the same pool |
| **Disk saturation** | 34.2 GB of WAV I/O per iteration (10 passes) + 1.3 GB of record I/O | 60–70 s of pure disk time at SATA SSD rates; hash passes are the largest removable share | 2 passes instead of 5 (−10.3 GB); stream the record (no 445 MB temp) |
| **Partial file analysed** | `capture_render` returns after the action; `register_asset` checks size>0 and size-stable-during-hash only | A file still being flushed could be hashed consistently if the writer pauses | A's completion invariant (header + data length + frame count + handle closure) before admission; current check is necessary but not sufficient |
| **Hash/copy duplication** | 5 SHA passes; `register_asset` result discarded; `_sha256_file` twice in one loop | ~14 s and 17 GB of reads per capture | Single pass; carry `RegisteredArtifact.sha256` into the record; verify only the archive temp copy |
| **JSON memory spike** | 3 × 445 MB dumps; `_freeze` creates ~5.6 M proxy/tuple objects; `dumps`+`encode` hold 445 MB str + 445 MB bytes | Peak +2–4 GB; 40–84 s of CPU; GC pressure (52.5 s vs 38.5 s with GC off) | Streaming writer, no freeze, fragments from workers |
| **Archive contention** | Two captures can target the same SHA blob; `target.exists()` then `os.replace` | Benign (same content) but the temp name must stay unique (it is: pid + `id(item)`) | Keep the temp-name scheme; add an O_EXCL claim if concurrent captures become routine |
| **GPU OOM / contention** | 8 CUDA contexts + per-asset 0.85–1.7 GB arrays on a 16 GB card also used by Flamingo/Ossicle | OOM kills the worker; model tools fail | Do not GPU-accelerate the default path; single GPU process with an explicit VRAM budget if the spike succeeds |
| **Tolerance drift** | Already realised: `short_term_lufs` −200 vs −3033.8 in 2,824 cells | Record differs visibly in silent tails; a "0.1 dB" claim in the worklog does not hold at capture scale | Restore the reference behaviour for underflowed blocks; add the per-field gate in (g) |
| **Silent metric change** | Any change to summation order, window, or rounding | Record looks valid, values moved | Per-field tolerance table in the parity gate; CI fails on an unexplained diff |
| **Cleanup / lease leak** | Process killed between `archive.append` and `workspace.close()` leaves `.incoming/job-*` | 3.4 GB leaked until `cleanup_stale_audio_jobs` (24 h) | Keep the 24 h sweep; log the job dir in the response so a retry can reclaim it; never delete a blob that a committed record references |
| **Response lost after commit** | Post-commit exception in `finally` → `_guard` returns `ok=false` | The 703 s run: valid record, failed response, no `capture_id` visible to the caller | Make post-commit failures warnings, not errors; return `capture_id` + `record_path` in the error payload if a capture was committed |

---

## (g) Experiment protocol and pass/fail criteria

### g.1 Fixtures and invariants

- **Same project revision** for every compared run (`revision_before.change_count` and `path` equal;
  `change_count` is provenance, so compare it as a recorded value, never as a parity target).
- **Same source GUID set** (43 = 42 tracks + master), same `stage` strings, same `capture_method`.
- **Same time window** (`0.0 … 300.52 s`), same `signal_window` per asset.
- **Same samples/rate/channels**: 13,252,932 frames, 44,100 Hz, 2 ch, 24-bit; assert per asset, not
  only in aggregate.
- **Same render stage**: `selected_track_via_master_parallel_batch_post_fx` for tracks,
  `master_mix_post_fx` for the master; assert `RENDER_SETTINGS`, `RENDER_SRATE`, `RENDER_CHANNELS`,
  `RENDER_TAILMS` echoes.
- **Full master**: master asset present, gate `valid`, exactly one `is_master`.
- **Event identity**: the `events_2ms` list per asset must be **byte-identical** (canonical hash), as
  it already is across the two sealed captures (43/43).
- **Record/panorama integrity**: `integrity_sha256` verifies on read, panorama binding valid,
  `coverage` keys equal and all true, `missing == []`.

### g.2 Metrics and statistics

- Primary: end-to-end `wall_s` per complete iteration, measured outside the tool (harness clock) and
  inside (`timings.total_s`), plus per-stage timers.
- Secondary: `analysis_pool_wall_s`, `dsp_worker_elapsed_sum_s`, `archive_s`, `seal_s` (new timer),
  peak process-tree RSS (sample every 250 ms with `psutil` over the whole tree), bytes read/written
  (`psutil.io_counters` delta), and the count of SHA passes (instrument `_sha256_file`).
- Statistics: **1 warm-up discarded + 5 measured repetitions** per design, report **p50 and p95** and
  the full list; a run is invalid if another capture/REAPER render was active (detect via the bridge
  lock file + `reaper_session`), if the harness process RSS at start exceeded 4 GB, or if any stage timer is
  missing. Outliers are **not** dropped; a run more than 20 % above the p50 is re-run once and both
  are reported.
- Acceptance for a performance change: p50 improvement ≥ 10 % and p95 not worse than the baseline
  p50. For a parity change: every gate in g.3 passes.

### g.3 Parity gates (fail-closed)

| Field class | Gate |
|---|---|
| Audio container | report SHA-256 equality **separately** from PCM; a container diff alone is not a failure but must be explained by a chunk inventory |
| PCM payload (all 43) | decoded float64 SHA-256 equal (sample-exact); master allowed ≤ 1.2e-7 FS with a recorded justification |
| `events_2ms` | canonical hash equal, 43/43 |
| `band_level_time_dbfs` | non-master: equal; master: max abs(delta) ≤ 1e-3 dB |
| `rms_400ms_dbfs` | non-master: equal; master: max abs(delta) ≤ 1e-6 dB |
| `temporal_profile.cells` | all fields equal except `short_term_lufs`; that field max abs(delta) ≤ 0.1 dB **or** exactly equal after the underflow fix |
| metrics | `integrated_lufs`, `loudness_range_lu` ≤ 1e-9; `rms_dbfs`, `crest_db`, `side_mid_db`, `mono_fold_loss_db` ≤ 1e-6; `spectral_centroid_hz` ≤ 1e-4; `pitch_jitter_pct` informational only (unstable) |
| project map | `tracks`, `items`, `takes`, `midi`, `fx` parameter values, `routing`, `envelopes`, `automation`, `markers`, `regions`, `tempo`, `selection` equal; `transport.play_position_s` and `change_count` excluded with a comment; FX chunk diffs must decode to identical parameter blocks |
| record | new record's `analysis` bytes equal the old record's `analysis` bytes when the DSP is unchanged (streaming-writer equivalence test) |

### g.4 Scripts to add (not executed here)

```
scripts/bench_capture_stages.py
  # runs FullCaptureService.capture on an isolated candidate copy, wraps the service with
  # time.perf_counter() around: snapshot, digest, render_batch, register, source_activity,
  # master_render, master_gate, after_snapshot, pool, record_loop, freeze, canonical,
  # stream_write, asset_copy, asset_hash, panorama, validate, response, finally_restore.
  # Writes work/benchmark/himmp/perf/stages-<capture_id>.json and never edits production code
  # (uses monkey-patched wrappers in the harness process only).

scripts/bench_pcm_parity.py --a <capA> --b <capB> --out work/benchmark/himmp/perf/pcm-parity.json
  # per asset: RIFF chunk inventory (id, offset, size, sha256 of payload), decoded float64
  # sha256, max abs delta, first differing sample index; separates container vs PCM verdicts.

scripts/bench_dsp_scaling.py --assets <dir with 43 sealed WAVs> --workers 4 6 8 10 12 \
        --repeats 3 --out work/benchmark/himmp/perf/dsp-scaling-<date>.json
  # reuses analyze_asset_job unchanged; reports pool wall, Σ DSP, peak tree RSS, per-asset
  # duration distribution, makespan vs LPT lower bound, p50/p95.

scripts/bench_record_stream.py --record <sealed record.json>
  # proves the streaming writer is byte-identical: rebuilds the record from fragments and
  # compares sha256 of the analysis object and of the canonical payload.

scripts/bench_gpu_stage.py --asset <wav> --stage stft|truepeak --dtype float64|float32
  # CPU vs CuPy/cuFFT on the same stage, transfer timed separately, per-field comparison
  # against the CPU result; fails if any field moves beyond the g.3 tolerance.
```

### g.5 Pass/fail for the recommended design

Pass only if **all** hold: (1) p50 wall ≤ 340 s and p95 ≤ 380 s for D2+D3 on the same project
revision; (2) every g.3 gate passes; (3) peak tree RSS ≤ 32 GB at 8 workers and ≤ 40 GB at 10;
(4) no unbounded queue (assert admitted ≤ W, queued ≤ 2W); (5) SHA passes per WAV = 2 (assert by
instrumentation); (6) `ok=true` with `capture_id` and a readable record/panorama on **5/5** runs;
(7) temp workspace removed after every run (`job-*` count unchanged).

---

## (h) What cannot be concluded

1. **The exact 149.9 s split.** Two of the three components have no timer; my split rests on a
   synthetic 515 MB payload and a 20 s archive probe, both under contention. The range 73–150 s is
   honest; a point estimate is not.
2. **Whether 8 workers is optimal for *this* 43-asset mix.** Only eight *identical* 300 s assets were
   scaled; per-asset durations are not stored, so the true load-balance distribution and the best
   worker count for a mixed-density project are unknown.
3. **Whether the master PCM difference (≈1.19e-7 FS) is render nondeterminism** or a real change in
   the master chain. No repeated-render-in-one-session test exists in the artifacts.
4. **Whether the container SHA differences are metadata-only.** Lengths are equal, but no chunk
   inventory or payload hash exists for all 43; three spot checks are not evidence for 42 tracks.
5. **Whether all 42 source PCMs are bit-identical.** Derived fields (band curves, RMS curves, event
   lists) are identical, which is consistent with but does not prove sample equality.
6. **Any GPU speedup.** No GPU path exists, no stage has been timed on the V100, and FP64/VRAM/context
   costs are unmeasured here. The V100 also serves other tools in this repo.
7. **Whether within-batch render completion is detectable.** That is A's question; D2b's ~92 s depends
   entirely on it. D2a's ~70 s depends only on batch semantics being neutral.
8. **Antivirus / filter-driver impact on file I/O.** The cold `cp` of 79.5 MB took 3.54 s (22 MB/s)
   while a cached read ran at 2.1 GB/s; the cause (Defender, SATA, or the concurrent audit) was not
   isolated.
9. **Scaling of the seal beyond one payload size.** `_freeze`/`__post_init__` were measured at one
   size (515 MB); linearity is assumed, not proven.
10. **Representativeness of 703 s.** One run, no repetitions, no p50/p95, on a project whose FX chain
    may not represent the next project.
11. **Whether the `Program 1` VST chunk difference changes plugin behaviour** — the parameter bytes
    are identical, but no audio-level A/B of that plugin state was run.

---

## Cross-workstream questions for A

| # | Question for A | B's working assumption |
|---|---|---|
| A1 | Does `Main_OnCommandEx(42230)` return only after every target is closed, or can a target be complete earlier? | It blocks until all targets finish; per-file completion is **not** observable through it. D2a (batch pipelining) is therefore the safe overlap; D2b is contingent. |
| A2 | Is `RENDER_TARGETS` / `RENDER_STATS` populated incrementally, and does either expose a completion flag? | Neither is a completion signal; only the RPC return is authoritative. |
| A3 | Does splitting 42 tracks into 4 batches change per-track isolation, master FX state, or PDC? | No: each target is rendered through the same master chain with the same settings; the only difference is 4 RPCs instead of 1 and 4 setting-restore cycles. A must confirm the per-batch overhead. |
| A4 | What is the per-render RPC overhead (setting restore, selection restore, file enumeration)? | ~1–2 s per call, so 4 batches add ~3–6 s and still net −70 s. |
| A5 | Can the master render run while the DSP pool is busy without changing the master PCM? | Yes, but it will contend for cores/bandwidth; D2e limits the pool to W−2 during render. |
| A6 | Is the master render bit-identical across two consecutive renders in one session? | Unknown; this is the missing test behind parity item (b3). |
| A7 | After a render, is REAPER's play position left moved (the record shows 150.00 → 300.51)? | Yes, and the code does not restore it; the digest therefore differs across sessions. A should confirm this is not a restore failure. |

---

## Coordinator reconciliation note (Hermes, 2026-11-13)

Independent recomputation confirms B's parity attribution: 2,824 `short_term_lufs`
cells in 23 assets, max 3033.8 dB (baseline floor −3233.8, optimized −200.0); only the
master differs in `band_level_time_dbfs` (≈332k values by a strict >1e-9 count, max
2.17e-4 dB — B's 339,793 uses a looser threshold) and `rms_400ms_dbfs` (≈2.3k, max
4.55e-8); all 43 event lists are byte-identical. Two small additions to B's §b.6:
A correctly identifies a third post-archive raise path, `_jsonable`
(`mcp_server.py:1725`), and the 42-RPC mute loop is exactly the kind of late RPC that
can lose a committed response. Neither changes B's design or its 285–330 s estimate.

---

### One-paragraph answer

The 703-second iteration is 58 % DSP, 19 % render, 21 % packaging and 2.5 % serial decode. The DSP is
already at 92 % of its 8-worker ideal and is bandwidth-bound (8→16 workers buys 4.4 %), so more
workers cannot help and 16 workers would need ~59 GB on a 47 GiB host. The packaging tail is the
largest removable block: 149.9 s of untimed seal/serialise/copy work, dominated by a deep freeze and
three 445 MB JSON dumps. Streaming the record from worker-written fragments, hashing once, copying
before sealing and skipping 42 no-op RPCs takes ~105 s out with byte-identical output; adding bounded
render/DSP overlap and longest-first scheduling takes the iteration to **440–480 s**; exact DSP
vectorisation of the 3,005-cell temporal path takes it to **285–330 s**, with a floor near 230–250 s.
The GPU is not on that path: transfer is negligible (212 MB per asset ≈ 17 ms at PCIe Gen3), but the
dominant stages are Python-shaped or sequential-IIR-shaped, FP64 is mandatory, and its best case
(~230 s) is a 1.3× increment on top of D3 — for a rewrite that D3 has already done half of.
