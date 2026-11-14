# REAPER Workbench — implementation blueprint (R3)

**Status:** design only. No production code, tests, configuration, REAPER project or media were changed.
**Baseline:** branch `main`, HEAD `86b14bb5136c9a0b00be4d6948986511be2d9674` (2026-11-12 09:04:36 +0300).
**Shared dirty tree (preserved):** `docs/AI-MIX-SURFACE.md`, `docs/TZ.md`, `scripts/reaper_bridge.lua`; untracked `docs/EDITING-ACCEPTANCE.md`, `docs/research/`.
**Inputs:** six final R2 reports, six final R3 reports (`work/research/round-3/reports/R3-01…R3-06`), the R3 readiness contract, and the actual source/tests.
**Not a claim of completion.** A later phase still has to write code, run the live tests and collect human ratings.

---

## 1. What is being built

A library that lets a text model:

1. establish which REAPER project and revision it is looking at;
2. obtain stage-correct audio evidence and measurements tied to exact objects, windows and revisions;
3. turn signal evidence into cautious events, notes, time/structure hypotheses and mix claims, each with provenance and alternatives;
4. assemble a compact, navigable context instead of a giant report;
5. perform typed, revision-bound operations with `expected_after`, real readback and undo;
6. re-capture the same stage/window and verify the musical result;
7. refuse or ask when evidence is missing, stale or contested.

The architecture is deliberately **not** a genre pipeline and not a graph database. It is graph *semantics* on a relational core plus an append-only evidence/action ledger (R3-05).

---

## 2. Canonical architecture

### 2.1 New modules (proposal until accepted)

| Module | Owner | Responsibility |
|---|---|---|
| `src/reaper_audio/timebase.py` | R3-05/R3-03 | `TimeBasis`, `TimeRef`, `Interval`, `TimeMap`, composition, `sample_index`, `mapping_status` |
| `src/reaper_audio/context/schema.py` | R3-05 | envelope, `record_kind`, `RevisionRef`, `schema_version`, JSON Schema export |
| `src/reaper_audio/context/audio.py` | R3-01 | `Stage`, `CaptureMethod`, `SourceKind`, `ChannelView`, `DeliveryStatus`, `SignalWindow`, `ObjectRef`, `CaptureRequest`, `Capture`, `AudioAsset`, `ListenRequest`, `ListenResult`, `DeliveryEvidence` |
| `src/reaper_audio/context/ids.py` | R3-05 | ID registry, deterministic hash for derived records, ULID for authored records, tombstones |
| `src/reaper_audio/context/ledger.py` | R3-05 | append-only JSONL evidence/action journal |
| `src/reaper_audio/context/store.py` | R3-05 | SQLite projections, migrations, rebuild from ledger |
| `src/reaper_audio/context/deps.py` | R3-05 | `dep_set`, scoped staleness, revalidation |
| `src/reaper_audio/context/claims.py` | R3-05 | claim/hypothesis/intent lifecycle and evidence grades |
| `src/reaper_audio/context/anchors.py` | R3-05 | resolve ID/name/role/time/text to entities; ambiguity groups |
| `src/reaper_audio/context/manifest.py` | R3-05 | compact overview, stable ordering, token budget |
| `src/reaper_audio/context/views.py` | R3-05 | addressable views, cursor pagination, omission ledger |
| `src/reaper_audio/context/assembler.py` | R3-05 | goal → `ContextEnvelope` |
| `src/reaper_audio/context/audio_handoff.py` | R3-05/R3-01 | audio asset handoff and delivery proof |
| `src/reaper_audio/analysis/event_model.py` | R3-02 | event/occurrence/gesture/score types and invariants |
| `src/reaper_audio/analysis/event_build.py` | R3-02 | producers from DSP events, transients, project snapshot, MIDI |
| `src/reaper_audio/analysis/event_policy.py` | R3-02 | tolerances, eligibility resolver, abstain reasons |
| `src/reaper_audio/analysis/event_index.py` | R3-02 | compact index, filters, cursor |
| `src/reaper_audio/analysis/project_time_map.py` | R3-03 | tempo/time-signature map, bar/beat conversion |
| `src/reaper_audio/analysis/rhythm_map.py` | R3-03 | pulse/level/meter/bar/grid relation, groove |
| `src/reaper_audio/analysis/structure.py` | R3-03 | riff/section/repetition/variant/cadence/tail |
| `src/reaper_audio/analysis/edit_intent.py` | R3-03 | `EditIntent`, `Correspondence`, protected regions, preview rows |
| `src/reaper_audio/analysis/score_time.py` | R3-03 | PPQ ↔ musical time, beat-attach policy |
| `src/reaper_audio/mix/models.py` | R3-04 | `MixScope`, `MixObservation`, `MixClaim`, `IssueHypothesis`, `PostChangeCheck` |
| `src/reaper_audio/mix/scope.py` | R3-04 | stage/channel/level binding |
| `src/reaper_audio/mix/constructs.py` | R3-04 | C1–C19 construct catalogue as data |
| `src/reaper_audio/mix/references.py` | R3-04 | reference cases/sets, comparability, LOO |
| `src/reaper_audio/mix/claims.py` | R3-04 | claim builder |
| `src/reaper_audio/mix/severity.py` | R3-04 | provisional severity, calibration hooks |
| `src/reaper_audio/mix/causes.py` | R3-04 | causal hypotheses and discriminating tests |
| `src/reaper_audio/mix/verify.py` | R3-04 | post-change check against the target construct |
| `src/reaper_audio/mix/actions.py` | R3-04 | action candidates with expected evidence change |
| `src/reaper_audio/daw/verify.py` | R3-06 | readback vs `expected_after`, tolerances |
| `src/reaper_audio/daw/revision.py` | R3-06 | revision predicates, stale gates |

