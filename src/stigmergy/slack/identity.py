"""Slack profile email -> `ops/identities.json`, fail closed — this package's security surface.

`resolve_slack_identity` is the ONE function every Slack event handler calls before constructing a
`BrainService`. It never reads `ops/identities.json` itself: the audience resolution is
`stigmergy.server.identity.resolve_audiences`, the SAME function and the SAME file every other
transport uses — there is no second identity registry. What this module adds, on top of that
shared seam, is entirely about resolving a SLACK IDENTITY to the email that function takes:

    Slack user id -> `users.info` -> profile email -> resolve_audiences(email)

**Five outcomes, deliberately five different types, so a caller cannot collapse two of them by
accident** (a `match`/`isinstance` on `IdentityResult` is exhaustive-checkable, unlike a shared
sentinel string would be):

- `Ignored`   — a bot/app/workflow event, or the bot's own message. ZERO Slack traffic; identity
               resolution is not even attempted.
- `ForeignTeam` — the event's `team_id` does not match the configured workspace. Also silent: a
               shared channel from another company gets nothing, not the no-access reply, which
               would presume a relationship a stranger from an unrelated workspace does not have.
- `TransientFailure` — `users.info` itself failed (a timeout, a 5xx, a rate limit): NOT an
               unmapped user. The caller renders the server-error copy, never the "ask to be
               added" copy — and, like every other branch here, no `BrainService` is constructed.
- `NoAccess`  — the email is absent, empty, or `resolve_audiences` raised `IdentityError` (an
               email genuinely not in the file, or a malformed file). One outcome for all three
               reasons, on purpose — this is not a degraded read tier, no `BrainService` is
               constructed at all, and a caller must not distinguish "no email" from "email not
               registered" in what it tells the user, or a determined prober learns the identity
               file's shape by comparing responses (the same discipline `read_page`'s "unknown
               page" shape already applies).
- `Resolved`  — an email and its audience scope (`None` = unrestricted), ready to build a
               `BrainService` from, exactly the way `transport_http._BearerAuthMiddleware` builds
               one per bearer token.

Bot users, app users, workflow users and the bot's OWN messages must be excluded BEFORE any of the
above runs — `is_ignorable_event` is the one place that check lives, so an `app_mention` fired by
the bot's own post can never loop.
"""
import time
from dataclasses import dataclass

from stigmergy.server.errors import IdentityError
from stigmergy.server.identity import resolve_audiences
from stigmergy.slack.gateway import SlackApiError

# Slack marks an automated message this way; a genuine human event never carries it.
BOT_MESSAGE_SUBTYPE = "bot_message"

# How long a positive users.info -> email lookup is trusted before it is refreshed. Small relative
# to how rarely a person's Slack profile email changes, generous enough that a busy thread does not
# re-hit the rate-limited API on every message in it.
DEFAULT_TTL_SECONDS = 300


@dataclass(frozen=True)
class Ignored:
    """Zero Slack traffic — a bot/app/workflow event, or the bot's own message."""
    reason: str


@dataclass(frozen=True)
class ForeignTeam:
    """The event's `team_id` does not match the configured workspace. Silent."""
    team_id: str


@dataclass(frozen=True)
class TransientFailure:
    """`users.info` itself failed. NOT an unmapped user — the caller renders the server-error copy,
    never the no-access one."""
    detail: str


@dataclass(frozen=True)
class NoAccess:
    """No email, an empty email, or an email `resolve_audiences` does not recognize. One outcome
    for all three — the caller's copy must not distinguish them."""


@dataclass(frozen=True)
class Resolved:
    """A resolved Slack identity: the email `BrainService.identity` is built from, and its
    audience scope (`None` = unrestricted, exactly `resolve_audiences`'s own convention)."""
    email: str
    audiences: frozenset[str] | None


IdentityResult = Ignored | ForeignTeam | TransientFailure | NoAccess | Resolved


# A bound, not a tune: no real workspace has anywhere near this many distinct Slack users, so this
# never fires in practice — it exists so the cache cannot grow without bound for the life of the
# process (a misbehaving workspace, or an Enterprise Grid org fanning out many distinct team_ids).
# Eviction is oldest-first, on insert, which is exactly enough to bound memory; it is not an LRU
# and does not need to be — a re-evicted, still-active user simply pays one extra `users.info`
# call on their next question, the same cost as a cold cache entry.
DEFAULT_MAX_ENTRIES = 10_000


