"""Wave B lane 6: what a project digest actually proves.

``digest_evidence`` turns the bridge's narrow, versioned field capture into one
fingerprint plus an explicit scope.  These tests mutate every sound-affecting
field the digest claims to cover and prove the fingerprint moves; they also
mutate the metadata the digest does *not* claim to cover and prove it does not
create a false content claim.  A partial digest stays coarse, and two identical
coarse digests are still not evidence of equal content.
"""

from __future__ import annotations

import copy

import pytest

from reaper_audio.context.errors import (ContractValidationError,
                                         RevisionMismatchError)
from reaper_audio.context.revision import (DIGEST_REQUIRED_FIELDS, DIGEST_VERSION,
                                           digest_evidence, require_same_content,
                                           revision_from_digest, same_content)
from _digest_payloads import FULL_COVERAGE, full_extra_fields

PROJECT = "reaper:wave-b-digest"


def _full_digest() -> dict:
    """A digest shaped exactly like ``project.digest`` in the Lua bridge."""
    return {
        "project_id": PROJECT,
        "path": "C:/wave-b/digest.rpp",
        "change_count": 12,
        "digest_version": DIGEST_VERSION,
        "coverage": {name: True for name in FULL_COVERAGE},
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
                    "guid": "{FX-1}",
                    "name": "ReaEQ",
                    "enabled": True,
                    "offline": False,
                    "params": [{"index": 0, "normalized": 0.5},
                               {"index": 1, "normalized": 0.25}],
                }],
                "sends": [{
                    "dest_guid": "{BUS-1}",
                    "volume": 0.3,
                    "pan": 0.0,
                    "mode": 0,
                }],
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
            "tempo": [{
                "time_s": 0.0,
                "bpm": 120.0,
                "numerator": 4,
                "denominator": 4,
                "linear": False,
            }],
            **full_extra_fields(["{TRACK-A}"], "{MASTER}"),
        },
    }


def _partial_digest() -> dict:
    digest = _full_digest()
    digest["coverage"] = {**digest["coverage"], "envelopes": False}
    digest["missing"] = ["envelopes"]
    return digest


def _track(digest: dict) -> dict:
    return digest["fields"]["tracks"][0]


def _item(digest: dict) -> dict:
    return digest["fields"]["items"][0]


def _tempo(digest: dict) -> dict:
    return digest["fields"]["tempo"][0]


def _fingerprint(digest: dict) -> str:
    return digest_evidence(PROJECT, digest).fingerprint


