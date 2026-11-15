"""Shared MCP panorama flow helper: open, fetch every page, acknowledge."""
from __future__ import annotations

from typing import Any


def open_and_ack(mcp_server: Any, project_id: str, archive_root: str,
                 **kwargs: Any) -> tuple[dict, dict, list[dict]]:
    opened = mcp_server.reaper_panorama_open(project_id, archive_root, **kwargs)
    if opened.get("status") != "page":
        return opened, opened, [opened]
    handle = opened["handle"]
    revision = opened["revision"]
    manifest = opened["manifest"]
    page_ids = manifest.get("page_ids")
    if page_ids is None:
        page_count = int(manifest["page_count"])
        page_ids = [f"pg_{index}_of_{page_count}" for index in range(1, page_count + 1)]
    pages = [opened]
    for page_id in page_ids[1:]:
        pages.append(mcp_server.reaper_panorama_page(
            handle, revision, archive_root, page_id))
    ack = mcp_server.reaper_panorama_ack(
        handle, revision, archive_root,
        [page["page_hash"] for page in pages])
    return opened, ack, pages
