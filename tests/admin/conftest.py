"""Fixtures for the admin-console suite.

Real Postgres through `tests.testdb` (never a faked queue — the house rule), with exactly the two
NETWORK edges faked: the GitHub gateway (a recording fake with the real gateway's four methods)
and, where a digest posts, the digest suite's own fake-gateway posture (here: `gateway=None` plus
env, since only dry-run runs in this suite). Since ADR 030, `entity_approve` reaches real git too
(`entities.remote.mint_via_clone`) — `build_bare_knowledge_repo`/`require_gitleaks` below are this
package's OWN, minimal fixture for that, never a faked commit (this repo's own testing doctrine).

The composed-branch fixtures drive the REAL `routes.compose` product over `httpx.ASGITransport`
— real middleware order, real 404/401/421 paths, no uvicorn needed (nothing here exercises the
MCP session manager's lifespan).
"""
import os
import subprocess

import pytest

from stigmergy.admin.schema import ensure_admin_schema
from stigmergy.admin.settings import AdminSettings
from stigmergy.capture import queue
from stigmergy.capture import schema as capture_schema
from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.gardener.schema import ensure_gardener_schema
from stigmergy.index import store as index_store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server import review
from stigmergy.server.audit import ensure_audit_table
from stigmergy.server.identity import hash_token
from stigmergy.server.settings import Settings
from tests import testdb

ADMIN_TOKEN = "test-admin-console-token-for-the-suite-only"
ADMIN_HASH = hash_token(ADMIN_TOKEN)


@pytest.fixture(scope="module")
def conn():
    connection = testdb.connect_or_skip("admin")
    capture_schema.ensure_capture_schema(connection)
    ensure_audit_table(connection)
    review.ensure_review_schema(connection)
    ensure_gardener_schema(connection)
    ensure_admin_schema(connection)
    # A real (empty) pages_index + index_meta so the zone counts and the substrate check run
    # against the actual store DDL rather than a hand-rolled table. The FAKE embedder's own
    # model/dim, not made-up values: `index_meta` outlives this module, and a later suite that
    # opens a service with `embedder=None` ("match the index's model") must find a model that
    # `build_embedder` can actually construct — a fabricated name broke exactly those suites.
    fake = build_embedder("fake", None)
    index_store.init_schema(connection, dim=fake.dim, model=fake.model, fts_config="english")
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def clean_tables(conn):
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE capture_queue, job_runs, ingest_errors, audit_log, admin_actions,"
            " gardener_findings, review_decisions RESTART IDENTITY")
    yield


@pytest.fixture()
def admin_settings():
    return AdminSettings(token_hash=ADMIN_HASH, actor="suite-default-actor")


@pytest.fixture()
def server_settings():
    return Settings()


class FakeGateway:
    """The real gateway's four methods, recording instead of calling GitHub."""

    def __init__(self):
        self.calls = []
        self.fail_with = None   # set to an exception to make every method raise it

    def _record(self, *call):
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append(call)

    def workflows(self):
        self._record("workflows")
        return [
            {"id": 1, "name": "index-rebuild", "path": ".github/workflows/index-rebuild.yml",
             "state": "active"},
            {"id": 2, "name": "retention-purge", "path": ".github/workflows/retention-purge.yml",
             "state": "active"},
            {"id": 3, "name": "gardener", "path": ".github/workflows/gardener.yml",
             "state": "disabled_manually"},
        ]

    def runs(self, workflow_file, *, limit=10):
        self._record("runs", workflow_file, limit)
        return [{"id": 77, "status": "completed", "conclusion": "success", "event": "schedule",
                 "created_at": "2026-08-03T04:17:11Z", "updated_at": "2026-08-03T04:19:02Z",
                 "html_url": "https://github.com/example/actions/runs/77",
                 "display_title": "nightly"}]

    def dispatch(self, workflow_file, *, ref="main", inputs=None):
        self._record("dispatch", workflow_file, ref, inputs)

    def set_enabled(self, workflow_file, *, enabled):
        self._record("set_enabled", workflow_file, enabled)


@pytest.fixture()
def fake_gateway():
    return FakeGateway()


def submit_one(conn, *, material=None, submitted_by="steward@example.com", kind="raw", hints=None):
    material = material or f"suite material {os.urandom(8).hex()}"
    return queue.submit(conn, MemoryEvidenceStore(), kind=kind, material=material, hints=hints,
                        submitted_by=submitted_by)


def park(conn, submission_id, *, status=capture_schema.TRIAGE, report=None, error=""):
    """Claim the (only queued) row and park it — the same `finish` transition the librarian
    uses, so the console's dispositions run against rows shaped exactly like production's."""
    item = queue.claim_next(conn)
    assert item is not None and item["id"] == submission_id, "fixture expects one queued row"
    queue.finish(conn, submission_id, status=status, expected_attempts=item["attempts"],
                 report=report, error=error)
    return item


def unresolved_entity_report(name):
    """The LEGACY park shape: one unresolved name under the retired singular `SITUATION_NAME_KEY`.

    **Deliberately kept after the plural collapse, and this is the reason.** Nothing writes this
    key any more — a park writes `SITUATION_NAMES_KEY`, a list, whatever the count — but rows
    carrying it are never migrated, so the console has to keep rendering them forever. Every caller
    below is therefore also this repo's coverage that the console reads a pre-collapse row
    correctly, which no fixture built from today's builder could give it.

    Use `unresolved_entity_names_report` for the shape a park writes today (both counts). That the
    two are indistinguishable to every reader downstream is pinned once, at the ONE place the
    fallback lives: `tests/entities/test_situations.py`."""
    return {capture_schema.SITUATION_KEY: capture_schema.SITUATION_UNRESOLVED_ENTITY,
            capture_schema.SITUATION_NAME_KEY: name}


