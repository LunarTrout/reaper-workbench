# Capability coverage audit — existing 21-tool surface plus lifecycle

> **Historical record (superseded transport).** This document describes the
> paged panorama contract that has since been replaced by one whole-view result.
> Current behaviour: `docs/DECISION-single-response-panorama.md` and
> `docs/MCP-README-LLM.md`. Treat this file as history, not instruction.


Date: 2026-11-13
Status: audit, not a claim that every catalog function was executed against a live
REAPER instance.

## Starting state

- The server already registered **21 tools** and **5 resource templates**.
- `api_catalog.json` contains **732 documented signatures** generated from the
  official REAPER v7.85 reference, with source version and content hash.
- Discovery is three-layered: `reaper_capabilities` (domain index/paging),
  structured `reaper_api_search` (object/operation/qualifiers/kind/detail/limit),
  and generic `reaper_api_call` (exact positional values).
- Typed tools cover project/session context, integrated analysis, mix evidence,
  edit-map evidence, transaction edits, Actions, save, release, spatial analysis,
  Music Flamingo, timeline context, capture snapshot and capture read.
- The implementation did **not** add one tool per ReaScript function and did not
  replace the catalog. It added eight panorama lifecycle tools, bringing the
  total to **29**.

## What is actually covered

| Domain | Path | Evidence |
|---|---|---|
| Session/instances/projects | `reaper_session`, `reaper_project` | live with three projects during audit |
| Capability discovery | `reaper_capabilities`, `reaper_api_search` | 732-signature catalog tests; structured card parsing |
| Generic API dispatch | `reaper_api_call` | signature-count validation, host-handle conversion, revision binding, runtime availability check |
| Actions | `reaper_action` | search + main section invocation + MIDI editor section 32060 |
| Edits | `reaper_edit` | preview/apply/undo, plan cache, exact post-apply revision, 256-operation bound |
| Save | `reaper_save` | save_current/save_as, overwrite refusal |
| Capture | `reaper_capture_snapshot`, `reaper_capture_read` | full project map, per-track/master render, analysis, durable archive; readback validity now visible |
| Analysis/comparison | `reaper_analyze`, report resources, `reaper_mix_evidence`, `reaper_edit_map` | integrated DSP suite; reference comparisons carry identity/window/method |
| Listener/spatial | `reaper_spatial_status/analyze/timeline`, Flamingo tools | GPU-gated; not exercised in this audit |
| Timeline | `reaper_timeline_context` | reads items/markers/tempo around a window |
| Panorama lifecycle | eight `reaper_panorama_*` tools | live clean-session open/page/ack/edit/readback/capture/delta |

## Edge cases that matter

- **Host handles**: `reaper_api_call` requires typed `$reaper_handle` or
  `$reaper_ref` for host-object parameters; opaque handles are bound to
  project identity, change_count and bridge session. A handle cannot be
  transferred across REAPER instances.
- **Revision binding**: a call may pass `expected_revision`; the bridge rejects
  a project whose change counter moved. The panorama receipt adds a second,
  content-root gate before mutating.
- **Runtime availability**: a documented function can be absent from the
  installed REAPER version. The bridge checks availability and returns an
  explicit error; the catalog is a discovery surface, not a runtime promise.
- **Generic calls are low-level**: `reaper_api_call` and Action invocation are
  not automatically covered by the transaction preview/undo guarantees. The
  model must read back and is told so.
- **Mutations now require a receipt**: `reaper_edit` apply/undo,
  `reaper_api_call`, `reaper_action` invoke and `reaper_save` fail closed
  without a complete panorama receipt and exact revision. Internal read-only
  timeline/analysis calls bypass the public gate via a thread-local, not via a
  model-settable flag.
- **Partial page sets**: `reaper_panorama_ack` returns `incomplete` and
  `missing_pages`; it mints no receipt. Duplicate `page` requests are
  idempotent; duplicate successful `ack` returns the same receipt.
- **Cross-archive delta**: base and target must share one capture archive;
  otherwise `stale_base/base_not_in_archive` and a refresh instruction.
- **Capture validity**: `reaper_capture_read` exposes `capture_status`,
  `invalid_reasons`, `master_validation`, `coverage` and `missing`; an archived
  invalid master is not presented as complete evidence.

## Actual gaps found in this audit

1. **No exhaustive call test for all 732 functions.** Catalog parsing and
   signature validation are tested; live execution of every function is not.
   Runtime-specific arguments (optional parameters, union-like host objects,
   array returns) still need a generated smoke matrix.
2. **Large-project capture target list (fixed, audio still open).** The 41-track
   copy initially failed with an empty `RENDER_TARGETS` while another project tab
   was active; `524416` was the valid batch bitfield. `capture_render` now
   activates the target project and restores the previous tab. The inactive
   copy now captures, but returned `valid_expected_silence`; a fully valid
   41-track audio capture with working sources is still unproven.
3. **Old MCP process cache.** The configured `reaper-mcp` server in the running
   Hermes process still advertised the pre-lifecycle 21 tools. A separate
   `reaper-panorama` entry discovered the 29-tool server in a fresh session.
   This is a client restart/discovery issue, not a server schema issue.
4. **Codex live delivery and provider token usage not measured.** Hermes host
   thresholds and page character sizes were measured; Codex's declared 10K-token
   limit was checked only from its config.
5. **Spatial/Flamingo/listener paths** were not exercised in this audit; their
   GPU and audio-delivery gates remain as previously documented.

FX insertion is not a gap: the captured panorama contains six track `ReaEQ`
slots plus master `ReaLimit`; `Guitar Bus` deliberately has no FX. The clean
session's “bus has 0 FX” statement is accurate.

## Verification commands run

- `PYTHONPATH=src python -m pytest -q tests/test_panorama_lifecycle.py tests/test_mcp_server.py tests/test_capture_panorama.py` — 41 passed at that point.
- `PYTHONPATH=src python -m pytest -q` — **897 passed, 2 skipped** after the
  lifecycle, idempotency and cross-archive base tests.
- `PYTHONPATH=src <path-to-library-venv-python> -m pytest -q`
  — **897 passed, 2 skipped** in the MCP server environment.
- `hermes mcp test reaper-panorama` — connected, **29 tools discovered**,
  including `reaper_panorama_delta_ack`.
- Fresh `hermes -z` session: 3-page open, all pages fetched, ack receipt, gated
  fader edit, readback 0.55, valid capture, delta/invalidation.
- Real two-cycle driver on `archive5`: two edit→capture→delta→ack cycles with
  no page replay; successor token gate accepted the second target revision.
