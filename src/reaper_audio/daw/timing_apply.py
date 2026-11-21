"""Apply a timing plan's 1x block geometry as one typed edit transaction.

Why a dedicated operation kind
------------------------------
``item.split`` moves the right-hand slice as a whole and offers no way to set
the left slice's source offset or both fade lengths, so a plan of N clips
cannot be built from splits without dropping, repeating or mis-offsetting
source material (a 161-block plan would also need well over 256 operations).
``item.set`` has no ``start_offset_s`` field.  Creating every clip with
``item.create`` + ``item.set`` would need two operations per block, would
still leave the original item in place, and could not be verified as one unit.

This module therefore compiles a timing plan into exactly one
``item.timing_apply`` operation on the existing edit transaction.  The
operation replaces one file-backed 1x item with N clips of the same take.
Position, source offset, length, playrate 1.0 and both fade lengths are
written explicitly; the bridge reads every clip back before it commits, and
this module reads the project again through the normal snapshot afterwards.
The operation also carries a geometry digest (``apply_id``): clips created by
an interrupted earlier attempt on the same track are removed before the new
clips are created, so a retry cannot duplicate audio.

Contract
--------
* one revision-bound ``session.plan`` / ``session.apply`` pair per plan;
* pre-flight checks before anything is sent to REAPER: the item is a
  file-backed 1x audio take without stretch markers, the plan covers the
  item's whole source window without gaps, every clip stays inside the source
  file, and no protected range is cut or moved;
* read-back of every created clip against the plan, including duplicate and
  leftover detection;
* undo goes through the transaction's own revision-bound receipt
  (``session.undo`` with the applied ``plan_id`` and post-apply revision) and
  is verified against the recorded original geometry.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import StaleContext, ValidationError
from .session import EditOperation, ProjectRevision

SCHEMA = "reaper-workbench.timing-apply/1"
OPERATION_KIND = "item.timing_apply"
MAX_BLOCKS = 400
#: Seconds.  A clip boundary closer than this to a requested value counts as
#: the same value; REAPER stores doubles and the plan is rounded to 1e-7.
GEOMETRY_ATOL = 1e-6
COVERAGE_ATOL = 1e-6
#: Adjacent blocks may overlap by a seam crossfade at 1x.  The planner reports
#: a shift jump from 30 ms, so anything past this is a broken plan, not a seam.
MAX_SEAM_OVERLAP_S = 0.060

BlockInput = Mapping[str, Any]
APPLIED: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------


def _finite(value: object, *, field: str, block_index: int | None = None,
            code: str = "timing_block_invalid") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a finite number",
                              details={"code": code, "field": field,
                                       "block_index": block_index,
                                       "value": value})
    number = float(value)
    if not math.isfinite(number):
        raise ValidationError(f"{field} must be a finite number",
                              details={"code": code, "field": field,
                                       "block_index": block_index,
                                       "value": value})
    return number


def _round(value: float) -> float:
    return round(float(value), 9)


# ---------------------------------------------------------------------------
# Clip geometry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimingClip:
    """One 1x clip: where it plays, which file samples it reads, how it fades."""

    position_s: float
    source_start_s: float
    length_s: float
    fade_in_s: float
    fade_out_s: float
    rate: float
    event_ids: tuple[str, ...] = ()

    @property
    def source_end_s(self) -> float:
        return self.source_start_s + self.length_s

    @property
    def end_s(self) -> float:
        return self.position_s + self.length_s

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_s": _round(self.position_s),
            "source_start_s": _round(self.source_start_s),
            "length_s": _round(self.length_s),
            "fade_in_s": _round(self.fade_in_s),
            "fade_out_s": _round(self.fade_out_s),
            "rate": self.rate,
            "event_ids": list(self.event_ids),
        }

    def to_operation_values(self) -> dict[str, Any]:
        """The exact ``values.blocks`` entry the bridge validates."""
        return self.to_dict()


def _clip_from_block(block: BlockInput, index: int, *,
                     project_start_s: float) -> TimingClip:
    """Accept either a planner block or explicit clip geometry.

    ``project_start_s`` is the project time at which the take's source sample 0
    would play (``item.position_s - item.start_offset_s`` at 1x).  The planner
    reports target times relative to that origin, so the same block list can be
    re-applied after the item itself moved only when the caller passes the
    current item geometry.
    """
    if not isinstance(block, Mapping):
        raise ValidationError("Every timing block must be an object",
                              details={"code": "timing_block_invalid",
                                       "block_index": index})
    explicit = {"position_s", "source_start_s", "length_s"} <= set(block)
    if explicit:
        position = _finite(block["position_s"], field="position_s",
                           block_index=index)
        source_start = _finite(block["source_start_s"], field="source_start_s",
                               block_index=index)
        length = _finite(block["length_s"], field="length_s", block_index=index)
        if "source_end_s" in block:
            source_end = _finite(block["source_end_s"], field="source_end_s",
                                 block_index=index)
            if abs((source_end - source_start) - length) > GEOMETRY_ATOL:
                raise ValidationError(
                    "A timing block's source_end_s disagrees with its length",
                    details={"code": "timing_block_invalid", "block_index": index,
                             "source_end_s": source_end,
                             "expected_source_end_s": source_start + length})
    else:
        missing = [name for name in ("start_s", "end_s", "target_start_s",
                                     "target_end_s", "fade_in_s", "fade_out_s")
                   if name not in block]
        if missing:
            raise ValidationError(
                "A timing block is missing required geometry",
                details={"code": "timing_block_invalid", "block_index": index,
                         "missing": missing,
                         "next_action": ("pass a planner block or explicit "
                                         "position_s/source_start_s/length_s")})
        start_s = _finite(block["start_s"], field="start_s", block_index=index)
        end_s = _finite(block["end_s"], field="end_s", block_index=index)
        target_start = _finite(block["target_start_s"], field="target_start_s",
                               block_index=index)
        target_end = _finite(block["target_end_s"], field="target_end_s",
                             block_index=index)
        if end_s <= start_s:
            raise ValidationError("A timing block ends before it starts",
                                  details={"code": "timing_block_invalid",
                                           "block_index": index,
                                           "start_s": start_s, "end_s": end_s})
        if abs((target_end - target_start) - (end_s - start_s)) > GEOMETRY_ATOL:
            raise ValidationError(
                "A timing block's target length differs from its source length; "
                "only 1x blocks can be applied",
                details={"code": "timing_block_invalid", "block_index": index,
                         "source_length_s": end_s - start_s,
                         "target_length_s": target_end - target_start})
        source_start = start_s
        length = end_s - start_s
        position = project_start_s + target_start
        if "source_offset_s" in block:
            declared = _finite(block["source_offset_s"], field="source_offset_s",
                               block_index=index)
            if abs(declared - source_start) > GEOMETRY_ATOL:
                raise ValidationError(
                    "A timing block's source_offset_s disagrees with its start_s",
                    details={"code": "timing_block_invalid",
                             "block_index": index,
                             "source_offset_s": declared,
                             "start_s": source_start})
        if "shift_s" in block:
            shift = _finite(block["shift_s"], field="shift_s", block_index=index)
            actual = position - (project_start_s + source_start)
            if abs(actual - shift) > GEOMETRY_ATOL:
                raise ValidationError(
                    "A timing block's shift_s disagrees with its target geometry",
                    details={"code": "timing_block_invalid",
                             "block_index": index, "shift_s": shift,
                             "derived_shift_s": actual})
    if length <= 0:
        raise ValidationError("A timing block needs a positive length",
                              details={"code": "timing_block_invalid",
                                       "block_index": index, "length_s": length})
    if source_start < 0:
        raise ValidationError("A timing block reads before the start of the file",
                              details={"code": "timing_block_invalid",
                                       "block_index": index,
                                       "source_start_s": source_start})
    if position < 0:
        raise ValidationError("A timing block would play before project zero",
                              details={"code": "timing_block_invalid",
                                       "block_index": index,
                                       "position_s": position})
    rate = _finite(block.get("rate", 1.0), field="rate", block_index=index)
    if abs(rate - 1.0) > 1e-9:
        raise ValidationError(
            "Timing blocks are applied at 1x only; stretch is not supported",
            details={"code": "timing_rate_unsupported", "block_index": index,
                     "rate": rate})
    fade_in = _finite(block.get("fade_in_s", 0.0), field="fade_in_s",
                      block_index=index)
    fade_out = _finite(block.get("fade_out_s", 0.0), field="fade_out_s",
                       block_index=index)
    if fade_in < 0 or fade_out < 0:
        raise ValidationError("Fade lengths must be non-negative",
                              details={"code": "timing_block_invalid",
                                       "block_index": index,
                                       "fade_in_s": fade_in, "fade_out_s": fade_out})
    if fade_in + fade_out > length + GEOMETRY_ATOL:
        raise ValidationError(
            "A timing block's fades are longer than the clip",
            details={"code": "timing_block_invalid", "block_index": index,
                     "fade_in_s": fade_in, "fade_out_s": fade_out,
                     "length_s": length})
    raw_ids = block.get("event_ids") or ()
    if isinstance(raw_ids, (str, bytes)) or not isinstance(raw_ids, Sequence):
        raise ValidationError("event_ids must be a list of event identifiers",
                              details={"code": "timing_block_invalid",
                                       "block_index": index})
    event_ids = tuple(str(item) for item in raw_ids)
    return TimingClip(position_s=position, source_start_s=source_start,
                      length_s=length, fade_in_s=fade_in, fade_out_s=fade_out,
                      rate=rate, event_ids=event_ids)


def normalize_clips(blocks: Sequence[BlockInput], *,
                    project_start_s: float) -> tuple[TimingClip, ...]:
    """Validate blocks and return them in deterministic, playable order.

    Deterministic means the same block set always produces the same operation,
    whatever order the plan lists it in: clips are sorted by source position
    and every ordering, overlap and duplication rule is checked afterwards.
    """
    if isinstance(blocks, (str, bytes)) or not isinstance(blocks, Sequence):
        raise ValidationError("A timing apply needs a list of blocks",
                              details={"code": "timing_blocks_missing"})
    if not blocks:
        raise ValidationError("A timing apply needs at least one block",
                              details={"code": "timing_blocks_missing"})
    if len(blocks) > MAX_BLOCKS:
        raise ValidationError(
            "Too many timing blocks for one transaction",
            details={"code": "timing_blocks_too_many", "blocks": len(blocks),
                     "limit": MAX_BLOCKS})
    clips = [_clip_from_block(block, index, project_start_s=project_start_s)
             for index, block in enumerate(blocks)]
    clips.sort(key=lambda clip: (clip.source_start_s, clip.position_s))
    for index in range(1, len(clips)):
        previous, current = clips[index - 1], clips[index]
        if abs(current.source_start_s - previous.source_start_s) <= COVERAGE_ATOL:
            raise ValidationError(
                "Two timing blocks repeat the same source position",
                details={"code": "timing_source_duplicate", "block_index": index,
                         "source_start_s": _round(current.source_start_s),
                         "position_s": _round(current.position_s)})
        source_overlap = previous.source_end_s - current.source_start_s
        if source_overlap > MAX_SEAM_OVERLAP_S + GEOMETRY_ATOL:
            raise ValidationError(
                "Two timing blocks read overlapping source material by more than a seam",
                details={"code": "timing_source_overlap", "block_index": index,
                         "previous_source_end_s": _round(previous.source_end_s),
                         "source_start_s": _round(current.source_start_s),
                         "overlap_s": _round(source_overlap),
                         "limit_s": MAX_SEAM_OVERLAP_S})
        if current.position_s < previous.position_s - GEOMETRY_ATOL:
            raise ValidationError(
                "The plan would reorder the clips on the timeline",
                details={"code": "timing_target_order", "block_index": index,
                         "previous_position_s": _round(previous.position_s),
                         "position_s": _round(current.position_s)})
        overlap = previous.end_s - current.position_s
        if overlap > MAX_SEAM_OVERLAP_S + GEOMETRY_ATOL:
            raise ValidationError(
                "Adjacent timing blocks overlap more than a seam crossfade",
                details={"code": "timing_seam_overlap", "block_index": index,
                         "overlap_s": _round(overlap),
                         "limit_s": MAX_SEAM_OVERLAP_S,
                         "next_action": "review the plan's adjacent shift jump"})
    return tuple(clips)


def check_source_handles(clips: Sequence[TimingClip], item: Mapping[str, Any]) -> dict[str, Any]:
    """Refuse a plan whose clips read outside the take's file."""
    source_length = item.get("source_length_s")
    if item.get("source_length_is_qn"):
        raise ValidationError(
            "The take's source length is reported in quarter notes, not seconds",
            details={"code": "source_length_unknown",
                     "next_action": "use a time-based file source for timing edits"})
    if isinstance(source_length, bool) or not isinstance(source_length, (int, float)) or \
            not math.isfinite(float(source_length)) or float(source_length) <= 0:
        raise ValidationError(
            "The take does not report a usable source length",
            details={"code": "source_length_unknown",
                     "source_length_s": source_length,
                     "next_action": "re-read the item; timing apply needs a known file length"})
    source_length = float(source_length)
    needed = max(clip.source_end_s for clip in clips)
    if needed > source_length + GEOMETRY_ATOL:
        raise ValidationError(
            "The timing plan needs audio past the end of the take",
            details={"code": "source_handles_unavailable",
                     "needed_s": _round(needed),
                     "source_length_s": _round(source_length),
                     "overflow_s": _round(needed - source_length),
                     "next_action": "reduce the shift or trim the plan to the available source"})
    if min(clip.source_start_s for clip in clips) < -GEOMETRY_ATOL:
        raise ValidationError(
            "The timing plan starts before the take's source file",
            details={"code": "source_handles_unavailable",
                     "source_start_s": _round(min(clip.source_start_s for clip in clips))})
    return {"needed_source_s": _round(needed),
            "source_length_s": _round(source_length)}