New packages must not duplicate domain types. `models.py` remains the owner of `Section`, `AudioBuffer`, `MetricValue`, `MeasurementReport`. `context/audio.py` imports `RevisionRef`/`TimeRef` from `context/schema.py`/`timebase.py`; `analysis`, `daw` and `mcp_server` import from there. A local `AudioAsset` in `session.py` or `mcp_server.py` is forbidden.

### 2.2 Existing modules to extend

| Current symbol | Role after R3 |
|---|---|
| `src/reaper_audio/daw/session.py::ProjectSession.capture_audio` | returns typed `Capture` + `AudioAsset[]`; `source_revision: RevisionRef` |
| `src/reaper_audio/daw/session.py::ProjectSession.analyze` | consumes typed capture; attaches `TimeRef`, not bare floats |
| `src/reaper_audio/analysis/artifacts.py::register_existing_files` | becomes `register_asset()` with streaming sha256, bytes, mime, tombstone |
| `src/reaper_audio/analysis/bundle.py::analyze_bundle` | produces measurements and comparisons with asset/window/stage references |
| `src/reaper_audio/analysis/metrics.py::measure_audio` | stops overwriting stage; exposes `plr`, `psr`, band crest, clip runs |
| `src/reaper_audio/analysis/events.py::detect_events` | producer of `SignalEvent`, anchor `detector_frame` |
| `src/reaper_audio/analysis/transients.py::detect_transients` | producer of `SignalEvent`, anchor `detector_frame` |
| `src/reaper_audio/analysis/tempo.py::estimate_tempo_curve` | kept as compatibility wrapper; new pulse/level/phase functions |
| `src/reaper_audio/analysis/timeline_context.py::_bar_at` | fixed or replaced by `project_time_map`; must not guess denominator |
| `src/reaper_audio/analysis/edit_map.py::tempo_grid`, `_nearest_beat` | consumers of `RhythmMap`/`EditIntent`; no nearest-line decision |
| `src/reaper_audio/analysis/edit_map_report.py` | must stop dropping `numerator`/`denominator` |
| `src/reaper_audio/analysis/mix_evidence.py` | becomes evidence producer for `MixClaim`; thresholds become policy data |
| `src/reaper_audio/analysis/compare.py::reference_corridor` | gains stage/loudness/section/stereo guards and LOO |
| `src/reaper_audio/corpus/model.py` | distribution/percentile engine behind `ReferenceSet` |
| `src/reaper_audio/daw/cycle.py` | distance becomes guardrail, not acceptance objective |
| `src/reaper_audio/mcp_server.py` | 19 tools / 6 resource endpoints; adds context/view/verify; tool list generated from one source |
| `src/reaper_audio/reaper/bridge_client.py::capture_render` | passes tail/channels, returns settings and restore report |
| `scripts/reaper_bridge.lua::item_context` | adds `D_STARTOFFS`, source bounds, stretch-marker positions, loop/beat-attach/channel/group/lanes |
| `scripts/reaper_bridge.lua::validate_track_values` | accepts `solo`, `phase`, `automode`, `folder`, `nchan` |
| `scripts/reaper_bridge.lua::transaction_apply` | readback compared with `expected_after`; real notes for MIDI |
| `scripts/reaper_bridge.lua::project_capture_render` | tail flags, channel count, restore report |

### 2.3 Actual interface inventory (reconciled from code)

