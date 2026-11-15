"""Owner-contract regressions: size-independent panorama, lossless pages, deltas.

These tests do not treat one green unit set as completion.  They exercise the
default open path on a project whose full view exceeds one response, the exact
reassembly of an oversized nested scalar, compact acknowledgement, handle
resumption/expiry, semantic deltas across object insertion/move, and paged
delivery of a delta that is too large for one result.
"""

from __future__ import annotations

import copy
import json
import time

import pytest

from reaper_audio import mcp_server
from reaper_audio.context.full_capture import CaptureArchive
from reaper_audio.context.lifecycle import (
    DELTA_INLINE_MAX_BYTES,
    DELTA_PAGE_MAX_BYTES,
    PanoramaLifecycle,
    _delivered_bytes,
    _merge_view_pages,
    _pack_view_sections,
)
from reaper_audio.context.panorama import _hash
from reaper_audio.context.panorama_view import canonical
from reaper_audio.context.semantic_delta import build_semantic_delta
from reaper_audio.errors import StaleContext, ValidationError

from test_panorama_pages import _archive
from test_identity_and_delta import (PROJECT_ID, TRACK_A, _acknowledge,
                                     _archive as _identity_archive,
                                     _capture, _digest, _revision)


def _pages(lifecycle: PanoramaLifecycle, opened: dict) -> list[dict]:
    manifest = opened["manifest"]
    ids = manifest.get("page_ids")
    if ids is None:
        ids = [f"pg_{index}_of_{manifest['page_count']}"
               for index in range(1, manifest["page_count"] + 1)]
    return [opened] + [lifecycle.view_page(opened["handle"], opened["revision"], page_id)
                       for page_id in ids[1:]]


def _page_bytes(page: dict) -> int:
    return _delivered_bytes(page)


def test_auto_open_over_budget_delivers_every_page_under_the_limit(tmp_path) -> None:
    archive, record, lifecycle = _archive(tmp_path, sources=101)
    opened = mcp_server.reaper_panorama_open(
        record.project_id, str(archive.root), max_response_bytes=48_000)

    assert opened["status"] == "page"
    assert opened["mode"] == "pages"
    assert opened["fallback"]["from"] == "single"
    assert opened["fallback"]["reason"] == "single_response_over_budget"
    assert opened["manifest"]["requested_mode"] == "auto"
    page_ids = opened["manifest"]["page_ids"]
    pages = [opened] + [
        mcp_server.reaper_panorama_page(opened["handle"], opened["revision"],
                                        str(archive.root), page_id)
        for page_id in page_ids[1:]
    ]
    assert len(pages) == opened["page_count"] > 1
    assert max(_page_bytes(page) for page in pages) <= opened["manifest"]["max_page_bytes"]
    assert all(page["page_hash"] in opened["manifest"]["page_hashes"] for page in pages)
    merged = dict(_merge_view_pages(pages))
    rebuilt = _hash({key: value for key, value in merged.items() if key != "view_sha256"})
    assert rebuilt == opened["view_sha256"]

    ack = mcp_server.reaper_panorama_ack(
        opened["handle"], opened["revision"], str(archive.root),
        [page["page_hash"] for page in pages])
    assert ack["status"] == "complete"
    assert ack["verified_by"] == "page_hashes"
    assert ack["receipt"].startswith("pr_") and ack["context_token"].startswith("ct_")


def test_explicit_single_over_budget_falls_back_instead_of_refusing(tmp_path) -> None:
    archive, record, lifecycle = _archive(tmp_path, sources=101)
    opened = mcp_server.reaper_panorama_open(
        record.project_id, str(archive.root), mode="single", max_response_bytes=1_000)

    assert opened["status"] == "page"
    assert opened["manifest"]["requested_mode"] == "single"
    assert opened["fallback"]["max_response_bytes"] == 1_000
    assert opened["manifest"]["page_count"] > 1


