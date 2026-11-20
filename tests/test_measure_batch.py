"""One measure call: sources × intervals × metrics, against a fake host.

The fake renders a deterministic signal that is a pure function of absolute
project time, so a union render cut into windows is sample-identical to a
render of each window on its own.  That is what makes "one cell equals one
separate measurement" a meaningful assertion.
"""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from reaper_audio.analysis.audio import load_audio
from reaper_audio.analysis.metrics import ANALYSIS_VERSION, measure_audio
from reaper_audio.daw.measure import MeasureService, cache_options
from reaper_audio.daw.measure_table import parse_bands, parse_metrics
from reaper_audio.errors import MediaUnresolved, ValidationError
from reaper_audio.models import AudioBuffer, Section

SR = 48000
PROJECT_ID = "proj-vers1"
PROJECT_PATH = "C:/fake/vers1.rpp"
INSTANCE_ID = "inst-1"
EPOCH = 4
G1 = "{TRACK-G1}"
G2 = "{TRACK-G2}"
MASTER = "{MASTER}"

#: Per-track synthetic content: amplitude, frequency, stereo skew.
TRACKS = {
    G1: {"name": "GTR RHYTHM L", "amp": 0.30, "freq": 220.0, "skew": 0.85},
    G2: {"name": "GTR RHYTHM R", "amp": 0.22, "freq": 330.0, "skew": 0.95},
}


def _signal(guid: str, start_s: float, frames: int, *, sr: int = SR,
            gain: float = 1.0) -> np.ndarray:
    spec = TRACKS[guid]
    t = start_s + np.arange(frames, dtype=np.float64) / sr
    envelope = 1.0 + 0.5 * np.sin(2 * math.pi * 0.25 * t)
    mono = spec["amp"] * envelope * (np.sin(2 * math.pi * spec["freq"] * t)
                                     + 0.4 * np.sin(2 * math.pi * 1.5 * spec["freq"] * t))
    left = mono * gain
    right = mono * spec["skew"] * gain
    return np.stack([left, right], axis=1)


def _master_signal(start_s: float, frames: int) -> np.ndarray:
    return sum(( _signal(guid, start_s, frames) for guid in TRACKS ),
               start=np.zeros((frames, 2), dtype=np.float64))


class FakeBridge:
    """Renders the synthetic project and records every host call."""

    def __init__(self, context: dict[str, Any]) -> None:
        self.context = context
        self.api_calls: list[tuple[str, tuple]] = []
        self.render_calls: list[dict[str, Any]] = []
        self.media_repair = True

    # -- context ----------------------------------------------------- #
    def _tracks(self) -> list[dict[str, Any]]:
        return self.context["tracks"]

    def project_context(self, project_id: str | None = None, **_kwargs: Any) -> dict:
        return copy.deepcopy(self.context)

    def project_digest(self, project_id: str | None = None, **_kwargs: Any) -> dict:
        return {
            "project_id": PROJECT_ID, "path": PROJECT_PATH,
            "change_count": self.context["revision"]["change_count"],
            "digest_version": 1, "coverage": {}, "missing": [],
            "fields": {"tracks": [{"guid": track["guid"], "sends": []}
                                  for track in self._tracks()]},
        }

    def call_api(self, name: str, *values: Any, **_kwargs: Any) -> dict:
        self.api_calls.append((name, values))
        if name == "Main_OnCommand" and values and values[0] == 1016:
            self.context["transport"]["play_state"] = 0
        if name == "Main_OnCommand" and values and values[0] == 40101:
            if self.media_repair:
                for track in self._tracks():
                    for item in track.get("items") or []:
                        item["source_online"] = True
                        item["source_length_s"] = 8.0
                        item["source_sample_rate"] = SR
                        item["source_channels"] = 2
        return {"ok": True}

    # -- render ------------------------------------------------------ #
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
                              "source_guid": guid,
                              "name": TRACKS[guid]["name"], "is_master": False,
                              "frames": frames, "sample_rate": SR, "channels": 2})
        return {"files": files, "revision_after": copy.deepcopy(self.context["revision"]),
                "channels": 2, "sample_rate": SR}


class FakeSession:
    def __init__(self, bridge: FakeBridge) -> None:
        self.bridge = bridge

    def snapshot(self, **_kwargs: Any) -> dict:
        return copy.deepcopy(self.bridge.context)


