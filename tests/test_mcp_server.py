from __future__ import annotations

import asyncio

import numpy as np
import pytest
import soundfile as sf

pytest.importorskip("mcp")

from reaper_audio import mcp_server
from reaper_audio.daw.session import ProjectRevision
from reaper_audio.reaper.api_catalog import get_api_catalog
from reaper_audio.reaper.handles import ReaperHandle


def test_capability_domains_cover_catalog_once() -> None:
    catalog = get_api_catalog()
    domains = mcp_server._api_domains()
    flattened = [name for names in domains.values() for name in names]

    assert len(catalog.functions) == 732
    assert len(flattened) == 732
    assert set(flattened) == set(catalog.functions)
    assert len(flattened) == len(set(flattened))


def test_capability_tool_pages_one_domain() -> None:
    result = mcp_server.reaper_capabilities(
        mode="domain", domain="tracks", offset=0, limit=1, detail="names")

    assert result["returned_count"] == 1
    assert result["documented_function_count"] > 1
    assert result["next_offset"] == 1
    assert "runtime_note" in result


def test_structured_search_finds_item_split() -> None:
    result = mcp_server.reaper_api_search(
        object="media_item", operation="split",
        qualifiers=["at_project_time"], kind="api", limit=5)

    assert result["structured_query"]["object"] == "media_item"
    assert any(hit["name"] == "SplitMediaItem" for hit in result["api"]["results"])


def test_structured_search_surfaces_track_value_setter_for_fader_and_pan() -> None:
    result = mcp_server.reaper_api_search(
        object="track", operation="set",
        qualifiers=["D_VOL", "D_PAN"], kind="api", limit=5)

    assert result["api"]["results"][0]["name"] == "SetMediaTrackInfo_Value"


def test_reference_windows_use_reference_file_time_and_have_a_tool_schema(
        tmp_path) -> None:
    path = tmp_path / "reference.wav"
    sample_rate = 8_000
    time = np.arange(2 * sample_rate, dtype=np.float64) / sample_rate
    sf.write(path, 0.1 * np.sin(2 * np.pi * 440 * time),
             sample_rate, subtype="FLOAT")

    requests = mcp_server._reference_requests(None, [{
        "path": str(path), "start_s": 1.0, "end_s": 2.0,
    }])
    reference = mcp_server.load_audio(requests[0]["path"], requests[0]["section"])
    tool = next(tool for tool in asyncio.run(mcp_server.server.list_tools())
                if tool.name == "reaper_analyze")

    assert requests[0]["selection"] == "source_file_window"
    assert reference.start_s == pytest.approx(1.0)
    assert reference.duration_s == pytest.approx(1.0)
    assert "reference_windows" in tool.input_schema["properties"]
    window_schema = tool.input_schema["$defs"]["ReferenceWindow"]
    assert window_schema["required"] == ["path", "start_s", "end_s"]


def test_errors_are_returned_as_typed_tool_results() -> None:
    result = mcp_server.reaper_capabilities(mode="unknown")

    assert result["ok"] is False
    assert result["error"]["code"] == "validation_error"
    assert result["error"]["next_action"]


def test_project_reference_becomes_revision_bound_host_handle() -> None:
    revision = ProjectRevision(project_id="project-id", change_count=7,
                               path="C:/music/test.RPP")

    track = mcp_server._handle_argument(
        {"$reaper_ref": {"kind": "MediaTrack", "guid": "{TRACK-GUID}"}},
        kind="MediaTrack", revision=revision, bridge_session_id=None)
    project = mcp_server._handle_argument(
        {"$reaper_ref": {"kind": "ReaProject"}}, kind="ReaProject",
        revision=revision, bridge_session_id=None)

    assert isinstance(track, ReaperHandle)
    assert track.stable_id == "{TRACK-GUID}"
    assert track.change_count == 7
    assert project.kind == "ReaProject"
    assert project.stable_id == revision.project_id


