"""Contract tests for the Wave A mix diagnosis types.

These tests pin behaviour, not prose: every invariant the mix contract claims
to enforce is exercised through the public constructors, and every record that
crosses a process boundary is round-tripped through strict JSON.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TypeVar

import pytest

from reaper_audio.context.audio import ChannelView, SignalWindow, Stage
from reaper_audio.context.errors import ContractValidationError
from reaper_audio.context.schema import RevisionRef
from reaper_audio.mix import (
    AlternativeGroup,
    AssetRef,
    CauseFamily,
    ClaimClass,
    ClaimConfidence,
    ClaimStatus,
    DiscriminatingTest,
    EvidenceGrade,
    IntentRef,
    IssueClass,
    IssueHypothesis,
    LevelReference,
    ListenerInfo,
    MetricReading,
    MixClaim,
    MixMethod,
    MixObservation,
    MixScope,
    ObservationKind,
    PostChangeCheck,
    Prediction,
    RoleRef,
    ScopeEntity,
    SectionRef,
    Severity,
    SignalPathRefs,
    dumps_mix_record,
    loads_mix_record,
)
from reaper_audio.mix import models as mix_models
from reaper_audio.timebase import MappingStatus

SHA_A = "a" * 64
SHA_B = "b" * 64
ASSET_A = f"ast_sha256_{SHA_A}"
ASSET_B = f"ast_sha256_{SHA_B}"
SCOPE_ID = "sc_01J8ZK9Q7M4X2V6B3N5C8D1F7H"
CLAIM_ID = "clm_01J8ZK9Q7M4X2V6B3N5C8D1F7H"
HYP_ID = "hyp_01J8ZK9Q7M4X2V6B3N5C8D1F7H"
OBS_ID = "mo_01J8ZK9Q7M4X2V6B3N5C8D1F7H"
CHECK_ID = "ver_01J8ZK9Q7M4X2V6B3N5C8D1F7H"
GROUP_ID = "hypgrp_01J8ZK9Q7M4X2V6B3N5C8D1F7H"
RIVAL_ID = "hyp_01J8ZK9Q7M4X2V6B3N5C8D1F8J"


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def _revision() -> RevisionRef:
    return RevisionRef(
        project_id="reaper:master-guid:/songs/song.rpp",
        instance_id="pri_01HZZZ",
        epoch=2,
        counter=11,
        fingerprint="9f2c1a77d0b3e455",
    )


def _window(**overrides: object) -> SignalWindow:
    values: dict[str, object] = {
        "project_start_s": 12.0,
        "project_end_s": 16.0,
        "file_start_s": 0.0,
        "file_end_s": 4.0,
        "boundary": "bar",
        "mapping_status": MappingStatus.verified,
        "uncertainty_ms": 0.5,
    }
    values.update(overrides)
    return SignalWindow(**values)  # type: ignore[arg-type]


def _scope_values() -> dict[str, object]:
    return {
        "scope_id": SCOPE_ID,
        "project_id": "prj_01J8ZK9Q7M4X2V6B3N5C8D1F7H",
        "revision": _revision(),
        "entities": (
            ScopeEntity(
                entity_id="trk_01J8ZK9Q7M4X2V6B3N5C8D1F7H",
                track_role="track",
                role=RoleRef(value="kick", source="human", confidence=0.9),
                asset_id=ASSET_A,
            ),
        ),
        "stage": Stage.track_stem_post_fx,
        "window": _window(),
        "channel_view": ChannelView.mono_sum,
        "level_reference": LevelReference(mode="unknown"),
        "signal_path_refs": SignalPathRefs(
            track_guids=("{8F0E2C11-0000-4000-8000-000000000001}",),
            send_ids=("snd_01J8ZK9Q7M4X2V6B3N5C8D1F7H",),
            fx_guids=("fx_01J8ZK9Q7M4X2V6B3N5C8D1F7H",),
        ),
        "section": SectionRef(
            section_id="sec_01J8ZK9Q7M4X2V6B3N5C8D1F7H",
            label="chorus",
            label_source="marker",
            level="section",
        ),
    }


def _scope(**overrides: object) -> MixScope:
    values = _scope_values()
    values.update(overrides)
    return MixScope(**values)  # type: ignore[arg-type]


def _method() -> MixMethod:
    return MixMethod(name="spectral_masking", version="1.0", params_hash="sha256:abc")


def _metric(**overrides: object) -> MetricReading:
    values: dict[str, object] = {
        "name": "masking_index",
        "family": "masking",
        "value": 2.5,
        "unit": "dB",
        "method": _method(),
        "coverage": 0.8,
        "uncertainty": 0.3,
    }
    values.update(overrides)
    return MetricReading(**values)  # type: ignore[arg-type]


def _observation_values() -> dict[str, object]:
    return {
        "observation_id": OBS_ID,
        "kind": ObservationKind.measurement,
        "scope": _scope(),
        "construct_ids": ("c_masking_low_mid",),
        "evidence_grade": EvidenceGrade.A_measured_dsp,
        "status": "measured",
        "metric": _metric(),
        "asset": AssetRef(asset_id=ASSET_A, sha256=SHA_A, capture_id="cap_01HZZZ"),
        "limitations": ("mono_sum only",),
        "freshness": "as_of_rev",
    }


def _observation(**overrides: object) -> MixObservation:
    values = _observation_values()
    values.update(overrides)
    return MixObservation(**values)  # type: ignore[arg-type]


def _claim_values() -> dict[str, object]:
    return {
        "claim_id": CLAIM_ID,
        "statement_template": "kick masks the bass between 60 and 90 Hz",
        "claim_class": ClaimClass.mix_detected,
        "construct_ids": ("c_masking_low_mid",),
        "scope": _scope(),
        "evidence_ids": (OBS_ID,),
        "evidence_grade": EvidenceGrade.A_measured_dsp,
        "method": _method(),
        "confidence": ClaimConfidence(basis="agreement", value=0.7, n=3),
        "severity": Severity(level="moderate", anchored_by="heuristic", value=0.6),
    }


def _claim(**overrides: object) -> MixClaim:
    values = _claim_values()
    values.update(overrides)
    return MixClaim(**values)  # type: ignore[arg-type]


def _test(**overrides: object) -> DiscriminatingTest:
    values: dict[str, object] = {
        "test_id": "test_mute_kick",
        "description": "mute the kick for one bar",
        "expected_if_true": "the bass opens up in 60-90 Hz",
        "expected_if_false": "the bass stays masked",
    }
    values.update(overrides)
    return DiscriminatingTest(**values)  # type: ignore[arg-type]


def _hypothesis_values() -> dict[str, object]:
    return {
        "hypothesis_id": HYP_ID,
        "issue_class": IssueClass.muddy,
        "construct_ids": ("c_masking_low_mid",),
        "scope": _scope(),
        "proposition": "the kick is masking the bass",
        "cause_family": CauseFamily.masking,
        "mechanism": "spectral overlap around 70 Hz",
        "predictions": (
            Prediction(if_true="muting the kick opens the bass", if_false="nothing changes"),
        ),
        "supports": (OBS_ID,),
        "discriminating_tests": (_test(),),
    }


def _hypothesis(**overrides: object) -> IssueHypothesis:
    values = _hypothesis_values()
    values.update(overrides)
    return IssueHypothesis(**values)  # type: ignore[arg-type]


def _check_values() -> dict[str, object]:
    return {
        "check_id": CHECK_ID,
        "claim_id": CLAIM_ID,
        "scope": _scope(),
        "target_metric": _metric(),
        "expected_direction": "decrease",
        "status": "planned",
    }


def _check(**overrides: object) -> PostChangeCheck:
    values = _check_values()
    values.update(overrides)
    return PostChangeCheck(**values)  # type: ignore[arg-type]


RecordT = TypeVar("RecordT")


def _round_trip(record: RecordT, cls: type[RecordT]) -> RecordT:
    result: RecordT = loads_mix_record(cls, dumps_mix_record(record))
    return result


# --------------------------------------------------------------------------- #
# scope
# --------------------------------------------------------------------------- #


def test_scope_requires_at_least_one_entity() -> None:
    with pytest.raises(ContractValidationError, match="at least one entity"):
        _scope(entities=())


def test_scope_requires_window() -> None:
    with pytest.raises(ContractValidationError, match="window"):
        _scope(window=None)


def test_scope_requires_channel_view() -> None:
    with pytest.raises(ContractValidationError, match="channel_view"):
        _scope(channel_view=None)


def test_scope_rejects_duplicate_entities() -> None:
    entity = ScopeEntity(entity_id="trk_A", track_role="track")
    with pytest.raises(ContractValidationError, match="unique"):
        _scope(entities=(entity, entity))


def test_scope_stage_pair_must_name_two_different_stages() -> None:
    with pytest.raises(ContractValidationError, match="two different stages"):
        _scope(stage_pair=(Stage.track_stem_post_fx, Stage.track_stem_post_fx))


def test_scope_stage_pair_accepts_two_stages() -> None:
    scope = _scope(stage_pair=(Stage.track_stem_post_fx, Stage.bus_stem_post_fx))
    assert scope.stage_pair == (Stage.track_stem_post_fx, Stage.bus_stem_post_fx)
    assert scope.stage is Stage.track_stem_post_fx


def test_scope_rejects_malformed_scope_id() -> None:
    with pytest.raises(ContractValidationError, match="sc_"):
        _scope(scope_id="scope-1")


def test_scope_rejects_unknown_track_role() -> None:
    with pytest.raises(ContractValidationError, match="track_role"):
        ScopeEntity(entity_id="trk_A", track_role="instrument")


def test_scope_rejects_matched_level_without_a_window() -> None:
    with pytest.raises(ContractValidationError, match="match_window"):
        LevelReference(mode="matched", gain_db=-1.5)


def test_scope_rejects_absolute_level_without_lufs() -> None:
    with pytest.raises(ContractValidationError, match="absolute_lufs"):
        LevelReference(mode="absolute")


def test_scope_round_trips_through_json() -> None:
    scope = _scope()
    restored = _round_trip(scope, MixScope)
    assert restored == scope
    assert isinstance(restored, MixScope)
    assert restored.window == scope.window
    assert restored.revision == scope.revision
    assert restored.section == scope.section


def test_scope_is_frozen() -> None:
    scope = _scope()
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.stage = Stage.source_file  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# observations
# --------------------------------------------------------------------------- #


def test_measurement_observation_requires_a_metric() -> None:
    with pytest.raises(ContractValidationError, match="metric"):
        _observation(metric=None)


def test_measurement_observation_rejects_listener_grade() -> None:
    with pytest.raises(ContractValidationError, match="graded A"):
        _observation(evidence_grade=EvidenceGrade.C_listener_unverified)


def test_listener_observation_requires_listener_info() -> None:
    with pytest.raises(ContractValidationError, match="listener"):
        _observation(
            kind=ObservationKind.listener,
            metric=None,
            evidence_grade=EvidenceGrade.C_listener_unverified,
            status="model_observation",
        )


def test_listener_observation_rejects_measured_grade() -> None:
    with pytest.raises(ContractValidationError, match="graded B"):
        _observation(
            kind=ObservationKind.listener,
            metric=None,
            evidence_grade=EvidenceGrade.A_measured_dsp,
            status="model_observation",
            listener=ListenerInfo(engine="qwen-audio"),
        )


def test_listener_grade_c_needs_no_pinned_identity() -> None:
    observation = _observation(
        kind=ObservationKind.listener,
        metric=None,
        evidence_grade=EvidenceGrade.C_listener_unverified,
        status="model_observation",
        listener=ListenerInfo(engine="qwen-audio", limits=("no absolute level",)),
    )
    assert observation.evidence_grade is EvidenceGrade.C_listener_unverified


def test_listener_grade_b_requires_pinned_identity() -> None:
    with pytest.raises(ContractValidationError, match="pinned listener identity"):
        _observation(
            kind=ObservationKind.listener,
            metric=None,
            evidence_grade=EvidenceGrade.B_listener_verified,
            status="model_observation",
            listener=ListenerInfo(engine="qwen-audio", model_revision="r1"),
        )


def test_listener_grade_b_accepts_a_pinned_identity() -> None:
    observation = _observation(
        kind=ObservationKind.listener,
        metric=None,
        evidence_grade=EvidenceGrade.B_listener_verified,
        status="model_observation",
        listener=ListenerInfo(
            engine="qwen-audio",
            model_revision="r1",
            weights_sha256=SHA_B,
            prompt_sha256=SHA_A,
            context_injected=True,
        ),
    )
    assert observation.listener is not None
    assert observation.listener.weights_sha256 == SHA_B


def test_observation_rejects_unknown_status() -> None:
    with pytest.raises(ContractValidationError, match="status"):
        _observation(status="guessed")


def test_observation_requires_a_construct() -> None:
    with pytest.raises(ContractValidationError, match="construct"):
        _observation(construct_ids=())


def test_unavailable_measurement_may_omit_the_metric() -> None:
    observation = _observation(metric=None, status="unavailable")
    assert observation.metric is None


def test_observation_round_trips_through_json() -> None:
    observation = _observation()
    restored = _round_trip(observation, MixObservation)
    assert restored == observation
    assert restored.kind is ObservationKind.measurement
    assert restored.asset == observation.asset


# --------------------------------------------------------------------------- #
# claims
# --------------------------------------------------------------------------- #


def test_claim_requires_evidence() -> None:
    with pytest.raises(ContractValidationError, match="evidence"):
        _claim(evidence_ids=())


def test_inferred_claim_requires_alternatives() -> None:
    with pytest.raises(ContractValidationError, match="alternative group"):
        _claim(claim_class=ClaimClass.mix_inferred)


def test_inferred_claim_rejects_a_single_alternative() -> None:
    with pytest.raises(ContractValidationError, match="at least two members"):
        AlternativeGroup(group_id=GROUP_ID, members=(HYP_ID,))


def test_inferred_claim_accepts_two_alternatives() -> None:
    claim = _claim(
        claim_class=ClaimClass.mix_inferred,
        alternatives=AlternativeGroup(group_id=GROUP_ID, members=(HYP_ID, RIVAL_ID)),
    )
    assert claim.alternatives is not None
    assert len(claim.alternatives.members) == 2


def test_detected_claim_needs_no_alternatives() -> None:
    assert _claim().alternatives is None


def test_claim_carries_no_quality_or_score_field() -> None:
    claim = _claim()
    payload = claim.to_dict()
    assert "quality" not in payload
    assert "score" not in payload
    assert not hasattr(claim, "quality")
    assert not hasattr(claim, "score")
    with pytest.raises(TypeError):
        MixClaim(**{**_claim_values(), "quality": 0.8})  # type: ignore[arg-type]


def test_claim_defaults_to_asserted() -> None:
    assert _claim().status is ClaimStatus.asserted


def test_claim_rejects_unknown_status() -> None:
    with pytest.raises(ContractValidationError, match="status"):
        _claim(status="maybe")


def test_claim_round_trips_through_json() -> None:
    claim = _claim(
        claim_class=ClaimClass.mix_inferred,
        alternatives=AlternativeGroup(group_id=GROUP_ID, members=(HYP_ID, RIVAL_ID)),
        intent=IntentRef(
            goal_id="int_01J8ZK9Q7M4X2V6B3N5C8D1F7H",
            statement="keep the low end clean",
            source="owner",
            constraints=("no sidechain",),
        ),
        reference_ids=("ref_01J8ZK9Q7M4X2V6B3N5C8D1F7H",),
        status=ClaimStatus.contested,
        not_checked=("mono compatibility",),
    )
    restored = _round_trip(claim, MixClaim)
    assert restored == claim
    assert restored.claim_class is ClaimClass.mix_inferred
    assert restored.intent is not None
    assert restored.intent.constraints == ("no sidechain",)


# --------------------------------------------------------------------------- #
# hypotheses
# --------------------------------------------------------------------------- #


def test_hypothesis_without_a_test_is_rejected_with_its_own_code() -> None:
    with pytest.raises(ContractValidationError) as caught:
        _hypothesis(discriminating_tests=())
    assert caught.value.code == "hypothesis_not_testable"
    assert caught.value.field == "discriminating_tests"
    assert caught.value.next_action == "add_discriminating_test_or_mark_untestable"


def test_hypothesis_may_declare_itself_untestable() -> None:
    hypothesis = _hypothesis(discriminating_tests=(), not_testable=True)
    assert hypothesis.not_testable is True
    assert hypothesis.discriminating_tests == ()


def test_hypothesis_with_a_discriminating_test_passes() -> None:
    hypothesis = _hypothesis()
    assert hypothesis.discriminating_tests[0].test_id == "test_mute_kick"


def test_hypothesis_rejects_duplicate_test_ids() -> None:
    with pytest.raises(ContractValidationError, match="unique"):
        _hypothesis(discriminating_tests=(_test(), _test()))


def test_issue_and_cause_vocabularies_cover_the_contract() -> None:
    assert IssueClass.muddy == "muddy"
    assert IssueClass.weak_attack == "weak_attack"
    assert CauseFamily.reference_mismatch == "reference_mismatch"
    assert CauseFamily.unknown == "unknown"
    assert len(IssueClass) == 14
    assert len(CauseFamily) == 15


def test_hypothesis_round_trips_through_json() -> None:
    hypothesis = _hypothesis()
    restored = _round_trip(hypothesis, IssueHypothesis)
    assert restored == hypothesis
    assert restored.issue_class is IssueClass.muddy
    assert restored.cause_family is CauseFamily.masking
    assert restored.predictions == hypothesis.predictions


# --------------------------------------------------------------------------- #
# post-change checks
# --------------------------------------------------------------------------- #


def test_post_change_rejects_unknown_status() -> None:
    with pytest.raises(ContractValidationError, match="status"):
        _check(status="probably_fine")


def test_post_change_rejects_unknown_direction() -> None:
    with pytest.raises(ContractValidationError, match="expected_direction"):
        _check(expected_direction="sideways")


def test_post_change_verified_requires_the_observed_metric() -> None:
    with pytest.raises(ContractValidationError, match="observed metric"):
        _check(status="verified", observed=None)


def test_post_change_failed_requires_the_observed_metric() -> None:
    with pytest.raises(ContractValidationError, match="observed metric"):
        _check(status="failed", observed=None)


def test_post_change_verified_with_observation_passes() -> None:
    check = _check(status="verified", observed=_metric(value=1.1), evidence_ids=(OBS_ID,))
    assert check.status == "verified"
    assert check.observed is not None
    assert check.observed.value == 1.1


def test_post_change_requires_a_claim_id() -> None:
    with pytest.raises(ContractValidationError, match="clm_"):
        _check(claim_id="claim-1")


def test_post_change_round_trips_through_json() -> None:
    check = _check(
        status="applied",
        observed=_metric(value=1.1),
        evidence_ids=(OBS_ID,),
        notes=("EQ cut 2 dB at 70 Hz",),
    )
    restored = _round_trip(check, PostChangeCheck)
    assert restored == check
    assert restored.observed is not None
    assert restored.observed.value == 1.1
    assert restored.notes == ("EQ cut 2 dB at 70 Hz",)


# --------------------------------------------------------------------------- #
# severity and serialization discipline
# --------------------------------------------------------------------------- #


def test_severity_carries_no_score_and_no_thresholds() -> None:
    severity = Severity(level="severe", anchored_by="calibration", value=0.8, ci=(0.7, 0.9))
    assert not hasattr(severity, "score")
    assert set(severity.to_dict()) == {"level", "anchored_by", "value", "ci"}
    assert [name for name in dir(mix_models) if "THRESHOLD" in name.upper()] == []
    assert "score" not in {field.name for field in dataclasses.fields(Severity)}


def test_severity_rejects_unknown_level() -> None:
    with pytest.raises(ContractValidationError, match="level"):
        Severity(level="catastrophic")


def test_severity_rejects_a_reversed_interval() -> None:
    with pytest.raises(ContractValidationError, match="low must be <= high"):
        Severity(level="mild", anchored_by="heuristic", ci=(0.9, 0.1))


def test_metric_rejects_non_finite_values() -> None:
    with pytest.raises(ContractValidationError, match="finite"):
        _metric(value=float("nan"))


def test_json_dumps_refuse_nan() -> None:
    assert mix_models._JSON_DUMPS_OPTIONS["allow_nan"] is False
    payload = json.loads(dumps_mix_record(_metric()))
    assert payload["value"] == 2.5
    with pytest.raises(ContractValidationError):
        _metric(value=float("inf"))


def test_enum_fields_serialize_as_plain_strings() -> None:
    payload = json.loads(dumps_mix_record(_scope()))
    assert payload["stage"] == "track_stem_post_fx"
    assert type(payload["stage"]) is str
    assert payload["channel_view"] == "mono_sum"
    claim_payload = json.loads(dumps_mix_record(_claim()))
    assert claim_payload["claim_class"] == "mix_detected"
    assert claim_payload["status"] == "asserted"


def test_asset_ref_enforces_the_content_address() -> None:
    with pytest.raises(ContractValidationError, match="embed sha256"):
        AssetRef(asset_id=ASSET_A, sha256=SHA_B)


# --------------------------------------------------------------------------- #
# module shape
# --------------------------------------------------------------------------- #


def test_models_module_does_not_redefine_shared_classes() -> None:
    source = inspect.getsource(mix_models)
    for forbidden in (
        "class Stage",
        "class TimeRef",
        "class RevisionRef",
        "class SignalWindow",
        "class ChannelView",
    ):
        assert forbidden not in source
    assert "import numpy" not in source
    assert "from ..analysis" not in source


def test_importing_mix_does_not_pull_the_analysis_package() -> None:
    """The mix contracts must be readable without the DSP layer.

    ``reaper_audio/__init__`` already imports numpy through the root models, so
    the assertion here is about the dependency this package adds: none.
    """
    source_root = Path(__file__).resolve().parents[1] / "src"
    code = (
        "import sys; import reaper_audio.mix as mix; "
        "assert 'reaper_audio.analysis' not in sys.modules; "
        "print(mix.MixScope.__name__)"
    )
    env = {**os.environ, "PYTHONPATH": str(source_root)}
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "MixScope"
