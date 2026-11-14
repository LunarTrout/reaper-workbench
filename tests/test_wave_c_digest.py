"""Wave C: a digest is only ``full`` when it can back every claim with a payload.

The digest under test is the shape ``project.digest`` produces (see
``scripts/reaper_bridge.lua``): per-track content, the master track, automation
(track/master/item/FX envelopes with points and automation items), render and
playback settings, and the serialized FX chain state.  Every test here is built
from plain dictionaries; none of them starts, connects to, or simulates REAPER.

The contract has two halves.  A sound-affecting value that the digest claims to
cover must move the fingerprint when it changes.  Metadata the digest does not
claim to cover must not, and a coverage flag without its payload must never buy
a ``full`` scope.
"""

from __future__ import annotations

import copy

import pytest

from reaper_audio.context.errors import RevisionMismatchError
from reaper_audio.context.revision import (DIGEST_COVERAGE_PATHS,
                                           DIGEST_REQUIRED_FIELDS, DIGEST_VERSION,
                                           digest_coverage_gaps, digest_evidence,
                                           require_same_content,
                                           revision_from_digest, same_content)

PROJECT = "reaper:wave-c-digest"

#: Exactly the ``GetSetProjectInfo`` keys the bridge puts in ``fields.project``.
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

COVERED_NAMES = ("tracks", "items", "takes", "fx", "sends", "tempo", "master",
                 "automation", "envelopes", "project", "fx_state")


def _settings() -> dict:
    values = {name: 0 for name in PROJECT_SETTING_KEYS}
    values.update(render_settings=0, render_channels=2, render_srate=0,
                  render_boundsflag=1, render_tailms=0, project_srate=48000,
                  project_srate_use=1, project_timebase=0,
                  render_fadeinshape=1, render_fadeoutshape=1)
    return values


def _envelope(guid: str, name: str, *, value: float = 1.0) -> dict:
    return {
        "guid": guid,
        "name": name,
        "chunk": f"<VOLENV2\nACT 1 -1\nARM 1\nPT 0 {value} 0\n>",
        "active": True,
        "visible": True,
        "armed": False,
        "point_count": 2,
        "automation_item_count": 1,
        "points": [
            {"index": 0, "time_s": 0.0, "value": value, "shape": 0, "tension": 0.0},
            {"index": 1, "time_s": 4.0, "value": 0.5, "shape": 1, "tension": 0.3},
        ],
        "automation_items": [{
            "index": 0, "position_s": 0.0, "length_s": 4.0, "start_offset_s": 0.0,
            "playrate": 1.0, "baseline": 0.5, "amplitude": 1.0,
            "loop_source": False, "mute": False, "pool_id": -1,
        }],
    }


