# REAPER Workbench — handbook (read once per session)

What the library can do, how to call it, and the mistakes that look reasonable but break the work. `reaper_measure`, `reaper_edit`, `reaper_midi` and `reaper_analyze` refuse with `HANDBOOK_REQUIRED` until `reaper://handbook` is read; the gate re-arms when this file changes.

## 1. Mental model

The library is a **measuring and editing instrument**, not an advisor: it reports facts (levels, spectra, positions, routing, parameters, receipts) and never judges a sound. Thresholds exist only if the owner put them in `policy.json`; they appear as `violations[]` with `source: "policy"`. Every number you quote must come from a measurement, an evidence token or a receipt.

| Object | Meaning |
|---|---|
| `revision` | `project_id` + `change_count` + `path`. Any edit changes it. |
| evidence `token` | Units behind every cell of one `reaper_measure` answer. |
| `receipt` | Proof one batch of edits was applied, with read-back values. |
| `structural_token` | Cheap proof you saw the project map; authorises edits. |

## 2. First calls

```text
reaper_session(action="health")                    # bridge session + active project
reaper_session(action="context", archive_root=...) # ONE project context: map, health, facts, fx, changes
reaper_session(action="status", detail="summary")  # revision, tracks, routing, tempo
reaper_capabilities(mode="tools")                  # the eleven task groups
reaper_project(project_id=...)                     # context beyond status
reaper_structural_token(project_id=...)            # before any mutation
```

- `status(detail="summary|full", sections=[...])`: sections `runtime`…`evidence` (13 names); `gaps[]` names what the digest could not supply.
- `context(archive_root, detail="summary|full", max_facts=50, since=token, pages=false)`: the single agent-facing project context built from `state.json` — project/revision, compact map, health, facts summary, fx summary, changes, audio references and detail pointers. Prefer it over separate status reads. `since=state_token` returns only project/health/changes/next plus the live revision (a few hundred tokens), never the map again; `pages=true` returns pages and no full context. Summary fact rows list sources/windows/metric sets without values; values come from `state.query`.
- `state(archive_root, source=..., stage=..., window=..., metrics=..., status=..., since=token)`: read facts already measured, without rendering. `state_detail="full"` returns complete rows; the default is compact and includes values only for requested metrics.
- `preflight(allow_solo=[...], check_focus=false)` returns `blockers`, `warnings`, `info` with codes; no verdict. `runtime.stale` → `reaper_session(action="app", app_command="restart")` before editing.
- Every answer repeats `mode` (`strict` / `advisory` / `off`).

## 3. Task groups

Eleven groups: `session`, `project`, `capture`, `panorama`, `media`, `analysis`, `measure`, `inspect`, `edit`, `discovery`, `research`. `reaper_capabilities(mode="tools")` lists each group's tools and required context.

**session** — `reaper_session` (`action`: `health`, `status`, `context`, `state`, `preflight`, `transport`, `project`, `settings`, `app`, `wait_for_change`, `list_instances`, `list_projects`, `dirty`), `reaper_checkpoint_save`, `reaper_bridge_reload`, `reaper_bridge`, `reaper_mode`, `reaper_structural_token`.

- `context`: the one project view. `since=state_token` returns only project/health/changes/next plus the live revision; `pages=true` returns pages and no full context; `detail` and `max_facts` bound the rest.
- `state`: facts already measured, without a render; filters by source/stage/window/metrics/status, compact rows by default.
- `transport`: `transport_action="play|stop|pause"`, `position_s`; `app`: `app_command="ensure|restart|focus"`; `wait_for_change`: `since={"revision"|"transport": ...}`, `timeout_s`.
- `project`: `project_command="open|save|save_as"`, `path`, `overwrite_existing`; `settings`: `settings_command="read|write"`.
- `reaper_checkpoint_save` writes a new file only; `reaper_save(mode="save_current|save_as", ...)`; `reaper_mode` is the user's decision; `reaper_structural_token(project_id)` is one digest read, no render.

**project** — `reaper_project` reads the full project document; `reaper_timeline_context` returns tempo/markers/regions; `reaper_save` writes the current project or a new file.

