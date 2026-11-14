"""Audio evidence contracts: what was captured, where and how well.

An ``AudioAsset`` is never an anonymous WAV.  It names the stage it was taken
from, the method that produced it, the channel view, the hash of its bytes and
the signal window it covers.  ``DeliveryEvidence`` records whether a listener
actually *heard* the audio or only received a URI: the two are not the same
claim and this module never lets them collapse into one.

Wave A keeps this file to types, validation and serialization.  Capture,
rendering and lease bookkeeping live elsewhere and must not grow here.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, TypeVar

from ..timebase import MappingStatus
from .errors import ContractValidationError
from .schema import RevisionRef, validate_revision


class Stage(StrEnum):
    """Point in the signal path an asset was taken from."""

    source_file = "source_file"
    take_sum_pre_fx = "take_sum_pre_fx"
    track_stem_post_fx = "track_stem_post_fx"
    bus_stem_post_fx = "bus_stem_post_fx"
    send_return_post_fx = "send_return_post_fx"
    track_via_master_post_fx = "track_via_master_post_fx"
    master_mix_post_fx = "master_mix_post_fx"
    reference_file = "reference_file"


class CaptureMethod(StrEnum):
    """Mechanism used to obtain the audio."""

    native_render = "native_render"
    audio_accessor = "audio_accessor"
    source_read = "source_read"
    file_import = "file_import"


class SourceKind(StrEnum):
    """How the underlying signal was recorded or generated."""

    di = "di"
    mic = "mic"
    amp_print = "amp_print"
    line = "line"
    midi_render = "midi_render"
    unknown = "unknown"


class ChannelView(StrEnum):
    """Which channels of a source the asset exposes."""

    native = "native"
    left = "left"
    right = "right"
    mid = "mid"
    side = "side"
    mono_sum = "mono_sum"
    channel_pair = "channel_pair"


class DeliveryStatus(StrEnum):
    """How far a listener's copy of an asset actually travelled."""

    not_requested = "not_requested"
    uri_only = "uri_only"
    forwarded_unverified = "forwarded_unverified"
    forwarded_verified = "forwarded_verified"
    unsupported = "unsupported"
    failed = "failed"


class ObjectKind(StrEnum):
    """Kind of REAPER object an asset or capture refers to."""

    track = "track"
    master = "master"
    folder = "folder"
    return_ = "return"
    item = "item"
    take = "take"
    source_file = "source_file"


class ArtifactRole(StrEnum):
    """Intended lifetime of a captured artifact."""

    temporary = "temporary"
    baseline = "baseline"
    final = "final"


class Isolation(StrEnum):
    """How other signal was kept out of the capture."""

    none = "none"
    mute_restore = "mute_restore"
    selected_via_master = "selected_via_master"


CAPTURE_CHANNEL_MODES = frozenset({"native", "mono", "stereo"})

SIGNAL_WINDOW_BOUNDARIES = frozenset(
    {"bar", "event", "user", "adaptive", "fixed", "window_bounds"}
)

ASSET_STATUSES = frozenset({"available", "released", "expired", "missing"})

CAPTURE_STATUSES = frozenset({"complete", "partial", "failed"})

