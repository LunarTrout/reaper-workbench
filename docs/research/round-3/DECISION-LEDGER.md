# REAPER Workbench — decision ledger (R3)

**Status:** design record. No code, tests, REAPER project or media changed.
**Baseline:** `main`, HEAD `86b14bb5136c9a0b00be4d6948986511be2d9674`; shared dirty tree preserved.
**Sources:** R2 finals in `work/research/round-2/reports/`, R3 finals in `work/research/round-3/reports/`, current code/tests, `docs/research/round-3/00-IMPLEMENTATION-READINESS-CONTRACT.md`.

Classes:

- **accepted** — agreed architecture invariant; may still need an owner signature, but all six reports converge and the code supports the direction.
- **proposed** — concrete schema/name/threshold/sequence offered for implementation; not a fact and not yet approved.
- **owner decision** — cannot be settled by research; the owner must choose.
- **empirically blocked** — needs a live REAPER run, V100 inference, audio delivery, or human ratings before the claim can be verified.

---

## 1. Accepted architecture decisions

| ID | Decision | Evidence | Owner | Next action |
|---|---|---|---|---|
| D01 | Context is layered: project state, capture, measurement, listener observation, hypothesis, goal, plan, change, verification are different record kinds, not fields of one JSON. | R3-05 §3.1; R2-05 §3.1; `context/schema.py` proposal | R3-05 | freeze `record_kind` registry in P0 |
| D02 | Eight canonical signal stages: `source_file`, `take_sum_pre_fx`, `track_stem_post_fx`, `bus_stem_post_fx`, `send_return_post_fx`, `track_via_master_post_fx`, `master_mix_post_fx`, `reference_file`. | R3-01 §3.2; R2-01 §3.4 | R3-01 | implement enum in `context/audio.py` |
| D03 | `stage`, `capture_method`, `source_kind` are orthogonal. None is inferred from another or from a track name. | R3-01 §3.2; R3-02 §3.1; R2-01/R2-02 C2 | R3-01 | schema test rejects missing `source_kind` |
| D04 | `take_sum_pre_fx` is neither DI nor a stem. DI is an annotation (`source_kind=di`), and vers1 guitars are `amp_print`. | R3-01 §3.2; R3-02 §2.2; R2-02 §2.3 | R3-01/R3-02 | note claims on vers1 guitars stay human-gated |
| D05 | Capture is a side-effecting transaction, not read-only: it changes selection/settings/dirty and must restore and read back. | R3-01 §3.5; R3-06 §3.4; R2-01 I1–I12 | R3-01/R3-06 | implement restore report and G1 |
| D06 | Audio assets are content-addressed (`sha256`), with bytes, SR, channels, frames, duration, lease, TTL and tombstone. | R3-01 §3.3; R3-05 §3.1; R2-05 §6.7 | R3-01/R3-05 | `artifacts.register_asset` in P1 |
| D07 | `AudioAsset.delivery_status` is mandatory; the edge `heard` exists only for `forwarded_verified`. `audio_uri` alone is `uri_only`. | R3-01 §3.2; R2-01 §3.6; R2-05 §6.7 | R3-01/R3-05 | canary before any "model heard" claim |
| D08 | Derived records use deterministic hash IDs; authored/session records use ULID. Re-analysis of the same revision and parameters reproduces IDs. | R3-02 EV-02; R3-05 §3.1 | R3-05/R3-02 | `context/ids.py` + property test |
| D09 | Bus/folder/master are track roles, not separate entity types; one object has one ID. | R3-05 §3.1; R3-04 §3.1 | R3-05 | `Track.track_role` enum |
| D10 | Revision uses `RevisionRef {project_id, instance_id, epoch, counter, fingerprint, fingerprint_scope}` with separate `same_identity` and `same_content` predicates; `counter` is diagnostic. | R3-05 §3.2; R3-06 §3.2 | R3-05/R3-06 | adapter over `ProjectRevision` |
| D11 | Time values carry `basis`; `mapping_status ∈ {verified, approx, unverified, unsupported}`; `anchor` lives on `Interval`, not `TimeRef`. | R3-03 §3.1; R3-02 §3.2 | R3-03/R3-05 | `timebase.py` in P0 |
| D12 | The project tempo map is a prior for intent and bar numbering, not evidence of performed timing. `performed_beat` exists only on a hypothesis. | R3-03 §3.1; R2-03 §6 | R3-03 | `GridRelation` stores both sides |
| D13 | Statuses are separate per layer: knowledge, event, claim, verification. One universal `status` is forbidden. | R3-05 §3.1; R3-02 §3.3; R3-04 §3.1 | R3-05 | four enums in schema |
| D14 | Causal statements are hypotheses with `mechanism`, `predictions`, `supports`, `against` and a discriminating test or explicit `not_testable`; `caused_by` as fact is forbidden. | R3-04 §3.1; R3-05 §3.1 | R3-04/R3-05 | claim validator |
| D15 | DSP measurements are the authority; listener text is `model_observation`; corpus distance is a guardrail, never overall quality. | R3-01 §3.4; R3-04 §3.2; R2-04 §5.5 | R3-04 | remove distance from `cycle.py` acceptance |
| D16 | The full analysis is a navigation contract, not an inline blob: manifest plus addressable views, raw curves on demand. | R3-05 §3.5; R2-05 §6.2 | R3-05 | manifest/views in P3 |
| D17 | Graph semantics on a relational core plus an append-only ledger; a property-graph database is an export option, not the foundation. | R3-05 §3.1/§3.10; R2-05 §7.1 | R3-05 | SQLite + JSONL in P3 |
| D18 | Invalidation is scoped by `dep_set`; global `stale_analysis` is replaced. Fingerprint is a fast path, not the only path. | R3-05 §3.2; R3-03 §9; R3-06 §3.2 | R3-05 | dependency matrix tests |
| D19 | Typed operations carry `expected_after`, tolerances, readback and undo; generic ReaScript/Actions remain fully discoverable but are not automatically safe or verified. | R3-06 §3.1; R2-06 §7 | R3-06 | capability cards + verifier |
| D20 | Reversibility is classified R0–R5; a destructive render without source mapping is R4/R5, not "a render". | R3-06 §3.4; R2-06 §7 | R3-06 | class per operation card |
| D21 | Only origin and final project copies are retained; temp captures are manifest-owned; no wildcard deletion; no silent overwrite. | R3-06 §3.10; R3-01 §3.5; contract §17 | R3-06 | lifecycle tests |
| D22 | The live server has 19 tools and 6 resource endpoints (1 static + 5 templates); `tests/test_mcp_server.py:110` asserts 19. | R3-05 §2.1; code: 19 `@server.tool`, 6 `@server.resource`; test line 110 | R3-05 | generate `tool_surface.json`; doc-test |
| D23 | The API catalog is v7.85 with 732 functions; the live REAPER in validation was 7.84. Every capability card carries `min_version` and `APIExists` status. | R3-06 §3.2; `docs/VALIDATION.md:10`; `api_catalog.json` | R3-06 | `available: true/false/unknown` |
| D24 | No CPU inference or offload; the only local GPU is the V100 16 GB; no parallel GPU jobs. | readiness contract §5; R2 protocol | owner | keep GPU tests sequential |
| D25 | MIDI in the project is a `ProjectFact` (`sco_`), not audio evidence; it proves project state, not the heard note. | R3-02 §3.4; R2-02 §2.2 | R3-02 | `origin=project_midi` separate from `prf_` |

