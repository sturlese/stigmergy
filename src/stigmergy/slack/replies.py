"""Two things that happen inside an EXISTING thread, neither of them a new question: the
submitter's ask-back answer, and a click on the "Show it here" button.
"""
import logging

from stigmergy.slack import copy, render
from stigmergy.slack.gateway import SlackApiError
from stigmergy.slack.identity import Resolved
from stigmergy.slack.store import find_thread_submissions, is_awaiting_reply

log = logging.getLogger(__name__)

# Slack's own section-text ceiling is 3000 characters; leave room for the "📄 *title*\n\n" header.
SHOW_IT_HERE_EXCERPT_CHARS = 2800

_FENCE_PREFIX = "<<<UNTRUSTED-DATA\n"
_FENCE_SUFFIX = "\nUNTRUSTED-DATA;end>>>"


def _unfence(body: str) -> str:
    """`read_page`'s body is fenced for an AGENT's context; this affordance is for a human, so
    the fence delimiters are stripped for display — trust and sanitization already happened
    upstream."""
    if body.startswith(_FENCE_PREFIX) and body.endswith(_FENCE_SUFFIX):
        return body[len(_FENCE_PREFIX):-len(_FENCE_SUFFIX)]
    return body


# ── ask-back: the submitter's reply, and nobody else's ───────────────────────────────────────────
async def handle_thread_message(ctx, *, team_id: str, channel_id: str, thread_ts: str,
                                slack_user_id: str, text: str) -> None:
    """Called for every ordinary message inside an EXISTING thread; a no-op for any thread
    holding no Slack-originated capture — the overwhelming majority of Slack's traffic.

    **Only the original submitter's reply counts** — anyone else is ignored ENTIRELY (no
    `brain_reply`, no error, no reaction), because accepting another person's text would
    attribute their words to the submitter. Checked here, BEFORE `BrainService.reply()` is ever
    called.

    `team_id` is the EVENT's own workspace; passing the configured `ctx.settings.team_id` would
    make the workspace check a tautology. A thread may hold several captures (the UNIQUE key is
    per (thread, reactor)), so the row that matters is the CURRENT replier's own `needs_input`
    row, filtered by resolved email — and "not `needs_input`" is not "already answered": `q.reply`
    (set only once a question was actually answered) decides that, and a row never asked anything
    gets silence, symmetric with a bystander.
    """

    submissions = find_thread_submissions(ctx.conn, team_id=team_id, channel_id=channel_id,
                                          thread_ts=thread_ts)
    if not submissions:
        return   # ordinary conversation — no Slack-originated capture in this thread

    identity_result = await ctx.resolve_slack_identity(event_team_id=team_id,
                                                       slack_user_id=slack_user_id)
    if not isinstance(identity_result, Resolved):
        return

    own_submissions = [s for s in submissions if s["submitted_by"] == identity_result.email]
    if not own_submissions:
        return   # no row here belongs to this sender — ignored entirely, like a bystander

    needs_input = next((s for s in own_submissions if is_awaiting_reply(s["status"])), None)
    if needs_input is None:
        newest_own = own_submissions[0]   # `find_thread_submissions` orders newest-first
        if newest_own.get("reply"):
            await ctx.post_or_log(
                ctx.gateway.chat_post_ephemeral(channel_id, slack_user_id,
                                                blocks=render.render_reply_already_answered(),
                                                text=copy.REPLY_ALREADY_ANSWERED,
                                                thread_ts=thread_ts),
                what=f"reply-already-answered ephemeral in {channel_id}")
        return   # never asked anything — silent, symmetric with a bystander

    service = ctx.build_service(identity_result.email, identity_result.audiences)
    try:
        service.reply(needs_input["submission_id"], text)
    except Exception:
        log.error("slack ask-back: brain_reply failed for submission %s",
                 needs_input["submission_id"], exc_info=True)
        await ctx.post_or_log(
            ctx.gateway.chat_post_message(channel_id, blocks=render.render_server_error(),
                                          text=copy.server_error(), thread_ts=thread_ts),
            what=f"ask-back server-error in {channel_id}")
        return

    await ctx.post_or_log(
        ctx.gateway.chat_post_message(channel_id, blocks=render.render_reply_delivered(),
                                      text=copy.REPLY_DELIVERED, thread_ts=thread_ts),
        what=f"reply-delivered in {channel_id}")


# ── the "show it here" affordance ────────────────────────────────────────────────────────────────
async def handle_show_it_here(ctx, *, action_value: str, clicking_slack_user_id: str,
                              channel_id: str, thread_ts: str | None, is_dm: bool,
                              event_team_id: str) -> None:
    """`action_value` is the OPAQUE token `SlackContext.mint_show_it_here_token` minted — a
    button value is retrievable by any workspace member via `conversations.history`, so
    `(path, owner_slack_user_id)` lives server-side, keyed on the token, short-TTL.

    **Server-side scoping is the whole of the affordance's access control**: the button is
    visible to everyone, so ANYONE may click, and the first job is deciding whether the CLICK
    counts. A click from anyone but the original asker is silently declined — nothing observable
    to them. The comparison is a plain Slack-user-id equality (Slack's own authenticated fact
    about who clicked), cheaper than an email resolution and exactly as strong. `event_team_id`
    is the interaction's OWN workspace — the configured one would make the workspace check a
    tautology here too.
    """
    entry = ctx.consume_show_it_here_token(action_value)
    if entry is None:
        return   # an unknown or expired token — declined the same as a wrong clicker
    path, owner_slack_user_id = entry
    if clicking_slack_user_id != owner_slack_user_id:
        return   # silently declined — someone other than the original asker

    identity_result = await ctx.resolve_slack_identity(event_team_id=event_team_id,
                                                       slack_user_id=clicking_slack_user_id)
    if not isinstance(identity_result, Resolved):
        return   # silently declined — including an identity failure of the clicking user

    service = ctx.build_service(identity_result.email, identity_result.audiences)
    try:
        result = service.read_page(path)
    except Exception:
        # Silence is the DELIBERATE answer for a wrong clicker, an expired token and an identity
        # failure — which is exactly why a real fault must not borrow it. `RateLimitError` is an
        # ordinary, user-reachable raise through `BrainService._call`, and swallowed silence
        # would tell an asker over budget they were not the owner of their own answer.
        log.error("slack show-it-here: read_page failed for %s", path, exc_info=True)
        await ctx.post_or_log(
            ctx.gateway.chat_post_ephemeral(channel_id, clicking_slack_user_id,
                                            blocks=render.render_server_error(),
                                            text=copy.server_error()),
            what=f"show-it-here server-error in {channel_id}")
        return
    if "error" in result:
        blocks = render.render_show_it_here_refusal(path)
        text = copy.show_it_here_refusal(path)
    else:
        excerpt = _unfence(result.get("body", ""))[:SHOW_IT_HERE_EXCERPT_CHARS]
        blocks = render.render_show_it_here_success(page_title=result.get("title") or path,
                                                     excerpt=excerpt)
        text = copy.show_it_here_fallback(result.get("title") or path)

    try:
        if is_dm:
            await ctx.gateway.chat_post_message(channel_id, blocks=blocks, text=text,
                                                thread_ts=thread_ts)
        else:
            await ctx.gateway.chat_post_ephemeral(channel_id, clicking_slack_user_id, blocks=blocks,
                                                  text=text, thread_ts=thread_ts)
    except SlackApiError:
        log.error("slack: could not post the 'show it here' result", exc_info=True)
