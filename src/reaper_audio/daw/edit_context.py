"""Revision-bound read model for safe edits: tracks, sends and envelopes.

The transaction API (``transaction.preview``/``apply``) validates operations
against a project revision, but it does not answer the questions an edit layer
must answer *before* it builds a plan:

* which send already reaches this destination (so a second ``send.connect``
  changes level instead of creating a duplicate);
* which points an envelope currently has (so a guard can compare the proposed
  write with what is already automated);
* which values a later receipt must restore (so undo is targeted, not "the
  last thing REAPER happens to have on its undo stack").

``project.context`` carries fader values and names; ``project.digest`` carries
the send rows and the envelope point rows.  This module joins the two into one
typed snapshot and refuses to present a half-read project as a complete one.
No threshold, track name, section or envelope name is assumed here.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import BridgeUnavailable, StaleContext, ValidationError
from .session import ProjectRevision

#: REAPER's ``I_AUTOMODE`` / ``GetTrackAutomationMode`` values.  This is the
#: host protocol enum, not a mixing decision: the caller passes the value (or
#: its name) explicitly.
AUTOMATION_MODES: dict[int, str] = {
    0: "trim/read",
    1: "read",
    2: "touch",
    3: "write",
    4: "latch",
    5: "latch preview",
}

_AUTOMATION_MODE_ALIASES = {
    "trim/read": 0,
    "trim read": 0,
    "trim": 0,
    "read": 1,
    "touch": 2,
    "write": 3,
    "latch": 4,
    "latch preview": 5,
}

#: Envelope scaling reported by REAPER's ``GetEnvelopeScalingMode``.
SCALING_MODE_NAMES = {0: "amplitude", 1: "fader"}


def _finite(value: object) -> float | None:
    """Return a finite float for a numeric bridge value, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def linear_to_db(value: float) -> float | None:
    """Convert a linear amplitude to dB, or ``None`` for non-positive values.

    Zero and negative amplitudes have no finite dB value; they are reported as
    ``None`` (rather than a large negative sentinel) so a guard can never
    mistake "silence" for "a very quiet level".
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return 20.0 * math.log10(number)


def db_to_linear(db: float) -> float:
    """Convert dB to a linear amplitude.  ``-inf`` maps to zero."""
    if isinstance(db, bool) or not isinstance(db, (int, float)):
        raise ValidationError("dB value must be a finite number",
                              details={"value": db})
    number = float(db)
    if not math.isfinite(number):
        raise ValidationError("dB value must be finite", details={"value": db})
    return 10.0 ** (number / 20.0)


def parse_automation_mode(mode: object) -> int | None:
    """Return the REAPER automation-mode integer for a name or integer.

    ``None`` means "not requested".  Unknown names are refused with the list of
    accepted protocol names instead of being guessed.
    """
    if mode is None:
        return None
    if isinstance(mode, bool):
        raise ValidationError("automation mode must be an integer or name",
                              details={"mode": mode})
    if isinstance(mode, int):
        if mode not in AUTOMATION_MODES:
            raise ValidationError(
                "automation mode must be between 0 and 5",
                details={"mode": mode, "accepted": AUTOMATION_MODES,
                         "next_action": "pass a REAPER automation mode"},
            )
        return mode
    if isinstance(mode, str):
        key = " ".join(mode.strip().lower().replace("_", " ").split())
        if key in _AUTOMATION_MODE_ALIASES:
            return _AUTOMATION_MODE_ALIASES[key]
        raise ValidationError(
            "unknown automation mode name",
            details={"mode": mode, "accepted": sorted(_AUTOMATION_MODE_ALIASES)},
        )
    raise ValidationError("automation mode must be an integer or name",
                          details={"mode": mode})


def automation_mode_name(mode: int | None) -> str | None:
    """Return the protocol name for an automation mode, when it is known."""
    if mode is None:
        return None
    return AUTOMATION_MODES.get(int(mode))


@dataclass(frozen=True, slots=True)
class TrackRow:
    """One track as seen by the edit layer (faders from context)."""

    guid: str
    name: str
    index: int | None
    volume: float
    pan: float
    mute: bool
    is_master: bool = False
    automation_mode: int | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "guid": self.guid, "name": self.name, "index": self.index,
            "volume": self.volume, "volume_db": linear_to_db(self.volume),
            "pan": self.pan, "mute": self.mute, "is_master": self.is_master,
        }
        if self.automation_mode is not None:
            value["automation_mode"] = self.automation_mode
            value["mode_name"] = automation_mode_name(self.automation_mode)
        return value

    def baseline_values(self) -> dict[str, Any]:
        """Editable values a receipt must be able to restore."""
        return {"name": self.name, "volume": self.volume, "pan": self.pan,
                "mute": self.mute,
                **({"automation_mode": self.automation_mode}
                   if self.automation_mode is not None else {})}


@dataclass(frozen=True, slots=True)
class SendRow:
    """One ordinary (post-FX, pre-fader by REAPER default) track send row."""

    source_guid: str
    source_name: str
    send_index: int
    destination_guid: str
    destination_name: str
    volume: float
    pan: float
    mute: bool
    mode: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_guid": self.source_guid,
            "source_name": self.source_name,
            "send_index": self.send_index,
            "destination_guid": self.destination_guid,
            "destination_name": self.destination_name,
            "volume": self.volume,
            "volume_db": linear_to_db(self.volume),
            "pan": self.pan,
            "mute": self.mute,
            **({"mode": self.mode} if self.mode is not None else {}),
        }

    def baseline_values(self) -> dict[str, Any]:
        return {"destination_guid": self.destination_guid,
                "volume": self.volume, "pan": self.pan, "mute": self.mute}


@dataclass(frozen=True, slots=True)
class EnvelopePoint:
    """One envelope point, always stored as a linear amplitude."""

    index: int
    time_s: float
    value: float
    shape: int = 0
    tension: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "time_s": self.time_s,
                "value": self.value, "value_db": linear_to_db(self.value),
                "shape": self.shape, "tension": self.tension}


@dataclass(frozen=True, slots=True)
class EnvelopeRow:
    """One automation envelope with every point the bridge reported."""

    owner_guid: str
    owner_name: str
    name: str
    guid: str
    scaling_mode: int
    active: bool | None
    visible: bool | None
    armed: bool | None
    points: tuple[EnvelopePoint, ...]
    fx_index: int | None = None
    parameter_index: int | None = None

    @property
    def scaling_name(self) -> str:
        return SCALING_MODE_NAMES.get(self.scaling_mode, "unknown")

    def to_dict(self, *, include_points: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "owner_guid": self.owner_guid, "owner_name": self.owner_name,
            "name": self.name, "envelope_guid": self.guid,
            "scaling_mode": self.scaling_mode,
            "native_scaling": self.scaling_name,
            "active": self.active, "visible": self.visible,
            "armed": self.armed, "point_count": len(self.points),
        }
        if self.fx_index is not None:
            value["fx_index"] = self.fx_index
            value["parameter_index"] = self.parameter_index
        if include_points:
            value["points"] = [point.to_dict() for point in self.points]
        return value

    def baseline_values(self) -> dict[str, Any]:
        return {"points": [{"time_s": point.time_s, "value": point.value,
                            "shape": point.shape, "tension": point.tension}
                           for point in self.points],
                "active": self.active,
                "scaling_mode": self.scaling_mode}


def evaluate_envelope(points: Sequence[EnvelopePoint], time_s: float) -> float | None:
    """Evaluate a linear-amplitude envelope at ``time_s`` (hold at the ends).

    Returns ``None`` for an envelope without points, which is not the same as
    "the value is zero": an empty envelope carries no automation at all.
    """
    if not points:
        return None
    ordered = sorted(points, key=lambda point: point.time_s)
    if time_s <= ordered[0].time_s:
        return ordered[0].value
    if time_s >= ordered[-1].time_s:
        return ordered[-1].value
    for left, right in zip(ordered, ordered[1:]):
        if left.time_s <= time_s <= right.time_s:
            span = right.time_s - left.time_s
            if span <= 0:
                return right.value
            ratio = (time_s - left.time_s) / span
            return left.value + (right.value - left.value) * ratio
    return ordered[-1].value


@dataclass(frozen=True, slots=True)
class EditSnapshot:
    """One consistent project read used to compile, guard and receipt an edit."""

    revision: ProjectRevision
    tracks: tuple[TrackRow, ...]
    master: TrackRow | None
    sends: tuple[SendRow, ...]
    envelopes: tuple[EnvelopeRow, ...]
    missing: tuple[str, ...] = ()
    sends_complete: bool = True
    automation_complete: bool = True

    @property
    def project_id(self) -> str:
        return self.revision.project_id

    def track(self, guid: str) -> TrackRow | None:
        if self.master is not None and self.master.guid == guid:
            return self.master
        return next((track for track in self.tracks if track.guid == guid), None)

    def require_track(self, guid: str) -> TrackRow:
        """Return one track or refuse with the available identities."""
        track = self.track(guid)
        if track is not None:
            return track
        available = [{"guid": row.guid, "name": row.name}
                     for row in ([*self.tracks, self.master]
                                 if self.master is not None else self.tracks)]
        raise ValidationError(
            "The edit target track does not exist in the current project",
            details={"track_guid": guid, "available_tracks": available,
                     "next_action": ("read the project context again and pass "
                                     "a current track GUID")},
        )

    def sends_from(self, source_guid: str) -> tuple[SendRow, ...]:
        return tuple(row for row in self.sends if row.source_guid == source_guid)

    def envelope(self, owner_guid: str, name: str) -> EnvelopeRow | None:
        return next((row for row in self.envelopes
                     if row.owner_guid == owner_guid and row.name == name), None)

    def require_envelope(self, owner_guid: str, name: str) -> EnvelopeRow:
        row = self.envelope(owner_guid, name)
        if row is not None:
            return row
        names = sorted({entry.name for entry in self.envelopes
                        if entry.owner_guid == owner_guid})
        raise ValidationError(
            "The envelope does not exist on this owner",
            details={"owner_guid": owner_guid, "envelope": name,
                     "available_envelopes": names,
                     "next_action": "pass an envelope name from the project context"},
        )

    def to_dict(self, *, include_envelope_points: bool = False) -> dict[str, Any]:
        return {
            "revision": self.revision.to_dict(),
            "tracks": [track.to_dict() for track in self.tracks],
            "master": self.master.to_dict() if self.master is not None else None,
            "sends": [send.to_dict() for send in self.sends],
            "envelopes": [row.to_dict(include_points=include_envelope_points)
                          for row in self.envelopes],
            "missing": list(self.missing),
            "sends_complete": self.sends_complete,
            "automation_complete": self.automation_complete,
        }


def _track_from_context(entry: Mapping[str, Any], *, is_master: bool) -> TrackRow:
    guid = entry.get("guid")
    if not isinstance(guid, str) or not guid:
        raise BridgeUnavailable("REAPER returned a track without a GUID")
    volume = _finite(entry.get("volume"))
    pan = _finite(entry.get("pan"))
    if volume is None or pan is None:
        raise BridgeUnavailable("REAPER returned a track with an invalid fader value",
                                details={"track_guid": guid})
    index = entry.get("index")
    mode = entry.get("automation_mode")
    return TrackRow(
        guid=guid,
        name=str(entry.get("name") or ""),
        index=index if isinstance(index, int) and not isinstance(index, bool) else None,
        volume=volume,
        pan=pan,
        mute=bool(entry.get("mute")),
        is_master=is_master,
        automation_mode=(mode if isinstance(mode, int) and not isinstance(mode, bool)
                         else None),
    )


def _send_rows(digest: Mapping[str, Any], names: Mapping[str, str]) -> tuple[SendRow, ...]:
    fields = digest.get("fields")
    if not isinstance(fields, Mapping):
        raise BridgeUnavailable("REAPER returned a project digest without fields")
    track_rows = fields.get("tracks")
    if not isinstance(track_rows, list):
        raise BridgeUnavailable("REAPER returned a project digest without track rows")
    rows: list[SendRow] = []
    for track in [*track_rows,
                  *([fields["master"]] if isinstance(fields.get("master"), Mapping)
                    else [])]:
        if not isinstance(track, Mapping):
            continue
        source_guid = track.get("guid")
        sends = track.get("sends")
        if not isinstance(source_guid, str) or not isinstance(sends, list):
            continue
        for send in sends:
            if not isinstance(send, Mapping):
                continue
            index = send.get("index")
            destination = send.get("dest_guid")
            volume = _finite(send.get("volume"))
            pan = _finite(send.get("pan"))
            if (isinstance(index, bool) or not isinstance(index, int) or index < 0 or
                    not isinstance(destination, str) or not destination or
                    volume is None or pan is None):
                raise BridgeUnavailable(
                    "REAPER returned an invalid send row",
                    details={"source_guid": source_guid, "send": dict(send)},
                )
            mode = send.get("mode")
            rows.append(SendRow(
                source_guid=source_guid,
                source_name=names.get(source_guid, ""),
                send_index=index,
                destination_guid=destination,
                destination_name=str(send.get("dest_name")
                                     or names.get(destination) or ""),
                volume=volume,
                pan=pan,
                mute=bool(send.get("mute")),
                mode=(mode if isinstance(mode, int) and not isinstance(mode, bool)
                      else None),
            ))
    rows.sort(key=lambda row: (row.source_guid, row.send_index))
    return tuple(rows)


def _envelope_rows(digest: Mapping[str, Any], names: Mapping[str, str],
                   master_guid: str | None) -> tuple[tuple[EnvelopeRow, ...], bool]:
    fields = digest.get("fields")
    automation = fields.get("automation") if isinstance(fields, Mapping) else None
    if not isinstance(automation, Mapping):
        # Automation may be reported as unreadable while routing is fine; a
        # send edit must still be possible.  The caller turns this into
        # ``automation_complete=False`` and refuses envelope writes.
        return (), False
    groups: list[tuple[str, list[Any]]] = []
    track_groups = automation.get("tracks")
    if isinstance(track_groups, list):
        for group in track_groups:
            if isinstance(group, Mapping) and isinstance(group.get("envelopes"), list):
                owner = group.get("track_guid")
                if isinstance(owner, str) and owner:
                    groups.append((owner, group["envelopes"]))
    master_rows = automation.get("master")
    if isinstance(master_rows, list) and master_guid:
        groups.append((master_guid, master_rows))
    rows: list[EnvelopeRow] = []
    for owner_guid, envelopes in groups:
        for envelope in envelopes:
            if not isinstance(envelope, Mapping):
                continue
            name = envelope.get("name")
            guid = envelope.get("guid")
            points = envelope.get("points")
            scaling = envelope.get("scaling_mode")
            if (not isinstance(name, str) or not name or
                    not isinstance(guid, str) or not guid or
                    not isinstance(points, list) or
                    isinstance(scaling, bool) or not isinstance(scaling, int)):
                raise BridgeUnavailable(
                    "REAPER returned an invalid envelope row",
                    details={"owner_guid": owner_guid,
                             "envelope": envelope.get("name")},
                )
            parsed: list[EnvelopePoint] = []
            for point in points:
                if not isinstance(point, Mapping):
                    raise BridgeUnavailable("REAPER returned an invalid envelope point")
                time_s = _finite(point.get("time_s"))
                value = _finite(point.get("value"))
                if time_s is None or value is None or time_s < 0:
                    raise BridgeUnavailable(
                        "REAPER returned an envelope point with an invalid time or value",
                        details={"owner_guid": owner_guid, "envelope": name,
                                 "point": dict(point)},
                    )
                shape = point.get("shape")
                tension = point.get("tension")
                parsed.append(EnvelopePoint(
                    index=int(point.get("index") or 0),
                    time_s=time_s, value=value,
                    shape=(int(shape) if isinstance(shape, (int, float)) and
                           not isinstance(shape, bool) else 0),
                    tension=(float(tension) if isinstance(tension, (int, float)) and
                             not isinstance(tension, bool) else 0.0),
                ))
            fx_index = envelope.get("fx_index")
            rows.append(EnvelopeRow(
                owner_guid=owner_guid,
                owner_name=names.get(owner_guid, ""),
                name=name,
                guid=guid,
                scaling_mode=scaling,
                active=(envelope.get("active") if isinstance(envelope.get("active"), bool)
                        else None),
                visible=(envelope.get("visible")
                         if isinstance(envelope.get("visible"), bool) else None),
                armed=(envelope.get("armed") if isinstance(envelope.get("armed"), bool)
                       else None),
                points=tuple(sorted(parsed, key=lambda point: point.time_s)),
                fx_index=(fx_index if isinstance(fx_index, int) and
                          not isinstance(fx_index, bool) else None),
                parameter_index=(envelope.get("parameter_index")
                                 if isinstance(envelope.get("parameter_index"), int) and
                                 not isinstance(envelope.get("parameter_index"), bool)
                                 else None),
            ))
    rows.sort(key=lambda row: (row.owner_guid, row.name, row.guid))
    return tuple(rows), True


def read_edit_context(session: Any) -> EditSnapshot:
    """Read one consistent snapshot of tracks, sends and envelopes.

    The context read and the digest read are two bridge round trips; the
    project must not move between them.  A counter difference is a real change,
    so the snapshot fails closed with :class:`StaleContext` instead of pairing
    faders from one instant with routing from another.
    """
    context = session.snapshot(include_items=False)
    if not isinstance(context, Mapping):
        raise BridgeUnavailable("REAPER returned an invalid project context")
    revision = ProjectRevision.from_dict(context.get("revision") or {})
    if not revision.project_id or revision.change_count < 0:
        raise BridgeUnavailable("REAPER returned an invalid project revision")
    track_entries = context.get("tracks")
    master_entry = context.get("master")
    if not isinstance(track_entries, list) or not isinstance(master_entry, Mapping):
        raise BridgeUnavailable("REAPER returned an incomplete project context")
    declared_count = context.get("track_count")
    if (isinstance(declared_count, int) and not isinstance(declared_count, bool)
            and declared_count != len(track_entries)):
        raise BridgeUnavailable(
            "REAPER track count does not match its context snapshot",
            details={"track_count": declared_count,
                     "returned_tracks": len(track_entries)},
        )
    tracks = tuple(_track_from_context(entry, is_master=False)
                   for entry in track_entries if isinstance(entry, Mapping))
    master = _track_from_context(master_entry, is_master=True)
    names = {track.guid: track.name for track in tracks}
    names[master.guid] = master.name

    digest = session.digest()
    if not isinstance(digest, Mapping):
        raise BridgeUnavailable("REAPER returned an invalid project digest")
    raw_change_count = digest.get("change_count")
    digest_change_count = (raw_change_count
                           if isinstance(raw_change_count, int)
                           and not isinstance(raw_change_count, bool) else -1)
    digest_revision = ProjectRevision(
        project_id=str(digest.get("project_id") or ""),
        change_count=digest_change_count,
        path=str(digest.get("path") or ""),
    )
    if (digest_revision.project_id != revision.project_id or
            digest_revision.change_count != revision.change_count or
            digest_revision.path != revision.path):
        raise StaleContext(
            "The project changed while its tracks, sends and envelopes were being read",
            details={"context_revision": revision.to_dict(),
                     "digest_revision": digest_revision.to_dict()},
        )
    missing = digest.get("missing")
    coverage = digest.get("coverage")
    sends_complete = True
    automation_complete = True
    if isinstance(coverage, Mapping):
        sends_complete = coverage.get("sends") is not False
        automation_complete = (coverage.get("automation",
                                            coverage.get("envelopes")) is not False)
    envelope_rows, automation_readable = _envelope_rows(digest, names, master.guid)
    automation_complete = automation_complete and automation_readable
    return EditSnapshot(
        revision=revision,
        tracks=tracks,
        master=master,
        sends=_send_rows(digest, names),
        envelopes=envelope_rows,
        missing=tuple(str(entry) for entry in missing)
        if isinstance(missing, list) else (),
        sends_complete=sends_complete,
        automation_complete=automation_complete,
    )


__all__ = [
    "AUTOMATION_MODES",
    "SCALING_MODE_NAMES",
    "EditSnapshot",
    "EnvelopePoint",
    "EnvelopeRow",
    "SendRow",
    "TrackRow",
    "automation_mode_name",
    "db_to_linear",
    "evaluate_envelope",
    "linear_to_db",
    "parse_automation_mode",
    "read_edit_context",
]
