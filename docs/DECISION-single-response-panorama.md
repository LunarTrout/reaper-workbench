# Decision record: one response, one panorama, no pages

Status: adopted 2026-11-14, **partly superseded the same day** by a direct owner
instruction: the model panorama is delivered in measured pages (open → page →
ack) with no page-count limit. The one-response reasoning below remains the
record of the trade-offs; `docs/MCP-README-LLM.md` is authoritative for current
behaviour.
Scope: the model-facing project context contract of the MCP server
(`reaper_audio.context.panorama_view`, `reaper_audio.context.query`,
`reaper_audio.context.lifecycle`, `reaper_audio.mcp_server`).

## 1. The problem

The previous contract delivered one logical panorama document as deterministic
byte pages: `reaper_panorama_open` returned page 0 plus a manifest,
`reaper_panorama_page` fetched the rest, and `reaper_panorama_ack` minted the
view receipt only after every page hash matched.

That protected real invariants (a model must not edit or make whole-project
claims from a partial view), but it made the *normal* path impossible for an
agent that is not driven by a hand-written client loop:

* the model had to remember to open, then page N times, then ack, before it was
  allowed to reason about the project at all;
* the receipt proved that the server had *served* every page, never that the
  model had read any of it, so the ceremony bought a weaker guarantee than it
  looked like;
* on a real song the cost was not page size but the number of round trips: a
  241 KB document became 10 pages, and a clean-session evaluation measured
  ~193 K cumulative tokens for load + ack;
* a model that answered from page 0 was, by the contract, wrong, even when the
  page contained the structure it needed.

The second draft fixed the round trips by sending the whole projection in one
result, but it measured **118,387 bytes** (envelope 120,931 bytes) on a real
41-track capture and therefore answered `response_over_budget` at any
host-realistic budget — a complete interface on a toy fixture and useless on a
real song. Raising the budget to 200,000 bytes was rejected: Hermes spills near
50,000 characters and other hosts cap lower, so "send everything and hope" is not
a portable contract either.

Measured on the archived 41-track / 42-source capture
(`cap_01M79659V69NDC5YNAA0SMPX51`):

| document section | bytes |
|---|---|
| `relations` (edges + pairs + unavailable) | 840,989 |
| `analysis` | 112,265 |
| `details` (manifest of exact rows) | 77,743 |
| `analysis_temporal` | 77,222 |
| `audio` | 55,450 |
| `crosslinks` | 45,804 |
| whole canonical document | 1,378,352 |

So the canonical sidecar cannot be the model view, and paging it was a
transport workaround for a formatting problem.

## 2. Decision

One call returns one self-describing, compact, whole-project view.

`reaper_panorama_open` now:

1. builds the canonical document for the captured revision (unchanged, archived,
   authoritative);
2. projects it into the columnar *panorama view*
   (`reaper-workbench.panorama-view/2`): every canonical domain appears exactly
   once, as a table, a descriptor, or a catalogued archive family;
3. measures the exact serialised response in UTF-8 bytes, using the real token
   and receipt lengths rather than placeholder estimates;
4. if it fits the declared budget, mints the `receipt` and the `context_token`
   **in the same result** and returns the whole view;
5. if it does not fit, returns `response_over_budget` with the measured size per
   section, the reason, and the semantic scopes that would fit — and mints
   nothing at all (no receipt, no token, no placeholder).

Exact high-frequency rows are served by one generic semantic query
(`reaper_panorama_query`): selectors are objects, interval, data family, series
and resolution. One call returns the whole selected fragment. If that fragment
is over budget the answer is `query_too_large` with its measured size per family
and the scope to narrow. There are no pages, no cursors and no numbered chunks.

Compaction levers, all of them lossless or explicitly declared:

| lever | effect |
|---|---|
| entity dictionary | every GUID and name appears once; tables reference rows by index and keep `'<kind>:<guid>'` as the stable id |
| one declared schema per table | `columns` + `rows`, shared units in `units` and `analysis.metric_units`, method and provenance stated once |
| one time axis | `[start_s,end_s)` everywhere, `_s` suffixes, `project_extent` vs derived `content_extent`, `audio_contract` holding the file↔project↔sample formulas |
| archive instead of inline rows | relation pairs, series cells, MIDI notes, envelope points, take media paths and frame maps stay sealed and are catalogued with counts and byte sizes |
| declared rounding | inline metrics to 3 decimals, level statistics to 2, stated in `reading.precision`; exact values come from the query |
| declared temporal summary | levels 300 s and 30 s with width, aggregation, anchor, bucket count and an `exact_series` pointer |
| no duplicated metadata | one `binding`, one `reading`, one `archive`; no per-row hash repetition, no repeated stage/provenance strings |

## 3. Flow (the normal path)

1. `reaper_capture_snapshot(project_id, expected_revision, archive_root)` — the
   capture exists; returns archive metadata only, never the view.
2. `reaper_panorama_open(project_id, archive_root)` — the whole view, the
   receipt and the context token in one result.
