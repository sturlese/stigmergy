"""The steward doorbell.

Real git (`tests.librarian.support.build_repo`, the same fixture knowledge repo the review-lane
suite uses) + real Postgres: `ops/stewards.json` is read at the base commit
(`stigmergy.server.review.load_stewards`), the proposals are real pages the librarian would have
committed, and the doorbell reads the registry the index snapshot carries — a faked inbox would
prove nothing about the property under test, the doorbell's own steward resolution and its
one-card-per-(item, steward) discipline.
"""
import asyncio
import json
import os

import pytest

from stigmergy.capture import schema as capture_schema
from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.entities import generator as entities_generator
from stigmergy.index import store as index_store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.librarian import githubapp
from stigmergy.repair import schema as repair_schema
from stigmergy.repair import store as repair_store
from stigmergy.server import review
from stigmergy.server.audit import AuditWriter, ensure_audit_table
from stigmergy.server.settings import Settings
from stigmergy.slack import doorbell as doorbell_module
from stigmergy.slack import store as slack_store
from stigmergy.slack.context import SlackContext
from stigmergy.slack.doorbell import poll_once
from stigmergy.slack.gateway import FakeSlackGateway
from stigmergy.slack.settings import SlackSettings
from tests import testdb
from tests.librarian import support

pytestmark = [pytest.mark.usefixtures("no_real_github_app"), pytest.mark.timeout(60)]

TEAM_ID = "T_STIGMERGY"
STEWARD = "steward@example.com"
STEWARD_SLACK_ID = "U_STEWARD"
ALICE = "alice@example.com"


@pytest.fixture(autouse=True)
def no_real_github_app(monkeypatch):
    for name in (githubapp.APP_ID_ENV, githubapp.INSTALLATION_ID_ENV,
                githubapp.PRIVATE_KEY_ENV, githubapp.PRIVATE_KEY_FILE_ENV):
        monkeypatch.delenv(name, raising=False)


def connect_or_skip():
    conn = testdb.connect_or_skip("doorbell")
    capture_schema.ensure_capture_schema(conn)
    review.ensure_review_schema(conn)
    # The third review kind's table. The doorbell reads `review.items_for_doorbell`, which is the
    # MANAGEMENT read over ALL kinds — so this suite meets `repair_proposals` whether or not it
    # seeds one, and a missing table would be an `UndefinedTable` in every test here.
    repair_schema.ensure_repair_schema(conn)
    ensure_audit_table(conn)
    from stigmergy.slack.store import ensure_slack_schema
    ensure_slack_schema(conn)
    return conn


@pytest.fixture()
def conn():
    c = connect_or_skip()
    with c.cursor() as cur:
        cur.execute("DELETE FROM capture_queue")
        cur.execute("DELETE FROM review_decisions")
        cur.execute("DELETE FROM job_runs")
        cur.execute("DELETE FROM steward_notifications")
        cur.execute("DELETE FROM audit_log")
        cur.execute("DELETE FROM repair_proposals")
    # The doorbell reads the registry off the index snapshot: start every test with none.
    index_store.clear_ops_file(c, index_store.ENTITY_REGISTRY_RELPATH)
    yield c
    index_store.clear_ops_file(c, index_store.ENTITY_REGISTRY_RELPATH)
    c.close()


@pytest.fixture()
def require_gitleaks():
    if support.gitleaks_available():
        return
    pytest.skip("gitleaks not on PATH (brew install gitleaks) — the repo fixtures need it")


@pytest.fixture()
def env(tmp_path, require_gitleaks):
    return support.build_repo(str(tmp_path))


