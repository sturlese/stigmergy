"""The three repo-sourced inputs the fast lane reads — **at the commit it files against**.

`ops/acl.json`, `ops/entity-registry.json` and `.claude/tools/stigmergy_lint.py` are read at
`base`, never off the working tree: an uncommitted local edit must never influence a filing, or a
page is anchored to an entity nobody approved and stamped with labels its own commit disagrees
with. Missing keeps its meaning in every case — no ACL config is an open corpus, no registry is an
empty one, a missing linter is a loud fail-closed refusal.
"""
import json
import os
import tempfile
from contextlib import contextmanager

from stigmergy.kernel import registry as registry_module
from stigmergy.librarian import acl_rules, config, gitcmd
from stigmergy.librarian.errors import GitError, LibrarianConfigError

# Temp directories this module makes. Deliberately NOT under `gitcmd.WORKTREE_PREFIX`: startup
# reaping deletes worktree directories, and a materialized input named like a worktree would be
# swept out from under the run that is executing it.
_TEMP_PREFIX = "stigmergy-base-input-"


def where(base: gitcmd.BaseRef, relpath: str) -> str:
    """`origin/main@abc123def456:ops/acl.json` — how every message in this module names an input,
    shared with `worker._check_skill_at` so the two cannot use different layouts."""
    return f"{base.describe()}:{relpath}"


def push_or_commit_hint(base: gitcmd.BaseRef) -> str:
    """Why a file that is on disk can still be absent at `base`: the worktrees branch from the
    REMOTE, so a commit that exists only locally might as well not exist."""
    if base.remote:
        return (f"Push the commit that adds it: the worktree is built from {base.ref}, not from "
                f"your local checkout.")
    return (f"Commit it on {base.ref}: the worktree is built from that branch, not from the "
            f"working tree.")


# ── the shared base: one place that knows what "read at the base commit" means ────────────────
def read_at(repo: str, base: gitcmd.BaseRef, relpath: str) -> str | None:
    """The content of one repo-sourced input AT `base`, or `None` when that commit has no such path.

    `blob_size` first rather than catching `GitError` from `show`: "this commit does not carry the
    file" is a legitimate configuration and "the read failed" is a fault, and one `except` around
    `git show` would collapse the two.
    """
    if gitcmd.blob_size(repo, base.sha, relpath) < 0:
        return None
    try:
        return gitcmd.show(repo, base.sha, relpath)
    except GitError as ex:
        raise LibrarianConfigError(
            f"{where(base, relpath)} could not be read ({ex}) — it is one of the three inputs the "
            f"librarian judges a capture with, and it will not file without knowing what they "
            f"say") from ex


@contextmanager
def _materialized(text: str, relpath: str):
    """`text` as a real file named after `relpath`, for the life of the block. The basename is
    preserved so anything reporting the file names something recognizable."""
    with tempfile.TemporaryDirectory(prefix=_TEMP_PREFIX) as tmp:
        path = os.path.join(tmp, relpath.rsplit("/", 1)[-1])
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        yield path


# ── the three wrappers: one per input, each keeping its own semantics ──────────────────────────
def load_acl(repo: str, base: gitcmd.BaseRef):
    """`ops/acl.json` at `base`, through the librarian's dialect adapter — no file anywhere. A
    commit with no ACL config is an open corpus, `acl.py`'s own semantics for a missing one."""
    relpath = config.ACL_RELPATH
    return acl_rules.load_text(read_at(repo, base, relpath), label=where(base, relpath))


def load_registry(repo: str, base: gitcmd.BaseRef):
    """`ops/entity-registry.json` at `base`, materialized so the kernel's own path-taking reader
    stays untouched.

    A malformed registry is answered by a SENTENCE, not a traceback: `load_registry` raises bare
    `ValueError`/`OSError`, which `cli.main` would let through to the terminal. The temp path in
    the reader's words is substituted for the base-commit locator an operator can act on.
    """
    relpath = config.REGISTRY_RELPATH
    label = where(base, relpath)
    text = read_at(repo, base, relpath)
    if text is None:
        # Missing -> empty registry: the graph works unregistered, and every name then parks as an
        # entity situation rather than anchoring.
        return registry_module.Registry()
    with _materialized(text, relpath) as path:
        try:
            return registry_module.load_registry(path)
        except (ValueError, OSError) as ex:
            raise LibrarianConfigError(
                f"the entity registry at {label} could not be loaded "
                f"({str(ex).replace(path, label)}) — it is what every anchoring decision resolves "
                f"against, so a broken one would either refuse every capture or file "
                f"pages against entities that do not exist") from ex


