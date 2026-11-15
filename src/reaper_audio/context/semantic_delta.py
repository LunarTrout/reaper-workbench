"""Semantic capture-to-capture differences with stable object identity.

A capture-to-capture delta answers "which facts differ between these two
captures".  It must not answer it with positional array paths: inserting one
track would otherwise rename every later track, and moving an FX slot would
look like a parameter change on a different object.

This module walks two panorama documents, matches known row collections by
their stable identifiers, and emits field-level operations addressed as
``/<section>/<stable object id>/<field...>``.  Every operation carries the
before and after value exactly, plus the measurement context it belongs to
(unit, method/version, stage, window) where that context exists.

Comparability is computed only from declared matching criteria.  A missing
method version or an unknown grid is reported as ``unknown``; a stated method
or unit mismatch is reported as ``incomparable``.  The delta never emits a
better/worse verdict and never hides a factual field change because its
comparability is unknown.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

SEMANTIC_DELTA_SCHEMA = "reaper-workbench.capture-delta/2"

#: Row collections whose identity is a declared field, not their array index.
_KEYED_COLLECTIONS: dict[str, tuple[Any, ...]] = {
    "tracks": ("stable_id", "guid"),
    "items": ("item_id", "guid"),
    "takes": ("take_id", "guid"),
    "midi": ("midi_id", "take_id", "guid"),
    "fx": ("fx_id", "guid"),
    "routes": (("source_id", "destination_id", "send_index"), "route_id"),
    "analysis": ("source_id", "analysis_id"),
    "audio": ("audio_id", "source_id"),
    "per_track": ("stable_id", "track_id", "guid"),
    "crosslinks": (("relation", "from", "to"), ("kind", "from_id", "to_id"),
                   "id", "ref"),
}

_NESTED_KEYS: dict[str, tuple[Any, ...]] = {
    "details.entries": ("ref",),
    "relations.relations": (("kind", "from_id", "to_id"), "relation_id", "id"),
    "relations.edges": (("kind", "from_id", "to_id"), "id"),
    "relations.evaluated_pairs": (("from_id", "to_id"), "id"),
    "analysis_temporal.sources": ("source_id",),
    "analysis_temporal.buckets": ("start_s",),
    "envelopes.tracks": ("track_id", "guid"),
    "envelopes.fx": ("fx_id", "guid"),
    "envelopes.items": ("item_id", "guid"),
    "automation.tracks": ("track_id", "guid"),
    "automation.fx": ("fx_id", "guid"),
    "automation.items": ("item_id", "guid"),
    "time_map.tempo_points": ("time_s",),
    "tempo": ("time_s",),
    "time_signatures": ("time_s",),
    "markers": ("time_s", "name"),
    "regions": (("start_s", "end_s"), "name"),
}

_UNIT_SUFFIXES = (
    ("_s", "s"),
    ("_hz", "Hz"),
    ("_dbfs", "dBFS"),
    ("_db", "dB"),
    ("_bpm", "BPM"),
    ("_lufs", "LUFS"),
    ("_percent", "%"),
    ("frames", "samples"),
    ("sample_rate", "Hz"),
    ("count", "count"),
)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []


def _ptr(token: Any) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def _join(path: Sequence[Any]) -> str:
    return "/" + "/".join(_ptr(step) for step in path)


def _candidate(key: Any, row: Mapping[str, Any]) -> str | None:
    if isinstance(key, tuple):
        values = []
        for name in key:
            if row.get(name) is None:
                return None
            values.append(str(row.get(name)))
        return "|".join(values)
    if key in row and row.get(key) is not None:
        return str(row.get(key))
    return None


def _row_key(row: Any, keys: Sequence[Any]) -> str | None:
    if not isinstance(row, Mapping):
        return None
    for key in keys:
        found = _candidate(key, row)
        if found:
            return found
    return None


def _keyed(rows: Sequence[Any], keys: Sequence[Any] | None) -> dict[str, Mapping[str, Any]] | None:
    if not keys:
        return None
    found: dict[str, Mapping[str, Any]] = {}
    seen: dict[str, int] = {}
    for row in rows:
        key = _row_key(row, keys)
        if not key:
            return None
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            # Parallel relations can share the same semantic tuple.  Keep their
            # occurrence number so an insertion elsewhere does not rename them.
            key = f"{key}#{seen[key]}"
        found[key] = row
    return found


def _collection_keys(path: Sequence[Any], rows: Sequence[Any]) -> Sequence[Any] | None:
    dotted = ".".join(str(step) for step in path if not isinstance(step, int))
    if dotted in _NESTED_KEYS:
        return _NESTED_KEYS[dotted]
    if path and str(path[-1]) in _KEYED_COLLECTIONS:
        return _KEYED_COLLECTIONS[str(path[-1])]
    if rows and isinstance(rows[0], Mapping):
        for name in ("stable_id", "source_id", "item_id", "take_id", "fx_id",
                     "route_id", "audio_id", "analysis_id", "ref", "guid", "id"):
            if all(isinstance(row, Mapping) and row.get(name) is not None for row in rows):
                return (name,)
    return None


def _table_rows(value: Mapping[str, Any]) -> tuple[list[str], list[list[Any]]] | None:
    columns = value.get("columns")
    rows = value.get("rows")
    if (not isinstance(columns, list) or not isinstance(rows, list) or
            not all(isinstance(row, list) for row in rows)):
        return None
    return [str(name) for name in columns], rows


def _table_key(columns: Sequence[str], row: Sequence[Any]) -> str | None:
    names = {name: index for index, name in enumerate(columns)}
    for candidate in (("source", "metric"), ("from_id", "to_id"), ("source",),
                      ("ref",), ("id",), ("relation_id",)):
        if all(name in names for name in candidate):
            return "|".join(str(row[names[name]]) for name in candidate)
    return None


def _object_kind(path: Sequence[Any]) -> str | None:
    for step in path:
        if str(step) in _KEYED_COLLECTIONS:
            return {
                "tracks": "track", "items": "item", "takes": "take", "midi": "midi",
                "fx": "fx", "routes": "route", "analysis": "analysis",
                "audio": "audio", "per_track": "track", "crosslinks": "crosslink",
            }.get(str(step))
    if "details" in [str(step) for step in path]:
        return "detail"
    if "relations" in [str(step) for step in path]:
        return "relation"
    return None


def _unit_for(path: Sequence[Any], field: str, base: Mapping[str, Any],
              target: Mapping[str, Any]) -> str | None:
    units = _mapping(base.get("analysis_metric_units")) or _mapping(target.get("analysis_metric_units"))
    if field in units:
        return str(units[field]) if units.get(field) is not None else None
    contract = _mapping(base.get("analysis_contract")) or _mapping(target.get("analysis_contract"))
    metric_units = _mapping(_mapping(contract.get("metrics")).get("units"))
    if field in metric_units:
        return str(metric_units[field]) if metric_units.get(field) is not None else None
    lowered = field.lower()
    for suffix, unit in _UNIT_SUFFIXES:
        if lowered.endswith(suffix):
            return unit
    return None


def _measurement_context(base_document: Mapping[str, Any],
                         target_document: Mapping[str, Any]) -> dict[str, Any]:
    base_contract = _mapping(base_document.get("analysis_contract"))
    target_contract = _mapping(target_document.get("analysis_contract"))
    base_method = _mapping(base_contract.get("method"))
    target_method = _mapping(target_contract.get("method"))
    base_units = dict(_mapping(base_document.get("analysis_metric_units")))
    target_units = dict(_mapping(target_document.get("analysis_metric_units")))
    if not base_units:
        base_units = dict(_mapping(_mapping(base_contract.get("metrics")).get("units")))
    if not target_units:
        target_units = dict(_mapping(_mapping(target_contract.get("metrics")).get("units")))
    unit_differences = {
        name: {"base": base_units.get(name), "target": target_units.get(name)}
        for name in sorted(set(base_units) | set(target_units))
        if base_units.get(name) != target_units.get(name)
    }
    base_rows = {str(row.get("source_id")): row for row in _list(base_document.get("analysis"))
                 if isinstance(row, Mapping) and row.get("source_id")}
    target_rows = {str(row.get("source_id")): row for row in _list(target_document.get("analysis"))
                   if isinstance(row, Mapping) and row.get("source_id")}
    shared_sources = sorted(set(base_rows) & set(target_rows))
    stage_mismatches = []
    channel_mismatches = []
    window_mismatches = []
    stage_evidence = channel_evidence = window_evidence = False
    for source in shared_sources:
        left, right = base_rows[source], target_rows[source]
        if left.get("stage") is not None or right.get("stage") is not None:
            stage_evidence = True
        if left.get("channel_view") is not None or right.get("channel_view") is not None:
            channel_evidence = True
        if left.get("measured_window") is not None or right.get("measured_window") is not None:
            window_evidence = True
        if left.get("stage") != right.get("stage"):
            stage_mismatches.append({"source": source, "base": left.get("stage"),
                                     "target": right.get("stage")})
        if left.get("channel_view") != right.get("channel_view"):
            channel_mismatches.append({"source": source, "base": left.get("channel_view"),
                                       "target": right.get("channel_view")})
        if _mapping(left.get("measured_window")) != _mapping(right.get("measured_window")):
            window_mismatches.append({
                "source": source,
                "base": _mapping(left.get("measured_window")),
                "target": _mapping(right.get("measured_window")),
            })
    base_temporal = _mapping(base_document.get("analysis_temporal"))
    target_temporal = _mapping(target_document.get("analysis_temporal"))
    grid = {
        "base": {key: base_temporal.get(key) for key in
                 ("bucket_width_s", "aggregation", "anchor", "representation",
                  "time_basis", "units")},
        "target": {key: target_temporal.get(key) for key in
                   ("bucket_width_s", "aggregation", "anchor", "representation",
                    "time_basis", "units")},
    }
    grid_evidence = any(value is not None for side in grid.values() for value in side.values())
    method_name = {"base": base_method.get("analysis"), "target": target_method.get("analysis")}
    method_version = {"base": base_method.get("version"), "target": target_method.get("version")}
    criteria: dict[str, Any] = {
        "identity": {"state": "match", "base": _mapping(base_document.get("identity")),
                     "target": _mapping(target_document.get("identity"))},
        "method": {"state": ("match" if method_name["base"] == method_name["target"]
                             and method_name["base"] else "unknown"),
                   **method_name},
        "method_version": {
            "state": ("match" if method_version["base"] is not None
                      and method_version["base"] == method_version["target"]
                      else "unknown"),
            **method_version,
            "reason": (None if method_version["base"] is not None
                       and method_version["base"] == method_version["target"]
                       else "method_version_unstated_or_different"),
        },
        "units": {"state": ("unknown" if not base_units and not target_units else
                            ("mismatch" if unit_differences else "match")),
                  "differences": unit_differences,
                  "reason": ("unit_evidence_missing" if not base_units and not target_units
                             else None)},
        "stage": {"state": ("unknown" if not stage_evidence else
                            ("mismatch" if stage_mismatches else "match")),
                  "differences": stage_mismatches,
                  "reason": ("stage_evidence_missing" if not stage_evidence else None)},
        "channel_view": {"state": ("unknown" if not channel_evidence else
                                   ("mismatch" if channel_mismatches else "match")),
                         "differences": channel_mismatches,
                         "reason": ("channel_evidence_missing" if not channel_evidence
                                    else None)},
        "measured_window": {"state": ("unknown" if not window_evidence else
                                      ("mismatch" if window_mismatches else "match")),
                            "differences": window_mismatches,
                            "reason": ("window_evidence_missing" if not window_evidence
                                       else None)},
        "temporal_grid": {"state": ("unknown" if not grid_evidence else
                                    ("match" if grid["base"] == grid["target"] else "mismatch")),
                          **grid,
                          "reason": ("grid_evidence_missing" if not grid_evidence else None)},
    }
    unknown = [name for name, row in criteria.items()
               if row.get("state") in {"unknown", "mismatch"}]
    mismatched = [name for name, row in criteria.items() if row.get("state") == "mismatch"]
    if mismatched:
        state = "incomparable"
    elif unknown:
        state = "unknown"
    else:
        state = "comparable"
    return {
        "state": state,
        "criteria": criteria,
        "unknown_reasons": unknown,
        "mismatched_criteria": mismatched,
        "method": dict(base_method),
        "note": ("comparability is computed only from these declared criteria. A "
                 "mismatch means a point comparison is not justified; the factual "
                 "before/after values are still reported, and no better/worse "
                 "verdict is produced."),
    }


def _shared_sources(base: Mapping[str, Any],
                    target: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Exact per-source measurement context, dictionary-compressed.

    Repeated format/provenance/window objects are stored once and referenced by
    index.  Every original value is still present in ``dictionaries``; nothing
    is rounded or dropped.
    """
    raw: dict[str, dict[str, Any]] = {}
    for document in (base, target):
        for row in _list(document.get("analysis")):
            if not isinstance(row, Mapping):
                continue
            source = str(row.get("source_id") or row.get("source_guid") or "")
            if not source:
                continue
            entry = raw.setdefault(source, {})
            entry.update({
                "analysis_id": row.get("analysis_id"),
                "stage": row.get("stage"),
                "channel_view": row.get("channel_view"),
                "measured_window": _delta_view(row.get("measured_window")),
                "available": row.get("available"),
            })
        for row in _list(document.get("audio")):
            if not isinstance(row, Mapping):
                continue
            source = str(row.get("source_id") or "")
            if not source:
                continue
            entry = raw.setdefault(source, {})
            entry.update({
                "audio_id": row.get("audio_id"),
                "audio_stage": _mapping(row.get("render_provenance")).get("stage"),
                "render_provenance": _delta_view(row.get("render_provenance")),
                "format": _delta_view(row.get("format")),
            })
    dictionaries: dict[str, list[Any]] = {
        "format": [], "render_provenance": [], "measured_window": [],
    }
    indexes: dict[str, dict[str, int]] = {name: {} for name in dictionaries}
    rows: dict[str, Any] = {}
    for source in sorted(raw):
        entry = raw[source]
        row: dict[str, Any] = {}
        for field in ("analysis_id", "audio_id", "stage", "channel_view",
                      "available", "audio_stage"):
            if entry.get(field) is not None:
                row[field] = entry[field]
        for field in dictionaries:
            value = entry.get(field)
            if value is None:
                continue
            digest = _sha(value)
            if digest not in indexes[field]:
                indexes[field][digest] = len(dictionaries[field])
                dictionaries[field].append(value)
            row[f"{field}_ref"] = indexes[field][digest]
        rows[source] = row
    return rows, dictionaries


