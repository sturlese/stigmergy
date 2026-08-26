"""`@brain <question>` — ask, rendered for humans, plus the channel/DM split that follows up in
private when the asker's own scope is wider than the channel's. The envelope is acked in `app`
before anything here runs, so nothing here is on Slack's 3-second budget; `ASK_TIMEOUT_S` is a
client-side backstop that turns a genuinely stuck run into an honest "took too long" edit instead
of a placeholder left up forever.
"""
import asyncio
import logging
import re

from stigmergy.answer.service import TOTAL_ANSWER_TIMEOUT_S
from stigmergy.kernel.blocking import run_blocking
from stigmergy.server.errors import CapabilityUnavailableError, IdentityError, RateLimitError
from stigmergy.slack import channels, copy, render
from stigmergy.slack.context import run_with_connection, run_with_service, short_ref
from stigmergy.slack.gateway import SlackApiError
from stigmergy.slack.identity import IdentityResult, NoAccess, Resolved, TransientFailure
from stigmergy.slack.mrkdwn import escape_mrkdwn, to_mrkdwn

log = logging.getLogger(__name__)

ASK_TIMEOUT_S = TOTAL_ANSWER_TIMEOUT_S + 10

# Per-scope hit budget for the cheap retrieval-set comparison: wide enough that a wider scope's
# extra pages rarely sit past this rank, small enough that both calls stay one filtered query.
COMPARISON_MAX_RESULTS = 10

# Slack writes a mention as `<@U123>` or, when it carries a display label, `<@U123|name>`.
_LEADING_MENTION_RE = re.compile(r"^\s*<@\w+(?:\|[^>]*)?>")


def strip_mention(text: str, bot_user_id: str = "") -> str:
    """The BOT's own mention(s), stripped — what's left is the question. Targeted, not a blanket
    sweep: a mention INSIDE the question ("what did `<@U123>` agree to?") is part of it, and a
    blanket sweep deletes the question's subject. Without `bot_user_id`, only a LEADING mention is
    removed — the addressing token whatever its id.
    """
    s = text or ""
    if bot_user_id:
        return re.sub(rf"<@{re.escape(bot_user_id)}(?:\|[^>]*)?>", "", s).strip()
    return _LEADING_MENTION_RE.sub("", s).strip()


async def _edit_or_fallback(ctx, *, channel_id: str, ts: str, thread_ts: str, blocks: list,
                           text: str) -> None:
    """The placeholder is EDITED into the result: retry the edit once, then post the answer as a
    NEW message in the same thread. If every blocks-carrying attempt is refused (Slack's real
    `invalid_blocks` class), one more attempt sends `text` alone — a REAL rendering
    (`_answer_fallback_text`), never a stub. Only then give up and log: never drop an answer the
    system already paid for."""
    for attempt in range(2):
        try:
            await ctx.gateway.chat_update(channel_id, ts, text=text, blocks=blocks)
            return
        except SlackApiError as error:
            log.warning(
                "slack: chat.update failed (attempt %d) for %s/%s (%s)",
                attempt + 1,
                channel_id,
                ts,
                error.__class__.__name__,
            )
    try:
        await ctx.gateway.chat_post_message(channel_id, text=text, blocks=blocks,
                                            thread_ts=thread_ts)
        return
    except SlackApiError as error:
        log.warning("slack: chat.postMessage (with blocks) failed for %s/%s — degrading to a "
                   "text-only send (invalid_blocks or another blocks-shaped rejection) so the "
                   "answer still reaches the asker (%s)", channel_id, ts,
                   error.__class__.__name__)
    try:
        await ctx.gateway.chat_post_message(channel_id, text=text, thread_ts=thread_ts)
    except SlackApiError as error:
        log.error("slack: could not deliver the answer at all (edit, fallback post, and the "
                 "text-only degrade all failed) for %s/%s — log-only incident (%s)", channel_id, ts,
                  error.__class__.__name__)


async def _run_ask(service, question: str) -> dict:
    """Routed through `service.call_async` — the seam that writes `audit_log` and spends the
    `ask` rate-limit bucket, exactly like every other transport's `ask`. Calling
    `AnswerService(service).ask()` directly would bypass both. A `rate_limited=False` service
    keeps `audit`, so the fuller-answer DM is still audited while never touching the asker's
    budget."""
    from stigmergy.answer.service import AnswerService, audit_summary

    async def run():
        return await asyncio.wait_for(AnswerService(service).ask(question), timeout=ASK_TIMEOUT_S)

    # `summarize=audit_summary`: the same per-tool outcome summary the MCP `ask` tool writes, so
    # `audit_log.result` means one thing for `ask` regardless of transport.
    return await service.call_async("ask", {"question": question}, run, summarize=audit_summary)


