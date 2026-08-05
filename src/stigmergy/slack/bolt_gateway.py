"""`BoltSlackGateway` — the real `SlackGateway`, wrapping `slack_sdk`'s `AsyncWebClient`.

Kept in its own module and imported lazily, so every other module in this package — and every test
in `tests/slack/` except the two that exercise this gateway and the process wiring themselves —
needs no `slack_sdk` import at all, matching the offline-first posture the rest of `stigmergy.slack`
takes (package docstring). Every `slack_sdk` failure — the SDK's own `SlackApiError`, a timeout, a
connection reset — is collapsed to this package's own `SlackApiError`, so no caller anywhere in
`stigmergy.slack` needs to know `slack_sdk`'s exception shape.
"""
from stigmergy.slack.gateway import SlackApiError


class BoltSlackGateway:
    def __init__(self, client) -> None:
        self._client = client   # a slack_sdk.web.async_client.AsyncWebClient

    async def _call(self, method, **kwargs):
        from slack_sdk.errors import SlackApiError as SdkSlackApiError
        try:
            return await method(**kwargs)
        except SdkSlackApiError as ex:
            raise SlackApiError(str(ex)) from ex
        except Exception as ex:  # noqa: BLE001 — a timeout/connection reset is an API failure too
            raise SlackApiError(f"{ex.__class__.__name__}: {ex}") from ex

    async def users_info(self, user_id: str) -> dict:
        return await self._call(self._client.users_info, user=user_id)

    async def conversations_info(self, channel_id: str) -> dict:
        return await self._call(self._client.conversations_info, channel=channel_id)

    async def conversations_replies(self, channel_id: str, thread_ts: str) -> list[dict]:
        result = await self._call(self._client.conversations_replies, channel=channel_id,
                                  ts=thread_ts)
        return result.get("messages", [])

    async def get_permalink(self, channel_id: str, message_ts: str) -> str:
        result = await self._call(self._client.chat_getPermalink, channel=channel_id,
                                  message_ts=message_ts)
        return result.get("permalink", "")

    async def chat_post_message(self, channel_id: str, *, text: str = "",
                               blocks: list | None = None, thread_ts: str | None = None) -> dict:
        return await self._call(self._client.chat_postMessage, channel=channel_id, text=text,
                                blocks=blocks, thread_ts=thread_ts)

    async def chat_update(self, channel_id: str, ts: str, *, text: str = "",
                         blocks: list | None = None) -> dict:
        return await self._call(self._client.chat_update, channel=channel_id, ts=ts, text=text,
                                blocks=blocks)

    async def chat_post_ephemeral(self, channel_id: str, user_id: str, *, text: str = "",
                                 blocks: list | None = None, thread_ts: str | None = None) -> dict:
        return await self._call(self._client.chat_postEphemeral, channel=channel_id, user=user_id,
                                text=text, blocks=blocks, thread_ts=thread_ts)

    async def reactions_add(self, channel_id: str, message_ts: str, name: str) -> dict:
        """`already_reacted` is Slack's own answer when the reaction is already there — an event
        redelivery reaches this — an honest "already in the state we wanted", not a failure, so it
        is translated to a successful response here rather than let `SlackGateway.reactions_add`'s
        contract be broken by a caller having to special-case it. Same posture
        `users_lookup_by_email` takes for `users_not_found`. Every OTHER failure (a missing
        `reactions:write` scope, a timeout, a rate limit) still becomes `SlackApiError`."""
        from slack_sdk.errors import SlackApiError as SdkSlackApiError
        try:
            return await self._client.reactions_add(channel=channel_id, timestamp=message_ts,
                                                    name=name)
        except SdkSlackApiError as ex:
            if getattr(ex, "response", None) is not None and ex.response.get("error") == "already_reacted":
                return {"ok": True}
            raise SlackApiError(str(ex)) from ex
        except Exception as ex:  # noqa: BLE001 — a timeout/connection reset is an API failure too
            raise SlackApiError(f"{ex.__class__.__name__}: {ex}") from ex

    async def reactions_remove(self, channel_id: str, message_ts: str, name: str) -> dict:
        """`no_reaction` is Slack's own answer when the reaction is already gone — a previous
        cleanup attempt, or a redelivery — translated to success the same way `reactions_add`
        translates `already_reacted`."""
        from slack_sdk.errors import SlackApiError as SdkSlackApiError
        try:
            return await self._client.reactions_remove(channel=channel_id, timestamp=message_ts,
                                                       name=name)
        except SdkSlackApiError as ex:
            if getattr(ex, "response", None) is not None and ex.response.get("error") == "no_reaction":
                return {"ok": True}
            raise SlackApiError(str(ex)) from ex
        except Exception as ex:  # noqa: BLE001 — a timeout/connection reset is an API failure too
            raise SlackApiError(f"{ex.__class__.__name__}: {ex}") from ex

    async def users_lookup_by_email(self, email: str) -> dict | None:
        """`users_not_found` is Slack's OWN documented error code for "no workspace member has
        this email" — an honest negative answer, not an API failure, so it is translated to `None`
        here rather than let `_call`'s blanket `SlackApiError` collapse hide the distinction
        `SlackGateway.users_lookup_by_email`'s own contract requires. Every OTHER SDK failure (a
        timeout, a rate limit, a malformed response) still becomes `SlackApiError` exactly like
        every other method on this class."""
        from slack_sdk.errors import SlackApiError as SdkSlackApiError
        try:
            return await self._client.users_lookupByEmail(email=email)
        except SdkSlackApiError as ex:
            if getattr(ex, "response", None) is not None and ex.response.get("error") == "users_not_found":
                return None
            raise SlackApiError(str(ex)) from ex
        except Exception as ex:  # noqa: BLE001 — a timeout/connection reset is an API failure too
            raise SlackApiError(f"{ex.__class__.__name__}: {ex}") from ex

    async def views_open(self, *, trigger_id: str, view: dict) -> dict:
        return await self._call(self._client.views_open, trigger_id=trigger_id, view=view)


def build_gateway(bot_token: str) -> BoltSlackGateway:
    """The real gateway from just a bot token — the one seam a caller OUTSIDE this package needs
    when it only ever posts (never listens for events, so it needs none of `SlackSettings`'s other
    three secrets). `stigmergy.gardener.cli`, `stigmergy.digest.cli` and `stigmergy.admin.service` are
    such callers. This exists so those modules can reach a real, working gateway without importing
    the web-client SDK themselves, which would put this package's own identifier vocabulary and its
    only SDK dependency outside its own boundary (`tests/test_architecture.py::
    test_no_slack_identifiers_below_the_slack_package`). `stigmergy.slack.app.build_context` stays
    the fuller constructor for the long-running Socket Mode process; this is its one-secret sibling
    for an on-demand poster."""
    from slack_sdk.web.async_client import AsyncWebClient
    return BoltSlackGateway(AsyncWebClient(token=bot_token))
