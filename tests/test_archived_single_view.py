"""Archived-fixture budget, metric, precision and read-scope regressions.

The archived 41-track capture is a real capture fixture, not a hand-built toy.
These tests are skipped only when the archive is absent; when it is present they
prove the actual open envelope and the metric/source contract rather than a
one-off report number.  State files are written under ``tmp_path`` so the raw
archive is never modified.
"""

from __future__ import annotations

import math
import json
from pathlib import Path

import pytest

from reaper_audio.context.full_capture import CaptureArchive
from reaper_audio.context.lifecycle import PanoramaLifecycle
from reaper_audio.context.panorama import build_panorama
from reaper_audio.context.panorama_view import build_view, canonical, view_bytes
from reaper_audio.context.query import run_query
from reaper_audio.errors import ReaperAudioError, StaleContext, ValidationError

from test_capture_panorama import _record

ARCHIVE = (Path(__file__).resolve().parents[1]
           / "work/benchmark/himmp/capture-archive")
CAPTURE = "cap_01M79659V69NDC5YNAA0SMPX51"
pytestmark = pytest.mark.skipif(
    not (ARCHIVE / "captures" / CAPTURE / "record.json").is_file(),
    reason="archived 41-track capture is not present; budget result remains unverified")


def _archive_document():
    record = CaptureArchive(ARCHIVE).read(CAPTURE)
    document = build_panorama(
        record, record_path=f"captures/{CAPTURE}/record.json",
        max_bytes=10 ** 12, max_tokens=10 ** 12)
    return record, document


def test_archived_42_source_open_fits_one_result_with_all_metrics(tmp_path: Path) -> None:
    record, _document = _archive_document()
    lifecycle = PanoramaLifecycle(ARCHIVE, state_root=tmp_path / "state")
    opened = lifecycle.single(record.project_id)

    assert opened["status"] == "complete"
    assert opened["delivery"]["fits"] is True
    assert opened["delivery"]["response_bytes"] <= 48_000
    view = opened["view"]
    assert view["view_mode"] == "lean_overview"
    assert view_bytes(view) <= 45_000
    assert "items" not in view and "takes" not in view
    assert "content_index" in view
    assert view["analysis"]["precision"]["lossless"] is False
    assert view["analysis"]["precision"]["exact_values_preserved"] is True
    assert len(view["analysis"]["metric_columns"]) == 24
    assert len(view["analysis"]["metric_units"]) == 24
    assert len(view["analysis"]["metric_decimals"]) == 24
    assert len(view["analysis"]["rows"]["columns"]) == 6 + 24
    assert len(view["analysis"]["rows"]["rows"]) == 42
    assert {row[0] for row in view["analysis"]["rows"]["rows"]} == set(range(42))
    assert len(view["content_index"]["rows"]) == 42
    assert sum(row[1] for row in view["content_index"]["rows"]) == 34
    assert sum(row[4] for row in view["content_index"]["rows"]) == 34
    assert {"items", "takes"} <= set(view["archive"]["catalogued_families"])
    assert "items" in view["archive"]["exact"]
    assert "takes" in view["archive"]["exact"]


def test_archived_full_and_lean_carry_the_same_metric_facts() -> None:
    _record_obj, document = _archive_document()
    full = build_view(document, lean=False)
    lean = build_view(document, lean=True)

    assert full["analysis"]["metric_columns"] == lean["analysis"]["metric_columns"]
    assert full["analysis"]["metric_units"] == lean["analysis"]["metric_units"]
    assert full["analysis"]["metric_decimals"] == lean["analysis"]["metric_decimals"]
    assert full["analysis"]["methods"] == lean["analysis"]["methods"]
    assert full["analysis"]["stages"] == lean["analysis"]["stages"]
    assert full["analysis"]["signal_stages"] == lean["analysis"]["signal_stages"]
    assert full["analysis"]["windows"] == lean["analysis"]["windows"]
    assert full["analysis"]["channels"] == lean["analysis"]["channels"]
    assert full["analysis"]["absent"] == lean["analysis"]["absent"]
    assert len(full["analysis"]["rows"]["rows"]) == 42
    for full_row, lean_row in zip(full["analysis"]["rows"]["rows"],
                                  lean["analysis"]["rows"]["rows"]):
        # source, availability, measurement stage, signal stage, window, channel
        assert full_row[:6] == lean_row[:6]
        # all 24 rounded scalar facts are identical in both projections
        assert full_row[6:] == lean_row[6:]
    assert all(row[0] != -1 for row in lean["analysis"]["rows"]["rows"])


