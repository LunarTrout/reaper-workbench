# Round 7 — search log

Date of all access checks: 2026-11-13 (UTC+03:00). Repository read-only; no tests, REAPER
sessions, renders, downloads or GPU work were performed.

This log records what the coordinator verified directly in round 7. It does not restate the
round-5 source tables (`docs/research/round-5/AGENT-A-PERCEPTUAL-CONTEXT-REPORT.md`,
`AGENT-B-MUSICAL-ACTIONS-REPORT.md`); those entries remain the prior record and are cited here
only where a round-7 decision re-uses them. Entries marked **[C]** were opened and read by the
coordinator; **[A]** were verified by a round-7 subagent and cross-checked against the
coordinator's own read where the log says so; **[R5]** are inherited from the round-5 tables and
were not re-opened in round 7 (their verification depth is whatever round 5 recorded).

Verification depth is one of:

- **FULL** — the page/PDF returned its body text and the relevant section was read.
- **ABS** — title, authors, venue and abstract returned; method/evaluation sections not read.
- **CODE** — repository page, README and license metadata returned.
- **CARD** — dataset/model card metadata returned (size, license, splits), not the data itself.
- **META** — bibliographic metadata only (DOI/venue/date); content not read.

A paper's own reported number is recorded as the paper's claim, not as a measurement on this
machine. No source below establishes a universal mix-quality score, and none is cited as one.

## 1. Perception, masking and clarity

