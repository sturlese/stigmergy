"""The 🧠 gesture — end to end against real Postgres, offline Slack."""
import asyncio
import dataclasses

import pytest

from stigmergy.slack import copy
from stigmergy.slack.capture import (
    DONE_REACTION,
    PROGRESS_REACTION,
    finish_progress,
    handle_reaction_added,
    mark_in_progress,
)
from stigmergy.slack.gateway import FakeSlackGateway, SlackApiError
from stigmergy.slack.identity import NoAccess, Resolved, TransientFailure, resolve_slack_identity
from tests.slack.conftest import (
    FINANCE_CHANNEL,
    TEAM_ID,
    UNLISTED_CHANNEL,
    build_context,
)

pytestmark = pytest.mark.timeout(30)


def _run(coro):
    return asyncio.run(coro)


def _seed_thread(gw: FakeSlackGateway, channel: str, root_ts: str):
    gw.seed_channel(channel, name="finance-team")
    gw.seed_user("U_ANA", "ana@example.com", display_name="Ana")
    gw.seed_thread(channel, root_ts, [
        {"ts": root_ts, "thread_ts": root_ts, "user": "U_ANA", "text": "we should track this"},
        {"ts": "100.2", "thread_ts": root_ts, "user": "U_ANA", "text": "decision: ship it Friday"},
    ])


def _fetch_row(conn, submission_id):
    with conn.cursor() as cur:
        cur.execute("SELECT payload->>'text', hints, submitted_by FROM capture_queue WHERE id = %s",
                   (submission_id,))
        return cur.fetchone()


def test_capture_queues_one_row_with_byte_identical_material_and_provenance_hints(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    _seed_thread(gw, FINANCE_CHANNEL, "100.1")
    ctx = build_context(fixture, conn, gateway=gw)
    identity = Resolved(email="ana@example.com", audiences=frozenset({"finance"}))

    _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL,
                               message_ts="100.1", slack_user_id="U_ANA", identity_result=identity))

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT payload->>'text', hints, submitted_by FROM capture_queue")
        material, hints, submitted_by = cur.fetchone()

    assert material == "we should track this\ndecision: ship it Friday"
    assert submitted_by == "ana@example.com"   # attributed to the REACTING user
    assert hints["client"]["source_client"] == "slack"
    assert hints["client"]["source_channel_id"] == FINANCE_CHANNEL
    assert "Ana" in hints["client"]["source_participants"]

    assert len(gw.posted) == 1
    assert "queued and attributed to Ana" in gw.posted[0].text
    for forbidden in ("saved", "searchable", "is filed", "has been filed", "was filed"):
        assert forbidden not in gw.posted[0].text.lower()


def test_a_single_message_not_in_a_thread_captures_just_that_message(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_channel(FINANCE_CHANNEL, name="finance-team")
    gw.seed_user("U_ANA", "ana@example.com", display_name="Ana")
    gw.seed_thread(FINANCE_CHANNEL, "200.1", [{"ts": "200.1", "user": "U_ANA", "text": "one-off note"}])
    ctx = build_context(fixture, conn, gateway=gw)
    identity = Resolved(email="ana@example.com", audiences=frozenset({"finance"}))

    _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL,
                               message_ts="200.1", slack_user_id="U_ANA", identity_result=identity))

    material, _, _ = _fetch_row(conn, _first_id(conn))
    assert material == "one-off note"


def _first_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM capture_queue ORDER BY id DESC LIMIT 1")
        return cur.fetchone()[0]


def test_redelivered_event_produces_exactly_one_queue_row(indexed, clean_tables):
    """Exactly-once, the redelivery half."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    _seed_thread(gw, FINANCE_CHANNEL, "300.1")
    ctx = build_context(fixture, conn, gateway=gw)
    identity = Resolved(email="ana@example.com", audiences=frozenset({"finance"}))
    args = dict(reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL, message_ts="300.1",
               slack_user_id="U_ANA", identity_result=identity)

    _run(handle_reaction_added(ctx, **args))
    _run(handle_reaction_added(ctx, **args))   # the identical event, delivered twice

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 1
    assert len(gw.posted) == 1   # only ONE ack, not two


def test_a_remove_then_re_add_by_the_same_person_also_produces_one_row(indexed, clean_tables):
    """Exactly-once, the remove-then-re-add half — modeled as the same reaction_added event
    firing again (Slack's own redelivery shape for this scenario: the dedup key is
    (team, channel, message_ts, slack_user_id), which is identical whether it is a genuine
    redelivery or a real remove-then-re-add)."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    _seed_thread(gw, FINANCE_CHANNEL, "301.1")
    ctx = build_context(fixture, conn, gateway=gw)
    identity = Resolved(email="ana@example.com", audiences=frozenset({"finance"}))
    args = dict(reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL, message_ts="301.1",
               slack_user_id="U_ANA", identity_result=identity)
    _run(handle_reaction_added(ctx, **args))
    _run(handle_reaction_added(ctx, **args))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 1


