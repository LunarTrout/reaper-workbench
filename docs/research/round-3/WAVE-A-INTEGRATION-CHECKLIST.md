# Wave A — integration checklist (lane 6 of 6)

**Baseline:** HEAD `86b14bb`, dirty tree preserved (`docs/AI-MIX-SURFACE.md`, `docs/TZ.md`,
`scripts/reaper_bridge.lua`, untracked `docs/EDITING-ACCEPTANCE.md`, `docs/research/`).
**Lane scope:** independent verification only. No domain module, no other lane's test and no
dirty tracked file was modified by this lane. Owned artifacts: this checklist and
`tests/test_contract_integration.py`.
**Question answered:** can the eight Wave A modules be imported, composed and serialized in one
process without a second definition of a shared type, a second ID vocabulary or a softened
invariant?

**Verdict:** integration-ready except for one P0 registry gap — `hypgrp_` is minted by
`mix/models.py` (R3-04 assigns it to R3-05) but is absent from `context.ids.PREFIXES`. Two
integration tests fail on exactly that defect; everything else passes (301/303).

---

## 1. Owners and files

Ownership below is the blueprint's domain owner (`IMPLEMENTATION-BLUEPRINT.md` §2.1). Wave A
delivered all eight files as untracked additions; the per-lane attribution is not restated here
because lane 6's dispatch does not assign file→lane, and guessing it would be a false record.

| File | Blueprint owner | Lines | Public names (`__all__`) | Contract test file | Items | State |
|---|---|---|---|---|---|---|
| `src/reaper_audio/timebase.py` | R3-05 / R3-03 | 408 | 12 | `tests/test_timebase_schema.py` | 19 | green |
| `src/reaper_audio/context/errors.py` | R3-05 | 97 | 8 | exercised by every suite | — | green |
| `src/reaper_audio/context/schema.py` | R3-05 | 258 | 8 | `tests/test_timebase_schema.py` | 19 | green |
| `src/reaper_audio/context/ids.py` | R3-05 | 413 | 15 | `tests/test_ids_event_model.py` | 27 | green, **registry incomplete** |
| `src/reaper_audio/context/audio.py` | R3-01 | 1261 | 25 | `tests/test_context_audio.py` | 141 | green |
| `src/reaper_audio/context/revision.py` | R3-06 | 266 | 11 | `tests/test_revision.py` | 36 | green |
| `src/reaper_audio/analysis/event_model.py` | R3-02 | 843 | 20 | `tests/test_ids_event_model.py` | 27 | green |
| `src/reaper_audio/mix/models.py` (+ `mix/__init__.py`) | R3-04 | 1739 | 42 | `tests/test_mix_models.py` | 54 | green, **mints an unregistered prefix** |

Not delivered in this wave and therefore out of scope for this checklist: `context/ledger.py`,
`store.py`, `deps.py`, `claims.py`, `anchors.py`, `manifest.py`, `views.py`, `assembler.py`,
`audio_handoff.py`, `analysis/event_build.py`, `event_policy.py`, `event_index.py`,
`project_time_map.py`, `rhythm_map.py`, `structure.py`, `edit_intent.py`, `score_time.py`,
`mix/scope.py`, `constructs.py`, `references.py`, `claims.py`, `severity.py`, `causes.py`,
`verify.py`, `actions.py`, `daw/verify.py`, `daw/revision.py`.

---

## 2. Import map (verified from source and by AST)

| Module | Imports (intra-package) |
|---|---|
| `context/errors.py` | — |
| `timebase.py` | `context.errors` |
| `context/schema.py` | `context.errors` |
| `context/ids.py` | `context.errors` |
| `context/revision.py` | `context.errors`, `context.schema` |
| `context/audio.py` | `timebase` (`MappingStatus`), `context.errors`, `context.schema` |
| `analysis/event_model.py` | `context.errors`, `context.ids`, `timebase` |
| `mix/models.py` | `context.audio`, `context.errors`, `context.schema` |
| `mix/__init__.py` | `mix.models` |

