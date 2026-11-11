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
