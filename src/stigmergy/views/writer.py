"""views.writer — the one commit path for a view, authored by the App bot always, never a
steward.

Depends only on `stigmergy.librarian.gitcmd` / `.errors` / `.githubapp` — never on
`stigmergy.entities`: the worker-triggered path would otherwise make the unattended worker
transitively depend on the steward's CLI package. The two guard checks below are a declared,
small duplication of `entities.clone`'s rather than an import in the wrong direction.

A view write passes ZERO of the librarian's gates, by ruling: a view is a synthesis over pages
that already passed them, its frontmatter and path are code-composed, and wholesale regeneration
is exactly the shape `gate_body_rewrite` exists to refuse; the knowledge repo's CI still runs the
contract linter over every commit. The ruling EXPIRES the moment a view reads anything that is
not a filed page — a change to `skeleton.py`'s or `synthesis.py`'s inputs.
"""
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


def repo_slug(repo: str) -> str:
    """`owner/name` from the checkout's `origin`, for the App's push URL. Deliberately
    duplicates `librarian.processing._repo_slug` — three lines, private there."""
    url = gitcmd.origin_url(repo)
    slug = url.rsplit(":", 1)[-1] if url.startswith("git@") else url.split("github.com/")[-1]
    return slug.removesuffix(".git")


def commit_and_push(repo: str, *, branch: str, message: str) -> str:
    """Stage and commit everything as the App bot, then push. Returns the sha that actually
    landed — `gitcmd.push` rebases and retries on a race, so it may differ from the one
    committed. The caller runs the two guards above where they apply (the steward path)."""
    author_name, author_email = githubapp.identity()
    gitcmd.commit(repo, message=message, author_name=author_name, author_email=author_email)
    remote_url, config_env = "", {}
    if githubapp.configured():
        slug = repo_slug(repo)
        remote_url = githubapp.push_url(slug)
        config_env = githubapp.push_config(githubapp.installation_token(), slug)
    return gitcmd.push(repo, branch=branch, remote_url=remote_url, config_env=config_env,
                       author_name=author_name, author_email=author_email)
