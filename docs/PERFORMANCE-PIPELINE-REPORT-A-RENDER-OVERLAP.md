# Workstream A — REAPER render completion and safe render/analysis overlap

Scope owner: Workstream A (DAW/bridge side). Target host: Windows 11, REAPER 7.84, Python 3.13.
Basis: static reading of the worktree at `main` (37 commits ahead, dirty), no execution of REAPER, no
MCP calls, no renders, no tests. External sources accessed 2026-11-13.
Evidence labels (defined here; section (d) classifies every material fact and inference,
and inline labels mark claims not already in those tables): **[MEASURED]** (a number or behaviour present in an artifact or a
recorded live observation in this repository), **[SOURCED]** (an official Cockos statement or a
third-party statement, quoted), **[INFERENCE]** (my reasoning from the two above, not itself observed).

---

## (a) Question and scope

Workstream A answers one question: **when is a REAPER-rendered WAV finished, and what may safely run
while REAPER is still rendering?** Concretely:

1. What REAPER 7.84 exposes through documented ReaScript for render progress, completion, per-target
   status, render statistics, and the identity of completed output files.
2. Whether `Main_OnCommandEx(42230, …)` blocks until every target is written, and whether
   `RENDER_TARGETS` / `RENDER_STATS` carry any completion signal before it returns.
3. Which safe designs exist for overlapping rendering with DSP analysis, and what each costs in render
   overhead, state churn, FX/PDC/master semantics, ordering, revision binding and crash recovery.
4. The completion invariant a file must satisfy before it enters the worker pool.
5. Whether the master stage may run concurrently with track analysis, and whether the master-only pass
   is semantically the same render as the batch.
6. A minimal REAPER-only experiment that proves or disproves within-batch overlap, plus the
   completion-invariant validation experiment.

**Out of scope** (Workstream B): whole-pipeline stage modelling, CPU/process/I-O/archive throughput,
worker-count and memory scaling, bounded scheduling and back-pressure, exact-analysis throughput, GPU,
Amdahl bounds, benchmark gates. Where A needs a B number, A states an assumption and asks B in the
cross-workstream section.

A also does **not** decide whether the current analytic results are correct, nor whether the optimized
run is equivalent to the baseline. It only states which comparisons are still missing (section h).

**Constraint actually honoured:** no REAPER process was started, no bridge/MCP call was made, no
script under `work/benchmark/` was run, nothing under `capture-archive/` was written. All artifact
reads were read-only. Two scratch downloads (official docs, changelog) were written outside the repo.

---

## (b) Current code observations

### b.1 The call graph as it exists

```
FullCaptureService.capture                         src/reaper_audio/daw/full_capture.py:298
├─ session.snapshot(include_items, fx_params)      :335        RPC  → project map + revision
├─ bridge.project_digest()                         :355        RPC  → digest (coverage must be all-true)
├─ AudioArtifactWorkspace(.incoming)               :381        creates job-<uuid>/ + manifest
├─ _render_batch(...)                              :389 → :714
│   └─ bridge.capture_render(..., render_settings=128,
│        selected_tracks_via_master=True,
│        parallel_via_master=True, timeout_s=600)  :721-736
│        └─ bridge_client.call("project.capture_render")   bridge_client.py:596 / :248
│             └─ mailbox request.json → Lua defer loop     reaper_bridge.lua:4141
│                  └─ project_capture_render               reaper_bridge.lua:1466
│                       ├─ read/backup 13 RENDER_* fields  :1530-1563
│                       ├─ select ALL tracks               :1622-1627
│                       ├─ write settings (128 + 8<<16)    :1644-1685
│                       ├─ read RENDER_TARGETS (pre-render):1716
│                       ├─ assert action 42230 by name     :1749
│                       ├─ Main_OnCommandEx(42230,0,proj)   :1754
│                       ├─ enumerate targets, io.open each :1756-1804
│                       └─ restore() settings/selection/…  :1574-1619, called :1833
├─ register_asset(path) per track file              :420        stat + full SHA-256 read
├─ _track_activity(42 stems)                        :427-433    full decode of every stem
├─ _render_master_with_gate(...)                    :434 → :769
│   └─ up to 2 × _render_master_only (RENDER_SETTINGS=0, expected_track_count=0, 600 s)
│        then _sha256_file + _wave_master_validation (full decode)  :793-794
├─ second snapshot + digest + _restore_report       :461-471
├─ _build_record(...)                               :473 → :967   ← includes the whole DSP pool
│   ├─ _analyze_rendered_files(payloads, workers)   :1026 / :256
│   └─ analysis_wall_s measured from :977
├─ archive.append(record, files)                    :512        re-hash + copy + re-hash + seal
├─ load_panorama / build_panorama                   :523-533
└─ finally: _set_every_mute(current, original, None):588-600    42 serial RPCs
```

Two facts about this graph matter more than any other:

* **Nothing is analysed until every render has returned.** `_analyze_rendered_files` is called from
  `_build_record` (`full_capture.py:1026`), and `_build_record` is called at `:473`, after the batch,
  after the source-activity pass, after the master gate, and after the post-render snapshot. The
  render phase and the analysis phase are strictly serial [MEASURED: code order].
* **The completion criterion is the RPC return plus four weak file checks**, not a writer-closure
  proof. See b.4.

### b.2 The RPC transport

* One mailbox file per REAPER instance: `runtime_dir/request.json`; the Lua deferred loop reads it,
  deletes it, executes the handler **synchronously on REAPER's main thread**, writes
  `responses/<id>.json`, then re-arms `reaper.defer` (`reaper_bridge.lua:4083-4164`).
* A single global `client.lock` serialises all bridge calls from all Python threads/processes
  (`bridge_client.py:201-231`). There is no per-operation concurrency.
* The client polls for the response every 20 ms up to a hard deadline (`bridge_client.py:284-297`);
  `capture_render` passes `timeout_s=600.0` (`full_capture.py:726`, `:754`).
* The Lua loop also writes a liveness beacon (10-digit `os.time()`) at most every 2 s
  (`reaper_bridge.lua:4142-4150`, `write_heartbeat` `:1912`). **The beacon is produced by the same
  thread that runs the render**, so it necessarily freezes for the whole render.

### b.3 Measured stage times of the sealed production capture

Artifact: `work/benchmark/himmp/capture-archive/captures/cap_01M7A7ZV8VWK4SA8FHGRC9Q04S/record.json`
(445,001,421 bytes), `timings` block. All values are seconds.

| Field | Value | Note |
|---|---|---|
| `capture_render_s` | 134.851 | snapshot + digest + both renders + source activity + hashing |
| `render_batch_s` | 98.470 | 42 selected tracks via master, parallel flag on |
| `render_master_s` | 9.505 | master-only pass, attempt 1 |
| `render_s` | 107.975 | exactly `98.470 + 9.505` |
| `source_activity_s` | 17.767 | parent-side decode of all 42 stems |
| `analysis_wall_s` | 418.177 | from `_build_record` start to pool end + row assembly |
| `analysis_pool_wall_s` | 408.274 | ProcessPoolExecutor lifetime |
| `dsp_worker_elapsed_sum_s` | 3011.645 | sum over workers (not wall) |
| `wav_decode_worker_elapsed_sum_s` | 16.552 | sum over workers |
| `batch_render_settings` | `524416` | 128 + (8<<16) |
| `master_render_settings` | `0` | master mix |
| `batch_target_count` / `file_count` | 42 / 42 | |
| `master_target_count` / `file_count` | 1 / 1 | |
| `capture_status` | `valid` | |
| `revision_before.change_count` → `revision_after` | 86 → 254 | Δ168 for two render calls |
| `restore_report.counter_delta` | 168 | REAPER's counter is not a content fingerprint |

