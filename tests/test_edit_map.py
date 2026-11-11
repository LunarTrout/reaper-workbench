import numpy as np

from reaper_audio.analysis.edit_map import build_edit_map, tempo_grid
from reaper_audio.models import AudioBuffer


def _click_track(times, *, fs=48000, duration=2.0, amplitude=0.8):
    samples = np.zeros(int(duration * fs))
    rng = np.random.default_rng(7)
    for time_s in times:
        start = int(time_s * fs)
        length = int(0.03 * fs)
        envelope = np.exp(-np.arange(length) / (0.012 * fs))
        samples[start:start + length] += amplitude * envelope * rng.standard_normal(length)
    return AudioBuffer(np.column_stack([samples, samples]), fs)


def test_tempo_grid_follows_constant_tempo():
    beats = tempo_grid([{"time_s": 0.0, "bpm": 120.0}], 2.0)
    assert [round(beat["time_s"], 3) for beat in beats[:5]] == [0.0, 0.5, 1.0, 1.5, 2.0]


def test_edit_map_clusters_agreeing_sources_and_flags_offsets():
    a = _click_track([0.53, 1.03, 1.53])
    b = _click_track([0.53, 1.03, 1.53], amplitude=0.6)
    result = build_edit_map({"kick": a, "snare": b},
                            [{"time_s": 0.0, "bpm": 120.0}], intent="tight")
    clusters = result["clusters"]
    assert len(clusters) >= 3
    near = [cluster for cluster in clusters if 0.45 <= cluster["time_s"] <= 1.60]
    assert len(near) >= 3
    assert all(abs(cluster["time_s"] - round(cluster["time_s"] * 2) / 2) < 0.06
               for cluster in near)
    assert any(cluster["action"] == "cut" for cluster in near)
    assert all(cluster["confidence"] > 0 for cluster in near)
    assert result["quantize_strength"] == 1.0


def test_edit_map_preserve_intent_is_stricter():
    a = _click_track([0.52, 1.02])
    tight = build_edit_map({"kick": a}, [{"time_s": 0.0, "bpm": 120.0}],
                           intent="tight")
    preserve = build_edit_map({"kick": a}, [{"time_s": 0.0, "bpm": 120.0}],
                              intent="preserve")
    assert len(tight["recommended_cuts"]) >= len(preserve["recommended_cuts"])
