"""A measure call writes its cells into the one mutable project state.

The fake host renders a deterministic signal and answers ``project.digest`` from
a mutable per-track row, so the tests can move a fader between two measurements
without any REAPER.  Every assertion is about the state document the service
wrote: which facts exist, what they carry, and which of them a changed object
invalidated before the new values were stored.
"""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from reaper_audio.context.project_state import ProjectState, StateStore
from reaper_audio.daw.measure import MeasureService

SR = 48000
PROJECT_ID = "proj-state-k"
PROJECT_PATH = "C:/fake/state.rpp"
INSTANCE_ID = "inst-state-k"
EPOCH = 3
G1 = "{TRACK-STATE-1}"
G2 = "{TRACK-STATE-2}"
MASTER = "{MASTER-STATE}"

#: Per-track synthetic content: frequency and level of the rendered signal.
TRACKS = {
    G1: {"name": "TRACK ONE", "freq": 220.0},
    G2: {"name": "TRACK TWO", "freq": 330.0},
}

VIA_MASTER_STAGE = "selected_track_via_master_parallel_batch_post_fx"
MASTER_STAGE = "master_mix_post_fx"
EXTERNAL_STAGE = "external_file"


def _signal(guid: str, start_s: float, frames: int, *, sr: int = SR) -> np.ndarray:
    spec = TRACKS[guid]
    t = start_s + np.arange(frames, dtype=np.float64) / sr
    mono = 0.25 * np.sin(2 * math.pi * spec["freq"] * t)
    return np.stack([mono, mono], axis=1)


def _master_signal(start_s: float, frames: int) -> np.ndarray:
    return sum((_signal(guid, start_s, frames) for guid in TRACKS),
               start=np.zeros((frames, 2), dtype=np.float64))


class FakeBridge:
    """Renders the synthetic project and answers the digest from track rows."""

    def __init__(self, context: dict[str, Any]) -> None:
        self.context = context
        self.render_calls: list[dict[str, Any]] = []
        self.digest_reads = 0
        self.volume = {guid: 1.0 for guid in TRACKS}
        self.sends: dict[str, list[dict[str, Any]]] = {guid: [] for guid in TRACKS}

    # -- context ------------------------------------------------------ #
    def project_context(self, project_id: str | None = None, **_kwargs: Any) -> dict:
        return copy.deepcopy(self.context)

    def digest_track_rows(self) -> list[dict[str, Any]]:
        return [{"guid": guid, "volume": self.volume[guid], "pan": 0.0,
                 "mute": False, "solo": 0, "channel_count": 2, "fx": [],
                 "sends": copy.deepcopy(self.sends[guid])}
                for guid in TRACKS]

    def project_digest(self, project_id: str | None = None, **_kwargs: Any) -> dict:
        self.digest_reads += 1
        return {
            "project_id": PROJECT_ID, "path": PROJECT_PATH,
            "change_count": self.context["revision"]["change_count"],
            "digest_version": 1, "coverage": {}, "missing": [],
            "fields": {
                "tracks": self.digest_track_rows(),
                "master": {"guid": MASTER, "volume": 1.0, "pan": 0.0,
                           "mute": False, "solo": 0, "channel_count": 2,
                           "fx": [], "sends": []},
                "automation": {"tracks": [{"track_guid": guid, "envelopes": []}
                                          for guid in TRACKS]},
                "items": [],
            },
        }

    def call_api(self, name: str, *values: Any, **_kwargs: Any) -> dict:
        if name == "Main_OnCommand" and values and values[0] == 1016:
            self.context["transport"]["play_state"] = 0
        return {"ok": True}

    # -- render ------------------------------------------------------- #
    def capture_render(self, **kwargs: Any) -> dict[str, Any]:
        self.render_calls.append(kwargs)
        output_dir = Path(kwargs["output_dir"])
        start = float(kwargs.get("section_start_s") or 0.0)
        end = float(kwargs.get("section_end_s") or self.context["length_s"])
        frames = int(round((end - start) * SR))
        files: list[dict[str, Any]] = []
        if kwargs.get("master_only"):
            path = output_dir / f"{kwargs['job_id']}-0-MASTER.wav"
            sf.write(path, _master_signal(start, frames), SR, subtype="FLOAT")
            files.append({"path": str(path), "bytes": path.stat().st_size,
                          "source_guid": MASTER, "name": "MASTER",
                          "is_master": True, "frames": frames,
                          "sample_rate": SR, "channels": 2})
        else:
            guids = kwargs.get("track_guids") or list(TRACKS)
            for index, guid in enumerate(guids):
                path = output_dir / f"{kwargs['job_id']}-{index}-{guid.strip('{}')}.wav"
                sf.write(path, _signal(guid, start, frames), SR, subtype="FLOAT")
                files.append({"path": str(path), "bytes": path.stat().st_size,
                              "source_guid": guid, "name": TRACKS[guid]["name"],
                              "is_master": False, "frames": frames,
                              "sample_rate": SR, "channels": 2})
        return {"files": files,
                "revision_after": copy.deepcopy(self.context["revision"]),
                "channels": 2, "sample_rate": SR}


