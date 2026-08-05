"""The steward's own clone: the checks before a push, and the push that survives a race.

**This is a HUMAN's working copy, and that governs every decision here.** The librarian works in a
throwaway worktree it created and may destroy; this module operates on the checkout the operator
edits by hand. So nothing in it repairs, resets, stashes or discards: it REFUSES and says what to
do. A tool that tidies a working tree can throw away work no commit holds, and it would do so at
the exact moment its operator was thinking about something else.

**Everything here acts on HEAD, so HEAD is checked before anything else** (`ensure_on_branch`,
the first check in `preflight`). A guard that asks git about the branch NAMED `main` while the
commit, the rebase and the push all act on whatever HEAD happens to be is a guard answering a
question about a branch nobody is on: a steward sitting on a feature branch with a clean tree and
a synced local `main` passes every check, and the push publishes their private commits to `main`
as well. Worse, a feature branch that does not descend from `origin/main` makes the push fail and
sends `_rebase_onto_remote` through the steward's OWN branch history — this module rewriting
commits it does not own, which is the one thing a tool operating on somebody's working copy must
never do. Two independent defences, because a guard and a refspec are different kinds: HEAD must
BE the branch, and the push names `refs/heads/{branch}` explicitly so the refspec itself cannot
publish a ref nothing validated.

**Why this reaches into `librarian.gitcmd` instead of wrapping `subprocess` again.**

`gitcmd` is where this repository's one git dialect lives: the binary invocation, `capture_output=
True, text=True`, a non-zero return code raising `GitError` with stderr TRUNCATED and, crucially,
SCRUBBED of any credential-bearing URL (`_scrub`). A second `subprocess.run(["git", ...])` wrapper
here would be a second dialect and a second place to remember that a push URL can carry a token —
the "two dialects for one fact" defect this codebase keeps paying to fix (`RECLAIM_NOW`,
`depth_line`, `format_ms`, `SEARCHABILITY_NOTE` are all the same lesson). Same posture as
`librarian.acl_rules` adapting `kernel.acl`'s matcher: import and adapt, never rewrite, with the
reach declared rather than incidental.

What is NOT reused is `gitcmd.push`, and that is the interesting half. Its retry rebases and
pushes again, which is right for a page — a page is the same page after a rebase. It is WRONG for
this commit: the commit carries a page *and the registry derived from the state of the branch*, so
a rebase onto a moved `origin/main` can leave a registry that no longer describes the tree it sits
in. The loop below therefore fetches, re-runs the generator after every rebase, and amends before
retrying.

**Never force-pushes.** Not as a fallback, not on the last attempt, not behind a flag. After a
successful rebase the push is a fast-forward and needs no force; if it still fails, the honest
answer is a message, because the only thing `--force` could buy here is overwriting somebody
else's commit on the branch this system's whole governance story rests on.
"""
import os
import time
from dataclasses import dataclass

from stigmergy.entities.errors import CloneStateError, PushRaceError
from stigmergy.librarian import gitcmd
from stigmergy.librarian.errors import GitError

# Three attempts, so at most two retries ("retrying (1/2)", then "could not push after 3
# attempts"). Lower than `gitcmd.PUSH_ATTEMPTS` (6) on purpose: that budget belongs to an
# unattended worker draining a queue, where patience is free. This one runs in front of a person
# who is watching, and each attempt costs a fetch, a rebase AND a regeneration — after three, "try
# again in a moment" is better advice than a longer wait, because the branch moving that fast means
# somebody else is working on it right now.
MAX_PUSH_ATTEMPTS = 3
PUSH_BACKOFF_BASE_S = 0.2


@dataclass(frozen=True)
class Wording:
    """The subject-specific phrases `identity`/`commit_and_push`/`_rebase_onto_remote` compose
    their refusal messages from.

    Parameterized rather than hardcoded to entity-birth language, because the git machinery below
    is generic and the sentences around it are not: hardcoding them makes every message a lie the
    moment a second caller reuses this module for a DIFFERENT governed write. `git blame` naming an
    approver "is what makes entity birth governed" says nothing true about, say, an edit to an
    operations document, and "another steward is approving" points a single-operator reader at a
    colleague who does not exist.

    A parameterized module beats a second, near-duplicate one: every field here defaults to the
    entity-birth wording, so `entities/cli.py`'s own call sites pass no `wording` at all. A caller
    with a different write story builds its own `Wording` and passes it through."""
    identity_governs: str = "approved an entity is what makes entity birth governed at all"
    conflict_target: str = "the same entity page or the same registry entry"
    conflict_kind: str = "an identity decision"
    conflict_owner: str = "a steward has to make"
    conflict_next: str = "decide whether this entity still needs creating"
    race_middle: str = ", and nothing about this entity conflicted with anything"
    race_tail: str = ("once the branch settles, or check whether another steward is approving at "
                      "the same time")


