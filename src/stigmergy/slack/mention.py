"""`@brain <question>` — ask, rendered for humans, plus the channel/DM split that follows up in
private when the asker's own scope is wider than the channel's.

The envelope is acked by Bolt, in `stigmergy.slack.app`, before anything here runs: the envelope is
acked immediately, then the work happens asynchronously, so nothing in this module is on Slack's
3-second budget. What IS on a budget here is `ASK_TIMEOUT_S`. The answering agent's own ceiling is
generous (`ANSWER_REQUEST_LIMIT` 6 requests / `ANSWER_TOOL_CALLS_LIMIT` 8 tool calls, plus one
corrective retry), so this is a client-side backstop that turns a genuinely stuck run into an
honest "took too long" edit instead of a `thinking…` placeholder left up forever.
"""
import asyncio
import logging
import re
import uuid

from stigmergy.server.errors import CapabilityUnavailableError, IdentityError, RateLimitError
from stigmergy.slack import channels, copy, render
from stigmergy.slack.gateway import SlackApiError
from stigmergy.slack.identity import IdentityResult, NoAccess, Resolved, TransientFailure
from stigmergy.slack.mrkdwn import escape_mrkdwn, to_mrkdwn

log = logging.getLogger(__name__)

ASK_TIMEOUT_S = 90

# How many hits the cheap retrieval-set comparison pulls per scope: wide enough that a genuinely
# wider scope's extra pages are unlikely to sit past this rank, small enough that both calls stay
# one filtered query each — a fraction of the cost of a second full `ask()` run.
COMPARISON_MAX_RESULTS = 10

# Slack writes a mention as `<@U123>` or, when it carries a display label, `<@U123|name>`.
_LEADING_MENTION_RE = re.compile(r"^\s*<@\w+(?:\|[^>]*)?>")


def strip_mention(text: str, bot_user_id: str = "") -> str:
    """The BOT's own mention(s), stripped — what's left is the question.

    Targeted, not a blanket sweep. The pattern used to be a bare `<@\\w+>` applied to the whole
    text, so a question that named a colleague — "@brain what did `<@U123>` agree to?" — reached
    the answering agent, retrieval and the audit row as "what did  agree to?", with its subject
    deleted. Only the addressing token is noise; a mention INSIDE the question is part of it.

    `bot_user_id` is what every caller already has (Bolt puts it in `context`). Without it, only a
    LEADING mention is removed: that is the addressing token whatever its id, and it keeps this
    function from guessing about mentions in the body of a sentence.
    """
    s = text or ""
    if bot_user_id:
        return re.sub(rf"<@{re.escape(bot_user_id)}(?:\|[^>]*)?>", "", s).strip()
    return _LEADING_MENTION_RE.sub("", s).strip()


def _short_id() -> str:
    """An opaque correlation token for the server-error copy: logged alongside the real exception so
    an operator can find it, safe to show a user (no path, no DSN, no traceback)."""
    return uuid.uuid4().hex[:8]


async def _edit_or_fallback(ctx, *, channel_id: str, ts: str, thread_ts: str, blocks: list,
                           text: str) -> None:
    """The placeholder is EDITED into the result. Retry the edit once; if that also fails, post the
    answer as a NEW message in the same thread. If EVERY blocks-carrying attempt above is refused
    — Slack's real `invalid_blocks` (an unsupported block, a nesting/length limit, a colliding
    `block_id`; `gateway._raise_if_invalid_blocks` mirrors the whole class, not only the collision
    that was hit in production) — one more attempt strips `blocks` entirely and sends `text` alone:
    `text` is `_answer_fallback_text`'s own REAL rendering of the answer, never a stub, so this
    floor still gets the asker their answer even when Block Kit itself is what keeps failing. Only
    if THAT also fails does this give up and log — never drop an answer the system already paid
    for."""
    for attempt in range(2):
        try:
            await ctx.gateway.chat_update(channel_id, ts, text=text, blocks=blocks)
            return
        except SlackApiError:
            log.warning("slack: chat.update failed (attempt %d) for %s/%s", attempt + 1,
                       channel_id, ts, exc_info=True)
    try:
        await ctx.gateway.chat_post_message(channel_id, text=text, blocks=blocks,
                                            thread_ts=thread_ts)
        return
    except SlackApiError:
        log.warning("slack: chat.postMessage (with blocks) failed for %s/%s — degrading to a "
                   "text-only send (invalid_blocks or another blocks-shaped rejection) so the "
                   "answer still reaches the asker", channel_id, ts, exc_info=True)
    try:
        await ctx.gateway.chat_post_message(channel_id, text=text, thread_ts=thread_ts)
    except SlackApiError:
        log.error("slack: could not deliver the answer at all (edit, fallback post, and the "
                 "text-only degrade all failed) for %s/%s — log-only incident", channel_id, ts,
                 exc_info=True)