Derived arithmetic [MEASURED, computed from the fields above]:

* `capture_render_s − render_s = 26.876 s` of non-render work inside the render phase, of which
  17.767 s is the source-activity decode; ~9.1 s remains for hashing/stat/snapshot overhead.
* `703.072 − 134.851 − 418.177 = 150.044 s` unaccounted by those two timers — 21.3 % of end-to-end.
  That is `archive.append` (re-hash + copy + re-hash of 43 files ≈ 3.34 GB), record commit
  (445 MB), panorama build, and MCP `_jsonable`. Component split belongs to B; A only asserts the
  gap exists and is not inside either named timer.
* Worker utilisation `3011.645 / (408.274 × 8) = 92.2 %`; pool efficiency `408.274 / 418.177 = 97.6 %`.
  The pool is already well fed — the wasted capacity is the 108 s in which the pool does not exist.

### b.4 What the current completion criterion actually is

In order of strength:

1. **RPC return.** `bridge.capture_render` returns only after `project_capture_render` finishes, and
   the Lua function cannot finish before `Main_OnCommandEx(42230)` returns (`:1754` precedes `:1756`).
   If 42230 is synchronous, the RPC return *is* an authoritative completion signal for the whole batch.
2. **Target-set contract.** Before rendering, the Lua reads `RENDER_TARGETS`, checks every target's
   parent equals `output_dir`, requires a `.wav` suffix, **refuses to render if any target already
   exists** (`:1734-1738`), and requires `#targets == expected_track_count` (`:1742-1747`).
3. **Per-file open and size.** After the render each target is opened with `io.open(path,"rb")` and
   `seek("end")`; a size `<= 44` is rejected (`:1796-1800`). This proves the file exists and has a
   header, **not** that a writer closed it: `io.open` uses the C runtime's default share mode, which
   normally permits other readers, so a file REAPER still holds open can be opened and sized
   [INFERENCE — the share mode is a runtime default, not a REAPER guarantee].
4. **Registration hash.** `workspace.register_asset(path)` (`full_capture.py:420`) stats the file,
   then streams SHA-256 and fails if the byte count read differs from the stat size
   (`analysis/artifacts.py:367-388`). This catches "grew while hashing", not "grows after hashing".
5. **Archive re-hash.** `CaptureArchive.append` re-hashes every source and compares with the record,
   then copies and re-hashes the copy (`context/full_capture.py:574-599`). This is the only check that
   would catch a write that continued after registration — and it runs *after* analysis.

So the present path is *saved* by the RPC return being synchronous, and by nothing else. If 42230 ever
returned early, the failure would not be detected until `append`, i.e. after 418 s of DSP on possibly
truncated files.

### b.5 The master stage

* The batch sets `RENDER_SETTINGS=128` (+`8<<16` when `parallel_via_master`), and 128 has bit 0 and
  bit 1 clear, so **the batch contains no master target** (`includes_master = render_settings % 2 == 1`
  is false at `:1660`; `expected_target_count = track_count` at `:1742`).
* The master pass sets `RENDER_SETTINGS=0`, `expected_track_count=0`, and expects exactly one target
  (`:1650-1651`, `:1742`, `:1805`). It produced exactly one 79,518,282-byte file
  [MEASURED: `master_attempts[0]`].
* **The 44,100 Hz in `master_attempts[0]` is not a divergence.** All 87 `"sample_rate"` occurrences in
  the sealed record are 44,100, and all 44 `"frames"` occurrences are 13,252,932
  (= 300.52 s × 44,100). `RENDER_SRATE=0` means "project rate" per the official docs, and the project
  is 44.1 kHz; every one of the 43 assets is 2-channel, 13,252,932 frames, 300.52 s
  [MEASURED: `grep -o '"sample_rate":[0-9]*'` → 87 × 44100; `frames` → 44 × 13252932].
* **Master-only is not "the batch's master", because the batch has no master.** The two stages are
  different by construction: the stems are *selected tracks via master*; the master is the mix sum.
  The record labels them `selected_track_via_master_parallel_batch_post_fx` and `master_mix_post_fx`
  (`full_capture.py:413`, `:454`), which is correct.
* **129 is not a legal combination.** The documented mask makes `128` = "selected tracks via master"
  and `&(1|2)==0` = "master mix" — alternative source selectors, not additive flags. The live probe
  recorded in `full_capture.py:386-387` produced only the master file with an empty `RENDER_TARGETS`,
  which is consistent with bit 0 taking precedence. Keeping two calls is the only documented route.
* **The master cannot render concurrently with anything else in REAPER.** It is a second
  `Main_OnCommandEx(42230)` on the same main thread, so it queues behind the track batch. Only DSP
  analysis can overlap it, because that is a separate OS process (I9). "Master during track analysis"
  is legal; "master during track render" is not.
* **Evidence required before accepting the master gate:** (i) C1–C7 on the master file itself;
  (ii) source evidence that is *not* routed through the master chain, or an explicit `unknown` that
  makes the classifier fail closed rather than "explained"; (iii) proof that the FX chain was online
  and initialised when the render started. Point (iii) is not hypothetical: the documented bit
  `&(16<<16)` exists precisely to "delay render start to allow FX to initialize and load samples", and
  a project that loads with plug-ins still scanning — or offlined after a crash — renders a silent
  master through a fully "valid" settings echo. The 13:33 silent master (M19) is consistent with that
  class, and a clean REAPER restart is exactly the remedy that worked.

### b.6 Semantics of "via master" that the record does not state

`whatsnew.txt` v7.70 distinguishes "rendering stems … **with master FX** (as opposed to **via master**,
which also renders track sends)". So a via-master stem is the selected track *plus its sends*, taken
through the master path — not a dry stem and not a pre-master sum. Two consequences:

* Summing the 42 stems will not reconstruct the master unless every master insert is linear
  [INFERENCE from the definition of via-master].
* `_track_activity` decodes those same via-master stems (`full_capture.py:427-433`) and feeds the
  master-silence classifier (`:86-133`). If a master-chain insert kills signal without muting or
  zeroing the master fader, the stems are silent too, the classifier reports
  `all_rendered_sources_silent` → `expected_silence=True`, and the gate **accepts** a broken master.
  That is a circular-evidence hole, not a bug in the arithmetic [INFERENCE].

### b.7 Revision and timer accounting gaps

* Every render call selects all tracks and writes/restores 13 settings; the counter moved 168 for two
  calls (≈84 per call). `GetProjectStateChangeCount` is documented only as "returns an integer that
  changes when the project state changes" — it cannot distinguish our own churn from a user edit.
