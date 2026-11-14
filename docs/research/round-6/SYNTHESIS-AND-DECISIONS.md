# Round 6 — Synthesis and decisions: one portable panorama, revision-bound deltas, explicit refresh

> **Historical record (superseded transport).** This document describes the
> paged panorama contract that has since been replaced by one whole-view result.
> Current behaviour: `docs/DECISION-single-response-panorama.md` and
> `docs/MCP-README-LLM.md`. Treat this file as history, not instruction.


Date: 2026-11-13
Status: Research and decision record. No code, tests, MCP surface, capture archive or configuration
was changed by this research workstream. The owner has now authorized a follow-on phase to build the
production-ready MCP surface and model-facing README, then evaluate it in a fresh Hermes session.
The execution scope is recorded in `docs/research/round-6/IMPLEMENTATION-DIRECTIVE.md`.

Inputs:

- `docs/research/round-6/AGENT-A-CONTEXT-CONTINUITY.md` (Workstream A, final revision).
- `docs/research/round-6/AGENT-B-INCREMENTAL-PANORAMA.md` (Workstream B, final revision).
- The dispatch and its owner clarifications of 2026-11-13 (client-agnostic contract; full open /
  changes-since / explicit refresh; no client-specific feature; no reduced temporal grid).
- Read-only inspection of the capture archive and the installed Hermes 0.23.1 and Codex
  `0.167.0-alpha.4.1` clients.

Research agents: exactly two logical subagents, A and B. The same two were re-engaged for revision
after the owner clarifications; no third agent or workstream was created.

Labels: **[OBS]** observed in the checkout, archive or installed client; **[SRC]** supported by a
cited primary source; **[INF]** inference from observed facts plus sources; **[DEC]** a decision this
synthesis recommends. Statements marked unresolved are not decisions.

---

## 0. Decision summary

| ID | Decision | Status |
|---|---|---|
| **D6-01** | The product contract is a **portable MCP tool contract**, not a resource-only or client-specific design. It must work with a stock tools-only client, Codex and Hermes. Resources, templates and subscriptions are optional mirrors, never the required path. | DEC |
| **D6-02** | Separate the **immutable archive**, the **server-maintained current projection**, and the **client-held panorama state**. The server is the source of truth; the client holds only `project_id` + `revision` + `fact_root` (and optionally an opaque handle). | DEC |
| **D6-03** | The owner accepts a multi-call initial load as transport for **one logical complete panorama**, provided every page is delivered and verified before project edits or whole-project claims. Use deterministic pages, a manifest and page/document hashes; default `page_bytes = 24,576` for measured Hermes/Codex profiles, adjustable lower for clients with smaller caps. | OWNER-ACCEPTED; delivery gate required |
| **D6-04** | The 20,838-byte “spine” is a **rejected lossy experiment**, not a candidate, default or fallback. It drops the 30 s temporal level (50,567 B on its own), rounds numeric values and omits units, uncertainty, provenance, validity, automation, per-track detail and explicit relationships. | DEC, owner-rejected |
| **D6-05** | Full temporal granularity is preserved in the full panorama: **both the 30 s and 300 s levels**, and any finer level present. A 300 s-only or otherwise reduced time grid is not a full panorama. | DEC, owner-binding |
| **D6-06** | A lossless compact encoding may be evaluated only if it preserves every numeric value, both temporal levels, provenance, units, validity/coverage/uncertainty and relationships. The measured lower bound is **169,285 B**, still far above both named clients, so even a lossless compact form pages. | DEC |
| **D6-07** | `panorama_changes_since` requires an **exact base**: `base_revision` plus `base_fact_root` (or the exact issued `document_hash`). It returns `current`, `delta`, `stale_base` or `refresh_required`; never a partial patch. A delta applied to the base must reproduce the target fact root. | DEC |
| **D6-08** | `panorama_refresh` is a normal, always-available operation, not a hidden recovery path. It is mandatory after a missing/out-of-order delta, unsupported base, project identity/epoch change, schema incompatibility, unknown handle or any condition where continuity cannot be proven. | DEC |
| **D6-09** | Use **separate explicit tools** (`open`, `changes_since`, `refresh`, `page`, `slice`, `status`) rather than one mode-switching tool. The decisive reason is schema enforcement plus per-tool client policy: both named clients bind output budgets and approval behaviour to a tool name. Names remain provisional until the acceptance run. | DEC |
| **D6-10a** | Canonical **storage/transport** remains typed JSON with JCS (RFC 8785) canonicalisation and content hashes. It is internal/interchange only; do not expose a disconnected set of JSON files or a manually unfolded hierarchy as the model's project view. | SETTLED (storage) |
| **D6-10b** | The **model-facing representation is unresolved**. Normalized linked JSON, a natural-language/structured-text panorama, and hybrid forms are candidates. No format is declared the winner without task-based comprehension checks on actual Workbench questions (whole-project relationships, track/time-window comparisons, FX/routing state, exact measurements, deltas). The one-panorama/no-loss contract is fixed regardless of format. | OPEN |
| **D6-11** | Invalidation follows the **render graph** and `render_key`, never audio-file hashes. A re-render of identical content produced 43/43 different SHA-256 values in the archive; audio hashes cannot identify “unchanged”. | DEC |
| **D6-12** | Whole-song state is never narrowed by an interval request: `state.scope = project`; only `evidence.scope` narrows. `[36,45)` returns the whole project structure plus full-resolution window evidence, with explicit “not in scope” coverage. | DEC |
| **D6-13** | The full document must be made actually full before it is called full: today's 241 KB panorama lacks measurement method/version/stage/channel/window/units, capture validity on the read path, populated uncertainty and several digest fields. Those are prerequisites, not optional refinements. | DEC |
| **D6-14** | The owner authorized the follow-on implementation: complete, client-agnostic MCP integration for the library plus the English/Russian LLM README, then a fresh-session Hermes task evaluation. The research phase is not itself a code patch. No client-specific context feature is required. | OWNER-AUTHORIZED |

---

## 1. What is verified, inferred and unresolved

### 1.1 Verified locally in the archive and source

| Fact | Measurement / evidence | Label |
|---|---|---|
| Full model-facing panorama, 42 tracks + master, 43 sources | 241,342 compact bytes; sidecar 395,336 B | OBS |
| Earlier full-song capture, 41 tracks + master | 237,477 B compact / 388,654 B sidecar; 128,449 tokens under the external Music Flamingo tokenizer | OBS |
| 20 s capture, 41 tracks | 198,480 B compact / 291,445 B sidecar | OBS |
| Temporal levels | 30 s: 473 rows / **50,567 B**; 300 s: 86 rows / 2,128 B | OBS |
| Lossless compact lower bound (dedupe constants, drop derivable indexes/crosslinks, keep every numeric value and both temporal levels) | **169,285 B**; with crosslinks 187,610 B; with inline canonical IDs 181,930 B | OBS |
| Rejected reduced spine | 20,838 B, reduced to 300 s and metadata-stripped | OBS |
| Audio hash behaviour | Two captures with `digest_fields_equal=true` produced **43/43 different** audio SHA-256 values; **42/43** analysis metric vectors were identical | OBS |
| Invalid master read-back | `cap_01M7A9ES…` has `capture_status=invalid_master` in `record.json`, but `reaper_capture_read` returns `ok:true`, `coverage.complete:true`, `truncation.silent_drop:false`, all rows `render_provenance.complete:true`, and no validity field | OBS |
| Take identity | 34/34 take rows have `track_id: null` | OBS |
| FX pointers | 15 FX rows share only 5 distinct `raw_parameters_pointer` values | OBS |
| Time-map identity | `time_map_id` resolves to `time_map:cap_<capture_id>`, so an unchanged tempo map gets a new identity per capture | OBS |
| Analysis identity | `analysis:<source_guid>` is method-blind; analysis rows are a 9-value positional array without method/version/stage/window/units | OBS |
| Redundancy | 78,104 of 241,342 B (32.4%) are repeated constants, derivable identity plumbing or duplicated relations | OBS |
| Digest omissions | Track digest rows omit `channel_count`/`name`; item digest rows omit `channel_mode`/`group_id`/`source_channels`; `I_PANMODE` appears only in the API catalogue, nowhere in bridge/digest/panorama | OBS |
| Existing revision resource | `reaper://projects/{sid}/{pid}/context/{change_count}` is revision-pinned and raises `StaleContext`; it is a useful addressing primitive, not a continuity mechanism | OBS |
| Existing capture archive | 26 captures, 7 panorama sidecars, content-addressed audio; read-only | OBS |

