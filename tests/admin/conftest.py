"""Fixtures for the admin-console suite.

Real Postgres through `tests.testdb` (never a faked queue — the house rule), with exactly the two
NETWORK edges faked: the GitHub gateway (a recording fake with the real gateway's four methods)
and, where a digest posts, the digest suite's own fake-gateway posture (here: `gateway=None` plus
env, since only dry-run runs in this suite). Since ADR 030, `entity_approve` reaches real git too
(`entities.remote.decide_via_clone`) — `build_bare_knowledge_repo`/`require_gitleaks` below are this
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
from stigmergy.repair import schema as repair_schema
from stigmergy.repair import store as repair_store
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
    repair_schema.ensure_repair_schema(connection)
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
            " gardener_findings, review_decisions, repair_proposals RESTART IDENTITY")
    # The registry snapshot is what the inbox and the Entities desk read: a test that published
    # one must not hand its proposals to the next.
    index_store.clear_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH)
    yield
    index_store.clear_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH)


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
            {"id": 4, "name": "repair-propose",
             "path": ".github/workflows/repair-propose.yml", "state": "active"},
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


def finish_one(conn, submission_id, *, status=capture_schema.FAILED, report=None, error=""):
    """Claim the (only queued) row and finish it — the same `finish` transition the librarian
    uses, so the console reads rows shaped exactly like production's."""
    item = queue.claim_next(conn)
    assert item is not None and item["id"] == submission_id, "fixture expects one queued row"
    queue.finish(conn, submission_id, status=status, expected_attempts=item["attempts"],
                 report=report, error=error)
    return item


# ── the governed door's own git fixture ───────────────────────────────────────────────
# A LOCAL, minimal bare-repo builder rather than importing `tests.entities.conftest.build_repo` or
# `tests.librarian.support.build_repo`: each test package that needs real git builds its own
# (`tests/entities/conftest.py` and `tests/librarian/support.py` already do this independently of
# each other), so this one stays admin's own and never becomes a second caller of either.
_ENTITY_TEMPLATE = """---
type: entity
title: "<Entity Name>"
status: developing        # seed|developing|mature|canonical (canonical requires `owner`)
entity_type: organization # person|organization|product|tool|repository|place|project
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
    """A fresh `git init --bare` remote, seeded with exactly what a birth or a decision needs in
    an otherwise-empty knowledge repo: the entity template and an EMPTY registry — drift-free by
    construction. The console clones this path directly (the door's `credential` is unused for a
    non-`https://` remote), so no App credential and no network are ever involved.
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


def propose_repair(conn, *, path="wiki/notes/Renewals.md", link="Existing Note", kind="backlink",
                   note="", rationale="neither page links the other") -> int:
    """One PENDING `repair_proposals` row, through the package's own writers — `target_paths` and
    `content_key` are DERIVED exactly as `repair.proposer` derives them, so no fixture here can
    seed a row whose two stored facts disagree (the disagreement `remote._cross_check` exists to
    catch is worth reaching by tampering, never by a careless fixture)."""
    ops = [{"op": kind, "path": path, "link": link, "note": note}]
    return repair_store.insert_proposal(
        conn, run_id=0, finding_ids=[1], target_paths=repair_schema.target_paths(ops), ops=ops,
        rationale=rationale, content_key=repair_schema.content_key(ops), model_id="fake")


def propose_entity_body(conn, *, path="wiki/entities/Meridian Partners.md",
                        body="## What / Who\n\nA freight broker.\n", role="",
                        rationale="the page is still its template") -> int:
    """One PENDING `entity-body` row — the second kind, whose op carries PROSE rather than a link.
    Derived through the same writers for the same reason as its sibling above."""
    ops = [{"op": repair_schema.KIND_ENTITY_BODY, "path": path, "body_markdown": body,
            "role": role}]
    return repair_store.insert_proposal(
        conn, run_id=0, finding_ids=[1], target_paths=repair_schema.target_paths(ops), ops=ops,
        rationale=rationale, kind=repair_schema.KIND_ENTITY_BODY,
        content_key=repair_schema.content_key(ops, kind=repair_schema.KIND_ENTITY_BODY),
        model_id="fake")


def propose_delete(conn, *, doomed="wiki/notes/Old Memo.md",
                   scrubbed="wiki/decisions/Refunds.md",
                   rationale="the memo was superseded") -> int:
    """One PENDING `delete` row — the third kind, whose ops carry whole PAGES. Derived through the
    same writers for the same reason as its two siblings above."""
    ops = [{"op": repair_schema.DELETE_OP_NAME, "path": doomed},
           {"op": repair_schema.SCRUB_OP_NAME, "path": scrubbed, "expected_before_hash": "0" * 64,
            "planned_after": "---\ntype: decision\n---\n\n# Refunds\n\nNo link any more.\n"}]
    return repair_store.insert_proposal(
        conn, run_id=0, finding_ids=[], target_paths=repair_schema.target_paths(ops), ops=ops,
        rationale=rationale, kind=repair_schema.KIND_DELETE,
        content_key=repair_schema.content_key(ops, kind=repair_schema.KIND_DELETE), model_id="")


@pytest.fixture()
def entity_mint_repo(tmp_path) -> str:
    """The bare remote path — see `build_bare_knowledge_repo`. Fresh per test."""
    return build_bare_knowledge_repo(str(tmp_path / "git"))


