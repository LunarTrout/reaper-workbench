"""Import a reference track: a local file or a URL becomes a measured WAV.

The import is deliberately explicit about every transformation it performs:

* a URL is downloaded by ``yt-dlp`` (Python module first, executable second);
* any input is decoded and converted by ``ffmpeg`` to PCM WAV at the requested
  sample rate and channel count, with no level change;
* the result is written to ``<archive_root>/references/<label>.wav``;
* the metrics profile comes from the same ``measure_audio`` analyzer the rest
  of the library uses, so a reference is comparable with a project render.

Both external tools are discovered, never assumed (see ``external_tools``).
A missing tool, an unreachable URL and a label collision are typed errors with
codes — the module never substitutes a different file or a different program.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import soundfile as sf

from ..analysis.audio import load_audio
from ..analysis.metrics import ANALYSIS_VERSION, measure_audio
from ..context.measurement_cache import sha256_file
from ..errors import ReaperAudioError, ValidationError
from .external_tools import (ToolUnavailable, find_ffmpeg, find_yt_dlp,
                             load_yt_dlp_module, tail)

#: URL schemes treated as downloads; every other string is a filesystem path.
_URL_SCHEMES = ("http://", "https://")
#: Longest label kept in a file name, in characters.
MAX_LABEL_LENGTH = 120
#: Default PCM format of an imported reference.  Float preserves material
#: whose samples sit above full scale instead of clipping it during conversion.
DEFAULT_PCM_CODEC = "pcm_f32le"
#: Default conversion targets; both are parameters of every call.
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHANNELS = 2
#: Characters Windows does not allow in a file name, plus control characters.
_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class ReferenceUnavailable(ReaperAudioError):
    """The source could not be downloaded or is not readable audio."""

    code = "reference_unavailable"


class ReferenceConversionFailed(ReaperAudioError):
    """ffmpeg could not turn the source into the requested WAV."""

    code = "reference_conversion_failed"


class ReferenceConflict(ValidationError):
    """A different reference already occupies the requested label."""

    code = "reference_label_conflict"


@dataclass(frozen=True, slots=True)
class DownloadedSource:
    """One downloaded URL payload before conversion."""

    path: Path
    title: str | None = None
    extractor: str | None = None


Downloader = Callable[[str, Path], str | Path | Mapping[str, Any]]


def is_url(source: str) -> bool:
    """True for an HTTP(S) URL; every other string is treated as a path."""
    return isinstance(source, str) and source.strip().lower().startswith(_URL_SCHEMES)


def sanitize_label(value: str) -> str:
    """Turn a title into a portable file stem without silently dropping it."""
    text = _UNSAFE_FILENAME.sub("_", str(value))
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    text = text[:MAX_LABEL_LENGTH].rstrip(" .")
    if not text:
        return "reference"
    if text.split(".")[0].upper() in _WINDOWS_RESERVED:
        text = f"_{text}"
    return text


def resolve_archive_root(archive_root: str | os.PathLike[str] | None) -> Path:
    """Archive root from the argument, the project's environment, or the default."""
    raw = archive_root
    if raw is None:
        raw = os.environ.get("REAPER_CAPTURE_ARCHIVE") or "capture-archive"
    return Path(raw).expanduser().absolute()


def _try_probe_audio(path: Path) -> Any | None:
    """Header read for provenance; None when libsndfile cannot open the container.

    A downloaded ``.webm``/``.m4a`` may not be readable by libsndfile even
    though ffmpeg decodes it.  The conversion is the authority; this probe is
    only used to report the source format when it is available.
    """
    try:
        return sf.info(str(path))
    except Exception:  # noqa: BLE001 - fall back to ffmpeg for the conversion
        return None


