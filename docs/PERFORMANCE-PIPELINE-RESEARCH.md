# REAPER Workbench — maximum safe full-capture throughput

Joint synthesis of Workstream A (`PERFORMANCE-PIPELINE-REPORT-A-RENDER-OVERLAP.md`) and
Workstream B (`PERFORMANCE-PIPELINE-REPORT-B-THROUGHPUT.md`), reconciled against the
sealed artifacts and the current worktree.

Date: 2026-11-13. Status: research/design only. No production source, test, project,
render artifact, or capture archive was modified. The only new files are the two
workstream reports and this synthesis.

Evidence labels: **[MEASURED]** = present in an artifact or independently recomputed
here; **[SOURCED]** = external document (see the two reports' source tables);
**[INFERENCE]** = arithmetic or code reasoning from the two, with its basis stated.

---

## 1. Current pipeline and the measured serial critical path

### 1.1 What actually runs, in order

```
FullCaptureService.capture()                       src/reaper_audio/daw/full_capture.py:298
│
├─ 1  snapshot (items, FX params, MIDI)            :335          0.073 s
├─ 2  project digest + coverage gate               :355          0.073 s
├─ 3  track batch render, 42 targets, one RPC      :389/:714    98.470 s   ← pool does not exist
│      render_settings=128 (+8<<16), via master,
│      parallel_via_master=True, timeout 600 s
├─ 4  per-track register_asset: full SHA-256       :420         ~2.7 s
├─ 5  source-activity gate: full decode of 42      :427-433     17.767 s  ← pool does not exist
├─ 6  master render, master-only, one RPC          :434/:744     9.505 s  ← pool does not exist
├─ 7  master gate: SHA + full decode + classify    :793-797     ~6.2 s
├─ 8  after-snapshot + digest + restore report     :461-471      0.150 s
├─ 9  analysis pool, 8 spawn workers, 43 tasks     :1026/:256   408.274 s ← render does not exist
├─ 10 per-asset record loop (2× SHA per asset)     :1031-1065    9.903 s
├─ 11 FullCaptureRecord.__post_init__: freeze +   context/full_capture.py:194-396
│      canonical 445 MB dump + integrity hash       (no timer)   40-84 s
├─ 12 CaptureArchive.append: re-dump, 3.42 GB      context/full_capture.py:527-696
│      copy + 2 hash passes, 445 MB write+fsync     (no timer)   30-45 s
├─ 13 panorama load/validate, master validation,   full_capture.py:523-587  ~3-21 s
│      response assembly
└─ 14 finally: 42 sequential no-op mute RPCs +     :588-600      (after total_s)
       delete 43 temp WAVs
```

**[MEASURED]** sealed capture `cap_01M7A7ZV8VWK4SA8FHGRC9Q04S`:
`elapsed_wall_s=703.072`, `capture_render_s=134.851`, `render_batch_s=98.470`,
`render_master_s=9.505`, `source_activity_s=17.767`, `analysis_wall_s=418.177`,
`analysis_pool_wall_s=408.274`, `dsp_worker_elapsed_sum_s=3011.645`,
`wav_decode_worker_elapsed_sum_s=16.552`, 42 tracks + master, 43 assets, 8 workers.
The brief's `13.399` for decode is stale; the record says **16.552** (both reports
independently caught this).

### 1.2 Where the 703 seconds go

| Block | Seconds | Share | Timer? |
|---|---:|---:|---|
| DSP analysis pool | 408.274 | 58.1 % | yes |
| Render (batch + master) | 107.975 | 15.4 % | yes |
| Non-render work inside `capture_render_s` (snapshot, digest, 2.7 s registration hashing, 17.767 s source-activity decode, 6.2 s master gate) | 26.876 | 3.8 % | partly |
| Restore snapshot/digest | 0.150 | 0.02 % | yes |
| Record loop after pool | 9.903 | 1.4 % | implied |
| Packaging tail: seal, archive, panorama, validation, response, mute restore | 149.894 | 21.3 % | **no** |
| **Total** | **703.072** | 100 % | |

**[MEASURED/derived]** `703.072 − 134.851 − 0.150 − 418.177 = 149.894 s`.
**[INFERENCE]** That tail decomposes as seal 40–84 s + archive 30–45 s + response/
restore/cleanup 3–21 s (B §d.3; the seal figure comes from a 515 MB synthetic-payload
probe, scaled to 445 MB — the only unmeasured extrapolation in the model).
Two of the three largest packaging costs have **no timer at all**.