Repo evidence: `src/reaper_audio/context/panorama.py:31-36, 543-553, 1148-1171, 1549-1585, 1588,
2279-2324`; `src/reaper_audio/mcp_server.py:1205-1221, 1690-1811`;
`src/reaper_audio/context/revision.py:73-128`; `scripts/reaper_bridge.lua:1032-1136`;
`work/benchmark/himmp/capture-archive/captures/`.

### 1.2 Verified client and protocol behaviour

| Fact | Evidence | Label |
|---|---|---|
| Hermes 0.23.1 spills any MCP result above **50,000 chars** to `$HERMES_HOME/cache/spillover` and substitutes a **1,500-char preview + path** | `tools/budget_config.py:13-20`; `tools/tool_result_storage.py:276-281, 391-408, 422-463` | OBS |
| Hermes's absolute inline MCP ceiling is **100,000 chars**; `mcp_result_size_chars` is capped by `default_result_size` | `tools/budget_config.py:56-75, 92-114` | OBS |
| Hermes compaction trigger is ~0.50 of the effective input window (raise-only 0.75 floor below 512K); summary ceiling is 10,000 tokens; `proactive_prune_tokens` defaults to 0 | `config.yaml:686-690, 773-780`; `agent/context_compressor.py:828, 2225-2239, 2751-2790`; `hermes_cli/config_defaults.py:596-607` | OBS |
| Hermes `changes_since` is a tool call; a `read_resource` result is also a tool result and **appends** content. Resource templates are never requested; subscriptions are not consumed | `tools/mcp_tool_handlers.py:839-844`; `agent/context_engine.py` (only as client behaviour); grep for `resources/templates/list` / `resources/subscribe` yields nothing | OBS |
| Codex CLI installed: `codex-cli 0.167.0-alpha.4.1`; model catalog `gpt-6-luna` has `context_window: 272000`, `truncation_policy: {mode: "tokens", limit: 10000}` | `codex --version`; `~/.codex/models_cache.json` | OBS |
| Codex registers `list_mcp_resources`, `list_mcp_resource_templates`, `read_mcp_resource`; it does **not** implement `resources/subscribe`/`unsubscribe` and only logs `notifications/resources/*` | `openai/codex` issue #16159 (opened 2026-03-29); `mcp_resource.rs`; local `codex mcp list` | SRC/OBS |
| Codex truncates an oversized MCP result with `truncation_policy` plus a 20% serialization allowance, counted at 4 bytes/token, keeping head and tail and cutting the middle | `codex-rs/core/src/tools/context.rs`; `codex-rs/utils/string/src/truncate.rs` (`APPROX_BYTES_PER_TOKEN = 4`); `codex-rs/core/src/context_manager/history.rs` | SRC |
| MCP current revision is **2026-07-28**: no protocol sessions/`initialize`; cross-call state uses explicit server-minted handles (SEP-2567); `subscriptions/listen` replaces `resources/subscribe`; `notifications/resources/updated` carries only `uri` (+ subscription id); `ttlMs`/`cacheScope` are advisory and there is no ETag | MCP 2026-07-28 changelog, `server/resources`, `basic/patterns/subscriptions`; SEP-2549; SEP-2567 | SRC |
| Routerai: cache read **0.33 ₽ / 1M** input tokens; cache write unsupported; routing is stability first, then lowest price, with fallbacks; the earlier “40 providers” count could not be re-verified and is withdrawn | `routerai.ru/models/deepseek/deepseek-v4.1-flash-1017`; `routerai.ru/docs/guides/overview/provider-selection` | OBS/SRC |
| Current session model: `deepseek/deepseek-v4.1-flash-1017`, 1,048,576-context, tokenizer label “DeepSeek”; config default is `upstage/solar-pro4`, 524,288. Neither is proven to be the future mixer model | `/v1/models` query; `config.yaml:72-75, 105-110, 2421-2452` | OBS |
| Hermes has no runtime per-model tokenizer; `CHARS_PER_TOKEN=4`; actual usage comes from provider `usage` | `agent/model_metadata.py:2336-2366`; `agent/context_engine.py:75-82` | OBS |

### 1.3 What is inferred, not verified

- The DeepSeek tokenizer's actual count for the panorama is unknown. The 128,449 figure is an
  external Music Flamingo measurement; the Hermes estimator gives 60,336 for 241,342 B. Treat both
  as different counters, never as one “token count”. [INF]
- REAPER renders are non-deterministic on this archive (43/43 hashes differ with identical digested
  content). The cause (float summation, dither, plugin state, denormals) is inferred, not proven.
  [INF]
- GUID reuse after track delete/recreate is assumed not to happen; not tested. [INF]
- The generic tools-only client's per-result cap is unknown; the contract must assume a small cap
  and detect truncation. [INF]
- Production serializer sizes after adding the missing metadata will be larger than the 169,285 B
  lower bound; the exact size must be re-measured. [INF]
- Model attention cannot be guaranteed by any protocol or client; the empirical test measures it,
  not proves it. [INF]

### 1.4 Owner decision recorded

On 2026-11-13 the owner accepted deterministic multi-call page delivery as the transport for one
complete logical panorama, with the requirement that the model receive every page before it makes
project-wide claims or edits the project. A literal single result cannot hold a lossless full
panorama on the measured Hermes/Codex profiles: the 30 s temporal block alone is 50,567 B, above
both per-result limits. The reduced/lossy one-result projection is rejected. The server must track
page delivery, verify page and document hashes, and block mutations until a complete-view receipt is
issued. This proves delivery through the tool flow, not model attention; comprehension remains an
acceptance test. Page-size negotiation and behavior for unknown client limits remain technical
requirements, not owner decisions.

---

## 2. Three artifacts, four ledgers, one contract

| Artifact | Lifetime | Owner | Mutable? | Identity |
|---|---|---|---|---|
| **Immutable archive / journal** | permanent, append-only | server archive | never | `cap_`, `rev_`, `fact_root`, content hashes |
| **Current projection** | per `(project, fact_root, evidence_set)` | server | rebuilt, never edited | `document_hash`, `schema_version` |
| **Client-held panorama state** | until the client loses it | client/model | replaced only by open/refresh | `(project_id, revision, fact_root, handle?)` |
| **Delivery** (page, delta, slice, status) | one tool result | MCP | n/a | `page i/n`, `from_root`/`to_root` |

The four ledgers remain distinct:

| Ledger | What it holds | Who changes it | What it does not do |
|---|---|---|---|
| Conversation | the exact token sequence of one request | client/host only | does not make server state current |
| Provider cache | KV/prefix blocks | provider | does not remove tokens from the request |
| Server state | archive, projection, journal | server | does not enter the model until a read |
| Model attention | which received tokens influence the answer | nobody directly | cannot be guaranteed by MCP |

“Memory” belongs to server state (durable) and only weakly to the conversation when content is
resident. A prompt cache is not memory; it is recomputation avoidance on a prefix that must still be
transmitted.

### 2.1 What the server stores versus what a client can actually keep active

**Server must store (authoritative):**

- The immutable capture archive: sealed records, per-track/master audio blobs, content hashes,
  analysis reports and the retention log.
- The revision journal: append-only `rev_` records with parent, `fact_root`, `domain_hashes`,
  `fact_coverage`; `chg_` delta records with `from_root`/`to_root` and idempotency keys.
- The active projection: the full document at `(fact_root, evidence_set)` (model-facing format
  chosen per §5), plus its deterministic page plan and page hashes. It is a rebuildable cache,
  never the source of truth.
- The handle registry: opaque handle → `(project, epoch, fact_root, schema, projection, created_at,
  TTL)`. Handles are optional; revision + root remain sufficient.
- The measurement/evidence ledger: measurement IDs, `series_key`, units, method/version, stage,
  channel, window, provenance, validity and uncertainty.

**Client can keep active (not guaranteed):**

- **Hermes 0.23.1:** a 1,048,576-token window; compaction fires around 0.50 of the effective input
  window (≈524K on this route), and any MCP result above 50,000 chars spills to a 1,500-char
  preview + path. A 24 KB page is safe per result; the full 241 KB document is ~130K real tokens by
  the only external measurement, so it fits the window after 10 pages but is not guaranteed to
  survive compaction. The server must assume it may be lost.
- **Codex 0.167.0-alpha.4.1:** a 272K-token window (95% effective); per-tool truncation at 10,000
  tokens counted at 4 B/token (~40 KB, ~48 KB with the 20% allowance); auto-compaction near 90%.
  A 24 KB page is safe per result; the full document fits the window, but the per-tool cap makes a
  single full response impossible.