---

## 2. Proposed decisions (not yet approved)

| ID | Proposal | Evidence | Why provisional | Next action |
|---|---|---|---|---|
| P01 | New module paths: `timebase.py`, `context/schema.py`, `context/audio.py`, `analysis/event_model.py`, `mix/models.py`, `daw/verify.py`. | R3-01 §3.1; R3-02 §3.1; R3-03 §4; R3-04 §3.1; R3-05 §4 | names are design choices, not facts | owner/architect freeze in P0 |
| P02 | 14 tools / 10 resources instead of 19 / 6. | R3-05 §4 C05-16; R2-05 §6 | must be settled by task evaluation, not taste | run task-eval before changing surface |
| P03 | Manifest budget ≤1500 tokens on the 33-track fixture; hard ceiling 4k. | R3-05 C05-12; R2-05 §6.2 | measured only on paper | measure on a real snapshot |
| P04 | Coarse fingerprint fallback above 20 000 entities. | R3-06 §3.2 | threshold not measured | instrument digest on vers1 |
| P05 | `false_merge = 0` as a gate, with an upper confidence bound of 3/N (0/60 → 4.9%). | R3-02 §4 | statistically motivated, not measured on metal | annotate double-track fixtures |
| P06 | Tolerance policy: view ±50 ms; diagnosis ±10–20 ms; cut ±10 ms at precision ≥0.95; MIDI onset ±25 ms, duration ±20%. | R3-02 §3.6; R2-02 §7.2 | values are proposals; some need attack calibration | owner approves after T0/U5 |
| P07 | Severity is `provisional` until human ratings; no `db/6` heuristic. | R3-04 §3.3; `mix_evidence.py` current severity | calibration absent | build ratings protocol first |
| P08 | Evidence grades A–F (`A_measured_dsp`, `B_listener_verified`, `C_listener_unverified`, `D_derived`, `E_annotation`, `F_unsupported`). | R3-04 §3.1; R3-05 §3.1 | naming/ordering is a schema choice | freeze in `context/schema.py` |
| P09 | `schema_version=1`, additive migrations only within a major version. | R3-05 §4 C05-01 | no migration exists yet | golden fixtures |
| P10 | SQLite projections + append-only JSONL ledger; rebuild from ledger must equal store. | R3-05 §3.10 | storage choice could change | P3 test |
| P11 | Reference set of 20–30 homogeneous metal tracks; HiMMP gives 9 mixes of one track, not a distribution. | R3-04 §5.5; R2-04 §8.1 | corpus not assembled | owner decision O03 |
| P12 | Vertical slice: 8 s at 40–48 s, one track + master, one claim, one operation, re-capture. | this blueprint §9 | slice size is a planning choice | owner approves scope |
| P13 | One band grid for cross-module comparison (28 vs 29 bands currently unresolved). | R3-04 U11 | code has both | owner/architect picks one |
| P14 | Context-sufficiency experiment: A0–A7 arms, 42 tasks, 3 experts, blind, Latin square. | R3-05 §5; R2-05 §8.1 | protocol proposal | recruit experts, freeze rubric |