SOUND_AFFECTING_MUTATIONS = [
    pytest.param(lambda d: _track(d).__setitem__("volume", 0.5),
                 id="track-volume"),
    pytest.param(lambda d: _track(d).__setitem__("pan", 0.2), id="track-pan"),
    pytest.param(lambda d: _track(d).__setitem__("mute", True), id="track-mute"),
    pytest.param(lambda d: _track(d).__setitem__("solo", 1), id="track-solo"),
    pytest.param(lambda d: _track(d).__setitem__("folder_depth", 1),
                 id="track-folder-depth"),
    pytest.param(lambda d: _track(d)["fx"][0]["params"][1].__setitem__(
        "normalized", 0.75), id="fx-param"),
    pytest.param(lambda d: _track(d)["fx"][0]["params"][0].__setitem__(
        "index", 7), id="fx-param-identity"),
    pytest.param(lambda d: _track(d)["fx"][0].__setitem__("enabled", False),
                 id="fx-enabled"),
    pytest.param(lambda d: _track(d)["fx"][0].__setitem__("offline", True),
                 id="fx-offline"),
    pytest.param(lambda d: _track(d)["fx"][0].__setitem__("name", "ReaComp"),
                 id="fx-name"),
    pytest.param(lambda d: _track(d)["sends"][0].__setitem__("volume", 0.31),
                 id="send-volume"),
    pytest.param(lambda d: _track(d)["sends"][0].__setitem__("pan", 0.5),
                 id="send-pan"),
    pytest.param(lambda d: _track(d)["sends"][0].__setitem__("mode", 3),
                 id="send-mode"),
    pytest.param(lambda d: _track(d)["sends"][0].__setitem__(
        "dest_guid", "{BUS-2}"), id="send-destination"),
    pytest.param(lambda d: _item(d).__setitem__("position_s", 4.5),
                 id="item-position"),
    pytest.param(lambda d: _item(d).__setitem__("length_s", 7.5),
                 id="item-length"),
    pytest.param(lambda d: _item(d).__setitem__("start_offset_s", 0.75),
                 id="item-start-offset"),
    pytest.param(lambda d: _item(d).__setitem__("playrate", 0.5),
                 id="item-playrate"),
    pytest.param(lambda d: _item(d).__setitem__("mute", True), id="item-mute"),
    pytest.param(lambda d: _item(d).__setitem__("fade_in_s", 0.2),
                 id="item-fade-in"),
    pytest.param(lambda d: _item(d).__setitem__("fade_out_s", 0.4),
                 id="item-fade-out"),
    pytest.param(lambda d: _item(d).__setitem__("take_guid", "{TAKE-B}"),
                 id="item-take"),
    pytest.param(lambda d: _item(d).__setitem__("source_path",
                                                "C:/audio/snare.wav"),
                 id="item-source"),
    pytest.param(lambda d: _item(d).__setitem__("loop_source", True),
                 id="item-loop-source"),
    pytest.param(lambda d: _item(d).__setitem__("beat_attach_mode", 1),
                 id="item-beat-attach"),
    pytest.param(lambda d: _item(d)["stretch_marker_positions"][0].__setitem__(
        "srcpos", 1.5), id="item-stretch-marker"),
    pytest.param(lambda d: _tempo(d).__setitem__("bpm", 121.0), id="tempo-bpm"),
    pytest.param(lambda d: _tempo(d).__setitem__("numerator", 7),
                 id="tempo-numerator"),
    pytest.param(lambda d: _tempo(d).__setitem__("denominator", 8),
                 id="tempo-denominator"),
    pytest.param(lambda d: _tempo(d).__setitem__("linear", True),
                 id="tempo-linear"),
]


def test_full_digest_reports_full_scope_and_names_its_coverage() -> None:
    evidence = digest_evidence(PROJECT, _full_digest())

    assert evidence.scope == "full"
    assert evidence.missing == ()
    assert DIGEST_REQUIRED_FIELDS.issubset(evidence.covered)
    assert "envelopes" in evidence.covered
    assert list(evidence.covered) == sorted(evidence.covered)
    assert evidence.digest_version == DIGEST_VERSION
    assert evidence.fingerprint.startswith("sha256:")


@pytest.mark.parametrize("mutate", SOUND_AFFECTING_MUTATIONS)
def test_every_covered_sound_affecting_field_changes_the_fingerprint(
        mutate) -> None:
    digest = _full_digest()
    baseline = _fingerprint(digest)

    mutate(digest)

    assert _fingerprint(digest) != baseline


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda d: d.__setitem__(
        "selection", {"track_guids": ["{TRACK-A}"], "item_guids": []}),
        id="selection"),
    pytest.param(lambda d: d.__setitem__(
        "transport", {"play_state": 1, "position_s": 12.5}), id="transport"),
    pytest.param(lambda d: d.__setitem__("name", "Renamed Project"),
                 id="project-name"),
    pytest.param(lambda d: d.__setitem__("change_count", 999), id="change-count"),
    pytest.param(lambda d: d.__setitem__("path", "C:/elsewhere/other.rpp"),
                 id="path"),
    pytest.param(lambda d: d.__setitem__("notes", ["unmeasured"]),
                 id="extra-key"),
])
def test_unmeasured_metadata_never_creates_a_content_claim(mutate) -> None:
    digest = _full_digest()
    baseline = _fingerprint(digest)

    mutate(digest)

    assert _fingerprint(digest) == baseline