class _Context:
    def __init__(self, base: Mapping[str, Any], target: Mapping[str, Any]) -> None:
        self.base = base
        self.target = target
        self.method = _measurement_context(base, target)
        self.base_identity = _mapping(base.get("identity"))
        self.target_identity = _mapping(target.get("identity"))
        self.schema_match = base.get("schema_version") == target.get("schema_version")
        self.shared_sources, self.shared_dictionaries = _shared_sources(base, target)
        contract = _mapping(base.get("analysis_contract")) or _mapping(
            target.get("analysis_contract"))
        method = _mapping(contract.get("method"))
        self.method_info = {
            "analysis": method.get("analysis"),
            "version": method.get("version"),
            "decoder": method.get("decoder"),
        }

    def unit(self, path: Sequence[Any], field: str) -> str | None:
        return _unit_for(path, str(field), self.base, self.target)

    def measurement_state(self, path: Sequence[Any], field: str) -> dict[str, Any]:
        criteria = {name: dict(value) if isinstance(value, Mapping) else value
                    for name, value in self.method["criteria"].items()}
        units = _mapping(criteria.get("units"))
        differences = _mapping(units.get("differences"))
        field_unit = differences.get(str(field))
        criteria["metric_unit"] = {
            "state": "mismatch" if field_unit else "match",
            "metric": str(field),
            "base": (field_unit or {}).get("base"),
            "target": (field_unit or {}).get("target"),
        }
        mismatched = [name for name in
                      ("method", "method_version", "stage", "channel_view",
                       "measured_window", "temporal_grid")
                      if _mapping(criteria.get(name)).get("state") == "mismatch"]
        unknown = [name for name in
                   ("method", "method_version", "stage", "channel_view",
                    "measured_window", "temporal_grid")
                   if _mapping(criteria.get(name)).get("state") == "unknown"]
        if field_unit:
            state, reason = "incomparable", "unit_mismatch"
        elif mismatched:
            state, reason = "incomparable", "method_criteria_mismatch"
        elif unknown or _mapping(criteria.get("units")).get("state") == "unknown":
            state, reason = "unknown", "method_evidence_incomplete"
        else:
            state, reason = "comparable", None
        return {
            "state": state,
            "reason": reason,
            "unit": (field_unit or {}).get("base"),
        }

    def structural_state(self) -> dict[str, Any]:
        if not self.schema_match:
            return {"state": "unknown", "reason": "schema_version_differs",
                    "criteria": {"identity": "match",
                                 "schema": {"base": self.base.get("schema_version"),
                                            "target": self.target.get("schema_version")}}}
        return {"state": "comparable", "reason": None,
                "criteria": {"identity": "match",
                             "schema": {"base": self.base.get("schema_version"),
                                        "target": self.target.get("schema_version")}}}

    def enrich(self, op: dict[str, Any], path: Sequence[Any], field: str) -> None:
        kind = _object_kind(path)
        op["object_kind"] = kind
        object_id = None
        for step in path:
            if isinstance(step, int):
                continue
            text = str(step)
            if text in _KEYED_COLLECTIONS:
                continue
            if object_id is None and ((":" in text) or text.startswith("{")
                                      or "|" in text):
                object_id = text
                break
        op["object_id"] = object_id
        op["field"] = str(field)
        if "/metrics/" in "/" + "/".join(str(step) for step in path):
            op["metric"] = str(field)
        if "analysis_temporal" in [str(step) for step in path] and "buckets" in [
                str(step) for step in path]:
            steps = [str(step) for step in path]
            index = steps.index("buckets")
            if index + 1 < len(steps):
                start = steps[index + 1]
                bucket = self._bucket(start)
                op["interval"] = {
                    "start_s": bucket.get("start_s") if bucket else start,
                    "end_s": bucket.get("end_s") if bucket else None,
                    "basis": "stored temporal summary bucket",
                }
        measurement_roots = {"analysis", "analysis_temporal", "audio", "relations",
                             "relations_summary"}
        domain = next((str(step) for step in path if str(step) in measurement_roots), None)
        if domain:
            op["domain"] = "measurement"
            op["unit"] = self.unit(path, str(field))
            op["method"] = dict(self.method_info)
            op["comparability"] = self.measurement_state(path, str(field))
            source = object_id if object_id in self.shared_sources else None
            op["source"] = source
            op["stage_ref"] = source
            op["window_ref"] = source
        else:
            op["domain"] = "structural"
            op["comparability"] = self.structural_state()

    def _bucket(self, start: Any) -> Mapping[str, Any] | None:
        for document in (self.base, self.target):
            temporal = _mapping(document.get("analysis_temporal"))
            for bucket in _list(temporal.get("buckets")):
                if isinstance(bucket, Mapping) and str(bucket.get("start_s")) == str(start):
                    return bucket
        return None

    def _stage_for(self, path: Sequence[Any]) -> Any:
        for step in path:
            text = str(step)
            if ":" in text or text.startswith("{"):
                for document in (self.base, self.target):
                    for row in _list(document.get("analysis")):
                        if isinstance(row, Mapping) and str(row.get("source_id")) == text:
                            return row.get("stage")
                    for row in _list(document.get("audio")):
                        if isinstance(row, Mapping) and str(row.get("source_id")) == text:
                            return _mapping(row.get("render_provenance")).get("stage")
        return None

    def _window_for(self, path: Sequence[Any]) -> Any:
        for step in path:
            text = str(step)
            if ":" in text or text.startswith("{"):
                for document in (self.base, self.target):
                    for row in _list(document.get("analysis")):
                        if isinstance(row, Mapping) and str(row.get("source_id")) == text:
                            return row.get("measured_window")
                temporal = _mapping(document.get("analysis_temporal"))
                if temporal:
                    return temporal.get("covered_interval")
        return None