def _probe_audio(path: Path, *, role: str) -> Any:
    try:
        info = sf.info(str(path))
    except Exception as exc:  # noqa: BLE001 - libsndfile explains its own refusal
        raise ReferenceUnavailable(
            f"The {role} is not a readable audio file",
            details={"path": str(path), "reason": str(exc),
                     "next_action": "pass an audio file or a URL yt-dlp can download"},
        ) from exc
    if info.frames <= 0 or info.samplerate <= 0 or info.channels <= 0:
        raise ReferenceUnavailable(
            f"The {role} contains no samples",
            details={"path": str(path), "frames": int(info.frames),
                     "sample_rate": int(info.samplerate),
                     "channels": int(info.channels)},
        )
    return info


def cached_reference_report(audio, path: str | os.PathLike[str], section,
                            archive_root: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Measure one reference through the content-addressed measurement cache.

    The key covers the file bytes, window, analyzer version and options, so a
    changed reference or a code change misses.  When ``archive_root`` is None
    the report is measured without caching.
    """
    from ..analysis.metrics import measure_audio
    from ..context.measurement_cache import MeasurementCache, sha256_file
    from ..models import MeasurementReport, MetricValue

    if archive_root is None:
        return measure_audio(audio, section=section)
    cache = MeasurementCache(archive_root, section=None if section is None else {
        "start_s": section.start_s, "end_s": section.end_s,
    }, options={"kind": "reference"})
    digest = sha256_file(path)
    key, hit = cache.lookup(path, stage="reference_file", fingerprint=digest)
    if hit is not None:
        stored = dict(hit["report"])
        stored["metrics"] = {
            name: MetricValue(**cell) if isinstance(cell, dict) else cell
            for name, cell in (stored.get("metrics") or {}).items()
        }
        return MeasurementReport(**stored)
    report = measure_audio(audio, section=section)
    cache.remember(key, stage="reference_file",
                   result={"analysis": report.to_dict(), "dsp_s": None, "decode_s": None},
                   path=path)
    return report


def _newest_download(directory: Path) -> Path | None:
    candidates = [path for path in directory.rglob("*")
                  if path.is_file() and not path.name.endswith(".part")
                  and path.stat().st_size > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def _download_with_module(url: str, dest_dir: Path, module) -> DownloadedSource:
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(dest_dir / "%(title).120s [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    try:
        with module.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as exc:  # noqa: BLE001 - yt-dlp failure is the reported fact
        raise ReferenceUnavailable(
            "yt-dlp could not download the URL",
            details={"source": url, "tool": "yt-dlp", "reason": str(exc),
                     "next_action": ("check the link, the network and whether the site "
                                     "still serves the item")},
        ) from exc
    if not isinstance(info, Mapping):
        raise ReferenceUnavailable("yt-dlp returned no item description",
                                   details={"source": url})
    if info.get("_type") in {"playlist", "multi_video"}:
        raise ReferenceUnavailable(
            "The URL resolves to a collection, not a single track",
            details={"source": url, "type": info.get("_type"), "title": info.get("title"),
                     "next_action": "pass the URL of one track"},
        )
    downloaded = _newest_download(dest_dir)
    if downloaded is None:
        raise ReferenceUnavailable("yt-dlp reported success but wrote no file",
                                   details={"source": url, "directory": str(dest_dir)})
    return DownloadedSource(path=downloaded, title=info.get("title") or info.get("id"),
                            extractor=str(info.get("extractor_key") or "yt-dlp"))


def _download_with_executable(url: str, dest_dir: Path, executable: Path,
                              timeout_s: float | None) -> DownloadedSource:
    command = [str(executable), "--no-playlist", "--no-warnings", "--no-progress",
               "-f", "bestaudio/best",
               "-o", str(dest_dir / "%(title)s.%(ext)s"), url]
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ReferenceUnavailable(
            "yt-dlp did not finish within the timeout",
            details={"source": url, "tool": "yt-dlp", "timeout_s": timeout_s},
        ) from exc
    if completed.returncode != 0:
        raise ReferenceUnavailable(
            "yt-dlp could not download the URL",
            details={"source": url, "tool": "yt-dlp", "exit_code": completed.returncode,
                     "stderr": tail(completed.stderr)},
        )
    downloaded = _newest_download(dest_dir)
    if downloaded is None:
        raise ReferenceUnavailable("yt-dlp reported success but wrote no file",
                                   details={"source": url, "directory": str(dest_dir)})
    return DownloadedSource(path=downloaded, title=downloaded.stem, extractor="yt-dlp")


def _download(url: str, dest_dir: Path, *, downloader: Downloader | None,
              yt_dlp: str | os.PathLike[str] | None,
              timeout_s: float | None) -> DownloadedSource:
    """Download one URL through an injected callable, the module or the binary."""
    if downloader is not None:
        try:
            result = downloader(url, dest_dir)
        except Exception as exc:  # noqa: BLE001 - the injected tool reports its reason
            raise ReferenceUnavailable(
                "The reference downloader refused the URL",
                details={"source": url, "reason": str(exc)},
            ) from exc
        if isinstance(result, Mapping):
            raw_path = result.get("path")
            title = result.get("title")
            extractor = result.get("extractor")
        else:
            raw_path, title, extractor = result, None, None
        if not isinstance(raw_path, (str, os.PathLike)):
            raise ReferenceUnavailable("The reference downloader returned no path",
                                       details={"source": url, "returned": type(raw_path).__name__})
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise ReferenceUnavailable("The reference downloader returned a missing file",
                                       details={"source": url, "path": str(path)})
        return DownloadedSource(path=path, title=title, extractor=extractor)

    if yt_dlp is not None:
        executable = find_yt_dlp(yt_dlp)
        assert executable is not None
        return _download_with_executable(url, dest_dir, executable, timeout_s)
    module = load_yt_dlp_module()
    if module is not None:
        return _download_with_module(url, dest_dir, module)
    executable = find_yt_dlp(required=False)
    if executable is None:
        raise ToolUnavailable(
            "yt-dlp is not installed; a URL cannot be downloaded",
            details={"tool": "yt-dlp",
                     "searched": ["Python module yt_dlp", "PATH", "REAPER_WORKBENCH_YTDLP"],
                     "next_action": ("install the yt-dlp module or executable, or pass "
                                     "downloader=... for a custom download")},
        )
    return _download_with_executable(url, dest_dir, executable, timeout_s)


def _convert(source: Path, target: Path, *, ffmpeg: Path, sample_rate: int,
             channels: int, pcm_codec: str, timeout_s: float | None) -> None:
    command = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-y",
        "-i", str(source),
        "-vn", "-map_metadata", "-1",
        "-ac", str(channels), "-ar", str(sample_rate),
        "-c:a", pcm_codec,
        str(target),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ReferenceConversionFailed(
            "ffmpeg did not finish within the timeout",
            details={"source": str(source), "timeout_s": timeout_s},
        ) from exc
    if completed.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        raise ReferenceConversionFailed(
            "ffmpeg could not convert the reference to WAV",
            details={"source": str(source), "output": str(target),
                     "exit_code": completed.returncode, "stderr": tail(completed.stderr),
                     "next_action": "inspect the source file; no partial WAV is kept"},
        )


def _publish(converted: Path, target: Path) -> None:
    """Move the finished WAV into ``references/`` without a partial file."""
    try:
        os.replace(converted, target)
    except OSError as exc:
        raise ReferenceConversionFailed(
            "The imported reference could not be written",
            details={"target": str(target), "reason": str(exc),
                     "next_action": ("close the file in any player or DAW, or choose "
                                     "another label")},
        ) from exc


def _metric(report, name: str) -> float | None:
    metric = report.metrics.get(name)
    return None if metric is None else metric.value


def measure_reference_profile(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Metrics profile of one imported reference via ``measure_audio``.

    Keys carry their unit where the name alone would be ambiguous.  Bands are
    the analyzer's 1/6-octave grid, not a separately invented one.
    """
    audio = load_audio(Path(path))
    report = measure_audio(audio)
    curves = report.curves or {}
    notes = {name: metric.note for name, metric in report.metrics.items()
             if metric.note}
    return {
        "lufs": _metric(report, "integrated_lufs"),
        "lra": _metric(report, "loudness_range_lu"),
        "peak_dbfs": _metric(report, "peak_dbfs"),
        "true_peak_dbtp": _metric(report, "true_peak_estimate_dbfs"),
        "crest_db": _metric(report, "crest_db"),
        "centroid_hz": _metric(report, "spectral_centroid_hz"),
        "side_mid_db": _metric(report, "side_mid_db"),
        "stereo_correlation": _metric(report, "stereo_correlation"),
        "mono_fold_loss_db": _metric(report, "mono_fold_loss_db"),
        "bands": {
            "kind": "1_6_octave",
            "centers_hz": list(curves.get("band_centers_hz") or []),
            "levels_dbfs": list(curves.get("band_level_dbfs") or []),
        },
        "units": {
            "lufs": "LUFS",
            "lra": "LU",
            "peak_dbfs": "dBFS",
            "true_peak_dbtp": "dBTP",
            "crest_db": "dB",
            "centroid_hz": "Hz",
            "side_mid_db": "dB",
            "stereo_correlation": "correlation",
            "mono_fold_loss_db": "dB",
            "bands": "dB per 1/6-octave band",
        },
        "notes": notes,
        "provenance": {
            "analyzer": "analysis.metrics.measure_audio",
            "analysis_version": ANALYSIS_VERSION,
            "sample_rate": report.sample_rate,
            "channels": report.channels,
            "duration_s": report.duration_s,
            "source": report.source,
        },
    }


def import_reference(source: str | os.PathLike[str], *, label: str | None = None,
                     archive_root: str | os.PathLike[str] | None = None,
                     sample_rate: int = DEFAULT_SAMPLE_RATE,
                     channels: int = DEFAULT_CHANNELS,
                     pcm_codec: str = DEFAULT_PCM_CODEC,
                     profile: bool = True,
                     overwrite: bool = False,
                     ffmpeg: str | os.PathLike[str] | None = None,
                     downloader: Downloader | None = None,
                     yt_dlp: str | os.PathLike[str] | None = None,
                     timeout_s: float | None = None) -> dict[str, Any]:
    """Import one reference file or URL and return its path, hash and profile.

    ``source`` is an HTTP(S) URL or a path to an audio file.  A URL needs
    ``yt-dlp`` (module, executable or an injected ``downloader``); every source
    is converted by ``ffmpeg`` to ``<archive_root>/references/<label>.wav``.

    The report contains exactly the facts of the import: where the file is, its
    SHA-256, its format after conversion and the measured profile.  A different
    file already occupying the label is refused unless ``overwrite=True``; the
    same file imported again is reused and marked ``reused: true``.
    """
    text = str(source).strip()
    if not isinstance(source, (str, os.PathLike)):
        raise ValidationError("A reference source must be a path or a URL",
                              details={"source_type": type(source).__name__})
    if not text:
        raise ValidationError("A reference source must not be empty")
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate <= 0:
        raise ValidationError("sample_rate must be a positive integer",
                              details={"sample_rate": sample_rate})
    if isinstance(channels, bool) or not isinstance(channels, int) or channels <= 0:
        raise ValidationError("channels must be a positive integer",
                              details={"channels": channels})
    if not isinstance(pcm_codec, str) or not pcm_codec.strip():
        raise ValidationError("pcm_codec must be a non-empty ffmpeg codec name",
                              details={"pcm_codec": pcm_codec})
    if not isinstance(profile, bool):
        raise ValidationError("profile must be a boolean")
    if not isinstance(overwrite, bool):
        raise ValidationError("overwrite must be a boolean")
    if label is not None and (not isinstance(label, str) or not label.strip()):
        raise ValidationError("label must be a non-empty string or null",
                              details={"label": label})
    if timeout_s is not None and (isinstance(timeout_s, bool)
                                  or not isinstance(timeout_s, (int, float))
                                  or timeout_s <= 0):
        raise ValidationError("timeout_s must be a positive number or null",
                              details={"timeout_s": timeout_s})

    url_source = is_url(text)
    local_path: Path | None = None
    if not url_source:
        local_path = Path(text).expanduser()
        if not local_path.is_absolute():
            local_path = (Path.cwd() / local_path).absolute()
        local_path = local_path.resolve()
        if not local_path.is_file():
            raise ValidationError(
                "The reference file does not exist",
                details={"path": str(local_path),
                         "next_action": "pass an existing audio file or an http(s) URL"},
            )

    ffmpeg_path = find_ffmpeg(ffmpeg)
    assert ffmpeg_path is not None
    root = resolve_archive_root(archive_root)
    references = root / "references"
    incoming = root / ".incoming"
    references.mkdir(parents=True, exist_ok=True)
    incoming.mkdir(parents=True, exist_ok=True)

    workdir = Path(tempfile.mkdtemp(prefix="reference-", dir=str(incoming)))
    try:
        if url_source:
            downloaded = _download(text, workdir, downloader=downloader,
                                   yt_dlp=yt_dlp, timeout_s=timeout_s)
            input_path = downloaded.path
            title = downloaded.title or Path(text.split("?")[0]).stem
            source_kind = "url"
            extractor = downloaded.extractor
        else:
            assert local_path is not None
            input_path = local_path
            title = local_path.stem
            source_kind = "file"
            extractor = None

        source_info = _try_probe_audio(input_path)
        source_digest = sha256_file(input_path)
        chosen_label = sanitize_label(label if label is not None else title)

        converted = workdir / f"converted-{uuid.uuid4().hex}.wav"
        _convert(input_path, converted, ffmpeg=ffmpeg_path,
                 sample_rate=sample_rate, channels=channels,
                 pcm_codec=pcm_codec.strip(), timeout_s=timeout_s)
        converted_info = _probe_audio(converted, role="converted reference")
        digest = sha256_file(converted)

        target = references / f"{chosen_label}.wav"
        reused = False
        if target.exists():
            existing_digest = sha256_file(target)
            if existing_digest == digest:
                reused = True
            elif not overwrite:
                raise ReferenceConflict(
                    "Another reference already uses this label",
                    details={"label": chosen_label, "path": str(target),
                             "existing_sha256": existing_digest,
                             "new_sha256": digest,
                             "next_action": ("pass another label, or overwrite=true to "
                                             "replace the existing reference")},
                )
            else:
                _publish(converted, target)
        else:
            _publish(converted, target)

        final_info = _probe_audio(target, role="imported reference")
        source_format = None
        if source_info is not None:
            source_format = {
                "sample_rate": int(source_info.samplerate),
                "channels": int(source_info.channels),
                "frames": int(source_info.frames),
                "format": str(source_info.format),
                "subtype": str(source_info.subtype),
            }
        result: dict[str, Any] = {
            "ok": True,
            "operation": "reference.import",
            "path": str(target),
            "label": chosen_label,
            "sha256": digest,
            "duration_s": round(final_info.frames / final_info.samplerate, 6),
            "sample_rate": int(final_info.samplerate),
            "channels": int(final_info.channels),
            "frames": int(final_info.frames),
            "source": text,
            "source_kind": source_kind,
            "source_sha256": source_digest,
            "source_format": source_format,
            "reused": reused,
            "conversion": {
                "tool": "ffmpeg",
                "executable": str(ffmpeg_path),
                "pcm_codec": pcm_codec.strip(),
                "sample_rate": int(converted_info.samplerate),
                "channels": int(converted_info.channels),
                "frames": int(converted_info.frames),
                "level_change": 0.0,
            },
            "archive_root": str(root),
            "profile": measure_reference_profile(target) if profile else None,
        }
        if extractor is not None:
            result["source_extractor"] = extractor
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


__all__ = [
    "DEFAULT_CHANNELS",
    "DEFAULT_PCM_CODEC",
    "DEFAULT_SAMPLE_RATE",
    "DownloadedSource",
    "ReferenceConflict",
    "ReferenceConversionFailed",
    "ReferenceUnavailable",
    "import_reference",
    "is_url",
    "measure_reference_profile",
    "resolve_archive_root",
    "sanitize_label",
]
