# Round 9 — independent audit of the model-facing MCP context interface

**Status:** audit + design recommendation. Read-only: no production file, test, README or other
document was modified; the only repository write is this file.
**Date:** 2026-11-14 (UTC+03:00). **Repository:** `reaper-audio-library`, branch `main`,
HEAD `86b14bb`, worktree dirty (pre-existing, intentional).
**Mode:** in-process against the archived capture and the deterministic test fixtures. No live
REAPER, no user project, no GPU/audio/model service, no live MCP client was started.
**Inputs reconciled:** `docs/TZ.md`, `docs/DECISION-single-response-panorama.md`,
`docs/FULL-CAPTURE.md`, `docs/MCP-README-LLM.md`, `docs/ARCHITECTURE.md`, `docs/VALIDATION.md`,
`docs/research/round-3..8`, current `src/reaper_audio/context/*` and `src/reaper_audio/mcp_server.py`,
plus the current primary MCP/client documentation opened on 2026-11-14.

Every byte number below is a UTF-8 length of canonical JSON (`sort_keys`, `separators=(",",":")`)
measured today unless the row says "historical". Token counts are **not** measured with a tokenizer;
where a token estimate appears it is the code's own `bytes/2.2` fallback. The reproduction snippets
are in §10.

---

## 1. What the requirement actually is (and what it is not)

The requirement is not "one JSON blob". It is a chain of owner decisions:

1. **The panorama is the only model-facing state representation** (round-4 owner addendum,
   2026-11-13). A model must not have to choose between several partial readings of the project.
