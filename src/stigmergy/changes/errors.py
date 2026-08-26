"""Domain errors for change-record construction."""


class ChangeError(RuntimeError):
    """A change record could not be constructed safely."""

    retryable = True
