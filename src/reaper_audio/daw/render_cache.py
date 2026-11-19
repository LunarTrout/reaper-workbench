"""One current render per source and interval, reused until the project changes.

The archive keeps no history of renders: a fixed path per key is overwritten on
the next revision, and a lookup only succeeds when the recorded revision still
equals the project's current one.  Any edit therefore invalidates the file by
construction, while a second analysis of the same audio (a finer profile step)
never renders twice.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Mapping


def _key(**parts: Any) -> str:
    payload = json.dumps(parts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class RenderCache:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def _paths(self, key: str) -> tuple[Path, Path]:
        return self.root / f"{key}.wav", self.root / f"{key}.json"

    def lookup(self, *, project_id: str, revision: Mapping[str, Any],
               source_guid: str, start_s: float | None, end_s: float | None,
               stage: str, channels: str, sample_rate: int | None,
               tail_ms: int) -> dict[str, Any] | None:
        key = _key(project_id=project_id, source=source_guid, start=start_s,
                   end=end_s, stage=stage, channels=channels,
                   sample_rate=sample_rate, tail=tail_ms)
        wav, meta_path = self._paths(key)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not wav.is_file() or wav.stat().st_size <= 44:
            return None
        recorded = meta.get("revision") or {}
        if (int(recorded.get("change_count", -1)) !=
                int(revision.get("change_count", -2)) or
                str(recorded.get("instance_id")) != str(revision.get("instance_id"))):
            return None
        return {"path": str(wav), "meta": meta, "key": key}

    def store(self, *, project_id: str, revision: Mapping[str, Any],
              source_guid: str, start_s: float | None, end_s: float | None,
              stage: str, channels: str, sample_rate: int | None,
              tail_ms: int, source_wav: Path,
              frames: int, rate: int, channels_count: int) -> dict[str, Any]:
        key = _key(project_id=project_id, source=source_guid, start=start_s,
                   end=end_s, stage=stage, channels=channels,
                   sample_rate=sample_rate, tail=tail_ms)
        wav, meta_path = self._paths(key)
        self.root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_wav, wav)
        meta = {"project_id": project_id, "source_guid": source_guid,
                "start_s": start_s, "end_s": end_s, "stage": stage,
                "channels": channels, "sample_rate": sample_rate,
                "tail_ms": tail_ms, "frames": int(frames),
                "rate": int(rate), "channels_count": int(channels_count),
                "revision": dict(revision)}
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        return {"path": str(wav), "meta": meta, "key": key}
