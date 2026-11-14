# Round 7 — requirement archaeology of the LLM-facing MCP interface

**Scope:** the model-facing project-context contract of `reaper-audio-library`
(`reaper_audio.context.*`, `reaper_audio.mcp_server`) and the documents that
produced it.
**Date of audit:** 2026-11-14 (UTC+03:00). **Mode:** read-only. No production
file was modified; the only repository write is this file. No test suite was
run, no REAPER session was opened, no render was made.
**Status of this document:** evidence record, not an instruction. It classifies
what each older document *is*; it does not adopt anything from it.

---

## 0. How status and dates were established (read this before quoting anything)

The paged-panorama rounds were never committed: `git log` shows only the
pre-research history (HEAD `86b14bb`), and every file under `docs/research/` plus
the DECISION record is untracked. Chronology therefore rests on (a) date lines
inside documents, (b) file mtimes, and (c) explicit "supersedes" / "historical
record" banners. Where only (b) is available, the date is marked **inferred**.

| File | Stated date | mtime (full-iso) | Authority note |
|---|---|---|---|
| `docs/DECISION-single-response-panorama.md` | none in file | 2026-11-14 05:18:47 | **newest authority**; date inferred from mtime and task context |
| `docs/MCP-README-LLM.ru.md` | none | 2026-11-14 05:14:30 | model guide, RU |
| `docs/VALIDATION.md` | none | 2026-11-14 05:12:40 | validation record |
| `docs/MCP-README-LLM.md` | none | 2026-11-14 05:08:01 | model guide, EN |
| `docs/FULL-CAPTURE.md` | none | 2026-11-14 04:59:36 | capture + view contract |
| `docs/ARCHITECTURE.md` | none | 2026-11-14 04:56:04 | architecture |
| `docs/ARCHITECTURE.ru.md` | none | 2026-11-14 01:23:42 | older RU architecture |
| `docs/STATUS.ru.md` | "Дата: 2026-11-11" | 2026-11-12 22:48:56 | live-check status |
| `docs/AI-INTERFACE.ru.md` | none | 2026-11-12 22:48:56 | early interface design |
| `docs/TZ.md`, `docs/AI-MIX-SURFACE.md`, `docs/EDITING-ACCEPTANCE.md` | none | 2026-11-12 14:11 | product spec / interface project |
| `docs/research/00..06` | none | 2026-11-12 16:04 | round-1 briefs |
| `docs/research/round-2/*` | none | 2026-11-12 17:18 | round-2 briefs |
| `docs/research/round-3/DECISION-LEDGER.md` | none; Wave records dated 2026-11-12 | 2026-11-12 23:07 | design record |
| `docs/research/round-4/MCP-CONTEXT-RELEASE-GATE.md` | none; addendum dated 2026-11-13 | 2026-11-13 18:43 | gate + owner clarification |
| `docs/research/round-4/CONTEXT-LEDGER-DESIGN.md` | none; addendum dated 2026-11-13 | 2026-11-14 05:00 (bulk) | design, superseded in part by its own addendum |
| `docs/research/round-6/*` | "Date: 2026-11-13" | 2026-11-14 05:00 (bulk) | research + owner directive |
| `docs/research/round-7/INTEGRATED-DECISIONS.md` | "Date: 2026-11-13" | 2026-11-14 05:00 | banner: historical, superseded transport |
| `docs/research/round-7/WORKSTREAM-A/B-AUDIT.md` | "Date: 2026-11-13" | 2026-11-13 22:59 | code audit at that moment |
| `docs/research/round-7/SEARCH-LOG.md` | "2026-11-13" | 2026-11-13 23:13 | source-access log |

**Timestamp anomaly, stated explicitly.** The round-4/6/7 files share one bulk
mtime (2026-11-14 05:00) that is *earlier* than the DECISION file (05:18), yet
several of them carry banners that refer to the DECISION as already existing.
Either those files were re-written with a preserved timestamp, or the tree was
copied after editing. Consequence: mtime cannot order the banners against the
DECISION; the banner text is the only reliable statement of order, and it says
the DECISION is newer.

**Date-inference rule used below.** A statement is dated by its own header when
one exists; otherwise the mtime is given and labelled *(mtime)*. The owner
requirements in §2 are dated by the document that records them, which is the
earliest date at which that requirement can be shown to exist — not necessarily
the date the owner first said it.

---

## 1. Chronological ledger of the LLM-interface design

Legend: **ADOPTED** = current contract; **SUPERSEDED** = was authoritative, a
named newer document replaced it; **REJECTED** = considered and refused, and the
refusal still binds; **UNVERIFIED** = asserted but not measurable from the repo.

