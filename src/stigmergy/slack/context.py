"""`SlackContext` — the process-wide resources every handler shares, built ONCE at startup
(`app.build_context`) and threaded through every event. What differs per event is only the
resolved identity a `BrainService` is built with, never the shared resources.
"""
import logging
import time
import uuid
from dataclasses import dataclass, field

from stigmergy.server import ops_files
from stigmergy.server.service import SLACK_DOOR, BrainService
from stigmergy.slack.gateway import SlackApiError, SlackGateway
from stigmergy.slack.identity import UsersInfoCache, resolve_slack_identity
from stigmergy.slack.settings import SlackSettings, no_link_resolver

log = logging.getLogger(__name__)

# How long a "Show it here" button stays live: long enough to click in the same conversation,
# short enough that a token scraped from `conversations.history` later has stopped working.
SHOW_IT_HERE_TOKEN_TTL_S = 3600

# Bounded like `UsersInfoCache` — max size, oldest-first eviction on insert, alongside the TTL.
# A bound, not a tune: no real workspace mints anywhere near this many live tokens.
SHOW_IT_HERE_MAX_TOKENS = 10_000


def short_ref() -> str:
    """Return an opaque correlation token safe for logs and user-facing errors."""
    return uuid.uuid4().hex[:8]


@dataclass
class SlackContext:
    settings: SlackSettings
    gateway: SlackGateway
    conn: object
    embedder: object
    rate_limiter: object = None
    audit: object = None
    evidence: object = None
    cache: UsersInfoCache = field(default_factory=UsersInfoCache)
    link_resolver: object = no_link_resolver
    # `token -> (path, owner_slack_user_id, expires_at)`. A button value is retrievable by any
    # workspace member via `conversations.history`, so it must never carry the path or an email in
    # cleartext — the token is an IDENTIFIER, not a credential, and `handle_show_it_here`
    # re-resolves the clicker independently.
    _show_it_here_tokens: dict = field(default_factory=dict)
    # Injectable to support smaller deployments without changing the process-wide default.
    _show_it_here_max_tokens: int = SHOW_IT_HERE_MAX_TOKENS
    # Injectable clock, the same seam `UsersInfoCache(clock=...)` uses.
    _clock: object = time.monotonic

    async def resolve_slack_identity(self, *, event_team_id: str, slack_user_id: str):
        """The ONE identity call every handler makes before building a `BrainService`, with the
        configured workspace and the identities file taken off these settings. `event_team_id` is
        the EVENT's own workspace and is the caller's to source: passing the configured
        `settings.team_id` here would bypass the workspace boundary."""
        return await resolve_slack_identity(
            self.gateway, self.cache, identities_path=self.settings.server.identities_path,
            configured_team_id=self.settings.team_id, event_team_id=event_team_id,
            slack_user_id=slack_user_id, conn=self.conn)

    def build_service(self, email: str, audiences, *, rate_limited: bool = True) -> BrainService:
        """A per-identity `BrainService` sharing every process-wide resource. `audiences` accepts
        `None` (unrestricted) or any iterable of labels.

        `rate_limited=False` is for SYSTEM-initiated work the asker did not request
        (`mention._maybe_dm_fuller_answer`): spending their own budget on it would make an asker
        for whom content was withheld observably likelier to hit the rate-limit message on their
        next real question. `identity=email` is unchanged either way, so audit attribution stays
        the same."""
        principal = ops_files.resolve_identity_principal(
            self.conn,
            self.settings.server.identities_path,
            email,
        )
        aud = None if audiences is None else set(audiences)
        # `door`: the Slack transport is the one door whose `source_*` hints are composed by
        # server code from Slack's API responses — `_submit` accepts them here and refuses them
        # from every client-facing service (`capture.schema.reject_source_provenance_hints`).
        return BrainService(self.settings.server, self.conn, self.embedder, aud, identity=email,
                            rate_limiter=self.rate_limiter if rate_limited else None,
                            audit=self.audit, evidence=self.evidence, door=SLACK_DOOR,
                            principal=principal)

    async def decline(self, *, channel_id: str, slack_user_id: str, is_dm: bool, blocks: list,
                      text: str, thread_ts: str | None = None) -> None:
        """The ONE way this package declines a request for an identity reason (`NoAccess`,
        `TransientFailure`): ephemeral in a channel — an identity failure must never be disclosed
        to the whole channel — and a real message when the surface itself IS a DM (Slack has no
        "ephemeral to yourself" there). Routed through `post_or_log`: a Slack outage while
        declining degrades, never raises out of the handler."""
        if is_dm:
            await self.post_or_log(
                self.gateway.chat_post_message(channel_id, blocks=blocks, text=text,
                                               thread_ts=thread_ts),
                what=f"decline (message) in {channel_id}")
        else:
            await self.post_or_log(
                self.gateway.chat_post_ephemeral(channel_id, slack_user_id, blocks=blocks,
                                                 text=text, thread_ts=thread_ts),
                what=f"decline (ephemeral) in {channel_id}")

    async def post_or_log(self, coro, *, what: str) -> dict | None:
        """The ONE seam every NON-CRITICAL Slack send goes through: a `SlackApiError` anywhere in
        `coro` is logged and swallowed, never raised. Returns the gateway's response, or `None` on
        a caught failure — a caller that needs the response (the placeholder's own `ts`) keeps its
        own policy and does not use this seam (`mention._edit_or_fallback`)."""
        try:
            return await coro
        except SlackApiError as error:
            log.error("slack: %s failed (%s)", what, error.__class__.__name__)
            return None

    def mint_show_it_here_token(self, path: str, owner_slack_user_id: str) -> str:
        """An opaque per-answer token for the "Show it here" button's value — injected into
        `render` as `mint_token`, so that module stays free of any notion of a token STORE.
        `(path, owner_slack_user_id)` lives ONLY here, server-side. Bounded at
        `self._show_it_here_max_tokens`, oldest-first eviction on insert."""
        token = uuid.uuid4().hex
        if (token not in self._show_it_here_tokens
                and len(self._show_it_here_tokens) >= self._show_it_here_max_tokens):
            del self._show_it_here_tokens[next(iter(self._show_it_here_tokens))]   # oldest-first
        self._show_it_here_tokens[token] = (path, owner_slack_user_id,
                                            time.monotonic() + SHOW_IT_HERE_TOKEN_TTL_S)
        return token

    def consume_show_it_here_token(self, token: str) -> tuple[str, str] | None:
        """`(path, owner_slack_user_id)` for a live token, `None` for an unknown or expired one.
        Not single-use — the button may legitimately be clicked more than once; entries are
        dropped by TTL expiry, never by having been read."""
        entry = self._show_it_here_tokens.get(token)
        if entry is None:
            return None
        path, owner_slack_user_id, expires_at = entry
        if time.monotonic() >= expires_at:
            del self._show_it_here_tokens[token]
            return None
        return path, owner_slack_user_id
