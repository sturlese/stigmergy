"""views.errors — the domain exceptions this package raises. Mirrors `entities.errors`'
shape (one small hierarchy, no cross-package reuse needed for something this small)."""


class ViewError(Exception):
    """A refusal a human should read as one sentence, no traceback — the CLI's `main()` catches
    this (and its subclasses) the same way `stigmergy-entities`/`stigmergy-queue` catch their own
    domain errors."""
