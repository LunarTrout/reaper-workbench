# Round 8 — model-facing panorama and metric representation

**Status:** research and target contract. Not implemented. This research did not
modify code, tests, README or existing docs; Part 1's small pre-existing
`render_candidate` validation fix is outside this report. The only repository
write made by this research is this file.
**Date:** 2026-11-14 (host date, UTC+03:00).
**Repository:** `reaper-audio-library`, branch `main`, HEAD `86b14bb`, worktree dirty
(90 entries in `git status --porcelain` before this file was added). `src/reaper_audio/context/**` and
`docs/research/**` are **untracked** (`git ls-files` returns 0 for both), so no commit
describes the code cited here. Every code claim below is a statement about the worktree
at the time of writing, not about a release.
**Method:** read-only. I read the code, the docs and the public MCP/host documentation,
and ran pure Python 3.14.9 builds of the existing projection functions with
`PYTHONPATH=src` on the archived capture `cap_01M79659V69NDC5YNAA0SMPX51`
(41 tracks + master, 42 sources, 20 s window). I did **not** open REAPER, did **not**
start the MCP server, did **not** run pytest, and did **not** call any
`PanoramaLifecycle` method (so no receipt, handle, context or delta record was minted and
no archive file was written). No audio was decoded and no DSP was run: every measurement
below is a byte size, count or projection of archived JSON, and the DSP facts are read
from the cited code. Where a number comes from the documentation rather than my run, it
is labelled `[DOC]`.

Evidence labels:

| label | meaning |
|---|---|
| `[CODE]` | verified by reading the cited file and line range today |
| `[MEASURED]` | produced by the read-only in-process run described in Appendix A |
| `[DOC]` | a statement in a repository document, quoted or paraphrased with its location |
| `[SPEC]` | the MCP specification or official host documentation, fetched 2026-11-14 |
| `[HOST]` | host-specific behaviour documented by Claude Code, Codex or Hermes |
| `[INFERENCE]` | my reasoning from the cited facts, not directly stated by a source |
| `[UNVERIFIED]` | asserted somewhere but not checkable in this environment |

---

## 0. What this report found, in one page

The one-response architecture is sound and reproduces: the compact view is
**42,864 B** and the open envelope **44,719 B** on the reference capture, both exactly
the documented figures, at the 48,000-byte default `[MEASURED]`. The view is nearly flat
in song duration (20 s → 300 s adds 642 B, +1.5 %) because series are catalogued rather
than inlined `[MEASURED]`. That is the correct shape for a portable, no-pages contract.

What does **not** hold is the view's *internal contract*:

1. **The 24-metric matrix is not attributable to tracks.** `analysis.rows.source` is
   `-1` for all 42 rows, and `audio.rows.source` is `-1` for all 42 assets. The
   documented foreign key "source indexes the track list" is false in the current code.
   The values are present; the join that tells a model *whose* values they are is not.
   `[CODE]` `[MEASURED]`
2. **One advertised series family crashes.** `reaper_panorama_query` with
   `series=["temporal_profile"]` (or `data=["all"]` on one track) raises
   `TypeError: mappingproxy is not JSON serialisable` before it can return or refuse.
   `[CODE]` `[MEASURED]`
3. **The "measured" archive catalog is wrong for three of nine families.** `series`,
   `midi_notes` and `envelope_points` report the byte size of a *summary* section, not
   the archived family; `series` and `analysis` both report 112,265 B because they
   measure the same object, while the actual series payload is **27,138,014 B** for the
   project. `[CODE]` `[MEASURED]`
4. **The `project_map` query family is not the raw project map.** It returns a 2.4 KB
   `project` block; the raw map in the record is 228,348 B / 17 keys and no MCP tool
   returns it. `[CODE]` `[MEASURED]`
5. **Several declared columns are permanently empty**: take `rate`, `pitch`,
   `stretch_n`, `fx_n`; FX `params`; `audio.source`; `analysis.source`. The producer's
   canonical projection does not carry those fields, so the columns are schema without
   data. `[CODE]` `[MEASURED]`
6. **Per-measurement provenance is document-level only.** `analysis.method.version` is
   `null` because no producer writes `analysis_version`; `analysis.stage` is
   `"file_or_buffer"` for every row, not the render stage; per-metric confidence is
   never produced by `measure_audio`; the seven metric caveats are in the sealed
   document but the view
   reports only their count. `[CODE]` `[MEASURED]`
7. **The four completeness dimensions are design-only.** No `completeness` object
   exists; one `coverage.complete` boolean and `delivery.fits` carry all four meanings.
   `[CODE]`
8. **Removed tools are still named in reachable responses.** `changes_since` returns
   `next_call.tool = "reaper_panorama_delta_ack"`; the canonical document's detail
   entries carry `fetch_tool = "reaper_panorama_detail"` and its `reading_contract`
   names that tool; the capacity error's `next_action` names it too. `[CODE]`
9. **`reaper_capture_read` is a second, unbounded representation that fails on the
   flagship.** It rebuilds a legacy sidecar with the same 700,000-byte limit that just
   failed, so it raises `PanoramaCapacityError` on the 1.35 MB document; when it does
   succeed it returns the whole canonical document. `[CODE]`
10. **A global 3-decimal rounding creates false zeros.** 39 measured values on the
    reference round to `0` at 3 decimals, including `dc_offset = 3.4e-05` and
    `mono_fold_loss_db = -0.000327`. `[MEASURED]`

The host research does not rescue any of this. The MCP specification puts **no size
limit and no truncation semantics** on tool results, and resources are explicitly
application-driven; Claude Code, Codex and Hermes each impose their own per-result
budget, and Hermes replaces an over-threshold MCP result with a **1,500-character
preview plus a file path**. A resource link or a UI expander is therefore not content
delivered to the model. The one-response contract is the right portable shape; the work
is to make its inventory, attribution, completeness and refusal payloads truthful.

**The target contract in one sentence:** keep one inline view with all 24 metrics for
every source, make every declared FK and column resolve, add four named completeness
dimensions and per-measurement descriptors, make every query selector cover every row it
names and return units/method with the rows, make refusals carry the matching counts and
the resolution that would fit, and key deltas by stable id with an explicit
`not_comparable` verdict across method/window/stage changes.

---

## 1. The current contract, element by element

The table separates what the contract *is* from why it exists. "Why" is traced to an
owner/product document where one exists; implementation choices are marked as such in
§2.2.

| # | element | current code | why it exists | state |
|---|---|---|---|---|
| C1 | **Revision identity**: `project_id`, `change_count`, `path`, `instance_id`, `epoch` | `context/revision.py`, `context/identity.py`; compared by `lifecycle.revisions_match` (`lifecycle.py:366–386`) | A path and a counter do not prove continuity after a REAPER restart; `[DOC]` `ARCHITECTURE.md:22–24` | Working; a side that lacks `instance_id`/`epoch` compared with one that has it is a mismatch, not a pass (`lifecycle.py:382–386`) |
| C2 | **Capture binding**: `capture_id`, record integrity, revision before/after, time window | `panorama.build_panorama` `capture_binding` (`panorama.py:2190–2201`); `validate_binding` (`2548–2585`) | The canonical record is the truth; a sidecar must prove it belongs to that record | Working; `validate_binding` recomputes the document hash and rejects tampering |
| C3 | **Document identity**: `document_sha256` = hash of the document except itself | `panorama.py:2467–2469`; `lifecycle.document_hash` returns the sealed value (`330–341`) | One hash that can be recomputed from the archive; a second, differently-defined hash was rejected in the DECISION audit | Working; the DECISION §9 correction is in code |
| C4 | **Fact root**: capture-independent factual view hash | `lifecycle.stable_fact_view` (`233–323`), `fact_root` (`326–327`) | A delta must be bound to facts, not to capture-local ids, paths or render hashes | Working; ids, capture ids, per-render hashes and detail locators are removed before hashing |
| C5 | **View hash**: `view_sha256` over the projection | `panorama_view.build_view` (`1034`) | Binds the delivered bytes; the receipt carries it | Working; deterministic for a given document + budget parameter (the budget is part of `reading.budget`, so the hash varies with `max_response_bytes`; the receipt records the budget, so it is recoverable) |
| C6 | **Receipt** (`pr_…`): delivery, not comprehension | `lifecycle.single` (`1138–1175`), `require_receipt` (`2249–2406`) | The server can prove it served one exact view; it cannot prove the model read it. `[DOC]` DECISION §5 (122–125) | Working; 24 h TTL (`lifecycle.py:91`), exact capture/project/root/document checks |
| C7 | **Context token** (`ct_…`): the cross-call state handle | `lifecycle._mint_context` (`812–846`); accepted by `require_receipt` (`2350–2372`) | MCP has no session; the spec sanctions server-minted handles passed as ordinary arguments `[SPEC]` (`server/tools`, "Stateful Tools") | Working; the token is accepted as the mutation gate and as the query's `panorama_receipt` argument; a delta mints a successor |
| C8 | **Whole-or-nothing open** | `lifecycle.single` (`1067–1182`): measure, mint only if it fits, else `response_over_budget` with no view/receipt/token/handle (`_over_budget`, `2409–2472`) | The owner rejected page walks and partial views as the architecture `[DOC]` DECISION §7 | Working; envelope 44,719 B reproduces exactly at 48,000 B `[MEASURED]` |
| C9 | **Exact semantic query** | `query.run_query` (`334–492`); `lifecycle.query` (`1184–1238`); `mcp_server.reaper_panorama_query` (`2712–2757`) | Exact rows must be requestable whole, without pages or cursors; a semantic selector is not a transport slice `[DOC]` DECISION §2 | Partly working: `temporal_profile` crashes; `metrics` is advertised but absent; `kinds`/`bands_hz` do not narrow pairs; exact `analysis` loses units |
| C10 | **Deltas** | `lifecycle.changes_since` (`1240–1480`), `record_change` (`1527–1662`), `changes.build_structural_delta` (`546–688`) | A model must be able to update its picture without re-reading, and must not silently patch a wrong base `[DOC]` DECISION §5; round-6 D6-07 | Working for mutations (id-keyed object facts, auto-advance); capture-to-capture metric diffs are positional (see G16) |
| C11 | **Audio freshness separate from structure** | `changes.build_structural_delta` (`615–647`); `lifecycle._require_audio_fresh` (`2229–2247`) | A structural delta must not make stale audio current `[DOC]` DECISION §5 | Working; a standard manifest always declares unverified classes, so a mutation delta is normally `stale` (§2 of the guide) |
| C12 | **Stale refusal** | `StaleContext` with `stale_context`; `changes_since` returns `stale_base` reasons (`1253–1351`) and `refresh_required` (`1319–1351`) | A wrong base must be refused, not guessed `[DOC]` round-6 D6-07 | Working; reasons are specific (`wrong_project`, `root_mismatch`, `identity_discontinuity`, `chain_too_long`, `delta_too_large`) |
| C13 | **Completeness** | `coverage.complete` (`panorama_view.py:737`), `reading_inventory` (`1027–1032`), `delivery.fits` (`lifecycle.py:1111`) | The word "complete" must not hide four different claims `[DOC]` `LLM-MCP-NO-PAGES-DESIGN.md:46–57` | **Design-only**: one boolean, no dimension names (G10) |
| C14 | **Provenance** | `binding` (`926–950`), `analysis.method` (`panorama.py:1840–1845`), `units` (`958–970`), stage/window indices (`panorama_view.py:558–565`) | Every number must answer whose/where/when/how/precision `[DOC]` `IMPLEMENTATION-DIRECTIVE` §2.4; TZ §3 | Partial: units yes, document-level method yes, per-metric method/version/stage/confidence no (G7–G9) |
| C15 | **No invented meaning** | `reading.epistemic` (`868–878`); `tracks.role` always `null`; relation `meaning`/`is_not` are authored constants (`panorama.py:1298–1325`) | No role, genre, quality or cause from the builder `[DOC]` DECISION §5; TZ §1 | Working; the only prose in the view is fixed author-written caveat text, never model output |

### 1.1 The four completeness dimensions, as designed

The round-7 design names them P1–P4 `[DOC]` `LLM-MCP-NO-PAGES-DESIGN.md:46–57`:

| id | dimension | what it asserts |
|---|---|---|
| P1 | `project_inventory_complete` | every track/master/item/take/FX/route/envelope/asset/analysis source appears exactly once with a stable id |
| P2 | `overview_metrics_complete` | every source has all 24 scalar columns, a unit per metric, `null` + absent counts, stage/window/method/provenance, both levels, relation kinds with caveats |
| P3 | `archive_families_addressable` | every exact-only family is named with a typed ref, row count/unit, byte size, selectors, delivery status and coverage |
| P4 | `exact_scope_delivered` | a query returned every row of the chosen selector, hash-verified, or refused with its measured size |

Implementation status: **none of the four names exists in `src/`** (grep returns no
`project_inventory_complete`, `overview_metrics_complete`,
`archive_families_addressable` or `exact_scope_delivered`). The current response has one
`coverage.complete` (`panorama_view.py:737`) that means only "no declared domain is
missing or truncated", and `delivery.fits` that means only "the bytes fit". A model
cannot distinguish "inventory complete, exact rows not delivered" from "everything
delivered", which is exactly the conflation the design was written to prevent.

---

## 2. Requirements, implementation choices, and prior-agent assumptions

### 2.1 Owner/product requirements

"Owner" means the person commissioning the work as recorded in a repository document.
Quotes are verbatim and trimmed.