- `mcp_server.py` registers **19** tools (19 decorators; `tests/test_mcp_server.py:110` asserts `len(names) == 19`) and **6** resource endpoints: one static `reaper://capabilities/index` plus five templates (`capabilities/{domain}`, `api/{name}`, `projects/.../context/{change_count}`, `analyses/{id}/report`, `analyses/{id}/audio/{source_guid}`).
- `docs/ARCHITECTURE.md` still says 12 tools / 5 templates; `docs/MCP-README-LLM.md` says 19 / 5. Both are documentation drift, not behaviour. Source of truth after R3 is a generated `tool_surface.json` checked by a doc-test (C05-17).
- `api_catalog.json` has **732** documented functions at catalog v7.85; the live REAPER in validation was **7.84**. Every capability card therefore carries `min_version` and `available: true|false|unknown` from `APIExists`.
- R3-05's 14 tools / 10 resources is a **hypothesis for task evaluation**, not an accepted count. The current 19 must not be reduced before an eval shows no loss in task completion and tool-selection recall.

---

## 3. Canonical types and invariants

### 3.1 Identity

One namespace, one prefix registry, immutable IDs, tombstone on delete.

| Entity | ID | Basis |
|---|---|---|
| Project key | `project_id = reaper:<master_guid>:<path>` | current `ProjectRevision` / Lua `project_identity` |
| Project instance | `pri_<ulid>` | every open/reload; distinguishes same file, new session |
| Revision | `rev_<project_hash>_<epoch>_<counter>_<fp8>` | value, not an entity |
| Track/Bus/Master | `trk_<guid>`, `trk_master_<project_id>` | `GetTrackGUID`; bus is a track role, not a type |
| Item/Take | `itm_<guid>`, `tak_<guid>` | REAPER GUID |
| Source file | `src_<sha256:16>` | file content; path is an attribute |
| FX | `fx_<guid>` + `chain` | `TrackFX_GetFXGUID`; index is a hint |
| Send | `snd_<src>_<dest>_<ordinal>` | no GUID; destination GUID validates |
| Envelope | `env_<owner_kind>_<owner_guid>_<param>` | composite key |
| MIDI | `sco_<hash(take,ppq,pitch,channel,method)>` | symbolic project fact |
| Marker/Region | `mrk_<guid>`, `rgn_<guid>` | `EnumProjectMarkers3`; index fallback flagged |
| Time map | `tm_<project_hash>_<version>` | versioned map |
| Rhythm/correspondence | `rmp_<hash>`, `cor_<hash>` | natural key + hypothesis |
| Section/Riff/Phrase/Gesture | `sec_`, `rif_`, `phr_`, `peg_` | hypotheses; `ges_` is deprecated and never emitted |
| Occurrence | `occ_<hash(instance,item,take,loop_iteration)>` | one file can sound twice |
| Signal/performance event | `evt_`, `prf_`, `peg_`, `mea_` | deterministic hash |
| Asset/Capture | `ast_sha256_<hex>`, `cap_<ulid>` | bytes / one capture call |
| Listener result | `obs_<ulid>` | one model call |
| Annotation | `ann_<ulid>` | human label/rating |
| Claim/Hypothesis/Intent | `clm_`, `hyp_`, `int_` | authored |
| Plan/Operation/Change/Verification | `pln_`, `op_`, `chg_`, `ver_` | authored |

Rule: derived records use deterministic hashes from natural key + `method.version` + `revision.fingerprint`; authored/session records use ULID. Re-analysis of the same revision with the same parameters must reproduce the same `evt_`/`prf_`/`occ_` IDs.

### 3.2 Revision

`RevisionRef {project_id, instance_id, epoch, counter, fingerprint, fingerprint_scope}`.

Two predicates, never one:

- `same_identity`: same open project instance (or a deliberate reload mapping);
- `same_content`: same sound-affecting content.

`counter` is diagnostic only. `dep_set` drives scoped invalidation; global `stale_analysis` is forbidden. Fingerprint covers tracks, items, takes, FX, routing, envelopes and tempo; it excludes transport, selection and render settings. The canonical digest is produced by Lua `project.digest` as canonical records; sha256 is computed in Python. A coarse fallback (proposal: above 20 000 entities) degrades to track-level and must be reported, not silently accepted.

### 3.3 Time

`TimeRef {basis, value, unit, uncertainty_ms, map_id, mapping_status}`; `anchor` lives on `Interval{start, end, anchor}`.

Bases: `sample_index`, `file_s`, `source_s`, `take_s`, `project_s`, `musical_qn`, `bar_beat`, `performed_beat`, `capture_s`.

Invariants:

