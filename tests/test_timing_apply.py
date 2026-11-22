"""Tests for applying a timing plan as one typed edit transaction.

No live REAPER: :class:`FakeTimingProject` implements the bridge surface the
apply path uses (snapshot, revision-bound preview/apply, revision-bound undo)
and mutates an in-memory item list only when an ``item.timing_apply``
operation carries the geometry.  It never invents an item, so a read-back
mismatch can only come from the code under test.
"""

from __future__ import annotations

import copy
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from reaper_audio.daw.session import EditOperation, ProjectRevision
from reaper_audio.daw.timing_apply import (
    MAX_SEAM_OVERLAP_S,
    OPERATION_KIND,
    apply_timing_plan,
    normalize_clips,
    undo_timing_apply,
)
from reaper_audio.errors import StaleContext, ValidationError

SOURCE_PATH = "C:/fixtures/Blind-grid-dense/Media/Take-30-DI.wav"
TRACK_GUID = "{TRACK-GTR}"
ITEM_GUID = "{ITEM-TAKE-30}"
TAKE_GUID = "{TAKE-30}"


class FakeBridgeError(RuntimeError):
    """A failure the fake bridge raises after it started mutating."""


@dataclass
class FakeItem:
    guid: str
    track_guid: str
    position_s: float
    length_s: float
    source_offset_s: float = 0.0
    playrate: float = 1.0
    fade_in_s: float = 0.0
    fade_out_s: float = 0.0
    source_path: str = SOURCE_PATH
    take_guid: str = ""
    take_name: str = "Take-30-DI"
    notes: str = ""
    volume: float = 1.0
    pan: float = 0.0
    pitch: float = 0.0
    stretch_markers: int = 0
    is_midi: bool = False

    def geometry(self) -> dict[str, float]:
        return {"position_s": self.position_s, "length_s": self.length_s,
                "source_offset_s": self.source_offset_s,
                "playrate": self.playrate, "fade_in_s": self.fade_in_s,
                "fade_out_s": self.fade_out_s}


@dataclass
class FakePlan:
    plan_id: str
    revision: ProjectRevision
    operations: tuple[EditOperation, ...]
    reason: str
    before: list[dict[str, Any]] = field(default_factory=list)