| # | requirement | evidence | current state |
|---|---|---|---|
| O1 | Portable tools-only MCP; no harness-specific mechanism | `[DOC]` `round-6/IMPLEMENTATION-DIRECTIVE.md` §2.1 (quoted in `ARCHAEOLOGY-REQUIREMENTS.md:123`) | Met: no context-engine import; `mcp_server.py` uses `MCPServer` only |
| O2 | One model-facing state representation | `[DOC]` `round-4/CONTEXT-LEDGER-DESIGN.md` owner addendum (quoted `ARCHAEOLOGY:124`) | Mostly met for the panorama path; `reaper_project` is a declared bootstrap read (`mcp_server.py:808–887`), `reaper_capture_read` is a second full-document read (G21) |
| O3 | No reduced panorama; keep both 30 s and 300 s levels | `[DOC]` `round-6/PANORAMA-CONTINUITY-RESEARCH-DISPATCH.md` §138–140 (quoted `ARCHAEOLOGY:125`) | Met: `temporal.levels` always declares both (`panorama_view.py:579–662`); a finer-than-stored level is `available:false` |
| O4 | Whole project in one response, or nothing | `[DOC]` `MCP-README-LLM.md:389–394`; DECISION §2 | Met on the reference; refusal payload exists |
| O5 | No mandatory page walk / client loop | `[DOC]` DECISION §7 (189) | Met on the tool surface: 27 tools, none page/ack/detail/slice (`grep @server.tool`) |
| O6 | Exact unrounded values remain reachable | `[DOC]` DECISION §6 (177–179) | Partly met: `analysis`, `items`, `takes`, `audio`, `relations` work; `temporal_profile` crashes; `metrics` selector missing |
| O7 | Receipt = delivery, not comprehension | `[DOC]` DECISION §5 (122–125) | Met: `receipt_guarantee` at `lifecycle.py:1114–1115` |
| O8 | Delta advances without a second round trip | `[DOC]` DECISION §3 (101), §5 (151–154) | Met: `record_change` calls `delta_ack` itself (`1636–1653`); `changes_since` auto-advances (`1450–1479`) |
| O9 | Audio freshness separate from structure | `[DOC]` DECISION §5 (155–157) | Met: `require_receipt(..., require_audio_fresh=True)` on interpretation |
| O10 | No invented meaning, no genre recipe | `[DOC]` DECISION §5 (137–141); `TZ.md:14–16` | Met; see C15 |
| O11 | Keep units, methods/versions, stage, channel view, windows, validity, coverage, uncertainty, source links | `[DOC]` `IMPLEMENTATION-DIRECTIVE` §2.4 (quoted `ARCHAEOLOGY:133`); `TZ.md:21–29` | Partial: units and windows yes; per-metric method/version, stage, channel view and confidence no (G7–G9) |
| O12 | Minimise the number of agent actions | `[DOC]` `TZ.md` §3 (line 28: "Свести число действий агента к необходимому минимуму") | Met for the normal path (one open, one query when needed); over-budget forces a different task decomposition, not a smaller call |
| O13 | Context includes FX parameters and takes | `[DOC]` `TZ.md:21–29` ("FX, параметры, айтемы, тейки") | Not met inline: `fx.params` is always `[]`; take `rate/pitch/stretch_n/fx_n` are empty (G5–G6) |
| O14 | Bilingual model guide, English source of truth | `[DOC]` `IMPLEMENTATION-DIRECTIVE` §3B (quoted `ARCHAEOLOGY:135`) | Files exist; RU is 799 lines vs EN 761 and contains material absent from EN (`ARCHAEOLOGY:377–386`, still true) |

### 2.2 Implementation choices that are not owner requirements

These are defensible engineering decisions, but they are not requirements and should not
be defended as if they were:

* **48,000-byte default** (`panorama_view.py:53`). A policy default chosen below the
  Hermes 50,000-character spill and inside a 25,000-token host cap at ~3.07 B/token. The
  DECISION's parenthetical "inside the Codex ~10,000-token output budget at ~4
  bytes/token" does not hold arithmetically: 48,000 / 4 = 12,000 tokens (15,635 at 3.07
  B/token). The default is a policy choice, not a protocol constant and not a proof of
  fitting a 10k-token budget.
* **3 decimals for every inline metric** (`panorama_view.py:57`) and **2 for level
  statistics** (`:60`). A compression choice; it creates false zeros (§4.3).
* **24 inline grid buckets** (`panorama.py:62`). A presentation bound for the activity
  grid, not a measurement resolution.
* **Entity dictionary + index FKs** (`panorama_view.py:242–281`). A lossless
  deduplication choice; it is only lossless if the indices resolve (they do not, G1).
* **20,000-point query cap** (`details.py:35`) and **4,000-op delta cap**
  (`lifecycle.py:89`). Bounds that turn into typed refusals; not requirements.
* **24-hour receipt TTL** (`lifecycle.py:91`). An operational choice.
* **Page machinery retained internally** (`lifecycle.open/page/ack/slice/detail`,
  `147`–`2152`). Archive compatibility; no tool exposes it. It keeps the paging tests
  alive (`tests/test_panorama_lifecycle.py:89–487`) and is dead weight for the
  model-facing contract.

### 2.3 Prior-agent assumptions, checked

| assumption | where it appears | verdict |
|---|---|---|
| The view asserts four completeness dimensions | `LLM-MCP-NO-PAGES-DESIGN.md:46–57` | **Not implemented** (G10) |
| `analysis.metric_methods`, `analysis.source_ids`, `analysis.evidence` exist | same, 409–413 | **Not implemented**; the implemented view has a `source` index column instead of `source_ids`, and no metric methods/evidence block |
| "series: 112,265 B on a 20 s capture" | same, 773; DECISION §1 table | **Wrong referent**: 112,265 B is the canonical `analysis` row size; the exact series payload is 27,138,014 B `[MEASURED]` (G3) |
| Catalog `bytes` are "measured on the canonical family" | `panorama_view.py:817` | **False for 3 of 9 families** (G3) |
| "Every project-target analysis requires a receipt for that exact capture" | `MCP-README-LLM.md:401–402` | **False for `reaper_analyze`**: it uses `_require_interpretation_receipt` without `expected_capture_id` (`mcp_server.py:953–956` vs `134–163`) |
| "No model-facing string names a removed tool" | DECISION §9 (227) | **False for `reaper_panorama_detail` and `reaper_panorama_delta_ack`** in reachable responses (G19) |
| "One response is the only portable path" | DECISION §2, §7 | A defensible `[INFERENCE]` from host behaviour, not a protocol guarantee (§5.5) |
| Relation kinds carry `byte size` | `MCP-README-LLM.md:307` | **Not in the view**; the kind object has `edges`, units, meaning, method, uncertainty, alternatives, coverage rule, no bytes (`panorama_view.py:690–702`) |
| The view carries `model_interpretations` / `causal_hypotheses` keys | `MCP-README-LLM.md:320–322` | **Not in the view**; only prose strings `reading.epistemic.interpretations`/`.hypotheses` (`panorama_view.py:868–870`) |

### 2.4 What the documents actually require, separated from transport history

Five statements survive every supersession and are supported by both a document and the
code (`ARCHAEOLOGY:516–532`):

1. one representation, one revision, one response (or an honest refusal);
2. nothing measured is dropped: 24 declared columns, nulls counted, both levels,
   relations catalogued with counts and caveats;
3. exactness is one query away, whole and hash-verified, never paged;
4. structure and audio freshness are separate, and a delta advances only what it can
   prove;
5. no invented meaning: no role, genre, quality verdict or cause in the view.

Everything else — pages, ack ceremonies, cursors, tiered briefs, the 20,838-byte
"spine" — is either superseded transport or a rejected alternative. The four
completeness dimensions and per-metric method descriptors are *design intentions* that
have not yet become code; this report treats them as the target, not as current facts.

---

## 3. Actual data inventory

Everything in this section was read in the current worktree. Where a family is
advertised but not produced or not reachable, the table says so.

### 3.1 Scalar metrics: 24, one value per source

Declared in `panorama.py:63–75` (`ANALYSIS_METRIC_KEYS`), units in `:78–91`
(`ANALYSIS_METRIC_UNITS`), produced in `analysis/metrics.py:449–485`. The two sets match
one-to-one; units are complete (24/24) `[MEASURED]`.

| group | metrics (unit) | producer condition |
|---|---|---|
| level / loudness (6) | `integrated_lufs` (LUFS), `peak_dbfs` (dBFS), `rms_dbfs` (dBFS), `crest_db` (dB), `true_peak_estimate_dbfs` (dBTP), `loudness_range_lu` (LU) | LUFS/LRA are `None` for >2 channels or <3 s (`metrics.py:387–406`); true peak is a 4× FIR estimate, note says "not validated for standards compliance" (`451–454`) |
| spectral / timbre (2) | `spectral_centroid_hz` (Hz), `coarse_speech_band_ratio` (ratio) | always; the second is an "energy ratio proxy; not standards SII" (`473–474`) |
| stereo / spatial (3) | `stereo_correlation` (correlation), `side_mid_db` (dB), `mono_fold_loss_db` (dB) | only for 2-channel input; otherwise `None` (`366–380`, notes `463–469`) |
| temporal / rhythmic (6) | `transient_density_per_s` (events/s), `transient_count` (count), `two_ms_event_count` (count), `onset_count` (count), `spike_count` (count), `local_tempo_median_bpm` (BPM) | always except tempo (`None` when no confident local estimate) |
| modulation / pitch (3) | `modulation_rate_hz` (Hz), `modulation_depth` (ratio), `pitch_jitter_pct` (%) | from `temporal_profile`; `None` for unsupported channel layouts |
| clipping / DC (4) | `clipped_sample_ratio` (ratio), `clipped_sample_count` (samples), `clipping_event_count` (count), `dc_offset` (amplitude) | always |
| **total** | **24** | |

Two unit mismatches between producer and declaration `[CODE]`: `dc_offset` is produced
with unit `"FS"` (`metrics.py:475`) but declared `"amplitude"` (`panorama.py:87`);
`transient_count` and the three event counts are produced as `"events"`
(`metrics.py:462,478–481`) but declared `"count"` (`panorama.py:83,88–89`). Cosmetic
today, but a consumer that trusts `metric_units` gets a different unit from the one the
producer attached.

Absent values on the reference capture `[MEASURED]`: `integrated_lufs` 4,
`loudness_range_lu` 4, `true_peak_estimate_dbfs` 4, `stereo_correlation` 4,
`local_tempo_median_bpm` 8 — 24 nulls out of 1,008 cells. The absent counts are
declared in `analysis.absent` (`panorama_view.py:556`), so a model can see them.

Other scalar families that exist but are **not** in the 24-column matrix:

* `peak_estimate` (oversample factor, coverage) — stored in `curves` (`metrics.py:495`),
  visible only through the true-peak note.
* `coverage` per analysis report (`loudness`, `true_peak`, `stereo`, `transients`,
  `temporal_profile`, `short_events`, `local_tempo`) — in the record, not in the
  panorama's per-source row (`metrics.py:509–525`).
* `rms_stats` count/min/max/mean/p50 — inline in `analysis.rms_stats`
  (`panorama_view.py:534–538,566–568`), derived from `rms_400ms`.

### 3.2 Time series: three, all query-only

| series | producer | stored shape | nominal resolution | inline? | query status |
|---|---|---|---|---|---|
| `rms_400ms` | `_time_levels` (`metrics.py:84–94`), 0.4 s window, 0.1 s hop | `curves.rms_400ms_time_s`, `curves.rms_400ms_dbfs` | 0.1 s; **decimated** by `step=ceil(len/20000)` (`metrics.py:446–448`) | no; summary in the activity grid + levels | works; one source 20 s = 8,747 B `[MEASURED]` |
| `band_level` | `_spectrogram` + `_band_profile` (`metrics.py:46–81`), 28 third-octave centres 25 Hz–12.5 kHz | `curves.band_level_time_dbfs` (cells × 28), `curves.spectrogram_time_s`, `curves.band_centers_hz` | STFT hop 1024 samples at the capture rate (~21 ms at 48 kHz), also `step`-decimated | no; only counts and band count in `series_contract.meta` | works; one source 5 s = 136,853 B `[MEASURED]` |
| `temporal_profile` | `analysis/temporal.py`, per-cell loudness, flux, pan, width, tempo, event summary | `curves.temporal_profile` (dict of `times` + `cells`) | 0.1 s hop | no; descriptor only | **crashes** on `mappingproxy` (G2) |

`series_entry` (`details.py:467–479`) declares only `count`, `content_sha256`, `units`,
`value_kind` and `band_count`; it does **not** declare the window width, hop, decimation
factor or cell fields. The view's `analysis.series_contract.meta`
(`panorama_view.py:569–572`) therefore cannot tell a model that a "400 ms" series on a
track longer than ~33 minutes is actually 0.2 s or coarser. On the 20 s reference
`step=1`, so no data is lost today; the declaration gap is still a silent-downsample
path for long tracks (G23).

The 2 ms event list (`curves.events_2ms`, `metrics.py:494`; produced by
`analysis/events.py` with kinds `onset`, `spike`, `clip`) is measured and stored but
has **no query family and no detail entry**; only its four counts reach the model
(G26). The per-cell tempo fields (`tempo_bpm`, `tempo_confidence`,
`tempo_alternatives`) and `event_summary_2ms` ride inside `temporal_profile` cells
(`metrics.py:418–442`) and are reachable only through that series.

### 3.3 The temporal grid and the two declared levels

* The canonical grid is built by `panorama._activity_grid` (`791–867`): at most 24
  buckets over the widest source extent (`INLINE_MAP_BUCKETS=24`, `panorama.py:62`),
  each cell the mean of the `rms_400ms` cells in that bucket, keyed by **stable source
  id** (`panorama.py:834`).
* On the reference: width 0.8167 s, 25 buckets, 42 sources, covered interval 0–19.6 s
  `[MEASURED]`.
* The view's `_temporal` (`panorama_view.py:579–662`) aggregates the *stored cells*, not
  the raw 400 ms cells, into 300 s and 30 s levels, emits `[buckets,min,max,mean,p50]`
  per source with `aggregation`, `decimals`, and `available:false` + reason when the
  stored grid is coarser (`622–625`). On a 20 s capture both levels have one bucket; on
  a 300.5 s capture they have 2 and 11 `[MEASURED]`.
* `stored.cell_meaning` correctly states the double aggregation (`654–657`). The
  `anchor_basis` string is wrong: it says `"stored anchor_s"` even though the canonical
  grid has no `anchor_s` key (it has `anchor: "first_source_cell_start_s"`,
  `panorama.py:851`), so the code took the `anchor_raw is None` branch and still labelled
  it "stored" (`panorama_view.py:586–601`) (G24).

### 3.4 Relations: six declared kinds, two emitted, three not measured, one never implemented

`context/relations.py:27–36` declares
`band_dominance`, `band_dominance_pair`, `phase_pair`, `track_master_level_relation`,
`reference_comparison`, `partial_loudness_share`; `MANDATORY_RELATION_KINDS` is
`{band_dominance, track_master_level_relation}` (`37–39`). The capture path
`panorama._relations_domain` (`1491–1624`) builds:

