"""The 🧠 gesture — the thread, verbatim, public channels only.

Calls the SAME `BrainService.submit()` every other transport calls; nothing here decides
visibility (`ctx.build_service` is the only enforcement-adjacent thing this module touches, and it
enforces nothing itself — it just builds the scoped service). The dedup key
(`stigmergy.slack.store.reserve`) is what keeps a redelivered event or a remove-then-re-add from
ever producing a second `capture_queue` row: `reserve()` is the only write that can race, and it
races against Postgres's own UNIQUE index.

The instant progress reaction (`mark_in_progress`/`finish_progress`, LATENCY-PLAN.md §3.4) is a
SEPARATE lifecycle from all of that: `app.on_reaction_added` marks before this module is even
called and finishes in a `finally`, driven by this module's own boolean return, so every one of
`handle_reaction_added`'s several early returns clears it the same way without a reaction call
scattered into each one.
"""
import asyncio
import logging

from stigmergy.slack import copy, render
from stigmergy.slack.gateway import SlackApiError
from stigmergy.slack.identity import IdentityResult, NoAccess, Resolved, TransientFailure, UsersInfoCache
from stigmergy.slack.store import MAX_HINT_CHARS, attach_submission, release_reservation, reserve

log = logging.getLogger(__name__)

BRAIN_REACTION = "brain"   # Slack's own name for 🧠

# The progress marker's own two faces — an hourglass while the pipeline runs, upgraded to a
# checkmark on a genuine success. Never a third emoji: `finish_progress` always removes the
# hourglass, and adds the checkmark ONLY on success, so a refusal/failure/duplicate leaves no
# trace of an attempt that produced no capture (the exact "stranded placeholder" shape issue #32
# was, for the reaction lifecycle rather than a message edit).
PROGRESS_REACTION = "hourglass_flowing_sand"
DONE_REACTION = "white_check_mark"


def is_public_channel(channel_meta: dict) -> bool:
    """Public-channel-only: neither private, nor a DM, nor a group DM."""
    return not (channel_meta.get("is_private") or channel_meta.get("is_im")
               or channel_meta.get("is_mpim"))


async def _react_or_log(gateway, *, channel_id: str, message_ts: str, name: str, add: bool,
                        what: str) -> None:
    """The one place a reaction call happens — never allowed to break a capture. `already_reacted`/
    `no_reaction` (event redelivery makes both reachable) are already collapsed to a plain success
    at `bolt_gateway`'s boundary, the same way it collapses `users_not_found` for
    `users_lookup_by_email`, so they never even reach this `except`; every OTHER `SlackApiError` —
    a missing `reactions:write` scope, a timeout, a rate limit — is logged and swallowed here,
    exactly like `SlackContext.post_or_log` does for a Slack send."""
    try:
        if add:
            await gateway.reactions_add(channel_id, message_ts, name)
        else:
            await gateway.reactions_remove(channel_id, message_ts, name)
    except SlackApiError:
        log.error("slack capture: %s failed for %s/%s", what, channel_id, message_ts,
                 exc_info=True)


async def mark_in_progress(gateway, *, channel_id: str, message_ts: str) -> None:
    """The instant progress marker — called from `app.on_reaction_added` as close to the raw
    event as possible, before any identity work: visible in ~200ms instead of the 1.5-4s of
    silence the un-instrumented pipeline left before its "queued" thread ack
    (LATENCY-PLAN.md §3.4)."""
    await _react_or_log(gateway, channel_id=channel_id, message_ts=message_ts,
                        name=PROGRESS_REACTION, add=True, what="progress reaction")


async def finish_progress(gateway, *, channel_id: str, message_ts: str, ok: bool) -> None:
    """The progress marker's cleanup — the other half of `mark_in_progress`, called from
    `app.on_reaction_added`'s `finally` so EVERY exit path clears it, never just the success one.
    `ok=True` upgrades the marker to a done mark; anything else — a refusal, a failure, a
    duplicate — just removes it.

    **The done mark means QUEUED, not filed, and it is never revoked.** The queue row is the
    durable fact this marker reports; the thread ack that follows it goes through
    `SlackContext.post_or_log` and is best-effort by design, so the mark does not claim the ack
    reached Slack. It equally does not claim the librarian later ACCEPTED the capture — a rejected
    or failed filing keeps its done mark, and the poller's thread report is what carries the
    terminal outcome. Same reasoning as `reaction_removed` being ignored: a marker the system
    cannot honour later is worse than a marker with a narrow, stated meaning."""
    await _react_or_log(gateway, channel_id=channel_id, message_ts=message_ts,
                        name=PROGRESS_REACTION, add=False, what="progress-reaction cleanup")
    if ok:
        await _react_or_log(gateway, channel_id=channel_id, message_ts=message_ts,
                            name=DONE_REACTION, add=True, what="done reaction")


