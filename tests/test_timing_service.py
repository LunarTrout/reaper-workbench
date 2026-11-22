from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from reaper_audio.daw.timing_service import analyze_timing, plan_timing


class _Session:
    def __init__(self, path: Path) -> None:
        self.path = path

    def snapshot(self, *, project_id=None, include_items=False):
        return {
            "project": {"project_id": "proj_test"},
            "tracks": [{
                "guid": "{TRACK}", "name": "GTR",
                "items": [{
                    "guid": "{ITEM}", "position_s": 1.0, "length_s": 1.9,
                    "start_offset_s": 0.0, "playrate": 1.0,
                    "active_take": {"guid": "{TAKE}", "is_midi": False,
                                    "source_path": str(self.path)},
                }],
            }],
        }


def test_analysis_and_plan_translate_source_time_to_project_time(tmp_path) -> None:
    rate = 48_000
    samples = np.zeros((rate * 2, 2), dtype=np.float64)
    for time_s in (0.10, 0.35, 0.60, 0.85):
        index = int(time_s * rate)
        samples[index:index + 80, :] = 0.8
    source = tmp_path / "take.wav"
    sf.write(source, samples, rate, subtype="FLOAT")

    analysis = analyze_timing(_Session(source), project_id="proj_test",
                              track_guid="{TRACK}", item_guid="{ITEM}",
                              profile="picked", sensitivity=1.8)
    assert analysis["event_count"] >= 3
    assert analysis["events"][0]["project_s"] > 1.0

    plan = plan_timing(analysis["analysis_id"],
                       tempo_context={"bpm": 120, "numerator": 4,
                                      "denominator": 4, "markers": []},
                       subdivisions=(4,), tolerance_s=0.100)
    assert plan["blocks"]
    assert plan["subdivision"] == 4
    assert plan["blocks"][0]["project_target_start_s"] >= 1.0


def test_reference_events_change_the_targets(tmp_path) -> None:
    rate = 48_000
    samples = np.zeros((rate * 2, 2), dtype=np.float64)
    for time_s in (0.10, 0.35, 0.60):
        index = int(time_s * rate)
        samples[index:index + 80, :] = 0.8
    source = tmp_path / "ref-target.wav"
    sf.write(source, samples, rate, subtype="FLOAT")
    analysis = analyze_timing(_Session(source), project_id="proj_test",
                              track_guid="{TRACK}", item_guid="{ITEM}",
                              profile="picked", sensitivity=1.8)
    tempo = {"bpm": 120, "numerator": 4, "denominator": 4, "markers": []}
    plain = plan_timing(analysis["analysis_id"], tempo_context=tempo,
                        subdivisions=(4,))
    pushed = plan_timing(analysis["analysis_id"], tempo_context=tempo,
                         reference_events=[{"target_s": row["project_s"] + 0.04}
                                           for row in analysis["events"]])
    assert plain["target_kind"] == "project_grid"
    assert pushed["target_kind"] == "reference_events"
    assert pushed["blocks"][0]["shift_s"] > plain["blocks"][0]["shift_s"]


