"""The 🧠 gesture — the thread, verbatim, public channels only.

Calls the SAME `BrainService.submit()` every other transport calls; nothing here decides
visibility. The dedup key (`store.reserve`) races against Postgres's own UNIQUE index, so a
redelivered event or a remove-then-re-add never produces a second `capture_queue` row. The
progress reaction (`mark_in_progress`/`finish_progress`) is a separate lifecycle, driven by
`app.on_reaction_added` around this module: marked before it is called, finished in a `finally`
off this module's boolean return, so every early return clears it the same way.
"""
import asyncio
import logging

from stigmergy.server.errors import IdentityError
from stigmergy.server.service import SubmitRefused
from stigmergy.slack import channels, copy, render
from stigmergy.slack.gateway import SlackApiError
from stigmergy.slack.identity import IdentityResult, NoAccess, Resolved, TransientFailure, UsersInfoCache
from stigmergy.slack.store import MAX_HINT_CHARS, attach_submission, release_reservation, reserve

log = logging.getLogger(__name__)

BRAIN_REACTION = "brain"   # Slack's own name for 🧠

# An hourglass while the pipeline runs, upgraded to a checkmark ONLY on a genuine success — a
# refusal/failure/duplicate leaves no stranded marker for an attempt that produced no capture.
PROGRESS_REACTION = "hourglass_flowing_sand"
DONE_REACTION = "white_check_mark"


def _is_public_channel(channel_meta: dict) -> bool:
    """Public-channel-only: neither private, nor a DM, nor a group DM."""
    return not (channel_meta.get("is_private") or channel_meta.get("is_im")
               or channel_meta.get("is_mpim"))


async def _react_or_log(gateway, *, channel_id: str, message_ts: str, name: str, add: bool,
                        what: str) -> None:
    """The one place a reaction call happens — never allowed to break a capture.
    `already_reacted`/`no_reaction` are already collapsed to success at `bolt_gateway`'s boundary;
    every OTHER `SlackApiError` is logged and swallowed here."""
    try:
        if add:
            await gateway.reactions_add(channel_id, message_ts, name)
        else:
            await gateway.reactions_remove(channel_id, message_ts, name)
    except SlackApiError:
        log.error("slack capture: %s failed for %s/%s", what, channel_id, message_ts,
                 exc_info=True)


async def mark_in_progress(gateway, *, channel_id: str, message_ts: str) -> None:
    """The instant progress marker — called from `app.on_reaction_added` before any identity
    work, so it is visible in ~200ms rather than after seconds of silence."""
    await _react_or_log(gateway, channel_id=channel_id, message_ts=message_ts,
                        name=PROGRESS_REACTION, add=True, what="progress reaction")


async def finish_progress(gateway, *, channel_id: str, message_ts: str, ok: bool) -> None:
    """The other half of `mark_in_progress`, called from `app.on_reaction_added`'s `finally` so
    EVERY exit path clears the marker; `ok=True` upgrades it to a done mark.

    **The done mark means QUEUED, not filed, and is never revoked**: the queue row is the durable
    fact it reports — the thread ack is best-effort, and the poller's thread report carries the
    terminal outcome. A marker the system cannot honour later is worse than one with a narrow,
    stated meaning."""
    await _react_or_log(gateway, channel_id=channel_id, message_ts=message_ts,
                        name=PROGRESS_REACTION, add=False, what="progress-reaction cleanup")
    if ok:
        await _react_or_log(gateway, channel_id=channel_id, message_ts=message_ts,
                            name=DONE_REACTION, add=True, what="done reaction")


async def _display_name(gateway, cache: UsersInfoCache, team_id: str, user_id: str) -> str:
    """Best-effort decoration for the ack/hints — never load-bearing enough to fail a capture
    over. Checks the SAME cache `resolve_slack_identity` populates before calling `users.info`;
    falls back to the raw user id on any API trouble. Positive results only are cached."""
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
    """Verbatim material plus the provenance hints (`capture.schema.SOURCE_HINT_KEYS`).
    `material` is the newline-joined `text` of every message, in Slack's own order (oldest
    first) — the join character is the one piece of structure this function adds.
    """
    material = "\n".join(m.get("text", "") for m in messages)
    thread_ts = (messages[0].get("thread_ts") or messages[0].get("ts", "")) if messages else ""
    seen_users: list[str] = []
    for m in messages:
        uid = m.get("user") or ""
        if uid and uid not in seen_users:
            seen_users.append(uid)
    # One `gather`, no manual hit/miss split: `_display_name` checks the cache synchronously
    # before its coroutine ever suspends, so only genuine misses hit `users.info` concurrently.
    # `gather` preserves `seen_users`' first-appearance order.
    participants = list(await asyncio.gather(
        *(_display_name(gateway, cache, team_id, uid) for uid in seen_users)))
    timestamps = [m.get("ts", "") for m in messages]
    hints = {
        "source_client": "slack",
        "source_permalink": permalink,
        "source_channel_id": channel_id,
        "source_channel_name": channel_name,
        "source_thread_ts": thread_ts,
        # Truncated — `normalize_hints` refuses any hint value over `MAX_HINT_CHARS` outright, so
        # an overflowing list-derived hint would fail the capture DETERMINISTICALLY on every
        # retry. These two are provenance, never the captured material: truncating a `source_*`
        # hint loses none of "the thread, verbatim"; truncating `material` would.
        "source_participants": ", ".join(participants)[:MAX_HINT_CHARS],
        "source_message_timestamps": ", ".join(timestamps)[:MAX_HINT_CHARS],
    }
    return material, hints, thread_ts