STAGE_METHODS: Mapping[Stage, frozenset[CaptureMethod]] = MappingProxyType(
    {
        Stage.source_file: frozenset(
            {CaptureMethod.source_read, CaptureMethod.file_import}
        ),
        Stage.take_sum_pre_fx: frozenset({CaptureMethod.audio_accessor}),
        Stage.track_stem_post_fx: frozenset({CaptureMethod.native_render}),
        Stage.bus_stem_post_fx: frozenset({CaptureMethod.native_render}),
        Stage.send_return_post_fx: frozenset({CaptureMethod.native_render}),
        Stage.track_via_master_post_fx: frozenset({CaptureMethod.native_render}),
        Stage.master_mix_post_fx: frozenset({CaptureMethod.native_render}),
        Stage.reference_file: frozenset({CaptureMethod.file_import}),
    }
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
#: Body shape produced by ``context.ids.derive_id("ast_sha256", ...)``.
_DERIVED_BODY = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_OBSERVATION_ID = re.compile(r"^obs_[A-Za-z0-9._-]+$")
_CAPTURE_ID = re.compile(r"^cap_[A-Za-z0-9._-]+$")

class _FromDict(Protocol):
    """Anything this module can rebuild from a plain mapping."""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Any: ...


RecordT = TypeVar("RecordT", bound=_FromDict)


class CaptureStageUnsupportedError(ContractValidationError):
    """The requested stage cannot be produced by the requested method."""

    code = "capture_stage_unsupported"


# --------------------------------------------------------------------------- #
# scalar helpers
# --------------------------------------------------------------------------- #


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(
            f"{field} must be a non-empty string", field=field
        )
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _plain_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractValidationError(f"{field} must be an integer", field=field)
    return value


def _non_negative_int(value: Any, field: str) -> int:
    number = _plain_int(value, field)
    if number < 0:
        raise ContractValidationError(f"{field} must be >= 0", field=field)
    return number


def _positive_int(value: Any, field: str) -> int:
    number = _plain_int(value, field)
    if number <= 0:
        raise ContractValidationError(f"{field} must be > 0", field=field)
    return number


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractValidationError(f"{field} must be a number", field=field)
    number = float(value)
    if not math.isfinite(number):
        raise ContractValidationError(f"{field} must be finite", field=field)
    return number


def _non_negative_float(value: Any, field: str) -> float:
    number = _finite(value, field)
    if number < 0:
        raise ContractValidationError(f"{field} must be >= 0", field=field)
    return number


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ContractValidationError(f"{field} must be a bool", field=field)
    return value


def _enum(value: Any, enum_cls: Any, field: str) -> Any:
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except (ValueError, TypeError) as exc:
        raise ContractValidationError(
            f"unknown {field}: {value!r}", field=field
        ) from exc


def _hex64(value: Any, field: str) -> str:
    text = _text(value, field).strip().lower()
    if not _HEX64.match(text):
        raise ContractValidationError(
            f"{field} must be 64 lowercase hex characters", field=field
        )
    return text


def _optional_hex64(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _hex64(value, field)


def _asset_id(value: Any, field: str = "asset_id") -> tuple[str, str | None]:
    """Validate an asset id and return ``(canonical_id, embedded_sha256)``.

    An asset id is a content address: the full 64-hex digest of the asset bytes.
    Registry-derived 26-character bodies are not asset identities and are
    rejected; ``context.ids`` also refuses to mint or derive this prefix.
    """
    text = _text(value, field).strip()
    if not text.startswith("ast_sha256_"):
        raise ContractValidationError(
            "asset id must start with ast_sha256_", field=field
        )
    body = text[len("ast_sha256_") :]
    lowered = body.lower()
    if _HEX64.match(lowered):
        return f"ast_sha256_{lowered}", lowered
    raise ContractValidationError(
        "asset_id must be ast_sha256_<64 hex>",
        field=field,
    )


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{field} must be a mapping", field=field)
    return dict(value)


def _text_tuple(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    try:
        items = tuple(value)
    except TypeError as exc:
        raise ContractValidationError(
            f"{field} must be a sequence of strings", field=field
        ) from exc
    return tuple(_text(item, f"{field}[{index}]") for index, item in enumerate(items))


def validate_stage_method(stage: Stage, method: CaptureMethod) -> None:
    """Raise ``CaptureStageUnsupportedError`` when a stage cannot come from a method."""
    allowed = STAGE_METHODS.get(stage)
    if allowed is None:
        raise CaptureStageUnsupportedError(
            f"unknown capture stage: {stage!r}", field="stages"
        )
    if method not in allowed:
        names = ", ".join(sorted(str(item) for item in allowed))
        raise CaptureStageUnsupportedError(
            f"stage {stage} cannot be captured with {method}; use {names}",
            field="method",
            next_action="choose_capture_method",
        )


# --------------------------------------------------------------------------- #
# references and windows
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ObjectRef:
    """Stable identity of one project object, independent of its index."""

    kind: ObjectKind
    guid: str
    parent_guid: str | None = None
    index_hint: int | None = None

    def __post_init__(self) -> None:
        kind = _enum(self.kind, ObjectKind, "kind")
        guid = _text(self.guid, "guid")
        parent_guid = _optional_text(self.parent_guid, "parent_guid")
        index_hint = self.index_hint
        if index_hint is not None:
            index_hint = _non_negative_int(index_hint, "index_hint")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "guid", guid)
        object.__setattr__(self, "parent_guid", parent_guid)
        object.__setattr__(self, "index_hint", index_hint)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "guid": self.guid,
            "parent_guid": self.parent_guid,
            "index_hint": self.index_hint,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "ObjectRef") -> "ObjectRef":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            raise ContractValidationError(
                "object ref must be a mapping or ObjectRef", field="objects"
            )
        return cls(
            kind=data.get("kind", ""),
            guid=data.get("guid", ""),
            parent_guid=data.get("parent_guid"),
            index_hint=data.get("index_hint"),
        )


@dataclass(frozen=True, slots=True)
class SignalWindow:
    """The stretch of signal an asset covers, on every clock it touches.

    ``project_*`` and ``file_*`` are mandatory because an asset that cannot say
    where it sits in the project is not evidence.  ``source_*`` is optional and
    all-or-nothing: a half-specified source window would silently mean two
    different things to two readers.
    """

    project_start_s: float
    project_end_s: float
    file_start_s: float
    file_end_s: float
    source_start_s: float | None = None
    source_end_s: float | None = None
    boundary: str = "window_bounds"
    mapping_status: MappingStatus = MappingStatus.unverified
    latency_offset_ms: float | None = None
    map_id: str | None = None
    uncertainty_ms: float | None = None

    def __post_init__(self) -> None:
        project_start = _finite(self.project_start_s, "project_start_s")
        project_end = _finite(self.project_end_s, "project_end_s")
        file_start = _finite(self.file_start_s, "file_start_s")
        file_end = _finite(self.file_end_s, "file_end_s")
        if project_start >= project_end:
            raise ContractValidationError(
                "project_start_s must be < project_end_s", field="project_end_s"
            )
        if file_start >= file_end:
            raise ContractValidationError(
                "file_start_s must be < file_end_s", field="file_end_s"
            )

        source_start = self.source_start_s
        source_end = self.source_end_s
        if (source_start is None) != (source_end is None):
            raise ContractValidationError(
                "source_start_s and source_end_s must both be set or both be None",
                field="source_start_s" if source_start is None else "source_end_s",
            )
        if source_start is not None and source_end is not None:
            source_start = _finite(source_start, "source_start_s")
            source_end = _finite(source_end, "source_end_s")
            if source_start >= source_end:
                raise ContractValidationError(
                    "source_start_s must be < source_end_s", field="source_end_s"
                )

        boundary = _text(self.boundary, "boundary")
        if boundary not in SIGNAL_WINDOW_BOUNDARIES:
            raise ContractValidationError(
                f"unknown window boundary: {boundary!r}", field="boundary"
            )
        mapping_status = _enum(
            self.mapping_status, MappingStatus, "mapping_status"
        )

        latency = self.latency_offset_ms
        if latency is not None:
            latency = _non_negative_float(latency, "latency_offset_ms")
        uncertainty = self.uncertainty_ms
        if uncertainty is not None:
            uncertainty = _non_negative_float(uncertainty, "uncertainty_ms")
        map_id = _optional_text(self.map_id, "map_id")

        object.__setattr__(self, "project_start_s", project_start)
        object.__setattr__(self, "project_end_s", project_end)
        object.__setattr__(self, "file_start_s", file_start)
        object.__setattr__(self, "file_end_s", file_end)
        object.__setattr__(self, "source_start_s", source_start)
        object.__setattr__(self, "source_end_s", source_end)
        object.__setattr__(self, "boundary", boundary)
        object.__setattr__(self, "mapping_status", mapping_status)
        object.__setattr__(self, "latency_offset_ms", latency)
        object.__setattr__(self, "map_id", map_id)
        object.__setattr__(self, "uncertainty_ms", uncertainty)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_start_s": self.project_start_s,
            "project_end_s": self.project_end_s,
            "file_start_s": self.file_start_s,
            "file_end_s": self.file_end_s,
            "source_start_s": self.source_start_s,
            "source_end_s": self.source_end_s,
            "boundary": self.boundary,
            "mapping_status": str(self.mapping_status),
            "latency_offset_ms": self.latency_offset_ms,
            "map_id": self.map_id,
            "uncertainty_ms": self.uncertainty_ms,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "SignalWindow") -> "SignalWindow":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            raise ContractValidationError(
                "window must be a mapping or SignalWindow", field="window"
            )
        return cls(
            project_start_s=data.get("project_start_s"),
            project_end_s=data.get("project_end_s"),
            file_start_s=data.get("file_start_s"),
            file_end_s=data.get("file_end_s"),
            source_start_s=data.get("source_start_s"),
            source_end_s=data.get("source_end_s"),
            boundary=data.get("boundary", "window_bounds"),
            mapping_status=data.get("mapping_status", MappingStatus.unverified),
            latency_offset_ms=data.get("latency_offset_ms"),
            map_id=data.get("map_id"),
            uncertainty_ms=data.get("uncertainty_ms"),
        )