# The default used everywhere a caller does not pass its own — named so a reader of `entities/
# cli.py` (which never constructs one) can still find, by name, which wording its calls resolve to.
ENTITY_WORDING = Wording()


# Every leg of this module that talks to a remote is bounded. The CLI could afford to wait
# forever — a human is watching it — but since ADR 030 the same functions run inside an HTTP
# request on a server-driven mint, where an unanswered remote pins a worker (audit M2).
NETWORK_TIMEOUT_S = 60


def identity(repo: str, *, action: str = "approve", wording: Wording = ENTITY_WORDING
            ) -> tuple[str, str]:
    """`(name, email)` from the clone's own git config — the STEWARD's identity, not the App's.

    This is the governance: `git blame` on an entity page has to answer "who approved this
    identity" with a human. Refused rather than defaulted when unset, because every fallback
    available is a lie — the App's identity would attribute a human's judgment to a bot, and a
    machine-derived `user@hostname` would attribute it to a laptop.
    """
    name = gitcmd.run("config", "user.name", cwd=repo, check=False).stdout.strip()
    email = gitcmd.run("config", "user.email", cwd=repo, check=False).stdout.strip()
    if not name or not email:
        missing = " and ".join(n for n, v in (("user.name", name), ("user.email", email)) if not v)
        raise CloneStateError(
            f"your clone at {repo} has no git {missing} configured, and `{action}` commits with "
            f"YOUR identity rather than the librarian's — `git blame` naming the human who "
            f"{wording.identity_governs}. Set it with `git -C "
            f"{repo} config user.name \"Your Name\"` (and user.email), then re-run this command")
    return name, email


def ensure_clean(repo: str, *, action: str = "approve") -> None:
    """Refuse a working tree with anything uncommitted in it.

    `--porcelain` covers staged, unstaged AND untracked, and all three matter: this command is
    about to `git add` the page and the registry and commit, so anything else lying around would
    be swept into the same commit — a steward's half-finished edit to an unrelated page, signed by
    them, inside a commit whose message says it created an entity.
    """
    dirty = gitcmd.run("status", "--porcelain", cwd=repo).stdout.strip()
    if not dirty:
        return
    count = len(dirty.splitlines())
    raise CloneStateError(
        f"refusing to {action} — your local clone at {repo} has {count} uncommitted change(s). "
        f"`{action}` commits and pushes with your own git identity, and anything already in the "
        f"working tree would land in that commit too; commit or stash first (`git -C {repo} "
        f"status` to see what is pending), then re-run this command")


def ensure_on_branch(repo: str, branch: str, *, action: str = "approve") -> None:
    """Refuse unless HEAD *is* `branch`. The cheapest check here and the most consequential.

    `ensure_in_sync` asks about the local branch NAMED `branch`; `gitcmd.commit` commits on HEAD and
    the push publishes it. When those are two different refs every downstream guard is answering a
    question about a branch nobody is on, and the push carries whatever the steward's current branch
    holds. So this is not "a nicer error for a rare state": it is the check that makes the other two
    mean what they say.

    A detached HEAD reports the literal string `HEAD`, which this comparison refuses too, and must
    keep refusing — committing onto a detached HEAD would leave the entity on no branch at all once
    the push was rejected.
    """
    head = gitcmd.run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo, check=False).stdout.strip()
    if head != branch:
        where = repr(head) if head else "an unreadable HEAD"
        raise CloneStateError(
            f"refusing to {action} — your clone at {repo} is on {where}, not {branch!r}, and "
            f"`{action}` commits on HEAD and pushes it to {branch}: everything on {where} that "
            f"is not on {branch} would be published too. Run `git -C {repo} switch {branch}` "
            f"first, then re-run this command")


