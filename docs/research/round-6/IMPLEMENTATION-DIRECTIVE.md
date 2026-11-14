# REAPER Workbench — implementation and fresh-session evaluation directive

> **Historical record (superseded transport).** This document describes the
> paged panorama contract that has since been replaced by one whole-view result.
> Current behaviour: `docs/DECISION-single-response-panorama.md` and
> `docs/MCP-README-LLM.md`. Treat this file as history, not instruction.


Date: 2026-11-13  
Status: Owner-authorized follow-on work. The research documents are evidence and design inputs; this directive captures the broader product requirements that must be implemented and then exercised end to end.

## 1. Objective

Deliver a production-ready REAPER Workbench library and MCP interface that lets an LLM understand, inspect and safely modify an arbitrary REAPER project using native REAPER/ReaScript mechanisms. The model must be able to use the entire supported library capability surface, including audio analysis, comparison, editing, arrangement, MIDI, FX, routing, automation, tempo/time-map and save/undo operations. A fixed mixing or editing recipe is not the product.

After the library, MCP contract and model-facing guide are complete, open a fresh Hermes session with no inherited mixing history and evaluate whether the model can use the system end to end. Observe the context it receives, which capabilities it discovers, what it overlooks, how it changes the project, and how it verifies those changes. Fix recurring interface/context failures and repeat on an isolated project until the stated acceptance criteria pass. Human listening remains part of sound-quality evaluation; do not invent an automatic better/worse verdict.

The owner authorizes this follow-on implementation and evaluation. Do not ask for permission again for routine, reversible work within this scope. Never alter a user project during testing; use an isolated disposable copy.

## 2. Product constraints

1. **Portable MCP:** the required behavior must work through ordinary MCP tools in Codex, Hermes and other conforming tools-only clients. No Hermes Context Engine, plugin, private API, transcript hook or client-specific context harness is part of the solution.
2. **Native REAPER access:** use the supported ReaScript/REAPER bridge. No process-memory reads/writes, Frida, reverse-engineered memory layouts or FL Studio remnants.
3. **Whole-project context:** the model-facing view is one coherent, cross-linked panorama of project state and audio evidence: tracks, master, items/takes/MIDI, FX, routing, automation, tempo/time map, processed signal, measurements, provenance, validity and relationships.
4. **No lossy “full” snapshot:** preserve all available time-resolved data, including both 30 s and 300 s temporal levels and any finer captured levels. Keep units, methods/versions, processing stage, channel view, exact windows, validity, coverage, uncertainty and source links. The 20,838 B reduced spine is rejected.
5. **Required paged load:** the owner accepts a multi-call page sequence as transport for one full logical panorama. The model must fetch every page and verify page/document hashes before making whole-project claims or mutating the project. Missing, truncated, duplicated, out-of-order or cross-revision pages keep the view incomplete and block writes. Every mutation also requires the exact expected project revision. Clearly distinguish server-verifiable delivery from model attention, which is tested empirically.
6. **Incremental updates:** after the initial complete load, provide factual revision-bound deltas. Preserve unchanged facts by exact base identity; report additions, replacements, removals, invalidations and missing/invalid/stale/incomparable evidence. Never silently patch a stale base. Full refresh is always available and required whenever continuity cannot be proven.
7. **Full captures and audit:** preserve immutable capture/revision history and factual before/after changes as agreed in the existing retention policy. Do not accumulate loose intermediate stems. Never add an automatic quality score or better/worse verdict.
8. **Flexible musical work:** support arbitrary user goals, including editing and creating MIDI, without hard-coded genre recipes or a mandatory order of operations. The interface and README should teach reliable context use, not force a mixing pipeline.
9. **Compact and discoverable capability surface:** do not expose hundreds of undifferentiated tools. Build an accurate searchable capability catalog with exact function names, signatures, argument types, targets, side effects and constraints; use structured filters such as object, operation, scope and required context. A generic native API dispatch path may cover the long tail, but every documented function must be discoverable and correctly callable or have an explicit tested limitation.
10. **Safe edits:** use revision-bound previews/transactions where appropriate, explicit expected state, post-change readback, factual deltas, and undo/rollback. Distinguish a preview from an applied change. Do not report success without checking the resulting REAPER state.

