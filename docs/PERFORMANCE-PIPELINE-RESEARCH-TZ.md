# REAPER Workbench — research brief: maximum safe full-capture throughput

## Assignment

Research the fastest production-grade way to complete one full REAPER Workbench
iteration: render every project track plus the post-FX master, run the complete
current analysis suite for every asset, bind all outputs to one project revision,
and return a complete, self-describing capture. The purpose is to enable more
mixing/listening iterations per hour without losing context or silently changing
what the analysis means.

This is a research and design task. Do not edit production code, alter REAPER
projects, start a full capture, or change the current performance run. Return a
source-backed implementation design, a falsifiable benchmark plan, and an ordered
decision. Another Hermes task is currently finishing a live capture-parity audit;
do not interrupt or duplicate that run.

## Required breadth: cover every stage and every acceleration family

Do not reduce this study to render/analysis overlap, worker count, or GPU as a
single yes/no question. Build a **stage × technique matrix** for the whole
current full-capture path. Every cell must say `applicable`, `not applicable`
with a reason, or `unverified`; do not silently omit a class of optimization.
The matrix is a research map, not an instruction to implement every technique.

Cover at least these pipeline stages:

1. REAPER project/context reads, digest/revision checks, and bridge RPC.
2. Native rendering of every post-FX track and the processed master, including
   master-gate retries and render state restore.
3. WAV open/read/decode, payload transfer, and worker startup/scheduling.
4. Every current analysis family and its expensive subroutines: loudness,
   temporal/cell analysis, spectral/ERB features, events/onsets/transients,
   stereo/phase, and cross-track or reference aggregates. First enumerate the
   actual current functions from the worktree and profiler; do not assume this
   list is complete.
5. Optional model analysis already integrated or explicitly planned for
   capture. Mark it separately from deterministic DSP and do not count future
   models as current work.
6. Result assembly, provenance/hashes, record and panorama serialization,
   archive copy/write/readback, and cleanup/lease handling.

For every applicable stage, evaluate all relevant method families:

- Parallel work: across tracks/assets, within one asset, across pipeline
  stages, REAPER's own render/DSP parallelism, and bounded batch/queue designs.
- GPU: CUDA kernels or GPU libraries for deterministic DSP, and inference for
  optional models. Include V100/16 GB support, precision parity, transfer and
  launch overhead, VRAM high-water mark, contention with REAPER, and measured
  end-to-end crossover size. “GPU-capable” is not evidence of a speedup.
- Algorithmic improvements: mathematically equivalent reformulations,
  vectorization, FFT reuse, fused passes, better data structures, compiled
  kernels/native extensions, and reductions in repeated work. Separate exact
  transformations from approximate methods; approximations must remain
  opt-in and carry explicit error bounds and parity gates.
- Latency and data movement: process startup, RPC round trips, Python spawn,
  copies/serialization, disk reads/writes, memory layout, shared memory or
  memory mapping, chunking, and bounded back-pressure. Include the 47 GB RAM
  limit and cleanup requirements.
- Scheduling/configuration: worker counts, thread oversubscription, affinity
  only if supported by measurements, stage ordering, persistent workers, and
  safe overlap. Explain interactions with REAPER FX/PDC and master semantics.

For each candidate, report the bottleneck it targets, measured or source-backed
expected gain, applicability threshold, fidelity, CPU/GPU/RAM/I/O cost,
dependencies and failure modes, plus a reproducible experiment with pass/fail
criteria. Rank candidates by end-to-end complete captures per hour, not isolated
kernel speed. Preserve the full metrics and content by default; no caching,
dropped features, reduced sample rate, or lower-resolution analysis.

## Project intent and hard constraints

- The snapshot must stay complete: all project tracks, the processed master,
  project map, revision/provenance, and every current analysis output.
- Do not propose caching, dropping metrics, reducing sample rate, analyzing
  lower-resolution audio, or keeping only a subset of tracks as the default.
- Do not change the meaning, stage, alignment, or window of any measurement to
  make a benchmark look faster. Exact algorithmic improvements and parallel
  scheduling are welcome; any approximation must be separately named, opt-in,
  uncertainty-labelled, and compared with the full mode.
- Use REAPER's documented API/official mechanisms. No Frida, memory injection,
  private process-memory inspection, or project mutation for performance tricks.
- Hardware context: Windows, REAPER 7.84, Python 3.13 environment, one Tesla
  V100 16 GB, 47 GB system RAM. GPU use is optional; no claim of GPU benefit
  without end-to-end measurement. The current audio DSP path is CPU-based.
- The official online ReaScript help currently identifies itself as generated
  by REAPER 7.85. Separate documentation claims from behavior verified on the
  installed REAPER 7.84; check release notes for version availability before
  recommending an API flag.
- Temporary WAVs must remain outside the REAPER project and obey the existing
  lease/cleanup policy. A file still being written must never be analyzed or
  archived as complete.
- An iteration is successful only if it returns a valid capture ID and readable
  archive record/panorama, with the master gate passed and project state restored.
  An archived record alone is not a successful tool response.

