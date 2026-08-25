"""`stigmergy-slack` — the process entry point: Bolt's ASYNC app, ONE process, Socket Mode. Every
listener acks its envelope FIRST (Slack's 3-second budget), then keeps running in the same
coroutine — Socket Mode dispatches each event as its own asyncio task. Only this module and
`bolt_gateway.py` import `slack_bolt`/`slack_sdk` at module scope; every other module takes a
`SlackGateway` as a plain argument.
"""
import argparse
import asyncio
import logging
import sys

from stigmergy.index import store
from stigmergy.server.audit import AuditWriter, ensure_audit_table
from stigmergy.server.errors import StartupError, StigmergyServerError
from stigmergy.server.ratelimit import RateLimiter
from stigmergy.server.service import EmptyIndexError, evidence_plane, open_scoped_resources
from stigmergy.slack import capture, mention, poller, show_it_here
from stigmergy.slack.context import SlackContext, short_ref
from stigmergy.slack.gateway import SlackApiError
from stigmergy.slack.identity import is_ignorable_event
from stigmergy.slack.render import SHOW_IT_HERE_ACTION_ID
from stigmergy.slack.settings import SlackSettings, no_link_resolver
from stigmergy.slack.store import ensure_write_path_schema

log = logging.getLogger(__name__)


def build_context(settings: SlackSettings, *, gateway=None, conn=None) -> SlackContext:
    """Wire the process-wide resources ONCE — conn, embedder, rate limiter, audit writer,
    evidence — exactly the shape `transport_http.build_http_app` uses for the HTTP transport."""
    conn, embedder = open_scoped_resources(settings.server, conn)
    store.bound_statements(conn)   # the adapter serves reads; it never runs an index rebuild
    ensure_audit_table(conn)
    ensure_write_path_schema(conn)   # the capture-queue tables + this package's own, one DDL lock
    audit = AuditWriter(conn)
    rate_limiter = RateLimiter()
    evidence = evidence_plane.store_from_env()

    if gateway is None:
        from slack_sdk.web.async_client import AsyncWebClient

        from stigmergy.slack.bolt_gateway import BoltSlackGateway
        gateway = BoltSlackGateway(AsyncWebClient(token=settings.bot_token))

    link_resolver = no_link_resolver

    return SlackContext(settings=settings, gateway=gateway, conn=conn, embedder=embedder,
                        rate_limiter=rate_limiter, audit=audit, evidence=evidence,
                        link_resolver=link_resolver)


def _event_team_id(event: dict, body: dict | None = None) -> str:
    """The EVENT'S OWN workspace — never `context["team_id"]`, which Bolt populates from the
    *authorization* (the workspace this app is INSTALLED in): for an external user in a Slack
    Connect shared channel that comparison is a tautology that can never fail. Order matters:
    `user_team` (the sender's own workspace), then `team`, then the envelope's `body["team_id"]`.

    **The envelope fallback is not optional**: a `reaction_added` payload carries neither
    `user_team` nor `team` (those exist on message events only), so without it this returns `""`,
    identity resolution fails closed to `ForeignTeam`, and the whole 🧠 capture path dies
    silently — zero Slack traffic, not one log line."""
    return (event.get("user_team") or event.get("team")
            or (body or {}).get("team_id") or "")


def _interaction_actor(body: dict) -> tuple[str, str, str]:
    """`(channel_id, user_id, event_team_id)` off an INTERACTION payload — a button click or a
    `view_submission`. All three come from THIS interaction's own authoritative `body`, which is
    Slack asserting who just acted and where; a modal's `private_metadata` carries WHAT the
    decision is about and is never a source for WHO is making it. `team` is the interaction's own
    workspace, never the installation's (see `_event_team_id`)."""
    return ((body.get("channel") or {}).get("id", ""),
            (body.get("user") or {}).get("id", ""),
            (body.get("team") or {}).get("id", ""))


def _log_listener_failure(listener_name: str, error: Exception) -> None:
    """Record a value-free incident after an acknowledged Slack event fails."""
    ref = short_ref()
    log.error(
        "slack: %s failed unexpectedly (ref=%s error=%s)",
        listener_name,
        ref,
        error.__class__.__name__,
    )


def _is_dm(*, channel_type: str = "") -> bool:
    """The ONE check for "is this a 1:1 DM with the bot": `channel_type == "im"`, Slack's own
    assertion on EVENT payloads. **Deliberately no `channel_name == "directmessage"` fallback**:
    interaction payloads carry no `channel_type`, and a DM's channel NAME is workspace-authored —
    any member can create a public channel called `directmessage`, at which point
    `on_show_it_here` would post the page body publicly. Interaction payloads ask Slack what the
    channel IS instead (`_is_dm_channel` below)."""
    return channel_type == "im"


async def _is_dm_channel(ctx, channel_id: str) -> bool:
    """`_is_dm`'s sibling for INTERACTION payloads: `conversations.info`'s `is_im`. **Fail-closed
    is `False`**: `True` posts publicly with `chat_post_message`, `False` posts ephemerally — so
    an API failure or unknown channel degrades to the private answer, and the reader still gets
    their page while nobody else does."""
    if not channel_id:
        return False
    try:
        meta = (await ctx.gateway.conversations_info(channel_id)).get("channel", {})
    except SlackApiError:
        log.error("slack: conversations.info failed for %s — treating it as not a DM", channel_id)
        return False
    return bool(meta.get("is_im"))


