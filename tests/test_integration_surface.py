"""The merged streams must be reachable through the MCP wrappers."""
from __future__ import annotations

import inspect

from reaper_audio import mcp_server


def test_measure_accepts_three_axes():
    params = inspect.signature(mcp_server.reaper_measure).parameters
    for name in ("intervals", "metrics", "bands", "auto_transport"):
        assert name in params, name


def test_session_accepts_status_actions():
    params = inspect.signature(mcp_server.reaper_session).parameters
    for name in ("detail", "sections", "transport_action", "project_command",
                 "settings_command", "app_command", "since", "timeout_s"):
        assert name in params, name


def test_midi_accepts_grid_and_pattern_arguments():
    params = inspect.signature(mcp_server.reaper_midi).parameters
    for name in ("pattern", "from_bar", "to_bar", "subdivision", "accents",
                 "humanize", "tolerance_ms", "bars", "notes", "apply"):
        assert name in params, name


def test_edit_accepts_sends_envelopes_and_receipts():
    params = inspect.signature(mcp_server.reaper_edit).parameters
    for name in ("verify", "receipt", "source_guid", "owner_guid", "envelope",
                 "interval", "scaling", "allow_overwrite"):
        assert name in params, name


def test_handbook_route_is_registered():
    assert "reaper://handbook" in mcp_server.server._resource_manager._resources
