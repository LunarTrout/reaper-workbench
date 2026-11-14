from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from reaper_audio.context.full_capture import CaptureArchive, FullCaptureRecord, REQUIRED_MAP_KEYS
from reaper_audio.daw.full_capture import _archived_record_binding
from reaper_audio.context.panorama import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_TOKENS,
    MIDI_NOTE_LIMIT,
    _canonical,
    _time_summary,
    estimate_tokens,
    PANORAMA_FILE_NAME,
    PANORAMA_SCHEMA_VERSION,
    PanoramaBindingError,
    PanoramaCapacityError,
    build_panorama,
    load_panorama,
    validate_binding,
    write_panorama_atomic,
)
from reaper_audio.errors import ValidationError
from reaper_audio.mcp_server import _read_capture_panorama


TRACK_A = "{TRACK-A}"
TRACK_B = "{TRACK-B}"
MASTER = "{MASTER}"
ITEM_A = "{ITEM-A}"
ITEM_B = "{ITEM-B}"
TAKE_A = "{TAKE-A}"
TAKE_B = "{TAKE-B}"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _audio(path: Path, frames: int = 144000) -> None:
    sf.write(path, np.zeros((frames, 2)), 48000, subtype="PCM_24")


def _analysis(guid: str) -> dict:
    return {
        "channels": 2,
        "duration_s": 3.0,
        "sample_rate": 48000,
        "section": None,
        "provenance": {
            "analysis": "test", "decoder": "soundfile", "stage": "file_or_buffer",
            "file_frames": 144000, "file_channels": 2,
            "measured_section_s": {"start_s": 0.0, "end_s": 3.0},
            "section_requested": None, "start_s": 0.0,
        },
        "metrics": {
            "integrated_lufs": {"value": -18.0, "unit": "LUFS",
                                 "confidence": None,
                                 "note": "computed on the captured file"},
            "peak_dbfs": {"value": -6.0, "unit": "dBFS",
                          "confidence": 0.9, "note": None},
        },
        "coverage": {"levels": "complete", "loudness": "available",
                     "true_peak": "estimated"},
        "curves": {
            "rms_400ms_dbfs": [-22.0, -21.0, -20.0, -19.0],
            "rms_400ms_time_s": [0.0, 0.1, 0.2, 0.3],
            "band_centers_hz": [100.0, 1000.0],
            "band_level_dbfs": [-30.0, -35.0],
            "band_level_time_dbfs": [[-30.0, -35.0], [-29.0, -34.0]],
            "spectrogram_time_s": [0.0, 0.1],
            "events_2ms": {
                "count": 2, "onsets": 1, "spikes": 0, "clips": 0,
                "onset_rate_hz": 0.5, "regularity": 0.1,
                "max_peak_db": -5.0, "min_gap_ms": 20.0,
                "events": [{"time_s": 0.1}, {"time_s": 0.2}],
            },
            "temporal_profile": {
                "hop_s": 0.1, "lufs": -18.0, "lra": 1.0,
                "onset_rate_hz": 0.5, "jitter_pct": 1.0,
                "mod_depth": 0.1, "mod_rate_hz": 2.0,
                "sii_proxy": 0.5,
                "times": [0.0, 0.1, 0.2],
                "cells": [
                    {"rms_db": -22.0, "peak": 0.2, "crest_db": 12.0,
                     "centroid_hz": 500.0, "low_db": -30.0, "mid_db": -35.0,
                     "high_db": -40.0, "width": 0.8, "pan": 0.0,
                     "corr": 1.0, "tempo_bpm": 120.0,
                     "momentary_lufs": -20.0, "short_term_lufs": -20.0},
                    {"rms_db": -21.0, "peak": 0.3, "crest_db": 13.0,
                     "centroid_hz": 600.0, "low_db": -29.0, "mid_db": -34.0,
                     "high_db": -39.0, "width": 0.7, "pan": 0.1,
                     "corr": 0.9, "tempo_bpm": 120.0,
                     "momentary_lufs": -19.0, "short_term_lufs": -19.0},
                    {"rms_db": -20.0, "peak": 0.4, "crest_db": 14.0,
                     "centroid_hz": 700.0, "low_db": -28.0, "mid_db": -33.0,
                     "high_db": -38.0, "width": 0.6, "pan": 0.2,
                     "corr": 0.8, "tempo_bpm": 120.0,
                     "momentary_lufs": -18.0, "short_term_lufs": -18.0},
                ],
                "tempo": {"points": [{"time_s": 0.0, "bpm": 120.0}],
                          "method": "test"},
            },
        },
    }