- **Generic tools-only client:** unknown window and cap; assume it may truncate at any size. The
  contract must page and detect truncation, never infer a cap from `clientInfo`.

The server's guarantee is therefore not “the model still has it”; it is “the model can always prove
what it holds, get an exact delta, or obtain a bounded full refresh”. Correctness is recoverability,
not residence.

---

## 3. The portable contract

### 3.1 Tool surface

Working names only; the comparison in §3.2 is the decision, not the names.

| Tool | Arguments | Returns |
|---|---|---|
| `panorama_open` | `project_id`, `expected_revision?`, `page_bytes=24576`, `encoding="full"\|"lossless_compact"` | Page 0: identity, revision, `fact_root`, `document_hash`, coverage/states, handle, manifest, first payload chunk |
| `panorama_page` | `handle`, `revision`, `page_index` | One deterministic page; concatenation of payloads reproduces `document_hash` |
| `panorama_changes_since` | `project_id`, `base_revision`, `base_fact_root` (or exact issued `document_hash`), `handle?` | `current` / `delta` / `stale_base` / `refresh_required` |
| `panorama_refresh` | `project_id`, `expected_revision?`, `page_bytes=24576`, `encoding` | Same envelope as open, at the current revision; always available |
| `panorama_slice` | `handle`, `revision`, `window?` or `entity_ids?` or `domain?` | Whole-project structure at full structural resolution + window evidence at full numeric resolution; explicit `evidence.scope` and `coverage.not_in_scope` |
| `panorama_status` | `project_id` | Tiny current-target record: `revision`, `fact_root`, `document_hash`, `schema_version`, optional `handle` |

The minimum durable client state is **`project_id` + `revision` + `fact_root`**. A handle is an
optimisation: it lets the server verify that the caller holds the matching document and hand back a
projection-consistent slice, but a lost handle must not require a refresh if the revision and root
still match. A bare revision string without the root is not a base.

### 3.2 Separate tools versus one mode-switching tool

| Criterion | One `panorama(mode=…)` | Separate explicit tools |
|---|---|---|
| Model discoverability | mode semantics live in the description; models may default or omit it | names state intent: open / changes-since / refresh |
| Schema reliability | union of fields; `base_revision` conditionally required, enforced by prose | closed schema per tool; base arguments required exactly where needed |
| Error semantics | one result union; wrong mode may look valid | wrong call is structurally impossible; missing base fails before execution |
| Per-tool client policy | impossible to give open a large budget and deltas a small one | works: both Hermes and Codex bind budgets/approval to tool names |
| Wrong-mode risk | real (open where refresh was meant; stale base treated as current) | low: refresh cannot be mistaken for a delta |
| Cost | one definition | a few hundred extra tokens; negligible against 169–241 KB |

**Decision:** separate explicit tools. `open` and `refresh` may share one handler internally, but
the names stay distinct because `refresh` is the explicit recovery operation and cannot be confused
with a delta. `page` is transport, not a lifecycle mode. `slice` is evidence, not a completeness
claim. `status` is a tiny lost-state aid; it may be omitted if the tool count is a problem, but the
contract is simpler with it.

### 3.3 Open / changes-since / refresh semantics

**Open** returns one revision for the whole response; every domain is present, every existing
entity appears once, coverage/validity are explicit, and the page manifest is complete. If
`expected_revision` is supplied and does not match, the server returns `stale_base` with the current
revision and no document; the caller either opens current (omit the argument) or refreshes.

**Changes-since** accepts only an exact base. The server answers one of four states:

- `current`: base equals target; no content.
- `delta`: `{base_revision, target_revision, from_root, to_root, chain, ops[], unchanged{domain:{count,hash}}, invalidations[], coverage_delta, handle}`.
- `stale_base`: the base is not an ancestor, or belongs to another project/epoch/schema; no delta.
- `refresh_required`: the base is an ancestor but the journal chain is missing, too long, or the
  projection was evicted; the current target and the exact refresh call are returned.

`ops[]` are factual `add`/`replace`/`remove` by entity ID and field path, with `before`, `after` and
`unit`. `unchanged` names every domain that was not re-sent, with counts and hashes, so silence is
not mistaken for “nothing changed”. Invalidations are explicit `set_state` ops, not prose. There is
no `better`, `worse`, score or ranking field anywhere.

**No silent patch.** A wrong base is rejected; the server never rebases, never merges best-effort,
and never returns a delta whose `from_root` differs from the caller's declared base. Idempotence is
by `chg_id`; a duplicate returns `already_applied` with the same `to_root`.

**Delta verification.** Every delta names `from_root` and `to_root`; the chain is contiguous
(`chain[i].to_root == chain[i+1].from_root`). A client library recomputes the target root from the
base document plus `ops[]` using the published JCS/Merkle canonicalisation. A model cannot compute
SHA-256 in its head; the model-checkable part is the chain and the unchanged counts, and the
library/reference-client check is the acceptance test. If either check fails, refresh.

**Refresh is normal.** It is valid at any time, with no preceding error. It is mandatory after:
a missing/out-of-order/duplicate delta; a non-ancestor or unknown base; a project identity or epoch
change (Save-As, move, reopen, second REAPER instance); a schema incompatibility; a handle whose
projection was evicted; a page/artifact that fails its head/tail completeness check; a paraphrased
or truncated revision string; or a `client_root` that does not match the root the server issued for
that revision. A full refresh is not a recovery-only hidden path.

### 3.4 Client-state table

| Client state | What it has | Portable action |
|---|---|---|
| Fresh / new chat / restart | nothing | `panorama_open`; page 0 + manifest, then pages 1..n |
| Handle + revision + root | the document and its identity | `panorama_changes_since`; on `stale_base`/`refresh_required` → `panorama_refresh` |
| Revision + root, no handle | a valid base, no server-side handle | `panorama_changes_since` with the exact base; refresh if the chain is unavailable |
| Revision string only (post-compaction summary) | no document, no root | `panorama_refresh`; a paraphrase or truncated revision is not a base |
| Pages 0..k of n, k < n | a partial document | `panorama_page` for the missing indices; verify `document_hash`; no edit before complete |
| Truncated / spilled / middle-cut artifact | an incomplete result | `panorama_refresh` with a smaller `page_bytes`; never use as a base |
| Project identity or epoch changed | an old base | `panorama_refresh`; record lineage |
| Unknown schema | an old document | `panorama_refresh` with the current schema; do not reinterpret |

MCP cannot observe client memory. These are caller rules, not server-side detection. The server can
make them safe by binding writes to `expected_revision` and the exact `document_hash`; a stale or
unverified client cannot silently overwrite the project. The server cannot prove the model read
every page; the acceptance test and the contract rule (“no edit before all pages and hash”)
measure compliance.

### 3.5 What MCP guarantees and what it cannot

**Server guarantees, independently of host:**

- Stable identity: project/instance/epoch, entity IDs, capture/revision IDs.
- An immutable archive of captures, records, audio blobs and analysis; no destructive rewrite.
- One current projection per revision; deterministic rebuild.
- Append-only revisions with parent links and a `fact_root`; factual deltas between named roots.
- Content hashes and a deterministic `document_hash`; reproducible pages and page hashes.
- Dependency invalidation and explicit `stale`/`invalid`/`not_captured`/`incomparable` states.
- Consistent interval views: whole-project structure, narrowed evidence, absolute buckets.
- A bounded full refresh, always available; a declared page count and rebuild time.
- Schema versioning and a capability surface that a stock client can call.

**Not guaranteed by MCP or any host:**

- That the client retains an earlier tool result forever (Hermes spills; Codex truncates; both
  compact).
- That an earlier conversation message can be replaced by a newer one.
- That the model attends to every field it received.
- That an arbitrary-size response is accepted; no protocol maximum exists, and client caps differ.
- That a notification is delivered (Hermes has no resource-update handler; Codex logs it; the
  protocol message carries only a URI).
- That resources or templates are discoverable (Hermes does not request templates; Codex does;
  generic clients may not).
- That the server can observe client memory, page possession, or the model's understanding.

**No client-specific integration is required.** The only client obligations are portable caller
behaviours: fetch all pages, carry `project_id` + `revision` + `fact_root`, verify the hash, and
refresh when continuity cannot be proven. A client library may automate them; a bare model can do
them with ordinary tool calls. That is why the contract is cross-client.

The correct framing is **correctness is recoverability, not residence**. The server makes the
client's state verifiable, the delta exact and the loss recoverable. Whether the model still “has”
the panorama at turn 40 is a host property; the contract is designed so the answer does not change
any guarantee.

---

