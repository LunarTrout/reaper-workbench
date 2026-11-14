# Workstream A — auditory evidence and perception: what to compute, when, and what to refuse

**Model:** `deepseek/deepseek-v4.1-flash-1017` via RouterAI (`custom:routerai.ru`), reasoning level **Ultra**.
**Date:** 2026-11-13. **Round:** 7. **Scope:** masking/clarity, auditory texture, source separation, instrument-wise FX representation, reference analysis, evaluation corpora, and the perception fields of the panorama.
**Not in scope (Workstream B):** edit transactions, onset/note/event models, tempo/stretch, action API contracts, FX parameter optimisation semantics. I state only the evidence preconditions those layers must honour.

**Snapshot note.** “Current tree” in this report means the pre-implementation dirty tree read on 2026-11-13 before the separate relation/capture follow-up. The later implementation appendix in `INTEGRATED-DECISIONS.md` §9.5 is not part of this audit and does not retroactively change these observations; where it conflicts, the audit records the historical state and the synthesis records the later state explicitly.

## 1. Method, evidence labels, and what I could not do

I read the Round-5 dispatch, Agent A report and synthesis, the Round-6 synthesis and continuity report, `TZ.md`, `FULL-CAPTURE.md`, `AI-MIX-SURFACE.md`, `MCP-README-LLM.md`, `EDITING-ACCEPTANCE.md`, the four performance documents, and the current source and tests under `analysis/`, `context/`, `daw/`, `corpus/` and `mix/`. Every claim about the tree below was grepped in this working copy; line numbers are from the dirty tree as read today and may move. I did not run pytest, REAPER, a render, a benchmark, an install, a model download or any GPU work; the analysis is document-only, and the measured figures below are quoted from existing artifacts, never re-measured.

Labels used throughout:

- **[MEASURED path]** — a number present in a named artifact or sealed record in this repository.
- **[ARITHMETIC]** — my arithmetic over measured inputs, with the inputs named.
- **[FULL-TEXT] / [ABSTRACT] / [REPO] / [CARD]** — what I actually read of an external source today: body text, abstract/metadata only, official repository or licence file, dataset card.
- **[INFERENCE]** — my reasoning from the two above, not observed.
- **UNMEASURED** — no number exists; I refuse to invent one.

Access limits that matter for the decisions: the Mix Evaluation Dataset 2.0 mirror still has a broken viewer and no visible licence (verified today); I did not open the ODAQ Zenodo archive; Fx-Encoder++ has a paper but no code or weight link on its arXiv page; the Audiobox card confirms CC-BY-4.0 but not its input rate or window. No audio dataset was downloaded, so dataset claims below are about access and licence, not content statistics.

The attachment is the Round-5 source list; my job is to say which claims the tree confirms, which it contradicts, and what should be computed in full capture, on demand, or not at all.

## 2. Reconciliation: Round-5 and Round-6 claims against the current tree

### 2.1 Round-5 Workstream A claims

| # | Round-5 claim | Current-tree evidence | Verdict | Reason |
|---|---|---|---|---|
| 1 | `temporal.masking()` is a band-dominance heuristic, not a threshold | `temporal.py:803` still `masking(tracks, min_db=10.0, top=8, floor_db=-80.0)`; per-cell broadband centring; both-active gate; max median band difference; payload keys `masker`/`target`; `bundle.py:290-293` overrides the interpretation string with "Relative band-level dominance heuristic; not a psychoacoustic masking model." | **KEEP** | Unchanged and still the correct description. |
| 2 | Rename `masking` → `band_dominance`, carry `pan_relation`, `broadband_delta_db`, `active_fraction`, `cells`, `is_psychoacoustic_threshold:false` (D5-02) | Only the *consumer* was renamed: `mix_evidence.py:49` emits `id="band_dominance"`. The producer still emits `masker`/`target`, and there is no pan, no broadband delta, no active fraction, no method flag anywhere in the payload. `grep -rn "pan_relation\|is_psychoacoustic" src/` = 0 hits. | **REVISE (half done)** | The name the model reads in `reaper_mix_evidence` is right; the name in the analysis report is still wrong, and the context fields that make the number interpretable are absent. |
| 3 | Delete the unreachable `masking_db=6.0` path; one threshold in one place (D5-03) | `mix_evidence.py:34` still defaults `masking_db=6.0`; `:46` still filters `db < masking_db`; `:50` still computes `severity = min(3, db/masking_db)`. `temporal.masking()` cannot emit a pair below 10 dB, so the filter is still inert and the severity divisor is still a fixed constant. | **KEEP (still open)** | Round 5 was right; the defect survives. Only the 5 %/45 % partial-audibility rule was removed. |
| 4 | Partial loudness must not emit shares unless a reconstruction gate passes (D5-04) | Implemented. `partial.py:589-601` compares summed source band levels against the declared mix render; `median_abs_db > max_median_error_db` or `p95_abs_db > max_p95_abs_db` → `status="fail"` and `_unavailable_payload()`; `:702-736` emits `n_sources`, `equal_share_baseline_pct`, `share_denominator`, `gate`, and an explicit "not audibility" note. `tests/test_partial_loudness_gate.py` has 18 fail-closed cases (stage mismatch, node reuse, window mismatch, undeclared source, channel view, nonlinear master). | **KEEP (done)** | This is the strongest implemented piece of the perception layer. |
| 5 | Raw shares only after the gate; no built-in too-quiet/exposed flag; `100/n` descriptive only (D5-05) | `mix_evidence.py:84-113` builds a `partial_loudness` block with `shares_pct`, `n_sources`, `share_denominator`, `equal_share_baseline_pct`, `gate`; no severity; no issue row; `tests/test_mix_evidence.py` asserts `"partial_audibility" not in ids` and `"severity" not in block`. | **KEEP (done)** | Verified by test, not prose. |
| 6 | Texture statistics over 2–4 s: cross-band correlation, modulation rate/depth, cascaded depth (D5-06/D5-07) | **MISSING.** `grep -rn "texture\|cross_band_corr\|cascaded" src/` = 0 hits. What exists is `temporal._modulation()` (`temporal.py:313-333`): a 20 ms RMS envelope, FFT, dominant peak in 2–12 Hz, and `depth = peak_bin / band_sum`. That is a whole-file envelope descriptor, not a texture statistic, and `depth` is a spectral concentration share, not an AM depth. | **REVISE (redefine, then decide)** | The existing field overpromises. A real texture block does not exist. |
| 7 | Reference corridor must carry stage, section, loudness match and n, and refuse when n < 8 (S7) | `compare.reference_corridor()` (`compare.py:56-92`) returns median/MAD per metric and `reference_count`; no stage, no section type, no loudness match, no minimum n. Reachable through `reaper_analyze(reference_paths=…)`. Separately, a `corpus/` package exists with a manifest that carries `stage`, `source`, `genre`, `year` (`corpus/manifest.py`), a level-invariant 42-feature measure, and a Ledoit-Wolf Mahalanobis model. | **REVISE** | The weak mechanism is still model-facing; the strong mechanism is CLI-only. They must be reconciled. |
| 8 | No learned quality head, no universal score, embeddings are retrieval aids (D5-06) | No quality head in `src/`. But `daw/cycle.py` implements `run_eq_cycle`: it proposes EQ moves from corpus band deviations, renders master-only, and **accepts a move when the measured corpus distance improves**. Default plugin `VST: ReaEQ (Cockos)`, `eq_count=2`. Script-only (`scripts/eq_cycle.py`), one test touches `new_violations`. | **REVISE / DROP the objective** | This is a distance-to-corpus minimiser — the exact "blandest possible mix" failure Round 5 warned about. It is not MCP-exposed, but it is product code. |
| 9 | Source separation is analysis-only, read-only, lease-managed, never retained (D5-07) | No separator exists in the repo (`grep -rin "demucs\|roformer" src/` = 0). `corpus/roles.py` consumes a directory of pre-separated stems (`<root>/<model>/<source>/<role>.wav`) and checks reconstruction. `work/role_assess.json` records corr 0.99967, residual −29.86 dB for one such set. | **KEEP** | The contract holds by construction because nothing writes separated audio into the project. |
| 10 | Audiobox PQ is a weak validator only | Model card licence `cc-by-4.0` confirmed today; the 16 kHz/10 s/bf16 details still come from project docs, not from the card. | **KEEP** | Clean licence; input limits unverified. |
| 11 | MED 2.0 HF mirror is unusable for measurement (MP3, no licence, broken viewer) | Verified today: viewer still raises `FeaturesError: FileNotFoundError`; no licence tag or file visible. | **KEEP** | Still not a corpus. |
| 12 | Parker's best variant needs no masking model (IBR-MR ρ ≈ 0.80); masking-threshold variants are weaker | No repo claim depends on it. | **KEEP as prior** | Do not implement MPEG Model II. |
| 13 | Hafezi & Reiss: objective masking measures are ill-suited for perceived masking | Publisher abstract re-read today: the sentence is verbatim. | **KEEP (load-bearing)** | The strongest reason not to build a masking threshold. |
| 14 | Wakefield & Dewey: panning both sources apart wins; cutting the masker alone is least preferred | Not re-read today; the PDF link is live. | **KEEP** | Still the only blind comparison; still n = 11, 2-source scenarios. |
| 15 | Colonel & Reiss: mixdown features show virtually no correlation with preference | Not re-read today. | **KEEP (load-bearing)** | The strongest argument against a quality score. |
| 16 | Fx-Encoder++ can at best compare processing embeddings and cannot recover a chain | arXiv page (3 Jul 2025) confirms the instrument-query extractor; no code/weights link found today. | **KEEP, strengthen to NOT JUSTIFIED** | Reproducibility and licence unresolved. |

