"""A measurable EQ cycle: corpus deltas -> plugin moves -> render -> verdict.

The cycle never assumes a magnitude.  It builds candidate moves from the corpus
deviation, applies one at a time, renders the section through REAPER, measures
the distance, keeps a move only when the measured distance improves, and
restores loudness at the end.  Every decision is a comparison; there is no
threshold or weight in this file.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

from ..corpus.measure import BAND_CENTERS, measure_signal
from ..corpus.model import CorpusModel
from . import eq
from .session import EditOperation

DB = 10.0 / math.log(10.0)


@dataclass
class CycleResult:
    distance_before: float
    distance_after: float
    lufs_before: float
    lufs_after: float
    accepted: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    contributions: list[tuple[str, float, float]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "distance_before": self.distance_before,
            "distance_after": self.distance_after,
            "ratio": self.distance_after / self.distance_before if self.distance_before else None,
            "lufs_before": self.lufs_before,
            "lufs_after": self.lufs_after,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "contributions": [{"feature": n, "z": z, "share": s}
                              for n, z, s in self.contributions],
        }


def _render(session, output_dir: str | Path, job: str, start_s: float, end_s: float) -> Path:
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*.wav"):
        stale.unlink()
    revision = session.snapshot()["revision"]
    result = session.bridge.capture_render(
        expected_revision=revision, output_dir=str(directory), job_id=job,
        expected_track_count=len(session.snapshot()["tracks"]),
        section_start_s=start_s, section_end_s=end_s, master_only=True,
    )
    files = result.get("files", [])
    master = [f for f in files if "master" in str(f.get("kind", "")).lower()
              or "master" in str(f.get("path", "")).lower()]
    if not master and not files:
        raise RuntimeError("REAPER returned no render")
    return Path((master or files)[0]["path"])


def _measure(path: Path):
    x, sr = sf.read(str(path), dtype="float64", always_2d=True)
    return measure_signal(x, sr)


def _band_deltas(model: CorpusModel, values: np.ndarray) -> dict[float, float]:
    """Deviation of each third-octave band from the corpus centre, in dB."""
    ours = values[: len(BAND_CENTERS)]
    centre = model.center[: len(BAND_CENTERS)]
    return {float(BAND_CENTERS[i]): float(DB * (centre[i] - ours[i]))
            for i in range(len(BAND_CENTERS))}


def _calibrate_output_gain(session, track_guid: str, fx_guid: str):
    params = session.bridge.call("fx.list_params",
                                 {"track_guid": track_guid, "fx_guid": fx_guid})["parameters"]
    target = next((p for p in params
                   if str(p.get("name", "")).lower() in ("global gain", "output gain")), None)
    if target is None:
        return None
    original, _ = eq._read_value(session, track_guid, fx_guid, target["index"])
    points = []
    for norm in (0.0, 0.25, 0.5, 0.75, 1.0):
        eq._set(session, track_guid, fx_guid, target["index"], norm)
        _, formatted = eq._read_value(session, track_guid, fx_guid, target["index"])
        value = eq._parse_float(formatted)
        if value is not None and math.isfinite(value):
            points.append((norm, value))
    eq._set(session, track_guid, fx_guid, target["index"], original)
    if len(points) < 2:
        return None
    return eq.Calibration(target["index"], sorted(points), log_y=False)


def run_eq_cycle(session, *, track_guid: str, model: CorpusModel,
                 output_dir: str | Path, section_start_s: float, section_end_s: float,
                 plugin: str = "VST: ReaEQ (Cockos)", eq_count: int = 2,
                 max_moves: int | None = None) -> CycleResult:
    """Run one measured EQ pass on a track and report the verdict.

    ``max_moves`` is a budget, not a method constant: the caller decides how
    many moves this pass may consider.
    """
    chain = session.snapshot(include_items=True)["tracks"]
    track = next((t for t in chain if t["guid"] == track_guid), None)
    if track is None:
        raise ValueError("track_guid is not in the project")
    while len(track.get("fx", [])) < eq_count:
        session.apply(session.plan([EditOperation(kind="fx.insert", target_guid=track_guid,
                                                  values={"name": plugin})],
                                   reason="cycle: ensure FX chain"), dry_run=False)
        track = next(t for t in session.snapshot(include_items=True)["tracks"]
                     if t["guid"] == track_guid)
    fx_guids = [f["guid"] for f in track["fx"]][:eq_count]
    calibrations = {}
    for fx_guid in fx_guids:
        calibration = eq.calibrate(session, track_guid, fx_guid)
        if calibration:
            calibrations[fx_guid] = calibration
            eq.apply_moves(session, track_guid, fx_guid, calibration, [],
                           reason="cycle: flat start", flatten=True)
    # The output stage also belongs to the flat start: a previous cycle may have
    # left it boosted.
    for fx_guid in fx_guids:
        gain = _calibrate_output_gain(session, track_guid, fx_guid)
        if gain is not None:
            session.apply(session.plan(
                [EditOperation(kind="fx.set_parameter", target_guid=track_guid,
                               values={"fx_guid": fx_guid,
                                       "parameter_index": gain.index,
                                       "normalized": gain.to_normalized(0.0)})],
                reason="cycle: reset output gain"), dry_run=False)

    before = _measure(_render(session, output_dir, "before", section_start_s, section_end_s))
    distance_before = float(model.distance(before.values)[0])
    ordered = sorted(_band_deltas(model, before.values).items(), key=lambda kv: -abs(kv[1]))
    if max_moves is not None:
        ordered = ordered[:max_moves]

    accepted, rejected = [], []
    current = distance_before
    cursor = 0
    for fx_guid in fx_guids:
        if cursor >= len(ordered):
            break
        calibration = calibrations.get(fx_guid)
        if not calibration:
            continue
        take = dict(ordered[cursor:cursor + 2])
        moves = eq.plan_moves(take, calibration, max_bands=2)
        if not moves:
            continue
        for move in moves:
            eq.apply_moves(session, track_guid, fx_guid, calibration, [move],
                           reason="cycle: candidate", flatten=False)
            probe = _measure(_render(session, output_dir, "probe",
                                     section_start_s, section_end_s))
            distance = float(model.distance(probe.values)[0])
            record = {"fx": fx_guid, "band": move["band"],
                      "freq_hz": move["freq_hz"], "gain_db": move["gain_db"],
                      "distance": distance}
            if distance < current:
                accepted.append(record)
                current = distance
            else:
                gain = calibration[move["band"]]["gain"]
                session.apply(session.plan(
                    [EditOperation(kind="fx.set_parameter", target_guid=track_guid,
                                   values={"fx_guid": fx_guid,
                                           "parameter_index": gain.index,
                                           "normalized": gain.to_normalized(0.0)})],
                    reason="cycle: revert candidate"), dry_run=False)
                rejected.append(record)
        cursor += len(moves)

    after = _measure(_render(session, output_dir, "after", section_start_s, section_end_s))
    distance_after = float(model.distance(after.values)[0])

    gain = _calibrate_output_gain(session, track_guid, fx_guids[0])
    lufs_after = after.extras["integrated_lufs"]
    final = after
    if gain is not None:
        # The plugin's gain law is not linear, so the compensation is found by
        # measurement: bisection on the normalised parameter until the rendered
        # loudness matches the pre-cycle one within the measurement tolerance.
        target = before.extras["integrated_lufs"]
        lo, hi = 0.0, 1.0
        best = after
        for _ in range(6):
            mid = (lo + hi) / 2
            session.apply(session.plan(
                [EditOperation(kind="fx.set_parameter", target_guid=track_guid,
                               values={"fx_guid": fx_guids[0],
                                       "parameter_index": gain.index,
                                       "normalized": mid})],
                reason="cycle: loudness search"), dry_run=False)
            probe = _measure(_render(session, output_dir, "gain", section_start_s, section_end_s))
            best = probe
            lufs = probe.extras["integrated_lufs"]
            if abs(lufs - target) <= 0.1:
                break
            if lufs > target:
                hi = mid
            else:
                lo = mid
        final = best
        lufs_after = final.extras["integrated_lufs"]
        distance_after = float(model.distance(final.values)[0])
    contributions = model.contributions(final.values)[:8]

    return CycleResult(
        distance_before=distance_before,
        distance_after=distance_after,
        lufs_before=float(before.extras["integrated_lufs"]),
        lufs_after=float(lufs_after),
        accepted=accepted,
        rejected=rejected,
        contributions=contributions,
    )