@pytest.fixture()
def require_gitleaks():
    """Skip on a laptop with no gitleaks on PATH; FAIL in CI (`$STIGMERGY_TEST_DSN` set) rather than
    let a secrets gate silently never run — the same posture `tests/entities/conftest.py` and
    `tests/server/conftest.py` each hold for their own `entity_approve`-adjacent git suites.
    `mint.mint` always scans (`refuse_secrets`), so every test that mints for real needs this."""
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
    """`entity_approve` walks the same `entities.remote.decide_via_clone` door a `librarian_repo_url`
    pointed at a real `https://` remote would authenticate through — same guard
    `tests/server/conftest.py` applies to its own package: no test here may mint a real GitHub
    installation token out of an operator's `.env`. Harmless for every OTHER admin suite in this
    package, which never sets `librarian_repo_url` at all."""
    from stigmergy.librarian import githubapp
    for name in (githubapp.APP_ID_ENV, githubapp.INSTALLATION_ID_ENV,
                githubapp.PRIVATE_KEY_ENV, githubapp.PRIVATE_KEY_FILE_ENV):
        monkeypatch.delenv(name, raising=False)


# ── what the librarian leaves behind: proposals, pushed to the bare remote and published ──────
def _proposed_page(name: str, entity_type: str, aliases, proposed_aliases=()) -> str:
    from stigmergy.entities import generator
    listed = "[" + ", ".join(f'"{a}"' for a in aliases) + "]"
    pending = "[" + ", ".join(f'"{a}"' for a in proposed_aliases) + "]"
    return (f'---\ntype: entity\ntitle: "{name}"\nentity_type: {entity_type}\nrole: ""\n'
            f'status: developing\naliases: {listed}\ncreated: 2026-08-20\nupdated: 2026-08-20\n'
            f'tags: [entity, {entity_type}]\nentity: ["{generator.canonical_id_for(name)}"]\n'
            f'related: []\nsources: []\napproved_by: ""\nproposed_aliases: {pending}\n---\n\n'
            f"# {name}\n\n## What / Who\n\n{name} is a {entity_type} the librarian proposed.\n")


def _with_clone(bare: str, work) -> None:
    """Clone the bare remote, run `work(clone)`, regenerate the registry, commit and push."""
    import tempfile

    from stigmergy.entities import generator
    with tempfile.TemporaryDirectory(prefix="admin-proposal-") as tmp:
        clone = os.path.join(tmp, "clone")
        _git("clone", "--quiet", bare, clone, cwd=tmp)
        work(clone)
        generator.regenerate(clone)
        _git("add", "--all", cwd=clone)
        _git("commit", "--quiet", "-m", "feat(note): the librarian proposed", cwd=clone)
        _git("push", "--quiet", "origin", "main", cwd=clone)


def publish_registry(bare: str, conn) -> None:
    """The index's registry snapshot — what the console's reads answer from — refreshed from the
    remote's tip, the way the push webhook refreshes it."""
    text = subprocess.run(["git", "show", "main:ops/entity-registry.json"], cwd=bare,
                          capture_output=True, text=True, check=True).stdout
    index_store.ensure_ops_file_table(conn)
    index_store.write_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH, text, "test")


def propose_identity(bare: str, conn, name: str = "Globex Robotics", *,
                     entity_type: str = "organization", aliases=(), proposed_aliases=()) -> str:
    """One proposed entity page (plus a note anchored to it) on the bare remote, and the
    snapshot published. Returns the entity id."""
    from stigmergy.entities import generator
    entity_id = generator.canonical_id_for(name)

    def work(clone):
        os.makedirs(os.path.join(clone, "wiki", "entities"), exist_ok=True)
        os.makedirs(os.path.join(clone, "wiki", "notes"), exist_ok=True)
        with open(os.path.join(clone, "wiki", "entities", f"{name}.md"), "w", encoding="utf-8") as f:
            f.write(_proposed_page(name, entity_type, aliases, proposed_aliases))
        with open(os.path.join(clone, "wiki", "notes", f"{name} kickoff.md"), "w",
                  encoding="utf-8") as f:
            f.write(f'---\ntype: note\ntitle: "{name} kickoff"\nstatus: developing\n'
                    f'created: 2026-08-20\nupdated: 2026-08-20\ntags: [note]\n'
                    f'entity: ["{entity_id}"]\nrelated: []\nsources: []\n---\n\n# {name} kickoff\n\nBody.\n')
    _with_clone(bare, work)
    publish_registry(bare, conn)
    return entity_id


def register_entity(bare: str, conn, name: str, *, entity_type: str = "organization",
                    aliases=(), proposed_aliases=()) -> str:
    """A CONFIRMED entity page on the bare remote (the merge target, or an alias proposal's
    owner), and the snapshot published. Returns the entity id."""
    from stigmergy.entities import generator
    entity_id = generator.canonical_id_for(name)

    def work(clone):
        os.makedirs(os.path.join(clone, "wiki", "entities"), exist_ok=True)
        text = _proposed_page(name, entity_type, aliases, proposed_aliases).replace(
            'approved_by: ""', 'approved_by: "steward@example.com"')
        with open(os.path.join(clone, "wiki", "entities", f"{name}.md"), "w", encoding="utf-8") as f:
            f.write(text)
    _with_clone(bare, work)
    publish_registry(bare, conn)
    return entity_id


def remote_registry(bare: str) -> dict:
    import json
    return json.loads(subprocess.run(["git", "show", "main:ops/entity-registry.json"], cwd=bare,
                                     capture_output=True, text=True, check=True).stdout)["entities"]


def remote_files(bare: str) -> list[str]:
    return subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"], cwd=bare,
                          capture_output=True, text=True, check=True).stdout.splitlines()
