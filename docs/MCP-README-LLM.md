# REAPER Workbench MCP: Guide for Language Models

> **Current interface:** the stdio server registers 29 tools and five resource
> templates, including the bundled 732-signature REAPER v7.85 catalog, structured
> API search, generic dispatch, project context, analysis and plan-bound rendering.
>
> **Project context:** open, fetch every page, acknowledge, then act.
> `reaper_panorama_open` returns page 1 of the bounded, self-describing panorama
> for the captured revision plus a manifest of every page (`page_id`,
> `page_hash`, measured size). Fetch the rest with `reaper_panorama_page`, then
> `reaper_panorama_ack` with the page hashes in order; only that receipt yields
> the `context_token` that unlocks interpretation, exact queries and mutations.
> The page count is adaptive: each page is measured and must fit the host's
> configured per-page budget, so a larger project yields more pages. The receipt
> proves delivery of every page of this bounded projection; it does not mean
> every raw audio point or exact relation row is inline, that you understood the
> view, or that you heard audio. The panorama is a complete project map for its
> declared coverage, not full-resolution evidence. Its temporal rows may be
> aggregated; exact high-frequency rows (relation edges, series cells, MIDI notes,
> envelope points) come from one generic
> `reaper_panorama_query` by semantic selection, which returns the whole selected
> fragment or an explicit over-budget report. Deltas carry their successor token
> in the same response; `refresh` is the normal recovery path.
>
> Live stdio-to-REAPER paged load, gated edit, readback, recapture and delta
> passed on a disposable archive capture. Codex live delivery, other harnesses'
> per-result caps, page counts on very large projects and owner listening remain
> unverified; this is not a release claim.
>
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

## Read the panorama without overclaiming

The acknowledged panorama is the whole-project map for one capture and revision;
it is not a lossless, full-resolution dump of the audio analysis. Treat its receipt
as proof that every panorama page was delivered, not proof that every detailed
series was shown, that you interpreted every page, or that an MCP host delivered
the WAV audio to your model.

- Inspect the declared `coverage`, `analysis_contract`, series `count`, temporal
  resolution, aggregation, units, signal stage and provenance before drawing a
  conclusion. A blank, absent, unavailable or aggregated field is not evidence
  that the sound or event is absent.
- The inline cross-source RMS map is bounded (at most 24 buckets). The exact
  `rms_400ms` cells, `band_level` series, temporal profile, relation edges, MIDI
  notes and envelope points are linked to the same capture and can be retrieved
  with `reaper_panorama_query` after acknowledging the panorama. Use the relevant
  source IDs and project-time interval; narrow the interval if the server reports
  `query_too_large`. Do not describe a bucket mean as the underlying time series.
- Fetch exact detail whenever the user's question or a proposed edit depends on
  fine timing, frequency, transients, phase/relations, MIDI, or automation. Do not
  fetch every dense family by default when the task does not depend on it.
- To claim direct listening, confirm the MCP host actually delivered the audio
  resource to an audio-capable model path. An audio URI or measurement report alone
  is not listening evidence.

The library talks to REAPER through its resident ReaScript bridge and native,
documented mechanisms. It does not inspect or patch REAPER process memory.

## Model operating principles — not a mixing pipeline

These are durable decision-making rules, not a required sequence of EQ,
compression, panning, editing, or mastering. Select relevant capabilities for
the task while keeping the project and audio evidence connected:

- Treat the **panorama** as the unified project picture: REAPER facts, audio,
  measurements, track/bus/master relationships, FX, routing, automation, time
  interval, and revision. The intended model-facing contract has one
  state-read representation; raw archive JSON must not become a parallel source
  of project truth.
- Bind material conclusions to object IDs, time window, capture point, method,
  and revision. Distinguish REAPER state, measured signal properties, model
  hypotheses, and the user's aesthetic judgment.
- Choose tools from the goal and evidence. Full capability does not mean running
  every analyzer or adding FX by default. Do not turn an example into a
  genre-specific recipe or fixed production pipeline.
- Consider tracks, buses, and master together while keeping signal stages
  distinct. A track stem is not the full master, and a pre-FX signal is not a
  post-FX output. An audio URI proves that a resource exists, not that the
  client actually delivered it for the model to hear.
- Preserve alternatives and uncertainty. Do not infer role, cause, audibility,
  or quality from a track name, one metric, or an unvalidated heuristic
  threshold. Make missing or partial coverage explicit.
- After an edit, inspect actual state and relevant audio at the bound revision
  and interval. Batch compatible operations, but do not let batching replace a
  necessary check. Use a safe rollback when the observed result differs from
  intent and rollback is supported.
- Record neutral before/after values and deltas. A change is not an automatic
  `better`/`worse` verdict; keep user listening feedback separate from
  measurements.

### Failure patterns from prior runs