### 2.2 Round-6 continuity claims that touch audio evidence

| # | Round-6 claim | Current-tree evidence | Verdict |
|---|---|---|---|
| R6-1 | Invalidation follows the render graph and `render_key`, never audio hashes (D6-11) | `grep -rn "render_key" src/reaper_audio/daw/full_capture.py` = 0 hits; B independently reports 0 hits and that `lifecycle._invalidations()` only marks coarse scopes inside `changes_since` between two captures. | **KEEP the decision, mark UNIMPLEMENTED** |
| R6-2 | A re-render of identical content produced 43/43 different audio SHA-256 values | Round-6 measurement stands; no re-measurement here. | **KEEP** — audio hashes cannot be a freshness test. |
| R6-3 | The full document must be made actually full before it is called full: units, method, version, stage, channel, window per measurement; validity on the read path | `panorama.py:39-45` still limits compact analysis to 9 metric keys; `_metrics_view(..., compact=True)` at `:1063` keeps a bare value per metric; `analysis_metric_units` exists only at top level. `reaper_capture_read` **does** now return `capture_status`, `invalid_reasons`, `master_validation`, `coverage`, `missing` (`mcp_server.py:1833-1860`), but `panorama_open`'s envelope (`lifecycle.py:486-524`) and the document itself carry no validity block. | **PARTIAL** |
| R6-4 | `state.scope = project`, only `evidence.scope` narrows; slice returns structure + window evidence | `panorama_slice` exists; implemented, not exercised here. | **KEEP** |
| R6-5 | Both temporal levels (30 s and 300 s) retained; reduced grids rejected | `FULL-CAPTURE.md` and the columnar `analysis_temporal` table describe both levels; no lossy projection in the tree. | **KEEP** |
| R6-6 | 29 tools / 6 resources (Round 6 said 21 before the lifecycle tools) | `grep -c "@server.tool()"` = **29**; six resources. Docs saying 21 are stale. | **REVISE the docs** |

**What this reconciliation changes.** Three Round-5 items were implemented better than proposed (the partial-loudness gate, the descriptive-shares rule, and a corpus model Round 5 did not know existed). Three were not implemented at all (band-dominance context, texture, reference gating). One landed in the wrong place (a distance-minimising EQ cycle). And the keystone dependency — a plan-bound `render_candidate` — still does not exist (B: 0 hits), so no perception claim can yet be verified against a candidate edit.

## 3. Current implementation audit

### 3.1 Two audio paths, and only one of them is the capture

- **Full capture** (`daw/full_capture.py`) renders one selected-track asset per project track through the master path plus a separate master render, then runs `analyze_asset_job` per asset in a bounded pool. It produces the sealed record and the panorama. It computes **no cross-source relations, no masking, no partial loudness, no reference comparison**. The panorama's `analysis[]` rows are per-asset.
- **The bundle path** (`ProjectSession.analyze` → `analysis.bundle.analyze_bundle`) renders the project and references, measures every source, and *then* computes `masking_evidence` (`bundle.py:290`), `reference_corridors` (`:239`), `partial_loudness` (`:301`, only when `include_partial_loudness=True`, default `False` at `session.py:1263`) and pairwise phase. It is reachable only through the `reaper_analyze` tool.

So the model can get relational audio evidence, but only by issuing a second, non-captured analysis call. The capture — the immutable, revision-bound object the whole product is built on — contains no relation at all. Round 5's G6/G7 findings are therefore still true, and they are an architectural split, not a missing field.

### 3.2 Band dominance: exact behaviour and exact defects

`temporal.masking(tracks, min_db=10.0, top=8, floor_db=-80.0)` at `temporal.py:803`:

1. Builds a `(cells, 28)` band matrix per source from the 100 ms cell grid (hop 0.1 s, `nfft=4096`, `FC` = 28 third-octave centres 25 Hz–12.5 kHz, `temporal.py:13-18`).
2. Centres each cell by its own broadband mean (`normalized = raw - raw.mean(axis=1)`), so the comparison is tonal balance, not fader level.
3. Masks cells where either source is at or below −80 dB in that band.
4. Takes the **median** difference per band over valid cells and reports the maximum median band difference if it is ≥ 10 dB; also the mirrored minimum for the other direction.
5. Returns up to `top=8` pairs, sorted by dB, with `masker`, `target`, `band_hz`, `db`, `cells`.

Defects, all verified:

- **It is not a masking model.** No threshold in quiet, no tonality, no temporal masking, no binaural term. The bundle says so in prose; the payload keys still say `masker`/`target`, an attribution the measurement cannot support.
- **No pan relation.** The comparison is on a mono power average; two hard-panned opposite-side sources look exactly like two centred sources. Given A-05, this is the most consequential omission.
- **No broadband delta or active fraction.** A 10 dB band difference from a source that is 20 dB louder overall is presented identically to one that is not.
- **No method flag.** Nothing prevents a client from reading `db: 11.4` as "11.4 dB of masking".
- **The dead filter and fixed severity** (`mix_evidence.py:34/46/50`) survive.
- **Cymbal/guitar, kick/bass and fuzz cases** are structurally outside the measurement: the first two are separated by attack envelope and stereo position, the third by fusion after distortion.