```
errors ──┬── timebase ──┐
         ├── ids ───────┼──> event_model
         ├── schema ──┬─┴──> revision
         │            └────> audio ──> mix.models ──> mix
         └────────────────────────────────────────────┘
```

* **No cycle.** Kahn's algorithm over the nine nodes above terminates with all nodes visited
  (`test_wave_a_import_graph_is_acyclic`). The graph is a DAG with `errors` as the only source.
* **No analysis dependency in mix.** `mix/models.py` imports no `reaper_audio.analysis` module
  (`test_mix_lane_does_not_depend_on_the_analysis_package`), matching the package docstring and
  the R3-04 independence rule.
* **Direction is correct:** audio/event/mix import shared contracts; shared contracts import no
  domain. `event_model` re-exports `TimeRef`/`Interval` (import, not redefinition) so downstream
  code can keep one import site.

---

## 3. Exported names

| Module | `__all__` |
|---|---|
| `timebase` | `TimeBasis, MappingStatus, TimeUnit, TimeRef, Interval, TimeMap, INTERVAL_ANCHORS, validate_time_ref, time_ref_to_dict, time_ref_from_dict, compose, to_sample_index` |
| `context.errors` | `ContractError, UnknownPrefixError, MissingTimeBasisError, InvalidStatusError, RevisionMismatchError, MappingUnavailableError, SchemaVersionError, ContractValidationError` |
| `context.schema` | `SCHEMA_VERSION, FINGERPRINT_SCOPES, RecordKind, RevisionRef, RecordEnvelope, validate_revision, dumps_record, loads_record` |
| `context.ids` | `CROCKFORD_ALPHABET, ID_BODY_LENGTH, GESTURE_PREFIX, PREFIXES, DEPRECATED_PREFIXES, DERIVED_PREFIXES, AUTHORED_PREFIXES, new_id, derive_id, parse_id, validate_id, is_valid_id, is_derived_prefix, is_authored_prefix, canonical_key` |
| `context.audio` | `Stage, CaptureMethod, SourceKind, ChannelView, DeliveryStatus, ObjectKind, ArtifactRole, Isolation, ObjectRef, SignalWindow, AudioAsset, CaptureRequest, Capture, DeliveryEvidence, ListenRequest, ListenResult, CaptureStageUnsupportedError, STAGE_METHODS, SIGNAL_WINDOW_BOUNDARIES, CAPTURE_CHANNEL_MODES, ASSET_STATUSES, CAPTURE_STATUSES, validate_stage_method, dumps_audio_record, loads_audio_record` |
| `context.revision` | `FINGERPRINT_PREFIX, FINGERPRINT_VERSION, PLACEHOLDER_PREFIX, fingerprint_fields, from_project_revision, is_placeholder, is_stale, merge_dep_sets, require_same_content, same_content, same_identity` |
| `analysis.event_model` | `EVENT_INVARIANT_CODE, CONFIDENCE_BASES, PERFORMANCE_EVENT_KINDS, SCORE_ORIGINS, GESTURE_MEMBER_PREFIXES, SignalKind, EventStatus, UsageScope, Confidence, MethodRef, EvidenceRef, Pitch, EventDuration, SignalEvent, PerformanceOccurrence, PerformanceEvent, PerceivedGesture, ScoreObject, TimeRef, Interval` |
| `mix.models` | `MIX_INVARIANT_CODE, HYPOTHESIS_NOT_TESTABLE_CODE, OBSERVATION_STATUSES, TRACK_ROLES, SECTION_LEVELS, LABEL_SOURCES, ROLE_SOURCES, LEVEL_MODES, CONFIDENCE_BASES, SEVERITY_LEVELS, SEVERITY_ANCHORS, INTENT_SOURCES, EXPECTED_DIRECTIONS, POST_CHANGE_STATUSES, IssueClass, ObservationKind, ClaimClass, ClaimStatus, EvidenceGrade, CauseFamily, SectionRef, RoleRef, ScopeEntity, SignalPathRefs, LevelReference, MixScope, MixMethod, MetricReading, AssetRef, ListenerInfo, MixObservation, AlternativeGroup, ClaimConfidence, Severity, IntentRef, MixClaim, Prediction, DiscriminatingTest, IssueHypothesis, PostChangeCheck, dumps_mix_record, loads_mix_record` |