def _write_stewards(env, content: str) -> None:
    path = os.path.join(env.repo, "ops", "stewards.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    # `support.commit_and_push`, not a hand-rolled add/commit/push: it passes an explicit author
    # identity, and a commit without one fails on a CI runner (no global git config, no domain in
    # the hostname) while passing on any laptop. This file had its own copy and CI caught it.
    support.commit_and_push(env.repo, "stewards.json")


def make_ctx(env, conn, *, gateway=None) -> SlackContext:
    server_settings = Settings(identity=STEWARD, knowledge_repo=env.repo, dsn=testdb.dsn(),
                              embedder="fake", llm="fake")
    slack_settings = SlackSettings(app_token="xapp-test", bot_token="xoxb-test", team_id=TEAM_ID,
                                   channels_path="", server=server_settings)
    return SlackContext(settings=slack_settings, gateway=gateway or FakeSlackGateway(), conn=conn,
                        embedder=build_embedder("fake"), evidence=MemoryEvidenceStore(),
                        audit=AuditWriter(conn))


def _configuration_rows(conn) -> list[str]:
    """Every deployment-wide "nothing can ever ring" fault the doorbell recorded, newest last."""
    with conn.cursor() as cur:
        cur.execute("SELECT error FROM job_runs WHERE job = 'steward-doorbell' "
                    "AND stats->>'event' = 'doorbell-configuration' ORDER BY id")
        return [row[0] for row in cur.fetchall()]


# ── what the librarian leaves behind, and what the doorbell reads it through ───────────────────
def _proposed_page(name: str, entity_type: str = "organization", aliases=()) -> str:
    listed = "[" + ", ".join(f'"{a}"' for a in aliases) + "]"
    return (f'---\ntype: entity\ntitle: "{name}"\nentity_type: {entity_type}\nrole: ""\n'
            f'status: developing\naliases: {listed}\ncreated: 2026-08-20\nupdated: 2026-08-20\n'
            f'tags: [entity, {entity_type}]\n'
            f'entity: ["{entities_generator.canonical_id_for(name)}"]\n'
            f'related: []\nsources: []\napproved_by: ""\nproposed_aliases: []\n---\n\n'
            f"# {name}\n\n## What / Who\n\n{name} is a {entity_type} the librarian proposed — the "
            f"raw material is never quoted here.\n")


def _publish_registry(env, conn) -> None:
    """The index's snapshot of the registry — what the deployed doorbell reads — refreshed from
    the checkout, the way the push webhook refreshes it after the librarian's commit."""
    with open(os.path.join(env.repo, "ops", "entity-registry.json"), encoding="utf-8") as f:
        index_store.write_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH, f.read(), "test")


