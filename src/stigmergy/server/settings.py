"""Runtime configuration constructed at process entry points."""
import os
from dataclasses import dataclass

from stigmergy.index import store
from stigmergy.kernel.llm import ANSWER_MODEL

# The workspace a single-tenant process (stdio server, command-line tools) acts for, by slug.
TENANT_ENV = "STIGMERGY_TENANT"


@dataclass(frozen=True)
class Settings:
    identity: str | None = None
    tenant: str | None = None
    knowledge_repo: str = ""
    knowledge_branch: str = "main"
    dsn: str | None = None
    embedder: str | None = None
    llm: str = "openrouter"
    model: str = ANSWER_MODEL

    @classmethod
    def from_args(cls, args) -> "Settings":
        """Build settings from parsed arguments and environment fallbacks."""
        repo = getattr(args, "repo", None)
        knowledge_repo = os.environ.get("STIGMERGY_KNOWLEDGE_REPO") or repo or ""

        return cls(
            identity=args.identity or os.environ.get("STIGMERGY_IDENTITY"),
            tenant=getattr(args, "tenant", None) or os.environ.get(TENANT_ENV) or None,
            knowledge_repo=knowledge_repo,
            knowledge_branch=os.environ.get("STIGMERGY_LIBRARIAN_BRANCH", "main"),
            dsn=args.dsn or store.dsn(),
            embedder=args.embedder,
            llm=(getattr(args, "answer_llm", None) or os.environ.get("ANSWER_LLM", cls.llm)).lower(),
            model=os.environ.get("ANSWER_MODEL", cls.model),
        )
