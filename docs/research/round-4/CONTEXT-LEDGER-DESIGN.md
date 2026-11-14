# CONTEXT-LEDGER-DESIGN — append-only project evidence ledger and model views

> **Historical record (superseded transport).** This document describes the
> paged panorama contract that has since been replaced by one whole-view result.
> Current behaviour: `docs/DECISION-single-response-panorama.md` and
> `docs/MCP-README-LLM.md`. Treat this file as history, not instruction.


**Status: DESIGN PROPOSAL ONLY (Wave D, 2026-11-12).**
No ledger store, journal file, dependency resolver, view builder, or ledger MCP tool is
implemented by this document. The only code change shipped alongside it is the
`reaper_analyze` pass-through for `tail_ms`, `channels`, and `sample_rate`
(`src/reaper_audio/mcp_server.py`, `tests/test_wave_d_mcp_analyze.py`). Everything below
labelled "proposed" does not exist yet and must not be reported as a working feature.

This proposal follows `docs/research/round-3/R3-05-CONTEXT-GRAPH-AND-MCP.md` and the
Wave D acceptance text in
`docs/research/round-4/WAVE-D-AND-HIMMP-AB-EXECUTION-DISPATCH.md` §"Context-ledger /
model-interface acceptance". It audits the code at HEAD `86b14bb` plus the dirty Wave A–D
tree; line references are to that state. The tree already contains uncommitted Wave B/C
modules (`context/`, `mix/`, `timebase.py`), so this design treats them as the current
contract surface, not as released API.

Normative words: **MUST**, **MUST NOT**, **SHOULD** describe the proposed contract.
**EXISTS** marks behaviour present in the checkout today.

---

## 1. What exists today, and what is missing

| Area | Source | EXISTS today | Gap this proposal fills |
|---|---|---|---|
| Record envelope | `src/reaper_audio/context/schema.py:142-212` | `RecordEnvelope(record_kind, record_id, schema_version=1, revision, created_at, producer)`; strict `SchemaVersionError` for any other version | No `seq`, `dep_set`, `supersedes`, payload hash/URI, idempotency key, or journal position |
| Record kinds | `context/schema.py:26-51` | 23 `RecordKind` values: project, revision, capture, asset, measurement, observation, annotation, event, occurrence, gesture, score, time_map, rhythm_map, section, riff, claim, hypothesis, intent, plan, operation, change, verification, delivery | No segment/snapshot/view/edge/undo kinds; no distinction between a claim and its lifecycle transitions |
| Revision identity | `context/schema.py:74-139`, `context/revision.py:150-262` | `RevisionRef(project_id, instance_id, epoch, counter, fingerprint, fingerprint_scope)`; `same_identity`, `same_content`, placeholder fingerprints | No `rev_`/`pri_` record IDs; `epoch` assigned nowhere in the record layer |
| Typed IDs | `context/ids.py:43-117`, `:200-303` | Registered prefixes for project/instance/revision, track/item/take/source/FX/send/envelope/time/section, asset/capture, observation/claim/hypothesis/plan/operation/change/verification/delivery/reference; authored ULIDs vs derived SHA-256 IDs | No ID for journal segments, snapshots, views, edges, undo attempts, or measurement methods |
| Audio evidence | `context/audio.py:29-136`, `:312-473`, `:492-674`, `:675-810`, `:818-946` | `Stage`, `CaptureMethod`, `ChannelView`, `AudioAsset`, `SignalWindow`, `CaptureRequest`, `Capture`; stage/method legality; content-addressed `ast_sha256_<64hex>`; tail only widens the file window | No store tying these records together; no lease owner record; no cross-record dependency set |
| Listener evidence | `context/audio.py:955-1006`, `:1009-1202` | `DeliveryEvidence.heard` is true only for `forwarded_verified`; `ListenResult` refuses empty text unless delivery is unsupported/failed | No ledger append path; no project-level "audio was actually delivered" gate outside a single record |
| Freshness / coverage | `context/revision.py:80-124`, `:545-696`, `:248-262` | Digest schema v1; required fields tracks/items/takes/fx/sends/tempo/master/automation/project/fx_state; unbacked coverage becomes `missing`; `is_stale()` accepts `dep_set` but **ignores it** | No entity-level dependency sets, no transitive invalidation, no persisted stale/contested state |
| Mix evidence chain | `src/reaper_audio/mix/models.py:925-1062`, `:1271-1398`, `:1479-1587`, `:1590-1667` | `MixObservation → MixClaim → IssueHypothesis → PostChangeCheck`; inferred claims require ≥2 alternatives; verified/failed checks require the observed metric; evidence grades A–F | No ledger IDs linking these records to captures/assets/operations; no append-only lifecycle |
| Artifact ownership | `src/reaper_audio/analysis/artifacts.py:18`, `:48-260` | Per-job directory, `.reaper-audio-job.json` manifest with `owner`, `job_id`, `artifact_names`, `assets[{name,bytes,sha256,mime}]`; `retain()` for bounded leases; cleanup only manifest-registered names | No project/instance namespace in the manifest; no baseline/final manifest class distinct from temporary jobs |
| MCP surface | `src/reaper_audio/mcp_server.py:1152-1230`; `tests/test_mcp_server.py:104-120` | 19 tools and 6 resource endpoints (1 static + 5 templates); in-memory `_analysis_records` with 1 h TTL, role replacement, manifest-checked audio resources | No ledger read/write tools or resources; analysis reports are ephemeral and not append-only |
| Analyze windows | `src/reaper_audio/daw/session.py:934-1004`, `:1170-1250`; `mcp_server.py:382-419`, `:623-793` | `analyze()` supports `tail_ms/channels/sample_rate`; per-source `analysis_window`; top-level `tail_s` and `capture_time_window`; MCP forwards the three options with strict wire ints (booleans rejected) and emits `analysis_windows` | Not a ledger; the report is still an in-memory lease with no durable revision-linked identity |

**Source-count note (owned by Hermes, not edited here):** the live server registers
19 tools and 6 resource endpoints (`1` static + `5` templates). `README.md:117`,
`docs/AI-INTERFACE.ru.md:200`, `docs/MCP-README-LLM.ru.md:4`, and `docs/STATUS.ru.md:44`
still say 12 tools; `docs/MCP-README-LLM.md:3`, `docs/VALIDATION.md:10`, and
`docs/GAP-ANALYSIS.md:21` say 19. This document uses 19/6 as the observed surface and does
not change those files.

---

## 2. Ledger model

### 2.1 Append-only journal

The ledger is a per-project append-only sequence of **records**. A record is never edited
or deleted. A correction is a new record that names what it `supersedes` or `retracts`;
a view derives current state from the sequence and the supersession edges.

Proposed envelope (extends `RecordEnvelope`, keeps every existing field):