| # | Date | Document / section | Decision | Status | One-line reason |
|---|---|---|---|---|---|
| 1 | 2026-11-12 *(mtime)* | `docs/TZ.md` §8 (line 61) | Compact capability map + structured search + broad ReaScript access; describe current MCP and future interface separately | ADOPTED (partly unimplemented) | Least-loaded discovery without hiding capability; the "describe separately" clause is not honoured by the current guide |
| 2 | 2026-11-12 *(mtime)* | `docs/AI-MIX-SURFACE.md` §6 (148–165) | A 100 ms per-source matrix; "в контекст — только роллы, дельты и адреса" ("only rolls, deltas and addresses into context"); JSON would choke | **SUPERSEDED** by round-4 owner addendum (2026-11-13) and DECISION (2026-11-14) | The owner later required every measured value and both temporal levels in the model view; no npz matrix was ever built (the archive is JSON) |
| 3 | 2026-11-12 *(mtime)* | `docs/AI-INTERFACE.ru.md` §"Детали для экономии контекста" (258–277) | Compact summary by default + a detail mode; fixed `tools/list`; retries only on explicit request | **PARTLY SUPERSEDED** | "Detail on demand" survives as the semantic query; "compact summary by default" is reversed |
| 4 | 2026-11-12 | `docs/research/round-2/R2-05` §"Решить конфликты" (line 75) | "unified report is a navigation contract, not a giant inline blob" | **REVERSED** later | True for round 2's budget assumptions; falsified by the 42,864 B columnar projection |
| 5 | 2026-11-12 | `round-3/DECISION-LEDGER.md` D16 (line 35) | "The full analysis is a navigation contract, not an inline blob: manifest plus addressable views, raw curves on demand" | **SUPERSEDED** by DECISION §2 | The manifest-plus-views split was the direct ancestor of the paged contract |
| 6 | 2026-11-12 | `round-3/DECISION-LEDGER.md` D22 (line 41) | 19 tools / 6 resource endpoints; `tests/test_mcp_server.py:110` asserts 19 | **SUPERSEDED** (29 tools by round 6, 26 now) | The surface grew with capture, panorama and dialect tools |
| 7 | 2026-11-12 | `round-3/IMPLEMENTATION-BLUEPRINT.md` (line 274) | Manifest ≤1500 tokens on the 33-track fixture, 10 cursor-paged views, omission ledger | **REJECTED / never built** | Cursor views and a truncating brief are the design the owner later forbade |
| 8 | 2026-11-13 | `round-4/MCP-CONTEXT-RELEASE-GATE.md` §"Owner clarification" (38–65) | One unified panorama is the only model-facing state representation; raw records are internal | **ADOPTED** (active) | Removes the "which JSON is the truth" ambiguity |
| 9 | 2026-11-13 | `round-4/CONTEXT-LEDGER-DESIGN.md` §5.1–5.2 (413–449) | Tier-0 brief capped at 12 track rows; Tier-1 views paginated with opaque cursors | **SUPERSEDED by the addendum in the same file** (line 791) | A truncated brief cannot satisfy "nothing silently dropped" |
| 10 | 2026-11-13 | `round-4/CONTEXT-LEDGER-DESIGN.md` §"Owner addendum" (791–819, 821–836) | Panorama is the only model-facing representation; the journal records every available metric and temporal series, "not only flagged values or a selected subset" | **ADOPTED** (active) | This is the sentence that kills every "top-N issues" summary |
| 11 | 2026-11-13 | `round-6/PANORAMA-CONTINUITY-RESEARCH-DISPATCH.md` §104–116 | Portable MCP tools-only contract; Hermes ContextEngine/plugin/hook explicitly out of scope | **ADOPTED** (active) | A client-specific harness cannot be the product contract |
| 12 | 2026-11-13 | same file §120–131 | Full open, `changes_since` from an exact base, explicit refresh; never silently patch | **ADOPTED** (active) | The three-operation lifecycle that still exists |
| 13 | 2026-11-13 | same file §138–140 | Reject a reduced panorama; preserve both temporal levels; "If the complete lossless document cannot fit one stock MCP response, research deterministic page transport" | **Content rule ADOPTED; page clause SUPERSEDED** | The conditional was resolved differently on 2026-11-14: make it fit instead of paging it |
| 14 | 2026-11-13 | `round-6/SYNTHESIS-AND-DECISIONS.md` D6-03 (line 39) | "The owner accepts a multi-call initial load as transport … default `page_bytes = 24,576`" | **SUPERSEDED** by DECISION §1–§2 | Accepted as transport, later rejected as architecture |
| 15 | 2026-11-13 | same, D6-04 (line 40) | The 20,838 B "spine" is a rejected lossy experiment, not a candidate or fallback | **ADOPTED** (re-affirmed in DECISION §7) | It dropped the 30 s level (50,567 B alone) and measurement metadata |
| 16 | 2026-11-13 | same, D6-05 (line 41) | Both 30 s and 300 s levels, plus any finer captured level, are mandatory | **ADOPTED** (active) | A 300 s-only grid is not a full panorama |
| 17 | 2026-11-13 | same, D6-07/D6-08 (43–44) | Exact-base delta; refresh is a normal, always-available operation | **ADOPTED** (active) | No silent patch; recovery is not a hidden path |
| 18 | 2026-11-13 | same, D6-09 (line 45) | Separate explicit tools rather than one mode-switching tool | **ADOPTED** (5 panorama tools today) | Schema enforcement and per-tool client policy |
| 19 | 2026-11-13 | same, D6-10b (line 47) | "The model-facing representation is unresolved"; no format wins without task-based comprehension checks | **UNRESOLVED / overtaken** | The DECISION adopts columnar tables without ever running that test (§6 admits it) |
| 20 | 2026-11-13 | `round-6/DELTA-CONTINUITY-IMPLEMENTATION-ADDENDUM.md` (14–46) | Delta acknowledgement is mandatory before the token advances; ack is idempotent | **SUPERSEDED** (auto-advance in the same response) | The ack added a round trip without adding a guarantee |
| 21 | 2026-11-13 | `round-6/IMPLEMENTATION-DIRECTIVE.md` §2.5, §3A (26, 39–42) | "Required paged load": fetch every page, verify hashes, page-completion gate | **SUPERSEDED** by DECISION §1, §7 | The directive codified the transport as a model ritual |
| 22 | 2026-11-13 | `round-6/CLEAN-SESSION-EVAL-LOG.md` (61–74, 138–141, 158–160) | A 3-page load + one edit measured **193,078** cumulative tokens; a longer session **967,040** | **EVIDENCE, not a decision** | This is the measurement that made paging untenable on cost, not only on discipline |
| 23 | 2026-11-13 | `round-7/INTEGRATED-DECISIONS.md` §9.6 (922–975) | "Client/model must fetch every deterministic page … before it interprets"; calls itself "an owner rule" | **SUPERSEDED** (page walk); the interpretation gate survives | The gate is real; the page walk is not |
| 24 | 2026-11-14 *(mtime)* | `docs/DECISION-single-response-panorama.md` §2–§5 | One call returns one compact whole-project view + receipt + token; over budget = refuse and mint nothing; exact rows via one semantic query | **ADOPTED** (current) | One response is the only path a stock client can follow without a loop |
| 25 | 2026-11-14 | same, §7 (189–196) | Rejected: keep pages; one 1.33 MB raw response; 200 KB budget; lossy spine; page tooling kept but not mandatory; client reassembly / resources preload; cursor detail; dropping relation edges | **REJECTED** (binding) | Each rejection has a named failure mode; the list is the negative specification |
| 26 | 2026-11-14 | same, §8 (200–216) | View carries 24 metric columns; `page/ack/detail/slice/delta_ack` removed from the model surface; page assembly and `delta_ack` remain internal | **ADOPTED; verified in code** (§4 below) | The model surface and the lifecycle stopped being the same thing |