def load_stewards(repo: str, base: gitcmd.BaseRef) -> dict:
    """`ops/stewards.json` at `base`: the doorbell's scope -> steward-emails map, read at the base
    commit like `load_acl`/`load_registry` above. See `_parse_stewards` for the shape rules."""
    relpath = config.STEWARDS_RELPATH
    return _parse_stewards(read_at(repo, base, relpath), where(base, relpath))


def load_stewards_file(path: str) -> dict:
    """The same map from a PLAIN FILE — the deploy-time snapshot a process with no checkout reads.

    A separate entry point rather than a `repo=""` branch of the reader above: the two answer
    different questions, and one reader silently answering "what the image shipped with" when
    asked "what the governing commit says" is how a base-commit guarantee stops holding."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return {}
    # `UnicodeDecodeError` is a `ValueError`, not an `OSError`: without it here, non-UTF-8 bytes
    # escape as a raw decode error past every caller that fails closed on `LibrarianError`.
    except (OSError, UnicodeDecodeError) as ex:
        raise LibrarianConfigError(
            f"{path} could not be read ({ex.__class__.__name__}) — it is the deployed doorbell's "
            f"own scope -> steward map, and an unreadable one must not be mistaken for 'nobody is "
            f"on call'") from ex
    return _parse_stewards(text, path)


def _parse_stewards(text: str | None, label: str) -> dict:
    """The stewards map's shape rules, shared by both readers.

    Missing -> an empty map: no scope resolves to anyone, which `record_undeliverable` makes
    visible. Malformed JSON stays LOUD — an unparseable file must not look identical to that
    honest "nobody is on call yet" case.
    """
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as ex:
        raise LibrarianConfigError(
            f"{label} is not valid JSON ({ex}) — it is the doorbell's own scope -> steward map, "
            f"and a broken one must not be mistaken for 'nobody is on call'") from ex
    if not isinstance(data, dict):
        raise LibrarianConfigError(
            f"{label} must be an object mapping a zone path prefix (or \"*\") to a steward email "
            f"or a list of steward emails")
    return data


def check_linter_at(repo: str, base: gitcmd.BaseRef) -> None:
    """The contract linter must be IN the commit the worktrees branch from. Startup, once.

    Fail-closed and loud, before a single item is claimed: per-item validation would produce N
    identical `failed` rows with the real cause buried under attempts-exhausted noise.
    """
    if gitcmd.blob_size(repo, base.sha, config.LINTER_RELPATH) < 0:
        raise LibrarianConfigError(_missing_linter(base))


@contextmanager
def linter_at(repo: str, base: gitcmd.BaseRef):
    """The contract linter, materialized from `base`, for the life of the block.

    Per item rather than per run: `processing.process_item` resolves `base` per item, so a linter
    fix pushed between two polls takes effect without a restart. `gates.gate_contract` executes it
    under `gitcmd.base_env()` — it is code out of the curated repo and never sees the App key or
    the queue DSN.
    """
    text = read_at(repo, base, config.LINTER_RELPATH)
    if text is None:
        raise LibrarianConfigError(_missing_linter(base))
    with _materialized(text, config.LINTER_RELPATH) as path:
        yield path


# The meeting distiller's brief, sibling to `agent.SKILL_RELPATH`. Hand-mirrored rather than
# imported: this module sits below `agent` in the import graph. If one changes, change the other.
#
# There is deliberately NO `load_meeting_brief` reader here: the brief is read off the detached
# worktree at `base.sha` by `agent.read_meeting_brief`, and a second reader would build the
# agent's prompt from one read while the file it is TOLD to open is another.
MEETING_BRIEF_RELPATH = ".claude/skills/meeting-distiller/SKILL.md"


def _missing_linter(base: gitcmd.BaseRef) -> str:
    """One sentence, two callers, so a linter that vanishes between the startup check and the
    per-item materialization cannot be described two different ways."""
    return (f"the contract linter is not in the commit the worktrees branch from "
            f"({where(base, config.LINTER_RELPATH)}) — it is the knowledge repo's own gate and the "
            f"librarian will not file without it. {push_or_commit_hint(base)}")