def _record(tmp_path: Path, capture_id: str = "cap_panorama",
            asset_duration_s: float = 3.0) -> tuple[FullCaptureRecord, dict[str, Path]]:
    empty = {name: True for name in REQUIRED_MAP_KEYS - {"coverage"}}
    coverage = {name: True for name in
                REQUIRED_MAP_KEYS - {"coverage"} | {"audio", "analysis"}}
    revision = {"project_id": "reaper:test", "change_count": 7, "path": "test.rpp",
                "instance_id": "pri_test", "epoch": 1,
                "epoch_basis": "bridge_session_project_generation"}
    map_track_a = {
        "guid": TRACK_A, "name": "Source A", "channel_count": 2,
        "folder_depth": 0, "volume": 0.5, "pan": -0.25, "mute": False,
        "solo": 0, "selected": False, "record_arm": 0, "record_mode": 0,
        "record_mode_flags": 0, "sends": [{"index": 0, "dest_guid": TRACK_B,
                                            "volume": 1.0, "pan": 0.0,
                                            "mode": 0, "mute": False}],
        "receives": 0, "hardware_sends": 0,
        "items": [{
            "guid": ITEM_A, "position_s": 10.0, "length_s": 3.0,
            "start_offset_s": 0.0, "source_length_s": 3.0,
            "source_sample_rate": 48000, "source_channels": 2,
            "source_length_is_qn": False, "beat_attach_mode": 0,
            "channel_mode": 0, "playrate": 1.0, "mute": False,
            "loop_source": False, "fade_in_s": 0.0, "fade_out_s": 0.0,
            "selected": False, "take_count": 3,
            "active_take": {
                "guid": TAKE_A, "name": "Take A", "is_midi": True,
                "playrate": 1.0, "preserve_pitch": True,
                "stretch_markers": 0, "fx_count": 0,
                "source": str(tmp_path / "source-a.wav"),
            },
        }],
        "fx": [{"index": 0, "guid": "{FX-A}", "name": "Test FX",
                "enabled": True, "offline": False, "parameter_count": 2,
                "parameters": [
                    {"index": 0, "name": "Gain", "normalized": 0.5,
                     "formatted": "0.0"},
                    {"index": 1, "name": "Mix", "normalized": 1.0,
                     "formatted": "100"},
                ]}],
    }
    map_track_b = {
        "guid": TRACK_B, "name": "Source B", "channel_count": 2,
        "folder_depth": 0, "volume": 1.0, "pan": 0.25, "mute": False,
        "solo": 0, "selected": False, "record_arm": 0, "record_mode": 0,
        "record_mode_flags": 0, "sends": [], "receives": 1,
        "hardware_sends": 0,
        "items": [{
            "guid": ITEM_B, "position_s": 20.0, "length_s": 70.0,
            "start_offset_s": 0.0, "source_length_s": 70.0,
            "source_sample_rate": 48000, "source_channels": 2,
            "source_length_is_qn": False, "beat_attach_mode": 0,
            "channel_mode": 0, "playrate": 1.0, "mute": False,
            "loop_source": False, "fade_in_s": 0.0, "fade_out_s": 0.0,
            "selected": True, "take_count": 1,
            "active_take": {
                "guid": TAKE_B, "name": "Take B", "is_midi": False,
                "playrate": 1.0, "preserve_pitch": True,
                "stretch_markers": 0, "fx_count": 0,
                "source": str(tmp_path / "source-b.wav"),
            },
        }],
        "fx": [],
    }
    master = {
        "guid": MASTER, "name": "MASTER", "channel_count": 2,
        "folder_depth": 0, "volume": 1.0, "pan": 0.0, "mute": False,
        "solo": 0, "selected": False, "items": [], "sends": 0,
        "receives": 0, "hardware_sends": 0,
        "fx": [{"index": 0, "guid": "{FX-M}", "name": "Master FX",
                "enabled": True, "offline": False, "parameter_count": 1,
                "parameters": [{"index": 0, "name": "Gain",
                                "normalized": 1.0, "formatted": "0.0"}]}],
    }
    project_map = {
        "tracks": [map_track_a, map_track_b],
        "master": master,
        "items": [
            {**map_track_a["items"][0], "track_guid": TRACK_A,
             "source_path": str(tmp_path / "source-a.wav")},
            {**map_track_b["items"][0], "track_guid": TRACK_B,
             "source_path": str(tmp_path / "source-b.wav")},
        ],
        "takes": [
            map_track_a["items"][0]["active_take"],
            map_track_b["items"][0]["active_take"],
        ],
        "midi": [{
            "take_guid": TAKE_A,
            "events": {
                "note_count": 10001, "returned_note_count": 2,
                "truncated": True,
                "notes": [
                    {"index": 0, "pitch": 40, "velocity": 100, "channel": 0,
                     "start_s": 10.0, "end_s": 10.5,
                     "start_ppq": 0.0, "end_ppq": 0.5},
                    {"index": 1, "pitch": 43, "velocity": 90, "channel": 0,
                     "start_s": 11.0, "end_s": 11.25,
                     "start_ppq": 1.0, "end_ppq": 1.25},
                ],
            },
        }],
        "fx": [map_track_a["fx"][0], master["fx"][0]],
        "routing": [{"source_guid": TRACK_A, "dest_guid": TRACK_B,
                     "index": 0, "volume": 1.0, "pan": 0.0, "mode": 0,
                     "mute": False}],
        "envelopes": {
            "complete": True, "items_scanned": 2, "gaps": [],
            "master": [], "fx": [], "items": [],
            "tracks": [{"track_guid": TRACK_A, "envelopes": []},
                       {"track_guid": TRACK_B, "envelopes": []}],
        },
        "automation": {
            "complete": True, "items_scanned": 2, "gaps": [],
            "master": [], "fx": [], "items": [],
            "tracks": [{"track_guid": TRACK_A, "envelopes": []},
                       {"track_guid": TRACK_B, "envelopes": []}],
        },
        "tempo": {
            "bpm": 120, "numerator": 4, "denominator": 4,
            "markers": [
                {"time_s": 0.0, "bpm": 120, "numerator": 4,
                 "denominator": 4, "measure": 0, "beat": 0, "linear": False},
                {"time_s": 50.0, "bpm": 180, "numerator": 3,
                 "denominator": 4, "measure": 25, "beat": 0, "linear": False},
            ],
        },
        "time_signatures": [
            {"time_s": 0.0, "bpm": 120, "numerator": 4, "denominator": 4,
             "measure": 0, "beat": 0, "linear": False},
            {"time_s": 50.0, "bpm": 180, "numerator": 3, "denominator": 4,
             "measure": 25, "beat": 0, "linear": False},
        ],
        "markers": [{"time_s": 10.0, "name": "Marker"}],
        "regions": [{"start_s": 10.0, "end_s": 13.0, "name": "Region",
                     "render": True}],
        "selection": {"track_guids": [TRACK_B], "time_start_s": 10.0,
                      "time_end_s": 13.0},
        "transport": {"play_state": 0, "cursor_s": 10.0,
                      "edit_cursor_s": 10.0, "play_position_s": 0.0},
        "coverage": empty,
        "digest": {
            "change_count": 7,
            "fields": {
                "project": {"settings": {"render_endpos": 100.0,
                                         "project_srate": 48000}},
                "tracks": [
                    {"guid": TRACK_A, "sends": map_track_a["sends"]},
                    {"guid": TRACK_B, "sends": []},
                ],
                "automation": {},
                "envelopes": {},
            },
        },
    }
    files: dict[str, Path] = {}
    audio = []
    for guid, name, is_master in ((TRACK_A, "source-a", False),
                                  (TRACK_B, "source-b", False),
                                  (MASTER, "master", True)):
        path = tmp_path / f"{name}.wav"
        _audio(path, frames=144000)
        digest = _sha(path)
        files[guid] = path
        audio.append({
            "asset_id": f"ast_sha256_{digest}", "source_guid": guid,
            "name": name, "is_master": is_master,
            "stage": "master_mix_post_fx" if is_master else
            "selected_track_via_master_parallel_batch_post_fx",
            "capture_method": "native_render", "sample_rate": 48000,
            "channels": 2, "frames": 144000,
            "duration_s": asset_duration_s,
            "bytes": path.stat().st_size, "sha256": digest,
            "signal_window": {"project_start_s": 10.0, "project_end_s": 12.0,
                               "file_start_s": 0.0, "file_end_s": 3.0},
            "capture_revision": revision, "soloed_guid": None,
            "status": "leased",
            "lease_id": "job-test",
        })
    analysis = {guid: _analysis(guid) for guid in files}
    record = FullCaptureRecord(
        capture_id=capture_id, project_id="reaper:test",
        project_path="test.rpp", revision_before=revision,
        revision_after=revision,
        time_window={"project_start_s": 10.0, "project_end_s": 12.0,
                     "file_start_s": 0.0, "file_end_s": 3.0},
        project_map=project_map, audio=audio, analysis=analysis,
        coverage=coverage, missing=[], created_at="2026-01-01T00:00:00+00:00",
        producer="test", restore_report={"no_media_items_added": True},
        timings={"batch_render_settings": {"RENDER_TAILMS": 1000}},
    )
    return record, files


