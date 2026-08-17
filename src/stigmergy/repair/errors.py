"""Domain refusals for the repair loop: library code raises, `cli.py` maps to a clean stderr line
and a non-zero exit, and the review lane maps to its own vocabulary.

Every sentence raised from `remote.apply_via_clone` is written to be PUBLISHABLE: a steward reads
it through the review lane, so it may name repo-relative paths and gate codes and must never name
this host's throwaway clone, an absolute path or a caught exception's own words — the same
discipline `entities/remote.py` states for its door, for the same reason.
"""


class RepairError(RuntimeError):
    """A refusal about one proposal: it would not validate, a gate vetoed it, or the apply could
    not be completed. Never a judgment about the finding that prompted it."""


class ProposalStateError(RepairError):
    """The proposal is not in a state this operation can act on — already decided, already
    applied, or gone. A subclass so a caller may answer "somebody else got there first"
    differently from "the gates refused this", and a `RepairError` so a caller that does not care
    still fails closed."""
