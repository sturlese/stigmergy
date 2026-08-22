"""The startup checks that make the environment the tool's problem instead of the operator's
memory, and keep `git blame` from lying.

They live in `worker.startup_checks`, before a single item is claimed, for the reason that module's
docstring already gives: a fault discovered mid-run becomes N identical `failed` rows with the real
cause buried under attempts-exhausted noise.

- **a github.com remote + no GitHub App** — the push would be made with whoever's disk credentials
  the process holds, so a page the librarian wrote would be blamed on a human.
- **a malformed or unreadable entity registry** — one loud line before the first claim, not a
  traceback per capture.

**A whole section left this file with the `sdk` backend**: the agent-credential pre-flight, which
proved the Claude Code CLI had something to authenticate with, plus the three-way
`credential_status` group that was its benign twin for an interactively-logged-in machine. Its
subject is gone — there is no subprocess and no CLI. The DOCTRINE it proved out is not: a missing
credential is caught at startup, never mid-run, and the refusal never offers `--backend double` as
a workaround, because the double files fabricated pages. That doctrine now lives on the provider-key
pre-flight in `test_pydantic_preflight.py`, which is where a reader should look for it.

`_check_push_identity` is exercised DIRECTLY rather than through `startup_checks` for the github.com
cases, and that is deliberate rather than lazy: `startup_checks` resolves the base ref first, which
runs `git fetch origin main` — against a real `https://github.com/...` remote that is a network call
in a unit test, slow at best and a credential prompt at worst. The check itself takes a repo path and
reads one local `git remote get-url`, so calling it is both honest and offline.
"""
import os

import pytest

from stigmergy.librarian import config, githubapp, worker
from stigmergy.librarian.errors import LibrarianConfigError
from tests.librarian import support


def _remote(repo: str, url: str) -> None:
    support.gitcmd.run("remote", "set-url", "origin", url, cwd=repo)



# ── the push identity ─────────────────────────────────────────────────────────────────────────────
def test_a_local_bare_remote_needs_no_github_app(rig):
    """What the suite and the docker e2e do: push to a bare local remote that wants no credential at
    all. An unconditional App requirement would break both, which is why the check is conditional on
    the DESTINATION rather than on the configuration."""
    env, _ = rig
    worker._check_push_identity(env.repo)                # must not raise


def test_a_repo_with_no_remote_at_all_needs_no_github_app(tmp_path):
    repo = tmp_path / "no-remote"
    repo.mkdir()
    support.gitcmd.run("init", "--quiet", "-b", "main", str(repo))
    worker._check_push_identity(str(repo))               # must not raise


def test_a_github_remote_without_the_app_is_refused_naming_what_git_blame_would_say(rig):
    env, _ = rig
    _remote(env.repo, "https://github.com/acme/knowledge.git")

    with pytest.raises(LibrarianConfigError) as exc_info:
        worker._check_push_identity(env.repo)

    message = str(exc_info.value)
    assert "github.com" in message
    assert "git blame" in message
    assert githubapp.APP_ID_ENV in message and githubapp.PRIVATE_KEY_ENV in message
    # the setup procedure is named by path, so the fix is one file away rather than a guess
    assert "docs/reference/operator-runbook.md" in message


def test_an_ssh_github_remote_is_refused_too(rig):
    """`git@github.com:owner/name.git` is the same destination in a different dialect, and
    `githubapp.repo_slug` already parses both — a check that only understood https would let the
    form the operator's own checkout uses straight through."""
    env, _ = rig
    _remote(env.repo, "git@github.com:acme/knowledge.git")
    with pytest.raises(LibrarianConfigError, match="github.com"):
        worker._check_push_identity(env.repo)


def test_a_github_remote_with_the_app_configured_is_accepted(rig, monkeypatch):
    """The benign twin: this is the real deployed setup, and it must pass. Environment only —
    nothing here mints a token or reaches GitHub."""
    env, _ = rig
    _remote(env.repo, "https://github.com/acme/knowledge.git")
    monkeypatch.setenv(githubapp.APP_ID_ENV, "123456")
    monkeypatch.setenv(githubapp.INSTALLATION_ID_ENV, "7654321")
    monkeypatch.setenv(githubapp.PRIVATE_KEY_ENV, "-----BEGIN RSA PRIVATE KEY-----\nnot-a-key\n")

    worker._check_push_identity(env.repo)                # must not raise


def test_a_half_configured_app_is_refused_even_against_a_local_remote(rig, monkeypatch):
    """`githubapp.configured()` raises on half a credential, and this check calls it BEFORE looking
    at the destination — so somebody who meant to set the App up is told so whatever they are pushing
    to, rather than silently pushing as themselves against the bare remote."""
    env, _ = rig
    monkeypatch.setenv(githubapp.APP_ID_ENV, "123456")   # ...and nothing else

    with pytest.raises(LibrarianConfigError, match="half-configured"):
        worker._check_push_identity(env.repo)