- Operating REAPER through standalone scripts and presenting that as
  model-facing MCP use. A script can independently verify an artifact, but it
  cannot prove that the model received the same context or performed the action
  through MCP.
- Treating a registered tool list as proof of successful calls in the active
  model session, or an audio URI as proof that the model heard its contents.
- Treating a revision counter change as a semantic project change: rendering may
  temporarily change mute states, restore them, and still leave the counter
  advanced.
- Comparing mismatched signal stages, windows, channels, methods, or projects
  and treating the resulting number as a valid delta.
- Inferring causality from simultaneous changes without an isolating comparison,
  inferring a processing need from one metric, or treating a track stem as the
  master.
- Turning a user example into a special-purpose pipeline, quantizing by default,
  or adding processing without support from the goal and evidence.
- Hiding missing/truncated data, stale revisions, or uncertain track identity;
  emitting a universal mix-quality verdict.
- Treating a spilled, truncated or hand-assembled tool result as the project
  view. The view is whole only when every page in the manifest was received and
  `reaper_panorama_ack` returned `complete`; a page that was spilled was not
  read, so the map is incomplete even though the project is fine.
- Reconstructing a view from several `reaper_panorama_query` calls and then
  claiming a whole-project result: the panorama pages are the only whole-project
  delivery, and exact queries are detail behind them.
- Applying a delta to a different capture, revision or fact root, or treating a
  missing delta as “no change”, silence or an empty domain.
- Mutating again after an edit without using the successor token returned with
  the delta. The old token is stale for a write at the newer revision; use
  `advance.context_token`, `changes_since` from the older base, or refresh
  before the next write.
- Using a coarser temporal level in place of a finer one you were given, omitting
  units or validity metadata, or calling a declared aggregation “the raw curve”.
  The exact cells are one query away; never present a bucket mean as the series.
- Treating `reaper_panorama_ack` as proof that every raw time series was inlined,
  every page was understood, or audio was heard. The receipt covers delivery of
  the bounded panorama only.
- Calling relation edges, series cells or MIDI notes “missing” because the
  panorama only catalogues them: the catalog states their count, units and byte
  size, and the query returns them whole.

## Tool map

| Tool | Use |
|---|---|
| `reaper_session` | Check bridge health, discover REAPER instances, list open projects, or inspect unsaved-change state before a controlled restart. |
| `reaper_bridge_reload` | Activate a newer resident bridge build in place, with project/session identity checks; refuses legacy bridges rather than trying action bootstrap. |
| `reaper_capabilities` | Read the domain index or page through the 732 documented API functions. |
| `reaper_api_search` | Find API functions or Actions using a structured object/operation card. |
| `reaper_project` | Read one project's master, tracks, routing, tempo, items, FX, or MIDI context. |
| `reaper_analyze` | Capture and analyze every project track and rendered master in one request. |
| `reaper_mix_evidence` | Summarize measured mix evidence for an existing analysis; it does not choose or apply processing. |
| `reaper_edit_map` | Build cut and seam evidence from an existing analysis; it does not edit the project. |
| `reaper_edit` | Preview, apply, or undo a revision-bound batch of supported edits. |
| `reaper_render_candidate` | Render one applied plan's processed master output through the native REAPER path, for one exact interval, with verified settings restoration, explicit post-render revision, stage/FX/routing provenance, SHA-256 and a leased audio resource. It fails closed on a plan/revision mismatch, stale context or incomplete provenance; a monotonic counter advance is reported, not refused or hidden. |
| `reaper_api_call` | Call a function in the bundled official ReaScript signature catalog. |
| `reaper_action` | Search Actions or invoke one in the main or MIDI Editor section. |
| `reaper_save` | Save the current project or save as a new `.RPP` path. |
| `reaper_checkpoint_save` | Save the active project as a new, non-overwriting checkpoint inside the configured checkpoint root and verify project/media state. |
| `reaper_release` | Release one analysis or candidate report and its temporary WAV files now. |
| `reaper_capture_snapshot` | Create a complete linked capture and archive its self-describing panorama; return capture/archive metadata only, not the view. |
| `reaper_capture_read` | Read one archived capture only with a complete-view receipt for that exact capture; otherwise fail closed with a next action. |
| `reaper_panorama_open` | Open the bounded whole-project panorama: page 1 plus a manifest of every stable `page_id`, its payload hash and measured page size. Fetch every page, then acknowledge; exact dense detail remains queryable. |
| `reaper_panorama_page` | Fetch one page by `page_id`; idempotent, revision-bound, and unknown ids list the valid ones. |
| `reaper_panorama_ack` | Verify every delivered page hash and reconstruct the view; mints the `receipt` and `context_token`, or returns `incomplete` with missing page ids. |
| `reaper_panorama_query` | Return exact rows for a semantic selection (objects, interval, family, series, metrics) in one result after the acknowledged receipt; `query_too_large` instead of truncation. |
| `reaper_panorama_status` | Report the latest captured revision/root and one handle's delivery state. |
| `reaper_panorama_changes_since` | Return a delta from an exact base capture/fact root plus its successor token, or `stale_base`/`refresh_required`. |
| `reaper_panorama_refresh` | Re-deliver the current view after loss, staleness or context compaction. |