## Current code path to inspect

Start by reading the current worktree, not by assuming this description is exact.

- `src/reaper_audio/daw/full_capture.py`: `FullCaptureService.capture`,
  `_render_batch`, `_render_master_only`, `_build_record`,
  `_analyze_rendered_files`.
- `src/reaper_audio/daw/analysis_workers.py`: per-asset decode and
  `measure_audio` worker.
- `scripts/reaper_bridge.lua`: `project_capture_render`; it sets the render
  target list, calls the native render action via `Main_OnCommandEx(42230, ...)`,
  then enumerates and opens targets after that action returns.
- `src/reaper_audio/analysis/artifacts.py` and
  `src/reaper_audio/context/full_capture.py`: temporary artifact lifecycle,
  archive, hashes, record serialization, panorama creation/readback.
- `work/benchmark/himmp/perf/PERF-WORKLOG.md`, `worker-scaling.json`,
  `optimized-capture.json`, `capture-parity.json`, and the latest sealed
  `record.json` are current measurement evidence. Treat hashes and response
  success carefully: a wrapper error can coexist with an archived record.

## Verified or reported benchmark evidence to independently check

Use the artifact files as authority and distinguish direct measurements from
derived estimates.

1. A 300.52 s, 48 kHz stereo WAV analysis fell from 60.245 s to 34.946 s
   (1.72x) after exact event de-duplication and temporal/loudness optimizations.
   The event list matched the baseline; the documented short-term loudness
   tolerance was 0.1 dB and LRA drift was about 2.9e-11.
2. Eight identical 300 s WAVs: 8/12/16 workers took 95.817/95.122/91.636 s;
   measured peak process-tree RSS was 29.6/29.2/30.0 GB. More workers gave
   little gain on that batch.
3. Latest valid full-song capture: 300.52 s, 42 tracks + master, 43 analysis
   assets, 8 workers; `elapsed_wall_s=703.072`, `capture_render_s=134.851`,
   `render_batch_s=98.470`, `render_master_s=9.505`,
   `analysis_wall_s=418.177`, `analysis_pool_wall_s=408.274`. Confirm every
   number in the sealed record. The first retry was invalid because its master
   was silent; a clean isolated REAPER restart produced a valid master.
4. A spot-check of three source PCM payloads found exact sample equality; that
   does **not** yet prove all 42 source assets. A master PCM comparison reported
   a maximum difference around 1.19e-7 FS. Container SHA values differ,
   plausibly because of WAV metadata; verify rather than assume. A project
   digest field also differs in FX chunk metadata (`Program 1`); establish
   whether that changes behavior or is serialization only. Do not call the run
   equivalent until all required comparisons are complete.
5. End-to-end time includes work beyond `capture_render_s` and
   `analysis_wall_s`; account for every stage (snapshot, project digest,
   source-activity check, archive copy/hash, record serialization, panorama,
   validation, wrapper/MCP serialization). Do not infer a total from partial
   timers or from a prior estimated baseline.
6. A later full rerun after the low-power loudness correction produced archive
   record `cap_01M7A9ESAB5QA5T6QWSVV5SKDA`: wall time 702.726 s,
   `capture_render_s=137.836`, `analysis_wall_s=419.906`,
   `analysis_pool_wall_s=410.351`, 42 tracks + master and 43 analyses. It is
   **invalid**, not a successful performance result: both master attempts were
   silent (`peak_dbfs=rms_dbfs=-160`) despite 42 audible source tracks, and the
   master gate rejected them. The benchmark wrapper overwrote its structured
   response with a traceback, but the sealed record contains the timings and
   failure evidence. Do not infer a cause or claim a speedup from this run;
   account for the reliability failure when defining a valid throughput sample.
7. The official API page documents `RENDER_SETTINGS & (16<<16)` as a delay for
   FX initialization/sample loading, with `RENDER_DELAY` in seconds. Treat this
   as a diagnostic hypothesis for silent-master failures, not as a proven fix.
   Verify availability in REAPER 7.84 and specify a short isolated A/B render
   before recommending production use; a restart or one successful render does
   not establish the failure's cause.

## Workstream A — REAPER render completion and overlap pipeline

Investigate only the DAW/bridge side and safe hand-off to analysis workers.

1. Establish exactly what REAPER 7.84 exposes through documented ReaScript for:
   render progress, completion callbacks/events, per-target status, render
   statistics, and completed output files during a multi-target batch.
   Cite the current official API docs and any relevant official release notes.
2. Determine whether `Main_OnCommandEx(42230, ...)` blocks until all targets
   finish, and whether `RENDER_TARGETS`/`RENDER_STATS` provide any completion
   signal before return. Do not infer a callback from file appearance alone.
3. Compare safe designs:
   - Native batch with a race-safe watcher that starts analysis only after a
     target is provably closed and complete.
   - Several bounded render batches, with Python analyzing batch N while
     REAPER renders batch N+1.
   - One target/region at a time with explicit bridge acknowledgements.
   - Any genuinely native streaming or render-queue route supported by REAPER.
   For each, explain render overhead, state changes, FX/PDC/master semantics,
   ordering, revision binding, and crash recovery.