* `record.revision_after` is taken from the master render's `revision_after` (`full_capture.py:456`),
  i.e. **before** the `finally` restore loop.
* The `finally` calls `_set_every_mute(current, original_mute, None)` (`:591`), which issues **one RPC
  per track** (`:660-662` → `_set_mute` `:632-652`), each carrying an exact `change_count`. The record's
  `timings.total_s` is computed at `:585`, before that loop. So `total_s` excludes 42 round trips, and
  the returned `revision` may be stale if a no-op `B_MUTE` write bumps the counter
  [INFERENCE; not resolvable from the artifacts, see g/E5].
* `mute_bridge_calls = 0` in the record does not mean "no restore RPCs happened"; it means the counter
  was snapshotted before the loop.

### b.8 Why a wrapper error coexists with a sealed, valid record

`optimized-capture.json` says `ok:false`, `elapsed_wall_s:703.072`,
`error: "capture did not return a valid capture_id"`. The record written at the same minute
(13:53:35) says `capture_status:"valid"`, master attempt 1 `valid:true`, peak −1.42 dBFS.
Both are true, and the code explains it:

1. `reaper_capture_snapshot` is wrapped by `_guard`, which converts **any** exception into
   `{"ok": false, "error": {"code", "message", "details", "next_action"}}`
   (`mcp_server.py:128-154`). `capture_id` then lives at `error.details.capture_id`, not at the top
   level — and only for the typed `CaptureMasterInvalid`.
2. The harness checks `result.get("ok") is True and result.get("capture_id")`
   (`work/benchmark/himmp/perf/optimized_capture_run.py:51`) and raises when the top-level key is
   absent, then **overwrites `optimized-capture.json` with the traceback**, destroying the only copy of
   the tool response.
3. Therefore the 703 s run raised *after* `archive.append` succeeded. Three code paths can do that:
   (i) the 42-RPC `finally` restore loop (`full_capture.py:591`) — 42 chances for a `stale_context` or
   a 10 s timeout; (ii) `load_panorama` on the freshly written sidecar (`:525`), which can raise
   `panorama_binding_invalid` or `panorama_capacity_exceeded`; (iii) `_jsonable(result)` in the MCP
   wrapper (`mcp_server.py:1725`). All three leave the record valid and sealed.

**The artifacts cannot discriminate between (i), (ii) and (iii)** because the response was discarded.
This is itself a finding: a capture can be durably committed and still be reported as a failure, and
the caller loses the `capture_id` needed to recover it [MEASURED for the coexistence; INFERENCE for
the mechanism].

By contrast, the 13:33 capture `cap_01M7A6T9AVHXP5W9C24GFPHEJG` legitimately has
`capture_status:"invalid_master"`, two attempts, both `peak_dbfs:-160.0`, `silent:true` — that is the
documented `CaptureMasterInvalid` path, and it does carry `capture_id` in its details
[MEASURED].

---

### b.9 What the documented API does not provide (direct answer to Q1/Q2)

* **No render-progress function.** The 732-entry reference contains no function whose name or
  description mentions render progress (S7); `whatsnew` records progress only as a UI feature
  ("show percentage complete in render processing dialog").
* **No completion event or callback.** `defer` is a generic idle callback (S6/S9); nothing in the API
  fires on render start, per-target completion, or render end.
* **No per-target status.** `RENDER_TARGETS` is a prediction (S2); `RENDER_STATS` is a post-hoc list for
  "the most recently rendered files" (S3), with no documented schema, no timestamps and no way to tell
  a *current* render's partial results from a previous render's leftovers.
* **No live output-file channel other than the filesystem.** During a batch the only observable is the
  WAV files themselves — which is exactly why C2 (writer closure) matters.
* **The only in-band completion signal is the return of the blocking action**, and that signal is
  aggregate: all targets, not one.

---

## (c) External sources

All accessed 2026-11-13. Official pages are the live v7.85 documentation; the installed build is 7.84,
one release behind, and A found no render-API change between them in the changelog.

