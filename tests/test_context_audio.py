"""Contract tests for the frozen Wave A audio evidence types."""

from __future__ import annotations

import json
from enum import StrEnum

import pytest

from reaper_audio.context.audio import (
    ASSET_STATUSES,
    CAPTURE_CHANNEL_MODES,
    CAPTURE_STATUSES,
    SIGNAL_WINDOW_BOUNDARIES,
    STAGE_METHODS,
    ArtifactRole,
    AudioAsset,
    Capture,
    CaptureMethod,
    CaptureRequest,
    CaptureStageUnsupportedError,
    ChannelView,
    DeliveryEvidence,
    DeliveryStatus,
    Isolation,
    ListenRequest,
    ListenResult,
    ObjectKind,
    ObjectRef,
    SignalWindow,
    SourceKind,
    Stage,
    dumps_audio_record,
    loads_audio_record,
    validate_stage_method,
)
from reaper_audio.context.errors import ContractError, ContractValidationError
from reaper_audio.context.schema import RevisionRef
from reaper_audio.timebase import MappingStatus

SHA_A = "a" * 64
SHA_B = "b" * 64
ASSET_A = f"ast_sha256_{SHA_A}"
ASSET_B = f"ast_sha256_{SHA_B}"
#: Shape minted by ``context.ids.derive_id`` (26 Crockford characters).
DERIVED_ASSET = "ast_sha256_01J8ZK9Q7M4X2V6B3N5C8D1F7H"