---

## 3. Owner decisions

| ID | Question | Why it cannot be researched | Options | Default if silent |
|---|---|---|---|---|
| O01 | Which host/model controls the library: text-only (current DeepSeek) or audio-capable (Gemini/GPT-class)? | determines whether direct hearing exists or hearing is delegated | text-only two-link; audio-capable with canary | keep two-link; treat listener text as observation |
| O02 | Freeze the canonical vocabulary and module names (P01, D02, D08, D10, D11). | names are architecture, not evidence | accept R3-05 canon; rename | R3-05 canon stands as proposal |
| O03 | Corpus: 20–30 metal references, or HiMMP + 10 commercial, or own stems only? | licences and access are owner-specific | HiMMP only; mixed; own only | HiMMP + own, no commercial |
| O04 | Thresholds: residual for "aligned" (proposal 5 ms), LUFS/TP targets, beat/cut tolerances. | musical acceptance is a policy | strict/loose profiles | provisional values; gates fail closed |
| O05 | Which take is ORIG, and which attacks may move? | musical intention is human | owner-marked ORIG; protected list | no automatic move without ORIG |
| O06 | May Music Flamingo (NVIDIA OneWay noncommercial) be used in the product? | licence choice | include; replace with Apache model; drop | drop from default path |
| O07 | Permission for live tests A1–A8 on a copy, including a destructive crash drill. | risk to real project | copy-only; full; none | copy-only, no crash drill |
| O08 | Retention: where `final` lives, how many backups, TTL for temp assets. | user data policy | orig+final only; +N backups | orig+final only, TTL 1 h |
| O09 | Is tablature/fingering a product feature? | scope choice | yes; later; no | defer |
| O10 | Permission to edit a copy of vers1 for acceptance. | owner's material | copy; read-only; no | read-only |
| O11 | Who annotates 2×30 s of guitar techniques, and which experts rate mixes? | requires people | owner; hired experts; none | owner only, no calibration |
| O12 | Final tool count after task evaluation (19 vs 14). | empirical | keep 19; reduce; hybrid | keep 19 until eval |
| O13 | One band grid (28 vs 29 bands). | implementation consistency | 28; 29; resample | pick one before P5 |

---

## 4. Empirically blocked

| ID | Unknown | Blocks | Evidence | Next resolving action |
|---|---|---|---|---|
| E01 | Does `GetAudioAccessorSamples` include fader/pan/mute/solo/receives; is `D_VOL` double-applied? | exact `take_sum_pre_fx`, gain provenance | R3-01 N1; R3-02 C3 | live A1: fader −6 dB, compare accessor vs render |
| E02 | Does `RENDER_SETTINGS &4` override `RENDER_CHANNELS`? | multichannel guarantee | R3-01 N2 | live A3 on a 4-channel track |
| E03 | Does `&128` give selected-track-via-master? | `track_via_master_post_fx` | R3-01 N3; R3-06 | live A4 with bus/master FX |
| E04 | Is `RENDER_TAILFLAG` compatible with `RENDER_BOUNDSFLAG`? | tail default | R3-01 N4 | live A5: section + 200 ms tail |
| E05 | Actual PDC/latency per chain | sample-accurate claim | R3-01 N5; R3-06 | live A8: round-trip impulse |
| E06 | Does the MCP host deliver `AudioContent`/resource to the model? | `forwarded_verified`, "model heard" | R3-01 N6; R2-01 A7 | canary: 1 s L/R tone + phrase, raw JSON-RPC |
| E07 | Can autosave fire during modified render settings? | strong restore guarantee | R3-01 N7 | live A6: capture with autosave on a copy |
| E08 | Does Music Flamingo GGUF Q5_1 run on V100 16 GB? | Flamingo inclusion | R3-01 N8; R2-01 | sequential V100 test; VRAM/latency |
| E09 | Real `bpm` semantics for /8 and `TimeMap_GetDividedBpmAtTime` | final `bpm_basis`/meter contract | R3-03 U1 | live tempo-map probe on 6/8, 7/8 |
| E10 | Linear ramp model: BPM-linear or time-per-beat linear | exact QN integral | R3-03 U2 | analytic + live marker comparison |
| E11 | Marker `measure`/`beat` semantics when `beat != 1` | bar/beat for mid-bar markers | R3-03 U3 | live `TimeMap2_timeToBeats` matrix |
| E12 | Stretch-marker positions in saved RPP | verified source mapping | R3-03 U6; R3-06 | read RPP + live readback |
| E13 | Attack-start offset vs detector frame | ±10 ms cut tolerance | R3-02 U4 | impulse/DI calibration |
| E14 | `MIDI_GetProjTimeFromPPQPos` on looped items | MIDI iteration/readback | R3-02 U5 | live looped MIDI take |
| E15 | Send/return availability and EDT/reverb-to-dry accuracy | C1–C3 mix claims | R3-04 U1/U3 | live send/return capture + known degradation |
| E16 | Is separation good enough for SMR? | trust in C12 | R3-04 U4 | stem-separation benchmark on metal |
| E17 | JND, inter-rater agreement, severity calibration | calibrated severity | R3-04 U6; P07 | blind human study |
| E18 | Group/lane sync semantics through API | safe group edits | R3-06 blockers | live null test on grouped items |
| E19 | Save/DIRTY semantics, undo across reorder, FX move GUID | save/checkpoint/rollback proof | R3-06 blockers | live isolated copy |
| E20 | Human preference and reference distribution | mix acceptance | R3-04 §8 | expert panel + owner ratings |

