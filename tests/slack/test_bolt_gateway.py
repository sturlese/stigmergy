"""`bolt_gateway.build_gateway`: the real gateway from just a bot token — the one seam a caller
outside `stigmergy.slack` needs, so that nothing outside this package imports `slack_sdk` directly.
No network call happens at construction time, so this needs no mocking."""

import asyncio

import pytest
from slack_sdk.errors import SlackApiError as SdkSlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from stigmergy.slack import snapshot
from stigmergy.slack.bolt_gateway import API_TIMEOUT_S, MAX_THREAD_PAGES, BoltSlackGateway, build_gateway
from stigmergy.slack.gateway import SlackApiError


def test_build_gateway_returns_a_bolt_slack_gateway_wrapping_the_given_token():
    gateway = build_gateway("xoxb-test-token")

    assert isinstance(gateway, BoltSlackGateway)
    assert isinstance(gateway._client, AsyncWebClient)
    assert gateway._client.token == "xoxb-test-token"
    assert gateway._client.timeout == API_TIMEOUT_S


def _run(coro):
    return asyncio.run(coro)


class _StubClient:
    """A minimal stand-in for `slack_sdk`'s `AsyncWebClient`, just enough surface for
    `reactions_add`/`reactions_remove` to raise the SDK's own `SlackApiError` with a given error
    code — the shape a real `already_reacted`/`no_reaction`/`missing_scope` response carries."""

    def __init__(self, error_code: str):
        self._error_code = error_code

    async def reactions_add(self, **kwargs):
        raise SdkSlackApiError("the request failed", {"ok": False, "error": self._error_code})

    async def reactions_remove(self, **kwargs):
        raise SdkSlackApiError("the request failed", {"ok": False, "error": self._error_code})

    async def chat_update(self, **kwargs):
        raise SdkSlackApiError("the request failed", {"ok": False, "error": self._error_code})


class _TimingOutClient:
    """A client whose failure never reaches Slack's own error vocabulary at all — a timeout or a
    connection reset, which is what `_call`'s second `except` collapses."""

    async def chat_update(self, **kwargs):
        raise TimeoutError("read timed out")