def test_two_different_people_reacting_to_the_same_message_both_get_queued(indexed, clean_tables):
    """Dedup is per (message, person), never per message alone."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    _seed_thread(gw, FINANCE_CHANNEL, "302.1")
    gw.seed_user("U_STEWARD", "steward@example.com", display_name="Sam")
    ctx = build_context(fixture, conn, gateway=gw)

    _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL,
                               message_ts="302.1", slack_user_id="U_ANA",
                               identity_result=Resolved(email="ana@example.com",
                                                        audiences=frozenset({"finance"}))))
    _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL,
                               message_ts="302.1", slack_user_id="U_STEWARD",
                               identity_result=Resolved(email="steward@example.com", audiences=None)))

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 2
    assert len(gw.posted) == 2


def test_a_reaction_other_than_brain_does_nothing(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    _run(handle_reaction_added(ctx, reaction="thumbsup", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL,
                               message_ts="1.1", slack_user_id="U_ANA",
                               identity_result=Resolved(email="ana@example.com",
                                                        audiences=frozenset({"finance"}))))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 0
    assert gw.posted == [] and gw.ephemeral == []


# ── identity failures: no BrainService constructed (verified by "no queue row") ─────────────────
def test_no_access_identity_gets_ephemeral_and_no_queue_row(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL,
                               message_ts="1.1", slack_user_id="U_STRANGER",
                               identity_result=NoAccess()))
    assert len(gw.ephemeral) == 1
    assert gw.ephemeral[0].text == copy.no_access(is_dm=False)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 0


def test_transient_identity_failure_gets_its_own_copy_not_the_no_access_one(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL,
                               message_ts="1.1", slack_user_id="U_FLAKY",
                               identity_result=TransientFailure("timeout")))
    assert gw.ephemeral[0].text == copy.TRANSIENT_IDENTITY_FAILURE
    assert gw.ephemeral[0].text != copy.no_access(is_dm=False)


# ── Ignored/ForeignTeam through the actual handler, not just the unit-level
# classification (`is_ignorable_event`/`resolve_slack_identity`) — zero Slack traffic, no queue row
def test_ignored_and_foreign_team_identities_queue_nothing_and_post_nothing(indexed, clean_tables):
    from stigmergy.slack.identity import ForeignTeam, Ignored
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    for identity in (Ignored("bot_message"), ForeignTeam("T_OTHER")):
        _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID,
                                   channel_id=FINANCE_CHANNEL, message_ts="1.1",
                                   slack_user_id="U_X", identity_result=identity))
    assert gw.posted == [] and gw.ephemeral == []
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 0


# ── private channel / group DM / DM refusal ───────────────────────────────────────────────────
@pytest.mark.parametrize("kwargs", [
    {"is_private": True}, {"is_im": True}, {"is_mpim": True},
])
def test_non_public_channel_refuses_and_queues_nothing(indexed, clean_tables, kwargs):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_channel("C_PRIVATE", **kwargs)
    gw.seed_user("U_ANA", "ana@example.com")
    ctx = build_context(fixture, conn, gateway=gw)
    _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID, channel_id="C_PRIVATE",
                               message_ts="1.1", slack_user_id="U_ANA",
                               identity_result=Resolved(email="ana@example.com",
                                                        audiences=frozenset({"finance"}))))
    assert gw.ephemeral[0].text == copy.PRIVATE_CHANNEL_REFUSAL
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 0


# ── a system error while queuing ───────────────────────────────────────────────────────────────
def test_a_system_error_while_queuing_tells_the_reactor_and_releases_the_dedup_key(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    _seed_thread(gw, FINANCE_CHANNEL, "400.1")
    ctx = build_context(fixture, conn, gateway=gw)
    broken_ctx = dataclasses.replace(ctx, evidence=None)   # BrainService.submit refuses without one
    identity = Resolved(email="ana@example.com", audiences=frozenset({"finance"}))

    _run(handle_reaction_added(broken_ctx, reaction="brain", team_id=TEAM_ID,
                               channel_id=FINANCE_CHANNEL, message_ts="400.1",
                               slack_user_id="U_ANA", identity_result=identity))

    assert gw.ephemeral[0].text == copy.CAPTURE_FAILED
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM slack_submissions")
        assert cur.fetchone()[0] == 0   # the reservation was released, not left dangling

    # the dedup key is free again — a real retry (not merely a redelivery) can now succeed
    _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL,
                               message_ts="400.1", slack_user_id="U_ANA", identity_result=identity))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 1


# ── reserve + submit + attach are ONE transaction ─────────────────────────────────────────────
def test_attach_submission_failure_rolls_back_the_whole_capture_not_just_leaving_it_orphaned(
        indexed, clean_tables, monkeypatch):
    """The OLD code called `attach_submission` OUTSIDE any `try`: if `submit()` succeeded and
    `attach_submission` then raised, a REAL `capture_queue` row was left committed with
    `slack_submissions.submission_id` stuck NULL — invisible to `find_thread_submission`/
    `due_for_report` forever (both filter `submission_id IS NOT NULL`), so ask-back was dead for
    that capture and every redelivery logged a false "duplicate" against a capture that was never
    actually retrievable. Wrapping reserve+submit+attach in one transaction means this failure
    rolls back EVERYTHING, so a genuine retry succeeds cleanly instead of being silently lost."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    _seed_thread(gw, FINANCE_CHANNEL, "500.1")
    ctx = build_context(fixture, conn, gateway=gw)
    identity = Resolved(email="ana@example.com", audiences=frozenset({"finance"}))

    from stigmergy.slack import capture as capture_mod

    def _boom(conn, reservation_id, submission_id):
        raise RuntimeError("simulated failure between submit() and attach_submission()")

    monkeypatch.setattr(capture_mod, "attach_submission", _boom)

    _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL,
                               message_ts="500.1", slack_user_id="U_ANA", identity_result=identity))

    assert gw.ephemeral[0].text == copy.CAPTURE_FAILED
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 0   # the queue row submit() wrote is GONE, not orphaned
        cur.execute("SELECT count(*) FROM slack_submissions")
        assert cur.fetchone()[0] == 0   # the reservation is gone too

    # a genuine retry (the dedup key was truly freed, not just marked) succeeds cleanly
    monkeypatch.undo()
    _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL,
                               message_ts="500.1", slack_user_id="U_ANA", identity_result=identity))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT submission_id FROM slack_submissions WHERE message_ts = '500.1'")
        assert cur.fetchone()[0] is not None   # properly attached this time


