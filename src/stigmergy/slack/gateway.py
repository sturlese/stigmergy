"""`SlackGateway` — the one seam every Slack Web API call in this package crosses.

Same posture as `stigmergy.index.backends.fake_embedder` and `stigmergy.librarian.double`: a single
narrow interface, a real implementation that wraps the actual SDK client, and an offline double
(`FakeSlackGateway`) that records what would have happened instead of calling out to Slack. Every
handler in `stigmergy.slack` takes a gateway as a constructor argument — never imports `slack_sdk`
or `slack_bolt` directly — so the whole package runs and is tested with no network; only the
checks that need a live Slack workspace are exercised by hand.

`SlackApiError` is the ONE exception every method below raises on failure, deliberately collapsed
to one class the way `EvidenceError`/`IdentityError` are elsewhere in this codebase: the real
`slack_sdk.errors.SlackApiError` (and any transport failure underneath it — a timeout, a
connection reset) is caught at the real gateway's boundary and re-raised as this, so calling code
never has to know slack_sdk's own exception shape. That collapse is what makes the distinction one
layer up possible: a transient failure calling `users.info` must read as "the API had a problem",
never as "this person is unmapped" — the two are handled by completely different code in
`stigmergy.slack.identity`.
"""
from dataclasses import dataclass
from typing import Protocol


class SlackApiError(RuntimeError):
    """Any failure calling the real Slack Web API: a timeout, a 5xx, a rate limit, a malformed
    response. Never raised for an ordinary "no such thing" answer (an unmapped user's `users.info`
    call still SUCCEEDS and returns no email — see `identity.py`) — only for the API itself having
    failed to answer the question at all."""


