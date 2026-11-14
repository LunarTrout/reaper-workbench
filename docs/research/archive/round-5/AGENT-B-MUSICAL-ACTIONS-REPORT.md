# Workstream B — From audio evidence to general, safe, verifiable REAPER actions

**Scope:** musical events and editing; note/onset/transcription and microtiming; time-scale modification and its artifacts; general-purpose black-/gray-box effect optimization and control; panning and subgrouping; operation-specific datasets and end-to-end validation of these actions.

**Status:** research report for round 5. No source code, test, project, media, configuration or model was modified. One file created: this one. The concurrently optimized files (`analysis/events.py`, `temporal.py`, `metrics.py`, `daw/analysis_workers.py`, `tests/test_analysis_optimizations.py`, `docs/PERFORMANCE-OPTIMIZATION-TZ.md`, `docs/VALIDATION.ru.md`) were read for current behaviour only; where a conclusion depends on their in-flight state it is flagged.

**Repository state read:** `main`, HEAD `86b14bb` ("Alignment: no classifier, measured drift, policy decides"), shared dirty tree preserved (21 modified paths listed by `git status --porcelain`, including `scripts/reaper_bridge.lua`, `src/reaper_audio/analysis/events.py`, `daw/session.py`, `mcp_server.py`).

**Evidence labels used throughout:** `[FULL-TEXT]`, `[ABSTRACT-ONLY]`, `[REPO-CODE]`, `[DATASET-CARD]`, `[PROJECT path:line]`, `[INFERENCE]`. Every number I quote is either quoted from the source or shown as arithmetic with its inputs.

---

## 0. Interim note to Workstream A

Written before your report existed, then reconciled against it (your §0 and §0.5). Your five answers changed three things in mine; I record the changes rather than quietly absorbing them.

### 0.1 What I depend on from you

1. **A calibrated audibility model, or an explicit refusal.** My seam and edit verification can measure level, sample step and spectral flux, but it cannot say whether a 0.6 dB dip over 8 ms is audible under four distorted guitars. If your masking work cannot produce a defensible threshold for dense distorted metal, my gates must stay *relative* (before/after on the same material) and never absolute. Please state which of the two you are delivering.
2. **Reference-analysis targets.** Reference-guided FX optimization needs a target. If your reference analysis produces per-band/per-dynamic targets with uncertainty, black-box search has an objective. If it produces only descriptive text or corpus distance, my section 4 conclusion stands: parameter search without a target is a random walk with a score.
3. **A style/distance metric that is not a quality verdict.** ST-ITO's whole method rests on a learned *production-style* similarity (AFx-Rep), and the Gaussian-prior follow-up shows that unconstrained optimization of such a metric yields "unrealistic configurations or biased outcomes". If your work endorses a particular embedding (Fx-Encoder++, Audiobox, CLAP), say whether it is stable enough to be an optimization objective or only a retrieval aid.
4. **Stem/separation verdict.** If your separation assessment says per-instrument streams are unreliable on dense metal, then chain inversion per instrument (section 5) is off the table and FX work reduces to whole-track/whole-master parameter fitting.
5. **Listening protocol and dataset legality.** My test matrix (section 6) needs your blind/paired protocol and your licence audit of candidate corpora, so we do not run two incompatible listening designs.

### 0.2 Overlap I am deliberately *not* re-auditing

- Fx-Encoder++: I use only the *conclusion* that a representation can encode effect identity/parameters well enough for retrieval, and that this does **not** identify a unique chain. I do not re-verify the model.
- Masking thresholds, clarity metrics, auditory texture, ODAQ/Audiobox/Mix Evaluation datasets, ViSQOL/PEAQ/PEMO, HT Demucs / BS-RoFormer / BS-RNN: yours. Where I mention separation or PEAQ it is only to state a limit of an *action* method.
- HiMMP corpus statistics: yours. I use it only as a licence-clean same-song multitrack for the test matrix.

### 0.3 My questions, and your answers — what changed in this report

| My question | Your answer (A §0.5) | What I changed |
|---|---|---|
| **Q-A1** fixed click/gap threshold? | **No.** Relative gates only; any absolute number is a policy key, fails closed when unset (`threshold_basis: "relative"`). | §4.3 A/B and §6 A7–A8 now mark every absolute number as a policy key with a named owner; the click test is explicitly relative |
| **Q-A2** numeric targets? | Numeric but **conditional and distributional** (median/MAD/n/bootstrap CI, `target_kind`), only with stage match, section match, n ≥ 8, loudness matched ≤ 0.5 dB. **Minimize distance to the corridor, not to the median**; a corridor is not a quality target. | §5.3 now requires corridor *rank/percentile* distance and forbids the "drive distance to zero" objective |
| **Q-A3** is the embedding monotone? | **No.** "Virtually no correlation" between mixdown features and preference; FX representations report reduced parameter matching. Ranking/retrieval only. | §5.3 Step 8 is now **gated**: no CMA-ES against a style embedding until a monotone objective exists; search must target explicit DSP constructs |
| **Q-A4** strongest source claim? | Band-difference **yes**, source attribution **hypothesis-only**; 4-stem separation means "guitar" is not a stream. | §5.4 adopts your phrasing verbatim in substance: no attribution, only ranked hypotheses |
| **Q-A5** short seam stimuli? | **Yes**, but ≥ ~2 s, loudness matched ≤ 0.5 dB, **forced-choice 2AFC/ABX**, not preference. | §6 Step 10 adds the ≥ 2 s, 0.5 dB and forced-choice constraints |
| **Q-A6** licence-clean corpora? | Not answered in your §0.5. | Still open; §6 keeps licence status per tier and refuses to assume |

### 0.4 Your questions to me, answered

- **Q-B1 — does an edit transaction preserve the capture hash and `RevisionRef` so evidence stays fresh/stale correctly?** **No, not today.** `EditPlan` carries a `ProjectRevision` (`project_id`, `change_count`, `path`), not a `RevisionRef` with fingerprint (`daw/session.py:68-80`); the MCP caches `applied_revision` (`mcp_server.py:968-976`); and no dependency record is written on apply, so a capture's `ast_sha256` is never linked to the edit that invalidated it. Your `state` field would indeed be decorative. **Design commitment from my side:** `session.apply` must write an invalidation record naming (a) the post-apply `RevisionRef`, (b) every operation's target GUIDs, (c) the evidence IDs the plan cited, and (d) the asset IDs whose measurements are now stale. That is Step 3/5 work, and it is cheap because the plan already knows (b) and (c).
- **Q-B2 — will I expose per-operation expected effect on measured constructs?** Partially, and I will not overclaim. What exists: `eq.calibrate` maps normalised↔displayed values by sampling the plugin (`daw/eq.py:140-169`), and `limiter` searches a threshold until a *measured* loudness target is met (`daw/limiter.py:100-167`). What does not exist: a predicted band-level delta for an arbitrary EQ move. I will add `expected_after` for **geometry** (exact) and for **calibrated single parameters** (value, not audio effect). Construct-level prediction (your "band 315 Hz rises by X dB") needs a measurement of the plugin's response and is a later step; until then a post-change delta cannot be attributed to the operation alone, and my plans must say so.
- **Q-B3 — do I need your texture statistics for transient/attack preservation?** Yes, but only as a **shared vocabulary**, not a second metric set. I will reference, not redefine: your texture summary over 2–4 s windows for "does the wall survive", plus my four local seam quantities (below). If you define attack density and spectral-flux decay in the shared contract, I will consume them.

**The four artifact quantities I will expose, with units, so you can cite them instead of inventing a second vocabulary:**

| Name | Unit | Definition | What it catches |
|---|---|---|---|
| `attack_count_delta` | integer | matched attacks after − before, same source/window | doubling, skipping |
| `attack_position_error_ms` | ms, signed | per matched attack, after − before | smearing, delay, drift |
| `pre_echo_ratio_db` | dB | energy in the 20 ms before the attack ÷ energy in the attack window | pre-echo from PV/OLA |
| `attack_rise_ms` | ms | 10 %→90 % of the attack envelope | transient softening |

Phasiness has **no** validated proxy in my scope; I will not invent one. If you have a texture statistic that tracks it, name it and I will use yours.

### 0.5 Remaining disagreement

Only one, and it is narrow. You say the masker-only cut was the *least* preferred de-masking tool in the blind study; my seam work can still require a cut to remove a bad transient, not to de-mask. Those are different uses of the same primitive, and I will label the intent so the two never get conflated: `edit_intent ∈ {timing, artifact_removal, arrangement, demasking}`. A `demasking` intent should be the one your evidence constrains, not `timing`.



---

## 1. Executive answer

Five design changes follow from the evidence, and one thing should *not* be built.

1. **Split "alignment" into two named operations and drop the quantizing default.** The repo holds both semantics: `daw/alignment.py:12-31` moves events *to the project grid* (`move = (grid − time) × strength`), while `daw/align.py:82-125` matches *the same transient in a reference take*. `docs/TZ.md` §5 asks for the second and forbids blind quantization. Two functions with opposite musical meanings under one name is the most dangerous ambiguity in the action layer. Keep both; rename them (`align_to_reference`, `quantize_to_grid`); require an explicit intent for the latter.

2. **Make the edge a first-class operation, and stop pretending `right_trim_s` reveals source.** `scripts/reaper_bridge.lua:3504-3556` splits, extends the left item by `tail_s`, and moves the right item by `move_s + right_trim_s` **without touching `D_STARTOFFS`** — exactly the limitation `docs/EDITING-ACCEPTANCE.md:35` names. The owner's ORIG case needs the opposite: reveal the right slice's past while the already-placed attack stays fixed, i.e. `Δposition = −Δ`, `Δlength = +Δ`, `ΔD_STARTOFFS = −Δ × playrate`, atomically, with readback.

3. **Wire the event model to the action path and enforce `UsageScope`.** `analysis/event_model.py` defines `SignalEvent`, `PerformanceOccurrence`, `PerformanceEvent`, `PerceivedGesture`, `ScoreObject` and a `UsageScope` (`timing/pitch/duration/midi/destructive_cut`, lines 161-196, 451-692). Nothing outside that file constructs or consumes any of them, and `daw/session.py` never checks the scope. An onset-only detector can therefore authorize a destructive cut through the typed API.

4. **Treat FX as two problems.** Chain topology is a musical decision from context. Parameter optimization inside a *user-supplied* chain is a bounded search the literature has solved under stated assumptions (ST-ITO: non-differentiable effects, series only, input assumed minimally processed, CMA-ES 64 × 25 ≈ 1,600 renders). Do not import differentiable-DSP methods (GRAFX, NablAFx, Text2FX) as if they applied to arbitrary VSTs, and never call the search cheap: in REAPER every candidate is a native render.

5. **Verification must be render-based and relative.** A split currently reads back only `item_count` (`reaper_bridge.lua:3046-3049`), and the seam ranker's click term is amplitude- and frequency-dependent (full-scale 1 kHz → 0.1425/sample; 5 kHz → 0.7124), which exceeds its own 0.1 normalizer. Compare before/after on the same material; read geometry per sample.

**Do not build** a "tighten the guitars" tool. Dannenberg's own retrospective on losing rhythmic feel, Degara's 50 ms annotation floor and the 30 ms clustering at `edit_map.py:27` all point to the same general capability: event hypotheses with confidence, a reversible edge/move/stretch primitive, and verification.

---

## 2. (a) Verified source-by-source findings

Each block states what the source actually does, its input/output, assumptions, data, licence, compute, reported metrics, failure cases, access status, the project decision it informs, and what earlier rounds already settled or got wrong.

### 2.1 Intelligent multi-track editing

#### B01. Dannenberg, *An Intelligent Multi-Track Audio Editor*, ICMC 2007 — [FULL-TEXT]

- **What it does.** A complete score-driven editing pipeline (IAED): audio-to-score alignment (chroma + DTW) → onset labelling (with a bootstrap learner) → per-note RMS and YIN F0 → three edit classes: pitch correction, time alignment, level balancing. Verified in the paper's §3–§4.
- **Input/output.** Input: multi-track audio + a machine-readable score (MIDI) + optional tapped beats. Output: re-rendered tracks with corrected pitch/timing/balance. The edit representation is explicit and is the important part: *"Each operation is described by a triple (Ti, Fi, Di), where Ti is the time to play clip i, which is taken from the original track at offset Fi and duration Di"* (§4.2, Figure 2 caption). That triple is precisely `D_POSITION` / `D_STARTOFFS` / `D_LENGTH` in REAPER.
- **Assumptions.** *"For simplicity, assume that every track is a recording of a pitched monophonic instrument"* (§3). Time alignment uses a "master" track: lower voices are warped so that score-coincident notes become audio-coincident (§4.2). Dynamic level uses mean RMS over the half-power span around each note's peak; F0 uses YIN averaged over the same span, ignoring low-confidence frames (§3.2).
- **Data / licence / compute.** No dataset released; no numbers. Prototype described as *"fully implemented"* but *"only been used for small test examples"*, evaluation *"necessarily subjective"* (§5). Compute is 2007-era DSP, no GPU. Licence: paper on the author's CMU page; no code release found.
- **Reported metrics.** None quantitative. Qualitative claims: better intonation, "greatly improved rhythmic accuracy", acceptable balance; and an explicit negative: *"The one area where expressiveness seems to be compromised is the rhythmic 'feel'"* (§5).
- **Failure cases it names itself.** Onset alignment is *"only an approximation to a more sophisticated view that considers perceptual onset times"*; it suggests instead detecting outliers and matching them to the mean rather than lining up all onsets (§6). Pitch correction assumes equal temperament. Polyphonic material is called out as out of scope (§6).
- **Decision informed.** (i) The edit triple is the correct abstraction for our `item.edge`/`item.split` operations. (ii) "Fix outliers, not everything" is the policy our `edit_map` intent levels are groping toward, and it should be expressed as *protected events + outlier test*, not as a global strength knob. (iii) Score/MIDI is a *specification of intent*, not evidence of performance — matches `ScoreObject`'s `origin=project_midi` design (`event_model.py:757-819`).
- **Prior rounds.** R2-02 already established "MIDI proves project state, not the heard note" (decision D25). This paper is the primary source for *why* a score-driven editor needs the score as intent, and for the cost: it cannot work without one.

#### B02. Liu, Dannenberg, Cai, *The Intelligent Music Editor*, ICIC 2010 — [ABSTRACT-ONLY]