The official API catalog and runtime support are different facts. A documented
function may not exist in the installed REAPER version. The bridge checks its
availability at call time and returns an explicit error.

For bridge maintenance, `reaper_bridge_reload` is an in-place activation for a
bridge that already implements the lifecycle contract. It proves one new
session, the expected build, and the unchanged project identity. A legacy bridge
returns `bootstrap_required`; do not try to invoke Actions or start a duplicate
loop through MCP. Follow [the bridge lifecycle procedure](bridge-lifecycle.md)
for the operator-managed cold start or legacy upgrade.

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
`project_id`, `change_count`, `path`, and — from a bridge that proves them —
`instance_id` and `epoch`. Treat the revision as the exact address of a project
state: `project_id` names the project, `instance_id` names the running REAPER
bridge, `epoch` names the bridge's generation for that project, and
`change_count` is the edit counter. A matching path and counter do **not** prove
continuity: after a REAPER restart or a project reload the same file can present
the same path with a fresh counter. If `instance_id` or `epoch` is missing, the
server marks the identity unknown and refuses to reuse context across it; refresh
from a bridge that reports both. Address objects with stable GUIDs; names and
list positions are context, not safe identifiers. Request `include_items`,
`include_fx_params`, or `include_midi_notes` only when needed. Use GUID filters
to keep large responses focused.

Every edit plan, save, Action invocation, and project-bound API call carries
`project_id` and the full `expected_revision` from `reaper_project`. If the
revision is stale, reread the project, find out what changed, and prepare a new
operation. Do not replay stale write arguments.

`reaper://projects/{bridge_session_id}/{project_id}/context/{change_count}`
re-reads a project only while that instance and revision remain available. It
is not a frozen historical snapshot.

## Analyze and listen

`reaper_analyze` has two project-audio modes. For a live project, call it after
the complete-view receipt for that project and revision; without `capture_id` it
renders and analyzes every project track and the master. If an exact durable
capture already exists, use its `capture_id` and `archive_root` from
`reaper_capture_snapshot`, together with the receipt acknowledged for that same
capture. This archive mode reads the verified capture WAVs and makes no REAPER
bridge call or project render. For the exact captured window with no rendered
tail, stored per-source measurements are reused. A selected subwindow, a
rendered tail, or stored measurements that do not exactly cover the requested
window cause the WAV to be remeasured from the archive; they still do not cause
a REAPER render. The result is bound to that capture's revision and is not
evidence of any later project edits. Capture mode rejects requests to change
sample rate, channel layout, or add a render tail rather than silently creating
a new render.

Both modes can analyze a project-time section (`start_s`, `end_s`), compare full
reference files through `reference_paths`, select reference-file-local excerpts
with `reference_windows`, request selected phase comparisons, or request all
pairwise comparisons. Pairwise work grows with source count; use it when it
supports the current question. Prefer archive mode to compare references with
the exact audio and measurements already delivered in a capture; make a fresh
capture only when the project state or requested signal is genuinely different.

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

Full-capture master gate: `reaper_capture_snapshot` checks the rendered master
peak/RMS and classifies silence from the rendered source stems and captured
REAPER state. An intentionally silent section, all-muted sources, or a muted/
zero-volume master is accepted as `capture_status="valid_expected_silence"` with
`master_validation.status="expected_silence"`. Unexpected silence (audible
sources, no mute/zero explanation) allows one bounded retry and records every
attempt in `timings.master_attempts`. Failed attempts are also copied into the
durable archive. Persistent unexpected silence returns `capture_master_invalid`
with the archived capture id/record path and attempt evidence; it never returns
`ok=true` with an unexplained silent master. A retry success is `ok=true` with
`capture_status="valid_after_retry"`. A valid `reaper_capture_read` performs the full
record and audio/evidence SHA-256 verification.
`project.revision` is the current REAPER revision after capture; use it for the
next edit. The render path temporarily sets render options and track selection,
then restores them; REAPER's monotonic change counter can advance even when the
library verifies that project context and render settings were restored. For sequential
calls, compare the previous `project.revision` with the next
`project.capture_revision` and inspect the returned context before assuming
there was no intervening edit. Before/after reports are expected to have
different revisions after edits. Correlation between one track stem and the
full master is not a validity test: they are different signals, and routing,
buses, and master processing affect their relationship. The result names
`evidence_revision` (the revision the measured WAVs are bound to) separately from
`revision_after_capture` (the live counter after the capture's own render-settings
churn). Use `evidence_revision` when matching a panorama receipt for edit maps or
mix evidence; do not treat `revision_after_capture` as proof that the audio was
measured after a later edit. Temporal analysis cells
include the effective REAPER project tempo when available. Read `coverage`
instead of assuming optional analyses succeeded.

