import numpy as np

from reaper_audio.analysis.seam import (crossfade, equal_power_crossfade,
                                        rms_db, seam_metrics)


def test_seam_metrics_flag_a_gap():
    sr = 48000
    t = np.arange(int(0.2 * sr)) / sr
    tone = 0.2 * np.sin(2 * np.pi * 220.0 * t).astype(np.float32)
    gap = np.zeros_like(tone)
    metrics = seam_metrics(tone, gap, sr)
    assert metrics["level_delta_db"] < -60
    assert metrics["interpretation"]


def test_equal_power_crossfade_has_no_click_for_a_continuous_sine():
    sr = 48000
    t = np.arange(int(0.3 * sr)) / sr
    tone = 0.2 * np.sin(2 * np.pi * 220.0 * t).astype(np.float32)
    cut = int(0.2 * sr)
    extra = int(0.02 * sr)
    # A split extends the LEFT item past the cut, so its tail and the right
    # item's head are the SAME source region. That is the correlated case.
    left = tone[:cut + extra]
    right = tone[cut:]
    blended = crossfade(left, right, 0.02, sr, curve="linear")
    assert blended.dtype == np.float32
    assert len(blended) == len(left) + len(right) - int(0.02 * sr)
    metrics = seam_metrics(blended[cut - int(0.02 * sr):cut],
                           blended[cut:cut + int(0.02 * sr)], sr)
    assert abs(metrics["level_delta_db"]) < 2.0
    assert metrics["max_sample_step"] < 0.1


def test_rms_db_floor():
    assert rms_db(np.zeros(16)) == -120.0
