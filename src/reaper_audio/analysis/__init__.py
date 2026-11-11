"""Signal analysis with explicit units, coverage and provenance."""

from .artifacts import AudioArtifactWorkspace, CleanupReport, cleanup_stale_audio_jobs
from .audio import load_audio, slice_audio
from .bundle import analyze_bundle
from .events import detect_events
from .metrics import measure_audio
from .partial import attribute_partial_loudness
from .peaks import estimate_true_peak
from .phase import compare_track_phase
from .perceptual import third_octave_levels, zwicker_series
from .temporal import analyze as analyze_temporal_profile, masking as analyze_masking
from .tempo import estimate_tempo_curve
from .transients import detect_transients

__all__ = ["AudioArtifactWorkspace", "CleanupReport", "analyze_bundle", "load_audio",
           "slice_audio", "measure_audio", "analyze_temporal_profile",
           "analyze_masking", "detect_events", "estimate_tempo_curve",
           "attribute_partial_loudness", "third_octave_levels", "zwicker_series",
           "detect_transients", "estimate_true_peak", "compare_track_phase",
           "cleanup_stale_audio_jobs"]