def test_new_capture_archive_gets_automatic_sidecar_and_reads_it(tmp_path: Path) -> None:
    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archived = archive.append(record, files)
    sidecar = Path(archived.record_path).parent / PANORAMA_FILE_NAME
    assert sidecar.is_file()
    panorama = load_panorama(sidecar, archive.read(record.capture_id))
    assert panorama["schema_version"] == PANORAMA_SCHEMA_VERSION
    result = _read_capture_panorama(record.capture_id, str(archive.root))
    assert result["panorama_source"] == "sidecar"
    assert result["panorama"]["capture_binding"]["capture_id"] == record.capture_id


def test_capture_binding_uses_archived_integrity_hash_without_reloading_record(
        tmp_path: Path) -> None:
    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archived = archive.append(record, files)
    sidecar = Path(archived.record_path).parent / PANORAMA_FILE_NAME

    sealed = archive.read(record.capture_id)
    binding = _archived_record_binding(record, archived)

    assert archived.integrity_sha256 == sealed.integrity_sha256
    assert archived.integrity_sha256 != record.integrity_sha256
    panorama = load_panorama(sidecar, binding)
    assert panorama["capture_binding"]["record_integrity_sha256"] == (
        archived.integrity_sha256)

    read = _read_capture_panorama(record.capture_id, str(archive.root))
    assert read["capture"]["integrity_sha256"] == archived.integrity_sha256
    assert read["panorama"]["capture_binding"]["record_integrity_sha256"] == (
        archived.integrity_sha256)
    assert read["panorama_source"] == "sidecar"


