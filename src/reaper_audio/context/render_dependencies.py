"""Deterministic render identity and dependency sets for captured audio.

A container hash is not a render identity: REAPER writes fresh PCM (and fresh
container metadata) on every render, so 43 of 43 hashes can differ for
identical project content.  This module answers the question a freshness gate
actually needs: *would the same project state produce the same signal at this
node?*

The answer is a canonical hash of every render-affecting dependency the bridge
digest can read, grouped into components::

    track state (level, pan, mute, solo, channel count, pan mode, folder depth)
    items       (normalized item state chunk: geometry, fades, take, stretch,
                 take FX, source identity)
    fx          (ordered chain identity, bypass/offline, every parameter)
    fx_state    (normalized <FXCHAIN> text: opaque plugin state)
    automation  (every envelope, point, automation item and FX envelope)
    routing     (sends, hardware outputs)
    master      (master state, FX, FX state, automation)
    tempo       (the tempo/time-signature map)
    project     (render/playback settings that change what is produced)

The source closure follows the signal path: a captured track stem includes the
track, every sender that feeds it, every child of a folder it owns, every
ancestor folder that processes it, the master chain (the capture renders
``selected tracks via master``) and the project-level render settings.  The
master mix includes every track.

Completeness is explicit.  ``complete`` means every dependency the digest
contract covers was read for every source.  ``unverified`` names dependency
classes outside that contract (media file bytes on disk, plugin binaries,
project default pan law, external hardware, unexposed routing flags); they are
declared, never silently treated as equal.  An authoritative cross-capture
verdict requires both manifests to be complete **and** to declare no unresolved
dependency classes.  A fresh capture remains valid evidence for its own
revision; the uncertainty only prevents it from being called unchanged after a
later revision.  Otherwise the caller invalidates affected audio conservatively.

Nothing here renders, reads audio or talks to REAPER.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .errors import ContractValidationError
from .revision import digest_evidence

__all__ = [
    "MANIFEST_SCHEMA",
    "RENDER_KEY_PREFIX",
    "build_render_manifest",
    "compare_render_manifests",
    "manifest_by_source",
    "project_render_manifest",
    "render_key_for",
]

MANIFEST_SCHEMA = "reaper-workbench.render-manifest/1"
RENDER_KEY_PREFIX = "rk1:"
RENDER_KEY_VERSION = 1

#: Track fields that change what the track produces.
_TRACK_STATE_KEYS = (
    "volume", "pan", "mute", "solo", "channel_count", "pan_mode", "pan_law",
    "pan_law_flags", "width", "dualpan_left", "dualpan_right", "phase_inverted",
    "main_send", "play_offset", "play_offset_flags", "freeze_count", "folder_depth",
)

#: Item fields carried as explicit facts in the panorama.  The digest item row
#: also carries a normalized state chunk, which is what the render key hashes.
_ITEM_KEYS = (
    "position_s", "length_s", "start_offset_s", "playrate", "mute",
    "fade_in_s", "fade_out_s", "take_guid", "source_path", "loop_source",
    "beat_attach_mode", "channel_mode", "group_id", "stretch_marker_positions",
)

_UNVERIFIED_DEPENDENCIES = (
    {
        "class": "media_file_content",
        "reason": ("the digest proves source paths and item state, not the bytes of the "
                   "media files on disk; a file replaced under the same path is not "
                   "detected by a project-state digest"),
        "policy": "cross_capture_comparison_unknown",
        "scope": "all_project_audio",
    },
    {
        "class": "external_realtime_input",
        "reason": ("live inputs, hardware monitoring and external clock are outside the "
                   "project-state digest"),
        "policy": "cross_capture_comparison_unknown",
        "scope": "all_project_audio",
    },
    {
        "class": "project_default_pan_law",
        "reason": ("per-track D_PANLAW is read, but a value of -1 means \"project "
                   "default\" and REAPER exposes no documented GetSetProjectInfo key for "
                   "the project pan law; changing only the project default cannot be "
                   "seen by this digest"),
        "policy": "cross_capture_comparison_unknown",
        "scope": "all_project_audio",
    },
    {
        "class": "plugin_binary_version",
        "reason": ("FX identity, bypass, parameter values and serialized chain state are "
                   "hashed, but REAPER exposes no version/model string for the loaded "
                   "plugin binary; replacing a plugin binary under the same identity is "
                   "outside the digest"),
        "policy": "cross_capture_comparison_unknown",
        "scope": "all_project_audio",
    },
    {
        "class": "unexposed_track_routing_flags",
        "reason": ("FX pin mappings, per-track C_BEATATTACHMODE timebase and fixed-lane "
                   "C_LANEPLAYS/C_ALLLANESPLAY playback flags are not read as numeric "
                   "digest fields; the serialized FX chain may include pin mappings but "
                   "that coverage is not verified, so these are declared rather than "
                   "assumed irrelevant"),
        "policy": "cross_capture_comparison_unknown",
        "scope": "all_project_audio",
    },
)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _rows_by_guid(rows: Iterable[Any], key: str = "guid") -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if isinstance(row, Mapping) and isinstance(row.get(key), str) and row[key]:
            result[str(row[key])] = row
    return result


def _folder_relations(tracks: Sequence[Mapping[str, Any]]) -> tuple[dict[str, str | None],
                                                                   dict[str, list[str]]]:
    """Recover folder nesting from ordered ``folder_depth`` values.

    REAPER's ``I_FOLDERDEPTH`` is a delta: a positive value opens a folder
    span, a negative value closes it at the end of that track.  The parent of a
    track is the innermost open folder at its position; the children of a
    folder are every track inside its span.
    """
    parents: dict[str, str | None] = {}
    children: dict[str, list[str]] = {}
    stack: list[str] = []
    for row in tracks:
        guid = row.get("guid")
        if not isinstance(guid, str) or not guid:
            continue
        parents[guid] = stack[-1] if stack else None
        if stack:
            children.setdefault(stack[-1], []).append(guid)
        depth = row.get("folder_depth")
        try:
            depth_value = int(depth) if depth is not None else 0
        except (TypeError, ValueError):
            depth_value = 0
        if depth_value > 0:
            stack.append(guid)
        elif depth_value < 0:
            for _ in range(min(abs(depth_value), len(stack))):
                stack.pop()
    for guid in parents:
        children.setdefault(guid, [])
    return parents, children


def _item_payload(item: Mapping[str, Any]) -> dict[str, Any]:
    payload = {key: item.get(key) for key in _ITEM_KEYS}
    payload["take_volume"] = item.get("take_volume")
    payload["take_pan"] = item.get("take_pan")
    payload["take_pitch"] = item.get("take_pitch")
    payload["preserve_pitch"] = item.get("preserve_pitch")
    chunk = item.get("state_chunk")
    payload["state_chunk_sha256"] = _sha(chunk) if isinstance(chunk, str) else None
    payload["state_chunk_complete"] = item.get("state_chunk_complete") is True
    return payload


def _automation_index(automation: Any) -> dict[str, list[Mapping[str, Any]]]:
    """Assign every envelope row to the track that owns its signal path."""
    index: dict[str, list[Mapping[str, Any]]] = {}
    if not isinstance(automation, Mapping):
        return index

    def add(owner: Any, row: Mapping[str, Any], master: bool = False) -> None:
        if master:
            index.setdefault("__master__", []).append(row)
            return
        if isinstance(owner, str) and owner:
            index.setdefault(owner, []).append(row)

    for row in _as_list(automation.get("tracks")):
        if isinstance(row, Mapping):
            for envelope in _as_list(row.get("envelopes")):
                if isinstance(envelope, Mapping):
                    add(row.get("track_guid"), envelope)
    for row in _as_list(automation.get("fx")):
        if isinstance(row, Mapping):
            add(row.get("track_guid"), row, master=bool(row.get("is_master")))
    for row in _as_list(automation.get("items")):
        if isinstance(row, Mapping):
            add(row.get("track_guid"), row, master=bool(row.get("is_master")))
    for row in _as_list(automation.get("master")):
        if isinstance(row, Mapping):
            index.setdefault("__master__", []).append(row)
    return index


def _sort_rows(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(rows, key=lambda row: str(row.get("guid") or row.get("index") or ""))


def _base_components(*, digest: Mapping[str, Any]) -> dict[str, Any]:
    """Split one digest into per-object and per-domain dependency hashes."""
    fields = _as_mapping(digest.get("fields"))
    tracks = [row for row in _as_list(fields.get("tracks")) if isinstance(row, Mapping)]
    items = [row for row in _as_list(fields.get("items")) if isinstance(row, Mapping)]
    master = fields.get("master") if isinstance(fields.get("master"), Mapping) else None
    fx_state = _as_mapping(fields.get("fx_state"))
    chunks = {
        str(row.get("track_guid")): row
        for row in _as_list(fx_state.get("tracks"))
        if isinstance(row, Mapping) and row.get("track_guid")
    }
    automation = _automation_index(fields.get("automation"))

    items_by_track: dict[str, list[Mapping[str, Any]]] = {}
    for row in items:
        track_guid = row.get("track_guid")
        if isinstance(track_guid, str) and track_guid:
            items_by_track.setdefault(track_guid, []).append(row)

    track_components: dict[str, dict[str, Any]] = {}
    for row in tracks:
        guid = row.get("guid")
        if not isinstance(guid, str) or not guid:
            continue
        chunk = chunks.get(guid)
        chunk_complete = bool(chunk and chunk.get("complete") is True)
        state = {key: row.get(key) for key in _TRACK_STATE_KEYS}
        track_items = items_by_track.get(guid, [])
        track_components[guid] = {
            "track_state": _sha(state),
            "items": _sha([_item_payload(item) for item in
                           sorted(track_items, key=lambda item: str(item.get("guid") or ""))]),
            "fx": _sha(_as_list(row.get("fx"))),
            "fx_state": _sha({
                "complete": chunk_complete,
                "chunk": (chunk or {}).get("chunk") if chunk_complete else None,
            }),
            "automation": _sha(_sort_rows(automation.get(guid, []))),
            "routing": _sha({
                "sends": _as_list(row.get("sends")),
                "hardware_outputs": _as_list(row.get("hardware_outputs")),
            }),
            "item_count": len(track_items),
            "fx_count": len(_as_list(row.get("fx"))),
            "fx_state_complete": chunk_complete,
            "items_complete": all(item.get("state_chunk_complete") is True
                                  for item in track_items),
        }

    master_chunk = next((row for guid, row in chunks.items()
                         if row.get("is_master") is True), None)
    master_complete = bool(master_chunk and master_chunk.get("complete") is True)
    master_component = None
    if master is not None:
        master_component = {
            "track_state": _sha({key: master.get(key) for key in _TRACK_STATE_KEYS}),
            "fx": _sha(_as_list(master.get("fx"))),
            "fx_state": _sha({
                "complete": master_complete,
                "chunk": (master_chunk or {}).get("chunk") if master_complete else None,
            }),
            "automation": _sha(_sort_rows(automation.get("__master__", []))),
            "routing": _sha({
                "sends": _as_list(master.get("sends")),
                "hardware_outputs": _as_list(master.get("hardware_outputs")),
            }),
            "fx_count": len(_as_list(master.get("fx"))),
            "fx_state_complete": master_complete,
        }

    project_settings = _as_mapping(fields.get("project"))
    return {
        "tracks": track_components,
        "master": master_component,
        "master_guid": str(master.get("guid")) if master and master.get("guid") else None,
        "tempo": _sha(_as_list(fields.get("tempo"))),
        "project": _sha(project_settings),
        "items_complete": all(
            component["items_complete"] for component in track_components.values()),
        "fx_state_complete": all(
            component["fx_state_complete"] for component in track_components.values()),
        "unavailable": [],
    }


def _dependency_closure(track_guid: str, *, components: Mapping[str, Any],
                        parents: Mapping[str, str | None],
                        children: Mapping[str, list[str]],
                        senders: Mapping[str, list[str]]) -> dict[str, list[str]]:
    """Resolve the actual signal path around one track."""
    own: set[str] = {track_guid}
    senders_of: set[str] = set()
    frontier = [track_guid]
    while frontier:
        current = frontier.pop()
        for sender in senders.get(current, []):
            if sender not in own:
                senders_of.add(sender)
                own.add(sender)
                frontier.append(sender)
    descendants: set[str] = set()

    def walk(node: str) -> None:
        for child in children.get(node, []):
            if child not in descendants:
                descendants.add(child)
                walk(child)

    walk(track_guid)
    ancestors: list[str] = []
    parent = parents.get(track_guid)
    while parent:
        ancestors.append(parent)
        parent = parents.get(parent)
    return {
        "track": sorted(own),
        "senders": sorted(senders_of),
        "children": sorted(descendants),
        "ancestors": ancestors,
    }


def _source_components(*, scope: str, track_guid: str | None,
                       components: Mapping[str, Any],
                       parents: Mapping[str, str | None],
                       children: Mapping[str, list[str]],
                       senders: Mapping[str, list[str]]) -> tuple[list[str], dict[str, str], list[str]]:
    """Component hashes for one render node plus the reasons that make it weak."""
    unknown: list[str] = []
    digests: dict[str, str] = {}
    track_components: Mapping[str, Any] = components["tracks"]
    if scope == "master":
        dependency_guids = sorted(track_components)
        for guid in dependency_guids:
            row = track_components[guid]
            digests[f"track:{guid}:track_state"] = row["track_state"]
            digests[f"track:{guid}:items"] = row["items"]
            digests[f"track:{guid}:fx"] = row["fx"]
            digests[f"track:{guid}:fx_state"] = row["fx_state"]
            digests[f"track:{guid}:automation"] = row["automation"]
            digests[f"track:{guid}:routing"] = row["routing"]
        master = components.get("master")
        if not isinstance(master, Mapping):
            unknown.append("master_state_unreadable")
        else:
            for key in ("track_state", "fx", "fx_state", "automation", "routing"):
                digests[f"master:{key}"] = master[key]
    else:
        if track_guid is None or track_guid not in track_components:
            return [], {}, ["track_state_unreadable"]
        closure = _dependency_closure(track_guid, components=components,
                                      parents=parents, children=children,
                                      senders=senders)
        for guid in closure["track"]:
            row = track_components[guid]
            role = ("self" if guid == track_guid
                    else "sender" if guid in closure["senders"]
                    else "child" if guid in closure["children"]
                    else "identity")
            for key in ("track_state", "items", "fx", "fx_state", "automation", "routing"):
                digests[f"track:{guid}:{role}:{key}"] = row[key]
        for guid in closure["ancestors"]:
            row = track_components.get(guid)
            if row is None:
                unknown.append(f"ancestor_state_unreadable:{guid}")
                continue
            for key in ("track_state", "fx", "fx_state", "automation"):
                digests[f"track:{guid}:ancestor:{key}"] = row[key]
        master = components.get("master")
        if not isinstance(master, Mapping):
            unknown.append("master_state_unreadable")
        else:
            # The capture renders selected tracks via the master chain.
            for key in ("track_state", "fx", "fx_state", "automation"):
                digests[f"master:{key}"] = master[key]
        dependency_guids = sorted(closure["track"])
    digests["tempo"] = components["tempo"]
    digests["project"] = components["project"]
    if not components.get("items_complete", False):
        unknown.append("item_state_chunk_unreadable")
    if not components.get("fx_state_complete", False):
        unknown.append("fx_chain_state_unreadable")
    return dependency_guids, digests, unknown


def render_key_for(*, scope: str, stage: str | None, window: Mapping[str, Any],
                   components: Mapping[str, str], complete: bool,
                   unknown: Sequence[str]) -> str:
    """Canonical key over the ordered component hashes and the render contract."""
    payload = {
        "v": RENDER_KEY_VERSION,
        "scope": scope,
        "stage": stage,
        "window": dict(window),
        "components": dict(sorted(components.items())),
        "complete": bool(complete),
        "unknown": sorted(set(unknown)),
    }
    return RENDER_KEY_PREFIX + hashlib.sha256(_canonical(payload)).hexdigest()


def _unverified_classes(manifest: Mapping[str, Any]) -> list[str]:
    """Return dependency classes this manifest explicitly did not verify.

    A manifest that predates the declaration is not treated as fully verified:
    absence of ``unverified`` is itself an unresolved fact.  An explicitly empty
    list plus ``cross_revision_authoritative: true`` is the only case that can
    permit an authoritative cross-capture result.
    """
    if "unverified" not in manifest:
        return ["manifest_missing_unverified_declaration"]
    entries = manifest.get("unverified")
    if entries is None:
        return ["manifest_missing_unverified_declaration"]
    if not isinstance(entries, (list, tuple)):
        return ["manifest_unverified_declaration_malformed"]
    classes: set[str] = set()
    for entry in entries:
        if isinstance(entry, Mapping):
            name = entry.get("class")
            if isinstance(name, str) and name:
                classes.add(name)
            else:
                classes.add("manifest_unverified_entry_malformed")
        elif isinstance(entry, str) and entry:
            classes.add(entry)
        else:
            classes.add("manifest_unverified_entry_malformed")
    if manifest.get("cross_revision_authoritative") is False and not classes:
        classes.add("manifest_declares_cross_revision_non_authoritative")
    if manifest.get("cross_revision_authoritative") is not True and not classes:
        classes.add("manifest_missing_cross_revision_declaration")
    return sorted(classes)


def build_render_manifest(*, project_id: str, digest: Mapping[str, Any],
                          sources: Sequence[Mapping[str, Any]],
                          time_window: Mapping[str, Any],
                          render_contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build one deterministic render manifest for a captured project state.

    ``sources`` entries need ``source_guid``, ``scope`` (``"source"`` or
    ``"master"``) and optionally ``stage``, ``channels`` and ``sample_rate``.
    The returned manifest is hash-bound but not itself a capture record; the
    caller stores it in ``project_map.render_manifest`` so record integrity
    covers it.
    """
    if not isinstance(project_id, str) or not project_id:
        raise ContractValidationError("project_id must be a non-empty string",
                                      field="project_id")
    evidence = digest_evidence(project_id, digest)
    fields = _as_mapping(digest.get("fields"))
    tracks = [row for row in _as_list(fields.get("tracks")) if isinstance(row, Mapping)]
    parents, children = _folder_relations(tracks)
    senders: dict[str, list[str]] = {}
    for row in tracks:
        guid = row.get("guid")
        if not isinstance(guid, str) or not guid:
            continue
        for send in _as_list(row.get("sends")):
            if isinstance(send, Mapping):
                destination = send.get("dest_guid")
                if isinstance(destination, str) and destination:
                    senders.setdefault(destination, []).append(guid)
    components = _base_components(digest=digest)
    window = {
        "project_start_s": time_window.get("project_start_s"),
        "project_end_s": time_window.get("project_end_s"),
    }
    contract = dict(render_contract or {})
    rows: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        guid = source.get("source_guid")
        scope = str(source.get("scope") or "source")
        if scope not in {"source", "master"}:
            raise ContractValidationError("render manifest scope must be source or master",
                                          field="scope")
        if not isinstance(guid, str) or not guid:
            rows.append({
                "source_guid": None, "scope": scope, "stage": source.get("stage"),
                "render_key": None, "complete": False,
                "dependency_guids": [], "components": {},
                "unknown": ["source_identity_missing"],
            })
            continue
        dependency_guids, digests, unknown = _source_components(
            scope=scope,
            track_guid=None if scope == "master" else guid,
            components=components,
            parents=parents,
            children=children,
            senders=senders,
        )
        removed = scope != "master" and not dependency_guids
        if removed:
            # A captured track that no longer exists in this digest is a known
            # fact, not an unreadable one: the source row stays comparable and
            # its evidence is invalidated because the node is gone.
            unknown = []
            digests = {}
        if evidence.scope != "full":
            unknown = sorted(set(unknown) | {f"digest_coverage:{name}"
                                             for name in evidence.missing})
        row_contract = dict(contract)
        for key in ("channels", "sample_rate", "render_settings"):
            if source.get(key) is not None:
                row_contract[key] = source.get(key)
        complete = not unknown
        rows.append({
            "source_guid": guid,
            "scope": scope,
            "stage": source.get("stage"),
            "window": window,
            "render_contract": row_contract,
            "render_key": (None if removed else render_key_for(
                scope=scope, stage=source.get("stage"), window=window,
                components=digests, complete=complete, unknown=unknown)),
            "complete": complete or removed,
            "removed": bool(removed),
            "dependency_guids": dependency_guids,
            "components": dict(sorted(digests.items())),
            "unknown": unknown,
        })
    return {
        "schema": MANIFEST_SCHEMA,
        "version": 1,
        "project_id": project_id,
        "change_count": digest.get("change_count"),
        "window": window,
        "render_contract": contract,
        "digest": {
            "digest_version": digest.get("digest_version"),
            "fingerprint": evidence.fingerprint,
            "scope": evidence.scope,
            "missing": list(evidence.missing),
        },
        "unverified": [dict(item) for item in _UNVERIFIED_DEPENDENCIES],
        "cross_revision_authoritative": not _UNVERIFIED_DEPENDENCIES,
        "sources": rows,
        "complete": bool(rows) and all(row["complete"] for row in rows),
    }


