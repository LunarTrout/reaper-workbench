# Agent B — Canonical panorama, incremental project/audio state, dependency correctness

> **Historical record (superseded transport).** This document describes the
> paged panorama contract that has since been replaced by one whole-view result.
> Current behaviour: `docs/DECISION-single-response-panorama.md` and
> `docs/MCP-README-LLM.md`. Treat this file as history, not instruction.


**Round 6, Workstream B.** Research only: no source, test, archive or MCP change was made. Written against
`86b14bb` plus the shared dirty tree (the Hermes performance work was left untouched). All repo facts below were
re-read from the checkout; all archive numbers were recomputed read-only from
`work/benchmark/himmp/capture-archive/`.

**Labels used throughout:** `[obs]` observed in code or archive with a file:line or command; `[src]` supported by a
cited source; `[inf]` inference from the two; `[dec]` a design decision this report recommends. Round-4/5 documents
are cited where they already settled something; this report does not re-litigate them.

**Revision 2 (2026-11-13).** Rewritten around a **client-agnostic MCP contract** after the owner clarifications at
`PANORAMA-CONTINUITY-RESEARCH-DISPATCH.md:104-137`. The identity, delta, invalidation and archive-audit work of
revision 1 stands unchanged. What changed is the delivery layer, the lifecycle, the first-load strategy, the interval
path and the acceptance tests: every guarantee below has to hold for a stock MCP client with no resources, no
subscriptions and no client-side assistance. Client-side context manipulation is out of scope; the contract must work
without it.

---

## 1. Executive summary

The engineering answer is a **portable MCP contract**, not a client feature and not a format migration. Keep the
canonical record as typed JSON, add a project-level append-only revision journal with a per-revision `fact_root`, and
make the panorama a deterministic **materialization** of `(project, instance, epoch, fact_root, evidence_set)` rather
than a sidecar owned by one capture. Expose it through three ordinary state calls — open, changes-since, refresh —
plus one evidence accessor, whose entire client-side requirement is a `project_id` and a `revision` string. Deltas stay panorama-shaped typed
operations keyed by stable entity IDs, with before/after values, explicit states and a comparability verdict. Nothing
in the required path depends on any particular host, on client-side context machinery, or on the client retaining
anything: a stock
client that loses everything recovers by a bounded full refresh, and one that loses only its handle still gets an
exact delta
because revisions are server-global. Seven findings drive that design.

1. **A capture is an event, not a state change.** Two consecutive captures of identical project content
   (`restore_report.digest_fields_equal = true` in both) produce 43/43 different audio SHA-256 values, while 42/43
   analysis metric vectors are identical. Re-rendering is not bit-identical, so audio bytes cannot identify
   "unchanged", and a hash-based dependency graph would invalidate everything on every capture. `[obs]` §3.4.
2. **A panorama can be complete, hash-valid and wrong.** An archived capture whose master render was silent
   (`timings.capture_status = invalid_master`) still reads back through `reaper_capture_read` as `ok: true`,
   `coverage.complete: true`, `truncation.silent_drop: false`, all 43 rows `render_provenance.complete: true`, with
   no validity field anywhere. `[obs]` `cap_01M7A9ES…`. The snapshot response does carry `capture_status` and
   `master_validation`; the read path drops both. Capture-time validation fails closed; read-back does not.
3. **Provenance claims exceed the payload.** Compact mode — the only mode used for real captures
   (`panorama.py:1703-1704`) — gives each `analysis[]` row a positional 9-value array and nothing else
   (`panorama.py:1154-1171`). Method, version, stage, sample rate, channels, window and uncertainty are dropped, so
   `epistemic_layers.uncertainty.measurement_notes` is structurally `[]` while `coverage.complete` is `true` `[obs]`.
4. **Identity is capture-scoped or method-blind.** `time_map:<capture_id>` (`panorama.py:1882`) gives an unchanged
   tempo map a new identity per capture; `analysis:<source_guid>` (`panorama.py:1148`) keeps the same identity across
   method changes, so deltas compare unlike algorithms. All 34 take rows have `track_id: null` from a fallback that
   reads a key the item view never has (`panorama.py:246-249`), and the 15 FX rows share only 5 distinct
   `raw_parameters_pointer`s because the FX slot is used as a track index (`panorama.py:368-371`) `[obs]`.
5. **Fingerprint and panorama cover different fields.** Digest track rows omit `channel_count` and `name`; digest item
   rows omit `channel_mode`, `group_id`, `source_channels` `[obs]`. Pan law (`I_PANMODE`) appears nowhere in bridge,
   digest or panorama `[obs]`. A pan-law change alters every render with no detectable revision change.
6. **The budget rests on the wrong tokenizer and contradicts its own numbers.** "700,000 bytes / 256,000 tokens" is
   not two independent limits: at 2.2 B/token the token cap binds at 563,200 bytes. Against the only measured
   tokenizer the documented ratio is 237,477 / 128,449 = **1.8488 B/token**, so `estimate_tokens` under-estimates by
   ~16 %, and a 563,200-byte document is ~304,600 actual tokens — 19 % over the stated ceiling `[obs]` §3.5.
7. **Delivery, not encoding, is the binding constraint.** A 241 KB panorama cannot arrive whole on either named
   client, for different reasons: Hermes persists MCP results over 50,000 chars to a host file and leaves a
   **1,500-char preview**; Codex truncates MCP output to the model's `{limit:10000, mode:"tokens"}` policy, counts
   bytes at 4 B/token (≈40 KB) and cuts from the **middle**, keeping the head and tail of the JSON. Neither behaviour
   is visible in MCP, and neither generalises to other clients. The proof is arithmetic: the **30 s temporal level
   alone is 50,567 B** (473 rows × 43 sources), already past both ceilings before a single track, item, FX or
   measurement is added; the 300 s level is only 2,128 B. Any full panorama therefore needs page transport, and any
   "small" panorama is small because it dropped the 30 s grid. `[obs]` §R2.0, §R2.5.

**Recommendation `[dec]`:** canonical storage stays typed JSON (JCS, RFC 8785); the model-facing surface is one
**normalized, reference-based panorama** (entity tables + edges + columnar numeric tables + explicit state block),
delivered as **deterministic 24 KiB pages of the complete document** on open (10 pages at 241,342 B, 7 pages at the
169,285 B lossless-compact encoding; `document_hash` reassembly), as a revision-bound delta on change, as a
pinned-revision slice for intervals, and as a bounded rebuild on refresh. A 20,838 B variant that dropped the 30 s
temporal grid exists and is **rejected**; it is not the initial load and not a delta base. Invalidation follows the
real render graph and is keyed on `render_key`, never on audio hashes; intervals narrow `evidence.scope` while
`state.scope` stays whole-project. The normative contract is the next section; §3-§8 supply its evidence.

**Three claims from revision 1 are withdrawn.** (a) That a complete document can be delivered whole on first contact
— false on both named clients. (b) That the delta path is an optimisation whose worst case is "send the whole
document" — the worst case is now a paged rebuild, because the whole document may not fit either. (c) That anything
about the host's context handling is a dependency for correctness — the contract assumes no client memory at all and
treats full refresh as a normal operation (§R2.4).

**What it buys:** 78,104 of 241,342 compact bytes (32.4 %) are one repeated constant block or identity plumbing
re-derivable from fields (§3.6), and a two-change edit changed 298 of 11,966 leaves — none of them a track, item, FX,
route or automation fact. The semantic delta is a few hundred bytes, not 240 KB.

---

## Revision 2 — portable MCP contract (owner clarification 2026-11-13)

### R2.0 Why the delivery layer, not the delta algebra, had to change

Findings 1-6 and the archive audit stand. What did not stand was an unstated assumption in revision 1: that a complete
panorama, once built, is *delivered*. Two named clients falsify that, in different ways, and neither failure is
visible in the MCP protocol.

| Client / version | What happens to a large tool result | Consequence for a 241 KB panorama | Label |
|---|---|---|---|
| **Hermes 0.23.1** | MCP results above `mcp_result_size_chars = 50,000` are persisted to `$HERMES_HOME/cache/spillover/<id>.txt` and replaced in context by a `<persisted-output>` block carrying a **1,500-char preview**, the host path and recovery instructions; a 200,000-char per-turn budget spills the largest results first | The model sees ~0.6 % of the document plus a Windows path no other client can open. "Read the spill file" is a Hermes-only recovery path | `[obs]` `tools/budget_config.py:15,20`; `tools/tool_result_storage.py` |
| **Codex CLI 0.167.0-alpha.4.1** | MCP output is truncated to the model's `truncation_policy` (per-tool override `tools.<tool>.output_token_limit`); all ten models in the local catalog declare `{limit:10000, mode:"tokens"}`; the count is `bytes/4`; truncation is **middle** truncation — head and tail kept, middle replaced by a marker | At 1.85 B/token the document is ~129 K real tokens against a 10 K budget counted as 40 KB, so ~83 % of the bytes are dropped from the middle. What survives is the document's opening and closing keys — not a coherent subset of the project | `[obs]` `~/.codex/models_cache.json`; `codex-rs/core/src/tools/handlers/mcp.rs:295-298`; `codex-rs/utils/string/src/truncate.rs:6,80` |
| **MCP, rev 2026-07-28** | No maximum result size; no protocol-level session; servers needing cross-call state mint **explicit handles passed as ordinary tool arguments**; resources are application-driven and an update notification carries only a URI | Nothing in the protocol makes a large result arrive whole, and nothing lets a server replace a result already delivered | `[src]` S5, S40, S41 |

Three consequences structure the rest of this revision. (a) The contract must be defined over **ordinary tool calls
with string arguments**, because that is the only channel every conforming client implements. (b) Delivery must be
**page-sized**: a unit small enough that both named clients deliver it whole without configuration — a byte budget,
because the clients count differently and neither exposes a tokenizer to the server. (c) A response must be able to
**prove its own completeness**, because the server cannot observe what the client kept.

### R2.1 Lifecycle: open, changes-since, refresh

Three state operations and two accessors (page, slice), all over the same document contract. Names are provisional
until the comparison in R2.2 is settled; the argument lists show what each operation requires.

**Open** — `(project_id, expected_revision?, budget?, projection?)`.
Returns the complete unified panorama, paged by default, plus `delivery`, `handle`, `revision`, `fact_root`,
`schema_version`, `coverage`, `document_hash` and per-domain validity states.

- One revision for the whole response: no field is read at a later state than another.
- Every domain present; every entity that exists in the project appears exactly once at the chosen projection;
  `coverage` states expected/observed/unknown and validity per domain.
- `document_hash` is JCS-SHA-256 over the whole document, repeated on every page; `handle` is server-minted, opaque,
  ≥128 random bits, and binds `(project_id, epoch, fact_root, schema_version, projection)`. It is not a path, not a
  client name, not a mutable cursor.
- `expected_revision` mismatch → `stale_base` with the current revision and **no document**. This stops an open from
  silently binding to a state the caller did not intend (a human edited between `reaper_project` and the open).
