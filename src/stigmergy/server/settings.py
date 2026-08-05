"""Runtime configuration — constructed once at the entry point, passed down explicitly.

Same ground rule as every other stigmergy package: modules never read the environment at import
time — `from_args` is the one place flags and env fallbacks are read.
"""
import os
from dataclasses import dataclass

from stigmergy.index import store
from stigmergy.server import entity_aliases, identity

# `librarian.config.REPO_URL_ENV`'s own spelling, for the same env var — the knowledge repo a
# server-driven entity mint clones from (`review.review_decide`'s entity-proposal approve, ADR 030
# D3). Spelled here rather than imported: `server` may not import `stigmergy.librarian` at all
# (`tests/test_architecture.py::test_server_never_imports_the_librarian` has no exception for this
# module), so the duplication is declared instead of discovered — the same posture
# `entities.mint.GITLEAKS_BIN_ENV` already carries for its own analogous constant.
LIBRARIAN_REPO_URL_ENV = "STIGMERGY_LIBRARIAN_REPO_URL"


@dataclass(frozen=True)
class Settings:
    identity: str | None = None        # the resolved identity name (--identity / STIGMERGY_IDENTITY)
    identities_path: str = ""          # the versioned identities file
    # `ops/entity-registry.json`, read-only. An EXPLICIT `--entity-registry` path wins over the
    # `--repo` convention, the same precedence `identities_path` already has — the deployed server
    # passes no `--repo` at all (`fly.toml`'s `[processes].app` command names `--identities`
    # directly), so `--repo`-derivation alone would leave this permanently empty in production.
    # Baked at server startup either way: base-commit semantics, never a live re-read of a working
    # tree.
    entity_registry_path: str = ""
    # A local checkout of the knowledge repo, fetchable from `origin` — `review_decide`'s steward
    # resolution (`ops/stewards.json`, read at `origin/main`) is the one thing that needs it. Same
    # `--repo` convention as `identities_path` above rather than a new flag; every read tool and
    # the fast-lane write tools work with it empty, exactly like a keyless embedder.
    knowledge_repo: str = ""
    # The deploy-time SNAPSHOT of `ops/stewards.json`, for a process that holds no checkout at
    # all. The `app` and `slack` groups are exactly that: `fly.toml` starts them with baked
    # `--identities`/`--entity-registry` and no `--repo`, so `load_stewards`' read at
    # `origin/main` had nothing to read — the doorbell returned 0 in silence and EVERY
    # entity-proposal decision failed closed on a server whose steward was correctly configured.
    # Same mechanism as the three files the deploy already bakes, and the same trade
    # `identities.json` accepted first: a redeploy to change it, in exchange for existing at all.
    # The repo read still WINS wherever a checkout exists (see `review.load_stewards`), so the
    # worker and a local stdio server keep ADR 016's per-decision freshness.
    stewards_path: str = ""
    # `$STIGMERGY_LIBRARIAN_REPO_URL` — where `review.review_decide`'s entity-proposal approve clones
    # FROM to mint (ADR 030 D3), the SAME repo the librarian worker itself clones. A settings field
    # rather than a raw env read inside `review.py`, deliberately: `Settings(...)` is how a test
    # points a mint at a real local bare remote instead of GitHub, with no env var to monkeypatch
    # and no leakage between tests that construct their own `Settings`. Empty means "no server-
    # driven mint is possible here" — refused by name (`entities.errors.CapabilityUnavailableError`,
    # mapped to this package's own), never a silent no-op, exactly like a keyless embedder.
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
        knowledge_repo = (getattr(args, "knowledge_repo", None)
                         or os.environ.get("STIGMERGY_KNOWLEDGE_REPO") or repo or "")
        stewards_path = (getattr(args, "stewards", None)
                        or os.environ.get("STIGMERGY_STEWARDS_PATH") or "")
        librarian_repo_url = (getattr(args, "librarian_repo_url", None)
                             or os.environ.get(LIBRARIAN_REPO_URL_ENV, ""))

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
