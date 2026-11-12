"""Corpus measurements, model and validation for reference-driven mixing.

Nothing in this package is a quality formula.  It provides:

* measurements defined by standards or by an explicit signal-processing
  definition (``measure.py``);
* a distance model whose centre, scale and covariance are estimated from a
  reference corpus at runtime (``model.py``);
* controlled degradations used to test whether the model reacts to production
  changes rather than to identity or level (``degrade.py``);
* the leave-one-track-out benchmark that reports that reaction (``benchmark.py``);
* defect gates independent of the tonal model (``gates.py``);
* an assessment report with an empirical percentile (``report.py``).
"""

from .measure import FEATURE_NAMES, measure_signal, measure_file
from .model import CorpusModel
from .degrade import DEGRADATIONS, degrade
from .benchmark import run_benchmark
from .gates import check, gate_values
from .manifest import Entry, Manifest
from .roles import Reconstruction, discover, reconstruction, role_matrix, role_models
from .report import Assessment, assess, assess_file, corpus_matrix_from_dir

__all__ = [
    "FEATURE_NAMES",
    "measure_signal",
    "measure_file",
    "CorpusModel",
    "DEGRADATIONS",
    "degrade",
    "run_benchmark",
    "check",
    "gate_values",
    "Entry",
    "Manifest",
    "Reconstruction",
    "discover",
    "reconstruction",
    "role_matrix",
    "role_models",
    "Assessment",
    "assess",
    "assess_file",
    "corpus_matrix_from_dir",
]
