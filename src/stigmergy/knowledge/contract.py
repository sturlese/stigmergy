"""Knowledge-repository release contract validation."""

from __future__ import annotations

import argparse
import re
from importlib.resources import files
from pathlib import Path


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


def expected_librarian_skill() -> bytes:
    return files("stigmergy.knowledge").joinpath("librarian_skill.md").read_bytes()


def validate_librarian_skill(repository: str | Path) -> None:
    path = Path(repository) / ".claude" / "skills" / "librarian" / "SKILL.md"
    try:
        actual = path.read_bytes()
    except OSError as error:
        raise KnowledgeContractError("librarian skill is missing or unreadable") from error
    if actual != expected_librarian_skill():
        raise KnowledgeContractError("librarian skill does not match the platform contract")


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
        ".platform/.venv/bin/stigmergy-index --rebuild --repo .",
    )
    if any(value not in rebuild for value in required):
        raise KnowledgeContractError("index rebuild workflow does not satisfy its contract")
    forbidden = ("continue-on-error:", "|| true", "if: false")
    if any(value in rebuild for value in forbidden):
        raise KnowledgeContractError("index rebuild workflow can suppress failure")


def validate_repository(repository: str | Path) -> None:
    validate_librarian_skill(repository)
    validate_workflows(repository)


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
