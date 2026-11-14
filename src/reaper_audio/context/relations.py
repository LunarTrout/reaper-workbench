"""Typed, revision-bound relation records for the unified panorama.

The analysis layer decides *what* was measured (``analysis.relations``); this
module decides *how that measurement is identified and bound*.  A relation is
only valid inside one capture, revision, window, stage, node and channel view,
and its identifier is derived from exactly those facts plus the method version.

No relation carries a quality score.  Every record has raw metrics with units,
coverage, an explicit uncertainty record, at least two alternatives and a
``dep_set`` that names the assets and the revision it depends on.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .errors import ContractValidationError
from .ids import derive_id, validate_id

RELATION_SCHEMA_VERSION = 1

RELATION_KINDS = frozenset(
    {
        "band_dominance",
        "band_dominance_pair",
        "phase_pair",
        "track_master_level_relation",
        "reference_comparison",
        "partial_loudness_share",
    }
)
MANDATORY_RELATION_KINDS = frozenset(
    {"band_dominance", "track_master_level_relation"}
)
ENDPOINT_TYPES = frozenset({"source", "master", "reference"})
COVERAGE_STATUSES = frozenset({"complete", "partial", "unavailable"})
UNCERTAINTY_STATUSES = frozenset({"measured", "unknown", "not_applicable"})
BAND_PAIR_METHOD = "analysis.relations.band_dominance_pair"
BAND_PAIR_METHOD_VERSION = "1"


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(
            f"{field_name} must be a non-empty string", field=field_name
        )
    return value


def _stable_id(value: Any, field_name: str) -> str:
    text = _text(value, field_name)
    if any(character.isspace() for character in text):
        raise ContractValidationError(
            f"{field_name} must not contain whitespace", field=field_name
        )
    return text


def _finite(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractValidationError(
            f"{field_name} must be a finite number", field=field_name
        )
    number = float(value)
    if not math.isfinite(number):
        raise ContractValidationError(
            f"{field_name} must be finite", field=field_name
        )
    return number


def _json_safe(value: Any, field_name: str) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractValidationError(
                f"{field_name} contains a non-finite number", field=field_name
            )
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ContractValidationError(
                    f"{field_name} keys must be non-empty strings", field=field_name
                )
            result[key] = _json_safe(item, f"{field_name}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, f"{field_name}[]") for item in value]
    raise ContractValidationError(
        f"{field_name} contains an unsupported value of type {type(value).__name__}",
        field=field_name,
    )


@dataclass(frozen=True, slots=True)
class RelationWindow:
    """One half-open evidence interval in project seconds and samples."""

    start_s: float
    end_s: float
    start_sample: int | None = None
    end_sample: int | None = None
    sample_rate_hz: int | None = None

    def __post_init__(self) -> None:
        start = _finite(self.start_s, "window.start_s")
        end = _finite(self.end_s, "window.end_s")
        if end <= start:
            raise ContractValidationError(
                "window.end_s must be greater than window.start_s", field="window"
            )
        for name in ("start_sample", "end_sample"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ContractValidationError(
                    f"window.{name} must be a non-negative integer or null",
                    field=f"window.{name}",
                )
        if self.start_sample is not None and self.end_sample is not None:
            if self.end_sample <= self.start_sample:
                raise ContractValidationError(
                    "window.end_sample must be greater than window.start_sample",
                    field="window",
                )
        if self.sample_rate_hz is not None:
            if (
                isinstance(self.sample_rate_hz, bool)
                or not isinstance(self.sample_rate_hz, int)
                or self.sample_rate_hz <= 0
            ):
                raise ContractValidationError(
                    "window.sample_rate_hz must be a positive integer or null",
                    field="window.sample_rate_hz",
                )
            if self.start_sample is not None and self.end_sample is not None:
                expected = (self.end_sample - self.start_sample) / self.sample_rate_hz
                if abs(expected - (end - start)) > 1.0 / self.sample_rate_hz:
                    raise ContractValidationError(
                        "window sample range and seconds range disagree",
                        field="window",
                        details={
                            "sample_duration_s": expected,
                            "second_duration_s": end - start,
                        },
                    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_s": float(self.start_s),
            "end_s": float(self.end_s),
            "start_sample": self.start_sample,
            "end_sample": self.end_sample,
            "sample_rate_hz": self.sample_rate_hz,
            "units": "project_seconds_and_samples",
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RelationWindow":
        if not isinstance(data, Mapping):
            raise ContractValidationError("window must be a mapping", field="window")
        return cls(
            start_s=data.get("start_s"),
            end_s=data.get("end_s"),
            start_sample=data.get("start_sample"),
            end_sample=data.get("end_sample"),
            sample_rate_hz=data.get("sample_rate_hz"),
        )


@dataclass(frozen=True, slots=True)
class RelationBinding:
    """The one capture/revision context every relation in a set shares."""

    capture_id: str
    project_id: str
    revision_fingerprint: str
    fact_root: str
    window: RelationWindow
    stage: str
    node: str
    channel_view: str
    render_key: str | None = None
    producer: str = "reaper_audio.context.relations"
    created_at: str | None = None

    def __post_init__(self) -> None:
        _stable_id(self.capture_id, "capture_id")
        _stable_id(self.project_id, "project_id")
        _text(self.revision_fingerprint, "revision_fingerprint")
        _text(self.fact_root, "fact_root")
        if not isinstance(self.window, RelationWindow):
            raise ContractValidationError(
                "window must be a RelationWindow", field="window"
            )
        _text(self.stage, "stage")
        _text(self.node, "node")
        _text(self.channel_view, "channel_view")
        if self.render_key is not None:
            _text(self.render_key, "render_key")
        _text(self.producer, "producer")
        if self.created_at is not None:
            _text(self.created_at, "created_at")

    @property
    def revision_key(self) -> str:
        """The identity used for relation IDs: render key, else revision."""

        return self.render_key or self.revision_fingerprint

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "project_id": self.project_id,
            "revision_fingerprint": self.revision_fingerprint,
            "fact_root": self.fact_root,
            "render_key": self.render_key,
            "window": self.window.to_dict(),
            "stage": self.stage,
            "node": self.node,
            "channel_view": self.channel_view,
            "producer": self.producer,
            "created_at": self.created_at,
        }


def derive_relation_id(
    *,
    kind: str,
    from_id: str,
    to_id: str,
    window: RelationWindow,
    stage: str,
    node: str,
    channel_view: str,
    method: str,
    method_version: str,
    revision_key: str,
) -> str:
    """Derive the reproducible identifier for one relation.

    The natural key is exactly the typed endpoints, window, processing stage,
    method and method version; the revision key (render key when available,
    else the revision fingerprint) makes a recapture with identical inputs
    reproduce the same id and a changed input produce a different one.
    """

    natural_key = {
        "kind": _text(kind, "kind"),
        "from_id": _stable_id(from_id, "from_id"),
        "to_id": _stable_id(to_id, "to_id"),
        "window": window.to_dict(),
        "stage": _text(stage, "stage"),
        "node": _text(node, "node"),
        "channel_view": _text(channel_view, "channel_view"),
        "method": _text(method, "method"),
    }
    return derive_id(
        "rel",
        natural_key,
        _text(method_version, "method_version"),
        _text(revision_key, "revision_key"),
    )


@dataclass(frozen=True, slots=True)
class RelationRecord:
    """One typed relation inside a capture-bound relation set."""

    relation_id: str
    kind: str
    from_type: str
    from_id: str
    to_type: str
    to_id: str
    window: RelationWindow
    stage: str
    node: str
    channel_view: str
    method: str
    method_version: str
    metrics: Mapping[str, float]
    units: Mapping[str, str]
    attributes: Mapping[str, Any]
    coverage: Mapping[str, Any]
    uncertainty: Mapping[str, Any]
    alternatives: tuple[str, ...]
    interpretation: str
    provenance: Mapping[str, Any]
    dep_set: Mapping[str, Any]

    def __post_init__(self) -> None:
        validate_id(self.relation_id, "rel")
        if self.kind not in RELATION_KINDS:
            raise ContractValidationError(
                f"unknown relation kind: {self.kind!r}", field="kind"
            )
        if self.from_type not in ENDPOINT_TYPES or self.to_type not in ENDPOINT_TYPES:
            raise ContractValidationError(
                "endpoint types must be source, master or reference", field="endpoint"
            )
        _stable_id(self.from_id, "from_id")
        _stable_id(self.to_id, "to_id")
        if not isinstance(self.window, RelationWindow):
            raise ContractValidationError(
                "window must be a RelationWindow", field="window"
            )
        _text(self.stage, "stage")
        _text(self.node, "node")
        _text(self.channel_view, "channel_view")
        _text(self.method, "method")
        _text(self.method_version, "method_version")

        if not isinstance(self.metrics, Mapping) or not self.metrics:
            raise ContractValidationError(
                "metrics must be a non-empty mapping", field="metrics"
            )
        normalized_metrics: dict[str, float] = {}
        for name, value in self.metrics.items():
            if not isinstance(name, str) or not name:
                raise ContractValidationError(
                    "metric names must be non-empty strings", field="metrics"
                )
            normalized_metrics[name] = _finite(value, f"metrics.{name}")
        if not isinstance(self.units, Mapping):
            raise ContractValidationError("units must be a mapping", field="units")
        if set(self.units) != set(normalized_metrics):
            raise ContractValidationError(
                "units must name exactly the metric keys", field="units"
            )
        for name, unit in self.units.items():
            _text(unit, f"units.{name}")
        object.__setattr__(self, "metrics", normalized_metrics)

        if not isinstance(self.attributes, Mapping):
            raise ContractValidationError(
                "attributes must be a mapping", field="attributes"
            )
        object.__setattr__(
            self, "attributes", _json_safe(self.attributes, "attributes")
        )
        if self.kind == "band_dominance":
            if self.attributes.get("is_psychoacoustic_threshold") is not False:
                raise ContractValidationError(
                    "band_dominance must declare is_psychoacoustic_threshold=false",
                    field="attributes.is_psychoacoustic_threshold",
                )

        if not isinstance(self.coverage, Mapping):
            raise ContractValidationError("coverage must be a mapping", field="coverage")
        status = self.coverage.get("status")
        if status not in COVERAGE_STATUSES:
            raise ContractValidationError(
                "coverage.status must be complete, partial or unavailable",
                field="coverage.status",
            )
        if status != "complete" and not self.coverage.get("reasons"):
            raise ContractValidationError(
                "non-complete coverage must name its reasons",
                field="coverage.reasons",
            )
        object.__setattr__(self, "coverage", _json_safe(self.coverage, "coverage"))

        if not isinstance(self.uncertainty, Mapping):
            raise ContractValidationError(
                "uncertainty must be a mapping", field="uncertainty"
            )
        uncertainty_status = self.uncertainty.get("status")
        if uncertainty_status not in UNCERTAINTY_STATUSES:
            raise ContractValidationError(
                "uncertainty.status must be measured, unknown or not_applicable",
                field="uncertainty.status",
            )
        if uncertainty_status == "unknown" and not self.uncertainty.get("reason"):
            raise ContractValidationError(
                "unknown uncertainty must name a reason", field="uncertainty.reason"
            )
        object.__setattr__(
            self, "uncertainty", _json_safe(self.uncertainty, "uncertainty")
        )

        if (
            not isinstance(self.alternatives, Sequence)
            or isinstance(self.alternatives, (str, bytes))
            or len(self.alternatives) < 2
        ):
            raise ContractValidationError(
                "every relation must name at least two alternatives",
                field="alternatives",
            )
        alternatives: list[str] = []
        for index, alternative in enumerate(self.alternatives):
            alternatives.append(_text(alternative, f"alternatives[{index}]"))
        object.__setattr__(self, "alternatives", tuple(alternatives))
        _text(self.interpretation, "interpretation")

        if not isinstance(self.provenance, Mapping):
            raise ContractValidationError(
                "provenance must be a mapping", field="provenance"
            )
        for required in ("capture_id", "project_id", "revision_fingerprint", "asset_ids"):
            if required not in self.provenance:
                raise ContractValidationError(
                    f"provenance is missing {required}", field=f"provenance.{required}"
                )
        if not isinstance(self.dep_set, Mapping):
            raise ContractValidationError("dep_set must be a mapping", field="dep_set")
        for required in ("capture_id", "revision", "asset_ids", "method_family"):
            if required not in self.dep_set:
                raise ContractValidationError(
                    f"dep_set is missing {required}", field=f"dep_set.{required}"
                )
        object.__setattr__(self, "provenance", _json_safe(self.provenance, "provenance"))
        object.__setattr__(self, "dep_set", _json_safe(self.dep_set, "dep_set"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "relation_id": self.relation_id,
            "kind": self.kind,
            "from_type": self.from_type,
            "from_id": self.from_id,
            "to_type": self.to_type,
            "to_id": self.to_id,
            "window": self.window.to_dict(),
            "stage": self.stage,
            "node": self.node,
            "channel_view": self.channel_view,
            "method": self.method,
            "method_version": self.method_version,
            "metrics": dict(self.metrics),
            "units": dict(self.units),
            "attributes": dict(self.attributes),
            "coverage": dict(self.coverage),
            "uncertainty": dict(self.uncertainty),
            "alternatives": list(self.alternatives),
            "interpretation": self.interpretation,
            "provenance": dict(self.provenance),
            "dep_set": dict(self.dep_set),
        }


def _endpoint_types(kind: str) -> tuple[str, str]:
    if kind in {"band_dominance", "band_dominance_pair", "phase_pair"}:
        return ("source", "source")
    if kind in {"track_master_level_relation", "partial_loudness_share"}:
        return ("source", "master")
    if kind == "reference_comparison":
        return ("master", "reference")
    raise ContractValidationError(f"unknown relation kind: {kind!r}", field="kind")


def _record_from_candidate(
    candidate: Any,
    binding: RelationBinding,
    *,
    source_assets: Mapping[str, str],
    master_id: str | None,
) -> RelationRecord:
    kind = candidate.kind
    from_type, to_type = _endpoint_types(kind)
    from_id = candidate.from_id
    to_id = candidate.to_id
    if kind == "reference_comparison" and from_id == "project":
        if not master_id:
            raise ContractValidationError(
                "reference relations need the master endpoint id",
                field="master_id",
            )
        from_id = master_id

    from_asset = source_assets.get(from_id)
    to_asset = source_assets.get(to_id) if to_type != "reference" else None
    asset_ids = tuple(
        sorted({asset for asset in (from_asset, to_asset) if isinstance(asset, str) and asset})
    )
    if not asset_ids:
        raise ContractValidationError(
            "a relation must name at least one input asset", field="asset_ids"
        )

    method_family = candidate.method.rsplit(".", 1)[0] if "." in candidate.method else candidate.method
    dep_set = {
        "capture_id": binding.capture_id,
        "revision": binding.revision_fingerprint,
        "asset_ids": list(asset_ids),
        "method_family": method_family,
    }
    provenance = {
        "capture_id": binding.capture_id,
        "project_id": binding.project_id,
        "revision_fingerprint": binding.revision_fingerprint,
        "fact_root": binding.fact_root,
        "render_key": binding.render_key,
        "asset_ids": list(asset_ids),
        "producer": binding.producer,
        "created_at": binding.created_at,
        "method": candidate.method,
        "method_version": candidate.method_version,
    }
    return RelationRecord(
        relation_id=derive_relation_id(
            kind=kind,
            from_id=from_id,
            to_id=to_id,
            window=binding.window,
            stage=binding.stage,
            node=binding.node,
            channel_view=binding.channel_view,
            method=candidate.method,
            method_version=candidate.method_version,
            revision_key=binding.revision_key,
        ),
        kind=kind,
        from_type=from_type,
        from_id=from_id,
        to_type=to_type,
        to_id=to_id,
        window=binding.window,
        stage=binding.stage,
        node=binding.node,
        channel_view=binding.channel_view,
        method=candidate.method,
        method_version=candidate.method_version,
        metrics=dict(candidate.metrics),
        units=dict(candidate.units),
        attributes=dict(candidate.attributes),
        coverage=dict(candidate.coverage),
        uncertainty=dict(candidate.uncertainty),
        alternatives=tuple(candidate.alternatives),
        interpretation=candidate.interpretation,
        provenance=provenance,
        dep_set=dep_set,
    )


@dataclass(frozen=True, slots=True)
class RelationSet:
    """All canonical relations and pair evaluations for one capture window."""

    binding: RelationBinding
    relations: tuple[RelationRecord, ...]
    coverage: Mapping[str, Any]
    unavailable: Mapping[str, str]
    truncated: Mapping[str, Any]
    evaluations: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = RELATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RELATION_SCHEMA_VERSION:
            raise ContractValidationError(
                f"unsupported relation schema version: {self.schema_version}",
                field="schema_version",
            )
        if not isinstance(self.binding, RelationBinding):
            raise ContractValidationError(
                "binding must be a RelationBinding", field="binding"
            )
        for relation in self.relations:
            if not isinstance(relation, RelationRecord):
                raise ContractValidationError(
                    "relations must be RelationRecord instances", field="relations"
                )
            if relation.window != self.binding.window:
                raise ContractValidationError(
                    "every relation must share the set window", field="window"
                )
            provenance_capture = relation.provenance.get("capture_id")
            if provenance_capture != self.binding.capture_id:
                raise ContractValidationError(
                    "relation provenance does not match the set capture",
                    field="provenance.capture_id",
                )
        object.__setattr__(
            self, "evaluations", _json_safe(self.evaluations, "evaluations")
        )

    @property
    def is_complete(self) -> bool:
        """True only when every mandatory kind was fully evaluated.

        A thresholded or top-K list does not make a set complete.  Completeness
        is ``evaluated == expected`` per mandatory kind, no unavailable unit and
        no truncated projection.  Emitted relations may be fewer than expected
        because ``no_dominant_band`` is a valid evaluated result.
        """

        if self.truncated:
            return False
        for kind in MANDATORY_RELATION_KINDS:
            coverage = self.coverage.get(kind)
            if not isinstance(coverage, Mapping):
                return False
            if coverage.get("status") != "complete":
                return False
            if int(coverage.get("evaluated", 0)) != int(coverage.get("expected", 0)):
                return False
        return True

    def _payload(self) -> dict[str, Any]:
        ordered = sorted(
            self.relations,
            key=lambda item: (item.kind, item.from_id, item.to_id, item.relation_id),
        )
        return {
            "schema_version": self.schema_version,
            "binding": self.binding.to_dict(),
            "complete": self.is_complete,
            "completeness_scope": (
                "every mandatory relation unit was evaluated with its coverage; "
                "temporal series are preserved separately and medians are summaries"
            ),
            "relations": [relation.to_dict() for relation in ordered],
            "evaluations": dict(self.evaluations),
            "coverage": dict(self.coverage),
            "unavailable": dict(self.unavailable),
            "truncated": dict(self.truncated),
        }

    def content_hash(self) -> str:
        payload = json.dumps(
            self._payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload()
        payload["content_hash"] = self.content_hash()
        return payload

    def projection(self, *, limit: int) -> dict[str, Any]:
        """A bounded convenience view; never a complete or ackable document."""

        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ContractValidationError(
                "limit must be a positive integer", field="limit"
            )
        ordered = sorted(
            self.relations,
            key=lambda item: (
                -float(item.metrics.get("band_delta_db", 0.0)),
                item.kind,
                item.relation_id,
            ),
        )
        selected = ordered[:limit]
        return {
            "schema_version": self.schema_version,
            "binding": self.binding.to_dict(),
            "complete": False,
            "reason": "bounded_projection",
            "returned": len(selected),
            "expected": {
                kind: int(value.get("expected", 0))
                for kind, value in self.coverage.items()
            },
            "evaluated": {
                kind: int(value.get("evaluated", 0))
                for kind, value in self.coverage.items()
            },
            "truncated": {
                "rule": "top_k_by_band_delta_db",
                "dropped": max(0, len(ordered) - len(selected)),
            },
            "relations": [relation.to_dict() for relation in selected],
            "canonical_content_hash": self.content_hash(),
        }


def _enrich_evaluations(
    binding: RelationBinding,
    tables: Mapping[str, Any],
    *,
    source_assets: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Give every canonical table row a stable typed address and binding."""

    enriched: dict[str, Any] = {}
    for name, table in tables.items():
        if not isinstance(table, Mapping):
            continue
        columns = list(table.get("columns") or [])
        rows = [list(row) for row in (table.get("rows") or [])]
        if name == "band_dominance_pairs":
            for column in ("relation_id", "dep_set"):
                if column not in columns:
                    columns.append(column)
            id_index = columns.index("relation_id")
            dep_index = columns.index("dep_set")
            for row in rows:
                while len(row) < len(columns) - 2:
                    row.append(None)
                mapping = dict(zip(columns, row))
                from_id = str(mapping.get("from_id") or "")
                to_id = str(mapping.get("to_id") or "")
                asset_ids = sorted({
                    asset for asset in (
                        (source_assets or {}).get(from_id),
                        (source_assets or {}).get(to_id),
                    ) if isinstance(asset, str) and asset
                })
                relation_id = derive_relation_id(
                    kind="band_dominance_pair",
                    from_id=from_id,
                    to_id=to_id,
                    window=binding.window,
                    stage=binding.stage,
                    node=binding.node,
                    channel_view=binding.channel_view,
                    method=BAND_PAIR_METHOD,
                    method_version=BAND_PAIR_METHOD_VERSION,
                    revision_key=binding.revision_key,
                )
                while len(row) <= max(id_index, dep_index):
                    row.append(None)
                row[id_index] = relation_id
                row[dep_index] = {
                    "capture_id": binding.capture_id,
                    "revision": binding.revision_fingerprint,
                    "asset_ids": asset_ids,
                    "method_family": BAND_PAIR_METHOD.rsplit(".", 1)[0],
                }
        enriched[name] = {
            **dict(table),
            "columns": columns,
            "rows": rows,
            "relation_kind": (
                "band_dominance_pair" if name == "band_dominance_pairs" else None
            ),
            "window": binding.window.to_dict(),
            "stage": binding.stage,
            "node": binding.node,
            "channel_view": binding.channel_view,
            "method": BAND_PAIR_METHOD if name == "band_dominance_pairs" else None,
            "method_version": (
                BAND_PAIR_METHOD_VERSION if name == "band_dominance_pairs" else None
            ),
            "revision_key": binding.revision_key,
            "capture_id": binding.capture_id,
            "project_id": binding.project_id,
            "canonical": True,
        }
    return enriched