def _window(value: Any, field: str) -> SignalWindow | None:
    if value is None:
        return None
    if isinstance(value, SignalWindow):
        return value
    if isinstance(value, Mapping):
        return SignalWindow.from_dict(value)
    raise ContractValidationError(
        f"{field} must be a SignalWindow or None", field=field
    )


# --------------------------------------------------------------------------- #
# assets
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AudioAsset:
    """One hashed piece of audio, with the provenance needed to trust it."""

    asset_id: str
    capture_id: str
    source_guid: str
    stage: Stage
    capture_method: CaptureMethod
    source_kind: SourceKind
    channel_view: ChannelView
    channel_pair: tuple[int, int] | None
    sample_rate: int
    channels: int
    frames: int
    duration_s: float
    bytes: int
    sha256: str
    uri: str | None = None
    lease_id: str | None = None
    lease_expires_at: str | None = None
    status: str = "available"
    window: SignalWindow | None = None
    derivation: str | None = None
    separator: str | None = None

    def __post_init__(self) -> None:
        stage = _enum(self.stage, Stage, "stage")
        capture_method = _enum(self.capture_method, CaptureMethod, "capture_method")
        source_kind = _enum(self.source_kind, SourceKind, "source_kind")
        channel_view = _enum(self.channel_view, ChannelView, "channel_view")

        sha256 = _hex64(self.sha256, "sha256")
        asset_id, embedded = _asset_id(self.asset_id)
        if embedded is not None and embedded != sha256:
            raise ContractValidationError(
                "asset_id must embed sha256 of the asset bytes", field="asset_id"
            )

        capture_id = _text(self.capture_id, "capture_id")
        if not _CAPTURE_ID.match(capture_id):
            raise ContractValidationError("capture_id must be cap_<id>", field="capture_id")
        source_guid = _text(self.source_guid, "source_guid")
        sample_rate = _positive_int(self.sample_rate, "sample_rate")
        channels = _positive_int(self.channels, "channels")
        frames = _non_negative_int(self.frames, "frames")
        duration_s = _finite(self.duration_s, "duration_s")
        if duration_s <= 0:
            raise ContractValidationError("duration_s must be > 0", field="duration_s")
        byte_count = _positive_int(self.bytes, "bytes")

        pair = self.channel_pair
        if isinstance(pair, (list, tuple)):
            if len(pair) != 2:
                raise ContractValidationError(
                    "channel_pair must hold exactly two channel indices",
                    field="channel_pair",
                )
            left = _non_negative_int(pair[0], "channel_pair[0]")
            right = _non_negative_int(pair[1], "channel_pair[1]")
            if left == right:
                raise ContractValidationError(
                    "channel_pair indices must differ", field="channel_pair"
                )
            pair = (left, right)
        elif pair is not None:
            raise ContractValidationError(
                "channel_pair must be a pair of channel indices or None",
                field="channel_pair",
            )
        wants_pair = channel_view is ChannelView.channel_pair
        if wants_pair and pair is None:
            raise ContractValidationError(
                "channel_view 'channel_pair' requires channel_pair", field="channel_pair"
            )
        if not wants_pair and pair is not None:
            raise ContractValidationError(
                "channel_pair is only allowed with channel_view 'channel_pair'",
                field="channel_pair",
            )

        status = _text(self.status, "status")
        if status not in ASSET_STATUSES:
            raise ContractValidationError(
                f"unknown asset status: {status!r}", field="status"
            )

        derivation = _optional_text(self.derivation, "derivation")
        separator = _optional_text(self.separator, "separator")
        if derivation == "separation" and separator is None:
            raise ContractValidationError(
                "derivation 'separation' requires separator", field="separator"
            )

        object.__setattr__(self, "asset_id", asset_id)
        object.__setattr__(self, "capture_id", capture_id)
        object.__setattr__(self, "source_guid", source_guid)
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "capture_method", capture_method)
        object.__setattr__(self, "source_kind", source_kind)
        object.__setattr__(self, "channel_view", channel_view)
        object.__setattr__(self, "channel_pair", pair)
        object.__setattr__(self, "sample_rate", sample_rate)
        object.__setattr__(self, "channels", channels)
        object.__setattr__(self, "frames", frames)
        object.__setattr__(self, "duration_s", duration_s)
        object.__setattr__(self, "bytes", byte_count)
        object.__setattr__(self, "sha256", sha256)
        object.__setattr__(self, "uri", _optional_text(self.uri, "uri"))
        object.__setattr__(self, "lease_id", _optional_text(self.lease_id, "lease_id"))
        object.__setattr__(
            self,
            "lease_expires_at",
            _optional_text(self.lease_expires_at, "lease_expires_at"),
        )
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "window", _window(self.window, "window"))
        object.__setattr__(self, "derivation", derivation)
        object.__setattr__(self, "separator", separator)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "capture_id": self.capture_id,
            "source_guid": self.source_guid,
            "stage": str(self.stage),
            "capture_method": str(self.capture_method),
            "source_kind": str(self.source_kind),
            "channel_view": str(self.channel_view),
            "channel_pair": None if self.channel_pair is None else list(self.channel_pair),
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "frames": self.frames,
            "duration_s": self.duration_s,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "uri": self.uri,
            "lease_id": self.lease_id,
            "lease_expires_at": self.lease_expires_at,
            "status": self.status,
            "window": None if self.window is None else self.window.to_dict(),
            "derivation": self.derivation,
            "separator": self.separator,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "AudioAsset") -> "AudioAsset":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            raise ContractValidationError(
                "asset must be a mapping or AudioAsset", field="assets"
            )
        return cls(
            asset_id=data.get("asset_id", ""),
            capture_id=data.get("capture_id", ""),
            source_guid=data.get("source_guid", ""),
            stage=data.get("stage", ""),
            capture_method=data.get("capture_method", ""),
            source_kind=data.get("source_kind", SourceKind.unknown),
            channel_view=data.get("channel_view", "native"),
            channel_pair=data.get("channel_pair"),
            sample_rate=data.get("sample_rate"),
            channels=data.get("channels"),
            frames=data.get("frames"),
            duration_s=data.get("duration_s"),
            bytes=data.get("bytes"),
            sha256=data.get("sha256", ""),
            uri=data.get("uri"),
            lease_id=data.get("lease_id"),
            lease_expires_at=data.get("lease_expires_at"),
            status=data.get("status", "available"),
            window=data.get("window"),
            derivation=data.get("derivation"),
            separator=data.get("separator"),
        )


