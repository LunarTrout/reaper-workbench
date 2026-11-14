"""Cross-lane integration checks for the Wave A contracts.

This file is owned by the integration lane.  It deliberately re-tests only the
seams *between* modules — identity of shared types, one registry of prefixes,
one revision type, one stage enum, one time type — plus the negative paths that
must never be softened:

* a time value without a basis,
* an unknown or retired ID prefix,
* an invalid status,
* an invalid revision,
* a delivery claim that the audio was heard,
* an inferred mix claim without alternatives,
* a cause hypothesis with no test and no explicit ``not_testable``,
* a performance event with no occurrence,
* a note with no pitch or no end.

Every check is written against the public names the other lanes exported; no
other lane's test or source file is imported, patched or skipped.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import numpy as np

# --- the one-process import gate: every Wave A module in a single interpreter --
from reaper_audio import AudioBuffer, MeasurementReport, MetricValue, Section
from reaper_audio.analysis import event_model
from reaper_audio.context import audio as audio_module
from reaper_audio.context import errors as errors_module
from reaper_audio.context import ids as ids_module
from reaper_audio.context import revision as revision_module
from reaper_audio.context import schema as schema_module
from reaper_audio.context.audio import (
    CaptureMethod,
    CaptureStageUnsupportedError,
    ChannelView,
    DeliveryEvidence,
    DeliveryStatus,
    SignalWindow,
    Stage,
    validate_stage_method,
)
from reaper_audio.context.errors import (
    ContractValidationError,
    MissingTimeBasisError,
    SchemaVersionError,
    UnknownPrefixError,
)
from reaper_audio.context.ids import (
    DEPRECATED_PREFIXES,
    PREFIXES,
    derive_id,
    is_derived_prefix,
    new_id,
    parse_id,
)
from reaper_audio.context.revision import (
    fingerprint_fields,
    from_project_revision,
    same_content,
)
from reaper_audio.context.schema import (
    SCHEMA_VERSION,
    RecordEnvelope,
    RecordKind,
    RevisionRef,
    dumps_record,
    loads_record,
)
from reaper_audio.mix import models as mix_models
from reaper_audio.mix.models import (
    AlternativeGroup,
    CauseFamily,
    ClaimClass,
    ClaimConfidence,
    EvidenceGrade,
    IssueClass,
    IssueHypothesis,
    LevelReference,
    MixClaim,
    MixMethod,
    MixScope,
    ScopeEntity,
)
from reaper_audio.timebase import (
    Interval,
    MappingStatus,
    TimeBasis,
    TimeRef,
    TimeUnit,
)

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "reaper_audio"

#: The prefix vocabulary the R3 ledger assigns to Wave A records.  ``hypgrp`` is
#: listed because R3-04 hands ``hypgrp_<ulid>`` to the R3-05 registry.
REQUIRED_PREFIXES: tuple[str, ...] = (
    "sc",
    "mo",
    "clm",
    "hyp",
    "hypgrp",
    "ver",
    "ref",
    "evt",
    "prf",
    "occ",
    "peg",
    "sco",
    "ast_sha256",
    "cap",
    "obs",
)

PROJECT = "reaper:{6A1B2C3D-0000-4000-8000-000000000001}:/songs/song.rpp"
SHA_A = "a" * 64
SHA_B = "b" * 64
ASSET_A = f"ast_sha256_{SHA_A}"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def _revision(
    *, scope: str = "full", fingerprint: str | None = None, counter: int = 41
) -> RevisionRef:
    return RevisionRef(
        project_id=PROJECT,
        instance_id="pri_01HZZZ",
        epoch=3,
        counter=counter,
        fingerprint=fingerprint or f"sha256:{SHA_A}",
        fingerprint_scope=scope,
    )


def _window() -> SignalWindow:
    return SignalWindow(
        project_start_s=40.0,
        project_end_s=48.0,
        file_start_s=0.0,
        file_end_s=8.0,
        boundary="bar",
        mapping_status=MappingStatus.verified,
    )


def _interval(start: float = 0.0, end: float = 0.25) -> Interval:
    return Interval(
        start=TimeRef(basis=TimeBasis.file_s, value=start, unit=TimeUnit.s),
        end=TimeRef(basis=TimeBasis.file_s, value=end, unit=TimeUnit.s),
        anchor="attack_start",
    )


def _scope(stage: Stage = Stage.master_mix_post_fx) -> MixScope:
    return MixScope(
        scope_id="sc_01J8ZK9Q7M4X2V6B3N5C8D1F7H",
        project_id=PROJECT,
        revision=_revision(),
        entities=(ScopeEntity(entity_id="trk_master_01J8ZK", track_role="master"),),
        stage=stage,
        window=_window(),
        channel_view=ChannelView.native,
        level_reference=LevelReference(),
    )


def _claim(**overrides: object) -> MixClaim:
    values: dict[str, object] = {
        "claim_id": "clm_01J8ZK9Q7M4X2V6B3N5C8D1F7H",
        "statement_template": "the low end is {level}",
        "claim_class": ClaimClass.mix_inferred,
        "construct_ids": ("C1_low_end_weight",),
        "scope": _scope(),
        "evidence_ids": ("mea_01J8ZK9Q7M4X2V6B3N5C8D1F7H",),
        "evidence_grade": EvidenceGrade.A_measured_dsp,
        "method": MixMethod(name="band_energy", version="1.0"),
        "confidence": ClaimConfidence(basis="none"),
        "alternatives": AlternativeGroup(
            group_id="hypgrp_01J8ZK9Q7M4X2V6B3N5C8D1F7H",
            members=(
                "hyp_01J8ZK9Q7M4X2V6B3N5C8D1F70",
                "hyp_01J8ZK9Q7M4X2V6B3N5C8D1F71",
            ),
        ),
    }
    values.update(overrides)
    return MixClaim(**values)  # type: ignore[arg-type]


def _hypothesis(**overrides: object) -> IssueHypothesis:
    values: dict[str, object] = {
        "hypothesis_id": "hyp_01J8ZK9Q7M4X2V6B3N5C8D1F7H",
        "issue_class": IssueClass.boomy,
        "construct_ids": ("C1_low_end_weight",),
        "scope": _scope(),
        "proposition": "the kick and bass share a 60 Hz bump",
        "cause_family": CauseFamily.eq,
        "mechanism": "overlapping low shelf on two tracks",
        "not_testable": True,
    }
    values.update(overrides)
    return IssueHypothesis(**values)  # type: ignore[arg-type]


def _module_sources() -> Iterator[tuple[Path, str]]:
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path, path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. one process, one definition per shared type
# --------------------------------------------------------------------------- #


def test_all_wave_a_modules_import_in_one_process() -> None:
    """Importing every lane's module together must not shadow a shared type."""
    modules = (
        errors_module,
        ids_module,
        schema_module,
        audio_module,
        revision_module,
        event_model,
        mix_models,
    )
    assert all(module.__name__.startswith("reaper_audio") for module in modules)

    # The event model re-exports the canonical time types rather than copies.
    assert event_model.TimeRef is TimeRef
    assert event_model.Interval is Interval
    # The mix model consumes the audio lane's enum object itself.
    assert mix_models.Stage is Stage
    # One error hierarchy for every lane.
    assert issubclass(UnknownPrefixError, errors_module.ContractError)
    assert issubclass(MissingTimeBasisError, errors_module.ContractError)
    assert issubclass(CaptureStageUnsupportedError, ContractValidationError)