# ── SlackApiError guarded at the gateway-call boundary, never left unguarded ──────────────────
def test_a_conversations_info_failure_gets_the_server_error_copy_not_an_unhandled_exception(
        indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.fail_conversations_info.add(FINANCE_CHANNEL)
    ctx = build_context(fixture, conn, gateway=gw)
    identity = Resolved(email="ana@example.com", audiences=frozenset({"finance"}))

    _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL,
                               message_ts="600.1", slack_user_id="U_ANA", identity_result=identity))

    assert gw.ephemeral[0].text == copy.server_error()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 0


def test_the_final_capture_ack_post_failing_does_not_raise_out_of_the_handler(
        indexed, clean_tables):
    """Reproduced against the real code — only
    `mention._edit_or_fallback` and `poller.poll_once` were guarded; the capture ack's own
    `chat_post_message` was not. A Slack outage on this LAST call must degrade honestly (the
    capture is already safely queued) rather than raise past a caller with no top-level guard."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    _seed_thread(gw, FINANCE_CHANNEL, "700.1")
    gw.fail_post_count = 1
    ctx = build_context(fixture, conn, gateway=gw)
    identity = Resolved(email="ana@example.com", audiences=frozenset({"finance"}))

    _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL,
                               message_ts="700.1", slack_user_id="U_ANA", identity_result=identity))

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 1   # the capture itself is safely queued regardless
    assert gw.posted == []   # the ack post failed and was swallowed, not retried into a duplicate


def test_the_no_access_ephemeral_failing_does_not_raise_out_of_the_handler(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.fail_ephemeral_count = 1
    ctx = build_context(fixture, conn, gateway=gw)

    _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL,
                               message_ts="1.1", slack_user_id="U_STRANGER",
                               identity_result=NoAccess()))
    # no exception raised — the failed ephemeral is logged and swallowed, not left to crash the
    # listener


# ── a very long thread must not overflow MAX_HINT_CHARS forever ───────────────────────────────
def test_a_very_long_thread_does_not_overflow_max_hint_chars_forever(indexed, clean_tables):
    """`source_message_timestamps` is the comma-joined `ts` of every message in the thread —
    for a thread with enough messages that string alone exceeds `MAX_HINT_CHARS` (8192), and
    `normalize_hints` refuses any hint value over that limit outright. Without truncation, this
    capture would fail DETERMINISTICALLY on every retry, forever. Truncating this PROVENANCE
    metadata loses nothing the material itself carries."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_channel(FINANCE_CHANNEL, name="finance-team")
    gw.seed_user("U_ANA", "ana@example.com", display_name="Ana")
    messages = [{"ts": f"{100000 + i}.000001", "thread_ts": "100000.000001", "user": "U_ANA",
                "text": f"message {i}"} for i in range(700)]
    gw.seed_thread(FINANCE_CHANNEL, "100000.000001", messages)
    ctx = build_context(fixture, conn, gateway=gw)
    identity = Resolved(email="ana@example.com", audiences=frozenset({"finance"}))

    _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL,
                               message_ts="100000.000001", slack_user_id="U_ANA",
                               identity_result=identity))

    assert gw.ephemeral == []   # not CAPTURE_FAILED
    assert len(gw.posted) == 1   # the ordinary capture ack
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT hints FROM capture_queue")
        hints = cur.fetchone()[0]
    assert len(hints["client"]["source_message_timestamps"]) <= 8192