```json
{
  "ledger_format": 1,
  "record_schema_version": 1,
  "record_kind": "measurement",
  "record_id": "mea_01J...",
  "seq": 184,
  "project_id": "prj_01J...",
  "instance_id": "pri_01J...",
  "revision": {
    "project_id": "...", "instance_id": "...", "epoch": 2, "counter": 417,
    "fingerprint": "sha256:...", "fingerprint_scope": "full"
  },
  "created_at": "2026-11-12T18:04:11.238Z",
  "producer": "reaper-workbench/0.2.0 analysis-bundle/1",
  "request_id": "req_...",
  "dep_set": ["rev_...", "trk_...", "itm_...", "ast_sha256_..."],
  "scope_set": ["tracks", "items", "tempo"],
  "supersedes": null,
  "payload_sha256": "sha256:...",
  "payload": { "...": "compact summary, bounded" },
  "payload_uri": null
}
```

Rules:

1. **MUST** assign `seq` monotonically per `(instance_id, project_id)`; gaps are allowed
   only for aborted appends and are recorded in the next segment header, never reused.
2. **MUST** treat `(record_kind, record_id)` as unique. Re-appending the same
   `request_id` returns the existing record (`idempotent_replay=true`) instead of
   creating a second one. Derived IDs are already reproducible
   (`context/ids.py:263-303`), so a re-run of the same method/revision/params is a
   natural no-op.
3. **MUST NOT** mutate `payload` in place. Status changes (claim confirmed/refuted,
   operation undone, asset expired) are separate `change`/`verification` records that
   supersede or annotate the original.
4. **MUST** store a hash of the canonical payload and, for payloads over the inline
   budget, a URI to a manifest-owned blob. The ledger never stores audio bytes or dense
   time series inline.
5. **MUST** reject a record whose `project_id`/`instance_id` does not match the journal
   it is appended to. Cross-project references are explicit `ref_` edges, never silent.
6. **MUST** validate `revision` with `RevisionRef` and refuse `same_content` claims for
   coarse/placeholder revisions (`context/revision.py:220-232`).

Storage is deliberately not fixed here. Two viable backends:

* **JSONL segments + fsync** (`ledger-000001.jsonl`, header record per segment): simplest
  to audit and diff; needs an append lock file on Windows.
* **SQLite WAL** with one table `records(seq, kind, id, revision_fingerprint, dep_set,
  payload_sha256, payload_json, payload_uri)` and an index on `(project_id, kind,
  record_id)`: stronger concurrent reads and pagination; the journal remains the source
  of truth if the DB is rebuilt from JSONL.

Recommendation: JSONL as durable truth, SQLite as a derived index. This keeps the
append-only artifact readable without the tool and lets a corrupt index be rebuilt.

### 2.2 Versioning

Two independent versions, because they change for different reasons:

| Version | Scope | Rule |
|---|---|---|
| `ledger_format` | envelope, journal, cursor, resource paths | **MUST** refuse unknown values (`SchemaVersionError`, `context/errors.py:78-81`). A reader that only knows 1 does not guess at 2. |
| `record_schema_version` | one record kind's `payload` | Additive fields are allowed inside a major version; removing/renaming/retyping a field bumps the major and requires an explicit migration record. |

`context/schema.py:21` currently freezes `SCHEMA_VERSION = 1` for all envelopes. The
proposal keeps that value for existing payloads and adds `ledger_format` beside it; a
future envelope shape change bumps `SCHEMA_VERSION` to 2 with a dual reader, never a
silent reinterpretation. A record kind may carry its own `method_version` (measurement,
derivation), and that version is part of the derived ID.

**Migration records.** A schema migration appends a `ledger_migration` record naming
`from_version`, `to_version`, the affected record IDs and a hash of the migration script.
Old records are not rewritten. Views apply compatible migrations on read; a reader that
cannot apply one returns `schema_version_unsupported` with the affected IDs.

### 2.3 Stable typed IDs

IDs extend the existing registry in `context/ids.py`, not a second scheme.

| Layer | Prefix | Kind | Identity rule |
|---|---|---|---|
| Project | `prj_` | project | authored ULID; path is metadata, not identity |
| Instance | `pri_` | project instance (one REAPER process/project pair) | authored ULID; `(instance_id, project_id)` is the isolation key |
| Revision | `rev_` | revision record | derived from `project_id + epoch + counter + fingerprint` (deterministic) |
| Track/bus/master | `trk_`, `trk_master_` | object | REAPER GUID; index only a hint (`ObjectRef`, `context/audio.py:312-354`) |
| Item/take/source | `itm_`, `tak_`, `src_` | object | GUID; take falls back to `src_` content hash only when REAPER reports no GUID |
| FX | `fx_` | object | track GUID + slot index + FX GUID/name; slot moves append a new edge, not a new identity |
| Send/route | `snd_` | edge | source GUID + destination GUID + send index |
| Envelope | `env_` | object | owning object GUID + envelope kind + index |
| Time map / section | `tm_`, `sec_` | derived | tempo-map digest / musical span natural key + revision |
| Asset | `ast_sha256_<64hex>` | content | SHA-256 of bytes (already enforced in `context/audio.py:524-529`) |
| Capture | `cap_` | authored | one capture run; groups assets |
| Window | no new ID | value | `SignalWindow` embedded in the asset/measurement record |
| Measurement/event/occurrence/gesture | `mea_`, `evt_`, `occ_`, `peg_` | derived | natural key + `method_version` + revision fingerprint |
| Listener note | `obs_` | authored | one listener result (`ListenResult`, `context/audio.py:1072-1202`) |
| Claim/hypothesis | `clm_`, `hyp_`, `hypgrp_` | authored | one assertion + its evidence set |
| Operation/plan/change | `op_`, `pln_`, `chg_` | authored | one proposed/applied batch; `chg_` is the state delta |
| Verification | `ver_` | authored | one post-change check (`PostChangeCheck`, `mix/models.py:1590`) |
| Reference/delivery | `ref_`, `dlv_` | authored | one reference input / one delivery attempt |
| Journal segment/snapshot | `seg_`, `snap_` | authored | storage bookkeeping only |
| View/edge | `view_`, `edge_` | derived | deterministic from view spec or `(from, relation, to)` |

Rules:

* An ID is **never** an array index, a track name, a file path, or a model sentence.
* `parse_id`/`validate_id` remain the only parsers (`context/ids.py:313-373`); new
  prefixes must be registered before use, and `ast_sha256` stays content-addressed.
* A derived ID **MUST NOT** include mutable presentation text; it includes the natural
  key, method version and revision fingerprint, exactly as `derive_id` does today.
* A record that references another record stores the typed ID, not a copy of its
  payload. This is what makes dependency invalidation possible.

### 2.4 Graph shape

