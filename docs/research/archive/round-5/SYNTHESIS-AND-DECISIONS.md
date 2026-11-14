# Round 5 — synthesis and decisions

**Inputs:** `AGENT-A-PERCEPTUAL-CONTEXT-REPORT.md` (721 lines, 31 sources) and
`AGENT-B-MUSICAL-ACTIONS-REPORT.md` (817 lines, 30 sources), both written against
`main` @ `86b14bb` with the shared dirty tree preserved.

**Status:** research and decisions only. No source code, tests, project files, model
weights, datasets or REAPER sessions were touched by this round. The concurrent
capture-performance work was left alone; `analysis/events.py`, `temporal.py` and
`metrics.py` were being edited by that task while the reports were written, so their
line numbers may have moved since.

**What this document is not:** not an implementation plan with dates, not a quality
score, not a genre recipe, and not a claim that any of the proposals below are
implemented. Every capability row below names its current code location or states
plainly that it does not exist.

---

## 1. Review of the two reports

Both reports were re-checked against the code before being accepted. The checks below
were run in this checkout, not taken on trust.

### 1.1 Independently confirmed in code

| Claim | Check |
|---|---|
| Tool surface is 21 tools / 6 resources, not 19 | `grep -c "@server.tool()"` = 21; `tests/test_mcp_server.py:110` asserts `len(names) == 21`. Both reports are right that the ledger's D22 is stale |
| `temporal.masking()` is a band-dominance heuristic, not a threshold | `temporal.py:771-835`: same-cell comparison, per-cell broadband centring, both-active gate, max median band difference ≥ `min_db=10.0` |
| `mix_evidence(masking_db=6.0)` can never bind | `masking()` cannot emit a pair below 10 dB and `bundle.py:277` calls it with defaults, so the 6.0 filter is unreachable. Confirmed |
| Partial loudness subtracts solo-render power from the mix and re-runs the loudness model on the residual | `partial.py:246-249` (`rest_power = max(mix_power - source_power, floor)`), `:250-252`, share at `:320-324` |
| `D_STARTOFFS` is never writable by a transaction | `grep -n D_STARTOFFS scripts/reaper_bridge.lua` → read at `:381`, `:754`; copied in `item.duplicate` at `:3582`; no setter. Confirmed |
| `item.split` moves the right slice without revealing its past | `reaper_bridge.lua:3504-3556` sets `D_LENGTH` (left tail), `D_POSITION` (right), `D_FADEINLEN`/`D_FADEOUTLEN`; `D_STARTOFFS` untouched |
| Split readback returns geometry-free state | `read_edit_state_after` for `item.split` returns `{ item_count = … }` only (`reaper_bridge.lua:3046-3049`) |
| Fade length is set to the full overlap | `fade_length = min(overlap > 0 and overlap or fade, left_new_length, right_length)` → a 69 ms overlap yields a 69 ms fade |
| Python never reads the edit-critical item fields | `grep -rn "start_offset_s" src/reaper_audio/` → no Python hit; `playrate`/stretch-marker hits are only in `api_catalog.json` |
| Panorama item rows drop offset/playrate/stretch/fades/groups | `panorama.py:178-222` keeps id/guid/track/position/length/take_count/take ids only; `reaper_project(include_items=True)` still carries the rest |
| `event_model.py` is a contract island | only `event_model.py` + `tests/test_ids_event_model.py` + `tests/test_contract_integration.py` reference it |
| `EditPlan.revision` is the legacy `ProjectRevision` (project_id/change_count/path), not a `RevisionRef` | `daw/session.py:41-55` and `EditPlan` at `:68-80` |
| Two different operation caps | `editing.py:15` = 200 cuts; `session.py:1675` = 256 operations |
| `edit_map` hard-codes a 120 BPM fallback and English name→role guesses | `edit_map.py` `tempo_grid` (`points` empty → `120.0`) and `_group` (kick/snare/overhead/… → `"drums"`, guitar/gtr/amp → `"guitars"`) |

### 1.2 Discrepancies between the reports, resolved here

**R1 — How many relation edges fit.** A proposes "top-8 per source" and then bounds it
at "≤ 168 edges" for 42 sources. 42 × 8 = 336, so 168 only holds if relations are
undirected and deduplicated per pair. **Resolution:** relations are defined as
*unordered pair* records with a `from`/`to` interpreted as the louder/quieter side at
that band, not as two directed claims. The acceptance bound is then
`≤ min(k · S, S(S−1)/2)` and the test must assert the definition it uses. Without this,
A's byte estimate (37 KB at 220 B/edge) and its test bound disagree.