| # | Label | Source | What it establishes (quoted where the claim depends on it) |
|---|---|---|---|
| S1 | [OFFICIAL REAPER DOCS] | https://www.reaper.fm/sdk/reascript/reascripthelp.html — `GetSetProjectInfo`, `RENDER_SETTINGS` | "RENDER_SETTINGS : (&(1\|2)==0)=master mix, &1=stems+master mix, &2=stems only, &4=multichannel tracks to multichannel files, &8=use render matrix, &16=tracks with only mono media to mono files, &32=selected media items, &64=selected media items via master, &128=selected tracks via master, … &(8<<16)=parallel render via master, &(16<<16)=delay render start to allow FX to initialize and load samples" — this is the exact meaning of 0, 128, 524416 and 129. |
| S2 | [OFFICIAL REAPER DOCS] | same page — `GetSetProjectInfo_String`, `RENDER_TARGETS` | "RENDER_TARGETS : semicolon separated list of files that would be written if the project is rendered using the most recent render settings" — **future tense: a prediction, not a record of completed files.** |
| S3 | [OFFICIAL REAPER DOCS] | same page — `RENDER_STATS` | "RENDER_STATS : (read-only) semicolon separated list of statistics for the most recently rendered files. call with valuestr=\"XXX\" to run an action (for example, \"42437\"=dry run render selected items) before returning statistics." — post-hoc statistics of the *most recent* render; it can *run an action*; it is not a progress channel. |
| S4 | [OFFICIAL REAPER DOCS] | same page — `RenderFileSection` | "RenderFileSection(source_filename, target_filename, start_percent, end_percent, playrate) … Not available while playing back." — renders a section of an existing media file, not the project graph. |
| S5 | [OFFICIAL REAPER DOCS] | same page — `Main_OnCommandEx` | "Performs an action belonging to the main action section." — no statement about blocking, return timing, or completion. |
| S6 | [OFFICIAL REAPER DOCS] | same page — `defer` | "Adds code to be called back by REAPER. Used to create persistent ReaScripts that continue to run and respond to input, while the user does other tasks." — a callback model; no event list, no render event. |
| S7 | [OFFICIAL REAPER DOCS] | same page — full function index | **No function in the 732-entry reference mentions render progress**: searching the reference for `RenderProgress`, `render_progress`, `GetRender`, `RENDER_PROGRESS` and "render status" returns 0 matches. |
| S8 | [OFFICIAL REAPER DOCS] | same page — `GetProjectStateChangeCount` | "returns an integer that changes when the project state changes" — the only revision signal, and it is coarse. |
| S9 | [OFFICIAL REAPER DOCS] | https://www.reaper.fm/sdk/reascript/reascript.php | Deferred scripts "run in the background until terminated by the user … react to events"; no render-lifecycle event is documented anywhere in the page. |
| S10 | [OFFICIAL COCKOS] | https://www.reaper.fm/whatsnew.txt, v5.974 (2019-04-06) | "Render: add action to render using most recent settings, then automatically close render dialog when finished" — the origin and intended semantics of action 42230: it closes **when finished**. |
| S11 | [OFFICIAL COCKOS] | whatsnew.txt, v5.980 | "Render: add option to render selected tracks via master" — the 128 source mode. |
| S12 | [OFFICIAL COCKOS] | whatsnew.txt, v6.10 | "ReaScript: add RENDER_TARGETS, to retrieve the list of files that would be written given current project render settings" — confirms S2's predictive reading. |
| S13 | [OFFICIAL COCKOS] | whatsnew.txt, v6.30 | "ReaScript: add GetSetProjectInfo_String(RENDER_STATS) to retrieve statistics for the most recently rendered files"; and "Render: ignore preference to auto-close render dialog if normalizing or dry-run rendering" — auto-close is conditional. |
| S14 | [OFFICIAL COCKOS] | whatsnew.txt, v7.26 | "Render: automatically reopen render dialog modelessly when render is complete, unless set to auto-close or auto-return to render settings". |
| S15 | [OFFICIAL COCKOS] | whatsnew.txt, v7.37 | "Render: add render setting to enable parallel render via master…"; and "Render: after canceling incomplete render, prompt the user to save or delete the partially rendered output files" — **partial output files are an officially acknowledged artifact that can survive on disk**. |
| S16 | [OFFICIAL COCKOS] | whatsnew.txt, v7.58 | "Render: support parallel rendering even if rendered areas do not exactly overlap in time, as long as routing permits". |
| S17 | [OFFICIAL COCKOS] | whatsnew.txt, v7.62 | "ReaScript: GetSetProjectInfo_String('RENDER_STATS') includes rendered file length" — the payload is being extended; its shape is not a stable contract in the docs. |
| S18 | [OFFICIAL COCKOS] | whatsnew.txt, v7.70 | "Render: add support for rendering stems/items/razor edits with master FX (as opposed to via master, which also renders track sends)" — defines what a via-master stem contains. |
| S19 | [OFFICIAL COCKOS] | whatsnew.txt, v7.75 | "ReaScript: automatically close render dialog after running GetSetProjectInfoString('RENDER_STATS') with 'calculate loudness of selected items source media'" — RENDER_STATS is tied to loudness analysis workflows, not to render completion. |
| S20 | [THIRD-PARTY] | https://github.com/danishaft/reaper-mcp (README, "Known limitations") | "The native render lifecycle remains experimental because action `42230` can block the Lua event loop. The isolated external render path is verified." — independent corroboration of the blocking behaviour, from a project with no relation to this one. |
| S21 | [THIRD-PARTY] | https://github.com/shiehn/total-reaper-mcp/blob/71b82bfc/server/tools/rendering.py | Maps `42230` to "Render project regions", `41824` to "Render entire project", `41825` to "Render time selection". This mapping is **wrong** relative to the runtime name measured in this repo ("File: Render project, using the most recent render settings, auto-close render dialog"). Cited only as evidence that third-party action-ID tables must not be trusted. |
| S22 | [THIRD-PARTY] | https://www.lightningjar.com/blog/binary-as-test-oracle (2026-11-01) | Uses `Main_OnCommand(42230, 0)` as the render oracle and must drive REAPER's dialogs externally ("Reaper still raises dialogs on a bare display: … the render dialog … the runner polls every 1.5 seconds, finds any visible window … and presses Return"). Independent evidence that the render path is dialog-coupled and not script-friendly. |

Repo-internal measured documents used as evidence (not external sources):

* `docs/flamingo-ab-render-proof.md` — A/B proof of the capture path against REAPER's own render;
  contains the only recorded statement in this repo about the render/bridge interaction:
  *"`Main_OnCommandEx` returns `bridge_timeout` because the render blocks the bridge defer loop, and
  the library's retry re-issues the command — the second render hits an existing file and REAPER opens
  a modal 'Render Warning: overwrite files', which then blocks the bridge indefinitely."* Also records
  native-vs-native residual −46.82 dBFS on the full mix, and −240.00 dBFS (exact zero) on an FX-free
  track.
* `docs/FULL-CAPTURE.md` — the master-gate contract; states that the non-parallel 128 path "was
  measured but is not equivalent to the archived production audio and must not be substituted".

---

## (d) Measured facts vs inferences

### Table D1 — Measured facts (artifact or recorded live observation)

| # | Fact | Value / evidence |
|---|---|---|
| M1 | Render phase share of end-to-end | 134.851 s of 703.072 s = 19.2 % [record `timings`] |
| M2 | Batch vs master render split | 98.470 s + 9.505 s = 107.975 s exactly |
| M3 | Non-render work inside `capture_render_s` | 26.876 s, of which 17.767 s is the parent-side source-activity decode |
| M4 | Analysis share | 418.177 s = 59.5 %; pool 408.274 s |
| M5 | Post-analysis unaccounted gap | 150.044 s = 21.3 % of end-to-end |
| M6 | Every asset is full length | 43/43 audio rows: 44,100 Hz, 2 ch, 13,252,932 frames, 300.52 s |
| M7 | Master attempt 1 is valid and complete | peak −1.4203 dBFS, RMS −15.6391 dBFS, 79,518,282 bytes, `valid:true` |
| M8 | Render settings actually used | batch 524416, master 0; both `RENDER_SRATE=0`, `RENDER_BOUNDSFLAG=1`, `RENDER_CHANNELS=2` |
| M9 | Counter churn per capture | 86 → 254, Δ168, two render calls |
| M10 | All container hashes differ baseline vs optimized | `capture-parity.json`: 43/43 `sha256_equal:false`, 43/43 `frames_equal:true`, `stage_equal:true` |
| M11 | Digest fields differ | `digest_fields_equal:false`, coverage equal |
| M12 | Render action name resolved at runtime | "File: Render project, using the most recent render settings, auto-close render dialog" (recorded in an analysis report) |
| M13 | Timeout headroom | 600 s budget vs 98.47 s observed batch = 6.1× |
| M14 | Bridge liveness beacon period | ≤ 2 s, written by the same defer loop (`reaper_bridge.lua:4143`) |
| M15 | Prior full-mix render is not deterministic | native-vs-native residual −46.82 dBFS, depth −28.18 dB (`flamingo-ab-render-proof.md` §4) |
| M16 | FX-free control is deterministic | native vs native and native vs capture both null to −240.00 dBFS (exact zero) |
| M17 | Render blocks the defer loop | recorded pitfall in `flamingo-ab-render-proof.md` §6: "the render blocks the bridge defer loop" |
| M18 | A modal overwrite prompt can block the bridge indefinitely | same pitfall, same section |
| M19 | 13:33 capture failed on master silence | `capture_status:"invalid_master"`, 2 attempts, both peak −160.0 |
| M20 | 13:53 capture is valid but its wrapper reported failure | record `valid`; `optimized-capture.json` `ok:false`, 703.072 s |

### Table D2 — Inferences (mine, not observed)

