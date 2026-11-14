# Clean-session evaluation log — panorama lifecycle and completion gate

> **Historical record (superseded transport).** This document describes the
> paged panorama contract that has since been replaced by one whole-view result.
> Current behaviour: `docs/DECISION-single-response-panorama.md` and
> `docs/MCP-README-LLM.md`. Treat this file as history, not instruction.


Date: 2026-11-13
Scope: disposable REAPER project only. No user project was edited or saved.

## Starting state (audited, not rebuilt)

The MCP server already exposed 21 tools, a bundled `api_catalog.json` with 732
documented REAPER v7.85 signatures, structured `reaper_api_search`, domain
indexing through `reaper_capabilities`, and generic `reaper_api_call`. Those
components were left intact. The implementation added eight lifecycle tools and
gating around the existing revision/handle checks; it did not duplicate the
catalog or replace the performance work in the dirty tree.

- Before: 21 tools, 5 resource templates.
- After: 29 tools, 5 resource templates (original 21 unchanged).
- New tools: `reaper_panorama_open`, `reaper_panorama_page`,
  `reaper_panorama_ack`, `reaper_panorama_status`,
  `reaper_panorama_changes_since`, `reaper_panorama_delta_ack`,
  `reaper_panorama_refresh`, `reaper_panorama_slice`.
- `reaper_panorama_ack` now returns a `context_token`; `delta_ack` advances that
  token so a second write after an edit does not require replaying pages.

## Code and contract changes

- `src/reaper_audio/context/lifecycle.py`: deterministic UTF-8 page split,
  server-recorded page fetches, page/document hashes, complete-view receipts,
  exact-base deltas, dependency invalidations, refresh and interval slice.
  `changes_since` rejects a base that is not in the same archive
  (`stale_base/base_not_in_archive`) instead of guessing.
- `src/reaper_audio/mcp_server.py`: eight lifecycle tools; `reaper_edit`
  apply/undo, `reaper_api_call`, `reaper_action` invoke and `reaper_save`
  require `panorama_receipt` plus the exact expected revision. Internal
  read-only API calls used by timeline/analysis helpers bypass the model-facing
  gate through a thread-local, not a public escape hatch.
- `src/reaper_audio/context/panorama.py`: content-derived `time_map` identity;
  take `track_id` recovery; FX raw-pointer owner index.
- `src/reaper_audio/mcp_server.py` capture readback: `capture_status`,
  `invalid_reasons`, `master_validation`, `coverage`, `missing`, so an archived
  invalid render is not presented as complete evidence.
- `scripts/reaper_bridge.lua`: digest now includes `channel_count` and
  `pan_mode`; no project-memory or Frida path was introduced.
- `docs/MCP-README-LLM.md`, `docs/MCP-README-LLM.ru.md`, `README.md`,
  `README.ru.md`: lifecycle, gate, delta/archive rule, anti-patterns and tool
  counts. The guide remains readable with ordinary file tools; no client
  context hook is required.

## Measured delivery

Real 7-track disposable project (`eval-session.rpp`, 7 tracks + master, ReaEQ
attempts, four sends to `Guitar Bus`, tempo 120→140 at 10 s):

- Panorama canonical document: **69,420 bytes**.
- Pages at 24,576-byte payload target: **3**; MCP response sizes
  **30,821 / 28,116 / 23,661 chars** including page envelope and page 0
  manifest.
- Full archive document from `cap_01M7AADBXY4XCH11E3WZTKA3BT`: **241,342
  bytes → 10 pages**. The 30 s temporal level alone is 50,567 B, so a single
  full response does not fit Hermes's 50,000-char spill threshold or Codex's
  approximately 40–48 KB effective MCP budget.
- Page 0 is a manifest plus one page, never the whole document. A spill preview
  is also not the document.

Hermes host facts measured earlier and relevant here: MCP results above 50,000
chars spill to a 1,500-char preview plus a path; the absolute inline ceiling is
100,000 chars; the host estimates 4 chars/token and does not use Music Flamingo's
tokenizer. A 30 KB page is well below both thresholds. Codex's declared
tool-output limit is 10,000 tokens with a ~272K window; the same pages fit under
that profile, but a Codex live test with this exact server was not run.

## Clean-session trials (fresh `hermes -z` processes)

The nested session was started after adding a separate `reaper-panorama` MCP
entry pointing at this repository and the 29-tool server. It did not inherit the
implementation transcript. The first discovery trials used the profile default
(`upstage/solar-pro4`); the required target model
(`deepseek/deepseek-v4.1-flash-1017`, Ultra reasoning, RouterAI custom provider)
was then used for the measured page and edit runs below.

### Trial 1 — open, fetch every page, acknowledge

Prompt: open the disposable project from `archive2`, fetch all pages, ack, and
report the page set and track facts without editing.

Result:

- `page_count=3`, `document_hash=85154ff102e2af7e92fb8bd581915f6d27b73fbb5662632fcf3c103cd0b2344b`
- `fact_root=b0a00f49a468a837419d8370ab942e623d6b705a9eeab2aeff24007604bcccf7`
- `pages_received=[0,1,2]`, receipt `pr_WtQhoXp6NO4vakQ29VTvTRr4-kFmclLo`
- Track table read from the panorama: Gtr L 1 −1.0/0.45; Gtr L 2 −0.8/0.70;
  Gtr R 1 +0.8/0.70; Gtr R 2 +1.0/0.70; Bass 0.0/0.80; Drums 0.0/0.80;
  Guitar Bus 0.0/0.80; master 1.00. Four guitar sends to Guitar Bus; tempo
  120→140 at 10 s.

This trial proves the model can discover the lifecycle tools, fetch the full page
set, and mint a receipt through ordinary MCP tools.

### Trial 2 — gated edit, readback, capture, delta

Prompt: repeat open/ack, change only `Gtr L 1` fader from 0.45 to 0.50 using
`reaper_edit` preview/apply with the receipt, read back, recapture to `archive3`,
and call `changes_since`.

Result:

- Receipt `pr_zxRH9qaLHUJJ7NNIbLNbWd0fEMe_wHSJ`, all 3 pages.
- Track GUID `{29551CA6-9DE9-435F-B611-3A38043BF142}`; preview and apply
  succeeded; `applied=true`, `changed=true`, `undo_available=true`.
- Readback at change_count 114 confirmed volume `0.5`.
- Capture `cap_01M7B00DNYP63J6Y1A8NPXZZBD`, status `valid`;
  capture revision change_count 163.
- The first delta call used `archive3` with a base capture that existed only in
  `archive2`; the server returned `stale_base` (then labelled `wrong_project`)
  and the session correctly did not invent a delta. This exposed a reason-label
  bug; it now reports `stale_base/base_not_in_archive` and tells the caller to
  refresh or use a base from the same archive.
- With the exact base capture copied into `archive3`, the delta returned
  `status=delta`, `336` ops, one `audio_and_analysis` invalidation for
  `/tracks/0/mixer/volume`, and `0.45 → 0.50` as the only fader change. The
  large op count is the measured analysis-curve response to that fader change,
  not a hidden project edit.

### Trial 3 — DeepSeek V4.1 Flash, measured page load and gated edit

The target model was run through the RouterAI custom provider with Ultra
reasoning and `--usage-file`.

Page load (`archive3`, latest capture `cap_01M7B00DNYP63J6Y1A8NPXZZBD`):

- 3 pages fetched and acked; document hash
  `b7033b0a32e60d7fb221b3fe18ef427564cd45f58a8bec9dbc90dbfd22524d72`.
- Usage: **191,262 input tokens**, **161,920 cache-read tokens**, 1,816 output,
  **5 API calls**, total 193,078 tokens, estimated $0.41. This is cumulative
  usage across calls, not one context window; it shows the repeated-context cost
  of a multi-call page load.

Gated edit (`archive3` → `archive4`):

- Receipt `pr_9FQL3OmiM1GDGXdk1wDDq1D1XtHHfwxF`; page hashes
  `0879e204…`, `a2817662…`, `90ab094b…`; revision change_count 163.
- `Gtr L 1` GUID `{29551CA6-9DE9-435F-B611-3A38043BF142}`, before volume 0.5,
  one ReaEQ slot and one send to `Guitar Bus`.
- Preview planned 0.5→0.55; apply returned `applied=true`, `changed=true`,
  revision 163→164, undo available. Readback returned 0.55 at revision 164 and
  all other track volumes unchanged.
- Capture `cap_01M7B0RJSF9AF7VQZQNHWCWSTE`, status `valid`, 8 assets, master
  peak −2.44 dBFS / RMS −11.66, signal present. Capture itself advanced the
  project to change_count 213.
- Measured corroboration: `Gtr L 1` render RMS moved from −21.83 dBFS at 0.5 to
  −21.00 dBFS at 0.55, **+0.83 dB**, matching `20*log10(0.55/0.5)=+0.83 dB`.
  This is a rendered-signal measurement, not listening.
- Usage: **958,452 input tokens**, **898,432 cache-read tokens**, 8,588 output,
  **16 API calls**, total **967,040 tokens**, estimated **$1.37**. This is close
  to a 1M-token budget in cumulative terms; page count and call count must be
  budgeted, not assumed free.
- Delta with the exact base `cap_01M7B00…` copied into `archive4`:
  `status=delta`, **115 ops**, one `audio_and_analysis` invalidation for
  `/tracks/0/mixer/volume`, and `0.5 → 0.55` as the only fader change.
- Continuity token check on the same archive: full ack at the 0.5 capture →
  `changes_since(context_token=…)` → `delta_ack` minted a `ct_…` successor at
  the 0.55 capture; `require_receipt(successor, target_revision)` passed. The
  unit suite also runs two edit→delta→ack cycles without replaying pages.