def test_inline_precision_is_explicit_and_exact_query_returns_raw_values() -> None:
    record, document = _archive_document()
    view = build_view(document, lean=True)
    analysis = document["analysis"][0]
    metric = "crest_db"
    raw = analysis["metrics"][metric]
    assert isinstance(raw, float)
    assert view["analysis"]["precision"]["lossless"] is False
    assert view["analysis"]["precision"]["exact_values_preserved"] is True
    assert "reaper_panorama_query" in view["analysis"]["precision"]["exact_query"]
    inline = view["analysis"]["rows"]["rows"][0][
        view["analysis"]["rows"]["columns"].index(metric)]
    assert inline != raw, "the fixture must exercise an actually rounded scalar"
    answer = run_query(
        document, record, data=["analysis"], objects=[analysis["analysis_id"]],
        metrics=[metric], max_response_bytes=48_000)
    assert answer["status"] == "query"
    returned = answer["data"]["analysis"][0]["metrics"][metric]
    assert returned == raw


def test_one_bucket_temporal_compression_is_value_equivalent() -> None:
    _record_obj, document = _archive_document()
    view = build_view(document, lean=True)
    temporal = document["analysis_temporal"]
    buckets = temporal["buckets"]
    source_ids = ["track:" + str(row[0]) for row in view["entities"]["track"]]
    if view["entities"].get("master"):
        source_ids.append("master:" + str(view["entities"]["master"][0]))
    levels = view["temporal"]["levels"]
    assert levels
    for level in levels:
        if not level.get("one_bucket_equality"):
            continue
        assert level["columns"] == ["buckets", "value"]
        for index, row in enumerate(level["rows"]):
            assert row[0] == 1
            values = [bucket["rms_dbfs_by_source"].get(source_ids[index])
                      for bucket in buckets
                      if bucket.get("rms_dbfs_by_source", {}).get(source_ids[index]) is not None]
            assert values
            expected = round(sum(values) / len(values), 2)
            assert math.isclose(row[1], expected, abs_tol=0.011)


def test_content_index_selectors_return_all_exact_item_and_take_rows() -> None:
    record, document = _archive_document()
    view = build_view(document, lean=True)
    track_id = "track:" + str(view["entities"]["track"][0][0])
    expected_items = view["content_index"]["rows"][0][1]
    expected_takes = view["content_index"]["rows"][0][4]

    items = run_query(document, record, data=["items"], objects=[track_id],
                      max_response_bytes=48_000)
    takes = run_query(document, record, data=["takes"], objects=[track_id],
                      max_response_bytes=48_000)
    assert items["status"] == "query"
    assert takes["status"] == "query"
    assert len(items["data"]["items"]) == expected_items
    assert len(takes["data"]["takes"]) == expected_takes


def test_read_scope_is_read_only_bound_and_queryable(tmp_path: Path) -> None:
    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)
    lifecycle = PanoramaLifecycle(archive.root, state_root=tmp_path / "state")
    opened = lifecycle.single(record.project_id, max_response_bytes=1_000)
    assert opened["status"] == "response_over_budget"
    assert "view" not in opened
    assert "receipt" not in opened and "context_token" not in opened and "handle" not in opened
    scope = opened["read_scope"]
    assert scope["id"].startswith("rs_")
    assert scope["permissions"] == ["read"]
    assert scope["allowed"] == ["reaper_panorama_query"]

    document = build_panorama(record, max_bytes=10 ** 9, max_tokens=10 ** 9)
    analysis = document["analysis"][0]
    answer = lifecycle.query(
        "", opened["revision"], data=["analysis"],
        objects=[analysis["analysis_id"]], metrics=["crest_db"],
        read_scope=scope["id"])
    assert answer["status"] == "query"
    assert answer["read_only"] is True
    assert answer["read_scope"] == scope["id"]
    assert "crest_db" in answer["data"]["analysis"][0]["metrics"]

    # A read_scope is not a full-view receipt and never authorizes mutation.
    with pytest.raises(ReaperAudioError):
        lifecycle.require_receipt(scope["id"], record.project_id, opened["revision"])
    with pytest.raises(StaleContext):
        lifecycle.query("", {**opened["revision"], "change_count": 999},
                        data=["analysis"], read_scope=scope["id"])


def test_archived_legacy_sidecar_metrics_are_mapped_not_dropped() -> None:
    sidecar = ARCHIVE / "captures" / CAPTURE / "panorama.json"
    document = json.loads(sidecar.read_text(encoding="utf-8"))
    assert document["schema_version"] == "reaper-workbench.capture-panorama/1"
    view = build_view(document)
    analysis = view["analysis"]
    assert analysis["metric_schema"] == "legacy_positional_list"
    assert analysis["metric_columns_source"] == "analysis_metric_columns"
    assert len(analysis["metric_columns"]) == 9
    assert len(analysis["rows"]["rows"]) == 42
    assert {row[0] for row in analysis["rows"]["rows"]} == set(range(42))
    first = document["analysis"][0]
    first_row = analysis["rows"]["rows"][0]
    for index, name in enumerate(analysis["metric_columns"]):
        raw = first["metrics"][index]
        expected = None if raw is None else round(raw, analysis["metric_decimals"][name])
        assert first_row[6 + index] == expected
        assert first_row[6 + index] is not None or raw is None
    assert analysis["stages"] == ["legacy_unspecified"]
    assert analysis["windows"] == [[None, None]]
    assert analysis["rms_stats"]["rows"] == []