class FakeTimingProject:
    """One track, many items, transactional apply and revision-bound undo."""

    def __init__(self, *, source_length_s: float = 12.0,
                 auto_fade_s: float = 0.0, ignore_source_offset: bool = False,
                 verify_geometry: bool = True, fail_at_block: int | None = None,
                 rollback_broken: bool = False) -> None:
        self.project_id = "proj_timing"
        self.path = "C:/fixtures/Blind-grid-dense/Blind-grid-dense.rpp"
        self.change_count = 1
        self.source_length_s = source_length_s
        self.auto_fade_s = auto_fade_s
        self.ignore_source_offset = ignore_source_offset
        self.verify_geometry = verify_geometry
        self.fail_at_block = fail_at_block
        self.rollback_broken = rollback_broken
        self.items: list[FakeItem] = []
        self.plan_calls = 0
        self.apply_calls = 0
        self.after_plan_hook: Any = None
        self._plan_counter = 0
        self._item_counter = 0
        self.consumed_plans: set[str] = set()
        self.plans: list[FakePlan] = []
        self.last_transaction: dict[str, Any] | None = None

    # ---- construction -------------------------------------------------
    def add_item(self, *, guid: str, position_s: float, length_s: float,
                 source_offset_s: float = 0.0, take_guid: str = TAKE_GUID,
                 source_path: str = SOURCE_PATH, fade_in_s: float = 0.0,
                 fade_out_s: float = 0.0, notes: str = "") -> FakeItem:
        item = FakeItem(guid=guid, track_guid=TRACK_GUID,
                        position_s=position_s, length_s=length_s,
                        source_offset_s=source_offset_s, take_guid=take_guid,
                        source_path=source_path, fade_in_s=fade_in_s,
                        fade_out_s=fade_out_s, notes=notes)
        self.items.append(item)
        return item

    def revision(self) -> ProjectRevision:
        return ProjectRevision(project_id=self.project_id,
                               change_count=self.change_count, path=self.path)

    def bump(self) -> None:
        self.change_count += 1

    # ---- bridge read surface ------------------------------------------
    def _item_context(self, item: FakeItem) -> dict[str, Any]:
        return {
            "guid": item.guid,
            "position_s": item.position_s,
            "length_s": item.length_s,
            "start_offset_s": item.source_offset_s,
            "playrate": item.playrate,
            "fade_in_s": item.fade_in_s,
            "fade_out_s": item.fade_out_s,
            "mute": False,
            "selected": False,
            "take_count": 1,
            "loop_source": False,
            "group_id": 0,
            "beat_attach_mode": 0,
            "source_length_s": self.source_length_s,
            "source_length_is_qn": False,
            "source_sample_rate": 48_000,
            "source_channels": 2,
            "source_online": True,
            "stretch_marker_positions": [],
            "active_take": {
                "guid": item.take_guid,
                "name": item.take_name,
                "source": item.source_path,
                "is_midi": item.is_midi,
                "playrate": item.playrate,
                "preserve_pitch": False,
                "stretch_markers": item.stretch_markers,
                "fx_count": 0,
                "volume": item.volume,
                "pan": item.pan,
                "pitch": item.pitch,
            },
        }

    def snapshot(self, *, project_id: str | None = None,
                 include_items: bool = False, **_: Any) -> dict[str, Any]:
        del project_id
        items = [self._item_context(item) for item in self.items] if include_items else []
        return {
            "revision": self.revision().to_dict(),
            "track_count": 1,
            "tracks": [{"guid": TRACK_GUID, "index": 0, "name": "GTR",
                        "items": items}],
            "length_s": 30.0,
            "transport": {"play_state": 0, "position_s": 0.0},
        }

    # ---- transactions -------------------------------------------------
    def plan(self, operations: list[EditOperation], *, reason: str,
             expected_revision: Any = None) -> FakePlan:
        self.plan_calls += 1
        if expected_revision is not None:
            expected = (expected_revision if isinstance(expected_revision, ProjectRevision)
                        else ProjectRevision.from_dict(dict(expected_revision)))
            if expected != self.revision():
                raise StaleContext("fake project changed before plan",
                                   details={"expected": expected.to_dict(),
                                            "current": self.revision().to_dict()})
        for operation in operations:
            if operation.kind != OPERATION_KIND:
                raise FakeBridgeError(f"fake only applies {OPERATION_KIND}")
        self._plan_counter += 1
        plan = FakePlan(plan_id=f"plan_{self._plan_counter}",
                        revision=self.revision(),
                        operations=tuple(operations), reason=reason,
                        before=[self._read_state(op) for op in operations])
        self.plans.append(plan)
        if self.after_plan_hook is not None:
            self.after_plan_hook()
        return plan

    def apply(self, plan: FakePlan, *, dry_run: bool = True) -> dict[str, Any]:
        self.apply_calls += 1
        if plan.plan_id in self.consumed_plans:
            raise StaleContext("The edit plan is missing or expired; preview it again",
                               details={"plan_id": plan.plan_id})
        if plan.revision != self.revision():
            raise StaleContext("Project changed after the plan was prepared",
                               details={"planned": plan.revision.to_dict(),
                                        "current": self.revision().to_dict()})
        if dry_run:
            return {"applied": False, "dry_run": True, "plan_id": plan.plan_id,
                    "revision": self.revision().to_dict(),
                    "before": plan.before,
                    "operations": [operation.to_dict()
                                   for operation in plan.operations]}
        saved = copy.deepcopy(self.items)
        results: list[dict[str, Any]] = []
        try:
            for operation in plan.operations:
                results.append(self._apply_operation(operation))
        except Exception:
            if not self.rollback_broken:
                self.items = saved
            else:
                self.change_count += 1
            raise
        self.bump()
        self.consumed_plans.add(plan.plan_id)
        self.last_transaction = {"plan_id": plan.plan_id, "state": saved,
                                 "revision_after": self.revision()}
        after = [{"operation_index": index, "kind": operation.kind,
                  "target_guid": operation.target_guid,
                  "values": self._read_state_after(operation)}
                 for index, operation in enumerate(plan.operations)]
        return {"applied": True, "dry_run": False, "no_op": False,
                "plan_id": plan.plan_id,
                "revision_before": plan.revision.to_dict(),
                "revision_after": self.revision().to_dict(),
                "before": plan.before, "after": after,
                "operation_results": results, "undo_available": True}

    def undo(self, *, expected_revision: ProjectRevision,
             plan_id: str) -> dict[str, Any]:
        if expected_revision != self.revision():
            raise StaleContext("Project changed after the transaction",
                               details={"expected": expected_revision.to_dict(),
                                        "current": self.revision().to_dict()})
        last = self.last_transaction
        if last is None or last["plan_id"] != plan_id:
            raise StaleContext("The requested plan is not the newest transaction",
                               details={"requested_plan_id": plan_id})
        self.items = copy.deepcopy(last["state"])
        self.last_transaction = None
        self.change_count += 1
        return {"undone": True, "plan_id": plan_id,
                "revision_before": expected_revision.to_dict(),
                "revision_after": self.revision().to_dict()}

    # ---- operation semantics -----------------------------------------
    def _require_item(self, guid: str) -> FakeItem:
        for item in self.items:
            if item.guid == guid:
                return item
        raise StaleContext("fake item is missing", details={"item_guid": guid})

    def _read_state(self, operation: EditOperation) -> dict[str, Any]:
        item = self._require_item(operation.target_guid)
        return {"item_guid": item.guid, **item.geometry(),
                "source_path": item.source_path, "take_guid": item.take_guid,
                "track_item_count": len(self.items)}

    def _read_state_after(self, operation: EditOperation) -> dict[str, Any]:
        exists = any(item.guid == operation.target_guid for item in self.items)
        return {"source_exists": exists, "item_count": len(self.items),
                "block_count": len(operation.values.get("blocks") or ())}

    def _apply_operation(self, operation: EditOperation) -> dict[str, Any]:
        values = operation.values
        source = self._require_item(operation.target_guid)
        if values.get("source_path") and source.source_path != values["source_path"]:
            raise FakeBridgeError("stale source path")
        if values.get("expected_take_guid") and \
                source.take_guid != values["expected_take_guid"]:
            raise FakeBridgeError("stale take")
        if abs(source.playrate - 1.0) > 1e-9 or source.stretch_markers:
            raise FakeBridgeError("source take is not a plain 1x take")
        blocks = values["blocks"]
        needed = max(block["source_start_s"] + block["length_s"] for block in blocks)
        if needed > self.source_length_s + 1e-6:
            raise FakeBridgeError("source_handles_unavailable")
        marker = f"wb-timing:{values['apply_id']}:"
        leftovers = [item for item in self.items
                     if item.guid != source.guid and item.notes.startswith(marker)]
        self.items = [item for item in self.items
                      if all(item is not left for left in leftovers)]
        created: list[str] = []
        for index, block in enumerate(blocks):
            if self.fail_at_block is not None and index == self.fail_at_block:
                raise FakeBridgeError(
                    f"bridge failed while creating clip {index} of {len(blocks)}")
            self._item_counter += 1
            guid = f"{{CLIP-{self._item_counter:04d}}}"
            item = FakeItem(
                guid=guid, track_guid=source.track_guid,
                position_s=float(block["position_s"]),
                length_s=float(block["length_s"]),
                source_offset_s=(0.0 if self.ignore_source_offset
                                 else float(block["source_start_s"])),
                playrate=1.0,
                fade_in_s=float(block["fade_in_s"]),
                fade_out_s=float(block["fade_out_s"]),
                source_path=source.source_path, take_guid=f"{{TAKE-CLIP-{self._item_counter}}}",
                take_name=source.take_name,
                notes=marker + str(index + 1),
                volume=source.volume, pan=source.pan, pitch=source.pitch)
            if self.auto_fade_s and item.fade_in_s == 0.0:
                item.fade_in_s = self.auto_fade_s
            self.items.append(item)
            created.append(guid)
            if self.verify_geometry:
                for field_name, wanted in (
                        ("position_s", block["position_s"]),
                        ("length_s", block["length_s"]),
                        ("source_offset_s", block["source_start_s"]),
                        ("fade_in_s", block["fade_in_s"]),
                        ("fade_out_s", block["fade_out_s"]),
                        ("playrate", 1.0)):
                    actual = item.geometry()[field_name]
                    if abs(float(actual) - float(wanted)) > 1e-6:
                        raise FakeBridgeError(
                            f"timing_geometry_mismatch: {field_name} "
                            f"expected {wanted} got {actual}")
        deleted = ""
        if values.get("delete_source", True):
            self.items = [item for item in self.items if item.guid != source.guid]
            deleted = source.guid
        return {"changed": True, "apply_id": values["apply_id"],
                "blocks": len(blocks), "created_item_guids": created,
                "deleted_source_item_guid": deleted,
                "replaced_leftovers": len(leftovers),
                "replaced_leftover_guids": [item.guid for item in leftovers]}


