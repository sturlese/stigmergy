"""Ask-back and the "show it here" affordance — against real Postgres, offline Slack.
"""
import asyncio

import pytest

from stigmergy.capture import queue
from stigmergy.capture import schema as capture_schema
from stigmergy.server.errors import RateLimitError
from stigmergy.slack import copy, replies
from stigmergy.slack.gateway import FakeSlackGateway
from stigmergy.slack.store import attach_submission, reserve
from tests.slack.conftest import TEAM_ID, build_context

pytestmark = pytest.mark.timeout(30)


def _run(coro):
    return asyncio.run(coro)


def _make_needs_input_submission(ctx, *, identity: str, channel_id: str, thread_ts: str) -> int:
    """A capture already `needs_input`, mapped to a Slack thread — built directly against the
    queue/schema primitives rather than through the whole librarian, since only the STATE and the
    mapping matter here."""
    service = ctx.build_service(identity, None)
    ack = service.submit("raw", f"about acme {thread_ts}")
    submission_id = ack["id"]
    claimed = queue.claim_next(ctx.conn)
    assert claimed["id"] == submission_id
    queue.finish(ctx.conn, submission_id, status=capture_schema.NEEDS_INPUT,
                expected_attempts=claimed["attempts"],
                error="needs_input — capture is parked on one question: your material seems to be "
                      "about \"Acme\", and the entity registry doesn't recognize that name.\n\n"
                      "Reply naming one of these exactly.\n\nReply with:\n"
                      f"  {capture_schema.reply_invocation(submission_id)}")
    reservation_id = reserve(ctx.conn, team_id=TEAM_ID, channel_id=channel_id, message_ts="1.1",
                             thread_ts=thread_ts, slack_user_id="U_ORIGINAL", submitted_by=identity)
    attach_submission(ctx.conn, reservation_id, submission_id)
    return submission_id


