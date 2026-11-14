# Workstream B — events, editing, FX and grouping against the current tree

## 1. Header

**Model / provider / reasoning:** `deepseek/deepseek-v4.1-flash-1017` via RouterAI (custom provider
`routerai.ru`), Ultra reasoning.
**Date:** 2026-11-13.
**Scope:** onset/note/performance events, repeated structure, tempo and grid hypotheses,
time-scale modification, FX optimization/control, panning and grouping, the bridge/MCP action
contract, and the performance-profiler reconciliation. Masking/clarity metrics, auditory texture,
separation quality, reference-analysis methods and corpus selection belong to Workstream A and are
only cited where an action depends on them.

**Snapshot note.** “Current tree” here is the pre-implementation dirty tree read on 2026-11-13,
before the separate relation/capture follow-up. The later implementation appendix in
`INTEGRATED-DECISIONS.md` §9.5 is not evidence for this audit and does not retroactively change
its findings; historical observations remain identifiable as pre-implementation.

**Method.** Document-only. I read the attached Russian list as data, the four round-5 documents, the
five round-6 documents (synthesis, Agent B, directive, delta addendum, clean-session log), the six
product docs and the four performance documents, then grepped and read the current source for every
symbol cited. **I ran nothing**: no pytest, REAPER, render, benchmark, install, model call or MCP
tool. Every timing below comes from a sealed artifact or a document that measured it. The dirty
worktree was treated as read-only.

**Access limitations.** (i) I re-verified five primary sources this round (Gong & Serra 2018; ST-ITO;
ITO-Master; Driedger & Müller 2016; Ronan et al. 2017) and the in-repo REAPER v7.85 API catalogue.
Round-5's full-text findings for Dannenberg 2007, Degara 2011, Kehling 2014 and Mounir 2021 are reused
**as Round-5 findings** and labelled accordingly. (ii) No live REAPER, so fade shape, group sync,
stretch-marker save/reload and MIDI-on-loop semantics remain unverified. (iii) No capture was run, so
the current overlap implementation has no wall-clock figure; the 703 s baseline predates it.

---

## 2. Reconciliation: prior B claims against the tree as it is now

Verdicts: **KEEP** = still true and still a requirement; **REVISE** = partly true or true in a
different place than stated; **DROP** = superseded or contradicted by the current tree.

| # | Claim (source) | Current-tree evidence | Verdict |
|---|---|---|---|
| 1 | D5-01: no absolute perceptual threshold in method code; gates relative or named policy keys | `analysis/temporal.py:803` `masking(tracks, min_db=10.0, ...)` still applies a fixed 10 dB band-dominance cut; `mix_evidence.py:34` still defaults `masking_db=6.0`. No policy-key mechanism exists in the method path. | **REVISE**: the decision is right, the implementation is absent |
| 2 | D5-02: rename `masking`→`band_dominance`, carry `pan_relation`, `broadband_delta_db`, `active_fraction`, `is_psychoacoustic_threshold:false` | Only the consumer's `id` was renamed (`mix_evidence.py:49`). The producer still returns `masking_evidence.pairs` with `masker`/`target`; no pan relation, no broadband delta, no active fraction, no threshold flag. | **KEEP as open**; half-done |
| 3 | D5-03: delete the unreachable `masking_db=6.0` path | Still present and still unreachable: `masking()` cannot emit a pair below `min_db=10.0`. | **KEEP** |
| 4 | D5-04/D5-05: reconstruction-gated partial loudness, raw shares only | Implemented and pinned by `tests/test_partial_loudness_gate.py` (A confirms independently). | **DROP from B's scope** (A owns it) |
| 5 | D5-08: `item.edge` writes `D_STARTOFFS` | **MISSING.** `D_STARTOFFS` is read (`:381`, `:755`) and copied (`:3639`) but never written; `normalize_edit_operation` has no `item.edge`; `validate_item_values` has no offset, fade or channel key. | **KEEP** |
| 6 | D5-09: full geometry readback plus `expected_after` per operation | `read_edit_state_after` (`:3086`) returns `{ item_count }` for `item.split`; `EditPlan` (`daw/session.py:70`) has `revision`, `operations`, `reason`, `plan_id`, `before` — no `expected_after`. | **KEEP** |
| 7 | D5-10: action path enforces `UsageScope` | `grep -rn usage_scope src/reaper_audio` → only `analysis/event_model.py`. No planner, bridge call or MCP handler reads it. | **KEEP** |
| 8 | D5-11: split the two alignment meanings | `daw/alignment.py:12` still exports `propose_moves(..., strength=1.0)`; `daw/align.py` is a separate reference-alignment module. No rename, no deprecation, no explicit intent argument. | **KEEP** |
| 9 | D5-12: FX parameter fitting is bounded search in a user-supplied chain | No `fx_search.py`. Only `daw/eq.py` calibration (parameter grid, no renders) and `daw/limiter.py` bisection (≤34 renders). | **KEEP** |
| 10 | D5-14: panorama carries edit-critical item fields | `panorama.py:_item_views` (`:178-221`) emits id/guid/track/position/length/take_count only — no `start_offset_s`, `playrate`, markers, fades, `group_id`, `channel_mode`. | **KEEP** |
| 11 | Transient events are discarded | `analysis/metrics.py:408` runs `detect_transients`; only `:461-462` density/count and `:520` coverage reach the report. Event times/strengths are dropped. | **KEEP** |
| 12 | `event_model.py` is a contract island | `grep event_model src/` → only the module itself; `tests/test_ids_event_model.py` and `tests/test_contract_integration.py` are the only other referrers. `src/reaper_audio/rhythm/` exists but contains **no files**. | **KEEP**; worse than stated — the rhythm package is an empty directory |
| 13 | Two operation caps (200 cuts vs 256 operations) | `daw/editing.py:15` `MAX_CUTS_PER_TRANSACTION = 200`; `session.plan` rejects `len > 256` (`session.py:1707`); the Lua preview rejects `#operations > 256` (`:3195`). | **KEEP** |
| 14 | "19/21 MCP tools" | `grep -c '@server.tool()'` = 29; `grep -c '@server.resource'` = 6. | **DROP**; both older counts are stale |
| 15 | `edit_map` hard-codes 120 BPM and guesses roles | `tempo_grid` still falls back to 120 BPM; `_group()` still maps kick/snare→drums, guitar/amp→guitars. `reaper_edit_map` now defaults `decide=False` with caller `source_groups`, so the guess is off the default path — but `tempo_grid` still supplies beat positions to evidence rows. | **REVISE** |
| 16 | Preview shows the before state and the requested operations only | `transaction.preview` returns `before[]` with `changes_state` per operation; `EditPlan` has no expected-after; `session.plan` stores only `before`. | **KEEP** |
| 17 | Round-6: panorama lifecycle is implemented | `context/lifecycle.py` + 8 MCP tools: `reaper_panorama_open/page/ack/status/changes_since/delta_ack/refresh/slice`; `DEFAULT_PAGE_BYTES = 24_576` (`:34`). | **KEEP** (verified) |
| 18 | Round-6: every mutation is gated by a complete receipt and exact revision | `mcp_server.py:85` `_require_panorama_receipt`; applied to `reaper_edit` apply/undo (`:952`), `reaper_api_call` (`:1089`), `reaper_action` (`:1154`), `reaper_save`. Internal read-only helpers bypass via a thread-local. | **KEEP** (verified) |
| 19 | Round-6: deltas require an exact base | `changes_since` requires `base_capture_id` (or a context token), verifies `base_fact_root`, and returns `stale_base/base_not_in_archive` for a cross-archive base. | **KEEP** |
| 20 | Round-6: two edit→delta→ack cycles, no refetch | **Not implemented as stated.** `changes_since` diffs **captures**; an edit without a new capture returns `not_modified`, and the token stays on the pre-edit revision. `reaper_edit` apply emits no `chg_` record and no invalidations. The measured run recaptured between edits (`change_count` 164→213). | **REVISE — blocking** |
| 21 | Round-6 D6-11: invalidation follows `render_key` | `grep -rn render_key src` → no implementation (the only hit is a local variable named `render_keys` over render settings in `session.py:1008`). | **DROP as implemented**; keep as design |
| 22 | Round-6 D6-13: full document carries method/version/stage/window/units | Partly: non-compact view has `method{name,decoder,stage}` and `measured_window` (`:1183-1189`); the default compact row is a 9-value array with shared units and no per-row window/stage. | **REVISE** |
| 23 | Round-6: revisions carry `instance_id`/`epoch` and are compared | `revision.py` has them at `:155-198`, but `lifecycle._normalise_revision` compares only project_id/change_count/path; receipts and deltas are blind to instance and epoch. | **REVISE** |
| 24 | Round-6 §6.1: `fact_root` is a Merkle root over entity facts | `lifecycle.fact_root` = `sha256(JCS(stable_fact_view(document)))` — a document hash with volatile ids removed, not an entity journal. | **REVISE** |
| 25 | Round-5 Step 4: seam metrics become relative (before/after) | `analysis/seam.py:61` `seam_metrics(before, after, sample_rate)` already compares two arrays; `seam_search.search_variants` still minimizes an absolute weighted score (`level/6 + step/0.1 + flux`). No pre/post-edit project comparison exists. | **KEEP** |
| 26 | Round-5: `right_trim_s` is a delay, not a reveal | Confirmed in `apply_edit` `item.split`: only `D_POSITION` is set for the right item; `D_STARTOFFS` is untouched. | **KEEP** |

