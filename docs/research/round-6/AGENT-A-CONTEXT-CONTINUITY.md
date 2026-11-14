# Agent A — Model, Session, MCP Context Continuity, and Cross-Client Portability

> **Historical record (superseded transport).** This document describes the
> paged panorama contract that has since been replaced by one whole-view result.
> Current behaviour: `docs/DECISION-single-response-panorama.md` and
> `docs/MCP-README-LLM.md`. Treat this file as history, not instruction.


Date: 2026-11-13
Workstream: A (MCP protocol vs client behaviour, context continuity, caching, host limits, test design)
Revision: **2** — owner clarification of 2026-11-13 makes the portable cross-client MCP contract the product; Hermes evidence is retained as one client's behaviour. See *Revision 2 — portable MCP contract* immediately after §1.
Status: Research only. No code, tests, archive or configuration were modified. The only repo write is this file.
Evidence labels used throughout: **[OBS]** observed in installed source or by command; **[SRC]** supported by a cited external source; **[INF]** inference from observed facts plus sources; **[DEC]** a decision or recommendation.

---

## 1. Executive summary — the engineering answer

**Revision 2 changes the primary architecture.** The library must work from Codex, Hermes and other conforming MCP clients. The required path is therefore a **tools-only MCP contract**: one complete unified panorama delivered as deterministic pages; a server-minted opaque handle plus revision and fact root; revision-bound factual deltas; stale-base refusal; and bounded full resync. Hermes 0.23.1 remains the most constrained *measured* client and its limits still bind Hermes deployments, but **no host-specific mechanism may be required for correctness**. The detailed revision is the section immediately below; §2–§13 keep the original investigation, now labelled as one client's evidence.

Measured facts that decide the design.

**Hermes 0.23.1 (one client, measured):**

1. The verified full-song HIMMP panorama is 237,477 compact JSON bytes (largest archived: 241,435). Running Hermes's own token estimator over that byte string gives **59,370 estimated tokens** (largest: 60,359). The Music Flamingo tokenizer measured 128,449 tokens for the same 237,477 bytes — 1.85 bytes/token **[OBS]**.
2. Hermes 0.23.1 spills any `mcp_*` tool result larger than **50,000 characters** to `$HERMES_HOME/cache/spillover` and replaces it in the conversation with a **1,500-character preview plus a file path** (`tools/budget_config.py:13-20`, `tools/tool_result_storage.py:422-447`). Every real panorama is ~4.8× over that cap **[OBS]**.
3. The maximum inline MCP result is **100,000 characters** even if `tool_budget.mcp_result_size_chars` is raised, because `resolve_threshold()` returns `min(mcp_result_size, default_result_size)` (`tools/budget_config.py:56-75`). There is no supported configuration that puts a 237 KB panorama into one tool result **[OBS]**.
4. MCP resource update notifications carry **only a URI** — no content, no delta, no push into the model's context — in both the 2025-11-25 and 2026-07-28 revisions. Subscriptions are an invalidation signal, not a delivery channel **[SRC]**.
5. Hermes 0.23.1 does not consume resource subscriptions at all: it exposes `resources/list` and `resources/read` as ordinary **tools** (`list_resources`, `read_resource`) and has no `subscriptions/listen`, no `resources/subscribe`, and no `resources/templates/list` call anywhere in the tree **[OBS]**.
6. Hermes destroys old tool results when compaction fires: `compress()` replaces earlier turns with an LLM-written summary, and the optional `compression.proactive_prune_tokens` (default `0`, off) reduces a pruned tool result to `[mcp__reaper_mcp__reaper_capture_read] capture_id=… (241,435 chars result)` (`agent/context_compressor_prune.py:37-119`) **[OBS]**. Either way the panorama is no longer in the request; the model may retain only a citation to it.
7. Prefix/KV caching reduces **prefill compute and price**, never the number of tokens in the request; Routerai does not support cache writes and selects among upstream providers by stability first, then lowest price, with fallbacks, so cross-call cache hits are not architecturally guaranteed **[OBS/SRC/INF]**.

**Codex CLI 0.167.0-alpha.4.1 (a second client, measured):**

8. Every MCP tool result is truncated to the model's `truncation_policy` — **10,000 tokens** for all ten models in this install's catalog — with a 20 % serialization allowance, i.e. ≈12,000 tokens ≈ **48,000 bytes**, retaining only the head and tail around a `…N tokens truncated…` marker **[OBS/SRC]**. A 237 KB panorama is therefore not merely spilled but structurally destroyed on arrival.
9. Codex implements resources and resource templates as model-callable tools (`list_mcp_resources`, `list_mcp_resource_templates`, `read_mcp_resource`) but has **no** `resources/subscribe`; update notifications are received and only logged **[SRC]**.
10. Codex compacts with a handoff summary ("CONTEXT CHECKPOINT COMPACTION…") at `model_auto_compact_token_limit`, defaulting to roughly 90 % of the raw context window **[OBS/SRC]**.

**Consequence.** The conversation ledger (what tokens are in the request) is controlled *only* by the client/host. MCP can change what the *next* tool result contains — full, page, delta, or refusal — and therefore the **growth rate** and the **fidelity** of what arrives; it cannot remove anything already appended, cannot rewrite a delivered message, and cannot make a client accept a document larger than its cap. Model attention cannot be guaranteed by any of these mechanisms; it can only be biased by placement, recitation, and by keeping total volume low.

**Recommended candidates** (detail in §8.D and R2.8):

- **P0 — portable MCP contract (primary, the only architecture):** tools only; complete unified panorama at open/refresh, delivered as deterministic pages with a completeness manifest; `handle` + `rev_id` + `fact_root`; `changes-since` factual deltas; exact-base matching with no silent patch; explicit resync. Works on a stock client with no resources or subscriptions.
- **P1 — representation and delta economics:** the canonical document must fit ~10 pages at 24 KB with honest deltas; this is where cost/benefit actually lives (Workstream B). Every variant must preserve the original temporal grid (30 s + 300 s, plus any finer level) and all measurements; reduced-time-grid projections are rejected.
- **P2 — optional resource mirror:** the same document exposed as a resource/template for clients that can use them. A convenience only; never the required path and never a correctness dependency.
- **Rejected:** making a mutable resource URI the continuity mechanism. It is protocol-correct but client-dependent: invisible on Hermes (no template discovery), and on both named clients an update notification is signal-only and unhandled.

There is also a **contract tension** the synthesis must resolve explicitly: `docs/FULL-CAPTURE.md` states the panorama "is not a summary plus a fetch instruction" and that capture "does not return … a URI instead of the panorama". Revision 1 read this as requiring a single inline result, which is impossible on both named clients. Revision 2 keeps the *content* contract (one complete document, no summary) and changes only the *transport*: a deterministic multi-page delivery of that one document, verified by hash. The owner must accept multi-call delivery; that is a requirements decision, not a technical one (see R2.8 and §12).

---

## Revision 2 — portable MCP contract (owner clarifications 2026-11-13)

Client-side context manipulation is out of scope; the contract must work without it. Every requirement below must hold on a client that implements nothing but `tools/call`.

### R2.1 What the server guarantees independently of the host

These are properties of the library, testable with a scripted client and no particular host:

| Guarantee | Meaning | Failure is |
|---|---|---|
| **Identity** | `project_id`, `instance_id`, `epoch`, and stable entity IDs (track/item/take/FX/send/envelope) that survive renames; no array index is an identity | `identity_ambiguous` |
| **Immutable archive** | Captures and audio assets are never rewritten; a revision's evidence stays retrievable | `archive_missing` |
| **Current server-side state** | One authoritative `(project, instance, epoch, rev_id, fact_root)` at any moment | `no_current_state` |
| **Revisions** | Monotonic per project; each carries `parent`, `fact_root`, domain hashes, `schema_version`, coverage | `revision_gap` |
| **Content hashes** | `document_sha256` over the canonical document; `fact_root` as a Merkle root over entity fact hashes; per-section hashes | `hash_mismatch` |
| **Factual deltas** | Adds / replacements / removals with entity + field path, before/after, units, and evidence invalidations; no better/worse field exists by construction | `incomparable` |
| **Dependency invalidation** | Which captures, assets and measurements a change invalidates, and why (Workstream B §6) | `invalidation_unknown` |
| **Consistent interval views** | A window is bound to the same revision, render stage, channel view, sample rate and method version, or states why it cannot be | `window_not_comparable` |
| **Bounded full resync** | The document is always rebuildable and deliverable in `ceil(doc_bytes / page_bytes)` responses, each ≤ `page_bytes` | `resync_unbounded` |

### R2.2 What MCP cannot force any host or model to do

This is a hard boundary, not a defect to engineer around:

- accept an arbitrarily large response — every client may cap, spill or truncate;
- retain an earlier tool response — compaction, pruning, eviction and truncation are legal client behaviour;
- replace or delete an old conversation message — no MCP revision has such a primitive;
- attend to every field that was delivered;
- receive or act on resource notifications — `subscribe`/`listen` are optional capabilities, and both named clients lack them;
- discover resources or resource templates — both are optional, and templates need a separate endpoint;
- call `changes-since` before acting, or finish all pages of a paged open.

The server can *validate a base the client claims*; it cannot *observe what the model still remembers*. Therefore continuity is a **client-declared, server-verified** property, and the only honest answer when it cannot be verified is `resync_required` — never a best-effort patch.

### R2.3 Protocol-level capability table (host-independent)