# --------------------------------------------------------------------------- #
# capture requests and results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    """A dry-run-able description of audio the caller wants to obtain."""

    request_id: str
    project_id: str
    expected_revision: RevisionRef
    objects: tuple[ObjectRef, ...]
    stages: tuple[Stage, ...]
    method: CaptureMethod
    window: SignalWindow | None = None
    tail_ms: int = 0
    channels: str = "native"
    sample_rate: int | None = None
    isolation: Isolation = Isolation.none
    allow_solo_restore: bool = False
    artifact_role: ArtifactRole = ArtifactRole.temporary
    lease_ttl_s: int = 3600
    dry_run: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        request_id = _text(self.request_id, "request_id")
        project_id = _text(self.project_id, "project_id")
        revision = validate_revision(self.expected_revision)

        raw_objects = self.objects
        if isinstance(raw_objects, (ObjectRef, Mapping)):
            raw_objects = (raw_objects,)
        objects = tuple(
            item if isinstance(item, ObjectRef) else ObjectRef.from_dict(item)
            if isinstance(item, Mapping)
            else _raise_object(item)
            for item in raw_objects
        )
        if not objects:
            raise ContractValidationError(
                "capture request needs at least one object", field="objects"
            )

        raw_stages = self.stages
        if isinstance(raw_stages, str):
            raw_stages = (raw_stages,)
        stages = tuple(_enum(item, Stage, "stages") for item in raw_stages)
        if not stages:
            raise ContractValidationError(
                "capture request needs at least one stage", field="stages"
            )

        method = _enum(self.method, CaptureMethod, "method")
        for stage in stages:
            validate_stage_method(stage, method)

        tail_ms = _non_negative_int(self.tail_ms, "tail_ms")
        channels = _text(self.channels, "channels")
        if channels not in CAPTURE_CHANNEL_MODES:
            raise ContractValidationError(
                "channels must be native, mono or stereo", field="channels"
            )
        sample_rate = self.sample_rate
        if sample_rate is not None:
            sample_rate = _positive_int(sample_rate, "sample_rate")
        isolation = _enum(self.isolation, Isolation, "isolation")
        artifact_role = _enum(self.artifact_role, ArtifactRole, "artifact_role")
        lease_ttl_s = _positive_int(self.lease_ttl_s, "lease_ttl_s")
        allow_solo_restore = _bool(self.allow_solo_restore, "allow_solo_restore")
        dry_run = _bool(self.dry_run, "dry_run")
        reason = self.reason
        if not isinstance(reason, str):
            raise ContractValidationError("reason must be a string", field="reason")

        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "expected_revision", revision)
        object.__setattr__(self, "objects", objects)
        object.__setattr__(self, "stages", stages)
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "window", _window(self.window, "window"))
        object.__setattr__(self, "tail_ms", tail_ms)
        object.__setattr__(self, "channels", channels)
        object.__setattr__(self, "sample_rate", sample_rate)
        object.__setattr__(self, "isolation", isolation)
        object.__setattr__(self, "allow_solo_restore", allow_solo_restore)
        object.__setattr__(self, "artifact_role", artifact_role)
        object.__setattr__(self, "lease_ttl_s", lease_ttl_s)
        object.__setattr__(self, "dry_run", dry_run)
        object.__setattr__(self, "reason", reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "project_id": self.project_id,
            "expected_revision": self.expected_revision.to_dict(),
            "objects": [item.to_dict() for item in self.objects],
            "stages": [str(item) for item in self.stages],
            "method": str(self.method),
            "window": None if self.window is None else self.window.to_dict(),
            "tail_ms": self.tail_ms,
            "channels": self.channels,
            "sample_rate": self.sample_rate,
            "isolation": str(self.isolation),
            "allow_solo_restore": self.allow_solo_restore,
            "artifact_role": str(self.artifact_role),
            "lease_ttl_s": self.lease_ttl_s,
            "dry_run": self.dry_run,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "CaptureRequest") -> "CaptureRequest":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            raise ContractValidationError(
                "capture request must be a mapping or CaptureRequest",
                field="request",
            )
        return cls(
            request_id=data.get("request_id", ""),
            project_id=data.get("project_id", ""),
            expected_revision=data.get("expected_revision", {}),
            objects=data.get("objects", ()),
            stages=data.get("stages", ()),
            method=data.get("method", ""),
            window=data.get("window"),
            tail_ms=data.get("tail_ms", 0),
            channels=data.get("channels", "native"),
            sample_rate=data.get("sample_rate"),
            isolation=data.get("isolation", Isolation.none),
            allow_solo_restore=data.get("allow_solo_restore", False),
            artifact_role=data.get("artifact_role", ArtifactRole.temporary),
            lease_ttl_s=data.get("lease_ttl_s", 3600),
            dry_run=data.get("dry_run", False),
            reason=data.get("reason", ""),
        )