**capture** — `reaper_capture_snapshot` materializes one immutable, revision-bound audio capture; `reaper_capture_read` returns its record. Captures back archived analysis and exact rows; for orientation prefer `session(context)`.

**panorama** — `reaper_panorama_open/page/ack` deliver the whole-project view of one capture, `reaper_panorama_query` returns exact rows by meaning, `reaper_panorama_changes_since` and `reaper_panorama_refresh` handle updates. A panorama receipt proves delivery of those pages, not understanding or listening.

**measure** — `reaper_measure`, `reaper_freshness`, `reaper_coverage`:

```text
reaper_measure(archive_root=..., project_id=..., sources=[guid, ...] | "all" | "master" |
    [{"kind":"file","path":"ref.wav"}], intervals="all" | "regions" | "bars:8" | [[12,20],[20,28]],
    metrics=["integrated_lufs","peak","crest","bands"], bands="octave" | "third" | [[lo,hi],...],
    stage="via_master", channels="stereo", sample_rate=None, tail_ms=0,
    analysis_workers=None, step_s=None, auto_transport=false)
```

- `sources` are **track GUIDs**, `"all"`, `"master"` (with `stage="master"`), or files; names are never resolved. `start_s`/`end_s` are one window, not combinable with `intervals`.
- `stage`: `via_master` (default, what the listener hears), `master`, or `external_file`; `premaster` is refused (`reason="stage_not_rendered"`), never substituted.
- Answer: `table.rows[i]`, `table.cells[i][source_id]`, `table.units`, `table.aggregates`, `evidence.token`, `measurement_id`, `revision`, `revision_before`, `self_noise`, `notes[]` (`intervals_overlap`); `step_s` adds one `profile`, `auto_transport=true` restores transport. `reaper_freshness` and `reaper_coverage` report what the stored evidence still describes. The render temporarily writes and restores REAPER render settings, so `change_count` grows by a fixed self-noise amount; the answer reports it in `self_noise`, `revision_before` stays usable for edits, and known self-noise never raises `stale_context`.

**edit** — `reaper_edit(mode="preview|apply|undo|sends|envelope_points", project_id, expected_revision, ...)`; `project_id` and `expected_revision` are required for every mode:

```text
reaper_edit(mode="preview", project_id=..., expected_revision=..., reason="...",
  operations=[{"kind":"track.set","target_guid":"<guid>","values":{"volume":0.9}},
              {"kind":"send.set","target_guid":"<guid>","values":{"destination_guid":"<guid>","volume":0.36}}])
→ {"plan_id": "...", "plan": {...}, "next_call": {...}}
```

- `apply`: with `plan_id` and the plan's base `expected_revision` → `update` + successor `context_token`; with `operations` and no `plan_id` → applies at once and returns a receipt. `undo`: `plan_id` plus the revision apply produced, or `receipt="r-..."`. Every mutation needs a current structural token (`structural_token`); `advisory` reports it missing instead of blocking, `off` skips it.
- Operations are `{kind, target_guid, values}`, up to 256 per batch: `track.set/create/delete/reorder`, `item.set/create/delete/split/duplicate/timing_apply`, `take.set`, `midi.create`, `fx.set_enabled/set_parameter/insert/delete/move/set_preset/offline`, `send.connect/set/delete`, `envelope.set`, `envelope.level`, `marker.create`, `region.delete`, `tempo.set`.
- `send.connect` is idempotent; `send.set` addresses a row by `destination_guid` or `send_index`. A batch cannot repeat a connect, mix `send.delete` with other send changes on one source, or delete a track it also changes.
- `envelope.set`: `name`, `points`, `mode`, `clear`, `guard` (`min_gain_db`, `forbid_silence`). In `mode="preview"` only, `envelope.level` builds a curve from a measured profile (`{"segments":[...],"target_db":...}`) and `fx.set_parameter` with `{"band":3,"target":"-2 dB"}` resolves through the calibration table — preview then apply.
- `mode="sends"` (`source_guid`) and `mode="envelope_points"` (`owner_guid`, `envelope`, `interval`, `scaling="amplitude|db"`) read without editing.
- `verify`: when passed as an object (`sources`, `interval`, `metrics`, optional `tolerance`), apply renders the sources before and after the batch through the same measurement service and returns `verify.before/after`. A boolean `true` keeps read-back only. The numbers are real measurements; compare them, do not assume the sign.
- Every answer carries `readback` (values re-read from the project) and a `receipt`. `undo` accepts the receipt, not only a plan id.
- FX summary in the context is per plugin: `total`, `set_params_count`, `available_params_count`, `reference`. Effects with readable parameters are compared against a freshly probed default; instruments and generic-parameter plugins stay `first_seen` until they are edited.
- `apply` and `undo` require a non-empty `journal.why` (or `reason`); without it they refuse with `journal_required`. The library appends the entry to `projects/<key>/journal.jsonl` with the receipt, readback, verify, revisions and `undoes` for an undo.

