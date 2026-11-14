from __future__ import annotations

import json
from pathlib import Path

import pytest

from reaper_audio.context.lifecycle import PanoramaLifecycle, fact_root
from reaper_audio.context.full_capture import CaptureArchive
from reaper_audio.context.panorama import PANORAMA_SCHEMA_VERSION
from reaper_audio.errors import StaleContext, ValidationError


def _revision(project_id: str, change_count: int) -> dict:
    return {"project_id": project_id, "change_count": change_count,
            "path": "C:/music/test.RPP", "instance_id": "pri_test", "epoch": 1,
            "epoch_basis": "bridge_session_project_generation"}


def _capture(root: Path, capture_id: str, project_id: str, change_count: int,
             body: int = 1) -> Path:
    revision = _revision(project_id, change_count)
    capture_dir = root / "captures" / capture_id
    capture_dir.mkdir(parents=True)
    binding = {
        "capture_id": capture_id,
        "project_id": project_id,
        "record_integrity_sha256": "integrity-" + capture_id,
        "revision_before": revision,
        "revision_after": revision,
        "time_window": {"project_start_s": 0.0, "project_end_s": 120.0},
    }
    document = {
        "schema_version": PANORAMA_SCHEMA_VERSION,
        "capture_binding": binding,
        "project": {"name": "test", "body": body},
        "tracks": [{"guid": "{TRACK-1}", "name": "Guitar", "volume": float(body)}],
        "items": [
            {"guid": "{ITEM-1}", "interval": {"start_s": 10.0, "end_s": 40.0}},
            {"guid": "{ITEM-2}", "interval": {"start_s": 80.0, "end_s": 100.0}},
        ],
        "analysis_temporal": {
            "representation": "cross_source_activity_grid",
            "units": "dBFS",
            "aggregation": "mean_per_bucket",
            "bucket_width_s": 30.0,
            "time_basis": {"start_s": "project_seconds",
                           "interval": "[start_s,end_s)"},
            "source_count": 1,
            "sources": [{"source_id": "{TRACK-1}",
                         "analysis_id": "analysis:{TRACK-1}",
                         "series_ref": None}],
            "buckets": [
                {"start_s": 0.0, "end_s": 30.0,
                 "rms_dbfs_by_source": {"{TRACK-1}": 0.1}},
                {"start_s": 30.0, "end_s": 60.0,
                 "rms_dbfs_by_source": {"{TRACK-1}": 0.2}},
            ],
            "coverage": {"status": "complete"},
        },
        "coverage": {"complete": True},
        "audio": [{"source_id": "{TRACK-1}", "is_master": False}],
        "truncation": {"truncated": False},
    }
    (capture_dir / "panorama.json").write_text(json.dumps(document), encoding="utf-8")
    (capture_dir / "record.json").write_text("{}", encoding="utf-8")
    row = {"capture_id": capture_id, "project_id": project_id,
           "revision_before": revision, "revision_after": revision}
    with (root / "index.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    return capture_dir / "panorama.json"


def _archive(tmp_path: Path) -> Path:
    root = tmp_path / "archive"
    root.mkdir(parents=True)
    (root / "index.jsonl").write_text("", encoding="utf-8")
    return root


def _ack(lifecycle: PanoramaLifecycle, opened: dict) -> dict:
    hashes = []
    for index in range(opened["page_count"]):
        page = lifecycle.page(opened["handle"], opened["revision"], index)
        hashes.append(page["page_hash"])
    return lifecycle.ack(opened["handle"], opened["revision"], hashes)


def test_page_gate_requires_every_page_and_mints_receipt(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", "project-1", 1)
    lifecycle = PanoramaLifecycle(root, page_bytes=180)
    opened = lifecycle.open("project-1")
    assert opened["page_count"] > 1
    assert opened["delivery"]["complete"] is False
    assert opened["manifest"]["page_hashes"][0] == opened["page_hash"]

    # A client can claim every hash without having fetched the pages: no receipt.
    incomplete = lifecycle.ack(opened["handle"], opened["revision"],
                               opened["manifest"]["page_hashes"])
    assert incomplete["complete"] is False
    assert incomplete["missing_pages"] == list(range(1, opened["page_count"]))

    hashes = [opened["page_hash"]]
    for index in range(1, opened["page_count"]):
        page = lifecycle.page(opened["handle"], opened["revision"], index)
        hashes.append(page["page_hash"])
    complete = lifecycle.ack(opened["handle"], opened["revision"], hashes)
    assert complete["complete"] is True
    assert complete["receipt"].startswith("pr_")
    assert complete["fact_root"] == opened["fact_root"]
    repeated = lifecycle.ack(opened["handle"], opened["revision"], hashes)
    assert repeated["receipt"] == complete["receipt"]
    assert repeated.get("idempotent") is True
    assert lifecycle.require_receipt(complete["receipt"], "project-1",
                                      opened["revision"])["complete"] is True

    again = lifecycle.page(opened["handle"], opened["revision"], 1)
    assert again["page_hash"] == hashes[1]


def test_wrong_revision_page_and_ack_fail_closed(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", "project-1", 1)
    lifecycle = PanoramaLifecycle(root, page_bytes=180)
    opened = lifecycle.open("project-1")
    wrong = dict(opened["revision"])
    wrong["change_count"] = 999
    with pytest.raises(StaleContext):
        lifecycle.page(opened["handle"], wrong, 1)
    with pytest.raises(StaleContext):
        lifecycle.ack(opened["handle"], wrong, opened["manifest"]["page_hashes"])
    with pytest.raises(StaleContext):
        lifecycle.require_receipt("pr_missing", "project-1", opened["revision"])
    with pytest.raises(ValidationError):
        lifecycle.require_receipt(None, "project-1", opened["revision"])  # type: ignore[arg-type]


def test_open_rejects_a_stale_expected_revision(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", "project-1", 1)
    lifecycle = PanoramaLifecycle(root, page_bytes=180)
    stale = _revision("project-1", 0)
    with pytest.raises(StaleContext):
        lifecycle.open("project-1", expected_revision=stale)


def test_changes_since_is_revision_bound_and_reports_ops(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", "project-1", 1, body=1)
    _capture(root, "cap_00000002", "project-1", 2, body=2)
    lifecycle = PanoramaLifecycle(root, page_bytes=180)
    base_doc = lifecycle._document("cap_00000001")
    base_root = fact_root(base_doc)

    same = lifecycle.changes_since("project-1", base_capture_id="cap_00000002")
    assert same["status"] == "not_modified"

    stale = lifecycle.changes_since("project-1", base_capture_id="cap_00000001",
                                    base_fact_root="not-the-root")
    assert stale["status"] == "stale_base"
    assert stale["reason"] == "root_mismatch"

    delta = lifecycle.changes_since("project-1", base_capture_id="cap_00000001",
                                    base_fact_root=base_root)
    assert delta["status"] == "delta"
    assert delta["from_root"] == base_root
    assert delta["to_root"] != base_root
    assert any(op["path"] == "/project/body" for op in delta["ops"])
    assert delta["to_revision"]["change_count"] == 2
    assert delta["invalidations"]
    assert delta["invalidations"][0]["requires_recapture"] is True


def test_changes_since_rejects_a_base_from_another_archive(tmp_path: Path) -> None:
    first = _archive(tmp_path / "one")
    second = _archive(tmp_path / "two")
    _capture(first, "cap_00000001", "project-1", 1)
    _capture(second, "cap_00000002", "project-1", 2)
    lifecycle = PanoramaLifecycle(second, page_bytes=180)
    result = lifecycle.changes_since("project-1", base_capture_id="cap_00000001")
    assert result["status"] == "stale_base"
    assert result["reason"] == "base_not_in_archive"
    assert result["next_call"]["tool"] == "reaper_panorama_refresh"


def test_delta_ack_advances_context_without_replaying_pages(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", "project-1", 1, body=1)
    lifecycle = PanoramaLifecycle(root, page_bytes=180)
    opened = lifecycle.open("project-1")
    hashes = [opened["page_hash"]]
    for index in range(1, opened["page_count"]):
        hashes.append(lifecycle.page(opened["handle"], opened["revision"], index)["page_hash"])
    full = lifecycle.ack(opened["handle"], opened["revision"], hashes)
    assert full["context_token"].startswith("ct_")
    assert lifecycle.require_receipt(full["context_token"], "project-1",
                                      opened["revision"])["complete"] is True

    _capture(root, "cap_00000002", "project-1", 2, body=2)
    delta = lifecycle.changes_since("project-1", context_token=full["context_token"])
    assert delta["status"] == "delta"
    assert delta["continuity"] is True
    assert delta["delta_id"].startswith("dl_")
    successor = lifecycle.delta_ack(delta["delta_id"], delta["delta_hash"],
                                    full["context_token"])
    assert successor["complete"] is True
    assert successor["context_token"] != full["context_token"]
    assert successor["kind"] == "delta"

    target_revision = delta["to_revision"]
    assert lifecycle.require_receipt(successor["context_token"], "project-1",
                                      target_revision)["complete"] is True
    with pytest.raises(StaleContext):
        lifecycle.require_receipt(full["context_token"], "project-1", target_revision)

    repeated = lifecycle.delta_ack(delta["delta_id"], delta["delta_hash"],
                                   full["context_token"])
    assert repeated["idempotent"] is True
    assert repeated["context_token"] == successor["context_token"]
    with pytest.raises(StaleContext):
        lifecycle.delta_ack(delta["delta_id"], "not-the-delta-hash",
                            full["context_token"])
    with pytest.raises(StaleContext):
        lifecycle.delta_ack(delta["delta_id"], delta["delta_hash"],
                            "ct_unknown_or_expired")

    _capture(root, "cap_00000003", "project-1", 3, body=3)
    second_delta = lifecycle.changes_since("project-1",
                                            context_token=successor["context_token"])
    assert second_delta["status"] == "delta"
    second_successor = lifecycle.delta_ack(second_delta["delta_id"],
                                           second_delta["delta_hash"],
                                           successor["context_token"])
    assert lifecycle.require_receipt(second_successor["context_token"], "project-1",
                                      second_delta["to_revision"])["complete"] is True


def test_receipt_binds_capture_fact_root_and_project(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", "project-1", 1)
    lifecycle = PanoramaLifecycle(root, page_bytes=180)
    opened = lifecycle.open("project-1")
    hashes = [opened["page_hash"]]
    for index in range(1, opened["page_count"]):
        hashes.append(lifecycle.page(opened["handle"], opened["revision"], index)["page_hash"])
    complete = lifecycle.ack(opened["handle"], opened["revision"], hashes)
    receipt = complete["receipt"]

    assert lifecycle.require_receipt(receipt, "project-1", opened["revision"])[
        "complete"] is True
    with pytest.raises(StaleContext):
        lifecycle.require_receipt(receipt, "other-project", opened["revision"])
    with pytest.raises(StaleContext):
        lifecycle.require_receipt(receipt, "project-1", opened["revision"],
                                  expected_fact_root="other-root")


def test_relations_participate_in_pages_ack_delta_and_slice(tmp_path: Path) -> None:
    root = _archive(tmp_path)

    def _with_relations(capture_id: str, project_id: str, change_count: int,
                        delta_db: float) -> None:
        path = _capture(root, capture_id, project_id, change_count)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["relations"] = {
            "schema_version": 1,
            "binding": {"capture_id": capture_id, "stage": "post_fx",
                        "revision_fingerprint": "revision"},
            "complete": True,
            "relations": [{
                "relation_id": f"rel_{capture_id}",
                "kind": "band_dominance",
                "from_id": "{TRACK-1}",
                "to_id": "{TRACK-2}",
                "metrics": {"band_delta_db": delta_db},
                "units": {"band_delta_db": "dB"},
                "attributes": {"temporal_evidence": {
                    "from_series_ref": f"#/analysis/{capture_id}/curves/x",
                    "to_series_ref": f"#/analysis/{capture_id}/curves/y",
                }},
                "coverage": {"status": "complete", "reasons": []},
                "provenance": {"asset_ids": [capture_id]},
                "dep_set": {"asset_ids": [capture_id]},
            }],
            "evaluations": {"band_dominance_pairs": {
                "columns": ["from_id", "to_id", "status", "relation_id", "dep_set"],
                "rows": [["{TRACK-1}", "{TRACK-2}", "no_dominant_band",
                          f"rel_{capture_id}", {"asset_ids": [capture_id]}]],
                "capture_id": capture_id,
                "revision_key": "revision",
            }},
            "coverage": {"band_dominance": {
                "expected": 1, "evaluated": 1, "status": "complete", "reasons": [],
            }},
            "unavailable": {},
            "truncated": {},
            "content_hash": f"hash-{capture_id}",
        }
        document["relations_summary"] = {
            "schema_version": 1,
            "complete": True,
            "coverage": document["relations"]["coverage"],
            "unavailable": {},
            "content_hash": f"hash-{capture_id}",
            "counts": {"relations": 1, "pairs": 1},
        }
        path.write_text(json.dumps(document), encoding="utf-8")

    _with_relations("cap_00000001", "project-1", 1, 3.0)
    _with_relations("cap_00000002", "project-1", 2, 7.0)
    lifecycle = PanoramaLifecycle(root, page_bytes=180)
    opened = lifecycle.open("project-1")
    hashes = [opened["page_hash"]]
    payloads = [opened["payload"]]
    for index in range(1, opened["page_count"]):
        page = lifecycle.page(opened["handle"], opened["revision"], index)
        hashes.append(page["page_hash"])
        payloads.append(page["payload"])
    assert any("relations" in payload for payload in payloads)
    assert lifecycle.ack(opened["handle"], opened["revision"], hashes)["complete"] is True

    delta = lifecycle.changes_since("project-1", base_capture_id="cap_00000001")
    assert delta["status"] == "delta"
    assert any("band_delta_db" in op["path"] for op in delta["ops"])

    sliced = lifecycle.slice(opened["handle"], opened["revision"],
                             window_start_s=0.0, window_end_s=30.0)
    assert sliced["state"]["relations_summary"]["counts"]["pairs"] == 1


def test_slice_recomputes_interval_relations_not_whole_capture(tmp_path: Path) -> None:
    from test_capture_panorama import _record, TRACK_A, TRACK_B, MASTER
    from reaper_audio.context.full_capture import FullCaptureRecord

    source = tmp_path / "source"
    source.mkdir()
    record, files = _record(source)
    data = record.to_mapping(deep=True)
    data["time_window"] = {"project_start_s": 0.0, "project_end_s": 60.0,
                           "file_start_s": 0.0, "file_end_s": 3.0}
    for asset in data["audio"]:
        asset["signal_window"]["project_start_s"] = 0.0
        asset["signal_window"]["project_end_s"] = 60.0
    data["integrity_sha256"] = None
    times = [0.0, 10.0, 20.0, 30.0, 40.0, 50.0]
    for guid in (TRACK_A, TRACK_B):
        curves = data["analysis"][guid]["curves"]
        curves["spectrogram_time_s"] = list(times)
        curves["band_centers_hz"] = [100.0, 1000.0]
        curves["band_level_time_dbfs"] = (
            [[20.0, 0.0] for _ in times] if guid == TRACK_A else
            [[20.0, 0.0], [20.0, 0.0], [20.0, 0.0], [20.0, 0.0],
             [0.0, 0.0], [20.0, 0.0]]
        )
    data["analysis"][MASTER]["curves"]["spectrogram_time_s"] = list(times)
    data["analysis"][MASTER]["curves"]["band_centers_hz"] = [100.0, 1000.0]
    data["analysis"][MASTER]["curves"]["band_level_time_dbfs"] = (
        [[18.0, 0.0] for _ in times]
    )
    captured = FullCaptureRecord.from_dict(data)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(captured, files)
    lifecycle = PanoramaLifecycle(archive.root)
    opened = lifecycle.open("reaper:test")
    _ack(lifecycle, opened)
    result = lifecycle.slice(opened["handle"], opened["revision"],
                               window_start_s=36.0, window_end_s=45.0)

    whole = opened.get("manifest", {}).get("coverage")
    assert whole is not None
    relations = result["evidence"]["relations"]
    assert relations["scope"] == "interval"
    assert relations["interval"] == {"start_s": 36.0, "end_s": 45.0}
    assert relations["relations"]
    band = next(relation for relation in relations["relations"]
                if relation["kind"] == "band_dominance")
    assert band["relation_id"].startswith("rel_")
    assert band["metrics"]["band_delta_db"] == 10.0
    assert band["window"]["start_s"] == 36.0
    assert band["window"]["end_s"] == 45.0
    assert relations["unavailable"]["track_master_level_relation"].startswith(
        "not_recomputed_for_interval")
    assert result["state"]["relations_summary"]["scope"] == "project"
    assert result["state"]["relations_summary"]["counts"]["relations"] == 0

    with pytest.raises(StaleContext):
        lifecycle.slice(opened["handle"],
                        {**opened["revision"], "capture_id": "cap_other"},
                        window_start_s=36.0, window_end_s=45.0)
    with pytest.raises(StaleContext):
        lifecycle.slice(opened["handle"],
                        {**opened["revision"], "fact_root": "other-root"},
                        window_start_s=36.0, window_end_s=45.0)


def test_slice_keeps_project_scope_and_marks_what_is_not_repeated(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", "project-1", 1)
    lifecycle = PanoramaLifecycle(root, page_bytes=180)
    opened = lifecycle.open("project-1")
    _ack(lifecycle, opened)
    slice_result = lifecycle.slice(opened["handle"], opened["revision"],
                                   window_start_s=0.0, window_end_s=30.0)
    assert slice_result["state"]["scope"] == "project"
    assert slice_result["evidence"]["scope"] == {"kind": "interval", "start_s": 0.0,
                                                 "end_s": 30.0}
    assert [item["guid"] for item in slice_result["evidence"]["items"]] == ["{ITEM-1}"]
    assert slice_result["coverage"]["not_in_scope"]


def test_slice_requires_a_complete_receipt(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", "project-1", 1)
    lifecycle = PanoramaLifecycle(root)
    opened = lifecycle.open("project-1")
    with pytest.raises(StaleContext) as excinfo:
        lifecycle.slice(opened["handle"], opened["revision"],
                        window_start_s=0.0, window_end_s=30.0)
    assert "reaper_panorama_ack" in excinfo.value.details["next_action"]


def test_refresh_opens_a_new_handle_at_the_current_capture(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", "project-1", 1)
    lifecycle = PanoramaLifecycle(root, page_bytes=180)
    first = lifecycle.open("project-1")
    second = lifecycle.refresh("project-1")
    assert second["status"] == "complete"
    assert second["handle"] != first["handle"]
    assert second["document_hash"] == first["document_hash"]
    assert second["view"]["reading"]["delivery"].startswith("paged MCP delivery")
    status = lifecycle.status("project-1", second["handle"])
    assert status["latest"]["capture_id"] == "cap_00000001"
    assert status["handle"]["delivered"] is True
    assert status["handle"]["receipt"].startswith("pr_")
    assert status["handle"]["context_token"].startswith("ct_")
    assert status["handle"]["view_sha256"]
    assert status["handle"]["delivery"]["view_sha256"] == status["handle"]["view_sha256"]
    assert "page_count" not in status["handle"]
    assert "pages_missing" not in status["handle"]
    assert "mode" not in status["handle"]


def test_mcp_mutation_gate_rejects_missing_or_partial_receipt(tmp_path: Path) -> None:
    from reaper_audio import mcp_server

    root = _archive(tmp_path)
    _capture(root, "cap_00000001", "project-1", 1)
    mcp_server._panorama_lifecycle(str(root))
    revision = _revision("project-1", 1)
    with pytest.raises(ValidationError, match="complete panorama"):
        mcp_server._require_panorama_receipt(None, "project-1", revision, str(root))
    with pytest.raises(StaleContext):
        mcp_server._require_panorama_receipt("pr_not_a_receipt", "project-1", revision,
                                             str(root))

    opened = mcp_server._panorama_lifecycle(str(root)).open("project-1")
    hashes = [opened["page_hash"]]
    for index in range(1, opened["page_count"]):
        hashes.append(mcp_server._panorama_lifecycle(str(root)).page(
            opened["handle"], opened["revision"], index)["page_hash"])
    receipt = mcp_server._panorama_lifecycle(str(root)).ack(
        opened["handle"], opened["revision"], hashes)["receipt"]
    assert mcp_server._require_panorama_receipt(receipt, "project-1",
                                                opened["revision"], str(root))["complete"]



def test_slice_carries_interval_grid_buckets(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_grid", "project-1", 1)
    lifecycle = PanoramaLifecycle(root, page_bytes=4096)
    opened = lifecycle.open("project-1")
    _ack(lifecycle, opened)
    result = lifecycle.slice(opened["handle"], opened["revision"],
                             window_start_s=36.0, window_end_s=45.0)
    temporal = result["evidence"]["temporal"]
    assert temporal["representation"] == "cross_source_activity_grid"
    assert temporal["bucket_width_s"] == 30.0
    assert [bucket["start_s"] for bucket in temporal["buckets"]] == [30.0]
    assert temporal["buckets"][0]["rms_dbfs_by_source"] == {"{TRACK-1}": 0.2}
    assert result["state"]["scope"] == "project"
    assert result["state"]["details_manifest"] is None  # hand-built doc has no details


def test_status_never_echoes_legacy_page_state(tmp_path: Path) -> None:
    """A handle written by the paged contract must not leak through status.

    Public status is an allowlist: page bookkeeping and mode/cursor/ack fields
    stay private even though the old handle file still contains them.
    """
    import time

    root = _archive(tmp_path)
    _capture(root, "cap_00000001", "project-1", 1)
    lifecycle = PanoramaLifecycle(root)
    handle = "pan_legacy_page_handle"
    view_sha = "a" * 64
    lifecycle._save_handle(handle, {
        "schema_version": "reaper-workbench.panorama-lifecycle/1",
        "handle": handle,
        "project_id": "project-1",
        "capture_id": "cap_00000001",
        "revision": _revision("project-1", 1),
        "fact_root": "f" * 64,
        "document_hash": "d" * 64,
        "created_at": time.time(),
        "complete": True,
        "receipt": "pr_legacy",
        "context_token": "ct_legacy",
        # legacy page bookkeeping that must never be serialized
        "mode": "pages",
        "page_count": 3,
        "received": [0, 1, 2],
        "delivery": {
            "mode": "pages",
            "page_count": 3,
            "pages_received": [0, 1, 2],
            "pages_missing": [],
            "next_page": 3,
            "cursor": "cur_1",
            "ack": True,
            "view_sha256": view_sha,
            "measured_bytes": 1234,
            "max_response_bytes": 24576,
            "fits": True,
        },
    })

    status = lifecycle.status("project-1", handle)
    text = json.dumps(status)

    for forbidden in ('"mode"', '"page_count"', '"pages_received"', '"pages_missing"',
                      '"next_page"', '"cursor"', '"ack"', '"received"', '"page"'):
        assert forbidden not in text, forbidden

    reported = status["handle"]
    assert reported["delivered"] is True
    assert reported["receipt"] == "pr_legacy"
    assert reported["context_token"] == "ct_legacy"
    assert reported["view_sha256"] == view_sha
    assert reported["delivery"] == {
        "view_sha256": view_sha,
        "measured_bytes": 1234,
        "max_response_bytes": 24576,
        "fits": True,
    }


def test_project_id_matching_normalizes_path_separators_and_case(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    archived = "reaper:{AB}:C:/Music/Track.rpp"
    _capture(root, "cap_0000000N", archived, 7)
    lifecycle = PanoramaLifecycle(root, page_bytes=180)

    requested = r"reaper:{ab}:c:\music\track.rpp"
    opened = lifecycle.open(requested)
    assert opened["status"] == "complete"
    status = lifecycle.status(requested, opened["handle"])
    assert status["latest"]["capture_id"] == "cap_0000000N"
    assert status["handle"]["delivered"] is True


def test_revisions_match_ignores_path_separator_and_case() -> None:
    from reaper_audio.context.lifecycle import revisions_match

    stored = _revision("reaper:{AB}:C:/Music/Track.rpp", 7)
    asked = dict(stored, project_id=r"reaper:{ab}:c:\music\track.rpp",
                 path=r"c:\music\track.rpp")
    assert revisions_match(stored, asked) is True
