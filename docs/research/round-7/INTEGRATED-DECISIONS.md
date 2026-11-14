# Round 7 — integrated decisions

> **Historical record (superseded transport).** This document describes the
> paged panorama contract that has since been replaced by one whole-view result.
> Current behaviour: `docs/DECISION-single-response-panorama.md` and
> `docs/MCP-README-LLM.md`. Treat this file as history, not instruction.


Date: 2026-11-13
Inputs: `WORKSTREAM-A-AUDIT.md` (auditory evidence), `WORKSTREAM-B-AUDIT.md` (events,
editing, FX, grouping), `SEARCH-LOG.md`, Round-5 reports and synthesis, Round-6 synthesis
and implementation directive, and the current dirty worktree read-only.
Model of this session and of both subagents: `deepseek/deepseek-v4.1-flash-1017` via
RouterAI (`custom:routerai.ru`), reasoning Ultra.

During the research phase, no source, test, profiler, REAPER project or benchmark file was
modified, and no test, render, REAPER session or GPU job was run. The owner-directed
implementation that followed is recorded in §9.5 separately; it changed only the relation,
capture-binding and panorama/lifecycle code and its tests. Every cost figure is either
quoted from a named artifact or labelled ARITHMETIC / UNMEASURED.

This document does not repeat the two audits. It resolves them: what is implemented, what
is missing, what is decided, what must be measured, and in what order.

---

## 0. What genuinely changes versus Round 5

1. **The split between the capture and the bundle is now the architectural gap.** Per-source
   measurements live in `reaper_capture_snapshot`; band dominance, partial loudness and
   reference comparison live in the separate `reaper_analyze` → `analysis/bundle.py` path.
   The capture record's `analysis[]` is a method-blind 9-value array and the panorama has
   structural crosslinks but no cross-source measurement. Round 7 treats that split as a gap
   to close, not an acceptable endpoint: the target is one revision-bound panorama carrying
   both per-source and cross-source evidence (§1.3). Until it is closed, every relational
   claim is a second, unarchived computation that cannot be bound to the capture it cites.
2. **The performance work moved the bottleneck away from render.** Master-first rendering,
   topology-aware batches and `BoundedAnalysisPool` are in the tree. The 703.072 s sealed
   profile predates them and may not be quoted as current cost. There is exactly one live
   measurement of the new pipeline: a 12 s, 42-track smoke at 29.812 s entry wall with
   17.370 s of render/analysis overlap. No full-song timing exists.
3. **One perception capability is production-grade and one is a misnamed number.**
   Reconstruction-gated partial loudness is real, fails closed, and is pinned by 17 tests.
   "Modulation depth" is not AM depth; it is a whole-file envelope-peak share. Band
   dominance is still `masker`/`target` at a hard-coded 10 dB with a dead 6.0 dB consumer
   filter.
4. **A measured reference model exists, and it is blind where it matters most.**
   The corpus model is level-invariant and detects tilt/width/air, but it is near chance on
   compression (0.583/0.645 closer-original) and worse than chance on clipping (0.355).
   It can rank similarity; it cannot judge dynamics. The distance-to-corpus objective in
   `daw/cycle.py` is therefore not admissible.
5. **The action layer, not perception, is the product bottleneck.** No `item.edge`, no
   `D_STARTOFFS` writer, split readback of `item_count` only, no `expected_after`, no
   `UsageScope` consumer, and `changes_since` is capture-bound — so a second edit costs a
   capture. Adding *optional* evidence before fixing this increases the amount of
   unverifiable context.

The practical consequence: **keep the full capture and its full-resolution measurements;
integrate the deterministic, interpretable relation core (source↔source, source↔master,
reference when supplied) into that same capture; and do not add opaque learned quality
judges, texture/separation models or unvalidated verdicts to the default path.** Spend the
next engineering on identity, invalidation and edit verification — relations without those
are unverifiable too.

**Sequencing around the active profiler.** The relation core touches
`daw/full_capture.py` and `context/panorama.py`, both on the measured critical path. Do not
edit those files while a profiling run is in flight; do not run a benchmark on a tree that
another session is editing; do not change worker policy, render settings or batching while
numbers are being collected. Land the relation stage behind an explicit off-by-default flag,
measure it on a clean tree, then enable it by default. The profiler's sealed artifacts and
benchmark files remain untouched.

---

## 1. The panorama/context lifecycle: what is implemented, and what is not

This section is deliberately separate from the gap list. The lifecycle is the strongest part
of the current tree and must not be re-described as missing.

### 1.1 Implemented and exercised

| Capability | Where | Evidence of implementation |
|---|---|---|
| Portable tool contract, 29 tools / 6 resources | `src/reaper_audio/mcp_server.py` | Round-6 capability audit; B recount (`@server.tool()` = 29) |
| Initial complete panorama as deterministic pages | `context/lifecycle.py::open/page/ack` | Round-6 clean session fetched and acked all pages |
| Page and document hashes; incomplete ack mints no receipt | `context/lifecycle.py::ack` | `incomplete` + `missing_pages`; idempotent duplicate page/ack |
| Exact-base changes-since | `context/lifecycle.py:743` | Requires base capture/root; returns delta, stale_base, refresh_required |
| Delta acknowledgement advancing a context token | `reaper_panorama_delta_ack` | Round-6 clean session |
| Refresh always available | `context/lifecycle.py:738` | Mandatory on unprovable continuity |
| Interval evidence slice over whole-song state | `context/lifecycle.py:940` | `state.scope=project`, `evidence.scope` narrows |
| Mutation gate: complete receipt + exact revision | `mcp_server.py` guard; `daw/session.py` | Applies to `reaper_edit` apply/undo, `reaper_api_call`, `reaper_action` invoke, `reaper_save` |
| Capture validity on the read path | `context/full_capture.py`; `reaper_capture_read` | `capture_status`, `invalid_reasons`, `master_validation`, `coverage`, `missing` |
| Fail-closed capture integrity | `daw/full_capture.py` | SHA-256 per asset, sealed record, archive re-hash, `capture_master_invalid` |
| Bounded analysis pool with back-pressure | `daw/analysis_pipeline.py:35` | Pool tests: in-flight bound, GUID order, failure propagation |
| Worker policy | `daw/analysis_policy.py:30` | Hard cap 8; 8 short / 4 long / 4 unknown; memory refusal before spawn |
| Topology-aware render batching | `daw/capture_batching.py:262` | Folder atomicity, singleton merge, whole-project fallback |
| Render/analysis overlap | `daw/full_capture.py:547-640` | 12 s smoke: 29/43 results before last render end |

### 1.2 Gaps, stated as gaps

| Gap | Consequence | Where it belongs |
|---|---|---|
| `changes_since` is bound to a base capture | An edit without a new capture returns no delta; second edit costs a capture (B: ≈5–12 min on the reference project) | `context/lifecycle.py:743-770` + apply path |
| No post-apply delta or invalidation record | Old claims stay "fresh"; token cannot advance honestly | `reaper_edit` apply path; `lifecycle._invalidations():261` is delta-only and coarse |
| `render_key` decided but not built | Assets are compared by hash, and 43/43 hashes differ for identical content | `daw/full_capture.py` (0 grep hits) |
| Revision identity drops `instance_id`/`epoch`; `fact_root` is a document root, not a per-entity journal | Cannot prove which entity changed | `context/revision.py`; `context/lifecycle.py:186` |
| Panorama item rows carry position/length/take only | A pan, offset, playrate, fade or channel-mode edit is invisible in the model's view | `context/panorama.py:178` |
| `analysis[]` rows are 9 positional values | No method, version, stage, channel, window, units; cannot be compared across revisions | `context/panorama.py:1063` |
| No validity block in `panorama_open`'s envelope | Invalid capture status is not on the first page | `context/lifecycle.py:486-524` |
| No `render_candidate` | No plan-bound audio for before/after verification; every check degrades to apply/render/undo | bridge + `daw/session.py` (0 grep hits) |
| `item.split` readback returns `item_count` only | Cannot distinguish a 5 ms overlap from a 69 ms fade | `scripts/reaper_bridge.lua:3103-3106` |
| No `D_STARTOFFS` setter, no `item.edge` | The one written owner acceptance case (69/27/22 ms) cannot be executed | `scripts/reaper_bridge.lua` (read :381/:755, copy :3639 only) |
| Large-project capture can fail on `RENDER_TARGETS` | A 41-track reference copy returned an empty target list; a 7-track project captured | capture/render state, not lifecycle (round-6 audit) |