### 1.3 Utilization facts that constrain every design

- **[MEASURED]** Pool efficiency: `3011.645 / (408.274 × 8) = 92.2 %`; ideal-balance
  wall `3011.645 / 8 = 376.5 s`; scheduling tail ≈31.8 s.
- **[MEASURED]** Whole-iteration CPU use: `3011.645 / 703.072 = 4.28` of 16 logical
  cores (26.8 %). The machine is idle most of the time because the pipeline is serial,
  not because the DSP is slow.
- **[MEASURED]** 8/12/16 workers on eight identical 300 s assets: 95.817 / 95.122 /
  91.636 s; peak process-tree RSS 29.56 / 29.19 / 29.97 GB. 16 workers buy 4.4 % for
  2× the memory: the DSP is bandwidth-bound.
- **[MEASURED]** per-worker peak ≈3.66 GB. With a 47 GiB budget, 16 busy workers need
  ≈59 GB — the current `MAX_ANALYSIS_WORKERS = 16` can OOM the host. The memory-derived
  cap is **10**.
- **[MEASURED]** Every asset is 44,100 Hz, 2 ch, 24-bit, 13,252,932 frames, 300.520 s,
  79,518,282 bytes; total 3,419,286,126 bytes. Both render paths used `RENDER_SRATE=0`
  (project rate), so there is **no** 48 kHz/44.1 kHz mismatch in this capture; the 48 kHz
  figure in the brief belongs to a separate saved test WAV.

---

## 2. Reconciliation: what the two reports disagree about, and what the artifacts say

### 2.1 The parity question is settled against the brief's suspicion

The brief warned that the optimized run's `analysis_numeric_diff_count=337,134` and
`analysis_max_numeric_delta=3033.8` might be path-string contamination. Workstream A
inferred exactly that. Workstream B parsed both records field by field and concluded the
opposite. **I re-ran the comparison independently; B is right.**

| Finding | Value | Verdict |
|---|---|---|
| `short_term_lufs` cells differing | **2,824 cells in 23 of 43 assets**, max delta **3033.8**, baseline minimum **−3233.8 dB**, optimized minimum **−200.0 dB** | Real field change in silent tails, caused by denormal underflow handling. Not path strings. |
| Cells exceeding the documented 0.1 dB tolerance | **2,774** | The worklog's "one cell at 0.1 dB" holds for the single 48 kHz test file, **not** for the 43-asset capture. |
| `band_level_time_dbfs` | Only the **master** differs: ≈332k of 362k values, max **2.17e-4 dB**; all 42 non-master assets exactly equal | Master-only PCM drift, not a track change. |
| `rms_400ms_dbfs` | Only the master: ≈2.3k values, max **4.55e-8 dB** | Same cause. |
| Metrics (`integrated_lufs`, LRA, RMS, crest, centroid, pitch jitter) | Only the master differs; `rms_dbfs` delta 2.6e-9, LRA 1.7e-9, centroid 6.3e-8 Hz, pitch jitter 0.01 pp | Track metrics are numerically identical. |
| Event lists | **43/43 byte-identical** (canonical hash) | Exact. |
| Container SHA-256 | **0/43 equal**, while bytes/frames/duration/SR are 43/43 equal | Metadata-only is plausible but **unproven** — no chunk inventory, no all-43 PCM hash. |
| Master PCM | ~1.19e-7 FS (from the brief; consistent with every master-only metric delta) | Plausible render nondeterminism; no repeated-render test exists. |

**[MEASURED, recomputed here]** The 337,134 counter decomposes into roughly
331,940 band values + 2,824 short-term cells + 2,332 RMS values + ~10 other master
cells (counts are threshold-dependent; B reports slightly higher counts at a looser
threshold). The counter is real, but **almost all of it is either silent-tail floor
or master-only drift**, not track DSP drift.

**Consequence:** the optimized capture is *not* "exact" under its own published
tolerance. The speed win is real, but the parity claim must be repaired before any
further optimization is layered on it. This is Phase 0 below.

### 2.2 The 703-second run that returned an error

**[MEASURED]** `optimized-capture.json` has `ok=false`,
`RuntimeError("capture did not return a valid capture_id")`, `elapsed_wall_s=703.072`,
while the archive contains a valid record with `capture_status:"valid"`, master
attempt 1 valid at −1.42 dBFS peak.

