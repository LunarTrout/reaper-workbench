"""Field-level comparison of stored and live project fingerprints.

This is the cheap half of "did the owner change something behind the agent's
back?": the state store keeps one fingerprint per track/master, reads a fresh
one, and asks this module *which object and which field* differs.  Nothing is
rendered, measured or written here, and the module deliberately depends on
nothing but the standard library — in particular it does not import the state
store, the capture layer or the bridge.  Inputs are plain dictionaries.

A fingerprint is either the project shape::

    {
        "tracks": {guid: {field: value, ...}, ...} | [{"guid": ..., ...}, ...],
        "master": {field: value, ...},
        "routing": {opaque routing facts},
    }

or a flat object map (``{"track:<guid>": {...}, "master": {...}}``).  Object
fingerprints are dictionaries; ``FINGERPRINT_FIELDS`` names the fields that are
compared.  Anything else in an object is ignored, deterministically: a store
may keep names, colours, indices or future facts in the same record without
making every read look like a change.

Three rules keep the answer honest:

* **missing and null are different.**  A field that was not read is not the
  same evidence as a field that was read and had no value.  Each field detail
  carries ``before_present``/``after_present`` next to the values.
* **unreadable is not unchanged.**  A composite field (FX parameters, envelope
  content) that is absent on either side is not compared at all.  The caller
  keeps its existing freshness rule for incomplete fingerprints; a diff is not
  allowed to invent "no change" from missing evidence.
* **field-level facts, never verdicts.**  The output names the object, the
  field, the before/after values and a reason such as ``fx_chain_reordered``.
  It does not say whether a measurement is usable; ``stale_fact_keys`` only
  maps known fingerprint changes onto the facts that depend on them.

Ordering is canonical everywhere: track GUIDs, field names, FX slots, envelope
ids and stale fact keys are sorted, mappings are compared by key union, and a
dictionary's insertion order never changes the answer.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

__all__ = [
    "FINGERPRINT_FIELDS",
    "REASON_MASTER_CHANGED",
    "REASON_PROJECT_CHANGED",
    "REASON_ROUTING_CHANGED",
    "REASON_SOURCE_ADDED",
    "REASON_SOURCE_CHANGED",
    "REASON_SOURCE_MISSING",
    "REASON_SOURCE_UNKNOWN",
    "STAGE_EXTERNAL_FILE",
    "STAGE_MASTER",
    "STAGE_PREMASTER",
    "STAGE_VIA_MASTER",
    "diff_fingerprints",
    "stale_fact_keys",
    "stale_facts",
    "state_changes",
]

#: Fields compared on every track and on the master, in canonical order.
#:
#: ``fx`` is the ordered chain (identity, enabled, offline and the parameter
#: fingerprint when one is available); ``sends`` is the routing leaving this
#: object; ``envelopes`` is the automation owned by it; ``items`` is a summary
#: of the items it carries.  No metric, threshold or musical vocabulary lives
#: in this list.
FINGERPRINT_FIELDS: tuple[str, ...] = (
    "fx",
    "volume",
    "pan",
    "mute",
    "solo",
    "sends",
    "envelopes",
    "items",
    "channel_count",
)

STAGE_PREMASTER = "premaster"
STAGE_VIA_MASTER = "via_master"
STAGE_MASTER = "master"
STAGE_EXTERNAL_FILE = "external_file"

REASON_SOURCE_MISSING = "source_missing"
REASON_SOURCE_ADDED = "source_added"
REASON_SOURCE_CHANGED = "source_changed"
REASON_SOURCE_UNKNOWN = "source_unknown"
REASON_MASTER_CHANGED = "master_changed"
REASON_ROUTING_CHANGED = "routing_changed"
REASON_PROJECT_CHANGED = "project_changed"

#: A fact may be invalidated by more than one dependency.  The reported
#: ``reason`` is the most specific one, in this order.
_REASON_PRIORITY: tuple[str, ...] = (
    REASON_SOURCE_MISSING,
    REASON_SOURCE_ADDED,
    REASON_SOURCE_CHANGED,
    REASON_MASTER_CHANGED,
    REASON_ROUTING_CHANGED,
    REASON_SOURCE_UNKNOWN,
    REASON_PROJECT_CHANGED,
)

_ABSENT = object()

#: Parameter fingerprints may be supplied under any of these names.  They are
#: integration aliases, not a metric vocabulary: whichever is present is
#: compared as one opaque value.
_PARAMETER_HASH_KEYS: tuple[str, ...] = (
    "param_hash",
    "params_hash",
    "params_sha256",
    "parameter_hash",
    "parameter_sha256",
    "parameters_hash",
    "parameters_sha256",
)

_SOURCE_KEYS: tuple[str, ...] = (
    "source_id",
    "source",
    "source_guid",
    "track_guid",
    "owner_guid",
)

_FACT_KEY_KEYS: tuple[str, ...] = ("key", "fact_key", "id", "fact_id")

#: Keys that describe the view, not the automation.  They are excluded when an
#: envelope row has no explicit content fingerprint to compare.
_ENVELOPE_VIEW_KEYS: tuple[str, ...] = ("visible", "selected")

#: A send's slot number is bookkeeping; the destination identity and the send
#: values are the routing fact.
_SEND_VOLATILE_KEYS: tuple[str, ...] = ("index",)


# --------------------------------------------------------------------------- #
# small, total helpers
# --------------------------------------------------------------------------- #


def _plain(value: Any) -> Any:
    """Return a JSON-shaped copy without dropping or truncating a fact."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return {"$bytes": bytes(value).hex()}
    if isinstance(value, float) and not math.isfinite(value):
        return {"$float": repr(value)}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return {"$type": type(value).__name__, "$repr": repr(value)}