def check_coverage(clips: Sequence[TimingClip], item: Mapping[str, Any], *,
                   crossfade: bool = False) -> dict[str, Any]:
    """Check source extent and continuity before sending native clips to REAPER.

    The legacy planner cannot skip source samples. The crossfade planner may
    skip a bounded segment between two measured seam handles, while requiring
    both fades to meet exactly over a gap-free project timeline.
    """
    offset = float(item.get("start_offset_s") or 0.0)
    length = float(item.get("length_s") or 0.0)
    window_end = offset + length
    from ..analysis.timing_seams import MAX_SOURCE_SKIP
    gaps: list[dict[str, Any]] = []
    source_skips = []
    if crossfade:
        if (abs(clips[0].position_s-item["position_s"]) > COVERAGE_ATOL or
                abs(clips[-1].end_s-item["position_s"]-length) > COVERAGE_ATOL):
            gaps.append({"kind": "target_extent"})
        if any(c.source_start_s < offset-COVERAGE_ATOL or
               c.source_end_s > window_end+COVERAGE_ATOL for c in clips):
            gaps.append({"kind": "source_extent"})
    if abs(clips[0].source_start_s - offset) > COVERAGE_ATOL:
        gaps.append({"at_s": _round(clips[0].source_start_s),
                     "expected_s": _round(offset), "kind": "head"})
    for previous, current in zip(clips, clips[1:]):
        gap = current.source_start_s - previous.source_end_s
        # A negative gap is a deliberate handle overlap at the seam, used to
        # fill a target gap with real source material.  A positive gap is a
        # hole and is still refused.
        if crossfade:
            overlap = previous.end_s - current.position_s
            if (overlap <= 0 or abs(overlap-previous.fade_out_s) > COVERAGE_ATOL
                    or abs(overlap-current.fade_in_s) > COVERAGE_ATOL):
                gaps.append({"kind": "target_seam", "overlap_s": _round(overlap)})
            if gap > COVERAGE_ATOL:
                source_skips.append([_round(previous.source_end_s), _round(current.source_start_s)])
        limit = MAX_SOURCE_SKIP + COVERAGE_ATOL if crossfade else COVERAGE_ATOL
        if gap > limit or gap < -MAX_SEAM_OVERLAP_S - GEOMETRY_ATOL:
            gaps.append({"at_s": _round(previous.source_end_s),
                         "expected_s": _round(current.source_start_s),
                         "gap_s": _round(gap), "kind": "gap"})
    if abs(clips[-1].source_end_s - window_end) > COVERAGE_ATOL:
        gaps.append({"at_s": _round(clips[-1].source_end_s),
                     "expected_s": _round(window_end), "kind": "tail"})
    if gaps:
        raise ValidationError(
            "The timing plan does not cover the item's source window exactly",
            details={"code": "plan_does_not_cover_item",
                     "item_source_window_s": [_round(offset), _round(window_end)],
                     "gaps": gaps[:8],
                     "next_action": ("re-plan over the whole item, or trim the item "
                                     "to the analysed range first")})
    report = {"source_window_s": [_round(offset), _round(window_end)]}
    if crossfade:
        report.update({"target_gap_count": 0, "source_seam_skips_s": source_skips})
    return report


