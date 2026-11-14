# Round 5: applied review of the attached audio research

## Assignment to Hermes

Spawn **exactly two research subagents** for the two workstreams below. Do not start a code implementation from these reports. Keep the existing performance-profiling/optimization work moving independently; do not edit, revert, or interrupt its in-progress files or REAPER session. The user asked for the material in the attached text to be distributed because it spans too many topics for one pass.

The source is the user's attached text titled **“Это раздел, на который я бы обратил особое внимание…”**. Read the whole attachment, including its descriptions and links. Treat those descriptions as hypotheses, not verified facts. Verify the primary papers, code, datasets, licenses, and current project state yourself. Do not publish the attachment's local filesystem path in any report or repository metadata.

## Shared project intent and constraints

The target is one dependable REAPER Workbench library that a text model can use for end-to-end music work: inspect and understand a project, hear the actual processed tracks and complete master, reason about relationships, and make/verify edits through REAPER's native API. It must stay flexible: no genre-specific mixing recipe, fixed production trajectory, or one-purpose “guitar-tightening” feature masquerading as general capability. A requested task should emerge from general capabilities and evidence.

The model needs a rich, jointly interpretable project/audio context. The project map and rendered sound are linked evidence: source identity, track/item/take/time ranges, FX and parameter state, routing, fader/pan/automation, tempo map, markers, edits, captures, analysis outputs, and revisions must be traceable to one another. Prefer a whole-project view whose relationships are visible together, with drill-down only for payloads that cannot fit inline. Preserve raw measured values and provenance; expose uncertainty and competing interpretations.

Do not invent a hard-coded “good/bad/better/worse” verdict or a universal mix-quality score. Separate technical defects and measured deltas from artistic preference. The user wants complete capture data and meaningful revision/delta history, not a pile of retained stems; follow the current storage contract and verify it in the repository. Do not assume that a fast AudioAccessor path is equivalent to rendered audio through track FX or the master. Check current code/docs for the exact behavior before making claims. REAPER integration must remain native; do not use process-memory injection.

The library must eventually be practical on this machine's single Tesla V100 16 GB; CPU audio DSP is permitted, but any proposed neural model must state whether it fits the V100, its VRAM, throughput/latency, and whether inference is optional or required. Do not assume GPU is available to multiple models concurrently.

## Shared preparation and coordination

Before researching, read:

- `docs/research/00-RESEARCH-PROTOCOL.md`
- The six initial briefs `01-AUDIO-PATH-AND-LISTENERS.md` through `06-REAPER-EDITING-AND-END-TO-END-VALIDATION.md`
- The latest applicable findings in `round-2/`, `round-3/`, and `round-4/`, especially snapshot/context ledger, acceptance matrix, full-capture, edit acceptance, mixing-quality methodology, and the current feature/gap docs
- Current implementation and tests for every capability you discuss; do not infer the code state from stale prose alone

Use the source groups in the attachment as follows:

- **Agent A:** perceived clarity/quality and masking; auditory texture; reference/mix assessment; source separation and instrument-specific effect representations; perceptual and mix-evaluation datasets; relevant end-to-end mixing studies.
- **Agent B:** musical events and editing; note/onset/transcription and microtiming; time stretching/artifacts; general-purpose effect optimization/control; panning/grouping; datasets and end-to-end validation relevant to those operations.

Each agent should send the other a short interim note after it has mapped the papers to the project, identifying dependencies, overlap, and questions the other workstream must resolve. Read and reconcile the other agent's report before finalizing. Avoid duplicating old reports: state what is already answered, what is contradicted or stale, and what new evidence changes the design.

## Workstream A — What the model can hear and infer from a complete mix

Investigate the attachment's perceptual masking/clarity, auditory-texture, reference-analysis, source-separation/effect-representation, subjective-evaluation, and dataset material. Include the listed mix-quality and listener-preference work, Meta AudioBox Aesthetics, ODAQ, Mix Evaluation Dataset (including 2.0 if usable), MedleyDB, and the listed source-separation and instrument-wise FX papers where relevant.

Answer with evidence:

1. What can be measured reliably from each processed track, stereo master, and their joint relation? Distinguish signal-level facts, psychoacoustic proxies, model inferences, and subjective judgments. Explain where masking/clarity features add information beyond the existing snapshot and where they are invalid or ambiguous, especially for dense black/death-metal guitar, fuzz, cymbal wash, double-kick, layered guitars, and production effects.
2. What representation lets an LLM see the whole arrangement and causal/relational context at once? Specify fields/edges for source identity, time/frequency regions, masking candidates, track-to-master contribution, routing/FX, panning, dynamics, and reference correspondences. Give one compact illustrative schema fragment, but do not shrink or discard the full capture. Show how a claim links to exact source audio, time range, analysis method/model/version, confidence, and project revision.
3. Assess whether source separation or instrument-wise FX representations help compare a full stereo reference with project tracks. State what they can and cannot identify, likely leakage/false attribution, metal-specific failure modes, and how to keep separated audio as temporary analysis material rather than retained stems.
4. Which datasets contain same-song alternate mixes, listener preferences, technical defects, stems, DAW sessions, or metal-relevant material? Verify access, completeness, licensing, labels, and suitability. Keep preference labels separate from technical-artifact labels. Identify data gaps that cannot be solved by more hand-written rules.
5. Propose a practical capture-time and on-demand analysis design: what is computed once per complete capture, what can be requested for a region/reference, what is model-assisted, and what can be cached only if the current contract permits it. State expected latency, VRAM/RAM/storage, and how the design degrades when the GPU/model is unavailable.
6. Define objective experiments that test whether this context actually improves a language model's decisions: ablations of context fields, expert/producer judgments, listener preference comparisons, localization accuracy, calibration, and contradictory evidence. Do not define a universal pass/fail score for whether a mix is artistically good.