1. A value without `basis` is rejected at the boundary.
2. `map_id` is mandatory for `source_s`, `take_s`, `musical_qn`, `bar_beat`; `performed_beat` requires `hypothesis_id`.
3. Composition only when `to_basis`/`from_basis` match; uncertainty is RSS of inputs, never below `max_residual_ms` of the map.
4. `mapping_status ∈ {verified, approx, unverified, unsupported}`. `approx` allows view/diagnosis, forbids cut and MIDI write. `unverified` returns `null` with a reason, not a guess.
5. `sample_index = floor(t·sr)` for starts, `ceil` for ends.
6. Denominator and marker `beat` are mandatory for `bar_beat`; missing → `null`.
7. No code derives `performed_beat` from `musical_qn` or vice versa. The only bridge is `GridRelation`.

`ProjectTimeMap` (`analysis/project_time_map.py`) is the notational clock: integral of tempo ramps, denominator, beat, measure, `bpm_basis`, `linear_model`. The project grid is a prior for user intent, not evidence of performance.

### 3.4 Signal stages and capture

Stages (8): `source_file`, `take_sum_pre_fx`, `track_stem_post_fx`, `bus_stem_post_fx`, `send_return_post_fx`, `track_via_master_post_fx`, `master_mix_post_fx`, `reference_file`.

Three orthogonal fields:

- `stage` — signal point;
- `capture_method` — `native_render | audio_accessor | source_read | file_import`;
- `source_kind` — `di | mic | amp_print | line | midi_render | unknown`.

`take_sum_pre_fx` is not DI and not a stem. `separated_estimate` is a derived asset (with separator name/version), never a project stage. In vers1 guitars are `amp_print`; pitch/note claims on them stay human-gated.

`CaptureRequest` must carry project revision, object refs (GUID, index hint only), stages, method, window, tail, channels, sample rate, isolation, lease TTL, artifact role, dry-run, reason. Capture is a **side-effecting transaction**: it changes selection/settings/dirty and must restore and read back. It is not "read-only".

`AudioAsset` carries `asset_id`, `capture_id`, stage, channel view, window (project + file + optional source), sample rate, channels, frames, duration, bytes, sha256, uri, lease, status, `source_kind`, `derivation`.

`DeliveryEvidence` is mandatory on every audio response: `not_requested | uri_only | forwarded_unverified | forwarded_verified | unsupported | failed`. The edge `heard` exists only for `forwarded_verified`.

### 3.5 Knowledge, claims and uncertainty

Three status enums, not one:

- knowledge: `measured | derived | model_observation | hypothesis | confirmed | not_requested | unsupported | failed | insufficient_evidence | uncalibrated`;
- event: `candidate | transcribed | reviewed | rejected`;
- claim: `asserted | contested | confirmed | refuted | unverified | retracted`;
- verification: `planned | applied | verified | failed | inconclusive | rolled_back`.

Evidence grades (consistent across R3-04/R3-05): `A_measured_dsp`, `B_listener_verified`, `C_listener_unverified`, `D_derived`, `E_annotation`, `F_unsupported`.

Causality: `caused_by` is forbidden as fact. A causal statement is `IssueHypothesis`/`Hypothesis` with `mechanism`, `predictions`, `supports`, `against`, and at least one `discriminating_test` or an explicit `not_testable`.

### 3.6 Operation and verification

Three capability classes with separate promises:

- **read capability** — completeness of declared fields at a revision; reads do not change selection/dirty; audio reads only under a capture lease;
- **typed operation** — changes exactly the declared fields, with `preconditions`, `expected_after[]`, comparator, tolerance, `SideEffectFootprint`, undo/restore class and `readback_fields`;
- **generic API/Action** — full documented access, but no automatic safety claim; a capability card states effects and risk; `ok` is not verification.

Rule: a class cannot be silently upgraded. If a task needs a function outside the typed set, the caller gets `generic_required` with a reason, not a hidden generic substitute. A generic call cannot be called verified because it returned `ok`.

Reversibility classes R0–R5:

- R0 read-only;
- R1 value overwrite with known previous value;
- R2 reversible transaction with undo/snapshot and verified restore;
- R3 requires checkpoint/save-copy;
- R4 destructive render or media write without source mapping;
- R5 irreversible.

Capture-render is R2 only when settings/selection/dirty were restored and `RENDER_ADDTOPROJ=0`. A destructive render without preserved source mapping is R4/R5, not "a render".

Operation lifecycle: `prepare → inspect diff → consent/risk → apply → readback → capture/analyze → confirm/refute → commit/checkpoint or rollback`.

---

## 4. Producer / consumer contracts

