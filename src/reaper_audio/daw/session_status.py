"""Bridge-facing session status and preflight.

:mod:`reaper_audio.context.status` owns the shape; this module owns the host
reads that fill it:

* one ``project.context`` read, plus one digest read when sends or envelopes
  were requested (``project.context`` only carries send *counts*);
* runtime freshness — the running bridge build against the build on disk and
  the process start time the bridge registry advertises;
* MIDI note counts per item, read one take at a time instead of requesting
  every note of the project;
* media rows, reusing the capture path's single definition of "offline";
* the optional edit-receipt journal and stored evidence units.

No project content is hardcoded here.  Limits that do exist are answer-size
limits (``limit`` arguments), not musical thresholds.

Every field is an observation (names, counts, levels, states).  This module
never grades a mix: no "too dynamic", "too wide" or similar code exists.  A
caller that wants threshold comparisons must supply its own policy and label
the result as policy-derived; absent a policy, only numbers are returned.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import __version__
from ..context.evidence import EvidenceStore
from ..context.state_reads import read_state
from ..context.status import (PREFLIGHT_SCHEMA, build_status, envelope_rows,
                              observation_codes)
from ..errors import BridgeUnavailable, ValidationError
from ..reaper.bridge_lifecycle import bridge_script_path, script_build
from ..reaper.handles import ReaperHandle
from .media_preflight import ensure_media_online, unresolved_rows
from .session import ProjectRevision

RECEIPT_JOURNAL = Path("state") / "edit_receipts.jsonl"


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _descriptor(bridge: Any, health: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The registry row for the session that answered ``health``, if any."""
    discover = getattr(bridge, "discover_instances", None)
    if not callable(discover):
        return None
    session_id = health.get("bridge_session")
    try:
        instances = discover()
    except Exception:  # noqa: BLE001 - freshness is descriptive, health is proof
        return None
    if not isinstance(instances, Sequence):
        return None
    for instance in instances:
        if (isinstance(instance, Mapping) and
                instance.get("bridge_session_id") == session_id):
            return instance
    return None


