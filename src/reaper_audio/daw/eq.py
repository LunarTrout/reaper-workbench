"""Turn measured band deltas into real EQ moves on a plugin, and read back.

The module assumes nothing about the plugin's parameter curves.  It reads the
plugin's own parameter list, calibrates each band parameter against the values
REAPER reports, and then writes normalised values.  Nothing here decides *how
much* to correct: the caller supplies the deltas, which come from the corpus
model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..errors import ValidationError

# Parameter-name patterns used by ReaEQ-style parametric EQs.  These are
# *names*, not values: they only identify which parameter controls what.
_PREFIXES = (("freq", ("freq-", "frequency-")), ("gain", ("gain-",)), ("bw", ("bw-", "bandwidth-", "q-")))


def classify(name: str) -> tuple[str, str] | None:
    """Split a parameter name into (kind, band key).

    The band key is whatever follows the prefix ("low shelf", "band 2", ...).
    It groups parameters that belong to the same band without assuming a fixed
    numbering or a fixed number of bands.
    """
    lowered = name.strip().lower().replace("_", " ").replace("-", " ")
    for kind, prefixes in _PREFIXES:
        for prefix in prefixes:
            prefix = prefix.replace("-", " ")
            if lowered.startswith(prefix):
                key = lowered[len(prefix):].strip()
                return (kind, key) if key else None
    return None


@dataclass(frozen=True)
class ParamRef:
    kind: str
    band: str
    index: int
    name: str


_FILTER_WORDS = ("pass", "notch", "band pass", "low pass", "high pass")


def is_filter_band(band: str) -> bool:
    lowered = band.lower()
    return any(word in lowered for word in _FILTER_WORDS)


def band_type(band: str) -> str:
    """Classify a band key by its control name: peaking, shelf or filter."""
    lowered = band.lower()
    if is_filter_band(lowered):
        return "filter"
    if "shelf" in lowered:
        return "shelf"
    return "peaking"


def layout(parameters: list[dict]) -> dict[str, dict[str, ParamRef]]:
    bands: dict[str, dict[str, ParamRef]] = {}
    for p in parameters:
        parsed = classify(str(p.get("name", "")))
        if not parsed:
            continue
        kind, band = parsed
        bands.setdefault(band, {})[kind] = ParamRef(kind, band, int(p["index"]), str(p["name"]))
    return bands


def _read_value(session, track_guid: str, fx_guid: str, index: int) -> tuple[float, str]:
    params = session.bridge.call("fx.list_params",
                                 {"track_guid": track_guid, "fx_guid": fx_guid})
    for p in params.get("parameters", []):
        if int(p["index"]) == index:
            return float(p["normalized"]), str(p.get("formatted", ""))
    raise ValidationError("parameter index disappeared from the plugin", details={"index": index})


def _set(session, track_guid: str, fx_guid: str, index: int, normalized: float) -> None:
    from .session import EditOperation
    op = EditOperation(kind="fx.set_parameter", target_guid=track_guid,
                       values={"fx_guid": fx_guid, "parameter_index": index,
                               "normalized": float(normalized)})
    session.apply(session.plan([op], reason="calibrate/apply EQ move"), dry_run=False)


def _parse_float(text: str) -> float | None:
    cleaned = text.strip().lower().replace(",", ".")
    for suffix in ("db", "hz", "khz", "oct", "x"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if "khz" in text.lower():
        value *= 1000.0
    return value


@dataclass
class Calibration:
    """Piecewise-linear map between a normalised parameter and its displayed value.

    The curve is the plugin's own: we sample it, drop non-finite points and
    interpolate.  Frequencies are interpolated in log space because that is how
    the control is laid out; gain is linear.
    """

    index: int
    samples: list[tuple[float, float]]
    log_y: bool

    def _ys(self) -> list[float]:
        if self.log_y:
            return [math.log(max(y, 1e-9)) for _, y in self.samples]
        return [y for _, y in self.samples]

    def to_normalized(self, target: float) -> float:
        xs = [x for x, _ in self.samples]
        ys = self._ys()
        y = math.log(max(target, 1e-9)) if self.log_y else target
        if len(xs) == 1:
            return xs[0]
        # np.interp needs increasing x; sampling is monotone by construction.
        order = sorted(range(len(xs)), key=lambda i: ys[i])
        ys_sorted = [ys[i] for i in order]
        xs_sorted = [xs[i] for i in order]
        return float(np.interp(y, ys_sorted, xs_sorted))


def calibrate(session, track_guid: str, fx_guid: str, *, samples: int = 5,
              kinds: tuple[str, ...] = ("freq", "gain")) -> dict[str, dict[str, Calibration]]:
    """Calibrate every band parameter by asking the plugin for its own values."""
    params = session.bridge.call("fx.list_params",
                                 {"track_guid": track_guid, "fx_guid": fx_guid})
    bands = layout(params.get("parameters", []))
    out: dict[str, dict[str, Calibration]] = {}
    grid = [i / (samples - 1) for i in range(samples)] if samples > 1 else [0.5]
    for band, band_kinds in bands.items():
        out[band] = {}
        for kind, ref in band_kinds.items():
            if kind not in kinds:
                continue
            original, _ = _read_value(session, track_guid, fx_guid, ref.index)
            points: list[tuple[float, float]] = []
            for norm in grid:
                _set(session, track_guid, fx_guid, ref.index, norm)
                _, formatted = _read_value(session, track_guid, fx_guid, ref.index)
                value = _parse_float(formatted)
                if value is None or not math.isfinite(value):
                    continue
                if kind != "gain" and value <= 0:
                    continue
                points.append((norm, value))
            _set(session, track_guid, fx_guid, ref.index, original)
            if len(points) >= 2:
                out[band][kind] = Calibration(ref.index, sorted(points), log_y=(kind != "gain"))
    return out


def plan_moves(deltas: dict[float, float], calibration: dict[str, dict[str, Calibration]],
               *, max_bands: int) -> list[dict]:
    """Assign each requested band correction to the nearest free EQ band.

    ``deltas`` maps frequency (Hz) to dB correction (positive = boost).  The
    number of moves is limited by the bands the plugin actually exposes.
    """
    # Only peaking bands take arbitrary frequency + gain moves.  Shelves keep
    # their corner: moving a high shelf down to 160 Hz is not a mix move, it is
    # a different filter, and the bench showed exactly that failure.
    free = [b for b, kinds in calibration.items()
            if "freq" in kinds and "gain" in kinds and band_type(b) == "peaking"]
    used: set[str] = set()
    moves: list[dict] = []
    for freq in sorted(deltas, key=lambda f: -abs(deltas[f])):
        if len(moves) >= max_bands:
            break
        candidates = [b for b in free if b not in used]
        if not candidates:
            break
        band = min(candidates, key=lambda b: abs(math.log(freq / _band_freq(calibration[b]))))
        used.add(band)
        moves.append({"band": band, "freq_hz": float(freq), "gain_db": float(deltas[freq])})
    return moves


def _band_freq(band: dict[str, Calibration]) -> float:
    cal = band["freq"]
    return cal.samples[len(cal.samples) // 2][1]


def apply_moves(session, track_guid: str, fx_guid: str, calibration: dict[str, dict[str, Calibration]],
                moves: list[dict], *, reason: str, flatten: bool = True) -> dict:
    from .session import EditOperation
    ops = []
    moved = {move["band"] for move in moves}
    # Start from a flat chain: any calibrated peaking/shelf band that is not
    # part of this move set is returned to 0 dB so runs do not accumulate.
    # During a search pass the caller sets flatten=False to keep accepted moves.
    for band_key, band_kinds in (calibration.items() if flatten else []):
        if band_key in moved or is_filter_band(band_key):
            continue
        gain = band_kinds.get("gain")
        if gain is None:
            continue
        ops.append(EditOperation(kind="fx.set_parameter", target_guid=track_guid,
                                 values={"fx_guid": fx_guid,
                                         "parameter_index": gain.index,
                                         "normalized": gain.to_normalized(0.0)}))
    for move in moves:
        band = calibration[move["band"]]
        freq_norm = band["freq"].to_normalized(move["freq_hz"])
        gain_norm = band["gain"].to_normalized(move["gain_db"])
        ops.append(EditOperation(kind="fx.set_parameter", target_guid=track_guid,
                                 values={"fx_guid": fx_guid,
                                         "parameter_index": band["freq"].index,
                                         "normalized": freq_norm}))
        ops.append(EditOperation(kind="fx.set_parameter", target_guid=track_guid,
                                 values={"fx_guid": fx_guid,
                                         "parameter_index": band["gain"].index,
                                         "normalized": gain_norm}))
    if not ops:
        raise ValidationError("no EQ moves to apply")
    return session.apply(session.plan(ops, reason=reason), dry_run=False)