def _run_ask_sync(service, question: str) -> dict:
    """Keep one answer's synchronous reads and audit on its worker-owned connection."""
    service.require_embedder()
    return asyncio.run(_run_ask(service, question))


def _answer_fallback_text(answer: dict) -> str:
    """The plain-text `text=` companion sent on every attempt — and the ONLY thing an asker sees
    on `_edit_or_fallback`'s blocks-free floor, so it is a REAL rendering, never a stub. The body
    goes through the same escape-then-`to_mrkdwn` order `render._render_markdown` uses (escaping
    after would corrupt the link syntax the conversion introduces), plus a compact Sources line:
    titles only, deduped by page, escaped. Text-only by construction, not a second Block Kit
    renderer."""
    if answer.get("refused"):
        return "I don't have that."
    body = to_mrkdwn(escape_mrkdwn(answer.get("answer_markdown") or ""))
    citations = answer.get("citations") or []
    if not citations:
        return body
    titles = list(dict.fromkeys(escape_mrkdwn(c.get("title") or c["path"]) for c in citations))
    return f"{body}\n\n{copy.degraded_sources_line(titles)}"


def _scope_could_be_wider(asker_audiences, channel_scope: set) -> bool:
    """A cheap pre-filter, pure arithmetic over audience LABEL SETS — never over a page; this is
    not `acl.visible()` and never decides what is shown. If the asker's scope is a SUBSET of the
    channel's, the truth table guarantees the asker can see nothing the channel scope does not
    already surface (a scoped audience's visible set is monotonic in its labels). An unrestricted
    asker (`None`) is always possibly wider: unrestricted also sees empty-acl pages no scoped
    audience ever can, so no label comparison settles it."""
    if asker_audiences is None:
        return True
    return not (set(asker_audiences) <= channel_scope)


async def handle_mention(ctx, *, event_team_id: str, channel_id: str, thread_ts: str,
                         is_dm: bool, asker_slack_user_id: str, question: str,
                         identity_result: IdentityResult) -> None:
    """The whole `@brain`/DM flow. `thread_ts` is the thread to answer IN; the answer is never
    posted to the channel itself. `identity_result` is already resolved by the caller."""
    if isinstance(identity_result, TransientFailure):
        # Via the decline seam: an unconditional channel post here would disclose one person's
        # access status to the whole channel — a public oracle over the identity registry.
        await ctx.decline(channel_id=channel_id, slack_user_id=asker_slack_user_id, is_dm=is_dm,
                          blocks=render.render_transient_identity_failure(),
                          text=copy.TRANSIENT_IDENTITY_FAILURE, thread_ts=thread_ts)
        return
    if isinstance(identity_result, NoAccess):
        await ctx.decline(channel_id=channel_id, slack_user_id=asker_slack_user_id, is_dm=is_dm,
                          blocks=render.render_no_access(is_dm=is_dm),
                          text=copy.no_access(is_dm=is_dm), thread_ts=thread_ts)
        return
    if not isinstance(identity_result, Resolved):
        return   # Ignored / ForeignTeam — zero Slack traffic

    email, asker_audiences = identity_result.email, identity_result.audiences
    # A channel answer is computed at the CHANNEL's audience scope (a channel is many readers); a
    # DM at the asker's own.
    if is_dm:
        effective_audiences = asker_audiences
    else:
        try:
            effective_audiences = await run_blocking(
                run_with_connection,
                ctx,
                lambda conn: channels.channel_audiences_live(
                    conn, ctx.settings.channels_path, channel_id
                ),
            )
        except IdentityError as error:
            # Fail-closed is right; total silence is not — the same honest server-error copy every
            # other unexpected failure gets, with a correlation ref.
            ref = short_ref()
            log.error(
                "slack: malformed slack-channels.json (ref=%s error=%s)",
                ref,
                error.__class__.__name__,
            )
            await ctx.post_or_log(
                ctx.gateway.chat_post_message(channel_id, blocks=render.render_server_error(ref),
                                              text=copy.server_error(ref), thread_ts=thread_ts),
                what=f"channel-audiences server-error in {channel_id}")
            return

    try:
        placeholder = await ctx.gateway.chat_post_message(
            channel_id, blocks=render.render_placeholder(), text=copy.PLACEHOLDER,
            thread_ts=thread_ts)
    except SlackApiError as error:
        # The FIRST post has no `ts` yet to edit a server-error copy into, so log-only is the
        # floor — a Slack outage here must degrade, not raise out of the listener.
        log.error(
            "slack: could not post the placeholder for %s/%s (%s)",
            channel_id,
            thread_ts,
            error.__class__.__name__,
        )
        return
    placeholder_ts = placeholder["ts"]

    try:
        answer = await run_blocking(
            run_with_service,
            ctx,
            email,
            effective_audiences,
            lambda service: _run_ask_sync(service, question),
        )
    except TimeoutError:
        await _edit_or_fallback(ctx, channel_id=channel_id, ts=placeholder_ts, thread_ts=thread_ts,
                                blocks=render.render_timeout(), text=copy.TIMEOUT)
        return
    except RateLimitError:
        await _edit_or_fallback(ctx, channel_id=channel_id, ts=placeholder_ts, thread_ts=thread_ts,
                                blocks=render.render_rate_limit(), text=copy.RATE_LIMIT)
        return
    except Exception as ex:
        ref = short_ref()
        level = log.error if not isinstance(ex, CapabilityUnavailableError) else log.warning
        level("slack ask failed (ref=%s error=%s)", ref, ex.__class__.__name__)
        await _edit_or_fallback(ctx, channel_id=channel_id, ts=placeholder_ts, thread_ts=thread_ts,
                                blocks=render.render_server_error(ref), text=copy.server_error(ref))
        return

    blocks = render.render_answer(answer, ctx.link_resolver,
                                  asker_slack_user_id=asker_slack_user_id,
                                  mint_token=ctx.mint_show_it_here_token)
    await _edit_or_fallback(ctx, channel_id=channel_id, ts=placeholder_ts, thread_ts=thread_ts,
                            blocks=blocks, text=_answer_fallback_text(answer))

    if not is_dm:
        await _maybe_dm_fuller_answer(ctx, email=email, asker_audiences=asker_audiences,
                                      asker_slack_user_id=asker_slack_user_id,
                                      channel_id=channel_id, question=question,
                                      effective_audiences=effective_audiences)


