"""The "show it here" affordance — against real Postgres, offline Slack.
"""
import asyncio

import pytest

from stigmergy.server.errors import RateLimitError
from stigmergy.slack import copy, show_it_here
from stigmergy.slack.gateway import FakeSlackGateway
from tests.slack.conftest import TEAM_ID, build_context

pytestmark = pytest.mark.timeout(30)


def _run(coro):
    return asyncio.run(coro)


# ── the workspace check must use the EVENT's own team, not the configured one ────────────────
def test_show_it_here_from_a_foreign_workspace_is_refused_not_treated_as_the_configured_one(
        indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ANA", fixture.ANA)
    ctx = build_context(fixture, conn, gateway=gw)
    value = ctx.mint_show_it_here_token(fixture.ACME_PAGE, "U_ANA")

    _run(show_it_here.handle_show_it_here(ctx, action_value=value, clicking_slack_user_id="U_ANA",
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

    _run(show_it_here.handle_show_it_here(ctx, action_value=value, clicking_slack_user_id="U_ANA",
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

    _run(show_it_here.handle_show_it_here(ctx, action_value=value, clicking_slack_user_id="U_STEWARD",
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

    _run(show_it_here.handle_show_it_here(ctx, action_value=value, clicking_slack_user_id="U_ENG",
                                     channel_id="C1", thread_ts="1.1", is_dm=False,
                                     event_team_id=TEAM_ID))

    assert gw.ephemeral[0].blocks[0]["text"]["text"] == copy.show_it_here_refusal(fixture.ACME_PAGE)


# ── A8: the button's value is an opaque token, never the asker's email in cleartext ──────────────
def test_an_unknown_or_expired_show_it_here_token_is_declined_silently(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ANA", fixture.ANA)
    ctx = build_context(fixture, conn, gateway=gw)

    _run(show_it_here.handle_show_it_here(ctx, action_value="not-a-real-token",
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

    _run(show_it_here.handle_show_it_here(ctx, action_value=value, clicking_slack_user_id="U_ANA",
                                     channel_id="C1", thread_ts="1.1", is_dm=False,
                                     event_team_id=TEAM_ID))

    assert len(gw.ephemeral) == 1, "the clicker must be told something"
    assert gw.ephemeral[0].text == copy.server_error()
