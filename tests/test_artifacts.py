from __future__ import annotations

import json
from pathlib import Path

import pytest

from reaper_audio.analysis.artifacts import (
    AudioArtifactWorkspace,
    cleanup_audio_job,
    cleanup_stale_audio_jobs,
)
from reaper_audio.errors import ValidationError


def test_workspace_removes_only_registered_capture_files(tmp_path: Path) -> None:
    with AudioArtifactWorkspace(tmp_path, job_id="job-a") as workspace:
        captured = workspace.allocate("track-001.wav")
        captured.write_bytes(b"rendered")
        assert workspace.artifact_names == ("track-001.wav",)

    assert not captured.exists()
    assert not (tmp_path / "job-job-a").exists()


def test_workspace_retains_audio_for_a_short_resource_lease(tmp_path: Path) -> None:
    workspace = AudioArtifactWorkspace(tmp_path, job_id="mcp-audio")
    audio = workspace.allocate("track.wav")
    audio.write_bytes(b"temporary audio")
    retained_directory = workspace.retain()

    assert retained_directory == workspace.directory_real
    assert audio.exists()
    manifest = json.loads(workspace.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "retained"

    reports = cleanup_stale_audio_jobs(
        tmp_path, max_age_s=0, now_epoch=manifest["created_epoch"] + 1,
    )
    assert len(reports) == 1
    assert reports[0].removed_job_directory
    assert not audio.exists()


def test_cleanup_audio_job_removes_only_manifest_registered_files(tmp_path: Path) -> None:
    workspace = AudioArtifactWorkspace(tmp_path, job_id="mcp-role")
    audio = workspace.allocate("master.wav")
    audio.write_bytes(b"temporary audio")
    unrelated = workspace.directory / "note.txt"
    unrelated.write_text("keep", encoding="utf-8")
    workspace.retain()

    report = cleanup_audio_job(tmp_path, "mcp-role")

    assert report is not None
    assert report.removed_files == ("master.wav",)
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert not report.removed_job_directory

    unrelated.unlink()
    assert cleanup_audio_job(tmp_path, "mcp-role").removed_job_directory


def test_workspace_preserves_unregistered_file_and_reports_it(tmp_path: Path) -> None:
    workspace = AudioArtifactWorkspace(tmp_path, job_id="job-b")
    captured = workspace.allocate("master.wav")
    captured.write_bytes(b"rendered")
    unrelated = workspace.directory / "user-note.txt"
    unrelated.write_text("keep", encoding="utf-8")

    report = workspace.close()

    assert not captured.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert not report.removed_job_directory
    assert str(unrelated) in report.preserved_paths
    assert (workspace.directory / ".reaper-audio-job.json").exists()


@pytest.mark.parametrize("filename", ["../escape.wav", "a/b.wav", "a\\b.wav", ".."])
def test_workspace_rejects_non_local_artifact_names(tmp_path: Path, filename: str) -> None:
    workspace = AudioArtifactWorkspace(tmp_path, job_id="job-c")
    with pytest.raises(ValidationError):
        workspace.allocate(filename)
    workspace.close()


def test_allocate_rejects_existing_path(tmp_path: Path) -> None:
    workspace = AudioArtifactWorkspace(tmp_path, job_id="job-d")
    existing = workspace.directory / "master.wav"
    existing.write_bytes(b"do not replace")
    with pytest.raises(ValidationError):
        workspace.allocate("master.wav")
    workspace.close()
    assert existing.read_bytes() == b"do not replace"


def test_workspace_registers_host_created_render_files_for_cleanup(
    tmp_path: Path,
) -> None:
    workspace = AudioArtifactWorkspace(tmp_path, job_id="host-render")
    track = workspace.directory / "track-01-overheads midside.wav"
    master = workspace.directory / "master.wav"
    track.write_bytes(b"track render")
    master.write_bytes(b"master render")

    registered = workspace.register_existing_files([track, master])
    manifest = json.loads(workspace.manifest_path.read_text(encoding="utf-8"))

    assert registered == (track.resolve(), master.resolve())
    assert manifest["artifact_names"] == ["track-01-overheads midside.wav", "master.wav"]
    report = workspace.close()
    assert set(report.removed_files) == {"track-01-overheads midside.wav", "master.wav"}
    assert report.removed_job_directory
    assert not track.exists()
    assert not master.exists()


def test_register_existing_files_rejects_outside_file_without_manifest_change(
    tmp_path: Path,
) -> None:
    workspace = AudioArtifactWorkspace(tmp_path, job_id="outside-render")
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"keep")

    with pytest.raises(ValidationError):
        workspace.register_existing_files([outside])

    manifest = json.loads(workspace.manifest_path.read_text(encoding="utf-8"))
    assert manifest["artifact_names"] == []
    assert outside.read_bytes() == b"keep"
    workspace.close()