def _revision(counter: int = 11) -> RevisionRef:
    return RevisionRef(
        project_id="reaper:master-guid:/songs/song.rpp",
        instance_id="pri_01HZZZ",
        epoch=2,
        counter=counter,
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


def _object(kind: ObjectKind = ObjectKind.track) -> ObjectRef:
    return ObjectRef(kind=kind, guid="{8F0E2C11-0000-4000-8000-000000000001}", index_hint=3)


def _asset(**overrides: object) -> AudioAsset:
    values: dict[str, object] = {
        "asset_id": ASSET_A,
        "capture_id": "cap_01HZZZ",
        "source_guid": "{8F0E2C11-0000-4000-8000-000000000001}",
        "stage": Stage.track_stem_post_fx,
        "capture_method": CaptureMethod.native_render,
        "source_kind": SourceKind.di,
        "channel_view": ChannelView.native,
        "channel_pair": None,
        "sample_rate": 48000,
        "channels": 2,
        "frames": 192000,
        "duration_s": 4.0,
        "bytes": 768000,
        "sha256": SHA_A,
        "uri": "file:///renders/kick.wav",
        "lease_id": "lease_01HZZZ",
        "lease_expires_at": "2026-11-12T11:00:00+03:00",
        "status": "available",
        "window": _window(),
        "derivation": None,
        "separator": None,
    }
    values.update(overrides)
    return AudioAsset(**values)  # type: ignore[arg-type]


def _request(**overrides: object) -> CaptureRequest:
    values: dict[str, object] = {
        "request_id": "req_01HZZZ",
        "project_id": "reaper:master-guid:/songs/song.rpp",
        "expected_revision": _revision(),
        "objects": (_object(),),
        "stages": (Stage.track_stem_post_fx,),
        "method": CaptureMethod.native_render,
        "window": _window(),
        "tail_ms": 500,
        "channels": "stereo",
        "sample_rate": 48000,
        "isolation": Isolation.mute_restore,
        "artifact_role": ArtifactRole.baseline,
        "reason": "mix review",
    }
    values.update(overrides)
    return CaptureRequest(**values)  # type: ignore[arg-type]


def _listen_result(**overrides: object) -> ListenResult:
    values: dict[str, object] = {
        "observation_id": "obs_01HZZZ",
        "request_id": "lreq_01HZZZ",
        "asset_id": ASSET_A,
        "channel_view": ChannelView.native,
        "window": _window(),
        "text": "The kick is boomy around 60 Hz and slightly left of centre.",
        "model": "music-flamingo",
        "claims": ("low end is boomy",),
        "delivery": DeliveryEvidence(
            status=DeliveryStatus.forwarded_verified,
            checked_at="2026-11-12T10:05:00+03:00",
            method="native_audio_part",
            canary="tone_1k",
        ),
        "model_revision": "rev-7",
        "weights_sha256": SHA_B,
        "prompt_sha256": SHA_A,
        "context_injected": True,
        "limits": ("single window",),
    }
    values.update(overrides)
    return ListenResult(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# enums
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("enum_cls", "expected"),
    [
        (
            Stage,
            (
                "source_file",
                "take_sum_pre_fx",
                "track_stem_post_fx",
                "bus_stem_post_fx",
                "send_return_post_fx",
                "track_via_master_post_fx",
                "master_mix_post_fx",
                "reference_file",
            ),
        ),
        (
            CaptureMethod,
            ("native_render", "audio_accessor", "source_read", "file_import"),
        ),
        (SourceKind, ("di", "mic", "amp_print", "line", "midi_render", "unknown")),
        (
            ChannelView,
            ("native", "left", "right", "mid", "side", "mono_sum", "channel_pair"),
        ),
        (
            DeliveryStatus,
            (
                "not_requested",
                "uri_only",
                "forwarded_unverified",
                "forwarded_verified",
                "unsupported",
                "failed",
            ),
        ),
        (
            ObjectKind,
            ("track", "master", "folder", "return", "item", "take", "source_file"),
        ),
        (ArtifactRole, ("temporary", "baseline", "final")),
        (Isolation, ("none", "mute_restore", "selected_via_master")),
    ],
)
def test_enum_values_round_trip(enum_cls: type[StrEnum], expected: tuple[str, ...]) -> None:
    members = tuple(enum_cls)
    assert tuple(str(member) for member in members) == expected
    assert tuple(member.value for member in members) == expected
    for value in expected:
        member = enum_cls(value)
        assert member is enum_cls(value)
        assert member == value, "StrEnum members must compare equal to their value"
        assert isinstance(member, str)


@pytest.mark.parametrize("boundary", sorted(SIGNAL_WINDOW_BOUNDARIES))
def test_every_declared_boundary_is_accepted(boundary: str) -> None:
    assert _window(boundary=boundary).boundary == boundary


def test_vocabulary_sets_are_closed() -> None:
    assert CAPTURE_CHANNEL_MODES == {"native", "mono", "stereo"}
    assert ASSET_STATUSES == {"available", "released", "expired", "missing"}
    assert CAPTURE_STATUSES == {"complete", "partial", "failed"}


# --------------------------------------------------------------------------- #
# stage / method matrix
# --------------------------------------------------------------------------- #

ALLOWED_PAIRS = [
    (stage, method)
    for stage, methods in STAGE_METHODS.items()
    for method in sorted(methods, key=str)
]
REJECTED_PAIRS = [
    (stage, method)
    for stage in Stage
    for method in CaptureMethod
    if method not in STAGE_METHODS[stage]
]


def _pair_id(pair: tuple[Stage, CaptureMethod]) -> str:
    return f"{pair[0].value}<-{pair[1].value}"


ALLOWED_IDS = [_pair_id(pair) for pair in ALLOWED_PAIRS]
REJECTED_IDS = [_pair_id(pair) for pair in REJECTED_PAIRS]


@pytest.mark.parametrize(("stage", "method"), ALLOWED_PAIRS, ids=ALLOWED_IDS)
def test_stage_method_matrix_allows_supported_pairs(
    stage: Stage, method: CaptureMethod
) -> None:
    request = _request(objects=(_object(ObjectKind.source_file),), stages=(stage,), method=method)
    assert request.stages == (stage,)
    assert request.method is method


@pytest.mark.parametrize(("stage", "method"), REJECTED_PAIRS, ids=REJECTED_IDS)
def test_stage_method_matrix_rejects_unsupported_pairs(
    stage: Stage, method: CaptureMethod
) -> None:
    with pytest.raises(CaptureStageUnsupportedError) as info:
        _request(objects=(_object(ObjectKind.source_file),), stages=(stage,), method=method)
    assert info.value.code == "capture_stage_unsupported"
    assert isinstance(info.value, ContractValidationError)
    assert isinstance(info.value, ContractError)
    assert info.value.field == "method"
    assert info.value.next_action == "choose_capture_method"

    with pytest.raises(CaptureStageUnsupportedError):
        validate_stage_method(stage, method)


def test_matrix_rejects_unknown_stage() -> None:
    with pytest.raises(CaptureStageUnsupportedError):
        validate_stage_method("bounce", CaptureMethod.native_render)  # type: ignore[arg-type]


def test_matrix_covers_every_stage() -> None:
    assert set(STAGE_METHODS) == set(Stage)
    for methods in STAGE_METHODS.values():
        assert methods
        assert all(isinstance(method, CaptureMethod) for method in methods)


# --------------------------------------------------------------------------- #
# signal windows
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "overrides",
    [
        {"project_start_s": 16.0, "project_end_s": 12.0},
        {"project_start_s": 16.0, "project_end_s": 16.0},
        {"file_start_s": 4.0, "file_end_s": 0.0},
        {"file_end_s": 0.0},
        {"source_start_s": 9.0, "source_end_s": 3.0},
        {"source_start_s": 3.0, "source_end_s": 3.0},
    ],
)
def test_window_rejects_non_increasing_bounds(overrides: dict[str, object]) -> None:
    with pytest.raises(ContractValidationError) as info:
        _window(**overrides)
    assert info.value.code == "contract_validation"


