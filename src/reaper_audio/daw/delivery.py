"""Delivery stage: measure loudness and true peak, trim the master, verify.

The library does not invent a loudness target.  A policy supplies it; this
module measures the rendered master, applies a gain trim on the master track,
re-renders and reports the result.  It never claims a limiter was used: if the
target cannot be reached without breaking the true-peak ceiling, that is
reported as "needs limiting", not hidden behind a gain change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import soundfile as sf

from ..corpus.measure import measure_signal
from .session import EditOperation


@dataclass
class DeliveryReport:
    lufs: float
    true_peak_dbtp: float
    plr_db: float
    target_lufs: float | None
    ceiling_dbtp: float | None
    ok: bool
    issues: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"lufs": self.lufs, "true_peak_dbtp": self.true_peak_dbtp,
                "plr_db": self.plr_db, "target_lufs": self.target_lufs,
                "ceiling_dbtp": self.ceiling_dbtp, "ok": self.ok,
                "issues": self.issues, "history": self.history}


def measure_delivery(path: str | Path) -> dict:
    x, sr = sf.read(str(path), dtype="float64", always_2d=True)
    measurement = measure_signal(x, sr)
    extras = measurement.extras
    return {"lufs": float(extras["integrated_lufs"]),
            "true_peak_dbtp": float(extras["true_peak_dbtp"]),
            "plr_db": float(extras["plr_db"])}


def status(lufs: float, true_peak: float, policy: dict) -> tuple[bool, list[str]]:
    issues = []
    target = policy.get("target_lufs")
    tolerance = policy.get("max_lufs_error", 0.2)
    ceiling = policy.get("max_true_peak_dbtp")
    if target is not None and abs(lufs - target) > tolerance:
        issues.append(f"loudness {lufs:.2f} against target {target:.2f}")
    if ceiling is not None and true_peak > ceiling:
        issues.append(f"true peak {true_peak:.2f} above {ceiling:.2f}")
    return (not issues), issues


def plan_master_trim(session, *, master_guid: str, delta_db: float, reason: str):
    context = session.snapshot()
    master = context.get("master") or {}
    current = float(master.get("volume") or 1.0)
    if current <= 0:
        raise ValueError("master volume is not a positive linear value")
    new_volume = current * (10.0 ** (delta_db / 20.0))
    return session.plan([EditOperation(kind="track.set", target_guid=master_guid,
                                       values={"volume": new_volume})], reason=reason)


def trim_to_target(session, *, master_guid: str, policy: dict, render,
                   max_iterations: int = 3) -> DeliveryReport:
    """Render, measure, trim, repeat; stop when the policy is satisfied.

    ``render(job_name) -> Path`` is supplied by the caller because the section
    and the output directory belong to the task, not to this module.
    """
    target = policy.get("target_lufs")
    ceiling = policy.get("max_true_peak_dbtp")
    history: list[dict] = []
    measured = measure_delivery(render("delivery"))
    for _ in range(max_iterations):
        ok, issues = status(measured["lufs"], measured["true_peak_dbtp"], policy)
        history.append({**measured, "ok": ok, "issues": issues})
        if ok or target is None:
            break
        delta = target - measured["lufs"]
        if abs(delta) <= float(policy.get("max_lufs_error", 0.2)):
            break
        plan = plan_master_trim(session, master_guid=master_guid, delta_db=delta,
                                reason="delivery: loudness trim")
        session.apply(plan, dry_run=False)
        measured = measure_delivery(render("delivery"))
    ok, issues = status(measured["lufs"], measured["true_peak_dbtp"], policy)
    if ceiling is not None and measured["true_peak_dbtp"] > ceiling:
        issues.append("needs limiting: gain alone cannot meet the ceiling")
    return DeliveryReport(lufs=measured["lufs"],
                          true_peak_dbtp=measured["true_peak_dbtp"],
                          plr_db=measured["plr_db"], target_lufs=target,
                          ceiling_dbtp=ceiling, ok=ok, issues=issues,
                          history=history)