### 1.1 The five turning points, in plain terms

1. **"A report is a navigation contract" (2026-11-12)** was a budget assumption,
   not a measured fact. It produced D16, then the manifest-plus-views ledger,
   then the paged panorama.
2. **The owner's "one panorama, no raw fragments" clarification (2026-11-13)**
   removed the alternative state views but left the transport question open.
3. **The spine measurement (20,838 B)** proved that shrinking the *content* was
   the only way to make one response fit, and the owner refused that trade.
   That left paging as the only lossless option — under the assumption that the
   canonical document had to be sent.
4. **The clean-session log (193 K / 967 K tokens)** showed the cost was round
   trips, not bytes, and the receipt proved delivery rather than reading.
5. **The projection (2026-11-14)** broke the assumption: 1,349,027 B canonical →
   42,864 B view. One response became possible, so paging lost its only
   justification.

---

## 2. Owner-requirement table

"Owner" here means the person commissioning the work, as recorded by a document.
Requirements that appear only in agent-authored prose are excluded; requirements
that appear only in the commissioning context are noted separately in §2.1.
Quotes are verbatim, trimmed with `…`.

| # | Requirement | Verbatim quote (short) | Doc + section | Date | Status | Current code location | Gap |
|---|---|---|---|---|---|---|---|
| O1 | Portable tools-only MCP; no harness-specific mechanism | "must work through ordinary MCP tools in Codex, Hermes and other conforming tools-only clients. No Hermes Context Engine, plugin, private API, transcript hook…" | `IMPLEMENTATION-DIRECTIVE.md` §2.1 (line 22) | 2026-11-13 | active | no context-engine import anywhere; `mcp_server.py` uses `MCPServer` only | none found |
| O2 | One model-facing state representation | "Panorama is the only model-facing representation of REAPER project and audio state." | `CONTEXT-LEDGER-DESIGN.md` §Owner addendum (799) | 2026-11-13 | active | `panorama_view.build_view`; `mcp_server.py:2348` | `reaper_project` (801–857) and `reaper_capture_read` (2324) still return project/archive JSON — allowed as bootstrap/archive reads, but they *are* alternate reads |
| O3 | No lossy or reduced panorama; keep both temporal levels | "The owner rejects a reduced panorama that replaces the existing 30-second temporal level with a coarser 300-second level…" | `PANORAMA-CONTINUITY-RESEARCH-DISPATCH.md` §138–140 | 2026-11-13 | active | `panorama_view.py:579–662` (`level_widths=(300.0, 30.0)`) | on a 20 s capture both levels exist but have 1 bucket; the stored grid is capped at 24 buckets (`panorama.py:62`), so on a 300 s extent the "30 s level" is built from ≈12.5 s cells (arithmetic), not 400 ms. Declared in `temporal.stored.cell_meaning`, not hidden |
| O4 | Whole project in one response, or nothing | "either the whole view arrives or nothing does" | `MCP-README-LLM.md` §379–392; DECISION §2 | 2026-11-14 | active | `lifecycle.single` (1067–1182) | none; verified (§4.1) |
| O5 | No mandatory page walk / client loop | "The user rejected the mandatory page walk as the architecture: it requires model discipline and client loops…" | DECISION §7 (line 189) | 2026-11-14 *(mtime)* | active | no page tool registered (`mcp_server.py` tool list) | `lifecycle.open` still writes `next_call.tool = "reaper_panorama_page"` (891–895) — unreachable, but the string exists |
| O6 | Exact unrounded values must remain reachable | "A model that needs exact cells must query; the view says so and names the selector." | DECISION §6 (177–179) | 2026-11-14 | active | `lifecycle.query` (1184–1238); `details.py` | 20,000-point cap (`details.py:35`) can force several queries; each is whole, none is paged |
| O7 | Receipt = delivery, not comprehension | "**Delivery, not comprehension.** … The server cannot observe whether the host kept the bytes or the model read them" | DECISION §5 (122–125) | 2026-11-14 | active | `receipt_guarantee` string (1114–1115); `MCP-README` §446–449 | none |
| O8 | Delta advances without a second round trip | "no acknowledgement round trip" | DECISION §3 (101); §5 (151–154) | 2026-11-14 | active | `record_change` auto-calls `delta_ack` (1630–1652) | `delta_ack` remains public on the lifecycle, not on MCP |
| O9 | Audio freshness is separate from structure | "A delta whose `audio_evidence.state` is `stale` advances structural edits only" | DECISION §5 (155–157) | 2026-11-14 | active | `require_receipt(require_audio_fresh=True)` (2249+), `_require_audio_fresh` (2229) | none |
| O10 | No invented meaning, no genre recipe | "The view adds no role, genre, quality verdict or cause" | DECISION §5 (137–141); `TZ.md` §1 | 2026-11-14 | active | `_reading` (847–887) | the view says interpretations are "empty"; the canonical document carries the explicit empty keys (`panorama.py:2382–2387`) — the view does not carry those key names |
| O11 | Keep units, method, stage, window, validity, provenance | "Keep units, methods/versions, processing stage, channel view, exact windows, validity, coverage, uncertainty and source links." | `IMPLEMENTATION-DIRECTIVE.md` §2.4 (25) | 2026-11-13 | active | `analysis.metric_units`, `analysis.stages`, `analysis.windows`, `analysis.method` (553–576) | **method/version is document-level only**; there is no per-row method column, so two sources measured by different methods cannot be told apart from the view |
| O12 | Mandatory page fetch before interpretation | "The model must fetch every page and verify page/document hashes before making whole-project claims or mutating" | `IMPLEMENTATION-DIRECTIVE.md` §2.5 (26) | 2026-11-13 | **superseded** by DECISION §1/§7 | page tools deleted | historical only |
| O13 | Bilingual model guide, English source of truth | "a complete model operating guide in English and Russian, with English as the source of truth" | `IMPLEMENTATION-DIRECTIVE.md` §3B (51–54) | 2026-11-13 | active | both files exist | RU is 768 lines vs EN 707 and contains normative text absent from EN (§4.7) |
| O14 | Minimise the number of agent actions | "Свести число действий агента к необходимому минимуму." | `TZ.md` §3 (line 27) | 2026-11-12 *(mtime)* | active | one `panorama_open` replaces open+page×N+ack | over-budget forces a different task decomposition, not a smaller call |

