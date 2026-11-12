"""Assessment report: percentile against the corpus and defect gates."""

from __future__ import annotations

import numpy as np

from reaper_audio.corpus import CorpusModel, assess, corpus_matrix_from_dir


def _corpus(n: int = 8, seed: int = 1):
    rng = np.random.default_rng(seed)
    from reaper_audio.corpus import FEATURE_NAMES
    matrix = rng.normal(0.0, 1.0, size=(n, len(FEATURE_NAMES)))
    return matrix, CorpusModel.fit(matrix, FEATURE_NAMES)


def test_percentile_between_zero_and_one():
    matrix, model = _corpus()
    x = np.zeros((32000, 2))
    result = assess(model, matrix, x, 32000)
    assert 0.0 <= result.percentile <= 1.0


def test_contributions_sorted_by_share():
    matrix, model = _corpus()
    rng = np.random.default_rng(5)
    x = rng.normal(0.0, 0.05, size=(32000, 2))
    result = assess(model, matrix, x, 32000)
    shares = [abs(s) for _, _, s in result.contributions]
    assert shares == sorted(shares, reverse=True)


def test_policy_violation_is_reported():
    matrix, model = _corpus()
    rng = np.random.default_rng(6)
    x = rng.normal(0.0, 0.02, size=(64000, 2))
    x[:100] = 1.0
    result = assess(model, matrix, x, 32000, policy={"max_clip_runs": 0})
    assert "clipping" in result.violations


def test_stage_filter_uses_manifest(tmp_path):
    import soundfile as sf
    from reaper_audio.corpus.manifest import Entry, Manifest
    rng = np.random.default_rng(8)
    for i in range(3):
        sf.write(str(tmp_path / f"t{i}.wav"), rng.normal(0, 0.05, (32000 * 2, 2)), 32000)
    manifest = Manifest([
        Entry(file="t0.wav", source="t0", stage="master"),
        Entry(file="t1.wav", source="t1", stage="master"),
        Entry(file="t2.wav", source="t2", stage="mix"),
    ])
    masters = corpus_matrix_from_dir(tmp_path, window_s=1.0, manifest=manifest, stage="master")
    assert masters.shape[0] == 4


def test_distance_timeline_covers_every_window():
    import numpy as np
    from reaper_audio.corpus.report import distance_timeline
    matrix, model = _corpus()
    rng = np.random.default_rng(9)
    audio = rng.normal(0.0, 0.05, size=(32000 * 4, 2))
    timeline = distance_timeline(model, matrix, audio, 32000, window_s=1.0)
    assert len(timeline) == 4
    assert all(0.0 <= row["percentile"] <= 1.0 for row in timeline)
    assert timeline[0]["start_s"] == 0.0 and timeline[-1]["end_s"] == 4.0


def test_corpus_matrix_from_dir(tmp_path):
    import soundfile as sf
    rng = np.random.default_rng(7)
    for i in range(3):
        audio = rng.normal(0.0, 0.05, size=(32000 * 3, 2))
        sf.write(str(tmp_path / f"t{i}.wav"), audio, 32000)
    matrix = corpus_matrix_from_dir(tmp_path, window_s=1.0)
    assert matrix.shape[0] == 9