def _propose(env, conn, name: str = "Globex Robotics", *, aliases=(), commit: bool = True) -> str:
    """One proposed identity, committed and pushed with the registry regenerated, and published
    to the index snapshot. Returns the item id (the entity's registry id)."""
    path = os.path.join(env.repo, "wiki", "entities", f"{name}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_proposed_page(name, aliases=aliases))
    if commit:
        entities_generator.regenerate(env.repo)
        support.commit_and_push(env.repo, f"feat(note): the librarian proposed {name}")
        _publish_registry(env, conn)
    return entities_generator.canonical_id_for(name)


def _propose_alias(env, conn, entity_id: str, alias: str) -> str:
    """One proposed spelling on a REGISTERED entity's page. Returns the item id."""
    [page] = [e for e in entities_generator.read_entity_pages(env.repo)
              if e.canonical_id == entity_id]
    full = os.path.join(env.repo, *page.relpath.split("/"))
    with open(full, encoding="utf-8") as f:
        text = f.read()
    listed = ", ".join(f'"{a}"' for a in (*page.proposed_aliases, alias))
    text = text.replace("related:", f"proposed_aliases: [{listed}]\nrelated:", 1)
    with open(full, "w", encoding="utf-8") as f:
        f.write(text)
    entities_generator.regenerate(env.repo)
    support.commit_and_push(env.repo, f"feat(entity): propose {alias}")
    _publish_registry(env, conn)
    return f"{entity_id}:{alias}"


def _decline_on_another_door(env, conn, entity_id: str, *, actor=STEWARD,
                             source=review.SOURCE_ADMIN, verdict="reject") -> None:
    """A decision landing through some OTHER door: the knowledge repo first (the proposed page is
    gone, the registry regenerated, the snapshot refreshed), then the ledger row — the order every
    real door writes them in."""
    [page] = [e for e in entities_generator.read_entity_pages(env.repo)
              if e.canonical_id == entity_id]
    os.remove(os.path.join(env.repo, *page.relpath.split("/")))
    entities_generator.regenerate(env.repo)
    support.commit_and_push(env.repo, f"chore(entity): decline {entity_id}")
    _publish_registry(env, conn)
    review.record_decision(conn, item_kind=review.KIND_IDENTITY_PROPOSAL, item_id=entity_id,
                           verdict=verdict, actor=actor, source=source)


def _run(coro):
    return asyncio.run(coro)


def _block_text(posted) -> str:
    """The rendered section text of a posted doorbell DM — `posted.text` is only the plain-text
    accessibility fallback, never the actual body."""
    return posted.blocks[0]["text"]["text"]


def _buttons(posted) -> list[str]:
    return [e["text"]["text"] for b in posted.blocks if b["type"] == "actions"
            for e in b["elements"]]


# ── a proposal rings the bell within one poller cycle ──────────────────────────────────────────
def test_a_proposed_identity_rings_the_doorbell_with_what_the_decision_needs(env, conn):
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    _propose(env, conn, "Globex Robotics", aliases=["Globex"])

    sent = _run(poll_once(ctx))

    assert sent == 1
    assert len(gw.posted) == 1
    posted = gw.posted[0]
    assert posted.channel_id == STEWARD_SLACK_ID
    text = _block_text(posted)
    assert "Globex Robotics" in text and "organization" in text
    assert "also spelled Globex" in text
    assert _buttons(posted) == ["Approve", "Merge into…", "Decline"]
    assert "globex-robotics" in posted.text   # the notification fallback names the item


def test_a_proposed_spelling_rings_with_approve_and_decline_only(env, conn):
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _propose_alias(env, conn, "acme-corp", "ACME Industries")

    sent = _run(poll_once(ctx))

    assert sent == 1
    posted = gw.posted[0]
    text = _block_text(posted)
    assert "ACME Industries" in text and "Acme Corp" in text
    assert _buttons(posted) == ["Approve", "Decline"]
    assert item_id in posted.text


def test_the_doorbell_rings_exactly_once_per_item_and_steward(env, conn):
    """A proposal has one open state, so a second pass with nothing changed stays quiet — and a
    second proposal rings on its own."""
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    _propose(env, conn, "Globex Robotics")

    assert _run(poll_once(ctx)) == 1
    assert _run(poll_once(ctx)) == 0
    assert len(gw.posted) == 1

    _propose(env, conn, "Initrode")
    assert _run(poll_once(ctx)) == 1
    assert len(gw.posted) == 2


def test_an_identity_proposed_again_after_a_decision_rings_again_with_a_live_card(env, conn):
    """The ledger keeps the old verdict for good, while a new page under the same id is a new
    proposal (a steward's `create`, a re-proposal after the decline memory was bypassed). The
    closing pass asks "decided SINCE this card", so the fresh card stays live."""
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    entity_id = _propose(env, conn, "Globex Robotics")
    assert _run(poll_once(ctx)) == 1

    _decline_on_another_door(env, conn, entity_id)
    _run(poll_once(ctx))                     # the first card is closed, correctly
    assert len(gw.updated) == 1

    _propose(env, conn, "Globex Robotics")   # the same id, proposed again
    assert _run(poll_once(ctx)) == 1, "the re-proposal must ring the bell again"
    assert len(gw.posted) == 2
    assert len(gw.updated) == 1, (
        "the NEW card must still be live — the only decision on this id predates it")


# ── the card's scope is the entity page: a delegated zone rings its own steward ────────────────
def test_a_zone_steward_is_rung_for_a_proposal_whose_page_is_in_their_zone(env, conn):
    """`wiki/entities` delegated to ALICE: the proposal's page sits there, so ALICE is rung and
    the general steward is not — the zone delegation `ops/stewards.json` exists for, now honoured
    by the doorbell because a proposal HAS a page path. It needs the page in the index (the item's
    `page` is the index's answer); an unindexed proposal resolves the universal key."""
    _write_stewards(env, f'{{"*": ["{STEWARD}"], "wiki/entities": ["{ALICE}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    gw.seed_email(ALICE, "U_ALICE")
    ctx = make_ctx(env, conn, gateway=gw)
    entity_id = _propose(env, conn, "Globex Robotics")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pages_index WHERE path = %s", ("wiki/entities/Globex Robotics.md",))
        cur.execute("INSERT INTO pages_index (path, page_id, zone, type, entity, content_hash) "
                    "VALUES (%s, %s, 'wiki', 'entity', %s, '')",
                    ("wiki/entities/Globex Robotics.md", entity_id, [entity_id]))
    try:
        sent = _run(poll_once(ctx))
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pages_index WHERE path = %s",
                        ("wiki/entities/Globex Robotics.md",))
    assert sent == 1
    assert [p.channel_id for p in gw.posted] == ["U_ALICE"]


# ── undeliverable is recorded, never swallowed ─────────────────────────────────────────────────
def test_no_steward_resolves_for_this_items_scope_writes_a_job_runs_row(env, conn):
    """A POPULATED map that simply does not cover this item's scope (no matching prefix, no `"*"`
    fallback) — the per-item "nobody is on call for THIS scope" fact, distinct from the "the map
    is entirely empty" misconfiguration case below."""
    _write_stewards(env, '{"wiki/some/other/zone/": ["someone@example.com"]}')
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _propose(env, conn)

    sent = _run(poll_once(ctx))

    assert sent == 0
    assert gw.posted == []
    with conn.cursor() as cur:
        cur.execute("SELECT error, stats FROM job_runs WHERE job = 'steward-doorbell'")
        row = cur.fetchone()
    assert row is not None
    error, stats = row
    assert "no steward resolves" in error
    assert item_id in stats["item_ref"]


def test_a_completely_empty_stewards_map_is_recorded_once_not_once_per_item(env, conn):
    _write_stewards(env, "{}")   # exactly what ships before ops/stewards.json is committed+pushed
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    for name in ("Globex Robotics", "Initrode", "Vandelay Imports"):
        _propose(env, conn, name)

    first = _run(poll_once(ctx))
    second = _run(poll_once(ctx))   # a second pass, same empty map — must add nothing

    assert first == 0 and second == 0
    assert gw.posted == []
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_runs WHERE job = 'steward-doorbell'")
        (count,) = cur.fetchone()
    assert count == 1, "three open items and two passes must not multiply into three (or six) rows"


def test_a_steward_with_no_slack_identity_writes_a_job_runs_row(env, conn):
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()   # STEWARD's email is never seeded — no Slack identity resolves
    ctx = make_ctx(env, conn, gateway=gw)
    _propose(env, conn)

    sent = _run(poll_once(ctx))

    assert sent == 0
    assert gw.posted == []
    with conn.cursor() as cur:
        cur.execute("SELECT error FROM job_runs WHERE job = 'steward-doorbell'")
        (error,) = cur.fetchone()
    assert STEWARD in error
    assert "no Slack identity" in error


def test_a_transient_lookup_failure_is_undeliverable_but_never_crashes_the_pass(env, conn):
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.fail_lookup_by_email.add(STEWARD)
    ctx = make_ctx(env, conn, gateway=gw)
    _propose(env, conn)

    sent = _run(poll_once(ctx))   # must not raise
    assert sent == 0


def test_a_transient_lookup_failure_is_never_recorded_as_no_slack_identity(env, conn):
    """A timeout/5xx/429 must never be recorded as the SAME fact an honest `users_not_found` miss
    is — that would be a false, potentially permanent claim about a real person who simply could
    not be looked up just now. No `job_runs` row at all for this pass; the next pass retries."""
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.fail_lookup_by_email.add(STEWARD)
    ctx = make_ctx(env, conn, gateway=gw)
    _propose(env, conn)

    _run(poll_once(ctx))

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_runs WHERE job = 'steward-doorbell'")
        (count,) = cur.fetchone()
    assert count == 0


def test_the_slack_user_id_lookup_is_cached_across_polls(env, conn):
    """`users.lookupByEmail` is Tier-3 (~50/min): once resolved, a second pass must not call the
    gateway's lookup again for the same (team, email)."""
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    _propose(env, conn, "Globex Robotics")
    _propose(env, conn, "Initrode")

    _run(poll_once(ctx))
    cached_id = ctx.cache.get_id_by_email(ctx.settings.team_id, STEWARD)
    assert cached_id == STEWARD_SLACK_ID

    # A second pass, with the gateway's lookup now scripted to fail outright — it must not be
    # consulted at all, because the id is already cached.
    gw.fail_lookup_by_email.add(STEWARD)
    _propose(env, conn, "Vandelay Imports")
    sent = _run(poll_once(ctx))
    assert sent == 1   # the new item still gets a DM — the cached id served it, no lookup needed


def test_a_failed_send_is_retried_next_pass_and_not_marked_notified(env, conn):
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    gw.fail_post_count = 1
    ctx = make_ctx(env, conn, gateway=gw)
    _propose(env, conn)

    first = _run(poll_once(ctx))
    assert first == 0        # the post failed
    assert gw.posted == []

    second = _run(poll_once(ctx))
    assert second == 1       # retried, and this time it succeeds
    assert len(gw.posted) == 1


# ── a delivered DM must be MEASURABLE, not only a failed one ───────────────────────────────────
def test_a_delivered_dm_writes_an_audit_log_row(env, conn):
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _propose(env, conn)

    sent = _run(poll_once(ctx))

    assert sent == 1
    with conn.cursor() as cur:
        cur.execute("SELECT identity, tool, outcome, args FROM audit_log")
        rows = cur.fetchall()
    assert len(rows) == 1
    identity, tool, outcome, args = rows[0]
    assert identity == STEWARD
    assert tool == "steward-doorbell"
    assert outcome == "ok"
    assert args["item_kind"] == review.KIND_IDENTITY_PROPOSAL
    assert args["item_id"] == item_id


def test_an_undeliverable_notification_writes_no_audit_log_row(env, conn):
    _write_stewards(env, '{"*": ["nobody-registered@example.com"]}')
    ctx = make_ctx(env, conn, gateway=FakeSlackGateway())
    _propose(env, conn)

    sent = _run(poll_once(ctx))

    assert sent == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_log")
        assert cur.fetchone()[0] == 0


def test_no_audit_writer_wired_does_not_crash_a_delivered_pass(env, conn):
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    ctx.audit = None
    _propose(env, conn)

    sent = _run(poll_once(ctx))

    assert sent == 1
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_log")
        assert cur.fetchone()[0] == 0


# ── every open item rings, and a bounded read says when it is not enough ──────────────────────
def test_several_proposals_all_ring_in_one_pass(env, conn):
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    names = ["Globex Robotics", "Initrode", "Vandelay Imports", "Hooli", "Pied Piper"]
    for name in names[:-1]:
        _propose(env, conn, name, commit=False)
    ids = [entities_generator.canonical_id_for(n) for n in names]
    _propose(env, conn, names[-1])     # one commit carrying all five

    sent = _run(poll_once(ctx))

    assert sent == len(names), "every open item should ring — none silently dropped"
    for item_id in ids:
        assert slack_store.last_notified_state(
            conn, item_kind=review.KIND_IDENTITY_PROPOSAL, item_id=item_id,
            steward_email=STEWARD) is not None


def test_items_for_doorbell_logs_when_its_own_limit_is_still_not_enough(env, conn, caplog):
    """The "no silent caps" half: a caller sizing `limit` generously (the doorbell's own 500)
    should still be able to tell, from the process log, when that was not enough."""
    for name in ("Globex Robotics", "Initrode"):
        _propose(env, conn, name, commit=False)
    _propose(env, conn, "Vandelay Imports")

    with caplog.at_level("WARNING", logger="stigmergy.server.review"):
        items = review.items_for_doorbell(conn, limit=2)

    assert len(items) == 2
    assert any("raise the limit to see the rest" in r.message for r in caplog.records)


# ── `ops/stewards.json` must not be re-fetched from git on every poll pass ─────────────────────
def test_load_stewards_is_cached_across_poll_passes_within_the_ttl(env, conn, monkeypatch):
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    _propose(env, conn)

    calls = []
    original = review.load_stewards

    def _spy(repo, baked_path=""):
        calls.append(repo)
        return original(repo)

    monkeypatch.setattr(review, "load_stewards", _spy)

    _run(poll_once(ctx))
    _run(poll_once(ctx))
    _run(poll_once(ctx))

    assert len(calls) == 1, \
        "ops/stewards.json was re-read (a real git fetch) on more than one poll pass"


def test_load_stewards_cache_refreshes_once_its_ttl_expires(env, conn, monkeypatch):
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    fake_now = {"t": 0.0}
    ctx._clock = lambda: fake_now["t"]
    _propose(env, conn)

    calls = []
    original = review.load_stewards

    def _spy(repo, baked_path=""):
        calls.append(repo)
        return original(repo)

    monkeypatch.setattr(review, "load_stewards", _spy)

    _run(poll_once(ctx))
    assert len(calls) == 1
    fake_now["t"] += doorbell_module._STEWARDS_CACHE_TTL_S + 1
    _run(poll_once(ctx))
    assert len(calls) == 2, "the cache never expired — a stewards.json edit would never take effect"


# ── the card carries the page's own summary, never a capture's raw material ───────────────────
def test_the_card_shows_the_pages_summary_and_never_a_captures_raw_material(env, conn):
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    entity_id = _propose(env, conn, "Globex Robotics")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pages_index WHERE path = %s", ("wiki/entities/Globex Robotics.md",))
        cur.execute("INSERT INTO pages_index (path, page_id, zone, type, entity, body, content_hash)"
                    " VALUES (%s, %s, 'wiki', 'entity', %s, %s, '')",
                    ("wiki/entities/Globex Robotics.md", entity_id, [entity_id],
                     "# Globex Robotics\n\n## What / Who\n\nA robotics vendor met in a pilot.\n"))
    try:
        _run(poll_once(ctx))
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pages_index WHERE path = %s",
                        ("wiki/entities/Globex Robotics.md",))

    text = _block_text(gw.posted[0])
    assert "A robotics vendor met in a pilot." in text
    assert "raw material" not in text