| Producer | Contract | Consumers |
|---|---|---|
| R3-01 capture | `CaptureRequest → Capture → AudioAsset[]` with revision, stage, method, source_kind, channel view, window, hash, lease, delivery status | R3-02 events, R3-04 mix, R3-05 ledger/context, R3-06 post-edit capture |
| R3-02 events | `evt_` signal events, `occ_` occurrences, `prf_` performance events, `peg_` gestures, `sco_` MIDI facts; status, uncertainty, eligibility | R3-03 pulse/structure, R3-04 performance evidence, R3-05 graph/index, R3-06 cut/MIDI gates |
| R3-03 time | `TimeMap`, `RhythmMap`, `GridRelation`, `Section`/`RiffInstance`, `EditIntent`, `Correspondence`; alternatives and protected sets | R3-02 musical projection, R3-04 section matching, R3-05 time edges, R3-06 edit targets |
| R3-04 mix | `MixScope`, `MixObservation`, `MixClaim`, `IssueHypothesis`, `PostChangeCheck`, `ActionCandidate`; ≥2 alternatives for inferred claims | R3-05 claim ledger, R3-06 action/verification |
| R3-05 context | IDs, `RevisionRef`, `TimeRef`, `dep_set`, ledger/store, manifest/views, tool surface | all domains; R3-06 plans/verifications |
| R3-06 operations | `OperationPlan`, `Readback`, `Verification`, `SideEffectFootprint`, `UndoRecord`, `SaveCheckpoint`; state delta | R3-05 lineage/invalidation; R3-01 re-capture; all domain refreshes |

Independence rules:

- R3-01 can proceed without events or mix; it must not depend on R3-02.
- R3-02 needs capture stage/time but not mix.
- R3-03 needs events for attack evidence; it can start with project time map and synthetic fixtures.
- R3-04 needs capture stage and sections; CPU DSP work can start before listener/V100.
- R3-05 is integration: it must not invent analyzers; it owns types and context assembly.
- R3-06 must not invent musical semantics; it executes intents and verifies them.

---

## 5. Staged build order

### P0 — Vocabulary and contracts (no REAPER, no audio)

**Prerequisites:** none.
**Scope:** `timebase.py`, `context/schema.py`, `context/ids.py`, `errors.py`, `mix/models.py` type skeletons, `analysis/event_model.py`.
**Artifacts:** frozen dataclasses, JSON round-trip, `schema_version=1`, ID registry, error registry.
**Exit gates:** schema round-trip; no duplicate `Stage`/`TimeRef`; unknown ID prefix rejected; a value without `basis` rejected; `mypy`/lint clean as configured in `pyproject.toml`.
**Rollback:** pure additive modules; deleting them restores the tree.
**Blockers:** none.

### P1 — Capture truth (code now, live proof later)

**Prerequisites:** P0.
**Scope:** `daw/session.py::capture_audio`, `analysis/artifacts.py`, `bridge_client.py::capture_render`, Lua `project_capture_render`, `track_context`/`validate_track_values`, MCP `_render_isolated_track`.
**Artifacts:** typed `Capture`, asset hash/bytes, tail/channels/settings/restore report, solo warning, folder-aware isolation or explicit refusal, manifest-owned temp files.
**Exit gates:** G1 structural; G2 transaction; stage enum; old `test_daw_session` still passes; hash same/mutated; cleanup only owned.
**Blockers:** N1–N5 (accessor semantics, multichannel, `&128`, tail, PDC). Code can land behind `unverified`; acceptance waits.

### P2 — Project state read model and revision

**Prerequisites:** P0; can run parallel with P1.
**Scope:** Lua `item_context` fields (`D_STARTOFFS`, playrate, source frames/SR/channels, `B_LOOPSRC`, stretch-marker positions, `C_BEATATTACHMODE`, `I_CHANMODE`, `I_GROUPID`, lanes), `ProjectRevision` v2, `project.digest`, `daw/revision.py`, `reaper/handles.py`, address resolvers.
**Artifacts:** complete read model for items/takes/routing/automation/FX; `same_identity`/`same_content`; `ambiguous_target` with candidates.
**Exit gates:** G0/G2/G3 read-only; old dicts parse; two reads without changes yield identical fingerprint; fader/item/tempo/envelope change it; selection/transport do not.
**Blockers:** exact Lua semantics for group/lane/stretch; live `APIExists`.

### P3 — Ledger and context assembly

**Prerequisites:** P0, P2.
**Scope:** `context/ledger.py`, `store.py`, `deps.py`, `claims.py`, `anchors.py`, `manifest.py`, `views.py`, `assembler.py`, `audio_handoff.py`.
**Artifacts:** append-only JSONL, SQLite projections, scoped invalidation, manifest ≤1500 tokens on the 33-track fixture (hard ceiling 4k from R2), 10 views with cursor and omission ledger.
**Exit gates:** rebuild store from ledger equals store; dangling edges 0; no duplicate/missing rows across 10 slices; manifest ordering stable; `heard` impossible without delivery proof.
**Blockers:** none for types/assembly; audio handoff needs N6.

