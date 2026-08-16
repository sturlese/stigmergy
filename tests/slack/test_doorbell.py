"""The steward doorbell.

Real git (`tests.librarian.support.build_repo`, the same fixture knowledge repo the review-lane
suite uses) + real Postgres: `ops/stewards.json` is read at the base commit
(`stigmergy.server.review.load_stewards`), and a faked git tree would prove nothing about the
property under test — the doorbell's own steward resolution.
"""
import asyncio

import pytest
from psycopg.types.json import Jsonb

from stigmergy.capture import queue as capture_queue
from stigmergy.capture import schema as capture_schema
from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.librarian import githubapp
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

pytestmark = [pytest.mark.usefixtures("no_real_github_app"), pytest.mark.timeout(30)]

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
    yield c
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
    import os
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


def _park_capture(conn, evidence, *, submitted_by=ALICE, summary="parked for a look",
                  situation=None, kind: str = "raw") -> int:
    key = evidence.put(b"the raw material -- never shown by the doorbell")
    report = {"summary": summary, "status": capture_schema.TRIAGE}
    if situation:
        report[capture_schema.SITUATION_KEY] = situation
        report[capture_schema.SITUATION_NAME_KEY] = "Globex Robotics"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO capture_queue (kind, payload, blob_refs, submitted_by, status, report) "
            "VALUES (%s, '{}', %s, %s, %s, %s) RETURNING id",
            (kind, [key], submitted_by, capture_schema.TRIAGE, Jsonb(report)))
        return cur.fetchone()[0]


def _run(coro):
    return asyncio.run(coro)


def _block_text(posted) -> str:
    """The rendered section text of a posted doorbell DM — `posted.text` is only the plain-text
    accessibility fallback (`f"{noun} #{id} needs a decision"`), never the actual body."""
    return posted.blocks[0]["text"]["text"]


# ── a parked capture rings the bell within one poller cycle ────────────────────────────────────
def test_triage_capture_rings_the_doorbell(env, conn):
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _park_capture(conn, MemoryEvidenceStore())

    sent = _run(poll_once(ctx))

    assert sent == 1
    assert len(gw.posted) == 1
    posted = gw.posted[0]
    assert posted.channel_id == STEWARD_SLACK_ID
    text = posted.blocks[0]["text"]["text"]
    assert f"#{item_id}" in text
    assert "stigmergy-queue show" in text
    assert "parked for a look" in text


# ── the doorbell rings exactly once per state change, for EVERY kind. The rest of this file
# exercises the doorbell against `kind="raw"` rows only; the doorbell's own read path
# (`stigmergy.server.review.items_for_doorbell` -> `capture.queue._shape_listed`) never branches on
# `kind` when read, but "I read it and it doesn't branch" is exactly the class of claim that
# needs a test rather than a code review.
def test_a_parked_meeting_capture_rings_the_doorbell_the_same_way_a_raw_one_does(env, conn):
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _park_capture(conn, MemoryEvidenceStore(),
                            summary="the meeting names entities the registry does not recognize",
                            kind="meeting")

    sent = _run(poll_once(ctx))

    assert sent == 1
    assert len(gw.posted) == 1
    posted = gw.posted[0]
    assert posted.channel_id == STEWARD_SLACK_ID
    text = _block_text(posted)
    assert f"#{item_id}" in text
    assert "stigmergy-queue show" in text
    assert "the meeting names entities the registry does not recognize" in text

    # a second pass with nothing changed does not ring again — the SAME one-notification-per-
    # state-change property the ordinary `raw` case gets, now checked for `meeting`
    sent_again = _run(poll_once(ctx))
    assert sent_again == 0
    assert len(gw.posted) == 1