---

## 5. Conflicts reconciled in R3

| Conflict | Resolution | Status |
|---|---|---|
| 12 vs 19 MCP tools | Actual code: 19 tools, 6 resource endpoints; 12 is stale documentation. `tool_surface.json` becomes the single source; doc-test fails on drift. | closed (D22) |
| `ProjectRevision` vs `RevisionRef` vs v2 | One `RevisionRef` with adapter; v1 preview allowed, v2 required for apply. | closed (D10) |
| ID namespace: hash vs ULID | Hash for derived records, ULID for authored/session records. | closed (D08) |
| `ges_` vs `peg_` | `peg_` is the canonical gesture ID; `ges_` is deprecated and never emitted. R3-05 registry and blueprint are aligned. | closed |
| `uncertainty_s` vs `uncertainty_ms` | `uncertainty_ms` canonical; `_s` deprecated alias. | closed |
| Capture "read-only" vs transaction | Capture is side-effecting R2; reads do not claim immutability. | closed (D05) |
| `section_type` vs `section{label,label_source,level}` | `section_type` is a derived view only when label_source is human/marker; not a stored field. | closed |
| `MixObservation` vs `ConstructEvidence` | R3-04 canonical chain: `MixObservation → MixClaim → IssueHypothesis → PostChangeCheck`; no second class. | closed |
| `same_state` (counter+fingerprint) vs epoch+content | Two predicates `same_identity`/`same_content`; counter diagnostic. | closed (D10) |
| Distance as quality | Guardrail only; acceptance uses the target construct. | closed (D15) |
| `caused_by` edge | Forbidden as fact; hypothesis + discriminates only. | closed (D14) |
| 8 vs 3 signal stages | Eight stages; `immediately_pre_fx` maps to `take_sum_pre_fx`. | closed (D02) |
| `source_file` as DI | `source_kind` annotation; vers1 guitars are amp_print. | closed (D04) |

---

## 6. Unresolved or partially signed

1. **Mutual signatures.** R3-05 read R3-02 as DRAFT at one point; R3-02 finalized later. The content converged; the R3-05 registry and blueprint now use canonical `peg_` for gesture IDs.
2. **Tool count.** 19 is fact; 14 is a hypothesis. No evaluation has been run. Do not change the surface until task-eval exists.
3. **Manifest budget.** 1500 tokens on the 33-track fixture is a paper estimate. Measure before freezing.
4. **Severity and thresholds.** All musical thresholds are provisional. No calibration data exists.
5. **Goal ownership.** `Goal` lives in R3-05, `Plan` in R3-06, `intentional_character` in R3-04. Who resolves conflicting goals is not specified. Proposal: the owner's goal is an annotation (`ann_`) referenced by plans; a plan may not invent intent.
6. **Band grid.** 28 vs 29 bands must be unified before cross-module comparisons (R3-04 U11).
7. **`project_id` vs instance.** `project_id` remains the wire key; `pri_` distinguishes reopen. The adapter must not conflate them.

---

## 7. What R3 does not decide

R3 does not prove that any listener hears correctly, does not validate metal transcription, does not calibrate mix severity, does not prove feel preservation, and does not choose the final host model. Those remain empirical or owner decisions listed above. The ledger is a record of what is accepted, proposed, owned and blocked — not a claim that the product is ready.

---

## 8. Wave A implementation record (2026-11-12)

**Status:** P0 contracts landed as new files only. No existing product file was edited; the pre-existing dirty tree is preserved. Focused suite: **303 passed** (the six new test files). Command:

```
PYTHONPATH=src $LOCALAPPDATA/hermes/cache/scratch/rw-venv/Scripts/python.exe \
  -m pytest -q tests/test_timebase_schema.py tests/test_ids_event_model.py \
  tests/test_context_audio.py tests/test_revision.py tests/test_mix_models.py \
  tests/test_contract_integration.py
```

**Files (new, owner in brackets):**

