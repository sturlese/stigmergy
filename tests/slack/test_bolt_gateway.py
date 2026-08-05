"""`bolt_gateway.build_gateway`: the real gateway from just a bot token — the one seam a caller
outside `stigmergy.slack` needs, so that nothing outside this package imports `slack_sdk` directly.
No network call happens at construction time, so this needs no mocking."""
import asyncio

import pytest
from slack_sdk.errors import SlackApiError as SdkSlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from stigmergy.slack.bolt_gateway import BoltSlackGateway, build_gateway
from stigmergy.slack.gateway import SlackApiError


def test_build_gateway_returns_a_bolt_slack_gateway_wrapping_the_given_token():
    gateway = build_gateway("xoxb-test-token")

    assert isinstance(gateway, BoltSlackGateway)
    assert isinstance(gateway._client, AsyncWebClient)
    assert gateway._client.token == "xoxb-test-token"


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


# ── the reaction lifecycle's own benign redelivery outcomes are not failures at all ─────────────
def test_reactions_add_treats_already_reacted_as_success_not_a_failure():
    """Event redelivery can retry an add that already landed — `already_reacted` is Slack's own
    honest "already in the state we wanted" answer, not an API failure, so it never reaches a
    caller as `SlackApiError` (mirroring `users_lookup_by_email`'s `users_not_found` collapse)."""
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