The lifecycle is therefore **implemented and usable for reads and gated writes**, and
**not yet self-advancing for edits**. That distinction decides the near-term order.

---

### 1.3 The architectural gap: two audio paths, one target document

The owner's requirement is explicit: the split between the full capture (per-source
metrics) and the separate bundle relational analysis is a **gap to close**, not an acceptable
endpoint. The target is one coherent, revision-bound panorama containing complete per-source
and cross-source evidence — track↔track, track↔master, and project↔reference where a
reference is available — with every fact, measurement, event, audio asset, interval, FX slot
and route linked through stable typed identities and provenance.

**What exists today.**

| Layer | What it carries | Binding |
|---|---|---|
| `reaper_capture_snapshot` | 43 per-source measurement rows (top-level metrics, band-time curves, temporal levels, events), project map, FX/routing, master validation, audio assets | Immutable record + panorama sidecar; revision-bound |
| `reaper_analyze` / `analysis/bundle.py` | Band dominance pairs, partial-loudness shares, reference corridors, phase pairs, cross-source band timeline | A **separate** render/analyze call; no capture id in the payload; not archived; not revision-bound |
| `reaper_mix_evidence` | Issues derived from the bundle (band dominance, corridor deltas, phase, partial-loudness shares) | Consumes the bundle; no stable relation ids |
| `panorama.py` `crosslinks` | Structural object graph (track → item → take → source → FX) | In the document, but no measurement edges |

So the per-source half is archived and revision-bound; the cross-source half is neither.
That is the gap: a model can see 43 measurements and a structural graph, but it cannot see
"source A vs source B in band X over window W" as a fact of the same capture, with the same
provenance, in the same hash.

**Target document (one capture, one panorama).**

```
capture_binding: capture_id, project_id, epoch, revision, fact_root,
                 schema_version, document_hash
sources[]:       source_id, track_id, asset_id, asset_sha256/render_key,
                 stage, node, channel_view, window,
                 measurements[] (method/version/units/coverage),
                 events[] (typed, confidence basis)
relations[]:     relation_id, kind, from_id, to_id, window,
                 stage, node, channel_view, method, method_version,
                 metrics{...raw, units...}, coverage, uncertainty,
                 alternatives[], provenance
references[]:    reference_id, manifest_entry, stage, section, window,
                 method/version, relation_ids[] (or reference_status)
```

Required relation kinds, each deterministic from the capture's own assets:

- `band_dominance` — source↔source, same window, pan relation and active fraction;
- `track_master_level_relation` — source↔master level relation, explicitly an arithmetic
  share of summed solo renders (never a causal contribution through master FX, returns or
  PDC); the actual master comparison remains a separate measured asset pair;
- `phase_pair` — source↔source, correlation and mono-fold loss;
- `partial_loudness_share` — source↔declared node, **optional**: computed only when the
  reconstruction gate passes, written as an immutable analysis node (never a mandatory
  capture field); a failed gate is an explicit `gate_failed` record with the measured
  residual;
- `reference_comparison` — project↔reference, **when a manifest is supplied**, carrying the
  reference identity, stage, section and window; otherwise
  `reference_status="not_supplied"` (never an empty list that reads as "no difference").

The mandatory core is band dominance plus the source-master level relation. Phase is
measured when the capture supplies it and otherwise carries an explicit unavailable reason;
`partial_loudness_share` is an optional gated node because it costs model arithmetic and can
fail closed. Absence is always explicit, never silent.

**Identity rules.** Every endpoint is a stable typed id already used by the panorama
(`trk_`, `itm_`, `tak_`, `src_`, `snd_`, `fx_`, master). A relation id is derived from
`(kind, from_id, to_id, window, stage, method, method_version, render_key)` — not from a
per-capture counter — so the same relation in a re-capture with the same render keys keeps
its identity and can be compared. Every relation carries its `dep_set`
(`capture_id, revision, asset_ids, method_family`) so a later edit or re-capture can
invalidate it without guessing.

**Mandatory core vs optional analysis nodes.** The mandatory core is the scalable,
deterministic relationship set that must exist in every full capture: source↔source band
dominance, the source↔master level relation (never called a causal contribution), and the
per-source identity links. Source↔source phase is measured when the capture supplies it and
otherwise carries an explicit unavailable reason; it is not fabricated from a stereo master.
The core is computed from the capture's own assets at the same revision and window, carries
`relations.coverage`/`truncated`, and is part of the document hash. Optional expensive
calculations — texture statistics, separation-derived role evidence, instrument-wise
embeddings — may be deferred, but when they are computed they become **immutable analysis
nodes** with their own `analysis_id`, bound to the same `project_id`/`revision`/`capture_id`,
naming their input asset ids, method/version, window and `dep_set`, and linked into the
panorama by stable typed ids. They are never detached, never a parallel archive, and never a
loose file that outlives the capture they describe. This is completeness and traceability,
not a quality score.

**Completeness rules.** `relations.coverage` reports `expected`, `evaluated` and `observed`
per kind: at 42 sources band dominance expects 861 unordered pairs and **every one** is
retained as an evaluated columnar row, including `no_dominant_band` with its measured best
deltas, active fraction and a `series_ref` resolving to both aligned source band-level
series (table-level `temporal_model` gives the exact pointer template, transform and
`shared_frame_count` alignment). Every row also carries a derived `relation_id` from its
typed endpoints and the binding, so an unremarkable or unavailable pair is still
addressable evidence, plus a per-row `dep_set` naming the capture, revision, endpoint asset
ids and method family. A thresholded relation list is a secondary view, not the canonical
store. `top-K` is never a completeness rule; when a
bounded projection is requested it carries `complete=false`, the dropped count and its
canonical content hash, and cannot be acknowledged as a complete page set. A relation whose
endpoint asset is missing or invalid is `unavailable`, never silently omitted. Full time
coverage is preserved either on the analysis grid or by exact raw pointer; the median is
labelled a summary and never a complete temporal relation.

**Where it is implemented.** The first implementation computes the domain deterministically
at panorama build from the sealed record's per-source analysis (same capture, revision and
window), so no second render and no mutable side channel exists; the relation domain is part
of the panorama document hash. Moving the compute before the record seal is a later
optimization, not a correctness requirement for the current document. The bundle path stays
as the interactive/on-demand surface, but it must return the same typed relation records and
cite the capture they came from — not a parallel private format.

**Cost.** Band dominance is O(S²·B·cells): at 42 sources, 861 pairs × 28 bands × ~3,005
cells ≈ 72 M float operations (ARITHMETIC), plus partial-loudness model arithmetic if the
gate passes. **No wall-clock measurement exists for this domain yet.** The canonical output
keeps one columnar row per pair plus pointers to the aligned source series; it is paged or
columnar-encoded if it exceeds the page envelope, and a page set is complete only when every
page of the canonical domain is delivered. Byte growth must be measured against the
700,000 B budget before the stage is enabled by default; top-K is only a secondary view and
never a capacity strategy.

## 2. Corrections the synthesis makes to the two audits and to prior rounds

These are factual corrections, not disagreements about direction.

1. **Fx-Encoder++ does have an official code release.** A §5.4 and its top findings say no
   code or weight link was found. The coordinator verified
   `https://github.com/SonyResearch/Fx-Encoder_PlusPlus` (created 2025-06-12, README states
   CC BY-NC 4.0, GitHub license metadata "Other"). The conclusion is unchanged and stronger:
   non-commercial licence, instrument query presupposes the separation, synthetic series
   chains. Recorded as S-A7 in the search log.
2. **Partial-loudness gate tests number 17, not 18.** `grep -c '^def test_'` on
   `tests/test_partial_loudness_gate.py` = 17. The behavioural claim (fail-closed on stage,
   node, window, channel, missing source) is correct.
