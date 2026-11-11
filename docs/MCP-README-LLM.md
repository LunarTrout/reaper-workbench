# REAPER Workbench MCP: Guide for Language Models

> **Status:** first implementation. The stdio server registers 12 tools and
> five resource templates. Schema registration, catalog coverage, and structured
> API search have automated checks. Live stdio-to-REAPER initialization,
> project context, and a five-second all-track-plus-master analysis/release were
> verified on a REAPER 7.84 test copy. MCP edit preview/apply/readback/undo and
> forwarding audio resources to the model client still need live validation.
> This is not release-ready.
> Use `tools/list` as the source of truth for exact argument schemas.

## Purpose

This interface lets a model understand and work with a REAPER project: inspect
objects, use documented ReaScript functions and Actions, edit and verify project
state, capture and analyze audio, compare material, and save when requested.
User goals may be open-ended. The server exposes capabilities and project state;
it does not prescribe a genre recipe or fixed sequence for mixing, editing, MIDI,
or arrangement work.

Choose the shortest set of calls that preserves enough context to act correctly.
Batch independent reads and compatible edits. Use transaction tools when they
fit; use the full typed API or Actions when they do not. Never infer that an
operation is unavailable only because it has no dedicated MCP tool.

The library talks to REAPER through its resident ReaScript bridge and native,
documented mechanisms. It does not inspect or patch REAPER process memory.

## Tool map

| Tool | Use |
|---|---|
| `reaper_session` | Check bridge health, discover REAPER instances, list open projects. |
| `reaper_capabilities` | Read the domain index or page through the 732 documented API functions. |
| `reaper_api_search` | Find API functions or Actions using a structured object/operation card. |
| `reaper_project` | Read one project's master, tracks, routing, tempo, items, FX, or MIDI context. |
| `reaper_analyze` | Capture and analyze every project track and rendered master in one request. |
| `reaper_mix_evidence` | Summarize measured mix evidence for an existing analysis; it does not choose or apply processing. |
| `reaper_edit_map` | Build cut and seam evidence from an existing analysis; it does not edit the project. |
| `reaper_edit` | Preview, apply, or undo a revision-bound batch of supported edits. |
| `reaper_api_call` | Call a function in the bundled official ReaScript signature catalog. |
| `reaper_action` | Search Actions or invoke one in the main or MIDI Editor section. |
| `reaper_save` | Save the current project or save as a new `.RPP` path. |
| `reaper_release` | Release one analysis report and its temporary WAV files now. |

The official API catalog and runtime support are different facts. A documented
function may not exist in the installed REAPER version. The bridge checks its
availability at call time and returns an explicit error.

## Find an API capability

Do not send the user's whole request as a free-form search sentence. Convert
each capability question into one short card:

- `object`: one REAPER object, such as `media_item`, `track`, `take`, `fx`, or
  `envelope`;
- `operation`: one action, such as `split`, `move`, `read`, `set`, or `delete`;
- `qualifiers`: only behavior that changes the choice, such as
  `at_project_time`, `by_guid`, or `normalized_parameter`;
- `kind`: `api`, `actions`, or `both`;
- `detail`: `names`, `summary`, or `full`;
- `limit`: a small number of candidates, up to 20.

Example for finding the documented split primitive:

```json
{
  "object": "media_item",
  "operation": "split",
  "qualifiers": ["at_project_time"],
  "kind": "api",
  "detail": "summary",
  "limit": 5
}
```

Use `reaper_capabilities(mode="summary")` to find domains. Then page through a
domain, for example `reaper_capabilities(mode="domain", domain="items_takes",
offset=0, limit=30, detail="summary")`. Read `reaper://api/{name}` for the full
official catalog entry. Search results are candidates, not a direction to
execute the first hit.

If a request has two operations, make two cards. Do not combine a track move and
a media-item split into one query. Search finds implementation primitives; the
model decides how they relate to the user's goal and the current project.

## Establish and preserve project context

When the target instance or project is unclear, call
`reaper_session(action="list_instances")` or
`reaper_session(action="list_projects", bridge_session_id=...)`. If multiple
instances are active, carry the selected `bridge_session_id` through later
calls. Do not choose a project from its name alone when the target is ambiguous.

Read the current project with `reaper_project`. Its `revision` contains
`project_id`, `change_count`, and `path`. Address objects with stable GUIDs;
names and list positions are context, not safe identifiers. Request
`include_items`, `include_fx_params`, or `include_midi_notes` only when needed.
Use GUID filters to keep large responses focused.

Every edit plan, save, Action invocation, and project-bound API call carries
`project_id` and the full `expected_revision` from `reaper_project`. If the
revision is stale, reread the project, find out what changed, and prepare a new
operation. Do not replay stale write arguments.

