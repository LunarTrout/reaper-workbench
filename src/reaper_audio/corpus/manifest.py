"""Corpus manifest: what each reference actually is.

A distance computed over a pile of files that mixes masters with rough mixes,
albums with singles and black metal with djent is not a target, it is an
average.  The manifest records the facts that make a corpus usable:

* ``source`` — the recording the excerpt came from (an album is one source);
* ``stage``  — ``mix`` or ``master``;
* ``genre``  — free text, used for clustering;
* ``year``   — optional.

Nothing here is guessed silently: an unknown field is ``null`` and the caller
decides whether such a reference may enter a target.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

STAGES = ("mix", "master", "unknown")


@dataclass
class Entry:
    file: str
    source: str
    stage: str = "unknown"
    genre: str | None = None
    year: int | None = None
    notes: str | None = None

    def to_dict(self) -> dict:
        return {"file": self.file, "source": self.source, "stage": self.stage,
                "genre": self.genre, "year": self.year, "notes": self.notes}


@dataclass
class Manifest:
    entries: list[Entry] = field(default_factory=list)

    def by_stage(self, stage: str) -> list[Entry]:
        return [e for e in self.entries if e.stage == stage]

    def by_source(self) -> dict[str, list[Entry]]:
        out: dict[str, list[Entry]] = {}
        for e in self.entries:
            out.setdefault(e.source, []).append(e)
        return out

    def validate(self) -> list[str]:
        problems = []
        seen = set()
        for e in self.entries:
            if e.stage not in STAGES:
                problems.append(f"{e.file}: unknown stage {e.stage!r}")
            if e.file in seen:
                problems.append(f"{e.file}: duplicate entry")
            seen.add(e.file)
            if not Path(e.file).exists():
                problems.append(f"{e.file}: file missing")
        return problems

    def to_dict(self) -> dict:
        return {"entries": [e.to_dict() for e in self.entries]}

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                              encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Manifest":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([Entry(**item) for item in data.get("entries", [])])

    @classmethod
    def scan(cls, directory: str | Path, *, stage: str = "unknown") -> "Manifest":
        entries = []
        for path in sorted(Path(directory).glob("*.wav")):
            source = path.stem.split("_")[0]
            entries.append(Entry(file=str(path), source=source, stage=stage))
        return cls(entries)