| ID | Source | Link | Date / version | Depth | What it supports in round 7 | Limits |
|---|---|---|---|---|---|---|
| S-A1 | Parker & Fenton, *Musical Mix Clarity Prediction Using Decomposition and Perceptual Masking Thresholds*, Appl. Sci. 11(20):9578 | https://www.mdpi.com/2076-3417/11/20/9578 | published 2021-10-14 | ABS + intro **[C]** | A decomposition into transient/steady/residual with per-component masking thresholds reached Spearman ρ = 0.8382 against a controlled listening test; linear-band masking was the best variant | One stimulus set; clarity is not "quality"; the method section was not extracted, so no implementation of its threshold model is claimed |
| S-A2 | Hafezi & Reiss, *Autonomous Multitrack Equalization Based on Masking Reduction*, JAES 63(5):312–323 | https://aes.org/publications/elibrary/elibrary-page/?id=17637 | 2015-05-06 | ABS **[C]** | The authors' own conclusion: "existing objective measures of masking are ill-suited for quantifying perceived masking in multitrack musical audio." Autonomous EQ beat an amateur mix and approached a professional one in their listening test | Paywalled full text; the negative result is about the measures they tested (simplified masking), not proof that every masking model is invalid |
| S-A3 | De Man et al., *Perceptual Evaluation of Music Mixing Practices*, AES 138th, paper 9235 | https://www.aes.org/e-lib/browse.cfm?elib=17659 | 2015-05-06 | ABS **[C]** | Some extracted sonic features correlate with rated mix quality within a tool-limited set of mixes | Correlational; not a transferable score; full text paywalled |
| S-A4 | Colonel & Reiss, *Exploring Preference for Multitrack Mixes…*, AES 147th e-Brief 526 | https://aes.org/publications/elibrary/elibrary-page/?id=20549 | 2019-10-06 | ABS **[C]** | "Virtually no correlation" between embedded MIR features and average listener preference on 184 mixes / 19 songs (Mix Evaluation Dataset) | Abstract-level confirmation of the round-5 full-text read (A-10); supports the no-universal-score rule |
| S-A5 | McDermott & Simoncelli, *Sound texture perception via statistics of the auditory periphery*, Neuron 71(5) | https://pmc.ncbi.nlm.nih.gov/articles/PMC4143345/ | 2011-09-08 | ABS + intro **[C]** | Channel statistics alone did not produce convincing textures; adding **cross-channel correlations** did. A texture representation that omits inter-channel structure is not justified by this source | Synthesis/listening paradigm, not a mix metric; no threshold for "good" texture |
| S-A6 | Meta Audiobox Aesthetics, paper + model card | https://arxiv.org/abs/2502.05139 · https://huggingface.co/facebook/audiobox-aesthetics | 2025-02-07; model card updated 2025-03-04 | ABS + CARD **[C]** | Four no-reference axes (CE, CU, PC, PQ); 103.86M params, F32, license `cc-by-4.0`; evaluated against human MOS | No black/death-metal evidence; a model prediction is not a listening verdict; PQ is not calibrated to this producer's references |
| S-A7 | Fx-Encoder++, paper + official code | https://arxiv.org/abs/2507.02273 · https://github.com/SonyResearch/Fx-Encoder_PlusPlus | arXiv 2025-07-03; repo created 2025-06-12 | ABS + CODE **[C]** | Instrument-wise FX embeddings require an **instrument query** (audio or text); repo README states CC BY-NC 4.0, GitHub metadata says "Other" | Non-commercial license; the query presupposes the separation it is meant to justify; synthetic effect-chain evaluation; not usable as a production dependency in this project |
| S-A8 | Rouard, Massa, Défossez, *Hybrid Transformers for Music Source Separation* (HT Demucs) | https://arxiv.org/abs/2211.08553 | 2022-11-15 | ABS **[C]** | 4-stem separation; 9.20 dB SDR on MUSDB with 800 extra training songs | 4-stem taxonomy (vocals/bass/drums/other); "other" absorbs layered guitars; no metal evaluation; no VRAM/throughput measured here |
| S-A9 | Lu et al., *Music Source Separation with Band-Split RoPE Transformer* (BS-RoFormer) | https://arxiv.org/abs/2309.02612 · https://doi.org/10.1109/ICASSP48485.2024.10446843 | arXiv v2 2023-09-10; ICASSP 2024 | ABS **[C]** (IEEE PDF paywalled) | SDX'23 music-separation winner; smaller model 9.80 dB average SDR on MUSDB18HQ without extra data | Same 4-stem limitation; benchmark is not dense distorted metal; no local runtime measured |
| S-A10 | Driedger & Müller, *A Review of Time-Scale Modification of Music Signals*, Appl. Sci. 6(2):57 | https://www.mdpi.com/2076-3417/6/2/57 | 2016 | FULL (body fragments) **[C]** | OLA suits percussive material; WSOLA needs a frame covering the dominant period and tolerance ≥ half a period; artifact taxonomy (doubling, skipping, smearing, phasiness) | Review; no claim that any TSM method is transparent on distorted guitars |
| S-A11 | Tomczak & Hockman, *Onset Detection for String Instruments…*, Audio Mostly 2023 + repo | https://doi.org/10.1145/3616195.3616206 · https://github.com/arme-project/onset-detection-for-strings | 2023-08 | CODE **[C]** (paper page blocked) | Code BSD; annotated data CC BY-NC-SA 4.0; the paper reports a string-instrument onset model on >8,000 onsets | Dataset is non-commercial; no evidence it transfers to amp-print black-metal guitars; not adopted as a dependency |
| S-A12 | Böck et al., *madmom* | https://arxiv.org/abs/1605.07008 | 2016-05-23 | ABS **[C]** | Library scope: onset, beat, downbeat, meter, tempo, piano transcription; CPU processors | No round-7 measurement of its accuracy on this material; project rule excludes adding a new inference dependency without an experiment |
| S-A13 | ODAQ, Open Dataset of Audio Quality | https://github.com/Fraunhofer-IIS/ODAQ | repo created 2023-12-30; 2026-10 additions | CODE **[C]** | MUSHRA expert ratings, degradations incl. coding and separation-like processing; all content under Creative Commons licenses; ODAQ-Collection adds spatial distortions | Technical-degradation labels, not artistic preference; not a training set for a "good mix" model |

## 2. Datasets and licences

