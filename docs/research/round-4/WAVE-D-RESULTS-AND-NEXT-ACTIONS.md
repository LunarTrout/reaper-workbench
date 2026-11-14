# Wave D — results gate and next actions

This is a focused follow-up to `WAVE-D-AND-HIMMP-AB-EXECUTION-DISPATCH.md`.
It exists to turn the current findings into implementation and observable results.

## Verified now

- Full-project tail handling is fixed and fail-closed. The bridge's reported tail,
  rendered frame count, and REAPER project length must agree within one sample frame;
  otherwise the measured boundary is unknown and capture is rejected.
- `reaper_analyze` now keeps the measured project window separate from the captured
  file window. A requested render tail may be present in captured audio but is excluded
  from project measurements.
- Regression coverage includes full-project and section captures, tail rounding,
  malformed or missing boundary evidence, mismatch, short render, and the MCP guard
  against unknown project boundaries.
- Complete suite: Python 3.13.17: `760 passed, 2 skipped, 3 warnings` (14.90 s);
  Hermes Python 3.14.9: `760 passed, 2 skipped, 3 warnings` (16.01 s).
- Current-checkout MCP stdio proof passed on a disposable 2-second synthetic project:
  two tracks plus master, no explicit section, 500 ms tail; all three stereo resources
  and hashes verified, release removed the job, and the `.RPP` hash and project
  structure stayed unchanged. See `WAVE-D-LIVE-MCP-REPORT.md`.
- HiMMP inputs are now staged and hash-verified, and the disposable REAPER project
  builder exists. The autonomous mix has not yet been frozen or compared with a
  producer reference; that A/B result remains pending.

## Required next actions — do these in order

1. **Finish the HiMMP end-to-end benchmark.** Let Hermes build and work only in the
   dedicated disposable project. Verify that the source set and tempo map are interpreted
   correctly, then let the model make and inspect mix changes through the library.
2. **Freeze one autonomous result before reference access.** Save and hash the final
   project and full-song master; do not load or reveal producer reference audio before
   this freeze. Retain source, the frozen final, reference and manifests; do not retain
   intermediate stem WAVs.
3. **Run the same-song comparison.** After the freeze, obtain a legally usable producer
   mix and compare it against the autonomous full mix with aligned windows and explicit
   provenance. Report objective measurements and the listening artifacts without
   assigning a library-generated quality verdict.
4. **Continue live functional-domain checks** against disposable projects for the
   operations needed to complete the work. The synthetic tail-boundary proof does not
   establish complete project editing or full-song mix behavior.
5. Keep the persistent ledger as a design proposal until implemented and tested. The
   current capture proof does not establish full chronological history, dependency
   resolution, or unified model-and-DSP snapshots.
6. Report each item as `verified`, `failed`, `blocked`, or `not started`, with file/log
   evidence. Do not turn an unfinished implementation proposal into a claimed feature.

## Context ledger status boundary

`CONTEXT-LEDGER-DESIGN.md` is useful as a design proposal but explicitly states there
is no persistent ledger, dependency resolver, view builder, or ledger MCP tool. Do not
claim the universal project-context contract is implemented. Continue only the minimum
context work needed for the current live capture and autonomous mix; prioritize a
demonstrated full-cycle result over expanding the proposal.

## Semantic audio-model / capture boundary

Current-code audit (not a proposal): `reaper_analyze` does not invoke either
semantic model. `reaper_flamingo_describe` and `reaper_spatial_analyze` are
separate MCP calls; they take a fresh project snapshot, render the selected
master/track again, run inference, then delete their private temporary render.
Their reports include project/timeline context, but do not accept the audio
capture's `analysis_id` or attach to its `Capture` record. Flamingo consumes
one mono 16 kHz representation per run (L/R/Mid/Side are separately labelled
options); Ossicle consumes the true stereo L/R signal. `reaper_spatial_timeline`
is a DSP measurement tool, not a model call.

Therefore a caller can currently receive a DSP/audio capture and later receive
model descriptions from another render/revision. Do not describe the existing
MCP response as a unified model-and-DSP snapshot. For the eventual shared
context contract, every model result must identify the exact project revision,
source/capture asset, time window, channel view, model/checkpoint and inference
status. Prefer deriving the model input from the already captured asset and
linking any resampled/downmixed derivative to it; if a fresh REAPER render is
required, record it as a separate capture with its own revision and do not
silently merge it with older measurements. Model unavailability or failed
delivery must be explicit, never represented as a successful listening result.

## User retention requirement — full history, no intermediate stem WAVs

Retain the **full, lossless chronological project-state/evidence history** for
captures, complete project snapshots, revisions, measurements, model outputs,
claims, edits, readbacks and verification/undo events. Snapshots must include
all state exposed by REAPER that can affect sound or editing: tracks/items/takes,
FX chain order and complete available parameter/state data, routing/sends,
automation/envelopes, MIDI, markers/regions, tempo/time maps, master state and
relevant project/render settings. Do not silently drop fields to make a record
or view smaller. Store opaque/large native state as hash-verified external
records/blobs if needed, with exact links from the snapshot.

For every transition retain the full machine-readable before/after delta and
the exact revisions, object IDs, source evidence and verification result. Also
retain the model's description of what it intended and did as a separately
labelled, model-authored annotation; the observed REAPER diff remains the
authority for what actually changed. Mark unattributed/external edits as such
and invalidate dependent conclusions.

Deltas cover **every exposed project domain**, not only FX: track/item/take
structure, routing, faders/pan, FX and their full available state, automation,
MIDI, tempo/time maps, markers/regions and project/render settings. For each
change retain a neutral, human-readable description of what changed, linked to
the machine-readable delta and any measurements. Do not label the result
`better`, `worse`, `mixed`, or `inconclusive`, and do not compute or store a
quality verdict. The model receives the descriptive evidence and draws any
task-specific conclusion itself when asked; that conclusion is not a property
of the ledger or analysis library.

Any compact Tier-0 brief or task-specific view is only a derived entry point.
The complete source records and exact paging/expansion path must remain
available, and every view must state coverage/truncation. “Compact” must never
mean lossy retention or hidden omissions.

Do **not** retain intermediate per-track rendered WAVs/stems as durable
history. Keep them as manifest-owned temporary assets and remove them after
their lease or explicit release. Preserve original source media and the single
frozen final deliverable under the benchmark policy; retain hashes/metadata
for expired temporary audio so the record still proves what was measured,
without implying that the old audio can still be replayed. A retained capture
record is not a retained stem file.
