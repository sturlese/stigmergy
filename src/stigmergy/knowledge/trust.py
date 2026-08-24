"""Git authorship gate for model-owned knowledge paths."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

WRITER_NAME = "Stigmergy Librarian"
WRITER_EMAIL = "librarian@stigmergy.local"
PROTECTED_PREFIXES = (
    "wiki/notes/",
    "wiki/concepts/",
    "wiki/entities/",
    "sources/",
)
PROTECTED_FILES = frozenset({"ops/entity-registry.json"})


@dataclass(frozen=True, order=True)
class AuthorshipViolation:
    commit: str
    path: str
    message: str


def check_range(repo: str, *, base: str, head: str) -> tuple[AuthorshipViolation, ...]:
    commits = _git(repo, "rev-list", "--reverse", f"{base}..{head}").splitlines()
    violations = []
    for commit in commits:
        paths = _git(
            repo,
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-r",
            commit,
        ).splitlines()
        protected = [path for path in paths if is_protected(path)]
        if not protected:
            continue
        identity = _git(repo, "show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce", commit)
        author_name, author_email, committer_name, committer_email = identity.split("\0")
        if (
            author_name == WRITER_NAME
            and author_email == WRITER_EMAIL
            and committer_name == WRITER_NAME
            and committer_email == WRITER_EMAIL
        ):
            continue
        for path in protected:
            violations.append(
                AuthorshipViolation(
                    commit=commit,
                    path=path,
                    message="model-owned knowledge requires the trusted writer identity",
                )
            )
    return tuple(sorted(violations))


def is_protected(path: str) -> bool:
    return path in PROTECTED_FILES or path.startswith(PROTECTED_PREFIXES)


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
