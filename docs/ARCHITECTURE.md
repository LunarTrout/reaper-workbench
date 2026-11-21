# Architecture and data flow

REAPER Workbench separates host integration, project operations, signal analysis, and model-facing discovery. The MCP server is an optional interface over the Python library; it is not the implementation of REAPER access.

## Runtime path

~~~mermaid
flowchart LR
  Client[MCP client or Python application] -->|stdio or Python API| Core[Python library]
  Core --> BridgeClient[ReaperBridge client]
  BridgeClient -->|local request/response files| Lua[Resident Lua ReaScript]
  Lua -->|documented ReaScript API and Actions| Reaper[REAPER]
  Reaper -->|project context, render outputs| Lua
  Lua -->|structured response| BridgeClient
  BridgeClient --> Core
  Core --> Analysis[Audio analysis and comparison]
  Analysis -->|report, leased audio resources| Client
~~~

The resident script executes inside REAPER's Lua runtime. The Python process communicates with it through a local file mailbox under the current Windows user's `%LOCALAPPDATA%`. The bridge does not open a network port, inject into REAPER, or inspect private process memory. This transport keeps REAPER-owned objects on the REAPER side; Python carries project IDs, GUIDs, revisions, and typed values rather than raw host pointers.

## Project identity and context

Each request is associated with a bridge session and a project identity. The project ID is derived from the project path and master-track GUID because ReaScript does not expose a dedicated project GUID getter. A revision carries four continuity components: `project_id`, the running bridge `instance_id`, a per-project `epoch` assigned by the bridge, and REAPER's monotonic `change_count`. The epoch advances when the bridge observes a discontinuity — a project object it has never seen in this instance, a changed project identity, or a change counter that moved backwards (a reload). A missing instance or epoch is *unknown*: the server records the gap and refuses to reuse context across it, because a restarted REAPER can reuse both the path and the counter.

A project snapshot includes a monotonic change count and stable object GUIDs. Depending on the requested detail, context can include:

- tracks and master, names, indices from the current snapshot, fader/pan/mute/solo;
- folder hierarchy, sends, receives, parent/master routing, and channel configuration;
- items, takes, source media, positions, lengths, offsets, play rate, fades, channel mode, group id, stretch markers, and active take;
- FX chain order and optional parameter values;
- tempo and time-signature map, transport, markers/regions, envelopes, and optional MIDI note data.

Indices are snapshot-local hints, not persistent object identities. An edit uses GUIDs to resolve the object again in REAPER and an expected project revision to reject stale context. A fresh read is required after a render, save-as, project switch, or any edit that changes the project state count.

## Capability discovery

The bundled API catalog is generated from Cockos' official ReaScript reference. It contains the v7.85 documentation snapshot and 732 function signatures. The count describes the bundled reference, not the API exposed by every installed REAPER build and not 732 live-tested features. Calls are checked against runtime availability where possible.

The model interface does not create a tool for every API function. Forty MCP tools organize session health, project context, capability browsing, structured API search, integrated analysis, edit maps, mix evidence, revision-bound edits authorised by a cheap structural token (a panorama receipt is still accepted), one plan-bound candidate render, generic API calls, Actions, explicit saving, artifact release, listener/spatial diagnostics, timeline context, the paged project view, its generic exact-detail query and change updates. Project context is delivered as one compact columnar view split into measured pages: `open` returns page 1 and a manifest, `page` fetches the rest, and `ack` verifies every page hash and reconstructs the view before minting the receipt. There is no page-count limit and no byte-level transport on the model-facing surface; each page is measured against the response cap. Five resource templates and three static resources (`reaper://guide/model`, `reaper://handbook`, `reaper://capabilities/index`) expose the model guide, capability index, function documentation, revisioned project context, analysis reports, and leased audio. The exact schemas returned by `tools/list` are authoritative.

Typed edit plans cover operations that have a defined precondition, state snapshot, apply path, and undo behavior. Generic ReaScript calls and Actions remain available for broader control, but an arbitrary call is not automatically transactional or guaranteed to be undoable by the library.

## Editing and state transitions

For a typed edit, the library follows this sequence:

1. Read the project identity, revision, and target objects.
2. Validate the requested operation and prepare a preview against that revision.
3. Reject the plan if its project, instance, epoch, or revision no longer matches.
   Apply and undo also require a current structural token (`project_id`,
   instance, epoch, revision and the project-map digest, minted by one cheap
   read); a panorama receipt is still accepted. Preview needs no token.
4. Apply the batch inside a REAPER undo block.
5. Read back touched objects and compare the resulting state with the plan.
6. Read the post-operation project context and digest; project the changed
   facts onto the model's known state.
7. Return the post-edit revision and a change update (stable ids, new current
   values, audio freshness and the successor context token); undo is accepted only
   against the exact revision produced by that transaction and is itself a new
   forward revision with its own update.

This is a constrained safety boundary, not a sandbox. A raw API call or Action can have project, preference, filesystem, transport, plugin, or external-device effects that are outside the typed edit layer. Such calls return a change update with `scope="unknown"`: the server invalidates all project audio evidence conservatively instead of pretending it knows which objects survived, and requires operation-specific checks plus a fresh readback and capture.

### Change updates and evidence freshness

A full panorama is delivered to establish the model's known project state. It is
not a precondition for an edit: edits use the structural token above, so a local
change no longer costs a whole-project page walk. The context token identifies
that delivered state and advances with later updates;
the model does not need the full panorama repeated after every edit. A token
already at the latest known revision returns `not_modified`. If sealed mutations
follow it, `changes_since` returns only their net current values (newest record
wins) and reuses a proven successor token when the update fits inline. A live
revision newer than every sealed record returns
`stale_base/live_ahead_of_archive` instead of a false `not_modified`.