**[MEASURED, code]** `reaper_capture_snapshot` is wrapped by `_guard`
(`mcp_server.py:147-154`), which converts any exception into an error payload. The
harness (`optimized_capture_run.py:51-58`) requires a top-level `capture_id`, then
overwrites its own JSON with the traceback. Three post-archive raise paths exist:
the 42 sequential mute-restore RPCs in `finally` (`full_capture.py:588-600`), the
freshly written panorama load (`:525`), and `_jsonable` (`mcp_server.py:1725`).
The artifacts cannot say which fired because the response was discarded.

**Consequence:** a durably committed capture can be reported as a failure, and the
caller loses the only key needed to recover it. This is a correctness defect in the
success contract, independent of performance, and must be fixed before any timing
work (a lost response makes every benchmark suspect).

### 2.3 Smaller corrections to the brief and to A

| Claim | Reality | Source |
|---|---|---|
| "wav_decode sum 13.399 s" | **16.552 s** in the sealed record | record `timings` |
| "panorama 395,429 bytes" | **395,400 bytes** on disk | `stat` |
| "one short-term cell at 0.1 dB" | True only for the single 48 kHz test WAV; the capture has 2,774 cells above 0.1 dB | §2.1 |
| "337,134 numeric diffs are path strings" (A §h.12) | False; they are real numeric leaves, dominated by the silent-tail floor | §2.1 |
| "record's `timings.total_s`" (A §b.7) | `total_s` and `archive_s` are added to the **response only** (`full_capture.py:582-586`); the sealed record cannot answer packaging time | code |
| "two post-archive raise paths" (B §b.6) | Three, including `_jsonable`; A is more complete | code |
| "master 44.1 kHz is a divergence" | No: all 87 sample-rate fields are 44,100 and all 44 frame counts are 13,252,932; the project is 44.1 kHz | record |
| `Main_OnCommandEx(42230)` blocking | Measured and third-party corroborated, but **not documented** by Cockos; treat as high-confidence, not a guarantee | A §c/§h |

---

## 3. What can and cannot run concurrently

| Pair | Allowed? | Why |
|---|---|---|
| Track batch render ∥ master render | **No** | Both are `Main_OnCommandEx(42230)` on REAPER's main thread; they queue. |
| Any second bridge RPC ∥ a render | **No** | The Lua handler runs synchronously on the main thread; `bridge_client` also holds one global `client.lock`. A render call blocks the whole mailbox. |
| Project snapshot / digest ∥ a render | **No** | Same bridge lock and main thread. |
| DSP worker pool ∥ a render | **Yes** | Workers are separate OS processes; they touch only closed WAV files and never call the bridge. This is the only large overlap available today. |
| Master render ∥ DSP of already-admitted tracks | **Yes** | Same reason; master is one more REAPER call, DSP is elsewhere. |
| Archive copy/hash ∥ DSP | **Yes, with care** | Once a file is closed and admitted, copying and hashing it is read-only; it contends for disk and page cache but cannot change the measurement. |
| Record serialization ∥ archive copy | **Yes** | The streaming writer needs only the analysis fragments and metadata; the asset copy is independent. Must not start before all assets are final. |
| Batch k+1 render ∥ batch k DSP | **Yes** | The calling thread blocks in the render RPC; the pool runs in workers. Requires a track-subset render API (see §4.3). |
| Per-file admission inside one batch ∥ that batch's render | **Only if** REAPER closes targets progressively | Unknown; E3 decides. A preallocated file has its final size from the first byte, so size stability is not proof. |
| Analysis of a capture ∥ a *new* capture of the same project | **No, by default** | The second capture changes render settings/selection and the revision counter; the first record's revision binding would be stale. Only legal on a separate saved copy/instance, not the interactive path. |

**Revision rule:** every render and every analysis in one iteration must fall inside
one unchanged `change_count` window. Per-batch renders add counter churn (~84 per
call); the record must bind `revision_before`, `revision_after_restore`, and the
persisted target list, not just the last render's echo.

---

## 4. Ranked designs

### 4.1 Tier 0 — correctness and observability (no performance claim)