def _protected_window(row: Mapping[str, Any], item: Mapping[str, Any],
                      index: int) -> tuple[float, float]:
    if not isinstance(row, Mapping):
        raise ValidationError("protected_ranges entries must be objects",
                              details={"code": "protected_range_invalid",
                                       "range_index": index})
    start = row.get("start_s", row.get("start"))
    end = row.get("end_s", row.get("end"))
    start_s = _finite(start, field="start_s", code="protected_range_invalid")
    end_s = _finite(end, field="end_s", code="protected_range_invalid")
    if end_s <= start_s:
        raise ValidationError("A protected range ends before it starts",
                              details={"code": "protected_range_invalid",
                                       "range_index": index,
                                       "start_s": start_s, "end_s": end_s})
    basis = row.get("basis") or "project_s"
    if basis not in {"project_s", "source_s"}:
        raise ValidationError(
            "protected_ranges basis must be project_s or source_s",
            details={"code": "protected_range_invalid", "range_index": index,
                     "basis": basis})
    offset = float(item.get("start_offset_s") or 0.0)
    position = float(item.get("position_s") or 0.0)
    if basis == "project_s":
        return (offset + (start_s - position), offset + (end_s - position))
    return (start_s, end_s)


def check_protected_ranges(clips: Sequence[TimingClip], item: Mapping[str, Any],
                           ranges: Sequence[Mapping[str, Any]],
                           *, project_start_s: float) -> list[dict[str, Any]]:
    """Refuse a plan that cuts or moves protected material.

    A protected range must be covered by exactly one clip, no clip boundary may
    fall strictly inside it, and that clip must not move (shift 0).  A range
    outside the item is not this plan's material and is reported as untouched.
    """
    if not ranges:
        return []
    offset = float(item.get("start_offset_s") or 0.0)
    length = float(item.get("length_s") or 0.0)
    item_start, item_end = offset, offset + length
    report: list[dict[str, Any]] = []
    for index, row in enumerate(ranges):
        source_start, source_end = _protected_window(row, item, index)
        if source_end <= item_start + COVERAGE_ATOL or source_start >= item_end - COVERAGE_ATOL:
            report.append({"range_index": index, "state": "outside_item",
                           "source_window_s": [_round(source_start), _round(source_end)]})
            continue
        boundaries = [clip.source_start_s for clip in clips] + \
                     [clip.source_end_s for clip in clips]
        for boundary in boundaries:
            if source_start + COVERAGE_ATOL < boundary < source_end - COVERAGE_ATOL:
                raise ValidationError(
                    "The timing plan cuts through a protected range",
                    details={"code": "protected_range_cut", "range_index": index,
                             "source_window_s": [_round(source_start),
                                                 _round(source_end)],
                             "boundary_s": _round(boundary),
                             "next_action": "keep the protected range in one clip"})
        overlapping = [clip for clip in clips
                       if clip.source_start_s < source_end - COVERAGE_ATOL
                       and clip.source_end_s > source_start + COVERAGE_ATOL]
        if not overlapping:
            raise ValidationError(
                "The timing plan drops material inside a protected range",
                details={"code": "protected_range_dropped", "range_index": index,
                         "source_window_s": [_round(source_start),
                                             _round(source_end)],
                         "next_action": "re-plan so the protected range keeps its audio"})
        clip = overlapping[0]
        shift = clip.position_s - (project_start_s + clip.source_start_s)
        if abs(shift) > GEOMETRY_ATOL:
            raise ValidationError(
                "The timing plan moves a protected range",
                details={"code": "protected_range_moved", "range_index": index,
                         "source_window_s": [_round(source_start),
                                             _round(source_end)],
                         "shift_s": _round(shift),
                         "position_s": _round(clip.position_s),
                         "next_action": "pin this block or lower the applied strength"})
        report.append({"range_index": index, "state": "kept",
                       "source_window_s": [_round(source_start), _round(source_end)]})
    return report