### 3.3 Partial loudness: implemented, gated, and correctly narrow

`analysis/partial.py` computes a per-source Zwicker/ISO 532-1 or Moore-Glasberg partial loudness on the residual after subtracting the source's band power from the declared mix, and refuses to publish when the arithmetic does not reconstruct the mix. The gate (`:589-601`) is a *band-error* test, not a power test: summed source levels vs the measured mix level per band, `median |Δ| ≤ max_median_error_db` and `p95 |Δ| ≤ max_p95_abs_db`, with declared tolerances and overrides. It also refuses on alignment, stage-label, node, window, provenance, channel or sample-timeline mismatch, and on a nonlinear-master style mismatch (18 tests).

Decision consequence: this is the only perception-adjacent measurement in the tree that is falsifiable at the point of use. It should not be extended or "improved" before E3 (below) establishes that it passes on the owner's real master at all.

### 3.4 Loudness, dynamics, phase: the honest layer

`metrics.py:450-482` emits `peak_dbfs`, `true_peak_estimate_dbfs`, `rms_dbfs`, `crest_db`, `integrated_lufs`, `loudness_range_lu`, `clipped_sample_ratio`, `spectral_centroid_hz`, `transient_density_per_s`, `transient_count`, `stereo_correlation`, `side_mid_db`, `mono_fold_loss_db`, `modulation_rate_hz`, `modulation_depth`, `pitch_jitter_pct`, `coarse_speech_band_ratio`, `dc_offset`, `clipped_sample_count`, event counts and local tempo. These are signal facts or explicit signal definitions; they need no perceptual claim.

Two of them are defective and must be excluded from any comparator set: `f0_hz` and `pitch_jitter_pct`. The sealed-record parity study found `f0_hz` cells up to 166 kHz at 44.1 kHz and `pitch_jitter_pct` up to 430 % (`docs/PERFORMANCE-PIPELINE-RESEARCH.md` §2.1, §7 item 9; `PERFORMANCE-PIPELINE-REPORT-B-THROUGHPUT.md` §d.4). They are not evidence of anything and must not appear in a corridor or a claim.

### 3.5 Reference analysis: two mechanisms, one of them unsafe

**(a) `compare.reference_corridor`** (`compare.py:56-92`) — for each metric present in both the target and every reference, returns `mine`, `median`, `mad`, `delta`, `unit`, `references` (count), plus `reference_count` and `skipped`. It is reachable through `reaper_analyze(reference_paths=…)` and consumed by `mix_evidence` as `reference_delta` issues with `severity = min(3, |delta|/max(mad, 0.5 dB)/2)`. It has **no stage check, no section check, no loudness match and no minimum n**; a 2-MAD excursion against three references is reported with the same vocabulary as one against thirty.

**(b) `corpus/`** — a complete, level-invariant reference model:

- `manifest.py`: `Entry(file, source, stage, genre, year, notes)` with `stage ∈ {mix, master, unknown}`; nothing is guessed; unknown stays null.
- `measure.py:140-147`: 42 features — 29 third-octave levels as centred log-ratios (CLR), spectral tilt, side/mid, correlation, mono-fold loss, per-coarse-band side/mid and crest, onset density, and short-term crest p50/p95. Level-invariant by construction; absolute LUFS is deliberately not a feature.
- `model.py`: median centre, normal-consistent MAD scale, Ledoit-Wolf shrunk covariance estimated from the data, Mahalanobis distance, `n_samples`, `shrinkage`, `condition`; requires ≥ 2 samples.
- `report.py:44-58`: distance, **empirical percentile from leave-one-out distances**, per-feature contributions, defect gates, extras. `distance_timeline` gives a per-window distance so the model can see *where* a track leaves the corpus.
- `roles.py`: per-role matrices over pre-separated stems plus a reconstruction check (`correlation`, `residual_db`, `null_depth_db`).
- `degrade.py` / `benchmark.py`: ten controlled degradations and a leave-one-track-out protocol that asks whether a production degradation moves a copy *away* from the corpus while a pure gain change does not.

Measured behaviour (this is the decisive evidence for the reference decision):

| Degradation | Expectation | 38 excerpts / 76 windows | 6 excerpts / 36 windows | Reading |
|---|---|---|---|---|
| `gain` | invariant | 0.539 closer, ratio **1.000** | 0.472, **1.000** | Level invariance holds to 1e-15. |
| `bright` | better | **1.000**, 2.047× | 1.000, 2.351× | Detected. |
| `dark` | better | **1.000**, 1.725× | 0.972, 2.255× | Detected. |
| `air_cut` | better | **1.000**, 2.644× | 1.000, 2.586× | Detected. |
| `narrow` | better | **1.000**, 1.770× | 1.000, 1.999× | Detected. |
| `mono` | better | **1.000**, 38.752× | 1.000, 52.117× | Detected. |
| `mud` | better | 0.882, 1.075× | 0.694, 1.060× | Weak. |
| `presence_cut` | better | 0.789, 1.034× | 0.750, 1.090× | Weak. |
| `compressed` | better | **0.618**, 1.009× | 0.583, 1.013× | **Not detected.** |
| `clipped` | better | **0.355**, 0.991× | 0.417, 0.983× | **Worse than chance.** |

Sources: `work/corpus_benchmark_38.json`, `work/corpus_benchmark.json`; replicated on a 16-excerpt short-window run (`work/bench_short.json`: `compressed` 0.656, `mud` 0.656, `presence_cut` 0.438) and a dedicated 38-excerpt compression run (`work/bench_compressed.json`: 0.645, ratio 1.014). Model scale in those runs: `n_samples` 28–72, 42 features, `shrinkage` 0.16–0.17, `condition` 68–98.

That table is the most useful measured artifact in this round. It says the corpus model is a **tonal-balance and width descriptor**: it is invariant to level and sensitive to tilt, air, width and mono-fold, weakly sensitive to low-mid and presence, and blind to compression and clipping. It must therefore never be used to justify a dynamics decision, and `clipped` must be caught by the defect gates (`corpus/gates.py`), which is exactly what the package already separates.

**The unsafe path.** `scripts/reference_delta.py` takes the loudest 30 s window of each file and prints 12 numbers plus delta against the mean of the references, with a hard-coded direction hint per metric in Russian — including "много 120-300 = мутность и «коробка», режь гитары/бас в этом месте" ("lots of 120–300 = mud and boxiness, cut guitars/bass there"). Its own recorded output (`work/reference_delta.json`) compares ours at −12.81 LUFS against two commercial masters at −6.13 and −7.33 LUFS, over windows 195–225 s versus 165–195 s and 195–225 s of different songs, n = 2. Every comparability condition Round 5 named is violated, and the script then states a recipe. It is a script, not a library method, but it is in the repository and it is the kind of artifact that gets copied into a tool later.

### 3.6 Texture: MISSING, and the existing field is misnamed

`modulation_rate_hz` and `modulation_depth` come from `_modulation()` (`temporal.py:313-333`): 20 ms RMS envelope at a 5 ms hop, FFT, dominant peak in 2–12 Hz, `depth = peak_bin / sum(band)`. That is (a) a whole-file scalar, (b) a spectral-concentration ratio, not a modulation depth, and (c) computed on the mono RMS, so it cannot see cross-channel structure. The texture literature's minimal recipe — per-channel band envelopes, **cross-channel correlation**, modulation rate *and depth*, and a second-order (cascaded) term over multi-second windows — is entirely absent. There is no `texture` key in any payload, no window parameter, and no coverage field.