def ensure_in_sync(repo: str, branch: str, *, action: str = "approve") -> None:
    """Refuse a local branch that has diverged from its remote.

    Both directions are refused and for different reasons, so the message names which it is.
    BEHIND: the registry would be regenerated from a tree that is missing whatever landed on the
    remote, so the commit could un-register an entity somebody else just approved. AHEAD: there
    are local commits nobody has seen, and pushing would publish them silently alongside this one.

    A clone with no remote at all is in sync by definition — that is the offline/bare-remote case
    the suite and the end-to-end run in, and refusing it would make this path untestable without a
    network.
    """
    if not gitcmd.run("remote", cwd=repo, check=False).stdout.strip():
        return
    gitcmd.run("fetch", "--quiet", "origin", branch, cwd=repo, check=False,
               timeout=NETWORK_TIMEOUT_S)
    counts = gitcmd.run("rev-list", "--left-right", "--count",
                        f"{branch}...origin/{branch}", cwd=repo, check=False)
    if counts.returncode != 0:
        return      # no such remote branch yet — the first push creates it
    try:
        ahead, behind = (int(n) for n in counts.stdout.split())
    except ValueError:
        return
    if not ahead and not behind:
        return
    raise CloneStateError(
        f"refusing to {action} — your local {branch} has diverged from origin/{branch} "
        f"({ahead} ahead, {behind} behind). `{action}` pushes straight to {branch} and never "
        f"force-pushes; run `git -C {repo} pull --rebase` first, then re-run this command")


def preflight(repo: str, branch: str, *, action: str = "approve", wording: Wording = ENTITY_WORDING
             ) -> tuple[str, str]:
    """Every check that must pass before anything is written, in cost order. Returns the identity.

    Ordered cheapest-and-most-common first: the branch check and the identity are config/ref reads,
    dirty is one `status`, and in-sync costs a network fetch. A steward whose clone is dirty AND
    diverged is told about the dirty tree, which is the one they can fix without thinking about the
    remote.

    **`ensure_on_branch` runs before all of them**, and not only because it is cheapest: it is the
    precondition the other three are stated against. `ensure_in_sync` compares the local `branch`
    with its remote, and that comparison only describes what is about to be pushed if HEAD is that
    branch — so a wrong-branch clone must be refused before anything reports on a ref it is not
    standing on.

    `wording` is the ONLY thing that changes for a non-entity caller: `ensure_clean`/
    `ensure_on_branch`/`ensure_in_sync` are already generic (no entity-specific noun — only
    `action` varies their message), so they take no `wording` at all; `identity`'s own message
    does, and it is threaded through here rather than each caller reaching `identity` directly.
    """
    ensure_on_branch(repo, branch, action=action)
    who = identity(repo, action=action, wording=wording)
    ensure_clean(repo, action=action)
    ensure_in_sync(repo, branch, action=action)
    return who


def head(repo: str) -> str:
    return gitcmd.run("rev-parse", "HEAD", cwd=repo).stdout.strip()


def commit_and_push(repo: str, *, branch: str, message: str, author: tuple[str, str],
                    regenerate=None, attempts: int = MAX_PUSH_ATTEMPTS, on_retry=None,
                    wording: Wording = ENTITY_WORDING) -> str:
    """Commit everything staged in `repo` as `author`, then push it to `branch`. Returns the sha.

    `regenerate` is a zero-argument callable run after each rebase, and it is the whole reason this
    is not `gitcmd.push`: the commit contains a DERIVED file, so once the tree underneath it moves,
    the derivation has to be redone or the commit publishes a registry that does not describe the
    pages beside it. It is injected rather than imported so this module stays a git module — and
    so a test can drive the race without a generator at all.

    **That callable is also allowed to REFUSE, and an exception from it is not caught here.** A
    rebase does not only move the derived file, it moves the FACTS the caller's own gate was
    checked against: two stewards approving `Acme` and `Zenith Systems (alias: Acme)` at the same
    moment auto-merge cleanly and produce an ambiguous alias, because the second one's collision
    check ran against a registry that no longer exists. So this loop deliberately gives the caller
    a hook on the one path where that can happen and gets out of the way when it says no — the
    alternative is a retry loop that quietly launders a governance gate the caller already passed
    once and would now fail. The commit is left in the local clone, unpushed, exactly as the
    exhausted-attempts case leaves it.

    `on_retry` is called with a human sentence when a retry actually happens, and only then: a
    line about a race that did not occur is noise in the 999 runs out of 1000 that push first try.
    """
    author_name, author_email = author
    sha = gitcmd.commit(repo, message=message, author_name=author_name,
                        author_email=author_email)
    if not gitcmd.run("remote", cwd=repo, check=False).stdout.strip():
        return sha     # nothing to push to: a local-only clone is already "landed"

    for attempt in range(1, attempts + 1):
        # `refs/heads/{branch}` on both sides, never `HEAD:` — `preflight` validated the BRANCH,
        # and a refspec that reads HEAD would publish whatever HEAD is regardless of what was
        # validated. Naming the ref makes the thing pushed the same thing the guards checked, so a
        # future edit that drops or reorders `ensure_on_branch` fails to push rather than
        # publishing the wrong ref quietly.
        pushed = gitcmd.run("push", "origin", f"refs/heads/{branch}:refs/heads/{branch}",
                            timeout=NETWORK_TIMEOUT_S,
                            cwd=repo, check=False)
        if pushed.returncode == 0:
            # Re-read rather than trusting `sha`: a rebase on an earlier iteration rewrote the
            # commit, and this value is what gets printed and recorded as where the entity was
            # born. The librarian's own push carries the same rule (`gitcmd.push`'s docstring).
            return head(repo)
        if attempt == attempts:
            raise PushRaceError(
                f"could not push to {branch} after {attempts} attempts — origin/{branch} kept "
                f"moving faster than this retry loop could keep up with{wording.race_middle}. "
                f"The commit IS in your local clone "
                f"({head(repo)[:12]}) and nothing was force-pushed: run `git -C {repo} push origin "
                f"{branch}` {wording.race_tail}")
        if on_retry:
            on_retry(f"origin/{branch} moved while committing (another push landed first) — "
                     f"refetching, regenerating and retrying ({attempt}/{attempts - 1})")
        _rebase_onto_remote(repo, branch, wording=wording)
        if regenerate and regenerate():
            # The derived file changed against the new base, so the commit has to carry the new
            # one. `--amend` and not a second commit: the page and its registry land in ONE commit,
            # and a race is not a reason to publish two.
            gitcmd.run("add", "--all", cwd=repo)
            gitcmd.run("commit", "--quiet", "--no-verify", "--amend", "--no-edit", cwd=repo,
                       env={"GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email,
                            "GIT_COMMITTER_NAME": author_name, "GIT_COMMITTER_EMAIL": author_email})
        time.sleep(PUSH_BACKOFF_BASE_S * (2 ** (attempt - 1)))
    raise PushRaceError("push loop exited without a verdict")   # unreachable; no silent success


