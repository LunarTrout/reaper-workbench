"""Stage D2 acceptance: per-source documents and the thin manifest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reaper_audio.context.evidence_documents import (
    EvidenceDocumentError,
    build_source_document,
    manifest_for,
    read_source_document,
    write_capture_documents,
)

DATA = {
    "capture_id": "cap_test",
    "project_id": "reaper:{P}:C:/x/REAPER-CONTEXT-V2-TEST.RPP",
    "revision_before": {"change_count": 7},
    "time_window": {"start_s": 0.0, "end_s": 15.0},
    "integrity_sha256": "a" * 64,
    "audio": [
        {"source_guid": "{A}", "is_master": False, "render_key": "rk1:aa",
         "render_key_complete": True, "capture_method": "native_render",
         "asset_id": "as_1"},
        {"source_guid": "{M}", "is_master": True, "render_key": "rk1:mm",
         "render_key_complete": True, "capture_method": "native_render",
         "asset_id": "as_2"},
    ],
    "analysis": {"{A}": {"metrics": {"rms_dbfs": -18.0}}, "{M}": {"metrics": {"lufs_i": -14.0}}},
    "project_map": {"items": {"tracks": [{"guid": "{A}", "name": "GTR"}],
                              "master": {"guid": "{M}"}}},
    "relations": {"edges": [{"from": "{A}", "to": "{B}"}]},
}


def test_build_source_document_is_self_contained() -> None:
    document = build_source_document(DATA, "track:{A}")
    assert document["source_id"] == "track:{A}"
    assert document["fingerprint"]["render_key"] == "rk1:aa"
    assert document["analysis"]["metrics"]["rms_dbfs"] == -18.0
    assert document["project"]["items"]["tracks"][0]["name"] == "GTR"
    assert document["relations"] == [{"from": "{A}", "to": "{B}"}]


def test_master_document_is_marked_and_isolated() -> None:
    document = build_source_document(DATA, "master:{M}")
    assert document["is_master"] is True
    assert document["analysis"]["metrics"]["lufs_i"] == -14.0


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    manifest = write_capture_documents(archive_root=tmp_path, capture_id="cap_test",
                                       data=DATA, record_bytes=4_800_000)
    assert set(manifest["sources"]) == {"track:{A}", "master:{M}"}
    assert manifest["sources"]["track:{A}"]["complete"] is True
    document = read_source_document(tmp_path, "cap_test", "track:{A}")
    assert document["analysis"]["metrics"]["rms_dbfs"] == -18.0


def test_manifest_is_thin_and_points_at_the_record(tmp_path: Path) -> None:
    manifest = write_capture_documents(archive_root=tmp_path, capture_id="cap_test",
                                       data=DATA, record_bytes=4_800_000)
    on_disk = json.loads((tmp_path / "captures" / "cap_test" / "manifest.json")
                         .read_text(encoding="utf-8"))
    assert on_disk["record"]["bytes"] == 4_800_000
    assert on_disk["record"]["path"].endswith("record.json")
    assert manifest["sources"]["track:{A}"]["doc"].startswith("captures/cap_test/documents/")


def test_rewriting_with_different_bytes_is_refused(tmp_path: Path) -> None:
    write_capture_documents(archive_root=tmp_path, capture_id="cap_test",
                            data=DATA, record_bytes=1)
    tampered = json.loads(json.dumps(DATA))
    tampered["analysis"]["{A}"]["metrics"]["rms_dbfs"] = -3.0
    with pytest.raises(EvidenceDocumentError):
        write_capture_documents(archive_root=tmp_path, capture_id="cap_test",
                                data=tampered, record_bytes=1)


def test_missing_manifest_reads_as_none(tmp_path: Path) -> None:
    assert manifest_for(tmp_path, "cap_old") is None


def test_unknown_source_is_refused(tmp_path: Path) -> None:
    write_capture_documents(archive_root=tmp_path, capture_id="cap_test",
                            data=DATA, record_bytes=1)
    with pytest.raises(EvidenceDocumentError):
        read_source_document(tmp_path, "cap_test", "track:{ZZZ}")