Current revision **2026-07-28**; differences from 2025-11-25 noted. "Minimal client" means tools only, no resources, no subscriptions.

| Capability | Protocol position | What the server can guarantee | What the host must implement | Minimal-client outcome |
|---|---|---|---|---|
| `tools` | Core, model-controlled | Callable tool, stable schema, returned result | Call it and place the result in context | **Works.** The only universally available channel. |
| `resources/list` | Optional capability; if declared, MUST answer; list MUST NOT vary per-connection | A stable resource list | Discovery and selection policy | Usually absent; harmless. |
| `resources/templates/list` | Separate optional endpoint | Templates addressable | Must call the endpoint explicitly | Templates invisible; a URI never shown is unreachable. |
| `resources/read` | Core resource operation; `contents[]` text or blob | Content at a URI | Map into context; may truncate | Often absent; where present, each read appends a new message. |
| `subscriptions/listen` (2026-07-28) | Optional; replaces HTTP GET + `resources/subscribe`; client opts into `resourceSubscriptions` | Notification carrying `uri` + `subscriptionId` only | Long-lived stream, routing, re-read, injection policy | Not implemented in either named client. Signal-only where it exists. |
| `resources/subscribe` (≤2025-11-25) | Optional per-resource subscription | Same signal-only notification | Same | Same. |
| `notifications/resources/updated` | Notification, no content | That a URI changed | Decide whether to re-read | Hermes: no handler. Codex: received, logged only. |
| `notifications/resources/list_changed` | SHOULD when `listChanged` | That the list changed | Refresh the list | Ignored by both named clients. |
| Pagination (`nextCursor`) | Core for list endpoints | Deterministic page sequence | Follow cursors | SDK-level and usually present; does **not** page arbitrary tool payloads. |
| Content size | **No protocol maximum**; implementation-defined | Only that the server bounds its own output | Accept, truncate or spill | Unknown per client; treat as small. |
| Sessions | **Removed** in 2026-07-28; each request carries `_meta` (`protocolVersion`, `clientCapabilities`, `clientInfo`) | Nothing across calls unless it mints a handle | Send `_meta`; the SDK does it | Works; 2025-11-25 clients use `Mcp-Session-Id` instead. |
| Server-minted handles (SEP-2567) | Sanctioned replacement for session state | Opaque handle → server record; validated, never parsed | Carry the string in ordinary tool arguments | **Works everywhere**, including stateless clients. |
| TTL / `cacheScope` (SEP-2549) | Advisory on list and read results | Freshness hint | May cache | Ignored safely; no correctness dependency. |
| Tasks extension | Out of core in 2026-07-28 | — | — | Not needed; a slow capture is a slow tool call. |

`[SRC]` 2026-07-28 changelog and server/resources; `[DEC]` the contract uses only rows that work in the last column — tools, handles, revision checks, and paging expressed *inside* a tool's arguments.

### R2.4 Client capability matrix — host-imposed limits, not architecture

| | **Hermes 0.23.1** (measured) | **Codex CLI 0.167.0-alpha.4.1** (measured + source) | **Generic tools-only client** (conservative baseline) |
|---|---|---|---|
| Tools | yes | yes | yes |
| Resources list | via tool `list_resources` | via tool `list_mcp_resources` | usually no |
| Resource templates | **no** (`resources/templates/list` never called) | **yes** (`list_mcp_resource_templates`) | no |
| Resource read | via tool `read_resource`; appends a message | via tool `read_mcp_resource` | no |
| Subscriptions | no | no; notifications received but only logged | no |
| Per-result cap | 50,000 chars → spill file + 1,500-char preview; 100,000 chars max inline MCP result | `truncation_policy` 10,000 tokens ×1.2 ≈ 12,000 tokens ≈ 48,000 bytes; middle truncation | unknown; assume ≤ 32 KB |
| Truncation shape | whole result replaced by `<persisted-output>` + path | `Warning: truncated output (original token count: N)` + head 50 % / `…N tokens truncated…` / tail 50 % | unknown |
| Compaction | LLM summary at 0.50 of window (raise-only floor 0.75 below 512 K); earlier turns replaced | handoff summary ("CONTEXT CHECKPOINT COMPACTION…"); `model_auto_compact_token_limit`; default ≈90 % of raw window | varies |
| Window in this install | 1,048,576 (DeepSeek route) | 272,000 raw, 95 % effective (`gpt-6-luna`) | varies |
| Effect on a 237 KB result | spilled; model sees 1.5 KB + path | truncated to ≈48 KB, structurally broken JSON | unknown; assume loss |

Evidence: Hermes rows from §3 and §5 (`tools/tool_result_storage.py`, `tools/budget_config.py`, `agent/context_compressor.py`). Codex rows from `codex --version` → `codex-cli 0.167.0-alpha.4.1` and `codex debug models` → `truncation_policy {"mode":"tokens","limit":10000}`, `context_window 272000`, `effective_context_window_percent 95`, identical for all ten catalog models **[OBS]**; `codex-rs/core/src/tools/context.rs` (`McpToolOutput::response_payload` applies `truncate_function_output_payload` with `with_serialization_allowance(truncation_policy)`), `codex-rs/utils/output-truncation/src/lib.rs` (`truncate_mcp_tool_result` bounds an oversized serialized result), `codex-rs/utils/string/src/truncate.rs` (`APPROX_BYTES_PER_TOKEN = 4`, head/tail split, `…N tokens truncated…`), `codex-rs/core/src/context_manager/history.rs` (the same policy is applied again when recording history) **[SRC]**; Codex config reference: `tool_output_token_limit` is the "Token budget for storing individual tool/function outputs in history" **[SRC]**.

`[DEC]` **Do not extrapolate.** Codex's 10 k-token truncation is a property of this Codex version and its model catalog, not of MCP; Hermes's 50 K spill is a property of Hermes 0.23.1. The contract must satisfy the **minimum** of the three columns, which is why the default page budget is 24 KB — half the Codex cap, below the Hermes cap, with room for the response envelope.

### R2.5 Lifecycle: open → changes-since → refresh

**Initial open.** `open(project_id, page_bytes?)` returns the complete unified panorama for the selected project, or its first page plus a manifest, together with:

```
project_identity  {project_id, instance_id, epoch, name, lineage}
revision          {rev_id, parent_rev, schema_version, created_at}
roots             {fact_root, document_sha256, doc_bytes, page_count, page_bytes}
evidence_coverage {captured: [...], missing: [...], by_domain: {...},
                   uncovered_presented_fields: [...]}
state             {complete: bool, valid: bool,
                   invalid_captures: [...], stale_assets: [...], not_captured: [...]}
sections          [{index, path, bytes, sha256}]
handle            "rp_…"
page              {index, path, payload, sha256}   // complete: false when page_count > 1
```

Four things are deliberately separate: **identity** (which project/instance), **revision** (which state), **coverage** (what was captured), and **validity** (what is currently trustworthy). A panorama can be complete and still contain stale evidence; collapsing those into one boolean is how a partial view gets treated as current.

**Changes-since.** The client sends the last base it acknowledged — both parts:

```
changes_since(handle?, project_id, base_revision, base_fact_root, include_evidence=true)
```

The server answers one of three things, never a mixture:

- `{status: "current", base, target}` — base equals target; no content.
- `{status: "delta", base, target, chain: [chg_…], changes: [...], invalidations: [...], inherited: {...}, coverage_delta: {...}}`
- `{status: "resync_required", reason, base_echo, target, resync: {tool, args}}`

`changes[]` entries are `{op: add|replace|remove, entity: "trk_…", field: "mixer/volume", before, after, unit}` or a whole-row add/remove; `invalidations[]` are `{entity: "ast_sha256_…", state: "stale", reason: "master_fx_changed"}`; `inherited` names every unchanged domain with `{count, hash}` so "nothing changed here" is positive evidence rather than silence. There is no quality verdict, no ranking, and no `better`/`worse` field anywhere.

**No silent patch.** `base_revision` and `base_fact_root` are matched **exactly** against a retained revision. Any mismatch — older than the journal window, non-ancestor, different project, different epoch, different schema, unknown handle — returns `resync_required` with the current target and the exact refresh call. The server never downgrades to a partial delta, never rebases silently, and never applies a patch server-side on the client's behalf. Idempotence is by `chg_id`, so a retried delta is harmless.

**Verification.** Applying a delta must yield the target root, and the client must be able to check it at two levels:

1. **Continuity check (every client):** `delta.base.fact_root == client.base_fact_root`, `chain[0].from_root == base`, each `chain[i].to_root == chain[i+1].from_root`, and `chain[-1].to_root == target.fact_root`. Any break → discard the delta and refresh.
2. **Recomputation check (clients that implement the canonicalizer):** recompute `fact_root` from the base document plus `changes[]` using the published canonicalization (JCS over entity fact rows, Merkle over sorted `(entity_id, fact_hash)`) and compare with `target.fact_root`. The algorithm and `schema_version` are part of the contract, so a client library can do this without server help.

An LLM cannot compute SHA-256 in its head; the contract does not pretend otherwise. Level 1 is model-checkable from values in the response, level 2 is library-checkable, and the acceptance test in §10 uses a reference client that performs level 2.

**Explicit refresh.** `refresh(project_id, page_bytes?)` returns exactly what `open` returns at the current revision, at any time, with no error required. It is the normal way to reset a base, to recover after a gap, and to start a new chat. It is **mandatory** after: a missing or out-of-order delta, an unsupported or evicted base, a project identity/epoch change (save-as, reopen, second instance), a schema incompatibility, an unknown/expired handle, or any condition where the client cannot prove it still holds the base.

