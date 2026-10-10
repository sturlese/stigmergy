"""People, groups, bearer tokens, backoffice sessions, and Slack channel audiences in Postgres.

Every identity decision reads these tables through this module: MCP bearer tokens, Slack emails,
the writer's actor scope, capture audiences, and the backoffice. A lookup runs per request, so a
revoked token, a disabled person, or a removed group stops working on the next request.
Credentials are stored only as SHA-256 digests of 256-bit random values; a plaintext token or
session value exists only in the response that creates it.
"""
from __future__ import annotations

import datetime as dt
import re
import secrets
import uuid
from dataclasses import dataclass

from stigmergy.capture.schema import startup_ddl_lock
from stigmergy.kernel import layers, tenancy
from stigmergy.server.errors import IdentityError
from stigmergy.server.identity import (
    UNRESTRICTED_GROUP,
    Principal,
    check_group_names,
    hash_token,
)

TOKEN_PREFIX = "stg_"
TOKEN_BYTES = 32
SESSION_BYTES = 32
SESSION_IDLE = dt.timedelta(hours=8)
SESSION_ABSOLUTE = dt.timedelta(days=7)
MAX_LABEL_CHARS = 80
MAX_DISPLAY_NAME_CHARS = 120
MAX_DESCRIPTION_CHARS = 1000
KINDS = ("person", "agent")
# Serializes every roster write that checks another row first (the last administrator, a group in
# use), so two concurrent edits cannot both pass a check the other invalidates.
_ROSTER_LOCK = "stigmergy.roster.write"
UNKNOWN = "credential is not recognized"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CHANNEL_RE = re.compile(r"^[A-Z0-9]{6,20}$")