### Project view

Every capture archives a canonical `panorama` derived from the record and stores
it atomically as `capture-archive/captures/<capture_id>/panorama.json`.
`reaper_capture_snapshot` creates that document but returns capture/archive
metadata only. To see the project, call `reaper_panorama_open`: it returns page 1
of the bounded model-facing project view of that exact document together with a
manifest of every page (`page_id`, `page_hash`, measured response size). This
view is a derived projection, not every raw detail from the canonical record.
Fetch the remaining
pages with `reaper_panorama_page`, then call `reaper_panorama_ack` with the page
hashes in order. Only the acknowledged receipt yields the `context_token`; a
partial page set returns `incomplete` with the missing page ids.

The view is columnar and self-describing:

* `binding` — capture, project, record path/integrity, revision before/after,
  identity, evidence freshness, render manifest, source document hash;
* `project`, `tracks` (one row per track plus master), `items`, `takes`, `midi`,
  `fx`, `routes`, `automation`, `envelopes` — each table states its columns once,
  rows are arrays in that order, `*_i` columns are row indexes into that table;
* `time` — the revision-bound tempo map, time signatures, markers, regions;
  `transport` — selection and transport state;
* `audio` — one row per captured asset (stage, method, frames, sample rate,
  channels, duration, mapped end, render tail, SHA-256, analysis id) with shared
  `stages` and the exact `capture_revisions`;
* `analysis` — one row per source with **every** declared metric family as a
  column (24 scalar metrics, units in `analysis.metric_units`), plus declared
  RMS statistics and the series descriptors;
* `temporal` — the declared time levels of the activity grid (300 s and 30 s by
  default, plus the stored grid geometry); a level finer than the stored grid is
  reported as unavailable, never invented;
* `relations` — every relation kind with its edge count, byte size and units,
  pair outcomes, coverage, unavailable reasons and content hash; the edges
  themselves are archive family `relations`;
* `coverage` — expected/observed counts and explicit partial/missing/truncated
  status; `archive` — the measured catalog of exact families and the detail
  manifest; `reading` — the contract in one place, including which epistemic
  layers are empty.

Shared meaning is stated once, not per row: metric units, column units, the
file/sample/project mapping formulas, the render-stage table and the
crosslink rule ("the FK columns *are* the crosslinks"). A metric that was not
measured is `null` in a declared column, never zero and never a silently absent
column. `reading.epistemic` keeps project facts, signal measurements,
interpretations and hypotheses separate; `model_interpretations` and
`causal_hypotheses` are empty and must not be filled with invented roles,
genres, quality verdicts or causes.

Exact high-frequency rows are catalogued with their measured byte size and
fetched with one generic query, not with pages:

```json
{"tool": "reaper_panorama_query",
 "arguments": {"handle": "pan_...", "revision": {...}, "archive_root": "...",
               "panorama_receipt": "pr_...",
               "data": ["relations"], "objects": ["track:<guid>"], "interval": {"start_s": 41, "end_s": 73}}}
```

`data` accepts `relations`, `series`, `midi_notes`, `envelope_points`, `items`,
`takes`, `audio`, `analysis`, `fx`, `project_summary` (default all). `objects`
accepts `"all"`, stable ids, raw REAPER GUIDs or unique track names. `series`
selects `rms_400ms`, `band_level`, `temporal_profile`; `resolution_s` coarsens a
series inside the interval; `metrics` projects analysis columns; `kinds` and
`bands_hz` filter relations. One call returns the whole selected fragment.
Series, MIDI and envelope rows are re-hashed against the sealed detail manifest,
so a stale reference fails closed.

Authorization passes `revision`, `archive_root`, `handle` and the
`panorama_receipt` from the acknowledged panorama; a query without that receipt
fails closed:

```json
{"tool": "reaper_panorama_query", "arguments": {
  "revision": {"...": "..."}, "archive_root": "...",
  "handle": "pan_...", "panorama_receipt": "pr_...", "data": ["analysis"],
  "objects": ["analysis:<guid>"], "metrics": ["crest_db"]}}
```

The receipt authorizes exactly this panorama's exact rows. It is not a second
delivery of the map: the panorama pages are the map, and a query only fetches
detail behind them. If the selected fragment is over budget the call returns
`query_too_large` with its measured size per family and the scope to narrow. It
never truncates, never returns a numbered chunk and never invents a cursor.
(The legacy `read_scope` argument is still accepted for older flows but is not
part of the paged contract.)

