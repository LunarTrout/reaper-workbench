# Hermes execution brief — start assembling REAPER Workbench

## Assignment

R3 research and design are complete. Begin implementation from docs/research/round-3/IMPLEMENTATION-BLUEPRINT.md and docs/research/round-3/DECISION-LEDGER.md, using all six final R3 reports as rationale and detail. The owner clarified that Hermes is the implementer and integrator; the coordinating agent does not write product code. Use Hermes subagents to parallelize implementation as far as safe.

Use the same user-visible Hermes session and requested model/reasoning. Do not create a second chat. This brief supersedes the R3 research-only restriction that prohibited code changes: implementation is now authorized. Preserve the shared dirty tree exactly; do not reset, discard, or overwrite any pre-existing work.

## Parallel execution model

Use exactly six subagents for the implementation wave, with Hermes as sole integrator. Assign each a disjoint set of files and one module/contract owner. Never let two agents patch the same Python, Lua, MCP, test, or documentation file concurrently. An agent may not silently export or rename shared types owned by another agent. Freeze the blueprint's P0 contracts first; later phases may amend them only through an explicit integration decision recorded in DECISION-LEDGER.md.

The six implementation lanes are:

1. **P0 time and shared schema:** timebase.py; typed TimeRef/Interval/TimeMap and mapping semantics; context/schema.py and RevisionRef; JSON round-trip/schema-version contract. Coordinate exact shared names before parallel consumers import them.
2. **P0 identity and event skeleton:** context/ids.py canonical prefix registry, deterministic derived IDs and authored IDs; analysis/event_model.py types/invariants. Use peg_ as the one canonical gesture ID. Keep this lane's owned files disjoint from lane 1.
3. **P0/P1 audio evidence types:** context/audio.py CaptureRequest/Capture/AudioAsset/Listen/Delivery evidence, stage/method/source separation, lease and ownership contracts. This lane creates types/serialization first; host capture changes belong to later capture lane or an explicitly assigned phase owner.
4. **P0 mix contracts:** mix/models.py for MixObservation → MixClaim → IssueHypothesis → PostChangeCheck and evidence references. Do not add severity thresholds as universal truth.
5. **P2 project revision/read model:** inspect actual bridge and API first; implement the Python revision adapter, stable identity/content predicates, and read-only address/read-model pieces that are independent of unverified REAPER semantics. The bridge Lua file is owned by this lane alone if a later step requires it. Never claim a digest field is sound-affecting until its coverage is specified.
6. **P0 integration/contract verification:** inspect the six owned implementations against the blueprint, review their non-overlapping tests/serialization boundaries, and prepare the root integration checklist. This agent must not rewrite another agent's files; report proposed diffs to Hermes.

If actual file ownership differs from the blueprint after code inspection, stop and have the parent publish a revised ownership table in the Hermes session before agents edit. Use small focused changes and integrate after each wave. Parallelize only tasks whose inputs are frozen; dependent waves begin immediately after their prerequisites merge, without waiting for unrelated V100/live gates.

## Ordered implementation waves

### Wave A — P0 foundations plus independent P2 read model

Implement the six lanes above from the agreed contracts. Produce additive modules and compatibility adapters; preserve existing public types in models.py (Section, AudioBuffer, MetricValue, MeasurementReport). Do not rewrite the package or change existing MCP surface in this wave. Add focused unit/contract tests for boundary validation, time-basis requirements, deterministic IDs, schema serialization, typed audio evidence, and revision predicates. Run the relevant tests and report the exact command/results.

P0 exit gates: all canonical types have one owner; schema round-trips; missing time basis, unknown ID prefix, invalid status and invalid revision are rejected with stable structured errors; derived IDs repeat for same natural key/method version/revision; authored IDs are unique; no import cycle; old public types still import.

### Wave B — P1 capture truth and P2 live-facing read model

