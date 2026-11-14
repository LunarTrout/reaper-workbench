"""Topology-aware render batch planning for full capture.

Batching exists only so REAPER can render the next group of tracks while the
previous group is analysed.  The plan is derived from stable track GUIDs and
the project's current folder/send topology; names, genres and a fixed track
count are never consulted.

Two measured REAPER constraints shape the rules:

* A call that selects exactly one track is a different render path from a
  multi-track selection (deterministic PCM differences in the folder probe),
  so a project with more than one target track never gets a one-track batch.
* Folder subtrees are kept atomic so folder FX and nested routing are not
  split across renders.

The planner is pure: it never calls the bridge and never reads audio.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import ValidationError

DEFAULT_CAPTURE_BATCH_TARGET = 4
MAX_CAPTURE_BATCH_TARGET = 16


@dataclass(frozen=True)
class CaptureBatch:
    """One render call's stable track identities, in project order."""

    guids: tuple[str, ...]
    track_indexes: tuple[int, ...]


@dataclass(frozen=True)
class CaptureBatchPlan:
    """A complete, non-overlapping partition of the target tracks.

    ``batches`` is empty exactly when ``fallback_reason`` is set.  A fallback
    means the caller must use the proven single full-project render instead of
    a subset render; it is never a reason to skip a track.
    """

    batches: tuple[CaptureBatch, ...]
    units: tuple[tuple[str, ...], ...]
    fallback_reason: str | None
    cross_batch_sends: int
    master_sends: int
    unresolved_send_destinations: int
    hardware_output_guids: tuple[str, ...]
    topology_source: str

    @property
    def track_count(self) -> int:
        return sum(len(batch.guids) for batch in self.batches)

    def summary(self) -> dict[str, Any]:
        return {
            "batch_count": len(self.batches),
            "batch_sizes": [len(batch.guids) for batch in self.batches],
            "batch_track_indexes": [list(batch.track_indexes) for batch in self.batches],
            "unit_count": len(self.units),
            "fallback_reason": self.fallback_reason,
            "cross_batch_sends": self.cross_batch_sends,
            "master_sends": self.master_sends,
            "unresolved_send_destinations": self.unresolved_send_destinations,
            "hardware_output_tracks": list(self.hardware_output_guids),
            "topology_source": self.topology_source,
        }


@dataclass(frozen=True)
class _Track:
    guid: str
    index: int
    folder_depth: int
    snapshot_sends: int
    digest_row: Mapping[str, Any] | None


def _fallback(reason: str, tracks: int, *, topology_source: str = "unavailable",
              hardware: tuple[str, ...] = ()) -> CaptureBatchPlan:
    return CaptureBatchPlan(
        batches=(),
        units=(),
        fallback_reason=reason,
        cross_batch_sends=0,
        master_sends=0,
        unresolved_send_destinations=0,
        hardware_output_guids=hardware,
        topology_source=topology_source,
    )


def _digest_track_rows(digest: Mapping[str, Any] | None) -> tuple[dict[str, Mapping[str, Any]], str]:
    if not isinstance(digest, Mapping):
        return {}, "snapshot_only"
    fields = digest.get("fields")
    rows: Any = None
    if isinstance(fields, Mapping):
        rows = fields.get("tracks")
    if rows is None:
        rows = digest.get("tracks")
    if not isinstance(rows, list):
        return {}, "snapshot_only"
    by_guid: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        guid = row.get("guid")
        if isinstance(guid, str) and guid:
            by_guid[guid] = row
    return by_guid, "digest+snapshot"


def _normalise_tracks(tracks: Sequence[Any],
                      rows: Mapping[str, Mapping[str, Any]]) -> list[_Track] | None:
    entries: list[_Track] = []
    seen: set[str] = set()
    for fallback_index, track in enumerate(tracks):
        if not isinstance(track, Mapping):
            return None
        guid = track.get("guid")
        if not isinstance(guid, str) or not guid or guid in seen:
            return None
        seen.add(guid)
        row = rows.get(guid)
        raw_depth: Any = row.get("folder_depth") if row is not None else track.get("folder_depth")
        if isinstance(raw_depth, bool) or not isinstance(raw_depth, (int, float)):
            return None
        if not math.isfinite(float(raw_depth)) or float(raw_depth) % 1.0 != 0.0:
            return None
        snapshot_sends = track.get("sends")
        if isinstance(snapshot_sends, bool) or not isinstance(snapshot_sends, int):
            snapshot_sends = 0
        entries.append(_Track(
            guid=guid,
            index=int(track.get("index", fallback_index))
            if isinstance(track.get("index", fallback_index), int) else fallback_index,
            folder_depth=int(raw_depth),
            snapshot_sends=max(0, snapshot_sends),
            digest_row=row,
        ))
    return entries


def _topology_units(entries: Sequence[_Track]) -> tuple[list[list[int]], str | None]:
    """Split project order into atomic top-level folder subtrees.

    ``I_FOLDERDEPTH`` is a delta: positive opens folder levels after the track,
    negative closes levels after the track.  A top-level folder and every
    descendant therefore share the first stack entry as their unit.
    """
    units: list[list[int]] = []
    stack: list[int] = []
    for position, entry in enumerate(entries):
        depth = entry.folder_depth
        if depth < 0 and not stack:
            return [], "unbalanced_folder_close"
        unit_id = stack[0] if stack else len(units)
        if not stack:
            units.append([])
        units[unit_id].append(position)
        if depth > 0:
            stack.extend([unit_id] * depth)
        elif depth < 0:
            if -depth > len(stack):
                return [], "unbalanced_folder_close"
            del stack[-(-depth):]
    if stack:
        return [], "unbalanced_folder_open"
    if not units:
        return [], "no_tracks"
    return units, None


