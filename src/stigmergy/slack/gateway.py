"""`SlackGateway` — the one seam every Slack Web API call in this package crosses: a narrow
interface, the real `bolt_gateway` implementation, and `FakeSlackGateway`, the offline double
`tests/slack/` drives. Every handler takes a gateway as an argument and never imports `slack_sdk`.

`SlackApiError` is the ONE exception every method raises on failure, collapsed at the real
gateway's boundary. That collapse is what lets `identity.py` distinguish "the API had a problem"
from "this person is unmapped" one layer up.
"""
from dataclasses import dataclass
from typing import Protocol


class SlackApiError(RuntimeError):
    """Any failure calling the real Slack Web API. Never raised for an honest "no such thing"
    answer (an unmapped user's `users.info` still SUCCEEDS, with no email) — only for the API
    failing to answer at all.

    `code` is Slack's own error code when the failure carried one (`message_not_found`,
    `channel_not_found`, …) and `""` when it did not — a timeout or a connection reset has none.
    It is here so a caller can tell a refusal that can NEVER succeed from one worth retrying:
    `str(ex)` is prose the SDK assembles, and nothing should be pattern-matching that.
    """

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class SlackGateway(Protocol):
    """The narrow surface this package needs. Every method is a coroutine — the socket loop must
    never block. `thread_ts=None` on a post means "not in a thread", Slack's own convention."""

    async def users_info(self, user_id: str) -> dict:
        """`users.info`, read for the profile email. Raises `SlackApiError` on any API failure —
        never for a real user with no email set (a successful call `identity.py` treats as
        unmapped)."""
        ...

    async def conversations_info(self, channel_id: str) -> dict:
        """Channel metadata — `is_private`, `is_im`, `is_mpim`, `name`."""
        ...

    async def conversations_replies(self, channel_id: str, thread_ts: str) -> list[dict]:
        """Every message in the thread rooted at `thread_ts`, oldest first, Slack's own verbatim
        shape. For a message in no thread Slack still returns exactly that one message — the 🧠
        path relies on this to treat "a single message" and "a thread of one" identically."""
        ...

    async def get_permalink(self, channel_id: str, message_ts: str) -> str:
        """A permalink to one message — provenance for the capture's `hints`, never the archive:
        on Slack's free plan the link can outlive the message it points at."""
        ...

    async def chat_post_message(self, channel_id: str, *, text: str = "",
                               blocks: list | None = None, thread_ts: str | None = None) -> dict:
        """Post a new message. Returns Slack's own response shape (`ts` is the message's id)."""
        ...

    async def chat_update(self, channel_id: str, ts: str, *, text: str = "",
                         blocks: list | None = None) -> dict:
        """Edit a message in place — the placeholder is EDITED into the answer, never replaced,
        except on `mention._edit_or_fallback`'s retry-then-post-anyway path."""
        ...

    async def chat_post_ephemeral(self, channel_id: str, user_id: str, *, text: str = "",
                                 blocks: list | None = None, thread_ts: str | None = None) -> dict:
        """Post a message visible only to `user_id` — every refusal and the "show it here"
        affordance use this."""
        ...

    async def reactions_add(self, channel_id: str, message_ts: str, name: str) -> dict:
        """Add an emoji reaction. `already_reacted` (reachable on any event redelivery) is
        treated as success at the real gateway's boundary; a real API failure raises
        `SlackApiError`."""
        ...

    async def reactions_remove(self, channel_id: str, message_ts: str, name: str) -> dict:
        """Remove an emoji reaction. `no_reaction` (already gone — a previous cleanup, or a
        redelivery) is treated as success the same way; a real API failure raises
        `SlackApiError`."""
        ...

    async def users_lookup_by_email(self, email: str) -> dict | None:
        """The doorbell's reverse lookup (`ops/stewards.json` names stewards by EMAIL; a DM needs
        a user id). Returns `users_info`'s shape, or `None` when no workspace member has that
        email — Slack's `users_not_found` is an honest negative the doorbell must be able to
        record, so only an actual API failure raises `SlackApiError`."""
        ...

    async def views_open(self, *, trigger_id: str, view: dict) -> dict:
        """Open a Block Kit modal. `trigger_id` is single-use and expires quickly, so a failed
        open cannot be retried with the same one."""
        ...


@dataclass
class _Posted:
    channel_id: str
    text: str
    blocks: list | None
    thread_ts: str | None
    ts: str


@dataclass
class _Ephemeral:
    channel_id: str
    user_id: str
    text: str
    blocks: list | None
    thread_ts: str | None


@dataclass
class _Updated:
    channel_id: str
    ts: str
    text: str
    blocks: list | None


