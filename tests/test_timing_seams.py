
from types import SimpleNamespace

import numpy as np
import pytest

from reaper_audio.analysis.timing_seams import _seam, plan_crossfades
from reaper_audio.daw.timing_apply import check_coverage, normalize_clips
from reaper_audio.errors import ValidationError


def test_crossfade_plan_keeps_item_extent_and_pairs_every_fade():
    rate = 4000
    audio = SimpleNamespace(samples=np.zeros((2 * rate, 1)), sample_rate=rate)
    targets = np.arange(.2, 1.61, .1)
    attacks = targets + np.resize([.018, -.016, .012, -.021], len(targets))
    events = [{"id": f"a{i}", "source_s": attack, "project_s": attack,
               "confidence": 1., "energy_jump": 1.}
              for i, attack in enumerate(attacks)]
    plan = plan_crossfades(audio, events, np.arange(0., 2.01, .1),
                           position=0., offset=0., length=2., tolerance=.045,
                           strength=1., profile="picked")
    clips = normalize_clips(plan["blocks"], project_start_s=0.)
    assert len(clips) > 2
    item = {"position_s": 0., "start_offset_s": 0., "length_s": 2.}
    receipt = check_coverage(clips, item, crossfade=True)
    assert receipt["target_gap_count"] == 0
    assert plan["median_error_after_s"] < plan["median_error_before_s"]
    # Deliberately break one paired fade. The receipt must fail before REAPER.
    plan["blocks"][1]["fade_in_s"] = 0.
    with pytest.raises(ValidationError):
        check_coverage(normalize_clips(plan["blocks"], project_start_s=0.),
                       item, crossfade=True)


def test_seam_refuses_an_independent_attack_in_the_candidate_window():
    samples = np.zeros((8000, 2))
    assert _seam(samples, 4000, 0., 0., .008, .25, .31,
                 np.arange(.25, .32, .002), .013) is None