## 4. Initial full panorama: the 20.8 KB / 169 KB reconciliation

### 4.1 The measured forms

All forms measured on `cap_01M7AADBXY4XCH11E3WZTKA3BT` (241,342 compact bytes, 42 tracks + master,
43 sources):

| Form | Bytes | Share of full | What it preserves | Fits one stock result? |
|---|---|---:|---|---|
| Full document | 241,342 | 100% | everything currently captured | no |
| Lossless compact, lower bound: hoist repeated audio constants, drop `per_track`/derivable indexes/crosslinks, keep every numeric value and **both** temporal levels | **169,285** | 70.1% | semantics-preserving; only derivable indexes removed | no |
| Lossless compact + crosslinks | 187,610 | 77.7% | above plus the relationship graph | no |
| Lossless compact + inline canonical IDs | 181,930 | 75.4% | above plus self-contained IDs | no |
| Rejected reduced “spine” | 20,838 | 8.6% | all entities, but 300 s only, 3 dp, no units/uncertainty/provenance/validity/automation/per-track/full relations | yes, by losing the full document |
| 30 s temporal block alone | **50,567** | 21.0% | 473 rows, 43 sources; no other domain | no |

The 30 s block alone is decisive: **50,567 B is already above Hermes's 50,000-char spill threshold
and above Codex's ~40,000 B byte counter**, before a single track, item, FX row, audio asset or
analysis row is added. No semantics-preserving full panorama can fit one response on either named
client. Page transport is therefore mandatory, not an optimization.

### 4.2 The rejected 20,838 B spine: exact omissions

The spine audit found at least these omissions or reductions (B report R2.5.1; parent spot-check):

| Required fact | Full document | Reduced spine | Verdict |
|---|---|---|---|
| 30 s temporal level | 473 rows, 50,567 B | 300 s only, 86 rows | **omitted** |
| 300 s temporal level | 86 rows, 2,128 B | retained | retained |
| Metric units | `analysis_metric_units` 237 B | absent | **omitted** |
| Analysis method/version/params | absent in today's full document | absent | **missing in both** — must be added |
| Stage | per-asset `render_provenance`, 2 variants | one derived column | reduced |
| Channel view | absent | absent | **missing in both** |
| Exact window | `audio_file_window` 7,095 B, one constant | start/end per row at 3 dp | retained but precision reduced |
| Render provenance (settings, sample rate, channels, tail, plugin versions) | 255 B × 43, 2 variants | stage + one tail column | **omitted** |
| Capture validity (`capture_status`, `master_validation`) | absent on the read path | literal “valid” | **omitted in both** — must be added |
| Coverage | counts, `complete:true` even for an invalid capture | copied counts | reduced to counts; cannot express validity |
| Uncertainty / epistemic layers | structurally `[]` | absent | **omitted** |
| Per-entity relationships | 333 edges + integer legend | implicit foreign keys only | reduced; non-FK relations lost |
| Time map | 1,651 B, all points | 319 B, first 4 points | **truncated** |
| Tempo / time signatures | present | present | retained |
| Automation / envelopes | 827 + 825 B | absent | **omitted** |
| MIDI | 0 rows in this capture | absent | **omitted** (matters when non-empty) |
| Markers / regions | 0 rows in this capture | absent | **omitted** (matters when non-empty) |
| Float precision | 17 significant digits | 3 decimal places | reduced |
| Stable identity mapping | `stable_id_legend` 16,479 B | aliases inlined, legend dropped | **identity broken** |
| Per-asset constants | 32 KB repeated | hoisted | retained only if the block is complete |
| `per_track` counts | 7,020 B | absent | reduced (derivable by counting) |
| `truncation`, `selection`, `transport`, `domain_counts` | 334 B | absent | **omitted** |
| `audio_time_contract`, `audio_units`, `audio_capture_revisions` | 1,253 B | absent | **omitted** |
| `project`, `capture_binding` | 4,613 B | absent | **omitted** |
| `analysis_archive`, `provenance`, record pointers | 899 B | absent | **omitted** |

The spine is a **presentation subset**, not a full panorama. It cannot be called complete, cannot be
the default, and cannot be a fallback without a new explicit owner decision. The owner has rejected
it because it substitutes 300 s for the original 30 s grid and hides detail.

### 4.3 Page transport versus stock-client caps

| Client / version | Per-result behaviour | 241,342 B full | 169,285 B lossless | 20,838 B spine |
|---|---|---|---|---|
| Hermes 0.23.1 | spill >50,000 chars → 1,500-char preview + path; absolute 100,000 chars | spills | spills | fits |
| Codex 0.167.0-alpha.4.1 | `truncation_policy` 10,000 tokens, counted at 4 B/token (~40 KB), 20% allowance (~48 KB), middle cut | truncates | truncates | fits by its counter |
| Generic tools-only | unknown; assume a small cap and detect truncation | unknown | unknown | unknown |
| Paged full at 24,576 B | each page ≤24 KB payload + ≤2 KB envelope | **10 pages** | **7 pages** | n/a |
| Paged full at 24,576 B, 8 KB client | 30 pages at 241 KB | 30 | 21 | n/a |

24,576 B is chosen as approximately half of Codex's ~48 KB effective budget and half of Hermes's
50,000-char spill threshold, leaving ~2× margin for the response envelope and JSON escaping. A 32
KiB page fits on paper but leaves only ~1.5× margin against Codex and none against pathological
escape expansion. The budget is **client-declared**, never inferred from `clientInfo`: a server that
branches on a self-declared client name becomes client-specific and breaks on the next client.

### 4.4 Does page transport meet “one unified whole-project panorama visible on initial load”?

**Content: yes, as one logical document. Single response: no.** The owner has accepted the full
page sequence as the initial load, provided the completion gate is implemented.

- The pages are one logical document: same `project_id`, `revision`, `fact_root`, `schema_version`
  and one `document_hash`; fixed canonical order; page 0 carries the manifest (`total_pages`,
  `total_bytes`, per-page `page_hash`, section→page map); concatenating payloads in order
  reproduces `document_hash`; a page from another revision is `resync_required`.
- It is **not a hierarchy** and requires no manual expansion of domains. The model does not choose
  what to unfold; it receives the document in fixed order.
- It is **not a single result**. Page 0 alone shows the manifest and the first sections, not the
  whole project. Full whole-project awareness exists only after all pages are received and the hash
  verifies. A model that stops at page 3 has a partial document and must not claim the whole.
- The owner explicitly accepts the complete open sequence (all pages) as the initial load, provided
  no write or whole-project claim is allowed before all pages and the document hash verify.
- What the model can see without manual drill-down: from page 0, identity, revision, coverage,
  validity states, section list and the first sections; from the reassembled document, everything;
  from `panorama_slice`, the whole project structure plus one window's evidence. There is no state in
  which a partial document is presented as the whole.

### 4.5 The full document must be made actually full

Today's 241 KB “full” panorama is not yet the required full contract. Before it can be called full,
it must add or expose:

- Per measurement: metric names **and units**, method, method version, params hash, stage, channel
  view, exact window, sample rate, uncertainty. The current `analysis[]` row is a 9-value array with
  no metadata; `analysis_metric_units` exists only at top level.
- Per audio asset: capture validity, render settings, sample rate, channels, tail, render stage and
  revision; currently `render_provenance` is partial and the read path drops capture status.
- Per domain: expected/observed/unknown counts, `truncated`, `not_captured`, `invalid`, `stale`,
  `incomparable`; coverage must not be one `complete:true` boolean.
- `epistemic_layers.uncertainty`: populated notes or an explicit empty reason; currently structurally
  `[]`.
- Identity: stable `time_map` hash (not `time_map:<capture_id>`), measurement identity plus a
  `series_key` for before/after pairing, non-null take `track_id`, correct FX pointers, full
  relationships.
- Digest fields: pan law (`I_PANMODE`), track `channel_count`/name, item `channel_mode`/`group_id`/
  `source_channels`; otherwise a sound-changing edit can leave the revision fingerprint unchanged.

These additions increase the document above 241 KB, so the 169,285 B lossless lower bound is a lower
bound on today's facts, not a promise about the target full document. Pages remain mandatory.

---

## 5. Representation: storage finding, model-facing format unresolved

**What is settled: canonical storage and transport.** Typed JSON with JCS (RFC 8785)
canonicalisation and content hashes is the internal archive/interchange format. It is the right
substrate for integrity, patching and reproducible hashes, and it is not a claim about what the model
should read. The canonical record may remain JSON; a disconnected set of per-domain JSON files, or a
manually unfolded hierarchy, must not be exposed as the model's project view.