### 2.1 Requirements that exist only in the task context, not in any document

Stated for honesty: the commissioning context says the owner wants **no
screenshots** and **no manual steps**. No document under `docs/` states either as
a requirement. `TZ.md` §2 (line 15) in fact says the opposite about evidence —
"Скриншоты пользователя и форма волны годятся как доказательства и ориентиры"
("the user's screenshots and the waveform are valid as evidence and landmarks").
The "no screenshots" constraint is therefore recorded here as **task-context
only, unverified against the repository**, and it does not appear to have been
implemented as a rule anywhere.

---

## 3. Contradictions between documents

For each: A says X, B says Y, which is newer/authoritative, and the evidence.

### C1. Mandatory page walk vs one response — the central contradiction
* **A:** `round-7/INTEGRATED-DECISIONS.md` §9.6 (927–931): "the client/model must
  fetch **every deterministic page** … before it interprets project audio …
  or before it edits", labelled "an owner rule".
* **B:** `DECISION-single-response-panorama.md` §7 (189): "The user rejected the
  mandatory page walk as the architecture".
* **Which is authoritative:** B. It is the newest file (mtime 05:18 vs the
  round-7 bulk 05:00), it declares "Status: adopted (supersedes the paged-delivery
  decision)", and A now opens with "Historical record (superseded transport) …
  Treat this file as history, not instruction."
* **Evidence the "owner rule" label in A is wrong:** `SYNTHESIS-AND-DECISIONS.md`
  §1.4 (117–125) records only that the owner "accepted deterministic multi-call
  page delivery **as the transport** for one complete logical panorama" — a
  transport concession, not a process requirement. A converted a transport
  acceptance into a mandatory model ritual.

### C2. Delta acknowledgement required vs automatic
* **A:** `docs/ARCHITECTURE.md` §"Deltas and evidence freshness" (67–69):
  "Acknowledging the exact delta advances the context token". `ARCHITECTURE.ru.md:60`
  repeats it.
* **B:** DECISION §5 (151–154): "The lifecycle still implements `delta_ack`
  internally for archive compatibility, but no model step requires it."
* **Which is authoritative:** B, plus the code — `record_change` calls
  `delta_ack` itself and returns `advance.context_token` (1630–1652).
* **Evidence:** `tests/test_identity_and_delta.py:686–698` asserts the successor
  arrives in the same response; no MCP tool exposes `delta_ack`.

### C3. The view's schema name
* **A:** `docs/FULL-CAPTURE.md` (185–186): "The `panorama.json` sidecar and the
  compact view use the same schema (`reaper-workbench.capture-panorama/2`)."
* **B:** `panorama_view.py:46`: `VIEW_SCHEMA = "reaper-workbench.panorama-view/2"`;
  confirmed in a live in-process open (`view.schema_version` = `…panorama-view/2`).
* **Which is authoritative:** the code, and DECISION §2 (65) which names
  `reaper-workbench.panorama-view/2` explicitly.
* **Consequence:** a reader who validates the view against `capture-panorama/2`
  will look for fields (`capture_binding`, `analysis_temporal`) that the view
  deliberately renames (`binding`, `temporal`).

### C4. "No model-facing string names a page"
* **A:** DECISION §9 (227): "no model-facing string names a page".
* **B:** `mcp_server.py:679` docstring: "mode=domain pages through one domain";
  `docs/MCP-README-LLM.md:119`: "page through the 732 documented API functions".