def test_mcp_surface_is_bounded_and_has_discovery_resources() -> None:
    tools = asyncio.run(mcp_server.server.list_tools())
    templates = asyncio.run(mcp_server.server.list_resource_templates())
    names = {tool.name for tool in tools}
    uris = {resource.uri_template for resource in templates}

    assert len(names) == 29
    assert {"reaper_capabilities", "reaper_api_search", "reaper_api_call",
            "reaper_project", "reaper_analyze", "reaper_edit",
            "reaper_render_candidate", "reaper_edit_map", "reaper_mix_evidence",
            "reaper_action", "reaper_save", "reaper_release", "reaper_session",
            "reaper_timeline_context", "reaper_spatial_status",
            "reaper_spatial_analyze", "reaper_spatial_timeline",
            "reaper_flamingo_status", "reaper_flamingo_describe",
            "reaper_flamingo_ask", "reaper_capture_snapshot",
            "reaper_capture_read", "reaper_panorama_open",
            "reaper_panorama_page", "reaper_panorama_ack",
            "reaper_panorama_query", "reaper_panorama_status",
            "reaper_panorama_changes_since", "reaper_panorama_refresh"} == names
    # The paged panorama is model-visible; the old slice/detail/delta-ack tools are not.
    for removed in ("reaper_panorama_detail", "reaper_panorama_slice",
                    "reaper_panorama_delta_ack"):
        assert removed not in names
    assert "reaper://api/{name}" in uris
    assert "reaper://analyses/{analysis_id}/audio/{source_guid}" in uris


def test_capability_index_resource_is_compact_json() -> None:
    result = mcp_server.capability_index_resource()

    assert '"documented_api_count":732' in result
    assert "runtime_claim" in result


def test_mcp_read_scope_exact_query_is_read_only(tmp_path) -> None:
    from reaper_audio.context.full_capture import CaptureArchive
    from reaper_audio.context.panorama import build_panorama
    from reaper_audio.errors import ReaperAudioError
    from test_capture_panorama import _record

    record, files = _record(tmp_path)
    archive = CaptureArchive(tmp_path / "archive")
    archive.append(record, files)
    root = str(archive.root)
    opened = mcp_server._panorama_lifecycle(root).single(
        record.project_id, max_response_bytes=1_000)
    assert opened["status"] == "response_over_budget"
    scope = opened["read_scope"]
    document = build_panorama(record, max_bytes=10 ** 9, max_tokens=10 ** 9)
    source = document["analysis"][0]
    metric = next(name for name in document["analysis_metric_units"]
                  if isinstance(source["metrics"].get(name), (int, float)))
    answer = mcp_server.reaper_panorama_query(
        revision=opened["revision"], archive_root=root, read_scope=scope["id"],
        data=["analysis"], objects=[source["analysis_id"]], metrics=[metric])
    assert answer["status"] == "query"
    assert answer["read_only"] is True
    assert answer["data"]["analysis"][0]["metrics"][metric] == source["metrics"][metric]
    with pytest.raises(ReaperAudioError):
        mcp_server._require_panorama_receipt(
            scope["id"], record.project_id, opened["revision"], root)


def test_mcp_archived_exact_read_ignores_incomplete_identity(tmp_path) -> None:
    from test_archived_single_view import (
        CAPTURE, _archive_document, _copy_archive_metadata)
    from reaper_audio.errors import StaleContext

    root = _copy_archive_metadata(tmp_path)
    record, document = _archive_document()
    source = document["analysis"][0]
    metric = "crest_db"
    from panorama_flow import open_and_ack
    opened, ack, _pages = open_and_ack(
        mcp_server, record.project_id, root, capture_id=CAPTURE)
    assert ack["status"] == "complete"
    answer = mcp_server.reaper_panorama_query(
        revision=ack["revision"], archive_root=root, handle=ack["handle"],
        panorama_receipt=ack["receipt"], data=["analysis"],
        objects=[source["analysis_id"]], metrics=[metric])
    assert answer["status"] == "query"
    assert answer["data"]["analysis"][0]["metrics"][metric] == source["metrics"][metric]
    with pytest.raises(StaleContext):
        mcp_server._require_panorama_receipt(
            ack["receipt"], record.project_id, ack["revision"], root)
