"""The two startup checks that make the environment the tool's problem instead of the operator's
memory, and keep `git blame` from lying.

Both are in `worker.startup_checks`, before a single item is claimed, for the reason that module's
docstring already gives: a credential fault discovered mid-run becomes N identical `failed` rows
with the real cause buried under attempts-exhausted noise. What these two add is that the faults
which cost a hand-drained walk **four separate detours in one day** are caught by the tool rather
than remembered by the operator:

- **no Claude credential + `--backend sdk`** — the key lives in the gitignored root env file, `make`
  exports it, a directly-invoked `.venv/bin/stigmergy-librarian` does not inherit it. Before this
  check the run reached the agent, the CLI subprocess exited unauthenticated, and the item burned
  both attempts before landing `failed`.

  The first version of that check was **two-way and refused a working configuration**, which is
  the second group of tests in this file: a Claude Code authenticated interactively keeps its
  login under the CLI's own config directory — the macOS Keychain, so no variable AND no file —
  and `make librarian-walk`
  would have refused to start on the machine the walk was for. It is three-way
  (`agent.credential_status`), and both halves are tested here: the ambient shape proceeds with one
  advisory line, a genuinely bare environment still refuses with the same message.
- **a github.com remote + no GitHub App** — the push would be made with whoever's disk credentials
  the process holds, so a page the librarian wrote would be blamed on a human.

`_check_push_identity` is exercised DIRECTLY rather than through `startup_checks` for the github.com
cases, and that is deliberate rather than lazy: `startup_checks` resolves the base ref first, which
runs `git fetch origin main` — against a real `https://github.com/...` remote that is a network call
in a unit test, slow at best and a credential prompt at worst. The check itself takes a repo path and
reads one local `git remote get-url`, so calling it is both honest and offline.
"""
import dataclasses
import logging
import os

import pytest

from stigmergy.librarian import agent as agent_module
from stigmergy.librarian import config, githubapp, worker
from stigmergy.librarian.errors import LibrarianConfigError
from tests.librarian import support

FAKE_KEY = "sk-ant-fixture-not-a-real-key"


def _remote(repo: str, url: str) -> None:
    support.gitcmd.run("remote", "set-url", "origin", url, cwd=repo)


# ── the agent credential (`sdk` backend only) ─────────────────────────────────────────────────────
def test_credential_present_accepts_each_of_the_three_authentication_variables():
    """All three, from the module's own tuple rather than retyped: a credential the agent subprocess
    does not INHERIT (`agent.AGENT_ENV_PASSTHROUGH`) must not be one this check accepts, so the two
    lists are derived from each other."""
    for name in agent_module.CREDENTIAL_ENV:
        assert name in agent_module.AGENT_ENV_PASSTHROUGH
        assert agent_module.credential_present({name: FAKE_KEY}) is True


def test_credential_present_accepts_a_gateway_base_url_on_its_own():
    """A CLI pointed at a gateway carries its credential in whatever that gateway wants, so the
    presence of a base URL is itself evidence that authentication is somebody else's problem.
    Without this, a perfectly working proxied setup would be refused."""
    assert agent_module.credential_present({"ANTHROPIC_BASE_URL": "https://gateway.internal"}) is True


def test_credential_present_is_false_for_an_empty_environment_and_for_empty_values():
    assert agent_module.credential_present({}) is False
    # an exported-but-empty variable is the shape a half-written env file produces
    assert agent_module.credential_present({"ANTHROPIC_API_KEY": ""}) is False


