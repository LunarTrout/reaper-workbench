"""Safe edit surface: idempotent sends, guarded envelopes, rebase, undo."""

import math

import pytest

from reaper_audio.daw.edit import run_edit, send_list, undo_receipt
from reaper_audio.daw.edit_context import read_edit_context
from reaper_audio.daw.envelope import envelope_points
from reaper_audio.daw.receipt import (EditReceipt, FileReceiptStore,
                                      MemoryReceiptStore)
from reaper_audio.errors import (BridgeUnavailable, EnvelopeGuardRejected,
                                 ReceiptConflict, ReceiptNotUndoable,
                                 SendRoutingError, StaleAfterRetry,
                                 TrackDeleteRefused, ValidationError)

from edit_fakes import FakeEditProject, FakeMeasure


def _project() -> FakeEditProject:
    project = FakeEditProject()
    project.add_track("gtr", "GTR")
    project.add_track("bus", "GTR PARALLEL")
    return project


# ---------------------------------------------------------------------------
# Sends
# ---------------------------------------------------------------------------


def test_send_connect_is_idempotent_and_second_call_sets_level():
    project = _project()
    store = MemoryReceiptStore()
    first = run_edit(
        project,
        [{"kind": "send.connect", "source_guid": "gtr",
          "destination_guid": "bus", "volume_db": -3.0}],
        reason="connect parallel bus", receipt_store=store,
    )
    assert first["applied"] is True
    assert first["status"] == "applied"
    assert len(project.sends["gtr"]) == 1
    expected = 10 ** (-3.0 / 20.0)
    assert project.sends["gtr"][0].volume == pytest.approx(expected, abs=1e-9)
    assert first["readback"]["matched"] is True
    assert first["receipt_record"]["operations"][0]["kind"] == "send.create"

    second = run_edit(
        project,
        [{"kind": "send.connect", "source_guid": "gtr",
          "destination_guid": "bus", "volume_db": -6.0}],
        reason="trim parallel send", receipt_store=store,
    )
    assert second["applied"] is True
    assert len(project.sends["gtr"]) == 1
    assert project.sends["gtr"][0].volume == pytest.approx(10 ** (-6.0 / 20.0),
                                                           abs=1e-9)
    assert second["receipt_record"]["operations"][0]["kind"] == "send.set"
    assert second["undo_available"] is True


def test_send_connect_without_a_level_writes_nothing():
    project = _project()
    project.add_send("gtr", "bus", volume=0.5)
    result = run_edit(
        project,
        [{"kind": "send.connect", "source_guid": "gtr",
          "destination_guid": "bus"}],
        reason="ensure the route exists",
    )
    assert result["status"] == "no_op"
    assert result["applied"] is False
    assert result["operations"] == []
    assert len(project.sends["gtr"]) == 1
    assert project.sends["gtr"][0].volume == pytest.approx(0.5)


def test_send_set_addresses_by_destination_and_index():
    project = _project()
    project.add_send("gtr", "bus", volume=1.0)
    by_destination = run_edit(
        project,
        [{"kind": "send.set", "source_guid": "gtr",
          "destination_guid": "bus", "pan": -0.5, "mute": True}],
        reason="pan and mute the parallel send",
    )
    assert by_destination["applied"] is True
    row = project.sends["gtr"][0]
    assert row.pan == pytest.approx(-0.5)
    assert row.mute is True

    by_index = run_edit(
        project,
        [{"kind": "send.set", "source_guid": "gtr", "send_index": 0,
          "volume_db": -12.0}],
        reason="lower the parallel send",
    )
    assert by_index["applied"] is True
    assert project.sends["gtr"][0].volume == pytest.approx(10 ** (-12.0 / 20.0),
                                                           abs=1e-9)