# ── the ordinary requeue-and-reprocess loop must ring the bell AGAIN, not go permanently silent
# — end to end, through the REAL queue mechanics ───────────────────────────────────────────────
def test_requeue_and_reprocess_back_into_the_same_status_rings_a_second_time(env, conn):
    """The sequence that exposes it: capture parks (triage) -> bell rings -> steward
    clicks Requeue -> the librarian reprocesses and parks it in `triage` AGAIN (requeue's own
    purpose is "try again", and landing back in the same status is the ordinary outcome, not an
    edge case) -> the OLD code's `_state_signature` (status alone) was identical to what was
    already recorded, so the bell never rang again for that capture, ever.

    Driven through the REAL queue primitives (`dispositions.requeue`, `queue.claim_next`,
    `queue.finish`) rather than a raw UPDATE, so this proves the actual end-to-end wiring: `attempts`
    flows capture_queue -> `review._collect_open_items` -> `doorbell._state_signature`."""
    from stigmergy.capture import dispositions
    from stigmergy.capture import queue as capture_queue

    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _park_capture(conn, MemoryEvidenceStore(), summary="parked for a look")

    first = _run(poll_once(ctx))
    assert first == 1, "the first park must ring the bell"
    assert len(gw.posted) == 1

    second = _run(poll_once(ctx))
    assert second == 0, "no state change yet — must not re-ring"
    assert len(gw.posted) == 1

    # The steward clicks Requeue (the SAME disposition `review_decide` drives on this verdict).
    dispositions.requeue(conn, item_id, actor=STEWARD, note="try again")
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM capture_queue WHERE id = %s", (item_id,))
        assert cur.fetchone()[0] == capture_schema.QUEUED

    # The librarian reprocesses: claims it (a REAL delivery, incrementing `attempts`) and parks it
    # right back in `triage` — the ordinary "try again, same outcome" result requeue exists for.
    claimed = capture_queue.claim_next(conn)
    assert claimed is not None and claimed["id"] == item_id
    capture_queue.finish(conn, item_id, status=capture_schema.TRIAGE,
                         expected_attempts=claimed["attempts"],
                         report={"summary": "parked for a look, again", "status": capture_schema.TRIAGE})

    third = _run(poll_once(ctx))
    assert third == 1, (
        "a capture that returns to the SAME status after a real reprocessing claim must ring the "
        "bell again — otherwise the doorbell goes permanently silent for that item")
    assert len(gw.posted) == 2
    assert "parked for a look, again" in _block_text(gw.posted[1])

    # And it goes quiet again at THIS new state, exactly as before — the fix does not turn the
    # doorbell into a re-ring-every-pass nuisance.
    fourth = _run(poll_once(ctx))
    assert fourth == 0
    assert len(gw.posted) == 2




# ── undeliverable is recorded, never swallowed ─────────────────────────────────────────────────
def test_no_steward_resolves_for_this_items_scope_writes_a_job_runs_row(env, conn):
    """A POPULATED map that simply does not cover this item's scope (no matching prefix, no `"*"`
    fallback) — the per-item "nobody is on call for THIS scope" fact, distinct from the "the map
    is entirely empty" misconfiguration case below."""
    _write_stewards(env, '{"wiki/some/other/zone/": ["someone@example.com"]}')
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _park_capture(conn, MemoryEvidenceStore())

    sent = _run(poll_once(ctx))

    assert sent == 0
    assert gw.posted == []
    with conn.cursor() as cur:
        cur.execute("SELECT error, stats FROM job_runs WHERE job = 'steward-doorbell'")
        row = cur.fetchone()
    assert row is not None
    error, stats = row
    assert "no steward resolves" in error
    assert str(item_id) in stats["item_ref"]


# --- an EMPTY stewards map is one global fact, logged and recorded ONCE per process — not once
# per item per pass ------------------------------------------------------------------------------
def test_a_completely_empty_stewards_map_is_recorded_once_not_once_per_item(env, conn):
    _write_stewards(env, "{}")   # exactly what ships before ops/stewards.json is committed+pushed
    gw = FakeSlackGateway()
    ctx = make_ctx(env, conn, gateway=gw)
    _park_capture(conn, MemoryEvidenceStore())
    _park_capture(conn, MemoryEvidenceStore())
    _park_capture(conn, MemoryEvidenceStore())

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
    _park_capture(conn, MemoryEvidenceStore())

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
    _park_capture(conn, MemoryEvidenceStore())

    sent = _run(poll_once(ctx))   # must not raise
    assert sent == 0