@dataclass
class _Reaction:
    channel_id: str
    ts: str
    name: str


def _duplicate_block_id(blocks: list | None) -> str | None:
    """The first `block_id` naming more than one block in this payload, or `None`. Only blocks
    that SET the key explicitly are checked: Slack auto-assigns ids to blocks that omit it, and
    those never collide."""
    seen: set[str] = set()
    for block in blocks or []:
        block_id = block.get("block_id")
        if block_id is None:
            continue
        if block_id in seen:
            return block_id
        seen.add(block_id)
    return None


def _raise_if_invalid_blocks(blocks: list | None, *, fail_any_blocks: bool) -> None:
    """Slack's REAL `chat.postMessage`/`chat.update` reject the WHOLE message for a `blocks`
    payload naming the same explicit `block_id` on two blocks. Enforced UNCONDITIONALLY, on every
    call — a fake that stayed silent here would let a payload real Slack refuses sail through the
    offline suite. `fail_any_blocks` is the scripted, opt-in sibling: while True, ANY non-empty
    `blocks` payload is refused — Slack's real `invalid_blocks` has causes beyond a duplicate id,
    and a caller's degrade path must hold whichever cause tripped it."""
    if fail_any_blocks and blocks:
        raise SlackApiError("invalid_blocks")
    dup = _duplicate_block_id(blocks)
    if dup is not None:
        raise SlackApiError(f"invalid_blocks: block_id {dup} already exists")