@pytest.mark.parametrize(
    ("class_name", "owner"),
    [
        ("TimeRef", "timebase.py"),
        ("Interval", "timebase.py"),
        ("RevisionRef", "context/schema.py"),
        ("Stage", "context/audio.py"),
    ],
)
def test_shared_classes_are_defined_exactly_once(class_name: str, owner: str) -> None:
    pattern = re.compile(rf"^class\s+{class_name}\b", re.MULTILINE)
    found: list[str] = []
    for path, text in _module_sources():
        if pattern.search(text):
            found.append(path.relative_to(SRC_ROOT).as_posix())

    assert found == [owner], (
        f"{class_name} must be defined once in {owner}, found {found}. "
        "A second definition means two lanes invented the same domain type."
    )


def test_gesture_prefix_is_peg_everywhere() -> None:
    assert ids_module.GESTURE_PREFIX == "peg"
    assert event_model.PerceivedGesture.__name__ == "PerceivedGesture"
    for path, text in _module_sources():
        assert not re.search(r'["\']ges_[A-Za-z0-9<{]', text), (
            f"{path.name} still emits or accepts the retired ges_ prefix"
        )


# --------------------------------------------------------------------------- #
# 2. revision, schema and strict JSON
# --------------------------------------------------------------------------- #


def test_record_envelope_round_trip_with_and_without_revision() -> None:
    with_revision = RecordEnvelope(
        record_kind=RecordKind.capture,
        record_id="cap_01HZZZ",
        revision=_revision(),
        producer="pytest",
    )
    without_revision = RecordEnvelope(
        record_kind=RecordKind.delivery,
        record_id="dlv_01HZZZ",
    )

    for envelope in (with_revision, without_revision):
        payload = envelope.to_dict()
        # The wire format must survive the strictest encoder the project uses.
        strict = json.dumps(payload, sort_keys=True, allow_nan=False)
        assert loads_record(dumps_record(envelope)) == envelope
        assert json.loads(strict) == json.loads(dumps_record(envelope))

    assert with_revision.to_dict()["revision"]["fingerprint_scope"] == "full"
    assert without_revision.to_dict()["revision"] is None
    assert with_revision.schema_version == SCHEMA_VERSION == 1


