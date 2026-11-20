"""Encode a WAV and prove the true peak of the encoded file.

A sample peak measured before the encoder says nothing about the waveform a
decoder reconstructs: lossy codecs add ringing and can push inter-sample peaks
above the source.  This module therefore encodes first, decodes the encoded
file back to float PCM, and measures the result with the same loudness engine
the delivery stage uses.

If the decoded true peak is above the requested ceiling the gain is reduced by
the measured overshoot and the file is encoded again, from the original source,
so quantisation never accumulates.  The loop is bounded by
``max_iterations`` (three, per the delivery contract).  If the ceiling is still
not met after the last attempt the result says so with numbers; it never
pretends the ceiling was reached.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import soundfile as sf

from ..context.measurement_cache import sha256_file
from ..errors import ValidationError
from .delivery import measure_delivery
from .external_tools import find_ffmpeg, tail

#: The delivery contract allows at most three encode attempts.
MAX_ITERATIONS = 3
#: Default bitrate for lossy formats, in kbit/s.  Always a parameter.
DEFAULT_BITRATE = 320
#: Default ceiling, in dBTP.  Always a parameter.
DEFAULT_CEILING_DBTP = -1.0


@dataclass(frozen=True, slots=True)
class FormatSpec:
    """How one requested format maps to an ffmpeg encoder."""

    encoder: str
    suffix: str
    lossy: bool
    container_args: tuple[str, ...] = field(default_factory=tuple)


#: A codec table is not a mix decision: it says which encoder produces the
#: format the caller named, and nothing about how the audio should sound.
FORMATS: dict[str, FormatSpec] = {
    "mp3": FormatSpec("libmp3lame", ".mp3", True),
    "aac": FormatSpec("aac", ".m4a", True, ("-movflags", "+faststart")),
    "m4a": FormatSpec("aac", ".m4a", True, ("-movflags", "+faststart")),
    "opus": FormatSpec("libopus", ".opus", True),
    "flac": FormatSpec("flac", ".flac", False),
    "wav": FormatSpec("pcm_s24le", ".wav", False),
}


class EncodeFailed(ValidationError):
    """ffmpeg could not produce or decode the requested encoded file."""

    code = "encode_failed"


def _format_spec(value: str) -> tuple[str, FormatSpec]:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("format must be a non-empty string",
                              details={"format": value,
                                       "allowed": sorted(FORMATS)})
    name = value.strip().lower().lstrip(".")
    if name not in FORMATS:
        raise ValidationError("Unsupported encode format",
                              details={"format": value, "allowed": sorted(FORMATS)})
    return name, FORMATS[name]


def _bitrate_kbps(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValidationError("bitrate must be a positive integer or null",
                              details={"bitrate": value})
    if isinstance(value, str):
        text = value.strip().lower()
        if text.endswith("k"):
            text = text[:-1]
        if not text.isdigit():
            raise ValidationError("bitrate must look like 320, '320' or '320k'",
                                  details={"bitrate": value})
        number = int(text)
    elif isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    else:
        raise ValidationError("bitrate must be a positive integer or null",
                              details={"bitrate": value})
    if number <= 0:
        raise ValidationError("bitrate must be positive", details={"bitrate": value})
    return number


def _target_path(source: Path, spec: FormatSpec, out_path: str | os.PathLike[str] | None
                 ) -> Path:
    if out_path is None:
        target = source.with_suffix(spec.suffix)
    else:
        candidate = Path(out_path).expanduser()
        target = candidate / (source.stem + spec.suffix) if candidate.is_dir() else candidate
    target = target.absolute()
    if target.resolve() == source.resolve():
        raise ValidationError(
            "The encoded output would overwrite its source",
            details={"source": str(source), "output": str(target),
                     "next_action": "pass out_path with a different file name"},
        )
    return target


def _run(command: list[str], *, what: str, source: Path,
         timeout_s: float | None) -> subprocess.CompletedProcess:
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        raise EncodeFailed(f"ffmpeg did not finish while {what}",
                           details={"source": str(source), "timeout_s": timeout_s}) from exc
    if completed.returncode != 0:
        raise EncodeFailed(f"ffmpeg failed while {what}",
                           details={"source": str(source), "exit_code": completed.returncode,
                                    "stderr": tail(completed.stderr),
                                    "command_tail": command[-6:]})
    return completed


def _encode_once(source: Path, target: Path, *, ffmpeg: Path, spec: FormatSpec,
                 bitrate_kbps: int | None, gain_db: float, sample_rate: int | None,
                 channels: int | None, timeout_s: float | None) -> None:
    command = [str(ffmpeg), "-hide_banner", "-nostdin", "-y",
               "-i", str(source), "-vn", "-map_metadata", "-1"]
    if gain_db:
        command += ["-af", f"volume={gain_db:.6f}dB"]
    if sample_rate is not None:
        command += ["-ar", str(sample_rate)]
    if channels is not None:
        command += ["-ac", str(channels)]
    command += ["-c:a", spec.encoder]
    if spec.lossy and bitrate_kbps is not None:
        command += ["-b:a", f"{bitrate_kbps}k"]
    command += list(spec.container_args)
    command += [str(target)]
    _run(command, what="encoding", source=source, timeout_s=timeout_s)
    if not target.is_file() or target.stat().st_size == 0:
        raise EncodeFailed("ffmpeg returned success but wrote no audio",
                           details={"source": str(source), "output": str(target)})


def _decode_for_measurement(encoded: Path, target: Path, *, ffmpeg: Path,
                            timeout_s: float | None) -> dict[str, Any]:
    """Decode the encoded file with ffmpeg and measure the decoded PCM."""
    command = [str(ffmpeg), "-hide_banner", "-nostdin", "-y",
               "-i", str(encoded), "-vn", "-acodec", "pcm_f32le", str(target)]
    _run(command, what="decoding the encoded file for measurement",
         source=encoded, timeout_s=timeout_s)
    if not target.is_file() or target.stat().st_size == 0:
        raise EncodeFailed("ffmpeg decoded no audio from the encoded file",
                           details={"encoded": str(encoded)})
    return measure_delivery(target)


def _publish(encoded: Path, target: Path) -> None:
    """Copy the measured encode next to its final name and replace atomically."""
    staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(encoded, staging)
        os.replace(staging, target)
    except OSError as exc:
        raise EncodeFailed(
            "The encoded output could not be written",
            details={"target": str(target), "reason": str(exc),
                     "next_action": ("close the file in any player or DAW, or pass "
                                     "another out_path")},
        ) from exc
    finally:
        staging.unlink(missing_ok=True)


def encode(source: str | os.PathLike[str], *, format: str = "mp3",
           bitrate: int | str | None = DEFAULT_BITRATE,
           ceiling_dbtp: float = DEFAULT_CEILING_DBTP,
           out_path: str | os.PathLike[str] | None = None,
           max_iterations: int = MAX_ITERATIONS,
           tolerance_db: float = 0.0,
           sample_rate: int | None = None,
           channels: int | None = None,
           overwrite: bool = True,
           ffmpeg: str | os.PathLike[str] | None = None,
           timeout_s: float | None = None) -> dict[str, Any]:
    """Encode a WAV and report the measured true peak of the encoded result.

    ``gain_applied`` is the cumulative gain (dB) inserted before the encoder;
    it is zero when the first attempt already meets the ceiling.  ``iterations``
    is the number of encodes actually performed and ``history`` carries every
    measurement, including the attempts that overshot.
    """
    text = str(source).strip()
    if not isinstance(source, (str, os.PathLike)):
        raise ValidationError("An encode source must be a path",
                              details={"source_type": type(source).__name__})
    if not text:
        raise ValidationError("An encode source must not be empty")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).absolute()
    path = path.resolve()
    if not path.is_file():
        raise ValidationError("The encode source does not exist",
                              details={"path": str(path)})
    name, spec = _format_spec(format)
    bitrate_kbps = _bitrate_kbps(bitrate)
    if not spec.lossy:
        bitrate_kbps = None
    if isinstance(ceiling_dbtp, bool) or not isinstance(ceiling_dbtp, (int, float)):
        raise ValidationError("ceiling_dbtp must be a number",
                              details={"ceiling_dbtp": ceiling_dbtp})
    ceiling = float(ceiling_dbtp)
    if not math.isfinite(ceiling):
        raise ValidationError("ceiling_dbtp must be finite",
                              details={"ceiling_dbtp": ceiling_dbtp})
    if isinstance(tolerance_db, bool) or not isinstance(tolerance_db, (int, float)):
        raise ValidationError("tolerance_db must be a number",
                              details={"tolerance_db": tolerance_db})
    tolerance = float(tolerance_db)
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValidationError("tolerance_db must be finite and non-negative",
                              details={"tolerance_db": tolerance_db})
    if (isinstance(max_iterations, bool) or not isinstance(max_iterations, int)
            or not 1 <= max_iterations <= MAX_ITERATIONS):
        raise ValidationError(
            f"max_iterations must be an integer from 1 to {MAX_ITERATIONS}",
            details={"max_iterations": max_iterations, "limit": MAX_ITERATIONS})
    if sample_rate is not None and (isinstance(sample_rate, bool)
                                    or not isinstance(sample_rate, int)
                                    or sample_rate <= 0):
        raise ValidationError("sample_rate must be a positive integer or null",
                              details={"sample_rate": sample_rate})
    if channels is not None and (isinstance(channels, bool)
                                 or not isinstance(channels, int) or channels <= 0):
        raise ValidationError("channels must be a positive integer or null",
                              details={"channels": channels})
    if not isinstance(overwrite, bool):
        raise ValidationError("overwrite must be a boolean")
    if timeout_s is not None and (isinstance(timeout_s, bool)
                                  or not isinstance(timeout_s, (int, float))
                                  or timeout_s <= 0):
        raise ValidationError("timeout_s must be a positive number or null",
                              details={"timeout_s": timeout_s})
    try:
        source_info = sf.info(str(path))
    except Exception as exc:  # noqa: BLE001 - the decoder explains its refusal
        raise ValidationError("The encode source is not readable audio",
                              details={"path": str(path), "reason": str(exc)}) from exc
    if source_info.frames <= 0 or source_info.samplerate <= 0:
        raise ValidationError("The encode source contains no samples",
                              details={"path": str(path)})

    target = _target_path(path, spec, out_path)
    if target.exists() and not overwrite:
        raise ValidationError("The encoded output already exists",
                              details={"path": str(target),
                                       "next_action": "pass overwrite=true or another out_path"})
    target.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_path = find_ffmpeg(ffmpeg)
    assert ffmpeg_path is not None

    history: list[dict[str, Any]] = []
    gain_db = 0.0
    within_ceiling: bool | None = None
    measured: dict[str, Any] | None = None
    last_peak: float | None = None
    last_lufs: float | None = None
    workdir = Path(tempfile.mkdtemp(prefix="encode-"))
    try:
        for attempt in range(1, max_iterations + 1):
            encoded = workdir / f"encoded-{uuid.uuid4().hex}{spec.suffix}"
            decoded = workdir / f"decoded-{uuid.uuid4().hex}.wav"
            _encode_once(path, encoded, ffmpeg=ffmpeg_path, spec=spec,
                         bitrate_kbps=bitrate_kbps, gain_db=gain_db,
                         sample_rate=sample_rate, channels=channels,
                         timeout_s=timeout_s)
            measured = _decode_for_measurement(encoded, decoded,
                                               ffmpeg=ffmpeg_path,
                                               timeout_s=timeout_s)
            raw_peak = measured.get("true_peak_dbtp")
            raw_lufs = measured.get("lufs")
            peak_value = (float(raw_peak)
                          if isinstance(raw_peak, (int, float))
                          and math.isfinite(float(raw_peak)) else None)
            lufs_value = (float(raw_lufs)
                          if isinstance(raw_lufs, (int, float))
                          and math.isfinite(float(raw_lufs)) else None)
            decoded_info = sf.info(str(decoded))
            last_peak, last_lufs = peak_value, lufs_value
            history.append({
                "iteration": attempt,
                "gain_db": round(gain_db, 6),
                "lufs": None if lufs_value is None else round(lufs_value, 4),
                "true_peak_dbtp": (None if peak_value is None
                                   else round(peak_value, 4)),
                "encoded_bytes": encoded.stat().st_size,
                "decoded_frames": int(decoded_info.frames),
            })
            if peak_value is None:
                within_ceiling = None
                _publish(encoded, target)
                break
            if peak_value <= ceiling + tolerance:
                within_ceiling = True
                _publish(encoded, target)
                break
            within_ceiling = False
            if attempt == max_iterations:
                _publish(encoded, target)
                break
            # Reduce by exactly the measured overshoot; re-encoding starts from
            # the untouched source, so the correction is not cumulative coding
            # error but one gain value applied to the original waveform.
            gain_db += ceiling - peak_value
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    assert measured is not None
    final_info = sf.info(str(target))
    result: dict[str, Any] = {
        "ok": True,
        "operation": "render.encode",
        "path": str(target),
        "format": name,
        "bitrate": bitrate_kbps,
        "lufs": last_lufs,
        "true_peak": last_peak,
        "ceiling_dbtp": ceiling,
        "within_ceiling": within_ceiling,
        "overshoot_db": (round(max(0.0, last_peak - ceiling), 6)
                         if last_peak is not None else None),
        "gain_applied": round(gain_db, 6),
        "iterations": len(history),
        "history": history,
        "source": str(path),
        "source_sample_rate": int(source_info.samplerate),
        "source_channels": int(source_info.channels),
        "sample_rate": int(final_info.samplerate),
        "channels": int(final_info.channels),
        "file_size": target.stat().st_size,
        "sha256": sha256_file(target),
        "measurement": {
            "stage": "encoded_output",
            "decoder": "ffmpeg -> pcm_f32le",
            "method": "phonometry program_loudness (BS.1770 integrated, Annex 2 true peak)",
            "tolerance_db": tolerance,
        },
    }
    if last_peak is not None and within_ceiling is False:
        result["note"] = ("the ceiling was not reached within max_iterations; "
                          "the reported true peak is the last measured value")
    return result


__all__ = [
    "DEFAULT_BITRATE",
    "DEFAULT_CEILING_DBTP",
    "EncodeFailed",
    "FORMATS",
    "MAX_ITERATIONS",
    "encode",
]
