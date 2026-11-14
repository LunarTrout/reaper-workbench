# MCP context and real-world release gate

## Why this gate exists

The product requirement is that the REAPER library expose the full practical DAW capability to the model and maintain enough current project context for safe, coherent decisions. The model must not have to reconstruct hidden state from unrelated shell scripts. A standalone `list_tools` or stdio smoke test does not prove that the active model session can use this interface.

Current evidence in the benchmark session is mixed: a separate MCP test discovered 19 tools, but model-visible analysis calls previously failed validation and lost their stdio process. Later REAPER capture and comparison work used terminal-invoked Python helpers, including direct `ReaperBridge.capture_render`. Those results may serve as offline evidence, but they do not pass the MCP product gate.

The first mix has direct user feedback: it is better than the dry sum but far below the commissioned producer reference; source balance is poor and the user hears too little processing, without identifying a specific missing effect. This is an open-ended listening task, not a request to apply a fixed effects chain.

## Required library behavior

1. **Canonical live project context.** Through MCP, the model can obtain the active REAPER instance and project identity, a revision token, and a linked project map. The map must make all relevant entities discoverable: master, tracks and folders, stable IDs, parent/group relationships, sends and routing, FX chain order/identity/on-off state/parameters, items/takes/source media and time positions, MIDI, envelopes and automation, tempo/time-signature map and changes, markers/regions, selection and transport state, and render/capture settings. Report omitted or paginated data explicitly and make it searchable without losing project identity or revision.
2. **Context follows every action.** Read and mutation tools accept stable object IDs and the current project/revision context. Results state exactly which entities changed, the new revision, affected relationships, and what is stale. A later tool call must not silently act on a different REAPER instance, project, selection, or revision.
3. **Complete capability discovery.** The full REAPER/ReaScript surface remains reachable through the library, including less common domains. Do not give the model an unfiltered list of hundreds of unrelated calls. Provide structured search by object, operation, and qualifiers, then expose the exact callable schema and relevant constraints. Keep higher-level read, analysis, and transaction tools that bundle common context automatically.
4. **Audio is model-visible and revision-bound.** Captured track, bus, and master audio must be traceable to project revision, exact time window, signal path, channel layout, sample rate, FX inclusion, and capture method. The model must receive actual audio or an attributed audio-model result; a WAV written to disk or a numeric report alone does not mean the model heard it.
5. **Safe state changes.** Model-visible preview, apply, readback, and undo must operate against a bound project revision and stable entity IDs. The result must expose real post-apply state, including routing and FX, rather than only echoing a proposed plan.

## Required acceptance run

Run this against the disposable HiMMP REAPER project, preserving the frozen first mix and the user project:

1. From the **active Hermes model session**, enumerate the connected REAPER MCP entry and confirm tool names/schemas and resources actually available to the model. Select the HiMMP REAPER instance explicitly.
2. Request one coherent project snapshot through MCP and show its project ID/revision, full track/bus/master topology, routing, FX, media/arrangement, tempo/time-signature/automation and relevant selections. Confirm pagination/search does not hide relationships.
3. Through MCP, capture and deliver the same fixed passage from the dry source, current Hermes mix and one producer master. State the exact revision, window, audio path, alignment assumption, level matching and signal path. Use available hearing models only through their registered tools; record exactly what audio each model received and its response.
4. Diagnose the user's stated complaints from the listening evidence and project state. Identify source-balance issues and effect differences with evidence and uncertainty. Do not assume which FX to add, apply a genre recipe, or use independently normalized role stems as evidence of relative role loudness.
5. Prepare a revision-bound edit plan through MCP, apply it only to the disposable benchmark copy, read back the exact changed state through MCP, capture a post-edit passage, and compare again. Undo the trial through MCP and verify restoration. Preserve the first mix and the new revision as separately identified artifacts.
6. Keep offline Python/terminal analysis for independent measurement, file hashing, alignment checks and result formatting. Do not use terminal helpers to control REAPER or to claim that the model-facing MCP path passed.

## Pass criteria