# ---------------------------------------------------------------------------
# Item resolution
# ---------------------------------------------------------------------------


def _take_source_path(item: Mapping[str, Any]) -> str:
    take = item.get("active_take") or {}
    for candidate in (take.get("source"), take.get("source_path"),
                      item.get("source_path")):
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""


def _find_track(snapshot: Mapping[str, Any], track_guid: str | None) -> Mapping[str, Any]:
    tracks = [row for row in snapshot.get("tracks") or () if isinstance(row, Mapping)]
    if track_guid is None:
        raise ValidationError("A timing apply needs the source item's track GUID",
                              details={"code": "track_guid_missing"})
    for track in tracks:
        if track.get("guid") == track_guid:
            return track
    raise StaleContext(
        "The track that held the timing source item no longer exists",
        details={"code": "source_track_missing", "track_guid": track_guid,
                 "available_tracks": [row.get("guid") for row in tracks]},
    )


def find_source_item(snapshot: Mapping[str, Any], item_guid: str,
                     *, track_guid: str | None = None) -> tuple[Mapping[str, Any],
                                                                Mapping[str, Any]]:
    """Return ``(track, item)`` for one item GUID, refusing a moved take."""
    tracks = [row for row in snapshot.get("tracks") or () if isinstance(row, Mapping)]
    for track in tracks:
        if track_guid is not None and track.get("guid") != track_guid:
            continue
        for item in track.get("items") or ():
            if isinstance(item, Mapping) and item.get("guid") == item_guid:
                return track, item
    raise StaleContext(
        "The timing source item no longer exists in the project",
        details={"code": "source_item_missing", "item_guid": item_guid,
                 "track_guid": track_guid,
                 "next_action": "re-analyse the take; the item changed or was deleted"},
    )