**What this table changes about the plan.** None of the action-layer decisions (5–10, 25) was
implemented; the context-layer ones (17–19) were. The tree moved furthest where the previous round's
*contract* work was, and least where an edit has to touch audio geometry. The new blocking defect is
row 20: continuity is implemented against captures, so "edit → factual delta → next edit" still costs
a full recapture.

---

## 3. Current contract audit

### 3.1 Bridge surface and transaction operations

`scripts/reaper_bridge.lua` (4,231 lines) dispatches by method name at `:4109-4111` for
`transaction.preview/apply/undo` and exposes the rest through a method table. The transactional
operation set, from `normalize_edit_operation` (`:2774-2840`), is exactly:

`track.set`, `track.create`, `track.delete`, `track.reorder`, `item.set`, `item.create`,
`item.delete`, `item.split`, `item.duplicate`, `take.set`, `midi.create`, `fx.set_enabled`,
`fx.set_parameter`, `fx.insert`, `fx.delete`, `fx.move`, `fx.get_chunk`, `fx.set_preset`,
`fx.offline`, `send.set`, `send.create`, `send.delete`, `marker.create`, `region.delete`,
`envelope.set`, `tempo.set`.

There is **no** `item.edge`, no `item.set start_offset_s`, no fade-length setter, no channel-mode
setter, no group operation, no folder-depth operation, no render/audition operation, and no
"render candidate" operation. `item.set` accepts `position_s`, `length_s`, `mute`, `stretch_ratio`,
`preserve_pitch`, `stretch_markers`, `beat_attachmode`, `auto_stretch`
(`validate_item_values`, `:2334-2376`). The REAPER v7.85 catalogue in the repo
(`src/reaper_audio/reaper/api_catalog.json`, source `reascripthelp.html`, sha256 `eb2e8fae…`,
732 functions) documents `D_STARTOFFS` as `double *` on
`Get/SetMediaItemTakeInfo_Value` — i.e. writable — and documents `D_FADEINLEN`, `D_FADEOUTLEN`,
`C_FADEINSHAPE`, `D_FADEINDIR_NEW`, `D_FADEINDIR2_NEW`, `GetTakeStretchMarker` and
`SetTakeStretchMarker`. So the missing geometry operations are missing in **our** layer, not in
REAPER.

**Transport.** `bridge_client.py` is a file-mailbox client with a cross-process lock
(`client.lock`, `:201`, stale 300 s, owner-token release `:231`). One call holds it; the Lua handler
runs synchronously on REAPER's main thread. This is the hard wall behind every profiler concurrency claim.

### 3.2 Preview / apply / undo semantics

`ProjectSession.plan` (`daw/session.py:1703`) snapshots, builds a `ProjectRevision`
(`project_id`, `change_count`, `path`) and calls `transaction.preview`, which normalizes every
operation, resolves every GUID, and returns `before[i] = {operation_index, kind, target_guid,
values, changes_state}`. The plan id is cached in Python and in Lua (`transaction_plans`, TTL 900 s,
max 128 entries). `apply` (`:1772`) re-checks the revision, calls `transaction.apply`, which does
the following:

refuses an expired/unknown plan; re-resolves each GUID; wraps everything in one
`Undo_BeginBlock2`/`Undo_EndBlock2`; on exception attempts `Undo_DoUndo2` and, if the label is not on
top and the counter moved, fails with `transaction_rollback_failed` (`:3860-3882`); and returns
`revision_before`, `revision_after`, `before`, `after[]`, `operation_results`, `undo_label`.

`undo` requires the exact post-apply revision and that the stored label is on top; the Python layer
also holds `applied_revision` per plan so a second apply is refused.

**What is guaranteed:** one undo block per plan; stale revision rejected at preview and at apply;
objects resolved by GUID; deletes verified absent; per-operation readback for most operations. The
exception is `item.split`: its *before* state carries position, length and item count (`:2990-3001`),
but `read_edit_state_after` special-cases split to `{item_count}` (`:3103-3106`), so the after state is
strictly less informative than the before state.

**What is not guaranteed and matters for musical edits:** (a) no `expected_after`, so the caller
cannot see that a 69 ms overlap becomes a 69 ms fade before committing; (b) no geometry readback for
split or edge, so post-change verification is impossible for the operation the owner actually needs;
(c) no group atomicity — `I_GROUPID` is captured but the transaction targets one item; (d) no digest
comparison before/after, so routing/PDC side effects are not checked; (e) fade **shape** is never
set, and `C_FADEINSHAPE`/`D_FADEINDIR*` are absent from both the plan and the readback.

### 3.3 Revision and panorama gating

Two revision notions coexist and do not compose:

- `daw/session.ProjectRevision`: `{project_id, change_count, path}`. Used by plans, apply, undo and
  the bridge's `assert_revision`.
- `context/revision.py`'s richer record with `instance_id`/`epoch` (used by `DigestEvidence`), and
  `context/lifecycle.py`'s `fact_root`/`document_hash` on top of the **document**.

`lifecycle.require_receipt` (`:1030`) accepts either a `ct_` context token or a `pr_` receipt and
checks `project_id` plus a normalised revision equality. Because `_normalise_revision` drops
instance/epoch, a receipt cannot distinguish "same content, different instance" from "same
instance". `fact_root` is computed from `stable_fact_view(document)`, which strips volatile ids
(`fx_id`, `audio_id`, `analysis_id`, `sha256`, capture revision fields, record paths) — a pragmatic
choice that makes recaptures comparable, but it also means the root is a hash of the *presentation*,
not of a replicated entity journal. Round-6's Merkle-root design remains unimplemented.

The gate is therefore **two-key** (complete receipt + exact `change_count`), not a content-root gate:
adequate for "the model has seen this revision", not for "the facts it saw are the facts on disk".

### 3.4 Event model schema (present, unused)

`analysis/event_model.py` (843 lines) defines:

| Type | Fields that matter for action | Constraint |
|---|---|---|
| `UsageScope` (`:162`) | `timing`, `pitch`, `duration`, `midi`, `destructive_cut` (all bool, default only `timing=True`) | No consumer |
| `Confidence` (`:200`) | `basis ∈ {interval, rank, agreement, none}`, `value ∈ [0,1]`, `n`, `calibration_id` | Basis is mandatory; value may be null |
| `MethodRef` (`:247`) | `name`, version, params hash | — |
| `SignalEvent` (`:452`) | `asset_id` (`ast_sha256…`), `Interval`, `kind ∈ {onset, transient, spike, clip_run, silence, decay}`, `strength`, `status` | Asset-bound; cannot express a cross-track event |
| `PerformanceOccurrence` (`:511`) | item/take GUID, `loop_iteration`, `source_interval` | Separates source time from project time |
| `PerformanceEvent` (`:573`) | kind (`pick_attack`, `drum_strike`, `note`, `chord`), `pitch` + alternatives, `EventDuration`, `usage_scope`, `confidence`, `evidence[]`, `status` | The only type that carries authority |
| `PerceivedGesture` (`:696`) | perceptual grouping | No producer |
| `ScoreObject` (`:758`) | MIDI/score intent, `origin=project_midi` | No consumer |

`Confidence.basis` is the best idea in the file and the only thing that can make authority
mechanical: `agreement` with `n` is the only basis that can ever clear a destructive edit;
`interval` and `rank` are ordering signals; `none` must be treated as absent. Nothing enforces this
today. The producers that exist (`events.detect_events`, `transients.detect_transients`) return
plain dicts with no basis, no method ref and no asset id.

### 3.5 Analysis pipeline, policy and batching (current)

`FullCaptureService.capture` (`daw/full_capture.py:421`) now runs:

snapshot + digest; **master render first**, validated and classified (`:547-576`); create
`BoundedAnalysisPool(workers)` and submit the master (`:578-584`); plan batches with
`plan_capture_batches` (folder subtrees atomic, singleton remainders merged, no one-track batch on a
multi-track project, cross-batch sends reported, fallback to a full-project render on unstable
topology); per batch: render → admit (expected GUIDs, header and content hashes) → submit → next
render (`:597-624`); serial `_track_activity` decode of all WAVs (`:634-644`); master silence
classification; wait for analysis, seal, archive, panorama, respond.

Worker policy is one module now (`daw/analysis_policy.py`): hard cap `MAX_ANALYSIS_WORKERS = 8`,
short-window default 8, long/unknown default 4, memory envelope 384 MB + 14 MB/s capped at 70 % of
available RAM, with a typed refusal when the envelope cannot support one worker. This supersedes the
performance documents' `MAX_ANALYSIS_WORKERS = 16` and their memory-derived cap of 10; **the tree is
authoritative and the documents are stale on this point.**

Stage accounting (`timings`, `:777-800`) covers snapshot, digest, mute RPCs, render, batches,
source activity, master, per-track times, worker policy and restore, plus the overlap fields
(`batch_plan`, `batch_admissions`, `batch_render_ends_s`). It does **not** cover sealing, archive copy,
panorama build, response serialisation or `finally`; `archive_s`/`total_s` reach the response only
(`:879-882`), not the sealed record — exactly the gap the profiler identified.

### 3.6 MISSING, and where each belongs

| Missing capability | Where it belongs | Why it is not cosmetic |
|---|---|---|
| `SourceMap` (item/take/source/offset/playrate/markers/loop/group/channel/timebase) | new `daw/source_map.py`, fed by `item_context` | Without it no edge edit is computable and no post-edit readback is comparable |
| `item.edge` operation | `normalize_edit_operation` + `validate_item_values` + `apply_edit` + `read_edit_state_after` + `EditOperation` callers | The owner's acceptance case is an edge reveal |
| `expected_after` | `daw/session.EditPlan` + `transaction.preview` return | Preview cannot show consequence |
| Geometry readback for split/edge | `read_edit_state_after` | "Verify afterward" is false for splits today |
| `render_candidate(plan, window)` | `daw/session.py` (apply-in-undo-block → render → undo, or declared copy) | Every verification, FX search and A/B loop degrades without it |
| Post-apply factual delta + invalidations | `context/lifecycle.py` (`chg_` record) + `mcp_server.reaper_edit` | Continuity currently needs a capture |
| `fx_search` with budget/prior/top-k | new `daw/fx_search.py`, generalising `limiter._search_threshold` | FX optimization is 2 narrow tools today |
| Meter/downbeat hypothesis | `analysis/tempo.py` + a `GridHypothesis` record | Grid rows currently have no meter |
| Repeat/section relation | `analysis/edit_map._sections` or a new `analysis/sections.py` | Foote boundaries exist; identity does not |
| Group-aware edit | transaction planner | Silent partial group edits are the worst failure mode |

---

## 4. Profiler reconciliation

### 4.1 What the sealed baseline actually measured

From `docs/PERFORMANCE-PIPELINE-RESEARCH.md` (quoting sealed capture
`cap_01M7A7ZV8VWK4SA8FHGRC9Q04S`): `elapsed_wall_s=703.072`, `capture_render_s=134.851`,
`render_batch_s=98.470`, `render_master_s=9.505`, `source_activity_s=17.767`,
`analysis_wall_s=418.177`, `analysis_pool_wall_s=408.274`, `dsp_worker_elapsed_sum_s=3011.645`,
`wav_decode_worker_elapsed_sum_s=16.552`, 42 tracks + master, 43 assets, 8 workers. The derived
split is: DSP 408.3 s (58.1 %), render 108.0 s (15.4 %), other in-render work 26.9 s (3.8 %),
post-pool record loop 9.9 s (1.4 %), and an **untimed packaging tail of 149.9 s (21.3 %)**.

The same document records 8/12/16 workers on eight identical 300 s assets at
95.817/95.122/91.636 s with peak RSS 29.56/29.19/29.97 GB: the DSP is bandwidth-bound. That is why the
tree caps at 8.

### 4.2 Which optimizations are in the dirty tree

| Optimization | Present? | Evidence | Bottleneck it addresses |
|---|---|---|---|
| Master rendered first, classified before track work | **Yes** | `full_capture.py:547-576` | A silent master no longer costs 108 s render + 418 s analysis |
| Topology-aware batch planning | **Yes** | `capture_batching.py`; 12 tests in `tests/test_capture_batching.py` | Enables render/analysis overlap without splitting folder FX or cross-batch sends |
| Bounded analysis pool with backpressure | **Yes** | `daw/analysis_pipeline.py` `BoundedAnalysisPool`, one spawn pool per capture | Overlaps DSP with the next render RPC |
| Submit batch *k* before rendering *k+1* | **Yes** | `full_capture.py:616-624` | Hides most render time behind analysis |
| Batched mute RPC instead of 42 sequential calls | **Yes** | `_set_mutes` (`:907`), `mute_bridge_calls` timing | Removes the post-total timeout/response-loss risk |
| `short_term_lufs` underflow parity fix | **Yes** | `analysis/metrics.py:32,142-198,246-256`; `tests/test_analysis_optimizations.py:198-283` | Restores the reference value for silent-tail blocks |
| Worker policy unified and memory-capped | **Yes** | `analysis_policy.py` | Prevents the 16-worker OOM |
| Timers for render/analysis/restore/archive | **Partial** | `timings` block above | Seal/panorama/response/finally still untimed |
| Streaming record writer (single dump, one hash) | **No** | `context/full_capture.py` still builds `canonical_payload(record)` (whole 445 MB) and seals in `__post_init__` | The 40–84 s seal and 30–45 s archive remain |
| Copy-before-seal / single SHA pass per WAV | **No** | `archive.append` re-hashes every source and re-checks an existing blob | 2 hash passes per asset, plus the seal pass |
| Fold track activity into worker payload | **No** | `_track_activity` still decodes all 42 WAVs serially after the batches (`:634-644`) | ~17.8 s serial |
| Per-file streaming admission (E3) | **No** | Admission is per batch | The remaining render/analysis gap |
| `render_key` asset reuse | **No** | no `render_key` in `src/` | Every recapture re-renders and re-analyses everything |

