"""`SlackContext` — the process-wide resources every handler in this package shares, built ONCE at
startup (`stigmergy.slack.app.build_context`) and threaded through every event: the shared Postgres
connection and embedder, the rate limiter and audit writer, the identity cache, and the link
resolver. Same shape as `transport_http._BearerAuthMiddleware`'s constructor (module docstring:
"Sharing one Postgres connection and one query embedder across every identity is safe here") —
what differs per event is only the resolved identity a `BrainService` is built with, never the
shared resources themselves.
"""
import logging
import time
import uuid
from dataclasses import dataclass, field

from stigmergy.server.service import SLACK_DOOR, BrainService
from stigmergy.slack.gateway import SlackApiError
from stigmergy.slack.identity import UsersInfoCache
from stigmergy.slack.settings import SlackSettings, no_link_resolver

log = logging.getLogger(__name__)

# How long a "Show it here" button stays live. Long enough for someone to actually click it in the
# same conversation; short enough that a token scraped out of `conversations.history` well after
# the fact has stopped working by the time anyone could misuse it.
SHOW_IT_HERE_TOKEN_TTL_S = 3600

# The same treatment `UsersInfoCache` gets — a max size with oldest-first eviction on insert,
# alongside the TTL above. This dict lives for the process's whole lifetime (weeks), and without a
# bound it can only grow for as long as the process runs. Not a tune: no real workspace mints
# anywhere near this many live tokens, so it never fires in practice.
SHOW_IT_HERE_MAX_TOKENS = 10_000