def test_send_destination_ambiguity_is_refused_and_listed():
    project = _project()
    project.add_send("gtr", "bus", volume=0.5)
    project.add_send("gtr", "bus", volume=0.5)
    listing = send_list(project, source_guid="gtr")
    assert listing["count"] == 2
    assert listing["duplicates"][0]["send_indices"] == [0, 1]
    assert listing["duplicates"][0]["sum_db"] == pytest.approx(0.0, abs=1e-9)

    with pytest.raises(SendRoutingError) as caught:
        run_edit(
            project,
            [{"kind": "send.set", "source_guid": "gtr",
              "destination_guid": "bus", "volume_db": -3.0}],
            reason="try to set a duplicated route",
        )
    assert caught.value.code == "send_routing_conflict"
    assert caught.value.details["send_indices"] == [0, 1]
    assert "send_index" in caught.value.details["next_action"]

    with pytest.raises(SendRoutingError):
        run_edit(
            project,
            [{"kind": "send.connect", "source_guid": "gtr",
              "destination_guid": "bus", "volume_db": -3.0}],
            reason="connect a duplicated route",
        )


def test_one_batch_cannot_duplicate_a_connect_or_mix_index_shifts():
    project = _project()
    with pytest.raises(SendRoutingError):
        run_edit(
            project,
            [{"kind": "send.connect", "source_guid": "gtr",
              "destination_guid": "bus", "volume_db": -3.0},
             {"kind": "send.connect", "source_guid": "gtr",
              "destination_guid": "bus", "volume_db": -6.0}],
            reason="two connects in one batch",
        )
    assert project.sends["gtr"] == []

    project.add_send("gtr", "bus")
    with pytest.raises(SendRoutingError):
        run_edit(
            project,
            [{"kind": "send.delete", "source_guid": "gtr", "send_index": 0},
             {"kind": "send.set", "source_guid": "gtr", "send_index": 0,
              "volume_db": -3.0}],
            reason="delete and set one source in one batch",
        )
    assert len(project.sends["gtr"]) == 1


def test_track_delete_cannot_share_a_batch_with_other_changes():
    project = _project()
    with pytest.raises(ValidationError) as caught:
        run_edit(
            project,
            [{"kind": "track.delete", "target_guid": "gtr", "confirm": True},
             {"kind": "track.set", "target_guid": "gtr",
              "values": {"volume": 0.5}}],
            reason="delete and edit one track",
        )
    assert caught.value.details["track_guids"] == ["gtr"]


def test_send_delete_and_its_receipt_restore_the_route():
    project = _project()
    project.add_send("gtr", "bus", volume=0.25, pan=0.25, mute=True)
    deleted = run_edit(
        project,
        [{"kind": "send.delete", "source_guid": "gtr", "destination_guid": "bus"}],
        reason="remove a doubled send",
    )
    assert deleted["applied"] is True
    assert deleted["readback"]["matched"] is True
    assert project.sends["gtr"] == []

    undo = undo_receipt(project, deleted["receipt_record"])
    assert undo["applied"] is True
    assert len(project.sends["gtr"]) == 1
    restored = project.sends["gtr"][0]
    assert restored.destination_guid == "bus"
    assert restored.volume == pytest.approx(0.25)
    assert restored.pan == pytest.approx(0.25)
    assert restored.mute is True


# ---------------------------------------------------------------------------
# Track delete
# ---------------------------------------------------------------------------


def test_track_delete_with_unknown_guid_explains_what_exists():
    project = _project()
    with pytest.raises(TrackDeleteRefused) as caught:
        run_edit(project,
                 [{"kind": "track.delete", "target_guid": "missing",
                   "confirm": True}],
                 reason="delete a track")
    assert caught.value.code == "track_delete_refused"
    assert [row["guid"] for row in caught.value.details["available_tracks"]] == [
        "gtr", "bus"]
    assert caught.value.details["next_action"]


def test_track_delete_requires_confirmation_and_refuses_master():
    project = _project()
    with pytest.raises(TrackDeleteRefused) as caught:
        run_edit(project, [{"kind": "track.delete", "target_guid": "gtr"}],
                 reason="delete a track")
    assert "confirm=true" in caught.value.message
    assert caught.value.details["track_name"] == "GTR"

    with pytest.raises(TrackDeleteRefused) as caught:
        run_edit(project,
                 [{"kind": "track.delete", "target_guid": "master_guid",
                   "confirm": True}],
                 reason="delete the master")
    assert caught.value.details["track_guid"] == "master_guid"


