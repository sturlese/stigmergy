"""Terminal capture reports delivered back to the originating Slack thread."""

import asyncio
import logging

from stigmergy.capture import schema
from stigmergy.slack import copy, render
from stigmergy.slack.gateway import SlackApiError
from stigmergy.slack.store import due_for_report, mark_reported

log = logging.getLogger(__name__)
DEFAULT_POLL_INTERVAL_S = 5


def _blocks_for(row: dict) -> tuple[list[dict], str]:
    status = row["status"]
    report = row["report"] or {}
    if status == schema.LANDED:
        summary = report.get("summary") or "The capture landed in the team wiki."
    else:
        summary = row["error"] or "The capture could not be processed."
    return render.render_generic_report(status, summary), copy.report_fallback(status)


async def poll_once(ctx) -> int:
    reported = 0
    for row in due_for_report(ctx.conn):
        try:
            blocks, text = _blocks_for(row)
            await ctx.gateway.chat_post_message(
                row["channel_id"],
                blocks=blocks,
                text=text,
                thread_ts=row["thread_ts"],
            )
        except SlackApiError as error:
            log.error(
                "slack terminal report failed",
                extra={
                    "submission_id": str(row["submission_id"]),
                    "error_class": error.__class__.__name__,
                },
            )
            continue
        mark_reported(ctx.conn, row["id"], row["status"])
        reported += 1
    return reported


async def run_poller(
    ctx,
    *,
    interval_s: int = DEFAULT_POLL_INTERVAL_S,
    stop_event: asyncio.Event | None = None,
) -> None:
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        try:
            await poll_once(ctx)
        except Exception as error:  # noqa: BLE001
            log.error("slack poller pass failed (%s)", error.__class__.__name__)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except TimeoutError:
            pass