- Default delivery is **page 0 of n** (manifest + first chunk); `delivery.complete` is true only when the whole
  document is in the response. The caller fetches the remaining pages by `handle` and verifies reassembly against
  `document_hash`. A response is never a shortened document: it is either the whole thing or an explicitly numbered
  page of one.

**Changes-since** — `(project_id, base_revision, handle?)` → `status ∈ {not_modified, delta, stale_base,
refresh_required}`.

- `delta`: `base_revision`, `target_revision`, `from_root`, `to_root`, `ops[]`, `unchanged{domain:{count,hash}}`,
  `inherited_from: base_revision`, `delivery`, new handle. Every op is a factual add, replacement or removal by entity
  ID and field path; evidence invalidation is an op (`set_state`), not a prose note. The op set is **complete** for the
  interval, not sampled, and there is no `better`/`worse` field anywhere by construction.
- `stale_base`: `base_revision` is not an ancestor of the head in the current epoch (divergent, rolled back, unknown).
  No delta is emitted, and the response carries the current revision so the caller can refresh deliberately.
- `refresh_required` with `reason ∈ {journal_gap, chain_too_long, schema_changed, epoch_changed,
  projection_evicted}`: the base is an ancestor but the chain cannot be served.
- **The handle is optional.** Revisions are project-global and totally ordered within an epoch, so `project_id` +
  `base_revision` is sufficient to compute a delta. The handle only lets the server verify that the caller also holds
  the matching document and hand back a projection-consistent slice. This is what makes handle loss survivable.

**Refresh** — `(project_id, expected_revision?, budget?, projection?)`.
An explicit full panorama, valid at any time, with or without a preceding error. Same envelope as open plus
`refresh_reason` and `rebuild_ms`. It rebuilds from canonical facts, not from the previous projection, so a corrupted
cache cannot propagate; `document_hash` is deterministic for the same `(fact_root, schema_version, projection)`.
Refresh is a **normal supported operation**, not a hidden recovery path: it is the correct call whenever the caller
cannot prove what it holds (R2.4).

**Evidence slice** — `(handle, window?|ids?|domain?)`.
An accessor, not a state operation and never a completeness claim. Served at the handle's pinned revision, so it is
consistent with the document the caller already has. `state` is the full project **structure** at full structural
resolution (every track, item, take, FX, route, automation/envelope, master chain and the time map, with IDs and
relationships), and both temporal grids are **declared available** (`temporal_levels: [{width_s:30, rows:473},
{width_s:300, rows:86}]`) without repeating their whole-song payloads. `evidence` is the window's assets and
measurements at full resolution. `coverage.not_in_scope` names the whole-song evidence that was not repeated.
Target ≤ 48 KB; larger windows page through the same mechanism.

### R2.2 API surface: one mode-based tool or three explicit tools

The lifecycle can be exposed two ways. Both are ordinary tools at the protocol level; the differences are schema
enforcement, model behaviour, and — decisively — per-tool client policy.

| Dimension | Single tool `panorama(project_id, mode=open\|changes_since\|refresh, base_revision?, …)` | Three tools `panorama_open` / `panorama_changes_since` / `panorama_refresh` |
|---|---|---|
| Model discoverability | one entry with an obvious name; mode semantics live only in the description; models default to whichever mode the description leads with | three entries sharing a prefix; names state intent; three tool slots instead of one, which matters on a 21-tool server |
| Schema reliability | `base_revision` is conditionally required, so it must be declared optional and enforced by prose; response is a union of document, delta and several error statuses — a shape most clients render as an opaque object | closed schema per tool: `base_revision` is required for changes-since and absent elsewhere; each response has one stable shape |
| Validation and error semantics | invalid mode, missing base, `stale_base` and `refresh_required` all arrive through one result union; the caller must read the body to know whether a retry is legal | distinct top-level outcomes; `refresh_required` is a normal response of changes-since; a missing base is rejected before the call executes |
| Cross-client behaviour | identical transport, but **per-tool client policy becomes impossible**: Codex attaches `tools.<tool>.output_token_limit` and an approval mode to a tool name, and Hermes attaches its spill threshold to a tool name — one mode-switching tool cannot carry different budgets for a 241 KB open and a 3 KB delta | per-operation policy works: a client can give `panorama_refresh` a large budget and a stricter approval mode, and `panorama_changes_since` a small one |
| Wrong-mode risk | high: `mode=changes_since` without a base forces the server to invent or reject; `mode=open` where the caller meant refresh is indistinguishable from a deliberate reopen; the mode is a free-form string in clients that do not enforce enums | structurally removed: changes-since cannot be called without a base, and open/refresh are unambiguous verbs |

**Recommendation `[dec]`:** three explicit tools. The per-tool-policy argument is the decisive one and it is not
aesthetic: the two named clients both bind budgets and approval to tool names, so a single mode-switching entry makes
"full open with a large budget, deltas with a small one" unexpressible. Required-argument enforcement at the schema
boundary is the second reason — it removes the most damaging wrong-mode error before it reaches the server.

One honest simplification: `open` and `refresh` are schema-identical (complete document at a revision) and could later
be merged without loss, since `refresh_reason` is the only distinguishing field. `changes_since` must stay separate
either way. `panorama_page` and `panorama_slice` are accessors rather than state operations and stay separate for the
same schema reason: a page index and a time window are different arguments with different validation. Provisional
names are used in this report; the comparison, not the names, is the recommendation.

### R2.3 What the client must carry — and the state table

The minimum durable state is **`project_id` + `revision`**. Everything else is an optimisation. A model can hold two
short strings in its working notes or pass them in its next call; no client feature is required, and no assumption is
made that it remembers the document itself.

| Client state | What the server can safely do | Call | Response |
|---|---|---|---|
| Fresh process, new chat, no state | nothing | open | page 0 + handle + revision, then pages 1..n |
| handle + revision, revision == head | verify both | changes-since | `not_modified`, no document |
| handle + revision, ancestor, chain intact | exact delta | changes-since | `delta` + new handle |
| revision only (handle lost) | exact delta | changes-since | `delta` + new handle |
| handle only (revision unreadable) | cannot identify a base | open or refresh | page 0 + pages |
| revision from a dead epoch | cannot map | changes-since | `refresh_required(epoch_changed)` |
| revision not an ancestor | cannot diff | changes-since | `stale_base` + current revision |
| no revision, no handle | nothing | open | page 0 + manifest, then pages 1..n |
| truncated or incomplete artifact | cannot verify | open/refresh with smaller `budget` | complete artifact |
| holds page *k* of *n* only | fetch by handle, then verify hash | `panorama_page(handle, i)` for the missing *i* | complete document |
| post-compaction summary quoting a revision | delta only if the string is exact and current; a paraphrased or truncated revision is not a base | changes-since, else refresh | delta or document |