def test_waveform_finds_a_front_with_no_candidate_and_maps_the_same_source(tmp_path, monkeypatch):
    from reaper_audio import mcp_server
    from reaper_audio.daw import timing_service

    rate = 48000
    samples = np.zeros((rate, 2))
    samples[24000:24048] = [0.75, -0.75]
    source = tmp_path / "missed.wav"
    sf.write(source, samples, rate, subtype="FLOAT")
    snapshot = _Session(source).snapshot()
    snapshot["revision"] = {"project_id": "proj_test"}
    snapshot["tempo"] = {"bpm": 120, "numerator": 4, "denominator": 4, "markers": []}
    item = snapshot["tracks"][0]["items"][0]
    item.update(position_s=64.0, start_offset_s=0.25, length_s=0.75)
    monkeypatch.setattr(_Session, "snapshot", lambda self, **kwargs: snapshot)
    monkeypatch.setattr(mcp_server, "_session", lambda *args: _Session(source))
    monkeypatch.setitem(timing_service.ANALYSES, "a", {
        "method": "crossfade", "project_id": "proj_test", "track_guid": "{TRACK}",
        "item_guid": "{ITEM}", "take_guid": "{TAKE}", "source_path": str(source),
        "source_fingerprint": list(timing_service._file_fingerprint(source)),
        "position_s": 64.0, "source_offset_s": 0.25, "item_length_s": 0.75, "events": [],
    })

    result = mcp_server.reaper_timing(action="waveform", analysis_id="a",
                                      start_s=64.245, end_s=64.255, waveform_bins=10)
    assert result["ok"]
    assert result["signal"] == "source_pre_fx"
    assert result["candidate_count"] == 0
    assert result["level_columns"] == ["minimum", "maximum", "rms"]
    assert result["levels"][5] == [-0.75, 0.75, 0.75]
    assert result["source_sample_boundaries"][5] == 24000
    assert result["item_position_s"] + 24000 / rate - result["source_offset_s"] == 64.25
    assert any(row["project_s"] == 64.25 for row in result["grid"])

    item["start_offset_s"] += 0.01
    with pytest.raises(timing_service.ValidationError, match="changed since analysis"):
        timing_service.inspect_timing_waveform(
            _Session(source), project_id="proj_test", track_guid=None, item_guid=None,
            analysis_id="a", start_s=64.245, end_s=64.255)


def test_review_dispatch_respects_filter_when_both_bounds_are_set(monkeypatch):
    from reaper_audio import mcp_server
    from reaper_audio.daw import timing_service

    monkeypatch.setattr(timing_service, "inspect_timing_review", lambda plan_id, **args: args)
    result = mcp_server.reaper_timing(action="review", plan_id="p", review_kind="unassigned",
                                      start_s=1.0, end_s=2.0)
    assert result["kind"] == "unassigned"
    assert (result["start_s"], result["end_s"]) == (1.0, 2.0)


def test_pair_review_follows_same_events_after_edit_and_exposes_unpaired(tmp_path, monkeypatch):
    from reaper_audio import mcp_server
    from reaper_audio.daw import timing_service as timing

    snapshot = {"revision": {"project_id": "p"}, "tracks": []}
    for key, position in [("lead", 10.0), ("double", 10.04)]:
        source = tmp_path / f"{key}.wav"
        source.write_bytes(b"fixture")
        analysis = {"analysis_id": key, "project_id": "p", "track_guid": key,
                    "source_path": str(source),
                    "source_fingerprint": list(timing._file_fingerprint(source)),
                    "events": [{"id": "a", "source_s": 0.2, "project_s": position+0.2},
                               {"id": "b", "source_s": 0.7, "project_s": position+0.7}]}
        monkeypatch.setitem(timing.ANALYSES, key, analysis)
        snapshot["tracks"].append({"guid": key, "items": [
            {"guid": key + "-edited", "position_s": 10.01 if key == "double" else 10.0,
             "start_offset_s": 0.0, "length_s": 1.0, "active_take": {"source": str(source)}}]})

    class Session:
        def snapshot(self, **kwargs):
            return snapshot

    monkeypatch.setattr(mcp_server, "_session", lambda *args: Session())
    args = dict(action="pairs", reference_analysis_id="lead", analysis_id="double",
                event_pairs=[{"reference_event_id": "a", "event_id": "a"}],
                start_s=10.0, end_s=11.1)
    report = mcp_server.reaper_timing(**args)
    assert report["verified_pair_count"] == 1
    assert report["unpaired_counts"] == [1, 1]
    row, = report["rows"]
    assert row["original_delta_s"] == pytest.approx(0.04)
    assert row["live_delta_s"] == pytest.approx(0.01)

    snapshot["tracks"][1]["items"].append({
        **snapshot["tracks"][1]["items"][0], "guid": "duplicate", "position_s": 10.1})
    report = mcp_server.reaper_timing(**args)
    assert report["unverified_pair_count"] == 1
    assert len(report["rows"][0]["take_occurrences"]) == 2
    assert report["rows"][0]["live_delta_s"] is None