### 3.7 Panorama perception fields: nine numbers and a structural graph

- `ANALYSIS_METRIC_KEYS` = `integrated_lufs`, `peak_dbfs`, `rms_dbfs`, `crest_db`, `true_peak_estimate_dbfs`, `spectral_centroid_hz`, `stereo_correlation`, `transient_density_per_s`, `loudness_range_lu` (`panorama.py:39-45`). Compact analysis rows keep only these, as bare values (`:1063`).
- `crosslinks` are structural only: capture→project→track→item→take→midi, owner→fx, route→destination, owner→automation/envelope, source→audio→analysis→capture, and project→time_map/marker/region (`:1853-1958`), with names resolved through `crosslink_relation_legend`.
- There is **no** `audio_relations`, `texture`, `reference_links`, `claims`, `uncertainty`, `states.invalid_captures` or `stale_assets` block. `epistemic_layers.model_interpretations` and `causal_hypotheses` are structurally empty (`FULL-CAPTURE.md:106-108`).
- Item rows still carry no `start_offset_s`, `playrate`, stretch markers, fades, `group_id`, `channel_mode` or `pan_mode` (`grep` in `panorama.py` = 0 hits for all of them), so the "whole-project view" cannot describe a pan or channel-mode change — which is precisely the change that invalidates a band-dominance or width claim.

### 3.8 MISSING list, with the place it belongs