| # | Change | Why it must come first |
|---|---|---|
| 0.1 | Fix `short_term_lufs` underflow parity (direct summation for zero-energy blocks) | The current "exact" path changes 2,824 published cells. Every later optimization inherits the defect. |
| 0.2 | Make post-commit failures non-fatal: return `ok=true` + `warnings[]` + `capture_id`, persist the response before cleanup | The 703 s run lost its `capture_id` despite a valid record. |
| 0.3 | Add timers: `seal_s`, `archive_s`, `panorama_s`, `response_s`, `finally_s`, `restore_s` | 21 % of the iteration is untimed; no benchmark can attribute it. |
| 0.4 | Completion invariant C1–C7 (expected path, exclusive-open closure, RIFF/`data` length, frames, rate/channels, double hash, mtime window) + persist the intended target list before render; quarantine failures | Today the only closure proof is the RPC return; a partial file can be hashed and analyzed. |
| 0.5 | One SHA pass per WAV; carry `register_asset()`'s digest into the record; delete the duplicate `_sha256_file` calls at `:1041`/`:1052` | 5 hash passes / 17 GB per capture; two of them are provably redundant. |
| 0.6 | Skip the 42 no-op mute RPCs when `mute_bridge_calls==0`; otherwise one batched call | They run after `total_s`, can time out, and are the most likely response-killer. |
| 0.7 | Break the master-gate circularity: source activity is decoded from via-master stems, so a master insert that kills signal makes every stem silent and the classifier accepts "expected silence". Measure activity from a pre-master tap (or an independent witness) before accepting that branch | A silently broken master can currently pass as intentionally silent. |

**No audio change** except 0.1, which restores the reference value. **Expected time
change: −5 to −15 s** (0.5 + 0.6), not the reason to do it.

### 4.2 Tier 1 — immediate low-risk, high-value: packaging (no dependency on A)

**Design P1 — streaming, copy-before-seal record.**

1. Workers return a canonical `analysis/<guid>.json` fragment plus sha/bytes/timings;
   the parent never holds 43 analysis dicts (removes ~445 MB of pickle traffic and
   2–4 GB of parent RSS).
2. Write the record once through a hashing stream in canonical key order; reserve the
   64-char `integrity_sha256`, hash the segments around it, seek back and fill it.
   One dump, one hash, no 445 MB string, no `_freeze`.
3. Copy each WAV to its content-addressed blob **before** sealing (the SHA is already
   known), so the record is final when written; verify only the temp copy.
4. Longest-first scheduling (master first, then bytes descending), `chunksize=1`,
   one pool per capture, memory-derived cap `W=min(16, floor((budget−2 GB)/3.7 GB))=10`.
   These are low-risk and included in Phase 1, but the 590–620 s estimate below is
   packaging-only; the scheduling gain is extra and will be measured, not assumed.

**Expected: 590–620 s (central ~600), −83…−113 s.** Effort 1–2 days. Risk low: the
streaming writer can be proven byte-identical against a re-generated record; no metric,
window, stage, or file changes.

### 4.3 Tier 2 — high-upside pipeline: render/analysis overlap (depends on A)

**Design P2a — master-first + bounded batches.**

- Render master first (`RENDER_SETTINGS=0`), run the fail-closed classifier, then the
  track batches. A silent master costs 9.5 s instead of 108 s + 418 s.
- Split 42 tracks into 4 bounded batches (11/11/10/10) by stable GUID order. This
  requires an additive `track_guids`/`track_subset` parameter in `capture_render` and
  per-batch selection in the Lua handler; the current API always selects all tracks.
- Submit batch *k* to the existing pool, then issue the render RPC for batch *k+1*;
  collect futures only after the last render. Rate-limit the pool to `W−2` while a
  render is in flight if measurement shows >5 % render stretch.
- Fold `_track_activity` into the worker payload: the same `load_audio` pass already
  produces the peak/RMS the master-silence classifier needs, so returning those two
  numbers removes the 17.8 s serial decode. This is exact and independent of A; the
  table's 500–530 s excludes it, and the range becomes ~485–515 s with it.
- **Gate E4 first:** a 4-batch split must be PCM-neutral per track against the
  single-batch reference. "Via master" stems include track sends, so selecting a
  subset can change what is included if a send leaves the subset. If E4 fails, P2a
  collapses to master-first + fail-fast only.

**Expected: 500–530 s (central ~515), −173…−203 s including P1.** Incremental render
saving ~70–75 s. Effort +2 days. Risk medium (render semantics, not DSP).

**Design P2b — per-file streaming.**

Admit each WAV as soon as A's C2 closure probe succeeds. Render is then fully hidden
except the first file (~2–3 s) and the master. **Expected 440–480 s, −223…−263 s.**
Effort +3 days. Risk medium. **Contingent on E3**: if REAPER preallocates all targets
and closes them only at the end, per-file admission is impossible and P2a is the
ceiling. The only official statement about partial files (REAPER 7.37: prompt to
save/delete partially rendered output after cancel) confirms that partial files can
survive; it does not say when handles close.

