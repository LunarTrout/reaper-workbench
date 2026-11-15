"""Lossless summary projection of one ``reaper_analyze`` result.

The durable report and the persisted analysis record keep every measured fact in
its original, verbose per-source form: each source repeats the same capture
facts, the same render-dependency manifest and the same metric descriptors.  A
34-source capture therefore produces ~350 KB of metadata for a one-second
measurement (see HB-059), which no client can hold in one turn.

The model-facing summary is a *view* of that record, not a replacement:

* every source row, every metric value and every source-specific fact stays in
  the payload;
* facts that are byte-identical in **every** source are stored once in
  ``shared_capture`` and each row links to them by construction;
* metric definitions (``unit``/``confidence``/``note``) are stored once in
  ``metric_registry`` and each row keeps the named values in the same shape;
* a render-dependency component hash that appears in more than one source is
  stored once in ``component_hashes`` and each source's dependency block lists
  the component ids it contains.  The manifest's own ids are source-scoped
  (``master:*``, ``track:{guid}:*``), so the table is keyed by source and
  component without a second id namespace; identical ids with differing values
  are never merged and stay with the rows that disagree.

Nothing is sampled, rounded, paginated or dropped, and the merge rule is
published in the payload itself (``summary_projection``) so a reader never has
to guess how the parts combine.  :func:`reconstruct_sources` rebuilds the
pre-normalization rows exactly, which is what the focused tests assert.

Completeness is not a size guarantee.  The projection removes repeated bytes
only; a project with many sources can still produce a summary larger than a
client's inline budget, and that is reported rather than compressed, paged or
truncated.

The projection never runs on a persisted record: it is applied after the record
is written, to the model-facing result only.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from typing import Any

SUMMARY_PROJECTION_VERSION = "reaper-workbench.analyze-summary/1"

# Capture facts that describe the capture or project as a whole and may be
# stored once when every source reports the same value.
SHARED_CAPTURE_KEYS: tuple[str, ...] = (
    "stage",
    "capture_method",
    "sample_rate",
    "channels",
    "frames",
    "duration_s",
    "signal_window",
    "capture_revision",
    "render_key_complete",
)

# Identity and per-asset facts: never hoisted, even when two sources happen to
# agree, because they address one specific asset.
PER_SOURCE_CAPTURE_KEYS: tuple[str, ...] = (
    "asset_id",
    "source_guid",
    "name",
    "is_master",
    "sha256",
    "bytes",
    "render_key",
)

RENDER_DEPENDENCY_KEY = "render_dependencies"
COMPONENT_HASHES_KEY = "components_sha256"
COMPONENT_KEYS_KEY = "component_keys"
COMPONENT_TABLE_KEY = "component_hashes"
SHARED_RENDER_DEPENDENCY_KEYS: tuple[str, ...] = (
    "manifest_schema",
    "unknown",
    "unverified",
)

PROJECTION_KEY = "summary_projection"
METRIC_REGISTRY_KEY = "metric_registry"
SHARED_CAPTURE_KEY = "shared_capture"
SOURCE_ID_KEY = "source_id"
LOCAL_METRIC_DEFINITIONS_KEY = "metric_definitions_local"
VERBATIM_METRICS_KEY = "metric_objects_verbatim"

MERGE_RULE = (
    "capture = {**shared_capture, **row.capture}; render_dependencies = "
    "{**shared_capture.render_dependencies, **row.capture.render_dependencies}; "
    "components_sha256 = shared render_dependencies.components_sha256 | "
    "{k: component_hashes[k] for k in shared and row component_keys} | "
    "row.components_sha256; metric = {value: row.metrics[name], "
    "**metric_registry[name]} unless row.metric_definitions_local[name] or "
    "row.metric_objects_verbatim lists it"
)


class SummaryProjectionError(ValueError):
    """The projection cannot be proven lossless."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str,
                      separators=(",", ":"))


