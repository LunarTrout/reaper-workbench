"""External audio files as measurement sources.

A reference or a bounce does not have to be imported into the project to be
measured by the same analyzer: ``sources=[{"kind": "file", "path": ...}]``
measures the file directly.  The identity of such a source is the SHA-256 of
its bytes, so two different files can never share a row in the table or an
entry in the measurement cache.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import soundfile as sf

from ..errors import ValidationError
from ..context.measurement_cache import sha256_file

KIND_FILE = "file"
FILE_KIND_ALIASES = frozenset({"file", "external", "path"})
SOURCE_ID_PREFIX = "file:"
#: Evidence and cache stage for a source that is not a project render.
EXTERNAL_SIGNAL_STAGE = "external_file"


@dataclass(frozen=True, slots=True)
class ExternalSource:
    """One decoded-on-demand external asset."""

    source_id: str
    path: Path
    sha256: str
    sample_rate: int
    channels: int
    frames: int
    duration_s: float

    @property
    def evidence_id(self) -> str:
        return f"{SOURCE_ID_PREFIX}{self.sha256}"

    def to_dict(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "name": self.path.stem,
                "kind": KIND_FILE, "stage": EXTERNAL_SIGNAL_STAGE,
                "file": str(self.path), "sha256": self.sha256,
                "sample_rate": self.sample_rate, "channels": self.channels,
                "frames": self.frames, "duration_s": round(self.duration_s, 6)}


def is_external_entry(entry: Any) -> bool:
    return (isinstance(entry, Mapping)
            and str(entry.get("kind") or "").strip().lower() in FILE_KIND_ALIASES)


def resolve_external_source(entry: Mapping[str, Any]) -> ExternalSource:
    """Validate one ``{"kind": "file", "path": ...}`` source and read its header."""
    raw_path = entry.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValidationError("An external source needs a non-empty 'path'",
                              details={"source": dict(entry)})
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).absolute()
    path = path.resolve()
    if not path.is_file():
        raise ValidationError("External source file does not exist",
                              details={"path": str(path),
                                       "next_action": "pass an absolute path to a readable audio file"})
    try:
        info = sf.info(str(path))
    except Exception as exc:  # noqa: BLE001 - header read reports its own reason
        raise ValidationError("External source is not a readable audio file",
                              details={"path": str(path), "reason": str(exc)}) from exc
    if info.frames <= 0 or info.samplerate <= 0 or info.channels <= 0:
        raise ValidationError("External source contains no samples",
                              details={"path": str(path), "frames": int(info.frames),
                                       "sample_rate": int(info.samplerate)})
    digest = sha256_file(path)
    return ExternalSource(source_id=f"{SOURCE_ID_PREFIX}{digest[:16]}",
                          path=path, sha256=digest,
                          sample_rate=int(info.samplerate),
                          channels=int(info.channels),
                          frames=int(info.frames),
                          duration_s=float(info.frames) / float(info.samplerate))


__all__ = [
    "EXTERNAL_SIGNAL_STAGE",
    "ExternalSource",
    "FILE_KIND_ALIASES",
    "KIND_FILE",
    "SOURCE_ID_PREFIX",
    "is_external_entry",
    "resolve_external_source",
]
