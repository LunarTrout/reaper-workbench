"""Window review must surface the facts a single median cannot show."""

import statistics

import pytest

from reaper_audio.daw.timing_service import review_window
from reaper_audio.errors import ValidationError


def _plan():
    # Two attacks inside one seam pair, then a third one after a second seam.
    # The middle attack is deliberately late by 24 ms while the batch median
    # looks healthy, and it is covered by two overlapping clips.
    return {
        "method": "crossfade",
        "item_position_s": 10.0,
        "source_offset_s": 100.0,
        "item_length_s": 2.0,
        "duration_s": 130.0,
        "blocks": [
            {"position_s": 10.0, "source_start_s": 100.0, "length_s": 0.52,
             "fade_in_s": 0.0, "fade_out_s": 0.01, "event_ids": ["a1"]},
            {"position_s": 10.5, "source_start_s": 100.5, "length_s": 0.74,
             "fade_in_s": 0.01, "fade_out_s": 0.01, "event_ids": ["a2"]},
            {"position_s": 11.1, "source_start_s": 101.1, "length_s": 0.9,
             "fade_in_s": 0.01, "fade_out_s": 0.0, "event_ids": ["a3"]},
        ],
        "seams": [10.5, 11.1],
        "anchors": [
            {"id": "a1", "project_s": 10.20, "target_s": 10.1928, "confidence": 1.0},
            {"id": "a2", "project_s": 10.515, "target_s": 10.4910, "confidence": 1.0},
            {"id": "a3", "project_s": 11.30, "target_s": 11.2920, "confidence": 0.9},
        ],
    }


def _dense_plan():
    # Four healthy notes and one 22 ms late note: the median hides the late one.
    return {
        "method": "crossfade",
        "item_position_s": 0.0,
        "source_offset_s": 0.0,
        "item_length_s": 2.0,
        "duration_s": 10.0,
        "blocks": [
            {"position_s": 0.0, "source_start_s": 0.0, "length_s": 0.8,
             "fade_in_s": 0.0, "fade_out_s": 0.02, "event_ids": ["e1", "e2"]},
            {"position_s": 0.75, "source_start_s": 0.5, "length_s": 0.9,
             "fade_in_s": 0.02, "fade_out_s": 0.01, "event_ids": ["e3", "e4"]},
            {"position_s": 1.6, "source_start_s": 1.4, "length_s": 0.4,
             "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": ["e5"]},
        ],
        "seams": [0.75, 1.6],
        "anchors": [
            {"id": "e1", "project_s": 0.20, "target_s": 0.198, "confidence": 1.0},
            {"id": "e2", "project_s": 0.42, "target_s": 0.414, "confidence": 1.0},
            {"id": "e3", "project_s": 0.60, "target_s": 0.596, "confidence": 1.0},
            {"id": "e4", "project_s": 0.90, "target_s": 0.878, "confidence": 1.0},
            {"id": "e5", "project_s": 1.70, "target_s": 1.696, "confidence": 1.0},
        ],
    }


def _shuffled_plan():
    # Same geometry as _plan but blocks arrive out of project order and carry
    # distinct fades, so a seam must pick neighbours by position, not by list order.
    plan = _plan()
    plan["blocks"] = [
        {"position_s": 11.1, "source_start_s": 101.1, "length_s": 0.9,
         "fade_in_s": 0.03, "fade_out_s": 0.0, "event_ids": ["a3"]},
        {"position_s": 10.5, "source_start_s": 100.5, "length_s": 0.74,
         "fade_in_s": 0.02, "fade_out_s": 0.01, "event_ids": ["a2"]},
        {"position_s": 10.0, "source_start_s": 100.0, "length_s": 0.52,
         "fade_in_s": 0.0, "fade_out_s": 0.04, "event_ids": ["a1"]},
    ]
    return plan