---

## 4. Duplicate-type audit

Source scan over every `*.py` under `src/reaper_audio` (`test_shared_classes_are_defined_exactly_once`):

| Canonical type | Definitions found | Location |
|---|---|---|
| `TimeRef` | **1** | `timebase.py:161` |
| `Interval` | **1** | `timebase.py:227` |
| `RevisionRef` | **1** | `context/schema.py:75` |
| `Stage` | **1** | `context/audio.py:29` |

`event_model.TimeRef is timebase.TimeRef`, `event_model.Interval is timebase.Interval`,
`mix_models.Stage is audio.Stage` — identity, not equality (`test_all_wave_a_modules_import_in_one_process`).

**Concept-level near-duplicates (not P0, flagged for P5 consolidation):**

| Pair | Difference | Recommendation |
|---|---|---|
| `event_model.MethodRef{name, version, params_hash}` vs `mix.MixMethod{name, version, params_hash}` | Field-for-field identical, different class | Collapse to one `MethodRef` in `context/schema.py` before the ledger stores both |
| `event_model.Confidence{basis, value, calibration_id, n}` vs `mix.ClaimConfidence{…, interval}` | Same idea, mix adds an interval and a stricter `basis="interval"` rule | Promote `ClaimConfidence` semantics into one shared confidence type |
| `mix.SectionRef` vs `models.Section` | Different roles: reference-with-provenance vs geometric span | Keep both, document the direction (`SectionRef` should eventually point at a `sec_` id) |
| `mix.AssetRef` vs `audio.AudioAsset` | Pointer vs full asset record | Keep; `AssetRef` is the journal-safe projection |

---

## 5. Ownership violations

| # | Finding | Severity | Evidence |
|---|---|---|---|
| O1 | `mix/models.py` mints `hypgrp_` ids, but `context.ids.PREFIXES` does not contain `hypgrp`. Such an id can be written into a `MixClaim`/`AlternativeGroup` yet can never be minted, parsed or validated by the registry. | **P0** | `mix/models.py:1082`; `ids.PREFIXES`; `test_every_prefix_used_by_wave_a_is_registered`, `test_mix_id_prefixes_are_known_to_the_registry` (both fail) |
| O2 | `mix/models.py::_prefixed` re-implements identifier shape instead of calling `context.ids`. It accepts `sc_01ABC`, which `ids.validate_id("sc_01ABC")` rejects (`invalid_id`: body must be 26 chars). Two ID grammars for one namespace. | P1 | `mix/models.py:251-261`; runtime probe |
| O3 | `context/audio.py` re-implements the same grammar for `obs_` and `cap_` (`^obs_[A-Za-z0-9._-]+$`, `^cap_[A-Za-z0-9._-]+$`). `ListenResult(observation_id="obs_1")` and `AudioAsset(capture_id="cap_1")` are accepted; `ids.validate_id` rejects both. | P1 | `audio.py:141-142`, `1097-1101`, `839-841`; runtime probe |
| O4 | `audio.py:140` defines `_DERIVED_BODY` and never uses it — a leftover from the rejected "derived asset id" idea that `ids.new_id` now refuses by design. | P3 | grep: single hit, definition only |
| O5 | Domain error codes live outside the error registry: `capture_stage_unsupported` (`audio.py`), `event_invariant` (`event_model`), `mix_invariant` / `hypothesis_not_testable` (`mix`), `invalid_id` (`ids`) are assigned by mutating `error.code` on an instance of `ContractValidationError`. They work, but a caller cannot discover them from `context/errors.py`, which the blueprint names as the error registry. | P2 | grep `code = "` |

