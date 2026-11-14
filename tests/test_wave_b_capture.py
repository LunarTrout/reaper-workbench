"""Wave B lane 6: the capture contract through a fake bridge.

``ProjectSession.capture_audio`` is exercised end to end without REAPER.  The
fake bridge writes real PCM WAV files with the standard-library ``wave`` module
and answers ``project.context``, ``project.digest``, ``project.render_settings.read``,
``project.sample_tracks`` and ``project.capture_render`` with the response shape
the resident Lua bridge produces.

The assertions are about what the capture *claims*: typed assets, content
hashes, the project window versus the file window including the render tail,
manifest-owned cleanup, and the honest revision scope of a partial digest.
"""

from __future__ import annotations

import hashlib
import math
import struct
import wave
from pathlib import Path

import pytest

from reaper_audio.context.audio import (AudioAsset, Capture, CaptureMethod,
                                        ChannelView, SourceKind, Stage)
from reaper_audio.context.revision import same_content
from reaper_audio.daw.session import ProjectSession
from reaper_audio.daw.signal_gate import measure_wave
from reaper_audio.errors import (ArtifactCleanupError, BridgeUnavailable,
                                 CaptureMasterInvalid, MediaUnresolved)
from reaper_audio.models import Section
from _digest_payloads import FULL_COVERAGE, full_extra_fields
import reaper_audio.daw.session as session_module

PROJECT_ID = "reaper:wave-b-fixture"
PROJECT_PATH = "C:/wave-b/fixture.rpp"
SAMPLE_RATE = 48_000
TRACK_GUID = "{TRACK-A}"
MASTER_GUID = "{MASTER}"
ITEM_GUID = "{ITEM-A}"


def _write_wav(path: Path, *, duration_s: float, sample_rate: int = SAMPLE_RATE,
               channels: int = 2, tone_hz: float = 220.0) -> None:
    """Write deterministic 16-bit PCM with the standard library only."""
    frames = int(round(duration_s * sample_rate))
    amplitude = 4_000
    payload = bytearray()
    for index in range(frames):
        sample = int(amplitude * math.sin(
            2.0 * math.pi * tone_hz * index / sample_rate))
        payload.extend(struct.pack("<h", sample) * channels)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(bytes(payload))


