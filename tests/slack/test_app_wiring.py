"""`stigmergy.slack.app` — the Bolt wiring itself. Everything else in this package is tested through
plain functions with no Bolt/slack_sdk in the loop; this file is the one place that ISN'T true, and
it exists because a wrong listener-argument name or a removed `AsyncApp` constructor kwarg is
exactly the kind of error that only shows up by actually building the app. Having no live
workspace to test against does not excuse skipping this — it only excuses skipping the socket
connection itself.
"""
import asyncio

import pytest

from stigmergy.server.settings import Settings
from stigmergy.slack.app import _event_team_id, build_bolt_app, build_context, main
from stigmergy.slack.capture import DONE_REACTION, PROGRESS_REACTION
from stigmergy.slack.context import SlackContext
from stigmergy.slack.gateway import FakeSlackGateway
from stigmergy.slack.settings import SlackSettings, no_link_resolver
from tests.slack.conftest import FINANCE_CHANNEL, TEAM_ID, UNLISTED_CHANNEL
from tests.slack.conftest import build_context as build_slack_context

pytestmark = pytest.mark.timeout(30)


def _settings(**overrides):
    server_settings = Settings(identities_path="/tmp/does-not-matter.json",
                               dsn="postgresql://unused", embedder="fake", llm="fake")
    defaults = dict(app_token="xapp-test", bot_token="xoxb-test", team_id="T1", channels_path="",
                   server=server_settings)
    defaults.update(overrides)
    return SlackSettings(**defaults)


def _run(coro):
    return asyncio.run(coro)


async def _noop_ack():
    return None


def _listener(app, name):
    """The REAL, still-live listener function Bolt registered — `AsyncApp.event`/`.action`'s
    decorator returns the original function when called with a single one (`_register_listener`
    in `slack_bolt`), but `build_bolt_app` never exposes it by name; it is only reachable through
    the app's own listener registry. Driving it directly (rather than only proving `build_bolt_app`
    constructs without raising, `test_build_bolt_app_registers_every_listener_with_no_network`'s
    job) is what makes the WIRING-level behavior — not merely the classification one layer down —
    testable with no live Bolt dispatch."""
    for listener in app._async_listeners:
        if listener.ack_function.__name__ == name:
            return listener.ack_function
    raise AssertionError(f"no listener named {name!r} is registered on this app")


def test_build_bolt_app_registers_every_listener_with_no_network(indexed):
    """The regression this test exists for: `AsyncApp` does not accept a
    `token_verification_enabled` kwarg (it was removed/renamed upstream) — this raised a
    `TypeError` at import-adjacent construction time, before a single event was ever handled."""
    conn, fixture = indexed
    ctx = SlackContext(settings=_settings(), gateway=FakeSlackGateway(), conn=conn, embedder=None,
                       link_resolver=no_link_resolver)
    app = build_bolt_app(ctx)
    assert app is not None


def test_build_context_wires_the_process_wide_resources(indexed):
    conn, fixture = indexed
    settings = SlackSettings(
        app_token="xapp-test", bot_token="xoxb-test", team_id="T1",
        channels_path=fixture.identities_path,  # any real, readable path
        server=Settings(identities_path=fixture.identities_path,
                        dsn=None, embedder="fake", llm="fake"))
    ctx = build_context(settings, gateway=FakeSlackGateway(), conn=conn)
    assert ctx.embedder is not None
    assert ctx.audit is not None
    assert ctx.rate_limiter is not None
    assert ctx.evidence is not None


# ── Slack credentials come from the environment, never the repo ────────────────────────────────
def test_main_refuses_to_start_with_no_slack_app_token(monkeypatch):
    for var in ("SLACK_APP_TOKEN", "SLACK_BOT_TOKEN", "SLACK_TEAM_ID"):
        monkeypatch.delenv(var, raising=False)
    exit_code = main(["--dsn", "postgresql://unused"])
    assert exit_code == 2