| kind | produced by the capture? | evidence | on the reference |
|---|---|---|---|
| `band_dominance` | yes | `analysis/relations.py:374–585`, called at `panorama.py:1550` | 1,039 edges, method `analysis.relations.band_dominance`, version `1` |
| `track_master_level_relation` | yes | `analysis/relations.py:834–947`, called at `panorama.py:1556–1559` | 41 edges, method `analysis.relations.track_master_level_relation`, version `1` |
| `reference_comparison` | **no** (no references supplied) | `reference_candidates(master_report, [], ...)` `panorama.py:1560–1562` | `reference_status: "not_supplied"`, one `no_reference_supplied` unavailable entry |
| `phase_pair` | **no** | always `RelationBatch(unavailable={"phase_pair": "phase_pair_not_measured_in_capture"})` `panorama.py:1570–1572` | one unavailable entry |
| `band_dominance_pair` | as an **evaluation table**, not an edge kind | `evaluations.band_dominance_pairs` (`context/relations.py:682–734`) | 820 evaluated pairs, 577 `evaluated`, 174 `no_active_band_cells`, 69 `no_dominant_band` |
| `partial_loudness_share` | **never** | no producer in `src/`; partial loudness is computed only in `analysis/bundle.py:357–372` | absent from `relations.kinds` and from `unavailable` |

The view's `relations.kinds` lists only kinds with at least one edge
(`panorama_view.py:670`), so the two emitted kinds appear with meaning/is_not/method/
method_version/stage relation/uncertainty/alternatives/coverage rule/units
(`690–702`); the absent kinds do not appear there, only as two of the 176
`unavailable` reasons (G12). The kind metadata is good: for example
`band_dominance` says "not audibility, not masking, and not an instruction to change
either source" and carries an uncertainty reason about dense distorted material
`[MEASURED]`. This is the right shape and should be preserved.

Relation sizes `[MEASURED]`: 1,080 edges, 820 pairs, 176 unavailable; the canonical
family is 840,989 B; an unfiltered query is 827,767 B; one track is 54,912 B; one
track + kind + band fits at 48,000 B (1 edge + 40 pairs). A single edge averages 496 B
(16-digit floats + repeated coverage reason), a pair 334 B.

### 3.5 MIDI, automation, envelopes, items, takes, FX, routes, audio

| family | inline in the view | canonical document | query | not reachable |
|---|---|---|---|---|
| **tracks + master** | one row per track plus master (last row): channel count, folder depth, vol, pan, mute, solo, selected, arm, send/receive/hw counts, FX count, item count, audio/analysis indices, `role` (always null) | `tracks`, `master` (`panorama.py:1667–1723`) | `project_map` returns only the project block | some raw mixer/record fields (record mode, group, phase, width) are only in the raw map |
| **items** | geometry: track index, start/end/pos/len/offset, playrate, mute, loop, fades, channel mode, take count, active take index | `items` (`panorama.py:224–290`) | `items` returns the canonical rows | group id, item notes, stretch markers (in the raw map) |
| **takes** | `item`, `track`, `midi`, `rate` (null), `pitch` (0), `stretch_n` (null), `fx_n` (null); `media` computed but excluded from `columns` (`panorama_view.py:346–362`) | `takes` has only `take_id`, `item_id`, `track_id`, `name`, `is_midi`, `source_media_path`, `midi_id` (`panorama.py:319–327`) | `takes` returns media path + those fields | take playrate, preserve-pitch, stretch markers, channel mode, group — **not captured in the panorama projection** |
| **MIDI** | one row per captured MIDI take: take index, note count, start/end | `midi` summary + a typed detail ref (`panorama.py:331–420`) | `midi_notes` returns exact notes with derived durations, up to 10,000 (`MIDI_NOTE_LIMIT`, `panorama.py:57`) | per-note channel/PPQ are in the notes if captured; none on this capture |
| **automation** | one row per automation envelope: owner, kind, name, point count, interval | `automation.envelopes` (`panorama.py:2300–2305`) | `envelope_points` returns points for a track id | point shapes/targets are in the detail entry, not the view |
| **envelopes** | same shape for the `envelopes` domain | `envelopes.envelopes` (`2306–2311`) | same | the **same tempo-map envelope is listed in both domains with two ids** (`automation:…` and `envelope:…`) and two detail refs with identical content hashes (G25) |
| **FX** | owner, slot, name, on, offline, `param_n`, `params` (always `[]`) | `fx` rows have `parameter_count`, `raw_parameters_pointer`, no `parameters` (`panorama.py:442–454`) | no `fx_params` family; `reaper_project(include_fx_params=True)` is a live read, not the archive | all parameter values in the panorama path |
| **routes** | from/to track indices, send index, vol, pan, mode, mute, kind | `routes` (`panorama.py:403–420`) | `project_map` only; no route family | route IDs in the raw map |
| **audio** | source index (−1, broken), master flag, stage index, revision index, rev change count, SR, channels, duration, duration-consistent, tail flag, sha256 prefix, analysis index | `audio` (`panorama.py:959–1048`) | `audio` returns the full rows (frames, mapping, tail samples, 64-hex hash) | — |
| **stages** | `audio.stages` table `[stage, method, complete]`, e.g. `master_mix_post_fx`, `selected_track_via_master_parallel_batch_post_fx` (`full_capture.py:693,1136,1536–1537`) | per-asset `render_provenance` | same | — |

Two code areas the task listed are not metric families and should not be mistaken for
one: `analysis/artifacts.py` is a manifest/cleanup layer for rendered WAVs (ownership,
SHA-256, size, MIME, TTL), and `daw/render_candidate.py` is the plan-bound candidate
render path; both concern audio provenance and cleanup, not panorama measurement. The
capture assembly itself is `daw/full_capture.py:1499–1524` (audio rows + `analysis{}`),
the analyze path is `daw/session.py`, and the bridge supplies project facts through
`scripts/reaper_bridge.lua:1145` (`project_digest`) and rendered stages through `:1652`
(`project_capture_render`).

### 3.6 Master and signal stages

The master is a track row at index `len(tracks)` (`panorama_view.py:255–256,289`) and a
separate entity (`entities.master`). Its audio asset is a separate source with
`is_master: 1` and its own render stage. The analysis row's `stage` is **not** the render
stage: `_analysis_view` reads `provenance["stage"]` (`panorama.py:930`), which
`measure_audio` sets to `"file_or_buffer"` (`metrics.py:505`). On the reference every
analysis row reports `stage = "file_or_buffer"` and `analysis.stages = ["file_or_buffer"]`
`[MEASURED]`, while the actual render stages are only in `audio.stages`. A model that
reads `analysis.stage` learns nothing about whether a stem went through the master; it
must join analysis → audio → stage through `analysis_id`/`audio.analysis_i`, and the
analysis→track join is the broken `source` column (G1, G8).

### 3.7 Model-generated semantic claims

There are none. `epistemic_layers.model_interpretations` and `causal_hypotheses` are
constructed as `included:false, items: []` (`panorama.py:2382–2391`), and the view
restates that as two prose strings (`panorama_view.py:868–870`). `tracks.role` is always
`null` (`panorama.py:1696–1697`, view column `role`); the test asserts it
(`tests/test_single_view.py:254–261`). The only natural-language claims in the view are
fixed author-written caveats: relation `meaning`/`is_not`/`uncertainty`/`coverage_rule`,
`reading.*`, `units.*`, and table notes. No LLM output, no ranking, no quality verdict.

Outside the panorama, `reaper_mix_evidence` builds ranked evidence with heuristic
severities (`analysis/mix_evidence.py`), and `reaper_analyze` returns a report resource;
neither is part of the panorama and neither is archived into a capture.

### 3.8 Explicit missing / unsupported / error states

| state | where | example |
|---|---|---|
| per-domain coverage | `coverage.domains` with `status`, `truncated`, `missing` (`panorama_view.py:718–750`) | `complete: true` on the reference |
| unmeasured metric | `analysis.absent` counts + `null` cell | 5 metrics with nulls, §3.1 |
| unsupported channel layout | `lufs_note`/`lra_note`, `temporal_profile.feature_coverage` | `unsupported_channel_layout: provide an explicit downmix` (`metrics.py:330`) |
| too-short material | LRA note `not computed: LRA requires 3 s analysis windows` (`metrics.py:397–400`) | |
| insufficient signal | `coverage.local_tempo` (`metrics.py:524–525`) | `insufficient_signal: no confident local tempo estimate` |
| level not available | `temporal.levels[].available:false` + `reason: "stored grid is coarser"` (`panorama_view.py:622–625`) | |
| relation unavailable | `relations.unavailable` reasons (`panorama_view.py:672,705–707`) | `no_active_band_cells`, `no_reference_supplied`, `phase_pair_not_measured_in_capture` |
| MIDI cap | detail entry `truncated` flag + `MIDI_NOTE_LIMIT` (`panorama.py:57`) | coverage marks it; never "complete" |
| point cap | `query_too_large` with `reason: exact_detail_point_limit`, `point_limit` | `query.py:402–424` |
| byte cap | `query_too_large` with `reason: exact_detail_over_budget` / `selection_over_budget` | `query.py:425–491` |
| over-budget view | `response_over_budget`, no view/receipt/token/handle | `lifecycle._over_budget` (`2409–2472`) |
| stale context/base | `stale_context`, `stale_base` with reason, `refresh_required` | `lifecycle.py:1253–1351` |
| binding failure | `panorama_binding_invalid` | `validate_binding` (`2548–2585`) |
| capacity failure | `panorama_capacity_exceeded` | `build_panorama` (`2469–2499`) |
| capture invalid | `capture_master_invalid`, `media_unresolved` | `mcp_server._next_action` codes (`225–244`) |

### 3.9 Advertised but not delivered

This is the list that matters most for "an LLM must not miss an obvious capability":

| advertised | where | actual |
|---|---|---|
| `analysis.rows.source` indexes the track list | column note `panorama_view.py:561–563` | `-1` for all 42 rows (G1) |
| `audio.source` indexes the track list | note `panorama_view.py:483–487` | `-1` for all 42 rows (G1) |
| `takes.rate/pitch/stretch_n/fx_n` | columns `panorama_view.py:359` | null/null/null/0, fields absent from canonical takes (G5) |
| `fx.params` | column `panorama_view.py:399–400` | `[]` for every FX; canonical rows carry no `parameters` (G6) |
| `takes.media` | computed at `panorama_view.py:357`, omitted from `columns` at `:359` | media is query-only, which the note says — but the dead computed field is confusing |
| `analysis` family selector `metrics` | `panorama_view.py:94–96` | no `metrics` parameter in `run_query` (`query.py:334–342`) (G15) |
| `project_map` family | `panorama_view.py:97–98` "the raw REAPER project map" | returns `document["project"]`, 2,430 B vs the raw 228,348 B (G4) |
| catalog `bytes` measured on the canonical family | `panorama_view.py:817` | wrong for `series`, `midi_notes`, `envelope_points` (G3) |
| relation kinds with byte size | `MCP-README-LLM.md:307` | no bytes in the kind object (G12, doc) |
| four completeness dimensions | design `LLM-MCP-NO-PAGES-DESIGN.md:46–57` | absent (G10) |
| per-metric method/version/stage/confidence | design 409–413; TZ §3 | absent (G7–G9) |
| `phase_pair`, `partial_loudness_share` kinds | `relations.py:27–36` | one unavailable, one never emitted; neither listed in `relations.kinds` (G12) |
| event list (2 ms) | `metrics.py:494` | measured, stored, no address (G26) |

---

## 4. Measurements performed today

All numbers below are from the read-only in-process run in Appendix A. The archive is
`work/benchmark/himmp/capture-archive/captures/cap_01M79659V69NDC5YNAA0SMPX51`.

### 4.1 The view

| quantity | measured | documented | delta |
|---|---|---|---|
| canonical document | **1,350,947 B** | 1,349,027 B `[DOC]` DECISION §6, VALIDATION:11 | +1,920 B (+0.14 %) |
| compact view | **42,864 B** | 42,864 B `[DOC]` | exact |
| open envelope (reconstructed with the real revision block and 32-char tokens) | **44,719 B** | 44,719 B `[DOC]` | exact |
| track rows incl. master | 42 | 42 | exact |
| metric columns | 24 | 24 | exact |
| view top-level sections / unmapped | 27 / 0 | 27 / 0 | exact |
| 300.5 s capture, same 42 sources | 43,506 B | — | +642 B vs the 20 s view |
| 300.5 s capture, 43 sources | 44,410 B | — | +904 B vs the 42-source one (different project/name set and one fewer FX) |

The view and envelope reproduce exactly; only the canonical document figure is stale.
Because the view is unchanged, the extra 1.9 KB sits in sections the view does not
project verbatim (detail-entry locators and similar metadata).

Section sizes of the 42,864 B view `[MEASURED]`:

| section | bytes | section | bytes |
|---|---|---|---|
| analysis | 9,234 | binding | 2,184 |
| entities | 5,015 | tracks | 2,044 |
| temporal | 3,483 | reading | 1,927 |
| audio | 3,146 | archive | 1,713 |
| relations | 3,008 | items | 1,687 |
| routes | 1,193 | takes | 1,101 |
| audio_contract | 1,084 | project | 1,135 |
| coverage | 961 | time | 784 |
| fx | 781 | derived | 508 |
| reading_inventory | 362 | units | 311 |
| automation | 213 | envelopes | 211 |
| transport | 210 | midi | 130 |

Row costs measured by serializing the real row arrays `[MEASURED]`:

| row kind | n | total B | per row B |
|---|---|---|---|
| tracks (incl. master) | 42 | 1,762 | 42.0 |
| items | 34 | 1,469 | 43.2 |
| takes | 34 | 899 | 26.4 |
| audio | 42 | 2,091 | 49.8 |
| analysis | 42 | 5,186 | 123.5 |
| analysis.rms_stats | 42 | 1,294 | 30.8 |
| temporal level rows (each level) | 42 | 1,305 | 31.1 |
| fx | 16 | 642 | 40.1 |
| routes | 40 | 1,048 | 26.2 |

Bottom-up per source: 42 (track) + 55 (entity pair) + 50 (audio) + 124 (analysis)
+ 31 (rms stats) + 62 (two level rows) = **364 B**, before names and relation-count
digits. The controlled 42→43 comparison suggests 0.36–0.95 KB per source depending on
names/relations; treat the per-source cost as a band, not a constant.

### 4.2 Exact queries

| selection | status | bytes |
|---|---|---|
| `analysis`, all 42 sources | query | 112,265 |
| `analysis`, one source | query | 4,251 |
| `series` `rms_400ms`, one source, 0–20 s (197 cells) | query | 8,747 |
| `series` `rms_400ms`, one source, 0–20 s, `resolution_s=1` | query | 5,945 |
| `series` `band_level`, one source, 0–5 s | query | 136,853 |
| `series` `band_level`, one source, 0–2 s, `resolution_s=0.5` | query | 5,568 |
| `series` `temporal_profile`, any selection that fits the point cap | **TypeError** | — |
| `series`, all sources, any budget | `query_too_large/exact_detail_over_budget` | 27,138,014 |
| `relations`, all | query | 827,767 |
| `relations`, one track | `query_too_large/selection_over_budget` | 54,912 |
| `relations`, `kinds=["track_master_level_relation"]` | `query_too_large` | 314,955 |
| `relations`, one track + kind + band | query | fits (1 edge + 40 pairs) |
| `items`, one track | query | 2,181 |
| `takes`, all | query | 14,290 |
| `envelope_points`, all (6 points, duplicated) | query | 2,858 |
| `project_map`, all | query | 2,430 |