No lane wrote into another lane's file. `context/errors.py` is used, never redefined. `models.py`
(`Section`, `AudioBuffer`, `MetricValue`, `MeasurementReport`) is still the sole owner of the old
public types and imports cleanly alongside the new ones.

---

## 6. Divergences from the R3 blueprint

| Blueprint rule | Implementation | Status |
|---|---|---|
| 8 signal stages, exact names (`§3.4`) | `Stage` has exactly 8 members, same spellings; `STAGE_METHODS` covers all 8 | **match** |
| `capture_method` orthogonal to `source_kind` and `stage` | Three separate enums on `AudioAsset`/`CaptureRequest`; `validate_stage_method` enforces the matrix | **match** |
| `master_mix_post_fx` only from `native_render` | `STAGE_METHODS[master_mix_post_fx] == {native_render}` | **match** |
| `DeliveryEvidence` mandatory; `heard` only for `forwarded_verified` | `heard` is a property returning `status is forwarded_verified`; `ListenResult` additionally requires `checked_at` + `method` for that status | **match** |
| `RevisionRef {project_id, instance_id, epoch, counter, fingerprint, fingerprint_scope}`, one type | Exactly those fields, single definition; legacy dicts parse with `None/0/"full"` defaults | **match** |
| `counter` is diagnostic, never content | `same_content` ignores it; `same_identity` compares project/instance only | **match** |
| `TimeRef {basis, value, unit, uncertainty_ms, map_id, mapping_status}`, `anchor` on `Interval` | All six fields plus `hypothesis_id` (required by blueprint invariant 2 for `performed_beat`); `anchor` is on `Interval` only | **match, additive** |
| No cross-basis composition in Wave A; typed refusal | `compose` raises `MappingUnavailableError(next_action="project_time_map")` | **match** |
| `sample_index` floor for starts, ceil for ends | `to_sample_index(..., is_end)` | **match** |
| `ast_sha256_<64 hex>` content-addressed; never minted or derived | `parse_id` enforces 64 hex; `new_id`/`derive_id` refuse the prefix; `AudioAsset`/`AssetRef` require the id to embed `sha256` | **match in behaviour, grammar not shared** (O2/O3) |
| One `RevisionRef`, one `TimeRef`, one `Stage`, one `Interval` | 1/1/1/1 by source scan | **match** |
| `peg_` gesture, `ges_` retired and never emitted | `GESTURE_PREFIX == "peg"`; `ges` in `DEPRECATED_PREFIXES`; `parse_id("ges_…")` → `UnknownPrefixError(next_action="use peg_")`; no source emits `ges_` | **match** |
| One prefix registry for all record kinds | Registry exists and is the only minter — but `hypgrp` is missing and two modules keep private grammars | **diverges** (O1–O3) |
| `schema_version` frozen at 1, unknown versions refused | `SCHEMA_VERSION == 1`; `SchemaVersionError` on 2 and 99 | **match** |
| Inferred mix claim needs ≥2 alternatives; cause needs a test or `not_testable` | Enforced with `mix_invariant` / `hypothesis_not_testable` codes; positive controls pass | **match** |
| `prf_` belongs to an `occ_`; `note` needs pitch and duration | Enforced with `event_invariant`; wrong-prefix `occ` rejected too | **match** |

---

## 7. P0 gates