2. **Nothing measured may be dropped to save bytes** (same addendum: "every available metric and
   temporal series, not only flagged values or a selected subset"; round-6 D6-04/D6-05).
3. **No mandatory page walk or client loop** (DECISION §1, §7, 2026-11-14). The page receipt
   proved delivery, not reading, and cost ~193 K cumulative tokens for one load in the clean-session
   log — a historical measurement, not re-run here.
4. **Portable across stock tools-only MCP clients** (round-6 directive §2.1; round-7 O1). No
   Hermes ContextEngine, plugin, hook, or client patch.
5. **Minimise agent actions** (`TZ.md` §3, last bullet; §8).
6. **Truthful failure** (`TZ.md` §3 first bullet: state coverage including missing data and reasons;
   DECISION §5: no invented meaning, no silent loss).

Points 1–3 in combination are the hard constraint: the first response must be *the* project state,
not a teaser, and it must not require a ritual. Points 5–6 forbid hiding the cost of a project that
does not fit.

The current implementation keeps 1–3 on a 42-source project and breaks 5 for larger ones: at 101
sources the model receives no project state at all (§2.3). That is the central finding.

---

## 2. Inventory: what the model receives

### 2.1 First response — `reaper_panorama_open`

`mcp_server.reaper_panorama_open` (line 2684) → `PanoramaLifecycle.single` (`lifecycle.py:1112`) →
`panorama_view.build_view` (`panorama_view.py:1275`).

The envelope is `{schema_version, status, project_id, capture_id, revision, fact_root,
document_hash, delivery, view, handle, receipt, context_token, audio_evidence, next_action}`.
`delivery` states `mode: single_response`, `view_schema`, `view_sha256`, `view_bytes`,
`response_bytes`, `max_response_bytes`, `fits`, `pages: false`,
`requires_client_reassembly: false`, and an explicit `receipt_guarantee` that claims delivery, not
comprehension (`lifecycle.py:1156-1168`).

The view (`reaper-workbench.panorama-view/2`, `panorama_view.py:47`) for the archived 42-source
capture, section sizes measured today:

| section | bytes | what the model gets |
|---|---:|---|
| `analysis` | 11,900 | 42 rows × (source, available, stage, signal_stage, window, channel + **24** metric columns), units, per-metric decimals, absent counts, method descriptor, `rms_stats`, series descriptors, precision policy |
| `relations` | 3,495 | all 6 relation kinds with status/edges/`meaning`/`is_not`/method/coverage_rule, pair outcomes, unavailable reasons, coverage table |
| `audio` | 3,136 | one row per captured asset: source, stage, rev, frames, sr, ch, duration, tail, sha256 prefix, analysis index |
| `entities` | 2,710 | GUID + name dictionary (tracks, items, takes, master) |
| `archive` | 2,658 | exact-family catalogue with counts, row unit, bytes and `bytes_method`, plus the detail manifest hash |
| `binding` | 2,184 | capture/project/revision before+after/identity/evidence/render manifest/source document hash |
| `reading` | 2,080 | delivery + precision + epistemic contract |
| `tracks` | 2,044 | one row per track + master: ch, folder, vol, pan, mute, solo, sel, arm, send/recv/hw, fx_n, item_n, audio_i, analysis_i, role |
| `temporal` | 1,959 | stored grid geometry, 300 s and 30 s levels as declared aggregates, `cell_meaning`, exact-series pointer |
| `completeness` | 1,531 | four named completeness claims |
| `routes` | 1,193 | one row per send: from, to, volume, pan, mode, mute, kind |
| `content_index` | 1,190 | per-track item/take/midi counts and extents (lean mode only) |
| `project` / `audio_contract` / `fx` | 1,135 / 1,084 / 1,039 | extents, mapping formulas, FX rows with ≤4 inline params |
| `coverage` / `time` / `derived` | 961 / 784 / 508 | domain status, tempo map, derived-section notes |
| remaining 7 keys | ~1,567 | reading_inventory, units, automation, envelopes, transport, midi, schema fields |
| **view total** | **43,576** | section bytes sum to 43,158; the rest is JSON punctuation between top-level keys |
| **envelope total** | **45,431** | overhead 1,855 bytes |

`view_mode` is `lean_overview`: `items`/`takes` tables and the item/take GUID dictionaries are
removed and catalogued, `content_index` replaces them (`panorama_view.py:1223-1272`,
`lifecycle.py:1136-1142`). The decision is taken on **view** bytes versus
`LEAN_OVERVIEW_BYTES = 45_000`, then the real envelope is measured with exact-length placeholders
and re-measured after the minted tokens are written (`lifecycle.py:1145-1188`).

### 2.2 Exact query — `reaper_panorama_query`

`mcp_server.reaper_panorama_query` (2715) → `PanoramaLifecycle.query` (1244) → `run_query`
(`query.py:389`). Ten families (`panorama_view.ARCHIVE_FAMILIES`, line 79):

| family | selector | delivery path |
|---|---|---|
| `relations` | objects, kinds, bands_hz, interval | canonical document |
| `series` | objects, series, interval, resolution_s | sealed detail manifest, hash-verified |
| `midi_notes` | objects, interval | sealed detail manifest |
| `envelope_points` | objects, interval | sealed detail manifest |
| `items`, `takes`, `audio`, `fx`, `analysis` | objects (, interval for items/fx) | canonical document |
| `project_summary` | — | `project` block only; the raw map is not exposed |

Measured on the archived capture (budget 48,000):

| query | result |
|---|---|
| `analysis` objects=all | `query_too_large`, family 112,223 B |
| `analysis` one source | 5,623 B |
| `analysis` one source, 2 metrics | 3,883 B |
| `relations` all | `query_too_large`, 827,753 B |
| `relations` one track | **`query_too_large`, 53,466 B** |
| `relations` kinds=`track_master_level_relation` | 26,497 B |
| `items` all / `takes` all | 24,822 / 17,919 B |
| `fx` all / `audio` all | 31,766 / `query_too_large` 55,450 B |
| `envelope_points` / `midi_notes` | 4,385 / 1,673 B |
| `project_summary` | 3,924 B |
| `series` rms all / one source | `query_too_large` 241,579 / 8,772 B |

No family is paginated. `query_too_large` carries `measured_bytes`, `max_response_bytes`,
`family_bytes`, `narrow[]`, and for detail families `matching/returned` points (`query.py:561-583`).

### 2.3 Refusal — `response_over_budget`

`_over_budget` (`lifecycle.py:2488`) removes `view`, `receipt`, `context_token`, `handle`; keeps
capture/revision/root/document hash; adds `over_limit` (measured bytes, view bytes, over-by,
sections, `families_bytes`, `what_is_blocking`, three `options`, `no_hidden_fallback`) and a
read-only `read_scope` (`_new_read_scope`, line 704: `mode=read_scope`, `permissions=["read"]`,
`allowed=["reaper_panorama_query"]`, `complete: false`, `receipt: null`, `context_token: null`,
24 h TTL).

Measured on a 101-source synthetic project (100 tracks + master, 400 FX, 100 routes, built from the
real record by `tests/test_single_view.py::_synthetic_100_source_document`):

| quantity | value |
|---|---:|
| full view | 80,522 B |
| lean view | 82,832 B (larger: nothing to remove, `content_index` adds bytes) |
| `single()` status | `response_over_budget` |
| `over_limit.measured_bytes` | 81,851 |
| over by | 33,851 |
| `view` / `receipt` / `context_token` / `handle` present | no |
| `read_scope` present | yes |
| read-scope query, one analysis row | `query` 2,468 B, `read_only: true` |
| read-scope query, analysis all | `query_too_large` 177,700 B, no `data` |

**What the model does *not* receive in this case: any track name, any track id, any mixer value, any
metric value, any relation count.** `over_limit` names byte sizes and families, not objects. And it
cannot recover them by query, because **there is no `tracks` family** (and no `routes`, `automation`
or `envelopes` family) in `ARCHIVE_FAMILIES`; asking for one raises `validation_error: unknown data
family` (`query.py:406-410`). The only structural query that succeeds is `project_summary`
(3,924 B) and per-row `analysis` (which carries `source_id` but not the track name, fader, mute,
solo or folder).

That is gap **G1**, and it is the difference between "an honest refusal" and "an honest refusal the
model can act on".

### 2.4 Delta / journal

Two distinct objects:

* **Capture-to-capture** — `reaper_panorama_changes_since` (2805) → `changes_since`
  (lifecycle.py:1316). Bounded by `MAX_DELTA_CHAIN = 20` captures and `MAX_DELTA_OPS = 4_000`
  operations; otherwise `refresh_required`. Operations come from `_diff_values` (line 389), which
  emits **positional JSON paths** (`/analysis/17/metrics/crest_db`), not object ids. `compare` is
  hard-coded `{"project": "same", "verdict": "comparable"}` (line 1491) with no method/window/stage
  comparison.
* **Mutation delta** — `record_change` (1603) → `changes.build_structural_delta`; id-keyed
  `objects.changed` with `MAX_DELTA_OBJECTS = 2_000`, `MAX_EVIDENCE_IDS = 2_000`,
  `MAX_OPERATION_FACTS = 256`; `acknowledgeable` is false when the readback was partial.
  The successor token is returned in the same response (`advance.context_token`, line 1717).

Neither delta is byte-measured. A 4,000-op capture delta or a 2,000-object mutation delta has no
size gate at all — the only caps are counts. That is gap **G6**: a delta can be far larger than any
host cap while the interface treats "count-bounded" as "deliverable".

### 2.5 Size ladder measured today

| project | sources | canonical | full view | lean view | open envelope | outcome |
|---|---:|---:|---:|---:|---:|---|
| synthetic small fixture | 3 | 44,347 | 24,864 | 25,126 | 26,193 | complete (full) |
| archived capture (41 tracks + master) | 42 | 1,378,352 | 46,782 | 43,576 | 45,431 | complete (lean) |
| synthetic 100 tracks + master, 400 FX | 101 | — | 80,522 | 82,832 | 81,851 | `response_over_budget` |
| synthetic source-only sweep | 48 | — | 34,908 | 36,104 | 36,237 | complete |
| synthetic source-only sweep | 56 | — | 36,900 | 38,264 | 38,229 | complete |
| synthetic source-only sweep | 64 | — | 38,893 | 40,425 | 40,222 | complete |

The sweep varies only source count; it copies the fixture's 34 items and 16 FX, so it isolates the
per-source cost (≈249 B/source) and **not** the item/FX/relation cost that dominates a real song.
At the archived project's density (45,431 B for 42 sources, with 34 items, 16 FX, 40 routes,
1,039 band edges) the 48,000-byte ceiling is ≈**50–56 sources**; that is an extrapolation from the
measured marginal cost, not a measured project.

The historical DECISION numbers (47,540 / 49,395 for the full view) are superseded by 46,782 /
(lean 43,576 / 45,431). `DECISION §6` still quotes 43,576 / 45,431 correctly; §1's "118,387 bytes"
and the paged 241 KB / 10-page numbers are historical and not re-measured.

---

## 3. Requirement trace

