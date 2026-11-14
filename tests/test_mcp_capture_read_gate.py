"""Strict MCP archive-read gate: no receipt, no panorama payload."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from reaper_audio import mcp_server
from reaper_audio.context.full_capture import CaptureArchive
from reaper_audio.context.lifecycle import PanoramaLifecycle


def _receipt_for(archive: CaptureArchive, record) -> tuple[str, PanoramaLifecycle]:
    lifecycle = PanoramaLifecycle(str(archive.root), page_bytes=180)
    opened = lifecycle.open(record.project_id, capture_id=record.capture_id)
    hashes = [opened["page_hash"]]
    for index in range(1, opened["page_count"]):
        hashes.append(lifecycle.page(opened["handle"], opened["revision"], index)["page_hash"])
    ack = lifecycle.ack(opened["handle"], opened["revision"], hashes)
    assert ack["complete"] is True
    return ack["receipt"], lifecycle


def _append(tmp_path: Path, capture_id: str = "cap_gate"):
    from test_capture_panorama import _record

    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    record, files = _record(tmp_path, capture_id=capture_id)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)
    return record, archive


def test_capture_read_without_receipt_never_builds_or_returns_panorama(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record, archive = _append(tmp_path)
    called = False

    def _must_not_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("internal panorama reader ran before the gate")

    monkeypatch.setattr(mcp_server, "_read_capture_panorama", _must_not_run)
    result = mcp_server.reaper_capture_read(record.capture_id, str(archive.root))
    assert result["ok"] is False
    assert "panorama" not in result
    assert result["error"]["details"]["gate"] == "panorama_capture_read"
    assert "panorama_open" in result["error"]["details"]["next_action"]
    assert called is False


def test_partial_ack_mints_no_receipt_and_read_stays_closed(tmp_path: Path) -> None:
    record, archive = _append(tmp_path)
    lifecycle = PanoramaLifecycle(str(archive.root), page_bytes=180)
    opened = lifecycle.open(record.project_id, capture_id=record.capture_id)
    partial = lifecycle.ack(opened["handle"], opened["revision"],
                            opened["manifest"]["page_hashes"])
    assert partial["complete"] is False
    assert "receipt" not in partial
    result = mcp_server.reaper_capture_read(record.capture_id, str(archive.root))
    assert result["ok"] is False
    assert "panorama" not in result
    assert result["error"]["details"]["gate"] == "panorama_capture_read"


def test_stale_wrong_project_and_wrong_capture_receipts_are_rejected(tmp_path: Path) -> None:
    record_a, archive = _append(tmp_path / "a", "cap_gate_a")
    receipt_a, lifecycle = _receipt_for(archive, record_a)
    record_b, _ = _append(tmp_path / "b", "cap_gate_b")

    # The exact receipt for A remains valid after B is appended, but it cannot
    # be used to read B.
    archive_b = CaptureArchive((tmp_path / "b" / "archive"))
    receipt_b, _ = _receipt_for(archive_b, record_b)
    wrong = mcp_server.reaper_capture_read(record_a.capture_id, str(archive.root),
                                           panorama_receipt=receipt_b)
    assert wrong["ok"] is False and "panorama" not in wrong
    wrong = mcp_server.reaper_capture_read(record_b.capture_id, str(archive_b.root),
                                           panorama_receipt=receipt_a)
    assert wrong["ok"] is False and "panorama" not in wrong

    opened = lifecycle.open(record_a.project_id, capture_id=record_a.capture_id)
    base = {
        "schema_version": 1,
        "receipt": "pr_wrong",
        "handle": opened["handle"],
        "project_id": record_a.project_id,
        "capture_id": record_a.capture_id,
        "revision": opened["revision"],
        "fact_root": opened["fact_root"],
        "document_hash": opened["document_hash"],
        "complete": True,
        "created_at": time.time(),
    }
    stale_path = lifecycle.receipts_dir / "pr_stale.json"
    stale_path.write_text(json.dumps({**base, "receipt": "pr_stale",
                                      "document_hash": "wrong-document"}),
                          encoding="utf-8")
    stale = mcp_server.reaper_capture_read(record_a.capture_id, str(archive.root),
                                           panorama_receipt="pr_stale")
    assert stale["ok"] is False and "panorama" not in stale

    wrong_project_path = lifecycle.receipts_dir / "pr_wrong_project.json"
    wrong_project_path.write_text(json.dumps({**base, "receipt": "pr_wrong_project",
                                              "project_id": "other-project"}),
                                  encoding="utf-8")
    wrong = mcp_server.reaper_capture_read(record_a.capture_id, str(archive.root),
                                           panorama_receipt="pr_wrong_project")
    assert wrong["ok"] is False and "panorama" not in wrong


def test_exact_historical_capture_receipt_unlocks_only_that_capture(tmp_path: Path) -> None:
    record_a, archive = _append(tmp_path, "cap_gate_old")
    receipt_a, _ = _receipt_for(archive, record_a)
    from test_capture_panorama import _record

    (tmp_path / "new").mkdir(parents=True, exist_ok=True)
    record_b, files_b = _record(tmp_path / "new", capture_id="cap_gate_new")
    archive.append(record_b, files_b)
    # B is the latest capture in the same archive. A's exact receipt must still
    # validate against A, not silently bind to the latest capture.
    receipt_b, _ = _receipt_for(archive, record_b)
    read = mcp_server.reaper_capture_read(record_a.capture_id, str(archive.root),
                                          panorama_receipt=receipt_a)
    assert read["delivery"]["view_delivered"] is True
    assert read["delivery"]["capture_id"] == record_a.capture_id
    assert read["panorama"]["capture_binding"]["capture_id"] == record_a.capture_id

    wrong = mcp_server.reaper_capture_read(record_a.capture_id, str(archive.root),
                                           panorama_receipt=receipt_b)
    assert wrong["ok"] is False and "panorama" not in wrong