| # | Gate | Check | Result |
|---|---|---|---|
| 1 | Schema round-trip | `RecordEnvelope` with and without `revision`; `dumps_record`/`loads_record` equality; `json.dumps(..., allow_nan=False)` on both payloads | **PASS** |
| 2 | Missing basis | `TimeRef(basis=None)` → `MissingTimeBasisError`; `source_s` without `map_id` → `MissingTimeBasisError` | **PASS** |
| 3 | Unknown prefix | `new_id("nope")` / `parse_id("nope_…")` → `UnknownPrefixError`; `ges_` retired with `next_action="use peg_"` | **PASS** |
| 4 | Registry completeness | every prefix used by audio/event/mix registered | **FAIL — `hypgrp`** |
| 5 | Invalid status | `AudioAsset.status`, `Capture.status` (complete+missing refused), `MixObservation.status`, `PostChangeCheck.status`, `ClaimStatus` | **PASS** |
| 6 | Invalid revision | empty `fingerprint`, `fingerprint_scope="partial"`, `schema_version=99` all refused | **PASS** |
| 7 | Deterministic IDs | `derive_id` repeatable and input-sensitive; `ast_sha256` refused by both minters | **PASS** |
| 8 | No import cycle | AST graph + Kahn over 9 modules | **PASS** |
| 9 | Old public types import | `Section`, `MetricValue`, `MeasurementReport`, `AudioBuffer` + `daw.session.ProjectRevision` adapted through `from_project_revision` | **PASS** |
| 10 | Single definitions | `TimeRef`, `Interval`, `RevisionRef`, `Stage` defined once | **PASS** |
| 11 | Stage/method compatibility | `master_mix_post_fx` + `audio_accessor` refused; + `native_render` accepted | **PASS** |
| 12 | Delivery honesty | `heard is True` only for `forwarded_verified`; `uri_only` → `False` | **PASS** |
| 13 | Revision predicates | placeholder ≠ same_content (even with itself); full/full → `True`; coarse → `False` | **PASS** |
| 14 | Mix claim/hypothesis | inferred claim without alternatives refused; hypothesis without test and without `not_testable` refused | **PASS** |
| 15 | Event invariants | `prf` without `occ` refused; wrong-prefix `occ` refused; `note` without pitch or duration refused | **PASS** |

14 of 15 pass. Gate 4 is the single blocker and has one root cause (O1).

---

## 8. Test command and exact result

```
cd <repo>
PYTHONPATH=src /c/Users/epicc/AppData/Local/Temp/reaper-audio-library-python313/python.exe \
  -m pytest -q tests/test_timebase_schema.py tests/test_ids_event_model.py \
  tests/test_context_audio.py tests/test_revision.py tests/test_mix_models.py \
  tests/test_contract_integration.py
```

```
........................................................................ [ 23%]
........................................................................ [ 47%]
........................................................................ [ 71%]
.....................................................................FF. [ 95%]
...............                                                          [100%]
FAILED tests/test_contract_integration.py::test_every_prefix_used_by_wave_a_is_registered
FAILED tests/test_contract_integration.py::test_mix_id_prefixes_are_known_to_the_registry
2 failed, 301 passed in 1.60s
```

303 items = 19 (`timebase_schema`) + 27 (`ids_event_model`) + 141 (`context_audio`) +
36 (`revision`) + 54 (`mix_models`) + 26 (`contract_integration`).

Both failures are the same defect, reported twice on purpose: once from the declared prefix
vocabulary, once from an AST scan of `_prefixed(...)` call sites so future drift is caught even if
the hard-coded list is forgotten.

Interpreter note: the project venv (`…/hermes/installs/.../venv`) has no `pytest` and no
`numpy`; the working environment is the `reaper-audio-library-python313` interpreter
(Python 3.13.17, pytest 9.2.0, numpy 2.5.4, scipy 1.18.1, soundfile). No dependency was added.
That interpreter already resolves this checkout's `src` on `sys.path`, so `PYTHONPATH=src` is
kept for a clean environment rather than being the thing that makes the run work.

---

## 9. Proposed diffs (owner applies; this lane does not edit foreign files)

### D1 — P0: register `hypgrp` (owner: R3-05, `context/ids.py`)

```diff
--- a/src/reaper_audio/context/ids.py
+++ b/src/reaper_audio/context/ids.py
@@ -73,6 +73,7 @@
         "ann",
         "clm",
         "hyp",
+        "hypgrp",
         "int",
         "pln",
@@ -100,6 +101,7 @@
         "cap",
         "obs",
         "clm",
         "hyp",
+        "hypgrp",
         "int",
         "pln",
```

