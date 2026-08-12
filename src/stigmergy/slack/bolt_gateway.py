"""`BoltSlackGateway` — the real `SlackGateway`, wrapping `slack_sdk`'s `AsyncWebClient`.

In its own module, imported lazily, so nothing else in this package needs `slack_sdk` at all.
Every SDK failure — the SDK's own `SlackApiError`, a timeout, a connection reset — collapses to
this package's own `SlackApiError`, so no caller needs to know `slack_sdk`'s exception shape.
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
        """`already_reacted` (an event redelivery reaches this) is Slack saying "already in the
        state we wanted" — translated to success; every OTHER failure still becomes
        `SlackApiError`."""
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
        """`no_reaction` (the reaction already gone — a previous cleanup, or a redelivery) is
        translated to success the same way `reactions_add` translates `already_reacted`."""
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
        """`users_not_found` is Slack's honest "no workspace member has this email" — translated
        to `None`, the distinction `SlackGateway.users_lookup_by_email`'s contract requires; every
        OTHER SDK failure still becomes `SlackApiError`."""
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
    """The real gateway from just a bot token — for callers outside this package that only post
    (`gardener.cli`, `digest.cli`, `admin.service`) and must not import the SDK themselves.
    `stigmergy.slack.app.build_context` stays the full constructor for the Socket Mode process."""
    from slack_sdk.web.async_client import AsyncWebClient
    return BoltSlackGateway(AsyncWebClient(token=bot_token))
