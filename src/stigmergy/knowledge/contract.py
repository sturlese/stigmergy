"""Knowledge-repository release contract validation."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
from importlib.resources import files
from pathlib import Path

from stigmergy.knowledge.sources import REQUIRED_ARTIFACT_FIELDS, REQUIRED_SOURCE_FIELDS


class KnowledgeContractError(ValueError):
    pass


PLATFORM_CHECKOUT = re.compile(
    r"repository:\s*sturlese/stigmergy\s*\n\s*ref:\s*([0-9a-f]{40})"
)
UV_ACTION = "astral-sh/setup-uv@37802adc94f370d6bfd71619e3f0bf239e1f3b78"
UV_VERSION = 'version: "0.11.16"'
UV_CHECKSUM = (
    'checksum: "74947fe2c03315cf07e82ab3acc703eddef01aba4d5232a98e4c6825ec116131"'
)
LIBRARIAN_SKILL_PATH = Path(".claude/skills/librarian/SKILL.md")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}")


def expected_librarian_skill() -> bytes:
    return files("stigmergy.knowledge").joinpath("librarian_skill.md").read_bytes()


def validate_librarian_skill(repository: str | Path) -> None:
    path = Path(repository) / LIBRARIAN_SKILL_PATH
    try:
        actual = path.read_bytes()
    except OSError as error:
        raise KnowledgeContractError("librarian skill is missing or unreadable") from error
    if actual != expected_librarian_skill():
        raise KnowledgeContractError("librarian skill does not match the platform contract")


def validate_librarian_skill_at_ref(repository: str | Path, ref: str) -> None:
    """Validate the skill bytes in one selected Git snapshot.

    The ref provides commit provenance only. A later ordinary data commit is acceptable when its
    skill bytes still equal the packaged contract; no deployment-time commit pin is required.
    """
    try:
        actual = subprocess.check_output(
            ["git", "-C", str(repository), "show", f"{ref}:{LIBRARIAN_SKILL_PATH.as_posix()}"],
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise KnowledgeContractError("librarian skill is missing or unreadable at the selected base") from error
    if actual != expected_librarian_skill():
        raise KnowledgeContractError("librarian skill does not match the platform contract")


def librarian_skill_provenance(
    repository: str | Path, *, expected_commit: str | None = None
) -> dict[str, str]:
    """Return the exact packaged prompt bytes and checked-out brain commit."""
    root = Path(repository)
    validate_librarian_skill(root)
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        raise KnowledgeContractError("librarian skill repository commit is unavailable") from error
    if _GIT_COMMIT.fullmatch(commit) is None:
        raise KnowledgeContractError("librarian skill repository commit is invalid")
    if expected_commit is not None and commit != expected_commit:
        raise KnowledgeContractError("librarian skill repository commit does not match the verified checkout")
    return {
        "commit": commit,
        "sha256": hashlib.sha256((root / LIBRARIAN_SKILL_PATH).read_bytes()).hexdigest(),
    }


def validate_workflows(repository: str | Path) -> None:
    root = Path(repository) / ".github" / "workflows"
    paths = (root / "lint.yml", root / "index-rebuild.yml")
    texts = []
    refs = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise KnowledgeContractError(f"required workflow is missing: {path.name}") from error
        match = PLATFORM_CHECKOUT.search(text)
        if match is None:
            raise KnowledgeContractError(f"{path.name} must pin one platform commit")
        if any(value not in text for value in (UV_ACTION, UV_VERSION, UV_CHECKSUM)):
            raise KnowledgeContractError(f"{path.name} must use the verified uv installer")
        if "pip install uv" in text:
            raise KnowledgeContractError(f"{path.name} contains an unverified uv bootstrap")
        texts.append(text)
        refs.append(match.group(1))
    if len(set(refs)) != 1:
        raise KnowledgeContractError("platform workflow pins must match")

    rebuild = texts[1]
    required = (
        'cron: "17 4 * * *"',
        "workflow_dispatch:",
        "OPENROUTER_API_KEY:",
        ".platform/.venv/bin/stigmergy-index --rebuild --repo .",
    )
    if any(value not in rebuild for value in required):
        raise KnowledgeContractError("index rebuild workflow does not satisfy its contract")
    suppressed = (
        "continue-on-error:",
        "|| true",
        "if: false",
    )
    if any(value in rebuild for value in suppressed):
        raise KnowledgeContractError("index rebuild workflow can suppress failure")
    unsupported = (
        "EMBED_API_KEY:",
        "EMBED_BASE_URL:",
        "EMBED_MODEL:",
        "EMBED_DIMENSIONS:",
        "ANTHROPIC_API_KEY:",
        "OPENAI_API_KEY:",
        "GEMINI_API_KEY:",
    )
    if any(value in rebuild for value in unsupported):
        raise KnowledgeContractError("index rebuild workflow uses unsupported model configuration")


def validate_source_template(repository: str | Path) -> None:
    path = Path(repository) / "ops" / "templates" / "source.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise KnowledgeContractError("source template is missing or unreadable") from error
    missing_source = [
        field
        for field in sorted(REQUIRED_SOURCE_FIELDS)
        if re.search(rf"(?m)^{re.escape(field)}\s*:", text) is None
    ]
    missing_artifact = [
        field
        for field in sorted(REQUIRED_ARTIFACT_FIELDS)
        if re.search(rf"(?m)^\s+(?:-\s+)?{re.escape(field)}\s*:", text) is None
    ]
    if missing_source or missing_artifact:
        raise KnowledgeContractError("source template does not satisfy the source schema")


def validate_repository(repository: str | Path) -> None:
    validate_librarian_skill(repository)
    validate_workflows(repository)
    validate_source_template(repository)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stigmergy.knowledge.contract")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    try:
        validate_repository(args.repo)
    except KnowledgeContractError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
