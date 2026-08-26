"""Slack profile email -> `ops/identities.json`, fail closed — this package's security surface.

`resolve_slack_identity` is the ONE function every handler calls before constructing a
`BrainService`:

    Slack user id -> `users.info` -> profile email -> resolve_audiences(email)

— the SAME `stigmergy.server.identity.resolve_audiences` and the SAME file every transport uses;
there is no second identity registry. The five outcomes are five separate types, so a
`match`/`isinstance` is exhaustive-checkable and a caller cannot collapse two by accident; each
type's docstring says what it means, and no `BrainService` is constructed on any non-`Resolved`
path. Bot/app/workflow events and the bot's OWN messages are excluded FIRST
(`is_ignorable_event`), so an `app_mention` fired by the bot's own post can never loop.
"""
import inspect
import time
from dataclasses import dataclass

from stigmergy.server import ops_files
from stigmergy.server.errors import IdentityError
from stigmergy.slack.gateway import SlackApiError

# Slack marks an automated message this way; a genuine human event never carries it.
BOT_MESSAGE_SUBTYPE = "bot_message"

# How long a positive users.info -> email lookup is trusted: small relative to how rarely a
# profile email changes, generous enough that a busy thread does not re-hit the rate-limited API.
DEFAULT_TTL_SECONDS = 300


@dataclass(frozen=True)
class Ignored:
    """Zero Slack traffic — a bot/app/workflow event, or the bot's own message."""
    reason: str


@dataclass(frozen=True)
class ForeignTeam:
    """The event's `team_id` does not match the configured workspace. Silent — a stranger from an
    unrelated workspace gets nothing, not a reply that presumes a relationship."""
    team_id: str


@dataclass(frozen=True)
class TransientFailure:
    """`users.info` itself failed. NOT an unmapped user — the caller renders the server-error copy,
    never the no-access one."""
    detail: str


@dataclass(frozen=True)
class NoAccess:
    """No email, an empty email, or an email `resolve_audiences` does not recognize. One outcome
    for all three — a caller's copy that distinguished them would let a prober learn the identity
    file's shape by comparing responses."""


@dataclass(frozen=True)
class Resolved:
    """A resolved Slack identity: the email `BrainService.identity` is built from, and its
    audience scope (`None` = unrestricted, exactly `resolve_audiences`'s own convention)."""
    email: str
    audiences: frozenset[str] | None


IdentityResult = Ignored | ForeignTeam | TransientFailure | NoAccess | Resolved


# A bound, not a tune — it exists so the cache cannot grow for the life of the process. Eviction
# is oldest-first on insert, deliberately not an LRU: a re-evicted, still-active user pays one
# extra `users.info` call.
DEFAULT_MAX_ENTRIES = 10_000


class _TtlMap:
    """One workspace-scoped TTL store: `(team_id, <something>) -> string`, bounded, positive
    results only. `UsersInfoCache` holds THREE of these and never merges them — see its
    docstring for why that separation is the security property."""

    def __init__(self, ttl_seconds: int, clock, max_entries: int):
        self._ttl = ttl_seconds
        self._clock = clock
        self._max_entries = max_entries
        self._values: dict[tuple[str, str], tuple[float, str]] = {}

    def __len__(self) -> int:
        return len(self._values)

    def get(self, key: tuple[str, str]) -> str | None:
        entry = self._values.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            del self._values[key]
            return None
        return value

    def put(self, key: tuple[str, str], value: str) -> None:
        if not value:   # positive results only — see `UsersInfoCache`'s docstring
            return
        if key not in self._values and len(self._values) >= self._max_entries:
            del self._values[next(iter(self._values))]   # oldest-first — see module comment
        self._values[key] = (self._clock() + self._ttl, value)


