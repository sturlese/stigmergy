"""Non-fixture test support for the librarian suite: a real git repo + bare remote built from a
frozen fixture knowledge-repo skeleton, and the `processing.Deps` wiring every test needs.

Deliberately NOT a `conftest.py`. The worker-signal tests (`test_worker_signals.py`) drive a
`Worker` from a real, separate OS process so a real SIGINT/SIGTERM can be delivered to it — a
pytest fixture cannot be reached from that subprocess, but a plain module can be imported by both
the test process and the harness script it launches.

**Real git is required**: every test in this package that exercises the filing path works against
an actual `git init --bare` remote and an actual clone, never a faked diff — a fake would prove
nothing about the worktree/diff/commit/push properties these tests exist to pin.

**The fixture knowledge repo** (`fixtures/repo/`) mirrors the real knowledge repo's shape closely
enough to file against: `ops/entity-registry.json` with one registered entity (`Acme Corp`,
aliased `Acme`), `ops/acl.json` in the ON-DISK dialect the real repo actually uses (so
`acl_rules.load`'s dialect adapter is exercised by every integration test, not only its own unit
tests), a pre-existing entity page the registered name resolves to, a pre-existing note page for
the additive-edit/overlap/delete/rewrite scenarios, and `.claude/tools/stigmergy_lint.py` — a frozen
copy of the real contract linter from the knowledge repo, copied rather than referenced so the
suite never depends on a knowledge-repo checkout existing on the machine running the tests. That
copy is a declared duplicate, not a hidden one: if the real linter's contract ever changes this
copy needs a manual resync, and `test_frozen_linter.py` is the thing that notices when it has not
had one.
"""
import os
import pathlib
import shutil
import subprocess
import time
from dataclasses import dataclass

from stigmergy.capture import queue, schema
from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.kernel import registry as registry_module
from stigmergy.librarian import acl_rules, config, gitcmd, processing
from stigmergy.librarian.agent import build_agent
from tests import childwatch

FIXTURE_REPO = pathlib.Path(__file__).parent / "fixtures" / "repo"

DEFAULT_SUBMITTER = "tester@stigmergy.test"

# Matches `DoubleAgent._write_page`'s own hardcoded `created`/`updated` frontmatter, so a test
# that reads a filed page back sees consistent dates rather than "today" racing the double's
# constant. `as_of` is stamped separately, from `Deps.today` — the same value, for the same
# reason: nothing here should depend on the wall clock the suite happens to run on.
FIXED_TODAY = "2026-07-26"

_COMMIT_ENV = {"GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@stigmergy.test",
              "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@stigmergy.test"}


@dataclass(frozen=True)
class RepoEnv:
    """A fresh `git init --bare` remote plus a clone seeded from `fixtures/repo/`, one per test.
    `repo` is what a real `STIGMERGY_REPO` checkout looks like; `bare` stands in for GitHub, the
    same shape `docker-compose.yml`'s `git-remote` service gives the (unbuilt) e2e."""
    bare: str
    repo: str


def gitleaks_available() -> bool:
    return shutil.which("gitleaks") is not None


def build_repo(root: str, *, source: "str | pathlib.Path" = FIXTURE_REPO) -> RepoEnv:
    """One bare remote + one clone, seeded with the fixture skeleton and pushed once. Every test
    that calls this gets its OWN pair under `root` (a pytest `tmp_path`), so nothing here is
    shared, reused or capable of leaking a commit from one test into another's diff.

    `source` is the knowledge-repo skeleton to seed from, and it defaults to this package's own
    `fixtures/repo/` — every test in the suite takes the default and is unaffected. It exists for
    `evals/run_filing.py`, which drives this same real filing path against its OWN frozen mini
    knowledge repo (`evals/filing/repo/`): that fixture is a yardstick with its own freeze cadence
    and its own synthetic organizations, so it cannot be this one, and a second copy of the
    bare-remote-plus-clone dance is exactly the duplication this module exists to prevent.
    """
    bare = os.path.join(root, "origin.git")
    repo = os.path.join(root, "checkout")
    gitcmd.run("init", "--bare", "--quiet", "-b", "main", bare)
    gitcmd.run("init", "--quiet", "-b", "main", repo)
    shutil.copytree(source, repo, dirs_exist_ok=True)
    gitcmd.run("add", "-A", cwd=repo)
    gitcmd.run("commit", "--quiet", "--no-verify", "-m", "chore: seed the fixture knowledge repo",
              cwd=repo, env=_COMMIT_ENV)
    gitcmd.run("remote", "add", "origin", bare, cwd=repo)
    gitcmd.run("push", "--quiet", "-u", "origin", "main", cwd=repo)
    return RepoEnv(bare=bare, repo=repo)


