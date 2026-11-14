# Full Capture — status and failure contract

A full capture is one immutable record linking the project map, one audio asset
per project track plus the master, and one analysis report per asset.

## Master render gate

The master-only render is validated before a capture is accepted. The gate is
silence-aware: a silent master is accepted when the capture evidence explains
it, and retried/failed only when the evidence says it is unexpected.

- `timings.master_attempts` is a chronological list of every master render
  attempt, including failed attempts. Each entry contains at least `attempt`,
  `status`, `silent`, `valid`, `expected_silence`, `unexpected_silence`,
  `silence_reason`, `source_activity`, `peak_dbfs`, `rms_dbfs`, `frames`,
  `sample_rate`, `channels`, `sha256`, `archive_audio`, `render_s`,
  `render_settings`, `time_window`, and `selected`.
- Expected silence is classified from the rendered source stems and captured
  REAPER state, not by blanket rejection. The reasons are
  `all_rendered_sources_silent` (including an intentionally silent requested
  section), `all_project_tracks_muted`, `master_muted`, and
  `master_zero_volume`. Such a capture has `capture_status=valid_expected_silence`
  and `master_validation.status=expected_silence`; `valid=true` means the
  silence was explained.
- If the master is silent while at least one rendered source is audible and the
  captured master state is not muted/zeroed, the silence is unexpected. The gate
  makes at most two attempts. A signal on either attempt selects that master;
  `capture_status` is `valid` or `valid_after_retry`.
- Every unexpected failed attempt is copied into the archive
  content-addressed store and is verified by `reaper_capture_read` when a valid
  exact-capture receipt is supplied. It is never silently deleted or replaced.
- If both attempts are unexpectedly silent, the invalid capture is still
  archived as chronological evidence, but the call fails with structured error
  `capture_master_invalid`. `ok=true` is never returned for an invalid master.
  The error details include `capture_id`, `record_path`, `master_validation`,
  and `master_attempts`.


`reaper_capture_read` (with a complete receipt for the exact capture) verifies the
sealed record integrity and the SHA-256 of both normal audio assets and any
preserved master-attempt evidence.

## Render identity

Every accepted capture stores `project_map.render_manifest` and
`project_map.digest_facts` inside the sealed record. The manifest gives each
rendered source and the master a `render_key` computed from the signal path:
track state (volume, pan, pan mode/law/flags, width, dual pan, phase, mute/solo,
main send, folder depth), normalized item state chunks, item and take geometry,
FX identity/order/bypass/parameters plus serialized FX-chain state, automation,
sends and hardware outputs, folder ancestry, tempo/project settings, and the
master chain for every stem because stems render via master. Each audio row also
carries a compact `render_dependencies` digest so the panorama can name why a
key is authoritative or incomplete.

A manifest can be incomplete: an unreadable item chunk, FX chain or digest
coverage gap adds an `unknown` reason and makes that source row non-authoritative.
The manifest also declares, in `unverified`, dependencies it does not hash:
media-file bytes on disk, external realtime input, the project default pan law
(per-track `D_PANLAW` is read, but REAPER exposes no documented numeric key for
the project default), and the loaded plugin binary version, plus track routing
flags the numeric digest does not expose. A later mutation
delta compares this manifest against a freshly built one and invalidates every
source whose key changed; when either manifest is incomplete it invalidates all
project audio instead of guessing. A manifest can be structurally complete and
still declare the unresolved classes above; because they are not mapped to
individual sources, a cross-capture comparison is non-authoritative whenever
they are present and every old-revision source becomes stale. The fresh capture
itself remains current evidence for its own revision; this uncertainty only
concerns whether that evidence survives into a later revision.

## Analysis worker policy

Both production paths (`FullCaptureService.capture` and
`ProjectSession.analyze`) use one selector in
`src/reaper_audio/daw/analysis_policy.py`. The hard production cap is **8**
workers; 12 and 16 are benchmark-only and are not a production mode.

- A measured capture window of 30 s or less defaults to up to 8 workers,
  bounded by asset count and available memory.