class FakeCaptureBridge:
    """Bridge double with real files and a measured, versioned digest."""

    def __init__(self, *, partial_digest: bool = False,
                 drift: str | None = None) -> None:
        self.revision = {"project_id": PROJECT_ID, "change_count": 4,
                         "path": PROJECT_PATH, "instance_id": "pri_wave_b",
                         "epoch": 1,
                         "epoch_basis": "bridge_session_project_generation"}
        self.partial_digest = partial_digest
        self.drift = drift
        self.context_calls = 0
        self.calls: list[tuple[str, dict]] = []
        self.render_args: dict | None = None
        self.sampled_args: dict | None = None
        self.track_count_seen: list[int] = []
        self.item_count_seen: list[int] = []
        self.master = {
            "guid": MASTER_GUID, "index": -1, "name": "MASTER",
            "volume": 1.0, "pan": 0.0, "mute": False, "solo": 0,
            "fx": [{"guid": "{MASTER-FX}", "name": "Limiter"}],
        }
        self.tracks = [
            {"guid": TRACK_GUID, "index": 0, "name": "Guitar",
             "volume": 0.75, "pan": -0.2, "mute": False, "solo": 0,
             "fx": [{"guid": "{TRACK-FX}", "name": "EQ"}],
             "items": [{"guid": ITEM_GUID, "position_s": 0.0,
                        "length_s": 1.0, "name": "take 1"}]},
        ]

    # -- context ---------------------------------------------------------
    def project_context(self, project_id: str | None = None, **_: object) -> dict:
        self.context_calls += 1
        if self.context_calls == 2:
            if self.drift == "track":
                self.tracks = [*self.tracks, {
                    "guid": "{TRACK-B}", "index": 1, "name": "Bass",
                    "volume": 1.0, "pan": 0.0, "mute": False, "solo": 0,
                    "fx": [], "items": []}]
            elif self.drift == "item":
                self.tracks[0]["items"] = [*self.tracks[0]["items"], {
                    "guid": "{ITEM-B}", "position_s": 0.5,
                    "length_s": 0.5, "name": "take 2"}]
        self.track_count_seen.append(len(self.tracks))
        self.item_count_seen.append(sum(len(track["items"]) for track in self.tracks))
        return {
            "revision": dict(self.revision),
            "path": self.revision["path"],
            "name": "Wave B Fixture",
            "track_count": len(self.tracks),
            "tracks": [dict(track, items=list(track["items"]))
                       for track in self.tracks],
            "master": dict(self.master),
            "selection": {"track_guids": []},
            "tempo": {"bpm": 120.0, "numerator": 4, "denominator": 4,
                      "markers": []},
            "transport": {"play_state": 0, "position_s": 0.0},
            "length_s": 1.0,
        }

    # -- digest ----------------------------------------------------------
    def project_digest(self, project_id: str | None = None) -> dict:
        coverage = {name: True for name in FULL_COVERAGE}
        coverage["envelopes"] = not self.partial_digest
        missing = ["envelopes"] if self.partial_digest else []
        tracks: list[dict] = []
        items: list[dict] = []
        for track in self.tracks:
            tracks.append({
                "guid": track["guid"], "volume": track["volume"],
                "pan": track["pan"], "mute": track["mute"],
                "solo": track["solo"], "folder_depth": 0,
                "fx": [{"guid": fx["guid"], "name": fx["name"],
                        "enabled": True, "offline": False,
                        "params": [{"index": 0, "normalized": 0.5}]}
                       for fx in track["fx"]],
                "sends": [],
            })
            for item in track["items"]:
                items.append({
                    "guid": item["guid"], "track_guid": track["guid"],
                    "position_s": item["position_s"],
                    "length_s": item["length_s"],
                    "start_offset_s": 0.0, "playrate": 1.0, "mute": False,
                    "fade_in_s": 0.0, "fade_out_s": 0.0, "take_guid": "",
                    "source_path": "", "loop_source": False,
                    "beat_attach_mode": 0, "stretch_marker_positions": [],
                })
        return {
            "project_id": PROJECT_ID,
            "path": self.revision["path"],
            "change_count": self.revision["change_count"],
            "digest_version": 1,
            "coverage": coverage,
            "missing": missing,
            "fields": {"tracks": tracks, "items": items,
                       "tempo": [{"time_s": 0.0, "bpm": 120.0,
                                  "numerator": 4, "denominator": 4,
                                  "linear": False}],
                       **full_extra_fields([track["guid"] for track in self.tracks],
                                           self.master["guid"])},
        }

    # -- render ----------------------------------------------------------
    def capture_render(self, *, expected_revision: dict, output_dir: str,
                       job_id: str, expected_track_count: int,
                       timeout_s: float = 600.0,
                       section_start_s: float | None = None,
                       section_end_s: float | None = None,
                       master_only: bool = False, channels: str = "stereo",
                       sample_rate: int | None = None,
                       tail_ms: int = 0) -> dict:
        self.render_args = {
            "expected_revision": dict(expected_revision),
            "output_dir": str(output_dir), "job_id": job_id,
            "expected_track_count": expected_track_count,
            "timeout_s": timeout_s, "section_start_s": section_start_s,
            "section_end_s": section_end_s, "master_only": master_only,
            "channels": channels, "sample_rate": sample_rate,
            "tail_ms": tail_ms,
        }
        before = dict(self.revision)
        after = {**self.revision, "change_count": self.revision["change_count"] + 1}
        self.revision = after
        rate = sample_rate or SAMPLE_RATE
        duration = self._duration(section_start_s, section_end_s, tail_ms)
        channel_count = 1 if channels == "mono" else 2
        directory = Path(output_dir)
        files: list[dict] = []
        rendered_tracks = [] if master_only else list(self.tracks)
        for index, track in enumerate(rendered_tracks):
            path = directory / f"rali-{job_id}-{index + 1:02d}-{track['name']}.wav"
            _write_wav(path, duration_s=duration, sample_rate=rate,
                       channels=channel_count, tone_hz=220.0 + index * 40.0)
            files.append({"source_guid": track["guid"], "name": track["name"],
                          "track_index": index, "is_master": False,
                          "path": str(path), "bytes": path.stat().st_size})
        master_path = directory / f"rali-{job_id}-00-MASTER.wav"
        _write_wav(master_path, duration_s=duration, sample_rate=rate,
                   channels=channel_count, tone_hz=440.0)
        files.append({"source_guid": MASTER_GUID, "name": "MASTER",
                      "track_index": -1, "is_master": True,
                      "path": str(master_path),
                      "bytes": master_path.stat().st_size})
        return {
            "project_id": PROJECT_ID,
            "revision": before,
            "revision_after": after,
            "capture_point": ("native_render_master_only" if master_only
                              else "native_render_track_stems_plus_master"),
            "render_action": "File: Render project",
            "render_settings_used": {
                "RENDER_SETTINGS": 1.0,
                "RENDER_BOUNDSFLAG": 2.0 if section_start_s is not None else 1.0,
                "RENDER_CHANNELS": float(channel_count),
                "RENDER_SRATE": float(sample_rate or 0),
                "RENDER_TAILFLAG": 1.0 if tail_ms else 0.0,
                "RENDER_TAILMS": float(tail_ms),
                "RENDER_ADDTOPROJ": 0.0,
            },
            "channels": channel_count,
            "sample_rate": rate,
            "tail_s": tail_ms / 1000.0,
            "restore_report": {"restored": True, "failures": []},
            "files": files,
        }

    def sample_tracks(self, *, project_id: str, output_dir: str, job_id: str,
                      start_s: float, end_s: float,
                      sample_rate: int = 48_000,
                      timeout_s: float = 300.0) -> dict:
        self.sampled_args = {
            "project_id": project_id, "output_dir": str(output_dir),
            "job_id": job_id, "start_s": start_s, "end_s": end_s,
            "sample_rate": sample_rate, "timeout_s": timeout_s,
        }
        duration = end_s - start_s
        directory = Path(output_dir)
        files = []
        for index, track in enumerate(self.tracks):
            path = directory / f"rali-{job_id}-{index + 1:02d}-{track['name']}.wav"
            _write_wav(path, duration_s=duration, sample_rate=sample_rate,
                       channels=2, tone_hz=220.0 + index * 40.0)
            files.append({"source_guid": track["guid"], "name": track["name"],
                          "track_index": index, "is_master": False,
                          "path": str(path), "bytes": path.stat().st_size})
        return {"project_id": PROJECT_ID, "revision": dict(self.revision),
                "files": files, "capture_point": "native_audio_accessor_tracks",
                "sample_rate": sample_rate,
                "frame_count": int(round(duration * sample_rate))}

    # -- settings --------------------------------------------------------
    def call(self, operation: str, arguments: dict | None = None, **_: object) -> dict:
        self.calls.append((operation, dict(arguments or {})))
        if operation == "project.render_settings.read":
            return {"project_id": self.revision["project_id"],
                    "revision": dict(self.revision),
                    "render_settings": {"format": "wav", "bounds": "time"},
                    "render_targets": "C:/renders/old-target.wav",
                    "dirty": 0}
        raise AssertionError(f"unexpected bridge operation: {operation}")

    @staticmethod
    def _duration(section_start_s: float | None, section_end_s: float | None,
                  tail_ms: int) -> float:
        if section_start_s is None or section_end_s is None:
            base = 1.0
        else:
            base = section_end_s - section_start_s
        return base + tail_ms / 1000.0


