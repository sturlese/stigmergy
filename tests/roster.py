"""Test support for the Postgres roster: reset it and seed principals the way fixtures describe them."""
import secrets
import uuid

from stigmergy.server import roster
from stigmergy.server.identity import UNRESTRICTED_GROUP, hash_token


def reset_roster(conn) -> None:
    roster.ensure_roster_schema(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM roster_sessions")
        cur.execute("DELETE FROM roster_tokens")
        cur.execute("DELETE FROM roster_slack_channels")
        cur.execute("DELETE FROM roster_principals")
        cur.execute("DELETE FROM roster_groups WHERE name <> %s", (UNRESTRICTED_GROUP,))


def seed_roster(conn, identities: dict, *, admins=(), channels=None) -> dict[str, str]:
    """Replace the roster with `identities` ({subject: {display_name, groups, default_audience,
    kind?}}) and return one live token per subject."""
    reset_roster(conn)
    groups = {g for value in identities.values()
              for g in (*value.get("groups", ()), *(value.get("default_audience") or ()))}
    groups |= {g for audience in (channels or {}).values() if audience for g in audience}
    tokens = {}
    with conn.cursor() as cur:
        for name in sorted(groups - {UNRESTRICTED_GROUP, "public"}):
            cur.execute("INSERT INTO roster_groups (name) VALUES (%s)", (name,))
        for subject, value in identities.items():
            if str(subject).startswith("_"):
                continue
            key = subject.lower()
            default = value.get("default_audience")
            cur.execute(
                "INSERT INTO roster_principals (subject, display_name, kind, is_admin, groups, "
                "default_audience) VALUES (%s, %s, %s, %s, %s, %s)",
                (key, value.get("display_name") or subject, value.get("kind", "person"),
                 subject in admins, list(value.get("groups", ())),
                 list(default) if default is not None else None),
            )
            token = roster.TOKEN_PREFIX + secrets.token_urlsafe(roster.TOKEN_BYTES)
            cur.execute(
                "INSERT INTO roster_tokens (id, subject, label, prefix, sha256) "
                "VALUES (%s, %s, 'test', %s, %s)",
                (str(uuid.uuid4()), key, token[:10], hash_token(token)),
            )
            tokens[subject] = token
        for channel_id, audience in (channels or {}).items():
            cur.execute("INSERT INTO roster_slack_channels (channel_id, groups) VALUES (%s, %s)",
                        (channel_id, list(audience) if audience is not None else None))
    return tokens
