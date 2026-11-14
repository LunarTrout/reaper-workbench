"""Bounded render-to-analysis hand-off for full capture.

The REAPER bridge is single-threaded: one render RPC at a time.  DSP work runs
in a spawn process pool, so it can proceed while the calling thread is blocked
inside the next render.  This module is only the hand-off; it never calls the
bridge and never returns decoded samples to the parent.

Back-pressure is explicit: at most ``workers`` futures are in flight, and the
ready buffer holds only the tracks admitted but not yet submitted (at most one
planned batch).  A small dispatcher thread refills freed worker slots while the
main thread is inside a render RPC.
"""

from __future__ import annotations

import multiprocessing
import threading
import time
from collections import deque
from collections.abc import Iterable, Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from typing import Any, Callable

from ..errors import ValidationError
from .analysis_workers import analyze_asset_job

ExecutorFactory = Callable[[int], Any]


def _spawn_executor(workers: int) -> ProcessPoolExecutor:
    context = multiprocessing.get_context("spawn")
    return ProcessPoolExecutor(max_workers=workers, mp_context=context)


class BoundedAnalysisPool:
    """A bounded producer/consumer bridge between admission and DSP workers."""

    def __init__(self, workers: int, *,
                 executor_factory: ExecutorFactory | None = None) -> None:
        if (isinstance(workers, bool) or not isinstance(workers, int) or
                workers < 1):
            raise ValidationError("analysis pool workers must be a positive integer")
        self.workers = workers
        self._lock = threading.Condition()
        self._ready: deque[tuple[str, str]] = deque()
        self._futures: dict[Future, str] = {}
        self._results: dict[str, dict[str, Any]] = {}
        self._completion_times: dict[str, float] = {}
        self._error: BaseException | None = None
        self._accepting = True
        self._stop = False
        self._peak_in_flight = 0
        self._ready_peak = 0
        self._submitted = 0
        self._created = time.perf_counter()
        self._executor = (executor_factory or _spawn_executor)(workers)
        self._thread = threading.Thread(
            target=self._dispatch_loop,
            name="capture-analysis-dispatch",
            daemon=True,
        )
        self._thread.start()

    # ------------------------------------------------------------------ #
    # producer
    # ------------------------------------------------------------------ #
    def submit(self, payloads: Iterable[tuple[str, str]]) -> None:
        """Admit files for analysis without blocking on worker availability."""
        batch = list(payloads)
        if not batch:
            return
        with self._lock:
            self._raise_if_failed_locked()
            if not self._accepting:
                raise RuntimeError("analysis pool no longer accepts payloads")
            self._ready.extend(batch)
            self._ready_peak = max(self._ready_peak, len(self._ready))
            self._pump_locked()
            self._lock.notify_all()

    def finish(self) -> None:
        """Declare that no further payloads will be submitted."""
        with self._lock:
            self._accepting = False
            self._lock.notify_all()

    # ------------------------------------------------------------------ #
    # consumer
    # ------------------------------------------------------------------ #
    def wait_all(self) -> dict[str, dict[str, Any]]:
        """Wait for every admitted payload and return results by GUID."""
        with self._lock:
            while True:
                self._raise_if_failed_locked()
                if (not self._accepting and not self._ready and
                        not self._futures):
                    return dict(self._results)
                self._lock.wait(0.05)

    def result_list(self, guids: Sequence[str]) -> list[dict[str, Any]]:
        """Return results in the caller's deterministic GUID order."""
        with self._lock:
            missing = [guid for guid in guids if guid not in self._results]
            if missing:
                raise RuntimeError(
                    "Analysis results are incomplete: "
                    + ", ".join(sorted(missing)[:8]))
            return [dict(self._results[guid]) for guid in guids]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "workers": self.workers,
                "submitted": self._submitted,
                "completed": len(self._results),
                "peak_in_flight": self._peak_in_flight,
                "ready_buffer_high_water": self._ready_high_water(),
                "first_completion_s": (
                    min(self._completion_times.values()) - self._created
                    if self._completion_times else None),
                "last_completion_s": (
                    max(self._completion_times.values()) - self._created
                    if self._completion_times else None),
                "completion_times_s": {
                    guid: value - self._created
                    for guid, value in self._completion_times.items()
                },
                "pool_open_s": time.perf_counter() - self._created,
            }

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def close(self, *, cancel: bool = False, wait_workers: bool = True) -> None:
        with self._lock:
            self._accepting = False
            if cancel:
                for future in self._futures:
                    future.cancel()
                self._futures.clear()
                self._ready.clear()
                if self._error is None:
                    self._error = RuntimeError("analysis pool cancelled")
            self._stop = self._stop or cancel
            self._lock.notify_all()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._executor.shutdown(wait=wait_workers and not cancel,
                                cancel_futures=cancel)

    def abort(self) -> None:
        """Cancel queued work and stop waiting for workers (failure path)."""
        self.close(cancel=True, wait_workers=False)

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _ready_high_water(self) -> int:
        return getattr(self, "_ready_peak", 0)

    def _raise_if_failed_locked(self) -> None:
        if self._error is not None:
            raise RuntimeError("analysis worker failed") from self._error

    def _pump_locked(self) -> None:
        while (self._ready and len(self._futures) < self.workers and
               self._error is None):
            guid, path = self._ready.popleft()
            try:
                future = self._executor.submit(analyze_asset_job, (guid, path))
            except BaseException as exc:  # noqa: BLE001 - propagated to caller
                self._error = exc
                self._ready.clear()
                self._lock.notify_all()
                return
            self._futures[future] = guid
            self._submitted += 1
            self._peak_in_flight = max(self._peak_in_flight, len(self._futures))

    def _collect_locked(self) -> None:
        done = [future for future in self._futures if future.done()]
        for future in done:
            guid = self._futures.pop(future)
            try:
                result = future.result()
                if not isinstance(result, dict):
                    raise RuntimeError("analysis worker returned a non-object")
                if result.get("source_guid") != guid:
                    raise RuntimeError(
                        "analysis worker returned a different source GUID")
                if guid in self._results:
                    raise RuntimeError("analysis worker returned a duplicate GUID")
                self._results[guid] = result
                self._completion_times[guid] = time.perf_counter()
            except BaseException as exc:  # noqa: BLE001 - propagated to caller
                if self._error is None:
                    self._error = exc
                for other in self._futures:
                    other.cancel()
                self._futures.clear()
                self._ready.clear()
                self._lock.notify_all()
                return
        self._pump_locked()

    def _dispatch_loop(self) -> None:
        while True:
            with self._lock:
                self._collect_locked()
                if self._stop or self._error is not None:
                    return
                if (not self._accepting and not self._ready and
                        not self._futures):
                    self._lock.notify_all()
                    return
                self._lock.wait(0.05)


__all__ = ["BoundedAnalysisPool"]
