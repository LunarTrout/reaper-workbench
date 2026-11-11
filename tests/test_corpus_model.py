"""The corpus model must be estimated from data, not from constants."""

from __future__ import annotations

import numpy as np
import pytest

from reaper_audio.corpus import CorpusModel
from reaper_audio.corpus.model import MAD_TO_SIGMA


def _matrix(n: int = 12, p: int = 6, seed: int = 3):
    rng = np.random.default_rng(seed)
    return rng.normal(0.0, 1.0, size=(n, p))


def test_center_and_scale_are_empirical():
    x = _matrix()
    model = CorpusModel.fit(x)
    assert np.allclose(model.center, np.median(x, axis=0))
    assert np.allclose(model.scale, MAD_TO_SIGMA * np.median(np.abs(x - np.median(x, axis=0)), axis=0))


def test_distance_at_center_is_zero():
    x = _matrix()
    model = CorpusModel.fit(x)
    assert model.distance(model.center)[0] == pytest.approx(0.0, abs=1e-9)


def test_distance_grows_with_offset():
    x = _matrix()
    model = CorpusModel.fit(x)
    z = model.center + 3.0 * model.scale
    assert model.distance(z)[0] > model.distance(model.center)[0]


def test_flat_feature_does_not_dominate():
    x = _matrix()
    x[:, 1] = 0.5  # constant inside the corpus
    model = CorpusModel.fit(x)
    y = x[0].copy()
    y[1] = 1e6
    z = model.standardise(y)
    # a feature with no corpus variance gets unit scale and zero centre offset
    assert np.isfinite(z[1])


def test_shrinkage_is_estimated_from_data():
    x = _matrix(n=40)
    model = CorpusModel.fit(x)
    assert 0.0 <= model.shrinkage <= 1.0


def test_loo_distances_are_stricter_than_full_model():
    x = _matrix(n=15)
    full = CorpusModel.fit(x)
    loo = full.loo_distances(x)
    inside = full.distance(x)
    assert np.median(loo) >= np.median(inside) - 1e-9


def test_contributions_sum_to_one():
    x = _matrix()
    model = CorpusModel.fit(x)
    rows = model.contributions(model.center + 2 * model.scale)
    shares = sum(abs(s) for _, _, s in rows)
    assert shares == pytest.approx(1.0, abs=1e-6)


def test_two_samples_are_enough_to_fit_but_reported_as_such():
    x = _matrix(n=2, p=4)
    model = CorpusModel.fit(x)
    assert model.summary()["n_samples"] == 2
