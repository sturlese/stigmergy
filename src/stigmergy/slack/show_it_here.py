"""The "Show it here" button: a click inside an answer's thread that re-reads one cited page
under the clicker's own identity — server-side scoped, because Slack has no per-viewer buttons.
"""
import logging

from stigmergy.kernel.blocking import run_blocking
from stigmergy.slack import copy, render
from stigmergy.slack.context import run_with_service
from stigmergy.slack.gateway import SlackApiError
from stigmergy.slack.identity import Resolved

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

    try:
        result = await run_blocking(
            run_with_service,
            ctx,
            identity_result.email,
            identity_result.audiences,
            lambda service: service.read_page(path),
        )
    except Exception as error:
        # Silence is the DELIBERATE answer for a wrong clicker, an expired token and an identity
        # failure — which is exactly why a real fault must not borrow it. `RateLimitError` is an
        # ordinary, user-reachable raise through `BrainService._call`, and swallowed silence
        # would tell an asker over budget they were not the owner of their own answer.
        log.error("slack show-it-here: read_page failed (%s)", error.__class__.__name__)
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
    except SlackApiError as error:
        log.error(
            "slack: could not post the 'show it here' result (%s)",
            error.__class__.__name__,
        )
