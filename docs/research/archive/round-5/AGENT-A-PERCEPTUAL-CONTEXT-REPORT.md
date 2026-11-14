# Workstream A — Perceptual masking, clarity, texture, references, separation and evaluation data

**Status:** research report (Round 5). No source code, tests, docs, model weights, REAPER projects or datasets were modified or downloaded. One file added: this report.

**Baseline observed while writing:** branch `main`, HEAD `86b14bb` ("Alignment: no classifier, measured drift, policy decides"), dirty working tree preserved (the concurrent capture-performance work is in flight in `analysis/events.py`, `analysis/temporal.py`, `analysis/metrics.py`, `daw/analysis_workers.py`, `tests/test_analysis_optimizations.py`, `docs/PERFORMANCE-OPTIMIZATION-TZ.md`, `docs/VALIDATION.ru.md`). Line numbers below are from the checkout as read today; where the performance work has already shifted a line, the shift is noted rather than "corrected".

**Evidence labels used throughout**

| Label | Meaning |
|---|---|
| `[FULL-TEXT]` | I read the source's own body text (methods/results), not only its abstract |
| `[ABSTRACT-ONLY]` | I read the publisher/arXiv abstract and metadata; body paywalled or unextractable |
| `[REPO-CODE]` | I read the official repository README / licence file / code |
| `[DATASET-CARD]` | I read the dataset card / dataset API metadata |
| `[PROJECT path:line]` | A file in this repository that I opened |
| `[INFERENCE]` | My reasoning or arithmetic over verified inputs |

Anything not in one of those classes is in §9 "Could not verify" and is not used to support a design decision.

---

## 0. Interim note to Workstream B

You own musical events, editing, time-scale modification, FX optimization/control, panning/grouping and mix-style transfer. I own what a model can *hear and infer* from processed tracks, the master, and their joint relation, plus the datasets and evaluation protocol behind that.

**What I depend on from you (do not leave these unanswered):**

1. **Post-change verification primitive.** My whole design assumes a claim is checked by re-rendering the *same* stage/window/revision and re-measuring the *same* metric. `compare.simulate_gain` only predicts linear gain (`compare.py:130-147`). If your edit/verify loop does not expose "render a candidate section without committing", then reference-guided search and every post-change check in §4/§6 stay manual `apply → render → undo`.
2. **Pan semantics.** My masking/dominance evidence must know whether two sources share a pan position or sit on opposite sides. `panorama.py` carries `mixer.pan` (`panorama.py:1627`) but the analysis path mono-averages channels before computing band levels (`temporal.py:329`). If you change pan/subgroup behaviour, tell me what value lands in the capture, because "pan_relation" in §4 is read from the project map, not from audio.
3. **Whether you will treat source separation as an action tool.** If you put htdemucs/BS-RoFormer behind an edit tool that *writes* separated material into the project, that collides with the retention contract I am defending in §3/§4 (temporary analysis material only). Decide one owner for separation; I recommend it stays analysis-only and read-only.
4. **Time-stretch artifact metrics.** Your time-scale work produces artifact measurements (pre-echo, transient smearing, phasiness). I need their names/units so clarity evidence can reference them instead of inventing a second artifact vocabulary.
5. **Whether your FX-search objective will consume my reference corridor.** If yes, the corridor must carry stage/section/loudness-match provenance or your search optimizes toward a mismatched target.

**Overlaps I am deliberately *not* taking:**

- `Fx-Encoder++` appears in both lists. You own it "where it informs actionable analysis". I assess it only as a *representation* (what it can and cannot identify, §3), not as an action path.
- Differentiable/black-box DSP, GRAFX, ITO-Master, Text2FX, NablAFx, DDSP guitar amp, grey-box compression: entirely yours. I reference them only as constraints on what my evidence can prove.
- The degradation matrix in `docs/MIX-QUALITY-METHODOLOGY.ru.md:440-461` is shared ground: I use it to validate *measurement response*, you use it to validate *edit response*. Neither of us should re-derive it.

**Questions you must answer for me:**

- Q-B1: Does an edit transaction preserve the capture asset hash and the `RevisionRef` so that an evidence edge stays `fresh` vs `stale` correctly? (Current `is_stale` accepts `dep_set` and ignores it — `context/revision.py:248-262`; `CONTEXT-LEDGER-DESIGN.md:272-275` proposes activating it. If your edits do not write dependency records, my `state` field is decorative.)
- Q-B2: Will you expose per-operation *expected effect on measured constructs* (e.g. "this EQ move is expected to raise band 315 Hz on this track by X dB")? Without it, a post-change delta cannot be attributed to the operation versus the arrangement.
- Q-B3: Do you need my texture statistics for transient/attack preservation tests? If yes, I will define them once, in the shared contract, rather than twice.

### 0.5 Answers to Workstream B's questions Q-A1…Q-A5

Workstream B's report asks five questions that my material can answer directly. Answering them here so the parent does not have to infer my position.

**Q-A1 — is there perceptual evidence for a *fixed* click/gap threshold on saturated, limited, cymbal-dense material?**
No, and I would treat any such number as a policy value with a named owner, not a finding. The closest evidence is negative: the group that built a working autonomous de-masking EQ concluded that objective masking measures are "ill-suited for quantifying perceived masking in multitrack musical audio" `[ABSTRACT-ONLY]` (A-03), and the only blind comparison of de-masking techniques found the preferred technique changed by scenario `[FULL-TEXT]` (A-05). Nothing I verified measures the audibility of a 0.6 dB, 8 ms dip under four distorted guitars. **Deliverable: relative gates only** (before/after, same material, same stage, same window), with an explicit `threshold_basis: "relative"`; an absolute click threshold must be a project policy key and must fail closed when unset.

**Q-A2 — numeric per-band targets with confidence, or only descriptions?**
Numeric, but *conditional and distributional*. I will deliver, per band and per metric: median, MAD, n, bootstrap CI, and a `target_kind: "conformance"` label — only when stage matches, section type matches, n ≥ 8, and loudness is matched within 0.5 dB (`compare.py:56-92` plus the rules in §4.9 here). Outside those conditions the corridor refuses and you get critique only. Two warnings for your optimizer: (i) minimize distance to the *corridor* (rank/percentile), not to the median, because the vector of marginal medians is not a real track (`docs/MIX-QUALITY-METHODOLOGY.ru.md:384-386, 531`); (ii) a corridor is not a quality target, so an optimizer that drives distance to zero will produce the blandest possible mix.

**Q-A3 — is a production-style embedding monotone with audible similarity, i.e. usable as an optimization objective?**
No. Two independent reasons: across 184 mixes of 19 songs, mixdown features showed "virtually no correlation" with preference and the principal-component axes did not match intuitive descriptors `[FULL-TEXT]` (A-10); and the instrument-wise FX representation literature itself reports "reduced effect parameter matching performance" and "difficulties with single effect understanding" `[FULL-TEXT]` (A-20), i.e. the map from processing to embedding is many-to-one. **Ranking/retrieval aid only.** Optimizing such an embedding is exactly the failure mode your Gaussian-prior citation describes.

**Q-A4 — for a stereo reference with no stems, what is the strongest claim about *which* source causes a band difference?**
A ranked hypothesis with alternatives, never an attribution. The defensible statement is: "band X of the reference sits D dB (median, MAD, n) from band X of our master over window W, aligned by method M" plus "consistent with sources S1…Sk because …". Attribution fails for three verified reasons: separation is 4-stem so "guitar" does not exist as a stream (A-17/A-18), leave-one-out needs per-source renders and a reconstructed mix (§3 G4), and Fx-Encoder++ needs an instrument query, i.e. it presupposes the separation you would be trying to justify (A-20). So your identifiability section should be *no stricter* than this and *no looser*: band-difference yes, source attribution hypothesis-only.

**Q-A5 — do short (2–5 s) seam-level stimuli fit the listening protocol?**
Yes, and they are the right unit for seam work, with four constraints: same stage and window on both sides; loudness matched within 0.5 dB (`docs/POLICY.md:35`); **at least ~2 s**, because texture completion operates over seconds `[ABSTRACT-ONLY]` (A-16) and a 200 ms stimulus measures something else; and forced-choice (2AFC/ABX) rather than preference, because "which is cleaner" is a detection question while "which do you prefer" is a taste question. Whole-mix A/B remains for character and taste only.

---

## 1. Executive answer

The project already records its key negative result: `temporal.masking()` is a band-level dominance heuristic, not a masking threshold (`temporal.py:771-835`, `bundle.py:278-280`), as `docs/GAP-ANALYSIS.md:178-199` states bluntly. The literature adds *why that is right and what to build instead*.

Three findings change the design. First, both worked-out masking systems conclude that objective masking measures do not track perceived masking in multitrack music: Hafezi & Reiss say so explicitly while reporting an autonomous EQ that comes close to a professional mix `[ABSTRACT-ONLY]`; Wakefield & Dewey's blind, unlabelled-slider study found *panning both tracks apart* the most satisfying de-masking tool, and cutting only the masker the least preferred of all `[FULL-TEXT]`. A mono band difference cannot be the primary clarity evidence; pan and level relations must sit beside it.

Second, texture is carried by time-averaged statistics — spectrum, modulation, cross-channel correlation — and listeners complete an interrupted texture over seconds `[ABSTRACT-ONLY]`. A 100 ms band cell is the wrong scale for fuzz walls, cymbal wash and blast-beat beds, and the statistics that do carry texture are already computed and then discarded (`docs/GAP-ANALYSIS.md:164-170`).

Third, preference is not recoverable from mixdown features: across 184 mixes of 19 songs and ~5000 ratings, Colonel & Reiss found "virtually no correlation" between embedded MIR features and mean preference or its spread `[FULL-TEXT]`. That argues against any learned quality head and for the architecture already chosen: measurements, questions, the user's ears.

Recommended: (a) re-frame masking as *band dominance* carrying `pan_relation`, `broadband_delta_db`, `active_fraction`, and fix two severity constants — `6.0` dB is inert against a `10.0` dB producer, and the `5 %/45 %` share band flags every source once a project has ≥20 stems (`mix_evidence.py:34-98`); (b) gate partial loudness behind a reconstruction check, since it currently subtracts solo-render power from a limited master and calls the residual "share of partial loudness" (`partial.py:246-249,320-324`); (c) add a bounded `audio_relations[]` block (≤8 edges/source, ~37 KB at 42 sources) with method, version, window, state and evidence hashes; (d) add a texture summary over 2–4 s windows; (e) keep separation and FX embeddings temporary and read-only — a 4-stem model cannot isolate "guitar" here, and Fx-Encoder++ reports weak parameter matching and weak single-effect understanding `[FULL-TEXT]`.

No item produces an "is this mix good" score; each lets the model say where, which sources, how far to trust the measurement, and what would falsify it.

---

## 2. (a) Verified source-by-source findings

Each block: what it actually does → input/output → data → licence → compute → reported numbers → failure cases → access → the decision it informs → prior-round status.

### 2.1 Perceptual masking and clarity

#### A-01 Parker & Fenton, *Musical Mix Clarity Prediction Using Decomposition and Perceptual Masking Thresholds* (Appl. Sci. 11(20):9578, 2021) `[ABSTRACT-ONLY]`

