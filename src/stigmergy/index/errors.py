"""Domain errors of the index subsystem.

Library code raises these instead of SystemExit: the CLIs translate them to exit codes, and the
MCP server consumes the same seams (`build.rebuild`, `search.search`) — a server must get an
exception it can map to a response, never a process kill.
"""


class StigmergyIndexError(RuntimeError):
    """Base class for index domain errors."""


class EmptyIndexError(StigmergyIndexError):
    """A query arrived before any index was ever built in this database."""


class EmptyCorpusError(StigmergyIndexError):
    """A rebuild found no pages in the repo's included zones — wrong --repo path, usually."""
