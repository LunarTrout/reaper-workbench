"""Stable, machine-readable errors used at library boundaries."""


class ReaperAudioError(Exception):
    code = "reaper_audio_error"

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"ok": False, "error": self.code, "message": self.message,
                "details": self.details}


class ValidationError(ReaperAudioError):
    code = "validation_error"


class BridgeUnavailable(ReaperAudioError):
    code = "bridge_unavailable"


class BridgeTimeout(ReaperAudioError):
    code = "bridge_timeout"


class StaleContext(ReaperAudioError):
    code = "stale_context"


class ApiUnavailable(ReaperAudioError):
    code = "api_unavailable"


class TransactionFailed(ReaperAudioError):
    code = "transaction_failed"


class AnalysisUnavailable(ReaperAudioError):
    code = "analysis_unavailable"


class ArtifactCleanupError(ReaperAudioError):
    code = "artifact_cleanup_failed"


class CaptureMasterInvalid(ReaperAudioError):
    """Capture committed for evidence, but the master signal is invalid."""

    code = "capture_master_invalid"


class MediaUnresolved(ReaperAudioError):
    """A rendered source could not be resolved to decodable media."""

    code = "media_unresolved"


class PanoramaCapacityError(ReaperAudioError):
    """The panorama exceeds the caller's explicit response budget."""

    code = "panorama_capacity_exceeded"


class PanoramaBindingError(ReaperAudioError):
    """A panorama sidecar is not bound to the canonical capture record."""

    code = "panorama_binding_invalid"


class RenderCandidateRefused(ReaperAudioError):
    """A plan-bound candidate render was refused before it could be trusted."""

    code = "render_candidate_refused"


class RenderBudgetExceeded(RenderCandidateRefused):
    """The requested candidate window or resulting file is over budget."""

    code = "render_budget_exceeded"


class RenderProvenanceIncomplete(RenderCandidateRefused):
    """The render cannot prove its stage, FX or routing provenance."""

    code = "render_provenance_incomplete"


class RenderCandidateMismatch(RenderCandidateRefused):
    """The render is not bound to the applied plan/revision or changed content."""

    code = "render_candidate_mismatch"


class RenderUnsupported(RenderCandidateRefused):
    """The installed bridge cannot provide the required master render."""

    code = "render_unsupported"
