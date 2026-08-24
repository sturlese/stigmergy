"""Runtime configuration constructed at process entry points."""
import os
from dataclasses import dataclass

from stigmergy.index import store
from stigmergy.server import entity_aliases, identity


@dataclass(frozen=True)
class Settings:
    identity: str | None = None
    identities_path: str = ""
    entity_registry_path: str = ""
    knowledge_repo: str = ""
    knowledge_repo_url: str = ""
    knowledge_branch: str = "main"
    dsn: str | None = None
    embedder: str | None = None
    llm: str = "openai"
    model: str = "gpt-5.6-terra"
    reasoning_effort: str = "medium"

    @classmethod
    def from_args(cls, args) -> "Settings":
        """Build settings from parsed arguments and environment fallbacks."""
        repo = getattr(args, "repo", None)
        identities_path = args.identities or identity.default_path(repo)
        entity_registry_path = (getattr(args, "entity_registry", None)
                               or entity_aliases.default_path(repo))
        knowledge_repo = os.environ.get("STIGMERGY_KNOWLEDGE_REPO") or repo or ""

        return cls(
            identity=args.identity or os.environ.get("STIGMERGY_IDENTITY"),
            identities_path=identities_path,
            entity_registry_path=entity_registry_path,
            knowledge_repo=knowledge_repo,
            knowledge_repo_url=os.environ.get("STIGMERGY_LIBRARIAN_REPO_URL", ""),
            knowledge_branch=os.environ.get("STIGMERGY_LIBRARIAN_BRANCH", "main"),
            dsn=args.dsn or store.dsn(),
            embedder=args.embedder,
            llm=(getattr(args, "answer_llm", None) or os.environ.get("ANSWER_LLM", cls.llm)).lower(),
            model=os.environ.get("ANSWER_MODEL", cls.model),
            reasoning_effort=os.environ.get("ANSWER_REASONING_EFFORT", cls.reasoning_effort),
        )