| ID | Source | Link | Date / version | Depth | What it supports | Limits |
|---|---|---|---|---|---|---|
| S-D1 | De Man & Reiss, *The Mix Evaluation Dataset*, DAFx-17 | https://www.dafx.de/paper-archive/2017/papers/DAFx17_paper_49.pdf | 2017-09 | FULL (abstract + intro) **[C]** | 180 mixes, ~5,000 preference ratings, parameter settings, free-form comments, five countries; experienced subjects are harsher, more specific and agree more | Real preference data; evaluation-only. Does not supply a monotone objective |
| S-D2 | Mix-Evaluation-Dataset-2.0 (third-party HF mirror) | https://huggingface.co/datasets/sparrowreivun/Mix-Evaluation-Dataset-2.0 | last modified 2026-10-05 (card) | CARD **[C]** | 518 MB mirror; card points to the original DAFx paper; dataset viewer fails (`FeaturesError`) | No licence tag on the card; MP3; a mirror is not the authoritative release. Not usable as a shipped asset without provenance work |
| S-D3 | Slakh2100 | https://www.slakh.com/ | 2019 (WASPAA) | CARD **[C]** | 2,100 synthesised multitracks, 145 h, aligned MIDI, 34 instrument classes, CC BY 4.0 | Synthesised from MIDI: no room, bleed, amp or performance. Proves pipelines, never realism |
| S-D4 | MedleyDB | https://steinhardt.nyu.edu/marl/research/resources/medleydb | ISMIR 2014 | CARD **[C]** | Annotated multitrack, melody f0 and instrument activations; royalty-free for **non-commercial academic** research | Non-commercial terms; exact licence name still not established |
| S-D5 | Cambridge-MT "Mixing Secrets" multitrack library | https://cambridge-mt.com/ms2/mtk/ | n.d.; 645 projects listed | FULL (header/terms) **[C]** | Raw multitracks incl. rock/metal, free for educational use | "Should not be used for any commercial purpose without express permission"; research/portfolio use requires the FAQ. Not a redistributable corpus |
| S-D6 | ODAQ Collection | https://doi.org/10.5281/zenodo.23425817 (cited by S-A13) | 2026-10 | META (via repo) **[C]** | Unified collection incl. spatial-distortion subset | Not downloaded; no local evaluation |

## 3. Workstream-B decision sources

These are the FX/optimization sources that drive the Workstream-B decisions. The depth column
separates what I re-opened in round 7 from what is inherited from a round-5 full-text read;
**no row claims a round-7 full-text read where only an abstract was opened**.

| ID | Source | Link | Date / version | Depth | What it supports in round 7 | Limits |
|---|---|---|---|---|---|---|
| S-B1 | Steinmetz et al., *ST-ITO: Controlling Audio Effects for Style Transfer with Inference-Time Optimization* | https://arxiv.org/abs/2410.21233 | ISMIR 2024; arXiv 2410.21233 | ABS **[C]** in round 7; FULL-TEXT **[R5]** | Gradient-free search over arbitrary, including non-differentiable, effect chains; 1,600-evaluation budget (CMA-ES 64 × ≤25) is a round-5 body finding, not a round-7 re-read | Series chains only, user-supplied chain, minimally processed input; 1,600 renders are not cheap in REAPER |
| S-B2 | Koo et al., *ITO-Master: Inference-Time Optimization for Audio Effects Modeling of Music Mastering Processors* | https://arxiv.org/abs/2506.16889v3 | 3 Jul 2025, v3 | ABS **[C]** in round 7; FULL (partial) **[R5]** | Optimizes a reference embedding, not chain parameters; identifiability split between reverse engineering (same content) and style transfer | Content-dependent result; not a recipe for a stereo reference's plugin settings |
| S-B3 | Yu et al., *Improving Inference-Time Optimisation for Vocal Effects Style Transfer with a Gaussian Prior* | https://arxiv.org/abs/2505.11315 | 17 Oct 2025 (v2) | ABS **[C]**; ABS **[R5]** | Unconstrained ITO produces "unrealistic configurations"; a Gaussian prior cuts parameter MSE by up to 33 % | Vocal-effect setting; no full-text read; no claim that the prior transfers to arbitrary VST chains |
| S-B4 | Chu, O'Reilly, Barnett, Pardo, *Text2FX: Harnessing CLAP Embeddings for Text-Guided Audio Effects* | https://arxiv.org/abs/2409.18847 | 2024 | FULL-TEXT **[R5]**, not reopened in round 7 | Text as a control modality over a parameter space; 600 iterations × 3 runs; differentiable EQ/reverb only | Requires a differentiable effect; REAPER VSTs are not differentiable, so it is a design reference only |
| S-B5 | Comunità, Steinmetz, Reiss, *NablAFx: A Framework for Differentiable Black-box and Gray-box Modeling of Audio Effects* | https://arxiv.org/abs/2502.11668 | Feb 2025 | FULL-TEXT **[R5]**, not reopened in round 7 | Framework for black/gray-box modelling with time-varying conditioning | Per-device training dependency; a model, not a controller for arbitrary REAPER plugins |
| S-B6 | Wright & Välimäki, *Grey-Box Modelling of Dynamic Range Compression* | https://www.dafx.de/paper-archive/2022/papers/DAFx20in22_paper_35.pdf | DAFx20in22, Sep 2022 | ABSTRACT-ONLY **[R5]** | A structured compressor model matches a black-box baseline at <10 % operations/sample — the argument for a small structured search | Abstract only; no implementation or REAPER-plugin validation in this round |
| S-B7 | Comunità, Steinmetz, Reiss, *Differentiable Black-box and Gray-box Modeling of Nonlinear Audio Effects* | https://arxiv.org/abs/2502.14405 | arXiv 2502.14405; Frontiers 2025 | FULL-TEXT, partial **[R5]** | Large-scale black-box vs gray-box comparison; no universal plugin model; per-device data dependency; ToneTwist AFx dataset | Partial full-text inherited from round 5; not reopened in round 7; dataset audio has non-redistributable sources |