**What P2 cannot do:** overlap the master render with the track batch, overlap two
bridge calls, or start analysis before the first target is provably closed. The global
bridge lock and REAPER's main-thread render are hard walls.

### 4.4 Tier 3 — exact DSP work (no approximation)

| Stage | Measured share | Exact change | Expected |
|---|---|---|---|
| Temporal cells (3,005 Python iterations, 36 features) | 31.1 s of 72.4 s in the 42-asset stage profile; 10.3 s `analyze_cell` | Batch 3,005 windows into one `(3005, 4096, 2)` rfft; vectorize per-cell scalars; preserve `round()` and field order | 2–4× |
| Loudness | 17.9 s (25 %) | Keep one K-weighting pass; remove the redundant `pyloudnorm` fallback; direct-sum underflow blocks (also fixes 0.1) | 1.2–1.5× |
| Transients | 9.0 s (13 %) | Single STFT; band-limited flux on the fly; blockwise windows | 1.3–1.8× |
| True peak | 7.5 s (10 %) | Block the polyphase FIR; avoid 2× full-length temporaries | 1.2× |
| Spectrogram / events | 6.5 s (9 %) | One batched rfft for both channels; block the envelope loop | 1.5–2× |
| Full-buffer passes | — | Reuse one `abs()`; one `corrcoef` | ~0.4 s/asset |

**Combined: DSP 3011.6 → 1900–2150 CPU-s (1.4–1.6×), pool 240–270 s at 8 workers.**
This is consistent with the measured 1.72× on one file and the 1.30× actually observed
in the live capture (536.8 → 418.2 s), because the live mix has more silent/sparse
material where the earlier wins are smaller.

**P3 total on top of P2: 285–330 s (central ~305), −373…−418 s.** Effort 6–9 days.
Risk medium: every field needs a parity gate; no approximation is permitted.

### 4.5 Tier 4 — radical alternatives, ranked but not recommended first

| Design | Idea | Why not now |
|---|---|---|
| **R1 schema v2 sidecars** | Move the four dense arrays (6.42 MB + 2.18 MB + 0.32 MB per asset) to length-prefixed float64/int32 sidecars referenced by `{offset,length,dtype,sha256}`. Record 445 MB → ~60 MB. | Mechanical, high confidence, but only −10…−20 s after P1; needs a v1 reader and migration (2–3 days). Do after P1 if read latency still matters. |
| **R2 GPU (V100)** | cuFFT/FP64 for STFT, spectrogram, true peak. | Transfer is negligible (212 MB/asset ≈ 17 ms at PCIe Gen3), but the dominant stages are Python-shaped (3,005 tiny transforms + ~50 NumPy calls/cell) or sequential IIR (K-weighting). FP64 is mandatory for parity and halves the V100's FP32 rate. Best case ~230 s only after P3 has already vectorized the same FFTs; 5–10 days, high risk, shared 16 GB card. **Reject from the critical path.** |
| **R3 cross-iteration pipelining** | Render capture N+1 while analyzing capture N. | Only legal if both captures bind the same revision and the project is untouched between them; a normal mix iteration edits the project between captures. Not a default; possible later for batch A/B sweeps on a frozen revision. |
| **R4 render queue / second instance** | Use REAPER's render queue or a headless instance as a render farm. | Render queue renders saved RPP state and auto-saves the project (project mutation), has no completion API, and moves completion to a window a script cannot observe. A second instance doubles project/revision ambiguity. Reject for the interactive path. |
| **R5 fewer/larger assets** | Analyze buses instead of all tracks. | Forbidden by the brief: every track, full metrics, no subset. |

### 4.6 Decision table

| Design | Wall (s) | Δ vs 703 | Effort | Correctness risk | Peak RSS | Dependencies | Evidence confidence |
|---|---:|---:|---|---|---|---|---|
| P0 correctness/observability | ~690–700 | −3…−13 | 2–3 d | **Low** (restores reference) | same | none | High for defects; timers new |
| P1 streaming packaging | 590–620 | −83…−113 | 1–2 d | Low (byte-identical gate) | ↓ ~2 GB | none | Medium-high (seal extrapolated) |
| P2a master-first + 4 batches | 500–530 | −173…−203 | +2 d | Medium (batch render semantics) | unchanged | A: E4, subset API | Medium |
| P2b per-file streaming | 440–480 | −223…−263 | +3 d | Medium | unchanged | A: E3, C1–C7 | Medium-low |
| P3 exact DSP | **285–330** | **−373…−418** | 6–9 d | Medium (per-field parity) | ↓ | P1/P2 | Medium |
| R1 schema v2 | −10…−20 after P1 | — | 2–3 d | Medium (reader/migration) | ↓↓ | P1 | High (mechanical) |
| R2 GPU | ~230 best case after P3 | −55…−75 incremental | 5–10 d | High (FP64 parity, VRAM) | VRAM risk | P3 | Low |
| R3 cross-iteration | throughput only | — | 3–5 d | High (revision ambiguity) | unchanged | frozen revision | Low |

