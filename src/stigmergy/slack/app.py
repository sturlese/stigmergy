"""`stigmergy-slack` — the process entry point: Bolt's ASYNC app, ONE process, Socket Mode. Every
listener acks its envelope FIRST (Slack's 3-second budget), then keeps running in the same
coroutine — Socket Mode already dispatches each incoming event as its own asyncio task, so nothing
here needs a second `create_task` to keep the ack cheap. The socket loop itself is never blocked,
because the listener returns control to the event loop at every `await`, starting with `ack()`.

This module is the ONLY place `slack_bolt`/`slack_sdk` are imported at module scope in this
package (besides `bolt_gateway.py`, which it uses) — every other module in `stigmergy.slack` takes a
`SlackGateway` as a plain argument and is fully testable without either dependency installed
mattering to its own logic.
"""
import argparse
import asyncio
import logging
import re
import sys
import uuid

from stigmergy.server.audit import AuditWriter, ensure_audit_table
from stigmergy.server.errors import StartupError, StigmergyServerError
from stigmergy.server.ratelimit import RateLimiter
from stigmergy.server.service import EmptyIndexError, evidence_plane, open_scoped_resources
from stigmergy.slack import capture, doorbell, mention, poller, render, replies, review
from stigmergy.slack.context import SlackContext
from stigmergy.slack.gateway import SlackApiError
from stigmergy.slack.identity import is_configured_workspace, is_ignorable_event, resolve_slack_identity
from stigmergy.slack.render import SHOW_IT_HERE_ACTION_ID
from stigmergy.slack.settings import SlackSettings, no_link_resolver
from stigmergy.slack.store import ensure_write_path_schema

log = logging.getLogger(__name__)


def build_context(settings: SlackSettings, *, gateway=None, conn=None) -> SlackContext:
    """Wire the process-wide resources ONCE — conn, embedder, rate limiter, audit writer,
    evidence — exactly the shape `transport_http.build_http_app` uses for the HTTP transport."""
    conn, embedder = open_scoped_resources(settings.server, conn)
    ensure_audit_table(conn)
    ensure_write_path_schema(conn)   # the capture-queue tables + this package's own, one DDL lock
    audit = AuditWriter(conn)
    rate_limiter = RateLimiter()
    evidence = evidence_plane.store_from_env()

    if gateway is None:
        from slack_sdk.web.async_client import AsyncWebClient

        from stigmergy.slack.bolt_gateway import BoltSlackGateway
        gateway = BoltSlackGateway(AsyncWebClient(token=settings.bot_token))

    # There is no browsable read surface for pages, so `no_link_resolver` is wired in as
    # CONFIGURATION and every citation renders with the "Show it here" affordance and no link. A
    # future browsable surface wires its own resolver in here, replacing the VALUE, never this
    # module or `render.py`'s contract.
    link_resolver = no_link_resolver

    return SlackContext(settings=settings, gateway=gateway, conn=conn, embedder=embedder,
                        rate_limiter=rate_limiter, audit=audit, evidence=evidence,
                        link_resolver=link_resolver)


def _event_team_id(event: dict, body: dict | None = None) -> str:
    """The EVENT'S OWN workspace — never `context["team_id"]`, which Bolt populates from the
    *authorization* (the workspace this app is INSTALLED in). For an event from an external user in
    a Slack Connect shared channel, the authorization's team_id equals the configured one BY
    CONSTRUCTION, so comparing against it would be a tautology that can never fail. `user_team` is
    the sender's own workspace; `team` is the fallback Slack uses when `user_team` is absent (a
    same-workspace event).

    **`body["team_id"]` is the third fallback, and it is not optional — leaving it out silently
    disables the entire 🧠 capture path.** A `reaction_added` payload is
    `{type, user, reaction, item, item_user, event_ts}` and carries **neither `user_team` nor
    `team`** — those fields exist on message events only. Without the envelope fallback this
    returns `""`, and the hardening in `identity.resolve_slack_identity` — absence fails CLOSED —
    classifies every reaction as `ForeignTeam`: silent by design, zero Slack traffic, not one log
    line. The gesture is dead and looks like nothing happening.

    No test catches that on its own, because every reaction test **constructs its own payload** and
    includes a team field the real event does not have.

    The envelope's `team_id` is the workspace the event was DELIVERED from — weaker than
    `user_team` (which names the sender), stronger than the authorization (which names us), and
    the only thing Slack offers for this event type. Order matters: sender first, then the
    event's own team, then the envelope. `context["team_id"]` is never consulted."""
    return (event.get("user_team") or event.get("team")
            or (body or {}).get("team_id") or "")