def test_main_refuses_an_invalid_answer_llm(monkeypatch, tmp_path):
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_TEAM_ID", "T1")
    monkeypatch.setenv("ANSWER_LLM", "not-a-real-backend")
    exit_code = main(["--dsn", "postgresql://unused", "--identities", str(tmp_path / "i.json")])
    assert exit_code == 2


# ── `_event_team_id`'s `or` chain must treat an EMPTY-STRING `user_team` exactly like an
# ABSENT one, both falling through to `team` (and then to `""`) — the fail-closed guard
# (`resolve_slack_identity`'s own `if not event_team_id or ...`) depends entirely on this
# equivalence holding. Pinned directly on the function itself, for two distinct raw payload
# shapes, rather than only inferred from `test_identity.py`'s
# `test_an_absent_event_team_id_fails_closed_not_open` / `test_a_none_event_team_id_also_fails_
# closed`, which construct the already-derived string and never exercise this `or` chain at all.
def test_event_team_id_treats_an_empty_user_team_the_same_as_an_absent_one():
    user_team_present_but_empty = {"user_team": "", "team": ""}
    user_team_key_absent = {"team": ""}

    assert _event_team_id(user_team_present_but_empty) == ""
    assert _event_team_id(user_team_key_absent) == ""
    # both payload shapes fail CLOSED — neither is ever mistaken for a real configured workspace
    assert _event_team_id(user_team_present_but_empty) != TEAM_ID
    assert _event_team_id(user_team_key_absent) != TEAM_ID