Legend for evidence: **T** = test (name given), **C** = code (file:line), **M** = measurement made
today. "Gap" is the residual, not a defect of intent.

| # | Requirement (source) | Why it appeared | Current solution | Evidence | Gap |
|---|---|---|---|---|---|
| R1 | One model-facing state representation (round-4 owner addendum; round-7 O2) | Earlier manifest + views split made the model choose which JSON was true; partial views produced false whole-project claims | One `panorama-view/2` from `reaper_panorama_open`; `reaper_capture_snapshot` returns metadata only; `reaper_capture_read` is a gated archive read | C `panorama_view.build_view:1275`, `mcp_server:2656-2710`; T `test_open_delivers_the_whole_view_in_one_result` | `reaper_capture_read` is a second read path and cannot serve the flagship archive (G7) |
| R2 | No lossy or reduced panorama; keep both temporal levels (round-6 D6-04/D6-05) | A 300 s-only or top-N "spine" hides the 30 s level and unique values | Lean overview keeps all tracks/master, all 24 metric columns, both levels (300 s, 30 s), FX/routes/automation/audio/relations kinds; only item/take rows move to a declared exact family | C `panorama_view._temporal:736`, `_lean_projection:1223`; T `test_archived_42_source_open_fits_one_result_with_all_metrics`, `test_temporal_levels_are_declared_and_never_invented` | On a 20 s capture both levels have 1 bucket and the view compresses 4 equal statistics into `value`; correct but not a discriminating test of the two-level requirement |
| R3 | Every measured metric present, with unit, stage, window, channel, precision, absence (round-4 addendum; round-7 O11) | Metrics are the evidence base; a missing or unlabelled metric makes the model's answer false | 24 declared columns, `metric_units`, `metric_decimals`, `absent`, `stages`, `signal_stages`, `windows`, `channels`, `methods`, `precision{lossless:false, exact_query:...}` | C `panorama_view._analysis:545-733`; T `test_metric_families_are_complete_and_carry_units`, `test_scalar_metrics_complete_and_exact_query_round_trip` | `method.version` is `"unversioned"`; `metric_methods` maps all 24 to descriptor 0; per-source notes are counted, not inline (G5) |
| R4 | Exact unrounded values and exact rows reachable (DECISION §5, §6) | Brief events, notes, points and edges are not recoverable from averages | `run_query` + `details.resolve_details` with manifest hash verification; `query_too_large` instead of truncation | C `query.py:389`, `details.py:704`; T `test_query_returns_exact_rows_in_one_result`, `test_query_reports_the_point_limit_as_query_too_large` | `scope.matching_points` is 0 for series (G4); `analysis` keeps ~2 KB of per-row descriptors, so "2 metrics for all 42 sources" still measures 85,584 B and is refused |
| R5 | Whole project in one response, or nothing (DECISION §2; round-7 O4) | A partially delivered project is indistinguishable from a complete one once it is in the transcript | `single()` measures the real envelope and refuses; nothing is minted on refusal | C `lifecycle.single:1112-1242`, `_over_budget:2488`; T `test_over_budget_is_reported_honestly_and_mints_nothing`; M 42-source complete 45,431 / 101-source refusal 81,851 | The refusal is terminal for project orientation: no inventory, no track list, no metric (G1) |
| R6 | No mandatory page walk / cursor loop (DECISION §1, §7; round-7 O5) | Page ritual required model discipline and client loops; 10 pages ≈193 K tokens (historical) | No page tool registered; ten semantic families; no `page`, `cursor`, `offset`, `next` in the model surface | T `test_mcp_surface_is_bounded_and_has_discovery_resources` (27 tools, removed names), `test_the_view_contains_no_pagination_vocabulary` | Private `PanoramaLifecycle.open/page/ack/slice/detail` still exist with page strings (unreachable); `reaper_capabilities(mode="domain")` still documents `offset/limit` for the API catalogue |
| R7 | Portable tools-only MCP; no client-specific mechanism (round-6 directive; round-7 O1) | Codex/Hermes/Claude Code differ in how they persist large results | Tools only; no resource preload; `max_response_bytes` is an explicit caller parameter | C `mcp_server` has no client import; `lifecycle.single(max_response_bytes)` | No host-cap profile; the default is a policy that no host has confirmed; the docs still say it is "inside the Codex ~10,000-token output budget" (arithmetic: 48,000/4 = 12,000) |
| R8 | Receipt means delivery, not comprehension (DECISION §5; round-7 O7) | The page receipt proved the server served bytes, not that the model read them | `receipt_guarantee` states exactly that; `delivery.complete` is not a reading claim | C `lifecycle.py:1166-1167`; C `delivery_status` allowlist `lifecycle.py:499` | None found; wording is consistent in code and guide |
| R9 | Delta from an exact base; no second round trip; never silently patch (round-6 D6-07/D6-08; round-7 O8) | Stale edits silently corrupt a project; an ack round trip adds cost without a guarantee | `changes_since` with `stale_base` reasons; `record_change` returns `advance.context_token`; `delta_ack` is internal | C `changes_since:1316-1556`, `record_change:1603-1738`; T `test_single_receipt_unlocks_mutation_gates` | Capture delta is positional and unconditionally `comparable` (G3); no byte budget for either delta (G6) |
| R10 | Audio freshness separate from structure (DECISION §5; round-7 O9) | A structural edit does not make old audio describe the new mix | `audio_evidence` on receipt/token/delta; `_require_audio_fresh` refuses project-target analysis | C `lifecycle.py:2306`, `require_receipt:2326`; `VALIDATION.md` | `reaper_analyze` gates on project/revision/root but **not** on capture id (`_require_interpretation_receipt` has no `expected_capture_id`), so a receipt for another capture at the same revision unlocks analysis (G8) |
| R11 | Coverage and reasons, never a silent drop (TZ §3; DECISION §5) | A partial capture must not be read as complete | `coverage.domains` expected/observed/status/truncated; `missing`/`truncated`; `absent` per metric; `reading_inventory.unmapped_sections` | C `panorama_view._coverage:899`, `_completeness:1056`; T `test_every_canonical_section_is_placed_or_reported` | `completeness.project_inventory.complete = not missing and not truncated` does not compare expected/observed; `archive_families.complete = bool(families)` is true for one family (G2) |
| R12 | Minimise agent actions (TZ §3; round-7 O14) | Every extra call costs tokens and can be skipped | One open replaces open+page×N+ack | M 42-source: 1 call, 45,431 B | 101 sources: 1 call yields no project state; the model needs an undeclared number of queries (G1) |

