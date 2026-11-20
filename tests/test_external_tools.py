"""External tool discovery: explicit paths win, versioned bundles are ordered."""

from __future__ import annotations

from pathlib import Path

import pytest

import reaper_audio.daw.external_tools as tools


def _fake_bundle(root: Path, version: str) -> Path:
    executable = root / f"ffmpeg-{version}-win32-x64" / "bin" / "ffmpeg.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    return executable


def test_a_stale_explicit_path_is_refused_instead_of_ignored(tmp_path: Path) -> None:
    with pytest.raises(tools.ToolUnavailable) as error:
        tools.find_ffmpeg(tmp_path / "not-here" / "ffmpeg.exe")
    assert error.value.code == "tool_unavailable"
    assert error.value.details["tool"] == "ffmpeg"
    assert error.value.details["origin"] == "argument"


def test_the_newest_versioned_hermes_bundle_is_selected(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch
                                                        ) -> None:
    root = tmp_path / "hermes" / "tools"
    old = _fake_bundle(root, "9.0.1")
    new = _fake_bundle(root, "10.2")
    monkeypatch.setattr(tools, "_hermes_tool_roots", lambda: [root])
    monkeypatch.setattr(tools.shutil, "which", lambda name: None)
    monkeypatch.delenv("REAPER_WORKBENCH_FFMPEG", raising=False)
    monkeypatch.delenv("FFMPEG_BINARY", raising=False)

    found = tools.find_ffmpeg(required=False)
    assert found is not None
    assert found == new.resolve()
    assert found != old.resolve()


def test_an_explicit_path_outranks_a_stale_environment(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "given" / "ffmpeg.exe"
    explicit.parent.mkdir()
    explicit.write_bytes(b"")
    monkeypatch.setenv("REAPER_WORKBENCH_FFMPEG", str(tmp_path / "stale" / "ffmpeg.exe"))
    assert tools.find_ffmpeg(explicit) == explicit.resolve()


def test_an_environment_path_wins_over_the_bundle_search(tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch
                                                          ) -> None:
    explicit = tmp_path / "custom" / "ffmpeg.exe"
    explicit.parent.mkdir()
    explicit.write_bytes(b"")
    monkeypatch.setenv("REAPER_WORKBENCH_FFMPEG", str(explicit))
    monkeypatch.setattr(tools.shutil, "which", lambda name: pytest.fail("PATH must not be read"))
    assert tools.find_ffmpeg() == explicit.resolve()