Time is explicit: `project_extent` (as reported by REAPER) is separate from
derived `content_extent`; `capture_window` is in project seconds; every audio row
keeps exact frame count, sample rate, channel count and the mapped-end/tail
geometry, and `audio_time_contract` holds the file↔project↔sample formulas once. All
intervals are `[start_s,end_s)` and time field names carry `_s`. Item geometry
(`position_s`, `length_s`, `start_offset_s`, `playrate`, fades) is inline because
it is decision-critical.

The canonical sidecar is still the archival document; the view never rewrites it
and older sidecars are projected in memory on read. A sidecar whose capture id,
integrity, revision, time window or document hash does not match the record is
rejected as `panorama_binding_invalid`.

A valid `reaper_capture_read` also reports `capture_status`, `invalid_reasons`,
`master_validation`, `coverage` and `missing`, so an archived chronological
capture with an unexplained silent master is not mistaken for a complete,
comparable measurement. A valid expected-silence capture is still distinguishable
from an invalid render; read the status before using its audio as evidence.

The canonical sidecar keeps more than the view: per-kind relation contracts
(`relations.kinds` with interpretation, method, units, caveats and coverage
rule), one window/stage/node/channel binding for the whole relation set,
`relations.sources` mapping each endpoint to its asset SHA-256, every edge and
evaluated pair with its own endpoints and coverage, `details.entries` with the
schema, content SHA-256 and record integrity per exact family, and the full
`analysis_temporal` grid. The view quotes those contracts and catalogues those
rows; no reader has to page through the sidecar to find a measured value.
`coverage.domains` has expected/observed counts and explicit
`partial`/`missing`/`truncated` status for tracks, items, takes, MIDI, FX,
routes, automation, audio and analysis. Active-take-only capture and the MIDI
limit of 10,000 events are never reported as complete. A document written under
an older schema is upgraded in memory from the canonical record and the archived
sidecar is left untouched.

### Project context flow and the completion gate

The normal sequence has three steps:

1. `reaper_project(project_id)` establishes the live project and its revision.
   `reaper_capture_snapshot(project_id, expected_revision, archive_root)` creates
   the archived evidence document and returns capture/archive metadata only; it
   never reprints the view, so a capture cannot become a second reading path.
2. `reaper_panorama_open(project_id, archive_root)` returns page 1 of the whole
   bounded panorama plus the manifest: `page_count`, every `page_id`, `page_hash`
   and the server-measured size of each page. The page count is adaptive — a
   larger project yields more pages. Pagination preserves the panorama exactly;
   it does not restore detail already represented as an aggregate in the view.
   The configured budget counts characters in the server's indented JSON
   rendering (`page_target_bytes` defaults to 30,000 and `max_page_bytes` to
   46,000). These are not UTF-8 wire-byte counts or the complete MCP transport
   envelope, so the actual response limit of the connected host must be checked
   separately.
3. Fetch every page with `reaper_panorama_page(handle, revision, archive_root,
   page_id)`, then call `reaper_panorama_ack(handle, revision, archive_root,
   page_hashes)` with the hashes in manifest order. The server checks that every
   page was actually served, that the hashes match, and that the pages
   reconstruct the exact `view_sha256`. A partial set returns `incomplete` with
   the missing page ids and mints no receipt; a stale revision must be re-opened.
4. Work against the receipt's `context_token`: `reaper_edit` apply/undo,
   `reaper_api_call`, `reaper_action` invoke and `reaper_save` accept it in the
   `panorama_receipt` field together with the exact `expected_revision`. They
   reject a missing, expired, wrong-project, wrong-instance, wrong-epoch or
   wrong-revision token. Preview does not mutate. Structural bootstrap discovery
   and the initial capture are the only pre-receipt exceptions; `reaper_capture_read`,
   `reaper_panorama_query` and every project-target analysis require a receipt
   for that exact capture. A query without the acknowledged receipt fails
   closed; there is no separate pre-receipt route in the paged contract.

The view is columnar and self-describing: one `entities` dictionary holds every
object GUID and name once (a table row `i` uses entity `i` of its kind, and the
stable id stays `'<kind>:<guid>'`); every other table references those rows by
index. It carries all tracks and the master, items/takes with geometry, FX and
their parameters, routing/sends, automation and envelope summaries with point
counts, the revision-bound tempo map, signatures, markers and regions, audio
identity per asset (stage, sample rate, channels, duration, a 16-hex prefix of the
blob SHA-256 with the full hash in the archive), the
analysis matrix, the declared time levels, relation kinds with counts, units and
coverage, domain coverage and the archive catalog.