* **Which is authoritative:** the claim is false as literally worded; it is true
  for panorama strings only. Both survivors concern the API-catalogue browser,
  not the panorama, so the intent holds but the sentence overreaches.

### C5. Tool count in the same file
* **A:** `MCP-README-LLM.md:3` (and `.ru:4`): "26 tools and six resource templates".
* **B:** The "Tool map" table immediately below (`MCP-README-LLM.md:116–136`)
  lists **19** tools; the seven listener/spatial/timeline tools are missing.
* **Which is authoritative:** the code — 26 `@server.tool()` and 6
  `@server.resource(...)` entries. The table is incomplete, not the header wrong.

### C6. Truncation by design vs "nothing silently dropped"
* **A:** `CONTEXT-LEDGER-DESIGN.md` §5.1 (413–421): Tier-0 carries "master + bus
  topology (not all 24 track rows when over budget)" and "a cap (e.g. 12 rows)".
* **B:** The owner addendum in the *same file* (791–819) and DECISION §5 (132–136):
  every canonical section is inventoried; nothing disappears.
* **Which is authoritative:** B, which explicitly "supersedes any earlier proposal
  in this document".

### C7. "Only rolls, deltas and addresses into context"
* **A:** `docs/AI-MIX-SURFACE.md` §6 (164–165): "в контекст — только роллы,
  дельты и адреса" ("into context, only rolls, deltas and addresses").
* **B:** DECISION §2 and §6: the whole compact view, all 24 metric columns, both
  temporal levels, inline.
* **Which is authoritative:** B. **Hazard:** `AI-MIX-SURFACE.md` has no status
  line and no superseded banner, so it still reads as a live specification while
  its most concrete context rule is dead.

### C8. Format declared "unresolved" vs columnar tables adopted
* **A:** `SYNTHESIS-AND-DECISIONS.md` D6-10b (47): "The model-facing
  representation is unresolved … No format is declared the winner without
  task-based comprehension checks."
* **B:** DECISION §2 (63–65) fixes the columnar table projection
  (`panorama-view/2`).
* **Which is authoritative:** B by date. **But** B never reports the comprehension
  test A demanded; §6 (180–183) lists live delivery and token counts as
  "not verified". The format was therefore adopted by engineering judgement after
  the byte measurement, not by the evaluation A specified. That is a legitimate
  decision, but it is not the one A's rule required.

### C9. Metric count 9 vs 24
* **A:** `WORKSTREAM-A-AUDIT.md` (row R6-3): "`panorama.py:39-45` still limits
  compact analysis to 9 metric keys."
* **B:** `panorama.py:63–75` now declares 24 keys, with the comment that the other
  15 "were measured but never reached the document".
* **Which is authoritative:** B (code today). A was correct on 2026-11-13. The
  change is the one DECISION §8 describes.

### C10. Tool-count history is a stale-doc cluster, not a contradiction
19 (`DECISION-LEDGER` D22; `GAP-ANALYSIS.md:21`; `STATUS.ru.md:44`) → 29
(`WORKSTREAM-A-AUDIT` R6-6; `INTEGRATED-DECISIONS.md:84`) → 26 (`MCP-README`,
`ARCHITECTURE.md:40`, code). Only the code and the 2026-11-14 docs are current;
`STATUS.ru.md` (dated 2026-11-11) and `GAP-ANALYSIS.md` should not be cited for
the surface.

### C11. "Working implementation" vs "not a release claim"
* **A:** `MCP-README-LLM.ru.md:3`: "**Статус: рабочая реализация.**"
  ("Status: a working implementation").
* **B:** `MCP-README-LLM.md:3` and 19–24: descriptive status ending with "this is
  not a release claim"; `VALIDATION.md` §Release gate (39–41) forbids calling it
  production-ready.
* **Which is authoritative:** B for release status. The RU line is a stronger
  claim than the evidence supports and than the EN guide makes.

### C12. "Every project-target analysis requires a receipt for that exact capture"
* **A:** `MCP-README-LLM.md` (398–400): capture read, panorama query "and every
  project-target analysis require a receipt for that exact capture".
* **B:** `mcp_server.py:953–956` gates `reaper_analyze` through
  `_require_interpretation_receipt`, which validates project, revision, fact root
  and audio freshness but does **not** pass `expected_capture_id`; only
  `_require_capture_read_receipt` (133–160) does.
* **Which is authoritative:** the code. The exact-capture rule is enforced for
  `reaper_capture_read` and `reaper_panorama_query`, not for analysis tools,
  which re-measure the live project anyway.

---

## 4. Claims in documents that the code does not support

Only claims checked against the actual source are listed. "Verified" means
read in the code and, where a number is involved, reproduced in-process against
the archived 41-track capture (§4.1).

### 4.1 What the code does support (checked, so the negative list is credible)

Read-only, in-process, against
`work/benchmark/himmp/capture-archive/captures/cap_01M79659V69NDC5YNAA0SMPX51`
(no REAPER, no tests):