class FakeSession:
    def __init__(self, bridge: FakeBridge) -> None:
        self.bridge = bridge

    def snapshot(self, **_kwargs: Any) -> dict:
        return copy.deepcopy(self.bridge.context)


def _context(*, length_s: float = 8.0) -> dict:
    tracks = []
    for index, (guid, spec) in enumerate(TRACKS.items()):
        tracks.append({
            "guid": guid, "index": index, "name": spec["name"],
            "mute": False, "solo": False, "sends": [],
            "items": [{"guid": f"{{ITEM-{index}}}", "mute": False,
                       "position_s": 0.0, "length_s": length_s}],
        })
    return {
        "revision": {"project_id": PROJECT_ID, "path": PROJECT_PATH,
                     "change_count": 7, "instance_id": INSTANCE_ID, "epoch": EPOCH},
        "length_s": length_s,
        "tracks": tracks,
        "master": {"guid": MASTER, "name": "MASTER", "items": []},
        "regions": [], "markers": [],
        "tempo": {"bpm": 120.0, "numerator": 4, "denominator": 4, "markers": []},
        "transport": {"play_state": 0, "cursor_s": 0.0, "edit_cursor_s": 0.0},
    }


def _service(tmp_path: Path, context: dict | None = None
             ) -> tuple[MeasureService, FakeBridge]:
    bridge = FakeBridge(context or _context())
    return MeasureService(FakeSession(bridge), tmp_path / "archive"), bridge


def _state(tmp_path: Path) -> ProjectState:
    return ProjectState(StateStore(tmp_path / "archive"), PROJECT_ID)


class RecordingState:
    """A real ``ProjectState`` that records the order of the writer's calls."""

    def __init__(self, state: ProjectState) -> None:
        self.state = state
        self.calls: list[tuple[str, Any]] = []

    @property
    def objects(self) -> Any:
        return self.state.objects

    @property
    def state_token(self) -> int:
        return self.state.state_token

    def mark_stale(self, changed: Any, routing_changed: bool = False) -> dict[str, str]:
        result = self.state.mark_stale(changed, routing_changed=routing_changed)
        self.calls.append(("mark_stale", dict(result)))
        return result

    def upsert_objects(self, tracks: Any = None, master: Any = None,
                       routing: Any = None) -> dict[str, Any]:
        self.calls.append(("upsert_objects", None))
        return self.state.upsert_objects(tracks, master, routing)

    def upsert_fact(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("upsert_fact", kwargs.get("source_id")))
        return self.state.upsert_fact(**kwargs)

    def save(self) -> Path:
        self.calls.append(("save", None))
        return self.state.save()


@pytest.fixture(autouse=True)
def _no_analysis_pool(monkeypatch):
    """Window analysis runs in-process; the cache is not what is under test."""
    monkeypatch.setenv("REAPER_V2_ANALYSIS_CACHE", "0")


def test_every_interval_source_and_metric_becomes_one_fact(tmp_path) -> None:
    service, _bridge = _service(tmp_path)
    result = service.measure(sources=[G1, G2], intervals=[[0.0, 2.0], [2.0, 4.0]],
                             metrics=["lufs", "peak"], analysis_workers=1)

    assert result["state"]["facts_written"] == 2 * 2 * 2
    assert result["state"]["state_token"] >= result["state"]["facts_written"]

    state = _state(tmp_path)
    facts = state.facts_for(status="current")
    assert len(facts) == 8
    keys = {(fact["source_id"], tuple(fact["window"]), tuple(fact["key"]["metrics"]))
            for fact in facts}
    for source_id in (G1, G2):
        for window in ((0.0, 2.0), (2.0, 4.0)):
            for metric in ("integrated_lufs", "peak_dbfs"):
                assert (source_id, window, (metric,)) in keys

    cells = {(row["start_s"], row["end_s"]): result["table"]["cells"][index]
             for index, row in enumerate(result["table"]["rows"])}
    for fact in facts:
        assert fact["stage"] == VIA_MASTER_STAGE
        assert fact["revision"]["change_count"] == 7
        metric = fact["key"]["metrics"][0]
        cell = cells[tuple(fact["window"])][fact["source_id"]]
        assert fact["metrics"][metric] == cell[metric]
        assert fact["fingerprints"]["source"]["guid"] == fact["source_id"]
        assert fact["fingerprints"]["master"]["guid"] == MASTER
        assert fact["fingerprints"]["routing"] is not None

    # The objects the measurement saw are stored too, so the next comparison is
    # against what was actually measured rather than an older capture.
    objects = state.objects
    assert set(objects["tracks"]) == {G1, G2}
    assert objects["master"]["fingerprint"]["guid"] == MASTER


