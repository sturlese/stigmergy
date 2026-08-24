from __future__ import annotations

import os
from dataclasses import dataclass

from stigmergy.capture import queue
from stigmergy.capture.extraction import CAPTURE_TIMEOUT_S
from stigmergy.kernel.llm import LIBRARIAN_MODEL, OCR_MODEL
from stigmergy.librarian.errors import LibrarianConfigError

DEFAULT_MODEL = LIBRARIAN_MODEL
DEFAULT_MAX_TURNS = 12
DEFAULT_TIMEOUT_S = 300
DEFAULT_POLL_INTERVAL_S = 3.0
GATE_BUDGET_S = 120
VISIBILITY_HEADROOM_S = 180

REPO_ENV = "STIGMERGY_REPO"
REPO_DEFAULT = "../stigmergy-brain"
REPO_URL_ENV = "STIGMERGY_LIBRARIAN_REPO_URL"
REQUIRE_REMOTE_BASE_ENV = "STIGMERGY_LIBRARIAN_REQUIRE_REMOTE_BASE"
TIMEOUT_ENV = "STIGMERGY_LIBRARIAN_TIMEOUT_S"
GARDEN_AT_ENV = "STIGMERGY_LIBRARIAN_GARDEN_AT"

_TRUTHY = {"1", "true", "yes"}


def operation_budget_s(*, timeout_s: int = DEFAULT_TIMEOUT_S) -> int:
    return CAPTURE_TIMEOUT_S + int(timeout_s) + GATE_BUDGET_S


def minimum_visibility_timeout_s(*, timeout_s: int = DEFAULT_TIMEOUT_S) -> int:
    return operation_budget_s(timeout_s=timeout_s) + VISIBILITY_HEADROOM_S


DEFAULT_VISIBILITY_TIMEOUT_S = minimum_visibility_timeout_s()


def resolved_timeout_s() -> int:
    raw = os.environ.get(TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_TIMEOUT_S
    try:
        value = int(raw)
    except ValueError as error:
        raise LibrarianConfigError(f"${TIMEOUT_ENV} must be a whole number of seconds") from error
    if value <= 0:
        raise LibrarianConfigError(f"${TIMEOUT_ENV} must be positive")
    return value


def resolved_visibility_timeout_s(*, timeout_s: int | None = None) -> int:
    budget = resolved_timeout_s() if timeout_s is None else int(timeout_s)
    return minimum_visibility_timeout_s(timeout_s=budget)


@dataclass(frozen=True)
class Settings:
    repo: str = REPO_DEFAULT
    branch: str = "main"
    dsn: str | None = None
    require_remote_base: bool = False
    backend: str = "pydantic"
    model: str = DEFAULT_MODEL
    max_turns: int = DEFAULT_MAX_TURNS
    timeout_s: int = DEFAULT_TIMEOUT_S
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    visibility_timeout_s: int = DEFAULT_VISIBILITY_TIMEOUT_S
    max_attempts: int = queue.DEFAULT_MAX_ATTEMPTS
    garden_at: str = "05:07"
    worktree_root: str = ""
    ocr_model: str = OCR_MODEL

    @classmethod
    def from_args(cls, args) -> Settings:
        def option(name: str, default):
            value = getattr(args, name, None)
            return default if value is None else value

        timeout_s = resolved_timeout_s()
        settings = cls(
            repo=option("repo", os.environ.get(REPO_ENV) or cls.repo),
            branch=option("branch", os.environ.get("STIGMERGY_LIBRARIAN_BRANCH", cls.branch)),
            dsn=option("dsn", None),
            require_remote_base=os.environ.get(REQUIRE_REMOTE_BASE_ENV, "").strip().lower() in _TRUTHY,
            backend=str(option("backend", os.environ.get("STIGMERGY_LIBRARIAN_BACKEND", cls.backend))).lower(),
            model=os.environ.get("STIGMERGY_LIBRARIAN_MODEL", cls.model).strip(),
            max_turns=int(os.environ.get("STIGMERGY_LIBRARIAN_MAX_TURNS", cls.max_turns)),
            timeout_s=timeout_s,
            poll_interval_s=float(option("poll_interval", cls.poll_interval_s)),
            visibility_timeout_s=int(
                option("visibility_timeout", resolved_visibility_timeout_s(timeout_s=timeout_s))
            ),
            max_attempts=int(option("max_attempts", cls.max_attempts)),
            garden_at=os.environ.get(GARDEN_AT_ENV, cls.garden_at),
            worktree_root=os.environ.get("STIGMERGY_LIBRARIAN_WORKTREE_ROOT", ""),
            ocr_model=os.environ.get("STIGMERGY_OCR_MODEL", cls.ocr_model).strip(),
        )
        settings.check_domains()
        return settings

    def check_domains(self) -> None:
        if self.backend not in {"scripted", "pydantic"}:
            raise LibrarianConfigError("backend must be scripted or pydantic")
        if self.backend == "pydantic" and self.model != DEFAULT_MODEL:
            raise LibrarianConfigError(
                f"the librarian model must be {DEFAULT_MODEL}"
            )
        if self.max_turns < 1 or self.timeout_s < 1:
            raise LibrarianConfigError("model limits must be positive")
        if self.poll_interval_s <= 0 or self.max_attempts < 1:
            raise LibrarianConfigError("worker loop limits must be positive")
        if self.ocr_model != OCR_MODEL:
            raise LibrarianConfigError(f"the OCR model must be {OCR_MODEL}")
        if self.visibility_timeout_s < minimum_visibility_timeout_s(timeout_s=self.timeout_s):
            raise LibrarianConfigError("visibility timeout must outlive one writer operation")