def _canonical_text(value: Any) -> str:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=repr)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical_text(value).encode("utf-8")).hexdigest()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _same(left: Any, right: Any) -> bool:
    """Structural equality with deliberate rules for numbers and flags.

    ``0`` and ``0.0`` are the same fader position; ``True`` and ``1`` are not
    the same flag.  Two NaNs compare equal so an unreadable float does not
    produce a change on every read.  Missing is represented by ``_ABSENT`` and
    only equals itself.
    """
    if left is _ABSENT or right is _ABSENT:
        return left is right
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        left_map = {str(key): item for key, item in left.items()}
        right_map = {str(key): item for key, item in right.items()}
        if set(left_map) != set(right_map):
            return False
        return all(_same(left_map[key], right_map[key]) for key in left_map)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return (len(left) == len(right)
                and all(_same(a, b) for a, b in zip(left, right)))
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if (isinstance(left, float) and isinstance(right, float)
                and math.isnan(left) and math.isnan(right)):
            return True
        return left == right
    if type(left) is not type(right):
        return False
    return left == right


def _flag(value: Any) -> Any:
    """Canonicalise a 0/1 read into a bool; every other value stays as read."""
    if value is _ABSENT:
        return _ABSENT
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return value


def _present(container: Mapping[str, Any], key: str) -> tuple[bool, Any]:
    if key in container:
        return True, container[key]
    return False, _ABSENT


def _field_detail(before: Any, after: Any, *, reason: str) -> dict[str, Any]:
    return {
        "before": _plain(before) if before is not _ABSENT else None,
        "after": _plain(after) if after is not _ABSENT else None,
        "before_present": before is not _ABSENT,
        "after_present": after is not _ABSENT,
        "reason": reason,
    }


def _presence_reason(before: Any, after: Any) -> str:
    if before is _ABSENT:
        return "missing_before"
    return "missing_after"


def _occurrence_tokens(tokens: Iterable[str]) -> list[str]:
    """Make duplicate identities distinguishable without losing their order."""
    counts: dict[str, int] = {}
    result: list[str] = []
    for token in tokens:
        seen = counts.get(token, 0)
        counts[token] = seen + 1
        result.append(token if seen == 0 else f"{token}#{seen + 1}")
    return result


def _changed_fields(section: Any) -> list[str]:
    if not isinstance(section, Mapping):
        return []
    fields = section.get("fields")
    return [str(field) for field in fields] if isinstance(fields, (list, tuple)) else []


# --------------------------------------------------------------------------- #
# fingerprint shape
# --------------------------------------------------------------------------- #


def _track_map(value: Any) -> dict[str, Mapping[str, Any]]:
    """Accept ``{guid: row}`` and ``[{"guid": ...}]`` identically."""
    result: dict[str, Mapping[str, Any]] = {}
    if isinstance(value, Mapping):
        for key, row in value.items():
            if isinstance(row, Mapping) and isinstance(key, str) and key:
                result[key] = row
        return result
    for row in _rows(value):
        if isinstance(row, Mapping):
            guid = row.get("guid")
            if isinstance(guid, str) and guid:
                result[guid] = row
    return result


def _master_map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _project_shape(value: Any) -> tuple[dict[str, Mapping[str, Any]],
                                        Mapping[str, Any], Any, bool]:
    """Return ``(tracks, master, routing, routing_present)`` from any input.

    A project fingerprint carries ``tracks``/``master``/``routing``.  A flat
    object map is the convenience form: ``track:<guid>`` and ``master[:<guid>]``
    keys become the project shape, bare keys are tracks.  An envelope with a
    ``fingerprint`` or ``objects`` mapping is unwrapped so the state store can
    pass its stored record directly.
    """
    if not isinstance(value, Mapping):
        return {}, {}, _ABSENT, False

    wrapper = value.get("fingerprint")
    if isinstance(wrapper, Mapping):
        return _project_shape(wrapper)
    nested = value.get("objects")
    if isinstance(nested, Mapping):
        return _project_shape(nested)

    if "tracks" in value or "routing" in value:
        return (_track_map(value.get("tracks")),
                _master_map(value.get("master")),
                value.get("routing", _ABSENT),
                "routing" in value)

    prefixed = any(isinstance(key, str) and
                   (key.startswith("track:") or key.startswith("master:"))
                   for key in value)
    if "master" in value and not prefixed:
        return (_track_map(value.get("tracks")),
                _master_map(value.get("master")),
                _ABSENT, False)

    tracks: dict[str, Mapping[str, Any]] = {}
    master: Mapping[str, Any] = {}
    for key, row in value.items():
        if not isinstance(key, str) or not isinstance(row, Mapping):
            continue
        if key == "master" or key.startswith("master:"):
            master = row
        elif key.startswith("track:"):
            guid = key[len("track:"):]
            if guid:
                tracks[guid] = row
        else:
            tracks[key] = row
    return tracks, master, _ABSENT, False