def _raise_object(value: Any) -> Any:
    raise ContractValidationError(
        "objects must contain ObjectRef values", field="objects"
    )


@dataclass(frozen=True, slots=True)
class Capture:
    """The record of one capture run: what came back, what did not, why."""

    capture_id: str
    request_id: str
    method: CaptureMethod
    stages: tuple[Stage, ...]
    assets: tuple[AudioAsset, ...]
    revision_before: RevisionRef
    revision_after: RevisionRef | None
    render_settings: Mapping[str, Any]
    restore_report: Mapping[str, Any]
    solo_active: bool
    tail_s: float
    coverage: Mapping[str, Any]
    missing: tuple[str, ...]
    warnings: tuple[str, ...]
    status: str

    def __post_init__(self) -> None:
        capture_id = _text(self.capture_id, "capture_id")
        if not _CAPTURE_ID.match(capture_id):
            raise ContractValidationError("capture_id must be cap_<id>", field="capture_id")
        request_id = _text(self.request_id, "request_id")
        method = _enum(self.method, CaptureMethod, "method")

        raw_stages = self.stages
        if isinstance(raw_stages, str):
            raw_stages = (raw_stages,)
        stages = tuple(_enum(item, Stage, "stages") for item in raw_stages)

        raw_assets = self.assets
        if isinstance(raw_assets, AudioAsset):
            raw_assets = (raw_assets,)
        assets = tuple(
            item if isinstance(item, AudioAsset) else AudioAsset.from_dict(item)
            if isinstance(item, Mapping)
            else _raise_asset(item)
            for item in raw_assets
        )

        revision_before = validate_revision(self.revision_before)
        revision_after = self.revision_after
        if revision_after is not None:
            revision_after = validate_revision(revision_after)
        solo_active = _bool(self.solo_active, "solo_active")
        tail_s = _non_negative_float(self.tail_s, "tail_s")
        missing = _text_tuple(self.missing, "missing")
        warnings = _text_tuple(self.warnings, "warnings")
        status = _text(self.status, "status")
        if status not in CAPTURE_STATUSES:
            raise ContractValidationError(
                f"unknown capture status: {status!r}", field="status"
            )
        if status == "complete" and missing:
            raise ContractValidationError(
                "a complete capture cannot report missing assets", field="missing"
            )

        object.__setattr__(self, "capture_id", capture_id)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "stages", stages)
        object.__setattr__(self, "assets", assets)
        object.__setattr__(self, "revision_before", revision_before)
        object.__setattr__(self, "revision_after", revision_after)
        object.__setattr__(
            self, "render_settings", _mapping(self.render_settings, "render_settings")
        )
        object.__setattr__(
            self, "restore_report", _mapping(self.restore_report, "restore_report")
        )
        object.__setattr__(self, "solo_active", solo_active)
        object.__setattr__(self, "tail_s", tail_s)
        object.__setattr__(self, "coverage", _mapping(self.coverage, "coverage"))
        object.__setattr__(self, "missing", missing)
        object.__setattr__(self, "warnings", warnings)
        object.__setattr__(self, "status", status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "request_id": self.request_id,
            "method": str(self.method),
            "stages": [str(item) for item in self.stages],
            "assets": [item.to_dict() for item in self.assets],
            "revision_before": self.revision_before.to_dict(),
            "revision_after": (
                None if self.revision_after is None else self.revision_after.to_dict()
            ),
            "render_settings": dict(self.render_settings),
            "restore_report": dict(self.restore_report),
            "solo_active": self.solo_active,
            "tail_s": self.tail_s,
            "coverage": dict(self.coverage),
            "missing": list(self.missing),
            "warnings": list(self.warnings),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "Capture") -> "Capture":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            raise ContractValidationError(
                "capture must be a mapping or Capture", field="capture"
            )
        return cls(
            capture_id=data.get("capture_id", ""),
            request_id=data.get("request_id", ""),
            method=data.get("method", ""),
            stages=data.get("stages", ()),
            assets=data.get("assets", ()),
            revision_before=data.get("revision_before", {}),
            revision_after=data.get("revision_after"),
            render_settings=data.get("render_settings", {}),
            restore_report=data.get("restore_report", {}),
            solo_active=data.get("solo_active", False),
            tail_s=data.get("tail_s", 0.0),
            coverage=data.get("coverage", {}),
            missing=data.get("missing", ()),
            warnings=data.get("warnings", ()),
            status=data.get("status", ""),
        )