`reaper://projects/{bridge_session_id}/{project_id}/context/{change_count}`
re-reads a project only while that instance and revision remain available. It
is not a frozen historical snapshot.

## Analyze and listen

Call `reaper_analyze` once to run the available analysis suite on all project
tracks and the master. It can analyze a project-time section (`start_s`,
`end_s`), compare full reference files through `reference_paths`, select
reference-file-local excerpts with `reference_windows`, request selected phase
comparisons, or request all pairwise comparisons. Pairwise work grows with
source count; use it when it supports the current question.

Project and reference time are separate timelines. Never assume that a project
selection such as 40–48 seconds identifies the matching passage in a reference
song. Supply a reference-local window only when that passage is known or has been
aligned:

```json
{
  "start_s": 40.0,
  "end_s": 48.0,
  "reference_windows": [
    {"path": "C:/references/song.wav", "start_s": 18.0,
     "end_s": 26.0, "name": "verse reference"}
  ]
}
```

`reference_paths` analyzes each whole file. `reference_windows` adds separately
selected passages in each reference file; its `start_s` and `end_s` are local to
that file, independent of the project selection. The result's `reference_inputs`
states whether each comparison used a full file or a selected window.

The result links each source to its track GUID/context, pan, fader, mute state,
capture metadata, measurements, revision, and audio URI. Inspect
`capture.signal_stage`: `reaper_rendered_track_stem` is one track's rendered
stem; `reaper_rendered_master_mix` is the full master output. The master is a
separate source. `capture.time_window` and `capture.capture_revision` identify
the exact interval and starting project state used for the audio.
`project.revision` is the current REAPER revision after capture; use it for the
next edit. The render path temporarily sets render options and track selection,
then restores them; REAPER's monotonic change counter can advance even when the
library verifies that project context and render settings were restored. For sequential
calls, compare the previous `project.revision` with the next
`project.capture_revision` and inspect the returned context before assuming
there was no intervening edit. Before/after reports are expected to have
different revisions after edits. Correlation between one track stem and the
full master is not a validity test: they are different signals, and routing,
buses, and master processing affect their relationship. Temporal analysis cells
include the effective REAPER project tempo when available. Read `coverage`
instead of assuming optional analyses succeeded.

Audio is captured through REAPER's native render path. `GetAudioAccessorSamples`
returns track signal immediately before that track's FX; it is raw pre-FX audio,
not a processed mixer stem. Read `capture` metadata to see the actual capture
point. Analysis does not add tracks or items to the project.

The tool returns a concise summary and a report URI. Read
`reaper://analyses/{analysis_id}/report` for full metrics, curves, comparisons,
and coverage. Read `reaper://analyses/{analysis_id}/audio/{source_guid}` if the
MCP client can forward WAV resources to the model for listening. A URI existing
does not mean the model heard it. Distinguish numeric results from direct
listening.

For timing, transient, or cut questions, pass the returned `analysis_id` to
`reaper_edit_map`. It returns candidate cut and seam evidence without applying
changes. By default it does not choose cut/keep labels or infer source groups;
`decide=true` explicitly requests heuristic labels, and `source_groups` must be
provided by the caller. Review the project objects and candidates before
preparing any edits with `reaper_edit`.

For a compact reading of an existing analysis, call
`reaper_mix_evidence(analysis_id=...)`. It groups available measurements into
ranked evidence and questions, with the underlying values and coverage. Its
screening thresholds are heuristics; they are not target values, mix rules, or
proof that no other issue exists. It never selects a plugin, parameter, or
processing order.

Temporary WAVs are outside the `.RPP`, registered in a cleanup manifest, and
expire after one hour. The server keeps one analysis per role: `temporary`,
`baseline`, and `final`. Replacing a role releases its previous files.
`reaper_release(analysis_id=...)` releases one report and its audio immediately.
Reading a resource does not extend its lease. These are short-lived analysis
artifacts, not project versions or a permanent stem archive.

## Edit without losing context

For supported library transactions, use `reaper_edit`:

1. Call `mode="preview"` with `project_id`, the full `expected_revision`, a
   short `reason`, and operations containing `kind`, `target_guid`, and `values`.
   Preview does not modify the project.
2. Inspect the plan and before-state. If it addresses the intended objects and
   values, call `mode="apply"` with the returned `plan_id` and same revision.
3. Read touched objects back with `reaper_project`. Apply returns the new
   revision; use it for later changes or undo.
4. Call `mode="undo"` only with the same plan ID and exact revision returned by
   its apply. Undo is available only while that transaction remains the newest
   matching undo record.