def _object_field(obj: Mapping[str, Any], field: str) -> Any:
    """Read one canonical field from a fingerprint, tolerance included.

    The nested ``state`` mapping (the digest-facts shape) and a track-level
    ``routing`` mapping (the render-dependency shape) are accepted so the store
    does not have to reshape a digest before comparing it.
    """
    if field in obj:
        return obj[field]
    state = obj.get("state")
    if isinstance(state, Mapping) and field in state:
        return state[field]
    if field == "sends":
        routing = obj.get("routing")
        if isinstance(routing, Mapping) and "sends" in routing:
            return routing["sends"]
    if field == "envelopes":
        for alias in ("automation", "envelope"):
            if alias in obj:
                return obj[alias]
    if field == "items":
        for alias in ("item_summary", "items_summary", "items_digest"):
            if alias in obj:
                return obj[alias]
    return _ABSENT


# --------------------------------------------------------------------------- #
# FX chain
# --------------------------------------------------------------------------- #


def _fx_rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        chain = value.get("chain")
        if isinstance(chain, (list, tuple)):
            return [row for row in chain if isinstance(row, Mapping)]
        if "guid" in value or "name" in value:
            return [value]
        indexed = [row for row in value.values() if isinstance(row, Mapping)]
        return indexed
    return [row for row in _rows(value) if isinstance(row, Mapping)]


def _fx_identity(row: Mapping[str, Any], position: int) -> str:
    guid = row.get("guid")
    if isinstance(guid, str) and guid:
        return f"guid:{guid}"
    name = row.get("name")
    if isinstance(name, str) and name:
        return f"name:{name}"
    index = row.get("index")
    if isinstance(index, int) and not isinstance(index, bool):
        return f"index:{index}"
    return f"index:{position}"