def test_a_transient_lookup_failure_is_never_recorded_as_no_slack_identity(env, conn):
    """A timeout/5xx/429 must never be recorded as the SAME fact an honest
    `users_not_found` miss is — that would be a false, potentially permanent claim about a real
    person who simply could not be looked up just now. No `job_runs` row at all for this pass;
    the next pass just retries the lookup."""
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.fail_lookup_by_email.add(STEWARD)
    ctx = make_ctx(env, conn, gateway=gw)
    _park_capture(conn, MemoryEvidenceStore())

    _run(poll_once(ctx))

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_runs WHERE job = 'steward-doorbell'")
        (count,) = cur.fetchone()
    assert count == 0


def test_the_slack_user_id_lookup_is_cached_across_polls(env, conn):
    """`users.lookupByEmail` is Tier-3 (~50/min) and used to run once per (item,
    steward) on EVERY poll pass with no cache at all. Once resolved, a second pass must not call
    the gateway's lookup again for the same (team, email)."""
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    _park_capture(conn, MemoryEvidenceStore())
    _park_capture(conn, MemoryEvidenceStore())

    _run(poll_once(ctx))
    cached_id = ctx.cache.get_id_by_email(ctx.settings.team_id, STEWARD)
    assert cached_id == STEWARD_SLACK_ID

    # A second pass, with the gateway's lookup now scripted to fail outright — it must not be
    # consulted at all, because the id is already cached.
    gw.fail_lookup_by_email.add(STEWARD)
    _park_capture(conn, MemoryEvidenceStore())
    sent = _run(poll_once(ctx))
    assert sent == 1   # the new item still gets a DM — the cached id served it, no lookup needed


def test_a_failed_send_is_retried_next_pass_and_not_marked_notified(env, conn):
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    gw.fail_post_count = 1
    ctx = make_ctx(env, conn, gateway=gw)
    _park_capture(conn, MemoryEvidenceStore())

    first = _run(poll_once(ctx))
    assert first == 0        # the post failed
    assert gw.posted == []

    second = _run(poll_once(ctx))
    assert second == 1       # retried, and this time it succeeds
    assert len(gw.posted) == 1


# ── a delivered DM must be MEASURABLE, not only a failed one ───────────────────────────────────
def test_a_delivered_dm_writes_an_audit_log_row(env, conn):
    """A row used to land in `job_runs` when delivery FAILED (`_record_undeliverable_once`), but
    nothing recorded that a DM was ever delivered, and `mark_notified` upserts in place — not even
    a send history survived. The success signal the doorbell exists for ("the steward never
    discovered a pending item by remembering to look") is unmeasurable from the
    pilot report without a positive record."""
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _park_capture(conn, MemoryEvidenceStore())

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
    assert args["item_kind"] == review.KIND_PARKED_CAPTURE
    assert args["item_id"] == str(item_id)


def test_an_undeliverable_notification_writes_no_audit_log_row(env, conn):
    """The audit row is for a DELIVERED DM only — an undeliverable outcome already has its own
    `job_runs` record; doubling it into `audit_log` too would just be two ledgers for the same
    non-event."""
    _write_stewards(env, '{"*": ["nobody-registered@example.com"]}')
    ctx = make_ctx(env, conn, gateway=FakeSlackGateway())
    _park_capture(conn, MemoryEvidenceStore())

    sent = _run(poll_once(ctx))

    assert sent == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_log")
        assert cur.fetchone()[0] == 0


