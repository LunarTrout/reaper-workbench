"""Regression guard for the folder-parent render closure (Stage D1 defect).

The bug: a BUS render key stayed identical while a child track's FX changed,
because the closure computed descendants but never hashed them.  These tests
pin the corrected behaviour without touching REAPER: a folder parent's key must
move with its children and must not move without them.
"""

from __future__ import annotations

import copy

from reaper_audio.context.render_dependencies import build_render_manifest
from reaper_audio.context.revision import DIGEST_REQUIRED_FIELDS, DIGEST_VERSION

BUS = "{BUS-0000}"
CHILD = "{CHILD-00}"
OTHER = "{OTHER-00}"
MASTER = "{MASTER-0}"


def _digest(*, child_fx: str = "eq", child_position: float = 0.0) -> dict:
    return {
        "project_id": "reaper:{P}:C:/x/REAPER-CONTEXT-V2-TEST.RPP",
        "digest_version": DIGEST_VERSION,
        "change_count": 1,
        "sha256": "a" * 64,
        "coverage": {name: False for name in DIGEST_REQUIRED_FIELDS},
        "missing": sorted(DIGEST_REQUIRED_FIELDS),
        "fields": {
            "tracks": [
                {"guid": BUS, "index": 0, "name": "BUS", "folder_depth": 1,
                 "fx": [], "sends": [], "hardware_outputs": []},
                {"guid": CHILD, "index": 1, "name": "GTR", "folder_depth": -1,
                 "fx": [{"name": child_fx}], "sends": [], "hardware_outputs": []},
                {"guid": OTHER, "index": 2, "name": "VOX", "folder_depth": 0,
                 "fx": [{"name": "verb"}], "sends": [], "hardware_outputs": []},
            ],
            "items": [
                {"guid": "{ITEM-0}", "track_guid": CHILD, "position": child_position,
                 "length": 2.0, "state_chunk_complete": True},
            ],
            "master": {"guid": MASTER, "fx": [], "sends": [],
                       "hardware_outputs": []},
            "fx_state": {"tracks": []},
            "automation": [],
        },
    }


def _key(digest: dict, source_guid: str) -> str:
    manifest = build_render_manifest(
        project_id=digest["project_id"], digest=digest,
        sources=[{"source_guid": source_guid, "scope": "source"},
                 {"source_guid": MASTER, "scope": "master"}],
        time_window={"start_s": 0.0, "end_s": 2.0},
        render_contract={"track_stage": "via_master", "master_stage": "master"})
    for row in manifest["sources"]:
        if row["source_guid"] == source_guid:
            return row["render_key"]
    raise AssertionError("source missing from manifest")


def test_child_fx_change_moves_the_bus_key() -> None:
    before = _key(_digest(child_fx="eq"), BUS)
    after = _key(_digest(child_fx="eq-bright"), BUS)
    assert before != after


def test_descendant_components_are_hashed_into_the_parent() -> None:
    """The defect was that descendants were computed but never hashed."""
    from reaper_audio.context.render_dependencies import (_base_components,
                                                          _folder_relations,
                                                          _source_components)
    digest = _digest()
    components = _base_components(digest=digest)
    parents, children = _folder_relations(digest["fields"]["tracks"])
    _keys, digests, _unknown = _source_components(
        scope="source", track_guid=BUS, components=components,
        parents=parents, children=children, senders={})
    descendant_keys = [key for key in digests if ":descendant:" in key]
    assert any(key.endswith(":items") for key in descendant_keys)
    assert any(key.endswith(":fx") for key in descendant_keys)
    assert all(key.startswith(f"track:{CHILD}:descendant:") for key in descendant_keys)


def test_unrelated_track_change_leaves_the_bus_key_alone() -> None:
    before_digest = _digest()
    after_digest = copy.deepcopy(before_digest)
    after_digest["fields"]["tracks"][2]["fx"] = [{"name": "delay"}]
    assert _key(before_digest, BUS) == _key(after_digest, BUS)


def test_master_change_moves_the_key_when_the_stem_goes_through_master() -> None:
    """The declared render contract renders stems through the master chain, so
    a master-chain change belongs in every stem's fingerprint."""
    before_digest = _digest()
    after_digest = copy.deepcopy(before_digest)
    after_digest["fields"]["master"]["fx"] = [{"name": "limiter"}]
    assert _key(before_digest, BUS) != _key(after_digest, BUS)


def test_child_key_moves_with_its_own_fx() -> None:
    assert _key(_digest(child_fx="eq"), CHILD) != _key(_digest(child_fx="eq-bright"), CHILD)