**What is not settled: the model-facing representation.** The owner requires one coherent,
cross-linked, whole-project panorama — not disconnected JSON artifacts and not a summary plus fetch
instructions. Several forms remain candidates:

| Candidate | What it would look like | Current evidence | Status |
|---|---|---|---|
| Normalized linked JSON | entities + one edge table + columnar numeric tables + states/coverage/uncertainty + refs | 32.4% redundancy removable; no Workbench comprehension test | Provisional baseline, **not** winner |
| Natural-language / structured-text panorama | sectioned prose with explicit track/item/FX IDs, tables for measurements, inline relations and provenance | Closest to “natural unified panorama”; no controlled Workbench test | Candidate |
| Hybrid structured text + tables | natural-language labels and relations around compact tables, with resolvable refs | Format studies show no aggregate winner; table/graph effects are task-dependent | Candidate |
| Timeline matrix / graph view | lanes/time matrix or edge-first graph as the whole view | Columnar numeric gains are real; pure graph forces adjacency traversal; encoding effects 4.8–61.8% | Component, not whole-format winner |

**What the format evidence does and does not say.** 9,649 experiments across 11 models and four
formats found no significant aggregate accuracy effect (`χ²=2.45, p=0.484`); model tier dominated
(`arXiv:2602.05447`). Markdown had no reliable advantage and carried +22–37% token overhead
(`arXiv:2607.19257`). TOON/TRON saved 18–27% tokens but cost 9–14 accuracy points in agent loops
(`arXiv:2605.29676`); plain JSON won one-shot generation (`arXiv:2603.03306`). Columnar encoding cut
tokens 46–51% with no material loss on lookup/count/extract/aggregate, but only on Qwen2.5-7B and
synthetic operational data (`arXiv:2604.17512`). JSON tool-output processing remains hard and is
size/reasoning-dependent (`arXiv:2510.15955`). Graph encoding is task-dependent
(`arXiv:2310.04560`). None of these studies measured a DAW panorama, whole-project relationships,
track/time-window comparisons, FX/routing state, exact measurements or deltas. They constrain the
candidate set; they do not select a winner.

**Required evaluation before any winner is declared.** A blind, task-based comprehension test on
actual Workbench questions:

1. Whole-project relationships: which tracks feed a bus, which FX belong to which chain, what is the
   master chain, which items share a source.
2. Track/time-window comparisons: `[36,45)` versus the whole song; which sources have content,
   silence or no capture; bar/beat mapping.
3. FX/routing state: order, enabled/offline, parameters, sends/pre-post, automation.
4. Exact measurements: values with units, method/version, stage/channel/window, validity and
   uncertainty.
5. Deltas: what changed, what stayed unchanged, what was invalidated, what is incomparable.
6. Missing/stale/unknown detection: can the model tell “not captured” from “silent” from “invalid”?

Score accuracy, missing-fact detection, stale/provenance recognition, token cost and edit-planning
correctness, with answers extracted and graded blind against an archive-derived key. A candidate must
preserve the one-panorama/no-loss contract and all metadata. Until this runs, the model-facing format
is **unresolved**; normalized JSON is a provisional baseline, not the final surface.

**Consequences for the rest of the design.** The page/delta/tool contract is independent of the
model-facing format: it is defined over one document with a content hash, page plan, revision and
fact root. A later format change is a serializer change, not a lifecycle change. A lossless compact
encoding is admissible only if it preserves every fact field-by-field (169,285 B lower bound; still
pages). A reduced projection is not a format candidate; it is the rejected lossy product decision.
Both reports' preference for normalized JSON is recorded here as a candidate, not adopted as the
final model-facing format.

---

## 6. Revisions, deltas and invalidation

### 6.1 Revision and fact root

A revision is a content state, not a capture run. Several captures can share a revision (different
windows/channels/sample rates); one capture can span `revision_before` and `revision_after`.
`change_count` is diagnostic only: it moves on temporary capture mutes, can reset on reload, and is
not a semantic delta.

Each `rev_` record carries:

- `project_id`, `instance_id`, `epoch`, `counter` (diagnostic), `parent_rev`;
- `fact_root`: Merkle root over sorted `(entity_id, entity_fact_hash)` pairs, JCS over each entity's
  canonical fact row;
- `domain_hashes`; `fact_coverage` with `uncovered_presented_fields`;
- `capture_ids`, `created_at`, producer/version.

`fact_root` is the base a delta names. `document_hash` is the hash of the serialized model-facing
document at that revision; it verifies page reassembly. They are different hashes and must not be
conflated.

### 6.2 Delta record

A `chg_` record carries `from_rev`/`to_rev`, `from_root`/`to_root`, `from_capture`/`to_capture`,
`ops[]`, `unchanged{domain:{count,hash}}`, `invalidations[]`, `compare` (stage/channels/sample
rate/window/method/tempo map/verdict), `attribution` (operation/plan or `external`), `states` and a
schema version. Rules:

- Every op names an entity and a field path; no array indices.
- `null` is never “unknown” or “deleted”: deletion is `remove`; unknown is `state:"unknown"`;
  absent is `not_captured`.
- Every delta carries roots; it is applied only if the client's base equals `from_root`; otherwise
  refresh. Replay is idempotent by `chg_id`.
- `unchanged` is positive evidence, not silence.
- No `better`/`worse` field, no score, no ranking.
- A delta never crosses `project_id`, `instance_id`, `epoch` or lineage.

### 6.3 Invalidation matrix

The render graph is the dependency graph. Capture renders each selected track through its buses and
the master; isolating by muting other tracks while keeping destination buses open. Therefore a
master-FX change alters every track asset; a bus change alters every source routed into it; a
track's own change alters that asset plus every aggregate above it.

`render_key = sha256(JCS({track_facts, ancestor_facts[], isolation_set, automation_in_window,
window, render_settings, sample_rate, channels, tail_ms, reaper_version, plugin_versions}))`.
Assets are reused iff `render_key` matches, regardless of file hash.

| # | Change class | Invalidates audio | Invalidates measurements | Recompute cost | Residual risk |
|---|---|---|---|---|---|
| 1 | Track volume/pan/mute | that track's asset | all metrics on it + bus/master aggregates | 1 asset | none |
| 2 | Track FX param/order/enabled/offline | track (+ descendants) | same | 1 asset | plugin binary changes invisible |
| 3 | Bus FX/param/volume/pan | bus + every routed source | subtree + master | subtree | folder depth in digest |
| 4 | Master FX/param/volume/pan | **all 43 assets** | all | full recapture (~190 s render + ~537 s analysis reported) | plugin version not covered |
| 5 | Send gain/pan/mute/mode | source + destination | subtree | subtree | pre/post mode not in digest rows |
| 6 | Send create/delete, parent/folder | entire subtree + master | entire subtree | subtree | routing graph derived |
| 7 | Item move/length/fade/offset/playrate/stretch | that track | track + aggregates | 1 asset | digest has fields; panorama hides them |
| 8 | Item create/delete, take switch | that track | same | 1 asset | inactive takes never captured |
| 9 | MIDI note edit | that track | same | 1 asset | 10k-note limit |
| 10 | Tempo marker / tempo automation | all, iff timebase moves items | musical coordinates for all windows | full | timebase flags in digest |
| 11 | Time signature | none directly | bar/beat labels | facts only | — |
| 12 | Project sample rate / render channels / tail | all | all (different grid) | full | covered by digest settings |
| 13 | Analysis method/version/params | none | **all measurements** | analysis only (~537 s full) | version not recorded today |
| 14 | Plugin binary update | affected chain, silently | stale but undetectable | full | **not detectable from RPP** |
| 15 | Pan law / channel_count / item channel_mode | affected chain | affected | full | **not in digest** today |
| 16 | Selection/transport/cursor/marker/region | none | none | facts only | exclude from deltas |
| 17 | Human edit (any of 1–15) | per class | per class | per class | attribution `external` |

Cost anchors: the only measured full-capture cost in the repo is 190.6 s render + 536.8 s analysis
for a 300.5 s, 41-track project on the test machine. Facts-only recomputation is milliseconds; the
digest is a full-project read and must not be re-run per field.

### 6.4 Identity, validity and provenance fixes

- **Stable IDs:** `trk_/itm_/tak_/src_/fx_/snd_/env_/tm_` from REAPER GUIDs or content hashes;
  `time_map` from the tempo/signature digest, not the capture id; measurement from
  `(source, method, version, params, stage, channel, window, revision)` plus a separate `series_key`
  for before/after pairing; fail `unmatched` rather than zero-delta.