| File | Owner | Content |
|---|---|---|
| `src/reaper_audio/timebase.py` | lane 1 | `TimeBasis`, `MappingStatus`, `TimeUnit`, `TimeRef`, `Interval`, `TimeMap`, `compose`, `to_sample_index` |
| `src/reaper_audio/context/__init__.py` | lane 1 | package marker |
| `src/reaper_audio/context/errors.py` | lane 1 | `ContractError` + stable subclasses/codes |
| `src/reaper_audio/context/schema.py` | lane 1 | `SCHEMA_VERSION`, `RecordKind`, `RevisionRef`, `RecordEnvelope`, JSON round-trip |
| `src/reaper_audio/context/ids.py` | lane 2 | prefix registry, ULID `new_id`, deterministic `derive_id`, `parse_id` |
| `src/reaper_audio/analysis/event_model.py` | lane 2 | `SignalEvent`, `PerformanceOccurrence`, `PerformanceEvent`, `PerceivedGesture`, `ScoreObject` |
| `src/reaper_audio/context/audio.py` | lane 3 | 8 stages, capture/listen/asset/delivery types, stage-method compatibility |
| `src/reaper_audio/context/revision.py` | lane 5 | `RevisionRef` adapter, `same_identity`/`same_content`, placeholder discipline |
| `src/reaper_audio/mix/models.py` | lane 4 | `MixScope`, `MixObservation`, `MixClaim`, `IssueHypothesis`, `PostChangeCheck` |
| `src/reaper_audio/mix/__init__.py` | lane 4 | exports only |
| `tests/test_timebase_schema.py` | lane 1 | 19 tests |
| `tests/test_ids_event_model.py` | lane 2 | 27 tests |
| `tests/test_context_audio.py` | lane 3 | 140 tests |
| `tests/test_revision.py` | lane 5 | 35 tests |
| `tests/test_mix_models.py` | lane 4 | 54 tests |
| `tests/test_contract_integration.py` | lane 6 | cross-module gates |
| `docs/research/round-3/WAVE-A-INTEGRATION-CHECKLIST.md` | lane 6 | ownership/import/gate checklist |

**P0 gates:** schema round-trip PASS; missing time basis PASS; unknown prefix PASS; invalid status PASS; invalid revision PASS; deterministic derived IDs PASS; authored IDs unique PASS; no import cycle PASS; old public types import PASS; single `TimeRef`/`Interval`/`RevisionRef`/`Stage` definitions PASS; `ast_sha256` content-only PASS; stage/method matrix PASS; delivery honesty PASS; mix/event invariants PASS; registry completeness PASS after the `hypgrp` fix.

**Amendments and integration decisions:**

| ID | Decision | Class | Evidence | Next action |
|---|---|---|---|---|
| WA-01 | `ast_sha256` is a content address only. `new_id("ast_sha256")` and `derive_id("ast_sha256", …)` raise `ContractValidationError`; 26-char bodies are rejected; 64 lowercase hex is canonical. | accepted | lane 3 initially allowed both shapes; lane 2 registry made 26-char derivable; corrected after inspection; 303 tests pass | keep the invariant in all future asset producers |
| WA-02 | `hypgrp` is a registered authored prefix. | accepted | lane 6 found it missing; two integration tests failed; added to `PREFIXES` and `AUTHORED_PREFIXES` | none; registry complete |
| WA-03 | `same_identity` compares `project_id` + `instance_id` and deliberately does not compare `epoch`. Legacy records default `epoch=0`; instance is already per-open. | proposed (Wave A) | lane 5 deviation, documented in `context/revision.py` | revisit in Wave B if epoch becomes content-bearing |
| WA-04 | `from_project_revision(fingerprint=None)` returns `fingerprint="unavailable:…"` with `fingerprint_scope="coarse"`; `same_content` is then false. No digest exists in the live pipeline yet. | accepted placeholder | lane 5 audit: Lua `project_identity` has no instance/epoch/fingerprint; `ProjectRevision` has no fingerprint | Wave B must add `project.digest` before any `verified` freshness claim |
| WA-05 | Domain errors currently subclass `ContractValidationError` inside their modules and mutate `code`; the central registry does not yet discover them. | proposed fix (P1) | lane 6 O5: `capture_stage_unsupported`, `event_invariant`, `mix_invariant`, `hypothesis_not_testable`, `invalid_id` | Wave B: named subclasses in `context/errors.py`; behaviour unchanged |
| WA-06 | Two ID grammars still coexist: `mix/models.py::_prefixed` and `context/audio.py::_CAPTURE_ID/_OBSERVATION_ID` accept short bodies that `context.ids.validate_id` rejects. | open (P1) | lane 6 O2/O3 | Wave B: route both through `context.ids.validate_id` and widen fixtures to 26 chars |
| WA-07 | `analysis/__init__.py` eagerly imports the audio stack; the P0 modules themselves have no audio dependency. | environment only | lane 2 workaround; dev venv now carries numpy/scipy/soundfile/pyloudnorm/phonometry | no product change; keep the dev venv for tests |
| WA-08 | `ObjectKind.return_` is the enum member for value `"return"` because `return` is a Python keyword. | accepted | lane 3 deviation | serialize by value, never by member name |

**Blockers unchanged by Wave A:** accessor semantics, multichannel, `&128`, tail, PDC, host audio delivery, Flamingo V100, tempo `/8`/ramp/marker semantics, stretch-marker positions, attack calibration, MIDI loop PPQ, mix calibration, human ratings. Wave A deliberately did not touch them.

