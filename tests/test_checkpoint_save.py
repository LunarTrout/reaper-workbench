"""Contract tests for the narrow checkpoint save (mock transport only)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reaper_audio.daw.checkpoint import checkpoint_path, checkpoint_root, save_checkpoint
from reaper_audio.errors import BridgeUnavailable, ValidationError

OLD_ID = "reaper:{GUID}:C:/before/before_mix.RPP"
NEW_ID = "reaper:{GUID}:C:/before/ckpt_1.RPP"


def _world(tmp_path: Path, *, dirty: int = 0, saved_path: str | None = None,
           rebound: str = NEW_ID, online: bool = True, saved: bool = True) -> dict[str, Any]:
    target = tmp_path / "ckpt_1.RPP"
    state = {"health_calls": 0}

    def health() -> dict[str, Any]:
        state["health_calls"] += 1
        first = state["health_calls"] == 1
        return {"project": {"project_id": OLD_ID if first else rebound,
                            "change_count": 7 if first else 8}}

    def call(operation: str, arguments: dict[str, Any]) -> Any:
        if operation == "project.save":
            return {"saved": saved, "path": saved_path or str(target),
                    "mode": "save_as"}
        if operation == "project.render_settings.read":
            return {"dirty": dirty}
        raise AssertionError(f"unexpected operation {operation}")

    rows = [{"source_online": online, "source_length_s": 12.0,
             "active_take": {"is_midi": False, "source": "C:/m/a.wav"}},
            {"source_online": True, "source_length_s": 3.0,
             "active_take": {"is_midi": False, "source": "C:/m/b.wav"}},
            {"active_take": {"is_midi": True}}]

    def context() -> dict[str, Any]:
        return {"tracks": [{"items": rows}]}

    return {"target": target, "root": tmp_path, "health": health, "call": call,
            "context": context}


def _run(world: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "path": str(world["target"]), "project_id": OLD_ID,
        "expected_revision": {"change_count": 7},
        "call": world["call"], "health": world["health"],
        "context": world["context"], "root": world["root"],
    }
    kwargs.update(overrides)
    return save_checkpoint(**kwargs)


def test_success_reports_path_dirty_and_media(tmp_path: Path) -> None:
    world = _world(tmp_path)
    result = _run(world)
    assert result["saved"] is True
    assert result["path"] == str(world["target"])
    assert result["project_id"] == NEW_ID
    assert result["previous_project_id"] == OLD_ID
    assert result["dirty"] == 0
    assert result["media"] == {"audio_items": 2, "online": 2, "offline": 0}


def test_checkpoint_root_is_portable_and_configurable(monkeypatch: pytest.MonkeyPatch,
                                                       tmp_path: Path) -> None:
    monkeypatch.delenv("REAPER_CHECKPOINT_ROOT", raising=False)
    assert checkpoint_root() == Path.home() / "Documents" / "REAPER Workbench" / "versions"
    override = tmp_path / "project-versions"
    monkeypatch.setenv("REAPER_CHECKPOINT_ROOT", str(override))
    assert checkpoint_root() == override


def test_existing_file_is_refused(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world["target"].write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError) as raised:
        _run(world)
    assert "already exists" in str(raised.value)


def test_relative_and_non_rpp_paths_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        checkpoint_path("versions/ckpt.RPP")
    with pytest.raises(ValidationError):
        checkpoint_path(tmp_path / "ckpt.wav")
    with pytest.raises(ValidationError):
        checkpoint_path(tmp_path / "missing_dir" / "ckpt.RPP")


def test_foreign_project_id_is_refused(tmp_path: Path) -> None:
    world = _world(tmp_path)
    with pytest.raises(ValidationError) as raised:
        _run(world, project_id="reaper:{OTHER}:C:/other.RPP")
    assert "does not name the active project" in str(raised.value)


def test_stale_revision_is_refused_before_saving(tmp_path: Path) -> None:
    world = _world(tmp_path)
    calls: list[str] = []
    world["call"] = (lambda op, args: calls.append(op) or {})
    with pytest.raises(BridgeUnavailable) as raised:
        _run(world, expected_revision={"change_count": 3})
    assert raised.value.details["reason"] == "stale_context"
    assert calls == [], "a stale caller must not reach the save"


def test_missing_expected_revision_is_refused(tmp_path: Path) -> None:
    world = _world(tmp_path)
    calls: list[str] = []
    world["call"] = (lambda op, args: calls.append(op) or {})
    with pytest.raises(ValidationError) as raised:
        _run(world, expected_revision=None)
    assert "expected_revision is required" in str(raised.value)
    assert calls == []


def test_unconfirmed_save_is_an_error(tmp_path: Path) -> None:
    world = _world(tmp_path, saved=False)
    with pytest.raises(BridgeUnavailable) as raised:
        _run(world)
    assert raised.value.details["reason"] == "save_unconfirmed"


def test_path_mismatch_is_an_error(tmp_path: Path) -> None:
    world = _world(tmp_path, saved_path=str(tmp_path / "other.RPP"))
    with pytest.raises(BridgeUnavailable) as raised:
        _run(world)
    assert raised.value.details["reason"] == "path_mismatch"


def test_missing_rebind_is_an_error(tmp_path: Path) -> None:
    world = _world(tmp_path, rebound=OLD_ID)
    with pytest.raises(BridgeUnavailable) as raised:
        _run(world)
    assert raised.value.details["reason"] == "project_not_rebound"


def test_offline_media_fails_the_checkpoint(tmp_path: Path) -> None:
    world = _world(tmp_path, online=False)
    with pytest.raises(BridgeUnavailable) as raised:
        _run(world)
    assert raised.value.details["reason"] == "media_unresolved"
    assert raised.value.details["media"]["offline"] == 1


def test_dirty_state_after_save_fails_closed(tmp_path: Path) -> None:
    world = _world(tmp_path, dirty=1)
    with pytest.raises(BridgeUnavailable) as raised:
        _run(world)
    assert raised.value.details["reason"] == "dirty_after_save"


def test_target_outside_the_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "versions"
    root.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(ValidationError) as raised:
        checkpoint_path(other / "ckpt.RPP", root=root)
    assert "outside the allowed directory" in str(raised.value)


def test_parent_traversal_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "versions"
    root.mkdir()
    with pytest.raises(ValidationError) as raised:
        checkpoint_path(root / ".." / "escape.RPP", root=root)
    assert "'..'" in str(raised.value)


def test_containment_is_case_insensitive(tmp_path: Path) -> None:
    root = tmp_path / "versions"
    root.mkdir()
    upper = Path(str(root).upper()) / "CKPT.RPP"
    resolved = checkpoint_path(upper, root=root)
    assert resolved.name == "CKPT.RPP"
    assert resolved.parent.resolve() == root.resolve()


def test_symlinked_directory_cannot_escape_the_root(tmp_path: Path) -> None:
    root = tmp_path / "versions"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create directory symlinks without privileges")
    with pytest.raises(ValidationError) as raised:
        checkpoint_path(link / "escape.RPP", root=root)
    assert "outside the allowed directory" in str(raised.value)


def test_dangling_file_symlink_cannot_be_used_as_checkpoint(tmp_path: Path) -> None:
    root = tmp_path / "versions"
    root.mkdir()
    link = root / "escape.RPP"
    try:
        link.symlink_to(tmp_path / "not-created.RPP")
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create file symlinks without privileges")
    assert not link.exists()
    with pytest.raises(ValidationError) as raised:
        checkpoint_path(link, root=root)
    assert "already exists" in str(raised.value)


def test_existing_file_inside_the_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "versions"
    root.mkdir()
    target = root / "taken.RPP"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError) as raised:
        checkpoint_path(target, root=root)
    assert "already exists" in str(raised.value)
