"""The three repo-sourced inputs the fast lane reads — **at the commit it files against**.

`ops/acl.json`, `ops/entity-registry.json` and `.claude/tools/stigmergy_lint.py` used to resolve
against the local WORKING TREE of `settings.repo` while the diff they judge comes from a worktree
built at `base` (`origin/<branch>`, normally). So an uncommitted local edit to any of them changed
a run's behaviour without changing the commit being filed against — and for `acl.json` that meant
the audience labels stamped onto a page could disagree with the commit the page landed in.

**Why it is closed rather than tolerated.** `ops/entity-registry.json` is the output of a governed
flow: entities are born through the steward CLI, which materializes the page and regenerates the
registry in one commit signed by a human. Against that, a working-tree read is a read *around* the
steward's gate — an uncommitted local edit could anchor captures to an entity nobody approved.
That is a governance bypass, not a hygiene asymmetry. Two further consequences: a deployed worker
has no working tree anybody edits, so tree semantics would exist only in the one environment
production never runs in; and a filed page's stamps become reproducible from history.

`worker._check_skill_at` had already taken exactly this correction — *"a check that can pass while
the thing it checks is absent is worse than no check"* — and these three are now symmetric with
it. The cost is accepted and named: testing an uncommitted linter edit through a live run stops
working. The linter has its own suites for that.

**Three shapes, deliberately not one patch**, because the three inputs are three different kinds
of thing:

- **the ACL config** is data with a data-level parser, so it gets a `load_text` seam
  (`acl_rules.load_text` -> `kernel.acl.load_acl_config_text`) and never becomes a file at all;
- **the registry**'s parser is `kernel.registry.load_registry`, existing tested code that takes a
  PATH. Materializing the blob to a temp file keeps that cross-package contract untouched for one
  caller's benefit;
- **the linter** is a SCRIPT — it is executed, not parsed — so it has to exist as a file, and the
  file is written from the base blob per item.

**Missing keeps its meaning in every case.** No `ops/acl.json` at the base commit is an open
corpus; no registry is an empty registry (the graph works unregistered); a missing linter is the
same loud fail-closed refusal it always was. What changes is only *where the question is asked*.
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
# swept out from under the run that is executing it. Same reasoning as
# `config.REFUSED_DIFF_DIRNAME`, and belt-and-braces beside `gitcmd.reapable`'s full-name match.
_TEMP_PREFIX = "stigmergy-base-input-"


def where(base: gitcmd.BaseRef, relpath: str) -> str:
    """`origin/main@abc123def456:ops/acl.json` — how every message in this module names an input.

    One spelling, shared with `worker._check_skill_at`, because these refusals are read side by
    side in a terminal and an operator should not have to learn two layouts for the same fact.
    """
    return f"{base.describe()}:{relpath}"


def push_or_commit_hint(base: gitcmd.BaseRef) -> str:
    """Why a file that is on disk can still be absent at `base`, and what to do about it.

    The single most common cause of every refusal here, and the one an operator cannot guess:
    the worktrees branch from the REMOTE, so a commit that exists only locally might as well not
    exist. Shared by the skill check and the linter check rather than written twice — they are the
    same sentence about the same surprise (see the runbook's "Two things that will surprise you
    once").
    """
    if base.remote:
        return (f"Push the commit that adds it: the worktree is built from {base.ref}, not from "
                f"your local checkout.")
    return (f"Commit it on {base.ref}: the worktree is built from that branch, not from the "
            f"working tree.")


# ── the shared base: one place that knows what "read at the base commit" means ────────────────
def read_at(repo: str, base: gitcmd.BaseRef, relpath: str) -> str | None:
    """The content of one repo-sourced input AT `base`, or `None` when that commit has no such path.

    `None` is what the three wrappers below each translate into their own "the file is not there"
    semantics, so absence keeps meaning exactly what `os.path.exists` used to answer.

    `blob_size` first rather than catching `GitError` from `show`, on purpose: "this commit does
    not carry the file" is a legitimate configuration and "the read failed" is a fault, and a
    single `except` around `git show` would collapse the two into one message. It also mirrors
    `_check_skill_at`, which sizes the blob before reading it so a ceiling can be applied to
    content that has not been loaded yet.
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
    """`text` as a real file named after `relpath`, for the life of the block.

    Two of the three inputs need a path rather than bytes — one because the reader that owns its
    format takes a path, one because it is a script that gets executed. The basename is preserved
    so anything that reports the file (the registry reader's own `ValueError`, a traceback out of
    the linter) names something recognizable.
    """
    with tempfile.TemporaryDirectory(prefix=_TEMP_PREFIX) as tmp:
        path = os.path.join(tmp, relpath.rsplit("/", 1)[-1])
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        yield path


# ── the three wrappers: one per input, each keeping its own semantics ──────────────────────────
def load_acl(repo: str, base: gitcmd.BaseRef):
    """`ops/acl.json` at `base`, through the librarian's dialect adapter.

    No file anywhere: `acl_rules.load_text` parses the content and hands the reader's normalized
    config back. A commit with no ACL config is an open corpus, which is `acl.py`'s own semantics
    for a missing one.
    """
    relpath = config.ACL_RELPATH
    return acl_rules.load_text(read_at(repo, base, relpath), label=where(base, relpath))


def load_registry(repo: str, base: gitcmd.BaseRef):
    """`ops/entity-registry.json` at `base`, loaded by the kernel's own reader.

    Materialized rather than re-parsed here: `registry.load_registry` is existing, tested code, and
    giving the kernel a data-level entry point for one caller in another package would be a
    cross-package contract change made for convenience.

    **A malformed registry is answered by a SENTENCE and not a traceback.** `load_registry` raises
    a bare `ValueError` (and `OSError`/`JSONDecodeError` from the read), none of them a
    `LibrarianError`, so `cli.main` — which catches those — would let every one through to the
    operator's terminal as a stack trace. Same defect `acl_rules._guard_delegation` closes on the
    file loaded one line above it.

    The reader names the file it read, and here that is a temp path nobody can open; the locator
    an operator can act on is the base-commit one, so it is substituted into the reader's own
    words rather than replacing them.
    """
    relpath = config.REGISTRY_RELPATH
    label = where(base, relpath)
    text = read_at(repo, base, relpath)
    if text is None:
        # Missing -> empty registry, exactly what `load_registry(None)` answers: the graph works
        # unregistered, and every name then parks as an entity situation rather than anchoring.
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
    """`ops/stewards.json` at `base`: the doorbell's scope -> steward-emails map,
    read at the base commit like `load_acl`/`load_registry` above — never the working tree,
    so an uncommitted local edit cannot change who the doorbell rings for without changing the
    commit that governs the corpus.

    **Missing -> an empty map, not an error.** No stewards file at all means no scope resolves to
    anyone — the doorbell's own `record_undeliverable` path is what makes that honestly visible,
    and crashing here would be a worse failure mode than "nobody is on call yet" for a fresh
    checkout that has not added the file.

    **Malformed JSON is a configuration fault and stays loud**, exactly like `load_acl`'s own
    posture for `ops/acl.json`: a broken stewards file must not be silently read as "no one
    resolves for any scope" (which would look identical to `record_undeliverable`'s honest case)
    when the real problem is that the file cannot be parsed at all.
    """
    relpath = config.STEWARDS_RELPATH
    return _parse_stewards(read_at(repo, base, relpath), where(base, relpath))


def load_stewards_file(path: str) -> dict:
    """The same map from a PLAIN FILE — the deploy-time snapshot a process with no checkout reads
    (`server.review.load_stewards`'s fallback). Same three postures as the git read
    above, deliberately: absent is an empty map, malformed is loud, and the shape is checked. It
    is a separate entry point rather than a `repo=""` branch of the reader above because the two
    answer different questions — "what did the commit governing this corpus say" and "what did the
    image this process is running ship with" — and a reader that silently answered the second when
    asked the first is how a base-commit guarantee quietly stops holding."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return {}
    except OSError as ex:
        raise LibrarianConfigError(
            f"{path} could not be read ({ex.__class__.__name__}) — it is the deployed doorbell's "
            f"own scope -> steward map, and an unreadable one must not be mistaken for 'nobody is "
            f"on call'") from ex
    return _parse_stewards(text, path)


def _parse_stewards(text: str | None, label: str) -> dict:
    """The stewards map's shape rules, shared by both readers.

    **Missing -> an empty map, not an error.** No stewards file at all means no scope resolves to
    anyone — the doorbell's own `record_undeliverable` path is what makes that honestly visible,
    and crashing here would be a worse failure mode than "nobody is on call yet" for a fresh
    checkout that has not added the file.

    **Malformed JSON is a configuration fault and stays loud**, exactly like `load_acl`'s own
    posture for `ops/acl.json`: a broken stewards file must not be silently read as "no one
    resolves for any scope" (which would look identical to `record_undeliverable`'s honest case)
    when the real problem is that the file cannot be parsed at all.
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

    Per item rather than once per run, and that is not incidental: `processing.process_item`
    resolves `base` per item, so the linter that runs is always the one in the commit the diff was
    actually built from — including a linter fix pushed between two polls, which takes effect
    without a restart exactly like a registry entry does.

    The script is executed with `gitcmd.base_env()` by `gates.gate_contract`: it is code out of
    the repo the librarian curates, so it never sees the App key or the queue DSN. Materializing
    it does not change that — it changes only WHICH bytes run, from "whatever is in the operator's
    working tree" to "what the commit being filed against says".
    """
    text = read_at(repo, base, config.LINTER_RELPATH)
    if text is None:
        raise LibrarianConfigError(_missing_linter(base))
    with _materialized(text, config.LINTER_RELPATH) as path:
        yield path


# The meeting distiller's brief, sibling to `agent.SKILL_RELPATH`. Hand-mirrored rather than
# imported from `agent` — same reason `agent.skill_path`'s own docstring gives for keeping the
# librarian skill's relpath OUT of `config`: this module sits below `agent` in the import graph
# (`agent` does not import `base_inputs`, and adding the reverse edge here to save one string
# would be the wrong direction to bend it). If one changes, this comment is the pointer to the
# other.
#
# **There is deliberately NO `load_meeting_brief` reader here**, unlike the ACL config, the
# registry and the linter above. The brief is read by `SdkAgent._run_meeting` via
# `agent.read_meeting_brief(worktree_root)`, the SAME pattern the ordinary flow's skill uses.
# That worktree is checked out `--detach` at `base.sha` by `gitcmd.ephemeral_worktree`, and the
# agent can neither write `.claude/` (`agent.confined_write`) nor survive a corrective retry's
# `reset --hard HEAD` (`processing._reset_for_retry`) — so reading it off the worktree IS reading
# it at the base commit, by the same mechanism the ordinary skill already relies on. Adding a
# second reader here would build the agent's PROMPT from one read while the file it is TOLD to
# open with `Read` (`{relpath}` in `MEETING_SYSTEM_PROMPT_HEADER`) is another: two sources of
# truth for one brief.
MEETING_BRIEF_RELPATH = ".claude/skills/meeting-distiller/SKILL.md"


def _missing_linter(base: gitcmd.BaseRef) -> str:
    """One sentence, two callers (the startup check and the per-item materialization), so a linter
    that vanishes between them cannot be described two different ways."""
    return (f"the contract linter is not in the commit the worktrees branch from "
            f"({where(base, config.LINTER_RELPATH)}) — it is the knowledge repo's own gate and the "
            f"librarian will not file without it. {push_or_commit_hint(base)}")