Nodes are records; edges are first-class records (`edge_`) with
`from_id`, `relation`, `to_id`, optional `qualifiers`. Proposed relations:

```
contains            project -> track/master
has_take            item -> take
uses_source         take -> source
inserts_fx          track -> fx (slot, enabled, order)
routes_to           track -> bus/master (send index, gain, pre/post)
automates           envelope -> parameter target
spans               asset/measurement/event -> time map/section/occurrence
derived_from        measurement/event/claim -> capture/asset/other evidence
supports            observation/measurement -> claim
contradicts         observation/measurement -> claim
proposes            hypothesis -> operation
applied_as          operation -> change
verified_by         change -> verification
supersedes          any record -> earlier record
retracts            authored record -> earlier authored record
belongs_to          any record -> project/instance
```

The required traversal is fixed:

```
project -> instance -> revision
revision -> track/item/take/FX/route/time-map
asset/window -> measurement/event/listener-note
measurement/event/listener-note -> claim/hypothesis
claim/hypothesis -> operation/state-delta
operation/state-delta -> verification/undo
```

Every arrow is an explicit edge record. A claim that cannot be walked back to an asset
or a measured observation is `F_unsupported` (`mix/models.py:124-139`), not a softer
claim.

### 2.5 Payload contracts by layer

| Layer | Record kind | Required payload | Reuses |
|---|---|---|---|
| Project/instance | `project`, `ledger_instance` | project id, path, name, instance id, process start, REAPER version | `ProjectRevision` (`daw/session.py:41-54`), bridge session id |
| Revision | `revision` | `RevisionRef` + digest coverage (`covered`, `missing`, `scope`) | `RevisionRef`, `DigestEvidence` (`context/revision.py:545-696`) |
| Object | `object` | `ObjectRef` (kind, guid, parent, index hint), name, role, fader/pan/mute/solo, FX/send counts, hidden flag | `ObjectRef`, snapshot rows |
| Time | `time_map`, `section`, `occurrence` | `TimeMap`/`TimeRef` values, mapping status, uncertainty, tempo points | `timebase.TimeRef/TimeMap` |
| Capture/asset | `capture`, `asset` | `Capture` fields + `AudioAsset` fields + `SignalWindow` + lease id/expiry/status | `Capture`, `AudioAsset`, `SignalWindow` |
| Measurement | `measurement` | metric name/value/unit, method + version, params hash, stage, channel view, window, revision, uncertainty, coverage | `MetricReading` (`mix/models.py:759`) |
| Event/listener | `event`, `observation` | event kind/time/confidence, or `ListenResult` text + `DeliveryEvidence` + listener identity | `ListenResult`, `DeliveryEvidence` |
| Claim/hypothesis | `claim`, `hypothesis` | statement, class, evidence IDs, grade, method, confidence, alternatives, status | `MixClaim`, `IssueHypothesis` |
| Intent | `intent` | owner statement, source, constraints | `IntentRef` (`mix/models.py:1230`) |
| Operation | `plan`, `operation`, `change` | typed operations, expected revision, actual revision before/after, touched IDs, side-effect footprint, reversibility class, undo handle, state delta | edit plan (`daw/session.py:57-79`) plus new fields |
| Verification/undo | `verification`, `undo` | check ID, claim/operation ID, expected direction, observed metric, status, evidence IDs, restored fingerprint | `PostChangeCheck` |
| Delivery/reference | `delivery`, `reference` | status (`not_requested/uri_only/forwarded_verified/...`), checked_at, method, canary; reference path + window | `DeliveryEvidence`, `_reference_requests` |

All payloads are JSON with `allow_nan=False` and sorted keys, matching
`dumps_record`/`dumps_audio_record`/`dumps_mix_record`.

---

## 3. Dependency sets and invalidation

### 3.1 Two dependency axes

1. **`dep_set` — exact record IDs.** The measurement of track `trk_A` at revision
   `rev_7` depends on `rev_7`, `trk_A`, the item/take/source it measured, the capture
   and asset. If any of those records is superseded or its object changes, the
   measurement is stale.
2. **`scope_set` — coarse measured groups.** Names come from the digest contract:
   `tracks`, `items`, `takes`, `fx`, `sends`, `tempo`, `master`, `automation`,
   `project`, `fx_state` (`context/revision.py:80-83`). A claim that says "the low end
   is muddy across the mix" declares `["tracks", "master", "tempo"]`; it cannot survive
   a digest that never measured `master` or `automation`.

A record **MUST** carry both when it makes a claim about project content. A record with
an empty `dep_set` and no `scope_set` is an annotation (`E_annotation`) and cannot
support an A/B-grade conclusion.

### 3.2 Invalidation algorithm

On append of a new revision record:

1. Compare `same_content(old_revision, new_revision)` (`context/revision.py:220-232`).
   Same content and same identity: no invalidation, even if `counter` moved.
2. Different content: compute `changed_groups` from the digest's changed fields and the
   changed object IDs from the snapshot diff. Coarse/placeholder revisions invalidate
   every record whose `fingerprint_scope` was not full.
3. Mark a record **stale** when `dep_set ∩ changed_ids` is non-empty **or**
   `scope_set ∩ changed_groups` is non-empty.
4. Propagate along `derived_from`, `supports`, `proposes`, `applied_as`, `verified_by`
   edges with a cycle-safe visited set. A claim is stale if any evidence record is
   stale; a hypothesis is stale if its supporting claim is stale; an operation preview
   is invalid if its expected revision is stale; a verification is stale if the change
   it verifies is stale.
5. Persist the invalidation as an appended `change` record
   (`reason="dependency_changed"`, `changed_ids`, `changed_groups`), not by mutating
   the original. A view renders the original with `status="stale"` and a pointer to the
   invalidation record.

`is_stale(report_revision, current_revision, dep_set)` already accepts the dependency
set and documents that it is ignored (`context/revision.py:248-262`). The proposal
activates it without changing the signature: first compare content, then consult the
dependency index. This is the smallest compatible migration point.

### 3.3 Truth states

Every view item exposes one of:

| State | Meaning | Effect on claims |
|---|---|---|
| `fresh` | revision content unchanged and all deps fresh | usable at recorded grade |
| `stale` | a dependency changed or a newer revision exists | cannot be asserted as current; must be re-measured or explicitly historical |
| `partial` | capture/digest coverage incomplete (`missing` non-empty) | maximum grade capped at D/annotation for the missing scope |
| `contested` | two live records disagree on the same natural key | both retained; claim status `contested`; no automatic winner |
| `unavailable` | bytes/lease/resource gone | evidence ID remains, grade drops to F; no re-hash invention |
| `not_heard` | a URI was sent but delivery not verified | listener claims are C or unsupported, never B |