def _project_windows(asset: AudioAsset, *, project_end_s: float,
                     file_end_s: float) -> None:
    assert asset.window is not None
    assert asset.window.project_start_s == 0.0
    assert asset.window.project_end_s == pytest.approx(project_end_s)
    assert asset.window.file_start_s == 0.0
    assert asset.window.file_end_s == pytest.approx(file_end_s)


def test_capture_audio_returns_typed_hashed_assets_with_project_and_file_windows(
        tmp_path: Path) -> None:
    bridge = FakeCaptureBridge()
    session = ProjectSession(bridge)

    with session.capture_audio(tmp_path / "artifacts", section=Section(0.0, 1.0),
                               tail_ms=250) as captured:
        assert captured["schema_version"] == 1
        assert captured["render_action"] == "File: Render project"
        assert captured["capture_point"] == "native_render_track_stems_plus_master"
        assert bridge.render_args["section_start_s"] == 0.0
        assert bridge.render_args["section_end_s"] == 1.0
        assert bridge.render_args["tail_ms"] == 250
        assert bridge.render_args["expected_revision"]["change_count"] == 4

        capture = captured["capture"]
        assert isinstance(capture, Capture)
        assert capture.method is CaptureMethod.native_render
        assert capture.status == "complete"
        assert capture.missing == ()
        assert capture.warnings == ()
        assert capture.tail_s == pytest.approx(0.25)
        assert capture.solo_active is False
        assert capture.coverage["expected"] == 2
        assert capture.coverage["assets"] == 2
        assert capture.coverage["digest_missing"] == []
        assert capture.coverage["digest"]["envelopes"] is True
        assert capture.restore_report == {"restored": True, "failures": []}
        assert capture.render_settings["RENDER_TAILMS"] == 250.0
        assert capture.render_settings["RENDER_ADDTOPROJ"] == 0.0
        assert capture.revision_before.counter == 4
        assert capture.revision_after is not None
        assert capture.revision_after.counter == 5
        assert capture.revision_before.fingerprint_scope == "full"
        # Rendering must not change project content: the measured digest before
        # and after the render has to prove exactly that.
        assert same_content(capture.revision_before, capture.revision_after) is True

        assets = captured["assets"]
        assert isinstance(assets, tuple)
        assert len(assets) == 2
        assert all(isinstance(asset, AudioAsset) for asset in assets)
        by_guid = {asset.source_guid: asset for asset in assets}
        track_asset = by_guid[TRACK_GUID]
        master_asset = by_guid[MASTER_GUID]

        assert track_asset.stage is Stage.track_stem_post_fx
        assert track_asset.capture_method is CaptureMethod.native_render
        assert track_asset.source_kind is SourceKind.unknown
        assert track_asset.channel_view is ChannelView.native
        assert master_asset.stage is Stage.master_mix_post_fx
        assert master_asset.capture_method is CaptureMethod.native_render
        assert master_asset.channel_view is ChannelView.native

        entries = {TRACK_GUID: captured["tracks"][0],
                   MASTER_GUID: captured["master"]}
        for guid, asset in by_guid.items():
            entry = entries[guid]
            file_path = Path(entry["capture"]["path"])
            assert file_path.is_file()
            measured = hashlib.sha256(file_path.read_bytes()).hexdigest()
            assert asset.sha256 == measured
            assert asset.asset_id == f"ast_sha256_{measured}"
            assert asset.bytes == file_path.stat().st_size
            assert asset.sample_rate == SAMPLE_RATE
            assert asset.frames == 60_000
            assert asset.duration_s == pytest.approx(1.25)
            assert asset.lease_id == captured["artifact_job"]["job_id"]
            _project_windows(asset, project_end_s=1.0, file_end_s=1.25)
            assert entry["capture"]["asset_id"] == asset.asset_id
            assert entry["capture"]["sha256"] == measured
            assert entry["capture"]["stage"] == str(asset.stage)
            assert entry["capture"]["capture_method_typed"] == str(asset.capture_method)
            assert entry["capture"]["source_kind"] == "unknown"
            assert entry["capture"]["channel_view"] == "native"
            assert entry["capture"]["time_window"] == captured["time_window"]

        assert captured["tracks"][0]["capture"]["capture_method"] == \
            "reaper_native_offline_render"
        assert captured["tracks"][0]["capture"]["signal_stage"] == \
            "reaper_rendered_track_stem"
        assert captured["master"]["capture"]["signal_stage"] == \
            "reaper_rendered_master_mix"
        assert captured["master"]["capture"]["capture_method"] == \
            "reaper_native_offline_render"

        assert captured["time_window"]["project_start_s"] == 0.0
        assert captured["time_window"]["project_end_s"] == pytest.approx(1.0)
        assert captured["time_window"]["file_end_s"] == pytest.approx(1.25)
        assert captured["time_window"]["requested_section"] == {
            "start_s": 0.0, "end_s": 1.0}

        assert captured["capture_revision"]["fingerprint_scope"] == "full"
        assert captured["project"]["revision"]["change_count"] == 5
        job_directory = Path(captured["artifact_job"]["directory"])
        assert job_directory.is_dir()

    assert not job_directory.exists()
    cleanup = captured["artifact_job"]["cleanup"]
    assert cleanup["retained"] is False
    assert cleanup["removed_job_directory"] is True
    assert len(cleanup["removed_files"]) == 2
    # The capture only rendered and restored: no track or item was added.
    assert bridge.track_count_seen == [1, 1]
    assert bridge.item_count_seen == [1, 1]
    assert [operation for operation, _ in bridge.calls] == [
        "project.render_settings.read", "project.render_settings.read"]


