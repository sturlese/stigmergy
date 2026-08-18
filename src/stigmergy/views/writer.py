"""views.writer — the one commit path for a view, authored by the App bot always, never a
steward.

Depends only on `stigmergy.librarian.gitcmd` / `.errors` / `.githubapp` — never on
`stigmergy.entities`: the worker-triggered path would otherwise make the unattended worker
transitively depend on the steward's CLI package. The two guard checks below are a declared,
small duplication of `entities.clone`'s rather than an import in the wrong direction.

A view write passes ZERO of the librarian's gates, by ruling: a view is a synthesis over pages
that already passed them, its frontmatter and path are code-composed, and wholesale regeneration
is exactly the shape `gate_body_rewrite` exists to refuse.

**That premise's coverage has moved, so the compensating control has to be named.** "Pages that
already passed the gates" is true of librarian-filed pages and NOT of hand-committed ones, and the
periodic convergence pass covers a hand edit in the knowledge repo on purpose. What stands in for
the gates on that road is the KNOWLEDGE REPO's own PR CI, which runs the contract linter — the
same one `gates.gate_contract` executes — over every commit, and validates `entity:` against the
registry. That control is load-bearing for an unattended writer: it is why a hand edit cannot
anchor a page to an entity nobody minted, and therefore why a synthesis over hand-written pages is
still a synthesis over governed ones. The ruling EXPIRES on either of two changes: a view reading
anything that is not a filed page (a change to `skeleton.py`'s or `synthesis.py`'s inputs), or the
knowledge repo ceasing to run that linter on every path into `main`.
"""
from dataclasses import dataclass

from stigmergy.librarian import gitcmd, githubapp
from stigmergy.librarian.errors import GitError


class ViewWriteError(GitError):
    """A steward-facing refusal from the view writer (dirty tree, wrong branch)."""


def ensure_on_branch(repo: str, branch: str) -> None:
    """Refuse unless HEAD is `branch` — a steward's clone only; the worker's ephemeral worktree
    is always a fresh, detached checkout and is never asked this."""
    head = gitcmd.run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo, check=False).stdout.strip()
    if head != branch:
        where = repr(head) if head else "an unreadable HEAD"
        raise ViewWriteError(
            f"refusing to regenerate — your clone at {repo} is on {where}, not {branch!r}, and "
            f"a view commit lands on HEAD and pushes it to {branch}. Run `git -C {repo} "
            f"switch {branch}` first, then re-run this command")


def ensure_clean(repo: str) -> None:
    """Refuse a dirty working tree: the commit is `git add --all`, so anything lying around
    would land inside a commit whose message says it only regenerated a view."""
    dirty = gitcmd.run("status", "--porcelain", cwd=repo).stdout.strip()
    if not dirty:
        return
    count = len(dirty.splitlines())
    raise ViewWriteError(
        f"refusing to regenerate — your local clone at {repo} has {count} uncommitted change(s), "
        f"and a view commit stages everything in the working tree. Commit or stash first "
        f"(`git -C {repo} status` to see what is pending), then re-run this command")


@dataclass(frozen=True)
class Landed:
    """What one view commit ended up as. `rebased` is the fact a BATCH caller needs and a
    single-entity one can ignore: `gitcmd.push` answers a lost race by fetching and rebasing this
    worktree onto `FETCH_HEAD`, which checks somebody else's commits into the tree — so a caller
    reading anything it parsed before that point is now reading a description of a different
    working tree."""
    sha: str
    rebased: bool


def commit_and_push(repo: str, *, branch: str, message: str) -> Landed:
    """Stage and commit everything as the App bot, then push.

    `Landed.sha` is the sha that actually landed — `gitcmd.push` rebases and retries on a race, so
    it may differ from the one committed, and that DIFFERENCE is exactly `rebased`: a rebase
    rewrites the commit, so an unchanged sha means nothing foreign entered the tree and a changed
    one means something did. Derived from the two shas rather than reported by `gitcmd.push`,
    whose `-> str` contract three packages depend on. The caller runs the two guards above where
    they apply (the steward path).
    """
    author_name, author_email = githubapp.identity()
    committed = gitcmd.commit(repo, message=message, author_name=author_name,
                              author_email=author_email)
    remote_url, config_env = "", {}
    if githubapp.configured():
        slug = githubapp.repo_slug(repo)
        remote_url = githubapp.push_url(slug)
        config_env = githubapp.push_config(githubapp.installation_token(), slug)
    landed = gitcmd.push(repo, branch=branch, remote_url=remote_url, config_env=config_env,
                         author_name=author_name, author_email=author_email)
    return Landed(sha=landed, rebased=landed != committed)
