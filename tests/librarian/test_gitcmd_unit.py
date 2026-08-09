"""Unit-level `gitcmd` coverage the end-to-end processing tests do not reach directly:
crash-leftover reaping, `ensure_repo`'s validation, `base_commit`'s remote-fallback shape, the
push retry-then-conflict paths, and the token-scrubbing regex. Real git throughout — a faked git
proves nothing about the property being claimed — so these are still "real git" tests, just
exercising `gitcmd.py`'s functions directly rather than through the whole filing flow.
"""
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


# ── reap: crash leftovers, identified by repo AND creating pid, never a live sibling's worktree ──
# The identifying shape is deliberately NARROW. It used to be "the name starts with the prefix",
# which under the default worktree root — the shared system temp directory — swept any librarian
# worktree on the machine, including one another process was working in. `make librarian-walk`
# beside a `stigmergy-librarian run` loop is a documented pairing, and it destroyed the loop's
# in-flight worktree silently (both halves of the reap are `ignore_errors=True` / `check=False`),
# losing the item.
def test_reap_removes_a_leftover_worktree_directory_and_its_git_registration(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    base = gitcmd.run("rev-parse", "HEAD", cwd=str(repo)).stdout.strip()
    root = tmp_path / "worktrees"
    root.mkdir()

    # simulate a crash: create a worktree and never remove it (no `finally`, unlike
    # `ephemeral_worktree`), named as that process would have named it.
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
    """The narrow shape's own property, and the one an operator loses an item to. THIS process is
    alive and did not create the directory, so its own pid stands in for the sibling's — a reap
    that removed it would be the `librarian-walk`-beside-`run` failure, which took the loop's
    worktree out from under it."""
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    base = gitcmd.run("rev-parse", "HEAD", cwd=str(repo)).stdout.strip()
    root = tmp_path / "worktrees"
    root.mkdir()
    sibling = root / (f"{gitcmd.WORKTREE_PREFIX}{gitcmd.worktree_key(str(repo))}"
                      f"-{os.getpid()}-feedfeedfeed")
    gitcmd.run("worktree", "add", "--detach", "--quiet", str(sibling), base, cwd=str(repo))

    # A pid check cannot tell "us" from "a live sibling", so `reapable` is asked with an explicit
    # `pid` — the seam that makes the rule testable from one process.
    assert gitcmd.reapable(sibling.name, key=gitcmd.worktree_key(str(repo)),
                           pid=os.getpid() + 1) is False
    assert sibling.is_dir()


def test_reap_never_touches_a_worktree_belonging_TO_ANOTHER_REPO(tmp_path):
    """The cross-repo half, which the default shared temp root made reachable: two librarians on two
    different checkouts saw each other's directories and each swept the other's."""
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
    """The two halves have to agree: whatever `ephemeral_worktree` names a directory, `reapable` must
    recognize as this repo's and this process's, or a crash leaves a leftover nothing collects."""
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
def test_base_commit_uses_the_local_branch_when_there_is_no_remote_at_all(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    local_head = gitcmd.run("rev-parse", "main", cwd=str(repo)).stdout.strip()
    assert gitcmd.base_commit(str(repo), "main") == local_head


def test_base_commit_prefers_the_remote_tip_when_a_remote_exists(tmp_path):
    env, deps = support.build_rig(tmp_path)
    # `deps.repo` already has `origin` (support.build_repo) and is exactly at origin/main.
    remote_head = gitcmd.run("rev-parse", "origin/main", cwd=env.repo).stdout.strip()
    assert gitcmd.base_commit(env.repo, "main") == remote_head


def test_base_ref_names_the_local_branch_it_fell_back_to(tmp_path):
    """The ref, not only the sha. A service that files from the canonical remote is correct; one
    that silently diverges from the operator's local branch is not, and that divergence cost a walk
    — a skill commit existed locally, the check read the checkout, the run read a worktree built
    from `origin/main`, and the item burned both agent attempts finding out."""
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
    """The exact divergence item 9 is about, as a fact about `base_ref` rather than prose: a commit
    that exists locally and is not pushed is NOT what the worktree gets."""
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


# ── tracked_paths / blob_size / show: reading the commit, not the working tree ──────────────────
def test_tracked_paths_lists_what_already_exists_and_not_what_was_just_written(tmp_path):
    """The input to `agent.confined_write`'s "and it must not exist yet" half."""
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    with open(os.path.join(repo, "fresh.md"), "w", encoding="utf-8") as f:
        f.write("untracked\n")
    tracked = gitcmd.tracked_paths(str(repo))
    assert "page.md" in tracked
    assert "fresh.md" not in tracked


def test_tracked_paths_handles_a_path_with_a_space_and_an_accent(tmp_path):
    """`-z`, so no quoting decision exists at all."""
    env, _ = support.build_rig(tmp_path)
    assert any(" " in path for path in gitcmd.tracked_paths(env.repo))
    assert any("é" in path or "Caf" in path for path in gitcmd.tracked_paths(env.repo))


def test_blob_size_and_show_read_the_content_at_a_commit(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    head = gitcmd.run("rev-parse", "HEAD", cwd=str(repo)).stdout.strip()
    assert gitcmd.blob_size(str(repo), head, "page.md") == len(b"line one\nline two\nline three\n")
    assert gitcmd.show(str(repo), head, "page.md").startswith("line one")


def test_blob_size_reports_minus_one_for_a_path_the_commit_does_not_carry(tmp_path):
    """`-1` rather than 0: an absent blob and an empty one are different problems, and the startup
    check has a different sentence for each."""
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    head = gitcmd.run("rev-parse", "HEAD", cwd=str(repo)).stdout.strip()
    assert gitcmd.blob_size(str(repo), head, "not/there.md") == -1


def test_show_raises_a_git_error_for_a_path_the_commit_does_not_carry(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    head = gitcmd.run("rev-parse", "HEAD", cwd=str(repo)).stdout.strip()
    with pytest.raises(GitError):
        gitcmd.show(str(repo), head, "not/there.md")


# ── the diff surface: changed_files / added_lines / diff_text ──────────────────────────────────
def test_changed_files_reports_new_modified_and_deleted_paths(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    os.remove(os.path.join(repo, "page.md"))
    with open(os.path.join(repo, "new.md"), "w", encoding="utf-8") as f:
        f.write("brand new\n")
    changes = dict((path, status) for status, path in gitcmd.changed_files(str(repo)))
    assert changes.get("new.md") == "A"
    assert changes.get("page.md") == "D"


def test_added_lines_reports_only_genuinely_new_lines_with_new_file_line_numbers(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    with open(os.path.join(repo, "page.md"), "a", encoding="utf-8") as f:
        f.write("line four\n")
    added = gitcmd.added_lines(str(repo))
    assert ("page.md", 4, "line four") in added


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


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The TOCTOU window between the gated diff and the commit.
#
# Every gated caller reads `diff_entries`, runs eight gates over it — the contract linter and
# gitleaks as SUBPROCESSES, with the worktree sitting on disk — and then committed with
# `add --all`, i.e. whatever was on disk at commit time. Anything written into the worktree in
# that window landed UNGATED. The property the whole write path rests on is "the diff the gates
# approved is the diff that lands", and nothing enforced it.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _write(repo, rel: str, text: str) -> None:
    path = os.path.join(str(repo), rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_the_sabotage_twin_a_file_planted_after_the_gates_ran_refuses_the_commit(tmp_path):
    """**The reproduction.** The gates see one page; a second file appears while they run; the
    commit must refuse rather than carry it. This is the whole finding, driven by real git."""
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    _write(repo, "wiki/notes/Gated Page.md", "the page the gates judged\n")
    gated = gitcmd.diff_entries(str(repo))
    assert [entry.path for entry in gated] == ["wiki/notes/Gated Page.md"]

    # ...the window: gitleaks and the contract linter are running as subprocesses right here.
    _write(repo, "wiki/notes/Planted.md", "nothing gated this\n")

    with pytest.raises(gitcmd.GatedDiffChangedError) as excinfo:
        gitcmd.commit(str(repo), message="feat(note): file it", author_name="t",
                      author_email="t@example.com", gated_entries=gated)
    assert "Planted.md" in str(excinfo.value)
    assert "appeared after the gates ran" in str(excinfo.value)
    # and NOTHING was committed — not the planted file, and not the gated one either
    assert gitcmd.run("rev-list", "--count", "HEAD", cwd=str(repo)).stdout.strip() == "1"


def test_the_planted_file_used_to_ride_along_which_is_what_add_all_means(tmp_path):
    """The other half of the same reproduction, kept as a test rather than a claim: with
    `gated_entries` omitted the old behaviour is still exactly what it was, and it commits the
    planted file. This is what every gated caller used to do — the assertion is here so the hole
    cannot be read as theoretical, and so the default path stays documented."""
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    _write(repo, "wiki/notes/Gated Page.md", "the page the gates judged\n")
    _write(repo, "wiki/notes/Planted.md", "nothing gated this\n")

    gitcmd.commit(str(repo), message="feat(note): file it", author_name="t",
                  author_email="t@example.com")            # no gated_entries: `add --all`
    committed = gitcmd.run("show", "--name-only", "--format=", "HEAD", cwd=str(repo)).stdout
    assert "Planted.md" in committed                        # ...and there it is


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
    """The subtlest shape: the path list is IDENTICAL and only the bytes changed after they were
    judged. The gated page here is an ADDITION (`A`) and rewriting it keeps it an addition, so a
    set of PATHS is unchanged and a path-set comparison sees nothing.

    OLD BEHAVIOUR: it committed. The assertion below used to read
    `assert "a body no gate ever saw" in body`, recorded as an honest known bound — the window was
    closed against files appearing and vanishing but not against an in-place rewrite. The producer
    that can perform one is not hypothetical: `gate_contract` is 7th of 8 and runs the knowledge
    repo's own `.claude/tools/stigmergy_lint.py` with the worktree path as an argument, after every
    content gate has already read the files.

    `DiffEntry.blob` is the content hash taken at gate time, and the comparison is over
    (path, blob) pairs."""
    repo = tmp_path / "repo"
    _init_repo(str(repo))
    _write(repo, "wiki/notes/A.md", "the gated body\n")
    gated = gitcmd.diff_entries(str(repo))

    # content swapped under the same path, no new path
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


# ── push: rebase-and-retry on a race, and a genuine conflict fails the item ─────────────────────
def _clone(bare: str, dest: str) -> None:
    gitcmd.run("clone", "--quiet", bare, dest)


def test_push_rebases_and_retries_past_a_non_conflicting_race(tmp_path):
    bare = tmp_path / "origin.git"
    gitcmd.run("init", "--bare", "--quiet", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _init_repo(str(seed))
    gitcmd.run("remote", "add", "origin", str(bare), cwd=str(seed))
    gitcmd.run("push", "--quiet", "-u", "origin", "main", cwd=str(seed))

    worker_clone = tmp_path / "worker"
    _clone(str(bare), str(worker_clone))
    with open(os.path.join(worker_clone, "librarian-page.md"), "w", encoding="utf-8") as f:
        f.write("filed by the worker\n")
    gitcmd.commit(str(worker_clone), message="feat: filed page", author_name="t",
                 author_email="t@example.com")

    # a DIFFERENT file lands on the remote first — a non-conflicting race.
    with open(os.path.join(seed, "someone-elses-edit.md"), "w", encoding="utf-8") as f:
        f.write("a steward's own edit\n")
    gitcmd.run("add", "-A", cwd=str(seed))
    gitcmd.run("commit", "--quiet", "-m", "steward's edit", cwd=str(seed),
              env={"GIT_AUTHOR_NAME": "steward", "GIT_AUTHOR_EMAIL": "steward@example.com",
                   "GIT_COMMITTER_NAME": "steward", "GIT_COMMITTER_EMAIL": "steward@example.com"})
    gitcmd.run("push", "--quiet", "origin", "main", cwd=str(seed))

    local_before = gitcmd.run("rev-parse", "HEAD", cwd=str(worker_clone)).stdout.strip()
    # The identity is not decoration and must not be dropped: a rebase REWRITES commits, so it needs
    # a committer, and git takes one from wherever it can. Passing none here used to work on a
    # developer's machine — where git auto-detects `user@hostname` — and fail on a CI runner, whose
    # hostname has no domain to auto-detect from, so git refused and this function reported the
    # refusal as "the page conflicts with a change made on the branch". Every real caller passes one.
    pushed = gitcmd.push(str(worker_clone), branch="main",
                         author_name="librarian", author_email="librarian@example.com")

    # `origin/main` is stale in the local clone's own remote-tracking ref until a fetch — check
    # the BARE remote directly, the actual destination `push` just wrote to.
    final_on_remote = gitcmd.run("log", "--format=%s", "main", cwd=str(bare)).stdout
    assert "filed page" in final_on_remote
    assert "steward's edit" in final_on_remote

    # **The returned sha is the one that LANDED, not the one that was committed.** The rebase rewrote
    # the commit, so the pre-push sha names an object in no reachable history — and that string is
    # what becomes `result_ref`, and `git show <sha>` on it must display the filing. The docker
    # e2e caught this with two workers racing: three of twelve pages were reported at shas the remote
    # had never heard of, and `git show` on them said `bad object`.
    remote_head = gitcmd.run("rev-parse", "main", cwd=str(bare)).stdout.strip()
    assert pushed == remote_head

    # The rebase stamped the LIBRARIAN as committer, not whoever happens to be configured on the
    # machine. `commit()`'s docstring promises "nothing about the operator's own git identity leaks
    # into a librarian commit"; a rebase that inherits an ambient identity breaks that promise
    # silently, and on a developer's machine it did — the rewritten commit came back committed by
    # the operator. Asserted here rather than trusted, because the failure is invisible locally.
    committer = gitcmd.run("log", "-1", "--format=%cn <%ce>", "main", cwd=str(bare)).stdout.strip()
    assert committer == "librarian <librarian@example.com>", committer
    assert pushed != local_before
    assert gitcmd.run("cat-file", "-e", pushed, cwd=str(bare), check=False).returncode == 0


def test_push_returns_the_committed_sha_unchanged_when_there_was_no_race(tmp_path):
    """The benign twin: with nothing to rebase past, the sha that landed IS the sha that was
    committed, and the fix must not have introduced a second value for the ordinary case."""
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


def test_push_a_genuine_conflict_fails_the_item_rather_than_being_resolved(tmp_path):
    bare = tmp_path / "origin.git"
    gitcmd.run("init", "--bare", "--quiet", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _init_repo(str(seed))
    gitcmd.run("remote", "add", "origin", str(bare), cwd=str(seed))
    gitcmd.run("push", "--quiet", "-u", "origin", "main", cwd=str(seed))

    worker_clone = tmp_path / "worker"
    _clone(str(bare), str(worker_clone))
    with open(os.path.join(worker_clone, "page.md"), "w", encoding="utf-8") as f:
        f.write("the LIBRARIAN's version of this line\nline two\nline three\n")
    gitcmd.commit(str(worker_clone), message="feat: librarian edit", author_name="t",
                 author_email="t@example.com")

    # a CONFLICTING edit to the SAME line lands on the remote first.
    with open(os.path.join(seed, "page.md"), "w", encoding="utf-8") as f:
        f.write("STEWARD's own conflicting version of this line\nline two\nline three\n")
    gitcmd.run("add", "-A", cwd=str(seed))
    gitcmd.run("commit", "--quiet", "-m", "steward's conflicting edit", cwd=str(seed),
              env={"GIT_AUTHOR_NAME": "steward", "GIT_AUTHOR_EMAIL": "steward@example.com",
                   "GIT_COMMITTER_NAME": "steward", "GIT_COMMITTER_EMAIL": "steward@example.com"})
    gitcmd.run("push", "--quiet", "origin", "main", cwd=str(seed))

    with pytest.raises(GitError, match="does not resolve conflicts"):
        gitcmd.push(str(worker_clone), branch="main")

    # the worktree's own rebase was aborted, not left mid-conflict for a future push to trip on
    status = gitcmd.run("status", "--porcelain=v1", cwd=str(worker_clone)).stdout
    assert "UU" not in status


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
# is worse than waiting — this parameter is `None` there, by design), a server-driven mint (ADR
# 030) runs git inside an HTTP request: an unanswered remote must not pin that request forever.
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


def test_push_does_not_report_an_unreachable_remote_as_a_conflict(tmp_path):
    """OLD BEHAVIOUR: the submitter was told their page conflicted with somebody else's change.

    The retry loop treated EVERY non-zero push as a lost race: it fetched with `check=False` and
    threw the result away, then rebased onto `FETCH_HEAD`. When the remote is simply unreachable —
    a revoked or expired installation token, DNS, a branch-protection reject — the fetch failed
    too, so `FETCH_HEAD` was stale or (in the ephemeral linked worktree the deployed worker pushes
    from) absent entirely, the rebase failed on that, and this function raised the CONFLICT
    sentence. The push's stderr, the fetch's stderr and the rebase's stderr were all discarded on
    that path, so the real cause was written down nowhere: not in the submitter's report, not in
    the worker log.

    The comment beside the rebase already records this exact misreport happening once before, for
    the missing-identity cause. That instance was fixed; the shape — any non-race failure wearing
    the conflict sentence — was not.
    """
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
                    remote_url=str(tmp_path / "this-remote-does-not-exist.git"),
                    author_name="lib", author_email="lib@example.com")

    message = str(caught.value)
    assert "conflicts with a change made on the branch" not in message, message
    assert "could not reach the remote" in message, message


def test_a_genuine_conflict_still_says_conflict(tmp_path):
    """The benign twin: naming the unreachable-remote case must not cost the real one its own
    sentence — `test_push_a_genuine_conflict_fails_the_item_rather_than_being_resolved` above is
    that assertion, and it still passes; this one states the pairing so the two are read together.
    """
    bare = tmp_path / "origin.git"
    gitcmd.run("init", "--bare", "--quiet", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _init_repo(str(seed))
    gitcmd.run("remote", "add", "origin", str(bare), cwd=str(seed))
    gitcmd.run("push", "--quiet", "-u", "origin", "main", cwd=str(seed))
    worker_clone = tmp_path / "worker"
    _clone(str(bare), str(worker_clone))
    with open(os.path.join(worker_clone, "page.md"), "w", encoding="utf-8") as f:
        f.write("the LIBRARIAN's version\nline two\n")
    gitcmd.commit(str(worker_clone), message="feat: librarian edit", author_name="t",
                  author_email="t@example.com")
    with open(os.path.join(seed, "page.md"), "w", encoding="utf-8") as f:
        f.write("the STEWARD's conflicting version\nline two\n")
    gitcmd.run("add", "-A", cwd=str(seed))
    gitcmd.run("commit", "--quiet", "-m", "conflicting", cwd=str(seed),
               env={"GIT_AUTHOR_NAME": "s", "GIT_AUTHOR_EMAIL": "s@e",
                    "GIT_COMMITTER_NAME": "s", "GIT_COMMITTER_EMAIL": "s@e"})
    gitcmd.run("push", "--quiet", "origin", "main", cwd=str(seed))

    with pytest.raises(GitError, match="does not resolve conflicts"):
        gitcmd.push(str(worker_clone), branch="main")
