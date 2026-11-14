"""One memory-aware policy for every production analysis process pool.

The capture path (``FullCaptureService``) and the bundle path
(``ProjectSession.analyze``) must not choose worker counts independently.
This module is the single selector.  It keeps the measured hard ceiling at
eight workers, chooses a conservative default by capture window, and refuses
explicit counts that would exceed the available-memory envelope before any
process is started.

Measured envelope used below (DSP worker peak RSS):

* 20 s slice: about 0.38 GB per worker (stage-2 parallel benchmark)
* 300 s full render: about 3.96-4.29 GB per worker (offline profiles)

The linear envelope is deliberately conservative: workers for long or
unknown-duration captures are estimated at the full 300 s value.  The policy
budgets only 70 % of currently available physical memory.
"""

from __future__ import annotations

import ctypes
import math
import os
from dataclasses import dataclass
from typing import Any

from ..errors import ValidationError

MAX_ANALYSIS_WORKERS = 8
MIN_ANALYSIS_WORKERS = 1
SHORT_WINDOW_MAX_S = 30.0
SHORT_DEFAULT_WORKERS = 8
LONG_DEFAULT_WORKERS = 4
UNKNOWN_DEFAULT_WORKERS = 4

# Conservative worker RSS estimate: base plus per-second growth measured from
# the 20 s and 300 s datasets.  Unknown durations use the full 300 s value.
MEMORY_ENVELOPE_BASE_MB = 384.0
MEMORY_ENVELOPE_MB_PER_S = 14.0
MEMORY_ENVELOPE_UNKNOWN_S = 300.0
MEMORY_SAFETY_FRACTION = 0.70
_MB = 1024.0 * 1024.0


@dataclass(frozen=True, slots=True)
class AnalysisWorkerPlan:
    """The selected worker count and the evidence used to select it."""

    workers: int
    requested: int | None
    asset_count: int
    window_s: float | None
    window_class: str
    per_worker_envelope_mb: float
    memory_budget_mb: float | None
    memory_limit_workers: int | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "workers": self.workers,
            "requested": self.requested,
            "asset_count": self.asset_count,
            "window_s": self.window_s,
            "window_class": self.window_class,
            "per_worker_envelope_mb": round(self.per_worker_envelope_mb, 3),
            "memory_budget_mb": (None if self.memory_budget_mb is None
                                 else round(self.memory_budget_mb, 3)),
            "memory_limit_workers": self.memory_limit_workers,
            "reason": self.reason,
        }


def _system_available_memory_bytes() -> int | None:
    """Return currently available physical memory without a new dependency."""
    if os.name == "nt":
        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullAvailPhys)
        except (AttributeError, OSError, ValueError):
            return None
        return None
    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if isinstance(pages, int) and isinstance(page_size, int):
            return pages * page_size
    except (AttributeError, OSError, ValueError):
        return None
    return None


def estimate_worker_peak_mb(window_s: float | None) -> float:
    """Conservative per-worker RSS envelope for one capture window."""
    duration = MEMORY_ENVELOPE_UNKNOWN_S if window_s is None else float(window_s)
    return MEMORY_ENVELOPE_BASE_MB + MEMORY_ENVELOPE_MB_PER_S * duration


def available_memory_bytes() -> int | None:
    """Public wrapper for the platform memory probe."""
    return _system_available_memory_bytes()


def _validate_asset_count(asset_count: int) -> int:
    if isinstance(asset_count, bool) or not isinstance(asset_count, int) or asset_count < 0:
        raise ValidationError("asset_count must be a non-negative integer",
                              details={"asset_count": asset_count})
    return asset_count


def _validate_requested(requested: int | None) -> int | None:
    if requested is None:
        return None
    if (isinstance(requested, bool) or not isinstance(requested, int) or
            requested < MIN_ANALYSIS_WORKERS or requested > MAX_ANALYSIS_WORKERS):
        raise ValidationError(
            f"analysis_workers must be between {MIN_ANALYSIS_WORKERS} and "
            f"{MAX_ANALYSIS_WORKERS}",
            details={"requested": requested,
                     "max_safe_workers": MAX_ANALYSIS_WORKERS,
                     "hard_cap": MAX_ANALYSIS_WORKERS},
        )
    return requested


def validate_analysis_worker_request(requested: int | None) -> None:
    """Validate only the explicit hard-cap range, before a window is known."""
    _validate_requested(requested)


def _window_class(window_s: float | None) -> str:
    if window_s is None:
        return "unknown"
    if not isinstance(window_s, (int, float)) or isinstance(window_s, bool):
        raise ValidationError("window_s must be a finite positive number or null",
                              details={"window_s": window_s})
    if not math.isfinite(float(window_s)) or float(window_s) <= 0:
        raise ValidationError("window_s must be a finite positive number or null",
                              details={"window_s": window_s})
    return "short" if float(window_s) <= SHORT_WINDOW_MAX_S else "full"