### 3.1 Gap register

| id | gap | severity for the stated requirement |
|---|---|---|
| G1 | Over-budget path has no structural or metric inventory and no `tracks`/`routes` query family | **blocking** — the model cannot orient on a large project at all |
| G2 | `completeness` predicates are weaker than their names | high — "no partial marked complete" is only partly enforced |
| G3 | Capture delta is positional and unconditionally `comparable` | high — numeric before/after across method/window/stage is a false claim |
| G4 | `query.scope.matching_points` is 0 for series; `analysis` filter keeps ~2 KB/row of descriptors | medium — completeness of a series answer is not checkable; metrics-for-all-sources needs per-source loops |
| G5 | Method/version is `"unversioned"`; one descriptor for 24 metrics | medium — provenance requirement is only nominal |
| G6 | No byte measurement on delta responses | medium — a legal 4,000-op delta can exceed any host cap |
| G7 | `reaper_capture_read` builds the canonical document at `DEFAULT_MAX_BYTES = 700_000`, which the 1,378,338-byte archive exceeds | medium — the archive read path fails on the flagship project |
| G8 | `reaper_analyze` does not bind the receipt to a capture id | medium — a receipt for capture A unlocks analysis at capture B's revision/root |
| G9 | Docs/code drift: `FULL-CAPTURE.md` says view and sidecar share `capture-panorama/2` and that the default projection budget is 700,000 B; `MCP-README-LLM.md` says relation kinds carry a byte size | low individually, high for auditability |
| G10 | The 48,000-byte default is described as "inside the Codex ~10,000-token output budget" | low, but it is an arithmetic error in a decision record |

---

## 4. Current vs target: page-free alternatives

"Current" is the worktree at HEAD+dirty. "Target" is §7. "Portable" means: works in a stock client
with no client-side extension.

| alternative | current | target | portable? | verdict |
|---|---|---|---|---|
| **Single response** (whole view in one `tools/call` result) | Yes for ≤~50 sources at the archive's density: 42 → 45,431 B complete. No for larger: 101 → refusal | Keep as the primary path; make the refusal deliver a declared minimum (§7 L1) instead of nothing | Yes — in-band `content[]`, the only mechanism that puts bytes in the model's request | **Adopt, with the L1 fallback** |
| **Compact linked representation** (IDs, catalogue, selectors) | Yes: `entities` + integer FKs, `archive.catalog` with counts/bytes/selectors, `archive.exact` with selectors | Keep; add `tracks`/`routes` families and a `metrics_only` projection so the catalogue is actually actionable after a refusal | Yes — links are plain ids, not URIs | **Adopt** |
| **Summary + exact query** | Partly: the "summary" *is* the whole view (not a separate smaller object), and the query lacks the structural families | Define the minimum evidence object (tracks + all metrics + relation counts + coverage) as the L1 summary; exact query unchanged | Yes | **Adopt, and name the summary explicitly** |
| **MCP resources / URIs** | 6 resource templates exist, none for the panorama; `reaper_capture_read` is the archive path | Do not put any mandatory fact behind a resource; optionally expose a read-only `reaper://captures/{id}/view` for human/UI use, never as the model's only path | **No** — resources are application-driven; the host decides whether to read one, and a `resource_link` is "not guaranteed to appear in `resources/list`" | **Reject as a required path** |
| **Files / artifacts** | Archive files on disk (`record.json`, `panorama.json`); `reaper_capture_read` gated and capacity-limited; no file tool in the MCP surface | Keep as the durable authority; never as the delivery path | **No** — a stock model cannot open a path unless the host gives it a file tool; Hermes spills are a host behaviour, not a contract | **Reject as a required path** |
| **Delta protocol** | Yes: capture delta + auto-advance + mutation delta | Keep; make capture delta id-keyed and comparability-gated; byte-measure it | Yes | **Adopt, fix G3/G6** |

Why resources cannot close the gap, concretely: the archived view is 43,576 B and the canonical
document 1.38 MB. A resource read returns through the same host pipeline as a tool result
(Hermes: "`read_resource` is an ordinary tool result", installed reference), so it does not raise
any ceiling. It only adds a step.

---

## 5. External sources

All opened **2026-11-14**. Protocol version **2026-07-28**. "Status" separates a normative protocol
statement, an observed host behaviour, and this report's inference.

