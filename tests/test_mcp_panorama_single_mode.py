"""The single-response panorama mode is opt-in and does not change the old flow."""

from __future__ import annotations

from pathlib import Path

from reaper_audio import mcp_server
from reaper_audio.context.full_capture import CaptureArchive
from reaper_audio.context.panorama_view import DEFAULT_RESPONSE_BYTES

from test_capture_panorama import _record


def _archive(tmp_path: Path) -> tuple[CaptureArchive, str]:
    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)
    return archive, record.project_id


def test_single_mode_returns_the_view_receipt_and_token_in_one_result(tmp_path: Path) -> None:
    archive, project_id = _archive(tmp_path)

    opened = mcp_server.reaper_panorama_open(project_id, str(archive.root), mode="single")

    assert opened["status"] == "complete"
    assert opened["delivery"]["mode"] == "single_response"
    assert opened["delivery"]["pages"] is False
    assert opened["delivery"]["requires_client_reassembly"] is False
    assert opened["delivery"]["response_bytes"] <= DEFAULT_RESPONSE_BYTES
    assert opened["receipt"].startswith("pr_")
    assert opened["context_token"].startswith("ct_")
    assert isinstance(opened["view"], dict)
    assert "manifest" not in opened


def test_default_mode_is_auto_and_small_view_fits_single(tmp_path: Path) -> None:
    archive, project_id = _archive(tmp_path)

    opened = mcp_server.reaper_panorama_open(project_id, str(archive.root))

    assert opened["status"] == "complete"
    assert opened["delivery"]["mode"] == "single_response"
    assert opened["delivery"]["requested_mode"] == "auto"
    assert "view" in opened
    assert "manifest" not in opened


def test_large_audio_delta_is_paged_with_every_changed_cell(tmp_path: Path) -> None:
    import time

    archive, project_id = _archive(tmp_path)
    opened = mcp_server.reaper_panorama_open(project_id, str(archive.root), mode="single")
    revision = opened["revision"]
    capture_id = opened["capture_id"]
    guid = opened["view"]["entities"]["track"][0][0]
    times = [index * 0.01 for index in range(2500)]

    def report(values):
        return {
            "project": {"project_id": project_id},
            "section": {"start_s": 0.0, "end_s": 25.0},
            "sources": {guid: {
                "metrics": {"integrated_lufs": {"value": values[0], "unit": "LUFS"}},
                "curves": {"rms_400ms_time_s": times, "rms_400ms_dbfs": values[1]},
            }},
        }

    records = {
        "base-audio": report((-14.0, [-20.0] * 2500)),
        "target-audio": report((-12.5, [-19.0] * 2500)),
    }
    for analysis_id, body in records.items():
        mcp_server._analysis_records[analysis_id] = {
            "analysis_id": analysis_id, "project_id": project_id,
            "capture_id": capture_id, "revision": revision,
            "report": body, "expires_at": time.time() + 300,
        }
    try:
        result = mcp_server.reaper_mix_evidence(
            "base-audio", panorama_receipt=opened["receipt"],
            archive_root=str(archive.root), compare_analysis_id="target-audio")
    finally:
        for analysis_id in records:
            mcp_server._analysis_records.pop(analysis_id, None)

    assert result["status"] == "result_page"
    assert result["result_kind"] == "audio_delta"
    manifest = result["manifest"]
    ids = manifest.get("page_ids") or [
        f"rp_{index}_of_{manifest['page_count']}"
        for index in range(1, manifest["page_count"] + 1)]
    pages = [result] + [
        mcp_server.reaper_panorama_page(result["handle"], result["revision"],
                                        str(archive.root), page_id)
        for page_id in ids[1:]]
    from reaper_audio.context.lifecycle import _merge_view_pages
    merged = dict(_merge_view_pages(pages))
    assert len(merged["series"]["rows"][0]["cells"]) == 2500
    ack = mcp_server.reaper_panorama_ack(
        result["handle"], result["revision"], str(archive.root),
        [page["page_hash"] for page in pages])
    assert ack["status"] == "complete"
    assert ack["context_token"] is None
    assert ack["mutation_unlocked"] is False