def _digest() -> dict:
    """A digest shaped exactly like a fully covered ``project.digest``."""
    return {
        "project_id": PROJECT,
        "path": "C:/wave-c/digest.rpp",
        "change_count": 31,
        "digest_version": DIGEST_VERSION,
        "coverage": {name: True for name in COVERED_NAMES},
        "missing": [],
        "fields": {
            "tracks": [{
                "guid": "{TRACK-A}",
                "volume": 0.75,
                "pan": -0.2,
                "mute": False,
                "solo": 0,
                "folder_depth": 0,
                "fx": [{
                    "index": 0,
                    "guid": "{TRACK-FX-1}",
                    "name": "ReaEQ",
                    "enabled": True,
                    "offline": False,
                    "params": [{"index": 0, "normalized": 0.5}],
                }],
                "sends": [{"index": 0, "dest_guid": "{BUS-1}", "volume": 0.3,
                           "pan": 0.0, "mode": 0, "mute": False}],
                "hardware_outputs": [{"index": 0, "volume": 1.0, "pan": 0.0,
                                      "mute": False, "phase": False, "mono": False,
                                      "source_channels": 0,
                                      "destination_channels": 0, "send_mode": 0}],
            }],
            "items": [{
                "guid": "{ITEM-A}",
                "track_guid": "{TRACK-A}",
                "position_s": 4.0,
                "length_s": 8.0,
                "start_offset_s": 0.5,
                "playrate": 1.0,
                "mute": False,
                "fade_in_s": 0.01,
                "fade_out_s": 0.02,
                "take_guid": "{TAKE-A}",
                "source_path": "C:/audio/kick.wav",
                "loop_source": False,
                "beat_attach_mode": 0,
                "stretch_marker_positions": [{"pos": 1.0, "srcpos": 1.0}],
            }],
            "tempo": [{"time_s": 0.0, "bpm": 120.0, "numerator": 4,
                       "denominator": 4, "linear": False}],
            "master": {
                "guid": "{MASTER}",
                "is_master": True,
                "name": "",
                "volume": 1.0,
                "pan": 0.0,
                "mute": False,
                "solo": 0,
                "channel_count": 2,
                "main_send": True,
                "fx": [
                    {"index": 0, "guid": "{MASTER-FX-1}", "name": "ReaXcomp",
                     "enabled": True, "offline": False,
                     "params": [{"index": 0, "normalized": 0.5},
                                {"index": 1, "normalized": 0.25}]},
                    {"index": 1, "guid": "{MASTER-FX-2}", "name": "ReaComp",
                     "enabled": True, "offline": False,
                     "params": [{"index": 0, "normalized": 0.1}]},
                ],
                "sends": [],
                "hardware_outputs": [{"index": 0, "volume": 1.0, "pan": 0.0,
                                      "mute": False, "phase": False, "mono": False,
                                      "source_channels": 0,
                                      "destination_channels": 0, "send_mode": 0}],
            },
            "automation": {
                "tracks": [{
                    "track_guid": "{TRACK-A}",
                    "envelopes": [_envelope("{ENV-VOL}", "Volume")],
                }],
                "master": [],
                "items": [dict(_envelope("{ENV-TAKE}", "Take Volume"),
                               track_guid="{TRACK-A}", item_guid="{ITEM-A}",
                               take_guid="{TAKE-A}")],
                "fx": [dict(_envelope("{ENV-FX}", "ReaEQ - Band 1 Gain"),
                            track_guid="{TRACK-A}", is_master=False, fx_index=0,
                            parameter_index=0)],
                "items_scanned": 1,
                "complete": True,
                "gaps": [],
            },
            "project": {"settings": _settings(), "unread": [], "complete": True},
            "fx_state": {
                "algorithm": "rpp_fxchain_text_v1",
                "ui_lines_removed": ["WNDRECT", "SHOW", "LASTSEL", "DOCKED",
                                     "FLOATPOS", "WAK", "MULTIFXLINK"],
                "tracks": [
                    {"track_guid": "{TRACK-A}", "is_master": False, "fx_count": 1,
                     "chunk": "<FXCHAIN\nBYPASS 0 0 0\nFXID {TRACK-FX-1}\n>",
                     "complete": True},
                    {"track_guid": "{MASTER}", "is_master": True, "fx_count": 2,
                     "chunk": "<FXCHAIN\nBYPASS 0 0 0\nFXID {MASTER-FX-1}\n>",
                     "complete": True},
                ],
                "complete": True,
                "gaps": [],
            },
        },
    }


def _fields(digest: dict) -> dict:
    return digest["fields"]


def _master(digest: dict) -> dict:
    return _fields(digest)["master"]


def _automation(digest: dict) -> dict:
    return _fields(digest)["automation"]


def _track_envelope(digest: dict) -> dict:
    return _automation(digest)["tracks"][0]["envelopes"][0]


def _item_envelope(digest: dict) -> dict:
    return _automation(digest)["items"][0]


def _fx_envelope(digest: dict) -> dict:
    return _automation(digest)["fx"][0]


def _settings_of(digest: dict) -> dict:
    return _fields(digest)["project"]["settings"]


def _fx_state(digest: dict) -> dict:
    return _fields(digest)["fx_state"]


def _fingerprint(digest: dict) -> str:
    return digest_evidence(PROJECT, digest).fingerprint


# --------------------------------------------------------------------------
# the fixture itself
# --------------------------------------------------------------------------


def test_the_fixture_is_a_full_digest_with_every_required_payload() -> None:
    evidence = digest_evidence(PROJECT, _digest())

    assert evidence.scope == "full"
    assert evidence.missing == ()
    assert DIGEST_REQUIRED_FIELDS.issubset(evidence.covered)
    assert digest_coverage_gaps(_fields(_digest()), _digest()["coverage"]) == ()
    assert set(DIGEST_COVERAGE_PATHS) >= DIGEST_REQUIRED_FIELDS


# --------------------------------------------------------------------------
# (a) master track
# --------------------------------------------------------------------------