After Wave A integration, implement capture and project readback in parallel with file ownership assigned by Hermes. Preserve the API-only route (Lua/ReaScript/Actions; no Frida or process memory). Make capture explicitly transactional: request, side-effect footprint, render/accessor settings, selection/dirty changes, restore readback, output hashes, manifest ownership, cleanup outcome and audio-delivery state. Use the eight canonical stages and do not call take_sum_pre_fx a DI/stem. Stage or capability with unresolved REAPER behavior returns unverified; it must not become a measured fact.

For any Lua bridge work, there is one designated owner of scripts/reaper_bridge.lua and one merge point. Other agents can implement Python adapters against the agreed request/response schema but must not edit the Lua file.

### Wave C — P3 context ledger and progressive views

Build the append-only evidence/action ledger and SQLite projections, scoped dependency invalidation, manifest, typed views and pagination. Preserve provenance and expose stale/contested/missing-audio states. Enforce that `heard` is only set after verified forwarding to a listener; URI availability alone is not hearing. Temporary audio artifacts are manifest-owned with TTL/tombstones; no unbounded intermediate stems or project copies.

### Wave D — P4/P5 interpretation

In parallel, implement event normalization/occurrence/group types and the musical time/structure layer where contracts are independent. Then build mix claims and matched reference comparisons on those outputs. Keep signal observations, listener observations, MIDI project facts, human annotations and hypotheses distinct. `performed_beat` is a hypothesis; grid proximity does not establish an error; unsupported or ambiguous interpretation can abstain. A cause requires alternatives and a discriminating test. No hard-coded genre recipe.

### Wave E — P6/P7 edits, MCP and one vertical slice

Implement typed operations with revision gates, expected_after, state delta, side-effect footprint, risk/reversibility class, readback, undo/restore and verification evidence. Keep generic documented ReaScript/Actions discoverable with explicit weaker guarantees. Generate or validate MCP schemas from one source of truth; current inventory is 19 tools and 6 resource endpoints according to R3, while the 14/10 alternative remains an evaluation hypothesis.

Integrate one minimal acceptance slice specified in the blueprint (master + one track, one bounded audio window, one evidence-backed claim, one typed proposed change, actual readback, re-capture and verify/rollback). This is a test fixture for composability, not a product workflow. Expand task evaluation across editing, MIDI, mixing, references, automation/FX, routing and project navigation before claiming completeness.

## Safety, data and hardware constraints

- Do not operate on a user's live/open project. Use an isolated disposable copy or synthetic fixtures for implementation tests. Never change or save-as over an existing project without collision-safe naming and verified source hash.
- Maintain only orig and final project versions; intermediates are temporary leased artifacts, registered and cleaned only by their owner. No glob deletion.
- Do not upload user audio, project files, stems or private paths to external services. Do not use Frida, memory inspection or undocumented process access.
- The owner has one Tesla V100 16 GB and forbids CPU inference. Do not run any model inference on CPU. Do not claim a model/audio capability works until its input delivery and V100 execution have been measured. CPU DSP and pure Python control work are allowed.
- Mark unverified REAPER behavior, listener delivery, model quality, human calibration and owner-chosen thresholds explicitly. Keep coding independent of those gates only where the blueprint permits; acceptance must remain blocked until evidence exists.
- Save policy must never silently overwrite an existing project or create unlimited versions. No project reload or dirty-state reset may be assumed to verify an operation.

## Verification and delivery

In implementation waves, add tests for new behavior and run the focused tests plus the relevant existing suite at each integration gate. Do not report “all works” based only on static inspection or a mock bridge. Clearly separate unit, bridge, isolated-live REAPER, V100/listener and human-listening results. For tests that cannot run because REAPER/V100/model/audio host is unavailable, deliver the exact fixture/procedure and mark the gate blocked or unverified.

At each meaningful milestone, send a short update in this same Hermes chat: files/modules completed, subagents and ownership, tests run/results, blockers and next wave. At completion of the first implementation wave, return a compact integration report, updated DECISION-LEDGER.md entries, and the next safe wave. Do not claim the overall product complete until P0–P7 gates and the end-to-end acceptance matrix pass.

