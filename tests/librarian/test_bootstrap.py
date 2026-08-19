"""`stigmergy-librarian-boot` (`librarian.bootstrap`) — the checkout verification and `worker_env`.
The assertions are on the CONSTRUCTED deployed configuration, never on a double's runtime.

Pure git + plain dicts throughout: `verify_checkout_at_base` needs a real checkout (three distinct
refusals are real git states, not stand-ins for them), and `worker_env` is a pure function of a
dict → dict (module docstring: "returned as a plain value on purpose... assertable with no
container, no Fly and no key").
"""
import os
import subprocess

import pytest

from stigmergy.librarian import bootstrap
from stigmergy.librarian.errors import LibrarianConfigError


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(root: str) -> str:
    repo = os.path.join(root, "checkout")
    os.makedirs(repo)
    _git("init", "--quiet", "-b", "main", repo, cwd=root)
    _git("config", "user.name", "fixture", cwd=repo)
    _git("config", "user.email", "fixture@example.com", cwd=repo)
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("x\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "--quiet", "-m", "seed", cwd=repo)
    return repo


def _bare_and_checkout(tmp_path) -> tuple[str, str]:
    bare = str(tmp_path / "origin.git")
    _git("init", "--bare", "--quiet", "-b", "main", bare, cwd=str(tmp_path))
    repo = _init_repo(str(tmp_path))
    _git("remote", "add", "origin", bare, cwd=repo)
    _git("push", "--quiet", "-u", "origin", "main", cwd=repo)
    return bare, repo


# ══════════════════════════════════════════════════════════════════════════════════════════════
# `verify_checkout_at_base` — three distinct refusals, plus the benign twin.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_refuses_when_the_base_did_not_come_from_the_remote(tmp_path):
    """No remote at all: `gitcmd.base_ref` falls back to the local branch, and that fallback —
    right for a laptop — is exactly what this check exists to refuse for a deployed worker."""
    repo = _init_repo(str(tmp_path))
    with pytest.raises(LibrarianConfigError, match="no reachable"):
        bootstrap.verify_checkout_at_base(repo, "main")


def test_refuses_when_head_is_not_the_base_commit(tmp_path):
    bare, repo = _bare_and_checkout(tmp_path)
    with open(os.path.join(repo, "second.md"), "w") as f:
        f.write("a second commit the remote has never seen\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "--quiet", "-m", "local-only commit", cwd=repo)

    with pytest.raises(LibrarianConfigError, match="worktrees branch from"):
        bootstrap.verify_checkout_at_base(repo, "main")


def test_refuses_a_dirty_working_tree(tmp_path):
    _bare, repo = _bare_and_checkout(tmp_path)
    with open(os.path.join(repo, "untracked.md"), "w") as f:
        f.write("something wrote into the checkout itself\n")

    with pytest.raises(LibrarianConfigError, match="uncommitted changes"):
        bootstrap.verify_checkout_at_base(repo, "main")


def test_the_benign_twin_a_checkout_exactly_at_the_remotes_tip_passes(tmp_path):
    bare, repo = _bare_and_checkout(tmp_path)
    base = bootstrap.verify_checkout_at_base(repo, "main")
    assert base.remote is True
    assert base.sha == _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def test_prepare_composes_ensure_checkout_then_verify(tmp_path):
    """`prepare` = clone-or-update, then verify — the whole of what a container needs before its
    first claim (module docstring)."""
    bare = str(tmp_path / "origin.git")
    _git("init", "--bare", "--quiet", "-b", "main", bare, cwd=str(tmp_path))
    seed = _init_repo(str(tmp_path / "seed"))
    _git("remote", "add", "origin", bare, cwd=seed)
    _git("push", "--quiet", "-u", "origin", "main", cwd=seed)

    container_repo = str(tmp_path / "container-checkout")
    base = bootstrap.prepare(repo=container_repo, url=bare, branch="main")
    assert os.path.isdir(container_repo)
    assert base.remote is True


def test_ensure_checkout_refuses_a_fresh_container_with_no_url_configured(tmp_path):
    """A container starts with no knowledge-repo checkout at all — module docstring's own opening
    fact. With neither an existing checkout nor a URL to clone from, there is nothing to do and no
    machine this should silently succeed on."""
    with pytest.raises(LibrarianConfigError, match="no.*checkout"):
        bootstrap.ensure_checkout(str(tmp_path / "nothing-here"), url="", branch="main")


def test_ensure_checkout_refuses_a_checkout_of_the_wrong_repository(tmp_path):
    """A volume reused across deployments, or mounted onto the wrong container — refused by name
    rather than fast-forwarded, verified and filed into as if it were the configured repo."""
    bare_a = str(tmp_path / "a.git")
    bare_b = str(tmp_path / "b.git")
    _git("init", "--bare", "--quiet", "-b", "main", bare_a, cwd=str(tmp_path))
    _git("init", "--bare", "--quiet", "-b", "main", bare_b, cwd=str(tmp_path))
    seed = _init_repo(str(tmp_path / "seed"))
    _git("remote", "add", "origin", bare_a, cwd=seed)
    _git("push", "--quiet", "-u", "origin", "main", cwd=seed)

    existing = str(tmp_path / "existing-checkout")
    _git("clone", "--quiet", bare_a, existing, cwd=str(tmp_path))

    with pytest.raises(LibrarianConfigError, match="different repository"):
        bootstrap.ensure_checkout(existing, url=bare_b, branch="main")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# `worker_env` — a plain dict in, a plain dict out: assert the constructed configuration, never
# the double's runtime.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_worker_env_exports_the_require_remote_base_flag():
    env = bootstrap.worker_env({})
    assert env[bootstrap.config.REQUIRE_REMOTE_BASE_ENV] == "1"


def test_worker_env_strips_the_read_paths_openai_key():
    """The CONSTRUCTED environment, not a run through the double. `bootstrap.worker_env({})` — an
    EMPTY input — proves the key is stripped even in the degenerate case, and the populated case
    below proves it is stripped rather than merely absent by coincidence."""
    assert "OPENAI_API_KEY" not in bootstrap.worker_env({})

    populated = bootstrap.worker_env({"OPENAI_API_KEY": "sk-should-not-reach-the-worker",
                                      "ANTHROPIC_API_KEY": "keep-me", "PATH": "/usr/bin"})
    assert "OPENAI_API_KEY" not in populated
    assert populated["ANTHROPIC_API_KEY"] == "keep-me"
    assert populated["PATH"] == "/usr/bin"


def test_worker_env_strips_the_embedders_own_key_and_keeps_the_providers(monkeypatch):
    """`EMBED_API_KEY` is the read path's embedder credential under its own name (the key for an
    `$EMBED_BASE_URL` host), so it is stripped exactly as `OPENAI_API_KEY` is — while
    `OPENROUTER_API_KEY`, a PROVIDER key the filing model may authenticate with, must pass
    through: stripping it would make `openrouter:` models undeployable with nothing to say why."""
    populated = bootstrap.worker_env({"EMBED_API_KEY": "sk-embed-host",
                                      "OPENROUTER_API_KEY": "sk-or-keep-me"})
    assert "EMBED_API_KEY" not in populated
    assert populated["OPENROUTER_API_KEY"] == "sk-or-keep-me"


def test_worker_env_keeps_everything_else_from_the_source_environment():
    source = {"HOME": "/home/app", "STIGMERGY_REPO": "/home/app/knowledge", "TZ": "UTC"}
    env = bootstrap.worker_env(source)
    for key, value in source.items():
        assert env[key] == value


def test_worker_env_defaults_to_os_environ_when_none_is_given(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "should-be-stripped")
    monkeypatch.setenv("SOME_OTHER_VAR", "kept")
    env = bootstrap.worker_env()
    assert "OPENAI_API_KEY" not in env
    assert env["SOME_OTHER_VAR"] == "kept"


def test_worker_command_resolves_to_stigmergy_librarian_run():
    argv = bootstrap.worker_command(["--json"])
    assert argv[-2:] == ["run", "--json"]
    assert "stigmergy-librarian" in argv[0] or argv[0] == __import__("sys").executable


def test_worker_launch_returns_argv_and_the_constructed_env_as_plain_values():
    """The seam this is proven through: `main` never has to actually exec anything for this to be
    assertable — `worker_launch` returns the two plain values `os.execvpe` would have
    received."""
    argv, env = bootstrap.worker_launch([], environ={"OPENAI_API_KEY": "x", "KEEP": "y"})
    assert "OPENAI_API_KEY" not in env
    assert env["KEEP"] == "y"
    assert env[bootstrap.config.REQUIRE_REMOTE_BASE_ENV] == "1"
    assert argv[-1] == "run"


def test_main_execs_with_the_constructed_argv_and_env_never_the_read_paths_key(tmp_path, monkeypatch):
    """`main`'s own contract: prepare, verify, THEN exec with the constructed environment —
    `execute` is injected exactly so this is provable without a real process replacement
    (`bootstrap.main`'s own docstring)."""
    bare, repo = _bare_and_checkout(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-the-worker")
    captured = {}

    def fake_execute(path, argv, env):
        captured["path"], captured["argv"], captured["env"] = path, argv, env

    rc = bootstrap.main(["--repo", repo, "--url", bare, "--branch", "main"], execute=fake_execute)
    assert rc == 0
    assert "OPENAI_API_KEY" not in captured["env"]
    assert captured["env"][bootstrap.config.REQUIRE_REMOTE_BASE_ENV] == "1"
    assert captured["argv"][-1] == "run"


def test_main_check_only_verifies_and_exits_without_execing(tmp_path):
    bare, repo = _bare_and_checkout(tmp_path)
    called = []
    rc = bootstrap.main(["--repo", repo, "--url", bare, "--branch", "main", "--check-only"],
                        execute=lambda *a: called.append(a))
    assert rc == 0
    assert called == []


def test_main_refuses_cleanly_with_no_traceback_when_verification_fails(tmp_path, capsys):
    repo = _init_repo(str(tmp_path))     # no remote at all -> verify_checkout_at_base refuses
    rc = bootstrap.main(["--repo", repo, "--url", "", "--branch", "main"],
                        execute=lambda *a: (_ for _ in ()).throw(AssertionError("must not exec")))
    assert rc == bootstrap.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "stigmergy-librarian-boot:" in err