Rows are **cumulative designs, not additive savings**: P1 includes P0's hash/RPC
savings, P2a includes P1, P3 includes P2. The P2a range excludes the ~15 s activity
folding described in §4.3; with it, expect ~485–515 s.

**Best attainable without approximating or dropping metrics: ~230–250 s per complete
iteration** (DSP at ~2×, packaging ~25–36 s, render hidden), i.e. 2.8–3.0× today.
The recommended, evidence-backed design is **P0 + P1 + P2a (P2b if E3 passes) + P3**,
at **285–330 s p50 (2.1–2.5×)**, with a measured floor near 230–250 s.

---

## 5. Recommended architecture

**One sentence:** keep REAPER as the only renderer, make packaging a single streaming
write instead of a 445 MB freeze-and-dump, feed one long-lived DSP pool batch by batch
while REAPER renders the next batch, and make the DSP itself cheaper with exact
vectorization — in that order, behind a fail-closed parity gate.

```
                    ┌──────────────────────────────────────────────┐
  snapshot+digest   │  REAPER main thread                          │
  (0.15 s) ────────▶│  master render (9.5 s) → gate → classify     │
                    │  batch 1 render (≈25 s)                      │
                    │  batch 2 render ──────────────┐              │
                    │  batch 3 render               │              │
                    │  batch 4 render               │              │
                    └───────────────────────────────┼──────────────┘
                                                    │ one RPC at a time
                    ┌───────────────────────────────▼──────────────┐
                    │  DSP pool (8–10 spawn workers, one per run)   │
                    │  batch 1 tasks ∥ batch 2 render               │
                    │  batch 2 tasks ∥ batch 3 render  ...          │
                    │  longest-first, bounded admission             │
                    └───────────────────────────────┬──────────────┘
                                                    │ canonical fragments
                    ┌───────────────────────────────▼──────────────┐
                    │  streaming record writer (one hash, one dump) │
                    │  asset copy to audio/sha256/<sha>.wav          │
                    │  panorama → validate → response (ok+capture_id)│
                    └──────────────────────────────────────────────┘
```

**Why this is the fastest complete/correct design:**

- It removes the 150 s packaging tail, which is the largest single removable block and
  the only one with *no* dependency on undocumented REAPER behaviour.
- It hides almost all of the 108 s render behind DSP that must happen anyway, without
  changing a single render setting or metric.
- It attacks the DSP with exact transforms, not approximations, and stops at the
  memory-bandwidth wall instead of adding workers that would OOM.
- It leaves the master, all 42 tracks, all metrics, the stage labels, the windows, and
  the revision/provenance contract intact.
- It does not rely on GPU, caching, sample-rate reduction, or a track subset.

**Why not the alternatives:** GPU attacks the same FFTs P3 already vectorizes (and is
1.3× for a rewrite); schema v2 is a read-latency optimization worth only 10–20 s after
P1; render queue/instance farms mutate the project or split revision identity.

---

## 6. Implementation sequence and acceptance checks

No hardcoded musical workflow: batches are cut by stable track GUID order, worker count
is derived from measured per-worker RSS and available RAM, targets come from
`RENDER_TARGETS`, and no code assumes a track count, instrument name, genre, or the
user's four-guitar layout.

### Phase 0 — Correctness gate (must pass before timing work)

1. Fix the `short_term_lufs` underflow path so zero-energy blocks match the reference
   exactly (direct summation for underflowed prefix sums; keep the −70 LUFS gate).
2. Make post-commit failures warnings: wrap the `finally` mute restore, panorama load,
   and `_jsonable`; persist the response before cleanup; always return `capture_id` and
   `record_path` if a record was committed.
3. Add `seal_s`, `archive_s`, `panorama_s`, `response_s`, `finally_s`, `restore_s` to
   the record's additive timings block.