def test_stale_cleanup_requires_own_manifest_and_preserves_unknown_files(
    tmp_path: Path,
) -> None:
    stale = tmp_path / "job-stale"
    stale.mkdir()
    owned = stale / "stem.wav"
    owned.write_bytes(b"temp")
    marker = stale / "project.rpp"
    marker.write_bytes(b"user project")
    (stale / ".reaper-audio-job.json").write_text(json.dumps({
        "schema": 1,
        "owner": "reaper-audio-library",
        "job_id": "stale",
        "status": "active",
        "created_epoch": 100,
        "artifact_names": ["stem.wav"],
    }), encoding="utf-8")

    reports = cleanup_stale_audio_jobs(tmp_path, max_age_s=10, now_epoch=1000)

    assert len(reports) == 1
    assert not owned.exists()
    assert marker.read_bytes() == b"user project"
    assert not reports[0].removed_job_directory
    assert marker.exists()


def test_stale_cleanup_skips_invalid_or_fresh_manifests(tmp_path: Path) -> None:
    invalid = tmp_path / "job-invalid"
    invalid.mkdir()
    (invalid / ".reaper-audio-job.json").write_text("{}", encoding="utf-8")
    fresh = tmp_path / "job-fresh"
    fresh.mkdir()
    owned = fresh / "stem.wav"
    owned.write_bytes(b"temp")
    (fresh / ".reaper-audio-job.json").write_text(json.dumps({
        "schema": 1,
        "owner": "reaper-audio-library",
        "job_id": "fresh",
        "created_epoch": 995,
        "artifact_names": ["stem.wav"],
    }), encoding="utf-8")

    reports = cleanup_stale_audio_jobs(tmp_path, max_age_s=10, now_epoch=1000)

    assert reports == ()
    assert invalid.exists()
    assert owned.exists()


def test_stale_cleanup_reclaims_only_job_prefixed_orphan_renders(
    tmp_path: Path,
) -> None:
    job = tmp_path / "job-crashed-render"
    job.mkdir()
    orphan_render = job / "rali-crashed-render-00-Master.wav"
    unrelated_render = job / "other-master.wav"
    project_file = job / "session.rpp"
    orphan_render.write_bytes(b"orphan render")
    unrelated_render.write_bytes(b"unregistered audio")
    project_file.write_bytes(b"project data")
    (job / ".reaper-audio-job.json").write_text(json.dumps({
        "schema": 1,
        "owner": "reaper-audio-library",
        "job_id": "crashed-render",
        "status": "active",
        "created_epoch": 100,
        "artifact_names": [],
    }), encoding="utf-8")

    reports = cleanup_stale_audio_jobs(tmp_path, max_age_s=10, now_epoch=1000)

    assert len(reports) == 1
    assert reports[0].removed_files == (orphan_render.name,)
    assert not orphan_render.exists()
    assert unrelated_render.exists()
    assert project_file.exists()
    assert not reports[0].removed_job_directory


def test_stale_cleanup_leaves_fresh_orphan_render_for_live_lease(
    tmp_path: Path,
) -> None:
    job = tmp_path / "job-live-render"
    job.mkdir()
    orphan_render = job / "rali-live-render-00-Master.wav"
    orphan_render.write_bytes(b"new render")
    (job / ".reaper-audio-job.json").write_text(json.dumps({
        "schema": 1,
        "owner": "reaper-audio-library",
        "job_id": "live-render",
        "status": "active",
        "created_epoch": 995,
        "artifact_names": [],
    }), encoding="utf-8")

    reports = cleanup_stale_audio_jobs(tmp_path, max_age_s=10, now_epoch=1000)

    assert reports == ()
    assert orphan_render.exists()


def test_stale_cleanup_retries_after_unregistered_file_is_removed(tmp_path: Path) -> None:
    job = tmp_path / "job-retry"
    job.mkdir()
    (job / "stem.wav").write_bytes(b"temp")
    unrelated = job / "unregistered.txt"
    unrelated.write_text("keep until caller handles it", encoding="utf-8")
    (job / ".reaper-audio-job.json").write_text(json.dumps({
        "schema": 1,
        "owner": "reaper-audio-library",
        "job_id": "retry",
        "created_epoch": 100,
        "artifact_names": ["stem.wav"],
    }), encoding="utf-8")

    first = cleanup_stale_audio_jobs(tmp_path, max_age_s=10, now_epoch=1000)
    assert len(first) == 1
    assert not (job / "stem.wav").exists()
    assert (job / ".reaper-audio-job.json").exists()
    assert unrelated.exists()

    unrelated.unlink()
    second = cleanup_stale_audio_jobs(tmp_path, max_age_s=10, now_epoch=1000)
    assert len(second) == 1
    assert second[0].removed_job_directory
    assert not job.exists()


def test_cleanup_does_not_follow_registered_file_symlink(tmp_path: Path) -> None:
    workspace = AudioArtifactWorkspace(tmp_path, job_id="job-link")
    target = tmp_path / "outside.wav"
    target.write_bytes(b"keep")
    link = workspace.allocate("track.wav")
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable in this Windows test environment")

    report = workspace.close()

    assert target.read_bytes() == b"keep"
    assert link.is_symlink()
    assert not report.removed_job_directory