All 24 measured metric families are declared columns in `analysis.rows`, with
their units in `analysis.metric_units` and the number of absent values per family
in `analysis.absent`; a null is "not measured for that source", never zero.
Inline metric values use the per-metric `analysis.metric_decimals` (default 3;
selected metrics use 4, 6 or 8), never one global precision, and
`analysis.rounded_to_zero` reports any non-zero value rounded to zero. Inline
level statistics use 2 decimals. `analysis.precision.lossless` is false: exact
unrounded values, exact series cells, every relation edge and pair, MIDI notes,
envelope points, take media paths and audio frame mapping stay in the sealed
archive. One semantic query returns the whole selected scope **or**
`query_too_large`; do not assume every exact composition fits one result:

```json
{"tool": "reaper_panorama_query",
 "arguments": {"handle": "pan_...", "revision": {...}, "archive_root": "...",
               "panorama_receipt": "pr_...",
               "data": ["series"], "series": ["rms_400ms"],
               "objects": ["track:<guid>"], "interval": {"start_s": 41, "end_s": 73}}}
```

`data` accepts `relations`, `series`, `midi_notes`, `envelope_points`, `items`,
`takes`, `audio`, `analysis`, `fx`, `project_summary` (default all). `objects` accepts
`"all"`, stable ids, raw REAPER GUIDs or unique track names; selecting a track
also addresses its measurement series and its automation. `series` selects
`rms_400ms`, `band_level`, `temporal_profile`; `resolution_s` coarsens a series
inside the interval; `kinds` and `bands_hz` filter relations. Series, MIDI and
envelope rows are re-hashed against the sealed manifest, so a stale reference
fails closed. A selection over the budget returns `query_too_large` with its
measured size per family and the scope to narrow — never a truncated or paged
answer.

Time is explicit: `project_extent` (as reported by REAPER) is separate from
derived `content_extent`; `capture_window` is in project seconds; every interval
is `[start_s,end_s)` and time field names carry `_s`. `audio_contract` holds the
file↔project↔sample formulas once.

The receipt guarantees delivery, not comprehension. It binds the exact view the
pages reconstruct (`view_sha256`), the document hash, the revision and the fact
root. The server cannot observe whether your host kept the bytes or whether you
read them, so state the scope you actually worked from.

If the page set was interrupted, resume instead of re-opening:
`reaper_panorama_status(project_id, archive_root, handle)` reports
`delivery.pages_received` and `delivery.pages_missing`; fetch only the missing
`page_id`s (page fetches are idempotent) and acknowledge again. The receipt is
invalidated by a new capture or revision, by a refreshed handle, by the handle
TTL, or by a different project/instance/epoch — in any of those cases open a new
panorama; `reaper_panorama_refresh` is the normal way to request the full set
again after loss or compaction.

After a change:

5. Every successful mutation returns a **factual delta** bound to the exact base:
   `reaper_edit` apply and undo, `reaper_api_call` when the revision changed, and
   `reaper_action` invoke when the revision changed. The delta names the
   post-operation revision, per-operation readback facts, added/changed/deleted
   object ids, field-level before/after values, the render-key verdict per
   captured source, and every invalidated audio, analysis and relation id.
   * The delta carries its successor token in the same response
     (`advance.context_token`, with `advance.guarantee`); there is no separate
     acknowledgement step.
   * A delta whose `audio_evidence.state` is `current` advances structural edits
     and later audio interpretation. With current manifests this requires both
     compared manifests to declare no unresolved dependency classes; the standard
     manifest always declares some, so a mutation delta is normally `stale`.
   * A delta whose state is `stale` advances **structural edits only**. Its
     `audio_evidence` lists the invalidated sources, assets, analyses and
     relations. Do not describe old audio as current, and do not run a
     project-target analysis against that token: it fails with
     `stale_context/audio_evidence_fresh` until a fresh capture is opened. That is
     the intended next action, not an error to work around.
   * `scope="unknown"` (raw API calls and Actions) never names a precise subset of
     untouched tracks: it invalidates all project audio conservatively.
6. `reaper_panorama_changes_since` is the capture-to-capture path: pass an exact
   `base_capture_id` (or a revision carrying `capture_id`) plus `base_fact_root`
   and the current `context_token`. Base and target must be in the same archive;
   another archive returns `stale_base/base_not_in_archive`, an identity break
   returns `stale_base/identity_discontinuity`, and an over-long or oversized
   chain returns `refresh_required`. The successor token comes back in the same
   response.
7. `reaper_panorama_refresh` re-opens the current panorama from page 1 after loss, compaction,
   a new chat, a schema mismatch or any unprovable continuity. The old handle or
   token must not be used to mutate.

A whole-project claim requires: every manifest page delivered and acknowledged
for the revision being discussed (`reaper_panorama_ack` → `complete`), no
invalidation of that revision on the audio
being cited, and a stated scope for scoped analyses. A spilled result, an
over-budget answer, a stale token, or a capture whose `audio_evidence` is `stale`
qualifies or blocks the claim. The time levels state their width, aggregation and
anchor, and every exact cell remains available through the query; never present a
level mean as the raw curve and never call a bounded projection the full document.