class FakeSlackGateway:
    """The offline double every test in `tests/slack/` drives: every call RECORDED, every failure
    SCRIPTED (sets of ids that always raise, or countdowns of failures before success) — a fake
    that silently "just works" would prove nothing about the retry/fallback logic it exists to
    exercise. The one exception: the block_id-uniqueness rule is enforced unconditionally, like
    the real API (`_raise_if_invalid_blocks`).
    """

    def __init__(self) -> None:
        self.users: dict[str, str | None] = {}          # user_id -> email (None = no email set)
        self.display_names: dict[str, str] = {}          # user_id -> display name (capture hints)
        self.channels: dict[str, dict] = {}              # channel_id -> {"is_private","is_im","is_mpim","name"}
        self.threads: dict[tuple[str, str], list[dict]] = {}   # (channel_id, thread_ts) -> messages
        self.permalinks: dict[tuple[str, str], str] = {}

        # Scripted failures: `fail_users_info`/`fail_conversations_info` are ids that ALWAYS
        # raise; `fail_*_count` are countdowns (that many calls raise, then success);
        # `fail_any_blocks` refuses any non-empty `blocks` payload while True (see
        # `_raise_if_invalid_blocks`).
        self.fail_users_info: set[str] = set()
        self.fail_conversations_info: set[str] = set()
        self.fail_post_count = 0
        self.fail_update_count = 0
        # The Slack error CODE a scripted `chat.update` failure carries. `""` is the coded-less
        # shape (a timeout, a reset), so the SAME countdown drives a caller that classifies
        # terminal refusals — `message_not_found` and a timeout must not degrade the same way.
        self.fail_update_code = ""
        self.fail_ephemeral_count = 0
        self.fail_any_blocks = False
        # The progress-reaction lifecycle's own countdowns, so "the reactions API is down, the
        # capture still works" is testable in isolation.
        self.fail_reactions_add_count = 0
        self.fail_reactions_remove_count = 0

        self.posted: list[_Posted] = []
        self.updated: list[_Updated] = []
        self.ephemeral: list[_Ephemeral] = []
        self.reactions_added: list[_Reaction] = []
        self.reactions_removed: list[_Reaction] = []
        self._next_ts = 1000

        # The doorbell's reverse email lookup, and the review surface's modals.
        self.emails: dict[str, str] = {}                  # email -> user_id (reverse of `users`)
        self.fail_lookup_by_email: set[str] = set()        # emails whose lookup ALWAYS raises
        self.opened_views: list[dict] = []
        self.fail_views_open_count = 0

    # ── seeding helpers (tests set these up, then drive a handler) ───────────
    def seed_user(self, user_id: str, email: str | None, *, display_name: str = "") -> None:
        self.users[user_id] = email
        if display_name:
            self.display_names[user_id] = display_name

    def seed_email(self, email: str, user_id: str) -> None:
        """The reverse of `seed_user`: a member findable by email; also seeds `users[user_id]` so
        a subsequent `users_info` on the same id agrees."""
        self.emails[email] = user_id
        self.users.setdefault(user_id, email)

    def seed_channel(self, channel_id: str, *, is_private: bool = False, is_im: bool = False,
                     is_mpim: bool = False, name: str = "") -> None:
        self.channels[channel_id] = {"is_private": is_private, "is_im": is_im,
                                     "is_mpim": is_mpim, "name": name}

    def seed_thread(self, channel_id: str, thread_ts: str, messages: list[dict]) -> None:
        self.threads[(channel_id, thread_ts)] = messages

    def _new_ts(self) -> str:
        self._next_ts += 1
        return f"{self._next_ts}.000001"

    # ── SlackGateway ──────────────────────────────────────────────────────
    async def users_info(self, user_id: str) -> dict:
        if user_id in self.fail_users_info:
            raise SlackApiError(f"users.info failed for {user_id}")
        email = self.users.get(user_id)
        display_name = self.display_names.get(user_id, "")
        profile = {}
        if email:
            profile["email"] = email
        if display_name:
            profile["display_name"] = display_name
        return {"user": {"id": user_id, "profile": profile}}

    async def conversations_info(self, channel_id: str) -> dict:
        if channel_id in self.fail_conversations_info:
            raise SlackApiError(f"conversations.info failed for {channel_id}")
        meta = self.channels.get(channel_id, {})
        return {"channel": {"id": channel_id, **meta}}

    async def conversations_replies(self, channel_id: str, thread_ts: str) -> list[dict]:
        return list(self.threads.get((channel_id, thread_ts), []))

    async def get_permalink(self, channel_id: str, message_ts: str) -> str:
        return self.permalinks.get((channel_id, message_ts),
                                   f"https://example.slack.com/archives/{channel_id}/p{message_ts}")

    async def chat_post_message(self, channel_id: str, *, text: str = "",
                               blocks: list | None = None, thread_ts: str | None = None) -> dict:
        if self.fail_post_count > 0:
            self.fail_post_count -= 1
            raise SlackApiError("chat.postMessage failed")
        _raise_if_invalid_blocks(blocks, fail_any_blocks=self.fail_any_blocks)
        ts = self._new_ts()
        self.posted.append(_Posted(channel_id, text, blocks, thread_ts, ts))
        return {"ok": True, "channel": channel_id, "ts": ts}

    async def chat_update(self, channel_id: str, ts: str, *, text: str = "",
                         blocks: list | None = None) -> dict:
        if self.fail_update_count > 0:
            self.fail_update_count -= 1
            raise SlackApiError("chat.update failed", code=self.fail_update_code)
        _raise_if_invalid_blocks(blocks, fail_any_blocks=self.fail_any_blocks)
        self.updated.append(_Updated(channel_id, ts, text, blocks))
        return {"ok": True, "channel": channel_id, "ts": ts}

    async def chat_post_ephemeral(self, channel_id: str, user_id: str, *, text: str = "",
                                 blocks: list | None = None, thread_ts: str | None = None) -> dict:
        if self.fail_ephemeral_count > 0:
            self.fail_ephemeral_count -= 1
            raise SlackApiError("chat.postEphemeral failed")
        # Subject to the SAME Block Kit rules as `chat.postMessage`. It matters most here: the
        # ephemeral leg carries the refusals and the "Show it here" excerpt, and
        # `post_or_log`/`decline` swallow the error, so the live symptom is silence.
        _raise_if_invalid_blocks(blocks, fail_any_blocks=self.fail_any_blocks)
        self.ephemeral.append(_Ephemeral(channel_id, user_id, text, blocks, thread_ts))
        return {"ok": True}

    async def reactions_add(self, channel_id: str, message_ts: str, name: str) -> dict:
        if self.fail_reactions_add_count > 0:
            self.fail_reactions_add_count -= 1
            raise SlackApiError("reactions.add failed")
        self.reactions_added.append(_Reaction(channel_id, message_ts, name))
        return {"ok": True}

    async def reactions_remove(self, channel_id: str, message_ts: str, name: str) -> dict:
        if self.fail_reactions_remove_count > 0:
            self.fail_reactions_remove_count -= 1
            raise SlackApiError("reactions.remove failed")
        self.reactions_removed.append(_Reaction(channel_id, message_ts, name))
        return {"ok": True}

    async def users_lookup_by_email(self, email: str) -> dict | None:
        if email in self.fail_lookup_by_email:
            raise SlackApiError(f"users.lookupByEmail failed for {email}")
        user_id = self.emails.get(email)
        if user_id is None:
            return None
        return {"user": {"id": user_id, "profile": {"email": email}}}

    async def views_open(self, *, trigger_id: str, view: dict) -> dict:
        if self.fail_views_open_count > 0:
            self.fail_views_open_count -= 1
            raise SlackApiError("views.open failed")
        self.opened_views.append({"trigger_id": trigger_id, "view": view})
        return {"ok": True, "view": {**view, "id": f"V{len(self.opened_views):04d}"}}
