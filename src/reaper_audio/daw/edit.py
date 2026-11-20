"""Safe edit batches: destination-addressed sends, guarded envelopes, rebase,
before/after verification and receipts.

The transaction API applies a revision-bound plan, but on its own it cannot
answer the questions a safe edit batch needs answered:

* "does this send already exist?" — ``send.connect`` resolves the destination
  first, so a repeated call writes the requested level to the existing row
  instead of adding a second route;
* "what will this envelope write do?" — the guard compares the proposed curve
  with the current one and with the caller's explicit limits, and refuses with
  the offending times;
* "did it actually apply?" — every applied batch is read back, and measurable
  operations can be measured before/after through an injected measure service
  (reported as before/after/delta/matched, no quality verdict);
* "the project moved" — a stale revision is rebuilt and retried, and exhausting
  the retries is its own error code;
* "undo just this edit" — a receipt records the affected values, so undo is the
  inverse operations, not "whatever is newest on REAPER's undo stack".

Everything is a parameter: track owners, destinations, intervals, metrics,
tolerances and guard limits.  No track name, section, threshold, envelope name
or target loudness is assumed in this module.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..errors import (BridgeUnavailable, ReaperAudioError, ReceiptNotFound,
                      ReceiptNotUndoable, SendRoutingError, StaleAfterRetry,
                      StaleContext, TrackDeleteRefused, ValidationError)
from .edit_context import (EditSnapshot, SendRow, db_to_linear, linear_to_db,
                           read_edit_context)
from .envelope import plan_envelope_set
from .receipt import (EditReceipt, MemoryReceiptStore, ReceiptStore,
                      build_inverse_operations, capture_baseline,
                      envelope_curves_match, expected_envelope_points,
                      mark_undone, new_receipt_id, receipt_block_reason)
from .session import EditOperation

#: A measure service is injected, never imported from the live REAPER path.
#: It receives exactly the requested sources, interval and metrics and returns
#: ``{source_guid: {metric: number | None}}``.
MeasureService = Callable[..., Mapping[str, Any]]

_READ_ONLY_KINDS = frozenset({"send.list", "envelope.points"})


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _request_guid(request: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = request.get(name)
        if isinstance(value, str) and value:
            return value
    raise ValidationError("The operation is missing a target GUID",
                          details={"expected_fields": list(names),
                                   "operation": dict(request)})


def _require_sends(snapshot: EditSnapshot) -> None:
    """Refuse a send decision when the routing read is incomplete."""
    if snapshot.sends_complete:
        return
    raise BridgeUnavailable(
        "REAPER did not return a complete send list, so routing cannot be edited safely",
        details={"project_id": snapshot.project_id,
                 "missing": list(snapshot.missing),
                 "next_action": ("fix the bridge read (project.digest coverage.sends) "
                                 "before changing sends")},
    )


def _volume_linear(request: Mapping[str, Any]) -> float | None:
    """Resolve ``volume`` (linear) or ``volume_db``; refuse both at once."""
    has_linear = request.get("volume") is not None
    has_db = request.get("volume_db") is not None
    if has_linear and has_db:
        raise ValidationError("Pass either volume or volume_db, not both",
                              details={"operation": dict(request)})
    if has_db:
        db = _finite(request["volume_db"])
        if db is None:
            raise ValidationError("volume_db must be a finite number",
                                  details={"volume_db": request["volume_db"]})
        return db_to_linear(db)
    if has_linear:
        volume = _finite(request["volume"])
        if volume is None or volume < 0:
            raise ValidationError("volume must be a finite non-negative linear value",
                                  details={"volume": request["volume"]})
        return volume
    return None


def _send_values(request: Mapping[str, Any], *, include_index: bool,
                 send_index: int | None = None) -> dict[str, Any]:
    volume = _volume_linear(request)
    values: dict[str, Any] = {}
    if include_index:
        if send_index is None:
            raise ValidationError("send.set needs a resolved send index")
        values["send_index"] = send_index
    if volume is not None:
        values["volume"] = volume
    if request.get("pan") is not None:
        pan = _finite(request["pan"])
        if pan is None or not -1 <= pan <= 1:
            raise ValidationError("send pan must be between -1 and 1",
                                  details={"pan": request["pan"]})
        values["pan"] = pan
    if request.get("mute") is not None:
        if not isinstance(request["mute"], bool):
            raise ValidationError("send mute must be a boolean",
                                  details={"mute": request["mute"]})
        values["mute"] = request["mute"]
    return values


def _resolve_send(snapshot: EditSnapshot, request: Mapping[str, Any], *,
                  field_prefix: str = "send") -> SendRow:
    """Resolve one send by index or unique destination; refuse ambiguity."""
    source_guid = _request_guid(request, "source_guid", "target_guid")
    snapshot.require_track(source_guid)
    _require_sends(snapshot)
    destination = request.get("destination_guid")
    send_index = request.get("send_index")
    if (destination is None) == (send_index is None):
        raise ValidationError(
            f"{field_prefix} needs exactly one of destination_guid or send_index",
            details={"source_guid": source_guid,
                     "destination_guid": destination, "send_index": send_index},
        )
    rows = snapshot.sends_from(source_guid)
    if send_index is not None:
        if isinstance(send_index, bool) or not isinstance(send_index, int) or send_index < 0:
            raise ValidationError("send_index must be a non-negative integer",
                                  details={"send_index": send_index})
        row = next((entry for entry in rows if entry.send_index == send_index), None)
        if row is None:
            raise ValidationError(
                "The source has no send at that index",
                details={"source_guid": source_guid, "send_index": send_index,
                         "available_send_indices": [entry.send_index for entry in rows],
                         "next_action": "read send.list and pass a current index "
                                        "or a destination_guid"},
            )
        return row
    if not isinstance(destination, str) or not destination:
        raise ValidationError("destination_guid must be a non-empty string",
                              details={"destination_guid": destination})
    matches = [entry for entry in rows if entry.destination_guid == destination]
    if not matches:
        raise ValidationError(
            "The source has no send to that destination",
            details={"source_guid": source_guid, "destination_guid": destination,
                     "available_sends": [entry.to_dict() for entry in rows],
                     "next_action": "call send.connect first"},
        )
    if len(matches) > 1:
        raise SendRoutingError(
            "The source has more than one send to that destination",
            details={"source_guid": source_guid, "destination_guid": destination,
                     "send_indices": [entry.send_index for entry in matches],
                     "send_levels_db": [linear_to_db(entry.volume) for entry in matches],
                     "next_action": ("pass send_index to address one row; more than "
                                     "one row matches this destination")},
        )
    return matches[0]


def send_list(session: Any, *, source_guid: str | None = None) -> dict[str, Any]:
    """List every ordinary send with its direction, level and mode.

    Rows are listed as REAPER reports them.  Directions that occur more than
    once are also grouped under ``duplicates`` with every matching index and
    the summed level, so a repeated route is visible without parsing chunks.
    """
    snapshot = read_edit_context(session)
    if source_guid is not None:
        snapshot.require_track(source_guid)
    rows = [row for row in snapshot.sends if source_guid is None
            or row.source_guid == source_guid]
    grouped: dict[tuple[str, str], list[int]] = {}
    for row in rows:
        grouped.setdefault((row.source_guid, row.destination_guid), []).append(
            row.send_index)
    duplicates = [
        {"source_guid": source, "destination_guid": destination,
         "send_indices": indices, "sum_db": None}
        for (source, destination), indices in sorted(grouped.items())
        if len(indices) > 1
    ]
    for entry in duplicates:
        levels = [row.volume for row in rows
                  if row.source_guid == entry["source_guid"]
                  and row.destination_guid == entry["destination_guid"]]
        total = sum(levels)
        entry["sum_db"] = linear_to_db(total)
    return {
        "project_id": snapshot.project_id,
        "revision": snapshot.revision.to_dict(),
        "source_guid": source_guid,
        "complete": snapshot.sends_complete,
        "count": len(rows),
        "sends": [row.to_dict() for row in rows],
        "duplicates": duplicates,
    }


def _compile_send_connect(snapshot: EditSnapshot,
                          request: Mapping[str, Any]) -> tuple[list[EditOperation], dict[str, Any]]:
    source_guid = _request_guid(request, "source_guid")
    snapshot.require_track(source_guid)
    _require_sends(snapshot)
    destination = request.get("destination_guid")
    if not isinstance(destination, str) or not destination:
        raise ValidationError("send.connect requires destination_guid",
                              details={"operation": dict(request)})
    destination_track = snapshot.require_track(destination)
    if destination_track.is_master:
        raise ValidationError(
            "The master track is not a send destination in REAPER",
            details={"source_guid": source_guid, "destination_guid": destination,
                     "next_action": "route to a project track or the master hardware output"},
        )
    if destination == source_guid:
        raise ValidationError("A track cannot send to itself",
                              details={"source_guid": source_guid})
    values = _send_values(request, include_index=False)
    matches = [row for row in snapshot.sends_from(source_guid)
               if row.destination_guid == destination]
    if len(matches) > 1:
        raise SendRoutingError(
            "Duplicate sends already exist from this source to this destination",
            details={"source_guid": source_guid, "destination_guid": destination,
                     "send_indices": [row.send_index for row in matches],
                     "next_action": ("pass send_index to address one row; a connect "
                                     "cannot choose between duplicate destinations")},
        )
    if matches:
        existing = matches[0]
        if not values:
            # Ensure-only call: the route already exists and the caller passed
            # no value, so there is nothing to write.  In particular this must
            # not write back a "suggested" level of its own.
            return [], {"action": "exists", "send": existing.to_dict()}
        operation = EditOperation("send.set", source_guid,
                                  {"send_index": existing.send_index, **values})
        return [operation], {"action": "exists", "send": existing.to_dict()}
    operation = EditOperation("send.create", source_guid,
                              {"destination_guid": destination, **values})
    return [operation], {"action": "create", "send": None}


def _compile_send_set(snapshot: EditSnapshot,
                      request: Mapping[str, Any]) -> tuple[list[EditOperation], dict[str, Any]]:
    row = _resolve_send(snapshot, request, field_prefix="send.set")
    values = _send_values(request, include_index=True, send_index=row.send_index)
    if len(values) == 1:
        raise ValidationError("send.set requires volume, volume_db, pan, or mute",
                              details={"operation": dict(request)})
    return ([EditOperation("send.set", row.source_guid, values)],
            {"action": "set", "send": row.to_dict()})


def _compile_send_delete(snapshot: EditSnapshot,
                         request: Mapping[str, Any]) -> tuple[list[EditOperation], dict[str, Any]]:
    row = _resolve_send(snapshot, request, field_prefix="send.delete")
    return ([EditOperation("send.delete", row.source_guid,
                           {"send_index": row.send_index})],
            {"action": "delete", "send": row.to_dict()})


def _compile_track_delete(snapshot: EditSnapshot,
                          request: Mapping[str, Any]) -> tuple[list[EditOperation], dict[str, Any]]:
    guid = _request_guid(request, "target_guid")
    track = snapshot.track(guid)
    if track is None:
        raise TrackDeleteRefused(
            "The track to delete does not exist in the current project",
            details={"track_guid": guid,
                     "available_tracks": [{"guid": row.guid, "name": row.name}
                                          for row in snapshot.tracks],
                     "next_action": ("read the project context again; the GUID may "
                                     "come from a closed project or an old revision")},
        )
    if track.is_master:
        raise TrackDeleteRefused(
            "The master track cannot be deleted",
            details={"track_guid": guid, "track_name": track.name,
                     "is_master": True,
                     "next_action": "target a project track GUID, not the master"},
        )
    send_count = len(snapshot.sends_from(guid))
    envelope_count = sum(1 for row in snapshot.envelopes if row.owner_guid == guid)
    if request.get("confirm") is not True:
        raise TrackDeleteRefused(
            "track.delete requires confirm=true",
            details={"track_guid": guid, "track_name": track.name,
                     "send_count": send_count, "envelope_count": envelope_count,
                     "next_action": ("re-read the track, then pass confirm=true; "
                                     "deleting a track cannot be inverted by a receipt")},
        )
    return ([EditOperation("track.delete", guid, {"confirm": True})],
            {"action": "delete", "track": track.to_dict()})


def _compile_envelope_set(snapshot: EditSnapshot,
                          request: Mapping[str, Any]) -> tuple[list[EditOperation], dict[str, Any]]:
    owner_guid = _request_guid(request, "owner_guid", "target_guid")
    if not snapshot.automation_complete:
        raise BridgeUnavailable(
            "REAPER did not return a complete envelope list, so an envelope "
            "cannot be edited safely",
            details={"project_id": snapshot.project_id,
                     "missing": list(snapshot.missing),
                     "next_action": ("fix the bridge read (project.digest "
                                     "coverage.automation) before writing automation")},
        )
    name = request.get("envelope", request.get("name"))
    if not isinstance(name, str) or not name:
        raise ValidationError("envelope.set requires the envelope name",
                              details={"operation": dict(request)})
    points = request.get("points")
    if not isinstance(points, list):
        raise ValidationError("envelope.set requires a points list",
                              details={"operation": dict(request)})
    operations, report = plan_envelope_set(
        snapshot, owner_guid, name, points,
        scaling=str(request.get("scaling") or "amplitude"),
        mode=request.get("mode"),
        active=request.get("active"),
        clear=request.get("clear", False),
        guard=request.get("guard"),
        interval=request.get("interval"),
        point_atol=request.get("point_atol", 1e-9),
    )
    return operations, report


def _compile_passthrough(request: Mapping[str, Any]) -> tuple[list[EditOperation], dict[str, Any]]:
    kind = request.get("kind")
    target = request.get("target_guid")
    values = request.get("values")
    if not isinstance(kind, str) or not kind:
        raise ValidationError("Every operation needs a kind")
    if not isinstance(target, str) or not target:
        raise ValidationError("Every operation needs a target_guid",
                              details={"kind": kind})
    if not isinstance(values, Mapping) or not values:
        raise ValidationError("Passthrough operations need a non-empty values object",
                              details={"kind": kind, "target_guid": target})
    return ([EditOperation(kind, target, dict(values))],
            {"action": "passthrough"})


def compile_operations(
    snapshot: EditSnapshot,
    requests: Sequence[Mapping[str, Any]],
) -> tuple[list[EditOperation], list[dict[str, Any]]]:
    """Turn high-level operation requests into transaction operations.

    Read-only requests (``send.list``, ``envelope.points``) are refused here:
    an edit batch must not contain calls that change nothing.
    """
    if not isinstance(requests, Sequence) or isinstance(requests, (str, bytes)):
        raise ValidationError("operations must be a list")
    if not requests:
        raise ValidationError("operations must contain at least one operation")
    if len(requests) > 256:
        raise ValidationError("operations is limited to 256 entries",
                              details={"operations": len(requests)})
    # A batch is compiled against one pre-batch routing state.  Two connects to
    # the same destination would both see "no send" and create duplicates, so
    # that pair is refused before anything is planned.
    connect_pairs: set[tuple[str, str]] = set()
    for index, request in enumerate(requests):
        if not isinstance(request, Mapping) or request.get("kind") != "send.connect":
            continue
        source = request.get("source_guid")
        destination = request.get("destination_guid")
        if isinstance(source, str) and isinstance(destination, str):
            pair = (source, destination)
            if pair in connect_pairs:
                raise SendRoutingError(
                    "A batch connects the same source and destination twice",
                    details={"request_index": index, "source_guid": source,
                             "destination_guid": destination,
                             "next_action": ("send one connect with the final volume; "
                                             "a repeated connect in one batch would "
                                             "create two rows")},
                )
            connect_pairs.add(pair)
    operations: list[EditOperation] = []
    meta: list[dict[str, Any]] = []
    for index, request in enumerate(requests):
        if not isinstance(request, Mapping):
            raise ValidationError("Every operation must be an object",
                                  details={"request_index": index})
        kind = request.get("kind")
        if kind in _READ_ONLY_KINDS:
            raise ValidationError(
                "Read-only operations cannot be part of an edit batch",
                details={"request_index": index, "kind": kind,
                         "next_action": "call send.list or envelope.points directly"},
            )
        if kind == "send.create":
            # A raw create cannot know whether the route already exists.  The
            # idempotent entry point is send.connect.
            raise ValidationError(
                "send.create is not accepted in a safe edit batch; use send.connect",
                details={"request_index": index, "kind": kind,
                         "next_action": ("send.connect resolves the destination and "
                                         "writes the requested level to an existing row")},
            )
        if kind == "send.connect":
            compiled, detail = _compile_send_connect(snapshot, request)
        elif kind == "send.set":
            compiled, detail = _compile_send_set(snapshot, request)
        elif kind == "send.delete":
            compiled, detail = _compile_send_delete(snapshot, request)
        elif kind == "track.delete":
            compiled, detail = _compile_track_delete(snapshot, request)
        elif kind == "envelope.set":
            compiled, detail = _compile_envelope_set(snapshot, request)
        else:
            compiled, detail = _compile_passthrough(request)
        for operation in compiled:
            operations.append(operation)
            meta.append({"request_index": index, "request_kind": str(kind),
                         "request": dict(request), **detail})
    send_kinds: dict[str, list[int]] = {}
    for operation in operations:
        if operation.kind.startswith("send."):
            send_kinds.setdefault(operation.target_guid, []).append(
                int(operation.kind == "send.delete"))
    for source_guid, deletes in send_kinds.items():
        if sum(deletes) > 0 and len(deletes) > 1:
            raise SendRoutingError(
                "A send.delete cannot share a batch with another send change on the same source",
                details={"source_guid": source_guid,
                         "send_operations": len(deletes),
                         "send_deletes": sum(deletes),
                         "next_action": ("delete the send in its own batch; deleting "
                                         "shifts every later send index in the same source")},
            )
    deleted_tracks = {operation.target_guid for operation in operations
                      if operation.kind == "track.delete"}
    if deleted_tracks:
        mixed = sorted({operation.target_guid for operation in operations
                        if operation.kind != "track.delete"
                        and operation.target_guid in deleted_tracks})
        if mixed:
            raise ValidationError(
                "track.delete cannot share a batch with other changes to the same track",
                details={"track_guids": mixed,
                         "next_action": "delete the track in its own batch"},
            )
    return operations, meta


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _interval(value: object) -> list[float] | None:
    if value is None or value == "all":
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValidationError("verify.interval must be [start_s, end_s] or 'all'",
                              details={"interval": value})
    start, end = _finite(value[0]), _finite(value[1])
    if start is None or end is None or start < 0 or end <= start:
        raise ValidationError(
            "verify.interval must be two finite numbers with 0 <= start < end",
            details={"interval": value},
        )
    return [start, end]


def _resolve_verify(verify: object, defaults: Mapping[str, Any] | None,
                    snapshot: EditSnapshot,
                    operations: Sequence[EditOperation]) -> dict[str, Any] | None:
    """Normalize the verify request; every default must be supplied explicitly."""
    if verify is None or verify is False:
        if verify is None and isinstance(defaults, Mapping):
            # The integration layer may supply explicit defaults; verification
            # is then on by default without this module inventing metrics.
            verify = defaults
        else:
            return None
    if verify is True:
        if not isinstance(defaults, Mapping):
            raise ValidationError(
                "verify=true needs verify_defaults with metrics and tolerance; "
                "this layer has no hidden measurement defaults",
                details={"next_action": "pass verify_defaults or an explicit verify object"},
            )
        verify = defaults
    if not isinstance(verify, Mapping):
        raise ValidationError("verify must be an object, true, or false",
                              details={"verify": verify})
    metrics = verify.get("metrics")
    if (not isinstance(metrics, (list, tuple)) or not metrics or
            any(not isinstance(item, str) or not item for item in metrics)):
        raise ValidationError("verify.metrics must be a non-empty list of names",
                              details={"metrics": metrics})
    if len(set(metrics)) != len(metrics):
        raise ValidationError("verify.metrics must not repeat a metric")
    raw_sources = verify.get("sources")
    if raw_sources is None or raw_sources == "targets":
        sources = [operation.target_guid for operation in operations]
    elif isinstance(raw_sources, (list, tuple)) and all(
            isinstance(item, str) and item for item in raw_sources):
        sources = list(raw_sources)
    else:
        raise ValidationError("verify.sources must be a list of GUIDs or 'targets'",
                              details={"sources": raw_sources})
    sources = list(dict.fromkeys(sources))
    if not sources:
        raise ValidationError("verify needs at least one source")
    for source in sources:
        snapshot.require_track(source)
    tolerance = verify.get("tolerance") or {}
    if not isinstance(tolerance, Mapping) or any(
            _finite(value) is None or float(value) < 0
            for value in tolerance.values()):
        raise ValidationError("verify.tolerance must map metric names to "
                              "non-negative numbers",
                              details={"tolerance": tolerance})
    expected = verify.get("expected") or {}
    if not isinstance(expected, Mapping) or any(
            _finite(value) is None for value in expected.values()):
        raise ValidationError("verify.expected must map metric names to numbers",
                              details={"expected": expected})
    gain_metrics = verify.get("gain_metrics") or []
    if (not isinstance(gain_metrics, (list, tuple)) or
            any(not isinstance(item, str) or not item for item in gain_metrics)):
        raise ValidationError("verify.gain_metrics must be a list of metric names",
                              details={"gain_metrics": gain_metrics})
    return {
        "sources": sources,
        "interval": _interval(verify.get("interval")),
        "metrics": [str(item) for item in metrics],
        "tolerance": {str(key): float(value) for key, value in tolerance.items()},
        "expected": {str(key): float(value) for key, value in expected.items()},
        "gain_metrics": [str(item) for item in gain_metrics],
    }


def _call_measure(measure: MeasureService, request: Mapping[str, Any]) -> dict[str, Any]:
    result = measure(sources=list(request["sources"]),
                     interval=request["interval"],
                     metrics=list(request["metrics"]))
    if not isinstance(result, Mapping):
        raise ValidationError("measure service must return an object",
                              details={"result_type": type(result).__name__})
    sources = list(request["sources"])
    if len(sources) == 1 and all(key in request["metrics"] for key in result):
        result = {sources[0]: dict(result)}
    rows: dict[str, dict[str, Any]] = {}
    for source in sources:
        row = result.get(source)
        if not isinstance(row, Mapping):
            raise ValidationError("measure service did not return every requested source",
                                  details={"missing_source": source,
                                           "returned_sources": sorted(result)})
        values: dict[str, Any] = {}
        for metric in request["metrics"]:
            value = row.get(metric)
            if value is not None and _finite(value) is None:
                raise ValidationError("measure service returned a non-numeric metric",
                                      details={"source": source, "metric": metric,
                                               "value": value})
            values[metric] = None if value is None else float(value)
        rows[source] = values
    return rows


def _expected_gain(snapshot: EditSnapshot, operations: Sequence[EditOperation],
                   request: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    """Derive expected dB deltas from explicit gain operations.

    Only ``track.set`` volume is used: it changes the measured track level by
    exactly the requested ratio.  A send level changes a bus, not the source,
    so it is never guessed.  Metrics that should follow a gain are named by
    the caller in ``verify.gain_metrics``.
    """
    if request["expected"]:
        return {source: dict(request["expected"]) for source in request["sources"]}
    gain_metrics = set(request["gain_metrics"])
    if not gain_metrics:
        return {}
    expected: dict[str, dict[str, float]] = {}
    for operation in operations:
        if operation.kind != "track.set" or "volume" not in operation.values:
            continue
        if operation.target_guid not in request["sources"]:
            continue
        track = snapshot.track(operation.target_guid)
        new_volume = _finite(operation.values["volume"])
        if track is None or new_volume is None:
            continue
        gain_db = linear_to_db(new_volume)
        old_db = linear_to_db(track.volume)
        if gain_db is None or old_db is None:
            continue
        expected[operation.target_guid] = {
            metric: gain_db - old_db for metric in gain_metrics
        }
    return expected


def _compare_measurements(before: Mapping[str, Mapping[str, Any]],
                          after: Mapping[str, Mapping[str, Any]],
                          request: Mapping[str, Any],
                          expected: Mapping[str, Mapping[str, float]]) -> dict[str, Any]:
    """Return measured facts: before, after, delta and an explicit match.

    ``matched`` is ``True``/``False`` only for a metric that has both an
    expected delta and a tolerance; otherwise it is ``None``.  It says whether
    the measurement agrees with the requested change, never whether the sound
    is good or bad.
    """
    delta: dict[str, dict[str, Any]] = {}
    comparable = 0
    failed = False
    undecided = False
    for source in request["sources"]:
        source_delta: dict[str, Any] = {}
        for metric in request["metrics"]:
            before_value = before[source][metric]
            after_value = after[source][metric]
            change = (None if before_value is None or after_value is None
                      else after_value - before_value)
            source_delta[metric] = change
            expected_value = expected.get(source, {}).get(metric)
            tolerance = request["tolerance"].get(metric)
            if expected_value is None or tolerance is None or change is None:
                undecided = True
                continue
            comparable += 1
            if abs(change - expected_value) > tolerance + 1e-12:
                failed = True
        delta[source] = source_delta
    matched: bool | None
    if failed:
        matched = False
    elif comparable and not undecided:
        matched = True
    else:
        matched = None
    return {
        "sources": list(request["sources"]),
        "interval": request["interval"],
        "metrics": list(request["metrics"]),
        "tolerance": dict(request["tolerance"]),
        "before": {source: dict(before[source]) for source in request["sources"]},
        "after": {source: dict(after[source]) for source in request["sources"]},
        "delta": delta,
        "matched": matched,
    }


def _after_fact(result: Mapping[str, Any], index: int) -> Mapping[str, Any]:
    rows = result.get("after")
    if isinstance(rows, list) and index < len(rows) and isinstance(rows[index], Mapping):
        values = rows[index].get("values")
        if isinstance(values, Mapping):
            return values
    return {}


def _result_row(result: Mapping[str, Any], index: int) -> Mapping[str, Any]:
    rows = result.get("operation_results")
    if isinstance(rows, list) and index < len(rows) and isinstance(rows[index], Mapping):
        return rows[index]
    return {}


def _matches(expected: object, actual: object, *, atol: float, rtol: float) -> bool:
    left, right = _finite(expected), _finite(actual)
    if left is not None and right is not None:
        return abs(left - right) <= max(atol, rtol * max(abs(left), abs(right)))
    return expected == actual


def _envelope_readback(before: EditSnapshot, after: EditSnapshot,
                       operation: EditOperation, *, envelope_atol: float) -> dict[str, Any]:
    name = str(operation.values.get("envelope") or "")
    baseline = before.envelope(operation.target_guid, name)
    current = after.envelope(operation.target_guid, name)
    if current is None:
        return {"matched": False, "reason": "envelope_missing",
                "mismatches": [{"field": "envelope", "expected": name,
                                "actual": None}]}
    if baseline is None:
        return {"matched": False, "reason": "baseline_missing", "mismatches": []}
    expected_points = expected_envelope_points(baseline.baseline_values()["points"],
                                               operation.values)
    mismatches: list[dict[str, Any]] = []
    if operation.values.get("clear") and len(current.points) != len(expected_points):
        mismatches.append({"field": "point_count", "expected": len(expected_points),
                           "actual": len(current.points)})
    if not envelope_curves_match(current.points, expected_points,
                                 atol=envelope_atol):
        mismatches.append({"field": "curve",
                           "expected_point_count": len(expected_points),
                           "actual_point_count": len(current.points)})
    return {"matched": not mismatches, "point_count": len(current.points),
            "mismatches": mismatches}


def _readback(before: EditSnapshot, after: EditSnapshot,
              operations: Sequence[EditOperation], meta: Sequence[Mapping[str, Any]],
              result: Mapping[str, Any], *, atol: float, rtol: float,
              envelope_atol: float) -> dict[str, Any]:
    checked: list[dict[str, Any]] = []
    for index, operation in enumerate(operations):
        kind = operation.kind
        entry: dict[str, Any] = {"operation_index": index, "kind": kind,
                                 "request_kind": meta[index].get("request_kind")}
        mismatches: list[dict[str, Any]] = []
        if kind == "track.set":
            track = after.track(operation.target_guid)
            if track is None:
                entry.update({"matched": False,
                              "mismatches": [{"field": "track", "expected": "exists",
                                              "actual": None}]})
                checked.append(entry)
                continue
            values = track.baseline_values()
            for key, expected in operation.values.items():
                if key == "automation_mode":
                    facts = _after_fact(result, index)
                    actual = facts.get("automation_mode_after",
                                       facts.get("automation_mode"))
                else:
                    actual = values.get(key)
                if actual is None:
                    continue
                if not _matches(expected, actual, atol=atol, rtol=rtol):
                    mismatches.append({"field": key, "expected": expected,
                                       "actual": actual})
        elif kind == "send.set":
            send_index = operation.values.get("send_index")
            row = next((item for item in after.sends_from(operation.target_guid)
                        if item.send_index == send_index), None)
            if row is None:
                mismatches.append({"field": "send", "expected": send_index,
                                   "actual": None})
            else:
                for key in ("volume", "pan", "mute"):
                    if key not in operation.values:
                        continue
                    expected = operation.values[key]
                    actual = getattr(row, key)
                    if not _matches(expected, actual, atol=atol, rtol=rtol):
                        mismatches.append({"field": key, "expected": expected,
                                           "actual": actual})
        elif kind == "send.create":
            created = _result_row(result, index).get("send_index")
            row = next((item for item in after.sends_from(operation.target_guid)
                        if item.send_index == created), None)
            if row is None:
                mismatches.append({"field": "send", "expected": created,
                                   "actual": None})
            else:
                if row.destination_guid != operation.values.get("destination_guid"):
                    mismatches.append({"field": "destination_guid",
                                       "expected": operation.values.get("destination_guid"),
                                       "actual": row.destination_guid})
                for key in ("volume", "pan", "mute"):
                    if key not in operation.values:
                        continue
                    if not _matches(operation.values[key], getattr(row, key),
                                    atol=atol, rtol=rtol):
                        mismatches.append({"field": key,
                                           "expected": operation.values[key],
                                           "actual": getattr(row, key)})
        elif kind == "send.delete":
            facts = _after_fact(result, index)
            expected_index = operation.values.get("send_index")
            if facts.get("exists") is not False:
                mismatches.append({"field": "exists", "expected": False,
                                   "actual": facts.get("exists")})
            if any(row.send_index == expected_index
                   for row in after.sends_from(operation.target_guid)):
                mismatches.append({"field": "send_index",
                                   "expected": "removed", "actual": expected_index})
        elif kind == "track.delete":
            if after.track(operation.target_guid) is not None:
                mismatches.append({"field": "exists", "expected": False,
                                   "actual": True})
        elif kind == "envelope.set":
            envelope = _envelope_readback(before, after, operation,
                                          envelope_atol=envelope_atol)
            entry.update({"matched": envelope["matched"],
                          "mismatches": envelope["mismatches"]})
            if "point_count" in envelope:
                entry["point_count"] = envelope["point_count"]
            checked.append(entry)
            continue
        else:
            entry.update({"matched": None, "reason": "readback_not_implemented"})
            checked.append(entry)
            continue
        entry["matched"] = not mismatches
        entry["mismatches"] = mismatches
        checked.append(entry)
    decided = [entry["matched"] for entry in checked if entry["matched"] is not None]
    return {
        "mode": "readback",
        "matched": all(decided) if decided else None,
        "checked_operations": len(decided),
        "operations": checked,
    }


def _error_payload(exc: Exception) -> dict[str, Any]:
    serializer = getattr(exc, "as_dict", None)
    if callable(serializer):
        payload = serializer()
        if isinstance(payload, Mapping):
            return dict(payload)
    return {"ok": False, "error": type(exc).__name__, "message": str(exc),
            "details": {}}


# ---------------------------------------------------------------------------
# Batch execution with rebase
# ---------------------------------------------------------------------------


def _plan_and_apply(
    session: Any,
    build: Callable[[EditSnapshot], tuple[list[EditOperation], list[Any]]],
    *,
    reason: str,
    dry_run: bool,
    max_rebase: int,
    initial_snapshot: EditSnapshot | None = None,
) -> tuple[EditSnapshot, list[EditOperation], list[Any], Any, dict[str, Any],
           int, list[dict[str, Any]]]:
    """Plan and apply one batch, rebuilding it after each stale revision."""
    if isinstance(max_rebase, bool) or not isinstance(max_rebase, int) or max_rebase < 0:
        raise ValidationError("max_rebase must be a non-negative integer",
                              details={"max_rebase": max_rebase})
    attempts: list[dict[str, Any]] = []
    rebased = 0
    snapshot = initial_snapshot if initial_snapshot is not None else read_edit_context(session)
    operations, meta = build(snapshot)
    for attempt in range(max_rebase + 1):
        try:
            plan = session.plan(operations, reason=reason)
            result = session.apply(plan, dry_run=dry_run)
            attempts.append({"attempt": attempt, "rebase": rebased,
                             "revision": snapshot.revision.to_dict()})
            return snapshot, operations, meta, plan, result, rebased, attempts
        except StaleContext as exc:
            attempts.append({"attempt": attempt, "rebase": rebased,
                             "revision": snapshot.revision.to_dict(),
                             "stale": str(exc), "details": dict(exc.details)})
            if attempt >= max_rebase:
                raise StaleAfterRetry(
                    "The project kept changing; the edit was not applied",
                    details={"attempts": attempts, "max_rebase": max_rebase,
                             "rebased": rebased,
                             "next_action": ("read the current revision and retry, "
                                             "or pass a larger max_rebase explicitly")},
                ) from exc
            rebased += 1
            snapshot = read_edit_context(session)
            operations, meta = build(snapshot)
    raise AssertionError("unreachable")


def _enrich_baseline_modes(baseline: dict[str, Any],
                           operations: Sequence[EditOperation],
                           before_rows: object) -> None:
    """Copy automation modes from the preview facts into the receipt baseline.

    ``project.context`` does not expose ``I_AUTOMODE`` on every bridge build,
    while ``transaction.preview`` reads it for a ``track.set`` request.  When
    the fact exists it is authoritative for undo; when it does not, the
    receipt keeps whatever the context reported (possibly nothing) rather than
    inventing a mode.
    """
    if not isinstance(before_rows, list):
        return
    tracks = baseline.get("tracks")
    if not isinstance(tracks, dict):
        return
    for index, operation in enumerate(operations):
        if operation.kind != "track.set" or "automation_mode" not in operation.values:
            continue
        row = before_rows[index] if index < len(before_rows) else None
        values = row.get("values") if isinstance(row, Mapping) else None
        mode = values.get("automation_mode") if isinstance(values, Mapping) else None
        if isinstance(mode, int) and not isinstance(mode, bool):
            tracks.setdefault(operation.target_guid, {})["automation_mode"] = mode
    envelopes = baseline.get("envelopes")
    if isinstance(envelopes, list):
        for entry in envelopes:
            if not isinstance(entry, dict) or "automation_mode" in entry:
                continue
            track = tracks.get(str(entry.get("owner_guid")))
            if isinstance(track, Mapping) and isinstance(track.get("automation_mode"), int):
                entry["automation_mode"] = track["automation_mode"]


def _post_apply_readback(
    session: Any, snapshot: EditSnapshot, operations: Sequence[EditOperation],
    meta: Sequence[Mapping[str, Any]], result: Mapping[str, Any], *,
    atol: float, rtol: float, envelope_atol: float,
) -> dict[str, Any]:
    """Read back an applied batch; a read failure never hides the apply.

    The transaction has already committed.  If the project moves before the
    read-back, reporting the edit as failed would be false; the report says the
    read-back could not be completed and carries the error.
    """
    try:
        snapshot_after = read_edit_context(session)
        return _readback(snapshot, snapshot_after, operations, meta, result,
                         atol=atol, rtol=rtol, envelope_atol=envelope_atol)
    except ReaperAudioError as exc:
        return {"mode": "readback", "matched": None,
                "reason": "readback_failed", "error": _error_payload(exc)}


def _post_apply_measure(
    measure: MeasureService, request: Mapping[str, Any],
    before: Mapping[str, Mapping[str, Any]], snapshot: EditSnapshot,
    operations: Sequence[EditOperation],
) -> dict[str, Any]:
    """Measure after an apply; an unavailable measure does not undo the edit."""
    try:
        after = _call_measure(measure, request)
    except Exception as exc:  # noqa: BLE001 - the committed edit must still return
        return {
            "sources": request["sources"], "interval": request["interval"],
            "metrics": request["metrics"], "tolerance": request["tolerance"],
            "before": {source: dict(before[source]) for source in request["sources"]},
            "after": None, "delta": None, "matched": None,
            "error": _error_payload(exc),
        }
    expected = _expected_gain(snapshot, operations, request)
    return _compare_measurements(before, after, request, expected)


def _receipt_from_result(
    *, snapshot: EditSnapshot, operations: Sequence[EditOperation],
    reason: str, plan: Any, result: Mapping[str, Any], status: str,
    rebased: int, verify: Mapping[str, Any] | None,
    readback: Mapping[str, Any] | None, undoes: str | None = None,
    error: Mapping[str, Any] | None = None,
) -> EditReceipt:
    operation_dicts = tuple(operation.to_dict() for operation in operations)
    block_reason = receipt_block_reason(operation_dicts)
    after_rows = result.get("after")
    apply_results = result.get("operation_results")
    baseline = capture_baseline(snapshot, operations)
    _enrich_baseline_modes(baseline, operations, result.get("before"))
    return EditReceipt(
        receipt_id=new_receipt_id(),
        project_id=snapshot.project_id,
        plan_id=str(getattr(plan, "plan_id", "") or "no_plan"),
        reason=reason,
        status=status,
        operations=operation_dicts,
        revision_before=snapshot.revision.to_dict(),
        revision_after=(dict(result["revision_after"])
                        if isinstance(result.get("revision_after"), Mapping)
                        else dict(result["revision"])
                        if isinstance(result.get("revision"), Mapping) else None),
        before=tuple(dict(row) for row in (result.get("before") or ())
                     if isinstance(row, Mapping)),
        after=tuple(dict(row) for row in (after_rows or ())
                    if isinstance(row, Mapping)),
        apply_results=tuple(apply_results or ()),
        baseline=baseline,
        rebased=rebased,
        verify=dict(verify) if isinstance(verify, Mapping) else None,
        readback=dict(readback) if isinstance(readback, Mapping) else None,
        undo_available=(status == "applied" and block_reason is None),
        undo_block_reason=block_reason,
        undoes=undoes,
        error=dict(error) if isinstance(error, Mapping) else None,
    )


def _record_failure(
    session: Any,
    requests: Sequence[Mapping[str, Any]],
    *,
    reason: str,
    exc: Exception,
    store: ReceiptStore,
    fallback_snapshot: EditSnapshot,
    fallback_operations: Sequence[EditOperation],
) -> EditReceipt:
    """Persist a failed attempt so a failure is visible next to applied edits."""
    snapshot = fallback_snapshot
    compiled: list[EditOperation] = list(fallback_operations)
    try:
        snapshot = read_edit_context(session)
        compiled = compile_operations(snapshot, requests)[0]
    except Exception:  # noqa: BLE001 - the recorded failure matters more
        pass
    plan_id = "no_plan"
    details = getattr(exc, "details", None)
    if isinstance(details, Mapping) and isinstance(details.get("plan_id"), str):
        plan_id = details["plan_id"]
    receipt = EditReceipt(
        receipt_id=new_receipt_id(),
        project_id=snapshot.project_id,
        plan_id=plan_id,
        reason=reason,
        status="failed",
        operations=tuple(operation.to_dict() for operation in compiled),
        revision_before=snapshot.revision.to_dict(),
        revision_after=None,
        baseline=capture_baseline(snapshot, compiled),
        undo_available=False,
        undo_block_reason="the edit did not apply",
        error=_error_payload(exc),
    )
    store.put(receipt)
    if isinstance(details, dict):
        details.setdefault("receipt", receipt.receipt_id)
    return receipt


def run_edit(
    session: Any,
    operations: Sequence[Mapping[str, Any]],
    *,
    reason: str,
    verify: object = None,
    verify_defaults: Mapping[str, Any] | None = None,
    measure: MeasureService | None = None,
    dry_run: bool = False,
    max_rebase: int = 2,
    receipt_store: ReceiptStore | None = None,
    readback_atol: float = 1e-9,
    readback_rtol: float = 1e-6,
    envelope_atol: float = 2e-3,
) -> dict[str, Any]:
    """Apply one edit batch with rebase, read-back, optional measurement, receipt.

    ``verify`` is an explicit object: ``sources`` (GUIDs or ``"targets"``),
    ``interval`` (``[start_s, end_s]`` or ``"all"``), ``metrics``,
    ``tolerance`` and, optionally, ``expected`` deltas or ``gain_metrics`` to
    derive them from a ``track.set`` volume change.  When ``verify`` is omitted
    but ``verify_defaults`` is supplied, those defaults are used, so the
    integration layer can verify by default without this module inventing any
    metric or threshold.  ``matched`` is a boolean only when every requested
    metric has an expected delta and a tolerance; a definite mismatch is
    ``False``, anything not comparable is ``None``.  Nothing is measured unless
    a ``measure`` service is supplied; parameter operations are always read
    back from the project without a render.
    """
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 300:
        raise ValidationError("reason must contain 1 to 300 characters")
    if not isinstance(dry_run, bool):
        raise ValidationError("dry_run must be a boolean")
    for name, value in (("readback_atol", readback_atol),
                        ("readback_rtol", readback_rtol),
                        ("envelope_atol", envelope_atol)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or \
                not math.isfinite(float(value)) or value < 0:
            raise ValidationError(f"{name} must be a finite non-negative number")
    store = receipt_store if receipt_store is not None else MemoryReceiptStore()

    first_snapshot = read_edit_context(session)
    first_operations, _ = compile_operations(first_snapshot, operations)
    verify_request = _resolve_verify(verify, verify_defaults, first_snapshot,
                                     first_operations)
    if verify_request is not None:
        if measure is None:
            raise ValidationError(
                "verify needs an injected measure service; this layer never renders by itself",
                details={"next_action": "pass measure=<callable> or verify=false"},
            )
        before_measure = _call_measure(measure, verify_request)
    else:
        before_measure = None
    if not first_operations:
        receipt = EditReceipt(
            receipt_id=new_receipt_id(),
            project_id=first_snapshot.project_id,
            plan_id="no_plan",
            reason=reason,
            status="no_op",
            operations=(),
            revision_before=first_snapshot.revision.to_dict(),
            revision_after=first_snapshot.revision.to_dict(),
            undo_available=False,
            undo_block_reason="no operation changes project state",
            verify=({"sources": verify_request["sources"],
                     "interval": verify_request["interval"],
                     "metrics": verify_request["metrics"],
                     "tolerance": verify_request["tolerance"],
                     "before": before_measure, "after": None,
                     "delta": None, "matched": None}
                    if verify_request is not None and before_measure is not None
                    else None),
        )
        store.put(receipt)
        return {
            "ok": True, "applied": False, "dry_run": dry_run, "no_op": True,
            "status": "no_op", "rebased": 0, "rebased_revisions": [],
            "receipt": receipt.receipt_id, "receipt_record": receipt.to_dict(),
            "project_id": first_snapshot.project_id,
            "revision_before": first_snapshot.revision.to_dict(),
            "revision_after": first_snapshot.revision.to_dict(),
            "operations": [], "readback": None, "verify": receipt.verify,
            "undo_available": False,
            "undo_block_reason": receipt.undo_block_reason,
        }

    try:
        snapshot, compiled, meta, plan, result, rebased, attempts = _plan_and_apply(
            session,
            lambda fresh: compile_operations(fresh, operations),
            reason=reason, dry_run=dry_run, max_rebase=max_rebase,
            initial_snapshot=first_snapshot,
        )
    except StaleAfterRetry as exc:
        _record_failure(session, operations, reason=reason, exc=exc,
                        store=store, fallback_snapshot=first_snapshot,
                        fallback_operations=first_operations)
        raise
    except ValidationError:
        raise
    except ReaperAudioError as exc:
        _record_failure(session, operations, reason=reason, exc=exc,
                        store=store, fallback_snapshot=first_snapshot,
                        fallback_operations=first_operations)
        raise

    no_op = bool(result.get("no_op"))
    applied = result.get("applied") is True and not dry_run
    status = "dry_run" if dry_run else ("no_op" if no_op or not applied else "applied")
    readback: dict[str, Any] | None = None
    verify_report: dict[str, Any] | None = None
    if applied and not no_op:
        readback = _post_apply_readback(
            session, snapshot, compiled, meta, result,
            atol=float(readback_atol), rtol=float(readback_rtol),
            envelope_atol=float(envelope_atol))
        if verify_request is not None and before_measure is not None:
            assert measure is not None
            verify_report = _post_apply_measure(
                measure, verify_request, before_measure, snapshot, compiled)
    elif verify_request is not None:
        verify_report = {
            "sources": verify_request["sources"],
            "interval": verify_request["interval"],
            "metrics": verify_request["metrics"],
            "tolerance": verify_request["tolerance"],
            "before": (before_measure or {}),
            "after": None, "delta": None, "matched": None,
        }

    receipt = _receipt_from_result(
        snapshot=snapshot, operations=compiled, reason=reason, plan=plan,
        result=result, status=status, rebased=rebased,
        verify=verify_report, readback=readback,
    )
    store.put(receipt)
    return {
        "ok": True,
        "applied": applied,
        "dry_run": dry_run,
        "no_op": no_op or not applied,
        "status": status,
        "rebased": rebased,
        "rebased_revisions": [entry["revision"] for entry in attempts[:-1]],
        "receipt": receipt.receipt_id,
        "receipt_record": receipt.to_dict(),
        "project_id": snapshot.project_id,
        "revision_before": snapshot.revision.to_dict(),
        "revision_after": receipt.revision_after,
        "operations": [operation.to_dict() for operation in compiled],
        "readback": readback,
        "verify": verify_report,
        "undo_available": receipt.undo_available,
        "undo_block_reason": receipt.undo_block_reason,
    }


def undo_receipt(
    session: Any,
    receipt: EditReceipt | Mapping[str, Any] | str,
    *,
    reason: str | None = None,
    receipt_store: ReceiptStore | None = None,
    allow_overwrite: bool = False,
    dry_run: bool = False,
    verify: object = None,
    verify_defaults: Mapping[str, Any] | None = None,
    measure: MeasureService | None = None,
    max_rebase: int = 2,
    readback_atol: float = 1e-9,
    readback_rtol: float = 1e-6,
    envelope_atol: float = 2e-3,
) -> dict[str, Any]:
    """Undo exactly one receipt by applying its inverse operations.

    The reverse batch is built from the receipt's own before-values, so later
    unrelated edits survive.  A target whose value changed since the receipt
    raises :class:`ReceiptConflict` unless ``allow_overwrite`` is explicit; an
    operation with no precise inverse raises :class:`ReceiptNotUndoable`.
    """
    store = receipt_store if receipt_store is not None else MemoryReceiptStore()
    if isinstance(receipt, str):
        loaded = store.get(receipt)
        if loaded is None:
            raise ReceiptNotFound(
                "No receipt with this identifier is stored",
                details={"receipt_id": receipt,
                         "next_action": ("pass the receipt object returned by the edit, "
                                         "or open the archive that stores it")},
            )
        record = loaded
    else:
        if isinstance(receipt, EditReceipt):
            record = receipt
        elif isinstance(receipt, Mapping):
            record = EditReceipt.from_dict(receipt)
        else:
            raise ValidationError("receipt must be a receipt object, JSON object, or id",
                                  details={"received_type": type(receipt).__name__})
    if record.status not in {"applied", "no_op", "dry_run"}:
        raise ReceiptNotUndoable(
            "This receipt has no applied edit to undo",
            details={"receipt_id": record.receipt_id, "status": record.status,
                     "next_action": "apply the edit first; a failed or undone "
                                    "receipt has nothing to reverse"},
        )
    if not record.undo_available:
        raise ReceiptNotUndoable(
            "This receipt has no precise inverse",
            details={"receipt_id": record.receipt_id,
                     "reason": record.undo_block_reason,
                     "next_action": ("use a checkpoint or a saved .RPP for this kind "
                                     "of operation")},
        )
    undo_reason = reason or f"undo {record.receipt_id}"
    if not isinstance(undo_reason, str) or not undo_reason.strip() or \
            len(undo_reason) > 300:
        raise ValidationError("reason must contain 1 to 300 characters")
    if not isinstance(dry_run, bool):
        raise ValidationError("dry_run must be a boolean")

    first_snapshot = read_edit_context(session)
    first_inverse, _ = build_inverse_operations(
        record, first_snapshot, allow_overwrite=allow_overwrite,
        value_atol=float(readback_atol), value_rtol=float(readback_rtol),
        envelope_atol=float(envelope_atol))
    verify_request = _resolve_verify(verify, verify_defaults, first_snapshot,
                                     first_inverse)
    if verify_request is not None:
        if measure is None:
            raise ValidationError("verify needs an injected measure service")
        before_measure = _call_measure(measure, verify_request)
    else:
        before_measure = None

    def build_inverse(fresh: EditSnapshot) -> tuple[list[EditOperation], list[Any]]:
        operations, _ = build_inverse_operations(
            record, fresh, allow_overwrite=allow_overwrite,
            value_atol=float(readback_atol), value_rtol=float(readback_rtol),
            envelope_atol=float(envelope_atol))
        return operations, [{"request_kind": "undo"} for _ in operations]

    try:
        snapshot, compiled, meta, plan, result, rebased, attempts = _plan_and_apply(
            session,
            build_inverse,
            reason=undo_reason, dry_run=dry_run, max_rebase=max_rebase,
            initial_snapshot=first_snapshot,
        )
    except StaleAfterRetry as exc:
        _record_failure(session, [], reason=undo_reason, exc=exc, store=store,
                        fallback_snapshot=first_snapshot,
                        fallback_operations=first_inverse)
        raise
    except ValidationError:
        raise
    except ReaperAudioError as exc:
        _record_failure(session, [], reason=undo_reason, exc=exc, store=store,
                        fallback_snapshot=first_snapshot,
                        fallback_operations=first_inverse)
        raise
    no_op = bool(result.get("no_op"))
    applied = result.get("applied") is True and not dry_run
    status = "dry_run" if dry_run else ("no_op" if no_op or not applied else "applied")
    readback: dict[str, Any] | None = None
    verify_report: dict[str, Any] | None = None
    if applied and not no_op:
        readback = _post_apply_readback(
            session, snapshot, compiled, meta, result,
            atol=float(readback_atol), rtol=float(readback_rtol),
            envelope_atol=float(envelope_atol))
        if verify_request is not None and before_measure is not None:
            assert measure is not None
            verify_report = _post_apply_measure(
                measure, verify_request, before_measure, snapshot, compiled)
    elif verify_request is not None:
        verify_report = {"sources": verify_request["sources"],
                         "interval": verify_request["interval"],
                         "metrics": verify_request["metrics"],
                         "tolerance": verify_request["tolerance"],
                         "before": (before_measure or {}), "after": None,
                         "delta": None, "matched": None}

    undo_record = _receipt_from_result(
        snapshot=snapshot, operations=compiled, reason=undo_reason, plan=plan,
        result=result, status=status, rebased=rebased,
        verify=verify_report, readback=readback, undoes=record.receipt_id,
    )
    store.put(undo_record)
    if status == "applied":
        store.put(mark_undone(record, undo_receipt_id=undo_record.receipt_id))
    return {
        "ok": True,
        "applied": applied,
        "dry_run": dry_run,
        "no_op": no_op or not applied,
        "status": status,
        "rebased": rebased,
        "undo_of": record.receipt_id,
        "receipt": undo_record.receipt_id,
        "receipt_record": undo_record.to_dict(),
        "revision_before": snapshot.revision.to_dict(),
        "revision_after": undo_record.revision_after,
        "operations": [operation.to_dict() for operation in compiled],
        "readback": readback,
        "verify": verify_report,
    }


__all__ = [
    "MeasureService",
    "compile_operations",
    "run_edit",
    "send_list",
    "undo_receipt",
]