def test_a_missing_envelope_marks_the_digest_coarse() -> None:
    evidence = digest_evidence(PROJECT, _partial_digest())

    assert evidence.scope == "coarse"
    assert evidence.missing == ("envelopes",)
    assert evidence.covered == tuple(sorted(
        name for name, measured in _partial_digest()["coverage"].items()
        if measured))


def test_two_identical_partial_digests_never_share_content() -> None:
    digest = _partial_digest()
    left = revision_from_digest(PROJECT, 12, digest)
    right = revision_from_digest(PROJECT, 12, copy.deepcopy(digest))

    assert left.fingerprint == right.fingerprint
    assert left.fingerprint_scope == "coarse"
    assert same_content(left, right) is False
    assert same_content(right, left) is False
    with pytest.raises(RevisionMismatchError):
        require_same_content(left, right)


def test_a_coarse_scope_never_matches_even_with_an_equal_fingerprint() -> None:
    """Coverage is identical; only ``missing`` makes the digest coarse."""
    full = _full_digest()
    partial = copy.deepcopy(full)
    partial["missing"] = ["envelopes"]

    full_ref = revision_from_digest(PROJECT, 12, full)
    partial_ref = revision_from_digest(PROJECT, 12, partial)

    assert full_ref.fingerprint == partial_ref.fingerprint
    assert full_ref.fingerprint_scope == "full"
    assert partial_ref.fingerprint_scope == "coarse"
    assert same_content(full_ref, partial_ref) is False
    assert same_content(partial_ref, full_ref) is False


def test_empty_fields_are_coarse_even_with_full_coverage() -> None:
    digest = _full_digest()
    digest["fields"] = {}

    evidence = digest_evidence(PROJECT, digest)
    ref = revision_from_digest(PROJECT, 12, digest)

    assert evidence.scope == "coarse"
    assert ref.fingerprint_scope == "coarse"
    assert ref.fingerprint.startswith("sha256:")
    assert same_content(ref, ref) is False


def test_revision_from_digest_uses_the_callers_counter_and_digest_scope() -> None:
    digest = _full_digest()  # digest["change_count"] is 12

    ref = revision_from_digest(PROJECT, 42, digest, instance_id="bridge-1",
                               epoch=3)

    assert ref.project_id == PROJECT
    assert ref.counter == 42
    assert ref.instance_id == "bridge-1"
    assert ref.epoch == 3
    assert ref.fingerprint_scope == "full"
    assert ref.fingerprint == digest_evidence(PROJECT, digest).fingerprint


def test_revision_from_digest_rejects_a_negative_counter() -> None:
    with pytest.raises(ContractValidationError, match="change_count"):
        revision_from_digest(PROJECT, -1, _full_digest())


@pytest.mark.parametrize(("mutate", "fragment"), [
    pytest.param(lambda d: d.__setitem__("digest_version", 2),
                 "unsupported digest_version", id="future-version"),
    pytest.param(lambda d: d.pop("missing"), "digest.missing is required",
                 id="absent-missing"),
    pytest.param(lambda d: d.pop("coverage"), "digest.coverage is required",
                 id="absent-coverage"),
    pytest.param(lambda d: d["coverage"].__setitem__("tracks", 1),
                 "must be a boolean", id="coverage-int"),
    pytest.param(lambda d: d.__setitem__("missing", "envelopes"),
                 "must be a list of strings", id="missing-bare-string"),
    pytest.param(lambda d: d.__setitem__("fields", []),
                 "fields must be a mapping", id="fields-list"),
    pytest.param(lambda d: d.__setitem__("change_count", True),
                 "change_count must be an integer", id="boolean-change-count"),
])
def test_digest_evidence_rejects_a_malformed_digest(mutate, fragment) -> None:
    digest = _full_digest()
    mutate(digest)

    with pytest.raises(ContractValidationError, match=fragment):
        digest_evidence(PROJECT, digest)
