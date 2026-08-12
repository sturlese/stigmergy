"""Domain errors of the entity-birth subsystem: library code raises these, never `SystemExit`;
`entities.cli` maps them to one stderr line plus a non-zero exit. No wire path here, so messages
are written for the operator in front of the clone, naming real paths and real git state.
"""


class EntityError(RuntimeError):
    """Base class for entity-subsystem errors — every refusal an operator can hit."""


class CollisionError(EntityError):
    """Resolve-before-mint refused: an id, name or alias already resolves to a registered entity.

    The one refusal here that is a GOVERNANCE verdict rather than an operational fault: the
    corrective action is pointing the capture at the existing entity, not minting a second one.
    The message names which input collided and with which entry.
    """


class CloneStateError(EntityError):
    """The steward's clone is not in a state `approve` may push from — dirty, diverged, or without
    a git identity to sign with. Never repaired automatically: this is a human's own working copy,
    and a tool that tidies it can discard work no commit holds."""


class PushRaceError(EntityError):
    """`origin/main` kept moving faster than the bounded fetch-regenerate-retry loop.

    Distinct from a genuine conflict because the fix is different — nothing conflicted, the
    branch never stopped moving. The message carries what the loop left behind (the commit is in
    the local clone, nothing was force-pushed): "could not push" without the resulting state is a
    failure an operator cannot act on.
    """


class CapabilityUnavailableError(EntityError):
    """A server-driven mint (`entities.remote.mint_via_clone`) lacks a capability this process
    was not given: the librarian GitHub App credential, or a knowledge-repo URL to clone from. A
    named missing capability — not a governance refusal, not a startup failure. A distinct class
    because `stigmergy.entities` may not import `stigmergy.server.errors` to raise the server's
    equivalent directly; `stigmergy.server.review` is where the mapping happens.
    """
