"""`entities.clone` — the steward's own git safety net, and the wrong-branch-push regression.

Real git throughout: every test here pushes to and reads back from an actual `git init --bare`
remote — the property under test ("what is actually on `origin/main` afterwards") has no meaning
against a faked diff.
"""
import os
import subprocess

import pytest

from stigmergy.entities import clone
from stigmergy.entities.errors import CloneStateError, PushRaceError
from stigmergy.librarian.errors import GitError
from tests.entities import conftest as fx


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _stage_and_commit(repo: str, relpath: str, text: str, *, message: str = "test commit") -> None:
    path = os.path.join(repo, *relpath.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    _git("add", "--all", cwd=repo)
    _git("commit", "--quiet", "-m", message, cwd=repo)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# `approve`/`create` used to push the steward's CURRENT branch to `main` — the guard checked the
# branch NAMED `main` while the commit and the push acted on whatever HEAD was.
# Assert the REMOTE'S CONTENTS afterwards (module docstring's own instruction), never the exit
# code alone: the exit code was 0 *and* wrong before the fix.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_preflight_refuses_a_feature_branch_and_publishes_nothing(repo):
    remote, steward = repo
    _git("switch", "-c", "wip", cwd=steward)
    _stage_and_commit(steward, "SECRET-DRAFT.md", "a private commit nobody has seen\n")
    before = fx.remote_log(remote)

    with pytest.raises(CloneStateError, match="not 'main'"):
        clone.preflight(steward, "main", action="approve")

    assert fx.remote_log(remote) == before
    assert "SECRET-DRAFT.md" not in fx.remote_files(remote)


def test_preflight_refuses_a_detached_head_and_publishes_nothing(repo):
    remote, steward = repo
    main_sha = _git("rev-parse", "main", cwd=steward).stdout.strip()
    _git("checkout", "--detach", main_sha, cwd=steward)
    before = fx.remote_log(remote)

    with pytest.raises(CloneStateError, match="HEAD"):
        clone.preflight(steward, "main", action="approve")

    assert fx.remote_log(remote) == before


def test_preflight_benign_twin_on_main_clean_and_in_sync_passes(repo):
    """The steward's ordinary case: on `main`, clean, in sync with origin. Must NOT be refused —
    the benign twin every refusal above needs beside it."""
    _remote, steward = repo
    author = clone.preflight(steward, "main", action="approve")
    assert author == (fx.STEWARD_NAME, fx.STEWARD_EMAIL)


def test_a_private_commit_on_a_feature_branch_stays_unpublished_even_after_a_benign_run_on_main(repo):
    """The full wrong-branch shape in one test: a feature branch carrying a private commit is
    refused, and a SEPARATE, legitimate run on `main` afterwards must still not have published
    it — the two states (the feature branch existing at all, and `main` being used correctly)
    must not interact."""
    remote, steward = repo
    _git("switch", "-c", "wip", cwd=steward)
    _stage_and_commit(steward, "SECRET-DRAFT.md", "private\n")
    with pytest.raises(CloneStateError):
        clone.preflight(steward, "main", action="approve")

    _git("switch", "main", cwd=steward)
    clone.preflight(steward, "main", action="approve")   # must not raise
    clone.ensure_clean(steward)  # still clean; nothing from `wip` touched the working tree

    assert "SECRET-DRAFT.md" not in fx.remote_files(remote)


def test_preflight_never_force_pushes_a_diverged_clone(repo):
    """A local `main` that has diverged from `origin/main` is refused outright — before any push
    is even attempted, so a force-push is never the recovery."""
    remote, steward = repo
    # a second clone lands a commit on origin/main first
    other = os.path.join(os.path.dirname(steward), "other")
    fx.clone_of(remote, other)
    _stage_and_commit(other, "wiki/entities/Elsewhere.md",
                      fx.page_text("Elsewhere", "organization", []))
    _git("push", "--quiet", "origin", "main", cwd=other)

    # the steward's own clone now has an unrelated local commit, diverged from the moved remote
    _stage_and_commit(steward, "wiki/entities/Local Only.md",
                      fx.page_text("Local Only", "organization", []))

    with pytest.raises(CloneStateError, match="diverged"):
        clone.preflight(steward, "main", action="approve")


# ── ensure_clean / ensure_on_branch / ensure_in_sync individually ────────────────────────────────
def test_ensure_clean_refuses_an_untracked_file(repo):
    _remote, steward = repo
    with open(os.path.join(steward, "untracked.md"), "w") as f:
        f.write("stray\n")
    with pytest.raises(CloneStateError, match="uncommitted"):
        clone.ensure_clean(steward)


def test_ensure_clean_passes_on_a_pristine_clone(repo):
    _remote, steward = repo
    clone.ensure_clean(steward)     # must not raise


def test_ensure_in_sync_passes_with_no_remote_at_all(tmp_path):
    """A clone with no remote is in sync by definition (module docstring) — the offline/bare-remote
    case must not need a network to be testable."""
    local = str(tmp_path / "local-only")
    os.makedirs(local)
    _git("init", "--quiet", "-b", "main", local, cwd=str(tmp_path))
    _git("config", "user.name", "x", cwd=local)
    _git("config", "user.email", "x@example.com", cwd=local)
    with open(os.path.join(local, "f.txt"), "w") as f:
        f.write("x")
    _git("add", "-A", cwd=local)
    _git("commit", "--quiet", "-m", "init", cwd=local)
    clone.ensure_in_sync(local, "main")     # must not raise


def test_identity_is_refused_when_unset(tmp_path):
    local = str(tmp_path / "no-identity")
    os.makedirs(local)
    _git("init", "--quiet", "-b", "main", local, cwd=str(tmp_path))
    with pytest.raises(CloneStateError, match="user.name"):
        clone.identity(local)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Never force-pushes; survives a push race by fetch + regenerate + retry.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_commit_and_push_lands_a_clean_commit_with_the_given_identity(repo):
    remote, steward = repo
    _stage_and_commit_pending(steward, "wiki/entities/New Co.md",
                              fx.page_text("New Co", "organization", []))
    sha = clone.commit_and_push(steward, branch="main", message="feat(entity): add New Co",
                                author=(fx.STEWARD_NAME, fx.STEWARD_EMAIL))
    assert len(sha) == 40
    log = _git("log", "-1", "--format=%an <%ae>", sha, cwd=remote).stdout.strip()
    assert log == f"{fx.STEWARD_NAME} <{fx.STEWARD_EMAIL}>"
    assert _git("rev-parse", "main", cwd=remote).stdout.strip() == sha


def _stage_and_commit_pending(repo, relpath, text):
    """Stage a new file WITHOUT committing — `commit_and_push` does the commit itself."""
    path = os.path.join(repo, *relpath.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    _git("add", "--all", cwd=repo)


def test_commit_and_push_survives_a_real_race_by_rebasing_and_retrying(repo):
    """A retry path needs a test that actually loses the race: a SECOND real clone lands a commit
    on `origin/main` after this steward's own commit is made locally but BEFORE the push — forcing
    the retry loop to actually rebase and land, rather than merely exercising the happy first-try
    path."""
    remote, steward = repo
    other = os.path.join(os.path.dirname(steward), "other")
    fx.clone_of(remote, other)

    _stage_and_commit_pending(steward, "wiki/entities/Mine.md",
                              fx.page_text("Mine", "organization", []))

    # land the OTHER steward's commit on the remote first — the race window
    _stage_and_commit(other, "wiki/entities/Theirs.md",
                      fx.page_text("Theirs", "organization", []))
    _git("push", "--quiet", "origin", "main", cwd=other)
    retried = []
    sha = clone.commit_and_push(
        steward, branch="main", message="feat(entity): add Mine",
        author=(fx.STEWARD_NAME, fx.STEWARD_EMAIL),
        regenerate=lambda: False,
        on_retry=lambda msg: retried.append(msg))

    assert retried, "the retry hook never fired — the race was not actually forced"
    assert "Mine" in _git("show", f"{sha}:wiki/entities/Mine.md", cwd=remote).stdout
    files = fx.remote_files(remote)
    assert "wiki/entities/Theirs.md" in files   # the other steward's commit also landed
    assert _git("rev-parse", "main", cwd=remote).stdout.strip() == sha


def test_commit_and_push_never_force_pushes_when_the_race_cannot_resolve(repo):
    """A genuine conflict — both stewards touching the SAME registry entry — is not resolved by
    force; the loop reports the race and leaves the commit local, unpushed."""
    remote, steward = repo
    other = os.path.join(os.path.dirname(steward), "other")
    fx.clone_of(remote, other)

    registry_path_local = os.path.join(steward, "ops", "entity-registry.json")
    with open(registry_path_local, "a") as f:
        f.write("\n// steward B's local edit marker, forced into the same file\n")
    # both edit the SAME file so the auto-rebase genuinely conflicts
    _stage_and_commit_pending(steward, "ops/entity-registry.json",
                              open(registry_path_local).read() + "conflict-b\n")

    other_registry = os.path.join(other, "ops", "entity-registry.json")
    with open(other_registry, "a") as f:
        f.write("conflict-a\n")
    _git("add", "-A", cwd=other)
    _git("commit", "--quiet", "-m", "steward A's edit", cwd=other)
    _git("push", "--quiet", "origin", "main", cwd=other)
    before_push = fx.remote_log(remote)

    with pytest.raises((PushRaceError, GitError)):
        clone.commit_and_push(steward, branch="main", message="steward B's edit",
                              author=(fx.STEWARD_NAME, fx.STEWARD_EMAIL))

    # never force-pushed: the remote is exactly what steward A left it as
    assert fx.remote_log(remote) == before_push
    # and never even ATTEMPTED as an argument: no quoted `--force`/`-f` git argument anywhere in
    # this module (belt-and-braces beside the behavioural assertion above — the docstring's own
    # prose mentions the word, so this checks for the argument LITERAL, not the substring)
    import inspect

    from stigmergy.entities import clone as clone_mod
    source = inspect.getsource(clone_mod)
    assert '"--force"' not in source and "'--force'" not in source


def test_commit_and_push_exhausts_its_bounded_attempts_and_leaves_the_commit_local(repo, monkeypatch):
    """The bounded half of "bounded, never force-pushes": a push that NEVER succeeds (the remote
    keeps moving) raises after `MAX_PUSH_ATTEMPTS`, and the commit is left in the local clone."""
    _remote, steward = repo
    _stage_and_commit_pending(steward, "wiki/entities/Never.md",
                              fx.page_text("Never", "organization", []))

    from stigmergy.librarian import gitcmd as gitcmd_mod
    real_run = gitcmd_mod.run

    def _always_reject_push(*args, **kwargs):
        if args and args[0] == "push":
            class _Rejected:
                returncode = 1
                stdout = ""
                stderr = "rejected"
            return _Rejected()
        return real_run(*args, **kwargs)

    monkeypatch.setattr(clone.gitcmd, "run", _always_reject_push)
    # `**kwargs`: this fake stands in for the real function's SIGNATURE too, so the retry
    # loop's call still binds if `_rebase_onto_remote` ever grows a keyword again.
    monkeypatch.setattr(clone, "_rebase_onto_remote", lambda repo, branch, **kwargs: None)

    with pytest.raises(PushRaceError, match="after 3 attempts"):
        clone.commit_and_push(steward, branch="main", message="feat(entity): add Never",
                              author=(fx.STEWARD_NAME, fx.STEWARD_EMAIL))

    monkeypatch.setattr(clone.gitcmd, "run", real_run)
    assert "Never" in real_run("show", "HEAD:wiki/entities/Never.md", cwd=steward).stdout


# ── discard_untracked / write_page: the rollback path, and its "dead parameter" regression ───────
def test_write_page_refuses_to_overwrite_an_existing_file(repo):
    _remote, steward = repo
    with pytest.raises(CloneStateError, match="already exists"):
        clone.write_page(steward, "wiki/entities/Jordan Reyes.md", "clobber\n")


def test_discard_untracked_removes_exactly_the_file_it_is_given(repo):
    _remote, steward = repo
    path = clone.write_page(steward, "wiki/entities/Throwaway.md", "x\n")
    assert os.path.exists(path)
    clone.discard_untracked(path)
    assert not os.path.exists(path)


def test_discard_untracked_does_not_raise_on_an_already_gone_file(tmp_path):
    """The regression this pins: `discard_untracked` used to declare a leading `repo` argument it
    never read, so the ONE call in the rollback path raised `TypeError` the moment a refusal
    actually reached it (module docstring). Proven here as a plain contract test — one argument,
    the absolute path, and no exception when there is nothing to remove."""
    missing = str(tmp_path / "already-gone.md")
    clone.discard_untracked(missing)      # must not raise