def test_audio_render_provenance_distinguishes_track_and_master(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    panorama = build_panorama(record)
    track = next(row for row in panorama["audio"] if not row["is_master"])
    master = next(row for row in panorama["audio"] if row["is_master"])
    track_provenance = track["render_provenance"]
    master_provenance = master["render_provenance"]

    assert track_provenance["complete"] is True
    assert master_provenance["complete"] is True
    assert track_provenance["capture_method"] == "native_render"
    assert master_provenance["capture_method"] == "native_render"
    assert track_provenance["stage"] == (
        "selected_track_via_master_parallel_batch_post_fx")
    assert master_provenance["stage"] == "master_mix_post_fx"
    assert track_provenance["capture_revision_change_count"] == 7
    assert master_provenance["capture_revision_change_count"] == 7
    assert len(track_provenance["capture_revision_sha256"]) == 64
    assert (track_provenance["capture_revision_sha256"] ==
            master_provenance["capture_revision_sha256"])
    assert track_provenance["soloed_guid"] is None
    assert master_provenance["soloed_guid"] is None
    assert track["source_id"] is not None
    assert master["source_id"] is not None
    revisions = panorama["audio_capture_revisions"]
    assert len(revisions) == 1
    assert track_provenance["capture_revision_sha256"] in revisions
    assert revisions[track_provenance["capture_revision_sha256"]]["change_count"] == 7
    assert all(row["render_provenance"]["complete"] for row in panorama["audio"])

    incomplete_record = record.to_mapping()
    incomplete_audio = [dict(asset) for asset in incomplete_record["audio"]]
    incomplete_audio[0].pop("soloed_guid", None)
    incomplete_audio[0].pop("capture_revision", None)
    incomplete_record["audio"] = incomplete_audio
    incomplete_record["integrity_sha256"] = None
    incomplete = build_panorama(incomplete_record)
    assert incomplete["audio"][0]["render_provenance"]["complete"] is False


def test_valid_legacy_sidecar_is_rebuilt_when_render_provenance_is_missing(
        tmp_path: Path) -> None:
    record, files = _record(tmp_path, "cap_legacy_provenance")
    archive = CaptureArchive(tmp_path / "archive")
    archived = archive.append(record, files)
    sidecar = Path(archived.record_path).parent / PANORAMA_FILE_NAME
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    for audio in data["audio"]:
        audio.pop("render_provenance", None)
    data.pop("document_sha256", None)
    data["document_sha256"] = hashlib.sha256(_canonical(data)).hexdigest()
    write_panorama_atomic(sidecar, data)
    legacy_bytes = sidecar.read_bytes()
    record_bytes = Path(archived.record_path).read_bytes()

    result = _read_capture_panorama(record.capture_id, str(archive.root))

    assert result["panorama_source"] == "read_time_stale_sidecar_contract"
    assert all(row["render_provenance"]["complete"]
               for row in result["panorama"]["audio"])
    assert sidecar.read_bytes() == legacy_bytes
    assert Path(archived.record_path).read_bytes() == record_bytes


def test_old_capture_read_builds_without_touching_archive(tmp_path: Path) -> None:
    record, files = _record(tmp_path, "cap_old")
    archive = CaptureArchive(tmp_path / "archive")
    archived = archive.append(record, files)
    sidecar = Path(archived.record_path).parent / PANORAMA_FILE_NAME
    sidecar.unlink()
    before = Path(archived.record_path).read_bytes()
    result = _read_capture_panorama("cap_old", str(archive.root))
    assert result["panorama_source"] == "read_time"
    assert result["panorama"]["schema_version"] == PANORAMA_SCHEMA_VERSION
    assert not sidecar.exists()
    assert Path(archived.record_path).read_bytes() == before


def test_panorama_is_self_contained_with_complete_domains_and_crosslinks(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    panorama = build_panorama(record)
    required = {
        "project", "time_map", "tracks", "master", "items", "takes",
        "midi", "fx", "routes", "automation", "envelopes", "markers",
        "regions", "selection", "transport", "audio", "analysis",
        "crosslinks", "per_track", "coverage", "epistemic_layers",
    }
    assert required <= set(panorama)
    assert len(panorama["tracks"]) == 2
    assert panorama["master"]["is_master"] is True
    assert len(panorama["items"]) == 2
    assert len(panorama["takes"]) == 2
    assert len(panorama["audio"]) == 3
    assert len(panorama["analysis"]) == 3
    assert panorama["crosslinks"]
    assert panorama["coverage"]["complete"] is False
    assert panorama["epistemic_layers"]["model_interpretations"]["included"] is False
    assert panorama["epistemic_layers"]["causal_hypotheses"]["included"] is False
    assert panorama["reading_contract"]["requires_pagination"] is False
    assert panorama["reading_contract"]["requires_external_tool_call"] is False
    ids = {link["from"] for link in panorama["crosslinks"]}
    ids.update(link["to"] for link in panorama["crosslinks"])
    for track in panorama["tracks"]:
        assert track["stable_id"] in ids
        assert track["audio_id"] in ids
        assert track["analysis_id"] in ids


def test_panorama_lengths_windows_tail_and_file_mapping(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    panorama = build_panorama(record)
    project = panorama["project"]
    assert project["project_extent"]["end_s"] == 100.0
    assert project["project_extent"]["basis"] == "derived_from_reaper_render_endpos_not_project_extent"
    assert project["project_extent"]["reported_by_reaper"] is False
    assert project["content_extent"]["end_s"] == 90.0
    assert project["content_extent"]["basis"] == "derived_from_all_project_item_intervals"
    assert project["capture_window"]["project_start_s"] == 10.0
    assert project["capture_window"]["project_end_s"] == 12.0
    assert project["capture_window"]["interval"] == "[start_s,end_s)"
    assert project["render_tail"]["present"] is True
    assert project["render_tail"]["start_s"] == 12.0
    assert project["render_tail"]["end_s"] == 13.0
    audio = panorama["audio"][0]
    assert audio["format"]["frame_count"] == 144000
    assert audio["format"]["sample_rate_hz"] == 48000
    assert audio["format"]["channel_count"] == 2
    assert audio["format"]["duration_s"] == 3.0
    # The file/sample mapping formulas live once in audio_contract; each asset
    # keeps the values that differ plus the tail geometry.
    contract = panorama["audio_time_contract"]
    assert contract["project_window"]["project_start_s"] == 10.0
    assert contract["interval"] == "[start_s,end_s)"
    assert "audio_file_time_s" in contract["file_to_project"]
    mapping = audio["mapping"]
    assert mapping["file_start_s"] == 0.0
    assert mapping["file_end_s"] == 3.0
    assert mapping["mapped_file_end_s"] == 2.0
    assert mapping["valid_end_sample_exclusive"] == 96000
    assert mapping["tail_start_sample"] == 96000
    assert mapping["tail_end_sample_exclusive"] == 144000
    assert mapping["tail_sample_count"] == 48000
    assert mapping["tail_present"] is True
    assert mapping["tail_start_s"] == 12.0
    assert mapping["tail_end_s"] == 13.0
    assert mapping["tail_maps_to_project"] is False
    item = next(row for row in panorama["items"] if row["guid"] == ITEM_A)
    assert item["length_s"] == 3.0
    assert item["item_duration_s"] == 3.0
    assert item["interval"]["start_s"] == 10.0
    assert item["interval"]["end_s"] == 13.0
    # Notes are linked exactly, not reprinted; the summary still carries the
    # population, occupied interval and truncation state.
    midi = panorama["midi"][0]
    assert midi["note_count"] == 10001
    assert midi["returned_note_count"] == 2
    assert midi["truncated"] is True
    assert midi["occupied_interval"]["start_s"] == 10.0
    assert midi["occupied_interval"]["end_s"] == 11.25
    assert midi["pitch_range"] == {"min": 40, "max": 43}
    assert midi["detail"]["kind"] == "midi_notes"
    assert midi["detail"]["fetch"]["tool"] == "reaper_panorama_query"
    assert "notes" not in midi
    entry = next(row for row in panorama["details"]["entries"]
                 if row["ref"] == midi["detail"]["ref"])
    assert entry["count"] == 10001
    assert entry["truncated"] is True


def test_panorama_variable_tempo_is_revision_bound_and_not_base_bpm_derived(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    panorama = build_panorama(record)
    time_map = panorama["time_map"]
    assert time_map["revision_bound"] is True
    assert time_map["revision"] == record.revision_before
    assert [point["bpm"] for point in time_map["tempo_points"]] == [120.0, 180.0]
    assert time_map["musical_coordinates"]["computed_from_single_bpm"] is False
    assert time_map["map_source"].endswith("time_signatures")


def test_panorama_active_take_and_midi_limit_are_not_complete(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    panorama = build_panorama(record)
    coverage = panorama["coverage"]
    assert coverage["domains"]["takes"]["expected_count"] == 4
    assert coverage["domains"]["takes"]["observed_count"] == 2
    assert coverage["domains"]["takes"]["active_take_only"] is True
    assert coverage["domains"]["takes"]["status"] == "partial"
    assert coverage["domains"]["midi"]["midi_note_limit"] == MIDI_NOTE_LIMIT
    assert coverage["domains"]["midi"]["truncated"] is True
    assert "midi.note_limit" in coverage["truncated"]
    assert coverage["complete"] is False


def test_panorama_temporal_summary_is_declared_and_exactly_addressable(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    panorama = build_panorama(record)
    grid = panorama["analysis_temporal"]
    assert grid["representation"] == "cross_source_activity_grid"
    assert grid["units"] == "dBFS"
    assert grid["aggregation"] == "mean_per_bucket"
    assert grid["time_basis"]["start_s"] == "project_seconds"
    assert grid["bucket_width_s"] > 0
    assert grid["source_count"] == len(panorama["analysis"])
    assert grid["buckets"]
    for bucket in grid["buckets"]:
        assert set(bucket) == {"start_s", "end_s", "rms_dbfs_by_source"}
        assert bucket["end_s"] > bucket["start_s"]
        for source_id, value in bucket["rms_dbfs_by_source"].items():
            assert source_id.startswith(("track:", "master:"))
            assert isinstance(value, float)
    # The grid declares its resolution instead of pretending to be the raw curve.
    assert grid["coverage"]["note"].startswith("a source without RMS cells")
    # Whole-window statistics stay per source; exact cells are linked.
    row = panorama["analysis"][0]
    assert row["rms_stats"]["count"] >= 1
    assert row["rms_stats"]["units"] == "dBFS"
    assert row["series"]["rms_400ms"]["count"] == 4
    entry = next(item for item in panorama["details"]["entries"]
                 if item["ref"] == row["detail"]["ref"])
    assert entry["kind"] == "analysis_series"
    assert entry["series"]["rms_400ms"]["count"] == 4
    assert len(entry["series"]["rms_400ms"]["content_sha256"]) == 64
    assert entry["series"]["rms_400ms"]["content_sha256"]
    assert entry["record_integrity_sha256"] == record.integrity_sha256


def test_audio_duration_must_match_frames_and_sample_rate(tmp_path: Path) -> None:
    record, _ = _record(tmp_path, asset_duration_s=0.03)
    with pytest.raises(ValidationError) as raised:
        build_panorama(record)
    assert "frames / sample_rate" in str(raised.value)
    assert raised.value.details["frames"] == 144000
    assert raised.value.details["derived_duration_s"] == 3.0


def test_exact_reaper_project_extent_is_not_render_endpos(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    data = record.to_dict()
    data["integrity_sha256"] = None
    data["project_map"]["digest"]["fields"]["project"]["settings"][
        "project_extent_s"] = 101.25
    record = FullCaptureRecord.from_dict(data)
    panorama = build_panorama(record)
    extent = panorama["project"]["project_extent"]
    assert extent["end_s"] == 101.25
    assert extent["basis"] == "reaper_reported_project_extent"
    assert extent["source_field"] == "project_extent_s"
    assert extent["reported_by_reaper"] is True
    assert extent["derived"] is False


def test_record_mapping_does_not_thaw_the_analysis_payload(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    shallow = record.to_mapping()
    deep = record.to_dict()
    assert shallow["analysis"] is record.analysis
    assert deep["analysis"] is not record.analysis
    assert shallow["project_map"] == deep["project_map"]


def test_panorama_does_not_mutate_canonical_track_maps(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    before = record.project_map["tracks"][0]
    assert "stable_id" not in before
    build_panorama(record)
    assert "stable_id" not in before


def test_panorama_token_budget_failure_is_structured(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    with pytest.raises(PanoramaCapacityError) as raised:
        build_panorama(record, max_tokens=10)
    details = raised.value.details
    assert details["estimated_tokens"] > 10
    assert details["max_tokens"] == 10
    assert details["raw_series_included"] is False


def test_panorama_capacity_failure_is_structured(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    with pytest.raises(PanoramaCapacityError) as raised:
        build_panorama(record, max_bytes=100)
    error = raised.value
    assert error.code == "panorama_capacity_exceeded"
    assert error.details["estimated_bytes"] > 100
    assert error.details["max_bytes"] == 100
    assert error.details["raw_series_included"] is False


def test_panorama_is_deterministic_and_binding_rejects_tampering(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    first = build_panorama(record)
    second = build_panorama(record)
    assert first == second
    path = tmp_path / PANORAMA_FILE_NAME
    write_panorama_atomic(path, first)
    validate_binding(first, record)
    tampered = dict(first)
    tampered["capture_binding"] = dict(first["capture_binding"])
    tampered["capture_binding"]["capture_id"] = "cap_other"
    with pytest.raises(PanoramaBindingError):
        validate_binding(tampered, record)
    content_tampered = json.loads(json.dumps(first))
    content_tampered["project"]["counts"]["tracks"] = 99
    with pytest.raises(PanoramaBindingError):
        validate_binding(content_tampered, record)


def test_tampered_sidecar_is_rejected_by_read(tmp_path: Path) -> None:
    record, files = _record(tmp_path, "cap_tampered_sidecar")
    archive = CaptureArchive(tmp_path / "archive")
    archived = archive.append(record, files)
    sidecar = Path(archived.record_path).parent / PANORAMA_FILE_NAME
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["project"]["counts"]["tracks"] = 999
    sidecar.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PanoramaBindingError):
        _read_capture_panorama("cap_tampered_sidecar", str(archive.root))


def test_panorama_has_no_pagination_contract(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    panorama = build_panorama(record)
    contract = panorama["reading_contract"]
    assert contract["requires_pagination"] is False
    assert contract["requires_external_tool_call"] is False
    assert "next_page" not in panorama
    assert "cursor" not in panorama


def test_panorama_missing_or_tampered_asset_is_rejected_by_archive(tmp_path: Path) -> None:
    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archived = archive.append(record, files)
    archive_audio = Path(archived.record_path).parent / "record.json"
    data = json.loads(archive_audio.read_text(encoding="utf-8"))
    blob = Path(archived.audio_directory) / (
        Path(data["audio"][0]["archive_audio"]).name)
    blob.write_bytes(b"tampered")
    with pytest.raises(Exception):
        archive.read(record.capture_id)


def test_temporal_grid_covers_every_source_without_positional_columns(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    panorama = build_panorama(record)
    table = panorama["analysis_temporal"]
    assert table["representation"] == "cross_source_activity_grid"
    assert table["source_count"] == len(panorama["analysis"])
    assert table["coverage"]["expected_sources"] == table["coverage"]["observed_sources"]
    assert table["coverage"]["status"] == "complete"
    assert table["coverage"]["sources_without_rms_cells"] == []
    assert [row["source_id"] for row in table["sources"]] == [
        row["source_id"] for row in panorama["analysis"]]
    assert len({row["source_id"] for row in table["sources"]}) == table["source_count"]
    assert "stable_id_legend" not in panorama
    assert panorama["analysis_metric_units"]["integrated_lufs"] == "LUFS"
    assert panorama["analysis_metric_units"]["true_peak_estimate_dbfs"] == "dBTP"
    # No positional rows: every bucket names its sources and every analysis row
    # names its metrics.
    assert all(isinstance(bucket["rms_dbfs_by_source"], dict)
               for bucket in table["buckets"])
    assert all(isinstance(value, float)
               for bucket in table["buckets"]
               for value in bucket["rms_dbfs_by_source"].values())
    assert all(isinstance(row["metrics"], dict) for row in panorama["analysis"])


def test_panorama_token_budget_is_declared_and_measured_before_return(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    panorama = build_panorama(record)
    assert DEFAULT_MAX_TOKENS == 256_000
    assert DEFAULT_MAX_BYTES == 700_000
    assert estimate_tokens(_canonical(panorama)) <= DEFAULT_MAX_TOKENS
    # The projection must not silently drop a domain.
    assert panorama["domain_counts"]["analysis"] == len(panorama["analysis"])
    assert panorama["domain_counts"]["temporal_sources"] == len(panorama["analysis"])
    assert panorama["truncation"]["silent_drop"] is False
    assert panorama["reading_contract"]["requires_detail_fetch_for_exact_rows"] is True


def test_time_summary_honours_requested_coarse_levels() -> None:
    times = [i * 0.1 for i in range(201)]  # 20 s
    values = [-30.0 - (i % 7) for i in range(201)]
    summary = _time_summary(
        times, values, units="dBFS", pointer="#/analysis/test/curves/rms",
        record_integrity="deadbeef", record_path="captures/test/record.json",
        levels=[20.0, 30.0],
    )
    assert [level["bucket_width_s"] for level in summary["levels"]] == [20.0, 30.0]
    assert len(summary["levels"][0]["buckets"]) == 1
    assert summary["levels"][0]["buckets"][0]["start_s"] == 0.0
    assert summary["levels"][0]["buckets"][0]["end_s"] == 20.0


def test_temporal_grid_and_domain_counts_agree(tmp_path: Path) -> None:
    record, _ = _record(tmp_path)
    panorama = build_panorama(record)
    table = panorama["analysis_temporal"]
    assert table["source_count"] == len(panorama["analysis"])
    assert table["coverage"]["status"] == "complete"
    assert table["coverage"]["expected_sources"] == table["coverage"]["observed_sources"]
    assert len(table["sources"]) == table["source_count"]
    assert panorama["domain_counts"]["tracks"] == 2
    assert panorama["domain_counts"]["audio"] == 3
    assert panorama["domain_counts"]["analysis"] == 3
    assert panorama["domain_counts"]["temporal_sources"] == table["source_count"]
    assert panorama["domain_counts"]["detail_refs"] == len(panorama["details"]["entries"])
    assert panorama["truncation"]["truncated"] == panorama["coverage"]["truncated"]
    assert panorama["truncation"]["missing"] == panorama["coverage"]["missing"]


def test_real_capture_sidecars_have_coarse_levels_and_full_coverage() -> None:
    root = Path(__file__).resolve().parents[1]
    cases = {
        "cap_01M79659V69NDC5YNAA0SMPX51": [20.0, 30.0],
        "cap_01M7923N26X1PSCH2FK32MQSH4": [30.0, 300.0],
    }
    for capture_id, expected_levels in cases.items():
        sidecar = (root / "work/benchmark/himmp/capture-archive/captures" /
                   capture_id / "panorama.json")
        if not sidecar.is_file():
            continue
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        table = data["analysis_temporal"]
        assert table["source_count"] == 42
        assert table["coverage"]["expected"] == 42
        assert table["coverage"]["observed"] == 42
        assert table["coverage"]["status"] == "complete"
        assert table["truncated"] is False
        assert [level["bucket_width_s"] for level in table["levels"]] == expected_levels
        assert len(data["tracks"]) == 41
        assert len(data["analysis"]) == 42
        stages = [(row.get("is_master"), row["render_provenance"]["stage"])
                  for row in data["audio"]]
        assert stages.count((True, "master_mix_post_fx")) == 1
        assert stages.count((False, "selected_track_via_master_parallel_batch_post_fx")) == 41
        assert all(row["render_provenance"]["complete"] for row in data["audio"])
        assert all(row["source_id"] for row in data["audio"])
        revisions = data["audio_capture_revisions"]
        for row in data["audio"]:
            assert row["render_provenance"]["capture_revision_sha256"] in revisions