| Capability | Status | Where it belongs |
|---|---|---|
| Band-dominance context (`pan_relation`, `broadband_delta_db`, `active_fraction`, `is_psychoacoustic_threshold`, method/version) | MISSING | `analysis/temporal.py` producer + `analysis/mix_evidence.py` consumer |
| Rename producer keys `masker`/`target` → `from`/`to` with `kind="band_dominance"` | MISSING | `analysis/temporal.py:803-860` |
| Texture block (2–4 s windows, cross-band correlation, AM depth, cascaded depth, coverage) | MISSING | new `analysis/texture.py`; consumed by a new on-demand view |
| Reference stage/section/n gate on the per-metric corridor | MISSING | `analysis/compare.py:56-92` |
| One model-facing reference mechanism | MISSING (two coexist) | `analysis/compare.py` vs `corpus/report.py`; choose one |
| Validity block in the panorama document and in `panorama_open`'s envelope | MISSING | `context/panorama.py` builder + `context/lifecycle.py:486-524` |
| Per-measurement method/version/stage/channel/window in `analysis[]` | MISSING | `context/panorama.py:1063` (`_metrics_view`) |
| Item offset/playrate/channel_mode/pan_mode in item rows | MISSING | `context/panorama.py` item builder |
| `render_key` invalidation | MISSING | `daw/full_capture.py` (B confirms 0 hits) |
| Invalidation records at apply time | MISSING | edit path (B's territory) |
| `render_candidate(plan, window)` | MISSING | B's territory; blocks every verification loop here |
| Separation | MISSING entirely (no code) | external, analysis-only, lease-managed |
| Instrument-wise FX embedding | MISSING | not to be built (see §5.4) |

## 4. Measured performance envelope: what the artifacts prove, what is stale, what is unmeasured

### 4.1 The sealed 300 s capture (baseline, superseded)

`cap_01M7A7ZV8VWK4SA8FHGRC9Q04S` [MEASURED, `docs/PERFORMANCE-PIPELINE-RESEARCH.md` §1.1; `PERFORMANCE-PIPELINE-REPORT-A` §b.3; `REPORT-B` §b.1]:

| Block | Seconds | Share |
|---|---:|---:|
| Snapshot + digest | 0.146 | 0.02 % |
| Track batch render (42 targets) | 98.470 | 14.0 % |
| Source-activity decode (42 stems, parent) | 17.767 | 2.5 % |
| Master render | 9.505 | 1.4 % |
| Master gate (SHA + decode) | ~6.2 | 0.9 % |
| Analysis pool (8 workers, 43 tasks) | 408.274 | 58.1 % |
| Per-asset record loop | 9.903 | 1.4 % |
| Untimed packaging tail (seal + archive + panorama + response + mute restore) | 149.894 | 21.3 % |
| **Total** | **703.072** | 100 % |

Supporting numbers: `dsp_worker_elapsed_sum_s` 3011.645, decode 16.552, 43 assets × 13,252,932 frames at 44.1 kHz stereo 24-bit = 3,419,286,126 B, record 445,001,421 B, panorama sidecar 395,400 B, pool efficiency 92.2 %.

**This baseline is stale.** The dirty tree implements master-first rendering (`daw/full_capture.py:547`), topology-aware bounded batches (`capture_batching.py`), render/analysis overlap (`analysis_pipeline.BoundedAnalysisPool`), batched mute RPCs, and the `short_term_lufs` fix. B confirms the 703 s baseline is superseded and no new full-song timing exists.

### 4.2 The new pipeline: one live measurement, short window

`cap_01M7AHYX2EHGFEFWSS01M2J3B9`, section 100–112 s, 42 tracks [MEASURED, `work/benchmark/himmp/perf/live-overlap-smoke-20261113.json`]:

| Field | Value |
|---|---:|
| Entry-point wall | 29.812 s |
| Internal total | 27.333 s |
| Master render | 1.043 s |
| Track batch render (4 batches 11/11/11/9, 5 calls) | 16.883 s |
| Analysis wall | 21.527 s |
| Analysis/render overlap | 17.370 s |
| Results completed before last render end | 29 of 43 |
| Workers | 8 |
| DSP worker sum | 114.058 s |
| Panorama bytes | 297,037 |
| Record bytes | 18,494,200 |

This is correctness-and-overlap evidence for a 12 s window, not a full-song speedup. The overlap works: 29 of 43 results finished before the last render returned. But the short-window panorama is **297 KB** — 42 % of the 700,000-byte budget, and noticeably larger than the 198,480 B / 20 s figure in `FULL-CAPTURE.md:130`. Any new panorama block must therefore be priced against the short-window document.

### 4.3 Worker scaling and the memory wall

43 × 20 s slices, production `analyze_asset_job` [MEASURED, `work/benchmark/himmp/perf/PERF-WORKLOG.md`]:

| Workers | Wall (s) | Speed-up | Efficiency | Job CPU sum (s) | Peak tree RSS |
|---:|---:|---:|---:|---:|---:|
| 1 | 72.746 | 1.000× | 100 % | 70.609 | 0.449 GB |
| 2 | 42.117 | 1.727× | 86.4 % | 80.234 | 0.734 GB |
| 4 | 28.140 | 2.585× | 64.6 % | 106.031 | 1.381 GB |
| 8 | 21.659 | 3.359× | 42.0 % | 148.063 | 2.635 GB |
| 12 | 19.709 | 3.691× | 30.8 % | 161.766 | 3.438 GB |
| 16 | 19.145 | 3.800× | 23.8 % | 165.734 | 4.029 GB |

Knee at 8; 12 buys 9 %, 16 buys 3 %, while CPU sum rises 2.35× from 1 worker to 16. The workload is bandwidth-bound. For a 300 s asset the measured per-worker peak is **3.96–4.29 GB**, so 8 workers is already ~32–34 GB on a 47 GB machine. The production policy (`analysis_policy.py:30-42`) is hard cap 8, 8 workers for ≤ 30 s, **4 for full/unknown**, envelope 384 MB + 14 MB/s, 70 % of available memory, and a structured refusal before any process starts. The performance synthesis still says `MAX_ANALYSIS_WORKERS = 16` and a memory cap of 10; **the tree wins and the doc is stale** (B agrees).

### 4.4 The cost model I use for proposals

Two measured rates, and they disagree: 300 s assets cost 3011.645 CPU-s / (43 × 300.52 s) = **0.233 CPU-s per source-second**, while 20 s slices cost 70.609 / (43 × 20) = **0.082** [ARITHMETIC]. The 300 s class is 2.84× more expensive per second, and the cause is not established (fixed per-file costs, density, or a superlinear stage such as `pyloudnorm` or the 3,005-cell loop). The rule: **price a new stage on the duration class it ships in**, never by scaling the 20 s rate.

What is unmeasured and must not be guessed: full-song wall time of the new pipeline; the cost of `include_pairwise`/`include_partial_loudness` on a 300 s bundle; the cost of a 2 s/4 s texture pass; any separation runtime or VRAM on the V100; any embedding-model throughput. The performance documents also leave the master's ~1.19e-7 FS PCM difference, the container-hash cause, and the 150 s tail split unresolved.

## 5. Candidate matrix

For each candidate: capability, primary evidence, exact mapping or MISSING, cost, failure modes, decision, falsifiable acceptance test. Decisions use only the three allowed outcomes. **No candidate is decided FULL CAPTURE**: the capture already carries the per-source measurements, and an unvalidated construct should not become immutable.

### 5.1 Masking / clarity

**Capability.** Say where two sources compete in a band over a window, with the caveats that make the number honest, and let the model treat it as a locator.

**Primary evidence.** Hafezi & Reiss, *Autonomous Multitrack Equalization Based on Masking Reduction*, JAES 63(5):312–323, May 2015, DOI 10.17743/jaes.2015.0021 [ABSTRACT, re-read today via the AES issue page]: the autonomous systems reduce perceived and objective masking and the offline semi-autonomous version reaches "close to a professional mix", **and** "existing objective measures of masking are ill-suited for quantifying perceived masking in multitrack musical audio". Wakefield & Dewey, AES 138th Convention, May 2015 [FULL-TEXT read in Round 5, PDF link live today]: 11 subjects, seven two-instrument scenarios, panning both sources apart significantly preferred, cutting only the masker significantly least preferred, spectrum sharing not significant, preferred variant changes per scenario. Parker, PhD thesis, University of Huddersfield, July 2022 [FULL-TEXT, Round 5]: the strongest clarity variant (IBR-MR, ρ ≈ 0.80) uses no masking model; the masking-threshold variants are weaker and negatively signed.

**Current mapping.** Producer `temporal.masking()`; consumer `mix_evidence.build_mix_evidence()`; tool `reaper_mix_evidence(analysis_id)`. Not in the panorama; not in the capture record.

**Cost.** O(S²·B·cells) in numpy over data already in memory in the bundle: at 42 sources, 861 pairs × 28 bands × ~3,005 cells [ARITHMETIC]. Not separately timed. Today the only path is a bundle analysis of the requested window, i.e. a section render through `reaper_analyze`; a cheaper derived view over a capture's stored cell arrays is possible but not implemented. A top-K block would be cheap in CPU but expensive in *output*: the relation count grows quadratically (861 pairs at 42 sources, 3,403 at 83).

**Failure modes and uncertainty.** Mono downmix ignores binaural unmasking; no threshold in quiet without a playback calibration; no temporal masking; no tonality; a double-tracked decorrelated pair can produce a band difference that is a comb-filter artefact; distortion-driven fusion is invisible; and the mapping from dB to perceived unmasking is technique- and material-dependent (A-05). Whether 10 dB means anything on this material is open (E2).

**Decision: ON DEMAND.** Keep it, rename it, give it context, and compute it only when the model asks for relations (`reaper_mix_evidence` or a dedicated relations view), not on every capture. The evidence forbids it as a threshold, so it cannot gate anything; it is quadratic in sources; and the whole-project view does not need 861 edges — the model asks "what competes here" after seeing levels and pan. A top-K panorama block is *admissible* later (≈ 40–70 KB at 8 per source) but must be priced by a measured byte count on a short capture first.

**Acceptance test.** With two tracks panned −1.0 and +1.0, the payload reports `pan_relation="opposite_side"` and a caveat `mono_downmix_ignores_binaural_unmasking`; with −1.0/−0.85, `same_side`; a pair at 9.5 dB is emitted by **neither** layer (no 6.0 filter anywhere); `is_psychoacoustic_threshold is False`; `severity is None` unless a policy supplies a divisor; and no serialised claim contains `better|worse|good|bad|score`.

### 5.2 Auditory texture

**Capability.** Describe a dense, noise-like layer by time-averaged statistics over multi-second windows: per-band level, cross-band correlation, modulation rate and depth, and a cascaded (second-order) term.

**Primary evidence.** McDermott & Simoncelli, *Sound Texture Perception via Statistics of the Auditory Periphery*, Neuron 71(5), 2011 [ABSTRACT]: channel statistics alone "generally failed to produce compelling synthetic textures"; adding **correlations between channels** produced identifiable textures; implausible filter spacing degraded the result. McWalter & Dau, *Cascaded Amplitude Modulations in Sound Texture Perception*, Front. Neurosci. 11:485, 11 Sep 2017 [FULL-TEXT introduction]: AM and modulations-of-modulations are part of the represented statistics. McWalter & McDermott, *Illusory sound texture reveals multi-second statistical completion*, Nat. Commun. 10:5096, 2 Nov 2019 [ABSTRACT]: completion lasts several seconds and biases subsequent texture estimates.

**Current mapping.** MISSING. `modulation_rate_hz`/`modulation_depth` are whole-file scalars of a misnamed quantity (§3.6).

**Cost.** A 2 s non-overlapping texture pass on a 300 s asset is ~150 windows × ~38 frames of 4096-point FFT — about twice the FFT count of the existing 3,005-cell temporal pass, whose `analyze_cell` cost is 10.289 s in a 52.181 s single-file profile [MEASURED, `docs/PERFORMANCE-OPTIMIZATION-TZ.md`]. So ~10–20 s per 300 s asset [ARITHMETIC], i.e. **+430–860 CPU-s across 43 assets, +14–29 % on the 3,011.6 CPU-s DSP sum**, which at 8 workers is +54–108 s of pool wall. That is the whole argument for not doing it in every capture. On demand for one asset from the archived blob it is 10–20 s single-threaded [ARITHMETIC], with no render.

**Failure modes and uncertainty.** These are perception/synthesis studies, not mix-decision studies; no evidence exists that a texture statistic improves a mixing decision. The window length is a listening question. Cross-band correlation over 2 s from a 100 ms grid would be statistically thin (20 samples), so it needs its own finer pass. And a texture number reads easily as a verdict ("low correlation = mushy"), so the payload must forbid that in its own fields.

**Decision: ON DEMAND.** Compute from the archived post-FX asset or master (durable content-addressed blob, `context/full_capture.py:502,671`, "no expiry"), over a declared 2 s or 4 s window, with `coverage`, `window_s`, method/version, and `descriptive_only: true`. Do **not** add it to full capture in this round. Separately, rename the existing `modulation_depth` to `envelope_peak_share` (or document it exactly), because the current name asserts a quantity it does not compute.

**Acceptance test.** White noise and a 1 kHz tone differ by ≥ 0.3 in mean off-diagonal cross-band correlation; the same material at 2 s and 4 s reports both `window_s` values and never averages across a section boundary; a pure gain change leaves every texture statistic unchanged within a stated tolerance (level invariance); and the payload contains no threshold, severity or verdict key.

### 5.3 Source separation

**Capability.** Split a stereo mix into a small set of stems for analysis only — section energy balance, bleed checks, role-level reference comparison.

**Primary evidence.** Rouard, Massa & Défossez, *Hybrid Transformers for Music Source Separation* (Demucs v4), arXiv 2211.08553, 15 Nov 2022 [REPO, Round 5]: MIT, **SDR 9.00 dB** on MUSDB HQ, 4 stems (drums/bass/vocals/other), unmaintained. Lu, Wang, Kong & Hung, *Music Source Separation with Band-Split RoPE Transformer*, ICASSP 2024, arXiv 2309.02612 [ABSTRACT]: 9.80 dB average SDR for the MUSDB18HQ-only model; **checkpoint licence not established**. Neither is evaluated on dense distorted metal; both are 4-stem, so "other" contains all guitars, cymbals, keys and orchestration.

**Current mapping.** No separator. `corpus/roles.py` reads pre-separated stems and checks reconstruction; `work/role_assess.json` records correlation 0.99967, residual −29.86 dB, and per-role distances (bass 3.18, drums 12.97, other 8.30).

**Cost.** UNMEASURED. No VRAM, throughput or wall time was measured on the V100 for either model. I will not quote a number. The only cost fact is that a full 42-track separation is not a capture stage under any reading: it is 42 model invocations plus ~3.4 GB of derived audio that the retention contract forbids keeping.

**Failure modes and uncertainty.** Role bleed, false attribution, 4-stem taxonomy, no metal evaluation, unresolved checkpoint licence, and — measured on the owner's own material — a −29.9 dB reconstruction residual, i.e. ~3 % amplitude error, which is material for band ratios near −30 dB. A separated reference is only comparable to project stems if the **same separator and version** ran on both sides and the reconstruction gate passed.

**Decision: ON DEMAND, analysis-only.** Never an edit source, never retained, lease-managed; role comparisons only with matching separator/version and a passing reconstruction gate; "other" is one role and must never be relabelled "guitar". Do not add to full capture.

**Acceptance test.** Reconstruct the mix from the stems: residual ≤ −30 dB and correlation ≥ 0.999 (the measured values are the reference); a role-level comparison refuses when separator/version differ or reconstruction fails; no separated file survives the lease (a new `test_no_stems_retained` over the archive root); and a `role` label that is not in the separator's own output is rejected.

### 5.4 Instrument-wise FX representation

**Capability.** Say whether two sources' processing resembles each other, or whether a reference's processing resembles a project track's, in an embedding space.

**Primary evidence.** Yeh, Koo, Martínez-Ramírez, Liao, Yang & Mitsufuji, *Fx-Encoder++*, arXiv 2507.02273, 3 Jul 2025 [ABSTRACT re-read today; FULL-TEXT in Round 5]: contrastive mixture-level FX embeddings plus an "extractor" that needs an **instrument query** (audio or text) to produce instrument-wise embeddings. Training is synthetic: random chains over EQ, delay, distortion, stereo imager, compression, limiter, reverb, gain, on MUSDB/MedleyDB stems. Round 5 recorded the paper's own limits — reduced effect-parameter matching and difficulty with single-effect understanding. Today I found **no code or weight link** on the arXiv page.

**Current mapping.** MISSING.

**Cost.** UNMEASURED; and irrelevant until weights and a licence exist.

**Failure modes and uncertainty.** The extractor presupposes the instrument/stream you are trying to justify; training chains are series-only synthetic, not amp-sim + IR + pedal chains; the map from processing to embedding is many-to-one; and a "these two guitars are similar" claim cannot distinguish a fuzz pedal from a dirty amp.

**Decision: NOT JUSTIFIED.** It cannot recover a chain, cannot identify a parameter, and cannot localise which of four guitars has more gain. Representational similarity is not evidence a plan can act on.

**Falsifiable acceptance test (if ever revisited).** Four real chains × five riffs from the owner's project; ask the model which riffs share a chain; require top-1 ≥ 0.8 against a matched-random baseline of 0.25, with weights and licence on file. Below that, drop.

### 5.5 Reference analysis

**Capability.** Place a mix or master against a distribution of references and say how unusual it is, on which features, and where in time — without a score.

**Primary evidence.** Colonel & Reiss, AES 147th Convention, e-Brief 526, Oct 2019 [FULL-TEXT, Round 5]: 184 mixes / 19 songs, "virtually no correlation" between MIR features and mean preference or its spread; PCA axes did not match "width" or "loudness". De Man & Reiss, *The Mix Evaluation Dataset*, DAFx-17 [FULL-TEXT abstract+intro]: 180 mixes, ~5,000 ratings, parameter settings; experts harsher, more specific and more agreeing. De Man et al., AES 138th, 2015 [ABSTRACT]: some features do correlate with rated quality **within** tool-limited mixes of the same song — so any feature→quality claim must be within-song, within-section and stage-matched. ODAQ (CC licences) is a quality-metric calibration corpus, not a mixing corpus. MED 2.0's mirror has no licence and a broken viewer (today). Cambridge-MT's FAQ says research use is not pre-agreed; MedleyDB is non-commercial; HiMMP "in solitude" is the only licence-clean real dense multitrack recorded in the in-repo benchmark.

**Current mapping.** Two mechanisms (§3.5): the weak per-metric corridor (live, model-facing) and the strong corpus model (CLI only, measured).

**Cost.** `CorpusModel.fit` is negligible (42 features, tens of rows). `measure_signal` is one STFT + band integration per 10–15 s window; a 38-excerpt corpus with 76 windows measured end-to-end in the benchmark artifact, but the timing is not recorded → **UNMEASURED for wall time**. Per-assessment cost is one window measurement, i.e. well under a second [INFERENCE from the feature definitions, not measured].

**Failure modes and uncertainty.** The measured detection profile (§3.5) is the honest failure map: level-invariant; blind to compression and clipping; weak on mud and presence cut; sensitive to tilt/width/air/mono. Section typing does not exist in the project, so `section_type` will be `unknown` in most cases and any claim built on a mixed-section corpus is capped. A corpus of commercial masters implies a genre/material choice — allowed as material, forbidden as a recipe. And the median of a corpus is not a real track: a distance-to-median objective produces the blandest possible mix.

**Decision: ON DEMAND, with the corpus model promoted to the single model-facing mechanism.** Concretely: (a) `corpus` gets a tool wrapper with a manifest, stage filter, minimum window count and the measured per-degradation profile attached to the result; (b) `compare.reference_corridor` either gains the same stage/section/n gate and returns a *rank* against the leave-one-out distribution, or stops being model-facing; (c) `scripts/reference_delta.py` is retired from any model-facing path and its direction hints are deleted, because it is a recipe and its comparison is unmatched; (d) `daw/cycle.py`'s distance-minimising objective is removed or rewritten as gate-constrained rank improvement — it currently drives distance to zero.

**Acceptance test.** A manifest with mixed stages refuses unless a stage is selected; fewer than 12 windows returns `insufficient_reference_windows`; the payload carries `n_sources`, `n_windows`, `shrinkage`, `condition`, `percentile` and the degradation profile; a gain-only change moves distance by < 1e-6 (measured 1.000 ratio); a compression change is reported as **not detected** rather than as an improvement; and no serialised assessment contains a quality score or a recipe string.

### 5.6 Subjective and technical evaluation corpora

**Capability.** Calibrate what a measurement responds to, and keep preference labels separate from defect labels.

**Primary evidence.** ODAQ [REPO, today]: CC licences, MUSHRA with expert listeners across Fraunhofer IIS, Netflix and Ball State, 44.1/48 kHz stereo, music excerpts including solo recordings; quality levels span the full range; the 2026-10 collection adds spatial distortions. Slakh2100, Zenodo 10.5281/zenodo.4599665, 2019 [CARD]: CC BY 4.0, 2,100 **synthesised** multitracks with MIDI — excellent fixtures, no realism. MED 2.0 mirror [CARD, today]: no licence, MP3, broken viewer. ToneTwist AFx [REPO, today]: repository MIT, but dry inputs are IDMT-SMT-Guitar/Bass (research terms), Neural Amp Modeler, "Private Guitar Data" and YouTube bass recordings — **the audio is not redistributable even though the repo is MIT**. HiMMP "in solitude" [MEASURED, in-repo benchmark]: CC BY, 41 tracks, 300.52 s, 200→240→200 BPM.

**Current mapping.** No third-party audio in the repo (correct). The corpus package can read a user-supplied directory. The controlled degradation matrix already exists in `corpus/degrade.py` (ten degradations) with the leave-one-track-out benchmark.

**Cost.** Storage and licensing, not compute (~1 GB for 38 × 30 s at 44.1/24 [ARITHMETIC]); the repo must not carry it. **Failure modes.** Preference labels from other listeners do not transfer to this owner (A-11: five engineers, fourteen raters, cultural/generational specificity); lossy mirrors measure the codec; synthetic corpora cannot validate masking; and a "metal corpus" becomes a genre recipe the moment a target is derived from it rather than a distribution.

**Decision: NOT JUSTIFIED as shipped assets; ON DEMAND as user-supplied corpora.** Use ODAQ only to check that a defect metric responds monotonically across its five quality levels; Slakh only as deterministic fixtures; HiMMP as the one real, licence-clean dense multitrack. Never ship preference labels; never derive a per-genre target.

**Acceptance test.** A repository scan finds no third-party audio (`test_no_third_party_audio`); every manifest entry carries `licence` and `stage`, and an entry without a licence is refused by the assessment tool; a metric calibration run on ODAQ reports monotonic response or fails.

### 5.7 Round-5 candidates I now reject outright

| Candidate | Why rejected now |
|---|---|
| MPEG-1 Model II psychoacoustic masking as the primary clarity feature | Cost O(S²·F·B); assumes tonality that dense guitars violate; the two groups who built working masking systems report objective masking does not track perceived masking; and Parker's best variant needs no masking model at all. |
| A learned mix-quality head trained on MED 2.0 | Near-zero feature↔preference correlation; the only accessible MED 2.0 is unlicensed MP3; the product forbids a universal score. |
| Per-role corpus targets from separated stems, presented as "guitar" | 4-stem taxonomy; "other" is guitars+cymbals+keys; bleed; measured −29.9 dB reconstruction; encodes a genre prior. |
| Audiobox PQ (or any no-reference model) as a judge | 16 kHz mono discards the air and stereo the user cares about; no-reference cannot see intent; misuse as a validator would create a second judge beside the owner. |
| Fx-Encoder++ to recover an amp/IR chain | Weak parameter matching, single-effect difficulty, instrument query presupposes separation, no weights/licence found. |
| One mixed "clarity score" per song | Whole-mix scalar with no source attribution; would silently become a second judge; and the strongest published variant is not a masking model. |
| Distance-to-corpus as an optimisation objective (`daw/cycle.py`) | The median vector is not a real track; monotone descent produces the blandest mix; and the distance is blind to compression, so the search cannot see the damage it does to dynamics. |

## 6. Dense distorted material without genre rules

The user's material is dense distorted guitars, double-kick, cymbal wash and layered vocals. The temptation is to write a metal heuristic. The evidence says the opposite: every measurement below has a *structural* blind spot on this material, and the honest response is a narrower claim, not a genre-tuned threshold.

### 6.1 What masking can and cannot tell you here

**Can.** Over a window of several seconds, on the same stage: source A's band X sits D dB above source B's band X for F of the active cells, with both sources active, and they share a pan side (or do not). That is a checkable statement about the arrangement's spectral budget — the only current field that relates two sources at all. It is useful as a *locator*: it tells the model where to look and what to solo.

**Cannot.** It cannot say a source is inaudible. Concretely, on this material:

- **Hard-panned doubles (2 L, 2 R).** The measurement mono-averages, so opposite-side pairs look like strong competition; binaural unmasking is not modelled. The real competition is same-side, and there the two performances are decorrelated, so a band difference may be a comb-filter/phase effect rather than masking. `phase.py` is the right instrument and is available.
- **Fuzz and high-gain layers.** Distortion fills inter-harmonic gaps; the percept is fusion plus attack alignment, not band competition. A large low-mid difference between two guitars is usually the intended weight.
- **Cymbal wash vs guitar presence (3–8 kHz).** Always reports a large difference; the ear separates by attack envelope and stereo position (overheads/room), neither of which is in a band cell.
- **Double-kick vs bass (60–120 Hz plus a 2–5 kHz click).** Reports permanent low-band dominance; audible separation comes from the beater click and sidechain ducking, both temporal or processing facts.
- **Reverb and delay returns.** Absent from per-track solo renders, so band dominance and partial loudness are blind to the primary clarity mechanism. Rendering through the master path mitigates this but does not make a solo render what the listener hears.
- **Below ~50 Hz.** Large differences are reported where the region is near or below the threshold of hearing at normal monitoring levels; without a declared playback calibration the number is unfalsifiable.

### 6.2 What texture can and cannot tell you

Texture statistics describe a *layer*, not a relationship. On a fuzz wall they can distinguish "steady, broadband, high cross-band correlation" from "modulated, narrower, low correlation" — a real difference between a sustained wall and a tremolo-picked figure. They cannot say which is right for the song, and they cannot locate a masking event. Multi-second completion (A-16) is why the window must be seconds: a 4 s section with a guitar over a cymbal bed may be heard as the bed continuing, so a 100 ms cell can report masking that is not heard and miss masking that is. The window length is a listening question and must be declared, never averaged silently.

### 6.3 What separation can and cannot tell you

A 4-stem separator on black metal gives drums, bass, vocals and "other". "Other" is all guitars, cymbals, keys and any orchestration. Therefore:

- A per-guitar statement is impossible; a per-cymbal statement is impossible.
- A "vocals" stem merges screams and clean vocals.
- Bleed means a role's band levels are contaminated by its neighbours; the measured reconstruction residual (−29.9 dB on the owner's material) is good but not exact.
- The only defensible use is coarse: "in this section, drums carry X % of the energy, other Y %, bass Z %", with the separator and version recorded, and a reconstruction gate that refuses when the stems do not rebuild the mix.