**Next safe wave:** Wave B. Recommended split: one owner for `scripts/reaper_bridge.lua`; one owner for the Python capture adapter (`daw/session.py`, `bridge_client.py`, `analysis/artifacts.py`); one owner for read-model fields (`item_context` is Lua, so the Lua owner lands both capture and read fields in one merge point); one owner for tests/fixtures. Do not start any acceptance gate until the live isolated-copy tests exist. The P0 modules are now frozen for consumers; changes require an entry in this ledger.

---

## 9. Wave B implementation record (2026-11-12)

**Status:** capture/revision vertical slice landed; **601 passed, 2 skipped** in the full suite. The two skips are the live REAPER tests, gated on `REAPER_WORKBENCH_LIVE=1` and a disposable open project. No live REAPER, render, listener or model run was executed. The pre-existing dirty tree is preserved; existing files changed only in the five areas below plus two bridge-client fixtures.

**Focused command/result:**

```
PYTHONPATH=src $TEMP/reaper-audio-library-python313/python.exe \
  -m pytest -q tests/test_wave_b_capture.py tests/test_wave_b_digest.py \
  tests/test_wave_b_bridge_contract.py tests/test_wave_b_live_capture.py
-> 85 passed, 2 skipped
```

Full suite: `601 passed, 2 skipped in 12.69s`.

**Changed files:**

| File | Lane | What changed |
|---|---|---|
| `scripts/reaper_bridge.lua` | 1 | `project.digest`; additive `item_context` fields (`D_STARTOFFS`, stretch markers, loop/beat-attach/channel/group, source metadata); capture `channels`/`sample_rate`/`tail_ms`; `render_settings_used`/`channels`/`sample_rate`/`tail_s`/`restore_report` |
| `src/reaper_audio/reaper/bridge_client.py` | 3 | `project_digest()`; `capture_render` args and response validation; `render_action`/settings/restore checks |
| `src/reaper_audio/analysis/artifacts.py` | 4 | `RegisteredArtifact`, `register_asset(s)`, streaming sha256, manifest `assets[]`; `register_existing_files` compatibility wrapper |
| `src/reaper_audio/context/revision.py` | 5 | `DigestEvidence`, `digest_evidence`, `revision_from_digest`; full/coarse rule |
| `src/reaper_audio/daw/session.py` | 2 | typed `Capture`/`AudioAsset`; digest-bound revisions; tail/channels/sample-rate; restore/no-new-objects guards; legacy dict compatibility |
| `tests/test_bridge_client.py` | integration | two fixtures updated to the new capture response contract |
| `tests/test_wave_b_*.py` | 6 | 85 new tests plus skipped live procedure |
| `docs/research/round-3/WAVE-B-LIVE-CHECKLIST.md` | 6 | disposable-copy procedure and gates |

**Revision digest field coverage (Lua `project.digest`):**

- Covered: `tracks` (volume/pan/mute/solo/folder depth/FX list with normalized params/sends with dest GUID, volume, pan, mode), `items` (position, length, `D_STARTOFFS`, playrate, mute, fades, active take, source path, loop, beat-attach, stretch-marker positions), `tempo` (time, BPM, numerator, denominator, linear).
- Missing: `envelopes`, `master`. `coverage` marks both false; `missing` lists both.
- Consequence: the digest is always **coarse**, so `same_content` can never be true for a live capture. This is intentional honesty, not a bug: envelope and master coverage are not implemented.
- Mutations tested: track volume/pan/mute/solo, FX normalized param, send volume/pan/mode, item position/length/offset/playrate/mute/fades, tempo BPM. Unrelated metadata (selection, transport) is outside `fields` and does not change the fingerprint.

**Capture contract implemented:**

- A normal capture returns per-track `track_stem_post_fx` and one separate `master_mix_post_fx` native render for the requested project-time window.
- Accessor non-master is `take_sum_pre_fx` + `audio_accessor`; master remains `master_mix_post_fx` + `native_render`. `source_kind` is `unknown` for the current material; no DI claim.
- Every asset carries `ast_sha256_<64hex>` equal to the hashed bytes, source GUID, capture ID, stage, method, channel view, sample rate, frames, exact project/file window, bytes, lease ID, status and typed `RevisionRef`.
- Capture reports render settings, channels, sample rate, tail, solo state and restore report; it refuses on changed track/item structure and restores settings/selection/dirty in Lua `finally`-equivalent xpcall path.
- Temporary audio is manifest-owned; unregistered files are preserved; cleanup removes only registered names.

**Wave B amendments / open gaps:**