def test_no_audit_writer_wired_does_not_crash_a_delivered_pass(env, conn):
    """`ctx.audit` is `None` for a caller that never wired one (an older test double, a process
    still starting up) — a delivered DM must still be sent even though nothing can be recorded
    about it."""
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    ctx.audit = None
    _park_capture(conn, MemoryEvidenceStore())

    sent = _run(poll_once(ctx))

    assert sent == 1
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_log")
        assert cur.fetchone()[0] == 0


# ── `DOORBELL_ITEM_LIMIT` must not be silently re-clamped down to `capture_queue.MAX_LIST_LIMIT`
# — the oldest parked items are exactly what a doorbell exists for ─────────────────────────────
def test_more_open_items_than_the_page_size_all_ring_including_the_oldest(env, conn):
    """`items_for_doorbell`'s own `limit=500` used to reach `capture_queue.query_submissions` in
    ONE call, which silently clamps any request above its own page ceiling
    (`MAX_LIST_LIMIT` = 200) — so with 201+ open `triage` rows, the OLDEST ones (sorted last,
    newest-first) never reached the doorbell at all. `_query_all_open_submissions` pages through
    instead; this seeds just past the old ceiling and checks the very first (oldest) row still
    rings."""
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    total = capture_queue.MAX_LIST_LIMIT + 3
    ids = [_park_capture(conn, MemoryEvidenceStore(), summary=f"item {i}") for i in range(total)]
    oldest_id = ids[0]

    sent = _run(poll_once(ctx))

    assert sent == total, "every open item should ring — none silently dropped by the page clamp"
    assert slack_store.last_notified_state(
        conn, item_kind=review.KIND_PARKED_CAPTURE, item_id=str(oldest_id),
        steward_email=STEWARD) is not None, \
        "the OLDEST parked item never rang — the query_submissions 200-row clamp bug"


def test_items_for_doorbell_logs_when_its_own_limit_is_still_not_enough(conn, caplog):
    """The "no silent caps" half, covered cheaply through `items_for_doorbell`'s own
    exposed `limit` rather than seeding past the real 200-row page size: a caller sizing `limit`
    generously (the doorbell's own 500) should still be able to tell, from the process log, when
    even that many pages were not enough."""
    for _ in range(3):
        _park_capture(conn, MemoryEvidenceStore())

    with caplog.at_level("WARNING", logger="stigmergy.server.review"):
        items = review.items_for_doorbell(conn, limit=2)

    assert len(items) == 2
    assert any("open-submission paging hit its own limit" in r.message for r in caplog.records)


# ── `ops/stewards.json` must not be re-fetched from git on every poll pass ─────────────────────
def test_load_stewards_is_cached_across_poll_passes_within_the_ttl(env, conn, monkeypatch):
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    _park_capture(conn, MemoryEvidenceStore())

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
    _park_capture(conn, MemoryEvidenceStore())

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


# ── no material excerpt for a capture the librarian has not yet looked at ──────────────────────
def test_the_triage_dm_never_carries_the_raw_material(env, conn):
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    _park_capture(conn, MemoryEvidenceStore(), summary="unsupported material, parked for a look")

    _run(poll_once(ctx))

    text = _block_text(gw.posted[0])
    assert "the raw material" not in text   # the evidence blob's own content, never quoted


def test_the_entity_proposal_dm_shows_only_the_name_never_the_agents_rationale(env, conn):
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    _park_capture(conn, MemoryEvidenceStore(),
                 situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)

    _run(poll_once(ctx))

    text = _block_text(gw.posted[0])
    assert "Globex Robotics" in text
    assert "the raw material" not in text
    assert "stigmergy-entities show" in text


