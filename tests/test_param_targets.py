"""Value targets: "25 ms" must resolve from a measured table, never by guess."""

from __future__ import annotations

import pytest

from reaper_audio.daw.param_targets import parse_target, resolve

PREDELAY = [
    {"normalized": 0.0, "formatted": "0.00 ms"},
    {"normalized": 0.1, "formatted": "2.38 ms"},
    {"normalized": 0.2, "formatted": "11.91 ms"},
    {"normalized": 0.3, "formatted": "30.54 ms"},
    {"normalized": 0.5, "formatted": "100.00 ms"},
]
RATIO = [
    {"normalized": 0.4, "formatted": "2.00:1"},
    {"normalized": 0.5, "formatted": "2.75:1"},
    {"normalized": 0.6, "formatted": "4.00:1"},
]


def test_parse_target_units() -> None:
    assert parse_target("3:1") == (3.0, ":1")
    assert parse_target("25 ms") == (0.025, "s")
    assert parse_target("7 kHz") == (7000.0, "hz")
    assert parse_target("-2.5 dB") == (-2.5, "db")


def test_resolve_interpolates_between_measured_points() -> None:
    normalized = resolve(PREDELAY, "25 ms")
    assert 0.2 < normalized < 0.3
    assert abs(normalized - 0.27) < 0.01


def test_resolve_picks_the_nearest_discrete_ratio() -> None:
    assert resolve(RATIO, "3:1") == pytest.approx(0.52, abs=0.01)
    assert resolve(RATIO, "10:1") == pytest.approx(0.6)


def test_resolve_refuses_incomparable_units() -> None:
    with pytest.raises(ValueError):
        resolve(PREDELAY, "3:1")


def test_resolve_refuses_an_empty_table() -> None:
    with pytest.raises(ValueError):
        resolve([], "25 ms")
