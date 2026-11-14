"""Shared digest payloads for the Wave B/C contract tests.

The Wave C digest requires concrete payloads for the master, automation,
project settings and serialized FX state.  These helpers provide the smallest
honest payloads so older Wave B fixtures can exercise scope/predicate behavior
without pretending a flag alone is evidence.
"""

from __future__ import annotations

PROJECT_SETTING_KEYS = (
    "render_settings", "render_channels", "render_srate", "render_boundsflag",
    "render_startpos", "render_endpos", "render_tailflag", "render_tailms",
    "render_addtoproj", "render_dither", "render_normalize",
    "render_normalize_target", "render_brickwall", "render_fadein",
    "render_fadeout", "render_fadeinshape", "render_fadeoutshape",
    "render_fadelpf", "render_padstart", "render_padend", "render_trimstart",
    "render_trimend", "render_delay", "project_srate", "project_srate_use",
    "project_timebase", "project_timebase_flags",
)

FULL_COVERAGE = (
    "tracks", "items", "takes", "fx", "sends", "tempo", "master",
    "automation", "envelopes", "project", "fx_state",
)


def project_settings() -> dict:
    values = {name: 0 for name in PROJECT_SETTING_KEYS}
    values.update(
        render_settings=0,
        render_channels=2,
        render_srate=48000,
        render_boundsflag=1,
        project_srate=48000,
        project_srate_use=1,
        project_timebase=0,
        render_fadeinshape=1,
        render_fadeoutshape=1,
    )
    return values


def full_extra_fields(track_guids, master_guid: str) -> dict:
    """Payloads for the groups Wave C added to ``DIGEST_REQUIRED_FIELDS``."""
    return {
        "master": {
            "guid": master_guid,
            "is_master": True,
            "volume": 1.0,
            "pan": 0.0,
            "mute": False,
            "solo": 0,
            "channel_count": 2,
            "main_send": True,
            "fx": [],
            "sends": [],
            "hardware_outputs": [],
        },
        "automation": {
            "tracks": [{"track_guid": guid, "envelopes": []}
                       for guid in track_guids],
            "master": [],
            "items": [],
            "fx": [],
            "complete": True,
        },
        "project": {"settings": project_settings(), "complete": True},
        "fx_state": {
            "algorithm": "rpp_fxchain_text_v1",
            "tracks": [
                {"track_guid": guid, "is_master": guid == master_guid,
                 "fx_count": 0, "chunk": "<FXCHAIN\n>", "complete": True}
                for guid in (*track_guids, master_guid)
            ],
            "complete": True,
        },
    }