def test_window_requires_both_source_bounds() -> None:
    with pytest.raises(ContractValidationError) as info:
        _window(source_start_s=1.0)
    assert info.value.field == "source_end_s"

    with pytest.raises(ContractValidationError) as info:
        _window(source_end_s=2.0)
    assert info.value.field == "source_start_s"


def test_window_accepts_complete_source_bounds() -> None:
    window = _window(source_start_s=1.0, source_end_s=2.5)
    assert window.source_start_s == 1.0
    assert window.source_end_s == 2.5


@pytest.mark.parametrize(
    "overrides",
    [
        {"uncertainty_ms": -0.1},
        {"latency_offset_ms": -1.0},
        {"project_start_s": float("nan")},
        {"file_end_s": float("inf")},
    ],
)
def test_window_rejects_bad_numbers(overrides: dict[str, object]) -> None:
    with pytest.raises(ContractValidationError):
        _window(**overrides)


def test_window_rejects_unknown_boundary() -> None:
    with pytest.raises(ContractValidationError) as info:
        _window(boundary="loop_point")
    assert info.value.field == "boundary"


def test_window_rejects_unknown_mapping_status() -> None:
    with pytest.raises(ContractValidationError) as info:
        _window(mapping_status="probably")
    assert info.value.field == "mapping_status"


def test_window_round_trip_preserves_optional_fields() -> None:
    window = _window(
        source_start_s=1.0,
        source_end_s=2.5,
        boundary="event",
        latency_offset_ms=0.75,
        map_id="tm_01HZZZ",
        uncertainty_ms=1.25,
    )
    data = window.to_dict()
    assert data["boundary"] == "event"
    assert data["mapping_status"] == "verified"
    assert data["latency_offset_ms"] == 0.75
    assert SignalWindow.from_dict(data) == window


def test_window_from_legacy_dict_defaults() -> None:
    window = SignalWindow.from_dict(
        {
            "project_start_s": 0.0,
            "project_end_s": 1.0,
            "file_start_s": 0.0,
            "file_end_s": 1.0,
        }
    )
    assert window.boundary == "window_bounds"
    assert window.mapping_status is MappingStatus.unverified
    assert window.source_start_s is None
    assert window.uncertainty_ms is None


# --------------------------------------------------------------------------- #
# object refs and channel pairs
# --------------------------------------------------------------------------- #


def test_object_ref_validates_identity() -> None:
    ref = _object(ObjectKind.return_)
    assert ref.kind is ObjectKind.return_
    assert ref.to_dict()["kind"] == "return"
    assert ObjectRef.from_dict(ref.to_dict()) == ref

    with pytest.raises(ContractValidationError):
        ObjectRef(kind=ObjectKind.track, guid="   ")
    with pytest.raises(ContractValidationError):
        ObjectRef(kind="bus", guid="{A}")  # type: ignore[arg-type]
    with pytest.raises(ContractValidationError):
        ObjectRef(kind=ObjectKind.track, guid="{A}", index_hint=-1)