_TRANSPORT_TOP_LEVEL = frozenset({
    # Proven only to be transport binding for the capture document.  The exact
    # values are still carried in the delta's own ``provenance`` block.
    "document_sha256",
    "capture_binding",
    "provenance",
})


def _delta_view(value: Any, path: tuple[str, ...] = ()) -> Any:
    """Copy a document without only the declared transport-binding fields.

    This is deliberately path-aware at the top level: a nested ``provenance``,
    ``dep_set``, ``asset_sha256`` or ``raw_parameters_pointer`` is a fact about
    a source/object and must stay in the delta.  Stable object ids and every
    measured value are preserved; no key is discarded merely because its name
    looks volatile somewhere else.
    """
    if isinstance(value, Mapping):
        if not path and all(str(key) in _TRANSPORT_TOP_LEVEL for key in value):
            return {str(key): _delta_view(item, path + (str(key),))
                    for key, item in value.items()
                    if str(key) not in _TRANSPORT_TOP_LEVEL}
        return {str(key): _delta_view(item, path + (str(key),))
                for key, item in value.items()
                if not (not path and str(key) in _TRANSPORT_TOP_LEVEL)}
    if isinstance(value, (list, tuple)):
        return [_delta_view(item, path) for item in value]
    return value


def semantic_ops(base_document: Mapping[str, Any],
                 target_document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return exact, stable-id-addressed differences between two documents."""
    context = _Context(base_document, target_document)
    operations: list[dict[str, Any]] = []

    def add(op: str, path: Sequence[Any], *, before: Any = None, after: Any = None,
            include_before: bool = True, include_after: bool = True) -> None:
        entry: dict[str, Any] = {"op": op, "path": _join(path)}
        if include_before:
            entry["before"] = before
        if include_after:
            entry["after"] = after
        context.enrich(entry, path, str(path[-1]) if path else "")
        operations.append(entry)

    def diff(before: Any, after: Any, path: Sequence[Any], keys: Sequence[Any] | None) -> None:
        if before == after:
            return
        if isinstance(before, Mapping) and isinstance(after, Mapping):
            left_table = _table_rows(before)
            right_table = _table_rows(after)
            if left_table and right_table and left_table[0] == right_table[0]:
                left_columns, left_values = left_table
                right_columns, right_values = right_table
                left_map: dict[str, dict[str, Any]] = {}
                right_map: dict[str, dict[str, Any]] = {}
                for row in left_values:
                    key = _table_key(left_columns, row)
                    if key is None or key in left_map:
                        left_map = {}
                        break
                    left_map[key] = dict(zip(left_columns, row))
                for row in right_values:
                    key = _table_key(right_columns, row)
                    if key is None or key in right_map:
                        right_map = {}
                        break
                    right_map[key] = dict(zip(right_columns, row))
                if left_map and right_map:
                    for key in sorted(set(left_map) | set(right_map)):
                        child = tuple(path) + ("rows", key)
                        if key not in left_map:
                            add("add", child, after=right_map[key], include_before=False)
                        elif key not in right_map:
                            add("remove", child, before=left_map[key], include_after=False)
                        else:
                            diff(left_map[key], right_map[key], child, None)
                    for name in sorted(set(before) | set(after)):
                        if name in {"columns", "rows"}:
                            continue
                        child = tuple(path) + (str(name),)
                        if name not in before:
                            add("add", child, after=after[name], include_before=False)
                        elif name not in after:
                            add("remove", child, before=before[name], include_after=False)
                        else:
                            diff(before[name], after[name], child, None)
                    return
            for name in sorted(set(before) | set(after)):
                child = tuple(path) + (str(name),)
                if name not in before:
                    add("add", child, after=after[name], include_before=False)
                elif name not in after:
                    add("remove", child, before=before[name], include_after=False)
                else:
                    diff(before[name], after[name], child,
                         _collection_keys(child, _list(after[name]))
                         if isinstance(after[name], (list, tuple)) else None)
            return
        if isinstance(before, (list, tuple)) and isinstance(after, (list, tuple)):
            left_rows = list(before)
            right_rows = list(after)
            keyed_left = _keyed(left_rows, keys)
            keyed_right = _keyed(right_rows, keys)
            if keyed_left is not None and keyed_right is not None:
                for key in sorted(set(keyed_left) | set(keyed_right)):
                    child = tuple(path) + (key,)
                    if key not in keyed_left:
                        add("add", child, after=keyed_right[key], include_before=False)
                    elif key not in keyed_right:
                        add("remove", child, before=keyed_left[key], include_after=False)
                    else:
                        diff(keyed_left[key], keyed_right[key], child, None)
                return
            for index in range(max(len(left_rows), len(right_rows))):
                child = tuple(path) + (index,)
                if index >= len(left_rows):
                    add("add", child, after=right_rows[index], include_before=False)
                elif index >= len(right_rows):
                    add("remove", child, before=left_rows[index], include_after=False)
                else:
                    diff(left_rows[index], right_rows[index], child, None)
            return
        add("replace", path, before=before, after=after)

    diff(_delta_view(base_document), _delta_view(target_document), (), None)
    return operations


def _section_domains(base_document: Mapping[str, Any],
                     target_document: Mapping[str, Any],
                     operations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_root: dict[str, int] = {}
    for op in operations:
        parts = [part for part in str(op.get("path") or "").split("/") if part]
        if parts:
            by_root[parts[0]] = by_root.get(parts[0], 0) + 1
    domains: dict[str, Any] = {}
    for name in sorted(set(base_document) | set(target_document)):
        if str(name) in _TRANSPORT_TOP_LEVEL:
            continue
        before = _delta_view(base_document.get(name), (str(name),))
        after = _delta_view(target_document.get(name), (str(name),))
        count_before = len(before) if isinstance(before, (list, Mapping)) else (before is not None)
        count_after = len(after) if isinstance(after, (list, Mapping)) else (after is not None)
        unchanged = before == after
        domains[str(name)] = {
            "status": ("unchanged" if unchanged else
                       "changed" if by_root.get(str(name)) else "residual"),
            "ops": by_root.get(str(name), 0),
            "before_count": count_before,
            "after_count": count_after,
            "before_sha256": _sha(before) if before is not None else None,
            "after_sha256": _sha(after) if after is not None else None,
        }
    return domains


def build_semantic_delta(base_document: Mapping[str, Any],
                         target_document: Mapping[str, Any], *,
                         base_capture_id: str | None = None,
                         target_capture_id: str | None = None) -> dict[str, Any]:
    """Build the structured capture delta payload (without lifecycle scoring)."""
    operations = semantic_ops(base_document, target_document)
    all_operations = list(operations)
    context = _Context(base_document, target_document)
    domains = _section_domains(base_document, target_document, all_operations)
    residual = [name for name, row in domains.items() if row["status"] == "residual"]
    measurement_ops = [op for op in all_operations if op.get("domain") == "measurement"]
    structural_ops = [op for op in all_operations if op.get("domain") == "structural"]
    comparable = sum(1 for op in measurement_ops
                     if _mapping(op.get("comparability")).get("state") == "comparable")
    unknown = sum(1 for op in measurement_ops
                  if _mapping(op.get("comparability")).get("state") == "unknown")
    incomparable = sum(1 for op in measurement_ops
                       if _mapping(op.get("comparability")).get("state") == "incomparable")
    # A project-wide metric revision produces one op per source × metric.  Above
    # a small threshold, the same facts are delivered as one declared table
    # (source index, metric, unit, exact before/after, comparability) instead of
    # thousands of repeated path/method objects.  The table is exact and
    # reconstructs every omitted op through ``path_pattern``.
    metric_table: dict[str, Any] | None = None
    metric_ops = [op for op in measurement_ops
                  if op.get("op") == "replace" and "/metrics/" in str(op.get("path"))
                  and op.get("object_kind") == "analysis"]
    if len(metric_ops) > 32:
        source_ids = sorted(context.shared_sources)
        source_index = {source: index for index, source in enumerate(source_ids)}
        metric_names = sorted({str(op.get("field")) for op in metric_ops})
        metric_index = {name: index for index, name in enumerate(metric_names)}
        unit_values: list[Any] = []
        unit_index: dict[Any, int] = {}
        state_values: list[Any] = []
        state_index: dict[Any, int] = {}
        reason_values: list[Any] = []
        reason_index: dict[Any, int] = {}
        rows = []
        for op in metric_ops:
            comp = _mapping(op.get("comparability"))
            unit = op.get("unit")
            if unit not in unit_index:
                unit_index[unit] = len(unit_values)
                unit_values.append(unit)
            state = comp.get("state")
            if state not in state_index:
                state_index[state] = len(state_values)
                state_values.append(state)
            reason = comp.get("reason")
            if reason not in reason_index:
                reason_index[reason] = len(reason_values)
                reason_values.append(reason)
            rows.append([
                source_index.get(str(op.get("source")), -1),
                metric_index[str(op.get("field"))],
                unit_index[unit],
                op.get("before"),
                op.get("after"),
                state_index[state],
                reason_index[reason],
            ])
        metric_paths = {str(op.get("path")) for op in metric_ops}
        operations = [op for op in operations if str(op.get("path")) not in metric_paths]
        metric_table = {
            "columns": ["source_index", "metric_index", "unit_index", "before",
                        "after", "comparability_index", "reason_index"],
            "rows": rows,
            "dictionaries": {
                "source_ids": source_ids,
                "metrics": metric_names,
                "units": unit_values,
                "comparability": state_values,
                "reasons": reason_values,
            },
            "path_pattern": "/analysis/{source_id}/metrics/{metric}",
            "object_kind": "analysis",
            "count": len(rows),
            "context_ref": ("shared.sources[source_id] + shared.dictionaries[*_ref] "
                            "give stage/channel/window/format/provenance exactly; "
                            "shared.method gives method/version; comparability gives "
                            "the matching criteria"),
            "note": ("each row is exactly one replace op at path_pattern; before/after "
                     "are the stored values, not a summary. Column dictionaries are "
                     "exact; the full comparability criteria are in this delta's "
                     "comparability block."),
        }
    journal_entries = []
    for index, op in enumerate(operations):
        journal_entries.append({
            "operation_index": index,
            "source": "capture_delta",
            "domain": op.get("domain"),
            "object_kind": op.get("object_kind"),
            "object_id": op.get("object_id"),
            "path": op.get("path"),
            "interval": op.get("interval"),
            "before_ref": f"ops[{index}].before",
            "after_ref": f"ops[{index}].after",
            "comparability_ref": f"ops[{index}].comparability",
        })
    journal = {
        "schema_version": SEMANTIC_DELTA_SCHEMA.replace("/2", "-journal/1"),
        "kind": "capture_delta",
        "project_id": (_mapping(base_document.get("identity")).get("project_id")
                       or _mapping(target_document.get("identity")).get("project_id")),
        "base": {
            "capture_id": base_capture_id,
            "document_sha256": base_document.get("document_sha256"),
            "revision": dict(_mapping(_mapping(base_document.get("capture_binding")).get(
                "revision_after")) or _mapping(_mapping(base_document.get("capture_binding")).get(
                    "revision_before"))),
        },
        "target": {
            "capture_id": target_capture_id,
            "document_sha256": target_document.get("document_sha256"),
            "revision": dict(_mapping(_mapping(target_document.get("capture_binding")).get(
                "revision_after")) or _mapping(_mapping(target_document.get("capture_binding")).get(
                    "revision_before"))),
        },
        "structural": {"state": context.structural_state().get("state"),
                       "ops": len(structural_ops)},
        "captured_audio": {
            "state": "unknown",
            "reason": ("capture-to-capture audio freshness is computed by the "
                       "lifecycle render-manifest comparison; this journal never "
                       "declares old audio current"),
            "target_capture_id": target_capture_id,
        },
        "metric_table": (None if metric_table is None else {
            "rows": len(metric_table["rows"]),
            "path_pattern": metric_table["path_pattern"],
            "source_ids": metric_table["dictionaries"]["source_ids"],
            "capture_id": target_capture_id,
            "comparability_ref": "comparability.measurements",
        }),
        "entries": journal_entries,
        "invalidations_ref": "invalidations",
        "note": ("one journal contract: capture/revision/source/object/interval ids "
                 "bind every operation, its measured consequence and its invalidation; "
                 "structural state and captured-audio freshness are distinct"),
    }
    return {
        "schema_version": SEMANTIC_DELTA_SCHEMA,
        "kind": "capture_delta",
        "capture_ids": {"base": base_capture_id, "target": target_capture_id},
        "journal": journal,
        "identity": {"base": dict(_mapping(base_document.get("identity"))),
                     "target": dict(_mapping(target_document.get("identity")))},
        "provenance": {
            "base": {"capture_id": base_capture_id,
                     "document_hash": _mapping(base_document.get("capture_binding")).get(
                         "record_integrity_sha256"),
                     "document_sha256": base_document.get("document_sha256"),
                     "capture_binding": dict(_mapping(base_document.get("capture_binding"))),
                     "revision": dict(_mapping(_mapping(base_document.get("capture_binding")).get(
                         "revision_after")) or _mapping(
                             _mapping(base_document.get("capture_binding")).get(
                                 "revision_before"))),
                     "captured_at": _mapping(base_document.get("provenance")).get(
                         "captured_at")},
            "target": {"capture_id": target_capture_id,
                       "document_hash": _mapping(target_document.get("capture_binding")).get(
                           "record_integrity_sha256"),
                       "document_sha256": target_document.get("document_sha256"),
                       "capture_binding": dict(_mapping(target_document.get("capture_binding"))),
                       "revision": dict(_mapping(_mapping(target_document.get(
                           "capture_binding")).get("revision_after")) or _mapping(
                               _mapping(target_document.get("capture_binding")).get(
                                   "revision_before"))),
                       "captured_at": _mapping(target_document.get("provenance")).get(
                           "captured_at")},
            "note": ("exact capture binding, revision and hashes for both sides; "
                     "per-source render provenance and measurement windows are in "
                     "shared.sources"),
        },
        "shared": {
            "sources": context.shared_sources,
            "dictionaries": context.shared_dictionaries,
            "method": context.method_info,
            "units": dict(_mapping(base_document.get("analysis_metric_units"))
                          or _mapping(_mapping(_mapping(base_document.get(
                              "analysis_contract")).get("metrics")).get("units"))),
        },
        "ops": operations,
        "metric_table": metric_table,
        "object_summary": {
            kind: {
                "added": sorted({str(op.get("object_id")) for op in all_operations
                                 if op.get("op") == "add" and op.get("object_kind") == kind}),
                "removed": sorted({str(op.get("object_id")) for op in all_operations
                                   if op.get("op") == "remove" and op.get("object_kind") == kind}),
                "changed": sorted({str(op.get("object_id")) for op in all_operations
                                   if op.get("op") == "replace" and op.get("object_kind") == kind
                                   and op.get("object_id")}),
            }
            for kind in ("track", "item", "take", "midi", "fx", "route",
                         "analysis", "audio", "relation", "detail")
        },
        "measurements": {
            "ops": len(measurement_ops),
            "comparable": comparable,
            "unknown": unknown,
            "incomparable": incomparable,
            "metric_table_rows": len(metric_table["rows"]) if metric_table else 0,
        },
        "structural": {"ops": len(structural_ops)},
        "domains": domains,
        "coverage": {
            "sections_compared": sorted(domains),
            "residual_sections": residual,
            "complete": not residual,
            "metric_table": (None if metric_table is None else
                             {"rows": len(metric_table["rows"]),
                              "reconstruct": metric_table["path_pattern"]}),
            "rule": ("every changed top-level section has either field-level ops or an "
                     "explicit residual with before/after hashes; a metric table row is "
                     "one exact op and is never a summary"),
        },
        "comparability": {
            "identity": dict(context.method["criteria"].get("identity") or {}),
            "structural": context.structural_state(),
            "measurements": context.method,
            "note": ("structural changes and captured audio/analysis changes are "
                     "separate. A stale audio evidence statement is not used as evidence "
                     "about the current project structure. No better/worse verdict."),
        },
    }


__all__ = [
    "SEMANTIC_DELTA_SCHEMA",
    "build_semantic_delta",
    "semantic_ops",
]