The byte column is the whole query response (`budget.measured_bytes`) where the code
reports it, otherwise the family payload; the two differ by the query envelope (~1 KB).
The point of the table is the order of magnitude, not a per-row promise.

Per-cell costs `[MEASURED]`: `rms_400ms` ≈ 44 B/cell; `band_level` ≈ 291 B/cell
(≈27.4 KB per second per source); `analysis` ≈ 2,673 B/source; a relation edge ≈ 496 B;
a relation pair ≈ 334 B.

### 4.3 The rounding defect

`INLINE_METRIC_DECIMALS = 3` (`panorama_view.py:57`) and `nz` rounds every float
(`195–208`). On the reference, **39 measured values are non-zero but round to 0**,
including `dc_offset = 3.4e-05` → `0`, `dc_offset = 2.1e-05` → `0`, and
`mono_fold_loss_db = -0.000327` → `0` `[MEASURED]`. For DC offset, "0" is a different
signal-hygiene claim from "34 µFS": a model acting on the inline view would conclude
there is no DC offset. The exact query recovers the value, but the inline view is the
one the owner required to carry every measured metric.

### 4.4 The FK defect

`Entities.source_index` is keyed by the **raw GUID**
(`panorama_view.py:265–269`), but `_audio` and `_analysis` look up `row["source_id"]`,
which is the **stable id** `track:<guid>` or `master:<guid>` (`panorama.py:1007,923,531`;
`panorama.py:1007` sets `source_id` from `track_id`/`master_id`). No key matches, so the
lookup returns `-1`:

* `analysis.rows`: 42/42 `source == -1`
* `audio.rows`: 42/42 `source == -1`

`tracks.audio_i` and `tracks.analysis_i` work (they look up `audio_id`/`analysis_id`,
which are stable ids in both places). The view's note "source indexes the track list"
(`panorama_view.py:561–563,483–487`) is therefore false, and the only remaining
attribution is row order, which no note declares. Worse, `reading.tables` says
"`-1 = absent`" (`panorama_view.py:856–857`), so a model that follows the contract is
told that every analysis row and every audio asset has **no source**, not that the join
failed. `tests/test_single_view.py:57–59` asserts row counts and column presence, not FK
values, so the defect is untested.

### 4.5 The temporal_profile crash

`query.run_query` resolves detail rows and then measures them with
`section_bytes(detail_payload)` (`query.py:445`). `section_bytes` calls
`panorama_view.canonical` → `_json_default` (`panorama_view.py:169–174`), which handles
`Path` and objects with `.item()` but **not** `MappingProxyType`. The record is frozen
into `MappingProxyType` by `context/full_capture.py:120`, and `temporal_profile` cells
contain nested frozen mappings (e.g. `event_summary_2ms`). `panorama.canonical`
(`panorama.py:100–107`) and `details.canonical` (`details.py:45–52`) both handle it;
the view's serializer does not. Reproduced:

```
run_query(document, record, data=["series"], series=["temporal_profile"], ...)
→ TypeError: mappingproxy is not JSON serialisable
run_query(document, record, data=["all"], objects=["track:…"])  → same
```

No test queries `temporal_profile` through `run_query` (`grep` over `tests/` finds it
only in analysis-level tests), so the failure is unguarded.

### 4.6 The catalog defect

`_archive` (`panorama_view.py:753–844`) measures each family with
`section_bytes(source)` where the source is chosen per family (`762–775`):

* `series` → `entities.analysis` (the analysis rows), so its 112,265 B is the same
  object as `analysis`; the actual series cells are in `record.analysis[*].curves` and
  the query payload is 27,138,014 B.
* `midi_notes` → `document["midi"]` (the summary rows), 2 B here, not the note payload.
* `envelope_points` → the automation/envelope summary rows, 3,075 B, not the point
  payload (2,858 B for all six points).
* `relations` → the relations domain, 840,989 B — correct.
* `items`, `takes`, `audio`, `analysis` → the canonical rows — correct.
* `project_map` → `document["project"]`, 2,414 B, labelled `top_level_keys` 12 — wrong
  object (G4).

The note "bytes measured on the canonical family" (`817–818`) is therefore a false
guarantee in the one place whose purpose is to prove nothing was dropped.

---

## 5. MCP and host research (primary sources, accessed 2026-11-14)

### 5.1 Protocol guarantees

Current specification: **2026-07-28** (the `latest` redirect resolves there). All URLs
below were fetched on 2026-11-14.

| # | source | finding that constrains the design |
|---|---|---|
| S1 | `https://modelcontextprotocol.io/specification/2026-07-28/server/tools` | A tool result carries `content[]` (text, image, audio, `resource_link`, embedded `resource`) and optional `structuredContent`; `outputSchema` is optional. `resource_link` is "a URI that can be subscribed to or fetched by the client", and such links are "not guaranteed to appear in the results of a `resources/list` request". `structuredContent` is "server-produced result data"; a tool that returns it **SHOULD** also return the serialized JSON in a text block for compatibility. |
| S2 | `.../server/resources` | Resources are **application-driven**: "host applications determining how to incorporate context based on their needs". Reading is a separate `resources/read`; the protocol does not require a host to read anything a tool links. |
| S3 | `.../basic/index` | MCP is **stateless**; state across calls "MUST be referenced by an explicit identifier the client passes on each request". |
| S4 | `.../server/tools` §"Stateful Tools" | Non-normative but explicit: "The protocol has no concept of a state handle; from the wire's perspective a handle is an ordinary string in a tool result and an ordinary argument to subsequent tool calls." Servers that need state return an explicit handle; the model carries it forward. |
| S5 | `.../server/utilities/pagination` | Pagination applies to **list operations** ("servers to yield results in smaller chunks"); page size is server-determined; cursors are opaque. There is no page/cursor mechanism for a single tool result. |
| S6 | `.../server/tools` (whole page) | The specification defines **no size limit** and **no truncation semantics** for a tool result. The only size-related normative text in the spec concerns `$ref` fetching (`basic/index`, JSON Schema usage), not result payloads. |

Consequences that are protocol-level, not host-level:

* A server may return an arbitrarily large text block; the protocol will carry it. Any
  cap is imposed by the host or transport. `[SPEC]`
* A `resource_link` is an address, not content. Whether the model ever sees the resource
  depends on the host's context assembly, which the protocol explicitly leaves to the
  application. `[SPEC]`
* A server-minted handle (`handle`, `receipt`, `context_token`) is the sanctioned
  cross-call state pattern, not a workaround. `[SPEC]`
* There is no protocol way to make an unlimited payload fit, and no protocol page for a
  tool result. A server can only measure and refuse, or return a semantic selection.
  `[INFERENCE from S1–S6]`

### 5.2 Host-specific limits

| host | documented behaviour | source |
|---|---|---|
| **Claude Code** | "displays a warning when MCP tool output exceeds 10,000 tokens and limits output to 25,000 tokens by default"; `MAX_MCP_OUTPUT_TOKENS` changes the limit; tools can declare `anthropic/maxResultSizeChars`; successful text results longer than **50,000 characters** are saved to a file regardless of the variable unless the tool declares that annotation; over the limit with no image content, the result is saved to a file and replaced in the conversation with a message naming the path | `https://code.claude.com/docs/en/mcp-configuration` (MCP output limits and warnings); `https://code.claude.com/docs/en/mcp.md` |
| **Codex** | `mcp_servers.<id>.tools.<tool>.output_token_limit`: "Token budget for one MCP tool's output, before the standard 20% serialization allowance. Overrides the model's default output truncation budget for that tool." Server `instructions` should keep the first 512 characters self-contained. | `https://www.codex-docs.com/en/docs/config-file/config-reference`; `https://www.codex-docs.com/en/docs/extend/mcp` |
| **Hermes** | MCP tool results are persisted above **50,000 chars**; the message is replaced by `<persisted-output>` + a **1,500-char** inline preview + a path; the full text is only seen if the model calls `read_file`. `read_resource` is an ordinary tool result subject to the same spill; resource subscriptions do not update the model's view; `list_changed` does not inject content. | local install, `skills/autonomous-ai-agents/hermes-agent/references/context-assembly-and-tool-budgets.md:13–17,29–33`; `https://hermes-agent.nousresearch.com/docs/user-guide/configuration` (tool_output caps) |

Three different behaviours, one conclusion: **above a host's threshold the content is
not delivered to the model** — it is replaced by a path, a preview, or a truncation
marker. The thresholds are host policy, not protocol. The smallest documented one we
found is Hermes's 50,000-character spill (and Claude Code's 50,000-character file
spill); Claude Code additionally warns at 10,000 tokens and caps at 25,000 by default.
The 48,000-byte default sits below all three, which is why it works on the reference —
but it is a policy, not a guarantee. `[HOST]` `[INFERENCE]`

An important unknown: Codex's *default* per-tool output budget is not stated in the
current config reference; only the override key is documented. An older `docs/config.md` in the
`openai/codex` repository (`https://github.com/openai/codex/blob/7c6a47958abe86590f131848a7af5fd2d4c69819/docs/config.md`,
accessed 2026-11-14) lists `tool_output_token_limit` default **2,560 tokens** for
stored function/tool outputs. If that default is still
shipped, a 42,864-byte view (~14k tokens at 3.07 B/token) would be truncated in history
even though the tool call succeeded. This is `[UNVERIFIED]` for the current build and is
the strongest reason to keep the view far below 48 KB where possible and to report its
measured size. `[HOST]` `[UNVERIFIED]`

### 5.3 Why a resource link, a UI expander, or a client feature is not content

* **Resource link.** The spec defines it as a URI "that can be subscribed to or fetched
  by the client" (S1), and resources are "application-driven" (S2). Hermes registers
  `read_resource` as an ordinary tool result and applies the same spill rules; its
  reference explicitly says a resource subscription notification does not update the
  model's view. The model receives a URI or a preview, not the bytes, unless a later
  call fetches them. `[SPEC]` `[HOST]`
* **UI expander.** An expander is a human-facing control in the host UI. It changes what
  the *person* sees; it cannot change the bytes the host placed in the model request.
  If the host truncated the result, the model's context still contains the truncated
  text. `[INFERENCE]`
* **Client-side reassembly / preload.** The DECISION rejects it for portability, and the
  spec supports the rejection: a stock client is not required to fetch a link, subscribe
  to a resource, or preload anything. A contract that depends on it is a contract for
  one client. `[SPEC]` `[DOC]` DECISION §7.
* **`structuredContent`.** It is the same JSON value, not a compression; the spec says a
  server SHOULD also mirror it as text for compatibility (S1), which can double the
  bytes in hosts that keep both. In the installed Python SDK, a tool returning a plain
  dict without an output model emits unstructured content only
  (`mcp/server/mcpserver/utilities/func_metadata.py:203–226` in the environment used
  here), so the current server does not double the payload — but that is an SDK
  implementation detail, not a protocol guarantee. `[SPEC]` `[CODE]` (environment)

### 5.4 Patterns for lossless structured representation

| pattern | how it preserves losslessness | cost / limit | fit for this contract |
|---|---|---|---|
| schema-once columnar JSON (this view) | one `columns` legend per table, row arrays, shared units/method; no key repetition | measured 31.5× smaller than the canonical document; still linear in entities | **keep**; it is the reason the one-response contract exists |
| dictionary/interning of identities | every GUID/name once in `entities`, rows reference by index | only lossless if the indices resolve (G1) | **keep and fix** |
| content-addressed manifest + typed refs | counts, units, hash, interval per exact family; fetch verifies the hash before returning rows | a ref is not a row; the model must query | **keep**; it is the honest exact path |
| revision-bound delta | JSON-path or id-keyed operations against a base root | positional paths produce false changes on insert; must be id-keyed | **keep, fix** (G16) |
| server-minted handle | spec-sanctioned cross-call state (S4) | handle lifetime/expiry must be stated | **keep** |
| spill-to-file + handle | host keeps the bytes out of context and gives a path | the model must read the file; the read itself consumes context; the path may be host-specific | **host behaviour, not a contract**; do not depend on it |
| code execution / query-over-data | model runs code and returns only the needed rows | requires a sandboxed code host; not portable | **optional accelerator only** (`https://www.anthropic.com/research/code-execution-with-mcp`, accessed 2026-11-14) |
| binary columnar (Arrow/Parquet) + base64 | compact for numeric matrices | +33 % base64, unreadable to the model, needs a decoder; worse for a text-only MCP result | **reject** for model-facing rows; acceptable for archived artifacts |
| progressive disclosure / tool search | reduces tool-schema cost, not result cost | orthogonal | **orthogonal**; does not solve the view size |

Anthropic's context-engineering guidance makes the same point from the other side:
context is a finite, curated budget and large tool results are normally compacted or
cleared (`https://www.anthropic.com/research/effective-context-engineering-for-ai-agents`,
accessed 2026-11-14). That is an argument for one compact view plus exact queries, not
for trusting a large result to survive.

### 5.5 What the evidence does and does not establish

* **Establishes:** the protocol has no result-size limit and no page for tool results; a
  resource link is not delivered content; handles are the sanctioned cross-call state;
  the three measured hosts all replace or spill large results.
