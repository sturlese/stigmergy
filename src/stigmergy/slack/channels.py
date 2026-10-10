"""Slack channel audiences, read from the roster on every event."""

from stigmergy.server import roster
from stigmergy.server.errors import IdentityError


def channel_scope_for_capture(conn, channel_id: str) -> tuple[str, ...] | None:
    """The audience a capture from `channel_id` files under; an unmapped channel captures nothing."""
    mapping = roster.channel_map(conn)
    if channel_id not in mapping:
        raise IdentityError("Slack channel is not mapped for capture")
    return mapping[channel_id]


def channel_audiences_live(conn, channel_id: str) -> set[str]:
    """The groups a channel answer is scoped to; an unmapped or open channel has none."""
    return set(roster.channel_map(conn).get(channel_id) or ())