MASTER_MUTATIONS = [
    pytest.param(lambda d: _master(d).__setitem__("volume", 0.5), id="volume"),
    pytest.param(lambda d: _master(d).__setitem__("pan", 0.3), id="pan"),
    pytest.param(lambda d: _master(d).__setitem__("mute", True), id="mute"),
    pytest.param(lambda d: _master(d).__setitem__("solo", 1), id="solo"),
    pytest.param(lambda d: _master(d)["fx"][0].__setitem__("name", "ReaComp"),
                 id="fx-name"),
    pytest.param(lambda d: _master(d)["fx"][0].__setitem__("guid", "{MASTER-FX-9}"),
                 id="fx-identity"),
    pytest.param(lambda d: _master(d)["fx"].reverse(), id="fx-order"),
    pytest.param(lambda d: _master(d)["fx"][0].__setitem__("enabled", False),
                 id="fx-enabled"),
    pytest.param(lambda d: _master(d)["fx"][0].__setitem__("offline", True),
                 id="fx-offline"),
    pytest.param(lambda d: _master(d)["fx"][0]["params"][1].__setitem__(
        "normalized", 0.75), id="fx-param-value"),
    pytest.param(lambda d: _master(d)["fx"][0]["params"][0].__setitem__("index", 4),
                 id="fx-param-identity"),
    pytest.param(lambda d: _master(d)["fx"].pop(), id="fx-removed"),
    pytest.param(lambda d: _master(d)["fx"].append(copy.deepcopy(
        _master(d)["fx"][0])), id="fx-added"),
    pytest.param(lambda d: _master(d)["hardware_outputs"][0].__setitem__(
        "volume", 0.25), id="hardware-output-volume"),
    pytest.param(lambda d: _master(d)["hardware_outputs"][0].__setitem__(
        "pan", -1.0), id="hardware-output-pan"),
    pytest.param(lambda d: _master(d)["hardware_outputs"][0].__setitem__(
        "mute", True), id="hardware-output-mute"),
    pytest.param(lambda d: _master(d)["sends"].append(
        {"index": 0, "dest_guid": "{BUS-9}", "volume": 0.5, "pan": 0.0,
         "mode": 0, "mute": False}), id="master-send-added"),
]


@pytest.mark.parametrize("mutate", MASTER_MUTATIONS)
def test_every_master_change_moves_the_fingerprint(mutate) -> None:
    digest = _digest()
    baseline = _fingerprint(digest)

    mutate(digest)

    assert _fingerprint(digest) != baseline


# --------------------------------------------------------------------------
# (b) automation
# --------------------------------------------------------------------------


AUTOMATION_MUTATIONS = [
    pytest.param(lambda d: _track_envelope(d)["points"][1].__setitem__(
        "value", 0.25), id="point-value"),
    pytest.param(lambda d: _track_envelope(d)["points"][1].__setitem__(
        "time_s", 4.5), id="point-time"),
    pytest.param(lambda d: _track_envelope(d)["points"][1].__setitem__(
        "shape", 5), id="point-shape"),
    pytest.param(lambda d: _track_envelope(d)["points"][1].__setitem__(
        "tension", -0.5), id="point-tension"),
    pytest.param(lambda d: _track_envelope(d)["points"].append(
        {"index": 2, "time_s": 6.0, "value": 0.75, "shape": 0, "tension": 0.0}),
        id="point-added"),
    pytest.param(lambda d: _track_envelope(d)["points"].pop(), id="point-removed"),
    pytest.param(lambda d: _track_envelope(d).__setitem__("active", False),
                 id="envelope-active"),
    pytest.param(lambda d: _track_envelope(d).__setitem__("visible", False),
                 id="envelope-visible"),
    pytest.param(lambda d: _track_envelope(d).__setitem__("armed", True),
                 id="envelope-armed"),
    pytest.param(lambda d: _track_envelope(d).__setitem__("name", "Volume (Pre-FX)"),
                 id="envelope-name"),
    pytest.param(lambda d: _track_envelope(d).__setitem__("chunk", "<VOLENV2\n>"),
                 id="envelope-chunk"),
    pytest.param(lambda d: _track_envelope(d)["automation_items"][0].__setitem__(
        "position_s", 1.0), id="automation-item-position"),
    pytest.param(lambda d: _track_envelope(d)["automation_items"][0].__setitem__(
        "length_s", 2.0), id="automation-item-length"),
    pytest.param(lambda d: _track_envelope(d)["automation_items"][0].__setitem__(
        "baseline", 0.25), id="automation-item-baseline"),
    pytest.param(lambda d: _track_envelope(d)["automation_items"][0].__setitem__(
        "amplitude", 0.5), id="automation-item-amplitude"),
    pytest.param(lambda d: _track_envelope(d)["automation_items"][0].__setitem__(
        "loop_source", True), id="automation-item-loop"),
    pytest.param(lambda d: _track_envelope(d)["automation_items"][0].__setitem__(
        "mute", True), id="automation-item-mute"),
    pytest.param(lambda d: _track_envelope(d)["automation_items"].clear(),
                 id="automation-item-removed"),
    pytest.param(lambda d: _automation(d)["master"].append(
        _envelope("{ENV-MASTER}", "Master Volume")), id="master-envelope-added"),
    pytest.param(lambda d: _item_envelope(d)["points"][0].__setitem__(
        "value", 0.1), id="item-envelope-point"),
    pytest.param(lambda d: _item_envelope(d).__setitem__("active", False),
                 id="item-envelope-active"),
    pytest.param(lambda d: _fx_envelope(d)["points"][0].__setitem__(
        "value", 0.9), id="fx-envelope-point"),
    pytest.param(lambda d: _fx_envelope(d).__setitem__("parameter_index", 1),
                 id="fx-envelope-parameter"),
    pytest.param(lambda d: _automation(d)["tracks"][0]["envelopes"].clear(),
                 id="track-envelope-removed"),
    pytest.param(lambda d: _automation(d)["fx"].clear(), id="fx-envelope-removed"),
    pytest.param(lambda d: _automation(d)["items"].clear(), id="item-envelope-removed"),
]


