"""A persisted analysis report must survive an MCP process restart.

The server keeps analysis records in a process-local map.  A restart used to
discard a completed multi-minute reference analysis, and the only way back was
to recompute it from the archived capture.  These tests empty that map and prove
the same analysis id is still served from the archive, and that a report whose
capture fact root no longer matches is refused instead of substituted.
"""

import pytest

from reaper_audio import mcp_server
from reaper_audio.context.analysis_store import (analysis_input_fingerprint,
                                                 persist_analysis_record)

PROJECT = "reaper:{A15E3431-9A9E-43E6-AF7A-36231C8F0E79}:C:\\p\\v8.RPP"
PATH = "C:\\p\\v8.RPP"
INSTANCE = "6af87df4-3845e4b87a"
CAPTURE = "cap_test"
ROOT = "a" * 64


def _revision(change_count: int = 6753) -> dict:
    return {"project_id": PROJECT, "path": PATH, "instance_id": INSTANCE,
            "epoch": 10, "change_count": change_count, "capture_id": CAPTURE}


def _persist(archive_root) -> None:
    fingerprint = analysis_input_fingerprint(
        capture_id=CAPTURE,
        project_id=PROJECT,
        revision=_revision(6753),
        revision_after=_revision(7050),
        capture_fact_root=ROOT,
        capture_fact_root_state="available",
        section={"start_s": 150.0, "end_s": 200.0},
        references=[{
            "reference_id": "ref_a", "selection": "path", "window": None,
            "path": str(archive_root / "ref_a.wav"),
            "sha256": "b" * 64, "bytes": 1024,
        }],
        options={
            "include_pairwise": True, "phase_pairs": None,
            "include_partial_loudness": False, "parallel": True,
            "tail_ms": 0, "channels": "stereo", "sample_rate": None,
            "artifact_role": "temporary", "detail": "full",
        },
    )
    persist_analysis_record(archive_root, {
        "analysis_id": "a-test",
        "project_id": PROJECT,
        "revision": _revision(6753),
        "revision_after": _revision(7050),
        "role": "temporary",
        "capture_id": CAPTURE,
        "archive_root": str(archive_root),
        "audio_files": {},
        "report": {"sources": {}, "coverage": {"reference_comparison": "available"}},
    }, fingerprint)


def _archive(tmp_path, monkeypatch, fact_root):
    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setenv("REAPER_ANALYSIS_INDEX", str(tmp_path / "index"))
    monkeypatch.setattr(mcp_server, "_prune_analysis_state", lambda: None)
    monkeypatch.setattr(mcp_server, "_capture_document_root",
                        lambda root, capture_id: fact_root)
    monkeypatch.setattr(mcp_server, "_require_analysis_receipt",
                        lambda receipt, record, archive_root=None: {"complete": True})
    _persist(archive)
    with mcp_server._LOCK:
        mcp_server._analysis_records.clear()
        mcp_server._analysis_roles.clear()
    return archive


def test_restart_serves_evidence_from_the_durable_report(tmp_path, monkeypatch):
    archive = _archive(tmp_path, monkeypatch, ROOT)

    result = mcp_server.reaper_mix_evidence(
        analysis_id="a-test", panorama_receipt="pr_x", archive_root=str(archive))

    assert result["ok"] is True, result
    assert result["analysis_id"] == "a-test"
    assert result["mix_evidence"] is not None


def test_report_for_another_fact_root_is_not_substituted(tmp_path, monkeypatch):
    archive = _archive(tmp_path, monkeypatch, "c" * 64)

    result = mcp_server.reaper_mix_evidence(
        analysis_id="a-test", panorama_receipt="pr_x", archive_root=str(archive))

    assert result["ok"] is False
    assert "fact root" in result["error"]["message"]