### 6.4 Intentional masking, and why the library must not judge it

In this repertoire the wall *is* the arrangement. A guitar burying a cymbal, or two guitars fused into one perceived source, is frequently the goal. No measurement here can distinguish intended from unintended masking; only the owner can. The contract therefore has to be: report the relation with window, stage, pan relation and caveats; name at least two live alternatives (intentional doubling weight, arrangement change, mono-downmix artefact); name a discriminating test (solo the pair, compare attack alignment, check phase, listen level-matched); and never emit a defect flag, a constant-derived severity, or a "should". `mix/models.py` already encodes the two-alternatives rule and `not_a_verdict`; the band-dominance payload carries neither.

### 6.5 Stereo-only reference identifiability

From a stereo reference with no stems, the strongest defensible claim is: "band X of the reference sits D dB from band X of our master over window W, aligned by method M, against a corpus of n references with dispersion S" — plus "consistent with sources S1…Sk because …" as a ranked hypothesis. Attribution fails for three verified reasons: separation is 4-stem, leave-one-out needs per-source renders and a reconstructed mix, and Fx-Encoder++ needs the instrument query that presupposes the separation it would justify. A stereo reference can never support a per-instrument FX claim; the library must mark it `not_identifiable`.

### 6.6 How to avoid encoding a genre recipe