# ── the submitter's own reply is delivered ───────────────────────────────────────────────────────
def test_the_submitters_reply_is_delivered_and_confirmed(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ORIGINAL", fixture.ANA)
    ctx = build_context(fixture, conn, gateway=gw)
    submission_id = _make_needs_input_submission(ctx, identity=fixture.ANA, channel_id="C1",
                                                 thread_ts="5.1")

    _run(replies.handle_thread_message(ctx, team_id=TEAM_ID, channel_id="C1", thread_ts="5.1",
                                       slack_user_id="U_ORIGINAL", text="it's Acme Corp"))

    assert gw.posted[-1].text == copy.REPLY_DELIVERED
    trace = queue.get_submission_trace(conn, submission_id)
    assert trace["status"] == capture_schema.QUEUED
    # the row is back in `queued`, which is a WITHHELD state (the gate
    # has not looked at the answer yet) — `get_submission_trace`'s wire-facing `reply` is therefore
    # "" here by design, not a data-loss bug. Assert the reply actually PERSISTED by reading the
    # raw column directly, which is the fact this test exists to protect.
    assert trace["reply"] == ""
    assert trace["withheld_reason"] == capture_schema.WITHHELD_PENDING_NOTE
    with conn.cursor() as cur:
        cur.execute("SELECT reply FROM capture_queue WHERE id = %s", (submission_id,))
        assert cur.fetchone()[0] == "it's Acme Corp"


def test_a_second_reply_from_the_submitter_after_it_is_answered_gets_the_already_answered_copy(
        indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ORIGINAL", fixture.ANA)
    ctx = build_context(fixture, conn, gateway=gw)
    _make_needs_input_submission(ctx, identity=fixture.ANA, channel_id="C1", thread_ts="6.1")
    _run(replies.handle_thread_message(ctx, team_id=TEAM_ID, channel_id="C1", thread_ts="6.1",
                                       slack_user_id="U_ORIGINAL", text="it's Acme Corp"))
    gw.posted.clear()

    _run(replies.handle_thread_message(ctx, team_id=TEAM_ID, channel_id="C1", thread_ts="6.1",
                                       slack_user_id="U_ORIGINAL", text="wait, actually..."))

    assert len(gw.ephemeral) == 1
    assert gw.ephemeral[0].text == copy.REPLY_ALREADY_ANSWERED
    assert gw.posted == []   # nothing NEW posted to the thread for the second reply


# ── developer ruling 5: only the ORIGINAL SUBMITTER's reply counts ──────────────────────────────
def test_a_reply_from_someone_else_in_the_thread_is_ignored_entirely(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ORIGINAL", fixture.ANA)
    gw.seed_user("U_BYSTANDER", fixture.STEWARD)
    ctx = build_context(fixture, conn, gateway=gw)
    submission_id = _make_needs_input_submission(ctx, identity=fixture.ANA, channel_id="C1",
                                                 thread_ts="7.1")

    _run(replies.handle_thread_message(ctx, team_id=TEAM_ID, channel_id="C1", thread_ts="7.1",
                                       slack_user_id="U_BYSTANDER", text="I think it's Acme"))

    assert gw.posted == [] and gw.ephemeral == []   # no brain_reply, no error, no reaction
    trace = queue.get_submission_trace(conn, submission_id)
    assert trace["status"] == capture_schema.NEEDS_INPUT   # untouched


def test_an_ordinary_thread_with_no_capture_in_it_is_a_no_op(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    _run(replies.handle_thread_message(ctx, team_id=TEAM_ID, channel_id="C1", thread_ts="999.1",
                                       slack_user_id="U_ANYONE", text="just chatting"))
    assert gw.posted == [] and gw.ephemeral == []


# ── A1 leg 3: the workspace check must use the EVENT's own team, not the configured one ──────────
def test_a_foreign_team_reply_is_refused_not_treated_as_the_configured_workspace(indexed, clean_tables):
    """The old code hard-coded `event_team_id=ctx.settings.team_id` (the CONFIGURED value) inside
    `handle_thread_message` itself, so the workspace check there was `configured == configured` —
    a tautology that could never catch a genuinely foreign sender, even though the function
    already receives the event's own `team_id` as a parameter."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ORIGINAL", fixture.ANA)
    ctx = build_context(fixture, conn, gateway=gw)
    submission_id = _make_needs_input_submission(ctx, identity=fixture.ANA, channel_id="C1",
                                                 thread_ts="8.1")

    _run(replies.handle_thread_message(ctx, team_id="T_OTHER", channel_id="C1", thread_ts="8.1",
                                       slack_user_id="U_ORIGINAL", text="it's Acme Corp"))

    assert gw.posted == [] and gw.ephemeral == []   # a foreign-workspace event must be silent
    trace = queue.get_submission_trace(conn, submission_id)
    assert trace["status"] == capture_schema.NEEDS_INPUT   # untouched — the reply never landed


def test_show_it_here_from_a_foreign_workspace_is_refused_not_treated_as_the_configured_one(
        indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ANA", fixture.ANA)
    ctx = build_context(fixture, conn, gateway=gw)
    value = ctx.mint_show_it_here_token(fixture.ACME_PAGE, "U_ANA")

    _run(replies.handle_show_it_here(ctx, action_value=value, clicking_slack_user_id="U_ANA",
                                     channel_id="C1", thread_ts="1.1", is_dm=False,
                                     event_team_id="T_OTHER"))

    assert gw.ephemeral == [] and gw.posted == []


# ── "show it here" ────────────────────────────────────────────────────────────────────────────
def test_show_it_here_reads_under_the_askers_own_identity_and_posts_ephemerally(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ANA", fixture.ANA)
    ctx = build_context(fixture, conn, gateway=gw)
    value = ctx.mint_show_it_here_token(fixture.ACME_PAGE, "U_ANA")

    _run(replies.handle_show_it_here(ctx, action_value=value, clicking_slack_user_id="U_ANA",
                                     channel_id="C1", thread_ts="1.1", is_dm=False,
                                     event_team_id=TEAM_ID))

    assert len(gw.ephemeral) == 1
    assert gw.ephemeral[0].user_id == "U_ANA"
    text = gw.ephemeral[0].blocks[0]["text"]["text"]
    assert "Acme" in text
    assert "UNTRUSTED-DATA" not in text   # the fence is stripped for a human reader


def test_show_it_here_declines_silently_for_anyone_other_than_the_original_asker(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ANA", fixture.ANA)
    gw.seed_user("U_STEWARD", fixture.STEWARD)
    ctx = build_context(fixture, conn, gateway=gw)
    value = ctx.mint_show_it_here_token(fixture.ACME_PAGE, "U_ANA")   # minted for U_ANA, not U_STEWARD

    _run(replies.handle_show_it_here(ctx, action_value=value, clicking_slack_user_id="U_STEWARD",
                                     channel_id="C1", thread_ts="1.1", is_dm=False,
                                     event_team_id=TEAM_ID))

    assert gw.ephemeral == [] and gw.posted == []   # silently declined, nothing observable


def test_show_it_here_returns_the_same_refusal_read_page_gives_for_an_out_of_scope_page(
        indexed, clean_tables):
    """The SAME "unknown page" string `read_page` already returns — never a
    different sentence for out-of-scope vs nonexistent."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ENG", fixture.ENG)
    ctx = build_context(fixture, conn, gateway=gw)
    # ENG has no "finance" audience — the acme page is out of scope for them
    value = ctx.mint_show_it_here_token(fixture.ACME_PAGE, "U_ENG")

    _run(replies.handle_show_it_here(ctx, action_value=value, clicking_slack_user_id="U_ENG",
                                     channel_id="C1", thread_ts="1.1", is_dm=False,
                                     event_team_id=TEAM_ID))

    assert gw.ephemeral[0].blocks[0]["text"]["text"] == copy.show_it_here_refusal(fixture.ACME_PAGE)


# ── A8: the button's value is an opaque token, never the asker's email in cleartext ──────────────
def test_an_unknown_or_expired_show_it_here_token_is_declined_silently(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ANA", fixture.ANA)
    ctx = build_context(fixture, conn, gateway=gw)

    _run(replies.handle_show_it_here(ctx, action_value="not-a-real-token",
                                     clicking_slack_user_id="U_ANA", channel_id="C1",
                                     thread_ts="1.1", is_dm=False, event_team_id=TEAM_ID))

    assert gw.ephemeral == [] and gw.posted == []


def test_the_show_it_here_token_never_carries_the_askers_email_or_the_page_path_in_the_clear(
        indexed, clean_tables):
    """A8: the OLD button value was `json.dumps({"path": ..., "asker_email": ...})` — retrievable
    by any workspace member via `conversations.history`. The minted token must be opaque: neither
    the email nor the path readable from it."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    token = ctx.mint_show_it_here_token(fixture.ACME_PAGE, "U_ANA")

    assert fixture.ANA not in token
    assert fixture.ACME_PAGE not in token
    assert ctx.consume_show_it_here_token(token) == (fixture.ACME_PAGE, "U_ANA")


def test_a_show_it_here_token_expires_after_its_ttl(indexed, clean_tables, monkeypatch):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    clock = {"t": 0.0}
    monkeypatch.setattr("stigmergy.slack.context.time.monotonic", lambda: clock["t"])
    from stigmergy.slack.context import SHOW_IT_HERE_TOKEN_TTL_S

    token = ctx.mint_show_it_here_token(fixture.ACME_PAGE, "U_ANA")
    assert ctx.consume_show_it_here_token(token) == (fixture.ACME_PAGE, "U_ANA")

    clock["t"] = SHOW_IT_HERE_TOKEN_TTL_S + 1
    assert ctx.consume_show_it_here_token(token) is None


# ── C2-2: the token store is bounded, same shape as `identity.UsersInfoCache`'s own bound (A11) ──
def test_the_show_it_here_token_store_is_bounded_with_oldest_first_eviction_on_insert(
        indexed, clean_tables):
    """This dict is a process-lifetime store on a process meant to run for weeks — `UsersInfoCache`
    got a max size with oldest-first eviction on insert at A11; this store had a TTL but no size
    bound at all. `_show_it_here_max_tokens=2` is set directly (like `UsersInfoCache(max_entries=2)`
    in `test_identity.py`) so this test does not need to mint the real default (10,000) tokens."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    ctx._show_it_here_max_tokens = 2

    first = ctx.mint_show_it_here_token(fixture.ACME_PAGE, "U_FIRST")
    second = ctx.mint_show_it_here_token(fixture.ACME_PAGE, "U_SECOND")
    third = ctx.mint_show_it_here_token(fixture.ACME_PAGE, "U_THIRD")   # over the bound

    assert ctx.consume_show_it_here_token(first) is None   # the oldest was evicted
    assert ctx.consume_show_it_here_token(second) == (fixture.ACME_PAGE, "U_SECOND")
    assert ctx.consume_show_it_here_token(third) == (fixture.ACME_PAGE, "U_THIRD")
    assert len(ctx._show_it_here_tokens) == 2


# ── A5: SlackApiError guarded at the gateway-call boundary in the ask-back path too ──────────────
def test_reply_delivered_post_failing_does_not_raise_out_of_the_handler(indexed, clean_tables):
    """`handle_thread_message`'s posts used to be unguarded — a Slack outage on the
    REPLY_DELIVERED confirmation used to raise straight out of the handler even though the reply
    itself was already durably recorded."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ORIGINAL", fixture.ANA)
    ctx = build_context(fixture, conn, gateway=gw)
    submission_id = _make_needs_input_submission(ctx, identity=fixture.ANA, channel_id="C1",
                                                 thread_ts="9.1")
    gw.fail_post_count = 1

    _run(replies.handle_thread_message(ctx, team_id=TEAM_ID, channel_id="C1", thread_ts="9.1",
                                       slack_user_id="U_ORIGINAL", text="it's Acme Corp"))

    # no exception raised — the confirmation post failed and was swallowed, but the reply itself
    # (the durable, load-bearing half) was already recorded before this post was even attempted
    trace = queue.get_submission_trace(conn, submission_id)
    assert trace["status"] == capture_schema.QUEUED
    assert gw.posted == []


def test_already_answered_ephemeral_failing_does_not_raise_out_of_the_handler(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ORIGINAL", fixture.ANA)
    ctx = build_context(fixture, conn, gateway=gw)
    _make_needs_input_submission(ctx, identity=fixture.ANA, channel_id="C1", thread_ts="10.1")
    _run(replies.handle_thread_message(ctx, team_id=TEAM_ID, channel_id="C1", thread_ts="10.1",
                                       slack_user_id="U_ORIGINAL", text="it's Acme Corp"))
    gw.fail_ephemeral_count = 1

    _run(replies.handle_thread_message(ctx, team_id=TEAM_ID, channel_id="C1", thread_ts="10.1",
                                       slack_user_id="U_ORIGINAL", text="wait, actually..."))
    # no exception raised


# ── A7: two defects, one query ───────────────────────────────────────────────────────────────────
def test_the_submitters_ordinary_chatter_right_after_the_ack_is_silent_not_already_answered(
        indexed, clean_tables):
    """Defect 1: the OLD code treated ANY status other than `needs_input` as "already answered" —
    including `queued`, the row's state immediately after the capture ack, before any question was
    ever posted. That copy belongs only to a second reply to an ACTUAL ask."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ORIGINAL", fixture.ANA)
    ctx = build_context(fixture, conn, gateway=gw)
    service = ctx.build_service(fixture.ANA, None)
    ack = service.submit("raw", "material about acme 11.1")   # QUEUED — never reached needs_input
    reservation_id = reserve(ctx.conn, team_id=TEAM_ID, channel_id="C1", message_ts="1.1",
                             thread_ts="11.1", slack_user_id="U_ORIGINAL", submitted_by=fixture.ANA)
    attach_submission(ctx.conn, reservation_id, ack["id"])

    _run(replies.handle_thread_message(ctx, team_id=TEAM_ID, channel_id="C1", thread_ts="11.1",
                                       slack_user_id="U_ORIGINAL", text="oh, one more thing"))

    assert gw.posted == [] and gw.ephemeral == []   # silent — no question was ever asked


def test_an_older_needs_input_capture_is_found_even_when_a_newer_capture_shares_the_thread(
        indexed, clean_tables):
    """Defect 2: the OLD query's `LIMIT 1 ORDER BY created_at DESC` picked whichever row was
    newest in the thread, regardless of status or submitter — so ANA's genuinely open
    `needs_input` question was silently dropped once a newer, unrelated capture (STEWARD's) landed in
    the SAME thread afterward (legal: the UNIQUE key is per-message, not per-thread)."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ANA", fixture.ANA)
    gw.seed_user("U_STEWARD", fixture.STEWARD)
    ctx = build_context(fixture, conn, gateway=gw)
    older_id = _make_needs_input_submission(ctx, identity=fixture.ANA, channel_id="C1",
                                            thread_ts="12.1")

    steward_service = ctx.build_service(fixture.STEWARD, None)
    newer_ack = steward_service.submit("raw", "a second, unrelated capture in the same thread")
    newer_reservation = reserve(ctx.conn, team_id=TEAM_ID, channel_id="C1", message_ts="12.2",
                                thread_ts="12.1", slack_user_id="U_STEWARD", submitted_by=fixture.STEWARD)
    attach_submission(ctx.conn, newer_reservation, newer_ack["id"])

    _run(replies.handle_thread_message(ctx, team_id=TEAM_ID, channel_id="C1", thread_ts="12.1",
                                       slack_user_id="U_ANA", text="it's Acme Corp"))

    assert gw.posted[-1].text == copy.REPLY_DELIVERED
    trace = queue.get_submission_trace(conn, older_id)
    assert trace["status"] == capture_schema.QUEUED   # ANA's older needs_input capture WAS answered
    # `queued` withholds the reply on the read surface (the gate has not
    # looked at it yet) — read the raw column to prove it was persisted (see the sibling test above
    # for the full reasoning).
    assert trace["reply"] == ""
    with conn.cursor() as cur:
        cur.execute("SELECT reply FROM capture_queue WHERE id = %s", (older_id,))
        assert cur.fetchone()[0] == "it's Acme Corp"


def test_a_read_page_fault_tells_the_clicker_instead_of_going_silent(indexed, clean_tables,
                                                                     monkeypatch):
    """OLD BEHAVIOUR: the token's rightful owner clicked and got absolutely nothing.

    Silence is this handler's DELIBERATE answer to a wrong clicker, an expired token and an
    identity failure — which is exactly why a real fault must not borrow it. `read_page` goes
    through `BrainService._call`, which checks the rate limiter FIRST, so `RateLimitError` is an
    ordinary, user-reachable raise; unwrapped, it escaped to `app.py`'s listener backstop, which
    logs and posts nothing. An asker over their budget was told, by silence, that they were not the
    owner of their own answer — while `mention.py` renders the rate-limit copy for that same
    exception one surface over.
    """
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ANA", fixture.ANA)
    ctx = build_context(fixture, conn, gateway=gw)
    value = ctx.mint_show_it_here_token(fixture.OPEN_PAGE, "U_ANA")

    real_build_service = ctx.build_service

    def _service_that_faults(*a, **kw):
        service = real_build_service(*a, **kw)

        def _boom(_path):
            raise RateLimitError("slow down")

        service.read_page = _boom
        return service

    monkeypatch.setattr(ctx, "build_service", _service_that_faults)

    _run(replies.handle_show_it_here(ctx, action_value=value, clicking_slack_user_id="U_ANA",
                                     channel_id="C1", thread_ts="1.1", is_dm=False,
                                     event_team_id=TEAM_ID))

    assert len(gw.ephemeral) == 1, "the clicker must be told something"
    assert gw.ephemeral[0].text == copy.server_error()