| # | source (URL) | exact claim (quoted or narrowly paraphrased) | status | applicability |
|---|---|---|---|---|
| S1 | `https://modelcontextprotocol.io/specification/2026-07-28/server/tools` | A tool result has `content[]` (text/image/audio/resource_link/embedded resource) and optional `structuredContent`; `outputSchema` is optional; a structured result **SHOULD** also be serialized as text for compatibility | standard | `content[]` is the only in-band delivery; duplicating `structuredContent` as text is a SHOULD, not a size mechanism |
| S2 | same, §"Resource Links" | A tool **MAY** return a `resource_link`; "Resource links returned by tools are not guaranteed to appear in the results of a `resources/list` request" | standard | a link is an address, not content; cannot be the only path to a mandatory fact |
| S3 | same, §"Stateful Tools" | "This section is non-normative guidance… The protocol has no concept of a state handle; from the wire's perspective a handle is an ordinary string" | standard (non-normative section) | `handle`, `read_scope`, `context_token` are legitimate as explicit identity strings; they are not protocol state, and the server must validate them on every call |
| S4 | `https://modelcontextprotocol.io/specification/2026-07-28/server/resources` | Resources are "**application-driven**, with host applications determining how to incorporate context based on their needs"; `resources/read` is a separate request; `subscribe` delivers `notifications/resources/updated` only | standard | resources can never guarantee that the model receives an object |
| S5 | `https://modelcontextprotocol.io/specification/2026-07-28/basic/index` | "MCP is a **stateless protocol**: all the information needed to process a request is contained in the request itself… no state should be inferred from previous requests" | standard | every call must carry `revision`/`handle`/`archive_root`; a "current view" cannot be re-read implicitly |
| S6 | `https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/pagination` | Pagination is supported for `resources/list`, `resources/templates/list`, `prompts/list`, `tools/list` only | standard | there is no protocol page for a tool result; any page numbering is a server invention |
| S7 | `https://modelcontextprotocol.io/specification/2026-07-28/schema.md` | `CallToolResult` is `content`/`structuredContent`/`isError`/`_meta`; a keyword scan for `truncat`, `maxResultSize`, `maxSize`, `byte limit`, `size limit` returns no field | standard (absence verified by scan) | the protocol defines no result-size cap and no truncation semantics; the limit is always the host's |
| S8 | `https://code.claude.com/docs/en/mcp` | "Claude Code displays a warning when MCP tool output exceeds 10,000 tokens and limits output to 25,000 tokens by default… Unless the server raises a tool's own limit, successful text results longer than 50,000 characters are saved to a file regardless of this variable." | host behaviour | a 45,431-byte / ~11 K-token (bytes/4) view is under the 50 K-character file threshold but at/over the 10 K-token warning; a 2× larger project would spill |
| S9 | `https://learn.chatgpt.com/docs/config-file/config-reference` (redirect of `developers.openai.com/codex/config-reference`) | `mcp_servers.<id>.tools.<tool>.output_token_limit` — "Token budget for one MCP tool's output, before the standard 20% serialization allowance. Overrides the model's default output truncation budget for that tool." No default value is stated on the page | host behaviour; default **unverified** | Codex truncates per tool; the interface cannot assume a specific number, so `max_response_bytes` must stay a parameter |
| S10 | Hermes installed source `%LOCALAPPDATA%\hermes\hermes-agent\tools\budget_config.py` (0.23.x) | `DEFAULT_MCP_RESULT_SIZE_CHARS = 50_000`, `DEFAULT_PREVIEW_SIZE_CHARS = 1_500`, `DEFAULT_RESULT_SIZE_CHARS = 100_000`, `DEFAULT_TURN_BUDGET_CHARS = 200_000`; `tool_result_storage.maybe_persist_tool_result` writes the full text to a spill file and returns `<persisted-output>` + preview + path | host behaviour (this installation) | any MCP result above 50,000 characters reaches the model as a 1.5 KB preview unless the model reads the spill path; the public Hermes docs do **not** state this number, so it is install-specific |
| S11 | `https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp` | documents server registration, filtering, lazy start; the MCP feature page does not state a tool-result size threshold | host docs (absence noted on this page) | confirms S10 must be cited as an installed-source fact, not a documented guarantee |

**What follows from the standards, not from inference**

* No MCP rule limits a tool result or defines truncation (S1, S7).
* No MCP mechanism delivers a complete object to the model except a tool result the host chooses to
  pass through; resources, links, subscriptions and notifications are all mediated by the host
  (S2, S4).
* Cross-call state is only what the caller explicitly sends (S3, S5); pagination is a list-operation
  device (S6).

**What is inference, and is labelled as such in §7**

* A single result size that is "safe" for every host does not exist; 48,000 B is a policy chosen
  below two known 50,000-character thresholds and above an unknown Codex default.
* The only portable guarantee is: "one exact object was serialized into this response, and its
  measured size is reported". Whether the host keeps it is outside the protocol.

---

## 6. What the current design gets right (do not regress)

1. **Measured, not estimated, envelopes.** Placeholders have the exact length of the minted tokens
   and the response is re-measured after they are written (`lifecycle.py:1145-1188`). The 101-source
   refusal's `measured_bytes` (81,851) equals the real serialization.
2. **Nothing is minted for an undelivered view.** The refusal drops `view`, `receipt`,
   `context_token`, `handle`; the test asserts it (`test_over_budget_is_reported_honestly_and_mints_nothing`).
3. **The receipt wording is honest** — delivery of a document hash/revision/fact root, not reading.
4. **`read_scope` is genuinely read-only** — `mode=read_scope`, `permissions=["read"]`,
   `allowed=["reaper_panorama_query"]`, no receipt, no token; mutation gates require a full-view
   receipt (`test_read_scope_is_read_only_bound_and_queryable`).
5. **The lean projection is a projection, not a summary** — same 24 columns, same 42 rows, same
   units/decimals; it removes only item/take rows and declares where they went
   (`inline_families` / `catalogued_families`, `content_index`).
6. **Hash-bound detail** — series, MIDI and envelope rows are re-hashed against the sealed manifest
   (`details.resolve_details`), so a stale archive fails closed.

---

## 7. Recommended design

**Name:** *one measured object, then a declared minimum, then a measured refusal* (L0/L1/L2).

The design keeps the current single-response contract and adds exactly one new delivery state. It
does not add pages, cursors, offsets, numbered chunks or client reassembly.

### 7.1 L0 — the whole view fits (primary, unchanged)

`reaper_panorama_open` returns one `panorama-view/2` (full or lean), the receipt and the context
token in one result. Requirements:

* the view is built by the same pure projection; `view_sha256` is present;
* `delivery.response_bytes` is the measured length of the actual envelope;
* `status: "complete"` is legal only when the whole view is inside this response
  (`delivery.fits` true and `view` present);
* `structuredContent`, if the server ever adds it, must be byte-identical to the text block and
  must not be the only copy.

This is what the code does today; no change to the 42-source path.

### 7.2 L1 — the whole view does not fit, but the decision-critical minimum does (new fallback)

When the measured envelope exceeds the budget, do **not** return an empty refusal first. Build a
second projection from the same document and measure it:

`minimum = {binding, inventory, tracks, analysis (metrics-only matrix), relations (kind counts +
coverage), coverage, completeness{scope}, archive{catalogued_families, exact}, reading, budget}`

with, concretely:

* `tracks`: one row per track + master — id, name, folder, vol, pan, mute, solo, arm, send/recv/hw
  counts, fx_n, item_n (the same `_tracks` table, 2,044 B for 42 sources, 4,807 B for 101);
* `analysis`: `projection: "metrics_only"` — `columns=[source, *24]`, rows of values, `metric_units`,
  `metric_decimals`, `absent`, `windows`, `channels`, `stages`. No per-row `series` descriptors and
  no per-row uncertainty notes (they stay reachable through the full `analysis` query). Measured on
  the 101-source document: **8,539 B**, 9,186 B with units — versus 175,305 B for the current
  `analysis` family with descriptors;
* `relations`: the kind table with edge counts, `meaning`, `is_not`, coverage rule, pair outcomes
  and unavailable reasons (no edges);
* `inventory`: counts only — tracks, master, items, takes, midi, fx, routes, automation,
  audio, analysis, plus the capture window;