# ── remaining unit-level coverage for pure/mechanical helpers ────────────────────────────────────
def test_state_signature_is_one_open_state_per_proposal():
    """A proposal is open or decided; the anchored-page count growing is not a reason to ring."""
    first = doorbell_module._state_signature(
        {"kind": review.KIND_IDENTITY_PROPOSAL, "id": "x", "anchored_total": 1})
    second = doorbell_module._state_signature(
        {"kind": review.KIND_IDENTITY_PROPOSAL, "id": "x", "anchored_total": 4})
    assert first == second == "proposed"


def test_a_malformed_stewards_json_does_not_crash_the_pass(env, conn):
    path = os.path.join(env.repo, "ops", "stewards.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not valid json")
    support.commit_and_push(env.repo, "malformed stewards.json")
    ctx = make_ctx(env, conn, gateway=FakeSlackGateway())
    _propose(env, conn)

    assert _run(poll_once(ctx)) == 0   # must not raise


def test_run_doorbell_calls_poll_once_each_pass_and_stops_on_the_stop_event(env, conn, monkeypatch):
    calls = []

    async def fake_poll_once(ctx):
        calls.append(1)
        return 0

    monkeypatch.setattr(doorbell_module, "poll_once", fake_poll_once)
    ctx = make_ctx(env, conn, gateway=FakeSlackGateway())
    stop_event = asyncio.Event()

    async def _drive():
        task = asyncio.ensure_future(doorbell_module.run_doorbell(ctx, interval_s=0.01,
                                                                  stop_event=stop_event))
        await asyncio.sleep(0.05)
        stop_event.set()
        await task

    _run(_drive())
    assert calls   # the loop actually invoked poll_once at least once before stopping


