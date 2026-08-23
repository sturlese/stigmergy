"""Runtime configuration — constructed once at the entry point, passed down explicitly.

Same ground rule as every other stigmergy package: modules never read the environment at import
time — `from_args` is the one place flags and env fallbacks are read.
"""
import os
from dataclasses import dataclass

from stigmergy.index import store
from stigmergy.server import entity_aliases, identity


@dataclass(frozen=True)
class Settings:
    identity: str | None = None        # the resolved identity name (--identity / STIGMERGY_IDENTITY)
    identities_path: str = ""          # the versioned identities file
    # `ops/entity-registry.json`, read-only, and the FALLBACK source rather than the source: the
    # service answers from the index's registry snapshot wherever the database has one, and reads
    # this file only where it has none (a local `--repo` run, an index built before that table
    # existed). An EXPLICIT `--entity-registry` path still wins over the `--repo` convention — the
    # deployed server passes no `--repo` at all, so derivation alone would leave this empty in
    # production, which is exactly where the fallback has to work.
    entity_registry_path: str = ""
    # A local checkout of the knowledge repo, fetchable from `origin`. Nothing on the serving
    # path needs it since the review lane was retired; it stays because a local stdio server
    # started with `--repo` derives the ops-file paths below from it.
    knowledge_repo: str = ""
    dsn: str | None = None             # Postgres DSN (None -> store.dsn())
    embedder: str | None = None        # 'openai' | 'fake' | None (None = match the index's model)
    # the answer model policy: the same server process that serves the read tools also serves
    # `ask`, so its synthesizer settings ride here. `fake` runs the whole path keyless.
    llm: str = "openai"                # 'openai' | 'fake' (ANSWER_LLM); unknown value fails fast
    # ANSWER_MODEL, two forms: a bare name is the OpenAI Responses API (OPENAI_API_KEY); a
    # provider-prefixed pydantic-ai id ("openrouter:…") authenticates with that provider's own key.
    model: str = "gpt-5.6-terra"
    reasoning_effort: str = "medium"   # ANSWER_REASONING_EFFORT

    @classmethod
    def from_args(cls, args) -> "Settings":
        """Build settings from parsed CLI args, applying env fallbacks and the `--repo`
        conventions (identities at <repo>/ops/identities.json)."""
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
            dsn=args.dsn or store.dsn(),
            embedder=args.embedder,
            llm=(getattr(args, "answer_llm", None) or os.environ.get("ANSWER_LLM", cls.llm)).lower(),
            model=os.environ.get("ANSWER_MODEL", cls.model),
            reasoning_effort=os.environ.get("ANSWER_REASONING_EFFORT", cls.reasoning_effort),
        )