async def _run_ask(service, question: str) -> dict:
    """Routed through `service.call_async`, the SAME seam `mcp_server.py`'s `ask` tool closure uses
    — this is what makes a Slack `ask` rate-limited and audited exactly like every other transport:
    `audit_log` rows from Slack are indistinguishable in kind from HTTP ones, and rate limits are
    per-person, not per-bot. Calling `AnswerService(service).ask(question)` directly here would
    bypass `call_async` entirely, and no Slack `ask` would write an `audit_log` row or spend the
    `ask` rate-limit bucket, no matter which `service` (rate-limited or not) it was handed.

    This composes with the fuller-answer DM for free: `_maybe_dm_fuller_answer` calls this with a
    service built via `ctx.build_service(..., rate_limited=False)`, which sets `rate_limiter=None`
    but keeps `audit` — so `call_async` here still writes that call's audit row while never
    touching the asker's own rate-limit budget (`RateLimiter.check` is only ever called when
    `rate_limiter is not None`, see `BrainService.call_async`)."""
    from stigmergy.answer.service import AnswerService, audit_summary

    async def run():
        return await asyncio.wait_for(AnswerService(service).ask(question), timeout=ASK_TIMEOUT_S)

    # `summarize=audit_summary`: the SAME per-tool outcome summary `mcp_server.py`'s `ask` tool
    # writes, so `audit_log.result` means one thing for `ask` regardless of which transport called
    # it — the same "one seam, one behavior" this function's own docstring already argues for
    # `call_async` itself.
    return await service.call_async("ask", {"question": question}, run, summarize=audit_summary)


def _answer_fallback_text(answer: dict) -> str:
    """The plain-text `text=` companion `_edit_or_fallback` sends alongside `blocks` on every
    attempt — including its own last, blocks-free one, which makes this the ONLY thing an asker
    ever sees when every blocks-carrying attempt is refused (`_edit_or_fallback`'s degrade leg,
    reached live). A refusal keeps its short existing shape; an actual answer needs a REAL rendering
    rather than a stub that says nothing once blocks are gone: the answer body through the same
    `escape_mrkdwn`-then-`to_mrkdwn` order `render._render_markdown` uses (escaping first protects
    a literal `&`/`<`/`>` in the MODEL-derived text; running it after would corrupt the `<url|text>`
    link syntax `to_mrkdwn` introduces), plus a compact Sources line naming what was cited — titles
    only, deduped by page like `render._citation_blocks`' buttons, and escaped the same way that
    module escapes a citation title. No link, no quote, no button: this lane is text-only by
    construction, not a second Block Kit renderer."""
    if answer.get("refused"):
        return "I don't have that."
    body = to_mrkdwn(escape_mrkdwn(answer.get("answer_markdown") or ""))
    citations = answer.get("citations") or []
    if not citations:
        return body
    titles = list(dict.fromkeys(escape_mrkdwn(c.get("title") or c["path"]) for c in citations))
    return f"{body}\n\n{copy.degraded_sources_line(titles)}"


def _scope_could_be_wider(asker_audiences, channel_scope: set) -> bool:
    """A cheap pre-filter, pure arithmetic over audience LABEL SETS (never over a page — this is
    not `acl.visible()`, and it never decides what is shown): if the asker's own scope is a SUBSET
    of the channel's, `acl.visible()`'s truth table guarantees the asker can never see a page the
    channel scope does not already surface — a scoped audience's visible-page set is monotonic in
    its label set (a superset of labels never sees fewer non-empty-acl pages, and an empty-acl
    page is equally invisible to any two merely-scoped audiences regardless of their labels). An
    unrestricted asker (`None`) is always treated as possibly wider: unrestricted also sees
    empty-acl ("nobody") pages no scoped audience ever can, so no label comparison settles it.
    This is what lets `_maybe_dm_fuller_answer` skip the retrieval-set comparison outright in the
    common case (the asker's own scope equals or narrows the channel's) without spending even the
    cheap two `search()` calls."""
    if asker_audiences is None:
        return True
    return not (set(asker_audiences) <= channel_scope)