- **Does:** decomposes a mixed signal into transient, steady-state and residual components; computes masking thresholds per component; combines their relative relationship into a single masking score as "mix clarity".
- **I/O:** input = a stereo mixdown; output = one scalar masking score. No source separation, no per-track attribution.
- **Reported:** best of three variants reached Spearman ρ = 0.8382 (p < 0.01) against subjective clarity from a controlled listening test; a separate degradation dataset showed the relationship held "particularly when masking was measured using linearly spaced analysis bands"; noise-like residual energy reduced perceived clarity.
- **Failure cases the paper itself flags:** the construct is *clarity-as-separability* (Pedersen/Zerarov "clean"), not overall quality; linearly spaced bands (rather than critical bands) performed better, which is a warning that the psychoacoustic machinery is not doing what its name implies.
- **Access:** open access at MDPI; I could read the abstract, introduction and discussion fragments, but the method section was truncated by every extraction path I tried (MDPI blocks the browser with an Akamai "Access Denied"; `web_extract` returns a head+tail window). The listening-test N and stimulus list are therefore **not verified** (§9).
- **Decision informed:** clarity is a real, measurable construct with one published correlation — and it is a *single-number* construct computed on a *whole mix*. It cannot tell you which of four guitars is buried. Use it, if at all, as a section-level index whose provenance is a specific model version, never as a per-source verdict.
- **Prior-round status:** already used by `docs/MIX-QUALITY-METHODOLOGY.ru.md:29-30,576` (ρ≈0.84, "the only construct with a published number"). **New here:** (i) the 0.8382 belongs to the best variant, and the thesis reports the *component-masking* variants at ρ = −0.7964 and −0.6930 (negative because more masking = less clarity) — see A-02; quoting "0.84" without the sign convention invites a sign error in any reimplementation.

#### A-02 Parker, *Towards a Perceptual Model of Clarity in Music Mixes* (PhD thesis, University of Huddersfield) `[FULL-TEXT]` (front matter, abstract, ch.1–2; body beyond ~50 k characters not read)

- **Does:** three model families — inter-band relationship (IBR / IBR-MR), signal-separation + component masking (L2PM-MC, L3PM-MC), and a semi-supervised CNN classifier.
- **Reported (abstract):** IBR r = 0.6, ρ = 0.6079; IBR-MR r = 0.7635, ρ = 0.8024; L2PM-MC r = −0.6103, ρ = −0.7964; L3PM-MC r = −0.6483, ρ = −0.6930; CNN classifier 80 % accuracy on unseen data.
- **Date correction:** the attachment described this as a 2023 thesis; the title page says **July 2022** (award recorded 2023 in the repository).
- **Failure cases:** the strongest variant (IBR-MR, ρ = 0.80) needs *no* masking model at all — it is a multi-resolution inter-band relationship. The masking-threshold models are the *weaker* ones. This is the opposite of what the attachment's framing implies.
- **Decision informed:** if the project wants one clarity number, the evidence favours an inter-band-relationship formulation over a full psychoacoustic masking model, at a fraction of the cost. This is a direct argument against implementing MPEG Model II as a first step.
- **Prior-round status:** not previously cited in the repo (the methodology doc cites only the journal paper). **New:** the variant ranking, and the negative-sign convention.

#### A-03 Hafezi & Reiss, *Autonomous Multitrack Equalization Based on Masking Reduction* (JAES 63(5):312–323, 2015; DOI 10.17743/jaes.2015.0021) `[ABSTRACT-ONLY]`

- **Does:** designs "a simplified measure of masking based on best practices in sound engineering"; implements off-line and real-time low-latency autonomous multitrack EQ to reduce that measure; validates objectively and with a listening test against raw, amateur and professional mixes.
- **Reported:** autonomous systems reduce perceived and objective spectral masking and improve overall quality; the off-line semi-autonomous system beat the amateur mix and came close to the professional one "by simply controlling one user parameter"; and — the sentence that matters most here — "our results also suggest that existing objective measures of masking are ill-suited for quantifying perceived masking in multitrack musical audio."
- **Failure cases:** real-time versions trade frequency resolution for latency; the underlying measure is explicitly a *simplification of practice*, not a threshold model.
- **Access:** the QMUL PDF returns HTTP 422 to the extractor and the scispace mirror returns 405; I read the publisher abstract as reproduced on Academia.edu. Full method not verified.
- **Decision informed:** do not present any masking number as "the masking"; even the group that built a commercial autonomous EQ says the objective measure does not track perceived masking in music.
- **Prior-round status:** cited as background in `docs/research/04-MIX-PERCEPTION-AND-REFERENCES.md` and in the methodology doc's reference list [17], but the **negative conclusion was never carried into the design**. **New and consequential.**

#### A-04 De Man, Boerum, Leonard, King, Massenburg & Reiss, *Perceptual Evaluation of Music Mixing Practices* (AES 138th Convention, Warsaw, 2015) `[ABSTRACT-ONLY]`

- **Does:** has experienced subjects rate different mixes of different songs made with "a representative set of audio engineering tools"; correlates perceived mix quality with sonic features extracted from the mixes.
- **Reported:** "a number of features correlate with quality" — i.e. *some* feature-level signal exists at the level of tool-mediated mixes of the same material.
- **Failure cases:** the abstract does not name the features, their effect sizes, or the rater count; the design cannot separate arrangement from mix because each song differs.
- **Access:** QMUL PDF blocked (422); abstract read via Academia.edu.
- **Decision informed:** this paper and A-10 (Colonel & Reiss 2019) point in *opposite* directions — "some features correlate" vs "virtually no correlation". The reconciliation is methodological: A-04 compares *within-song, tool-limited* mixes rated by experienced subjects; A-10 compares *across 19 songs* and asks whether MIR features predict *preference*. Any feature→preference claim must therefore be within-song, within-section, and stage-matched, or it is not supported by either paper.
- **Prior-round status:** not previously cited in the repo. **New.**

#### A-05 Wakefield & Dewey, *An investigation into the efficacy of methods commonly employed by mix engineers to reduce frequency masking in the mixing of multitrack musical recordings* (AES 138th Convention, Warsaw, 2015) `[FULL-TEXT]`

- **Does:** the only controlled, blind, unlabelled-slider comparison of the three canonical de-masking techniques I could find. 11 subjects (2nd/3rd-year Music Technology/Production undergraduates, own headphones), three experiments, seven two-instrument scenarios (bass+kick, bass+lead guitar, rhythm+lead guitar, distorted+clean guitar, guitar+synth, two synths, two electric guitars), randomised order/scenario/tool. Satisfaction on a 5-point Likert; JND-style slider task.
- **Tools:** mirrored EQ (boost target, cut masker, both) at fixed frequency and Q = 7; frequency-spectrum sharing (LPF one / HPF other / both) with slope 0.7; panning (both apart vs target centre + masker panned).
- **Reported:** the tool panning **both** tracks apart was most favoured overall, "having the highest satisfaction score in all but one of the scenarios", and was **statistically significantly** favoured over all other tools. The tool that only **cut the masking track was least favoured overall** and significantly least favoured — "interesting as some mix engineers are known to favour this technique". Mirrored EQ and boost-only were statistically indistinguishable. Spectrum-sharing variants showed **no significant difference**, with the preferred variant changing per scenario.
- **Failure cases:** 11 undergraduates; fixed Q = 7 and fixed frequencies per scenario; 2-instrument reductions only; stereo monitoring unknown beyond "own headphones"; Likert not a preference-ratio scale.
- **Decision informed (large):** (i) panning is a *first-class* de-masking dimension and must be represented next to band dominance; (ii) "cut the masker" — the reflex the model will reach for — is the least preferred intervention in blind testing; (iii) filtering choices are scenario-dependent, which is an argument against a single de-masking recipe.
- **Prior-round status:** not cited anywhere in the repo. **New, and it changes what the model should be told.**

#### A-06 Meta Audiobox Aesthetics (arXiv 2502.05139, 2025) `[ABSTRACT-ONLY]` + `[REPO-CODE]`

- **Does:** no-reference per-item prediction on four axes — CE (Content Enjoyment), CU (Content Usefulness), PC (Production Complexity), PQ (Production Quality) — trained on new annotation guidelines that decompose listening perspectives.
- **Data/licence:** releases code, pre-trained weights and an evaluation dataset with per-rater annotations (the README shows 10 ratings per item). Repository and model are **CC-BY-4.0**, with portions under MIT (WavLM-derived code) `[REPO-CODE]`.
- **Size:** Hugging Face model card states **0.1B params** `[REPO-CODE]`.
- **Failure cases:** "comparable or superior" to human MOS is the paper's own claim, not independent validation; the axes are no-reference and therefore cannot see what the mix was supposed to be; PQ is a *production* axis, not a *mix-balance* axis. The project's own doc already records the practical constraint: default `precision="bf16"` will not run on SM 7.0 (V100 has no bf16), and the input is 16 kHz mono in 10 s windows (`docs/MIX-QUALITY-METHODOLOGY.ru.md:181,194`) — I could not re-confirm those two constants from the repo code (§9).
- **Decision informed:** usable as a *weak external validator* for a pre/post change (does PQ move in the expected direction?), never as a judge and never as a target. Its 16 kHz mono input discards exactly the "air" and stereo information this user cares about.
- **Prior-round status:** already in the methodology doc as a weak validator. **New:** licence is verified CC-BY-4.0 at the repo level (so it is usable in a commercial product, unlike the Flamingo path in O06), and the model is 0.1B params.

#### A-07 He, Williams & Fazenda, *Evaluating the Performance of Objective Audio Quality Metrics in Response to Common Audio Degradations* (DAFx-25, Ancona, 2025) `[FULL-TEXT]`

