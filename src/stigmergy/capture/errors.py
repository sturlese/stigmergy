"""Safe domain errors for capture acquisition and processing."""


class CaptureError(RuntimeError):
    category = "capture"
    retryable = False


class SubmissionRejected(CaptureError):
    category = "invalid_submission"


class LayerErasing(SubmissionRejected):
    category = "layer_erasing"


class ArtifactRejected(SubmissionRejected):
    category = "invalid_artifact"


class FetchRejected(SubmissionRejected):
    category = "unsafe_url"


class FetchUnavailable(CaptureError):
    category = "fetch_unavailable"
    retryable = True


class ExtractionError(CaptureError):
    category = "extraction_failed"


class ExtractionTimeout(ExtractionError):
    retryable = True


class EvidenceError(CaptureError):
    category = "evidence_unavailable"
    retryable = True


class UploadError(CaptureError):
    category = "upload_failed"


class QueueStateError(CaptureError):
    category = "queue_state"
