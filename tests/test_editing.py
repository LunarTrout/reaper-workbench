import pytest

from reaper_audio.daw.editing import (MAX_CUTS_PER_TRANSACTION, apply_cut_times,
                                      cut_plan)
from reaper_audio.errors import ValidationError


class FakeSession:
    def __init__(self, tracks):
        self.tracks = tracks
        self.operations = None
        self.reason = None

    def snapshot(self, include_items=False):
        assert include_items is True
        return {"tracks": self.tracks}

    def plan(self, operations, *, reason):
        self.operations = list(operations)
        self.reason = reason
        return {"plan_id": "fake-plan"}

    def apply(self, plan, *, dry_run=True):
        return {"applied": not dry_run, "dry_run": dry_run,
                "operations_requested": len(self.operations)}


def _track(guid, name, items):
    return {"guid": guid, "name": name, "items": items}


def _item(guid, position, length):
    return {"guid": guid, "position_s": position, "length_s": length}


def test_cut_plan_resolves_group_and_splits_latest_first():
    session = FakeSession([
        _track("kick", "kick", [_item("kick-item", 0.0, 10.0)]),
        _track("snare", "snare", [_item("snare-item", 0.0, 10.0)]),
    ])
    operations, skipped = cut_plan(
        session,
        [{"time_s": 1.0, "group": "drums"},
         {"time_s": 2.0, "group": "drums"}],
        reason="test",
    )
    assert not skipped
    assert len(operations) == 4
    assert [operation.values["position_s"] for operation in operations] == [
        2.0, 2.0, 1.0, 1.0]
    assert all(operation.kind == "item.split" for operation in operations)
    assert {operation.target_guid for operation in operations} == {
        "kick-item", "snare-item"}


def test_cut_plan_preserves_seam_parameters_for_reaper():
    session = FakeSession([
        _track("guitar", "guitar", [_item("guitar-item", 0.0, 10.0)]),
    ])
    operations, skipped = cut_plan(
        session,
        [{"time_s": 1.0, "track_guids": ["guitar"], "fade_s": 0.005,
          "tail_s": 0.130, "right_trim_s": 0.025, "move_s": 0.100}],
        reason="preserve measured seam",
    )
    assert not skipped
    assert len(operations) == 1
    assert operations[0].values == {
        "position_s": 1.0,
        "fade_s": 0.005,
        "tail_s": 0.130,
        "right_trim_s": 0.025,
        "move_s": 0.100,
    }


def test_cut_plan_skips_items_without_audio_at_the_time():
    session = FakeSession([
        _track("kick", "kick", [_item("kick-item", 0.0, 2.0)]),
    ])
    operations, skipped = cut_plan(
        session, [{"time_s": 5.0, "group": "drums"}], reason="test")
    assert operations == []
    assert skipped and skipped[0]["reason"] == "no item at time"


def test_cut_plan_rejects_too_many_operations():
    session = FakeSession([
        _track("kick", "kick", [_item("kick-item", 0.0, 1000.0)]),
    ])
    with pytest.raises(ValidationError, match="Too many cuts"):
        cut_plan(session,
                 [{"time_s": index * 0.1, "group": "drums"}
                  for index in range(1, MAX_CUTS_PER_TRANSACTION + 2)],
                 reason="test")


def test_apply_cut_times_uses_one_transaction():
    session = FakeSession([
        _track("kick", "kick", [_item("kick-item", 0.0, 10.0)]),
    ])
    result = apply_cut_times(
        session, [{"time_s": 1.0, "group": "drums"}],
        reason="test", dry_run=False)
    assert result["applied"] is True
    assert result["operations_requested"] == 1
    assert session.reason == "test"
