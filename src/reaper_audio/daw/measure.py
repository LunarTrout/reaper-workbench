"""`measure` — the edit → listen unit of the contract (Stage B2/B2a/B2b).

``measure`` renders only the requested sources (or only the master), measures
them, records evidence units and mints an evidence token.  It never creates a
project capture, never seals a project-wide record and never mints a global
receipt.

Three independent axes, all supplied by the caller or read from the project:

* **what** — ``sources``: any track/bus/master ids, or external files given as
  ``{"kind": "file", "path": ...}``.  No name is special;
* **where** — ``intervals``: an explicit list of ``[start_s, end_s]`` windows
  (they may overlap), ``"regions"`` (the project's regions, or marker spans
  when no region exists), ``"bars:<n>"`` (a grid built from the project's own
  tempo and time-signature map) or ``"all"``;
* **what of it** — ``metrics`` and ``bands``: the metric names that leave the
  library, and how the measured 1/3-octave bands are aggregated
  (``octave``/``third``/explicit edges).

The render covers one union span for all intervals, so a 4×6 table costs one
render per source instead of 24.  Each cell is then decoded over exactly its own
window, which is why a cell and a standalone measurement of that window agree.

Backward compatibility: a call with ``section=(start, end)`` (or neither
``section`` nor ``intervals``) and one source returns the same values and the
same top-level keys it always did; the table is added next to them.

Stage vocabulary:

* ``via_master`` (default) — stem rendered through the master chain, i.e. what
  you hear;
* ``master`` — the master mix only (no track renders);
* external files — measured in place, never rendered, stage ``external_file``;
* ``premaster`` — not produced by this renderer yet.  Asking for it is an
  explicit refusal with reason ``stage_not_rendered`` instead of a silent
  substitution (§2.3, §4.3a).
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..analysis.artifacts import AudioArtifactWorkspace
from ..analysis.audio import load_audio
from ..analysis.metrics import ANALYSIS_VERSION
from ..context import contract_v2
from ..context.capture_state import digest_state_rows
from ..context.evidence import EvidenceStore
from ..context.measurement_cache import MeasurementCache
from ..context.project_state import (ProjectState, StateStore, fingerprint_master,
                                     fingerprint_routing, fingerprint_track)
from ..context.state_diff import state_changes
from ..errors import BridgeUnavailable, MediaUnresolved, ValidationError
from ..models import Section
from .analysis_cache_stage import merge as merge_cached_results
from .analysis_cache_stage import split as split_cached_payloads
from .analysis_cache_stage import store as store_measured_results
from .analysis_policy import (resolve_analysis_workers,
                              validate_analysis_worker_request)
from .full_capture import (MASTER_SIGNAL_STAGE, TRACK_SIGNAL_STAGE,
                           FullCaptureService, ProjectRevision, _number)
from .measure_external import (EXTERNAL_SIGNAL_STAGE, ExternalSource,
                               is_external_entry, resolve_external_source)
from .measure_intervals import (MeasureInterval, overlapping_pairs,
                                parse_intervals, union_span)
from .measure_table import (BANDS_KEY, BandPlan, MetricSelection, build_cell,
                            parse_bands, parse_metrics, summarize_rows)
from .measure_window import analyze_windows
from .media_preflight import ensure_media_online, unresolved_rows
from .profile import PROFILE_VERSION, build_profile
from .render_cache import RenderCache

MEASURE_STAGE_VIA_MASTER = "via_master"
MEASURE_STAGE_MASTER = "master"
MEASURE_STAGE_PREMASTER = "premaster"
MEASURE_STAGES = (MEASURE_STAGE_VIA_MASTER, MEASURE_STAGE_MASTER)

#: REAPER transport commands; infrastructure, not a mix decision.
TRANSPORT_STOP = 1016
TRANSPORT_PLAY = 1007
TRANSPORT_PAUSE = 1008
PLAY_STATE_PLAYING = 1
PLAY_STATE_PAUSED = 2
PLAY_STATE_RECORDING = 4
TRANSPORT_STOP_ATTEMPTS = 10
TRANSPORT_STOP_POLL_S = 0.05

#: Cache schema for the table path; a change here invalidates stored cells.
MEASURE_CACHE_SCHEMA = "measure-table/1"

#: The profile gates a caller may override; every one has a declared default in
#: ``daw.profile`` and the values used are returned under ``profile.thresholds``.
PROFILE_GATE_KEYS = ("active_margin_db", "floor_percentile", "noise_gate_dbfs",
                     "relative_ceiling_db", "min_segment_s")


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


def cache_options(selection: MetricSelection, band_plan: BandPlan) -> dict[str, Any]:
    """Everything besides the audio and the window that can change a cell.

    The metric selection and the band plan take part in the key on purpose: a
    stored cell is only reused for the same request, and the analyzer/profile
    versions make an old entry unreachable after either one changes.
    """
    return {
        "measure_schema": MEASURE_CACHE_SCHEMA,
        "analysis_version": ANALYSIS_VERSION,
        "profile_version": PROFILE_VERSION,
        "metrics": list(selection.keys) + ([BANDS_KEY] if selection.bands else []),
        "bands": band_plan.mode,
        "band_edges": ([[low, high] for low, high, _label in band_plan.bands]
                       if band_plan.mode == "explicit" else None),
    }


# --------------------------------------------------------------------------- #
# project state: one digest read -> object fingerprints
# --------------------------------------------------------------------------- #

#: Structural keys a table cell uses for the band array and its aggregate.
#: They are part of the table contract (``daw.measure_table.build_cell``), not a
#: metric vocabulary: the scalar metrics come from the request itself.
BAND_CELL_KEYS: tuple[str, ...] = (BANDS_KEY, "band_aggregate")

#: ``ProjectState.mark_stale`` keys a master change under this name, not a GUID
#: (see ``context.project_state.MASTER_STAGE_PREFIX``).
MASTER_CHANGE_KEY = "master"


def _digest_fields(digest: object) -> Mapping[str, Any]:
    """The ``fields`` block of a project digest, or an empty mapping."""
    if not isinstance(digest, Mapping):
        return {}
    fields = digest.get("fields")
    return fields if isinstance(fields, Mapping) else {}


def _fingerprint_fields(fingerprint: object) -> dict[str, Any]:
    """The field map of a stored fingerprint, or an empty mapping."""
    if isinstance(fingerprint, Mapping):
        fields = fingerprint.get("fields")
        if isinstance(fields, Mapping):
            return dict(fields)
        return dict(fingerprint)
    return {}


def _stored_object_view(objects: Mapping[str, Any]) -> dict[str, Any]:
    """``ProjectState.objects`` as the plain fingerprint maps ``state_diff`` compares.

    The state store keeps ``{guid: {"guid", "fingerprint"}}`` records; the diff
    layer compares object field maps.  Unwrapping here keeps both modules
    independent of each other's storage shape.
    """
    tracks: dict[str, dict[str, Any]] = {}
    stored_tracks = objects.get("tracks")
    if isinstance(stored_tracks, Mapping):
        for guid, record in stored_tracks.items():
            if isinstance(record, Mapping):
                tracks[str(guid)] = _fingerprint_fields(record.get("fingerprint"))
    view: dict[str, Any] = {"tracks": tracks}
    master = objects.get("master")
    if isinstance(master, Mapping):
        view["master"] = _fingerprint_fields(master.get("fingerprint"))
    routing = objects.get("routing")
    if isinstance(routing, Mapping):
        view["routing"] = _fingerprint_fields(routing.get("fingerprint"))
    return view


def refresh_state_objects(state: ProjectState, digest: object):
    """Update saved fingerprints and invalidate facts from a live digest."""
    if not isinstance(digest, Mapping) or not isinstance(_digest_fields(digest).get("tracks"), list):
        return {}, None, None, {}
    rows, master_row, routing_rows = digest_state_rows(digest)
    track_rows = {row["guid"]: row for row in rows}

    track_fingerprints = {guid: fingerprint_track(row)
                          for guid, row in track_rows.items()}
    master_fingerprint = (fingerprint_master(master_row)
                          if master_row is not None else None)
    routing_fingerprint = (fingerprint_routing(routing_rows)
                           if routing_rows is not None else None)

    stored_view = _stored_object_view(state.objects)
    current_view: dict[str, Any] = {
        "tracks": {guid: _fingerprint_fields(fingerprint)
                   for guid, fingerprint in track_fingerprints.items()},
    }
    comparable_stored: dict[str, Any] = {"tracks": dict(stored_view["tracks"])}
    if master_fingerprint is not None:
        current_view["master"] = _fingerprint_fields(master_fingerprint)
        comparable_stored["master"] = stored_view.get("master", {})
    if routing_fingerprint is not None:
        current_view["routing"] = _fingerprint_fields(routing_fingerprint)
        comparable_stored["routing"] = stored_view.get("routing", {})
    changes = state_changes(comparable_stored, current_view)
    objects_block = changes.get("objects") or {}
    changed: dict[str, list[str]] = {}
    for guid, section in (objects_block.get("tracks") or {}).items():
        if isinstance(section, Mapping):
            changed[str(guid)] = [str(field) for field in section.get("fields") or []]
    for guid in (*objects_block.get("added_tracks", ()), *objects_block.get("removed_tracks", ())):
        changed.setdefault(guid, ["track_identity"])
    master_fields = (objects_block.get("master") or {}).get("fields") or []
    if master_fields:
        changed[MASTER_CHANGE_KEY] = [str(field) for field in master_fields]
    routing_changed = bool((objects_block.get("routing") or {}).get("changed"))

    stale = state.mark_stale(changed, routing_changed=routing_changed)

    if track_rows or master_row is not None or routing_rows is not None:
        state.upsert_objects(
            tracks=track_rows or None,
            master=master_row,
            routing=routing_rows,
        )
    if objects_block.get("removed_tracks"):
        state.remove_tracks(objects_block["removed_tracks"])

    return track_fingerprints, master_fingerprint, routing_fingerprint, {
        "objects_changed": bool(changes.get("changed")),
        "changed": changed,
        "routing_changed": routing_changed,
        "stale_facts": sorted(stale),
        "added_tracks": list(objects_block.get("added_tracks") or []),
        "removed_tracks": list(objects_block.get("removed_tracks") or []),
    }


@dataclass(frozen=True, slots=True)
class _Selection:
    """Project sources to render plus external files to read in place."""

    track_guids: tuple[str, ...]
    external: tuple[ExternalSource, ...]


@dataclass(frozen=True, slots=True)
class _Asset:
    """One measurable source: a rendered file or an external file."""

    source_id: str
    evidence_id: str
    name: str
    kind: str
    stage: str
    path: Path
    offset_s: float
    duration_s: float
    sample_rate: int
    channels: int
    frames: int
    fingerprint: str | None = None
    is_master: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "name": self.name,
                "kind": self.kind, "stage": self.stage,
                "file": str(self.path),
                "duration_s": round(self.duration_s, 6),
                "sample_rate": self.sample_rate, "channels": self.channels}


class MeasureService(FullCaptureService):
    """Narrow measurement service built on the capture render primitives.

    A measurement is also the write that keeps a project's state current: after
    the table is built, every cell is upserted into the project state as a fact
    carrying the object fingerprints read for that measurement, and objects
    whose fingerprint moved since the stored one are marked stale first.  The
    state writer is resolved lazily from the archive root; a service built
    without one is pure analysis and writes no state.
    """

    def __init__(self, session: Any, archive_root: Any, *,
                 state_writer: Any = None) -> None:
        """Bind the session and the archive; optionally inject a state writer.

        ``state_writer`` (tests, or a caller that already owns a
        :class:`~reaper_audio.context.project_state.ProjectState`) must expose
        ``objects``, ``state_token``, ``mark_stale``, ``upsert_objects``,
        ``upsert_fact`` and ``save``.  When it is omitted, one ``StateStore`` is
        created for the archive root and a fresh ``ProjectState`` is loaded per
        measurement, so a state file written by another writer is picked up.

        A falsy ``archive_root`` disables state recording entirely.  It keeps
        the inherited root at the working directory, exactly as an empty string
        always did, so existing analysis-only callers keep their behaviour.
        """
        self._state_enabled = bool(archive_root)
        self._state_writer = state_writer
        self._state_store: StateStore | None = None
        super().__init__(session, archive_root if archive_root else "")

    # ------------------------------------------------------------------ #
    # project state
    # ------------------------------------------------------------------ #
    def _project_state(self, project_id: str) -> Any:
        """The state writer for this measurement, or ``None`` when disabled."""
        if not self._state_enabled:
            return None
        if self._state_writer is not None:
            return self._state_writer
        if self._state_store is None:
            self._state_store = StateStore(self.archive_root)
        return ProjectState(self._state_store, project_id)

    def _state_digest(self, digest: object, project_id: str) -> Mapping[str, Any]:
        """The one digest read the state write derives its fingerprints from.

        ``measure`` already reads the digest for every measurement that renders
        project sources and hands it in, so this normally reuses that read and
        adds no bridge call.  Only a caller that supplies no digest at all (a
        direct ``_record_state`` call, or a future call site) triggers one read
        here; a bridge that cannot answer leaves the fingerprints unknown
        rather than failing a measurement whose audio is already analysed.
        """
        if isinstance(digest, Mapping) and isinstance(_digest_fields(digest).get("tracks"), list):
            return digest
        reader = getattr(self.bridge, "project_digest", None)
        if not callable(reader):
            return {}
        try:
            answer = reader(project_id)
        except BridgeUnavailable:
            return {}
        return answer if isinstance(answer, Mapping) else {}

    def _record_state(self, *, project_id: str, revision: Mapping[str, Any],
                      table: Mapping[str, Any], digest: object) -> dict[str, Any]:
        """Upsert one measurement's cells into the project state.

        For every row (interval) × column (source) × scalar metric one fact is
        written; the band array and its aggregate travel together as the single
        metric ``bands``.  Each fact carries the source, master and routing
        fingerprints of the one digest read for this measurement (external
        files have none), so a later change can prove exactly which
        measurements it invalidated.

        Before any fact is written, the stored object fingerprints are compared
        with the current ones and :meth:`ProjectState.mark_stale` is called for
        every changed track, the master and routing.  The new fingerprints are
        then stored, so the next measurement compares against what it measured.

        Returns the additive ``state`` block of the measure result:
        ``{"state_token", "facts_written", "changes"}``.
        """
        disabled = {"state_token": 0, "facts_written": 0, "changes": {}}
        state = self._project_state(project_id)
        if state is None:
            return disabled

        digest = self._state_digest(digest, project_id)
        track_fingerprints, master_fingerprint, routing_fingerprint, state_changes_report = (
            refresh_state_objects(state, digest))

        def facts_for(source_id: str, stage: str, kind: str) -> dict[str, Any] | None:
            """The three fingerprints a fact of this source depends on."""
            if stage == EXTERNAL_SIGNAL_STAGE:
                return None
            if kind == "master" or stage == MASTER_SIGNAL_STAGE:
                source = master_fingerprint
            else:
                source = track_fingerprints.get(source_id)
            return {"source": source, "master": master_fingerprint,
                    "routing": routing_fingerprint}

        rows = table.get("rows") or []
        columns = table.get("columns") or []
        cells = table.get("cells") or []
        metric_selection = table.get("metric_selection") or {}
        metric_keys = [str(key) for key in (metric_selection.get("keys") or [])]
        written = 0
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            window = [_number(row.get("start_s"), "interval.start_s"),
                      _number(row.get("end_s"), "interval.end_s")]
            cell_row = cells[index] if index < len(cells) else {}
            if not isinstance(cell_row, Mapping):
                continue
            for column in columns:
                if not isinstance(column, Mapping):
                    continue
                source_id = str(column.get("source_id") or "")
                stage = str(column.get("stage") or "")
                cell = cell_row.get(source_id)
                if not source_id or not isinstance(cell, Mapping):
                    continue
                fingerprints = facts_for(source_id, stage, str(column.get("kind") or ""))
                for metric in metric_keys:
                    if metric not in cell:
                        continue
                    state.upsert_fact(source_id=source_id, stage=stage, window=window,
                                      metrics={metric: cell[metric]},
                                      fingerprints=fingerprints, revision=dict(revision))
                    written += 1
                bands = {key: cell[key] for key in BAND_CELL_KEYS if key in cell}
                if bands:
                    state.upsert_fact(source_id=source_id, stage=stage, window=window,
                                      metrics={"bands": bands},
                                      fingerprints=fingerprints, revision=dict(revision))
                    written += 1

        state.save()
        return {
            "state_token": int(state.state_token),
            "facts_written": written,
            "changes": state_changes_report,
        }

    def measure(self, *,
                sources: Sequence[str | Mapping[str, Any]] | str = "all",
                section: tuple[float, float] | None = None,
                intervals: Any = None,
                metrics: Any = None,
                bands: Any = None,
                stage: str = MEASURE_STAGE_VIA_MASTER,
                channels: str = "stereo",
                sample_rate: int | None = None,
                tail_ms: int = 0,
                analysis_workers: int | None = None,
                step_s: float | None = None,
                split_db: float | None = None,
                profile_options: Mapping[str, Any] | None = None,
                auto_transport: bool = False,
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
        if not isinstance(auto_transport, bool):
            raise ValidationError("auto_transport must be a boolean")
        if step_s is not None and (isinstance(step_s, bool) or
                                   not isinstance(step_s, (int, float)) or step_s <= 0):
            raise ValidationError("step_s must be a positive number or null")
        if section is not None and intervals is not None:
            raise ValidationError(
                "Pass either section (one window) or intervals, not both",
                details={"section": list(section) if isinstance(section, Sequence) else section,
                         "intervals": intervals})

        metric_selection = parse_metrics(metrics)
        band_plan = parse_bands(bands)
        if profile_options is not None and step_s is None:
            raise ValidationError("profile_options only apply with step_s",
                                  details={"step_s": step_s})
        profile_gates = self._profile_gates(profile_options)

        legacy_section: tuple[float, float] | None = None
        if section is not None:
            if not isinstance(section, (tuple, list)) or len(section) != 2:
                raise ValidationError("section must be [start_s, end_s]")
            start_s = _number(section[0], "section.start_s")
            end_s = _number(section[1], "section.end_s")
            if end_s <= start_s:
                raise ValidationError("section.end_s must be greater than start_s")
            legacy_section = (start_s, end_s)

        started = time.perf_counter()
        self._profile = {"mute_rpc_s": 0.0, "mute_rpc_calls": 0,
                         "mute_bridge_calls": 0, "mute_track_mutations": 0,
                         "render_s": 0.0, "render_calls": 0,
                         "render_times": {}, "render_batch_s": 0.0,
                         "admit_s": 0.0, "decode_s": 0.0, "dsp_s": 0.0}
        self._preflight: dict[str, Any] | None = None
        transport_context = self.session.snapshot(include_items=False,
                                                  include_fx_params=False,
                                                  include_midi_notes=False)
        needs_render = self._needs_render(sources)
        with self._transport_controlled(transport_context,
                                        auto_transport=auto_transport,
                                        needed=needs_render) as transport_report:
            context = self._snapshot_online(repair=needs_render)
            result = self._measure_resolved(
                context=context, sources=sources, stage=stage, channels=channels,
                sample_rate=sample_rate, tail_ms=tail_ms,
                analysis_workers=analysis_workers, step_s=step_s, split_db=split_db,
                metric_selection=metric_selection, band_plan=band_plan,
                intervals_spec=intervals, legacy_section=legacy_section,
                transport_report=transport_report, started=started,
                producer=producer, profile_gates=profile_gates)
        return result

    # ------------------------------------------------------------------ #
    def _measure_resolved(self, *, context: dict[str, Any], sources: Any,
                          stage: str, channels: str, sample_rate: int | None,
                          tail_ms: int, analysis_workers: int | None,
                          step_s: float | None, split_db: float | None,
                          metric_selection: MetricSelection,
                          band_plan: BandPlan, intervals_spec: Any,
                          legacy_section: tuple[float, float] | None,
                          transport_report: dict[str, Any], started: float,
                          producer: str,
                          profile_gates: Mapping[str, Any] | None = None
                          ) -> dict[str, Any]:
        revision = ProjectRevision.from_dict(context.get("revision", {}))
        if not revision.project_id or not revision.path:
            raise BridgeUnavailable("REAPER returned an incomplete project identity")
        tracks = context.get("tracks")
        master = context.get("master")
        if not isinstance(tracks, list) or not tracks or not isinstance(master, dict):
            raise BridgeUnavailable("REAPER returned an incomplete project map")
        project_length_s = float(context.get("length_s") or 0.0)
        tracks_by_guid = {str(track.get("guid")): track for track in tracks
                          if isinstance(track, Mapping) and track.get("guid")}
        master_guid = str(master.get("guid"))
        selection = self._resolve_selection(sources, stage=stage,
                                            track_guids=list(tracks_by_guid),
                                            master_guid=master_guid)
        digest = (self.bridge.project_digest(revision.project_id)
                  if selection.track_guids else {"fields": {"tracks": []}})
        intervals_list = self._intervals(intervals_spec, legacy_section,
                                         project_length_s=project_length_s,
                                         context=context)
        span_start, span_end = union_span(intervals_list)

        for guid in selection.track_guids:
            track = tracks_by_guid.get(guid) or {}
            if not track.get("items") and stage != MEASURE_STAGE_MASTER:
                if not _feeder_guids(digest, [guid]):
                    raise ValidationError(
                        "This source has no items and nothing feeds it; a render would be silent",
                        details={"source_guid": guid,
                                 "next_action": "measure the tracks that send into this bus, or the bus after routing is fixed"})

        workspace_root = self.archive_root / ".incoming"
        workspace_root.mkdir(parents=True, exist_ok=True)
        workspace = AudioArtifactWorkspace(workspace_root)
        render_started = time.perf_counter()
        try:
            assets, current = self._render_assets(
                context=context, revision=revision, selection=selection,
                span=(span_start, span_end), stage=stage, channels=channels,
                sample_rate=sample_rate, tail_ms=tail_ms,
                tracks_by_guid=tracks_by_guid, master_guid=master_guid,
                digest=digest, project_length_s=project_length_s,
                workspace=workspace)
            render_s = time.perf_counter() - render_started

            analysis_started = time.perf_counter()
            results, cache_stats, worker_rows = self._analyze_intervals(
                assets=assets, intervals=intervals_list,
                selection=metric_selection, bands=band_plan,
                analysis_workers=analysis_workers)
            analysis_s = time.perf_counter() - analysis_started

            profile = None
            if step_s is not None:
                if len(intervals_list) != 1 or len(assets) != 1:
                    raise ValidationError(
                        "profile needs exactly one interval and one source",
                        details={"intervals": len(intervals_list),
                                 "sources": len(assets)})
                asset = assets[0]
                interval = intervals_list[0]
                audio = load_audio(str(asset.path),
                                   Section(round(interval.start_s - asset.offset_s, 6),
                                           round(interval.end_s - asset.offset_s, 6)))
                profile = build_profile(audio.samples, int(audio.sample_rate),
                                        step_s=float(step_s),
                                        offset_s=float(interval.start_s),
                                        split_db=split_db,
                                        **(dict(profile_gates or {})))
        finally:
            workspace.close()

        columns = [asset.to_dict() | {"evidence_source_id": asset.evidence_id}
                   for asset in assets]
        rows = [interval.to_dict(index=index)
                for index, interval in enumerate(intervals_list)]
        cells: list[dict[str, Any]] = []
        units: dict[str, str] = {}
        for index, interval in enumerate(intervals_list):
            row: dict[str, Any] = {}
            for asset in assets:
                cell, cell_units = build_cell(
                    results[(index, asset.source_id)]["analysis"],
                    metric_selection, band_plan)
                row[asset.source_id] = cell
                units.update(cell_units)
            cells.append(row)
        emitted = list(metric_selection.keys)
        if metric_selection.bands:
            emitted.append(BANDS_KEY)
            if band_plan.mode != "none":
                emitted.append("band_aggregate")
        table = {
            "metrics": emitted,
            "metric_selection": metric_selection.to_dict(),
            "units": units,
            "bands": band_plan.to_dict(),
            "columns": columns,
            "rows": rows,
            "cells": cells,
            "aggregates": summarize_rows(rows, columns, cells, metric_selection),
        }

        fingerprints = {asset.source_id: asset.fingerprint for asset in assets}
        evidence_units = self._evidence_units(
            intervals_list, columns, results,
            revision=current, fingerprints=fingerprints)
        store = EvidenceStore(self.archive_root)
        unit_ids = store.put_many(evidence_units)
        token = store.mint_token(project_id=revision.project_id,
                                 revision=current.to_dict(), units=unit_ids)

        notes = []
        overlaps = overlapping_pairs(intervals_list)
        if overlaps:
            notes.append({
                "code": "intervals_overlap",
                "pairs": overlaps,
                "detail": ("the render covers the union of the intervals and each "
                           "cell is cut from its own window; the shared audio is "
                           "measured once per interval that contains it"),
            })
        revision_dict = current.to_dict()
        result: dict[str, Any] = {
            "ok": True,
            "operation": "measure",
            "stage": stage,
            "interval": {"start_s": span_start, "end_s": span_end},
            "intervals": [row for row in rows],
            "sources": [asset.to_dict() for asset in assets],
            "source_count": len(assets),
            "table": table,
            "evidence": {"units": len(unit_ids), "token": token["token"],
                         "token_kind": token["kind"]},
            "analysis_cache": cache_stats,
            "analysis_workers": worker_rows,
            "revision": revision_dict,
            "timings": {"render_s": round(render_s, 3),
                        "analysis_s": round(analysis_s, 3),
                        "total_s": round(time.perf_counter() - started, 3)},
            "transport": transport_report,
            "preflight": self._preflight,
            "project_capture_created": False,
            "producer": producer,
        }
        if notes:
            result["notes"] = notes
        if profile is not None:
            if not profile.get("segments"):
                raise ValidationError(
                    "The rendered window is silent; a profile would describe nothing",
                    details={"interval": result["interval"],
                             "sources": [asset.source_id for asset in assets]})
            result["profile"] = profile
        # Measurements are the writes that keep the project state current.  This
        # runs after every check that can refuse the call, so a refused measure
        # never leaves facts behind, and it never rewrites the table above.
        result["state"] = self._record_state(
            project_id=revision.project_id, revision=revision_dict,
            table=table, digest=digest)
        return result

    # ------------------------------------------------------------------ #
    def _intervals(self, spec: Any, legacy_section: tuple[float, float] | None, *,
                   project_length_s: float, context: Mapping[str, Any]
                   ) -> list[MeasureInterval]:
        if legacy_section is not None:
            return [MeasureInterval(start_s=legacy_section[0],
                                    end_s=legacy_section[1],
                                    label="section", origin="section")]
        parsed = parse_intervals(spec, project_length_s=project_length_s,
                                 regions=context.get("regions"),
                                 markers=context.get("markers"),
                                 tempo=context.get("tempo"))
        if parsed:
            return parsed
        if project_length_s <= 0:
            raise ValidationError(
                "REAPER returned no project length; pass explicit intervals")
        return [MeasureInterval(0.0, project_length_s, "all", "all")]

    @staticmethod
    def _needs_render(sources: Any) -> bool:
        """False only when every requested source is an external file."""
        if isinstance(sources, Mapping):
            return not is_external_entry(sources)
        if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes)):
            entries = list(sources)
            return bool(entries) and any(not is_external_entry(entry)
                                         for entry in entries)
        return True

    def _resolve_selection(self, sources: Any, *, stage: str,
                           track_guids: list[str], master_guid: str) -> _Selection:
        if stage == MEASURE_STAGE_MASTER:
            if isinstance(sources, str) and sources in {"all", "master"}:
                raw: list[Any] = ["master"]
            elif isinstance(sources, Sequence) and not isinstance(sources, (str, bytes)):
                raw = list(sources)
            else:
                raise ValidationError("stage=master accepts sources='master' or sources='all'",
                                      details={"sources": sources})
            project: list[str] = []
            external: list[ExternalSource] = []
            for entry in raw:
                if is_external_entry(entry):
                    external.append(resolve_external_source(entry))
                    continue
                text = str(entry)
                if text not in {"master", "master:" + master_guid}:
                    raise ValidationError(
                        "stage=master accepts sources='master' or sources='all'",
                        details={"sources": sources})
                if master_guid not in project:
                    project.append(master_guid)
            if not project and not external:
                raise ValidationError("sources must not be empty")
            return _Selection(tuple(project), self._unique_external(external))

        if isinstance(sources, str):
            if sources == "all":
                raw = list(track_guids)
            elif sources == "dirty":
                raise ValidationError("sources='dirty' needs a freshness map; "
                                      "pass explicit source ids")
            else:
                raw = [sources]
        elif isinstance(sources, Sequence) and not isinstance(sources, (str, bytes)):
            raw = list(sources)
        elif is_external_entry(sources):
            raw = [sources]
        else:
            raise ValidationError("sources must be a list of track ids, 'all' or 'master'",
                                  details={"sources": sources})
        resolved: list[str] = []
        external = []
        for entry in raw:
            if is_external_entry(entry):
                external.append(resolve_external_source(entry))
                continue
            text = str(entry)
            if text.startswith("track:"):
                text = text.split(":", 1)[1]
            if text not in track_guids:
                raise ValidationError("Unknown source id for this project",
                                      details={"source": entry})
            if text not in resolved:
                resolved.append(text)
        if not resolved and not external:
            raise ValidationError("sources must not be empty")
        return _Selection(tuple(resolved), self._unique_external(external))

    @staticmethod
    def _unique_external(external: Sequence[ExternalSource]) -> tuple[ExternalSource, ...]:
        seen: dict[str, ExternalSource] = {}
        for source in external:
            seen.setdefault(source.source_id, source)
        return tuple(seen.values())

    # ------------------------------------------------------------------ #
    def _snapshot_online(self, *, repair: bool) -> dict[str, Any]:
        """Snapshot the project and prove every unmuted audio source is online.

        The check runs inside the measurement path, immediately before the
        render, so a render can never start from a state the capture path
        would have refused.
        """
        context = self.session.snapshot(include_items=True, include_fx_params=True,
                                        include_midi_notes=False)
        rows = unresolved_rows(context.get("tracks") or [])
        if not rows:
            return context
        preflight: dict[str, Any]
        if repair:
            preflight = ensure_media_online(self.session)
        else:
            preflight = {"ok": False, "recovered": False, "attempts": 0,
                         "unresolved": rows}
        self._preflight = preflight
        context = self.session.snapshot(include_items=True, include_fx_params=True,
                                        include_midi_notes=False)
        remaining = unresolved_rows(context.get("tracks") or [])
        if remaining:
            raise MediaUnresolved(
                "Some take sources could not be resolved; the measurement render would be silent",
                details={"sources": remaining, "preflight": preflight})
        return context

    @contextmanager
    def _transport_controlled(self, context: Mapping[str, Any], *,
                              auto_transport: bool, needed: bool
                              ) -> Iterator[dict[str, Any]]:
        """Stop playback when asked, and always put it back the way it was."""
        report: dict[str, Any] = {
            "auto_transport": auto_transport, "needed": needed,
            "stopped": False, "restored": False, "position_s": None,
            "play_state_before": None,
        }
        if not needed:
            yield report
            return
        transport = context.get("transport")
        if not isinstance(transport, Mapping):
            raise BridgeUnavailable("REAPER returned no transport state")
        state = int(transport.get("play_state") or 0)
        report["play_state_before"] = state
        cursor = transport.get("cursor_s", transport.get("edit_cursor_s"))
        if cursor is not None:
            report["position_s"] = float(cursor)
        if state == 0:
            yield report
            return
        if not auto_transport:
            raise ValidationError(
                "Stop playback before measuring",
                details={"transport": transport,
                         "next_action": ("pass auto_transport=true to stop, measure "
                                         "and restore the position and play state")})
        if state & PLAY_STATE_RECORDING:
            raise ValidationError(
                "auto_transport refuses to stop a recording",
                details={"transport": transport})
        self.bridge.call_api("Main_OnCommand", TRANSPORT_STOP, 0, timeout_s=10.0)
        current: Mapping[str, Any] = transport
        for _attempt in range(TRANSPORT_STOP_ATTEMPTS):
            time.sleep(TRANSPORT_STOP_POLL_S)
            observed = self.session.snapshot(include_items=False,
                                             include_fx_params=False).get("transport")
            if isinstance(observed, Mapping):
                current = observed
                if int(observed.get("play_state") or 0) == 0:
                    break
        else:
            raise ValidationError(
                "REAPER did not stop playback for the measurement",
                details={"transport": dict(current)})
        report["stopped"] = True
        try:
            yield report
        finally:
            errors: list[str] = []
            if report["position_s"] is not None:
                try:
                    self.bridge.call_api("SetEditCurPos",
                                         float(report["position_s"]), True, False,
                                         timeout_s=10.0)
                except Exception as exc:  # noqa: BLE001 - restoration must not mask the body
                    errors.append(f"SetEditCurPos: {exc}")
            try:
                if state & PLAY_STATE_PLAYING:
                    self.bridge.call_api("Main_OnCommand", TRANSPORT_PLAY, 0,
                                         timeout_s=10.0)
                elif state & PLAY_STATE_PAUSED:
                    self.bridge.call_api("Main_OnCommand", TRANSPORT_PLAY, 0,
                                         timeout_s=10.0)
                    self.bridge.call_api("Main_OnCommand", TRANSPORT_PAUSE, 0,
                                         timeout_s=10.0)
            except Exception as exc:  # noqa: BLE001 - see above
                errors.append(f"transport restore: {exc}")
            report["restored"] = not errors
            if errors:
                report["restore_errors"] = errors

    # ------------------------------------------------------------------ #
    def _render_assets(self, *, context: Mapping[str, Any], revision: ProjectRevision,
                       selection: _Selection, span: tuple[float, float], stage: str,
                       channels: str, sample_rate: int | None, tail_ms: int,
                       tracks_by_guid: Mapping[str, Mapping[str, Any]],
                       master_guid: str, digest: Mapping[str, Any],
                       project_length_s: float,
                       workspace: AudioArtifactWorkspace
                       ) -> tuple[list[_Asset], ProjectRevision]:
        """Render every project source once over the union span."""
        span_start, span_end = span
        assets: list[_Asset] = []
        current = revision
        cache = RenderCache(self.archive_root / "renders")
        if not selection.track_guids:
            for source in selection.external:
                assets.append(self._external_asset(source))
            return assets, current
        fingerprints = self._render_fingerprints(
            project_id=revision.project_id, digest=digest,
            track_guids=[guid for guid in selection.track_guids
                         if guid != master_guid],
            master_guid=master_guid, start_s=span_start, end_s=span_end,
            project_length_s=project_length_s)

        cached = None
        if len(selection.track_guids) == 1 and stage != MEASURE_STAGE_MASTER:
            cached = cache.lookup(project_id=revision.project_id,
                                  revision=revision.to_dict(),
                                  source_guid=selection.track_guids[0],
                                  start_s=span_start, end_s=span_end, stage=stage,
                                  channels=channels, sample_rate=sample_rate,
                                  tail_ms=tail_ms)
        if cached is not None:
            guid = selection.track_guids[0]
            meta = cached["meta"]
            track = tracks_by_guid.get(guid) or {}
            assets.append(_Asset(
                source_id=guid, evidence_id=f"track:{guid}",
                name=str(track.get("name") or ""), kind="track",
                stage=TRACK_SIGNAL_STAGE, path=Path(str(cached["path"])),
                offset_s=span_start,
                duration_s=float(meta["frames"]) / float(meta["rate"]),
                sample_rate=int(meta["rate"]), channels=int(meta["channels_count"]),
                frames=int(meta["frames"]),
                fingerprint=fingerprints.get(guid)))
        elif stage == MEASURE_STAGE_MASTER:
            rendered = self._render_master_only(current, workspace,
                                                f"{workspace.job_id}-master",
                                                span_start, span_end, channels,
                                                sample_rate, tail_ms)
            current = ProjectRevision.from_dict(rendered["revision_after"])
            item = self._one_file(rendered, workspace)
            info = self._check_rendered_wav(Path(str(item["path"])), rendered)
            assets.append(_Asset(
                source_id=master_guid, evidence_id=f"master:{master_guid}",
                name="MASTER", kind="master", stage=MASTER_SIGNAL_STAGE,
                path=Path(str(item["path"])), offset_s=span_start,
                duration_s=float(info.frames) / float(info.samplerate),
                sample_rate=int(info.samplerate),
                channels=int(info.channels),
                frames=int(info.frames),
                fingerprint=fingerprints.get(master_guid), is_master=True))
        else:
            feeders = _feeder_guids(digest, selection.track_guids)
            batch_guids = list(selection.track_guids) + feeders
            rendered = self._render_batch(
                current, workspace, f"{workspace.job_id}-measure",
                len(batch_guids), span_start, span_end, channels, sample_rate,
                tail_ms,
                track_guids=(None if len(batch_guids) == len(tracks_by_guid)
                             else batch_guids))
            current = ProjectRevision.from_dict(rendered["revision_after"])
            admitted = self._admit_track_batch(rendered, batch_guids,
                                               tracks_by_guid, workspace)
            admitted = [row for row in admitted
                        if str(row["source_guid"]) in set(selection.track_guids)]
            for row in admitted:
                guid = str(row["source_guid"])
                assets.append(_Asset(
                    source_id=guid, evidence_id=f"track:{guid}",
                    name=str(row.get("name") or ""), kind="track",
                    stage=TRACK_SIGNAL_STAGE, path=Path(str(row["path"])),
                    offset_s=span_start,
                    duration_s=float(row.get("frames") or 0) / float(row.get("sample_rate") or 1),
                    sample_rate=int(row.get("sample_rate") or 0),
                    channels=int(row.get("channels") or 0),
                    frames=int(row.get("frames") or 0),
                    fingerprint=fingerprints.get(guid)))
            if len(selection.track_guids) == 1 and assets:
                row = admitted[0] if admitted else None
                if row is not None:
                    try:
                        cache.store(project_id=current.project_id,
                                    revision=current.to_dict(),
                                    source_guid=selection.track_guids[0],
                                    start_s=span_start, end_s=span_end,
                                    stage=stage, channels=channels,
                                    sample_rate=sample_rate, tail_ms=tail_ms,
                                    source_wav=Path(str(row["path"])),
                                    frames=int(row.get("frames") or 0),
                                    rate=int(row.get("sample_rate") or 0),
                                    channels_count=int(row.get("channels") or 0))
                    except OSError:
                        pass
        for source in selection.external:
            assets.append(self._external_asset(source))
        return assets, current

    @staticmethod
    def _external_asset(source: ExternalSource) -> _Asset:
        return _Asset(source_id=source.source_id, evidence_id=source.evidence_id,
                      name=source.path.stem, kind="file",
                      stage=EXTERNAL_SIGNAL_STAGE, path=source.path,
                      offset_s=0.0, duration_s=source.duration_s,
                      sample_rate=source.sample_rate, channels=source.channels,
                      frames=source.frames,
                      fingerprint=f"sha256:{source.sha256}")

    # ------------------------------------------------------------------ #
    def _window_payload(self, interval: MeasureInterval, asset: _Asset
                        ) -> tuple[str, str, float, float, float]:
        offset = round(interval.start_s - asset.offset_s, 6)
        if offset < -1e-6:
            raise ValidationError(
                "The interval starts before the source audio",
                details={"source_id": asset.source_id,
                         "interval": interval.to_dict(),
                         "audio_start_s": asset.offset_s})
        return (asset.source_id, str(asset.path), max(0.0, offset),
                interval.start_s, interval.end_s)

    @staticmethod
    def _profile_gates(options: Mapping[str, Any] | None
                       ) -> dict[str, float] | None:
        """Validate caller overrides for the profile gates.

        None means "use the declared defaults"; every accepted key is a keyword
        argument of ``daw.profile.build_profile`` and is echoed back in
        ``profile.thresholds``.
        """
        if options is None:
            return None
        if not isinstance(options, Mapping):
            raise ValidationError("profile_options must be a mapping",
                                  details={"profile_options": options})
        unknown = sorted(set(options) - set(PROFILE_GATE_KEYS))
        if unknown:
            raise ValidationError("Unknown profile option",
                                  details={"unknown": unknown,
                                           "allowed": list(PROFILE_GATE_KEYS)})
        resolved: dict[str, float] = {}
        for key, raw in options.items():
            number = _number(raw, f"profile_options.{key}")
            if key == "floor_percentile" and not 0.0 <= number <= 100.0:
                raise ValidationError("profile_options.floor_percentile must be 0-100",
                                      details={"value": number})
            if key in {"active_margin_db", "relative_ceiling_db"} and number < 0:
                raise ValidationError(f"profile_options.{key} must not be negative",
                                      details={"value": number})
            if key == "min_segment_s" and number <= 0:
                raise ValidationError("profile_options.min_segment_s must be positive",
                                      details={"value": number})
            resolved[key] = number
        return resolved

    def _window_cache(self, interval: MeasureInterval,
                      selection: MetricSelection, bands: BandPlan
                      ) -> MeasurementCache | None:
        if not contract_v2.analysis_cache_enabled():
            return None
        return MeasurementCache(
            self.archive_root,
            section={"start_s": float(interval.start_s),
                     "end_s": float(interval.end_s)},
            options=cache_options(selection, bands))

    def _analyze_intervals(self, *, assets: Sequence[_Asset],
                           intervals: Sequence[MeasureInterval],
                           selection: MetricSelection, bands: BandPlan,
                           analysis_workers: int | None
                           ) -> tuple[dict[tuple[int, str], dict[str, Any]],
                                      dict[str, Any] | None,
                                      list[dict[str, Any]]]:
        results: dict[tuple[int, str], dict[str, Any]] = {}
        cache_stats: list[dict[str, Any]] = []
        worker_rows: list[dict[str, Any]] = []
        stage_by_id = {asset.source_id: asset.stage for asset in assets}
        fingerprint_by_id = {asset.source_id: asset.fingerprint or ""
                             for asset in assets}
        for index, interval in enumerate(intervals):
            payloads = [(asset.source_id, str(asset.path)) for asset in assets]
            plan = resolve_analysis_workers(asset_count=len(payloads),
                                            window_s=interval.duration_s,
                                            requested=analysis_workers)
            worker_rows.append({"interval": index, **plan.to_dict()})
            cache = self._window_cache(interval, selection, bands)
            if cache is None:
                measured = analyze_windows(
                    [self._window_payload(interval, asset) for asset in assets],
                    plan.workers)
            else:
                hits, misses = split_cached_payloads(payloads, stage_by_id, cache,
                                                     fingerprint_by_id)
                missing_ids = {key for key, _path in misses}
                measured = analyze_windows(
                    [self._window_payload(interval, asset) for asset in assets
                     if asset.source_id in missing_ids],
                    plan.workers)
                measured = merge_cached_results(payloads, hits, measured)
                store_measured_results(payloads, stage_by_id, measured, cache)
                cache_stats.append(cache.stats())
            for row in measured:
                results[(index, str(row["source_guid"]))] = row
        return results, self._merge_cache_stats(cache_stats), worker_rows

    @staticmethod
    def _merge_cache_stats(stats_rows: Sequence[Mapping[str, Any]]
                           ) -> dict[str, Any] | None:
        if not stats_rows:
            return None
        merged: dict[str, Any] = {
            "enabled": True, "hits": 0, "misses": 0, "fingerprint_reuse": 0,
            "stored": 0, "reused_dsp_s": 0.0, "errors": [], "root": None,
            "windows": len(stats_rows),
        }
        for row in stats_rows:
            for key in ("hits", "misses", "fingerprint_reuse", "stored"):
                merged[key] += int(row.get(key) or 0)
            merged["reused_dsp_s"] += float(row.get("reused_dsp_s") or 0.0)
            merged["errors"].extend(row.get("errors") or [])
            if merged["root"] is None and row.get("root"):
                merged["root"] = row.get("root")
        merged["reused_dsp_s"] = round(merged["reused_dsp_s"], 3)
        return merged

    @staticmethod
    def _evidence_units(intervals: Sequence[MeasureInterval],
                        columns: Sequence[Mapping[str, Any]],
                        results: Mapping[tuple[int, str], Mapping[str, Any]],
                        *, revision: ProjectRevision,
                        fingerprints: Mapping[str, str | None]) -> list[dict[str, Any]]:
        units: list[dict[str, Any]] = []
        for index, interval in enumerate(intervals):
            window = {"start_s": float(interval.start_s),
                      "end_s": float(interval.end_s)}
            for column in columns:
                source_id = str(column["source_id"])
                result = results.get((index, source_id))
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
                        "source_id": str(column.get("evidence_source_id") or source_id),
                        "interval": dict(window),
                        "metric": str(metric),
                        "method": str(provenance.get("analysis") or contract_v2.ANALYZER),
                        "method_version": method_version,
                        "stage": str(column.get("stage") or ""),
                        "measurement_stage": str(provenance.get("stage") or ""),
                        "sample_rate": int(result.get("sample_rate") or 0),
                        "channels": int(result.get("channels") or 0),
                        "value": float(value),
                        "unit": str(raw.get("unit") if isinstance(raw, Mapping) else ""),
                        "fingerprint": fingerprints.get(source_id),
                        "project_id": revision.project_id,
                        "revision": {"change_count": revision.change_count,
                                     "epoch": revision.epoch,
                                     "instance_id": revision.instance_id},
                        "capture_id": None,
                    })
        return units


__all__ = ["MeasureService", "MEASURE_STAGE_MASTER", "MEASURE_STAGE_VIA_MASTER",
           "MEASURE_STAGE_PREMASTER", "cache_options"]