`hypgrp_<ulid>` is minted once per authored alternative group (R3-04 report, line 435), so it
belongs in `AUTHORED_PREFIXES` and must stay out of `DERIVED_PREFIXES`. No other file changes.
After this diff both failing tests pass without touching any test.

**Verified, not assumed:** the two edits were applied to a throwaway copy of `src/` + `tests/`
and the six-file suite was re-run there — `303 passed`. D1 is therefore sufficient and minimal;
no other module needs to change. The repository itself was not modified.

### D2 — P1: one ID grammar (owner: R3-04 + R3-05, `mix/models.py`)

```diff
--- a/src/reaper_audio/mix/models.py
+++ b/src/reaper_audio/mix/models.py
@@
-from ..context.errors import ContractValidationError
+from ..context.errors import ContractError, ContractValidationError
+from ..context.ids import parse_id
@@
-def _prefixed(value: Any, prefix: str, field: str) -> str:
-    """Require ``prefix_<body>``; the body's shape is the minter's business."""
-    text = _text(value, field).strip()
-    head, separator, body = text.partition("_")
-    if separator != "_" or head != prefix or not _ID_BODY.match(body):
-        _fail(
-            f"{field} must be a {prefix}_ identifier",
-            field=field,
-            next_action=f"use {prefix}_",
-        )
-    return text
+def _prefixed(value: Any, prefix: str, field: str) -> str:
+    """Require a registry-valid ``prefix_<body>`` identifier."""
+    text = _text(value, field).strip()
+    try:
+        found, _body = parse_id(text)
+    except ContractError as exc:
+        _fail(
+            f"{field} must be a {prefix}_ identifier: {exc.message}",
+            field=field,
+            next_action=f"use {prefix}_",
+        )
+    if found != prefix:
+        _fail(f"{field} must use prefix {prefix!r}", field=field,
+              next_action=f"use {prefix}_")
+    return text
```

Prerequisite: D1. `_ID_BODY` then becomes unused and should be deleted. `tests/test_mix_models.py`
already uses 26-character bodies (`sc_01J8…`, `clm_01J8…`, `hyp_01J8…`, `hypgrp_01J8…`), so it
should stay green; verify before applying.

### D3 — P1: route `obs_`/`cap_` through the registry (owner: R3-01 + R3-05, `context/audio.py`)

```diff
--- a/src/reaper_audio/context/audio.py
+++ b/src/reaper_audio/context/audio.py
@@
-from .errors import ContractValidationError
+from .errors import ContractError, ContractValidationError
+from .ids import validate_id
@@
-_OBSERVATION_ID = re.compile(r"^obs_[A-Za-z0-9._-]+$")
-_CAPTURE_ID = re.compile(r"^cap_[A-Za-z0-9._-]+$")
@@
-        if not _OBSERVATION_ID.match(observation_id):
-            raise ContractValidationError(
-                "observation_id must be obs_<id>", field="observation_id"
-            )
+        try:
+            validate_id(observation_id, "obs")
+        except ContractError as exc:
+            raise ContractValidationError(
+                f"observation_id must be obs_<id>: {exc.message}",
+                field="observation_id",
+            ) from exc
@@
-        if not _CAPTURE_ID.match(capture_id):
-            raise ContractValidationError("capture_id must be cap_<id>", field="capture_id")
+        try:
+            validate_id(capture_id, "cap")
+        except ContractError as exc:
+            raise ContractValidationError(
+                f"capture_id must be cap_<id>: {exc.message}", field="capture_id"
+            ) from exc
```

Also delete the dead `_DERIVED_BODY` (O4) in the same patch. **This one is not drop-in:**
`tests/test_context_audio.py` uses `cap_01HZZZ`, `obs_01HZZZ`, `obs_2` (8 and 5 character bodies),
so five fixtures must be widened to 26-character bodies in the same PR — those edits belong to
R3-01's test owner, not to this lane.

### D4 — P2: make the domain codes discoverable (owner: R3-05, `context/errors.py`)

