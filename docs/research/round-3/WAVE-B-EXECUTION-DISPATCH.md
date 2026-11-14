# REAPER Workbench — Wave B implementation dispatch

## Objective

Integrate the frozen Wave A contracts with the existing REAPER project snapshot,
audio-capture and artifact-lifecycle path. Build one truthful capture-and-revision
vertical slice. This wave does not implement MCP changes or claim that the whole
mixing system is complete.

## Verified starting state

- Wave A contracts are present under `src/reaper_audio/context/`,
  `src/reaper_audio/timebase.py`, `src/reaper_audio/analysis/event_model.py`,
  and `src/reaper_audio/mix/models.py`.
- The current local full test suite passed: **516 passed**. Hermes recorded
  303 passing tests across its six focused Wave A test files.
- The new contracts are not yet imported by `daw/session.py` or
  `mcp_server.py`; existing capture and MCP behavior remains the old path.
- `context/revision.py` explicitly returns a coarse placeholder when the bridge
  supplies no measured content digest. The current wire identity/change counter
  is not proof of equal project content.
- The shared tree contains pre-existing dirty files. Preserve them; do not reset,
  revert, clean, commit or overwrite unrelated work.

## Parallel work — at most two subagents

Hermes is the sole integrator and may spawn **zero, one or two subagents total**.
Never spawn more than two. If the work cannot be safely split, use one agent or
work serially. Assign disjoint file ownership before either agent edits, and
report actual status rather than implying agents ran.

Owner steering: an initial Wave B invocation carrying the obsolete six-agent
wording was interrupted before the corrected dispatch. First inspect whether
that interrupted run left any workers active. Stop duplicate or excess workers
so the total for this task never exceeds two, then continue from the current
working tree without repeating Wave A.

1. **Lua bridge owner:** sole owner of `scripts/reaper_bridge.lua`. Specify and
   implement the narrowly scoped project-digest/readback and capture metadata
   needed for Wave B. Preserve existing edits in that file. Report exactly which
   project fields are covered; do not label a partial digest as full content
   identity.
2. **Python capture owner:** sole owner of `src/reaper_audio/daw/session.py`,
   `src/reaper_audio/reaper/bridge_client.py`, and
   `src/reaper_audio/analysis/artifacts.py`. Adapt capture to typed `Capture`
   and `AudioAsset` records; validate the bridge response; hash and register
   assets; restrict cleanup to manifest-owned paths.

Hermes itself owns `src/reaper_audio/context/revision.py`, test/fixture files,
integration, and final review. If a lane becomes too broad, stop and split only
within the two-agent maximum; do not add an independent third reviewer.

If an ownership conflict appears, stop the affected lanes and publish a revised
ownership table before editing.

## Required behavior

- A normal analysis capture returns the individually rendered project tracks
  and a separate master render for the requested project-time window.
- Each audio asset carries stable source/object identity, stage, capture method,
  channel view, sample rate, exact time window, content hash, lifecycle status,
  and source revision. `track_stem_post_fx` and `master_mix_post_fx` require
  verified native-render semantics. `audio_accessor` may only be called
  `take_sum_pre_fx`; never label it a processed stem or DI without evidence.
- Capture must disclose selection, render-setting, dirty-state and other side
  effects; attempt restoration in `finally`; read back the restored state; and
  report incomplete restoration as failure/unverified. It must not add tracks or
  items to the project.
- Temporary audio is external to the `.RPP`, owned by a manifest, and cleaned
  only by that manifest on both success and failure. Keep long-lived project
  versions bounded to original and final; do not accumulate intermediate stems.
- Any revision digest states its field coverage and scope. A partial digest can
  invalidate affected evidence but cannot assert `same_content`. Add mutation
  tests for every included sound-affecting field and prove unrelated metadata
  does not create a false content claim.
- Keep the new types at one canonical import path. Reuse the Wave A validators;
  do not create a second ID grammar or incompatible error serialization.
- Do not change `mcp_server.py` in this wave. Do not use Frida, private process
  memory, undocumented hooks, model inference on CPU, or external uploads of
  user audio/projects.

## Verification gates

### Unit and fake-bridge

- Capture request/response schema validation and compatibility.
- Stage/method constraints; exact window/channel/sample-rate propagation;
  asset hash and manifest ownership.
- Successful restore and injected render/write/cleanup failures; no unowned
  deletion, no false success, no leaked temporary files.
- Revision fingerprints change for each covered sound-affecting field; partial
  or unavailable fingerprints never satisfy `same_content`.
- Existing public models and old session behavior remain importable where
  compatibility is required.

### Isolated live REAPER

Use a disposable copy or generated fixture, never the user's live project.
Exercise track and master renders with known dry audio, track FX, master FX,
fader/pan, mute/solo, routing/send, stereo and tail cases. Read back project
state after capture. Compare the capture with an independently requested native
render of the same source, stage, window and settings. If REAPER or a required
fixture is unavailable, report this gate as blocked/unverified; do not infer
success from unit tests.

## Wave B exit report

Return: zero-to-two subagent assignments and actual statuses; changed files; exact focused
and full-suite commands/results; live-REAPER fixture and results or blocker;
field coverage of the revision digest; known gaps; and the smallest next safe
wave. Do not claim audio equivalence, full revision freshness, complete REAPER
coverage, model hearing or production readiness without the corresponding live
evidence.