**R2 — The 41-source audibility flood.** A's executive summary says every source falls
below the 5 % band; its own §9 flags the real capture as unmeasured. **Correction:**
with *n* equal sources the uniform share is `100/n` %, which is 2.44 % at n = 41, so the
lower bound is structurally below the uniform share. On real, unequal material the
number of flagged rows is unknown and must be measured (Experiment X3). What is
certain from the code is that the threshold does not scale with *n* and therefore
encodes "too quiet" as a function of track count — which is not a musical fact.

**R3 — Relations are "integer-coded".** A is right that `crosslinks` emits
`relation: <int>` (`panorama.py:1992-1997`), but there is a
`crosslink_relation_legend` in the same document (`:2148`), so it is resolvable. The
real gap A identifies — that there are **no audio relations at all**, only structural
ones — is correct and is the one that matters.

**R4 — Q-A6 "licence-clean corpora: not answered".** B marked this open after reading
A's §0.5, but A answers it in §2.4: ODAQ is permissive, Slakh2100 is CC BY 4.0,
Mix Evaluation Dataset 2.0 exists only as an unlicensed lossy MP3 mirror, Cambridge-MT
research use is not pre-agreed, MedleyDB is non-commercial. **Resolution:** there is
exactly one licence-clean *real, dense, same-song, multitrack* asset in play — HiMMP
"in solitude" (CC BY, recorded at `WAVE-D-BENCHMARK-REPORT.md:84`). Everything else is
synthetic, degraded, restricted, or unlicensed. That single fact constrains the whole
test matrix and is stated in §7.

**R5 — MedleyDB's licence name.** B writes "CC BY-NC-SA"; A could only verify
"offered free of charge for non-commercial". **Resolution:** cite it as
*non-commercial terms; exact licence name unverified*. Do not repeat a specific
Creative Commons variant until the download page is read.

**R6 — Two new panorama blocks, one budget.** A adds `audio_relations` + `texture` +
`reference_links`; B adds `source_map` + `grid_hypothesis`. Neither priced the other.
**Resolution:** one combined budget test, run on a real capture, before either block
ships (see §6). By the reports' own arithmetic the addition is roughly 50–60 KB on a
237 KB full-song document, but that is an estimate, not a measurement.

**R7 — Texture truncation vs the no-silent-omission rule.** A suggests computing
texture for "the first 8–16 sources by energy, or all if budget allows". **Resolution:**
any such subset must carry `coverage`/`truncation` fields exactly like every other
domain, or the block is rejected. Energy-ranked selection is also a musical prior; it
must be stated as one.

**R8 — `pan_relation` is geometric, not audible.** A reads pan from the project map
(`panorama.py:1627`), which is correct, but neither report captures `I_PANMODE` or the
pan law. **Resolution:** `pan_relation` is labelled a *project-map fact*; any claim
that depends on width or mono-compatibility must additionally cite pan mode/law, which
is a capture gap (§5, row 2).

### 1.3 Where the reports agree without having coordinated

Both independently concluded, from different evidence, that (a) no absolute
click/gap/masking threshold is defensible and every gate must be relative or a named
policy key that fails closed; (b) a learned embedding is a retrieval/ranking aid and
not an optimization objective; (c) source separation is analysis-only and 4-stem, so
"guitar" is not a stream; (d) the missing primitive that both workstreams need is a
**non-committing render of a candidate**; (e) the current action layer cannot verify
what it did. Those five agreements are the spine of §2.

---

## 2. One dependency-aware architecture

The two reports describe the same system from opposite ends. Joined, the dependency
order is:

```
L0  revision + capture record + assets (lease, hash)
      │
L1  project map ──► SourceMap (item/take/source/time/playrate/markers/groups/pan mode)
      │                     │
L2  signal measurements ────┤  (levels, bands, temporal, events, transients, phase,
      │                     │   partial loudness)
L3  audio relations ◄───────┘  (band dominance + pan relation, texture, track→master,
      │                        reference links)
L4  hypotheses (tempo, grid/meter, section/repeat, role) with alternatives
      │
L5  claims (evidence ids, alternatives, discriminator, dep_set, state, grade)
      │
L6  plans (expected_after, UsageScope gate) ──► apply (one undo block) ──► readback
      │                                                              │
L7  verification (same stage, same window, before/after delta) ◄──────┘
      │
      └──► invalidation records (asset + RevisionRef + dep_set) back into L3/L5
```

