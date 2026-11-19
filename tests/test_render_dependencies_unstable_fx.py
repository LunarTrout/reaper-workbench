"""Declared-unknown FX chains (Kontakt-style non-deterministic serialization).

A plug-in may write fresh session identifiers into its state on every
serialization.  Hashing those bytes makes the project digest differ between two
reads of an unchanged project: structural tokens die and every source is
re-considered dirty.  The bridge now detects that by reading the chain twice and
reports the track as a declared unknown; these tests pin the contract:

* the map digest stays usable (the field is backed, not "missing");
* only the affected source is marked incomplete;
* its neighbours keep a clean, reusable key.
"""

from __future__ import annotations

from _digest_payloads import FULL_COVERAGE, full_extra_fields
from reaper_audio.context.render_dependencies import _base_components, _source_components
from reaper_audio.context.revision import DIGEST_VERSION, digest_evidence

STABLE = "{STABLE-00}"
UNSTABLE = "{UNSTABLE}"
MASTER = "{MASTER-0}"
PID = "reaper:{P}:C:/x/REAPER-CONTEXT-V2-TEST.RPP"


def _digest(*, unstable_chunk: str = "") -> dict:
    fields = full_extra_fields([STABLE, UNSTABLE], MASTER)
    fields.update({
        "tracks": [
            {"guid": STABLE, "index": 0, "name": "GTR", "folder_depth": 0,
             "fx": [{"name": "eq", "params": []}], "sends": [],
             "hardware_outputs": []},
            {"guid": UNSTABLE, "index": 1, "name": "ACC", "folder_depth": 0,
             "fx": [{"name": "Kontakt 7", "params": []}], "sends": [],
             "hardware_outputs": []},
        ],
        "items": [],
        "tempo": [],
        "fx_state": {
            "algorithm": "rpp_fxchain_text_v1",
            "complete": True,
            "gaps": [],
            "unstable": [{"track_guid": UNSTABLE}],
            "tracks": [
                {"track_guid": STABLE, "is_master": False, "fx_count": 1,
                 "chunk": "<FXCHAIN stable>", "complete": True},
                {"track_guid": UNSTABLE, "is_master": False, "fx_count": 1,
                 "chunk": unstable_chunk, "complete": False,
                 "unstable": True, "reason": "fx_chain_unstable"},
                {"track_guid": MASTER, "is_master": True, "fx_count": 0,
                 "chunk": "<FXCHAIN\n>", "complete": True},
            ],
        },
    })
    return {
        "project_id": PID,
        "digest_version": DIGEST_VERSION,
        "change_count": 7,
        "sha256": "a" * 64,
        "coverage": {name: True for name in FULL_COVERAGE},
        "missing": [],
        "fields": fields,
    }


def test_unstable_chain_is_backed_not_missing() -> None:
    evidence = digest_evidence(PID, _digest())
    assert evidence.scope == "full"
    assert evidence.missing == ()


def test_fingerprint_ignores_the_volatile_bytes() -> None:
    assert (digest_evidence(PID, _digest(unstable_chunk="")).fingerprint
            == digest_evidence(PID, _digest(unstable_chunk="")).fingerprint)


def test_only_the_unstable_source_is_marked_incomplete() -> None:
    digest = _digest()
    components = _base_components(digest=digest)
    _keys, _digests, unknown = _source_components(
        scope="source", track_guid=UNSTABLE, components=components,
        parents={}, children={}, senders={})
    assert f"fx_chain_state_unstable:{UNSTABLE}" in unknown

    _keys, _digests, clean = _source_components(
        scope="source", track_guid=STABLE, components=components,
        parents={}, children={}, senders={})
    assert not any("fx_chain_state" in reason for reason in clean)