def test_run_doorbell_survives_a_bad_pass_and_keeps_looping(env, conn, monkeypatch):
    calls = []

    async def flaky_poll_once(ctx):
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(doorbell_module, "poll_once", flaky_poll_once)
    ctx = make_ctx(env, conn, gateway=FakeSlackGateway())
    stop_event = asyncio.Event()

    async def _drive():
        task = asyncio.ensure_future(doorbell_module.run_doorbell(ctx, interval_s=0.01,
                                                                  stop_event=stop_event))
        await asyncio.sleep(0.05)
        stop_event.set()
        await task

    _run(_drive())   # must not raise, even though every pass fails
    assert calls


def test_no_stewards_source_at_all_is_recorded_not_swallowed(env, conn):
    """A deployment whose `app`/`slack` groups hold no checkout and no baked map: nothing can
    ever resolve to a steward, and that fact reaches `job_runs` once per process rather than
    vanishing into scrollback."""
    server_settings = Settings(identity=STEWARD, dsn=testdb.dsn(), embedder="fake", llm="fake")
    slack_settings = SlackSettings(app_token="xapp-test", bot_token="xoxb-test", team_id=TEAM_ID,
                                   channels_path="", server=server_settings)
    ctx = SlackContext(settings=slack_settings, gateway=FakeSlackGateway(), conn=conn,
                       embedder=build_embedder("fake"), evidence=MemoryEvidenceStore())
    _propose(env, conn)

    assert _run(poll_once(ctx)) == 0

    rows = _configuration_rows(conn)
    assert len(rows) == 1
    assert "neither" in rows[0].lower()
    assert _run(poll_once(ctx)) == 0
    assert len(_configuration_rows(conn)) == 1