Five properties of this graph matter more than any individual node:

1. **L1 is incomplete today, and L6 cannot be correct without it.** The edge operation
   the owner's case needs (`position −= Δ`, `length += Δ`, `D_STARTOFFS −= Δ·playrate`)
   is not expressible because the third term has no writer and because Python never
   reads the first two from item context.
2. **L3 must not be built on L2 alone.** Band dominance without the pan relation and
   without the stage of both assets is the measurement the only blind study says is the
   *least* preferred basis for action (A-05: panning both sources apart beat every EQ
   strategy; cutting the masker alone was last).
3. **L5's `state` is decorative until L7 writes invalidation records.** A asked, B
   confirmed: nothing today links an applied edit to the asset hashes whose
   measurements it invalidated.
4. **L6 needs `expected_after` computed by the same code that renders the preview.**
   Today the preview returns the *before* state and the requested operations only, so a
   69 ms overlap silently becoming a 69 ms fade is invisible until after commit.
5. **The keystone missing primitive is `render_candidate(plan, window) -> audio +
   hash`**, executed as apply-in-undo-block → render → undo, or on a declared copy.
   Without it, every "verify by re-measuring" loop in both reports degrades to manual
   apply/render/undo. With it, reference corridors, FX search, seam acceptance and
   A/B freezes all become the same operation.

### 2.1 The representation contract, in one place

Additive only; the canonical record keeps everything it has.

| Field | Layer | Status today |
|---|---|---|
| `source_map[]`: item/take/source id, position, length, `start_offset_s`, `playrate`, stretch markers, loop flag, timebase, group id, channel mode, `mapping_status` | L1 | **missing entirely in Python**; data exists in the Lua `item_context` |
| `audio_relations[]`: kind, from/to (track/asset), window in project **seconds and bar/beat**, band, `band_delta_median_db`, `broadband_delta_db`, `pan_relation`, `active_fraction`, stage, channel view, method+version, `is_psychoacoustic_threshold`, evidence hashes, revision, state | L3 | **missing**; the numbers exist inside `masking_evidence` but with no pan, stage or window |
| `texture[]`: window 2–4 s, band levels, cross-band correlation, modulation rate/depth, cascaded depth, coverage | L3 | values computed per 100 ms cell and **discarded** (`temporal.py:406-423`) |
| `reference_links[]`: both stages, both windows, alignment method + lag, loudness match, `n_references`, section-type match | L3 | **missing**; corridor exists without any pairing record |
| `tempo_hypothesis[]` / `grid_hypothesis[]` / `section_hypothesis[]` | L4 | tempo curve exists (`tempo.py`); meter, phase and repeat relation do not |
| `role_hypothesis[]` | L4 | name-substring guessing only (`edit_map._group`) |
| `claims[]`: statement, construct ids, grade, confidence basis, alternatives, discriminator, supports/against, `dep_set`, `scope_set`, revision, state, `not_a_verdict` | L5 | schema exists (`mix/models.py`); not attached to captures |
| `expected_after[]` per plan | L6 | **missing** |
| `invalidation[]`: post-apply revision, target guids, cited evidence ids, stale asset ids | L7 | **missing** |

Rule across all of it: a window is never project-seconds-only. Every temporal scope
carries the project interval *and* its musical position resolved through the
revision-bound tempo map, because the user's projects have tempo changes and a single
base BPM is exactly the error `FULL-CAPTURE.md:58-61` already forbids.

---

## 3. Decisions we can make now

These follow from verified evidence plus current code. None needs a GPU, a new model,
or a new corpus.