## 4. Project-internal evidence read this round

These are not external sources; they are the current-tree facts a decision cites. All were read
in the dirty worktree and none was modified.

| ID | File / symbol | What was verified |
|---|---|---|
| P1 | `src/reaper_audio/analysis/temporal.py::masking` (`:803`) | Band-dominance over per-cell tonal-balance-normalised bands; `min_db=10.0` default; emits `masker`/`target`, `band_hz`, `db`, `cells`; no pan relation, stage, active fraction or `is_psychoacoustic_threshold` |
| P2 | `src/reaper_audio/analysis/mix_evidence.py` (`:34`, `:43`) | Consumer renamed its issue id to `band_dominance` but still reads `report["masking_evidence"]["pairs"]` and still exposes an unused `masking_db=6.0` parameter |
| P3 | `src/reaper_audio/analysis/partial.py::attribute_partial_loudness` (`:475`) | Reconstruction gate implemented: node/stage/channel/window declaration, per-band median ≤1 dB and p95 ≤3 dB, shares withheld on failure, `equal_share_baseline_pct` descriptive only |
| P4 | `src/reaper_audio/analysis/temporal.py::_modulation` (`:313`) | Whole-file modulation rate/depth scalars only; no 2–4 s windowed texture statistics, no cross-band correlation |
| P5 | `src/reaper_audio/analysis/compare.py::reference_corridor` (`:56`) | Median + MAD over references; no stage/section/loudness pairing gate, no minimum-n beyond non-empty, no `incomparable` state |
| P6 | `src/reaper_audio/corpus/model.py`, `corpus/report.py` | Ledoit-Wolf covariance + leave-one-out empirical percentile; report explicitly "a vector plus a rank, not a score" |
| P7 | `scripts/reference_delta.py` (`:47-48`) | Contains Russian direction hints ("режь гитары/бас в этом месте") — a recipe if surfaced through MCP |
| P8 | `src/reaper_audio/daw/analysis_pipeline.py::BoundedAnalysisPool` | Bounded producer/consumer pool; explicit back-pressure; per-GUID results; no bridge calls from workers |
| P9 | `src/reaper_audio/daw/analysis_policy.py` (`MAX_ANALYSIS_WORKERS=8`) | 8/4/4 worker policy; memory envelope 384 MB + 14 MB/s per worker; explicit overrides fail closed |
| P10 | `src/reaper_audio/daw/capture_batching.py::plan_capture_batches` | Topology-aware batches, folder atomicity, singleton merge, fallback to whole-project render |
| P11 | `src/reaper_audio/daw/full_capture.py:560-640` | Master-first render, per-batch admission, pool submission during next render; no per-file progressive closure |
| P12 | `src/reaper_audio/context/panorama.py::_item_views` (`:178`) | Item view carries position/length/take ids only — no source offset, playrate, stretch markers, fades, mute, group or channel mode |
| P13 | `scripts/reaper_bridge.lua:372-421`, `:1156-1165` | Snapshot/digest **do** read stretch-marker positions and playrate-adjacent take fields |
| P14 | `scripts/reaper_bridge.lua:2907-2955`, `:3086-3109` | `item.set` supports position/length/mute/stretch_ratio/stretch_markers/beat_attachmode/auto_stretch; post-edit readback for `item.split` returns `item_count` only; no `D_STARTOFFS` setter |
| P15 | `tests/test_analysis_optimizations.py`, `tests/test_capture_pipeline.py`, `tests/test_full_capture_parallel.py` | The optimisations are pinned by tests (vectorised envelope/RMS parity, short-term reference values, pool bounds, GUID order, failure propagation, overlap timing) |
| P16 | `docs/PERFORMANCE-PIPELINE-RESEARCH.md`, `REPORT-A`, `REPORT-B` | 703.072 s sealed baseline; DSP pool 408.274 s; render 107.975 s; packaging tail 149.894 s; 20 s capture 61.697 s (M27) vs 78.518 s in `FULL-CAPTURE.md` — different runs, not reconciled |