- A full/long capture (over 30 s) defaults to **4** workers.
- An unknown duration is treated like a full capture and defaults to **4**.
- The memory envelope is conservative: 384 MB + 14 MB per second of window
  per worker, with unknown duration charged as 300 s, and only 70 % of
  currently available physical memory is budgeted. Short 20 s assets fit;
  full 300 s assets are estimated at about 4.6 GB per worker.
- An explicit `analysis_workers` override is honored only inside the hard cap
  and the memory-safe limit. If it is unsafe, the call fails before any
  process starts with a structured `validation_error` whose details include
  `requested`, `max_safe_workers`, `per_worker_envelope_mb`,
  `memory_budget_mb`, `window_s` and `window_class`.
- The selected count and its evidence are recorded in
  `timings.analysis_worker_policy`.

This policy exists because measured 20 s slices reached the practical knee at
8 workers (3.36x over one worker), while 12/16 added only 9 %/3 %. For a full
300 s capture, one worker measured 3.96-4.29 GB peak RSS; eight such workers
already approach the 34.9 GB available on the test machine, so eight is not a
safe default and sixteen is unsafe. The short-slice and full-capture memory
envelopes are deliberately kept separate.

## Capture panorama (one archived capture, model view delivered in pages)

Every successful capture also writes `capture-archive/captures/<capture_id>/panorama.json`.
The sidecar is derived from the sealed record and is written atomically after the
canonical `record.json` is committed. It never replaces or rewrites the raw
record. `reaper_capture_snapshot` creates the archive document and returns
capture/archive metadata only. `reaper_panorama_open` returns page 1 of the
revision-bound model view plus the manifest of all pages; fetch each page with
`reaper_panorama_page` and acknowledge the ordered hashes with
`reaper_panorama_ack`. The receipt proves delivery of every page of that exact
view and revision, not delivery of every raw audio point, model comprehension,
or direct listening. `reaper_capture_read` is a separate archive read that
requires a complete receipt for that exact capture; it does not fall back to a
latest-capture receipt. For an older capture the deterministic view is built in
memory and the archive is left untouched.

The model view is a columnar projection of the archived panorama: shared units
and contracts are stated once, measured scalar metric families are declared,
and dense detail rows are catalogued with counts, intervals and content hashes.
The projection is complete for its declared coverage but is not a lossless dump
of every raw measurement. Dense time series are summarized in the view and linked
to exact rows in the canonical record. `reaper_panorama_query` retrieves a whole
semantic selection (objects, interval, family, series), or reports
`query_too_large`; it does not page. Pagination preserves the model view exactly:
`reaper_panorama_open` returns page 1 and a manifest, `reaper_panorama_page`
serves each remaining page, and `reaper_panorama_ack` verifies page hashes before
minting the receipt. A section larger than one page is split at list-element
boundaries with its head repeated; the page operation does not truncate the view.

The model-facing view joins these domains and their declared coverage; the
canonical capture record remains the source of exact archived values:

- `capture_binding`: `capture_id`, project/path, record schema and integrity
  SHA-256, revision before/after, and the exact capture time window.
- `project`: `project_extent` reported by REAPER and a separately derived
  `content_extent`; `capture_window` in project seconds; counts; `render_tail`.
- `time_map`: revision-bound REAPER tempo points and time signatures. Musical
  coordinates are not inferred from a single base BPM.
- Declared domains: `tracks`, `master`, `items`, `takes`, `midi`, `fx`, `routes`,
  `automation`, `envelopes`, `tempo`, `time_signatures`, `markers`, `regions`,
  `selection`, `transport`, `audio`, and `analysis`.
- Each audio asset has exact `frame_count`, `sample_rate_hz`, `channel_count`,
  `duration_s`, a file-local `audio_file_window`, a project `capture_window`,
  and explicit file-time/sample-index ↔ project-time formulas. A render tail is
  a separate interval and never extends `capture_window` or the project extent.
- Each audio row carries `render_provenance`: capture method, exact render
  stage, capture-revision change count and SHA-256 fingerprint, solo source,
  and an explicit completeness flag. This distinguishes selected-track
  post-FX renders from the post-FX master render. A valid older sidecar that
  lacks this contract is rebuilt from the sealed record in memory; reading it
  does not rewrite the archive.