async def _display_name(gateway, cache: UsersInfoCache, team_id: str, user_id: str) -> str:
    """Best-effort — a decorative fact for the ack/hints, never load-bearing enough to fail a
    capture over. Checks the SAME cache `identity.resolve_slack_identity` populates, keyed
    identically on `(team_id, slack_user_id)`, before calling `users.info` at all — the reactor's
    own display name is very often already there, cached moments earlier by the identity
    resolution `app._resolve` just ran. Falls back to the raw Slack user id on any API trouble, as
    before; a name this call fetches is cached for next time, an empty one never is (the cache's
    own positive-only rule)."""
    if not user_id:
        return ""
    cached = cache.get_display_name(team_id, user_id)
    if cached is not None:
        return cached
    try:
        profile = await gateway.users_info(user_id)
    except SlackApiError:
        return user_id
    prof = (profile.get("user") or {}).get("profile") or {}
    name = prof.get("display_name") or prof.get("real_name") or ""
    if not name:
        return user_id
    cache.put_display_name(team_id, user_id, name)
    return name


async def _material_and_hints(gateway, cache: UsersInfoCache, messages: list[dict], *,
                              team_id: str, channel_id: str, channel_name: str,
                              permalink: str) -> tuple[str, dict, str]:
    """Verbatim material — text exactly as Slack returns it, no summarizing, no redacting, no
    tidying — plus the provenance hints (`capture.schema.SOURCE_HINT_KEYS`).

    `material` is the newline-joined `text` of every message `conversations.replies` returned, in
    the order Slack returned them (oldest first): byte-identical to the concatenated thread text
    Slack returned, with the join character being the one piece of structure this function adds
    and nothing else (no summarizing, no reordering, no trimming).
    """
    material = "\n".join(m.get("text", "") for m in messages)
    thread_ts = (messages[0].get("thread_ts") or messages[0].get("ts", "")) if messages else ""
    seen_users: list[str] = []
    for m in messages:
        uid = m.get("user") or ""
        if uid and uid not in seen_users:
            seen_users.append(uid)
    # A single `gather`, not a manual hit/miss split: `_display_name` itself checks the cache
    # FIRST, synchronously, so a cache hit resolves before its coroutine ever suspends — only an
    # actual `users.info` call (a genuine cache miss) runs concurrently with the others. Order is
    # preserved (`gather`'s own contract), matching `seen_users`' first-appearance order exactly
    # as the old serial loop did.
    participants = list(await asyncio.gather(
        *(_display_name(gateway, cache, team_id, uid) for uid in seen_users)))
    timestamps = [m.get("ts", "") for m in messages]
    hints = {
        "source_client": "slack",
        "source_permalink": permalink,
        "source_channel_id": channel_id,
        "source_channel_name": channel_name,
        "source_thread_ts": thread_ts,
        # Truncated, not the material. `normalize_hints` refuses any hint value over
        # `MAX_HINT_CHARS` outright — a thread long enough (`source_message_timestamps` overflows
        # around 450 messages) would otherwise make this capture fail DETERMINISTICALLY, forever,
        # on every retry. These two are the only list-derived hints (everything else here is a
        # single scalar), and they are PROVENANCE metadata, never the captured material itself —
        # truncating a `source_*` hint loses none of "the thread, verbatim"; truncating `material`
        # would.
        "source_participants": ", ".join(participants)[:MAX_HINT_CHARS],
        "source_message_timestamps": ", ".join(timestamps)[:MAX_HINT_CHARS],
    }
    return material, hints, thread_ts