def test_capture_audio_preserves_an_unregistered_file_and_fails_cleanup(
        tmp_path: Path) -> None:
    bridge = FakeCaptureBridge()
    captured: dict | None = None

    with pytest.raises(ArtifactCleanupError) as raised:
        with ProjectSession(bridge).capture_audio(tmp_path / "artifacts") as captured:
            job_directory = Path(captured["artifact_job"]["directory"])
            stray = job_directory / "another-writer.wav"
            stray.write_bytes(b"not owned by this manifest" + bytes(32))
            assert stray.is_file()

    assert captured is not None
    cleanup = captured["artifact_job"]["cleanup"]
    assert cleanup["preserved_paths"] == [str(stray)]
    assert cleanup["removed_job_directory"] is False
    assert len(cleanup["removed_files"]) == 2
    assert raised.value.details["preserved_paths"] == [str(stray)]
    # The unowned file is untouched; the registered renders are gone.
    assert stray.read_bytes() == b"not owned by this manifest" + bytes(32)
    assert sorted(path.name for path in job_directory.iterdir()) == [
        ".reaper-audio-job.json", "another-writer.wav"]


@pytest.mark.parametrize("drift", ["track", "item"])
def test_capture_audio_refuses_project_growth_during_the_render(
        tmp_path: Path, drift: str) -> None:
    bridge = FakeCaptureBridge(drift=drift)
    root = tmp_path / "artifacts"

    with pytest.raises(BridgeUnavailable,
                       match="added or removed project objects") as raised:
        with ProjectSession(bridge).capture_audio(root, section=Section(0.0, 1.0)):
            pass

    changed = raised.value.details["changed"]
    if drift == "track":
        assert "track_count" in changed
        assert "track_guids" in changed
    else:
        assert "item_count" in changed
        assert "item_guids" in changed
    # The failed capture still cleans its own job directory.
    assert not list(root.glob("job-*"))