| # | Inference | Basis | Confidence |
|---|---|---|---|
| I1 | `Main_OnCommandEx(42230)` blocks until all targets are written | M17 + M18 + the fact that 43 full-length files existed at hash time; S10 ("close … when finished") | High |
| I2 | No ReaScript callback, event or progress API exists for renders | S7 (0 matches), S9, S5, S20 | High |
| I3 | `RENDER_STATS` cannot serve as a completion signal for a *new* render | S3 (most recently rendered files, stale until replaced), S17/S19 (evolving shape), and it can trigger an action | High |
| I4 | The 150 s gap is archive copy/hash + 445 MB record + panorama + jsonable | M5, `context/full_capture.py:574-599`, worklog serialization measurements | Medium |
| I5 | The 703 s failure came from a post-archive raise (finally-restore, panorama load, or jsonable) | b.8 code trace | Medium (mechanism family high, specific path unknown) |
| I6 | The returned revision may be stale by 42 if no-op `B_MUTE` writes bump the counter | b.7; S8 | Low–Medium (needs E5) |
| I7 | Via-master stems include master-chain processing, so the source-activity gate is not independent evidence | S18 | Medium–High |
| I8 | Splitting the render into batches can change PCM for non-deterministic plugins | M15, M16, `FULL-CAPTURE.md` ("non-parallel 128 … not equivalent") | Medium–High |
| I9 | Python DSP analysis can run while REAPER renders | Separate OS processes, no shared state, files on disk; only the bridge lock is held | High (but completion proof is the problem, not the concurrency) |
| I10 | The bridge cannot distinguish "still rendering" from "hung" | b.2: one mailbox, one lock, absent response in both cases; heartbeat frozen in both cases | High |
| I11 | Reordering master before batch saves ~108 s on runs whose master is silent | M2, M19: 13:33 spent the batch before discovering silence | High |
| I12 | `$tracknumber`→GUID mapping assumes index+1 and is unchecked | `reaper_bridge.lua:1776-1788`; no cross-check exists | Low (risk, not observed fault) |

---

## (e) Candidate designs

### e.0 The completion invariant (required by every design)

A file may enter the worker pool only when **all** of the following hold. The signal that starts the
check is either the authoritative RPC return (batch designs) or an explicit closure probe (watcher
designs); a sleep, a single size sample, or "size did not change once" is not a criterion.

| # | Check | Test | Rejects |
|---|---|---|---|
| C1 | Expected identity | path ∈ pre-render `RENDER_TARGETS`; direct child of the job dir; `rali-<job_id>-` prefix; parses to exactly one track index or master; no index claimed twice | stale targets from a previous job, path collisions, duplicate mapping |
| C2 | Writer closure | `CreateFileW(path, GENERIC_READ, dwShareMode=0, OPEN_EXISTING)` succeeds, three consecutive times 250 ms apart | a file REAPER still holds open; a file AV/indexer is scanning (safe false "open") |
| C3 | Container | RIFF/WAVE; `fmt ` chunk present with the expected channels/rate/bit depth; `data` chunk size = frames × channels × bytes; **actual file size** = header bytes + data size (no truncation, no trailing bytes) | truncated files, zero-length data, wrong sink format |
| C4 | Length | frames = round((end−start) × rate) + tail frames, tolerance 0 | partial renders, off-by-one window, wrong bounds flag |
| C5 | Rate/channels | equal to the request and to the bridge's echo (already enforced client-side, `bridge_client._validate_render_echo`) | silent setting-ignore |
| C6 | Stability | SHA-256 over two full reads separated by ≥ 250 ms is identical, and byte count equals the stat size in both | writes continuing after C2 |
| C7 | Provenance | mtime ≥ render-call start and ≤ RPC return + skew; file absent before the render (already pre-checked at `:1734`) | recycled files |
| C8 | Failure handling | any failure ⇒ the file never reaches a worker; it is moved to `<job>/rejected/` (not deleted, not archived) and the capture fails with a typed error naming the target | silent acceptance |

C2 is the only check that can prove *closure* rather than *consistency*. It is safe (read-only intent,
no sharing requested, no write access) and it cannot corrupt or truncate. Its false positives are
"file is still open" when an unrelated reader holds a handle — which delays, never accepts. Its false
negative is a writer that has already closed, which is what we want. C2 is meaningless *during* a
render unless REAPER closes each target as it finishes it — see g/E3b.

### e.1 Design comparison

| Design | Render calls | Added render overhead | Counter/state churn | FX/PDC/master semantics | Ordering / overlap | Revision binding | Crash recovery | Legal under documented API? |
|---|---|---|---|---|---|---|---|---|
| **D0** current: one batch + one master, then pool | 2 | 0 | ≈168 per capture [M9] | unchanged; stems via master, master separate | none: 108 s + 418 s serial | one window, 2 expected-revision checks | archive only at end; partials reclaimed by stale cleanup | yes |
| **D1** one batch + race-safe watcher | 2 | 0 | ≈168 | unchanged | analysis starts per closed file; potential −95 s | same window; watcher must not call the bridge (lock is held) | analysed prefixes survive a crash; no record | file polling only; the *watcher* is not a documented API, and no Lua watcher can run (I2/I10) |
| **D2** N bounded batches + master | N+1 | ~1.3–2 s fixed per call (settings set/restore + target enumeration + `RENDER_TARGETS`) + re-selection of all tracks | ≈168 + 84·(N−1) | unchanged per batch; **but plugin state may differ between calls (M15)** | analyse batch k while rendering k+1; −54 s at N=2, −81 s at N=4 | must stay inside one revision window; each call re-checks | completed batches' audio is on disk; analysis results are in memory only | yes |
| **D3** one target per call | 43 | 42 × fixed cost + 42 × full-track re-selection; +40–80 s [INFERENCE] | 42 × 84 ≈ 3,500 | per-target render is the most isolated; identical settings each time | maximal granularity, maximal overhead | 43 checks; trivially attributable | best (each file provable individually) | yes |
| **D4** render queue | queue add + "render all" | queue auto-saves the project (whatsnew), adds a queue window and a saved-RPP dependency | queue-managed | renders from a saved RPP, not the live project object | completion only by polling files/window | binds to the saved RPP, not to the in-memory revision | queue survives project close | yes, but mutates the project file and has no completion API |
| **D5** `RenderFileSection` | — | — | — | — | — | — | — | **no**: renders a section of an existing media file (S4), not the project |

Every cell in this table is a design statement or an **[INFERENCE]** unless it cites M9 (the measured
Δ168) or S1/S18 (documented semantics); no design other than D0 has been executed.

### e.2 Per-design assessment

**D0 — current.** The cheapest in state churn and the only path with production evidence. Its cost is
structural: 108 s in which 8 DSP workers are idle, and a 600 s timeout that is 6.1× the observed batch.
Keep as the fallback and as the parity reference.

**D1 — native batch + race-safe watcher.** Attractive because render overhead is zero and analysis
could start 2–3 s into a 98 s batch. It is only viable if REAPER **closes each target as it finishes
it** and does not preallocate. Both are unknown and both are cheap to measure (g/E3b, g/E3c). Even if
viable, it has three hard constraints: (i) the watcher cannot be Lua (no defer cycle runs — M17/I2) so
it must be an external process; (ii) that process must not touch the bridge (the global lock is held
for the whole render, `bridge_client.py:201-231`), so it cannot ask REAPER anything — it can only look
at the filesystem; (iii) closure must be proved by C2, not by size stability, because a preallocated
file has its final size from the first byte.