def test_startup_checks_runs_the_push_identity_check_for_the_double_backend_too(rig, monkeypatch):
    """This check is backend-independent, and the double is the case that proves it: it pushes real
    commits to a real remote (that is how the whole processing suite works), so it can misattribute
    exactly as a real backend can."""
    env, deps = rig
    _remote(env.repo, "https://github.com/acme/knowledge.git")
    # keep `base_ref`'s fetch offline: the check under test runs after it, and a real fetch against
    # github.com in a unit test is a network call at best. Stubbed to the FIXTURE's real sha rather
    # than an all-zeros placeholder: `base_inputs.check_linter_at` reads the
    # linter's blob AT this sha before the push-identity check ever runs, and an all-zeros sha names
    # no commit at all — so the linter-missing refusal fired first and the check under test was
    # never reached (this is the fixture's own real sha, so the linter IS there and that check
    # passes through to the one this test is actually about).
    real_sha = support.branch_sha(env.repo)
    monkeypatch.setattr(worker.gitcmd, "base_ref",
                        lambda repo, branch: worker.gitcmd.BaseRef(real_sha, branch, False))

    with pytest.raises(LibrarianConfigError, match="git blame"):
        worker.startup_checks(deps.settings)


# ── a malformed entity registry: one loud line, not a stack trace ─────────────────────────────────
# Found by the traceback sweep, and one more occurrence of the raw-traceback defect class here.
# `registry.load_registry` raises a bare `ValueError`, which is neither a `LibrarianConfigError` nor a
# `LibrarianError` — so `cli.main`, which catches those, printed a Python stack trace at an operator
# for a one-character mistake in a config file. `base_inputs`' own re-raise already fixed exactly
# this for the file loaded on the line above it.
def test_a_malformed_entity_registry_refuses_with_a_sentence_and_not_a_traceback(rig):
    env, deps = rig
    with open(deps.settings.registry_path, "w", encoding="utf-8") as f:
        f.write('{"entities": {"broken": {}}}\n')
    # The registry is read at the BASE COMMIT, in every mode — a working-tree edit alone is
    # invisible to the run, so the sabotage has to reach `origin/main` the same way a steward's
    # `stigmergy-entities approve` would.
    support.commit_and_push(env.repo, "test: a malformed entity registry")

    with pytest.raises(LibrarianConfigError) as exc_info:
        worker.startup_checks(deps.settings)

    message = str(exc_info.value)
    # which file to open — named at the base commit (`origin/main@<sha>:ops/entity-registry.json`),
    # never a local disk path: a deployed worker has no working tree for a path like that to name.
    assert config.REGISTRY_RELPATH in message
    assert "broken" in message                         # and which entry in it
    assert "anchoring" in message                      # and why the worker will not run without it


def test_an_unreadable_entity_registry_is_refused_the_same_way(rig):
    """The read itself raises `OSError`/`JSONDecodeError` rather than `ValueError`, and neither is a
    `LibrarianError` either — so both go through the same guard."""
    env, deps = rig
    with open(deps.settings.registry_path, "w", encoding="utf-8") as f:
        f.write("{not json")
    support.commit_and_push(env.repo, "test: an unreadable entity registry")

    with pytest.raises(LibrarianConfigError, match="entity registry"):
        worker.startup_checks(deps.settings)


def test_the_real_fixture_registry_loads_without_complaint(rig):
    """The benign twin: this refusal must never fire on the registry every other test files
    against. Every `rig` test depends on it, but a check that refuses is worth its own assertion on
    the configuration it will actually meet."""
    _, deps = rig
    resolved = worker.startup_checks(deps.settings)
    assert resolved["registry"].canonical_id("Acme Corp")


def test_the_reaped_worktree_count_survives_the_new_checks(rig, tmp_path):
    """A regression guard on ORDER: the new checks sit between the linter check and the reap, so a
    misplaced early return would silently stop reaping a crashed run's leftovers."""
    env, deps = rig
    base = support.gitcmd.run("rev-parse", "HEAD", cwd=env.repo).stdout.strip()
    leftover = os.path.join(deps.settings.worktree_root,
                            support.crash_leftover_name(env.repo))
    os.makedirs(deps.settings.worktree_root, exist_ok=True)
    support.gitcmd.run("worktree", "add", "--detach", "--quiet", leftover, base, cwd=env.repo)

    assert worker.startup_checks(deps.settings)["reaped"] >= 1