def test_a_second_measurement_of_the_same_cell_supersedes_the_first(tmp_path) -> None:
    service, _bridge = _service(tmp_path)
    first = service.measure(sources=[G1], intervals=[[0.0, 2.0]], metrics=["lufs"],
                            analysis_workers=1)
    second = service.measure(sources=[G1], intervals=[[0.0, 2.0]], metrics=["lufs"],
                             analysis_workers=1)

    assert second["state"]["state_token"] > first["state"]["state_token"]
    assert second["state"]["changes"]["changed"] == {}
    assert second["state"]["changes"]["stale_facts"] == []

    state = _state(tmp_path)
    current = state.facts_for(source_id=G1, status="current")
    superseded = state.facts_for(source_id=G1, status="superseded")
    assert len(current) == len(superseded) == 1
    assert superseded[0]["superseded_by"] == current[0]["id"]
    assert current[0]["superseded_by"] is None
    assert current[0]["status"] == "current"


def test_a_changed_fader_marks_old_facts_stale_before_the_new_write(tmp_path) -> None:
    bridge = FakeBridge(_context())
    service = MeasureService(FakeSession(bridge), tmp_path / "archive")
    before = _state(tmp_path)  # not used before the write; proves a fresh load
    assert before.state_token == 0

    first = service.measure(sources=[G1], intervals=[[0.0, 2.0]], metrics=["lufs"],
                            analysis_workers=1)
    old_fact = _state(tmp_path).facts_for(source_id=G1, status="current")[0]
    assert first["state"]["changes"]["stale_facts"] == []

    recorder = RecordingState(_state(tmp_path))
    service = MeasureService(FakeSession(bridge), tmp_path / "archive",
                             state_writer=recorder)
    bridge.volume[G1] = 0.5
    second = service.measure(sources=[G1], intervals=[[2.0, 4.0]], metrics=["lufs"],
                             analysis_workers=1)

    changes = second["state"]["changes"]
    assert changes["changed"] == {G1: ["volume"]}
    assert changes["routing_changed"] is False
    assert changes["stale_facts"] == [old_fact["id"]]
    names = [name for name, _payload in recorder.calls]
    assert names[0] == "mark_stale", "the stale check runs before any new value"
    assert names.index("mark_stale") < names.index("upsert_fact")
    assert names.index("upsert_objects") < names.index("upsert_fact")
    assert recorder.calls[0][1] == {old_fact["id"]: "source:volume"}

    state = _state(tmp_path)
    stale = state.facts_for(source_id=G1, status="stale")
    assert [fact["id"] for fact in stale] == [old_fact["id"]]
    assert "volume" in stale[0]["stale_reason"]
    current = state.facts_for(source_id=G1, status="current")
    assert [fact["window"] for fact in current] == [[2.0, 4.0]]

    # The new fingerprint was stored with the measurement, so the same unchanged
    # project must not invalidate the fact that was just written.
    third = service.measure(sources=[G1], intervals=[[4.0, 6.0]], metrics=["lufs"],
                            analysis_workers=1)
    assert third["state"]["changes"]["changed"] == {}
    assert third["state"]["changes"]["stale_facts"] == []
    assert len(_state(tmp_path).facts_for(source_id=G1, status="current")) == 2


