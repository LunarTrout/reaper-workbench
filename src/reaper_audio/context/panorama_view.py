"""Single-response model view of a capture panorama.

The canonical panorama document (``context/panorama.py``) is the archival
sidecar: rich, self-describing, and far too large for one MCP tool result on a
real song (1.35 MB on a 41-track capture, of which relation edges alone are
841 KB).  This module projects it into one *compact columnar view* that fits a
single ordinary MCP tool result:

* every domain of the canonical document is inventoried and appears exactly
  once -- as a table, a descriptor, or a catalogued archive family;
  ``SECTION_INVENTORY`` is exhaustive and a test fails if the canonical document
  grows a key that the view does not place;
* repeated definitions are stated once: units, column meanings, per-table
  contracts, stage tables, measurement windows and the time axis;
* object identities live in one ``entities`` dictionary, so a GUID appears once
  in the whole response instead of once per row and per reference.  Stable ids
  stay derivable (``track:{guid}``, ``analysis:{source_guid}``) and remain the
  currency of ``reaper_panorama_query``;
* high-frequency exact data (relation edges and pairs, exact series cells, MIDI
  notes, envelope points, the raw project map, exact unrounded measurements) is
  neither averaged away nor inlined: it stays in the sealed archive, is
  catalogued with its measured byte size, and is returned whole by one generic
  semantic query;
* nothing is invented: the view adds no role, genre, quality or cause.

Field-level precision is declared, never implicit: inline metric values are
rounded to ``INLINE_METRIC_DECIMALS`` and inline level statistics to
``SUMMARY_DECIMALS`` so a real project fits one response; the exact stored
values are one query away and the rounding is written into ``reading.precision``.

Determinism: the projection is a pure function of the document, so the same
document always yields the same ``view_sha256``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from reaper_audio.errors import ValidationError
from .relations import RELATION_KINDS

VIEW_SCHEMA = "reaper-workbench.panorama-view/2"

#: Conservative size of one standard MCP tool result, in bytes of UTF-8 JSON.
#: Basis: below the Hermes 50,000-char inline/spill threshold and within the
#: Codex ~10,000-token output budget at ~4 bytes per token, with envelope
#: margin.  It is a *policy default*, not a measured host contract: a host that
#: knows its own cap passes ``max_response_bytes`` explicitly.
DEFAULT_RESPONSE_BYTES = 48_000
#: A full view above this size is projected to the explicit lean overview:
#: all tracks/master and all metrics stay inline; item/take rows become exact
#: query families with a per-track content inventory.  The margin keeps the
#: measured open envelope under the 48,000-byte policy.
LEAN_OVERVIEW_BYTES = 45_000

#: Decimal places kept for inline metric values.  Exact stored values are in the
#: archive and are returned by ``reaper_panorama_query`` (family ``analysis``).
INLINE_METRIC_DECIMALS = 3

#: Metrics whose meaningful variation is below three decimals.  Keeping this
#: map explicit prevents a non-zero measurement from being rendered as zero.
METRIC_DECIMALS: dict[str, int] = {
    "dc_offset": 8,
    "mono_fold_loss_db": 6,
    "clipped_sample_ratio": 8,
    "stereo_correlation": 4,
    "modulation_depth": 4,
}

#: Decimal places kept for the declared level statistics of the activity grid.
SUMMARY_DECIMALS = 2

#: Families that the query tool serves whole, with the selector keys it accepts.
ARCHIVE_FAMILIES: dict[str, dict[str, Any]] = {
    "relations": {
        "units": {"band_delta_db": "dB", "broadband_delta_db": "dB",
                  "active_fraction": "ratio"},
        "selectors": ["objects", "kinds", "bands_hz", "interval"],
        "note": "every evaluated pair and edge, including unavailable pairs with a reason",
    },
    "series": {
        "units": {"rms_400ms": "dBFS", "band_level": "dBFS",
                  "temporal_profile": "mixed, see series_units"},
        "selectors": ["objects", "series", "interval", "resolution_s"],
        "note": "exact measurement cells, not the level summaries",
    },
    "midi_notes": {
        "units": {"time_s": "s", "pitch": "MIDI note", "velocity": "0..127"},
        "selectors": ["objects", "interval"],
        "note": "every note of every captured MIDI take",
    },
    "envelope_points": {
        "units": {"time_s": "s", "value": "normalised 0..1"},
        "selectors": ["objects", "interval"],
        "note": "every automation/envelope point of the selected tracks",
    },
    "items": {"units": {"position_s": "s", "length_s": "s"},
              "selectors": ["objects", "interval"],
              "note": "every media item with geometry at full precision"},
    "takes": {"units": {}, "selectors": ["objects"],
              "note": "every take and its source media path"},
    "audio": {"units": {"duration_s": "s", "frames": "samples"},
              "selectors": ["objects"],
              "note": "every captured audio asset and its mapping"},
    "fx": {"units": {}, "selectors": ["objects", "interval"],
            "note": "every captured FX instance and its exact parameter rows"},
    "analysis": {"units": {"*": "see analysis metric units"},
                 "selectors": ["objects", "metrics"],
                 "note": "every measurement row with exact unrounded values"},
    "project_summary": {"units": {}, "selectors": [],
                        "note": "the project/scene summary; the raw REAPER project map is not exposed"},
}

#: Every top-level key of a built document, and where it goes in the view.
SECTION_INVENTORY: dict[str, str] = {
    "schema_version": "binding",
    "document_type": "binding",
    "capture_binding": "binding",
    "provenance": "binding",
    "identity": "binding",
    "evidence_freshness": "binding",
    "render_manifest": "binding",
    "structural_facts": "binding",
    "document_sha256": "binding",
    "project": "project",
    "tracks": "tracks",
    "master": "tracks",
    "items": "items",
    "takes": "takes",
    "midi": "midi",
    "fx": "fx",
    "routes": "routes",
    "automation": "automation",
    "envelopes": "envelopes",
    "time_map": "time",
    "tempo": "time",
    "time_signatures": "time",
    "markers": "time",
    "regions": "time",
    "selection": "transport",
    "transport": "transport",
    "audio": "audio",
    "analysis": "analysis",
    "analysis_axis": "analysis",
    "analysis_contract": "analysis",
    "analysis_metric_units": "analysis",
    "analysis_archive": "archive",
    "analysis_temporal": "temporal",
    "audio_capture_revisions": "audio",
    "audio_capture_revision_contract": "audio",
    "audio_time_contract": "audio",
    "relations": "relations",
    "relations_summary": "relations",
    "crosslinks": "derived",
    "per_track": "derived",
    "coverage": "coverage",
    "truncation": "coverage",
    "domain_counts": "coverage",
    "epistemic_layers": "reading",
    "legend": "reading",
    "reading_contract": "reading",
    "crosslink_contract": "derived",
    "details": "archive",
}

DERIVED_SECTIONS = {
    "crosslinks": ("derived_from", "tables",
                   "every crosslink is one FK column: items.track, items.take, "
                   "takes.item, takes.track, midi.take, fx.owner, routes.from/to, "
                   "automation.owner, envelopes.owner, audio.source, analysis.source"),
    "per_track": ("derived_from", "tracks",
                  "tracks.fx_n / item_n and the per-table row counts"),
    "crosslink_contract": ("replaced_by", "reading.tables",
                           "FK columns carry the same from/relation/to semantics"),
}


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------

def _json_default(value: Any) -> Any:
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"), default=_json_default).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _n(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def nz(value: Any, decimals: int | None = None) -> float | int | None:
    """A JSON number: ints stay ints, floats round only where declared."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    number = _n(value)
    if number is None:
        return None
    if decimals is not None:
        number = round(number, decimals)
        if number == int(number) and abs(number) < 1e15:
            return int(number)
    return number


def table(rows: Sequence[Mapping[str, Any]], columns: Sequence[str],
          *, note: str | None = None,
          decimals: Mapping[str, int] | None = None) -> dict[str, Any]:
    """A shared-schema table: one column legend, one array per row."""
    decimals = decimals or {}
    payload: dict[str, Any] = {
        "columns": list(columns),
        "rows": [[nz(row.get(column), decimals.get(column))
                  if isinstance(row.get(column), (int, float))
                  and not isinstance(row.get(column), bool)
                  else row.get(column) for column in columns] for row in rows],
    }
    if note:
        payload["note"] = note
    return payload


def _rows(value: Any, key: str = "envelopes") -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        value = value.get(key)
    return [row for row in value or [] if isinstance(row, Mapping)]