* **Does not establish:** that 48,000 B is safe on every host (Codex's default is
  unknown); that a model comprehends the columnar view (no comprehension test was run;
  the design's D6-10b required one — `ARCHAEOLOGY:226–235`); that a host will not
  duplicate `structuredContent` and text; that Codex/Claude Code live delivery has been
  exercised (DECISION §6 says it has not).

---

## 6. Target representation

### 6.1 Design rules

1. **One inline view per revision; all 24 metrics for every source stay inline.** The
   matrix is the cheapest decision-critical data in the view (124 B/source) and the
   thing the owner named explicitly ("every available metric … not only flagged
   values", `ARCHAEOLOGY:456–459`). It is never replaced by a link, a top-N, or a
   per-source query workflow.
2. **Exactness is a query, not a page.** A selector names a meaning (objects, interval,
   family, series, metrics, kinds, bands); the response is the whole selection or a
   measured refusal. No order, no cursor, no ceremony.
3. **Every declared field resolves.** A column that cannot be populated is removed or
   marked `not_captured` with a reason; an FK that returns `-1` for a present object is
   a contract violation.
4. **Completeness is four named dimensions**, never one boolean.
5. **Provenance travels with the number**: unit, method, method version, stage, channel
   view, window basis, decimals, validity and confidence (or `confidence: not_produced`).
6. **No silent downsample or zero.** Every aggregation declares width, anchor,
   aggregation and exact pointer; every rounding declares decimals and flags a value
   that rounded to zero from a non-zero measurement.
7. **Refusals are measured and actionable**: exact bytes, per-family bytes, matching
   counts, and the resolution/interval that would fit.
8. **Deltas are id-keyed and comparability-gated**: a numeric before/after only when
   method, window and stage match; otherwise `not_comparable`.
9. **The catalog never lies about bytes.** A size is either measured on the exact
   payload or labelled `estimate` with its method.
10. **The view adds no meaning.** No role, genre, quality, cause; the only prose is
    authored caveat text.

### 6.2 Inline view, schema `reaper-workbench.panorama-view/3`

Keep the current section names where they work; add/repair the following. This is a
field-level target, not a wire dump.

```jsonc
{
  "schema_version": "reaper-workbench.panorama-view/3",
  "binding": { /* capture_id, project_id, revision{before,after}, time_window,
                  record_integrity_sha256, source_document_sha256, identity,
                  evidence, render_manifest */ },

  "completeness": {
    "project_inventory": {
      "complete": true,
      "counts": {"tracks": 41, "master": 1, "items": 34, "takes": 34, "fx": 16,
                  "routes": 40, "envelopes": 1, "audio": 42, "analysis": 42},
      "missing": [], "truncated": []
    },
    "inline_metrics": {
      "complete": true, "sources": 42, "declared_metrics": 24,
      "present_per_metric": {"integrated_lufs": 38, "...": 0},
      "absent": {"integrated_lufs": 4, "...": 0},
      "attribution": "analysis.rows.source -> entities.track index"
    },
    "archive_families": {
      "complete": true, "families": 9,
      "catalogued": ["relations","series","midi_notes","envelope_points","items",
                     "takes","audio","analysis","project_summary"],
      "unavailable": []
    },
    "exact_delivery": {"delivered": false,
                        "reason": "inline_view_contains_no_exact_rows"}
  },

  "entities": {
    "track": [["{GUID}", "1. SubKick"], ...],   // index -> [guid, name]
    "item":  [["{GUID}"], ...],
    "take":  [["{GUID}", "take name"], ...],
    "audio": [["{GUID}"], ...],
    "analysis": [["{GUID}"], ...],
    "source": ["track:{GUID}", ..., "master:{GUID}"],  // index -> stable source id
    "note": "row i of a table uses entity i of its kind; ids are '<kind>:<guid>'"
  },

  "tracks": {
    "columns": ["source","ch","folder","vol","pan","mute","solo","sel","arm",
                 "send_n","recv_n","hw_n","fx_n","item_n","audio_i","analysis_i",
                 "stage_i","role"],
    "rows": [[0,2,0,0.56,0,0,0,0,0,1,0,0,0,1,0,0,0,null], ...],
    "stages": [["master_mix_post_fx","native_render",1],
               ["selected_track_via_master_parallel_batch_post_fx","native_render",1]]
  },

  "items": { /* as today: track index, [start,end), pos, len, offset, rate,
                mute, loop, fades, channel mode, take count, active take index */ },

  "takes": {
    "columns": ["item","track","midi","rate","pitch","stretch_n","fx_n",
                 "ch_mode","group","media_ref"],
    "rows": [[0,0,0,1.0,0,0,0,"normal",null,null], ...],
    "note": "rate/pitch/stretch_n/fx_n/ch_mode/group come from the raw take subtree;
              media_ref is the archive pointer; media path is query-only"
  },

  "fx": {
    "columns": ["owner","slot","name","on","offline","param_n","params","params_omitted"],
    "rows": [[34,0,"VST: ReaEQ (Cockos)",1,0,19,[],19], ...],
    "note": "params inline only when param_n <= 8; otherwise params_omitted names the
              exact query family fx_params"
  },

  "analysis": {
    "metric_columns": ["clipped_sample_count", ... 24 sorted names ...],
    "metric_units": {"...": "..."},
    "metric_methods": {
      "integrated_lufs": {"unit":"LUFS","method":"analysis.metrics.bs1770",
                           "method_version":"1","stage":"file_or_buffer",
                           "window_basis":"measured_section","decimals":2,
                           "channel_view":"2_channels"},
      "...": {}
    },
    "methods": [["analysis.metrics.bs1770","1","file_or_buffer","2_channels"], ...],
    "rows": {
      "columns": ["source","available","method_i","stage_i","window_i",
                   ... 24 metric names ...],
      "rows": [[0,1,0,1,0,0,0,...], ...]
    },
    "windows": [[0.0,20.0]],
    "absent": {"integrated_lufs": 4, "...": 0},
    "metric_decimals": {"dc_offset": 6, "clipped_sample_ratio": 6,
                         "stereo_correlation": 4, "peak_dbfs": 2, "...": 3},
    "rounded_to_zero": [{"source": "track:...", "metric": "dc_offset",
                          "stored": 3.4e-05, "inline": 0}],
    "uncertainty": {"notes": [{"metric":"true_peak_estimate_dbfs",
                                "note":"4x polyphase FIR estimate; not validated ..."}],
                     "confidence": {"metric": {"source": value}},
                     "confidence_basis": "not_produced"  // or "per_metric_analysis_record"
    },
    "rms_stats": { /* as today */ },
    "series_contract": {
      "meta": {"rms_400ms": {"units":"dBFS","value_kind":"scalar_per_cell",
                              "window_s":0.4,"hop_s":0.1,"decimation_step":1},
                "band_level": {"units":"dBFS","value_kind":"band_vector_per_cell",
                                "band_count":28,"hop_s":0.0213,"decimation_step":1},
                "temporal_profile": {"units":null,"value_kind":"cell_record_per_cell",
                                      "hop_s":0.1,"cell_fields":["..."]}}
    }
  },

  "temporal": {
    "stored": {"width_s":0.8167,"anchor_s":0.0,
                "anchor_basis":"first stored cell (no anchor_s in the document)",
                "buckets":25,"sources":42,
                "cell_meaning":"each stored cell is the mean of the rms_400ms cells
                                 in that bucket"},
    "levels": [{"width_s":300.0,"available":true,"buckets":1,
                 "aggregation":"mean of stored grid cells per width-second bucket",
                 "decimals":2,"rows":[[42,...]]}, ...],
    "exact": "query family series, series=rms_400ms, objects=[source], interval=[a,b)"
  },

  "relations": {
    "complete": true,
    "kinds": [
      {"kind":"band_dominance","status":"emitted","edges":1039,"units":{...},
       "meaning":"...","is_not":[...],"method":"analysis.relations.band_dominance",
       "method_version":"1","stage_relation":{...},"uncertainty":{...},
       "alternatives":[...],"coverage_rule":"..."},
      {"kind":"track_master_level_relation","status":"emitted","edges":41, ...},
      {"kind":"reference_comparison","status":"unavailable",
       "reason":"no_reference_supplied","edges":0, ...},
      {"kind":"phase_pair","status":"unavailable",
       "reason":"phase_pair_not_measured_in_capture","edges":0, ...},
      {"kind":"partial_loudness_share","status":"not_measured_in_capture","edges":0,
       "meaning":"..."}
    ],
    "evaluated_pairs": 820,
    "pair_outcomes": {"evaluated":577,"no_active_band_cells":174,"no_dominant_band":69},
    "unavailable": {"count":176,"reasons":{...}},
    "coverage": { /* counts, not repeated per-pair reason lists */ },
    "reference_status": "not_supplied",
    "content_hash": "..."
  },

  "archive": {
    "catalog": {
      "columns": ["family","rows","row_unit","inline_bytes","exact_bytes",
                   "exact_bytes_method","selectors","delivered_inline"],
      "rows": [
        ["relations",2076,"edges+pairs+unavailable",3008,840989,"measured_json",[...],false],
        ["series",56112,"cells",0,27138014,"measured_query_payload",[...],false],
        ["midi_notes",0,"notes",0,0,"measured_json",[...],false],
        ["envelope_points",1,"points",0,2858,"measured_json",[...],false],
        ...
      ]
    },
    "exact": { /* count/address per family, as today */ }
  },

  "project": {"extent":...,"content_extent":...,"window":...,"render_tail":...,
               "counts":...},
  "audio": { /* as today, with source fixed and stage index */ },
  "audio_contract": { /* as today */ },
  "coverage": { /* domains + incomplete/missing/truncated + counts */ },
  "reading": { /* precision incl. per-metric decimals, epistemic, ids, time,
                  detail, budget */ },
  "reading_inventory": {"mapped_sections":[...],"unmapped_sections":[]},
  "view_sha256": "..."
}
```

Notes on the sketch:

* `entities.source` is the missing dictionary that makes `analysis.rows.source` and
  `audio.rows.source` meaningful; alternatively emit `source_id` strings in both rows
  (cost ~50 B/row × 84 ≈ 4.2 KB — the dictionary is cheaper and keeps ids stable).
* `analysis.stage_i` points into `tracks.stages`, so the render stage is on every
  measurement row; `method_i` points into `analysis.methods`, so a capture with two
  measurement paths can say so per row. A single-method capture costs one row.
* `metric_methods` is 24 small objects (~2.5 KB, estimate). If that is too much, the
  `methods` table plus a per-metric `method_i` map is smaller; either is acceptable as
  long as the per-metric method/version/stage/window/channel/decimals are resolvable.
* `uncertainty.notes` inline costs ~600 B (estimate) and removes the current "7 notes
  exist but you cannot see them" state.
* `rounded_to_zero` is a small array (39 entries ≈ 3 KB on this capture, estimate). It is
  **too expensive for the 5,136 B headroom**; make the default a per-metric
  `min_abs_inline` declaration (`10^-decimals / 2`) and expose the named
  `rounded_to_zero` list only through the query. Do not silently print 0 either way.
* `relations.kinds` must include zero-edge kinds with `status` and `reason`. This is the
  only place the caveat can travel when a kind was not measured.
* `archive.catalog.exact_bytes` for `series` is expensive to compute by serializing
  1.1 M floats; the builder can store `cells × value_kind` and a measured
  `bytes_per_cell` from a sample, labelled `estimate`. For `relations`, `midi_notes`,
  `envelope_points`, `items`, `takes`, `audio`, `analysis`, the exact payloads are small
  enough to measure at build time. The rule is: measured or labelled estimate, never a
  different object's size.

**Budget note.** The reference view has **5,136 B** of headroom at the 48,000-byte
default. The P1 additions as sketched (metric methods ~2.5 KB, notes ~0.6 KB, an inline
`rounded_to_zero` list ~3 KB) would exceed it. The default must therefore be:
`methods` table + per-metric index for method/version/stage, inline notes, and
`min_abs_inline` instead of the named rounding list; the named list is a query extra.
If a proposed field cannot fit that budget, it moves to the query path or the owner
explicitly raises the default — the one-response fit on the reference must not be
broken silently.

### 6.3 Exact query, schema `reaper-workbench.panorama-query/2`

Signature:

```
reaper_panorama_query(
  handle, revision, archive_root, panorama_receipt,
  data=[relations|series|midi_notes|envelope_points|items|takes|audio|analysis|
        project_summary|fx_params],
  objects=[stable ids | guids | unique names | "all"],
  interval={start_s,end_s},
  series=[rms_400ms|band_level|temporal_profile],
  metrics=[...],                  # new: filters analysis columns, not rows
  kinds=[...], bands_hz=[...],
  resolution_s=float,
  include_pairs=true|false,       # default true for relations; false narrows cost
  max_response_bytes=int
)
```

Response (success):

```jsonc
{
  "schema_version": "reaper-workbench.panorama-query/2",
  "status": "query",
  "binding": {"capture_id":..., "project_id":..., "revision":...,
               "document_sha256":..., "matches_manifest": true},
  "selector": {"data":[...],"objects":[...],"interval":...,"series":[...],
                "metrics":[...],"kinds":[...],"bands_hz":[...],"resolution_s":...},
  "units": {"analysis": {...24...}, "series": {...}, "relations": {...},
             "midi_notes": {...}, "envelope_points": {...}},
  "method": {"analysis":"reaper-audio-library","version":"...","decoder":"libsndfile"},
  "metric_methods": {"...": {"unit":"...","method":"...","method_version":"...",
                              "stage":"...","window_basis":"...","channel_view":"...",
                              "decimals":3}},
  "scope": {"matching_rows": 42, "returned_rows": 42, "matching_points": 1336,
             "returned_points": 1336, "complete": true, "hash_verified": true},
  "data": {"analysis": [ ... ]},
  "budget": {"measured_bytes":..., "max_response_bytes":..., "fits":true,
              "family_bytes": {...}},
  "note": "the whole selection; no pages, no cursor, no truncation"
}
```

Response (refusal):

```jsonc
{
  "status": "query_too_large",
  "reason": "selection_over_budget" | "exact_detail_point_limit" |
            "exact_detail_over_budget",
  "binding": {...},
  "selector": {...},
  "matching_rows": ..., "matching_points": ...,
  "point_limit": 20000,                    // when relevant
  "bytes_per_point": 44.4,                 // measured for this family
  "max_points_for_budget": 1081,           // floor(max_response_bytes / bytes_per_point)
  "min_resolution_s_for_budget": 0.02,     // duration / max_buckets, for series
  "measured_bytes": ..., "max_response_bytes": ...,
  "family_bytes": {...},
  "narrow": ["objects=[...]", "interval={...}", "metrics=[...]",
              "series=[...] with resolution_s=..."],
  "note": "the rows exist in the archive; this is one response's size, not a missing
            measurement. Nothing was truncated or paged."
}
```

Rules:

* Every selector applies to **every row kind it names**. If `kinds` is supplied, the
  `evaluated_pairs` table must be filtered to that kind (or `include_pairs=false` must
  omit it); otherwise the filter does not reduce cost and the model's mental model of
  the selector is wrong (G11).
* `metrics` filters columns, not rows: `analysis` rows still carry `source`,
  `available`, `method_i`, `stage_i`, `window_i` so attribution survives a projected
  matrix. The response's `metric_methods` covers the selected metrics only.
* `units` and `metric_methods` are present in **every** response, including a refusal
  for the analysis family. A model must never need to remember the earlier view to
  interpret a number (G14).
* `scope.matching_*` are computed before slicing; `returned_*` after. `complete` is true
  only when they are equal. This is the machine-checkable no-truncation invariant.
* `min_resolution_s_for_budget` is an `[INFERENCE]` helper, computed from the measured
  `bytes_per_point` and the requested interval; it is advisory, not a promise.
* No `data` key on a refusal; no partial rows; no `next`/`cursor`/`offset`.

### 6.4 Delta, schema `reaper-workbench.structural-delta/2`

```jsonc
{
  "schema_version": "reaper-workbench.structural-delta/2",
  "base": {"context_token":..., "capture_id":..., "revision":...,
            "fact_root":..., "view_root":..., "document_hash":...},
  "target": {"revision":..., "fact_root":..., "document_hash":...},
  "changed": [
    {"kind":"track","id":"{track-guid}","field":"pan","before":-0.25,"after":0.1},
    {"kind":"analysis","id":"analysis:{source-guid}","metric":"peak_dbfs",
      "before":-1.2,"after":-0.8},
    {"kind":"relation","id":"rel_...","field":"metrics.band_delta_db",
      "before":20.3,"after":19.7}
  ],
  "added": [{"kind":"item","id":"{item-guid}"}],
  "removed": [{"kind":"fx","id":"fx:{owner}:{slot}"}],
  "invalidated": {"audio_ids":[...], "analysis_ids":[...], "relation_ids":[...],
                   "sources":[...], "reason":"render_dependency_changed",
                   "complete": true},
  "comparability": {"method_match": true, "window_match": true, "stage_match": true,
                     "verdict": "comparable"},
  "audio_evidence": {"state":"stale"|"current", ...},
  "advance": {"context_token":"ct_...", "guarantee":"..."}
}
```

Rules:

* Every `changed`/`added`/`removed` entry carries a stable id. No positional paths in a
  model-facing delta (G16).
* If `comparability.verdict != "comparable"`, no numeric `analysis`/`relation` deltas
  are emitted; the entries become `invalidated` with a reason
  (`method_changed`, `window_changed`, `stage_changed`, `identity_discontinuity`). Two
  captures measured with different `analysis_version` or over different windows are not
  a numeric comparison, and pretending otherwise is the "comparing mismatched stages"
  failure the guide already warns about (`MCP-README-LLM.md:88–89`).
* Mutation deltas keep the current id-keyed object facts and auto-advance; capture
  deltas adopt the same id-keyed shape. `delta_ack` stays internal; no response names
  it (G19).

### 6.5 Identity, units, method, validity, confidence

| field | rule | where it must appear |
|---|---|---|
| stable id | `kind:<guid>` (or `fx:owner:slot`, `route:source:send`, `envelope:owner:guid`) exactly once per object; no duplicate ids across domains | `entities`, every table row that owns an object, every query row, every delta entry |
| lineage | `capture_id` + `revision` + `record_integrity_sha256` + `document_sha256` + `fact_root`; a query echoes all five | binding of every response |
| unit | one per metric, one per series, one per relation metric, one per MIDI/envelope field | view `metric_units`/`units`; query `units` |
| method | `method` + `method_version` per metric family and per relation kind | view `metric_methods`/`relations.kinds`; query `metric_methods` |
| stage | the actual render stage of the measured signal, plus the measurement stage (`file_or_buffer`) separately | analysis row `stage_i` → `tracks.stages`; `metric_methods.stage` |
| window | `[start_s,end_s)` per measurement, with basis (`measured_section`, `capture_window`, `project`) | analysis row `window_i` → `windows`; series descriptor |
| validity | audio freshness (`current`/`stale`), capture validity (`capture_status`, `invalid_reasons`), coverage status | binding/evidence, coverage |
| confidence | per metric when the producer supplies it; otherwise `confidence_basis: not_produced` | analysis `uncertainty.confidence` |
| decimals | per metric, declared; a value that rounds to zero is listed or flagged | analysis `metric_decimals`/`rounded_to_zero` |

### 6.6 Byte/token arithmetic and ceilings

Token conversions, using the measured ~3.07 bytes/token cited in `panorama.py:53–55`
and the conservative 2.2 used by the view:

| bytes | tokens at 3.07 B/token | tokens at 4 B/token | tokens at 2.2 B/token |
|---|---|---|---|
| 42,864 (view) | 13,962 | 10,716 | 19,484 |
| 44,719 (envelope) | 14,566 | 11,180 | 20,327 |
| 48,000 (budget) | 15,635 | 12,000 | 21,818 |
| 77,000 (Claude Code 25k cap at 3.07) | 25,081 | 19,250 | 35,000 |
| 50,000 (Hermes char spill) | 16,287 | 12,500 | 22,727 |

Conclusions:

* The reference view fits every documented host threshold we found (Claude Code 25k
  tokens, Hermes 50k chars), with ~3.3 KB of headroom at 48 KB.
* The 24-metric matrix is **124 B/source**; 100 sources would be ~12.4 KB of metrics
  alone, but the surrounding per-source rows (track, entity, audio, rms stats, two level
  rows) add ~240 B, so the per-source cost is 0.36–0.95 KB, not 124 B.
* Shared material is **≈25.7 KB**: 42,864 B total minus 17,162 B of entity row arrays
  (tracks, items, takes, audio, analysis, rms stats, level rows, FX, routes, automation,
  envelopes, tempo). It contains entities 5,015; binding 2,184; reading 1,927; relations
  3,008; archive 1,713; project 1,135; audio contract 1,084; coverage 961; time 784;
  derived 508; reading inventory 362; units 311; transport 210; plus table legends and
  notes (~6.5 KB). That is ~60 % of the reference view; a larger project does not
  amortize it.
* Per-source cost is 0.36–0.95 KB (measured rows vs the controlled 42→43 difference).
  At 48 KB the reference has 5,136 B of headroom → **~5–14 more sources**, i.e. a
  ceiling of roughly **47–56 sources** at this item density `[ARITHMETIC]`.
* At 300 s the view grows +642 B (+1.5 %): duration is nearly free because series are
  not inline. At 300 s / 43 sources it is 44,410 B, still under 48 KB.
* A project with 100 sources at the reference item count is ~65–125 KB by the linear
  model (25.7 KB shared + 100×0.36–0.95 KB + 34×~0.11 KB); with 400 items it is
  ~110–170 KB — over budget, and the honest answer is `response_over_budget`, not a
  skeleton `[ARITHMETIC]`.

Exact-query ceilings at 48 KB:

| family | bytes | what fits |
|---|---|---|
| `analysis` (exact metrics) | 2,673 B/source | ~17 sources; all 42 = 112,265 B → refusal |
| `rms_400ms` | 44.4 B/cell | ~1,080 cells ≈ 108 s for one source; a 2-hour track needs `resolution_s` or an interval |
| `band_level` | ~291 B/cell (27.4 KB/s) | ~1.75 s at full resolution for one source; `resolution_s=0.5` gives ~17 s; coarser or narrower otherwise |
| `relations` | 496 B/edge + 334 B/pair | one track = 54,912 B → refusal; one track + kind + band fits; whole = 827,767 B → refusal |
| `items` / `takes` / `audio` | 43 / 26 / 50 B/row | all 34/34/42 fit (23 KB / 14 KB / 55 KB; audio all = 55,460 B → refusal at 48 KB) |
| `envelope_points` / `midi_notes` | ~40 B/point; ~30 B/note | small captures fit; a 10,000-note MIDI take is ~300 KB (estimate) → refusal |

### 6.7 Large project, many tracks, long track, many items

| situation | what the contract does | honest statement |
|---|---|---|
| 100–200 tracks | `response_over_budget` with per-section bytes, entity counts and the budget needed; no view, receipt, token or handle | The view cannot scale past ~47–56 sources at 48 KB because shared material (~25.7 KB) plus per-source rows exceed the budget. A host with a larger documented cap can pass `max_response_bytes`; the server must not assume one. |
| 5,000 items / 2,000 takes | refusal; the item/take tables are the second-largest cost (~110 B each, estimate) | A project with thousands of edits cannot be delivered as one inventory at 48 KB. This is the true ceiling of the no-pages contract and must be stated in the refusal, not hidden. |
| 2-hour single track | the view fits (duration is free); `series` refuses with `matching_points`, `point_limit` and `min_resolution_s_for_budget` | Exact series are duration-bound: ~1.36 M cells at 0.1 s for `rms_400ms` (~60 MB JSON, estimate), ~1.36 M × 28 for `band_level`. Only a resolution or interval selection can be delivered. |
| 10,000-note MIDI take | `midi_notes` detail entry is `truncated` at the capture cap; query can return up to the point limit | The capture cap must be visible in coverage and in the view; it must never be called complete. |
| no audio / no master | view still builds; `analysis` empty or unavailable; `temporal.levels` unavailable with reason | A silent/empty project is a valid panorama with explicit empty layers, not an error. |
| >2 channels | LUFS/LRA and stereo metrics are `None` with notes; `temporal_profile` is `unsupported_channel_layout` | The model must see the unsupported state, not a plausible number. |

### 6.8 What must never be done

* A lossy "spine", top-N, or "important metrics only" projection (owner-rejected,
  `ARCHAEOLOGY:79–82`).
* A resource link, URI, UI expander or client preload as the **only** path to a mandatory
  fact (`[SPEC]` S1–S2).
* A page, cursor, numbered chunk, or "fetch the rest" instruction (owner-rejected;
  `[SPEC]` S5 has no such mechanism for a tool result).
* Silent nulls or zeros for an unmeasured value. `null` + absent count, or an explicit
  `not_captured`/`not_produced` reason.
* A global rounding that turns a non-zero measurement into `0` without a flag.
* A declared FK that returns `-1` for a present object, or a declared column that is
  always empty.
* A catalog byte figure measured on a different object.
* A numeric delta across different methods, windows, stages or identities.
* A model-facing string naming a tool that is not on the surface.

---

## 7. Current-vs-target gap matrix

Severity: **S0** = the contract is false or data is unreachable; **S1** = a
decision-critical field/family is missing or misattributed; **S2** = provenance,
honesty or completeness metadata is wrong; **S3** = documentation drift.

| id | element | current (evidence) | target | sev |
|---|---|---|---|---|
| G1 | source attribution | `analysis.rows.source` and `audio.rows.source` are `-1` for all 42 rows; `source_index` keyed by raw GUID, rows carry stable ids (`panorama_view.py:265–269,463,531`; `panorama.py:923,1007`) | `entities.source` dictionary (or stable-id strings) and a test that every present FK resolves | **S0** |
| G2 | `temporal_profile` query | `TypeError: mappingproxy is not JSON serialisable` (`panorama_view.py:169–174`, `query.py:445`) | `MappingProxyType` handled in `panorama_view._json_default`; all three series round-trip in tests | **S0** |
| G3 | catalog bytes | `series`/`midi_notes`/`envelope_points` measure summary objects; `series` = `analysis` = 112,265 B; true series 27,138,014 B (`panorama_view.py:762–794,817`) | `exact_bytes` per family measured on the exact payload or labelled `estimate` with method | **S0** |
| G4 | `project_map` family | returns `document["project"]` (2,430 B); raw map is 228,348 B, unreachable (`query.py:294–295`; `panorama_view.py:774–783`) | rename to `project_summary`; implement a real `project_map` with object/interval selectors, or declare it unsupported | **S0** |
| G5 | take fields | `rate/pitch/stretch_n/fx_n` empty; canonical takes carry only 7 fields (`panorama_view.py:346–362`; `panorama.py:319–327`) | carry the fields from the raw take subtree; or remove the columns and name the live tool | **S1** |
| G6 | FX parameters | `fx.params` always `[]`; canonical rows have no `parameters`; `raw_parameters_pointer` points into a map no tool returns (`panorama_view.py:395–400`; `panorama.py:439–454`) | inline up to K params, `params_omitted`, and a working `fx_params` query; or remove the column and say where params live | **S1** |
| G7 | method/version | `analysis.method.version` is `null`; no producer writes `analysis_version` (`panorama.py:1840–1845`) | one `analysis_version` constant written into provenance; `metric_methods` per metric | **S2** |
| G8 | stage | `analysis.stage` = `"file_or_buffer"` for all rows; render stage only in `audio.stages` (`panorama.py:930`; `metrics.py:505`) | `stage_i` on the analysis row → `tracks.stages`; `metric_methods.stage` = measurement stage | **S2** |
| G9 | notes/confidence | view reports `measurement_notes: 7` only; `measure_audio` never sets `confidence` (`panorama_view.py:872`; `models.py:71`) | inline the deduped notes; `confidence_basis: not_produced` or per-metric confidence | **S2** |
| G10 | completeness | one `coverage.complete` + `delivery.fits`; no dimension names (`panorama_view.py:737`; `lifecycle.py:1111`) | four named dimensions with counts and reasons | **S1** |
| G11 | relation selectors | `kinds`/`bands_hz` filter edges only; pairs/unavailable stay whole (`query.py:162–174`); one track 54,912 B; one kind alone 314,955–809,134 B | selectors apply to pairs/unavailable too, or `include_pairs=false`; counts in the response | **S1** |
| G12 | relation kind registry | only kinds with edges listed; `phase_pair` unavailable, `partial_loudness_share` never emitted and absent (`panorama_view.py:670`; `panorama.py:1570–1573`) | all declared kinds with `status`, `reason`, caveats, `edges` (0 allowed) | **S1** |
| G13 | empty columns | 4 take columns, FX `params`, 2 source FKs; `takes.media` computed but not a column | no declared column that cannot be populated; `not_captured` reason where genuinely unavailable | **S1** |
| G14 | exact units/method | `analysis` query has no units; `selection.units` says "see analysis metric units" which the response lacks (`query.py:378`; `panorama_view.py:94–96`) | `units` + `metric_methods` in every query response | **S1** |
| G15 | `metrics` selector | advertised in `ARCHIVE_FAMILIES`; no parameter in `run_query` (`panorama_view.py:94–96`; `query.py:334–342`) | implement column filtering; test it | **S1** |
| G16 | capture-delta ops | `_diff_values` emits positional `/analysis/17/metrics/...` paths (`lifecycle.py:389–414`) | id-keyed ops; reorder must not create false changes | **S1** |
| G17 | delta comparability | no method/window/stage check; two incomparable captures can yield numeric before/after | `comparability` verdict; `not_comparable` forbids numeric metric deltas | **S1** |
| G18 | cross-capture metric delta | `stable_fact_view` includes analysis values but not method/window | include method/version/window in the comparison key | **S2** |
| G19 | removed tool names | `changes_since` `next_call` delta_ack (`lifecycle.py:1446`); details `fetch_tool`/contract (`details.py:32,255`); `reading_contract.linked_exact` (`panorama.py:2216–2221`); capacity `next_action` (`2495`) | no model-facing string names a removed tool; test scans responses | **S2** |
| G20 | `capture_read` capacity | fallback rebuilds with the same 700,000 B limit (`mcp_server.py:2588–2599`), so it raises on 1.35 MB | rebuild with a large in-memory cap (as `lifecycle._document` does), or return the view | **S0** |
| G21 | second representation | `reaper_capture_read` returns the canonical document (`mcp_server.py:2630`), 1.35 MB, with stale detail addresses | make it return the view + binding, or restrict it to archive tooling and remove it from the model surface | **S1** |
| G22 | rounding zeros | 39 values round to 0 at 3 decimals, including `dc_offset` (`panorama_view.py:57,195–208`) | per-metric decimals + `rounded_to_zero`/`min_abs_inline` | **S2** |
| G23 | series decimation | `step=ceil(len/20000)` with no `step`/`hop`/`window` in the series descriptor (`metrics.py:446–448,486–496`; `details.py:467–479`) | declare `window_s`, `hop_s`, `decimation_step`, `cell_fields` | **S2** |
| G24 | anchor basis | says `"stored anchor_s"` though no `anchor_s` exists (`panorama_view.py:586–601`; `panorama.py:851`) | `"first stored cell"` when `anchor_s` is absent | **S3** |
| G25 | duplicate envelope id | the same tempo-map envelope is listed under `automation` and `envelopes` with two ids and two identical detail refs (`panorama.py:2300–2311`; measured details) | one id per object, or `same_as`; dedupe the point query | **S2** |
| G26 | 2 ms event list | stored in `curves.events_2ms`, no query family/detail ref (`metrics.py:494`) | a `events` family or a stated "counts only" | **S2** |
| G27 | raw fields unreachable | group id, item notes, take stretch markers, record mode, phase/width are only in the raw map (228 KB) | implement the real `project_map` selector, or list the fields as live-read only | **S2** |
| G28 | point-limit refusal | returns limit + requested series, not matching points or the resolution that fits (`query.py:402–424`) | `matching_points`, `bytes_per_point`, `min_resolution_s_for_budget` | **S2** |
| G29 | gate order | `reaper_panorama_query` runs the query before the receipt check (`mcp_server.py:2747–2756`) | check the receipt first | **S3** |
| G30 | docs drift | canonical 1,349,027 vs 1,350,947; 8,633 vs 8,747; 730,117 vs crash; schema claim `FULL-CAPTURE.md:186`; ack claim `ARCHITECTURE.md:67–69`; key names `MCP-README:320–322`; relation bytes `MCP-README:307`; tool map 20 of 27 | update or label every figure with its selector/date | **S3** |
| G31 | EN/RU parity | RU 799 vs EN 761 lines; RU has extra sections and a stronger status claim (`ARCHAEOLOGY:377–386`) | either EN is the source of truth and RU is a faithful translation, or both are normative and linked | **S3** |
| G32 | budget declaration | 48 KB is a policy default; hosts differ (10k/25k tokens, 50k chars, per-tool token limits) | keep the default; always report `measured_bytes`, `max_response_bytes`, `fits`; document that it is a policy | **S3** |
| G33 | comprehension test | none was run; D6-10b required one (`ARCHAEOLOGY:226–235`) | a task-based comprehension check on the view (not a byte measurement) before calling the format settled | **S2** |

---

## 8. Sequenced implementation and acceptance plan

The view is a pure projection; none of P0–P3 requires rewriting an archive. Producer
changes (method version, notes, take/FX fields) change the canonical document hash, so
they need a `capture-panorama/3` schema bump and an in-memory upgrade for old sidecars —
the same pattern already used for `capture-panorama/1` (`panorama.py:48`, `mcp_server.py:2562–2600`).

### P0 — Correctness and honesty (no schema change)

Files: `context/panorama_view.py`, `context/query.py`, `context/details.py`,
`context/panorama.py`, `context/lifecycle.py`, `mcp_server.py`, `tests/`.

1. Fix `Entities.source_index` (key by stable id, keep GUIDs as aliases) or emit
   `source_id` in `analysis`/`audio`; add a test that no present FK is `-1`.
2. Add `MappingProxyType` to `panorama_view._json_default`; add a test that every
   advertised series name round-trips through `run_query`.
3. Fix the catalog: measure the exact payload per family or rename the column and mark
   estimates; never reuse `entities.analysis` for `series`.
4. Rename `project_map` → `project_summary`; either implement a real raw-map family with
   selectors or mark it unsupported in the catalog.
5. Remove/replace every reachable removed-tool string (`details.DETAIL_FETCH_TOOL`, the
   details contract prose, `reading_contract.linked_exact`, the capacity `next_action`,
   `changes_since`/`record_change` `next_call`).
6. Fix `capture_read`'s capacity fallback and decide whether it returns the view or
   stays archive-only.
7. Fix `anchor_basis`; dedupe the envelope id; fix the `dc_offset`/event-count unit
   strings.
8. Add per-metric decimals and a `rounded_to_zero`/`min_abs_inline` declaration.

Exit: the contract checker (§9.1) passes on the 42-source archive; 42/42 FKs resolve;
no removed-tool string in any response; the reference view is still ≤ 47 KB.

### P1 — View v3: completeness and provenance

Files: `panorama_view.py`, `panorama.py`, `analysis/metrics.py`, `models.py`, tests.

1. Add `completeness.{project_inventory, inline_metrics, archive_families,
   exact_delivery}` with counts and reasons.
2. Write `analysis_version` (e.g. `"metrics/1"`) into `MeasurementReport.provenance`;
   add `metric_methods` and `methods`; add `stage_i` to the analysis row.
3. Inline the deduped metric notes; add per-metric confidence only if produced, with
   `confidence_basis`.
4. Carry take `rate/pitch/stretch_n/fx_n/ch_mode/group` from the raw take subtree; add
   `media_ref`.
5. Add `fx.params` for `param_n <= K` with `params_omitted`; add a `fx_params` query.
6. List all declared relation kinds with status/reason; mark `partial_loudness_share`
   `not_measured_in_capture` unless it is implemented in the capture path.
7. Declare series `window_s`, `hop_s`, `decimation_step`, `cell_fields`.
8. Bump `VIEW_SCHEMA` to `/3`; keep the archive schema unchanged if possible; if the
   producer fields change the canonical document, bump `PANORAMA_SCHEMA_VERSION` to
   `/3` and upgrade `/2` in memory.

Exit: the view is **≤ 48,000 B at the default budget** on the reference (the whole point
of the contract); if the new fields cannot fit, move the named rounding list and any
other non-decision-critical field to the query path, or obtain an explicit owner
decision to raise the default. Every declared column has at least one non-null value on
a capture that has the object; all 24 metrics have a non-null method/version/stage/unit.

### P2 — Query v2

Files: `query.py`, `details.py`, `panorama_view.py`, `mcp_server.py`, tests.

1. Implement `metrics` (column filter) and make `units`/`metric_methods` present in
   every response.
2. Make `kinds`/`bands_hz` filter pairs and unavailable, or add `include_pairs=false`;
   return coverage counts without the repeated per-pair reasons unless requested.
3. Add `scope.matching_*`/`returned_*`/`complete`/`hash_verified`.
4. Add `matching_points`, `bytes_per_point`, `max_points_for_budget`,
   `min_resolution_s_for_budget` to the refusal.
5. Return the point-limit refusal with the same binding/selector/units as a success.
6. Implement `project_map` (if kept) and `fx_params`; otherwise remove them from the
   catalog.

Exit: a selector matrix test for every family × selector; every success reports
`returned == matching`; every refusal reports a measured size and no `data`.

### P3 — Delta v2

Files: `changes.py`, `lifecycle.py`, tests.

1. Replace positional capture-delta paths with id-keyed entries.
2. Add `comparability` and forbid numeric metric deltas when method/window/stage differ.
3. Include `analysis_version` and window in the comparison key.
4. Keep auto-advance; keep `delta_ack` internal; no response names it.

Exit: property test `apply(base, delta) == target` for a set of generated changes; a
reorder of unrelated objects produces no false metric changes; a stale base matrix
returns the right reason and no ops.

### P4 — Acceptance harness

Files: `tests/test_panorama_contract.py` (new), `tests/test_panorama_adversarial.py`
(new), a small `tests/contract_checks.py` helper.

1. A response checker that runs on every open/query/delta/refusal in tests:
   * `budget.measured_bytes == len(canonical(response))` where applicable;
   * `returned == matching` or `complete:false` + reason;
   * every `*_i`/`source` FK resolves or is `-1` with `present:false`;
   * every declared metric has a unit and a method;
   * no `data` on a refusal, no view/token/handle on `response_over_budget`;
   * no forbidden tool name.
2. Adversarial fixtures (generated records, no REAPER): 100 tracks/400 items; 2-hour
   single track; 10,000 MIDI notes; duplicate names; no master; no audio; mono and
   4-channel; missing metric; legacy sidecar; corrupt sidecar hash; budget 1 and budget
   10⁹; zero/negative interval; unknown object/kind/series/band.
3. A CI gate that runs the checker on the reference archive if it is present, or on a
   synthesized 42-source fixture otherwise.

Exit: the matrix below passes; the checker is part of the suite, not a one-off script.

### P5 — Documentation reconciliation

Update `DECISION-single-response-panorama.md`, `MCP-README-LLM.md`/`.ru.md`,
`ARCHITECTURE.md`/`.ru.md`, `VALIDATION.md`/`.ru.md`, `FULL-CAPTURE.md`,
`AI-MIX-SURFACE.md` (add a superseded banner). Every number gets a selector and a date;
every removed-tool name is deleted; the four dimensions and per-metric methods are
described only after they exist.

---

## 9. Acceptance

### 9.1 Harness-neutral invariants

These are assertions on the response object, independent of REAPER, MCP transport or
client:

| id | invariant | how tested |
|---|---|---|
| A1 | **Response completeness**: `open` has one row per track/master/item/take/FX/route/envelope/asset/analysis source, and `completeness.project_inventory.counts` equals the independently counted fixture | fixture builder + `len()` |
| A2 | **No hidden truncation**: every array with a declared count has `returned == count` or `truncated:true` + reason; a query has `scope.returned == scope.matching` or a refusal | checker |
| A3 | **Exact query whole-or-refusal**: for every family/selector, the returned set equals the independently filtered fixture set, or `status=="query_too_large"` with `measured_bytes > max` and no `data` | selector matrix |
| A4 | **No silent null/zero**: a removed metric is `null` + `absent` count, never `0`; a `0` that came from rounding is flagged by `min_abs_inline` or listed in the query's `rounded_to_zero` | mutation test |
| A5 | **Attribution**: every non-null `*_i`/`source` resolves to an entity whose stable id equals the row's canonical id; no `-1` for a present object | fixture |
| A6 | **Provenance**: 24 units, 24 method descriptors, non-null `method_version`, per-row stage/window indices resolvable | fixture |
| A7 | **Stable revision**: every response echoes the same `capture_id`/`revision`/`document_hash`/`fact_root`; a wrong one is `stale_context`/`stale_base` | stale matrix |
| A8 | **Delta application**: `apply(base_document, delta.changed)` equals the target document for the changed facts; no positional paths | property test |
| A9 | **Comparability**: a delta across different method/window/stage has `verdict: not_comparable` and no numeric metric entries | two-capture fixture |
| A10 | **Energy/byte honesty**: `delivery.response_bytes == len(canonical(response))`; `fits` is false exactly when the measured size exceeds the budget; a refusal mints nothing | boundary test |
| A11 | **No removed tools**: no response string matches `reaper_panorama_(page|detail|ack|slice|delta_ack)` | scan |
| A12 | **Self-description**: a query response alone (without the earlier view) contains units/method/window/stage for every returned number | query unit test |

### 9.2 Test matrix

| test | fixture | assertion |
|---|---|---|
| `test_reference_view_fits_and_counts` | archived 42-source capture, if present | view ≤ 48,000 B; 42 tracks; 24 metrics; no unmapped section |
| `test_view_has_no_empty_declared_column` | same | every column has ≥1 non-null on this capture, or its note says `not_captured` |
| `test_fk_resolves` | same | 0 unresolved FKs for present objects |
| `test_all_series_roundtrip` | same | `rms_400ms`, `band_level`, `temporal_profile` all `status=="query"`, values equal the record arrays |
| `test_analysis_query_carries_units_and_method` | same | `units` has 24 entries; `metric_methods` has 24; no null version |
| `test_kind_filter_narrows_pairs` | same | `kinds=[...]` response has only that kind's pairs, and its size is below the unfiltered one |
| `test_one_track_relations_refusal_is_measured` | same | one-track relations = `query_too_large`, `matching_rows`/`family_bytes` present, no `data` |
| `test_catalog_bytes_match_exact_payload` | same | for each family, `exact_bytes` equals the measured payload or is labelled `estimate` |
| `test_project_summary_is_not_raw_map` | same | `project_summary` returns the project block; `project_map` (if present) returns the 17-key raw map (~228 KB) or a measured refusal |
| `test_take_fields_present` | capture with takes | `rate/pitch/stretch_n/fx_n` non-null where the raw take has them |
| `test_fx_params_policy` | capture with FX | `params` non-empty for `param_n <= K`; `params_omitted` otherwise; `fx_params` returns them |
| `test_rounding_zero_flagged` | capture with `dc_offset=3.4e-05` | inline 0 is flagged by `min_abs_inline`, or `dc_offset` uses 6 decimals, or the query returns the exact 3.4e-05 |
| `test_open_budget_boundary` | any | size−1 → refusal, no view/receipt/token/handle; size → complete |
| `test_large_project_refusal` | 100 tracks/400 items | refusal with counts and `bytes_per_entity`; no skeleton/link |
| `test_long_track_series_refusal` | 2-hour track | refusal with `matching_points`, `point_limit`, `min_resolution_s_for_budget` |
| `test_missing_metric` | report without `integrated_lufs` | null + `absent` 1, never 0 |
| `test_changed_dependency` | pan edit fixture | delta names the source + master; `audio_evidence.state=="stale"`; successor token present |
| `test_stale_base_matrix` | wrong capture/revision/root/hash/identity | `stale_base` reason, no ops |
| `test_reorder_no_false_delta` | swapped unrelated tracks | no metric changes |
| `test_duplicate_envelope_one_id` | tempo map in both domains | one id or `same_as`; one point result |
| `test_no_removed_tool_strings` | all responses | scan |
| `test_legacy_sidecar_upgrade` | schema-1 sidecar | view built in memory; sidecar untouched; hash bound |
| `test_unknown_selectors` | unknown object/kind/series/band | `validation_error` naming allowed values |
| `test_empty_capture` | no audio | view complete with empty layers; no crash |

### 9.3 Adversarial matrix

| case | expected behaviour |
|---|---|
| 100 tracks / 400 items | `response_over_budget`; counts + per-section bytes; no view/receipt/token/handle |
| 2-hour single track | view fits; exact `rms_400ms` refuses with point math; `resolution_s=10` fits |
| 10,000-note MIDI | detail entry `truncated:true`; coverage says so; `midi_notes` query returns the point limit or refuses |
| duplicate track names | object selector by name is ambiguous → `validation_error` (the index only keeps unique names, `query.py:106–114`) |
| no master | `track_master_level_relation` listed `unavailable`; no crash |
| no audio | `analysis` empty; `temporal.levels` unavailable with reason; view still complete |
| mono / 4-channel | LUFS/stereo fields `None` + notes; `temporal_profile` unsupported; no invented downmix |
| missing metric | null + absent; exact query omits the key, not zero |
| changed dependency | delta invalidates the named sources; no numeric metric delta |
| stale base / wrong root / wrong revision / identity break | `stale_base` with reason; no ops; `refresh` is the next call |
| budget 1 / negative / huge | validation error for non-positive; measured refusal for 1; complete for huge |
| zero/negative/outside interval | validation error naming the interval |
| unknown object/kind/series/band | validation error with allowed values |
| legacy sidecar | in-memory upgrade; no rewrite; hash bound |
| corrupt sidecar | `panorama_binding_invalid`; no view |
| reorder unrelated objects | no false metric/relation deltas |

### 9.4 Live/host checks (not merge blockers)

These are recorded separately because they need a host or REAPER and cannot be asserted
in CI:

* one live Codex and one live Claude Code open of the reference view, recording the
  host's reported token count and whether it spilled/truncated;
* one live Hermes open, checking whether the 42,864 B view stays under the 50,000-char
  spill;
* one live `reaper_capture_read` on a 41-track capture after the P0 fix;
* one task-based comprehension check (the D6-10b requirement): can a model answer
  "which track has the highest crest factor", "what is the master LUFS", "which sources
  are missing LUFS", "what changed in track 5" from the view alone?

---

## 10. Documentation reconciliation list

| claim | location | evidence | action |
|---|---|---|---|
| canonical document 1,349,027 B | DECISION §1 table (50), §6 (165); VALIDATION:11 | measured 1,350,947 B today | update or label "as measured on 2026-11-13; rebuilds 1,350,947 today" |
| view 42,864 B / envelope 44,719 B | DECISION §6; VALIDATION:12–14 | exact today | keep |
| "48,000 B ... inside the Codex ~10,000-token output budget at ~4 bytes/token" | DECISION §6 (161–164) | 48,000 / 4 = 12,000 tokens; 15,635 at 3.07 B/token | correct the arithmetic or say "a 25,000-token cap at 3.07 B/token" |
| 8,633 B for `rms_400ms` 0–20 s | VALIDATION:17 | 8,747 B for `1. SubKick` (track id `{51FAF…}`) | name the track and the date, or re-measure |
| 730,117 B band/temporal selection | VALIDATION:18–19 | the same call now raises `TypeError`; band-only 0–20 s is ~547 KB | re-measure after G2, with the object scope |
| view schema `capture-panorama/2` | FULL-CAPTURE.md:186 | view is `panorama-view/2` | correct |
| "Acknowledging the exact delta advances the token" | ARCHITECTURE.md:67–69; .ru:60 | auto-advance (`lifecycle.py:1636–1653`) | rewrite |
| `model_interpretations` / `causal_hypotheses` keys | MCP-README:320–322 | not in the view; prose strings only | rewrite |
| relation kinds carry byte size | MCP-README:307 | no bytes in the kind object | remove or add |
| every project-target analysis requires an exact-capture receipt | MCP-README:401–402 | `reaper_analyze` has no `expected_capture_id` | correct the claim or add the check |
| tool map lists 20 tools | MCP-README:114–138 | 27 tools | complete the table |
| "only rolls, deltas and addresses into context" | AI-MIX-SURFACE.md:164–165 | superseded by the whole view | add a superseded banner |
| RU guide is a translation of EN | MCP-README-LLM.ru.md | 799 vs 761 lines; extra normative sections | reconcile or label both |
| "one representation" | DECISION §5 | `reaper_project` bootstrap + `reaper_capture_read` full document | declare both exceptions explicitly |

---

## 11. Owner decisions still open

These change the target contract and should not be decided by an implementer:

1. **Ceiling behaviour.** For a project over the budget, the current decision is
   refusal. Is a *voluntary* inventory scope (e.g. `objects=[folder subtree]`) ever
   acceptable as a task choice, or is any scoped inventory a forbidden completion
   mechanism? The round-7 owner clarification allows a semantic scope "only as a
   voluntary, meaningful choice for a specific task" (`LLM-MCP-NO-PAGES-DESIGN.md:27–35`),
   which suggests a scoped *query* is allowed but a scoped *panorama* is not. Confirm.
2. **FX parameters inline.** TZ §3 lists FX parameters in context; 19 params × 16 FX is
   ~12 KB. What is the inline threshold K, and is `param_n` + `fx_params` acceptable for
   larger chains?
3. **Take performance fields.** Are take `rate`/`pitch`/`stretch_n`/`ch_mode`/`group`
   mandatory inline for an editing task (EDITING-ACCEPTANCE §2), or may they be
   query-only?
4. **`partial_loudness_share`.** Should it be a capture relation (implemented in the
   capture path), or remain an `reaper_analyze`-only measurement outside the panorama?
5. **`reaper_capture_read`.** Should it return the compact view (and become a duplicate
   of `open`), or stay an archive tool outside the model surface?
6. **Default budget.** Keep 48,000 B, or lower it toward ~31 KB (the Claude Code
   10,000-token warning at ~3.07 B/token)? Hermes's 50,000 chars is a spill, not a hard
   cap. A lower default reduces the entity ceiling but reduces the chance of a silent
   spill; a higher one does the opposite.

---

## Appendix A — measured numbers and reproduction

Environment: Windows 11, Python 3.14.9, `PYTHONPATH=src`, no REAPER, no MCP server, no
pytest. Archive:
`work/benchmark/himmp/capture-archive/captures/cap_01M79659V69NDC5YNAA0SMPX51`
(41 tracks + master, 42 sources, 20 s window).

Reproduction sketch:

```python
from pathlib import Path
from reaper_audio.context.full_capture import CaptureArchive
from reaper_audio.context.panorama import build_panorama
from reaper_audio.context.panorama_view import build_view, view_bytes, canonical
from reaper_audio.context.query import run_query

root = Path("work/benchmark/himmp/capture-archive")
cap = "cap_01M79659V69NDC5YNAA0SMPX51"
rec = CaptureArchive(root).read(cap)
doc = build_panorama(rec, record_path=f"captures/{cap}/record.json",
                     max_bytes=10**12, max_tokens=10**12)
view = build_view(doc, record_path=f"captures/{cap}/record.json")
print(view_bytes(view), len(canonical(doc)))
```

Measured table (all `[MEASURED]`):

| quantity | value |
|---|---|
| canonical document | 1,350,947 B |
| view | 42,864 B |
| envelope (reconstructed) | 44,719 B |
| tracks incl. master | 42 |
| metric columns / units | 24 / 24 |
| absent metrics | 4,4,4,4,8 for LUFS, LRA, true peak, correlation, tempo |
| analysis rows with `source == -1` | 42 / 42 |
| audio rows with `source == -1` | 42 / 42 |
| relation edges / pairs / unavailable | 1,080 / 820 / 176 |
| relation kinds emitted | `band_dominance` 1,039; `track_master_level_relation` 41 |
| catalog series bytes / analysis bytes | 112,265 / 112,265 |
| exact series payload (all sources) | 27,138,014 B |
| raw project map | 228,348 B, 17 keys |
| 300 s view (42 sources) | 43,506 B |
| 300 s view (43 sources) | 44,410 B |
| values rounding to 0 at 3 decimals | 39 |

Limitations: the canonical document figure differs from the recorded 1,349,027 B by
1,920 B; the view and envelope match exactly. No lifecycle call was made, so no
receipt/handle/context/delta was minted and no archive file was written. The 43-source
capture is a different project with a different name set and one fewer FX; its +904 B
difference is not a clean per-source slope.

---

## Appendix B — sources and access dates

All external URLs were fetched on **2026-11-14** (host date, UTC+03:00).

| # | source | used for |
|---|---|---|
| 1 | https://modelcontextprotocol.io/specification/2026-07-28/server/tools | tool result content types, `structuredContent`, `outputSchema`, `resource_link`, stateful-tool guidance, error handling |
| 2 | https://modelcontextprotocol.io/specification/2026-07-28/server/resources | resources are application-driven; `resources/read`; resource contents/annotations |
| 3 | https://modelcontextprotocol.io/specification/2026-07-28/basic/index | stateless protocol; explicit identifiers for cross-call state; result types |
| 4 | https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/pagination | pagination is for list operations; opaque cursors; no page for a tool result |
| 5 | https://code.claude.com/docs/en/mcp-configuration | 10,000-token warning; 25,000-token default; `MAX_MCP_OUTPUT_TOKENS` |
| 6 | https://code.claude.com/docs/en/mcp.md | 50,000-character file spill; `anthropic/maxResultSizeChars` |
| 7 | https://www.codex-docs.com/en/docs/config-file/config-reference | `mcp_servers.<id>.tools.<tool>.output_token_limit`; per-tool output budget |
| 8 | https://www.codex-docs.com/en/docs/extend/mcp | server `instructions`; first 512 characters self-contained |
| 9 | https://www.anthropic.com/research/code-execution-with-mcp | intermediate tool results consume tokens; code-execution pattern |
| 10 | https://www.anthropic.com/research/effective-context-engineering-for-ai-agents | context is a finite, curated budget; compaction/clearing |
| 11 | Hermes local install: `skills/autonomous-ai-agents/hermes-agent/references/context-assembly-and-tool-budgets.md` | 50,000-char spill; 1,500-char preview; `read_resource` is an ordinary tool result; subscriptions do not update the view |
| 12 | https://hermes-agent.nousresearch.com/docs/user-guide/configuration | `tool_output.max_bytes: 50000` terminal cap; `max_lines`/`max_line_length` (search-result snippet; page body not fully extracted) |
| 13 | `docs/research/round-7/LLM-MCP-NO-PAGES-DESIGN.md` | P1–P4 design; host research; byte arithmetic (used as a prior, checked against code) |
| 14 | `docs/research/round-7/ARCHAEOLOGY-REQUIREMENTS.md` | owner-requirement table; contradictions; prior measurements |
| 15 | `docs/DECISION-single-response-panorama.md` | the adopted contract and its audit |

---

## Appendix C — code path index

| file | lines | what |
|---|---|---|
| `src/reaper_audio/context/panorama.py` | 45–91 | schema, budgets, 24 metric keys/units |
| | 291–328 | canonical take projection (7 fields) |
| | 421–461 | canonical FX projection (params dropped, pointer) |
| | 791–867 | activity grid (24 buckets, stable-id cells) |
| | 870–956 | canonical analysis row (metrics, stage, window, rms stats, series, uncertainty) |
| | 1274–1408 | relation view (kind metadata, edges, pairs, coverage) |
| | 1491–1624 | relation domain (2 emitted kinds, phase/reference unavailable) |
| | 1627–2500 | document build, analysis contract, notes, epistemic layers, capacity error |
| `src/reaper_audio/context/panorama_view.py` | 46–99 | view schema, default budget, decimals, archive families |
| | 102–162 | section inventory and derived sections |
| | 169–174 | `_json_default` (missing `MappingProxyType`) |
| | 242–281 | `Entities` and `source_index` (raw GUID keys) |
| | 288–490 | tracks/items/takes/fx/routes/envelopes/audio tables |
| | 497–576 | analysis table |
| | 579–662 | temporal grid/levels |
| | 665–715 | relations view |
| | 718–750 | coverage |
| | 753–844 | archive catalog and exact block |
| | 847–887 | reading block |
| | 907–1035 | `build_view` |
| `src/reaper_audio/context/query.py` | 155–193 | relation filtering (edges only) |
| | 291–331 | family filters (`project_map` → project block) |
| | 334–492 | `run_query`, detail path, budget/refusal |
| `src/reaper_audio/context/details.py` | 31–35 | detail schema, fetch tool name, point limit |
| | 240–275 | details document and contract prose |
| | 467–479 | series descriptor (no window/hop/step) |
| | 560–657 | series resolution, point cap |
| | 697–763 | `resolve_details` |
| `src/reaper_audio/context/lifecycle.py` | 233–341 | stable fact view, fact root, document hash |
| | 366–414 | revision comparison, positional diff |
| | 1067–1182 | `single` (measure, mint, refuse) |
| | 1184–1238 | `query` |
| | 1240–1480 | `changes_since`, stale reasons, auto-advance |
| | 1527–1662 | `record_change` |
| | 2249–2406 | `require_receipt` |
| | 2409–2472 | `_over_budget` |
| `src/reaper_audio/mcp_server.py` | 88–163 | receipt gates |
| | 178–203 | server instructions |
| | 808–887 | `reaper_project` (bootstrap read) |
| | 2535–2651 | `_read_capture_panorama` (capacity fallback) |
| | 2681–2805 | open/query/status/changes/refresh |
| `src/reaper_audio/analysis/metrics.py` | 18–23 | 28 bands |
| | 334–526 | `measure_audio`; 24 metrics, curves, provenance |
| | 446–448 | series decimation |
| `src/reaper_audio/models.py` | 67–72 | `MetricValue` (unit, confidence, note) |
| `src/reaper_audio/context/relations.py` | 27–45 | declared kinds, mandatory kinds, method versions |
| | 608–626 | relation payload |
| `src/reaper_audio/analysis/relations.py` | 374–585, 834–947, 948–1000 | band dominance, track/master, reference candidates |
| `src/reaper_audio/analysis/bundle.py` | 357–372 | separate analyze-path families (partial loudness, phase, corridors) |
| `src/reaper_audio/context/full_capture.py` | 120 | deep `MappingProxyType` freeze |
| | 1499–1524 | audio/analysis assembly |
| `scripts/reaper_bridge.lua` | 1145, 1652 | project digest, capture render |
| `tests/test_single_view.py` | 40–318 | current single-view tests (no FK assertion) |
| `tests/test_panorama_lifecycle.py` | 89–487 | legacy page/ack/slice tests still present |