def _raise_asset(value: Any) -> Any:
    raise ContractValidationError("assets must contain AudioAsset values", field="assets")


# --------------------------------------------------------------------------- #
# delivery and listening
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DeliveryEvidence:
    """What is known about a listener's copy of an asset.

    ``heard`` is true only for ``forwarded_verified``: a URI that was sent is
    not audio that arrived.
    """

    status: DeliveryStatus = DeliveryStatus.not_requested
    checked_at: str | None = None
    method: str | None = None
    detail: str | None = None
    canary: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", _enum(self.status, DeliveryStatus, "status")
        )
        object.__setattr__(self, "checked_at", _optional_text(self.checked_at, "checked_at"))
        object.__setattr__(self, "method", _optional_text(self.method, "method"))
        object.__setattr__(self, "detail", _optional_text(self.detail, "detail"))
        object.__setattr__(self, "canary", _optional_text(self.canary, "canary"))

    @property
    def heard(self) -> bool:
        """True only when delivery was forwarded and then verified."""
        return self.status is DeliveryStatus.forwarded_verified

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "checked_at": self.checked_at,
            "method": self.method,
            "detail": self.detail,
            "canary": self.canary,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "DeliveryEvidence") -> "DeliveryEvidence":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            raise ContractValidationError(
                "delivery must be a mapping or DeliveryEvidence", field="delivery"
            )
        return cls(
            status=data.get("status", DeliveryStatus.not_requested),
            checked_at=data.get("checked_at"),
            method=data.get("method"),
            detail=data.get("detail"),
            canary=data.get("canary"),
        )


