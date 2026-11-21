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