# ── the progress-reaction lifecycle ───────────────────────────────────────────────────────────
# `mark_in_progress`/`finish_progress` are exercised directly here (they are called from
# `app.on_reaction_added`, never from `handle_reaction_added` itself — see the module docstring),
# and end to end through the listener in `tests/slack/test_app_wiring.py`.
def test_mark_in_progress_adds_the_hourglass():
    gw = FakeSlackGateway()
    _run(mark_in_progress(gw, channel_id="C1", message_ts="1.1"))
    assert len(gw.reactions_added) == 1
    assert gw.reactions_added[0].name == PROGRESS_REACTION
    assert gw.reactions_added[0].channel_id == "C1" and gw.reactions_added[0].ts == "1.1"


def test_finish_progress_ok_true_removes_the_hourglass_and_adds_the_checkmark():
    gw = FakeSlackGateway()
    _run(finish_progress(gw, channel_id="C1", message_ts="1.1", ok=True))
    assert [r.name for r in gw.reactions_removed] == [PROGRESS_REACTION]
    assert [r.name for r in gw.reactions_added] == [DONE_REACTION]


def test_finish_progress_ok_false_only_removes_the_hourglass(indexed, clean_tables):
    """A refusal/failure/duplicate never gets the checkmark — just cleanup, no trace of an
    attempt that produced no capture."""
    gw = FakeSlackGateway()
    _run(finish_progress(gw, channel_id="C1", message_ts="1.1", ok=False))
    assert [r.name for r in gw.reactions_removed] == [PROGRESS_REACTION]
    assert gw.reactions_added == []


# ── the benign twin: the reactions API being down never breaks a capture ─────────────────────
def test_mark_in_progress_is_best_effort_when_the_reactions_api_is_down():
    gw = FakeSlackGateway()
    gw.fail_reactions_add_count = 99
    _run(mark_in_progress(gw, channel_id="C1", message_ts="1.1"))   # must not raise
    assert gw.reactions_added == []


def test_finish_progress_is_best_effort_when_the_reactions_api_is_down():
    gw = FakeSlackGateway()
    gw.fail_reactions_add_count = 99
    gw.fail_reactions_remove_count = 99
    _run(finish_progress(gw, channel_id="C1", message_ts="1.1", ok=True))   # must not raise
    assert gw.reactions_added == [] and gw.reactions_removed == []