class UsersInfoCache:
    """TWO separate TTL maps, each keyed on a workspace-scoped pair — never the user id or the
    email alone, so a person can never resolve under the wrong workspace's cached value:

    - `_entries` — `(team_id, slack_user_id)` -> EMAIL, the lookup the ACL depends on;
    - `_display_names` — `(team_id, slack_user_id)` -> display name, decorative copy only.

    The separation IS the security property: nothing writes a display name through the email
    accessors, so a user-settable display name can never reach an identity — and therefore an
    ACL — decision. Both hold ONLY positive results, so an unmapped user who gets mapped
    works on their next question."""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS, clock=time.monotonic,
                max_entries: int = DEFAULT_MAX_ENTRIES):
        self._entries = _TtlMap(ttl_seconds, clock, max_entries)
        # The 🧠 gesture's display names — a SECOND map, not a bigger value on `_entries`, so the
        # email lookup stays uncoupled from a decorative feature. Populated free of charge from
        # `resolve_slack_identity`'s own `users.info` response, and lazily by
        # `capture._display_name` for the other thread participants.
        self._display_names = _TtlMap(ttl_seconds, clock, max_entries)

    def get(self, team_id: str, slack_user_id: str) -> str | None:
        return self._entries.get((team_id, slack_user_id))

    def put(self, team_id: str, slack_user_id: str, email: str) -> None:
        self._entries.put((team_id, slack_user_id), email)

    def get_display_name(self, team_id: str, slack_user_id: str) -> str | None:
        """The display-name sibling of `get` — same key shape, same TTL, same positive-only
        rule."""
        return self._display_names.get((team_id, slack_user_id))

    def put_display_name(self, team_id: str, slack_user_id: str, display_name: str) -> None:
        self._display_names.put((team_id, slack_user_id), display_name)


def is_ignorable_event(event: dict, *, bot_user_id: str | None) -> bool:
    """True when identity resolution must not even be attempted: a bot/app/workflow message, or an
    event carrying the bot's own user id (an `app_mention` on the bot's own post must not loop).
    Checked BEFORE anything else in this module runs."""
    if event.get("bot_id"):
        return True
    if event.get("app_id"):
        return True
    if event.get("subtype") == BOT_MESSAGE_SUBTYPE:
        return True
    user = event.get("user")
    if not user:
        return True   # a workflow step or similar carries no human user at all
    return bool(bot_user_id) and user == bot_user_id


def is_configured_workspace(event_team_id: str, configured_team_id: str) -> bool:
    """Return whether the event workspace matches the configured workspace; absence fails closed."""
    return bool(event_team_id) and event_team_id == configured_team_id


async def resolve_slack_identity(gateway, cache: UsersInfoCache, *, identities_path: str,
                                 configured_team_id: str, event_team_id: str,
                                 slack_user_id: str, conn=None,
                                 resolve_audiences=None) -> IdentityResult:
    """The one function every handler calls before constructing a `BrainService`. Callers run
    `is_ignorable_event` FIRST; this function assumes a real human Slack user id and starts at
    the workspace check. **Fails CLOSED on an absent event team**: a missing `event_team_id` (an
    Enterprise Grid org-wide install, or a caller that failed to source one) is untrusted, never
    treated as the configured workspace."""
    if not is_configured_workspace(event_team_id, configured_team_id):
        return ForeignTeam(event_team_id or "<absent>")

    email = cache.get(event_team_id, slack_user_id)
    if email is None:
        try:
            profile = await gateway.users_info(slack_user_id)
        except SlackApiError as ex:
            return TransientFailure(str(ex))
        prof = (profile.get("user") or {}).get("profile") or {}
        email = prof.get("email") or ""
        cache.put(event_team_id, slack_user_id, email)
        # A free side effect of the SAME `users.info` response: the display name is cached too,
        # so `capture._display_name`'s later lookup is a hit rather than a second round trip.
        cache.put_display_name(event_team_id, slack_user_id,
                               prof.get("display_name") or prof.get("real_name") or "")

    if not email:
        return NoAccess()

    try:
        # Snapshot-first through the server's one chooser (`server.ops_files`): the deployed
        # slack group holds no checkout, and an identity edit must not wait for a deploy.
        resolver = resolve_audiences or (
            lambda resolved_email: ops_files.resolve_identity_audiences(
                conn, identities_path, resolved_email
            )
        )
        audiences_tuple = resolver(email)
        if inspect.isawaitable(audiences_tuple):
            audiences_tuple = await audiences_tuple
    except IdentityError:
        return NoAccess()
    audiences = frozenset(audiences_tuple) if audiences_tuple is not None else None
    return Resolved(email=email, audiences=audiences)
