"""`ops/slack-channels.json` — the channel's audience scope.

A second, smaller version of the shape `stigmergy.server.identity` establishes for
`ops/identities.json`: a versioned JSON file in the SAME knowledge-repo checkout
(`channel_id -> [labels]`), read fresh on every lookup (no caching — this file changes rarely and
a stale read of an ACCESS-SCOPING file is the wrong kind of cheap), fail-closed on a malformed
file, and defaulting to the EMPTY set for any channel not listed.

**The empty-set default is the load-bearing property, not an edge case.** Per `acl.visible()`'s
truth table, an empty audience set sees pages carrying no `acl` label and nothing else — the
fail-closed default that requires a deliberate edit (adding the channel to this file) to widen,
and no edit at all to be safe. A channel's scope is therefore ALWAYS a `set[str]`, never `None`:
even the unrestricted asker in a channel not listed here gets the empty set, not "everything", and
this function's return type makes that structurally true (`set[str]`, never `set[str] | None`).

**What that default resolves to depends entirely on whether any page carries a label.** Where
`ops/slack-channels.json` has not been created at all, every channel resolves to the empty
audience set — and per the truth table above, the empty set sees every page carrying no `acl`
label. If no rule in `ops/acl.json` labels anything either, the fail-closed default resolves to
the whole corpus. Nothing here is wrong — the default is correct, and it becomes genuinely
restrictive the moment the first labelled page exists — but it is indistinguishable from no
scoping at all until then, so a green two-identity test in a channel is not on its own evidence
that channel scoping restricts anything.

This module never calls `acl.visible()` and never decides whether a page is visible — it only
resolves ONE fact (a channel's labels), the same way `identity.resolve_audiences` resolves one
fact about a person. `BrainService` (via `stigmergy.server`) is what actually enforces the scope,
exactly as `stigmergy.slack`'s package docstring requires.
"""
import json
import os

from stigmergy.server.errors import IdentityError

DEFAULT_RELATIVE = os.path.join("ops", "slack-channels.json")


def default_path(repo_dir: str | None) -> str:
    """The conventional channels file inside a knowledge-repo checkout, mirroring
    `identity.default_path` — '' when no repo is given, so the resolver fails closed with an
    actionable message rather than silently reading nothing."""
    return os.path.join(repo_dir, DEFAULT_RELATIVE) if repo_dir else ""


def channel_audiences(channels_path: str, channel_id: str) -> set[str]:
    """The effective audience scope for a public-channel answer: the labels `channel_id` is
    mapped to, or the EMPTY set for a channel not listed (or when `channels_path` itself is not
    configured — a Slack deployment that never created this file gets the safe default on every
    channel, not an error, because "no file yet" and "no scope for THIS channel" are the same
    fact from a channel's point of view).

    Malformed content (the file does not parse, is not a JSON object, or a channel's value is not
    a list of strings) raises `IdentityError` — read once, fail loudly, the same posture
    `identity.resolve_audiences` takes for its own file: a scoping file the server cannot make
    sense of must never be treated as "no restrictions apply".
    """
    if not channels_path or not os.path.exists(channels_path):
        return set()
    try:
        with open(channels_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as ex:
        raise IdentityError(
            f"slack channels file unreadable or malformed: {channels_path}: {ex}") from ex
    if not isinstance(data, dict):
        raise IdentityError(
            f"slack channels file malformed: {channels_path} "
            '(expected an object mapping channel_id -> [audience labels])')
    if channel_id not in data:
        return set()
    value = data[channel_id]
    if not isinstance(value, list) or not all(isinstance(a, str) for a in value):
        raise IdentityError(
            f"channel {channel_id!r} has a malformed audience value in {channels_path} "
            f"(expected a list of audience labels, got {type(value).__name__})")
    return {s.strip() for s in value if s.strip()}