@pytest.mark.parametrize("mutate", AUTOMATION_MUTATIONS)
def test_every_automation_change_moves_the_fingerprint(mutate) -> None:
    digest = _digest()
    baseline = _fingerprint(digest)

    mutate(digest)

    assert _fingerprint(digest) != baseline


# --------------------------------------------------------------------------
# (c) render / playback settings
# --------------------------------------------------------------------------


PROJECT_MUTATIONS = [
    pytest.param(lambda d: _settings_of(d).__setitem__("render_settings", 1),
                 id="render-settings"),
    pytest.param(lambda d: _settings_of(d).__setitem__("render_channels", 1),
                 id="render-channels"),
    pytest.param(lambda d: _settings_of(d).__setitem__("render_srate", 44100),
                 id="render-srate"),
    pytest.param(lambda d: _settings_of(d).__setitem__("render_boundsflag", 2),
                 id="render-bounds"),
    pytest.param(lambda d: _settings_of(d).__setitem__("render_tailms", 1000),
                 id="render-tail"),
    pytest.param(lambda d: _settings_of(d).__setitem__("render_addtoproj", 1),
                 id="render-add-to-project"),
    pytest.param(lambda d: _settings_of(d).__setitem__("render_dither", 1),
                 id="render-dither"),
    pytest.param(lambda d: _settings_of(d).__setitem__("render_normalize", 1),
                 id="render-normalize"),
    pytest.param(lambda d: _settings_of(d).__setitem__("render_brickwall", 0.9),
                 id="render-brickwall"),
    pytest.param(lambda d: _settings_of(d).__setitem__("render_fadein", 0.01),
                 id="render-fade-in"),
    pytest.param(lambda d: _settings_of(d).__setitem__("render_fadeout", 0.02),
                 id="render-fade-out"),
    pytest.param(lambda d: _settings_of(d).__setitem__("project_srate_use", 0),
                 id="project-srate-use"),
    pytest.param(lambda d: _settings_of(d).__setitem__("project_timebase", 1),
                 id="project-timebase"),
]


@pytest.mark.parametrize("mutate", PROJECT_MUTATIONS)
def test_every_render_and_playback_setting_moves_the_fingerprint(mutate) -> None:
    digest = _digest()
    baseline = _fingerprint(digest)

    mutate(digest)

    assert _fingerprint(digest) != baseline


def test_the_fx_chain_state_is_hashed() -> None:
    digest = _digest()
    baseline = _fingerprint(digest)

    _fx_state(digest)["tracks"][1]["chunk"] = "<FXCHAIN\nBYPASS 1 0 0\n>"

    assert _fingerprint(digest) != baseline