@dataclass(frozen=True, slots=True)
class ListenRequest:
    """A request for a model to describe one asset window."""

    request_id: str
    asset_id: str
    channel_view: ChannelView
    window: SignalWindow | None
    prompt: str
    model: str
    model_revision: str | None = None
    prompt_sha256: str | None = None

    def __post_init__(self) -> None:
        request_id = _text(self.request_id, "request_id")
        asset_id, _ = _asset_id(self.asset_id)
        channel_view = _enum(self.channel_view, ChannelView, "channel_view")
        prompt = _text(self.prompt, "prompt")
        model = _text(self.model, "model")
        model_revision = _optional_text(self.model_revision, "model_revision")
        prompt_sha256 = _optional_hex64(self.prompt_sha256, "prompt_sha256")

        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "asset_id", asset_id)
        object.__setattr__(self, "channel_view", channel_view)
        object.__setattr__(self, "window", _window(self.window, "window"))
        object.__setattr__(self, "prompt", prompt)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "model_revision", model_revision)
        object.__setattr__(self, "prompt_sha256", prompt_sha256)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "asset_id": self.asset_id,
            "channel_view": str(self.channel_view),
            "window": None if self.window is None else self.window.to_dict(),
            "prompt": self.prompt,
            "model": self.model,
            "model_revision": self.model_revision,
            "prompt_sha256": self.prompt_sha256,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "ListenRequest") -> "ListenRequest":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            raise ContractValidationError(
                "listen request must be a mapping or ListenRequest", field="request"
            )
        return cls(
            request_id=data.get("request_id", ""),
            asset_id=data.get("asset_id", ""),
            channel_view=data.get("channel_view", ""),
            window=data.get("window"),
            prompt=data.get("prompt", ""),
            model=data.get("model", ""),
            model_revision=data.get("model_revision"),
            prompt_sha256=data.get("prompt_sha256"),
        )