### P4 — Events and musical time

**Prerequisites:** P1 (stage/time), P2 (occurrence/offset), P3 (IDs/ledger).
**Scope:** `event_model.py`, `event_build.py`, `event_policy.py`, `event_index.py`, `project_time_map.py`, `rhythm_map.py`, `structure.py`, `edit_intent.py`, `score_time.py`.
**Artifacts:** signal/performance events with deterministic IDs; occurrence grouping; pulse/beat/meter/bar hypotheses with alternatives; riff/section/repetition; `EditIntent` and monotonic correspondence.
**Exit gates:** repeat analysis → same IDs; two items of one file → two occurrences; 3/4 vs 6/8 → `preferred=null` when accents are weak; through-composed → `no_repetition`; intent without correspondence rejected; protected region blocks.
**Blockers:** U1–U6 (tempo `bpm_basis`, ramp model, marker beat semantics, stretch positions, attack calibration, MIDI loop PPQ); pitch/note claims on vers1 guitars blocked by missing DI.

### P5 — Mix claims and references

**Prerequisites:** P1 stage/socket, P3 ledger, P4 sections.
**Scope:** `mix/models.py`, `scope.py`, `constructs.py`, `references.py`, `claims.py`, `severity.py`, `causes.py`, `verify.py`, `actions.py`; DSP additions `reverb.py`, `masking.py`; `compare.py` guards.
**Artifacts:** C1–C19 construct catalogue; `MixClaim` with mandatory scope and ≥2 alternatives; reference registry with comparability guards; post-change check.
**Exit gates:** claim without scope rejected; stage/loudness/section mismatch → `NotComparable`; no `db/6` severity; distance improvement without target-construct shift → `inconclusive`.
**Blockers:** U1/U3/U4/U5/U6/U11 (send-return availability, EDT/reverb accuracy, separation for SMR, corpus/licences, JND/agreement, band grid, Russian band count).

### P6 — Typed operations and verification

**Prerequisites:** P2 read model, P3 revision/ledger; musical ops need P4.
**Scope:** `daw/verify.py`, `revision.py`, Lua `transaction_apply`, `item_context` write fields, `item.edge/slip/fade`, clean `item.split`, `track.set`, FX, send, MIDI, automation, tempo, markers, save/checkpoint.
**Artifacts:** capability cards, `OperationPlan` with `expected_after`, readback comparators, footprint, undo/restore, save collision handling.
**Exit gates:** G2/G3/G6; readback equals expected within tolerance; preview does not change; undo restores; stale refuses; replay idempotent where declared; `recFX=false`; `fx.get_chunk` fixed or removed.
**Blockers:** live API semantics for edge/fade on stretch/loop/comp, group sync, save/DIRTY, undo across reorder.

### P7 — MCP surface, vertical slice, E2E

**Prerequisites:** P0–P6.
**Scope:** `mcp_server.py` context/view/verify tools, resources, generated `tool_surface.json`, doc-test, four capability scenarios.
**Artifacts:** 19 current tools preserved or a task-eval-backed replacement; manifest + views; `reaper_context`, `reaper_view`, `reaper_verify`; error registry; LLM guide generated from schema.
**Exit gates:** G0–G10; four scenarios; negative cases; no doc/code count drift.
**Blockers:** N6 audio delivery, N8 Flamingo, human acceptance.

---

## 6. Test design and quantitative gates

### 6.1 Test levels

1. **Unit/property** — pure types, time conversion, IDs, claim invariants, policy resolution.
2. **Golden/contract** — serialized fixtures, schema round-trip, manifest/views determinism.
3. **Offline DSP known-answer** — synthetic tones, impulses, controlled degradations, null tests.
4. **Bridge contract** — Lua ↔ Python message shapes, error codes, revision and readback.
5. **Live REAPER on an isolated copy** — capture, edit, undo, save, crash/restart.
6. **Audio/listener** — delivery canary, calibrated listener axes.
7. **Human** — blind ratings, JND, inter-rater agreement, owner acceptance.

No test is run in R3. All thresholds below are proposals until the owner accepts them.

### 6.2 Gates

