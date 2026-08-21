"""Domain errors of the entity-birth subsystem: library code raises these, never `SystemExit`;
`entities.cli` maps them to one stderr line plus a non-zero exit. Messages are written for the
operator in front of the clone, naming real paths and real git state.

**The TYPE is what the second door reads.** `entities.remote.decide_via_clone` re-words the refusals
whose sentences only make sense to somebody holding that clone (ADR 030's two-door amendment), and
it tells them apart by class and by nothing else — never by matching the text. So a raise site
whose sentence names a path or a git command needs a class no clean raise site shares, and
splitting one is a governance change, not a tidy-up: it moves which door says what.
"""


class EntityError(RuntimeError):
    """Base class for entity-subsystem errors — every refusal an operator can hit."""


class CollisionError(EntityError):
    """Resolve-before-mint refused: an id, name or alias already resolves to a registered entity.

    The one refusal here that is a GOVERNANCE verdict rather than an operational fault: the
    corrective action is pointing the capture at the existing entity, not minting a second one.
    The message names which input collided and with which entry — door-neutral by construction,
    which is why it is the class that passes through `entities.remote` untouched.
    """


class CollisionRaceError(CollisionError):
    """The SAME verdict, reached after a rebase — `origin/main` gained the colliding entity while
    this mint was committing.

    A subclass rather than a flag so both doors keep working unchanged: every `except
    CollisionError` still catches it, and the steward at a terminal still reads the full sentence.
    What it buys is the one thing the text cannot say by itself — that this collision did NOT
    exist when the caller pressed Approve. Its message splices the local clone's sha and two
    `git -C <clone>` commands, which is the diagnosis for a human standing in that clone and noise
    on the server door, where the clone is a `TemporaryDirectory` already deleted. Mapping the
    plain `CollisionError` there instead would rewrite "this identity already exists, point the
    capture at it" into "something moved, approve again" — a governance verdict turned into a
    retry loop.
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


class TemplateMissingError(EntityError):
    """`ops/templates/entity.md` is not in the clone being minted into.

    A new entity page IS that template with its identity fields filled in, and no door carries a
    copy of its own — an edit to the template must reach minted pages with no platform release.
    Its own class for the reason the module docstring gives: the CLI's sentence names WHICH
    checkout is missing it, which is the whole diagnosis there and meaningless on the server door,
    where the answer is always "commit it to the knowledge repo".
    """


class CapabilityUnavailableError(EntityError):
    """A server-driven decision (`entities.remote.decide_via_clone`) lacks a capability this process
    was not given: the librarian GitHub App credential, or a knowledge-repo URL to clone from. A
    named missing capability — not a governance refusal, not a startup failure. A distinct class
    because `stigmergy.entities` may not import `stigmergy.server.errors` to raise the server's
    equivalent directly; `stigmergy.server.review` is where the mapping happens.
    """