def test_startup_checks_refuses_an_sdk_run_with_no_credential(rig):
    """The refusal names the variables to set and points at the make target that exports them —
    which is what turns a four-detour day into one line."""
    _, deps = rig
    settings = dataclasses.replace(deps.settings, backend="sdk")

    with pytest.raises(LibrarianConfigError) as exc_info:
        worker.startup_checks(settings)

    message = str(exc_info.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "make librarian-walk" in message
    # and it must NOT offer the double as a workaround: that files fabricated pages, and suggesting
    # it here would invite committing the double's output to the company's knowledge repo
    assert "--backend double" not in message


def test_startup_checks_does_not_require_a_credential_for_the_double(rig):
    """The specificity half. The offline double never authenticates anything, so requiring a key of
    it would be a check that can only fail on something nothing was going to use — the same argument
    the skill check already makes for itself."""
    _, deps = rig
    assert deps.settings.backend == "double"
    worker.startup_checks(deps.settings)                 # must not raise


def test_the_credential_check_reads_the_process_environment_by_default(rig, monkeypatch):
    """Driven through the real environment (the package's autouse fixture clears it, so this is the
    one place the positive path is proven end to end rather than through an injected dict)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    worker._check_agent_credential()                     # must not raise


# ── the ambient credential: the benign twin the two-way check never had ───────────────────────────
# Everything below is `agent.credential_status`, which replaced a two-way check that refused a
# WORKING configuration. The shape it got wrong is the DEFAULT one for anybody using Claude Code
# interactively: no variable set, the CLI's own config directory present, and — on macOS — no
# credentials file inside it either, because the login lives in the Keychain. `make librarian-walk`
# would have refused to start on the machine the walk was for, making the guard one more detour
# than the ones it was written to prevent — hence the benign twin below, and a case for the default
# configuration nobody had run it against.
def test_credential_status_is_env_when_a_variable_carries_one_and_stats_nothing():
    """The cheap branch, asserted as a MECHANISM and not as an outcome: the filesystem seam is
    handed a callable that fails the test if it is reached at all, so this cannot pass because a
    directory happened to exist on the machine running it."""
    def never(path):
        raise AssertionError(f"the env branch must not stat anything, and it stat'd {path}")

    for name in (*agent_module.CREDENTIAL_ENV, "ANTHROPIC_BASE_URL"):
        assert agent_module.credential_status(
            {name: FAKE_KEY}, config_dir_exists=never) == agent_module.CREDENTIAL_IN_ENV


def test_credential_status_is_ambient_for_a_keychain_login_with_no_variable_and_no_file(tmp_path):
    """**THE defect, in the shape it was found in.** No `ANTHROPIC_*`, no `CLAUDE_CODE_OAUTH_TOKEN`,
    a `~/.claude` that exists, and no `.credentials.json` in it. The real agent authenticates fine in
    this shape — it ran four times during a real walk and its verification — and the two-way check
    called it "no credential" and refused the run.

    Deliberately against the REAL `os.path.isdir` over a real directory rather than an injected
    predicate: the point of this test is the default configuration, and a stubbed filesystem would be
    testing the stub."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    assert not (home / ".claude" / ".credentials.json").exists()
    environ = {"PATH": "/usr/bin:/bin", "HOME": str(home)}

    assert agent_module.credential_present(environ) is False          # the old gate: refuse
    assert agent_module.credential_status(environ) == agent_module.CREDENTIAL_AMBIENT


def test_credential_status_is_missing_for_a_home_that_has_never_authenticated(tmp_path):
    """The genuine failure the check exists for, and it must still be caught: the expensive outcome
    is an unauthenticated CLI subprocess burning both agent attempts and landing the item `failed`
    with a stage name, which reads as a product defect and is a missing export."""
    environ = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "never-logged-in")}
    assert agent_module.credential_status(environ) == agent_module.CREDENTIAL_MISSING