def test_capture_audio_reports_a_partial_digest_without_claiming_equality(
        tmp_path: Path) -> None:
    bridge = FakeCaptureBridge(partial_digest=True)

    with ProjectSession(bridge).capture_audio(
            tmp_path / "artifacts", section=Section(0.0, 1.0)) as captured:
        capture = captured["capture"]
        assert capture.status == "complete"
        assert "revision_digest_partial" in capture.warnings
        assert capture.coverage["digest_missing"] == ["envelopes"]
        assert capture.coverage["digest"]["envelopes"] is False
        # ``missing`` speaks about captured assets only; the digest gap is
        # reported separately instead of being hidden.
        assert capture.missing == ()
        assert capture.revision_before.fingerprint_scope == "coarse"
        assert capture.revision_after is not None
        assert capture.revision_after.fingerprint_scope == "coarse"
        assert captured["capture_revision"]["fingerprint_scope"] == "coarse"
        assert captured["capture_revision"]["fingerprint"].startswith("sha256:")
        assert same_content(capture.revision_before, capture.revision_after) is False


def test_accessor_capture_calls_take_sums_pre_fx_and_master_native(
        tmp_path: Path) -> None:
    bridge = FakeCaptureBridge()

    with ProjectSession(bridge).capture_audio(
            tmp_path / "artifacts", section=Section(0.0, 1.0),
            capture_mode="accessor") as captured:
        capture = captured["capture"]
        assert capture.method is CaptureMethod.audio_accessor
        assert capture.stages == (Stage.take_sum_pre_fx, Stage.master_mix_post_fx)
        assert bridge.render_args["master_only"] is True
        assert bridge.render_args["channels"] == "stereo"
        assert bridge.sampled_args["start_s"] == 0.0
        assert bridge.sampled_args["end_s"] == 1.0
        assert bridge.sampled_args["sample_rate"] == SAMPLE_RATE

        track_asset = next(asset for asset in captured["assets"]
                           if asset.source_guid == TRACK_GUID)
        master_asset = next(asset for asset in captured["assets"]
                            if asset.source_guid == MASTER_GUID)
        assert track_asset.stage is Stage.take_sum_pre_fx
        assert track_asset.capture_method is CaptureMethod.audio_accessor
        assert master_asset.stage is Stage.master_mix_post_fx
        assert master_asset.capture_method is CaptureMethod.native_render

        track_capture = captured["tracks"][0]["capture"]
        assert track_capture["capture_method"] == "reaper_audio_accessor"
        assert track_capture["capture_method_typed"] == "audio_accessor"
        assert track_capture["signal_stage"] == "immediately_pre_fx"
        assert track_capture["track_fx_included"] is False
        assert captured["master"]["capture"]["capture_method"] == \
            "reaper_native_offline_render"
        assert captured["master"]["capture"]["capture_method_typed"] == \
            "native_render"
        assert captured["capture_point"] == \
            "native_audio_accessor_pre_fx_tracks_plus_master_render"
        # No tail was requested, so every file is exactly the section.
        assert all(asset.duration_s == pytest.approx(1.0)
                   for asset in captured["assets"])
        _project_windows(track_asset, project_end_s=1.0, file_end_s=1.0)


