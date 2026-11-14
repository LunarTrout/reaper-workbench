"""Paged delivery of the model panorama: exactness, receipt and gating.

The view is split on whole top-level sections, so merging page payloads in order
must reproduce the view byte-for-byte.  These tests also pin the adaptive page
count (no fixed maximum: N grows with the project, each page under the cap) and
the fail-closed gates that only a complete receipt unlocks.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from reaper_audio.context.full_capture import CaptureArchive
from reaper_audio.context.lifecycle import (
    VIEW_PAGE_MAX_BYTES,
    PanoramaLifecycle,
    _pack_view_sections,
)
from reaper_audio.context.panorama import _hash, build_panorama
from reaper_audio.context.panorama_view import build_view, canonical as view_canonical
from reaper_audio.errors import StaleContext, ValidationError


def _document(tmp_path: Path, sources: int = 3):
    from test_single_view import _record

    record, files = _record(tmp_path)
    document = json.loads(view_canonical(
        build_panorama(record, max_bytes=10 ** 9, max_tokens=10 ** 9)))
    if sources <= len(document.get("tracks") or []):
        return record, files, document
    metric_names = sorted(document["analysis_metric_units"])
    base_track = document["tracks"][0]
    base_analysis = document["analysis"][0]
    base_audio = document["audio"][0]
    master = document["master"]
    tracks, analysis, audio = [], [], []
    for index in range(sources - 1):
        guid = "{SCALE-%03d}" % index
        stable = "track:" + guid
        tracks.append({**base_track, "guid": guid, "stable_id": stable,
                       "analysis_id": "analysis:" + guid, "audio_id": "audio:" + guid})
        analysis.append({**base_analysis, "analysis_id": "analysis:" + guid,
                         "source_id": stable, "source_guid": guid,
                         "metrics": {name: float(index + offset + 1)
                                     for offset, name in enumerate(metric_names)}})
        audio.append({**base_audio, "source_id": stable, "audio_id": "audio:" + guid,
                      "analysis_id": "analysis:" + guid})
    master_guid = master["guid"]
    analysis.append({**base_analysis, "analysis_id": "analysis:" + master_guid,
                     "source_id": "master:" + master_guid, "source_guid": master_guid,
                     "metrics": {name: 100.0 + offset
                                 for offset, name in enumerate(metric_names)}})
    audio.append({**base_audio, "source_id": "master:" + master_guid,
                  "audio_id": "audio:" + master_guid,
                  "analysis_id": "analysis:" + master_guid, "is_master": True})
    document["tracks"] = tracks
    document["analysis"] = analysis
    document["audio"] = audio
    return record, files, document


def _archive(tmp_path: Path, sources: int = 3):
    record, files, document = _document(tmp_path, sources)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)
    payload = {key: value for key, value in document.items() if key != "document_sha256"}
    document["document_sha256"] = _hash(payload)
    (archive.captures / record.capture_id / "panorama.json").write_text(
        view_canonical(document).decode("utf-8"), encoding="utf-8")
    lifecycle = PanoramaLifecycle(archive.root)
    return archive, record, lifecycle


def _fetch_all(lifecycle: PanoramaLifecycle, opened: dict) -> list[dict]:
    pages = [opened]
    for page_id in opened["manifest"]["page_ids"][1:]:
        pages.append(lifecycle.view_page(opened["handle"], opened["revision"], page_id))
    return pages


def _merged(pages: list[dict]) -> dict:
    from reaper_audio.context.lifecycle import _merge_view_pages
    return dict(_merge_view_pages(pages))


def test_pages_reconstruct_the_view_exactly(tmp_path: Path) -> None:
    _archive_obj, _record_obj, lifecycle = _archive(tmp_path, sources=12)
    opened = lifecycle.open_view(_record_obj.project_id)
    pages = _fetch_all(lifecycle, opened)

    assert opened["status"] == "page"
    assert opened["page_id"] == opened["manifest"]["page_ids"][0]
    assert opened["page_count"] == len(pages)
    assert opened["view_sha256"] == opened["manifest"]["view_sha256"]

    merged = _merged(pages)
    rebuilt = _hash({key: value for key, value in merged.items()
                     if key != "view_sha256"})
    assert rebuilt == opened["view_sha256"]
    assert set(merged) == set(build_view(
        json.loads((_archive_obj.captures / _record_obj.capture_id
                    / "panorama.json").read_text(encoding="utf-8")), lean=False))

    ack = lifecycle.ack_pages(opened["handle"], opened["revision"],
                              [page["page_hash"] for page in pages])
    assert ack["status"] == "complete"
    assert ack["reconstructed"] is True
    assert ack["page_count"] == len(pages)
    assert ack["receipt"].startswith("pr_")
    assert ack["context_token"].startswith("ct_")


def test_missing_page_blocks_ack_and_lists_it(tmp_path: Path) -> None:
    _archive_obj, record, lifecycle = _archive(tmp_path, sources=12)
    opened = lifecycle.open_view(record.project_id, page_target_bytes=8_000)
    assert opened["page_count"] > 1
    pages = [opened] + [lifecycle.view_page(opened["handle"], opened["revision"], page_id)
                        for page_id in opened["manifest"]["page_ids"][1:-1]]

    ack = lifecycle.ack_pages(opened["handle"], opened["revision"],
                              [page["page_hash"] for page in pages])
    assert ack["status"] == "incomplete"
    assert ack["complete"] is False
    assert ack["missing_pages"] == [opened["manifest"]["page_ids"][-1]]
    assert "receipt" not in ack


def test_duplicate_page_fetch_is_idempotent(tmp_path: Path) -> None:
    _archive_obj, record, lifecycle = _archive(tmp_path, sources=12)
    opened = lifecycle.open_view(record.project_id, page_target_bytes=8_000)
    second_id = opened["manifest"]["page_ids"][1]

    first = lifecycle.view_page(opened["handle"], opened["revision"], second_id)
    again = lifecycle.view_page(opened["handle"], opened["revision"], second_id)
    assert first["page_hash"] == again["page_hash"]
    assert again["delivery"]["pages_received"] == first["delivery"]["pages_received"]

    _fetch_all(lifecycle, opened)
    assert lifecycle.view_page(opened["handle"], opened["revision"],
                               second_id)["page_hash"] == first["page_hash"]
    ack = lifecycle.ack_pages(opened["handle"], opened["revision"],
                              opened["manifest"]["page_hashes"])
    assert ack["status"] == "complete"


def test_wrong_or_reordered_hashes_fail(tmp_path: Path) -> None:
    _archive_obj, record, lifecycle = _archive(tmp_path, sources=12)
    opened = lifecycle.open_view(record.project_id, page_target_bytes=8_000)
    _fetch_all(lifecycle, opened)
    hashes = list(opened["manifest"]["page_hashes"])

    with pytest.raises(StaleContext):
        lifecycle.ack_pages(opened["handle"], opened["revision"],
                            list(reversed(hashes)))
    tampered = list(hashes)
    tampered[0] = "0" * 64
    with pytest.raises(StaleContext):
        lifecycle.ack_pages(opened["handle"], opened["revision"], tampered)


def test_unknown_page_id_and_stale_revision_fail(tmp_path: Path) -> None:
    _archive_obj, record, lifecycle = _archive(tmp_path, sources=12)
    opened = lifecycle.open_view(record.project_id, page_target_bytes=8_000)

    with pytest.raises(ValidationError) as excinfo:
        lifecycle.view_page(opened["handle"], opened["revision"], "pg_99_of_99")
    assert opened["manifest"]["page_ids"][0] in excinfo.value.details["page_ids"]

    stale = dict(opened["revision"])
    stale["change_count"] = int(stale.get("change_count") or 0) + 1
    with pytest.raises(StaleContext):
        lifecycle.view_page(opened["handle"], stale, opened["page_id"])


def test_page_count_is_adaptive_and_every_page_fits(tmp_path: Path) -> None:
    for sources, expected_min in ((42, 1), (101, 2)):
        case = tmp_path / f"sources-{sources}"
        case.mkdir()
        _archive_obj, record, lifecycle = _archive(case, sources=sources)
        opened = lifecycle.open_view(record.project_id)
        pages = _fetch_all(lifecycle, opened)

        assert opened["page_count"] == len(pages) >= expected_min
        view_bytes = opened["manifest"]["view_bytes"]
        assert view_bytes > 30_000
        sizes = [len(view_canonical(page)) for page in pages]
        assert max(sizes) <= VIEW_PAGE_MAX_BYTES, sizes
        # A split section repeats its head fields in every part, so the sum of
        # payload bytes is bounded by the view plus that repetition, never less.
        assert sum(page["page_bytes"] for page in pages) <= view_bytes * 3
        assert _hash({key: value for key, value in _merged(pages).items()
                      if key != "view_sha256"}) == opened["view_sha256"]
        ack = lifecycle.ack_pages(opened["handle"], opened["revision"],
                                  [page["page_hash"] for page in pages])
        assert ack["status"] == "complete"


def test_receipt_gate_refuses_before_ack_and_opens_after(tmp_path: Path) -> None:
    from reaper_audio import mcp_server

    archive, record, lifecycle = _archive(tmp_path, sources=12)
    opened = lifecycle.open_view(record.project_id)
    lifecycle_path = str(archive.root)
    # the MCP gate resolves its own lifecycle store for the archive root
    mcp_server._panorama_lifecycle(lifecycle_path)
    receipt = None
    with pytest.raises(ValidationError):
        mcp_server._require_panorama_receipt(receipt, record.project_id,
                                             opened["revision"], lifecycle_path)

    pages = _fetch_all(lifecycle, opened)
    ack = lifecycle.ack_pages(opened["handle"], opened["revision"],
                              [page["page_hash"] for page in pages])
    checked = mcp_server._require_panorama_receipt(
        ack["receipt"], record.project_id, opened["revision"], lifecycle_path)
    assert checked["capture_id"] == record.capture_id
    assert checked["fact_root"] == ack["fact_root"]


def test_unsplittable_section_fails_loudly() -> None:
    view = {"schema_version": "x", "huge": "te" * 20_000}
    with pytest.raises(ValidationError) as excinfo:
        _pack_view_sections(view, payload_budget=6_000, max_page_bytes=6_000)
    details = excinfo.value.details
    assert details["section"] == "huge"
    assert details["section_bytes"] > 4_000
    assert details["page_max_bytes"] == 6_000


def test_oversized_columnar_section_splits_on_rows(tmp_path: Path) -> None:
    _archive_obj, record, lifecycle = _archive(tmp_path, sources=101)
    opened = lifecycle.open_view(record.project_id, max_page_bytes=26_000)
    pages = _fetch_all(lifecycle, opened)
    assert opened["page_count"] > 2
    assert any(page["payload"].get("__parts__") for page in pages)
    merged = _merged(pages)
    assert _hash({key: value for key, value in merged.items()
                  if key != "view_sha256"}) == opened["view_sha256"]


def test_packing_keeps_section_order_and_content() -> None:
    view = {"a": {"n": 1}, "b": [1, 2, 3], "c": "text"}
    packed = _pack_view_sections(view, payload_budget=20_000)
    assert [key for page in packed for key, _ in page] == ["a", "b", "c"]
    assert dict(packed[0]) == view