async def handle_reaction_added(ctx, *, reaction: str, team_id: str, channel_id: str,
                                message_ts: str, slack_user_id: str,
                                identity_result: IdentityResult) -> bool:
    """The whole 🧠 flow for one `reaction_added` event; `identity_result` is already resolved by
    the caller. Returns `True` only when the queue row committed — deliberately NOT also requiring
    the best-effort thread ack — and `False` on every other exit (a refusal, a failure, a
    duplicate). `app.on_reaction_added` uses the return as the progress reaction's
    done-mark-vs-remove signal; it is not a `capture_queue` outcome."""
    if reaction != BRAIN_REACTION:
        return False

    if isinstance(identity_result, TransientFailure):
        # The one shared decline seam — the 🧠 gesture is public-channel-only, hence is_dm=False.
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

    if not _is_public_channel(channel_meta):
        await ctx.post_or_log(
            ctx.gateway.chat_post_ephemeral(channel_id, slack_user_id,
                                            blocks=render.render_private_channel_refusal(),
                                            text=copy.PRIVATE_CHANNEL_REFUSAL),
            what=f"private-channel refusal in {channel_id}")
        return False

    # Guarded like `conversations.info` above — `conversations.replies` needs `channels:history`
    # and is rate-limited. Unguarded, an error here escapes the handler and the reactor sees the
    # ⏳ appear, vanish, and nothing else: no capture, no refusal, no acknowledgement at all.
    try:
        messages = await ctx.gateway.conversations_replies(channel_id, message_ts)
        permalink = await ctx.gateway.get_permalink(channel_id, message_ts)
    except SlackApiError:
        log.error("slack capture: reading the thread failed for %s/%s", channel_id, message_ts,
                  exc_info=True)
        await ctx.post_or_log(
            ctx.gateway.chat_post_ephemeral(channel_id, slack_user_id,
                                            blocks=render.render_server_error(),
                                            text=copy.server_error()),
            what=f"capture server-error ephemeral in {channel_id}")
        return False
    channel_name = channel_meta.get("name", "")
    material, hints, thread_ts = await _material_and_hints(
        ctx.gateway, ctx.cache, messages, team_id=team_id, channel_id=channel_id,
        channel_name=channel_name, permalink=permalink)

    # THE audience decision for this capture: the groups of the channel the person
    # reacted in. A channel not listed is public, and public is OPEN — `channel_audiences_live`
    # returns the empty set there, and the door stores `None` rather than `{}`, because "this
    # channel has no groups" is a fact about the channel and never the `acl: []` of a page.
    #
    # Asked HERE, before the dedup reservation, and not inside the transaction below: a refusal is
    # a refusal, and running it there would spend the reservation and surface as "the capture
    # failed", which is the wrong sentence and the wrong recovery. `resolve_submit_audience` is
    # the SAME check `brain_submit` runs — one rule, two doors.
    try:
        channel_groups = sorted(channels.channel_groups_for_capture(
            ctx.conn, ctx.settings.channels_path, channel_id))
    except IdentityError:
        log.error("slack capture: channel scope unreadable for %s", channel_id, exc_info=True)
        await ctx.post_or_log(
            ctx.gateway.chat_post_ephemeral(channel_id, slack_user_id,
                                            blocks=render.render_server_error(),
                                            text=copy.server_error()),
            what=f"capture server-error ephemeral in {channel_id}")
        return False
    try:
        probe = ctx.build_service(email, audiences)
        capture_acl = probe.check_submit_audience(channel_groups or None)
    except SubmitRefused:
        log.info("slack capture: %s is not in the groups %s files at — refused", email, channel_id)
        await ctx.post_or_log(
            ctx.gateway.chat_post_ephemeral(
                channel_id, slack_user_id,
                blocks=render.render_not_in_this_channels_groups(channel_name),
                text=copy.not_in_this_channels_groups(channel_name), thread_ts=thread_ts),
            what=f"channel-audience refusal in {channel_id}")
        return False
    except Exception:  # noqa: BLE001 — see below; the reactor must be told SOMETHING
        # Guarded like every other seam in this function, and for its reason: unguarded, an error
        # here escapes into `app.on_reaction_added`'s outer handler and the reactor sees the ⏳
        # appear, vanish, and nothing else. Two classes are reachable and neither is a
        # `SubmitRefused`: `RateLimitError` (a server error, since `check_submit_audience` goes
        # through the audited `_call` seam) and a failure of the `audit_log` write inside that
        # seam's own `finally`. Do not narrow this back to those two by name — the point is that
        # the reactor is told, whatever went wrong.
        log.error("slack capture: the audience check failed for %s in %s",
                  email, channel_id, exc_info=True)
        await ctx.post_or_log(
            ctx.gateway.chat_post_ephemeral(channel_id, slack_user_id,
                                            blocks=render.render_server_error(),
                                            text=copy.server_error(), thread_ts=thread_ts),
            what=f"capture server-error ephemeral in {channel_id}")
        return False

    # reserve + submit + attach are ONE transaction. A crash between `submit` succeeding and
    # `attach_submission` running (a deploy does exactly this) would otherwise commit a
    # `capture_queue` row whose `slack_submissions.submission_id` stays NULL — invisible to
    # `find_thread_submissions`/`due_for_report` forever, ask-back dead, every redelivery a false
    # "duplicate". One transaction means ANY failure — a process crash included — rolls the
    # reservation back too, so a genuine RETRY succeeds cleanly. `release_reservation` below is a
    # defensive no-op after a rollback.
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
            ack = service.submit("raw", material, hints=hints, audience=capture_acl)
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