_DDL = (
    f"""
    CREATE TABLE IF NOT EXISTS roster_groups (
        {tenancy.TENANT_COLUMN},
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_by TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (tenant_id, name)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS roster_principals (
        {tenancy.TENANT_COLUMN},
        subject TEXT NOT NULL CHECK (subject = lower(subject)),
        display_name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('person', 'agent')),
        is_admin BOOLEAN NOT NULL DEFAULT FALSE,
        groups TEXT[] NOT NULL DEFAULT '{{}}',
        default_audience TEXT[],
        disabled_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_by TEXT NOT NULL DEFAULT '',
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_by TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (tenant_id, subject),
        CHECK (NOT is_admin OR (kind = 'person' AND 'brain-admins' = ANY(groups)))
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS roster_tokens (
        id UUID PRIMARY KEY,
        {tenancy.TENANT_COLUMN},
        subject TEXT NOT NULL,
        label TEXT NOT NULL,
        prefix TEXT NOT NULL,
        sha256 TEXT NOT NULL UNIQUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_by TEXT NOT NULL DEFAULT '',
        last_used_at TIMESTAMPTZ,
        expires_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ,
        FOREIGN KEY (tenant_id, subject) REFERENCES roster_principals (tenant_id, subject)
            ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS roster_tokens_subject_idx ON roster_tokens (tenant_id, subject)",
    f"""
    CREATE TABLE IF NOT EXISTS roster_sessions (
        sha256 TEXT PRIMARY KEY,
        {tenancy.TENANT_COLUMN},
        subject TEXT NOT NULL,
        token_id UUID NOT NULL REFERENCES roster_tokens(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ NOT NULL,
        revoked_at TIMESTAMPTZ,
        FOREIGN KEY (tenant_id, subject) REFERENCES roster_principals (tenant_id, subject)
            ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS roster_sessions_subject_idx ON roster_sessions (tenant_id, subject)",
    "CREATE INDEX IF NOT EXISTS roster_sessions_token_idx ON roster_sessions (token_id)",
    f"""
    CREATE TABLE IF NOT EXISTS roster_slack_channels (
        {tenancy.TENANT_COLUMN},
        channel_id TEXT NOT NULL,
        groups TEXT[],
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_by TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (tenant_id, channel_id)
    )
    """,
)

TABLES = ("roster_groups", "roster_principals", "roster_tokens", "roster_sessions",
          "roster_slack_channels")

_PRINCIPAL_COLUMNS = ("subject", "display_name", "kind", "is_admin", "groups", "default_audience",
                      "disabled_at", "created_at", "updated_at")


def ensure_roster_schema(conn) -> None:
    with startup_ddl_lock(conn) as cur:
        for statement in _DDL:
            cur.execute(statement)
        for table in TABLES:
            tenancy.protect(cur, table)
    if tenancy.current_tenant(conn) is not None:
        seed_tenant(conn)


def seed_tenant(conn) -> None:
    """The rows every tenant's roster starts with: the unrestricted role."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO roster_groups (name, description, created_by) VALUES (%s, %s, 'system') "
            "ON CONFLICT (tenant_id, name) DO NOTHING",
            (UNRESTRICTED_GROUP, "Reads every page in the brain."),
        )


class RosterRefused(ValueError):
    """An administrative change that would break a roster rule. The message is safe to show."""


@dataclass(frozen=True)
class TokenGrant:
    token: str
    id: str
    subject: str
    label: str
    prefix: str
    expires_at: str | None


@dataclass(frozen=True)
class Session:
    principal: Principal
    token_id: str
    expires_at: dt.datetime


# ── reading identities ────────────────────────────────────────────────────────────────────────
def _principal(row, tenant) -> Principal:
    values = dict(zip(_PRINCIPAL_COLUMNS, row, strict=True))
    return Principal(
        subject=values["subject"],
        display_name=values["display_name"],
        groups=tuple(values["groups"] or ()),
        default_audience=(tuple(values["default_audience"])
                          if values["default_audience"] is not None else None),
        kind=values["kind"],
        is_admin=bool(values["is_admin"]),
        tenant=str(tenant),
    )


# A tenant that is not suspended: a provisioning tenant's administrator may already sign in.
_LIVE_TENANT = "JOIN tenants n ON n.id = p.tenant_id AND n.status <> 'suspended' "


def principal(conn, subject: str | None) -> Principal:
    """The enabled principal named `subject` in a workspace that is not suspended; unknown,
    disabled, and suspended read alike."""
    if not subject:
        raise IdentityError("no identity was provided")
    # A subject may exist in several tenants; platform scope would see them all.
    tenant = tenancy.require_tenant(conn)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(f'p.{column}' for column in _PRINCIPAL_COLUMNS)}, p.tenant_id "
            f"FROM roster_principals p {_LIVE_TENANT}"
            "WHERE p.tenant_id = %s AND p.subject = %s AND p.disabled_at IS NULL",
            (tenant, subject.strip().lower()),
        )
        row = cur.fetchone()
    if row is None:
        raise IdentityError("identity is not configured")
    return _principal(row[:-1], row[-1])


def principal_for_token(conn, token: str | None) -> tuple[Principal, str]:
    """The enabled principal holding a live token, and that token's id. The token is looked up
    before any tenant is known; the principal carries the tenant its row belongs to."""
    if not token:
        raise IdentityError("no bearer token was presented")
    columns = ", ".join(f"p.{column}" for column in _PRINCIPAL_COLUMNS)
    with tenancy.platform_scope(conn), conn.cursor() as cur:
        cur.execute(
            f"SELECT {columns}, p.tenant_id, t.id FROM roster_tokens t "
            "JOIN roster_principals p ON p.tenant_id = t.tenant_id AND p.subject = t.subject "
            f"{_LIVE_TENANT}"
            "WHERE t.sha256 = %s AND t.revoked_at IS NULL "
            "AND (t.expires_at IS NULL OR t.expires_at > now()) AND p.disabled_at IS NULL",
            (hash_token(token),),
        )
        row = cur.fetchone()
        if row is None:
            raise IdentityError(UNKNOWN)
        cur.execute(
            "UPDATE roster_tokens SET last_used_at = now() WHERE id = %s "
            "AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')",
            (row[-1],),
        )
    return _principal(row[:-2], row[-2]), str(row[-1])


def layer_descriptions(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT name, description FROM roster_groups")
        return {row[0]: row[1] for row in cur.fetchall()}


def known_groups(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM roster_groups")
        return {row[0] for row in cur.fetchall()}


def channel_map(conn) -> dict[str, tuple[str, ...] | None]:
    with conn.cursor() as cur:
        cur.execute("SELECT channel_id, groups FROM roster_slack_channels")
        return {row[0]: (tuple(row[1]) if row[1] is not None else None) for row in cur.fetchall()}


# ── administering people ──────────────────────────────────────────────────────────────────────
def _subject(value) -> str:
    subject = str(value or "").strip().lower()
    if not _EMAIL_RE.fullmatch(subject) or len(subject) > 254:
        raise RosterRefused("the identity must be an email address")
    return subject


def _groups(conn, value, *, subject: str) -> tuple[str, ...]:
    try:
        groups = check_group_names(list(value or []), origin="the roster", subject=subject)
    except IdentityError as error:
        raise RosterRefused(str(error)) from error
    unknown = sorted(set(groups) - known_groups(conn))
    if unknown:
        raise RosterRefused(f"{subject} names a group that does not exist: {', '.join(unknown)}")
    if any(group != UNRESTRICTED_GROUP and not layers.is_layer_name(group) for group in groups):
        raise RosterRefused(f"{subject} names a group that is not a layer")
    return tuple(sorted(groups))


def _validated(conn, data: dict, *, subject: str) -> dict:
    """The complete, checked field set for one principal; `data` must name every field."""
    display_name = " ".join(str(data.get("display_name") or "").split())
    if not display_name or len(display_name) > MAX_DISPLAY_NAME_CHARS:
        raise RosterRefused("a display name of at most 120 characters is required")
    kind = data.get("kind") or "person"
    if kind not in KINDS:
        raise RosterRefused("kind must be person or agent")
    is_admin = bool(data.get("is_admin"))
    if is_admin and kind != "person":
        raise RosterRefused("only a person can administer the backoffice")
    groups = _groups(conn, data.get("groups"), subject="groups")
    # The operational sections show every page, so administering requires unrestricted reading.
    if is_admin and UNRESTRICTED_GROUP not in groups:
        raise RosterRefused(f"an administrator must belong to {UNRESTRICTED_GROUP}")
    default = data.get("default_audience")
    if not isinstance(default, (list, tuple)) or len(default) != 1 or not isinstance(default[0], str):
        raise RosterRefused("a default layer is required: `public` or one of the identity's layers")
    default_layer = default[0].strip()
    if default_layer != layers.PUBLIC and (default_layer == UNRESTRICTED_GROUP or default_layer not in groups):
        raise RosterRefused("the default layer must be `public` or one of the identity's layers")
    return {"subject": subject, "display_name": display_name, "kind": kind, "is_admin": is_admin,
            "groups": list(groups), "default_audience": [default_layer]}


def _lock_roster(cur) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_ROSTER_LOCK,))


def _admins_left(cur, *, excluding: str) -> int:
    cur.execute(
        "SELECT count(*) FROM roster_principals WHERE is_admin AND disabled_at IS NULL "
        "AND subject <> %s",
        (excluding,),
    )
    return cur.fetchone()[0]


def create_principal(conn, data: dict, *, actor: str) -> Principal:
    with conn.transaction(), conn.cursor() as cur:
        _lock_roster(cur)
        values = _validated(conn, data, subject=_subject(data.get("subject")))
        cur.execute(
            "INSERT INTO roster_principals (subject, display_name, kind, is_admin, groups, "
            "default_audience, created_by, updated_by) VALUES (%(subject)s, %(display_name)s, "
            "%(kind)s, %(is_admin)s, %(groups)s, %(default_audience)s, %(actor)s, %(actor)s) "
            "ON CONFLICT (tenant_id, subject) DO NOTHING",
            {**values, "actor": actor},
        )
        if cur.rowcount != 1:
            raise RosterRefused("that identity already exists")
    return principal(conn, values["subject"])


def update_principal(conn, subject: str, data: dict, *, actor: str) -> Principal:
    """Change the fields `data` names; a field it omits keeps its stored value."""
    subject = _subject(subject)
    with conn.transaction(), conn.cursor() as cur:
        _lock_roster(cur)
        cur.execute("SELECT display_name, kind, is_admin, groups, default_audience "
                    "FROM roster_principals WHERE subject = %s FOR UPDATE", (subject,))
        row = cur.fetchone()
        if row is None:
            raise RosterRefused("that identity does not exist")
        stored = dict(zip(("display_name", "kind", "is_admin", "groups", "default_audience"), row,
                          strict=True))
        values = _validated(conn, {**stored, **data}, subject=subject)
        if row[2] and not values["is_admin"] and _admins_left(cur, excluding=subject) == 0:
            raise RosterRefused("the last administrator cannot lose the role")
        cur.execute(
            "UPDATE roster_principals SET display_name = %(display_name)s, kind = %(kind)s, "
            "is_admin = %(is_admin)s, groups = %(groups)s, default_audience = %(default_audience)s, "
            "updated_at = now(), updated_by = %(actor)s WHERE subject = %(subject)s",
            {**values, "actor": actor},
        )
    return principal(conn, subject)


def set_disabled(conn, subject: str, disabled: bool, *, actor: str) -> None:
    subject = _subject(subject)
    with conn.transaction(), conn.cursor() as cur:
        _lock_roster(cur)
        cur.execute("SELECT is_admin, disabled_at FROM roster_principals WHERE subject = %s "
                    "FOR UPDATE", (subject,))
        row = cur.fetchone()
        if row is None:
            raise RosterRefused("that identity does not exist")
        if disabled and row[0] and row[1] is None and _admins_left(cur, excluding=subject) == 0:
            raise RosterRefused("the last administrator cannot be disabled")
        cur.execute(
            "UPDATE roster_principals SET disabled_at = CASE WHEN %s THEN now() ELSE NULL END, "
            "updated_at = now(), updated_by = %s WHERE subject = %s",
            (disabled, actor, subject),
        )
        if disabled:
            cur.execute("UPDATE roster_sessions SET revoked_at = now() WHERE subject = %s "
                        "AND revoked_at IS NULL", (subject,))


def delete_principal(conn, subject: str) -> None:
    subject = _subject(subject)
    with conn.transaction(), conn.cursor() as cur:
        _lock_roster(cur)
        cur.execute("SELECT is_admin, disabled_at FROM roster_principals WHERE subject = %s "
                    "FOR UPDATE", (subject,))
        row = cur.fetchone()
        if row is None:
            raise RosterRefused("that identity does not exist")
        if row[0] and row[1] is None and _admins_left(cur, excluding=subject) == 0:
            raise RosterRefused("the last administrator cannot be deleted")
        cur.execute("DELETE FROM roster_principals WHERE subject = %s", (subject,))


def list_principals(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_PRINCIPAL_COLUMNS)} FROM roster_principals ORDER BY subject")
        rows = cur.fetchall()
        cur.execute(
            "SELECT subject, count(*) FILTER (WHERE revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > now())), max(last_used_at) "
            "FROM roster_tokens GROUP BY subject")
        tokens = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
    result = []
    for row in rows:
        values = dict(zip(_PRINCIPAL_COLUMNS, row, strict=True))
        live, last_used = tokens.get(values["subject"], (0, None))
        result.append({
            "subject": values["subject"], "display_name": values["display_name"],
            "kind": values["kind"], "is_admin": values["is_admin"],
            "groups": list(values["groups"] or ()),
            "default_audience": (list(values["default_audience"])
                                 if values["default_audience"] is not None else None),
            "unrestricted": UNRESTRICTED_GROUP in (values["groups"] or ()),
            "disabled": values["disabled_at"] is not None,
            "live_tokens": live, "last_used_at": _iso(last_used),
            "created_at": _iso(values["created_at"]), "updated_at": _iso(values["updated_at"]),
        })
    return result


# ── administering groups ──────────────────────────────────────────────────────────────────────
def list_groups(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT g.name, g.description, g.created_at, "
            "(SELECT count(*) FROM roster_principals p WHERE g.name = ANY(p.groups)) "
            "FROM roster_groups g ORDER BY g.name")
        return [{"name": r[0], "description": r[1], "created_at": _iso(r[2]), "members": r[3],
                 "unrestricted": r[0] == UNRESTRICTED_GROUP} for r in cur.fetchall()]


def create_group(conn, name: str, description: str, *, actor: str) -> dict:
    clean = str(name or "").strip()
    if not layers.is_layer_name(clean):
        raise RosterRefused("a layer name is 2 to 32 lowercase letters, digits, or hyphens, starting "
                            "with a letter, and not a reserved word")
    description = " ".join(str(description or "").split())
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise RosterRefused("the description is too long")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO roster_groups (name, description, created_by) VALUES (%s, %s, %s) "
                    "ON CONFLICT (tenant_id, name) DO NOTHING", (clean, description, actor))
        if cur.rowcount != 1:
            raise RosterRefused("that group already exists")
    return {"name": clean, "description": description}


def update_group(conn, name: str, description: str) -> None:
    description = " ".join(str(description or "").split())
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise RosterRefused("the description is too long")
    with conn.cursor() as cur:
        cur.execute("UPDATE roster_groups SET description = %s WHERE name = %s", (description, name))
        if cur.rowcount != 1:
            raise RosterRefused("that group does not exist")


def delete_group(conn, name: str) -> None:
    if name == UNRESTRICTED_GROUP:
        raise RosterRefused("the unrestricted group cannot be deleted")
    with conn.transaction(), conn.cursor() as cur:
        _lock_roster(cur)
        cur.execute("SELECT 1 FROM roster_principals WHERE %s = ANY(groups) "
                    "OR %s = ANY(coalesce(default_audience, '{}')) LIMIT 1", (name, name))
        in_use = cur.fetchone() is not None
        cur.execute("SELECT 1 FROM roster_slack_channels WHERE %s = ANY(coalesce(groups, '{}')) "
                    "LIMIT 1", (name,))
        if in_use or cur.fetchone() is not None:
            raise RosterRefused("a group in use by a person or a channel cannot be deleted")
        _refuse_unless_empty(cur, name)
        cur.execute("DELETE FROM roster_groups WHERE name = %s", (name,))
        if cur.rowcount != 1:
            raise RosterRefused("that group does not exist")


def _refuse_unless_empty(cur, layer: str) -> None:
    """Only an index that has converged on the repository can show a layer is empty, and a capture
    still waiting to be filed there would recreate its content."""
    cur.execute("SELECT to_regclass('pages_index') IS NOT NULL, to_regclass('index_health') IS NOT NULL, "
                "to_regclass('capture_queue') IS NOT NULL, to_regclass('knowledge_changes') IS NOT NULL")
    has_index, has_health, has_queue, has_ledger = cur.fetchone()
    converged = None
    if has_index and has_health:
        # A commit the webhook has not reported yet is newer than the index's last update.
        newer = ("NOT EXISTS (SELECT 1 FROM knowledge_changes WHERE created_at > "
                 "greatest(last_incremental_at, last_full_rebuild_at))" if has_ledger else "true")
        cur.execute("SELECT NOT dirty AND repository_head_sha = indexed_commit_sha AND "
                    f"{newer} FROM index_health WHERE singleton")
        converged = cur.fetchone()
    if not converged or not converged[0]:
        raise RosterRefused("the search index has not caught up with the repository, so the layer "
                            "cannot be shown to be empty; try again after the index rebuild")
    cur.execute("SELECT 1 FROM pages_index WHERE acl @> ARRAY[%s]::text[] LIMIT 1", (layer,))
    if cur.fetchone() is not None:
        raise RosterRefused("a layer that holds knowledge cannot be deleted")
    if has_queue:
        cur.execute("SELECT 1 FROM capture_queue WHERE acl @> ARRAY[%s]::text[] "
                    "AND status NOT IN ('landed', 'failed') LIMIT 1", (layer,))
        if cur.fetchone() is not None:
            raise RosterRefused("a capture is still being filed in this layer")


# ── administering tokens ──────────────────────────────────────────────────────────────────────
def issue_token(conn, subject: str, label: str, *, actor: str,
                expires_at: dt.datetime | None = None) -> TokenGrant:
    subject = _subject(subject)
    label = " ".join(str(label or "").split())
    if not label or len(label) > MAX_LABEL_CHARS:
        raise RosterRefused("a token label of at most 80 characters is required")
    if expires_at is not None and expires_at <= dt.datetime.now(dt.UTC):
        raise RosterRefused("the expiry must be in the future")
    token = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    token_id = str(uuid.uuid4())
    prefix = token[: len(TOKEN_PREFIX) + 6]
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM roster_principals WHERE subject = %s", (subject,))
        if cur.fetchone() is None:
            raise RosterRefused("that identity does not exist")
        cur.execute(
            "INSERT INTO roster_tokens (id, subject, label, prefix, sha256, created_by, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (token_id, subject, label, prefix, hash_token(token), actor, expires_at),
        )
    return TokenGrant(token=token, id=token_id, subject=subject, label=label, prefix=prefix,
                      expires_at=_iso(expires_at))


def list_tokens(conn, subject: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, label, prefix, created_at, created_by, last_used_at, expires_at, revoked_at "
            "FROM roster_tokens WHERE subject = %s ORDER BY created_at DESC",
            (_subject(subject),))
        rows = cur.fetchall()
    now = dt.datetime.now(dt.UTC)
    return [{"id": str(r[0]), "label": r[1], "prefix": r[2], "created_at": _iso(r[3]),
             "created_by": r[4], "last_used_at": _iso(r[5]), "expires_at": _iso(r[6]),
             "revoked_at": _iso(r[7]),
             "live": r[7] is None and (r[6] is None or r[6] > now)} for r in rows]


def revoke_token(conn, token_id: str) -> str:
    try:
        token_uuid = str(uuid.UUID(str(token_id)))
    except ValueError as error:
        raise RosterRefused("that token does not exist") from error
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT subject FROM roster_tokens WHERE id = %s AND revoked_at IS NULL "
                    "FOR UPDATE", (token_uuid,))
        row = cur.fetchone()
        if row is None:
            raise RosterRefused("that token does not exist or is already revoked")
        subject = row[0]
        cur.execute("UPDATE roster_tokens SET revoked_at = now() WHERE id = %s", (token_uuid,))
        cur.execute("UPDATE roster_sessions SET revoked_at = now() WHERE token_id = %s "
                    "AND revoked_at IS NULL", (token_uuid,))
    return subject


# ── Slack channels ────────────────────────────────────────────────────────────────────────────
def list_channels(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT channel_id, groups, updated_at, updated_by FROM roster_slack_channels "
                    "ORDER BY channel_id")
        return [{"channel_id": r[0], "groups": list(r[1]) if r[1] is not None else None,
                 "updated_at": _iso(r[2]), "updated_by": r[3]} for r in cur.fetchall()]


def set_channel(conn, channel_id: str, groups, *, actor: str) -> None:
    channel_id = str(channel_id or "").strip().upper()
    if not _CHANNEL_RE.fullmatch(channel_id):
        raise RosterRefused("a Slack channel id looks like C0123456789")
    with conn.transaction(), conn.cursor() as cur:
        _lock_roster(cur)
        if groups is not None and list(groups) == [layers.PUBLIC]:
            groups = None
        audience = (None if groups is None
                    else list(_groups(conn, groups, subject="channel audience")))
        if audience is not None and len(audience) != 1:
            raise RosterRefused("a channel files into exactly one layer")
        if audience is not None and UNRESTRICTED_GROUP in audience:
            raise RosterRefused("a channel cannot capture for the unrestricted group")
        cur.execute(
            "INSERT INTO roster_slack_channels (channel_id, groups, updated_by) VALUES (%s, %s, %s) "
            "ON CONFLICT (tenant_id, channel_id) DO UPDATE SET groups = EXCLUDED.groups, updated_at = now(), "
            "updated_by = EXCLUDED.updated_by",
            (channel_id, audience, actor),
        )


def delete_channel(conn, channel_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM roster_slack_channels WHERE channel_id = %s",
                    (str(channel_id or "").strip().upper(),))
        if cur.rowcount != 1:
            raise RosterRefused("that channel is not mapped")


# ── backoffice sessions ───────────────────────────────────────────────────────────────────────
def open_session(conn, token: str) -> tuple[str, Session]:
    """Exchange a live token for a new session value, shown to the browser once. An agent is never
    an administrator, so its session reaches only the wiki it may read."""
    who, token_id = principal_for_token(conn, token)
    value = secrets.token_urlsafe(SESSION_BYTES)
    expires = dt.datetime.now(dt.UTC) + SESSION_ABSOLUTE
    with tenancy.tenant_scope(conn, who.tenant), conn.cursor() as cur:
        cur.execute("DELETE FROM roster_sessions WHERE expires_at < now() - %s "
                    "OR revoked_at < now() - %s", (SESSION_ABSOLUTE, SESSION_ABSOLUTE))
        cur.execute("INSERT INTO roster_sessions (sha256, subject, token_id, expires_at) "
                    "VALUES (%s, %s, %s, %s)", (hash_token(value), who.subject, token_id, expires))
    return value, Session(principal=who, token_id=token_id, expires_at=expires)


def session_principal(conn, value: str | None) -> Session:
    """The principal behind a live session; expired, idle, revoked, and unknown read alike."""
    if not value:
        raise IdentityError(UNKNOWN)
    columns = ", ".join(f"p.{column}" for column in _PRINCIPAL_COLUMNS)
    with tenancy.platform_scope(conn), conn.cursor() as cur:
        cur.execute(
            f"SELECT {columns}, p.tenant_id, s.token_id, s.expires_at FROM roster_sessions s "
            "JOIN roster_principals p ON p.tenant_id = s.tenant_id AND p.subject = s.subject "
            "JOIN roster_tokens t ON t.id = s.token_id "
            f"{_LIVE_TENANT}"
            "WHERE s.sha256 = %s AND s.revoked_at IS NULL AND s.expires_at > now() "
            "AND s.last_seen_at > now() - %s AND p.disabled_at IS NULL "
            "AND t.revoked_at IS NULL AND (t.expires_at IS NULL OR t.expires_at > now())",
            (hash_token(value), SESSION_IDLE),
        )
        row = cur.fetchone()
        if row is None:
            raise IdentityError(UNKNOWN)
        cur.execute("UPDATE roster_sessions SET last_seen_at = now() WHERE sha256 = %s "
                    "AND last_seen_at < now() - interval '1 minute'", (hash_token(value),))
    return Session(principal=_principal(row[:-3], row[-3]), token_id=str(row[-2]),
                   expires_at=row[-1])


def close_session(conn, value: str | None) -> None:
    if not value:
        return
    with tenancy.platform_scope(conn), conn.cursor() as cur:
        cur.execute("UPDATE roster_sessions SET revoked_at = now() WHERE sha256 = %s "
                    "AND revoked_at IS NULL", (hash_token(value),))


# ── bootstrap and one-time import ─────────────────────────────────────────────────────────────
def admin_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM roster_principals WHERE is_admin AND disabled_at IS NULL")
        return cur.fetchone()[0]


def bootstrap_admin(conn, subject: str, display_name: str, *, actor: str) -> TokenGrant:
    """Create the first administrator and its token, only while no administrator exists."""
    with conn.transaction(), conn.cursor() as cur:
        _lock_roster(cur)
        if admin_count(conn) > 0:
            raise RosterRefused("an administrator already exists")
        create_principal(conn, {
            "subject": subject, "display_name": display_name, "kind": "person",
            "is_admin": True, "groups": [UNRESTRICTED_GROUP], "default_audience": [layers.PUBLIC],
        }, actor=actor)
        return issue_token(conn, subject, "bootstrap", actor=actor)


def import_roster(conn, *, principals: dict[str, Principal], channels: dict,
                  token_store: dict[str, str], admins: set[str], agents: set[str],
                  actor: str) -> dict:
    """Load a legacy file roster, channel map, and token digests into an empty roster, through
    the same validation as every backoffice edit, in one transaction."""
    admins = {subject.strip().lower() for subject in admins}
    agents = {subject.strip().lower() for subject in agents}
    if admins & agents:
        raise RosterRefused("an identity cannot be both an administrator and an agent")
    keys = {subject.strip().lower(): who for subject, who in principals.items()}
    if (admins | agents) - set(keys):
        raise RosterRefused("an administrator or agent is not in the roster")
    groups = {g for who in keys.values() for g in (*who.groups, *(who.default_audience or ()))
              if g != layers.PUBLIC}
    groups |= {g for audience in channels.values() if audience for g in audience}
    with conn.transaction(), conn.cursor() as cur:
        _lock_roster(cur)
        cur.execute("SELECT count(*) FROM roster_principals")
        if cur.fetchone()[0]:
            raise RosterRefused("the roster already has people; the import runs only once")
        for name in sorted(groups - known_groups(conn)):
            create_group(conn, name, "", actor=actor)
        for key, who in sorted(keys.items()):
            create_principal(conn, {
                "subject": key, "display_name": who.display_name,
                "kind": "agent" if key in agents else "person", "is_admin": key in admins,
                "groups": list(who.groups),
                "default_audience": (list(who.default_audience)
                                     if who.default_audience is not None else [layers.PUBLIC]),
            }, actor=actor)
        for number, (digest, subject) in enumerate(sorted(token_store.items()), 1):
            key = subject.strip().lower()
            if key not in keys:
                raise RosterRefused("the token store names an identity the roster lacks")
            cur.execute(
                "INSERT INTO roster_tokens (id, subject, label, prefix, sha256, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (str(uuid.uuid4()), key, f"imported #{number}", f"sha256:{digest[:8]}", digest,
                 actor),
            )
        for channel_id, audience in channels.items():
            set_channel(conn, channel_id, list(audience) if audience is not None else None,
                        actor=actor)
        if admin_count(conn) == 0:
            raise RosterRefused("the import must name at least one administrator")
    return {"principals": len(keys), "tokens": len(token_store), "channels": len(channels),
            "groups": len(groups)}


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None
