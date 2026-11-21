from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from reaper_audio.analysis.timing_rhythm import infer_rhythm
from reaper_audio.daw.midi import TempoMap
from reaper_audio.daw.timing_apply import check_coverage, check_protected_ranges, normalize_clips
from reaper_audio.daw.timing_service import (
    build_timing_rhythm, plan_timing_crossfades, read_timing_rhythm,
)
from reaper_audio.errors import ValidationError

RATE = 16_000
TEMPO = {"bpm": 120, "numerator": 4, "denominator": 4, "markers": []}
TEMPO_MAP = TempoMap.from_context(TEMPO)
TRUE_QN = [.0, .25, .5, .75, 1., 1+1/3, 1+2/3, 2., 2.25, 2.5, 2.75, 3.]
OFFSETS = [.009, -.010, .021, -.010, .003, .014, -.015, .013, -.014, .020, -.006, .004]


def _performance():
    samples = np.zeros(RATE * 2)
    events = []
    for index, (qn, shift) in enumerate(zip(TRUE_QN, OFFSETS)):
        time = .5*qn+shift
        f = int(time*RATE)
        wave = .5 * np.sin(2*np.pi*220*np.arange(480)/RATE)
        samples[f:f+len(wave)] = wave
        events.append({"id": f"e{index}", "source_s": time, "project_s": time,
                       "confidence": .91, "novelty": 1.8, "prominence": .7,
                       "energy_jump": 1.})
    return samples, events


def test_mixed_subdivisions_and_measured_release_stay_distinct():
    samples, events = _performance()
    out = infer_rhythm(SimpleNamespace(samples=samples, sample_rate=RATE),
                       events, TEMPO_MAP, start_s=0., end_s=2., source_origin=0.,
                       tolerance_s=.046, profile="picked")
    assert [s["options"][0]["division_per_beat"] for s in out[:3]] == [4, 3, 4]
    assert out[3]["status"] == "ambiguous"
    assert out[3]["recommended_option"] is None
    assert len({a["event_id"] for s in out for a in s["observations"]}) == len(events)
    first = out[0]["observations"][0]
    assert first["sound_end_s"] < .06
    assert first["next_candidate_gap_s"] > .09
    assert out[0]["options"][0]["anchors"][0]["qn_to_next_anchor"] == .25
    assert out[0]["similar_grid_patterns"] == ["bar_1_beat_3"]


def test_rhythm_plan_retains_pins_and_explicit_targets(tmp_path, monkeypatch):
    samples, events = _performance()
    path = tmp_path / "test-take.wav"
    sf.write(path, samples, RATE, subtype="FLOAT")
    stat = path.stat()
    analysis = {"method": "crossfade", "project_id": "project", "track_guid": "track",
                "item_guid": "item", "take_guid": "take", "position_s": 0.,
                "source_offset_s": 0., "item_length_s": 2., "edge_fades_s": [0., 0.],
                "source_fingerprint": [stat.st_size, stat.st_mtime_ns],
                "source_path": str(path), "duration_s": 2., "events": events,
                "project_window": {"start_s": 0., "end_s": 2.}, "profile": "picked",
                "take_count": 1, "take_fx_count": 0}
    snapshot = {"revision": {"project_id": "project"}, "tempo": TEMPO,
                "tracks": [{"guid": "track", "items": [{"guid": "item",
                            "position_s": 0., "start_offset_s": 0., "length_s": 2.,
                            "active_take": {"guid": "take", "source": str(path)}}]}]}
    monkeypatch.setattr("reaper_audio.daw.timing_service.ANALYSES", {"analysis": analysis})
    monkeypatch.setattr("reaper_audio.daw.timing_service.PLANS", {})
    monkeypatch.setattr("reaper_audio.daw.timing_service.RHYTHMS", {})
    rid = build_timing_rhythm("analysis", snapshot=snapshot)
    first_page = read_timing_rhythm(rid, limit=1)
    assert first_page["next_offset"] == 1
    assert read_timing_rhythm(rid, offset=1, limit=1)["segments"][0]["id"] == "bar_1_beat_2"
    kwargs = {"tempo_context": TEMPO, "subdivision": 4, "tolerance_s": .046,
              "strength": 1., "project_id": "project"}
    plan = plan_timing_crossfades("analysis", rhythm_id=rid,
                                 rhythm_choices={"bar_1_beat_2": "option_1"}, **kwargs)
    assert plan["target_kind"] == "rhythm_map"
    assert plan["rhythm_choices"]["bar_1_beat_4"] == "keep"
    assert not any(a["id"] == "e11" for a in plan["anchors"])
    assert {a["id"] for a in plan["anchors"]} >= {"e4", "e5", "e6"}
    assert len(plan["blocks"]) > 1
    clips = normalize_clips(plan["blocks"], project_start_s=0.)
    item = {"position_s": 0., "start_offset_s": 0., "length_s": 2.}
    assert check_coverage(clips, item, crossfade=True)["target_gap_count"] == 0
    kept = check_protected_ranges(clips, item, plan["protected_ranges"], project_start_s=0.)
    assert kept[0]["state"] == "kept"
    drifted = {**TEMPO, "bpm": 125}
    with pytest.raises(ValidationError, match="Tempo map or phase"):
        plan_timing_crossfades("analysis", rhythm_id=rid, **{**kwargs, "tempo_context": drifted})
    with pytest.raises(ValidationError, match="preserve attack order"):
        plan_timing_crossfades("analysis", reference_events=[
            {"event_id": "e0", "target_s": .02},
            {"event_id": "e1", "target_s": .02},
        ], **{**kwargs, "tolerance_s": .15})
    snapshot["tracks"][0]["items"][0]["length_s"] = 1.75
    with pytest.raises(ValidationError, match="source changed"):
        build_timing_rhythm("analysis", snapshot=snapshot)


def test_vocal_observations_can_show_pitch_motion_inside_one_event():
    t = np.arange(RATE)/RATE
    pitch = np.where(t < .22, 220., 330.)
    samples = .2*np.sin(2*np.pi*np.cumsum(pitch)/RATE)
    samples[int(.44*RATE):] = 0.
    event = {"id": "voice", "project_s": .01, "confidence": .95}
    beats = infer_rhythm(SimpleNamespace(samples=samples, sample_rate=RATE), [event],
                         TEMPO_MAP, start_s=0., end_s=.5, source_origin=0., profile="vocal")
    observed = beats[0]["observations"][0]
    assert observed["pitch_observations"]["early"]["hz"] == pytest.approx(220, abs=5)
    assert observed["pitch_observations"]["late"]["hz"] == pytest.approx(330, abs=8)
    assert observed["sound_end_s"] == pytest.approx(.44, abs=.02)
    assert beats[0]["recommended_option"] is None