The guide itself is readable with ordinary tools by reading this file (or
`docs/MCP-README-LLM.ru.md`). Resources and prompts are conveniences, not a
required client context mechanism.


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
`baseline`, and `final`, plus one candidate lease per project. Replacing a role
or making a new candidate releases its previous files.
`reaper_release(analysis_id=...)` releases one report and its audio immediately.
`reaper_render_candidate` uses the same release path with its `candidate_id`;
the next candidate for a project replaces the previous candidate lease.
Reading a resource does not extend its lease. These are short-lived analysis
artifacts, not project versions or a permanent stem archive.

## Edit without losing context

For supported library transactions, use `reaper_edit`:

1. Call `mode="preview"` with `project_id`, the full `expected_revision`, a
   short `reason`, and operations containing `kind`, `target_guid`, and `values`.
   Preview does not modify the project.
2. Inspect the plan and before-state. If it addresses the intended objects and
   values, call `mode="apply"` with the returned `plan_id` and same revision.
   Apply returns the new revision **and a factual delta**. Inspect the delta's
   `objects` (added/changed/deleted ids and field values) and
   `audio_evidence.invalidated_sources`; do not assume untouched audio is
   current without that list.
3. Use the successor token delivered with the delta (`advance.context_token`).
   It is bound to the new revision, so the next edit can be previewed and applied
   without capturing the project again and without a separate acknowledgement
   call. If the delta is not advanceable (partial readback), capture again
   instead of retrying.
4. Read touched objects back with `reaper_project`. Use the new revision for
   later changes or undo.
5. Call `mode="undo"` only with the same plan ID and exact revision returned by
   its apply, after inspecting the apply delta. Undo is a new forward
   revision with its own delta; it never erases the previous record. Undo is
   available only while that transaction remains the newest matching undo
   record.

A captured source's `render_key` is a hash of the signal path the capture used,
not of the output file. It covers track state (including pan law), normalized
item state chunks, FX identity/order/bypass/parameters and serialized chain
state, automation, routing and folder ancestry, tempo and render settings, plus
the master chain because stems render via master. A full capture records this
manifest; a mutation delta compares it and names exactly which captured nodes
survived. Dependencies the implementation cannot prove are declared in the
manifest's `unverified` list instead of being assumed equal: the bytes of media
files on disk, external realtime inputs, the project default pan law (per-track
`D_PANLAW` is read; REAPER exposes no documented numeric key for the project
default), the loaded plugin binary version, and track routing flags the numeric
digest does not expose (FX pin mappings, per-track timebase, fixed-lane
playback). If either manifest is incomplete, the delta says scope `unknown` and
invalidates all project audio rather than guessing. Even a manifest whose known
components are complete still declares those unverified classes. They are not
mapped to individual sources, so a cross-capture comparison is non-authoritative
whenever they are present: no source is called unchanged, and every captured
source at the old revision becomes stale. The fresh capture itself remains
current evidence for its own revision; the uncertainty only prevents it from
being reused after a later revision. A new capture restores interpretation.

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

## Verify an applied edit with a candidate render

`reaper_render_candidate` is the plan-bound listening/verification primitive.
It answers one question: what does this already-applied edit sound like at this
exact revision and interval, through the real master path?

Use it on the project that owns the applied plan. The native REAPER render
temporarily changes render settings and the time selection, then restores them;
the response includes the restoration report and a before/after project-digest
comparison. The monotonic change counter may advance: the tool does not refuse,
hide or roll back that advance. It returns `revision_after_render` and a
`context_advance` block whose token is the next valid lifecycle token. It never
reloads, saves or resets the project. Live verification is performed only on an
operator-verified disposable fixture; that is a test policy, not a tool-side
project gate.

Arguments: the `plan_id` returned by `reaper_edit`, the exact `revision_after`
returned by that plan's apply, `project_id`, and an explicit `start_s`/`end_s`.
The render is a master mix (`RENDER_SETTINGS=0`), so track, bus and master FX
and routing are included; it is not a pre-FX stem and AudioAccessor is not
used. The response carries:

- `audio.sha256`, bytes, sample rate, channels, frames and exact file/project
  windows (a rendered tail is in the file window but not the measured window);
- `audio.stage = master_output_post_track_bus_master_fx`, `node = master`,
  `channel_view`, and the native render settings echo;
- `provenance` with the master FX chain, FX counts/bypass/offline state, routing
  summary, routing/FX/project-digest hashes and the render action;
- `revision_after` (the applied content revision), `revision_after_render` (the
  current revision after settings restoration; may differ only by the
  monotonic counter), and `context_advance` (the post-render token or an
  explicit unavailable reason);
- `project_state`, which reports restoration, content equality and counter
  churn instead of hiding them;