# ── no knowledge repo configured: the doorbell is a no-op, not a crash ─────────────────────────
# ── remaining unit-level coverage for pure/mechanical helpers ────────────────────────────────────
# ── the state signature is monotonic, not a two-value alphabet ─────────────────────────────────
def test_state_signature_changes_on_attempts_even_when_status_returns_to_the_same_value():
    """The requeue-and-reprocess loop (steward clicks Requeue -> librarian reprocesses -> parks
    again in the SAME status, the ordinary "try again" outcome) must not produce the identical
    signature it started at, or the bell never rings again for that item. `attempts` is
    `capture_queue`'s own monotonic per-delivery counter, forwarded onto the item by
    `stigmergy.server.review._collect_open_items` — this is the pure unit-level proof of the
    mechanism; `test_requeue_and_reprocess_back_into_the_same_status_rings_a_second_time` below
    proves it end to end through the real queue primitives."""
    first = doorbell_module._state_signature(
        {"kind": "parked-capture", "status": "triage", "attempts": 1})
    second = doorbell_module._state_signature(
        {"kind": "parked-capture", "status": "triage", "attempts": 2})
    assert first != second


def test_state_signature_is_the_old_safe_shape_when_attempts_is_absent():
    """No regression for an item that does not (yet) carry `attempts` — the exact shape today's
    `review._collect_open_items` still produces."""
    sig = doorbell_module._state_signature({"kind": "parked-capture", "status": "triage"})
    assert sig == "triage"


# ── the doorbell's own fail-closed guard on a parked capture's summary ─────────────────────────
def test_a_parked_capture_summary_is_withheld_when_the_gate_has_not_run_yet():
    for status in (capture_schema.QUEUED, capture_schema.CLAIMED, capture_schema.FAILED):
        rendered = doorbell_module._summary_for_doorbell(
            {"status": status, "summary": "a secret the gate never scanned"})
        assert "a secret the gate never scanned" not in rendered
        assert rendered   # a real sentence, not silently blank


def test_a_parked_capture_summary_is_shown_for_the_ordinary_reportable_statuses():
    for status in (capture_schema.TRIAGE, capture_schema.NEEDS_INPUT):
        rendered = doorbell_module._summary_for_doorbell(
            {"status": status, "summary": "an ordinary, gate-cleared summary"})
        assert rendered == "an ordinary, gate-cleared summary"



def test_a_malformed_stewards_json_does_not_crash_the_pass(env, conn):
    import os
    path = os.path.join(env.repo, "ops", "stewards.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not valid json")
    support.commit_and_push(env.repo, "malformed stewards.json")
    ctx = make_ctx(env, conn, gateway=FakeSlackGateway())
    _park_capture(conn, MemoryEvidenceStore())

    assert _run(poll_once(ctx)) == 0   # must not raise


def test_run_doorbell_calls_poll_once_each_pass_and_stops_on_the_stop_event(env, conn, monkeypatch):
    calls = []

    async def fake_poll_once(ctx):
        calls.append(1)
        return 0

    monkeypatch.setattr(doorbell_module, "poll_once", fake_poll_once)
    ctx = make_ctx(env, conn, gateway=FakeSlackGateway())
    stop_event = asyncio.Event()

    async def _stop_after_first_pass():
        stop_event.set()

    async def _drive():
        task = asyncio.ensure_future(doorbell_module.run_doorbell(ctx, interval_s=0.01,
                                                                  stop_event=stop_event))
        await asyncio.sleep(0.05)
        stop_event.set()
        await task

    _run(_drive())
    assert calls   # the loop actually invoked poll_once at least once before stopping


def test_run_doorbell_survives_a_bad_pass_and_keeps_looping(env, conn, monkeypatch):
    """"One bad pass must never kill the process" (mirrors `poller.run_poller`'s own posture) —
    a `poll_once` that raises is logged and swallowed, and the loop keeps running rather than
    dying on the first failure."""
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


