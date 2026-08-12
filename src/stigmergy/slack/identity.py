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
import time
from dataclasses import dataclass

from stigmergy.server.errors import IdentityError
from stigmergy.server.identity import resolve_audiences
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


class UsersInfoCache:
    """THREE separate TTL maps, each keyed on a workspace-scoped pair — never the user id or the
    email alone, so a person can never resolve under the wrong workspace's cached value:

    - `_entries` — `(team_id, slack_user_id)` -> EMAIL, the lookup the ACL depends on;
    - `_by_email` — `(team_id, email)` -> slack_user_id, the doorbell's reverse direction;
    - `_display_names` — `(team_id, slack_user_id)` -> display name, decorative copy only.

    The separation IS the security property: nothing writes a display name through the email
    accessors, so a user-settable display name can never reach an identity — and therefore an
    ACL — decision. All three hold ONLY positive results, so an unmapped user who gets mapped
    works on their next question. `clock` is injectable so a test drives expiry without a real
    sleep."""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS, clock=time.monotonic,
                max_entries: int = DEFAULT_MAX_ENTRIES):
        self._ttl = ttl_seconds
        self._clock = clock
        self._max_entries = max_entries
        self._entries: dict[tuple[str, str], tuple[float, str]] = {}
        # The doorbell's REVERSE lookup shares this same object rather than a second store —
        # `users.lookupByEmail` is Tier-3 (~50/min), and uncached per-(item, steward)-per-pass
        # calls turn a transient 429 into a sustained one that reads every steward as having no
        # Slack identity. Same TTL/eviction shape, positive results only.
        self._by_email: dict[tuple[str, str], tuple[float, str]] = {}
        # The 🧠 gesture's display names — a THIRD map, not a bigger value on `_entries`, so the
        # email lookup stays uncoupled from a decorative feature. Populated free of charge from
        # `resolve_slack_identity`'s own `users.info` response, and lazily by
        # `capture._display_name` for the other thread participants.
        self._display_names: dict[tuple[str, str], tuple[float, str]] = {}

    def get(self, team_id: str, slack_user_id: str) -> str | None:
        key = (team_id, slack_user_id)
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, email = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            return None
        return email

    def put(self, team_id: str, slack_user_id: str, email: str) -> None:
        if not email:   # positive results only — see class docstring
            return
        key = (team_id, slack_user_id)
        if key not in self._entries and len(self._entries) >= self._max_entries:
            del self._entries[next(iter(self._entries))]   # oldest-first — see module comment
        self._entries[key] = (self._clock() + self._ttl, email)

    def get_id_by_email(self, team_id: str, email: str) -> str | None:
        """`None` on a miss OR an expired entry, like `get` — a miss means "ask the API", never
        "this person has no Slack identity" (not this cache's fact to assert)."""
        key = (team_id, email)
        entry = self._by_email.get(key)
        if entry is None:
            return None
        expires_at, slack_user_id = entry
        if self._clock() >= expires_at:
            del self._by_email[key]
            return None
        return slack_user_id

    def put_id_by_email(self, team_id: str, email: str, slack_user_id: str) -> None:
        if not slack_user_id:   # positive results only — see class docstring
            return
        key = (team_id, email)
        if key not in self._by_email and len(self._by_email) >= self._max_entries:
            del self._by_email[next(iter(self._by_email))]   # oldest-first
        self._by_email[key] = (self._clock() + self._ttl, slack_user_id)

    def get_display_name(self, team_id: str, slack_user_id: str) -> str | None:
        """The display-name sibling of `get` — same key shape, same TTL, same positive-only
        rule."""
        key = (team_id, slack_user_id)
        entry = self._display_names.get(key)
        if entry is None:
            return None
        expires_at, name = entry
        if self._clock() >= expires_at:
            del self._display_names[key]
            return None
        return name

    def put_display_name(self, team_id: str, slack_user_id: str, display_name: str) -> None:
        if not display_name:   # positive results only — see class docstring
            return
        key = (team_id, slack_user_id)
        if key not in self._display_names and len(self._display_names) >= self._max_entries:
            del self._display_names[next(iter(self._display_names))]   # oldest-first
        self._display_names[key] = (self._clock() + self._ttl, display_name)


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
    """The cheap, synchronous half of `resolve_slack_identity`'s fail-closed workspace check —
    extracted so the 🧠 progress reaction can ask the SAME question before any identity work,
    rather than a second comparison that could drift. Fails CLOSED: an absent `event_team_id` is
    never "the configured workspace"."""
    return bool(event_team_id) and event_team_id == configured_team_id


async def resolve_slack_identity(gateway, cache: UsersInfoCache, *, identities_path: str,
                                 configured_team_id: str, event_team_id: str,
                                 slack_user_id: str) -> IdentityResult:
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
        audiences_tuple = resolve_audiences(identities_path, email)
    except IdentityError:
        return NoAccess()
    audiences = frozenset(audiences_tuple) if audiences_tuple is not None else None
    return Resolved(email=email, audiences=audiences)