# ---------------------------------------------------------------------------
# Plan builders
# ---------------------------------------------------------------------------


def _plan(*, item_guid: str = ITEM_GUID, track_guid: str = TRACK_GUID,
          source_offset_s: float = 0.0, source_length_s: float = 12.0,
          item_position_s: float = 1.0, count: int = 8,
          shifts: list[float] | None = None,
          fades: list[tuple[float, float]] | None = None) -> dict[str, Any]:
    step = source_length_s / count
    boundaries = [source_offset_s + index * step for index in range(count + 1)]
    blocks = []
    for index in range(count):
        shift = 0.0 if shifts is None else shifts[index]
        fade_in, fade_out = ((0.0045, 0.0045) if fades is None else fades[index])
        start, end = boundaries[index], boundaries[index + 1]
        blocks.append({
            "start_s": round(start, 7), "end_s": round(end, 7),
            "source_offset_s": round(start, 7),
            "target_start_s": round(start + shift, 7),
            "target_end_s": round(end + shift, 7),
            "shift_s": round(shift, 7),
            "rate": 1.0,
            "fade_in_s": fade_in, "fade_out_s": fade_out,
            "event_ids": [f"evt_{index:05d}"],
        })
    return {"schema": "reaper-workbench.timing-plan/1", "plan_id": "tpl_dense",
            "analysis_id": "tan_test", "project_id": "proj_timing",
            "track_guid": track_guid, "item_guid": item_guid,
            "strength": 1.0, "coverage": "complete", "blocks": blocks}