def build_relation_set(
    binding: RelationBinding,
    batch: Any,
    *,
    source_assets: Mapping[str, str],
    known_ids: Sequence[str] | None = None,
    master_id: str | None = None,
    truncated: Mapping[str, Any] | None = None,
) -> RelationSet:
    """Bind one analysis batch to one capture/revision/window.

    ``source_assets`` maps every endpoint id to the asset that represents it in
    the capture.  ``known_ids``, when supplied, rejects relations pointing at an
    object that is not in the same capture.
    """

    if not isinstance(binding, RelationBinding):
        raise ContractValidationError(
            "binding must be a RelationBinding", field="binding"
        )
    known = set(known_ids) if known_ids is not None else None
    records: dict[str, RelationRecord] = {}
    for candidate in batch.candidates:
        record = _record_from_candidate(
            candidate,
            binding,
            source_assets=source_assets,
            master_id=master_id,
        )
        if known is not None:
            for endpoint in (record.from_id, record.to_id):
                if record.to_type == "reference" and endpoint == record.to_id:
                    continue
                if endpoint not in known:
                    raise ContractValidationError(
                        f"relation endpoint {endpoint!r} is not in this capture",
                        field="endpoint",
                    )
        previous = records.get(record.relation_id)
        if previous is not None:
            if previous.to_dict() != record.to_dict():
                raise ContractValidationError(
                    "two different relations produced the same id",
                    field="relation_id",
                )
            continue
        records[record.relation_id] = record

    coverage: dict[str, Any] = {}
    kinds = set(batch.expected) | set(batch.evaluated) | set(batch.observed) | {
        key.split(":", 1)[0] for key in batch.unavailable
    }
    for kind in sorted(kinds):
        expected = int(batch.expected.get(kind, 0))
        evaluated = int(batch.evaluated.get(kind, 0))
        observed = int(batch.observed.get(kind, 0))
        reasons = [
            reason for key, reason in batch.unavailable.items()
            if key == kind or key.startswith(kind + ":")
        ]
        if expected == 0 and not reasons:
            reasons = ["producer_returned_no_expected_units"]
        if evaluated == 0 and expected > 0 and not reasons:
            reasons = ["no_pair_evaluated"]
        status = "complete"
        if reasons or evaluated < expected:
            status = "unavailable" if evaluated == 0 else "partial"
        coverage[kind] = {
            "expected": expected,
            "evaluated": evaluated,
            "observed": observed,
            "fraction": (evaluated / expected) if expected else None,
            "relations_emitted": observed,
            "status": status,
            "reasons": reasons,
        }

    merged_truncated = dict(batch.truncated)
    if truncated:
        merged_truncated.update(truncated)

    return RelationSet(
        binding=binding,
        relations=tuple(records.values()),
        coverage=coverage,
        unavailable=dict(batch.unavailable),
        truncated=merged_truncated,
        evaluations=_enrich_evaluations(
            binding, batch.tables, source_assets=source_assets
        ),
    )


__all__ = [
    "MANDATORY_RELATION_KINDS",
    "RELATION_KINDS",
    "RELATION_SCHEMA_VERSION",
    "RelationBinding",
    "RelationRecord",
    "RelationSet",
    "RelationWindow",
    "build_relation_set",
    "derive_relation_id",
]