def test_a_new_send_marks_via_master_facts_stale(tmp_path) -> None:
    bridge = FakeBridge(_context())
    service = MeasureService(FakeSession(bridge), tmp_path / "archive")
    service.measure(sources=[G1], intervals=[[0.0, 2.0]], metrics=["lufs"],
                    analysis_workers=1)
    old_fact = _state(tmp_path).facts_for(source_id=G1, status="current")[0]

    bridge.sends[G1] = [{"index": 0, "dest_guid": G2, "volume": 1.0,
                         "pan": 0.0, "mode": 0}]
    second = service.measure(sources=[G1], intervals=[[2.0, 4.0]], metrics=["lufs"],
                             analysis_workers=1)

    changes = second["state"]["changes"]
    assert changes["routing_changed"] is True
    assert "sends" in changes["changed"][G1]
    assert changes["stale_facts"] == [old_fact["id"]]
    stale = _state(tmp_path).facts_for(source_id=G1, status="stale")
    assert "routing" in stale[0]["stale_reason"]


def test_a_band_array_is_one_fact_named_bands(tmp_path) -> None:
    service, _bridge = _service(tmp_path)
    result = service.measure(sources=[G1], intervals=[[0.0, 2.0]],
                             metrics=["lufs", "bands"], bands="octave",
                             analysis_workers=1)
    # One scalar metric plus the band array (raw levels + aggregate).
    assert result["state"]["facts_written"] == 2

    facts = _state(tmp_path).facts_for(source_id=G1, status="current")
    band_facts = [fact for fact in facts if fact["key"]["metrics"] == ["bands"]]
    assert len(band_facts) == 1
    values = band_facts[0]["metrics"]["bands"]
    cell = result["table"]["cells"][0][G1]
    assert values["band_level_dbfs"] == cell["band_level_dbfs"]
    assert values["band_aggregate"] == cell["band_aggregate"]


def test_an_external_file_is_stored_without_object_fingerprints(tmp_path) -> None:
    reference = tmp_path / "reference.wav"
    sf.write(reference, _signal(G1, 0.0, 4 * SR), SR, subtype="FLOAT")
    service, bridge = _service(tmp_path)
    result = service.measure(sources=[{"kind": "file", "path": str(reference)}],
                             intervals=[[0.0, 2.0]], metrics=["lufs"],
                             analysis_workers=1)
    assert bridge.render_calls == []

    facts = _state(tmp_path).facts_for(status="current")
    assert len(facts) == 1
    fact = facts[0]
    assert fact["source_id"] == result["sources"][0]["source_id"]
    assert fact["stage"] == EXTERNAL_STAGE
    assert fact["fingerprints"] == {"source": None, "master": None, "routing": None}
    assert _state(tmp_path).objects["tracks"] == {}


def test_a_master_measure_keeps_the_master_fingerprint(tmp_path) -> None:
    service, bridge = _service(tmp_path)
    result = service.measure(sources="all", stage="master", intervals=[[0.0, 2.0]],
                             metrics=["lufs"], analysis_workers=1)
    # The measurement reads the digest once; the state write reuses that read.
    assert bridge.digest_reads == 1
    assert result["state"]["facts_written"] == 1

    fact = _state(tmp_path).facts_for(status="current")[0]
    assert fact["stage"] == MASTER_STAGE
    assert fact["source_id"] == MASTER
    assert fact["fingerprints"]["source"]["guid"] == MASTER


def test_no_archive_root_writes_no_state(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    bridge = FakeBridge(_context())
    service = MeasureService(FakeSession(bridge), None)
    result = service.measure(sources=[G1], intervals=[[0.0, 2.0]], metrics=["lufs"],
                             analysis_workers=1)

    assert result["state"] == {"state_token": 0, "facts_written": 0, "changes": {}}
    assert not (tmp_path / "projects").exists(), "no state directory is created"
    assert bridge.digest_reads == 1, "only the render read the digest"

def test_saved_mcp_state_refreshes_master_after_live_fader_change(tmp_path):
    from types import SimpleNamespace
    from reaper_audio.mcp_server import _refresh_saved_state

    service, bridge = _service(tmp_path)
    service.measure(sources="all", stage="master", intervals=[[0, 2]],
                    metrics=["lufs"], analysis_workers=1)
    bridge.volume[next(iter(TRACKS))] = 0.5
    session = SimpleNamespace(digest=bridge.project_digest)
    prior_renders = len(bridge.render_calls)
    document, basis = _refresh_saved_state(session, str(tmp_path / "archive"), PROJECT_ID)
    assert basis == "live_digest"
    master, = document["facts"].values()
    assert master["status"] == "stale"
    assert "master_inputs" in master["stale_reason"]
    assert _state(tmp_path).facts_for()[0]["status"] == "stale"
    assert len(bridge.render_calls) == prior_renders