| Claim (source) | Result |
|---|---|
| Canonical document 1,349,027 B (DECISION §1, VALIDATION:11) | **1,349,027 B** exactly |
| Compact view 42,864 B, envelope 44,719 B, `fits: true`, `pages: false` (DECISION §6, VALIDATION:12–14) | **42,864 / 44,719**, `status: complete`, `fits True`, `pages False` |
| 24 metric columns (DECISION §8, README:413) | **24** |
| 42 track rows incl. master (DECISION §6) | **42 rows**, 41 tracks + master |
| Both temporal levels declared (D6-05) | levels `[(300.0, available, 1 bucket), (30.0, available, 1 bucket)]`; stored grid width 0.8167 s, 25 buckets, 42 sources |
| All sections placed, none unmapped (DECISION §5) | `unmapped_sections: []`, 27 view sections |
| Over budget mints nothing (DECISION §2.5) | at 10,000 B: `status: response_over_budget`, no `view`, no `receipt`, no `context_token`, no `handle`, per-section bytes, three narrowing options |
| Relation kinds carry `meaning`/`is_not`/method/uncertainty (DECISION §5) | `band_dominance`, `track_master_level_relation`, each with those keys (`panorama_view.py:690–702`) |
| Old sidecars are projected in memory (DECISION §4) | sidecar on disk **291,445 B** rebuilds to **1,349,027 B**; file untouched (mtime 2026-11-13 06:31) |
| `document_hash` is the sealed `document_sha256` (DECISION §9) | `lifecycle.py:330–341` returns the stored value, falls back to a hash only when absent |

### 4.2 Claims not supported

**K1 — View schema.** `FULL-CAPTURE.md:185–186` says the view uses
`reaper-workbench.capture-panorama/2`. Code: `panorama-view/2`
(`panorama_view.py:46`). See C3.

**K2 — "No model-facing string names a page."** `DECISION §9:227`. Counter-example:
`mcp_server.py:679`. See C4.

**K3 — `model_interpretations` / `causal_hypotheses` are empty "in the view".**
`MCP-README-LLM.md:318–320` names these keys as part of the view contract. They
exist only in the canonical document (`panorama.py:2382–2387`); the view exposes
`reading.epistemic.interpretations` / `.hypotheses` as prose
(`panorama_view.py:868–878`). A model searching the view for the quoted key
names will not find them.

**K4 — Exact-capture receipt for every analysis.** `MCP-README-LLM.md:398–400`.
Code gates analysis on project/revision/fact-root/audio-freshness, not capture id
(`mcp_server.py:953–956` vs `133–160`). See C12.

**K5 — `reaper_panorama_query` "8,633 bytes for one track over 0-20 s".**
`VALIDATION.md:16–17`. Measured today on the same capture, track `1. SubKick`,
`data=["series"], series=["rms_400ms"], interval 0–20 s`: **9,293 B**. The doc
does not name the track, so the figure is not reproducible as written; the
difference (7.6 %) is consistent with a different track or a pre-audit schema.

**K6 — "over-budget band/temporal selection (730,117 bytes)".**
`VALIDATION.md:18–19`. Measured: one track, `band_level` + `temporal_profile`,
0–20 s → **723,581 B**, `query_too_large/exact_detail_over_budget`. Same selector
over **all** sources → **26,895,126 B**. The doc omits the object scope, so its
number reads as a project-wide selection when it is a one-track selection.

**K7 — "1005 passed, 4 skipped".** `VALIDATION.md:7–9`. Not run in this audit by
instruction; unverified here. Note the same file says "treat the run, not this
line, as the source of truth", and round 7 recorded 929 passed — the number has
already drifted once.

**K8 — "Acknowledging the exact delta advances the context token."**
`ARCHITECTURE.md:67–69`, `ARCHITECTURE.ru.md:60`. Code advances automatically
(`lifecycle.py:1630–1652`). See C2.

**K9 — The guide's "Tool map" is the tool surface.** `MCP-README-LLM.md:114–136`
lists 19 of 26 tools. The seven absent tools (`reaper_flamingo_*`,
`reaper_spatial_*`, `reaper_timeline_context`) are model-facing and ungated by
the panorama receipt for `file:` targets, which the guide never explains.

**K10 — "every measured metric family" carries method/version/stage/window.**
`IMPLEMENTATION-DIRECTIVE.md` §2.4 and `MCP-README:299–301` imply per-measurement
provenance. The view has one document-level `analysis.method` and a
`stages_contract` (`panorama_view.py:574–575`), plus per-row `stage` and `window`
indices — but no per-metric method or version. Where two measurement paths exist
(e.g. partial loudness vs per-source RMS), the view cannot distinguish them by
method.

**K11 — `TZ.md` §8's "describe the current MCP set and the future interface
separately".** `TZ.md:61`. The current guide presents one contract as both; there
is no "future interface" section. The requirement is unimplemented rather than
contradicted.

**K12 — `reaper_capture_read` works on the flagship capture.** The guide
(`MCP-README-LLM.md:131`, `398–400`) presents the archive read as available with
an exact-capture receipt, and `FULL-CAPTURE.md:39–41` says it verifies the sealed
record. On `cap_01M79659V69NDC5YNAA0SMPX51` it does not: the sidecar is legacy
schema 1 (198,480 B compact), so `_read_capture_panorama` rebuilds it with
`max_bytes=DEFAULT_MAX_BYTES` (700,000) at `mcp_server.py:2232–2238`;
`build_panorama` raises `PanoramaCapacityError` (measured: estimated
**1,350,933 B**, **614,061 tokens**, domains 41 tracks / 34 items / 42 audio /
42 analysis). The `except PanoramaCapacityError` handler at `2255–2265` retries
the **identical** build with the **identical** limits and therefore re-raises —
the "legacy sidecar budget" fallback cannot succeed by construction. The raised
error's own `next_action` (`panorama.py:2495`) tells the model to
"use `reaper_panorama_detail` for exact rows", a tool removed from the surface by
DECISION §8. So the failure path is doubly stale: it fails, and its advice names
a tool that no longer exists. `lifecycle._document` avoids this by rebuilding with
`max_bytes=10**12` (`lifecycle.py:609–610`), which is why `panorama_open` succeeds
on the same capture. The view itself is clean: it contains neither
`reaper_panorama_detail` nor `reaper_panorama_page` (checked on the live view).

