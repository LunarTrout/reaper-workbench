from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from reaper_audio.daw.timing_preview import render_timing_preview


def test_preview_writes_original_aligned_and_click(tmp_path: Path) -> None:
    rate = 48_000
    samples = np.zeros((rate, 2), dtype=np.float64)
    samples[1000:1200, :] = 0.8
    source = tmp_path / "take.wav"
    sf.write(source, samples, rate, subtype="PCM_24")
    plan = {
        "source_path": str(source),
        "duration_s": 1.0,
        "blocks": [
            {"start_s": 0.0, "end_s": 0.1, "target_start_s": 0.005,
             "target_end_s": 0.105, "fade_in_s": 0.003, "fade_out_s": 0.003},
            {"start_s": 0.1, "end_s": 1.0, "target_start_s": 0.1,
             "target_end_s": 1.0, "fade_in_s": 0.003, "fade_out_s": 0.003},
        ],
    }
    result = render_timing_preview(plan, out_dir=tmp_path / "preview", bpm=120)
    assert Path(result["original_uri"]).is_file()
    assert Path(result["aligned_uri"]).is_file()
    assert Path(result["click_uri"]).is_file()
    assert result["block_count"] == 2
    assert result["native_render"] is False
    aligned, _ = sf.read(result["aligned_uri"], always_2d=True)
    assert len(aligned) == rate

def test_preview_click_follows_project_tempo_meter_and_phase(tmp_path):
    from reaper_audio.daw.midi import TempoMap
    from reaper_audio.daw.timing_preview import _click_beats

    context = {"bpm": 120, "numerator": 4, "denominator": 4, "markers": [
        {"time_s": 2.0, "bpm": 60, "numerator": 3, "denominator": 8, "measure": 1, "beat": 0},
    ]}
    tempo = TempoMap.from_context(context)
    assert list(_click_beats(tempo, 1.125, 3.126, 0.125)) == [
        (0.0, False), (0.5, False), (1.0, True), (1.5, False), (2.0, False),
    ]
    rate = 8000
    source = tmp_path / "take.wav"
    sf.write(source, np.zeros(rate * 4), rate)
    plan = {
        "method": "crossfade", "source_path": str(source),
        "item_position_s": 0.0, "source_offset_s": 0.0, "item_length_s": 4.0,
        "tempo_context": {**context, "markers": [
            {"time_s": 2.0, "bpm": 60, "numerator": 4, "denominator": 4}]},
        "blocks": [{"source_start_s": 0.0, "position_s": 0.0, "length_s": 4.0}],
    }
    result = render_timing_preview(plan, out_dir=tmp_path / "preview")
    click, _ = sf.read(result["click_uri"])
    assert not np.any(click[int(2.5 * rate):int(2.6 * rate)])
    assert np.any(click[3 * rate:3 * rate + 50])