**D2 — bounded batches (recommended first step).** Each batch's completion signal is the RPC return,
which is authoritative and needs no undocumented behaviour. The DSP pool is created once and fed
batch by batch; batch k+1 renders while batch k is measured. Costs: one settings set/restore, one
`RENDER_TARGETS` check, and one full-track selection pass per extra call. The whole non-render residue
inside `capture_render_s` is `capture_render_s − render_s − source_activity_s ≈ 9.1 s` for **two**
calls (hashing, stat, snapshots included), so the settings/enumeration part is ≤ 4.5 s per call and
probably 1–2 s [INFERENCE from M3]. Expected saving on the render+analysis segment (526.152 s):
**54 s at N=2, 81 s at N=4, 94.5 s at N=8** (perfect overlap, zero contention) — 81 s is 15.4 % of
that segment and 11.5 % of the 703 s iteration.
The real risk is not overhead but **semantics**: M15 shows the same project rendered twice in one
session differs by −46.82 dBFS residual on the full mix because of analogue-modelled plugins. Any
batching change must therefore pass a PCM parity gate (g/E4) before it is allowed to replace the
single-batch path. Note that `docs/FULL-CAPTURE.md` already records that switching the *parallel flag*
changed the archived audio — the project has direct evidence that render-path changes are audible.

**D3 — one target per call.** The only design in which GUID→file identity is provable by construction:
a call with `expected_track_count=1` and a master-only-style target list cannot return another track's
file. It costs 42 extra settings cycles and ~40–80 s, i.e. it consumes most of what D2 saves. Use it
as (a) a correctness oracle for D2's mapping, and (b) the fallback if E4 shows that batch splitting is
not PCM-neutral.

**D4 — render queue.** Officially supported and genuinely batched, but it renders *saved* project
state, writes/auto-saves an RPP, has no completion API, and would break the "no project mutation"
constraint. It also moves the completion question to a window that a script cannot observe. Reject for
the interactive path; note it as an option for an offline farm.

**D5 — `RenderFileSection`.** Rejected by the documentation (S4). It cannot render a project graph.

### e.3 Recommended ordering (A's opinion)

1. **Immediate, zero-audio-risk:** implement C1–C8; make the post-commit path non-fatal (persist the
   response, then restore mutes in one batched RPC or make failures non-raising and reported); record
   `total_s` after the `finally`; add `revision_after_restore`. These change no audio and remove the
   class of failure seen at 13:53.
2. **Low-risk, real gain:** D2 with N=4 and master **first**, plus a strict classifier when source
   evidence is absent (fail-fast: a silent master costs 9.5 s instead of 108 s + 418 s), and the
   `&(16<<16)` delay-render-start bit on the first render after a project load. Gate on E4 and E7.
3. **Conditional:** D1 only if E3b proves progressive closure; then combine D2 + D1 (watcher per
   batch).
4. **Fallback/oracle:** D3 for identity checks and for any project where E4 fails.

---

## (f) Failure modes

Each entry: mechanism → how it manifests today → detection → required mitigation. Mechanisms are
**[INFERENCE]** unless a measurement or source is cited inline.

1. **Partial file treated as complete.** Officially possible: REAPER asks whether to keep partially
   rendered files after a cancel (S15). Today a partial file passes checks 3–4 of b.4 (exists, > 44
   bytes) and its hash is recorded at `:420`; only `append` would catch a later change.
   *Mitigation:* C2+C3+C4+C6; quarantine, never delete.
2. **Render dialog state.** `42230` auto-closes *when finished* (S10), but auto-close is suppressed
   for normalize/dry-run (S13), and an existing target raises a modal overwrite warning which "blocks
   the bridge indefinitely" (M18). Today the code refuses to render if any target exists (`:1734`),
   which prevents the common case; it cannot prevent a warning caused by a path REAPER considers
   existing (case, short name, AV lock).
   *Mitigation:* unique job-scoped prefixes (already), pre-flight existence check (already), a
   dialog-title watchdog outside REAPER, and never retrying a render call that timed out.
3. **Master silence.** Two causes: genuine project silence (classifier handles it) and
   master-chain failure (classifier cannot see it, I7). M19 shows the fail-closed path works; the
   circular-evidence hole remains for a non-muted master insert that kills signal.
   *Mitigation:* compute source activity from a stage that does not pass through the master chain
   (pre-master tap or a second render mode), or require at least one stem whose peak exceeds the floor
   before accepting `all_rendered_sources_silent` as an explanation.
4. **Stale targets.** `RENDER_TARGETS` is a prediction (S2). If a track is added/removed between the
   target read and the action, the count check fails *after* the render (`:1766`), and the cleanup
   `os.remove`s the new files (`:1835-1838`). Correct but wasteful.
5. **Orphan handles.** `os.remove` on a file still held by REAPER fails silently in Lua; the file then
   survives in the job dir and is only reclaimed by `cleanup_stale_audio_jobs` after 24 h with the
   `rali-<job_id>-` prefix rule (`analysis/artifacts.py:458-475`).
6. **Revision drift.** Two distinct drifts: (a) returned revision may precede the `finally` restore
   (b.7); (b) the record's per-asset `capture_revision` is the *pre-render* revision for every asset
   (`full_capture.py:1059`), so it cannot prove the project did not change mid-capture. The only guard
   is the per-call `expected.change_count` check (`reaper_bridge.lua:1503-1508`), which cannot
   distinguish a user edit from our own churn.
7. **Crash mid-batch.** The archive is written only after analysis. A crash at any point before
   `append` leaves: temp WAVs in `.incoming/job-<id>/` (reclaimed after 24 h), no record, and a
   project whose render settings may or may not have been restored (the Lua `restore()` runs only if
   the handler returns; a hard kill skips it). `DIRTY` and the time selection are also restored only
   on the normal path. Worse, the authoritative target list exists only in the dead process's memory:
   after a restart `RENDER_TARGETS` is recomputed from whatever settings survived, so it may describe a
   different file set than the crash left behind, and there is no `revision_after` to bind either set.
   Recovery must therefore be "write the intended target list to the job manifest *before* the render",
   which today does not happen.
8. **Path collisions.** Prevented by the per-job directory and the `rali-<job_id>-` prefix; the
   residual risk is case-insensitive collisions on Windows and the pre-existing-target check.
9. **Antivirus / OS caching.** A scanner opening a just-written WAV makes C2 fail (safe, delays) and
   can make `os.replace` fail; a scanner reading during hashing can slow the 3.3 GB pass. On the
   positive side, the OS page cache makes the second hashing pass cheap, which is why three full reads
   (register, append-verify, append-copy) cost far less than 3 × 3.3 GB of I/O [INFERENCE].
10. **Cleanup/lease leaks.** The `finally` runs `workspace.close()` even when the archive succeeded, so
    temp WAVs are removed after the archive copy — correct. But if `_set_every_mute` raises *before*
    `workspace.close()` (it is in the same `finally`, `:591`), the workspace is still closed by the
    nested `finally` (`:592-594`) — good — while the caller receives the mute error instead of the
    capture result (the 13:53 class of failure).