def test_track_delete_applies_but_records_that_it_cannot_be_undone():
    project = _project()
    project.add_send("gtr", "bus")
    result = run_edit(
        project,
        [{"kind": "track.delete", "target_guid": "gtr", "confirm": True}],
        reason="delete an empty track",
    )
    assert result["applied"] is True
    assert result["readback"]["matched"] is True
    assert "gtr" not in project.tracks
    assert result["undo_available"] is False
    assert "track.delete" in result["undo_block_reason"]

    with pytest.raises(ReceiptNotUndoable) as caught:
        undo_receipt(project, result["receipt_record"])
    assert caught.value.code == "receipt_not_undoable"


def test_a_failed_readback_does_not_hide_a_committed_edit():
    project = _project()
    project.fail_snapshot_after_change = True
    result = run_edit(
        project,
        [{"kind": "track.set", "target_guid": "gtr", "values": {"volume": 0.5}}],
        reason="apply then lose the read-back",
    )
    assert result["applied"] is True
    assert result["status"] == "applied"
    assert result["readback"]["matched"] is None
    assert result["readback"]["reason"] == "readback_failed"
    assert result["undo_available"] is True


# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------


def _vocal_project() -> FakeEditProject:
    project = FakeEditProject()
    project.add_track("vox", "VOX")
    project.add_envelope("vox", "Volume", points=[
        {"time_s": 0.0, "value": 0.5},
        {"time_s": 120.0, "value": 0.5},
    ])
    return project


def test_envelope_points_reports_interval_and_db():
    project = _vocal_project()
    snapshot = read_edit_context(project)
    points = envelope_points(snapshot, "vox", "Volume", scaling="db")
    assert points["point_count"] == 2
    assert points["native_scaling"] == "amplitude"
    assert points["complete"] is True
    assert points["points"][0]["value"] == pytest.approx(
        20 * math.log10(0.5), abs=1e-9)
    # A live session (anything with snapshot/digest) is accepted directly.
    direct = envelope_points(project, "vox", "Volume", interval=(0.0, 10.0))
    assert direct["returned_point_count"] == 1
    windowed = envelope_points(snapshot, "vox", "Volume",
                               interval=(60.0, 90.0))
    assert windowed["returned_point_count"] == 0
    assert windowed["point_count"] == 2
    with pytest.raises(ValidationError) as caught:
        envelope_points(snapshot, "vox", "Pan")
    assert "Volume" in caught.value.details["available_envelopes"]


def test_incomplete_routing_or_automation_reads_are_refused():
    project = _project()
    project.coverage["sends"] = False
    with pytest.raises(BridgeUnavailable) as caught:
        run_edit(
            project,
            [{"kind": "send.connect", "source_guid": "gtr",
              "destination_guid": "bus", "volume_db": -3.0}],
            reason="connect on an incomplete read",
        )
    assert caught.value.details["project_id"] == "proj_fake"
    assert send_list(project, source_guid="gtr")["complete"] is False

    vocal = _vocal_project()
    vocal.coverage["automation"] = False
    with pytest.raises(BridgeUnavailable):
        run_edit(
            vocal,
            [{"kind": "envelope.set", "owner_guid": "vox",
              "envelope": "Volume", "points": [{"time_s": 60.0, "value": 0.4}]}],
            reason="write automation on an incomplete read",
        )
    assert envelope_points(vocal, "vox", "Volume")["complete"] is False