- **Capture validity:** `reaper_capture_read` must expose `capture_status`, `master_validation` and
  `invalid_captures`/`stale_assets` states. An `invalid_master` capture must never contribute
  `fresh` measurements. Coverage (counts) and validity (trust) are separate fields.
- **Coverage/uncertainty:** expected/observed/unknown, `truncated`, `not_captured`, `invalid`,
  `stale`, `incomparable`; populate `epistemic_layers.uncertainty` or state why it is empty.
- **Digest:** add `I_PANMODE`, track `channel_count`/name, item `channel_mode`/`group_id`/
  `source_channels`, send pre/post mode and FX pin/plugin state where feasible. Until then, label
  them `presented_not_fingerprinted` and treat a periodic full capture as the only trustworthy
  reconciliation.
- **Render non-determinism:** 43/43 hash differences with identical content. Never use audio hashes
  as change detection; use `render_key`. A re-rendered asset with the same `render_key` is
  `rerendered_same_content`, not a change.

---

## 7. Whole-song state versus interval evidence

The contract has two scopes in one document:

| Field | Whole-song request | `[36,45)` request |
|---|---|---|
| `state.scope` | project | **project** (unchanged) |
| `state.entities` | all tracks, items, takes, FX, routes, automation, master, time map | identical structure |
| `state.arrangement` | every item interval | every item interval + `overlap` for the window |
| `evidence.scope` | capture, full song | interval `[36,45)` |
| `evidence.sources` | all assets/metrics | only sources with content/route in the window |
| `evidence.resolution` | 30 s + 300 s (and any finer captured level) | window at full resolution; declared step-down |
| `coverage.not_in_scope` | empty | names whole-song evidence not repeated |

`panorama_slice` is an evidence accessor, not a completeness claim. It returns the whole-project
structure at full structural resolution (every entity and relationship, time map, both temporal
levels declared as available) plus window evidence at full numeric resolution with units, method,
version, stage, channel, window and provenance. It must not repeat the whole-song measurement tables;
it states `coverage.not_in_scope`. Target ≤48 KB; page if not. This keeps a local question affordable
without turning it into a disconnected project view.

Worked example `[36,45)`:

1. Resolve 36 and 45 through the revision-bound tempo map; if a tempo/signature marker falls inside,
   emit the segment list, not a single BPM.
2. Select items whose `[position, position+length)` intersects the window; emit clipped interval,
   full item span and `partial:true`.
3. For each track: `content_in_window` ∈ `{content, no_content_in_window, silent_in_window,
   not_captured}`. These are four different facts and must not collapse into “missing number”.
4. Each asset covers the window plus optional tail; the tail is a separate interval and never maps
   to project time.
5. Measurements carry `window:[36,45]`, stage, channel view, sample rate, method, version, units and
   `series`.
6. Buckets are absolute `[k·w, (k+1)·w)`, never anchored at the first sample. Today `_buckets`
   anchors at `times[0]` (`panorama.py:543-553`), so a section capture and a full capture produce
   different bucket boundaries for the same music; fix this before interval deltas are comparable.
7. Resolution comes from a global ladder and is stated; cross-width comparison is `incomparable`.

---

## 8. Verification and acceptance

### 8.1 Measuring actual client cost (not an external tokenizer)

The contract must be measured with each client's own counters, not with the Music Flamingo tokenizer:

- **Hermes:** provider `usage.prompt_tokens` / `completion_tokens`, plus cache read/write fields when
  reported; Hermes's own `last_prompt_tokens`; count `<persisted-output>` spill events and 1,500-char
  previews; record peak prompt tokens and compaction events.
- **Codex:** `token_count` events (`input_token_count`, `output_token_count`, `cached_token_count`,
  `tool_token_count`), the effective `truncation_policy`, `tool_output_token_limit`, and
  `model_auto_compact_token_limit`; detect head/tail truncation by the manifest/sentinel.
- **Generic client:** byte count and hash verification; assume truncation and page down.
- **Server:** log `response_bytes`, `page_count`, `page_hash`, `document_hash`, `revision`,
  `fact_root`, `delta_ops`, and `resync_required` per call. Correlate with client usage.
- **External MF count:** only an independent ratio/check, never the Hermes or Codex count. The
  Hermes estimator is `bytes/4` with a CJK heuristic; it is optimistic on dense JSON (60,336 vs
  128,449 external for 241,342 B).

### 8.2 Acceptance tests

**A. Full initial load.**

1. `panorama_open` returns page 0 with `document_hash`, `page_count`, `total_bytes`, section map and
   `coverage`/`states`; `delivery.complete` is false when `page_count > 1`.
2. Fetch all pages; concatenate payloads in order; hash equals `document_hash`.
3. Every domain is present; entity counts match `domain_counts`; both 30 s and 300 s levels are
   present; every analysis row has 9 metrics with units, method/version/stage/channel/window; every
   audio row has hash, stage/revision and time mapping; provenance, validity, coverage, uncertainty
   and relationships are present.
4. A missing page, a page from another revision, or a middle/tail cut fails the hash/sentinel and
   forces refresh; no partial document is accepted as whole.
5. After all pages are read, the client acknowledges the ordered page IDs/hashes and
   `document_hash`. The server mints a `complete_view_token` only if every expected page was served
   for that exact revision and the receipt matches the manifest.
6. Every mutating tool and every project-wide analysis call rejects a missing, stale or mismatched
   `complete_view_token` with `incomplete_view`; it also requires the exact `expected_revision`.
   Missing, duplicate, out-of-order, truncated and cross-revision page cases must fail closed.
7. The model guide prohibits whole-project claims before the token is issued. This gate proves that
   the page sequence was delivered through the tool flow; fresh-session evaluation separately tests
   whether the model actually uses the received context correctly.

**B. Delta.**

1. `changes_since` with the exact base returns `delta` with `from_root`/`to_root`, `ops[]`,
   `unchanged{count,hash}` and `invalidations[]`.
2. Applying `ops[]` to the base document reproduces `to_root`/`document_hash` (reference client).
3. A wrong/old/non-ancestor base returns `stale_base` or `refresh_required`; no partial patch.
4. A duplicate delta is idempotent by `chg_id`; an out-of-order delta is rejected by `from_root`.
5. No delta contains `better`/`worse`/score; a changed fact and an invalidated measurement are
   distinct.

**C. Refresh.**

1. `panorama_refresh` succeeds with no client state, at any time, and returns the current full
   document/pages.
2. It is mandatory after a lost handle, compaction, restart, new chat, identity/epoch change,
   schema mismatch, page loss and a truncated artifact; each case is tested.
3. It never returns an old revision as current; `expected_revision` mismatch is stated.

**D. Slice.**

1. `panorama_slice([36,45))` returns whole-project structure + window evidence; `state.scope` is
   project, `evidence.scope` is the window.
2. It never returns data from another revision; `coverage.not_in_scope` is explicit; it is ≤48 KB or
   pages.
3. Out-of-window audio is `not_captured` or triggers a bounded capture at the expected revision; it
   never guesses.

**E. Reduced projection rejection.**

1. Any `projection:"compact"`/reduced response carries `delivery.reduced[]`,
   `coverage.missing[]` and `complete:false`.
2. A reduced response claiming `complete:true` is a test failure, not a tolerable degradation.
3. The 300 s-only variant fails the full assertion; the 30 s level is mandatory.

**F. Cross-client.**

1. Hermes stock, Codex, and a generic tools-only stub run the same sequence; no resource,
   template or subscription is required.
2. Each page is ≤24,576 B payload + envelope; each client receives it without spill/truncation; if a
   client declares a smaller cap, pages get smaller, not lossy.
3. A client that truncates is detected by the head/tail markers and must refresh; no client-specific
   branch in the server.
4. Removing Hermes changes no server guarantee.

**G. Invalid capture and invalidation.**

1. `invalid_master` is visible on read; no fresh master measurement; `states.invalid_captures`
   non-empty.
2. A master-FX change invalidates all assets; a bus change invalidates its subtree; a track fader
   invalidates that asset and aggregates; a tempo change re-resolves musical coordinates.
3. Identical content re-captured with a moved counter yields zero project ops and
   `counter_moved_content_same:true`; 43/43 hash differences are labeled
   `rerendered_same_content`, not changes.

### 8.3 Fault injection