11. **Timeout hysteresis.** A timeout at 600 s makes Python raise while REAPER keeps rendering; the
    Lua side later writes a response nobody reads (orphan in `responses/`), and any follow-up RPC
    blocks on the still-held lock. The correct behaviour is to treat a render timeout as fatal for the
    instance (restart REAPER), never to retry — which is exactly what the recorded pitfall warns
    about (M18).
12. **Liveness false alarm.** The heartbeat freezes during a render (M14). A watchdog that declares
    REAPER hung after 30 s of stale pulse will kill a healthy 98 s render.
13. **Batch-split audio drift.** If D2 is adopted without E4, tracks rendered in different calls may
    differ from the single-batch reference by the session's non-determinism floor (M15), silently
    changing the meaning of every downstream comparison.
14. **Mapping assumption.** `$tracknumber` is assumed to be index+1 (`reaper_bridge.lua:1776-1788`);
    if REAPER's track-number wildcard can diverge from the index, 42 unique GUIDs can still be mapped
    to a permutation, and no check would notice (I12).

---

## (g) Experiment protocol and pass/fail criteria

All experiments use a **disposable copy** of a small project under
`$TMPDIR/rp-overlap/proj/` (or `$LOCALAPPDATA/hermes/cache/scratch/rp-overlap/proj/`),
with render output in a sibling `out/` directory that is **outside the project media folder**. The
original RPP is never opened for writing. All runs are on an isolated REAPER instance with its own
`bridge_session_id`; all experiments are to be executed by whoever owns the live instance, not by this
report. A scratch Lua probe lives in the REAPER resource `Scripts/` folder, not in the repo.

### E1 — Does `Main_OnCommandEx(42230)` block the defer loop? (REAPER-only)

Setup: 2-track project, 60 s. Probe script (`overlap_probe.lua`) started as a deferred script:

1. `t0 = reaper.time_precise()`; start a `defer` tick that appends `(tick_index, time_precise())` to
   `probe.log` and re-arms every cycle.
2. On the first tick, set `RENDER_FILE`, `RENDER_PATTERN="probe-$tracknumber-$track"`,
   `RENDER_FORMAT="evaw"`, `RENDER_SETTINGS=2` (stems only), `RENDER_BOUNDSFLAG=1`,
   `RENDER_CHANNELS=2`, `RENDER_SRATE=0`, `RENDER_ADDTOPROJ=0`.
3. Read `RENDER_TARGETS` (record the exact string and count).
4. `tb = time_precise()`; `reaper.Main_OnCommandEx(42230, 0, proj)`; `ta = time_precise()`.
5. Record `ticks_during = count(tb < t < ta)`, `rendered = {exists, size} for each target`,
   and `RENDER_STATS` read **without** a value argument (`GetSetProjectInfo_String(proj,
   "RENDER_STATS", "", false)`).
6. Repeat the whole run with one target pre-created, to observe the overwrite prompt.

Measurements and thresholds:

| Measurement | Pass (blocking) | Fail (non-blocking) |
|---|---|---|
| `ticks_during` | 0 | > 0 |
| `ta − tb` vs independently measured render wall time | ≥ 0.8× | < 0.2× |
| targets present and non-empty at `ta` | all | any missing |
| `RENDER_STATS` before any render in a fresh instance | empty or absent | populated with this run's files |
| `RENDER_STATS` after the render | non-empty, contains the rendered names/lengths | empty |

Notes: `RENDER_STATS` must be read with `""`; passing a value runs an action (S3). If the pre-created
target does not produce a dialog (REAPER may auto-increment), the case is inconclusive, not a pass.
Run the blocking test three times to exclude a one-off; the pass requires 3/3.

### E2 — Completion-invariant validation (positive and negative controls)

After any render:

1. Apply C2 (exclusive open, three times, 250 ms apart), C3 (parse RIFF chunks), C4 (frames), C6
   (two hashes), C7 (mtime window) to every target.
2. **Positive control:** a normal complete batch — all 43 must pass.
3. **Negative control A (cancel):** start a long render, cancel it, answer "keep" to the partial-file
   prompt (S15). The partial file must **fail** C3 or C4 and be quarantined. If it passes, the
   invariant is too weak and the design must not ship.
4. **Negative control B (in-flight):** while a render is writing target *k*, run C2 on target *k*.
   It must fail with a sharing violation. If it succeeds, REAPER is writing with full sharing and C2
   has no discriminating power — the design must fall back to C6 over a quiet period plus C3/C4, and
   D1 becomes unsafe.

Threshold: 43/43 pass in the positive control; both negative controls behave as stated. Any deviation
is a design failure, not a test failure.

### E3 — Progressive closure and preallocation (decides D1)

Setup: 4 targets, 60 s each, one batch, output to `out/`. An external probe (Python `ctypes`, no
bridge) samples every 100 ms: `stat().st_size`, and the result of a share-0 open.

| Measurement | Meaning | Threshold |
|---|---|---|
| E3a first time each target becomes share-0 openable, relative to `tb` and `ta` | does REAPER close per target? | D1 viable if ≥ 2 targets become openable before `ta − 10 s`; D1 dead if all become openable only after `ta` |
| E3b file size trajectory per target | is the file preallocated to final size at creation? | if size jumps to final at open and never grows, size checks are useless and only C2 counts |
| E3c does the header's `data` size claim the full length before the data is written? | can a partial file look complete? | if yes, C3 must compare actual size, never the header's claim alone |

Pass/fail: D1 is approved only if E3a shows progressive closure **and** E3c shows the header cannot
pass C3 for an incomplete file. Otherwise D1 is rejected and D2 is the ceiling.

### E4 — Batch-split PCM parity (gates D2)

Setup: same project copy, same section, same settings. Render the 42 tracks (a) in one batch, as today,
and (b) in 4 batches of 10/10/11/11. Decode and compare per track.

| Metric | Pass | Fail |
|---|---|---|
| Per-track max abs difference | 0.0 FS for deterministic projects; ≤ the session's own native-vs-native floor otherwise (measure it in the same session, as `flamingo-ab-render-proof.md` did) | above the floor |
| Frame counts | identical | any mismatch |
| Event lists / metrics | identical, or differences bounded by the already-documented tolerances | any new field drift |

Report WAV-container hashes separately from PCM equality (the container carries BWF metadata, so
different hashes with zero PCM difference are expected — the repo already documents this).
A fail means D2 must use a single batch and the only available overlap is D1 or none.

### E5 — Post-capture revision and timing accounting

After a capture returns, without any other edit: read `GetProjectStateChangeCount`, then call a
no-op `reaper_project` and compare.

| Measurement | Expected if the `finally` is harmless | Expected if it drifts |
|---|---|---|
| live `change_count` vs `record.revision_after` | equal | +42 (one per track) |
| wall time from RPC return to Python return vs `timings.total_s` | ≈ 0 | ≈ 8–10 s |

Threshold: equality both ways. Any drift requires `revision_after_restore` in the record and a
`total_s` measured after the `finally`.

### E6 — Timeout discrimination (operational)

