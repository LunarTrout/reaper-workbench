"""Persisted per-plug-in summaries, bound to the state they describe.

A summary is cheap to read and expensive to build (it needs every parameter of
the plug-in).  It is therefore cached on disk and reused while the track's FX
chain is unchanged.  Validity is *not* a timestamp: the entry stores the hash of
the FX chain it was derived from, so a summary can never describe an older
state than the chain it claims.

An unstable chain (a plug-in that rewrites its own serialization on every read,
Kontakt 7 is one) has no stable chain hash.  Such entries are stored with
``chain_fingerprint = None`` and reported as ``unverified`` — never as current.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Mapping

PLUGIN_STATE_SCHEMA = "reaper-workbench.plugin-state-store/1"


def chain_fingerprint(chunk: str | None) -> str | None:
    if not isinstance(chunk, str) or not chunk:
        return None
    return "sha256:" + hashlib.sha256(chunk.encode("utf-8", "replace")).hexdigest()


def _key(track_guid: str, fx_guid: str) -> str:
    return f"{track_guid}|{fx_guid}"


class PluginStateStore:
    """One JSON file per project under ``<archive>/state/plugin_summaries``."""

    def __init__(self, archive_root: str | Path) -> None:
        self.root = Path(archive_root) / "state" / "plugin_summaries"
        self._lock = threading.Lock()

    def _path(self, project_id: str) -> Path:
        return self.root / (hashlib.sha1(project_id.encode("utf-8")).hexdigest() + ".json")

    def load(self, project_id: str) -> dict[str, Any]:
        path = self._path(project_id)
        if not path.is_file():
            return {"schema": PLUGIN_STATE_SCHEMA, "project_id": project_id,
                    "entries": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema": PLUGIN_STATE_SCHEMA, "project_id": project_id,
                    "entries": {}}
        if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
            return {"schema": PLUGIN_STATE_SCHEMA, "project_id": project_id,
                    "entries": {}}
        return data

    def save(self, project_id: str, data: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(project_id)
        tmp = path.with_suffix(".tmp")
        payload = {"schema": PLUGIN_STATE_SCHEMA, "project_id": project_id,
                   "entries": dict(data.get("entries") or {}),
                   "calibrations": dict(data.get("calibrations") or {})}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, path)

    def entries(self, project_id: str) -> dict[str, dict[str, Any]]:
        return dict(self.load(project_id).get("entries") or {})

    def calibrations(self, project_id: str) -> dict[str, dict[str, Any]]:
        return dict(self.load(project_id).get("calibrations") or {})

    def put_calibration(self, project_id: str, track_guid: str, fx_guid: str,
                        index: int, *, name: str,
                        table: list[dict[str, Any]]) -> None:
        """Store one measured normalized→formatted table for a parameter."""
        with self._lock:
            data = self.load(project_id)
            calibrations = dict(data.get("calibrations") or {})
            calibrations[f"{track_guid}|{fx_guid}|{index}"] = {
                "track_guid": track_guid, "fx_guid": fx_guid,
                "parameter_index": int(index), "name": name,
                "table": list(table),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            self.save(project_id, {"entries": dict(data.get("entries") or {}),
                                   "calibrations": calibrations})

    def get_calibration(self, project_id: str, track_guid: str,
                        fx_guid: str, index: int) -> dict[str, Any] | None:
        return self.calibrations(project_id).get(f"{track_guid}|{fx_guid}|{index}")

    def put(self, project_id: str, track_guid: str, fx_guid: str, *,
            summary: Mapping[str, Any], chain_fingerprint: str | None) -> None:
        with self._lock:
            data = self.load(project_id)
            entries = dict(data.get("entries") or {})
            entries[_key(track_guid, fx_guid)] = {
                "track_guid": track_guid,
                "fx_guid": fx_guid,
                "plugin": summary.get("name"),
                "family": summary.get("family"),
                "chain_fingerprint": chain_fingerprint,
                "parameter_fingerprint": summary.get("fingerprint"),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "summary": dict(summary),
            }
            self.save(project_id, {"entries": entries})

    def migrate(self, old_project_id: str, new_project_id: str) -> int:
        """Copy summaries when a save moves the project to a new path.

        The FX chains did not change, so chain fingerprints stay valid; only the
        store key moved.  Returns the number of entries carried over.
        """
        if old_project_id == new_project_id:
            return 0
        old_entries = self.entries(old_project_id)
        if not old_entries:
            return 0
        with self._lock:
            data = self.load(new_project_id)
            entries = dict(data.get("entries") or {})
            entries.update(old_entries)
            calibrations = dict(self.load(old_project_id).get("calibrations") or {})
            self.save(new_project_id, {"entries": entries,
                                       "calibrations": calibrations})
        return len(old_entries)

    def status(self, project_id: str, track_guid: str, fx_guid: str,
               current_chain: str | None) -> tuple[dict[str, Any] | None, str]:
        """Return ``(summary, state)`` where state is current/unverified/stale/missing."""
        entry = self.entries(project_id).get(_key(track_guid, fx_guid))
        if not entry:
            return None, "missing"
        summary = entry.get("summary")
        if not isinstance(summary, Mapping):
            return None, "missing"
        stored = entry.get("chain_fingerprint")
        if stored is None or current_chain is None:
            return dict(summary), "unverified"
        return (dict(summary), "current") if stored == current_chain else (None, "stale")