def _pack_units(units: Sequence[Sequence[int]], target_batches: int) -> list[list[int]]:
    total = sum(len(unit) for unit in units)
    if total <= 1:
        return [list(unit) for unit in units]
    desired = min(target_batches, len(units), max(1, math.ceil(total / 2)))
    target_size = max(2, math.ceil(total / desired))
    batches: list[list[int]] = []
    current: list[int] = []
    for unit in units:
        candidate = list(unit)
        if current and len(current) + len(candidate) > target_size:
            batches.append(current)
            current = []
        current.extend(candidate)
    if current:
        batches.append(current)

    # A one-track render batch is a different REAPER path.  Merge every
    # singleton remainder into an adjacent batch, preferring the smaller one.
    while len(batches) > 1:
        singleton = next((index for index, batch in enumerate(batches)
                          if len(batch) == 1), None)
        if singleton is None:
            break
        if singleton == 0:
            batches[1] = batches[0] + batches[1]
            del batches[0]
            continue
        if singleton == len(batches) - 1:
            batches[singleton - 1].extend(batches[singleton])
            del batches[singleton]
            continue
        previous = batches[singleton - 1]
        following = batches[singleton + 1]
        if len(previous) <= len(following):
            previous.extend(batches[singleton])
            del batches[singleton]
        else:
            batches[singleton + 1] = batches[singleton] + batches[singleton + 1]
            del batches[singleton]
    return batches


def _send_topology(entries: Sequence[_Track],
                   assignment: Mapping[str, int]) -> tuple[int, int, int, tuple[str, ...]]:
    known = set(assignment)
    cross_batch = 0
    master_sends = 0
    unresolved = 0
    hardware: list[str] = []
    for entry in entries:
        row = entry.digest_row
        if isinstance(row, Mapping):
            outputs = row.get("hardware_outputs")
            if isinstance(outputs, list) and outputs:
                hardware.append(entry.guid)
            sends = row.get("sends")
            if isinstance(sends, list):
                for send in sends:
                    if not isinstance(send, Mapping):
                        unresolved += 1
                        continue
                    destination = send.get("dest_guid")
                    if not isinstance(destination, str) or not destination:
                        unresolved += 1
                        continue
                    if destination not in known:
                        # A send to the master is still project routing, but it
                        # cannot cross a track batch because the master is
                        # rendered by its own pass.
                        master_sends += 1
                        continue
                    if assignment[destination] != assignment[entry.guid]:
                        cross_batch += 1
                continue
        if entry.snapshot_sends:
            unresolved += entry.snapshot_sends
    return cross_batch, master_sends, unresolved, tuple(hardware)


def plan_capture_batches(
    tracks: Sequence[Any],
    digest: Mapping[str, Any] | None = None,
    *,
    target_batches: int = DEFAULT_CAPTURE_BATCH_TARGET,
) -> CaptureBatchPlan:
    """Partition target tracks into safe multi-track render batches.

    ``tracks`` is the live project snapshot's track list (project order).
    ``digest`` is the bridge project digest; its per-track rows supply folder
    depth and send destinations when present.  Invalid or incomplete topology
    returns ``fallback_reason`` and no batches, which the caller must treat as
    "render the whole project in one proven call", never as "skip work".
    """
    if isinstance(target_batches, bool) or not isinstance(target_batches, int):
        raise ValidationError("target_batches must be an integer")
    if target_batches < 1 or target_batches > MAX_CAPTURE_BATCH_TARGET:
        raise ValidationError(
            f"target_batches must be between 1 and {MAX_CAPTURE_BATCH_TARGET}",
            details={"requested": target_batches},
        )
    if not isinstance(tracks, (list, tuple)) or not tracks:
        return _fallback("no_tracks", 0)

    rows, topology_source = _digest_track_rows(digest)
    entries = _normalise_tracks(tracks, rows)
    if entries is None:
        return _fallback("unstable_track_identity", len(tracks))

    units, reason = _topology_units(entries)
    if reason is not None:
        return _fallback(reason, len(entries), topology_source=topology_source)

    packed = _pack_units(units, target_batches)
    if not packed:
        return _fallback("empty_partition", len(entries), topology_source=topology_source)
    if len(entries) > 1 and any(len(batch) == 1 for batch in packed):
        return _fallback("singleton_batch", len(entries), topology_source=topology_source)

    batches: list[CaptureBatch] = []
    assignment: dict[str, int] = {}
    seen: set[int] = set()
    for batch_index, positions in enumerate(packed):
        ordered = sorted(positions)
        guids = tuple(entries[position].guid for position in ordered)
        indexes = tuple(entries[position].index for position in ordered)
        for position in ordered:
            if position in seen:
                return _fallback("duplicate_partition", len(entries),
                                 topology_source=topology_source)
            seen.add(position)
        for guid in guids:
            assignment[guid] = batch_index
        batches.append(CaptureBatch(guids=guids, track_indexes=indexes))
    if len(seen) != len(entries):
        return _fallback("incomplete_partition", len(entries),
                         topology_source=topology_source)

    cross, master_sends, unresolved, hardware = _send_topology(entries, assignment)
    return CaptureBatchPlan(
        batches=tuple(batches),
        units=tuple(tuple(entries[position].guid for position in unit) for unit in units),
        fallback_reason=None,
        cross_batch_sends=cross,
        master_sends=master_sends,
        unresolved_send_destinations=unresolved,
        hardware_output_guids=hardware,
        topology_source=topology_source,
    )


__all__ = [
    "CaptureBatch",
    "CaptureBatchPlan",
    "DEFAULT_CAPTURE_BATCH_TARGET",
    "MAX_CAPTURE_BATCH_TARGET",
    "plan_capture_batches",
]
