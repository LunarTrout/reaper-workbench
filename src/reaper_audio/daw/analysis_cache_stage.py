"""Stage A1 staging: split rendered payloads into cache hits and misses.

The full capture has two analysis paths (pooled and synchronous).  Both call
this module so the cache behaves identically:

* ``split`` asks the cache for every payload and returns the hits plus the
  payloads that still need DSP work;
* ``merge`` rebuilds the caller's deterministic payload order from stored hits
  and fresh results;
* ``store`` writes only what was actually measured in this run.

Nothing here decides *whether* caching is allowed: that is the flag in
``context/contract_v2.py`` and the caller's choice of cache instance.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..context.measurement_cache import MeasurementCache


def split(payloads: Sequence[tuple[str, str]],
          stage_by_guid: Mapping[str, str],
          cache: MeasurementCache,
          fingerprint_by_guid: Mapping[str, str]) -> tuple[dict[str, dict[str, Any]],
                                                           list[tuple[str, str]]]:
    """Return ``(hits, misses)`` for ``(guid, path)`` payloads."""
    hits: dict[str, dict[str, Any]] = {}
    misses: list[tuple[str, str]] = []
    for guid, path in payloads:
        stage = stage_by_guid.get(guid)
        fingerprint = fingerprint_by_guid.get(guid)
        if not stage or not fingerprint:
            # An incomplete fingerprint is a dirty source (R3): never a hit.
            misses.append((guid, path))
            continue
        _key, hit = cache.lookup(path, stage=stage, fingerprint=fingerprint)
        if hit is None:
            misses.append((guid, path))
        else:
            hits[guid] = {
                "source_guid": guid,
                "analysis": hit["report"],
                "sample_rate": int(hit["sample_rate"]),
                "channels": int(hit["channels"]),
                "frames": int(hit["frames"]),
                "duration_s": float(hit["duration_s"]),
                "decode_s": 0.0,
                "dsp_s": 0.0,
                "reused": True,
                "cache_key": _key,
            }
    return hits, misses


def merge(payloads: Sequence[tuple[str, str]],
          hits: Mapping[str, Mapping[str, Any]],
          results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return one result per payload, in payload order."""
    by_guid = {str(result["source_guid"]): dict(result) for result in results}
    for guid, hit in hits.items():
        if guid in by_guid:
            raise RuntimeError(f"analysis returned a result for a cached source: {guid}")
        by_guid[guid] = dict(hit)
    missing = [guid for guid, _path in payloads if guid not in by_guid]
    if missing:
        raise RuntimeError("Analysis results are incomplete: "
                           + ", ".join(sorted(missing)[:8]))
    return [by_guid[guid] for guid, _path in payloads]


def store(payloads: Sequence[tuple[str, str]],
          stage_by_guid: Mapping[str, str],
          results: Sequence[Mapping[str, Any]],
          cache: MeasurementCache) -> None:
    """Persist results measured in this run; hits are not rewritten."""
    for result in results:
        guid = str(result["source_guid"])
        if result.get("reused"):
            continue
        stage = stage_by_guid.get(guid)
        path = next((p for g, p in payloads if g == guid), None)
        if not stage or path is None:
            continue
        key = cache.key_of(path)
        if key is None:
            continue
        cache.remember(key, stage=stage, result=result, path=path)


__all__ = ["merge", "split", "store"]