def _dense_plan(*, count: int = 161, source_length_s: float = 12.0) -> dict[str, Any]:
    rng = random.Random(20261121)
    shifts = [round(rng.uniform(-0.0035, 0.0035), 6) for _ in range(count)]
    fades = [(round(rng.uniform(0.0035, 0.011), 6),
              round(rng.uniform(0.0035, 0.011), 6)) for _ in range(count)]
    # Fades must fit the clip; the shortest clip here is 12/161 = 74 ms.
    fades = [(min(fade_in, 0.011), min(fade_out, 0.011)) for fade_in, fade_out in fades]
    return _plan(count=count, source_length_s=source_length_s,
                 shifts=shifts, fades=fades)


def _project(*, item_length_s: float = 12.0, source_offset_s: float = 0.0,
             item_position_s: float = 1.0, **kwargs: Any) -> FakeTimingProject:
    project = FakeTimingProject(source_length_s=item_length_s + source_offset_s + 1.0,
                                **kwargs)
    project.add_item(guid=ITEM_GUID, position_s=item_position_s,
                     length_s=item_length_s, source_offset_s=source_offset_s)
    return project


# ---------------------------------------------------------------------------
# Dense plan and determinism
# ---------------------------------------------------------------------------


def test_dense_161_block_plan_applies_in_one_transaction_with_exact_geometry() -> None:
    project = _project(item_length_s=12.0)
    plan = _dense_plan(count=161)

    result = apply_timing_plan(project, plan)

    assert result["status"] == "applied"
    assert result["block_count"] == 161
    # The old item.split/item.set path would need at least two operations per
    # clip; this is one typed operation in one revision-bound transaction.
    assert result["block_count"] * 2 > 256
    assert project.plan_calls == 1 and project.apply_calls == 1
    assert len(result["operation"]["values"]["blocks"]) == 161
    assert result["operation"]["kind"] == OPERATION_KIND
    assert len(result["created_item_guids"]) == 161
    assert len(project.items) == 161
    assert all(item.guid != ITEM_GUID for item in project.items)
    for block, guid in zip(plan["blocks"], result["created_item_guids"]):
        item = next(row for row in project.items if row.guid == guid)
        assert abs(item.position_s - (1.0 + block["target_start_s"])) < 1e-9
        assert abs(item.source_offset_s - block["start_s"]) < 1e-9
        assert abs(item.length_s - (block["end_s"] - block["start_s"])) < 1e-9
        assert abs(item.fade_in_s - block["fade_in_s"]) < 1e-9
        assert abs(item.fade_out_s - block["fade_out_s"]) < 1e-9
        assert item.playrate == 1.0
    assert result["readback"]["matched"] is True
    assert result["readback"]["duplicate_count"] == 0
    assert result["readback"]["source_item_removed"] is True
    assert result["undo"]["plan_id"] == result["plan_id"]
    assert result["undo"]["expected_revision"] == result["revision_after"]


def test_shuffled_blocks_produce_the_same_operation() -> None:
    project = _project(item_length_s=12.0)
    plan = _dense_plan(count=161)
    shuffled = dict(plan)
    blocks = list(plan["blocks"])
    random.Random(7).shuffle(blocks)
    shuffled["blocks"] = blocks

    first = apply_timing_plan(project, plan, dry_run=True)
    second = apply_timing_plan(project, shuffled, dry_run=True)

    assert first["operation"] == second["operation"]
    assert first["apply_id"] == second["apply_id"]
    assert first["geometry"] == second["geometry"]
    assert project.apply_calls == 2  # two previews, no mutation