| ID | Decision | Why (evidence) |
|---|---|---|
| **D5-01** | No absolute perceptual threshold in method code. All gates are either relative (before/after, same material/stage/window) or a named policy key that fails closed when unset. | A-03 (the group that built autonomous de-masking: objective masking measures are "ill-suited for quantifying perceived masking in multitrack musical audio"), A-05 (preferred technique changes by scenario), B's Q1/Q6 |
| **D5-02** | Rename `masking` → `band_dominance`; carry `pan_relation`, `broadband_delta_db`, `active_fraction`, `cells`, and `is_psychoacoustic_threshold: false` in the payload. Keep old keys as deprecated aliases for one version. | A's §4.2/§4.3; the payload's `masker`/`target` vocabulary implies an attribution the measurement cannot support |
| **D5-03** | Delete the unreachable `masking_db=6.0` path; one threshold lives in one place. Severity is `provisional` (null) unless a policy supplies a divisor. | `mix_evidence.py:34` vs `temporal.py:822-831`; `docs/POLICY.md` forbids constants in method code |
| **D5-04** | Partial loudness emits **no shares** unless a reconstruction gate passes at the declared node (median |Δ| ≤ 1 dB, p95 ≤ 3 dB per band). Gate at the node whose inputs were rendered: master **or** a bus, not master only. | `partial.py:246-249` subtracts solo powers from a limited master; `GAP-ANALYSIS.md:457` already sets the 1/3 dB rule; the bus variant is this synthesis's addition and makes the gate usable when the master fails |
| **D5-05** | After D5-04's reconstruction gate, report raw per-source shares with `n_sources`, node, stage, and window. Do not call a share “audibility,” label a source too quiet/exposed, or emit a built-in flag from fixed percentages or `n`-based rules. `100/n` may be included only as the descriptive equal-share baseline, never as a perceptual threshold. Any caller-requested classification must name a user policy and preserve the raw measurement separately. | `mix_evidence.py:84-98`; at 41 equal sources, `100/n` is 2.44 %, demonstrating why a fixed 5 % cutoff changes meaning with track count; no cited evidence validates that cutoff as audibility |
| **D5-06** | No learned quality head, no universal score, no "distance to zero" objective. Embeddings are retrieval/ranking aids. | A-10 (184 mixes / 19 songs: "virtually no correlation" between mixdown features and preference), A-20 (Fx-Encoder++ reports reduced parameter matching), project policy |
| **D5-07** | Source separation is analysis-only, read-only, lease-managed, never an edit source and never retained. | 4-stem taxonomy (A-17/A-18); retention contract in `WAVE-D-RESULTS-AND-NEXT-ACTIONS.md:114-120`; B concurs independently |
| **D5-08** | `item.edge` becomes a first-class operation that writes `D_STARTOFFS`, with exact readback. `right_trim_s` keeps its current meaning and is documented as *delay*, not *reveal*. | `EDITING-ACCEPTANCE.md:35`; `reaper_bridge.lua:3504-3556`; no `D_STARTOFFS` setter exists |
| **D5-09** | Split/edge/move readback returns full geometry (position, length, offset, fades, playrate, marker count, new guid, item count) and the plan carries `expected_after`. | `read_edit_state_after` returns `item_count` only; preview cannot show that overlap = fade length |
| **D5-10** | The action path enforces `UsageScope`: `destructive_cut` requires `basis="agreement"`, `n ≥ 2`, and a numeric value; `midi` requires pitch evidence. Plans citing no events stay valid. | `event_model.py:161-196`; nothing checks it today |
| **D5-11** | Split the two alignment meanings: `align_to_reference` (measured lag, drift, policy decides) vs `quantize_to_grid` (explicit intent only, never a default). | `daw/align.py:82-125` vs `daw/alignment.py:12-31`; `TZ.md` §5 forbids blind quantization; `EDITING-ACCEPTANCE.md:25` |
| **D5-12** | FX: chain topology is a model decision from context; parameter fitting is a bounded, measured search inside a user-supplied chain. No differentiable-DSP method is assumed to apply to arbitrary VSTs. No exhaustive search is described as cheap. | ITO-Master's identifiability split (B15), ST-ITO's 1,600-render budget (B14), Gaussian-prior negative result (B25), GRAFX/Text2FX/NablAFx scope (B16/B17/B21) |
| **D5-13** | Every model output (Flamingo, Ossicle, Audiobox, any separation) carries capture/asset id, window, channel view, checkpoint and inference status; unavailability is an explicit record, never an absent field. | `WAVE-D-RESULTS-AND-NEXT-ACTIONS.md:57-78`; A's §4.8 |
| **D5-14** | The panorama must carry the edit-critical item fields (SourceMap) and the audio relations; a "whole-project view" that cannot describe an edit is not the whole view. | `panorama.py:178-222` drops exactly the fields edits need; B's gap row 18 |
| **D5-15** | Fix the stale surface count everywhere (21 tools / 6 resources) and stop deriving capability claims from round-3 prose. | `DECISION-LEDGER.md:41`; `tests/test_mcp_server.py:110` |
| **D5-16** | No relationship record without a stage on both sides. Mix-vs-master comparisons are `incomparable` unless an offset is declared. | `GAP-ANALYSIS.md:214-217`; A's §4.9; the Wave D lags of 0.95–4.40 s show how far apart two "same" windows can be |