def collect_runtime(session: Any, *,
                    health: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Describe the running bridge and whether the code on disk is the code running.

    ``bridge_build`` is the content identity the bridge exposes; a mismatched
    build is ``stale_runtime``.  A source file whose mtime is newer than the
    session start is also ``stale_runtime`` (``runtime_age_s > 0``): a reload
    mints a newer session start, so a freshly reloaded runtime stays current.
    """
    if health is None:
        health = session.bridge.health(timeout_s=3.0)
    if not isinstance(health, Mapping):
        raise BridgeUnavailable("REAPER returned an invalid health response")
    descriptor = _descriptor(session.bridge, health) or {}

    script = health.get("script_path") or descriptor.get("script_path")
    script_path = Path(script) if isinstance(script, str) and script else bridge_script_path()
    try:
        code_mtime = script_path.stat().st_mtime
    except OSError:
        code_mtime = None
    on_disk_build = script_build(script_path)
    running_build = health.get("bridge_build")
    running_build = running_build if isinstance(running_build, str) and running_build else None

    started = _positive_int(descriptor.get("started_epoch"))
    if started is None:
        started = _positive_int(health.get("started_epoch"))
    code_newer = (code_mtime is not None and started is not None and
                  int(code_mtime) > int(started))
    runtime_age_s = (code_mtime - started
                     if code_mtime is not None and started is not None else None)
    build_matches = (on_disk_build == running_build
                     if on_disk_build is not None and running_build is not None else None)
    if build_matches is False:
        state, reason = "stale_runtime", "build_mismatch"
    elif code_newer:
        # The file changed after this bridge session started.  A reload gives
        # the session a newer start time than the file, so this cannot mark a
        # freshly reloaded runtime stale.
        state, reason = "stale_runtime", "code_newer_than_process"
    elif build_matches is None:
        state, reason = "unknown", "build_unknown"
    else:
        state, reason = "current", None

    from .session_control import reaper_window_focused
    return {
        "state": state,
        "reason": reason,
        "stale": state == "stale_runtime",
        "focused": reaper_window_focused(),
        "service": health.get("service"),
        "protocol": health.get("protocol"),
        "workbench_version": __version__,
        "reaper_version": health.get("reaper_version"),
        "bridge_session_id": health.get("bridge_session"),
        "bridge_build": running_build,
        "on_disk_build": on_disk_build,
        "build_matches": build_matches,
        "script_path": str(script_path),
        "code_mtime_epoch": code_mtime,
        "code_mtime_at": _iso(code_mtime),
        "process_started_epoch": started,
        "process_started_at": _iso(started),
        "runtime_age_s": runtime_age_s,
        "code_newer_than_process": code_newer,
        "open_projects": len(health.get("open_projects") or []),
    }


def merge_digest_routing(
        context: dict[str, Any],
        digest: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """Attach digest send/envelope rows to the context; report what is missing."""
    gaps: list[str] = []
    if not isinstance(digest, Mapping):
        return context, ["digest_unavailable"]
    fields = digest.get("fields")
    fields = fields if isinstance(fields, Mapping) else {}

    digest_tracks = {str(row.get("guid")): row
                     for row in fields.get("tracks") or []
                     if isinstance(row, Mapping) and row.get("guid")}
    for track in context.get("tracks") or []:
        if not isinstance(track, dict):
            continue
        row = digest_tracks.get(str(track.get("guid")))
        if row is None:
            continue
        sends = row.get("sends")
        if isinstance(sends, list):
            track["send_count"] = track.get("sends")
            track["sends"] = sends

    automation = fields.get("automation")
    automation = automation if isinstance(automation, Mapping) else {}
    envelope_tracks = {str(row.get("track_guid")): row.get("envelopes")
                       for row in automation.get("tracks") or []
                       if isinstance(row, Mapping)}
    for track in context.get("tracks") or []:
        if not isinstance(track, dict):
            continue
        envelopes = envelope_tracks.get(str(track.get("guid")))
        if isinstance(envelopes, list):
            track["envelopes"] = envelopes
    master = context.get("master")
    if isinstance(master, dict):
        master_envelopes = automation.get("master")
        if isinstance(master_envelopes, list):
            master["envelopes"] = master_envelopes

    coverage = digest.get("coverage")
    if isinstance(coverage, Mapping):
        for group in ("sends", "automation", "envelopes"):
            if group in coverage and coverage.get(group) is False:
                gaps.append(f"digest_{group}_unreadable")
    return context, gaps


def _safe_digest(session: Any, revision: ProjectRevision) -> Mapping[str, Any] | None:
    try:
        return session.bridge.project_digest(revision.project_id)
    except Exception:  # noqa: BLE001 - the status answer states the gap
        return None


def media_report(tracks: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]],
                                                               list[dict[str, Any]]]:
    """Offline rows plus the subset whose source file does not exist on disk.

    "Offline" is the capture path's rule (unmuted audio take, no decodable
    media).  "Missing" additionally checks the filesystem, so an unloaded file
    is not confused with a deleted one.
    """
    offline: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for row in unresolved_rows(list(tracks)):
        entry = dict(row)
        source = entry.get("source")
        exists = False
        if isinstance(source, str) and source.strip():
            try:
                exists = Path(source).exists()
            except OSError:
                exists = False
        entry["file_exists"] = exists
        offline.append(entry)
        if not exists:
            missing.append(entry)
    return offline, missing


def count_midi_notes(session: Any, context: dict[str, Any],
                     *, timeout_s: float = 5.0) -> int:
    """Fill ``item["midi_note_count"]`` for MIDI takes via ``MIDI_CountEvts``.

    Reading every note array for a status call would dwarf the answer, so only
    the count is requested, one take at a time.  A take that cannot be counted
    keeps ``None`` — never zero, which would claim an empty item.
    """
    revision = ProjectRevision.from_dict(context.get("revision") or {})
    if not revision.project_id or revision.change_count < 0:
        return 0
    counted = 0
    for track in context.get("tracks") or []:
        if not isinstance(track, Mapping):
            continue
        for item in track.get("items") or []:
            if not isinstance(item, dict):
                continue
            take = item.get("active_take")
            if not isinstance(take, Mapping) or not take.get("is_midi"):
                continue
            take_guid = take.get("guid")
            if not isinstance(take_guid, str) or not take_guid:
                continue
            handle = ReaperHandle(kind="MediaItem_Take",
                                  project_id=revision.project_id,
                                  change_count=revision.change_count,
                                  stable_id=take_guid)
            try:
                result = session.bridge.call_api("MIDI_CountEvts", handle,
                                                 timeout_s=timeout_s)
            except Exception:  # noqa: BLE001 - one uncountable take is a gap
                continue
            returns = getattr(result, "returns", ())
            if len(returns) >= 2 and isinstance(returns[1], int):
                item["midi_note_count"] = returns[1]
                counted += 1
    return counted


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    malformed = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if isinstance(value, Mapping):
            records.append(dict(value))
        else:
            malformed += 1
    return records, malformed


def read_receipts(archive_root: str | Path | None, *,
                  limit: int = 50) -> dict[str, Any]:
    """Read the edit-receipt journal, newest last, without creating anything.

    The journal is optional: this reader is the status view of whatever the
    edit path recorded under ``<archive>/state/edit_receipts.jsonl``.  A
    missing journal is ``empty``, not an error; malformed lines are counted so
    a torn write cannot masquerade as an empty history.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValidationError("limit must be a positive integer")
    if not archive_root:
        return {"state": "unavailable", "reason": "no_archive_root",
                "source": None, "records": [], "count": 0}
    path = Path(archive_root) / RECEIPT_JOURNAL
    if not path.is_file():
        return {"state": "empty", "source": str(path), "records": [],
                "count": 0, "malformed_count": 0}
    records, malformed = _read_jsonl(path)
    return {"state": "ok" if records else "empty", "source": str(path),
            "records": records[-limit:], "count": len(records),
            "malformed_count": malformed}