def _rebase_onto_remote(repo: str, branch: str, *, wording: Wording = ENTITY_WORDING) -> None:
    """Fetch and replay this commit on top of whatever landed. A genuine conflict is NOT resolved.

    Same rule the librarian follows and for a stronger reason: a conflict here means somebody
    else's commit touched the same entity page or the same registry entry, which is precisely the
    identity collision this whole subsystem exists to make a human decide.
    """
    gitcmd.run("fetch", "origin", branch, cwd=repo, check=False, timeout=NETWORK_TIMEOUT_S)
    rebase = gitcmd.run("rebase", "FETCH_HEAD", cwd=repo, check=False)
    if rebase.returncode != 0:
        gitcmd.run("rebase", "--abort", cwd=repo, check=False)
        raise PushRaceError(
            f"another commit on {branch} changed {wording.conflict_target}, "
            f"and this command does not resolve conflicts — that overlap is {wording.conflict_kind}, "
            f"which is the one thing {wording.conflict_owner} personally. Your clone was left "
            f"unchanged (the rebase was aborted); run `git -C {repo} pull --rebase`, look at what "
            f"landed, and {wording.conflict_next}")


def write_page(repo: str, relpath: str, text: str) -> str:
    """Write one page inside the clone. Returns the absolute path.

    Refuses to overwrite: every caller has already checked for a collision, so a file appearing
    between that check and this write is a race with a human editing their own clone, and
    clobbering it would destroy work this tool never saw.
    """
    path = os.path.join(repo, *relpath.split("/"))
    if os.path.exists(path):
        raise CloneStateError(
            f"{relpath} already exists in {repo} and this command never overwrites a page — it "
            f"appeared after the collision check, so something else is writing to this clone")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def discard_untracked(path: str) -> None:
    """Remove a page this command just wrote, after a later step refused.

    The one deletion in this module, and bounded to exactly that: a file THIS process created
    moments ago, by absolute path, never a glob and never `git clean`. Without it a failed
    approval leaves an untracked entity page behind, and the next `approve` refuses on a dirty
    tree — a first failure that makes the retry impossible.

    **One argument, the absolute path**, matching what both call sites pass. A signature carrying a
    leading `repo` nobody passes turns the ONE function whose job is cleaning up after a refusal
    into a `TypeError` the first time a refusal actually reaches it — a rollback that never
    happens, on the only path that needs one.
    """
    try:
        os.remove(path)
    except OSError:
        pass


__all__ = ["ENTITY_WORDING", "MAX_PUSH_ATTEMPTS", "Wording", "commit_and_push",
           "discard_untracked", "ensure_clean", "ensure_in_sync", "ensure_on_branch", "head",
           "identity", "preflight", "write_page", "GitError"]