`partial` and `stale` are not errors; they are visible states. The model may still read
historical evidence, but a claim record **MUST** state the state it was built from.

### 3.4 Conflicts

Two measurements of the same metric/window/revision with different values are both
appended. The view groups by `(metric, scope, window, revision_fingerprint)` and returns
`contested=true`, the list of values, and the method/uncertainty of each. No averaging,
no "latest wins". A claim citing only one of them **MUST** carry the alternative group
or be marked `not_checked`. This mirrors the existing rule that an inferred claim needs
≥2 alternatives (`mix/models.py:1330-1337`).

---

## 4. Asset ownership, retention, separation

### 4.1 Ledger never owns audio bytes

The ledger stores `asset_id` (`ast_sha256_<64hex>`), `uri`, `bytes`, `sha256`, `mime`,
`lease_id`, `lease_expires_at`, and `status` (`available/released/expired/missing`,
`context/audio.py:119`). Bytes live only in an `AudioArtifactWorkspace` job directory
with a `.reaper-audio-job.json` manifest (`analysis/artifacts.py:18`, `:48-260`).

Dereference order **MUST** be: ledger record → project/instance ownership check →
manifest owner/job check → file is a regular non-link direct child → SHA-256 matches the
recorded hash → size matches → read. A hash mismatch is an integrity error, not a new
asset. The existing MCP resource path already performs most of this
(`mcp_server.py:1152-1230`); the ledger adds the project/instance and hash checks.

### 4.2 Ownership namespaces

```
<artifact-root>/<instance_id>/<project_id>/<role>/job-<job_id>/
```

* The manifest gains `instance_id`, `project_id`, `role`, and `source_revision`.
* A job is owned by exactly one `(instance_id, project_id, role)`.
* Content hashes may be deduplicated **for reading**, but a lease is never shared across
  projects: deleting project A's lease must not remove a byte project B still references.
* Cross-project `ref_` records are allowed only for references/analysis of external
  files; they carry their own path/window and never inherit a project lease.

### 4.3 Retention classes

| Role | Purpose | Default TTL | Deletion rule |
|---|---|---|---|
| `temporary` | per-analysis listening/measurement | 1 h (current `_ANALYSIS_TTL_S`) | role replacement or TTL; manifest-owned only |
| `baseline` | frozen evidence for a revision (pre-change fingerprint, A/B baseline) | project lifetime | explicit release; cannot be replaced silently |
| `final` | frozen deliverable/master | project lifetime | explicit release only; hash recorded in ledger |
| `reference` | external reference file/window | project lifetime | user-owned; never copied into the project |

Deleting bytes does **not** delete ledger records. It appends an `asset` lifecycle
record with `status="expired"`/`"released"` and the timestamp. Dependent measurements
become `unavailable` in views; they keep their numbers and their hash for audit, but
cannot be presented as current listening evidence.

### 4.4 Separation and safety

* Paths are resolved and must remain direct children of the role directory; reject
  symlinks, junctions, `..`, absolute escapes, and unregistered names (already enforced
  in `analysis/artifacts.py:158-212`).
* The manifest must say `owner="reaper-audio-library"` and the expected `job_id`.
* The ledger index is per `(instance_id, project_id)`; a query for project A never scans
  project B's journal.
* Two REAPER instances with the same project path have different `instance_id`s. A
  request without `instance_id` when more than one matches **MUST** fail with
  `instance_ambiguous` and list candidates; it must not pick the first.
* `reaper_release` releases one analysis lease; it must never delete another project's
  or another instance's job.

---

## 5. Views and context assembly

### 5.1 Tier-0 project brief (≤ 4,000 tokens)

One deterministic JSON object per `(instance_id, project_id, revision_fingerprint)`.
Budget is measured on the serialized UTF-8 JSON with a pinned estimator; the builder
**MUST** stop at 3,800 estimated tokens and reserve 200 for the wrapper. It never
truncates silently: every section reports `returned`, `total`, `truncated`, and a
`next` URI.

Proposed shape:

```json
{
  "ledger_format": 1,
  "view": "tier0",
  "project": {"project_id": "prj_...", "instance_id": "pri_...", "name": "In Solitude",
              "path": "C:/.../mix.RPP", "reaper_version": "7.84"},
  "revision": {"fingerprint": "sha256:...", "scope": "full", "counter": 417,
               "covered": ["tracks","items","takes","fx","sends","tempo","master",
                           "automation","project","fx_state"], "missing": []},
  "structure": {"track_count": 24, "bus_count": 4, "master_present": true,
                "hidden_track_count": 2, "tempo_map": {"point_count": 3,
                "meter": "4/4", "status": "measured"}},
  "tracks": [{"id": "trk_...", "name": "Kick", "role": "drums", "pan": 0.0,
              "fader_db": -3.2, "mute": false, "solo": false, "hidden": false,
              "fx_count": 2, "send_count": 1, "uri": "reaper://ledger/.../tracks/trk_..."}],
  "routing": {"bus_count": 4, "orphan_track_count": 0,
              "summary_uri": "reaper://ledger/.../views/routing"},
  "evidence": {"fresh": 12, "stale": 3, "partial": 1, "contested": 0,
               "assets_available": 8, "assets_expired": 2},
  "claims": [{"id": "clm_...", "statement": "...", "status": "contested",
              "grade": "C_listener_unverified", "uri": "reaper://ledger/.../claims/clm_..."}],
  "operations": {"last": {"id": "op_...", "status": "verified",
                          "revision_after": "rev_...", "uri": "..."},
                 "open_plan_count": 0},
  "delivery": {"master": "forwarded_verified", "checked_at": "...",
               "stems_heard": 0, "note": "URI alone is not listening"},
  "coverage": {"sections": ["project","revision","structure","tracks","routing",
                            "evidence","claims","operations","delivery"],
               "tracks_returned": 12, "tracks_total": 24, "truncated": true,
               "next": "reaper://ledger/.../views/tracks?cursor=..."}
}
```

Inclusion policy for Tier 0:

* all project/revision/coverage fields (never truncated);
* master + bus topology (not all 24 track rows when over budget);
* tracks ordered by signal role then name, with a cap (e.g. 12 rows) and exact counts;
* only the newest operation and any open plan;
* only claims that are `contested`, `stale`, or grade A/B; a count of the rest;
* delivery status for the master and a `not_heard` marker when no verified delivery;
* every omitted list carries a `next` URI.

The brief **MUST NOT** include raw metrics arrays, curves, MIDI notes, full FX
parameter lists, or base64 audio. Those are Tier-1/deep.

### 5.2 Tier-1 paged views

A small fixed set of views, each with a typed query:

| View | Rows | Filters | Stable sort |
|---|---|---|---|
| `tracks` | track/bus/master summary | role, hidden, mute, solo, name prefix | `track_guid` |
| `items` | item placement/take summary | track_guid, start/end window, take_guid | `(track_guid, position_qn, item_guid)` |
| `fx` | slot/identity/enabled/parameter summary | track_guid, slot, enabled | `(track_guid, slot)` |
| `routing` | send/route edges | source, destination, pre/post | `(source_guid, send_index)` |
| `timeline` | tempo/meter/markers/regions/sections | musical or project range | `(time_qn, kind, id)` |
| `assets` | asset/lease/window summary | role, status, revision | `(created_at, asset_id)` |
| `evidence` | measurements/events/listener notes | construct, state, grade, window | `(revision_fingerprint, recorded_at, id)` |
| `claims` | claims/hypotheses + alternatives | status, grade, construct | `(recorded_at, claim_id)` |
| `operations` | plans/changes/verifications | status, revision, target | `(applied_at, op_id)` |
| `delivery` | listener deliveries | status, asset, listener | `(checked_at, delivery_id)` |

Pagination contract:

```json
{"view": "tracks", "limit": 50, "cursor": "opaque-base64url",
 "filters": {"role": "drums"}, "include": ["fx_summary"],
 "rows": [], "returned": 50, "total": 137, "next_cursor": "...",
 "has_more": true, "snapshot_revision": "sha256:..."}
```

* `limit` is capped (e.g. 200) and `total` is exact for the filter, not an estimate.
* Cursor encodes `(snapshot_revision, sort_key, record_id)`; it is invalid after a
  revision change and returns `ledger_cursor_stale` rather than silently shifting rows.
* `include` expands at most one level (`fx_summary`, `send_summary`,
  `asset_window`); deeper expansion requires a record URI.
* A page **MUST NOT** exceed a byte cap (e.g. 256 KiB); rows are compact summaries with
  URIs.

### 5.3 Deep records and resources

One record read returns the full payload plus its edges and state, not the whole
project. Dense payloads (curves, spectra, MIDI, waveform peaks) live in blobs with
`payload_sha256` + `payload_uri`; the ledger record carries the summary and the blob
pointer.

Proposed resource templates (in addition to the existing five):

```
reaper://ledger/index
reaper://ledger/{instance_id}/{project_id}/tier0
reaper://ledger/{instance_id}/{project_id}/views/{view_id}
reaper://ledger/{instance_id}/{project_id}/records/{record_id}
reaper://ledger/{instance_id}/{project_id}/records/{record_id}/dependencies
reaper://ledger/{instance_id}/{project_id}/assets/{asset_id}
reaper://ledger/{instance_id}/{project_id}/claims/{claim_id}
reaper://ledger/{instance_id}/{project_id}/operations/{operation_id}
```

That is eight templates; the implementation may collapse `records/{id}` and
`claims/{id}` into one typed-record template to stay within a six-template budget. The
hard rule is that every ledger URI is resolvable from the Tier-0 brief without a
tool call, and every URI carries `instance_id` + `project_id` so it cannot cross a
project boundary.

### 5.4 Token and payload budget

| Class | Inline budget | Storage |
|---|---|---|
| Tier-0 brief | ≤ 3,800 estimated tokens | derived on read, cached by revision fingerprint |
| Tier-1 row | ≤ 400 bytes | journal `payload` |
| Record summary | ≤ 2 KiB | journal `payload` |
| Full record | ≤ 64 KiB | journal `payload` or blob |
| Curves/MIDI/peaks | never inline | manifest blob + `payload_sha256` + `payload_uri` |
| Audio | never inline | `reaper://analyses/{id}/audio/{guid}` lease |

The estimator and budget are configuration, not a model prompt. A view that cannot fit
returns fewer rows plus exact totals; it never emits an unbounded list and hopes.

---

## 6. MCP binding and discovery

### 6.1 Tools

Keep the surface bounded. Recommendation: **two** new tools, not one per entity.

1. `reaper_ledger` — read-only, one entry point:
   ```text
   mode: "tier0" | "view" | "record" | "dependencies" | "changes"
   project_id: str | None
   instance_id: str | None
   view: str | None            # tracks/items/fx/routing/timeline/assets/evidence/claims/operations/delivery
   record_id: str | None
   filters: dict | None
   include: list[str] | None
   cursor: str | None
   limit: int = 50
   detail: "summary" | "full" = "summary"
   ```
   Returns a compact envelope with `ok`, `view`, `rows`/`record`, `returned`, `total`,
   `next_cursor`, `revision`, and `state_counts`.

2. `reaper_ledger_record` — append-only write:
   ```text
   kind: "observation" | "claim" | "hypothesis" | "intent" | "plan" |
         "operation" | "change" | "verification" | "delivery" | "annotation"
   project_id: str
   expected_revision: dict
   request_id: str
   payload: dict
   evidence_ids: list[str] = []
   dep_set: list[str] = []
   scope_set: list[str] = []
   supersedes: str | None = None
   ```
   `expected_revision` is mandatory for anything that asserts project state. The tool
   validates the typed payload via the existing `mix.models`/`context.audio` classes
   before appending; it never accepts free-form JSON that bypasses those invariants.

Optionally a third, `reaper_ledger_verify`, if verification is not folded into
`reaper_ledger_record(kind="verification")`. Preference: fold it in; fewer tools, one
contract. Total surface would become 21 tools (19 + 2).

Why not reuse `reaper_project` for Tier-0? `reaper_project` is a live snapshot with
optional items/FX/MIDI and no evidence history. The ledger brief is revision-pinned and
includes claims/operations/delivery. Keeping them separate preserves the existing
contract and lets a client choose live state vs recorded evidence. The Tier-0 response
**SHOULD** include a `live_snapshot_uri` pointing at the existing project context
resource.

### 6.2 Discovery path

The model must find the ledger without a 732-function flood:

1. `reaper_session(action="health")` → instances, project IDs, bridge session IDs, and
   `ledger_uri` per project.
2. `reaper_capabilities` gains one family:
   ```json
   {"id": "context_ledger",
    "description": "Revision-pinned, append-only project evidence: Tier-0 brief, paged views, records, dependencies.",
    "resource_uri": "reaper://ledger/index",
    "tool": "reaper_ledger"}
   ```
3. `reaper://ledger/index` returns a compact map: projects, view IDs, record-kind
   counts, schema versions, and URI patterns. It is bounded (< 2 KiB per project, max
   N projects) and pages with a cursor.
4. `reaper_project` adds `ledger_tier0_uri` and `ledger_revision_id` beside the existing
   `context_uri`, so a caller that already has a project lands on the brief.
5. `reaper_analyze` records the capture/asset/measurement records and returns their
   IDs and URIs beside the existing `analysis_id`; `reaper_edit_map` and
   `reaper_mix_evidence` accept `analysis_id` or a ledger capture ID.

