"""Git integration coverage for writer primitives."""
import os

import pytest

from stigmergy.librarian import gitcmd
from stigmergy.librarian.errors import GitError, LibrarianConfigError, WorktreeError
from tests.librarian import support


def _init_repo(path: str) -> None:
    gitcmd.run("init", "--quiet", "-b", "main", path)
    with open(os.path.join(path, "page.md"), "w", encoding="utf-8") as f:
        f.write("line one\nline two\nline three\n")
    gitcmd.run("add", "-A", cwd=path)
    gitcmd.run("commit", "--quiet", "-m", "seed", cwd=path,
              env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})


# ── ensure_repo: validated once, loudly ─────────────────────────────────────────────────────
def test_ensure_repo_raises_for_a_path_that_does_not_exist(tmp_path):
    with pytest.raises(LibrarianConfigError, match="does not exist"):
        gitcmd.ensure_repo(str(tmp_path / "nope"))


def test_ensure_repo_raises_for_a_directory_that_is_not_a_git_checkout(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(LibrarianConfigError, match="not a git checkout"):
        gitcmd.ensure_repo(str(plain))


def test_ensure_repo_accepts_a_real_checkout_and_returns_its_absolute_path(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    assert gitcmd.ensure_repo(str(repo)) == str(repo.resolve())


def test_reap_removes_a_leftover_worktree_directory_and_its_git_registration(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    base = gitcmd.run("rev-parse", "HEAD", cwd=str(repo)).stdout.strip()
    root = tmp_path / "worktrees"
    root.mkdir()

    leftover = root / support.crash_leftover_name(str(repo))
    gitcmd.run("worktree", "add", "--detach", "--quiet", str(leftover), base, cwd=str(repo))
    assert leftover.is_dir()

    removed = gitcmd.reap(str(repo), str(root))

    assert removed >= 1
    assert not leftover.exists()
    listing = gitcmd.run("worktree", "list", "--porcelain", cwd=str(repo)).stdout
    assert str(leftover) not in listing


def test_reap_never_touches_a_directory_without_the_worktree_prefix(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    root = tmp_path / "worktrees"
    root.mkdir()
    unrelated = root / "not-a-librarian-worktree"
    unrelated.mkdir()

    gitcmd.reap(str(repo), str(root))

    assert unrelated.exists()


def test_reap_leaves_a_live_siblings_in_flight_worktree_alone(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    base = gitcmd.run("rev-parse", "HEAD", cwd=str(repo)).stdout.strip()
    root = tmp_path / "worktrees"
    root.mkdir()
    sibling = root / (f"{gitcmd.WORKTREE_PREFIX}{gitcmd.worktree_key(str(repo))}"
                      f"-{os.getpid()}-feedfeedfeed")
    gitcmd.run("worktree", "add", "--detach", "--quiet", str(sibling), base, cwd=str(repo))

    assert gitcmd.reapable(sibling.name, key=gitcmd.worktree_key(str(repo)),
                           pid=os.getpid() + 1) is False
    assert sibling.is_dir()


def test_reap_never_touches_a_worktree_belonging_TO_ANOTHER_REPO(tmp_path):
    ours, theirs = tmp_path / "ours", tmp_path / "theirs"
    _init_repo(str(ours))
    _init_repo(str(theirs))
    root = tmp_path / "worktrees"
    root.mkdir()
    other = root / support.crash_leftover_name(str(theirs))
    other.mkdir()

    gitcmd.reap(str(ours), str(root))

    assert other.exists(), "reaped a worktree directory belonging to a different repo"


def test_an_ephemeral_worktree_is_named_so_a_reap_can_identify_its_owner(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    base = gitcmd.run("rev-parse", "HEAD", cwd=str(repo)).stdout.strip()
    root = tmp_path / "worktrees"

    with gitcmd.ephemeral_worktree(str(repo), base, str(root)) as path:
        name = os.path.basename(path)

    assert gitcmd.reapable(name, key=gitcmd.worktree_key(str(repo))) is True
    assert gitcmd.reapable(name, key="00000000") is False


def test_reap_is_a_no_op_when_nothing_was_left_behind(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    root = tmp_path / "worktrees"
    root.mkdir()
    assert gitcmd.reap(str(repo), str(root)) == 0


# ── ephemeral_worktree: created, yielded, removed however the block exits ──────────────────────
def test_ephemeral_worktree_is_removed_even_when_the_block_raises(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    base = gitcmd.run("rev-parse", "HEAD", cwd=str(repo)).stdout.strip()
    root = tmp_path / "worktrees"

    captured_path = None
    with pytest.raises(ValueError), \
         gitcmd.ephemeral_worktree(str(repo), base, str(root)) as path:
        captured_path = path
        assert os.path.isdir(path)
        raise ValueError("simulated failure mid-item")

    assert not os.path.exists(captured_path)


def test_ephemeral_worktree_raises_worktree_error_when_the_base_commit_is_bogus(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    root = tmp_path / "worktrees"
    with pytest.raises(WorktreeError), gitcmd.ephemeral_worktree(str(repo), "0" * 40, str(root)):
        pass


# ── base_ref: remote tip when there is a remote, local branch otherwise — and it SAYS which ────
def test_base_ref_uses_the_local_branch_when_there_is_no_remote_at_all(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    local_head = gitcmd.run("rev-parse", "main", cwd=str(repo)).stdout.strip()
    assert gitcmd.base_ref(str(repo), "main").sha == local_head


def test_base_ref_prefers_the_remote_tip_when_a_remote_exists(tmp_path):
    env, deps = support.build_rig(tmp_path)
    # `deps.repo` already has `origin` (support.build_repo) and is exactly at origin/main.
    remote_head = gitcmd.run("rev-parse", "origin/main", cwd=env.repo).stdout.strip()
    assert gitcmd.base_ref(env.repo, "main").sha == remote_head


def test_base_ref_names_the_local_branch_it_fell_back_to(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    base = gitcmd.base_ref(str(repo), "main")
    assert base.ref == "main" and base.remote is False
    assert base.describe().startswith("main@")
    assert base.sha[:12] in base.describe()


def test_base_ref_names_origin_when_that_is_where_the_worktree_will_branch_from(tmp_path):
    env, _ = support.build_rig(tmp_path)
    base = gitcmd.base_ref(env.repo, "main")
    assert base.ref == "origin/main" and base.remote is True
    assert base.describe().startswith("origin/main@")


def test_base_ref_resolves_to_the_remote_tip_even_when_the_local_branch_has_moved_on(tmp_path):
    env, _ = support.build_rig(tmp_path)
    with open(os.path.join(env.repo, "wiki", "notes", "Local Only.md"), "w",
              encoding="utf-8") as f:
        f.write("---\ntype: note\n---\n\nlocal only\n")
    gitcmd.run("add", "-A", cwd=env.repo)
    gitcmd.run("commit", "--quiet", "-m", "local only", cwd=env.repo,
              env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"})
    local_head = gitcmd.run("rev-parse", "main", cwd=env.repo).stdout.strip()

    base = gitcmd.base_ref(env.repo, "main")
    assert base.sha != local_head
    assert base.sha == gitcmd.run("rev-parse", "origin/main", cwd=env.repo).stdout.strip()


def test_show_reads_the_content_at_a_commit(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    head = gitcmd.run("rev-parse", "HEAD", cwd=str(repo)).stdout.strip()
    assert gitcmd.show(str(repo), head, "page.md").startswith("line one")


def test_show_raises_a_git_error_for_a_path_the_commit_does_not_carry(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    head = gitcmd.run("rev-parse", "HEAD", cwd=str(repo)).stdout.strip()
    with pytest.raises(GitError):
        gitcmd.show(str(repo), head, "not/there.md")


# ── commit: author/committer set per-invocation, never inherited from the operator ─────────────
def test_commit_sets_the_authoring_identity_from_arguments_not_ambient_git_config(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    with open(os.path.join(repo, "new.md"), "w", encoding="utf-8") as f:
        f.write("new page\n")
    sha = gitcmd.commit(str(repo), message="feat: a page",
                        author_name="stigmergy-librarian[bot]",
                        author_email="1+stigmergy-librarian[bot]@users.noreply.github.com")
    shown = gitcmd.run("show", "-s", "--format=%an <%ae> / %cn <%ce>", sha, cwd=str(repo)).stdout
    assert "stigmergy-librarian[bot] <1+stigmergy-librarian[bot]@users.noreply.github.com>" in shown


# ── commit() with nothing staged ────────────────────────────────────────────────────────────────
# `commit()` has no `allow_empty` escape hatch: the only caller that ever wanted one is gone, and
# so is the recomputed `verification` value that made an empty commit meaningful. What every
# remaining caller depends on is the property below — an empty commit is an ERROR, not a silent
# no-op.
def test_commit_with_nothing_staged_raises(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    with pytest.raises(GitError):
        gitcmd.commit(str(repo), message="feat: nothing changed", author_name="t",
                      author_email="t@example.com")


def _write(repo, rel: str, text: str) -> None:
    path = os.path.join(str(repo), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_diff_entries_exposes_a_rename_as_delete_and_create(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    os.rename(repo / "page.md", repo / "renamed.md")

    entries = gitcmd.diff_entries(str(repo))

    assert {(entry.status, entry.path) for entry in entries} == {
        ("D", "page.md"),
        ("A", "renamed.md"),
    }


def test_the_sabotage_twin_a_file_planted_after_the_gates_ran_refuses_the_commit(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    _write(repo, "wiki/notes/Gated Page.md", "the page the gates judged\n")
    gated = gitcmd.diff_entries(str(repo))
    assert [entry.path for entry in gated] == ["wiki/notes/Gated Page.md"]

    _write(repo, "wiki/notes/Planted.md", "nothing gated this\n")

    with pytest.raises(gitcmd.GatedDiffChangedError) as excinfo:
        gitcmd.commit(str(repo), message="feat(note): file it", author_name="t",
                      author_email="t@example.com", gated_entries=gated)
    assert "Planted.md" in str(excinfo.value)
    assert "appeared after the gates ran" in str(excinfo.value)
    # and NOTHING was committed — not the planted file, and not the gated one either
    assert gitcmd.run("rev-list", "--count", "HEAD", cwd=str(repo)).stdout.strip() == "1"


def test_commit_without_gated_entries_commits_all_changes(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    _write(repo, "wiki/notes/Gated Page.md", "the page the gates judged\n")
    _write(repo, "wiki/notes/Planted.md", "nothing gated this\n")

    gitcmd.commit(str(repo), message="feat(note): file it", author_name="t",
                  author_email="t@example.com")
    committed = gitcmd.run("show", "--name-only", "--format=", "HEAD", cwd=str(repo)).stdout
    assert "Planted.md" in committed


def test_a_gated_path_that_vanished_before_the_commit_also_refuses(tmp_path):
    """The other direction of set-equality, which matters for the same reason: a page the gates
    approved and that is then REMOVED means the commit would carry less than what was judged, and
    the submitter would be told a page was filed that is not there."""
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    _write(repo, "wiki/notes/A.md", "a\n")
    _write(repo, "wiki/notes/B.md", "b\n")
    gated = gitcmd.diff_entries(str(repo))
    os.remove(os.path.join(str(repo), "wiki/notes/B.md"))

    with pytest.raises(gitcmd.GatedDiffChangedError, match="gated but now absent"):
        gitcmd.commit(str(repo), message="feat(note): file them", author_name="t",
                      author_email="t@example.com", gated_entries=gated)


def test_the_benign_twin_an_unchanged_worktree_commits_exactly_the_gated_set(tmp_path):
    """A benign twin for every defense. Nothing was planted, nothing vanished:
    the commit contains exactly the gated paths and the refusal never fires. Without this, a
    `gated_entries` implementation that refused everything would pass the three tests above."""
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    _write(repo, "wiki/notes/A.md", "a\n")
    _write(repo, "wiki/notes/B.md", "b\n")
    gated = gitcmd.diff_entries(str(repo))

    gitcmd.commit(str(repo), message="feat(note): file them", author_name="t",
                  author_email="t@example.com", gated_entries=gated)
    committed = gitcmd.run("show", "--name-only", "--format=", "HEAD",
                           cwd=str(repo)).stdout.split()
    assert sorted(committed) == sorted(entry.path for entry in gated)
    assert gitcmd.diff_entries(str(repo)) == []      # nothing left behind uncommitted


def test_a_modification_to_a_gated_page_after_the_gates_is_refused_too(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    _write(repo, "wiki/notes/A.md", "the gated body\n")
    gated = gitcmd.diff_entries(str(repo))

    _write(repo, "wiki/notes/A.md", "a body no gate ever saw\n")

    with pytest.raises(gitcmd.GatedDiffChangedError) as excinfo:
        gitcmd.commit(str(repo), message="feat(note): file it", author_name="t",
                      author_email="t@example.com", gated_entries=gated)
    assert "changed after the gates ran" in str(excinfo.value)
    assert "wiki/notes/A.md" in str(excinfo.value)
    # and nothing landed: not the rewrite, not the version the gates approved
    assert gitcmd.run("rev-list", "--count", "HEAD", cwd=str(repo)).stdout.strip() == "1"


def test_a_gated_page_rewritten_to_the_very_same_bytes_still_commits(tmp_path):
    """The benign twin for the content check. A linter that rewrites a file to byte-identical
    content — reformatting that changes nothing, or a touch — has changed nothing the gates
    judged, and refusing it would bounce someone's real work for a no-op."""
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    _write(repo, "wiki/notes/A.md", "the gated body\n")
    gated = gitcmd.diff_entries(str(repo))

    _write(repo, "wiki/notes/A.md", "the gated body\n")      # same bytes, written again

    gitcmd.commit(str(repo), message="feat(note): file it", author_name="t",
                  author_email="t@example.com", gated_entries=gated)
    assert "the gated body" in gitcmd.run("show", "HEAD:wiki/notes/A.md", cwd=str(repo)).stdout


def _clone(bare: str, dest: str) -> None:
    gitcmd.run("clone", "--quiet", bare, dest)


def test_push_lands_the_exact_gated_commit(tmp_path):
    bare = tmp_path / "origin.git"
    gitcmd.run("init", "--bare", "--quiet", "-b", "main", str(bare))
    worker_clone = tmp_path / "worker"
    _init_repo(str(worker_clone))
    gitcmd.run("remote", "add", "origin", str(bare), cwd=str(worker_clone))
    with open(os.path.join(worker_clone, "librarian-page.md"), "w", encoding="utf-8") as f:
        f.write("filed by the worker\n")
    committed = gitcmd.commit(str(worker_clone), message="feat: filed page", author_name="t",
                              author_email="t@example.com")

    assert gitcmd.push(str(worker_clone), branch="main") == committed
    assert gitcmd.run("rev-parse", "main", cwd=str(bare)).stdout.strip() == committed


def test_push_rejects_a_moved_branch_without_rebasing(tmp_path):
    bare = tmp_path / "origin.git"
    gitcmd.run("init", "--bare", "--quiet", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _init_repo(str(seed))
    gitcmd.run("remote", "add", "origin", str(bare), cwd=str(seed))
    gitcmd.run("push", "--quiet", "-u", "origin", "main", cwd=str(seed))
    worker_clone = tmp_path / "worker"
    _clone(str(bare), str(worker_clone))
    with open(os.path.join(worker_clone, "writer.md"), "w", encoding="utf-8") as f:
        f.write("gated writer change\n")
    gitcmd.commit(
        str(worker_clone),
        message="feat: gated writer change",
        author_name="t",
        author_email="t@example.com",
    )
    with open(os.path.join(seed, "someone-elses-edit.md"), "w", encoding="utf-8") as f:
        f.write("a steward's own edit\n")
    gitcmd.run("add", "-A", cwd=str(seed))
    gitcmd.run("commit", "--quiet", "-m", "steward's edit", cwd=str(seed),
              env={"GIT_AUTHOR_NAME": "s", "GIT_AUTHOR_EMAIL": "s@example.com",
                   "GIT_COMMITTER_NAME": "s", "GIT_COMMITTER_EMAIL": "s@example.com"})
    gitcmd.run("push", "--quiet", "origin", "main", cwd=str(seed))
    with pytest.raises(gitcmd.GitError, match="was rejected"):
        gitcmd.push(str(worker_clone), branch="main")

    log = gitcmd.run("log", "--format=%s", "main", cwd=str(bare)).stdout
    assert "gated writer change" not in log
    assert "steward's edit" in log


# ── the token-scrubbing regex: the one thing that must never reach a log or an error ───────────
# `_scrub` is tested directly (pure function, no network) rather than by forcing a real `git
# push` against a credentialed URL to fail: that would either hang on a real DNS lookup for
# `github.com` or depend on network reachability the sandbox this suite runs in should never
# require: no test may need an API key, and the same posture extends to needing the network at
# all.
def test_scrub_removes_a_push_url_credential_from_arbitrary_text():
    hostile = ("error: failed to push some refs to "
              "'https://x-access-token:ghs_supersecrettoken@github.com/acme/knowledge.git'")
    scrubbed = gitcmd._scrub(hostile)
    assert "ghs_supersecrettoken" not in scrubbed
    assert "https://***@github.com/acme/knowledge.git" in scrubbed


def test_scrub_leaves_ordinary_text_with_no_credential_untouched():
    plain = "error: failed to push some refs to 'origin'"
    assert gitcmd._scrub(plain) == plain


def test_run_scrubs_a_credentialed_argument_in_its_own_raised_error(tmp_path):
    """`run()`'s own contract: a failing command's arguments are scrubbed in the raised
    `GitError`, not just its stderr — proven with a LOCAL, fast-failing target (an invalid path),
    never a real network push."""
    hostile_url = "https://x-access-token:ghs_supersecrettoken@github.invalid/nowhere.git"
    with pytest.raises(GitError) as exc_info:
        gitcmd.run("ls-remote", hostile_url, cwd=str(tmp_path))
    assert "ghs_supersecrettoken" not in str(exc_info.value)


# ── run(timeout=...): a bound subprocess (audit M2) ──────────────────────────────────────────────
# Unlike the worker's own git (a loop with a lease behind it, where cutting a push off mid-flight
# is worse than waiting — this parameter is `None` there, by design), a server-driven mint
# runs git inside an HTTP request: an unanswered remote must not pin that request forever.
# Proven against a REAL, local, slow git invocation — a `!sleep` alias — never a mocked subprocess
# and never the network, the same posture `test_run_scrubs_...` above states for this file.
def test_run_raises_a_git_error_naming_the_budget_when_the_timeout_elapses(tmp_path):
    repo = str(tmp_path / "repo")
    _init_repo(repo)
    gitcmd.run("config", "alias.slow", "!sleep 2", cwd=repo)

    with pytest.raises(GitError, match=r"exceeded its 0\.05s budget") as exc_info:
        gitcmd.run("slow", cwd=repo, timeout=0.05)
    assert "did not answer in time" in str(exc_info.value)


def test_run_with_no_timeout_still_completes_an_ordinary_command(tmp_path):
    """The benign twin: `timeout=None` (the default, and the worker's own call shape) must keep
    working exactly as before — this parameter is additive, never a behavior change for a caller
    that does not pass it."""
    repo = str(tmp_path / "repo")
    _init_repo(repo)
    result = gitcmd.run("rev-parse", "HEAD", cwd=repo)
    assert len(result.stdout.strip()) == 40


def test_run_a_generous_timeout_does_not_interrupt_a_fast_command(tmp_path):
    """The other benign twin: a real, ample budget on an ordinary fast command must not itself
    become a source of flaky failures — `timeout=` bounds a stall, it does not race one."""
    repo = str(tmp_path / "repo")
    _init_repo(repo)
    result = gitcmd.run("rev-parse", "HEAD", cwd=repo, timeout=30)
    assert len(result.stdout.strip()) == 40


def test_push_reports_a_rejected_remote_without_calling_it_a_conflict(tmp_path):
    seed = tmp_path / "seed"
    _init_repo(str(seed))
    worker_clone = tmp_path / "worker"
    _clone(str(seed), str(worker_clone))
    with open(os.path.join(worker_clone, "page.md"), "w", encoding="utf-8") as f:
        f.write("filed by the librarian\n")
    gitcmd.commit(str(worker_clone), message="feat: page", author_name="lib",
                  author_email="lib@example.com")

    # No network needed to be unreachable: a remote path that does not exist fails the same way.
    with pytest.raises(GitError) as caught:
        gitcmd.push(str(worker_clone), branch="main",
                    remote_url=str(tmp_path / "this-remote-does-not-exist.git"))

    message = str(caught.value)
    assert "conflict" not in message.lower()
    assert "was rejected" in message
