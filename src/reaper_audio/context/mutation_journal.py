"""Post-hoc change journal for generic mutations — Context Contract v2 §4.11.

`api_call` and `action` cannot declare a scope: a raw ReaScript call may touch
one FX parameter or every track.  Instead of declaring "everything is stale",
the server takes the project digest before the call and after it, builds two
render manifests and names what actually moved.

Honesty rules:

* only **known** components are compared; the manifest's declared unverified
  classes (media file bytes, plugin binary version, project default pan law,
  external inputs, unexposed routing flags) are project-global and are listed,
  never silently promoted to "unchanged";
* a source whose render key or component hashes changed is ``changed`` — the
  caller may treat only those as dirty;
* equal keys mean "these known components did not change", not a certificate
  for the unverified classes.

No threshold, no heuristic and no inference about *why* a component moved.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .render_dependencies import build_render_manifest


def _rows(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for row in manifest.get("sources") or []:
        if not isinstance(row, Mapping) or not row.get("source_guid"):
            continue
        scope = "master" if row.get("scope") == "master" else "track"
        out[f"{scope}:{row['source_guid']}"] = row
    return out


def _unverified(manifest: Mapping[str, Any]) -> list[str]:
    classes = {str(item.get("class")) for item in manifest.get("unverified") or []
               if isinstance(item, Mapping) and item.get("class")}
    return sorted(classes)


def diff_digests(*, project_id: str,
                 before_digest: Mapping[str, Any],
                 after_digest: Mapping[str, Any],
                 track_guids: Sequence[str],
                 master_guid: str,
                 time_window: Mapping[str, Any],
                 render_contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Exact known-component dirty set between two project digests."""
    before = build_render_manifest(project_id=project_id, digest=before_digest,
                                   sources=[{"source_guid": guid, "scope": "source"}
                                            for guid in track_guids] +
                                           [{"source_guid": master_guid, "scope": "master"}],
                                   time_window=time_window,
                                   render_contract=render_contract)
    after = build_render_manifest(project_id=project_id, digest=after_digest,
                                  sources=[{"source_guid": guid, "scope": "source"}
                                           for guid in track_guids] +
                                          [{"source_guid": master_guid, "scope": "master"}],
                                  time_window=time_window,
                                  render_contract=render_contract)
    before_rows, after_rows = _rows(before), _rows(after)
    changed: list[str] = []
    unchanged: list[str] = []
    component_changes: dict[str, list[str]] = {}
    for source_id in sorted(set(before_rows) | set(after_rows)):
        old, new = before_rows.get(source_id), after_rows.get(source_id)
        if old is None or new is None:
            changed.append(source_id)
            component_changes[source_id] = ["added" if old is None else "removed"]
            continue
        old_components = dict(old.get("components") or {})
        new_components = dict(new.get("components") or {})
        diff_keys = sorted(key for key in set(old_components) | set(new_components)
                           if old_components.get(key) != new_components.get(key))
        if old.get("render_key") != new.get("render_key") or diff_keys:
            changed.append(source_id)
            if diff_keys:
                component_changes[source_id] = diff_keys
        elif old.get("complete") and new.get("complete"):
            unchanged.append(source_id)
        else:
            # An incomplete row proves nothing: treat as changed, not clean.
            changed.append(source_id)
            component_changes[source_id] = ["incomplete_fingerprint"]
    unverified = sorted(set(_unverified(before)) | set(_unverified(after)))
    return {
        "schema": "reaper-workbench.change-journal/1",
        "scope": "known_components",
        "changed": changed,
        "unchanged": unchanged,
        "component_changes": component_changes,
        "unverified_classes": unverified,
        "dirty_sources": changed,
        "note": ("known components only; the unverified classes listed above are "
                 "project-global and are not certified by equal keys"),
    }


__all__ = ["diff_digests"]