Start a deliberately long render (> 600 s is not needed; use a 300 s render and a 60 s client timeout
in a test harness). Confirm that: (i) the client raises `BridgeTimeout` with
`details.timeout_s`; (ii) the lock file still exists and its PID is alive; (iii) the heartbeat is stale;
(iv) a second call blocks on `client_lock` and then times out. This proves the only reliable
discriminator is "is reaper.exe consuming CPU and are the target files growing", and that no
API-level discriminator exists.

### E7 — FX-initialisation delay vs silent master (operational, cheap)

Setup: a project with heavy sample-based instruments, loaded fresh (cold cache). Render master-only
with `RENDER_SETTINGS=0`, once without and once with `&(16<<16)`.

| Measurement | Pass for adopting the bit | Reject |
|---|---|---|
| Silent-master rate over 5 cold starts | 0/5 with the bit, > 0/5 without | no difference |
| Added render time | ≤ 2 s on a 10 s master (measure, do not assume) | more than the failure it prevents is worth |
| Peak/RMS equality to the no-bit render on a warm start | identical PCM (bit-exact) | any PCM change |

The bit is documented (S1) as "delay render start to allow FX to initialize and load samples"; this
experiment is the only way to know whether the 13:33 silent master belongs to that class.

---

## (h) What cannot be concluded

1. **Whether 42230 is *guaranteed* to block.** It is measured and third-party corroborated (M17, S20),
   but not documented (S5). A future build, a preference change, or a different render source could
   change it. Any design that relies on the RPC return as the completion signal should assert the
   observed batch duration against a floor (e.g. fail if `ta − tb < 1 s` for a 300 s project).
2. **Which post-archive exception produced the 13:53 wrapper failure.** The response was destroyed by
   the harness. Three code paths are possible; the artifacts cannot separate them.
3. **Whether 42230 always auto-closes.** Auto-close is documented as conditional (S13) and can be
   pre-empted by a modal warning (M18). The record has no field describing the dialog state.
4. **Whether REAPER closes targets progressively or preallocates them.** Not measured; E3 decides it.
5. **Whether the 42 stems map to the right GUIDs.** The mapping is by `$tracknumber`, and no
   independent identity check exists (I12).
6. **Whether the master gate's `all_rendered_sources_silent` branch is reachable by a master-chain
   fault.** Reasoned from S18; not reproduced.
7. **Whether the via-master stems sum to the master.** Requires a PCM experiment; the current record
   only stores per-asset metrics.
8. **The exact split of the 150 s gap.** A gives the total; attribution to archive copy, record
   serialization, panorama and jsonable needs B's instrumentation.
9. **Whether the returned revision is stale.** Depends on whether a no-op `B_MUTE` write bumps
   REAPER's counter; E5 decides it.
10. **Whether batch splitting is PCM-neutral for this project.** E4 decides it; the existing evidence
    (M15) suggests it may not be.
11. **Whether `RENDER_STATS` content is stable enough to key on.** The docs describe it as
    "statistics for the most recently rendered files" and it has changed shape across releases
    (S17, S19); no schema is documented.
12. **Anything about the optimized run's equivalence.** Only 3 of 42 source PCM payloads were
    spot-checked; `capture-parity.json` reports 43/43 container-SHA differences and 337,134 "numeric"
    differences whose maximum (3033.8) is, by inspection of the saved sample, produced by differing
    `analysis.*.source` path strings rather than by audio [INFERENCE: the diff walker appears to
    classify string values as numeric diffs]. A does not certify equivalence and treats the parity
    artifact as contaminated by provenance strings.

---

## Cross-workstream questions for B

| # | Question for B | A's working assumption |
|---|---|---|
| B1 | What is the true cost split of the 150.044 s post-analysis gap (archive copy 3.34 GB, 445 MB record write, panorama, `_jsonable`, MCP framing)? | Archive copy + record dominate; A ignores it in all overlap arithmetic. |
| B2 | With 8 DSP workers already at 92.2 % utilisation, how much does concurrent REAPER rendering slow the pool (CPU contention, page cache, disk)? | Overlap efficiency ≤ 0.75 during the render window; A's 54–81 s estimates are upper bounds. |
| B3 | Is a single long-lived pool across batches cheaper than the current one-shot pool (spawn cost per capture)? | Spawn cost is small relative to 408 s; A assumes pool reuse is neutral. |
| B4 | Does the analysis pipeline need bounded queues when fed batch-by-batch (memory ceiling 47 GB, 3.34 GB of WAVs per capture)? | At most 2 batches of WAVs resident at once; A assumes B will enforce the ceiling. |
| B5 | Can the source-activity pass (17.767 s of redundant full decodes) be folded into the worker results without changing any metric? | Yes: the same `load_audio` + peak/RMS over the same file, computed once. A treats the 17.767 s as recoverable. |
| B6 | What is the per-run variance of `capture_render_s` (98.47 s here vs 108.67 s at 13:33) and does it track plugin warm-up? | ±10 % session-to-session; A uses 98.47 s as the planning number. |

---

## Coordinator reconciliation note (Hermes, 2026-11-13)

Independent recomputation against both sealed records changes two A statements; neither
changes A's design conclusions.

1. **§h.12 is superseded on the parity mechanism.** The 337,134 numeric differences are
   not `analysis.*.source` path strings. The diff walker classifies strings separately
   (`kind:"value"`); the numeric leaves are real and decompose into ≈331,940 master
   band-curve values (max 2.17e-4 dB), 2,824 `short_term_lufs` cells in 23 assets
   (max 3033.8 dB, a −3233.8 → −200.0 denormal-floor artifact), ≈2,332 master
   `rms_400ms` values (max 4.55e-8), and a handful of master metrics. Only the master
   differs outside the silent tails. A's "do not certify equivalence" is correct; the
   mechanism in §h.12 is not. See Workstream B §d.4 and the joint synthesis §2.1.
2. **§b.7's `timings.total_s` is response-only.** `total_s` and `archive_s` are added to
   the returned response (`full_capture.py:582-586`), not to the sealed record. The
   rest of A's point — that the 42-RPC `finally` runs after the response value is
   computed and can discard it — stands.
3. **The 0.1 dB tolerance is not met at capture scale.** It holds for the single 48 kHz
   test WAV; 2,774 cells across 23 assets exceed it, all in silent tails where gating is
   unaffected. This is a Phase 0 correctness item, not a reason to reject A's design.

---

## Sources that could not be verified

* **Cockos forum threads** — `forum.cockos.com/showthread.php?t=286900` ("Reaper Extension API —
  Rendering"), `t=213212` ("How to use Defer?"), `t=264054` ("Close THIS Window after render") — all
  returned an HTTP 403/bot-verification interstitial on 2026-11-13 from both the plain fetch and a
  real browser session. Their snippets (visible in search results) are consistent with the findings
  above but are **not** cited as evidence.
* **`Main_OnCommandEx` blocking semantics** have no official statement; S5 is silent, and no Cockos
  post could be retrieved to close the gap.
* **`RENDER_STATS` payload schema** — no official example of the string was found; only the changelog
  notes that it now includes file length (S17).
* **The exact 42230 action ID → name mapping** is asserted by the runtime check in this repo
  (`reaper_bridge.lua:1749`, M12) and contradicted by one third-party table (S21); no official
  action-ID table is published, so the runtime check remains the only authority.