def test_no_stewards_source_at_all_is_recorded_not_swallowed(conn):
    """OLD BEHAVIOUR: this branch returned 0 in SILENCE — no log line, no `job_runs`
    row — so a deployment whose `app`/`slack` groups hold no checkout produced an item parked for
    twenty minutes with `steward_notifications` empty and nothing anywhere saying why. The
    empty-map branch three blocks below has always logged loudly AND recorded an undeliverable for
    a configuration that is barely worse; this module's own docstring promises that an
    undeliverable notification is recorded, never swallowed."""
    server_settings = Settings(identity=STEWARD, dsn=testdb.dsn(), embedder="fake", llm="fake")
    slack_settings = SlackSettings(app_token="xapp-test", bot_token="xoxb-test", team_id=TEAM_ID,
                                   channels_path="", server=server_settings)
    ctx = SlackContext(settings=slack_settings, gateway=FakeSlackGateway(), conn=conn,
                       embedder=build_embedder("fake"), evidence=MemoryEvidenceStore())
    _park_capture(conn, MemoryEvidenceStore())

    assert _run(poll_once(ctx)) == 0

    rows = _configuration_rows(conn)
    assert len(rows) == 1, "the reason must reach job_runs — an operator reads the database, not "\
                           "the scrollback of a process that has been polling for a week"
    assert "neither" in rows[0].lower()
    # Once per PROCESS, not once per pass: the doorbell polls every 10s and this fact cannot
    # change between them (the same cost argument the empty-map branch already makes).
    assert _run(poll_once(ctx)) == 0
    assert len(_configuration_rows(conn)) == 1


def test_a_baked_stewards_map_rings_the_doorbell_with_no_checkout_at_all(conn, tmp_path):
    """The benign twin, and the deployed shape this exists for: the `slack` group holds no
    checkout, so the ONLY thing that can make the bell ring is the map the deploy baked into the
    image. Nothing covered this road before — the suite proved the refusal and not the delivery."""
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
    item_id = _park_capture(conn, MemoryEvidenceStore())

    assert _run(poll_once(ctx)) == 1
    assert f"#{item_id}" in _block_text(gw.posted[0])
    assert _configuration_rows(conn) == [], "a working deployment records no configuration fault"


# ── issue #41 part 3: a decided item's card closes itself ──────────────────────────────────────
# A doorbell DM is a live control surface. Every button on it stayed clickable forever, including
# long after the item had been decided somewhere else — so the steward's own inbox kept offering
# actions that could only ever come back as a staleness refusal, and the DM never recorded what
# actually happened. The closing pass edits the message in place.
def _decide(conn, item_kind, item_id, *, verdict, actor, source, close):
    """A decision landing through some OTHER door: the disposition first, then the ledger row —
    the order every real door writes them in."""
    close(conn, item_id, actor)
    review.record_decision(conn, item_kind=item_kind, item_id=str(item_id), verdict=verdict,
                           actor=actor, source=source)


def _reject(conn, item_id, actor):
    from stigmergy.capture import dispositions
    dispositions.reject(conn, item_id, actor=actor, reason="not an entity after all")


def _requeue(conn, item_id, actor):
    from stigmergy.capture import dispositions
    dispositions.requeue(conn, item_id, actor=actor, note="back to the librarian")


def _ring_once(env, conn, *, situation=None):
    """The doorbell's ordinary first pass: one item, one DM, one recorded notification."""
    _write_stewards(env, f'{{"*": ["{STEWARD}"]}}')
    gw = FakeSlackGateway()
    gw.seed_email(STEWARD, STEWARD_SLACK_ID)
    ctx = make_ctx(env, conn, gateway=gw)
    item_id = _park_capture(conn, MemoryEvidenceStore(), situation=situation)
    assert _run(poll_once(ctx)) == 1
    return ctx, gw, item_id