def test_invalid_revision_is_rejected() -> None:
    with pytest.raises(ContractValidationError) as info:
        RecordEnvelope(
            record_kind=RecordKind.capture,
            record_id="cap_01HZZZ",
            revision={"project_id": PROJECT, "fingerprint": ""},  # type: ignore[arg-type]
        )
    assert info.value.field == "fingerprint"

    with pytest.raises(ContractValidationError) as bad_scope:
        RevisionRef(
            project_id=PROJECT,
            instance_id=None,
            epoch=0,
            counter=1,
            fingerprint="sha256:" + SHA_A,
            fingerprint_scope="partial",
        )
    assert bad_scope.value.field == "fingerprint_scope"

    with pytest.raises(SchemaVersionError):
        RecordEnvelope.from_dict(
            {"record_kind": "capture", "record_id": "cap_1", "schema_version": 99}
        )


# --------------------------------------------------------------------------- #
# 3. ID registry is the single source of prefixes
# --------------------------------------------------------------------------- #


def test_every_prefix_used_by_wave_a_is_registered() -> None:
    missing = sorted(prefix for prefix in REQUIRED_PREFIXES if prefix not in PREFIXES)
    assert not missing, (
        "prefixes used by audio/event/mix are absent from context.ids.PREFIXES: "
        f"{missing}. They can be written into records but can never be minted, "
        "parsed or validated, so a journal replay cannot recompute the id."
    )