def test_channel_pair_required_for_pair_view() -> None:
    with pytest.raises(ContractValidationError) as info:
        _asset(channel_view=ChannelView.channel_pair, channel_pair=None)
    assert info.value.field == "channel_pair"


@pytest.mark.parametrize(
    "view",
    [
        ChannelView.native,
        ChannelView.left,
        ChannelView.right,
        ChannelView.mid,
        ChannelView.side,
        ChannelView.mono_sum,
    ],
)
def test_channel_pair_forbidden_for_scalar_views(view: ChannelView) -> None:
    with pytest.raises(ContractValidationError) as info:
        _asset(channel_view=view, channel_pair=(0, 1))
    assert info.value.field == "channel_pair"
    assert "channel_pair" in str(info.value)


def test_channel_pair_accepts_valid_indices() -> None:
    asset = _asset(channel_view=ChannelView.channel_pair, channel_pair=(0, 1), channels=2)
    assert asset.channel_pair == (0, 1)
    assert asset.to_dict()["channel_pair"] == [0, 1]
    assert AudioAsset.from_dict(asset.to_dict()) == asset


@pytest.mark.parametrize("pair", [(0,), (0, 1, 2), (0, 0), (-1, 0), "0,1"])
def test_channel_pair_rejects_bad_shapes(pair: object) -> None:
    with pytest.raises(ContractValidationError):
        _asset(channel_view=ChannelView.channel_pair, channel_pair=pair)


# --------------------------------------------------------------------------- #
# assets
# --------------------------------------------------------------------------- #


def test_asset_accepts_complete_record() -> None:
    asset = _asset()
    assert asset.asset_id == ASSET_A
    assert asset.status == "available"
    assert asset.window == _window()


@pytest.mark.parametrize(
    "overrides",
    [
        {"asset_id": "ast_sha256_short"},
        {"asset_id": "sha256_" + SHA_A},
        {"asset_id": f"ast_sha256_{SHA_A.upper()}"},  # normalizes, still valid
        {"sha256": "not-a-hash"},
        {"sha256": SHA_A[:-1]},
        {"asset_id": ASSET_B},  # id must embed the hash it claims
    ],
)
def test_asset_identity_is_content_addressed(overrides: dict[str, object]) -> None:
    if overrides.get("asset_id") == f"ast_sha256_{SHA_A.upper()}":
        assert _asset(**overrides).asset_id == ASSET_A
        return
    with pytest.raises(ContractValidationError):
        _asset(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"sample_rate": 0},
        {"sample_rate": -48000},
        {"channels": 0},
        {"frames": -1},
        {"duration_s": 0.0},
        {"duration_s": -1.0},
        {"bytes": 0},
        {"bytes": -8},
        {"frames": 1.0},
        {"sample_rate": True},
    ],
)
def test_asset_rejects_bad_geometry(overrides: dict[str, object]) -> None:
    with pytest.raises(ContractValidationError):
        _asset(**overrides)


def test_asset_rejects_unknown_enums_and_status() -> None:
    with pytest.raises(ContractValidationError) as info:
        _asset(stage="bounce")
    assert info.value.field == "stage"

    with pytest.raises(ContractValidationError) as info:
        _asset(status="ready")
    assert info.value.field == "status"

    with pytest.raises(ContractValidationError):
        _asset(channel_view="stereo")


def test_asset_capture_id_keeps_its_prefix() -> None:
    with pytest.raises(ContractValidationError) as info:
        _asset(capture_id="take_01HZZZ")
    assert info.value.field == "capture_id"


def test_asset_separation_requires_separator() -> None:
    with pytest.raises(ContractValidationError) as info:
        _asset(derivation="separation")
    assert info.value.field == "separator"

    separated = _asset(derivation="separation", separator="demucs-htdemucs")
    assert separated.separator == "demucs-htdemucs"

    assert _asset(derivation="trim").separator is None


def test_asset_rejects_registry_derived_id() -> None:
    with pytest.raises(ContractValidationError) as info:
        _asset(asset_id=DERIVED_ASSET)
    assert info.value.field == "asset_id"