- **What it does.** The successor platform paper to B01: an automated platform for music analysis and editing. Springer chapter, LNCS 6215/6216, pp. 123–…, DOI [10.1007/978-3-642-14932-0_16](https://doi.org/10.1007/978-3-642-14932-0_16). The author's own bibliography lists it as *"Proceedings of the Seventh International Conference on Intelligent Computing, Cairo, Egypt, December 2010, pp. 123-"*.
- **Access.** Paywalled; I could not obtain the full text, and I will not characterise its internals beyond the title and venue. **Verification level: title/venue confirmed from two independent listings (Springer chapter page; Dannenberg's publication list); content not verified.**
- **Decision informed.** None beyond lineage. I recommend *not* citing it as evidence for any mechanism; B01 is the substantive source.

### 2.2 Onset detection, note transcription, microtiming

#### B03. Tomczak & Hockman, *Onset Detection for String Instruments Using Bidirectional Temporal and Convolutional Recurrent Networks*, Audio Mostly 2023 — [ABSTRACT-ONLY] + [REPO-CODE]

- **What it does.** Two models (bidirectional temporal CNN + convolutional recurrent network) for onset detection aimed at *less percussive* onsets, i.e. bowed/plucked strings. Abstract: the models *"outperform the state-of-the-art algorithms on string recordings while maintaining comparative performance on other types of music"*.
- **Data.** A new string dataset of *"approximately 40 minutes and over 8,000 annotated onsets with varied expressive playing styles"*, plus a MIR benchmark. Dataset = Virtuoso Strings release 2.0.
- **Licence (verified at source).** Code BSD; dataset **CC BY-NC-SA 4.0** ([repo README](https://github.com/arme-project/onset-detection-for-strings)). The non-commercial clause matters for a product.
- **Compute.** Not stated in the abstract; the repo exists but no VRAM/throughput figures were verified. Treat V100 fit as unverified.
- **Failure cases.** The motivating claim is that *percussive* onsets are easy and *string* onsets are hard because of weak transients — which is the opposite of our problem in one respect (distorted guitars have strong attacks) and the same in another (sustained, legato, tremolo passages have weak ones). Domain is acoustic strings (violin family, guitar), not high-gain electric guitar.
- **Decision informed.** A specialised onset model is *plausible* but not established for our material; if we ever adopt one, it must be retrained/fine-tuned because the transfer evidence in B05 says cross-dataset transfer fails.
- **Access.** PDF text layer at the BCU open-access mirror was partial; numbers (F1 per dataset) are **not verified**, so I quote none.

#### B04. Wang, Stables, Li, Duan, *Score-Aligned Polyphonic Microtiming Estimation*, ICASSP 2018 — [ABSTRACT-ONLY]

- **What it does.** Estimates microtiming (per-note timing deviation) in polyphonic music by aligning to a score, rather than detecting onsets blind. DOI [10.1109/ICASSP.2018.8461699](https://doi.org/10.1109/ICASSP.2018.8461699).
- **Access.** ACM/IEEE pages returned 403; the author PDF extract yielded only a fragment about chroma + DTW alignment. **I did not verify its method details, dataset, or numbers.** I therefore use it only as a pointer: the *idea* of score-aligned microtiming is the right frame for "groove relative to a notated grid", and it requires a score — which our project has only when MIDI exists.
- **Decision informed.** Microtiming claims must be *relative to a declared reference* (score, another take, or a beat hypothesis), never absolute. This is consistent with `performed_beat` requiring `hypothesis_id` (`timebase.py:201-204`).

#### B05. Gong & Serra, *Towards an efficient deep learning model for musical onset detection*, arXiv 1806.06773 — [ABSTRACT-ONLY] (ar5iv page truncated at §1.1)

- **What it does.** Compares seven deep architectures for onset detection and proposes one with **28.3 % of the trainable parameters** of their SOTA implementation at equivalent performance. Reproducibility is the stated motivation (missing hyper-parameters, missing training code, unfair architecture comparison).
- **The finding that matters most for us.** *"The results show that the model pre-trained on one dataset fails to detect onsets on another dataset, which denotes the importance of providing the implementation code to enable re-training the model for a different dataset."* Datasets: one instrumental, one self-built solo-singing.
- **Licence/compute.** Code and notebook public ([ronggong/musical-onset-efficient](https://github.com/ronggong/musical-onset-efficient)); licence not verified in this pass.
- **Decision informed.** **Do not** treat any pretrained onset model as a drop-in for distorted metal. Cross-dataset failure is reported by the authors themselves. This is the strongest single argument for keeping our onset evidence DSP-based and multi-source, with a learned model only as an optional, locally validated addition.

#### B06. Degara, Davies, Pena, Plumbley, *Onset Event Decoding Exploiting the Rhythmic Structure of Polyphonic Music*, IEEE JSTSP 5(6), 2011 — [FULL-TEXT]

- **What it does.** Probabilistic decoding of onset sequences using inter-onset-time templates derived from a tempo estimate: it integrates an arbitrary onset detection function with rhythmic expectations and decodes the most likely event sequence.
- **Data and evaluation.** 13 polyphonic excerpts, **551 annotated onsets**, hand-annotated by experts; **50 ms tolerance** window for a correct detection (quoted: *"A correct detection is defined as one occurring within a 50 ms tolerance window of each ground truth onset"*). Compared against adaptive thresholding with several ODFs (spectral flux, RTFI) and two tempo estimators (Davies, Ellis).
- **Reported result.** Rhythmic decoding beats adaptive thresholding on all files except one, where *"four of the annotated onsets are less than 50 ms away from each other"* and the annotation itself is at the noise floor of hand labelling.
- **Failure cases.** Requires a tempo estimate; performance tracks tempo-estimator quality. Dense co-articulation below 50 ms is unresolvable *by the evaluation* itself.
- **Decision informed.** Two concrete numbers for our tolerances: (i) at 200 BPM a 16th is 75 ms and a 32nd 37.5 ms; at 300 BPM a 32nd is 25 ms. Any metric with a 50 ms tolerance cannot distinguish adjacent 32nds in fast metal — exactly the regime of blast beats and tremolo. (ii) Rhythmic priors *help* when the tempo hypothesis is right and *hurt* when it is wrong; our `tempo.py` already keeps metrical alternatives, so the decoder must consume the alternatives, not the top candidate only.

#### B07. Kehling, Abeßer, Dittmar, Schuller, *Automatic Tablature Transcription of Electric Guitar Recordings*, DAFx 2014 — [FULL-TEXT]

- **What it does.** Full tablature transcription chain: onset detection → pitch detection (per-string) → string/fret classification → playability filtering. Evaluated on the IDMT-SMT-Guitar database (seven guitars, standard tuning, varying pickups).
- **Reported numbers (quoted).** Onset detection **F-measure 98.5 %**; pitch detection **F-measure 0.96**; abstract states *"very high accuracy values of 98 %"* and *"82 % for …"* (the 82 % figure is in a passage my extract did not preserve — I do not quote its subject).
- **Assumptions that break for us.** Standard tuning, isolated note recordings, clean (non-distorted) electric guitar, monophonic note sequences per string. Drop tunings, 6/7/8-string instruments, palm mutes, pinch harmonics, and high-gain timbre are out of the evaluated domain.
- **Compute/licence.** DSP + classifier, CPU-scale, 2014. Dataset IDMT-SMT-Guitar: research licence, not commercial.
- **Decision informed.** Tablature/fingering is a *separate, harder* task than note onsets (decision O09 already defers it). The 98.5 % onset F-measure is on clean guitar — it is not evidence for distorted metal. Do not promise tab from the current pipeline.

#### B08. Mounir, Karsmakers, van Waterschoot, *Musical note onset detection based on a spectral sparsity measure*, EURASIP JASMP 2021 — [FULL-TEXT]

- **What it does.** A non-data-driven ODF (NINOS²) based on a spectral-sparsity ratio of *low-energy* STFT bins (deliberately excluding the high-energy harmonic bins). Two improved variants; comparison against log-spectral flux (LSF); complexity analysis; a semi-synthetic annotation method built from isolated note recordings.
- **Why the idea is interesting for us.** The paper's own observation is that transient frames are *less* spectrally sparse than steady-state frames, and that the discriminative information sits in the low-energy bins — which is exactly the regime where a heavily compressed, harmonically dense distorted guitar has little headroom.
- **Reported numbers.** My extract did not preserve the results tables (the HTML renders them as images/math), so I quote **no** F-measure. Claims: superior performance in *"challenging NOD scenarios involving polyphonic music, repeated notes, and vibrato effects"*.
- **Failure cases implied by the method.** Repeated notes with insufficient magnitude increase at the onset are named as a weakness of difference-based ODFs generally; the sparsity feature is designed to counter that, but the evaluation is semi-synthetic.
- **Decision informed.** A cheap, deterministic, non-neural ODF is a legitimate *second opinion* alongside our 2 ms envelope detector — and it is CPU-only, so it does not touch the V100 budget.

#### B09. Böck et al., *madmom: a new Python Audio and Music Signal Processing Library*, arXiv 1605.07008 — [ABSTRACT-ONLY]

- **What it does.** Python MIR library with `Processor` pipelines, HMM/RNN modules, and reference implementations for onset detection, beat/downbeat/meter tracking, tempo estimation, piano transcription.
- **Licence.** BSD (well-known; not re-verified in this pass — flag in §9). **Compute.** CPU; the RNN onsets are small but CPU inference is excluded by the project rule, which matters: adopting madmom's RNN onsets would require a GPU port to satisfy the constraint, or would violate it.
- **Decision informed.** madmom is the right *reference implementation* to compare our DSP detectors against offline, and the wrong dependency to add to the runtime path under the "no CPU neural inference" rule.

### 2.3 Time-scale modification

#### B10. Driedger & Müller, *A Review of Time-Scale Modification of Music Signals*, Applied Sciences 6(2):57, 2016 — [FULL-TEXT]

This is the single most useful source in my scope for the *action* design, because it explains exactly which artifacts each method produces and why.

- **Taxonomy and mechanisms (quoted/paraphrased from the article).**
  - **OLA**: unsuited to harmonic signals — phase-jump artifacts and "a characteristic warbling sound"; *good* for percussive signals if the frame is short, *"roughly 10 milliseconds of audio material"*, to reduce transient doubling.
  - **WSOLA**: aligns successive frames by cross-correlation. *"Transient doubling"* when stretching, *"transient skipping"* when compressing. It can only preserve *"the most prominent periodic pattern"*, so polyphonic material still warbles. Typical parameters: frame ≈ 50 ms, tolerance ≥ 25 ms.
  - **PV-TSM**: preserves *horizontal* phase coherence (no phase jumps) but destroys *vertical* coherence → **transient smearing** and **"phasiness"**, described as *"reverberant, having 'less bite', or a 'loss of presence'"*. Identity phase locking and PVSOLA reduce it.
  - **Transient-preservation schemes**: detect transients, cut them out, fill the gaps (e.g. by linear prediction), stretch the remainder, re-insert the transients at scaled positions. This is exactly the Nagel & Walther scheme (B12).
  - **HPS-based**: separate harmonic and percussive components, PV-TSM the harmonic part, short-frame OLA the percussive part. Implicit transient preservation; the transient may be doubled but the doubling is too fast to hear.
  - **Anchor-point stretch functions**: define pairs (input time, output time), interpolate linearly, apply a TSM procedure. The paper's example is aligning two performances of Beethoven 5 by their note onsets.
- **Licence.** MDPI open access (CC BY). **Compute.** Review; the referenced implementations are TSM Toolbox (MATLAB), librosa, Rubber Band (transient-preserving PV), SoundTouch (WSOLA-like), élastique (proprietary).
- **Decision informed.** (i) **Anchor points are exactly what a REAPER stretch marker is.** Our `item.set stretch_markers` writes `(position_s, source_position_s)` pairs (`scripts/reaper_bridge.lua:2301-2320`), i.e. the same object. So the literature's non-linear time-warp model maps directly onto a native REAPER primitive. (ii) The artifact taxonomy gives us *named* failure modes to verify: doubling, skipping, smearing, phasiness. (iii) For the owner's case, **any** stretch is an artifact source, and `docs/EDITING-ACCEPTANCE.md:36` forbids it — the review explains why that is musically correct rather than arbitrary.

#### B11. Bryan, Herrera, Wang, *User-Guided Variable-Rate Time-Stretching via Stiffness Control*, DAFx 2012 — [FULL-TEXT]

- **What it does.** The user draws a time-dependent *stiffness* curve plus timing constraints (total length, quantization to swing); a constrained optimization (physical spring analogy) converts that into a time-varying stretch rate, which then drives a phase-vocoder or PSOLA processor.
- **Why it matters.** It separates *musical intent* (stiffness/constraints) from *the DSP rate curve*. That is the interface shape our edit API should expose: the model states anchors and protected events; the library computes positions.
- **Failure cases / compute.** Initial results only (paper says "initial results are demonstrated"); PC/PSOLA, CPU.
- **Decision informed.** Do not expose a raw "stretch factor" and expect a model to use it musically. Expose anchors + protected events + total-length constraint.

#### B12. Nagel & Walther, *A Novel Transient Handling Scheme for Time Stretching Algorithms*, AES 127th Convention, Paper 7926, Oct 2009 — [ABSTRACT-ONLY]

- **What it does (abstract, quoted).** *"Transient regions are replaced by stationary signals. The thereby removed transients are saved and re-inserted to the time dilated stationary audio signal after time stretching."*
- **Access.** AES e-library paywalled; abstract obtained from the AES convention-forum page (paper 7926, 127th Convention, October 2009). No numbers verified.
- **Decision informed.** Confirms that "protect the attack, stretch the sustain" is a 15-year-old, well-understood decomposition — and that any implementation must therefore *identify* transients and *re-insert* them, i.e. two failure points. If we ever stretch a guitar item, the verification must separately check attack count and attack position, not just spectral similarity.

#### B13. Juillerat, *Audio Time Stretching with Controllable Phase Coherence*, AES 142nd Convention, Paper 9779, 2017 — [ABSTRACT-ONLY]

- **What it does.** A hybrid TSM where a *single parameter* trades vertical against horizontal phase coherence: one extreme behaves like a time-domain method, the other like a phase-locked vocoder. Claim: middle values work well for mixed content.
- **Why it matters.** It formalises the exact trade-off that decides whether a stretched guitar sounds "smeared" or "warbling", and it gives a *control* rather than a fixed algorithm. REAPER's own stretch modes expose a similar choice.
- **Access.** Abstract + introduction read from the author's PDF; the extract did not include the evaluation, so **no metrics are quoted**.
- **Decision informed.** When a stretch is unavoidable, the stretch *mode* is a first-class parameter to record in the transaction (and to A/B), not an implementation detail.

### 2.4 Effect modelling, optimization and control

#### B14. Steinmetz et al., *ST-ITO: Controlling audio effects for style transfer with inference-time optimization*, arXiv 2410.21233 (ISMIR 2024) — [FULL-TEXT]

- **What it does.** Instead of training a network to predict effect parameters, it *searches* the parameter space of a user-supplied effect chain at inference time, maximising cosine similarity between an audio-production-style embedding of the processed input and of a reference. The embedding (AFx-Rep) is pretrained self-supervised by predicting *which effect* and *which preset* was applied.
- **Assumptions, stated by the authors.** Input is *"assumed to have minimal processing, as our system does not remove effects"*. The user *"provide[s] an appropriate chain of audio effects to be controlled"*. *"While our method supports arbitrarily complex effect chains, we consider only series connections."*
- **Optimizer and budget (quoted).** CMA-ES; *"a population size of 64 and a maximum of 25 optimization steps"*; σ initialised 0.3; parameters initialised at 0.5 in [0,1]; early stopping after 10 steps with improvement < 0.1. Control dimension ≈ 100; two chains evaluated: **5 VST effects / 73 parameters** (TubeScreamer, ZamEQ2, ZamCompX2, ZamDelay, TAL-Reverb-4) and a pedalboard chain of **36 parameters**.
- **Arithmetic.** 64 × 25 = **1,600 candidate evaluations** per transfer, before early stopping.
- **Failure cases.** Non-differentiable is fine, but *unseen* effects still need the chain to be supplied; the metric is a learned proxy, not a perceptual truth; the paper's own benchmark exists because existing style metrics were inadequate.
- **Decision informed.** This is the *only* method in scope that matches our constraint "arbitrary REAPER VST, no gradients". It also fixes the realistic budget: ~10³ renders, not ~10².
- **Licence/code.** Code public ([csteinmetz1/st-ito](https://github.com/csteinmetz1/st-ito)); licence not re-verified here.

#### B15. Koo, Martínez-Ramírez, et al., *ITO-Master: Inference-Time Optimization for Audio Effects Modeling of Music Mastering Processors*, arXiv 2506.16889v3, 2025 — [FULL-TEXT, partial]

- **What it does.** Reference-based mastering style transfer with inference-time optimization applied to the **reference embedding** rather than to the chain parameters, to give the user fine control after the first pass.
- **The key negative result (quoted).** *"in reverse engineering tasks—where the input and output content is identical—optimizing Θ works well despite the complexity of the Fx chain. However, in mastering style transfer, content-independent loss functions are used to replicate only the mastering style from the reference track. This distinction highlights why ITO on Θ is less effective in this context."*
- **Decision informed.** **This is the cleanest statement of the identifiability boundary in my whole scope.** When input and output content are the *same* (we have the dry signal and the processed signal of the same performance), fitting chain parameters is well-posed. When only a *different* track's master is available as reference (our usual case), fitting the chain is ill-posed and the method degrades to fitting a style embedding. Any "match this reference master" feature must state which of the two regimes it is in.
- **Licence.** CC BY 4.0 (stated on the arXiv HTML). Metrics: AF loss and DRV; subjective test with a low anchor and no high anchor.

#### B16. Chu, O'Reilly, Barnett, Pardo, *Text2FX: Harnessing CLAP Embeddings for Text-Guided Audio Effects*, arXiv 2409.18847 — [FULL-TEXT]

- **What it does.** Single-instance gradient optimization of *differentiable* EQ (6-band, 18 params) and reverb (23 params) parameters so that the CLAP embedding of the processed audio moves toward a text prompt (cosine) or along a text direction (directional). No retraining.
- **Optimization details (quoted).** Adam, lr 1e-2, *"random shift of at most 1500 ms … at every iteration"* to stop the model latching onto content, *"convergence within 300-400 iterations"*, extended to **600 iterations**, **three runs** per instance picking the lowest loss. So ≈ 1,800 optimization steps per prompt/instance.
- **Evaluation.** Listener study: 20 prompts per chain × 3 chains, 4 audio files each, 5 participants per set, **1,200 evaluations**.
- **Assumption that blocks transfer.** *"while we demonstrate with equalization and reverberation, any differentiable audio effect may be controlled."* REAPER VSTs are not differentiable. Text2FX is therefore a *design reference* (text as a control modality over a parameter space), not an implementable method here.
- **Decision informed.** If we ever add text→parameter control, it must be search-based (B14) or model-based, not gradient-based through the plugin.

#### B17. Comunità, Steinmetz, Reiss, *NablAFx: A Framework for Differentiable Black-box and Gray-box Modeling of Audio Effects*, arXiv 2502.11668 — [FULL-TEXT]

- **What it does.** PyTorch/Lightning framework for configuring, training and comparing black-box (TCN, S4, etc.) and gray-box (DDSP blocks + controllers) effect models; includes ESR-style frequency/phase response measurement and time-varying conditioning methods (TFiLM, TTFiLM, TVFiLM).
- **Why it matters to us.** It is the *tooling* for "model this specific device from dry/wet pairs". It is not a way to control an arbitrary plugin. The gray-box block library (EQ, compressor with attack/release, distortion, fuzz) is a vocabulary we could use to *describe* a chain, not to execute it in REAPER.
- **Decision informed.** If we ever build "calibrate a model of this pedal/amp so we can optimize it fast", NablAFx is the reference implementation; the model must be trained per device, which is the training-data dependency I flag in §5.

#### B18. Comunità, Steinmetz, Reiss, *Differentiable Black-box and Gray-box Modeling of Nonlinear Audio Effects* (arXiv 2502.14405; Frontiers 2025) — [FULL-TEXT, partial]

- **What it does.** Large-scale comparison of black-box vs gray-box models across many nonlinear devices; introduces time-varying gray-box models (compressor, distortion, fuzz); releases **ToneTwist AFx**, *"the first open to community contributions"* dataset; extensive subjective evaluation.
- **The problem statement (quoted).** *"most publications experiment with one type of nonlinear audio effect and a very small number of devices"* — i.e. the field's evidence base is narrow.
- **Decision informed.** Supports the claim that grey-box models are more interpretable at similar accuracy, and that any per-device model needs that device's data. Confirms "no universal plugin model exists".

#### B19. Wright & Välimäki, *Grey-Box Modelling of Dynamic Range Compression*, DAFx20in22, 2022 — [ABSTRACT-ONLY]

- **What it does (quoted).** Uses the *structure* of a traditional digital compressor (static gain computer, level detector, make-up gain) inside a trainable model, so it can be trained end-to-end from recordings of a target device while staying interpretable; complexity is adjustable.
- **Reported result (quoted).** *"similar accuracy to a black-box baseline, whilst requiring less than 10 % of the number of operations per sample at runtime."*
- **Decision informed.** A compressor is the one FX class where a *structured* model with few parameters is both accurate and cheap. If we optimize compression parameters in REAPER, a 4–6 parameter structured search is defensible; a generic 40-parameter CMA-ES over a compressor is not.

#### B20. Hayes et al., *A review of differentiable digital signal processing for music and speech synthesis*, Frontiers in Signal Processing 3:1284100, published 11 Jan 2024 — [FULL-TEXT]

- **What it does.** Surveys DDSP: which DSP operations have differentiable implementations, applications (performance rendering, sound matching, voice transformation), and open challenges.
- **The sentence I rely on (quoted).** It highlights *"open challenges, including optimisation pathologies, robustness to real-world conditions, and design trade-offs"*.
- **Decision informed.** "Optimization pathologies" is the review-level admission that gradient/search optimization of audio processors does not reliably converge to musically sensible settings. That is the main reason my FX recommendation is *bounded search + explicit priors + human/model choice of topology*, not "optimize everything".

#### B21. Lee et al., *GRAFX: An Open-Source Library for Audio Processing Graphs in PyTorch*, arXiv 2408.03204 — [FULL-TEXT] + [REPO-CODE]

- **What it does.** Represents an effect graph as a DAG of differentiable processors with typed edges, batches *nodes of the same type* for GPU parallelism, supports changing the graph between optimization steps, and exposes graph tensors for GNNs.
- **Reported speed.** Single RTX 3090; the optimal schedule needs **11.8 processor calls on average** versus 12.6 (beam-32), 16.4 (greedy) and **77.6 (one-by-one)**.
- **Constraints (quoted).** *"We allow any graph except those with cycles, as feedback loops are hard to resolve"*; processors are the library's own differentiable blocks; default stereo.
- **Decision informed.** Graph *topology* is a first-class object and can be optimized — but only over processors you implemented. REAPER's routing graph is arbitrary and its plugins are opaque, so GRAFX informs our *representation* of a chain (nodes/edges/series-vs-parallel) and not our execution.

#### B22. Koo, Paik, Lee, *End-to-end Music Remastering System Using Self-supervised and Adversarial Training*, arXiv 2202.08520, 2022 — [ABSTRACT-ONLY]

- **What it does.** Learns a direct mastering-style transform from released pop songs with self-supervision plus a projection discriminator; validated with objective metrics and a subjective test.
- **Relevance.** It is a *direct transformation* (audio → audio), i.e. it does not produce interpretable parameter changes and cannot be applied "through" a REAPER chain. Also pop-only training.
- **Decision informed.** Direct-transform models are incompatible with the project's "edits must be visible, reversible, native" requirement. Cite as a rejected alternative.

#### B23. Yeh et al., *DDSP Guitar Amp: Interpretable Guitar Amplifier Modeling*, arXiv 2408.11405, 2024 — [ABSTRACT-ONLY]

- **What it does.** Models preamp, tone stack, power amp and output transformer with DSP-inspired differentiable blocks; claims performance comparable to black-box baselines at **< 10 % of the operations per sample**, enabling real-time use. Licence CC BY 4.0 (per arXiv page).
- **Relevance to the owner's project.** Four distorted guitar tracks are amp prints. A structured amp model is the most plausible way to get an *interpretable* parameterization of "the guitar sound" — but it models the amp, not the whole chain (no cab/mic/IR, no pedalboard).
- **Decision informed.** If we ever want "guitar tone knobs" that mean something, this is the shape; it is a research dependency, not a shipped capability.

#### B24. ToneTwist AFx Dataset — [DATASET-CARD] + [REPO-CODE]

- **What it is.** Dry/wet pairs for FX research: 4 categories (analog, analog-parametric, digital, digital-parametric), controls quantised 0–10, WAV 48 kHz/32-bit float, synchronisation impulses at start and end of every file. Dry inputs: IDMT-SMT-Guitar sets 2 and 4 (7:23, 6:08, 5:14), IDMT-SMT-Bass (5:58), Neural Amp Modeler audio (3:11), private guitar (5:19), YouTube bass (10:09) — roughly **43 minutes** of dry source material.
- **Licence.** Repo **MIT**; **but** the audio provenance includes IDMT datasets and YouTube recordings, so the MIT grant cannot be assumed to cover the audio itself. **Flag: audio licensing unresolved; treat as research-only until checked.** [INFERENCE from the dataset card's own provenance list]
- **Decision informed.** This is the concrete meaning of "training-data-dependent": to optimize a *specific* pedal you need its wet/dry pairs; the dataset gives you maybe a dozen devices, not "arbitrary REAPER VSTs".

#### B25. Yu, Martínez-Ramírez, Koo, Liao, Mitsufuji, Fazekas, *Improving Inference-Time Optimisation for Vocal Effects Style Transfer with a Gaussian Prior*, arXiv 2505.11315, 2025 — [ABSTRACT-ONLY]

- **What it does.** Adds a **Gaussian prior over the parameter space**, derived from the DiffVox vocal-preset dataset, to ST-ITO — making the objective a MAP estimate rather than a pure embedding distance.
- **Why it is the most important negative evidence in the FX cluster (quoted).** ST-ITO *"treats all possible configurations equally and relies solely on the embedding space, which can result in unrealistic configurations or biased outcomes."* The fix *"reduces the parameter mean squared error by up to 33 %"* and improves subjective preference with 16 participants.
- **Decision informed.** A pure "make it sound like the reference" search will happily find settings no engineer would use, that nonetheless score well. Any FX search we ship must carry a **prior or a plausibility constraint** (parameter ranges, monotone expectations, "don't boost 12 dB at 40 Hz"), and must report that the found setting is *one* member of an equivalence class.

### 2.5 Panning and subgrouping

#### B26. Perez-Gonzalez & Reiss, *A Real-Time Semiautonomous Audio Panning System for Music Mixing*, EURASIP JASP 2010 — [FULL-TEXT, abstract + intro]

- **What it does.** Real-time stereo panning using spectral decomposition, constraint rules and cross-adaptive algorithms; places sources without knowledge of their true locations.
- **Evaluation (quoted).** A subjective test: *"the automatic panning technique performed better than a nonexpert and showed no significant statistical difference to the performance of a professional mixing engineer."*
- **Failure cases / assumptions.** The paper explicitly says it does *not* emulate artistic/subjective decisions; it encodes "common practices and technical constraints". It assumes sources are unidentified and interchangeable — the opposite of a production where four guitars are deliberately hard-panned as an arrangement decision.
- **Decision informed.** Automatic panning is a *fallback for unidentified sources*, not a default. For our project, pan is an intentional arrangement property; a panner that "spreads similar spectra" would destroy double-tracked hard-panned guitars. Expose panning as an explicit operation with a *protected* set.

#### B27. Perez-Gonzalez & Reiss, *Automatic Mixing: Live Downmixing Stereo Panner*, DAFx 2007 — [FULL-TEXT, partial]

- **What it does.** Discrete panning steps by filter-bank spectral similarity + priority channels; keeps low-frequency sources unpanned; aims to minimise spectral masking by separating similar spectra.
- **Concrete behaviour quoted.** A drum kit signal *"manages to converge into central position (PF=0.5)"* because its energy is ambiguous between hi-hat and kick; sinusoids below 200 Hz are kept centred and summed.
- **Decision informed.** Two rules worth stealing as *evidence*, not as policy: (i) low-frequency content should not be hard-panned (mono compatibility / distortion), (ii) sources with near-identical spectra competing in the same band are candidates for separation. Both are measurable with our existing band data; neither should be applied without the model choosing it.

#### B28. Ronan, Gunes, Reiss, *Analysis of the subgrouping practices of professional mix engineers*, AES 142nd Convention, Paper 9700, 2017 — [FULL-TEXT, partial]

- **What it does.** Questionnaire + inductive thematic analysis of professional subgrouping practice; produces rules/constraints intended for intelligent tools.
- **Findings quoted/near-quoted.** Rank order of mix process: **1 panning, 2 subgrouping/EQ/DRC, 3 loudness/level** (Table 2). DRC is referenced **31 times** and EQ **10 times** in the responses. Practitioner statements: *"The more compression required, the more subgroups necessary"*; *"Many genres of music need subgroups, it's not the genre, but the amount of information in the mix."* Ten respondents.
- **Decision informed.** Subgrouping is driven by *processing need and information density*, not by instrument labels or genre. That directly contradicts any `role → bus` hardcode and supports deriving bus structure from measured needs (how many sources share a compressor, how many need the same EQ). It also justifies the project's refusal of a genre recipe.

### 2.6 Adjacent, transferable-only

#### B29. Liu, Mourgela, Ai, Reiss, *An automatic mixing speech enhancement system for multi-track audio*, arXiv 2404.17821, 2024 — [ABSTRACT-ONLY]

- **What it does.** Iterative **Harmony search** over level balance, EQ, DRC and spatialization to minimise **PEAQ**-estimated masking across simultaneous speakers.
- **Transferable idea.** A metaheuristic over a *small* number of global mix parameters with a perceptual objective and a subjective check. **Not transferable:** PEAQ as the objective (it is a codec-quality model for speech), and speech stems as the material (noise-like, no distortion, different masking regime).
- **Decision informed.** Confirms the "small parameter count + perceptual objective + listening test" pattern; does not justify PEAQ in our pipeline.

#### B30. Demerlé, Esling, Doras, Genova, *Combining audio control and style transfer using latent diffusion*, arXiv 2408.00196, 2024 — [FULL-TEXT]

- **What it does.** Diffusion autoencoder with disentangled "structure" (time-varying: melody, loudness) and "timbre" (global: timbre, style, genre) spaces; can render MIDI or an audio example with a target timbre; claims faster-than-real-time inference on an A5000 GPU.
- **Relevance.** This is *generation*, not mixing: it produces new audio, not parameter changes, and cannot be verified against a REAPER project state. Its "structure vs timbre" split is conceptually useful for describing what a reference contributes.
- **Decision informed.** Reject for the action path; keep the vocabulary (global style vs local structure) for how we describe a reference in context.

---

## 3. (b) Current-project capability / gap map

Status legend: **in code** (implementation exists), **MCP** (reachable through a tool), **offline-tested** (unit tests assert behaviour), **live-tested** (exercised against a running REAPER), **E2E-accepted** (a user goal was demonstrated end to end with listening/acceptance).

| # | Requirement | Current implementation (path:line) | Verified in tests? | Gap | Evidence |
|---|---|---|---|---|---|
| 1 | Onset evidence at fine resolution | `analysis/events.py:82-190` — 2 ms hop, 4 ms window, 5 dB rise out of a 3-frame dip, 12 ms refractory, −70 dB floor; duration to −15 dB capped at 150 ms (`:126-137`) | Offline: yes (`tests/test_phase_robust_analysis.py` exercises `detect_events`; `tests/test_edit_map.py` exercises the clustering built on it) | Rule-based, single-band envelope; no spectral or harmonic criterion; clip event is one merged event (`:163-166`) | [PROJECT src/reaper_audio/analysis/events.py:13-14, 103-166] |
| 2 | Independent transient evidence | `analysis/transients.py:40-119` — STFT flux, 2048/256, median+MAD threshold, min gap 35 ms | Offline: yes | **Events are discarded**: `metrics.py:300` computes them, but only count/density reach the report (`metrics.py:364-365`); only `temporal.transients_stats` returns them (`temporal.py:761-768`) | [PROJECT metrics.py:300, 364-365; temporal.py:761-768] |
| 3 | Musical event record with confidence and usage licence | `analysis/event_model.py` — `SignalEvent`, `PerformanceOccurrence`, `PerformanceEvent`, `PerceivedGesture`, `ScoreObject`, `UsageScope`, `Confidence`, `MethodRef`, `EvidenceRef` | Offline: yes (`tests/test_ids_event_model.py`) | **Zero consumers.** No analyzer constructs them; no editor reads `usage_scope`. They are a contract island | [PROJECT event_model.py:161-196, 451-692; `grep` returns no non-`event_model` hits] |
| 4 | Local tempo with metrical alternatives | `analysis/tempo.py:8-127` — 8 s window, IOR quantiles × {0.25,1/3,0.5,1,2,3,4}, score 0.55 phase coherence + 0.30 timing consistency + 0.15 evidence; alternatives within 0.04 | Offline: yes (`tests/test_tempo.py`) | Confidence is a heuristic score, not calibrated; needs ≥3 onsets in the window; no beat/downbeat, no meter | [PROJECT tempo.py:66-99] |
| 5 | Grid hypothesis vs performed timing | `edit_map.tempo_grid` builds a piecewise-constant beat grid from tempo points, defaulting to **120 BPM** (`edit_map.py:91-115`) | Offline: yes (`tests/test_edit_map.py:18`) | 120 BPM fallback is a genre-neutral but musically arbitrary prior; grid is *beats only* (no downbeat, no meter grouping) | [PROJECT edit_map.py:96-99] |
| 6 | Cut-candidate clustering across sources | `edit_map.py:65-88` — 30 ms tolerance; confidence 0.55 agreement + 0.30 strength + 0.15 low-band | Offline: yes | At 300 BPM a 16th is 50 ms and a 32nd 25 ms, so 30 ms merges adjacent fast notes | [PROJECT edit_map.py:27, 80-88] |
| 7 | Evidence-only edit map (no decision) | `edit_map_report.build_edit_map_from_report` with `decide=False` → `suggested="unreviewed"`; tails from the 75th percentile of `dur_ms`, clipped 20–500 ms | Offline: yes (`tests/test_edit_map_report.py`) | Tail is estimated from *detector durations*, not from measured decay (`tail.estimate_tail` exists but is not used here) | [PROJECT edit_map_report.py:56-100, 120-150] |
| 8 | Seam measurement | `analysis/seam.py:61-90` — level delta, `max_sample_step` over 64 samples each side, spectral flux over 50 ms | Offline: yes (`tests/test_seam.py`) | `max_sample_step` is HF-content dependent, not a click detector: a full-scale 1 kHz tone gives 0.1425/sample and 5 kHz 0.7124/sample at 44.1 kHz (arithmetic, §2.4 note) | [PROJECT seam.py:72-84; arithmetic] |
| 9 | Seam search over real geometry | `analysis/seam_search.py:73-210` — 5×5×5 = 125 variants; score = \|Δlevel\|/6 + step/0.1 + flux; tail bounds `max_left_extend` | Offline: yes; geometry test asserts overlap 5 ms for tail 130/move 100/trim 25 (`test_seam_search.py:42-49`) | Score weights and normalisers are uncalibrated; mono/stereo metrics differ in kind; no per-note model | [PROJECT seam_search.py:145-184] |
| 10 | Split with seam parameters | `daw/editing.py:27-93` → `EditOperation("item.split")`; cap **200** cuts (`editing.py:15`) while `session.plan` allows **256** (`session.py:1675`) | Offline: yes (`tests/test_editing.py:36-95`) | Two different caps; no edge-reveal operation; `right_trim_s` moves rather than reveals | [PROJECT editing.py:15, 80-92; session.py:1675] |
| 11 | Native split semantics | `scripts/reaper_bridge.lua:3504-3556` — `SplitMediaItem`, extend left by `tail_s`, move right by `move_s + right_trim_s`, set `D_FADEOUTLEN`/`D_FADEINLEN` to `min(overlap or fade, …)` | Offline: bridge contract tests only cover capture/digest (`tests/test_wave_b_bridge_contract.py`); **no Lua-level test** | **`D_STARTOFFS` untouched**; fade length = full overlap, so a 69 ms overlap yields a 69 ms fade; fade *shape* is REAPER's default, not the linear/equal-power curve the preview assumes | [PROJECT reaper_bridge.lua:3504-3556] |
| 12 | Post-edit readback of geometry | `read_edit_state_after` for `item.split` returns **`{item_count}` only** (`reaper_bridge.lua:3046-3049`) | Not tested | No position/length/offset/fade readback, so "verify afterward" is impossible for splits today | [PROJECT reaper_bridge.lua:3029-3052] |
| 13 | Move without stretch | `daw/align.py:182-234` — `item.set position_s` only; refuses before zero | Offline: yes (`tests/test_align.py:89-98`) | Correct, but no group/folder handling and no phase re-check after the move | [PROJECT align.py:182-234] |
| 14 | Reference-take alignment | `daw/align.py:82-125` — per-attack cross-correlation, median lag, MAD, drift slope + stderr, `alignment_report` refuses/decides only with a policy | Offline: yes (`tests/test_align.py:35-171`) | 50 ms search window default; mono only (`_mono`, `:77-79`); no stereo-phase verification | [PROJECT align.py:82-125, 237-265] |
| 15 | Grid quantization | `daw/alignment.py:12-31` — `move = (grid − time) × strength`, `plan_for_group` filters by explicit group | Offline: yes (`tests/test_alignment.py`) | **Conflicts with TZ §5**: it quantizes to the project grid, which the owner's case forbids; name collides with #14 | [PROJECT alignment.py:1-6, 12-31; docs/TZ.md §5] |
| 16 | Stretch marker write | `item.set stretch_markers` → `SetTakeStretchMarker` (`reaper_bridge.lua:2301-2320`); `stretch_ratio` validated 0.5–2.0 (`:2293-2296`) | Bridge contract: not covered | Round-trip readback of markers is only `GetTakeNumStretchMarkers` (count, `:2889-2890`), not positions | [PROJECT reaper_bridge.lua:2301-2320, 2886-2890] |
| 17 | Item context exposure | Lua `item_context` carries `start_offset_s`, `playrate`, `stretch_marker_positions`, `group_id`, `channel_mode`, `beat_attach_mode`, `source_length`+qN flag (`reaper_bridge.lua:347-430`) | Indirect (snapshot tests) | **Python never reads `start_offset_s`/`playrate`/stretch markers** — `grep` over `src/` yields zero hits | [PROJECT reaper_bridge.lua:347-430; grep] |
| 18 | Whole-project view for the model | `context/panorama.py:178-260` item/take views keep only `item_id/guid/track_id/position_s/length_s/take_count/active_take_id` (+take name, is_midi, source path) | Offline: yes (`tests/test_capture_panorama.py`) | The compact view **drops** offsets, playrate, stretch markers, fades, groups — exactly the fields edits need. Raw `reaper_project(include_items=True)` still has them | [PROJECT panorama.py:178-222, 224-260; mcp_server.py:584-632] |
| 19 | Transaction: preview/apply/undo | `daw/session.py:1671-1776`; Lua `transaction_preview/apply/undo` (`reaper_bridge.lua:3126-3260, 3717-3884`); plan TTL 900 s, max 128 plans (`:43-47`) | Offline: yes (`tests/test_daw_session.py:525-595`) | Apply verifies only that REAPER returned a valid result; per-op post-state is thin (see #12); rollback relies on the undo label matching; `EditPlan.revision` is legacy `ProjectRevision`, not `RevisionRef` | [PROJECT session.py:41-80, 1740-1776; reaper_bridge.lua:3717-3846] |
| 20 | Typed operation surface | 21 MCP tools (`grep -c "@server.tool()"` → 21; `tests/test_mcp_server.py:110` asserts 21); 6 resource endpoints | Offline: yes | Decision ledger D22 still says 19 — **stale**; R3/round-4 prose says 19 | [PROJECT mcp_server.py; test_mcp_server.py:110; DECISION-LEDGER.md D22] |
| 21 | FX parameter calibration | `daw/eq.py:140-169` (normalised↔displayed piecewise map by sampling the plugin's own formatted values), `daw/limiter.py:100-167` (probe low/high, bisect ≤9 steps, ≤3 ceiling passes) | Offline: yes (`tests/test_eq.py`, `test_limiter.py`) | This is the *existing* model for FX optimization: small, model-based, render-verified. It is not generalized to arbitrary parameters | [PROJECT eq.py:140-169; limiter.py:100-167] |
| 22 | EQ cycle with measured accept/reject | `daw/cycle.py:1-12` — corpus deltas → one move at a time → render → measure distance → keep only on improvement | Offline: yes | Tied to corpus distance; requires a corpus; not usable for reference-free work | [PROJECT cycle.py:1-12] |
| 23 | Panning | `track.set pan` via `EditOperation` (`reaper_bridge.lua:2867-2880`), context reports pan | Offline: yes; audited in Wave C | No pan law / pan mode read (`I_PANMODE`), no mono-compatibility check tied to pan moves, no protected-pan concept | [PROJECT reaper_bridge.lua:2867-2880; DOMAIN-ACCEPTANCE-MATRIX] |
| 24 | Grouping / buses | `track.create`, `send.create`, `track.set main_send` | Offline: yes; HiMMP run created 5 buses + 2 returns (HB-002) | No folder-depth operation in the typed set; no "subgroup by measured need" evidence | [PROJECT reaper_bridge.lua:2964-2975; HERMES-MIXING-BEHAVIOR-LEDGER.md HB-002] |
| 25 | End-to-end edit acceptance | `docs/EDITING-ACCEPTANCE.md` defines ORIG | **Not achieved** — no ORIG fixture in the repo, no accepted run | The acceptance case is a document, not a test | [PROJECT docs/EDITING-ACCEPTANCE.md:1-48] |

**Three summary judgements.**

1. The *measurement* layer is broader than the *action* layer. We can detect, cluster, score seams and align — but the operations that would realize the owner's case (reveal an edge, verify geometry) do not exist.
2. The *types* are ahead of the *wiring*. `event_model.py` is a well-designed contract with no producers or consumers; `UsageScope` cannot currently stop anything.
3. The *live* evidence is thin exactly where B's scope lives. Wave C proved capture/digest/master-render equality; the only live edit evidence is "a revision-bound item split changed the item count and undo restored it" (`docs/VALIDATION.md`), which is not a musical acceptance.

---

## 4. (c) Event / action / context contract

### 4.1 The intermediate representation

The requirement is an IR that (a) is produced by measurement, (b) carries its own uncertainty and alternatives, (c) can be *consumed by an editor without re-detecting anything*, and (d) never asserts a musical role it cannot support.

**Layer 0 — Source map (missing today; the keystone).** For each `(item, active take)` a single affine-or-piecewise map:

```
SourceMap:
  item_guid, take_guid, source_id (ast_sha256 or media path hash)
  position_s            # project time of item start
  length_s              # project duration
  start_offset_s        # D_STARTOFFS, source seconds
  playrate              # D_PLAYRATE
  stretch_markers: [{pos_s, srcpos_s}]   # project seconds, source seconds
  loop_source: bool
  beat_attach_mode, auto_stretch, channel_mode, group_id
  mapping_status: verified | approx | unverified | unsupported
```

With no stretch markers and no loop, `src(t) = start_offset_s + (t − position_s) × playrate` and the item covers `[start_offset_s, start_offset_s + length_s × playrate]`. This is the formula `docs/EDITING-ACCEPTANCE.md:31-33` uses for "pull the edges", and it is the only thing that makes an edge operation computable. The data exists in Lua (`reaper_bridge.lua:347-430`) and is dropped by every Python consumer.

**Layer 1 — Signal events.** `SignalEvent` as already defined: `evt_`, `ast_sha256` asset, `Interval` with `anchor ∈ {attack_start, energy_peak, detector_frame, window_bounds, event_boundary}` (`timebase.py:87-95`), `kind ∈ {onset, transient, spike, clip_run, silence, decay}`, `strength`, `MethodRef`, `status`.

**Layer 2 — Performance occurrences and events.** `PerformanceOccurrence` (`occ_`, item/take GUID, `loop_iteration`, `source_interval`) and `PerformanceEvent` (`prf_`, kind ∈ `pick_attack|drum_strike|note|chord`, pitch + alternatives, `EventDuration`, `UsageScope`, `Confidence`) as defined. The occurrence indirection is essential: the same take can appear in several items and several loop iterations, and an event's *source* time must not be confused with its *project* time.

**Layer 3 — Time hypotheses.** Missing types:
- `TempoHypothesis` — `{bpm_curve, alternatives, method, confidence, evidence_ids}`, matching what `tempo.py` already returns per cell.
- `GridHypothesis` — `{tempo_hypothesis_id, meter: {numerator, denominator, grouping}, phase_offset_qn, subdivision, confidence}`. This does not exist; `tempo_grid` returns bare beat times.
- `SectionHypothesis` — `{interval, repeat_of: section_id | null, similarity, boundary_confidence}`. `edit_map._sections` computes Foote novelty (`edit_map.py:161-193`) but exposes only boundaries, no repeat relation — so "same riff, different take" cannot be expressed.

**Layer 4 — Roles and chains.** `RoleHypothesis` (`{track_guid, role, evidence, alternatives}`) and `ChainState` (`{track_guid, ordered fx with parameter identity, units, normalized value, calibrated real value}`). Today `edit_map._group` guesses role from English words in the track name (`edit_map.py:196-207`) and the MCP path bypasses it entirely with `source_groups` (`mcp_server.py:840-845`). The owner can only give simple track names, so name-based roles will be wrong; roles must be *hypotheses with evidence*, confirmable by a question.

#### 4.1.1 A compact schema fragment

Not a storage format — the minimum field set that makes the contract checkable. Field names follow the existing contracts where they exist.

```jsonc
{
  "source_map": {                       // NEW: the keystone (daw/source_map.py)
    "item_guid": "{...}", "take_guid": "{...}",
    "source_id": "ast_sha256_<64hex>",  // or a media path hash when not captured
    "position_s": 10.0, "length_s": 2.0,
    "start_offset_s": 0.5, "playrate": 1.0,
    "stretch_markers": [],              // [{pos_s, srcpos_s}] — non-empty => piecewise
    "loop_source": false, "beat_attach_mode": 0, "auto_stretch": false,
    "channel_mode": 0, "group_id": 0,
    "mapping_status": "verified",       // verified|approx|unverified|unsupported
    "derivation": "reaper_item_context_v1"
  },
  "signal_event": {                     // EXISTS (event_model.SignalEvent)
    "event_id": "evt_...", "asset_id": "ast_sha256_...",
    "interval": {"start": {"basis":"project_s","value":10.5,"unit":"s",
                           "uncertainty_ms": 2.0, "mapping_status":"verified"},
                 "end":   {"basis":"project_s","value":10.65,"unit":"s",
                           "uncertainty_ms": 15.0, "mapping_status":"verified"},
                 "anchor": "attack_start"},
    "kind": "onset", "strength": 6.2,
    "method": {"name":"events.detect_events","version":"1","params_hash":"..."},
    "status": "candidate", "occurrence_id": "occ_..."
  },
  "performance_event": {                // EXISTS (event_model.PerformanceEvent)
    "event_id": "prf_...", "occurrence_id": "occ_...", "kind": "pick_attack",
    "pitch": null, "duration": {"start_s":10.5,"end_s":10.9},
    "usage_scope": {"timing": true, "pitch": false, "duration": true,
                    "midi": false, "destructive_cut": true},
    "confidence": {"basis": "agreement", "value": 0.86, "n": 3,
                   "calibration_id": null},
    "alternatives": ["attack may be a cymbal bleed artefact"],
    "evidence_ids": ["evt_...", "evt_...", "sco_..."],
    "status": "reviewed"
  },
  "grid_hypothesis": {                  // NEW
    "grid_id": "grd_...", "tempo_hypothesis_id": "tmp_...",
    "meter": {"numerator": 3, "denominator": 4, "grouping": [3]},
    "phase_offset_qn": 0.0, "subdivision": 4,
    "confidence": {"basis": "rank", "value": 0.71, "n": 1},
    "alternatives": [{"meter": {"numerator": 6, "denominator": 8}, "score": 0.68}]
  },
  "edit_plan": {                        // EXISTS (daw/session.EditPlan) + NEW field
    "revision": {...}, "plan_id": "...", "reason": "...",
    "operations": [{"kind": "item.edge", "target_guid": "{...}",
                    "values": {"edge": "left", "delta_s": -0.030, "fade_s": 0.005}}],
    "expected_after": [{"position_s": 9.970, "length_s": 2.030,
                        "start_offset_s": 0.470, "fade_in_s": 0.005}]
  }
}
```


### 4.2 How the four evidence sources must interact

| Evidence | Supports | Contradicts | Required behaviour |
|---|---|---|---|
| Track audio (captured post-FX stem) | Onset/transient existence, level, spectral band, tail length | Another track's onset at the same time (double-track flam), or the same track's MIDI | If two guitar tracks disagree by > tolerance, mark `contested`; never average them into one "guitar onset" |
| Project tempo map | Bar/beat numbering, target grid positions, stretch-marker placement | Performed timing | Tempo map is a **prior for intent**, never evidence of performance (decision D12) |
| MIDI in project | Note pitch, intended onset, intended duration, `origin=project_midi` | The recorded performance | MIDI proves *project state*; it does not prove a heard note (D25). A MIDI note with no audio onset is a candidate for "not played"; an audio onset with no MIDI note is a candidate for "not written" |
| Cross-track alignment | Common attack identity, drift, phase relation | Single-track detector noise | Only when the same transient is matched by correlation with a reported confidence (as `align.estimate_alignment` does) |

**Rule:** a `PerformanceEvent` may only be promoted to `reviewed` when at least two of {audio onset, MIDI note, cross-track match} agree within the declared tolerance, and the *disagreement* must be recorded, not resolved silently.

#### 4.2.1 What confidence is allowed to mean

`event_model.Confidence` already restricts `basis` to `{interval, rank, agreement, none}` (`event_model.py:27`). That is a better design than a bare float, and it should be made operational rather than decorative:

| `basis` | What the number means | May authorize |
|---|---|---|
| `interval` | A normalised margin inside a measured interval (e.g. detector strength above threshold, `/denom` in `transients.py:94-99`) | Nothing destructive by itself; ordering and review |
| `rank` | Relative ordering only (this event is more likely than that one) | Display, triage, choosing which candidate to audition first |
| `agreement` | Fraction of independent methods/sources agreeing, with `n` recorded | **Only this basis may clear a destructive edit**, and only with `n ≥ 2` |
| `none` | Unknown / not calibrated | Nothing; must be treated as absent |

Two consequences the code does not yet enforce. First, `transients.py` computes `confidence = clip((strength − threshold)/(p99.5 − threshold))` — that is an `interval` basis, but the dict carries no basis at all, so a consumer cannot tell. Second, `edit_map._confidence` mixes source agreement, level strength and low-band share into one score (`edit_map.py:80-88`), which is a *composite* with no stated basis; it should be split into `agreement` (sources) and `interval` (strength) so the destructive gate can use the first and ignore the second.

Proposed gate (needs the owner's signature, ledger P05/P06 are still proposals): `destructive_cut` requires `basis="agreement"`, `n ≥ 2`, `value ≥ 0.8`, **or** an explicit human/model instruction naming the event. The 0.8 is not calibrated; it is a placeholder chosen so that two-of-two full agreement clears it and one-of-two does not.



### 4.3 Pre-inspect / post-verify contract per operation class

Every row below is a *general* operation. None is a genre recipe.

#### A. Cut / split (`item.split`)

- **Before.** `SourceMap` for the target item (`mapping_status` must be `verified` for exact work); the containing item and its neighbours; the confirmed performance event(s) at the cut and their `UsageScope` (`destructive_cut` must be true); the measured tail at the onset (`tail.estimate_tail`, drop chosen by the task); the cross-track agreement at that time; group membership (`group_id`) and whether other items in the group must move.
- **Parameters.** `position_s` (strictly inside the item — enforced at `reaper_bridge.lua:2936-2940`), `tail_s` (left extension), `move_s` (right item offset), `right_trim_s` (**not** a source reveal — see B), `fade_s`.
- **After (missing today).** Read back, per sample: left item `position_s`, `length_s`, end fade length; right item `position_s`, `length_s`, `start_offset_s`, fade-in length; item count; the new item's GUID; and that `D_STARTOFFS` of the left item is unchanged. Then render the seam region and compute level delta / step / flux **against the same region before the edit**.
- **Refuse when.** The item has stretch markers or `playrate ≠ 1` and the caller asked for an edge reveal; the source has insufficient material (`start_offset_s − Δ × playrate < 0` or beyond source length); the cut lands inside another event's sustain without an explicit instruction; the group has locked/synced items whose relative offset would change.

#### B. Edge reveal / trim (new `item.edge`)

This is the operation the ORIG case actually needs, and it does not exist.

- **Left edge out by Δ** (project seconds): `position_s −= Δ`, `length_s += Δ`, `start_offset_s −= Δ × playrate`. The already-placed attack stays at the same project time; new source material `[s − Δr, s]` becomes audible.
- **Left edge in by Δ**: the inverse; refuse if `Δ ≥ length_s` or the resulting offset exceeds source bounds.
- **Right edge out by Δ**: `length_s += Δ`; reveals `[s + Lr, s + (L+Δ)r]`.
- **Right edge in by Δ**: `length_s −= Δ`; must not cut the last confirmed event's tail.
- **After.** Exact readback of all four numbers plus fades, and a render check that the **previously placed attack** is still at the same project sample (the ORIG invariant).
- **Why not `right_trim_s`.** Moving the right item later by `right_trim_s` while leaving `D_STARTOFFS` alone *delays* the right slice's content by `right_trim_s`; it does not open the source. Confirmed at `reaper_bridge.lua:3528-3536` (only `D_POSITION` is set). `docs/EDITING-ACCEPTANCE.md:35` states the same limitation as a requirement to fix.

#### Worked arithmetic on the ORIG geometry

The owner's saved example has overlaps of 69, 27 and 22 ms (`docs/EDITING-ACCEPTANCE.md:9`). Take a cut at project 10.000 s with `tail_s` chosen from the measured tail:

| Intent | Current parameters | Actual result | Musical consequence |
|---|---|---|---|
| 69 ms overlap on a continuous take | `tail_s=0.069, move_s=0, right_trim_s=0` | left end 10.069, right start 10.000, overlap 69 ms, both fades = 69 ms | Correct geometry; the fade *curve* is REAPER's default, not the linear complementary curve the preview models |
| Shorten the overlap to 5 ms by delaying the right slice | `tail_s=0.130, move_s=0.100, right_trim_s=0.025` | right start 10.125, left end 10.130, overlap 5 ms (asserted in `tests/test_seam_search.py:42-49`) | The right slice's content is now **125 ms late** relative to the cut; the overlap was changed by changing timing |
| Reveal 30 ms of the right slice's past, keep the attack where it is | no parameter combination achieves it | `right_trim_s=0.030` gives right start 10.030, overlap 39 ms, and every attack inside the right slice moves **30 ms late** | This is the case the ORIG edit actually needs, and the primitive cannot express it |

The third row is the whole argument for `item.edge`: at playrate 1, revealing 30 ms means `position_s = 9.970`, `length_s = 2.030`, `start_offset_s = 0.470`. Two of those three numbers are reachable today (`item.set` writes position and length); the third is not writable at all — `D_STARTOFFS` appears in the bridge in exactly three places: read in the snapshot (`reaper_bridge.lua:381`, `:754`), and **copied** from the source take in `item.duplicate` (`:3582-3585`). No transaction operation can set it to a new value. Also note that `cut_plan` merges `move_s` and `right_trim_s` into one operation (`editing.py:84-92`), so a *musical* move and a *seam* adjustment cannot be planned separately even though they have different evidence and different verification.



#### C. Move (`item.set position_s`)

- **Before.** `SourceMap`, group membership, whether the item is the reference or the target of an alignment, the measured lag and its confidence, the drift slope if the take is long.
- **After.** Readback of position; re-render both the moved item and the sum with its double-track partner; report correlation and per-band level change. A move that improves alignment but destroys the double-track relationship is a regression (comb filtering), so the *pair* is the unit of verification.
- **Refuse when.** `position + length` would cross another item's boundary in a way that creates an unintended overlap without a fade; the item is in a group with sync enabled; the shift exceeds the alignment search window (i.e. is not a "small correction").

#### D. Stretch (playrate or stretch markers)

- **Before.** `playrate`, existing stretch markers, `auto_stretch`, `beat_attach_mode`, item timebase; the tail length; whether the material is transient-dense.
- **After.** Readback of markers by *position* (not count), plus render comparison of attack count and attack positions (the TSM review's named failure modes: doubling, skipping, smearing).
- **Default posture.** Refuse unless the task explicitly asks for time modification. For the owner's case, `docs/EDITING-ACCEPTANCE.md:36` forbids it; the bridge will happily do it (`item.set stretch_ratio` accepts 0.5–2.0, `reaper_bridge.lua:2293-2296`), so the *policy* must live in the plan layer, not in the primitive.

#### E. MIDI write (`midi.create`)

- **Before.** The target track's instrument/VSTi, its existing MIDI items and PPQ grid, the tempo map in the item's range, the audio events the notes are supposed to represent, and the take's `origin`.
- **After.** Read back note count, positions (PPQ and project time), and verify each written note maps back to a project time within tolerance; `MIDI_GetProjTimeFromPPQPos` on looped items is an open empirical question (ledger E14).
- **Contract.** Writing MIDI from transcription requires `UsageScope.midi = true` and `pitch` evidence; onset-only evidence must not be allowed to write pitches.

#### F. FX insert / parameter change

- **Before.** The full chain with parameter identity, units and normalised values; `recFX=false` (the historical bug in HB-004 inserted into the record-monitoring chain and the FX were inaudible in the mix); the plugin's own min/max/real values (`TrackFX_GetParamEx` exists in `fx.list_params`); PDC of the chain; the current automation on that parameter.
- **After.** Readback of the normalised value *and* the formatted value; a render of the affected track or master over the same window; a measured delta on the construct the change was supposed to affect; and a check that no other parameter moved.
- **Refuse when.** The parameter is automated and the write would be masked by an envelope; the plugin is offline; the change would exceed a stated plausibility range without explicit instruction.

### 4.3.1 Cross-cutting invariants: timebase, PDC, groups

Three properties cut across every operation and are the usual cause of "the edit looked right and sounded wrong".

**Timebase.** An item's position can be time-based or beat-based (`C_BEATATTACHMODE`, `C_AUTOSTRETCH`; both are captured in `item_context`, `reaper_bridge.lua:388-396`, and written by `item.set`, `:2882-2883`). If any target item is beat-attached, a tempo change — including one the model makes for an unrelated reason — moves or stretches it. `docs/EDITING-ACCEPTANCE.md:16` already requires reading project/track/item timebase before the first edit and keeping the tempo fixed for that case. General rule: **a plan that changes tempo must enumerate every beat-attached item and either preview its new position or refuse.** No such check exists today; `tempo.set` applies `SetCurrentBPM`/`SetTempoTimeSigMarker` directly (`reaper_bridge.lua:3397-3470`).

**PDC (plugin delay compensation).** Item geometry is pre-FX; the audible result is post-PDC. A chain that reports latency shifts the *rendered* signal relative to the item's timeline position, and REAPER compensates by delaying other tracks — but the compensation depends on every plugin reporting correctly. Consequence for us: a time measured on `track_stem_post_fx` and a position read from the item cannot be compared without knowing the chain's effective latency. The pre-FX accessor (`take_sum_pre_fx`) and the post-FX render (`track_stem_post_fx`) are *different clocks* in this respect. The ledger records actual per-chain PDC as empirically blocked (E05); until it is measured, any edit whose justification is a post-FX onset time must state that the latency term is unknown, and destructive edits should be planned from pre-FX or item-level evidence.

**Groups.** `I_GROUPID` is captured (`reaper_bridge.lua:392-393`) but the transaction layer has no group-aware operation: a split or move targets one item GUID. Whether REAPER's group sync applies to an API-driven change depends on project group settings and is an open empirical question (E18). Rule: if the target's `group_id ≠ 0`, the plan must either operate on every member explicitly or refuse; silent partial group edits are the worst outcome because the picture and the sound diverge.

### 4.4 Preview / commit / undo model

Current semantics, precisely:

1. `reaper_edit(mode="preview")` → `session.plan()` (`session.py:1671-1707`) takes a snapshot, compares `expected_revision`, calls Lua `transaction.preview`, and caches a `plan_id` in **both** the Python process (`_edit_plans`, MCP) and the Lua process (`transaction_plans`, TTL 900 s, max 128).
2. `mode="apply"` → `session.apply(dry_run=False)` (`session.py:1740-1759`) re-checks the revision, calls `transaction.apply`, which (a) refuses an expired/unknown plan, (b) re-resolves every GUID, (c) wraps all operations in one `Undo_BeginBlock2`/`EndBlock2`, (d) on exception attempts `Undo_DoUndo2` and reports `transaction_rollback_failed` if the label does not match (`reaper_bridge.lua:3780-3820`).
3. `mode="undo"` → `session.undo(expected_revision)` (`session.py:1761-1776`) requires the *post-apply* revision and that the library's undo label is on top of the undo stack; otherwise `stale_context`.

**What is guaranteed:** one undo block per plan; stale revision rejected at preview and again at apply; object resolution by GUID at apply time; delete operations verified to be gone (`reaper_bridge.lua:3782-3790`).

**What is not guaranteed, and must be added:**

- **Revision identity is legacy.** `EditPlan.revision` is a `ProjectRevision` with only `project_id`, `change_count` and `path` (`daw/session.py:41-55, 68-80`) — it is not the `RevisionRef` with `instance_id`, `epoch`, `fingerprint` and `fingerprint_scope` that the context layer defines (decision D10). So the transaction cannot distinguish "same content" from "same counter", and it cannot tell a capture's freshness from a re-open. This is the concrete reason Workstream A's `state` field would be decorative today.
- **Geometry verification** for split/edge/move (see A/B/C).
- **Side-effect verification** for anything that changes PDC, folder structure, or routing: the digest covers tracks/items/fx/sends/tempo/master/automation (`Wave C` record), but the *transaction* does not compare the digest before/after; it compares only the change counter via `project_identity`.
- **Atomic multi-item group edits**: a "group" is `I_GROUPID`, and the transaction has no group-aware operation. `item.split` on one member of a group leaves the others untouched and REAPER's group sync may or may not apply (open question E18 in the ledger).
- **Preview fidelity**: the preview returns the *before* state and the requested operations; it does not return the *expected after* geometry. So the model cannot see, before committing, that a 69 ms overlap will produce a 69 ms fade.

**Recommended contract addition:** `EditPlan.expected_after` per operation, computed by the same code that renders the candidate (`seam_search.assemble_variant`), then compared sample-for-sample against the readback after apply. This is exactly the "same geometry" requirement in `tests/test_seam_search.py:42-49`, extended to the real item.

---

## 5. (d) Candidate methods and limits

### 5.1 Events / onset / transcription

| Method | Operating domain | Compute | Training data | Temporal resolution | Stereo/phase | Production-ready? | V100 fit |
|---|---|---|---|---|---|---|---|
| Current 2 ms envelope + rise rule (`events.py`) | Any; best on percussive | Trivial (numpy) | None | 2 ms hop / 4 ms window | Channel-averaged power, no phase sum (`events.py:19-40`) | Yes, but fragile on saturated/legato material | N/A (CPU) |
| STFT flux transients (`transients.py`) | Any | Small (scipy STFT) | None | 5.8 ms at 44.1 k (hop 256) | Per-channel power averaged, anti-phase safe (`transients.py:65-74`) | Yes; **currently discarded** | N/A |
| Spectral-sparsity ODF (Mounir 2021) | Polyphonic, repeated notes, vibrato | Small | None | Frame hop (not verified) | Mono assumed in the paper | Research; CPU | N/A |
| Rhythmic decoding (Degara 2011) | Polyphonic mixtures | Small + tempo estimator | None | Frame hop; 50 ms evaluation tolerance | Not addressed | Research; needs tempo hypothesis | N/A |
| CNN/CRNN onsets (Tomczak 2023) | Bowed/plucked strings | GPU inference | Virtuoso Strings (CC BY-NC-SA) + MIR benchmark | Frame hop | Mono | Research code; non-commercial data | Plausible, unverified |
| Efficient CNN (Gong 2018) | Instrumental + singing | GPU/CPU | Two datasets | Frame hop | Mono | Research; **cross-dataset transfer fails** | Plausible |
| Guitar tablature (Kehling 2014) | Clean electric guitar, standard tuning | CPU | IDMT-SMT-Guitar | 98.5 % onset F, 0.96 pitch F | Mono | Research; wrong domain for high gain | N/A |
| madmom RNN onsets | General | CPU (excluded) / GPU port needed | Trained models | Frame hop | Mono | Library, mature | Needs port |

**Explicit statement required by the brief:** onset-strength detectors degrade under saturated, compressed, cymbal-dense material. The mechanism is visible in our own detector: a 5 dB rise out of a 6 ms dip (`events.py:105-113`) presupposes (i) a dip, which heavy compression and sustained distortion remove, (ii) a rise that is not masked by cymbal wash, and (iii) a level above −70 dB, which dense cymbal/hi-hat energy keeps true even in gaps. The `clip` rule (`|x| ≥ 0.999`) can also fire as one merged event on a limited master. **Fallback evidence, in order:** (1) project MIDI as intent; (2) cross-track transient agreement; (3) the independent STFT-flux detector; (4) the *pre-FX / DI* capture if it exists (`take_sum_pre_fx`, but vers1 guitars are amp prints per D04); (5) explicit model/user anchors. When (1)–(4) disagree beyond tolerance, the event is `contested` and no destructive edit is planned.

### 5.2 Time-scale modification

| Method | Artifacts | Best for | Worst for | Native in REAPER? | Use in this project |
|---|---|---|---|---|---|
| OLA (short frames) | Phase jumps on harmonic content; transient doubling if frames long | Percussion | Sustained guitar | No (not exposed) | Reference only |
| WSOLA | Doubling (stretch) / skipping (compress); only dominant periodicity preserved | Monophonic, small ratios | Polyphonic, dense metal | SoundTouch-like modes exist | Only with explicit request |
| PV-TSM | Smearing, "phasiness", loss of bite | Large ratios, polyphony | Transients (our attacks) | élastique / spectral modes | Avoid for attacks |
| Transient-preserving (Nagel 2009) | Errors in detection or re-insertion | Material with clear attacks | Blast beats where attacks overlap | Some modes do this internally | Verify attack count after |
| HPS-based | Doubling masked by short frames | Mixed harmonic/percussive | Very dense spectra | No | Reference only |
| Anchor-point warp (stretch markers) | Depends on underlying TSM; marker placement errors | Non-linear timing (tempo map, rubato) | Anything with a fixed grid requirement | **Yes** (`SetTakeStretchMarker`) | The only native non-linear warp; must be read back by position |

**Decision:** for performance editing under a fixed grid (the owner's case), do not stretch. Use cut + edge reveal + move. Reserve stretch for explicitly requested time modification, and when used, verify attack count and position.

### 5.3 FX: topology vs parameters

| Approach | Needs | Transfers to arbitrary REAPER VST? | Cost | Evidence |
|---|---|---|---|---|
| Choose chain topology from context (model decision) | Project context, role hypotheses, evidence | Yes (it is a decision, not an algorithm) | ~0 compute | No paper needed; this is what engineers do (B28 shows it is the #2 step in the mix order) |
| Parameter fit with dry+wet of the same performance | Paired dry/wet, same content | Yes, by search | ~10²–10³ renders | ITO-Master's "reverse engineering works when content is identical" (B15) |
| ST-ITO-style CMA-ES on a user-supplied chain | Reference embedding + chain + render loop | Yes (non-differentiable OK) | 64 × 25 = 1,600 renders | B14 |
| Gaussian-prior MAP | Prior over parameters (presets) | Yes, if a prior exists | Same + prior fitting | B25; reduces parameter MSE up to 33 % |
| Text2FX | Differentiable EQ/reverb | **No** | 600 iters × 3 runs | B16 |
| GRAFX | Differentiable processors, DAG, no cycles | **No** | GPU, batch | B21 |
| NablAFx / DDSP grey-box | Per-device dry/wet training | **No** (model, not controller) | Training | B17, B18, B19 |
| Remastering / latent diffusion | Direct audio transform | **No** (not parameterised) | GPU | B22, B30 |

**Realistic evaluation counts and latency.** ST-ITO's budget is 1,600 candidate renders per transfer. In REAPER each candidate is not a DSP call: the bridge sets ~10 render settings, sets a time selection, invokes the render action, reads the settings back, validates the file, restores the settings and selection, then hashes and registers the asset (`scripts/reaper_bridge.lua:1660-1720`, `:1800-1831`; Python `_capture_render`/`analyze` at `daw/session.py:454-520`, `:1221-1360`). Arithmetic only: at 0.25 s/render → 6.7 min; 0.5 s → 13.3 min; 1 s → 26.7 min; 2 s → 53.3 min. I did **not** measure render latency, so these are cost envelopes, not predictions. By contrast, the in-repo limiter search uses 2 probes + ≤9 bisections × ≤3 passes ≈ **33 renders** (`limiter.py:136-167`) — a 50× cheaper search over 2 parameters. That is the right shape for "fit a compressor threshold", and CMA-ES is the right shape only for ~70–100 parameters with a reference target.

**V100 feasibility.** None of the FX-search methods requires a GPU *at optimization time* if the metric is DSP-based. ST-ITO's AFx-Rep encoder is a small audio encoder; running it on 1,600 short candidates is GPU-friendly but its VRAM/throughput is **not verified** here. The heavy GPU item would be training a per-device model (NablAFx/DDSP), which is a separate decision and must be sequential on the single V100.

**What Workstream A's answer changes here.** A's Q-A3 answer says no validated monotone style metric exists ("virtually no correlation" between mixdown features and preference). That does not refute ST-ITO as a method; it removes its objective for us. Consequence: the only admissible objectives today are **explicit DSP constructs** — band levels, loudness, crest/PLR, stereo correlation, a measured attack-count/position delta — or, if A's corridor is used, **rank/percentile distance inside the corridor's stated conditions** (stage match, section match, n ≥ 8, loudness matched ≤ 0.5 dB). Two prohibitions follow: do not minimize distance to the corridor *median* (the vector of marginal medians is not a real track), and do not drive any distance to zero (that produces the blandest possible result). A style-embedding objective stays behind a validation gate until A can show monotonicity on this material.

### 5.4 Panning / subgrouping / style transfer

| Method | What it can do | What it must not do here | Identifiability caveat |
|---|---|---|---|
| Auto-panning (B26, B27) | Place unidentified sources, keep LF centred | Move intentionally hard-panned double tracks | Pan position is an arrangement decision; the system cannot know intent |
| Subgrouping (B28) | Justify buses by shared processing need | Map "guitar" → "GTR BUS" by name | Names are not roles (`_group` guesses from English words) |
| Style transfer (B14, B25, B30) | Move a production style toward a reference | Claim it recovered the reference's chain | Many parameter sets map to similar embeddings; the prior paper says unconstrained solutions are "unrealistic" |
| Chain inversion from a stereo reference | Nothing reliable | Anything | ITO-Master: content-independent loss makes Θ-fitting ineffective |

**Identifiability, stated once and adopted from A's Q-A4 answer.** From a stereo reference and no stems, the defensible output is: *"band X of the reference sits D dB (median, MAD, n) from band X of our master over window W, aligned by method M"*, plus *"consistent with sources S1…Sk because …"*. That is a **band difference plus a ranked hypothesis**, never an attribution. Three reasons, all verified in A's scope: 4-stem separation means "guitar" is not a stream; leave-one-out needs per-source renders and a reconstructed mix; and Fx-Encoder++ needs an instrument query, i.e. it presupposes the separation being justified. I do not make this stricter (by demanding a unique chain) or looser (by naming a culprit).

**Source separation as an action tool: no.** Separation stays analysis-only and read-only. It is never written into the project as items or takes, never used as an edit source, and never retained as a stem (project retention rule). If a model wants to act on "the guitar", it acts on the *track* that the project map identifies, not on a separated stream.


---

## 6. (e) Implementation sequence and acceptance tests

Ordered, small, independently testable. Each step names its exit gate.

### Step 1 — `SourceMap` in Python (read-only)

- Consume the Lua `item_context` fields (`start_offset_s`, `playrate`, `stretch_marker_positions`, `source_length_s`, `source_length_is_qn`, `group_id`, `channel_mode`, `beat_attach_mode`) in a new `daw/source_map.py`.
- **Assertions:** for a fixture item with position 10.0 s, length 2.0 s, offset 0.5 s, playrate 1.0 → `src(10.0)=0.5`, `src(12.0)=2.5`. With playrate 0.5 → `src(12.0)=1.5`. With one stretch marker at project 11.0 / source 0.9 → piecewise, both segments exact at the marker.
- **Tolerance:** 1 sample (1/48 000 s = 20.8 µs) on round-trip; justified because REAPER stores seconds as doubles and the bridge writes exact values, and because the repo already uses `1.0/sample_rate` as its boundary tolerance (`session.py:137, 174`).
- **Exit:** unit tests only; no REAPER needed.

### Step 2 — `item.edge` operation (bridge + session + MCP)

- New typed operation with `edge ∈ {left, right}`, `delta_s`, optional `fade_s`; refuses on `stretch_markers` non-empty or `loop_source` true (v1 scope, explicit refusal).
- **Assertions (offline, fake bridge):** operation values → expected `position/length/offset` triple; refusal paths.
- **Assertions (live, disposable copy):** after `left, −0.069` on an item at 10.0/2.0/0.5, readback is 9.931/2.069/0.431 within 1 sample; the attack previously at project 10.500 is still at 10.500 within 1 sample; item count unchanged.
- **Exit:** live readback test; no render yet.

### Step 3 — geometry readback for split and edge

- Extend `read_edit_state_after` to return `{position_s, length_s, start_offset_s, fade_in_s, fade_out_s, playrate, stretch_marker_count}` for the target *and* the new item GUID.
- **Assertions:** after a split at 11.0 with `tail_s=0.130, move_s=0.100, right_trim_s=0.025`, readback overlap = `left_end − right_start` = 5 ms ± 1 sample; fade lengths equal the overlap ± 1 sample; left `start_offset_s` unchanged exactly.
- **Exit:** live test on a copy; this is the first time "verify afterward" is true for a split.

### Step 4 — seam metrics, relative and stereo-aware

- Replace the absolute score in `search_variants` with a **before/after** comparison: compute the same three metrics on the un-edited region and report deltas. Keep the raw values.
- **Assertions:** a synthetic correlated split with a linear complementary fade shows `|level_delta| ≤ 0.1 dB`; the same with an equal-power fade shows `+3.0 ± 0.2 dB` at the midpoint (arithmetic: cos 45° + sin 45° = 1.4142 → +3.01 dB). This is a *test*, not a policy: it proves the measurement can distinguish the two curves, which today it cannot because the applier does not choose the curve.
- **Exit:** unit tests; `seam.py`/`seam_search.py` unchanged in API.

### Step 5 — enforce `UsageScope` on the action path

- `session.plan()` (or the MCP wrapper) must refuse `item.split`/`item.edge`/`item.delete` when the cited evidence has `destructive_cut=False`, and `midi.create` when `midi=False`.
- **Assertions:** a plan citing an onset-only `PerformanceEvent` is refused with a typed error naming the missing scope; a plan citing a reviewed event with `destructive_cut=True` passes.
- **Exit:** unit + MCP test; no behaviour change for plans that cite no events (backwards compatible).

### Step 6 — rename and separate the two alignments

- `alignment.propose_moves` → `quantize_to_grid` (keeps behaviour, requires an explicit `intent` argument); `align.*` stays `align_to_reference`.
- **Assertions:** the old name raises a deprecation error naming the new one; no default path quantizes.
- **Exit:** unit test; docs updated in the same change.

### Step 7 — FX parameter search, small and model-based first

- Generalise `limiter._search_threshold` into `daw/fx_search.py`: probe two bounds, bisect ≤ N steps, accept only on measured improvement, always restore on failure. Support 1–3 parameters.
- **Assertions:** on a synthetic gain plugin, the search recovers a target within ±0.3 dB in ≤ 12 renders; on a non-monotone parameter it terminates and reports "no monotone relation" instead of returning a wrong value.
- **Exit:** offline test with a fake session; live test on ReaLimit/ReaEQ on a copy.

### Step 8 — optional CMA-ES behind an explicit budget

- Only for a user-supplied chain with a reference target; mandatory: `max_evaluations`, `prior` (parameter ranges), `plausibility` check, and a report listing the best parameters *and* the top-k alternatives.
- **Gate (from A's Q-A3 and Q-A2 answers):** the objective must be either an explicit DSP construct or a corridor *rank/percentile* distance under the corridor's stated conditions. A style-embedding objective is not admissible until monotonicity is demonstrated on this material; the step ships behind that gate, not before.
- **Assertions:** budget is enforced exactly (fake renderer counts calls); a run that hits the budget returns `status="budget_exhausted"`, not a silent best; a non-monotone parameter terminates with `status="no_monotone_relation"`.
- **Exit:** offline; not shipped until Step 7 is accepted and the objective gate is satisfied.

### Step 9 — the ORIG acceptance case

Fixture: a safe copy of a 3-take guitar track, fixed grid, first riff in 3/4, working copy at **150 BPM**, stretch markers present at playrate 1, playrate change forbidden, item edges offset from attacks with overlaps of **69 / 27 / 22 ms** (`docs/EDITING-ACCEPTANCE.md:7-11`).

Acceptance table (all measured on the candidate, compared with ORIG):

| # | Assertion | Tolerance | Why this tolerance |
|---|---|---|---|
| A1 | Every confirmed attack present in ORIG is present in the candidate (same count, same order) | exact count; no tolerance | `EDITING-ACCEPTANCE.md:45` rejects a 9→2 attack loss regardless of grid fit |
| A2 | Each matched attack's project position vs ORIG | median ≤ 5 ms, max ≤ 10 ms | ledger P06 proposes cut ±10 ms at precision ≥ 0.95; 5 ms median is the "aligned" proposal. **Owner must sign off** |
| A3 | Item-edge geometry readback vs plan | ≤ 1 sample | doubles in REAPER; repo's own boundary tolerance |
| A4 | Seam overlap per joint vs planned | ≤ 1 sample | geometry identity; `test_seam_search.py:42-49` already asserts exact geometry offline |
| A5 | No unintended gap (right start > left end) | none allowed | a gap is an audible hole; the assembler raises on it (`seam_search.py:99-101`) |
| A6 | No unintended repeat (left end beyond the next attack's start) | none allowed | ORIG invariant from `EDITING-ACCEPTANCE.md:34` |
| A7 | Level continuity across each seam | \|Δ\| ≤ 1.0 dB over 20 ms windows | **policy value, not a finding** (`threshold_basis: "policy"`, fail closed when unset) — A's Q-A1 answer confirms no perceptual basis exists; owner signs the key |
| A8 | No click: relative step test | candidate max step ≤ 1.5 × the pre-edit max step in the same ±5 ms window | `threshold_basis: "relative"`; absolute step is content-dependent (0.14 for 1 kHz, 0.71 for 5 kHz full scale), so only a relative test is defensible |
| A9 | Playrate unchanged, no new stretch markers | exact | `EDITING-ACCEPTANCE.md:36` |
| A10 | Tempo map unchanged | exact | same |
| A11 | Undo restores the pre-edit digest | exact fingerprint equality | Wave C established full digest coverage (master+automation), so `same_content` is meaningful |
| A12 | Re-render of the seam region matches the previewed candidate | ≤ 1 sample alignment, ≤ 0.1 dB level | preview/commit must not diverge; today it can, because the fade shape is REAPER's default |
| A13 | Artifact quantities vs pre-edit, same window | `attack_count_delta = 0`; \|`attack_position_error_ms`\| ≤ 2 ms; `pre_echo_ratio_db` ≤ +1 dB; `attack_rise_ms` ≤ 1.2 × pre-edit | the four names in §0.4, so A can cite them; the numbers are **relative** to the pre-edit measurement on the same material |

**Deliberately not an acceptance criterion:** a fixed fade length, a fixed number of cuts, or a percentage of onsets within the grid. `EDITING-ACCEPTANCE.md:46` explicitly rejects those as readiness criteria.

### Step 10 — end-to-end test matrix (beyond ORIG)

| Tier | Material | Ground truth | What it proves | Licence |
|---|---|---|---|---|
| T0 synthetic | Impulses at known sample positions; correlated stereo splits; a 3/4 click grid at 150 BPM | exact | Detector/geometry/metric correctness | created in-repo |
| T1 synthetic music | Rendered stems from a MIDI score with known note times; deliberate tempo ramp; deliberate 60 Hz hum, clipping, DC | exact note times, exact tempo curve | Event and tempo hypotheses vs truth | created in-repo |
| T2 real multitrack, same song, multiple mixes | HiMMP “In Solitude” (41 tracks, 34 items, 300.52 s, tempo 200→240→200; CC BY per Wave D record) | producer mixes (8) as *references*, not truth | Action safety and measurement stability on real dense material | CC BY (verified in Wave D record) |
| T3 real multitrack, single mix | IDMT-SMT-Guitar / Bass (research licence), MedleyDB (CC BY-NC-SA), MUSDB18 (non-commercial) | stems where available | Domain stress (distorted guitar, real drums) | **research-only**; do not ship derivatives |
| T4 the owner's project | a disposable copy | owner's ORIG | Musical acceptance | private; not redistributed |

**Why a clean reference-video soundtrack is not a valid ground-truth mix.** Five reasons, in order of severity: (1) it is a *different mix* of possibly different takes and edits, so per-source attribution is impossible; (2) it has passed through lossy codecs and the platform's loudness normalisation, so absolute level and true peak are meaningless; (3) it may be a different master (streaming target) than the one being compared; (4) video container audio is often resampled and may be sync-shifted, so sample-level comparison is invalid; (5) it is copyrighted, so it cannot be stored, rendered against, or shipped in a test corpus. It is usable only as a *listening reference* by a human, never as a numeric target.

**Listening constraints for seam acceptance (adopted from A's Q-A5 answer).** Each seam stimulus must be at least ~2 s (texture completion operates over seconds; a 200 ms stimulus measures something else), loudness-matched within 0.5 dB (`docs/POLICY.md:35`), same stage and window on both sides, and presented as a **forced-choice** trial (2AFC/ABX: "which has the cleaner join"), not a preference question. Whole-mix A/B remains reserved for character and taste, and cannot resolve a 5 ms crossfade. With 12 seams per case and 2AFC, 10 trials per seam gives 120 presentations per listener per condition — enough for a binomial test at α = 0.05 only if the effect is large, which is itself a finding worth reporting.


---

## 7. (f) Open research questions and the evidence that would settle each

| # | Question | Why it blocks | What would settle it |
|---|---|---|---|
| Q1 | **Answered cross-workstream (A Q-A1):** no absolute click/gap threshold is defensible; gates are relative. Remaining question: *how large* a defect is detectable at all on this material | Determines whether A8 needs any absolute number (it does not) and whether a "seam is clean" claim can ever be positive rather than comparative | Inject defects of known magnitude (0.5/1/2/3 dB dips, 5/10/20 ms gaps, step discontinuities) into 4-guitar stems; measure the detection threshold per listener and per metric; report the spread, not a single number |
| Q2 | Does the 2 ms envelope detector's F1 hold on amp-print guitars at 150–200 BPM with cymbals? | Everything downstream cites its onsets | Annotate 2 × 30 s of the owner's material (owner + one other), compute F1 at ±10/±25/±50 ms, compare with the STFT-flux detector |
| Q3 | Is `MIDI_GetProjTimeFromPPQPos` correct on looped MIDI items? | MIDI write verification | A live probe with a looped 2-bar item (ledger E14) |
| Q4 | Do stretch-marker positions survive a save/reload exactly? | Any stretch verification | Read RPP + live readback (ledger E12) |
| Q5 | Does group sync apply through the API when splitting one member of a group? | Group-safe edits | A live null test: split one of two grouped items, measure the other (ledger E18) |
| Q6 | **Answered cross-workstream (A Q-A3):** no monotone relation for available mixdown/style embeddings, so they are ranking-only. Remaining question: could a *purpose-built* production metric be monotone on metal? | Decides whether FX search can ever have a learned objective, or only DSP constructs | Train a metric on paired processing changes with known parameter deltas (dry × preset grid), then test monotonicity on held-out metal: rank correlation between embedding distance and parameter distance; require ρ > 0.5 before any optimizer consumes it |
| Q7 | How many renders can the bridge sustain per minute on this machine? | Converts "1,600 evaluations" into wall-clock | Measure `capture_audio`/render round-trip on a 5 s section, 100 repetitions; report p50/p95 |
| Q8 | Does a per-device model trained on ToneTwist generalize to the owner's amp prints? | Decides whether grey-box amp modelling is worth it | Train on ToneTwist analog devices, test on held-out amp prints; report ESR/ESR-E and a listening test |
| Q9 | Can a `SourceMap` with stretch markers be inverted reliably for edge edits? | v2 of `item.edge` | Analytic test + live: place a marker, extend the left edge, verify the marker's source position is unchanged |
| Q10 | What is the smallest `UsageScope` set that still lets the model act usefully? | Too strict → nothing can be edited; too loose → the gate is theatre | A task-based evaluation: 10 editing tasks, count how often a plan is refused for missing scope |

---

## 8. Source ledger

| ID | Citation | DOI / arXiv / URL | Date / version | Accessed | Level | Claims supported |
|---|---|---|---|---|---|---|
| B01 | Dannenberg, R. B. *An Intelligent Multi-Track Audio Editor.* ICMC 2007, Vol. II, pp. II-89–94 | [cmu.edu PDF](http://www.cs.cmu.edu/~rbd/papers/icmc2007iaed.pdf) | Aug 2007 | 2026-11-13 | FULL-TEXT | Score-driven edit pipeline; (T,F,D) edit triple; monophonic assumption; feel loss; outlier suggestion |
| B02 | Liu, Dannenberg, Cai. *The Intelligent Music Editor.* ICIC 2010, LNCS | [10.1007/978-3-642-14932-0_16](https://doi.org/10.1007/978-3-642-14932-0_16) | Dec 2010 | 2026-11-13 | ABSTRACT-ONLY (paywalled) | Lineage only |
| B03 | Tomczak, Hockman. *Onset Detection for String Instruments…* Audio Mostly 2023 | [10.1145/3616195.3616206](https://doi.org/10.1145/3616195.3616206); [repo](https://github.com/arme-project/onset-detection-for-strings) | Aug 2023 | 2026-11-13 | ABSTRACT + REPO-CODE | ~40 min / >8,000 onsets; CC BY-NC-SA data, BSD code |
| B04 | Wang, Stables, Li, Duan. *Score-Aligned Polyphonic Microtiming Estimation.* ICASSP 2018 | [10.1109/ICASSP.2018.8461699](https://doi.org/10.1109/ICASSP.2018.8461699) | Apr 2018 | 2026-11-13 | ABSTRACT-ONLY (403) | Microtiming is score-relative |
| B05 | Gong, Serra. *Towards an efficient deep learning model for musical onset detection.* arXiv 1806.06773 | [arXiv](https://arxiv.org/abs/1806.06773) | Jun 2018 | 2026-11-13 | ABSTRACT-ONLY | 28.3 % params; cross-dataset transfer fails |
| B06 | Degara, Davies, Pena, Plumbley. *Onset Event Decoding…* IEEE JSTSP 5(6) | [INESC PDF](https://repositorio.inesctec.pt/server/api/core/bitstreams/6fc4df16-d959-4c99-a0fb-974692317f9b/content) | Oct 2011 | 2026-11-13 | FULL-TEXT | 13 files, 551 onsets, 50 ms tolerance; rhythmic decoding beats adaptive thresholding |
| B07 | Kehling, Abeßer, Dittmar, Schuller. *Automatic Tablature Transcription of Electric Guitar Recordings.* DAFx 2014 | [DAFx PDF](https://www.dafx.de/paper-archive/2014/dafx14_christian_kehling_automatic_tablature_trans.pdf) | Sep 2014 | 2026-11-13 | FULL-TEXT | 98.5 % onset F, 0.96 pitch F; clean standard-tuned guitar |
| B08 | Mounir, Karsmakers, van Waterschoot. *Musical note onset detection based on a spectral sparsity measure.* EURASIP JASMP 2021:30 | [10.1186/s13636-021-00214-7](https://doi.org/10.1186/s13636-021-00214-7) | 2021 | 2026-11-13 | FULL-TEXT | NINOS² ODF; low-energy-bin sparsity; repeated notes |
| B09 | Böck et al. *madmom.* arXiv 1605.07008 | [arXiv](https://arxiv.org/abs/1605.07008) | May 2016 | 2026-11-13 | ABSTRACT-ONLY | Library scope; CPU inference (excluded by project rule) |
| B10 | Driedger, Müller. *A Review of Time-Scale Modification of Music Signals.* Appl. Sci. 6(2):57 | [10.3390/app6020057](https://doi.org/10.3390/app6020057) | 2016 | 2026-11-13 | FULL-TEXT | Artifact taxonomy: doubling/skipping/smearing/phasiness; anchor points; implementations |
| B11 | Bryan, Herrera, Wang. *User-Guided Variable-Rate Time-Stretching via Stiffness Control.* DAFx 2012 | [DAFx PDF](https://dafx.de/paper-archive/2012/papers/dafx12_submission_67.pdf) | Sep 2012 | 2026-11-13 | FULL-TEXT | Intent (stiffness/constraints) separated from rate curve |
| B12 | Nagel, Walther. *A Novel Transient Handling Scheme for Time Stretching Algorithms.* AES 127th, Paper 7926 | [AES forum page](https://secure.aes.org/forum/pubs/conventions/?elib=15121) | 1 Oct 2009 | 2026-11-13 | ABSTRACT-ONLY | Remove/re-insert transients around stretching |
| B13 | Juillerat. *Audio Time Stretching with Controllable Phase Coherence.* AES 142nd, Paper 9779 | [pitchtech.ch PDF](https://www.pitchtech.ch/Confs/AES142/AES142.pdf) | May 2017 | 2026-11-13 | ABSTRACT-ONLY | One parameter trades vertical/horizontal coherence |
| B14 | Steinmetz et al. *ST-ITO.* arXiv 2410.21233 (ISMIR 2024) | [arXiv](https://arxiv.org/abs/2410.21233); [code](https://github.com/csteinmetz1/st-ito) | 2024 | 2026-11-13 | FULL-TEXT | CMA-ES 64×25=1,600; series only; chain supplied by user; input minimally processed |
| B15 | Koo et al. *ITO-Master.* arXiv 2506.16889v3 | [arXiv](https://arxiv.org/abs/2506.16889v3) | 3 Jul 2025 | 2026-11-13 | FULL-TEXT (partial) | Θ-fitting works only when content is identical; CC BY 4.0 |
| B16 | Chu, O'Reilly, Barnett, Pardo. *Text2FX.* arXiv 2409.18847 | [arXiv](https://arxiv.org/abs/2409.18847) | 2024 | 2026-11-13 | FULL-TEXT | Differentiable EQ/reverb only; 600 iters × 3 runs; 1,200 listener evaluations |
| B17 | Comunità, Steinmetz, Reiss. *NablAFx.* arXiv 2502.11668 | [arXiv](https://arxiv.org/abs/2502.11668) | Feb 2025 | 2026-11-13 | FULL-TEXT | Framework for black/gray-box modelling incl. time-varying conditioning |
| B18 | Comunità, Steinmetz, Reiss. *Differentiable Black-box and Gray-box Modeling of Nonlinear Audio Effects.* arXiv 2502.14405 | [arXiv](https://arxiv.org/abs/2502.14405) | Feb 2025 | 2026-11-13 | FULL-TEXT (partial) | Field evidence base is narrow; ToneTwist introduced |
| B19 | Wright, Välimäki. *Grey-Box Modelling of Dynamic Range Compression.* DAFx20in22 | [DAFx PDF](https://www.dafx.de/paper-archive/2022/papers/DAFx20in22_paper_35.pdf) | Sep 2022 | 2026-11-13 | ABSTRACT-ONLY | Similar accuracy to black-box at <10 % ops/sample |
| B20 | Hayes et al. *A review of DDSP for music and speech synthesis.* Front. Sig. Proc. 3:1284100 | [10.3389/frsip.2023.1284100](https://doi.org/10.3389/frsip.2023.1284100) | 11 Jan 2024 | 2026-11-13 | FULL-TEXT | Optimisation pathologies; robustness; design trade-offs |
| B21 | Lee et al. *GRAFX.* arXiv 2408.03204 | [arXiv](https://arxiv.org/abs/2408.03204); [repo](https://github.com/sh-lee97/grafx) | 6 Aug 2024 | 2026-11-13 | FULL-TEXT + REPO | DAG of differentiable processors; no cycles; 11.8 vs 77.6 calls |
| B22 | Koo, Paik, Lee. *End-to-end Music Remastering…* arXiv 2202.08520 | [arXiv](https://arxiv.org/abs/2202.08520) | Feb 2022 | 2026-11-13 | ABSTRACT-ONLY | Direct transform, no parameters; CC BY 4.0 |
| B23 | Yeh et al. *DDSP Guitar Amp.* arXiv 2408.11405 | [arXiv](https://arxiv.org/abs/2408.11405) | 21 Aug 2024 | 2026-11-13 | ABSTRACT-ONLY | Interpretable amp model at <10 % ops; CC BY 4.0 |
| B24 | Comunità. *ToneTwist AFx Dataset* | [repo](https://github.com/mcomunita/tonetwist-afx-dataset) | created 3 Jan 2024 | 2026-11-13 | DATASET-CARD | ~43 min dry, 48 kHz/32-bit, controls 0–10, MIT repo licence, upstream audio provenance |
| B25 | Yu et al. *Improving ITO for Vocal Effects Style Transfer with a Gaussian Prior.* arXiv 2505.11315 | [arXiv](https://arxiv.org/abs/2505.11315) | 17 Oct 2025 (v2) | 2026-11-13 | ABSTRACT-ONLY | Unconstrained ITO → "unrealistic configurations"; prior cuts parameter MSE up to 33 % |
| B26 | Perez-Gonzalez, Reiss. *A Real-Time Semiautonomous Audio Panning System…* EURASIP JASP 2010:436895 | [10.1155/2010/436895](https://doi.org/10.1155/2010/436895) | 26 May 2010 | 2026-11-13 | FULL-TEXT (abstract+intro) | Rule-based panning ≈ professional in a subjective test; not artistic decisions |
| B27 | Perez-Gonzalez, Reiss. *Automatic Mixing: Live Downmixing Stereo Panner.* DAFx 2007 | [DAFx PDF](https://www.dafx.de/paper-archive/2007/Papers/p063.pdf) | Sep 2007 | 2026-11-13 | FULL-TEXT (partial) | LF unpanned; priority channels; discrete steps |
| B28 | Ronan, Gunes, Reiss. *Analysis of the subgrouping practices of professional mix engineers.* AES 142nd, Paper 9700 | [QMUL PDF](https://joshreiss.github.io/documents/2017/Ronan%20Gunes%20Reiss%20-%20AES142.pdf) | May 2017 | 2026-11-13 | FULL-TEXT (partial) | Panning → subgrouping/EQ/DRC → loudness; DRC 31 vs EQ 10 mentions; need-driven, not genre-driven |
| B29 | Liu, Mourgela, Ai, Reiss. *An automatic mixing speech enhancement system for multi-track audio.* arXiv 2404.17821 | [arXiv](https://arxiv.org/abs/2404.17821) | Apr 2024 | 2026-11-13 | ABSTRACT-ONLY | Harmony search over level/EQ/DRC/space; PEAQ objective; speech only |
| B30 | Demerlé, Esling, Doras, Genova. *Combining audio control and style transfer using latent diffusion.* arXiv 2408.00196 | [arXiv](https://arxiv.org/abs/2408.00196) | Aug 2024 | 2026-11-13 | FULL-TEXT | Structure/timbre disentanglement; generation, not parameterisation |

---

## 9. Could not verify

1. **B02 full text** — paywalled. No mechanism claims made.
2. **B04 method, data, numbers** — ACM/IEEE returned 403; the author PDF extract was a fragment. Used as a pointer only.
3. **B03 F1 numbers** — the open-access PDF text layer was partial (tables likely images). Only abstract-level claims quoted.
4. **B08 F-measure values** — tables rendered as images/math in the HTML; no numbers quoted.
5. **B12, B13, B19, B22, B23, B25, B29 internals** — abstract-level only (paywall or extract limits). Licence claims for B22/B23 come from the arXiv page banner (CC BY 4.0), not from a licence file.
6. **madmom licence** — widely known to be BSD but not re-verified from the repository in this pass; do not rely on it without checking.
7. **GRAFX repository licence** — the GitHub page extract did not include the licence field; the *paper* is CC BY-SA 4.0. Code licence unverified.
8. **ToneTwist audio licensing** — repo is MIT, but dry inputs include IDMT datasets and YouTube recordings; whether the MIT grant covers the audio is unresolved.
9. **V100 fit for any neural onset model** — no VRAM/throughput figures verified; I did not run anything.
10. **REAPER render latency** — not measured; the 6.7–53.3 min envelopes are arithmetic over assumed per-render costs, not predictions.
11. **Fade shape actually used by `D_FADEINLEN`/`D_FADEOUTLEN`** — depends on project fade shape settings; not verified live. This is why A12 (preview vs render) is an acceptance test rather than an assumption.
12. **Stretch-marker position round-trip through save/reload** — ledger E12 remains open; I did not read an RPP.
13. **Group sync semantics through the API** — ledger E18 remains open.
14. **Whether `MIDI_GetProjTimeFromPPQPos` is correct on looped items** — ledger E14 remains open.
15. **The exact 82 % figure in Kehling 2014** — present in my extract but without its subject; not quoted.
16. **Whether `reaper_project(include_items=True)` and the panorama are ever shown to the same model in one context** — I verified the surfaces exist and differ, not which is used in practice.

---

## 10. What earlier rounds already answered, and what is now stale

| Prior claim | Where | Status after this review |
|---|---|---|
| "19 MCP tools" | DECISION-LEDGER D22; R3-05; round-4 docs | **Stale.** `grep -c "@server.tool()"` = 21; `tests/test_mcp_server.py:110` asserts 21 |
| "`alignment.propose_moves` quantizes to the grid, which TZ §5 forbids" | GAP-ANALYSIS §3.6 | **Partly stale.** True of `daw/alignment.py`; `daw/align.py` now implements reference-take matching with drift and policy. The conflict is now *two competing functions*, not a missing capability |
| "No cross-track transient matching" | GAP-ANALYSIS §3.6 | **Closed** by `daw/align.py:82-125` (per-attack cross-correlation, median/MAD, slope+stderr) |
| "Seam search and native application use the same geometry" | test name `test_reported_live_candidate_has_same_geometry_as_split_parameters` | **Only arithmetic.** The geometry formula matches; the *fade curve* and the *readback* do not |
| "Revision-bound transactions are safe" | Wave B/C records | **Mostly true for revision/undo; not true for post-conditions.** Splits return only item counts |
| "MIDI is a project fact, not audio evidence" | D25 | **Confirmed and still unenforced**: `ScoreObject` exists, nothing consumes it |
| "`UsageScope` gates destructive use" | event_model design | **Type-level only.** No consumer |
| "Panorama is the whole-project view" | round-4 context ledger | **Lossy for edits**: drops offset/playrate/stretch/fades/groups |
| "No genre constants" | TZ §4, GAP-ANALYSIS | **Still violated** in `edit_map._group` (English name heuristics), `edit_map` 120 BPM fallback, and `edit_map` intent thresholds |