def _context(*, regions: list[dict] | None = None,
             markers: list[dict] | None = None,
             tempo: dict | None = None,
             play_state: int = 0, cursor_s: float = 0.0,
             length_s: float = 12.0, offline: bool = False) -> dict:
    tracks = []
    for index, (guid, spec) in enumerate(TRACKS.items()):
        item = {"guid": f"{{ITEM-{index}}}", "mute": False,
                "position_s": 0.0, "length_s": length_s}
        if offline:
            item.update({"source_online": False, "source_length_s": 0.0,
                         "source_sample_rate": 0, "source_channels": 0})
        tracks.append({"guid": guid, "index": index, "name": spec["name"],
                       "mute": False, "solo": False, "items": [item],
                       "sends": []})
    return {
        "revision": {"project_id": PROJECT_ID, "path": PROJECT_PATH,
                     "change_count": 7, "instance_id": INSTANCE_ID, "epoch": EPOCH},
        "length_s": length_s,
        "tracks": tracks,
        "master": {"guid": MASTER, "name": "MASTER", "items": []},
        "regions": list(regions or []),
        "markers": list(markers or []),
        "tempo": tempo or {"bpm": 120.0, "numerator": 4, "denominator": 4,
                           "markers": []},
        "transport": {"play_state": play_state, "cursor_s": cursor_s,
                      "edit_cursor_s": cursor_s},
    }


def _service(tmp_path: Path, context: dict | None = None) -> tuple[MeasureService, FakeBridge]:
    bridge = FakeBridge(context or _context())
    return MeasureService(FakeSession(bridge), tmp_path / "archive"), bridge


@pytest.fixture(autouse=True)
def _no_analysis_pool(monkeypatch):
    """Window analysis runs in-process; the cache is exercised explicitly."""
    monkeypatch.setenv("REAPER_V2_ANALYSIS_CACHE", "0")


def test_a_table_cell_equals_a_separate_measurement_of_the_same_window(tmp_path) -> None:
    service, _bridge = _service(tmp_path)
    windows = [[0.0, 4.0], [4.0, 8.0]]
    metrics = ["lufs", "peak", "crest"]
    table = service.measure(sources=[G1, G2], intervals=windows, metrics=metrics,
                            analysis_workers=1)

    assert table["table"]["rows"][0]["start_s"] == 0.0
    assert len(table["table"]["columns"]) == 2
    assert len(table["table"]["cells"]) == 2
    assert table["source_count"] == 2

    for index, (start, end) in enumerate(windows):
        for guid in (G1, G2):
            single = service.measure(sources=[guid], section=(start, end),
                                     metrics=metrics, analysis_workers=1)
            cell = table["table"]["cells"][index][guid]
            separate = single["table"]["cells"][0][guid]
            for key in ("integrated_lufs", "peak_dbfs", "crest_db"):
                assert cell[key] == pytest.approx(separate[key], abs=0.1), (
                    f"{guid} {key} at {start}-{end}")


def test_a_cell_matches_the_signal_computed_directly(tmp_path) -> None:
    service, _bridge = _service(tmp_path)
    result = service.measure(sources=[G1], section=(2.0, 6.0), metrics=["lufs"],
                             analysis_workers=1)
    direct = measure_audio(AudioBuffer(samples=_signal(G1, 2.0, 4 * SR),
                                       sample_rate=SR))
    assert result["table"]["cells"][0][G1]["integrated_lufs"] == pytest.approx(
        direct.metrics["integrated_lufs"].value, abs=0.1)


def test_overlapping_intervals_are_measured_per_window(tmp_path) -> None:
    service, bridge = _service(tmp_path)
    result = service.measure(sources=[G1], intervals=[[0.0, 6.0], [3.0, 9.0]],
                             metrics=["lufs"], analysis_workers=1)
    assert len(bridge.render_calls) == 1, "one union render, not one per window"
    note = result["notes"][0]
    assert note["code"] == "intervals_overlap"
    assert note["pairs"][0]["a"] == 0 and note["pairs"][0]["b"] == 1
    assert note["pairs"][0]["shared_audio_s"] == pytest.approx(3.0)
    assert note["pairs"][0]["a_interval"] == {"start_s": 0.0, "end_s": 6.0}
    assert note["pairs"][0]["b_interval"] == {"start_s": 3.0, "end_s": 9.0}
    first = result["table"]["cells"][0][G1]["integrated_lufs"]
    second = result["table"]["cells"][1][G1]["integrated_lufs"]
    assert first != second, "the two windows cover different audio"
    for index, (start, end) in enumerate([(0.0, 6.0), (3.0, 9.0)]):
        single = service.measure(sources=[G1], section=(start, end),
                                 metrics=["lufs"], analysis_workers=1)
        assert result["table"]["cells"][index][G1]["integrated_lufs"] == pytest.approx(
            single["table"]["cells"][0][G1]["integrated_lufs"], abs=0.1)


