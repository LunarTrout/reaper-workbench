"""Freshness wiring: capture record + current project state (Stage B1).

The pure rules live in ``context/freshness.py``.  This module supplies the two
inputs the rules need — the captured render manifest and the *current*
fingerprints from the live project — and never invents a third one.

Nothing here renders audio: reading the digest is enough to say what is dirty.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..daw.session import ProjectSession
from .freshness import build_freshness
from .render_dependencies import render_keys

TRACK_STAGE = "selected_track_via_master_parallel_batch_post_fx"
MASTER_STAGE = "master_mix_post_fx"


def current_fingerprints(session: ProjectSession, project_id: str) -> dict[str, dict[str, str]]:
    """Live ``{source_id: {stage: render_key}}`` for every complete source."""
    context = session.snapshot(include_items=True, include_fx_params=True,
                               include_midi_notes=False)
    tracks = context.get("tracks") or []
    master = context.get("master") or {}
    if not tracks or not master.get("guid"):
        raise ValueError("REAPER returned an incomplete project map")
    digest = session.digest(project_id)
    length_s = float(context.get("length_s") or 0.0)
    track_guids = [str(track["guid"]) for track in tracks if track.get("guid")]
    return render_keys(
        project_id=project_id,
        digest=digest,
        track_guids=track_guids,
        master_guid=str(master["guid"]),
        time_window={"project_start_s": 0.0, "project_end_s": length_s},
        render_contract={"capture": "native_render", "track_stage": TRACK_STAGE,
                         "master_stage": MASTER_STAGE,
                         "track_signal_includes_master_processing": True},
    )


def latest_capture_id(archive_root: str | Path, project_id: str) -> str | None:
    index = Path(archive_root) / "index.jsonl"
    try:
        lines = index.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("project_id") == project_id and row.get("capture_id"):
            return str(row["capture_id"])
    return None


def load_capture_record(archive_root: str | Path, capture_id: str) -> dict[str, Any]:
    path = Path(archive_root) / "captures" / capture_id / "record.json"
    return json.loads(path.read_text(encoding="utf-8"))


def freshness_report(session: ProjectSession, archive_root: str | Path,
                     project_id: str, *, capture_id: str | None = None) -> dict[str, Any]:
    """Per-source, per-stage freshness for a capture against the live project."""
    capture = capture_id or latest_capture_id(archive_root, project_id)
    if not capture:
        return {"schema": "reaper-workbench.freshness/1", "capture_id": None,
                "sources": {}, "summary": {"sources_total": 0,
                                           "sources_current": 0,
                                           "sources_stale": 0,
                                           "sources_unknown": 0,
                                           "stale_fraction": None},
                "reason": "no_capture_for_project"}
    record = load_capture_record(archive_root, capture)
    try:
        current = current_fingerprints(session, project_id)
    except Exception:  # noqa: BLE001 - unreadable state is "unknown", not "clean"
        current = None
    return build_freshness(record, current_fingerprints=current,
                           revision=record.get("revision_after"),
                           capture_id=capture)


__all__ = ["current_fingerprints", "freshness_report", "latest_capture_id",
           "load_capture_record"]