def test_envelope_guard_refuses_a_write_that_creates_silence():
    project = _vocal_project()
    with pytest.raises(EnvelopeGuardRejected) as caught:
        run_edit(
            project,
            [{"kind": "envelope.set", "owner_guid": "vox", "envelope": "Volume",
              "points": [{"time_s": 60.0, "value": 0.0}],
              "scaling": "amplitude",
              "guard": {"forbid_silence": True}}],
            reason="try to mute the vocal",
        )
    assert caught.value.code == "envelope_guard_rejected"
    violations = caught.value.details["violations"]
    assert violations[0]["code"] == "forbid_silence"
    assert violations[0]["time_s"] == 60.0
    assert project.envelopes[("vox", "Volume")].points == [
        {"time_s": 0.0, "value": 0.5}, {"time_s": 120.0, "value": 0.5}]


def test_envelope_guard_refuses_a_fixed_floor_and_a_large_delta():
    project = _vocal_project()
    with pytest.raises(EnvelopeGuardRejected) as caught:
        run_edit(
            project,
            [{"kind": "envelope.set", "owner_guid": "vox", "envelope": "Volume",
              "points": [{"time_s": 60.0, "value": -60.0}],
              "scaling": "db",
              "guard": {"min_gain_db": -24.0}}],
            reason="write below the explicit floor",
        )
    assert caught.value.details["violations"][0]["limit_db"] == -24.0

    with pytest.raises(EnvelopeGuardRejected) as caught:
        run_edit(
            project,
            [{"kind": "envelope.set", "owner_guid": "vox", "envelope": "Volume",
              "points": [{"time_s": 60.0, "value": 0.0}],
              "scaling": "db",
              "guard": {"max_delta_db": 3.0}}],
            reason="move the curve by too much",
        )
    delta = caught.value.details["violations"][0]
    assert delta["code"] == "max_delta_db"
    assert delta["delta_db"] == pytest.approx(6.0206, abs=1e-3)


def test_envelope_set_applies_within_guard_and_undo_restores_points():
    project = _vocal_project()
    result = run_edit(
        project,
        [{"kind": "envelope.set", "owner_guid": "vox", "envelope": "Volume",
          "points": [{"time_s": 60.0, "value": -8.0}],
          "scaling": "db", "mode": "read",
          "guard": {"min_gain_db": -24.0, "max_delta_db": 6.0,
                    "forbid_silence": True}}],
        reason="duck the vocal by a measured amount",
    )
    assert result["applied"] is True
    assert result["readback"]["matched"] is True
    assert project.tracks["vox"].automation_mode == 1
    points = project.envelopes[("vox", "Volume")].points
    assert len(points) == 3
    assert points[1]["time_s"] == 60.0
    assert points[1]["value"] == pytest.approx(10 ** (-8.0 / 20.0), abs=1e-9)
    assert result["undo_available"] is True

    undo = undo_receipt(project, result["receipt_record"])
    assert undo["applied"] is True
    restored = project.envelopes[("vox", "Volume")].points
    assert [(point["time_s"], point["value"]) for point in restored] == [
        (0.0, 0.5), (120.0, 0.5)]
    assert project.tracks["vox"].automation_mode == 0


def test_envelope_points_outside_declared_interval_are_refused():
    project = _vocal_project()
    with pytest.raises(ValidationError) as caught:
        run_edit(
            project,
            [{"kind": "envelope.set", "owner_guid": "vox", "envelope": "Volume",
              "points": [{"time_s": 200.0, "value": 0.4}],
              "interval": [10.0, 20.0]}],
            reason="write outside the intended span",
        )
    assert caught.value.details["point_index"] == 0


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def test_verify_measures_before_and_after_with_an_injected_service():
    project = _project()
    measure = FakeMeasure(project)
    result = run_edit(
        project,
        [{"kind": "track.set", "target_guid": "gtr", "values": {"volume": 0.5}}],
        reason="trim the guitar",
        verify={"sources": ["gtr"], "interval": "all", "metrics": ["lufs"],
                "tolerance": {"lufs": 0.1}, "gain_metrics": ["lufs"]},
        measure=measure,
    )
    report = result["verify"]
    assert report["matched"] is True
    assert report["delta"]["gtr"]["lufs"] == pytest.approx(-6.0206, abs=1e-3)
    assert report["before"]["gtr"]["lufs"] == pytest.approx(0.0, abs=1e-9)
    assert report["after"]["gtr"]["lufs"] == pytest.approx(-6.0206, abs=1e-3)
    assert [call["interval"] for call in measure.calls] == [None, None]