class UsersInfoCache:
    """THREE separate TTL maps, each keyed on a workspace-scoped pair — never on the Slack user id
    or the email alone, so a person can never be resolved under the wrong workspace's cached value
    (defense in depth: this bot serves one configured workspace, but the key is the fact that
    identifies a person, not an assumption about how many workspaces ever call this):

    - `_entries` — `(team_id, slack_user_id)` -> EMAIL, the identity lookup the ACL depends on;
    - `_by_email` — `(team_id, email)` -> slack_user_id, the doorbell's reverse direction;
    - `_display_names` — `(team_id, slack_user_id)` -> display name, decorative copy only.

    They are separate dicts with separate accessors on purpose, and the separation is the security
    property: nothing writes a display name through the email accessors, so a user-settable display
    name can never reach an identity — and therefore an ACL — decision. Each map carries its own
    `max_entries` bound, so the process ceiling is three bounds, not one.

    All three hold ONLY positive results: the cache is never consulted for a negative one, so an
    unmapped user who gets mapped works on their next question. `clock` is injectable so a test
    drives expiry without a real sleep."""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS, clock=time.monotonic,
                max_entries: int = DEFAULT_MAX_ENTRIES):
        self._ttl = ttl_seconds
        self._clock = clock
        self._max_entries = max_entries
        self._entries: dict[tuple[str, str], tuple[float, str]] = {}
        # The steward doorbell's REVERSE lookup (email -> Slack user id, `users.lookupByEmail`)
        # shares this SAME cache object (`ctx.cache`, already threaded through every handler)
        # rather than a second store. Without a cache, `doorbell._resolve_slack_user_id` calls
        # `users.lookupByEmail` once per (item, steward) on EVERY poll pass, and that is a Tier-3
        # endpoint (~50/min): twenty open items on a 10-second loop is 120 calls/min, sustained
        # 429s, and — because a 429 collapses to the SAME `None` an honest "no such person" does —
        # every steward reads as having no Slack identity until the rate limit clears, which it
        # never does under that load. Same TTL/eviction shape as the forward direction, positive
        # results only.
        self._by_email: dict[tuple[str, str], tuple[float, str]] = {}
        # The 🧠 gesture's own need (`capture.py`): a display name, not an email — decorative
        # copy for the ack and the `source_participants` hint, never load-bearing. A THIRD map,
        # not a bigger cached VALUE on `_entries` (a `(email, display_name)` tuple, or the raw
        # profile dict): the email lookup and this cache's eviction bound stay untouched by a
        # feature that has nothing to do with identity, and a caller that wants only the email
        # stays uncoupled from whether a display name was ever fetched for the same user.
        # Populated two ways: as a side effect of `resolve_slack_identity`'s OWN `users.info`
        # call, at zero extra network cost, for whichever user that call already resolved (almost
        # always the reactor); lazily by `capture._display_name` on its own cache miss for every
        # OTHER thread participant, who `resolve_slack_identity` never touches at all. Same
        # TTL/eviction shape as `_entries`, positive results only.
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
        """The reverse direction (`users.lookupByEmail`'s own question) — `None` on a miss OR an
        expired entry, exactly like `get` above; a miss here means "ask the API", never "this
        person has no Slack identity" (that fact is not this cache's to assert)."""
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
        """The display-name sibling of `get` — same key shape, same TTL, same positive-only rule.
        `None` on a miss OR an expired entry, exactly like `get` — a miss means "ask `users.info`",
        never "this person has no display name"."""
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
    """The cheap, synchronous half of `resolve_slack_identity`'s fail-closed workspace check — no
    `await`, no cache, no API call. Extracted so a caller that needs to know "is this our
    workspace" before doing ANY identity work — the 🧠 gesture's instant progress reaction
    (`app.on_reaction_added`), fired before `resolve_slack_identity` is even called — asks the
    SAME question the fail-closed guard below asks, rather than a second comparison that could
    drift from it. Fails CLOSED exactly like the guard it is extracted from: an absent
    `event_team_id` is never "the configured workspace"."""
    return bool(event_team_id) and event_team_id == configured_team_id


async def resolve_slack_identity(gateway, cache: UsersInfoCache, *, identities_path: str,
                                 configured_team_id: str, event_team_id: str,
                                 slack_user_id: str) -> IdentityResult:
    """The one function every handler calls before constructing a `BrainService`. Callers run
    `is_ignorable_event` FIRST and pass only events that survive it — this function assumes a real
    human Slack user id, and its job starts at the workspace check.

    **Fails CLOSED on an absent event team, not open**: a missing or empty `event_team_id` — an
    Enterprise Grid org-wide install, or a caller that failed to source one — is untrusted, never
    treated as "the configured workspace". `configured_team_id` is already `_require_env`-backed
    (`SlackSettings.from_args`), so there is no legitimate reason for the comparison to be
    skipped; a second way to disable this check would be dead weight."""
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
        # A side effect of the SAME `users.info` response, at zero extra network cost: whoever
        # this call just resolved (almost always the 🧠 gesture's reactor) has their display name
        # cached too, so `capture._display_name`'s later lookup for this identical
        # (team_id, slack_user_id) is a hit rather than a second `users.info` round-trip.
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