**midi** — `reaper_midi(action="notes|velocity_scale|quantize|generate|grid_check|preview|replace", item_guid=...)`:

- `generate(pattern, from_bar, to_bar, subdivision, accents, humanize, apply=false)`: quarter-note positions through the tempo map, 1-based inclusive bars, step `1/subdivision` QN, `subdivision` has no default.
- `pattern` is caller data: a bare list is one pass; `{"notes":[...],"length_beats":N}` repeats it; same-pitch overlaps are clipped (`collisions_clipped`), exact duplicate onsets are errors. `accents={"beats":[...],"velocity"|"velocity_delta":...}`; `humanize=None | ms | {"time_ms","velocity","seed"}`. `replace(notes)` takes absolute project seconds (`time_s`, `end_s`), validated before the take changes; `generate(apply=true)` keeps notes outside the requested bars.
- `grid_check(tolerance_ms=0, subdivision=None)` returns `deviation_ms`, `notes_within_tolerance`, `notes_beyond_tolerance`, `max_abs_deviation_ms` — numbers, no verdict.
- `preview` **refuses** today: no fragment renderer is injected (`validation_error: preview requires an injected renderer`). Do not promise an audition.

**media** — `reaper_media(action="import|encode", source=...)`:

- `import`: a local path or URL becomes a WAV under `<archive_root>/references` plus a measured profile (LUFS, LRA, peak, true peak, crest, centroid, bands, side/mid). `label` names it; `sha256` identifies the exact file.
- `encode`: a WAV becomes `format`/`bitrate` (mp3 320 by default), and the encoded file is measured; if its true peak exceeds `ceiling_dbtp`, the gain is lowered and it is encoded again (bounded iterations). Answer includes `true_peak`, `lufs`, `iterations`.

**analysis** — `reaper_analyze` runs analyzers over a capture or the live project; `reaper_mix_evidence` returns mix evidence; `reaper_edit_map` maps editable targets; `reaper_render_candidate(plan_id, project_id, revision_after, start_s, end_s, max_duration_s=120)` renders an applied plan's master; `reaper_release` frees a leased artifact.

**inspect** — `reaper_evidence` quotes units behind an evidence token; `reaper_calibrate` records a plug-in parameter map; `reaper_midi` reads and edits MIDI takes; `reaper_timing` measures the click and clean audio attack fronts, ranks local rhythm maps, scores individual crossfades, and applies verified 1x clips (`clock` → `analyze` → `rhythm` → `plan` → `apply`). See [Timing](TIMING.md).

**discovery** — `reaper_capabilities` lists tools and analyzers; `reaper_api_search` + `reaper_api_call` reach the REAPER API catalog; `reaper_action` invokes native Actions.

**research** — `reaper_flamingo_*` and `reaper_spatial_*` are optional listening/spatial backends. They report status and are not required for measurement or editing.

## 4. Units

