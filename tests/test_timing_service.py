from __future__ import annotations

from pathlib import Path

import numpy as np
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