Every verified mutation returns a model-facing `change_update` relative to the
last delivered context: the current revision, stable IDs and only the new current
values of changed facts, plus a separate audio-freshness report. Unchanged facts
and the original panorama are omitted. The full server-side journal (including
before/after facts and invalidation reasons) remains available for audit but is
not serialized into this normal update. The successor token accompanies an
inline update; a large update is delivered as complete `change_page` pages and
advances the token only after every page is acknowledged.

When a newer full capture exists, `reaper_panorama_changes_since` can also
perform an explicit comparison between two archived captures. That comparison
is a distinct, factual version-comparison path and may include the exact values
on both sides; it is not the normal mutation update and does not issue a
better/worse judgment. A call without a context token is informational and does
not establish continuity for future edits. Use `refresh` for a lost or stale
context, a new model session, or when a new whole-project view is needed.

Audio freshness is keyed by (source, stage) and answers `current`, `stale` or
`unknown` with a reason — never “no change” for missing evidence. A coverage
passport records which sources and intervals are measured; a `global` claim needs
both every source and intervals that cover the claimed span.

Audio evidence is treated separately from structure. Each captured source and
the master carry a deterministic `render_key` computed over the signal path the
capture actually used: track state (including per-track pan law), normalized item
state chunks, FX identity, bypass and parameters, serialized FX-chain state,
automation, routing, folder ancestry, tempo and render settings, plus the master
chain because stems render via master. The capture records this manifest inside
the sealed record and the panorama. An update compares the target manifest with the
base manifest and names exactly which captured nodes survived. If either manifest
is incomplete, the verdict is unknown and all project audio is invalidated. The
manifest also declares dependencies it cannot prove instead of assuming them
equal: media-file bytes on disk, external realtime input, the project default pan
law (per-track `D_PANLAW` is read, but REAPER exposes no documented numeric key
for the project default), the loaded plugin binary version, and track routing
flags the numeric digest does not expose. Those classes are not mapped to
individual sources, so a cross-capture comparison is non-authoritative whenever
they are present: a mutation update invalidates all captured audio at the old
revision, even when every known render key is equal. The capture itself remains
valid, current evidence for its own revision; only survival into a later revision
is unproven. An update that changed a known key marks every dependent asset,
analysis and relation stale; project-audio interpretation refuses until a fresh
capture is acknowledged. Historical captures are never rewritten: they remain
readable as historical evidence with their own receipt.

## Audio capture and measurement

Project analysis uses REAPER's native offline-render path to obtain individual source outputs and a separate master output for a selected time range. Each source is bound to the project and capture revision, GUID, render window, sample rate, and available mixer context. The capture temporarily uses REAPER render settings and target selection, then restores them; REAPER may still advance its project change counter. Consumers must use the returned post-capture revision and must not reload the project or reset that counter.

Captures are incremental by default: a source whose render fingerprint is
unchanged is reused from the archive (`reused: true` with its capture id and
render key) and the master is always rendered. Measurements are cached by render
fingerprint, window, sample rate, channels, stage and analyzer; the WAV hash is
kept as a check, not as the cache key. Each capture also writes one self-contained
document per source plus a thin `manifest.json` (`source -> doc, sha256, render
key`), while `record.json` stays the sealed integrity anchor and is never
rewritten.

Every captured source also receives a render-dependency manifest (see *Change updates and evidence freshness*). The manifest is part of the sealed record and the panorama document hash, and it names the known dependencies of each source. It does not let a later edit call a source "provably survived" while unresolved classes remain; those classes are declared as unverified rather than assumed equal, and they make cross-capture freshness unknown.

The analysis bundle combines per-source measurements, temporal profiles, event/transient evidence, tempo annotations from the project map, and optional reference or source-to-source comparisons. It reports missing coverage and provenance instead of silently treating unavailable data as zero. It does not select a mix target, genre rule, or mastering limit on the user's behalf.

`GetAudioAccessorSamples` is a distinct pre-FX source tap. It is not an equivalent replacement for a processed track render. The capture point is therefore part of the data model and must accompany any report or audio resource.

Rendered WAV files are stored outside `.RPP` in a per-job directory. The manifest identifies owned artifacts so cleanup can remove only library-created files. MCP analysis records, plan-bound candidate records and audio resources have a bounded lease; `reaper_release` removes a completed job. A client may forward audio to its model provider; provider-side retention is controlled by that client, not this library.

## Why MCP is compact

The underlying REAPER surface is broad; the model-facing tool list remains organized around project context, discovery, analysis, supported edits, and explicit low-level calls. A model can inspect a capability domain or search by structured object/operation/qualifier fields before calling a function. That keeps schemas discoverable while preserving access to the broader ReaScript catalog. It does not impose a fixed music-production pipeline.

## Current limits

- Windows is the only supported bridge platform at present; the runtime path depends on `LOCALAPPDATA`.
- The API catalog is wider than the set of live-tested calls and typed transactions.
- Generic API calls and Actions are not covered by typed edit rollback guarantees.
- Audio capture uses offline rendering; it is not a zero-latency stream and can advance the project's internal change count even after settings are restored.
- Capture, analysis and candidate rendering currently call `_focus_reaper_window`, which can restore a minimized REAPER window and bring it to the foreground. Minimized/background operation without focus changes is not yet supported or verified.
- Signal-stage equivalence must be checked for the REAPER version, render path, routing, FX chain, and requested source. Pre-FX AudioAccessor output is explicitly different.
- This is a pre-1.0 development build with no stable compatibility promise.