## 3. Required implementation deliverables

### A. Complete library/MCP integration

- Audit the current bridge, library APIs and MCP tools first. Reuse sound, tested components; do not duplicate or replace ongoing performance work without evidence.
- Produce a function/domain coverage matrix for the supported REAPER API and the library. Cover project/session, tracks, items/takes, MIDI, FX and parameters, routing/sends, envelopes/automation, tempo/time map, markers/regions, transport, save/undo, analysis/capture, comparisons, revisions and history.
- Implement the panorama lifecycle defined in `SYNTHESIS-AND-DECISIONS.md`: complete `open`, page fetch/acknowledgement, exact-base `changes_since`, `refresh`, interval evidence `slice`, and status/identity. Tool names may change if tests show a clearer portable schema.
- Implement a server-verifiable page-completion gate. A partial page set must not mint a complete-view receipt; write operations must reject missing, stale or mismatched receipts and revisions. The gate must handle duplicate requests idempotently and must not accept pages from different project epochs or revisions.
- **Continuity invariant:** one initial complete panorama establishes the model's project view. Later state is advanced by exact-base, factual deltas with their own delivery/acknowledgement and a successor context token; routine edits must not force all unchanged panorama pages to be sent again. Each applied operation must return verified before/after revision data and a factual readback delta that the model acknowledges before context advances. Signal-affecting edits invalidate dependent audio evidence until a new capture produces a revision-bound audio delta. Full refresh remains available on request and is mandatory whenever exact continuity cannot be proven, including an oversized/unrepresentable delta, project/epoch mismatch, missing base or stale chain. See `DELTA-CONTINUITY-IMPLEMENTATION-ADDENDUM.md` for the blocking state machine and acceptance tests.
- **Continuity acceptance gate:** demonstrate two sequential edit → readback/delta → acknowledgement cycles from one initial full panorama without refetching unchanged full pages. Demonstrate that partial, stale, wrong-project, wrong-base and unacknowledged deltas block context advancement and dependent writes.
- **Claim-scope gate:** project-wide analysis and whole-project conclusions require the latest acknowledged continuity token too; scoped analysis must carry an explicit target/window and cannot be mislabeled as a full-project conclusion. Capture creation may precede model receipt so the full panorama can be generated, but it does not itself prove that the model received it.
- Make capture readback expose invalid, stale, missing, incomplete and incomparable evidence faithfully. Fix identity/digest omissions and dependency invalidation identified by the research reports before calling a panorama complete.
- Expose all existing sound-analysis and comparison functions through one usable capture/analysis surface where technically possible. The normal full-project capture should include each applicable track and the master with observed processing state (pan/fader/FX/routing context) and explicit signal stage. Reference-dependent comparisons must state the reference identity and method.
- Support ordinary project edits through the complete library/API surface, not only through standalone scripts. The MCP must be able to inspect, preview, apply, verify and undo project changes while preserving project identity/revision context.
- Measure page sizes and token/response usage using the actual tested clients. For clients with smaller declared limits, reduce page size rather than reduce content. Unknown client limits must fail clearly or use an explicitly conservative tested profile; never silently truncate.

### B. Model-facing documentation

Deliver a complete model operating guide in English and Russian, with English as the source of truth:

- `docs/MCP-README-LLM.md` and `docs/MCP-README-LLM.ru.md` for the model's operating guide;
- update repository-facing `README.md` and `README.ru.md` as needed so installation and purpose are clear from a clean machine.

The guide must explain, accurately and without childlike simplifications:

- what the library does and how to connect it to REAPER through the native bridge;
- how to discover the capability catalog and construct exact structured searches/calls;
- the full panorama/page/delta/refresh lifecycle, what “complete” means, and what to do on a missing page or stale revision;
- how project structure, audio evidence, measurements, processing stage, time windows, tempo/time map, provenance, uncertainty and invalid/missing data relate;
- how to make and verify changes, use preview/apply/undo and record factual change descriptions;
- all supported functional domains, limitations and error states;
- observed positive and negative Hermes behavior patterns collected in the existing behavior ledger.