def test_mix_id_prefixes_are_known_to_the_registry() -> None:
    """Every literal ``_prefixed(...)`` prefix in the mix lane must be registered."""
    source = (SRC_ROOT / "mix" / "models.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    used: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_prefixed"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            used.add(node.args[1].value)

    assert used, "no _prefixed() call sites were found; the scan itself broke"
    unregistered = sorted(used - PREFIXES)
    assert not unregistered, (
        "mix/models.py mints ids with prefixes the registry does not know: "
        f"{unregistered}"
    )


def test_retired_gesture_prefix_stays_retired() -> None:
    assert "ges" in DEPRECATED_PREFIXES
    assert "ges" not in PREFIXES
    with pytest.raises(UnknownPrefixError) as info:
        parse_id("ges_" + "0" * 26)
    assert info.value.next_action == "use peg_"


def test_ast_sha256_is_content_addressed_only() -> None:
    prefix, body = parse_id(f"ast_sha256_{SHA_A}")
    assert (prefix, body) == ("ast_sha256", SHA_A)
    # Content-addressed, so the registry treats it as a derived prefix…
    assert is_derived_prefix("ast_sha256") is True
    # …but neither minter may invent a body: the bytes decide the id.
    with pytest.raises(ContractValidationError):
        new_id("ast_sha256")

    # A 26-character ULID-shaped body is not an asset identity.
    with pytest.raises(ContractValidationError) as short:
        parse_id("ast_sha256_" + "A" * 26)
    assert short.value.code == "invalid_id"

    with pytest.raises(ContractValidationError):
        derive_id("ast_sha256", {"path": "kick.wav"}, "1.0", "sha256:" + SHA_A)


def test_unknown_prefix_is_rejected_for_minting_and_parsing() -> None:
    with pytest.raises(UnknownPrefixError) as minted:
        new_id("nope")
    assert minted.value.code == "unknown_id_prefix"

    with pytest.raises(UnknownPrefixError):
        parse_id("nope_" + "0" * 26)

    assert new_id("sc_").startswith("sc_")
    assert new_id("sc")[:3] == "sc_"


# --------------------------------------------------------------------------- #
# 4. time, stage and delivery seams
# --------------------------------------------------------------------------- #


def test_time_value_without_basis_is_rejected() -> None:
    with pytest.raises(MissingTimeBasisError) as info:
        TimeRef(basis=None, value=0.0, unit=TimeUnit.s)  # type: ignore[arg-type]
    assert info.value.code == "missing_time_basis"
    assert info.value.field == "basis"

    # A mapping-based clock with no map is just as unusable as no basis at all.
    with pytest.raises(MissingTimeBasisError):
        TimeRef(basis=TimeBasis.source_s, value=1.0, unit=TimeUnit.s)


def test_stage_method_compatibility() -> None:
    validate_stage_method(Stage.master_mix_post_fx, CaptureMethod.native_render)

    with pytest.raises(CaptureStageUnsupportedError) as info:
        validate_stage_method(Stage.master_mix_post_fx, CaptureMethod.audio_accessor)
    assert info.value.code == "capture_stage_unsupported"
    assert info.value.field == "method"

    # Eight stages, no more and no less, and every one has a method row.
    assert len(Stage) == 8
    assert set(audio_module.STAGE_METHODS) == set(Stage)


def test_delivery_heard_only_after_verified_forwarding() -> None:
    heard = DeliveryEvidence(
        status=DeliveryStatus.forwarded_verified,
        checked_at="2026-11-12T10:05:00+03:00",
        method="native_audio_part",
    )
    assert heard.heard is True

    for status in (
        DeliveryStatus.uri_only,
        DeliveryStatus.forwarded_unverified,
        DeliveryStatus.not_requested,
        DeliveryStatus.unsupported,
        DeliveryStatus.failed,
    ):
        assert DeliveryEvidence(status=status).heard is False


def test_revision_predicates_separate_identity_from_content() -> None:
    placeholder = from_project_revision(PROJECT, "/songs/song.rpp", 41)
    assert placeholder.fingerprint_scope == "coarse"
    assert not same_content(placeholder, placeholder)

    full = _revision()
    assert same_content(full, _revision(counter=99))

    coarse = _revision(scope="coarse")
    assert not same_content(coarse, coarse)

    measured = from_project_revision(
        PROJECT, "/songs/song.rpp", 41, fingerprint_fields(PROJECT, {"tracks": 33})
    )
    assert measured.fingerprint_scope == "full"
    # A full fingerprint without an instance/epoch cannot prove continuity:
    # content equality is only reachable through a complete identity.
    assert not same_content(measured, measured)
    identified = from_project_revision(
        PROJECT, "/songs/song.rpp", 41, fingerprint_fields(PROJECT, {"tracks": 33}),
        instance_id="pri_test", epoch=1,
    )
    assert same_content(identified, identified)
    assert not same_content(measured, full)


# --------------------------------------------------------------------------- #
# 5. mix and event invariants that must not be softened
# --------------------------------------------------------------------------- #


def test_mix_scope_uses_the_audio_lane_stage() -> None:
    scope = _scope()
    assert scope.stage is Stage.master_mix_post_fx
    assert type(scope.stage) is Stage

    fields = mix_models.MixScope.__dataclass_fields__
    assert fields["stage"].type in (Stage, "Stage")
    # The window is the audio lane's type, not a mix-local copy.
    rebuilt = MixScope.from_dict(scope.to_dict())
    assert rebuilt.window == scope.window
    assert isinstance(rebuilt.window, SignalWindow)


def test_inferred_claim_without_alternatives_is_rejected() -> None:
    with pytest.raises(ContractValidationError) as info:
        _claim(alternatives=None)
    assert info.value.code == "mix_invariant"
    assert info.value.field == "alternatives"

    # Two members are the floor: one explanation plus a straw man is not a set.
    with pytest.raises(ContractValidationError):
        _claim(
            alternatives=AlternativeGroup(
                group_id="hypgrp_01J8ZK9Q7M4X2V6B3N5C8D1F7H",
                members=("hyp_01J8ZK9Q7M4X2V6B3N5C8D1F70",),
            )
        )

    assert _claim().alternatives is not None
    detected = _claim(claim_class=ClaimClass.mix_detected, alternatives=None)
    assert detected.alternatives is None


def test_hypothesis_needs_a_test_or_an_explicit_not_testable() -> None:
    with pytest.raises(ContractValidationError) as info:
        _hypothesis(not_testable=False)
    assert info.value.code == "hypothesis_not_testable"
    assert info.value.field == "discriminating_tests"

    assert _hypothesis(not_testable=True).not_testable is True

    tested = _hypothesis(
        not_testable=False,
        discriminating_tests=(
            mix_models.DiscriminatingTest(
                test_id="t1",
                description="narrow 2 dB at 60 Hz on the bass",
                expected_if_true="the boom decreases",
                expected_if_false="the boom stays",
            ),
        ),
    )
    assert len(tested.discriminating_tests) == 1


def test_performance_event_requires_an_occurrence() -> None:
    common: dict[str, object] = {
        "kind": "pick_attack",
        "interval": _interval(),
        "method": event_model.MethodRef(name="transient", version="1.0"),
        "usage_scope": event_model.UsageScope(),
    }

    with pytest.raises(ContractValidationError) as info:
        event_model.PerformanceEvent(
            event_id="prf_01J8ZK9Q7M4X2V6B3N5C8D1F7H",
            occurrence_id="evt_01J8ZK9Q7M4X2V6B3N5C8D1F7H",
            **common,  # type: ignore[arg-type]
        )
    assert info.value.code == "event_invariant"
    assert info.value.field == "occurrence_id"

    with pytest.raises(ContractValidationError):
        event_model.PerformanceEvent(
            event_id="evt_01J8ZK9Q7M4X2V6B3N5C8D1F7H",
            occurrence_id="occ_01J8ZK9Q7M4X2V6B3N5C8D1F7H",
            **common,  # type: ignore[arg-type]
        )

    accepted = event_model.PerformanceEvent(
        event_id="prf_01J8ZK9Q7M4X2V6B3N5C8D1F7H",
        occurrence_id="occ_01J8ZK9Q7M4X2V6B3N5C8D1F7H",
        **common,  # type: ignore[arg-type]
    )
    assert accepted.occurrence_id.startswith("occ_")


def test_note_requires_pitch_and_duration() -> None:
    base: dict[str, object] = {
        "event_id": "prf_01J8ZK9Q7M4X2V6B3N5C8D1F7H",
        "occurrence_id": "occ_01J8ZK9Q7M4X2V6B3N5C8D1F7H",
        "kind": "note",
        "interval": _interval(),
        "method": event_model.MethodRef(name="transcribe", version="1.0"),
        "usage_scope": event_model.UsageScope(pitch=True, duration=True),
    }

    with pytest.raises(ContractValidationError) as no_pitch:
        event_model.PerformanceEvent(**base)  # type: ignore[arg-type]
    assert no_pitch.value.field == "pitch"

    with pytest.raises(ContractValidationError) as no_duration:
        event_model.PerformanceEvent(
            pitch=event_model.Pitch(midi=40),
            **base,  # type: ignore[arg-type]
        )
    assert no_duration.value.field == "duration"

    accepted = event_model.PerformanceEvent(
        pitch=event_model.Pitch(midi=40),
        duration=event_model.EventDuration(start_s=0.0, end_s=0.25),
        **base,  # type: ignore[arg-type]
    )
    assert accepted.duration is not None


def test_signal_event_assets_are_content_addressed() -> None:
    event = event_model.SignalEvent(
        event_id="evt_01J8ZK9Q7M4X2V6B3N5C8D1F7H",
        asset_id=ASSET_A,
        interval=_interval(),
        kind=event_model.SignalKind.onset,
        strength=0.8,
        method=event_model.MethodRef(name="onset", version="1.0"),
    )
    assert event.asset_id == ASSET_A

    with pytest.raises(ContractValidationError):
        event_model.SignalEvent(
            event_id="evt_01J8ZK9Q7M4X2V6B3N5C8D1F7H",
            asset_id="ast_sha256_" + "A" * 26,
            interval=_interval(),
            kind=event_model.SignalKind.onset,
            strength=0.8,
            method=event_model.MethodRef(name="onset", version="1.0"),
        )


# --------------------------------------------------------------------------- #
# 6. import graph and backwards compatibility
# --------------------------------------------------------------------------- #

_WAVE_A_MODULES: tuple[str, ...] = (
    "reaper_audio.context.errors",
    "reaper_audio.timebase",
    "reaper_audio.context.schema",
    "reaper_audio.context.ids",
    "reaper_audio.context.revision",
    "reaper_audio.context.audio",
    "reaper_audio.analysis.event_model",
    "reaper_audio.mix.models",
    "reaper_audio.mix",
)


def _resolve_import(module: str, node: ast.ImportFrom) -> str | None:
    if node.level:
        parts = module.split(".")[:-1]
        drop = node.level - 1
        if drop:
            parts = parts[:-drop]
        if node.module:
            parts = parts + node.module.split(".")
        return ".".join(parts)
    return node.module


def _module_file(module: str) -> Path:
    relative = module.split(".")[1:]
    base = SRC_ROOT.joinpath(*relative)
    if base.with_suffix(".py").exists():
        return base.with_suffix(".py")
    return base / "__init__.py"


def test_wave_a_import_graph_is_acyclic() -> None:
    known = set(_WAVE_A_MODULES)
    edges: dict[str, set[str]] = {module: set() for module in _WAVE_A_MODULES}

    for module in _WAVE_A_MODULES:
        tree = ast.parse(_module_file(module).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            target = _resolve_import(module, node)
            if target in known and target != module:
                edges[module].add(target)

    # Kahn's algorithm: anything left unvisited sits on a cycle.
    indegree = {module: 0 for module in known}
    for targets in edges.values():
        for target in targets:
            indegree[target] += 1

    ready = [module for module, degree in indegree.items() if degree == 0]
    visited: list[str] = []
    while ready:
        current = ready.pop()
        visited.append(current)
        for target in edges[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)

    assert set(visited) == known, (
        "wave A import cycle among: "
        f"{sorted(known - set(visited))} (edges={ {k: sorted(v) for k, v in edges.items()} })"
    )


def test_mix_lane_does_not_depend_on_the_analysis_package() -> None:
    tree = ast.parse((SRC_ROOT / "mix" / "models.py").read_text(encoding="utf-8"))
    targets = {
        _resolve_import("reaper_audio.mix.models", node)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    targets |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not [target for target in targets if target and target.startswith("reaper_audio.analysis")]


def test_old_public_types_still_import() -> None:
    section = Section(0.0, 4.0)
    metric = MetricValue(value=-14.0, unit="db")
    report = MeasurementReport(
        source="master.wav",
        sample_rate=48000,
        channels=2,
        duration_s=4.0,
        section=section,
        metrics={"peak": metric},
    )
    buffer = AudioBuffer(samples=np.zeros((8, 1)), sample_rate=48000)
    assert report.section is section
    assert buffer.frames == 8

    session = pytest.importorskip(
        "reaper_audio.daw.session", reason="daw.session needs the optional audio stack"
    )
    legacy = session.ProjectRevision.from_dict(
        {"project_id": PROJECT, "change_count": 41, "path": "/songs/song.rpp"}
    )
    adapted = from_project_revision(
        str(legacy.project_id), legacy.path, int(legacy.change_count)
    )
    assert adapted.counter == legacy.change_count
    assert adapted.project_id == legacy.project_id
