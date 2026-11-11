import numpy as np

from reaper_audio.analysis.tail import estimate_tail


def _decay(seconds=1.5, sr=48000, tau=0.2):
    t = np.arange(int(seconds * sr)) / sr
    envelope = np.exp(-t / tau)
    return (0.2 * envelope * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)


def test_estimates_a_real_decay_tail():
    result = estimate_tail(_decay(), 0.0, 48000, drop_db=20.0,
                           max_tail_s=1.0, hop_s=0.01)
    # 20 dB below peak at tau=0.2 s is about 0.46 s.
    assert 0.35 <= result["tail_s"] <= 0.55
    assert result["capped"] is False


def test_marks_a_tail_that_hits_the_window_as_capped():
    result = estimate_tail(_decay(), 0.0, 48000, drop_db=40.0,
                           max_tail_s=0.1, hop_s=0.01)
    assert result["tail_s"] == 0.1
    assert result["capped"] is True


def test_accepts_stereo_and_rejects_bad_onset():
    stereo = np.stack([_decay(), _decay()], axis=1)
    assert estimate_tail(stereo, 0.0, 48000)["tail_s"] > 0
    try:
        estimate_tail(_decay(), 9.0, 48000)
    except ValueError as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("expected ValueError")