| Gate | Check | Quantitative exit | False-pass risk |
|---|---|---|---|
| G0 identity/install | health, version, `APIExists`, project/revision binding | 100% of declared fields; version in manifest | project switched silently |
| G1 structural | capture adds no tracks/items/files; settings restored | 0 new objects; RPP hash unchanged; restore report complete | restore reported but not read back |
| G2 transaction | preview no-op; apply only plan; undo restores; stale/replay | +1 undo entry; stale → refusal; replay same result | undo label exists but state differs |
| G3 geometry | positions/lengths/offset/fades | ±1 sample; fade ±1 ms; offset ±1 sample × rate | readback echoes request |
| G4 acoustic null | capture vs native render | dry deterministic chain ≤ −120 dBFS; full mix at measured session floor | comparing different windows/pairs |
| G5 musical | event table: each target once, order, no gap/re-attack | residual ≤ owner threshold (proposal 5 ms start); 0 duplicates; 0 gaps; 0 lost attacks | grid proximity called success |
| G6 MIDI/FX/automation | note count/pitch/PPQ/velocity; FX in main chain; PDC; automated fader | exact notes; `TrackFX_GetCount +1`; `GetRecCount` unchanged; PDC reported | request echo instead of actual |
| G7 delivery | LUFS/TP/format/dither/stems | LUFS ±0.2 LU; TP ≤ ceiling; SR/channels correct | loudness measured on wrong stage |
| G8 recovery | crash mid-operation, bridge restart, save collision | orig hash unchanged; `.RPP-bak` opens; undo requires re-read | recovery tested on synthetic only |
| G9 no side effects | footprint: selection, dirty, mute/solo, undo entries, files | 0 unplanned; all in footprint | hidden autosave |
| G10 context sufficiency | blind task eval, ablation | task success and grounding improve vs flat/DSP-only; no increase in critical omissions | judge leakage |

### 6.3 Context sufficiency experiment

Arms: A0 goal only; A1 flat summary; A2 DSP-only; A3 audio/listener-only; A4 project-only; A5 linked full context; A6 manifest + views; A7 ablation without alternatives/causal edges. 42 tasks, 7 classes, 3 experts, blind, Latin-square order, 20% repeats. Metrics: expert score, task pass, grounding rate, entity/time exactness, critical omission rate, tool selection, tokens/calls, hallucinated facts. Report per-arm medians with CIs, not a single composite.

---

## 7. Reversibility, versioning, lifecycle

- **Schema:** `schema_version` on every record; additive changes only within a major version; migrations tested on golden fixtures. `RevisionRef` v2 keeps old `ProjectRevision` dicts parseable through an adapter.
- **IDs:** immutable; deletion is a tombstone; no reuse.
- **Audio:** content-addressed `ast_sha256_`; lease/TTL; `available | released | expired | missing`; tombstone after release; only `song.orig.rpp` and `song.final.rpp` are retained long-term; temp captures are manifest-owned and cleaned on success and failure.
- **Save:** collision-safe; never silent overwrite; checkpoint hash + DIRTY + mtime; `.RPP-bak` policy explicit.
- **Reversibility:** R0–R5 per operation; a destructive render without source mapping is not reversible.
- **Dependency:** `dep_set` invalidation; no global stale; fingerprint is the fast path, not the only path.

---

## 8. Empirical blockers

| ID | Unknown | Blocks | Can code proceed? |
|---|---|---|---|
| N1 | Does `GetAudioAccessorSamples` include fader/pan/mute/solo/receives; is `D_VOL` applied twice? | exact `take_sum_pre_fx` semantics, gain provenance | yes, behind `unverified`; timing-only |
| N2 | Does `RENDER_SETTINGS &4` override `RENDER_CHANNELS`? | multichannel guarantee | types yes, acceptance no |
| N3 | Does `&128` produce track-via-master? | `track_via_master_post_fx` stage | stage defined, status unverified |
| N4 | Is `RENDER_TAILFLAG` compatible with `RENDER_BOUNDSFLAG`? | tail default | code yes, gate no |
| N5 | Actual PDC/latency per plugin chain | sample-accurate claim | adapter yes, tolerance no |
| N6 | Does the MCP host deliver `AudioContent`/resource to the model? | `forwarded_verified`, acoustic acceptance | handoff code yes, `heard` no |
| N7 | Can autosave fire during modified render settings? | strong restore guarantee | restore code yes, guarantee no |
| N8 | Does Music Flamingo GGUF Q5_1 run on V100 16 GB? | Flamingo inclusion | adapter yes, model no |
| U1 | Real `bpm` semantics for /8 and `TimeMap_GetDividedBpmAtTime` | final `bpm_basis`/meter contract | types yes, map acceptance no |
| U2 | Linear ramp model: BPM-linear or time-per-beat linear | exact QN integral | `status=approx` until tested |
| U3 | Marker `measure`/`beat` semantics when `beat != 1` | bar/beat for mid-bar markers | abstain path yes |
| U4 | Stretch-marker positions in saved RPP | verified source mapping | reader yes, verified no |
| U5 | Attack-start calibration vs detector frame | ±10 ms cut tolerance | policy yes, cut no |
| U6 | `MIDI_GetProjTimeFromPPQPos` on looped items | MIDI iteration/readback | reader yes, write no |
| M04 | Send/return availability, EDT/reverb accuracy, separation for SMR, corpus/licences, JND/agreement, band grid | C1–C3/C12, severity calibration, reference set | types/CPU DSP yes, acceptance no |

