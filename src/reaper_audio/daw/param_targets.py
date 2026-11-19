"""Resolve a human-readable parameter target ("25 ms", "3:1") to normalized.

Normalized values are opaque: 0.27 means nothing until it is read back as
"25 ms", and guessing the mapping is how a mix ends up with a 0.48 ms pre-delay.
This module turns a written target into the normalized value a plug-in expects,
using a calibration table measured beforehand (see
``scripts/calibrate_params.py``).  No table means no answer — it never guesses.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

_TARGET = re.compile(r"^\s*([-+]?\d+(?:[.,]\d+)?)\s*([a-zA-Z%:/0-9]+)?\s*$")

# value = number * factor (to the base unit of its kind)
_UNIT_FACTORS = {
    "hz": ("hz", 1.0), "khz": ("hz", 1000.0),
    "ms": ("s", 0.001), "s": ("s", 1.0),
    "db": ("db", 1.0), "db/oct": ("db/oct", 1.0),
    "%": ("%", 1.0), ":1": (":1", 1.0), "x": ("x", 1.0),
    "": (None, 1.0),
}


def parse_target(text: str) -> tuple[float, str | None]:
    """Parse "25 ms" / "7 kHz" / "3:1" / "-2.5 dB" into (value, base unit)."""
    if not isinstance(text, str):
        raise ValueError("target must be a string")
    match = _TARGET.match(text)
    if not match:
        raise ValueError(f"cannot parse target {text!r}")
    number = float(match.group(1).replace(",", "."))
    unit = (match.group(2) or "").lower()
    if unit.startswith(":"):  # "3:1" is split by the regex as ":1"
        unit = ":1"
    base, factor = _UNIT_FACTORS.get(unit, (unit, 1.0))
    return number * factor, base


def _entry_value(entry: Mapping[str, Any]) -> tuple[float, str | None] | None:
    raw = entry.get("formatted")
    if not isinstance(raw, str):
        return None
    try:
        return parse_target(raw)
    except ValueError:
        return None


def resolve(table: Sequence[Mapping[str, Any]], target: str) -> float:
    """Pick the normalized value whose measured display is closest to target."""
    wanted, wanted_unit = parse_target(target)
    points: list[tuple[float, float]] = []
    for entry in table:
        parsed = _entry_value(entry)
        normalized = entry.get("normalized")
        if parsed is None or not isinstance(normalized, (int, float)):
            continue
        value, unit = parsed
        if wanted_unit is not None and unit is not None and unit != wanted_unit:
            continue
        points.append((float(normalized), value))
    if not points:
        raise ValueError(f"calibration has no values comparable to {target!r}")

    points.sort(key=lambda pair: pair[1])
    best = min(points, key=lambda pair: abs(pair[1] - wanted))
    if abs(best[1] - wanted) < 1e-9:
        return best[0]
    # Linear interpolation between the two neighbours that bracket the target.
    lower = max((p for p in points if p[1] <= wanted), default=None)
    upper = min((p for p in points if p[1] >= wanted), default=None)
    if lower and upper and upper[1] != lower[1]:
        fraction = (wanted - lower[1]) / (upper[1] - lower[1])
        return lower[0] + (upper[0] - lower[0]) * fraction
    return best[0]


def describe(table: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    points = []
    for entry in table:
        parsed = _entry_value(entry)
        if parsed is None:
            continue
        points.append({"normalized": entry.get("normalized"),
                       "value": parsed[0], "unit": parsed[1],
                       "formatted": entry.get("formatted")})
    return {"points": points, "count": len(points)}