def test_a_baked_stewards_map_rings_the_doorbell_with_no_checkout_at_all(env, conn, tmp_path):
    """The deployed shape: the `slack` group holds no checkout, so the ONLY thing that can make
    the bell ring is the map the deploy baked into the image — and the registry it reads is the
    index's snapshot."""
    baked = tmp_path / "stewards.json"
    baked.write_text(f'{{"*": ["{STEWARD}"]}}')
    server_settings = Settings(identity=STEWARD, dsn=testdb.dsn(), embedder="fake", llm="fake",
                               stewards_path=str(baked))
    slack_settings = SlackSettings(app_token="xapp-test", bot_token="xoxb-test", team_id=TEAM_ID,
                                   channels_path="", server=server_settings)
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = SlackContext(settings=slack_settings, gateway=gw, conn=conn,
                       embedder=build_embedder("fake"), evidence=MemoryEvidenceStore())
    item_id = _propose(env, conn)

    assert _run(poll_once(ctx)) == 1
    assert item_id in gw.posted[0].text
    assert _configuration_rows(conn) == [], "a working deployment records no configuration fault"


# ── a decided item's card closes itself ────────────────────────────────────────────────────────
def _ring_once(env, conn):
    """The doorbell's ordinary first pass: one item, one DM, one recorded notification."""
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _propose(env, conn)
    assert _run(poll_once(ctx)) == 1
    return ctx, gw, item_id


def test_a_decided_proposals_card_is_closed_exactly_once(env, conn):
    """Exactly once is the load-bearing half: the closing pass runs every poll, so a pass that
    re-edited an already-closed card would rewrite the steward's DM every ten seconds forever.
    `mark_notified(state="closed:<verdict>")` is what makes the second pass a no-op."""
    ctx, gw, item_id = _ring_once(env, conn)
    _decline_on_another_door(env, conn, item_id, actor="console-operator@example.com")

    assert _run(doorbell_module.close_decided_cards(ctx)) == 1
    assert _run(doorbell_module.close_decided_cards(ctx)) == 0, "the second pass must be a no-op"

    assert len(gw.updated) == 1
    assert len(gw.posted) == 1, "closing edits the card in place — it never posts a second one"
    updated = gw.updated[0]
    assert (updated.channel_id, updated.ts) == (gw.posted[0].channel_id, gw.posted[0].ts)


