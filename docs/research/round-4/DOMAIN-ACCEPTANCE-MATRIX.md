# Functional-domain coverage and acceptance matrix

This is a current-state inventory, not a release claim. API counts come from the 732-entry catalog grouped by the current `mcp_server._api_domains()`. “Typed operations” means operations routed through the library's preview/apply/readback/undo transaction contract; it does not imply that every field in that domain is exposed or live-verified.

## Summary

- **15 discovery domains** cover the 732 documented REAPER functions.
- The MCP server has **19 tools**; it does not create a separate tool for every ReaScript function.
- **8 domains** have at least one typed transaction operation, but several are narrow or incomplete.
- Other domains remain available through generic ReaScript API calls or Actions where applicable; those calls do not automatically inherit typed transaction safety.
- Wave C's strongest live proof is the project digest and full track/master audio capture on one REAPER 7.84 disposable fixture. It is not broad functional coverage.

## Domain inventory

| Catalog domain | API entries | Typed coverage currently present | Live evidence in Wave C | Main remaining acceptance work |
|---|---:|---|---|---|
| `actions_keyboard` | 18 | Search/invoke Action tool; not an edit transaction | None in Wave C | Search correctness, invoke/readback, stale project, failure behavior |
| `audio_capture_measurement` | 29 | Batch capture/analyze/release; typed capture provenance | **Strong, limited:** 11 tracks + master; full digest; settings restore; independent master render null | MCP tail/channel/rate pass-through; audio delivery to a real model; more routing/FX topologies, scale, REAPER versions |
| `automation_envelopes` | 49 | `envelope.set` | **One case:** master tempo-map point changed digest and undid | Track, item/take, FX envelopes; insert/update/delete, readback and curves under automation |
| `devices_control_surfaces` | 8 | Generic API only | None | Discover runtime availability, read/write contract, safety scope; decide which functions belong in typed operations |
| `fx_plugins` | 105 | Enable, parameter, insert, delete, preset, move, offline; get-chunk support | Master FX enable and parameter mutation changed digest and undid | FX insertion/deletion/reorder, plugin parameter units, chunks/state, failures and undo; representative built-in and third-party FX |
| `items_takes_sources` | 42 | Item set/create/delete/split/duplicate; take set | No broad item/take operation in Wave C; track/item counts stayed unchanged | Split/fades/slip/stretch markers/take selection/source offsets, GUID stability, collision/failure, full undo/readback |
| `markers_regions` | 35 | Marker create; region delete | None in Wave C | Region create/update/delete, marker update/delete, ordering, project-time readback |
| `midi` | 53 | `midi.create` only in typed transaction layer | None in Wave C | Read/write/edit/delete/quantize/transpose; looped takes, PPQ/time-map conversion, note readback and audition evidence |
| `other_documented_api` | 168 | Generic API call only | None as a domain-wide group | Runtime/API-version audit, argument and return schemas, high-value safe wrappers, unknown/unsupported handling |
| `project_state_persistence` | 54 | Project context/digest and save tool; capture restores render settings | Digest/settings restore on one fixture | save/save-as collision rules, backup/version lifecycle, save failure and restart recovery, complete project-setting coverage across versions |
| `routing_recording` | 15 | Send set/create/delete | None in Wave C | Track/bus/master/hardware routing, receive paths, pre/post modes, feedback/loops, recording inputs and safety/readback |
| `tempo_grid_meter` | 34 | `tempo.set` | One master tempo-map automation point, not a full tempo editing matrix | BPM/time-signature markers, 6/8 and 7/8, mid-bar markers, linear ramps and beat/QN conversion |
| `tracks` | 81 | Track set/create/delete/reorder | Master volume changed digest and undid; no broad ordinary-track lifecycle run in Wave C | Ordinary-track create/delete/reorder, folder/group behavior, gain/pan/mute/phase, inputs/outputs, GUIDs and undo |
| `transport` | 26 | Generic API/Action surface | None | Play/stop/seek/record semantics, playback-state preconditions for render, stale transport and failure recovery |
| `ui_windows` | 15 | Generic API/Action surface | None | Identify headless vs UI-dependent APIs; avoid relying on window state for core operations; runtime checks where supported |

Typed transaction names presently listed by the server (25): `track.set/create/delete/reorder`, `item.set/create/delete/split/duplicate`, `take.set`, `midi.create`, seven `fx.*` operations, `send.set/create/delete`, `marker.create`, `region.delete`, `envelope.set`, and `tempo.set`.

The eight partial typed domains are tracks, items/takes/sources, MIDI, FX, routing/recording, markers/regions, automation/envelopes, and tempo/grid/meter. Project persistence and audio capture/measurement have dedicated tools outside the edit-operation set; Actions have a separate search/invoke path. Four groups—devices/control surfaces, transport, UI/windows, and the 168-entry miscellaneous domain—have no comparable typed transaction layer in the current server.

## What this does and does not count

Do not report “8 of 15 complete.” The matrix counts whether a domain has any typed operation, not whether it can perform every useful action. A live pass for one parameter or one object type does not close the domain. Each domain needs operation-level tests (valid/invalid inputs, stale revision, independent readback, undo/recovery where supported) and a real REAPER scenario for the sound-affecting or destructive paths.

The 732 catalog entries are documented/discoverable and can be routed through generic API invocation subject to installed REAPER runtime availability. They are not 732 verified capabilities. The remaining rewrite effort should be prioritized by end-to-end user tasks and safety boundaries, then expanded based on the domain matrix—not by writing 732 wrappers.

## Release-level acceptance floor

For a functional domain to be called verified, record:

1. API and REAPER version/runtime availability.
2. Object IDs and project/revision scope used.
3. Preconditions and expected state delta.
4. Preview, apply, independent readback, and undo/recovery evidence where the operation can be reversed.
5. Audio recapture/analysis for sound-affecting edits, with the project-time window, source stage, channels, sample rate, and master included.
6. A negative case (stale context, missing object/FX/API, invalid value, or partial failure) proving the system fails clearly and leaves the project in a known state.

Wave C's separate master render equality is a strong capture-equivalence result, but it is not a substitute for these per-domain gates, actual model audio delivery, or user listening to a mix.