* `completeness.scope`: `{scope: "minimum", whole_project: false, complete_for_scope: true,
  catalogued: ["items","takes","series","relations","midi_notes","envelope_points","fx_params"]}`;
* `delivery`: `mode: "minimum"`, `view_bytes`, `response_bytes`, `max_response_bytes`, `fits`,
  `pages: false`, and `receipt_guarantee: "this minimum was delivered; it is not the whole-project
  view and mints no receipt"`.

The response mints **no** `receipt`, **no** `context_token`, **no** `handle`; it carries a
`read_scope` for exact queries. A mutation still requires a later L0 delivery. The model instruction
must say: "if `delivery.mode` is `minimum`, you have the project's structure and every scalar metric,
but not item/take rows, relation edges or series cells; use `read_scope` and
`reaper_panorama_query` for those, and do not claim whole-project completeness."

Why this does not violate R1/R2: it is the *same* tables from the *same* projection, explicitly
scoped and explicitly not the panorama. It replaces nothing and hides nothing that it claims; it
declares the families it catalogued. It is not the rejected "spine", which claimed to be the
panorama and dropped the 30 s level and the measurement metadata.

Estimated coverage: at ≈132 B/source for tracks + metrics (measured 4,807 + 8,539 over 101 sources),
L1 fits ≈300 sources in 48,000 B; that is an extrapolation, not a measured project. The view's
fixed overhead (~10 KB of binding/reading/coverage/archive) dominates below that.

### 7.3 L2 — even the minimum does not fit (last resort)

Return the current `response_over_budget`, plus:

* `over_limit.inventory` (counts, capture window, capture id) — measured from the document, no values;
* `read_scope` (unchanged);
* `query_plan`: an ordered list of semantic calls that each fit, with the measured or estimated bytes
  of each: `project_summary` (measured 3,924 B on the archive), `tracks` (new family), `analysis`
  with `projection: "metrics_only"`, `relations` with `kinds=[...]`, then per-object families;
* `min_required_calls` and `no_hidden_fallback` (already present).

No view, no receipt, no token, no handle. The plan is advisory; the model may call any subset in any
order. Nothing in it is positional.

### 7.4 Query families to add (both L1 and L2 depend on them)

| family | shape | measured basis |
|---|---|---|
| `tracks` | one row per track/master, same columns as the view's `tracks` table | 2,044 B (42) / 4,807 B (101) in view form |
| `routes` | the view's `routes` table | 1,193 B (42) / 2,836 B (101) |
| `analysis` + `projection: "metrics_only"` | `[source, *24]` + units/decimals | 8,539 B for 101 sources |

These are additive read-only families. They do not change the canonical document, the view schema,
the receipt, or any mutation gate.

### 7.5 Delta fixes (independent of L0/L1/L2)

1. Capture delta becomes **id-keyed**: `{kind, id, field, before, after}` derived from stable ids, not
   `/analysis/17/...` paths. A property test must assert `apply(base, delta) == target` and that
   reordering the target does not produce spurious changes.
2. `compare.verdict` is computed, never hard-coded: `comparable` only when method, version, window,
   stage/channel and units match; otherwise `not_comparable` with the mismatched fields, and no
   numeric before/after for the affected rows.
3. Every delta response is byte-measured. If it exceeds the budget, return `delta_too_large` with
   `measured_bytes`, `objects`, `unchanged` and the two recovery options: a narrower delta (by
   object/kind) or a full refresh. No truncation, no partial ops.

### 7.6 Honest physical limits

* **The 48,000-byte default is policy, not a protocol constant.** It sits below Claude Code's and
  Hermes's 50,000-character spill thresholds and above an unknown Codex per-tool default. A host
  with a lower cap will truncate or spill; a host with a higher one can be told
  `max_response_bytes`.
* **No format fits an unbounded project.** At the archive's density the ceiling is ≈50–56 sources;
  JSON grows ≈249 B per source before items/FX/relations, and relations alone are 840,989 B in the
  canonical document.
* **Exact queries are not always one call either.** A single track's relation edges measured
  53,466 B — over budget. Series cells measure 27,060,069 B for all sources; one source's rms is
  8,772 B. Narrowing by kind/band/interval is part of the contract, not an edge case.
* **Bytes are not tokens.** The only token figure in the code is `bytes/2.2`; no tokenizer was run.
  A 45,431 B response is ~11.4 K tokens at 4 B/token, i.e. already above Claude Code's 10 K-token
  warning threshold even though it is below the 50 K-character spill threshold. The two host limits
  are different limits and both bind.
* **Delivery is not comprehension.** The server cannot observe what the host kept or the model read.

### 7.7 If the host truncates anyway

Three cases, three answers; none of them is "pretend it arrived":

1. **The server knows it is over the caller's budget** → it refuses (L1/L2) and emits a measured
   object, so no truncated payload is ever produced by this server.
2. **The host cap is lower than the declared budget and truncates mid-JSON** → the text is not valid
   JSON; a conforming host surfaces a tool error, not a partial object. The instruction must say:
   "if the open result cannot be parsed, or has no `delivery.response_bytes` / `view_sha256`, do not
   reason from it; call `reaper_panorama_status` and re-open with a smaller `max_response_bytes`."
3. **The host spills to a file and shows a preview** (Claude Code >50,000 chars; Hermes >50,000
   chars) → the model has a path, not the data. The same recovery applies, plus: the model may read
   the spill file if the host exposes a file tool, but the interface must not depend on it. The
   response header (`binding` + `delivery` + `budget`) is deliberately first in key order so a
   preview still carries the identity of what was truncated.

There is no protocol mechanism — resource, link, notification, subscription, `structuredContent`,
MRTR, or tasks — that guarantees the model receives a complete object once the host caps the result.
The design therefore promises only what the server controls: **one exact object, measured, or an
explicit smaller exact object, or a measured refusal — never a silent partial.**

---

## 8. Rejected alternatives