**K13 — RU guide is a translation of the EN source of truth.**
`IMPLEMENTATION-DIRECTIVE.md` §3B says English is the source of truth, but the RU
file contains normative content with no EN counterpart: an extra failure pattern
("Подменять уровень 30 с грубым уровнем 300 с, выбрасывать единицы или
метаданные валидности либо называть lossy-проекцию «полной»",
`MCP-README-LLM.ru.md:110–111`) and five whole sections absent from EN
(`Масштаб задач` 382, `Как выбирать инструменты` 400, `Открытые музыкальные
цели` 584, `Что сообщить пользователю по завершении` 739, `Проверка качества
интерфейса` 754). English is the source of truth in name only; the richer guide
is Russian.

---

## 5. Metric-family inventory as the documents define it

### 5.1 Scalar metrics: 24, one value per source

Declared in `panorama.py:63–91` (`ANALYSIS_METRIC_KEYS` + `ANALYSIS_METRIC_UNITS`)
and rendered as columns of `analysis.rows` in the view (`panorama_view.py:497–576`).
Document references: `MCP-README-LLM.md:299–301` and `413–415` ("24 scalar
metrics"), `FULL-CAPTURE.md:115–117`, `VALIDATION.md:15–16`, DECISION §8 (205–207).

| Group | Metrics (unit) | Count |
|---|---|---|
| Level / loudness | `integrated_lufs` (LUFS), `peak_dbfs` (dBFS), `rms_dbfs` (dBFS), `crest_db` (dB), `true_peak_estimate_dbfs` (dBTP), `loudness_range_lu` (LU) | 6 |
| Spectral / timbre | `spectral_centroid_hz` (Hz), `coarse_speech_band_ratio` (ratio) | 2 |
| Stereo / spatial | `stereo_correlation` (correlation), `side_mid_db` (dB), `mono_fold_loss_db` (dB) | 3 |
| Temporal / rhythmic | `transient_density_per_s` (events/s), `transient_count`, `two_ms_event_count`, `onset_count`, `spike_count`, `local_tempo_median_bpm` (BPM) | 6 |
| Modulation / pitch | `modulation_rate_hz` (Hz), `modulation_depth` (ratio), `pitch_jitter_pct` (%) | 3 |
| Clipping / DC | `clipped_sample_ratio`, `clipped_sample_count`, `clipping_event_count`, `dc_offset` | 4 |

The first nine are the original set; the remaining fifteen were added because
"a measured value with no path to the model is a dropped domain"
(`panorama.py:67–69`). Inline precision is 3 decimals
(`INLINE_METRIC_DECIMALS`, `panorama_view.py:57`), declared in
`reading.precision`; exact values come from the query.

### 5.2 Temporal families: one stored grid, two declared levels

* **Stored grid** — `analysis_temporal` in the canonical document; at most 24
  buckets over the content extent (`INLINE_MAP_BUCKETS = 24`, `panorama.py:62`),
  each cell the mean of the `rms_400ms` cells in that bucket (`panorama.py:828–834`).
  Measured today: width 0.8167 s, 25 buckets, 42 sources on a 20 s capture.
* **Declared levels** — 300 s and 30 s, computed by aggregating the stored cells,
  emitted as `[buckets, min, max, mean, p50]` per source with `aggregation`,
  `buckets`, `decimals` and an availability flag
  (`panorama_view.py:579–662`). A level finer than the stored grid is
  `available: false, reason: "stored grid is coarser"` — never invented.
* **Exact series** — `rms_400ms` remains in the record and is returned whole by
  the query; the view states this in `temporal.exact` (660–661).

Document references: D6-05 (`SYNTHESIS:41`), directive §2.4, DECISION §5
(142–146), `MCP-README-LLM.md:302–304`, `FULL-CAPTURE.md:157–165`.

### 5.3 Series families: 3, query-only

`rms_400ms`, `band_level`, `temporal_profile`
(`details.py:406–423`, `query.py:504`). They are catalogue entries in
`archive.catalog` with counts and byte sizes, never inline rows. `resolution_s`
coarsens a series inside an interval (`query.py` selector note); it does not
change the stored data.

### 5.4 Relation families: 2 kinds, query-only rows

`band_dominance` and `track_master_level_relation` (verified in the live view).
Inline: per-kind edge count, units, meaning, `is_not`, method, method_version,
stage relation, uncertainty, alternatives, coverage rule, plus pair outcomes and
coverage rows (`panorama_view.py:665–715`). Exact edges and evaluated pairs come
from query family `relations`. The rename from `masking` to `band_dominance` is
D5-02 (`round-5/SYNTHESIS:190`) and D7-06 (`INTEGRATED-DECISIONS:442`).

### 5.5 What "all metrics" is supposed to mean — four different meanings

This is the single most overloaded phrase in the corpus:

1. **All declared columns** — "Every metric family the capture contract knows is
   a declared column; an unmeasured value is `null`, never zero"
   (DECISION §5:134–136). This is the implemented meaning: 24 columns always
   present, `analysis.absent` counting nulls.
2. **Every value, not only flagged ones** — the journal "records every available
   metric and temporal series, not only flagged values or a selected subset"
   (`CONTEXT-LEDGER-DESIGN:826–829`). This is the owner meaning, and it is why a
   top-N issue list is not a substitute.
3. **All families in a query** — `data` defaults to `"all"`, i.e. the nine archive
   families `relations, series, midi_notes, envelope_points, items, takes, audio,
   analysis, project_map` (`query.py:40–42`, `501`). Here "all" means families,
   not metrics, and it is a different axis from (1).
4. **The full document** — "all metrics" as the canonical sidecar, 1,349,027 B on
   the 41-track capture, which is explicitly *not* what the model receives
   (DECISION §1:52–53).

Documents do not reconcile these four. A model reading "all metrics" in the guide
cannot tell whether it has all 24 columns (yes), all values at full precision
(no — 3 decimals inline), all series cells (no — query), or the canonical
document (no — projection).

**Terminology drift:** the docs call the 24 scalars "metric families"
(`MCP-README:299`, `413`), while round 5 used "family" for a construct class
(masking, partial loudness, texture). Code calls them `ANALYSIS_METRIC_KEYS` /
`COLUMNS`. The word "family" therefore has three referents; this report uses
"scalar metric" for the 24 and "family" only for the query axis.

---

## 6. What no document states

Explicit absences, so they are not mistaken for decisions:

* **No document states how metrics measured by different methods are
  distinguished.** DECISION §2 requires method "stated once" per table, and the
  code accordingly carries one document-level `analysis.method`
  (`panorama_view.py:574`). The directive's "keep methods/versions" is therefore
  satisfied only while the whole table shares one method; nothing states what the
  viewer should do when it does not.
* **No document states the token cost of the 42,864 B view under a real
  tokenizer.** DECISION §6 (180–183) lists this as not verified; the view reports
  `bytes/2.2` (`panorama_view.py:1058`), and `panorama.py:53–55` says a real BPE
  measured ~3.07 bytes/token — i.e. the estimate is deliberately conservative in
  the opposite direction from that measurement.
* **No document states the build time or CPU cost of `single`.** The DECISION
  reports bytes only.
* **No document bounds the project size at which the one-response promise holds.**
  DECISION §6 (170–176) explicitly refuses to promise it and names the failure
  mode instead.
* **No document states what a host should do when its real cap is between 48,000
  and the view size.** The refusal payload offers "raise the budget", but the
  guide does not say how a model discovers its host's cap.
* **No document states an acceptance test for the format choice** (D6-10b
  required one; none is recorded).
* **No document records the owner's own words for the 2026-11-14 reversal.** Only
  the DECISION's paraphrase exists ("The user rejected…"), and the document has
  no date line.
* **No document states whether `reaper_capture_read`'s payload is bounded.**
  It is a separate archive read and not part of the view contract.
* **No document defines "spine" outside the 20,838 B experiment**, so later uses
  of the word (e.g. "lossy projection") are analogies, not a defined artifact.

---

## 7. What the documents, taken together, actually require

Five statements survive every supersession and are supported by both a document
and the code:

1. **One representation, one revision, one response** (or an honest refusal).
2. **Nothing measured is dropped**: 24 declared columns, nulls counted, both
   temporal levels, relations catalogued with counts and caveats.
3. **Exactness is one query away**, whole and hash-verified, never paged.
4. **Structure and audio freshness are separate**, and a delta advances only what
   it can prove.
5. **No invented meaning**: no role, genre, quality verdict or cause in the view.

Everything else in the corpus is either transport history (pages, ack, cursors,
tiered briefs), a rejected alternative (spine, 200 KB, client reassembly), or a
requirement that was never implemented (per-metric method columns, EN/RU parity,
current-vs-future interface separation).

---

## Appendix A — searches run

`rg -i` over `docs/`, `src/`, `tests/` for: `page|pages|paginate|panorama|context|
receipt|delta|metric|relation|contract|chunk|cursor|ack|spill|budget|token`
(raw hit counts across `docs/*.md`: page 404, pages 134, panorama 580, context
605, receipt 104, delta 432, metric 219, relation 362, contract 281, chunk 51,
cursor 35, spill 52, budget 174, token 303). Targeted follow-ups: `spine|lossy|
reassembl|preload`, `owner (said|wrote|asked|rejects…)`, `^\| \*\*D5`, status and
date headers, and every line number cited above.

## Appendix B — measurements performed for this audit

Read-only, in-process, Python 3.13 venv, `PYTHONPATH=src`, no pytest, no REAPER:

* `PanoramaLifecycle._document("cap_01M79659…")` → 1,349,027 B, 24 metric units.
* `PanoramaLifecycle.single(project_id, capture_id=…)` → `complete`, view 42,864 B,
  envelope 44,719 B, `fits: true`, `pages: false`, 27 sections, 42 track rows,
  24 metric columns, `unmapped_sections: []`.
* Same call with `max_response_bytes=10_000` → `response_over_budget`, no
  view/receipt/token/handle, 8 section sizes, 3 narrowing options.
* `PanoramaLifecycle.query` one track `rms_400ms` 0–20 s → 9,293 B; one track
  `band_level`+`temporal_profile` 0–20 s → `query_too_large`, 723,581 B; all
  sources, same families → `query_too_large`, 26,895,126 B.
* `build_panorama(record, max_bytes=700_000, max_tokens=256_000)` on that capture
  → `PanoramaCapacityError`, estimated 1,350,933 B / 614,061 tokens; this is the
  call `reaper_capture_read` makes for a legacy sidecar (K12).
* Built view text contains no `reaper_panorama_page` and no
  `reaper_panorama_detail`.
* Sidecar on disk for that capture: 291,445 B (198,480 B compact, schema 1),
  mtime 2026-11-13 06:31 — unchanged.

**Side effect, disclosed:** each `single` call mints an additive receipt, handle
and context-token record under
`work/benchmark/himmp/capture-archive/lifecycle/{receipts,handles,contexts}/`.
Those are records of read-only opens; no capture, `record.json`, `panorama.json`
or index entry was written or modified. No file was deleted.