### 4.3 What the numbers do and do not prove

They **do** prove the baseline's serial structure, the DSP share, the packaging tail's size (by
subtraction), the pool's 92.2 % efficiency, and that workers past 8 are not a throughput strategy on
this machine.

They **do not** prove the dirty tree is faster. There is no post-overlap sealed capture in the
documents I read, and I ran none. The implementation is structurally sound (bounded queue, one pool,
batch admission, exact GUID-set check) but the performance document's acceptance criteria (p50 ≤530 s
P2a, ≤480 s P2b, PCM parity, events byte-identical) are **unmet and unmeasured**. The only plausible
post-change number is A's 12 s/42-track smoke (29.812 s entrypoint, 17.37 s overlap), which is a
short-window smoke, not comparable to the 300 s baseline, and I did not run it.

### 4.4 What remains on the critical path for *this* workstream

For a text model doing musical work, the relevant cost is not only "one capture" but "one capture per
verification". Three facts compound:

1. A full capture of a 300 s, 41-track project is ~703 s in the only sealed measurement, and no
   post-overlap number exists. Even the optimistic P3 target is 285–330 s.
2. A capture **advances the project revision**: in the clean-session log, an edit took
   `change_count` 163→164 and the following capture took it to 213. Because the mutation gate
   compares the receipt/context token to the *current* revision, the post-capture token is a new
   object; any plan made before the capture is stale.
3. `changes_since` only diffs capture documents (row 20 above). So today the loop is
   **edit → full capture → delta → ack → edit**, not **edit → delta → ack → edit**.

Consequences for B's candidates: any capability whose verification requires a fresh capture inherits
~5–12 minutes per iteration on the reference project. That rules out per-onset iterative search,
per-cut audition loops over dozens of candidates, and any CMA-ES-style FX search on the full mix.
It does **not** rule out single-shot edits with geometry readback (cheap, no render) or a small
number of candidate renders on a short window if a plan-bound `render_candidate` is built.

**Cost labels.** 703.072 s, 408.274 s, 134.851 s, 98.470 s, 17.767 s, 3011.645 s, 92.2 %, 3.96–4.29 GB
per worker: **measured** (sealed record via the performance documents). 285–330 s and 440–530 s:
**estimates**. "≤34 renders" for the limiter: **by code structure**, not timed. FX-search wall-clock:
**UNMEASURED**.

---

## 5. Candidate matrix

Each block states the capability, the primary evidence (with what I verified), the exact current
mapping or MISSING, the detection method and its confidence basis, the edit authority, the
preconditions/readback/rollback, the budget, the failure modes, and one falsifiable
operation-specific acceptance test. "Authority" is always narrower than "detection": a capability can
measure something it may not act on.

### 5.1 Onset / note / performance events

**Capability.** Given a captured post-FX track asset, produce candidate attack times with a strength
and an uncertainty; cluster them across sources; expose a performance event that an editor can cite
without re-detecting; never assert a pitch or a note that was not measured.