class _PaginatedRepliesClient:
    def __init__(self):
        self.calls = []

    async def conversations_replies(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("cursor"):
            return {"messages": [{"ts": "3.0"}], "response_metadata": {}}
        return {
            "messages": [{"ts": "1.0"}, {"ts": "2.0"}],
            "response_metadata": {"next_cursor": "next-page"},
        }


def test_conversations_replies_collects_every_cursor_page():
    client = _PaginatedRepliesClient()
    gateway = BoltSlackGateway(client)

    messages = _run(gateway.conversations_replies("C1", "1.0"))

    assert [message["ts"] for message in messages] == ["1.0", "2.0", "3.0"]
    assert client.calls == [
        {"channel": "C1", "ts": "1.0", "limit": 200},
        {"channel": "C1", "ts": "1.0", "limit": 200, "cursor": "next-page"},
    ]


class _OverflowRepliesClient:
    async def conversations_replies(self, **kwargs):
        if kwargs.get("cursor"):
            return {"messages": [{"ts": "overflow"}], "response_metadata": {}}
        return {
            "messages": [{"ts": str(index)} for index in range(snapshot.MAX_THREAD_MESSAGES)],
            "response_metadata": {"next_cursor": "overflow-page"},
        }


def test_conversations_replies_refuses_threads_over_the_message_limit():
    gateway = BoltSlackGateway(_OverflowRepliesClient())

    with pytest.raises(SlackApiError):
        _run(gateway.conversations_replies("C1", "1.0"))


class _MaximumRepliesClient:
    def __init__(self):
        self.calls = 0

    async def conversations_replies(self, **kwargs):
        self.calls += 1
        start = (self.calls - 1) * 200
        size = min(200, snapshot.MAX_THREAD_MESSAGES - start)
        next_cursor = f"page-{self.calls + 1}" if start + size < snapshot.MAX_THREAD_MESSAGES else ""
        return {
            "messages": [{"ts": str(index)} for index in range(start, start + size)],
            "response_metadata": {"next_cursor": next_cursor},
        }


def test_conversations_replies_accepts_the_message_limit_across_pages():
    client = _MaximumRepliesClient()
    gateway = BoltSlackGateway(client)

    messages = _run(gateway.conversations_replies("C1", "1.0"))

    assert len(messages) == snapshot.MAX_THREAD_MESSAGES
    assert client.calls == 3


class _EmptyContinuedRepliesClient:
    def __init__(self):
        self.calls = 0

    async def conversations_replies(self, **kwargs):
        self.calls += 1
        return {"messages": [], "response_metadata": {"next_cursor": f"cursor-{self.calls}"}}


def test_conversations_replies_rejects_an_empty_continued_page():
    client = _EmptyContinuedRepliesClient()
    gateway = BoltSlackGateway(client)

    with pytest.raises(SlackApiError, match="empty continued"):
        _run(gateway.conversations_replies("C1", "1.0"))

    assert client.calls == 1


class _UnboundedCursorRepliesClient:
    def __init__(self):
        self.calls = 0

    async def conversations_replies(self, **kwargs):
        self.calls += 1
        return {
            "messages": [{"ts": str(self.calls)}],
            "response_metadata": {"next_cursor": f"cursor-{self.calls}"},
        }


def test_conversations_replies_bounds_pagination_work():
    client = _UnboundedCursorRepliesClient()
    gateway = BoltSlackGateway(client)

    with pytest.raises(SlackApiError, match="pagination-work limit"):
        _run(gateway.conversations_replies("C1", "1.0"))

    assert client.calls == MAX_THREAD_PAGES


class _ReplyRepliesClient:
    def __init__(self):
        self.calls = []

    async def conversations_replies(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["ts"] == "2.0":
            return {"messages": [{"ts": "2.0", "thread_ts": "1.0"}]}
        return {"messages": [{"ts": "1.0"}, {"ts": "2.0", "thread_ts": "1.0"}]}


def test_conversations_replies_resolves_a_reply_to_its_complete_thread():
    client = _ReplyRepliesClient()
    gateway = BoltSlackGateway(client)

    messages = _run(gateway.conversations_replies("C1", "2.0"))

    assert [message["ts"] for message in messages] == ["1.0", "2.0"]
    assert client.calls == [
        {"channel": "C1", "ts": "2.0", "limit": 200},
        {"channel": "C1", "ts": "1.0", "limit": 200},
    ]


# ── the reaction lifecycle's own benign redelivery outcomes are not failures at all ─────────────
def test_reactions_add_treats_already_reacted_as_success_not_a_failure():
    """Event redelivery can retry an add that already landed — `already_reacted` is Slack's own
    honest "already in the state we wanted" answer, not an API failure, so it never reaches a
    caller as `SlackApiError` (the `tolerate` map's whole purpose)."""
    gateway = BoltSlackGateway(_StubClient("already_reacted"))
    result = _run(gateway.reactions_add("C1", "1.1", "hourglass_flowing_sand"))
    assert result == {"ok": True}


def test_reactions_remove_treats_no_reaction_as_success_not_a_failure():
    """The remove-side twin: a previous cleanup (or a redelivery) already took the reaction off —
    `no_reaction` is likewise not surfaced as a failure."""
    gateway = BoltSlackGateway(_StubClient("no_reaction"))
    result = _run(gateway.reactions_remove("C1", "1.1", "hourglass_flowing_sand"))
    assert result == {"ok": True}


# ── every OTHER failure, missing_scope included, still raises — this package's ONE exception ────
def test_reactions_add_still_raises_slack_api_error_for_a_missing_scope():
    """An operator whose Slack app lacks `reactions:write` must not lose captures — this is what
    makes that true one layer down: `missing_scope` is a REAL failure (not redelivery-benign), so
    it still becomes `SlackApiError`, which every caller in `stigmergy.slack` already treats as
    best-effort and swallows (`capture._react_or_log`)."""
    gateway = BoltSlackGateway(_StubClient("missing_scope"))
    with pytest.raises(SlackApiError):
        _run(gateway.reactions_add("C1", "1.1", "hourglass_flowing_sand"))


def test_reactions_remove_still_raises_slack_api_error_for_a_missing_scope():
    gateway = BoltSlackGateway(_StubClient("missing_scope"))
    with pytest.raises(SlackApiError):
        _run(gateway.reactions_remove("C1", "1.1", "hourglass_flowing_sand"))


# ── the error CODE survives the collapse, because a caller decides retry-or-not on it ───────────
def test_a_failed_call_carries_slacks_own_error_code_onto_slack_api_error():
    """`doorbell` classifies `message_not_found` as terminal and stops re-editing that card once
    per poll pass forever. That decision is only real if the code actually reaches it — `str(ex)`
    is prose the SDK assembles, and nothing may go pattern-matching it. Proven at the REAL
    gateway's own boundary: the offline double raising a coded error proves only the double."""
    gateway = BoltSlackGateway(_StubClient("message_not_found"))

    with pytest.raises(SlackApiError) as raised:
        _run(gateway.chat_update("C1", "1.1", text="closed"))

    assert raised.value.code == "message_not_found"


def test_a_failure_that_never_reached_slack_carries_no_code_at_all():
    """The benign twin, and the one that matters most: a timeout has no Slack error code, so it
    must read as `""`. Anything else would classify an outage as permanent and leave a perfectly
    editable card live with its buttons, marked as unreachable."""
    gateway = BoltSlackGateway(_TimingOutClient())

    with pytest.raises(SlackApiError) as raised:
        _run(gateway.chat_update("C1", "1.1", text="closed"))

    assert raised.value.code == ""
