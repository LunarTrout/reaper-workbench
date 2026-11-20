"""Encoded delivery: true peak is measured after the codec, gain follows it."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from reaper_audio.daw.delivery import measure_delivery
from reaper_audio.daw.encode import encode
from reaper_audio.daw.external_tools import ToolUnavailable, find_ffmpeg
from reaper_audio.errors import ValidationError

try:
    FFMPEG = find_ffmpeg(required=False)
except ToolUnavailable:  # a stale environment variable must not break collection
    FFMPEG = None

requires_ffmpeg = pytest.mark.skipif(FFMPEG is None,
                                     reason="ffmpeg is not installed on this machine")
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _tone(path: Path, *, sample_rate: int = 44100, seconds: float = 2.0,
          amplitude: float = 0.2) -> Path:
    time = np.arange(int(seconds * sample_rate)) / sample_rate
    signal = amplitude * np.sin(2 * np.pi * 997.0 * time + 0.3)
    sf.write(str(path), np.stack([signal, signal], axis=1), sample_rate, subtype="FLOAT")
    return path


@requires_ffmpeg
def test_encode_returns_the_contract_fields(tmp_path: Path) -> None:
    source = _tone(tmp_path / "source.wav", amplitude=0.2)
    result = encode(source, format="mp3", bitrate=320, ceiling_dbtp=-1.0,
                    ffmpeg=FFMPEG)

    target = Path(result["path"])
    assert target.is_file()
    assert target.suffix == ".mp3"
    assert result["ok"] is True
    assert result["operation"] == "render.encode"
    assert result["format"] == "mp3"
    assert result["bitrate"] == 320
    assert result["iterations"] == 1
    assert result["gain_applied"] == 0.0
    assert result["within_ceiling"] is True
    assert result["true_peak"] <= -1.0
    assert result["lufs"] < 0.0
    assert math.isfinite(result["lufs"])
    assert len(result["sha256"]) == 64
    assert result["file_size"] > 0
    assert result["sample_rate"] == 44100
    assert result["channels"] == 2
    assert result["history"][0]["iteration"] == 1
    assert result["measurement"]["stage"] == "encoded_output"


@requires_ffmpeg
def test_reported_true_peak_is_the_decoded_file(tmp_path: Path) -> None:
    source = _tone(tmp_path / "source.wav", amplitude=0.2)
    result = encode(source, ffmpeg=FFMPEG)
    independent = measure_delivery(result["path"])
    assert result["true_peak"] == pytest.approx(independent["true_peak_dbtp"], abs=0.15)
    assert result["lufs"] == pytest.approx(independent["lufs"], abs=0.15)


@requires_ffmpeg
def test_a_hot_source_is_gained_down_after_the_codec(tmp_path: Path) -> None:
    source = _tone(tmp_path / "hot.wav", amplitude=1.2)
    source_peak = measure_delivery(source)["true_peak_dbtp"]
    assert source_peak > 0.0

    result = encode(source, ceiling_dbtp=-1.0, ffmpeg=FFMPEG)
    assert result["iterations"] >= 2
    assert result["iterations"] <= 3
    assert result["gain_applied"] < 0.0
    assert result["true_peak"] <= -1.0
    assert result["within_ceiling"] is True
    assert result["overshoot_db"] == 0.0
    assert result["history"][0]["true_peak_dbtp"] > -1.0
    assert result["history"][-1]["gain_db"] == pytest.approx(result["gain_applied"])
    assert result["history"][-1]["true_peak_dbtp"] <= -1.0


@requires_ffmpeg
def test_the_ceiling_is_a_parameter(tmp_path: Path) -> None:
    source = _tone(tmp_path / "hot.wav", amplitude=1.2)
    at_minus_one = encode(source, ceiling_dbtp=-1.0, ffmpeg=FFMPEG)
    at_minus_six = encode(source, ceiling_dbtp=-6.0, ffmpeg=FFMPEG)
    assert at_minus_six["ceiling_dbtp"] == -6.0
    assert at_minus_six["true_peak"] <= -6.0
    assert at_minus_six["gain_applied"] < at_minus_one["gain_applied"]


@requires_ffmpeg
def test_encoding_the_same_source_twice_is_idempotent(tmp_path: Path) -> None:
    source = _tone(tmp_path / "hot.wav", amplitude=1.2)
    first = encode(source, out_path=tmp_path / "first.mp3", ffmpeg=FFMPEG)
    second = encode(source, out_path=tmp_path / "second.mp3", ffmpeg=FFMPEG)
    assert second["gain_applied"] == pytest.approx(first["gain_applied"], abs=1e-9)
    assert second["iterations"] == first["iterations"]
    assert second["lufs"] == pytest.approx(first["lufs"], abs=1e-6)
    assert second["true_peak"] == pytest.approx(first["true_peak"], abs=1e-6)
    assert second["sha256"] == first["sha256"]


@requires_ffmpeg
def test_bitrate_accepts_a_kilobit_string(tmp_path: Path) -> None:
    source = _tone(tmp_path / "source.wav", amplitude=0.1)
    result = encode(source, bitrate="192k", ffmpeg=FFMPEG)
    assert result["bitrate"] == 192
    assert Path(result["path"]).is_file()


def test_an_unknown_format_is_refused_before_any_binary_runs(tmp_path: Path) -> None:
    source = _tone(tmp_path / "source.wav")
    with pytest.raises(ValidationError) as error:
        encode(source, format="ogg", ffmpeg=tmp_path / "missing-ffmpeg.exe")
    assert "Unsupported encode format" in error.value.message
    assert "mp3" in error.value.details["allowed"]


def test_more_than_three_iterations_is_refused(tmp_path: Path) -> None:
    source = _tone(tmp_path / "source.wav")
    with pytest.raises(ValidationError) as error:
        encode(source, max_iterations=4, ffmpeg=tmp_path / "missing-ffmpeg.exe")
    assert error.value.details["limit"] == 3


def test_an_explicit_missing_ffmpeg_is_a_tool_error(tmp_path: Path) -> None:
    source = _tone(tmp_path / "source.wav")
    with pytest.raises(ToolUnavailable) as error:
        encode(source, ffmpeg=tmp_path / "missing-ffmpeg.exe")
    assert error.value.code == "tool_unavailable"
    assert error.value.details["tool"] == "ffmpeg"


def test_a_missing_source_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as error:
        encode(tmp_path / "nope.wav")
    assert error.value.code == "validation_error"


def test_encoding_a_wav_over_its_own_path_is_refused(tmp_path: Path) -> None:
    source = _tone(tmp_path / "source.wav")
    with pytest.raises(ValidationError) as error:
        encode(source, format="wav")
    assert "overwrite its source" in error.value.message


@requires_ffmpeg
def test_overwrite_false_refuses_an_existing_output(tmp_path: Path) -> None:
    source = _tone(tmp_path / "source.wav", amplitude=0.1)
    target = tmp_path / "fixed.mp3"
    encode(source, out_path=target, ffmpeg=FFMPEG)
    with pytest.raises(ValidationError) as error:
        encode(source, out_path=target, overwrite=False, ffmpeg=FFMPEG)
    assert error.value.details["path"] == str(target.absolute())