def unresolved_entity_names_report(*names):
    """The park shape a librarian writes TODAY: `SITUATION_NAMES_KEY` and nothing else — the exact
    row `report.triage_entity` produces for any number of unresolved names, one or many, which
    never writes the singular key beside it, so neither does this. Deliberately a second function
    rather than an optional argument on the one above: the two report shapes are different data —
    one current, one legacy — and a caller has to choose which one it is exercising."""
    return {capture_schema.SITUATION_KEY: capture_schema.SITUATION_UNRESOLVED_ENTITY,
            capture_schema.SITUATION_NAMES_KEY: list(names)}


# ── entity_approve's own git fixture (ADR 030) ───────────────────────────────────────────────
# A LOCAL, minimal bare-repo builder rather than importing `tests.entities.conftest.build_repo` or
# `tests.librarian.support.build_repo`: each test package that needs real git builds its own
# (`tests/entities/conftest.py` and `tests/librarian/support.py` already do this independently of
# each other), so this one stays admin's own and never becomes a second caller of either.
_ENTITY_TEMPLATE = """---
type: entity
title: "<Entity Name>"
status: developing        # seed|developing|mature|canonical (canonical requires `owner`)
entity_type: organization # person|organization|product|tool|repository|place
role: ""
aliases: []
created: <YYYY-MM-DD>
updated: <YYYY-MM-DD>
tags: [entity]
related: []
sources: []
---

# <Entity Name>

## What / Who

<One clear paragraph: what this entity is and why it's in the brain.>
"""

_SEED_STEWARD_ENV = {"GIT_AUTHOR_NAME": "Test Steward", "GIT_AUTHOR_EMAIL": "steward@example.com",
                     "GIT_COMMITTER_NAME": "Test Steward",
                     "GIT_COMMITTER_EMAIL": "steward@example.com"}


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
                   env={**os.environ, **_SEED_STEWARD_ENV})


def build_bare_knowledge_repo(root: str) -> str:
    """A fresh `git init --bare` remote, seeded with exactly what `entities.mint.mint` needs to
    mint into an otherwise-empty knowledge repo: the entity template and an EMPTY registry —
    drift-free by construction (no pages, no registry entries — the two already agree), so no
    test here needs a regenerate-first step the way `tests/server/test_review.py`'s
    `drift_free_env` does for the large, shared librarian fixture repo. `entity_approve` clones
    this path directly (`mint_via_clone`'s `credential` is unused for a non-`https://` remote), so
    no App credential and no network are ever involved.
    """
    bare = os.path.join(root, "knowledge.git")
    seed = os.path.join(root, "seed")
    os.makedirs(os.path.join(seed, "ops", "templates"), exist_ok=True)
    _git("init", "--bare", "--quiet", "--initial-branch=main", bare, cwd=root)
    _git("init", "--quiet", "--initial-branch=main", seed, cwd=root)
    with open(os.path.join(seed, "ops", "templates", "entity.md"), "w", encoding="utf-8") as f:
        f.write(_ENTITY_TEMPLATE)
    with open(os.path.join(seed, "ops", "entity-registry.json"), "w", encoding="utf-8") as f:
        f.write('{\n  "entities": {}\n}\n')
    _git("add", "--all", cwd=seed)
    _git("commit", "--quiet", "-m", "chore: seed the fixture knowledge repo", cwd=seed)
    _git("remote", "add", "origin", bare, cwd=seed)
    _git("push", "--quiet", "-u", "origin", "main", cwd=seed)
    return bare


@pytest.fixture()
def entity_mint_repo(tmp_path) -> str:
    """The bare remote path — see `build_bare_knowledge_repo`. Fresh per test."""
    return build_bare_knowledge_repo(str(tmp_path / "git"))


@pytest.fixture()
def require_gitleaks():
    """Skip on a laptop with no gitleaks on PATH; FAIL in CI (`$STIGMERGY_TEST_DSN` set) rather than
    let a secrets gate silently never run — the same posture `tests/entities/conftest.py` and
    `tests/server/conftest.py` each hold for their own `entity_approve`-adjacent git suites.
    `mint.mint` always scans (`_refuse_secrets`), so every test that mints for real needs this."""
    from tests.librarian import support
    if support.gitleaks_available():
        return
    if testdb.required():
        pytest.fail("$STIGMERGY_TEST_DSN is set (CI mode) but gitleaks is not on PATH — refusing to "
                   "skip the admin console's entity-mint suite silently. Install gitleaks BEFORE "
                   "the test step (see .github/workflows/ci.yml).")
    pytest.skip("gitleaks not on PATH (brew install gitleaks) — entity_approve's secrets gate "
               "cannot be exercised without it")


@pytest.fixture(autouse=True)
def no_real_github_app(monkeypatch):
    """`entity_approve` walks the same `entities.remote.mint_via_clone` door a `librarian_repo_url`
    pointed at a real `https://` remote would authenticate through — same guard
    `tests/server/conftest.py` applies to its own package: no test here may mint a real GitHub
    installation token out of an operator's `.env`. Harmless for every OTHER admin suite in this
    package, which never sets `librarian_repo_url` at all."""
    from stigmergy.librarian import githubapp
    for name in (githubapp.APP_ID_ENV, githubapp.INSTALLATION_ID_ENV,
                githubapp.PRIVATE_KEY_ENV, githubapp.PRIVATE_KEY_FILE_ENV):
        monkeypatch.delenv(name, raising=False)
