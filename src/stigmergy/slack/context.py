"""`SlackContext` — shared Slack process resources built once at startup.

`conn` remains open only for the Socket Mode singleton advisory-lock lifetime. Handlers build
their short-lived database units through `connection_factory`.
"""
import logging
import time
import uuid
from dataclasses import dataclass, field

from stigmergy.kernel.blocking import run_blocking
from stigmergy.server import ops_files
from stigmergy.server.audit import AuditWriter
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
    connection_factory: object = None
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
        async def resolve_audiences(email: str):
            return await run_blocking(
                self.with_connection,
                lambda conn: ops_files.resolve_identity_audiences(
                    conn, self.settings.server.identities_path, email
                ),
            )

        return await resolve_slack_identity(
            self.gateway, self.cache, identities_path=self.settings.server.identities_path,
            configured_team_id=self.settings.team_id, event_team_id=event_team_id,
            slack_user_id=slack_user_id, resolve_audiences=resolve_audiences)

    def with_connection(self, operation):
        """Run one short database unit, keeping the singleton-lock connection untouched."""
        if self.connection_factory is None:
            return operation(self.conn)
        conn = self.connection_factory()
        try:
            return operation(conn)
        finally:
            conn.close()

    def build_service(self, email: str, audiences, *, rate_limited: bool = True,
                      conn=None) -> BrainService:
        """A per-identity `BrainService` sharing every process-wide resource. `audiences` accepts
        `None` (unrestricted) or any iterable of labels.

        `rate_limited=False` is for SYSTEM-initiated work the asker did not request
        (`mention._maybe_dm_fuller_answer`): spending their own budget on it would make an asker
        for whom content was withheld observably likelier to hit the rate-limit message on their
        next real question. `identity=email` is unchanged either way, so audit attribution stays
        the same."""
        conn = self.conn if conn is None else conn
        principal = ops_files.resolve_identity_principal(
            conn,
            self.settings.server.identities_path,
            email,
        )
        aud = None if audiences is None else set(audiences)
        # `door`: the Slack transport is the one door whose `source_*` hints are composed by
        # server code from Slack's API responses — `_submit` accepts them here and refuses them
        # from every client-facing service (`capture.schema.reject_source_provenance_hints`).
        audit = self.audit if conn is self.conn else AuditWriter(conn)
        return BrainService(self.settings.server, conn, self.embedder, aud, identity=email,
                            rate_limiter=self.rate_limiter if rate_limited else None,
                            audit=audit, evidence=self.evidence, door=SLACK_DOOR,
                            principal=principal)

    def run_service(self, email: str, audiences, operation, *, rate_limited: bool = True):
        """Build and use one per-listener service on the connection it owns."""
        return self.with_connection(
            lambda conn: operation(
                self.build_service(email, audiences, rate_limited=rate_limited, conn=conn)
            )
        )

    async def decline(self, *, channel_id: str, slack_user_id: str, is_dm: bool, blocks: list,
                      text: str, thread_ts: str | None = None) -> None:
        """Send an identity refusal without disclosing it to a channel."""
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
        """Post a non-critical Slack response, logging and swallowing Slack failures."""
        try:
            return await coro
        except SlackApiError as error:
            log.error("slack: %s failed (%s)", what, error.__class__.__name__)
            return None

    def mint_show_it_here_token(self, path: str, owner_slack_user_id: str) -> str:
        """Mint one bounded opaque token for the human-facing page excerpt action."""
        token = uuid.uuid4().hex
        if (token not in self._show_it_here_tokens
                and len(self._show_it_here_tokens) >= self._show_it_here_max_tokens):
            del self._show_it_here_tokens[next(iter(self._show_it_here_tokens))]
        self._show_it_here_tokens[token] = (path, owner_slack_user_id,
                                            time.monotonic() + SHOW_IT_HERE_TOKEN_TTL_S)
        return token

    def consume_show_it_here_token(self, token: str) -> tuple[str, str] | None:
        """Return a live opaque token's scoped path and owner, if any."""
        entry = self._show_it_here_tokens.get(token)
        if entry is None:
            return None
        path, owner_slack_user_id, expires_at = entry
        if time.monotonic() >= expires_at:
            del self._show_it_here_tokens[token]
            return None
        return path, owner_slack_user_id


def run_with_connection(ctx, operation):
    """Use the context connection seam, including lightweight listener test doubles."""
    with_connection = getattr(ctx, "with_connection", None)
    return with_connection(operation) if with_connection is not None else operation(ctx.conn)


def run_with_service(ctx, email: str, audiences, operation, *, rate_limited: bool = True):
    """Use the context service seam, including lightweight listener test doubles."""
    run_service = getattr(ctx, "run_service", None)
    if run_service is not None:
        return run_service(email, audiences, operation, rate_limited=rate_limited)
    if rate_limited:
        return operation(ctx.build_service(email, audiences))
    return operation(ctx.build_service(email, audiences, rate_limited=False))