### R2.6 Client-state table — what the caller does with what it has

| Client state | What it holds | Portable action |
|---|---|---|
| **Fresh** | nothing (new chat, restart, post-compaction with no surviving facts) | `open` (full) |
| **Handle + root** | `(handle, rev_id, fact_root)` and the document | `changes_since`; on `resync_required` → `refresh` |
| **Revision only** | `(project_id, rev_id, fact_root)` but no handle (reconnect, handle expiry) | `changes_since` accepts revision addressing; on eviction → `refresh` |
| **No state / uncertain** | a summary or a revision string with no document | `status(project_id)` → `open` if it cannot reproduce `base_digest`; `changes_since` only if it can |

`status(project_id)` is deliberately tiny: `{project_id, epoch, rev_id, fact_root, document_sha256, doc_bytes, schema_version, handle?}`. It exists so a caller that lost everything can learn the current target without re-downloading the document, and so a caller that has a revision string but no content can decide honestly between `changes_since` and `open`.

**Recognising lost context without host help.** A caller must treat these as loss and refresh: the conversation no longer contains the document; the last panorama result was a spill/truncation marker; counts it can see do not match `base_digest`; the delta's `base.fact_root` differs from the root it remembers; the session was restarted or the chat is new; the project was reopened or saved under a new identity. Possession of a revision string is **not** possession of the base — a summary that says "revision 41" proves nothing. The contract's answer is always the same: `refresh`, then continue. MCP cannot observe client memory, so no server-side signal is invented.

### R2.7 API surface: one mode-based tool vs separate explicit tools

Both are plain tools and therefore equally portable at the protocol level. The difference is schema and model behaviour.

| Criterion | One `panorama(mode=…)` tool | Separate `open` / `changes_since` / `refresh` tools |
|---|---|---|
| Model discoverability | One name; the mode enum and its semantics must be read from the schema. Models frequently omit optional parameters or take the first enum value. | Three intention-named tools; selection is by name/description, which is the surface models actually use. |
| Schema reliability | Union of all fields; many optional parameters; invalid combinations only detectable at runtime. | Small closed schemas; `base_revision`/`base_fact_root` can be **required** for changes-since. |
| Validation / error semantics | Server must validate `mode` × argument combinations and explain misuse; a wrong mode looks like a valid call. | Wrong call is structurally impossible: no `base` argument exists on `refresh`; missing base fails schema validation before execution. |
| Risk of wrong mode | Real: default/omitted mode, or `open` where `changes_since` was meant, silently re-downloads or, worse, treats a stale base as current. | Low: the name states the intent; `refresh` cannot be mistaken for a delta. |
| Cross-client | Identical on Hermes, Codex and tools-only clients. | Identical; more tool definitions, but all three clients handle several tools. |
| Context cost | One definition, slightly smaller. | Three or four definitions; a few hundred tokens, negligible against a 237 KB document. |
| Paging | A fourth mode, mixing lifecycle with transport. | `page` is its own transport call with its own invariants. |

`[DEC]` **Recommend separate explicit tools**, with `page` and `status` separate as well. Working names only — `panorama_open`, `panorama_changes_since`, `panorama_refresh`, `panorama_page`, `panorama_status` — to be finalized by the synthesis. The decisive reasons are that required-base fields become schema-enforced, `refresh` cannot be confused with a delta, and a wrong mode cannot masquerade as a valid lifecycle step. The one-tool form is defensible only if a client imposes a hard tool-count budget; none of the three targets does. Codex's catalog reports `tool_mode: code_mode_only`, which changes how tools are *surfaced* in that client, not whether separate tools are valid; this was not exercised against a live REAPER server and does not change the protocol-level conclusion.

### R2.8 Initial full panorama under client caps

Measured document: 241,342 B for B's reference capture (237,477 B in the earlier compact measurement; largest archived 241,435). Caps: Hermes 50,000 chars, Codex ≈48,000 bytes, generic unknown.

| | **Admissible: paged FULL (default)** | **Admissible: lossless compact encoding, still paged** | **Rejected: reduced-time-grid / manifest-only** |
|---|---|---|---|
| First-load bytes | 241,342 B over **10 calls** at 24,576 | 169,285–187,610 B over **7–8 calls** at 24,576 | lossy projection or index-only |
| Fidelity | exact; every fact; 30 s + 300 s levels | exact same document, lossless encoding; both levels, every measurement, provenance, units, validity, coverage, uncertainty, relations | lossy: 300 s only, or index without content |
| Provable completeness | yes: manifest + `document_sha256` | yes: same manifest + hash after reassembly | no |
| Whole-project awareness | full after the last page | full after the last page | partial or none |
| Typical failure | model stops at page 3 of 10 | model stops before the last page | projection mistaken for the document |
| Owner's "one unified view, not a summary plus fetch" | satisfies | satisfies (same document, smaller encoding) | violates |

`[DEC]` **Portable default: paged full document**, default `page_bytes = 24,576`, manifest mandatory, fixed page order, `document_sha256` verification mandatory. It preserves the content contract exactly (one document, one revision, no hierarchy to unfold), survives both measured caps with ~2× margin, and needs only `tools`. Pages are slices at section/entity boundaries, each a valid JSON fragment; concatenating `payload` in index order reproduces `document_sha256`. A page request for a revision other than the handle's returns `resync_required` — a document is never assembled across revisions.

Only two forms are admissible: the **paged full document** (default) and a **lossless compact encoding of the same document**, which still pages because 169,285 B remains far above both caps. A reduced-time-grid projection (300 s only) and a manifest-only mode are rejected: neither can be a default, fallback or candidate.

The honest cost is ~10 round-trips at open and at refresh. Mitigations: page 0 carries identity, revision, coverage, state and the manifest so work can start; a client with a larger budget passes `page_bytes=65536` and gets four pages. The budget is **client-declared**, never inferred from `clientInfo`: a server that branches on a self-declared name becomes client-specific and breaks on the next client.

### R2.9 Reconciliation with the measured lossless-compact forms

**Measured lossless compaction does not rescue a single response.** Five measurements on the same capture (`cap_01M7AAD…`):

| Form | Bytes | What it preserves | Fits Hermes 50 K / Codex ~40 K? |
|---|---|---|---|
| **30 s temporal level alone** (473 rows, 43 sources) | **50,567** | 30 s resolution only — no tracks, items, FX, audio or analysis | **no / no** |
| Full document | 241,342 | everything | no / no |
| Lossless compact, lower bound: dedupe constants, drop derivable indexes/crosslinks, keep **all** numeric values and **both** 30 s and 300 s temporal levels | **169,285** | semantics-preserving (only derivable indexes removed) | **no** — 3.4× / 3.5× |
| Lossless compact + crosslinks | 187,610 | above, plus the relationship graph | no |
| Lossless compact + inline canonical IDs | 181,930 | above, plus self-contained IDs | no |

The 169,285 B lower bound is decisive, and one measurement makes it unconditional: the **30 s temporal level alone is 50,567 B** (473 rows, 43 sources) — already above Hermes's 50,000-char spill threshold and Codex's ~40,000 B counter before a single track, item, FX row, audio asset or analysis row is added. No semantics-preserving full panorama can fit one response on either client, so page transport is mandatory, not an optimization. The most favourable lossless form is still ~3.4× the Hermes spill point and ~3.5× the Codex counter.

**Therefore the default initial load is the paged FULL document**, `page_bytes = 24,576`:

- one document, one revision, fixed section order, `document_sha256` reassembly across pages;
- **10 pages at 241,342 B**; the same page size would give **7 pages at the 169,285 B lossless lower bound** and **8 pages at 181,930–187,610 B**. The default stays on the full document: three extra pages are cheap, and it avoids any equivalence argument about a compacted form;
- a client that declares 8,192 B gets 30 pages at 241 KB (21 at 169 KB); one that declares 65,536 B gets 4;
- 24,576 is chosen as roughly **half** of Codex's ~48 KB effective budget and half of Hermes's 50,000-char ceiling, leaving room for the response envelope and JSON escaping. A 32 KiB page also fits on paper, but leaves only ~1.5× margin against Codex and none against pathological escape expansion; 24 KiB is the largest budget with a genuine 2× safety factor on both clients;
- pages are justified by the **absence of a semantics-preserving single response**, not by convenience. They are transport, not a content hierarchy: the model does not choose what to expand, it receives one document in fixed order;
- **no edit may be attempted until all pages are received and `document_sha256` is verified.** A partial document is not a base; the server refuses writes that carry a revision the client cannot have verified (it can only check the revision string, not the client's memory, so this is a contract rule the client must follow).

**A 20,838 B "spine" prototype is a rejected lossy experiment, not a comparable form**: it is small only because it drops the 30 s temporal level (50,567 B on its own), rounds floats, and omits units, uncertainty, provenance, validity, automation, per-track detail and explicit relationships. It is not a candidate, default or fallback. Every panorama variant must preserve the original temporal grid — 30 s and 300 s, plus any finer level present in the archive — and all available measurements.

---

## 2. Method and labelling

I read the installed Hermes tree at `$LOCALAPPDATA\hermes\hermes-agent` (install stamp `0.23.1`, git `3b42157858`), the active `config.yaml`, the bundled MCP SDK (`mcp==2.3.0`), the repo's `mcp_server.py`/`context/`/`docs`, and the round-4/round-5 research. I re-queried the provider's `/v1/models` without printing credentials. For the Codex target I used the locally installed CLI (`codex-cli 0.167.0-alpha.4.1`), its `debug models` catalog and `mcp` commands, and the open-source `openai/codex` tree at `main`. External claims are from the primary spec, vendor docs, or papers listed in §7. Where I could not verify, §13 says so.

---

## 3. Verified Hermes 0.23.1 facts

### 3.1 Version, install, model routing

| Fact | Evidence | Label |
|---|---|---|
| Hermes `0.23.1` (display `0.23.1`), installed at `$LOCALAPPDATA\hermes\hermes-agent`, git HEAD `3b42157858` | `install-stamp.json:11-12`; `git log -1` | OBS |
| Runtime venv Python bundles `mcp==2.3.0` | `pyproject.toml:430`; `importlib.metadata.version('mcp')` | OBS |
| `model.default: "upstage/solar-pro4"`, `provider: "custom"`, `base_url: https://routerai.ru/api/v1` | `config.yaml:72-75, 105-110` | OBS |
| The **current session** runs `deepseek/deepseek-v4.1-flash-1017` (session header), which is *not* the config default. Both are listed under `custom_providers[0].models` | `config.yaml:2422-2452`; session header | OBS |
| Therefore: the CLI task model ≠ the future mixer model, and **neither is proven** to be the mixer model | — | INF |

### 3.2 Context, compaction, retention

| Fact | Evidence | Label |
|---|---|---|
| Compression threshold `0.50`; **raise-only floor 0.75 for windows < 512,000 tokens** | `config.yaml:686-690`; `agent/context_compressor.py:1150-1151, 2751-2756` | OBS |
| Effective trigger: `(context_length − max_tokens) × 0.50`, floored at a minimum context length — **≤ 524,288 tokens** on the DeepSeek route and **≤ 262,144** on solar-pro4 (524,288 is not *below* 512 K, so the 0.75 floor does not apply) | `agent/context_compressor.py:2751-2790` (docstring for `_compute_threshold_tokens`); `config.yaml:686-690`; arithmetic | INF |
| `protect_first_n=3`, `protect_last_n=20`, `target_ratio=0.20` (post-compaction summary budget = threshold × ratio), summary ceiling `min(5 % of window, 10,000)` tokens — the often-quoted "~12 K" is wrong; the constant is `_SUMMARY_TOKENS_CEILING = 10_000` | `config.yaml:773-780` (compression block); `agent/context_compressor.py:828, 2225, 2236-2239` | OBS |
| `compress()` is LLM summarization that replaces earlier turns at the threshold; the original panorama result is no longer in the request | `agent/context_compressor.py`; `config.yaml` compression block | OBS |
| `prune_tool_results_only()` is deterministic (no LLM), dedupes byte-identical results, demotes non-tail tool results > 8,000 chars to a one-line summary, retires stale images, commits in place to the session DB (`archive_and_compact`), gated by `proactive_prune_tokens` (default 0), `proactive_prune_min_result_chars` 8,000, `proactive_prune_min_reclaim_tokens` 4,096 | `agent/context_compressor_prune.py:37-119`; `hermes_cli/config_defaults.py:596-607` | OBS |
| Generic tool-result stub text: `"[<tool>] <arg>=<val> (<N> chars result)"`; image/oversize variants exist; a placeholder `"[Old tool output cleared to save context space]"` also exists | `agent/context_compressor.py:837, 2011-2023, 3172-3200` | OBS |
| `maybe_persist_tool_result()` spills results over a per-tool threshold to `$HERMES_HOME/cache/spillover/<id>.txt`, returning `<persisted-output>` with a **1,500-char preview**, the path, and recovery instructions; retention = session retention window (default 90 d), not a 24 h cache | `tools/tool_result_storage.py:276-281, 391-408, 422-463`; `_spillover_retention_hours()` 51-66 | OBS |
| Per-turn aggregate cap `200,000` chars: largest results are spilled first until the turn's tool output fits | `tools/tool_result_storage.py:488-520`; `tools/budget_config.py:14` | OBS |
| Budgets scale with window only *downward*: `default_result_size = min(0.15×window_chars, 100_000)`, `turn_budget = min(0.30×window_chars, 200_000)`. On both candidate models this clamps to the 100 K/200 K defaults | `tools/budget_config.py:92-114` | OBS |
| `tool_overrides` exists in `BudgetConfig` but is **never populated from config** — not a usable lever | `grep tool_overrides` → only `budget_config.py` and its `tree/` copy | OBS |
| Hard MCP text cap 2,000,000 chars inside the MCP result renderer (a flood guard, far above the spill threshold) | `tools/mcp_tool_content.py:25` | OBS |
| Results ≥ 1,000,000 chars additionally flag a post-batch trim | `agent/tool_executor.py:72, 1175-1177` | OBS |

### 3.3 Token accounting

| Fact | Evidence | Label |
|---|---|---|
| `CHARS_PER_TOKEN = 4` is "the single source for every N tokens ≈ N*4 chars budget conversion" | `agent/model_metadata.py:2336-2340` | OBS |
| `estimate_tokens_rough()`: ASCII → `ceil(len/4)`; non-ASCII → CJK codepoints 1 token each, remaining UTF-8 **bytes** / 4 (so Cyrillic ≈ chars/2). Calibrated against cl100k/o200k/Qwen2.5 | `agent/model_metadata.py:2349-2366` | OBS |
| There is **no runtime per-model tokenizer**. `tiktoken` appears only in `evals/` and in `hermes_cli/tools_config.py` tool-name size estimation. Nothing selects a DeepSeek tokenizer | `grep tiktoken` (no hits under `agent/`) | OBS |
| Real accounting comes from provider `usage`: `prompt_tokens`/`completion_tokens` plus optional `cache_read_tokens`/`cache_write_tokens`/`reasoning_tokens` | Hermes provider-usage parser (`update_from_response()`) | OBS |
| Post-response compression decisions use that real `prompt_tokens` (`should_compress_info` uses the passed value, else `last_prompt_tokens`) | `agent/context_compressor.py:2994-3003` | OBS |
| Preflight (pre-request) uses the rough estimator. On dense JSON it is optimistic: 237,477 B of compact panorama JSON → **59,370** estimated tokens (4.00 B/token) while a real BPE tokenizer measured **128,449** (1.85 B/token) | measured; Music Flamingo figure from `docs/FULL-CAPTURE.md:127-129` | OBS |

**Engineering reading.** The estimator is a budgeting heuristic, not a measurement. Because 4.0 B/token vs 1.85 B/token is a 2.16× gap on exactly the payload type this project generates, a preflight estimate near the threshold can correspond to a real prompt well past the window; the provider then rejects the request rather than silently truncating. Post-response decisions self-correct, so the damage is bounded to the *first* oversized request after a large ingestion.

### 3.4 The REAPER MCP surface as Hermes sees it

| Fact | Evidence | Label |
|---|---|---|
| 21 `@server.tool()` functions | `grep -c "@server.tool()"` = 21; names listed in §3.4 note | OBS |
| 6 `@server.resource(...)` registrations: 1 concrete (`reaper://capabilities/index`) + 5 **templates** (`capabilities/{domain}`, `api/{name}`, `projects/{sid}/{pid}/context/{change_count}`, `analyses/{id}/report`, `analyses/{id}/audio/{guid}`) | `mcp_server.py:1176-1253`; `docs/MCP-README-LLM.md:3` ("21 tools and five resource templates") | OBS |
| `reaper_capture_snapshot` and `reaper_capture_read` are **tools returning the whole panorama dict** (`{"ok":true,"panorama":{…},"panorama_source":…}`) | `mcp_server.py:1690-1729, 1734-1811` | OBS |
| Panorama one-shot budget: 700,000 compact bytes / 256,000 estimated tokens, checked before return; over budget → structured `panorama_capacity_exceeded`, never a truncated panorama | `context/panorama.py:31,36`; `docs/FULL-CAPTURE.md:125-141` | OBS |
| The project-context resource is **revision-pinned by URI**: it raises `StaleContext` unless `change_count` still matches | `mcp_server.py:1205-1221` | OBS |
| Hermes cannot discover that template: no `resources/templates/list` call exists; it only paginates `resources/list` | `grep list_resource_templates` → none; `tools/mcp_tool_handlers.py:839-844` | OBS |

**Note — the 21 tools:** `reaper_session, reaper_capabilities, reaper_api_search, reaper_project, reaper_analyze, reaper_edit_map, reaper_mix_evidence, reaper_edit, reaper_api_call, reaper_action, reaper_save, reaper_release, reaper_flamingo_status, reaper_flamingo_describe, reaper_flamingo_ask, reaper_spatial_status, reaper_spatial_analyze, reaper_spatial_timeline, reaper_timeline_context, reaper_capture_snapshot, reaper_capture_read`.

---

## 4. Model, provider, context and tokenizer

| Property | `deepseek/deepseek-v4.1-flash-1017` | `upstage/solar-pro4` | Evidence |
|---|---|---|---|
| `context_length` | **1,048,576** | **524,288** | `/v1/models` today |
| `architecture.tokenizer` | **"DeepSeek"** | **"Other"** | `/v1/models` |
| Modalities | text+image → text | text → text | `/v1/models` |
| Tools / parallel tool calls | yes / yes | yes / yes | `supported_parameters` |
| `per_request_limits` | `null` | `null` | `/v1/models` |
| Cache read price on Routerai | 0.33 ₽ / 1 M tokens (input 8 ₽, output 12 ₽); **cache write unsupported** | not checked per-model | `routerai.ru/models/deepseek/deepseek-v4.1-flash-1017` |
| Upstream selection | Stability first, then lowest price, with fallbacks; no provider count is published | same mechanism | Routerai provider-selection documentation |
| Real tokenizer available to Hermes | **no** — only chars/4 + CJK heuristic | same | `agent/model_metadata.py` |

**Consequences.**

- **[INF]** A 1 M window sounds like permission to keep ~8 full panoramas resident (8 × 128 K real ≈ 1.02 M), but the 0.50 trigger fires at 524,288 real prompt tokens, i.e. after roughly **four** full-panorama ingestions plus ordinary tool noise.
- **[INF]** Prefix caching on this route is best-effort twice over: DeepSeek documents cache as "best-effort … does not guarantee a 100 % cache hit rate" with units persisted at request boundaries and fixed intervals, and Routerai may route consecutive calls to different upstream providers, invalidating any per-provider cache. Caching must never be part of the correctness argument.
- **[DEC]** Any claim about "tokens" in this project must state which counter: Hermes rough estimate, provider `prompt_tokens`, or an external tokenizer (Music Flamingo). Conflating them is how the 128,449 vs 59,370 discrepancy arose.

---

## 5. MCP protocol vs Hermes 0.23.1 client behaviour

Current spec revision: **2026-07-28**, which supersedes 2025-11-25. The 2026-07-28 changelog removes protocol-level sessions and the `initialize` handshake, makes every request self-contained, and replaces `resources/subscribe`/`unsubscribe` + the HTTP GET stream with a single `subscriptions/listen` POST-response stream. Hermes's bundled SDK (mcp 2.3.0) implements both eras, and Hermes's transport layer can fall back to `server/discover` for stateless servers (`tools/mcp_tool_transport.py:219-249`).

| Capability | Protocol guarantee (2026-07-28 unless noted) | Hermes 0.23.1 behaviour | Consequence |
|---|---|---|---|
| **Tools** | Model-controlled; server declares `tools`; results returned to the model | Fully implemented; results rendered to a JSON envelope `{"result","structuredContent","_meta"}` | Normal path |
| **Resources — list** | `resources/list`, paginated, cacheable via `ttlMs`/`cacheScope` (SEP-2549) | Exposed as a *tool* `list_resources`; follows `nextCursor` up to 50 pages; reads `ttlMs`/`cacheScope` into cache metadata | Works, but only as an extra model-initiated call |
| **Resources — templates** | `resources/templates/list` is a distinct endpoint | **Not implemented** (no call anywhere) | Parameterised resources (all 5 REAPER ones) are **invisible**; the model must already know the URI shape from prose |
| **Resources — read** | `resources/read` returns `contents[]` (text or base64 blob) | Exposed as tool `read_resource(uri)`; returns `{"result": "<joined text>"}`; blobs cached as documents | Equivalent to a tool call; **appends a new message** every time |
| **Resource update notification** | `notifications/resources/updated` carries **only** `uri` (+ `subscriptionId`); client must re-read | **No handler.** No `subscriptions/listen`, no `resources/subscribe`, no notification routing | The model is never told a resource changed; staleness must be detected by the model or by revision-pinned URIs |
| **List-changed** | `notifications/resources/list_changed` (SHOULD) | Not consumed | New captures/analyses do not appear in the model's world model |
| **Prompts** | `prompts/list`, `prompts/get` (user-controlled) | Exposed as tools `list_prompts`, `get_prompt` | Model can fetch prompt templates; irrelevant to continuity |
| **Sampling** | Server→client `sampling/createMessage`; **deprecated** upstream in 2026-07-28 (SEP-2577, replaced by MRTR) | Implemented with rate limits/model allow-lists (`tools/mcp_tool_sampling.py:95-120`) | Does not help continuity |
| **Elicitation** | Server-initiated structured user input | Supported (`_pending_call_context`, elicitation callback) | Not a state mechanism |
| **Long-running work** | Moved out of core into the **Tasks** extension in 2026-07-28 (source matrix #26) | Not used by Hermes for MCP calls (per-call timeouts, stdio respawn) | A 78 s capture is just a slow tool call; no resumable handle |
| **Pagination** | Opaque `nextCursor` | Fully drained client-side (max 50 pages) | Large lists are not truncated by pagination, by design |
| **Content size** | No protocol limit; implementation-defined | 2,000,000-char hard cap, then **50,000-char spill** with 1,500-char preview | The binding constraint on panorama delivery |
| **State across calls** | **No session state.** Servers needing cross-call state must mint explicit handles passed as ordinary tool arguments (SEP-2567) | Nothing client-side; a handle would just be a string the model must repeat | This is the protocol-sanctioned shape of the recommended design |

**The one-sentence answer to "can a resource keep the model's context current?"** No. On this client, reading a resource produces a *new tool result appended to the conversation*; an update notification is never delivered; and even if it were, it would carry only a URI. A resource can keep the **server** current and can let the model **re-fetch**; it cannot make the model's existing context current.

Stated as the three-way test the dispatch asks for: an update causes **appended content** (a new tool result on the next read) — never **replacement content** (no MCP primitive exists to rewrite a delivered result, and Hermes has no such path for resources), and the notification itself is **signal-only** (a URI that Hermes does not even receive).

---

## 6. Four ledgers that must not be conflated

| Ledger | What it holds | Who can change it | Effect of change |
|---|---|---|---|
| **L1 Conversation** | The exact token sequence sent this request | Client/host only (message assembly, compaction, pruning) | Changes what the model can attend to, and cost/latency |
| **L2 Provider cache** | KV/prefix blocks on the inference side | Provider; influenced by prefix stability and routing | Cuts prefill compute and price; **does not** reduce request tokens |
| **L3 Server state** | Canonical archive, materialised panorama, revision/delta journal | REAPER MCP server | Changes what the *next* read returns; zero effect on L1 until a read happens |
| **L4 Model attention** | Which of the in-context tokens actually influence the answer | Nobody, directly | Degrades with length and with middle placement; can only be biased |

"Memory" belongs to L3 (durable, external) and, weakly, to L1 when content is resident. A prompt cache is not memory: it is recomputation avoidance on a prefix that must still be transmitted. DeepSeek's own docs make this explicit — cached prefix tokens are still input tokens, reported separately as `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`.

Empirical support for keeping L1 small rather than merely cached: Chroma's *Context Rot* (18 models) shows accuracy falling as input length grows even on trivial tasks; *Lost in the Middle* (TACL 2024) shows a U-shaped position effect; JetBrains' *The Complexity Trap* shows a simple observation-masking policy **halves cost versus the raw agent while matching or slightly exceeding LLM summarization** on SWE-bench Verified across five model configurations — i.e. deleting old observations is a legitimate, competitive policy, not a crude fallback.

---

## 7. Source matrix

Access date for all rows: **2026-11-13**. P = primary, S = secondary.

| # | Source (direct URL) | Date / version | P/S | Exact claim supported |
|---|---|---|---|---|
| 1 | https://modelcontextprotocol.io/specification/2026-07-28/changelog | rev 2026-07-28 | P | "Remove protocol-level sessions and the `Mcp-Session-Id` header… Servers that need cross-call state use explicit, server-minted handles passed as ordinary tool arguments (SEP-2567)." Also: `resources/subscribe`/`unsubscribe` replaced by `subscriptions/listen`. |
| 2 | https://modelcontextprotocol.io/specification/2026-07-28/server/resources | rev 2026-07-28 | P | Resources are "application-driven, with host applications determining how to incorporate context"; `notifications/resources/updated` params contain only `uri` + `subscriptionId`. `listChanged`/`subscribe` are optional capabilities. |
| 3 | https://modelcontextprotocol.io/specification/2025-11-25/server/resources | rev 2025-11-25 | P | Prior revision: `resources/subscribe` request, and the update notification carries only `uri`. Confirms the "signal only" semantics predate 2026. |
| 4 | https://modelcontextprotocol.io/specification/2026-07-28/server/tools | rev 2026-07-28 | P | "Tools in MCP are designed to be **model-controlled**"; annotations are untrusted unless from trusted servers; clients SHOULD validate tool results before passing to the LLM. |
| 5 | https://modelcontextprotocol.io/seps/2549-TTL-for-list-results | Final; created 2026-04-09 | P | Adds `ttlMs` and `cacheScope` to `tools/list`, `prompts/list`, `resources/list`, `resources/templates/list`, and `resources/read`; semantics "analogous to HTTP Cache-Control max-age". No ETag/conditional-request mechanism. |
| 6 | https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2567 | SEP-2567 (merged into 2026-07-28) | P | "removal of protocol-level sessions from MCP, replacing implicit session-scoped state with explicit, server-minted state handles". |
| 7 | https://modelcontextprotocol.io/specification/2026-07-28/basic/index | rev 2026-07-28 | P | Stateless core: every request carries `protocolVersion`, `clientCapabilities` and (SHOULD) `clientInfo` in `_meta`; no `initialize` handshake. |
| 8 | https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/mcp_resource.rs | commit on `main`, accessed 2026-11-13 | P | Codex registers `list_mcp_resources`, `list_mcp_resource_templates`, `read_mcp_resource` as model-callable tools; resource reads are bounded by the model's `truncation_policy`. |
| 9 | https://github.com/openai/codex/blob/main/codex-rs/utils/output-truncation/src/lib.rs | commit on `main`, accessed 2026-11-13 | P | `truncate_mcp_tool_result` replaces an oversized serialized MCP result with a bounded text preview; `with_serialization_allowance` adds 20 %. |
| 10 | https://api-docs.deepseek.com/guides/kv_cache/ | accessed 2026-11-13 | P | Prefix-unit cache; hits require full match of a persisted unit; `prompt_cache_hit_tokens`/`prompt_cache_miss_tokens`; "best-effort… does not guarantee a 100 % cache hit rate"; cleared "within a few hours to a few days". |
| 11 | https://www.deepseek.com/en/news/context-caching/ | 2024-08 (feature launch) | P | Context caching on disk; cache-hit input priced ~an order of magnitude below miss. |
| 12 | https://routerai.ru/models/deepseek/deepseek-v4.1-flash-1017 | accessed 2026-11-13 | P (provider) | Cache read **0.33 ₽/1 M**; "Cache: write not supported". Provider selection: stability first, then lowest price, with fallbacks; the earlier "40 providers" count could not be re-verified and is withdrawn. |
| 13 | https://platform.claude.com/docs/en/build-with-claude/prompt-caching | accessed 2026-11-13 | P | Caches `tools`→`system`→`messages` prefix up to a breakpoint; default 5-minute TTL, optional 1-hour at extra cost; cache refresh on use. |
| 14 | https://platform.claude.com/docs/en/build-with-claude/context-editing | accessed 2026-11-13 | P | `clear_tool_uses_20250919` with `trigger`/`keep`/`clear_at_least`/`exclude_tools`; "Tool result clearing: invalidates cached prompt prefixes when content is cleared… use `clear_at_least`". |
| 15 | https://platform.claude.com/docs/en/build-with-claude/compaction | accessed 2026-11-13 | P | "Compaction replaces the older turns of a conversation with a summary that Claude writes on the server"; on-demand vs threshold. |
| 16 | https://platform.openai.com/docs/guides/prompt-caching | accessed 2026-11-13 | P | Prompt caching enabled by default for supported models; cached input "discounted up to 95 %"; caches full rendered context incl. tool definitions and history. |
| 17 | https://developers.openai.com/api/docs/guides/conversation-state | accessed 2026-11-13 | P | `previous_response_id` + `store: true` chains server-held conversation state; manual mode replays the whole item list. |
| 18 | https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus | 2025-07-18 | P (production eng.) | KV-cache hit rate as the top production metric; "make your context append-only"; "Mask, don't remove"; "Use the file system as context" — compression must be restorable (URL/path retained); recitation (`todo.md`) pushes goals into the recency window. |
| 19 | https://www.anthropic.com/research/effective-context-engineering-for-ai-agents | 2025-09 | P (vendor research) | "attention budget"; context rot; recommended strategies: compaction, structured note-taking, sub-agent architectures. |
| 20 | https://www.trychroma.com/research/context-rot | 2025-07 | P (research) | 18 models; "performance grows increasingly unreliable as input length grows", including on simple tasks. |
| 21 | https://aclanthology.org/2024.tacl-1.9/ | TACL 12:157-173, 2024 | P | Performance highest when relevant information is at the beginning or end; degrades in the middle. |
| 22 | https://arxiv.org/abs/2310.08560 | 2023-10 (v2 2024-02) | P | MemGPT virtual context management: paging between tiers to provide "extended context within the LLM's limited context window". |
| 23 | https://arxiv.org/abs/2508.21433 | v3 2025-10-27 (NeurIPS '25 DL4C) | P | Observation masking "halves cost relative to the raw agent while matching, and sometimes slightly exceeding, the solve rate of LLM summarization". |
| 24 | https://arxiv.org/abs/2609.32961 | 2026-09-26 | P | ~35,000 runs: "fewer tokens need not mean faster or cheaper execution"; policies with ~1/3 the tokens can take 20-80 % longer. |
| 25 | https://openrouter.ai/docs/guides/best-practices/prompt-caching | accessed 2026-11-13 | S (comparable intermediary) | Aggregators use "provider sticky routing" to maximise cache hits; sticky sessions expire after 60 minutes of inactivity. Used only to reason about Routerai, which does not publish an equivalent guarantee. |
| 26 | https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/ | 2026-05-21 (RC; final 2026-07-28) | P | "extensions including server-rendered UIs through MCP Apps and long-running work through the Tasks extension"; stateless core; clients may cache `tools/list` for as long as `ttlMs` permits. |
| 27 | https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/context.rs | commit on `main`, accessed 2026-11-13 | P | `McpToolOutput::response_payload()` applies `truncate_function_output_payload` with `with_serialization_allowance(self.truncation_policy)` before the result becomes a model item. |
| 28 | https://github.com/openai/codex/blob/main/codex-rs/utils/string/src/truncate.rs | commit on `main`, accessed 2026-11-13 | P | `APPROX_BYTES_PER_TOKEN = 4`; token truncation keeps half the budget at the head and half at the tail, inserting `…N tokens truncated…`. |
| 29 | https://github.com/openai/codex/blob/main/codex-rs/core/src/context_manager/history.rs | commit on `main`, accessed 2026-11-13 | P | The same truncation policy is applied to `FunctionCallOutput` items when they are recorded into history, not only when produced. |
| 30 | https://learn.chatgpt.com/codex/config-file/config-reference | accessed 2026-11-13 | P (official) | `tool_output_token_limit`: "Token budget for storing individual tool/function outputs in history." Also documents `model_auto_compact_token_limit` and `experimental_compact_prompt_file`. |
| 31 | https://github.com/openai/codex/issues/16159 | opened; accessed 2026-11-13 | S (issue, corroborated by source) | Codex implements `resources/list`, `resources/read` and `resources/templates/list` as a client but not `resources/subscribe`/`unsubscribe`; `notifications/resources/*` are "received but only logged". |
| 32 | https://github.com/openai/codex/issues/40095 | opened; accessed 2026-11-13 | S (reported behaviour) | Default auto-compaction uses the raw window: 244,800 threshold on a 258,400-token usable window (272,000 × 95 %), i.e. ~94.7 % of usable context. |
| 33 | https://github.com/openai/codex | repo, `codex-cli 0.167.0-alpha.4.1` installed locally | P | Source of record for the Codex CLI behaviour above; local version confirmed by `codex --version`, catalog by `codex debug models`. |

Repo evidence is cited inline by `file:line`; the repo is a primary source for its own behaviour.

---

## 8. Architecture options, with token arithmetic

Baseline unit: one full panorama = 237,477 compact bytes ≈ **59,370 Hermes-estimated** tokens ≈ **128,449** real tokens (Music Flamingo measurement of the same bytes; the DeepSeek tokenizer is unmeasured but in the same class). Hermes compaction triggers at 0.50 × 1,048,576 = **524,288** real prompt tokens. Client caps that matter: Hermes 50,000 chars per result, Codex ≈48,000 bytes per result.

### A. Full panorama resend (status quo)

Every `reaper_capture_snapshot`/`reaper_capture_read` returns the whole panorama.

- Occupancy: strictly linear, ~128 K real tokens per capture (plus the spill preview and any `read_file` paging).
- Practical ceiling: ~4 captures before the compaction trigger, fewer with analysis chatter.
- Reality on Hermes: **each capture is spilled** (>50 K chars) → the model actually sees 1,500 chars + a path, then must page ~241 KB through `read_file`, i.e. ~3 × 100 K-char results ≈ 60 K estimated tokens of re-serialized JSON — roughly the same total as ingesting it once, with 3 extra round-trips and line-number overhead.
- Reality on Codex: the result is middle-truncated to ≈48 KB, producing broken JSON and losing the middle of the document.
- Attention: the panorama lands as a giant undifferentiated blob in the middle of the transcript, the worst position per *Lost in the Middle*.
- Verdict: **[DEC] unacceptable as the steady state**; keep only as the cold-start path.

### B. Initial full panorama + revision-bound deltas, no paging

First load full; subsequent loads return a panorama-shaped delta against a named capture/handle.

- Occupancy: 128 K once, then ~2–8 K real tokens per edit (a one-fader change on 41 tracks should be a few hundred bytes; a structural edit with re-analysis can reach tens of KB).
- ~78 delta turns before the Hermes trigger instead of ~4 full resends — a **~20×** improvement in edit capacity.
- Requires: stable IDs, revision binding, invalidation honesty (Workstream B), and an exact base argument to prevent double-applying or skipping.
- Fatal flaw on its own: the initial full panorama still exceeds both named clients' per-result caps, so the base never arrives intact.
- Verdict: **[DEC] necessary component**, insufficient alone.

### C. Mutable resource URI as the continuity mechanism

`reaper://projects/{sid}/{pid}/panorama/{change_count}` — the server holds one current panorama; the URI pins the revision and raises `StaleContext` when it moved (this already exists for the project context resource, `mcp_server.py:1205-1221`).

- Protocol: correct and idiomatic. Version-in-URI is the app-level analogue of an ETag, since MCP has no conditional requests (`ttlMs`/`cacheScope` are cache hints only, SEP-2549).
- On Hermes: **`read_resource` is a tool**, so a read appends a full result to the conversation; a notification would never arrive (no handler); and the resource is a *template*, invisible to `list_resources`.
- On Codex: resources and templates are reachable as tools, but no subscription; each read is still one more bounded result.
- Token effect: identical to B (one new result per read). Zero removal.
- Verdict: **[DEC] keep only as an optional addressing/staleness mirror** — it is a clean way to say "this view is only valid at revision N" — but never the required path.

### D. Portable lifecycle contract (recommended)

Server owns the immutable archive, the current projection and the revision journal, and mints an explicit **handle** passed as an ordinary tool argument — the SEP-2567 pattern, so it works on stateless 2026-07-28 servers and on every tools-only client.

Surfaces:
1. `open(project_id, page_bytes?)` → complete document, or page 0 + manifest + `handle` + `rev_id` + `fact_root` + coverage/validity states.
2. `page(handle, rev_id, index)` → one deterministic page; concatenation reproduces `document_sha256`.
3. `changes_since(handle?, project_id, base_revision, base_fact_root)` → `current` | `delta` | `resync_required`, exact-base matching, evidence invalidations, inherited unchanged facts.
4. `refresh(project_id, page_bytes?)` → the same as `open` at the current revision, always available.
5. `status(project_id)` → tiny current-target record for callers that lost their handle.

- Occupancy: 237 KB once at open (≈10 results of 24 KB), then only deltas; no growth from unchanged facts.
- Staleness: **best** — exact base matching, `resync_required` on any unprovable continuity, and writes bound to `expected_revision`.
- Client coverage: works on Hermes, Codex and a tools-only client with no resources, templates or subscriptions.
- Cost: ~10 round-trips at open and refresh; a few hundred bytes per delta.
- Limit: it does **not** remove old responses from the conversation; nothing a server can do does. It bounds each new response and makes the current state recoverable.
- Verdict: **[DEC] the only architecture.** Every other option is either a component of it (B's deltas) or a client-specific mirror (C).

### Option comparison

| | A full | B full+delta | C resource | D portable |
|---|---|---|---|---|
| Tokens after 20 edits (est.) | ~1.19 M (over window) | ~200 K | ~200 K | ~200 K |
| Growth per edit | ~128 K tokens | ~3 K tokens | ~128 K tokens | ~3 K tokens |
| Survives Hermes 50 K spill | no | only if the document fits | no | yes (paged) |
| Survives Codex 10 k-token truncation | no | only if the document fits | no | yes (paged) |
| Staleness safety | weak | revision-bound | good but undiscoverable | **best: exact base + refusal** |
| Bounded full resync | n/a | no | partial | **yes, manifest-verified** |
| Works on a tools-only client | yes | yes | no | **yes** |
| Removes old copies from L1 | no | no | no | no (client-only capability) |

Two forms are **not** options. A single-response full document is rejected by both named clients before any reasoning happens: the 30 s temporal level alone is 50,567 B, above both caps. A compact projection that reduces the time grid or omits measurement metadata is rejected as lossy (R2.9).

Token figures are estimates from the measured 59,370/128,449-per-panorama baseline plus a 3 K-token delta assumption; they are order-of-magnitude, not predictions.

---

## 9. Explicit limits

**Guaranteed by the server contract, on any client:**
- authoritative current state at a named revision, with `fact_root` and `document_sha256`;
- exact-base delta requests, and refusal (`resync_required`) whenever continuity cannot be proven;
- explicit full refresh at any time, bounded by `ceil(doc_bytes / page_bytes)`;
- cross-call continuity for stateless clients through server-minted handles (SEP-2567);
- factual deltas, dependency invalidation and consistent interval views (R2.1).

**Determined by the host, not by MCP:**
- whether an earlier response is still in the request after compaction, pruning or truncation;
- how large a single result may be (Hermes: 50,000 chars spill, 100,000 chars ceiling; Codex: 10,000-token policy ≈48,000 bytes);
- whether resource notifications are received or acted on (neither named client does);
- whether resource templates are discoverable (Hermes: no; Codex: yes);
- whether the model calls `changes_since` before acting or finishes all pages of an open.

**Cannot be guaranteed by any mechanism:**
- that the model **attends** to the current state rather than a salient recent delta — *Lost in the Middle* and *Context Rot* are properties of the model;
- that a summariser preserves the facts that matter: compaction is lossy;
- that the client has not silently lost the base — the server sees only the base the client claims, never its memory;
- that a human edit outside MCP is noticed before the next read — only revision checks catch it;
- that provider-side caching helps at all on an intermediary route with multiple upstream providers.

**One hard limit stated across clients:** a full panorama cannot be delivered as one tool result on either named client. The 30 s temporal level alone is 50,567 B — above Hermes's 50,000-char spill point and Codex's ~40,000 B counter — before tracks, items, FX, audio or analysis. Paged delivery is therefore part of the contract, not an optimization.

---

## 10. Empirical context test design (no implementation)

### 10.1 Arms, clients and state scenarios

| Arm | What the model receives |
|---|---|
| **A** full-resend | Whole panorama on every capture/read (current behaviour, including spill/truncation) |
| **B** initial + deltas | One full panorama at start *if it fits*, then revision-bound deltas; no paging, no refresh |
| **C** resource re-read | Full panorama fetched via `read_resource` on a versioned URI at each step, no deltas |
| **D** portable contract | Paged `open` + `changes_since` + explicit `refresh` + `status` (R2.5) |

Clients: **S** — stock tools-only harness (no resources, no templates, no subscriptions, 24 KB hard per-result cap); **C** — Codex CLI 0.167.0-alpha.4.1; **H** — Hermes 0.23.1 stock. Arm D must pass on all three; arms A–C are measured on H and C for comparison.

State scenarios, run for arm D on every client: (1) fresh open; (2) handle+root then one edit then `changes_since`; (3) revision only, handle discarded; (4) compaction/restart mid-task; (5) skipped delta then retry; (6) duplicate/out-of-order delta; (7) project save-as / epoch change; (8) simulated schema mismatch.

Each arm runs the same scripted 12-step editing/QA task on a disposable copy of the 41-track HiMMP project. Five repetitions per arm per model (`deepseek-v4.1-flash-1017`, `solar-pro4`) to expose variance; the model must be pinned per arm.

### 10.2 Measurements (all machine-collected)

1. **Occupancy**: `max(usage.prompt_tokens)` per call — the peak request size. This is the number that decides whether compaction fires.
2. **Cumulative billed input**: `Σ usage.prompt_tokens` per task; plus `Σ prompt_cache_hit_tokens`/`cache_read` when the provider reports it, and `cache_write` where applicable. Report the cache-hit ratio as `hit / (hit+miss)`, never as a token count.
3. **Growth rate**: Δ`prompt_tokens` per completed edit, median and p90; and the number of edits before the first compaction event.
4. **Truncation receipts**: count of `<persisted-output>` blocks in the transcript; count of `read_file` calls whose only purpose was paging a spill; any provider error with `context_length_exceeded`; `finish_reason` distribution.
5. **Staleness errors**: number of assistant claims or write attempts that used a superseded `revision`/`capture_id`; number of `StaleContext` refusals; number of times the model asserted a project fact without a matching revision in the same turn.
6. **Answer fidelity**: exact-match or rubric-scored answers to the question set below, scored **blind** (answers extracted by script, shuffled, graded against a key derived from the archive — not from model output).
7. **Time and cost**: wall-clock per step, TTFT, and billed cost at the Routerai per-million prices (input 8 ₽, output 12 ₽, cache read 0.33 ₽).
8. **Recoverability**: (i) force `/compress`, then re-ask the whole-project questions; (ii) kill and restart the session, reconnect MCP, re-ask; (iii) simulate spill expiry (delete the referenced spill file) and observe whether the model detects it and re-reads instead of inventing.

### 10.3 Question set (scored blind; answers derivable from the archived capture)

Whole-project questions (10):
1. How many tracks and how many buses/returns, and which tracks feed the `VERB RETURN`?
2. Which track has the longest FX chain, and what is its order?
3. Which tracks have automation envelopes, on which parameters, and in what time ranges?
4. Where does the tempo map change, and by how much? Are there time-signature changes?
5. Which items are active-take vs muted take, and which sources are shared between tracks?
6. What is the difference between `project_extent` and `content_extent`?
7. Which sources are missing analysis coverage, and why (per `coverage.domains`)?
8. What is the master chain, in order, with enabled/disabled state?
9. Which tracks are muted or soloed at the captured transport state, and does that affect the captured audio?
10. Which of the 42 analysis sources have `render_provenance.complete = false`?

Interval questions in project seconds `[36,45)` (8):
1. Which sources have non-zero energy in the window, and which are silent?
2. What is the peak/RMS of the master in that window vs the whole song?
3. Which items overlap the window boundary, and where does each start?
4. What is the bar/beat range covering `[36,45)`, given the tempo map?
5. Which automation curves are active in the window?
6. Which tracks' contributions in the window come via a bus rather than direct-to-master?
7. If the window is outside the captured audio, what does the tool do (must fail explicitly, not guess)?
8. After an edit that changes one track's fader, which measurements in the window must be recomputed, and which are unchanged?

### 10.4 Pass/fail criteria (per arm, per client)

| Metric | Pass | Marginal | Fail |
|---|---|---|---|
| Document reconstruction (arm D) | all `page_count` pages fetched; recomputed `document_sha256` matches | — | any missing page, hash mismatch, or `complete:false` at task start |
| Full-profile completeness | every domain and entity present; **both temporal levels (30 s + 300 s) and any finer level**; every measurement carries unit, method, version, stage, channel and window; uncertainty, coverage and validity are lists, not counts; relationships and provenance present | — | any required fact absent, any reduced time grid, or a reduced payload asserted as full |
| Temporal-grid assertion | a 300 s-only or otherwise reduced-time-grid panorama presented as `complete:true` is rejected by the validator | — | it is accepted, or the reduction is not declared |
| Page is not the document | every page carries `complete:false`, `index`, `page_count`, `document_sha256`; a single page cannot satisfy any whole-project question | — | a page or manifest accepted as the whole document |
| Delta application | applying `changes[]` to the base recomputes `target.fact_root` exactly | — | any mismatch, or a delta applied to a non-matching base |
| Stale-base refusal | every stale/unsupported base returns `resync_required`; zero bytes of patch applied | 1 refusal, recovered by refresh | any silent patch, rebase, or partial application |
| Client-state recovery | all 8 scenarios in §10.1 resolve to the specified call | 1 scenario recovered late | any scenario that proceeds on an unproven base |
| Whole-project accuracy | ≥ 90 % (≥ 9/10) | 70–89 % | < 70 % |
| Interval accuracy | ≥ 85 % (≥ 7/8) | 60–84 % | < 60 % |
| Stale-state errors | **0** | 1 (flagged, recovered) | ≥ 2, or any unflagged |
| Peak `prompt_tokens` at step 12 | ≤ 35 % of window | 35–50 % | ≥ 50 % (compaction fired) |
| Growth per edit (p90) | ≤ 5 % of first-load | 5–15 % | > 15 % |
| Spill/truncation-driven re-reads | ≤ 1 per task | 2–3 | > 3 |
| Recovery after compaction | ≥ 90 % of pre-compaction accuracy | 70–89 % | < 70 % |
| Unchanged facts inherited | model cites the delta's `inherited` hashes | — | model re-derives or contradicts |

A design is **rejected** if it fails document reconstruction, delta application, stale-base refusal, or stale-state errors on any repetition, regardless of accuracy.

### 10.5 Threats to validity

- Provider routing variance (multiple upstreams, fallback order) will perturb TTFT and cache hits; randomise arm order and log the routed provider if the response exposes it.
- The Music Flamingo token count is a *different tokenizer*; use it only as a ratio, never as the Hermes count.
- Compaction summariser quality is a confound: pin `auxiliary.compression.model` across arms.
- The question key must be generated from the archive by script, not written by the same agent that answers.

---

## 11. Search log and saturation

| Round | Queries (abridged) | New and directly relevant |
|---|---|---|
| 1 | MCP 2025-11-25 resources/subscribe; SEP-2549; DeepSeek KV cache; OpenAI prompt caching; Anthropic prompt caching | Current revision is **2026-07-28**; SEP-2549 `ttlMs`; DeepSeek prefix-unit cache + `prompt_cache_hit_tokens` |
| 2 | Manus context engineering; Anthropic effective context engineering; Anthropic context editing; Chroma context rot; Letta/MemGPT; LangGraph memory | Manus "mask, don't remove" + filesystem-as-context + recitation; Anthropic `clear_tool_uses` semantics |
| 3 | OpenAI conversation state; Chroma numbers; Lost in the Middle; MemGPT; MCP subscription client support | `previous_response_id`/`store`; TACL U-shape; no evidence of broad subscription support in clients |
| 4 | Observation masking / "Complexity Trap"; compression systematic study; scratchpad/state-file patterns | **The Complexity Trap** (halving cost, masking ≈ summarization); **Beyond Token Savings** (fewer tokens ≠ cheaper) |
| 5 | MCP ETag/If-None-Match; SEP-2549 final; SEP-2567 state handles; Routerai caching | No ETag/conditional requests in MCP; SEP-2567 explicit handles; Routerai cache-read 0.33 ₽, no cache write; provider selection by stability then price with fallbacks |
| 6 (saturation) | "MCP resource subscription client support", "MCP client resource template support" | **Nothing new** — only restatements of rounds 1–5 |
| 7 (saturation) | "routerai кэширование", "MCP long-running task state" | Only vendor marketing pages and the Tasks extension, which is out of scope here |
| 8 (revision 2) | "openai codex mcp resources list_mcp_resources read_mcp_resource"; "codex mcp tool output truncation"; "codex auto compaction token limit" | Codex source files for MCP resource tools, `truncate_mcp_tool_result`, `truncation_policy`; issue #16159 (no subscribe); config reference |
| 9 (revision 2) | "MCP stateless clientInfo _meta 2026-07-28"; "MCP resources list must not vary per connection"; "paged tool result manifest" | Stateless `_meta` requirement; list-stability rule; no protocol-level page primitive — paging must live inside tool arguments |
| 10 (saturation) | "portable MCP panorama handle revision delta"; "MCP resync stale base refusal" | **Nothing new** — no prior art for a revision-bound panorama contract; the design follows SEP-2567 plus ordinary tool schemas |

**Saturation point:** rounds 6, 7 and 10 produced no new architectural method or primary source. Duplicate/failed searches: "MCP resource subscription client support" returned only generic handbooks; "MCP etag" returned HTTP references and non-MCP servers; the 2025-11-25 resources page and the 2026-07-28 page are near-duplicates on subscription semantics (kept both, as the revision change matters).

---

## 12. Unresolved questions for Workstream B

1. **Handle vs revision.** Is there a monotonic, durable journal sequence in the archive that can serve as `base_revision`, or must the client carry a separate `handle`? `change_count` is explicitly unreliable (`CONTEXT-LEDGER-DESIGN.md` §"Do not treat REAPER's coarse change_count as a semantic delta"), so what is the stable surrogate? Does the handle need to pin schema and page budget, or is `(project_id, rev_id, fact_root)` sufficient?
2. **Page boundaries.** What is the largest irreducible section in the document, and does a 24 KB page split it cleanly? If a single entity (e.g. one track's MIDI) exceeds the budget, does it become its own content-addressed section, and does that violate "one unified document" in practice?
3. **Delta size distribution.** For the four canonical edit classes (fader, FX param, item move, routing change), what is the measured compact delta size *including* invalidated analysis? If a fader move invalidates every windowed RMS, the delta is not small.
4. **Delta shape.** Can a delta be "panorama-shaped" (owner addendum) and still be small, or does panorama-shape force re-sending unchanged context? If the latter, what exactly does the owner accept as a delta?
5. **Interval semantics.** Does `[36,45)` require a *new capture* when the window is outside the captured audio, and what is the cost/latency of that path (the 20 s capture measured 78.5 s end-to-end)?
6. **Identity stability.** Do GUIDs survive track delete/recreate and project re-open? HB-007 in `HERMES-MIXING-BEHAVIOR-LEDGER.md` records a master GUID changing between baseline and later captures — unresolved and directly threatening delta matching.
7. **Invalidation completeness.** Enumerate exactly which analyses are invalidated by: mute/solo, master FX change, tempo-map change, sample-rate change, analysis-version upgrade. This determines whether a "small delta" can be honest.
8. **Verification fields.** What is the minimal header every document and delta must carry so a client can verify continuity without trusting the server's ordering — `rev_id`, `parent_rev`, `fact_root`, `document_sha256`, `schema_version`, `chg_id` chain, per-section hashes? Which of these can be omitted without losing level-1 or level-2 verification (§R2.5)?
9. **Partial-document cost.** If only some pages are resident, which of the ten whole-project questions in §10.3 become unanswerable without another call? That number sets the real cost of a client that stops paging early.
10. **Contract wording.** Does the owner accept <em>multi-page delivery of one document</em> as satisfying "not a summary plus a fetch instruction"? This is a requirements question, not a technical one, and it gates R2.8.

---

## 13. What I could not verify

- **The DeepSeek tokenizer's actual count** for the panorama. No tokenizer is installed or wired in Hermes, and I did not run a remote tokenizer. The 128,449 figure is a Music Flamingo measurement of the same bytes; the 59,370 figure is Hermes's estimator. The true DeepSeek count is unknown and is probably between them.
- **Whether Routerai preserves prefix-cache locality** across calls. It publishes no sticky-routing guarantee; OpenRouter's equivalent documentation exists, Routerai's does not. The 0.33 ₽ cache-read price proves the *billing* path exists, not that this session hits it.
- **Whether `prompt_cache_hit_tokens` is surfaced in Routerai responses.** Hermes's usage parser accepts it, but I did not make a billed request to inspect a live response body.
- **Whether any Hermes client path can display resource *templates*.** I verified the absence of `resources/templates/list` in code; I did not test a live `list_resources` against the REAPER server, so I cannot state from observation that only one resource appears — only that the code never asks for templates.
- **The exact behaviour of `compress()` on a panorama-heavy transcript** (how much of the panorama survives summarization). This needs the empirical run in §10, not code reading.
- **Whether `tool_budget.mcp_result_size_chars` accepts values above 100,000 in practice.** The code caps it at `default_result_size`; I did not test a live override.
- **Owner acceptance** of any change to the "no URI instead of the panorama" contract. That is a decision, not a fact.
- **Codex against a live REAPER server.** I confirmed the resource tools, truncation policy, config keys and compaction prompt from the installed binary, `codex debug models`, and the `openai/codex` source, but I did not connect this Codex build to the REAPER MCP server and observe a 237 KB panorama being truncated. The behaviour is inferred from the code path that every MCP tool output passes through, not from a live call.
- **Whether `tool_output_token_limit` overrides the per-model `truncation_policy` in this build.** The config reference documents it as the history budget for tool outputs; I did not run a live override to confirm precedence.
- **Codex's exact auto-compaction threshold in this install.** The ~90 %-of-raw-window figure comes from a public issue report, not from a local measurement.
- **How `tool_mode: code_mode_only` surfaces separate panorama tools in the Codex TUI.** The catalog reports the mode; I did not exercise tool selection in an interactive session.
