import asyncio
from types import SimpleNamespace

from stigmergy.capture import schema
from stigmergy.slack import poller
from stigmergy.slack.gateway import SlackApiError


def _row(*, row_id: str, status: str, summary: str = "", error: str = "") -> dict:
    return {
        "id": row_id,
        "submission_id": f"submission-{row_id}",
        "status": status,
        "report": {"summary": summary} if summary else {},
        "error": error,
        "channel_id": "C1",
        "thread_ts": "1.0",
    }


class _Gateway:
    def __init__(self, *, fail_first: bool = False):
        self.fail_first = fail_first
        self.posts = []

    async def chat_post_message(self, channel_id, **kwargs):
        if self.fail_first:
            self.fail_first = False
            raise SlackApiError("temporary Slack outage")
        self.posts.append((channel_id, kwargs))


def test_terminal_reports_use_the_landed_summary_or_safe_failure_copy():
    landed_blocks, landed_text = poller._blocks_for(
        _row(row_id="landed", status=schema.LANDED, summary="Filed the renewal decision")
    )
    failed_blocks, failed_text = poller._blocks_for(
        _row(row_id="failed", status=schema.FAILED, error="The attachment was invalid")
    )

    assert "Filed the renewal decision" in str(landed_blocks)
    assert "The attachment was invalid" in str(failed_blocks)
    assert "landed" in landed_text.lower()
    assert "failed" in failed_text.lower()


def test_poll_once_marks_only_reports_successfully_delivered(monkeypatch):
    rows = [
        _row(row_id="first", status=schema.LANDED),
        _row(row_id="second", status=schema.FAILED),
    ]
    marked = []
    gateway = _Gateway(fail_first=True)
    monkeypatch.setattr(poller, "due_for_report", lambda _conn: rows)
    monkeypatch.setattr(
        poller,
        "mark_reported",
        lambda _conn, row_id, status: marked.append((row_id, status)),
    )

    reported = asyncio.run(poller.poll_once(SimpleNamespace(conn=object(), gateway=gateway)))

    assert reported == 1
    assert marked == [("second", schema.FAILED)]
    assert gateway.posts[0][0] == "C1"
    assert gateway.posts[0][1]["thread_ts"] == "1.0"


def test_poller_survives_a_failed_pass_and_stops_cleanly(monkeypatch):
    stop = asyncio.Event()

    async def fail_once(_ctx):
        stop.set()
        raise RuntimeError("unexpected pass failure")

    monkeypatch.setattr(poller, "poll_once", fail_once)

    asyncio.run(poller.run_poller(object(), interval_s=0, stop_event=stop))


def test_poller_wait_timeout_starts_the_next_pass(monkeypatch):
    stop = asyncio.Event()
    calls = 0

    async def two_passes(_ctx):
        nonlocal calls
        calls += 1
        if calls == 2:
            stop.set()
        return 0

    monkeypatch.setattr(poller, "poll_once", two_passes)

    asyncio.run(poller.run_poller(object(), interval_s=0, stop_event=stop))

    assert calls == 2
