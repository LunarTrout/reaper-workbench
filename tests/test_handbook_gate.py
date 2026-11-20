"""The handbook must be served whole, read once, and gate the work."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from reaper_audio import mcp_server
from reaper_audio.errors import ValidationError


@pytest.fixture(autouse=True)
def _reset_gate(monkeypatch):
    mcp_server._handbook_read.clear()
    monkeypatch.setenv("REAPER_HANDBOOK_TEST", "1")
    yield
    mcp_server._handbook_read.clear()


def test_handbook_is_served_whole():
    text = mcp_server.handbook_resource()
    raw = Path(mcp_server._HANDBOOK_PATH).read_bytes()
    assert text == raw.decode("utf-8")
    assert mcp_server._handbook_sha() == hashlib.sha256(raw).hexdigest()


def test_gate_refuses_until_read_then_unlocks():
    with pytest.raises(ValidationError) as error:
        mcp_server._handbook_required()
    assert error.value.details.get("code") == "HANDBOOK_REQUIRED"

    mcp_server.handbook_resource()
    mcp_server._handbook_required()  # no raise


def test_gate_resets_when_handbook_changes(monkeypatch):
    mcp_server._mark_handbook_read()
    mcp_server._handbook_required()

    monkeypatch.setattr(mcp_server, "_handbook_sha", lambda: "0" * 64)
    with pytest.raises(ValidationError):
        mcp_server._handbook_required()


def test_instructions_stay_short():
    source = Path(mcp_server.__file__).read_text(encoding="utf-8")
    start = source.find("instructions=(")
    assert start >= 0
    open_paren = source.find("(", start)
    depth = 0
    for index in range(open_paren, len(source)):
        if source[index] == "(":
            depth += 1
        elif source[index] == ")":
            depth -= 1
            if depth == 0:
                end = index
                break
    block = source[start:end + 1]
    assert len(block) <= 2200, f"instructions block is {len(block)} chars"
    assert "reaper://handbook" in block
    assert "HANDBOOK_REQUIRED" in block
    assert os.environ.get("REAPER_HANDBOOK_TEST") == "1"