def test_capture_still_succeeds_end_to_end_when_the_reactions_api_is_down(indexed, clean_tables):
    """The reaction is decorative; the capture is not. `handle_reaction_added` itself never calls
    the reactions API (that lifecycle lives in `app.on_reaction_added`), so this proves the OTHER
    half directly: nothing about a broken reactions API can reach this function at all."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    _seed_thread(gw, FINANCE_CHANNEL, "900.1")
    gw.fail_reactions_add_count = 99
    gw.fail_reactions_remove_count = 99
    ctx = build_context(fixture, conn, gateway=gw)
    identity = Resolved(email="ana@example.com", audiences=frozenset({"finance"}))

    outcome = _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID,
                                         channel_id=FINANCE_CHANNEL, message_ts="900.1",
                                         slack_user_id="U_ANA", identity_result=identity))

    assert outcome is True
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 1
    assert len(gw.posted) == 1


# ── handle_reaction_added's own return value: the finish_progress "ok" signal ────────────────
def test_handle_reaction_added_returns_true_only_on_the_genuine_success_path(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    _seed_thread(gw, FINANCE_CHANNEL, "901.1")
    ctx = build_context(fixture, conn, gateway=gw)
    identity = Resolved(email="ana@example.com", audiences=frozenset({"finance"}))
    outcome = _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID,
                                         channel_id=FINANCE_CHANNEL, message_ts="901.1",
                                         slack_user_id="U_ANA", identity_result=identity))
    assert outcome is True


def test_handle_reaction_added_returns_false_on_a_refusal_path(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_context(fixture, conn, gateway=gw)
    outcome = _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID,
                                         channel_id=FINANCE_CHANNEL, message_ts="1.1",
                                         slack_user_id="U_STRANGER", identity_result=NoAccess()))
    assert outcome is False


def test_handle_reaction_added_returns_false_on_a_duplicate(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    _seed_thread(gw, FINANCE_CHANNEL, "902.1")
    ctx = build_context(fixture, conn, gateway=gw)
    identity = Resolved(email="ana@example.com", audiences=frozenset({"finance"}))
    args = dict(reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL,
               message_ts="902.1", slack_user_id="U_ANA", identity_result=identity)
    assert _run(handle_reaction_added(ctx, **args)) is True
    assert _run(handle_reaction_added(ctx, **args)) is False   # the redelivered duplicate


# ── the display-name cache: a reactor already resolved by identity gets no second users.info ──
def test_the_reactors_display_name_is_served_from_the_identity_cache_not_a_second_users_info(
        indexed, clean_tables):
    """`identity.resolve_slack_identity`'s own `users.info` call (fetching the reactor's email)
    populates the SAME cache's display-name map as a side effect — `capture._display_name`'s
    later lookup, for the identical `(team_id, slack_user_id)`, must be a cache hit rather than a
    second round trip. This also covers the thread-participant fan-out: the reactor here is also
    the thread's only author, so a regression that dropped the cache-sharing would double the
    `users.info` count, not just fail to halve it."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    _seed_thread(gw, FINANCE_CHANNEL, "903.1")
    ctx = build_context(fixture, conn, gateway=gw)

    calls = {"n": 0}
    real_users_info = gw.users_info

    async def _counting_users_info(user_id):
        calls["n"] += 1
        return await real_users_info(user_id)

    gw.users_info = _counting_users_info

    identity_result = _run(resolve_slack_identity(
        gw, ctx.cache, identities_path=fixture.identities_path, configured_team_id=TEAM_ID,
        event_team_id=TEAM_ID, slack_user_id="U_ANA"))
    assert calls["n"] == 1   # the one call identity resolution itself needs

    _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL,
                               message_ts="903.1", slack_user_id="U_ANA",
                               identity_result=identity_result))

    assert calls["n"] == 1   # neither the participant fan-out nor the ack's own lookup called it again
    assert "Ana" in gw.posted[0].text


def test_a_thread_read_failure_tells_the_reactor_instead_of_vanishing(indexed, clean_tables,
                                                                      monkeypatch):
    """OLD BEHAVIOUR: the reactor saw the ⏳ appear, vanish, and nothing else — ever.

    `conversations.info` above was guarded and answered with an honest ephemeral; the two reads
    right after it were not. `conversations.replies` needs `channels:history` and is rate-limited,
    so a missing scope or a Tier-3 429 escaped `handle_reaction_added` entirely. `on_reaction_added`
    then ran its `finally` with `queued` still False (hourglass removed, no checkmark) and the outer
    handler logged the exception — no capture, no refusal, no acknowledgement of any kind.
    """
    conn, fixture = indexed
    gw = FakeSlackGateway()
    _seed_thread(gw, FINANCE_CHANNEL, "100.1")
    ctx = build_context(fixture, conn, gateway=gw)
    identity = Resolved(email="ana@example.com", audiences=frozenset({"finance"}))

    async def _boom(*_a, **_k):
        raise SlackApiError("conversations.replies failed (missing_scope)")

    monkeypatch.setattr(gw, "conversations_replies", _boom)

    ok = _run(handle_reaction_added(ctx, reaction="brain", team_id=TEAM_ID,
                                    channel_id=FINANCE_CHANNEL, message_ts="100.1",
                                    slack_user_id="U_ANA", identity_result=identity))

    assert ok is False
    assert len(gw.ephemeral) == 1, "the reactor must be told something"
    assert gw.ephemeral[0].text == copy.server_error()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 0   # nothing queued, and nothing half-queued


