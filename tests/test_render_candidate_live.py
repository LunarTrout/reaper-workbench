"""Opt-in live parity gate for a plan-bound candidate master render.

Nothing here runs by default. The operator must already have a *disposable
copy* of the project open in a dedicated REAPER instance, and must set:

- ``REAPER_WORKBENCH_LIVE=1``
- ``REAPER_WORKBENCH_RENDER_COPY`` to that copy's absolute ``.rpp`` path

The test applies one small reversible track edit, renders the candidate master
for a short explicit section, then asks REAPER for the same master render with
the same settings and interval. It compares the files byte-for-byte first; if
containers differ it measures PCM level/peak/RMS and the maximum sample
difference and reports the reason instead of calling it equal by eye.

It never opens, saves, switches or closes a project. If the declared copy is not
the active bridge project, the gate fails with instructions.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from reaper_audio.analysis.audio import load_audio
from reaper_audio.daw.session import EditOperation, ProjectRevision, ProjectSession
from reaper_audio.models import Section
from reaper_audio.reaper.bridge_client import ReaperBridge

LIVE = os.environ.get("REAPER_WORKBENCH_LIVE") == "1"
COPY = os.environ.get("REAPER_WORKBENCH_RENDER_COPY")
pytestmark = pytest.mark.skipif(
    not (LIVE and COPY),
    reason="live candidate gate: set REAPER_WORKBENCH_LIVE=1 and "
           "REAPER_WORKBENCH_RENDER_COPY to a disposable project copy",
)


def _normalized(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_project(bridge: ReaperBridge, copy: Path) -> str:
    wanted = _normalized(copy)
    for entry in bridge.list_projects(timeout_s=5.0)["projects"]:
        revision = entry.get("revision") or {}
        path = revision.get("path")
        if isinstance(path, str) and path and _normalized(path) == wanted:
            return str(revision["project_id"])
    pytest.fail(
        "The declared disposable copy is not open in this bridge; open exactly "
        f"{copy} in a dedicated REAPER instance first. This gate never opens, "
        "switches or saves a project."
    )


def _pcm_delta(left: Path, right: Path) -> dict[str, float | int | str]:
    a = load_audio(str(left))
    b = load_audio(str(right))
    if a.sample_rate != b.sample_rate or a.channels != b.channels:
        return {"status": "incomparable",
                "sample_rate_a": a.sample_rate, "sample_rate_b": b.sample_rate,
                "channels_a": a.channels, "channels_b": b.channels}
    n = min(a.frames, b.frames)
    diff = np.asarray(a.samples[:n]) - np.asarray(b.samples[:n])
    peak = float(np.max(np.abs(diff))) if n else 0.0
    rms = float(np.sqrt(np.mean(np.square(diff)))) if n else 0.0
    return {"status": "compared", "frames_compared": n,
            "max_abs_sample_delta": peak, "delta_rms": rms}


def test_live_candidate_master_matches_reference_render(tmp_path: Path) -> None:
    assert COPY, "REAPER_WORKBENCH_RENDER_COPY is required"
    copy = Path(COPY).expanduser().resolve(strict=True)
    assert copy.suffix.lower() == ".rpp"

    bridge = ReaperBridge()
    health = bridge.health()
    assert health.get("ok") is True, health
    project_id = _find_project(bridge, copy)
    session = ProjectSession(bridge, project_id=project_id)

    context = session.snapshot(include_items=True)
    revision = ProjectRevision.from_dict(context["revision"])
    tracks = [row for row in context.get("tracks") or [] if row.get("guid")]
    assert tracks, "the disposable fixture has no tracks"
    track = tracks[0]
    old_volume = float(track.get("volume", 1.0))
    # A tiny reversible change proves the plan/revision binding; it is not a mix
    # recommendation and is undone in the finally block when possible.
    plan = session.plan(
        [EditOperation("track.set", str(track["guid"]),
                       {"volume": old_volume * 0.999})],
        reason="live candidate parity smoke", expected_revision=revision,
    )
    applied = session.apply(plan, dry_run=False)
    revision_after = ProjectRevision.from_dict(applied["revision_after"])
    section = Section(5.0, 6.0)

    try:
        candidate = session.render_candidate(
            plan, revision_after=revision_after, section=section,
            artifact_root=tmp_path / "candidate", channels="stereo",
            sample_rate=None, tail_ms=0,
        )
        assert candidate["audio"]["stage"] == "master_output_post_track_bus_master_fx"
        assert candidate["audio"]["source_kind"] == "rendered_master_mix"
        assert candidate["audio"]["audio_accessor_used"] is False
        assert candidate["provenance"]["master_fx_count"] >= 0
        assert candidate["provenance"]["routing_summary"]["track_count"] == len(tracks)

        reference_dir = tmp_path / "reference"
        reference_dir.mkdir()
        current = ProjectRevision.from_dict(candidate["revision_after_render"])
        reference = bridge.capture_render(
            expected_revision=current.to_dict(),
            output_dir=str(reference_dir), job_id="candidate-reference",
            expected_track_count=0, timeout_s=600.0,
            section_start_s=section.start_s, section_end_s=section.end_s,
            master_only=True, render_settings=0, channels="stereo",
            sample_rate=None, tail_ms=0,
        )
        files = reference.get("files") or []
        assert len(files) == 1 and files[0].get("is_master") is True
        reference_path = Path(str(files[0]["path"]))
        candidate_path = Path(str(candidate["audio"]["path"]))
        candidate_sha = _sha(candidate_path)
        reference_sha = _sha(reference_path)
        assert candidate_sha == candidate["audio"]["sha256"]
        if candidate_sha == reference_sha:
            print("candidate/reference: sha256 identical")
        else:
            delta = _pcm_delta(candidate_path, reference_path)
            print("candidate/reference delta:", delta, "candidate bytes:",
                  candidate_path.stat().st_size, "reference bytes:",
                  reference_path.stat().st_size)
            assert delta["status"] == "compared", delta
            assert float(delta["max_abs_sample_delta"]) <= 1e-6, (
                "candidate and reference master audio differ; do not call the "
                f"candidate stage verified: {delta}")
        print("candidate render_s:", candidate["render_s"],
              "duration_s:", candidate["duration_s"])
    finally:
        try:
            live = session.snapshot()
            current = ProjectRevision.from_dict(live["revision"])
            session.undo(expected_revision=current)
        except Exception as exc:  # noqa: BLE001 - disposable fixture, report
            print("live candidate undo not completed:", type(exc).__name__, str(exc))