def build_bolt_app(ctx: SlackContext):
    """Register every listener. Bolt's own middleware populates `context["bot_user_id"]`;
    `context["team_id"]` is never used to identify an event's sender (see `_event_team_id`)."""
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=ctx.settings.bot_token)

    @app.event("app_mention")
    async def on_app_mention(event, context, ack, body):
        await ack()
        try:
            if is_ignorable_event(event, bot_user_id=context.get("bot_user_id")):
                return
            team_id = _event_team_id(event, body)
            channel_id = event["channel"]
            identity_result = await ctx.resolve_slack_identity(event_team_id=team_id,
                                                               slack_user_id=event["user"])
            await mention.handle_mention(
                ctx, event_team_id=team_id, channel_id=channel_id,
                thread_ts=event.get("thread_ts") or event["ts"],
                is_dm=_is_dm(channel_type=event.get("channel_type", "")),
                asker_slack_user_id=event["user"],
                question=mention.strip_mention(event.get("text", ""),
                                                   context.get("bot_user_id", "")),
                identity_result=identity_result)
        except Exception as error:
            _log_listener_failure("on_app_mention", error)

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
                identity_result = await ctx.resolve_slack_identity(event_team_id=team_id,
                                                                   slack_user_id=event["user"])
                await mention.handle_mention(
                    ctx, event_team_id=team_id, channel_id=channel_id, thread_ts=event["ts"],
                    is_dm=True, asker_slack_user_id=event["user"],
                    # Stripped exactly as `on_app_mention` does: people mention the bot in DMs
                    # anyway, and a raw `<@UBOT>` would make the same sentence a different question.
                    question=mention.strip_mention(event.get("text", ""),
                                                   context.get("bot_user_id", "")),
                    identity_result=identity_result)
                return

            # Anything else — a threaded message, a top-level channel message with no mention —
            # is not this bot's business: nothing a capture does ever waits on a reply in its
            # thread, so a thread message is ordinary conversation.
            return
        except Exception as error:
            _log_listener_failure("on_message", error)

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

            identity_result = await ctx.resolve_slack_identity(
                event_team_id=team_id, slack_user_id=slack_user_id)
            await capture.handle_reaction_added(
                ctx, reaction=event["reaction"], team_id=team_id, channel_id=channel_id,
                message_ts=message_ts, slack_user_id=slack_user_id,
                identity_result=identity_result)
        except Exception as error:
            _log_listener_failure("on_reaction_added", error)

    @app.action(SHOW_IT_HERE_ACTION_ID)
    async def on_show_it_here(ack, body, action):
        await ack()
        try:
            channel_id, user_id, event_team_id = _interaction_actor(body)
            message = body.get("message") or {}
            thread_ts = message.get("thread_ts") or message.get("ts")
            is_dm = await _is_dm_channel(ctx, channel_id)
            await show_it_here.handle_show_it_here(
                ctx, action_value=action.get("value", ""), clicking_slack_user_id=user_id,
                channel_id=channel_id, thread_ts=thread_ts, is_dm=is_dm,
                event_team_id=event_team_id)
        except Exception as error:
            _log_listener_failure("on_show_it_here", error)

    return app


# Socket Mode has no leader election, and `fly deploy` creates two machines by default for a new
# process group. A DIFFERENT key from `capture.schema._STARTUP_DDL_LOCK_KEY` — both are
# session-scoped advisory locks on the SAME database, and sharing a key would make them interfere.
_SINGLETON_LOCK_KEY = int.from_bytes(b"SYNSLCK", "big")


def acquire_singleton_lock(conn) -> None:
    """Refuse to start a SECOND `stigmergy-slack` process against this database.
    `pg_try_advisory_lock`, never the blocking variant — a second machine must FAIL its startup
    immediately, not hang. Session-scoped on the connection this process already holds, so the
    lock dies the instant the connection closes and the next machine to start acquires it."""
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
    try:
        await handler.start_async()
    finally:
        poller_task.cancel()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="stigmergy-slack",
        description="Stigmergy Slack transport (Socket Mode): @brain to ask, react 🧠 to capture "
                    "a thread.")
    parser.add_argument("--repo", default=None,
                        help="knowledge-repo checkout (defaults --identities/--channels/"
                             "--entity-registry)")
    # `Settings.from_args` (the shared builder) reads `args.identity` unconditionally — unused
    # here (identity resolves PER SLACK EVENT), declared only so the builder needs no carve-out.
    parser.add_argument("--identity", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--identities", default=None,
                        help="path to identities.json (default: <repo>/ops/identities.json)")
    parser.add_argument("--channels", default=None,
                        help="path to slack-channels.json (default: <repo>/ops/slack-channels.json)")
    parser.add_argument("--entity-registry", dest="entity_registry", default=None,
                        help="path to entity-registry.json for ask's entity-first resolution "
                            "(default: <repo>/ops/entity-registry.json)")
    parser.add_argument("--dsn", default=None, help="Postgres DSN (default: $STIGMERGY_INDEX_DSN)")
    parser.add_argument("--embedder", choices=["openrouter", "fake"], default=None)
    parser.add_argument(
        "--answer-llm",
        dest="answer_llm",
        choices=["openrouter", "fake"],
        default=None,
    )
    args = parser.parse_args(argv)

    try:
        settings = SlackSettings.from_args(args)
        if settings.server.llm not in ("openrouter", "fake"):
            raise StartupError(
                f"invalid ANSWER_LLM: {settings.server.llm!r} (use 'openrouter' or 'fake')"
            )
        asyncio.run(_async_main(settings))
    except (StigmergyServerError, EmptyIndexError) as ex:
        print(f"stigmergy-slack: {ex}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