4. Implement C1–C7 (expected path, share-0 exclusive-open closure ×3 at 250 ms,
   RIFF/`data` length, frames, rate/channels, two hashes, mtime window); persist the
   intended target list in the job manifest **before** the render; quarantine failures
   under `<job>/rejected/`, never delete.
5. One SHA pass per WAV; skip no-op mute RPCs.
6. Make the master gate fail closed when its only "source is audible" evidence comes
   through the master chain; require a pre-master tap or an independent witness.

**Acceptance:** 43/43 assets pass C1–C7; the silent-tail parity diff is 0 cells above
0.1 dB and the max is ≤1e-9; an induced post-commit failure still returns `ok=true`,
`capture_id`, and a readable record; an injected master-chain mute fails the gate (or is
explained by an independent pre-master witness); stage timers sum to wall within 1 %; temp workspace
removed after every run.

### Phase 1 — Packaging (P1)

Streaming record writer, copy-before-seal, single hash, longest-first scheduling,
memory-derived worker cap.

**Acceptance:** p50 ≤620 s, p95 ≤1.15×p50 over 5 runs; the new record's `analysis`
bytes are identical to the old record's for unchanged DSP; `integrity_sha256` verifies
on read; peak tree RSS ≤40 GB at 10 workers; SHA passes per WAV = 2; `ok=true` 5/5;
`job-*` count unchanged after each run.

### Phase 2 — Overlap (P2a, then P2b if E3 passes)

Master first; additive `track_subset` render parameter; 4 bounded batches; one pool fed
batch by batch; rate-limit during render if contention >5 %.

**Preconditions:** E4 (batch-split PCM parity) for P2a; E3 (progressive closure)
additionally for P2b.
**Acceptance:** p50 ≤480 s (P2b) or ≤530 s (P2a); per-track PCM equal to the
single-batch reference (or within the measured native-vs-native floor); master PCM
≤1.2e-7 FS; events byte-identical; no bridge RPC during a render; admitted ≤W,
queued ≤2W; peak RSS unchanged; 5/5 clean responses.

### Phase 3 — Exact DSP (P3)

Batch the 3,005 temporal windows; one batched rfft per channel pair; vectorize per-cell
scalars with the same rounding; remove the redundant loudness fallback; block the
transient/spectrogram/event loops.

**Acceptance:** p50 ≤330 s, p95 ≤380 s; `band_level_time_dbfs` non-master exact, master
≤1e-3 dB; `rms_400ms` non-master exact, master ≤1e-6 dB; `short_term_lufs` exact after
the Phase 0 fix; integrated/LRA ≤1e-9; events byte-identical 43/43; peak RSS ≤32 GB at
8 workers; DSP 1.4–1.6× on the same 43-asset set.

### Phase 4 — Optional, only after Phase 3 is measured

- Schema v2 sidecars if record read latency still matters (byte-identical values).
- GPU spike only if Phase 3 stalls; FP64, one process, explicit VRAM budget; reject if
  end-to-end gain <10 %.
- Cross-iteration pipelining only for frozen-revision sweeps, never for a normal mix
  iteration.

### Benchmark protocol (all phases)

Same project revision and path, same 43 source GUIDs, same 0–300.52 s window, same
13,252,932 frames / 44,100 Hz / 2 ch / 24-bit per asset, same stage strings, exactly one
master, full master gate, `coverage` all true, `missing==[]`, `integrity_sha256` and
panorama binding valid. Report WAV-container SHA separately from decoded-PCM SHA. One
warm-up discarded; 5 measured runs; p50 and p95; no outlier deletion; a run is invalid
if another capture/render was active or a stage timer is missing.

---

## 7. Unanswered questions and experiments needed before coding