def test_compact_manifest_hash_acknowledges_a_large_page_set(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("reaper_audio.context.lifecycle.MANIFEST_INLINE_MAX_PAGES", 2)
    _archive_obj, record, lifecycle = _archive(tmp_path, sources=42)
    opened = lifecycle.open_view(record.project_id, max_page_bytes=16_000)
    assert opened["manifest"]["page_count"] > 2
    assert opened["manifest"]["page_ids_inline"] is False
    assert "page_ids" not in opened["manifest"]
    assert opened["manifest"]["manifest_sha256"]

    pages = _pages(lifecycle, opened)
    ack = lifecycle.ack_pages(opened["handle"], opened["revision"], [],
                              page_manifest_sha256=opened["manifest"]["manifest_sha256"])
    assert ack["status"] == "complete"
    assert ack["verified_by"] == "manifest_sha256"
    assert ack["receipt"]
    assert _delivered_bytes(ack) < 8_000

    with pytest.raises(StaleContext):
        lifecycle.ack_pages(opened["handle"], opened["revision"], [],
                            page_manifest_sha256="0" * 64)


def test_partial_page_set_mints_no_receipt_and_no_edit_authority(tmp_path) -> None:
    archive, record, lifecycle = _archive(tmp_path, sources=101)
    opened = lifecycle.open_view(record.project_id, max_page_bytes=16_000)
    second = opened["manifest"]["page_ids"][1]
    lifecycle.view_page(opened["handle"], opened["revision"], second)

    ack = lifecycle.ack_pages(opened["handle"], opened["revision"], [],
                              page_manifest_sha256=opened["manifest"]["manifest_sha256"])
    assert ack["status"] == "incomplete"
    assert ack["complete"] is False
    assert "receipt" not in ack and "context_token" not in ack
    with pytest.raises(ValidationError):
        mcp_server._require_panorama_receipt(None, record.project_id,
                                             opened["revision"], str(archive.root))


def test_repeated_page_is_idempotent_and_stale_revision_is_refused(tmp_path) -> None:
    _archive_obj, record, lifecycle = _archive(tmp_path, sources=42)
    opened = lifecycle.open_view(record.project_id, max_page_bytes=16_000)
    page_id = opened["manifest"]["page_ids"][1]
    first = lifecycle.view_page(opened["handle"], opened["revision"], page_id)
    again = lifecycle.view_page(opened["handle"], opened["revision"], page_id)
    assert first["page_hash"] == again["page_hash"]
    assert again["delivery"]["pages_received"] == first["delivery"]["pages_received"]

    stale = dict(opened["revision"])
    stale["change_count"] = int(stale.get("change_count") or 0) + 1
    with pytest.raises(StaleContext):
        lifecycle.view_page(opened["handle"], stale, page_id)


def test_handle_survives_a_new_lifecycle_instance_and_expires(tmp_path) -> None:
    archive, record, lifecycle = _archive(tmp_path, sources=42)
    opened = lifecycle.open_view(record.project_id, max_page_bytes=16_000)
    restarted = PanoramaLifecycle(archive.root, page_bytes=lifecycle.page_bytes)
    page = restarted.view_page(opened["handle"], opened["revision"],
                               opened["manifest"]["page_ids"][1])
    assert page["page_hash"] == opened["manifest"]["page_hashes"][1]

    handle_path = restarted.handles_dir / f"{opened['handle']}.json"
    state = json.loads(handle_path.read_text(encoding="utf-8"))
    state["created_at"] = time.time() - 2 * 24 * 60 * 60
    handle_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(StaleContext):
        restarted.view_page(opened["handle"], opened["revision"],
                            opened["manifest"]["page_ids"][1])


def test_oversized_nested_scalar_is_lossless_in_pages() -> None:
    view = {"schema_version": "x",
            "huge": "данные" * 30_000,
            "nested": {"rows": [{"i": index, "payload": "x" * 2_000}
                                for index in range(20)]}}
    packed = _pack_view_sections(view, payload_budget=8_000, max_page_bytes=12_000)
    pages = [{"payload": dict(page)} for page in packed]
    assert len(pages) > 3
    assert max(_page_bytes(page) for page in pages) <= 12_000
    merged = dict(_merge_view_pages(pages))
    assert merged == view
    assert canonical(merged) == canonical(view)


# ---------------------------------------------------------------------------
# semantic deltas
# ---------------------------------------------------------------------------


def _base_document() -> dict:
    return {
        "schema_version": "s/2",
        "analysis_metric_units": {"peak_dbfs": "dBFS", "integrated_lufs": "LUFS"},
        "analysis_contract": {"method": {"analysis": "reaper-audio-library",
                                         "version": "1.0"}},
        "tracks": [
            {"stable_id": "track:A", "guid": "{A}", "index": 0,
             "mixer": {"volume": 0.5, "pan": 0.0}},
            {"stable_id": "track:B", "guid": "{B}", "index": 1,
             "mixer": {"volume": 0.6, "pan": 0.0}},
        ],
        "items": [{"item_id": "item:1", "guid": "{I1}", "track_id": "track:A",
                   "position_s": 1.0}],
        "fx": [{"fx_id": "fx:A:0", "owner_id": "track:A", "slot_index": 0,
                "name": "EQ", "enabled": True}],
        "analysis": [
            {"source_id": "track:A", "analysis_id": "analysis:A", "stage": "file_or_buffer",
             "channel_view": "2_channels", "measured_window": {"start_s": 0.0, "end_s": 10.0},
             "metrics": {"peak_dbfs": -6.0, "integrated_lufs": -14.0}},
            {"source_id": "track:B", "analysis_id": "analysis:B", "stage": "file_or_buffer",
             "channel_view": "2_channels", "measured_window": {"start_s": 0.0, "end_s": 10.0},
             "metrics": {"peak_dbfs": -7.0, "integrated_lufs": -15.0}},
        ],
        "audio": [{"audio_id": "audio:A", "source_id": "track:A", "sha256": "aaa",
                   "render_provenance": {"stage": "post_fx"}}],
        "relations": {"relations": [{"kind": "band_dominance", "from_id": "track:A",
                                     "to_id": "track:B",
                                     "metrics": {"band_delta_db": 3.0},
                                     "units": {"band_delta_db": "dB"}}]},
        "details": {"entries": [{"ref": "series:A", "kind": "analysis_series",
                                 "count": 10, "content_sha256": "h1"}]},
        "project": {"name": "x", "body": 1},
    }


def test_nested_provenance_hashes_and_dependency_links_stay_addressable() -> None:
    base = _base_document()
    base["tracks"][0]["asset_sha256"] = "a" * 64
    base["tracks"][0]["dep_set"] = ["dep:1"]
    base["tracks"][0]["raw_parameters_pointer"] = "#/fx/fx:A:0/params"
    base["tracks"][0]["provenance"] = {"origin": "take:1", "revision_key": "r1"}
    target = copy.deepcopy(base)
    target["tracks"][0]["asset_sha256"] = "b" * 64
    target["tracks"][0]["dep_set"] = ["dep:2"]
    target["tracks"][0]["raw_parameters_pointer"] = "#/fx/fx:A:0/params/v2"
    target["tracks"][0]["provenance"] = {"origin": "take:2", "revision_key": "r2"}

    delta = build_semantic_delta(base, target)
    paths = {op["path"]: op for op in delta["ops"]}
    for path in ("/tracks/track:A/asset_sha256", "/tracks/track:A/dep_set/0",
                 "/tracks/track:A/raw_parameters_pointer",
                 "/tracks/track:A/provenance/origin",
                 "/tracks/track:A/provenance/revision_key"):
        assert path in paths, path
    assert delta["journal"]["kind"] == "capture_delta"
    assert delta["journal"]["entries"]
    assert delta["provenance"]["base"]["capture_binding"] is not None
    assert delta["provenance"]["target"]["capture_binding"] is not None


def test_track_insert_and_move_do_not_shift_change_identity() -> None:
    base = _base_document()
    target = copy.deepcopy(base)
    target["tracks"].insert(0, {"stable_id": "track:Z", "guid": "{Z}", "index": 0,
                                "mixer": {"volume": 0.9, "pan": 0.2}})
    target["tracks"][1]["index"] = 1
    target["tracks"][2]["index"] = 2
    target["fx"].insert(0, {"fx_id": "fx:A:new", "owner_id": "track:A",
                            "slot_index": 0, "name": "Comp", "enabled": True})
    target["fx"][1]["slot_index"] = 1

    delta = build_semantic_delta(base, target)
    paths = {op["path"]: op for op in delta["ops"]}
    assert any(path == "/tracks/track:Z" for path in paths)
    assert paths["/tracks/track:A/index"]["object_id"] == "track:A"
    assert paths["/tracks/track:A/index"]["before"] == 0
    assert paths["/tracks/track:A/index"]["after"] == 1
    assert paths["/tracks/track:B/index"]["object_id"] == "track:B"
    assert any(path == "/fx/fx:A:new" for path in paths)
    assert paths["/fx/fx:A:0/slot_index"]["object_id"] == "fx:A:0"
    # No positional rename exists: the inserted object has its own id and the
    # existing objects keep theirs.
    assert not any("/tracks/0" in path for path in paths)


def test_track_delete_keeps_remaining_stable_ids() -> None:
    base = _base_document()
    target = copy.deepcopy(base)
    target["tracks"] = [row for row in target["tracks"]
                        if row["stable_id"] != "track:B"]
    target["tracks"][0]["index"] = 0
    target["analysis"] = [row for row in target["analysis"]
                          if row["source_id"] != "track:B"]
    target["relations"]["relations"] = []

    delta = build_semantic_delta(base, target)
    paths = {op["path"] for op in delta["ops"]}
    assert "/tracks/track:B" in paths
    assert not any(path.startswith("/tracks/1") for path in paths)
    assert delta["object_summary"]["track"]["removed"] == ["track:B"]
    assert "track:A" not in delta["object_summary"]["track"]["removed"]


def test_unit_mismatch_and_method_mismatch_are_unknown_not_lies() -> None:
    base = _base_document()
    target = copy.deepcopy(base)
    target["analysis"][1]["metrics"]["peak_dbfs"] = -3.0
    target["analysis_metric_units"]["peak_dbfs"] = "dBFS_alt"
    target["analysis_contract"]["method"]["version"] = "2.0"

    delta = build_semantic_delta(base, target)
    op = next(op for op in delta["ops"]
              if op["path"] == "/analysis/track:B/metrics/peak_dbfs")
    assert op["comparability"]["state"] == "incomparable"
    assert op["comparability"]["reason"] == "unit_mismatch"
    assert op["before"] == -7.0 and op["after"] == -3.0
    assert delta["comparability"]["measurements"]["state"] == "incomparable"
    assert "units" in delta["comparability"]["measurements"]["mismatched_criteria"]

    only_method = copy.deepcopy(base)
    only_method["analysis"][1]["metrics"]["integrated_lufs"] = -13.0
    only_method["analysis_contract"]["method"]["version"] = "2.0"
    method_delta = build_semantic_delta(base, only_method)
    method_op = next(op for op in method_delta["ops"]
                     if op["path"] == "/analysis/track:B/metrics/integrated_lufs")
    assert method_op["comparability"]["state"] == "unknown"


def test_large_mutation_delta_is_paged_and_advances_exactly_once(tmp_path) -> None:
    root = tmp_path / "archive"
    root.mkdir()
    (root / "index.jsonl").write_text("", encoding="utf-8")
    _capture(root, "cap_00000001", _revision(1), _digest(change_count=1))
    lifecycle = PanoramaLifecycle(root, page_bytes=4096)
    token = _acknowledge(lifecycle)["context_token"]
    facts = [
        {"operation_index": index, "kind": "track.set", "target_guid": TRACK_A,
         "before": {"blob": "x" * 4_000, "index": index},
         "after": {"blob": "y" * 4_000, "index": index + 1},
         "changes_state": True}
        for index in range(30)
    ]
    delta = lifecycle.record_change(
        kind="bulk", context_token=token, project_id=PROJECT_ID,
        base_revision=_revision(1), target_revision=_revision(2),
        after_digest=_digest(change_count=2, pan_a=1.0), scope="known",
        operation_facts=facts, readback_complete=True)

    assert delta["status"] == "delta_page"
    assert delta["page_count"] > 1
    assert "advance" not in delta

    pages = _pages(lifecycle, delta)
    assert max(_page_bytes(page) for page in pages) <= DELTA_PAGE_MAX_BYTES
    merged = dict(_merge_view_pages(pages))
    assert len(merged["operation_facts"]) == 30
    assert merged["operation_facts"][0]["after"]["index"] == 1
    with pytest.raises(StaleContext):
        lifecycle.ack_pages(delta["handle"], delta["revision"],
                            list(reversed([page["page_hash"] for page in pages])))
    stale_revision = dict(delta["revision"])
    stale_revision["change_count"] = int(stale_revision.get("change_count") or 0) + 1
    with pytest.raises(StaleContext):
        lifecycle.ack_pages(delta["handle"], stale_revision,
                            [page["page_hash"] for page in pages])
    ack = lifecycle.ack_pages(delta["handle"], delta["revision"],
                              [page["page_hash"] for page in pages])
    assert ack["status"] == "complete"
    assert ack["advance"]["advanced"] is True
    successor = ack["context_token"]
    assert successor and successor != token
    again = lifecycle.ack_pages(delta["handle"], delta["revision"], [],
                                page_manifest_sha256=delta["manifest"]["manifest_sha256"])
    assert again["status"] == "complete"
    assert again["idempotent"] is True
    assert again["context_token"] == successor

    # A missing page never advances.
    state = json.loads((lifecycle.handles_dir / f"{delta['handle']}.json")
                       .read_text(encoding="utf-8"))
    state["complete"] = False
    state["received"] = [0]
    (lifecycle.handles_dir / f"{delta['handle']}.json").write_text(
        json.dumps(state), encoding="utf-8")
    partial = lifecycle.ack_pages(delta["handle"], delta["revision"], [],
                                  page_manifest_sha256=delta["manifest"]["manifest_sha256"])
    assert partial["status"] == "incomplete"
    assert "context_token" not in partial

    restarted = PanoramaLifecycle(root, page_bytes=lifecycle.page_bytes)
    resumed = restarted.view_page(delta["handle"], delta["revision"],
                                  delta["manifest"]["page_ids"][1])
    assert resumed["page_hash"] == delta["manifest"]["page_hashes"][1]
    handle_path = restarted.handles_dir / f"{delta['handle']}.json"
    handle_state = json.loads(handle_path.read_text(encoding="utf-8"))
    handle_state["created_at"] = time.time() - 2 * 24 * 60 * 60
    handle_path.write_text(json.dumps(handle_state), encoding="utf-8")
    with pytest.raises(StaleContext):
        restarted.view_page(delta["handle"], delta["revision"],
                            delta["manifest"]["page_ids"][1])


def test_large_exact_query_is_delivered_as_complete_result_pages(tmp_path) -> None:
    archive, record, lifecycle = _archive(tmp_path, sources=101)
    opened = mcp_server.reaper_panorama_open(
        record.project_id, str(archive.root), mode="pages")
    pages = _pages(lifecycle, opened)
    ack = lifecycle.ack_pages(opened["handle"], opened["revision"],
                              [page["page_hash"] for page in pages])
    assert ack["receipt"]

    result = mcp_server.reaper_panorama_query(
        revision=opened["revision"], archive_root=str(archive.root),
        handle=opened["handle"], panorama_receipt=ack["receipt"],
        data=["analysis"], objects="all", max_response_bytes=8_000)
    assert result["status"] == "result_page"
    assert result["result_kind"] == "query"
    ids = result["manifest"].get("page_ids") or [
        f"rp_{index}_of_{result['manifest']['page_count']}"
        for index in range(1, result["manifest"]["page_count"] + 1)]
    result_pages = [result] + [
        mcp_server.reaper_panorama_page(result["handle"], result["revision"],
                                        str(archive.root), page_id)
        for page_id in ids[1:]]
    from reaper_audio.context.lifecycle import _delivered_bytes as _bytes
    assert max(_bytes(page) for page in result_pages) <= 8_000
    merged = dict(_merge_view_pages(result_pages))
    assert len(merged["data"]["analysis"]) == 101
    result_ack = mcp_server.reaper_panorama_ack(
        result["handle"], result["revision"], str(archive.root),
        [page["page_hash"] for page in result_pages])
    assert result_ack["status"] == "complete"
    assert result_ack["context_token"] is None
    assert result_ack["mutation_unlocked"] is False


def test_context_chain_advances_from_each_acknowledged_state(
        tmp_path) -> None:
    root = _identity_archive(tmp_path)
    _capture(root, "cap_00000001", _revision(1), _digest(change_count=1))
    lifecycle = PanoramaLifecycle(root, page_bytes=4096)
    first = _acknowledge(lifecycle, capture_id="cap_00000001")
    token_a = first["context_token"]

    delta_b = lifecycle.record_change(
        kind="edit", context_token=token_a, project_id=PROJECT_ID,
        base_revision=_revision(1), target_revision=_revision(2),
        after_digest=_digest(change_count=2, pan_a=-0.5), scope="known",
        operation_facts=[{"operation_index": 0, "kind": "track.set",
                          "target_guid": TRACK_A, "before": {"pan": 0.0},
                          "after": {"pan": -0.5}, "changes_state": True}],
        readback_complete=True)
    token_b = (delta_b.get("advance") or {}).get("context_token") or delta_b["context_token"]
    assert token_b != token_a
    assert delta_b["journal"]["kind"] == "structural_mutation"
    assert delta_b["journal"]["base"]["context_token"] == token_a

    delta_c = lifecycle.record_change(
        kind="edit", context_token=token_b, project_id=PROJECT_ID,
        base_revision=_revision(2), target_revision=_revision(3),
        after_digest=_digest(change_count=3, pan_a=-1.0), scope="known",
        operation_facts=[{"operation_index": 0, "kind": "track.set",
                          "target_guid": TRACK_A, "before": {"pan": -0.5},
                          "after": {"pan": -1.0}, "changes_state": True}],
        readback_complete=True)
    token_c = (delta_c.get("advance") or {}).get("context_token") or delta_c["context_token"]
    assert token_c not in {token_a, token_b}
    assert delta_c["journal"]["base"]["context_token"] == token_b
    # Both deltas are anchored to the state actually acknowledged by the model,
    # not to the first capture.
    assert delta_b["base"]["revision"]["change_count"] == 1
    assert delta_c["base"]["revision"]["change_count"] == 2


def test_capture_delta_has_no_chain_horizon(tmp_path) -> None:
    root = _identity_archive(tmp_path)
    for index in range(1, 25):
        _capture(root, f"cap_{index:08d}", _revision(index),
                 _digest(change_count=index))
    lifecycle = PanoramaLifecycle(root)
    result = lifecycle.changes_since(
        PROJECT_ID, base_capture_id="cap_00000001",
        base_revision=_revision(1), base_fact_root=None)
    assert result.get("reason") != "chain_too_long"
    assert result["status"] in {"delta", "delta_page"}
    assert result.get("from_capture") == "cap_00000001"


def test_capture_to_capture_delta_is_paged_and_advances_after_ack(tmp_path) -> None:
    archive, record, lifecycle = _archive(tmp_path, sources=42)
    base_id = record.capture_id
    base_document = lifecycle._document(base_id)
    target_id = "cap_panorama2"
    target = copy.deepcopy(base_document)
    target["capture_binding"]["capture_id"] = target_id
    revision = copy.deepcopy(target["capture_binding"]["revision_after"])
    revision["change_count"] = int(revision.get("change_count") or 0) + 1
    target["capture_binding"]["revision_before"] = revision
    target["capture_binding"]["revision_after"] = revision
    for row in target["analysis"]:
        row["metrics"] = {name: (value + 1.0 if isinstance(value, (int, float)) else value)
                          for name, value in (row.get("metrics") or {}).items()}
    payload = {key: value for key, value in target.items() if key != "document_sha256"}
    target["document_sha256"] = _hash(payload)
    capture_dir = archive.captures / target_id
    capture_dir.mkdir(parents=True)
    (capture_dir / "panorama.json").write_text(
        canonical(target).decode("utf-8"), encoding="utf-8")
    (capture_dir / "record.json").write_text(
        json.dumps({"schema_version": "reaper-workbench.full-capture/1"}),
        encoding="utf-8")
    with (archive.root / "index.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "capture_id": target_id, "project_id": record.project_id,
            "revision_before": revision, "revision_after": revision,
            "project_path": record.project_path,
        }) + "\n")

    opened = lifecycle.open_view(record.project_id, capture_id=base_id)
    pages = _pages(lifecycle, opened)
    base_ack = lifecycle.ack_pages(opened["handle"], opened["revision"],
                                   [page["page_hash"] for page in pages])
    token = base_ack["context_token"]

    delta = lifecycle.changes_since(record.project_id, base_capture_id=base_id,
                                    context_token=token)
    assert delta["status"] == "delta_page"
    assert delta["page_count"] > 1
    assert "advance" not in delta

    # No page set, no successor.
    partial = lifecycle.ack_pages(delta["handle"], delta["revision"], [],
                                  page_manifest_sha256=delta["manifest"]["manifest_sha256"])
    assert partial["status"] == "incomplete"
    assert "context_token" not in partial

    delta_pages = _pages(lifecycle, delta)
    assert max(_page_bytes(page) for page in delta_pages) <= DELTA_PAGE_MAX_BYTES
    merged_delta = dict(_merge_view_pages(delta_pages))
    assert merged_delta["metric_table"]["count"] > 32

    ack = lifecycle.ack_pages(delta["handle"], delta["revision"],
                              [page["page_hash"] for page in delta_pages])
    assert ack["status"] == "complete"
    assert ack["advance"]["advanced"] is True
    successor = ack["context_token"]
    assert successor and successor != token
    checked = lifecycle.require_receipt(successor, record.project_id,
                                        delta["revision"])
    assert checked["complete"] is True
    with pytest.raises(StaleContext):
        lifecycle.require_receipt(token, record.project_id, delta["revision"])