def resolve_analysis_workers(
    *,
    asset_count: int,
    window_s: float | None = None,
    requested: int | None = None,
    available_memory_bytes: int | None = None,
) -> AnalysisWorkerPlan:
    """Select the analysis worker count for one capture or bundle.

    ``window_s`` is the measured project window per asset (section length or
    full project length).  ``None`` means the duration is unknown and is
    treated conservatively as a full 300 s input.  ``requested`` is a user
    override and is rejected when it exceeds either the hard cap or the
    memory-safe limit.
    """
    asset_count = _validate_asset_count(asset_count)
    requested = _validate_requested(requested)
    window_kind = _window_class(window_s)
    per_worker_mb = estimate_worker_peak_mb(window_s)

    memory_budget_mb: float | None = None
    memory_limit_workers: int | None = None
    if available_memory_bytes is None:
        available = _system_available_memory_bytes()
    else:
        available = available_memory_bytes
    if available is not None:
        if (isinstance(available, bool) or not isinstance(available, int) or
                available < 0):
            raise ValidationError("available_memory_bytes must be a non-negative integer",
                                  details={"available_memory_bytes": available})
        memory_budget_mb = (float(available) / _MB) * MEMORY_SAFETY_FRACTION
        memory_limit_workers = int(memory_budget_mb // per_worker_mb)

    if asset_count == 0:
        return AnalysisWorkerPlan(
            workers=0, requested=requested, asset_count=0, window_s=window_s,
            window_class=window_kind, per_worker_envelope_mb=per_worker_mb,
            memory_budget_mb=memory_budget_mb,
            memory_limit_workers=memory_limit_workers, reason="no_assets",
        )

    if memory_limit_workers is not None and memory_limit_workers < MIN_ANALYSIS_WORKERS:
        raise ValidationError(
            "Available memory cannot support one analysis worker",
            details={
                "available_memory_bytes": available,
                "memory_budget_mb": round(memory_budget_mb or 0.0, 3),
                "per_worker_envelope_mb": round(per_worker_mb, 3),
                "max_safe_workers": 0,
                "window_s": window_s,
                "window_class": window_kind,
                "requested": requested,
            },
        )

    if requested is not None:
        workers = min(requested, asset_count)
        if memory_limit_workers is not None and workers > memory_limit_workers:
            raise ValidationError(
                "analysis_workers exceeds the memory-safe limit for this capture",
                details={
                    "requested": requested,
                    "max_safe_workers": memory_limit_workers,
                    "memory_budget_mb": round(memory_budget_mb or 0.0, 3),
                    "per_worker_envelope_mb": round(per_worker_mb, 3),
                    "window_s": window_s,
                    "window_class": window_kind,
                },
            )
        reason = ("explicit_limited_by_asset_count" if workers < requested
                  else "explicit_override")
        return AnalysisWorkerPlan(
            workers=workers, requested=requested, asset_count=asset_count,
            window_s=window_s, window_class=window_kind,
            per_worker_envelope_mb=per_worker_mb,
            memory_budget_mb=memory_budget_mb,
            memory_limit_workers=memory_limit_workers, reason=reason,
        )

    default = (SHORT_DEFAULT_WORKERS if window_kind == "short"
               else UNKNOWN_DEFAULT_WORKERS if window_kind == "unknown"
               else LONG_DEFAULT_WORKERS)
    workers = min(default, asset_count, MAX_ANALYSIS_WORKERS)
    if memory_limit_workers is not None:
        workers = min(workers, memory_limit_workers)
    workers = max(MIN_ANALYSIS_WORKERS, workers)
    reason = ("short_window_default" if window_kind == "short"
              else "unknown_window_default" if window_kind == "unknown"
              else "long_window_default")
    return AnalysisWorkerPlan(
        workers=workers, requested=None, asset_count=asset_count,
        window_s=window_s, window_class=window_kind,
        per_worker_envelope_mb=per_worker_mb,
        memory_budget_mb=memory_budget_mb,
        memory_limit_workers=memory_limit_workers, reason=reason,
    )


__all__ = [
    "AnalysisWorkerPlan",
    "LONG_DEFAULT_WORKERS",
    "MAX_ANALYSIS_WORKERS",
    "MIN_ANALYSIS_WORKERS",
    "SHORT_DEFAULT_WORKERS",
    "SHORT_WINDOW_MAX_S",
    "UNKNOWN_DEFAULT_WORKERS",
    "available_memory_bytes",
    "estimate_worker_peak_mb",
    "resolve_analysis_workers",
    "validate_analysis_worker_request",
]