def test_regions_are_the_intervals(tmp_path) -> None:
    context = _context(regions=[{"position_s": 0.0, "end_s": 4.0, "name": "intro"},
                                {"position_s": 4.0, "end_s": 9.0, "name": "verse"}],
                       length_s=12.0)
    service, _bridge = _service(tmp_path, context)
    result = service.measure(sources=[G1], intervals="regions",
                             metrics=["lufs"], analysis_workers=1)
    assert [row["label"] for row in result["table"]["rows"]] == ["intro", "verse"]
    assert [row["origin"] for row in result["table"]["rows"]] == ["region", "region"]
    assert result["table"]["rows"][1]["start_s"] == 4.0


def test_a_project_without_regions_fails_with_a_usable_message(tmp_path) -> None:
    service, bridge = _service(tmp_path)
    with pytest.raises(ValidationError) as error:
        service.measure(sources=[G1], intervals="regions", analysis_workers=1)
    assert "regions" in error.value.message.lower()
    assert "bars:" in error.value.details["next_action"]
    assert bridge.render_calls == [], "nothing is rendered for an impossible request"


def test_a_bar_grid_is_built_from_the_tempo_map(tmp_path) -> None:
    tempo = {"bpm": 120.0, "numerator": 4, "denominator": 4,
             "markers": [{"time_s": 4.0, "bpm": 60.0, "numerator": 4,
                          "denominator": 4, "linear": False}]}
    service, _bridge = _service(tmp_path, _context(tempo=tempo, length_s=12.0))
    result = service.measure(sources=[G1], intervals="bars:2",
                             metrics=["lufs"], analysis_workers=1)
    # Bars at 120 BPM are 2 s (0, 2, 4); the 60 BPM bars after 4 s are 4 s.
    assert [(row["start_s"], row["end_s"]) for row in result["table"]["rows"]] == [
        (0.0, 4.0), (4.0, 12.0)]
    assert result["table"]["rows"][0]["label"] == "bars 1-2"


def test_metrics_filter_what_leaves_the_library(tmp_path) -> None:
    service, _bridge = _service(tmp_path)
    bare = service.measure(sources=[G1], intervals=[[0.0, 4.0]],
                           metrics=["lufs"], analysis_workers=1)
    assert set(bare["table"]["cells"][0][G1]) == {"integrated_lufs"}
    with_bands = service.measure(sources=[G1], intervals=[[0.0, 4.0]],
                                 metrics=["lufs", "bands"], bands="octave",
                                 analysis_workers=1)
    cell = with_bands["table"]["cells"][0][G1]
    assert len(cell["band_level_dbfs"]) == 28
    assert len(cell["band_aggregate"]["level_dbfs"]) == 10
    assert with_bands["table"]["bands"]["mode"] == "octave"


def test_an_unknown_metric_is_refused_before_any_render(tmp_path) -> None:
    service, bridge = _service(tmp_path)
    with pytest.raises(ValidationError):
        service.measure(sources=[G1], intervals=[[0.0, 4.0]],
                        metrics=["loudness_target"], analysis_workers=1)
    assert bridge.render_calls == []


def test_section_and_intervals_together_are_refused(tmp_path) -> None:
    service, _bridge = _service(tmp_path)
    with pytest.raises(ValidationError):
        service.measure(sources=[G1], section=(0.0, 4.0),
                        intervals=[[0.0, 4.0]], analysis_workers=1)


def test_an_external_file_is_measured_in_place(tmp_path) -> None:
    reference = tmp_path / "reference.wav"
    sf.write(reference, _signal(G1, 0.0, 8 * SR, gain=0.5), SR, subtype="FLOAT")
    service, bridge = _service(tmp_path)

    result = service.measure(sources=[{"kind": "file", "path": str(reference)}],
                             intervals=[[0.0, 2.0], [2.0, 5.0]],
                             metrics=["lufs", "peak"], analysis_workers=1)
    assert bridge.render_calls == [], "an external file is never rendered"
    assert result["sources"][0]["kind"] == "file"
    assert result["sources"][0]["stage"] == "external_file"
    assert result["sources"][0]["source_id"].startswith("file:")

    for index, (start, end) in enumerate([(0.0, 2.0), (2.0, 5.0)]):
        direct = measure_audio(load_audio(str(reference), Section(start, end)))
        cell = result["table"]["cells"][index][result["sources"][0]["source_id"]]
        assert cell["integrated_lufs"] == pytest.approx(
            direct.metrics["integrated_lufs"].value, abs=0.1)
        assert cell["peak_dbfs"] == pytest.approx(
            direct.metrics["peak_dbfs"].value, abs=0.1)


