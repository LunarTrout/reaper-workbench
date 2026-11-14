"""Task-scoped audio artifacts with manifest-only cleanup."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path

from ..errors import ArtifactCleanupError, ValidationError

_MANIFEST_NAME = ".reaper-audio-job.json"
_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ARTIFACT_NAME = re.compile(r"^[^<>:\"/\\|?*\x00-\x1f]{1,128}$")
_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class CleanupReport:
    job_id: str
    removed_files: tuple[str, ...]
    preserved_paths: tuple[str, ...]
    removed_job_directory: bool


@dataclass(frozen=True, slots=True)
class RegisteredArtifact:
    """Content identity of one manifest-owned audio file.

    ``sha256`` is 64 lowercase hexadecimal characters, ``bytes`` is the exact
    file size that was hashed, and ``mime`` is sniffed from the filename with a
    binary fallback for unknown extensions.
    """

    path: Path
    filename: str
    bytes: int
    sha256: str
    mime: str


class AudioArtifactWorkspace:
    """A unique directory for one capture/analyze/compare job.

    Only artifact names allocated by this object are eligible for cleanup.
    Unregistered files, directories, links, and paths outside the job directory
    are preserved. Persistent project checkpoints are deliberately outside this
    class and can never be passed to its cleanup routines.
    """

    def __init__(self, root: str | os.PathLike[str], *, job_id: str | None = None):
        self.root = Path(root).expanduser().absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ValidationError("Artifact root must be a directory",
                                  details={"path": str(self.root)})
        self.root_real = self.root.resolve(strict=True)
        self.job_id = job_id or uuid.uuid4().hex
        if not isinstance(self.job_id, str) or not _JOB_ID.fullmatch(self.job_id):
            raise ValidationError("job_id contains unsupported characters")
        self.directory = self.root / f"job-{self.job_id}"
        self.directory.mkdir(exist_ok=False)
        self.directory_real = self.directory.resolve(strict=True)
        if self.directory_real.parent != self.root_real:
            self.directory.rmdir()
            raise ValidationError("Artifact job directory escaped its managed root")
        self.manifest_path = self.directory / _MANIFEST_NAME
        self._artifact_names: list[str] = []
        self._assets: list[dict[str, object]] = []
        self._closed = False
        self._created_epoch = datetime.now(timezone.utc).timestamp()
        self._write_manifest(status="active")

    def __enter__(self) -> "AudioArtifactWorkspace":
        if self._closed:
            raise ValidationError("Artifact workspace is already closed")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    @property
    def artifact_names(self) -> tuple[str, ...]:
        return tuple(self._artifact_names)

    def allocate(self, filename: str) -> Path:
        """Register and return a new direct-child file path for a host render."""
        if self._closed:
            raise ValidationError("Cannot allocate an artifact after workspace close")
        if not _safe_artifact_name(filename):
            raise ValidationError("Artifact filename must be a safe direct-child name")
        path = self.directory / filename
        if path.exists() or path.is_symlink():
            raise ValidationError("Artifact target already exists",
                                  details={"filename": filename})
        self._artifact_names.append(filename)
        self._write_manifest(status="active")
        return path

    def register_asset(self, path: str | os.PathLike[str]) -> RegisteredArtifact:
        """Register one host-created file and return its content identity."""
        return self.register_assets((path,))[0]

    def register_assets(
        self, paths: tuple[str | os.PathLike[str], ...] | list[str | os.PathLike[str]],
    ) -> tuple[RegisteredArtifact, ...]:
        """Register host-created files and record their measured content identity.

        Every input must already exist as a non-empty, regular, non-link file
        directly in this job directory. Validation and hashing are completed
        for the whole batch before the manifest changes, so a malformed bridge
        response cannot partially register a set of renders.
        """
        validated = self._validate_pending_assets(paths)
        assets = tuple(
            RegisteredArtifact(
                path=resolved,
                filename=filename,
                bytes=size,
                sha256=_stream_sha256(resolved, expected_bytes=size),
                mime=_artifact_mime(filename),
            )
            for filename, resolved, size in validated
        )

        previous_names = list(self._artifact_names)
        previous_assets = [dict(asset) for asset in self._assets]
        self._artifact_names.extend(asset.filename for asset in assets)
        self._assets.extend(
            {"name": asset.filename, "bytes": asset.bytes,
             "sha256": asset.sha256, "mime": asset.mime}
            for asset in assets
        )
        try:
            self._write_manifest(status="active")
        except Exception:
            self._artifact_names = previous_names
            self._assets = previous_assets
            raise
        return assets

    def register_existing_files(
        self, paths: tuple[str | os.PathLike[str], ...] | list[str | os.PathLike[str]],
    ) -> tuple[Path, ...]:
        """Compatibility wrapper returning only the registered paths.

        New callers should prefer :meth:`register_assets`, which also reports
        the byte size, SHA-256 digest and media type of every owned file.
        """
        return tuple(asset.path for asset in self.register_assets(paths))

    def _validate_pending_assets(
        self, paths: tuple[str | os.PathLike[str], ...] | list[str | os.PathLike[str]],
    ) -> list[tuple[str, Path, int]]:
        if self._closed:
            raise ValidationError("Cannot register artifacts after workspace close")
        if not isinstance(paths, (tuple, list)) or not paths:
            raise ValidationError("paths must be a non-empty list of rendered files")

        validated: list[tuple[str, Path, int]] = []
        seen: set[str] = set()
        for value in paths:
            if not isinstance(value, (str, os.PathLike)):
                raise ValidationError("Rendered artifact paths must be filesystem paths")
            path = Path(value)
            filename = path.name
            if not _safe_artifact_name(filename):
                raise ValidationError("Rendered artifact has an unsafe filename",
                                      details={"path": str(path)})
            if filename in seen:
                raise ValidationError("Rendered artifact paths must be unique",
                                      details={"filename": filename})
            seen.add(filename)
            if path.is_symlink() or _is_junction(path) or not path.is_file():
                raise ValidationError("Rendered artifact must be an existing regular file",
                                      details={"path": str(path)})
            try:
                resolved = path.resolve(strict=True)
                parent = resolved.parent
                given_parent = path.parent.resolve(strict=True)
            except OSError as exc:
                raise ValidationError("Rendered artifact path cannot be resolved",
                                      details={"path": str(path),
                                               "reason": str(exc)}) from exc
            if (given_parent != self.directory_real or
                    parent != self.directory_real or
                    resolved.name != filename):
                raise ValidationError("Rendered artifact must be a direct child of its job directory",
                                      details={"path": str(path),
                                               "job_directory": str(self.directory_real)})
            if filename in self._artifact_names or any(
                    asset.get("name") == filename for asset in self._assets):
                raise ValidationError("Rendered artifact is already registered",
                                      details={"filename": filename})
            try:
                size = resolved.stat().st_size
            except OSError as exc:
                raise ValidationError("Rendered artifact cannot be inspected",
                                      details={"path": str(path),
                                               "reason": str(exc)}) from exc
            if size <= 0:
                raise ValidationError("Rendered artifact must not be empty",
                                      details={"path": str(path),
                                               "filename": filename})
            validated.append((filename, resolved, size))
        return validated

    def close(self, *, reclaim_orphans: bool = False) -> CleanupReport:
        if self._closed:
            return CleanupReport(self.job_id, (), (), not self.directory.exists())
        report = _cleanup_manifest_job(
            self.root_real,
            self.directory,
            self.directory_real,
            expected_job_id=self.job_id,
            expected_names=tuple(self._artifact_names),
            reclaim_orphans=reclaim_orphans,
        )
        self._closed = report.removed_job_directory
        return report

    def retain(self) -> Path:
        """Keep this manifest-owned job for a bounded external resource lease.

        The directory remains eligible for ``cleanup_stale_audio_jobs``. This
        is intended for short-lived MCP audio resources, never for project
        versions or permanent exports.
        """
        if self._closed:
            raise ValidationError("Cannot retain an already closed artifact workspace")
        self._write_manifest(status="retained")
        self._closed = True
        return self.directory_real

    def _write_manifest(self, *, status: str) -> None:
        manifest = {
            "schema": 1,
            "owner": "reaper-audio-library",
            "job_id": self.job_id,
            "status": status,
            "created_epoch": self._created_epoch,
            "artifact_names": list(self._artifact_names),
            "assets": [dict(asset) for asset in self._assets],
        }
        temp_path = self.directory / f".{_MANIFEST_NAME}.{self.job_id}.tmp"
        if temp_path.exists() or temp_path.is_symlink():
            raise ArtifactCleanupError(
                "Refusing to replace an unexpected artifact manifest temporary file",
                details={"path": str(temp_path)},
            )
        with temp_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, self.manifest_path)


def cleanup_stale_audio_jobs(
    root: str | os.PathLike[str], *, max_age_s: float = 24 * 60 * 60,
    now_epoch: float | None = None,
) -> tuple[CleanupReport, ...]:
    """Clean old jobs only when their owner manifest is valid and paths are safe."""
    if (isinstance(max_age_s, bool) or not isinstance(max_age_s, (int, float))
            or not isfinite(max_age_s) or max_age_s < 0):
        raise ValidationError("max_age_s must be a non-negative number")
    base = Path(root).expanduser().absolute()
    if not base.exists():
        return ()
    if base.is_symlink() or _is_junction(base) or not base.is_dir():
        raise ValidationError("Artifact root must be a regular directory",
                              details={"path": str(base)})
    base_real = base.resolve(strict=True)
    now = now_epoch if now_epoch is not None else datetime.now(timezone.utc).timestamp()
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not isfinite(now):
        raise ValidationError("now_epoch must be a finite number")
    reports: list[CleanupReport] = []
    for directory in base.iterdir():
        if (not directory.name.startswith("job-") or directory.is_symlink()
                or _is_junction(directory) or not directory.is_dir()):
            continue
        manifest_path = directory / _MANIFEST_NAME
        try:
            if manifest_path.is_symlink() or _is_junction(manifest_path):
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            job_id = manifest["job_id"]
            created_epoch = float(manifest["created_epoch"])
            names = manifest["artifact_names"]
            valid = (
                manifest.get("schema") == 1
                and manifest.get("owner") == "reaper-audio-library"
                and isinstance(job_id, str)
                and directory.name == f"job-{job_id}"
                and _JOB_ID.fullmatch(job_id) is not None
                and isinstance(names, list)
                and all(_safe_artifact_name(name) for name in names)
            )
            if not valid or not isfinite(created_epoch) or now - created_epoch < max_age_s:
                continue
            directory_real = directory.resolve(strict=True)
            if directory_real.parent != base_real:
                continue
            reports.append(_cleanup_manifest_job(
                base_real, directory, directory_real, expected_job_id=job_id,
                expected_names=tuple(names), reclaim_orphans=True,
            ))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
    return tuple(reports)


def cleanup_audio_job(
    root: str | os.PathLike[str], job_id: str,
) -> CleanupReport | None:
    """Remove one manifest-owned job without touching unrelated files."""
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        raise ValidationError("job_id contains unsupported characters")
    base = Path(root).expanduser().absolute()
    if not base.exists():
        return None
    if base.is_symlink() or _is_junction(base) or not base.is_dir():
        raise ValidationError("Artifact root must be a regular directory",
                              details={"path": str(base)})
    base_real = base.resolve(strict=True)
    directory = base / f"job-{job_id}"
    if not directory.exists():
        return None
    if directory.is_symlink() or _is_junction(directory) or not directory.is_dir():
        raise ArtifactCleanupError("Refusing to clean a non-directory audio job",
                                   details={"path": str(directory)})
    directory_real = directory.resolve(strict=True)
    if directory_real.parent != base_real:
        raise ArtifactCleanupError("Audio job is outside its managed root",
                                   details={"path": str(directory_real)})
    manifest_path = directory / _MANIFEST_NAME
    if manifest_path.is_symlink() or _is_junction(manifest_path):
        raise ArtifactCleanupError("Refusing to clean a linked audio manifest",
                                   details={"path": str(manifest_path)})
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        names = manifest["artifact_names"]
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ArtifactCleanupError("Audio job has no valid ownership manifest",
                                   details={"job_id": job_id}) from exc
    valid = (
        isinstance(manifest, dict)
        and manifest.get("schema") == 1
        and manifest.get("owner") == "reaper-audio-library"
        and manifest.get("job_id") == job_id
        and isinstance(names, list)
        and all(_safe_artifact_name(name) for name in names)
    )
    if not valid:
        raise ArtifactCleanupError("Refusing to clean an invalid audio job manifest",
                                   details={"job_id": job_id})
    return _cleanup_manifest_job(
        base_real, directory, directory_real, expected_job_id=job_id,
        expected_names=tuple(names),
    )


def _stream_sha256(path: Path, *, expected_bytes: int) -> str:
    """Hash a registered file in bounded chunks and verify it stayed stable."""
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
    except OSError as exc:
        raise ValidationError("Rendered artifact cannot be hashed",
                              details={"path": str(path),
                                       "reason": str(exc)}) from exc
    if total != expected_bytes:
        raise ValidationError("Rendered artifact changed while it was hashed",
                              details={"path": str(path),
                                       "expected_bytes": expected_bytes,
                                       "hashed_bytes": total})
    return digest.hexdigest()


def _artifact_mime(filename: str) -> str:
    """Return the media type for an owned artifact name."""
    if Path(filename).suffix.lower() == ".wav":
        return "audio/wav"
    mime, _encoding = mimetypes.guess_type(filename, strict=False)
    return mime or "application/octet-stream"


def _safe_artifact_name(name: object) -> bool:
    if (not isinstance(name, str) or _ARTIFACT_NAME.fullmatch(name) is None or
            name in {".", "..", _MANIFEST_NAME} or name[-1:] in {" ", "."}):
        return False
    device_name = name.split(".", 1)[0].rstrip(" .").upper()
    if (device_name in {"CON", "PRN", "AUX", "NUL"} or
            re.fullmatch(r"(?:COM|LPT)[1-9]", device_name)):
        return False
    return Path(name).name == name


def _is_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _cleanup_manifest_job(
    root_real: Path,
    directory: Path,
    directory_real: Path,
    *,
    expected_job_id: str,
    expected_names: tuple[str, ...],
    reclaim_orphans: bool = False,
) -> CleanupReport:
    if (directory.is_symlink() or _is_junction(directory)
            or directory.resolve(strict=True) != directory_real
            or directory_real.parent != root_real
            or directory.name != f"job-{expected_job_id}"):
        raise ArtifactCleanupError("Artifact job path changed; refusing cleanup",
                                   details={"path": str(directory)})

    removed: list[str] = []
    preserved: list[str] = []
    names = tuple(dict.fromkeys(expected_names))
    for name in names:
        if not _safe_artifact_name(name):
            preserved.append(name)
            continue
        path = directory / name
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or _is_junction(path):
            preserved.append(str(path))
            continue
        try:
            if path.parent.resolve(strict=True) != directory_real or not path.is_file():
                preserved.append(str(path))
                continue
            path.unlink()
            removed.append(name)
        except OSError:
            preserved.append(str(path))

    # REAPER creates the WAVs before Python can register the completed render.
    # If the caller is interrupted during that gap, only stale-job cleanup may
    # reclaim those orphaned outputs. The unique job-id prefix and direct-child
    # check keep this away from unrelated files; immediate cleanup deliberately
    # leaves unregistered files untouched in case the host is still writing.
    if reclaim_orphans:
        prefix = f"rali-{expected_job_id}-"
        for path in directory.iterdir():
            if (path.name in names or not path.name.startswith(prefix) or
                    path.suffix.lower() != ".wav"):
                continue
            if path.is_symlink() or _is_junction(path):
                preserved.append(str(path))
                continue
            try:
                if (path.parent.resolve(strict=True) != directory_real or
                        not path.is_file()):
                    preserved.append(str(path))
                    continue
                path.unlink()
                removed.append(path.name)
            except OSError:
                preserved.append(str(path))

    manifest = directory / _MANIFEST_NAME
    temp_manifest = directory / f".{_MANIFEST_NAME}.{expected_job_id}.tmp"
    removed_directory = False
    for path in (manifest, temp_manifest):
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or _is_junction(path):
            preserved.append(str(path))
        elif not path.is_file():
            preserved.append(str(path))

    managed_names = {_MANIFEST_NAME, temp_manifest.name}
    for child in directory.iterdir():
        if child.name not in managed_names:
            preserved.append(str(child))

    if not preserved:
        try:
            for path in (manifest, temp_manifest):
                if path.exists():
                    if path.parent.resolve(strict=True) != directory_real:
                        preserved.append(str(path))
                        break
                    path.unlink()
            if preserved:
                return CleanupReport(expected_job_id, tuple(removed),
                                     _unique(preserved), False)
            directory.rmdir()
            removed_directory = True
        except OSError:
            preserved.extend(str(path) for path in directory.iterdir())

    return CleanupReport(expected_job_id, tuple(removed), _unique(preserved),
                         removed_directory)


def _unique(paths: list[str]) -> tuple[str, ...]:
    """Order-preserving de-duplication for cleanup reports.

    A file that could not be unlinked is preserved by the name pass *and* seen
    again by the final directory scan; reporting it twice would make the
    preserved set look larger than the files actually left behind.
    """
    return tuple(dict.fromkeys(paths))
