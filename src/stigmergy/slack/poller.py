"""The push channel: outcomes for a Slack-originated capture, reported to its origin thread.

**Read-only against `capture_queue`** — never a claim, lease or mutation; the only write is
`mark_reported`, against `slack_submissions`. Runs as a background `asyncio` task in the bot's
own process. Every terminal status is reported the same way: say so plainly, with the reason the
server gives and nothing added to it. Nothing is ever asked of the submitter.
"""
import asyncio
import logging

from stigmergy.slack import copy, render
from stigmergy.slack.gateway import SlackApiError
from stigmergy.slack.store import FILED, due_for_report, mark_reported

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_S = 5


def _blocks_for(status: str, report: dict, result_ref: str) -> tuple[list[dict], str]:
    """`(blocks, plain_text_fallback)` for one reportable status."""
    if status == FILED:
        page_path = report.get("page_path", "") or result_ref
        commit = report.get("commit", "")
        anchor = report.get("anchored_to", "") or "(none)"
        # `source_pages` part 1 is the head of the chain — the same page the synthesis's
        # `sources:` cites; further parts are reachable from it.
        source_page = (report.get("source_pages") or [""])[0]
        blocks = render.render_filed(page_path=page_path, commit=commit, anchor=anchor,
                                     source_page=source_page,
                                     anchor_reason=report.get("anchor_reason", ""),
                                     proposed=[str(e.get("name") or e.get("id") or "")
                                               for e in (report.get("entities_proposed") or ())
                                               if isinstance(e, dict)])
        return blocks, copy.filed_fallback(page_path=page_path)
    # rejected / resolved / failed: reuse the report's own sentence, bold-prefixed.
    blocks = render.render_generic_report(status, report.get("summary", ""))
    return blocks, copy.report_fallback(status)


async def poll_once(ctx) -> int:
    """One pass: every Slack-originated submission whose state changed reports exactly once.
    Returns how many were reported (test seam — `tests/slack/test_poller.py` asserts on this)."""
    reported = 0
    for row in due_for_report(ctx.conn):
        status = row["status"]
        report = row["report"] or {}
        try:
            blocks, text = _blocks_for(status, report, row["result_ref"] or "")
            await ctx.gateway.chat_post_message(row["channel_id"], blocks=blocks, text=text,
                                                thread_ts=row["thread_ts"])
        except SlackApiError:
            log.error("slack poller: could not post the %s report for submission %s",
                     status, row["submission_id"], exc_info=True)
            continue   # try again next pass — `last_status` is only updated on a successful post
        mark_reported(ctx.conn, row["id"], status)
        reported += 1
    return reported


async def run_poller(ctx, *, interval_s: int = DEFAULT_POLL_INTERVAL_S,
                     stop_event: asyncio.Event | None = None) -> None:
    """The loop `stigmergy.slack.app` runs as a background task. `stop_event` lets a test (or a
    graceful shutdown) end the loop after a bounded number of passes instead of running forever."""
    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        try:
            await poll_once(ctx)
        except Exception:  # noqa: BLE001 — one bad pass must never kill the poller's process
            log.error("slack poller: pass failed", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except TimeoutError:
            pass
