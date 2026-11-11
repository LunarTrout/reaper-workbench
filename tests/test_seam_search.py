import numpy as np

from reaper_audio.analysis.seam_search import assemble_variant, search_variants


def _tone(seconds=0.6, sr=48000):
    t = np.arange(int(seconds * sr)) / sr
    return (0.2 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)


def test_variant_rejects_a_gap_when_right_starts_after_left_ends():
    try:
        assemble_variant(_tone(), 0.2, 48000, left_extend_s=0.01,
                         move_s=0.0, right_trim_s=0.02)
    except ValueError as exc:
        assert "gap" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_seam_is_measured_at_the_moved_right_item_start():
    variant = assemble_variant(_tone(), 0.2, 48000, left_extend_s=0.08,
                               move_s=0.03, right_trim_s=0.0)
    assert variant["seam_position_s"] == 0.23
    assert variant["crossfade_end_s"] == 0.28
    assert variant["overlap_s"] == 0.05
    assert variant["metrics"]["level_delta_db"] > -20
    assert set(variant["metrics"]["boundaries"]) == {
        "overlap_start", "overlap_end"}
    boundaries = variant["metrics"]["boundaries"]
    assert variant["metrics"]["max_sample_step"] == max(
        boundaries["overlap_start"]["max_sample_step"],
        boundaries["overlap_end"]["max_sample_step"])
    assert variant["metrics"]["spectral_flux"] == max(
        boundaries["overlap_start"]["spectral_flux"],
        boundaries["overlap_end"]["spectral_flux"])
    assert abs(variant["metrics"]["level_delta_db"]) == max(
        abs(boundaries["overlap_start"]["level_delta_db"]),
        abs(boundaries["overlap_end"]["level_delta_db"]))


def test_reported_live_candidate_has_same_geometry_as_split_parameters():
    variant = assemble_variant(
        _tone(seconds=3.0), 1.078, 48000,
        left_extend_s=0.130, move_s=0.100, right_trim_s=0.025)
    assert variant["seam_position_s"] == 1.203
    assert variant["crossfade_end_s"] == 1.208
    assert variant["overlap_s"] == 0.005


def test_variant_rejects_overlap_longer_than_available_right_audio():
    try:
        assemble_variant(_tone(seconds=0.3), 0.2, 48000,
                         left_extend_s=0.09, move_s=-0.08)
    except ValueError as exc:
        assert "available right-side" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_search_returns_ranked_measured_variants():
    result = search_variants(_tone(), 0.2, 48000, steps=3)
    assert result["variants"]
    assert result["best"]["score"] <= result["variants"][-1]["score"]
    assert 0.0 < result["best"]["seam_position_s"] < 1.0
    assert "decision" in result


def test_stereo_source_reports_channels_and_phase():
    tone = _tone()
    stereo = np.stack([tone, 0.5 * tone], axis=1)
    variant = assemble_variant(stereo, 0.2, 48000, left_extend_s=0.08,
                               move_s=0.02)
    assert variant["audio"].ndim == 2
    assert len(variant["metrics"]["channels"]) == 2
    assert variant["metrics"]["phase_correlation"] > 0.9
    assert len(variant["metrics"]["end_channels"]) == 2


def test_tail_bounds_the_search():
    from reaper_audio.analysis.seam_search import search_with_tail
    sr = 48000
    t = np.arange(int(1.0 * sr)) / sr
    decay = (0.2 * np.exp(-t / 0.1) *
             np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
    result = search_with_tail(decay, 0.1, sr, drop_db=20.0,
                              max_tail_s=1.0, max_left_extend_cap_s=0.5,
                              steps=3)
    assert result["tail_evidence"]["tail_s"] > 0.0
    assert result["max_left_extend_s"] <= 0.5
    assert result["variants"]
