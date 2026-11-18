"""Content-addressed cache for rendered-audio measurements (Stage A1).

The cache key is the SHA-256 of the rendered WAV plus every input that can
change the measured number:

    (wav_sha256, section, sample_rate, channels, stage, analyzer,
     analyzer_version, options_digest, schema)

Because the key is the audio content itself, a hit cannot change a value: the
same bytes decoded with the same analyzer and the same window produce the same
report.  That also makes the cache immune to plugin nondeterminism (R2): a
nondeterministic plugin re-renders to different bytes and simply misses.

The cache never stores a path: ``source`` is blanked before writing and set to
the current file on read, so a reused report cannot claim to have been measured
from a file that no longer exists.  ``file_frames``/``file_channels`` and every
metric stay exactly as measured.

Layout::

    <archive>/measurement-cache/<key[:2]>/<key>.json

Writes are atomic (temp file in the same directory, fsync, ``os.replace``); a
torn or unreadable entry is treated as a miss, never as evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .contract_v2 import (ANALYZER, ANALYZER_VERSION, MEASUREMENT_CACHE_SCHEMA,
                          analysis_cache_root_name, fingerprint_requires_bytes)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def sha256_file(path: str | os.PathLike[str], *, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def measurement_key(*, wav_sha256: str,
                    fingerprint: str,
                    section: Mapping[str, float] | None,
                    sample_rate: int,
                    channels: int,
                    stage: str,
                    options: Mapping[str, Any] | None = None) -> str:
    """Cache key: the source state fingerprint plus every measurement input.

    ``wav_sha256`` is *not* part of the key: REAPER's offline render is not
    byte-identical between runs (measured on the bench: one stem differed by
    54 ms of ~-80 dB leakage in the silent channel), so a byte key would miss
    every time.  The bytes are stored in the entry as ``measured_sha256`` and
    compared on read, so a hit can declare whether it was byte-identical.
    """
    if not isinstance(wav_sha256, str) or len(wav_sha256) != 64:
        raise ValueError("wav_sha256 must be a 64-character hex digest")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("fingerprint must be a non-empty source-state digest")
    interval = None
    if section is not None:
        interval = {"start_s": float(section["start_s"]),
                    "end_s": float(section["end_s"])}
    payload = {
        "schema": MEASUREMENT_CACHE_SCHEMA,
        "fingerprint": fingerprint,
        "section": interval,
        "sample_rate": int(sample_rate),
        "channels": int(channels),
        "stage": str(stage),
        "analyzer": ANALYZER,
        "analyzer_version": ANALYZER_VERSION,
        "options": dict(options or {}),
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def cache_root(archive_root: str | os.PathLike[str]) -> Path:
    return Path(archive_root).expanduser().absolute() / analysis_cache_root_name()


class MeasurementCache:
    """Read/write access to the measurement cache for one capture."""

    def __init__(self, archive_root: str | os.PathLike[str], *,
                 section: Mapping[str, float] | None,
                 options: Mapping[str, Any] | None = None) -> None:
        self.root = cache_root(archive_root)
        self.section = section
        self.options = dict(options or {})
        self.hits = 0
        self.misses = 0
        self.fingerprint_reuse = 0
        self.reused_dsp_s = 0.0
        self.stored = 0
        self.errors: list[dict[str, str]] = []
        self._keys: dict[str, str] = {}
        self._format: dict[str, tuple[int, int]] = {}

    # ------------------------------------------------------------------ #
    def _format_of(self, path: str | os.PathLike[str]) -> tuple[int, int]:
        import soundfile as sf

        resolved = str(Path(path).absolute())
        if resolved not in self._format:
            info = sf.info(resolved)
            self._format[resolved] = (int(info.samplerate), int(info.channels))
        return self._format[resolved]

    def key_for(self, path: str | os.PathLike[str], *, stage: str,
                fingerprint: str) -> tuple[str, str]:
        """Return ``(wav_sha256, cache_key)`` for a rendered file."""
        digest = sha256_file(path)
        sample_rate, channels = self._format_of(path)
        key = measurement_key(wav_sha256=digest, fingerprint=fingerprint,
                              section=self.section,
                              sample_rate=sample_rate, channels=channels,
                              stage=stage, options=self.options)
        self._keys[str(Path(path).absolute())] = key
        return digest, key

    def key_of(self, path: str | os.PathLike[str]) -> str | None:
        return self._keys.get(str(Path(path).absolute()))

    def lookup(self, path: str | os.PathLike[str], *, stage: str,
               fingerprint: str) -> tuple[str, dict | None]:
        digest, key = self.key_for(path, stage=stage, fingerprint=fingerprint)
        entry = self._read(key)
        if entry is None:
            self.misses += 1
            return key, None
        report = entry.get("report")
        if not isinstance(report, dict):
            self.errors.append({"key": key, "reason": "entry has no report"})
            self.misses += 1
            return key, None
        stored_sha = entry.get("measured_sha256")
        byte_identical = isinstance(stored_sha, str) and stored_sha == digest
        if fingerprint_requires_bytes() and not byte_identical:
            self.misses += 1
            self.errors.append({"key": key, "reason": "byte identity required"})
            return key, None
        report = dict(report)
        report["source"] = str(Path(path).absolute())
        self.hits += 1
        if not byte_identical:
            self.fingerprint_reuse += 1
        try:
            self.reused_dsp_s += float(entry.get("dsp_s") or 0.0)
        except (TypeError, ValueError):
            pass
        return key, {
            "report": report,
            "frames": entry.get("frames"),
            "duration_s": entry.get("duration_s"),
            "sample_rate": entry.get("sample_rate"),
            "channels": entry.get("channels"),
            "dsp_s": entry.get("dsp_s"),
            "decode_s": entry.get("decode_s"),
            "byte_identical": byte_identical,
            "measured_sha256": stored_sha,
        }

    def remember(self, key: str, *, stage: str, result: Mapping[str, Any],
                 path: str | os.PathLike[str]) -> None:
        report = result.get("analysis")
        if not isinstance(report, dict):
            return
        stored_report = dict(report)
        stored_report["source"] = ""
        sample_rate, channels = self._format_of(path)
        entry = {
            "schema": MEASUREMENT_CACHE_SCHEMA,
            "key": key,
            "stage": str(stage),
            "section": (None if self.section is None else
                        {"start_s": float(self.section["start_s"]),
                         "end_s": float(self.section["end_s"])}),
            "sample_rate": sample_rate,
            "channels": channels,
            "analyzer": ANALYZER,
            "analyzer_version": ANALYZER_VERSION,
            "dsp_s": result.get("dsp_s"),
            "decode_s": result.get("decode_s"),
            "measured_sha256": sha256_file(path),
            "frames": result.get("frames"),
            "duration_s": result.get("duration_s"),
            "report": stored_report,
        }
        try:
            self._write(key, entry)
            self.stored += 1
        except OSError as exc:  # a cache write must never fail a capture
            self.errors.append({"key": key, "reason": f"write failed: {exc}"})

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "hits": self.hits,
            "misses": self.misses,
            "fingerprint_reuse": self.fingerprint_reuse,
            "stored": self.stored,
            "reused_dsp_s": round(self.reused_dsp_s, 3),
            "errors": list(self.errors),
            "root": str(self.root),
        }

    # ------------------------------------------------------------------ #
    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def _read(self, key: str) -> dict | None:
        path = self._path(key)
        try:
            raw = path.read_text(encoding="utf-8")
            entry = json.loads(raw)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            self.errors.append({"key": key, "reason": f"unreadable entry: {exc}"})
            return None
        if not isinstance(entry, dict) or entry.get("key") != key:
            self.errors.append({"key": key, "reason": "entry key mismatch"})
            return None
        if entry.get("schema") != MEASUREMENT_CACHE_SCHEMA:
            self.errors.append({"key": key, "reason": "schema mismatch"})
            return None
        return entry

    def _write(self, key: str, entry: Mapping[str, Any]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(entry, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent),
            prefix=f".{key[:8]}.", suffix=".tmp", delete=False)
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, path)
        finally:
            try:
                if os.path.exists(handle.name):
                    os.unlink(handle.name)
            except OSError:
                pass


__all__ = ["MeasurementCache", "cache_root", "measurement_key", "sha256_file"]
