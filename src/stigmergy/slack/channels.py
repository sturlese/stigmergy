"""Slack channel visibility resolved from the versioned channel map."""

import os

from stigmergy.server import identity as identity_module
from stigmergy.server import ops_files
from stigmergy.server.errors import IdentityError

DEFAULT_RELATIVE = os.path.join("ops", "slack-channels.json")


def default_path(repo_dir: str | None) -> str:
    return os.path.join(repo_dir, DEFAULT_RELATIVE) if repo_dir else ""


def channel_map_from_text(
    text: str, *, origin: str
) -> dict[str, tuple[str, ...] | None]:
    return identity_module.channel_map_from_text(text, origin=origin)


def _live_map(conn, channels_path: str) -> dict[str, tuple[str, ...] | None] | None:
    text = ops_files.text_or_none(conn, ops_files.SLACK_CHANNELS_RELPATH)
    if text is not None:
        return channel_map_from_text(text, origin=ops_files.CHANNELS_SNAPSHOT_ORIGIN)
    if not channels_path or not os.path.exists(channels_path):
        return None
    try:
        with open(channels_path, encoding="utf-8") as file:
            return channel_map_from_text(file.read(), origin=channels_path)
    except OSError as error:
        raise IdentityError("Slack channel map is unavailable") from error


def channel_scope_for_capture(
    conn, channels_path: str, channel_id: str
) -> tuple[str, ...] | None:
    mapping = _live_map(conn, channels_path)
    if mapping is None or channel_id not in mapping:
        raise IdentityError("Slack channel is not mapped for capture")
    return mapping[channel_id]


def channel_audiences_live(conn, channels_path: str, channel_id: str) -> set[str]:
    mapping = _live_map(conn, channels_path)
    if mapping is None:
        return set()
    return set(mapping.get(channel_id) or ())


def channel_audiences(channels_path: str, channel_id: str) -> set[str]:
    mapping = _live_map(None, channels_path)
    if mapping is None:
        return set()
    return set(mapping.get(channel_id) or ())


def channel_audiences_from_text(text: str, channel_id: str, *, origin: str) -> set[str]:
    return set(channel_map_from_text(text, origin=origin).get(channel_id) or ())


def channel_groups_for_capture(conn, channels_path: str, channel_id: str) -> set[str]:
    return set(channel_scope_for_capture(conn, channels_path, channel_id) or ())
