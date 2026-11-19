"""`measure` — the edit → listen unit of the contract (Stage B2/B2a/B2b).

``measure`` renders only the requested sources (or only the master), measures
them, records evidence units and mints an evidence token.  It never creates a
project capture, never seals a project-wide record and never mints a global
receipt.

The interval is a parameter.  This module has no idea what a bar, a section or
a "chorus" is: the caller declares ``start_s``/``end_s`` and gets exactly that
interval measured.  If a metric is not defined on the requested interval the
analyzer returns its own ``null``/note; nothing is substituted here.

Stage vocabulary:

* ``via_master`` (default) — stem rendered through the master chain, i.e. what
  you hear;
* ``master`` — the master mix only (no track renders);
* ``premaster`` — not produced by this renderer yet.  Asking for it is an
  explicit refusal with reason ``stage_not_rendered`` instead of a silent
  substitution (§2.3, §4.3a).
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..analysis.artifacts import AudioArtifactWorkspace
from ..context import contract_v2
from ..context.evidence import EvidenceStore
from ..errors import BridgeUnavailable, MediaUnresolved, ValidationError
from .full_capture import (MASTER_SIGNAL_STAGE, TRACK_SIGNAL_STAGE,
                           FullCaptureService, ProjectRevision,
                           _analyze_rendered_files, _number)
from .analysis_cache_stage import merge as merge_cached_results
from .analysis_cache_stage import split as split_cached_payloads
from .analysis_cache_stage import store as store_measured_results
from .analysis_policy import resolve_analysis_workers, validate_analysis_worker_request
from .media_preflight import ensure_media_online, unresolved_rows
from .profile import build_profile
from .render_cache import RenderCache

MEASURE_STAGE_VIA_MASTER = "via_master"
MEASURE_STAGE_MASTER = "master"
MEASURE_STAGE_PREMASTER = "premaster"
MEASURE_STAGES = (MEASURE_STAGE_VIA_MASTER, MEASURE_STAGE_MASTER)



def _feeder_guids(digest: Mapping[str, Any], targets: Sequence[str]) -> list[str]:
    """Tracks that feed the targets through sends, transitively.

    A bus owns no items of its own: rendering only the bus yields silence. The
    render therefore selects the bus together with every track that sends into
    it, and the requested sources are filtered back after admission.
    """
    rows = {str(track.get("guid")): track
            for track in (digest.get("fields") or {}).get("tracks") or []}
    wanted = set(targets)
    changed = True
    while changed:
        changed = False
        for guid, track in rows.items():
            if guid in wanted:
                continue
            for send in track.get("sends") or []:
                if str(send.get("dest_guid") or "") in wanted:
                    wanted.add(guid)
                    changed = True
                    break
    return [guid for guid in wanted if guid not in set(targets)]

class MeasureService(FullCaptureService):
    """Narrow measurement service built on the capture render primitives."""

    def measure(self, *,
                sources: Sequence[str] | str = "all",
                section: tuple[float, float] | None = None,
                stage: str = MEASURE_STAGE_VIA_MASTER,
                channels: str = "stereo",
                sample_rate: int | None = None,
                tail_ms: int = 0,
                analysis_workers: int | None = None,
                step_s: float | None = None,
                split_db: float | None = None,
                producer: str = "hermes") -> dict[str, Any]:
        if stage == MEASURE_STAGE_PREMASTER:
            raise ValidationError(
                "The premaster stage is not produced by this renderer",
                details={"stage": stage,
                         "reason": "stage_not_rendered",
                         "next_action": ("use stage=via_master, or declare the premaster "
                                         "set empty; the library never substitutes one "
                                         "stage for another")})
        if stage not in MEASURE_STAGES:
            raise ValidationError("stage must be via_master or master",
                                  details={"stage": stage, "allowed": list(MEASURE_STAGES)})
        if channels not in {"stereo", "mono"}:
            raise ValidationError("channels must be stereo or mono")
        if sample_rate is not None and (isinstance(sample_rate, bool) or
                                        not isinstance(sample_rate, int) or sample_rate <= 0):
            raise ValidationError("sample_rate must be a positive integer or null")
        if isinstance(tail_ms, bool) or not isinstance(tail_ms, int) or tail_ms < 0:
            raise ValidationError("tail_ms must be a non-negative integer")
        if analysis_workers is not None:
            validate_analysis_worker_request(analysis_workers)

        start_s = end_s = None
        if section is not None:
            if not isinstance(section, (tuple, list)) or len(section) != 2:
                raise ValidationError("section must be [start_s, end_s]")
            start_s, end_s = _number(section[0], "section.start_s"), _number(section[1], "section.end_s")
            if end_s <= start_s:
                raise ValidationError("section.end_s must be greater than start_s")

        started = time.perf_counter()
        self._profile = {"mute_rpc_s": 0.0, "mute_rpc_calls": 0,
                         "mute_bridge_calls": 0, "mute_track_mutations": 0,
                         "render_s": 0.0, "render_calls": 0,
                         "render_times": {}, "render_batch_s": 0.0,
                         "admit_s": 0.0, "decode_s": 0.0, "dsp_s": 0.0}
        context = self.session.snapshot(include_items=True, include_fx_params=True,
                                        include_midi_notes=False)
        if unresolved_rows(context.get("tracks") or []):
            # REAPER drops media while its window is inactive; repair and prove
            # it before declaring the render silent.
            preflight = ensure_media_online(self.session)
            context = self.session.snapshot(include_items=True, include_fx_params=True,
                                            include_midi_notes=False)
            remaining = unresolved_rows(context.get("tracks") or [])
            if remaining:
                raise MediaUnresolved(
                    "Some take sources could not be resolved; the analysis render would be silent",
                    details={"sources": remaining, "preflight": preflight})
        revision = ProjectRevision.from_dict(context.get("revision", {}))
        if not revision.project_id or not revision.path:
            raise BridgeUnavailable("REAPER returned an incomplete project identity")
        tracks = context.get("tracks")
        master = context.get("master")
        if not isinstance(tracks, list) or not tracks or not isinstance(master, dict):
            raise BridgeUnavailable("REAPER returned an incomplete project map")
        transport = context.get("transport")
        if not isinstance(transport, Mapping) or transport.get("play_state") not in (0, 0.0):
            raise ValidationError("Stop playback before measuring",
                                  details={"transport": transport})
        project_length_s = float(context.get("length_s") or 0.0)
        digest = self.bridge.project_digest(revision.project_id)

        tracks_by_guid = {str(track.get("guid")): track for track in tracks
                          if isinstance(track, Mapping) and track.get("guid")}
        master_guid = str(master.get("guid"))
        selected = self._resolve_sources(sources, stage=stage,
                                         track_guids=list(tracks_by_guid),
                                         master_guid=master_guid)
        if stage == MEASURE_STAGE_MASTER and selected != [master_guid]:
            raise ValidationError("stage=master measures only the master",
                                  details={"sources": selected})

        workspace_root = self.archive_root / ".incoming"
        workspace_root.mkdir(parents=True, exist_ok=True)
        workspace = AudioArtifactWorkspace(workspace_root)
        analysis_cache = self._analysis_cache(start_s, end_s)
        fingerprints = (self._render_fingerprints(
            project_id=revision.project_id, digest=digest,
            track_guids=[guid for guid in selected if guid != master_guid],
            master_guid=master_guid, start_s=start_s, end_s=end_s,
            project_length_s=project_length_s)
            if analysis_cache is not None else {})

        cache = RenderCache(self.archive_root / "renders")
        cached = None
        if len(selected) == 1 and stage != MEASURE_STAGE_MASTER:
            cached = cache.lookup(project_id=revision.project_id,
                                  revision=revision.to_dict(),
                                  source_guid=selected[0], start_s=start_s,
                                  end_s=end_s, stage=stage, channels=channels,
                                  sample_rate=sample_rate, tail_ms=tail_ms)

        item_rows: list[dict[str, Any]] = []
        stage_by_guid: dict[str, str] = {}
        render_started = time.perf_counter()
        try:
            current = revision
            if cached is not None:
                track = tracks_by_guid[selected[0]]
                meta = cached["meta"]
                item_rows = [{"source_guid": selected[0],
                              "name": str(track.get("name") or ""),
                              "path": cached["path"],
                              "stage": TRACK_SIGNAL_STAGE, "is_master": False,
                              "channels": int(meta["channels_count"]),
                              "sample_rate": int(meta["rate"]),
                              "frames": int(meta["frames"])}]
                stage_by_guid[selected[0]] = TRACK_SIGNAL_STAGE
            elif stage == MEASURE_STAGE_MASTER:
                rendered = self._render_master_only(current, workspace,
                                                    f"{workspace.job_id}-master",
                                                    start_s, end_s, channels,
                                                    sample_rate, tail_ms)
                current = ProjectRevision.from_dict(rendered["revision_after"])
                item = self._one_file(rendered, workspace)
                self._check_rendered_wav(Path(str(item["path"])), rendered)
                item.update({"source_guid": master_guid, "name": "MASTER",
                             "is_master": True, "stage": MASTER_SIGNAL_STAGE})
                item_rows = [item]
                stage_by_guid[master_guid] = MASTER_SIGNAL_STAGE
            else:
                feeders = _feeder_guids(digest, selected)
                batch_guids = list(selected) + feeders
                rendered = self._render_batch(
                    current, workspace, f"{workspace.job_id}-measure",
                    len(batch_guids), start_s, end_s, channels, sample_rate, tail_ms,
                    track_guids=(None if len(batch_guids) == len(tracks_by_guid)
                                 else batch_guids))
                current = ProjectRevision.from_dict(rendered["revision_after"])
                admitted = self._admit_track_batch(rendered, batch_guids,
                                                    tracks_by_guid, workspace)
                admitted = [row for row in admitted
                            if str(row["source_guid"]) in set(selected)]
                item_rows = admitted
                if len(selected) == 1 and item_rows:
                    try:
                        cache.store(project_id=current.project_id,
                                    revision=current.to_dict(),
                                    source_guid=selected[0], start_s=start_s,
                                    end_s=end_s, stage=stage, channels=channels,
                                    sample_rate=sample_rate, tail_ms=tail_ms,
                                    source_wav=Path(str(item_rows[0]["path"])),
                                    frames=int(item_rows[0].get("frames") or 0),
                                    rate=int(item_rows[0].get("sample_rate") or 0),
                                    channels_count=int(item_rows[0].get("channels") or 0))
                    except OSError:
                        pass
                for row in admitted:
                    stage_by_guid[str(row["source_guid"])] = TRACK_SIGNAL_STAGE
            render_s = time.perf_counter() - render_started

            # The rendered file lives in the workspace, which is closed with the
            # call: read the profile here, while the WAV still exists.
            profile = None
            if step_s is not None:
                if len(item_rows) != 1:
                    raise ValidationError("profile needs exactly one source",
                                          details={"sources": len(item_rows)})
                from ..analysis.audio import load_audio
                audio = load_audio(str(item_rows[0]["path"]))
                profile = build_profile(audio.samples, int(audio.sample_rate),
                                        step_s=float(step_s),
                                        offset_s=float(start_s or 0.0),
                                        split_db=split_db)

            payloads = [(str(row["source_guid"]), str(row["path"])) for row in item_rows]
            window_s = (float(end_s) - float(start_s)
                        if start_s is not None and end_s is not None
                        else project_length_s)
            worker_plan = resolve_analysis_workers(asset_count=len(payloads),
                                                   window_s=window_s or None,
                                                   requested=analysis_workers)
            analysis_started = time.perf_counter()
            if analysis_cache is not None:
                hits, misses = split_cached_payloads(payloads, stage_by_guid,
                                                     analysis_cache, fingerprints)
                measured = _analyze_rendered_files(misses, worker_plan.workers) if misses else []
                results = merge_cached_results(payloads, hits, measured)
                store_measured_results(payloads, stage_by_guid, results, analysis_cache)
            else:
                results = _analyze_rendered_files(payloads, worker_plan.workers)
            analysis_s = time.perf_counter() - analysis_started
        finally:
            workspace.close()

        units = self._evidence_units(item_rows, results, stage_by_guid=stage_by_guid,
                                     revision=current, start_s=start_s, end_s=end_s,
                                     project_length_s=project_length_s,
                                     fingerprints=fingerprints)
        store = EvidenceStore(self.archive_root)
        unit_ids = store.put_many(units)
        token = store.mint_token(project_id=revision.project_id,
                                 revision=current.to_dict(), units=unit_ids)
        result = {
            "ok": True,
            "operation": "measure",
            "stage": stage,
            "interval": {"start_s": start_s if start_s is not None else 0.0,
                         "end_s": end_s if end_s is not None else project_length_s},
            "sources": [{"source_id": str(row["source_guid"]),
                         "name": str(row.get("name") or ""),
                         "stage": stage_by_guid[str(row["source_guid"])],
                         "file": str(row["path"])} for row in item_rows],
            "source_count": len(item_rows),
            "evidence": {"units": len(unit_ids), "token": token["token"],
                         "token_kind": token["kind"]},
            "analysis_cache": analysis_cache.stats() if analysis_cache is not None else None,
            "revision": current.to_dict(),
            "timings": {"render_s": round(render_s, 3),
                        "analysis_s": round(analysis_s, 3),
                        "total_s": round(time.perf_counter() - started, 3)},
            "project_capture_created": False,
            "producer": producer,
        }
        if profile is not None:
            result["profile"] = profile
        return result

    # ------------------------------------------------------------------ #
    def _resolve_sources(self, sources: Sequence[str] | str, *, stage: str,
                         track_guids: list[str], master_guid: str) -> list[str]:
        if stage == MEASURE_STAGE_MASTER:
            if isinstance(sources, str) and sources in {"all", "master"}:
                return [master_guid]
            if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes)):
                if list(sources) in (["master"], ["master:" + master_guid]):
                    return [master_guid]
            raise ValidationError("stage=master accepts sources='master' or sources='all'",
                                  details={"sources": sources})
        if isinstance(sources, str):
            if sources == "all":
                return list(track_guids)
            if sources == "dirty":
                raise ValidationError("sources='dirty' needs a freshness map; "
                                      "pass explicit source ids")
            sources = [sources]
        if not isinstance(sources, Sequence):
            raise ValidationError("sources must be a list of track ids, 'all' or 'master'")
        resolved: list[str] = []
        for raw in sources:
            text = str(raw)
            if text.startswith("track:"):
                text = text.split(":", 1)[1]
            if text not in track_guids:
                raise ValidationError("Unknown source id for this project",
                                      details={"source": raw})
            if text not in resolved:
                resolved.append(text)
        if not resolved:
            raise ValidationError("sources must not be empty")
        return resolved

    @staticmethod
    def _evidence_units(item_rows: Sequence[Mapping[str, Any]],
                        results: Sequence[Mapping[str, Any]],
                        *, stage_by_guid: Mapping[str, str],
                        revision: ProjectRevision,
                        start_s: float | None, end_s: float | None,
                        project_length_s: float,
                        fingerprints: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        by_guid = {str(result["source_guid"]): result for result in results}
        interval = {"start_s": 0.0 if start_s is None else float(start_s),
                    "end_s": project_length_s if end_s is None else float(end_s)}
        units: list[dict[str, Any]] = []
        for row in item_rows:
            guid = str(row["source_guid"])
            result = by_guid.get(guid)
            if result is None:
                continue
            report = result.get("analysis") or {}
            metrics = report.get("metrics") or {}
            provenance = report.get("provenance") or {}
            method_version = str(provenance.get("analysis_version") or "1")
            for metric, raw in metrics.items():
                value = raw.get("value") if isinstance(raw, Mapping) else raw
                if value is None:
                    continue
                units.append({
                    "source_id": (f"master:{guid}" if row.get("is_master")
                                  else f"track:{guid}"),
                    "interval": dict(interval),
                    "metric": str(metric),
                    "method": str(provenance.get("analysis") or contract_v2.ANALYZER),
                    "method_version": method_version,
                    "stage": stage_by_guid.get(guid, ""),
                    "measurement_stage": str(provenance.get("stage") or ""),
                    "sample_rate": int(result.get("sample_rate") or 0),
                    "channels": int(result.get("channels") or 0),
                    "value": float(value),
                    "unit": str(raw.get("unit") if isinstance(raw, Mapping) else ""),
                    "fingerprint": (fingerprints or {}).get(guid),
                    "project_id": revision.project_id,
                    "revision": {"change_count": revision.change_count,
                                 "epoch": revision.epoch,
                                 "instance_id": revision.instance_id},
                    "capture_id": None,
                })
        return units


__all__ = ["MeasureService", "MEASURE_STAGE_MASTER", "MEASURE_STAGE_VIA_MASTER"]