class SlackGateway(Protocol):
    """The narrow surface this package needs. Every method is a coroutine — the transport is Bolt's
    async app in ONE process, and the socket loop must never block — so the real implementation
    wraps `slack_sdk`'s `AsyncWebClient` and no Slack Web API call ever blocks the event loop the
    socket-mode connection shares with every other event. `thread_ts=None` on a post means "not in
    a thread" — Slack's own convention (posting with no `thread_ts` starts a new top-level message;
    the channel's `ts` becomes the thread root the moment anything replies to it)."""

    async def users_info(self, user_id: str) -> dict:
        """The Slack user's profile — `users.info`, read for the profile email. Raises
        `SlackApiError` on any API failure — never on a real user with no email set, which is a
        successful call whose profile simply has none (`identity.py` treats that as unmapped, not
        as an error)."""
        ...

    async def conversations_info(self, channel_id: str) -> dict:
        """Channel metadata — `is_private`, `is_im`, `is_mpim`, `name`. Raises `SlackApiError` on
        any API failure."""
        ...

    async def conversations_replies(self, channel_id: str, thread_ts: str) -> list[dict]:
        """Every message in the thread rooted at `thread_ts`, oldest first, Slack's own verbatim
        shape (`user`, `ts`, `text`, ...). For a message that is not part of any thread, Slack's
        real API still returns exactly that one message — the 🧠 capture path relies on this to
        treat "a single message" and "a thread of one" identically."""
        ...

    async def get_permalink(self, channel_id: str, message_ts: str) -> str:
        """A permalink to one message — provenance for the capture's `hints`, never the archive
        itself: on Slack's free plan the link can outlive the message it points at."""
        ...

    async def chat_post_message(self, channel_id: str, *, text: str = "",
                               blocks: list | None = None, thread_ts: str | None = None) -> dict:
        """Post a new message. Returns Slack's own response shape (`ts` is the new message's own
        timestamp — its id)."""
        ...

    async def chat_update(self, channel_id: str, ts: str, *, text: str = "",
                         blocks: list | None = None) -> dict:
        """Edit an existing message in place: the placeholder is EDITED into the answer, never
        replaced with a second message — except on `mention._edit_or_fallback`'s
        retry-then-post-anyway path."""
        ...

    async def chat_post_ephemeral(self, channel_id: str, user_id: str, *, text: str = "",
                                 blocks: list | None = None, thread_ts: str | None = None) -> dict:
        """Post a message visible only to `user_id` — the no-access reply in a channel, the
        private-channel refusal, the capture-failed notice, the "already answered" reply and the
        "show it here" affordance all use this."""
        ...

    async def reactions_add(self, channel_id: str, message_ts: str, name: str) -> dict:
        """Add an emoji reaction to a message — the 🧠 gesture's instant progress marker,
        fired before any identity work. Raises `SlackApiError` on a real
        API failure (a missing `reactions:write` scope, a timeout, a rate limit); `already_reacted`
        — reachable whenever Slack redelivers the triggering event — is treated as success at the
        real gateway's boundary, never surfaced as a failure a caller has to special-case."""
        ...

    async def reactions_remove(self, channel_id: str, message_ts: str, name: str) -> dict:
        """Remove an emoji reaction — the progress marker's own cleanup, on every exit path.
        Raises `SlackApiError` on a real API failure; `no_reaction` — the reaction already gone,
        from a previous cleanup attempt or a redelivery — is treated as success the same way."""
        ...

    async def users_lookup_by_email(self, email: str) -> dict | None:
        """The steward doorbell's own reverse lookup — `ops/stewards.json` names stewards by EMAIL
        (same posture as `identities.json`), but a DM needs a Slack user id. Returns the same
        `{"user": {"id": ..., ...}}` shape `users_info` does, or `None` when no workspace member
        has that email: Slack's real `users.lookupByEmail` answers a `users_not_found` error for
        that case, which the real gateway collapses to `None` rather than `SlackApiError` — it is
        not an API failure, it is an honest "no such Slack identity", and the doorbell has to be
        able to record exactly that fact. Raises `SlackApiError` only for an actual API failure (a
        timeout, a rate limit, a 5xx) — never for the not-found case."""
        ...

    async def views_open(self, *, trigger_id: str, view: dict) -> dict:
        """Open a Block Kit modal — the note or reason a steward types costs them a composed
        sentence, not a click. Raises `SlackApiError` on any API failure; the caller's `trigger_id`
        is single-use and expires quickly, so a failed open cannot be retried with the same one."""
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
    """The first `block_id` that names more than one block in this payload, or `None`. Only blocks
    that SET the key explicitly are checked: Slack auto-assigns an internal id to a block that
    omits it, so two such blocks never collide from the caller's own perspective — exactly what
    every `_section`/`_context`/divider block `render.py` builds today (none of them ever sets
    `block_id`; only an `actions` block does)."""
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
    """Slack's REAL `chat.postMessage`/`chat.update` reject the WHOLE message — not just the
    offending block — for a `blocks` payload that names the same explicit `block_id` on two
    blocks: the real failure is `invalid_blocks`, with a message of the shape "block_id
    show_it_here:<path> already exists" (a recorded production failure, from two
    "Show it here" actions blocks built one-per-citation for two citations of the same page).
    Enforced UNCONDITIONALLY, on every call, because this mirrors real Slack API behaviour rather
    than scripting a failure a test opts into — a fake that stayed silent here would let a caller
    ship two `show_it_here` buttons for one page in a test and never learn Slack itself would have
    refused the whole message, which is exactly what made this bug invisible to the offline
    suite before this fake was extended.

    `fail_any_blocks` is the scripted, opt-in sibling, in the same countdown/set style as this
    class's other scripted failures: while True, ANY non-empty `blocks` payload is refused with
    `invalid_blocks`, unique ids or not. Slack's real `invalid_blocks` has causes besides a
    duplicate id (an unsupported block type, a nesting or length limit, ...) — a caller's degrade
    path must hold no matter WHICH cause tripped it, not only the one this fake can construct a
    concrete reproduction for."""
    if fail_any_blocks and blocks:
        raise SlackApiError("invalid_blocks")
    dup = _duplicate_block_id(blocks)
    if dup is not None:
        raise SlackApiError(f"invalid_blocks: block_id {dup} already exists")