---

## 4. Questions that require experiments

These cannot be settled by reading. They are ordered by what they unblock.

| ID | Question | Experiment | What would count as an answer |
|---|---|---|---|
| E1 | Does the 2 ms envelope detector hold on amp-print guitars at 150–200 BPM with cymbals? | Hand-annotate 2 × 30 s of the user's own tracks (owner + one other), score F1 at ±10/±25/±50 ms, compare with the STFT-flux detector | F1 and the disagreement set; if F1 < 0.9 at ±25 ms, every downstream edit claim inherits that error |
| E2 | Does band dominance at 10 dB predict anything audible on dense distorted guitars? | Same-side guitar pair, sweep one down in 1 dB steps in the dominant band, forced choice, ~20 trials/level | A threshold and its monotonicity; if non-monotone, the feature is a locator only |
| E3 | Does partial loudness survive the reconstruction gate on a real capture, and what share distribution does it produce? | One real capture: compute `sum(stems)` vs master per band; then report raw shares, `n_sources`, node/stage/window, and the descriptive `100/n` equal-share baseline | Gate status at master and at a bus; reproducible shares with denominator and provenance; no built-in audibility flag. If a named user policy is supplied, report its result separately from the raw shares |
| E4 | What render round-trip cost does this machine actually sustain? | 100 renders of a 5 s section through the existing capture path; report p50/p95 | Converts every "1,600 evaluations" claim into wall-clock; sets the FX-search budget |
| E5 | Is 30 ms clustering safe on blast/tremolo material? | Run `edit_map` on a fragment with a known grid; count clusters containing > 1 attack | False-merge count vs tempo; may force tempo-dependent clustering |
| E6 | Do group operations propagate through the API? | Split one of two grouped items in a disposable project; read the other | Sync applies or not; if not, the plan layer must expand groups explicitly |
| E7 | Do stretch-marker positions survive save/reload exactly? | Place markers, save, reload, read positions back | Exact or lossy; gates any stretch verification |
| E8 | Is `MIDI_GetProjTimeFromPPQPos` correct on looped MIDI items? | Loop a 2-bar item, probe the mapping | Correct/incorrect; gates MIDI write verification |
| E9 | Can a SourceMap with stretch markers be inverted for edge edits? | Analytic test + live: place a marker, extend the left edge, verify the marker's source position is unchanged | Invertible or v1 refuses on markers |
| E10 | Do expert listeners agree on "clarity" in black metal? | Krippendorff's α on 30 paired comparisons, ≥ 5 experienced listeners | α ≥ 0.5 → clarity is a shared construct; below → only defect-level claims |

### 4.1 Questions the current literature cannot answer

- **Whether any masking metric is perceptually valid for black metal.** No study; the
  two systems that came closest both report the objective measure does not track
  perceived masking in music.
- **Whether pan-first de-masking holds for already-hard-panned double tracks.** The
  only blind comparison used centred two-source scenarios.
- **Whether per-instrument FX can be recovered from a stereo master.** No method does
  this; Fx-Encoder++ needs an instrument query, i.e. the separation it would justify.
- **Whether a monotone mix-style objective exists.** Only negative evidence.
- **VRAM/throughput of the named models on a V100.** Not published; nothing was run.
- **Whether 50 ms annotation tolerance is adequate for the music.** At 300 BPM a 32nd is
  25 ms — the literature's evaluation floor is coarser than the material's events.
- **What "the same mix, better" means for this user.** Only his ears; the library must
  never encode it.

---

## 5. Capability map

Cost is engineering cost, not GPU. "Uncertainty" is what could still invalidate the
capability after it is built.

