"""views.errors — the domain exceptions this package raises."""


class ViewError(Exception):
    """A refusal a human reads as one sentence, no traceback — caught by the CLI's `main()`."""
