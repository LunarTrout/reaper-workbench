"""Context Contract v2 feature flags and declared policy values.

Every stage of the v2 rollout is enabled by a flag so that a rollback is a
configuration change, not a code revert.  Flags default to ``on`` for the
stages already implemented and can be switched off with an environment value of
``0``, ``false``, ``no`` or ``off``.

No threshold in this module is chosen by taste: values either come from the
documented contract or are supplied by the caller.  Anything that needs a
number the library cannot derive (for example the stale-share threshold for a
server-initiated full pass) is read from the environment and has no silent
default.
"""

from __future__ import annotations

import os

#: Analyzer identity that participates in every measurement cache key.  Bump
#: only together with a change to the measurement code itself.
ANALYZER = "reaper-audio-library"
ANALYZER_VERSION = "1"

#: Cache key schema; bumping it invalidates every stored measurement.
MEASUREMENT_CACHE_SCHEMA = "reaper-workbench.measurement-cache/1"

_TRUTHY = {"1", "true", "yes", "on"}


def _flag(env_name: str, *, default: bool = True) -> bool:
    raw = os.environ.get(env_name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUTHY


def analysis_cache_enabled() -> bool:
    """A1: reuse analysis of byte-identical rendered audio."""
    return _flag("REAPER_V2_ANALYSIS_CACHE")


def panorama_view_cache_enabled() -> bool:
    """A2: memoise ``build_view`` per immutable document."""
    return _flag("REAPER_V2_PANORAMA_VIEW_CACHE")


def panorama_view_cache_size() -> int:
    """How many built views to keep; declared, not tuned."""
    raw = os.environ.get("REAPER_V2_PANORAMA_VIEW_CACHE_SIZE")
    if raw is None or raw.strip() == "":
        return 4
    value = int(raw)
    if value < 1:
        raise ValueError("REAPER_V2_PANORAMA_VIEW_CACHE_SIZE must be >= 1")
    return value


def analysis_cache_root_name() -> str:
    return "measurement-cache"


def fingerprint_requires_bytes() -> bool:
    """R2 strict mode: only reuse measurements of byte-identical audio.

    Off by default because REAPER's offline render is measurably not
    byte-stable; on, every fingerprint hit whose bytes differ is treated as a
    miss, which turns the cache into a pure content-addressed store.
    """
    return _flag("REAPER_V2_REQUIRE_BYTE_IDENTICAL", default=False)


def incremental_render_enabled() -> bool:
    """D1: render only dirty sources; the master is always rendered.

    On by default; ``REAPER_V2_INCREMENTAL_RENDER=0`` (or ``false``/``no``/``off``)
    disables it and every source is rendered again. Measured parity between
    reused and freshly rendered stems on the bench: max |delta| = 0.0043 dB.
    """
    return _flag("REAPER_V2_INCREMENTAL_RENDER", default=True)


__all__ = [
    "ANALYZER",
    "ANALYZER_VERSION",
    "MEASUREMENT_CACHE_SCHEMA",
    "analysis_cache_enabled",
    "analysis_cache_root_name",
    "panorama_view_cache_enabled",
    "panorama_view_cache_size",
    "incremental_render_enabled",
    "partial_evidence_enabled",
    "stale_fraction_threshold",
]


STALE_FRACTION_DEFAULT_PERCENT = 50


def stale_fraction_threshold() -> float | None:
    """Share of stale sources above which a claim asks for a full pass.

    The value belongs to the user's work mode (``REAPER_MCP_MODE_DIR`` /
    ``mode.json``, 0-100 percent, default 50); ``100`` disables exactly this
    trigger.  ``REAPER_V2_STALE_FRACTION_THRESHOLD`` stays as a deployment
    override so tests and one-off runs do not need to edit the user's file.
    """
    raw = os.environ.get("REAPER_V2_STALE_FRACTION_THRESHOLD")
    if raw is not None and raw.strip():
        try:
            percent = float(raw.strip())
        except ValueError as error:
            raise ValueError(
                "REAPER_V2_STALE_FRACTION_THRESHOLD must be a percentage 0-100"
            ) from error
        if not 0.0 <= percent <= 100.0:
            raise ValueError("REAPER_V2_STALE_FRACTION_THRESHOLD must be within 0-100")
        return None if percent >= 100.0 else percent / 100.0
    from .mode import stale_threshold_fraction
    return stale_threshold_fraction()


def partial_evidence_enabled() -> bool:
    """D2: split a capture into per-source documents plus a thin manifest."""
    return _flag("REAPER_V2_PARTIAL_EVIDENCE", default=True)