- The clean DeepSeek trials ran on the 28-tool build (before `delta_ack` was
  added). The 29th tool and the two-cycle state machine are covered by the
  lifecycle tests and the direct real-archive check above, not by that specific
  model session. A new session must be started after the gateway reloads MCP
  tools to see all 29.
- Stock MCP transport check: `hermes mcp test reaper-panorama` connected and
  discovered **29 tools**, including `reaper_panorama_delta_ack`. A plain MCP
  stdio client then called `reaper_panorama_delta_ack` and received
  `is_error=false`, `complete=true`, `idempotent=true`, with the successor token
  `ct_N4dFdTjsb5DGuql2TqD0NMwJui8U-c6q`.

### Trial 4 — two real mutation → delta → ack cycles without page replay

On the same disposable 7-track project, with `archive5` as one archive:

- Baseline capture `cap_01M7B2B09SQX5P6ECDAWF1GP2M`; full pages/ack minted
  `ct_-LFxTaN`.
- Cycle 1: `reaper_edit` preview/apply changed `Gtr L 1` 0.55→0.60
  (revision 52→53); capture `cap_01M7B2B61KWV46HH85VFD87DAX`; exact delta
  `65` ops, one invalidation; `delta_ack` minted `ct_N4dFdTj`.
- Cycle 2: the successor token authorized a second preview/apply 0.60→0.65
  (revision 102→103); capture `cap_01M7B2BBRD99EMZ181JERGPB1K`; delta `64` ops,
  one invalidation; `delta_ack` minted `ct_uhy0kAj`.
- `require_receipt` with the second successor and the target capture revision
  returned complete. No `reaper_panorama_page` call was made after the initial
  full acknowledgement.

The first `require_receipt` check in the scratch driver used the pre-capture
revision and correctly failed as stale; the target context token's capture
revision is the one the gate accepts.

### Capture diagnostic — inactive project tab

The observed `REAPER could not build the analysis render target list …
RENDER_SETTINGS=524416.0` was reproduced with the isolated 41-track copy while a
different project tab was active. `524416` is the batch bitfield (`128 + 8<<16`),
not a corrupt value. REAPER's render-target/settings resolution was being read
against the active tab; the target project was open but not active.

Fix in `scripts/reaper_bridge.lua`: `project.capture_render` records the active
project, calls `reaper.SelectProjectInstance(target)` for the render, and
restores the previous active tab in its existing restore path. Final re-test
with the copy inactive: capture `cap_01M7B3VD4PNYACF77DBBVJP2ZB` completed
(`valid_expected_silence`), and the previous active tab was restored afterwards.
This removes the target-list failure; a fully valid 41-track audio capture still
depends on that copy's source/state and was not used as an audio-quality test.

### What the clean session did not prove

- Human listening and a subjective quality verdict were not performed and must
  not be invented.
- Codex was not driven live against these 24 KB pages; only its documented
  limits were checked.
- The 3-page DeepSeek run used ~193K cumulative tokens for load/ack and ~967K
  for load+edit+capture. A 10-page whole-archive load was not run with the target
  model, so the practical page budget at 10 pages remains a measured-next-step,
  not a proven free operation.
- The 41-track reference copy's target-list failure is fixed and its capture now
  completes while inactive, but that copy returned `valid_expected_silence`; a
  fully valid 41-track audio capture with working sources remains unproven.
- Track-level FX was not a gap: the captured panorama contains six track
  `ReaEQ` slots plus master `ReaLimit`; `Guitar Bus` intentionally has no FX.
  The clean session's "bus has 0 FX" statement is accurate.

## Acceptance status

Passed in this run:

- Partial page sets do not mint a receipt.
- A complete page set and all page hashes are required before mutation.
- Missing/stale/mismatched receipt and revision are rejected.
- The clean session fetched all pages before editing and used preview/apply.
- Readback confirmed the intended fader value.
- Delta is bound to an exact base; a cross-archive base is refused rather than
  guessed.
- Invalidations mark rendered evidence stale when the fader dependency changes.
- The target DeepSeek V4.1 Flash session fetched all pages before editing,
  used preview/apply, read back 0.55, recaptured, and produced a measured
  +0.83 dB render change consistent with the fader ratio.
- Two real mutation → delta → ack cycles on one archive completed without
  refetching the full panorama; the successor token authorized the second write.
- Inactive-project capture target-list failure is fixed and the previous active
  tab is restored after capture.
- Readback exposes invalid-master and coverage status.

Still open:

- Full 732-function call coverage and edge cases per domain.
- Codex live page sequence and the 10-page target-model token/page budget.
- Owner listening and final audio acceptance.
- A fully valid 41-track audio capture with working sources; the current copy
  captures but returns expected silence.