def test_verify_defaults_turn_verification_on_when_verify_is_omitted():
    project = _project()
    measure = FakeMeasure(project)
    result = run_edit(
        project,
        [{"kind": "track.set", "target_guid": "gtr", "values": {"volume": 0.5}}],
        reason="trim with configured defaults",
        verify_defaults={"sources": ["gtr"], "interval": "all",
                         "metrics": ["lufs"], "tolerance": {"lufs": 0.1},
                         "gain_metrics": ["lufs"]},
        measure=measure,
    )
    assert result["verify"]["matched"] is True
    assert len(measure.calls) == 2


def test_verify_matched_is_absent_without_an_explicit_expectation():
    project = _project()
    measure = FakeMeasure(project)
    result = run_edit(
        project,
        [{"kind": "track.set", "target_guid": "gtr", "values": {"volume": 0.5}}],
        reason="trim without an expected delta",
        verify={"sources": ["gtr"], "interval": "all", "metrics": ["lufs"]},
        measure=measure,
    )
    report = result["verify"]
    assert report["matched"] is None
    assert report["delta"]["gtr"]["lufs"] == pytest.approx(-6.0206, abs=1e-3)


def test_verify_reports_false_when_the_measured_delta_misses_the_expectation():
    project = _project()
    measure = FakeMeasure(project)
    result = run_edit(
        project,
        [{"kind": "track.set", "target_guid": "gtr", "values": {"volume": 0.5}}],
        reason="trim with a different expectation",
        verify={"sources": ["gtr"], "interval": "all", "metrics": ["lufs"],
                "tolerance": {"lufs": 0.1}, "expected": {"lufs": 3.0}},
        measure=measure,
    )
    assert result["verify"]["matched"] is False


def test_verify_requires_an_explicit_measure_service_and_defaults():
    project = _project()
    with pytest.raises(ValidationError) as caught:
        run_edit(project,
                 [{"kind": "track.set", "target_guid": "gtr",
                   "values": {"volume": 0.5}}],
                 reason="trim",
                 verify={"sources": ["gtr"], "interval": "all",
                         "metrics": ["lufs"], "tolerance": {"lufs": 0.1}})
    assert "measure" in caught.value.message

    with pytest.raises(ValidationError) as caught:
        run_edit(project,
                 [{"kind": "track.set", "target_guid": "gtr",
                   "values": {"volume": 0.5}}],
                 reason="trim", verify=True)
    assert "verify_defaults" in caught.value.message


# ---------------------------------------------------------------------------
# Rebase
# ---------------------------------------------------------------------------


def test_stale_plan_is_rebased_once_and_the_receipt_records_it():
    project = _project()
    project.stale_on_plan = 1
    result = run_edit(
        project,
        [{"kind": "send.connect", "source_guid": "gtr",
          "destination_guid": "bus", "volume_db": -3.0}],
        reason="connect after a background change",
    )
    assert result["applied"] is True
    assert result["rebased"] == 1
    assert result["receipt_record"]["rebased"] == 1
    assert project.plan_calls == 2
    assert len(project.sends["gtr"]) == 1


def test_stale_apply_is_rebased_up_to_the_explicit_limit():
    project = _project()
    project.stale_on_apply = 2
    result = run_edit(
        project,
        [{"kind": "send.connect", "source_guid": "gtr",
          "destination_guid": "bus", "volume_db": -3.0}],
        reason="connect across two background changes",
        max_rebase=2,
    )
    assert result["applied"] is True
    assert result["rebased"] == 2
    assert project.apply_calls >= 2