def test_a_missing_external_file_is_refused(tmp_path) -> None:
    service, _bridge = _service(tmp_path)
    with pytest.raises(ValidationError) as error:
        service.measure(sources=[{"kind": "file", "path": str(tmp_path / "nope.wav")}],
                        intervals=[[0.0, 2.0]], analysis_workers=1)
    assert "does not exist" in error.value.message


def test_auto_transport_stops_and_restores_position_and_play_state(tmp_path) -> None:
    service, bridge = _service(tmp_path, _context(play_state=1, cursor_s=5.25))
    result = service.measure(sources=[G1], intervals=[[0.0, 2.0]],
                             metrics=["lufs"], analysis_workers=1,
                             auto_transport=True)
    names = [(name, values) for name, values in bridge.api_calls]
    assert ("Main_OnCommand", (1016, 0)) in names
    assert ("SetEditCurPos", (5.25, True, False)) in names
    assert ("Main_OnCommand", (1007, 0)) in names
    assert result["transport"]["stopped"] is True
    assert result["transport"]["restored"] is True
    # The restore happens after the render, in that order.
    stop_index = names.index(("Main_OnCommand", (1016, 0)))
    cursor_index = names.index(("SetEditCurPos", (5.25, True, False)))
    assert stop_index < cursor_index


def test_playback_without_auto_transport_is_still_refused(tmp_path) -> None:
    service, _bridge = _service(tmp_path, _context(play_state=1))
    with pytest.raises(ValidationError) as error:
        service.measure(sources=[G1], intervals=[[0.0, 2.0]], analysis_workers=1)
    assert "Stop playback" in error.value.message
    assert "auto_transport" in error.value.details["next_action"]


def test_recording_is_never_stopped_by_auto_transport(tmp_path) -> None:
    service, _bridge = _service(tmp_path, _context(play_state=5))
    with pytest.raises(ValidationError) as error:
        service.measure(sources=[G1], intervals=[[0.0, 2.0]], analysis_workers=1,
                        auto_transport=True)
    assert "recording" in error.value.message


