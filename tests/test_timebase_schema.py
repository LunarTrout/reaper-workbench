"""Contract tests for the frozen Wave A time base and context schema."""

from __future__ import annotations

import json

import numpy as np
import pytest

from reaper_audio import AudioBuffer, MeasurementReport, MetricValue, Section
from reaper_audio.context.errors import (
    ContractError,
    ContractValidationError,
    MappingUnavailableError,
    MissingTimeBasisError,
    SchemaVersionError,
)
from reaper_audio.context.schema import (
    SCHEMA_VERSION,
    RecordEnvelope,
    RecordKind,
    RevisionRef,
    dumps_record,
    loads_record,
    validate_revision,
)
from reaper_audio.timebase import (
    Interval,
    MappingStatus,
    TimeBasis,
    TimeMap,
    TimeRef,
    TimeUnit,
    compose,
    time_ref_from_dict,
    time_ref_to_dict,
    to_sample_index,
    validate_time_ref,
)


def _revision() -> RevisionRef:
    return RevisionRef(
        project_id="reaper:master-guid:/songs/song.rpp",
        instance_id="pri_01HZZZ",
        epoch=2,
        counter=11,
        fingerprint="9f2c1a77d0b3e455",
    )


def test_record_envelope_json_round_trip() -> None:
    envelope = RecordEnvelope(
        record_kind=RecordKind.capture,
        record_id="cap_01HZZZ",
        revision=_revision(),
        created_at="2026-11-12T10:00:00+03:00",
        producer="pytest",
    )
    text = dumps_record(envelope)
    data = json.loads(text)
    assert data["record_kind"] == "capture"
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["revision"]["fingerprint_scope"] == "full"
    assert "NaN" not in text
    assert loads_record(text) == envelope


def test_revision_ref_from_legacy_dict() -> None:
    revision = RevisionRef.from_dict(
        {"project_id": "reaper:g:/song.rpp", "counter": 4, "fingerprint": "abc123"}
    )
    assert revision.instance_id is None
    assert revision.epoch == 0
    assert revision.fingerprint_scope == "full"
    assert validate_revision(revision) is revision
    assert validate_revision(revision.to_dict()) == revision


def test_unknown_schema_version_is_rejected() -> None:
    with pytest.raises(SchemaVersionError) as info:
        RecordEnvelope.from_dict(
            {"record_kind": "project", "record_id": "prj_1", "schema_version": 2}
        )
    assert info.value.code == "schema_version_unsupported"
    assert info.value.field == "schema_version"
    assert info.value.to_dict()["code"] == "schema_version_unsupported"

    with pytest.raises(SchemaVersionError):
        RecordEnvelope(record_kind=RecordKind.project, record_id="prj_1", schema_version=2)


def test_missing_time_basis_is_rejected() -> None:
    with pytest.raises(MissingTimeBasisError) as info:
        TimeRef(basis=None, value=0.0, unit=TimeUnit.s)
    assert info.value.code == "missing_time_basis"
    assert info.value.field == "basis"
    assert isinstance(info.value, ContractError)
    assert isinstance(info.value, ValueError)


def test_wrong_unit_for_basis_is_rejected() -> None:
    with pytest.raises(ContractValidationError) as info:
        TimeRef(basis=TimeBasis.file_s, value=1.0, unit=TimeUnit.qn)
    assert info.value.code == "contract_validation"
    assert info.value.field == "unit"


@pytest.mark.parametrize(
    ("basis", "unit"),
    [
        (TimeBasis.source_s, TimeUnit.s),
        (TimeBasis.take_s, TimeUnit.s),
        (TimeBasis.musical_qn, TimeUnit.qn),
        (TimeBasis.bar_beat, TimeUnit.bar_beat),
    ],
)
def test_mapping_basis_requires_map_id(basis: TimeBasis, unit: TimeUnit) -> None:
    with pytest.raises(MissingTimeBasisError) as info:
        TimeRef(basis=basis, value=1.0, unit=unit)
    assert info.value.code == "missing_time_basis"
    assert info.value.field == "map_id"

    grounded = TimeRef(
        basis=basis,
        value=1.0,
        unit=unit,
        map_id="tm_song_1",
        mapping_status=MappingStatus.approx,
    )
    assert grounded.map_id == "tm_song_1"


def test_direct_basis_rejects_map_id() -> None:
    with pytest.raises(ContractValidationError) as info:
        TimeRef(basis=TimeBasis.file_s, value=1.0, unit=TimeUnit.s, map_id="tm_song_1")
    assert info.value.code == "contract_validation"
    assert info.value.field == "map_id"


def test_performed_beat_requires_hypothesis_id() -> None:
    with pytest.raises(MissingTimeBasisError) as info:
        TimeRef(basis=TimeBasis.performed_beat, value=3.5, unit=TimeUnit.beat)
    assert info.value.field == "hypothesis_id"

    grounded = TimeRef(
        basis=TimeBasis.performed_beat,
        value=3.5,
        unit=TimeUnit.beat,
        hypothesis_id="hyp_pulse_1",
    )
    assert grounded.map_id is None
    assert grounded.hypothesis_id == "hyp_pulse_1"