def test_accessor_capture_honours_the_render_tail(
        tmp_path: Path) -> None:
    """Accessor stems use the same section + tail file window as the master."""
    bridge = FakeCaptureBridge()

    with ProjectSession(bridge).capture_audio(
            tmp_path / "artifacts", section=Section(0.0, 1.0),
            capture_mode="accessor", tail_ms=250) as captured:
        assert bridge.sampled_args["end_s"] == pytest.approx(1.25)
        assert all(asset.duration_s == pytest.approx(1.25)
                   for asset in captured["assets"])
        assert captured["time_window"]["file_end_s"] == pytest.approx(1.25)


def test_capture_rejects_a_silent_master_when_stems_are_audible(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A structurally valid silent master must not be accepted as audio."""
    bridge = FakeCaptureBridge()
    real_measure = session_module.measure_wave

    def measure_with_silent_master(path: str | Path) -> dict:
        result = real_measure(path)
        if "MASTER" in Path(path).name.upper():
            result = {**result, "peak_dbfs": -160.0, "rms_dbfs": -160.0,
                      "silent": True, "audible": False}
        return result

    monkeypatch.setattr(session_module, "measure_wave", measure_with_silent_master)
    with pytest.raises(CaptureMasterInvalid,
                       match="silent master while rendered source tracks were audible"):
        with ProjectSession(bridge).capture_audio(
                tmp_path / "artifacts", section=Section(0.0, 1.0)):
            pass


def test_online_source_capture_contains_audible_pcm(
        tmp_path: Path) -> None:
    """A structurally valid online capture must also contain real samples."""
    bridge = FakeCaptureBridge()

    with ProjectSession(bridge).capture_audio(
            tmp_path / "artifacts", section=Section(0.0, 1.0)) as captured:
        entries = {entry["guid"]: entry
                   for entry in [*captured["tracks"], captured["master"]]}
        for guid, entry in entries.items():
            content = measure_wave(Path(entry["capture"]["path"]))
            assert content["audible"] is True, f"{guid} is unexpectedly silent"


def test_capture_rejects_an_unresolved_source_before_render(
        tmp_path: Path) -> None:
    bridge = FakeCaptureBridge()
    bridge.tracks[0]["items"][0].update({
        "active_take": {"source": "C:/fixtures/Guitar.wav", "is_midi": False},
        "source_online": False,
        "source_length_s": 0.0,
        "source_sample_rate": 0,
        "source_channels": 1,
    })

    with pytest.raises(MediaUnresolved,
                       match="no decodable media"):
        with ProjectSession(bridge).capture_audio(
                tmp_path / "artifacts", section=Section(0.0, 1.0)):
            pass
    assert bridge.render_args is None


def test_capture_rejects_silent_render_with_active_audio_items(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """All-silent output is not accepted merely because every stem is silent."""
    bridge = FakeCaptureBridge()
    silent = {"frames": 60_000, "sample_rate": SAMPLE_RATE, "channels": 2,
              "peak_dbfs": -160.0, "rms_dbfs": -160.0,
              "silent": True, "audible": False}
    monkeypatch.setattr(session_module, "measure_wave", lambda path: dict(silent))

    with pytest.raises(CaptureMasterInvalid,
                       match="silent master"):
        with ProjectSession(bridge).capture_audio(
                tmp_path / "artifacts", section=Section(0.0, 1.0)):
            pass


def test_capture_accepts_intentional_silence_when_all_tracks_are_muted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Muted tracks are evidence-based expected silence, not a failed render."""
    bridge = FakeCaptureBridge()
    bridge.tracks[0]["mute"] = True
    silent = {"frames": 60_000, "sample_rate": SAMPLE_RATE, "channels": 2,
              "peak_dbfs": -160.0, "rms_dbfs": -160.0,
              "silent": True, "audible": False}
    monkeypatch.setattr(session_module, "measure_wave", lambda path: dict(silent))

    with ProjectSession(bridge).capture_audio(
            tmp_path / "artifacts", section=Section(0.0, 1.0)) as captured:
        assert captured["master_validation"]["expected_silence"] is True
        assert captured["master_validation"]["unexpected_silence"] is False
        assert "all_project_tracks_muted" in captured["master_validation"]["silence_reason"]


def test_capture_accepts_intentional_silence_with_no_active_audio_items(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge = FakeCaptureBridge()
    bridge.tracks[0]["items"] = []
    silent = {"frames": 60_000, "sample_rate": SAMPLE_RATE, "channels": 2,
              "peak_dbfs": -160.0, "rms_dbfs": -160.0,
              "silent": True, "audible": False}
    monkeypatch.setattr(session_module, "measure_wave", lambda path: dict(silent))

    with ProjectSession(bridge).capture_audio(
            tmp_path / "artifacts", section=Section(0.0, 1.0)) as captured:
        assert captured["master_validation"]["expected_silence"] is True
        assert "no_active_audio_sources" in captured["master_validation"]["silence_reason"]

