"""Contract tests for Wave A identifier registry and event model."""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from pathlib import Path

import pytest

from reaper_audio.context.errors import (
    ContractError,
    ContractValidationError,
    UnknownPrefixError,
)
from reaper_audio.context.ids import (
    AUTHORED_PREFIXES,
    DEPRECATED_PREFIXES,
    DERIVED_PREFIXES,
    GESTURE_PREFIX,
    ID_BODY_LENGTH,
    PREFIXES,
    canonical_key,
    derive_id,
    is_authored_prefix,
    is_derived_prefix,
    is_valid_id,
    new_id,
    parse_id,
    validate_id,
)
from reaper_audio.timebase import Interval, TimeBasis, TimeRef, TimeUnit

_CROCKFORD = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")

#: Imported by ``reaper_audio.analysis.__init__`` but irrelevant to this module.
_AUDIO_STACK = {"soundfile", "scipy", "phonometry", "pyloudnorm", "librosa"}


def _load_event_model():
    """Import ``event_model`` without requiring the audio stack.

    The Wave A contract venv ships numpy + pytest only, while
    ``reaper_audio.analysis.__init__`` eagerly imports soundfile.  The event
    model itself is pure Python, so when (and only when) an optional audio
    dependency is what is missing, load the module by path under its real name
    so its relative imports still resolve.
    """
    try:
        from reaper_audio.analysis import event_model as module

        return module
    except ModuleNotFoundError as exc:
        if exc.name not in _AUDIO_STACK:
            raise
        path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "reaper_audio"
            / "analysis"
            / "event_model.py"
        )
        spec = importlib.util.spec_from_file_location(
            "reaper_audio.analysis.event_model", path
        )
        assert spec is not None and spec.loader is not None
        loaded = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = loaded
        spec.loader.exec_module(loaded)
        return loaded


em = _load_event_model()
Confidence = em.Confidence
EventDuration = em.EventDuration
EventStatus = em.EventStatus
EvidenceRef = em.EvidenceRef
MethodRef = em.MethodRef
PerceivedGesture = em.PerceivedGesture
PerformanceEvent = em.PerformanceEvent
PerformanceOccurrence = em.PerformanceOccurrence
Pitch = em.Pitch
ScoreObject = em.ScoreObject
SignalEvent = em.SignalEvent
SignalKind = em.SignalKind
UsageScope = em.UsageScope


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _interval(
    start: float = 0.0, end: float | None = 1.0, anchor: str = "attack_start"
) -> Interval:
    return Interval(
        start=TimeRef(basis=TimeBasis.project_s, value=start, unit=TimeUnit.s),
        end=None
        if end is None
        else TimeRef(basis=TimeBasis.project_s, value=end, unit=TimeUnit.s),
        anchor=anchor,
    )


def _method() -> MethodRef:
    return MethodRef(name="onset_detector", version="1.2.0", params_hash="deadbeef")


def _signal_event(**overrides: object) -> SignalEvent:
    values: dict[str, object] = {
        "event_id": new_id("evt"),
        "asset_id": "ast_sha256_" + "a" * 64,
        "interval": _interval(),
        "kind": SignalKind.onset,
        "strength": 0.82,
        "method": _method(),
    }
    values.update(overrides)
    return SignalEvent(**values)  # type: ignore[arg-type]


def _occurrence(**overrides: object) -> PerformanceOccurrence:
    values: dict[str, object] = {
        "occurrence_id": new_id("occ"),
        "project_instance_id": new_id("pri"),
        "item_guid": "{ITEM-GUID}",
        "take_guid": "{TAKE-GUID}",
    }
    values.update(overrides)
    return PerformanceOccurrence(**values)  # type: ignore[arg-type]


