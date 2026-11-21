"""Signal-level checks for an applied timing plan.

Every signal here is synthetic and every attack sits on a known sample, so the
expected times are arithmetic, not measurement.
"""

from __future__ import annotations

import numpy as np
import pytest

from reaper_audio.daw.timing_verify import (conflict_report, measure_attacks,
                                            render_plan, verify_plan)

RATE = 48_000


def _signal(duration_s: float, clicks=(), *, amplitude: float = 0.9,
            width_s: float = 0.0015) -> np.ndarray:
    samples = np.zeros(int(round(duration_s * RATE)), dtype=np.float64)
    width = max(1, int(round(width_s * RATE)))
    for time_s in clicks:
        at = int(round(time_s * RATE))
        samples[at:at + width] = amplitude
    return samples


def _hit(duration_s, time_s, decay_s=0.012, amplitude=0.8, rise_s=0.0):
    """One decaying attack: a fast rise, then an exponential tail."""
    samples = np.zeros(int(round(duration_s * RATE)), dtype=np.float64)
    at = int(round(time_s * RATE))
    length = min(len(samples) - at, max(1, int(round(decay_s * 7.0 * RATE))))
    index = np.arange(length, dtype=np.float64)
    shape = np.exp(-index / max(1.0, decay_s * RATE))
    if rise_s > 0.0:
        ramp = int(round(rise_s * RATE))
        if ramp > 0:
            shape[:ramp] *= np.linspace(0.0, 1.0, ramp, endpoint=False)
    samples[at:at + length] = amplitude * shape
    return samples