- `crosslinks` joins every object by stable IDs, and `per_track` is a compact
  per-track index. The model should not need another call to resolve GUIDs.
- `epistemic_layers` separates project facts, signal measurements, model
  interpretations, causal hypotheses, and uncertainty. The builder leaves
  interpretations and hypotheses empty and does not invent roles or genres.
- `coverage.domains` reports expected/observed counts and `partial`, `missing`,
  or `truncated` status for tracks, items, takes, MIDI, FX, routes,
  automation, audio, and analysis. Active-take-only and the MIDI limit of
  10,000 events are explicit; a truncated capture is never labelled complete.
- Dense curves are deterministic multi-resolution summaries with half-open
  bucket boundaries (`[start_s,end_s)`), units, aggregation method, source point
  count, raw-series SHA-256, canonical `record.json` pointer, and
  `raw_series_included=false`. The raw series remain in the canonical record;
  they are not silently dropped and are not returned as hundreds of megabytes.
  For large captures the per-source RMS envelope is a columnar
  `analysis_temporal` table: `source_ids` covers every analysis object, and
  each level documents `bucket_width_s`, `columns`, and rows of
  `[source_index,start_s,end_s,count,min,max,mean,p50]`. The exact raw series
  remains in the canonical record. `domain_counts`, `coverage`, and
  `truncation` make missing or truncated domains explicit.

The archived panorama builder has a default budget of 700,000 compact JSON bytes
and a conservative estimate of 256,000 tokens; it checks the budget before
writing the sidecar and fails with `panorama_capacity_exceeded` rather than
silently dropping archive fields. The separately derived model view is delivered
in pages. On the archived 41-track / 42-source fixture, the view is 46,977
compact bytes and takes 5 pages (maximum measured page: 38,084 characters under
the configured 46,000-character limit). A 101-source synthetic fixture takes
12 pages (maximum 45,185 characters). The page counter measures the character
length of the server's indented JSON rendering, not UTF-8 wire bytes or the full
MCP transport envelope; actual host limits still need validation. The page count
is not capped. A sidecar whose capture ID, record integrity SHA-256, revision,
time window, or document hash does not match the canonical record is rejected as
`panorama_binding_invalid`.

The `panorama.json` sidecar uses schema
`reaper-workbench.capture-panorama/2`; the derived model view uses
`reaper-workbench.panorama-view/2`. Shared meaning is stated once: relation
kind blocks carry the interpretation, method, units, caveats, alternatives and
coverage rule; the relation set carries one window/stage/node/channel view and one
provenance record; the audio contract carries the file/sample/project mapping
formulas; the analysis contract carries metric units and method. Every row keeps
its own exact endpoints, values, band, coverage and stable ID. There is no alias
legend and no positional metric array: `analysis[].metrics` is a named object and
`crosslinks[].relation` is the literal link name.

MIDI notes, envelope/automation points and exact analysis cells are not reprinted
inline. Each owning row carries a navigational summary (owner chain, time basis,
occupied interval, count, ranges, truncation, coverage) plus a typed entry in
`details.entries` whose `fetch` object is the exact request for
`reaper_panorama_query` by its typed request. The entry is bound to this capture, project, revision
and record integrity, and carries the exact `content_sha256` of the stored rows.
Fetching recomputes that hash from the canonical record and fails closed on any
mismatch, so a linked reference is an exact address, never a lossy substitute.
A document written under the previous schema is upgraded in memory from the
canonical record on read; the archived sidecar is never rewritten.

## Delivery and restoration

The production render path is the selected-track batch with
`RENDER_SETTINGS=524416` (128 plus REAPER's parallel-via-master bit), followed
by a separate master-only render. The non-parallel `128` path was measured but
is not equivalent to the archived production audio and must not be substituted.

Restoration verifies the edit cursor and stopped transport state. `GetPlayPositionEx`
is an observed playback position, not a restorable project cursor; while stopped
it is reported as diagnostic (`play_position_matches=null`,
`play_position_restorable=false`) and is not used to fail restoration.
