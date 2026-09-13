import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from evals.filing import parity

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


def _artifact_path(repo: pathlib.Path) -> pathlib.Path:
    return repo.parent / f"{repo.name}-parity-result.json"


def _sidecar_path(repo: pathlib.Path, name: str) -> pathlib.Path:
    return repo.parent / f"{repo.name}-{name}"


def _run_deploy(
    tmp_path: pathlib.Path,
    *,
    roster=ROSTER,
    python: str | None = sys.executable,
    parity_artifact: str = "valid",
    dirty_platform: bool = False,
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

    source_cases = ROOT / "evals" / "filing" / "cases"
    candidate_cases = tmp_path / "evals" / "filing" / "cases"
    candidate_cases.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "evals" / "filing" / "parity.py", candidate_cases.parent / "parity.py")
    shutil.copytree(source_cases, candidate_cases)
    candidate_skill = tmp_path / "src" / "stigmergy" / "knowledge"
    candidate_skill.mkdir(parents=True)
    shutil.copy2(
        ROOT / "src" / "stigmergy" / "knowledge" / "librarian_skill.md",
        candidate_skill / "librarian_skill.md",
    )

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    for name, value in EMPTY_DEFAULTS.items():
        (deploy / name).write_text(json.dumps(value) + "\n", encoding="utf-8")
    probe = deploy / "unmanaged" / "tracked.txt"
    probe.parent.mkdir(parents=True)
    probe.write_text("keep\n", encoding="utf-8")

    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    candidate_commit = _commit(tmp_path, "candidate parity inputs")

    knowledge = _sidecar_path(tmp_path, "knowledge")
    subprocess.run(["git", "init", "-q", "-b", "main", str(knowledge)], check=True)
    _git(knowledge, "config", "user.email", "test@example.com")
    _git(knowledge, "config", "user.name", "Test User")
    ops = knowledge / "ops"
    ops.mkdir(parents=True)
    (ops / "identities.json").write_text(json.dumps(roster), encoding="utf-8")
    (ops / "entity-registry.json").write_text(json.dumps(REGISTRY), encoding="utf-8")
    (ops / "slack-channels.json").write_text(json.dumps(CHANNELS), encoding="utf-8")
    staging_sha = _commit(knowledge, "test controls")

    seen = _sidecar_path(tmp_path, "seen")
    bin_dir = _sidecar_path(tmp_path, "bin")
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
    artifact = _artifact_path(tmp_path)
    if parity_artifact != "absent":
        payload = _parity_artifact(tmp_path, candidate_commit)
        if parity_artifact == "stale":
            payload["stigmergy_commit"] = "a" * 40
        elif parity_artifact == "mismatched":
            payload["librarian_skill_sha256"] = "b" * 64
        artifact.write_text(json.dumps(payload), encoding="utf-8")
        env["STIGMERGY_PARITY_ARTIFACT"] = str(artifact)
    if dirty_platform:
        with (candidate_skill / "librarian_skill.md").open("a", encoding="utf-8") as output:
            output.write("\nDirty after parity recording.\n")
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


def _raw_gates(*, passed=True):
    return {gate: "passed" if passed else "failed" for gate in parity.REQUIRED_SEMANTIC_GATES}


