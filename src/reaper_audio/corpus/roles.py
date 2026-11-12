"""Role-level corpus: per-stem statistics and a reconstruction check.

Stems are only usable for targets if (a) the separator is the same on both
sides and (b) the stems actually reconstruct the mix.  This module provides
both checks and the per-role matrices; it does not decide which roles are
trustworthy beyond naming the measured error.

Role names come from the separator's output directory, not from a genre
template: whatever stems exist on both sides are compared.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .measure import FEATURE_NAMES, measure_signal
from .model import CorpusModel


@dataclass
class Reconstruction:
    correlation: float
    residual_db: float
    null_depth_db: float

    def to_dict(self) -> dict:
        return {"correlation": self.correlation, "residual_db": self.residual_db,
                "null_depth_db": self.null_depth_db}


def discover(root: str | Path) -> dict[str, list[Path]]:
    """Map role name -> stem files, using the layout <root>/<model>/<source>/<role>.wav."""
    roles: dict[str, list[Path]] = {}
    for path in sorted(Path(root).rglob("*.wav")):
        role = path.stem.lower()
        roles.setdefault(role, []).append(path)
    return roles


def _read(path: Path) -> tuple[np.ndarray, int]:
    info = sf.info(str(path))
    x, sr = sf.read(str(path), dtype="float64", always_2d=True)
    return x, int(sr)


def reconstruction(mix_path: str | Path, stems: dict[str, str | Path]) -> Reconstruction:
    """Compare the sum of stems with the mix they came from."""
    mix, sr = _read(Path(mix_path))
    total = np.zeros_like(mix)
    for path in stems.values():
        x, stem_sr = _read(Path(path))
        if stem_sr != sr:
            raise ValueError(f"stem {path} has {stem_sr} Hz against mix {sr} Hz")
        n = min(total.shape[0], x.shape[0])
        total = total[:n]
        total += x[:n]
        mix = mix[:n]
    residual = mix - total
    denom = float(np.sum(mix ** 2)) + 1e-20
    corr = float(np.corrcoef(mix.mean(axis=1), total.mean(axis=1))[0, 1])
    residual_db = float(10 * np.log10((np.sum(residual ** 2) + 1e-20) / denom))
    null_depth_db = float(20 * np.log10((np.sqrt(np.sum(mix ** 2)) + 1e-20) /
                                        (np.sqrt(np.sum(residual ** 2)) + 1e-20)))
    return Reconstruction(correlation=corr, residual_db=residual_db,
                          null_depth_db=null_depth_db)


def role_matrix(paths: list[Path], *, window_s: float = 10.0,
                max_seconds: float | None = None) -> np.ndarray:
    rows = []
    for path in paths:
        x, sr = _read(path)
        if max_seconds is not None:
            x = x[: int(max_seconds * sr)]
        step = int(window_s * sr)
        for start in range(0, x.shape[0] - step + 1, step):
            rows.append(measure_signal(x[start:start + step], sr).values)
    if not rows:
        raise ValueError(f"no windows in {paths[:1]}")
    return np.stack(rows)


def role_models(roles: dict[str, list[Path]], *, window_s: float = 10.0) -> dict[str, CorpusModel]:
    models = {}
    for role, paths in roles.items():
        matrix = role_matrix(paths, window_s=window_s)
        models[role] = CorpusModel.fit(matrix, FEATURE_NAMES)
        models[role].matrix = matrix  # type: ignore[attr-defined]
    return models