def test_pages_mode_is_always_available(tmp_path: Path) -> None:
    archive, project_id = _archive(tmp_path)

    opened = mcp_server.reaper_panorama_open(project_id, str(archive.root),
                                             mode="pages")

    assert opened["status"] == "page"
    assert opened["page_id"] == opened["manifest"]["page_ids"][0]
    assert "view" not in opened
    assert "receipt" not in opened


def test_unknown_mode_is_rejected_without_opening(tmp_path: Path) -> None:
    archive, project_id = _archive(tmp_path)

    result = mcp_server.reaper_panorama_open(project_id, str(archive.root),
                                             mode="chunked")

    assert result["ok"] is False
    assert result["error"]["code"] == "validation_error"


def test_capability_tools_mode_is_the_logical_surface() -> None:
    surface = mcp_server.reaper_capabilities(mode="tools")

    assert surface["surface"] == "logical_task_groups"
    groups = surface["groups"]
    assert set(groups) >= {"session", "project", "capture", "panorama",
                           "analysis", "edit", "discovery"}
    for name, group in groups.items():
        assert group["purpose"] and group["use_when"] and group["context"]
        assert group["tools"], name
    # Existing tools are grouped, never duplicated into one tool per operation.
    all_tools = [tool for group in groups.values() for tool in group["tools"]]
    assert len(all_tools) == len(set(all_tools))


def test_mix_evidence_compare_returns_the_exact_audio_delta(tmp_path: Path) -> None:
    import time

    archive, project_id = _archive(tmp_path)
    opened = mcp_server.reaper_panorama_open(project_id, str(archive.root), mode="single")
    revision = opened["revision"]
    capture_id = opened["capture_id"]
    guid = opened["view"]["entities"]["track"][0][0]

    def report(values):
        return {
            "project": {"project_id": project_id},
            "section": {"start_s": 0.0, "end_s": 2.0},
            "sources": {guid: {
                "metrics": {"integrated_lufs": {"value": values[0], "unit": "LUFS"}},
                "curves": {"rms_400ms_time_s": [0.0, 0.4, 0.8, 1.2],
                           "rms_400ms_dbfs": values[1]},
            }},
        }

    records = {
        "base-analysis": report((-14.0, [-20.0, -20.0, -20.0, -20.0])),
        "target-analysis": report((-12.5, [-20.0, -18.0, -18.0, -20.0])),
    }
    for analysis_id, body in records.items():
        mcp_server._analysis_records[analysis_id] = {
            "analysis_id": analysis_id,
            "project_id": project_id,
            "capture_id": capture_id,
            "revision": revision,
            "report": body,
            "expires_at": time.time() + 300,
        }
    try:
        result = mcp_server.reaper_mix_evidence(
            "base-analysis", panorama_receipt=opened["receipt"],
            archive_root=str(archive.root),
            compare_analysis_id="target-analysis")
    finally:
        for analysis_id in records:
            mcp_server._analysis_records.pop(analysis_id, None)

    assert result["mode"] == "compare"
    assert result["audio_delta"]["schema_version"] == "reaper-workbench.audio-delta/1"
    metric = result["audio_delta"]["metrics"]["rows"][0]
    assert metric["metric"] == "integrated_lufs"
    assert metric["delta"] == 1.5
    series = result["audio_delta"]["series"]["rows"][0]
    assert series["changed_intervals"] == [
        {"start_s": 0.4, "end_s": 1.2, "cells": 2, "first_cell": 1, "last_cell": 2,
         "end_basis": "next_stored_cell_start"},
    ]
    assert series["changed_intervals_unbounded_cells"] == 0
    assert result["audio_delta"]["freshness"]["state"] == "current"