## 5. Access failures and honest gaps

| Source / attempt | Result | Consequence |
|---|---|---|
| `https://www.open-access.bcu.ac.uk/15136/` (string-onset paper) | Blocked: "URL targets a private or internal network address" | Only the DOI and the official repo (S-A11) were verified; no method claims from the paper |
| IEEE Xplore PDF for BS-RoFormer (S-A9) | Paywalled ("You do not have access to this PDF"); abstract page returned | Abstract-level claim only; SDR numbers are the paper's, not reproduced |
| AES full texts S-A2, S-A3 | E-library abstract pages only | The negative masking result is quoted from the publisher's abstract; no method-section citation |
| MDPI clarity paper (S-A1) | HTML returned abstract + introduction; method/evaluation sections were not extracted | ρ = 0.8382 is the paper's reported result; no claim about its thresholds being reproducible |
| Mix Evaluation Dataset 2.0 viewer | `FeaturesError`, split not viewable | Card metadata only; file contents not inspected; licence unresolved |
| `https://github.com/facebookresearch/fx-encoder` | HTTP 404 | The real official repo is `SonyResearch/Fx-Encoder_PlusPlus` (S-A7); the first URL in the attachment does not exist |
| REAPER ReaScript API reference | Page returned a 50,000-character head/tail window of a much larger document; `GetTakeStretchMarker` fell outside the window | Stretch-marker capability is instead verified from the project's own bridge (P13), not from a complete API-doc read |
| GPU/model runtime for any neural option | Not attempted (prohibited this round) | Every VRAM/throughput figure in the round-7 documents is UNMEASURED |

## 6. Search strategy and stopping rule

Queries started from the attachment's titles, then followed the primary artefact (publisher page,
arXiv, official repository, dataset card). A source was admitted to a decision only if at least its
abstract and artefact metadata were retrieved this round or it was already recorded at a stated
depth in the round-5 tables. Searching stopped when two consecutive rounds of queries added no new
decision-relevant source: the 2025–2026 additions (Audiobox Aesthetics, Fx-Encoder++, ODAQ spatial
subset) were already present in the attachment and changed licence/cost facts rather than the
architecture.

Deliberately **not** searched: plugin-specific parameter values from reference masters, genre
mixing recipes, "best EQ for black metal" material, and any source that would only support a
universal quality score. Those are out of scope by contract, not by lack of time.
