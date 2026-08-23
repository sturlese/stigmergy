"""Domain refusals for a page removal: library code raises, the worker's removal flow publishes the
sentence to whoever asked, and the capture's report carries it back.

Every sentence raised here is written to be READ — it reaches the person who asked for the removal
over MCP or on the console — so it may name repo-relative paths and gate codes, and must never name
this host's worktree, an absolute path, or a caught exception's own words.
"""


class RepairError(RuntimeError):
    """A refusal about one removal: a page it names may not be removed, the plan is larger than one
    approval may be, or the sweep could not write the pages that stay. Never a judgment about
    anything else the corpus holds."""