@dataclass
class SlackContext:
    settings: SlackSettings
    gateway: object                    # a SlackGateway
    conn: object
    embedder: object
    rate_limiter: object = None
    audit: object = None
    evidence: object = None
    cache: UsersInfoCache = field(default_factory=UsersInfoCache)
    link_resolver: object = no_link_resolver
    bot_user_id: str = ""
    # `token -> (path, owner_slack_user_id, expires_at)`. The "Show it here" button's value must
    # never carry `{"path": ..., "asker_email": ...}` in cleartext — a button value is retrievable
    # by any workspace member via `conversations.history` and by any other app with history scope.
    # A short-TTL dict on the context is enough: the button's value is an opaque token that names
    # nothing on its own, and `handle_show_it_here` re-resolves the clicker independently, so this
    # store makes the token an IDENTIFIER, not a credential.
    _show_it_here_tokens: dict = field(default_factory=dict)
    # Injectable (like `UsersInfoCache(max_entries=...)`) so a test can bound it small without
    # minting thousands of real tokens.
    _show_it_here_max_tokens: int = SHOW_IT_HERE_MAX_TOKENS
    # Has `doorbell.poll_once` already logged and recorded a deployment-wide "nothing can ever
    # ring" fault — no stewards source at all, an empty map, or one that cannot be loaded — for
    # this process? ONE flag for all three because they are mutually exclusive for the whole
    # process lifetime (`Settings` is frozen at startup), so none can consume another's
    # one-shot. A per-item, per-pass `record_undeliverable` write for "the map is empty"
    # turns one global misconfiguration fact into N-items x 8,640-passes/day of identical
    # `job_runs` rows — an unbounded write the doorbell exists to bound, not to cause. This flag
    # makes that ONE fact logged and recorded once per process lifetime rather than once per item
    # per pass; it resets naturally the next time the process restarts (or a fresh `SlackContext`
    # is built, as every test does).
    _stewards_empty_warned: bool = False
    # `doorbell._load_stewards_cached`'s own TTL cache for `ops/stewards.json` — `{"repo": str,
    # "loaded_at": float, "map": dict}` or `{}` before the first load. Uncached,
    # `review.load_stewards` is a real `git fetch origin main` on EVERY poll pass. This cache is
    # LOCAL to the doorbell's own steward resolution and must never be reused for `review_decide`'s
    # authorization check (`review._is_steward`), which calls `review.load_stewards` directly and
    # stays on its own always-fresh read on purpose: a revoked steward's approval must never
    # succeed off a stale cache.
    _stewards_cache: dict = field(default_factory=dict)
    # Injectable, like `UsersInfoCache(clock=...)` — the SAME seam this package already uses for a
    # TTL a test needs to move without sleeping or monkeypatching `time` globally. Only
    # `doorbell._load_stewards_cached` reads it.
    _clock: object = time.monotonic

    def build_service(self, email: str, audiences, *, rate_limited: bool = True) -> BrainService:
        """A per-identity `BrainService`, sharing every process-wide resource — the same
        construction `transport_http.py` does per request. `audiences` accepts `None`
        (unrestricted), a `frozenset`/`set` (a resolved identity's own scope), or any iterable of
        labels (a channel's scope, always a plain `set` per `channels.channel_audiences`).

        `rate_limited=False` builds a service that shares every OTHER process-wide resource but
        never touches `self.rate_limiter` — for SYSTEM-initiated work the asker did not request
        (`mention._maybe_dm_fuller_answer`'s cheap retrieval-set comparison and the fuller DM `ask`
        it may trigger): that work must not draw on the asker's OWN budget, or an asker for whom
        content was withheld becomes measurably likelier to hit the public rate-limit message on
        their next real question — a difference in treatment they could observe. `identity=email`
        is unchanged either way, so audit attribution stays the same."""
        aud = set(audiences) if audiences is not None else None
        # `door`: this construction is the Slack transport's own — the one door whose
        # `source_client`/`source_permalink` hints are composed by server code from Slack's API
        # responses, so `_submit` accepts them here and refuses them from every client-facing
        # service (`capture.schema.reject_source_provenance_hints`; the constant crosses through
        # `server.service`'s re-export because of this package's pinned import list).
        return BrainService(self.settings.server, self.conn, self.embedder, aud, identity=email,
                            rate_limiter=self.rate_limiter if rate_limited else None,
                            audit=self.audit, evidence=self.evidence, door=SLACK_DOOR)

    async def decline(self, *, channel_id: str, slack_user_id: str, is_dm: bool, blocks: list,
                      text: str, thread_ts: str | None = None) -> None:
        """The ONE way this package tells someone a request was declined for an identity reason
        (`NoAccess`, `TransientFailure`) — ephemeral to the asker in a channel, because an identity
        failure must never be disclosed to the whole channel, and a real message when the surface
        itself IS a DM (Slack has no "ephemeral to yourself" there). Centralizing this is what
        stops each handler from inventing its own way to decline; without a shared seam this
        package drifts into three (silent, ephemeral, and — wrongly — public in a channel). Routed
        through `post_or_log`: a Slack outage while declining must degrade honestly, never raise
        out of the handler."""
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
        """The ONE seam every NON-CRITICAL Slack send in this package goes through: post-or-log,
        never raise. `coro` is an already-constructed coroutine (e.g.
        `self.gateway.chat_post_message(...)`) — a `SlackApiError` anywhere in it is logged and
        swallowed rather than propagated, matching the discipline `mention._edit_or_fallback`
        already applies to the placeholder-edit path (which keeps its own more specific
        retry/fallback policy and does not go through this seam). Returns the gateway's response on
        success, `None` on a caught failure — a caller that needs the response (the placeholder's
        own `ts`) must decide for itself what "failed to post at all" means, so it does not use
        this seam either."""
        try:
            return await coro
        except SlackApiError:
            log.error("slack: %s failed", what, exc_info=True)
            return None

    def mint_show_it_here_token(self, path: str, owner_slack_user_id: str) -> str:
        """An opaque per-answer token for the "Show it here" button's value — injected into
        `stigmergy.slack.render` as `mint_token`, matching how `link_resolver` is injected, so that
        module stays free of any notion of a token STORE. `(path, owner_slack_user_id)` lives ONLY
        here, server-side.

        Bounded at `self._show_it_here_max_tokens`, oldest-first eviction on insert — the same
        shape as `identity.UsersInfoCache`'s own bound, so there is one pattern for a
        process-lifetime store in this package, not two."""
        token = uuid.uuid4().hex
        if (token not in self._show_it_here_tokens
                and len(self._show_it_here_tokens) >= self._show_it_here_max_tokens):
            del self._show_it_here_tokens[next(iter(self._show_it_here_tokens))]   # oldest-first
        self._show_it_here_tokens[token] = (path, owner_slack_user_id,
                                            time.monotonic() + SHOW_IT_HERE_TOKEN_TTL_S)
        return token

    def consume_show_it_here_token(self, token: str) -> tuple[str, str] | None:
        """`(path, owner_slack_user_id)` for a live token, `None` for an unknown or expired one.
        Not single-use — the button may legitimately be clicked more than once — so a lookup only
        READS the entry; it is dropped by TTL expiry, never by having been read."""
        entry = self._show_it_here_tokens.get(token)
        if entry is None:
            return None
        path, owner_slack_user_id, expires_at = entry
        if time.monotonic() >= expires_at:
            del self._show_it_here_tokens[token]
            return None
        return path, owner_slack_user_id