The truncated-artifact, missing-page and post-compaction rows are the portable substitute for client introspection. A
model cannot measure its own context, but it can check the artifact it received: every response carries **at the head**
`delivery.sections[]` with per-section counts and `delivery.page = i/n`, and **at the tail** an end marker repeating
`document_hash` and `i/n`. A head-only
client (Hermes's 1,500-char preview) still sees the manifest; a head+tail client (Codex) sees both; a document whose
middle was cut fails the section check, and one whose tail was cut fails the sentinel. An artifact that fails either
check is **not state**, and must not be used as a delta base.

### R2.4 Mandatory refresh and delta verification

**Refresh is mandatory** — not optional, not silent recovery — in exactly these cases:

1. The client missed a delta, received one out of order, or received a duplicate it cannot order.
2. `base_revision` is unknown, divergent, or not an ancestor (`stale_base`).
3. `project_id` changed (Save-As, move) or the epoch changed (REAPER restarted, project reopened).
4. `schema_version` is incompatible with the client's document.
5. The journal cannot serve the chain (`journal_gap`, `chain_too_long`) or the handle's projection was evicted.
6. The received artifact fails its own completeness check (head manifest sections missing, or tail sentinel absent).
7. The client cannot state an exact prior revision — a paraphrase, a truncated string, or a compaction summary is not
   a base.
8. `client_root` does not match the root the server issued for that revision.

Case 8 is how a client verifies delta application **without hashing anything**. Every state call accepts an optional
`client_root`; the server compares it with the `document_hash` it issued for that revision and returns
`state_mismatch` (with `refresh_required`) on disagreement. The second, independent check is arithmetic: per domain,
`changed + unchanged = domain total`, so a lost or skipped op is detectable from the response itself (invariant I4).
A client that cannot perform either check must refresh; there is no "probably fine" state.

**No silent patch.** If the supplied base does not exactly match an ancestor, the server must reject rather than
best-effort merge: `stale_base` for a non-ancestor, `refresh_required` for an unservable chain. It must never apply a
delta to a document it cannot identify, and it must never return a delta whose `from_root` differs from the caller's
declared base. The server does not infer what the client missed; the client asserts its base and the server verifies
ancestry.

**Refresh is normal.** It can be requested at any time with `refresh_reason: "user_requested" | "scheduled" |
"budget_changed" | "recovery"`, and a caller that simply prefers a clean document should use it without inventing an
error. The operational cost is the point: a refresh is bounded and declared (page count, `rebuild_ms`), so prefer it
over reasoning about a doubtful delta.

### R2.5 The oversized first load — and why a compact single response cannot be the default

Three portable options were considered. The third one fails an audit, and the failure decides the default.

#### R2.5.1 Rejected: the 20,838 B reduced-grid experiment (owner decision 2026-11-13)

It is recorded only because the audit is the evidence for the rejection. It is **not a candidate**, not a fallback and
not a default, and it must not be reintroduced without an explicit owner decision.

| Required fact | In today's full document | In the 20,838 B prototype | Verdict |
|---|---|---|---|
| 9 metrics per source | yes, 43 × 9, full precision | yes, 43 × 9 at 3 dp | retained, precision reduced |
| metric units | `analysis_metric_units` (237 B) | absent | **omitted** |
| analysis method / version / params | absent everywhere (§3.1 gap) | absent | **missing in both** — must be added to the full document |
| stage | per-asset `render_provenance`, 2 distinct values | one derived column | retained only if the hoisted block keeps per-row exceptions |
| channel view | absent | absent | **missing in both** |
| exact window | `audio_file_window`, 1 constant × 43 | start/end per row, 3 dp | retained |
| render provenance (settings, sample rate, channels, tail, plugin versions) | 255 B × 43, 2 distinct values | `stage` + one tail column | **omitted** (settings, sample rate, plugin versions) |
| capture validity (`capture_status`, `master_validation`) | absent on the read path (finding 2) | one literal `"valid"` | **omitted in both** — must be added |
| coverage | counts, `complete:true` even for an invalid capture | copied counts | reduced to counts; cannot express validity |
| uncertainty / epistemic layers | structurally `[]` | absent | **omitted** |
| per-entity relationships | 333 edges + integer legend | implicit foreign keys only | reduced: structural edges derivable; `measured_by`, `belongs_to_capture` and non-FK relations lost |
| time map | 1,651 B, all points | 319 B, first 4 points (prototype shortcut) | **truncated in the prototype** — must keep all |
| tempo / time signatures | yes | yes | retained |
| automation / envelopes | 827 + 825 B, duplicated domain | absent | **omitted** |
| MIDI | 0 rows in this capture | absent | **omitted** (matters when non-empty, 10 k-note cap) |
| markers / regions | 0 rows in this capture | absent | **omitted** (matters when non-empty) |
| temporal resolution levels | 30 s (473 rows, 50,567 B block) + 300 s (86) | 300 s only (86) | **30 s level omitted** |
| float precision | 17 significant digits | 3 decimal places | reduced; no tolerance declared |
| stable identity mapping | `stable_id_legend` (16,479 B) | aliases inlined, legend dropped | **identity broken**: `id1` without the legend is not a GUID |
| per-asset constants | 32 KB repeated | hoisted | retained only if the block is complete — the prototype's block is not |
| `per_track` counts | 7,020 B | absent | reduced (derivable by counting rows) |
| `truncation`, `selection`, `transport`, `domain_counts` | 334 B | absent | **omitted** |
| `audio_time_contract`, `audio_units`, `audio_capture_revisions` | 1,253 B | absent | **omitted** |
| `project`, `capture_binding` | 4,613 B | absent | **omitted** |
| `analysis_archive`, `provenance`, record pointers | 899 B | absent | **omitted** |

Seventeen required facts are omitted, five are reduced, one is broken (333 `crosslinks` are replaced by foreign keys,
which preserve only the edges derivable from `track_id`/`item_id`/`owner_id`/`source_id`). The decisive omission is the
temporal grid: the 30 s level is 50,567 B of the full document and the prototype carries only 86 rows of 300 s
summaries, so nine tenths of the time resolution disappears from the first view. **Rejected by the owner** for exactly
that reason, and because units, uncertainty, provenance, validity, automation, `per_track` and the full relation set
are also absent. It is a lossy preview, not a panorama.

#### R2.5.2 Measured lower bound for a lossless single response

The decisive measurement is one domain: the **30 s temporal level is 50,567 B** (473 rows, 43 sources). That single
level already exceeds Hermes's 50,000-char MCP spill threshold and Codex's ~40 KB byte counter, before tracks (17,106 B),
items (12,597 B), FX, audio (72,521 B) or analysis (11,129 B) are added. The 300 s level is 2,128 B. **No
semantics-preserving single-response full panorama can fit either named client, even with perfect field dedup —
page transport is mandatory for the full contract**, and any panorama small enough for one response is small only
because it dropped the 30 s grid.

Measured on the same capture by hoisting the repeated per-asset constants (`project_mapping` 20,468 B, `audio_file_window`
7,095 B, `format` 4,816 B, `render_provenance` variants), dropping `per_track` (7,020 B, derivable), deduplicating
derivable boilerplate and duplicate crosslinks — **while keeping every numeric value at full precision and both
temporal levels**:

| Variant | Bytes | Share of full (241,342 B) |
|---|---|---|
| lossless, crosslinks kept | 187,610 | 77.7 % |
| lossless, derivable crosslinks dropped | **169,285** | **70.1 %** |
| lossless, inline canonical IDs | 181,930 | 75.4 % |

Even the best variant is **4.2× Codex's ~40 KB byte counter, 3.4× Hermes's 50,000-char spill threshold and 1.7× the
100,000-char generic default**. So the answer to the decisive question is **no**: no semantics-preserving single
response fits a stock client, and the true lower bound is ~169 KB, not 20 KB. Encoding buys 30 %; it cannot buy 85 %.
`[obs]` measurement, `[inf]` generalisation to larger projects.

#### R2.5.3 Corrected default: one panorama, delivered as pages

**Default `[dec]`:** `panorama_open` returns **page 0 = manifest plus the first 24,576 B chunk**;
`panorama_page(handle, i)` returns page *i* for *i* = 1..n-1. That is **10 pages** for the current 241,342 B document
and **7 pages** for the 169,285 B lossless-compact encoding.

Pages are one logical panorama, not ten documents:

- same `project_id`, `revision`, `fact_root`, `schema_version` and one `document_hash` on every page;
- fixed canonical order (sections sorted once, never per response), so page *i* is byte-identical across calls;
- page 0 carries the manifest: `total_pages`, `total_bytes`, per-page `page_hash`, section→page map, and the list of
  sections in the document;
- reassembly is the concatenation of page payloads in order; its hash must equal `document_hash`;
- **no edits and no whole-project claim before all pages are received and the hash verifies.** A caller that must act
  earlier uses `expected_revision` and refreshes afterwards. The server cannot enforce possession, but the rule is part
  of the contract's usage and is testable at the client harness level.

**What the model actually sees.** From page 0 alone: the manifest and the first sections (project, capture binding,
coverage) — not the whole project. From the reassembled document: everything the full panorama contains. From an
explicit `panorama_slice`: the whole-project state plus one window's evidence. There is no state in which a partial
document is presented as the whole.

**Lossless compact encoding is permitted, lossy projection is not.** `encoding:"compact"` may only re-encode the same
document — every numeric value at full precision, both temporal levels, provenance, units, validity, coverage,
uncertainty and relations preserved — and it still pages. The 20,838 B variant fails this test and is rejected
(R2.5.1).

**Why 24 KiB:** it is 13.3 K real tokens at the measured 1.85 B/token but only ~6 K on Codex's `bytes/4` counter, and
it is half of Hermes's 50,000-char spill threshold. A page survives both clients with margin, and the manifest makes a
missing page detectable.

**What the server cannot do:** know the client's cap. `budget` is a request, not a guarantee; a client with an 8 K token
cap will still cut a 24 KiB page. That is why the head manifest and tail sentinel exist, and why an artifact that fails
the completeness check resolves to a refresh rather than a retry of the same call. This is a real limit of the portable
contract, not something to engineer away.

### R2.6 What the server guarantees / what no host can be forced to do

| Server guarantees, independently of host | Not guaranteed by MCP or any host |
|---|---|
| G1 identity: stable entity IDs, project/instance/epoch separation, handle binding | that the client retains an earlier tool result (Hermes spills it, Codex cuts it, both compact history) |
| G2 immutable archive: `cap_`, `rev_`, assets never mutated | that an earlier conversation message can be replaced by a newer one |
| G3 current state at one revision per response | that the model attends to every field it received |
| G4 revisions: append-only, parent-chained, totally ordered per epoch | that a notification is delivered (Hermes 0.23.1 has no `resources/updated` handler; the protocol message carries only a URI) |
| G5 content hashes: JCS-SHA-256, reproducible `document_hash`/`fact_root` | that a response of arbitrary size is accepted |
| G6 factual deltas: complete op set between two roots, roots on every response | that the client applies a delta correctly, or at all |
| G7 dependency invalidation: deterministic rule set, explicit states | — |
| G8 interval consistency: absolute buckets, whole-project state, narrowed evidence | — |
| G9 bounded full refresh: rebuild from facts in bounded time, declared page count | — |

The right reading is that **correctness is recoverability, not residence.** The server's obligation is to make the
client's state verifiable (`document_hash`), the delta exact (`from_root`/`to_root`), and the loss recoverable
(bounded full refresh). Whether the model still "has" the panorama at turn 40 is a property of the host; the contract
is designed so that the answer changes no guarantee.

### R2.7 Interval evidence in the same contract

`panorama_slice(handle, window:[36,45))` replaces any notion of a separately injected interval context. One ordinary
tool call, identical for every client, served at the handle's revision:

- `state` = full project **structure** at full structural resolution: every track, item, take, FX slot, route,
  automation/envelope, the master chain and the time map, with entity IDs and their relationships. Both temporal grids
  are **declared** (`temporal_levels` with width, row count and availability) but their whole-song bucket payloads are
  not repeated — repeating them would make a nine-second question cost ~169 KB and defeat the accessor.
- `state.scope` stays `project`; `evidence.scope` becomes `[36,45)`.
- `evidence` = the window's assets and measurements at full resolution, each carrying units, method, version, stage,
  channel view, exact window, render provenance and validity; each source also carries `content_in_window ∈
  {content, no_content_in_window, silent_in_window, not_captured}` — four states, because "no item here", "item but
  silent" and "never captured" are different facts (§7.2).
- `coverage.not_in_scope` lists the whole-song evidence that was deliberately not repeated (for example
  `temporal/30s/rows[0..472]`, other tracks' window measurements), so absence is declared, never implied.
- Target ≤ 48 KB; a larger window pages through the same page mechanism rather than degrading resolution.
- Buckets are absolute (`[k·w, (k+1)·w)`), never anchored at the first sample (§7.2, correction retained).
- Resolution comes from a global ladder and is stated; a step-down is explicit and cross-width comparison is
  `incomparable`.
- No client-side injection and no resource: the entire interval behaviour is the `window` argument.

### R2.8 Generic flow, end to end

1. `reaper_project` → `revision: R0` (or skip and open without `expected_revision`).
2. `panorama_open(project_id, expected_revision=R0)` → page 0 + manifest of the full document, `handle H0`,
   `revision R0`, `document_hash D0`; `panorama_page(handle, i)` for the remaining pages, then verify the hash.
3. Model edits with `reaper_edit(expected_revision=R0)`; the server applies, appends `rev R1` and `chg`.
4. `panorama_changes_since(project_id, base_revision=R0)` → 3 ops with `from_root`/`to_root`, `handle H1`,
   `revision R1`. The model notes `(project_id, R1)`.
5. Fifty turns and several edits later: `panorama_changes_since(project_id, base_revision=R1)` → contiguous chain →
   delta R1→R7 with `chain:[…]`. Over the limit → `refresh_required(chain_too_long)` → `panorama_refresh`.
6. Compaction dropped the panorama but kept the note `(project_id, R7)`: the model has a revision but **no document to
   apply a delta to**. It must `panorama_refresh(expected_revision=R7)`, not changes-since. This is the one rule the
   client prompt has to state, and it is checkable: "do I still hold the document at `from_root`?" If not, refresh.
7. Client restart: no handle, no revision → `panorama_open`; history is intact server-side, only the client's copy was
   lost.
8. Two clients (Codex and Hermes) on one project: each carries its own `(handle, revision)`; the server serves each its
   own chain; neither can invalidate the other; concurrent edits are serialized by `expected_revision`, and the loser
   gets `stale_base` with the new revision rather than a silent overwrite.
9. Human edit between two calls: no notification arrives; the next `panorama_since` returns the change with
   `attribution:"external"`, and the model sees exactly what it missed.

### R2.9 Acceptance criteria for the contract

1. Every response either matches the client's `from_root` exactly or is a full refresh; there is no third state.
2. A delta applied to the document at `from_root` reproduces `document_hash(to_root)` byte-for-byte.
3. Every response names what it did not re-send (`unchanged` counts + hashes) and what it reduced (`delivery.reduced`).
4. A response that lost its middle is detectable from the head manifest; one that lost its tail from the sentinel.
5. `panorama_slice` never returns data from another revision, never omits whole-project structure, declares
   `coverage.not_in_scope`, and stays ≤ 48 KB or pages.
6. `panorama_refresh` succeeds with no client-side state and no client-specific feature.
7. Every page ≤ 24,576 B; concatenating page payloads 0..n-1 reproduces `document_hash` byte-for-byte; a missing page
   is named by the manifest.
8. Removing Hermes entirely changes no guarantee in R2.6.
9. No required fact is absent from the reassembled initial load: all 9 metrics with units, method/version/params,
   stage, channel view, window, render provenance, capture validity, coverage, uncertainty, automation, time map,
   both temporal levels, entity relations, and the provenance block.
10. A response claiming `projection:"full"` that carries only the 300 s level, rounded floats in place of full
    precision, or a subset of the domains **fails**; a compact encoding is acceptable only if every numeric value and
    both temporal levels survive.
11. Page loss or `page_hash` mismatch forces `panorama_refresh`; a reassembled document that does not hash to
    `document_hash` is never used as a delta base.

---

## 2. What "incremental" must mean here

Three artifacts are routinely conflated and must be separated in both the schema and the API `[dec]`:

| Artifact | Lifetime | Owner | Mutable? | Identity |
|---|---|---|---|---|
| **Immutable archived capture / revision journal** | permanent, append-only | archive | never | `cap_`, `rev_`, `fact_root` |
| **Server-maintained current projection** | per `(project, fact_root, evidence_set)` | server | rebuilt, never edited | `document_hash`, `schema_version` |
| **Client-held panorama state** | until the client loses it | client/model | replaced only by open or refresh | `(handle, revision/fact_root)` |
| **Delivery** (document, delta, page, slice) | one tool result | MCP | n/a | `from_root`/`to_root`, `page i/n` |

The client-held row is the one revision 1 under-specified. It is not a server object and the server cannot inspect it.
The contract therefore treats it as an opaque pair of strings the caller may or may not still have, and every response
is written so that either case is safe: deltas name `from_root` and `to_root`, responses state which facts they did
not re-send, and a full refresh is a first-class call rather than a hidden recovery path.

"Saving tokens" is not the same as "removing tokens from the model's active conversation". A delta avoids *sending*
unchanged facts again; it does not delete the earlier full panorama from the conversation history, and this report no
longer needs to know whether any host can evict it. If the client loses the document, loses the handle, loses the
revision, or receives a truncated result, the portable answer is the same one the caller can always take:
`panorama_refresh`.

This is also why the owner's "one panorama" requirement is compatible with incremental transport: *one document
contract*, optionally transmitted as a patch. The contract is the invariant; the transmission is an optimisation.

---

## 3. Current panorama audit

### 3.1 What is built, from what, and with which identity

`build_panorama` (`panorama.py:1588`) reads a `FullCaptureRecord` mapping and emits roughly forty top-level keys
(`panorama.py:2011-2241`). Coverage is asserted by counting (`panorama.py:1439-1540`). The two model-facing reads are
not equivalent: `reaper_capture_snapshot` returns `{capture_status, revision, time_window, panorama,
master_validation, master_attempts, timings}` (`daw/full_capture.py:847-879`), while `reaper_capture_read` returns
`{panorama, panorama_source, capture, retention_status}` (`mcp_server.py:1794-1811`) — the validity evidence is
in the first and absent from the second. There is no MCP tool that lists captures or compares two of them; the archive
has `list_index()` (`context/full_capture.py:864-869`) but exposes it to no caller.

| Domain | Built from | Identity | Model gets | Main loss |
|---|---|---|---|---|
| `tracks` (42) | bridge context | `track:<guid>` | name, index, fader/pan/mute/solo, counts, audio/analysis ids | pan law, automation mode |
| `master` | context | `master:<guid>` | same shape | same |
| `items` (34) | `project_map.items` | `item:<guid>` | position, length, take_count, active take | **drops** `start_offset_s`, `playrate`, fades, `group_id`, `mute`, `channel_mode`, `loop_source`, stretch markers (`panorama.py:203-216`) `[obs]` |
| `takes` (34) | active take only | `take:<guid>` | name, is_midi, source path | inactive takes; `track_id` null on all rows |
| `midi` | `project_map.midi` | `midi:<take_guid>` | notes ≤10,000, truncation flag | what the map captured only |
| `fx` (15) | track/master FX | `fx:<owner>:<slot>:<guid>` | name, enabled, offline, parameter_count | **no parameter values**; only 5 distinct pointers for 15 rows |
| `routes` (40) | digest track rows | `route:<src>:<dst>:<idx>` | gain, pan, mute | pre/post, send mode, channel mapping |
| `automation` + `envelopes` | the same digest object twice | two IDs per envelope (`panorama.py:441`) | points, shape, tension | duplicated domain |
| `time_map` | tempo markers + signatures | `time_map:<capture_id>` | points, signatures, revision | capture-scoped identity |
| `audio` (43) | assets | `audio:<source_guid>` | windows, mapping, hash, stage | 37 KB of constants repeated 43×; no validity |
| `analysis` (43) | reports | `analysis:<source_guid>` | 9 metrics, optional curves | no method/version/window/stage/uncertainty |
| `analysis_temporal` | RMS curves | positional `source_index` | 2 bucket levels, quantiles | index rows; per-source grid anchor |
| `crosslinks` (261) | all of the above | integer relation codes | structural edges only | no audio relations (round-5 G6) |
| `coverage` | counts | n/a | expected/observed/status | `complete` can be true for an invalid capture |

### 3.2 Identity defects, with consequences for deltas

**What the IDs mean.** `capture_id` names one capture run; it never names a project state. A revision names a content
state and is not unique per capture. They are orthogonal: several captures can share one revision (different windows,
channels, sample rates), and one capture spans two revisions (`revision_before` for the audio, `revision_after` for the
post-capture state). `change_count` is neither: it moves on temporary capture mutes and can reset on reload
(§3.4 shows 254 → 2 with identical content). A delta therefore needs `(from_rev, to_rev, from_root, to_root)`, not two
counter values.

- **Project identity is path-bound.** `project_id` embeds the `.rpp` path (`context/revision.py:172-175` docstring;
  `[obs]` archive `project_id` contains the full path). Save-As or moving the project therefore changes project
  identity; a journal must record an explicit `moved_from`/lineage edge or it will report a new project and lose
  history. `[inf]`
- **`time_map:<capture_id>` cannot be diffed.** Every capture produces a new ID for an unchanged tempo map. The
  86→254 diff shows exactly one changed leaf under `/time_map`: `revision/change_count`, although the tempo points
  are identical `[obs]`. Stable identity must be `tm_<digest(tempo points + signatures)>`.
- **`analysis:<source_guid>` is method-blind.** Re-analysing a source with a different algorithm, FFT size or hop
  keeps the same ID, so a delta by ID compares unlike numbers. Measurement identity must include method, version,
  params and window; a separate `series_key` should carry the *comparison* identity (§5.2).
- **Aliases and array indices are not identity.** `_compact_stable_ids` numbers IDs by first-appearance order
  (`panorama.py:1549-1585`), so an earlier insertion renumbers every later alias; `analysis_temporal.rows[0]` is a
  source array index (`panorama.py:1112-1115`), so inserting a track rewrites the first column of all 473 rows at
  the 30 s level. The two full-song captures checked happen to resolve `id0/id3/id50/id100` identically `[obs]` —
  which is the trap: aliases look stable until the structure changes.

### 3.3 Coverage vocabulary does not distinguish "complete" from "valid"

`_coverage` only compares expected vs observed counts (`panorama.py:1485-1515`). It has no notion of a failed
capture, a stale asset or an incomparable metric. Consequences verified in the archive:

| Claim | Payload | Evidence |
|---|---|---|
| `coverage.complete: true` | master metrics `[0.0, null, null, -160.0, …]` for an `invalid_master` capture | `cap_01M7A9…` `[obs]` |
| `takes.status: complete` | all 34 takes lack `track_id`, `per_track[].counts.takes = 0` | `cap_01M7AAD…` `[obs]` |
| `epistemic_layers.uncertainty.measurement_notes: []` | per-metric notes exist in the record but compact views drop them | `panorama.py:2191-2205` + `[obs]` |
| `render_provenance.complete: true` | means "method+stage+revision+solo field present", not "capture valid" | `panorama.py:1278-1282` `[obs]` |

A model cannot tell "the master is silent" from "the master render failed". That is the exact failure class the
owner's acceptance gate 5 forbids.

### 3.4 What actually changes between captures

Two adjacent captures of the same project, change counters 254 → 2 (the project was reloaded; the counter reset),
`digest_fields_equal = true` in both:

| Measurement | Result |
|---|---|
| Leaf nodes compared | 11,966 vs 11,966 |
| Changed / added / removed leaves | 298 / 3 / 3 |
| Sum of changed leaf values | ~13,970 bytes |
| `tracks`, `items`, `takes`, `fx`, `routes`, `automation`, `master`, `tempo` | **byte-identical** |
| Audio rows with identical SHA-256 | **0 of 43** |
| Analysis metric arrays differing | **1 of 43** (the invalid master) |
| Temporal rows differing | 11 of 473 (30 s), 2 of 86 (300 s) |
| Non-project churn | capture_id, record path, record hash, `captured_at`, revision counters, `document_sha256`, `stable_id_legend/id0`, `transport.play_position_s` |

`[obs]` recomputed from `cap_01M7A9…` and `cap_01M7AAD…`. Three conclusions follow. (a) **A delta must be defined
over facts, not over serialized bytes**, or it reports ~300 changes for zero project changes. (b) **Asset hashes are
not a change detector**: identical digested state produced 43 new hashes. (c) **Ephemeral state leaks**: the playhead
position and build timestamp are inside `document_sha256` (`panorama.py:2246-2248`), so the document hash can never
serve as a content root.

### 3.5 Byte and token evidence (reproducible)

Command: `python` over `captures/*/panorama.json` with
`json.dumps(sort_keys=True, separators=(',',':'))`.

| Capture | Tracks | Assets | Compact bytes | Sidecar bytes | `estimate_tokens` |
|---|---|---|---|---|---|
| `cap_01M7923…` (full song) | 41 | 42 | **237,477** | 388,654 | 107,945 |
| `cap_01M79659…` (20 s) | 41 | 42 | **198,480** | 291,445 | 90,219 |
| `cap_01M7AAD…` (full song) | 42 | 43 | **241,342** | 395,336 | 109,701 |

The first two rows reproduce `docs/FULL-CAPTURE.md:125-134` exactly `[obs]`. Budget arithmetic `[obs]`:

- `700,000 / 2.2 = 318,182` estimated tokens, so the byte cap alone always exceeds the token cap.
- `256,000 × 2.2 = 563,200` bytes is the effective ceiling — 136,800 bytes below the documented byte cap.
- Documented measured ratio: `237,477 / 128,449 = 1.8488` bytes/token. At that ratio the effective ceiling is
  ~304,600 tokens, i.e. the "conservative" estimator is 19 % optimistic against the only tokenizer that was measured.
- A 9 s interval at 10 Hz for 43 sources is 3,870 points (~70 KB), so intervals are cheap; a whole-song full-rate
  RMS table is 43 × 3,005 = 129,215 points (~2.3 MB at ~18 B/value), so it **cannot** fit one response. Resolution
  must follow scope (§7).

These are Music Flamingo tokenizer measurements quoted from the project doc, **not** Hermes/model tokens and not
reproducible here (no tokenizer installed). They are used only to show the arithmetic inconsistency, not as a Hermes
estimate.

The same measurement also shows the estimator is serialization-dependent: the *same* document pretty-printed as the
sidecar is 388,654 bytes / 164,980 tokens = **2.356 B/token**, versus 1.8488 for the compact form `[obs]` derived from
`docs/FULL-CAPTURE.md:127-131`. Whitespace and punctuation change the ratio by 27 %, so a byte ceiling calibrated on one
serialization cannot be carried to another, and `utf8_bytes/2.2` is not a tokenizer substitute in either direction.
That is why the recommendation is to measure the actual client tokenizer once (Workstream A, Q5) and then keep the
budget in *tokens with a measured ratio per serialization*, not in bytes.

### 3.6 Duplication and path-bound data

| Block | Bytes | Share | Distinct values across 43 audio rows |
|---|---|---|---|
| `audio[].project_mapping` | 20,468 | 8.5 % | **1** |
| `audio[].audio_file_window` | 7,095 | 2.9 % | **1** |
| `audio[].format` | 4,816 | 2.0 % | **1** |
| `audio[].render_provenance` | 10,935 | 4.5 % | **2** (track / master) |
| `crosslinks` | 18,311 | 7.6 % | 261 edges; 129 of them (`has_audio`, `measured_by`, `belongs_to_capture`) are derivable from fields |
| `stable_id_legend` | 16,479 | 6.8 % | 261 aliases |
| **Total** | **78,104** | **32.4 %** | |

`[obs]`. Path-bound fields: `capture_binding.project_path`, `project.path`, `provenance.record_path`,
`analysis_archive.record_path`, `audio.archive_audio` and every `raw_series_pointer`. The last group is a
record-relative JSON Pointer (`#/analysis/{guid}/curves/...`) into a 445 MB `record.json` that the model is
explicitly not supposed to read — a pointer the consumer cannot follow is provenance theatre, not provenance.
Round-5 already established the item-field loss (D5-14); this section prices it.

---

## 4. Representation comparison

Evidence on format comprehension is **mixed and model-dependent**, and the strongest recent study finds format is
not the dominant factor:

- 9,649 experiments, 11 models, four formats (YAML, Markdown, JSON, TOON): **no significant aggregate accuracy
  effect** (`χ² = 2.45, p = 0.484`); model tier accounted for a 21-point gap; file-based retrieval helped frontier
  models (+2.7 %) and hurt open-source models (−7.7 %) `[src: arXiv 2602.05447]`.
- No reliable Markdown advantage, recall near ceiling to 64-128 k tokens then sharp model-dependent decay, and
  **+22 % to +37 % token overhead** for structured formats over plain text `[src: arXiv 2607.19257]`.
- Prompt-template changes moved GPT-3.5 by up to 40 % on code translation while GPT-4 was robust
  `[src: arXiv 2411.10541]`. Table comprehension varies with format, content order and partition marks
  `[src: arXiv 2305.13062]`.
- JSON tool-output processing remains hard even for frontier models, and the best strategy depends on output size and
  reasoning complexity `[src: arXiv 2510.15955]` — directly relevant, because the panorama *is* a tool output.
- Graph encoding choice changes accuracy by **4.8 %-61.8 %** depending on task and graph structure, with no single
  winning encoder `[src: arXiv 2310.04560]`.
- Token-optimised notations save 18-27 % of tokens but cost 9-14 accuracy points in end-to-end agent loops, and one
  cascades parse failures across turns `[src: arXiv 2605.29676]`; plain JSON wins one-shot generation accuracy
  `[src: arXiv 2603.03306]`. Columnar "schema once, data many" reports **46-51 % token reduction** with no material
  comprehension loss on lookup/count/extract/aggregate `[src: arXiv 2604.17512]`.

| Option | Optimises | Evidence | Main risk | Verdict |
|---|---|---|---|---|
| Canonical typed JSON | fidelity, standards, hashing | RFC 8785 canonicalisation; JSON Pointer/Patch tooling `[src]` | verbose | **keep for storage** `[dec]` |
| Normalised linked JSON (entity tables + edge table) | no duplication, stable refs, patchable | our 32.4 % redundancy; `[src]` format-neutral | client must resolve refs | **model-facing default** `[dec]` |
| Flattened record list (one row per entity) | streaming, per-row patch | RFC 6902 addressability `[src]` | nesting lost | use as the patch target `[dec]` |
| Graph/event view | relationships, causality | encoding effect 4.8-61.8 %, no dominant encoder `[src]` | forces adjacency traversal; worse for scalar lookup | keep **edge list**, not pure graph `[dec]` |
| Timeline matrix / columnar | dense numeric series | 46-51 % token cut `[src]`; already used for RMS | index addressing, grid anchoring | **numeric tables only** `[dec]` |
| YAML / TOON / textual DSL | brevity | no aggregate gain; 9-14 pt loss in agent loops `[src]` | parse fragility, no canonical form | reject as primary `[dec]` |
| Markdown prose | readability | no reliable advantage; overhead `[src]` | loses typing/precision | secondary summary only `[dec]` |

**"Native" cannot mean unmediated.** The only native channels are audio resources (the model may or may not actually
hear them; round-4 Wave D boundary) and tool calls. Everything else is text the model reads. So the choice is not
"JSON vs native perception"; it is "which text, with which identities, and what is left out".

**Recommendation `[dec]`:** one hybrid document — canonical machine record stays JSON; the model view is JSON too,
but normalised: `entities` (typed rows, stable IDs, no repeated constants), `edges` (one array, relation names not
integer codes), `tables` (columnar with an explicit column header and absolute time axis), `states` (validity,
staleness, coverage, uncertainty), `refs` (pointers that the client can actually resolve, e.g. capture id + byte
range, not a path into a 445 MB file). JSON is retained because the general-purpose alternatives buy 18-27 % of tokens
at a measured 9-14 point accuracy cost in agent loops, while columnar encoding (adopted here for numeric tables only)
buys 46-51 % with no material comprehension loss, and normalisation buys ~32 % on this corpus with **no** comprehension
risk — it removes duplication, not information.

**Can one structurally single panorama hold graph links, lanes, entities, uncertainty and multi-resolution
summaries without manual unfolding?** Yes, if "unfolded" is taken literally: the document is one JSON object whose
sections all resolve internally — entity rows carry their IDs, the edge array joins them, the lane/timeline table
gives per-track intervals, the numeric tables carry their own column headers, and the state block qualifies every
value. Nothing requires a second tool call. What makes today's document *feel* unfolded is not nesting but two
indirections: the `stable_id_legend` alias layer (`id0…idN`) and integer relation codes (`relation: 3`), both of which
force the model to decode before it can reason. Removing those two indirections — full IDs in place, relation names in
place — is a smaller change than any format migration and addresses the actual comprehension cost. `[dec]`

---

## 5. Incremental architecture

### 5.1 Layers

```
L0  REAPER project (single writer: one REAPER instance)
L1  revision journal      rev_ records, append-only, one per accepted content state
L2  object facts          per-entity fact rows (hash-addressed), object identity = REAPER GUID
L3  evidence              capture assets (cap_, ast_sha256_), measurements (mea_), annotations (ann_)
L4  active projection     deterministic panorama at (project, fact_root, evidence_set)
L5  delivery              whole panorama | panorama delta (chg_) | interval evidence overlay
```

L1-L3 are durable; L4 is a cache that can always be rebuilt; L5 is disposable. This mirrors the event-sourcing +
materialised-view split that is now also being applied to LLM agents: the agent emits structured intentions, a
deterministic orchestrator validates and appends events, and a verifiable materialised view is projected from the log
`[src: arXiv 2602.23193]`. It also matches the older "record facts, then derive" formulation
`[src: Helland, CIDR 2015]`.

### 5.2 Stored entities and identities

| Entity | ID | Identity rule | Notes |
|---|---|---|---|
| Project | `prj_` | bridge `project_id` | path-bound; add `lineage` edge on save-as `[obs]` |
| Instance | `pri_` | bridge session id + process epoch | separates two open copies |
| Revision | `rev_` | `derive_id(project, epoch, counter, fingerprint)` | `counter` diagnostic only (`revision.py:161-164`) |
| Track/master | `trk_`, `trk_master_` | REAPER GUID | index is a hint, never identity |
| Item/take/source | `itm_`, `tak_`, `src_` | GUID; take falls back to source hash | fixes the null `track_id` `[obs]` |
| FX | `fx_` | owner GUID + FX GUID (slot is an attribute) | slot moves must not mint a new identity |
| Send | `snd_` | source + destination + send index | pre/post and mode are attributes |
| Envelope | `env_` | owner + kind + index | automation and "envelopes" are one domain, not two |
| Time map | `tm_` | sha256 of tempo points + signatures | replaces `time_map:<capture_id>` |
| Capture | `cap_` | authored ULID | one capture run |
| Asset | `ast_sha256_<hex>` | file bytes | content-addressed, already enforced |
| **Measurement** | `mea_` | `derive_id(source, method, method_version, params_hash, stage, channel_view, window, revision)` | new: exact identity |
| **Series** | value, not ID | `sha256(source, metric, method_family, window, stage, channel_view)` | new: the key that pairs before/after |
| Annotation | `ann_`, `clm_`, `obs_` | authored | never mixed into measured facts |
| Change | `chg_` | authored per delta | carries `from_root`/`to_root` |

The **measurement/series split is the single most important schema change** `[dec]`. Exact identity answers "which
numbers are these"; series identity answers "may these two numbers be compared". Without the split, a method upgrade
either silently reuses the old ID (false delta) or changes every ID (no pairing).

### 5.3 Revision record

```json
{"record_kind":"revision","rev_id":"rev_…","project_id":"…","instance_id":"pri_…","epoch":2,
 "counter":254,"fingerprint":"sha256:…","fingerprint_scope":"full","parent":"rev_…",
 "fact_root":"sha256:…","domain_hashes":{"tracks":"…","items":"…","fx":"…","routes":"…",
 "automation":"…","tempo":"…","master":"…","project":"…"},
 "fact_coverage":{"covered":["tracks","items","takes","fx","sends","tempo","master",
 "automation","project","fx_state"],"missing":[],
 "uncovered_presented_fields":["channel_count","track_name","item_group_id","item_channel_mode","pan_law"]},
 "capture_ids":["cap_…"],"created_at":"…","producer":"…"}
```

`fact_root` is a Merkle root over sorted `(entity_id, entity_fact_hash)` pairs; `entity_fact_hash` is JCS over the
entity's normalised fact row. One changed field changes exactly one branch, and an unchanged subtree keeps its hash
across revisions, so "unchanged" is provable rather than asserted. `uncovered_presented_fields` is deliberately part
of the record `[dec]`: it turns the §3.3 problem into data, so a reader can see that `channel_count` is displayed but
not fingerprinted instead of trusting a completeness flag.

### 5.4 Delta record

```json
{"record_kind":"change","chg_id":"chg_…","project_id":"…","from_rev":"rev_A","to_rev":"rev_B",
 "from_root":"sha256:…","to_root":"sha256:…","from_capture":"cap_A","to_capture":"cap_B",
 "attribution":{"kind":"operation","op_ids":["op_…"],"plan_ids":["pln_…"]},
 "compare":{"project":"same","stage":"same","channels":"same","sample_rate":"same",
            "window":"same","method":"same","tempo_map":"same","verdict":"comparable"},
 "ops":[{"op":"replace","entity":"trk_{…}","field":"mixer/volume","before":0.56,"after":0.63,
         "unit":"linear","state":"changed"},
        {"op":"add","entity":"itm_{…}","row":{…},"state":"added"},
        {"op":"remove","entity":"itm_{…}","state":"removed"},
        {"op":"set_state","entity":"ast_sha256_…","field":"state","before":"fresh",
         "after":"stale","reason":"master_fx_changed"}],
 "unchanged":{"tracks":{"count":42,"hash":"…"},"items":{"count":34,"hash":"…"},
              "fx":{"count":15,"hash":"…"},"routes":{"count":40,"hash":"…"}},
 "measurements":[{"series":"…","before":-14.2,"after":-14.4,"delta":-0.2,"unit":"LUFS",
                  "state":"comparable","window":[0,300.5],"method_version":"…"}],
 "incomparable":[{"series":"…","reason":"method_version_changed","before":"1.0","after":"2.0"}],
 "audio":[{"source":"trk_{…}","before":"ast_sha256_…","after":"ast_sha256_…",
           "hash_equal":false,"render_key_equal":true,"state":"rerendered_same_content"}],
 "states":{"invalid_captures":["cap_A"],"stale":["ast_sha256_…"],"not_captured":[]}}
```

Rules `[dec]`:

- **D1** Every op names an `entity` ID and a field path relative to it; no array indices, ever.
- **D2** `null` never means "unknown" or "deleted". Deletion is `op:"remove"`; unknown is `state:"unknown"`; absent
  is `state:"not_captured"`. RFC 7386 defines `null` as *remove* `[src: §2]`, so a null-based delta cannot express
  "we did not measure it".
- **D3** Every delta carries `from_root`/`to_root` and is applied only when the client's root equals `from_root`;
  otherwise refresh. This is JSON Patch's `test` `[src: RFC 6902 §4.6]` lifted to the document root. Replay is
  idempotent by `chg_id`.
- **D4** `unchanged` carries counts and hashes, so "nothing changed here" is positive evidence. `measurements[]`
  carries `state` plus an `incomparable` entry when a pair cannot be compared; there is no `better`/`worse` field
  anywhere, by construction.
- **D5** `audio[].hash_equal` is *not* a change signal; `render_key_equal` is. `rerendered_same_content` exists
  precisely because hashes differ when content did not (§3.4). Deltas never cross `project_id`, `instance_id` or
  lineage.

### 5.5 Active projection algorithm

`project(project_id, requested_scope, evidence_policy)`:

1. Read `context` and `project_digest`; build `RevisionRef` and `fact_root`.
2. If `(fact_root, evidence_set)` equals the active key, return it (`cache_hit: true`).
3. Else compute `changed = diff(entity_facts(previous), entity_facts(current))` per domain.
4. Map `changed` through the invalidation graph (§6) to `invalidated_assets`, `invalidated_measurements`.
5. Reuse an asset iff its `render_key` is unchanged; reuse a measurement iff `(asset_id, method, params, window,
   stage, channel)` is unchanged. This is the only place "unchanged" is decided.
6. Rebuild only affected rows/tables; unchanged domains are copied by hash reference.
7. Compute `content_root` over `{meta minus generated_at, state, evidence, states}` and a separate
   `document_sha256` over the whole document for tamper detection.
8. Validate I1-I6 (§8); a violation is a `projection_invalid` error, not a warning.
9. Persist the projection keyed by `(fact_root, evidence_set)`; keep the previous N for delta serving.

**Bounded rebuild `[dec]`:** if `changed` exceeds 30 % of entities, or fingerprint scope is `coarse`, or `epoch`
changed, or `fact_coverage.missing` is non-empty → rebuild from the canonical record, set `rebuild_reason`, return
the whole document (never a partial delta). This is the "copy-and-transform" escape hatch the event-sourcing
literature names for projection rebuilds `[src: arXiv 2104.01146]`.

### 5.6 Transaction / readback sequence

1. `read` → document + `{project_id, instance_id, epoch, counter, fingerprint, fact_root, evidence_set}`.
2. Model proposes an edit bound to that full revision (`expected_revision`, as today).
3. Server re-reads the digest before apply; mismatch → `stale_context`, no write (`session.py:1772-1782`).
4. Apply in one undo block; read back touched objects *and* the new digest.
5. Append `op_` + `chg_` + `rev_`; compute the delta against the pre-apply revision.
6. If a capture ran, append `cap_` with `revision_before`/`revision_after` and `restore_report`.
7. Return the delta plus `to_root`; the client verifies by applying it, and asks for the whole document on mismatch.

The delta comes from the journal, not from the model's plan. The plan is an annotation (`attribution`), never the
authority for what changed — "the observed REAPER diff remains the authority".

### 5.7 Refresh / changes-since mechanics

This is the server-side machinery behind R2.1. The client presents `(rev, fact_root)` and, optionally, `client_root`.
Three outcomes:

| Case | Server response |
|---|---|
| `fact_root` is current | `not_modified`, no document |
| `fact_root` is an ancestor and a contiguous `chg_` chain exists (≤ 20 records) | concatenated delta, `chain:[chg_…]`, `to_root` |
| chain has a gap, > 20 records, schema migration, `epoch` changed, or a projection was evicted | full document from `panorama_refresh`, `refresh_required`, `refresh_reason` |

Because every delta carries `from_root`/`to_root`, a gap is detectable without trusting the server's ordering. This
is the same offset/log property that makes a partitioned commit log replayable `[src: Kafka documentation]`.

### 5.8 Human edits

There is no event stream from REAPER to the journal, and MCP's current revision `2026-07-28` replaced per-resource
subscribe with a single `subscriptions/listen` stream, while removing protocol-level sessions
`[src: MCP changelog 2026-07-28]`. A design that depends on push notifications for correctness is therefore not
portable `[dec]`. Correctness must come from **pull + fingerprint**:

- Every read re-reads the digest. Equal `fact_root` → no change, even if `counter` moved (capture mutes; owner gate 4).
- Different `fact_root` with no matching `op_` → `attribution.kind = "external"`; dependent claims become `stale`.
- A capture whose `revision_after` differs from `revision_before` records both and marks `capture_side_changes`; audio
  is bound to `revision_before`, post-state to `revision_after`.
- Save-As/move → new `project_id`; append a `lineage` edge so history is not silently split.

---

## 6. Dependency and invalidation matrix

**The render graph is the dependency graph.** Capture renders each selected track *through its buses and the master*
(`RENDER_SETTINGS=128`, `selected_tracks_via_master`; `scripts/reaper_bridge.lua:1700-1708`), isolating by muting
non-selected tracks while keeping destination buses open (`daw/full_capture.py:986-1014`). Consequences `[obs]`:
a master-FX change alters every track asset; a bus-FX change alters every source routed into it; a track's own
change alters that asset plus every aggregate above it.

`render_key = sha256(JCS({track_facts, ancestor_facts[], isolation_set, automation_in_window,
window, render_settings, sample_rate, channels, tail_ms, reaper_version, plugin_versions}))` `[dec]`.
Assets are reused iff `render_key` matches, regardless of file hash.

| # | Change class | Invalidates audio | Invalidates measurements | Recompute cost | Residual risk |
|---|---|---|---|---|---|
| 1 | Track volume/pan/mute | that track's asset | all metrics on it + bus/master aggregates | 1 asset | none |
| 2 | Track FX param/order/enabled/offline | that track (+ descendants) | same | 1 asset | plugin binary changes are invisible |
| 3 | Bus FX/param/volume/pan | bus + every routed source | all sources in subtree + master | subtree | folder depth in digest; bus FX state covered |
| 4 | Master FX/param/volume/pan | **all 43 assets** | all | full recapture (~190 s render + ~537 s analysis, reported) | master FX bypass state covered by `fx_state`; plugin version not |
| 5 | Send gain/pan/mute/mode | source + destination | subtree | subtree | mode (pre/post) not in digest rows |
| 6 | Send create/delete, parent/folder change | entire subtree + master | entire subtree | subtree | folder depth in digest; routing graph derived |
| 7 | Item move/length/fade/offset/playrate/stretch | that track | that track + aggregates | 1 asset | digest has these; panorama hides them |
| 8 | Item create/delete, take switch | that track | same | 1 asset | inactive takes never captured |
| 9 | MIDI note edit | that track | same | 1 asset | MIDI limited to 10 k notes |
| 10 | Tempo marker / tempo automation | all, **iff** project timebase moves items | musical coordinates for all windows | full | timebase flags in digest; musical re-resolution needed |
| 11 | Time signature | none directly | bar/beat labels | facts only | — |
| 12 | Project sample rate / render channels / tail | all | all (different grid) | full | covered by digest project settings |
| 13 | Analysis method/version/params | none | **all measurements** | analysis only (~537 s full) | version not recorded in reports `[obs]` |
| 14 | Plugin binary update | affected chain, silently | stale but undetectable | full | **not detectable from RPP** |
| 15 | Pan law / `channel_count` / item `channel_mode` | affected chain | affected | full | **not in digest** `[obs]` |
| 16 | Selection/transport/cursor/marker/region | none | none | facts only | must be excluded from deltas |
| 17 | Human edit (any of 1-15) | per class | per class | per class | attribution `external` |

Cost anchors: the only measured full-capture cost in the repo is 190.6 s render + 536.8 s analysis for a 300.5 s,
41-track project (`docs/research/round-5/SYNTHESIS-AND-DECISIONS.md` §6), i.e. ~4.5 s render and ~12.8 s analysis per
asset on that machine (derived). Facts-only recomputation is milliseconds; the digest is a full-project read and
must not be re-run per field.

**Materially uncovered fields** `[obs]`: `I_PANMODE` (nowhere), `I_NCHAN` (context only, not digest), item
`channel_mode`, `group_id`, `source_channels` (context only), track `name` (context only), sends pre/post mode, FX pin
connectors (covered indirectly by `fx_state` chunk), automation mode. Any of these can change the sound while the
revision fingerprint is unchanged. Until the digest is extended, the projection must label them
`coverage: presented_not_fingerprinted` and treat a periodic full re-capture as the only trustworthy reconciliation.

**Non-determinism is a first-class fact** `[obs]`: identical `fact_root` produced 43/43 different asset hashes. Any
design that says "reuse when the hash is the same" will recompute everything; any design that says "recompute when the
hash differs" will never stop. `render_key` is the only stable key.

---

## 7. Whole-song state vs interval evidence

### 7.1 Two scopes in one document

Both columns are the **same contract**: `state` is whole-project in both, and only `evidence` narrows. The interval
column is what `panorama_slice(handle, window=[36,45))` returns; there is no separate interval mode, no client-side
injection and no second document shape.

| Field | Whole-song request | `panorama_slice(window=[36,45))` |
|---|---|---|
| `state.scope` | project | **project** (unchanged) |
| `state.entities` | all 42 tracks, 34 items, 40 routes, 15 FX, time map | identical |
| `state.arrangement` | every item interval | every item interval, plus `overlap: true/false` for the window |
| `evidence.scope` | `{kind:"capture", interval:[0,300.5]}` | `{kind:"interval", interval:[36,45]}` |
| `evidence.sources` | 43 assets + metrics | only sources with content/route in the window |
| `evidence.resolution` | multiresolution (e.g. 30 s, 300 s) | full-rate if budget allows |
| `evidence.musical` | bar/beat per tempo segment | bar/beat at 36 and 45, plus segments crossed |

The whole-project state is never narrowed: routing, FX chains, identities, master chain and the full arrangement stay
visible, so the model does not lose the song while looking at nine seconds. Only *evidence* narrows. This satisfies
"one panorama" without pretending nine seconds of audio is the whole song.

### 7.2 Worked example `[36,45)`

The call is `panorama_slice(handle, window=[36,45))`; every step below is server-side resolution, not a client-side
expansion of a hierarchy.

1. Resolve 36 and 45 through the revision-bound tempo map into bar/beat; if a tempo marker or signature falls inside,
   emit the segment list, not a single BPM (`panorama.py:1884-1926` already refuses a base-BPM grid).
2. Select items whose `[position, position+length)` intersects `[36,45)`. For each, emit `interval` clipped to the
   window, `item_span` (full interval), `partial: true` if clipped. Items entirely outside are still listed in
   `state.arrangement` but carry no evidence.
3. For each track: `content_in_window` = true/false. Three distinct states matter and must not collapse:
   **`no_content_in_window`** (no items), **`silent_in_window`** (items, measured silence), **`not_captured`** (no
   asset). Today all three look like missing numbers.
4. Each source asset covers exactly the window plus optional tail; the tail is a separate interval that never maps to
   project time (`panorama.py:1244-1254`, `audio_time_contract`).
5. Measurements carry `window:[36,45]`, `stage`, `channel_view`, `sample_rate`, `method`, `version`, and `series`.
6. Temporal tables are anchored to **absolute project time**: bucket *k* = `[k·w, (k+1)·w)`, not `times[0] + k·w`.
   Today `_buckets` anchors at the first sample (`panorama.py:547-553`), so a section capture starting at 36 and a
   full capture starting at 0 produce different bucket boundaries for the same music `[obs]` — their summaries can
   never be compared bucket-by-bucket.
7. Resolution is fixed per document from a global ladder (e.g. 0.5, 2, 10, 30, 300 s) and stated in
   `evidence.resolution`; a comparison between different widths is `incomparable` (§5.4 D5).

### 7.3 Flat all-times vs multiresolution

Full-rate RMS for 43 sources over 300.5 s at 10 Hz is 129,215 points (~2.3 MB), over three times the byte cap `[obs]`
arithmetic. So a whole-song document **cannot** carry all-times full-rate evidence, and any claim that it does is
false. Multiresolution is therefore not a stylistic preference; it is forced by arithmetic. The owner's constraint is
satisfied by making the reduction explicit and reversible: every bucket carries count/min/max/mean/p50 and a pointer +
SHA-256 to the exact series in the canonical record, and the interval view can serve full-rate for the narrow window.
What must **not** happen is a compact summary presented as if it were the measurement (`[src]` round-5 R7 principle).

Evidence on whether summaries are "acceptable" to a model is thin: no study measures DAW-panorama comprehension. The
closest evidence is that table/graph encoding effects are task-dependent (`[src]` 2305.13062, 2310.04560) and that
tool-output processing degrades with size and reasoning complexity (`[src]` 2510.15955). This is an argument for
**explicit resolution + drill-down**, not for assuming a summary is understood.

---

## 8. Fault injection and evaluation

Invariants checked on every response `[dec]`:

- **I1 Currency:** every value is bound to the response's `fact_root`, or carries `state ∈ {stale, not_captured,
  invalid, incomparable}`.
- **I2 Replay:** applying a delta to `from_state` yields `to_state` (hash equality).
- **I3 Comparability:** no measurement pair is compared across different stage/window/channel/sample-rate/method/
  revision.
- **I4 Reconciliation:** `unchanged counts + changed counts = domain total`; a mismatch is an error.
- **I5 Validity:** a capture with `capture_status != valid*` can never contribute `fresh` measurements.
- **I6 Determinism:** rebuilding the same `(fact_root, evidence_set)` yields the same `content_root` byte-for-byte.

| # | Injection | Expected behaviour | Pass criterion |
|---|---|---|---|
| T1 | Missed delta: client at rev A, project at C | contiguous chain A→C, else `refresh_required` + full document | client root equals server root; no stale value |
| T2 | Out-of-order patch (B→C before A→B) | rejected on `from_root` mismatch | projection unchanged; error names both roots |
| T3 | Duplicate patch | idempotent by `chg_id` | `already_applied:true`, same `to_root` |
| T4 | Server/app restart | projection rebuilt from journal | I6 holds; `refresh_reason:"restart"` |
| T5 | Partial capture: master silent | capture fails closed at snapshot; **read-back must expose `invalid`** | T5 fails today `[obs]`; pass = no `fresh` master measurement, `states.invalid_captures` non-empty |
| T6 | Routing/master chain changed | all/subtree assets `stale`; no metric delta until re-capture | delta has `set_state` ops, zero measurement deltas |
| T7 | Human edit outside MCP | delta computed, `attribution:"external"`, dependent claims stale | no operation ID invented |
| T8 | Track deleted and recreated with same name | old `removed`, new `added`, `unmatched` | zero "changed" ops for that pair |
| T9 | Tempo automation added | `tempo_map` hash changes; musical coordinates re-resolve; audio invalidation depends on timebase | windows' bar/beat differ; audio state matches timebase rule |
| T10 | Stale `expected_revision` on an edit | `stale_context`, no write | project digest unchanged |
| T11 | Analysis version upgrade | measurements `superseded`; pairs `incomparable` unless re-based | no numeric delta across versions |
| T12 | Missing audio window | `not_captured`, never silence | master/track metrics absent, state explicit |
| T13 | Long project / size pressure | paged delivery, never truncation | reconstructable to `document_hash`; `coverage` explicit |
| T14 | Identical content re-capture (counter moved) | empty project delta; `counter_moved_content_same:true` | 0 project ops; measurements equal within tolerance; `hash_equal:false` allowed |
| T15 | Save-As / move | new `project_id` + lineage edge | history walks across the edge |
| C1 | Stock client with no resources and no subscriptions | whole lifecycle over ordinary tool calls | no resource URI, subscription or notification appears anywhere in the flow |
| C2 | Client loses the handle after compaction, keeps `revision` | `changes_since(project_id, base_revision)` | exact delta; no document re-sent; new handle valid |
| C3 | Client restarts mid-sequence | no state → open at the revision reported by `reaper_session` | no partial patch applied; `expected_revision` enforced |
| C4 | Revision stale by 12, then by 25 | 12 → delta; 25 → `refresh_required(chain_too_long)` | both outcomes correct in the same code path |
| C5 | Client accepts only ~8 KB responses | open at `budget=8192` → 24 KiB pages → 8192-byte pages | concatenated pages hash to `document_hash`; section map complete |
| C6 | Codex and Hermes on the same project | each carries its own `(handle, revision)` | both deltas correct; neither client's state affects the other |
| C7 | Concurrent human edit between base and request | delta with `attribution:"external"`, or `refresh_required` | no silent overwrite; loser receives the new revision |
| C8 | Client-side truncation of a response | head manifest present, tail sentinel absent | the artifact fails the completeness check and is not used as a base |
| C9 | Client drops one op while applying a delta | `client_root` mismatch on the next call | `state_mismatch` → `refresh_required`; no silent drift |
| C10 | Required-fact audit of the reassembled initial load | every fact in the R2.5.1 audit present, or an explicit `coverage.missing` entry | test fails if any audited fact is silently absent |
| C11 | A 300 s-only variant served as `projection:"full"` | rejected before serialization | fails the assertion that both temporal levels are present |
| C12 | Delta computed against a reduced/preview root | rejected | `stale_base`/`refresh_required`; no preview-shaped document may claim a full root |
| C13 | Page loss or `page_hash` mismatch | reassembly rejected, `panorama_refresh` required | no delta base is established from an unverified document |
| C14 | Nine-second slice over 43 sources | structure complete, `evidence.scope=[36,45)`, `coverage.not_in_scope` populated | ≤ 48 KB; whole-song buckets not repeated; no structural entity missing |

Acceptance is **not** "the panorama contains N fields". It is: for every injection, either the value is current or its
non-current state is explicit; every response is reconstructable to its declared `document_hash`; no comparison crosses
an incomparable boundary; and no case requires a client feature beyond calling a tool and passing strings back.

---

## 9. Source matrix

| # | Source | Date / version | Type | Exact claim used |
|---|---|---|---|---|
| S1 | https://www.rfc-editor.org/rfc/rfc6902.txt | Apr 2013 | primary | JSON Patch: sequential ops, `test`, JSON-Pointer paths |
| S2 | https://www.rfc-editor.org/rfc/rfc7386.txt | Oct 2014 | primary | merge patch: `null` removes a member |
| S3 | https://www.rfc-editor.org/rfc/rfc6901.txt | Apr 2013 | primary | JSON Pointer addressing |
| S4 | https://www.rfc-editor.org/rfc/rfc8785.txt | 2020 | primary | JCS canonical JSON for reproducible hashing |
| S5 | https://modelcontextprotocol.io/specification/2026-07-28/changelog | 2026-07-28 | primary | sessions removed; `resources/subscribe` → `subscriptions/listen` |
| S6 | https://simon.peytonjones.org/assets/pdfs/build-systems-original.pdf | ICFP 2018 | primary | build systems vary by dep kind, early cutoff, persistent info |
| S7 | https://www.cidrdb.org/cidr2015/Papers/CIDR15_Paper16.pdf | CIDR 2015 | primary | "record facts, then derive"; append-only immutable data |
| S8 | https://martin.kleppmann.com/2017/04/24/json-crdt.html | TPDS 2017 | primary | JSON CRDT merges concurrent edits without lost updates |
| S9 | https://inria.hal.science/inria-00609399v1/document | RR 7687, Jul 2011 | primary | CRDT convergence under eventual consistency |
| S10 | https://dl.acm.org/doi/10.1145/67544.66963 | SIGMOD 1989 | primary | operational transformation for concurrent editing |
| S11 | https://www.w3.org/TR/prov-o/ | 30 Apr 2013 | primary | entity/activity/agent provenance model |
| S12 | https://www.bipm.org/en/committees/jc/jcgm/publications | JCGM 100:2008 | primary | a measurement result carries an uncertainty statement |
| S13 | https://www.itu.int/rec/R-REC-BS.1770-5-202311-I/en | BS.1770-5, 11/2023 | primary | versioned loudness / true-peak algorithm |
| S14 | https://tech.ebu.ch/publications/r128 | R128 v5.0, Nov 2023 | primary | loudness, range and true peak are separate descriptors |
| S15 | https://arxiv.org/abs/2307.03172 | v3 Nov 2023 | primary | performance depends on position in long context |
| S16 | https://arxiv.org/abs/2310.04560 | Oct 2023 | primary | graph encoding changes accuracy 4.8-61.8 % |
| S17 | https://arxiv.org/abs/2411.10541 | Nov 2024 | primary | template format moves GPT-3.5 up to 40 %; GPT-4 robust |
| S18 | https://arxiv.org/abs/2408.02442 | Aug 2024 | primary | format restrictions can reduce reasoning |
| S19 | https://arxiv.org/abs/2610.12056 | Oct 2026 | primary | schema design, not structure, drives the "structure tax" |
| S20 | https://arxiv.org/abs/2602.05447 | Feb 2026 | primary | 9,649 runs: format not significant (p=0.484); model tier dominates |
| S21 | https://arxiv.org/abs/2604.17512 | Apr 2026 | primary | columnar: 46-51 % fewer tokens, no material comprehension loss |
| S22 | https://arxiv.org/abs/2605.29676 | May 2026 | primary | TOON/TRON: 18-27 % tokens, 9-14 pp accuracy cost in agent loops |
| S23 | https://arxiv.org/abs/2603.03306 | Feb 2026 | primary | plain JSON best one-shot generation accuracy |
| S24 | https://arxiv.org/abs/2510.15955 | Oct 2025 | primary | JSON tool-output processing hard; strategy depends on size |
| S25 | https://arxiv.org/abs/2607.19257 | Jul 2026 | primary | no Markdown advantage; +22-37 % overhead; decay near ceiling |
| S26 | https://www.trychroma.com/research/context-rot | 14 Jul 2025 | secondary | performance varies non-uniformly with input length |
| S27 | https://zenodo.org/records/20753849 | 18 Jun 2026 | secondary | controlled null: no length decay ≤150 k on needle probes |
| S28 | https://www.postgresql.org/docs/current/sql-refreshmaterializedview.html | docs 18 | primary | refresh replaces the whole view |
| S29 | https://www.sqlite.org/wal.html | current | primary | WAL gives atomic commit and concurrent readers |
| S30 | https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/ | draft-08, 22 Oct 2026 | primary | idempotency key makes non-idempotent methods fault-tolerant |
| S31 | https://docs.feldera.com/vldb23.pdf | PVLDB 16(7), 2023 | primary | general IVM over rich query languages (DBSP) |
| S32 | https://www.sigmod.org/wp-content/uploads/2024/09/GemsPODS24-Olteanu.pdf | PODS 2024 | primary | IVM: small input change → small output change |
| S33 | https://www.vldb.org/dblp/db/journals/debu/GuptaM95.html | 18(2), 1995 | primary | taxonomy of view-maintenance problems |
| S34 | https://arxiv.org/abs/2104.01146 | JSS 178, 2021 | primary | event-sourced systems: projection rebuilds; schema tactics |
| S35 | https://arxiv.org/abs/2602.23193 | Feb 2026 | primary | agent intentions → validated events → verifiable view |
| S36 | https://opentimelineio.readthedocs.io/en/latest/tutorials/otio-file-format-specification.html | OTIO 0.19 dev | primary | per-type schema versions; no instancing |
| S37 | https://github.com/bitwig/dawproject | v1.0 stable | primary | single-file project: audio, automation, plugin state, beats+seconds |
| S38 | https://kafka.apache.org/documentation/ | current | primary | durable ordered log, replayable, exactly-once processing |
| S39 | https://git-scm.com/book/en/v2/Git-Internals-Git-Objects | current | primary | content-addressed objects |
| S40 | https://modelcontextprotocol.io/specification/2026-07-28/server/tools | rev 2026-07-28 | primary | tools are model-controlled; tool-result size is implementation-defined, with no protocol maximum |
| S41 | https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2567 | merged into 2026-07-28 | primary | protocol-level sessions replaced by explicit, server-minted state handles passed as ordinary tool arguments |
| S42 | https://learn.chatgpt.com/docs/extend/mcp | accessed 2026-11-13 | primary (vendor) | Codex MCP config; `tools.<tool>.output_token_limit` is a positive token budget "before the standard 20% serialization allowance" |
| S43 | https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/mcp.rs | main, accessed 2026-11-13 | primary | MCP output truncated by the per-tool limit when set, otherwise by `model_info.truncation_policy` |
| S44 | https://github.com/openai/codex/blob/main/codex-rs/utils/string/src/truncate.rs | main, accessed 2026-11-13 | primary | `APPROX_BYTES_PER_TOKEN = 4`; `truncate_middle_with_token_budget` keeps head and tail |
| S45 | `~/.codex/models_cache.json` (local install) | client_version `0.167.0`, 2026-11-13 | primary (local evidence) | all ten catalog models declare `truncation_policy {limit: 10000, mode: "tokens"}`; `gpt-6-luna` context window 272,000 |
| S46 | Hermes `0.23.1` local install, `tools/budget_config.py:15,20` | 2026-11-13 | primary (local evidence) | MCP result threshold 50,000 chars; preview 1,500 chars; default result budget 100,000; turn budget 200,000 |

Access date for every row: **2026-11-13**.

---

## 10. Search log and saturation

| Round | Queries (abridged) | New directly relevant method/source |
|---|---|---|
| R1-R2 | JSON/YAML/CSV/table comprehension; format restrictions; tool-output processing; graph encoding; long context | 2602.05447, 2411.10541, 2408.02442, 2510.15955, 2305.13062, 2310.04560, 2307.03172, Chroma 2025, Zenodo null 2026 |
| R3-R5 | RFC 6902/7386/6901/8785; build systems/IVM/differential dataflow; CRDT/OT/git/idempotency | patch and canonicalisation semantics; early cutoff; IVM taxonomy; CRDTs not needed for a single writer |
| R6-R7 | provenance, GUM, BS.1770/R128; OTIO, DAWproject | uncertainty + versioned metric comparability; per-type schema versions; no instancing |
| R8-R11 | DAW incremental diff; event sourcing + audio; semantic JSON diff; IVM/JSON stores | R9 only: 2104.01146, 2602.23193. R8, R10, R11 produced **nothing new** for this design |
| R12-R13 | MCP tool-result size limits; server-minted handles (SEP-2567); Codex `output_token_limit`; `codex-rs` truncation; client compaction of tool results; Codex resource tools | SEP-2567 handle model; Codex per-tool budget and middle truncation; no protocol maximum; client caps are per-implementation |

**Saturation point:** R8, R10 and R11 produced no new architectural method or primary source; R9 only confirmed the
event-sourcing pattern. R12-R13 closed the delivery question instead of opening a new one: the protocol has no size
guarantee and no replacement primitive, and the two named clients cap results in different, non-generalisable ways.
Failed/duplicate searches: "MCP resource versioning ETag" returned the spec changelog only; "DAW dependency graph"
returned no research; Chinese-language queries returned nothing relevant.

---

## 11. Questions for Workstream A — Codex and generic-client behaviour

The contract no longer depends on any host's context machinery; these questions are about the client limits that
change delivery, and they are scoped to Codex and to conforming clients generally.

1. Codex 0.167: is the model-facing MCP result truncated before or after `structuredContent` is flattened into text,
   and does the 10,000-token policy apply to the output of the built-in `read_mcp_resource` tool as well as to
   `tools/call`? If it does, resources are not an alternative delivery channel on that client.
2. Does Codex's truncation notice ("Warning: truncated output …") reach the model as ordinary text, and is the middle
   marker visible in the JSON the model sees? That determines whether a truncated artifact is detectable at all.
3. Does Codex consume `notifications/resources/updated` or `subscriptions/listen`, or does it only expose resources as
   pull tools? Protocol capability and observed behaviour must be reported separately.
4. What does Codex auto-compaction do to prior tool results — keep, summarize or drop — at what threshold, and is the
   threshold per-model in the shipped catalog? The contract only needs to know whether a revision string can survive.
5. Across two or three other conforming clients (e.g. a minimal SDK client and one IDE client), is there any that
   silently truncates a tool result with no marker and no error? A silent cutter is the only case the head-manifest
   check cannot detect.
6. Do any clients validate tool results against a declared `outputSchema` or `structuredContent` schema, such that a
   union-shaped response (document | delta | status) would be rejected rather than rendered? This bears directly on the
   API-surface choice in R2.2.
7. Is there a protocol-level maximum or recommendation for tool-result size beyond "implementation-defined", and does
   the 2026-07-28 handles guidance (SEP-2567) say anything about handle lifetime or handle loss?
8. On Codex, does `tools.<tool>.output_token_limit` change only the model-facing copy, or also the persisted rollout
   copy (64 KiB cap)? Only the former matters for delivery, but the distinction affects test design.

My design depends on Q1 and Q4: if Codex truncates resource reads to the same budget, resources are not a delivery
channel; and if compaction can drop tool results entirely, only the revision string survives — which is exactly why the
contract requires nothing more than `project_id` + `revision` from the caller.

---

## 12. What I could not verify

- **Provider token counts.** No tokenizer was run; the only measured numbers are Music Flamingo values quoted in
  `docs/FULL-CAPTURE.md`. The 1.8488 bytes/token ratio is derived from those numbers, not re-measured.
- **Whether the documented 128,449 tokens were measured on the compact projection or the sidecar.** The ratio matches
  the compact byte count, which is consistent, but the measurement method is not recorded.
- **Whether REAPER renders are non-deterministic on other machines/settings.** The 43/43 hash difference is observed
  on this archive; the cause (float summation, dither, plugin state, denormals) is inference.
- **Whether REAPER ever reuses a GUID after delete/recreate.** Assumed not; not tested.
- **Whether the digest covers every audio-affecting field.** It demonstrably does not (pan law, `I_NCHAN`, item
  `channel_mode`); the full set of uncovered fields is bounded by reading the bridge, not proven exhaustive.
- **Whether `[36,45)` captures currently produce comparable bucket grids.** The code anchors at `times[0]`, so they
  do not; no live section capture was run to confirm the end-to-end effect.
- **Real-model comprehension of the proposed normalised document.** No user study; the representation recommendation
  rests on measured redundancy plus format-neutral comprehension evidence, not on a DAW-specific experiment.
- **Exact MF tokenizer behaviour on the normalized document** (fewer bytes, different token density).
- **The rejected 20,838 B variant on the production serializer.** It was computed read-only over one archived
  panorama using the document's existing 5-char aliases; a real serializer was never written. Its omission table
  (R2.5.1) is the only claim that matters now, and it is why the variant is rejected.
- **Whether Codex applies the same 10,000-token truncation to `read_mcp_resource` output** as to `tools/call`. The
  handlers are separate; not tested.
- **Whether any inspected client truncates a tool result silently**, with no marker and no error. Only Hermes 0.23.1
  and Codex 0.167.0 were inspected; a silent cutter would defeat the head-manifest check.
- **What Codex auto-compaction does to prior tool results** (keep, summarize or drop) at 0.167.0. The threshold is
  documented as model-relative; the treatment of a 20 KB tool result is not.
