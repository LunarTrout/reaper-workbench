"""Exact linked detail: typed refs, batch fetch, hash and revision binding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reaper_audio.context.details import (
    DEFAULT_DETAIL_BYTES,
    resolve_details,
)
from reaper_audio.context.full_capture import CaptureArchive
from reaper_audio.context.lifecycle import PanoramaLifecycle
from reaper_audio.context.panorama import (
    PANORAMA_SCHEMA_VERSION,
    build_panorama,
    panorama_path_for_record,
)
from reaper_audio.errors import PanoramaBindingError, StaleContext, ValidationError


def _record(tmp_path: Path):
    from test_capture_panorama import _record as build

    return build(tmp_path)


def _ack(lifecycle: PanoramaLifecycle, opened: dict) -> dict:
    hashes = []
    for index in range(opened["page_count"]):
        page = lifecycle.page(opened["handle"], opened["revision"], index)
        hashes.append(page["page_hash"])
    return lifecycle.ack(opened["handle"], opened["revision"], hashes)


def test_every_linked_domain_has_a_typed_reference(tmp_path: Path) -> None:
    record, _files = _record(tmp_path)
    document = build_panorama(record)
    entries = document["details"]["entries"]
    assert entries
    kinds = {entry["kind"] for entry in entries}
    assert {"midi_notes", "analysis_series"} <= kinds

    for entry in entries:
        assert entry["schema"]
        assert entry["owner_id"]
        assert entry["fetch"]["tool"] == "reaper_panorama_query"
        assert entry["fetch"]["request"]["kind"] == entry["kind"]
        assert len(entry["content_sha256"]) == 64
        assert entry["capture_id"] == record.capture_id
        assert entry["revision_fingerprint"]
        assert entry["interval"]["interval"] == "[start_s,end_s)"
        assert entry["time_basis"]

    # Every midi row and every analysis row resolves to exactly one entry.
    refs = {entry["ref"] for entry in entries}
    assert all(row["detail"]["ref"] in refs for row in document["midi"])
    assert all(row["detail"]["ref"] in refs for row in document["analysis"])


def test_midi_batch_returns_exact_notes_and_counts(tmp_path: Path) -> None:
    record, _files = _record(tmp_path)
    document = build_panorama(record)
    result = resolve_details(record, document, [
        {"kind": "midi_notes", "id": "take:{TAKE-A}"},
    ])
    assert result["status"] == "detail"
    item = result["results"][0]
    assert item["matches_manifest"] is True
    assert item["count"] == 10001
    assert item["returned"] == 2
    assert item["truncated"] is True
    assert [note["pitch"] for note in item["notes"]] == [40, 43]
    assert item["notes"][0]["start_s"] == 10.0
    assert item["notes"][0]["end_ppq"] == 0.5
    assert item["notes"][0]["note_duration_s"] == 0.5
    assert item["notes"][0]["note_duration_qn"] == 0.5
    assert "note_duration_s" in item["derived_fields"]

    windowed = resolve_details(record, document, [
        {"kind": "midi_notes", "id": "take:{TAKE-A}", "start_s": 10.0, "end_s": 10.6},
    ])
    assert windowed["results"][0]["returned"] == 1
    assert windowed["results"][0]["interval"]["start_s"] == 10.0


def test_series_batch_returns_exact_cells_and_optional_resolution(tmp_path: Path) -> None:
    record, _files = _record(tmp_path)
    document = build_panorama(record)
    result = resolve_details(record, document, [
        {"kind": "analysis_series", "id": "analysis:{TRACK-A}",
         "series": ["rms_400ms"]},
    ])
    series = result["results"][0]["series"]["rms_400ms"]
    assert series["times_s"] == [0.0, 0.1, 0.2, 0.3]
    assert series["values"] == [-22.0, -21.0, -20.0, -19.0]
    assert series["matches_manifest"] is True

    coarse = resolve_details(record, document, [
        {"kind": "analysis_series", "id": "analysis:{TRACK-A}",
         "series": ["rms_400ms"], "resolution_s": 0.2},
    ])["results"][0]["series"]["rms_400ms"]
    assert coarse["resolution_s"] == 0.2
    assert coarse["aggregation"] == "mean_min_max_p50"
    assert len(coarse["buckets"]) == 2
    assert coarse["buckets"][0]["mean"] == -21.5
    assert coarse["buckets"][0]["min"] == -22.0

    band = resolve_details(record, document, [
        {"kind": "analysis_series", "id": "analysis:{TRACK-A}",
         "series": ["band_level"], "resolution_s": 0.2},
    ])["results"][0]["series"]["band_level"]
    assert band["band_centers_hz"] == [100.0, 1000.0]
    # Two band cells at 0.0 and 0.1 s collapse into one bucket ending at 0.1 s.
    assert len(band["buckets"]) == 1
    assert band["buckets"][0]["count"] == 2
    assert band["buckets"][0]["mean"] == [-29.5, -34.5]


def test_unknown_reference_and_tampered_payload_fail_closed(tmp_path: Path) -> None:
    record, _files = _record(tmp_path)
    document = build_panorama(record)
    with pytest.raises(ValidationError) as raised:
        resolve_details(record, document, [{"kind": "midi_notes", "id": "take:nope"}])
    assert raised.value.details["kind"] == "midi_notes"

    tampered = json.loads(json.dumps(document))
    for entry in tampered["details"]["entries"]:
        if entry["kind"] == "midi_notes":
            entry["content_sha256"] = "0" * 64
    with pytest.raises(PanoramaBindingError):
        resolve_details(record, tampered, [{"kind": "midi_notes", "id": "take:{TAKE-A}"}])


def test_detail_batch_reports_the_byte_budget(tmp_path: Path) -> None:
    record, _files = _record(tmp_path)
    document = build_panorama(record)
    result = resolve_details(record, document, [
        {"kind": "analysis_series", "id": "analysis:{TRACK-A}"},
        {"kind": "analysis_series", "id": "analysis:{TRACK-B}"},
    ], max_bytes=64)
    assert result["status"] == "detail_too_large"
    assert result["max_bytes"] == 64
    assert len(result["requests"]) == 2
    assert result["next_call"]["tool"] == "reaper_panorama_query"


def test_lifecycle_detail_requires_ack_and_exact_revision(tmp_path: Path) -> None:
    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)
    lifecycle = PanoramaLifecycle(archive.root)
    opened = lifecycle.open(record.project_id)
    requests = [{"kind": "midi_notes", "id": "take:{TAKE-A}"}]

    with pytest.raises(StaleContext):
        lifecycle.detail(opened["handle"], opened["revision"], requests=requests)

    receipt = _ack(lifecycle, opened)
    assert receipt["complete"] is True
    result = lifecycle.detail(opened["handle"], opened["revision"], requests=requests)
    assert result["status"] == "detail"
    assert result["capture_id"] == record.capture_id
    assert result["fact_root"] == opened["fact_root"]
    assert result["evidence"]["results"][0]["returned"] == 2
    assert result["state"]["scope"] == "project"

    wrong = dict(opened["revision"])
    wrong["change_count"] = 999
    with pytest.raises(StaleContext):
        lifecycle.detail(opened["handle"], wrong, requests=requests)


def test_sidecar_records_the_current_schema_and_legacy_is_upgraded_in_memory(
        tmp_path: Path) -> None:
    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archived = archive.append(record, files)
    sidecar = panorama_path_for_record(Path(archived.record_path))
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["schema_version"] == PANORAMA_SCHEMA_VERSION

    # Simulate an archived document written by the previous representation.
    legacy = json.loads(json.dumps(data))
    legacy["schema_version"] = "reaper-workbench.capture-panorama/1"
    legacy.pop("details", None)
    from reaper_audio.context.panorama import _canonical

    legacy.pop("document_sha256", None)
    import hashlib

    legacy["document_sha256"] = hashlib.sha256(_canonical(legacy)).hexdigest()
    sidecar.write_text(json.dumps(legacy), encoding="utf-8")
    before = sidecar.read_bytes()

    lifecycle = PanoramaLifecycle(archive.root)
    opened = lifecycle.open(record.project_id)
    assert opened["manifest"]["page_count"] >= 1
    assert sidecar.read_bytes() == before  # never rewritten on read


def test_mcp_query_selects_exact_rows_by_meaning(tmp_path: Path) -> None:
    from reaper_audio import mcp_server

    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)
    root = str(archive.root)
    from panorama_flow import open_and_ack
    _page, ack, _pages = open_and_ack(mcp_server, record.project_id, root)
    assert ack["status"] == "complete"
    receipt = ack["receipt"]

    series = mcp_server.reaper_panorama_query(
        ack["revision"], root, ack["handle"],
        data=["series"], series=["rms_400ms"], panorama_receipt=receipt)
    assert series["status"] == "query"
    detail = series["data"]["exact_detail"]
    assert detail["status"] == "detail"
    assert detail["results"]
    first = detail["results"][0]["series"]["rms_400ms"]
    assert first["times_s"] == [0.0, 0.1, 0.2, 0.3]

    notes = mcp_server.reaper_panorama_query(
        ack["revision"], root, ack["handle"],
        data=["midi_notes"], objects=["take:{TAKE-A}"], panorama_receipt=receipt)
    assert notes["data"]["exact_detail"]["results"][0]["returned"] == 2

    refused = mcp_server.reaper_panorama_query(
        ack["revision"], root, ack["handle"], data=["series"])
    assert refused["ok"] is False
    assert "handle and panorama_receipt" in refused["error"]["message"]
    assert "read_scope" in refused["error"]["details"]["next_action"]


def test_mcp_query_refuses_a_stale_or_mismatched_handle(tmp_path: Path) -> None:
    from reaper_audio import mcp_server

    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)
    root = str(archive.root)
    from panorama_flow import open_and_ack
    _page, ack, _pages = open_and_ack(mcp_server, record.project_id, root)

    stale = {**ack["revision"], "document_hash": "0" * 64}
    answer = mcp_server.reaper_panorama_query(
        stale, root, ack["handle"], data=["items"],
        panorama_receipt=ack["receipt"])
    assert answer["ok"] is False
    assert "another" in answer["error"]["message"].lower()


def test_legacy_sidecar_is_upgraded_on_capture_read(tmp_path: Path) -> None:
    import hashlib

    from reaper_audio.context.panorama import _canonical
    from reaper_audio.mcp_server import _read_capture_panorama, reaper_panorama_open

    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archived = archive.append(record, files)
    sidecar = panorama_path_for_record(Path(archived.record_path))
    legacy = json.loads(sidecar.read_text(encoding="utf-8"))
    legacy["schema_version"] = "reaper-workbench.capture-panorama/1"
    legacy.pop("details", None)
    legacy.pop("document_sha256", None)
    legacy["document_sha256"] = hashlib.sha256(_canonical(legacy)).hexdigest()
    sidecar.write_text(json.dumps(legacy), encoding="utf-8")
    before = sidecar.read_bytes()

    read = _read_capture_panorama(record.capture_id, str(archive.root))
    assert read["panorama_source"] == "read_time_schema_upgrade"
    assert read["panorama"]["schema_version"] == PANORAMA_SCHEMA_VERSION
    assert read["panorama"]["details"]["entries"]
    assert sidecar.read_bytes() == before


def test_envelope_reference_round_trips_exact_points(tmp_path: Path) -> None:
    from test_capture_panorama import TRACK_A
    from reaper_audio.context.full_capture import FullCaptureRecord

    record, _files = _record(tmp_path)
    data = record.to_mapping(deep=True)
    data["integrity_sha256"] = None
    points = [
        {"index": 0, "time_s": 10.0, "value": 0.0, "shape": 0, "tension": 0.0},
        {"index": 1, "time_s": 11.0, "value": 0.5, "shape": 0, "tension": 0.0},
        {"index": 2, "time_s": 12.0, "value": 1.0, "shape": 0, "tension": 0.0},
    ]
    data["project_map"]["envelopes"]["tracks"] = [{
        "track_guid": TRACK_A,
        "envelopes": [{
            "guid": "{ENV-A}", "name": "Volume", "active": True, "armed": False,
            "visible": True, "point_count": 3, "points": points,
            "automation_item_count": 0, "automation_items": [],
        }],
    }]
    record = FullCaptureRecord.from_dict(data)
    document = build_panorama(record)

    row = document["envelopes"]["envelopes"][0]
    assert row["point_count"] == 3
    assert row["interval"]["start_s"] == 10.0
    assert row["interval"]["end_s"] == 12.0
    assert row["value_range"] == {"min": 0.0, "max": 1.0}
    assert row["target"]["parameter"] == "Volume"
    assert row["target"]["units_basis"] == "not_reported_by_reaper"
    assert "points" not in row
    assert row["detail"]["fetch"]["tool"] == "reaper_panorama_query"

    full = resolve_details(record, document, [
        {"kind": "envelope_points", "id": row["envelope_id"]},
    ])["results"][0]
    assert full["returned"] == 3
    assert [point["value"] for point in full["points"]] == [0.0, 0.5, 1.0]
    assert full["matches_manifest"] is True

    windowed = resolve_details(record, document, [
        {"kind": "envelope_points", "id": row["envelope_id"],
         "start_s": 10.5, "end_s": 12.0},
    ])["results"][0]
    # Half-open interval: the point at 12.0 belongs to the next interval.
    assert windowed["returned"] == 1
    inclusive = resolve_details(record, document, [
        {"kind": "envelope_points", "id": row["envelope_id"],
         "start_s": 10.5, "end_s": 12.01},
    ])["results"][0]
    assert inclusive["returned"] == 2


def test_linked_payload_change_is_visible_in_the_fact_view(tmp_path: Path) -> None:
    """A changed note must move the fact root even though notes are linked."""
    from reaper_audio.context.full_capture import FullCaptureRecord
    from reaper_audio.context.lifecycle import stable_fact_view

    record, _files = _record(tmp_path)
    data = record.to_mapping(deep=True)
    data["integrity_sha256"] = None
    changed = FullCaptureRecord.from_dict(data)
    first = stable_fact_view(build_panorama(changed))

    data["project_map"]["midi"][0]["events"]["notes"][0]["pitch"] = 41
    second = stable_fact_view(build_panorama(FullCaptureRecord.from_dict(data)))

    assert first["details"]["entries"][0]["content_sha256"] !=         second["details"]["entries"][0]["content_sha256"]
    assert first != second