The generic API catalog stays reachable through `reaper_capabilities` and
`reaper_api_search`; the ledger never replaces lower-level discovery.

### 6.3 Error contract

New stable codes (same shape as `context/errors.py`):

| Code | When | `next_action` |
|---|---|---|
| `ledger_unavailable` | journal/index missing or unreadable | check project ledger root |
| `ledger_cursor_stale` | cursor revision != current snapshot | re-read Tier-0 |
| `ledger_cursor_invalid` | malformed cursor | restart pagination |
| `record_conflict` | idempotency key reused with a different payload hash | read the existing record |
| `dependency_cycle` | append would create a cycle in `derived_from`/`supersedes` | remove the edge |
| `instance_ambiguous` | >1 instance matches a project path | pass `instance_id` |
| `asset_missing` / `asset_hash_mismatch` | bytes gone or hash differs | re-capture; do not trust the URI |
| `not_heard` | listener claim requested B grade without verified delivery | forward and verify audio |
| `schema_version_unsupported` | unknown `ledger_format`/record major | upgrade reader |

### 6.4 Idempotency and revision gates

* Reads never mutate the project; they may append a `view`/`delivery` observation only
  when explicitly requested.
* Writes require `expected_revision` and compare `same_content`, not only
  `change_count`. A stale expected revision returns `stale_context` with both refs.
* `request_id` is the idempotency key. Replaying the same request with the same
  canonical payload returns the existing record and `idempotent_replay=true`; replaying
  it with a different `payload_sha256` returns `record_conflict`.
* Audio delivery is a separate `delivery` record. A ledger record may say
  `uri_only`/`forwarded_unverified`; only `forwarded_verified` with `checked_at` and
  `method` sets `heard=true`.

---

## 7. Migration from current analysis records

Today `reaper_analyze` keeps a report in `_analysis_records[analysis_id]` with
`project_id`, `revision`, `role`, `artifact_job_id`, `audio_files`, `report`,
`expires_at` (`mcp_server.py:740-791` in the current checkout). That
record is a lease, not a ledger.

### 7.1 Mapping

| Current field | Ledger record |
|---|---|
| `analysis_id` (`a-<epoch>-<hex>`) | legacy alias on a `capture` record; keep for old clients |
| `project_id` + `revision` | `project`/`revision` records; `RevisionRef` with digest scope |
| `artifact_job_id` + `audio_files` | `capture` + one `asset` per source GUID; URI `reaper://analyses/{id}/audio/{guid}` |
| `report.capture.time_window` / `tail_s` | `SignalWindow` on the capture; `project`/`file`/`measured` spans |
| `report.sources[*].analysis_window` | per-asset window; measured span is the project section |
| `report.sources[*].metrics` | `measurement` records, one per metric/window/method |
| `report.sources[*].curves` | blob (`payload_sha256` + `payload_uri`), summary inline |
| `report.sources[*].capture` | asset provenance (stage, method, channel view, sample rate, channels) |
| `report.coverage` | revision digest coverage + per-record `partial` flags |
| `analysis_id` expiry (`expires_at`) | `asset.lease_expires_at`; expiry appends a lifecycle record |
| `build_mix_evidence(report)` | derived `observation` records with method version; never auto-claims |
| `reaper_edit_map` output | `event`/`section` records + a derived view, not a new truth |

### 7.2 Migration sequence (no rewrite)

1. **Schema registry + journal writer** (new module `context/ledger.py`, no changes to
   existing dataclasses). Accepts the existing `to_dict()` payloads.
2. **Read-only adapter** `analysis_report_to_records(report, analysis_id)` that emits
   the records above without writing. Tests compare adapter output with the current
   report fields.
3. **Dual write** in `reaper_analyze`: keep `_analysis_records` for the existing
   `reaper_edit_map`/`reaper_mix_evidence`/`reaper_release` paths and append ledger
   records after a successful capture. If the ledger append fails, the analysis result
   still returns with `ledger_status="unavailable"`; it must not pretend the ledger
   succeeded.
4. **Alias lookup**: `reaper_edit_map`/`reaper_mix_evidence` accept either
   `analysis_id` or a `cap_`/`rev_` ledger ID. The alias table is itself a ledger
   record (`legacy_alias`).
5. **Views** built from records, compared against the current report for parity.
6. **Dependency invalidation** enabled behind a feature flag; old reports without
   `dep_set` are `partial`, never silently `fresh`.
7. **Deprecate** the in-memory record only after the alias path and lease cleanup have
   lived through a full test cycle. `reaper_release` keeps its contract; it releases
   the lease and appends the lifecycle record.

Backward compatibility: old report JSON remains readable; `schema_version=1` records
need no rewrite; a missing `ledger_format` on an old file is an error, not a guess.
Rollback: stop dual-writing; the legacy path is unchanged because the ledger append is
post-success and never required for the analysis result.

---

## 8. Adversarial tests (headless, MCP level)

These extend `tests/test_mcp_server.py` / `tests/test_wave_d_mcp_analyze.py` style:
fake bridge/session, no live REAPER. Each test names the required behaviour, not just a
status code.

| ID | Scenario | Required result |
|---|---|---|
| L-A1 | Stale revision: record measured at `rev_1`, project now `rev_2` with changed tracks | record stays readable; view state `stale`; claim cannot be `fresh`; `stale_context` on write |
| L-A2 | Partial capture: digest `coverage.master=false`, `missing=["master"]` | Tier-0 shows `partial`; no A-grade claim about master; `coverage.missing` non-empty |
| L-A3 | Conflicting measurements: two methods, different values, same metric/window | both records retained; view `contested=true`; alternatives listed; no average |
| L-A4 | Two REAPER instances, same project path | `instance_ambiguous` with candidates; no record read across instances |
| L-A5 | Two projects with the same track GUID | per-project journal; track lookup without `project_id` fails; no cross-project edge |
| L-A6 | Hidden tracks and master | hidden/muted tracks still present with flags; master always present; a claim about "all tracks" requires count match |
| L-A7 | Tempo-map automation: edit at bar 17 after a tempo change | `TimeRef` carries `bar_beat` + map ID; no map → `mapping_unavailable`; seconds are not guessed |
| L-A8 | Long project pagination: 10k records, limit 50 | exact `total`; stable cursor; no duplicate/missing rows; page byte cap respected; cursor invalid after revision change |
| L-A9 | Missing audio delivery | asset URI only → `delivery.status="uri_only"`; listener claim `C_listener_unverified`; `not_heard` surfaced |
| L-A10 | Huge discovery | `reaper://ledger/index` and Tier-0 stay under budget; no 732-function dump; capabilities index remains compact |
| L-A11 | Expired asset | bytes gone; record `status="expired"`; dependent measurement `unavailable`; no silent re-hash |
| L-A12 | Undo/verification | operation `rev_N -> rev_N+1`; verification at `N+1`; undo restores `rev_N` fingerprint and appends `undo` + `change`; no mutation of original |
| L-A13 | Idempotent append | same `request_id` + same payload → one record, `idempotent_replay=true`; different payload → `record_conflict` |
| L-A14 | Unknown schema | `ledger_format=99` → `schema_version_unsupported`; no fields guessed; old reader refuses |
| L-A15 | Path attack | asset URI `../../other-project/...`, junction, or unregistered filename → rejected; cross-project lease refused |
| L-A16 | False coverage | digest says `coverage=true` but payload missing | `DigestEvidence` degrades to `coarse`, `missing` includes the unbacked name; no `full` scope |
| L-A17 | Multiple projects, same change_count | identity compared by `project_id` + `instance_id` + fingerprint, not `counter`; no false fresh |
| L-A18 | Stale cursor | read page, apply edit, reuse cursor | `ledger_cursor_stale`; caller re-reads Tier-0; no silent row shift |

