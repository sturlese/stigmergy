"""Slack brain-reaction capture adapter."""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import quote, urlsplit, urlunsplit

from stigmergy.capture import artifacts, evidence, schema
from stigmergy.capture.errors import CaptureError
from stigmergy.kernel.blocking import run_blocking
from stigmergy.server.errors import IdentityError
from stigmergy.server.service import SubmitRefused
from stigmergy.slack import channels, copy, render
from stigmergy.slack.context import run_with_connection, run_with_service
from stigmergy.slack.gateway import SlackApiError
from stigmergy.slack.identity import (
    IdentityResult,
    NoAccess,
    Resolved,
    TransientFailure,
    UsersInfoCache,
)
from stigmergy.slack.snapshot import (
    MAX_SNAPSHOT_BYTES,
    MAX_THREAD_MESSAGES,
    SlackSnapshot,
    SnapshotAttachment,
    SnapshotMessage,
    canonical_bytes,
    timestamp_from_slack,
)
from stigmergy.slack.store import (
    attach_submission,
    bind_thread,
    release_reservation,
    reserve_reaction,
)

log = logging.getLogger(__name__)

BRAIN_REACTION = "brain"
CAPTURE_ACQUISITION_TIMEOUT_S = 60
PROFILE_LOOKUP_CONCURRENCY = 8
_SLACK_TIMESTAMP_RE = re.compile(r"^\d+\.\d+$")


async def _display_name(gateway, cache: UsersInfoCache, team_id: str, user_id: str) -> str:
    if not user_id:
        return "Unknown speaker"
    cached = cache.get_display_name(team_id, user_id)
    if cached is not None:
        return cached
    try:
        profile = await gateway.users_info(user_id)
    except SlackApiError:
        return user_id
    values = (profile.get("user") or {}).get("profile") or {}
    name = values.get("display_name") or values.get("real_name") or user_id
    cache.put_display_name(team_id, user_id, name)
    return name


