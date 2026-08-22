"""Domain refusals for the repair loop: library code raises, the worker's pass records the
sentence in the ledger, and the deletion door publishes it to whoever asked.

Every sentence raised from `apply.apply_in_tree` is written to be READ — it lands in `repairs.error`
and on the console, and for a deletion it goes straight back over MCP. So it may name repo-relative
paths and gate codes, and must never name this host's worktree, an absolute path, or a caught
exception's own words.
"""


class RepairError(RuntimeError):
    """A refusal about one repair: it would not validate, a gate vetoed it, or the apply could not
    be completed. Never a judgment about the finding that prompted it — the finding stays in the
    gardener's report either way."""


class CorpusMovedError(RepairError):
    """The repair was fine and the WORLD moved: the pages it was derived against are not the pages
    it met, or another push landed between the plan and this one.

    A separate class because it decides what the ledger remembers. `content_key` is normally
    permanent — a repair the gates refused is not retried, or the loop would spend a model call
    every night on a question it cannot answer. But a refusal that is about the TREE rather than
    about the repair is not a verdict on the repair at all, and remembering it would retire a
    finding for a race. So this one records a `failed` row with NO key, and the next pass derives
    the same repair again against the corpus as it now stands.

    Learnt from a real one: two duplicate-entity merges in one pass. Both are derived against the
    pass's base; the first regenerates `ops/entity-registry.json` and pushes, and the second then
    meets a tree its plan no longer describes. Remembered, that pair would never be merged.
    """


class ProposalStateError(RepairError):
    """There is nothing here to apply: no ops, or every op is already performed. A subclass so a
    caller may answer "this changes nothing" differently from "the gates refused this", and a
    `RepairError` so a caller that does not care still fails closed."""