def test_operation_geometry_is_sorted_by_source_not_input_order() -> None:
    clips = normalize_clips(list(reversed(_dense_plan(count=12)["blocks"])),
                            project_start_s=1.0)
    assert [clip.source_start_s for clip in clips] == \
        sorted(clip.source_start_s for clip in clips)


# ---------------------------------------------------------------------------
# Pre-flight guards
# ---------------------------------------------------------------------------


def test_source_handles_are_checked_before_any_mutation() -> None:
    project = _project(item_length_s=6.0)
    project.source_length_s = 5.0
    plan = _plan(source_length_s=6.0, count=2)

    with pytest.raises(ValidationError) as caught:
        apply_timing_plan(project, plan)

    assert caught.value.details["code"] == "source_handles_unavailable"
    assert project.plan_calls == 0 and project.apply_calls == 0
    assert [item.guid for item in project.items] == [ITEM_GUID]


def test_plan_that_does_not_cover_the_item_is_refused() -> None:
    project = _project(item_length_s=10.0)
    project.source_length_s = 12.0
    plan = _plan(source_length_s=9.0, count=9)

    with pytest.raises(ValidationError) as caught:
        apply_timing_plan(project, plan)

    assert caught.value.details["code"] == "plan_does_not_cover_item"
    assert project.apply_calls == 0


def test_a_non_1x_take_is_refused() -> None:
    project = _project(item_length_s=12.0)
    project.items[0].playrate = 1.5

    with pytest.raises(ValidationError) as caught:
        apply_timing_plan(project, _plan(count=4))

    assert caught.value.details["code"] == "take_not_1x"
    assert project.apply_calls == 0


def test_a_take_with_stretch_markers_is_refused() -> None:
    project = _project(item_length_s=12.0)
    project.items[0].stretch_markers = 2

    with pytest.raises(ValidationError) as caught:
        apply_timing_plan(project, _plan(count=4))

    assert caught.value.details["code"] == "take_has_stretch_markers"
    assert project.apply_calls == 0


def test_a_stale_item_guid_is_refused_before_preview() -> None:
    project = _project(item_length_s=12.0)
    plan = _plan(count=4, item_guid="{ITEM-DELETED}")

    with pytest.raises(StaleContext) as caught:
        apply_timing_plan(project, plan)

    assert caught.value.details["code"] == "source_item_missing"
    assert project.apply_calls == 0


# ---------------------------------------------------------------------------
# Protected ranges
# ---------------------------------------------------------------------------


def test_a_moved_protected_range_is_refused() -> None:
    project = _project(item_length_s=12.0)
    plan = _plan(count=4, shifts=[0.0, 0.010, 0.010, 0.0])

    with pytest.raises(ValidationError) as caught:
        apply_timing_plan(project, plan,
                          protected_ranges=[{"basis": "project_s",
                                             "start_s": 4.0, "end_s": 4.2}])

    assert caught.value.details["code"] == "protected_range_moved"
    assert project.apply_calls == 0


def test_a_protected_range_cut_by_a_block_boundary_is_refused() -> None:
    project = _project(item_length_s=12.0)
    plan = _plan(count=4)

    with pytest.raises(ValidationError) as caught:
        apply_timing_plan(project, plan,
                          protected_ranges=[{"basis": "project_s",
                                             "start_s": 3.9, "end_s": 4.1}])

    assert caught.value.details["code"] == "protected_range_cut"
    assert project.apply_calls == 0


def test_a_protected_range_kept_inside_one_unshifted_block_is_allowed() -> None:
    project = _project(item_length_s=12.0)
    plan = _plan(count=4, shifts=[0.020, 0.0, 0.0, 0.020])
    block_start = plan["blocks"][2]["start_s"]

    result = apply_timing_plan(
        project, plan,
        protected_ranges=[{"basis": "source_s",
                           "start_s": block_start + 0.5,
                           "end_s": block_start + 2.0}])

    assert result["status"] == "applied"
    assert result["protected_ranges"][0]["state"] == "kept"


# ---------------------------------------------------------------------------
# Revision, retry and failure behaviour
# ---------------------------------------------------------------------------


def test_a_change_between_preview_and_apply_leaves_no_clips() -> None:
    project = _project(item_length_s=12.0)
    project.after_plan_hook = project.bump

    with pytest.raises(StaleContext):
        apply_timing_plan(project, _plan(count=4))

    assert [item.guid for item in project.items] == [ITEM_GUID]


