"""Domain errors of the entity-birth subsystem.

Same posture as `capture.errors` and `librarian.errors`: library code raises these instead of
`SystemExit`, and the console entry point (`entities.cli`) maps them to one clean `stigmergy-entities:
...` stderr line plus a non-zero exit code. Nothing here ever reaches the network — this subsystem
has no wire path at all, so the rule that keeps wire-facing errors generic does not apply and every
message is written for the operator standing in front of the clone it is about, naming real paths,
real entity ids and real git state.
"""


class EntityError(RuntimeError):
    """Base class for entity-subsystem errors — every refusal an operator can hit."""


class CollisionError(EntityError):
    """Resolve-before-mint refused: an id, name or alias already resolves to a registered entity.

    Its own class rather than a plain `EntityError` because it is the one refusal here that is a
    GOVERNANCE verdict rather than an operational fault. A dirty clone is the steward's own state
    and clears itself; a collision means the identity being proposed already exists under another
    spelling, and the corrective action is to point the capture at that entity instead of minting a
    second one — resolve before mint. The message names which of the three inputs collided and with
    which entry: a refusal that does not name its mechanism cannot be told apart from a refusal
    that fired for some other reason.
    """


class CloneStateError(EntityError):
    """The steward's clone is not in a state `approve` may push from — dirty, diverged, or without
    a git identity to sign with. Never repaired automatically: this is a human's own working copy,
    and a tool that tidies it can discard work no commit holds."""


class PushRaceError(EntityError):
    """`origin/main` kept moving faster than the bounded fetch-regenerate-retry loop.

    Distinct from a genuine conflict, and worded distinctly, because the fix is different: nothing
    about the entity conflicted with anything: the branch simply never stopped moving. Carries what
    the loop left behind — the commit is in the local clone and nothing was force-pushed — because
    a message that says "could not push" without saying what state that leaves is the exact failure
    an operator cannot act on.
    """


class CapabilityUnavailableError(EntityError):
    """A server-driven mint (`entities.remote.mint_via_clone`, ADR 030 D3) needs a capability this
    process was not given: the librarian GitHub App credential, or a knowledge-repo URL to clone
    from. Named after — and meant to be mapped to — `server.errors.CapabilityUnavailableError`,
    the SAME posture one layer up: a named missing capability, not a governance refusal and not a
    startup failure. A distinct class from `EntityError` rather than a message an `except` clause
    would have to pattern-match, because `stigmergy.entities` may not import `stigmergy.server.errors`
    to raise that type directly (the one-way layering `test_entities_never_imports_server_or_
    answer` pins) — `stigmergy.server.review` is the one place both names are in scope, and it is
    where the mapping happens.
    """