| # | Question | Experiment | Owner |
|---|---|---|---|
| 1 | Does 42230 *guarantee* blocking? | E1: deferred-script tick count during a 2-track render; 3/3 pass | A |
| 2 | Does REAPER close targets progressively or preallocate? | E3: 100 ms `stat` + share-0 probe over 4 targets; first-openable time vs RPC return | A |
| 3 | Is a 4-batch split PCM-neutral per track? | E4: one batch vs 4 batches, decoded PCM + events + metrics | A |
| 4 | Does the `finally` mute loop drift the revision or lose the response? | E5: live `change_count` vs `revision_after_restore`; induced RPC failure | A |
| 5 | Is the 150 s tail really seal 40–84 s + archive 30–45 s? | Phase 0.3 timers; repeat on a 20 s and a 300 s capture | B |
| 6 | Is the master 1.19e-7 FS render nondeterminism? | Two consecutive master renders in one session, decoded-PCM diff | B |
| 7 | Are all 42 source PCMs bit-identical? | Chunk inventory + decoded float64 SHA for 43 pairs | B |
| 8 | Is 8 workers optimal for a mixed 43-asset set? | Replay the sealed WAVs at 4/6/8/10/12 workers, 3 repeats, per-asset durations | B |
| 9 | Why do `f0_hz` cells reach 166 kHz and `pitch_jitter_pct` 430 %? | Inspect the estimator on a known 440 Hz tone and on a dense stem; treat as a metric-quality defect, not a parity gate | B |
| 10 | Does the `Program 1` FX chunk change behaviour? | Load both chunks in the same plugin, compare parameter values and a rendered null | B |
| 11 | Does `$tracknumber` always equal index+1? | Render a project with a renamed/reordered track and check GUID mapping | A |
| 12 | Is `RENDER_STATS` schema stable enough to key on? | Capture the raw string before/after renders on 7.84 and 7.85 | A |
| 13 | Does the `&(16<<16)` FX-init delay eliminate the silent master? | E7: 5 cold starts with/without the bit | A |

Until 1–7 and 13 are answered, the P2 overlap estimates and the "exact" claim remain
conditional.

---

## 8. Disagreements, unresolved items, and what cannot be concluded

**Resolved disagreements**

1. **Parity attribution.** A inferred the 337,134 numeric diffs were path-string
   contamination; B measured a real silent-tail floor change (2,824 cells, 23 assets,
   max 3033.8 dB) plus master-only drift. Independent recomputation confirms B. A's
   conclusion "do not certify equivalence" is correct; its mechanism is not.
2. **`_jsonable` as a post-archive failure path.** B lists two paths; A lists three.
   A is more complete (the tool calls `_jsonable` at `mcp_server.py:1725` inside the
   `_guard`-wrapped function).
3. **D2 savings.** A's 81 s at N=4 is render-only on the current 703 s baseline; B's
   ~70–75 s is incremental on top of P1. They are consistent once the baselines are
   separated.
4. **Master sample rate.** A is right: 44.1 kHz is the project rate, not a divergence.

**Unresolved**

- Exact split of the 149.9 s tail (no timers exist yet).
- Whether within-batch per-file completion is observable (E3).
- Whether batch splitting is PCM-neutral (E4).
- Whether the master's 1.19e-7 FS is nondeterminism or a real chain change.
- Whether the container SHA differences are metadata-only (all-43 chunk/PCM proof
  missing).
- Whether 8 workers is optimal for the mixed asset set (only identical assets scaled).
- The `f0_hz`/`pitch_jitter_pct` metric-quality defect (values up to 166 kHz / 430 %).
- The specific post-archive exception that killed the 703 s response.

**What cannot be concluded**

- That `Main_OnCommandEx(42230)` is contractually blocking; it is measured and
  third-party corroborated but undocumented. Any design must assert a duration floor
  and treat a fast return as an error.
- That the optimized run is equivalent to the baseline. It is structurally identical,
  event-identical 43/43, and numerically identical for the 42 non-master tracks on
  band/RMS/metrics — except for 2,824 `short_term_lufs` cells in 23 tracks' silent
  tails — and it differs in the master PCM.
- That the 285–330 s estimate is guaranteed. It assumes P1's byte-identical streaming
  writer, E4's batch neutrality, and P3's 1.4–1.6× DSP gain; each has a gate.
- That GPU would help. No GPU stage has been timed; the best case is a 1.3× increment
  after P3 for a rewrite of the same FFTs.

---

## 9. Next engineering decision to review

**Approve Phase 0 + Phase 1, not Phase 2 yet.**

Rationale: Phase 0 fixes a published-metric defect and a response-loss defect that
make every benchmark untrustworthy; Phase 1 removes ~100 s with no dependency on
undocumented REAPER behaviour and a byte-identical acceptance gate. Phase 2's ~70–75 s
incremental gain should not be authorized until E3 (progressive closure) and E4
(batch-split PCM neutrality) are measured on a disposable project copy, because a
failed E4 would silently change what "via master" contains for every track. Phase 3
should be scoped only after Phase 1's timers show the real packaging split and Phase 2's
measured overlap efficiency.

The review question: **do we accept the Phase 0 parity/response gate as a blocker, and
authorize the Phase 1 streaming-packaging implementation against the p50 ≤620 s /
byte-identical-record acceptance checks?**