def _shared_value(items: Sequence[Mapping[str, Any]], key: str) -> tuple[bool, Any]:
    """True only when every item carries ``key`` with an identical value."""
    if any(key not in item for item in items):
        return False, None
    first = _canonical(items[0][key])
    if any(_canonical(item[key]) != first for item in items[1:]):
        return False, None
    return True, items[0][key]


def _split_metric(value: Any) -> tuple[bool, Any, dict[str, Any]]:
    """Split one metric object into (payload, descriptor) or (verbatim, {})."""
    if isinstance(value, Mapping) and "value" in value:
        return True, value["value"], {key: item for key, item in value.items()
                                      if key != "value"}
    return False, value, {}


def _meta(applied: bool, *, reason: str | None, source_count: int,
          metric_count: int, shared_keys: Sequence[str],
          component_hashes: int = 0) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "schema_version": SUMMARY_PROJECTION_VERSION,
        "applied": applied,
        "lossless": True,
        "complete": True,
        "source_count": source_count,
        "metric_count": metric_count,
        "shared_capture_key": SHARED_CAPTURE_KEY,
        "shared_capture_keys": list(shared_keys),
        "metric_registry_key": METRIC_REGISTRY_KEY,
        "component_hash_table_key": COMPONENT_TABLE_KEY,
        "component_hash_count": component_hashes,
        "source_id_key": SOURCE_ID_KEY,
        "merge": MERGE_RULE,
        "size_guarantee": (
            "exact and complete, not bounded: repeated bytes are stored once, but a "
            "project with many sources can still exceed a client's inline response budget"),
    }
    if applied:
        meta["note"] = (
            "Every source row, metric value, source-specific capture fact and "
            "source-specific dependency is present in this payload; only facts "
            "identical across all sources, dependency hashes repeated across sources, "
            "and metric definitions are stored once.")
    if reason:
        meta["reason"] = reason
    return meta


