"""Two things that happen inside an EXISTING thread, neither of them a new question: the
submitter's ask-back answer, and a click on the "Show it here" button.
"""
import logging

from stigmergy.slack import copy, render
from stigmergy.slack.gateway import SlackApiError
from stigmergy.slack.identity import Resolved, resolve_slack_identity
from stigmergy.slack.store import find_thread_submissions, is_awaiting_reply

log = logging.getLogger(__name__)

# Slack's own section-text ceiling is 3000 characters; leave room for the "📄 *title*\n\n" header.
SHOW_IT_HERE_EXCERPT_CHARS = 2800

_FENCE_PREFIX = "<<<UNTRUSTED-DATA\n"
_FENCE_SUFFIX = "\nUNTRUSTED-DATA;end>>>"


def _unfence(body: str) -> str:
    """`read_page`'s body is fenced for an AGENT's context (`BrainService.fence`) — the affordance
    is for a human reading Slack, so the decorative fence delimiters are stripped for display. This
    changes nothing about trust or sanitization (both already happened upstream); it only removes
    markers a human reader has no use for."""
    if body.startswith(_FENCE_PREFIX) and body.endswith(_FENCE_SUFFIX):
        return body[len(_FENCE_PREFIX):-len(_FENCE_SUFFIX)]
    return body


# ── ask-back: the submitter's reply, and nobody else's ───────────────────────────────────────────
async def handle_thread_message(ctx, *, team_id: str, channel_id: str, thread_ts: str,
                                slack_user_id: str, text: str) -> None:
    """Called for every ordinary message inside a thread (never for a fresh top-level message —
    the caller only invokes this when `thread_ts` names an EXISTING thread). A no-op for every
    thread that is neither a Slack-originated capture's origin thread NOR a staged `@brain`/DM
    Q&A thread — the overwhelming majority of Slack's traffic.

    **Only the original submitter's reply counts**: a reply from anyone else is ignored ENTIRELY —
    no `brain_reply`, no error, no reaction — because `brain_reply` runs under the replier's
    identity, and accepting another person's text would attribute their words to the submitter (an
    attribution forgery, not a UX detail). The identity check happens here, BEFORE
    `BrainService.reply()` is ever called, precisely so a bystander's ordinary chatter in the
    thread never reaches the write path at all.

    `team_id` is the EVENT's own workspace (`stigmergy.slack.app._event_team_id`), and it is also the
    value the workspace check inside `resolve_slack_identity` uses. Passing the CONFIGURED
    `ctx.settings.team_id` instead would make that comparison a tautology that can never fail.

    A thread may legally hold more than one Slack-originated capture — the UNIQUE key is
    `(team_id, channel_id, thread_ts, slack_user_id)`, so two DIFFERENT people reacting in the SAME
    thread each reserve their own row — so the row that matters is never "whichever is newest". It
    is the CURRENT replier's own row that is actually `needs_input`, found by filtering on the
    resolved email, never assumed from the newest row in the thread. And "not `needs_input`" is not
    the same as "already answered": `q.reply` (set only once a `needs_input` question was actually
    answered, per `capture.queue.record_reply`) is what decides that — a row that was never asked
    anything (`queued`, right after the capture ack) gets silence, symmetric with a bystander.
    """

    submissions = find_thread_submissions(ctx.conn, team_id=team_id, channel_id=channel_id,
                                          thread_ts=thread_ts)
    if not submissions:
        return   # ordinary conversation — no Slack-originated capture in this thread

    identity_result = await resolve_slack_identity(
        ctx.gateway, ctx.cache, identities_path=ctx.settings.server.identities_path,
        configured_team_id=ctx.settings.team_id, event_team_id=team_id,
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
    """`action_value` is the OPAQUE token `SlackContext.mint_show_it_here_token` minted — NEVER the
    asker's email in cleartext. A button value is retrievable by any workspace member via
    `conversations.history` and by any other app with history scope, so `(path,
    owner_slack_user_id)` lives server-side, keyed on the token, short-TTL. `event_team_id` is the
    clicking interaction's OWN workspace (`app.py`'s `on_show_it_here` sources it from
    `body["team"]["id"]`); passing the configured `ctx.settings.team_id` instead would make the
    workspace check a tautology on this read path too.

    **Server-side scoping is the whole of the affordance's access control.** The button itself is
    visible to everyone who can see the message (Slack has no per-viewer button visibility) — so
    ANYONE may click it, and this function's first job is deciding whether the CLICK counts. A
    click from anyone other than the original asker is silently declined: no ephemeral, no error,
    nothing observable to them — the "must not exist as a button other channel members can press"
    property, enforced at the only point that can enforce it. The comparison is a plain
    Slack-user-id equality (the token's owner, set at render time from the ORIGINAL asker's own
    event) rather than a resolved-email comparison — cheaper (no `users.info` call wasted on a
    mismatched clicker) and exactly as strong, since a Slack user id is Slack's own authenticated
    fact about who clicked.
    """
    entry = ctx.consume_show_it_here_token(action_value)
    if entry is None:
        return   # an unknown or expired token — declined the same as a wrong clicker
    path, owner_slack_user_id = entry
    if clicking_slack_user_id != owner_slack_user_id:
        return   # silently declined — someone other than the original asker

    identity_result = await resolve_slack_identity(
        ctx.gateway, ctx.cache, identities_path=ctx.settings.server.identities_path,
        configured_team_id=ctx.settings.team_id, event_team_id=event_team_id,
        slack_user_id=clicking_slack_user_id)
    if not isinstance(identity_result, Resolved):
        return   # silently declined — including an identity failure of the clicking user

    service = ctx.build_service(identity_result.email, identity_result.audiences)
    try:
        result = service.read_page(path)
    except Exception:
        # Silence is this function's DELIBERATE answer to a wrong clicker, an expired token and an
        # identity failure (see the declines above) — which is exactly why a real fault must not
        # borrow it. `read_page` goes through `BrainService._call`, which checks the rate limiter
        # FIRST, so `RateLimitError` is an ordinary, user-reachable raise; unwrapped, it escaped to
        # `app.py`'s listener backstop, which logs and posts nothing. The asker over their budget
        # was told, by silence, that they were not the owner of their own answer. `mention.py`
        # already renders the rate-limit copy for this same exception one surface over.
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
        text = f"📄 {result.get('title') or path}"

    try:
        if is_dm:
            await ctx.gateway.chat_post_message(channel_id, blocks=blocks, text=text,
                                                thread_ts=thread_ts)
        else:
            await ctx.gateway.chat_post_ephemeral(channel_id, clicking_slack_user_id, blocks=blocks,
                                                  text=text, thread_ts=thread_ts)
    except SlackApiError:
        log.error("slack: could not post the 'show it here' result", exc_info=True)