def _performance_event(**overrides: object) -> PerformanceEvent:
    values: dict[str, object] = {
        "event_id": new_id("prf"),
        "occurrence_id": new_id("occ"),
        "kind": "note",
        "interval": _interval(),
        "method": _method(),
        "usage_scope": UsageScope(timing=True, pitch=True, duration=True),
        "pitch": Pitch(midi=64, cents_offset=-3.5),
        "duration": EventDuration(start_s=0.0, end_s=0.4),
        "confidence": Confidence(basis="agreement", value=0.9, n=3),
        "alternatives": ("take_b",),
        "evidence_ids": (new_id("obs"),),
    }
    values.update(overrides)
    return PerformanceEvent(**values)  # type: ignore[arg-type]


def _score(**overrides: object) -> ScoreObject:
    values: dict[str, object] = {
        "score_id": new_id("sco"),
        "take_guid": "{TAKE-GUID}",
        "ppq": 960,
        "pitch": 60,
        "channel": 3,
        "duration_ppq": 480,
        "method": _method(),
    }
    values.update(overrides)
    return ScoreObject(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# ids: derive_id
# --------------------------------------------------------------------------


def _natural_key(**changes: object) -> dict[str, object]:
    key: dict[str, object] = {"asset_id": "ast_sha256_x", "onset_s": 1.25, "channel": 0}
    key.update(changes)
    return key


def test_derive_id_is_repeatable_for_identical_inputs() -> None:
    first = derive_id("evt", _natural_key(), "1.0.0", "fp_rev_7")
    second = derive_id("evt", dict(reversed(list(_natural_key().items()))), "1.0.0", "fp_rev_7")
    assert first == second
    assert first.startswith("evt_")
    assert len(first.split("_", 1)[1]) == ID_BODY_LENGTH


def test_derive_id_changes_when_any_input_changes() -> None:
    baseline = derive_id("evt", _natural_key(), "1.0.0", "fp_rev_7")
    assert derive_id("evt", _natural_key(onset_s=1.26), "1.0.0", "fp_rev_7") != baseline
    assert derive_id("evt", _natural_key(), "1.0.1", "fp_rev_7") != baseline
    assert derive_id("evt", _natural_key(), "1.0.0", "fp_rev_8") != baseline
    assert derive_id("prf", _natural_key(), "1.0.0", "fp_rev_7") != baseline


def test_derive_id_rejects_empty_version_and_non_mapping_key() -> None:
    with pytest.raises(ContractValidationError) as info:
        derive_id("evt", _natural_key(), "", "fp_rev_7")
    assert info.value.field == "method_version"

    with pytest.raises(ContractValidationError) as info:
        derive_id("evt", _natural_key(), "1.0.0", "")
    assert info.value.field == "revision_fingerprint"

    with pytest.raises(ContractValidationError) as info:
        derive_id("evt", ["not", "a", "mapping"], "1.0.0", "fp")  # type: ignore[arg-type]
    assert info.value.field == "natural_key"


def test_canonical_key_is_order_and_tuple_insensitive_and_strict() -> None:
    assert canonical_key({"b": 1, "a": [1, 2]}) == canonical_key({"a": [1, 2], "b": 1})
    assert canonical_key({"a": (1, 2)}) == canonical_key({"a": [1, 2]})
    assert canonical_key({"a": "привет"}) == '{"a":"привет"}'
    assert "NaN" not in canonical_key({"a": 1.0})

    with pytest.raises(ContractValidationError):
        canonical_key({"a": float("nan")})
    with pytest.raises(ContractValidationError):
        canonical_key({1: "int key"})  # type: ignore[dict-item]


# --------------------------------------------------------------------------
# ids: new_id / parse_id / validate_id
# --------------------------------------------------------------------------


def test_new_id_is_unique_across_one_thousand_calls() -> None:
    ids = [new_id("evt") for _ in range(1000)]
    assert len(set(ids)) == 1000
    assert ids == sorted(ids), "ULID bodies must stay monotonic"
    for value in ids:
        prefix, body = parse_id(value)
        assert prefix == "evt"
        assert len(body) == ID_BODY_LENGTH
        assert set(body) <= _CROCKFORD


def test_new_id_normalizes_a_trailing_underscore() -> None:
    assert new_id("evt_").startswith("evt_")
    assert parse_id(new_id("trk_master"))[0] == "trk_master"
    with pytest.raises(ContractValidationError) as info:
        new_id("")
    assert info.value.field == "prefix"


def test_parse_id_round_trips_every_registered_prefix() -> None:
    for prefix in sorted(PREFIXES - {"ast_sha256"}):
        value = new_id(prefix)
        parsed_prefix, body = parse_id(value)
        assert parsed_prefix == prefix
        assert f"{parsed_prefix}_{body}" == value
        assert validate_id(value, prefix) == value
        assert is_valid_id(value)

    asset = "ast_sha256_" + "a" * 64
    assert parse_id(asset) == ("ast_sha256", "a" * 64)
    assert validate_id(asset, "ast_sha256") == asset
    assert is_valid_id(asset)


def test_parse_id_prefers_the_longest_prefix() -> None:
    value = new_id("trk_master")
    with pytest.raises(ContractValidationError) as info:
        validate_id(value, "trk")
    assert info.value.code == "invalid_id"


def test_parse_id_rejects_unknown_prefix() -> None:
    with pytest.raises(UnknownPrefixError) as info:
        parse_id("zzz_" + "0" * ID_BODY_LENGTH)
    assert info.value.code == "unknown_id_prefix"
    assert isinstance(info.value, ContractError)

    with pytest.raises(UnknownPrefixError):
        new_id("zzz")
    with pytest.raises(UnknownPrefixError):
        parse_id("evt" + "0" * ID_BODY_LENGTH)


def test_gesture_prefix_is_peg_and_ges_is_rejected() -> None:
    assert GESTURE_PREFIX == "peg"
    assert "ges" not in PREFIXES
    assert "ges" in DEPRECATED_PREFIXES

    body = "0" * ID_BODY_LENGTH
    with pytest.raises(UnknownPrefixError) as info:
        parse_id(f"ges_{body}")
    assert info.value.code == "unknown_id_prefix"
    assert info.value.next_action == "use peg_"

    with pytest.raises(UnknownPrefixError) as info:
        new_id("ges")
    assert info.value.next_action == "use peg_"

    gesture = new_id("peg")
    assert parse_id(gesture)[0] == "peg"


def test_parse_id_rejects_malformed_bodies() -> None:
    cases = [
        "evt_" + "0" * (ID_BODY_LENGTH - 1),
        "evt_" + "0" * (ID_BODY_LENGTH + 1),
        "evt_" + "0" * 25 + "I",
        "evt_" + "0" * 25 + "U",
        "evt_" + "0" * 25 + "-",
        "evt_",
        "",
    ]
    for value in cases:
        with pytest.raises(ContractValidationError) as info:
            parse_id(value)
        assert info.value.code == "invalid_id", value


def test_validate_id_rejects_prefix_mismatch() -> None:
    event_id = new_id("evt")
    assert validate_id(event_id, "evt_") == event_id
    with pytest.raises(ContractValidationError) as info:
        validate_id(event_id, "prf")
    assert info.value.code == "invalid_id"
    assert info.value.next_action == "use prf_"


def test_derived_and_authored_prefixes_are_disjoint_registered_sets() -> None:
    assert DERIVED_PREFIXES.isdisjoint(AUTHORED_PREFIXES)
    assert DERIVED_PREFIXES | AUTHORED_PREFIXES <= PREFIXES
    for prefix in DERIVED_PREFIXES:
        assert is_derived_prefix(prefix)
        assert not is_authored_prefix(prefix)
    for prefix in AUTHORED_PREFIXES:
        assert is_authored_prefix(prefix)
        assert not is_derived_prefix(prefix)
    assert not is_derived_prefix("trk")
    assert not is_authored_prefix("trk")


# --------------------------------------------------------------------------
# event_model: identity invariants
# --------------------------------------------------------------------------


def test_event_ids_must_use_their_own_prefix() -> None:
    with pytest.raises(ContractValidationError) as info:
        _signal_event(event_id=new_id("prf"))
    assert info.value.code == "event_invariant"
    assert info.value.field == "event_id"

    with pytest.raises(ContractValidationError):
        _performance_event(event_id=new_id("evt"))

    with pytest.raises(ContractValidationError):
        _performance_event(occurrence_id=new_id("evt"))

    with pytest.raises(ContractValidationError):
        _occurrence(occurrence_id=new_id("evt"))

    with pytest.raises(ContractValidationError):
        _score(score_id=new_id("evt"))

    with pytest.raises(ContractValidationError):
        PerceivedGesture(
            gesture_id=new_id("evt"),
            member_event_ids=(new_id("evt"),),
            kind="strum",
            interval=_interval(),
        )

    with pytest.raises(ContractValidationError) as info:
        _signal_event(asset_id=new_id("evt"))
    assert info.value.field == "asset_id"


def test_performance_event_requires_an_occurrence_id() -> None:
    with pytest.raises(ContractValidationError) as info:
        _performance_event(occurrence_id="take_1")
    assert info.value.code == "event_invariant"
    assert info.value.field == "occurrence_id"

    event = _performance_event()
    assert parse_id(event.occurrence_id)[0] == "occ"


def test_note_requires_pitch_and_duration() -> None:
    with pytest.raises(ContractValidationError) as info:
        _performance_event(pitch=None)
    assert info.value.field == "pitch"
    assert info.value.code == "event_invariant"

    with pytest.raises(ContractValidationError) as info:
        _performance_event(duration=None)
    assert info.value.field == "duration"

    pitched = _performance_event(duration=None, kind="pick_attack")
    assert pitched.pitch is not None
    assert pitched.duration is None


def test_chord_requires_at_least_two_notes() -> None:
    with pytest.raises(ContractValidationError) as info:
        _performance_event(
            kind="chord", pitch=None, duration=None, chord=(60,)
        )
    assert info.value.field == "chord"
    assert info.value.code == "event_invariant"

    chord = _performance_event(kind="chord", pitch=None, duration=None, chord=(60, 64, 67))
    assert chord.chord == (60, 64, 67)

    with pytest.raises(ContractValidationError):
        _performance_event(kind="chord", pitch=None, duration=None, chord=None)

    with pytest.raises(ContractValidationError) as info:
        _performance_event(kind="chord", chord=(60, 64))
    assert info.value.field == "chord"


def test_gesture_requires_members() -> None:
    with pytest.raises(ContractValidationError) as info:
        PerceivedGesture(
            gesture_id=new_id("peg"),
            member_event_ids=(),
            kind="strum",
            interval=_interval(),
        )
    assert info.value.field == "member_event_ids"
    assert info.value.code == "event_invariant"

    member = new_id("evt")
    with pytest.raises(ContractValidationError):
        PerceivedGesture(
            gesture_id=new_id("peg"),
            member_event_ids=(member, member),
            kind="strum",
            interval=_interval(),
        )

    with pytest.raises(ContractValidationError):
        PerceivedGesture(
            gesture_id=new_id("peg"),
            member_event_ids=(new_id("prj"),),
            kind="strum",
            interval=_interval(),
        )

    gesture = PerceivedGesture(
        gesture_id=new_id("peg"),
        member_event_ids=(member, new_id("prf")),
        kind="strum",
        interval=_interval(),
    )
    assert gesture.member_event_ids[0] == member


def test_score_object_rejects_negative_ppq_and_unknown_origin() -> None:
    with pytest.raises(ContractValidationError) as info:
        _score(ppq=-1)
    assert info.value.field == "ppq"
    assert info.value.code == "event_invariant"

    with pytest.raises(ContractValidationError) as info:
        _score(duration_ppq=-480)
    assert info.value.field == "duration_ppq"

    with pytest.raises(ContractValidationError) as info:
        _score(origin="guessed")
    assert info.value.field == "origin"

    for origin in ("project_midi", "transcription", "composed"):
        assert _score(origin=origin).origin == origin


def test_confidence_and_usage_scope_defaults() -> None:
    scope = UsageScope()
    assert scope.timing is True
    assert scope.pitch is False
    assert scope.destructive_cut is False

    with pytest.raises(ContractValidationError):
        Confidence(basis="vibes")
    with pytest.raises(ContractValidationError):
        Confidence(basis="interval", value=1.5)

    assert Confidence(basis="none").value is None
    assert Confidence(basis="rank", value=1, n=0).value == 1.0


# --------------------------------------------------------------------------
# event_model: JSON round-trip and shared types
# --------------------------------------------------------------------------


def test_signal_event_json_round_trip() -> None:
    event = _signal_event(
        status=EventStatus.reviewed,
        evidence=(
            EvidenceRef(record_id=new_id("mea"), record_kind="measurement"),
            EvidenceRef(record_id=new_id("cap"), record_kind="capture"),
        ),
    )
    text = event.to_json()
    assert "NaN" not in text
    payload = json.loads(text)
    assert payload["kind"] == "onset"
    assert payload["status"] == "reviewed"
    assert payload["interval"]["anchor"] == "attack_start"
    assert SignalEvent.from_dict(payload) == event
    assert SignalEvent.from_dict(payload).to_dict() == event.to_dict()


def test_performance_event_json_round_trip() -> None:
    event = _performance_event()
    text = event.to_json()
    payload = json.loads(text)
    assert payload["usage_scope"]["duration"] is True
    assert payload["pitch"]["pitch_class"] == 4
    assert payload["duration"]["end_s"] == 0.4
    restored = PerformanceEvent.from_dict(payload)
    assert restored == event
    assert restored.to_dict() == event.to_dict()


def test_occurrence_gesture_and_score_round_trip() -> None:
    occurrence = _occurrence(
        loop_iteration=2,
        source_interval=_interval(0.0, 4.0),
        limitations=("partial_capture",),
    )
    restored_occurrence = PerformanceOccurrence.from_dict(
        json.loads(occurrence.to_json())
    )
    assert restored_occurrence == occurrence

    gesture = PerceivedGesture(
        gesture_id=new_id("peg"),
        member_event_ids=(new_id("evt"),),
        kind="strum",
        interval=_interval(),
        confidence=Confidence(basis="interval", value=0.5),
    )
    assert PerceivedGesture.from_dict(json.loads(gesture.to_json())) == gesture

    score = _score(origin="transcription")
    restored_score = ScoreObject.from_dict(json.loads(score.to_json()))
    assert restored_score == score
    assert restored_score.origin == "transcription"


def test_json_serialization_refuses_non_finite_values() -> None:
    event = _signal_event()
    object.__setattr__(event, "strength", float("inf"))
    with pytest.raises(ValueError):
        event.to_json()


def test_time_and_stage_types_are_not_redefined() -> None:
    source = inspect.getsource(em)
    assert "class TimeRef" not in source
    assert "class Interval" not in source
    assert "class Stage" not in source
    assert em.TimeRef is TimeRef
    assert em.Interval is Interval
    assert isinstance(_signal_event().interval, Interval)


def test_signal_kind_and_event_status_values() -> None:
    assert {str(kind) for kind in SignalKind} == {
        "onset",
        "transient",
        "spike",
        "clip_run",
        "silence",
        "decay",
    }
    assert {str(status) for status in EventStatus} == {
        "candidate",
        "transcribed",
        "reviewed",
        "rejected",
    }


def test_asset_ids_are_content_hashes_only() -> None:
    with pytest.raises(ContractValidationError):
        new_id("ast_sha256")
    with pytest.raises(ContractValidationError):
        derive_id("ast_sha256", {"sha256": "a" * 64}, "1", "fp")
    prefix, body = parse_id("ast_sha256_" + "A" * 64)
    assert prefix == "ast_sha256"
    assert body == "a" * 64
    with pytest.raises(ContractValidationError):
        parse_id("ast_sha256_" + "A" * ID_BODY_LENGTH)