def test_single_late_attack_is_visible_while_median_is_small():
    view = review_window(_plan(), start_s=10.0, end_s=11.4)
    rows = {row["event_id"]: row for row in view["attacks"]}
    residuals = sorted(abs(row["residual_s"]) for row in view["attacks"])
    median = residuals[len(residuals) // 2]
    assert median <= 0.012              # the batch looks fine
    assert 0.020 <= rows["a2"]["residual_s"] <= 0.024   # yet one note is not
    assert rows["a2"]["in_window"] is True


def test_one_22ms_residual_survives_a_median_below_12ms():
    view = review_window(_dense_plan(), start_s=0.0, end_s=2.0)
    residuals = [abs(row["residual_s"]) for row in view["attacks"]]
    assert statistics.median(residuals) <= 0.012
    late = next(row for row in view["attacks"] if row["event_id"] == "e4")
    assert 0.020 <= late["residual_s"] <= 0.024
    assert late["in_window"] is True


def test_attack_inside_two_clips_reports_both_occurrences_and_its_fade():
    view = review_window(_plan(), start_s=10.0, end_s=11.4)
    row = next(r for r in view["attacks"] if r["event_id"] == "a2")
    assert row["duplicated"] is True
    assert len(row["occurrences"]) == 2
    assert [item["block_index"] for item in row["occurrences"]] == [0, 1]
    assert row["occurrences"][0]["fade_attenuation"] == pytest.approx(0.5)
    assert row["occurrences"][1]["fade_attenuation"] == pytest.approx(0.0)
    assert row["under_fade"] is True


def test_seam_rows_carry_cut_position_fades_and_remaining_source_audio():
    view = review_window(_plan(), start_s=10.0, end_s=11.4)
    assert view["seam_count"] == 2
    first = view["seams"][0]
    assert first["project_s"] == pytest.approx(10.5)
    assert first["cut_source_s"] == pytest.approx(100.5)
    assert first["fade_out_s"] == pytest.approx(0.01)
    assert first["fade_in_s"] == pytest.approx(0.01)
    # 130 s of source, the previous clip ends at 100.52 s -> plenty left
    assert first["source_audio_left_s"] == pytest.approx(130.0 - 100.52)
    assert first["edge_source_exhausted"] is False
    second = view["seams"][1]
    assert second["project_s"] == pytest.approx(11.1)
    assert second["cut_source_s"] == pytest.approx(101.1)
    assert second["fade_out_s"] == pytest.approx(0.01)
    assert second["fade_in_s"] == pytest.approx(0.01)
    assert second["source_audio_left_s"] == pytest.approx(130.0 - (100.5 + 0.74))
    assert second["edge_source_exhausted"] is False


def test_seam_flags_exhausted_source_instead_of_a_negative_remainder():
    plan = {
        "method": "crossfade",
        "item_position_s": 0.0,
        "source_offset_s": 10.0,
        "item_length_s": 1.0,
        "duration_s": 10.0,
        "blocks": [
            {"position_s": 0.0, "source_start_s": 10.0, "length_s": 0.5,
             "fade_in_s": 0.0, "fade_out_s": 0.01, "event_ids": []},
            {"position_s": 0.5, "source_start_s": 10.5, "length_s": 0.5,
             "fade_in_s": 0.01, "fade_out_s": 0.0, "event_ids": []},
        ],
        "seams": [0.5],
        "anchors": [],
    }
    seam = review_window(plan, start_s=0.0, end_s=1.0)["seams"][0]
    assert seam["source_audio_left_s"] == 0.0
    assert seam["edge_source_exhausted"] is True


def test_seam_neighbours_are_chosen_by_position_not_list_order():
    view = review_window(_shuffled_plan(), start_s=10.0, end_s=11.4)
    first, second = view["seams"]
    assert first["fade_in_s"] == pytest.approx(0.02)
    assert first["fade_out_s"] == pytest.approx(0.04)
    assert first["source_audio_left_s"] == pytest.approx(130.0 - 100.52)
    assert second["fade_in_s"] == pytest.approx(0.03)
    assert second["fade_out_s"] == pytest.approx(0.01)
    assert second["source_audio_left_s"] == pytest.approx(130.0 - (100.5 + 0.74))


def test_seam_without_a_previous_clip_reports_unknown_edge_audio():
    plan = {
        "method": "crossfade",
        "item_position_s": 0.0,
        "source_offset_s": 0.0,
        "item_length_s": 1.0,
        "duration_s": 10.0,
        "blocks": [
            {"position_s": 0.0, "source_start_s": 0.0, "length_s": 1.0,
             "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": []},
        ],
        "seams": [0.0],
        "anchors": [],
    }
    seam = review_window(plan, start_s=0.0, end_s=1.0)["seams"][0]
    assert seam["fade_out_s"] is None
    assert seam["source_audio_left_s"] is None
    assert seam["edge_source_exhausted"] is False


def test_source_project_and_musical_slots_are_reported_separately():
    view = review_window(_plan(), start_s=10.0, end_s=11.4)
    row = next(r for r in view["attacks"] if r["event_id"] == "a1")
    # 10.20 in the project is 100.20 in the file for this item
    assert row["source_s"] == pytest.approx(100.20)
    assert row["project_s"] == pytest.approx(10.20)
    assert row["target_s"] == pytest.approx(10.1928)
    assert row["residual_s"] == pytest.approx(10.20 - 10.1928)


def test_duplicated_attack_outside_both_fades_is_not_under_fade():
    plan = {
        "method": "crossfade",
        "item_position_s": 0.0,
        "source_offset_s": 5.0,
        "item_length_s": 1.5,
        "duration_s": 20.0,
        "blocks": [
            {"position_s": 0.0, "source_start_s": 5.0, "length_s": 1.0,
             "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": ["e1"]},
            {"position_s": 0.4, "source_start_s": 5.0, "length_s": 1.0,
             "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": ["e1"]},
        ],
        "seams": [],
        "anchors": [{"id": "e1", "project_s": 0.7, "target_s": 0.7, "confidence": 0.8}],
    }
    view = review_window(plan, start_s=0.0, end_s=1.5)
    row = view["attacks"][0]
    assert row["duplicated"] is True
    assert len(row["occurrences"]) == 2
    assert row["under_fade"] is False
    assert all(item["fade_attenuation"] == 0.0 for item in row["occurrences"])


def test_attack_rows_carry_residual_blockers_from_the_plan_review():
    plan = _plan()
    plan["review"] = {"residual": {"rows": [
        {"event_id": "a2", "fixed_by": ["range_a", "range_b"]},
        {"event_id": "a2", "fixed_by": ["range_b", "range_c"]},
        {"event_id": "a1", "fixed_by": []},
        {"event_id": "gone", "fixed_by": ["range_d"]},
    ]}}
    rows = {row["event_id"]: row for row in review_window(
        plan, start_s=10.0, end_s=11.4)["attacks"]}
    assert rows["a2"]["blockers"] == ["range_a", "range_b", "range_c"]
    assert rows["a1"]["blockers"] == []
    assert rows["a3"]["blockers"] == []


def test_attack_rows_without_review_rows_report_empty_blockers():
    view = review_window(_plan(), start_s=10.0, end_s=11.4)
    assert all(row["blockers"] == [] for row in view["attacks"])


def test_envelope_is_omitted_when_source_audio_is_unavailable():
    view = review_window(_plan(), start_s=10.0, end_s=11.4)
    for row in view["attacks"]:
        assert "envelope_db" not in row
        assert row["envelope_unavailable"] is True


def test_envelope_is_omitted_when_the_named_file_cannot_be_loaded():
    plan = _plan()
    plan["source_path"] = "does-not-exist-review-window.wav"
    view = review_window(plan, start_s=10.0, end_s=11.4)
    assert all(row.get("envelope_unavailable") is True for row in view["attacks"])
    assert all("envelope_db" not in row for row in view["attacks"])


def test_envelope_reports_rms_around_the_source_attack(tmp_path):
    import wave

    import numpy as np

    rate = 44100
    time = np.arange(rate, dtype=np.float64) / rate
    samples = np.zeros(rate, dtype=np.float64)
    samples[time >= 0.5] = 0.5 * np.sin(2.0 * np.pi * 440.0 * time[time >= 0.5])
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    path = tmp_path / "source.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())
    plan = {
        "method": "crossfade",
        "item_position_s": 0.0,
        "source_offset_s": 0.0,
        "item_length_s": 1.0,
        "duration_s": 1.0,
        "source_path": str(path),
        "blocks": [
            {"position_s": 0.0, "source_start_s": 0.0, "length_s": 1.0,
             "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": ["e1"]},
        ],
        "seams": [],
        "anchors": [{"id": "e1", "project_s": 0.51, "target_s": 0.51,
                     "confidence": 1.0}],
    }
    row = review_window(plan, start_s=0.0, end_s=1.0)["attacks"][0]
    envelope = row["envelope_db"]
    assert "envelope_unavailable" not in row
    assert len(envelope) == 41                 # +/-40 ms sampled every 2 ms
    assert all(isinstance(value, float) for value in envelope)
    assert max(envelope) > -20.0               # half-amplitude sine is about -9 dBFS
    assert min(envelope) < -60.0               # silence before the attack


def test_window_rejects_bad_intervals_and_non_crossfade_plans():
    with pytest.raises(ValidationError):
        review_window(_plan(), start_s=11.0, end_s=11.0)
    with pytest.raises(ValidationError):
        review_window(_plan(), start_s=11.0, end_s=10.0)
    with pytest.raises(ValidationError):
        review_window({"method": "other"}, start_s=1.0, end_s=2.0)