The transaction layer currently advertises these kinds through
`reaper_capabilities`: `track.set`, `track.create`, `track.delete`,
`track.reorder`, `item.set`, `item.create`, `item.delete`, `item.split`,
`item.duplicate`, `take.set`, `midi.create`, `fx.set_enabled`,
`fx.set_parameter`, `fx.insert`, `fx.delete`, `fx.set_preset`, `fx.move`,
`fx.offline`, `send.set`, `send.create`, `send.delete`, `marker.create`,
`region.delete`, `envelope.set`, and `tempo.set`.

For item.split, tail_s is the total extension of the left item past the
original split point. move_s and right_trim_s shift the right item's start;
right_trim_s is a project-time offset, not a media-source trim. The candidate
geometry is left_end = cut + tail_s, right_start = cut + move_s + right_trim_s,
and overlap = left_end - right_start. A positive overlap is crossfaded at its
measured length; a gap retains the requested fade_s at the item edges. Seam
search measures both overlap boundaries in the assembled audio and ranks by
the worse boundary. When applying a seam-search row, pass left_extend_s as
tail_s with the same value; do not add move_s or right_trim_s again. Read the
resulting item positions and fades back from REAPER.

Example of a generic operation shape. The GUID and values must come from the
current project and the user's goal:

```json
{
  "kind": "track.set",
  "target_guid": "{TRACK-GUID}",
  "values": {"pan": -0.25}
}
```

Do not turn an open-ended musical request into a fixed transaction recipe. Group
related changes when the transaction layer supports them; use generic API or
Actions for capabilities outside this set. Arbitrary API calls and Actions are
not automatically reversible. After an uncertain timeout, inspect REAPER
before retrying.

## Call the full ReaScript API

`reaper_api_call` accepts an exact catalog function name and a positional
`values` array matching its signature. It also requires `project_id` and
`expected_revision`. Use `null` only for parameters marked optional in the
catalog. `reaper.array` arguments are numeric JSON arrays.

Pass host objects as stable project-bound references:

```json
{"$reaper_ref":{"kind":"MediaTrack","guid":"{TRACK-GUID}"}}
```

For a project handle, use:

```json
{"$reaper_ref":{"kind":"ReaProject"}}
```

If an API call returned a `{"$reaper_handle": ...}` value, pass that entire
value back unchanged. Opaque handles are scoped to the REAPER bridge and
revision that created them; reacquire them after project changes or bridge
restart. A stale handle is an error, not a reason to guess a pointer.

The wrapper checks signature and handle types; the bridge checks runtime API
availability. An API call does not automatically create an undo group or
promise that global/external effects can be reversed. After calls that may
write state, reread and verify the project.

## Search and invoke Actions

Call `reaper_action(mode="search", query_object=..., query_operation=...,
section_id=0)` to find Actions in the main section. The MIDI Editor section is
`32060`. Search matches Action names; inspect the project and candidate before
invocation.

Invoke with `mode="invoke"`, numeric `command_id`, `project_id`,
`expected_revision`, and the matching section ID. Main Actions run against the
specified project through `Main_OnCommandEx`. MIDI Editor Actions require an
active editor. An Action can change project or application state and is not
assumed undoable. Read back the result and use state or audio checks.

## Save and file conflicts

Saving is explicit. `reaper_save(mode="save_current", project_id=...,
expected_revision=...)` saves the current project. `save_as` requires an
absolute `.RPP` path and does not overwrite an existing file unless
`overwrite_existing=true` is explicitly supplied. Respect the user's request
about which project versions to keep; do not create a new copy per iteration.

## Errors and truthful reporting

Tool errors use a stable structure:

```json
{
  "ok": false,
  "error": {
    "code": "stale_context",
    "message": "...",
    "details": {},
    "next_action": "..."
  }
}
```

For `stale_context`, refresh and replan. For `bridge_timeout`, inspect state
before retrying. For `api_unavailable`, check the REAPER version and discover
an alternative. For `validation_error`, correct the field in `details`. A tool
result does not prove that the user's musical goal is complete: report what was
read back, measured, and actually heard, and identify what remains unverified.

Analysis is synchronous in this version; there is no job status/cancel tool yet.
Use a short section when the task concerns an excerpt and do not start a duplicate
capture while a request is still running.

## Checks required before release

On an isolated multitrack project, verify the live stdio connection, project
discovery, context, full analysis of tracks and master, report/audio resources,
API/Action search, edit preview/apply/readback/undo, save, and existing-file
conflict handling. Repeat with MIDI, FX, routing, automation, tempo maps, item
editing, and at least one task not used while designing the interface. Confirm
that the intended MCP client forwards audio resources as audio. Tool count alone
does not establish coverage or reliability.
