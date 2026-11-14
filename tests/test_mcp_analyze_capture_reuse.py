from __future__ import annotations

import copy
import hashlib
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from reaper_audio.analysis.compare import compare_reports
from reaper_audio.analysis.metrics import measure_audio

from reaper_audio import mcp_server
from reaper_audio.context.full_capture import CaptureArchive, FullCaptureRecord
from reaper_audio.context.lifecycle import PanoramaLifecycle


def _receipt(archive: CaptureArchive, record: FullCaptureRecord) -> str:
    lifecycle = PanoramaLifecycle(str(archive.root), page_bytes=512)
    opened = lifecycle.open(record.project_id, capture_id=record.capture_id)
    hashes = [opened["page_hash"]]
    for index in range(1, opened["page_count"]):
        page = lifecycle.page(opened["handle"], opened["revision"], index)
        hashes.append(page["page_hash"])
    ack = lifecycle.ack(opened["handle"], opened["revision"], hashes)
    assert ack["complete"] is True
    return ack["receipt"]


def _append_capture(tmp_path: Path, *, exact_window: bool = False,
                    legacy_profile: bool = False):
    from test_capture_panorama import _record

    base, files = _record(tmp_path, capture_id=f"cap_reuse_{int(time.time() * 1000)}")
    if exact_window:
        data = copy.deepcopy(mcp_server._jsonable(base.to_mapping()))
        data["time_window"]["project_end_s"] = 13.0
        data["project_map"]["regions"][0]["end_s"] = 13.0
        data["project_map"]["selection"]["time_end_s"] = 13.0
        data["timings"]["batch_render_settings"]["RENDER_TAILMS"] = 0
        for asset in data["audio"]:
            asset["signal_window"]["project_end_s"] = 13.0
            if not legacy_profile:
                profile = data["analysis"][asset["source_guid"]]["curves"][
                    "temporal_profile"
                ]
                for cell in profile["cells"]:
                    # Match the current full-analysis schema used by analyze_bundle.
                    cell["bands"] = [cell["low_db"], cell["mid_db"], cell["high_db"]]
        # The fixture is intentionally changed; let the record recalculate its
        # canonical integrity digest for the edited capture window.
        data["integrity_sha256"] = None
        base = FullCaptureRecord.from_dict(data)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(base, files)
    return base, archive


def _reference(path: Path) -> None:
    sample_rate = 48_000
    time_s = np.arange(sample_rate * 3, dtype=np.float64) / sample_rate
    signal = 0.15 * np.sin(2 * np.pi * 110.0 * time_s)
    sf.write(path, np.column_stack((signal, signal)), sample_rate, subtype="FLOAT")


def test_reaper_analyze_reuses_exact_capture_for_reference_comparison(
        tmp_path: Path, monkeypatch) -> None:
    capture, archive = _append_capture(tmp_path, exact_window=True)
    receipt = _receipt(archive, capture)
    reference = tmp_path / "reference.wav"
    _reference(reference)

    def no_reaper(*args, **kwargs):
        raise AssertionError("archive comparison must not contact or focus REAPER")

    monkeypatch.setattr(mcp_server, "_session", no_reaper)
    monkeypatch.setattr(mcp_server, "_focus_reaper_window", no_reaper)
    result = mcp_server.reaper_analyze(
        project_id=capture.project_id,
        capture_id=capture.capture_id,
        reference_paths=[str(reference)],
        panorama_receipt=receipt,
        archive_root=str(archive.root),
        detail="full",
    )

    assert result["ok"] is True, result
    assert result["capture"]["analysis_reused"] is True
    assert result["execution"]["reaper_render_performed"] is False
    assert result["evidence_revision"]["capture_id"] == capture.capture_id
    assert result["source_count_including_master"] == len(capture.audio)
    report = result["report"]
    assert set(report["reference_comparisons"]) == {
        str(asset["source_guid"]) for asset in capture.audio
    }
    reference_id = result["reference_inputs"][0]["reference_id"]
    reference_report = measure_audio(mcp_server.load_audio(reference))
    first_asset = capture.audio[0]
    first_guid = str(first_asset["source_guid"])
    source_report = mcp_server._capture_measurement_report(
        first_guid, capture.analysis[first_guid], capture_id=capture.capture_id,
        asset_sha256=str(first_asset["sha256"]),
        project_start_s=float(capture.time_window["project_start_s"]),
        project_end_s=float(capture.time_window["project_end_s"]),
    )
    assert report["reference_comparisons"][first_guid][reference_id] == \
        compare_reports(source_report, reference_report)
    assert all(row["capture"]["capture_method"] == "native_render"
               for row in result["sources"])

    expected_path = archive.root / "audio" / "sha256" / (
        f"{capture.audio[0]['sha256']}.wav"
    )
    audio_resource = mcp_server.analysis_audio_resource(
        result["analysis_id"], first_guid)
    assert hashlib.sha256(audio_resource).hexdigest() == capture.audio[0]["sha256"]
    assert audio_resource == expected_path.read_bytes()