- `audio.resource_uri` for the leased WAV, and a `lease` with the cleanup rule.

Refusals are typed: `stale_context`, `render_candidate_mismatch`,
`render_budget_exceeded`, `render_provenance_incomplete`, `render_unsupported`,
or `artifact_cleanup_failed`. A failed restoration or content-digest difference
fails closed. No audio is returned with a failed provenance block.
Release with `reaper_release(analysis_id=<candidate_id>)`; the next candidate
for the project replaces the previous candidate lease, and the TTL is one hour.
The WAV lives outside the `.RPP`; no stem collection or version history is
created.

A candidate render is evidence about the master signal at that revision, not
proof that the edit is musically right. It also does not remove the need for a
fresh full capture before project-target analysis.

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

`reaper_checkpoint_save` is the narrow recovery save: it requires the exact live
project identity and revision, refuses an existing target, confines the path to
`REAPER_CHECKPOINT_ROOT` (default `%USERPROFILE%\Documents\REAPER Workbench\versions`),
and checks the rebound project id, dirty state and online audio media. Configure
the root during installation before using this tool. A verified checkpoint
proves persistence and media resolution, not audio quality.

A save that moves the revision or path returns `context_invalidated: true` and
no delta. Do not reuse the old context token after it: capture again (or
refresh the structural context) before editing. A save is not evidence that
project audio stayed the same.

## Errors and truthful reporting

Tool errors use a stable structure:

```json
{
  "ok": false,
  "error": {
    "code": "stale_context",
    "message": "...",
    "details": {},
    "next_action": "...",
    "recovery": {
      "contract": "reaper_mcp_recovery/1",
      "policy": "bounded_context_recovery",
      "retry_limit": 1,
      "stop_after_repeat": true
    }
  }
}
```

`recovery` is present for context/receipt gates and candidate-render mismatches.
Follow its scope and retry limit; it does not override `details`, mint a token,
or make stale audio current. A candidate mismatch sets `retry_limit` to `0`.

For `stale_context`, refresh and replan. If the error details say
`audio_state="stale"` or the gate is `audio_evidence_fresh`, the structural
context may still be correct but project audio is not: create a new capture and
acknowledge its panorama before interpreting audio. For `bridge_timeout`,
inspect state before retrying. For `api_unavailable`, check the REAPER version
and discover an alternative. For `validation_error`, correct the field in
`details`. A tool result does not prove that the user's musical goal is
complete: report what was read back, measured, and actually heard, and identify
what remains unverified.

### Bounded recovery and operational anti-patterns

Keep one current context tuple together: `project_id`, bridge/instance identity,
`epoch`, `change_count`, `capture_id`/fact root, and its `panorama_receipt`/
`context_token`. Never combine a token, revision, or receipt from another project,
REAPER instance, epoch, capture, or saved-as identity. After a successful edit,
use the returned successor token for structural reads and edits; its delta may say
that audio evidence is stale, which means the old measurements cannot justify a
new audio conclusion or a project-target analysis.

For `stale_context` or `audio_evidence_fresh`:

1. Read the error's `details` and `next_action`; do not replay the rejected
   arguments with an old token.
2. Check `reaper_panorama_status` and the exact live project revision. If a
   complete capture already matches it, finish or reuse that delivery instead of
   rendering another copy.
3. Otherwise make one fresh capture for that exact project/revision, open it,
   fetch every announced page, acknowledge all hashes, and retry the refused
   operation once. If it is refused again, stop and report the exact error; do
   not repeat the capture/analyze cycle or invent a token.

`reaper_analyze` is itself a native audio-render operation. Its
`evidence_revision` binds the measured WAVs; `revision_after_capture` can differ
because REAPER restores temporary render settings and advances its monotonic
counter. Never call the latter the audio's revision. Do not start a duplicate
synchronous analysis while one is running, and do not rerun analysis merely to
chase counter churn. Use an existing capture's exact measurements when they
answer the question; request another render only for missing evidence or a
different interval/reference comparison.

`render_candidate_mismatch` means candidate verification failed, even if the
reported difference is an FX state chunk. Do not rerun blindly, suppress the
mismatch, or assume that unchanged FX names/slots prove unchanged parameters.
Read the affected chain and parameter values through MCP. If the tool cannot
provide semantic before/after evidence, preserve the failure and label the audio
verification unresolved. A successful save/checkpoint proves that a file was
saved, not that the mix was measured after the final edit or sounds right. Report
these states separately: **saved**, **state read back**, **audio measured after
the last edit**, and **auditioned by a person**.

Use MCP for all project inspection, edits, and audio analysis. Do not bypass a
missing or failing MCP capability with a direct bridge call, script, GUI action,
or hand-edited `.RPP`; report the coverage gap instead. These are reliability
rules, not a prescribed order of musical decisions or a mixing recipe.

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
