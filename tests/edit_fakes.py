"""In-memory REAPER double for the safe-edit tests.

It implements only the surface the edit layer uses: a project context snapshot,
a structural digest with sends and envelopes, and a revision-bound plan/apply
pair.  Operations mutate the fake project so read-back and receipt undo can be
exercised without a live REAPER.

The fake is deliberately strict about what it reports: it never synthesizes a
send row, envelope point or revision that the operation did not produce.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from reaper_audio.daw.session import EditOperation, ProjectRevision
from reaper_audio.errors import StaleContext


@dataclass
class FakeTrack:
    guid: str
    name: str
    volume: float = 1.0
    pan: float = 0.0
    mute: bool = False
    automation_mode: int = 0
    is_master: bool = False
    index: int = 0


@dataclass
class FakeSend:
    index: int
    destination_guid: str
    volume: float = 1.0
    pan: float = 0.0
    mute: bool = False
    mode: int = 0


@dataclass
class FakeEnvelope:
    guid: str
    name: str
    points: list[dict[str, Any]] = field(default_factory=list)
    active: bool = True
    visible: bool = True
    armed: bool = False
    scaling_mode: int = 0


@dataclass
class FakePlan:
    plan_id: str
    revision: ProjectRevision
    operations: tuple[EditOperation, ...]
    reason: str
    before: list[dict[str, Any]]


class FakeEditProject:
    """A tiny project model with revision-bound transactional apply."""

    def __init__(self, *, project_id: str = "proj_fake", path: str = "fake.rpp"):
        self.project_id = project_id
        self.path = path
        self.change_count = 1
        self.tracks: dict[str, FakeTrack] = {}
        self.master = FakeTrack(guid="master_guid", name="MASTER", is_master=True,
                                index=-1)
        self.sends: dict[str, list[FakeSend]] = {}
        self.envelopes: dict[tuple[str, str], FakeEnvelope] = {}
        self.queue: list[dict[str, Any]] = []
        self._plan_counter = 0
        self._envelope_counter = 0
        self.stale_on_plan = 0
        self.stale_on_apply = 0
        self.stale_always = False
        self.plan_calls = 0
        self.apply_calls = 0
        self.fail_snapshot_after_change = False
        self.coverage = {"tracks": True, "sends": True, "automation": True,
                         "envelopes": True, "master": True}

    # ---- construction -------------------------------------------------
    def add_track(self, guid: str, name: str, *, volume: float = 1.0,
                  pan: float = 0.0, mute: bool = False,
                  automation_mode: int = 0) -> FakeTrack:
        track = FakeTrack(guid=guid, name=name, volume=volume, pan=pan, mute=mute,
                          automation_mode=automation_mode,
                          index=len(self.tracks))
        self.tracks[guid] = track
        self.sends.setdefault(guid, [])
        return track

    def add_send(self, source_guid: str, destination_guid: str, *,
                 volume: float = 1.0, pan: float = 0.0, mute: bool = False) -> int:
        rows = self.sends.setdefault(source_guid, [])
        index = len(rows)
        rows.append(FakeSend(index=index, destination_guid=destination_guid,
                             volume=volume, pan=pan, mute=mute))
        return index

    def add_envelope(self, owner_guid: str, name: str, *,
                     points: list[dict[str, Any]] | None = None,
                     active: bool = True) -> FakeEnvelope:
        self._envelope_counter += 1
        envelope = FakeEnvelope(guid=f"env_guid_{self._envelope_counter}",
                                name=name, active=active,
                                points=list(points or []))
        self.envelopes[(owner_guid, name)] = envelope
        return envelope

    def revision(self) -> ProjectRevision:
        return ProjectRevision(project_id=self.project_id,
                               change_count=self.change_count, path=self.path)

    def bump(self) -> None:
        self.change_count += 1

    # ---- REAPER bridge surface ---------------------------------------
    def snapshot(self, *, include_items: bool = False, **_: Any) -> dict[str, Any]:
        del include_items
        if self.fail_snapshot_after_change and self.change_count > 1:
            raise StaleContext("fake read-back failed after the apply",
                               details={"change_count": self.change_count})
        tracks = [self._track_context(track) for track in self.tracks.values()]
        return {
            "revision": self.revision().to_dict(),
            "track_count": len(tracks),
            "tracks": tracks,
            "master": self._track_context(self.master),
            "length_s": 300.0,
            "transport": {"play_state": 0, "position_s": 0.0},
        }

    def _track_context(self, track: FakeTrack) -> dict[str, Any]:
        return {
            "guid": track.guid, "index": track.index, "name": track.name,
            "is_master": track.is_master, "volume": track.volume, "pan": track.pan,
            "mute": track.mute, "solo": 0, "automation_mode": track.automation_mode,
        }

    def digest(self) -> dict[str, Any]:
        track_rows = []
        for track in self.tracks.values():
            track_rows.append(self._digest_track(track))
        master_row = self._digest_track(self.master)
        automation_tracks = [
            {"track_guid": track.guid,
             "envelopes": [self._envelope_row(track.guid, envelope)
                           for (owner, _), envelope in self.envelopes.items()
                           if owner == track.guid]}
            for track in self.tracks.values()
        ]
        master_envelopes = [self._envelope_row(self.master.guid, envelope)
                            for (owner, _), envelope in self.envelopes.items()
                            if owner == self.master.guid]
        return {
            "project_id": self.project_id,
            "path": self.path,
            "change_count": self.change_count,
            "digest_version": 1,
            "coverage": dict(self.coverage),
            "missing": [],
            "fields": {
                "tracks": track_rows,
                "items": [],
                "tempo": [],
                "master": master_row,
                "automation": {"tracks": automation_tracks,
                               "master": master_envelopes,
                               "complete": True, "gaps": []},
            },
        }

    def _digest_track(self, track: FakeTrack) -> dict[str, Any]:
        return {
            "guid": track.guid, "volume": track.volume, "pan": track.pan,
            "mute": track.mute, "solo": 0,
            "sends": [self._send_row(track.guid, row)
                      for row in self.sends.get(track.guid, [])],
        }

    def _send_row(self, source_guid: str, row: FakeSend) -> dict[str, Any]:
        destination = self.tracks.get(row.destination_guid)
        return {
            "index": row.index, "dest_guid": row.destination_guid,
            "dest_name": destination.name if destination else "",
            "volume": row.volume, "pan": row.pan, "mute": row.mute,
            "mode": row.mode,
        }

    def _envelope_row(self, owner_guid: str, envelope: FakeEnvelope) -> dict[str, Any]:
        del owner_guid
        return {
            "guid": envelope.guid, "name": envelope.name,
            "active": envelope.active, "visible": envelope.visible,
            "armed": envelope.armed, "point_count": len(envelope.points),
            "scaling_mode": envelope.scaling_mode,
            "points": [{"index": index, "time_s": point["time_s"],
                        "value": point["value"], "shape": point.get("shape", 0),
                        "tension": point.get("tension", 0.0)}
                       for index, point in enumerate(sorted(
                           envelope.points, key=lambda item: item["time_s"]))],
        }

    # ---- transactions -------------------------------------------------
    def plan(self, operations: list[EditOperation], *, reason: str,
             expected_revision: Any = None) -> FakePlan:
        del expected_revision
        self.plan_calls += 1
        if self.stale_always or self.stale_on_plan > 0:
            if not self.stale_always:
                self.stale_on_plan -= 1
            self.bump()
            raise StaleContext(
                "fake background change before planning",
                details={"change_count": self.change_count},
            )
        self._plan_counter += 1
        before = [{"operation_index": index, "kind": operation.kind,
                   "target_guid": operation.target_guid,
                   "values": self._read_state(operation)}
                  for index, operation in enumerate(operations)]
        return FakePlan(plan_id=f"plan_{self._plan_counter}",
                        revision=self.revision(),
                        operations=tuple(operations), reason=reason,
                        before=before)

    def apply(self, plan: FakePlan, *, dry_run: bool = True) -> dict[str, Any]:
        self.apply_calls += 1
        if self.stale_on_apply > 0:
            self.stale_on_apply -= 1
            self.bump()
            raise StaleContext("fake background change before apply",
                               details={"change_count": self.change_count})
        if plan.revision != self.revision():
            raise StaleContext("fake plan is stale",
                               details={"planned": plan.revision.to_dict(),
                                        "current": self.revision().to_dict()})
        before = plan.before
        if dry_run:
            return {"applied": False, "dry_run": True, "plan_id": plan.plan_id,
                    "revision": self.revision().to_dict(), "before": before,
                    "operations": [operation.to_dict()
                                   for operation in plan.operations]}
        changed = False
        results: list[dict[str, Any]] = []
        for operation in plan.operations:
            result = self._apply_operation(operation)
            results.append(result)
            changed = changed or bool(result.get("changed"))
        if not changed:
            return {"applied": True, "dry_run": False, "no_op": True,
                    "plan_id": plan.plan_id,
                    "revision_before": self.revision().to_dict(),
                    "revision_after": self.revision().to_dict(),
                    "before": before, "after": before, "undo_available": False,
                    "operation_results": results}
        self.bump()
        after = [{"operation_index": index, "kind": operation.kind,
                  "target_guid": operation.target_guid,
                  "values": self._read_state_after(operation)}
                 for index, operation in enumerate(plan.operations)]
        return {"applied": True, "dry_run": False, "no_op": False,
                "plan_id": plan.plan_id,
                "revision_before": plan.revision.to_dict(),
                "revision_after": self.revision().to_dict(),
                "before": before, "after": after,
                "operation_results": results, "undo_available": True}

    def _read_state(self, operation: EditOperation) -> dict[str, Any]:
        kind, values = operation.kind, operation.values
        if kind == "track.set":
            track = self.tracks.get(operation.target_guid)
            if track is None:
                return {}
            state: dict[str, Any] = {}
            for key in values:
                state[key] = getattr(track, key)
            return state
        if kind == "send.set":
            row = self._send_at(operation.target_guid, values.get("send_index"))
            if row is None:
                return {}
            return {"send_index": row.index, "volume": row.volume, "pan": row.pan,
                    "mute": row.mute}
        if kind == "send.create":
            return {"send_count": len(self.sends.get(operation.target_guid, [])),
                    "destination_guid": values.get("destination_guid")}
        if kind == "send.delete":
            return {"send_count": len(self.sends.get(operation.target_guid, [])),
                    "send_index": values.get("send_index")}
        if kind == "envelope.set":
            envelope = self.envelopes.get((operation.target_guid,
                                           str(values.get("envelope"))))
            return {"envelope": values.get("envelope"),
                    "point_count": len(envelope.points) if envelope else 0,
                    "scaling_mode": envelope.scaling_mode if envelope else 0}
        if kind == "track.delete":
            track = self.tracks.get(operation.target_guid)
            return {"track_name": track.name if track else ""}
        return dict(values)

    def _read_state_after(self, operation: EditOperation) -> dict[str, Any]:
        """Mirror the bridge's post-apply facts for deletes."""
        if operation.kind == "send.delete":
            rows = self.sends.get(operation.target_guid, [])
            index = operation.values.get("send_index")
            return {"exists": isinstance(index, int) and 0 <= index < len(rows),
                    "parent_exists": operation.target_guid in self.tracks,
                    "send_index": index, "send_count": len(rows)}
        if operation.kind == "track.delete":
            return {"exists": operation.target_guid in self.tracks}
        return self._read_state(operation)

    def _apply_operation(self, operation: EditOperation) -> dict[str, Any]:
        kind, values = operation.kind, operation.values
        if kind == "track.set":
            track = self.tracks.get(operation.target_guid)
            if track is None:
                raise StaleContext("fake track is missing",
                                   details={"guid": operation.target_guid})
            for key, value in values.items():
                setattr(track, key, value)
            return {"changed": True}
        if kind == "send.create":
            rows = self.sends.setdefault(operation.target_guid, [])
            index = len(rows)
            rows.append(FakeSend(
                index=index,
                destination_guid=str(values["destination_guid"]),
                volume=float(values.get("volume", 1.0)),
                pan=float(values.get("pan", 0.0)),
                mute=bool(values.get("mute", False)),
            ))
            return {"changed": True, "send_index": index}
        if kind == "send.set":
            index = int(values["send_index"])
            row = self._send_at(operation.target_guid, index)
            if row is None:
                raise StaleContext("fake send is missing",
                                   details={"send_index": index})
            for key in ("volume", "pan", "mute"):
                if key in values:
                    setattr(row, key, values[key])
            return {"changed": True, "send_index": index}
        if kind == "send.delete":
            index = int(values["send_index"])
            rows = self.sends.get(operation.target_guid, [])
            if index >= len(rows):
                raise StaleContext("fake send is missing",
                                   details={"send_index": index})
            rows.pop(index)
            for new_index, row in enumerate(rows):
                row.index = new_index
            return {"changed": True, "removed_send_index": index}
        if kind == "envelope.set":
            key = (operation.target_guid, str(values["envelope"]))
            envelope = self.envelopes.get(key)
            if envelope is None:
                raise StaleContext("fake envelope is missing",
                                   details={"envelope": values["envelope"]})
            if values.get("clear"):
                envelope.points = []
            for point in values.get("points") or ():
                envelope.points = [
                    existing for existing in envelope.points
                    if abs(float(existing["time_s"]) - float(point["time_s"])) > 1e-9
                ]
                envelope.points.append({
                    "time_s": float(point["time_s"]),
                    "value": float(point["value"]),
                    "shape": int(point.get("shape") or 0),
                    "tension": float(point.get("tension") or 0.0),
                })
            envelope.points.sort(key=lambda item: item["time_s"])
            for index, point in enumerate(envelope.points):
                point["index"] = index
            if values.get("active") is not None:
                envelope.active = bool(values["active"])
            return {"changed": True, "envelope": values["envelope"],
                    "points_inserted": len(values.get("points") or ()),
                    "cleared": bool(values.get("clear"))}
        if kind == "track.delete":
            track = self.tracks.pop(operation.target_guid, None)
            if track is None:
                raise StaleContext("fake track is missing",
                                   details={"guid": operation.target_guid})
            self.sends.pop(operation.target_guid, None)
            for key in [key for key in self.envelopes if key[0] == track.guid]:
                self.envelopes.pop(key, None)
            return {"changed": True}
        raise StaleContext("fake does not support this operation kind",
                           details={"kind": kind})

    def _send_at(self, source_guid: str, index: Any) -> FakeSend | None:
        if isinstance(index, bool) or not isinstance(index, int):
            return None
        rows = self.sends.get(source_guid, [])
        return rows[index] if 0 <= index < len(rows) else None


class FakeMeasure:
    """A measure service that derives one level metric from the fake faders.

    ``base_db`` and the metric name are parameters, so a test can ask for any
    unit without this fake encoding a mix decision.
    """

    def __init__(self, project: FakeEditProject, *, metric: str = "lufs",
                 base_db: float | None = None):
        self.project = project
        self.metric = metric
        self.base_db = base_db
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *, sources: list[str], interval: Any,
                 metrics: list[str]) -> dict[str, Any]:
        self.calls.append({"sources": list(sources), "interval": interval,
                           "metrics": list(metrics)})
        rows: dict[str, dict[str, Any]] = {}
        for source in sources:
            track = self.project.tracks.get(source) or (
                self.project.master if source == self.project.master.guid else None)
            level = (self.base_db if self.base_db is not None
                     else 20.0 * math.log10(max(track.volume, 1e-12))
                     if track is not None else None)
            rows[source] = {metric: level for metric in metrics}
        return rows


__all__ = ["FakeEditProject", "FakeEnvelope", "FakeMeasure", "FakePlan",
           "FakeSend", "FakeTrack"]