Deliver `docs/research/round-5/AGENT-A-PERCEPTUAL-CONTEXT-REPORT.md` with: (a) verified source-by-source findings and citations, (b) current-project gap map, (c) recommended representation and analysis design, (d) alternatives rejected with reasons, (e) implementation sequence and acceptance tests, (f) open research questions. Cite primary papers and official repos/datasets; distinguish results directly reported by the source from your inference. Do not claim a model “hears like a human.”

## Workstream B — From audio evidence to general, safe REAPER actions

Investigate the attachment's intelligent multitrack editing, guitar/string onset and note work, rhythmic structure/microtiming, time-scale modification and artifact handling, black-/gray-box FX control/optimization, panning/grouping, and operation-specific datasets. Include ST-ITO, differentiable black-box DSP, ITO-Master, Text2FX, Gaussian-prior work, NablAFx/ToneTwist, Fx-Encoder++ where it informs actionable analysis, and the listed onset, alignment, and time-stretch papers.

Answer with evidence:

1. What general intermediate representations are needed to turn audio into editable musical events without hard-coding a genre or task? Cover candidate note/onset boundaries, repeated sections/riffs, tempo and meter hypotheses (including tempo automation), grids, groove/microtiming, pitch/chords where supportable, source/track identity, and confidence/alternative hypotheses. Explain how evidence from track content, project tempo map, MIDI, and cross-track alignment should reinforce or contradict each other.
2. For edits such as cutting, moving, stretching, replacing, MIDI writing, or changing FX, what does the model need to inspect before acting and verify afterward to avoid clicks, gaps, phase damage, transient loss, altered groove, broken item/source references, or unexpected routing/automation changes? Map this to reusable REAPER-native operations and preview/commit/undo semantics, not one hard-coded “correct the riff” tool.
3. Compare the literature's event/onset, microtiming, repeated-pattern, guitar transcription, and time-stretch methods on dense distorted metal. Report operating domain, compute, training data, failure cases, temporal resolution, preservation of stereo/phase/transients, and whether a method is production-ready or only research.
4. What can reference-guided black-box FX optimization actually do? Separate choosing an FX chain/topology from optimizing parameters in a user-supplied chain; identify assumptions (processed versus dry source, reference availability, series-only setup, effect coverage, candidate-render budget, training data and differentiability). State which methods can transfer to arbitrary REAPER VSTs and which need plugin-specific training/calibration. Include realistic evaluation counts/latency and V100 feasibility; do not pitch exhaustive search as cheap.
5. Determine the useful role and limits of source separation, FX inversion, automatic panning, subgrouping, and mix-style transfer in completing actions from a stereo reference. Clarify identifiability: audible output similarity does not prove the original plugin chain or knob values.
6. Define an end-to-end test matrix using safe copies: synthetic fixtures plus real multitrack material, known event/time ground truth where available, before/after audio renders, edit diffs, rollback checks, blind/paired listening where appropriate, and objective artifact measurements. Include licensing and why a clean reference-video soundtrack may not be a valid ground-truth mix.

Deliver `docs/research/round-5/AGENT-B-MUSICAL-ACTIONS-REPORT.md` with: (a) verified source-by-source findings and citations, (b) current-project capability/gap map, (c) event/action/context contract, (d) candidate methods and limits, (e) implementation sequence and acceptance tests, (f) open research questions. Cite primary papers and official repos/datasets; clearly label inference versus source claims.

## Required final synthesis from Hermes

After both reports are complete, produce `docs/research/round-5/SYNTHESIS-AND-DECISIONS.md` that:

- Reconciles the reports into one dependency-aware architecture for a unified capture/context and general action surface.
- Identifies decisions we can make now, questions that require experiments, and questions that remain unanswerable from current literature.
- Maps each proposed capability to its source evidence, current implementation location/gap, resource cost, uncertainty, and acceptance test.
- Separates must-have reliable primitives from optional model-assisted analysis; do not imply either side alone is sufficient.
- Preserves user requirements: full project context, no recipe/genre hardcode, complete track-plus-master sound evidence, BPM/tempo-map awareness, safe reversible edits, no artificial better/worse judge, and no retained intermediate stem accumulation.
- Lists a small number of high-information experiments that determine what to build next, prioritizing expected reduction in model blind spots per engineering effort.

## Research quality gates

- Verify every material claim against the paper itself or official code/data; abstracts and review articles are not enough for implementation decisions.
- For each method, report input/output, assumptions, source availability, license, training/evaluation data, compute/runtime, failure cases, and the specific project decision it informs.
- Record publication date/version; avoid calling a method “SOTA” without a benchmark and comparison.
- Never treat a paper's reported metric as proof of professional mix quality or as a replacement for user listening.
- No code edits, package installs, model downloads, project mutation, or expensive GPU runs during this research phase. Work in reports only and leave the current performance task alone.