| alternative | why rejected, tied to a requirement |
|---|---|
| Mandatory pages + ack | R6/R12: requires model discipline and a client loop; the receipt proves delivery, not reading (the original defect). Historical cost 193 K tokens per load. |
| One giant raw response (1.38 MB canonical) | R5/R7: spills or truncates on every stock host; relations alone are 840,989 B. |
| Raise the default to 200 KB | R7: Hermes spills at 50 K characters and Claude Code saves to a file at 50 K; a larger default converts a clean refusal into a silent preview. |
| Lossy spine / top-N / "important metrics only" | R2/R3: hides unique values; owner-rejected twice (20,838 B experiment; round-6 D6-04). |
| Drop relation edges from the panorama | R3/R11: they are measured facts; they stay catalogued and queryable. |
| `resource_link` / MCP resource as the required path | R7 + S2/S4: application-driven; the host may never read it; not guaranteed in `resources/list`. |
| A file/artifact as the model's delivery path | R7: a stock model has no file tool; Hermes spill is host behaviour, not a contract. |
| Client-side reassembly / ContextEngine slot | R7/round-7 O1: requires a harness-specific mechanism. |
| Cursor/offset "detail" pagination | R6: the same failure mode as pages under another name. |
| Keep page tools but "optional" | R1: two contradictory contracts on `tools/list`; the model cannot tell which one guards its edits. |
| L1 as a *lossy replacement* for the panorama (the spine error) | R2: a minimum is allowed only if it is the same projection, explicitly scoped, `whole_project: false`, and mints no receipt. |
| Claim "any harness, any project, one call" | §7.6: false arithmetic; no host guarantees it. |

---

## 9. Acceptance criteria

Each criterion is testable in-process against a fixture. Nothing here requires live REAPER.

**A. Small project (3 sources; the existing `_record` fixture)**