| Injection | Expected behaviour | Pass criterion |
|---|---|---|
| Missed notification; client at A, project at C | delta A→C if contiguous, else refresh | client root equals server root; no stale value |
| Out-of-order delta | rejected on `from_root` | projection unchanged; both roots named |
| Duplicate delta | idempotent by `chg_id` | `already_applied`, same `to_root` |
| Server/app restart | rebuild from journal | deterministic `document_hash`; `rebuild_reason:"restart"` |
| Partial capture (master silent) | fails closed at snapshot; read exposes invalid | no `fresh` master measurement |
| Master/bus routing change | all/subtree assets stale | `set_state` ops, zero measurement deltas until recapture |
| Human edit outside MCP | delta with `attribution:"external"` | no operation ID invented |
| Track deleted/recreated | old `removed`, new `added`, `unmatched` | zero “changed” ops for that pair |
| Tempo automation | tempo hash changes; musical coordinates re-resolve | windows' bar/beat differ; audio state follows timebase |
| Stale `expected_revision` | `stale_context`/no write | project digest unchanged |
| Analysis version upgrade | measurements `superseded` | no numeric delta across versions |
| Missing audio window | `not_captured`, never silence | explicit state |
| Long project/context pressure | fail closed with budget error; interval path works | no truncation, coverage explicit |
| Identical content recapture | empty project delta | 0 project ops; equal metrics within tolerance |
| Save-As/move | new project + lineage edge | history walks across the edge |

---

## 9. Tradeoffs and rejected alternatives

| Alternative | Why rejected |
|---|---|
| One full response (status quo) | 241,342 B exceeds Hermes 50K/100K and Codex ~40–48 KB; Hermes spills to a path, Codex middle-cuts; the model sees a fraction or broken JSON |
| Reduced 20,838 B spine | Drops the 30 s level (50,567 B), precision, units, uncertainty, provenance, validity, automation, per-track detail, relations; owner-rejected; a presentation subset, not a full panorama |
| Manifest + on-demand only | Violates “one unified view”; the model never has the whole; a summary plus fetch instruction |
| Resource-only / versioned URI | Hermes does not request templates and `read_resource` is a tool result that appends; Codex supports templates but no subscriptions; resources are not a portable primary surface |
| MCP resource subscriptions as the update channel | Signal-only URI; Hermes does not consume them; Codex logs them; not a delivery or replacement mechanism |
| One mode-switching tool | Union schema, conditional required fields, wrong-mode risk, and no per-tool output budget; both clients bind budgets to tool names |
| CRDT/operational transforms | Single writer (one REAPER instance); no concurrent-edit requirement; the cost is unjustified |
| YAML/TOON/textual DSL as primary | No aggregate comprehension gain; 9–14 accuracy-point cost in agent loops; parse fragility. **Not selected as a winner**; the model-facing format stays open pending Workbench comprehension tests. |
| Declaring normalized JSON (or prose, tables, or any other format) the winner before Workbench tests | No study covers DAW panorama tasks; format effects are model/task-dependent. The owner requires one coherent panorama, not a format-by-preference. | Format decision open; normalized JSON is a provisional baseline. |
| Fragmented per-domain JSON files / manually unfolded hierarchy | Violates the one-panorama requirement; forces the model to assemble meaning. | Rejected regardless of the eventual format. |
| Pure graph view | Encoding effects 4.8–61.8% and no dominant encoder; keep an edge list, not adjacency-only |
| Audio-hash invalidation | 43/43 different hashes with identical content; use `render_key` |
| Automatic better/worse or score | No defensible universal objective; the contract reports factual change, not quality |

**Tradeoffs accepted:**

- Pages cost 7–10 round-trips at open and refresh. They preserve the full document exactly and fit
  both named clients; a smaller page size costs more calls, a larger one risks truncation.
- Separate tools add a few definitions; they buy schema enforcement and per-tool budgets.
- A full refresh re-sends the document; it is bounded and explicit, and is the only safe answer when
  continuity is unprovable.
- Normalization and columnar numeric tables reduce bytes without removing facts; the remaining
  size is dominated by real numeric content, especially the 30 s temporal block. This is a
  storage/derivation result, not a model-facing format winner.

---

## 10. Cross-check A↔B, contradictions and resolutions

The two reports were produced independently; neither agent saw the other's final text. This section is
the parent's claim-by-claim reconciliation against the final files, the archive and the cited sources.
Rows that say “both” are independent agreement; rows that say “corrected” are parent resolutions.

| # | A says | B says | Resolution |
|---|---|---|---|
| 1 | Default = paged full at 24,576 B | Revision 1 default = 20,838 B spine; revision 2 rejects it | **Paged full is the default.** The spine is a rejected lossy experiment; B's final revision agrees. |
| 2 | Full document 237–241 KB; lossless lower bound 169,285 B | Spine 20,838 B | Different artifacts. The spine is not a full document; the lossless lower bound is ~169 KB and still pages. |
| 3 | 24,576 B page | 32,768 B budget in the earlier draft | **24,576 B** for the full document; 32 KiB is not used because it leaves insufficient margin against Codex. |
| 4 | Codex ≈48 KB effective (10K ×1.2) | Codex ~40 KB byte counter | Both: 10,000 tokens counted at 4 B/token = 40 KB, plus 20% allowance = ~48 KB. The contract uses the lower 40 KB counter for safety. |
| 5 | `changes_since` requires `base_revision` + `base_fact_root` | Revision alone is sufficient; handle optional | **Require revision + root** (or the exact issued `document_hash`). A revision string alone does not prove the base; handle remains optional. |
| 6 | 10 pages at 241 KB; 7 at 169 KB | 10 pages at 241 KB; 7 at 169 KB | Agree. Default is the full document (10 pages); lossless compact is 7 if used. |
| 7 | Routerai cache read 0.83 ₽; “40 providers” | Did not assert the price | Direct page says **0.33 ₽**; the 40-provider count is withdrawn. A's final report corrected it. |
| 8 | Compact projection remains an “explicit option” | Spine rejected | **No reduced projection option.** Only paged full and lossless compact encoding (still paged). A's final report corrected it. |
| 9 | Codex resource templates supported; Hermes not | Same | Agree; resources are an optional mirror, not the primary path. |
| 10 | Codex no subscriptions; Hermes no subscriptions | Same | Agree; update notifications are signal-only and not consumed. |
| 11 | 298 changed leaves | 298 changed + 3 added + 3 removed | Both consistent: 304 total differences, 298 value changes. |
| 12 | Format choice matters less than model tier | Columnar 46–51% token cut | Both: format not dominant; columnar is used for dense numeric tables only, not as a whole-document migration. |
| 13 | Summary ceiling 10,000 tokens | Config comment says ~12K | **10,000** (`_SUMMARY_TOKENS_CEILING`); the 12K comment is stale. |
| 14 | 20,838 spine drops 30 s and metadata | Confirmed by audit | Agree; rejected. |

Additional corrections applied in this synthesis:

- The archive's 43/43 audio-hash divergence is **observed on this archive**, not a universal law;
  the cause is inferred. The design conclusion (use `render_key`) holds regardless.
- `coverage.complete:true` for an `invalid_master` capture is a verified defect, not a theoretical
  risk; it must be fixed before any panorama is called complete.
- `analysis[]` rows lack method/version/stage/window/units in **both** today's full document and the
  spine; the target full document must add them. The spine is not exonerated by the full document's
  own gaps.
- The 169,285 B lossless figure is a lower bound on today's facts; adding required metadata increases
  it. It is not a promise that a future lossless compact form will fit 169 KB.

---

## 11. Staged implementation plan (next phase is owner-authorized)

**Phase 0 — contract freeze and fixtures.**

- The owner has approved page transport as one logical initial load, contingent on complete delivery
  before project edits or whole-project claims. Freeze a tested per-client page budget (24,576 B for
  measured Codex/Hermes; smaller when declared client caps require it), separate tools, and no
  reduced temporal grid.
- Specify the completion gate: page manifest and per-page hashes; server tracks pages served for the
  exact `project_id`/revision; a completion/ack operation verifies the full page set and
  `document_hash`; a complete-view token is bound to that revision; every mutating tool requires
  that token plus `expected_revision`. Missing, duplicated, out-of-order, truncated or cross-revision
  pages keep the view incomplete and writes blocked. This guarantees tool-flow delivery, not model
  attention, which is separately evaluated.
- Freeze the envelope/schema: revision, fact root, document hash, page manifest, delta ops,
  coverage/validity/uncertainty blocks.
- Build a read-only acceptance fixture from the existing archive; no new captures required.
- Stop if any target client cannot receive pages below its declared cap or if the full page set
  cannot be verified. Never substitute a reduced panorama to bypass that failure.

**Phase 1 — journal, identity and archive.**

- `src/reaper_audio/context/revision.py`: add `fact_root`, `domain_hashes`, `fact_coverage`,
  `uncovered_presented_fields`, parent chain.