def _parameter_summary(value: Any) -> Any:
    """Normalize a parameter set to an order-independent comparison value."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        rows: list[dict[str, Any]] = []
        for position, row in enumerate(value):
            if isinstance(row, Mapping):
                payload = {str(key): item for key, item in row.items()
                           if key not in {"formatted", "name"}}
                payload.setdefault("index", row.get("index", position))
                payload.setdefault("position", position)
                rows.append(payload)
            else:
                rows.append({"position": position, "value": _plain(row)})
        return sorted(rows, key=_canonical_text)
    return _plain(value)


def _fx_parameters(row: Mapping[str, Any]) -> tuple[bool, Any]:
    """Return ``(present, value)`` for the plugin parameter fingerprint.

    An explicit hash wins; otherwise a parameter mapping or list is normalised
    and compared structurally.  ``present=False`` means the parameter state was
    not read — the diff then says nothing about parameters rather than guessing.
    """
    for key in _PARAMETER_HASH_KEYS:
        if key in row:
            value = row[key]
            if value is not None and not isinstance(value, (str, int, float, bool)):
                continue
            return True, _plain(value)
    if "parameters" in row:
        return True, _parameter_summary(row["parameters"])
    if "params" in row:
        return True, _parameter_summary(row["params"])
    return False, _ABSENT


def _slot_value(row: Mapping[str, Any], key: str) -> Any:
    present, value = _present(row, key)
    return _flag(value) if present else _ABSENT


def _fx_diff(before_value: Any, after_value: Any) -> dict[str, Any] | None:
    before_rows = _fx_rows(before_value)
    after_rows = _fx_rows(after_value)
    before_tokens = _occurrence_tokens(
        _fx_identity(row, position) for position, row in enumerate(before_rows))
    after_tokens = _occurrence_tokens(
        _fx_identity(row, position) for position, row in enumerate(after_rows))

    before_index = dict(zip(before_tokens, before_rows))
    after_index = dict(zip(after_tokens, after_rows))

    added = sorted((Counter(after_tokens) - Counter(before_tokens)).elements())
    removed = sorted((Counter(before_tokens) - Counter(after_tokens)).elements())
    common = [token for token in before_tokens if token in after_index]

    reordered = False
    if before_tokens != after_tokens and not added and not removed:
        reordered = True
    elif common:
        after_common = [token for token in after_tokens if token in before_index]
        reordered = common != after_common

    slots: dict[str, Any] = {}
    reasons: list[str] = []
    if reordered:
        reasons.append("fx_chain_reordered")
    if added:
        reasons.append("fx_added")
    if removed:
        reasons.append("fx_removed")

    for token in sorted(set(before_tokens) & set(after_tokens)):
        left, right = before_index[token], after_index[token]
        slot: dict[str, Any] = {}
        for field, reason in (("name", "fx_renamed"),
                              ("enabled", "fx_bypass_changed"),
                              ("offline", "fx_offline_changed")):
            left_value = _slot_value(left, field)
            right_value = _slot_value(right, field)
            if not _same(left_value, right_value):
                detail_reason = (reason if left_value is not _ABSENT
                                 and right_value is not _ABSENT
                                 else _presence_reason(left_value, right_value))
                slot[field] = _field_detail(left_value, right_value,
                                            reason=detail_reason)
                if detail_reason not in reasons:
                    reasons.append(detail_reason)

        left_has_params, left_params = _fx_parameters(left)
        right_has_params, right_params = _fx_parameters(right)
        if left_has_params and right_has_params and not _same(left_params, right_params):
            slot["parameters"] = _field_detail(left_params, right_params,
                                               reason="fx_parameters_changed")
            if "fx_parameters_changed" not in reasons:
                reasons.append("fx_parameters_changed")
        if slot:
            slots[token] = slot

    if not reasons and not slots:
        return None
    detail: dict[str, Any] = {
        "reasons": reasons,
        "added": added,
        "removed": removed,
        "slots": slots,
    }
    if reordered:
        detail["order_before"] = list(before_tokens)
        detail["order_after"] = list(after_tokens)
    return detail


# --------------------------------------------------------------------------- #
# sends, envelopes, items
# --------------------------------------------------------------------------- #


def _route_rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [row for row in value.values() if isinstance(row, Mapping)]
    return [row for row in _rows(value) if isinstance(row, Mapping)]


def _send_identity(row: Mapping[str, Any], position: int) -> str:
    for key in ("dest_guid", "dest_name", "dest"):
        target = row.get(key)
        if isinstance(target, str) and target:
            return f"{key}:{target}"
    index = row.get("index")
    if isinstance(index, int) and not isinstance(index, bool):
        return f"index:{index}"
    return f"index:{position}"


def _send_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, item in row.items():
        if key in _SEND_VOLATILE_KEYS:
            continue
        if key in {"mute", "phase", "mono"}:
            payload[str(key)] = _flag(item)
        else:
            payload[str(key)] = _plain(item)
    return payload


def _sends_diff(before_value: Any, after_value: Any) -> dict[str, Any] | None:
    if isinstance(before_value, (int, float)) or isinstance(after_value, (int, float)):
        if _same(before_value, after_value):
            return None
        return _field_detail(before_value, after_value, reason="value_changed")

    before_rows = _route_rows(before_value)
    after_rows = _route_rows(after_value)
    before_tokens = _occurrence_tokens(
        _send_identity(row, position) for position, row in enumerate(before_rows))
    after_tokens = _occurrence_tokens(
        _send_identity(row, position) for position, row in enumerate(after_rows))
    before_index = dict(zip(before_tokens, before_rows))
    after_index = dict(zip(after_tokens, after_rows))

    added = sorted((Counter(after_tokens) - Counter(before_tokens)).elements())
    removed = sorted((Counter(before_tokens) - Counter(after_tokens)).elements())
    changed: dict[str, Any] = {}
    for token in sorted(set(before_tokens) & set(after_tokens)):
        left = _send_payload(before_index[token])
        right = _send_payload(after_index[token])
        if not _same(left, right):
            changed[token] = {"before": left, "after": right,
                              "reason": "send_changed"}
    if not added and not removed and not changed:
        return None
    reasons: list[str] = []
    if added:
        reasons.append("send_added")
    if removed:
        reasons.append("send_removed")
    if changed:
        reasons.append("send_changed")
    return {
        "reasons": reasons,
        "added": added,
        "removed": removed,
        "changed": changed,
        "count_before": len(before_rows),
        "count_after": len(after_rows),
    }


def _envelope_rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        rows: list[Mapping[str, Any]] = []
        for key, row in value.items():
            if isinstance(row, Mapping):
                item = dict(row)
                item.setdefault("guid", str(key))
                rows.append(item)
        return rows
    return [row for row in _rows(value) if isinstance(row, Mapping)]


def _envelope_identity(row: Mapping[str, Any], position: int) -> str:
    guid = row.get("guid")
    if isinstance(guid, str) and guid:
        return f"guid:{guid}"
    name = row.get("name")
    if isinstance(name, str) and name:
        return f"name:{name}"
    index = row.get("index")
    if isinstance(index, int) and not isinstance(index, bool):
        return f"index:{index}"
    return f"index:{position}"


def _envelope_content(row: Mapping[str, Any]) -> tuple[bool, str]:
    """Content fingerprint of one envelope: explicit hash or canonical rows."""
    for key in ("sha256", "content_hash", "points_sha256", "hash", "digest"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return True, value
    for key in ("points", "chunk", "state_chunk"):
        if key in row:
            return True, _sha(row[key])
    return False, ""


def _envelope_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in ("name", "active", "armed", "point_count",
                "automation_item_count", "scaling_mode"):
        if key in row:
            summary[key] = _plain(row[key])
    has_content, content = _envelope_content(row)
    if has_content:
        summary["content"] = content
    else:
        payload = {str(key): item for key, item in row.items()
                   if key not in _ENVELOPE_VIEW_KEYS}
        summary["content"] = _sha(payload)
    return summary


def _envelopes_diff(before_value: Any, after_value: Any) -> dict[str, Any] | None:
    before_rows = _envelope_rows(before_value)
    after_rows = _envelope_rows(after_value)
    before_tokens = _occurrence_tokens(
        _envelope_identity(row, position) for position, row in enumerate(before_rows))
    after_tokens = _occurrence_tokens(
        _envelope_identity(row, position) for position, row in enumerate(after_rows))
    before_index = dict(zip(before_tokens, before_rows))
    after_index = dict(zip(after_tokens, after_rows))

    added = sorted((Counter(after_tokens) - Counter(before_tokens)).elements())
    removed = sorted((Counter(before_tokens) - Counter(after_tokens)).elements())
    modified: dict[str, Any] = {}
    for token in sorted(set(before_tokens) & set(after_tokens)):
        left = _envelope_summary(before_index[token])
        right = _envelope_summary(after_index[token])
        if not _same(left, right):
            modified[token] = {
                "before": left,
                "after": right,
                "reason": "envelope_changed",
            }
    if not added and not removed and not modified:
        return None
    reasons: list[str] = []
    if added:
        reasons.append("envelope_added")
    if removed:
        reasons.append("envelope_removed")
    if modified:
        reasons.append("envelope_changed")
    return {
        "reasons": reasons,
        "added": added,
        "removed": removed,
        "modified": modified,
        "count_before": len(before_rows),
        "count_after": len(after_rows),
    }


def _item_rows(value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, Mapping):
        return [(str(key), row) for key, row in value.items()]
    return [(str(row.get("guid") or position), row)
            for position, row in enumerate(_rows(value))]


def _items_summary(value: Any) -> dict[str, Any] | None:
    """Summarise item rows as count + ids + one canonical content hash."""
    if isinstance(value, (list, tuple)):
        entries = _item_rows(value)
    elif isinstance(value, Mapping) and value and all(
            isinstance(row, Mapping) for row in value.values()):
        entries = _item_rows(value)
    else:
        return None
    rows = [row for _key, row in entries]
    ids = _occurrence_tokens(key for key, _row in entries)
    ordered = sorted(({"id": key, "row": _plain(row)} for key, row in entries),
                     key=_canonical_text)
    return {
        "count": len(rows),
        "ids": sorted(ids),
        "content": _sha(ordered),
    }


def _items_diff(before_value: Any, after_value: Any) -> dict[str, Any] | None:
    before_summary = _items_summary(before_value)
    after_summary = _items_summary(after_value)
    if before_summary is not None and after_summary is not None:
        if _same(before_summary, after_summary):
            return None
        added = sorted(set(after_summary["ids"]) - set(before_summary["ids"]))
        removed = sorted(set(before_summary["ids"]) - set(after_summary["ids"]))
        reasons: list[str] = []
        if added:
            reasons.append("item_added")
        if removed:
            reasons.append("item_removed")
        if before_summary["content"] != after_summary["content"]:
            reasons.append("item_changed")
        return {
            "reasons": reasons,
            "count_before": before_summary["count"],
            "count_after": after_summary["count"],
            "added": added,
            "removed": removed,
            "content_changed": before_summary["content"] != after_summary["content"],
        }

    # Already a summary mapping (e.g. {"count": 3, "sha256": ...}) or a scalar:
    # compare every key present on either side, without imposing a metric list.
    if isinstance(before_value, Mapping) or isinstance(after_value, Mapping):
        left = _mapping(before_value) if before_value is not _ABSENT else {}
        right = _mapping(after_value) if after_value is not _ABSENT else {}
        details: dict[str, Any] = {}
        for key in sorted(set(left) | set(right)):
            left_present, left_item = _present(left, str(key))
            right_present, right_item = _present(right, str(key))
            left_item = left_item if left_present else _ABSENT
            right_item = right_item if right_present else _ABSENT
            if _same(left_item, right_item):
                continue
            reason = ("missing_before" if left_item is _ABSENT
                      else "missing_after" if right_item is _ABSENT
                      else "value_changed")
            details[str(key)] = _field_detail(left_item, right_item, reason=reason)
        if not details:
            return None
        return {"reasons": ["items_changed"], "changed_keys": sorted(details),
                "details": details}

    if _same(before_value, after_value):
        return None
    return _field_detail(before_value, after_value, reason="value_changed")


# --------------------------------------------------------------------------- #
# per-object comparison
# --------------------------------------------------------------------------- #


def _field_change(obj_before: Mapping[str, Any], obj_after: Mapping[str, Any],
                  field: str) -> Any:
    """Return a field detail, or ``None`` when the field did not change."""
    before_present, before = _present(obj_before, field)
    if not before_present:
        before = _object_field(obj_before, field)
        before_present = before is not _ABSENT
    after_present, after = _present(obj_after, field)
    if not after_present:
        after = _object_field(obj_after, field)
        after_present = after is not _ABSENT

    if not before_present and not after_present:
        return None
    if not before_present or not after_present:
        return _field_detail(before, after,
                             reason=_presence_reason(before, after))

    if field in {"fx", "sends", "envelopes", "items"}:
        # A present null is "read, but unreadable", not an empty chain/list.
        # Claiming items were added because a read failed would be a false
        # change; say the evidence differs and let freshness decide.
        if before is None or after is None:
            if before is None and after is None:
                return None
            return _field_detail(before, after, reason="unreadable")

    if field == "fx":
        return _fx_diff(before, after)
    if field == "sends":
        return _sends_diff(before, after)
    if field == "envelopes":
        return _envelopes_diff(before, after)
    if field == "items":
        return _items_diff(before, after)
    if field == "mute":
        before, after = _flag(before), _flag(after)
    if _same(before, after):
        return None
    return _field_detail(before, after, reason="value_changed")


def _object_diff(before: Mapping[str, Any],
                 after: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
    fields: list[str] = []
    details: dict[str, Any] = {}
    for field in FINGERPRINT_FIELDS:
        detail = _field_change(before, after, field)
        if detail is None:
            continue
        fields.append(field)
        details[field] = detail
    return fields, details


def _opaque_diff(before_value: Any, after_value: Any,
                 before_present: bool, after_present: bool) -> dict[str, Any]:
    """Compare an opaque routing fingerprint: mapping keys or one value."""
    if isinstance(before_value, Mapping) or isinstance(after_value, Mapping):
        left = _mapping(before_value)
        right = _mapping(after_value)
        fields: dict[str, Any] = {}
        for key in sorted(set(left) | set(right)):
            left_present, left_item = _present(left, str(key))
            right_present, right_item = _present(right, str(key))
            left_item = left_item if left_present else _ABSENT
            right_item = right_item if right_present else _ABSENT
            if _same(left_item, right_item):
                continue
            reason = ("missing_before" if left_item is _ABSENT
                      else "missing_after" if right_item is _ABSENT
                      else "value_changed")
            fields[str(key)] = _field_detail(left_item, right_item, reason=reason)
        return {"fields": fields}
    if _same(before_value, after_value) and before_present == after_present:
        return {"fields": {}}
    return {"fields": {"value": _field_detail(
        before_value if before_present else _ABSENT,
        after_value if after_present else _ABSENT,
        reason=("missing_before" if not before_present
                else "missing_after" if not after_present
                else "value_changed"))}}


def _master_guid(*masters: Mapping[str, Any]) -> str | None:
    for master in masters:
        guid = master.get("guid") if isinstance(master, Mapping) else None
        if isinstance(guid, str) and guid:
            return guid
    return None


def diff_fingerprints(stored: Mapping[str, Any],
                      current: Mapping[str, Any]) -> dict[str, Any]:
    """Compare two project fingerprints field by field.

    Only objects with at least one changed field appear under ``tracks`` (and
    inside ``master``/``routing``).  ``added_tracks``/``removed_tracks`` name
    identity changes, ``unchanged_tracks`` is the explicit complement so a fact
    can tell "unchanged" from "never seen".  Every detail is a fact about a
    field: values, presence, and a reason — no freshness verdict is included.
    """
    stored_tracks, stored_master, stored_routing, stored_routing_present = \
        _project_shape(stored)
    current_tracks, current_master, current_routing, current_routing_present = \
        _project_shape(current)

    stored_guids = set(stored_tracks)
    current_guids = set(current_tracks)
    added_tracks = sorted(current_guids - stored_guids)
    removed_tracks = sorted(stored_guids - current_guids)
    common_guids = sorted(stored_guids & current_guids)

    tracks: dict[str, Any] = {}
    unchanged: list[str] = []
    send_changes: list[str] = []
    for guid in common_guids:
        fields, details = _object_diff(stored_tracks[guid], current_tracks[guid])
        if "sends" in fields:
            send_changes.append(guid)
        if fields:
            tracks[guid] = {"fields": fields, "details": details}
        else:
            unchanged.append(guid)

    master_fields: list[str] = []
    master_details: dict[str, Any] = {}
    if stored_master or current_master:
        if not stored_master or not current_master:
            master_fields = ["presence"]
            master_details["presence"] = {
                "before_present": bool(stored_master),
                "after_present": bool(current_master),
                "reason": ("missing_before" if not stored_master
                           else "missing_after"),
            }
        else:
            master_fields, master_details = _object_diff(stored_master,
                                                         current_master)
            if "sends" in master_fields:
                send_changes.append("master")

    routing_fields: list[str] = []
    routing_details: dict[str, Any] = {}
    if stored_routing_present or current_routing_present:
        before_routing = stored_routing if stored_routing_present else _ABSENT
        after_routing = current_routing if current_routing_present else _ABSENT
        opaque = _opaque_diff(
            before_routing if before_routing is not _ABSENT else {},
            after_routing if after_routing is not _ABSENT else {},
            stored_routing_present,
            current_routing_present,
        )
        explicit_fields = sorted(opaque["fields"])
        if explicit_fields:
            routing_fields.extend(explicit_fields)
            routing_details["explicit"] = {
                "fields": explicit_fields,
                "details": opaque["fields"],
            }
    if send_changes:
        routing_fields.append("sends")
        routing_details["sends"] = {
            "sources": sorted(send_changes),
            "reason": "sends_changed",
        }

    changed = bool(added_tracks or removed_tracks or tracks or master_fields
                   or routing_fields)
    return {
        "changed": changed,
        "tracks": tracks,
        "master": {"fields": master_fields, "details": master_details},
        "routing": {
            "changed": bool(routing_fields),
            "fields": routing_fields,
            "details": routing_details,
        },
        "added_tracks": added_tracks,
        "removed_tracks": removed_tracks,
        "unchanged_tracks": unchanged,
        "master_guid": _master_guid(current_master, stored_master),
    }


# --------------------------------------------------------------------------- #
# facts
# --------------------------------------------------------------------------- #


def _fact_key(fact: Mapping[str, Any]) -> str | None:
    for name in _FACT_KEY_KEYS:
        if name not in fact:
            continue
        value = fact[name]
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        return None
    return None


def _fact_stage(fact: Mapping[str, Any]) -> str | None:
    for name in ("stage", "signal_stage"):
        value = fact.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _fact_source(fact: Mapping[str, Any]) -> str | None:
    for name in _SOURCE_KEYS:
        value = fact.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _stage_kind(stage: str | None, *, is_master: bool,
                has_source: bool) -> str:
    """Classify a fact's stage into its dependency kind.

    Stage names in this codebase are descriptive, not exact tokens
    (``selected_track_via_master_parallel_batch_post_fx`` carries both the
    source and the master in its signal path), so the match is structural:

    * anything naming ``external`` depends on nothing in the project;
    * anything naming ``via_master`` depends on source + master + routing;
    * an explicit ``premaster`` stage depends on the source only;
    * anything naming ``master`` (``master_mix_post_fx``) is a master-stage
      fact and depends on the master fingerprint only.

    A fact with a source but no recognisable stage defaults to ``via_master``:
    every track capture this library produces runs through the master, and
    under-invalidating is the one failure mode a freshness layer must not have.
    """
    if isinstance(stage, str):
        name = stage.strip().lower()
        if name == STAGE_EXTERNAL_FILE or "external" in name:
            return "external"
        if name == STAGE_VIA_MASTER or "via_master" in name:
            return "via_master"
        if name == STAGE_PREMASTER or "premaster" in name:
            return "source"
        if name == STAGE_MASTER or "master" in name:
            return "master"
    if is_master:
        return "master"
    if has_source:
        return "via_master"
    return "project"


def _source_scope(source: str | None,
                  master_guid: str | None) -> tuple[str, str | None]:
    if not isinstance(source, str) or not source:
        return "none", None
    if source.startswith("track:"):
        guid = source[len("track:"):]
        return ("track", guid) if guid else ("unknown", source)
    if source.startswith("master:"):
        return "master", source[len("master:"):] or master_guid
    if source == "master" or (master_guid and source == master_guid):
        return "master", source
    return "track", source


def _enumerates_objects(changes: Mapping[str, Any]) -> bool:
    """Whether the changes mapping lists unchanged objects as well.

    ``diff_fingerprints`` always does.  A caller that passes only the changed
    slice has no way to say "unchanged", so an unknown source is treated as
    unchanged rather than fabricated into a stale fact.
    """
    return isinstance(changes.get("unchanged_tracks"), (list, tuple))


def _track_status(guid: str | None, changes: Mapping[str, Any]) -> str:
    """Classify one source against the diff.

    ``routing_only`` is distinct from ``changed``: a track whose only changed
    field is ``sends`` has not altered its own signal.  The routing dependency
    already invalidates every ``via_master`` fact, and a ``premaster``
    measurement of that track stays valid — re-rendering it would be waste.
    """
    if not guid:
        return "unknown"
    if guid in set(changes.get("removed_tracks") or ()):
        return "removed"
    if guid in set(changes.get("added_tracks") or ()):
        return "added"
    track_changes = _mapping(changes.get("tracks"))
    if guid in track_changes:
        fields = _changed_fields(track_changes.get(guid))
        if fields and set(fields) == {"sends"}:
            return "routing_only"
        return "changed"
    if _enumerates_objects(changes):
        if guid in set(changes.get("unchanged_tracks") or ()):
            return "unchanged"
        return "unknown"
    return "unchanged"


def _reason_entry(key: str, reasons: Sequence[str], *,
                  stage: str, source: str | None) -> dict[str, Any]:
    ordered = [reason for reason in _REASON_PRIORITY if reason in set(reasons)]
    reasons_out = ordered or list(dict.fromkeys(reasons))
    reason = reasons_out[0] if reasons_out else None
    dependency = "project"
    if reason in {REASON_SOURCE_MISSING, REASON_SOURCE_ADDED,
                  REASON_SOURCE_CHANGED}:
        dependency = source or "source"
    elif reason == REASON_MASTER_CHANGED:
        dependency = "master"
    elif reason == REASON_ROUTING_CHANGED:
        dependency = "routing"
    elif reason == REASON_SOURCE_UNKNOWN:
        dependency = "source"
    return {
        "key": key,
        "reason": reason,
        "reasons": reasons_out,
        "dependency": dependency,
        "stage": stage,
        "source": source,
    }


def stale_facts(facts: Sequence[Mapping[str, Any]],
                changes: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Fact keys invalidated by fingerprint changes, with their reasons.

    Dependency rules:

    * ``external_file`` facts depend on nothing in the project and are never
      stale from a fingerprint diff;
    * master-stage facts depend on the master fingerprint only;
    * ``via_master`` facts depend on their own source, on the master and on
      routing — any routing change feeds a different mix to the master;
    * ``premaster`` facts depend on their own source only;
    * a track whose *only* changed field is ``sends`` has not changed its own
      signal: it invalidates ``via_master`` through routing, never a
      ``premaster`` fact;
    * a fact whose source was removed is stale with ``source_missing``;
    * a fact with no source and no recognisable stage is project-scoped and
      stale when anything in the project fingerprint changed.

    The result is deduplicated by fact key and sorted, so two runs over the
    same inputs produce the same list.
    """
    master_changed = bool(_changed_fields(_mapping(changes).get("master")))
    routing = _mapping(changes.get("routing"))
    routing_changed = (bool(routing.get("changed"))
                       or bool(_changed_fields(routing)))
    any_change = bool(
        master_changed or routing_changed
        or _mapping(changes.get("tracks"))
        or changes.get("added_tracks") or changes.get("removed_tracks")
    )
    master_guid = changes.get("master_guid")
    if not isinstance(master_guid, str) or not master_guid:
        master_guid = None

    result: dict[str, dict[str, Any]] = {}
    for fact in facts:
        if not isinstance(fact, Mapping):
            continue
        key = _fact_key(fact)
        if key is None:
            continue
        stage = _fact_stage(fact)
        source = _fact_source(fact)
        kind = _stage_kind(
            stage,
            is_master=fact.get("is_master") is True or fact.get("scope") == "master",
            has_source=source is not None,
        )

        reasons: list[str] = []
        if kind == "external":
            pass
        elif kind == "master":
            if master_changed:
                reasons.append(REASON_MASTER_CHANGED)
        elif kind == "project":
            if any_change:
                reasons.append(REASON_PROJECT_CHANGED)
        else:
            scope, identity = _source_scope(source, master_guid)
            if kind == "via_master":
                if scope == "track":
                    status = _track_status(identity, changes)
                    if status == "removed":
                        reasons.append(REASON_SOURCE_MISSING)
                    elif status == "added":
                        reasons.append(REASON_SOURCE_ADDED)
                    elif status == "changed":
                        reasons.append(REASON_SOURCE_CHANGED)
                    elif status == "unknown":
                        reasons.append(REASON_SOURCE_UNKNOWN)
                elif scope == "unknown":
                    reasons.append(REASON_SOURCE_UNKNOWN)
                if master_changed:
                    reasons.append(REASON_MASTER_CHANGED)
                if routing_changed:
                    reasons.append(REASON_ROUTING_CHANGED)
            else:  # premaster / source stage
                if scope == "track":
                    status = _track_status(identity, changes)
                    if status == "removed":
                        reasons.append(REASON_SOURCE_MISSING)
                    elif status == "added":
                        reasons.append(REASON_SOURCE_ADDED)
                    elif status == "changed":
                        reasons.append(REASON_SOURCE_CHANGED)
                    elif status == "unknown":
                        reasons.append(REASON_SOURCE_UNKNOWN)
                elif scope == "master":
                    # A "premaster" fact scoped to the master is really a
                    # master-stage fact; only the master can invalidate it.
                    if master_changed:
                        reasons.append(REASON_MASTER_CHANGED)
                else:
                    reasons.append(REASON_SOURCE_UNKNOWN)

        if not reasons:
            continue
        entry = _reason_entry(key, reasons, stage=kind, source=source)
        existing = result.get(key)
        if existing is None:
            result[key] = entry
            continue
        merged = list(dict.fromkeys(existing["reasons"] + entry["reasons"]))
        result[key] = _reason_entry(key, merged, stage=existing["stage"],
                                    source=existing["source"])

    return [result[key] for key in sorted(result)]