def test_a_decided_entity_proposals_card_is_closed_exactly_once(env, conn):
    """OLD BEHAVIOUR: nothing ever edited a doorbell card. Approve/Reject stayed live in the DM
    after the proposal had been decided on the console, and a steward clicking them got a
    staleness refusal for an item their own inbox was still advertising as open.

    Exactly once is the load-bearing half: the closing pass runs every poll, so a pass that
    re-edited an already-closed card would rewrite the steward's DM every ten seconds forever.
    `mark_notified(state="closed:<verdict>")` is what makes the second pass a no-op, and
    `open_notifications` is what enforces it.
    """
    ctx, gw, item_id = _ring_once(env, conn,
                                  situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)
    _decide(conn, review.KIND_ENTITY_PROPOSAL, item_id, verdict="reject",
            actor="console-operator@example.com", source=review.SOURCE_ADMIN, close=_reject)

    assert _run(doorbell_module.close_decided_cards(ctx)) == 1
    assert _run(doorbell_module.close_decided_cards(ctx)) == 0, "the second pass must be a no-op"

    assert len(gw.updated) == 1
    assert len(gw.posted) == 1, "closing edits the card in place — it never posts a second one"
    updated = gw.updated[0]
    assert (updated.channel_id, updated.ts) == (gw.posted[0].channel_id, gw.posted[0].ts)


def test_the_closed_card_names_the_verdict_the_actor_and_the_door_and_drops_its_buttons(env, conn):
    """What the edited card has to say. The buttons are the point: a card that still renders
    `actions` after the item is decided has closed nothing."""
    ctx, gw, item_id = _ring_once(env, conn,
                                  situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)
    _decide(conn, review.KIND_ENTITY_PROPOSAL, item_id, verdict="reject",
            actor="console-operator@example.com", source=review.SOURCE_ADMIN, close=_reject)

    _run(doorbell_module.close_decided_cards(ctx))

    blocks = gw.updated[0].blocks
    assert not [b for b in blocks if b["type"] == "actions"], (
        "a closed card must carry no buttons — a stale click is exactly what this removes")
    rendered = " ".join(str(b) for b in blocks)
    assert "reject" in rendered
    assert "console-operator@example.com" in rendered
    assert "admin" in rendered
    assert f"#{item_id}" in rendered, "the card must still name the item it was about"
    assert f"#{item_id}" in gw.updated[0].text, "and so must the notification fallback"


def test_an_undecided_items_card_is_left_alone(env, conn):
    """The benign twin. The closing pass reads EVERY open card on every poll; a bug in its
    decision lookup would silently disarm the whole doorbell by closing cards nobody decided."""
    ctx, gw, _item_id = _ring_once(env, conn,
                                   situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)

    assert _run(doorbell_module.close_decided_cards(ctx)) == 0
    assert gw.updated == []


def test_a_decided_parked_captures_card_closes_too_on_the_requeue_verdict(env, conn):
    """The other item kind, and the verdict most likely to be missed: `requeue` returns the row to
    the queue rather than closing it, so the ITEM leaves the doorbell's inbox while its card stays
    in the DM. The card is closed on the ledger row, not on the queue state."""
    ctx, gw, item_id = _ring_once(env, conn)
    _decide(conn, review.KIND_PARKED_CAPTURE, item_id, verdict="requeue", actor=STEWARD,
            source=review.SOURCE_SLACK, close=_requeue)

    assert _run(doorbell_module.close_decided_cards(ctx)) == 1

    rendered = " ".join(str(b) for b in gw.updated[0].blocks)
    assert "requeue" in rendered and "slack" in rendered


def test_a_notification_recorded_before_the_card_pointer_existed_is_skipped(env, conn):
    """Every row `steward_notifications` already holds was written without a channel or a ts, and
    no API call can recover them. Those cards age out; they must never make the closing pass
    guess a channel, and must never block the rows that DO carry a pointer."""
    ctx, gw, item_id = _ring_once(env, conn,
                                  situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)
    with conn.cursor() as cur:      # exactly what a pre-change database holds
        cur.execute("UPDATE steward_notifications SET channel_id = '', message_ts = ''")
    _decide(conn, review.KIND_ENTITY_PROPOSAL, item_id, verdict="reject", actor=STEWARD,
            source=review.SOURCE_ADMIN, close=_reject)

    assert _run(doorbell_module.close_decided_cards(ctx)) == 0
    assert gw.updated == []