Include flexible principles and anti-patterns, not fixed mixing steps. A user may request any outcome; the model should choose relevant capabilities from current context, inspect dependencies, act safely, listen/measure again and report what it actually changed. Do not prescribe genre-based EQ/pan/FX settings, a rigid mixing sequence or a canonical “correct” sound.

README/guide availability must itself be portable: provide a stock-tools-only way to read the guide, with resources/prompts only as optional conveniences. Do not depend on a client-specific context hook.

### C. Clean-session evaluation and iteration

Only start this phase after A and B are complete enough to run. Create a **new Hermes chat/session** with the intended evaluation model (`deepseek/deepseek-v4.1-flash-1017` through RouterAI, Ultra reasoning); do not inherit the implementation/research transcript. All Hermes subagents and evaluation sessions for this project must explicitly use this same model and reasoning level; no fallback or substitute model is permitted. Set model and reasoning explicitly at creation, then verify the saved session metadata before its first tool call. If that exact model is unavailable, stop and report the blocker instead of substituting. A run using the configured default model is invalid for this evaluation. Connect it to the finished MCP and give it the model guide. Keep the session visible to the owner where the existing Hermes workflow permits.

Use a disposable multi-track project with representative tracks, FX, routing, automation and tempo map plus ordinary finished reference masters. Do not provide producer-mixed stems or hidden target-track outputs as shortcuts. The model must get all panorama pages before acting; log the page receipts, complete-view hash, model/client token usage and all tool calls. Captures used to test `changes_since` must be appended to the same archive root as their base; otherwise that test covers only the expected `stale_base` response.

Evaluate varied user goals rather than rehearsing one fixed workflow. Include whole-mix diagnosis, track/section comparison, a safe project edit, an arrangement/MIDI task and a timing/alignment task only if the project supports them. For each, assess:

- whether all pages arrived and were verified before the model reasoned or edited;
- whether it identified the right project/revision, tracks, master, FX, routing, tempo and relevant intervals;
- whether it found and called relevant tools without needing a human to name the exact API function;
- whether its conclusions referenced actual measurements, units, stage, window, validity and provenance;
- whether it noticed dependencies and stale/missing evidence instead of treating absence as silence;
- whether edits were revision-bound, reversible and verified by readback;
- whether it used the audio-analysis/comparison tools where relevant rather than falling back to unrelated scripts;
- which facts or capabilities it ignored, misunderstood, over-requested or used incorrectly;
- whether its final audio is improved according to human listening and the supplied reference, without an automatic score substituting for that judgement.

Maintain a factual trial log: prompt, starting revision, page set/hash, tools called, returned context, edits/deltas, failures, model token usage and owner listening notes. Do not declare “ideal” from one successful run. Fix recurring omissions in tools, schemas, full panorama or guide; repeat with a fresh session. Exit only when all capability-domain checks pass, no page/revision safety failure occurs, the model can complete varied tasks without fixed recipes, and the owner accepts the resulting audio after listening. Record remaining limits honestly.

## 4. Required sequencing

1. Finalize the research synthesis and this owner-approved directive. The owner has already accepted the complete page sequence and rejected reduced temporal resolution.
2. Audit current code and in-progress Hermes changes. Agree file ownership with the active performance work; do not overwrite it.
3. Implement panorama completeness/validity, pagination gate, revision journal/deltas/resync and safe mutation preconditions.
4. Close functional-domain/API coverage gaps and expose the full library through the MCP discovery and dispatch design.
5. Complete bilingual model and repository documentation, including flexible usage principles and evidence-based anti-patterns.
6. Run unit/integration and cross-client acceptance tests on isolated projects; fix failures.
7. Start the new Hermes session and perform the varied end-to-end evaluation. Iterate MCP, panorama and guide until the exit conditions above pass.
8. Report the implementation, tests, clean-session behavior, owner listening checkpoint and any remaining limitations. Do not publish a release or overwrite user projects as part of this directive.

## 5. Reporting protocol for Hermes

At each meaningful milestone, state: files/domains changed; capability coverage completed and remaining; commands/tests actually run and their results; page/token measurements by client; unresolved safety/correctness failures; and the next concrete milestone. Do not say “done” when only a unit test or isolated script succeeds. Do not keep extending research once evidence is saturated; convert findings into code, acceptance checks and a short decision log.