Six rules, each traceable to a defect found in this round:

1. **No constant in method code.** Every gate is relative or a named policy key that fails closed. The live `masking_db=6.0` divisor and `severity` formula violate this today.
2. **No fixed action vocabulary presented as the method.** `daw/cycle.py` fixes EQ and two bands; that is a tool, not a capability.
3. **No distance-to-a-point objective.** Use rank/percentile against a leave-one-out distribution, never the median vector.
4. **No genre-derived target.** A corpus may be metal-heavy as *material*; its distribution must not become a per-genre target (`TZ.md` §6).
5. **No recipe strings in any artifact.** The Russian hints in `scripts/reference_delta.py` are the anti-pattern; they must not reach a tool description, payload or docstring.
6. **No ordering.** The library reports what is; the model decides sequence.

## 7. Cross-dependency response to Workstream B, and open questions

### 7.1 Answers to B's six questions

1. **Does the masking/partial path consume capture `analysis` rows, or reconstruct from stems?** Neither reads capture rows. Partial loudness runs in the bundle path on rendered buffers and *requires* declared stage, node, window and channel per source, failing closed on mismatch (18 tests). Band dominance reads the in-memory `temporal_profile.cells`. The capture record's `analysis[]` is a 9-value array with no method/version/stage/window and must not be used as a stage-bearing measurement.
2. **Which metrics are licensed as "same material, same window" comparators across a revision change?** Valid when asset, window, stage, channel and method/version match (in practice the same `render_key`): peak, RMS, crest, LUFS, LRA, band levels, phase, correlation, mono-fold loss. Invalid across a revision: anything at a nonlinear node (master, bus, limiter) unless the reconstruction gate passes; anything whose pan or channel mode changed, because a band difference can be a pan change; and `f0_hz`/`pitch_jitter_pct`, a measured metric defect (166 kHz, 430 %).
3. **Is band dominance still intended after Round 6?** Yes — demoted to a locator, not removed, never a gate. It will carry `pan_relation`, `broadband_delta_db`, `active_fraction`, `is_psychoacoustic_threshold:false` and its method/version, and no edit may cite it as a justification.
4. **What does A require from event evidence for a masking claim?** Nothing. Window overlap and active fraction are the requirement. Simultaneous-onset gating would be an experiment (it might remove cells where a transient dominates), not a precondition.
5. **Is a stereo reference admissible for per-instrument FX claims?** No. Band difference over an aligned window, yes; source attribution only as a ranked hypothesis; chain or parameter recovery, no. Any such claim is `not_identifiable`.
6. **Invalidation granularity.** Per asset (hash/`render_key`), per evidence window, plus a method/version family tag. A relation cites two assets, so per-asset-only invalidation cannot mark a relation stale correctly.