def _moved_plan() -> dict:
    """An item at 1.5 s whose second block shifts source 0.30 -> 1.825 s."""
    return {
        "schema": "reaper-workbench.timing-plan/1",
        "method": "crossfade",
        "item_position_s": 1.5,
        "source_offset_s": 0.0,
        "item_length_s": 0.8,
        "duration_s": 1.0,
        "blocks": [
            {"position_s": 1.5, "source_start_s": 0.0, "length_s": 0.285,
             "rate": 1.0, "fade_in_s": 0.0, "fade_out_s": 0.010,
             "event_ids": ["a1", "a2"]},
            {"position_s": 1.775, "source_start_s": 0.25, "length_s": 0.525,
             "rate": 1.0, "fade_in_s": 0.010, "fade_out_s": 0.0,
             "event_ids": ["a3"]},
        ],
        "anchors": [
            {"id": "a1", "project_s": 1.60, "target_s": 1.60},
            {"id": "a2", "project_s": 1.80, "target_s": 1.825},
            {"id": "a3", "project_s": 2.00, "target_s": 2.025},
        ],
        "seams": [1.775],
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_plan_applies_linear_fades_and_leaves_silence():
    source = np.ones(int(round(0.4 * RATE)), dtype=np.float64)
    plan = {
        "item_position_s": 0.0, "source_offset_s": 0.0, "item_length_s": 0.4,
        "blocks": [
            {"position_s": 0.0, "source_start_s": 0.0, "length_s": 0.2,
             "fade_in_s": 0.010, "fade_out_s": 0.010, "event_ids": []},
            {"position_s": 0.190, "source_start_s": 0.190, "length_s": 0.110,
             "fade_in_s": 0.010, "fade_out_s": 0.0, "event_ids": []},
        ],
        "anchors": [], "seams": [0.190],
    }
    buffer, rate = render_plan(plan, source, sample_rate=RATE)
    assert rate == RATE
    assert len(buffer) == int(round(0.4 * RATE))
    assert buffer[0] == 0.0
    assert buffer[240] == pytest.approx(0.5, abs=1e-9)
    assert buffer[480] == pytest.approx(1.0, abs=1e-9)
    assert buffer[int(round(0.15 * RATE))] == pytest.approx(1.0, abs=1e-9)
    # Complementary 10 ms fades sum to unity through the overlap.
    for time_s in (0.190, 0.195, 0.1999):
        assert buffer[int(round(time_s * RATE))] == pytest.approx(1.0, abs=2e-3)
    # The item window extends past the blocks: that part is silent.
    assert np.all(buffer[int(round(0.31 * RATE)):] == 0.0)


def test_render_plan_mixes_overlapping_blocks_additively():
    source = np.ones(int(round(0.2 * RATE)), dtype=np.float64)
    plan = {
        "item_position_s": 0.0, "item_length_s": 0.1,
        "blocks": [
            {"position_s": 0.0, "source_start_s": 0.0, "length_s": 0.1,
             "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": []},
            {"position_s": 0.0, "source_start_s": 0.05, "length_s": 0.1,
             "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": []},
        ],
    }
    buffer, _ = render_plan(plan, source, sample_rate=RATE)
    assert buffer[1000] == pytest.approx(2.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Attack measurement
# ---------------------------------------------------------------------------


def test_plan_moves_an_attack_and_measurement_follows_it():
    source = _signal(1.0, [0.10, 0.30, 0.50])
    plan = _moved_plan()
    buffer, rate = render_plan(plan, source, sample_rate=RATE)
    rows = measure_attacks(plan, buffer, sample_rate=rate)

    assert [row["event_id"] for row in rows] == ["a1", "a2", "a3"]
    moved = rows[1]
    assert moved["found"] is True
    assert moved["measured_s"] == pytest.approx(1.825, abs=0.002)
    assert abs(moved["measured_s"] - 1.80) > 0.020
    assert abs(moved["error_ms"]) <= 2.0
    assert rows[0]["measured_s"] == pytest.approx(1.60, abs=0.002)
    assert rows[2]["measured_s"] == pytest.approx(2.025, abs=0.002)
    assert moved["duplicate_nearby"] is False
    assert moved["seam_front"] is False


def test_verify_plan_summarizes_found_attacks_and_clean_seams():
    source = _signal(1.0, [0.10, 0.30, 0.50])
    result = verify_plan(_moved_plan(), source, sample_rate=RATE)

    assert result["summary"] == {"found": 3, "missing": 0,
                                 "duplicate_geometry": 0, "signal_echo": 0,
                                 "seam_fronts": 0}
    assert len(result["seams"]) == 1
    seam = result["seams"][0]
    assert seam["position_s"] == pytest.approx(1.775)
    assert seam["front_detected"] is False
    assert seam["level_db_delta"] == pytest.approx(0.0, abs=0.1)


def test_attack_outside_the_search_window_is_reported_missing():
    source = _signal(0.6, [0.30])
    plan = {
        "item_position_s": 0.0, "item_length_s": 0.5,
        "blocks": [{"position_s": 0.0, "source_start_s": 0.0, "length_s": 0.5,
                    "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": ["late"]}],
        "anchors": [{"id": "late", "project_s": 0.30, "target_s": 0.40}],
        "seams": [],
    }
    buffer, rate = render_plan(plan, source, sample_rate=RATE)
    rows = measure_attacks(plan, buffer, sample_rate=rate, search_s=0.060)
    assert rows[0]["found"] is False
    assert rows[0]["measured_s"] is None
    assert rows[0]["error_ms"] is None

    wider = measure_attacks(plan, buffer, sample_rate=rate, search_s=0.120)
    assert wider[0]["found"] is True
    assert wider[0]["measured_s"] == pytest.approx(0.30, abs=0.002)
    assert wider[0]["error_ms"] == pytest.approx(-100.0, abs=2.0)

    assert verify_plan(plan, source, sample_rate=RATE)["summary"]["missing"] == 1


def test_duplicate_attack_inside_an_overlap_is_flagged():
    # Block 2 re-reads source 0.48.., so the attack at 0.49 plays twice:
    # at 0.49 (block 1) and at 0.51 (block 2, half way into its fade-in).
    source = _hit(1.0, 0.49)
    plan = {
        "item_position_s": 0.0, "source_offset_s": 0.0, "item_length_s": 0.72,
        "blocks": [
            {"position_s": 0.0, "source_start_s": 0.0, "length_s": 0.52,
             "fade_in_s": 0.0, "fade_out_s": 0.020, "event_ids": ["dup"]},
            {"position_s": 0.50, "source_start_s": 0.48, "length_s": 0.22,
             "fade_in_s": 0.020, "fade_out_s": 0.0, "event_ids": []},
        ],
        "anchors": [{"id": "dup", "source_s": 0.49, "project_s": 0.49, "target_s": 0.49}],
        "seams": [0.50],
    }
    buffer, rate = render_plan(plan, source, sample_rate=RATE)
    rows = measure_attacks(plan, buffer, sample_rate=rate)

    assert rows[0]["found"] is True
    assert rows[0]["measured_s"] == pytest.approx(0.49, abs=0.002)
    assert rows[0]["duplicate_nearby"] is True
    result = verify_plan(plan, source, sample_rate=RATE)
    # Geometry is exact: source 0.49 is read by both blocks.
    assert result["summary"]["duplicate_geometry"] == 1


def test_seam_click_is_flagged_as_a_seam_front():
    # Block 1 ends on silence and block 2 starts on a click: a hard cut with
    # no crossfade, exactly the edit artifact the check exists for.
    source = _signal(0.8, [0.10, 0.60])
    plan = {
        "item_position_s": 0.0, "item_length_s": 0.5,
        "blocks": [
            {"position_s": 0.0, "source_start_s": 0.0, "length_s": 0.3,
             "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": ["a1"]},
            {"position_s": 0.3, "source_start_s": 0.6, "length_s": 0.2,
             "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": []},
        ],
        "anchors": [{"id": "a1", "project_s": 0.10, "target_s": 0.10}],
        "seams": [0.3],
    }
    result = verify_plan(plan, source, sample_rate=RATE)
    seam = result["seams"][0]
    assert seam["position_s"] == pytest.approx(0.3)
    assert seam["front_detected"] is True
    assert seam["level_db_delta"] > 20.0
    assert result["summary"]["seam_fronts"] == 1
    assert result["attacks"][0]["seam_front"] is False


def test_clean_seam_has_no_front_and_no_level_step():
    source = _signal(0.8, [0.10])
    plan = {
        "item_position_s": 0.0, "item_length_s": 0.5,
        "blocks": [
            {"position_s": 0.0, "source_start_s": 0.0, "length_s": 0.3,
             "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": ["a1"]},
            {"position_s": 0.3, "source_start_s": 0.6, "length_s": 0.2,
             "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": []},
        ],
        "anchors": [{"id": "a1", "project_s": 0.10, "target_s": 0.10}],
        "seams": [0.3],
    }
    result = verify_plan(plan, source, sample_rate=RATE)
    seam = result["seams"][0]
    assert seam["front_detected"] is False
    assert seam["level_db_delta"] == pytest.approx(0.0, abs=0.1)


def test_attack_expected_at_a_seam_is_not_a_seam_front():
    # The downbeat is meant to land on the cut; that front is expected and
    # must not be reported as a seam artifact.
    source = _signal(0.8, [0.10, 0.60])
    plan = {
        "item_position_s": 0.0, "item_length_s": 0.5,
        "blocks": [
            {"position_s": 0.0, "source_start_s": 0.0, "length_s": 0.3,
             "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": []},
            {"position_s": 0.3, "source_start_s": 0.6, "length_s": 0.2,
             "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": ["edge"]},
        ],
        "anchors": [{"id": "edge", "project_s": 0.3, "target_s": 0.3}],
        "seams": [0.3],
    }
    result = verify_plan(plan, source, sample_rate=RATE)
    assert result["attacks"][0]["found"] is True
    assert result["attacks"][0]["measured_s"] == pytest.approx(0.3, abs=0.002)
    assert result["attacks"][0]["seam_front"] is False
    assert result["seams"][0]["front_detected"] is False
    assert result["summary"]["seam_fronts"] == 0


# ---------------------------------------------------------------------------
# Geometry conflicts
# ---------------------------------------------------------------------------


def test_source_past_end_is_reported_and_no_audio_is_invented():
    time_s = np.arange(int(round(0.25 * RATE))) / RATE
    source = 0.5 * np.sin(2 * np.pi * 220.0 * time_s)
    plan = {
        "item_position_s": 0.0, "source_offset_s": 0.0, "item_length_s": 0.15,
        "blocks": [{"position_s": 0.0, "source_start_s": 0.20, "length_s": 0.15,
                    "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": []}],
        "anchors": [], "seams": [],
    }
    conflicts = conflict_report(plan, len(source) / RATE)
    past = [row for row in conflicts if row["code"] == "source_past_end"]
    assert len(past) == 1
    assert past[0]["excess_s"] == pytest.approx(0.10, abs=1e-6)

    buffer, _ = render_plan(plan, source, sample_rate=RATE)
    head = buffer[:int(round(0.045 * RATE))]
    tail = buffer[int(round(0.060 * RATE)):]
    assert np.sqrt(np.mean(head * head)) > 0.05
    assert np.all(tail == 0.0)


def test_silence_gaps_are_reported_as_conflicts():
    plan = {
        "item_position_s": 0.0, "source_offset_s": 0.0, "item_length_s": 0.4,
        "blocks": [
            {"position_s": 0.02, "source_start_s": 0.0, "length_s": 0.16,
             "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": []},
            {"position_s": 0.24, "source_start_s": 0.20, "length_s": 0.12,
             "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": []},
        ],
        "anchors": [], "seams": [],
    }
    gaps = [row for row in conflict_report(plan, 1.0) if row["code"] == "gap"]
    assert len(gaps) == 3
    intervals = [(row["start_s"], row["end_s"]) for row in gaps]
    for actual, wanted in zip(intervals, [(0.0, 0.02), (0.18, 0.24), (0.36, 0.4)]):
        assert actual[0] == pytest.approx(wanted[0], abs=1e-9)
        assert actual[1] == pytest.approx(wanted[1], abs=1e-9)
    assert sum(row["duration_s"] for row in gaps) == pytest.approx(0.12, abs=1e-9)


def test_attack_under_a_fade_reports_attenuation():
    plan = {
        "item_position_s": 0.0, "item_length_s": 0.5,
        "blocks": [{"position_s": 0.0, "source_start_s": 0.0, "length_s": 0.5,
                    "fade_in_s": 0.1, "fade_out_s": 0.1, "event_ids": []}],
        "anchors": [
            {"id": "buried", "project_s": 0.05, "target_s": 0.05},
            {"id": "clear", "project_s": 0.25, "target_s": 0.25},
        ],
        "seams": [],
    }
    faded = [row for row in conflict_report(plan, 1.0)
             if row["code"] == "attack_under_fade"]
    assert len(faded) == 1
    assert faded[0]["event_id"] == "buried"
    assert faded[0]["gain"] == pytest.approx(0.5, abs=1e-9)
    assert faded[0]["attenuation_db"] == pytest.approx(6.02, abs=0.01)


def test_block_shorter_than_four_milliseconds_is_reported():
    plan = {
        "item_position_s": 0.0, "item_length_s": 0.1,
        "blocks": [{"position_s": 0.0, "source_start_s": 0.0, "length_s": 0.003,
                    "fade_in_s": 0.003, "fade_out_s": 0.0, "event_ids": []}],
        "anchors": [], "seams": [],
    }
    report = conflict_report(plan, 1.0)
    short = [row for row in report if row["code"] == "block_too_short"]
    assert len(short) == 1
    assert short[0]["minimum_s"] == 0.004
    assert any(row["code"] == "gap" for row in report)


def test_overlap_longer_than_the_source_tail_is_reported():
    plan = {
        "item_position_s": 0.0, "item_length_s": 0.2,
        "blocks": [
            {"position_s": 0.0, "source_start_s": 0.42, "length_s": 0.08,
             "fade_in_s": 0.0, "fade_out_s": 0.01, "event_ids": []},
            {"position_s": 0.07, "source_start_s": 0.46, "length_s": 0.10,
             "fade_in_s": 0.01, "fade_out_s": 0.0, "event_ids": []},
        ],
        "anchors": [], "seams": [0.07],
    }
    codes = [row["code"] for row in conflict_report(plan, 0.425)]
    assert "source_past_end" in codes
    overlap = [row for row in conflict_report(plan, 0.425)
               if row["code"] == "overlap_exceeds_tail"]
    assert len(overlap) == 1
    assert overlap[0]["overlap_s"] == pytest.approx(0.01, abs=1e-9)
    assert overlap[0]["source_tail_s"] == pytest.approx(0.005, abs=1e-9)

def test_slow_rise_is_not_a_duplicate_attack():
    # Dense material (this project) has 30-75 ms attack rises.  One note with a
    # long rise must not be reported as two attacks just because a second front
    # crosses the threshold inside 40 ms.
    source = _hit(1.0, 0.40, decay_s=0.20, rise_s=0.060)
    plan = {
        "item_position_s": 0.0, "source_offset_s": 0.0, "item_length_s": 0.9,
        "blocks": [
            {"position_s": 0.0, "source_start_s": 0.0, "length_s": 0.9,
             "fade_in_s": 0.0, "fade_out_s": 0.0, "event_ids": ["one"]},
        ],
        "anchors": [{"id": "one", "project_s": 0.40, "target_s": 0.40}],
        "seams": [],
    }
    buffer, rate = render_plan(plan, source, sample_rate=RATE)
    rows = measure_attacks(plan, buffer, sample_rate=rate)

    assert rows[0]["found"] is True
    assert rows[0]["duplicate_nearby"] is False
