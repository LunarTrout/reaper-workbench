"""Discovery of the external tools the library may call.

Nothing here hard-codes an installation path.  An explicit argument wins, then
a named environment variable, then PATH, then the Hermes tools directory.  The
Hermes directory is versioned (``ffmpeg-9.0.1-win32-x64``), so the newest
installed build is selected instead of the first one the filesystem happens to
return.

A missing tool raises :class:`ToolUnavailable` with the paths that were
searched.  There is no silent fallback to another binary and no guessed
command line.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from ..errors import ReaperAudioError

#: Environment variables checked before PATH.  The first non-empty one wins.
FFMPEG_ENV_VARS = ("REAPER_WORKBENCH_FFMPEG", "FFMPEG_BINARY")
YTDLP_ENV_VARS = ("REAPER_WORKBENCH_YTDLP",)


class ToolUnavailable(ReaperAudioError):
    """An external program or Python module required by an operation is absent."""

    code = "tool_unavailable"


def _dedupe(values):
    seen = set()
    out = []
    for value in values:
        text = str(value)
        if text not in seen:
            seen.add(text)
            out.append(value)
    return out


def _hermes_tool_roots() -> list[Path]:
    """Directories where Hermes installs versioned tool bundles."""
    roots: list[Path] = []
    for variable in ("LOCALAPPDATA", "XDG_DATA_HOME"):
        raw = os.environ.get(variable)
        if raw:
            roots.append(Path(raw) / "hermes" / "tools")
    roots.append(Path.home() / "AppData" / "Local" / "hermes" / "tools")
    roots.append(Path.home() / ".local" / "share" / "hermes" / "tools")
    return _dedupe(roots)


def _version_key(executable: Path) -> tuple[int, ...]:
    """Sort key from a versioned bundle name; unversioned paths sort last."""
    for part in reversed(executable.parts):
        match = re.match(r"^ffmpeg-(\d+(?:\.\d+)*)", part, re.IGNORECASE)
        if match:
            return tuple(int(piece) for piece in match.group(1).split("."))
    return (0,)


def _first_existing(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _configured_paths(explicit: str | os.PathLike[str] | None,
                      env_vars: tuple[str, ...], *, tool: str) -> list[Path]:
    """Explicit path or the first non-empty environment variable, validated.

    An explicit argument outranks the environment, and a path that was supplied
    but does not exist is an error instead of being silently skipped in favour
    of a different binary.
    """
    supplied: tuple[str, str | os.PathLike[str]] | None = None
    if explicit is not None:
        supplied = ("argument", explicit)
    else:
        for variable in env_vars:
            value = os.environ.get(variable)
            if value:
                supplied = (variable, value)
                break
    if supplied is None:
        return []
    origin, raw = supplied
    path = Path(raw).expanduser()
    if not path.is_file():
        raise ToolUnavailable(
            f"The configured {tool} executable does not exist",
            details={"tool": tool, "origin": origin, "path": str(path),
                     "next_action": (f"point {origin} at an existing {tool} "
                                     "executable, or unset it to search PATH")},
        )
    return [path.resolve()]


def find_ffmpeg(explicit: str | os.PathLike[str] | None = None,
                *, required: bool = True) -> Path | None:
    """Locate ``ffmpeg`` without assuming an installation directory.

    Search order: explicit argument, environment, PATH, versioned Hermes
    bundles (newest version first).  ``required=False`` returns ``None``
    instead of raising, which lets a test or a capability probe ask whether
    the tool exists without handling an exception.
    """
    configured = _configured_paths(explicit, FFMPEG_ENV_VARS, tool="ffmpeg")
    if configured:
        return configured[0]

    from_path = shutil.which("ffmpeg")
    if from_path:
        return Path(from_path).resolve()

    bundles: list[Path] = []
    for root in _hermes_tool_roots():
        if not root.is_dir():
            continue
        bundles.extend(root.glob("ffmpeg-*/bin/ffmpeg.exe"))
        bundles.extend(root.glob("ffmpeg-*/bin/ffmpeg"))
    bundles.sort(key=_version_key, reverse=True)
    found = _first_existing(bundles)
    if found is not None:
        return found

    if not required:
        return None
    searched = ["argument", *FFMPEG_ENV_VARS, "PATH",
                *[str(root) for root in _hermes_tool_roots()]]
    raise ToolUnavailable(
        "ffmpeg was not found; audio conversion and true-peak checks need it",
        details={"tool": "ffmpeg", "searched": searched,
                 "next_action": ("install ffmpeg, put it on PATH, or pass ffmpeg=/path/to/ffmpeg "
                                 "(REAPER_WORKBENCH_FFMPEG is also read)")},
    )


def find_yt_dlp(explicit: str | os.PathLike[str] | None = None,
                *, required: bool = True) -> Path | None:
    """Locate the ``yt-dlp`` executable as a fallback to the Python module."""
    configured = _configured_paths(explicit, YTDLP_ENV_VARS, tool="yt-dlp")
    if configured:
        return configured[0]
    from_path = shutil.which("yt-dlp")
    if from_path:
        return Path(from_path).resolve()
    if not required:
        return None
    raise ToolUnavailable(
        "yt-dlp was not found; a URL cannot be downloaded without it",
        details={"tool": "yt-dlp", "searched": ["argument", *YTDLP_ENV_VARS, "PATH"],
                 "next_action": ("install the yt-dlp Python module or executable, or pass "
                                 "downloader=... / yt_dlp=... explicitly")},
    )


def load_yt_dlp_module():
    """Return the imported ``yt_dlp`` module or ``None`` when it is absent."""
    try:
        import yt_dlp  # noqa: PLC0415 - optional dependency, imported on demand
    except ImportError:
        return None
    return yt_dlp


def tail(text: str | None, limit: int = 2000) -> str:
    """Last ``limit`` characters of a tool's stderr, for an error's details."""
    if not text:
        return ""
    clean = text.strip()
    return clean if len(clean) <= limit else "..." + clean[-limit:]


__all__ = [
    "FFMPEG_ENV_VARS",
    "YTDLP_ENV_VARS",
    "ToolUnavailable",
    "find_ffmpeg",
    "find_yt_dlp",
    "load_yt_dlp_module",
    "tail",
]