def project_analyze_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return the lossless summary view of one analyze result.

    The input is not mutated.  When the payload does not have the expected
    per-source shape the rows are returned unchanged and the projection says so
    (``applied: false``), because a shape change must never silently lose data.
    """
    rows = result.get("sources")
    projected = dict(result)
    if not isinstance(rows, list) or not rows:
        projected[PROJECTION_KEY] = _meta(
            False, reason="no source rows to project", source_count=(len(rows)
                                                                     if isinstance(rows, list)
                                                                     else 0),
            metric_count=0, shared_keys=())
        return projected
    if any(not isinstance(row, Mapping) or
           not isinstance(row.get("capture"), Mapping) or
           not isinstance(row.get("metrics"), Mapping) for row in rows):
        projected[PROJECTION_KEY] = _meta(
            False, reason="source rows do not have the expected capture/metrics shape",
            source_count=len(rows), metric_count=0, shared_keys=())
        return projected

    captures = [row["capture"] for row in rows]
    shared: dict[str, Any] = {}
    for key in SHARED_CAPTURE_KEYS:
        present, value = _shared_value(captures, key)
        if present:
            shared[key] = copy.deepcopy(value)

    shared_dependencies: dict[str, Any] = {}
    # A component hash may be stored once only when the same component id appears
    # in more than one source with one value everywhere.  Ids that appear once
    # stay where they are; ids that disagree stay with the rows that disagree.
    occurrences: dict[str, int] = {}
    component_values: dict[str, list[Any]] = {}
    for capture in captures:
        dependencies = capture.get(RENDER_DEPENDENCY_KEY)
        components = (dependencies.get(COMPONENT_HASHES_KEY)
                      if isinstance(dependencies, Mapping) else None)
        if not isinstance(components, Mapping):
            continue
        for name, value in components.items():
            occurrences[name] = occurrences.get(name, 0) + 1
            component_values.setdefault(name, []).append(value)
    component_candidates: dict[str, Any] = {}
    for name, values in component_values.items():
        if occurrences[name] < 2:
            continue
        if len({_canonical(item) for item in values}) != 1:
            continue
        component_candidates[name] = copy.deepcopy(values[0])

    candidate_shared_keys = [name for name in component_candidates if all(
        isinstance(capture.get(RENDER_DEPENDENCY_KEY), Mapping) and
        isinstance(capture[RENDER_DEPENDENCY_KEY].get(COMPONENT_HASHES_KEY), Mapping) and
        name in capture[RENDER_DEPENDENCY_KEY][COMPONENT_HASHES_KEY]
        for capture in captures)]

    def _component_layout_cost(with_table: bool) -> int:
        """Encoded size of the component facts under one layout."""
        cost = 0
        for capture in captures:
            dependencies = capture.get(RENDER_DEPENDENCY_KEY)
            components = (dependencies.get(COMPONENT_HASHES_KEY)
                          if isinstance(dependencies, Mapping) else None)
            if not isinstance(components, Mapping):
                continue
            if with_table:
                linked = [name for name in components
                          if name in component_candidates and name not in candidate_shared_keys]
                literal = {name: value for name, value in components.items()
                           if name not in component_candidates}
                cost += _size(linked) + _size(literal)
            else:
                # Without the table the shared hashes move to the shared block and
                # stay inline everywhere else.
                cost += _size({name: value for name, value in components.items()
                               if name not in candidate_shared_keys})
        if with_table:
            cost += _size(component_candidates) + _size(candidate_shared_keys)
        else:
            cost += _size({name: component_candidates[name]
                           for name in candidate_shared_keys})
        return cost

    # The table is a layout choice, not a fact: it is used only when it is
    # strictly smaller than keeping the maps inline, so the projection can never
    # make a payload bigger than the layout it replaces.
    component_hashes = (component_candidates
                        if component_candidates and
                        _component_layout_cost(True) < _component_layout_cost(False)
                        else {})
    shared_component_keys = [name for name in candidate_shared_keys
                             if name in component_hashes]

    dependencies = [capture.get(RENDER_DEPENDENCY_KEY) for capture in captures]
    if all(isinstance(item, Mapping) for item in dependencies):
        for key in SHARED_RENDER_DEPENDENCY_KEYS:
            present, value = _shared_value(dependencies, key)
            if present:
                shared_dependencies[key] = copy.deepcopy(value)
    if shared_component_keys:
        shared_dependencies[COMPONENT_KEYS_KEY] = list(shared_component_keys)
    elif candidate_shared_keys:
        shared_dependencies[COMPONENT_HASHES_KEY] = {
            name: copy.deepcopy(component_candidates[name])
            for name in candidate_shared_keys}
    if shared_dependencies:
        shared[RENDER_DEPENDENCY_KEY] = shared_dependencies

    registry: dict[str, dict[str, Any]] = {}
    local_definitions: list[dict[str, Any]] = [{} for _ in rows]
    verbatim: list[list[str]] = [[] for _ in rows]
    for index, row in enumerate(rows):
        for name, value in row["metrics"].items():
            split, _payload, descriptor = _split_metric(value)
            if not split:
                verbatim[index].append(name)
                continue
            if name not in registry:
                registry[name] = copy.deepcopy(descriptor)
            elif descriptor != registry[name]:
                local_definitions[index][name] = copy.deepcopy(descriptor)

    projected_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        capture = row["capture"]
        local_capture = {key: copy.deepcopy(value) for key, value in capture.items()
                         if key not in shared}
        dependencies_local = capture.get(RENDER_DEPENDENCY_KEY)
        if isinstance(dependencies_local, Mapping):
            kept: dict[str, Any] = {}
            linked: list[str] = []
            for key, value in dependencies_local.items():
                if key == COMPONENT_HASHES_KEY and isinstance(value, Mapping):
                    literal: dict[str, Any] = {}
                    for name, item in value.items():
                        if name in component_hashes:
                            if name not in shared_component_keys:
                                linked.append(name)
                            continue
                        if name in candidate_shared_keys:
                            continue
                        literal[name] = copy.deepcopy(item)
                    # An explicitly empty map is a fact too: keep it rather than
                    # letting reconstruction invent or drop the key.
                    if literal or not value:
                        kept[COMPONENT_HASHES_KEY] = literal
                    continue
                if key in shared_dependencies and _canonical(value) == _canonical(
                        shared_dependencies[key]):
                    continue
                kept[key] = copy.deepcopy(value)
            if linked:
                kept[COMPONENT_KEYS_KEY] = linked
            local_capture[RENDER_DEPENDENCY_KEY] = kept
        entry: dict[str, Any] = {
            SOURCE_ID_KEY: str(row.get("source_guid")
                               or capture.get("source_guid")
                               or f"source_{index}"),
        }
        for key, value in row.items():
            if key in ("capture", "metrics"):
                continue
            entry[key] = copy.deepcopy(value)
        entry["capture"] = local_capture
        entry["metrics"] = {name: _split_metric(value)[1]
                            for name, value in row["metrics"].items()}
        if local_definitions[index]:
            entry[LOCAL_METRIC_DEFINITIONS_KEY] = local_definitions[index]
        if verbatim[index]:
            entry[VERBATIM_METRICS_KEY] = list(verbatim[index])
        projected_rows.append(entry)

    if component_hashes:
        projected[COMPONENT_TABLE_KEY] = component_hashes
    projected[SHARED_CAPTURE_KEY] = shared
    projected[METRIC_REGISTRY_KEY] = registry
    projected["sources"] = projected_rows
    projected[PROJECTION_KEY] = _meta(
        True, reason=None, source_count=len(rows), metric_count=len(registry),
        shared_keys=sorted(shared), component_hashes=len(component_hashes))
    if _size(projected) >= _size(result):
        # Nothing repeated enough to pay for the shared blocks.  Returning the
        # plain rows keeps the payload smaller and loses no fact; the absence of
        # "summary_projection" marks that layout.
        return dict(result)
    return projected


def reconstruct_sources(projection: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Rebuild the pre-normalization source rows from a projected payload.

    This is the exactness invariant of the projection: for any payload built by
    :func:`project_analyze_summary` the result compares equal to the original
    rows.  Mapping key order follows the merge, values do not change.
    """
    meta = projection.get(PROJECTION_KEY)
    rows = projection.get("sources") or []
    if not isinstance(meta, Mapping) or meta.get("applied") is False:
        # No meta means the projection was not smaller than the plain rows, so
        # the payload already is the complete per-source form.
        return [copy.deepcopy(row) for row in rows]
    shared = projection.get(SHARED_CAPTURE_KEY) or {}
    registry = projection.get(METRIC_REGISTRY_KEY) or {}
    component_hashes = projection.get(COMPONENT_TABLE_KEY) or {}
    if not isinstance(shared, Mapping) or not isinstance(registry, Mapping):
        raise SummaryProjectionError("the projection has no shared block or registry")
    if not isinstance(component_hashes, Mapping):
        raise SummaryProjectionError("the component hash table is not a mapping")
    restored_rows: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise SummaryProjectionError("a projected source row is not a mapping")
        restored = {key: copy.deepcopy(value) for key, value in row.items()
                    if key not in (SOURCE_ID_KEY, LOCAL_METRIC_DEFINITIONS_KEY,
                                   VERBATIM_METRICS_KEY)}
        local_capture = row.get("capture") or {}
        if not isinstance(local_capture, Mapping):
            raise SummaryProjectionError("a projected capture block is not a mapping")
        capture = {key: copy.deepcopy(value) for key, value in shared.items()
                   if key != RENDER_DEPENDENCY_KEY}
        capture.update({key: copy.deepcopy(value) for key, value in local_capture.items()
                        if key != RENDER_DEPENDENCY_KEY})
        shared_dependencies = shared.get(RENDER_DEPENDENCY_KEY)
        if shared_dependencies is not None or RENDER_DEPENDENCY_KEY in local_capture:
            merged: dict[str, Any] = {}
            shared_keys: list[str] = []
            if isinstance(shared_dependencies, Mapping):
                merged.update({key: copy.deepcopy(value) for key, value
                               in shared_dependencies.items()
                               if key not in (COMPONENT_HASHES_KEY, COMPONENT_KEYS_KEY)})
                shared_keys = list(shared_dependencies.get(COMPONENT_KEYS_KEY) or [])
            local_dependencies = local_capture.get(RENDER_DEPENDENCY_KEY) or {}
            if not isinstance(local_dependencies, Mapping):
                raise SummaryProjectionError(
                    "projected render dependencies are not a mapping")
            merged.update({key: copy.deepcopy(value) for key, value
                           in local_dependencies.items()
                           if key not in (COMPONENT_HASHES_KEY, COMPONENT_KEYS_KEY)})
            components: dict[str, Any] = {}
            if isinstance(shared_dependencies, Mapping):
                shared_map = shared_dependencies.get(COMPONENT_HASHES_KEY)
                if isinstance(shared_map, Mapping):
                    components.update(copy.deepcopy(shared_map))
            for name in shared_keys + list(local_dependencies.get(COMPONENT_KEYS_KEY) or []):
                if name not in component_hashes:
                    raise SummaryProjectionError(
                        f"component {name!r} is not in the component hash table")
                components[name] = copy.deepcopy(component_hashes[name])
            components.update(copy.deepcopy(
                local_dependencies.get(COMPONENT_HASHES_KEY) or {}))
            if components or COMPONENT_HASHES_KEY in local_dependencies:
                merged[COMPONENT_HASHES_KEY] = components
            capture[RENDER_DEPENDENCY_KEY] = merged
        restored["capture"] = capture
        verbatim = set(row.get(VERBATIM_METRICS_KEY) or ())
        overrides = row.get(LOCAL_METRIC_DEFINITIONS_KEY) or {}
        metrics: dict[str, Any] = {}
        for name, value in (row.get("metrics") or {}).items():
            if name in verbatim:
                metrics[name] = copy.deepcopy(value)
                continue
            if name in overrides:
                descriptor = overrides[name]
            else:
                if name not in registry:
                    raise SummaryProjectionError(
                        f"metric {name!r} has no definition in the registry")
                descriptor = registry[name]
            if not isinstance(descriptor, Mapping):
                raise SummaryProjectionError(
                    f"metric {name!r} has a non-mapping definition")
            metric: dict[str, Any] = {"value": copy.deepcopy(value)}
            metric.update(copy.deepcopy(dict(descriptor)))
            metrics[name] = metric
        restored["metrics"] = metrics
        restored_rows.append(restored)
    return restored_rows


