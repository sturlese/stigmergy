import json
import pathlib
import re
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
FLY_TOML = ROOT / "fly.toml"
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"

SUPPORTED_SCRIPTS = {
    "stigmergy-index": "operations",
    "stigmergy-server": "service",
    "stigmergy-bridge": "local bridge",
    "stigmergy-issue-token": "bootstrap",
    "stigmergy-librarian": "service",
    "stigmergy-librarian-boot": "bootstrap",
    "stigmergy-librarian-credential": "bootstrap",
    "stigmergy-slack": "service",
    "stigmergy-admin-token": "bootstrap",
}

EXECUTABLE_MODULES = {
    "src/stigmergy/admin/cli.py": "bootstrap",
    "src/stigmergy/librarian/bootstrap.py": "bootstrap",
    "src/stigmergy/librarian/cli.py": "service",
    "src/stigmergy/librarian/gitcredential.py": "bootstrap",
    "src/stigmergy/knowledge/contract.py": "operations",
    "src/stigmergy/ops/reset.py": "operations",
    "src/stigmergy/server/issue_token.py": "bootstrap",
    "src/stigmergy/server/mcp_server.py": "service",
    "src/stigmergy/slack/app.py": "service",
}


def _fly_config() -> dict:
    with FLY_TOML.open("rb") as handle:
        return tomllib.load(handle)


def _dockerfile_cmd() -> list[str]:
    text = DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(r"^CMD\s+(\[.*?\])\s*$", text, re.MULTILINE | re.DOTALL)
    assert match
    return json.loads(match.group(1).replace("\\\n", "\n"))


def test_process_groups_are_exactly_app_worker_and_slack():
    assert set(_fly_config()["processes"]) == {"app", "worker", "slack"}


def test_http_service_exposes_only_app():
    assert _fly_config()["http_service"]["processes"] == ["app"]


def test_app_process_matches_dockerfile_command():
    assert _fly_config()["processes"]["app"] == " ".join(_dockerfile_cmd())


def test_worker_uses_boot_entry_point():
    assert _fly_config()["processes"]["worker"] == "stigmergy-librarian-boot"


def test_slack_receives_all_baked_control_files():
    command = _fly_config()["processes"]["slack"]
    assert command.startswith("stigmergy-slack ")
    assert "--identities /app/identities.json" in command
    assert "--entity-registry /app/entity-registry.json" in command
    assert "--channels /app/slack-channels.json" in command


def test_static_environment_contains_no_credentials():
    forbidden = {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "STIGMERGY_INDEX_DSN",
        "STIGMERGY_TOKEN_STORE",
        "STIGMERGY_EVIDENCE_ACCESS_KEY_ID",
        "STIGMERGY_EVIDENCE_SECRET_ACCESS_KEY",
    }
    assert not forbidden.intersection(_fly_config().get("env", {}))


def test_vm_blocks_cover_all_process_groups():
    groups = {group for vm in _fly_config()["vm"] for group in vm["processes"]}
    assert groups == {"app", "worker", "slack"}


def test_app_autostarts_and_sleeps_when_idle():
    service = _fly_config()["http_service"]
    assert service["auto_stop_machines"] is True
    assert service["auto_start_machines"] is True
    assert service["min_machines_running"] == 0


def test_package_metadata_is_copied_before_install():
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    named = [project["readme"], *project.get("license-files", [])]
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    before_install = dockerfile[: dockerfile.index("RUN uv sync --frozen")]
    assert all(name in before_install for name in named)
    assert "pyproject.toml" in before_install
    assert "uv.lock" in before_install


def test_deployment_image_installs_the_ocr_engine():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"(?:^|\s)tesseract-ocr=\S+", dockerfile, re.MULTILINE)


def test_deploy_script_pins_service_consumers_to_one_machine():
    script = (ROOT / "scripts" / "deploy_staging.sh").read_text(encoding="utf-8")
    assert "fly scale count slack=1 worker=1 --yes" in script


def test_installed_commands_are_the_classified_supported_surface():
    with (ROOT / "pyproject.toml").open("rb") as handle:
        scripts = tomllib.load(handle)["project"]["scripts"]

    assert set(scripts) == set(SUPPORTED_SCRIPTS)
    assert set(SUPPORTED_SCRIPTS.values()) == {
        "service",
        "bootstrap",
        "operations",
        "local bridge",
    }
    assert {"stigmergy-search", "stigmergy-queue", "stigmergy-gardener"}.isdisjoint(scripts)


def test_no_supported_user_capability_exists_only_as_a_remote_cli():
    local = {name for name, category in SUPPORTED_SCRIPTS.items() if category == "local bridge"}
    remote = set(SUPPORTED_SCRIPTS) - local

    assert local == {"stigmergy-bridge"}
    assert all(
        SUPPORTED_SCRIPTS[name] in {"service", "bootstrap", "operations"}
        for name in remote
    )


def test_all_directly_executable_runtime_modules_are_classified():
    discovered = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src" / "stigmergy").rglob("*.py")
        if 'if __name__ == "__main__"' in path.read_text(encoding="utf-8")
    }
    assert discovered == set(EXECUTABLE_MODULES)
    assert set(EXECUTABLE_MODULES.values()) <= {
        "service", "bootstrap", "operations", "local bridge"
    }


def test_retired_runtime_modules_are_absent():
    retired = {
        "src/stigmergy/capture/cli.py",
        "src/stigmergy/gardener/__init__.py",
        "src/stigmergy/repair/__init__.py",
        "src/stigmergy/librarian/processing.py",
        "src/stigmergy/librarian/page.py",
        "src/stigmergy/kernel/page.py",
        "src/stigmergy/server/review.py",
    }
    assert [path for path in sorted(retired) if (ROOT / path).exists()] == []


def test_fresh_runtime_schema_has_no_compatibility_alters():
    offenders = []
    for path in (ROOT / "src" / "stigmergy").rglob("*.py"):
        if "ALTER TABLE" in path.read_text(encoding="utf-8").upper():
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_local_service_images_are_digest_pinned():
    images = re.findall(
        r"^\s*image:\s*(\S+)\s*$",
        COMPOSE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert images
    assert all(re.fullmatch(r"[^\s@]+:[^\s@]+@sha256:[0-9a-f]{64}", image) for image in images)


def test_ci_uses_a_commit_pinned_checksum_verified_uv_installer():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "astral-sh/setup-uv@37802adc94f370d6bfd71619e3f0bf239e1f3b78" in workflow
    assert 'version: "0.11.16"' in workflow
    assert (
        'checksum: "74947fe2c03315cf07e82ab3acc703eddef01aba4d5232a98e4c6825ec116131"'
        in workflow
    )
    assert "pip install uv" not in workflow


def test_runtime_has_no_human_task_or_separate_repair_queue():
    forbidden = (
        "awaiting_review",
        "needs_human",
        "human_task",
        "gardener_tasks",
        "garden_tasks",
        "repair_queue",
        "repair_worker",
    )
    offenders = []
    for path in (ROOT / "src" / "stigmergy").rglob("*.py"):
        text = path.read_text(encoding="utf-8").casefold()
        if any(term in text for term in forbidden):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []
