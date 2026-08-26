"""Contract for keeping the staging knowledge checkout current before consuming it."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import time

import pytest

from tests import childwatch

ROOT = pathlib.Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "refresh_staging_checkout.sh"
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy_staging.sh"
MAKEFILE = ROOT / "Makefile"


def _git(cwd: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: pathlib.Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _staging_checkout(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)

    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Test User")
    (seed / "ops").mkdir()
    (seed / "ops" / "identities.json").write_text("{}\n", encoding="utf-8")
    _commit(seed, "initial controls")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "main")

    checkout = tmp_path / "staging"
    subprocess.run(["git", "clone", "-q", str(origin), str(checkout)], check=True)
    _git(checkout, "config", "user.email", "test@example.com")
    _git(checkout, "config", "user.name", "Test User")
    return seed, checkout, origin


def _advance_remote(seed: pathlib.Path) -> str:
    (seed / "ops" / "identities.json").write_text('{"fresh": true}\n', encoding="utf-8")
    remote_head = _commit(seed, "advance controls")
    _git(seed, "push", "-q", "origin", "main")
    return remote_head


def _refresh(checkout: pathlib.Path) -> subprocess.CompletedProcess[str]:
    assert GUARD.is_file(), f"missing shared staging checkout guard: {GUARD}"
    return subprocess.run(
        ["bash", str(GUARD), str(checkout)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _assert_safe_failure(result: subprocess.CompletedProcess[str], secret: str = "") -> None:
    assert result.returncode != 0
    assert len(result.stderr) < 256
    if secret:
        assert secret not in result.stdout + result.stderr


def test_refreshes_a_clean_stale_checkout_to_its_configured_local_upstream(tmp_path):
    seed, checkout, _origin = _staging_checkout(tmp_path)
    remote_head = _advance_remote(seed)

    result = _refresh(checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(checkout, "rev-parse", "HEAD") == remote_head
    assert _git(checkout, "rev-parse", "@{upstream}") == remote_head


def test_refresh_rejects_a_non_git_root_with_bounded_diagnostic(tmp_path):
    non_git = tmp_path / "not-a-checkout"
    non_git.mkdir()

    result = _refresh(non_git)

    _assert_safe_failure(result)
    assert "not a Git worktree" in result.stderr


@pytest.mark.parametrize("case", ["divergent", "dirty", "detached", "missing_upstream"])
def test_refresh_fails_closed_without_moving_an_unsafe_checkout(tmp_path, case):
    seed, checkout, _origin = _staging_checkout(tmp_path)
    _advance_remote(seed)

    if case == "divergent":
        (checkout / "ops" / "local.json").write_text("{}\n", encoding="utf-8")
        _commit(checkout, "local divergence")
    elif case == "dirty":
        (checkout / "ops" / "identities.json").write_text('{"dirty": true}\n', encoding="utf-8")
    elif case == "detached":
        _git(checkout, "checkout", "-q", "--detach")
    else:
        _git(checkout, "config", "--unset", "branch.main.remote")
        _git(checkout, "config", "--unset", "branch.main.merge")

    before = _git(checkout, "rev-parse", "HEAD")
    result = _refresh(checkout)

    assert result.returncode != 0
    assert _git(checkout, "rev-parse", "HEAD") == before


def test_refresh_rejects_an_unresolved_upstream_without_moving_head(tmp_path):
    _seed, checkout, _origin = _staging_checkout(tmp_path)
    _git(checkout, "config", "branch.main.merge", "refs/heads/no-such-branch")
    before = _git(checkout, "rev-parse", "HEAD")

    result = _refresh(checkout)

    _assert_safe_failure(result)
    assert "upstream could not be resolved" in result.stderr
    assert _git(checkout, "rev-parse", "HEAD") == before


def test_refresh_hides_credentials_when_fetching_the_configured_upstream_fails(tmp_path):
    _seed, checkout, _origin = _staging_checkout(tmp_path)
    secret = "staging-fetch-secret"
    _git(checkout, "remote", "set-url", "origin", f"https://token:{secret}@127.0.0.1:9/private.git")
    before = _git(checkout, "rev-parse", "HEAD")

    result = _refresh(checkout)

    _assert_safe_failure(result, secret)
    assert "fetch failed" in result.stderr
    assert _git(checkout, "rev-parse", "HEAD") == before


def test_staging_consumers_run_the_shared_guard_before_reading_the_checkout():
    guard_call = "scripts/refresh_staging_checkout.sh"
    deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    rebuild = MAKEFILE.read_text(encoding="utf-8").split("rebuild-staging:", maxsplit=1)[1]

    assert guard_call in deploy
    materialize = 'git -C "$root" show "$sha:ops/$name"'
    assert materialize in deploy
    assert deploy.index(guard_call) < deploy.index(materialize)
    assert 'cp "$STIGMERGY_REPO/ops/' not in deploy
    assert guard_call in rebuild
    assert rebuild.index(guard_call) < rebuild.index("stigmergy-index --rebuild")


def test_refresh_reports_the_canonical_checkout_root_and_verified_sha(tmp_path):
    seed, checkout, _origin = _staging_checkout(tmp_path)
    remote_head = _advance_remote(seed)

    result = _refresh(checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == f"staging-refresh: root={checkout.resolve()} head={remote_head}\n"


def test_rebuild_staging_keeps_dsn_out_of_output_and_index_argv(tmp_path):
    _seed, checkout, _origin = _staging_checkout(tmp_path)
    sentinel = 'postgresql://staging-dsn-sentinel:quote"@example.invalid/staging'
    venv = tmp_path / "venv"
    index = venv / "bin" / "stigmergy-index"
    index.parent.mkdir(parents=True)
    (venv / ".deps-ok").touch()
    argv_path = tmp_path / "index.argv"
    dsn_path = tmp_path / "index.dsn"
    index.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$@" > "$INDEX_ARGV"\n'
        'printf "%s" "${STIGMERGY_INDEX_DSN:-}" > "$INDEX_DSN"\n',
        encoding="utf-8",
    )
    index.chmod(0o755)
    shell_argv_path = tmp_path / "shell.argv"
    shell = tmp_path / "shell"
    shell.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$@" > "$SHELL_ARGV"\n'
        'exec /bin/sh "$@"\n',
        encoding="utf-8",
    )
    shell.chmod(0o755)
    make_args = [
        "make",
        "--no-print-directory",
        f"VENV={venv}",
        f"STAGING_DSN={sentinel}",
        f"STIGMERGY_REPO={checkout}",
        f"SHELL={shell}",
        "rebuild-staging",
    ]

    dry_run = subprocess.run(
        [*make_args[:2], "-n", *make_args[2:]],
        cwd=ROOT,
        env=os.environ,
        capture_output=True,
        text=True,
        timeout=30,
    )

    result = subprocess.run(
        make_args,
        cwd=ROOT,
        env={
            **os.environ,
            "INDEX_ARGV": str(argv_path),
            "INDEX_DSN": str(dsn_path),
            "SHELL_ARGV": str(shell_argv_path),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert {
        "dry_run": sentinel not in dry_run.stdout + dry_run.stderr,
        "launcher_argv": sentinel not in shell_argv_path.read_text(encoding="utf-8"),
    } == {"dry_run": True, "launcher_argv": True}
    assert result.returncode == 0, result.stdout + result.stderr
    assert {
        "output": sentinel not in result.stdout + result.stderr,
        "argv": sentinel not in argv_path.read_text(encoding="utf-8"),
        "environment": dsn_path.read_text(encoding="utf-8") == sentinel,
        "canonical_root": str(checkout.resolve()) in argv_path.read_text(encoding="utf-8"),
    } == {
        "output": True,
        "argv": True,
        "environment": True,
        "canonical_root": True,
    }


def test_deploy_bakes_only_committed_runtime_controls_from_refresh_root_and_sha(tmp_path):
    _seed, checkout, _origin = _staging_checkout(tmp_path)
    controls = {
        "identities.json": b'{"committed":"identity"}\n',
        "entity-registry.json": b'{"version":1,"entities":{},"redirects":{}}\n',
        "slack-channels.json": b'{"committed":"channels"}\n',
    }
    for name, content in controls.items():
        (checkout / "ops" / name).write_bytes(content)
    assert not (checkout / "ops" / "acl_registry.yml").exists()
    assert not (checkout / "ops" / "prompt_policy.yml").exists()
    commit = _commit(checkout, "add committed controls")

    worktree = tmp_path / "worktree"
    scripts = worktree / "scripts"
    deploy = worktree / "deploy"
    scripts.mkdir(parents=True)
    deploy.mkdir()
    deploy_script = scripts / "deploy_staging.sh"
    deploy_script.write_bytes(DEPLOY_SCRIPT.read_bytes())
    deploy_script.chmod(0o755)
    refresh = scripts / "refresh_staging_checkout.sh"
    mutated = tmp_path / "mutated-controls"
    mutated.mkdir()
    for name in controls:
        (mutated / name).write_bytes(b"mutable-worktree-content\n")
    refresh.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf "staging-refresh: root=%s head=%s\\n" "$1" "$STAGING_SHA"\n'
        'for path in "$1"/ops/*; do ln -sfn "$MUTATED/$(basename "$path")" "$path"; done\n',
        encoding="utf-8",
    )
    refresh.chmod(0o755)
    fake_python = tmp_path / "python"
    fake_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    seen = tmp_path / "seen"
    fly = bin_dir / "fly"
    fly.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "deploy" ]; then mkdir -p "$SEEN"; cp deploy/* "$SEEN"/; fi\n',
        encoding="utf-8",
    )
    fly.chmod(0o755)

    result = subprocess.run(
        ["bash", str(deploy_script)],
        cwd=worktree,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "STIGMERGY_REPO": str(checkout),
            "STIGMERGY_PYTHON": str(fake_python),
            "STAGING_SHA": commit,
            "MUTATED": str(mutated),
            "SEEN": str(seen),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert {path.name: path.read_bytes() for path in seen.iterdir()} == controls


def test_overlapping_staging_deploy_fails_closed_and_releases_its_lock(tmp_path):
    _seed, checkout, _origin = _staging_checkout(tmp_path)
    controls = {
        "identities.json": b'{}\n',
        "entity-registry.json": b'{"version":1,"entities":{},"redirects":{}}\n',
        "slack-channels.json": b'{}\n',
        "acl_registry.yml": b"legacy: true\n",
        "prompt_policy.yml": b"legacy: true\n",
    }
    for name, content in controls.items():
        (checkout / "ops" / name).write_bytes(content)
    commit = _commit(checkout, "add controls for deploy lock")

    worktree = tmp_path / "worktree"
    scripts = worktree / "scripts"
    scripts.mkdir(parents=True)
    (worktree / "deploy").mkdir()
    deploy_script = scripts / "deploy_staging.sh"
    deploy_script.write_bytes(DEPLOY_SCRIPT.read_bytes())
    deploy_script.chmod(0o755)
    refresh = scripts / "refresh_staging_checkout.sh"
    refresh.write_text(
        "#!/usr/bin/env bash\n"
        'printf "refresh\\n" >> "$REFRESH_CALLS"\n'
        'printf "staging-refresh: root=%s head=%s\\n" "$1" "$STAGING_SHA"\n',
        encoding="utf-8",
    )
    refresh.chmod(0o755)
    calls = tmp_path / "fly.calls"
    validation_calls = tmp_path / "validation.calls"
    refresh_calls = tmp_path / "refresh.calls"
    git_show_calls = tmp_path / "git-show.calls"
    started = tmp_path / "fly.started"
    owner = tmp_path / "fly.owner"
    release = tmp_path / "fly.release"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        'for argument in "$@"; do\n'
        '  case "$argument" in "$DEPLOY_DIR"|"$DEPLOY_DIR"/*) '
        'printf "validation\\n" >> "$VALIDATION_CALLS"; exit 0;; esac\n'
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None
    git = bin_dir / "git"
    git.write_text(
        "#!/usr/bin/env bash\n"
        'for argument in "$@"; do\n'
            '  if [ "$argument" = "show" ]; then printf "show\\n" >> '
            '"$GIT_SHOW_CALLS"; break; fi\n'
        "done\n"
        'exec "$REAL_GIT" "$@"\n',
        encoding="utf-8",
    )
    git.chmod(0o755)
    fly = bin_dir / "fly"
    fly.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [ "$1" = "deploy" ]; then\n'
        '  printf "deploy\\n" >> "$FLY_CALLS"\n'
        '  if [ ! -e "$FLY_OWNER" ]; then\n'
        '    : > "$FLY_OWNER"\n'
        '    : > "$FLY_STARTED"\n'
        '    while [ ! -e "$FLY_RELEASE" ]; do sleep 0.05; done\n'
        "  fi\n"
        "fi\n",
        encoding="utf-8",
    )
    fly.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STIGMERGY_REPO": str(checkout),
        "STIGMERGY_PYTHON": str(fake_python),
        "STAGING_SHA": commit,
        "FLY_CALLS": str(calls),
        "VALIDATION_CALLS": str(validation_calls),
        "REFRESH_CALLS": str(refresh_calls),
        "GIT_SHOW_CALLS": str(git_show_calls),
        "DEPLOY_DIR": str(worktree / "deploy"),
        "REAL_GIT": real_git,
        "FLY_STARTED": str(started),
        "FLY_OWNER": str(owner),
        "FLY_RELEASE": str(release),
    }
    first = childwatch.spawn(
        ["bash", str(deploy_script)],
        cwd=worktree,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        for _ in range(100):
            if started.exists():
                break
            time.sleep(0.05)
        else:
            pytest.fail("first staging deploy did not reach Fly")

        first_show_calls = git_show_calls.read_text(encoding="utf-8")
        second = subprocess.run(
            ["bash", str(deploy_script)],
            cwd=worktree,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert second.returncode != 0
        assert calls.read_text(encoding="utf-8") == "deploy\n"
        assert validation_calls.read_text(encoding="utf-8") == "validation\n"
        assert refresh_calls.read_text(encoding="utf-8") == "refresh\n"
        assert git_show_calls.read_text(encoding="utf-8") == first_show_calls
    finally:
        release.touch()
        first_stdout, first_stderr = first.communicate(timeout=30)

    assert first.returncode == 0, first_stdout + first_stderr
    third = subprocess.run(
        ["bash", str(deploy_script)],
        cwd=worktree,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert third.returncode == 0, third.stdout + third.stderr
    assert calls.read_text(encoding="utf-8") == "deploy\ndeploy\n"
    assert validation_calls.read_text(encoding="utf-8") == "validation\nvalidation\n"
    assert refresh_calls.read_text(encoding="utf-8") == "refresh\nrefresh\n"
    assert git_show_calls.read_text(encoding="utf-8") == first_show_calls * 2