async def _maybe_dm_fuller_answer(ctx, *, email: str, asker_audiences, asker_slack_user_id: str,
                                  channel_id: str, question: str, effective_audiences: set) -> None:
    """Run `search` at both scopes and DM a fuller `ask` ONLY when the asker's own scope surfaces
    a page the channel's could not — the expensive `_run_ask` happens at most once, after a real
    difference."""
    if not _scope_could_be_wider(asker_audiences, effective_audiences):
        return
    # `rate_limited=False`: SYSTEM-initiated work must not spend the asker's own budget — an asker
    # for whom content was withheld would become observably likelier to hit the rate-limit message
    # on their next real question. `identity=email` is unchanged, so audit attribution is unaffected.
    # The channel answer has SHIPPED — nothing after this may escape uncaught into Bolt.
    try:
        channel_result = await run_blocking(
            run_with_service,
            ctx,
            email,
            effective_audiences,
            lambda service: service.search(question, max_results=COMPARISON_MAX_RESULTS),
            rate_limited=False,
        )
        asker_result = await run_blocking(
            run_with_service,
            ctx,
            email,
            asker_audiences,
            lambda service: service.search(question, max_results=COMPARISON_MAX_RESULTS),
            rate_limited=False,
        )
        channel_paths = {h["path"] for h in channel_result["hits"]}
        asker_paths = {h["path"] for h in asker_result["hits"]}
    except Exception as error:
        log.error(
            "slack: the DM comparison search() failed; the channel answer already shipped (%s)",
            error.__class__.__name__,
        )
        return
    if not (asker_paths - channel_paths):
        return   # nothing the asker's scope surfaces that the channel's could not

    try:
        fuller = await run_blocking(
            run_with_service,
            ctx,
            email,
            asker_audiences,
            lambda service: _run_ask_sync(service, question),
            rate_limited=False,
        )
    except Exception as error:
        log.error(
            "slack: the DM fuller-answer ask() failed; the channel answer already shipped (%s)",
            error.__class__.__name__,
        )
        return

    channel_name = ""
    try:
        info = await ctx.gateway.conversations_info(channel_id)
        channel_name = (info.get("channel") or {}).get("name", "")
    except SlackApiError:
        pass   # the DM reads fine without the channel name

    blocks = render.render_dm_fuller_answer(channel_name=channel_name, question=question,
                                            answer=fuller, link_resolver=ctx.link_resolver,
                                            asker_slack_user_id=asker_slack_user_id,
                                            mint_token=ctx.mint_show_it_here_token)
    try:
        # A user id as `channel_id` opens (or reuses) the 1:1 DM — Slack's own documented
        # `chat.postMessage` behavior, so no separate `conversations.open` call.
        await ctx.gateway.chat_post_message(asker_slack_user_id, blocks=blocks,
                                            text=_answer_fallback_text(fuller))
    except SlackApiError as error:
        log.error(
            "slack: could not DM the fuller answer to %s (%s)",
            asker_slack_user_id,
            error.__class__.__name__,
        )