def test_current_named_mapping_metrics_are_complete_and_match_raw() -> None:
    record, document = _archive_document()
    analysis = build_view(document)["analysis"]
    assert analysis["metric_schema"] == "current_named_mapping"
    assert len(analysis["metric_columns"]) == 24
    assert len(analysis["rows"]["rows"]) == 42
    for source_index, raw_row in enumerate(document["analysis"]):
        view_row = analysis["rows"]["rows"][source_index]
        assert view_row[0] == source_index
        for index, name in enumerate(analysis["metric_columns"]):
            raw = raw_row["metrics"].get(name)
            expected = None if raw is None else round(raw, analysis["metric_decimals"][name])
            assert view_row[6 + index] == expected


def test_archived_overbudget_read_scope_query_and_read_only(tmp_path: Path) -> None:
    record, document = _archive_document()
    lifecycle = PanoramaLifecycle(ARCHIVE, state_root=tmp_path / "state")
    opened = lifecycle.single(record.project_id, max_response_bytes=2_000)
    assert opened["status"] == "response_over_budget"
    assert "view" not in opened
    assert "receipt" not in opened and "context_token" not in opened and "handle" not in opened
    scope = opened["read_scope"]
    analysis = document["analysis"][0]
    metric = "crest_db"
    raw = analysis["metrics"][metric]
    answer = lifecycle.query(
        "", opened["revision"], data=["analysis"],
        objects=[analysis["analysis_id"]], metrics=[metric],
        read_scope=scope["id"])
    assert answer["status"] == "query"
    assert answer["read_only"] is True
    assert answer["data"]["analysis"][0]["metrics"][metric] == raw

    with pytest.raises(ValidationError):
        lifecycle.require_receipt(scope["id"], record.project_id, opened["revision"])

    # The exact query itself remains bounded and refuses; no partial data.
    oversized = lifecycle.query(
        "", opened["revision"], data=["series"],
        objects=[analysis["analysis_id"]], series=["band_level"],
        read_scope=scope["id"])
    assert oversized["status"] == "query_too_large"
    assert "data" not in oversized


def test_full_open_after_refusal_issues_normal_receipt_and_token(tmp_path: Path) -> None:
    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)
    lifecycle = PanoramaLifecycle(archive.root, state_root=tmp_path / "state")
    refused = lifecycle.single(record.project_id, max_response_bytes=1_000)
    assert refused["status"] == "response_over_budget"
    opened = lifecycle.single(record.project_id)
    assert opened["status"] == "complete"
    assert opened["receipt"].startswith("pr_")
    assert opened["context_token"].startswith("ct_")
    assert "read_scope" not in opened


def test_legacy_positional_metric_rows_are_mapped_without_archive(
        tmp_path: Path) -> None:
    record, _files = _record(tmp_path)
    document = build_panorama(record, max_bytes=10 ** 9, max_tokens=10 ** 9)
    legacy = json.loads(canonical(document))
    columns = ["integrated_lufs", "peak_dbfs"]
    legacy["analysis_metric_columns"] = columns
    legacy["analysis_metric_units"] = {
        "integrated_lufs": "LUFS", "peak_dbfs": "dBFS"}
    for row in legacy["analysis"]:
        source_guid = row.get("source_guid")
        assert source_guid
        row["source_guid"] = source_guid
        row.pop("source_id", None)
        row.pop("measured_window", None)
        row.pop("stage", None)
        row.pop("channel_view", None)
        row["metrics"] = [row["metrics"][name] for name in columns]
    view = build_view(legacy)
    analysis = view["analysis"]
    assert analysis["metric_schema"] == "legacy_positional_list"
    assert analysis["metric_columns"] == columns
    assert len(analysis["rows"]["rows"]) == len(legacy["analysis"])
    for index, source in enumerate(legacy["analysis"]):
        row = analysis["rows"]["rows"][index]
        assert row[0] >= 0
        for metric_index, name in enumerate(columns):
            raw = source["metrics"][metric_index]
            expected = None if raw is None else round(raw, analysis["metric_decimals"][name])
            assert row[6 + metric_index] == expected
            assert row[6 + metric_index] is not None or raw is None
    assert analysis["stages"] == ["legacy_unspecified"]
    assert analysis["windows"] == [[None, None]]