def test_media_is_repaired_inside_the_measurement_render(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("reaper_audio.daw.media_preflight.focus_reaper_window",
                        lambda: True)
    service, bridge = _service(tmp_path, _context(offline=True))
    result = service.measure(sources=[G1], intervals=[[0.0, 2.0]],
                             metrics=["lufs"], analysis_workers=1)
    assert ("Main_OnCommand", (40101, 0)) in bridge.api_calls
    assert result["preflight"]["recovered"] is True
    assert result["table"]["cells"][0][G1]["integrated_lufs"] is not None


def test_unresolvable_media_fails_before_the_render(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("reaper_audio.daw.media_preflight.focus_reaper_window",
                        lambda: True)
    service, bridge = _service(tmp_path, _context(offline=True))
    bridge.media_repair = False
    with pytest.raises(MediaUnresolved):
        service.measure(sources=[G1], intervals=[[0.0, 2.0]], analysis_workers=1)
    assert bridge.render_calls == []


def test_master_stage_is_a_single_master_column(tmp_path) -> None:
    service, bridge = _service(tmp_path)
    result = service.measure(sources="all", stage="master",
                             intervals=[[0.0, 2.0], [2.0, 4.0]],
                             metrics=["lufs"], analysis_workers=1)
    assert result["source_count"] == 1
    assert result["table"]["columns"][0]["kind"] == "master"
    assert result["sources"][0]["source_id"] == MASTER
    assert bridge.render_calls[0]["master_only"] is True


def test_a_legacy_single_window_call_keeps_its_shape(tmp_path) -> None:
    service, _bridge = _service(tmp_path)
    result = service.measure(sources=[G1], section=(0.0, 4.0), step_s=0.5,
                             analysis_workers=1)
    for key in ("ok", "operation", "stage", "interval", "sources", "source_count",
                "evidence", "revision", "timings", "project_capture_created",
                "producer", "profile"):
        assert key in result
    assert result["interval"] == {"start_s": 0.0, "end_s": 4.0}
    assert len(result["table"]["cells"]) == 1
    assert result["source_count"] == 1
    assert result["profile"]["step_s"] == 0.5
    assert result["profile"]["segments"], "the synthetic signal is not silent"


def test_profile_gates_are_parameters_and_the_used_values_are_returned(tmp_path) -> None:
    service, _bridge = _service(tmp_path)
    result = service.measure(sources=[G1], section=(0.0, 4.0), step_s=0.5,
                             profile_options={"noise_gate_dbfs": -60.0,
                                              "min_segment_s": 0.5},
                             analysis_workers=1)
    thresholds = result["profile"]["thresholds"]
    assert thresholds["noise_gate_dbfs"] == -60.0
    assert thresholds["min_segment_s"] == 0.5
    # A gate that was not overridden comes back as the declared default.
    assert thresholds["floor_percentile"] == 20.0
    assert thresholds["relative_ceiling_db"] == 50.0
    with pytest.raises(ValidationError):
        service.measure(sources=[G1], section=(0.0, 4.0), step_s=0.5,
                        profile_options={"loudness_target": -9.0},
                        analysis_workers=1)
    with pytest.raises(ValidationError):
        service.measure(sources=[G1], section=(0.0, 4.0),
                        profile_options={"noise_gate_dbfs": -60.0},
                        analysis_workers=1)


def test_evidence_units_cover_every_interval_and_source(tmp_path) -> None:
    service, _bridge = _service(tmp_path)
    result = service.measure(sources=[G1, G2], intervals=[[0.0, 2.0], [2.0, 4.0]],
                             metrics=["lufs", "peak"], analysis_workers=1)
    from reaper_audio.context.evidence import EvidenceStore

    units = EvidenceStore(tmp_path / "archive").all_units()
    assert result["evidence"]["units"] == len(units) > 0
    covered = {(unit["source_id"], unit["interval"]["start_s"],
                unit["interval"]["end_s"]) for unit in units}
    assert covered == {("track:" + G1, 0.0, 2.0), ("track:" + G1, 2.0, 4.0),
                       ("track:" + G2, 0.0, 2.0), ("track:" + G2, 2.0, 4.0)}
    assert {unit["method_version"] for unit in units} == {ANALYSIS_VERSION}


def test_the_cache_key_carries_metrics_bands_and_versions() -> None:
    from reaper_audio.daw.profile import PROFILE_VERSION

    first = cache_options(parse_metrics(["lufs"]), parse_bands(None))
    second = cache_options(parse_metrics(["lufs", "peak"]), parse_bands(None))
    third = cache_options(parse_metrics(["lufs"]), parse_bands("octave"))
    assert first["analysis_version"] == ANALYSIS_VERSION == "2"
    assert first["profile_version"] == PROFILE_VERSION
    assert first["metrics"] != second["metrics"]
    assert first["bands"] != third["bands"]
    assert first != second and first != third


def test_the_cache_reuses_only_an_identical_request_and_content(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REAPER_V2_ANALYSIS_CACHE", "1")
    reference = tmp_path / "reference.wav"
    sf.write(reference, _signal(G1, 0.0, 6 * SR, gain=0.5), SR, subtype="FLOAT")
    service, _bridge = _service(tmp_path)
    sources = [{"kind": "file", "path": str(reference)}]
    first = service.measure(sources=sources, intervals=[[0.0, 3.0]],
                            metrics=["lufs"], analysis_workers=1)
    assert first["analysis_cache"]["misses"] == 1
    assert first["analysis_cache"]["stored"] == 1
    value = first["table"]["cells"][0][first["sources"][0]["source_id"]]["integrated_lufs"]

    again = service.measure(sources=sources, intervals=[[0.0, 3.0]],
                            metrics=["lufs"], analysis_workers=1)
    assert again["analysis_cache"]["hits"] == 1
    assert again["table"]["cells"][0][again["sources"][0]["source_id"]][
        "integrated_lufs"] == value

    other_metric = service.measure(sources=sources, intervals=[[0.0, 3.0]],
                                   metrics=["rms"], analysis_workers=1)
    assert other_metric["analysis_cache"]["misses"] == 1, \
        "a different metric selection is a different cache key"
    with_bands = service.measure(sources=sources, intervals=[[0.0, 3.0]],
                                 metrics=["lufs"], bands="octave",
                                 analysis_workers=1)
    assert with_bands["analysis_cache"]["misses"] == 1, \
        "a different band plan is a different cache key"

    # Same path, louder content: the cache must not return the old numbers.
    sf.write(reference, _signal(G1, 0.0, 6 * SR, gain=1.0), SR, subtype="FLOAT")
    louder = service.measure(sources=sources, intervals=[[0.0, 3.0]],
                             metrics=["lufs"], analysis_workers=1)
    louder_value = louder["table"]["cells"][0][louder["sources"][0]["source_id"]][
        "integrated_lufs"]
    assert louder["analysis_cache"]["misses"] == 1
    assert louder_value > value + 5.0, "the rewritten file is 6 dB louder"
