"""Domain errors of the index subsystem. Library code raises these instead of SystemExit: the
CLIs translate them to exit codes, and the server maps them to responses — never a process kill.
"""


class StigmergyIndexError(RuntimeError):
    """Base class for index domain errors."""


class EmptyIndexError(StigmergyIndexError):
    """A query arrived before any index was ever built in this database."""


class EmptyCorpusError(StigmergyIndexError):
    """A rebuild found no pages in the repo's included zones — wrong --repo path, usually."""