async def handle_mention(ctx, *, event_team_id: str, channel_id: str, thread_ts: str,
                         is_dm: bool, asker_slack_user_id: str, question: str,
                         identity_result: IdentityResult) -> None:
    """The whole `@brain`/DM flow. `thread_ts` is the thread to answer IN — the mention's own ts
    for a fresh mention, or the thread's existing root for a mention inside one; the answer is
    never posted to the channel itself. `identity_result` is resolved by the caller
    (`stigmergy.slack.identity.resolve_slack_identity`, after `is_ignorable_event` and the workspace
    check)."""
    if isinstance(identity_result, TransientFailure):
        # Ephemeral to the asker in a channel, a real message only when this surface IS a DM. An
        # unconditional `chat_post_message` here would disclose an identity failure to the whole
        # channel — an unconsented disclosure of one person's access status, and a public oracle
        # over the identity registry's membership.
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
    if is_dm:
        effective_audiences = asker_audiences
    else:
        try:
            effective_audiences = channels.channel_audiences(ctx.settings.channels_path, channel_id)
        except IdentityError:
            # A malformed `ops/slack-channels.json` must not propagate straight out of here, BEFORE
            # any reply is posted: fail-closed is right, but total silence (and a Bolt-logged trace
            # nobody sees) is not. Caught, and given the same honest server-error copy every other
            # unexpected failure gets, with a correlation ref.
            ref = _short_id()
            log.error("slack: malformed slack-channels.json (ref=%s)", ref, exc_info=True)
            await ctx.post_or_log(
                ctx.gateway.chat_post_message(channel_id, blocks=render.render_server_error(ref),
                                              text=copy.server_error(ref), thread_ts=thread_ts),
                what=f"channel-audiences server-error in {channel_id}")
            return

    try:
        placeholder = await ctx.gateway.chat_post_message(
            channel_id, blocks=render.render_placeholder(), text=copy.PLACEHOLDER,
            thread_ts=thread_ts)
    except SlackApiError:
        # The placeholder's own FIRST post needs its own guard: a Slack outage here would otherwise
        # raise straight out of the listener instead of degrading honestly. There is no `ts` yet to
        # edit into a server-error copy (the whole reason `_edit_or_fallback` exists), so log-only
        # is the correct floor, matching the poller's own "one bad pass must never kill the
        # process" posture.
        log.error("slack: could not post the placeholder for %s/%s", channel_id, thread_ts,
                 exc_info=True)
        return
    placeholder_ts = placeholder["ts"]

    service = ctx.build_service(email, effective_audiences)
    try:
        service.require_embedder()
        answer = await _run_ask(service, question)
    except TimeoutError:
        await _edit_or_fallback(ctx, channel_id=channel_id, ts=placeholder_ts, thread_ts=thread_ts,
                                blocks=render.render_timeout(), text=copy.TIMEOUT)
        return
    except RateLimitError:
        await _edit_or_fallback(ctx, channel_id=channel_id, ts=placeholder_ts, thread_ts=thread_ts,
                                blocks=render.render_rate_limit(), text=copy.RATE_LIMIT)
        return
    except Exception as ex:
        ref = _short_id()
        level = log.error if not isinstance(ex, CapabilityUnavailableError) else log.warning
        level("slack ask failed (ref=%s)", ref, exc_info=True)
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
    """The cheap comparison: run `search` at both scopes and DM the asker a fuller `ask` ONLY when
    their own scope surfaces a page the channel's could not. `_run_ask` — the expensive call — is
    reached at most once here, and only after the comparison below finds a real difference."""
    if not _scope_could_be_wider(asker_audiences, effective_audiences):
        return
    # `rate_limited=False` — this whole comparison, and the fuller `ask()` it may trigger, is
    # SYSTEM-initiated work the asker did not request. Spending their own budget on it would make
    # an asker for whom content was withheld measurably likelier to hit the public rate-limit
    # message on their NEXT real question — an observable difference between an asker who has
    # wider scope and one who does not. `identity=email` is unchanged, so audit attribution is
    # unaffected.
    channel_service = ctx.build_service(email, effective_audiences, rate_limited=False)
    asker_service = ctx.build_service(email, asker_audiences, rate_limited=False)
    # Wrapped the same way the DM `ask()` below is — the channel answer has SHIPPED by the time
    # this runs, and nothing after it may escape uncaught into Bolt.
    try:
        channel_paths = {h["path"] for h in
                         channel_service.search(question, max_results=COMPARISON_MAX_RESULTS)["hits"]}
        asker_paths = {h["path"] for h in
                      asker_service.search(question, max_results=COMPARISON_MAX_RESULTS)["hits"]}
    except Exception:
        log.error("slack: the DM comparison search() failed; the channel answer already shipped",
                 exc_info=True)
        return
    if not (asker_paths - channel_paths):
        return   # nothing the asker's scope surfaces that the channel's could not

    try:
        fuller = await _run_ask(asker_service, question)
    except Exception:
        log.error("slack: the DM fuller-answer ask() failed; the channel answer already shipped",
                 exc_info=True)
        return

    channel_name = ""
    try:
        info = await ctx.gateway.conversations_info(channel_id)
        channel_name = (info.get("channel") or {}).get("name", "")
    except SlackApiError:
        pass   # the DM still reads fine without the channel name; not worth failing over

    blocks = render.render_dm_fuller_answer(channel_name=channel_name, question=question,
                                            answer=fuller, link_resolver=ctx.link_resolver,
                                            asker_slack_user_id=asker_slack_user_id,
                                            mint_token=ctx.mint_show_it_here_token)
    try:
        # A user id passed as `channel_id` opens (or reuses) the 1:1 DM — the real Slack Web API's
        # own documented behavior for `chat.postMessage`, so no separate `conversations.open` call
        # (and no extra gateway method) is needed.
        await ctx.gateway.chat_post_message(asker_slack_user_id, blocks=blocks,
                                            text=_answer_fallback_text(fuller))
    except SlackApiError:
        log.error("slack: could not DM the fuller answer to %s", asker_slack_user_id,
                 exc_info=True)