def test_the_closed_card_names_the_verdict_the_actor_and_the_door_and_drops_its_buttons(env, conn):
    ctx, gw, item_id = _ring_once(env, conn)
    _decline_on_another_door(env, conn, item_id, actor="console-operator@example.com")

    _run(doorbell_module.close_decided_cards(ctx))

    blocks = gw.updated[0].blocks
    assert not [b for b in blocks if b["type"] == "actions"], (
        "a closed card must carry no buttons — a stale click is exactly what this removes")
    rendered = " ".join(str(b) for b in blocks)
    assert "reject" in rendered
    assert "console-operator@example.com" in rendered
    assert "admin" in rendered
    assert item_id in rendered, "the card must still name the item it was about"
    assert item_id in gw.updated[0].text, "and so must the notification fallback"


def test_an_undecided_items_card_is_left_alone(env, conn):
    """The benign twin. The closing pass reads EVERY open card on every poll; a bug in its
    decision lookup would silently disarm the whole doorbell by closing cards nobody decided."""
    ctx, gw, _item_id = _ring_once(env, conn)

    assert _run(doorbell_module.close_decided_cards(ctx)) == 0
    assert gw.updated == []


def test_an_alias_decision_closes_its_own_card(env, conn):
    """The other item kind: the ledger row is keyed by the alias item id, and the card closes on
    it whichever verdict landed."""
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _propose_alias(env, conn, "acme-corp", "ACME Industries")
    assert _run(poll_once(ctx)) == 1
    review.record_decision(conn, item_kind=review.KIND_ALIAS_PROPOSAL, item_id=item_id,
                           verdict="approve", actor=STEWARD, source=review.SOURCE_SLACK)

    assert _run(doorbell_module.close_decided_cards(ctx)) == 1
    rendered = " ".join(str(b) for b in gw.updated[0].blocks)
    assert "approve" in rendered and "slack" in rendered


def test_a_notification_recorded_before_the_card_pointer_existed_is_skipped(env, conn):
    ctx, gw, item_id = _ring_once(env, conn)
    with conn.cursor() as cur:      # exactly what a pre-change database holds
        cur.execute("UPDATE steward_notifications SET channel_id = '', message_ts = ''")
    _decline_on_another_door(env, conn, item_id)

    assert _run(doorbell_module.close_decided_cards(ctx)) == 0
    assert gw.updated == []


def test_a_failed_chat_update_leaves_the_card_open_for_the_next_pass(env, conn):
    ctx, gw, item_id = _ring_once(env, conn)
    _decline_on_another_door(env, conn, item_id)
    gw.fail_update_count = 1

    assert _run(doorbell_module.close_decided_cards(ctx)) == 0
    assert _run(doorbell_module.close_decided_cards(ctx)) == 1, "the next pass retries it"
    assert len(gw.updated) == 1


def test_a_card_slack_says_is_gone_is_recorded_unreachable_and_never_retried(env, conn):
    ctx, gw, item_id = _ring_once(env, conn)
    _decline_on_another_door(env, conn, item_id)
    gw.fail_update_count = 1
    gw.fail_update_code = "message_not_found"

    assert _run(doorbell_module.close_decided_cards(ctx)) == 0, (
        "an unclosable card is not a closed one — the count is cards actually edited shut")
    assert slack_store.last_notified_state(
        conn, item_kind=review.KIND_IDENTITY_PROPOSAL, item_id=item_id,
        steward_email=STEWARD) == slack_store.CLOSED_UNREACHABLE

    gw.fail_update_count = 0     # Slack would answer perfectly well now; nothing must ask it
    assert _run(doorbell_module.close_decided_cards(ctx)) == 0
    assert gw.updated == []


def test_poll_once_closes_decided_cards_as_part_of_an_ordinary_pass(env, conn):
    ctx, gw, item_id = _ring_once(env, conn)
    _decline_on_another_door(env, conn, item_id)

    _run(poll_once(ctx))

    assert len(gw.updated) == 1