- The active Hermes session succeeds on the model-visible MCP calls above; a separate manual `hermes mcp test`, Python import, or `ReaperBridge` script is insufficient.
- Context identity and revision survive inspection, audio capture, edit preview/apply, readback and undo.
- Audio provenance and what the model actually heard are explicit.
- If any required operation is absent or fails, implement/fix the library/MCP interface and rerun the failed path. Do not mark it ready or route around it with a helper script.
- Report objective measurements as measurements and the user's quality judgement as the user's judgement. Do not create an automatic better/worse verdict.

## Owner clarification — panorama-only state view and version journal

The following requirements were clarified after this gate was drafted. They are
release requirements, not claims about the current implementation.

### Canonical model-facing representation

For REAPER project/audio state, the active model sees one unified panorama
contract. It must not have to choose among the project map, raw full-capture
JSON, standalone analysis JSON, or disconnected per-track payloads as separate
sources of state. Raw records may remain in the internal archive for integrity
and debugging; they are not model-facing alternatives.

The panorama includes the linked project graph, every captured track/bus and
master, item/take/MIDI and time-map context, FX/routing/automation, all available
measurements, coverage, provenance, revision and audio-resource references. A
request for an interval (for example `[36,45)` project seconds) returns the same
panorama contract with all relevant sources together. Audio must be bound to the
same project, revision, interval, signal stage and channel view. If that window
is not in the source capture, the request must capture it at the exact expected
revision or fail explicitly.

Capability discovery and mutations remain separate operations; the invariant
is that project/audio reads and their deltas use the panorama representation.
The model may compose a task-specific route from general REAPER capabilities.
The README must express decision principles, not an obligatory mix/edit order,
genre recipe, instruction to call every analyzer, or instruction to add FX by
default.

### Complete version history and deltas

The owner wants the journal to show what changed in each accepted project
version and how the complete set of available measurements changed across every
track, bus and master. For each comparable metric it preserves raw `before`,
raw `after`, signed delta, unit, method/version, source identity, window, stage,
channel view and both revisions. Structural before/after data covers all
captured project domains. Unchanged values are explicit; missing, partial,
unmatched or incomparable evidence is never silently converted to zero.

Every change links to the responsible operation/plan and post-change readback.
When several edits are batched, evidence may link the result to the batch; it
must not attribute causality to one edit without an isolating comparison. The
journal records measured direction and user/model annotations separately; it
does not assign a universal better/worse score.

The `CaptureArchive` index and retention log are not this journal. They identify
stored captures and retention events, but the current MCP surface has no history
or version-diff operation. `reaper_mix_evidence` summarizes selected evidence
within one analysis; it does not compare two project versions. The current
capture archive keeps successful full captures indefinitely and content-addresses
audio; retention behavior must remain explicit rather than silently deleting
owner-requested captures.

Do not define a semantic version delta as a difference in `change_count` alone.
Capture temporarily changes track mute state to render sources and restores it,
so a counter can advance without a lasting project edit. Compare bound project
fields/fingerprints and label capture-side changes separately. Reject or label
comparisons as incomparable when source identity, stage, channels, time window,
tempo mapping or metric version does not match.

### Additional acceptance run

Add these steps to the required active-model acceptance run:

1. Inspect `tools/list` and resource discovery in the active model session.
   Project/audio reads must offer the panorama as the only state representation;
   raw capture records and standalone JSON reports must not be advertised as
   alternate state views.
2. Capture a full baseline through MCP, then request one interval such as
   `[36,45)`. Confirm one panorama result includes the complete linked project
   context plus audio for every requested track/bus and master that exists in
   that capture. Confirm the model client actually receives/consumes the audio.
3. Make a bounded, revision-bound edit through MCP, capture the resulting
   version, and request a panorama-shaped difference between the two captures.
   Confirm it reports every structural change and every available metric for
   each matched track/bus/master, including unchanged values and explicit
   missing/unmatched coverage.
4. Confirm that temporary capture mutes which are restored do not appear as
   semantic project edits; deliberately mismatched stage/window/channel/method
   pairs are marked incomparable; no better/worse verdict is emitted.
5. Undo through MCP, read back the project and capture state, and confirm the
   journal records the undo as a new event without rewriting the earlier
   captures. Verify that the README explains the flexible operating principles
   and observed anti-patterns without prescribing a fixed pipeline.
