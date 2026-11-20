"""Reference import: a file or URL becomes a converted, measured WAV.

The conversion tests need ffmpeg and skip when it is not installed.  The
discovery, path validation and error-code tests never call an external binary.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

import reaper_audio.daw.reference as reference_module
from reaper_audio.daw.external_tools import ToolUnavailable, find_ffmpeg
from reaper_audio.daw.reference import (ReferenceConflict, ReferenceUnavailable,
                                       import_reference, measure_reference_profile,
                                       sanitize_label)
from reaper_audio.errors import ValidationError

try:
    FFMPEG = find_ffmpeg(required=False)
except ToolUnavailable:  # a stale environment variable must not break collection
    FFMPEG = None

requires_ffmpeg = pytest.mark.skipif(FFMPEG is None,
                                     reason="ffmpeg is not installed on this machine")
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _tone(path: Path, *, sample_rate: int = 44100, seconds: float = 2.0,
          amplitude: float = 0.2, channels: int = 2) -> Path:
    time = np.arange(int(seconds * sample_rate)) / sample_rate
    left = amplitude * np.sin(2 * np.pi * 440.0 * time)
    right = amplitude * np.sin(2 * np.pi * 660.0 * time)
    data = np.stack([left, right], axis=1) if channels == 2 else left[:, None]
    sf.write(str(path), data, sample_rate, subtype="FLOAT")
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@requires_ffmpeg
def test_file_import_converts_to_wav_and_reports_the_contract(tmp_path: Path) -> None:
    source = _tone(tmp_path / "source.wav")
    result = import_reference(source, label="reference", archive_root=tmp_path / "archive",
                              ffmpeg=FFMPEG)

    target = Path(result["path"])
    assert target == (tmp_path / "archive" / "references" / "reference.wav").absolute()
    assert target.is_file()
    assert result["ok"] is True
    assert result["operation"] == "reference.import"
    assert result["label"] == "reference"
    assert result["sha256"] == _sha256(target)
    assert len(result["sha256"]) == 64
    assert result["duration_s"] == pytest.approx(2.0, abs=1e-3)
    assert result["sample_rate"] == 48000
    assert result["channels"] == 2
    assert result["frames"] == 96000
    assert result["source_kind"] == "file"
    assert result["source_sha256"] == _sha256(source)
    assert result["reused"] is False
    assert result["conversion"]["level_change"] == 0.0


@requires_ffmpeg
def test_profile_uses_the_shared_analyzer_and_the_1_6_octave_grid(tmp_path: Path) -> None:
    # Four seconds so the loudness-range window is actually complete.
    source = _tone(tmp_path / "source.wav", seconds=4.0)
    result = import_reference(source, archive_root=tmp_path / "archive", ffmpeg=FFMPEG)
    profile = result["profile"]

    direct = measure_reference_profile(result["path"])
    for key in ("lufs", "lra", "peak_dbfs", "true_peak_dbtp", "crest_db",
                "centroid_hz", "side_mid_db"):
        assert profile[key] == pytest.approx(direct[key], abs=1e-9)
        assert profile[key] is not None
    assert profile["bands"]["kind"] == "1_6_octave"
    assert len(profile["bands"]["centers_hz"]) == 28
    assert len(profile["bands"]["levels_dbfs"]) == 28
    assert profile["provenance"]["analyzer"] == "analysis.metrics.measure_audio"
    assert profile["provenance"]["sample_rate"] == 48000
    assert profile["provenance"]["channels"] == 2


@requires_ffmpeg
def test_a_compressed_source_is_decoded_and_converted(tmp_path: Path) -> None:
    from reaper_audio.daw.encode import encode

    tone = _tone(tmp_path / "tone.wav")
    compressed = encode(tone, out_path=tmp_path / "reference.mp3", ffmpeg=FFMPEG)["path"]
    result = import_reference(compressed, archive_root=tmp_path / "archive",
                              profile=False, ffmpeg=FFMPEG)
    assert Path(result["path"]).suffix == ".wav"
    assert result["source_format"]["sample_rate"] == 44100
    assert result["sample_rate"] == 48000
    assert result["duration_s"] == pytest.approx(2.0, abs=0.05)


@requires_ffmpeg
def test_mono_source_becomes_the_requested_channel_count(tmp_path: Path) -> None:
    source = _tone(tmp_path / "mono.wav", channels=1)
    result = import_reference(source, archive_root=tmp_path / "archive",
                              channels=2, ffmpeg=FFMPEG)
    assert result["source_format"]["channels"] == 1
    assert result["channels"] == 2


@requires_ffmpeg
def test_float_reference_above_full_scale_is_not_clipped_by_conversion(
        tmp_path: Path) -> None:
    source = _tone(tmp_path / "hot.wav", amplitude=1.2)
    result = import_reference(source, archive_root=tmp_path / "archive",
                              profile=True, ffmpeg=FFMPEG)
    assert result["profile"]["peak_dbfs"] == pytest.approx(20 * np.log10(1.2), abs=0.05)
    assert result["profile"]["peak_dbfs"] > 0.0


@requires_ffmpeg
def test_the_label_follows_the_sanitized_name(tmp_path: Path) -> None:
    source = _tone(tmp_path / "source.wav")
    result = import_reference(source, label="My:Ref/01", archive_root=tmp_path / "archive",
                              ffmpeg=FFMPEG)
    assert result["label"] == sanitize_label("My:Ref/01")
    assert "/" not in result["label"] and ":" not in result["label"]
    assert Path(result["path"]).stem == result["label"]


@requires_ffmpeg
def test_reimporting_the_same_file_reuses_it(tmp_path: Path) -> None:
    source = _tone(tmp_path / "source.wav")
    first = import_reference(source, label="same", archive_root=tmp_path / "archive",
                             profile=False, ffmpeg=FFMPEG)
    second = import_reference(source, label="same", archive_root=tmp_path / "archive",
                              profile=False, ffmpeg=FFMPEG)
    assert second["path"] == first["path"]
    assert second["sha256"] == first["sha256"]
    assert second["reused"] is True


@requires_ffmpeg
def test_a_different_file_under_the_same_label_is_refused(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    first = import_reference(_tone(tmp_path / "one.wav", amplitude=0.2),
                             label="collision", archive_root=archive,
                             profile=False, ffmpeg=FFMPEG)
    with pytest.raises(ReferenceConflict) as error:
        import_reference(_tone(tmp_path / "two.wav", amplitude=0.3),
                         label="collision", archive_root=archive,
                         profile=False, ffmpeg=FFMPEG)
    assert error.value.code == "reference_label_conflict"
    assert error.value.details["existing_sha256"] == first["sha256"]

    replaced = import_reference(_tone(tmp_path / "two.wav", amplitude=0.3),
                                label="collision", archive_root=archive,
                                profile=False, overwrite=True, ffmpeg=FFMPEG)
    assert replaced["sha256"] != first["sha256"]


@requires_ffmpeg
def test_a_url_uses_the_injected_downloader_and_its_title(tmp_path: Path) -> None:
    fixture = _tone(tmp_path / "downloaded.wav")

    def downloader(url: str, destination: Path) -> dict:
        copied = destination / "payload.wav"
        shutil.copyfile(fixture, copied)
        return {"path": copied, "title": "Live: Ref?", "extractor": "fake-site"}

    result = import_reference("https://example.invalid/watch?v=1",
                              archive_root=tmp_path / "archive",
                              downloader=downloader, profile=False, ffmpeg=FFMPEG)
    assert result["source_kind"] == "url"
    assert result["source_extractor"] == "fake-site"
    assert result["label"] == sanitize_label("Live: Ref?")
    assert ":" not in result["label"] and "?" not in result["label"]
    assert Path(result["path"]).is_file()
    assert result["source_sha256"] == _sha256(fixture)


@requires_ffmpeg
def test_a_url_uses_the_yt_dlp_module_when_it_is_installed(tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch
                                                           ) -> None:
    fixture = _tone(tmp_path / "module-payload.wav")

    class FakeYoutubeDL:
        def __init__(self, options: dict) -> None:
            self.options = options

        def __enter__(self) -> "FakeYoutubeDL":
            return self

        def __exit__(self, *exc_info) -> bool:
            return False

        def extract_info(self, url: str, download: bool = True) -> dict:
            destination = Path(self.options["outtmpl"]).parent
            shutil.copyfile(fixture, destination / "payload.wav")
            return {"title": "Module Ref", "id": "abc", "extractor_key": "Fake"}

    class FakeModule:
        YoutubeDL = FakeYoutubeDL

    monkeypatch.setattr(reference_module, "load_yt_dlp_module", lambda: FakeModule)
    result = import_reference("https://example.invalid/watch?v=3",
                              archive_root=tmp_path / "archive",
                              profile=False, ffmpeg=FFMPEG)
    assert result["source_kind"] == "url"
    assert result["label"] == "Module Ref"
    assert result["source_extractor"] == "Fake"


@requires_ffmpeg
def test_a_url_without_yt_dlp_is_a_coded_error(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reference_module, "load_yt_dlp_module", lambda: None)
    monkeypatch.setattr(reference_module, "find_yt_dlp", lambda *args, **kwargs: None)
    with pytest.raises(ToolUnavailable) as error:
        import_reference("https://example.invalid/watch?v=4",
                         archive_root=tmp_path / "archive", ffmpeg=FFMPEG)
    assert error.value.code == "tool_unavailable"
    assert error.value.details["tool"] == "yt-dlp"
    assert "download" in error.value.message


@requires_ffmpeg
def test_an_unreachable_url_is_a_coded_error(tmp_path: Path) -> None:
    def downloader(url: str, destination: Path) -> Path:
        raise RuntimeError("blocked by the site")

    with pytest.raises(ReferenceUnavailable) as error:
        import_reference("https://example.invalid/watch?v=2",
                         archive_root=tmp_path / "archive",
                         downloader=downloader, ffmpeg=FFMPEG)
    assert error.value.code == "reference_unavailable"
    assert error.value.details["reason"] == "blocked by the site"
    assert error.value.details["source"] == "https://example.invalid/watch?v=2"


def test_a_missing_file_is_a_validation_error(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as error:
        import_reference(tmp_path / "nope.wav", archive_root=tmp_path / "archive")
    assert error.value.code == "validation_error"
    assert error.value.details["path"].endswith("nope.wav")


def test_an_explicit_missing_ffmpeg_is_a_tool_error(tmp_path: Path) -> None:
    source = _tone(tmp_path / "source.wav")
    with pytest.raises(ToolUnavailable) as error:
        import_reference(source, archive_root=tmp_path / "archive",
                         ffmpeg=tmp_path / "missing-ffmpeg.exe")
    assert error.value.code == "tool_unavailable"
    assert error.value.details["tool"] == "ffmpeg"


@requires_ffmpeg
def test_profile_can_be_switched_off(tmp_path: Path) -> None:
    source = _tone(tmp_path / "source.wav")
    result = import_reference(source, archive_root=tmp_path / "archive",
                              profile=False, ffmpeg=FFMPEG)
    assert result["profile"] is None