- `src/reaper_audio/context/full_capture.py`: append-only `rev_`/`chg_` records, handle registry,
  idempotency, retention.
- `src/reaper_audio/context/ids.py`: stable IDs; `time_map` hash; measurement/series split.
- Fix `takes.track_id`, FX pointers, `time_map_id`.
- Acceptance: I2 replay, I4 reconciliation, no change_count-as-delta.

**Phase 2 — full document and pages.**

- `src/reaper_audio/context/panorama.py`: serialize the model-facing representation selected by the
  §5 comprehension test; the provisional baseline is normalized JSON entities/edges/tables/states/
  refs. Both temporal levels; units/method/version/stage/channel/window; coverage/validity/
  uncertainty.
- Add deterministic page plan, page hashes, manifest, `document_hash`.
- `src/reaper_audio/mcp_server.py`: `panorama_open`, `panorama_page`, `panorama_refresh`,
  `panorama_status`.
- Acceptance: full open reassembles; 10 pages at 241 KB; 30 s block present; no partial edit.

**Phase 3 — deltas and resync.**

- `panorama_changes_since`: exact base, ops, unchanged counts, invalidations, `stale_base`/
  `refresh_required`, `chg_id` idempotency.
- `panorama_refresh` and `panorama_slice`; `client_root`/document hash verification.
- Acceptance: delta replay to target root; wrong base refused; refresh from no state; slice contract.

**Phase 4 — invalidation and digest correctness.**

- `render_key` in `src/reaper_audio/daw/full_capture.py`; dependency table; stale/invalid states.
- Extend digest: pan law, channel_count/name, item channel_mode/group/source_channels, send mode,
  plugin state where feasible.
- Fix `reaper_capture_read` validity propagation.
- Acceptance: master/bus/track invalidation cases; invalid capture never fresh; hash divergence
  does not invalidate.

**Phase 5 — cross-client acceptance.**

- Acceptance run: stock Hermes, Codex, generic tools-only stub; same project sequence.
- Measure actual counters, page receipts, spill/truncation, resync, answer fidelity.
- Acceptance §8.2 A–G; stop if any client cannot receive a 24 KB page or if a lossy substitute is
  proposed as full.

**Phase 6 — owner listening/trial.**

- Disposable copy of the project; no automatic quality verdict; owner feedback separate from measured
  facts.
- Stop before any live edit path if revision/hash binding is not proven.

**Stop conditions for the whole plan:**

- A semantics-preserving full panorama cannot be paged/reassembled under a client's cap → fix the
  serializer/page plan; never send a reduced subset as full.
- Handle/revision identity cannot survive reload/epoch change → design lineage or require refresh;
  do not guess.
- Invalidation cannot identify affected evidence → mark `stale`/`unknown`; never return fresh.
- Page delivery cannot be verified or edits can bypass the complete-view gate → stop and fix the
  contract; never weaken the full-context requirement.
- The clean-session evaluation cannot demonstrate complete context receipt, safe revision-bound
  edits, and enough tool discoverability for an arbitrary task → fix MCP/README and repeat.
- No live user-project write until revision/hash binding and isolated-copy acceptance are proven.

---

## 12. Owner decision and technical questions

**O1 resolved — page sequence accepted as one logical initial panorama.** The owner accepts
deterministic multi-call delivery provided every page is fetched and verified before project edits
or whole-project claims. A literal single response cannot carry the full lossless panorama on the
measured clients; the reduced one-response projection remains rejected. The completion-gate
mechanism is a technical design requirement, not an open owner choice.

| # | Technical question | Why it matters | Current position |
|---|---|---|---|
| T1 | Exact production size after adding metadata | 169 KB is a lower bound; may be larger | Re-measure on the real serializer |
| T2 | Can a client library recompute `fact_root` from `ops[]`? | Delta verification at the content level | Contract publishes JCS/Merkle; reference client test |
| T3 | Does Codex deliver 24 KB pages without truncation on this install? | 10K token policy counted at 4 B/token | Expected yes (~6K); test |
| T4 | Hermes spill behavior for a 24 KB page | 50K threshold | Expected no spill; test |
| T5 | GUID reuse after delete/recreate | Delta identity | Assume no; test |
| T6 | Render non-determinism cause | Cache/invalidation policy | Observed; cause inferred |
| T7 | Does the model actually fetch all pages? | Whole-project claim | Contract rule + acceptance measurement; server cannot prove |
| T8 | Attention/fidelity at 10 pages | Model behaviour | Blind question set; no guarantee |
| T9 | Page size and maximum calls at open | 24 KB is safe; 8 KB gives 30 pages | 24,576 B recommended; caller may declare smaller |
| T10 | Retention of captures, journals and page manifests | Archive growth and audit | Existing indefinite archive; needs policy |
| T11 | Future mixer model/provider/tokenizer | Actual token cost is model-specific | Not proven; measure with provider usage |
| T12 | Which model-facing format wins on Workbench comprehension tasks? | Owner requires one natural, cross-linked panorama; no winner without task evidence | Normalized JSON is provisional; prose/structured-text and hybrid forms are candidates; run the blind test in §5 |

---

## 13. Acceptance checklist (one page)

1. `panorama_open` returns a complete manifest and page 0; `page_count` is correct; `document_hash`
   is present.
2. All pages fetched; concatenation reproduces `document_hash`; a missing/foreign page fails.
3. Full document contains every domain and entity; both 30 s and 300 s levels; all metrics with
   units/method/version/stage/channel/window; provenance; validity; coverage; uncertainty;
   relationships.
4. No edit before all pages and hash; writes carry `expected_revision` and exact `document_hash`.
5. `changes_since` accepts only an exact base; returns `delta` or `stale_base`/`refresh_required`;
   never a partial patch.
6. Delta `ops[]` applied to the base reproduces `to_root`; `unchanged` counts reconcile; no
   better/worse.
7. `panorama_refresh` works with no state, at any time, after every mandatory-refresh condition.
8. `panorama_slice([36,45))` returns whole-project structure + window evidence; `state.scope=project`,
   `evidence.scope=window`; out-of-window is explicit; ≤48 KB or pages.
9. A reduced/300 s-only projection never claims `complete:true`; the full assertion fails it.
10. `invalid_master` is visible on read; no fresh measurement.
11. Identical content recapture produces zero project ops; hash differences are
    `rerendered_same_content`.
12. Cross-client: Hermes stock, Codex, generic tools-only; no resources/subscriptions required; no
    client-specific branch; removing Hermes changes no guarantee.
13. Actual token/cost measurements use provider/client usage, not the external tokenizer.
14. No client-specific integration is required in the path.
15. Model-facing format is chosen by the blind Workbench comprehension test in §5; until then,
    normalized JSON is a provisional baseline and no format is called the winner.

---

## 14. Bottom line

The library can hold one authoritative, current, whole-project panorama server-side and can prove
what the client holds. It cannot make a stock client accept a lossless 169–241 KB document in one
result, and it cannot make the model attend to pages it never fetched. The portable answer is:

- **one logical full panorama**, delivered as **checked, deterministic 24 KB pages**;
- **exact revision/root deltas** after the base, with no silent patch;
- **explicit full refresh** as a normal operation, mandatory when continuity cannot be proven;
- **one contract** for Codex, Hermes and any tools-only client;
- **no reduced temporal grid** and no lossy subset labeled full;
- **internal JSON is storage/transport only**; the model-facing format remains open until the
  Workbench comprehension test, with normalized JSON as a provisional baseline;
- **no client-specific integration requirement** in the architecture.

That is the research recommendation. The owner has resolved O1 in favor of a complete verified page
sequence and explicitly authorized the follow-on implementation plus fresh-session evaluation. The
implementation directive captures the broader MCP coverage and README requirements; no further
permission is needed for that authorized work.

### Implementation status at 2026-11-13

The page/receipt lifecycle, exact-base delta, refresh, slice, mutation gate and readback validity
are implemented and exercised on a disposable project. The original 21-tool/732-signature discovery
surface is unchanged; eight lifecycle tools were added, including delta
acknowledgement that advances a context token without replaying pages. A clean target-model session
(`deepseek/deepseek-v4.1-flash-1017`, Ultra, RouterAI) fetched/acked all pages, made a gated fader
edit, read it back, recaptured and measured a +0.83 dB render change consistent with the fader
ratio. See `CLEAN-SESSION-EVAL-LOG.md` for measured page sizes, token usage and the exact
pass/open list. It is not a release claim: full domain/API call coverage, Codex live delivery, the
10-page target-model budget, large-project capture and owner listening remain open.
