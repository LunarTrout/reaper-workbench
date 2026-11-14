"""Wave B live capture gate — skipped unless the operator arms it.

Nothing in this module runs by default.  It is the code half of
``docs/research/round-3/WAVE-B-LIVE-CHECKLIST.md`` and it never opens, saves or
closes a project on its own: the operator must already have a *disposable copy*
of the fixture open in a dedicated REAPER instance, and must point
``REAPER_WORKBENCH_FIXTURE`` at that copy's absolute ``.rpp`` path.  The test
verifies that the active bridge project really is that copy before capturing.

Environment:

- ``REAPER_WORKBENCH_LIVE=1`` — arm the gate.
- ``REAPER_WORKBENCH_FIXTURE`` — absolute path of the disposable project copy.

The live gate now requires a fully covered digest: the bridge must hash the
master, automation, project render/playback settings and serialized FX state,
and ``digest_evidence`` must call the result ``full``.  Wave C closed the P0
master gap; this test now fails rather than xfail when the bridge regresses.

REAPER 7.84 on this host offlines take media while the application is
inactive, without dirtying the project.  This gate never activates REAPER and
never toggles media; if the live project reports offline sources, it fails
closed with the exact track/item/source rows so the operator can reopen the
fixture and run the gate again.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from reaper_audio.context.audio import (CaptureMethod, ChannelView, SourceKind,
                                        Stage)
from reaper_audio.context.revision import digest_evidence
from reaper_audio.daw.signal_gate import measure_wave
from reaper_audio.daw.session import ProjectSession
from reaper_audio.models import Section
from reaper_audio.reaper.bridge_client import ReaperBridge

LIVE = os.environ.get("REAPER_WORKBENCH_LIVE") == "1"
FIXTURE = os.environ.get("REAPER_WORKBENCH_FIXTURE")

pytestmark = pytest.mark.skipif(
    not LIVE,
    reason="live REAPER gate: set REAPER_WORKBENCH_LIVE=1 and "
           "REAPER_WORKBENCH_FIXTURE to a disposable project copy",
)

SECTION = Section(5.0, 6.0)
TAIL_MS = 250


def _assert_audible(path: str | Path, label: str) -> dict:
    content = measure_wave(path)
    assert content["audible"] is True, f"{label} is unexpectedly silent: {content}"
    assert content["peak_dbfs"] > -60.0, f"{label} has no meaningful peak: {content}"
    assert content["rms_dbfs"] > -60.0, f"{label} has no meaningful RMS: {content}"
    return content


def _assert_sources_online(context: dict) -> None:
    rows = [
        (track.get("name"), item.get("source_online"),
         item.get("source_length_s"), item.get("source_sample_rate"),
         item.get("source_channels"))
        for track in context["tracks"]
        for item in track.get("items") or []
    ]
    assert rows, "the disposable fixture has no take sources"
    offline = [row for row in rows
               if not all((row[1], row[2], row[3], row[4]))]
    assert not offline, (
        "the disposable fixture is open with unresolved take sources; reopen it "
        f"from the .rpp before running the live capture gate: {offline}"
    )


def _normalized(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _find_open_project(bridge: ReaperBridge, fixture: Path) -> str:
    wanted = _normalized(fixture)
    projects = bridge.list_projects(timeout_s=5.0)["projects"]
    for entry in projects:
        revision = entry.get("revision") or {}
        path = revision.get("path")
        if isinstance(path, str) and path and _normalized(path) == wanted:
            return revision["project_id"]
    pytest.fail(
        "The disposable fixture is not open in this bridge. Open exactly "
        f"{fixture} in a dedicated REAPER instance first; this gate never opens "
        "a project because doing so could discard unsaved user work."
    )




def test_live_capture_of_the_disposable_copy(tmp_path: Path) -> None:
    assert FIXTURE, "REAPER_WORKBENCH_FIXTURE must name the disposable .rpp copy"
    fixture = Path(FIXTURE).expanduser().resolve(strict=True)
    assert fixture.suffix.lower() == ".rpp"

    source_files = sorted((fixture.parent / "media").glob("*.wav"))
    assert len(source_files) == 6, (
        "the disposable fixture must provide six source WAVs under media/"
    )
    source_content = {}
    for source_file in source_files:
        source_content[source_file.name] = _assert_audible(
            source_file, f"source {source_file.name}")
    print("source_content:", source_content)

    bridge = ReaperBridge()
    health = bridge.health()
    project_id = _find_open_project(bridge, fixture)
    session = ProjectSession(bridge, project_id=project_id)

    digest_before = bridge.project_digest(project_id)
    evidence_before = digest_evidence(project_id, digest_before)
    context_before = bridge.project_context(project_id, include_items=True,
                                            include_fx_params=True)
    _assert_sources_online(context_before)
    track_guids_before = tuple(track["guid"] for track in context_before["tracks"])
    item_guids_before = tuple(
        item["guid"]
        for track in context_before["tracks"]
        for item in track.get("items") or []
    )
    settings_before = bridge.call("project.render_settings.read",
                                  {"project_id": project_id})

    with session.capture_audio(tmp_path / "artifacts", section=SECTION,
                               tail_ms=TAIL_MS) as captured:
        capture = captured["capture"]
        assert capture.status == "complete"
        assert len(capture.assets) == len(track_guids_before) + 1
        assert capture.tail_s == pytest.approx(TAIL_MS / 1000.0)
        assert capture.restore_report["restored"] is True
        assert list(capture.restore_report["failures"]) == []
        assert capture.render_settings["RENDER_ADDTOPROJ"] == 0.0
        assert capture.render_settings["RENDER_TAILMS"] == float(TAIL_MS)
        assert capture.method is CaptureMethod.native_render

        assets = captured["assets"]
        master_assets = [asset for asset in assets
                         if asset.stage is Stage.master_mix_post_fx]
        stem_assets = [asset for asset in assets
                       if asset.stage is Stage.track_stem_post_fx]
        assert len(master_assets) == 1
        assert len(stem_assets) == len(track_guids_before)
        expected_window_end = SECTION.end_s - SECTION.start_s + TAIL_MS / 1000.0

        entries = {entry["guid"]: entry
                   for entry in [*captured["tracks"], captured["master"]]}
        for asset in assets:
            assert asset.capture_method is CaptureMethod.native_render
            assert asset.source_kind is SourceKind.unknown
            assert asset.sha256 == hashlib.sha256(
                Path(entries[asset.source_guid]["capture"]["path"]).read_bytes()
            ).hexdigest()
            assert asset.asset_id == f"ast_sha256_{asset.sha256}"
            entry_format = entries[asset.source_guid]["capture"]["audio_format"]
            assert asset.sample_rate == entry_format["sample_rate"]
            assert asset.frames == entry_format["frames"]
            assert asset.duration_s == pytest.approx(entry_format["duration_s"])
            assert asset.duration_s == pytest.approx(expected_window_end,
                                                     abs=1.0 / asset.sample_rate)
            assert asset.window is not None
            assert asset.window.project_start_s == pytest.approx(SECTION.start_s)
            assert asset.window.project_end_s == pytest.approx(SECTION.end_s)
            assert asset.window.file_start_s == 0.0
            assert asset.window.file_end_s == pytest.approx(expected_window_end,
                                                            abs=1.0 / asset.sample_rate)
            expected_view = (ChannelView.native if asset.channels == 2
                             else ChannelView.mono_sum)
            assert asset.channel_view is expected_view

        content_by_guid = {}
        track_items = {track["guid"]: bool(track.get("items"))
                       for track in context_before["tracks"]}
        for asset in assets:
            path = entries[asset.source_guid]["capture"]["path"]
            content = measure_wave(path)
            content_by_guid[asset.source_guid] = content
            if asset.source_guid == context_before["master"]["guid"]:
                assert content["audible"] is True, "master render is silent"
            elif track_items.get(asset.source_guid):
                assert content["audible"] is True, (
                    f"track stem {asset.source_guid} is silent despite items")
        assert captured["master_validation"]["valid"] is True
        assert captured["master_validation"]["unexpected_silence"] is False
        assert captured["source_activity"]["audible_count"] >= 6
        print("asset_content:", content_by_guid)

        assert captured["time_window"]["project_end_s"] == pytest.approx(
            SECTION.end_s)
        assert captured["time_window"]["file_end_s"] == pytest.approx(
            expected_window_end, abs=1.0 / assets[0].sample_rate)
        job_directory = Path(captured["artifact_job"]["directory"])

    assert not job_directory.exists()
    assert captured["artifact_job"]["cleanup"]["removed_job_directory"] is True

    digest_after = bridge.project_digest(project_id)
    evidence_after = digest_evidence(project_id, digest_after)
    # A render plus restore must not change a single measured sound-affecting
    # field; the digest, not the counter, proves it.
    assert evidence_after.fingerprint == evidence_before.fingerprint

    context_after = bridge.project_context(project_id, include_items=True,
                                           include_fx_params=True)
    assert tuple(track["guid"] for track in context_after["tracks"]) == \
        track_guids_before
    assert tuple(
        item["guid"]
        for track in context_after["tracks"]
        for item in track.get("items") or []
    ) == item_guids_before
    _assert_sources_online(context_after)

    settings_after = bridge.call("project.render_settings.read",
                                 {"project_id": project_id})
    for key in ("render_settings", "render_targets", "dirty"):
        assert settings_after[key] == settings_before[key]

    # Record the evidence the operator must paste into the gate report.
    print("reaper_version:", health.get("reaper_version"))
    print("project_id:", project_id)
    print("capture_id:", capture.capture_id)
    print("digest_scope:", evidence_before.scope)
    print("digest_covered:", evidence_before.covered)
    print("digest_missing:", evidence_before.missing)
    print("asset_hashes:", {asset.source_guid: asset.sha256 for asset in assets})


def test_live_capture_requires_online_media_sources() -> None:
    assert FIXTURE, "REAPER_WORKBENCH_FIXTURE must name the disposable .rpp copy"
    fixture = Path(FIXTURE).expanduser().resolve(strict=True)
    bridge = ReaperBridge()
    project_id = _find_open_project(bridge, fixture)
    context = bridge.project_context(project_id, include_items=True,
                                     include_fx_params=False)
    _assert_sources_online(context)


def test_live_accessor_capture_has_signal_and_allows_empty_tracks(
        tmp_path: Path) -> None:
    assert FIXTURE, "REAPER_WORKBENCH_FIXTURE must name the disposable .rpp copy"
    fixture = Path(FIXTURE).expanduser().resolve(strict=True)
    bridge = ReaperBridge()
    project_id = _find_open_project(bridge, fixture)
    session = ProjectSession(bridge, project_id=project_id)
    digest_before = bridge.project_digest(project_id)
    evidence_before = digest_evidence(project_id, digest_before)
    context_before = bridge.project_context(project_id, include_items=True,
                                            include_fx_params=True)
    _assert_sources_online(context_before)
    empty_guids = {track["guid"] for track in context_before["tracks"]
                   if not track.get("items")}
    settings_before = bridge.call("project.render_settings.read",
                                  {"project_id": project_id})

    with session.capture_audio(tmp_path / "artifacts",
                               section=SECTION, tail_ms=TAIL_MS,
                               capture_mode="accessor") as captured:
        capture = captured["capture"]
        assert capture.status == "complete"
        assert capture.method is CaptureMethod.audio_accessor
        assert len(capture.assets) == len(context_before["tracks"]) + 1
        assert capture.restore_report["restored"] is True
        assert list(capture.restore_report["failures"]) == []
        entries = {entry["guid"]: entry
                   for entry in [*captured["tracks"], captured["master"]]}
        master_guid = context_before["master"]["guid"]
        accessor_content = {}
        for asset in capture.assets:
            path = entries[asset.source_guid]["capture"]["path"]
            content = measure_wave(path)
            accessor_content[asset.source_guid] = content
            if asset.source_guid == master_guid:
                assert content["audible"] is True, "accessor capture master is silent"
            elif asset.source_guid in empty_guids:
                assert content["silent"] is True, (
                    "an empty track must be explicit silence, not a failed capture")
            else:
                assert content["audible"] is True, (
                    f"accessor stem {asset.source_guid} is silent")
        assert captured["master_validation"]["valid"] is True
        assert captured["master_validation"]["unexpected_silence"] is False
        job_directory = Path(captured["artifact_job"]["directory"])
        print("accessor_content:", accessor_content)
        print("accessor_empty_tracks:", sorted(empty_guids))

    assert not job_directory.exists()
    context_after = bridge.project_context(project_id, include_items=True,
                                           include_fx_params=True)
    _assert_sources_online(context_after)
    settings_after = bridge.call("project.render_settings.read",
                                 {"project_id": project_id})
    for key in ("render_settings", "render_targets", "dirty"):
        assert settings_after[key] == settings_before[key]
    digest_after = bridge.project_digest(project_id)
    evidence_after = digest_evidence(project_id, digest_after)
    assert evidence_after.fingerprint == evidence_before.fingerprint


def test_live_digest_discloses_master_track_coverage() -> None:
    assert FIXTURE, "REAPER_WORKBENCH_FIXTURE must name the disposable .rpp copy"
    fixture = Path(FIXTURE).expanduser().resolve(strict=True)
    bridge = ReaperBridge()
    project_id = _find_open_project(bridge, fixture)

    digest = bridge.project_digest(project_id)
    evidence = digest_evidence(project_id, digest)

    assert digest["coverage"].get("master") is True
    assert "master" not in digest["missing"]
    assert evidence.scope == "full"
    assert evidence.missing == ()