@pytest.mark.parametrize(
    "bad",
    ["ast_sha256_" + "I" * 26, "ast_sha256_" + "z" * 26, DERIVED_ASSET],
)
def test_asset_rejects_malformed_asset_ids(bad: str) -> None:
    with pytest.raises(ContractValidationError) as info:
        _asset(asset_id=bad)
    assert info.value.field == "asset_id"


def test_listen_types_accept_content_address() -> None:
    request = ListenRequest(
        request_id="lreq_1",
        asset_id=ASSET_A,
        channel_view=ChannelView.native,
        window=None,
        prompt="Describe.",
        model="music-flamingo",
    )
    assert request.asset_id == ASSET_A
    assert _listen_result(asset_id=ASSET_A).asset_id == ASSET_A


def test_asset_from_dict_defaults_for_legacy_records() -> None:
    data = _asset().to_dict()
    for optional in ("uri", "lease_id", "lease_expires_at", "window"):
        data.pop(optional)
    data.pop("status")
    restored = AudioAsset.from_dict(data)
    assert restored.uri is None
    assert restored.lease_id is None
    assert restored.window is None
    assert restored.status == "available"


def test_asset_accepts_window_mapping() -> None:
    asset = _asset(window=_window().to_dict())
    assert isinstance(asset.window, SignalWindow)
    assert asset.window == _window()


# --------------------------------------------------------------------------- #
# capture requests
# --------------------------------------------------------------------------- #


def test_capture_request_defaults_are_conservative() -> None:
    request = CaptureRequest(
        request_id="req_1",
        project_id="reaper:g:/song.rpp",
        expected_revision=_revision(),
        objects=(_object(),),
        stages=(Stage.track_stem_post_fx,),
        method=CaptureMethod.native_render,
    )
    assert request.window is None
    assert request.tail_ms == 0
    assert request.channels == "native"
    assert request.sample_rate is None
    assert request.isolation is Isolation.none
    assert request.allow_solo_restore is False
    assert request.artifact_role is ArtifactRole.temporary
    assert request.lease_ttl_s == 3600
    assert request.dry_run is False
    assert request.reason == ""


@pytest.mark.parametrize(
    "overrides",
    [
        {"tail_ms": -1},
        {"tail_ms": 1.5},
        {"lease_ttl_s": 0},
        {"lease_ttl_s": -60},
        {"channels": "quad"},
        {"sample_rate": 0},
        {"sample_rate": -44100},
        {"objects": ()},
        {"stages": ()},
        {"request_id": "  "},
        {"project_id": ""},
        {"isolation": "solo"},
        {"artifact_role": "permanent"},
        {"dry_run": "yes"},
    ],
)
def test_capture_request_rejects_bad_fields(overrides: dict[str, object]) -> None:
    with pytest.raises(ContractValidationError):
        _request(**overrides)


def test_capture_request_requires_revision_identity() -> None:
    with pytest.raises(ContractValidationError):
        _request(expected_revision={"project_id": "reaper:g:/song.rpp"})


def test_capture_request_round_trip() -> None:
    request = _request(
        objects=(_object(), _object(ObjectKind.master)),
        stages=(Stage.track_stem_post_fx, Stage.master_mix_post_fx),
    )
    data = request.to_dict()
    assert data["stages"] == ["track_stem_post_fx", "master_mix_post_fx"]
    assert data["objects"][1]["kind"] == "master"
    assert CaptureRequest.from_dict(data) == request
    assert CaptureRequest.from_dict(
        {
            "request_id": "req_1",
            "project_id": "reaper:g:/song.rpp",
            "expected_revision": _revision().to_dict(),
            "objects": [{"kind": "track", "guid": "{A}"}],
            "stages": ["source_file"],
            "method": "source_read",
        }
    ).tail_ms == 0


# --------------------------------------------------------------------------- #
# capture results
# --------------------------------------------------------------------------- #


