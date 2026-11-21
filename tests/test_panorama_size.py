"""Size and drill-down contract for a large capture.

A 40-track / five-minute project with 1,000 media items and 1,000 active takes
is the shape that used to make the one-shot panorama enormous: every item and
take was a full row and every item/take GUID was duplicated in entities and
crosslinks.  The map now carries one inventory row per track; this test pins:

* the archived panorama document still fits its declared 700 KB gate;
* the paged model view stays under ~60k tokens using the conservative
  ``utf-8 bytes / 2.2`` estimate;
* every analysis row and metric column survives unchanged;
* exact item, take and FX-parameter rows remain queryable by stable id;
* a legacy list-shaped sidecar is projected at read time, not rewritten.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from reaper_audio.context import panorama as panorama_module
from reaper_audio.context.full_capture import (CaptureArchive, FullCaptureRecord,
                                               REQUIRED_MAP_KEYS)
from reaper_audio.context.lifecycle import PanoramaLifecycle, _merge_view_pages
from reaper_audio.context.panorama import (ANALYSIS_METRIC_UNITS,
                                           DEFAULT_MAX_BYTES,
                                           _canonical,
                                           build_panorama,
                                           estimate_tokens,
                                           load_panorama,
                                           panorama_path_for_record)
from reaper_audio.context.panorama_view import build_view, view_bytes
from reaper_audio.context.query import run_query

TRACKS = 40
ITEMS_PER_TRACK = 25
ITEMS = TRACKS * ITEMS_PER_TRACK
CAPTURE_S = 300.0
TOKEN_BUDGET = 60_000
FRAMES = 4_800


def _guid(high: int, low: int) -> str:
    return "{%08X-0000-4000-8000-%012X}" % (high & 0xFFFFFFFF, low & 0xFFFFFFFFFFFF)


def _metric_names() -> list[str]:
    return sorted(ANALYSIS_METRIC_UNITS)


def _analysis_report(index: int) -> dict:
    metrics = {
        name: {"value": float(index + offset + 1), "unit": ANALYSIS_METRIC_UNITS[name],
               "confidence": None, "note": None}
        for offset, name in enumerate(_metric_names())
    }
    return {
        "channels": 2,
        "duration_s": FRAMES / 48000,
        "sample_rate": 48000,
        "section": None,
        "provenance": {
            "analysis": "test", "decoder": "soundfile", "stage": "file_or_buffer",
            "file_frames": FRAMES, "file_channels": 2,
            "measured_section_s": {"start_s": 0.0, "end_s": FRAMES / 48000},
            "section_requested": None, "start_s": 0.0,
        },
        "metrics": metrics,
        "coverage": {"levels": "complete", "loudness": "available",
                     "true_peak": "estimated"},
        "curves": {
            "rms_400ms_dbfs": [-22.0, -21.0, -20.0, -19.0],
            "rms_400ms_time_s": [0.0, 0.02, 0.04, 0.06],
        },
    }


def _item(track_index: int, item_index: int) -> tuple[dict, dict]:
    item_guid = _guid(0x1000 + track_index, item_index)
    take_guid = _guid(0x2000 + track_index, item_index)
    position = 0.5 + item_index * 10.0
    take = {
        "guid": take_guid, "name": "Take %02d %02d" % (track_index, item_index),
        "is_midi": False, "playrate": 1.0, "preserve_pitch": True,
        "stretch_markers": 0, "fx_count": 0,
        "source": "D:/media/t%02d/i%02d.wav" % (track_index, item_index),
    }
    item = {
        "guid": item_guid, "name": "Clip %02d" % item_index,
        "position_s": position, "length_s": 9.0,
        "start_offset_s": 0.0, "source_length_s": 9.0,
        "source_sample_rate": 48000, "source_channels": 2,
        "source_length_is_qn": False, "beat_attach_mode": 0,
        "channel_mode": 0, "playrate": 1.0, "mute": False,
        "loop_source": False, "fade_in_s": 0.0, "fade_out_s": 0.0,
        "selected": False, "take_count": 1, "active_take": take,
    }
    return item, take


def _build_record(tmp_path: Path) -> tuple[FullCaptureRecord, dict[str, Path]]:
    revision = {"project_id": "reaper:size", "change_count": 11, "path": "size.rpp",
                "instance_id": "pri_size", "epoch": 1,
                "epoch_basis": "bridge_session_project_generation"}
    tracks, items, takes, fx_rows, digest_tracks = [], [], [], [], []
    for track_index in range(TRACKS):
        track_guid = _guid(0x4000 + track_index, 0)
        track_items = []
        for item_index in range(ITEMS_PER_TRACK):
            item, take = _item(track_index, item_index)
            track_items.append(item)
            items.append({**item, "track_guid": track_guid,
                          "source_path": take["source"]})
            takes.append(take)
        effects = [{
            "index": 0, "guid": _guid(0x3000 + track_index, 0), "name": "Comp",
            "enabled": True, "offline": False, "parameter_count": 12,
            "parameters": [{"index": i, "name": "Param %d" % i,
                            "normalized": 0.5, "formatted": "0.5"} for i in range(12)],
        }, {
            "index": 1, "guid": _guid(0x3000 + track_index, 1), "name": "EQ",
            "enabled": True, "offline": False, "parameter_count": 8,
            "parameters": [{"index": i, "name": "Band %d" % i,
                            "normalized": 0.25, "formatted": "-3.0"} for i in range(8)],
        }]
        fx_rows.extend(effects)
        tracks.append({
            "guid": track_guid, "index": track_index, "name": "Track %02d" % track_index,
            "channel_count": 2, "folder_depth": 0, "volume": 0.7, "pan": 0.0,
            "mute": False, "solo": 0, "selected": False, "record_arm": 0,
            "record_mode": 0, "record_mode_flags": 0, "sends": [], "receives": 0,
            "hardware_sends": 0, "items": track_items, "fx": effects,
        })
        digest_tracks.append({"guid": track_guid, "sends": []})
    master_guid = _guid(0x5000, 0)
    master_fx = {
        "index": 0, "guid": _guid(0x5000, 1), "name": "Master FX",
        "enabled": True, "offline": False, "parameter_count": 4,
        "parameters": [{"index": i, "name": "Gain %d" % i, "normalized": 1.0,
                        "formatted": "0.0"} for i in range(4)],
    }
    fx_rows.append(master_fx)
    master = {
        "guid": master_guid, "index": -1, "name": "MASTER", "channel_count": 2,
        "folder_depth": 0, "volume": 1.0, "pan": 0.0, "mute": False, "solo": 0,
        "selected": False, "items": [], "sends": 0, "receives": 0,
        "hardware_sends": 0, "fx": [master_fx],
    }
    envelope_track_rows = [{"track_guid": track["guid"], "envelopes": []}
                           for track in tracks]
    project_map = {
        "tracks": tracks, "master": master, "items": items, "takes": takes,
        "midi": [], "fx": fx_rows, "routing": [],
        "envelopes": {"complete": True, "items_scanned": len(items), "gaps": [],
                      "master": [], "fx": [], "items": [],
                      "tracks": envelope_track_rows},
        "automation": {"complete": True, "items_scanned": len(items), "gaps": [],
                       "master": [], "fx": [], "items": [],
                       "tracks": envelope_track_rows},
        "tempo": {"bpm": 120, "numerator": 4, "denominator": 4,
                  "markers": [{"time_s": 0.0, "bpm": 120, "numerator": 4,
                               "denominator": 4, "measure": 0, "beat": 0,
                               "linear": False}]},
        "time_signatures": [{"time_s": 0.0, "bpm": 120, "numerator": 4,
                             "denominator": 4, "measure": 0, "beat": 0,
                             "linear": False}],
        "markers": [], "regions": [], "selection": {}, "transport": {},
        "coverage": {name: True for name in REQUIRED_MAP_KEYS - {"coverage"}},
        "digest": {"change_count": 11,
                   "fields": {"project": {"settings": {"render_endpos": CAPTURE_S,
                                                       "project_srate": 48000}},
                              "tracks": digest_tracks,
                              "automation": {}, "envelopes": {}}},
    }
    files: dict[str, Path] = {}
    audio, analysis = [], {}
    for index, guid in enumerate([track["guid"] for track in tracks] + [master_guid]):
        path = tmp_path / ("source-%02d.wav" % index)
        sf.write(path, np.zeros((FRAMES, 2)), 48000, subtype="PCM_24")
        files[guid] = path
        audio.append({
            "asset_id": "ast_%02d" % index, "source_guid": guid,
            "name": "source-%02d" % index, "is_master": guid == master_guid,
            "stage": ("master_mix_post_fx" if guid == master_guid
                      else "selected_track_via_master_parallel_batch_post_fx"),
            "capture_method": "native_render", "sample_rate": 48000,
            "channels": 2, "frames": FRAMES, "duration_s": FRAMES / 48000,
            "bytes": path.stat().st_size, "sha256": hashlib.sha256(
                path.read_bytes()).hexdigest(),
            "signal_window": {"project_start_s": 0.0, "project_end_s": CAPTURE_S,
                              "file_start_s": 0.0, "file_end_s": FRAMES / 48000},
            "capture_revision": revision, "soloed_guid": None,
            "status": "leased", "lease_id": "job-size",
        })
        analysis[guid] = _analysis_report(index)
    coverage = {name: True for name in
                REQUIRED_MAP_KEYS - {"coverage"} | {"audio", "analysis"}}
    record = FullCaptureRecord(
        capture_id="cap_size", project_id="reaper:size", project_path="size.rpp",
        revision_before=revision, revision_after=revision,
        time_window={"project_start_s": 0.0, "project_end_s": CAPTURE_S,
                     "file_start_s": 0.0, "file_end_s": FRAMES / 48000},
        project_map=project_map, audio=audio, analysis=analysis, coverage=coverage,
        missing=[], created_at="2026-11-21T00:00:00+00:00", producer="test",
    )
    return record, files


@pytest.fixture(scope="module")
def large_archive(tmp_path_factory: pytest.TempPathFactory):
    tmp = tmp_path_factory.mktemp("panorama-size")
    record, files = _build_record(tmp)
    archive = CaptureArchive(tmp / "archive")
    archive.append(record, files)
    return archive, record


def _fetch_all(lifecycle: PanoramaLifecycle, opened: dict) -> list[dict]:
    manifest = opened["manifest"]
    ids = manifest.get("page_ids")
    if ids is None:
        ids = ["pg_%d_of_%d" % (index, manifest["page_count"])
               for index in range(1, manifest["page_count"] + 1)]
    pages = [opened]
    for page_id in ids[1:]:
        pages.append(lifecycle.view_page(opened["handle"], opened["revision"], page_id))
    return pages


def test_panorama_document_fits_budget_and_view_stays_small(large_archive) -> None:
    archive, record = large_archive
    record_path = archive.captures / record.capture_id / "record.json"
    sidecar_path = panorama_path_for_record(record_path)
    # Loading with the declared gate is the assertion: a document over 700 KB
    # or 256k estimated tokens raises PanoramaCapacityError here.
    document = load_panorama(sidecar_path, archive.read(record.capture_id))
    encoded = _canonical(document)
    assert len(encoded) <= DEFAULT_MAX_BYTES
    assert estimate_tokens(encoded) <= 256_000
    assert document["schema_version"] == panorama_module.PANORAMA_SCHEMA_VERSION

    # Items/takes are inventories; FX keeps names, order, state and count only.
    assert document["items"]["representation"] == "per_track_summary"
    assert document["items"]["count"] == ITEMS == 1000
    assert document["takes"]["count"] == ITEMS
    assert sum(row[1] for row in document["items"]["rows"]) == ITEMS
    assert sum(row[1] for row in document["takes"]["rows"]) == ITEMS
    assert len(document["items"]["rows"]) == TRACKS + 1  # tracks plus master
    assert all("parameters" not in row for row in document["fx"])
    # Every analysis and audio row survives the map change.
    assert len(document["analysis"]) == TRACKS + 1
    assert all(len(row["metrics"]) == 24 for row in document["analysis"])
    assert len(document["audio"]) == TRACKS + 1
    assert isinstance(document["relations"].get("edges"), list)
    assert isinstance(document["coverage"]["domains"], Mapping)

    lifecycle = PanoramaLifecycle(archive.root)
    opened = lifecycle.open_view(record.project_id)
    manifest = opened["manifest"]
    view_tokens = estimate_tokens(b"\0" * int(manifest["view_bytes"]))
    assert manifest["view_bytes"] / 2.2 <= TOKEN_BUDGET
    assert view_tokens <= TOKEN_BUDGET
    assert manifest["page_count"] >= 1

    pages = _fetch_all(lifecycle, opened)
    assert max(len(json.dumps(page, indent=2, ensure_ascii=False).encode("utf-8"))
               for page in pages) <= manifest["max_page_bytes"]
    ack = lifecycle.ack_pages(opened["handle"], opened["revision"],
                              [page["page_hash"] for page in pages])
    assert ack["status"] == "complete"

    view = dict(_merge_view_pages(pages))
    assert view_bytes(view) == manifest["view_bytes"]
    # Every analysis row and metric column is still inline and exact in shape.
    rows = view["analysis"]["rows"]
    assert len(rows["rows"]) == TRACKS + 1
    assert len(view["analysis"]["metric_columns"]) == 24
    assert all(row[6:] and None not in row[6:] for row in rows["rows"])
    assert rows["rows"][0][6] == 1.0  # first source, first metric value
    # Every audio row is still inline.
    assert len(view["audio"]["rows"]) == TRACKS + 1
    # Item/take inventories carry all 1000 stable item ids and names.
    assert sum(row[1] for row in view["items"]["rows"]) == ITEMS
    assert sum(row[1] for row in view["takes"]["rows"]) == ITEMS
    assert sum(row[1] for row in view["items"]["rows"]) == 1000
    # No FX parameter values in the map.
    assert "params" not in view["fx"]["columns"]
    assert "parameters" not in view["fx"]["columns"]

    # Exact rows are one stable-id query away, against the canonical record.
    track_guid = _guid(0x4000 + 7, 0)
    track_id = "track:" + track_guid
    items = lifecycle.query(opened["handle"], opened["revision"],
                            data=["items"], objects=[track_id])
    queried = items["data"]["items"]
    assert len(queried) == ITEMS_PER_TRACK
    assert {row["item_id"] for row in queried} == {
        "item:" + _guid(0x1000 + 7, item_index) for item_index in range(ITEMS_PER_TRACK)}
    first = queried[0]
    assert first["position_s"] == 0.5 and first["length_s"] == 9.0
    assert first["interval"]["start_s"] == 0.5 and first["interval"]["end_s"] == 9.5
    assert first["name"] == "Clip 00"

    takes = lifecycle.query(opened["handle"], opened["revision"],
                            data=["takes"], objects=[track_id])
    assert len(takes["data"]["takes"]) == ITEMS_PER_TRACK
    assert takes["data"]["takes"][0]["take_id"] == "take:" + _guid(0x2000 + 7, 0)

    fx = lifecycle.query(opened["handle"], opened["revision"],
                         data=["fx"], objects=[track_id])
    assert len(fx["data"]["fx"]) == 2
    comp = fx["data"]["fx"][0]
    assert comp["name"] == "Comp" and comp["slot_index"] == 0
    assert comp["parameters_available"] is True
    assert len(comp["parameters"]) == 12
    assert comp["parameters"][0]["name"] == "Param 0"


def test_legacy_list_shaped_sections_are_projected_at_read_time(tmp_path: Path) -> None:
    record, _files = _build_record(tmp_path)
    document = build_panorama(record, max_bytes=10 ** 9, max_tokens=10 ** 9)
    data = record.to_mapping()
    project_map = data["project_map"]
    full_items = panorama_module._item_views(data, project_map)
    full_takes = panorama_module._take_views(full_items, project_map)
    legacy = dict(document)
    legacy["items"] = full_items
    legacy["takes"] = full_takes

    view = build_view(legacy)
    assert sum(row[1] for row in view["items"]["rows"]) == ITEMS
    assert sum(row[1] for row in view["takes"]["rows"]) == ITEMS

    track_id = "track:" + _guid(0x4000 + 3, 0)
    result = run_query(legacy, record, data=["items"], objects=[track_id])
    assert len(result["data"]["items"]) == ITEMS_PER_TRACK
    assert result["data"]["items"][0]["item_id"] == "item:" + _guid(0x1000 + 3, 0)
    takes = run_query(legacy, record, data=["takes"], objects=[track_id])
    assert len(takes["data"]["takes"]) == ITEMS_PER_TRACK