def _log_listener_failure(listener_name: str) -> None:
    """The top-level guard every listener has, matching `poller.run_poller`'s own "one bad pass must
    never kill the process" posture — the ack already happened (Bolt's 3-second budget is
    satisfied), so an unexpected exception here degrades to a logged incident rather than an
    uncaught traceback Bolt would otherwise log with no correlation id. Each handler's OWN internal
    guards (`SlackContext.post_or_log`, `mention.handle_mention`'s edit-into-server-error path)
    already cover the specific failures that CAN produce a user-facing reply; this is the
    last-resort backstop for everything else."""
    ref = uuid.uuid4().hex[:8]
    log.error("slack: %s failed unexpectedly (ref=%s)", listener_name, ref, exc_info=True)


def _is_dm(*, channel_type: str = "") -> bool:
    """The ONE shared fact-check for "is this a 1:1 DM with the bot" — never a channel-id prefix
    guess. `channel_type == "im"` is what Slack's own EVENT payloads carry (`message`,
    `app_mention`), and it is Slack asserting the channel's TYPE, not its label.

    **There is deliberately no `channel_name == "directmessage"` fallback.** INTERACTION payloads
    (block actions, slash commands) carry no `channel_type`, and the name Slack puts in their
    channel object for a DM is workspace-authored: any member who can create a public channel can
    call it `directmessage`, at which point `on_show_it_here` would read "yes, this is a private
    1:1" and post the page body with `chat_post_message` — publicly, at the clicking asker's own
    scope. Interaction payloads ask Slack what the channel IS instead (`_is_dm_channel` below),
    which is the pattern `capture.py` uses for the same question."""
    return channel_type == "im"


async def _is_dm_channel(ctx, channel_id: str) -> bool:
    """`_is_dm`'s sibling for INTERACTION payloads: the authoritative answer, from
    `conversations.info`'s `is_im` — the same fact `capture.is_public_channel` reads, asked the
    same way.

    **Fail-closed is `False` here**, and the direction is worth stating because it is the
    opposite of what "closed" usually means for a DM check: `True` makes the caller post with
    `chat_post_message` (everyone in the channel sees it) and `False` makes it post ephemerally
    (only the clicker does). So an API failure, an unknown channel, or a missing id degrades to
    the private answer — the reader still gets their page, nobody else does."""
    if not channel_id:
        return False
    try:
        meta = (await ctx.gateway.conversations_info(channel_id)).get("channel", {})
    except SlackApiError:
        log.error("slack: conversations.info failed for %s — treating it as not a DM", channel_id)
        return False
    return bool(meta.get("is_im"))