def test_dropping_an_fx_chain_chunk_changes_the_fingerprint() -> None:
    digest = _digest()
    baseline = _fingerprint(digest)

    _fx_state(digest)["tracks"].pop()

    assert _fingerprint(digest) != baseline


def test_an_incomplete_fx_chain_scan_blocks_a_full_scope() -> None:
    digest = _digest()
    _fx_state(digest)["complete"] = False

    evidence = digest_evidence(PROJECT, digest)

    assert evidence.scope == "coarse"
    assert "fx_state" in evidence.missing


# --------------------------------------------------------------------------
# (d) an unmeasured master or automation block never yields ``full``
# --------------------------------------------------------------------------


@pytest.mark.parametrize("block", ["master", "automation", "project", "fx_state"])
def test_absent_required_payload_blocks_a_full_scope(block: str) -> None:
    digest = _digest()
    digest["fields"].pop(block)  # coverage still claims it

    evidence = digest_evidence(PROJECT, digest)

    assert evidence.scope == "coarse"
    assert block in evidence.missing
    assert block not in evidence.covered
    assert same_content(revision_from_digest(PROJECT, 31, digest),
                        revision_from_digest(PROJECT, 31, digest)) is False


def test_a_legacy_digest_without_master_or_automation_is_coarse() -> None:
    """The Wave B digest measured neither; it must not pass for measured now."""
    digest = _digest()
    for name in ("master", "automation", "envelopes", "project", "fx_state"):
        digest["coverage"].pop(name, None)
    for name in ("master", "automation", "project", "fx_state"):
        digest["fields"].pop(name, None)

    evidence = digest_evidence(PROJECT, digest)

    assert evidence.scope == "coarse"
    assert set(evidence.missing) >= {"master", "automation", "project", "fx_state"}


# --------------------------------------------------------------------------
# (e) a coverage flag without its payload is not evidence
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "mutate"), [
    pytest.param("tempo", lambda d: d["fields"].pop("tempo"), id="tempo-absent"),
    pytest.param("tracks", lambda d: d["fields"]["tracks"][0].pop("guid"),
                 id="track-without-guid"),
    pytest.param("master", lambda d: _master(d)["fx"].__setitem__(0, "not-a-row"),
                 id="master-fx-not-a-row"),
    pytest.param("master", lambda d: _master(d).pop("hardware_outputs"),
                 id="master-hardware-absent"),
    pytest.param("automation", lambda d: _automation(d).pop("items"),
                 id="automation-items-absent"),
    pytest.param("project", lambda d: _settings_of(d).pop("render_tailms"),
                 id="project-setting-absent"),
    pytest.param("project", lambda d: _settings_of(d).__setitem__(
        "render_settings", "0"), id="project-setting-not-a-number"),
    pytest.param("fx_state", lambda d: _fx_state(d).__setitem__(
        "algorithm", ""), id="fx-state-algorithm-empty"),
    pytest.param("fx_state", lambda d: _fx_state(d)["tracks"][0].__setitem__(
        "complete", False), id="fx-state-row-incomplete"),
])
def test_a_claim_without_a_payload_is_reported_missing(name, mutate) -> None:
    digest = _digest()
    mutate(digest)

    evidence = digest_evidence(PROJECT, digest)

    assert evidence.scope == "coarse"
    assert name in evidence.missing
    assert name not in evidence.covered
    assert digest_coverage_gaps(digest["fields"], digest["coverage"])


def test_a_payload_that_calls_itself_incomplete_blocks_a_full_scope() -> None:
    digest = _digest()
    _automation(digest)["complete"] = False

    evidence = digest_evidence(PROJECT, digest)

    assert evidence.scope == "coarse"
    assert "automation" in evidence.missing


def test_a_disabled_coverage_name_must_be_declared_missing() -> None:
    digest = _digest()
    digest["coverage"]["project"] = False  # not named in ``missing``

    evidence = digest_evidence(PROJECT, digest)

    assert evidence.scope == "coarse"
    assert "project" in evidence.missing


def test_the_envelopes_alias_follows_automation() -> None:
    digest = _digest()
    digest["coverage"]["automation"] = False
    digest["coverage"]["envelopes"] = False
    digest["missing"] = ["automation"]

    evidence = digest_evidence(PROJECT, digest)

    assert evidence.scope == "coarse"
    assert evidence.missing == ("automation",)
    assert "envelopes" not in evidence.missing