| ID | Decision / gap | Class | Evidence | Next action |
|---|---|---|---|---|
| WB-01 | The digest explicitly marks `master=false` and `missing=["envelopes","master"]`. Master volume/pan/FX/sends are sound-affecting but not hashed. | accepted truthfulness gap | lane 6 P0; Lua `project_digest`; patch applied | Wave C: include master track or keep it missing and never claim full |
| WB-02 | Every live digest is coarse because envelopes are missing; `same_content` is always false for live captures. | accepted consequence | lane 5 + lane 6 | Wave C: envelope reader, or keep partial and rely on scoped invalidation |
| WB-03 | Accessor capture with `tail_ms>0` fails closed: accessor sampling has no tail. | accepted refusal | lane 6 P1; `session.py` duration check | document; use render for tail |
| WB-04 | Tail captures have no analysis consumer: `analyze` has no `tail_ms` and `_load_rendered_audio` expects exact section duration. | open P1 | lane 2/lane 6 | Wave C: pass tail through analyze or keep tail capture-only |
| WB-05 | `Capture.missing` cannot carry digest gaps; digest gaps live in `coverage.digest_missing` and warnings. | accepted schema compromise | `Capture` invariant; lane 2 | Wave C: add a distinct `digest_missing` field or relax invariant |
| WB-06 | Legacy `capture_method` string and typed `capture_method_typed` coexist; no production reader consumes the typed value yet. | open P2 | lane 2 | Wave C: migrate readers, then remove the legacy key |
| WB-07 | `session` defaults `restore_report` to `{restored: True}` when a bridge omits it; real `ReaperBridge` rejects that, but a loose double could record an unverified restore as true. | open P2 | lane 2/lane 6 | Wave C: require the field, no default |
| WB-08 | `digest_evidence` trusts self-asserted coverage flags: `coverage` all true + `fields={"tracks":[]}` yields `scope="full"` without checking that each covered group carries a field payload. Live Lua is always coarse anyway, so this matters only for forged/future digests. | open P1 | lane 6 review | Wave C: explicit coverage-name → field-group cross-check; keep `full` unreachable until then |

**Live gate:** blocked. No REAPER instance attached; no live track/master render, no independent native-render comparison, no audio-equivalence, no listener/model check. The exact procedure and pass/fail gates are in `WAVE-B-LIVE-CHECKLIST.md`; `tests/test_wave_b_live_capture.py` is skipped by default and never opens/saves/closes a project.

**Smallest next safe wave:** Wave C, with one owner for the envelope/master digest extension (Lua + revision), one owner for the context ledger/store/manifest (Python-only), one owner for the capture→analysis tail path, and one test owner. Do not claim full revision freshness until WB-01/WB-02 close.

---

## 10. Wave C implementation and live record (2026-11-12)

**Status:** digest P0 closed on the disposable fixture; capture→analysis tail path closed. Full suite **725 passed, 2 skipped** (live tests skipped by default). Focused Wave B/C suite **246 passed**. Live REAPER `7.84/x64` gate **2 passed**; independent native master render sample-identical. Pre-existing dirty tree preserved; `mcp_server.py` unchanged.

**Changed files:**

| File | Owner | What changed |
|---|---|---|
| `scripts/reaper_bridge.lua` | A | `project.digest` adds master, automation (track/master/item/FX envelopes, points, automation items), 27 project render/playback settings and normalized `<FXCHAIN>`/`<MASTERFXLIST>` state; honest coverage/missing; `project_digest` live size 93.9 KB, 49 ms |
| `src/reaper_audio/context/revision.py` | A | `DIGEST_COVERAGE_PATHS`, payload predicates, `digest_coverage_gaps`; required set adds `master`, `automation`, `project`, `fx_state`; unbacked claims become `missing`/coarse |
| `src/reaper_audio/daw/session.py` | B | `analyze(tail_ms, channels, sample_rate)`; `_load_rendered_audio` accepts section+tail and separates project/file/measured windows; mandatory factual `restore_report`; accessor+tail rejected before render |
| `src/reaper_audio/analysis/artifacts.py` | B | manifest-only cleanup de-duplicates preserved paths; regression coverage for foreign/unregistered files, hashes, lock failures |
| `tests/test_revision.py`, `tests/test_wave_c_digest.py` | A | 134 digest tests |
| `tests/test_wave_c_capture.py`, `tests/test_wave_c_analyze.py`, `tests/test_daw_session.py` | B | 55 capture/analyze/session tests |
| `tests/_digest_payloads.py`, `tests/test_wave_b_digest.py`, `tests/test_wave_b_capture.py`, `tests/test_wave_c_analyze.py` | Hermes | shared full-coverage fixtures; pre-Wave-C fixtures upgraded |
| `tests/test_wave_b_live_capture.py` | Hermes | master xfail removed; live gate now requires `coverage.master=true`, `missing=[]`, `scope=full` |
| `docs/research/round-3/WAVE-C-LIVE-REPORT.md` | Hermes | exact live evidence |

**Live digest:** coverage all true for `tracks/items/takes/fx/sends/tempo/master/automation/envelopes/project/fx_state`; `missing=[]`; `scope=full`; master has 3 FX; automation has 11 track rows, 1 tempo-map envelope, 108 items scanned, 0 FX envelopes; project has 27 settings; `fx_state` has 12 rows.

**Live fingerprint mutations:** master volume, master FX enabled, master FX parameter and master tempo-map automation point each changed the fingerprint and were restored exactly by `transaction.undo`.

