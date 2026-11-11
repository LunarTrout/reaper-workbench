"""Corpus model: centre, scale and covariance estimated from references.

The model has no tuning constants.  Location is the median, scale is the
normal-consistent MAD, and the covariance is Ledoit-Wolf shrunk towards a
scaled identity, whose shrinkage coefficient is estimated from the data.  The
distance is Mahalanobis in that metric.

With very few references the covariance estimate is weak; the model reports
the effective number of observations and the condition number so a caller can
see how much of the answer is supported by data.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.covariance import ledoit_wolf

MAD_TO_SIGMA = 1.4826  # normal-consistency factor of the MAD: a definition


@dataclass
class CorpusModel:
    center: np.ndarray
    scale: np.ndarray
    covariance: np.ndarray
    precision: np.ndarray
    n_samples: int
    names: tuple[str, ...]
    shrinkage: float
    condition: float

    @classmethod
    def fit(cls, matrix: np.ndarray, names: tuple[str, ...] | None = None) -> "CorpusModel":
        x = np.asarray(matrix, dtype=np.float64)
        if x.ndim != 2:
            raise ValueError("matrix must be (n_samples, n_features)")
        if x.shape[0] < 2:
            raise ValueError("need at least two references to estimate a scale")
        center = np.median(x, axis=0)
        scale = MAD_TO_SIGMA * np.median(np.abs(x - center), axis=0)
        # A feature that does not vary inside the corpus carries no distance
        # information; it gets unit scale and therefore zero contribution.
        flat = scale < 1e-12
        scale = np.where(flat, 1.0, scale)
        z = (x - center) / scale
        cov, shrinkage = ledoit_wolf(z, assume_centered=True)
        try:
            precision = np.linalg.inv(cov)
        except np.linalg.LinAlgError:
            precision = np.linalg.pinv(cov)
        cond = float(np.linalg.cond(cov)) if cov.size else 1.0
        return cls(
            center=center,
            scale=scale,
            covariance=cov,
            precision=precision,
            n_samples=int(x.shape[0]),
            names=tuple(names) if names is not None else tuple(f"f{i}" for i in range(x.shape[1])),
            shrinkage=float(shrinkage),
            condition=cond,
        )

    def standardise(self, matrix: np.ndarray) -> np.ndarray:
        return (np.asarray(matrix, dtype=np.float64) - self.center) / self.scale

    def distance(self, matrix: np.ndarray) -> np.ndarray:
        """Mahalanobis distance; accepts (n_features,) or (n, n_features)."""
        z = self.standardise(matrix)
        if z.ndim == 1:
            return np.array([float(np.sqrt(max(z @ self.precision @ z, 0.0)))])
        d2 = np.einsum("ij,jk,ik->i", z, self.precision, z)
        return np.sqrt(np.maximum(d2, 0.0))

    def contributions(self, vector: np.ndarray) -> list[tuple[str, float, float]]:
        """Per-feature signed standardised deviation and its share of the distance.

        Returns (name, z, share) sorted by absolute share, largest first.
        Share is the fraction of the squared Mahalanobis distance attributable
        to the feature's linear term; it is diagnostic, not a score.
        """
        z = self.standardise(vector)
        total = float(z @ self.precision @ z)
        rows = []
        for i, name in enumerate(self.names):
            linear = z[i] * float(self.precision[i] @ z)
            share = linear / total if total > 0 else 0.0
            rows.append((name, float(z[i]), float(share)))
        rows.sort(key=lambda r: -abs(r[2]))
        return rows

    def loo_distances(self, matrix: np.ndarray) -> np.ndarray:
        """Distance of each reference to the model fitted without it."""
        x = np.asarray(matrix, dtype=np.float64)
        out = np.empty(x.shape[0])
        for i in range(x.shape[0]):
            mask = np.ones(x.shape[0], dtype=bool)
            mask[i] = False
            model = CorpusModel.fit(x[mask], self.names)
            out[i] = model.distance(x[i])[0]
        return out

    def summary(self) -> dict:
        return {
            "n_samples": self.n_samples,
            "n_features": int(self.center.size),
            "shrinkage": self.shrinkage,
            "condition": self.condition,
        }