4. Specify the completion invariant before a file enters the worker pool:
   expected GUID/path, RIFF/WAVE header and data length, frame count, channels,
   sample rate, REAPER file-handle closure or authoritative RPC completion,
   stable hash, and handling of interrupted/partial files. A fixed sleep or
   “size did not change once” is not sufficient.
5. Address master dependencies. Per-track post-FX sources and the master render
   are different stages. State whether the master may render concurrently with
   track DSP analysis, and what evidence must be gathered before accepting the
   master gate.
6. Give a minimal REAPER-only experiment proving whether within-batch overlap
   is possible. Keep all output under a disposable isolated test project and
   outside the project media folder.

## Workstream B — end-to-end throughput and compute architecture

Investigate only analysis/packaging and whole-pipeline performance beyond the
REAPER render-completion question. Coordinate questions with A, but do not
duplicate A's API/render research.

1. Build a stage model from current files/timings. Identify critical path,
   serial fractions, CPU utilization, disk bandwidth/IOPS, process spawn and
   transfer costs, memory high-water marks, record/archive/serialization costs.
   Reconcile elapsed wall time against all internal timers.
2. Evaluate maximum safe overlap of the existing process pool with render,
   master validation, project-context capture, and archive work. Consider
   bounded queues/back-pressure, worker lifetime across batches, CPU affinity
   only if justified, and Windows spawn semantics. Never allow unbounded
   queued WAVs or >47 GB RAM use.
3. Investigate deeper exact optimizations: avoid repeated decoding/copying,
   shared-memory/zero-copy only when safe, native NumPy/SciPy/vectorization,
   worker start-up amortization, memory layout, I/O sequencing, serialization
   of the 445 MB record, content-addressed archive cost. Also inspect every
   current analysis family/subroutine and any already-integrated model path;
   show a profiler-backed stage × technique matrix so no expensive component
   is missed. Separate what affects analysis from what only affects packaging.
4. Assess GPU acceleration per eligible deterministic DSP family and per
   integrated/planned model path on the V100 end-to-end, accounting for transfer,
   launch overhead, precision, dependencies, VRAM and contention. Report the
   minimum workload size where GPU could win, or state why no defensible GPU
   candidate exists. Reject speculative “GPU is faster” claims without a
   benchmark design. CPU-only and GPU-assisted choices must preserve the full
   metrics contract.
5. Propose Amdahl-style lower bounds using measured stage times. Quantify
   expected iteration time for each candidate design, with a range and
   assumptions, not a guaranteed promise. Include a “best attainable without
   approximating or dropping metrics” estimate.
6. Specify benchmark and parity gates: same project revision, source GUID set,
   time window, samples/rate/channels, render stage, full master, event
   identity, numeric per-field tolerances, panorama/record integrity, process
   tree peak RSS, p50/p95 across repeated runs. Report WAV-container hashes
   separately from PCM-payload equality.

## Required joint synthesis from Hermes

Hermes must spawn exactly two non-overlapping research subagents, one per
workstream above. The subagents must read this brief and inspect the current
worktree independently; they may exchange questions about shared dependencies
but must not copy each other's analysis. Save their reports as
`docs/PERFORMANCE-PIPELINE-REPORT-A-RENDER-OVERLAP.md` and
`docs/PERFORMANCE-PIPELINE-REPORT-B-THROUGHPUT.md`. After both return, Hermes
must reconcile them into `docs/PERFORMANCE-PIPELINE-RESEARCH.md` with:

1. Current pipeline diagram and measured serial critical path.
2. Ranked designs: immediate low-risk, high-upside pipeline, and more radical
   alternatives. State what can run concurrently and what cannot.
3. A decision table with expected wall-time reduction, implementation effort,
   correctness risk, memory, dependencies, and evidence confidence. It must
   cover every applicable stage × technique cell from the required-breadth
   section, with explicit reasons for `not applicable` or `unverified` entries.
4. One recommended architecture for the user's priority: maximize complete
   analysis iterations/hour while preserving all tracks, master, metrics,
   revision/provenance, and reliable file cleanup.
5. Exact implementation sequence and acceptance checks, with no hardcoded
   musical workflow and no unlabelled approximations.
6. Explicit unanswered questions and experiments needed before coding.

## Report format and quality bar

Each subagent report must state: (a) question and scope, (b) current code
observations with file/line references, (c) external sources with direct links
and access date, (d) measured facts vs inferences, (e) candidate designs,
(f) failure modes, (g) experiment protocol and pass/fail criteria, (h) what
cannot be concluded. Prefer official Cockos/ReaScript sources for API claims;
also search serious engineering reports and primary research for scheduling,
streaming, signal processing, and pipeline techniques. Any third-party source
must be identified as such. The two reports must cross-reference shared
dependencies without copying each other's conclusions.

No implementation changes are authorized by this brief. Hermes should return
the two reports plus the joint synthesis, then stop for review before code
changes.
