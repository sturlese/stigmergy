"""The push channel: outcomes for a Slack-originated capture.

**Read-only against `capture_queue`** — the poller must not claim, lease or mutate a queue row;
that is the worker's job. Every read here goes through `stigmergy.slack.store.due_for_report`,
itself a plain `SELECT` joined to `capture_queue`; the only WRITE this module ever performs is
`mark_reported`, against `slack_submissions`, never against the queue.

**Runs in the bot's own process, not a machine of its own**: `run_poller` is a plain `asyncio`
loop `stigmergy.slack.app` starts as a background task alongside the socket-mode connection, on the
SAME `slack` process group.

Handles every terminal and parked state the queue's vocabulary (`capture.schema.STATUSES`) has —
`filed`, `needs_input`, `triage`, `rejected`, `resolved` and `failed`. On a state change the rule
is the same for all of them: say so plainly, with the reason the server gives and nothing added
to it.
"""
import asyncio
import logging

from stigmergy.slack import copy, render
from stigmergy.slack.gateway import SlackApiError
from stigmergy.slack.store import FILED, NEEDS_INPUT, due_for_report, mark_reported

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_S = 5


def _needs_input_prose(report: dict) -> str:
    """Reuse `report.needs_input()`'s situation-describing prose verbatim, swapping ONLY the
    trailing MCP invocation clause — a rendering-layer swap, not a second template.
    `reply_invocation` is the exact field `librarian/report.py` stores beside the sentence for
    precisely this kind of consumer (the same fact-beside-the-sentence shape as `reason_code`)."""
    summary = report.get("summary", "")
    invocation = report.get("reply_invocation", "")
    suffix = f"\n\nReply with:\n  {invocation}"
    return summary[:-len(suffix)] if invocation and summary.endswith(suffix) else summary


def _blocks_for(status: str, report: dict, result_ref: str) -> tuple[list[dict], str]:
    """`(blocks, plain_text_fallback)` for one reportable status. `needs_input` needs the row's
    `slack_user_id` for its @-mention, which the caller supplies separately (this function only
    shapes what the REPORT itself determines)."""
    if status == FILED:
        page_path = report.get("page_path", "") or result_ref
        commit = report.get("commit", "")
        anchor = report.get("anchored_to", "") or "(none)"
        # `source_pages` part 1 is the head of the chain — the card names one page, the same one
        # the synthesis's `sources:` cites; further parts are reachable from it.
        source_page = (report.get("source_pages") or [""])[0]
        blocks = render.render_filed(page_path=page_path, commit=commit, anchor=anchor,
                                     source_page=source_page)
        return blocks, f"filed: {page_path}"
    # triage / rejected / resolved / failed: reuse the report's own sentence, bold-prefixed.
    blocks = render.render_generic_report(status, report.get("summary", ""))
    return blocks, f"{status}: capture update"


async def poll_once(ctx) -> int:
    """One pass: every Slack-originated submission whose state changed reports exactly once.
    Returns how many were reported (test seam — `tests/slack/test_poller.py` asserts on this)."""
    reported = 0
    for row in due_for_report(ctx.conn):
        status = row["status"]
        report = row["report"] or {}
        try:
            if status == NEEDS_INPUT:
                prose = _needs_input_prose(report)
                blocks = render.render_needs_input(situation_prose=prose,
                                                    slack_user_id=row["slack_user_id"])
                text = copy.needs_input_body(prose, slack_user_id=row["slack_user_id"])
            else:
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