3. **The stretch-marker gap is narrower than "readback is count only".**
   `item_context` reads marker positions with `GetTakeStretchMarker`
   (`scripts/reaper_bridge.lua:403-406`), and the digest carries them (`:1156-1165`). The
   gap is the **post-edit** readback: `read_edit_state` returns marker count
   (`:2946-2947`) and `read_edit_state_after(item.split)` returns `item_count`
   (`:3103-3106`). Any acceptance test must target the edit readback, not claim the library
   cannot read markers.
4. **Panorama byte counts across captures are not a regression series.** The 12 s smoke
   measured 297,037 B; `FULL-CAPTURE.md` records 198,480 B for a 20 s capture and 237,477 B
   for a full song. Different projects, windows and schema moments. The only valid test is
   one project captured twice at the same window with the same schema.
5. **The 20 s capture timing is inconsistent in the existing docs.** `FULL-CAPTURE.md`
   says 78.518 s end-to-end; `PERFORMANCE-PIPELINE-REPORT-B` M27 records 61.697 s for a
   different 20 s run. Neither is a current-tree full-song number. Do not quote either as
   the pipeline's cost.
6. **The performance synthesis's `MAX_ANALYSIS_WORKERS = 16` and memory cap 10 are stale.**
   The tree is hard cap 8, 8/4/4 policy (`analysis_policy.py:30-42`). The tree wins.
7. **Round-5 D5-16 (stage on both sides) is only half satisfied.** Partial loudness has a
   declared node/stage/window and a gate; the per-metric reference corridor
   (`analysis/compare.py:56`) still has no stage, section, loudness or minimum-n gate.

### 2.1 Status of the Round-5 decisions

| ID | Round-5 decision | Status on the current tree |
|---|---|---|
| D5-01 | No absolute perceptual threshold in method code | **Violated**: `temporal.masking(min_db=10.0)` and `mix_evidence(masking_db=6.0)` |
| D5-02 | Rename masking → band dominance with context | **Partial**: consumer issue id renamed (`mix_evidence.py:49`); producer keys and fields unchanged |
| D5-03 | Delete the unreachable 6.0 dB path | **Not done** (`mix_evidence.py:34/46/50`) |
| D5-04 | Partial loudness behind a reconstruction gate | **Done** (`partial.py:475`, 17 tests); bus variant not built |
| D5-05 | Raw shares, no audibility classification | **Done** (`mix_evidence.py:84-107`) |
| D5-06 | No learned quality head / universal score | **Policy holds**; no such code |
| D5-07 | Separation analysis-only, never retained | **Policy holds**; no separator exists |
| D5-08 | `item.edge` writes `D_STARTOFFS` | **Not done** |
| D5-09 | Full geometry readback + `expected_after` | **Not done** |
| D5-10 | `UsageScope` enforced on destructive operations | **Not done**; no consumer of `event_model` |
| D5-11 | Separate reference alignment from grid quantization | **Partial**; names exist, `alignment.propose_moves(strength=1.0)` hazard remains |
| D5-12 | Bounded FX search in a user-supplied chain | **Partial**: limiter ≤34 renders, EQ calibration; no general search |
| D5-13 | Every model output bound to capture/asset/window/checkpoint | **Unresolved**; not re-verified this round |
| D5-14 | Panorama carries edit-critical item fields | **Not done** (`panorama.py:178`) |
| D5-15 | Fix the stale surface count | **Done** for the tool count (29); prose mentioning 21 remains in older docs |
| D5-16 | No relationship without a stage on both sides | **Partial**: partial loudness yes; corridor no |

Round 7 therefore does not "repeat Round 5". It records that the perception half was mostly
**not implemented**, while the performance half was, and that the action half is now the
critical path.

---

## 3. Cost model and critical path

### 3.1 What the artifacts actually prove

| Fact | Value | Source | Status |
|---|---:|---|---|
| Sealed 300 s capture, 42 tracks + master | 703.072 s | `PERFORMANCE-PIPELINE-RESEARCH` §1.1 | **Stale** (pre-overlap) |
| DSP pool wall | 408.274 s | sealed record | Stale |
| DSP worker sum | 3,011.645 s | sealed record | Stale |
| Render (batch + master) | 107.975 s | sealed record | Stale |
| Untimed packaging tail | 149.894 s | derived | Structure still relevant |
| Per-source-second DSP, 300 s class | 0.233 CPU-s | ARITHMETIC | Rate to use for 300 s |
| Per-source-second DSP, 20 s class | 0.082 CPU-s | ARITHMETIC | Do not scale to 300 s |
| Worker knee | 8 (3.359×) | `PERF-WORKLOG` | Current policy matches |
| Per-worker RSS, 300 s | 3.96–4.29 GB | `analysis_policy.py` docstring | Current policy matches |
| 12 s new-pipeline smoke | 29.812 s entry; 17.370 s overlap; 29/43 early | `live-overlap-smoke-20261113.json` | **Only live new-pipeline number** |
| Full-song new-pipeline wall | — | none | **UNMEASURED** |
| 12 s panorama | 297,037 B | smoke | 42 % of 700,000 B budget |
| Limiter search | ≤34 renders | `daw/limiter.py` | Measured by construction |
| 1,600-render ITO budget on full mixes | ≈43 h at 98.47 s/batch | B ARITHMETIC | Not a default |

Two rules follow. **Price a stage on the duration class it ships in**, because 300 s costs
2.84× more per second than 20 s for reasons not yet established. And **do not claim a
speedup**: the only sealed baseline predates the current pipeline.

### 3.2 What each proposed addition costs on the critical path

| Addition | Cost | Blind spot removed | Verdict |
|---|---|---|---|
| Item identity fields in panorama/digest | Bytes only; no DSP | Makes every edit and every before/after claim expressible | **Do first** |
| `render_key` + `dep_set` invalidation | Hashing, milliseconds | Removes false "unchanged"; enables asset reuse | **Do first** |
| Post-apply structural delta | One readback + JCS hash | Removes capture-per-edit | **Do first** |
| `render_candidate` | One short-window render per candidate; p50/p95 **UNMEASURED** | Makes verification plan-bound | **Do second** |
| In-capture relation domain (band dominance, phase, track↔master) | O(S²·B·cells): ~72 M float ops; synthetic 42×28×3,005 run **3.33 s** / 261,898 B table (includes per-row dep_set); capture wall **UNMEASURED**; every pair retained columnar, paged if over budget | Makes cross-source evidence part of the same revision-bound document | **Do first, scheduled off the render wait (D7-01)** |
| Band-dominance context | Inside the relation stage; full pair list on demand; not separately timed | Locates spectral competition honestly | **In capture + on-demand detail (D7-06)** |
| Reference relations | One window measurement per reference per window; wall **UNMEASURED** | Places the project against references in the same panorama | **In capture when a manifest is supplied (D7-09)** |
| Texture | 10–20 s per 300 s asset (ARITHMETIC); +430–860 CPU-s, +14–29 % DSP if in capture | Distinguishes wall from articulation | **On demand only** |
| Separation | **UNMEASURED**; 42 invocations + ~3.4 GB derived audio | Coarse role energy; bleed checks | **On demand, not now** |
| Fx-Encoder++ | **UNMEASURED**; NC licence | None actionable | **Reject** |
| Audiobox / quality head | 103.86M params, F32; **VRAM UNMEASURED** | None actionable; risks a second judge | **Reject as judge** |
| 1,600-render FX search | ≈43 h full-mix; short-window unmeasured | Parameter fitting | **Reject as default; ≤3 params only** |

The one addition with a clear blind-spot-per-cost win is **identity + invalidation +
geometry**, which removes the inability to prove anything after an edit. Texture and
separation remove descriptive blind spots but do not unblock a single operation.

---

## 4. Decisions justified now (A)

Each decision states the evidence, the current location or MISSING, the cost, the failure
mode, and a falsifiable check. Costs are per full capture unless stated.

### D7-01 — One capture, one panorama: move relational evidence into the capture