def section_bytes(value: Any) -> int:
    return len(canonical(value))


# --------------------------------------------------------------------------
# identity: one entity dictionary, integer references everywhere else
# --------------------------------------------------------------------------

class Entities:
    """One place where every object GUID and name appears."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        self.tracks = [row for row in document.get("tracks") or [] if isinstance(row, Mapping)]
        master = document.get("master")
        self.master = master if isinstance(master, Mapping) else None
        self.items = [row for row in document.get("items") or [] if isinstance(row, Mapping)]
        self.takes = [row for row in document.get("takes") or [] if isinstance(row, Mapping)]
        self.audio = [row for row in document.get("audio") or [] if isinstance(row, Mapping)]
        self.analysis = [row for row in document.get("analysis") or [] if isinstance(row, Mapping)]
        self.track_index: dict[str, int] = {
            str(row.get("stable_id")): index for index, row in enumerate(self.tracks)}
        if self.master and self.master.get("stable_id"):
            self.track_index[str(self.master.get("stable_id"))] = len(self.tracks)
        self.item_index = {str(row.get("item_id")): index
                           for index, row in enumerate(self.items)}
        self.take_index = {str(row.get("take_id")): index
                           for index, row in enumerate(self.takes)}
        self.audio_index = {str(row.get("audio_id")): index
                            for index, row in enumerate(self.audio)}
        self.analysis_index = {str(row.get("analysis_id")): index
                               for index, row in enumerate(self.analysis)}
        self.source_index: dict[str, int] = {
            str(row.get("stable_id")): index for index, row in enumerate(self.tracks)}
        if self.master and self.master.get("stable_id"):
            self.source_index[str(self.master.get("stable_id"))] = len(self.tracks)
        self.source_guid_index: dict[str, int] = {
            str(row.get("guid")): index for index, row in enumerate(self.tracks)
            if row.get("guid")}
        if self.master and self.master.get("guid"):
            self.source_guid_index[str(self.master.get("guid"))] = len(self.tracks)
        self.source_ids: list[str] = [str(row.get("stable_id")) for row in self.tracks]
        if self.master and self.master.get("stable_id"):
            self.source_ids.append(str(self.master.get("stable_id")))

    def source_index_for(self, source_id: Any, source_guid: Any = None) -> int:
        """Resolve both current source_id and legacy source_guid to one slot."""
        if source_id is not None:
            index = self.source_index.get(str(source_id))
            if index is not None:
                return index
        if source_guid is not None:
            index = self.source_guid_index.get(str(source_guid))
            if index is not None:
                return index
        return -1

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "track": [[row.get("guid"), row.get("name")] for row in self.tracks],
            "item": [[row.get("guid")] for row in self.items],
            "take": [[row.get("guid"), row.get("name")] for row in self.takes],
            "note": ("row i of a table uses entity i of its kind; tracks and master are one "
                     "list (master is the last row of tracks); analysis.source and "
                     "audio.source index that list; stable id = '<kind>:<guid>'"),
        }
        if self.master:
            payload["master"] = [self.master.get("guid"), self.master.get("name")]
        return payload


# --------------------------------------------------------------------------
# tables
# --------------------------------------------------------------------------

def _tracks(document: Mapping[str, Any], entities: Entities) -> dict[str, Any]:
    ordered = list(entities.tracks) + ([entities.master] if entities.master else [])
    rows = []
    for row in ordered:
        mixer = row.get("mixer") if isinstance(row.get("mixer"), Mapping) else {}
        rows.append({
            "ch": mixer.get("channel_count"),
            "folder": mixer.get("folder_depth"),
            "vol": mixer.get("volume"),
            "pan": mixer.get("pan"),
            "mute": 1 if mixer.get("mute") else 0,
            "solo": mixer.get("solo"),
            "sel": 1 if mixer.get("selected") else 0,
            "arm": mixer.get("record_arm"),
            "send_n": mixer.get("send_count"),
            "recv_n": mixer.get("receive_count"),
            "hw_n": mixer.get("hardware_send_count"),
            "fx_n": mixer.get("fx_count"),
            "item_n": mixer.get("item_count", len(row.get("item_ids") or [])),
            "audio_i": entities.audio_index.get(str(row.get("audio_id")), -1),
            "analysis_i": entities.analysis_index.get(str(row.get("analysis_id")), -1),
            "role": row.get("semantic_role"),
        })
    return table(rows, ["ch", "folder", "vol", "pan", "mute", "solo", "sel", "arm",
                        "send_n", "recv_n", "hw_n", "fx_n", "item_n", "audio_i",
                        "analysis_i", "role"],
                 decimals={"vol": 4, "pan": 4},
                 note=("row order matches entities.track then entities.master; vol/pan are "
                       "REAPER mixer values, not a gain in dB; role is never inferred"))


def _items(document: Mapping[str, Any], entities: Entities) -> dict[str, Any]:
    rows = []
    for row in entities.items:
        interval = row.get("interval") if isinstance(row.get("interval"), Mapping) else {}
        rows.append({
            "track": entities.track_index.get(str(row.get("track_id")), -1),
            "start_s": interval.get("start_s", row.get("position_s")),
            "end_s": interval.get("end_s"),
            "pos_s": row.get("position_s"),
            "len_s": row.get("length_s"),
            "off_s": row.get("start_offset_s"),
            "rate": row.get("playrate"),
            "mute": 1 if row.get("mute") else 0,
            "loop": 1 if row.get("loop_source") else 0,
            "fade_in": row.get("fade_in_s"),
            "fade_out": row.get("fade_out_s"),
            "ch_mode": row.get("channel_mode"),
            "take_n": row.get("take_count"),
            "take": entities.take_index.get(str(row.get("active_take_id")), -1),
        })
    return table(rows, ["track", "start_s", "end_s", "pos_s", "len_s", "off_s", "rate",
                        "mute", "loop", "fade_in", "fade_out", "ch_mode", "take_n", "take"],
                 decimals={"start_s": 6, "end_s": 6, "pos_s": 6, "len_s": 6, "off_s": 6,
                           "rate": 6, "fade_in": 6, "fade_out": 6},
                 note="row order matches entities.item; time is project seconds, [start,end)")


def _takes(document: Mapping[str, Any], entities: Entities) -> dict[str, Any]:
    rows = []
    for row in entities.takes:
        rows.append({
            "item": entities.item_index.get(str(row.get("item_id")), -1),
            "track": entities.track_index.get(str(row.get("track_id")), -1),
            "midi": 1 if row.get("is_midi") else 0,
            "rate": row.get("playrate"),
            "pitch": 1 if row.get("preserve_pitch") else 0,
            "stretch_n": row.get("stretch_marker_count"),
            "fx_n": row.get("fx_count"),
            "ch_mode": row.get("channel_mode"),
            "group": row.get("group_id"),
        })
    return table(rows, ["item", "track", "midi", "rate", "pitch", "stretch_n", "fx_n",
                        "ch_mode", "group"],
                 decimals={"rate": 6},
                 note=("row order matches entities.take; rate/pitch/stretch_n/fx_n/ch_mode/"
                       "group are captured values, not inferred; the source media path and "
                       "takes with no captured field stay in archive family takes"))


def _midi(document: Mapping[str, Any], entities: Entities) -> dict[str, Any]:
    rows = []
    for row in document.get("midi") or []:
        if not isinstance(row, Mapping):
            continue
        interval = row.get("interval") if isinstance(row.get("interval"), Mapping) else {}
        rows.append({
            "take": entities.take_index.get(str(row.get("take_id")), -1),
            "note_n": row.get("note_count"),
            "start_s": interval.get("start_s"),
            "end_s": interval.get("end_s"),
        })
    return table(rows, ["take", "note_n", "start_s", "end_s"],
                 decimals={"start_s": 6, "end_s": 6},
                 note="notes are exact in archive family midi_notes (address: take id)")


def _fx(document: Mapping[str, Any], entities: Entities) -> dict[str, Any]:
    rows = []
    for row in document.get("fx") or []:
        if not isinstance(row, Mapping):
            continue
        params = [param for param in row.get("parameters") or [] if isinstance(param, Mapping)]
        inline = len(params) <= 4
        rows.append({
            "owner": entities.track_index.get(str(row.get("owner_id")), -1),
            "slot": row.get("slot_index"),
            "name": row.get("name"),
            "on": 1 if row.get("enabled") else 0,
            "offline": 1 if row.get("offline") else 0,
            "param_n": row.get("parameter_count", len(params)),
            "params": ([[param.get("index"), param.get("name"),
                         nz(param.get("normalized"), 4), param.get("formatted")]
                        for param in params] if inline else []),
            "params_omitted": 0 if inline else len(params),
        })
    return table(rows, ["owner", "slot", "name", "on", "offline", "param_n", "params",
                        "params_omitted"],
                 note=("params rows are [index, name, normalized, formatted]; above 4 "
                       "parameters only params_omitted is inline; archive family fx returns "
                       "the exact parameter list"))


def _routes(document: Mapping[str, Any], entities: Entities) -> dict[str, Any]:
    rows = []
    for row in document.get("routes") or []:
        if not isinstance(row, Mapping):
            continue
        rows.append({
            "from": entities.track_index.get(str(row.get("source_id")), -1),
            "to": entities.track_index.get(str(row.get("destination_id")), -1),
            "send": row.get("send_index"),
            "vol": row.get("volume"),
            "pan": row.get("pan"),
            "mode": row.get("mode"),
            "mute": 1 if row.get("mute") else 0,
            "kind": row.get("kind"),
        })
    return table(rows, ["from", "to", "send", "vol", "pan", "mode", "mute", "kind"],
                 decimals={"vol": 4, "pan": 4},
                 note="routing is directional: from is the source, to the destination")


def _envelopes(document: Mapping[str, Any], key: str,
               entities: Entities) -> dict[str, Any]:
    container = document.get(key)
    rows = []
    for row in _rows(container, "envelopes"):
        interval = row.get("interval") if isinstance(row.get("interval"), Mapping) else {}
        rows.append({
            "owner": entities.track_index.get(str(row.get("owner_id")), -1),
            "kind": row.get("kind") or row.get("envelope"),
            "name": row.get("name") or row.get("parameter"),
            "point_n": row.get("point_count", row.get("points")),
            "start_s": interval.get("start_s"),
            "end_s": interval.get("end_s"),
        })
    meta = container if isinstance(container, Mapping) else {}
    return table(rows, ["owner", "kind", "name", "point_n", "start_s", "end_s"],
                 decimals={"start_s": 6, "end_s": 6},
                 note=(f"points are exact in archive family envelope_points; "
                       f"complete={meta.get('complete')}, "
                       f"items_scanned={meta.get('items_scanned')}, "
                       f"gaps={len(meta.get('gaps') or [])}"))


def _audio(document: Mapping[str, Any], entities: Entities) -> dict[str, Any]:
    revisions = document.get("audio_capture_revisions")
    revision_keys = sorted(revisions) if isinstance(revisions, Mapping) else []
    revision_index = {key: position for position, key in enumerate(revision_keys)}
    stages: list[list[Any]] = []
    stage_index: dict[str, int] = {}
    rows = []
    for row in entities.audio:
        fmt = row.get("format") if isinstance(row.get("format"), Mapping) else {}
        mapping = row.get("mapping") if isinstance(row.get("mapping"), Mapping) else {}
        prov = row.get("render_provenance") if isinstance(row.get("render_provenance"), Mapping) else {}
        key = f"{prov.get('stage')}|{prov.get('capture_method')}|{prov.get('complete')}"
        if key not in stage_index:
            stage_index[key] = len(stages)
            stages.append([prov.get("stage"), prov.get("capture_method"),
                           1 if prov.get("complete") else 0])
        rows.append({
            "source": entities.source_index.get(str(row.get("source_id")), -1),
            "master": 1 if row.get("is_master") else 0,
            "stage": stage_index[key],
            "rev": revision_index.get(str(prov.get("capture_revision_sha256")), -1),
            "rev_change": prov.get("capture_revision_change_count"),
            "frames": fmt.get("frame_count"),
            "sr": fmt.get("sample_rate_hz"),
            "ch": fmt.get("channel_count"),
            "dur_s": fmt.get("duration_s"),
            "dur_ok": 1 if fmt.get("duration_consistent") else 0,
            "mapped_end": mapping.get("mapped_file_end_s"),
            "tail_n": mapping.get("tail_sample_count"),
            "tail": 1 if mapping.get("tail_present") else 0,
            "sha256_16": str(row.get("sha256") or "")[:16],
            "analysis_i": entities.analysis_index.get(str(row.get("analysis_id")), -1),
        })
    payload = table(rows, ["source", "master", "stage", "rev", "rev_change",
                           "sr", "ch", "dur_s", "dur_ok", "tail", "sha256_16",
                           "analysis_i"],
                    decimals={"dur_s": 3},
                    note=("one row per captured asset; source indexes the track list; "
                          "stage indexes audio.stages, rev indexes audio.revisions; "
                          "sha256_16 is the first 16 hex characters of the archived "
                          "blob SHA-256 (identity prefix only); exact frame count, tail "
                          "samples and sample mapping are in archive family audio"))
    payload["stages"] = {"columns": ["stage", "method", "complete"], "rows": stages}
    payload["revisions"] = [[key, revisions[key]] for key in revision_keys]
    return payload


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------

def _analysis(document: Mapping[str, Any], entities: Entities, *,
              round_values: bool = True) -> dict[str, Any]:
    units = document.get("analysis_metric_units")
    units = dict(units) if isinstance(units, Mapping) else {}
    # Declare every metric family the capture contract knows, not only the ones
    # non-null in this capture: an absent value is a null in a declared column
    # and is counted in ``absent``, never a silently missing column.
    names = sorted(units) or sorted({name for row in entities.analysis
                                     for name in (row.get("metrics") or {})})
    legacy_columns = [str(name) for name in document.get("analysis_metric_columns") or []]
    has_legacy_rows = any(isinstance(row.get("metrics"), (list, tuple))
                          for row in entities.analysis)
    if has_legacy_rows:
        # The archived capture predates the named-metric object.  Its rows carry
        # a positional list and the shared `analysis_metric_columns` names; use
        # those names instead of silently reading every value as absent.
        if not legacy_columns:
            raise ValidationError(
                "Legacy analysis rows have no analysis_metric_columns names",
                details={"rows": len(entities.analysis)})
        names = legacy_columns
    legacy_stage = "legacy_unspecified"
    windows: list[list[Any]] = []
    window_index: dict[str, int] = {}
    stages: list[str] = []
    stage_index: dict[str, int] = {}
    channels: list[str] = []
    channel_index: dict[str, int] = {}
    signal_stage_by_source: dict[str, str] = {}
    for audio in entities.audio:
        provenance = audio.get("render_provenance")
        if isinstance(provenance, Mapping) and audio.get("source_id"):
            signal_stage_by_source[str(audio["source_id"])] = str(provenance.get("stage") or "")
    signal_stages: list[str] = []
    signal_stage_index: dict[str, int] = {}
    rows = []
    stats = []
    absent: Counter[str] = Counter()
    rounded_to_zero: list[list[Any]] = []
    decimals = {name: METRIC_DECIMALS.get(name, INLINE_METRIC_DECIMALS) for name in names}
    for row in entities.analysis:
        raw_metrics = row.get("metrics")
        legacy_row = isinstance(raw_metrics, (list, tuple))
        if isinstance(raw_metrics, Mapping):
            metrics = raw_metrics
        elif legacy_row:
            if len(raw_metrics) != len(names):
                raise ValidationError(
                    "Legacy analysis metric list does not match its column names",
                    details={"expected": len(names), "received": len(raw_metrics),
                             "columns": names})
            metrics = dict(zip(names, raw_metrics))
        else:
            metrics = {}
        window = row.get("measured_window") if isinstance(row.get("measured_window"), Mapping) else {}
        key = f"{window.get('start_s')}|{window.get('end_s')}"
        if key not in window_index:
            window_index[key] = len(windows)
            windows.append([nz(window.get("start_s"), 6), nz(window.get("end_s"), 6)])
        stage = str(row.get("stage") or (legacy_stage if legacy_row else ""))
        if stage not in stage_index:
            stage_index[stage] = len(stages)
            stages.append(stage)
        channel = str(row.get("channel_view") or (legacy_stage if legacy_row else ""))
        if channel not in channel_index:
            channel_index[channel] = len(channels)
            channels.append(channel)
        signal_stage = signal_stage_by_source.get(str(row.get("source_id")), "")
        if legacy_row and not signal_stage:
            signal_stage = legacy_stage
        if signal_stage not in signal_stage_index:
            signal_stage_index[signal_stage] = len(signal_stages)
            signal_stages.append(signal_stage)
        source_index = entities.source_index_for(
            row.get("source_id"), row.get("source_guid"))
        if source_index < 0:
            raise ValidationError(
                "Analysis row does not resolve to a captured track/master",
                details={"analysis_id": row.get("analysis_id"),
                         "source_id": row.get("source_id"),
                         "source_guid": row.get("source_guid")})
        values = []
        for name in names:
            raw = metrics.get(name)
            if raw is None or not isinstance(raw, (int, float)):
                absent[name] += 1
                values.append(None)
            else:
                if round_values:
                    rounded = nz(raw, decimals[name])
                    if rounded == 0 and raw != 0:
                        rounded_to_zero.append([source_index, name, raw])
                else:
                    rounded = nz(raw, None)
                values.append(rounded)
        available = row.get("available")
        if available is None:
            available = any(value is not None for value in values)
        rows.append([source_index,
                     1 if available else 0,
                     stage_index[stage], signal_stage_index[signal_stage],
                     window_index[key], channel_index[channel], *values])
        rms = row.get("rms_stats") if isinstance(row.get("rms_stats"), Mapping) else {}
        if rms:
            stats.append([rms.get("count"), nz(rms.get("min"), SUMMARY_DECIMALS),
                          nz(rms.get("max"), SUMMARY_DECIMALS),
                          nz(rms.get("mean"), SUMMARY_DECIMALS),
                          nz(rms.get("p50"), SUMMARY_DECIMALS)])
    series_rows = []
    series_meta: dict[str, dict[str, Any]] = {}
    for row in entities.analysis:
        series = row.get("series") if isinstance(row.get("series"), Mapping) else {}
        for name, entry in series.items():
            if not isinstance(entry, Mapping):
                continue
            series_rows.append([entities.analysis_index.get(str(row.get("analysis_id")), -1),
                                str(name), entry.get("count")])
            series_meta.setdefault(str(name), {
                key: entry.get(key) for key in
                ("units", "value_kind", "band_count", "window_s", "hop_s",
                 "decimation_step", "cell_fields")
                if entry.get(key) is not None
            })
    method = (document.get("analysis_contract") or {}).get("method")
    method = dict(method) if isinstance(method, Mapping) else {}
    methods = [{
        "analysis": method.get("analysis"),
        "version": method.get("version") or "unversioned",
        "decoder": method.get("decoder"),
        "measurement_stage": "file_or_buffer",
    }]
    metric_methods = {name: 0 for name in names}
    notes = ((document.get("analysis_contract") or {}).get("metric_notes") or [])
    notes = [row for row in notes if isinstance(row, Mapping)] if isinstance(notes, list) else []
    return {
        "metric_columns": names,
        "metric_schema": ("legacy_positional_list" if has_legacy_rows
                          else "current_named_mapping"),
        "metric_columns_source": ("analysis_metric_columns" if has_legacy_rows
                                  else "analysis_metric_units"),
        "metric_units": {name: units.get(name) for name in names},
        "metric_decimals": decimals,
        "metric_methods": metric_methods,
        "methods": methods,
        "method": method,
        "absent": {name: count for name, count in sorted(absent.items()) if count},
        "rounded_to_zero": rounded_to_zero,
        "precision_rule": ("inline values are exact in this projection; every metric is "
                           "serialized at its stored precision"
                           if not round_values else
                           "inline values are rounded to metric_decimals; the archive keeps "
                           "the exact canonical numbers, returned unrounded by "
                           "query data=['analysis'] with objects/metrics"),
        "precision": {
            "inline": ("exact" if not round_values else "rounded"),
            "lossless": (not round_values),
            "exact_values_preserved": True,
            "exact_query": ("reaper_panorama_query data=['analysis'], objects=[source id], "
                            "metrics=[metric name]"),
            "reason": ("all %d sources x %d scalar metrics are serialized at stored precision "
                       "in this minimum projection" % (len(entities.source_ids), len(names))
                       if not round_values else
                       ("legacy capture exposes %d named scalar metrics; " % len(names)
                        if has_legacy_rows else
                        "all 42 sources x 24 exact serialized floats exceed the default ")
                       + "exact values are preserved in the archive and returned by query; "
                         "the inline projection is rounded, never silently replaced"),
        },
        "rows": {
            "columns": ["source", "available", "stage", "signal_stage", "window", "channel",
                        *names],
            "rows": rows,
            "note": ("one row per source; source indexes the track row (master is the last "
                     "track row); stage is the measurement stage, signal_stage indexes "
                     "signal_stages; window indexes windows[]; channel indexes channels[]; "
                     "null = absent for that source"),
        },
        "stages": stages,
        "signal_stages": signal_stages,
        "channels": channels,
        "windows": windows,
        "uncertainty": {
            "notes": notes,
            "confidence_basis": ("not_produced" if not any(
                isinstance((row.get("uncertainty") or {}).get("confidence"), Mapping)
                and bool((row.get("uncertainty") or {}).get("confidence"))
                for row in entities.analysis) else "per_source_analysis_record"),
        },
        "rms_stats": {"columns": ["count", "min", "max", "mean", "p50"], "units": "dBFS",
                      "basis": "rms_400ms cells over the measured window",
                      "decimals": SUMMARY_DECIMALS, "rows": stats,
                      **({"note": "legacy analysis rows did not publish rms_stats"}
                         if not stats else {})},
        "series_contract": {
            "meta": series_meta,
            "counts": "per source in archive.manifest (kind analysis_series)",
            "exact": "archive family series returns the cells, never the statistics above",
        },
        "stages_contract": (document.get("analysis_contract") or {}).get("stages"),
    }


def _temporal(document: Mapping[str, Any], entities: Entities,
              level_widths: Sequence[float] = (300.0, 30.0)) -> dict[str, Any]:
    """Declared time levels of the exact per-source RMS grid."""
    temporal = document.get("analysis_temporal")
    if not isinstance(temporal, Mapping):
        return {"levels": [], "note": "capture has no activity grid"}
    buckets = [row for row in temporal.get("buckets") or [] if isinstance(row, Mapping)]
    base_width = temporal.get("bucket_width_s")
    anchor_raw = temporal.get("anchor_s")
    try:
        base = float(base_width) if base_width is not None else None
    except (TypeError, ValueError):
        base = None
    try:
        if anchor_raw is None:
            raise ValueError("no anchor_s")
        anchor_value = float(anchor_raw)
        anchor_basis = "stored anchor_s"
    except (TypeError, ValueError):
        # The canonical grid may name its anchor instead of giving seconds
        # ("first_source_cell_start_s").  Use the first stored cell and say so.
        starts = [float(row["start_s"]) for row in buckets
                  if row.get("start_s") is not None]
        anchor_value = min(starts) if starts else 0.0
        anchor_basis = ("first stored cell" if anchor_raw is None
                        else str(anchor_raw or "first stored cell"))
    cells: dict[str, list[tuple[float, float]]] = {}
    for bucket in buckets:
        start = bucket.get("start_s")
        values = bucket.get("rms_dbfs_by_source")
        if not isinstance(values, Mapping) or start is None:
            continue
        try:
            cell_start = float(start)
        except (TypeError, ValueError):
            continue
        for source_id, value in values.items():
            number = _n(value)
            if number is not None:
                cells.setdefault(str(source_id), []).append((cell_start, number))
    source_order = [str(row.get("stable_id")) for row in entities.tracks]
    if entities.master:
        source_order.append(str(entities.master.get("stable_id")))
    levels = []
    for width in level_widths:
        width = float(width)
        if base is not None and width < base - 1e-9:
            levels.append({"width_s": width, "available": False,
                           "reason": "stored grid is coarser"})
            continue
        rows = []
        buckets_seen = 0
        for stable_id in source_order:
            groups: dict[int, list[float]] = {}
            for cell_start, value in cells.get(stable_id) or []:
                groups.setdefault(int((cell_start - anchor_value) // width), []).append(value)
            means = sorted(sum(group) / len(group) for group in groups.values())
            buckets_seen = max(buckets_seen, len(means))
            if not means:
                rows.append([0, None, None, None, None])
                continue
            rows.append([len(means), nz(means[0], SUMMARY_DECIMALS),
                         nz(means[-1], SUMMARY_DECIMALS),
                         nz(sum(means) / len(means), SUMMARY_DECIMALS),
                         nz(means[len(means) // 2], SUMMARY_DECIMALS)])
        levels.append({
            "width_s": width,
            "available": True,
            "buckets": buckets_seen,
            "aggregation": ("mean of the stored grid cells per width-second bucket, per "
                            "source in track order; [buckets, min, max, mean, p50]"),
            "decimals": SUMMARY_DECIMALS,
            "rows": rows,
        })
        if rows and all(int(row[0]) <= 1 for row in rows):
            # A one-bucket level has min == max == mean == p50 by definition.
            # Encode the value once and state the identity instead of repeating
            # four equal numbers per source; no fact is dropped.
            level = levels[-1]
            level["columns"] = ["buckets", "value"]
            level["one_bucket_equality"] = "value == min == max == mean == p50"
            level["rows"] = [[row[0], row[3]] for row in rows]
    return {
        "stored": {"width_s": base_width, "anchor_s": anchor_value,
                   "anchor_basis": anchor_basis, "buckets": len(buckets),
                   "sources": len(cells),
                   "cell_meaning": ("each stored cell is already the mean of the "
                                    "rms_400ms cells in that bucket; the levels above "
                                    "are aggregations of these stored cells, not of the "
                                    "raw 400 ms cells")},
        "columns": ["buckets", "min", "max", "mean", "p50"],
        "levels": levels,
        "exact": ("archive family series (rms_400ms) with objects and interval; the exact "
                  "400 ms cells are never replaced by these levels"),
    }


def _relations(document: Mapping[str, Any]) -> dict[str, Any]:
    relations = document.get("relations")
    relations = relations if isinstance(relations, Mapping) else {}
    edges = [row for row in relations.get("edges") or [] if isinstance(row, Mapping)]
    pairs = [row for row in relations.get("evaluated_pairs") or [] if isinstance(row, Mapping)]
    kinds = Counter(str(row.get("kind")) for row in edges)
    pair_reasons = Counter(str(row.get("reason") or row.get("result")) for row in pairs)
    unavailable = relations.get("unavailable") or {}
    coverage = relations.get("coverage") if isinstance(relations.get("coverage"), Mapping) else {}
    kinds_meta = relations.get("kinds") if isinstance(relations.get("kinds"), Mapping) else {}
    # Per-pair reason lists in relations.coverage repeat one string per pair; the
    # counts below carry the same information in a few bytes.
    coverage_rows = []
    for kind, value in sorted(coverage.items()):
        if not isinstance(value, Mapping):
            continue
        reasons = Counter(str(reason) for reason in value.get("reasons") or [])
        coverage_rows.append([
            str(kind), value.get("status"), value.get("expected"), value.get("evaluated"),
            value.get("observed"), value.get("relations_emitted"),
            dict(sorted(reasons.items())) if reasons else None,
        ])
    summary = document.get("relations_summary")
    unavailable_map = unavailable if isinstance(unavailable, Mapping) else {}
    kind_rows = []
    for kind in sorted(RELATION_KINDS):
        meta = kinds_meta.get(kind) if isinstance(kinds_meta, Mapping) else None
        meta = meta if isinstance(meta, Mapping) else {}
        emitted = int(kinds.get(kind, 0))
        if emitted:
            status, reason = "emitted", None
        elif kind == "band_dominance_pair":
            status, reason = "evaluated_pairs", None
        elif kind in unavailable_map:
            status, reason = "unavailable", str(unavailable_map[kind])
        else:
            status, reason = "not_measured_in_capture", "not_emitted_by_this_capture"
        kind_rows.append({
            "kind": kind,
            "status": status,
            "reason": reason,
            "edges": emitted,
            **({"evaluated_pairs": len(pairs)} if kind == "band_dominance_pair" else {}),
            **({key: meta.get(key) for key in
                ("units", "meaning", "is_not", "method", "method_version",
                 "stage_relation", "uncertainty", "alternatives", "coverage_rule")
                if meta.get(key) is not None} if emitted else {}),
        })
    return {
        "complete": relations.get("complete"),
        "kinds": kind_rows,
        "evaluated_pairs": len(pairs),
        "pair_outcomes": dict(sorted(pair_reasons.items())),
        "unavailable": {"count": len(unavailable),
                        "reasons": dict(sorted(Counter(
                            str(reason) for reason in unavailable.values()).items()))},
        "coverage": {"columns": ["kind", "status", "expected", "evaluated", "observed",
                                 "emitted", "reasons"], "rows": coverage_rows},
        "reference_status": relations.get("reference_status"),
        "content_hash": relations.get("content_hash"),
        "summary_counts": (summary or {}).get("counts") if isinstance(summary, Mapping) else None,
        "edges": ("archive family relations: kind, both endpoints, measured deltas with "
                  "units, coverage and band; one query returns them whole"),
    }


def _coverage(document: Mapping[str, Any]) -> dict[str, Any]:
    """Flat coverage: one named row per domain, and only exceptions spelled out."""
    cov: dict[str, Any] = dict(document.get("coverage") or {})
    domains: dict[str, Any] = dict(cov.get("domains") or {})
    rows, incomplete = [], []
    for name, value in sorted(domains.items()):
        if not isinstance(value, Mapping):
            continue
        status = value.get("status")
        rows.append([str(name), value.get("expected_count"), value.get("observed_count"),
                     status, bool(value.get("truncated"))])
        if status not in (None, "complete") or value.get("truncated"):
            incomplete.append(str(name))
    raw: dict[str, Any] = dict(cov.get("raw_record_coverage") or {})
    trunc: dict[str, Any] = dict(document.get("truncation") or {})
    counts: dict[str, Any] = dict(document.get("domain_counts") or {})
    missing = list(cov.get("missing") or trunc.get("missing") or [])
    truncated = list(cov.get("truncated") or trunc.get("truncated") or [])
    return {
        "complete": not incomplete and not missing and not truncated,
        "domain_count": len(rows),
        "domains": {"columns": ["domain", "expected", "observed", "status", "truncated"],
                    "rows": rows,
                    "note": "expected null means the count is unknowable before capture"},
        "incomplete": incomplete,
        "missing": missing,
        "truncated": truncated,
        "silent_drop": trunc.get("silent_drop"),
        "raw_record": {"absent": sorted(k for k, v in raw.items() if not v),
                       "present_count": sum(1 for v in raw.values() if v)},
        "counts": dict(sorted(counts.items())),
        "rule": cov.get("coverage_rule"),
    }


def _archive(document: Mapping[str, Any], entities: Entities) -> dict[str, Any]:
    details = document.get("details")
    details = details if isinstance(details, Mapping) else {}
    entries = [row for row in details.get("entries") or [] if isinstance(row, Mapping)]
    relations = document.get("relations") if isinstance(document.get("relations"), Mapping) else {}
    midi_counts = [int(entry.get("count") or 0) for entry in entries
                   if str(entry.get("kind")) == "midi_notes"]
    envelope_counts = [int(entry.get("count") or 0) for entry in entries
                       if str(entry.get("kind")) == "envelope_points"]
    catalog = []
    series_counts: dict[str, int] = {}
    series_byte_estimate = 0
    for entry in entries:
        if str(entry.get("kind")) != "analysis_series":
            continue
        payload = entry.get("series") if isinstance(entry.get("series"), Mapping) else {}
        for name, value in payload.items():
            if not isinstance(value, Mapping):
                continue
            count = int(value.get("count") or 0)
            series_counts[str(name)] = series_counts.get(str(name), 0) + count
            # The view has counts, not the exact cell payloads; this is an
            # explicit estimate, never another family's measured bytes.
            per_cell = (291 if str(name) == "band_level" else
                        1000 if str(name) == "temporal_profile" else 48)
            series_byte_estimate += count * per_cell
    for family, spec in ARCHIVE_FAMILIES.items():
        if family == "relations":
            source: Any = relations
            size = section_bytes(source)
            row_count = (len(relations.get("edges") or [])
                         + len(relations.get("evaluated_pairs") or [])
                         + len(relations.get("unavailable") or {}))
            row_unit, bytes_method = "edges+pairs+unavailable", "measured"
        elif family == "series":
            size, row_count, row_unit = series_byte_estimate, sum(series_counts.values()), "cells"
            bytes_method = "estimate_from_cell_counts"
        elif family == "midi_notes":
            notes = sum(midi_counts)
            size, row_count, row_unit = notes * 30, notes, "notes"
            bytes_method = "estimate_from_note_count"
        elif family == "envelope_points":
            points = sum(envelope_counts)
            size, row_count, row_unit = points * 40, points, "points"
            bytes_method = "estimate_from_point_count"
        elif family == "items":
            source = entities.items; size = section_bytes(source)
            row_count, row_unit, bytes_method = len(source), "rows", "measured"
        elif family == "takes":
            source = entities.takes; size = section_bytes(source)
            row_count, row_unit, bytes_method = len(source), "rows", "measured"
        elif family == "audio":
            source = entities.audio; size = section_bytes(source)
            row_count, row_unit, bytes_method = len(source), "assets", "measured"
        elif family == "analysis":
            source = entities.analysis; size = section_bytes(source)
            row_count, row_unit, bytes_method = len(source), "sources", "measured"
        elif family == "fx":
            source = document.get("fx") or []; size = section_bytes(source)
            row_count, row_unit, bytes_method = len(source), "rows", "measured"
        elif family == "project_summary":
            source = document.get("project") or {}; size = section_bytes(source)
            row_count, row_unit, bytes_method = len(source), "top_level_keys", "measured"
        else:
            source = []
            size = row_count = 0; row_unit, bytes_method = "rows", "not_measured"
        catalog.append([family, row_count, row_unit, size, bytes_method, spec["selectors"]])
    catalog.sort(key=lambda row: str(row[0]))
    # Per-owner series counts, grouped by (series, count): a long list of
    # identical rows says nothing more than "every listed source has N cells".
    series_groups: dict[tuple[str, Any], int] = {}
    series_units: dict[str, Any] = {}
    for entry in entries:
        if str(entry.get("kind")) != "analysis_series":
            continue
        series = entry.get("series") if isinstance(entry.get("series"), Mapping) else {}
        for name, payload in series.items():
            if not isinstance(payload, Mapping):
                continue
            key = (str(name), payload.get("count"))
            series_groups[key] = series_groups.get(key, 0) + 1
            series_units.setdefault(str(name), payload.get("units"))
    series_rows = [[name, count, owners, series_units.get(name)]
                   for (name, count), owners in sorted(series_groups.items())]
    return {
        "authority": ("the sealed record.json and panorama sidecar are authoritative; this "
                      "view is one projection of that document at one revision"),
        "catalog": {
            "columns": ["family", "rows", "row_unit", "bytes", "bytes_method", "selectors"],
            "rows": catalog,
            "note": ("bytes_method=measured means the exact canonical payload for that family; "
                     "estimate_from_cell/count means the archive payload is not present in the "
                     "view and the exact query is the only delivery path. No estimate reuses "
                     "another family's bytes."),
        },
        "exact": {
            "series": {
                "columns": ["series", "cells", "sources", "units"],
                "rows": series_rows,
                "note": ("one row per distinct (series, cell count); sources is how many "
                         "analysis rows carry it. Fetch exact cells by passing a source "
                         "stable id (or 'all') and an optional interval/resolution to "
                         "reaper_panorama_query with data=['series']"),
            },
            "midi_notes": {
                "takes": len(midi_counts),
                "notes": sum(midi_counts),
                "address": ("query data=['midi_notes'] objects=[take id] interval=[start,end)"),
            },
            "envelope_points": {
                "envelopes": len(envelope_counts),
                "points": sum(envelope_counts),
                "address": ("query data=['envelope_points'] objects=[track id] "
                            "interval=[start,end)"),
            },
            "content_hash": _hash(entries),
            "rule": ("every count and address above is exact; the rows themselves stay in "
                     "the sealed record and are re-hashed against this manifest on fetch"),
        },
    }


def _completeness(document: Mapping[str, Any], entities: "Entities",
                  analysis: Mapping[str, Any], archive: Mapping[str, Any],
                  coverage: Mapping[str, Any]) -> dict[str, Any]:
    """Four independent completeness claims, never one generic boolean."""
    catalog_rows = (archive.get("catalog") or {}).get("rows") or []
    families = [str(row[0]) for row in catalog_rows if isinstance(row, (list, tuple)) and row]
    counts = {
        "tracks": len(entities.tracks),
        "master": 1 if entities.master else 0,
        "items": len(entities.items),
        "takes": len(entities.takes),
        "fx": len(document.get("fx") or []),
        "routes": len(document.get("routes") or []),
        "automation_envelopes": len(_rows(document.get("automation"), "envelopes")),
        "envelopes": len(_rows(document.get("envelopes"), "envelopes")),
        "midi": len(document.get("midi") or []),
        "audio": len(entities.audio),
        "analysis": len(entities.analysis),
    }
    missing = list(coverage.get("missing") or [])
    truncated = list(coverage.get("truncated") or [])
    absent = dict(analysis.get("absent") or {})
    declared = len(analysis.get("metric_columns") or [])
    sources = len(entities.source_ids)
    present = {name: sources - int(absent.get(name, 0))
               for name in analysis.get("metric_columns") or []}
    attribution_ok = all(
        isinstance(row[0], int) and 0 <= row[0] < sources
        for row in (analysis.get("rows") or {}).get("rows") or [])
    return {
        "project_inventory": {
            "complete": not missing and not truncated,
            "counts": counts,
            "missing": missing,
            "truncated": truncated,
        },
        "inline_metrics": {
            "complete": bool(declared == 24 and sources and len(present) == declared
                             and attribution_ok),
            "sources": sources,
            "declared_metrics": declared,
            "present_per_metric": present,
            "absent": absent,
            "attribution": "analysis.rows.source -> track row / master slot in entities",
            "precision_lossless": False,
            "precision_policy": "rounded_inline_exact_query",
            "rows": len((analysis.get("rows") or {}).get("rows") or []),
        },
        "archive_families": {
            "complete": bool(families),
            "families": families,
            "count": len(families),
            "unavailable": sorted(set(ARCHIVE_FAMILIES) - set(families)),
        },
        "exact_delivery": {
            "delivered": False,
            "reason": "the inline view catalogues exact families but does not contain their rows",
            "query": "reaper_panorama_query returns the whole selected scope or a measured refusal",
        },
    }


def _reading(document: Mapping[str, Any], budget: Mapping[str, Any]) -> dict[str, Any]:
    epistemic = document.get("epistemic_layers")
    epistemic = epistemic if isinstance(epistemic, Mapping) else {}
    uncertainty = epistemic.get("uncertainty")
    uncertainty = uncertainty if isinstance(uncertainty, Mapping) else {}
    return {
        "scope": "whole project at one captured revision",
        "delivery": ("paged MCP delivery: reaper_panorama_open returns page 1 and a "
                     "manifest; fetch every page with reaper_panorama_page, then "
                     "reaper_panorama_ack verifies every page hash; merging the payloads "
                     "in page order reproduces this view exactly"),
        "tables": ("each table states its columns once; *_i and source/owner index the "
                   "entity or table named in its note (-1 = absent); null = not measured"),
        "time": ("*_s are project seconds; intervals are half-open [start_s,end_s); "
                 "audio_contract maps files and samples"),
        "precision": {
            "inline_metrics": f"{INLINE_METRIC_DECIMALS} decimals",
            "inline_levels": f"{SUMMARY_DECIMALS} decimals",
            "inline_audio_hash": "audio.sha256_16 is the first 16 hex of the archived blob hash",
            "exact": "query analysis (metrics) or series (cells) returns stored values unrounded",
            "lossless": False,
            "policy": ("inline metric values are rounded; exact canonical values stay in the "
                       "archive and are returned by one semantic analysis query"),
        },
        "detail": ("exact rows stay in the archive, catalogued in archive.catalog; one "
                   "reaper_panorama_query call returns a coherent selection whole"),
        "epistemic": {
            "interpretations": "empty: no role, genre, quality or preference inferred",
            "hypotheses": "empty: no cause asserted by this view",
            "uncertainty": {
                "measurement_notes": len(uncertainty.get("measurement_notes") or []),
                "truncated_domains": uncertainty.get("truncated_domains"),
                "missing_domains": uncertainty.get("missing_domains"),
                "note": "measurement notes are per metric in the sealed document",
            },
            "rule": ("facts, measurements, interpretations and hypotheses are separate; this "
                     "view adds no role, genre, quality verdict or cause"),
        },
        "ids": {
            "syntax": ((document.get("legend") or {}).get("stable_ids")
                       if isinstance(document.get("legend"), Mapping) else None),
            "note": "entities maps each index to its GUID; query objects= takes these ids or a GUID",
        },
        "crosslinks": "derived: the FK columns are the crosslinks",
        "budget": dict(budget),
    }


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def _compact_evidence(value: Any) -> dict[str, Any]:
    """The freshness facts a reader needs, without the repeated prose."""
    value = value if isinstance(value, Mapping) else {}
    return {
        "state": value.get("state"),
        "revision": (value.get("revision") or {}).get("change_count")
        if isinstance(value.get("revision"), Mapping) else None,
        "complete": value.get("complete"),
        "cross_revision_authoritative": value.get("cross_revision_authoritative"),
        "render_manifest": value.get("render_manifest"),
    }


def _content_index(document: Mapping[str, Any], entities: Entities) -> dict[str, Any]:
    """Per-track item/take inventory without duplicating every item/take GUID."""
    items = [row for row in document.get("items") or [] if isinstance(row, Mapping)]
    takes = [row for row in document.get("takes") or [] if isinstance(row, Mapping)]
    midi = [row for row in document.get("midi") or [] if isinstance(row, Mapping)]
    rows: list[list[Any]] = []
    ordered = list(entities.tracks)
    if entities.master is not None:
        ordered.append(entities.master)
    for source, track in enumerate(ordered):
        source_id = str(track.get("stable_id"))
        own_items = [row for row in items if str(row.get("track_id")) == source_id]
        own_takes = [row for row in takes if str(row.get("track_id")) == source_id]
        take_ids = {str(row.get("take_id")) for row in own_takes}
        starts = []
        ends = []
        for row in own_items:
            interval = row.get("interval") if isinstance(row.get("interval"), Mapping) else {}
            if interval.get("start_s") is not None:
                starts.append(float(interval["start_s"]))
            if interval.get("end_s") is not None:
                ends.append(float(interval["end_s"]))
        midi_count = sum(
            1 for row in midi
            if str(row.get("take_id")) in take_ids or str(row.get("track_id")) == source_id)
        rows.append([
            source,
            len(own_items),
            round(min(starts), 6) if starts else None,
            round(max(ends), 6) if ends else None,
            len(own_takes),
            midi_count,
        ])
    return {
        "columns": ["source", "item_n", "first_s", "last_s", "take_n", "midi_n"],
        "rows": rows,
        "note": ("complete per-track item/take inventory; exact item/take rows are not "
                 "duplicated here: query data=['items'] or data=['takes'] with "
                 "objects=[track id] returns them whole"),
    }


def _lean_projection(view: Mapping[str, Any], document: Mapping[str, Any]) -> dict[str, Any]:
    """Explicit overview + exact-family catalogue, for views near the one-result budget.

    Every track/master and all 24 scalar metrics with their provenance stay
    inline.  Item/take rows and every item/take GUID move to the exact query,
    whose per-track selector is declared in ``content_index`` and
    ``archive.exact``.  The high-cardinality families are marked catalogued,
    never silently absent.
    """
    entities = Entities(document)
    lean = {key: value for key, value in view.items() if key not in {"items", "takes"}}
    lean["view_mode"] = "lean_overview"
    lean["entities"] = {
        key: value for key, value in (view.get("entities") or {}).items()
        if key not in {"item", "take"}
    }
    lean["entities"]["note"] = (
        "track row i and master map source i; item/take GUIDs are not duplicated in this "
        "overview; query data=['items']/['takes'] with objects=[track id] returns exact rows")
    lean["content_index"] = _content_index(document, entities)
    archive = dict(view.get("archive") or {})
    archive["inline_families"] = [
        "tracks", "content_index", "analysis", "fx", "routes", "automation",
        "envelopes", "audio", "time", "project", "coverage", "completeness",
    ]
    archive["catalogued_families"] = [
        "items", "takes", "series", "relations", "midi_notes", "envelope_points",
    ]
    exact = dict(archive.get("exact") or {})
    exact["items"] = {
        "selector": "data=['items'], objects=[track id]",
        "completeness": "all item rows for the selected track",
    }
    exact["takes"] = {
        "selector": "data=['takes'], objects=[track id]",
        "completeness": "all take rows for the selected track",
    }
    exact["audio"] = {
        "selector": "data=['audio'], objects=[source id]",
        "completeness": "all audio mapping rows for the selected source",
    }
    archive["exact"] = exact
    lean["archive"] = archive
    inventory = dict(lean.get("reading_inventory") or {})
    inventory["catalogued_sections"] = ["items", "takes"]
    inventory["rule"] = (
        "every canonical section is placed inline or catalogued with an exact selector; "
        "a catalogued section is available whole by query, not by pages")
    lean["reading_inventory"] = inventory
    return lean


def _minimum_inventory(document: Mapping[str, Any], entities: Entities,
                       analysis: Mapping[str, Any]) -> dict[str, Any]:
    """Counts only; the minimum projection carries no high-cardinality rows."""
    project = document.get("project") if isinstance(document.get("project"), Mapping) else {}
    return {
        "tracks": len(entities.tracks),
        "master": 1 if entities.master else 0,
        "sources": len(entities.source_ids),
        "items": len(document.get("items") or []),
        "takes": len(document.get("takes") or []),
        "midi_reports": len(document.get("midi") or []),
        "fx": len(document.get("fx") or []),
        "routes": len(document.get("routes") or []),
        "automation": len(_rows(document.get("automation"), "envelopes")),
        "envelopes": len(_rows(document.get("envelopes"), "envelopes")),
        "audio": len(entities.audio),
        "analysis_rows": len(entities.analysis),
        "metric_columns": len(analysis.get("metric_columns") or []),
        "capture_window": project.get("capture_window"),
    }


def build_minimum_view(document: Mapping[str, Any], *,
                       record_path: str | None = None,
                       max_response_bytes: int = DEFAULT_RESPONSE_BYTES) -> dict[str, Any]:
    """A declared, non-complete core for projects whose full view exceeds one result.

    It keeps the project/track inventory, all scalar metric columns at their
    stored precision, relation-kind counts and the exact-family catalogue.  It
    deliberately omits high-cardinality rows; those remain reachable by an
    exact semantic query.  The caller must label this as ``minimum`` and must
    never turn it into a full-view receipt.
    """
    lean = build_view(document, record_path=record_path,
                      max_response_bytes=max_response_bytes,
                      lean=True, exact_metrics=True)
    entities = Entities(document)
    analysis = lean["analysis"]
    keys = ("schema_version", "document_type", "binding", "reading", "units", "project",
            "entities", "tracks", "analysis", "relations", "time", "coverage",
            "archive", "completeness", "content_index", "reading_inventory", "derived")
    minimum = {key: lean[key] for key in keys if key in lean}
    minimum["view_mode"] = "minimum_core"
    minimum["entities"] = {
        key: value for key, value in (lean.get("entities") or {}).items()
        if key not in {"item", "take"}
    }
    minimum["entities"]["note"] = (
        "track row i and master map source i; item/take GUIDs are intentionally not "
        "duplicated in the minimum core; query data=['items']/['takes'] with "
        "objects=[track id] returns exact rows")
    archive = dict(lean.get("archive") or {})
    archive["inline_families"] = [
        "tracks", "analysis", "relations", "time", "project", "coverage",
    ]
    archive["catalogued_families"] = [
        "items", "takes", "fx", "routes", "audio", "automation", "envelopes",
        "midi_notes", "envelope_points", "series",
    ]
    archive["minimum"] = {
        "whole_project": False,
        "complete_for_scope": True,
        "why": ("the full/lean projection measured over the host result budget; this is "
                "the same document's track and scalar-metric core, not a complete view"),
    }
    minimum["archive"] = archive
    minimum["minimum"] = {
        "kind": "core",
        "whole_project": False,
        "complete_for_scope": True,
        "inline": ["tracks", "analysis (all scalar metrics, exact)", "relations (kind counts)",
                   "time", "project", "coverage"],
        "catalogued": archive["catalogued_families"],
        "query": "reaper_panorama_query returns each catalogued family whole or refuses",
        "receipt": None,
    }
    minimum["inventory"] = _minimum_inventory(document, entities, analysis)
    completeness = _completeness(document, entities, analysis, archive,
                                  minimum.get("coverage") or {})
    completeness["scope"] = {
        "kind": "minimum_core",
        "whole_project": False,
        "complete_for_scope": True,
        "catalogued": archive["catalogued_families"],
        "reason": ("full view exceeded the result budget; this core is complete for its "
                   "declared scope and is not a whole-project view"),
    }
    inline = completeness.get("inline_metrics") or {}
    inline["precision_lossless"] = True
    inline["precision_policy"] = "exact_inline"
    completeness["inline_metrics"] = inline
    completeness["archive_families"]["inline"] = archive["inline_families"]
    completeness["archive_families"]["catalogued"] = archive["catalogued_families"]
    completeness["exact_delivery"] = {
        "delivered": False,
        "reason": ("minimum_core carries track inventory and all scalar metrics; item/take, "
                   "FX/routing, audio and series rows remain exact-query families"),
        "query": "reaper_panorama_query returns the whole selected scope or a measured refusal",
    }
    minimum["completeness"] = completeness
    reading = dict(minimum.get("reading") or {})
    reading["scope"] = "minimum core at one captured revision (not the whole-project view)"
    reading["delivery"] = ("paged MCP delivery: open, fetch every page, then ack; a missing "
                           "detail family is catalogued, never implied")
    reading["precision"] = {
        "inline_metrics": "exact stored precision",
        "inline_levels": f"{SUMMARY_DECIMALS} decimals",
        "exact": "analysis values in this projection are unrounded; other families use the exact query",
        "lossless": True,
        "policy": ("minimum_core keeps every scalar metric exactly and marks high-cardinality "
                   "families catalogued"),
    }
    minimum["reading"] = reading
    minimum["view_sha256"] = _hash({key: value for key, value in minimum.items()
                                    if key != "view_sha256"})
    return minimum


def build_view(document: Mapping[str, Any], *,
               record_path: str | None = None,
               max_response_bytes: int = DEFAULT_RESPONSE_BYTES,
               lean: bool = False,
               exact_metrics: bool = False) -> dict[str, Any]:
    """Project a canonical panorama document into one model view."""
    if not isinstance(document, Mapping) or not document.get("capture_binding"):
        raise ValidationError("build_view needs a canonical capture panorama document")
    binding = (document.get("capture_binding")
               if isinstance(document.get("capture_binding"), Mapping) else {})
    project = document.get("project") if isinstance(document.get("project"), Mapping) else {}
    revision_before = (binding.get("revision_before")
                       if isinstance(binding.get("revision_before"), Mapping) else {})
    revision_after = (binding.get("revision_after")
                      if isinstance(binding.get("revision_after"), Mapping) else {})
    entities = Entities(document)
    unmapped = sorted(set(document) - set(SECTION_INVENTORY))
    time_map = document.get("time_map") if isinstance(document.get("time_map"), Mapping) else {}
    analysis_view = _analysis(document, entities, round_values=not exact_metrics)
    archive_view = _archive(document, entities)
    coverage_view = _coverage(document)
    completeness_view = _completeness(document, entities, analysis_view,
                                       archive_view, coverage_view)
    view: dict[str, Any] = {
        "schema_version": VIEW_SCHEMA,
        "document_type": "panorama_view",
        "binding": {
            "capture_id": binding.get("capture_id"),
            "project_id": binding.get("project_id"),
            "record_path": record_path or binding.get("record_path"),
            "record_schema": binding.get("record_schema_version"),
            "record_integrity_sha256": binding.get("record_integrity_sha256"),
            "revision": {
                "project_id": (revision_before.get("project_id")
                               or revision_after.get("project_id")),
                "path": revision_before.get("path") or revision_after.get("path"),
                "before": {key: revision_before.get(key)
                           for key in ("change_count", "instance_id", "epoch")},
                "after": {key: revision_after.get(key)
                          for key in ("change_count", "instance_id", "epoch")},
            },
            "time_window": binding.get("time_window"),
            "identity": document.get("identity"),
            "evidence": _compact_evidence(document.get("evidence_freshness")),
            "render_manifest": document.get("render_manifest"),
            "structural_facts": document.get("structural_facts"),
            "source_document_schema": document.get("schema_version"),
            "source_document_sha256": document.get("document_sha256"),
            "captured_at": (document.get("provenance") or {}).get("captured_at"),
            "producer": (document.get("provenance") or {}).get("producer"),
        },
        "reading": _reading(document, {
            "paging": ("every open states manifest.page_count, page_ids, page_hashes and "
                       "the measured delivered size of every page; no page exceeds "
                       "manifest.max_page_bytes"),
            "response_bytes": None,
        }),
        "units": {
            "metrics": "analysis.metric_units (stated once there, not repeated)",
            "columns": {
                "tracks.vol": "REAPER mixer value (not dB)",
                "tracks.pan": "-1..1",
                "items.start_s/end_s/pos_s/len_s": "s",
                "audio.frames": "samples",
                "audio.sr": "Hz",
                "audio.dur_s": "s",
                "temporal.levels.rows": "dBFS",
                "relations.kinds[].2": "units per kind",
            },
        },
        "project": {
            "extent": project.get("project_extent"),
            "content_extent": project.get("content_extent"),
            "window": project.get("capture_window"),
            "render_tail": project.get("render_tail"),
            "counts": project.get("counts"),
        },
        "entities": entities.payload(),
        "tracks": _tracks(document, entities),
        "items": _items(document, entities),
        "takes": _takes(document, entities),
        "midi": _midi(document, entities),
        "fx": _fx(document, entities),
        "routes": _routes(document, entities),
        "automation": _envelopes(document, "automation", entities),
        "envelopes": _envelopes(document, "envelopes", entities),
        "time": {
            "map_id": time_map.get("time_map_id"),
            "revision": time_map.get("revision"),
            "no_single_bpm_grid": time_map.get("base_bpm_not_used_for_musical_coordinates"),
            "tempo": [[point.get("time_s"), point.get("bpm"), point.get("numerator"),
                       point.get("denominator"), point.get("measure"), point.get("beat")]
                      for point in time_map.get("tempo_points") or []
                      if isinstance(point, Mapping)],
            "signatures": [[point.get("time_s"), point.get("numerator"),
                            point.get("denominator")]
                           for point in time_map.get("time_signatures") or []
                           if isinstance(point, Mapping)],
            "markers": [[marker.get("time_s"), marker.get("name")]
                        for marker in document.get("markers") or []
                        if isinstance(marker, Mapping)],
            "regions": [[(region.get("interval") or {}).get("start_s"),
                         (region.get("interval") or {}).get("end_s"), region.get("name")]
                        for region in document.get("regions") or []
                        if isinstance(region, Mapping)],
            "columns": {"tempo": ["time_s", "bpm", "num", "den", "measure", "beat"],
                        "signatures": ["time_s", "num", "den"],
                        "markers": ["time_s", "name"],
                        "regions": ["start_s", "end_s", "name"]},
        },
        "transport": {
            "selection": document.get("selection"),
            "transport": document.get("transport"),
        },
        "audio": _audio(document, entities),
        "audio_contract": {
            "mapping": document.get("audio_time_contract"),
            "note": document.get("audio_capture_revision_contract"),
        },
        "analysis": analysis_view,
        "temporal": _temporal(document, entities),
        "relations": _relations(document),
        "coverage": coverage_view,
        "archive": archive_view,
        "completeness": completeness_view,
        "derived": {key: {"relation": value[0], "target": value[1], "note": value[2]}
                    for key, value in DERIVED_SECTIONS.items()},
        "reading_inventory": {
            "mapped_sections": sorted({value for value in SECTION_INVENTORY.values()}),
            "unmapped_sections": unmapped,
            "rule": ("every canonical section is placed in this view exactly once; an "
                     "unmapped section is listed here rather than dropped"),
        },
    }
    if lean:
        view = _lean_projection(view, document)
    view["view_sha256"] = _hash(view)
    return view


def view_bytes(view: Mapping[str, Any]) -> int:
    return len(canonical(view))


def dominant_sections(view: Mapping[str, Any], limit: int = 12) -> list[dict[str, Any]]:
    sizes = []
    for key, value in view.items():
        if key == "view_sha256":
            continue
        sizes.append({"section": key, "bytes": section_bytes(value)})
    sizes.sort(key=lambda row: row["bytes"], reverse=True)
    return sizes[:limit]


def budget_report(view: Mapping[str, Any], *, max_response_bytes: int) -> dict[str, Any]:
    measured = view_bytes(view)
    return {
        "mode": "single_response",
        "max_response_bytes": int(max_response_bytes),
        "measured_bytes": measured,
        "measured_tokens_estimate": int(measured / 2.2),
        "token_estimate_method": "utf8_bytes/2.2 conservative fallback",
        "fits": measured <= int(max_response_bytes),
        "sections_bytes": dominant_sections(view, limit=8),
        "exact_detail_tool": "reaper_panorama_query",
    }


def serialise_view(view: Mapping[str, Any]) -> str:
    return json.dumps(view, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"), default=_json_default)


__all__ = [
    "ARCHIVE_FAMILIES",
    "DEFAULT_RESPONSE_BYTES",
    "DERIVED_SECTIONS",
    "INLINE_METRIC_DECIMALS",
    "SECTION_INVENTORY",
    "SUMMARY_DECIMALS",
    "VIEW_SCHEMA",
    "budget_report",
    "build_view",
    "canonical",
    "dominant_sections",
    "nz",
    "section_bytes",
    "serialise_view",
    "table",
    "view_bytes",
]
