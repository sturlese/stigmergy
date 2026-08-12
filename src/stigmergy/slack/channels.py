"""`ops/slack-channels.json` — the channel's audience scope: `channel_id -> [labels]`, read fresh
on every lookup (a stale read of an access-scoping file is the wrong kind of cheap), fail-closed
on a malformed file.

**The empty-set default is the load-bearing property.** Per `acl.visible()`'s truth table, an
empty audience set sees only pages carrying no `acl` label — widening takes a deliberate edit to
this file, being safe takes none. A channel's scope is ALWAYS a `set[str]`, never `None`: even an
unrestricted asker gets the empty set in an unlisted channel. Until the first labelled page
exists, that default is indistinguishable from no scoping at all — a green two-identity channel
test is not, on its own, evidence that scoping restricts anything.

This module never calls `acl.visible()`: it resolves ONE fact about a channel, and `BrainService`
enforces the scope.
"""
import json
import os

from stigmergy.server.errors import IdentityError

DEFAULT_RELATIVE = os.path.join("ops", "slack-channels.json")


def default_path(repo_dir: str | None) -> str:
    """The conventional channels file inside a knowledge-repo checkout — `''` when no repo is
    given, so the resolver fails closed rather than silently reading nothing."""
    return os.path.join(repo_dir, DEFAULT_RELATIVE) if repo_dir else ""


def channel_audiences(channels_path: str, channel_id: str) -> set[str]:
    """The labels `channel_id` maps to, or the EMPTY set for a channel not listed or a
    `channels_path` never configured — "no file yet" and "no scope for THIS channel" are the same
    fact from a channel's point of view. Malformed content raises `IdentityError`: a scoping file
    the server cannot make sense of must never be treated as "no restrictions apply".
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