def build_bolt_app(ctx: SlackContext):
    """Register every listener. `context["bot_user_id"]` is populated by Bolt's own
    `auth.test`-backed middleware on every event — no separate lookup needed here.
    `context["team_id"]` is NOT used to identify an event's sender (see `_event_team_id`)."""
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=ctx.settings.bot_token)

    async def _resolve(event_team_id: str, slack_user_id: str):
        return await resolve_slack_identity(
            ctx.gateway, ctx.cache, identities_path=ctx.settings.server.identities_path,
            configured_team_id=ctx.settings.team_id, event_team_id=event_team_id,
            slack_user_id=slack_user_id)

    @app.event("app_mention")
    async def on_app_mention(event, context, ack, body):
        await ack()
        try:
            if is_ignorable_event(event, bot_user_id=context.get("bot_user_id")):
                return
            team_id = _event_team_id(event, body)
            channel_id = event["channel"]
            identity_result = await _resolve(team_id, event["user"])
            await mention.handle_mention(
                ctx, event_team_id=team_id, channel_id=channel_id,
                thread_ts=event.get("thread_ts") or event["ts"],
                is_dm=_is_dm(channel_type=event.get("channel_type", "")),
                asker_slack_user_id=event["user"],
                question=mention.strip_mention(event.get("text", "")),
                identity_result=identity_result)
        except Exception:
            _log_listener_failure("on_app_mention")

    @app.event("message")
    async def on_message(event, context, ack, body):
        await ack()
        try:
            if is_ignorable_event(event, bot_user_id=context.get("bot_user_id")):
                return
            team_id = _event_team_id(event, body)
            channel_id = event["channel"]
            channel_type = event.get("channel_type", "")
            thread_ts = event.get("thread_ts")

            if _is_dm(channel_type=channel_type) and thread_ts is None:
                # Any message in a DM with the bot is a question — treated exactly like a fresh
                # mention, rooted in its own ts.
                identity_result = await _resolve(team_id, event["user"])
                await mention.handle_mention(
                    ctx, event_team_id=team_id, channel_id=channel_id, thread_ts=event["ts"],
                    is_dm=True, asker_slack_user_id=event["user"], question=event.get("text", ""),
                    identity_result=identity_result)
                return

            if not thread_ts:
                return   # a fresh top-level channel message with no mention: not this bot's business

            # Slack fires BOTH `message` and `app_mention` for a channel mention — skip a message
            # `on_app_mention` above is already answering, so a mention-inside-a-thread is not
            # ALSO mistaken for an ask-back reply. Guarded on `bot_user_id` itself, never on the
            # composed `f"<@{bot_user_id}>"`: that string is ALWAYS truthy (a literal `"<@>"` is
            # still non-empty), so guarding on it never short-circuits and a message containing the
            # literal text `<@>` would wrongly skip reply handling.
            bot_user_id = context.get("bot_user_id", "")
            if bot_user_id and f"<@{bot_user_id}>" in (event.get("text") or ""):
                return

            await replies.handle_thread_message(
                ctx, team_id=team_id, channel_id=channel_id, thread_ts=thread_ts,
                slack_user_id=event["user"], text=event.get("text", ""))
        except Exception:
            _log_listener_failure("on_message")

    @app.event("reaction_added")
    async def on_reaction_added(event, context, ack, body):
        await ack()
        try:
            if event.get("reaction") != capture.BRAIN_REACTION:
                return
            if is_ignorable_event(event, bot_user_id=context.get("bot_user_id")):
                return
            team_id = _event_team_id(event, body)
            item = event.get("item", {}) or {}
            channel_id = item.get("channel", "")
            message_ts = item.get("ts", "")
            slack_user_id = event.get("user", "")

            # `is_configured_workspace` is the same fail-closed check `resolve_slack_identity` runs
            # internally, asked here BEFORE any identity work so an Ignored event (filtered above)
            # or a ForeignTeam one never gets the progress reaction either — genuinely zero Slack
            # traffic, reaction included, not merely zero chat traffic.
            #
            # It is the ONLY gate in front of the marker, and that is a deliberate trade, not an
            # oversight. The channel check (`is_public_channel`) and the identity outcome are
            # decided further down, inside `handle_reaction_added`, so a PRIVATE channel, an
            # unrecognized reactor and a transient identity failure all get an hourglass that is
            # then removed — where they previously produced no channel-visible artifact at all.
            # Gating on the channel too would mean a `conversations.info` round-trip before the
            # reaction, which is the exact wait this marker exists to remove. Nothing is queued on
            # those paths and the refusal is still the ephemeral only the reactor sees; what
            # changed is that the bot now briefly shows it noticed. Stated in
            # `docs/reference/slack.md` so it is a decision rather than a surprise.
            reacted = is_configured_workspace(team_id, ctx.settings.team_id)
            queued = False
            try:
                # The progress reaction fires as the FIRST thing inside this `try`, before
                # identity resolution (which needs neither a `users.info` call nor the cache) —
                # and INSIDE the `try`, not before it, so that even an unexpected failure in
                # `mark_in_progress` itself still reaches the `finally` below and gets cleaned up.
                if reacted:
                    await capture.mark_in_progress(ctx.gateway, channel_id=channel_id,
                                                   message_ts=message_ts)
                identity_result = await _resolve(team_id, slack_user_id)
                queued = await capture.handle_reaction_added(
                    ctx, reaction=event["reaction"], team_id=team_id, channel_id=channel_id,
                    message_ts=message_ts, slack_user_id=slack_user_id,
                    identity_result=identity_result)
            finally:
                # Every exit path clears the progress reaction — a refused/failed capture must
                # never leave a dangling ⏳ — a live failure of exactly this shape, for a message
                # edit rather than a reaction, is why every exit path clears it.
                if reacted:
                    await capture.finish_progress(ctx.gateway, channel_id=channel_id,
                                                  message_ts=message_ts, ok=queued)
        except Exception:
            _log_listener_failure("on_reaction_added")

    @app.event("reaction_removed")
    async def on_reaction_removed(event, context, ack):
        # Ignored outright — removing the 🧠 is not an undo the system can honour.
        await ack()

    @app.action(SHOW_IT_HERE_ACTION_ID)
    async def on_show_it_here(ack, body, action):
        await ack()
        try:
            channel_id = (body.get("channel") or {}).get("id", "")
            user_id = (body.get("user") or {}).get("id", "")
            event_team_id = (body.get("team") or {}).get("id", "")
            message = body.get("message") or {}
            thread_ts = message.get("thread_ts") or message.get("ts")
            is_dm = await _is_dm_channel(ctx, channel_id)
            await replies.handle_show_it_here(
                ctx, action_value=action.get("value", ""), clicking_slack_user_id=user_id,
                channel_id=channel_id, thread_ts=thread_ts, is_dm=is_dm,
                event_team_id=event_team_id)
        except Exception:
            _log_listener_failure("on_show_it_here")

    # Every button on a doorbell card (`review:<kind>:<verdict>` /
    # `review-modal:<kind>:<verdict>`, `stigmergy.slack.render`'s own convention) — one regex
    # matcher rather than one `@app.action(...)` per (kind, verdict) pair, since the set is closed
    # and named in exactly one place (`render.py`).
    @app.action(re.compile(r"^(review|review-modal):"))
    async def on_review_action(ack, body, action):
        await ack()
        try:
            channel_id = (body.get("channel") or {}).get("id", "")
            user_id = (body.get("user") or {}).get("id", "")
            event_team_id = (body.get("team") or {}).get("id", "")
            trigger_id = body.get("trigger_id", "")
            await review.handle_block_action(
                ctx, action_id=action.get("action_id", ""), value=action.get("value", ""),
                trigger_id=trigger_id, channel_id=channel_id, slack_user_id=user_id,
                event_team_id=event_team_id)
        except Exception:
            _log_listener_failure("on_review_action")

    @app.view(render.REVIEW_NOTE_MODAL_CALLBACK_ID)
    async def on_review_note_modal_submission(ack, body, view):
        await ack()
        try:
            state_values = ((view or {}).get("state") or {}).get("values") or {}
            # WHO is submitting comes from this event's OWN authoritative `body`, exactly like
            # `on_review_action`/`on_show_it_here` above — never from the modal's
            # `private_metadata`, which is a value this package itself wrote when the modal was
            # opened, not a fact Slack is asserting about who just clicked Submit.
            user_id = (body.get("user") or {}).get("id", "")
            event_team_id = (body.get("team") or {}).get("id", "")
            await review.handle_note_modal_submission(
                ctx, private_metadata=(view or {}).get("private_metadata", ""),
                state_values=state_values, slack_user_id=user_id, event_team_id=event_team_id)
        except Exception:
            _log_listener_failure("on_review_note_modal_submission")

    @app.view(render.ENTITY_MINT_MODAL_CALLBACK_ID)
    async def on_entity_mint_modal_submission(ack, body, view):
        await ack()
        try:
            state_values = ((view or {}).get("state") or {}).get("values") or {}
            # Same rule as `on_review_note_modal_submission` above: WHO submitted comes from this
            # event's OWN authoritative `body`, never round-tripped through `private_metadata`.
            user_id = (body.get("user") or {}).get("id", "")
            event_team_id = (body.get("team") or {}).get("id", "")
            await review.handle_entity_mint_modal_submission(
                ctx, private_metadata=(view or {}).get("private_metadata", ""),
                state_values=state_values, slack_user_id=user_id, event_team_id=event_team_id)
        except Exception:
            _log_listener_failure("on_entity_mint_modal_submission")

    return app


