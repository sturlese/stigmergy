"""The steward's own clone: the checks before a push, and the push that survives a race.

This is a HUMAN's working copy: nothing here repairs, resets, stashes or discards — it REFUSES
and says what to do, because a tool that tidies a working tree can throw away work no commit
holds. Everything acts on HEAD, so HEAD is checked first (`ensure_on_branch`): the other guards
only describe what will be pushed when HEAD is the branch they ask about, and the push also names
`refs/heads/{branch}` explicitly so the refspec cannot publish a ref nothing validated. Git goes
through `librarian.gitcmd` — the one dialect, stderr truncated and credential-bearing URLs
scrubbed — never a second subprocess wrapper. `gitcmd.push` is NOT reused: this commit carries a
registry DERIVED from the state of the branch, so each retry must fetch, rebase, re-run the
generator and amend before pushing again. Never force-pushes, on any attempt, behind any flag —
the only thing `--force` could buy is overwriting somebody else's commit on the governed branch.
"""
import os
import time
from dataclasses import dataclass

from stigmergy.entities.errors import CloneStateError, PushRaceError
from stigmergy.librarian import gitcmd
from stigmergy.librarian.errors import GitError

# Lower than `gitcmd.PUSH_ATTEMPTS` on purpose: that budget belongs to an unattended worker. This
# runs in front of a person, each attempt costs a fetch + rebase + regeneration, and a branch
# moving that fast means somebody else is working on it right now.
MAX_PUSH_ATTEMPTS = 3
PUSH_BACKOFF_BASE_S = 0.2


@dataclass(frozen=True)
class Wording:
    """The subject-specific phrases `identity`/`commit_and_push`/`_rebase_onto_remote` compose
    their refusal messages from.

    Parameterized because the git machinery is generic and the sentences are not: hardcoded
    entity-birth wording becomes a lie the moment a second caller reuses this module for a
    different governed write. Every field defaults to the entity-birth wording, so this package's
    own call sites pass no `wording` at all."""
    identity_governs: str = "approved an entity is what makes entity birth governed at all"
    conflict_target: str = "the same entity page or the same registry entry"
    conflict_kind: str = "an identity decision"
    conflict_owner: str = "a steward has to make"
    conflict_next: str = "decide whether this entity still needs creating"
    race_middle: str = ", and nothing about this entity conflicted with anything"
    race_tail: str = ("once the branch settles, or check whether another steward is approving at "
                      "the same time")


# The default used everywhere a caller does not pass its own — named so a reader can find, by
# name, which wording an unparameterized call resolves to.
ENTITY_WORDING = Wording()


# Every leg that talks to a remote is bounded: these functions also run inside an HTTP request on
# a server-driven mint, where an unanswered remote pins a worker.
NETWORK_TIMEOUT_S = 60


def identity(repo: str, *, action: str = "approve", wording: Wording = ENTITY_WORDING
            ) -> tuple[str, str]:
    """`(name, email)` from the clone's own git config — the STEWARD's identity, not the App's.

    Refused rather than defaulted when unset: `git blame` must answer "who approved this
    identity" with a human, and every available fallback is a lie (the App = a bot,
    `user@hostname` = a laptop).
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

    `--porcelain` covers staged, unstaged AND untracked, and all three matter: anything lying
    around would be swept into the commit, signed by the steward, under a message that says it
    created an entity.
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
    """Refuse unless HEAD *is* `branch` — the check that makes the other guards mean what they
    say, since the commit and the push act on HEAD while `ensure_in_sync` asks about `branch`.

    A detached HEAD reports the literal string `HEAD`, refused too and it must stay refused:
    committing onto it would leave the entity on no branch at all once the push was rejected.
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
    """Refuse a local branch that has diverged from its remote; the message names the direction.

    BEHIND: the registry would be regenerated from a tree missing what landed, so the commit could
    un-register an entity somebody else just approved. AHEAD: local commits nobody has seen would
    be published silently alongside this one. A clone with no remote is in sync by definition —
    the offline/bare-remote case; refusing it would make this path untestable without a network.
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

    `ensure_on_branch` runs first — cheapest, and the precondition the others are stated against:
    a wrong-branch clone must be refused before anything reports on a ref it is not standing on.
    `wording` threads only into `identity`; the other checks are already generic.
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

    `regenerate` is a zero-argument callable run after each rebase — the whole reason this is not
    `gitcmd.push`: the commit contains a DERIVED file, and once the tree underneath moves the
    derivation must be redone or the commit publishes a registry that does not describe the pages
    beside it. Injected, so this stays a git module and a test can drive the race without a
    generator. The callable may REFUSE, and its exception is not caught here: a rebase also moves
    the FACTS the caller's gate was checked against, and swallowing the refusal would launder a
    governance gate the caller passed once and would now fail — the commit is left local and
    unpushed, exactly as the exhausted-attempts case leaves it. `on_retry` fires only when a retry
    actually happens.
    """
    author_name, author_email = author
    sha = gitcmd.commit(repo, message=message, author_name=author_name,
                        author_email=author_email)
    if not gitcmd.run("remote", cwd=repo, check=False).stdout.strip():
        return sha     # nothing to push to: a local-only clone is already "landed"

    for attempt in range(1, attempts + 1):
        # `refs/heads/{branch}` on both sides, never `HEAD:` — the guards validated the BRANCH, so
        # naming the ref makes the thing pushed the thing they checked; an edit that drops
        # `ensure_on_branch` then fails to push rather than publishing the wrong ref quietly.
        pushed = gitcmd.run("push", "origin", f"refs/heads/{branch}:refs/heads/{branch}",
                            timeout=NETWORK_TIMEOUT_S,
                            cwd=repo, check=False)
        if pushed.returncode == 0:
            # Re-read rather than trusting `sha`: a rebase on an earlier iteration rewrote the
            # commit, and this value is recorded as where the entity was born.
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
            # `--amend`, not a second commit: the page and its registry land in ONE commit, and a
            # race is not a reason to publish two.
            gitcmd.run("add", "--all", cwd=repo)
            gitcmd.run("commit", "--quiet", "--no-verify", "--amend", "--no-edit", cwd=repo,
                       env={"GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email,
                            "GIT_COMMITTER_NAME": author_name, "GIT_COMMITTER_EMAIL": author_email})
        time.sleep(PUSH_BACKOFF_BASE_S * (2 ** (attempt - 1)))
    raise PushRaceError("push loop exited without a verdict")   # unreachable; no silent success


def _rebase_onto_remote(repo: str, branch: str, *, wording: Wording = ENTITY_WORDING) -> None:
    """Fetch and replay this commit on top of whatever landed. A genuine conflict is NOT
    resolved: it means somebody else's commit touched the same entity page or registry entry —
    precisely the identity collision this subsystem exists to make a human decide.
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

    Refuses to overwrite: a file appearing between the collision check and this write is a race
    with a human editing their own clone, and clobbering it would destroy work this tool never saw.
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

    The one deletion in this module, bounded to a file THIS process created moments ago, by
    absolute path — never a glob, never `git clean`. Without it a failed approval leaves an
    untracked page behind and the next `approve` refuses on a dirty tree, making the retry
    impossible.
    """
    try:
        os.remove(path)
    except OSError:
        pass


__all__ = ["ENTITY_WORDING", "MAX_PUSH_ATTEMPTS", "Wording", "commit_and_push",
           "discard_untracked", "ensure_clean", "ensure_in_sync", "ensure_on_branch", "head",
           "identity", "preflight", "write_page", "GitError"]