def _require_applyable_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """Check that the take is a plain file-backed 1x audio source."""
    take = item.get("active_take")
    if not isinstance(take, Mapping):
        raise ValidationError(
            "The timing source item has no active take",
            details={"code": "source_take_missing", "item_guid": item.get("guid")})
    if take.get("is_midi"):
        raise ValidationError(
            "A timing apply needs an audio take, not MIDI",
            details={"code": "source_not_audio", "item_guid": item.get("guid")})
    source_path = _take_source_path(item)
    if not source_path:
        raise ValidationError(
            "The timing source take is not backed by a media file",
            details={"code": "source_not_file_backed", "item_guid": item.get("guid")})
    raw_take_rate = take.get("playrate")
    raw_item_rate = item.get("playrate")
    take_playrate = (float(raw_take_rate)
                     if isinstance(raw_take_rate, (int, float))
                     and not isinstance(raw_take_rate, bool) else 1.0)
    item_playrate = (float(raw_item_rate)
                     if isinstance(raw_item_rate, (int, float))
                     and not isinstance(raw_item_rate, bool) else take_playrate)
    for value in (take_playrate, item_playrate):
        if abs(value - 1.0) > 1e-6:
            raise ValidationError(
                "Timing apply supports 1x source takes only",
                details={"code": "take_not_1x", "playrate": value,
                         "item_guid": item.get("guid")})
    markers = take.get("stretch_markers")
    markers = 0 if markers is None else markers
    positions = item.get("stretch_marker_positions") or ()
    if (isinstance(markers, (int, float)) and not isinstance(markers, bool)
            and int(markers) > 0) or positions:
        raise ValidationError(
            "A take with stretch markers cannot be replaced at 1x without losing them",
            details={"code": "take_has_stretch_markers",
                     "stretch_markers": markers,
                     "next_action": "flatten the take or clear its stretch markers first"})
    if item.get("source_online") is False:
        raise ValidationError(
            "The take's source media is offline",
            details={"code": "source_offline", "item_guid": item.get("guid"),
                     "next_action": "relink the media before applying timing"})
    return {
        "item_guid": str(item.get("guid") or ""),
        "track_guid": None,
        "take_guid": str(take.get("guid") or ""),
        "source_path": source_path,
        "position_s": float(item.get("position_s") or 0.0),
        "length_s": float(item.get("length_s") or 0.0),
        "start_offset_s": float(item.get("start_offset_s") or 0.0),
        "playrate": 1.0,
        "fade_in_s": float(item.get("fade_in_s") or 0.0),
        "fade_out_s": float(item.get("fade_out_s") or 0.0),
        "source_length_s": float(item.get("source_length_s") or 0.0),
        "source_length_is_qn": bool(item.get("source_length_is_qn")),
        "source_online": item.get("source_online"),
        "take_count": item.get("take_count", 1),
        "take_fx_count": take.get("fx_count", 0),
    }


def read_item_geometry(session: Any, *, project_id: str | None,
                       item_guid: str, track_guid: str | None = None) -> dict[str, Any]:
    """Read the source item and its revision without editing anything."""
    snapshot = session.snapshot(project_id=project_id, include_items=True)
    if not isinstance(snapshot, Mapping):
        raise StaleContext("REAPER returned an invalid project snapshot")
    revision = ProjectRevision.from_dict(snapshot.get("revision") or {})
    if not revision.project_id or revision.change_count < 0:
        raise StaleContext("REAPER returned an invalid project revision",
                           details={"code": "revision_unavailable"})
    track, item = find_source_item(snapshot, item_guid, track_guid=track_guid)
    geometry = _require_applyable_item(item)
    geometry["track_guid"] = str(track.get("guid") or "")
    return {"revision": revision, "item": geometry, "snapshot": snapshot,
            "track": track}


