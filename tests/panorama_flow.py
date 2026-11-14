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
    pages = [opened]
    for page_id in opened["manifest"]["page_ids"][1:]:
        pages.append(mcp_server.reaper_panorama_page(
            handle, revision, archive_root, page_id))
    ack = mcp_server.reaper_panorama_ack(
        handle, revision, archive_root,
        [page["page_hash"] for page in pages])
    return opened, ack, pages