def stale_fact_keys(facts: Sequence[Mapping[str, Any]],
                    changes: Mapping[str, Any]) -> list[str]:
    """The fact keys to mark stale, sorted; see :func:`stale_facts`."""
    return [row["key"] for row in stale_facts(facts, changes)]


# --------------------------------------------------------------------------- #
# convenience wrapper
# --------------------------------------------------------------------------- #


def _stored_parts(value: Any) -> tuple[Mapping[str, Any], list[Mapping[str, Any]]]:
    """Split a stored-state envelope into ``(fingerprint, facts)``.

    Accepted forms::

        {"fingerprint": {...}, "facts": [...]}
        {"objects": {...}, "facts": [...]}
        {... project or flat fingerprint ...}
    """
    if not isinstance(value, Mapping):
        return {}, []
    facts: list[Mapping[str, Any]] = [
        fact for fact in _rows(value.get("facts")) if isinstance(fact, Mapping)
    ]
    for key in ("fingerprint", "objects"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            return nested, facts
    return value, facts


def state_changes(stored_objects: Any,
                  current_objects: Any) -> dict[str, Any]:
    """Convenience wrapper: compare stored/current state and stale its facts.

    ``stored_objects`` may be the stored record (fingerprint plus the facts
    recorded with it) and ``current_objects`` the live fingerprint.  The result
    has exactly three keys: ``changed``, ``objects`` (the object-level diff) and
    ``stale_facts`` (the deduplicated fact rows from :func:`stale_facts`).
    """
    stored_fingerprint, stored_facts = _stored_parts(stored_objects)
    current_fingerprint, current_facts = _stored_parts(current_objects)
    facts = stored_facts or current_facts
    changes = diff_fingerprints(stored_fingerprint, current_fingerprint)
    return {
        "changed": changes["changed"],
        "objects": {
            "tracks": changes["tracks"],
            "master": changes["master"],
            "routing": changes["routing"],
            "added_tracks": changes["added_tracks"],
            "removed_tracks": changes["removed_tracks"],
            "unchanged_tracks": changes["unchanged_tracks"],
            "master_guid": changes["master_guid"],
        },
        "stale_facts": stale_facts(facts, changes),
    }
