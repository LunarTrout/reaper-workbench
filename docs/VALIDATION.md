# Validation status

This page distinguishes implementation that has been exercised from coverage that is still open. It is not a release certification.

## Confirmed

- The Python unit suite passes against the recorded source state: **1049 passed,
  5 skipped, 1 existing SciPy warning, 105.41 s** (2026-11-14) with Python 3.13.
  PowerShell command: `$env:PYTHONPATH="src"; python -m pytest -q`.
  The count grows with the corpus module; treat the run, not this line, as the
  source of truth. This covers unit/mock contracts and the archived-fixture
  lifecycle/MCP paths; it does **not** prove a live REAPER capture, a live stdio
  MCP session, or a production mix.
- The paged project view is covered end to end on the archived capture
  (41 tracks / 42 sources): the canonical document measures 1,378,352 bytes, the
  model view 46,977 compact bytes, and the whole view is delivered in 5 pages
  (max 38,084 delivered characters) under the 46,000-character page cap, with
  `reaper_panorama_ack` reconstructing the exact `view_sha256`. The view keeps
  all 42 mapped source rows, all 24 scalar metric columns with
  units/decimals/window/stage provenance and per-metric absent counts, both
  declared time levels, all relation kinds, and a complete per-track
  `content_index`; item/take/audio rows are explicitly catalogued and returned
  whole by one exact query. Inline
  scalar values are rounded: `analysis.precision.lossless` is false and the
  canonical unrounded values are returned by `reaper_panorama_query` (for
  example crest 19.031398871… vs inline 19.031). The view is delivered in
  measured pages (open → page → ack): a query without the acknowledged receipt
  fails closed, and an exact selection either arrives whole or returns
  `query_too_large`; exact series cells carry a manifest hash that is verified.
- The MCP server completed a stdio handshake, advertised 29 tools and 5 resource templates, and answered health and project-context requests against REAPER 7.84.
- The model panorama is delivered in measured pages: `reaper_panorama_open` returns page 1 plus a manifest, `reaper_panorama_page` serves each remaining page, and `reaper_panorama_ack` verifies every page hash and reconstructs the view before minting the receipt. On the archived 41-track / 42-source capture the view is 46,977 compact bytes and needs 5 pages (max 38,084 delivered characters); the 101-source synthetic fixture (80,717-byte view) needs 12 pages (max 45,185). The page count is not capped; each page's delivered JSON text is measured against the 46,000-character cap.
- A live MCP analysis request captured a five-second window for 11 track sources and the separate master, attached project-tempo context, and returned a releasable analysis record. `reaper_release` removed the associated temporary audio directory.
- A revision-bound item split was exercised against an isolated REAPER copy; apply changed the item count as planned and undo restored the original item count and geometry.
- Native capture stores temporary render files outside the project file. The capture restores project render settings and selection; REAPER's monotonic change count may nevertheless advance, and the bridge returns the post-capture revision.
- The identity/freshness layer is covered by focused unit and tool-path tests: incomplete instance/epoch fails closed, a pan edit changes exactly that track's and the master's known render key, an acknowledged delta advances the structural token, a second structural edit applies without a new capture, and project-target analysis with stale audio evidence is refused with a capture next action. Tests also prove that two otherwise-identical manifests with unresolved dependency classes produce a non-authoritative, non-unchanged result, while a fresh capture remains `current` for its own revision. These tests use local fixtures and a fake session; they do not prove live REAPER behavior.
- On the disposable six-track fixture (REAPER 7.84, section 5–6 s plus 250 ms tail), native render and accessor capture both produced nonzero audio: source files peak -14.89 dBFS / RMS -17.91 dBFS; the render master -1.71 / -12.32 dBFS; rendered stems -13.27 to -17.29 / -20.75 to -25.02 dBFS. The empty Guitar Bus accessor asset is explicit digital silence, not a failed capture. REAPER 7.84 on this host can report take media as unresolved while the application is inactive (dirty stays 0); the live gate requires every unmuted take source to be online and fails closed with the exact source rows when it is not. It never activates REAPER, toggles media, or replaces a source. The bridge rejects unresolved media with `media_unresolved` rather than rendering silence. Accessor sampling calls `AudioAccessorUpdate` before reading and emits explicit zero blocks only for truly empty accessor ranges. No take source was replaced. Render settings and the digest fingerprint matched before and after.

## Still open

- `reaper_render_candidate` has focused unit and MCP transport coverage against a fake bridge (plan/revision mismatch, stale project, exact interval, master FX/routing provenance, lease release, over-budget, cleanup and unsupported-render refusals, failed restoration, content-digest change, counter advance and no isolation gate): `PYTHONPATH=src python -m pytest -q tests/test_render_candidate.py tests/test_mcp_render_candidate.py tests/test_mcp_server.py` -> 31 passed. It has **not** been exercised against a live REAPER master render: no `REAPER_WORKBENCH_RENDER_COPY` fixture was declared in this environment, so the live-vs-mock boundary is still open. Do not call it production-ready until a live master render is compared with a normal REAPER render of the same interval/settings/revision.
- Exercise the entire edit transaction through MCP transport against a live REAPER session: preview, apply, factual delta, delta acknowledgement, second apply on the advanced token, and undo with its own delta.
- Confirm on a real capture that the bridge digest covers every render dependency the manifest claims, that item state chunks are read for every item (including items without a take), that a re-capture at identical content yields identical render keys, and that REAPER 7.84 returns the newly read track/send fields (`D_PANLAW`, `I_PANLAW_FLAGS`, `D_WIDTH`, `D_DUALPANL/R`, `B_PHASE`, `B_MAINSEND`, `D_PLAY_OFFSET`, `I_PLAY_OFFSET_FLAG`, `I_FREEZECOUNT`, send `D_PANLAW`/`I_SRCCHAN`/`I_DSTCHAN`/`I_MIDIFLAGS`) with the expected types.
- Media-file bytes, plugin binary versions, the project default pan law and unexposed track routing flags are declared unverified, not hashed. A file or plugin replaced under the same path/identity can change sound without changing a known render key; this is why cross-capture freshness is conservative.
- Confirm that a real MCP client can consume the audio resource format and deliver the audio to its model in the expected way.
- Expand live checks across the typed edit domains, especially FX, sends, MIDI, automation, project save, track/item lifecycle, and failure recovery.
- Verify capture signal stage for representative routing, FX chains, track/master outputs, tails, and REAPER versions. Pre-FX AudioAccessor output must remain distinct from rendered outputs.
- Validate the install and bridge lifecycle on a clean Windows setup, then define support for other operating systems.
- Complete project version management and confirm the intended original/final retention behavior.
- Audit dependency and bundled-file licensing, select a repository license, and run clean-environment packaging and CI checks.

## Release gate

Do not call this production-ready until the clean install, MCP lifecycle, representative project operations, capture provenance, cleanup, and recovery behavior have passed reproducible checks. A passing unit suite does not establish full ReaScript API coverage or correct musical decisions.

The live integration checks use isolated REAPER sessions. They do not touch a user's active production project. This page omits local project paths and media names by design.