### 7.2 What I require from B

- **`render_candidate(plan, window)` returning audio + hash + stage + revision.** B confirms it does not exist (0 hits). Without it every reference, texture and delta check here degrades to manual apply/render/undo and cannot be bound to a capture. Unchanged since Round 5.
- **Invalidation records written by the edit path**, not only by `changes_since` between captures. B confirms `lifecycle._invalidations()` is coarse, apply-time records do not exist, and `render_key` is not implemented in `daw/full_capture.py` (0 hits) — Round-6 D6-11 is "decided", not built.
- **Pan mode, channel mode and pan law in the digest and in the panorama item rows.** B says track pan is writable and no operation changes channel count or pan law; the panorama still has no item offset/playrate/channel_mode/pan_mode, so a pan edit is invisible and my `pan_relation` cannot be re-read after it.
- **No corpus distance as a compression or limiting objective.** Measured: level-invariant (1.000), sensitive to tilt/width/air (1.7–38.8×), near chance on compression (0.618 closer, 1.009×) and worse than chance on clipping (0.355, 0.991×).
- **No band-dominance or partial-loudness share as an edit precondition or claim justification.** B already agrees; I record it as an A-owned constraint.

### 7.3 What I concede to B

- The 703 s baseline is superseded by master-first + batches + `BoundedAnalysisPool`; I will not quote it as current cost, and neither of us has a full-song timing for the new pipeline — only the 12 s smoke.
- I do not need relations, texture or references in every capture; on-demand views are acceptable and cheaper. I do not need event identity, tempo, meter or stretch evidence for any perception claim.
- If `render_candidate` is deferred, the correct behaviour is an explicit `verification_unavailable` record on every affected claim, not a silent downgrade to "unchanged".

### 7.4 Open questions that only measurement or the owner can settle

| # | Question | What settles it |
|---|---|---|
| A-E1 | Does band dominance at 10 dB predict anything audible on dense distorted guitars? | A JND-style forced-choice test on a same-side guitar pair, 1 dB steps, ~20 trials per level; if non-monotone, the feature is a locator only. |
| A-E2 | Is 2 s, 3 s or 4 s the right texture window? | The same procedure over window length on cymbal-wash and fuzz-wall excerpts. |
| A-E3 | Does partial loudness survive the reconstruction gate on the real master, and what share distribution does it produce? | One real capture: `sum(stems)` vs master per band at master and at one bus; report gate status at both nodes. |
| A-E4 | Do expert listeners agree on "clarity" in this repertoire? | Krippendorff's α on 30 paired comparisons, ≥ 5 experienced listeners; below 0.5, only defect-level claims. |
| A-E5 | Does pan-first de-masking hold for already hard-panned doubles? | Replicate Wakefield & Dewey's panning condition on same-side sources — the condition they did not test. |
| A-E6 | Can a corpus model be built for this owner without becoming a genre target? | A user-supplied corpus with a manifest, stage filter, n ≥ 12 windows, and a held-out assessment that reports percentile, not a score. |
| A-E7 | Does separation bleed make role-level band comparisons meaningless on this material? | A bleed matrix on the owner's own stems: known solo sources, measured cross-role leakage, and a reconstruction residual. No number exists yet. |

All are CPU-and-listening except A-E7, which needs a separator; none needs the V100.

### 7.5 Bottom line

Round 5 got the direction right and the implementation was partial. The tree has one genuinely strong piece — reconstruction-gated partial loudness with descriptive shares — and one strong reference model that nobody wired to the model. Everything else here is a misnamed locator (band dominance), a misnamed scalar (texture), a model with no weights (Fx-Encoder++), a capability with no code (separation), or a script that violates the no-recipe rule. The decisions are therefore conservative: keep the capture as it is, add no perceptual blocks to it, compute relations, texture and reference assessments on demand against the immutable record, promote the corpus model to the single reference mechanism, delete the distance-to-corpus objective and the recipe strings, and hold every claim behind the one primitive that still does not exist — a plan-bound render of a candidate.
