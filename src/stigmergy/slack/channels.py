"""`ops/slack-channels.json` — the channel's groups: `channel_id -> [group, …]`, read fresh on
every lookup (a stale read of an access-scoping file is the wrong kind of cheap), fail-closed on a
malformed file.

**The empty-set default is the load-bearing property, and it now says the same thing on both
sides.** A channel not listed is a PUBLIC channel: per `acl.visible()`'s truth table its empty
group set reads only pages carrying no label, and per ADR 045 D2 a capture taken there is filed
OPEN (the door stores `NULL`, never `{}` — no groups is a fact about the channel, not the `acl:
[]` of a page, which means nobody). Widening either side takes a deliberate edit to this file;
being safe takes none. A channel's scope is ALWAYS a `set[str]`, never `None`: Slack capture and
Slack answering are public-channel only, so no channel is ever unrestricted.

The grammar is `server.identity`'s, parsed there (ADR 045 D7) so the roster and this map cannot
come to disagree about what a group may be called — including the reserved name `all`. The WHOLE
file is validated on every lookup, not only the entry wanted: a malformed neighbour in an
access-scoping file is a file the server cannot make sense of.

This module never calls `acl.visible()`: it resolves ONE fact about a channel, and `BrainService`
enforces the scope.
"""
import os

from stigmergy.server import identity as identity_module
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
    snapshot and a `--channels` file cannot mean different things. An EMPTY text is malformed JSON
    and RAISES: on this road "no scoping declared" is spelled `{}`, a committed statement, never
    bytes that failed to arrive."""
    groups = identity_module.group_map_from_text(text, origin=origin, subject="channel")
    return set(groups.get(channel_id, ()))


def channel_groups_for_capture(conn, channels_path: str, channel_id: str) -> set[str]:
    """The WRITE side's resolution, and it differs from the read side's in exactly one way: the
    map must EXIST.

    Reading, an absent map is fail-closed — every channel gets the empty set and sees only open
    pages. Writing, the same empty set files every capture from every scoped channel OPEN, which
    is the same silence pointing the other way. So a deployment that has not configured the map
    at all is refused here rather than quietly publishing: `ops/identities.json` already takes
    that posture, and this file decides the same class of fact.

    A channel that is genuinely public is spelled by its ABSENCE FROM a map that exists, or by
    `{}` — a committed statement — never by no file at all.
    """
    if ops_files.text_or_none(conn, ops_files.SLACK_CHANNELS_RELPATH) is None and (
            not channels_path or not os.path.exists(channels_path)):
        raise IdentityError(
            "no slack channels map is configured, so the audience a capture from this channel "
            f"would be filed at cannot be established (expected {DEFAULT_RELATIVE} in the "
            "knowledge repo, or --channels). A brain with no scoped channels declares that with "
            "an empty object, which is a committed statement rather than a missing file")
    return channel_audiences_live(conn, channels_path, channel_id)


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