# Socket Mode has no leader election, and `fly deploy` creates two machines by default for a new
# process group — a comment in `fly.toml` plus a runbook line is prose, not a mechanism. A
# DIFFERENT key from `capture.schema._STARTUP_DDL_LOCK_KEY` (both are session-scoped advisory locks
# on the SAME database; sharing a key would make the two locks interfere).
_SINGLETON_LOCK_KEY = int.from_bytes(b"SYNSLCK", "big")


def acquire_singleton_lock(conn) -> None:
    """Refuse to start a SECOND `stigmergy-slack` process against this database — a mechanism, not an
    intention: `pg_try_advisory_lock`, never the blocking `pg_advisory_lock`, because a second
    machine must FAIL its startup immediately, not hang forever waiting for the first one to exit.
    Session-scoped, on the SAME connection this process already holds for everything else, so the
    lock dies the instant this connection closes — a crash, a deploy, `fly machine stop` — and the
    next machine to start acquires it automatically. No new dependency: the database is already
    serializing this the same way `capture.schema.startup_ddl_lock` serializes startup DDL, just
    with its own key."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s::bigint)", (_SINGLETON_LOCK_KEY,))
        acquired = cur.fetchone()[0]
    if not acquired:
        raise StartupError(
            "another stigmergy-slack process already holds the singleton lock — Socket Mode has no "
            "leader election, so a second machine in the `slack` process group would double-handle "
            "every event Slack delivers. Refusing to start; see the operator "
            "runbook (`fly scale count slack=1`, never raised).")


async def _async_main(settings: SlackSettings) -> None:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    ctx = build_context(settings)
    acquire_singleton_lock(ctx.conn)
    app = build_bolt_app(ctx)
    handler = AsyncSocketModeHandler(app, settings.app_token)
    poller_task = asyncio.create_task(poller.run_poller(ctx))
    # The steward doorbell rides the SAME process — no extra always-on process group — as a second
    # background task beside the poller above, never a second machine.
    doorbell_task = asyncio.create_task(doorbell.run_doorbell(ctx))
    try:
        await handler.start_async()
    finally:
        poller_task.cancel()
        doorbell_task.cancel()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="stigmergy-slack",
        description="Stigmergy Slack transport (Socket Mode): @brain to ask, react 🧠 to capture "
                    "a thread.")
    parser.add_argument("--repo", default=None,
                        help="knowledge-repo checkout (defaults --identities/--channels/"
                             "--entity-registry)")
    # `Settings.from_args` (the same builder stdio/HTTP use) reads `args.identity` unconditionally
    # — unused here (identity resolves PER SLACK EVENT, never per-process; same posture
    # `mcp_server.py`'s `--transport http` branch already takes), kept only so that shared builder
    # does not need an HTTP/Slack-specific carve-out.
    parser.add_argument("--identity", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--identities", default=None,
                        help="path to identities.json (default: <repo>/ops/identities.json)")
    parser.add_argument("--channels", default=None,
                        help="path to slack-channels.json (default: <repo>/ops/slack-channels.json)")
    parser.add_argument("--entity-registry", dest="entity_registry", default=None,
                        help="path to entity-registry.json for ask's entity-first resolution "
                            "(default: <repo>/ops/entity-registry.json)")
    parser.add_argument("--stewards", dest="stewards", default=None,
                        help="path to a baked stewards.json for a process with NO knowledge-repo "
                            "checkout (the deployed app/slack groups). The repo read at the base "
                            "commit wins wherever a checkout exists; this is the fallback that "
                            "keeps the doorbell ringing and review decisions decidable without "
                            "one (default: $STIGMERGY_STEWARDS_PATH)")
    parser.add_argument("--dsn", default=None, help="Postgres DSN (default: $STIGMERGY_INDEX_DSN)")
    parser.add_argument("--embedder", choices=["openai", "fake"], default=None)
    parser.add_argument("--answer-llm", dest="answer_llm", choices=["openai", "fake"], default=None)
    args = parser.parse_args(argv)

    try:
        settings = SlackSettings.from_args(args)
        if settings.server.llm not in ("openai", "fake"):
            raise StartupError(f"invalid ANSWER_LLM: {settings.server.llm!r} (use 'openai' or 'fake')")
        asyncio.run(_async_main(settings))
    except (StigmergyServerError, EmptyIndexError) as ex:
        print(f"stigmergy-slack: {ex}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