**Evidence.** Owner requirement (§1.3); A §3.1 (two audio paths); B §7 (both need the same
capture/window/stage identity). A's §5 recommended on-demand for every perception candidate
because none was validated as a quality claim; the owner's mandate overrides that for the
deterministic relation core, which asserts co-occurrence and geometry, not quality. The
bundle already computes the primitives, so the work is to move the deterministic ones into
the capture and give them typed ids — not to invent new measurements.
**Current location.** Implemented in round 7 as a panorama domain
(`context/panorama.py::_relations_domain`, `context/relations.py`,
`analysis/relations.py`) computed from the sealed record's per-source analysis. **Still
MISSING**: a `relations[]` field in the sealed record schema itself and a pre-seal compute
stage; the canonical relation domain currently lives in the panorama document hash, not the
record hash.
**Cost.** One relation domain: band dominance O(S²·B·cells) ≈ 72 M float operations at 42
sources (ARITHMETIC), plus the source-master level relation. A synthetic 42 × 28 × 3,005
all-pairs run took **3.33 s** and produced a 261,898 B table including per-row `dep_set`; **no full-capture wall time
exists**, and the stage must be scheduled outside the render wait or vectorized before it is
enabled by default. Byte growth must still be measured against the 700,000 B budget on a real
capture. The canonical pair table keeps every evaluated pair; paging/columnar encoding is
used if it does not fit, never a top-K cut.
**Failure.** A pair row points at a missing endpoint; a partial-loudness gate failure is
silently dropped; a reference relation is invented when no reference was supplied; a bounded
projection is mistaken for a complete document.
**Acceptance.** One capture returns `sources[]` and `relations[]` in the same document hash;
every expected pair has an evaluated row with its measured values and series pointer; every
relation endpoint resolves to a source in that capture; `relations.coverage` reports
expected/evaluated/observed and `complete` is false when any mandatory unit is unavailable;
an invalid master produces no fresh relations; a re-capture with identical `render_key`s
yields the same relation ids and metrics within tolerance; a capture without references
returns `reference_status="not_supplied"`; a bounded projection returns `complete=false` with
the dropped count and canonical content hash; no separated or intermediate audio is retained.

### D7-02 — Complete identity before adding evidence

**Evidence.** Round-6 D6-13; A §3.8; B §3.6. `panorama.py:178` item rows contain
position/length/take ids only; `panorama.py:1063` analysis rows are 9 positional values;
the digest has `I_PANMODE`/`I_NCHAN` but the panorama does not.
**Current location.** `context/panorama.py::_item_views` and `_metrics_view`;
`scripts/reaper_bridge.lua` snapshot/digest.
**MISSING.** `start_offset_s`, `playrate`, `stretch_markers[]` (positions), fades,
`mute`, `group_id`, `channel_mode`, `pan_mode` in the item row; method/version/stage/
channel/window/units in the analysis row.
**Cost.** Serialization only; byte delta **UNMEASURED** and must be measured on a short
capture. **Failure.** A larger page count; a schema change that older sidecars must rebuild.
**Acceptance.** A split+edge plan's post-apply delta names exactly the changed geometry
fields; an item with playrate 0.5 and one stretch marker round-trips through capture and
read; no edit-critical field is labelled `presented_not_fingerprinted`.

### D7-03 — Implement `render_key` and per-asset `dep_set`

**Evidence.** Round-6 D6-11; 43/43 audio hashes differ with identical digested content;
`grep render_key` in `src/` = 0 implementation hits (B, re-checked here).
**Current location.** **MISSING** in `daw/full_capture.py`; `lifecycle._invalidations()`
(`:261`) is delta-only and coarse.
**Cost.** One JCS hash per asset; negligible.
**Failure.** A key that omits a real dependency (pan law, plugin version) reuses stale
audio; a key that over-includes forces full recaptures.
**Acceptance.** Re-render identical content → same `render_key`, asset reused; change track
pan → that track's key changes and its dependants are marked stale; change one asset in a
two-asset relation → the relation is stale, not silently kept.

### D7-04 — Emit a post-apply structural delta and invalidation, without a capture

**Evidence.** B §3.3/§6.3: `changes_since` requires a base capture
(`lifecycle.py:743-770`); an edit with no capture returns `not_modified`; the measured
clean-session cycle moved `change_count` 163→164 then 213, so edit→delta→edit costs a full
capture.
**Current location.** **MISSING** at apply; `reaper_edit` returns `before`/`after` per
operation and revisions but writes no `chg_` record.
**Cost.** One readback + canonical hash + a small delta document; milliseconds.
**Failure.** A structural delta that advances the token while audio evidence is still
fresh — must be forbidden: signal-affecting edits invalidate dependent audio claims.
**Acceptance.** Two sequential edits with no capture: both apply, the token advances, and
each delta reproduces the new fact root. Any claim citing an asset invalidated by the first
edit is returned `stale`, and a write citing it is refused.

### D7-05 — Build `render_candidate(plan, window)` as the keystone primitive

**Evidence.** A §7.2 and B §7: both workstreams degrade to manual apply/render/undo without
it; `grep render_candidate` = 0 hits. Round-5 X2 (render round-trip cost) was never run.
**Current location.** **MISSING**; nearest is `session.capture_audio` (lease-managed
section render, not plan-bound).
**Cost.** One short render per candidate; **p50/p95 UNMEASURED**. Must reuse the existing
render settings and stage labels, not a new renderer.
**Failure.** A candidate that is not bound to the same stage/window as the base; a lease
that leaves audio behind; a render that blocks the main thread longer than the budget.
**Acceptance.** Returns `{audio_path, sha256, stage, node, channel_view, window,
revision, render_key}`; two calls with the same plan and revision produce the same
`render_key`; no file survives the lease; a plan whose candidate differs from the applied
render is reported `candidate_mismatch`, not "close enough".

### D7-06 — Rename band dominance, add context, compute it as a capture relation

**Evidence.** Hafezi & Reiss 2015 (the builders of autonomous de-masking: objective masking
measures are "ill-suited" for perceived masking); Parker 2022 (best clarity variant uses no
masking model); A §5.1/§6.1. Current producer `temporal.py:803` emits `masker`/`target` at
`min_db=10.0`; consumer `mix_evidence.py:34` still takes `masking_db=6.0`, unreachable
because 10 dB is the producer floor.
**Current location.** `analysis/temporal.py::masking`; `analysis/mix_evidence.py`.
**Cost.** O(S²·B·cells) in the relation domain (D7-01). The canonical pair table keeps every
evaluated pair; the thresholded relation list is a secondary view. The compute is not
separately timed; the capture budget must be measured. **Failure.** Mono downmix ignores
binaural unmasking; hard-panned doubles can look like competition; distortion fusion is
invisible; below ~50 Hz the number is uncalibrated.
**Acceptance.** The capture's `relations[]` carries `kind="band_dominance"`,
`pan_relation` (`same_side` / `opposite_side` / `unknown`), `broadband_delta_db`,
`active_fraction`, `is_psychoacoustic_threshold=false`, method/version, and two resolving
endpoints; a 9.5 dB pair is **present as a `no_dominant_band` evaluated row** (with its
positive/negative deltas) and absent from the thresholded relation list; no literal `6.0`;
no `masker`/`target` keys; no `severity` without a policy divisor; `relations.coverage`
counts all 861 expected pairs as evaluated at 42 sources.

### D7-07 — Keep partial loudness exactly as gated; extend only if E3 passes

**Evidence.** `partial.py:475` implements a reconstruction gate (median ≤1 dB, p95 ≤3 dB),
declared node/stage/channel/window, shares withheld on failure, `equal_share_baseline_pct`
descriptive; 17 tests. A §3.3.
**Current location.** `analysis/partial.py`; `analysis/bundle.py`; `reaper_mix_evidence`.
**Cost.** In the bundle path only; **not timed** on a 300 s bundle.
**Failure.** The gate may fail on every real master; shares are model arithmetic, not
audibility; bus attribution is untested.
**Acceptance.** On one real capture, report gate status at the master and at one bus; on
pass, emit shares with `n_sources` and the equal-share baseline and write them as an
immutable analysis node bound to the same capture/revision; on fail, `status="unavailable"`
with the measured residual and no share key anywhere, and the `gate_failed` record is
present in the panorama rather than an absent field.

### D7-08 — Texture: rename the misnamed scalar now, defer the block to on demand

