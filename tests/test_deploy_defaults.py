import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy_staging.sh"
EMPTY_DEFAULTS = {
    "identities.json": {},
    "entity-registry.json": {"version": 1, "entities": {}, "redirects": {}},
    "slack-channels.json": {},
}
ROSTER = {
    "someone@example.com": {
        "display_name": "Someone",
        "groups": ["brain-admins", "finance"],
        "default_audience": None,
    }
}
REGISTRY = {"version": 1, "entities": {}, "redirects": {}}
CHANNELS = {"C0123456789": ["finance"]}


@pytest.mark.parametrize("name, expected", sorted(EMPTY_DEFAULTS.items()))
def test_committed_deploy_controls_are_fail_closed_defaults(name, expected):
    assert json.loads((DEPLOY / name).read_text(encoding="utf-8")) == expected


def test_deploy_directory_contains_only_known_artifacts():
    assert {path.name for path in DEPLOY.iterdir()} == {
        *EMPTY_DEFAULTS,
        "slack-app-manifest.json",
    }


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


def _run_deploy(
    tmp_path: pathlib.Path,
    *,
    roster=ROSTER,
    python: str | None = sys.executable,
) -> tuple[subprocess.CompletedProcess[str], pathlib.Path, pathlib.Path]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(DEPLOY_SCRIPT, scripts / DEPLOY_SCRIPT.name)
    refresh_script = scripts / "refresh_staging_checkout.sh"
    refresh_script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf "staging-refresh: root=%s head=%s\\n" "$1" "$STAGING_SHA"\n',
        encoding="utf-8",
    )
    refresh_script.chmod(0o755)

    deploy = tmp_path / "deploy"
    probe = deploy / "unmanaged" / "tracked.txt"
    probe.parent.mkdir(parents=True)
    probe.write_text("keep\n", encoding="utf-8")

    knowledge = tmp_path / "knowledge"
    subprocess.run(["git", "init", "-q", "-b", "main", str(knowledge)], check=True)
    _git(knowledge, "config", "user.email", "test@example.com")
    _git(knowledge, "config", "user.name", "Test User")
    ops = knowledge / "ops"
    ops.mkdir(parents=True)
    (ops / "identities.json").write_text(json.dumps(roster), encoding="utf-8")
    (ops / "entity-registry.json").write_text(json.dumps(REGISTRY), encoding="utf-8")
    (ops / "slack-channels.json").write_text(json.dumps(CHANNELS), encoding="utf-8")
    staging_sha = _commit(knowledge, "test controls")

    seen = tmp_path / "seen"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fly = bin_dir / "fly"
    fly.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "deploy" ]; then mkdir -p "$SEEN"; cp deploy/*.json "$SEEN"/; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fly.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STIGMERGY_REPO": str(ops.parent),
        "STAGING_SHA": staging_sha,
        "SEEN": str(seen),
    }
    if python is not None:
        env["STIGMERGY_PYTHON"] = python
    else:
        env["STIGMERGY_PYTHON"] = str(tmp_path / "missing-python")
    result = subprocess.run(
        ["bash", str(scripts / DEPLOY_SCRIPT.name)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result, deploy, seen


def test_deploy_bakes_all_controls_then_restores_defaults(tmp_path):
    result, deploy, seen = _run_deploy(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads((seen / "identities.json").read_text()) == ROSTER
    assert json.loads((seen / "entity-registry.json").read_text()) == REGISTRY
    assert json.loads((seen / "slack-channels.json").read_text()) == CHANNELS
    assert {
        name: json.loads((deploy / name).read_text()) for name in EMPTY_DEFAULTS
    } == EMPTY_DEFAULTS
    assert (deploy / "unmanaged" / "tracked.txt").read_text() == "keep\n"


def test_invalid_control_file_stops_deploy(tmp_path):
    result, _, seen = _run_deploy(tmp_path, roster={"someone@example.com": "*"})
    assert result.returncode == 2
    assert "refusing to bake" in result.stderr
    assert not seen.exists()


def test_missing_preflight_runtime_stops_deploy(tmp_path):
    result, _, seen = _run_deploy(tmp_path, python=None)
    assert result.returncode == 2
    assert "cannot import stigmergy" in result.stderr
    assert not seen.exists()


@pytest.mark.parametrize("name", ["identities", "entity-registry", "slack-channels"])
def test_missing_control_file_stops_deploy(tmp_path, name):
    result, _, seen = _run_deploy(tmp_path)
    assert result.returncode == 0
    knowledge_file = tmp_path / "knowledge" / "ops" / f"{name}.json"
    knowledge_file.unlink()
    staging_sha = _commit(tmp_path / "knowledge", "remove deployed control")

    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "STIGMERGY_REPO": str(tmp_path / "knowledge"),
        "STAGING_SHA": staging_sha,
        "SEEN": str(seen),
        "STIGMERGY_PYTHON": sys.executable,
    }
    second = subprocess.run(
        ["bash", str(tmp_path / "scripts" / DEPLOY_SCRIPT.name)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert second.returncode == 2
    assert "required control file" in second.stderr
