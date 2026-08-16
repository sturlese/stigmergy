"""Runtime configuration — constructed once at the entry point, passed down explicitly.

Same ground rule as every other stigmergy package: modules never read the environment at import
time — `from_args` is the one place flags and env fallbacks are read.
"""
import os
from dataclasses import dataclass

from stigmergy.index import store
from stigmergy.server import entity_aliases, identity

# `librarian.config.REPO_URL_ENV`'s spelling for the same env var — the repo a server-driven
# entity mint clones from. Spelled here rather than imported: `server` may never import
# `stigmergy.librarian`, so the duplication is declared instead of discovered.
LIBRARIAN_REPO_URL_ENV = "STIGMERGY_LIBRARIAN_REPO_URL"


@dataclass(frozen=True)
class Settings:
    identity: str | None = None        # the resolved identity name (--identity / STIGMERGY_IDENTITY)
    identities_path: str = ""          # the versioned identities file
    # `ops/entity-registry.json`, read-only. An EXPLICIT `--entity-registry` path wins over the
    # `--repo` convention (the deployed server passes no `--repo` at all, so derivation alone
    # would leave this permanently empty in production). The PATH is resolved once at startup; the
    # file itself is read per call, so a deployment that must not track working-tree edits points
    # this at a baked artifact.
    entity_registry_path: str = ""
    # A local checkout of the knowledge repo, fetchable from `origin` — only `review_decide`'s
    # steward resolution needs it; everything else works with it empty.
    knowledge_repo: str = ""
    # The deploy-time SNAPSHOT of `ops/stewards.json`, for a process holding no checkout (the
    # deployed `app`/`slack` groups) — without it steward resolution there fails closed and the
    # doorbell rings for nobody. The repo read WINS wherever a checkout exists
    # (`review.load_stewards`), keeping per-decision freshness for the worker and local stdio.
    stewards_path: str = ""
    # `$STIGMERGY_LIBRARIAN_REPO_URL` — where an entity-proposal approve clones from to mint. A
    # settings field rather than a raw env read inside `review.py`, so a test can point a mint at
    # a local bare remote with no env monkeypatching. Empty = no server-driven mint here, refused
    # by name, never a silent no-op.
    librarian_repo_url: str = ""
    dsn: str | None = None             # Postgres DSN (None -> store.dsn())
    embedder: str | None = None        # 'openai' | 'fake' | None (None = match the index's model)
    # the answer model policy: the same server process that serves the read tools also serves
    # `ask`, so its synthesizer settings ride here. `fake` runs the whole path keyless.
    llm: str = "openai"                # 'openai' | 'fake' (ANSWER_LLM); unknown value fails fast
    model: str = "gpt-5.6-terra"       # ANSWER_MODEL (OPENAI_API_KEY required for 'openai')
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
        stewards_path = (getattr(args, "stewards", None)
                        or os.environ.get("STIGMERGY_STEWARDS_PATH") or "")
        librarian_repo_url = os.environ.get(LIBRARIAN_REPO_URL_ENV, "")

        return cls(
            identity=args.identity or os.environ.get("STIGMERGY_IDENTITY"),
            identities_path=identities_path,
            entity_registry_path=entity_registry_path,
            knowledge_repo=knowledge_repo,
            stewards_path=stewards_path,
            librarian_repo_url=librarian_repo_url,
            dsn=args.dsn or store.dsn(),
            embedder=args.embedder,
            llm=(getattr(args, "answer_llm", None) or os.environ.get("ANSWER_LLM", cls.llm)).lower(),
            model=os.environ.get("ANSWER_MODEL", cls.model),
            reasoning_effort=os.environ.get("ANSWER_REASONING_EFFORT", cls.reasoning_effort),
        )