def test_applying_the_same_previewed_plan_twice_is_refused() -> None:
    project = _project(item_length_s=12.0)
    result = apply_timing_plan(project, _plan(count=4))
    assert result["status"] == "applied"
    count_after_first = len(project.items)

    previewed = project.plans[-1]
    with pytest.raises(StaleContext):
        project.apply(previewed, dry_run=False)
    assert len(project.items) == count_after_first


def test_partial_bridge_failure_rolls_back_and_keeps_the_source_item() -> None:
    project = _project(item_length_s=12.0, fail_at_block=57)

    with pytest.raises(FakeBridgeError, match="clip 57"):
        apply_timing_plan(project, _dense_plan(count=161))

    assert [item.guid for item in project.items] == [ITEM_GUID]
    assert project.items[0].length_s == 12.0


def test_retry_after_a_broken_rollback_does_not_duplicate_clips() -> None:
    project = _project(item_length_s=12.0, fail_at_block=57,
                       rollback_broken=True)
    plan = _dense_plan(count=161)

    with pytest.raises(FakeBridgeError):
        apply_timing_plan(project, plan)
    assert len(project.items) == 58  # 57 clips plus the untouched source item

    project.fail_at_block = None
    project.rollback_broken = False
    result = apply_timing_plan(project, plan)

    assert result["status"] == "applied"
    assert result["replaced_leftovers"] == 57
    assert len(project.items) == 161
    assert result["readback"]["duplicate_count"] == 0
    positions = sorted(item.position_s for item in project.items)
    expected_positions = sorted(1.0 + block["target_start_s"] for block in plan["blocks"])
    assert all(abs(left - right) < 1e-9
               for left, right in zip(positions, expected_positions))


# ---------------------------------------------------------------------------
# Read-back catches real divergences
# ---------------------------------------------------------------------------


def test_readback_catches_an_automatic_fade_the_plan_did_not_ask_for() -> None:
    fades = [(0.0045, 0.0045)] * 8
    fades[3] = (0.0, 0.0045)
    project = _project(item_length_s=12.0, auto_fade_s=0.005,
                       verify_geometry=False)

    result = apply_timing_plan(project, _plan(count=8, fades=fades))

    assert result["status"] == "applied_readback_mismatch"
    assert result["readback"]["matched"] is False
    fields = {row["field"] for row in result["readback"]["mismatches"]}
    assert "fade_in_s" in fields
    assert result["readback"]["mismatches"][0]["block_index"] == 3


def test_readback_catches_the_same_source_read_from_the_wrong_offset() -> None:
    project = _project(item_length_s=12.0, ignore_source_offset=True,
                       verify_geometry=False)

    result = apply_timing_plan(project, _plan(count=8))

    assert result["status"] == "applied_readback_mismatch"
    fields = {row["field"] for row in result["readback"]["mismatches"]}
    assert "start_offset_s" in fields


def test_unrelated_items_on_the_track_are_not_reported_as_extras() -> None:
    project = _project(item_length_s=12.0)
    project.add_item(guid="{OTHER}", position_s=20.0, length_s=2.0,
                     source_offset_s=0.0, source_path="C:/fixtures/other.wav")

    result = apply_timing_plan(project, _plan(count=8))

    assert result["status"] == "applied"
    assert result["readback"]["extra_item_guids"] == []
    assert result["readback"]["item_count"] == 9


def test_a_second_take_of_the_same_file_keeps_absolute_offsets() -> None:
    """The same source file from another item offset must not shift clips."""
    project = _project(item_length_s=9.0, source_offset_s=0.75,
                       item_position_s=2.0)
    plan = _plan(source_offset_s=0.75, source_length_s=9.0,
                 item_position_s=2.0, count=6)

    result = apply_timing_plan(project, plan)

    assert result["status"] == "applied"
    for block, guid in zip(plan["blocks"], result["created_item_guids"]):
        item = next(row for row in project.items if row.guid == guid)
        assert abs(item.source_offset_s - block["start_s"]) < 1e-9
        # project_start = 2.0 - 0.75 = 1.25
        assert abs(item.position_s - (1.25 + block["target_start_s"])) < 1e-9


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------


def test_undo_restores_the_original_item_and_removes_every_clip() -> None:
    project = _project(item_length_s=12.0)
    original = project.items[0]
    original.fade_in_s = 0.0
    original.fade_out_s = 0.0
    original_geometry = original.geometry()
    applied = apply_timing_plan(project, _dense_plan(count=161))
    assert applied["status"] == "applied"

    result = undo_timing_apply(project, applied)

    assert result["status"] == "undone"
    assert result["readback"]["matched"] is True
    assert [item.guid for item in project.items] == [ITEM_GUID]
    restored = project.items[0]
    assert restored.geometry() == original_geometry
    assert restored.source_path == SOURCE_PATH