class FakeSlackGateway:
    """The offline double every test in `tests/slack/` drives. Every call is RECORDED (so a test
    can assert exactly what would have been posted) and every failure mode is SCRIPTED explicitly
    (a set of user/channel ids that raise, or a countdown of failures before success) rather than
    inferred — a fake that silently "just works" would prove nothing about the retry/fallback
    logic it exists to exercise.

    One exception to "every failure is scripted": `chat_post_message`/`chat_update` enforce
    Slack's real block_id-uniqueness rule on every call, unconditionally — see
    `_raise_if_invalid_blocks`. This is not a script a test arms; it is the same real-API
    constraint production code is subject to, reproduced here so a payload that would fail against
    the real Slack Web API fails against this double too, rather than only against a live
    workspace nobody runs in CI.
    """

    def __init__(self) -> None:
        self.users: dict[str, str | None] = {}          # user_id -> email (None = no email set)
        self.display_names: dict[str, str] = {}          # user_id -> display name (capture hints)
        self.channels: dict[str, dict] = {}              # channel_id -> {"is_private","is_im","is_mpim","name"}
        self.threads: dict[tuple[str, str], list[dict]] = {}   # (channel_id, thread_ts) -> messages
        self.permalinks: dict[tuple[str, str], str] = {}

        # Scripted failures. `fail_users_info`/`fail_conversations_info` are sets of ids that ALWAYS
        # raise SlackApiError (a persistent API outage for that lookup). `fail_post_count` /
        # `fail_update_count` are countdowns: that many calls raise, then the next one succeeds —
        # what a bounded-retry-with-backoff test drives. `fail_any_blocks` is a blanket switch (see
        # `_raise_if_invalid_blocks`): while True, `chat_post_message`/`chat_update` refuse ANY
        # non-empty `blocks` payload with `invalid_blocks`, standing in for any of Slack's real
        # causes for that failure, not only the duplicate-block_id one this fake also checks for
        # UNCONDITIONALLY (never behind a flag — see `_duplicate_block_id`), on every call.
        self.fail_users_info: set[str] = set()
        self.fail_conversations_info: set[str] = set()
        self.fail_post_count = 0
        self.fail_update_count = 0
        self.fail_ephemeral_count = 0
        self.fail_any_blocks = False
        # The progress-reaction lifecycle's own scripted failures — countdowns, same shape as
        # `fail_post_count`/`fail_update_count`, so "the reactions API is down, the capture still
        # works" is testable without touching any other call's script.
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

    def seed_email(self, email: str, user_id: str) -> None:
        """The reverse of `seed_user`: a workspace member findable by email, the way the steward
        lookup needs. Also seeds `users[user_id]` so a subsequent `users_info` on the same id
        agrees."""
        self.emails[email] = user_id
        self.users.setdefault(user_id, email)

    # ── seeding helpers (tests set these up, then drive a handler) ───────────
    def seed_user(self, user_id: str, email: str | None, *, display_name: str = "") -> None:
        self.users[user_id] = email
        if display_name:
            self.display_names[user_id] = display_name

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
            raise SlackApiError("chat.update failed")
        _raise_if_invalid_blocks(blocks, fail_any_blocks=self.fail_any_blocks)
        self.updated.append(_Updated(channel_id, ts, text, blocks))
        return {"ok": True, "channel": channel_id, "ts": ts}

    async def chat_post_ephemeral(self, channel_id: str, user_id: str, *, text: str = "",
                                 blocks: list | None = None, thread_ts: str | None = None) -> dict:
        if self.fail_ephemeral_count > 0:
            self.fail_ephemeral_count -= 1
            raise SlackApiError("chat.postEphemeral failed")
        # `chat.postEphemeral` is subject to the SAME Block Kit rules as `chat.postMessage`, and
        # `_raise_if_invalid_blocks` says it is "enforced UNCONDITIONALLY, on every call" — this
        # method was the one that did not. A double that lets through a payload real Slack rejects
        # is the failure this class's own docstring exists to prevent, and it matters here more
        # than elsewhere: the ephemeral leg carries the refusals and the "Show it here" excerpt,
        # and `post_or_log`/`decline` swallow the resulting error, so the live symptom is silence.
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