3. Work: `reaper_edit` / `reaper_api_call` / `reaper_action` / `reaper_save`
   accept that `context_token` with `expected_revision`.
4. After a change the returned delta carries its successor token in the same
   response (`advance.context_token`); no acknowledgement round trip.
5. `reaper_panorama_query` only when exact edges, cells, notes or points are
   needed; `reaper_panorama_refresh` when continuity cannot be proven.

## 4. Where the data lives

| data | location | address |
|---|---|---|
| canonical capture | `captures/<capture_id>/record.json` (sealed, immutable) | `capture_id` + `integrity_sha256` |
| canonical panorama document | `captures/<capture_id>/panorama.json` (sealed sidecar) | `document_sha256` |
| model view | built in memory, delivered in the response | `view_sha256` |
| exact series cells | the record, via the detail manifest | query family `series` + `series` + `objects` + `interval` (+ `resolution_s`) |
| MIDI notes, envelope points | the record, via the detail manifest | query families `midi_notes`, `envelope_points` |
| relation edges and unavailable pairs | the panorama document | `relations.edges`, query family `relations` (+ `objects`, `kinds`, `interval`) |
| metric scalars | the view (`analysis.rows`) and the document | stable analysis id |

The view never rewrites or replaces the archive; older sidecars are still
projected in memory on read.

## 5. Guarantees

* **Delivery, not comprehension.** The receipt guarantees: "this exact view —
  this document hash, this revision, this fact root — was delivered to this
  caller in this response". The server cannot observe whether the host kept the
  bytes or the model read them, and does not claim it.
* **One identity.** View, receipt and token carry the same `capture_id`,
  `project_id`, `revision`, `fact_root`, `document_hash`; a mutation at another
  revision, instance or epoch is refused (`stale_context`). `document_hash` is
  the sidecar's sealed `document_sha256` (recomputed and checked by
  `validate_binding` on load), not a second hash of the document that includes
  that field.
* **No silent loss.** Every canonical section is inventoried
  (`reading_inventory.mapped_sections`); a section the view does not place is
  listed in `unmapped_sections` instead of disappearing. Every metric family the
  capture contract knows is a declared column; an unmeasured value is `null`,
  never zero.
* **No invented meaning.** The view adds no role, genre, quality verdict or
  cause; `reading.epistemic` states which layers are included and which are
  empty, and `relations.kinds` carries each kind's `meaning`, `is_not`
  (band dominance is not audibility, masking or causation), method, uncertainty
  and coverage rule so the caveat travels with the counts.
* **Exactness behind the summary.** The view's `temporal.levels` aggregate the
  stored activity grid, and each stored grid cell is itself a mean of the
  `rms_400ms` cells in its bucket. The view says exactly that (`cell_meaning`,
  `anchor_basis`, `exact`) instead of calling the levels "exact cells"; the raw
  400 ms cells remain addressable by hash and are returned whole by the query.
* **Fail-closed detail.** Series, MIDI and envelope rows are re-hashed against
  the sealed manifest before they are returned; a selection over the per-request
  point limit returns `query_too_large` with the limit and narrowing advice, not
  a generic validation error.
* **Delta without a second round trip.** `changes_since` and mutation deltas
  return the successor token in the same response (`advance.context_token` with
  `advance.guarantee`). The lifecycle still implements `delta_ack` internally for
  archive compatibility, but no model step requires it.
* **Audio freshness is separate from structure.** A delta whose
  `audio_evidence.state` is `stale` advances structural edits only; audio
  interpretation waits for a fresh capture.

## 6. Honest limits

* Budget: `max_response_bytes` defaults to 48,000 UTF-8 bytes — below the Hermes
  50,000-char inline/spill threshold and inside the Codex ~10,000-token output
  budget at ~4 bytes/token. This is a policy default, not a measured promise
  from every host; a host that knows its own cap passes it explicitly.
* Measured on the 41-track / 42-source archive: canonical document 1,378,352
  bytes → **lean overview 43,576 bytes** → complete open envelope **45,431
  bytes** at the default 48,000-byte budget (`status: complete`,
  `delivery.fits: true`, `view_mode: lean_overview`). The overview keeps all 42
  mapped sources and all 24 scalar metric columns with units, decimals, window,
  stage/channel and method provenance, plus a complete per-track item/take
  `content_index`; exact item/take/audio rows are catalogued and returned whole
  by one semantic query. Inline scalar values are rounded and this is explicit:
  `analysis.precision.lossless` is false; exact canonical values are returned
  unrounded by the analysis query (for example crest 19.031398871… vs 19.031).
  If even the lean overview exceeds the budget, the refusal carries a read-only
  `read_scope` for one exact query; it is never a receipt or mutation
  authorization.
* The remaining floor is structure, not relations: one row per track/source,
  42 × 24 metric cells, one row per item/take/asset/route, plus the entity
  dictionary. A project several times larger, or one with hundreds of automation
  envelopes and thousands of items, can still exceed 48,000 bytes. That outcome
  is `response_over_budget` with measured section bytes; it is neither paginated
  nor truncated. No formatter can promise one response for an unbounded project
  on a host with a fixed per-result cap, and this contract does not pretend
  otherwise.