| # | Capability | Source evidence | Current location / gap | Cost | Uncertainty | Acceptance test |
|---|---|---|---|---|---|---|
| 1 | Revision-bound capture with per-track post-FX + master assets, lease, hashes | `FULL-CAPTURE.md`; Wave D live proof | Exists: `context/full_capture.py`, `daw/full_capture.py`, `analysis/artifacts.py` | — | none for structure; audio still not proven delivered to the calling model | existing capture tests + `reaper_capture_read` hash verification |
| 2 | Project map complete enough to compute an edit | B01's (time, offset, duration) triple is the edit abstraction | **Gap:** Python reads none of `start_offset_s`, `playrate`, stretch markers; no pan mode/law | S | timebase edge cases (loop, reverse, section source, auto-stretch) | `source_map` unit tests: `src(10.0)=0.5`, `src(12.0)=2.5`; playrate 0.5 → `1.5`; 1-sample round-trip |
| 3 | Band dominance with pan/stage/active-fraction context | A-03, A-05, A-01/A-02 | Exists as `masking()`; **gap:** name, pan, stage, window, threshold constant | S | whether 10 dB means anything (E2) | fixture: pan −1.0/+1.0 → `opposite_side`; `is_psychoacoustic_threshold is False`; old keys still parse |
| 4 | Reconstruction-gated partial loudness | `GAP-ANALYSIS.md:457`; A-03 | Exists: `partial.py`; **gap:** no gate, shares always emitted | S | may fail on every real master (E3) | unreconstructed fixture (mix × 1.5 in 1–4 kHz) → `status="not_valid"`, no `share_*` key anywhere |
| 5 | Reconstruction-gated partial-loudness shares | arithmetic over `mix_evidence.py` | **Gap:** fixed 5 %/45 % rules are presented as audibility classifications | XS | reconstruction may fail; share is not perceptual audibility | 41 equal sources → raw shares near 2.44 % plus `n_sources=41`; no automatic issue flag; `100/n` is labelled descriptive only |
| 6 | Texture statistics over 2–4 s | A-13 (cross-channel correlation required), A-15 (cascaded AM), A-16 (multi-second completion) | Values computed per 100 ms and discarded (`temporal.py:406-423`) | S | window choice is a listening question (A's Q2) | noise vs tone separate by ≥ 0.3 in `cross_band_corr`; window_s reported, no silent averaging |
| 7 | Reference pairing with stage/section/loudness/n | A-04 vs A-10 reconciliation; B's Wave D lags | **Gap:** corridor has no pairing record | S | section typing does not exist, so `unknown` will be the common case | mix-vs-master without offset → `incomparable`; 1.2 dB loudness mismatch → refusal; n = 3 → `insufficient_references` |
| 8 | Claims with alternatives and falsifiers | `mix/models.py`; D14 | Schema exists; not attached to captures | S | a null confidence is honest but less useful to a caller | claim with < 2 alternatives rejected; no `better/worse/score` key in any serialised claim |
| 9 | Event evidence with a confidence *basis* | B05 (cross-dataset transfer fails), B06 (50 ms floor), B08 (sparsity ODF) | `event_model.py` exists, unused; `transients.py` computes confidence with no basis field | M | detector F1 unknown on this material (E1) | onset-only event cannot authorize a destructive cut; `basis="agreement", n≥2` does |
| 10 | Tempo/grid/meter/section hypotheses with alternatives | B06; `tempo.py` | Tempo curve exists with alternatives; **gap:** meter, phase, repeat relation, and a 120 BPM fallback in `edit_map` | M | meter may be genuinely ambiguous at 3/4 vs 6/8 | grid carries `meter` + `alternatives`; no bare-BPM path in the model-facing API |
| 11 | `item.edge` with `D_STARTOFFS` write | `EDITING-ACCEPTANCE.md:31-35`; B01 | **Missing** entirely | M | fade shape from `D_FADEINLEN` is REAPER-dependent | live: 10.0/2.0/0.5, left −0.069 → 9.931/2.069/0.431 ±1 sample; the attack at 10.500 stays at 10.500 |
| 12 | Full geometry readback + `expected_after` | B's gap row 12 | **Missing** (`item_count` only) | M | none structural | split 11.0 with tail .130/move .100/trim .025 → overlap 5 ms ±1 sample, fade = overlap |
| 13 | Preview/apply/undo with invalidation records | A's Q-B1; B's §4.4 | Transactions exist (`session.py`, Lua); **gap:** legacy revision, no invalidation, no expected-after | M | undo label matching is the only rollback proof | after apply, asset hashes named by the plan are marked stale against the post-apply `RevisionRef`; undo restores the digest |
| 14 | Bounded FX parameter search (1–3 params) | B19 (structured compressor), in-repo limiter/eq | Exists in two narrow tools (`daw/limiter.py` ≈ 33 renders, `daw/eq.py` calibration) | M | non-monotone parameters exist | recovers a synthetic target within ±0.3 dB in ≤ 12 renders; reports `no_monotone_relation` instead of guessing |
| 15 | Chain-topology choice from context | B28 (subgroups driven by need, not genre) | Model decision; no code needed | XS | role hypotheses are name-based today | plan states the topology reason and the evidence ids it used |
| 16 | Optional style-embedding search | B14, B25, A-10 | **Not admissible yet** | L | no monotone objective (A's Q-A3) | gated: objective must be a DSP construct or a corridor *rank* distance; monotonicity ρ > 0.5 on held-out material first |
| 17 | Optional separation / instrument-wise FX embeddings | A-17..A-20 | Absent; stays absent as an action path | L | 4-stem taxonomy, no metal evaluation | bleed matrix on our own stems before any use; never written to the project; lease-managed |
| 18 | Model descriptions bound to a capture | A-06, A-08; Wave D boundary | **Gap:** Flamingo/Ossicle re-render and ignore `analysis_id` | M | Audiobox default bf16 will not run on V100 SM 7.0 | every model record names capture id, asset hash, window, channel view, checkpoint |

**Reading the table:** rows 1–8 are context; 9–13 are actions; 14–15 are the safe
optimization core; 16–17 are gated research; 18 is optional perception. The dependency
is one-directional: actions depend on context, never the reverse.

---

## 6. Must-have primitives vs optional model-assisted analysis

The split is not "cheap vs expensive". It is "what the model can rely on without a
model in the loop" vs "what adds evidence that must be labelled as inference".

**Must-have (CPU, deterministic, no model):**
capture + revision + assets; SourceMap; levels/band levels/LUFS/LRA/crest/phase;
band dominance with pan/stage; texture summary; reference pairing with stage/section/
loudness/n gates; tempo map + grid/meter hypotheses; event evidence with confidence
basis; claim records; `item.edge`/split/move with `expected_after`, readback, one undo
block, invalidation records; reconstruction-gated partial loudness **only when its gate
passes**.

**Optional (GPU, model, always labelled as inference):**
Audiobox PQ/CE as a weak pre/post validator; Flamingo/Ossicle descriptions; separation
for section-level energy sanity checks; Fx-Encoder++ retrieval; a learned onset model
trained/validated on this material; a learned style objective (gated by monotonicity);
per-device amp models.

**Neither side is sufficient alone.** The deterministic layer cannot say whether a
change served the user's intent, cannot hear a wall of guitars, and cannot compare a
reference's character. The model layer cannot measure, cannot be checked, and in the
current evidence base cannot be trusted to identify a source or a chain. The system
works because the model *decides* on deterministic evidence and the deterministic layer
*verifies* what the model did — not because either is authoritative.

**Resource position.** Nothing in the must-have list needs the V100. The measured cost
of the existing DSP capture is the budget: 190.6 s render + 536.8 s analysis for a
300.5 s, 41-track project, 8 analysis workers, on the ~47 GB RAM machine. The optional
models have no verified VRAM or throughput figures on this machine — the reports
deliberately quote none, because none were published or measured. They must be
scheduled sequentially, and the Audiobox bf16 default is a known V100 blocker that
would need fp16.

**Panorama budget.** Current: 700,000 bytes / 256,000 tokens; full-song capture
237,477 bytes / 128,449 tokens; 20 s capture 198,480 bytes / 92,307 tokens (≈ 11× more
expensive per source-second, so short drill-downs must be budgeted). The combined
addition is estimated at 50–60 KB, but the only acceptable proof is a measured byte
count on a real capture with both blocks present, plus the existing fail-closed
`panorama_capacity_exceeded` path. A short capture is the binding case.

---

## 7. Test material, and what it can prove

| Tier | Material | Ground truth | Proves | Licence |
|---|---|---|---|---|
| T0 | synthetic: impulses at known samples, correlated stereo splits, a 3/4 click at 150 BPM | exact | detector/geometry/metric correctness | in-repo |
| T1 | rendered stems from a MIDI score, deliberate tempo ramp, injected hum/clip/DC | exact note times, exact tempo curve | event and tempo hypotheses vs truth | in-repo |
| T2 | HiMMP "in solitude" (41 tracks, 300.52 s, 200→240→200 BPM) | 8 producer mixes as *references*, not truth | action safety and measurement stability on real dense material | **CC BY** (verified `WAVE-D-BENCHMARK-REPORT.md:84`) |
| T3 | IDMT-SMT-Guitar/Bass, MedleyDB, MUSDB18 | stems where available | domain stress (distorted guitar, real drums) | research/non-commercial; **no shipping of derivatives**; MedleyDB's exact licence name unverified |
| T4 | a disposable copy of the user's project | the owner's ORIG | musical acceptance | private |

Two limits that must be stated wherever the matrix is used: **Slakh2100 is synthesised
from MIDI** (no room, bleed, amp or performance — it can prove a pipeline, never
realism), and **a reference-video soundtrack is not ground truth** — different mix,
lossy codec, platform loudness normalisation, possible resampling/sync shift, and
copyright. It is a listening reference for a human, never a numeric target.

---

## 8. High-information experiments, ranked

Ranked by expected reduction in model blind spots per unit of engineering effort.

1. **X1 — Onset F1 on the user's own guitars.** 2 × 30 s hand-annotated, two annotators,
   compare the 2 ms detector with STFT flux at ±10/±25/±50 ms. Everything downstream —
   edit maps, seam search, alignment, microtiming claims — cites these onsets. Cheap,
   CPU-only, and it can invalidate the whole event layer.
2. **X5 — ORIG geometry acceptance.** Implement `item.edge` + readback on a copy, run
   the owner's 69/27/22 ms case, assert the placed attacks do not move. This is the only
   written user acceptance case on file and it closes the largest concrete gap.
3. **X2 — Render-candidate round-trip cost.** 100 renders of a 5 s section, p50/p95.
   Prices every verification loop in both reports and converts the FX-search budget from
   arithmetic into a measurement.
4. **X3 — Reconstruction residual at master and at one bus.** One real capture, CPU
   only. Decides whether partial loudness survives at all, and produces the actual
   41-source share distribution that R2 leaves open.
5. **X4 — Band-dominance JND plus one hard-panned pair.** Listening test on a same-side
   guitar pair, forced choice, ~20 trials/level, plus the pan condition Wakefield &
   Dewey did not test. Decides whether 10 dB carries information and whether pan-first
   de-masking generalises to this arrangement.

X1, X2, X3 and X5 need no GPU and no new data. X4 needs the owner's ears and about an
hour of his time; it is the only one that can tell us whether the perceptual layer is
worth keeping.

---

## 9. Preservation of the user's requirements

| Requirement | Where it is enforced in this design |
|---|---|
| Full project context, nothing silently dropped | Canonical record keeps everything; every compact view carries `coverage`/`truncation`; R6/R7 reject any block that cannot be budgeted and any subset that is not labelled |
| No genre recipe, no hard-coded metal/guitar pipeline | D5-07, D5-11, D5-12; `edit_map._group` name guessing and the 120 BPM fallback are named defects, not features; subgrouping is justified by measured need (B28) |
| Complete track-plus-master sound evidence | Every relation cites asset hashes for both sides and both stages; capture already renders post-FX per track + master with `render_provenance` |
| BPM / tempo-map awareness | Every window carries project seconds *and* bar/beat through the revision-bound tempo map; grid hypotheses carry meter and alternatives; no single-BPM path |
| Safe, reversible edits | D5-08..D5-11: `expected_after`, full readback, one undo block, revision re-check at preview and apply, invalidation records |
| No artificial better/worse judge | D5-01, D5-02, D5-06; claims carry `not_a_verdict`; no score, no distance-to-zero objective |
| No retained intermediate stems | D5-07; separation and any render derivative stay lease-managed; a retained capture record is not a retained stem file |
| Native REAPER only | All operations go through the bridge; no memory injection; the two missing primitives (`item.edge`, full readback) are native API calls |

---

## 10. What is still unverified

- Every VRAM/throughput figure for every optional model. Nothing was run on the GPU.
- Whether the reconstruction gate passes on the real project and what raw partial-
  loudness share distribution it yields (E3).
- Whether the onset detector's F1 is adequate on amp-print guitars (E1).
- The fade *shape* REAPER applies from `D_FADEINLEN`/`D_FADEOUTLEN`; this is why
  preview-vs-render equality is an acceptance test rather than an assumption.
- Group sync, stretch-marker round-trip and MIDI-on-looped-item semantics through the
  API (E6–E8).
- Whether the combined panorama addition fits the budget on a 20 s capture (R6).
- Exact licence names for MedleyDB and the ToneTwist audio (repo MIT; upstream audio
  provenance unresolved).
- Whether the reports' literature claims marked `[ABSTRACT-ONLY]` survive full-text
  reading — the Hafezi & Reiss and De Man PDFs are paywalled/blocked, and the Parker
  method section could not be extracted.

None of these blocks the decisions in §3. All of them block shipping the corresponding
capability, which is why each appears next to an acceptance test rather than a promise.