def test_reaper_analyze_remeasures_selected_project_window_from_archive_only(
        tmp_path: Path, monkeypatch) -> None:
    capture, archive = _append_capture(tmp_path)
    receipt = _receipt(archive, capture)

    def no_reaper(*args, **kwargs):
        raise AssertionError("archived interval analysis must not contact REAPER")

    monkeypatch.setattr(mcp_server, "_session", no_reaper)
    monkeypatch.setattr(mcp_server, "_focus_reaper_window", no_reaper)
    result = mcp_server.reaper_analyze(
        capture_id=capture.capture_id,
        start_s=10.5,
        end_s=11.5,
        panorama_receipt=receipt,
        archive_root=str(archive.root),
        detail="full",
    )

    assert result["ok"] is True
    assert result["capture"]["analysis_reused"] is False
    assert result["execution"]["reaper_render_performed"] is False
    assert result["execution"]["source_metrics"] == "remeasured_from_archived_wav"
    assert result["analysis_windows"]["measured"] == {
        "start_s": 10.5, "end_s": 11.5,
    }
    first_guid = str(capture.audio[0]["source_guid"])
    measured = result["report"]["sources"][first_guid]["provenance"][
        "measured_section_s"]
    assert measured == {"start_s": 10.5, "end_s": 11.5}
    assert result["analysis_windows"]["tail_s"] == 1.0


def test_reaper_analyze_remeasures_legacy_report_schema_without_render(
        tmp_path: Path, monkeypatch) -> None:
    capture, archive = _append_capture(
        tmp_path, exact_window=True, legacy_profile=True,
    )
    receipt = _receipt(archive, capture)

    def no_reaper(*args, **kwargs):
        raise AssertionError("legacy report fallback must use archive audio only")

    monkeypatch.setattr(mcp_server, "_session", no_reaper)
    monkeypatch.setattr(mcp_server, "_focus_reaper_window", no_reaper)
    result = mcp_server.reaper_analyze(
        capture_id=capture.capture_id,
        panorama_receipt=receipt,
        archive_root=str(archive.root),
        detail="full",
    )

    assert result["ok"] is True, result
    assert result["capture"]["analysis_reused"] is False
    assert result["execution"]["reaper_render_performed"] is False
    assert result["execution"]["source_metrics"] == "remeasured_from_archived_wav"


def test_reaper_analyze_requires_receipt_for_exact_archived_capture(
        tmp_path: Path, monkeypatch) -> None:
    capture, archive = _append_capture(tmp_path, exact_window=True)

    def no_reaper(*args, **kwargs):
        raise AssertionError("receipt refusal must happen before REAPER access")

    monkeypatch.setattr(mcp_server, "_session", no_reaper)
    monkeypatch.setattr(mcp_server, "_focus_reaper_window", no_reaper)
    result = mcp_server.reaper_analyze(
        capture_id=capture.capture_id,
        panorama_receipt="pr_missing",
        archive_root=str(archive.root),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "stale_context", result
    assert result["error"]["details"]["receipt"] == "pr_missing"


def test_reaper_analyze_rejects_tampered_capture_audio_before_reaper_access(
        tmp_path: Path, monkeypatch) -> None:
    capture, archive = _append_capture(tmp_path, exact_window=True)
    receipt = _receipt(archive, capture)
    first_asset = capture.audio[0]
    audio_path = archive.root / "audio" / "sha256" / (
        f"{first_asset['sha256']}.wav"
    )
    audio_path.write_bytes(audio_path.read_bytes() + b"tampered")

    def no_reaper(*args, **kwargs):
        raise AssertionError("tampered archive refusal must not contact REAPER")

    monkeypatch.setattr(mcp_server, "_session", no_reaper)
    monkeypatch.setattr(mcp_server, "_focus_reaper_window", no_reaper)
    result = mcp_server.reaper_analyze(
        capture_id=capture.capture_id,
        panorama_receipt=receipt,
        archive_root=str(archive.root),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "validation_error"
    assert "hash mismatch" in result["error"]["message"].lower()