Also blocked: human ratings and owner thresholds (residual, LUFS/TP, ORIG, movable attacks), live REAPER version differences (7.84 vs catalog 7.85), group/lane sync semantics, save/DIRTY semantics, undo across reorder.

---

## 9. Smallest end-to-end vertical slice

One section of one project, no genre workflow:

1. **Bind** — open project, obtain `project_id`, `pri_`, `RevisionRef`, `APIExists` map.
2. **Capture** — 8 s at 40–48 s: master (native render) + one track stem + optional pre-FX sum; hash, SR, channels, window, tail, restore report, delivery status.
3. **Measure** — LUFS/true peak/crest/bands/correlation for each asset; `TimeRef` for the window.
4. **Describe** — manifest of sources/stages/coverage/missing; one `MixObservation` for the chosen construct (e.g. presence).
5. **Claim** — one `MixClaim` with scope, stage, section, evidence, ≥2 alternatives and a discriminating test.
6. **Act** — one typed operation (e.g. `track.set` gain −1 dB or `item.fade` 10 ms) with `expected_after`, preview, consent if required, apply.
7. **Read back** — geometry/parameter match within tolerance; footprint.
8. **Re-capture** — the same stage/channel/window/loudness; measure the target construct.
9. **Verify** — `verification=verified|failed|inconclusive`; if failed, rollback; if inconclusive, say why.
10. **Journal** — claim → evidence → asset → revision → operation → verification, with `dep_set` invalidation.

Minimal pass: G0–G3 plus G5 subset; no new project objects or files; audio delivery status honest; the claim's target construct actually moved; rollback restores the original state. This slice proves the loop, not the product.

---

## 10. What can be implemented first

**Can start now, independently of live REAPER/V100/human data:**

- P0: `timebase.py`, `context/schema.py`, `context/ids.py`, error registry, event/mix type skeletons.
- P2 read-only parts: `ProjectRevision` v2 adapter, digest client, address resolvers, read-model field contracts.
- P3 ledger/store/manifest/views with synthetic fixtures.
- P4 CPU-DSP parts: event normalization, occurrence builder, time map, pulse/meter hypotheses, structure — on synthetic and existing captured fixtures.
- P5 CPU-DSP parts: construct catalogue, metric promotion, claim builder, reference registry, post-change check.
- P6 contract parts: `expected_after` schema, verifier, capability cards — with fake bridge.
- P7 manifest/view/tool-surface generation and doc-test.

**Cannot start acceptance before measurements:**

- any sample-accurate or `verified` claim about accessor, multichannel, `&128`, tail, PDC, group/lane, save/DIRTY, FX move/undo;
- `forwarded_verified` and any "the model heard it" claim;
- Music Flamingo inclusion;
- pitch/note claims on vers1 guitars (no DI);
- calibrated mix severity, JND, reference distribution;
- any musical acceptance based only on synthetic CPU tests.

**Safe coding increments:**

1. Contracts + IDs + timebase (one PR, additive, no behaviour change).
2. Capture adapter + asset hash + manifest (behind existing API, old tests green).
3. Revision v2 + read model fields (additive, old dicts parse).
4. Ledger + store + manifest/views (synthetic fixtures).
5. Event model/build/policy (synthetic + historical fixtures).
6. Time map + rhythm + structure (synthetic; project map on read-only snapshot).
7. Mix constructs + claims + references (CPU DSP).
8. Typed operations + verifier (fake bridge, then isolated live copy).
9. MCP surface + vertical slice.
10. E2E and human evaluation.

Each increment must leave the tree building and the existing 179-test suite green (the number is from `docs/VALIDATION.md`; re-check the actual run later).

---

## 11. Explicit non-claims

R3 does not prove that the model hears audio, does not validate a listener model, does not establish metal-specific note transcription, does not calibrate mix severity, and does not prove that REAPER operations preserve musical feel. Those are empirical questions with named tests and owners. The blueprint is code-ready only in the sense that interfaces, invariants, error semantics, owners, call sites, test oracles and rollback are specified; the live gates remain open.