Test harness rules: no live REAPER, no downloaded dataset, no network. Fake bridge
returns typed snapshots and digests; the ledger writer uses a temp directory; all
assertions read back the appended records and the rendered view. Live gates (real MCP
stdio + REAPER) remain separate and marked skipped by default.

---

## 9. Acceptance criteria and open decisions

A ledger implementation is acceptable only when:

1. A record is append-only and replaying the journal reproduces the same view state.
2. Every claim walks to an asset/window/measurement, and every measurement declares
   revision, stage, channel view, method/version, window and uncertainty.
3. Stale/partial/contested/unavailable/not_heard are visible and block the claims they
   should block.
4. Tier-0 stays ≤ 4k tokens for a 100-track project and reports truncation exactly.
5. Tier-1 pagination is stable, exact and byte-capped.
6. Assets are manifest-owned, hash-verified and isolated by instance/project.
7. The MCP surface stays bounded: no one-tool bottleneck, no 732-tool flood.
8. Old analysis reports remain readable; rollback to the legacy path is trivial.

Open decisions (must be resolved before coding, not guessed in the ledger):

* **Storage**: JSONL truth + SQLite index vs SQLite-only. Recommendation above is
  JSONL truth; confirm Windows append-lock behaviour.
* **Where the journal lives**: alongside the project (`<project>.reaper-ledger/`) vs a
  user cache root. Project-adjacent is auditable but may pollute the media folder;
  cache is safer but easier to lose. Recommendation: project-adjacent by default,
  configurable root, never inside the RPP.
* **Token estimator**: pinned tokenizer vs chars/4 approximation. The budget must be
  deterministic across clients; pin one and version it.
* **Envelope evolution**: add fields to `RecordEnvelope` under `schema_version=1` vs
  bump to 2. Adding optional fields is backward-compatible in JSON but the dataclass
  currently rejects unknown versions; recommendation is additive optional fields plus
  `ledger_format`, no bump until a field is removed.
* **Resource count**: 8 templates vs a collapsed 6. The exact count is a task-eval
  decision, not an aesthetic one; start with the collapsed set and add deep templates
  only when a measured task needs them.
* **Claim lifecycle**: whether `ClaimStatus` transitions are separate records
  (recommended) or mutable status in the claim payload (rejected: breaks append-only).
* **Retention of baseline/final audio**: project lifetime is a policy, not a default;
  the user must be able to release it explicitly and see the hash remain.

---

## 10. Explicit non-claims

* This document does **not** implement a ledger. There is no `context/ledger.py`, no
  journal file, no view builder, no ledger MCP tool/resource in the checkout.
* `reaper_analyze` now forwards `tail_ms`, `channels`, `sample_rate` and returns
  `analysis_windows`; that is real and tested. It is not a ledger.
* The 12-vs-19 documentation discrepancy is recorded in §1 and left to Hermes; no
  README/MCP guide was edited here.
* No dataset, Lua bridge, `daw/session.py` or `context/revision.py` change is part of
  this proposal.

## User clarification — completeness, trajectory and retention

The user requires a full, lossless historical record of project snapshots and
changes so the model can inspect progression across revisions. This requirement
clarifies that compact records/views are not a reason to omit source state:

* At each material revision, retain the complete available REAPER state needed
  to understand editing and sound: track/item/take topology; FX identity, order,
  full available parameter/state data; routing; faders and pan; envelopes and
  automation; MIDI; markers/regions; tempo/time maps; master and project/render
  settings. Large or opaque state may be stored in hash-verified payload blobs,
  but the complete value must remain retrievable. Compact summaries are derived
  navigation aids only; pagination must disclose coverage and truncation.
* Retain full machine-readable before/after deltas for every exposed domain and
  every revision. Link each delta to the operation, exact object IDs, source
  evidence, and post-change readback/undo verification.
* Retain factual, model-authored descriptions of the requested intent and the
  observed changes as annotations separate from machine-observed deltas. The
  system reports what changed and the supporting measurements; it does not
  assign a quality verdict such as "better" or "worse". Any interpretation or
  causal explanation must be clearly marked as an unverified hypothesis, not a
  property computed by the library. Compare only matching project revisions,
  signal stages, channels, time windows, and measurement settings, or explicitly
  mark the comparison as non-equivalent.
* Retain all capture/snapshot/measurement/model-result **records** and their
  provenance, windows, hashes, and dependency links. Do not retain intermediate
  per-track rendered WAVs/stems indefinitely. Temporary audio is manifest-owned
  and removed at lease/release; its hash and measurement record remain. Keep
  source media and the single accepted final project/master under the explicit
  benchmark retention policy. The ledger is not a pile of `.RPP` version copies.

## User correction — no quality verdicts

The user's latest clarification supersedes any earlier wording in this document
that suggested labeling a revision "better", "worse", "mixed", or "inconclusive".
The library must not infer or encode those judgments. It records complete
before/after deltas and neutral descriptions of what changed, with measurements
and provenance where available. Quality judgment belongs to the model/user in
their actual task context; the underlying ledger stays descriptive.

This full source-of-truth history is compatible with a bounded MCP entry view.
The model-facing representation contract is clarified and superseded by the
owner addendum below: the model receives the unified panorama, not separate
revision/entity JSON pages. Internal indexes and lossless records may still be
used to build that view.

## Owner addendum — 2026-11-13: panorama-only context and version deltas

This addendum records the owner's clarified requirements and supersedes any
earlier proposal in this document that makes multiple project-state views,
compact tiers, or raw record pages peer model-facing representations.

### One model-facing representation

* **Panorama is the only model-facing representation of REAPER project and
  audio state.** It includes the complete linked project map, tracks/buses and
  master, arrangement and items, MIDI, FX, routing, automation, tempo/time
  signature, captures, measurements, coverage and provenance. JSON may remain
  the transport encoding; the forbidden alternative is fragmented/raw domain
  records that make the model assemble the meaning itself.
