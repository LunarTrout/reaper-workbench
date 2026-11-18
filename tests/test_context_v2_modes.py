"""Stage E1–E2: work modes, journal, lock."""

from __future__ import annotations

import json

import pytest

from reaper_audio.context.mode import (ModeError, journal_path, load_mode,
                                       mode_brief, set_locked, set_mode,
                                       stale_threshold_fraction)


@pytest.fixture()
def mode_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("REAPER_MCP_MODE_DIR", str(tmp_path))
    monkeypatch.delenv("REAPER_V2_STALE_FRACTION_THRESHOLD", raising=False)
    return tmp_path


def test_default_is_strict_with_fifty_percent(mode_dir) -> None:
    state = load_mode()
    assert state.mode == "strict"
    assert state.stale_threshold_pct == 50
    assert state.locked is False
    assert stale_threshold_fraction(state) == pytest.approx(0.5)


def test_change_is_journalled_with_reason_and_time(mode_dir) -> None:
    set_mode(mode="advisory", stale_threshold_pct=25, reason="owner asked",
             requested_by="owner")
    entries = [json.loads(line) for line in
               journal_path().read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["from"]["mode"] == "strict"
    assert entry["to"]["mode"] == "advisory"
    assert entry["to"]["stale_threshold_pct"] == 25
    assert entry["reason"] == "owner asked"
    assert entry["requested_by"] == "owner"
    assert entry["at"]


def test_invalid_values_are_refused(mode_dir) -> None:
    for mode in ("turbo", ""):
        with pytest.raises(ModeError):
            set_mode(mode=mode, reason="x", requested_by="owner")
    for pct in (-1, 101, True):
        with pytest.raises(ModeError):
            set_mode(stale_threshold_pct=pct, reason="x", requested_by="owner")


def test_reason_is_required(mode_dir) -> None:
    with pytest.raises(ModeError):
        set_mode(mode="off", reason="  ", requested_by="owner")


def test_locked_refuses_mcp_changes_but_allows_external(mode_dir) -> None:
    set_locked(True, reason="freeze", requested_by="owner")
    with pytest.raises(ModeError):
        set_mode(mode="off", reason="try", requested_by="model")
    with pytest.raises(ModeError):
        set_locked(False, reason="try", requested_by="model")
    state = set_mode(mode="off", reason="owner asked", requested_by="owner",
                     external=True)
    assert state.mode == "off"


def test_unlock_from_inside_mcp_is_refused(mode_dir) -> None:
    set_locked(True, reason="freeze", requested_by="owner")
    with pytest.raises(ModeError):
        set_locked(False, reason="unfreeze", requested_by="owner")


def test_hundred_percent_disables_the_stale_trigger(mode_dir) -> None:
    set_mode(stale_threshold_pct=100, reason="disable trigger", requested_by="owner")
    assert stale_threshold_fraction() is None
    assert mode_brief() == {"mode": "strict", "stale_threshold_pct": 100,
                            "locked": False}


def test_malformed_state_file_fails_closed(mode_dir) -> None:
    (mode_dir / "mode.json").write_text("{not json", encoding="utf-8")
    assert load_mode().mode == "strict"
