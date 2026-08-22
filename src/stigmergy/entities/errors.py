"""Domain errors of the entity-birth rules: library code raises these, never `SystemExit`.

Two classes, and the split is the one distinction any caller acts on: a FAULT in what was
declared (`EntityError`) against the GOVERNANCE verdict that the identity already exists
(`CollisionError`). `librarian.identity` catches them separately for exactly that reason — the
first becomes a gate finding naming the field, the second one naming the entity to anchor to
instead.

Messages are written to be read by whoever captured. They name fields and pages, never a path in
somebody's checkout and never a git command: the librarian is the only caller, it runs in a
container, and its worktree is a temporary directory that is gone by the time anyone reads the
sentence.
"""


class EntityError(RuntimeError):
    """Base class for the birth rules' refusals — a declared identity this gate will not write."""


class CollisionError(EntityError):
    """Resolve-before-write refused: an id, name or alias already resolves to a registered entity.

    A GOVERNANCE verdict rather than an operational fault: the corrective action is anchoring to
    the entity that exists, not writing a second one. The message names which input collided and
    with which entry.
    """
