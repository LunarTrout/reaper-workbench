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

Each request is associated with a bridge session and a project identity. The project ID is derived from the project path and master-track GUID because ReaScript does not expose a dedicated project GUID getter. A project snapshot includes a monotonic change count and stable object GUIDs. Depending on the requested detail, context can include:

- tracks and master, names, indices from the current snapshot, fader/pan/mute/solo;
- folder hierarchy, sends, receives, parent/master routing, and channel configuration;
- items, takes, source media, positions, lengths, offsets, play rate, fades, and active take;
- FX chain order and optional parameter values;
- tempo and time-signature map, transport, markers/regions, envelopes, and optional MIDI note data.

Indices are snapshot-local hints, not persistent object identities. An edit uses GUIDs to resolve the object again in REAPER and an expected project revision to reject stale context. A fresh read is required after a render, save-as, project switch, or any edit that changes the project state count.

## Capability discovery

The bundled API catalog is generated from Cockos' official ReaScript reference. It contains the v7.85 documentation snapshot and 732 function signatures. The count describes the bundled reference, not the API exposed by every installed REAPER build and not 732 live-tested features. Calls are checked against runtime availability where possible.

The model interface does not create a tool for every API function. Twelve MCP tools organize session health, project context, capability browsing, structured API search, integrated analysis, edit maps, mix evidence, revision-bound edits, generic API calls, Actions, explicit saving, and artifact release. Five resource templates expose capability documentation, function documentation, revisioned project context, analysis reports, and leased audio. The exact schemas returned by `tools/list` are authoritative.

Typed edit plans cover operations that have a defined precondition, state snapshot, apply path, and undo behavior. Generic ReaScript calls and Actions remain available for broader control, but an arbitrary call is not automatically transactional or guaranteed to be undoable by the library.

## Editing and state transitions

For a typed edit, the library follows this sequence:

1. Read the project identity, revision, and target objects.
2. Validate the requested operation and prepare a preview against that revision.
3. Reject the plan if its project or revision no longer matches.
4. Apply the batch inside a REAPER undo block.
5. Read back touched objects and compare the resulting state with the plan.
6. Return the post-edit revision; undo is accepted only against the exact revision produced by that transaction.

This is a constrained safety boundary, not a sandbox. A raw API call or Action can have project, preference, filesystem, transport, plugin, or external-device effects that are outside the typed edit layer. Such calls require operation-specific checks and a fresh readback.

## Audio capture and measurement

Project analysis uses REAPER's native offline-render path to obtain individual source outputs and a separate master output for a selected time range. Each source is bound to the project and capture revision, GUID, render window, sample rate, and available mixer context. The capture temporarily uses REAPER render settings and target selection, then restores them; REAPER may still advance its project change counter. Consumers must use the returned post-capture revision and must not reload the project or reset that counter.

The analysis bundle combines per-source measurements, temporal profiles, event/transient evidence, tempo annotations from the project map, and optional reference or source-to-source comparisons. It reports missing coverage and provenance instead of silently treating unavailable data as zero. It does not select a mix target, genre rule, or mastering limit on the user's behalf.

`GetAudioAccessorSamples` is a distinct pre-FX source tap. It is not an equivalent replacement for a processed track render. The capture point is therefore part of the data model and must accompany any report or audio resource.

Rendered WAV files are stored outside `.RPP` in a per-job directory. The manifest identifies owned artifacts so cleanup can remove only library-created files. MCP analysis records and audio resources have a bounded lease; `reaper_release` removes a completed job. A client may forward audio to its model provider; provider-side retention is controlled by that client, not this library.

## Why MCP is compact

The underlying REAPER surface is broad; the model-facing tool list remains organized around project context, discovery, analysis, supported edits, and explicit low-level calls. A model can inspect a capability domain or search by structured object/operation/qualifier fields before calling a function. That keeps schemas discoverable while preserving access to the broader ReaScript catalog. It does not impose a fixed music-production pipeline.

## Current limits

- Windows is the only supported bridge platform at present; the runtime path depends on `LOCALAPPDATA`.
- The API catalog is wider than the set of live-tested calls and typed transactions.
- Generic API calls and Actions are not covered by typed edit rollback guarantees.
- Audio capture uses offline rendering; it is not a zero-latency stream and can advance the project's internal change count even after settings are restored.
- Signal-stage equivalence must be checked for the REAPER version, render path, routing, FX chain, and requested source. Pre-FX AudioAccessor output is explicitly different.
- This is a pre-1.0 development build with no stable compatibility promise.