def test_interval_requires_one_basis_and_unit() -> None:
    start = TimeRef(basis=TimeBasis.file_s, value=0.0, unit=TimeUnit.s)
    end = TimeRef(basis=TimeBasis.project_s, value=1.0, unit=TimeUnit.s)
    with pytest.raises(ContractValidationError) as info:
        Interval(start=start, end=end)
    assert info.value.code == "contract_validation"
    assert info.value.field == "end"

    same_clock = Interval(
        start=start,
        end=TimeRef(basis=TimeBasis.file_s, value=1.0, unit=TimeUnit.s),
        anchor="attack_start",
    )
    assert same_clock.end is not None
    assert same_clock.end.value == 1.0


def test_interval_rejects_reversed_bounds_and_unknown_anchor() -> None:
    with pytest.raises(ContractValidationError):
        Interval(
            start=TimeRef(basis=TimeBasis.file_s, value=2.0, unit=TimeUnit.s),
            end=TimeRef(basis=TimeBasis.file_s, value=1.0, unit=TimeUnit.s),
        )
    with pytest.raises(ContractValidationError):
        Interval(
            start=TimeRef(basis=TimeBasis.file_s, value=0.0, unit=TimeUnit.s),
            anchor="somewhere_else",
        )


def test_compose_identity_preserves_value_and_uncertainty() -> None:
    ref = TimeRef(
        basis=TimeBasis.project_s,
        value=12.5,
        unit=TimeUnit.s,
        uncertainty_ms=4.0,
        mapping_status=MappingStatus.verified,
    )
    time_map = TimeMap(
        map_id="tm_song_1",
        project_id="reaper:g:/song.rpp",
        version=1,
        method="identity",
        max_residual_ms=0.0,
    )
    composed = compose(ref, TimeBasis.project_s, time_map)
    assert composed.basis is TimeBasis.project_s
    assert composed.value == 12.5
    assert composed.unit is TimeUnit.s
    assert composed.uncertainty_ms == 4.0
    assert composed.mapping_status is MappingStatus.verified


def test_compose_cross_basis_refuses_with_code() -> None:
    ref = TimeRef(basis=TimeBasis.file_s, value=1.0, unit=TimeUnit.s)
    with pytest.raises(MappingUnavailableError) as info:
        compose(ref, TimeBasis.project_s)
    assert info.value.code == "mapping_unavailable"
    assert info.value.next_action == "project_time_map"
    assert info.value.to_dict()["next_action"] == "project_time_map"


def test_sample_index_uses_floor_for_starts_and_ceil_for_ends() -> None:
    ref = TimeRef(basis=TimeBasis.file_s, value=1.25 / 48000.0, unit=TimeUnit.s)
    assert to_sample_index(ref, 48000) == 1
    assert to_sample_index(ref, 48000, is_end=True) == 2

    indexed = TimeRef(basis=TimeBasis.sample_index, value=3.4, unit=TimeUnit.sample)
    assert to_sample_index(indexed, 48000) == 3
    assert to_sample_index(indexed, 48000, is_end=True) == 4

    with pytest.raises(ContractValidationError):
        to_sample_index(ref, 0)


def test_dumps_record_rejects_non_finite_values() -> None:
    envelope = RecordEnvelope(
        record_kind=RecordKind.measurement,
        record_id="mea_1",
        revision=_revision(),
    )
    object.__setattr__(envelope, "created_at", float("nan"))
    with pytest.raises(ValueError):
        dumps_record(envelope)


def test_time_ref_dict_round_trip_and_legacy_defaults() -> None:
    ref = TimeRef(
        basis=TimeBasis.musical_qn,
        value=16.0,
        unit=TimeUnit.qn,
        map_id="tm_song_1",
        mapping_status=MappingStatus.approx,
    )
    assert time_ref_to_dict(ref)["basis"] == "musical_qn"
    assert time_ref_from_dict(ref.to_dict()) == ref

    legacy = time_ref_from_dict({"basis": "project_s", "value": 1.5, "unit": "s"})
    assert legacy.mapping_status is MappingStatus.unverified
    assert legacy.uncertainty_ms is None
    assert legacy.map_id is None
    validate_time_ref(legacy)


def test_schema_and_old_public_types_import_together() -> None:
    assert len(RecordKind) == 23
    section = Section(0.0, 1.0)
    metric = MetricValue(value=1.0, unit="db")
    report = MeasurementReport(
        source="mix.wav",
        sample_rate=48000,
        channels=2,
        duration_s=1.0,
        section=section,
        metrics={"peak": metric},
    )
    buffer = AudioBuffer(samples=np.zeros((8, 1)), sample_rate=48000)
    assert report.section is section
    assert buffer.frames == 8
