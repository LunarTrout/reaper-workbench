"""Project-bound typed references for passing REAPER objects through ReaScript."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import ValidationError


@dataclass(frozen=True, slots=True)
class ReaperHandle:
    kind: str
    project_id: str
    change_count: int
    stable_id: str | None = None
    token: str | None = None
    bridge_session: str | None = None

    def __post_init__(self) -> None:
        if not self.kind or not self.project_id:
            raise ValidationError("A REAPER handle needs a type and project ID")
        if self.change_count < 0:
            raise ValidationError("A REAPER handle needs a valid project revision")
        if bool(self.stable_id) == bool(self.token):
            raise ValidationError("A REAPER handle must contain exactly one stable ID or token")
        if self.token and not self.bridge_session:
            raise ValidationError("An opaque REAPER handle must identify its bridge session")

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "project_id": self.project_id,
            "change_count": self.change_count,
        }
        if self.stable_id:
            payload["stable_id"] = self.stable_id
        else:
            payload["token"] = self.token
            payload["bridge_session"] = self.bridge_session
        return {"$reaper_handle": payload}

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> "ReaperHandle":
        payload = value.get("$reaper_handle")
        if not isinstance(payload, dict):
            raise ValidationError("Invalid REAPER handle response")
        return cls(kind=str(payload.get("kind", "")),
                   project_id=str(payload.get("project_id", "")),
                   change_count=int(payload.get("change_count", -1)),
                   stable_id=payload.get("stable_id"), token=payload.get("token"),
                   bridge_session=payload.get("bridge_session"))