def test_exhausted_rebase_has_its_own_code_and_a_failed_receipt():
    project = _project()
    project.stale_always = True
    store = MemoryReceiptStore()
    with pytest.raises(StaleAfterRetry) as caught:
        run_edit(
            project,
            [{"kind": "send.connect", "source_guid": "gtr",
              "destination_guid": "bus", "volume_db": -3.0}],
            reason="never converges",
            max_rebase=2, receipt_store=store,
        )
    assert caught.value.code == "stale_after_retry"
    assert caught.value.details["rebased"] == 2
    assert caught.value.details["max_rebase"] == 2
    failed = store.list_recent(1)[0]
    assert failed.status == "failed"
    assert failed.error["error"] == "stale_after_retry"
    assert caught.value.details["receipt"] == failed.receipt_id


# ---------------------------------------------------------------------------
# Receipts and targeted undo
# ---------------------------------------------------------------------------


def test_undo_restores_only_the_receipt_target_after_later_edits():
    project = _project()
    project.add_track("bass", "BASS")
    edit = run_edit(
        project,
        [{"kind": "track.set", "target_guid": "gtr", "values": {"volume": 0.5}}],
        reason="trim the guitar",
    )
    assert project.tracks["gtr"].volume == pytest.approx(0.5)
    # A later, unrelated change must survive the targeted undo.
    project.tracks["bass"].volume = 0.25
    project.bump()

    undo = undo_receipt(project, edit["receipt_record"])
    assert undo["applied"] is True
    assert undo["undo_of"] == edit["receipt"]
    assert project.tracks["gtr"].volume == pytest.approx(1.0)
    assert project.tracks["bass"].volume == pytest.approx(0.25)


def test_undo_refuses_to_overwrite_a_changed_target():
    project = _project()
    edit = run_edit(
        project,
        [{"kind": "track.set", "target_guid": "gtr", "values": {"volume": 0.5}}],
        reason="trim the guitar",
    )
    project.tracks["gtr"].volume = 0.75
    project.bump()
    with pytest.raises(ReceiptConflict) as caught:
        undo_receipt(project, edit["receipt_record"])
    assert caught.value.code == "receipt_conflict"
    assert caught.value.details["field"] == "volume"
    assert caught.value.details["current"] == pytest.approx(0.75)

    forced = undo_receipt(project, edit["receipt_record"], allow_overwrite=True)
    assert forced["applied"] is True
    assert project.tracks["gtr"].volume == pytest.approx(1.0)


def test_undo_by_receipt_id_uses_the_store():
    project = _project()
    store = MemoryReceiptStore()
    edit = run_edit(
        project,
        [{"kind": "track.set", "target_guid": "gtr", "values": {"volume": 0.5}}],
        reason="trim the guitar", receipt_store=store,
    )
    undo = undo_receipt(project, edit["receipt"], receipt_store=store)
    assert undo["applied"] is True
    assert project.tracks["gtr"].volume == pytest.approx(1.0)
    stored = store.get(edit["receipt"])
    assert stored is not None and stored.status == "undone"
    with pytest.raises(ValidationError) as caught:
        undo_receipt(project, "op_missing", receipt_store=store)
    assert caught.value.code == "receipt_not_found"


def test_raw_send_create_is_refused_in_a_safe_batch():
    project = _project()
    with pytest.raises(ValidationError) as caught:
        run_edit(
            project,
            [{"kind": "send.create", "target_guid": "gtr",
              "values": {"destination_guid": "bus"}}],
            reason="bypass the idempotent route",
        )
    assert "send.connect" in caught.value.message


def test_receipt_file_store_round_trips_the_record(tmp_path):
    project = _project()
    edit = run_edit(
        project,
        [{"kind": "track.set", "target_guid": "gtr",
          "values": {"volume": 0.5, "pan": -0.25}}],
        reason="trim and pan",
    )
    store = FileReceiptStore(tmp_path)
    record = EditReceipt.from_dict(edit["receipt_record"])
    store.put(record)
    loaded = store.get(record.receipt_id)
    assert loaded is not None
    assert loaded.to_dict() == record.to_dict()
    assert store.list_recent(5)[0].receipt_id == record.receipt_id
