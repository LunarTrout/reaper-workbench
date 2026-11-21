from reaper_audio.analysis.edit_map import tempo_grid


def test_tempo_grid_follows_constant_tempo():
    beats = tempo_grid([{"time_s": 0.0, "bpm": 120.0}], 2.0)
    assert [round(beat["time_s"], 3) for beat in beats[:5]] == [0.0, 0.5, 1.0, 1.5, 2.0]
