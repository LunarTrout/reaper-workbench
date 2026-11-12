"""Limiter stage: drive a broadband limiter until the policy is met.

The search is measured, not assumed: the limiter's own parameters are
calibrated against the values REAPER reports, then the threshold is moved and
the rendered master is measured after every step.  The report includes the
loudness/PLR trade-off, because hitting a loudness target with a limiter is
always a trade and the number of dB of limiting is the evidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import eq


@dataclass
class LimiterReport:
    lufs_before: float
    lufs_after: float
    true_peak_after: float
    plr_before: float
    plr_after: float
    target_lufs: float
    ceiling_dbtp: float
    threshold_db: float
    iterations: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (abs(self.lufs_after - self.target_lufs) <= 0.5
                and self.true_peak_after <= self.ceiling_dbtp + 0.05)

    def to_dict(self) -> dict:
        return {"lufs_before": self.lufs_before, "lufs_after": self.lufs_after,
                "true_peak_after": self.true_peak_after,
                "plr_before": self.plr_before, "plr_after": self.plr_after,
                "target_lufs": self.target_lufs, "ceiling_dbtp": self.ceiling_dbtp,
                "threshold_db": self.threshold_db, "ok": self.ok,
                "iterations": self.iterations}


def _params(session, track_guid: str, fx_guid: str) -> list[dict]:
    return session.bridge.call("fx.list_params",
                               {"track_guid": track_guid, "fx_guid": fx_guid})["parameters"]


def _index(parameters: list[dict], name: str) -> int:
    for p in parameters:
        if str(p.get("name", "")).strip().lower() == name.lower():
            return int(p["index"])
    raise KeyError(f"the limiter has no {name!r} parameter")


def calibrate(session, track_guid: str, fx_guid: str, index: int,
              *, samples: int = 5) -> eq.Calibration:
    return eq.calibrate_parameter(session, track_guid, fx_guid, index, samples=samples)


def ensure_limiter(session, *, track_guid: str, plugin: str = "VST: ReaLimit (Cockos)") -> str:
    """Return the limiter's GUID on a track or the master, inserting it if absent."""
    from .session import EditOperation

    def find() -> str | None:
        context = session.snapshot(include_items=True)
        containers = list(context.get("tracks", [])) + ([context["master"]]
                                                        if context.get("master") else [])
        for container in containers:
            if container.get("guid") != track_guid:
                continue
            for fx in container.get("fx", []):
                if fx.get("name") == plugin:
                    return fx["guid"]
        return None

    existing = find()
    if existing:
        return existing
    context = session.snapshot(include_items=True)
    known = {t["guid"] for t in context.get("tracks", [])}
    if track_guid not in known and context.get("master", {}).get("guid") != track_guid:
        raise ValueError("track_guid is neither a track nor the master")
    session.apply(session.plan([EditOperation(kind="fx.insert", target_guid=track_guid,
                                              values={"name": plugin})],
                               reason="limiter: insert"), dry_run=False)
    created = find()
    if not created:
        raise RuntimeError("the limiter did not appear in the chain")
    return created


def _set(session, track_guid: str, fx_guid: str, index: int, normalized: float) -> None:
    eq._set(session, track_guid, fx_guid, index, float(np.clip(normalized, 0.0, 1.0)))


def _measure(render, job: str) -> dict:
    from .delivery import measure_delivery
    return measure_delivery(render(job))


def _search_threshold(session, track_guid: str, fx_guid: str, threshold_index: int,
                      target_lufs: float, render, tolerance_lu: float,
                      max_iterations: int) -> tuple[dict, float]:
    """Bisect the normalised threshold until the measured loudness meets target."""
    _set(session, track_guid, fx_guid, threshold_index, 0.2)
    probe_low = _measure(render, "limiter_probe_low")
    _set(session, track_guid, fx_guid, threshold_index, 0.8)
    probe_high = _measure(render, "limiter_probe_high")
    louder_at_low = probe_low["lufs"] > probe_high["lufs"]

    best_norm = 0.2 if louder_at_low else 0.8
    best = probe_low if louder_at_low else probe_high
    lo, hi = 0.0, 1.0
    for _ in range(max_iterations):
        mid = (lo + hi) / 2
        _set(session, track_guid, fx_guid, threshold_index, mid)
        measured = _measure(render, "limiter_step")
        if abs(measured["lufs"] - target_lufs) < abs(best["lufs"] - target_lufs):
            best_norm, best = mid, measured
        if abs(measured["lufs"] - target_lufs) <= tolerance_lu:
            break
        # With louder_at_low, loudness falls as the normalised threshold rises.
        need_higher_norm = ((measured["lufs"] > target_lufs) if louder_at_low
                            else (measured["lufs"] < target_lufs))
        if need_higher_norm:
            lo = mid
        else:
            hi = mid
    return best, best_norm


def drive_to_target(session, *, track_guid: str, fx_guid: str, target_lufs: float,
                    ceiling_dbtp: float, render, tolerance_lu: float = 0.3,
                    max_iterations: int = 9) -> LimiterReport:
    """Set the ceiling, bisect the threshold, correct ceiling overshoot, repeat."""
    params = _params(session, track_guid, fx_guid)
    ceiling_index = _index(params, "Ceiling")
    threshold_index = _index(params, "Threshold")
    ceiling_cal = calibrate(session, track_guid, fx_guid, ceiling_index)
    threshold_cal = calibrate(session, track_guid, fx_guid, threshold_index)

    baseline = _measure(render, "limiter_before")
    ceiling_db = float(ceiling_dbtp)
    best: dict = baseline
    iterations: list[dict] = []
    for _ in range(3):
        _set(session, track_guid, fx_guid, ceiling_index,
             ceiling_cal.to_normalized(ceiling_db))
        best, _norm = _search_threshold(session, track_guid, fx_guid, threshold_index,
                                        target_lufs, render, tolerance_lu, max_iterations)
        iterations.append({"ceiling_db": ceiling_db, "lufs": best["lufs"],
                           "true_peak_dbtp": best["true_peak_dbtp"]})
        if best["true_peak_dbtp"] <= ceiling_dbtp + 0.05:
            break
        ceiling_db -= (best["true_peak_dbtp"] - ceiling_dbtp) + 0.1

    threshold_db = float(threshold_cal.samples[len(threshold_cal.samples) // 2][1])
    return LimiterReport(
        lufs_before=baseline["lufs"], lufs_after=best["lufs"],
        true_peak_after=best["true_peak_dbtp"], plr_before=baseline["plr_db"],
        plr_after=best["plr_db"], target_lufs=target_lufs,
        ceiling_dbtp=ceiling_dbtp, threshold_db=threshold_db,
        iterations=iterations)