- **Does:** evaluates **five** full-reference metrics — PEAQ Basic, PEAQ Advanced, PEMO-Q, ViSQOL, HAAQI — on **12 audio examples** under (a) progressively increasing degradations (hum, hiss, clipping, glitches) and (b) fixed-level randomly fluctuating degradations.
- **Reported:** HAAQI, PEMO-Q and PEAQ Basic tracked degradation changes; **PEAQ Advanced failed consistently**; **ViSQOL showed low sensitivity to hum and glitches**. Robustness (average standard deviation across repeats): ViSQOL 0.004, HAAQI 0.007, PEMO-Q 0.021, PEAQ Basic 0.057, PEAQ Advanced 0.065. ViSQOL also showed "low variability across audio examples, suggesting limited genre sensitivity".
- **Licence/access:** CC-BY 4.0; code and dataset released at `harmomyshoes/Audio-Quality-Metrics-for-Music-Production` (README confirmed the paper's identity; I did not inspect the code).
- **Failure cases (mine, `[INFERENCE]`):** all five are full-reference, so they need a clean original. The user has none: the "original" is the mix. They are therefore usable only as a *degradation ladder* (insert a known defect, confirm the metric moves) or as an *edit-side* check ("did this operation cost fidelity relative to the previous render?").
- **Decision informed:** supports the project's existing degradation-matrix approach (`docs/MIX-QUALITY-METHODOLOGY.ru.md:440-461`) and specifically warns against ViSQOL for detecting the hum/glitch class and against PEAQ Advanced entirely.
- **Prior-round status:** methodology doc cites ViSQOL/PEAQ/CDPAM but not this 2025 comparison. **New:** the per-metric failure pattern and the robustness numbers.

#### A-08 Torcoli, Kastner & Herre, *Objective Measures of Perceptual Audio Quality Reviewed: An Evaluation of Their Application Domain Dependence* (IEEE/ACM TASLP 29, 2021; arXiv 2110.11438) `[ABSTRACT-ONLY]`

- **Does:** correlates 12 measures (fwS-NRseg, dLLR, PESQ, PEAQ, POLQA, PEMO-Q, ViSQOLAudio, (SI-)BSSEval, PEASS, LKR-PI, 2f-model, HAAQI) against human scores from **7 listening tests on audio coding and 7 on source separation**; introduces SI-SA2f.
- **Reported:** "one method (2f-model) performs significantly better than the others on both domains"; the training dataset and a robust underlying auditory model are "crucial factors towards a universal, domain-independent objective measure".
- **Failure cases:** even the winner is a full-reference measure; the paper's whole point is that measures are used outside their intended domain and then mislead.
- **Decision informed:** if the project ever scores separation quality or codec-like damage, use a 2f-model-family measure, not ViSQOL/PEAQ by habit. It also licenses the project's refusal to treat any single metric as a mix judge.
- **Prior-round status:** methodology doc cites PEAQ's decline [11] and DeePAQ, but not Torcoli. **New:** the domain-dependence framing and the 2f-model result.

#### A-09 ViSQOLAudio, Hines, Harte, Gillen, Kokaram, Kelly & Skoglund (JASA 137(6):EL449–EL455, 2015; DOI 10.1121/1.4921674) `[ABSTRACT-ONLY]`

- **Does:** adapts ViSQOL for music at low bitrates; benchmarks PEAQ, POLQA and ViSQOLAudio against listener tests.
- **Reported:** speech-quality metrics have "strong potential" for low-bitrate audio codec assessment.
- **Correction to the attachment:** it is **JASA 2015**, not "IEEE TBC 2017".
- **Failure cases:** codec/compression domain only; full-reference; mono/stereo handling not established from the abstract.
- **Decision informed:** ViSQOLAudio's validated domain is *codec damage*, which is one of the defect classes the project measures (`true_peak`, clipping), not tonal balance or masking.
- **Prior-round status:** methodology doc lists ViSQOL (Apache-2.0) as a "validator". **Correction:** cite JASA 2015.

#### A-10 Colonel & Reiss, *Exploring preference for multitrack mixes using statistical analysis of MIR and textual features* (AES 147th Convention, e-Brief 526, 2019) `[FULL-TEXT]`

- **Does:** extracts 33 features from verses and choruses of stereo mixdowns in the Mix Evaluation Dataset (abstract says 184 mixes across 19 songs; the methods section says 192 mixes and ~5000 evaluations); PCA; correlates with average preference and the standard deviation of preference; maps principal components to listener comments.
- **Reported:** "virtually no correlation is found between the embedded features and either average preference or standard deviation of preference"; principal-component axes did **not** correspond to simple descriptors such as "width" or "loudness" — contradicting the earlier 1501-mix Cambridge study it builds on.
- **Failure cases:** stereo mixdowns only (no per-track evidence), 33 hand-picked features, PCA linearity, preference ratings from mixed-experience listeners.
- **Decision informed (large):** do not build a feature→preference predictor and call it evaluation. Preference must be collected from humans (or from the user) and kept in a separate record kind from measurements — which is exactly the `MixObservation`/`MixClaim` separation the project already has (`mix/models.py`), and which `mix_evidence.py` correctly refuses to collapse.
- **Prior-round status:** not cited. **New, and it is the single strongest empirical argument against a quality score in the repo's own terms.**

#### A-11 Pras, De Man & Reiss, *A case study of cultural influences on mixing practices* (AES 144th Convention, Milan, 2018) `[ABSTRACT-ONLY]`

- **Does:** five Paris Conservatoire student engineers mixed the same two pop songs and documented their process; the resulting mixes were rated in a blind multi-stimulus listening test by 13 students and 1 faculty member in a high-quality room.
- **Reported:** outcomes "highlight cultural and generational mixing specificities" — i.e. both the *process* and the *preference* vary with background.
- **Failure cases:** n = 5 engineers, n = 14 raters, two pop songs; no black metal.
- **Decision informed:** a per-user reference set is not a convenience, it is a requirement; and "expert agreement" must be measured, not assumed.
- **Prior-round status:** not cited. **New.**

#### A-12 Dobrowohl, Milne & Dean, *Timbre Preferences in the Context of Mixing Music* (Appl. Sci. 9(8):1695, 2019) `[METADATA-ONLY]`

- Metadata verified (title, authors, journal, volume/article number). The MDPI PDF timed out on every fetch attempt, so **no substantive claim is made from it** (§9). It is listed in the ledger only to record that the attachment's citation is real and correctly attributed.

### 2.2 Auditory texture

#### A-13 McDermott & Simoncelli, *Sound Texture Perception via Statistics of the Auditory Periphery: Evidence from Sound Synthesis* (Neuron 71(5), 2011) `[ABSTRACT-ONLY]`

- **Correction to the attachment:** **Neuron**, not PNAS.
- **Does:** processes real-world textures through an auditory model with frequency filters and modulation filters, measures statistics of that decomposition, and synthesises novel sounds matched to those statistics.
- **Reported:** statistics of individual frequency channels (spectral power, sparsity) "generally failed to produce compelling synthetic textures"; adding **correlations between channels** produced identifiable, natural-sounding textures; synthesis quality declined when statistics came from biologically implausible auditory models (e.g. linearly spaced filters).
- **Failure cases:** the study is about *synthesis validity*, not about measuring a mix; it says nothing about masking between musical sources.
- **Decision informed:** if the project wants a texture descriptor, band levels alone are provably insufficient — cross-band correlation is the ingredient that makes the statistic behave like the percept. The current per-cell `bands` (28 numbers) is the "individual frequency channels" case that the paper found insufficient.
- **Prior-round status:** not cited. **New.**

#### A-14 McDermott, Schemitsch & Simoncelli, *Summary statistics in auditory perception* (Nature Neuroscience 16(4):493–498, 2013) `[METADATA-ONLY]`

- Metadata verified (journal, volume, pages, date 24 Feb 2013). I did not read its abstract; no claim beyond existence and topic (§9).

#### A-15 McWalter & Dau, *Cascaded Amplitude Modulations in Sound Texture Perception* (Front. Neurosci. 11:485, 2017) `[FULL-TEXT]` (introduction)

- **Does:** extends the texture-statistics space into amplitude modulation, including modulations *of* modulations (cascaded AM).
- **Reported (intro):** the 2011 synthesis system's statistics were "requisite for different sound textures"; the paper tests what else the auditory system represents via time-averaging, specifically in the AM domain.
- **Decision informed:** a texture descriptor should include at least one modulation band (envelope rate/depth) and ideally a second-order term (modulation of the modulation), because that is where the perceptual difference between "shimmering cymbal wash" and "static fuzz" lives.
- **Prior-round status:** not cited. **New.**

#### A-16 McWalter & McDermott, *Illusory sound texture reveals multi-second statistical completion in auditory scene analysis* (Nature Communications 10:5096, 2019) `[ABSTRACT-ONLY]`

- **Does:** demonstrates filling-in of a background texture while it is interrupted by another sound.
- **Reported:** the effect "lasts much longer" than classical completion effects — several seconds — "enabling introspection"; illusory texture "biases subsequent texture statistic estimates indistinguishably from actual texture".
- **Failure cases:** laboratory textures (rain, fire, swarms), not music.
- **Decision informed (important):** a listener hearing a 4-second section with a guitar masking a cymbal bed may perceive the bed as continuing, so a 100 ms window measurement can report masking that is not heard, and can miss masking that is. **Texture/clarity claims should be computed over multi-second windows** (I propose 2–4 s, §4.4) and the model should be told the window.
- **Prior-round status:** not cited. **New, and it changes the analysis window.**

### 2.3 Source separation and instrument-wise FX representation

#### A-17 Rouard, Massa & Défossez, *Hybrid Transformers for Music Source Separation* (arXiv 2211.08553, 2022) / Demucs v4 `[REPO-CODE]`

- **Does:** hybrid spectrogram/waveform separator; v4 replaces the innermost layers with a cross-domain Transformer (self-attention within domain, cross-attention across).
- **Reported (official README):** **SDR 9.00 dB on MUSDB HQ test**; trained on MUSDB HQ + an extra 800 songs; separates drums, bass, vocals and "other".
- **Licence:** repository **MIT**. **Maintenance:** the Meta repo is explicitly unmaintained; the author's fork `adefossez/demucs` receives only important bug fixes.
- **Failure cases for this project:** the output taxonomy is 4-stem. In black metal, "other" = all guitars + cymbals + keys + any orchestration; "vocals" = scream + clean female vocal merged. There is no "guitar" stem and no per-instrument FX.
- **Compute:** the README does not report VRAM or throughput (§9). Do not quote a VRAM figure from memory.
- **Decision informed:** htdemucs is a legitimate *temporary* tool for "how much energy is in drums vs everything else in this section" and for bleed-aware sanity checks. It cannot support any per-guitar or per-cymbal claim.
- **Prior-round status:** `docs/GAP-ANALYSIS.md:391` already says "htdemucs gives 4 stems, but without bleed calibration it is noise; guitars/keys/accordion are not separated". **Confirmed by the repo's own stated stem set. No change.**

#### A-18 Lu, Wang, Kong & Hung, *Music Source Separation with Band-Split RoPE Transformer* (ICASSP 2024; arXiv 2309.02612) `[ABSTRACT-ONLY]`

- **Reported:** BS-RoFormer trained on MUSDB18HQ + 500 extra songs ranked **first** in the MSS track of the Sound Demixing Challenge 2023; a smaller version trained on MUSDB18HQ alone reaches **9.80 dB average SDR**.
- **Licence:** paper CC-BY 4.0. **Checkpoint licence not established** (§9) — this matters because "wins a challenge" and "may be redistributed in a commercial product" are different properties.
- **Decision informed:** if separation quality matters, BS-RoFormer is the current reference point (9.80 vs htdemucs 9.00 dB on the same benchmark). Both are 4-stem.
- **Prior-round status:** not cited. **New.**

#### A-19 Luo & Yu, *Music Source Separation with Band-split RNN* (arXiv 2209.15174; TASLP 2023) `[METADATA-ONLY]`

- Metadata verified (arXiv ID, authors, title). It is the architectural precursor of the band-split line; **no substantive claim is made** (§9).

#### A-20 Yeh, Koo, Martínez-Ramírez, Liao, Yang & Mitsufuji, *Fx-Encoder++: Extracting Instrument-Wise Audio Effects Representations from Mixtures* (arXiv 2507.02273, 2025) `[FULL-TEXT]`

- **Does:** contrastive (SimCLR-style) representation learning on mixtures, plus an "extractor" that turns mixture-level FX embeddings into instrument-wise embeddings given an instrument query (audio **or text**, via a pretrained CLAP encoder).
- **Training data:** synthetic. Effects pool = EQ, delay, distortion, stereo imager, compression, limiter, reverb, gain; random chains (order, number, types, parameters) applied to stems split into two segments; mixtures built with per-track loudness normalisation −18…−14 LUFS and −18 LUFS for the mixture. Evaluation: retrieval from **500 candidates**; parameter matching on **100 samples per instrument** synthesised from MUSDB and MedleyDB with **seven sequential effects** (EQ, multiband compressor, stereo imager, gain, distortion, delay, limiter).
- **Reported:** outperforms previous approaches at mixture level and adds instrument-wise extraction; text queries slightly better than audio queries.
- **Failure cases (stated):** "reduced effect parameter matching performance, and difficulties with single effect understanding". Also: the extractor requires you to supply the instrument — i.e. it presumes a separation you already trust.
- **Licence:** paper CC-BY 4.0; **weights/code licence not stated in the paper** (§9).
- **Decision informed:** Fx-Encoder++ can at best answer "does guitar A's processing resemble guitar B's?" in an embedding space, and even that is trained on synthetic series chains, not amp-sim + IR + pedal chains. It cannot return knob values; it cannot identify which of the 4 guitars has more gain; and it degrades exactly where this user's chain is most complex (single dominant effect, e.g. a fuzz pedal into a dirty amp).
- **Prior-round status:** the attachment lists it under both workstreams. **Boundary:** I assess representation only; any action use is Workstream B's call.

### 2.4 Datasets and evaluation corpora

#### A-21 De Man & Reiss, *The Mix Evaluation Dataset* (DAFx-17) `[FULL-TEXT]` (abstract + introduction)

- **Contents:** 180 mixes (the 2019 e-Brief says 184/192 across 19 songs), **parameter settings**, close to **5000 preference ratings**, free-form descriptions, contributors from **five countries**; gathered in a "real-life, ecologically valid setting".
- **Reported:** more experienced subjects were "more negative and more specific" and "increasingly agree with each other".
- **Licence/access:** the DAFx paper describes the dataset but the paper is not a licence grant. The Mix Evaluation Browser (per C4DM) exposes mixes + DAW files + raw tracks + ratings.
- **Decision informed (two things):** (i) expert ratings are systematically harsher and more reliable than novice ratings — so any evaluation panel must be experience-stratified and agreement reported; (ii) the dataset contains *DAW files and parameter settings*, which is the only public source of "what engineers actually did" at scale that I verified.
- **Prior-round status:** methodology doc cites it [20] for MED 2.0. **New:** the expertise effect, which is directly usable in the experiment design (§6).

#### A-22 `sparrowreivun/Mix-Evaluation-Dataset-2.0` (Hugging Face) `[DATASET-CARD]`

- **What it is:** a third-party re-upload of the Mix Evaluation Dataset, songs `ColdStar`, `GoodTime`, `HighBloodPressure`, `IdLikeToKnow`, … as **MP3** files (`MixEvaluation/audio/<song>/<mix>.mp3`).
- **Metadata verified via the HF API:** `"gated": false`, `"private": false`, `"tags": ["region:us"]`, `lastModified 2026-10-05`, ~1198 downloads. **There is no licence tag and no licence file in the listing.**
- **Problems:** (i) uploader is not the dataset's authors; (ii) **no declared licence**; (iii) audio is lossy MP3 — measuring LUFS, band levels or true peak on these files measures the codec as well as the mix; (iv) the HF dataset viewer is broken for this repo (`FeaturesError: FileNotFoundError`), so schema/labels cannot be inspected without downloading.
- **Decision informed:** usable for *exploration* of preference structure, **not** for spectral measurement or as a redistributable corpus in the product. If MED 2.0 is to be used, obtain it from the authors and record the licence.
- **Prior-round status:** the methodology doc recommends MED 2.0 as the external validation set (`:437,:465`). **Correction:** the HF mirror is not a safe substrate for measurement; this materially weakens that plan until an official, lossless, licensed copy is obtained.

#### A-23 ODAQ — Open Dataset of Audio Quality, Fraunhofer-IIS `[REPO-CODE]` + `[ABSTRACT-ONLY]`

- **Contents:** **240** audio samples, **26 expert listeners** after post-screening, **6 method classes × 5 quality levels** plus anchors; originals are stereo 44.1/48 kHz, **14 music excerpts (8 solo)** and **11 movie-like** excerpts with separate stems and transcripts. MUSHRA run in parallel at Fraunhofer IIS and Netflix.
- **Licence:** "released with permissive licenses"; dataset on Zenodo DOI 10.5281/zenodo.10405774; additional scores at 10.5281/zenodo.13377284; listening-test software public (ICASSP 2024 paper, arXiv 2401.00197).
- **Suitability:** degradations are those "encountered during audio coding and source separation" — i.e. it is a *quality-metric calibration* corpus, not a mixing corpus. No metal.
- **Decision informed:** the right use is to calibrate/validate whatever objective measure the project uses for separation or codec damage (2f-model, HAAQI, ViSQOL, PEMO-Q), and to check that the measure responds monotonically across 5 quality levels. It cannot tell you anything about blend or balance.
- **Prior-round status:** methodology doc cites ODAQ [22] only for "stereo degradations and metric limits". **New:** exact size/listener counts/licences, and the fact that it contains solo music excerpts — which makes it usable for per-instrument quality checks, unlike full mixes.

#### A-24 MedleyDB `[METADATA-ONLY]`

- Metadata verified: Bittner, Salamon, Tierney, Mauch, Cannam, Bello; ISMIR 2014; the download page states it is "offered free of charge for non-commercial" use. Multitrack with instrument annotations, mostly acoustic/rock/pop; no metal. **No substantive claim made** (§9).

#### A-25 Slakh2100 `[DATASET-CARD]`

- **Contents:** 2100 automatically mixed tracks with isolated instrument stems and aligned MIDI, **synthesised** from MIDI (not recorded performances).
- **Licence:** **CC BY 4.0** (Zenodo 10.5281/zenodo.4599665, 2019; Manilow, Wichern, Seetharaman, Le Roux).
- **Suitability:** excellent for pipeline tests (you know the exact ground truth stems, gains and MIDI) and *useless* for realism claims: no room, no bleed, no amp, no performance. It cannot validate masking on distorted guitars.
- **Decision informed:** use as the deterministic fixture generator for reconstruction tests (§6), not as evidence about mixes.
- **Prior-round status:** not cited. **New.**

#### A-26 Cambridge-MT "Mixing Secrets" multitrack library `[FULL-TEXT]` (FAQ)

- **Licence reality:** the FAQ is explicit — using mixes in a portfolio is "not permitted under the terms"; video tutorials are fine only if entirely not-for-profit; **research use "hasn't been specifically agreed with contributors"** and requires contacting them, "especially if your research is for commercial purposes".
- **Decision informed:** not a corpus for a product. Usable only as personal study material or after per-track permission. Any benchmark built on it cannot be shipped or published as audio.
- **Prior-round status:** the repo already used Cambridge multitracks indirectly (the 1501-mix study it cites). **Correction:** the licence position must be recorded before any benchmark uses it.

#### A-27 Tian, Lattner & Saitis, *Assessing the Alignment of Audio Representations With Timbre Similarity Ratings* (ISMIR 2025, pp. 710–718; Zenodo 17811470) `[DATASET-CARD]`

- **Metadata verified on the Zenodo record:** conference paper, ISMIR 2025, Daejeon; **CC-BY-4.0**; version v2, created 2025-12-04, modified 2026-02-15; also arXiv 2507.07764.
- **Why it matters:** it is the current reference for *how well audio embeddings track human timbre-similarity judgements* — the exact question behind "does guitar A sound like guitar B / like the reference?". I did not read its results, so I make no claim about which embedding wins (§9).
- **Decision informed:** if the project wants similarity evidence (guitar tone vs reference guitar), the evaluation protocol should follow this line of work (human similarity ratings as ground truth), not cosine distance between CLAP embeddings asserted as meaning.

### 2.5 End-to-end mixing studies

#### A-28 Martínez-Ramírez, Liao, Fabbro, Uhlich, Nagashima & Mitsufuji, *Automatic Music Mixing with Deep Learning and Out-of-Domain Data* (ISMIR 2022) `[FULL-TEXT]`

- **Does:** trains a deep automatic mixer on **out-of-domain wet/processed** multitrack data by normalising effects per class (loudness, EQ, panning, DRC, reverb) and applying the inverse at inference to dry stems; explores stereo-invariant losses.
- **Reported:** listening tests with "highly experienced mixing engineers" show the approach is "indistinguishable from professional human-made mixes". Code and audio released (`FxNorm-automix` project page).
- **Failure cases:** it outputs a *mixture*, not parameters; it is trained on MSS-style data with a fixed 5-class effect vocabulary; it cannot tell you which of four guitars to move or by how much; and "indistinguishable in a listening test" is a claim about preference, not about matching a target mix.
- **Decision informed:** this is the ceiling of the black-box path and simultaneously the reason it does not solve this user's problem: the product must produce *editable REAPER state with a reason*, and a black-box mixer produces a file.
- **Prior-round status:** not cited in the repo. **New.**

#### A-29 Jillings, *Automating the Production of the Balance Mix in Music Production* (PhD, Birmingham City University; submitted May 2022, awarded 2023) `[ABSTRACT-ONLY]`

- **Does:** builds a browser production environment to collect session data while subjects construct a *balance mix*; then builds assistive tools: semantic grouping/labelling of tracks, and an automatic masking-minimising balance tool using **evolutionary computing** as the solver.
- **Reported:** grouping "proved to be one of the most important, yet overlooked tasks"; "the minimisation of masking is a primary task of the mixing stage"; the systems produce a session structure similar to a real engineer's.
- **Decision informed:** two corroborations — (i) grouping/subgrouping precedes balance, which is also Workstream B's panning/grouping ground; (ii) masking reduction can be posed as *search over gains* without a trained model, which is the cheapest honest way to answer "what would happen if I moved this fader" — but it needs a render loop (my dependency B-1).
- **Prior-round status:** not cited. **New.**

#### A-30 Koszewski, Görne, Korvel & Kostek, *Automatic music signal mixing system based on one-dimensional Wave-U-Net autoencoders* (EURASIP JASMP 2023:1) `[METADATA-ONLY]`

- Metadata verified (journal, volume, article 1, DOI 10.1186/s13636-022-00266-3, open access). No substantive claim (§9). It is listed because the attachment cites it and the citation is real.

#### A-31 Stickla & Athauda, *A new audio mixing paradigm: evaluation from professional practitioners' perspectives* (Creative Industries Journal; DOI 10.1080/17510694.2022.2088164; online 21 Jun 2022) `[METADATA-ONLY]`

- **Correction to the attachment:** it is a **2022** online publication in a creative-industries journal (issue dated 2024), not a 2024 audio-engineering study; its subject is practitioner evaluation of a (cloud/collaborative) mixing paradigm, not mix-quality measurement. No substantive claim (§9).

---

## 3. (b) Current-project gap map

Requirement → current implementation → what the tests actually verify → gap → evidence.

| # | Requirement (from project intent) | Current implementation | Verified in tests | Gap | Evidence |
|---|---|---|---|---|---|
| G1 | "Masking" evidence must be interpretable and honestly labelled | `temporal.masking()` compares band levels between two sources in the same 100 ms cell, after centring each cell by its own broadband mean, only where both exceed −80 dB, and reports the max **median** band difference ≥ `min_db=10.0` | Only that `masking_evidence` exists and its interpretation string starts with "Relative band-level dominance heuristic" (`tests/test_analysis_bundle.py:34-37`). **No numeric test of the semantics.** | No pan, no level, no threshold, no tonality, no temporal masking, no binaural term; and the name still says "masking" in the payload keys (`masker`, `target`) | `temporal.py:771-835`; `temporal.py:794-797` (centring); `:811` (both-active gate); `:822-831` (max median); `bundle.py:277-280` |
| G2 | Severity must not encode a recipe | `build_mix_evidence(masking_db=6.0)` filters pairs at `db < 6.0` and sets `severity = min(3, db/6)` | A synthetic pair at `db: 9.0` passes (`tests/test_mix_evidence.py:18-19`) | **The 6.0 dB filter is inert**: `masking()` never emits a pair below 10 dB (`temporal.py:822-831`), so `masking_db=6.0` can never bind. The test only passes because it fabricates a 9 dB pair the real pipeline cannot produce. Severity at the true floor (10 dB) is 1.67, saturating at 18 dB | `mix_evidence.py:34-36,43-58`; `temporal.py:771-772`; `tests/test_mix_evidence.py:19` |
| G3 | No source should be flagged by arithmetic accident | `partial_audibility` fires when `share_of_partial_loudness_pct < 5.0` or `> 45.0` | Nothing tests the multi-source case; `tests/test_partial_loudness.py:35-53` uses 2 sources and asserts only that shares sum to 100 | With *n* equal sources the mean share is 100/n %; at **n = 41** (the measured 42-source capture minus master) the mean is **2.44 %**, so **every** source falls below 5 % and the report fills with 40 identical-severity rows (severity ≈ 1.76) before the `max_issues=40` cap | `mix_evidence.py:84-98,126-131`; `partial.py:320-324`; `docs/FULL-CAPTURE.md:97-104` (42 sources) `[INFERENCE for the arithmetic]` |
| G4 | Partial loudness must mean what its name says | `attribute_partial_loudness` subtracts the source's band power from the mix's band power, floors the residual at −60 dB, re-runs the loudness model on the residual, and reports `partial_sones` and `share_of_partial_loudness_pct` | Mix is the exact sum of stems in every test (`tests/test_partial_loudness.py:19-32,35-49`); alignment is rejected (`:56-61`) | In production the "mix" is a **post-master render** and the "sources" are **solo track renders that exclude sends, bus processing and master processing**; `sum(stems) ≠ mix` by construction. Where a source exceeds the mix in a band, the residual hits the floor and `relative_audibility` → 1.0 for that source | `partial.py:178-195` (docstring admits the nonlinearity), `:246-249` (subtraction), `:281-301`; `docs/GAP-ANALYSIS.md:189-199` |
| G5 | Reference comparison must not be a target | `reference_corridor` returns median + MAD per metric across references; `mix_evidence` turns `|delta|/max(MAD, 0.5 dB) ≥ 2` into a `reference_delta` question | Shape only; no test ties corridor rows to stage/section/loudness match | No stage matching (mix vs master), no section matching, no loudness matching, no reference count reported to the model at decision time; `n` is in the row but `reference_sigma=2.0` ignores it (with n = 3 a 2-MAD excursion is not evidence) | `compare.py:56-92`; `mix_evidence.py:60-82`; `docs/MIX-QUALITY-METHODOLOGY.ru.md:525-531` (stage confound) |
| G6 | The whole-project view must contain relational audio evidence | `panorama.build_panorama` emits 9 per-source metrics (`ANALYSIS_METRIC_KEYS`), band-level curves (summarised), and **structural** crosslinks only | Extensive structural/budget/determinism tests (`tests/test_capture_panorama.py:395-694`) | No `masked_by`, no `contributes_to`, no `differs_from_reference`, no texture node, no claim node. `epistemic_layers.model_interpretations` and `causal_hypotheses` are permanently empty. Relations are integer-coded (`relation: 3`) | `panorama.py:39-45`; `:1994-2003`; `:2086-2150`; `:2165-2206` |
| G7 | The model must see the analysis it is being asked to reason about | `reaper_mix_evidence` rebuilds evidence **on demand** from the in-memory report, separately from the panorama | Tool-level tests exist; the panorama has no mix-evidence block | Two different model-facing artefacts describe the same capture with different vocabularies; a client that only reads `reaper_capture_snapshot` never sees masking, partial loudness, phase or references | `mcp_server.py:868-892`; `panorama.py:2011-2150` |
| G8 | Texture-like layers must be describable | Per-cell `mod_rate_hz`, `mod_depth`, `decay_bands`, `coherence`, `erb` are computed and serialised | None of these are read anywhere outside `temporal.py` (documented at `docs/GAP-ANALYSIS.md:164-170`) | The statistics that the texture literature identifies as necessary (cross-band correlation, modulation) exist but are unreachable; the compaction step drops them before the model sees them | `temporal.py:406-423`; `docs/GAP-ANALYSIS.md:164-176` |
| G9 | No retained per-track stems | Assets are manifest-owned with a lease; `reaper_release` deletes them; hashes remain | Capture/release tests pass (`tests/test_full_capture.py`, `tests/test_wave_c_capture.py`) | No place in the contract currently *permits* a derived per-source separation product, even transiently; if separation is added it must reuse the same lease machinery, not a new directory | `analysis/artifacts.py:18,48-260`; `docs/research/round-4/CONTEXT-LEDGER-DESIGN.md:306-345` |
| G10 | Tool surface must match documentation | **21 tools** registered in `mcp_server.py` (I counted the `@server.tool()` decorators), **6 resources** | `tests/test_mcp_server.py:110` asserts `len(names) == 21` | Round-3 `DECISION-LEDGER.md:41` (D22) still says 19 tools / 6 resources and is **stale**; `CONTEXT-LEDGER-DESIGN.md:39-44` repeats the 12-vs-19 confusion. The capture tools (`reaper_capture_snapshot/read`) are the two that pushed 19 → 21 | `mcp_server.py` (21 decorators); `tests/test_mcp_server.py:110`; `docs/research/round-3/DECISION-LEDGER.md:41` |
| G11 | Model results must be bound to a capture | `reaper_flamingo_*` / `reaper_spatial_*` re-render and do not accept `analysis_id` | Documented as a boundary, not a test | A model description and a DSP measurement can refer to different renders/revisions with no visible link | `docs/research/round-4/WAVE-D-RESULTS-AND-NEXT-ACTIONS.md:57-78` |

**What is already answered and must not be re-litigated:** G1's semantic critique (`GAP-ANALYSIS.md:178-187`), G4's input-contract critique (`GAP-ANALYSIS.md:189-199`), G5's stage/section mismatch (`GAP-ANALYSIS.md:201-217`), and G2's "constants act as judge" (`GAP-ANALYSIS.md:376-383`). Those sections are correct against today's code. The new material in this report is: the inert-threshold proof (G2), the 41-source share arithmetic (G3), the pan/texture/time-scale argument with literature (§2.1, §2.2), and the "no relational edges at all" finding (G6).

**Stale items to correct (not defects of the in-flight performance work):**

- `docs/research/round-3/DECISION-LEDGER.md:41` (19 tools) — now 21.
- `docs/GAP-ANALYSIS.md:98,180` cites `temporal.py:661,725` for `masking`; after the performance rewrite the function is at `:771-835`. The *claim* is still true; the line reference has moved.
- `docs/MIX-QUALITY-METHODOLOGY.ru.md:29` presents ρ ≈ 0.84 as "the only construct with a published number" without the sign convention or the variant ranking; A-01/A-02 refine this.

---

## 4. (c) Recommended representation and analysis design

### 4.1 Four measurement classes, and what each may support

| Class | Examples | May support | May **not** support |
|---|---|---|---|
| **Signal facts** (grade A) | peak, true-peak estimate, RMS, crest, LUFS (when ≥ 3 s and stereo/mono), LRA (≥ 60 s), band levels, side/mid, correlation, mono-fold loss, DC, clip runs | "This is what the file contains"; defect gates; identity/hash checks | Any claim about what is heard |
| **Psychoacoustic proxies** (grade B only after calibration) | Zwicker/Moore loudness series, partial-loudness residual, sharpness, band dominance, texture statistics | Directional statements *with a stated model, window and calibration*: "under ISO 532-2 at a 90 dB SPL reference, this source contributes X % of modelled partial loudness in this band" | Threshold claims without a declared playback calibration; cross-source comparisons when the mix is not reconstructed by its stems |
| **Model inference** (grade C) | Music Flamingo / Ossicle descriptions, Fx-Encoder++ embeddings, any separation output | Hypotheses, retrieval, "sounds like" evidence | Verified measurement; attribution to a plugin or a parameter |
| **Subjective judgement** (grade E) | user's preference, expert ratings, ABX results | Goals, acceptance, "is this the character I want" | Anything the library computes |

The project already encodes this as A–F grades (`mix/models.py`, summarised in `DECISION-LEDGER.md:59`). The point of this section is that **band dominance and partial loudness currently sit in the wrong class**: they are reported with grade-A certainty ("masker", "share") while being grade-B-at-best constructs.

### 4.2 Where masking/clarity features add information beyond the current snapshot

They add information only in three narrow situations:

1. **Same-side competition with a level asymmetry and a known arrangement role.** Two rhythm guitars hard left, one 8 dB louder in 200–500 Hz across a whole riff: that is a real, checkable statement about the *arrangement's* spectral budget, and no other current field captures it (the panorama has per-source band levels but no relation between them).
2. **A change over time within one source.** "The 315 Hz band of guitar L rises 6 dB when the second layer enters at bar 33" is measurable and actionable; it needs no masking model at all, only aligned per-source curves.
3. **A defect gate.** Band dominance is a legitimate *trigger* for the model to look closer (and to ask for a solo'd listen), not a conclusion.

They add nothing — and actively mislead — when:

- the two sources are on opposite sides of the stereo image (binaural unmasking is not modelled; the analysis mono-averages: `temporal.py:329`);
- the material is dense and noise-like, where "tonality" is low and band differences are large everywhere (fuzz, cymbal wash, blast beats);
- the comparison is between a solo render and the master (the master is a different signal, not a sum);
- the band is below the threshold in quiet at a realistic monitoring level (25–50 Hz), where a 10 dB "dominance" may be inaudible.

### 4.3 Concrete failure modes for this user's material

| Situation | What the current numbers do | Why it diverges from hearing |
|---|---|---|
| **4 hard-panned distorted guitars (2 L, 2 R)** | Compares mono-downmixed band levels; cross-side pairs look like strong competition | Opposite-side sources are largely unmasked by lateralisation; same-side pairs are the real competition. Wakefield & Dewey's blind test put panning ahead of every EQ strategy `[FULL-TEXT]` |
| **Fuzz / high-gain layers** | Reports large low-mid dominance between guitars, and between guitars and bass | Distortion fills inter-harmonic gaps; the percept is *fusion* plus attack alignment, not band competition. Leave-one-out power subtraction is invalid because the master is nonlinear |
| **Cymbal wash vs guitar presence (3–8 kHz)** | Always reports a large difference between drums and guitars in that region | The ear separates by attack envelope and by stereo position (overheads/room), neither of which is in a band-level cell |
| **Double-kick vs bass (60–120 Hz + 2–5 kHz click)** | Reports permanent low-band dominance | Audible separation comes from the beater click and from sidechain ducking; both are temporal/processing facts invisible to band statistics |
| **Double-tracked guitars (decorrelated pair)** | A mono band-level comparison of two partially decorrelated performances reports a level change that is really a comb-filter/phase effect | Decorrelation is what makes a double "bigger"; `phase.py` exists and is the right tool, but `mix_evidence`'s `corr < 0.2` threshold is arbitrary (`mix_evidence.py:100-112`) |
| **Reverb/delay returns** | Not in per-track solo renders at all; therefore invisible to both masking and partial loudness | Reverb is a *primary* clarity mechanism; the capital error is measuring clarity on stems that do not contain the space |
| **Any band below ~50 Hz** | Differences of 10–20 dB reported | At normal monitoring levels this region is near or below the threshold of hearing; without a declared playback calibration the number is unfalsifiable |

### 4.4 The analysis window question (new, from the texture literature)

Every current cell is 100 ms with a 93 ms window (`docs/GAP-ANALYSIS.md:175-176`). The texture evidence says two things: the *statistics* that carry texture are time-averaged over much longer spans, and the *percept* completes an interrupted texture over several seconds `[ABSTRACT-ONLY A-16]`. Therefore:

- **Masking/dominance:** keep 100 ms cells for *localisation* (where in time), but aggregate to a **section-level** statement (≥ 2 s) before it becomes a claim. A cell-level claim should never reach the model without its duration and active fraction.
- **Texture:** compute over **2–4 s non-overlapping windows** (design choice; the exact value needs listening calibration, §7).
- **Never** present a per-cell maximum as the section's character; that is exactly how a single 100 ms coincidence becomes a global verdict.

### 4.5 Illustrative schema fragment (additive; the full capture is unchanged)

This is one block added to the panorama, plus one claim record. Everything is resolvable without a second tool call; dense series stay behind pointers with hashes, as today (`FULL-CAPTURE.md:83-93`).

```json
{
  "audio_relations": {
    "schema": "reaper-workbench.audio-relations/1",
    "producer": {"name": "reaper_audio.analysis.temporal.masking", "version": "1",
                 "params_sha256": "sha256:3f9c…", "is_psychoacoustic_threshold": false},
    "relations": [
      {
        "relation_id": "arel_01J8ZQ4T7K",
        "kind": "band_dominance",
        "from": {"id": "track:<guid-A>", "role_hint": null, "pan": -1.0},
        "to":   {"id": "track:<guid-B>", "role_hint": null, "pan": -0.85},
        "scope": {"window": {"start_s": 41.0, "end_s": 49.0, "basis": "project_time"},
                  "band_hz": 315.0, "bandwidth": "1/3 octave",
                  "cells_total": 80, "cells_active": 76, "active_fraction": 0.95},
        "measured": {"band_delta_median_db": 11.4, "broadband_delta_db": 0.8,
                     "pan_relation": "same_side", "stage": "track_stem_post_fx",
                     "channel_view": "mono_power_average"},
        "state": "fresh",
        "grade": "A_measured_dsp",
        "confidence": {"basis": "not_calibrated", "value": null,
                       "note": "level dominance only; no threshold, no tonality, no binaural term"},
        "caveats": ["mono_downmix_ignores_binaural_unmasking",
                    "no_threshold_in_quiet_without_playback_calibration",
                    "no_temporal_masking",
                    "invalid_when_mix_is_not_reconstructed_by_stems"],
        "evidence": [
          {"asset": "ast_sha256:<64hex>", "sha256": "<64hex>", "source_id": "track:<guid-A>"},
          {"asset": "ast_sha256:<64hex>", "sha256": "<64hex>", "source_id": "track:<guid-B>"}
        ],
        "revision": {"project_id": "prj_…", "instance_id": "pri_…", "epoch": 2,
                     "counter": 417, "fingerprint": "sha256:…", "fingerprint_scope": "full"}
      }
    ],
    "omitted": {"relations_total": 143, "returned": 8, "policy": "top_k_per_source=8",
                "next": "reaper://ledger/{instance}/{project}/views/audio_relations"}
  },

  "texture": {
    "method": {"name": "band_covariance_modulation/1", "version": "1"},
    "window_s": 2.0,
    "sources": [
      {"source_id": "track:<guid-cymbals>", "window": {"start_s": 41.0, "end_s": 49.0},
       "bands_hz": [250, 500, 1000, 2000, 4000, 8000],
       "band_level_dbfs": [-31.2, -28.7, -30.1, -27.4, -29.9, -33.0],
       "cross_band_corr": {"250-500": 0.62, "500-1000": 0.44, "1000-2000": 0.31,
                            "2000-4000": 0.57, "4000-8000": 0.71},
       "modulation": {"rate_hz": 7.8, "depth": 0.34, "cascaded_depth": 0.11},
       "state": "fresh", "grade": "A_measured_dsp",
       "caveats": ["summary_statistics_only", "no_event_identity"]}
    ]
  },

  "claims": [
    {
      "claim_id": "clm_01J8ZR2M4P",
      "statement": "Guitar L2 is 11.4 dB stronger than L1 at 315 Hz for bars 17-21; they share a pan side.",
      "construct_ids": ["c_band_dominance_lowmid"],
      "grade": "A_measured_dsp",
      "confidence": {"basis": "measured_delta_only", "value": null},
      "alternatives": [
        "intentional doubling weight",
        "arrangement change (second layer enters)",
        "measurement artefact from mono downmix"
      ],
      "supports": ["arel_01J8ZQ4T7K"],
      "against": [],
      "discriminates": "Solo L1 and L2 for bars 17-21 and compare attack alignment and low-mid level before any EQ.",
      "dep_set": ["rev_…", "trk_A", "trk_B", "ast_sha256:…", "ast_sha256:…"],
      "scope_set": ["tracks", "items", "master"],
      "revision": {"fingerprint": "sha256:…", "fingerprint_scope": "full"},
      "state": "fresh",
      "not_a_verdict": true
    }
  ]
}
```

Design notes that matter more than the field names:

- `is_psychoacoustic_threshold: false` travels *with the method*, so a client cannot mistake the number for a threshold even if it never reads `caveats`.
- `confidence.value: null` with `basis` is deliberate: the project requires confidence to have a basis/calibration rather than be an arbitrary number (`docs/research/round-2/00-COMMON-CONTRACT.md:46`), and forbids causal statements without alternatives and a discriminating test (D14, `DECISION-LEDGER.md:33`); a null with a reason is honest.
- `pan_relation` is computed from the project map (`panorama.py:1627`), not from audio, and is labelled as such.
- `dep_set`/`scope_set` reuse the digest vocabulary (`context/revision.py:80-83`) so invalidation can be activated without a new taxonomy.
- The `residual`/`reconstruction` gate belongs on the partial-loudness node: `{"reconstruction": {"status": "failed", "per_band_median_abs_db": 4.2, "p95_abs_db": 9.7, "tolerance_db": 1.0}}` — and when it fails, **no** share percentage is emitted.

### 4.6 Cost of the addition (measured inputs, my arithmetic)

| Item | Value | Source |
|---|---|---|
| Full-song panorama | 237,477 bytes / 128,449 tokens, 300.52 s, 42 sources | `FULL-CAPTURE.md:97-100` |
| 20 s panorama | 198,480 bytes / 92,307 tokens; full MCP response 93,099 tokens | `FULL-CAPTURE.md:100-104` |
| Budget | 700,000 bytes / 256,000 tokens | `panorama.py:31,36` |
| All source pairs at 42 sources | 861 pairs; ≈ 189 KB at ~220 B/edge | `[INFERENCE]` |
| Top-8-per-source edges | ≤ 168 edges; ≈ 37 KB = 5.3 % of the byte budget | `[INFERENCE]` |
| Tokens per source-second, 20 s capture | ≈ 110.8 | `[INFERENCE]` from 93,099/(20×42) |
| Tokens per source-second, full song | ≈ 10.2 (columnar summaries) | `[INFERENCE]` from 128,449/(300.52×42) |

Two consequences: (i) **never** emit all pairs — 861 edges would consume 27 % of the byte budget and most of the remaining token headroom; (ii) **short captures are ~11× more expensive per source-second than long ones**, so a 20 s drill-down must be a deliberate, budgeted request, not the default.

### 4.7 Compute plan: capture-time vs on-demand

| Stage | What runs | Cost driver | When |
|---|---|---|---|
| **Per capture, always (DSP only)** | `measure_audio` per source: levels, LUFS/LRA, 28 band levels + curves, transients, events, tempo, stereo; structural digest | O(N) per source; already the dominant cost | Every capture |
| **Per capture, always (cheap relations)** | Band dominance over < 1000-point cell grids; per-source pan relation from the project map; reconstruction residual per band | O(S²·B·cells) but vectorised; the current code already does the pair loop (`temporal.py:801-831`) | Every capture, but **emit only top-K** |
| **Per capture, conditional** | Partial loudness | O(S · F · model evaluations), F = duration/hop (0.2 s). At 20 s, F = 100, so 41 sources ≈ 16.4 k model evaluations (mix + rest + solo + sensitivity per source-frame) | Only when `include_partial_loudness` **and** the reconstruction gate passes on the requested section |
| **Per capture, cheap addition** | Texture block over 2–4 s windows (band covariance, modulation rate/depth) | O(N) FFTs per window; small | Every capture, first 8–16 sources by energy, or all if budget allows |
| **On demand, per section** | Full-resolution curves, cell-level dominance, per-band side/mid, phase pairs | Section length | Model asks for a region |
| **On demand, per reference** | Reference corridor with stage/section/loudness matching | O(R) per metric | Model asks, or a policy says "compare to corpus" |
| **Model-assisted, optional** | Audiobox PQ/CE (weak validator), Flamingo/Ossicle descriptions, Fx-Encoder++ embeddings | GPU, one model at a time | Explicit call, never automatic inside a capture |
| **Forbidden by contract** | Retaining separated stems or per-track render WAVs | — | Never |

### 4.8 Degradation when the GPU/model is unavailable

The design must remain useful with **no** model:

- Structural facts, levels, band levels, band dominance, pan relations, phase pairs, reconstruction residuals and reference corridors are all CPU DSP and stay available.
- Model results become `{"status": "unavailable", "reason": "no_gpu", "requested": "..."}` — an explicit record, never an absent field and never a fabricated description (`WAVE-D-RESULTS-AND-NEXT-ACTIONS.md:76-78` requires exactly this).
- Claims that cited a model result drop to grade C/F and are marked `state: "unavailable"` with the evidence ID retained (asset may be gone; the hash remains).
- The model-facing text must say *which* capabilities are missing, because a text model will otherwise assume silence means "nothing there" (`docs/research/round-2/00-COMMON-CONTRACT.md:45` — "absence of measurement is not absence of phenomenon").

### 4.9 Reference correspondences: the missing edge type

The current corridor answers "how does this source's metric compare with the median of these references" (`compare.py:56-92`) but never records *why* those two things were comparable. Wave D showed how much that matters: the frozen mix and the eight producer masters aligned with lags of **0.95–4.40 s** (`DECISION-LEDGER.md:317`), i.e. up to four seconds of offset. Any band-level or masking comparison made without that alignment is comparing different music.

Proposed edge, one per accepted correspondence, bounded like `audio_relations`:

```json
{
  "reference_links": [
    {
      "link_id": "rln_01J8ZS…",
      "kind": "section_pair",
      "project_side": {"scope": "section", "section_id": "sec_…",
                       "window": {"start_s": 41.0, "end_s": 63.0},
                       "stage": "master_mix_post_fx", "loudness_lufs": -9.93},
      "reference_side": {"reference_id": "ref_…", "window": {"start_s": 38.4, "end_s": 60.4},
                         "stage": "reference_file", "loudness_lufs": -9.80},
      "comparability": {"stage_match": "same", "section_type_match": "chorus",
                        "duration_match_s": 22.0, "n_references_in_corridor": 8},
      "alignment": {"method": "log_rms_envelope_xcorr", "search_s": 12.0,
                    "lag_s": 1.35, "lag_confidence": "measured",
                    "sample_locked": false},
      "loudness_match": {"applied_db": -0.13, "tolerance_db": 0.5,
                         "absolute_values_retained": true},
      "state": "fresh", "grade": "B_derived",
      "caveats": ["reference_is_a_master", "arrangement_may_differ"]
    }
  ]
}
```

Rules that make the edge worth having:

1. **Both stages are recorded**, and `stage_match` must be `same` or `offset_declared`. A mix-to-master comparison without an offset is `incomparable` (the PLR/LRA illusion in `GAP-ANALYSIS.md:214-217`).
2. **Both windows are recorded**, with the alignment method and lag. "Whole song vs whole song" is not a pairing; it is an average over different arrangements.
3. **Loudness match is applied for listening and retained as a number** — never by rewriting the file, so absolute LUFS survives (`docs/POLICY.md:35`; `docs/MIX-QUALITY-METHODOLOGY.ru.md:529` level confound).
4. **`n_references_in_corridor` travels with the link**, because a 2-MAD delta against n = 3 is not evidence (§6 S7).
5. **`section_type_match` may be `unknown`**, in which case the link exists but any claim built on it is capped at grade C. The project has no automatic section typing (`GAP-ANALYSIS.md:390`), so `unknown` will be the common case and must be visible.

---

## 5. (d) Alternatives rejected

| Alternative | Why rejected |
|---|---|
| **Implement MPEG-1 Model II psychoacoustic masking as the primary clarity feature** | Cost is O(S²·F·B); it assumes tonality structure that dense guitars violate; and the two groups who built working masking-based systems both report that objective masking measures do not track perceived masking in music (`[ABSTRACT-ONLY]` A-03; A-01's own result favours linearly spaced bands over critical bands). Keep it only as a *calibrated instrument* validated on synthetic masker/target pairs, if at all |
| **A learned "mix quality" regressor trained on MED 2.0** | Colonel & Reiss found near-zero correlation between mixdown features and preference across 184 mixes `[FULL-TEXT]`; the HF mirror has no licence and is lossy MP3 (A-22); and the project forbids a universal score. Three independent reasons |
| **An end-to-end neural mixer as the product surface** | Martínez-Ramírez et al. reach "indistinguishable from professional" `[FULL-TEXT]` — and output a waveform. The product must output *editable, reversible project state with a reason*. A mixer that cannot say which guitar it moved is not usable by this architecture |
| **Per-role corpus targets derived from separated stems** | htdemucs/BS-RoFormer are 4-stem; "other" contains all guitars + cymbals; per-role targets inherit bleed and would encode genre priors the user forbade (`docs/GAP-ANALYSIS.md:391`) |
| **A single universal masking threshold in dB** | A-03 and A-05 both show the mapping from dB difference to perceived unmasking is technique- and material-dependent; the project's own policy doc forbids constants in method code (`docs/POLICY.md:1-5`) |
| **Audiobox PQ as the judge** | No-reference, 16 kHz mono, trained on MOS from other domains; the project already classifies it as a weak external validator, and its input discards the "air" this user is chasing |
| **Fx-Encoder++ to recover the amp/IR chain** | The paper's own limitations: weak parameter matching, weak single-effect understanding; trained on synthetic series chains over MUSDB/MedleyDB; needs an instrument query, i.e. presumes separation. It can support "these two guitars' processing is similar", nothing more |
| **Storing separated stems as durable assets** | Violates the retention requirement (no intermediate per-track stem WAVs; a retained capture record is not a retained stem file — `docs/research/round-4/WAVE-D-RESULTS-AND-NEXT-ACTIONS.md:114-120`) |
| **One mixed "clarity score" per song** | Even the best published model is a whole-mix scalar with ρ ≈ 0.80–0.84 on a small listening test; it cannot attribute to a source, and any re-implementation would silently become a second judge alongside the user |

---

## 6. (e) Implementation sequence and acceptance tests

Each step is independently testable, ordered by information gained per unit of work. Nothing here requires a GPU until step 6.

**S1 — Rename and re-frame band dominance (no behaviour change).**
Emit `band_dominance` instead of `masking`, add `pan_relation`, `broadband_delta_db`, `active_fraction`, `cells`; keep `masker`/`target` as deprecated aliases for one version. Add `is_psychoacoustic_threshold: false` to the payload.
*Acceptance:* `test_band_dominance_carries_context` — for a fixture with two tracks panned −1.0 and +1.0, `pan_relation == "opposite_side"`; for −1.0/−0.85, `"same_side"`; `is_psychoacoustic_threshold is False`; the old keys still parse.

**S2 — Kill the inert threshold and make severity policy-driven.**
Remove the `6.0` filter or pass an explicit `min_db` through `analyze_bundle`; read severity divisor from policy with `provisional` severity when no policy is set.
*Acceptance:* `test_masking_threshold_is_consistent` — a pair at 9 dB is either emitted by both layers or by neither (assert `masking_evidence.min_db == mix_evidence.masking_db`); `test_severity_is_provisional_without_policy` — `severity_basis == "provisional"` and `severity is None` when no policy is supplied.

**S3 — Fix the partial-audibility flood.**
Compute the expected share `100/n` and flag against *relative* deviation (e.g. share/expected), require `n ≤ 12` for the absolute 5 %/45 % rule, and attach `n_sources` to the issue.
*Acceptance:* `test_partial_audibility_with_41_equal_sources` — exactly `0` issues when all shares are equal; `test_partial_audibility_reports_source_count`; `test_partial_audibility_single_source` — 1 source yields share 100 % and **no** "too exposed" issue.

**S4 — Reconstruction gate on partial loudness.**
Before attribution, compute `sum(stems)` band powers and compare to the mix on the analyzed section; if median |Δ| > 1.0 dB or p95 > 3.0 dB, return `{"status": "not_valid", "reconstruction": {...}}` and **no** shares.
*Acceptance:* `test_partial_loudness_refuses_unreconstructed_mix` — build a mix that is `sum(stems) × 1.5` in 1–4 kHz; assert `status == "not_valid"` and that no `share_of_partial_loudness_pct` key exists anywhere in the response; `test_partial_loudness_accepts_exact_sum` — tolerance path returns `status == "ok"` and shares summing to 100 ± 1e-6.
*Tolerance justification:* 1.0 dB median / 3.0 dB p95 is the project's own proposal at `docs/GAP-ANALYSIS.md:457` ("< 1 dB by band — usable; > 3 dB — role targets forbidden"). It is a *project* threshold, not a literature constant, and is therefore a policy key (`partial.reconstruction_tolerance_db`, `..._p95_db`).

**S5 — `audio_relations[]` in the panorama, top-K only.**
Emit up to `k=8` relations per source, sorted by |Δ|; `omitted` block with exact totals and a `next` URI; relations carry method/version/window/state/evidence hashes/revision.
*Acceptance:* `test_audio_relations_bounded` — 42-source fixture yields ≤ 168 relations and `omitted.relations_total` equals the exact count from the same computation; `test_audio_relations_resolve_to_assets` — every `evidence[].asset` exists in `audio[]` and its `sha256` matches; `test_audio_relations_survive_budget` — total bytes ≤ 700,000 on the fixture (measured, not estimated).
*Determinism:* same capture → byte-identical JSON (`test_audio_relations_deterministic`), matching the existing panorama determinism test (`tests/test_capture_panorama.py:573`).

**S6 — Texture block over 2–4 s windows.**
Band levels (6–8 bands), cross-band correlation matrix (upper triangle), modulation rate/depth, cascaded depth. CPU only.
*Acceptance:* `test_texture_separates_noise_from_tone` — white noise and a 1 kHz tone yield different `cross_band_corr` structure (noise: high off-diagonal; tone: near-zero) with a margin ≥ 0.3; `test_texture_window_independence` — the same material analysed at 2 s and 4 s reports `window_s` and does not silently average across a section boundary.
*Listening requirement:* the **value** of the window (2 s vs 4 s) must be settled by the user's ears on real material; the test only pins the mechanism.

**S7 — Reference pairing contract.**
A reference comparison record must carry `stage` (both sides), `section_type` (both sides), `loudness_match_db` (|Δ| ≤ 0.5), `loudness_match_method`, `n_references`, and refuse when n < 8.
*Acceptance:* `test_reference_refuses_stage_mismatch` — mix vs master with no declared offset → `status: "incomparable"`, `reason: "stage_mismatch"`; `test_reference_refuses_loudness_mismatch` — 1.2 dB difference → refusal; `test_reference_requires_min_n` — 3 references → `status: "insufficient_references"`.
*Justification:* 0.5 dB is the project's own level-match rule (`docs/POLICY.md:35`); n ≥ 8 follows the power table (`docs/MIX-QUALITY-METHODOLOGY.ru.md:309-319`: at n = 20 the minimum significant r is 0.44, so smaller corpora cannot support corridor claims).

**S8 — Claim records with alternatives and falsifiers.**
A claim must carry `evidence_ids`, `alternatives` (≥ 2 for anything inferred), `discriminates`, `dep_set`, `scope_set`, `state`, `grade`.
*Acceptance:* `test_claim_requires_two_alternatives` (mirrors `mix/models.py`'s rule, `DECISION-LEDGER.md:33`); `test_claim_without_evidence_is_F`; `test_claim_never_contains_verdict` — no key/value matching `better|worse|good|bad|score|quality_index` anywhere in generated claims (a guard test over the serialised payload).

**S9 — Degradation path test.**
*Acceptance:* `test_panorama_without_gpu` — with the model backends disabled, the panorama still contains structure + measurements + relations, and `model_interpretations.status == "unavailable"` with a reason; no claim is silently downgraded to grade A.

**S10 — Optional, GPU: Audiobox PQ pre/post.**
Only after the user has heard the first mix and only as a validator.
*Acceptance:* same-file repeat yields identical outputs (determinism); a known degradation (added hiss at −30 dBFS) moves PQ in the stated direction; results are stored as `model_observation` with checkpoint id and never as a mix claim.

**Deterministic fixtures (no network, no GPU, no REAPER):**

| Fixture | Construction | Used by |
|---|---|---|
| F1 band-dominance | two 8 kHz-sampled sines 1 kHz apart, amplitudes set to exact dB offsets (0, 6, 9, 10, 12, 18 dB), panned −1.0/−1.0 and −1.0/+1.0 | S1, S2 |
| F2 texture | 10 s white noise vs 10 s 1 kHz tone vs noise + 8 Hz AM, same RMS | S6 |
| F3 reconstruction-ok | three noise stems summed to a mix with unit gains | S4 accept path |
| F4 reconstruction-bad | same, with the mix multiplied by 1.5 in 1–4 kHz (a deterministic "master EQ") | S4 refuse path |
| F5 scale | 42 sources (the measured capture size) built from silence + one tone, so the panorama budget test is exact and fast | S5, and the existing budget tests |
| F6 stems-with-ground-truth | a Slakh2100 excerpt (CC BY 4.0) where stems, gains and MIDI are known | S4, S6 mechanism checks |
| F7 metric response | an ODAQ excerpt pair (permissive licence, known MUSHRA score) | S10 / any metric validation |

**What cannot be fixtured:** metal. Slakh is MIDI-synthesised, MED's mirror is lossy and unlicensed, Cambridge-MT requires per-track permission. The metal acceptance set has to be the user's own multitracks under an explicit permission and retention note — and that is also the only material on which the JND questions in §7 can be answered.

**What needs the user's ears, and cannot be automated:** whether 11 dB of low-mid dominance between L1 and L2 is a problem or the intended weight; whether 2 s or 4 s is the right texture window for his material; whether a corrected band actually sounds clearer; and whether any of it matters more than the arrangement.

---

## 7. (f) Open research questions

| # | Question | What would settle it |
|---|---|---|
| Q1 | Does band dominance at 10 dB predict *anything* audible on dense distorted guitars? | A JND-style listening test on the user's own stems: take a same-side guitar pair, sweep one down in 1 dB steps in the dominant band, ask "when does it stop being one sound?" (n ≈ 20 trials per level, forced choice). If the threshold is nowhere near 10 dB or is not monotone, the feature is a locator only |
| Q2 | Is the right texture window 2 s, 3 s or 4 s? | The same procedure over window length, using cymbal-wash and fuzz-wall excerpts; measure where reported "texture sameness" saturates |
| Q3 | Does partial loudness on a limited master carry any signal after the reconstruction gate? | Take a known-limited master and its pre-master sum; compare `relative_audibility` for a source that is objectively audible vs one that is not; if the ordering is not preserved, drop partial loudness entirely |
| Q4 | Do expert listeners agree on "clarity" in black metal? | Krippendorff's α on a 30-item paired comparison with 5+ experienced listeners; if α < 0.5, clarity is not a shared construct for this repertoire and only defect-level claims are defensible |
| Q5 | Does an instrument-wise FX embedding separate the four guitars by processing? | Retrieval test: 4 chains × 5 riffs, ask Fx-Encoder++ which riffs share a chain; report top-1 accuracy. Only meaningful with real amp/IR chains, not synthetic ones |
| Q6 | Does pan-first de-masking hold for hard-panned double-tracked guitars (not centred pairs)? | Replicate Wakefield & Dewey's panning experiment with already-hard-panned sources; the interesting condition (two sources on the *same* side) was not tested there |
| Q7 | Does the "multiple guitars as one wall" percept survive soloing? | The user's own ABX: full mix vs mix with one guitar muted; if he cannot hear the difference, per-guitar masking claims are moot and the arrangement is the variable |

---

## 8. Source ledger

| ID | Full citation | DOI / arXiv / URL | Date / version | Accessed | Level | Supports |
|---|---|---|---|---|---|---|
| A-01 | Parker, A.; Fenton, S. *Musical Mix Clarity Prediction Using Decomposition and Perceptual Masking Thresholds.* Appl. Sci. 11(20):9578 | 10.3390/app11209578; arXiv:2103.12152 | 14 Oct 2021 | 2026-11-13 | ABSTRACT-ONLY | ρ = 0.8382; decomposition into transient/steady/residual; linear-band finding |
| A-02 | Parker, A. *Towards a Perceptual Model of Clarity in Music Mixes.* PhD thesis, Univ. of Huddersfield | pure.hud.ac.uk/ws/portalfiles/portal/67214456/Thesis_final.pdf | Jul 2022 | 2026-11-13 | FULL-TEXT (front matter, ch.1–2) | Variant correlations incl. negative masking-model signs; IBR-MR best |
| A-03 | Hafezi, S.; Reiss, J. D. *Autonomous Multitrack Equalization Based on Masking Reduction.* JAES 63(5):312–323 | 10.17743/jaes.2015.0021 | May 2015 | 2026-11-13 | ABSTRACT-ONLY | Autonomous EQ near professional; "objective measures of masking are ill-suited for quantifying perceived masking" |
| A-04 | De Man, B.; Boerum, M.; Leonard, B.; King, R.; Massenburg, G.; Reiss, J. D. *Perceptual Evaluation of Music Mixing Practices.* AES 138th Conv. | eecs.qmul.ac.uk/~josh/documents/2015/DeMan%20Reiss%20-%20AES138.pdf | May 2015 | 2026-11-13 | ABSTRACT-ONLY | Some features correlate with rated quality within tool-limited mixes |
| A-05 | Wakefield, J. P.; Dewey, C. *An investigation into the efficacy of methods commonly employed by mix engineers to reduce frequency masking…* AES 138th Conv. | eprints.hud.ac.uk/id/eprint/25979/ | May 2015 | 2026-11-13 | FULL-TEXT | 11 subjects; panning both tracks wins; cutting masker alone least preferred; spectrum sharing n.s. |
| A-06 | Tjandra, A. et al. *Meta Audiobox Aesthetics.* arXiv | arXiv:2502.05139; github.com/facebookresearch/audiobox-aesthetics | 7 Feb 2025 | 2026-11-13 | ABSTRACT-ONLY + REPO-CODE | Four axes CE/CU/PC/PQ; CC-BY-4.0; 0.1B params; per-rater eval data |
| A-07 | He, X.; Williams, D.; Fazenda, B. *Evaluating the Performance of Objective Audio Quality Metrics in Response to Common Audio Degradations.* DAFx-25 | dafx.de/paper-archive/2025/DAFx25_paper_5.pdf | 2–5 Sep 2025 | 2026-11-13 | FULL-TEXT | 5 metrics, 12 examples; PEAQ Advanced fails; ViSQOL insensitive to hum/glitches; SD table |
| A-08 | Torcoli, M.; Kastner, T.; Herre, J. *Objective Measures of Perceptual Audio Quality Reviewed.* IEEE/ACM TASLP 29 | arXiv:2110.11438 | 21 Oct 2021 | 2026-11-13 | ABSTRACT-ONLY | 12 measures × 14 listening tests; 2f-model best across domains; domain dependence |
| A-09 | Hines, A.; Harte, N.; Gillen, E.; Kokaram, A.; Kelly, D.; Skoglund, J. *ViSQOLAudio.* JASA 137(6):EL449–EL455 | 10.1121/1.4921674 | 2015 | 2026-11-13 | ABSTRACT-ONLY | Codec-domain quality prediction |
| A-10 | Colonel, J.; Reiss, J. *Exploring preference for multitrack mixes using statistical analysis of MIR and textual features.* AES 147th, e-Brief 526 | joshreiss.github.io/documents/2019/20549.pdf | Oct 2019 | 2026-11-13 | FULL-TEXT | 184 mixes/19 songs; "virtually no correlation" features ↔ preference; PCA axes not "width"/"loudness" |
| A-11 | Pras, A.; De Man, B.; Reiss, J. D. *A case study of cultural influences on mixing practices.* AES 144th | pure.york.ac.uk/portal/en/publications/a-case-study-of-cultural-influences-on-mixing-practices/ | May 2018 | 2026-11-13 | ABSTRACT-ONLY | 5 engineers, 14 raters; cultural/generational specificity of mix and preference |
| A-12 | Dobrowohl, F. A.; Milne, A. J.; Dean, R. T. *Timbre Preferences in the Context of Mixing Music.* Appl. Sci. 9(8):1695 | 10.3390/app9081695 (metadata via MDPI/ORCID) | 2019 | 2026-11-13 | METADATA-ONLY | Citation validity only |
| A-13 | McDermott, J. H.; Simoncelli, E. P. *Sound Texture Perception via Statistics of the Auditory Periphery.* Neuron 71(5) | pmc.ncbi.nlm.nih.gov/articles/PMC4143345/ | 2011 | 2026-11-13 | ABSTRACT-ONLY | Channel statistics insufficient; cross-channel correlation required; implausible filter spacing degrades |
| A-14 | McDermott, J. H.; Schemitsch, M.; Simoncelli, E. P. *Summary statistics in auditory perception.* Nat. Neurosci. 16(4):493–498 | 10.1038/nn.3347 | 24 Feb 2013 | 2026-11-13 | METADATA-ONLY | Existence/topic only |
| A-15 | McWalter, R.; Dau, T. *Cascaded Amplitude Modulations in Sound Texture Perception.* Front. Neurosci. 11:485 | 10.3389/fnins.2017.00485 | 11 Sep 2017 | 2026-11-13 | FULL-TEXT (intro) | AM and cascaded AM as texture statistics |
| A-16 | McWalter, R.; McDermott, J. H. *Illusory sound texture reveals multi-second statistical completion…* Nat. Commun. 10:5096 | 10.1038/s41467-019-12893-0 | 2 Nov 2019 | 2026-11-13 | ABSTRACT-ONLY | Multi-second filling-in; illusory texture biases later estimates |
| A-17 | Rouard, S.; Massa, F.; Défossez, A. *Hybrid Transformers for Music Source Separation* / Demucs v4 | arXiv:2211.08553; github.com/facebookresearch/demucs | 15 Nov 2022 | 2026-11-13 | REPO-CODE | MIT; SDR 9.00 dB MUSDB HQ; 4 stems; unmaintained |
| A-18 | Lu, W.-T.; Wang, J.-C.; Kong, Q.; Hung, Y.-N. *Music Source Separation with Band-Split RoPE Transformer* | arXiv:2309.02612 | 2023 (ICASSP 2024) | 2026-11-13 | ABSTRACT-ONLY | SDX23 winner; 9.80 dB avg SDR (MUSDB18HQ, smaller model) |
| A-19 | Luo, Y.; Yu, J. *Music Source Separation with Band-split RNN* | arXiv:2209.15174 | 2022 (TASLP 2023) | 2026-11-13 | METADATA-ONLY | Precursor architecture only |
| A-20 | Yeh, Y.-T.; Koo, J.; Martínez-Ramírez, M. A.; Liao, W.-H.; Yang, Y.-H.; Mitsufuji, Y. *Fx-Encoder++* | arXiv:2507.02273 | 3 Jul 2025 | 2026-11-13 | FULL-TEXT | Contrastive instrument-wise FX embeddings; synthetic chain pool; weak parameter matching |
| A-21 | De Man, B.; Reiss, J. D. *The Mix Evaluation Dataset.* DAFx-17 | dafx.de/paper-archive/2017/papers/DAFx17_paper_49.pdf | 2017 | 2026-11-13 | FULL-TEXT (abstract+intro) | 180 mixes, ~5000 ratings, parameter settings, 5 countries; experts harsher/specific/more agreeing |
| A-22 | *Mix-Evaluation-Dataset-2.0* (third-party HF mirror) | huggingface.co/datasets/sparrowreivun/Mix-Evaluation-Dataset-2.0 | lastModified 2026-10-05 | 2026-11-13 | DATASET-CARD | MP3, no licence tag, broken viewer |
| A-23 | Torcoli, M. et al. *ODAQ: Open Dataset of Audio Quality* | 10.5281/zenodo.10405774; arXiv:2401.00197; github.com/Fraunhofer-IIS/ODAQ | 2024 | 2026-11-13 | REPO-CODE + ABSTRACT-ONLY | 240 samples, 26 experts, 6×5 conditions, 14 music/11 movie excerpts, permissive |
| A-24 | Bittner, R.; Salamon, J.; Tierney, M.; Mauch, M.; Cannam, C.; Bello, J. P. *MedleyDB* | medleydb.weebly.com | ISMIR 2014 | 2026-11-13 | METADATA-ONLY | Non-commercial terms only |
| A-25 | Manilow, E.; Wichern, G.; Seetharaman, P.; Le Roux, J. *Slakh2100* | 10.5281/zenodo.4599665 | 2019 | 2026-11-13 | DATASET-CARD | CC BY 4.0; 2100 synthesised multitracks with MIDI |
| A-26 | Cambridge-MT *Mixing Secrets* multitrack library FAQ | cambridge-mt.com/ms/mtk-faq/ | n.d. | 2026-11-13 | FULL-TEXT | Research use not pre-agreed; portfolio use prohibited |
| A-27 | Tian, H.; Lattner, S.; Saitis, C. *Assessing the Alignment of Audio Representations With Timbre Similarity Ratings.* ISMIR 2025:710–718 | 10.5281/zenodo.17811470; arXiv:2507.07764 | v2, 21 Sep 2025 | 2026-11-13 | DATASET-CARD | CC-BY-4.0; venue/pages verified |
| A-28 | Martínez-Ramírez, M. A. et al. *Automatic Music Mixing with Deep Learning and Out-of-Domain Data.* ISMIR 2022 | arXiv:2208.11428 | 2022 | 2026-11-13 | FULL-TEXT | Effect-normalised wet-data training; "indistinguishable from professional" in listening test |
| A-29 | Jillings, N. *Automating the Production of the Balance Mix in Music Production.* PhD, BCU | open-access.bcu.ac.uk/15026/ | submitted May 2022, awarded 2023 | 2026-11-13 | ABSTRACT-ONLY | Balance-mix data collection; grouping overlooked; masking-minimising evolutionary solver |
| A-30 | Koszewski, D.; Görne, T.; Korvel, G.; Kostek, B. *Automatic music signal mixing system based on 1-D Wave-U-Net autoencoders.* EURASIP JASMP 2023:1 | 10.1186/s13636-022-00266-3 | 2023 | 2026-11-13 | METADATA-ONLY | Existence only |
| A-31 | Stickla, S.; Athauda, R. *A new audio mixing paradigm: evaluation from professional practitioners' perspectives.* Creative Industries Journal | 10.1080/17510694.2022.2088164 | online 21 Jun 2022 | 2026-11-13 | METADATA-ONLY | Existence/date correction only |

**Counts:** 31 sources. FULL-TEXT 9 (A-02 front matter/ch.1–2, A-05, A-07, A-10, A-15 introduction, A-20, A-21, A-26, A-28); ABSTRACT-ONLY 10 (A-01, A-03, A-04, A-08, A-09, A-11, A-13, A-16, A-18, A-29); REPO-CODE / DATASET-CARD 6 (A-06, A-17, A-22, A-23, A-25, A-27); METADATA-ONLY 6 (A-12, A-14, A-19, A-24, A-30, A-31). Two sources (A-06, A-23) have both a paper and a repository/dataset surface; they are counted once, under the surface I actually used for each claim.

---

## 9. Could not verify

1. **Parker & Fenton listening-test details** (number of participants, stimuli, whether the 0.8382 variant is the IBR/IBR-MR or a masking-decomposition variant). MDPI blocks the browser (Akamai "Access Denied"); `web_extract` returned a head+tail window. The thesis abstract's per-variant numbers are the basis for my variant claim, and the thesis body beyond ~50 k characters was not retrieved.
2. **Full text of Hafezi & Reiss and De Man et al.** Both QMUL PDFs return HTTP 422 to the extractor; the scispace mirror returns 405. Only publisher abstracts (via Academia.edu) were read.
3. **ISO 226:2003 and ISO 532-1/2 full text.** Paywalled. My statements about equal-loudness contours, threshold in quiet and the input requirements of the Zwicker/Moore models are stated as standard scope, not as re-verified quotations.
4. **Audiobox Aesthetics input sample rate and window.** Not stated in the repo README, the HF card, or `model/aes.py`; the 16 kHz / 10 s / bf16 details come from the project's own `docs/MIX-QUALITY-METHODOLOGY.ru.md:181,194` and were not re-confirmed from code.
5. **VRAM and throughput for htdemucs, BS-RoFormer, Fx-Encoder++, Flamingo.** Not reported on the pages I read; I ran no GPU work. No VRAM number in this report is measured.
6. **BS-RoFormer and Fx-Encoder++ weight licences.** Papers are CC-BY-4.0; checkpoint licences were not found on the pages read.
7. **An official, licensed MED 2.0 distribution.** Only the third-party HF mirror was found; no QMUL-hosted MED 2.0 with a licence was verified. The Mix Evaluation Browser exists per C4DM but I did not open it.
8. **Dobrowohl et al. 2019 content.** MDPI PDF fetch timed out; metadata only.
9. **McDermott/Schemitsch/Simoncelli 2013 and Luo & Yu BSRNN content.** Metadata only.
10. **The attached literature text itself.** It is not in the repository; I worked from the dispatch's descriptions and verified each primary source independently. Where a description was wrong (thesis year, McDermott journal, ViSQOLAudio venue, the "2024" mixing-paradigm paper), I corrected it in place.
11. **Cambridge-MT per-track licence terms.** Only the FAQ's general statements were read; individual tracks may differ.
12. **Whether the 41-source `partial_audibility` flood actually occurs on the user's real project.** It is inferred from `mix_evidence.py:84-98`, `partial.py:320-324` and the 42-source capture size in `FULL-CAPTURE.md:97-104`; no capture was run and no project was opened.