def test_the_card_a_new_one_replaces_is_superseded_never_left_live_in_the_dm(env, conn):
    """`steward_notifications` holds ONE row per (item, steward), so posting a second card for
    the same item overwrites the first card's coordinates — the replaced card is edited into a
    buttonless "superseded" shape BEFORE the replacement is posted, so the pointer is spent while
    it is still the only one recorded."""
    ctx, gw, item_id = _ring_once(env, conn)
    # A decision and a re-proposal under the same id: the closing pass closes the first card;
    # the re-proposal posts a second one. To exercise the SUPERSEDE road the first card must still
    # be live when the second is posted, so the closing pass is skipped by deciding and
    # re-proposing between two `_notify_item`-only calls.
    _decline_on_another_door(env, conn, item_id)
    _propose(env, conn)
    stewards = review.load_stewards(ctx.settings.server.knowledge_repo)
    [item] = [i for i in review.items_for_doorbell(conn) if i["id"] == item_id]
    with conn.cursor() as cur:   # forget the decision's newer-than-card view for this pass
        cur.execute("UPDATE steward_notifications SET state = 'stale-state'")
    assert _run(doorbell_module._notify_item(ctx, item, stewards)) == 1
    assert len(gw.posted) == 2
    assert len(gw.updated) == 1, "the replaced card was edited before the new one was posted"
    rendered = " ".join(str(b) for b in gw.updated[0].blocks)
    assert "superseded" in rendered.lower()
    assert item_id in rendered
    assert not [b for b in gw.updated[0].blocks if b["type"] == "actions"]


@pytest.mark.parametrize("error_code", ["", "message_not_found"])
def test_a_failed_supersede_edit_still_lets_the_new_card_through(env, conn, error_code):
    """The priority when Slack is half-available: the steward MUST get the new card."""
    ctx, gw, item_id = _ring_once(env, conn)
    stewards = review.load_stewards(ctx.settings.server.knowledge_repo)
    [item] = [i for i in review.items_for_doorbell(conn) if i["id"] == item_id]
    with conn.cursor() as cur:
        cur.execute("UPDATE steward_notifications SET state = 'stale-state'")
    gw.fail_update_code = error_code
    gw.fail_update_count = 1

    assert _run(doorbell_module._notify_item(ctx, item, stewards)) == 1
    assert len(gw.posted) == 2


# ── a kind with no renderer must not ring ──────────────────────────────────────────────────────
def _propose_repair(conn, path="wiki/notes/Renewals.md") -> int:
    ops = [{"op": "backlink", "path": path, "link": "Existing Note", "note": ""}]
    return repair_store.insert_proposal(
        conn, run_id=0, finding_ids=[1], target_paths=repair_schema.target_paths(ops), ops=ops,
        rationale="neither page links the other", content_key=repair_schema.content_key(ops),
        model_id="fake")


def test_a_pending_repair_proposal_does_not_ring_the_doorbell(env, conn):
    """`repair-proposal` is in `review.items_for_doorbell`'s output — the MANAGEMENT read over
    every kind — but this module has no card for it. Silence is the correct behaviour until
    somebody writes the card; a wrong card is worse than none."""
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    proposal_id = _propose_repair(conn)

    sent = _run(poll_once(ctx))

    assert sent == 0
    assert gw.posted == []
    assert [i["kind"] for i in review.items_for_doorbell(conn)] == [review.KIND_REPAIR_PROPOSAL], (
        "the item is still IN the shared inbox read — the doorbell skips it, it is not hidden")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM steward_notifications WHERE item_id = %s",
                    (str(proposal_id),))
        assert cur.fetchone()[0] == 0, "a skipped kind leaves no notification state behind either"


def test_a_repair_proposal_beside_an_identity_proposal_rings_only_for_the_identity(env, conn):
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    _propose_repair(conn)
    item_id = _propose(env, conn)

    sent = _run(poll_once(ctx))

    assert sent == 1
    assert len(gw.posted) == 1
    assert item_id in gw.posted[0].text


def test_every_item_kind_the_doorbell_rings_for_has_a_renderer(env, conn):
    """`_EVENT_NAMES` IS the set of kinds that ring, and every one of them must be a kind
    `review_kinds` declares and a kind `_RENDERERS` can draw."""
    assert set(doorbell_module._EVENT_NAMES) <= set(review.ITEM_KINDS)
    assert set(doorbell_module._EVENT_NAMES) == set(doorbell_module._RENDERERS) == {
        review.KIND_IDENTITY_PROPOSAL, review.KIND_ALIAS_PROPOSAL}


def test_the_registry_snapshot_is_what_the_doorbell_reads(env, conn):
    """No snapshot, no proposals — the checkout alone is not enough for a process that holds
    none, and `_propose` publishes the snapshot for exactly that reason."""
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    _propose(env, conn)
    index_store.clear_ops_file(conn, index_store.ENTITY_REGISTRY_RELPATH)

    assert _run(poll_once(ctx)) == 0
    assert gw.posted == []
    assert json.loads(open(os.path.join(env.repo, "ops", "entity-registry.json")).read())[
        "entities"]["globex-robotics"]["proposed"] is True