def commit_and_push(repo: str, message: str = "test: sabotage the base commit",
                    branch: str = "main") -> None:
    """Stage everything in `repo`'s working tree, commit it, and push to `origin/<branch>`.

    `acl_rules`/the registry loader/the linter read **at the base commit** (`gitcmd.base_ref`,
    normally `origin/<branch>`'s tip), never off the working tree, in every mode. A test that
    wants to sabotage one of those three inputs has to land the sabotage on that ref — writing to
    the file on disk and stopping there changes nothing a run sees. This is the one place that
    lands it, so every such test does it the same way.
    """
    gitcmd.run("add", "-A", cwd=repo)
    gitcmd.run("commit", "--quiet", "--no-verify", "-m", message, cwd=repo, env=_COMMIT_ENV)
    gitcmd.run("push", "--quiet", "origin", branch, cwd=repo)


def branch_sha(path: str, branch: str = "main") -> str:
    """The commit `branch` names in the repo/bare-remote at `path`."""
    return gitcmd.run("rev-parse", "--verify", branch, cwd=path).stdout.strip()


def dead_pid() -> int:
    """A pid no process holds — a real child, run to completion, so the number was live and is now
    free. Exactly the state a crashed librarian leaves its worktree directory name in, and the reason
    `gitcmd.reapable` can tell that leftover from a live sibling's in-flight worktree.

    A finished `subprocess`, not `os.fork()`: forking a multi-threaded process is deprecated and can
    deadlock in the child, and pytest's own machinery makes this process multi-threaded. `git` because
    every test in this package already requires it.
    """
    proc = childwatch.spawn(["git", "--version"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    proc.wait()             # reaped, so the pid is genuinely free and not a zombie
    return proc.pid


def crash_leftover_name(repo: str, pid: int | None = None) -> str:
    """The worktree directory name a crashed run of `repo` leaves behind.

    Built from `gitcmd`'s own key rather than hand-spelled, because the NAME is the reaping
    contract: it carries the repo's identity and the creating pid so a start-up sweep can tell
    "a crashed run of this checkout" from "a worktree another live librarian is working in". A
    hand-written literal here would go stale the moment that shape changes and would take the reaping
    tests silently with it.
    """
    return f"{gitcmd.WORKTREE_PREFIX}{gitcmd.worktree_key(repo)}-{dead_pid() if pid is None else pid}-abc123abc123"


def read_filed_page(repo: str, ref: str, path: str) -> str:
    """The content of `path` at commit `ref`, read straight from the object database — what
    `git show <ref>:<path>` prints a human. This is how a test asserts on the FILED PAGE rather
    than on what the agent merely drafted: `ref` is the sha `process_item` actually pushed, and
    the worktree that created the commit shares this repo's object store even after the worktree
    itself was removed in `ephemeral_worktree`'s `finally`."""
    return gitcmd.run("show", f"{ref}:{path}", cwd=repo).stdout


def commit_message_body(repo: str, sha: str) -> str:
    """The whole commit message, subject and body — what `git log` answers "where did this entity
    come from" with."""
    return gitcmd.run("log", "-1", "--format=%B", sha, cwd=repo).stdout


def commit_subject(repo: str, sha: str) -> str:
    """One commit's subject line — the surface a title's characters have to survive into as well as
    the filename, the H1 and the `title` field."""
    return gitcmd.run("log", "-1", "--format=%s", sha, cwd=repo).stdout.strip()


def diff_of(repo: str, sha: str, path: str) -> str:
    """One commit's diff for ONE path — what a reviewer would read to decide whether an edit really
    was additive. Used to assert on the shape of code's own declared edits rather than on their
    effect alone."""
    return gitcmd.run("show", sha, "--format=", "--", path, cwd=repo).stdout


def changed_paths(repo: str, sha: str) -> list[str]:
    """Every path one commit touched, for asserting which pages a filing actually modified (e.g.
    a near-duplicate's overlap callout landing on the OTHER page too)."""
    out = gitcmd.run("show", "--name-status", "--format=", sha, cwd=repo).stdout
    return [line.split("\t", 1)[-1].strip() for line in out.splitlines() if line.strip()]


def changed_paths_with_status(repo: str, sha: str) -> list[tuple[str, str]]:
    """`[(status, path), ...]` for one commit — `changed_paths` with the letter git prints kept.

    `changed_paths` throws the status away, which is fine for "which pages did this touch" but not
    for "did this commit only ADD pages, or did it also MODIFY one that already existed" — a
    property a path list alone cannot express, and the one the meeting flow's no-additive-edits
    contract is about.
    """
    out = gitcmd.run("show", "--name-status", "--format=", sha, cwd=repo).stdout
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        status, _, path = line.partition("\t")
        rows.append((status.strip(), path.strip()))
    return rows


def all_ever_committed_paths(repo: str) -> set[str]:
    """Every path that appears in ANY commit reachable from ANY ref in `repo`'s history — every
    branch, every tag, the lot. The strongest form of "no partial page set was ever observed":
    not merely that the branch tip did not move (a return-value-shaped check a caller could get
    right by accident), but that the object database never even HELD a commit naming one of the
    meeting flow's pages, whatever ref might point at it. Meant to be called against the BARE
    remote (`env.bare`) for an atomicity assertion — a real `git log` walk, not a diff of two
    return values, because a faked git proves nothing."""
    out = gitcmd.run("log", "--all", "--name-only", "--format=", cwd=repo).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def all_commit_shas(repo: str) -> set[str]:
    """Every commit reachable from any ref — for asserting the TOTAL commit count in the bare
    remote never grew, the same "no partial set, ever" property `all_ever_committed_paths` checks
    by path, checked instead by counting objects a hypothetical local-commit-without-push bug
    could have left behind."""
    out = gitcmd.run("log", "--all", "--format=%H", cwd=repo).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def build_settings(env: RepoEnv, *, worktree_root: str, backend: str = "double",
                   **overrides) -> config.Settings:
    """`Settings` wired to one `RepoEnv`. `acl_path`/`registry_path`/`linter_path` are computed
    properties off `repo` (`config.py`), and the fixture repo places every one of those files
    exactly where the property expects — nothing here overrides them."""
    kwargs = dict(repo=env.repo, branch="main", backend=backend,
                 gitleaks_bin=shutil.which("gitleaks") or "gitleaks",
                 worktree_root=worktree_root, poll_interval_s=0.1)
    kwargs.update(overrides)
    return config.Settings(**kwargs)


def build_deps(env: RepoEnv, settings: config.Settings, *, evidence=None, agent=None,
              today: str = FIXED_TODAY) -> processing.Deps:
    """`processing.Deps` for one `RepoEnv` + `Settings`. `agent` defaults to whatever
    `settings.backend` dispatches to (`build_agent`) — pass an explicit double/SlowAgent to
    override."""
    registry = registry_module.load_registry(settings.registry_path)
    acl_config = acl_rules.load(settings.acl_path)
    return processing.Deps(
        settings=settings, evidence=evidence or MemoryEvidenceStore(),
        agent=agent or build_agent(settings), registry=registry, acl_config=acl_config,
        repo=env.repo, today=today)


def build_rig(tmp_path, *, agent=None, backend: str = "double",
             evidence=None) -> tuple[RepoEnv, processing.Deps]:
    """The one-call setup most tests want: a fresh repo + bare remote, and `Deps` wired to it."""
    env = build_repo(str(tmp_path / "git"))
    settings = build_settings(env, worktree_root=str(tmp_path / "worktrees"), backend=backend)
    deps = build_deps(env, settings, evidence=evidence, agent=agent)
    return env, deps


def submit(conn, deps: processing.Deps, material: str, *, submitted_by: str = DEFAULT_SUBMITTER,
          hints: dict | None = None) -> dict:
    """Archive + enqueue one capture through the real queue primitive, using `deps.evidence` so
    `processing._material` can read back what was just written."""
    return queue.submit(conn, deps.evidence, kind="raw", material=material, hints=hints,
                        submitted_by=submitted_by)


def submit_document(conn, deps: processing.Deps, text: str, *,
                    submitted_by: str = DEFAULT_SUBMITTER, title: str = "notes",
                    source_url: str = "https://drive.google.com/file/d/TESTID123456/view") -> dict:
    """`submit`'s document-kind sibling — exactly what `brain_submit(kind="document")` enqueues:
    the document's TEXT as the row's material and the two hints a document takes (`title`, and
    `source_url` when the submitter says where it came from; `""` sends none)."""
    hints = {"title": title}
    if source_url:
        hints["source_url"] = source_url
    return queue.submit(conn, deps.evidence, kind=schema.DOCUMENT, material=text, hints=hints,
                        submitted_by=submitted_by)


def submit_meeting(conn, deps: processing.Deps, material: str, *,
                   submitted_by: str = DEFAULT_SUBMITTER, title: str = "Q3 sync",
                   meeting_date: str = "2026-07-29", attendees: str = "") -> dict:
    """`submit`'s meeting-kind sibling — the hints a meeting takes (title, meeting_date,
    attendees), enqueued exactly as `brain_submit(kind="meeting")` does."""
    hints = {"title": title, "meeting_date": meeting_date, "source_label": "granola-manual"}
    if attendees:
        hints["attendees"] = attendees
    return queue.submit(conn, deps.evidence, kind=schema.MEETING, material=material, hints=hints,
                        submitted_by=submitted_by)


class DelayedAgent:
    """Wraps a real agent and sleeps `seconds` before calling through, calling `on_ready` first.

    The seam `test_worker_signals.py` uses to know a subprocess worker is genuinely mid-item
    before sending it a real OS signal — the double itself runs near-instantly (no network, no
    model), so without an artificial delay there would be no reliable window to interrupt.
    `on_ready` is used to print a line the parent test process reads with a real timeout instead
    of guessing with a fixed sleep (mirrors `tests/capture/test_cli.py`'s `_read_until`).

    **It copies `structured_ordinary` and `wants_gathered` for the reason
    `evals/run_filing.CountingAgent` does** (ADR 033, ADR 034): this stands where `processing`
    expects a `filing_port.FilingAgent`, and a wrapper that swallows a declared port member
    silently changes which branch of the ordinary flow runs behind it and what context that branch
    builds. Plain attribute access with no default, so a backend that forgot to declare either
    fails at construction rather than one delivery at a time.

    **And it forwards `run_meeting`, which it did not used to.** The delay belongs to the ORDINARY
    flow this harness interrupts, so the second call was simply absent — and a wrapper standing in
    for the port while answering one of its two calls is the same half-backend
    `test_filing_port_conformance` refuses in every other shape: a worker whose queue happened to
    hand this harness a `kind="meeting"` row would meet an `AttributeError` mid-item rather than a
    filed page set. Forwarded UNDELAYED and deliberately so — the sleep exists to open a window for
    a real SIGINT on the flow the signal tests actually drive, and slowing a call nothing here
    interrupts would only make the suite wait."""

    def __init__(self, inner, seconds: float, on_ready=lambda: None):
        self.inner = inner
        self.structured_ordinary = inner.structured_ordinary
        self.wants_gathered = inner.wants_gathered
        self.seconds = seconds
        self.on_ready = on_ready

    def run(self, **kwargs):
        self.on_ready()
        time.sleep(self.seconds)
        return self.inner.run(**kwargs)

    def run_meeting(self, **kwargs):
        return self.inner.run_meeting(**kwargs)