def test_undo_after_another_edit_is_refused_by_the_revision_binding() -> None:
    project = _project(item_length_s=12.0)
    applied = apply_timing_plan(project, _plan(count=4))
    project.bump()

    with pytest.raises(StaleContext):
        undo_timing_apply(project, applied)

    assert len(project.items) == 4


def test_dry_run_touches_nothing() -> None:
    project = _project(item_length_s=12.0)

    result = apply_timing_plan(project, _plan(count=4), dry_run=True)

    assert result["status"] == "dry_run"
    assert result["applied"] is False
    assert [item.guid for item in project.items] == [ITEM_GUID]
    assert project.apply_calls == 1
    assert result["geometry"]


# ---------------------------------------------------------------------------
# Planner integration
# ---------------------------------------------------------------------------


def test_a_plan_from_build_timing_plan_applies_without_translation() -> None:
    """The real planner's block dict is the apply path's input contract."""
    from reaper_audio.analysis.timing import build_timing_plan

    events = [{"id": f"evt_{index:05d}", "source_s": round(0.25 * index, 7),
               "sample": index * 12_000, "kind": "attack", "confidence": 0.9}
              for index in range(48)]
    assignments = []
    for index, event in enumerate(events):
        shift = round(((index % 5) - 2) * 0.0005, 7)
        assignments.append({"event_index": index, "grid_index": index,
                            "event_s": event["source_s"],
                            "target_s": round(event["source_s"] + shift, 7),
                            "qn": float(index), "offset_s": shift})
    plan = build_timing_plan(events, assignments, strength=1.0,
                             duration_s=12.0, group_gap_s=0.05,
                             group_shift_s=0.0001)
    assert len(plan["blocks"]) == 48
    assert plan["blocks"][0]["start_s"] == 0.0
    assert plan["blocks"][-1]["end_s"] == 12.0

    project = _project(item_length_s=12.0)
    result = apply_timing_plan(project, plan, item_guid=ITEM_GUID,
                               track_guid=TRACK_GUID)

    assert result["status"] == "applied"
    assert result["block_count"] == 48
    assert all(abs(item.fade_in_s - 0.0045) < 1e-9 for item in project.items)
    assert all(item.playrate == 1.0 for item in project.items)
    assert result["readback"]["matched"] is True


# ---------------------------------------------------------------------------
# Bridge contract
# ---------------------------------------------------------------------------


def _crossfade_fixture(tmp_path, project):
    from reaper_audio.daw.timing_service import _file_fingerprint

    source = tmp_path / "di.wav"
    source.write_bytes(b"fixture")
    project.items[0].source_path = str(source)
    return {
        "method": "crossfade", "plan_id": "child", "parent_plan_id": "parent",
        "project_id": project.project_id, "track_guid": TRACK_GUID,
        "item_guid": ITEM_GUID, "take_guid": TAKE_GUID, "source_path": str(source),
        "source_fingerprint": list(_file_fingerprint(source)),
        "source_offset_s": 0.0, "item_position_s": 1.0, "item_length_s": 12.0,
        "tempo_context": project.snapshot().get("tempo"),
        "blocks": [
            {"position_s": 1.0, "source_start_s": 0.0, "length_s": 6.01,
             "fade_in_s": 0.0, "fade_out_s": 0.01},
            {"position_s": 7.0, "source_start_s": 6.0, "length_s": 6.0,
             "fade_in_s": 0.01, "fade_out_s": 0.0},
        ],
    }


def test_refined_plan_needs_its_own_dry_run_at_current_revision(tmp_path, monkeypatch):
    from reaper_audio.daw.timing_service import PLANS

    project = _project(item_length_s=12.0)
    plan = _crossfade_fixture(tmp_path, project)
    monkeypatch.setitem(PLANS, "child", plan)
    monkeypatch.setitem(PLANS, "sibling", {**plan, "plan_id": "sibling"})
    assert apply_timing_plan(project, "sibling", dry_run=True)["status"] == "dry_run"
    with pytest.raises(ValidationError, match="Dry-run this exact"):
        apply_timing_plan(project, "child")
    assert apply_timing_plan(project, "child", dry_run=True)["status"] == "dry_run"
    project.bump()
    with pytest.raises(ValidationError, match="Dry-run this exact"):
        apply_timing_plan(project, "child")
    assert apply_timing_plan(project, "child", dry_run=True)["status"] == "dry_run"
    assert apply_timing_plan(project, "child")["readback"]["matched"]