* A request for a time interval (for example, project seconds `[36,45)`) returns
  the same panorama contract scoped to that interval, with all relevant tracks,
  buses and master together. It carries exact project-time and tempo-map
  coordinates, signal stage, capture revision and links to the corresponding
  audio payloads. A request outside the captured audio window must explicitly
  capture that interval at the expected revision or fail stale; it must not
  return unrelated or guessed audio.
* Raw `record.json`, standalone analysis reports and per-track JSON are
  internal storage/implementation details, not alternate tools or resources
  intended for the model. Capability discovery and edit operations remain
  available, but any read of project/audio state is represented as a panorama
  or a panorama delta.
* A panorama delta is also a panorama-shaped view. It preserves the linked
  project context and identifies the exact two captures/revisions being
  compared; it is not a disconnected list of metric differences.

### What the version journal must record

* The journal is the append-only history of complete captures and accepted
  project revisions, not just a list of revision counters. Each revision
  records the full structural/project state and the complete available
  per-source analysis for every track, bus and master. It records every
  available metric and temporal series, not only flagged values or a selected
  subset. Missing, unsupported, truncated and incomparable values stay
  explicit.
* For every comparable source/metric/window, a delta retains both raw values,
  signed change, unit, analysis method/version, channel view, signal stage,
  exact time window, source identity/lineage, asset hashes, and both revision
  bindings. Project-state deltas likewise preserve before/after values for
  tracks, items/takes, FX and parameters, routes, faders, pan, automation, MIDI,
  tempo/time-signature maps, markers and other captured domains.
* Link each change to the operation/plan that produced it and to post-change
  readback and capture evidence. When several edits are one batch, the journal
  may attribute the measured result to the batch, but must not claim which
  individual edit caused it without an isolating comparison.
* Descriptions of user intent and model actions are annotations linked to
  evidence, separate from machine-observed changes. The journal reports the
  direction and magnitude of measured changes; it never assigns a universal
  `better`/`worse` verdict or silently turns a delta into an artistic goal.
  User listening feedback remains a separately identified observation.
* Do not treat REAPER's coarse `change_count` as a semantic delta. Full capture
  temporarily changes track mute state to render sources and restores it;
  the counter may advance even when the lasting project state is restored.
  Compare actual revision-bound fields/fingerprints, record capture-side
  operations, and report state changes only when the captured values differ.
* All successful full captures remain complete, immutable records. Their
  per-track/master audio stays bound to its capture and content hashes rather
  than becoming loose files or playlist tracks. Identical content may be
  deduplicated. Retention or deletion must be explicit; do not silently expire
  a capture the owner asked to keep. The existing capture archive's indefinite
  retention must be surfaced so storage growth is visible and manageable.

### The current "delta" is not this journal

The checkout has a durable full-capture archive, an `index.jsonl` entry per
capture, and a `retention.jsonl` lifecycle log. These preserve capture identity,
revision/window, integrity and audio-asset references, and retention events.
They do **not** compute or expose project-version deltas. The MCP server has no
capture-history or capture-diff tool/resource. `reaper_mix_evidence` summarizes
selected evidence within one leased `analysis_id`; it is not a comparison of
two project versions. The context-ledger schema/design is not a working journal
implementation.

Some delta interpretations would currently be misleading:

1. A difference in `change_count` can be caused by temporary capture mutes and
   their restoration, not by a lasting edit.
2. Matching only track names or array position can compare different sources
   after insertions, deletions, duplication or regrouping. Use stable IDs and
   explicit lineage; unmatched entities are `unmatched`, not zero-delta.
3. Comparing different render stages, channel views, windows, sample rates,
   metric versions or tempo mappings mixes unlike evidence. Refuse or label it
   `incomparable`; do not hide the mismatch behind a number.
4. A project/FX parameter change and an audio-metric change in the same revision
   are correlation, not proof that the parameter caused that sound change.
5. A numeric delta says what changed, not whether the mix improved. Direction
   toward a stated reference/goal may be reported only as that explicit
   comparison; it is not an automatic quality verdict.

### Flexible model-use principles, not a mixing pipeline

The model guide should teach durable invariants, not a fixed order of EQ,
compression, panning, editing or mastering. The model should:

* keep one current panorama/revision in view and refresh or invalidate it after
  project changes;
* distinguish project facts, measured signal properties, model hypotheses and
  the user's aesthetic judgement, linking each conclusion to source, interval,
  method and revision;
* discover and use any relevant REAPER capability without loading hundreds of
  signatures at once, and without assuming that a missing dedicated tool means
  a capability is absent;
* choose analysis and editing operations from the user's goal and observed
  evidence. It must not call every analyzer, add effects for their own sake,
  infer a genre recipe, or quantize merely because an example mentioned rhythm;
* reason about the complete signal path and relationships among tracks, buses
  and master, including FX, routing, fader/pan and automation; a track render
  is not the master and an audio URI is not proof the model actually heard it;
* keep alternatives and uncertainty visible, surface missing coverage, and
  avoid inventing roles, causes or results from names and isolated metrics;
* make revision-bound changes through available safe operations, then inspect
  actual readback and relevant post-change audio. Batch compatible work to
  reduce calls, but do not skip a check needed to establish the requested
  result; use undo when the observed state differs from the intended change;
* never use scripts or out-of-band controls as a substitute for the model's
  documented MCP path to operate REAPER. Independent scripts may verify
  artifacts after the fact, but cannot prove model-visible MCP use.

These are operating principles, not mandatory steps. They must leave the model
free to choose an appropriate path for an open-ended task.

### Acceptance gates for this addendum

1. In the active model MCP session, project-state reads expose the panorama
   contract only; raw full-capture records and standalone JSON analysis reports
   are not listed as alternative model-facing reads.
2. From one panorama/capture, a request for `[36,45)` returns all captured
   tracks, buses and master for that interval, with the same project revision,
   full provenance and audio resources that the model client can actually
   consume. Stale or out-of-window requests fail explicitly.
3. Given two captures with known changes on selected tracks, the journal diff
   reports structural edits and every metric delta for every matched source;
   unchanged values remain represented, and missing/unmatched data is not
   silently treated as zero.
4. A capture that changes only temporary mute counters but restores the same
   project state produces no semantic project-change delta.
5. Mismatched stage/window/channel/method/revision is explicitly
   `incomparable`; no global better/worse field is emitted. User feedback and
   model-authored descriptions remain separate from measured facts.
6. The MCP README describes the actual available surface and these flexible
   principles/observed failure patterns without prescribing a genre-specific
   workflow. It states plainly which panorama, history, delta and audio
   delivery gates have not yet passed.