```diff
+class InvalidIdError(ContractValidationError):
+    """An identifier is malformed or uses a retired prefix."""
+
+    code = "invalid_id"
+
+
+class CaptureStageUnsupportedError(ContractValidationError):
+    """The requested stage cannot be produced by the requested method."""
+
+    code = "capture_stage_unsupported"
+
+
+class EventInvariantError(ContractValidationError):
+    """A cross-field event invariant does not hold."""
+
+    code = "event_invariant"
+
+
+class MixInvariantError(ContractValidationError):
+    """A cross-field mix invariant does not hold."""
+
+    code = "mix_invariant"
+
+
+class HypothesisNotTestableError(MixInvariantError):
+    """A cause hypothesis has no test and does not declare itself untestable."""
+
+    code = "hypothesis_not_testable"
```

and re-export them from each module's `__all__`. Behaviour is unchanged (the codes already match);
only discovery improves. Note the migration hazard: `audio.CaptureStageUnsupportedError` would
become a re-export, so `isinstance` checks keep working while pickling paths change.

---

## 10. Ready / blocked

**Ready to integrate as-is**

* `timebase.py` — frozen, self-contained, one `TimeRef`/`Interval`, typed refusals for every
  unsupported conversion.
* `context/schema.py` — envelope + `RevisionRef`, strict JSON, legacy-dict tolerance.
* `context/errors.py` — stable codes, `ValueError` subclass, machine-readable `to_dict`.
* `context/revision.py` — pessimistic placeholder, `same_identity` vs `same_content` split.
* `context/audio.py` — 8 stages, stage/method matrix, content-addressed assets, honest delivery.
* `analysis/event_model.py` — event/occurrence/gesture/score with deterministic-id validation.
* `mix/models.py` — scope/observation/claim/hypothesis with the two hard invariants.

**Blocked**

* Any journal/store work that persists `AlternativeGroup.group_id`: the id cannot be recomputed
  or validated until D1 lands (P0).
* Any cross-module id round-trip (`mix` → `ids`, `audio` → `ids`) is only guaranteed for 26-char
  bodies today; D2/D3 are prerequisites for a single-namespace claim (P1).
* Nothing else in Wave A is blocked. Live REAPER gates (N1–N8, U1–U6) are untouched by this wave
  and remain open by design.

**Not claimed**

* No listener actually heard anything; `forwarded_verified` is a type-level gate, not evidence.
* No DSP, REAPER, render or inference was run by this lane.
* The five concept-level duplicates in §4 are recorded, not resolved.

---

## Appendix — what `tests/test_contract_integration.py` covers (26 items)

| Area | Test |
|---|---|
| one-process import + type identity | `test_all_wave_a_modules_import_in_one_process` |
| single definitions | `test_shared_classes_are_defined_exactly_once` (4 params) |
| gesture prefix | `test_gesture_prefix_is_peg_everywhere` |
| schema | `test_record_envelope_round_trip_with_and_without_revision`, `test_invalid_revision_is_rejected` |
| ID registry | `test_every_prefix_used_by_wave_a_is_registered`, `test_mix_id_prefixes_are_known_to_the_registry`, `test_retired_gesture_prefix_stays_retired`, `test_ast_sha256_is_content_addressed_only`, `test_unknown_prefix_is_rejected_for_minting_and_parsing` |
| time/stage/delivery | `test_time_value_without_basis_is_rejected`, `test_stage_method_compatibility`, `test_delivery_heard_only_after_verified_forwarding` |
| revision | `test_revision_predicates_separate_identity_from_content` |
| mix | `test_mix_scope_uses_the_audio_lane_stage`, `test_inferred_claim_without_alternatives_is_rejected`, `test_hypothesis_needs_a_test_or_an_explicit_not_testable` |
| event | `test_performance_event_requires_an_occurrence`, `test_note_requires_pitch_and_duration`, `test_signal_event_assets_are_content_addressed` |
| architecture | `test_wave_a_import_graph_is_acyclic`, `test_mix_lane_does_not_depend_on_the_analysis_package`, `test_old_public_types_still_import` |
