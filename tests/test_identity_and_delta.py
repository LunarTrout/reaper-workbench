"""Identity, render-key and mutation-delta invariants.

These tests build capture documents and bridge digests locally; no REAPER
session, render or network call is involved.  They exercise the real lifecycle
open/page/ack path and the real delta builder, not a mock of either.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reaper_audio.context.changes import build_structural_delta, digest_facts
from reaper_audio.context.full_capture import CaptureArchive
from reaper_audio.context.identity import (ProjectIdentity, identity_from_mapping,
                                           same_identity_values)
from reaper_audio.context.lifecycle import PanoramaLifecycle
from reaper_audio.context.panorama import PANORAMA_SCHEMA_VERSION
from reaper_audio.context.render_dependencies import (build_render_manifest,
                                                      compare_render_manifests,
                                                      project_render_manifest)
from reaper_audio.context.revision import DIGEST_REQUIRED_FIELDS, _PROJECT_SETTING_KEYS
from reaper_audio.errors import StaleContext, ValidationError

PROJECT_ID = "reaper:{MASTER-GUID}:C:/music/test.rpp"
TRACK_A = "{TRACK-A}"
TRACK_B = "{TRACK-B}"
MASTER = "{MASTER-GUID}"
ITEM_A = "{ITEM-A}"
TAKE_A = "{TAKE-A}"
STABLE_A = "trk_a"
STABLE_B = "trk_b"
STABLE_M = "mst_m"


def _revision(change_count: int, *, instance: str = "pri_A", epoch: int = 1) -> dict:
    return {
        "project_id": PROJECT_ID, "change_count": change_count,
        "path": "C:/music/test.rpp", "instance_id": instance, "epoch": epoch,
        "epoch_basis": "bridge_session_project_generation",
    }


def _digest(*, change_count: int, pan_a: float = 0.0, volume_a: float = 1.0,
            gain_b: float = 0.5, master_volume: float = 1.0,
            pan_law_a: float = -1.0) -> dict:
    def track(guid: str, *, pan: float, volume: float, gain: float | None,
              fx_guid: str, pan_law: float = -1.0) -> dict:
        params = [{"index": 0, "normalized": gain}] if gain is not None else []
        return {
            "guid": guid, "volume": volume, "pan": pan, "mute": False, "solo": 0,
            "channel_count": 2, "pan_mode": 0, "pan_law": pan_law,
            "pan_law_flags": 0, "folder_depth": 0,
            "fx": [{"index": 0, "guid": fx_guid, "name": "Amp",
                    "enabled": True, "offline": False, "params": params}],
            "sends": [], "hardware_outputs": [],
        }

    items = [{
        "guid": ITEM_A, "track_guid": TRACK_A, "position_s": 0.0, "length_s": 4.0,
        "start_offset_s": 0.0, "playrate": 1.0, "mute": False,
        "fade_in_s": 0.0, "fade_out_s": 0.0, "channel_mode": 0, "group_id": 0,
        "loop_source": False, "beat_attach_mode": 0, "take_guid": TAKE_A,
        "source_path": "a.wav", "take_volume": 1.0, "take_pan": 0.0,
        "take_pitch": 0.0, "preserve_pitch": False,
        "stretch_marker_positions": [],
        "state_chunk": "<ITEM\nPOSITION 0\n>\n", "state_chunk_complete": True,
    }]
    automation = {
        "tracks": [{"track_guid": TRACK_A, "envelopes": []},
                   {"track_guid": TRACK_B, "envelopes": []}],
        "master": [], "items": [], "fx": [],
        "complete": True, "items_scanned": 1, "gaps": [],
    }
    fx_state = {
        "algorithm": "rpp_fxchain_text_v1",
        "tracks": [
            {"track_guid": TRACK_A, "is_master": False, "fx_count": 1,
             "chunk": "<FXCHAIN A>", "complete": True},
            {"track_guid": TRACK_B, "is_master": False, "fx_count": 1,
             "chunk": "<FXCHAIN B>", "complete": True},
            {"track_guid": MASTER, "is_master": True, "fx_count": 0,
             "chunk": "<FXCHAIN M>", "complete": True},
        ],
        "complete": True, "gaps": [],
    }
    project = {
        "complete": True,
        "settings": {key: 0 for key in _PROJECT_SETTING_KEYS},
    }
    return {
        "project_id": PROJECT_ID, "path": "C:/music/test.rpp",
        "change_count": change_count, "digest_version": 1,
        "coverage": {name: True for name in
                     (DIGEST_REQUIRED_FIELDS | {"envelopes"})},
        "missing": [],
        "fields": {
            "tracks": [
                track(TRACK_A, pan=pan_a, volume=volume_a, gain=0.5,
                      fx_guid="{FX-A}", pan_law=pan_law_a),
                track(TRACK_B, pan=0.0, volume=1.0, gain=gain_b, fx_guid="{FX-B}"),
            ],
            "items": items,
            "tempo": [{"time_s": 0, "bpm": 120, "numerator": 4,
                       "denominator": 4, "linear": False}],
            "automation": automation,
            "fx_state": fx_state,
            "project": project,
            "master": {
                "guid": MASTER, "is_master": True, "volume": master_volume,
                "pan": 0.0, "mute": False, "solo": 0, "fx": [],
                "sends": [], "hardware_outputs": [],
            },
        },
    }


def _manifest(document: dict, digest: dict) -> dict:
    manifest = build_render_manifest(
        project_id=PROJECT_ID, digest=digest,
        sources=[
            {"source_guid": TRACK_A, "scope": "source", "stage": "stem",
             "channels": 2, "sample_rate": 48000},
            {"source_guid": TRACK_B, "scope": "source", "stage": "stem",
             "channels": 2, "sample_rate": 48000},
            {"source_guid": MASTER, "scope": "master", "stage": "master",
             "channels": 2, "sample_rate": 48000},
        ],
        time_window={"project_start_s": 0.0, "project_end_s": 4.0},
        render_contract={"capture": "native_render"},
    )
    projected = project_render_manifest(
        manifest, {TRACK_A: STABLE_A, TRACK_B: STABLE_B, MASTER: STABLE_M})
    document["render_manifest"] = projected
    document["structural_facts"] = digest_facts(digest)
    keys = projected["by_source"]
    document["audio"] = [
        {"audio_id": "aud_a", "source_id": STABLE_A, "is_master": False,
         "render_provenance": {"stage": "stem"},
         "render_key": keys[STABLE_A]["render_key"], "render_key_complete": True},
        {"audio_id": "aud_b", "source_id": STABLE_B, "is_master": False,
         "render_provenance": {"stage": "stem"},
         "render_key": keys[STABLE_B]["render_key"], "render_key_complete": True},
        {"audio_id": "aud_m", "source_id": STABLE_M, "is_master": True,
         "render_provenance": {"stage": "master"},
         "render_key": keys[STABLE_M]["render_key"], "render_key_complete": True},
    ]
    document["analysis"] = [
        {"analysis_id": "ana_a", "source_guid": TRACK_A},
        {"analysis_id": "ana_b", "source_guid": TRACK_B},
    ]
    document["relations"] = {"relations": [
        {"relation_id": "rel_ab", "from_id": STABLE_A, "to_id": STABLE_B},
        {"relation_id": "rel_am", "from_id": STABLE_A, "to_id": STABLE_M},
    ]}
    return document


def _document(capture_id: str, revision: dict, digest: dict) -> dict:
    document = {
        "schema_version": PANORAMA_SCHEMA_VERSION,
        "capture_binding": {
            "capture_id": capture_id, "project_id": PROJECT_ID,
            "record_integrity_sha256": "integrity-" + capture_id,
            "revision_before": revision, "revision_after": revision,
            "time_window": {"project_start_s": 0.0, "project_end_s": 4.0},
        },
        "project": {"name": "test"},
        "tracks": [
            {"stable_id": STABLE_A, "guid": TRACK_A, "mixer": {"pan": 0.0}},
            {"stable_id": STABLE_B, "guid": TRACK_B, "mixer": {"pan": 0.0}},
        ],
        "master": {"stable_id": STABLE_M, "guid": MASTER},
        "coverage": {"complete": True},
        "truncation": {"truncated": False},
    }
    return _manifest(document, digest)


def _capture(root: Path, capture_id: str, revision: dict, digest: dict) -> None:
    capture_dir = root / "captures" / capture_id
    capture_dir.mkdir(parents=True)
    document = _document(capture_id, revision, digest)
    (capture_dir / "panorama.json").write_text(json.dumps(document), encoding="utf-8")
    (capture_dir / "record.json").write_text("{}", encoding="utf-8")
    row = {"capture_id": capture_id, "project_id": PROJECT_ID,
           "revision_before": revision, "revision_after": revision}
    with (root / "index.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def _archive(tmp_path: Path) -> Path:
    root = tmp_path / "archive"
    root.mkdir(parents=True)
    (root / "index.jsonl").write_text("", encoding="utf-8")
    return root


def _acknowledge(lifecycle: PanoramaLifecycle, project_id: str = PROJECT_ID,
                 capture_id: str | None = None) -> dict:
    opened = lifecycle.open(project_id, capture_id=capture_id, page_bytes=4096)
    hashes = [opened["page_hash"]]
    for index in range(1, opened["page_count"]):
        hashes.append(lifecycle.page(opened["handle"], opened["revision"], index)["page_hash"])
    ack = lifecycle.ack(opened["handle"], opened["revision"], hashes)
    assert ack["complete"] is True
    return ack


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


def test_identity_requires_every_component() -> None:
    complete = identity_from_mapping(_revision(3))
    assert complete.complete and complete.gaps() == ()
    assert not identity_from_mapping({"project_id": PROJECT_ID,
                                      "instance_id": "pri_A"}).complete
    zero_epoch = identity_from_mapping({**_revision(3), "epoch": 0})
    assert zero_epoch.gaps() == ("epoch",)


def test_identity_mismatch_is_never_equal() -> None:
    base = identity_from_mapping(_revision(3))
    other_epoch = identity_from_mapping(_revision(3, epoch=2))
    other_instance = identity_from_mapping(_revision(3, instance="pri_B"))
    unknown = ProjectIdentity(PROJECT_ID, None, None)
    assert same_identity_values(base, identity_from_mapping(_revision(3)))
    assert not same_identity_values(base, other_epoch)
    assert not same_identity_values(base, other_instance)
    assert not same_identity_values(base, unknown)


# ---------------------------------------------------------------------------
# render keys
# ---------------------------------------------------------------------------


def _manifest_from_digest(digest: dict) -> tuple[dict, dict]:
    document = _document("cap_manifest", _revision(digest["change_count"]), digest)
    return document["render_manifest"], document


def _verified(manifest: dict) -> dict:
    """A test-only manifest with no unresolved dependency classes.

    Production manifests always declare the unresolved classes; this helper is
    only for isolating the component-hash comparison itself.
    """
    value = dict(manifest)
    value["unverified"] = []
    value["cross_revision_authoritative"] = True
    return value


def test_unverified_dependencies_block_an_authoritative_unchanged_verdict() -> None:
    first, _ = _manifest_from_digest(_digest(change_count=1))
    second, _ = _manifest_from_digest(_digest(change_count=9))
    assert first["cross_revision_authoritative"] is False
    comparison = compare_render_manifests(first, second)
    assert comparison["authoritative"] is False
    assert comparison["scope"] == "unknown"
    assert comparison["unchanged"] == []
    assert comparison["changed"] == []
    assert comparison["freshness"] == "unknown"
    assert "media_file_content" in comparison["unverified"]
    assert any("unverified_cross_capture_dependency" in reason
               for reason in comparison["reasons"])


def test_a_manifest_without_an_unverified_declaration_is_not_trusted() -> None:
    first, _ = _manifest_from_digest(_digest(change_count=1))
    second = _verified(_manifest_from_digest(_digest(change_count=1))[0])
    del first["unverified"]
    comparison = compare_render_manifests(first, second)
    assert comparison["authoritative"] is False
    assert "manifest_missing_unverified_declaration" in comparison["unverified"]

    malformed = _verified(second)
    malformed["unverified"] = {"class": "media_file_content"}
    comparison = compare_render_manifests(malformed, second)
    assert comparison["authoritative"] is False
    assert "manifest_unverified_declaration_malformed" in comparison["unverified"]

    declared = _verified(second)
    declared["cross_revision_authoritative"] = False
    comparison = compare_render_manifests(declared, second)
    assert comparison["authoritative"] is False
    assert "manifest_declares_cross_revision_non_authoritative" in comparison["unverified"]


def test_verified_manifests_can_still_prove_an_unchanged_source() -> None:
    first, _ = _manifest_from_digest(_digest(change_count=1))
    second, _ = _manifest_from_digest(_digest(change_count=2))
    comparison = compare_render_manifests(_verified(first), _verified(second))
    assert comparison["authoritative"] is True
    assert comparison["unchanged"] == [STABLE_M, STABLE_A, STABLE_B]
    assert comparison["changed"] == []


def test_pan_change_updates_only_the_touched_track_and_master() -> None:
    before, before_document = _manifest_from_digest(_digest(change_count=1))
    after_digest = _digest(change_count=2, pan_a=-1.0)
    after_document = _document("cap_after", _revision(2), after_digest)
    after = after_document["render_manifest"]

    comparison = compare_render_manifests(_verified(before), _verified(after))
    assert comparison["authoritative"] is True
    assert comparison["changed"] == [STABLE_M, STABLE_A]
    assert comparison["unchanged"] == [STABLE_B]
    assert any("track_state" in key for key in comparison["component_changes"][STABLE_A])
    # The master mixes every track, so an unchanged master key here would be a lie.
    assert STABLE_M in comparison["changed"]


def test_unrelated_fx_parameter_leaves_the_other_stem_provably_fresh() -> None:
    before, _ = _manifest_from_digest(_digest(change_count=1))
    after_document = _document("cap_after", _revision(2),
                               _digest(change_count=2, gain_b=0.9))
    comparison = compare_render_manifests(
        _verified(before), _verified(after_document["render_manifest"]))
    assert comparison["changed"] == [STABLE_M, STABLE_B]
    assert comparison["unchanged"] == [STABLE_A]


def test_folder_ancestor_is_a_render_dependency_of_its_child() -> None:
    before_digest = _digest(change_count=1)
    before_digest["fields"]["tracks"][0]["folder_depth"] = 1
    before_document = _document("cap_folder", _revision(1), before_digest)
    after_digest = _digest(change_count=2)
    after_digest["fields"]["tracks"][0]["folder_depth"] = 1
    after_digest["fields"]["tracks"][0]["volume"] = 0.4
    after_document = _document("cap_folder_2", _revision(2), after_digest)

    before = before_document["render_manifest"]
    after = after_document["render_manifest"]
    assert (before["by_source"][STABLE_B]["render_key"] !=
            after["by_source"][STABLE_B]["render_key"])
    comparison = compare_render_manifests(
        _verified(before_document["render_manifest"]),
        _verified(after_document["render_manifest"]))
    assert comparison["changed"] == [STABLE_M, STABLE_A, STABLE_B]
    assert comparison["unchanged"] == []


def test_pan_law_change_invalidates_the_track_and_master_but_not_its_sibling() -> None:
    before, _ = _manifest_from_digest(_digest(change_count=1))
    after = _document("cap_panlaw", _revision(2),
                      _digest(change_count=2, pan_law_a=0.707))["render_manifest"]
    comparison = compare_render_manifests(
        _verified(before), _verified(after))
    assert comparison["changed"] == [STABLE_M, STABLE_A]
    assert comparison["unchanged"] == [STABLE_B]
    assert any(key.endswith(":track_state")
               for key in comparison["component_changes"][STABLE_A])


def test_track_deletion_invalidates_the_removed_source_and_master_only() -> None:
    before, _ = _manifest_from_digest(_digest(change_count=1))
    deleted_digest = _digest(change_count=2)
    deleted_digest["fields"]["tracks"] = [
        row for row in deleted_digest["fields"]["tracks"] if row["guid"] != TRACK_B]
    after = _document("cap_deleted", _revision(2), deleted_digest)["render_manifest"]
    comparison = compare_render_manifests(
        _verified(before), _verified(after))
    assert comparison["changed"] == [STABLE_M, STABLE_B]
    assert comparison["unchanged"] == [STABLE_A]
    assert comparison["component_changes"][STABLE_B] == ["source_removed"]


def test_send_routing_change_invalidates_the_path_not_the_unrelated_stem() -> None:
    before, _ = _manifest_from_digest(_digest(change_count=1))
    routed = _digest(change_count=2)
    routed["fields"]["tracks"][0]["sends"] = [{
        "index": 0, "dest_guid": TRACK_B, "volume": 1.0, "pan": 0.0,
        "mode": 0, "mute": False,
    }]
    after = _document("cap_routed", _revision(2), routed)["render_manifest"]
    comparison = compare_render_manifests(
        _verified(before), _verified(after))
    # A's own routing changed, B receives from A, and the master sums both.
    assert comparison["changed"] == [STABLE_M, STABLE_A, STABLE_B]
    assert comparison["unchanged"] == []
    assert any("routing" in key
               for key in comparison["component_changes"][STABLE_A])


def test_render_keys_are_stable_for_identical_state() -> None:
    first, _ = _manifest_from_digest(_digest(change_count=1))
    second, _ = _manifest_from_digest(_digest(change_count=99))
    assert first["by_source"][STABLE_A]["render_key"] == \
        second["by_source"][STABLE_A]["render_key"]
    assert first["complete"] is True


def test_manifest_is_incomplete_when_item_chunks_are_missing() -> None:
    digest = _digest(change_count=1)
    for item in digest["fields"]["items"]:
        item["state_chunk_complete"] = False
    document = _document("cap_broken", _revision(1), digest)
    assert document["render_manifest"]["complete"] is False
    assert any("item_state_chunk" in reason
               for row in document["render_manifest"]["sources"]
               for reason in row["unknown"])


# ---------------------------------------------------------------------------
# structural delta
# ---------------------------------------------------------------------------


def test_delta_names_the_pan_change_and_invalidates_the_right_evidence() -> None:
    before_digest = _digest(change_count=1)
    after_digest = _digest(change_count=2, pan_a=-1.0)
    document = _document("cap_base", _revision(1), before_digest)
    payload = build_structural_delta(
        kind="edit", project_id=PROJECT_ID,
        base={"context_token": "ct_test", "capture_id": "cap_base",
              "revision": _revision(1), "fact_root": "root",
              "view_root": "view", "document_hash": "doc"},
        target={"revision": _revision(2)},
        base_document=document,
        before_facts=digest_facts(before_digest),
        after_facts=digest_facts(after_digest),
        after_digest=after_digest, scope="known",
        operation_facts=[{"operation_index": 1, "kind": "track.set",
                          "target_guid": TRACK_A,
                          "before": {"pan": 0.0}, "after": {"pan": -1.0},
                          "changes_state": True}],
        readback_complete=True,
    )
    assert payload["acknowledgeable"] is True
    changed = payload["objects"]["changed"]
    track_a = next(row for row in changed if row["id"] == TRACK_A)
    assert track_a["fields"] == [{"field": "pan", "before": 0.0, "after": -1.0}]
    # Media bytes, plugin versions, project pan law and unexposed routing flags
    # are declared unverified in this manifest, so no source may be called fresh
    # across captures even though the known key is unchanged for TRACK_B.
    assert payload["render"]["authoritative"] is False
    assert payload["render"]["freshness"] == "unknown"
    assert "media_file_content" in payload["render"]["unverified"]
    assert payload["audio_evidence"]["state"] == "stale"
    assert payload["audio_evidence"]["invalidated_sources"] == [
        STABLE_M, STABLE_A, STABLE_B]
    assert payload["audio_evidence"]["fresh_sources"] == []
    assert set(payload["audio_evidence"]["invalidated_audio_ids"]) == {
        "aud_a", "aud_b", "aud_m"}
    assert payload["audio_evidence"]["invalidated_analysis_ids"] == [
        "ana_a", "ana_b"]
    assert payload["audio_evidence"]["invalidated_relation_ids"] == [
        "rel_ab", "rel_am"]
    assert payload["invalidations"][0]["complete"] is False
    assert payload["invalidations"][0]["reason"] == \
        "unverified_cross_capture_dependency"


def test_unknown_scope_never_claims_a_precise_subset() -> None:
    before_digest = _digest(change_count=1)
    after_digest = _digest(change_count=2, pan_a=-1.0)
    document = _document("cap_base", _revision(1), before_digest)
    payload = build_structural_delta(
        kind="api_call", project_id=PROJECT_ID,
        base={"context_token": "ct_test", "capture_id": "cap_base",
              "revision": _revision(1), "fact_root": "root",
              "view_root": "view", "document_hash": "doc"},
        target={"revision": _revision(2)}, base_document=document,
        before_facts=digest_facts(before_digest), after_facts=digest_facts(after_digest),
        after_digest=after_digest, scope="unknown", operation_facts=None,
        readback_complete=True,
    )
    assert payload["scope"] == "unknown"
    assert payload["render"]["authoritative"] is False
    assert payload["invalidations"][0]["scope"] == "all_project_audio"
    assert payload["invalidations"][0]["sources"] is None
    assert payload["invalidations"][0]["evidence_ids"] is None
    assert payload["audio_evidence"]["state"] == "stale"
    assert payload["audio_evidence"]["invalidated_sources"] == [STABLE_M, STABLE_A, STABLE_B]
    assert payload["audio_evidence"]["fresh_sources"] == []


def test_partial_readback_is_not_acknowledgeable() -> None:
    digest = _digest(change_count=1)
    document = _document("cap_base", _revision(1), digest)
    payload = build_structural_delta(
        kind="edit", project_id=PROJECT_ID,
        base={"context_token": "ct_test", "capture_id": "cap_base",
              "revision": _revision(1), "fact_root": "root",
              "view_root": "view", "document_hash": "doc"},
        target={"revision": _revision(2)}, base_document=document,
        before_facts=digest_facts(digest), after_facts=digest_facts(digest),
        after_digest=None, scope="known", operation_facts=None,
        readback_complete=False, readback_missing=["digest_unavailable"],
    )
    assert payload["acknowledgeable"] is False
    assert payload["render"]["authoritative"] is False
    assert payload["readback"]["missing"] == ["digest_unavailable"]


# ---------------------------------------------------------------------------
# lifecycle: advance without recapture, never advance stale audio
# ---------------------------------------------------------------------------


def test_two_sequential_edits_advance_without_a_new_capture(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", _revision(1), _digest(change_count=1))
    lifecycle = PanoramaLifecycle(root, page_bytes=4096)
    first = _acknowledge(lifecycle)
    token = first["context_token"]

    pan_delta = lifecycle.record_change(
        kind="edit", context_token=token, project_id=PROJECT_ID,
        base_revision=_revision(1), target_revision=_revision(2),
        after_digest=_digest(change_count=2, pan_a=-1.0), scope="known",
        operation_facts=[{"operation_index": 1, "kind": "track.set",
                          "target_guid": TRACK_A,
                          "before": {"pan": 0.0}, "after": {"pan": -1.0},
                          "changes_state": True}],
        readback_complete=True)
    assert pan_delta["acknowledgeable"] is True
    ack = lifecycle.delta_ack(pan_delta["delta_id"], pan_delta["delta_hash"], token)
    assert ack["complete"] is True and ack["kind"] == "delta"
    assert ack["revision"]["change_count"] == 2
    assert ack["audio_evidence"]["state"] == "stale"
    advanced = ack["context_token"]

    # The advanced token authorizes structural edits at the new revision.
    gate = lifecycle.require_receipt(advanced, PROJECT_ID, _revision(2))
    assert gate["complete"] is True and gate["kind"] == "delta"
    # But it does not authorize project-audio interpretation.
    with pytest.raises(StaleContext) as excinfo:
        lifecycle.require_receipt(advanced, PROJECT_ID, _revision(2),
                                  require_audio_fresh=True)
    assert excinfo.value.details["audio_state"] == "stale"
    assert "reaper_capture_snapshot" in excinfo.value.details["next_action"]

    second_delta = lifecycle.record_change(
        kind="edit", context_token=advanced, project_id=PROJECT_ID,
        base_revision=_revision(2), target_revision=_revision(3),
        after_digest=_digest(change_count=3, pan_a=-1.0, volume_a=0.5), scope="known",
        operation_facts=[{"operation_index": 1, "kind": "track.set",
                          "target_guid": TRACK_A,
                          "before": {"volume": 1.0}, "after": {"volume": 0.5},
                          "changes_state": True}],
        readback_complete=True)
    second_ack = lifecycle.delta_ack(second_delta["delta_id"],
                                     second_delta["delta_hash"], advanced)
    assert second_ack["revision"]["change_count"] == 3
    assert second_ack["delta_chain_length"] == 2
    assert second_ack["audio_evidence"]["state"] == "stale"

    # The archive still holds only the revision-1 capture. A capture-to-capture
    # delta must not claim "not modified" for a context that is already ahead.
    ahead = lifecycle.changes_since(PROJECT_ID,
                                    context_token=second_ack["context_token"])
    assert ahead["status"] == "stale_base"
    assert ahead["reason"] == "context_ahead_of_capture_archive"


def test_no_op_delta_advances_structure_but_cannot_claim_audio_fresh(
        tmp_path: Path) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", _revision(1), _digest(change_count=1))
    lifecycle = PanoramaLifecycle(root, page_bytes=4096)
    token = _acknowledge(lifecycle)["context_token"]
    # Only a name changed: the known digest and render keys are equal, but the
    # manifest still declares unresolved cross-capture dependencies.
    delta = lifecycle.record_change(
        kind="edit", context_token=token, project_id=PROJECT_ID,
        base_revision=_revision(1), target_revision=_revision(2),
        after_digest=_digest(change_count=2), scope="known",
        operation_facts=[{"operation_index": 1, "kind": "track.set",
                          "target_guid": TRACK_A,
                          "before": {"name": "Gtr"}, "after": {"name": "Gtr L"},
                          "changes_state": True}],
        readback_complete=True)
    assert delta["acknowledgeable"] is True
    assert delta["render"]["authoritative"] is False
    assert delta["audio_evidence"]["state"] == "stale"
    assert delta["audio_evidence"]["invalidated_sources"] == [
        STABLE_M, STABLE_A, STABLE_B]
    assert delta["audio_evidence"]["fresh_sources"] == []
    ack = lifecycle.delta_ack(delta["delta_id"], delta["delta_hash"], token)
    with pytest.raises(StaleContext):
        lifecycle.require_receipt(ack["context_token"], PROJECT_ID, _revision(2),
                                  require_audio_fresh=True)


def test_fresh_capture_evidence_is_current_with_unverified_manifest(
        tmp_path: Path) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", _revision(1), _digest(change_count=1))
    lifecycle = PanoramaLifecycle(root, page_bytes=4096)
    ack = _acknowledge(lifecycle)
    gate = lifecycle.require_receipt(ack["context_token"], PROJECT_ID, _revision(1),
                                     require_audio_fresh=True)
    assert gate["complete"] is True
    assert gate["audio_evidence"]["state"] == "current"
    assert gate["audio_evidence"]["fresh_sources"] == [
        STABLE_M, STABLE_A, STABLE_B]


def test_wrong_base_and_wrong_identity_cannot_advance(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", _revision(1), _digest(change_count=1))
    lifecycle = PanoramaLifecycle(root, page_bytes=4096)
    token = _acknowledge(lifecycle)["context_token"]

    with pytest.raises(StaleContext):
        lifecycle.record_change(
            kind="edit", context_token=token, project_id=PROJECT_ID,
            base_revision=_revision(7), target_revision=_revision(8),
            after_digest=_digest(change_count=8), scope="known",
            operation_facts=None, readback_complete=True)
    with pytest.raises(StaleContext):
        lifecycle.require_receipt(token, PROJECT_ID, _revision(1, instance="pri_B"))
    with pytest.raises(StaleContext):
        lifecycle.require_receipt(token, PROJECT_ID, _revision(1, epoch=2))


def test_partial_delta_cannot_be_acknowledged(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", _revision(1), _digest(change_count=1))
    lifecycle = PanoramaLifecycle(root, page_bytes=4096)
    token = _acknowledge(lifecycle)["context_token"]
    delta = lifecycle.record_change(
        kind="edit", context_token=token, project_id=PROJECT_ID,
        base_revision=_revision(1), target_revision=_revision(2),
        after_digest=None, scope="known", operation_facts=None,
        readback_complete=False, readback_missing=["digest_unavailable"])
    assert delta["acknowledgeable"] is False
    assert delta["refresh_required"] is True
    with pytest.raises(StaleContext):
        lifecycle.delta_ack(delta["delta_id"], delta["delta_hash"], token)


def test_repeated_ack_is_idempotent_and_chain_is_verified(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", _revision(1), _digest(change_count=1))
    lifecycle = PanoramaLifecycle(root, page_bytes=4096)
    token = _acknowledge(lifecycle)["context_token"]
    delta = lifecycle.record_change(
        kind="edit", context_token=token, project_id=PROJECT_ID,
        base_revision=_revision(1), target_revision=_revision(2),
        after_digest=_digest(change_count=2, pan_a=1.0), scope="known",
        operation_facts=None, readback_complete=True)
    first = lifecycle.delta_ack(delta["delta_id"], delta["delta_hash"], token)
    repeated = lifecycle.delta_ack(delta["delta_id"], delta["delta_hash"], token)
    assert repeated["idempotent"] is True
    assert repeated["context_token"] == first["context_token"]
    with pytest.raises(StaleContext):
        lifecycle.delta_ack(delta["delta_id"], "0" * 64, token)


def test_capture_to_capture_delta_refuses_identity_discontinuity(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", _revision(1), _digest(change_count=1))
    _capture(root, "cap_00000002", _revision(2, instance="pri_B"),
             _digest(change_count=2))
    lifecycle = PanoramaLifecycle(root, page_bytes=4096)
    result = lifecycle.changes_since(PROJECT_ID, base_capture_id="cap_00000001")
    assert result["status"] == "stale_base"
    assert result["reason"] == "identity_discontinuity"


def test_capture_read_rejects_a_delta_token(tmp_path: Path) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", _revision(1), _digest(change_count=1))
    lifecycle = PanoramaLifecycle(root, page_bytes=4096)
    token = _acknowledge(lifecycle)["context_token"]
    delta = lifecycle.record_change(
        kind="edit", context_token=token, project_id=PROJECT_ID,
        base_revision=_revision(1), target_revision=_revision(2),
        after_digest=_digest(change_count=2), scope="known",
        operation_facts=None, readback_complete=True)
    advanced = lifecycle.delta_ack(delta["delta_id"], delta["delta_hash"],
                                   token)["context_token"]
    with pytest.raises(StaleContext):
        lifecycle.require_receipt(advanced, PROJECT_ID, _revision(2),
                                  expected_capture_id="cap_00000001")


def test_changes_since_delivers_the_successor_token_in_the_same_response(
        tmp_path: Path) -> None:
    root = _archive(tmp_path)
    _capture(root, "cap_00000001", _revision(1), _digest(change_count=1))
    _capture(root, "cap_00000002", _revision(2), _digest(change_count=2, pan_a=-1.0))
    lifecycle = PanoramaLifecycle(root, page_bytes=4096)
    base = _acknowledge(lifecycle, capture_id="cap_00000001")

    result = lifecycle.changes_since(PROJECT_ID, base_capture_id="cap_00000001",
                                     context_token=base["context_token"])
    assert result["status"] == "delta"
    assert result["advance"]["advanced"] is True
    successor = result["advance"]["context_token"]
    assert successor and successor != base["context_token"]
    gate = lifecycle.require_receipt(successor, PROJECT_ID, _revision(2))
    assert gate["complete"] is True
    # The explicit re-ack is idempotent and yields the same successor.
    repeated = lifecycle.delta_ack(result["delta_id"], result["delta_hash"],
                                   base["context_token"])
    assert repeated["context_token"] == successor