**Evidence.** McDermott & Simoncelli 2011 (cross-channel correlation required); McWalter &
Dau 2017 (cascaded AM); A §3.6: the current `modulation_depth` is a whole-file
spectral-concentration share, not AM depth. Cost ARITHMETIC: 10–20 s per 300 s asset,
+14–29 % on the DSP sum if added to capture.
**Current location.** `analysis/temporal.py::_modulation` (`:313`). **MISSING**: a texture
module.
**Cost.** On demand: 10–20 s single-threaded per asset, no render. **Failure.** A texture
number reads as a verdict; window length is a listening question; statistics over 100 ms
cells are thin.
**Acceptance.** `modulation_depth` is renamed or documented as `envelope_peak_share`;
when the on-demand view exists, white noise and a 1 kHz tone differ by ≥0.3 in mean
off-diagonal cross-band correlation; the same material at 2 s and 4 s reports both windows;
a gain-only change leaves every statistic unchanged within a stated tolerance; no threshold
or verdict key; when computed it is written as an immutable analysis node with
`analysis_id`, the same capture/revision, input asset ids, method/version and a panorama
link — never a loose result.

### D7-09 — Make the corpus model the single model-facing reference mechanism

**Evidence.** A §3.5/§5.5; measured profile: gain-invariant 1.000; tilt/width/air/mono
1.7–38.8×; `compressed` 0.618/1.009 (not detected); `clipped` 0.355 (worse than chance).
Two mechanisms currently coexist: `analysis/compare.py::reference_corridor` (model-facing,
no stage/section/n gate) and `corpus/` (CLI only, measured). `scripts/reference_delta.py:47`
prints Russian direction hints; `daw/cycle.py` minimizes distance to the corpus.
**Reference relations in the panorama.** When a reference manifest is supplied to the
capture, `relations[]` carries `kind="reference_comparison"` edges naming the reference
identity, stage, section, window and method; when no manifest is supplied, the panorama
returns `reference_status="not_supplied"`. The corpus model remains the only ranking
mechanism; the per-metric corridor either gains the same gate and returns a rank, or stops
being model-facing.
**Current location.** `corpus/model.py` (Ledoit-Wolf), `corpus/report.py` (leave-one-out
percentile), `scripts/corpus_*.py` (CLI).
**Cost.** One window measurement per assessment (<1 s by feature definition;
**UNMEASURED** wall time); in-capture reference edges add one measurement per reference per
window plus their bytes. **Failure.** Compression/clipping blindness; section typing absent,
so `section_type=unknown` is common; a corpus median is not a track; a reference relation
without a matching stage is worse than none.
**Acceptance.** A manifest with mixed stages refuses unless a stage is selected; fewer than
12 windows returns `insufficient_reference_windows`; a gain-only change moves distance by
<1e-6; a compression change is reported **not detected**; every reference edge names the
reference id, stage, section, window and method; no assessment contains a score;
`reference_delta.py` is not reachable from any tool; `cycle.py` no longer minimizes
distance to zero.

### D7-10 — Separation stays on demand, analysis-only, never retained

**Evidence.** HT Demucs (4 stems, 9.00 dB SDR), BS-RoFormer (4 stems, 9.80 dB);
`work/role_assess.json` reconstruction correlation 0.99967, residual −29.86 dB on the
owner's own stems (a gate baseline, not a separator result); A §5.3.
**Current location.** **MISSING**; `corpus/roles.py` can read user-supplied stems.
**Cost.** **UNMEASURED** on the V100; 42 invocations + ~3.4 GB derived audio per full
project if ever run.
**Failure.** "Other" absorbs all guitars and cymbals; role bleed; a −29.9 dB residual is
material for band ratios near −30 dB; checkpoint licence unresolved for BS-RoFormer.
**Acceptance.** Reconstruct ≥ −30 dB and correlation ≥0.999; a role comparison refuses on
separator/version mismatch or failed reconstruction; no separated file survives the lease;
a label not produced by the separator is rejected (never relabel "other" as "guitar"); when
computed, role evidence is an immutable analysis node bound to the same capture/revision
with method/version and input asset ids, linked from the panorama.

### D7-11 — No neural quality judge in production

**Evidence.** Colonel & Reiss 2019 ("virtually no correlation" between MIR features and
preference); Audiobox Aesthetics has no metal evaluation and predicts four axes, not intent;
Fx-Encoder++ is CC BY-NC 4.0 and needs an instrument query (S-A7); A §5.4/§5.7.
**Current location.** None; `mix/models.py` already carries `not_a_verdict` and a
two-alternatives rule.
**Cost.** Audiobox 103.86M params F32; **VRAM UNMEASURED**. **Failure.** A second judge
beside the owner; a score that silently becomes an objective.
**Acceptance.** No serialized capture/analysis/claim payload contains a quality score,
`better`/`worse`, or a distance-to-zero field. Any future validator must pass a
monotonicity test on held-out material first, and its output must be labelled a prediction.

### D7-12 — One event record, two producers, authority enforced mechanically

**Evidence.** `event_model.py` defines `Confidence` (`:200`, bases interval/rank/agreement/
none) and `UsageScope` (`:162`), and nothing constructs or reads them; `events.py` and
`transients.py` emit two dict formats; `edit_map._confidence` is a composite with no basis.
B §5.1/§6.
**Current location.** `analysis/event_model.py`, `analysis/events.py`,
`analysis/transients.py`, `analysis/edit_map.py`.
**Cost.** No new DSP; the detectors already run inside the measured pass.
**Failure.** Heavy compression removes the dip; cymbal wash holds the envelope; 30 ms
clustering merges 32nds at ≥250 BPM; a double-track flam is averaged.
**Acceptance.** An onset-only event cannot authorize `item.split`; two agreeing sources
(`basis="agreement"`, `n≥2`) can; a plan citing no events remains valid but claims no event
evidence; an audio-only event cannot authorize a MIDI write.

### D7-13 — `item.edge` with `D_STARTOFFS`, full geometry readback, `expected_after`

**Evidence.** `EDITING-ACCEPTANCE.md:30-36` (69/27/22 ms; attacks must not move); B §5.7;
`D_STARTOFFS` is read (`lua:381`, `:755`) and copied (`:3639`) but never written;
`read_edit_state_after(item.split)` returns `item_count` (`:3103-3106`).
**Current location.** `scripts/reaper_bridge.lua`; `daw/editing.py` (`cut_plan`,
`apply_cut_times`); `daw/session.py` transaction path.
**Cost.** Geometry only; no render. **Failure.** Fade shape from `D_FADEINLEN` is
REAPER-dependent; source exhaustion at the left edge; group sync; stretch markers.
**Acceptance.** On a disposable copy: left edge −0.069 on an item at 10.0/2.0/0.5 reads back
9.931/2.069/0.431 ±1 sample; the attack at 10.500 stays at 10.500 ±1 sample; item count
unchanged; split at 11.0 with tail 0.130 / move 0.100 / trim 0.025 reads back overlap 5 ms
±1 sample and fades equal to the overlap; undo restores the pre-edit digest exactly.

### D7-14 — Tempo/grid: remove the 120 BPM fallback, keep the map as intent

**Evidence.** `TZ.md` §3/§5; `EDITING-ACCEPTANCE.md:25` forbids blind quantization;
`FULL-CAPTURE.md:58-61` forbids a single base BPM; B §5.3: `edit_map.tempo_grid` emits bare
beats and falls back to 120 BPM; no meter/downbeat; `alignment.propose_moves(strength=1.0)`
still defaults to full quantization.
**Current location.** `analysis/tempo.py`; `analysis/edit_map.py::tempo_grid`;
`daw/alignment.py`.
**Cost.** Trivial CPU. **Failure.** Half/double time; 3/4 vs 6/8; a ramp inside one window;
beat-attached items moving on an unrelated tempo edit.
**Acceptance.** A synthetic 120→140→120 click is recovered within ±0.5 BPM per segment; with
an empty tempo map every grid row carries `grid_basis="absent"` and no `offset_ms`; a 3/4
vs 6/8 ambiguity returns both; a tempo-changing plan enumerates every beat-attached item or
refuses.

### D7-15 — Stretch is refused by default; when requested, verify markers and attacks