def _capture(**overrides: object) -> Capture:
    values: dict[str, object] = {
        "capture_id": "cap_01HZZZ",
        "request_id": "req_01HZZZ",
        "method": CaptureMethod.native_render,
        "stages": (Stage.track_stem_post_fx,),
        "assets": (_asset(),),
        "revision_before": _revision(11),
        "revision_after": _revision(11),
        "render_settings": {"tail_ms": 500, "channels": 2},
        "restore_report": {"mutes_restored": True, "solo_cleared": True},
        "solo_active": False,
        "tail_s": 0.5,
        "coverage": {"requested": 1, "delivered": 1},
        "missing": (),
        "warnings": (),
        "status": "complete",
    }
    values.update(overrides)
    return Capture(**values)  # type: ignore[arg-type]


def test_capture_round_trip_with_assets() -> None:
    capture = _capture()
    data = capture.to_dict()
    assert data["assets"][0]["asset_id"] == ASSET_A
    assert data["revision_after"]["counter"] == 11
    restored = Capture.from_dict(data)
    assert restored == capture
    assert restored.assets[0].window == _window()


def test_capture_complete_cannot_hide_missing_assets() -> None:
    with pytest.raises(ContractValidationError) as info:
        _capture(status="complete", missing=("take-3",))
    assert info.value.field == "missing"

    partial = _capture(status="partial", missing=("take-3",), warnings=("mute restore skipped",))
    assert partial.status == "partial"
    assert partial.missing == ("take-3",)


def test_capture_rejects_unknown_status_and_negative_tail() -> None:
    with pytest.raises(ContractValidationError):
        _capture(status="done")
    with pytest.raises(ContractValidationError):
        _capture(tail_s=-0.1)


def test_capture_accepts_failed_run_without_assets() -> None:
    capture = _capture(assets=(), status="failed", revision_after=None, missing=("all",))
    assert capture.assets == ()
    assert capture.revision_after is None


# --------------------------------------------------------------------------- #
# delivery and listening
# --------------------------------------------------------------------------- #


def test_delivery_heard_only_when_verified() -> None:
    assert DeliveryEvidence().status is DeliveryStatus.not_requested
    assert DeliveryEvidence().heard is False

    for status in DeliveryStatus:
        evidence = DeliveryEvidence(status=status)
        assert evidence.heard is (status is DeliveryStatus.forwarded_verified)

    verified = DeliveryEvidence(status="forwarded_verified")  # type: ignore[arg-type]
    assert verified.heard is True
    assert verified.to_dict()["status"] == "forwarded_verified"
    assert DeliveryEvidence.from_dict(verified.to_dict()) == verified


def test_delivery_rejects_unknown_status() -> None:
    with pytest.raises(ContractValidationError) as info:
        DeliveryEvidence(status="delivered")  # type: ignore[arg-type]
    assert info.value.field == "status"


def test_listen_request_round_trip() -> None:
    request = ListenRequest(
        request_id="lreq_01HZZZ",
        asset_id=ASSET_A,
        channel_view=ChannelView.mid,
        window=_window(),
        prompt="Describe the low end.",
        model="music-flamingo",
        model_revision="rev-7",
        prompt_sha256=SHA_A,
    )
    assert ListenRequest.from_dict(request.to_dict()) == request


def test_listen_request_rejects_bad_fields() -> None:
    base = {
        "request_id": "lreq_1",
        "asset_id": ASSET_A,
        "channel_view": ChannelView.native,
        "window": None,
        "prompt": "Describe.",
        "model": "music-flamingo",
    }
    with pytest.raises(ContractValidationError):
        ListenRequest(**{**base, "prompt": "  "})  # type: ignore[arg-type]
    with pytest.raises(ContractValidationError):
        ListenRequest(**{**base, "asset_id": "ast_sha256_x"})  # type: ignore[arg-type]
    with pytest.raises(ContractValidationError):
        ListenRequest(**{**base, "prompt_sha256": "xyz"})  # type: ignore[arg-type]


def test_listen_result_carries_delivery_evidence() -> None:
    result = _listen_result()
    assert result.delivery.heard is True
    assert result.to_dict()["delivery"]["canary"] == "tone_1k"
    assert ListenResult.from_dict(result.to_dict()) == result


