"""Safe domain errors for capture acquisition and processing."""


class CaptureError(RuntimeError):
    category = "capture"


class SubmissionRejected(CaptureError):
    category = "invalid_submission"


class ArtifactRejected(SubmissionRejected):
    category = "invalid_artifact"


class FetchRejected(SubmissionRejected):
    category = "unsafe_url"


class ExtractionError(CaptureError):
    category = "extraction_failed"


class EvidenceError(CaptureError):
    category = "evidence_unavailable"


class UploadError(CaptureError):
    category = "upload_failed"


class QueueStateError(CaptureError):
    category = "queue_state"
