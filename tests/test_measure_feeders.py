"""A bus must be rendered together with the tracks that feed it."""

from __future__ import annotations

from reaper_audio.daw.measure import _feeder_guids

DIGEST = {"fields": {"tracks": [
    {"guid": "{A}", "sends": [{"dest_guid": "{BUS}"}]},
    {"guid": "{B}", "sends": [{"dest_guid": "{A}"}]},
    {"guid": "{C}", "sends": []},
    {"guid": "{BUS}", "sends": [{"dest_guid": "{MASTER}"}]},
]}}


def test_feeders_are_found_transitively() -> None:
    assert set(_feeder_guids(DIGEST, ["{BUS}"])) == {"{A}", "{B}"}


def test_a_track_without_feeders_has_none() -> None:
    assert _feeder_guids(DIGEST, ["{C}"]) == []