def _copy_archive_metadata(tmp_path: Path) -> str:
    import shutil
    root = tmp_path / "archive-copy"
    source = ARCHIVE / "captures" / CAPTURE
    target = root / "captures" / CAPTURE
    target.mkdir(parents=True)
    for name in ("record.json", "panorama.json"):
        shutil.copy2(source / name, target / name)
    rows = []
    for line in (ARCHIVE / "index.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("capture_id")) == CAPTURE:
            rows.append(line)
    (root / "index.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return str(root)


def test_mcp_archived_exact_read_ignores_incomplete_live_identity(tmp_path: Path) -> None:
    from reaper_audio import mcp_server

    root = _copy_archive_metadata(tmp_path)
    record, document = _archive_document()
    analysis = document["analysis"][0]
    metric = "crest_db"
    raw = analysis["metrics"][metric]
    from panorama_flow import open_and_ack
    opened, ack, _pages = open_and_ack(
        mcp_server, record.project_id, root, capture_id=CAPTURE)
    assert ack["status"] == "complete"
    answer = mcp_server.reaper_panorama_query(
        revision=ack["revision"], archive_root=root, handle=ack["handle"],
        panorama_receipt=ack["receipt"], data=["analysis"],
        objects=[analysis["analysis_id"]], metrics=[metric])
    assert answer["status"] == "query"
    assert answer["data"]["analysis"][0]["metrics"][metric] == raw

    # The same receipt cannot authorize a mutation when identity is incomplete.
    with pytest.raises(StaleContext):
        mcp_server._require_panorama_receipt(
            ack["receipt"], record.project_id, ack["revision"], root)

    refused = mcp_server._panorama_lifecycle(root).single(
        record.project_id, capture_id=CAPTURE, max_response_bytes=2_000)
    assert refused["status"] == "response_over_budget"
    scope = refused["read_scope"]
    scoped = mcp_server.reaper_panorama_query(
        revision=refused["revision"], archive_root=root, read_scope=scope["id"],
        data=["analysis"], objects=[analysis["analysis_id"]], metrics=[metric])
    assert scoped["status"] == "query"
    assert scoped["data"]["analysis"][0]["metrics"][metric] == raw


def test_mcp_query_schema_and_read_scope_dispatch(tmp_path: Path) -> None:
    import inspect
    from reaper_audio import mcp_server
    from test_capture_panorama import _record as small_record

    params = inspect.signature(mcp_server.reaper_panorama_query).parameters
    assert params["handle"].default is None
    assert params["revision"].default is inspect.Parameter.empty
    schema = mcp_server.server._tool_input_schema("reaper_panorama_query")
    assert schema is not None
    assert schema["required"] == ["revision", "archive_root"]
    assert "read_scope" in schema["properties"]
    assert "handle" not in schema["required"]

    record, files = small_record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)
    root = str(archive.root)
    opened = mcp_server._panorama_lifecycle(root).single(
        record.project_id, max_response_bytes=1_000)
    assert opened["status"] == "response_over_budget"
    scope = opened["read_scope"]
    document = build_panorama(record, max_bytes=10 ** 9, max_tokens=10 ** 9)
    analysis = document["analysis"][0]
    answer = mcp_server.reaper_panorama_query(
        revision=opened["revision"], archive_root=root,
        read_scope=scope["id"], data=["analysis"],
        objects=[analysis["analysis_id"]], metrics=["crest_db"])
    assert answer["status"] == "query"
    assert answer["read_only"] is True
    assert "crest_db" in answer["data"]["analysis"][0]["metrics"]

    combined = mcp_server.reaper_panorama_query(
        revision=opened["revision"], archive_root=root, handle="pan_fake",
        read_scope=scope["id"], data=["analysis"])
    assert combined["ok"] is False

    missing = mcp_server.reaper_panorama_query(
        revision=opened["revision"], archive_root=root, data=["analysis"])
    assert missing["ok"] is False


def test_tool_docs_describe_the_paged_receipt_route() -> None:
    from reaper_audio import mcp_server

    docstring = mcp_server.reaper_panorama_query.__doc__ or ""
    assert "panorama_receipt" in docstring
    assert "never pages" in docstring
    paths = [
        Path(__file__).resolve().parents[1] / "README.md",
        Path(__file__).resolve().parents[1] / "README.ru.md",
        Path(__file__).resolve().parents[1] / "docs/MCP-README-LLM.md",
        Path(__file__).resolve().parents[1] / "docs/MCP-README-LLM.ru.md",
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "reaper_panorama_ack" in text, path
        assert 'handle=""' not in text, path
        assert "query_too_large" in text, path