**Primary evidence.**
- Dannenberg, *An Intelligent Multi-Track Audio Editor*, ICMC 2007
  ([CMU PDF](http://www.cs.cmu.edu/~rbd/papers/icmc2007iaed.pdf), Aug 2007). The edit abstraction is
  the triple `(T_i, F_i, D_i)` = play time, source offset, duration — exactly
  `D_POSITION`/`D_STARTOFFS`/`D_LENGTH`. It assumes monophonic pitched tracks, reports no metrics, and
  says the rhythmic "feel" is what suffers. *Round-5 full text; not re-fetched.*
- Gong & Serra, *Towards an efficient deep learning model for musical onset detection*, arXiv
  1806.06773 (Jun 2018). Abstract re-verified today: the efficient model has **28.3 %** of the
  trainable parameters of their SOTA implementation, and *"the model pre-trained on one dataset
  fails to detect onsets on another dataset"*. This is the strongest single argument against
  treating any pretrained onset model as a drop-in for distorted metal.
- Degara, Davies, Peña, Plumbley, *Onset Event Decoding…*, IEEE JSTSP 5(6), 2011. 13 excerpts,
  **551 annotated onsets**, correct within **50 ms**. *Round-5 full text.* The tolerance is the point:
  at 300 BPM a 32nd is 25 ms, so a 50 ms floor cannot separate adjacent events in blast/tremolo material.
- Kehling et al., *Automatic Tablature Transcription of Electric Guitar Recordings*, DAFx 2014.
  Onset F **98.5 %**, pitch F **0.96**, on clean standard-tuned isolated guitar — a different domain
  from high-gain amp prints. *Round-5 full text.*
- Mounir et al., *Musical note onset detection based on a spectral sparsity measure*, EURASIP JASMP
  2021. A non-neural ODF built on low-energy STFT bins; relevant because heavy compression removes
  the headroom difference-based ODFs rely on. *Round-5 full text; no numbers quoted (tables were
  images).*

**Current mapping.** `analysis/events.py:detect_events` (2 ms hop, 4 ms window, 5 dB rise out of a
3-frame dip, 12 ms refractory, −70 dB floor, duration to −15 dB capped at 150 ms, clip run merged
into one event). `analysis/transients.py:detect_transients` (STFT 2048/256, median+MAD threshold at
2.8σ, 35 ms minimum gap, per-band flux, per-channel power average so anti-phase stereo cannot cancel
attacks). `analysis/edit_map._cluster` merges across sources at 30 ms. `analysis/event_model.py`
defines the record an editor would consume, and **nothing constructs or reads it**. `rhythm/` is an
empty directory.

**Detection method and confidence.** Both detectors produce an *interval* signal: `transients` uses
`clip((strength − threshold)/(p99.5 − threshold))`, a normalised margin, but the dict carries no
`basis`, method/version or asset id. `edit_map._confidence` is a composite (0.55 agreement + 0.30
strength + 0.15 low band) with **no stated basis**, so it cannot authorize anything. No calibration
exists on this material; E1 remains unrun.

**Edit authority.** *Suggest-only* today, because no consumer exists. The rule I would enforce when
one exists: event evidence may authorize `timing`-scoped review and ordering; `destructive_cut` only
with `basis="agreement"`, `n ≥ 2`, and either `value ≥ 0.8` or an explicit caller instruction naming
the event; `midi` never from audio-only evidence (it requires `pitch=true` and a `ScoreObject` or a
measured pitch, which the current detectors do not produce).

**Preconditions / readback / rollback.** SourceMap `mapping_status="verified"` for the target item;
the event's `UsageScope`; the containing item and neighbours; group membership. After an edit:
geometry readback (**MISSING** today), seam metrics on the same window, and undo (exists). Rollback
is one undo block; a failed rollback is a typed error, not a silent state.

**Budget.** Detection is already inside the measured DSP pass (metrics runs `detect_transients`;
`edit_map` re-runs both on the same buffers). No extra render. Any *verification* render costs a
capture (see §4.4).

**Failure modes.** No dip under heavy compression; cymbal wash holding the envelope above −70 dB;
one merged `clip` event on a limited master; 30 ms clustering merging 32nds at ≥250 BPM; a double-track
flam averaged instead of marked `contested`; bleed producing a plausible attack on the wrong source;
`dur_ms` capped at 150 ms and read as a real tail.

**Falsifiable acceptance test.** (a) On 2 × 30 s of the owner's guitars, two annotators, report F1 at
±10/±25/±50 ms; below 0.9 at ±25 ms every downstream event claim inherits the error. (b) A synthetic
300 BPM 32nd click must produce **zero** clusters with more than one attack at 30 ms tolerance (or the
tolerance becomes tempo-dependent, asserted). (c) An onset-only event must not authorize
`item.split`; two agreeing sources must.

### 5.2 Repeated structure

**Capability.** Say "this interval is a recurrence of that interval" with a similarity and an
alternative; use it to navigate, to choose comparable windows, and to propose anchors. Not to claim
"same take" or "same performance".

**Primary evidence.** No primary recurrence paper was reviewed in B's scope this round, and I will
not dress an engineering choice as literature. The verified fact is the negative: `_sections`
computes Foote novelty but exposes boundaries only, so "same riff, other take" cannot be expressed.
The method is 1 s frames, 0.5 s hop, 21 z-scored features (RMS, centroid, 7 log bands, 12 chroma),
kernel `min(8, max(2, n//8))`, peaks ≥4 s apart and ≥0.5σ prominence.

**Current mapping.** Boundaries and per-section `novelty` only. No `section_id`, no `repeat_of`, no
similarity matrix exposed, no transposition-invariant matching, no cross-capture section identity.

**Detection method and confidence.** Unnormalised novelty; no confidence field. The honest basis is
`rank` (this boundary is stronger than that one), never `interval` or `agreement`.

**Edit authority.** *Suggest-only.* A repeat relation may never authorize a cut, move or replacement;
it may be cited as context and used to pick comparable evidence windows.

**Preconditions / readback / rollback.** Same source set, same capture, same stage; a cross-capture
comparison is `incomparable` until bucket anchoring is revision-stable (Round-6 §7 item 6 found
buckets anchored at `times[0]`; I did not verify a fix). No readback or rollback applies because
nothing is applied.

**Budget.** O(n²) on 1 s frames: ~90k cells for a 5-minute song, trivial CPU; the FFT pass is the
cost. No render.

**Failure modes.** Arrangement changes (a guitar layer entering) look like form changes; tempo
changes shift frame alignment; different takes of the same riff are similar but not identical;
chroma is weak under distortion and cymbal wash; novelty peaks at every fade.

**Falsifiable acceptance test.** On a synthetic AABA fixture with known boundaries, boundary F1 at
±1 s must be 1.0 and the A sections must be returned as a repeat pair with similarity above the B
pair. On real material, a transposed repeat must be returned as `similar_not_identical` with the
transposition stated, not as identical; and the tool must return `insufficient_structure` rather
than a single whole-song "section" when fewer than three boundaries are found.

### 5.3 Tempo, grid and tempo map

**Capability.** Produce a local pulse hypothesis with metrical alternatives, carry the project tempo
map as intent, and compute bar/beat positions for every window without collapsing to one BPM.

**Primary evidence.** Degara 2011 (above): rhythmic priors help when the tempo hypothesis is right
and hurt when it is wrong; the decoder must consume alternatives, not the top candidate. `docs/TZ.md`
§5 and `docs/EDITING-ACCEPTANCE.md:25` forbid blind quantization; `docs/FULL-CAPTURE.md:58-61`
forbids a single base BPM for a project with tempo changes.

**Current mapping.** `tempo.py:estimate_tempo_curve` — 8 s window, IOR quantiles ×
{0.25, 1/3, 0.5, 1, 2, 3, 4}, score 0.55 phase coherence + 0.30 timing consistency + 0.15 evidence,
alternatives within 0.04, ≥3 onsets, ≤3 alternatives. `tempo_at` returns map points.
`edit_map.tempo_grid` turns them into **bare beats** and falls back to **120 BPM**. There is no
`GridHypothesis`, no downbeat, no meter grouping, no phase offset in quarter notes.

**Detection method and confidence.** The tempo score is an `interval`-like heuristic, uncalibrated;
meter does not exist, so it has no confidence at all. `tempo.py` returns `confidence: 0.0` when it
cannot decide, which is correct behaviour and should be preserved.

**Edit authority.** The tempo map is **intent**, never performance evidence (D12). A grid is a
hypothesis. `quantize_to_grid` may only run with an explicit intent argument; `align_to_reference`
is a different operation. Today `daw/alignment.py:propose_moves(..., strength=1.0)` still defaults
to full quantization and its name collides with reference alignment — a live hazard.

**Preconditions / readback / rollback.** Revision-bound tempo map; if a plan changes tempo, every
beat-attached item (`C_BEATATTACHMODE`, `C_AUTOSTRETCH`) must be enumerated and either previewed at
its new position or the plan refused. No such check exists; `tempo.set` applies
`SetCurrentBPM`/`SetTempoTimeSigMarker` directly. Readback is the map after apply; rollback is undo.

**Budget.** Trivial CPU; one transaction operation for a tempo change; a verification capture if the
change is claimed to fix timing.

**Failure modes.** Half/double time; 3/4 vs 6/8; a tempo ramp inside one 8 s window; fewer than three
onsets in a sparse section; the 120 BPM fallback silently shifting every `offset_ms` in the evidence
rows; beat-attached items moving when an unrelated tempo edit is applied.

**Falsifiable acceptance test.** (a) On a synthetic click with a known 120→140→120 ramp, the curve
must recover each segment within ±0.5 BPM and emit the segment list, not one BPM. (b) With an empty
tempo map, every grid row must carry `grid_basis="absent"` and **no** `offset_ms`; a row containing
`offset_ms` computed against 120 BPM fails the test. (c) With a 3/4 vs 6/8 ambiguity inside the
alternative threshold, both must be returned; a single meter with no alternatives fails.

### 5.4 Time-scale modification and stretch artifacts

**Capability.** Decide whether time modification is admissible at all; when it is explicitly
requested, apply it natively and verify the artifacts the literature names.

**Primary evidence.**
- Driedger & Müller, *A Review of Time-Scale Modification of Music Signals*, Applied Sciences 6(2):57,
  **18 February 2016** ([MDPI](https://www.mdpi.com/2076-3417/6/2/57)). Re-verified: *"there is no
  single TSM method that can cope with all kinds of audio signals equally well"*. The artifact
  taxonomy (doubling, skipping, smearing, "phasiness", loss of bite) is from the body as quoted in
  Round-5, where anchor points are pairs of (input time, output time) — the same object as a REAPER
  stretch marker.
- Nagel & Walther, AES 127th, Paper 7926 (2009): transients removed, remainder stretched, transients
  re-inserted — two failure points, not one. *Abstract only.*
- Juillerat, AES 142nd, Paper 9779 (2017): one parameter trades vertical against horizontal phase
  coherence; the stretch *mode* is a control. *Abstract only.*
- Bryan et al., DAFx 2012: separate intent (stiffness/constraints) from the DSP rate curve.
  *Round-5 full text.*

**Current mapping.** `item.set stretch_ratio` (0.5–2.0), `item.set stretch_markers`
(`position_s` strictly increasing, optional `source_position_s`, ≤256 entries) → `SetTakeStretchMarker`.
`item_context` reads marker positions with `GetTakeStretchMarker` (`lua:403-406`), so the data exists.
The `item.set` readback (through `read_edit_state_after`'s fallthrough) returns **marker count** only
(`:2946-2947`); no positions. No stretch mode, no `C_FADEINSHAPE`, no attack-count check anywhere.

**Detection method and confidence.** None: admissibility is policy, and the measurable check (attack
count/position before vs after) is an `agreement` between two measurements, not a model confidence.

**Edit authority.** *Refuse by default.* Apply only when the caller explicitly asks for time
modification, and never inside a "fix timing to grid" path. `EDITING-ACCEPTANCE.md:36` forbids
stretch for the owner's case; the bridge will happily do it, so the refusal must live in the plan
layer.

**Preconditions / readback / rollback.** Record `playrate`, existing markers, `auto_stretch`,
`beat_attach_mode`, item timebase, tail length before the edit. After: marker positions (implementable
via `GetTakeStretchMarker`), attack count and positions from a rendered comparison, playrate, length.
Rollback is undo; a marker set that cannot be restored exactly is a rollback failure.

**Budget.** The TSM itself runs inside REAPER (mode unknown, no CPU figure). One render per
verification; a capture if the verification must be revision-bound.

**Failure modes.** Doubling/skipping; smearing/phasiness; transient re-insertion errors; marker
placement errors; pitch-mode artifacts; stereo/phase decorrelation; a marker set that survives the
session but not save/reload (E7/E12 still open).

**Falsifiable acceptance test.** Stretch a 2 s guitar item by 1.05 and assert: attack count identical
to the pre-edit render; every matched attack within ±2 ms; `playrate` unchanged on every other item;
marker positions read back exactly (not just the count). Separately, place a marker at project 11.0,
extend the left edge by 30 ms, and assert the marker's source position is unchanged (E9); if it
moves, `item.edge` must refuse items with markers in v1.

### 5.5 FX optimization and control

**Capability.** Choose a chain topology from context (a model decision), then fit a small number of
parameters inside a user-supplied chain with a bounded, measured search that reports its budget, its
prior and its alternatives.

**Primary evidence.**
- Steinmetz et al., *ST-ITO*, arXiv 2410.21233 (2024). Abstract re-verified: it searches the parameter
  space at inference and *"enables control of arbitrary audio effect chains, including unseen and
  non-differentiable effects"* with a **gradient-free** optimizer. The budget (CMA-ES 64 × ≤25 =
  **1,600 evaluations**, series chains only, user-supplied chain, minimally processed input) is from
  the body as read in Round-5.
- Koo et al., *ITO-Master*, arXiv 2506.16889v3 (2025). Abstract re-verified: it optimizes the
  **reference embedding**, not the chain parameters. Round-5's body quote is the cleanest
  identifiability statement in scope: Θ-fitting works when input and output content are identical
  (reverse engineering) and is much less effective for style transfer with a content-independent
  loss — the line between "the dry signal of this performance" and "someone else's master".
- Yu et al., arXiv 2505.11315 (2025): unconstrained ITO produces *"unrealistic configurations"*; a
  Gaussian prior cuts parameter MSE by up to 33 %. *Abstract only.* Any search needs a prior and must
  say the result is one member of an equivalence class.
- Wright & Välimäki, DAFx20in22: a structured compressor model matches a black-box baseline at
  <10 % of operations per sample — the argument for a 4–6 parameter structured search over a
  compressor rather than a 40-parameter generic one. *Abstract only.*
- GRAFX, Text2FX and NablAFx require processors you implemented or differentiable plugins; none
  transfers to arbitrary REAPER VSTs.
- Ronan et al. 2017 (verified in full today, see 5.6): chain/subgroup decisions are driven by need,
  not genre — topology is a context decision, not an optimizer output.

**Current mapping.** `daw/limiter.py`: parameter calibration by sampling the plugin's own formatted
values, then 1 baseline render + up to 3 ceiling passes × (2 probes + ≤9 bisection renders) = **≤34
renders**. `daw/eq.py`: 5-point calibration per parameter (no renders; set/read only), `plan_moves`
needs corpus band deltas, `apply_moves` writes normalized values. `daw/cycle.py`: corpus distance →
one EQ move at a time → render → measure → keep only on improvement. `daw/delivery.py`: loudness and
true-peak status/trim. **MISSING:** a general `fx_search` with `max_evaluations`, prior, plausibility
check, `status="budget_exhausted"` and top-k alternatives; parameter-identity record across sessions;
automation detection before a write.

**Detection method and confidence.** Not a detector: confidence is a render delta on a named metric
(LUFS, true peak, band level, crest), same stage/window, with the render count recorded.

**Edit authority.** 1–3 parameters: *apply* with an explicit budget and plausibility ranges. More than
3: *suggest-only* (return the parameter vector and the evidence, do not write). Chain topology:
*model decision* — no computation, but the plan must state the reason and the evidence ids. Chain
inversion from a stereo reference: *never*. The only admissible objectives are explicit DSP
constructs or a corridor rank/percentile distance under A's stated conditions; a learned
style-embedding objective stays behind a monotonicity gate.

**Preconditions / readback / rollback.** Full chain with parameter identity, units and normalized
values; `recFX=false` (the historical HB-004 bug inserted into the record-monitoring chain); plugin
min/max via `TrackFX_GetParamEx`; PDC; existing automation on the parameter. After: normalized **and**
formatted value, no other parameter moved, a render delta on the intended construct, undo. Refuse if
the parameter is automated, the plugin is offline, or the value leaves the plausibility range without
explicit instruction.

**Budget.** ≤34 renders by structure for the limiter; ≤12 renders as the design target for a monotone
1–3 parameter search; 1,600 only for a user-supplied chain with a reference and an explicit budget. At
the measured 98.47 s per 42-track, 300 s render batch, 1,600 full-mix renders is ~43 hours —
**UNMEASURED for short windows, and clearly not a default**. A short-window candidate render is the
only shape that makes 10²–10³ evaluations affordable, and it does not exist.

**Failure modes.** Non-monotone parameters; automation masking the write; plugin latency changing the
comparison clock; parameter reordering after a preset change; a preset that changes topology; the
optimizer finding implausible settings that score well; corpus distance used for compression/limiting
(A measured it near chance on `compressed` and worse than chance on `clipped` — do not).

**Falsifiable acceptance test.** On a synthetic gain plugin, recover a target within ±0.3 dB in ≤12
renders and report the count; on a deliberately non-monotone parameter, terminate with
`status="no_monotone_relation"` and write nothing; on a budget of N, make exactly N renders and return
`status="budget_exhausted"` with the best-so-far **and** its evaluation index; after a real apply,
assert every other parameter's normalized value is bit-identical and the automation envelope is
untouched.

### 5.6 Panning and grouping

**Capability.** Change a track's pan as an explicit, reversible operation with a protected set; treat
subgrouping as a hypothesis justified by measured processing need, never by track names.

**Primary evidence.**
- Perez-Gonzalez & Reiss, *A Real-Time Semiautonomous Audio Panning System*, EURASIP JASP 2010:
  rule-based panning beat a non-expert and showed no significant difference from a professional — for
  *unidentified* sources. The paper says it does not emulate artistic decisions. Auto-panning is a
  fallback, not a default.
- Perez-Gonzalez & Reiss, DAFx 2007: low frequencies kept centred; near-identical spectra separated by
  priority. *Round-5 partial full text.*
- Ronan, Gunes & Reiss, *Analysis of the subgrouping practices of professional mix engineers*, AES
  142nd Convention, Paper 9700, May 2017 — **verified in full today**. Ten award-winning engineers,
  21 questions, nine assumptions, eight accepted. Table 2 rank order: **1 Panning, 2
  Subgrouping/EQ/DRC, 3 Loudness/Level, 4 Effects (temporal)**. DRC is referenced **31** times and EQ
  **ten** times. The decisive practitioner statement is quoted in the paper: *"Many genres of music
  need subgroups, it's not the genre, but the amount of information in the mix"*, alongside *"The more
  compression required, the more subgroups necessary."* This is direct evidence against role→bus
  hardcoding and for need-driven grouping.

**Current mapping.** `track.set pan` (readback `D_PAN`); digest carries `I_PANMODE`/`I_NCHAN`
(`lua:478-479`). No pan law in the plan path, no `channel_mode` setter, no folder-depth operation, no
group-aware edit, no protected-pan concept, and no grouping-evidence object.

**Detection method and confidence.** Pan is a project fact, not a detection; a pan *relation* is
`interval`-like and advisory. Subgroup need would be `agreement` across measured features (shared band
profile, shared dynamics) with `n` corroborating measurements.

**Edit authority.** Pan: *apply* (exact readback) except on a protected set — hard-panned doubles,
low-frequency sources, declared arrangement pan. Grouping: *suggest-only* unless structural change is
explicit; never create a bus because a track is named "Gtr".

**Preconditions / readback / rollback.** Pan: current pan, pan mode, channel count, group id, and the
pan relation evidence at the same window. After: `D_PAN` readback, mono-sum level and correlation
delta, and a check that no other track moved. Grouping: routing graph before/after, send pre/post
mode, folder depth, and the master path. Rollback: undo; a folder creation that changes PDC must be
verified by a render.

**Budget.** Pan write: no render required; an optional verification render. Grouping: one render to
verify the bus sum and one to verify the master.

**Failure modes.** Destroying intentional hard-panned doubles; a pan-law change altering the balance
of everything downstream; mono collapse; folder depth silently changing routing; send pre/post mode
misread; group edit applied to one member only.

**Falsifiable acceptance test.** Pan −1.0 → +1.0 reads back exactly and a mono-sum measurement reports
Δcorrelation and Δmono level; a plan panning a member of a declared double-track pair is refused
unless the pair is named explicitly; a bus-creation plan must cite at least two measured need signals
(e.g. shared band profile and shared dynamics) and state the routing delta; after a folder creation,
the master render must differ only by the intended bus processing (null test against a manually built
reference).

### 5.7 General editing operations

**Capability.** Cut, reveal an edge, move, and (only when asked) stretch — each with a preview that
shows the consequence, a readback that proves it, and one undo block.

**Primary evidence.** Dannenberg's `(T, F, D)` triple is the abstraction. `docs/EDITING-ACCEPTANCE.md`
fixes the musical requirement: overlaps of **69 / 27 / 22 ms**, attacks must not move, stretch and
tempo changes are forbidden, and a 9→2 attack loss fails regardless of grid fit. The v7.85 catalogue
documents `D_STARTOFFS`, `D_FADEINLEN`, `C_FADEINSHAPE` and `Get/SetTakeStretchMarker`, so every
missing operation is implementable natively.

**Current mapping.** `item.split` only. It splits at `position_s`, extends the left item by `tail_s`,
moves the right item by `move_s + right_trim_s`, computes `overlap = left_start + left_length −
right_start`, and sets both fades to `min(overlap or fade_s, lengths)`. `D_STARTOFFS` is untouched.
Readback is `{item_count}`. There is no `item.edge`, no fade-length setter, no `expected_after`.

**Detection method and confidence.** The operation has no detection; the *decision* is event-driven
(5.1). Geometry is exact arithmetic on doubles; the only uncertainty is REAPER's fade shape and
rounding, hence the one-sample readback tolerance (20.8 µs at 48 kHz).

**Edit authority.** Split: *apply* once geometry readback exists; until then every split result should
be labelled `geometry_unverified` in the response, because `item_count` cannot distinguish "the
intended 5 ms overlap" from "a 69 ms fade". Edge reveal: *apply* only after the `D_STARTOFFS` writer
and readback exist; before that it is *unavailable*, not "suggest-only". Move: *apply* with pair
verification for double tracks. Stretch: *refuse by default*.

**Preconditions / readback / rollback.** SourceMap verified; playrate and markers known; source bounds
checked (`start_offset_s − Δ·playrate ≥ 0`, and the new end within source length); group membership;
event scope. After: position, length, offset, fades, playrate, marker count/positions, new GUID, item
count; a seam render compared with the same region before the edit. Rollback: one undo block; undo
requires the post-apply revision and the label on top.

**Budget.** Geometry: no render. Seam verification: one short render. Cross-revision: a capture.

**Failure modes.** A 69 ms overlap becoming a 69 ms fade of unknown shape; `right_trim_s` delaying the
right slice instead of revealing its past; source exhaustion at the left edge; group desync; a
beat-attached item moving because of an unrelated tempo edit; a split inside another event's sustain.

**Falsifiable acceptance test.** On a disposable copy, left edge −0.069 on an item at 10.0/2.0/0.5
must read back 9.931/2.069/0.431 ±1 sample, the attack previously at 10.500 must still be at 10.500
±1 sample, and the item count must be unchanged. A split at 11.0 with `tail_s=0.130, move_s=0.100,
right_trim_s=0.025` must read back overlap 5 ms ±1 sample and fade lengths equal to the overlap ±1
sample. Undo must restore the pre-edit digest exactly. These are A3–A6 and A11 of the Round-5 ORIG
table, and they remain the correct tests.

---

## 6. Detection confidence versus edit authority

### 6.1 The schema that already exists

`event_model.Confidence` is the right shape and the only authority mechanism the codebase needs:

| `basis` | What the number means | What it may authorize |
|---|---|---|
| `interval` | Normalised margin inside a measured interval (detector strength above threshold) | Ordering, triage, which candidate to inspect first. **Nothing destructive.** |
| `rank` | Relative ordering only | Display and selection among alternatives. **Nothing destructive.** |
| `agreement` | Fraction of independent methods/sources agreeing, with `n` recorded | A destructive edit, only with `n ≥ 2` and a stated tolerance |
| `none` | Unknown or uncalibrated | Nothing; treated as absent, never as zero confidence that "passes" a `>=` test |

`UsageScope` is the second half: `timing`, `pitch`, `duration`, `midi`, `destructive_cut`. Defaults are
deliberately conservative (`timing=True`, everything else `False`). The two together are sufficient to
make authority mechanical. Today neither is read.

### 6.2 The two-stage flow I would enforce

1. **Detection.** A detector emits `SignalEvent`s bound to an asset, `MethodRef`, anchored `Interval`,
   strength and a `Confidence` with a basis. It may emit several at once; it may not merge them.
   `transients` and `events` become two producers of one record type, not two dict formats.
2. **Authority resolution.** The planner cites event ids. The gate checks existence, asset membership
   in the current capture, and that `UsageScope` covers the operation (`destructive_cut` for
   split/edge/delete, `midi` for MIDI write, `timing` for move). A destructive operation needs one
   cited event with `basis="agreement"`, `n ≥ 2` and a value at or above the policy threshold, **or**
   an explicit caller instruction. Plans citing no events stay valid but cannot claim event evidence.
3. **Apply.** One undo block, GUID re-resolution, revision re-check, per-operation readback;
   `expected_after` is compared before responding, and a mismatch is `postcondition_failed`.
4. **Verify.** Re-render the same stage and window (or a plan-bound candidate) with the same
   method/version. The result is a **new** measurement at a **new** revision, never a delta to the old
   one without the revision pair.
5. **Invalidate.** The applied plan writes post-apply revision, target GUIDs, cited evidence ids and
   stale asset ids. Until it does, claims on old assets are `stale` and the token cannot advance.

### 6.3 Stale revision and invalidation rules

- **Preview → apply:** the plan binds a `ProjectRevision`; a changed `change_count` is
  `stale_context` (implemented, tested by `test_project_session_blocks_stale_transaction_before_bridge_apply`).
- **Receipt → write:** complete receipt or context token bound to the same project and revision
  (implemented).
- **Apply → next write:** the token now names the pre-apply revision; advancing it needs a capture plus
  `changes_since` + `delta_ack`. That is correct but expensive — the addendum wanted the edit itself to
  produce the delta, so today every second edit costs a capture.
- **Capture validity:** `invalid_master` never contributes fresh measurements; the read path exposes
  `capture_status`, `invalid_reasons`, `master_validation`, `coverage`, `missing`.
- **Cross-revision measurement:** comparable only when asset, window, stage, channel and
  method/version match (`render_key`, A's answer 2). Nonlinear-node measurements need the
  reconstruction gate; a pan or channel-mode change invalidates a band difference even with the same fader.

### 6.4 What must never be auto-applied

1. A destructive cut or edge reveal justified by a single detector, a single source, or an
   `interval`/`rank` confidence.
2. A stretch, playrate change, or stretch-marker set that the user did not explicitly request.
3. A tempo or time-signature change while beat-attached items exist and have not been enumerated.
4. A pan move on a track declared as part of a hard-panned pair, or on a low-frequency source whose
   mono compatibility was not checked.
5. An FX parameter write on an automated parameter, an offline plugin, or a value outside the declared
   plausibility range.
6. Any operation whose target `SourceMap.mapping_status` is not `verified` (loop, reverse, section
   source, unresolved playrate).
7. Any edit whose cited evidence is `unreviewed`, `contested`, or bound to a stale capture.
8. Any group edit that would touch one member of a group without naming the others.

### 6.5 What is not identifiable from a stereo reference

These are boundaries, not implementation gaps; no amount of engineering removes them:

- The reference's per-instrument FX chain, plugin identities, or knob values. ITO-Master says it
  directly: content-independent loss makes Θ-fitting ineffective outside reverse engineering.
- The attribution of a band difference to a specific source. Four-stem separation means "guitar" is
  not a stream; a band delta is a band delta plus a ranked hypothesis.
- The reference's original pan positions, channel mode, or pan law. A stereo mix collapses them.
- Whether a difference is a different take, edit, or processing — without the reference multitrack these
  are indistinguishable.
- Absolute loudness/true peak after lossy coding and platform normalization.
- Whether an improvement is the change under test or a level match; only a loudness-matched,
  same-window comparison can say anything.

---

## 7. Cross-dependency response to Workstream A and open questions

A's final cross-note answers my six questions; the substantive reconciliation is below.

**Where we agree, and what it changes in this report.**
1. *Stage labels.* A's partial loudness runs in the bundle path with a declared stage, node, window and
   channel and fails closed on mismatch; it does not read capture `analysis[]`, which is still a
   9-value array. I have therefore not treated capture rows as stage-bearing, and any before/after
   claim in my layer must name the bundle's declared stage or be `incomparable`.
2. *Comparators.* Valid only when asset, window, stage, channel and method/version match (in practice
   `render_key`). That is stricter than my verify loop can guarantee, since `render_key` is not
   implemented; until it is, a pair is comparable only if the caller re-renders the same window at the
   same stage and the plan records both revisions.
3. *Band dominance and events.* A keeps band dominance as a locator with fields the tree has not yet
   added, and does not ask my layer to consume it; A needs no event identity for masking claims. So no
   edit precondition cites band dominance, and the event layer stays scoped to editing and timing.
4. *Stereo reference.* Band difference over an aligned window yes; attribution only as a ranked
   hypothesis; chain recovery no — exactly §6.5.
5. *Invalidation.* A needs per-asset, per-window and method/version-family tags because a relation
   cites two assets; I accept the relation-level requirement over my per-asset proposal.

**What A requires from me, and my answer.**
- `render_candidate(plan, window) -> {audio, sha256, stage, revision}`: **does not exist** (verified by
  grep). Until it does, A's reference/texture/delta checks degrade to manual apply/render/undo, and so
  do my seam and FX verifications. This is the single highest-leverage primitive for both workstreams.
- Invalidation records written by the edit path: **not implemented at apply time.** `lifecycle._invalidations`
  exists only inside `changes_since` between captures and is coarse (`audio_and_analysis` on an op
  path). I will specify the record shape in the implementation, but I will not claim it exists.
- `pan_mode`/`channel_mode` in digest **and** panorama item rows: digest yes (`lua:478-479`), item
  rows no — a pan or channel edit is invisible in the default view.
- No corpus distance for compression/limiting: accepted, with A's measured evidence (near chance on
  `compressed`, worse than chance on `clipped`). `daw/cycle.py`'s corpus-driven EQ path must not be
  surfaced as general FX optimization.

**What I require from A.**
- One model-facing reference mechanism, not two. Neither is an edit input; if a reference enters an FX
  plan it is a corridor **rank/percentile** under the corridor's stated conditions (stage match, section
  match, n ≥ 8, loudness matched ≤ 0.5 dB). A must choose which is model-facing; I will not cite both.
- Written confirmation that `band_dominance` cannot gate an edit (A's cross-note already says so).
- A's stage/window contract in machine-readable form so plans can name it without paraphrasing.

**Contradiction resolved.** A reads "changes_since diffs captures only" as a problem for derived
views; it is the same problem from both sides. The resolution is one record: every derived view and
every applied edit writes a `dep_set` of `{capture_id, revision, asset_ids, method_family}`, and
`changes_since` compares those sets as well as document facts. That is a lifecycle change and belongs
with the post-apply delta.

**Open questions I could not settle.**
1. Does the owner accept that, until a post-apply delta exists, a second edit on the same project
   requires a full capture (≈5–12 min on the reference project)? If not, the lifecycle change is a
   release blocker, not a refinement.
2. Is one-sample readback tolerance the right contract for `D_STARTOFFS` on all supported sample
   rates, or should it be `1/48000` s regardless of project rate? The repo uses `1/sample_rate` at
   `session.py:137,174`; I have assumed the project rate.
3. Does REAPER's default fade shape vary per project (Preferences → Project → Media Item Fades), and
   does the API let us pin it per item via `C_FADEINSHAPE`/`D_FADEINDIR*`? If yes, `item.edge` and
   `item.split` should set it explicitly and the preview can be exact. Unverified.
4. Do group-synced items follow an API split? Ledger E18 remains open; until it is answered the plan
   must expand groups explicitly.
5. Do stretch-marker positions survive save/reload exactly (E7/E12)? This gates any stretch
   verification, and therefore any claim that a stretch preserved attacks.
6. Is the 30 ms cluster tolerance safe at 250–300 BPM? My reading says no (a 32nd is 25 ms at 300 BPM);
   this needs the synthetic test in 5.1 before any edit map is trusted on blast material.
7. Should the two operation caps be unified at 200 or 256? The cut planner enforces the lower one while
   the transaction accepts the higher.

---

## 8. Bottom line

Seven things are true of the tree as it stands, and they decide the next step:

1. The **context layer** moved: panorama lifecycle, exact-base deltas, receipts and a fail-closed
   mutation gate are real and exercised.
2. The **action layer** did not: no `item.edge`, no `D_STARTOFFS` writer, no geometry readback, no
   `expected_after`, no `UsageScope` enforcement, no group-aware edit.
3. The **event layer** is a type system with no producers or consumers; `rhythm/` is empty.
4. **FX** is two narrow search tools (≤34 renders; calibration-only EQ) and a corpus cycle that must
   not be generalised.
5. The **profiler** implemented overlap (master-first, batches, bounded pool) but not packaging
   (streaming record, single hash pass, seal timers); no post-change wall-clock exists.
6. **Continuity** is bound to captures, so edit→delta→edit still costs a capture.
7. **Authority** is type-level only; the schema is right and nothing reads it.

The smallest change that changes the product is one step: `SourceMap` + `item.edge` + geometry
readback + `expected_after` + `UsageScope` enforcement + a post-apply delta. Needs no GPU or new model, and closes the only written owner acceptance case (69/27/22 ms). Everything else in this
report — repeated structure, meter, texture coupling, FX search beyond three parameters — is
downstream of it.