def test_crossfade_refuses_old_montage_before_writing(tmp_path):
    project = _project(item_length_s=12.0)
    plan = _crossfade_fixture(tmp_path, project)
    project.add_item(guid="{OLD}", position_s=2.0, length_s=1.0,
                     source_path=plan["source_path"])
    with pytest.raises(ValidationError, match="overlapping clips"):
        apply_timing_plan(project, plan)
    assert project.plan_calls == 0


def test_crossfade_undoes_a_readback_mismatch(tmp_path):
    project = _project(item_length_s=12.0, auto_fade_s=0.005, verify_geometry=False)
    plan = _crossfade_fixture(tmp_path, project)
    result = apply_timing_plan(project, plan)
    assert result["status"] == "rolled_back_readback_mismatch"
    assert not result["applied"]
    assert result["rollback"]["readback"]["matched"]
    assert [item.guid for item in project.items] == [ITEM_GUID]


def test_manual_later_attack_roundtrip_keeps_the_neighbour(tmp_path, monkeypatch):
    from reaper_audio.daw.timing_refine_actions import insert_seam, manual_attack
    from reaper_audio.daw.timing_service import PLANS

    project = _project(item_length_s=12.0)
    parent = _crossfade_fixture(tmp_path, project)
    parent.update(parent_plan_id=None, duration_s=12.0, anchors=[
        {"id": "neighbour", "source_s": 5.1, "project_s": 6.1, "target_s": 6.1},
    ])
    parent["blocks"] = [
        {"position_s": 1.0, "source_start_s": 0.0, "length_s": 12.0,
         "event_ids": ["neighbour"], "fade_in_s": 0.0, "fade_out_s": 0.0},
    ]
    manual, changes = manual_attack(parent, source_s=5.0, project_s=6.0,
                                    target_s=6.045, attack_end_s=5.004)
    child, shift = insert_seam(manual, event_id=changes["event_id"], max_shift_s=0.05)
    assert shift["applied"]
    for plan in (parent, manual, child):
        monkeypatch.setitem(PLANS, plan["plan_id"], plan)
    with pytest.raises(ValidationError, match="superseded"):
        apply_timing_plan(project, manual["plan_id"])
    assert apply_timing_plan(project, child["plan_id"], dry_run=True)["status"] == "dry_run"
    assert apply_timing_plan(project, child["plan_id"])["readback"]["matched"]
    for source, expected in [(5.0, 6.045), (5.1, 6.1)]:
        occurrences = [item.position_s + source - item.source_offset_s
                       for item in project.items if item.source_offset_s <= source
                       < item.source_offset_s + item.length_s - 1e-6]
        assert occurrences == pytest.approx([expected])


def test_bridge_declares_and_applies_the_typed_operation() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "reaper_bridge.lua"
    text = script.read_text(encoding="utf-8", errors="replace")
    assert "local function validate_item_timing_values" in text
    assert 'elseif kind == "item.timing_apply" then' in text
    assert '"item.timing_apply",' in text  # listed in supported operations
    assert "D_FADEINLEN_AUTO" in text and "D_FADEOUTLEN_AUTO" in text
    assert "timing_note_prefix" in text
    assert "timing_geometry_mismatch" in text

@pytest.mark.parametrize("condition,field", [
    ("extra", "extra_item_guids"), ("missing_other", "missing_item_guids"),
    ("retained", "source_item"), ("no_audio", "source_path"), ("offline", "source_path"),
])
def test_readback_requires_exact_track_membership_and_media(condition, field):
    from reaper_audio.daw.timing_apply import normalize_clips, verify_applied_geometry

    row = {"position_s": 1.0, "length_s": 1.0, "source_start_s": 0.0,
           "fade_in_s": 0.0, "fade_out_s": 0.0}
    clip, = normalize_clips([row], project_start_s=1.0)
    new = {**row, "start_offset_s": 0.0, "guid": "new", "playrate": 1.0,
           "active_take": {"source": SOURCE_PATH}}
    items = [new, {"guid": "keep", "position_s": 10.0}]
    if condition == "extra":
        items.append({"guid": "leftover"})
    elif condition == "missing_other":
        items.pop()
    elif condition == "retained":
        items.append({"guid": "old"})
    elif condition == "no_audio":
        new["active_take"] = {}
    elif condition == "offline":
        new["source_online"] = False
    report = verify_applied_geometry(
        {"tracks": [{"guid": TRACK_GUID, "items": items}]}, track_guid=TRACK_GUID,
        clips=[clip], created_guids=["new"], source_item_guid="old",
        source_removed=condition != "retained", source_path=SOURCE_PATH,
        before_item_guids=["old", "keep"])
    assert not report["matched"]
    assert field in {entry["field"] for entry in report["mismatches"]}