def test_credential_status_will_not_mistake_a_file_named_claude_for_the_config_directory(tmp_path):
    """`isdir`, not `exists`. A stray file is not a configuration directory, and accepting one would
    trade the refused-working-configuration bug for a proceeded-on-nothing one."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").write_text("not a directory", encoding="utf-8")
    assert agent_module.credential_status({"HOME": str(home)}) == agent_module.CREDENTIAL_MISSING


def test_the_explicit_config_dir_wins_over_the_home_default_in_both_directions(tmp_path):
    """`CLAUDE_CONFIG_DIR` is in the passthrough, so the subprocess honours it — and a check that
    asked a different question than the subprocess will is the mistake the skill check already made
    once (it read the local checkout while the agent read the worktree).

    Both directions, because the second is what the package's autouse fixture relies on to make "no
    credential" true on a laptop whose real `~/.claude` exists."""
    elsewhere, home = tmp_path / "elsewhere", tmp_path / "home"
    elsewhere.mkdir()
    (home / ".claude").mkdir(parents=True)

    # pointed somewhere that exists, while $HOME/.claude does not → ambient
    bare_home = {"HOME": str(tmp_path / "bare"), agent_module.CONFIG_DIR_ENV: str(elsewhere)}
    assert agent_module.agent_config_dir(bare_home) == str(elsewhere)
    assert agent_module.credential_status(bare_home) == agent_module.CREDENTIAL_AMBIENT

    # pointed somewhere that does NOT exist, while $HOME/.claude does → missing, not rescued
    redirected = {"HOME": str(home), agent_module.CONFIG_DIR_ENV: str(tmp_path / "nowhere")}
    assert agent_module.credential_status(redirected) == agent_module.CREDENTIAL_MISSING


def test_credential_status_is_missing_when_the_subprocess_would_get_no_home(tmp_path):
    """Answered from the ALLOW-LIST, not from this process: `agent_env` is what the subprocess gets,
    and a CLI with no `HOME` cannot reach a stored login however the parent is set up. Proven with a
    real `~/.claude` on disk that the resolution must refuse to see."""
    (tmp_path / ".claude").mkdir()
    assert agent_module.agent_config_dir({"PATH": "/usr/bin:/bin"}) is None
    assert agent_module.credential_status({"PATH": "/usr/bin:/bin"}) == agent_module.CREDENTIAL_MISSING


def test_the_variables_the_ambient_path_depends_on_are_ones_the_subprocess_inherits():
    """Derived-not-retyped, the same property the credential tuple asserts one test up: a directory
    the subprocess is never told about is a directory this check must not count on."""
    assert agent_module.CONFIG_DIR_ENV in agent_module.AGENT_ENV_PASSTHROUGH
    assert "HOME" in agent_module.AGENT_ENV_PASSTHROUGH


def test_the_guard_proceeds_on_ambient_auth_and_says_what_the_run_is_relying_on(tmp_path, caplog):
    """Proceeding silently would be the other half-fix: no pre-flight can tell a Keychain login from
    no login without spending a request, so the operator gets the diagnosis BEFORE the run rather
    than a `failed` row after it.

    WARNING and not INFO is load-bearing, not a style choice: nothing in this package configures
    logging, so `logging.lastResort` prints WARNING and above to stderr and drops INFO entirely — an
    advisory at INFO would not reach the operator at all."""
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    environ = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
               agent_module.CONFIG_DIR_ENV: str(config_dir)}

    with caplog.at_level(logging.WARNING, logger="stigmergy.librarian.worker"):
        worker._check_agent_credential(environ)          # must not raise

    assert len(caplog.records) == 1, "the advisory is one line, not a paragraph of them"
    assert caplog.records[0].levelno == logging.WARNING
    message = caplog.records[0].getMessage()
    assert str(config_dir) in message                    # what it is relying on
    assert agent_module.CREDENTIAL_ENV[0] in message      # ...and what would remove the doubt


def test_the_guard_stays_silent_when_the_environment_carries_the_credential(caplog):
    """The specificity half of the advisory: a fully configured run must not be told it is relying on
    anything, or the line stops meaning what it says."""
    with caplog.at_level(logging.WARNING, logger="stigmergy.librarian.worker"):
        worker._check_agent_credential({"ANTHROPIC_API_KEY": FAKE_KEY})
    assert caplog.records == []


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
    `processing._repo_slug` already parses both — a check that only understood https would let the
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
    """Unlike the credential check, this one is backend-independent: the double pushes real commits
    to a real remote (that is how the whole processing suite works), so it can misattribute exactly
    as the sdk backend can."""
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
# for a one-character mistake in a config file. `acl_rules._guard_delegation` already fixed exactly
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