async def _build_snapshot(
    gateway,
    cache: UsersInfoCache,
    *,
    team_id: str,
    channel_id: str,
    channel_name: str,
    root_permalink: str,
    messages: list[dict],
) -> tuple[bytes, tuple, tuple[schema.Participant, ...], str]:
    if not messages:
        raise CaptureError("Slack returned an empty thread")
    if len(messages) > MAX_THREAD_MESSAGES:
        raise CaptureError("Slack thread exceeds the message limit")
    thread_ts = messages[0].get("thread_ts") or messages[0].get("ts") or ""
    user_ids = []
    for message in messages:
        user_id = message.get("user") or message.get("bot_id") or "slack-system"
        if user_id not in user_ids:
            user_ids.append(user_id)
    semaphore = asyncio.Semaphore(PROFILE_LOOKUP_CONCURRENCY)

    async def load_name(user_id: str) -> str:
        async with semaphore:
            return await _display_name(gateway, cache, team_id, user_id)

    names = await asyncio.gather(*(load_name(user_id) for user_id in user_ids))
    speakers = dict(zip(user_ids, names, strict=True))
    participants = tuple(schema.Participant(subject=user_id, display_name=speakers[user_id]) for user_id in user_ids)

    attachment_values = []
    attachment_bytes = 0
    snapshot_reserve = min(MAX_SNAPSHOT_BYTES, max(1, schema.MAX_CAPTURE_BYTES // 2))
    attachment_budget = schema.MAX_CAPTURE_BYTES - snapshot_reserve
    snapshot_messages = []
    artifact_index = 2
    # Slack lists one upload under every message it reached; identical bytes are one artifact
    # that each of those messages references.
    shared: dict[str, SnapshotAttachment] = {}
    acquired: dict[tuple[str, str], str] = {}
    acquisition_count = 0
    for order, message in enumerate(messages, start=1):
        message_ts = str(message.get("ts") or "")
        permalink = _message_permalink(root_permalink, channel_id, message_ts)
        snapshot_attachments = []
        for item in message.get("files") or ():
            url = item.get("url_private_download") or item.get("url_private") or ""
            file_id = str(item.get("id") or "")
            acquisition_key = (file_id, url) if file_id and url else None
            cached_digest = acquired.get(acquisition_key) if acquisition_key is not None else None
            if cached_digest is None:
                if acquisition_count >= schema.MAX_ARTIFACTS - 1:
                    raise CaptureError("Slack capture exceeds the 20-artifact limit")
                data = await gateway.download_file(
                    url,
                    max_bytes=schema.MAX_ARTIFACT_BYTES,
                )
                digest = await run_blocking(evidence.sha256, data)
                acquisition_count += 1
                attachment = shared.get(digest)
            else:
                digest = cached_digest
                attachment = shared.get(digest)
                if attachment is None:
                    raise CaptureError("Slack attachment cache is inconsistent")
            if attachment is None:
                remaining = attachment_budget - attachment_bytes
                if len(data) > remaining:
                    raise CaptureError("Slack capture exceeds the capture-wide byte limit")
                if artifact_index > schema.MAX_ARTIFACTS:
                    raise CaptureError("Slack capture exceeds the 20-artifact limit")
                attachment_bytes += len(data)
                filename = item.get("name") or f"slack-file-{artifact_index}"
                media_type = await run_blocking(
                    artifacts.detect_media,
                    data,
                    declared=item.get("mimetype") or None,
                    original_name=filename,
                )
                attachment = SnapshotAttachment(
                    artifact_index=artifact_index,
                    file_id=str(item.get("id") or f"file-{artifact_index}"),
                    filename=filename,
                    media_type=media_type,
                    bytes=len(data),
                    sha256=digest,
                )
                shared[digest] = attachment
                attachment_values.append((data, media_type, filename, url))
                artifact_index += 1
            if acquisition_key is not None and cached_digest is None:
                acquired[acquisition_key] = digest
            snapshot_attachments.append(attachment)
        user_id = message.get("user") or message.get("bot_id") or "slack-system"
        snapshot_messages.append(
            SnapshotMessage(
                order=order,
                ts=message_ts,
                occurred_at=timestamp_from_slack(message_ts),
                user_id=user_id,
                speaker=speakers[user_id],
                text=str(message.get("text") or ""),
                permalink=permalink,
                attachments=tuple(snapshot_attachments),
            )
        )
    snapshot = SlackSnapshot(
        team_id=team_id,
        channel_id=channel_id,
        channel_name=channel_name or channel_id,
        thread_ts=thread_ts,
        permalink=root_permalink,
        messages=tuple(snapshot_messages),
    )
    snapshot_data = await run_blocking(canonical_bytes, snapshot)
    if len(snapshot_data) + attachment_bytes > schema.MAX_CAPTURE_BYTES:
        raise CaptureError("Slack capture exceeds the capture-wide byte limit")
    return snapshot_data, tuple(attachment_values), participants, thread_ts


def _message_permalink(root_permalink: str, channel_id: str, message_ts: str) -> str:
    try:
        parsed = urlsplit(root_permalink)
        host = (parsed.hostname or "").lower()
    except ValueError as error:
        raise CaptureError("Slack returned an invalid permalink") from error
    expected_prefix = f"/archives/{quote(channel_id, safe='')}/p"
    root_message = parsed.path.removeprefix(expected_prefix)
    if (
        parsed.scheme != "https"
        or not (host == "slack.com" or host.endswith(".slack.com"))
        or parsed.netloc.lower() != host
        or not parsed.path.startswith(expected_prefix)
        or not root_message.isdigit()
        or not _SLACK_TIMESTAMP_RE.fullmatch(message_ts)
    ):
        raise CaptureError("Slack returned an invalid permalink")
    path = f"{expected_prefix}{message_ts.replace('.', '')}"
    return urlunsplit(("https", parsed.netloc, path, parsed.query, ""))


async def _decline_server_error(ctx, channel_id: str, slack_user_id: str, thread_ts=None) -> None:
    await ctx.post_or_log(
        ctx.gateway.chat_post_ephemeral(
            channel_id,
            slack_user_id,
            blocks=render.render_capture_failed(),
            text=copy.CAPTURE_FAILED,
            thread_ts=thread_ts,
        ),
        what="capture failure",
    )


async def handle_reaction_added(
    ctx,
    *,
    reaction: str,
    team_id: str,
    channel_id: str,
    message_ts: str,
    slack_user_id: str,
    identity_result: IdentityResult,
) -> bool:
    if reaction != BRAIN_REACTION:
        return False
    if isinstance(identity_result, TransientFailure):
        await ctx.decline(
            channel_id=channel_id,
            slack_user_id=slack_user_id,
            is_dm=False,
            blocks=render.render_transient_identity_failure(),
            text=copy.TRANSIENT_IDENTITY_FAILURE,
        )
        return False
    if isinstance(identity_result, NoAccess):
        await ctx.decline(
            channel_id=channel_id,
            slack_user_id=slack_user_id,
            is_dm=False,
            blocks=render.render_no_access(is_dm=False),
            text=copy.no_access(is_dm=False),
        )
        return False
    if not isinstance(identity_result, Resolved):
        return False

    email, reader_audiences = identity_result.email, identity_result.audiences

    def authorize(service):
        channel_scope = channels.channel_scope_for_capture(
            service.conn, ctx.settings.channels_path, channel_id
        )
        capture_acl = service.check_submit_audience(
            None if channel_scope is None else list(channel_scope)
        )
        return capture_acl

    try:
        channel_meta = (await ctx.gateway.conversations_info(channel_id)).get("channel", {})
        if channel_meta.get("is_im") or channel_meta.get("is_mpim"):
            raise IdentityError("direct-message capture is not supported")
        capture_acl = await run_blocking(
            run_with_service, ctx, email, reader_audiences, authorize
        )
    except (IdentityError, SubmitRefused):
        await ctx.post_or_log(
            ctx.gateway.chat_post_ephemeral(
                channel_id,
                slack_user_id,
                blocks=render.render_private_channel_refusal(),
                text=copy.PRIVATE_CHANNEL_REFUSAL,
            ),
            what="capture audience refusal",
        )
        return False
    except Exception as error:  # noqa: BLE001
        log.error("Slack capture authorization failed (%s)", error.__class__.__name__)
        await _decline_server_error(ctx, channel_id, slack_user_id)
        return False

    reservation_id = None
    submission_attached = False

    def reserve(conn):
        nonlocal reservation_id
        with conn.transaction():
            reservation_id = reserve_reaction(
                conn,
                team_id=team_id,
                channel_id=channel_id,
                message_ts=message_ts,
                slack_user_id=slack_user_id,
                submitted_by=email,
            )
        return reservation_id

    async def release_unbound_reservation() -> None:
        if reservation_id is None or submission_attached:
            return
        await run_blocking(
            run_with_connection,
            ctx,
            lambda conn: _release_reservation(conn, reservation_id),
        )

    try:
        reservation_id = await run_blocking(run_with_connection, ctx, reserve)
        if reservation_id is None:
            return False
    except BaseException as error:  # noqa: BLE001
        await release_unbound_reservation()
        if not isinstance(error, Exception):
            raise
        log.error("Slack capture reservation failed (%s)", error.__class__.__name__)
        await _decline_server_error(ctx, channel_id, slack_user_id)
        return False

    try:
        async with asyncio.timeout(CAPTURE_ACQUISITION_TIMEOUT_S):
            messages = await ctx.gateway.conversations_replies(channel_id, message_ts)
            if not messages:
                raise CaptureError("Slack returned an empty thread")
            root_ts = str(messages[0].get("thread_ts") or messages[0].get("ts") or "")
            if not root_ts:
                raise CaptureError("Slack returned a thread without a root timestamp")
            root_permalink = await ctx.gateway.get_permalink(channel_id, root_ts)
            snapshot_data, attachments, participants, thread_ts = await _build_snapshot(
                ctx.gateway,
                ctx.cache,
                team_id=team_id,
                channel_id=channel_id,
                channel_name=channel_meta.get("name") or channel_id,
                root_permalink=root_permalink,
                messages=messages,
            )
    except BaseException as error:  # noqa: BLE001
        await release_unbound_reservation()
        if not isinstance(error, (SlackApiError, CaptureError, TimeoutError)):
            raise
        log.error("Slack capture acquisition failed (%s)", error.__class__.__name__)
        await _decline_server_error(ctx, channel_id, slack_user_id)
        return False

    def submit(service):
        nonlocal submission_attached
        with service.conn.transaction():
            if not bind_thread(
                service.conn,
                reservation_id,
                team_id=team_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                slack_user_id=slack_user_id,
            ):
                return False
            receipt = service.submit_artifacts(
                artifact_values=(
                    (
                        snapshot_data,
                        schema.MEDIA_SLACK,
                        f"slack-{thread_ts}.json",
                        root_permalink,
                    ),
                    *attachments,
                ),
                idempotency_key=(f"slack:{team_id}:{channel_id}:{thread_ts}:{slack_user_id}"),
                audience=capture_acl,
                title=f"Slack thread in #{channel_meta.get('name') or channel_id}",
                occurred_at=timestamp_from_slack(thread_ts),
                locator=root_permalink,
                participants=participants,
            )
            attach_submission(service.conn, reservation_id, receipt["id"])
        submission_attached = True
        return True

    try:
        submitted = await run_blocking(
            run_with_service, ctx, email, reader_audiences, submit
        )
        if not submitted:
            return False
    except BaseException as error:  # noqa: BLE001
        await release_unbound_reservation()
        if not isinstance(error, Exception):
            raise
        log.error("Slack capture queueing failed (%s)", error.__class__.__name__)
        await _decline_server_error(ctx, channel_id, slack_user_id, thread_ts)
        return False

    display_name = await _display_name(ctx.gateway, ctx.cache, team_id, slack_user_id)
    await ctx.post_or_log(
        ctx.gateway.chat_post_message(
            channel_id,
            blocks=render.render_capture_ack(display_name),
            text=copy.capture_ack(display_name),
            thread_ts=thread_ts,
        ),
        what="capture acknowledgement",
    )
    return True


def _release_reservation(conn, reservation_id) -> None:
    with conn.transaction():
        release_reservation(conn, reservation_id)