def test_a_failed_chat_update_leaves_the_card_open_for_the_next_pass(env, conn):
    """Same send-then-mark discipline `_notify_item` already keeps: a failed edit records nothing,
    so the next pass retries it rather than leaving a live-buttoned card marked closed."""
    ctx, gw, item_id = _ring_once(env, conn,
                                  situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)
    _decide(conn, review.KIND_ENTITY_PROPOSAL, item_id, verdict="reject", actor=STEWARD,
            source=review.SOURCE_ADMIN, close=_reject)
    gw.fail_update_count = 1

    assert _run(doorbell_module.close_decided_cards(ctx)) == 0
    assert _run(doorbell_module.close_decided_cards(ctx)) == 1, "the next pass retries it"
    assert len(gw.updated) == 1


def test_poll_once_closes_decided_cards_as_part_of_an_ordinary_pass(env, conn):
    """The wiring: nothing schedules `close_decided_cards` separately — it runs inside the pass
    the doorbell already makes, after the notify loop, so a deployment gets it with no new task."""
    ctx, gw, item_id = _ring_once(env, conn,
                                  situation=capture_schema.SITUATION_UNRESOLVED_ENTITY)
    _decide(conn, review.KIND_ENTITY_PROPOSAL, item_id, verdict="reject", actor=STEWARD,
            source=review.SOURCE_ADMIN, close=_reject)

    _run(poll_once(ctx))

    assert len(gw.updated) == 1


def test_a_re_parked_capture_gets_a_LIVE_card_again_not_one_the_old_decision_closes(env, conn):
    """The requeue loop meeting the closing pass, and the trap where they cross.

    `requeue` is a DECISION, so the ledger keeps a row for that item forever — but the item comes
    BACK: requeue's whole purpose is "try again", and parking again is the ordinary outcome. A
    closing pass that asked only "does a decision exist for this item" would close the fresh card
    the instant it was posted, in the very same pass, and the steward would never see an
    actionable card for a re-parked capture again — silently re-breaking exactly what
    `test_requeue_and_reprocess_back_into_the_same_status_rings_a_second_time` above protects.

    So the question is not "is there a decision" but "is there a decision NEWER than the
    notification it would close".

    Driven through the real queue primitives, like its sibling above, because the property only
    exists end to end.
    """
    from stigmergy.capture import dispositions
    from stigmergy.capture import queue as capture_queue

    ctx, gw, item_id = _ring_once(env, conn)
    _decide(conn, review.KIND_PARKED_CAPTURE, item_id, verdict="requeue", actor=STEWARD,
            source=review.SOURCE_SLACK, close=_requeue)
    _run(poll_once(ctx))                     # the first card is closed, correctly
    assert len(gw.updated) == 1

    # The librarian reprocesses and parks it right back in `triage`.
    claimed = capture_queue.claim_next(conn)
    assert claimed is not None and claimed["id"] == item_id
    capture_queue.finish(conn, item_id, status=capture_schema.TRIAGE,
                         expected_attempts=claimed["attempts"],
                         report={"summary": "parked again", "status": capture_schema.TRIAGE})

    assert _run(poll_once(ctx)) == 1, "the re-parked capture must ring the bell again"
    assert len(gw.posted) == 2
    assert len(gw.updated) == 1, (
        "the NEW card must still be live — the only decision on this item predates it")

    # and the loop closes properly the second time round too, on a decision that is genuinely new
    dispositions.reject(conn, item_id, actor=STEWARD, reason="enough")
    review.record_decision(conn, item_kind=review.KIND_PARKED_CAPTURE, item_id=str(item_id),
                           verdict="reject", actor=STEWARD, source=review.SOURCE_SLACK)
    _run(poll_once(ctx))
    assert len(gw.updated) == 2
    assert (gw.updated[1].channel_id, gw.updated[1].ts) == (gw.posted[1].channel_id,
                                                            gw.posted[1].ts)