def _apply_id(source_path: str, clips: Sequence[TimingClip]) -> str:
    """Stable digest of the clip geometry, used to clean up an interrupted run."""
    payload = {
        "source_path": source_path,
        "clips": [[_round(clip.position_s), _round(clip.source_start_s),
                   _round(clip.length_s), _round(clip.fade_in_s),
                   _round(clip.fade_out_s), clip.rate] for clip in clips],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return "timing_" + digest[:24]


def build_timing_apply_operation(item: Mapping[str, Any],
                                 clips: Sequence[TimingClip],
                                 *, delete_source: bool = True,
                                 source_length_s: float | None = None) -> EditOperation:
    """Compile the clips into the one typed operation the bridge applies."""
    if not clips:
        raise ValidationError("A timing apply needs at least one clip")
    apply_id = _apply_id(str(item["source_path"]), clips)
    values: dict[str, Any] = {
        "apply_id": apply_id,
        "blocks": [clip.to_operation_values() for clip in clips],
        "source_path": str(item["source_path"]),
        "delete_source": bool(delete_source),
    }
    if item.get("take_guid"):
        values["expected_take_guid"] = str(item["take_guid"])
    if source_length_s is not None and source_length_s > 0:
        values["source_length_s"] = round(float(source_length_s), 9)
    return EditOperation(kind=OPERATION_KIND,
                         target_guid=str(item["item_guid"]), values=values)


# ---------------------------------------------------------------------------
# Read-back
# ---------------------------------------------------------------------------


def _item_matches(item: Mapping[str, Any], clip: TimingClip,
                  *, expected_index: int) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []

    def compare(field: str, expected: float, actual: object) -> None:
        if isinstance(actual, bool) or not isinstance(actual, (int, float)) or \
                not math.isfinite(float(actual)) or \
                abs(float(actual) - float(expected)) > GEOMETRY_ATOL:
            mismatches.append({"block_index": expected_index, "field": field,
                               "expected": _round(expected), "actual": actual})

    compare("position_s", clip.position_s, item.get("position_s"))
    compare("length_s", clip.length_s, item.get("length_s"))
    compare("start_offset_s", clip.source_start_s, item.get("start_offset_s"))
    take = item.get("active_take") or {}
    playrate = take.get("playrate", item.get("playrate"))
    compare("playrate", clip.rate, playrate)
    compare("fade_in_s", clip.fade_in_s, item.get("fade_in_s"))
    compare("fade_out_s", clip.fade_out_s, item.get("fade_out_s"))
    return mismatches


def _track_items(snapshot: Mapping[str, Any], track_guid: str) -> list[Mapping[str, Any]]:
    for track in snapshot.get("tracks") or ():
        if isinstance(track, Mapping) and track.get("guid") == track_guid:
            return [row for row in track.get("items") or () if isinstance(row, Mapping)]
    return []


def verify_applied_geometry(snapshot: Mapping[str, Any], *,
                            track_guid: str, clips: Sequence[TimingClip],
                            created_guids: Sequence[str],
                            source_item_guid: str,
                            source_removed: bool,
                            source_path: str,
                            before_item_guids: Sequence[str] = ()) -> dict[str, Any]:
    """Check every created clip and refuse to call duplicates a success."""
    items = _track_items(snapshot, track_guid)
    by_guid = {str(item.get("guid") or ""): item for item in items}
    mismatches: list[dict[str, Any]] = []
    if len(created_guids) != len(clips):
        mismatches.append({"field": "created_item_guids", "expected": len(clips),
                           "actual": len(created_guids)})
    for index, (clip, guid) in enumerate(zip(clips, created_guids)):
        item = by_guid.get(guid)
        if item is None:
            mismatches.append({"block_index": index, "field": "item",
                               "expected": guid, "actual": None})
            continue
        mismatches.extend(_item_matches(item, clip, expected_index=index))
        take_path = _take_source_path(item)
        if take_path and take_path != source_path:
            mismatches.append({"block_index": index, "field": "source_path",
                               "expected": source_path, "actual": take_path})
    duplicates: list[dict[str, Any]] = []
    for index, clip in enumerate(clips):
        matching = [str(item.get("guid") or "") for item in items
                    if isinstance(item.get("position_s"), (int, float))
                    and not isinstance(item.get("position_s"), bool)
                    and abs(float(item["position_s"]) - clip.position_s) <= GEOMETRY_ATOL
                    and isinstance(item.get("start_offset_s"), (int, float))
                    and abs(float(item["start_offset_s"]) - clip.source_start_s) <= GEOMETRY_ATOL]
        if len(matching) > 1:
            duplicates.append({"block_index": index, "guids": matching,
                               "position_s": _round(clip.position_s),
                               "source_start_s": _round(clip.source_start_s)})
    expected_guids = set(created_guids) | set(before_item_guids)
    extra = sorted(set(by_guid) - expected_guids)
    removed = source_item_guid not in by_guid
    if removed != source_removed:
        mismatches.append({"field": "source_item",
                           "expected": "removed" if source_removed else "present",
                           "actual": "removed" if removed else "present"})
    return {
        "matched": not mismatches and not duplicates,
        "checked_blocks": len(clips),
        "mismatches": mismatches[:16],
        "mismatch_count": len(mismatches),
        "duplicate_clips": duplicates[:8],
        "duplicate_count": len(duplicates),
        "extra_item_guids": extra[:16],
        "source_item_removed": removed,
        "item_count": len(items),
    }


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _resolve_plan(plan: Mapping[str, Any] | str) -> dict[str, Any]:
    if isinstance(plan, str):
        from .timing_service import PLANS

        stored = PLANS.get(plan)
        if stored is None:
            raise ValidationError("Unknown timing plan_id",
                                  details={"code": "timing_plan_unknown",
                                           "plan_id": plan})
        return dict(stored)
    if not isinstance(plan, Mapping):
        raise ValidationError(
            "apply_timing_plan needs a timing plan object or a plan_id",
            details={"code": "timing_plan_missing"})
    return dict(plan)


def _revision_matches(expected: Any, current: ProjectRevision) -> bool:
    if isinstance(expected, ProjectRevision):
        return expected == current
    return ProjectRevision.from_dict(dict(expected)) == current


def apply_timing_plan(
    session: Any,
    plan: Mapping[str, Any] | str,
    *,
    project_id: str | None = None,
    track_guid: str | None = None,
    item_guid: str | None = None,
    protected_ranges: Sequence[Mapping[str, Any]] = (),
    expected_revision: Mapping[str, Any] | ProjectRevision | None = None,
    reason: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Replace one 1x item with a timing plan's clips in a single transaction.

    Nothing is sent to REAPER until the item, the plan's coverage of it, the
    source handles and every protected range have been checked.  With
    ``dry_run`` the transaction is previewed only.
    """
    if not isinstance(dry_run, bool):
        raise ValidationError("dry_run must be a boolean")
    plan_doc = _resolve_plan(plan)
    plan_id = str(plan_doc.get("plan_id") or "")
    target_item = item_guid or plan_doc.get("item_guid")
    if not isinstance(target_item, str) or not target_item:
        raise ValidationError("The timing plan does not name a source item",
                              details={"code": "source_item_missing",
                                       "timing_plan_id": plan_id})
    target_track = track_guid or plan_doc.get("track_guid")
    if not isinstance(target_track, str) or not target_track:
        raise ValidationError("The timing plan does not name a source track",
                              details={"code": "track_guid_missing",
                                       "timing_plan_id": plan_id})
    blocks = plan_doc.get("blocks")
    if not isinstance(blocks, Sequence) or isinstance(blocks, (str, bytes)):
        raise ValidationError("The timing plan has no block list",
                              details={"code": "timing_blocks_missing",
                                       "timing_plan_id": plan_id})
    project_id = project_id if project_id is not None else plan_doc.get("project_id")

    before = read_item_geometry(session, project_id=project_id,
                                item_guid=target_item, track_guid=target_track)
    revision: ProjectRevision = before["revision"]
    item = dict(before["item"])
    if expected_revision is not None and not _revision_matches(expected_revision, revision):
        raise StaleContext(
            "The project changed before the timing plan could be applied",
            details={"code": "timing_plan_stale",
                     "expected": (expected_revision.to_dict()
                                  if isinstance(expected_revision, ProjectRevision)
                                  else dict(expected_revision)),
                     "current": revision.to_dict(),
                     "next_action": "re-plan against the current revision"})

    crossfade = plan_doc.get("method") == "crossfade"
    if crossfade:
        from .timing_service import _file_fingerprint
        from pathlib import Path
        if item["take_count"] != 1 or item["take_fx_count"]:
            raise ValidationError("Crossfade replacement requires a single take without take FX")
        if (plan_doc.get("project_id") != revision.project_id or
                plan_doc.get("take_guid") != item["take_guid"] or
                Path(plan_doc["source_path"]) != Path(item["source_path"]) or
                any(abs(float(plan_doc[key])-item[field]) > COVERAGE_ATOL for key, field in (
                    ("item_position_s", "position_s"), ("source_offset_s", "start_offset_s"),
                    ("item_length_s", "length_s"))) or
                plan_doc.get("source_fingerprint") != list(_file_fingerprint(Path(item["source_path"])))):
            raise StaleContext("Source geometry or audio changed since timing analysis")
        if plan_doc.get("tempo_context") != before["snapshot"].get("tempo"):
            raise StaleContext("Project tempo changed since timing planning")
        protected_ranges = [*plan_doc.get("protected_ranges", ()), *protected_ranges]
    project_start_s = item["position_s"] - item["start_offset_s"]
    clips = normalize_clips(blocks, project_start_s=project_start_s)
    coverage = check_coverage(clips, item, crossfade=crossfade)
    handles = check_source_handles(clips, item)
    protected = check_protected_ranges(clips, item, protected_ranges,
                                       project_start_s=project_start_s)
    before_items = _track_items(before["snapshot"], target_track)
    operation = build_timing_apply_operation(
        item, clips, source_length_s=float(item.get("source_length_s") or 0.0) or None)

    timing_plan_id = plan_id
    batch_reason = (reason or f"timing apply {timing_plan_id or 'plan'}").strip()
    if len(batch_reason) > 300:
        batch_reason = batch_reason[:299]

    plan_handle = session.plan([operation], reason=batch_reason,
                               expected_revision=revision)
    plan_revision = getattr(plan_handle, "revision", None)
    if isinstance(plan_revision, ProjectRevision) and plan_revision != revision:
        raise StaleContext(
            "The preview was built against a different revision than the geometry checked",
            details={"code": "timing_plan_stale",
                     "checked": revision.to_dict(),
                     "previewed": plan_revision.to_dict(),
                     "next_action": "re-plan against the current revision"})
    applied = session.apply(plan_handle, dry_run=dry_run)
    revision_after = applied.get("revision_after") or applied.get("revision")
    base: dict[str, Any] = {
        "schema": SCHEMA,
        "timing_plan_id": timing_plan_id or None,
        "apply_id": operation.values["apply_id"],
        "project_id": revision.project_id,
        "track_guid": target_track,
        "item_guid": target_item,
        "block_count": len(clips),
        "operation": operation.to_dict(),
        "geometry": [clip.to_dict() for clip in clips],
        "source_item": item,
        "before_item_count": len(before_items),
        "before_item_guids": sorted(str(row.get("guid") or "") for row in before_items),
        "coverage": coverage,
        "source_handles": handles,
        "protected_ranges": protected,
        "revision_before": revision.to_dict(),
    }
    if dry_run or applied.get("applied") is not True:
        return {
            "ok": True,
            **base,
            "status": "dry_run" if dry_run else "not_applied",
            "applied": False,
            "dry_run": bool(dry_run),
            "plan_id": str(applied.get("plan_id") or ""),
            "revision_after": revision.to_dict(),
            "created_item_guids": [],
            "readback": None,
            "undo": None,
        }

    results = applied.get("operation_results")
    facts = results[0] if isinstance(results, list) and results and \
        isinstance(results[0], Mapping) else {}
    created = [str(guid) for guid in facts.get("created_item_guids") or ()]
    if len(created) != len(clips):
        created = []
    source_removed = bool(facts.get("deleted_source_item_guid"))
    after_snapshot = session.snapshot(project_id=project_id, include_items=True)
    readback = verify_applied_geometry(
        after_snapshot, track_guid=target_track, clips=clips,
        created_guids=created, source_item_guid=target_item,
        source_removed=source_removed, source_path=item["source_path"],
        before_item_guids=base["before_item_guids"])
    matched = bool(readback["matched"])
    result = {
        "ok": True,
        **base,
        "status": "applied" if matched else "applied_readback_mismatch",
        "applied": True,
        "dry_run": False,
        "plan_id": str(applied.get("plan_id") or ""),
        "revision_after": revision_after,
        "created_item_guids": created,
        "deleted_source_item_guid": facts.get("deleted_source_item_guid"),
        "replaced_leftovers": int(facts.get("replaced_leftovers") or 0),
        "readback": readback,
        "undo": {
            "contract": "reaper_edit.transaction_undo",
            "plan_id": str(applied.get("plan_id") or ""),
            "expected_revision": revision_after,
            "restores": "the original item and removes every clip this plan created",
        },
    }
    if matched and result["plan_id"]:
        APPLIED[str(result["plan_id"])] = result
    return result


def undo_timing_apply(
    session: Any,
    applied: Mapping[str, Any] | str,
    *,
    reason: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Undo one applied timing plan through its revision-bound receipt.

    The bridge undo is revision-bound and plan-bound; this wrapper then reads
    the project back and proves that the original item is byte-for-byte the
    same geometry and that none of the plan's clips remain.  A plan id is
    resolved from the in-process apply cache when the caller cannot pass the
    full apply result.
    """
    if isinstance(applied, str):
        cached = APPLIED.get(applied)
        if cached is None:
            raise ValidationError("Unknown applied timing plan_id",
                                  details={"code": "timing_undo_unavailable",
                                           "plan_id": applied})
        applied = cached
    if not isinstance(applied, Mapping):
        raise ValidationError("undo_timing_apply needs the result of an apply")
    if applied.get("applied") is not True or not applied.get("plan_id"):
        raise ValidationError(
            "This timing apply has no transaction to undo",
            details={"code": "timing_undo_unavailable",
                     "status": applied.get("status"),
                     "next_action": "undo targets an applied plan, not a dry run"})
    revision_after = applied.get("revision_after")
    if not isinstance(revision_after, Mapping):
        raise ValidationError("The applied result carries no post-apply revision",
                              details={"code": "timing_undo_unavailable"})
    source_item = applied.get("source_item")
    track_guid = applied.get("track_guid")
    created = [str(guid) for guid in applied.get("created_item_guids") or ()]
    if not isinstance(source_item, Mapping) or not isinstance(track_guid, str):
        raise ValidationError("The applied result carries no source geometry",
                              details={"code": "timing_undo_unavailable"})
    revision = ProjectRevision.from_dict(dict(revision_after))
    undo_reason = reason or f"undo timing apply {applied.get('plan_id')}"
    if len(undo_reason) > 300:
        undo_reason = undo_reason[:299]
    undone = session.undo(expected_revision=revision, plan_id=str(applied["plan_id"]))

    snapshot = session.snapshot(project_id=project_id, include_items=True)
    items = _track_items(snapshot, track_guid)
    by_guid = {str(row.get("guid") or ""): row for row in items}
    restored = by_guid.get(str(source_item.get("item_guid") or ""))
    mismatches: list[dict[str, Any]] = []
    if restored is None:
        mismatches.append({"field": "source_item", "expected": "present",
                           "actual": None})
    else:
        for field, key in (("position_s", "position_s"),
                           ("length_s", "length_s"),
                           ("start_offset_s", "start_offset_s")):
            expected = float(source_item.get(key) or 0.0)
            actual = restored.get(field)
            if isinstance(actual, bool) or not isinstance(actual, (int, float)) or \
                    abs(float(actual) - expected) > GEOMETRY_ATOL:
                mismatches.append({"field": field, "expected": _round(expected),
                                   "actual": actual})
        take = restored.get("active_take") or {}
        if abs(float(take.get("playrate") or 1.0) - 1.0) > GEOMETRY_ATOL:
            mismatches.append({"field": "playrate", "expected": 1.0,
                               "actual": take.get("playrate")})
        for field in ("fade_in_s", "fade_out_s"):
            expected = float(source_item.get(field) or 0.0)
            actual = restored.get(field)
            if isinstance(actual, bool) or not isinstance(actual, (int, float)) or \
                    abs(float(actual) - expected) > GEOMETRY_ATOL:
                mismatches.append({"field": field, "expected": _round(expected),
                                   "actual": actual})
    leftover = sorted(guid for guid in created if guid in by_guid)
    if leftover:
        mismatches.append({"field": "created_items", "expected": "removed",
                           "actual": leftover[:16]})
    expected_count = applied.get("before_item_count")
    if isinstance(expected_count, int) and expected_count != len(items):
        mismatches.append({"field": "item_count", "expected": expected_count,
                           "actual": len(items)})
    readback = {
        "matched": not mismatches,
        "mismatches": mismatches[:16],
        "source_item_restored": restored is not None,
        "leftover_created_items": leftover[:16],
        "item_count": len(items),
    }
    return {
        "ok": True,
        "schema": SCHEMA,
        "status": "undone" if readback["matched"] else "undone_readback_mismatch",
        "undone": True,
        "plan_id": undone.get("plan_id"),
        "revision_before": undone.get("revision_before"),
        "revision_after": undone.get("revision_after"),
        "readback": readback,
    }


__all__ = [
    "GEOMETRY_ATOL",
    "MAX_BLOCKS",
    "MAX_SEAM_OVERLAP_S",
    "APPLIED",
    "OPERATION_KIND",
    "SCHEMA",
    "TimingClip",
    "apply_timing_plan",
    "build_timing_apply_operation",
    "check_coverage",
    "check_protected_ranges",
    "check_source_handles",
    "find_source_item",
    "normalize_clips",
    "read_item_geometry",
    "undo_timing_apply",
    "verify_applied_geometry",
]