1. `single()` returns `status: complete`, `view_mode` absent (full), and `delivery.fits: true`
   with `response_bytes == len(canonical(response))` (26,193 B on today's fixture).
2. The view contains every section in `SECTION_INVENTORY`'s value set at least once, and
   `reading_inventory.unmapped_sections == []`.
3. Every `analysis.rows[].source` and `audio.rows[].source` resolves to `0..N-1`; no `-1` for a
   present object.
4. A metric absent in the capture is `null` with a non-zero `analysis.absent[name]`, never `0`.

**B. Typical project (archived 42-source capture)**

5. `single()` returns `complete`, `view_mode: lean_overview`, `delivery.response_bytes ≤ 48_000`
   (today 45,431), `view_bytes ≤ 45_000` (today 43,576), 42 `analysis.rows`, 24 metric columns,
   24 units, 24 decimals, 0 unmapped sections.
6. `canonical(panorama)` is 1,378,352 B and the view is a pure function of it: two builds produce
   the same `view_sha256`.
7. Exact round-trip: one source's `crest_db` from `analysis` query equals the canonical float
   (unrounded), and differs from the inline rounded value when the canonical value has more than
   3 decimals.
8. `relations.kinds` contains all six kinds with a status and, for emitted kinds, `units`,
   `meaning`, `is_not`, `method`, `coverage_rule`; `evaluated_pairs` equals the canonical
   `len(relations.evaluated_pairs)`.

**C. Over-limit project (101 sources, 400 FX)**

9. L0 fails honestly: no `view`, `receipt`, `context_token` or `handle`; `view` is absent from the
   keys (today true).
10. L1 (if adopted) returns `delivery.mode: "minimum"`, `completeness.scope.whole_project: false`,
    `complete_for_scope: true`, 101 track rows, 101 × 24 metric values, and `response_bytes ≤ budget`.
    If L1 is not adopted, this test is replaced by criterion 12 and the design is explicitly L2-only.
11. L1/L2 metrics equal the canonical values for **all** 101 × 24 cells (compare against
    `document["analysis"]`, allowing the declared inline rounding).
12. L2 carries `over_limit.inventory` with counts equal to the canonical counts, `read_scope`,
    and a `query_plan` whose every step is measured to fit; no step names a page, cursor or offset.
13. A read-scope query for `data=["analysis"], objects="all"` returns `query_too_large` with
    `family_bytes` and **no** `data`; a read-scope query for one source returns `query` with
    `read_only: true`.
14. `read_scope` is rejected by `reaper_edit`, `reaper_api_call`, `reaper_action`, `reaper_save`,
    `require_receipt`, and `reaper_capture_read`.
15. `tracks` and `routes` families (if added) return the same rows as the view's tables, and
    `analysis` with `projection="metrics_only"` returns 101 × 24 values in one response
    (expected ≈8.5 KB, measured projection today).

**D. All metrics, relations and sources**

16. Every metric declared in `analysis_metric_units` has a column, a unit, a decimal policy and an
    `absent` count; no metric is dropped when absent.
17. For every relation kind, `edges` equals the count of canonical edges of that kind, and a
    `kinds=[kind]` query returns every edge of that kind (count equality, endpoint equality).
18. Every source in the capture appears exactly once in `analysis.rows`, `audio.rows` and
    `content_index.rows`; the master is the last source slot.

**E. Fresh revision and delta**

19. After a mutation, the delta response contains `advance.context_token`, `advance.revision` equal
    to the target revision, and `advance.audio_evidence`; no second call is needed.
20. `apply(base_document, id_keyed_delta) == target_document` for a reorder-only change and for a
    value change; a reorder produces no spurious ops.
21. A delta across captures whose method/version/window/stage differs carries
    `compare.verdict: "not_comparable"` with the mismatched fields and no numeric before/after for
    the affected rows.
22. `stale_base` is returned for a wrong root, a wrong project, an identity discontinuity, a chain
    > 20 and a delta > 4,000 ops; each carries a `next_call` and mints nothing.

**F. Truncating transport / client**

23. With `max_response_bytes=20_000` on the archived capture, `single()` returns L1 or L2 with
    `response_bytes ≤ 20_000` and never a partial `view`; the refusal/minimum is internally
    consistent JSON.
24. A harness that cuts the serialized text at N bytes produces invalid JSON in every case where
    the cut is inside the object; the documented recovery (`status` → `refresh` with a lower
    `max_response_bytes`) is the only path, and no code path sets `status: complete` on that text.
25. `structuredContent`, if present, is byte-identical to the text block; a test removes the text
    block and asserts the interface still documents `content[]` as authoritative (no
    structured-only delivery).

**G. Errors and retry**

26. Unknown family/object/metric returns `validation_error` with `allowed` or `next_action`, never a
    traceback and never an empty success.
27. An expired or foreign `read_scope` returns `stale_context` with a next action; a repeated valid
    query returns byte-identical output and the same `document_sha256`.
28. Retrying the same open after a refusal with a narrower scope succeeds and issues a normal
    receipt/token (the L1/L2 path does not poison the later L0).

**H. No partial marked complete**

29. `status: "complete"` appears only with `delivery.fits: true` and a present `view`; the refusal
    and the minimum have `receipt: null`, `context_token: null`, `handle: null`.
30. `completeness.project_inventory.complete` is false when any `coverage.domains[]` row has
    `expected != observed`, `status != "complete"` or `truncated`; `archive_families.complete` is
    true only when every family in `ARCHIVE_FAMILIES` is catalogued or explicitly marked
    `not_measured_in_capture`.
31. `reading.scope` and `completeness.scope` state `whole_project` truthfully in every mode.

**I. No model-driven page memory**

32. No model-facing schema, description or response contains `page`, `cursor`, `offset`, `next_page`,
    `page_index`, `part N of M` in a panorama context (the API-catalogue `offset/limit` is a
    separate tool and must say so).
33. The only cross-call strings are identity: `handle`, `read_scope`, `context_token`,
    `revision`, `capture_id`, `fact_root`, `document_hash`. None encodes a position or an order.
34. Query results are order-independent: the same selection returns the same rows and the same
    `measured_bytes` regardless of the order of `objects`, `data` or prior calls.

**J. Documentation consistency**

35. `DECISION-single-response-panorama.md` §1/§6 numbers match a reproducible fixture test (today:
    canonical 1,378,352; lean 43,576; envelope 45,431); the "inside Codex ~10,000-token budget"
    sentence is removed or restated as 48,000/4 = 12,000 tokens.
36. `FULL-CAPTURE.md` no longer claims the view and sidecar share `capture-panorama/2`, and its
    default-budget paragraph no longer describes 700,000 B as the model-view budget.
37. `MCP-README-LLM.md` either adds the per-kind byte size to `relations.kinds` or drops the claim;
    its tool map matches 27 tools.

---

## 10. Reproduction

```python
# archived 42-source capture (read-only; state under a temp dir)
from pathlib import Path
from reaper_audio.context.full_capture import CaptureArchive
from reaper_audio.context.panorama import build_panorama
from reaper_audio.context.panorama_view import build_view, canonical, view_bytes
from reaper_audio.context.lifecycle import PanoramaLifecycle

root = Path("work/benchmark/himmp/capture-archive")
cap = "cap_01M79659V69NDC5YNAA0SMPX51"
rec = CaptureArchive(root).read(cap)
doc = build_panorama(rec, record_path=f"captures/{cap}/record.json",
                     max_bytes=10**12, max_tokens=10**12)
print(len(canonical(doc)),                     # 1378352
      view_bytes(build_view(doc)),             # 46782
      view_bytes(build_view(doc, lean=True)))  # 43576
opened = PanoramaLifecycle(root, state_root=Path("<tmp>/state")).single(rec.project_id)
print(opened["status"], opened["delivery"]["response_bytes"])  # complete 45431
```

Tests run today (all green): `PYTHONPATH=src python -m pytest -q tests/test_single_view.py
tests/test_archived_single_view.py tests/test_mcp_server.py tests/test_panorama_lifecycle.py`
→ **64 passed in 56.54 s**. The full suite was not re-run.

---

## 11. Verified facts vs unverified limits

**Verified today (code + in-process measurement + tests)**

* Model surface: 27 tools, 6 resource templates, no page/ack/detail/slice/delta_ack tool.
* 42-source open: `complete`, `lean_overview`, view 43,576 B, envelope 45,431 B, 42 rows, 24
  columns, 0 unmapped sections, canonical 1,378,352 B.
* Small fixture: 3 sources, 26,193 B envelope, `complete`.
* 101-source synthetic: 81,851 B measured refusal, no view/receipt/token/handle, `read_scope`
  present; read-scope query 2,468 B; all-analysis query refused at 177,700 B with no data.
* Exact-query sizes in §2.2, including the 53,466 B single-track relation refusal.
* `query.scope.matching_points == 0` for series is still present (round-8 defect not fixed).
* `compare.verdict` is hard-coded `"comparable"`; `_diff_values` emits positional paths.
* `reaper_capture_read` cannot build the canonical panorama at `DEFAULT_MAX_BYTES = 700_000`
  (`PanoramaCapacityError`, estimated 1,378,338 B, max tokens 256,000).
* `reaper_analyze` does not pass `expected_capture_id` to the receipt gate.
* Protocol: no result-size limit, no truncation field, resources application-driven, stateless,
  pagination list-only (2026-07-28 spec).
* Host numbers: Claude Code 10 K warning / 25 K default / 50 K-character file spill; Codex
  per-tool `output_token_limit` with no documented default; Hermes installed constants 50,000 /
  1,500 chars.

**Unverified (do not quote as fact)**

* Any real token count for the view (no tokenizer was run; `bytes/2.2` is the code's fallback).
* Live behaviour of Codex, Claude Code or Hermes on a 45,431 B result; no host session was started.
* The Codex default `output_token_limit`.
* The exact source ceiling for a real project (>50–56 sources is an extrapolation from 249 B/source,
  not a measured project).
* Whether an L1 minimum actually fits for a real 200+ source project (the 101-source projection was
  measured; the 300-source figure is arithmetic).
* `reaper_capture_read` with a valid receipt on a large archive (the capacity failure was reproduced
  at `build_panorama`, not through the MCP wrapper with a receipt).
* Comprehension: no model was asked to answer questions from the view.
* The historical paged-load costs (193 K / 967 K tokens) — taken from round-6's log, not re-run.

---

## 12. Decisions the owner must ratify

1. **L1 (minimum evidence object) or L2-only.** L1 changes `DECISION §2` from "if it does not fit,
   return `response_over_budget`" to "return the minimum, then refuse". It is the only change that
   makes a >50-source project usable in one call. Without it, the honest ceiling stays ≈50–56
   sources and larger projects start with an exact-query plan.
2. **Default budget.** 48,000 B is below two 50 K-character thresholds but above Claude Code's
   10 K-token warning at 4 B/token. A 31,000 B default would clear the warning at 3.07 B/token but
   would push more real projects into L1/L2. This is policy, not protocol.
3. **`reaper_capture_read`.** Raise its internal limit, return the view, or remove it from the model
   surface. Today it is a second read path that fails on the flagship archive.
4. **`reaper_analyze` capture binding.** Pass `expected_capture_id` and make the guide true, or state
   that analysis is revision-bound and capture-agnostic.
