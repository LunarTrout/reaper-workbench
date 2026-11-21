from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import pytest

from reaper_audio.analysis.audio import load_audio
from reaper_audio.analysis.timing import (build_timing_plan, detect_timing_events,
                                          grid_times, match_events)
from reaper_audio.daw.midi import TempoMap

REFERENCE = os.environ.get("REAPER_TIMING_REFERENCE")
pytestmark = pytest.mark.skipif(not REFERENCE, reason="REAPER_TIMING_REFERENCE is not set")


def _reference(root: Path):
    rpp = root / "Blind-grid-dense.rpp"
    source = root / "Media/Take-30-DI.wav"
    text = rpp.read_text(encoding="utf-8", errors="replace")
    part = text[text.index('NAME "B - Edited DI"'):]
    items = re.findall(r"<ITEM\n(.*?)\n      >", part, re.S)
    clips = [{"position_s": float(re.search(r"POSITION ([0-9.]+)", item).group(1)),
              "source_offset_s": float(re.search(r"SOFFS ([0-9.]+)", item).group(1)),
              "length_s": float(re.search(r"LENGTH ([0-9.]+)", item).group(1))}
             for item in items]
    return rpp, source, clips


def test_take_30_reference_is_reproducible_and_plan_keeps_order() -> None:
    root = Path(REFERENCE)
    rpp, source, clips = _reference(root)
    assert rpp.is_file() and source.is_file()
    assert len(clips) == 161
    assert hashlib.sha256(source.read_bytes()).hexdigest() == \
        "7cd0da26eb8ab021bcc1547de487eb188fa78fde17ab85b1b48ed95cfba8485f"

    position = 1.149297052154
    duration = 38.588639455782
    audio = load_audio(str(source))
    events = detect_timing_events(audio, sensitivity=1.4, min_gap_s=0.03)["events"]
    tempo = TempoMap.from_context({"bpm": 155.5, "numerator": 4,
                                   "denominator": 4, "markers": []})
    grid = grid_times(tempo, position, position + duration, subdivisions=16)
    source_grid = [{"time_s": row["time_s"] - position, "qn": row["qn"]}
                   for row in grid]
    matched = match_events(events, source_grid, tolerance_s=0.08)
    plan = build_timing_plan(events, matched["assignments"], duration_s=duration,
                             strength=1.0, group_gap_s=0.25, group_shift_s=0.012)

    assert plan["matched_count"] == matched["matched"]
    assert plan["median_error_after_s"] <= plan["median_error_before_s"]
    assert plan["blocks"], "the reference must produce an edit plan"
    # Source order and 1x playback are hard invariants of the planner.
    previous_source = -1.0
    previous_target = -1.0
    for block in plan["blocks"]:
        assert block["rate"] == 1.0
        assert block["start_s"] < block["end_s"]
        assert block["start_s"] >= previous_source
        assert block["target_start_s"] >= previous_target - 1e-6
        previous_source, previous_target = block["start_s"], block["target_start_s"]
    # The dense run must at least expose its out-of-handle first block instead
    # of silently clipping it.
    assert any(issue["code"] == "source_handles_unavailable"
               for issue in plan["issues"])