def manifest_by_source(manifest: Mapping[str, Any],
                       id_for_guid: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """Index a manifest by source identity (stable id when one is supplied)."""
    result: dict[str, dict[str, Any]] = {}
    mapping = id_for_guid or {}
    for row in _as_list(manifest.get("sources")):
        if not isinstance(row, Mapping):
            continue
        guid = row.get("source_guid")
        if not isinstance(guid, str):
            guid = None
        explicit = row.get("source_id")
        source_id = (str(explicit) if isinstance(explicit, str) and explicit
                     else mapping.get(guid or "", guid or ""))
        if not source_id:
            continue
        result[source_id] = dict(row)
    return result


def project_render_manifest(manifest: Mapping[str, Any],
                            id_for_guid: Mapping[str, str]) -> dict[str, Any]:
    """Project a guid-keyed manifest into the panorama's stable-id space.

    The panorama never needs the raw GUIDs to compare keys: it carries the
    stable object ids, the key, the component hashes and the reasons a key is
    not authoritative.
    """
    rows: list[dict[str, Any]] = []
    for row in _as_list(manifest.get("sources")):
        if not isinstance(row, Mapping):
            continue
        guid = row.get("source_guid")
        source_id = id_for_guid.get(str(guid)) if isinstance(guid, str) else None
        if not source_id:
            continue
        rows.append({
            "source_id": source_id,
            "scope": row.get("scope"),
            "stage": row.get("stage"),
            "render_key": row.get("render_key"),
            "complete": bool(row.get("complete")),
            "removed": bool(row.get("removed")),
            "dependency_ids": [id_for_guid.get(str(dep), str(dep))
                               for dep in _as_list(row.get("dependency_guids"))],
            "components": dict(_as_mapping(row.get("components"))),
            "unknown": list(_as_list(row.get("unknown"))),
        })
    return {
        "schema": MANIFEST_SCHEMA,
        "version": 1,
        "window": dict(_as_mapping(manifest.get("window"))),
        "render_contract": dict(_as_mapping(manifest.get("render_contract"))),
        "digest": dict(_as_mapping(manifest.get("digest"))),
        "unverified": [dict(item) for item in _as_list(manifest.get("unverified"))],
        "cross_revision_authoritative": bool(
            manifest.get("cross_revision_authoritative", False)),
        "sources": rows,
        "by_source": {row["source_id"]: row for row in rows},
        "complete": bool(rows) and all(row["complete"] for row in rows),
    }


def compare_render_manifests(before: Mapping[str, Any],
                             after: Mapping[str, Any]) -> dict[str, Any]:
    """Compare two manifests and name exactly which captured nodes survive.

    The answer is authoritative only when both manifests are complete, declare
    ``cross_revision_authoritative: true``, and carry no unresolved dependency
    class.  A missing or malformed declaration fails closed.  With unresolved
    classes the known keys may be equal while media bytes, plugin binaries, the
    project pan law or unexposed routing flags changed outside the digest, so the
    result is ``scope="unknown"``, names no source as unchanged, and invalidates
    all project audio.  A missing source is ``removed``; a new source is
    ``added``; an equal key is ``unchanged`` only in the authoritative branch.
    Components that differ are named per source so the delta can say *why* a
    node was invalidated instead of repeating the whole list.
    """
    before_rows = manifest_by_source(before)
    after_rows = manifest_by_source(after)
    unresolved = sorted(set(_unverified_classes(before)) |
                        set(_unverified_classes(after)))
    if unresolved:
        # The known component hashes can be equal while an unverified dependency
        # (media bytes, plugin binary version, project default pan law, external
        # input, unexposed routing flags) changed outside the digest.  No source
        # may be called unchanged, and the manifest does not map those classes to
        # individual sources, so the only honest scope is all project audio.
        return {
            "scope": "unknown",
            "authoritative": False,
            "unchanged": [],
            "changed": [],
            "added": sorted(set(after_rows) - set(before_rows)),
            "removed": sorted(set(before_rows) - set(after_rows)),
            "component_changes": {},
            "unverified": unresolved,
            "freshness": "unknown",
            "reasons": [f"unverified_cross_capture_dependency:{name}"
                        for name in unresolved],
            "complete": False,
        }
    complete_before = bool(before.get("complete")) and bool(before_rows)
    complete_after = bool(after.get("complete")) and bool(after_rows)
    if not (complete_before and complete_after):
        reasons: list[str] = []
        if not complete_before:
            reasons.append("base_render_manifest_incomplete_or_missing")
        if not complete_after:
            reasons.append("target_render_manifest_incomplete")
        return {
            "scope": "unknown",
            "authoritative": False,
            "unchanged": [],
            "changed": [],
            "added": sorted(set(after_rows) - set(before_rows)),
            "removed": sorted(set(before_rows) - set(after_rows)),
            "component_changes": {},
            "unverified": [],
            "freshness": "unknown",
            "reasons": reasons,
            "complete": False,
        }
    unchanged: list[str] = []
    changed: list[str] = []
    component_changes: dict[str, list[str]] = {}
    for source_id, row in before_rows.items():
        target = after_rows.get(source_id)
        if target is None:
            continue
        if (row.get("render_key") == target.get("render_key") and
                row.get("complete") is True and target.get("complete") is True and
                row.get("removed") is not True and target.get("removed") is not True):
            unchanged.append(source_id)
            continue
        changed.append(source_id)
        if target.get("removed") is True:
            component_changes[source_id] = ["source_removed"]
            continue
        base_components = _as_mapping(row.get("components"))
        target_components = _as_mapping(target.get("components"))
        component_changes[source_id] = sorted(
            key for key in set(base_components) | set(target_components)
            if base_components.get(key) != target_components.get(key)
        ) or ["render_key"]
    return {
        "scope": "known",
        "authoritative": True,
        "unchanged": sorted(unchanged),
        "changed": sorted(changed),
        "added": sorted(set(after_rows) - set(before_rows)),
        "removed": sorted(set(before_rows) - set(after_rows)),
        "component_changes": component_changes,
        "unverified": [],
        "freshness": "known",
        "reasons": [],
        "complete": True,
    }