def assert_projection_is_lossless(original_rows: Sequence[Mapping[str, Any]],
                                  projection: Mapping[str, Any]) -> dict[str, Any]:
    """Prove exact reconstruction and that shared facts are stored exactly once.

    Returns a small inventory for reporting.  Raises
    :class:`SummaryProjectionError` when a fact would be lost, duplicated into a
    row, or reconstructed differently.
    """
    restored = reconstruct_sources(projection)
    original = [copy.deepcopy(dict(row)) for row in original_rows]
    if restored != original:
        raise SummaryProjectionError(
            "reconstructed source rows differ from the measured rows")
    meta = projection.get(PROJECTION_KEY)
    if not isinstance(meta, Mapping) or meta.get("applied") is False:
        # Plain layout: the rows are already the complete measured form, so there
        # is nothing shared to account for.
        return {
            "reconstructed_rows": len(restored),
            "shared_capture_keys": [],
            "shared_component_hashes": 0,
            "component_hash_table": 0,
            "metric_registry_metrics": 0,
            "duplicate_facts": 0,
        }
    shared = projection.get(SHARED_CAPTURE_KEY) or {}
    shared_dependencies = (shared.get(RENDER_DEPENDENCY_KEY)
                           if isinstance(shared, Mapping) else None) or {}
    shared_component_keys = set(shared_dependencies.get(COMPONENT_KEYS_KEY) or ())
    shared_component_map = set(shared_dependencies.get(COMPONENT_HASHES_KEY) or ())
    component_hashes = projection.get(COMPONENT_TABLE_KEY) or {}
    referenced: set[str] = set(shared_component_keys)
    duplicates: list[str] = []
    for row in projection.get("sources") or []:
        source_id = str(row.get(SOURCE_ID_KEY) or "?")
        local_capture = row.get("capture") or {}
        for key in shared:
            if key == RENDER_DEPENDENCY_KEY:
                continue
            if key in local_capture:
                duplicates.append(f"{source_id}:capture.{key}")
        local_dependencies = local_capture.get(RENDER_DEPENDENCY_KEY) or {}
        for key in shared_dependencies:
            if key in (COMPONENT_HASHES_KEY, COMPONENT_KEYS_KEY):
                continue
            if key in local_dependencies:
                duplicates.append(f"{source_id}:render_dependencies.{key}")
        for name in local_dependencies.get(COMPONENT_KEYS_KEY) or ():
            referenced.add(name)
            if name in shared_component_keys:
                duplicates.append(f"{source_id}:component_keys.{name}")
            if name not in component_hashes:
                duplicates.append(f"{source_id}:unresolved_component.{name}")
        for name in (local_dependencies.get(COMPONENT_HASHES_KEY) or {}):
            if name in component_hashes or name in shared_component_map:
                duplicates.append(f"{source_id}:components_sha256.{name}")
        verbatim = set(row.get(VERBATIM_METRICS_KEY) or ())
        for name, value in (row.get("metrics") or {}).items():
            if name not in verbatim and isinstance(value, Mapping):
                duplicates.append(f"{source_id}:metrics.{name}")
    for name in component_hashes:
        if name not in referenced:
            duplicates.append(f"component_hashes.{name}:unreferenced")
    if duplicates:
        raise SummaryProjectionError(
            "projection does not represent every fact exactly once: "
            + ", ".join(duplicates[:5]))
    return {
        "reconstructed_rows": len(restored),
        "shared_capture_keys": sorted(shared),
        "shared_component_hashes": len(shared_component_keys),
        "component_hash_table": len(component_hashes),
        "metric_registry_metrics": len(projection.get(METRIC_REGISTRY_KEY) or {}),
        "duplicate_facts": 0,
    }


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def measure_projection_sizes(full: Mapping[str, Any],
                             projected: Mapping[str, Any]) -> dict[str, Any]:
    """Character counts for the same payload before and after projection."""
    before = _size(full)
    after = _size(projected)
    return {
        "before_chars": before,
        "after_chars": after,
        "saved_chars": before - after,
        "after_ratio": (round(after / before, 4) if before else None),
        "sources_before_chars": _size(full.get("sources") or []),
        "sources_after_chars": _size(projected.get("sources") or []),
    }


__all__ = [
    "SUMMARY_PROJECTION_VERSION",
    "SHARED_CAPTURE_KEYS",
    "PER_SOURCE_CAPTURE_KEYS",
    "SHARED_RENDER_DEPENDENCY_KEYS",
    "COMPONENT_TABLE_KEY",
    "COMPONENT_KEYS_KEY",
    "PROJECTION_KEY",
    "METRIC_REGISTRY_KEY",
    "SHARED_CAPTURE_KEY",
    "SOURCE_ID_KEY",
    "LOCAL_METRIC_DEFINITIONS_KEY",
    "VERBATIM_METRICS_KEY",
    "MERGE_RULE",
    "SummaryProjectionError",
    "assert_projection_is_lossless",
    "measure_projection_sizes",
    "project_analyze_summary",
    "reconstruct_sources",
]