def _parity_artifact(repo: pathlib.Path, commit: str) -> dict:
    expected = parity.current_release_inputs(repo)
    assert expected.commit == commit
    corpus = "0123456789abcdef" * 4
    initial = "0123456789abcdef0123456789abcdef01234567"
    provenance = {
        "corpus_sha256": corpus,
        "initial_graph_ref": initial,
        "source_cases": expected.source_cases,
        "commit": expected.commit,
        "librarian_skill_sha256": expected.librarian_skill_sha256,
    }

    def matrix(level: str, passed: bool) -> dict:
        gates = _raw_gates(passed=passed)
        if not passed:
            gates["bodies"] = "failed"
        return {
            "reasoning_level": level,
            "runtime": {
                "model": "openai/gpt-oss-120b",
                "reasoning_level": level,
                "provider": "cerebras",
            },
            "score": {"passed": passed},
            "gates": {"passed": passed},
            "raw_gates": gates,
            "provenance": provenance,
            "passed": passed,
        }

    return {
        "corpus_sha256": corpus,
        "initial_graph_ref": initial,
        "stigmergy_commit": expected.commit,
        "librarian_skill_sha256": expected.librarian_skill_sha256,
        "source_cases": [
            {"id": key, "sha256": value} for key, value in expected.source_cases.items()
        ],
        "runs": [
            {
                "implementation": "hippocampus",
                "run_id": "hippocampus-recorded-run",
                "runtime": {"model": "fixture", "reasoning_level": "high", "provider": "fixture"},
                "score": {"passed": True},
                "gates": {"passed": True},
                "raw_gates": _raw_gates(),
                "provenance": {
                    "corpus_sha256": corpus,
                    "initial_graph_ref": initial,
                    "source_cases": expected.source_cases,
                },
            },
            {
                "implementation": "stigmergy",
                "run_id": "stigmergy-recorded-run",
                "runtime": {
                    "model": "openai/gpt-oss-120b",
                    "reasoning_level": "high",
                    "provider": "cerebras",
                },
                "score": {"passed": True},
                "gates": {"passed": True},
                "raw_gates": _raw_gates(),
                "provenance": provenance,
            },
        ],
        "reasoning_matrix": [
            matrix("minimal", False),
            matrix("low", False),
            matrix("medium", False),
            matrix("high", True),
        ],
        "blind_editorial_review": {
            "verdict": "no_material_stigmergy_regression",
            "provenance": {
                "reviewer": "recorded-reviewer",
                "method": "blind-pairwise",
                "artifact_ref": "recorded-review",
                "corpus_sha256": corpus,
                "initial_graph_ref": initial,
                "source_cases": expected.source_cases,
                "runs": {
                    "hippocampus": "hippocampus-recorded-run",
                    "stigmergy": "stigmergy-recorded-run",
                },
            },
        },
    }


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


def test_dirty_platform_checkout_stops_deploy_before_parity_or_fly(tmp_path):
    result, _deploy, seen = _run_deploy(
        tmp_path, parity_artifact="stale", dirty_platform=True
    )

    assert result.returncode == 2
    assert "platform checkout has tracked or untracked changes" in result.stderr
    assert "recorded parity gate rejected" not in result.stderr
    assert not seen.exists()


@pytest.mark.parametrize("artifact_state", ["absent", "stale", "mismatched"])
def test_release_deploy_refuses_missing_or_candidate_mismatched_parity_evidence(tmp_path, artifact_state):
    result, _deploy, seen = _run_deploy(tmp_path, parity_artifact=artifact_state)

    assert result.returncode == 2
    assert "parity" in result.stderr
    assert not seen.exists()


@pytest.mark.parametrize("name", ["identities", "entity-registry", "slack-channels"])
def test_missing_control_file_stops_deploy(tmp_path, name):
    result, _, seen = _run_deploy(tmp_path)
    assert result.returncode == 0
    knowledge = _sidecar_path(tmp_path, "knowledge")
    knowledge_file = knowledge / "ops" / f"{name}.json"
    knowledge_file.unlink()
    staging_sha = _commit(knowledge, "remove deployed control")

    env = {
        **os.environ,
        "PATH": f"{_sidecar_path(tmp_path, 'bin')}{os.pathsep}{os.environ['PATH']}",
        "STIGMERGY_REPO": str(knowledge),
            "STAGING_SHA": staging_sha,
            "SEEN": str(seen),
            "STIGMERGY_PYTHON": sys.executable,
            "STIGMERGY_PARITY_ARTIFACT": str(_artifact_path(tmp_path)),
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