**Independent render:** normal REAPER action `42230`, custom 0–1 s, tail 250 ms, master mix, 48 kHz stereo, `RENDER_ADDTOPROJ=0`; captured and independent masters both 60,000 frames / 1.25 s; `max_abs_diff=0.0`, `null_residual≈−280.26 dBFS`, `correlation=1.0`. Settings restored exactly (`RENDER_BOUNDSFLAG=1`, `RENDER_ENDPOS=311.3063217`, `RENDER_TAILMS=1000`, `RENDER_SRATE=44100`, original `RENDER_FORMAT`, empty file/pattern). Retained: `work/live-test-waveb/compare2/`.

**Closed Wave B gaps:** WB-01 (master hashed), WB-02 (automation/envelopes covered), WB-03 (accessor+tail explicit refusal), WB-04 (tail analyzable without measuring tail), WB-07 (restore_report mandatory), WB-08 (coverage/payload cross-check). WB-05 and WB-06 remain accepted compatibility compromises. **New gaps:** WC-01 subjective listening not performed; WC-02 no same-song mix A/B; WC-03 live automation only tempo map (no track/item/FX envelopes in fixture); WC-04 MCP has no tail/channel/SR wiring; WC-08 scale beyond 11 tracks/108 items untested; WC-09 7.84-only live evidence.

**Next safe wave:** Wave D — context ledger/store and views over the now-verified digest/capture records, then MCP wiring of the existing capture contract. Keep subjective listening and same-song A/B separate; do not claim mix quality from this fixture.

---

## 11. Wave D same-song benchmark record (2026-11-12)

**Status:** experiment; first autonomous mix frozen; objective post-freeze comparison exists; no listening evidence; not production-ready. Wave D used **3 subagents total, 3 completed, 0 active**, never more than two at once. Full suite **760 passed, 2 skipped**.

**Environment:** REAPER 7.84/x64; `HIMMP-In-Solitude.rpp`, 41 tracks (34 source + 7 buses), 34 items, 300.52 s, tempo 200→240→200. Experiment MCP entry `reaper-workbench-benchmark` (temporary Python 3.13; PYTHONPATH points at this checkout) verified by `hermes mcp test`: **19 tools, 5 resource templates**; module path `src/reaper_audio/mcp_server.py`. Existing `reaper-mcp` entry untouched. `reaper_analyze` now accepts `tail_ms`, `channels`, `sample_rate` with validation and tests.

**Dataset:** official HiMMP CC BY share; 43 source files / 1.776 GB with SHA-256 and Box IDs; post-freeze 9 loudness-matched masters + 36 role stems / 3.478 GB. The 8 producer masters are a distribution, not one target; role stems are independently normalised and must not be used to infer relative balance.

**Frozen mix:**

| Artifact | Path | SHA-256 |
|---|---|---|
| Project | `work/benchmark/himmp/project/HIMMP-In-Solitude.rpp` | `e5458b3017b7e135284deb22adb32f99bff6408d7d22a61bf5aeccc89116f3ee` |
| Master | `work/benchmark/himmp/final/HIMMP-In-Solitude-Hermes-master.wav` | `0d8d50a447cc96a0b84b46d5355ce0fb4803a17135d63075286f5b7be2c9983c` |

Master: −9.93 LUFS, sample peak −2.43 dBFS, true-peak estimate −0.84 dBTP, crest 10.58 dB, LRA 2.87 LU, stereo correlation 0.706, side/mid −7.63 dB, 0 clipped samples. 44.1 kHz stereo, 300.52 s. True peak is an estimate, not a compliance meter. The model did **not** hear audio (`not_heard`).

**Objective comparison:** time-aligned by 50 ms log-RMS envelope (±12 s), gain-only matched to each reference; no EQ/compression/limiting applied. Eight producers: band MAE 1.54–3.06 dB (Nolly closest 1.54; Exeter furthest 3.06), band max 3.74–10.19 dB, crest 8.49–12.80 dB, LRA 2.03–4.12 LU, side/mid −3.12…−6.34 dB. Team reference: band MAE 2.59 dB, LRA 4.12 LU. Lags 0.95–4.40 s; not sample-locked. Table and caveats: `docs/research/round-4/WAVE-D-BENCHMARK-REPORT.md`, raw JSON `work/benchmark/himmp/analysis/objective-comparison.json`.

**Processing evidence:** 25 sources, 96 verified/10 inferred/12 unknown claims with timestamps; exact chains remain unknown; no reference audio was supplied to the mixing model before freeze. Register: `work/benchmark/himmp/evidence/PROCESSING-EVIDENCE.md`.

**New gaps:** WD-01 no listening; WD-02 bus/solo-stem render missing — receive-only buses render silent as normal track stems and MCP has no bus-stem tool, so role-stem comparison is unfinished; WD-03 MCP has no external-WAV comparison; the table is offline analysis; WD-04 final WAV came from the library's native render/capture path, not an MCP render tool; WD-05 one song, one REAPER version, 41 tracks; WD-06 one-shot MCP analyses are process-local — use a persistent session for analyze → evidence → release; WD-07 exact producer chains unknown.

**Next safe wave:** fix the capture/MCP surface for aux-bus solo/stem rendering and external reference comparison, then run a reference-guided pass only after the user has heard the frozen first mix. Do not call this mix professionally successful from metrics or tests.

