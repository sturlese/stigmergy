"""views.writer — the one commit path for a view, authored by the App bot always, never a
steward.

**Layering decision, stated explicitly:** this module depends ONLY on
`stigmergy.librarian.gitcmd` / `.errors` / `.githubapp` — never on `stigmergy.entities`, even though
`entities.clone` already has a near-identical commit/push shape. Reusing it was considered and
rejected: `entities.clone.commit_and_push` is built around the STEWARD's identity
(`clone.identity`, `clone.preflight`) and a co-derived file (`regenerate=`, the registry
re-derivation on rebase) that a view commit has neither of, and — more importantly — the
worker-triggered path would then have `stigmergy.librarian` importing `stigmergy.views` importing
`stigmergy.entities`, making the unattended worker transitively depend on the steward's CLI
package. That is exactly the dependency direction `entities/index.md`'s own "Avoid" section
calls load-bearing to keep one-way. `librarian.gitcmd.push` already provides the
fetch-rebase-retry-never-force-push loop this needs (the same one
`librarian.processing._file_meeting` uses for the meeting flow's own App-bot commit); the two
small guard checks below are a DECLARED, small duplication of `entities.clone`'s
`ensure_on_branch`/`ensure_clean` — the same "stated at both ends rather than resolved by an
import in the wrong direction" pattern `entities/index.md` already uses for a different fact
both packages need.

── WHICH OF THE EIGHT GATES A VIEW WRITE PASSES: NONE ─────────────────────────────────────────

`views.regenerate` performs its own `commit_and_push` and imports no gates at all — zero `gates`
references anywhere in `src/stigmergy/views/`. That is by design, and it is exactly why the meeting
flow's `edits_allowed=False` cannot break it: a view commit never crosses `gate_zone`. But "the
gates are not wired in" is not the same statement as "no gate should apply", and a bounded
agent's synthesis reaching `main` ungated deserves the second one in words.

**The ruling: zero gates, because every one of the eight (`gates.ALL_GATES`) would be answering
a question that is already answered, and three of them would answer it WRONG on this input.**

Gate by gate, which is the only way this is a decision and not a shrug:

* `gate_secrets` / `gate_pii` — a view is a synthesis over pages **that already passed both**
  when they were filed. Its only new prose comes from the bounded agent, which is fed those same
  pages. Re-scanning is not free of consequence either: it would put the whole rollup through
  gitleaks on every regeneration of every entity, including every `--all` backfill.
* `gate_binary_page` — the file is UTF-8 markdown composed by `views.render` and written through
  `kernel.fsutil.write_text_atomic`. No agent hands this module a blob, and there is no path by
  which a view acquires a NUL byte.
* `gate_anchoring` — a view is not anchored, it IS the entity's own rollup. `entity:` on a view
  is self-reference, and the boundary is explicit that an entity's view of what points at it
  stays **derived**.
* `gate_zone` — the write prefixes are `views/`, which no other lane may write and this one
  writes exclusively; the gate's own lane whitelist is `wiki/`, so it would refuse the path
  outright. The zone question has one answer and the writer is the thing that knows it.
* `gate_body_rewrite` — a view is REGENERATED wholesale by design; that is what it means for a
  derived page to be regenerated on change. The gate exists to refuse exactly this shape, so
  wiring it in would refuse every legitimate regeneration.
* `gate_contract` (the linter) — the one with a real argument for it, and it is genuinely
  applied: the knowledge repo's own CI runs the contract linter over every commit including this
  one. What that buys over running it here is a slower signal; what it costs is nothing.
* `gate_frontmatter` / the duplicate check — a view's frontmatter is written by `views.render`,
  not by an agent, and its path is derived from the entity id, so there is no declaration to
  cross-check and no duplicate to find.

**What this decision costs, stated so it is not discovered later**: the moment a view takes any
input the gated pages did not already carry — an external source, a fetched figure, a steward's
free text — this ruling expires and the gates in the first bullet become real. The trigger to
re-open it is *"a view reads something that is not a filed page"*, and that is a change to
`views/skeleton.py`'s or `views/synthesis.py`'s inputs, which is where a future reader will be
standing when it matters.
"""
from stigmergy.librarian import gitcmd, githubapp
from stigmergy.librarian.errors import GitError


class ViewWriteError(GitError):
    """A steward-facing refusal from the view writer (dirty tree, wrong branch)."""


def ensure_on_branch(repo: str, branch: str) -> None:
    """Refuse unless HEAD *is* `branch` — a steward's clone only; the worker's ephemeral worktree
    is always a fresh, detached checkout at the commit it should push from, so this guard is
    never asked of it (see `regenerate.regenerate_entity`'s two call sites)."""
    head = gitcmd.run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo, check=False).stdout.strip()
    if head != branch:
        where = repr(head) if head else "an unreadable HEAD"
        raise ViewWriteError(
            f"refusing to regenerate — your clone at {repo} is on {where}, not {branch!r}, and "
            f"a view commit lands on HEAD and pushes it to {branch}. Run `git -C {repo} "
            f"switch {branch}` first, then re-run this command")


def ensure_clean(repo: str) -> None:
    """Refuse a working tree with anything uncommitted — the same reason `entities.clone.
    ensure_clean` refuses it: the commit is `git add --all` (`gitcmd.commit`), so anything already
    lying around would land inside a commit whose message says it only regenerated a view."""
    dirty = gitcmd.run("status", "--porcelain", cwd=repo).stdout.strip()
    if not dirty:
        return
    count = len(dirty.splitlines())
    raise ViewWriteError(
        f"refusing to regenerate — your local clone at {repo} has {count} uncommitted change(s), "
        f"and a view commit stages everything in the working tree. Commit or stash first "
        f"(`git -C {repo} status` to see what is pending), then re-run this command")


def repo_slug(repo: str) -> str:
    """`owner/name` from the checkout's `origin`, for the App's push URL. Duplicated from
    `librarian.processing._repo_slug` deliberately (three lines, private to that module, not
    worth importing a large processing module for) — see the module docstring's declared-
    duplication rationale."""
    url = gitcmd.origin_url(repo)
    slug = url.rsplit(":", 1)[-1] if url.startswith("git@") else url.split("github.com/")[-1]
    return slug.removesuffix(".git")


def commit_and_push(repo: str, *, branch: str, message: str) -> str:
    """Stage and commit everything in `repo` as the App bot, then push to `branch`. Returns the
    sha that actually landed (which may differ from the one committed — `gitcmd.push` rebases and
    retries on a race, exactly as `librarian.processing._file_meeting`'s own App-bot commit
    does). Caller is responsible for the two guard checks above where they apply (the steward CLI
    path); a worker-triggered call against a fresh ephemeral worktree needs neither."""
    author_name, author_email = githubapp.identity()
    gitcmd.commit(repo, message=message, author_name=author_name, author_email=author_email)
    remote_url, config_env = "", {}
    if githubapp.configured():
        slug = repo_slug(repo)
        remote_url = githubapp.push_url(slug)
        config_env = githubapp.push_config(githubapp.installation_token(), slug)
    return gitcmd.push(repo, branch=branch, remote_url=remote_url, config_env=config_env,
                       author_name=author_name, author_email=author_email)