def _unit_revision_matches(recorded: Mapping[str, Any],
                           current: ProjectRevision) -> bool:
    count = recorded.get("change_count")
    if isinstance(count, bool) or not isinstance(count, int):
        return False
    if count != current.change_count:
        return False
    for key, value in (("instance_id", current.instance_id), ("epoch", current.epoch)):
        if recorded.get(key) is not None and value is not None and recorded[key] != value:
            return False
    return True


def read_evidence(archive_root: str | Path | None, revision: ProjectRevision,
                  *, limit: int = 200) -> dict[str, Any]:
    """Report which stored measurements belong to an older revision.

    Only units of this project are considered; a unit measured at another
    ``change_count`` is stale evidence for the current state.  Nothing is
    measured or re-rendered here.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValidationError("limit must be a positive integer")
    if not archive_root:
        return {"state": "unavailable", "reason": "no_archive_root",
                "units_total": 0, "units_current": 0, "units_stale": 0,
                "stale": []}
    try:
        units = EvidenceStore(archive_root).all_units()
    except Exception as error:  # noqa: BLE001 - a broken store is reported as such
        return {"state": "unavailable",
                "reason": f"{type(error).__name__}: {error}"[:200],
                "units_total": 0, "units_current": 0, "units_stale": 0,
                "stale": []}
    project_units = [unit for unit in units
                     if str(unit.get("project_id") or "") == revision.project_id]
    stale: list[dict[str, Any]] = []
    current = 0
    for unit in project_units:
        recorded = unit.get("revision")
        recorded = recorded if isinstance(recorded, Mapping) else {}
        if _unit_revision_matches(recorded, revision):
            current += 1
            continue
        stale.append({
            "id": unit.get("id"),
            "source_id": unit.get("source_id"),
            "metric": unit.get("metric"),
            "stage": unit.get("stage"),
            "interval": unit.get("interval"),
            "measured_change_count": recorded.get("change_count"),
        })
    state = ("empty" if not project_units
             else ("stale" if stale else "current"))
    return {
        "state": state,
        "current_change_count": revision.change_count,
        "units_total": len(project_units),
        "units_current": current,
        "units_stale": len(stale),
        "stale": stale[:limit],
        "stale_truncated": len(stale) > limit,
    }


def project_status(session: Any, *, detail: str = "summary", sections: object = None,
                   archive_root: str | Path | None = None,
                   include_midi_counts: bool | None = None) -> dict[str, Any]:
    """Build ``session.status`` from one context read plus descriptive extras."""
    detail, selected = _normalized(detail, sections)
    include_items = "items" in selected or "media" in selected
    context = session.snapshot(include_items=include_items)
    revision = ProjectRevision.from_dict(context.get("revision") or {})
    if not revision.project_id or revision.change_count < 0:
        raise BridgeUnavailable("REAPER returned an incomplete project revision")
    selected_set = set(selected)
    gaps: list[str] = []
    if "sends" in selected_set or "envelopes" in selected_set:
        context, digest_gaps = merge_digest_routing(dict(context),
                                             _safe_digest(session, revision))
        gaps.extend(digest_gaps)
    runtime = collect_runtime(session) if "runtime" in selected_set else None
    offline: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    if "media" in selected_set:
        offline, missing = media_report(context.get("tracks") or [])
    if (detail == "full" and "items" in selected_set and
            include_midi_counts is not False):
        count_midi_notes(session, context)
    root = archive_root if archive_root is not None else getattr(session, "archive_root", None)
    receipts = read_receipts(root) if "receipts" in selected_set else None
    state: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    if "evidence" in selected_set:
        try:
            state = read_state(root, revision.project_id)
        except ValidationError as error:
            # A present but unreadable state must not fall back to legacy
            # units: that would report old evidence as if state never existed.
            evidence = {
                "state": "unavailable",
                "reason": "state_unreadable",
                "detail": f"{type(error).__name__}: {error}"[:200],
                "units_total": 0,
                "units_current": 0,
                "units_stale": 0,
                "counts": {},
            }
        else:
            if state is None:
                evidence = read_evidence(root, revision)
    result = build_status(context=context, detail=detail, sections=selected,
                          runtime=runtime, offline=offline, missing=missing,
                          receipts=receipts, evidence=evidence, state=state)
    if gaps:
        result["gaps"] = sorted(set(gaps))
    return result


def _normalized(detail: str, sections: object) -> tuple[str, tuple[str, ...]]:
    # Imported lazily so this module's public surface stays one function per
    # host read; shape validation itself lives with the shape.
    from ..context.status import normalize_status_request
    return normalize_status_request(detail=detail, sections=sections)


def _soloed(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in rows
            if isinstance(row, Mapping) and (row.get("solo") or 0) != 0]


def _names(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    return sorted({str(row.get("name") or "") for row in rows
                   if str(row.get("name") or "")})


def _media_names(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    return sorted({str(row.get("track_name") or row.get("name") or "")
                   for row in rows
                   if str(row.get("track_name") or row.get("name") or "")})


def _allow_set(allow_solo: object) -> set[str]:
    if allow_solo is None:
        return set()
    if isinstance(allow_solo, str):
        return {allow_solo}
    if not isinstance(allow_solo, Sequence) or isinstance(allow_solo, (bytes, bytearray)):
        raise ValidationError("allow_solo must be a string or a list of strings")
    allowed: set[str] = set()
    for value in allow_solo:
        if not isinstance(value, str):
            raise ValidationError("allow_solo entries must be strings")
        allowed.add(value)
    return allowed


def project_preflight(session: Any, *, allow_solo: object = None,
                      check_focus: bool = False,
                      attempt_media_repair: bool = False) -> dict[str, Any]:
    """Check what would make a capture or an edit lie, and name it.

    Blocking findings are facts that invalidate rendered audio or the master
    path: an unrequested solo, offline media, a muted or unreadable master.
    Playing transport, muted tracks, bypassed FX and a background window are
    warnings/info: legitimate states the caller may want to change, not
    reasons to refuse an answer.
    """
    if not isinstance(check_focus, bool) or not isinstance(attempt_media_repair, bool):
        raise ValidationError("check_focus and attempt_media_repair must be booleans")
    allowed = _allow_set(allow_solo)
    context = session.snapshot(include_items=True)
    revision = ProjectRevision.from_dict(context.get("revision") or {})
    if not revision.project_id or revision.change_count < 0:
        raise BridgeUnavailable("REAPER returned an incomplete project revision")
    tracks = [track for track in context.get("tracks") or []
              if isinstance(track, Mapping)]
    master = context.get("master")
    master = master if isinstance(master, Mapping) else {}

    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    info: list[dict[str, Any]] = []

    transport = context.get("transport")
    transport = transport if isinstance(transport, Mapping) else {}
    play_state = transport.get("play_state")
    if isinstance(play_state, bool) or not isinstance(play_state, int):
        play_state = 0
    if play_state & 1:
        warnings.append({"code": "transport_playing",
                         "message": "REAPER is playing; stop or use auto_transport",
                         "names": []})

    soloed = _soloed(tracks)
    if (master.get("solo") or 0) != 0:
        soloed.append(master)
    blocked_solo = [row for row in soloed
                    if str(row.get("guid") or "") not in allowed and
                    str(row.get("name") or "") not in allowed]
    if blocked_solo:
        blockers.append({
            "code": "solo_active",
            "message": "Solo is active on tracks the caller did not allow",
            "names": _names(blocked_solo),
            "guids": [str(row.get("guid") or "") for row in blocked_solo],
        })

    muted = [track for track in tracks if bool(track.get("mute"))]
    if muted:
        info.append({"code": "tracks_muted", "message": "Muted tracks are part of the project",
                     "names": _names(muted)})

    offline, missing = media_report(tracks)
    repaired = False
    if offline and attempt_media_repair:
        repair = ensure_media_online(session)
        repaired = bool(repair.get("recovered"))
        context = session.snapshot(include_items=True)
        tracks = [track for track in context.get("tracks") or []
                  if isinstance(track, Mapping)]
        offline, missing = media_report(tracks)
    if offline:
        blockers.append({
            "code": "media_offline",
            "message": "Unmuted audio takes have no decodable media",
            "names": _media_names(offline),
            "sources": [str(row.get("source") or "") for row in offline],
        })
    if missing:
        blockers.append({
            "code": "media_missing",
            "message": "Offline take sources do not exist on disk",
            "names": _media_names(missing),
            "sources": [str(row.get("source") or "") for row in missing],
        })

    # Envelope facts come from the digest; a bridge without one says so rather
    # than reporting "no silent envelopes" for an unread automation map.
    gaps: list[str] = []
    context, digest_gaps = merge_digest_routing(
        dict(context), _safe_digest(session, revision))
    gaps.extend(digest_gaps)
    envelope_gaps = [gap for gap in gaps
                     if gap == "digest_unavailable" or "automation" in gap
                     or "envelope" in gap]
    envelopes_readable = not envelope_gaps
    with_silence = [
        {"owner": row["owner_name"], "owner_guid": row["owner_guid"],
         "guid": row["guid"], "name": row["name"],
         "point_count": row["point_count"], "min": row["min"], "max": row["max"]}
        for row in envelope_rows(context)
        if row["name"].strip().lower().startswith("volume")
        and row["point_count"] > 0 and row["max"] is not None and row["max"] <= 0.0
    ] if envelopes_readable else []

    focus: bool | None = None
    if check_focus:
        from .session_control import reaper_window_focused
        focus = reaper_window_focused()
        if focus is False:
            warnings.append({"code": "window_not_focused",
                             "message": "REAPER is not the foreground window",
                             "names": []})
        elif focus is None:
            info.append({"code": "focus_unknown",
                         "message": "Window focus cannot be read on this host",
                         "names": []})

    if not master or not master.get("guid"):
        blockers.append({"code": "master_unreadable",
                         "message": "The master track was not in the project read",
                         "names": ["master"]})
    else:
        if bool(master.get("mute")):
            blockers.append({"code": "master_muted",
                             "message": "The master track is muted",
                             "names": [str(master.get("name") or "master")]})
        master_fx = [fx for fx in master.get("fx") or [] if isinstance(fx, Mapping)]
        inactive = [fx for fx in master_fx
                    if bool(fx.get("offline")) or not bool(fx.get("enabled"))]
        if inactive:
            warnings.append({"code": "master_fx_bypassed_or_offline",
                             "message": "Master FX are bypassed or offline",
                             "names": [str(fx.get("name") or "") for fx in inactive]})
        if not master_fx:
            info.append({"code": "master_fx_empty",
                         "message": "The master track has no FX",
                         "names": []})

    track_fx_offline: list[dict[str, Any]] = []
    for track in tracks:
        for fx in track.get("fx") or []:
            if isinstance(fx, Mapping) and bool(fx.get("offline")):
                track_fx_offline.append({"track": str(track.get("name") or ""),
                                         "fx": str(fx.get("name") or "")})
    if track_fx_offline:
        warnings.append({
            "code": "fx_offline",
            "message": "Plug-ins are offline on project tracks",
            "names": [row["fx"] for row in track_fx_offline],
            "tracks": sorted({row["track"] for row in track_fx_offline}),
        })

    try:
        runtime = collect_runtime(session)
    except Exception as error:  # noqa: BLE001 - the context read already proved liveness
        runtime = {"state": "unknown", "stale": None,
                   "reason": f"{type(error).__name__}: {error}"[:200]}

    return {
        "schema": PREFLIGHT_SCHEMA,
        "ok": not blockers,
        "state": "ok" if not blockers else "blocked",
        "revision": revision.to_dict(),
        # Observable facts, one list per condition; a caller that disagrees
        # with the gate still sees exactly what was observed.
        "soloed": _names(soloed),
        "muted": _names(muted),
        "offline": _media_names(offline),
        "missing_sources": sorted({str(row.get("source") or "")
                                   for row in missing}),
        "transport": {"play_state": play_state,
                      "playing": bool(play_state & 1),
                      "cursor_s": transport.get("cursor_s")},
        "focused": focus,
        "runtime": runtime,
        "blockers": blockers,
        "warnings": warnings,
        "info": info,
        "codes": observation_codes(blockers, warnings),
        "checks": {
            "transport": {"play_state": play_state,
                          "playing": bool(play_state & 1),
                          "cursor_s": transport.get("cursor_s")},
            "solo": {"active": _names(soloed), "allowed": sorted(allowed),
                     "blocked": _names(blocked_solo)},
            "mute": {"muted": _names(muted)},
            "media": {"offline": _media_names(offline),
                      "missing": sorted({str(row.get("source") or "")
                                         for row in missing}),
                      "repaired": repaired},
            "focus": {"checked": bool(check_focus), "focused": focus},
            "envelopes": {"readable": envelopes_readable,
                          "source": "project.digest",
                          "with_silence": with_silence},
            "master": {"present": bool(master.get("guid")),
                       "mute": bool(master.get("mute")),
                       "fx_count": len(master.get("fx") or [])},
        },
        **({"gaps": sorted(set(gaps))} if gaps else {}),
    }


__all__ = [
    "RECEIPT_JOURNAL",
    "collect_runtime",
    "count_midi_notes",
    "media_report",
    "merge_digest_routing",
    "project_preflight",
    "project_status",
    "read_evidence",
    "read_receipts",
]
