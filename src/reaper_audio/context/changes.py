"""Factual structural deltas for verified project mutations.

A mutation delta is not a summary of intent.  It is the strongest before/after
fact set the bridge acknowledged, bound to the exact context the model had:

* the acknowledged base (context token, capture, revision, fact/view roots);
* the post-operation revision and digest fingerprint;
* per-operation readback facts (what the bridge read before and after);
* object-level structural differences from the two digests;
* the render-dependency verdict: which captured audio nodes provably survive,
  which changed, and the evidence ids those changes invalidate;
* explicit coverage.  A partial readback, an unknown mutation scope or a
  missing render manifest never yields an acknowledgeable delta.

Everything is hashed with canonical JSON.  ``delta_hash`` covers the payload
without its own hash, so a client can verify exactly what the server served.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .render_dependencies import (build_render_manifest, compare_render_manifests,
                                  project_render_manifest)

__all__ = [
    "DELTA_SCHEMA",
    "FACTS_SCHEMA",
    "MAX_DELTA_OBJECTS",
    "build_structural_delta",
    "delta_hash",
    "digest_facts",
    "next_view_root",
]

DELTA_SCHEMA = "reaper-workbench.structural-delta/1"
FACTS_SCHEMA = "reaper-workbench.digest-facts/1"
MAX_DELTA_OBJECTS = 2_000
MAX_EVIDENCE_IDS = 2_000
MAX_OPERATION_FACTS = 256

_TRACK_FIELDS = (
    "volume", "pan", "mute", "solo", "channel_count", "pan_mode", "pan_law",
    "pan_law_flags", "width", "dualpan_left", "dualpan_right", "phase_inverted",
    "main_send", "play_offset", "play_offset_flags", "freeze_count", "folder_depth",
)
_ITEM_FIELDS = (
    "position_s", "length_s", "start_offset_s", "playrate", "mute", "fade_in_s",
    "fade_out_s", "channel_mode", "group_id", "loop_source", "beat_attach_mode",
    "take_guid", "source_path", "take_volume", "take_pan", "take_pitch",
    "preserve_pitch",
)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _rows_by(rows: Iterable[Any], key: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if isinstance(row, Mapping) and isinstance(row.get(key), str) and row[key]:
            result[str(row[key])] = row
    return result


def delta_hash(payload: Mapping[str, Any]) -> str:
    """Hash a delta payload exactly as served (without its own hash)."""
    body = {key: value for key, value in payload.items()
            if key not in {"delta_hash", "delta_id", "created_at", "acknowledged",
                           "successor_token"}}
    return _sha(body)


def next_view_root(previous_view_root: str, payload_hash: str) -> str:
    """Chain hash: the model's structural view after acknowledging this delta."""
    return _sha({"previous_view_root": previous_view_root,
                 "delta_hash": payload_hash, "schema": DELTA_SCHEMA})


def _value(value: Any) -> Any:
    """Bound one reported value so a plugin blob cannot dominate a delta."""
    if isinstance(value, str):
        return value if len(value) <= 512 else value[:512] + "…"
    if isinstance(value, (list, tuple)):
        if len(value) > 64:
            return {"truncated": True, "count": len(value)}
        return [_value(item) for item in value]
    if isinstance(value, Mapping):
        if len(value) > 64:
            return {"truncated": True, "keys": len(value)}
        return {str(key): _value(item) for key, item in value.items()}
    return value