async def handle_reaction_added(ctx, *, reaction: str, team_id: str, channel_id: str,
                                message_ts: str, slack_user_id: str,
                                identity_result: IdentityResult) -> bool:
    """The whole 🧠 flow for one `reaction_added` event. `identity_result` is already resolved by
    the caller (`stigmergy.slack.identity.resolve_slack_identity`, after `is_ignorable_event` and the
    workspace check) — this function starts from there so its own responsibility is exactly the
    capture gesture, nothing about identity resolution.

    Returns `True` only on the genuine success path — the queue row committed. It deliberately
    does NOT also require the thread ack to have posted: that send goes through
    `post_or_log` and is swallowed on failure, and a capture that IS queued must not report itself
    as failed because a courtesy message did not reach Slack. `False` on every
    other exit — a refusal, a failure, a duplicate. `app.on_reaction_added` uses this as the
    progress reaction's own "done mark vs. just remove it" signal (`capture.finish_progress`); it
    is not a `capture_queue` outcome and callers that need one still read `capture_queue` itself."""
    if reaction != BRAIN_REACTION:
        return False   # no reaction other than 🧠 triggers anything

    if isinstance(identity_result, TransientFailure):
        # Routed through the one shared decline seam (`SlackContext.decline`) — the 🧠 gesture is
        # public-channel-only, so this is always the ephemeral branch, but the point of the seam
        # is that this module does not decide that for itself.
        await ctx.decline(channel_id=channel_id, slack_user_id=slack_user_id, is_dm=False,
                          blocks=render.render_transient_identity_failure(),
                          text=copy.TRANSIENT_IDENTITY_FAILURE)
        return False
    if isinstance(identity_result, NoAccess):
        await ctx.decline(channel_id=channel_id, slack_user_id=slack_user_id, is_dm=False,
                          blocks=render.render_no_access(is_dm=False),
                          text=copy.no_access(is_dm=False))
        return False
    if not isinstance(identity_result, Resolved):
        return False   # Ignored / ForeignTeam: no Slack traffic at all

    email, audiences = identity_result.email, identity_result.audiences

    try:
        channel_meta = (await ctx.gateway.conversations_info(channel_id)).get("channel", {})
    except SlackApiError:
        log.error("slack capture: conversations.info failed for %s", channel_id, exc_info=True)
        await ctx.post_or_log(
            ctx.gateway.chat_post_ephemeral(channel_id, slack_user_id,
                                            blocks=render.render_server_error(),
                                            text=copy.server_error()),
            what=f"capture server-error ephemeral in {channel_id}")
        return False

    if not is_public_channel(channel_meta):
        await ctx.post_or_log(
            ctx.gateway.chat_post_ephemeral(channel_id, slack_user_id,
                                            blocks=render.render_private_channel_refusal(),
                                            text=copy.PRIVATE_CHANNEL_REFUSAL),
            what=f"private-channel refusal in {channel_id}")
        return False

    messages = await ctx.gateway.conversations_replies(channel_id, message_ts)
    permalink = await ctx.gateway.get_permalink(channel_id, message_ts)
    material, hints, thread_ts = await _material_and_hints(
        ctx.gateway, ctx.cache, messages, team_id=team_id, channel_id=channel_id,
        channel_name=channel_meta.get("name", ""), permalink=permalink)

    # reserve + submit + attach are ONE transaction. A crash between `submit` succeeding and
    # `attach_submission` running — a deploy causes exactly this — would otherwise leave a
    # committed `capture_queue` row with `slack_submissions.submission_id` stuck NULL: invisible to
    # `find_thread_submissions`/`due_for_report` forever (both filter `submission_id IS NOT NULL`),
    # so ask-back is dead for that capture and every redelivery/re-add logs a false "duplicate".
    # Wrapping the whole sequence means ANY failure in it — including a real process crash, which
    # closes the connection and lets Postgres roll back the still-open transaction on its own —
    # undoes the reservation too, so a genuine RETRY (not merely a redelivery) can succeed cleanly.
    # `release_reservation` below is a no-op after a rollback (the row is already gone); kept as a
    # defensive no-op in case that ever changes, not because it is load-bearing here.
    reservation_id = None
    try:
        with ctx.conn.transaction():
            reservation_id = reserve(ctx.conn, team_id=team_id, channel_id=channel_id,
                                     message_ts=message_ts, thread_ts=thread_ts,
                                     slack_user_id=slack_user_id, submitted_by=email)
            if reservation_id is None:
                log.info("slack capture: duplicate (team=%s channel=%s ts=%s user=%s) — no-op",
                         team_id, channel_id, message_ts, slack_user_id)
                return False   # redelivered or re-added; no second row, no second post
            service = ctx.build_service(email, audiences)
            ack = service.submit("raw", material, hints=hints)
            attach_submission(ctx.conn, reservation_id, ack["id"])
    except Exception:
        if reservation_id is not None:
            release_reservation(ctx.conn, reservation_id)
        log.error("slack capture failed to queue (team=%s channel=%s ts=%s user=%s)",
                 team_id, channel_id, message_ts, slack_user_id, exc_info=True)
        await ctx.post_or_log(
            ctx.gateway.chat_post_ephemeral(channel_id, slack_user_id,
                                            blocks=render.render_capture_failed(),
                                            text=copy.CAPTURE_FAILED, thread_ts=thread_ts),
            what=f"capture-failed ephemeral in {channel_id}")
        return False

    display_name = await _display_name(ctx.gateway, ctx.cache, team_id, slack_user_id)
    await ctx.post_or_log(
        ctx.gateway.chat_post_message(channel_id, blocks=render.render_capture_ack(display_name),
                                      text=copy.capture_ack(display_name), thread_ts=thread_ts),
        what=f"capture ack in {channel_id}")
    return True