| Key (alias) | Unit |
|---|---|
| `integrated_lufs` (`lufs`), `loudness_range_lu` (`lra`) | LUFS / LU |
| `peak_dbfs` (`peak`), `true_peak_estimate_dbfs` (`true_peak`) | dBFS / dBTP |
| `rms_dbfs` (`rms`), `crest_db` (`crest`) | dB |
| `spectral_centroid_hz` (`centroid`), `side_mid_db` (`side_mid`), `stereo_correlation` (`stereo`) | Hz / dB / −1..1 |
| `band_level_dbfs` (`bands`) | dBFS per band + `band_aggregate` |
| `clips`, `transients`, `events`, `tempo`, `dc` | count / ratio / 1/s / BPM / linear |

`metrics=None` returns every scalar metric; `"all"`/`"*"` adds bands; an unknown name is refused with the accepted list. Stages: `via_master`, `master`, `external_file` — never substitute one. Audio is in seconds; MIDI uses musical time through the tempo map — never assume constant tempo.

## 5. Context and facts

1. `reaper_session(action="context", archive_root=...)` — project/revision, compact map, health, facts summary, FX summary, changes and audio references. This is the only project context an agent needs for orientation.
2. `reaper_session(action="context", ..., since=<state_token>)` — delta only: project/health/changes/next. Use it after an edit instead of re-reading the map.
3. `reaper_session(action="state", ...)` — exact measured facts, filtered; no render. Missing values mean "not measured in this window", not "absent".
4. `pages=true` on `context` splits the object into pages; the response has pages and no duplicate full context.

A fact is current for the fingerprint that produced it. If the track chain, routing or master changed, `freshness` marks it stale; re-measure only what the decision needs. A missing value is not proof of absence.

## 6. Errors

Every failure is `{"ok": false, "error": {"code", "message", "details", "next_action", "recovery"?}}`; context/candidate failures add `recovery.retry_limit`.

| Code | What to do |
|---|---|
| `validation_error` | details list valid names/indices; never retry unchanged |
| `stale_context` / `stale_after_retry` | read state, refresh once, retry once; after retry nothing was applied |
| `bridge_unavailable` / `bridge_timeout` | check the bridge; a timeout may have completed |
| `media_unresolved` | bring media online; never accept silence |
| `envelope_guard_rejected`, `send_routing_conflict`, `track_delete_refused` | inspect points; one connect per destination; delete alone |
| `receipt_not_found` / `receipt_conflict` / `receipt_not_undoable` | read `undo_block_reason`; undo cannot be exact |
| `transaction_failed`, `render_*` | read project state; no candidate audio was delivered |
| `HANDBOOK_REQUIRED` | read `reaper://handbook` |

## 7. Anti-patterns

1. **Writing a helper script for what a tool already does.** Check `reaper_capabilities(mode="tools")` first.
2. **Measuring a whole track for a local decision.** A 15–40 s window or `bars:8` answers most questions.
3. **Quoting numbers from memory.** Re-read `status`/`reaper_evidence`.
4. **Inventing thresholds.** Use `policy.violations` or state the fact.
5. **Judging by names.** Track/plugin names are not identity; roles come from routing and items.
6. **Assuming `sends: 3` is routing.** Read `mode="sends"`.
7. **Editing without a read-back.** "Applied" is not "changed".
8. **Changing the work mode to pass a gate.** The mode belongs to the owner.
9. **Treating a receipt as understanding**, or a page subset as the whole map.
10. **Substituting stages.** `premaster` does not exist here; `master` ≠ `via_master`.
11. **Silent retries.** After two failures, report the code.

## 8. One worked chain

**Kick punch:** `status` → `reaper_measure(sources=[drums_guid], intervals=[[120,135]], metrics=["integrated_lufs","bands"], bands="third")` → `reaper_edit(mode="preview", …)` → `mode="apply"` → measure the same window → quote both cells.

## 9. Where to read more

`reaper://guide/model`, `reaper://handbook`, `reaper://capabilities/index`, `reaper://capabilities/{domain}`, `reaper://api/{name}`, `reaper://projects/{bridge_session_id}/{project_id}/context/{change_count}`, `reaper://analyses/{analysis_id}/report`, `.../audio/{source_guid}`. A tool description wins over this file for arguments; report any disagreement.