**Evidence.** Driedger & Müller 2016 ("no single TSM method can cope with all kinds of audio
signals equally well"); `EDITING-ACCEPTANCE.md:36` forbids stretch for the owner's case;
the bridge can write stretch markers (`lua:2336-2363`) and read positions (`:403-406`), but
the edit readback returns count.
**Current location.** `item.set stretch_ratio` / `stretch_markers` in the bridge;
`read_edit_state` (`:2946-2947`).
**Cost.** One render per verification. **Failure.** Doubling/skipping, smearing, transient
re-insertion error, marker round-trip loss.
**Acceptance.** A 2 s guitar item stretched by 1.05 keeps attack count identical to the
pre-edit render, every matched attack within ±2 ms, `playrate` unchanged on every other
item, and marker positions read back exactly (not just the count). If markers do not survive
save/reload, `item.edge` refuses items with markers in v1.

### D7-16 — FX: apply only a bounded 1–3 parameter search; never a 1,600-render default

**Evidence.** ST-ITO (gradient-free, 1,600 evaluations, series chain, user-supplied);
ITO-Master (Θ-fitting only under content identity); Yu et al. (unconstrained ITO produces
"unrealistic configurations"); Wright & Välimäki (structured compressor at <10 % ops);
B §5.5. Current: limiter ≤34 renders; EQ calibration only; corpus cycle must not be
generalized.
**Current location.** `daw/limiter.py`, `daw/eq.py`, `daw/delivery.py`, `daw/cycle.py`.
**MISSING.** `fx_search` with `max_evaluations`, prior, plausibility, top-k, budget status.
**Cost.** Per render; at 98.47 s per 42-track 300 s batch, 1,600 full-mix renders ≈43 h
(ARITHMETIC); short-window cost **UNMEASURED**.
**Failure.** Non-monotone parameters; automation masking a write; plugin latency changing
the clock; a preset changing topology.
**Acceptance.** On a synthetic gain plugin, recover a target within ±0.3 dB in ≤12 renders
and report the count; a non-monotone parameter terminates with
`status="no_monotone_relation"` and writes nothing; a budget of N makes exactly N renders
and returns `status="budget_exhausted"` with the best-so-far and its index; after apply,
every other parameter and the automation envelope are bit-identical.

### D7-17 — Panning is apply-with-protection; grouping is suggest-only with need evidence

**Evidence.** Ronan, Gunes & Reiss 2017 (verified in full by B: panning first, subgrouping/
EQ/DRC second; need-driven, not genre-driven; "it's not the genre, but the amount of
information"); Perez-Gonzalez & Reiss 2010/2007 (rule-based panning is a fallback, not
artistic). A §6.1: hard-panned doubles are the case the pan literature did not test.
**Current location.** `track.set pan` (readback `D_PAN`); digest has `I_PANMODE`/`I_NCHAN`;
no pan law in the plan path, no protected set, no grouping evidence.
**Cost.** No render for pan; one or two for a bus.
**Failure.** Destroying intentional doubles; a pan-law change altering everything
downstream; mono collapse; folder depth changing routing.
**Acceptance.** −1.0 → +1.0 reads back exactly and a mono-sum measurement reports
Δcorrelation and Δmono level; a plan panning a declared double is refused unless the pair is
named; a bus-creation plan cites at least two measured need signals and states the routing
delta; a post-folder master render null-tests against a manually built reference.

### D7-18 — Governance: no score, no recipe, no distance-to-zero, always two alternatives

**Evidence.** AES 147th e-Brief 526 (near-zero feature↔preference correlation); `TZ.md` §6;
A §6.6; `scripts/reference_delta.py:47` and `daw/cycle.py` are live counter-examples;
`mix/models.py` already has the two-alternatives rule.
**Current location.** Policy-level; defects in `daw/cycle.py`,
`scripts/reference_delta.py`, `mix_evidence.py`.
**Cost.** None. **Failure.** A threshold or adjective silently becomes a verdict.
**Acceptance.** A repository scan finds no direction/recipe strings in model-facing code;
no serialized claim contains `better|worse|good|bad|score`; every relation names at least
two live alternatives and one discriminating test; `cycle.py` is not reachable as a general
FX surface.

---

## 5. Hypotheses that require an experiment (B)

These cannot be settled by reading, and each has a defined outcome that changes a decision.

| ID | Hypothesis | Experiment | What changes if it fails |
|---|---|---|---|
| H7-01 | The 2 ms envelope and STFT-flux detectors reach F1 ≥0.9 at ±25 ms on amp-print guitars | 2 × 30 s of the owner's tracks, two annotators, F1 at ±10/±25/±50 ms | Below 0.9 every event-driven edit inherits the error; event authority stays suggest-only |
| H7-02 | Band dominance at 10 dB predicts an audible change on a same-side double | Forced-choice, 1 dB steps, ~20 trials/level, same-side pair | Non-monotone → locator only, no threshold, no severity |
| H7-03 | Partial loudness passes the reconstruction gate on the real master and on one bus | One real capture: sum(stems) vs node per band; report both gates | If master fails but bus passes, attribute at the bus; if both fail, shares stay unavailable |
| H7-04 | A 2–4 s texture window separates a fuzz wall from a tremolo figure in a way the model uses | Window sweep 2/3/4 s on two excerpts; blind model choice with/without the block | If no decision changes, do not build the texture view at all |
| H7-05 | The corpus model's compression blindness can be compensated by a direct dynamics gate | Add crest/LRA/PLR gates beside the corpus rank; test on compressed/degraded pairs | If not, the corpus stays a similarity rank and never enters an FX loop |
| H7-06 | A short-window candidate render costs little enough for 10² evaluations | 100 renders of a 5 s section through the capture path; p50/p95 | Sets the FX budget; if >2 s/render, cap evaluations at tens |
| H7-07 | Stretch-marker positions survive save/reload exactly | Place markers, save, reload, read | Gates every stretch-verification claim; failure forces edge refusal on marked items |
| H7-08 | Group-synced items follow an API split | Split one of two grouped items; read the other | If not, plans expand groups explicitly |
| H7-09 | 30 ms clustering is safe at 250–300 BPM | Synthetic 300 BPM 32nd click; count merged clusters | If merged, clustering becomes tempo-dependent |
| H7-10 | REAPER's fade shape is pinnable per item | Set `C_FADEINSHAPE`/`D_FADEINDIR*`, render, compare with preview | If not, preview cannot be exact; the acceptance test keeps a tolerance |

H7-01, H7-02, H7-06, H7-07 and H7-10 are CPU-only. None requires the V100.

---

## 6. Unanswered and unknowable (C)

1. **Whether any masking metric is perceptually valid for this repertoire.** No study; the
   two groups that built working de-masking systems report the objective measure does not
   track perceived masking. This is why D7-06 forbids a gate.
2. **Whether a monotone mix-style objective exists.** Only negative evidence (AES 147th).
3. **VRAM/throughput of separation or any embedding model on the V100.** Nothing was run;
   no number may be quoted.
4. **Whether added panorama fields improve a model's decisions.** Delivery is testable;
   comprehension is an empirical acceptance run, not a schema property.
5. **The cause of master PCM nondeterminism (~1.19e-7 FS) and of 0/43 equal container
   hashes.** Unproven; `render_key` is the correct workaround, not a proof of equality.
6. **The split of the 149.9 s packaging tail after the new pipeline.** Two of its three
   largest components still have no timer.
7. **Whether the owner accepts a capture-per-edit.** If not, D7-04 is a release blocker.
8. **What "the same mix, better" means for this owner.** Only his ears; the library must
   never encode it.

---

## 7. Rejected candidates (R)

| ID | Candidate | Why rejected | What would reopen it |
|---|---|---|---|
| R7-01 | Any learned mix-quality head / Audiobox PQ as a judge | Near-zero feature↔preference correlation; no metal evaluation; creates a second judge | A held-out monotonicity test on this owner's material, used only as a labelled prediction |
| R7-02 | Fx-Encoder++ for chain/parameter recovery | CC BY-NC 4.0; instrument query presupposes separation; synthetic series chains; weak parameter matching | An MIT/Apache checkpoint and a 4-chain × 5-riff top-1 ≥0.8 experiment |
| R7-03 | Per-instrument FX claims from a stereo reference | 4-stem taxonomy; "other" is all guitars; attribution not identifiable | A separator that outputs the actual instrument with measured bleed below a declared threshold |
| R7-04 | Separation in full capture or retained stems | Cost unmeasured; 3.4 GB derived audio; retention contract forbids it; 4-stem roles | Never for capture; only a lease-managed on-demand analysis |
| R7-05 | MPEG-1 Model II masking as the primary clarity feature | O(S²·F·B); tonality assumption violated by dense guitars; builder's own negative result; best clarity variant uses no masking model | A validated masking model for this material, with a listening JND |
| R7-06 | A single whole-mix clarity score | No source attribution; silently becomes a verdict | Nothing — contract-forbidden |
| R7-07 | Distance-to-corpus as an optimization objective (`daw/cycle.py`) | Median vector is not a track; blind to compression; produces the blandest mix | Only a rank/percentile gate with a direct dynamics gate beside it |
| R7-08 | 1,600-render search as a default | ≈43 h full-mix; short-window cost unmeasured | H7-06 showing a candidate render cheap enough, plus an explicit user budget |
| R7-09 | Automatic quantization / automatic panning | `TZ.md` §5, `EDITING-ACCEPTANCE.md:25`; pan literature is a fallback for unidentified sources | Explicit user intent only; never a default |
| R7-10 | Texture or model analysis unconditionally in every capture | +14–29 % DSP for texture; 297 KB short-window panorama; no decision evidence. **The earlier draft of this row also rejected relational blocks; that is withdrawn** — the mandatory deterministic core belongs in every capture (§1.3) | H7-04 showing a decision change, plus a measured byte budget; optional nodes remain cost-gated |
| R7-11 | A reduced/lossy panorama | Round-6 owner-rejected; drops the 30 s level and provenance | Nothing — owner-binding |
| R7-12 | Detached relational analysis outside the capture (the bundle-only path as an endpoint) | It cannot be bound to the capture/revision it cites; the owner requires one coherent panorama; the current split is the gap, not the design | Replaced by the mandatory core (D7-01) plus optional immutable analysis nodes (§1.3) |

---

## 8. Cross-workstream dependency contract

Every capability in this round is bound by the same tuple. A claim that cannot fill it is
`incomparable`, not "approximately the same".

```
project_id, epoch, revision(change_count), fact_root,
capture_id, asset_sha256 | render_key,
stage, node, channel_view, window [start_s, end_s),
method, method_version, params, units,
revision_pair (before, after) when comparing
```

1. **Same capture/revision/window/stage.** Before/after is a new measurement at a new
   revision, never a delta to the old one without both revisions.
2. **Tempo map is intent.** Every window carries project seconds and bar/beat from the
   revision-bound map. No base BPM, no 120 fallback, no beat label without a map.
3. **Processing state and routing.** A solo track render is not the master; a nonlinear node
   needs the reconstruction gate; a pan or channel-mode change invalidates a band difference
   even with the same fader.
4. **Full-resolution time axes.** The 30 s and 300 s levels and any finer captured level are
   preserved. New evidence declares its own grid; it never resamples silently.
5. **Provenance.** Every measurement names method/version/stage/channel/window/units and the
   capture or asset it came from. Model outputs name checkpoint and inference status.
6. **Invalidation.** Derived views and edits write a `dep_set`
   (`capture_id, revision, asset_ids, method_family`). Relations cite two assets, so
   per-asset-only invalidation is insufficient.
7. **No retained stems.** Separation and any render derivative are lease-managed; the
   archive keeps the capture, not the intermediates.
8. **No universal score, no genre recipe.** The corpus may be metal-heavy as material; its
   distribution never becomes a per-genre target; no direction strings in any tool.
9. **Mandatory core, optional nodes, one archive.** The deterministic relation core is in
   every full capture. An optional calculation (texture, separation, embeddings) that is run
   becomes an immutable analysis node with `analysis_id`, the same
   `project_id`/`revision`/`capture_id`, its input asset ids, method/version, window and
   `dep_set`, and a typed link from the panorama. There is no second, detached relational
   archive and no loose intermediate audio.

---

## 9. Near-term implementation sequence

Ordered by dependency and by blind-spot reduction per unit of work. Each gate is falsifiable
and must pass before the next step.

### P0 — Identity and invalidation (no DSP, no quality risk)

1. Item rows: offset, playrate, markers, fades, mute, group, channel mode, pan mode
   (`context/panorama.py`; bridge snapshot/digest).
2. Analysis rows: method/version/stage/channel/window/units (`panorama.py:1063`).
3. `render_key` + `dep_set` (`daw/full_capture.py`; `context/lifecycle.py`).
4. Post-apply structural delta + invalidation records (`reaper_edit` apply path).

**Gate P0.** Two sequential edits with no capture advance the token and reproduce the fact
root; a pan edit changes that track's `render_key` and marks its dependants stale; a claim
on a stale asset is refused; a capture of the reference project still succeeds with the same
domain coverage.

### P0.5 — Mandatory deterministic relation core in the capture

5. `relations[]` is in the record schema and panorama: stable typed `relation_id`,
   `kind`, endpoints, window, stage, node, channel view, method/version, raw metrics,
   coverage, uncertainty, `dep_set`.
6. The core is computed from the capture's own assets after all assets are admitted and
   before the record is sealed (`daw/full_capture.py::_build_record`); the panorama reuses
   that domain. It contains source↔source band dominance (one evaluated columnar row per
   pair, including `no_dominant_band` with measured best deltas and an exact pair-series
   pointer), source↔source phase when available, and source↔master level relation. Report
   expected/evaluated/observed/unavailable per kind; no top-K cut is canonical.
7. Reference edges: only from a supplied manifest, each naming reference id, stage, section,
   window and method; otherwise `reference_status="not_supplied"`.
8. Optional analysis nodes (texture, separation, embeddings, gated partial loudness) are
   **not** in this phase. When they are later computed they enter as immutable `analysis`
   nodes with `analysis_id`, the same capture/revision, input asset ids and a panorama
   link — never as a detached result.

**Gate P0.5.** One capture returns both `sources[]` and `relations[]` under one document
hash; every expected pair has an evaluated row with its measured values, status and series
pointer; every endpoint resolves; coverage reports expected/evaluated/observed per kind and
`complete=false` when any mandatory unit is unavailable; a 9.5 dB band pair appears as a
`no_dominant_band` row and not in the thresholded relation list; a phase-inverted pair is
present when phase was measured; a track↔master relation cites both assets; a capture with no
reference returns `reference_status="not_supplied"` rather than an empty list; a re-capture
with identical `render_key`s keeps the same relation ids and metrics within tolerance; a
bounded projection is marked `complete=false` with its dropped count and cannot be
acknowledged as complete; the panorama byte count and relation-domain wall time are recorded
on a 12 s capture and on the 300 s class before the domain is enabled by default.

### P1 — The edit primitive

9. `render_candidate(plan, window)` with a lease and stage binding.
10. `item.edge` writing `D_STARTOFFS`; full geometry readback; `expected_after`;
    `UsageScope` enforcement.

**Gate P1.** The ORIG case passes (9.931/2.069/0.431 ±1 sample; attack at 10.500 unchanged;
split overlap 5 ms; undo restores the digest); an onset-only event cannot authorize a split;
a candidate render is reproducible and leaves no file.

### P2 — Honest perception and reference surface

11. Band-dominance detail view over the capture relations; delete the 6.0 dB path; no
    severity without policy.
12. Retire `scripts/reference_delta.py` from any tool path; remove the distance-to-zero
    objective in `daw/cycle.py`; wrap the corpus model behind a manifest/stage/n gate and
    emit reference edges into the capture panorama when a manifest is supplied.

**Gate P2.** Payload schema test (pan relation, no masker/target, no 6.0); corpus assessment
on a gain-only change <1e-6 and on compression "not detected"; every reference edge names
its reference id, stage, section, window and method; repository scan finds no recipe strings
and no score key.

### P3 — Tempo, stretch, panning protection

13. Remove the 120 BPM fallback; `grid_basis="absent"`; meter alternatives; beat-attached
    enumeration before a tempo edit.
14. Stretch refusal by default; marker-position readback; attack-count verification.
15. Protected hard-panned pairs; grouping suggest-only with need evidence.

**Gate P3.** Synthetic 120→140→120 within ±0.5 BPM; no `offset_ms` without a map; a 1.05
stretch keeps attack count and ±2 ms positions; a double-pan plan is refused unless named.

### P3.5 — Optional analysis nodes, deferred but never detached

16. When texture, separation or an embedding is computed, it enters as an immutable
    `analysis` node: `analysis_id`, `project_id`, `revision`, `capture_id`, input asset ids,
    method/version, window, units, `dep_set`, and a typed panorama link. No parallel
    archive, no loose WAV, no result that outlives its capture. Each node remains
    cost-gated by its own measured budget.

**Gate P3.5.** An optional node resolves from the panorama to the same capture and revision;
deleting the capture invalidates it (`stale`, not silently kept); no file survives the
lease; a node without `capture_id` or method/version is refused at write time.

### P4 — Measurement, then packaging

17. One full-song capture on the current tree with the new timers (relation stage, seal,
    archive, panorama, response), and a short-window candidate-render timing (H7-06).
18. Only then decide the packaging rewrite; texture and separation stay deferred and are
    scheduled only if H7-04/H7-07 justify them.

**Gate P4.** A full-song wall-clock with every stage timed; the 703 s baseline formally
retired; no speedup claim made before this exists.

**Do not do in this phase:** add texture/separation to every capture; build a general FX
search; implement a quality model; rewrite packaging before the timers; re-run profiling in
this research round.

---

## 9.5 Appendix — separate implementation follow-up (not part of this research brief)

A later request was misrouted to this research session and produced implementation work in
the same worktree. It is recorded here only for provenance and is **not** evidence for any
Round 7 research decision, not part of the two-workstream brief, and not a claim that the
research phase edited source or tests. The research deliverables remain the two audits, this
synthesis and the search log. Nothing in this appendix changes the decisions in §§4–9.

The separate follow-up produced a deterministic relation core:

- `context/relations.py` — `RelationBinding`, `RelationWindow`, `RelationRecord`,
  `RelationSet`, `derive_relation_id`; ids derive from typed endpoints + window +
  stage/node/channel + method/version + render key/revision.
- `analysis/relations.py` — all-pairs band-dominance table (one row per expected pair,
  statuses `evaluated` / `no_dominant_band` / `unavailable`), per-row `series_ref` to the
  aligned source band-level series plus an explicit transform, thresholded candidates,
  source-master level relation (renamed from "contribution"), reference candidates.
- `analysis/relations.py` + `context/relations.py` — table rows receive a derived
  `relation_id`, including canonical `no_dominant_band` rows; top-K is absent as a
  completeness rule and a bounded view must declare itself partial.
- `context/panorama.py` — `_relations_domain()` builds the domain from the sealed
  record's own per-source analysis; `build_relation_domain()` is the pre-seal entry point.
- `context/full_capture.py` — `FullCaptureRecord.relations` is part of the record payload
  and integrity hash; its binding must match capture/project/window.
- `daw/full_capture.py` — `_build_record()` computes the domain before sealing and records
  `relations_s` plus pair count in timings; the panorama reuses the record domain instead
  of recomputing it.
- `context/lifecycle.py` — pages/ack include the full relation domain; `relations_summary`
  is a structural slice key; delta fact view strips capture-local IDs/hashes but keeps
  relation metrics, statuses and coverage.
- `context/ids.py` — `rel` is a derived prefix.

Acceptance run: `PYTHONPATH=src python -m pytest -q` → **929 passed, 2 skipped**. The
focused `tests/test_relations.py` run is **19 passed** (fresh pytest process, not
a `.pyc` artifact). The
fixture capture (2 sources) produces a 36,636-byte panorama of which the relation domain is
9,397 bytes; 1 evaluated pair row, 2 source-master relations, reference absent. **No
full-song capture wall time exists yet.**

A synthetic all-pairs run at the current 42-source shape (42 × 28 bands × 3,005 cells, random
levels) took **3.33 s** in `band_dominance_candidates` after precomputing per-source
normalized matrices, and produced **261,898 B** of columnar table including per-row
`dep_set`. Input/prepared float64 matrices are ~28.3 MB each (filled + normalized) plus a
boolean active mask; peak Python memory is not measured. That is a real cost signal on the
full-song class, not a capture measurement: it must be scheduled outside the render wait
before the domain is enabled by default. At the 12 s class the same shape is ~120 cells and
costs roughly an order of magnitude less. No GPU, render, REAPER session or profiler rerun
was used for this measurement. No profiler or benchmark process was running; no worker,
render or benchmark setting was changed. The capture/panorama files were changed only to
bind the relation domain, not to alter profiling policy.

## 9.6 Owner requirement — complete panorama before interpretation (post-dispatch)

This requirement postdates the Round 7 dispatch and is accepted as an owner rule. It does not
change the literature conclusions; it changes the interface contract that delivers them.

**Rule.** After the bootstrap full capture, the client/model must fetch **every deterministic
page** of that same panorama and verify the exact document hashes before it interprets project
audio, project measurements or relationships, or before it edits. Page 0 is a manifest plus
one page, not the document. The initial capture/DSP operation is exempt because it creates the
evidence document; otherwise the rule would be circular.

**Required sequence.**

1. Structural bootstrap read (`reaper_project`) to establish `project_id` and the exact
   revision, with an explicit `panorama_gate.complete=false`.
2. `reaper_capture_snapshot` creates the revision-bound full capture. It is allowed without a
   prior receipt.
3. `reaper_panorama_open` returns page 0, handle, revision, fact root, document hash and the
   page-hash manifest.
4. The client fetches every page index `0..page_count-1` on that handle and checks each page
   hash against the manifest.
5. `reaper_panorama_ack` mints the complete-view receipt/context token. A partial set returns
   `incomplete` with `missing_pages` and mints nothing.
6. Only then may project-target analysis/interpretation tools or mutations run. The receipt is
   bound to project, capture, revision and fact root; a new capture, revision or root makes it
   stale.

**Server-enforced scope.** `reaper_analyze`, `reaper_timeline_context`, project-target
`reaper_spatial_analyze` / `reaper_spatial_timeline`, project-target
`reaper_flamingo_describe` / `reaper_flamingo_ask`, `reaper_mix_evidence`,
`reaper_edit_map`, `reaper_panorama_slice` and `reaper_capture_read` require a
complete receipt. For `reaper_capture_read` the receipt must match the exact
capture being read, including its project, revision, fact root and document
hash; a latest-capture receipt is not a fallback. `file:` targets remain allowed
because they do not interpret the current project. Existing mutation gates
remain: `reaper_edit` apply/undo, `reaper_api_call`, `reaper_action` invoke and
`reaper_save`. The bootstrap `reaper_capture_snapshot` returns metadata only,
never the panorama payload. The server cannot observe private model reasoning;
it can only gate tool calls and say so honestly in the guide.

**Acceptance gate.** A missing receipt yields a `panorama_capture_read` validation
error without a panorama payload; a partial page set yields no receipt; a receipt
from another project/capture/revision/fact root/document hash is rejected; a
receipt for a historical capture is validated against that capture, not the
latest capture; the bootstrap capture succeeds without a receipt but returns no
panorama payload; file-only analysis succeeds without a receipt; a valid exact
receipt allows the read. The RU/EN guide must state the sequence and the “no
conclusions before complete receipt” rule.

**Interval evidence is not the whole capture.** `reaper_panorama_slice` keeps
`state.relations_summary` project-scoped. Its interval evidence must be recomputed from the
same capture's stored source series for the requested window, or returned as explicit
per-relation unavailable; a whole-capture relation value must never be labelled as an
interval value.

## 10. What this round intentionally did not do

During the research phase:

- No source, test, README, REAPER project, capture or benchmark file was modified.
- No test, render, REAPER session, package install, download or GPU job was run.
- No new profiling: the performance section only reconciles existing artifacts.
- No claim that any speculative neural model is production-ready.
- No universal mix-quality score and no genre recipe is introduced or endorsed.

The owner-directed implementation described in §9.5 came afterwards and touched only the
relation modules, the panorama integration and their tests; it did not edit the capture,
render, worker or benchmark paths.

Round-5 decisions that remain unimplemented are tracked in §2.1 rather than silently
dropped.
