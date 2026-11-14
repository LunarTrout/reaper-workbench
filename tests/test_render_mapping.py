"""Regression cover for the unnamed-track render path.

Two sides rejected an unnamed track during the first full captures:

* the Lua mapping parsed ``$tracknumber-$track`` with a mandatory trailing dash,
  so ``…-33.wav`` (an empty track name) produced ``number = 0`` and the file was
  counted as an extra master (``tracks=5/6, masters=1/0``);
* the Python render-row validator required a non-empty ``name``, so the same
  row was rejected as invalid metadata.

Both are fixed, and this module pins the two rules that must stay fixed.
"""
from __future__ import annotations

import re
from pathlib import Path

from reaper_audio.reaper.bridge_client import _render_row_name_ok, ReaperBridge

LUA = Path(__file__).resolve().parents[1] / "scripts" / "reaper_bridge.lua"


def _lua_source() -> str:
    return LUA.read_text(encoding="utf-8", errors="replace")


def test_lua_maps_a_name_less_render_file_to_its_track_number() -> None:
    """The render pattern drops its dash when the track name is empty."""
    source = _lua_source()
    assert 'tonumber(rest:match("^(%d+)"))' in source, \
        "the track number must not require a trailing dash"
    assert 'tonumber(rest:match("^(%d+)%-"))' not in source, \
        "the old dash-dependent parse must not come back"
    assert 'if track_name == "" then track_name = "Track " .. tostring(number) end' in source, \
        "an unnamed track still needs a label for the render row"

    # Mirror the Lua rule on the filenames the last capture actually produced.
    prefix = "rali-c8f245f77e214fa3832f49bb937310fa-batch-4-"
    def number_of(file_name: str) -> int:
        rest = file_name[len(prefix):]
        match = re.match(r"^(\d+)", rest)
        return int(match.group(1)) if match else 0

    assert number_of(prefix + "28-GTR LEAD.wav") == 28
    assert number_of(prefix + "33.wav") == 33
    assert number_of(prefix + "0-MASTER.wav") == 0


def test_python_accepts_an_unnamed_render_row_but_not_a_missing_name() -> None:
    assert _render_row_name_ok("") is True
    assert _render_row_name_ok("Track 33") is True
    assert _render_row_name_ok(None) is False
    assert _render_row_name_ok(33) is False


def test_capture_validator_uses_the_row_name_helper() -> None:
    """The capture path must go through the helper, not a local inline check."""
    source = Path(ReaperBridge.__module__.replace(".", "/") + ".py")
    text = (Path(__file__).resolve().parents[1] / "src" / source).read_text(
        encoding="utf-8", errors="replace")
    assert "not _render_row_name_ok(name)" in text