def test_listen_result_cannot_claim_unverified_delivery() -> None:
    with pytest.raises(ContractValidationError) as info:
        _listen_result(
            delivery=DeliveryEvidence(status=DeliveryStatus.forwarded_verified)
        )
    assert info.value.field == "delivery"
    assert info.value.next_action == "forward_and_confirm"

    with pytest.raises(ContractValidationError):
        _listen_result(
            delivery=DeliveryEvidence(
                status=DeliveryStatus.forwarded_verified, method="native_audio_part"
            )
        )

    accepted = _listen_result(
        delivery=DeliveryEvidence(
            status=DeliveryStatus.forwarded_verified,
            checked_at="2026-11-12T10:05:00+03:00",
            method="native_audio_part",
        )
    )
    assert accepted.delivery.heard is True


def test_listen_result_accepts_mapping_delivery() -> None:
    result = _listen_result(delivery={"status": "uri_only"})
    assert isinstance(result.delivery, DeliveryEvidence)
    assert result.delivery.heard is False


@pytest.mark.parametrize("status", [DeliveryStatus.unsupported, DeliveryStatus.failed])
def test_listen_result_allows_empty_text_only_on_unsupported_or_failed(
    status: DeliveryStatus,
) -> None:
    result = _listen_result(
        text="", delivery=DeliveryEvidence(status=status, detail="no audio path")
    )
    assert result.text == ""

    for other in (
        DeliveryStatus.uri_only,
        DeliveryStatus.forwarded_unverified,
        DeliveryStatus.not_requested,
    ):
        with pytest.raises(ContractValidationError) as info:
            _listen_result(text="", delivery=DeliveryEvidence(status=other))
        assert info.value.field == "text"


def test_listen_result_rejects_bad_identity_fields() -> None:
    with pytest.raises(ContractValidationError) as info:
        _listen_result(observation_id="cap_01HZZZ")
    assert info.value.field == "observation_id"

    with pytest.raises(ContractValidationError):
        _listen_result(asset_id="ast_sha256_zz")
    with pytest.raises(ContractValidationError):
        _listen_result(weights_sha256="abc")
    with pytest.raises(ContractValidationError):
        _listen_result(model=" ")


def test_listen_result_defaults() -> None:
    result = ListenResult(
        observation_id="obs_2",
        request_id="lreq_2",
        asset_id=ASSET_B,
        channel_view=ChannelView.side,
        window=None,
        text="Wide but phasey.",
        model="ossicle-4b",
    )
    assert result.claims == ()
    assert result.delivery == DeliveryEvidence()
    assert result.delivery.heard is False
    assert result.context_injected is False
    assert result.limits == ()
    assert result.reliability == "uncalibrated"


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #


def test_json_round_trip_capture() -> None:
    capture = _capture(
        assets=(
            _asset(),
            _asset(
                asset_id=ASSET_B,
                sha256=SHA_B,
                stage=Stage.master_mix_post_fx,
                channel_view=ChannelView.channel_pair,
                channel_pair=(0, 1),
                derivation="separation",
                separator="demucs-htdemucs",
                window=_window(boundary="fixed", source_start_s=0.0, source_end_s=4.0),
            ),
        ),
        stages=(Stage.track_stem_post_fx, Stage.master_mix_post_fx),
        status="partial",
        missing=("send_return:plate",),
    )
    text = dumps_audio_record(capture)
    assert "NaN" not in text
    assert text == dumps_audio_record(capture), "serialization must be deterministic"
    payload = json.loads(text)
    assert payload["status"] == "partial"
    assert payload["assets"][1]["channel_pair"] == [0, 1]
    assert loads_audio_record(Capture, text) == capture


def test_json_round_trip_listen_result() -> None:
    result = _listen_result()
    text = dumps_audio_record(result)
    payload = json.loads(text)
    assert payload["delivery"]["status"] == "forwarded_verified"
    assert payload["window"]["mapping_status"] == "verified"
    assert loads_audio_record(ListenResult, text) == result


def test_json_helpers_reject_bad_input() -> None:
    with pytest.raises(ContractValidationError):
        dumps_audio_record(object())
    with pytest.raises(ContractValidationError):
        loads_audio_record(Capture, "not json")
    with pytest.raises(ContractValidationError):
        loads_audio_record(Capture, "[]")
    with pytest.raises(ContractValidationError):
        loads_audio_record(Capture, 42)  # type: ignore[arg-type]