# ── wiring-level: a foreign-team PAYLOAD produces zero traffic, driven through the REAL
# listener (not by injecting `ForeignTeam(...)` straight into a handler, which only proves the
# control at a boundary that was never broken). Each event below carries a `context["team_id"]`
# equal to the CONFIGURED workspace (Bolt's own `auth.test`-derived value, populated from the
# INSTALLATION — always the configured workspace by construction) alongside an event-level
# `user_team` naming a genuinely different workspace — the Slack Connect shared-channel shape,
# which is the threat here.
def test_app_mention_from_a_foreign_workspace_produces_zero_traffic(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_slack_context(fixture, conn, gateway=gw)
    app = build_bolt_app(ctx)
    listener = _listener(app, "on_app_mention")

    event = {"user": "U_STRANGER", "channel": FINANCE_CHANNEL, "text": "<@UBOT> hello",
             "ts": "1.1", "user_team": "T_OTHER", "team": TEAM_ID}
    context = {"bot_user_id": "UBOT", "team_id": TEAM_ID}   # the INSTALLED workspace — matches

    _run(listener(event=event, context=context, ack=_noop_ack, body={"team_id": TEAM_ID, "event": event}))

    assert gw.posted == [] and gw.ephemeral == [] and gw.updated == []


def test_message_dm_from_a_foreign_workspace_produces_zero_traffic(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_slack_context(fixture, conn, gateway=gw)
    app = build_bolt_app(ctx)
    listener = _listener(app, "on_message")

    event = {"user": "U_STRANGER", "channel": "D1", "channel_type": "im", "text": "hello",
             "ts": "1.1", "user_team": "T_OTHER", "team": TEAM_ID}
    context = {"bot_user_id": "UBOT", "team_id": TEAM_ID}

    _run(listener(event=event, context=context, ack=_noop_ack, body={"team_id": TEAM_ID, "event": event}))

    assert gw.posted == [] and gw.ephemeral == [] and gw.updated == []


def test_reaction_added_from_a_foreign_workspace_produces_zero_traffic_and_no_queue_row(
        indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_slack_context(fixture, conn, gateway=gw)
    app = build_bolt_app(ctx)
    listener = _listener(app, "on_reaction_added")

    event = {"reaction": "brain", "user": "U_STRANGER", "user_team": "T_OTHER", "team": TEAM_ID,
             "item": {"channel": FINANCE_CHANNEL, "ts": "1.1"}}
    context = {"bot_user_id": "UBOT", "team_id": TEAM_ID}

    _run(listener(event=event, context=context, ack=_noop_ack, body={"team_id": TEAM_ID, "event": event}))

    assert gw.posted == [] and gw.ephemeral == []
    # The progress reaction must not fire for a foreign workspace either — `is_configured_workspace`
    # gates it BEFORE identity resolution runs, using the same fail-closed comparison
    # `resolve_slack_identity` makes internally, so this is genuinely zero Slack traffic, reaction
    # included, not merely zero chat traffic.
    assert gw.reactions_added == [] and gw.reactions_removed == []
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 0


# ── the progress-reaction lifecycle, wired through the REAL listener ───────────────────────────
def test_reaction_added_success_upgrades_the_progress_reaction_to_a_done_mark(indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_channel(FINANCE_CHANNEL, name="finance-team")
    gw.seed_user("U_ANA", fixture.ANA, display_name="Ana")
    gw.seed_thread(FINANCE_CHANNEL, "10.1", [{"ts": "10.1", "user": "U_ANA", "text": "note"}])
    ctx = build_slack_context(fixture, conn, gateway=gw)
    app = build_bolt_app(ctx)
    listener = _listener(app, "on_reaction_added")

    event = {"reaction": "brain", "user": "U_ANA", "team": TEAM_ID,
             "item": {"channel": FINANCE_CHANNEL, "ts": "10.1"}}
    context = {"bot_user_id": "UBOT", "team_id": TEAM_ID}

    _run(listener(event=event, context=context, ack=_noop_ack, body={"team_id": TEAM_ID, "event": event}))

    assert [r.name for r in gw.reactions_added] == [PROGRESS_REACTION, DONE_REACTION]
    assert [r.name for r in gw.reactions_removed] == [PROGRESS_REACTION]
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 1


def test_reaction_added_refusal_clears_the_progress_reaction_without_a_done_mark(
        indexed, clean_tables):
    """`NoAccess` — a Slack user with no email `identities.json` recognizes — is a refusal, not a
    success: the marker is cleared, never upgraded to the checkmark."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_STRANGER", "stranger@example.com")
    ctx = build_slack_context(fixture, conn, gateway=gw)
    app = build_bolt_app(ctx)
    listener = _listener(app, "on_reaction_added")

    event = {"reaction": "brain", "user": "U_STRANGER", "team": TEAM_ID,
             "item": {"channel": FINANCE_CHANNEL, "ts": "1.1"}}
    context = {"bot_user_id": "UBOT", "team_id": TEAM_ID}

    _run(listener(event=event, context=context, ack=_noop_ack, body={"team_id": TEAM_ID, "event": event}))

    assert [r.name for r in gw.reactions_added] == [PROGRESS_REACTION]
    assert [r.name for r in gw.reactions_removed] == [PROGRESS_REACTION]
    assert len(gw.ephemeral) == 1   # the NoAccess ephemeral still fired normally
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 0


# ── the benign twin: a reactions API outage never breaks the capture it wraps ──────────────────
def test_reaction_added_capture_still_succeeds_when_the_reactions_api_is_down(
        indexed, clean_tables):
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_channel(FINANCE_CHANNEL, name="finance-team")
    gw.seed_user("U_ANA", fixture.ANA, display_name="Ana")
    gw.seed_thread(FINANCE_CHANNEL, "11.1", [{"ts": "11.1", "user": "U_ANA", "text": "note"}])
    gw.fail_reactions_add_count = 99
    gw.fail_reactions_remove_count = 99
    ctx = build_slack_context(fixture, conn, gateway=gw)
    app = build_bolt_app(ctx)
    listener = _listener(app, "on_reaction_added")

    event = {"reaction": "brain", "user": "U_ANA", "team": TEAM_ID,
             "item": {"channel": FINANCE_CHANNEL, "ts": "11.1"}}
    context = {"bot_user_id": "UBOT", "team_id": TEAM_ID}

    _run(listener(event=event, context=context, ack=_noop_ack,
                  body={"team_id": TEAM_ID, "event": event}))   # must not raise

    assert gw.reactions_added == [] and gw.reactions_removed == []   # every attempt failed, swallowed
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 1   # the capture itself is unaffected
    assert len(gw.posted) == 1


# ── is_dm derived from the payload, never a channel-id prefix guess or a hard-coded False ──────
def test_app_mention_derives_is_dm_from_the_event_instead_of_hard_coding_false(indexed, clean_tables):
    """If Slack delivers `app_mention` inside the bot DM, `channel_type` on the event says so —
    the old code always passed `is_dm=False`, which (per `handle_mention`) renders the CHANNEL
    no-access copy even inside a DM."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_slack_context(fixture, conn, gateway=gw)
    app = build_bolt_app(ctx)
    listener = _listener(app, "on_app_mention")

    event = {"user": "U_STRANGER", "channel": "D1", "channel_type": "im", "text": "<@UBOT> hi",
             "ts": "1.1", "team": TEAM_ID}
    context = {"bot_user_id": "UBOT", "team_id": TEAM_ID}

    _run(listener(event=event, context=context, ack=_noop_ack, body={"team_id": TEAM_ID, "event": event}))

    assert len(gw.posted) == 1
    from stigmergy.slack import copy
    assert gw.posted[0].text == copy.no_access(is_dm=True)   # the DM copy, not the channel one


def test_show_it_here_derives_is_dm_from_the_channel_type_not_a_channel_id_prefix(
        indexed, clean_tables):
    """Slack's interaction (block_actions) payload has no `channel_type` field at all — never a
    `D`-prefixed channel id to guess from either. The DM answer comes from `conversations.info`'s
    `is_im`, so this channel id deliberately does NOT start with "D" and carries no telling name."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ANA", fixture.ANA)
    gw.seed_channel("G_NOT_D_PREFIXED", is_im=True)
    ctx = build_slack_context(fixture, conn, gateway=gw)
    app = build_bolt_app(ctx)
    listener = _listener(app, "on_show_it_here")

    value = ctx.mint_show_it_here_token(fixture.ACME_PAGE, "U_ANA")
    body = {"channel": {"id": "G_NOT_D_PREFIXED"},
           "user": {"id": "U_ANA"}, "team": {"id": TEAM_ID},
           "message": {"ts": "1.1"}}
    action = {"value": value}

    _run(listener(ack=_noop_ack, body=body, action=action))

    assert len(gw.posted) == 1   # a DM posts a real message, never an ephemeral (handle_show_it_here)
    assert gw.ephemeral == []


# ── the sabotage twin — a channel NAME cannot buy a public post ────────────────────────────────
def test_a_public_channel_named_directmessage_does_not_make_show_it_here_post_publicly(
        indexed, clean_tables):
    """**The reproduction.** `_is_dm` used to answer this question with
    `channel_name == "directmessage"`, and an interaction payload's channel name is whatever the
    workspace called the channel. Any member who can create a public channel could name one
    `directmessage`, ask a question in it, click "Show it here" on their OWN answer, and have the
    bot `chat_post_message` up to 2800 characters of page body into the room — computed at the
    clicker's personal scope (`handle_show_it_here` builds the service from the CLICKER's
    identity) — a broadcast at the asker's own scope, into a room full of people who do not
    share it.

    The channel here is public and named `directmessage`; `is_im` is False, which is Slack's own
    answer about what it IS. The body must go out ephemerally — visible to the asker, to nobody
    else."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ANA", fixture.ANA)
    gw.seed_channel("C_PUBLIC_TRAP", name="directmessage")   # public, and lying about it
    ctx = build_slack_context(fixture, conn, gateway=gw)
    app = build_bolt_app(ctx)
    listener = _listener(app, "on_show_it_here")

    value = ctx.mint_show_it_here_token(fixture.ACME_PAGE, "U_ANA")
    body = {"channel": {"id": "C_PUBLIC_TRAP", "name": "directmessage"},
           "user": {"id": "U_ANA"}, "team": {"id": TEAM_ID},
           "message": {"ts": "1.1"}}

    _run(listener(ack=_noop_ack, body=body, action={"value": value}))

    assert gw.posted == []          # nothing was broadcast into the room
    assert len(gw.ephemeral) == 1   # the asker still got their page


def test_show_it_here_falls_back_to_ephemeral_when_conversations_info_fails(indexed, clean_tables):
    """The fail-closed direction, stated in `_is_dm_channel`'s docstring and pinned here: an API
    failure must degrade to the PRIVATE answer. A benign twin of the test above — same path, same
    outcome, reached by a fault instead of by an attack."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ANA", fixture.ANA)
    gw.seed_channel("D_REAL_DM", is_im=True)
    gw.fail_conversations_info.add("D_REAL_DM")
    ctx = build_slack_context(fixture, conn, gateway=gw)
    app = build_bolt_app(ctx)
    listener = _listener(app, "on_show_it_here")

    value = ctx.mint_show_it_here_token(fixture.ACME_PAGE, "U_ANA")
    body = {"channel": {"id": "D_REAL_DM"}, "user": {"id": "U_ANA"}, "team": {"id": TEAM_ID},
           "message": {"ts": "1.1"}}

    _run(listener(ack=_noop_ack, body=body, action={"value": value}))

    assert gw.posted == []
    assert len(gw.ephemeral) == 1


def test_a_threaded_message_is_ordinary_conversation_and_produces_no_traffic(indexed,
                                                                              clean_tables):
    """Nothing a capture does ever waits on a reply in its thread, so a message inside a thread —
    even one that looks like a bare `<@>` mention — is not this bot's business: no post, no
    ephemeral, no error."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ORIGINAL", fixture.ANA)
    ctx = build_slack_context(fixture, conn, gateway=gw)
    app = build_bolt_app(ctx)

    listener = _listener(app, "on_message")
    event = {"user": "U_ORIGINAL", "channel": "C1", "channel_type": "channel", "text": "<@>",
             "ts": "55.2", "thread_ts": "55.1", "team": TEAM_ID}
    context = {"team_id": TEAM_ID}   # no bot_user_id at all

    _run(listener(event=event, context=context, ack=_noop_ack, body={"team_id": TEAM_ID, "event": event}))

    assert gw.posted == [] and gw.ephemeral == []


# ── the listener-level try/except backstop ─────────────────────────────────────────────────────
def test_an_unexpected_exception_inside_a_listener_never_escapes_it(indexed, clean_tables,
                                                                     monkeypatch):
    """The poller loop already had "one bad pass must never kill the process"
    (`poller.run_poller`); the listeners were the asymmetric half of the same process. This proves
    a genuinely unexpected exception (not a `SlackApiError` — something the per-branch guards do
    not anticipate at all) is caught at the listener's own top level rather than propagating out
    to Bolt with no correlation id."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    ctx = build_slack_context(fixture, conn, gateway=gw)
    app = build_bolt_app(ctx)
    listener = _listener(app, "on_reaction_added")

    from stigmergy.slack import context as context_mod

    async def _boom(*_a, **_kw):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr(context_mod, "resolve_slack_identity", _boom)

    event = {"reaction": "brain", "user": "U_ANA", "team": TEAM_ID,
             "item": {"channel": FINANCE_CHANNEL, "ts": "1.1"}}
    context = {"bot_user_id": "UBOT", "team_id": TEAM_ID}

    _run(listener(event=event, context=context, ack=_noop_ack,
                  body={"team_id": TEAM_ID, "event": event}))   # must not raise


# ── the single-instance guarantee is a MECHANISM (an advisory lock), not prose ─────────────────
def test_acquire_singleton_lock_refuses_a_second_holder(monkeypatch):
    """Socket Mode has no leader election — a second stigmergy-slack process must be REFUSED at
    startup, not left to double-handle every event Slack delivers."""
    from stigmergy.server.errors import StartupError
    from stigmergy.slack.app import acquire_singleton_lock
    from tests import testdb

    conn1 = testdb.connect_or_skip("slack-singleton-1")
    conn2 = testdb.connect_or_skip("slack-singleton-2")
    try:
        acquire_singleton_lock(conn1)   # the first process: succeeds
        with pytest.raises(StartupError):
            acquire_singleton_lock(conn2)   # a second machine: refused immediately, never hangs
    finally:
        conn1.close()
        conn2.close()


def test_the_singleton_lock_releases_automatically_when_its_holders_connection_closes():
    """The whole point of a SESSION-scoped advisory lock over a runbook promise: failover is
    automatic. A crash, a deploy, or `fly machine stop` all close the connection, and the next
    machine to start must be able to acquire the lock with no manual step."""
    from stigmergy.slack.app import acquire_singleton_lock
    from tests import testdb

    conn1 = testdb.connect_or_skip("slack-singleton-3")
    acquire_singleton_lock(conn1)
    conn1.close()   # the crash/deploy/stop case: the session dies

    conn2 = testdb.connect_or_skip("slack-singleton-4")
    try:
        acquire_singleton_lock(conn2)   # must not raise — failover is automatic
    finally:
        conn2.close()


# ── the regression the live walk found, and no fabricated payload could ──────────────────────────
def test_a_real_reaction_added_payload_carries_no_team_field_and_is_still_handled(
        indexed, clean_tables):
    """The 🧠 gesture was DEAD in the real workspace and looked like nothing happening.

    Hardening the workspace check so an absent team fails CLOSED was correct, and it silently took
    the whole capture path with it, because a real `reaction_added` payload is
    `{type, user, reaction, item, item_user, event_ts}` and carries **neither `user_team` nor
    `team`**: those live on message events only. `_event_team_id` returned `""`, every reaction
    resolved to `ForeignTeam`, and `ForeignTeam` is silent by design — no reply, no log, no row.

    Every existing reaction test missed it by CONSTRUCTING an event with a team field the real one
    does not have. So this test's whole value is the payload below being the shape Slack actually
    sends: no `user_team`, no `team`, workspace only in the envelope."""
    conn, fixture = indexed
    gw = FakeSlackGateway()
    gw.seed_user("U_ANA", "ana@example.com", display_name="Ana")
    gw.seed_channel(UNLISTED_CHANNEL, name="unlisted")
    gw.seed_thread(UNLISTED_CHANNEL, "1.1", [
        {"ts": "1.1", "user": "U_ANA", "text": "drone AI in large enclosed spaces has real legs"}])
    ctx = build_slack_context(fixture, conn, gateway=gw)
    app = build_bolt_app(ctx)
    listener = _listener(app, "on_reaction_added")

    event = {"type": "reaction_added", "user": "U_ANA", "reaction": "brain",
             "item": {"type": "message", "channel": UNLISTED_CHANNEL, "ts": "1.1"},
             "item_user": "U_ANA", "event_ts": "1.2"}
    assert "user_team" not in event and "team" not in event, (
        "the point of this test is the REAL payload shape — adding a team field to make it pass "
        "would restore exactly the blind spot it exists to close")

    _run(listener(event=event, context={"bot_user_id": "UBOT", "team_id": TEAM_ID},
                  ack=_noop_ack, body={"team_id": TEAM_ID, "event": event}))

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue")
        assert cur.fetchone()[0] == 1, "the reaction must reach the capture path, not be dropped"