def test_an_unknown_coverage_name_is_not_judged() -> None:
    """A later schema may add a subgroup this reader cannot verify."""
    digest = _digest()
    digest["coverage"]["future_group"] = True

    assert digest_evidence(PROJECT, digest).scope == "full"


# --------------------------------------------------------------------------
# (f) equal, fully covered content really compares equal
# --------------------------------------------------------------------------


def test_equal_full_digests_share_a_fingerprint_and_content() -> None:
    left_digest = _digest()
    right_digest = copy.deepcopy(left_digest)

    left = revision_from_digest(PROJECT, 31, left_digest, instance_id="pri_test", epoch=1)
    right = revision_from_digest(PROJECT, 47, right_digest, instance_id="pri_test", epoch=1)

    assert left.fingerprint == right.fingerprint
    assert left.fingerprint_scope == "full"
    assert right.fingerprint_scope == "full"
    assert same_content(left, right) is True
    require_same_content(left, right)


def test_the_fingerprint_is_stable_under_key_reordering() -> None:
    digest = _digest()
    reordered = {
        "fields": dict(reversed(list(digest["fields"].items()))),
        "missing": [],
        "coverage": dict(reversed(list(digest["coverage"].items()))),
        "digest_version": DIGEST_VERSION,
        "change_count": 31,
        "path": "C:/wave-c/digest.rpp",
        "project_id": PROJECT,
    }

    assert _fingerprint(reordered) == _fingerprint(digest)


# --------------------------------------------------------------------------
# (g) coarse never claims content equality
# --------------------------------------------------------------------------


def test_two_identical_coarse_digests_never_share_content() -> None:
    digest = _digest()
    digest["missing"] = ["automation"]
    digest["coverage"]["automation"] = False
    digest["coverage"]["envelopes"] = False

    left = revision_from_digest(PROJECT, 31, digest)
    right = revision_from_digest(PROJECT, 31, copy.deepcopy(digest))

    assert left.fingerprint == right.fingerprint
    assert left.fingerprint_scope == "coarse"
    assert same_content(left, right) is False
    with pytest.raises(RevisionMismatchError):
        require_same_content(left, right)


def test_a_placeholder_never_matches_a_full_digest() -> None:
    from reaper_audio.context.revision import PLACEHOLDER_PREFIX, RevisionRef

    placeholder = RevisionRef(project_id=PROJECT, instance_id=None, epoch=0,
                              counter=31,
                              fingerprint=f"{PLACEHOLDER_PREFIX}{PROJECT}:31",
                              fingerprint_scope="coarse")
    full = revision_from_digest(PROJECT, 31, _digest())

    assert same_content(placeholder, full) is False
    assert same_content(full, placeholder) is False


# --------------------------------------------------------------------------
# (h) metadata the digest does not claim to cover
# --------------------------------------------------------------------------


UNMEASURED_METADATA = [
    pytest.param(lambda d: d.__setitem__("selection", {"track_guids": ["{TRACK-A}"]}),
                 id="selection"),
    pytest.param(lambda d: d.__setitem__("transport", {"play_state": 1,
                                                       "cursor_s": 12.5}),
                 id="transport"),
    pytest.param(lambda d: d.__setitem__("name", "Renamed Project"), id="name"),
    pytest.param(lambda d: d.__setitem__("path", "C:/elsewhere/other.rpp"), id="path"),
    pytest.param(lambda d: d.__setitem__("change_count", 999), id="change-count"),
    pytest.param(lambda d: d.__setitem__("notes", ["unmeasured"]), id="extra-key"),
    pytest.param(lambda d: d.__setitem__("project_id", "reaper:other"),
                 id="project-id-ignored-by-fingerprint"),
]


@pytest.mark.parametrize("mutate", UNMEASURED_METADATA)
def test_unmeasured_metadata_never_changes_the_fingerprint(mutate) -> None:
    digest = _digest()
    baseline = _fingerprint(digest)

    mutate(digest)

    assert _fingerprint(digest) == baseline


def test_unmeasured_metadata_does_not_upgrade_a_coarse_digest() -> None:
    digest = _digest()
    digest["fields"].pop("master")
    digest["selection"] = {"track_guids": ["{TRACK-A}"]}
    digest["transport"] = {"play_state": 0}

    evidence = digest_evidence(PROJECT, digest)

    assert evidence.scope == "coarse"
    assert "master" in evidence.missing