# ── the audience a 🧠 files at is the channel's, and the reactor must hold it ──────────────────
# ADR 045 D2. The door's rule is `acl.visible()` asked of the WRITER: you may file only what you
# could read afterwards. Asked BEFORE the dedup reservation, so a refusal reads as a refusal
# rather than as a capture that failed.

def _acl_of(conn, submission_id=None):
    with conn.cursor() as cur:
        cur.execute("SELECT acl FROM capture_queue ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    return row[0] if row else None


def test_a_capture_from_a_scoped_channel_is_filed_at_that_channels_groups(indexed, clean_tables):
    """The benign twin, and the case that carries the feature: a member of the channel captures,
    and the page set lands at the channel's audience."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    _seed_thread(gw, FINANCE_CHANNEL, "200.1")
    ctx = build_context(fixture, conn, gateway=gw)
    identity = Resolved(email="ana@example.com", audiences=frozenset({"finance"}))

    queued = _run(handle_reaction_added(
        ctx, reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL,
        message_ts="200.1", slack_user_id="U_ANA", identity_result=identity))

    assert queued is True
    assert _acl_of(conn) == ["finance"]


def test_a_capture_from_an_UNLISTED_channel_is_filed_open(indexed, clean_tables):
    """A channel not listed is public, and public is OPEN — stored as NULL, never `{}`. "This
    channel has no groups" is a fact about the channel; `{}` on a page means nobody."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    _seed_thread(gw, UNLISTED_CHANNEL, "201.1")
    ctx = build_context(fixture, conn, gateway=gw)
    identity = Resolved(email="ana@example.com", audiences=frozenset({"finance"}))

    queued = _run(handle_reaction_added(
        ctx, reaction="brain", team_id=TEAM_ID, channel_id=UNLISTED_CHANNEL,
        message_ts="201.1", slack_user_id="U_ANA", identity_result=identity))

    assert queued is True
    assert _acl_of(conn) is None


def test_a_reactor_outside_the_channels_groups_is_refused_and_nothing_is_queued(
        indexed, clean_tables):
    """The refusal. Somebody who cannot read the channel's material cannot capture it either —
    the page would be one they could not read back, which is what makes a mislabelled page
    unfixable. It must leave NO row: a reservation spent here would make the retry a duplicate."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    _seed_thread(gw, FINANCE_CHANNEL, "202.1")
    gw.seed_user("U_BOB", "bob@example.com", display_name="Bob")
    ctx = build_context(fixture, conn, gateway=gw)
    # Bob is authenticated and holds no group: he reads every open page and no other.
    identity = Resolved(email="bob@example.com", audiences=frozenset())

    queued = _run(handle_reaction_added(
        ctx, reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL,
        message_ts="202.1", slack_user_id="U_BOB", identity_result=identity))

    assert queued is False
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM slack_submissions")
        assert cur.fetchone()[0] == 0, "the dedup reservation must not be spent on a refusal"


def test_the_refusal_reaches_the_person_and_names_the_channel_not_the_groups(
        indexed, clean_tables):
    """It is ephemeral — a refusal about somebody's access is never posted to the channel — and it
    names the CHANNEL, which they are already in and can see. Which groups EXIST is not this
    message's to disclose."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    _seed_thread(gw, FINANCE_CHANNEL, "203.1")
    gw.seed_user("U_BOB", "bob@example.com", display_name="Bob")
    ctx = build_context(fixture, conn, gateway=gw)

    _run(handle_reaction_added(
        ctx, reaction="brain", team_id=TEAM_ID, channel_id=FINANCE_CHANNEL,
        message_ts="203.1", slack_user_id="U_BOB",
        identity_result=Resolved(email="bob@example.com", audiences=frozenset())))

    assert gw.ephemeral, "the reactor was told nothing at all"
    text = " ".join(str(e) for e in gw.ephemeral)
    assert "finance-team" in text, text
    assert "finance" not in text.replace("finance-team", ""), (
        "the refusal must not name the groups it checked against")
