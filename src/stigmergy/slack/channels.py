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

from stigmergy.server import ops_files
from stigmergy.server.errors import IdentityError

DEFAULT_RELATIVE = os.path.join("ops", "slack-channels.json")


def default_path(repo_dir: str | None) -> str:
    """The conventional channels file inside a knowledge-repo checkout — `''` when no repo is
    given, so the resolver fails closed rather than silently reading nothing."""
    return os.path.join(repo_dir, DEFAULT_RELATIVE) if repo_dir else ""


def channel_audiences(channels_path: str, channel_id: str) -> set[str]:
    """The labels `channel_id` maps to through the channels FILE, or the EMPTY set for a channel
    not listed or a `channels_path` never configured — "no file yet" and "no scope for THIS
    channel" are the same fact from a channel's point of view. Malformed content raises
    `IdentityError`: a scoping file the server cannot make sense of must never be treated as "no
    restrictions apply".
    """
    if not channels_path or not os.path.exists(channels_path):
        return set()
    try:
        with open(channels_path, encoding="utf-8") as f:
            text = f.read()
    except OSError as ex:
        raise IdentityError(f"slack channels file unreadable: {channels_path}: {ex}") from ex
    return channel_audiences_from_text(text, channel_id, origin=channels_path)


def channel_audiences_from_text(text: str, channel_id: str, *, origin: str) -> set[str]:
    """`channel_audiences` over the file's TEXT — the one parse under both roads, so the index's
    snapshot and a `--channels` file cannot mean different things. An EMPTY text is malformed
    JSON and RAISES: on this road "no scoping declared" is spelled `{}`, a committed statement,
    never bytes that failed to arrive."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as ex:
        raise IdentityError(f"slack channels malformed: {origin}: {ex}") from ex
    if not isinstance(data, dict):
        raise IdentityError(
            f"slack channels malformed: {origin} "
            '(expected an object mapping channel_id -> [audience labels])')
    if channel_id not in data:
        return set()
    value = data[channel_id]
    if not isinstance(value, list) or not all(isinstance(a, str) for a in value):
        raise IdentityError(
            f"channel {channel_id!r} has a malformed audience value in {origin} "
            f"(expected a list of audience labels, got {type(value).__name__})")
    return {s.strip() for s in value if s.strip()}


def channel_audiences_live(conn, channels_path: str, channel_id: str) -> set[str]:
    """The deployed resolution: the index's snapshot wherever the database carries one, this
    process's own file where it does not (`server.ops_files` states the order once). The DEPLOYED
    slack group holds no checkout, so its file is the copy baked at deploy time — a channel scoped
    after the rollout stayed effectively unscoped until the next deploy (issue #79). The
    fresh-read-per-lookup rule is unchanged on both roads: a stale read of an access-scoping fact
    is the wrong kind of cheap."""
    text = ops_files.text_or_none(conn, ops_files.SLACK_CHANNELS_RELPATH)
    if text is not None:
        return channel_audiences_from_text(text, channel_id,
                                           origin=ops_files.CHANNELS_SNAPSHOT_ORIGIN)
    return channel_audiences(channels_path, channel_id)