@dataclass(frozen=True, slots=True)
class ListenResult:
    """What a listener reported, and how much of it the system may rely on.

    Field order differs from the prose spec only where dataclass defaults
    require it: required fields come first, defaulted ones after.
    """

    observation_id: str
    request_id: str
    asset_id: str
    channel_view: ChannelView
    window: SignalWindow | None
    text: str
    model: str
    claims: tuple[str, ...] = ()
    delivery: DeliveryEvidence = DeliveryEvidence()
    model_revision: str | None = None
    weights_sha256: str | None = None
    prompt_sha256: str | None = None
    context_injected: bool = False
    limits: tuple[str, ...] = ()
    reliability: str = "uncalibrated"

    def __post_init__(self) -> None:
        observation_id = _text(self.observation_id, "observation_id")
        if not _OBSERVATION_ID.match(observation_id):
            raise ContractValidationError(
                "observation_id must be obs_<id>", field="observation_id"
            )
        request_id = _text(self.request_id, "request_id")
        asset_id, _ = _asset_id(self.asset_id)
        channel_view = _enum(self.channel_view, ChannelView, "channel_view")
        if not isinstance(self.text, str):
            raise ContractValidationError("text must be a string", field="text")
        text = self.text
        model = _text(self.model, "model")

        delivery = self.delivery
        if isinstance(delivery, Mapping):
            delivery = DeliveryEvidence.from_dict(delivery)
        if not isinstance(delivery, DeliveryEvidence):
            raise ContractValidationError(
                "delivery must be a DeliveryEvidence", field="delivery"
            )
        if delivery.status is DeliveryStatus.forwarded_verified and (
            delivery.checked_at is None or delivery.method is None
        ):
            raise ContractValidationError(
                "forwarded_verified delivery requires checked_at and method",
                field="delivery",
                next_action="forward_and_confirm",
            )
        if not text.strip() and delivery.status not in {
            DeliveryStatus.unsupported,
            DeliveryStatus.failed,
        }:
            raise ContractValidationError(
                "empty text is only allowed for unsupported or failed delivery",
                field="text",
                next_action="retry_or_mark_unsupported",
            )

        object.__setattr__(self, "observation_id", observation_id)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "asset_id", asset_id)
        object.__setattr__(self, "channel_view", channel_view)
        object.__setattr__(self, "window", _window(self.window, "window"))
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "claims", _text_tuple(self.claims, "claims"))
        object.__setattr__(self, "delivery", delivery)
        object.__setattr__(
            self, "model_revision", _optional_text(self.model_revision, "model_revision")
        )
        object.__setattr__(
            self, "weights_sha256", _optional_hex64(self.weights_sha256, "weights_sha256")
        )
        object.__setattr__(
            self, "prompt_sha256", _optional_hex64(self.prompt_sha256, "prompt_sha256")
        )
        object.__setattr__(
            self, "context_injected", _bool(self.context_injected, "context_injected")
        )
        object.__setattr__(self, "limits", _text_tuple(self.limits, "limits"))
        object.__setattr__(self, "reliability", _text(self.reliability, "reliability"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "request_id": self.request_id,
            "asset_id": self.asset_id,
            "channel_view": str(self.channel_view),
            "window": None if self.window is None else self.window.to_dict(),
            "text": self.text,
            "model": self.model,
            "claims": list(self.claims),
            "delivery": self.delivery.to_dict(),
            "model_revision": self.model_revision,
            "weights_sha256": self.weights_sha256,
            "prompt_sha256": self.prompt_sha256,
            "context_injected": self.context_injected,
            "limits": list(self.limits),
            "reliability": self.reliability,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | "ListenResult") -> "ListenResult":
        if isinstance(data, cls):
            return data
        if not isinstance(data, Mapping):
            raise ContractValidationError(
                "listen result must be a mapping or ListenResult", field="result"
            )
        return cls(
            observation_id=data.get("observation_id", ""),
            request_id=data.get("request_id", ""),
            asset_id=data.get("asset_id", ""),
            channel_view=data.get("channel_view", ""),
            window=data.get("window"),
            text=data.get("text", ""),
            model=data.get("model", ""),
            claims=data.get("claims", ()),
            delivery=data.get("delivery", DeliveryEvidence()),
            model_revision=data.get("model_revision"),
            weights_sha256=data.get("weights_sha256"),
            prompt_sha256=data.get("prompt_sha256"),
            context_injected=data.get("context_injected", False),
            limits=data.get("limits", ()),
            reliability=data.get("reliability", "uncalibrated"),
        )


def dumps_audio_record(record: Any) -> str:
    """Serialize an audio record as strict, deterministic JSON."""
    to_dict = getattr(record, "to_dict", None)
    if not callable(to_dict):
        raise ContractValidationError(
            "audio record must expose to_dict()", field="record"
        )
    return json.dumps(
        to_dict(),
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    )


def loads_audio_record(cls: type[RecordT], text: str) -> RecordT:
    """Parse JSON produced by ``dumps_audio_record`` back into ``cls``."""
    if not isinstance(text, str):
        raise ContractValidationError("record text must be a string", field="record")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractValidationError(
            "record text is not valid JSON", field="record"
        ) from exc
    if not isinstance(data, Mapping):
        raise ContractValidationError("record must be a JSON object", field="record")
    return cls.from_dict(data)


__all__ = [
    "Stage",
    "CaptureMethod",
    "SourceKind",
    "ChannelView",
    "DeliveryStatus",
    "ObjectKind",
    "ArtifactRole",
    "Isolation",
    "ObjectRef",
    "SignalWindow",
    "AudioAsset",
    "CaptureRequest",
    "Capture",
    "DeliveryEvidence",
    "ListenRequest",
    "ListenResult",
    "CaptureStageUnsupportedError",
    "STAGE_METHODS",
    "SIGNAL_WINDOW_BOUNDARIES",
    "CAPTURE_CHANNEL_MODES",
    "ASSET_STATUSES",
    "CAPTURE_STATUSES",
    "validate_stage_method",
    "dumps_audio_record",
    "loads_audio_record",
]