* Inline metrics are rounded (3 decimals) and temporal levels are declared
  summaries. A model that needs exact cells must query; the view says so and
  names the selector.
* Not verified: live delivery on Codex, the host caps of other MCP harnesses,
  and the measured token count of a real view with a host tokenizer (the view
  reports a conservative `bytes/2.2` estimate). The 41-track measurement above
  was built from the archived capture in-process; it was not a live Codex run.

## 7. Rejected alternatives

| alternative | why rejected |
|---|---|
| Keep mandatory pages | The user rejected the mandatory page walk as the architecture: it requires model discipline and client loops, and it proves delivery, not reading. |
| One giant raw response | 1.33 MB on a real capture; it spills or is truncated by every stock host, and relations alone are 841 KB. |
| Whole projection with a 200 KB budget | Not portable: Hermes spills near 50 K characters and other hosts cap lower; it also failed on a common project size. |
| A lossy "spine" summary | Measured 20,838 bytes; already owner-rejected as a lossy projection that hides unique values. |
| Keep page tooling but stop calling it mandatory | Leaves two contradictory contracts on `tools/list` and a model that cannot tell which one guards its edits. The page/ack/detail/slice/delta-ack tools are removed from the model surface instead; only private lifecycle code remains. |
| Client-side reassembly / MCP resources preload | Requires harness-specific behaviour; the contract must work in a stock MCP client. |
| Cursor-based "detail" pagination | Same failure mode as pages under another name, and it fragments a coherent fragment for transport reasons. |
| Dropping relation edges from the panorama | They are measured facts; they are catalogued with counts, kinds, units and byte sizes and served whole by the query. |

## 8. What changed in the code and what did not

Changed: the projection layer (`context/panorama_view.py`, replacing
`context/view.py`), the query layer (`context/query.py`),
`PanoramaLifecycle.single` / `.query` and the over-budget measurement, the
auto-advance on deltas, `reaper_panorama_open` semantics (no mode, no
`page_bytes`), `reaper_panorama_refresh` (re-delivers the view), the metric key
set of the canonical document (the panorama carried 9 of the 24 measured scalar
metrics; the view carries all 24 as declared columns, `null` when absent), and
the removal of
`reaper_panorama_page`, `reaper_panorama_ack`, `reaper_panorama_detail`,
`reaper_panorama_slice` and `reaper_panorama_delta_ack` from the model-facing
surface.

Unchanged: the canonical capture and panorama schemas and their hashes, the
sidecar binding validation, identity/revision comparison, the edit gates
(preview → apply → readback → undo), delta construction and fail-closed bases,
and the audio freshness rules. The lifecycle still implements page assembly and
`delta_ack` internally; no tool and no model instruction exposes them.


## 9. Independent audit: what it found and what was changed

A read-only audit of the contract (code, docs, tests, uncommitted history) found
places where the written guarantees were stronger than the implementation. The
contract was corrected rather than the wording alone:

| finding | resolution |
|---|---|
| The server `instructions`, several gate errors and `reaper_project` still told the model to fetch pages. | Rewritten to the single-response flow; no model-facing string names a page, and the page tools are no longer on `tools/list`. |
| `refresh` re-delivered page 0. | `PanoramaLifecycle.refresh` now calls `single`. |
| `document_hash` was a second hash that included the sealed `document_sha256`, so it could not be recomputed from the archive. | `document_hash()` returns the sealed `document_sha256`, which `validate_binding` recomputes on load. |
| A single handle reported 55 missing pages and the receipt carried a meaningless `page_count`. | Single handles are not split; receipts carry `page_count: null`; `status` reports `complete`/`mode`/`delivered` and no page gap. |
| The view called temporal levels "exact cells" while they aggregate the already-averaged 24-bucket grid. | `temporal.stored.cell_meaning`, `anchor_basis` and `exact` state the provenance; the raw `rms_400ms` cells remain queryable. |
| Relation caveats (not audibility, not masking, not causation) lived only in the sealed sidecar. | `relations.kinds` in the view carries `meaning`, `is_not`, method, uncertainty and the coverage rule. |
| A selection over the 20,000-point detail limit returned `validation_error`. | The query returns `query_too_large` with `reason: exact_detail_point_limit`, the limit and narrowing advice. |
| The delivered envelope was measured with a fixed reserve, not the real response. | `single` measures the full envelope with exact-length placeholders, re-measures after the real sizes are written, and refuses rather than exceed the budget. |
| `archive.catalog.rows` counted bytes for mappings, and the detail manifest was a numeric-coded matrix. | `catalog` names `row_unit`; `archive.exact` states series counts, MIDI note and envelope point totals with named selectors. |

Not changed by the audit: the relation evidence, the detail hash binding, the
edit gates, the identity/revision/epoch comparison and the audio-freshness
rules. The audit did not run tests or a live host session; the fixes above were
verified by the repository suite and an in-process build of the real capture.