def _field_diff(before: Mapping[str, Any], after: Mapping[str, Any],
                fields: Sequence[str]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for field in fields:
        if before.get(field) != after.get(field):
            changes.append({"field": field, "before": _value(before.get(field)),
                            "after": _value(after.get(field))})
    return changes


def _fx_changes(before: Sequence[Any], after: Sequence[Any]) -> dict[str, Any]:
    """Per-slot FX facts: identity/order, bypass/offline and changed parameters."""
    before_rows = [row for row in before if isinstance(row, Mapping)]
    after_rows = [row for row in after if isinstance(row, Mapping)]
    base = {str(row.get("guid") or row.get("index")): row for row in before_rows}
    target = {str(row.get("guid") or row.get("index")): row for row in after_rows}
    result: dict[str, Any] = {
        "order_before": [row.get("guid") for row in before_rows],
        "order_after": [row.get("guid") for row in after_rows],
        "added": sorted(set(target) - set(base)),
        "removed": sorted(set(base) - set(target)),
        "changed": [],
    }
    for guid in sorted(set(base) & set(target)):
        left, right = base[guid], target[guid]
        slot: dict[str, Any] = {"fx_guid": guid}
        changed = False
        for field in ("name", "enabled", "offline"):
            if left.get(field) != right.get(field):
                slot[field] = {"before": _value(left.get(field)),
                               "after": _value(right.get(field))}
                changed = True
        left_params = {int(row.get("index", -1)): row.get("normalized")
                       for row in _list(left.get("params")) if isinstance(row, Mapping)}
        right_params = {int(row.get("index", -1)): row.get("normalized")
                        for row in _list(right.get("params")) if isinstance(row, Mapping)}
        parameter_changes = {
            str(index): {"before": left_params.get(index), "after": right_params.get(index)}
            for index in sorted(set(left_params) | set(right_params))
            if left_params.get(index) != right_params.get(index)
        }
        if parameter_changes:
            slot["parameters"] = parameter_changes
            changed = True
        if changed:
            result["changed"].append(slot)
    return result


def _route_changes(before: Sequence[Any], after: Sequence[Any]) -> list[dict[str, Any]]:
    before_rows = [row for row in before if isinstance(row, Mapping)]
    after_rows = [row for row in after if isinstance(row, Mapping)]
    changes: list[dict[str, Any]] = []
    for index in range(max(len(before_rows), len(after_rows))):
        left = before_rows[index] if index < len(before_rows) else {}
        right = after_rows[index] if index < len(after_rows) else {}
        if left != right:
            changes.append({
                "index": index,
                "before": None if not left else _value(left),
                "after": None if not right else _value(right),
            })
    return changes


def _fx_fact_rows(rows: Iterable[Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for slot in rows:
        if not isinstance(slot, Mapping):
            continue
        params = {
            str(parameter.get("index")): _sha(parameter.get("normalized"))
            for parameter in _list(slot.get("params")) if isinstance(parameter, Mapping)
        }
        output.append({
            "index": slot.get("index"),
            "guid": slot.get("guid"),
            "name": slot.get("name"),
            "enabled": slot.get("enabled"),
            "offline": slot.get("offline"),
            "params_sha256": _sha(params),
            "param_count": len(params),
        })
    return output


def digest_facts(digest: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project a bridge digest into the compact facts a delta compares.

    Heavy payloads (parameter arrays, envelope point lists, state chunk text)
    are replaced by canonical hashes.  Nothing that changes the signal path is
    dropped: FX identity/order/bypass stay explicit and a parameter difference
    is still visible as a per-parameter-set hash.
    """
    if not isinstance(digest, Mapping):
        return {"schema": FACTS_SCHEMA, "available": False, "reason": "digest_absent",
                "tracks": {}, "items": {}, "master": None, "automation": [],
                "tempo_sha256": None, "project_sha256": None,
                "project_settings": {}, "fx_state": {}}
    fields = _mapping(digest.get("fields"))
    tracks: dict[str, Any] = {}
    for row in _list(fields.get("tracks")):
        if not isinstance(row, Mapping) or not row.get("guid"):
            continue
        tracks[str(row["guid"])] = {
            "state": {key: row.get(key) for key in _TRACK_FIELDS},
            "fx": _fx_fact_rows(_list(row.get("fx"))),
            "sends": [_value(send) for send in _list(row.get("sends"))],
            "hardware_outputs": [_value(output)
                                 for output in _list(row.get("hardware_outputs"))],
        }
    fx_state: dict[str, Any] = {}
    for row in _list(_mapping(fields.get("fx_state")).get("tracks")):
        if isinstance(row, Mapping) and row.get("track_guid"):
            chunk = row.get("chunk")
            fx_state[str(row["track_guid"])] = {
                "complete": row.get("complete") is True,
                "sha256": _sha(chunk) if isinstance(chunk, str) else None,
                "fx_count": row.get("fx_count"),
            }
    items: dict[str, Any] = {}
    for row in _list(fields.get("items")):
        if not isinstance(row, Mapping) or not row.get("guid"):
            continue
        chunk = row.get("state_chunk")
        items[str(row["guid"])] = {
            "track_guid": row.get("track_guid"),
            "fields": {key: row.get(key) for key in _ITEM_FIELDS},
            "stretch_marker_positions": _value(row.get("stretch_marker_positions")),
            "state_chunk_sha256": _sha(chunk) if isinstance(chunk, str) else None,
            "state_chunk_complete": row.get("state_chunk_complete") is True,
        }
    master_row = _mapping(fields.get("master"))
    master = None
    if master_row:
        master = {
            "guid": master_row.get("guid"),
            "state": {key: master_row.get(key) for key in _TRACK_FIELDS},
            "fx": _fx_fact_rows(_list(master_row.get("fx"))),
            "sends": [_value(send) for send in _list(master_row.get("sends"))],
            "hardware_outputs": [_value(output)
                                 for output in _list(master_row.get("hardware_outputs"))],
        }
    automation: list[dict[str, Any]] = []
    seen: set[str] = set()
    automation_field = _mapping(fields.get("automation"))
    envelope_field = _mapping(fields.get("envelopes"))
    for domain, container, group, nested in (
        ("automation", automation_field, "tracks", "envelopes"),
        ("automation", automation_field, "fx", None),
        ("automation", automation_field, "items", None),
        ("automation", automation_field, "master", None),
        ("envelope", envelope_field, "tracks", "envelopes"),
        ("envelope", envelope_field, "fx", None),
        ("envelope", envelope_field, "items", None),
        ("envelope", envelope_field, "master", None),
    ):
        for entry in _list(container.get(group)):
            if not isinstance(entry, Mapping):
                continue
            rows = entry.get(nested) if nested else [entry]
            for envelope in _list(rows):
                if not isinstance(envelope, Mapping) or not envelope.get("guid"):
                    continue
                guid = str(envelope["guid"])
                if guid in seen:
                    continue
                seen.add(guid)
                automation.append({
                    "guid": guid,
                    "domain": domain,
                    "owner_guid": entry.get("track_guid") or envelope.get("track_guid"),
                    "sha256": _sha({key: envelope.get(key) for key in
                                    ("guid", "name", "chunk", "active", "armed",
                                     "points", "automation_items")}),
                    "point_count": envelope.get("point_count"),
                    "automation_item_count": envelope.get("automation_item_count"),
                })
    project = _mapping(fields.get("project"))
    settings = _mapping(project.get("settings"))
    return {
        "schema": FACTS_SCHEMA,
        "available": True,
        "digest": {
            "version": digest.get("digest_version"),
            "change_count": digest.get("change_count"),
            "coverage": dict(_mapping(digest.get("coverage"))),
            "missing": sorted(str(name) for name in _list(digest.get("missing"))),
        },
        "tracks": tracks,
        "items": items,
        "master": master,
        "automation": sorted(automation, key=lambda row: row["guid"]),
        "tempo_sha256": _sha(_list(fields.get("tempo"))),
        "project_sha256": _sha(project),
        "project_settings": {str(key): _value(value)
                             for key, value in settings.items()},
        "fx_state": fx_state,
    }


def _object_delta(before: Mapping[str, Any],
                  after: Mapping[str, Any]) -> dict[str, Any]:
    """Compare two ``digest_facts`` projections at object and field level."""
    before_tracks = _mapping(before.get("tracks"))
    after_tracks = _mapping(after.get("tracks"))
    before_items = _mapping(before.get("items"))
    after_items = _mapping(after.get("items"))
    before_master = _mapping(before.get("master"))
    after_master = _mapping(after.get("master"))

    changed: list[dict[str, Any]] = []
    added: list[str] = sorted(set(after_tracks) - set(before_tracks))
    deleted: list[str] = sorted(set(before_tracks) - set(after_tracks))
    for guid in sorted(set(before_tracks) & set(after_tracks)):
        left, right = _mapping(before_tracks[guid]), _mapping(after_tracks[guid])
        fields = _field_diff(_mapping(left.get("state")), _mapping(right.get("state")),
                             _TRACK_FIELDS)
        fx = _fx_changes(_list(left.get("fx")), _list(right.get("fx")))
        sends = _route_changes(_list(left.get("sends")), _list(right.get("sends")))
        hardware = _route_changes(_list(left.get("hardware_outputs")),
                                  _list(right.get("hardware_outputs")))
        if fields or fx["added"] or fx["removed"] or fx["changed"] or sends or hardware:
            entry: dict[str, Any] = {"object_kind": "track", "id": guid}
            if fields:
                entry["fields"] = fields
            if fx["added"] or fx["removed"] or fx["changed"]:
                entry["fx"] = fx
            if sends:
                entry["sends"] = sends
            if hardware:
                entry["hardware_outputs"] = hardware
            changed.append(entry)
    for guid in sorted(set(before_items) | set(after_items)):
        left, right = _mapping(before_items.get(guid)), _mapping(after_items.get(guid))
        if not left:
            changed.append({"object_kind": "item", "id": guid, "change": "added",
                            "track_guid": right.get("track_guid")})
            continue
        if not right:
            changed.append({"object_kind": "item", "id": guid, "change": "deleted",
                            "track_guid": left.get("track_guid")})
            continue
        fields = _field_diff(_mapping(left.get("fields")), _mapping(right.get("fields")),
                             _ITEM_FIELDS)
        if left.get("stretch_marker_positions") != right.get("stretch_marker_positions"):
            fields.append({
                "field": "stretch_marker_positions",
                "before": _value(left.get("stretch_marker_positions")),
                "after": _value(right.get("stretch_marker_positions")),
            })
        if left.get("state_chunk_sha256") != right.get("state_chunk_sha256"):
            fields.append({"field": "state_chunk_sha256",
                           "before": left.get("state_chunk_sha256"),
                           "after": right.get("state_chunk_sha256")})
        if left.get("state_chunk_complete") != right.get("state_chunk_complete"):
            fields.append({"field": "state_chunk_complete",
                           "before": left.get("state_chunk_complete"),
                           "after": right.get("state_chunk_complete")})
        if fields:
            changed.append({"object_kind": "item", "id": guid,
                            "track_guid": right.get("track_guid"), "fields": fields})
    master_changes: dict[str, Any] = {}
    if before_master or after_master:
        fields = _field_diff(_mapping(before_master.get("state")),
                             _mapping(after_master.get("state")),
                             _TRACK_FIELDS)
        fx = _fx_changes(_list(before_master.get("fx")), _list(after_master.get("fx")))
        sends = _route_changes(_list(before_master.get("sends")),
                               _list(after_master.get("sends")))
        if fields:
            master_changes["fields"] = fields
        if fx["added"] or fx["removed"] or fx["changed"]:
            master_changes["fx"] = fx
        if sends:
            master_changes["sends"] = sends
        if master_changes:
            changed.append({"object_kind": "master", "id": "master", **master_changes})

    before_envelopes = _rows_by(before.get("automation") or [], "guid")
    after_envelopes = _rows_by(after.get("automation") or [], "guid")
    envelope_changes: list[dict[str, Any]] = []
    for guid in sorted(set(before_envelopes) | set(after_envelopes)):
        left, right = before_envelopes.get(guid), after_envelopes.get(guid)
        if left is None:
            target = right or {}
            envelope_changes.append({"object_kind": "envelope", "id": guid,
                                     "change": "added", "domain": target.get("domain"),
                                     "owner_guid": target.get("owner_guid"),
                                     "after_sha256": target.get("sha256")})
        elif right is None:
            envelope_changes.append({"object_kind": "envelope", "id": guid,
                                     "change": "deleted", "domain": left.get("domain"),
                                     "owner_guid": left.get("owner_guid"),
                                     "before_sha256": left.get("sha256")})
        elif left.get("sha256") != right.get("sha256"):
            envelope_changes.append({
                "object_kind": "envelope", "id": guid, "change": "modified",
                "domain": right.get("domain"), "owner_guid": right.get("owner_guid"),
                "before_sha256": left.get("sha256"),
                "after_sha256": right.get("sha256"),
                "point_counts": [left.get("point_count"), right.get("point_count")],
                "automation_item_counts": [left.get("automation_item_count"),
                                           right.get("automation_item_count")],
            })
        if len(envelope_changes) >= MAX_DELTA_OBJECTS:
            break
    changed.extend(envelope_changes)

    domains: list[dict[str, Any]] = []
    if before.get("tempo_sha256") != after.get("tempo_sha256"):
        domains.append({"domain": "tempo", "change": "modified",
                        "before_sha256": before.get("tempo_sha256"),
                        "after_sha256": after.get("tempo_sha256")})
    before_settings = _mapping(before.get("project_settings"))
    after_settings = _mapping(after.get("project_settings"))
    if before.get("project_sha256") != after.get("project_sha256"):
        domains.append({
            "domain": "project_settings", "change": "modified",
            "fields": [{"field": key,
                        "before": before_settings.get(key),
                        "after": after_settings.get(key)}
                       for key in sorted(set(before_settings) | set(after_settings))
                       if before_settings.get(key) != after_settings.get(key)][:128],
        })
    before_state = _mapping(before.get("fx_state"))
    after_state = _mapping(after.get("fx_state"))
    chunk_changes = []
    for guid in sorted(set(before_state) | set(after_state)):
        left = _mapping(before_state.get(guid))
        right = _mapping(after_state.get(guid))
        if left.get("sha256") != right.get("sha256") or \
                left.get("complete") != right.get("complete"):
            chunk_changes.append({
                "track_guid": guid,
                "before_sha256": left.get("sha256"),
                "after_sha256": right.get("sha256"),
                "complete": [left.get("complete"), right.get("complete")],
            })
    if chunk_changes:
        domains.append({"domain": "fx_state", "change": "modified",
                        "tracks": chunk_changes[:MAX_DELTA_OBJECTS]})
    return {
        "added": added,
        "changed": changed[:MAX_DELTA_OBJECTS],
        "deleted": deleted,
        "domains": domains,
        "counts": {"added": len(added), "changed": len(changed),
                   "deleted": len(deleted), "domains": len(domains),
                   "envelopes": len(envelope_changes)},
        "truncated": (len(changed) > MAX_DELTA_OBJECTS or
                      len(envelope_changes) >= MAX_DELTA_OBJECTS),
    }


def _source_guids(document: Mapping[str, Any]) -> dict[str, str]:
    """Map stable source id -> REAPER guid using the capture documents own rows."""
    result: dict[str, str] = {}
    for track in _list(document.get("tracks")):
        if isinstance(track, Mapping) and track.get("stable_id") and track.get("guid"):
            result[str(track["stable_id"])] = str(track["guid"])
    master = _mapping(document.get("master"))
    if master.get("stable_id") and master.get("guid"):
        result[str(master["stable_id"])] = str(master["guid"])
    return result


def _evidence_ids(document: Mapping[str, Any],
                  invalidated: Iterable[str]) -> dict[str, Any]:
    invalid = set(invalidated)
    audio_ids: list[str] = []
    analysis_by_source: dict[str, str] = {}
    for row in _list(document.get("analysis")):
        if isinstance(row, Mapping) and row.get("analysis_id"):
            analysis_by_source[str(row.get("source_guid") or "")] = str(row["analysis_id"])
    source_guids = _source_guids(document)
    for row in _list(document.get("audio")):
        if isinstance(row, Mapping) and str(row.get("source_id") or "") in invalid:
            if row.get("audio_id"):
                audio_ids.append(str(row["audio_id"]))
    analysis_ids: list[str] = []
    for source_id in sorted(invalid):
        guid = source_guids.get(source_id)
        if guid and guid in analysis_by_source:
            analysis_ids.append(analysis_by_source[guid])
    relation_ids: list[str] = []
    relations = _mapping(document.get("relations"))
    for row in _list(relations.get("relations")):
        if not isinstance(row, Mapping):
            continue
        endpoints = {str(row.get("from_id") or ""), str(row.get("to_id") or "")}
        if endpoints & invalid and row.get("relation_id"):
            relation_ids.append(str(row["relation_id"]))
    capped = len(relation_ids) > MAX_EVIDENCE_IDS
    return {
        "audio_ids": sorted(set(audio_ids)),
        "analysis_ids": sorted(set(analysis_ids)),
        "relation_ids": sorted(set(relation_ids))[:MAX_EVIDENCE_IDS],
        "relation_count": len(set(relation_ids)),
        "relation_ids_complete": not capped,
    }


def _build_after_manifest(*, project_id: str, document: Mapping[str, Any],
                          digest: Mapping[str, Any]) -> dict[str, Any]:
    source_guids = _source_guids(document)
    sources = []
    for row in _list(document.get("audio")):
        if not isinstance(row, Mapping):
            continue
        source_id = str(row.get("source_id") or "")
        guid = source_guids.get(source_id)
        if not guid:
            continue
        sources.append({
            "source_guid": guid,
            "scope": "master" if row.get("is_master") else "source",
            "stage": _mapping(row.get("render_provenance")).get("stage"),
        })
    stored = _mapping(document.get("render_manifest"))
    built = build_render_manifest(
        project_id=project_id,
        digest=digest,
        sources=sources,
        time_window=_mapping(stored.get("window")),
        render_contract=_mapping(stored.get("render_contract")),
    )
    # Compare in the panorama's stable-id space, exactly like the stored base
    # manifest.  Comparing GUID-keyed rows against stable ids would look like
    # "everything removed" and invalidate far more than the change requires.
    return project_render_manifest(
        built, {guid: source_id for source_id, guid in source_guids.items()})


def build_structural_delta(*, kind: str, project_id: str, base: Mapping[str, Any],
                           target: Mapping[str, Any],
                           base_document: Mapping[str, Any],
                           before_facts: Mapping[str, Any],
                           after_facts: Mapping[str, Any],
                           after_digest: Mapping[str, Any] | None,
                           scope: str,
                           operation_facts: Sequence[Mapping[str, Any]] | None = None,
                           readback_complete: bool,
                           readback_missing: Sequence[str] = (),
                           target_digest_evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build the factual delta for one acknowledged mutation.

    ``base`` carries ``context_token``, ``capture_id``, ``revision``,
    ``fact_root``, ``view_root`` and ``document_hash``.  ``target`` carries the
    post-mutation ``revision`` and optionally its ``digest_fingerprint``.
    ``scope`` is ``"known"`` only when the caller mapped the operation to a
    bounded set of objects; every raw API/Action mutation is ``"unknown"``.
    """
    if scope not in {"known", "unknown"}:
        raise ValueError("scope must be known or unknown")
    facts = []
    for fact in list(operation_facts or [])[:MAX_OPERATION_FACTS]:
        if isinstance(fact, Mapping):
            facts.append({
                "operation_index": fact.get("operation_index"),
                "kind": fact.get("kind"),
                "target_guid": fact.get("target_guid"),
                "before": _value(fact.get("before")),
                "after": _value(fact.get("after")),
                "changes_state": fact.get("changes_state"),
            })
    objects = _object_delta(_mapping(before_facts), _mapping(after_facts))
    base_manifest = _mapping(base_document.get("render_manifest"))
    after_manifest: dict[str, Any] = {"sources": [], "complete": False}
    manifest_error: str | None = None
    if isinstance(after_digest, Mapping):
        try:
            after_manifest = _build_after_manifest(
                project_id=project_id, document=base_document, digest=after_digest)
        except Exception as exc:  # noqa: BLE001 - a failed build is a coverage fact
            manifest_error = f"after_manifest_unavailable:{type(exc).__name__}"
    else:
        manifest_error = "after_digest_unavailable"

    if scope == "unknown" or manifest_error is not None:
        render = {
            "scope": "unknown",
            "authoritative": False,
            "unchanged": [],
            "changed": [],
            "added": [],
            "removed": [],
            "component_changes": {},
            "reasons": ([manifest_error] if manifest_error else []) +
                       [("unknown_mutation_scope" if scope == "unknown" else "")],
            "unverified": [],
            "freshness": "unknown",
            "complete": False,
        }
        render["reasons"] = [reason for reason in render["reasons"] if reason]
    else:
        render = compare_render_manifests(base_manifest, after_manifest)

    master_id = None
    for row in _list(base_manifest.get("sources")):
        if isinstance(row, Mapping) and row.get("scope") == "master":
            master_id = str(row.get("source_id") or "") or None
            break
    if scope == "unknown" or not render.get("authoritative"):
        invalidated_sources = sorted(_source_guids(base_document))
        fresh_sources: list[str] = []
        state = "stale"
        reasons = ["all captured audio invalidated: " +
                   (", ".join(render.get("reasons") or []) or "dependency proof unavailable")]
    else:
        invalidated_sources = sorted(set(render.get("changed") or []) |
                                     set(render.get("removed") or []))
        fresh_sources = sorted(set(render.get("unchanged") or []) - set(invalidated_sources))
        state = "stale" if invalidated_sources else "current"
        reasons = []
        if objects["changed"] or objects["domains"]:
            reasons.append("structural_change")
    evidence = _evidence_ids(base_document, invalidated_sources)
    audio_evidence = {
        "state": state,
        "base_capture_id": base.get("capture_id"),
        "invalidated_sources": invalidated_sources,
        "fresh_sources": fresh_sources,
        "invalidated_audio_ids": evidence["audio_ids"],
        "invalidated_analysis_ids": evidence["analysis_ids"],
        "invalidated_relation_ids": evidence["relation_ids"],
        "relation_count": evidence["relation_count"],
        "relation_ids_complete": evidence["relation_ids_complete"],
        "reasons": reasons,
        "coverage": {
            "authoritative": bool(render.get("authoritative")),
            "render_manifest_base": bool(base_manifest.get("sources")),
            "render_manifest_target": bool(after_manifest.get("sources")),
            "unverified": _list(base_manifest.get("unverified")),
        },
    }
    invalidations = [{
        "kind": "audio_and_analysis",
        "scope": "all_project_audio" if state == "stale" and not render.get("authoritative")
                 else "named_sources",
        "reason": ("unknown_mutation_scope" if scope == "unknown"
                   else "unverified_cross_capture_dependency"
                   if render.get("unverified")
                   else "render_dependency_changed" if invalidated_sources
                   else "no_render_dependency_changed"),
        "sources": invalidated_sources if render.get("authoritative") else None,
        "evidence_ids": evidence if render.get("authoritative") else None,
        "complete": bool(render.get("authoritative")),
        "requires_recapture": bool(invalidated_sources),
        "note": ("Old audio remains an immutable historical capture fact; it no longer "
                 "describes this revision. Capture again before interpreting project audio."),
    }]
    identity = _mapping(base.get("revision"))
    payload: dict[str, Any] = {
        "schema_version": DELTA_SCHEMA,
        "kind": kind,
        "project_id": project_id,
        "instance_id": identity.get("instance_id"),
        "epoch": identity.get("epoch"),
        "scope": scope,
        "base": dict(base),
        "target": dict(target),
        "operation_facts": facts,
        "objects": objects,
        "render": render,
        "audio_evidence": audio_evidence,
        "invalidations": invalidations,
        "readback": {
            "complete": bool(readback_complete),
            "missing": sorted({str(item) for item in readback_missing}),
            "digest_evidence": dict(target_digest_evidence or {}),
            "manifest_error": manifest_error,
        },
        "acknowledgeable": (bool(readback_complete) and not objects["truncated"] and
                            bool(audio_evidence["relation_ids_complete"])),
    }
    return payload
