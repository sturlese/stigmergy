"""Tenants, platform operators with their tokens and sessions, access requests, and the platform audit.

These tables carry no tenant: they are what the platform knows about tenants. Operator credentials
are stored like roster credentials, as SHA-256 digests of 256-bit random values. An operator acts in
one tenant at a time through its session, as an administrator who reads every layer; it queues no
writes, which the tenant's own administrators make.
"""
from __future__ import annotations

import datetime as dt
import re
import secrets
import uuid
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from stigmergy.capture.schema import startup_ddl_lock
from stigmergy.kernel import layers, tenancy
from stigmergy.server import roster
from stigmergy.server.errors import IdentityError
from stigmergy.server.identity import UNRESTRICTED_GROUP, Principal, hash_token

MAX_NAME_CHARS = 120
MAX_COMPANY_CHARS = 200
MAX_NOTE_CHARS = 500
OPERATOR_TOKEN_DAYS = 90
ACCESS_STATUSES = ("new", "handled")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")

_DDL = (
    """
    CREATE TABLE IF NOT EXISTS platform_operators (
        subject TEXT PRIMARY KEY CHECK (subject = lower(subject)),
        display_name TEXT NOT NULL,
        disabled_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_by TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS platform_tokens (
        id UUID PRIMARY KEY,
        subject TEXT NOT NULL REFERENCES platform_operators(subject) ON DELETE CASCADE,
        label TEXT NOT NULL,
        prefix TEXT NOT NULL,
        sha256 TEXT NOT NULL UNIQUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_by TEXT NOT NULL DEFAULT '',
        last_used_at TIMESTAMPTZ,
        expires_at TIMESTAMPTZ,
        revoked_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS platform_sessions (
        sha256 TEXT PRIMARY KEY,
        subject TEXT NOT NULL REFERENCES platform_operators(subject) ON DELETE CASCADE,
        token_id UUID NOT NULL REFERENCES platform_tokens(id) ON DELETE CASCADE,
        acting_tenant_id UUID REFERENCES tenants(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ NOT NULL,
        revoked_at TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS platform_actions (
        id BIGSERIAL PRIMARY KEY,
        ts TIMESTAMPTZ NOT NULL DEFAULT now(),
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        tenant_slug TEXT NOT NULL DEFAULT '',
        args JSONB NOT NULL DEFAULT '{}'::jsonb,
        outcome TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS platform_actions_ts_idx ON platform_actions (ts DESC)",
    f"""
    CREATE TABLE IF NOT EXISTS access_requests (
        id UUID PRIMARY KEY,
        email TEXT NOT NULL,
        company TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'new'
            CHECK (status IN ({", ".join(f"'{s}'" for s in ACCESS_STATUSES)})),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        handled_by TEXT NOT NULL DEFAULT '',
        handled_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS access_requests_created_idx ON access_requests (created_at DESC)",
    # No foreign key to `tenants` and no `tenant_id`: the request and its receipt outlive the
    # workspace it deleted, and only the master console reads them.
    """
    CREATE TABLE IF NOT EXISTS workspace_erasures (
        id UUID PRIMARY KEY,
        workspace_id UUID NOT NULL,
        slug TEXT NOT NULL,
        name TEXT NOT NULL,
        repo TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
        requested_by TEXT NOT NULL,
        requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        repo_deleted BOOLEAN NOT NULL DEFAULT FALSE,
        rows_deleted BOOLEAN NOT NULL DEFAULT FALSE,
        garbage JSONB NOT NULL DEFAULT '[]',
        receipt JSONB NOT NULL DEFAULT '{}',
        error TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS workspace_erasures_one_open ON workspace_erasures (workspace_id) "
    "WHERE status IN ('queued', 'running')",
)


# What a reset keeps beside the roster: the tenants and who operates the platform.
TABLES = ("tenants", "platform_operators", "platform_tokens", "platform_sessions",
          "platform_actions", "access_requests", "workspace_erasures")


def ensure_platform_schema(conn) -> None:
    with startup_ddl_lock(conn) as cur:
        tenancy.ensure_tenants(cur)
        for statement in _DDL:
            cur.execute(statement)


class PlatformRefused(ValueError):
    """A platform change that would break a rule. The message is safe to show."""


# ── tenants ───────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Tenant:
    id: str
    slug: str
    name: str
    status: str
    repo: str
    slack_team_id: str | None

    def as_json(self) -> dict:
        return {"id": self.id, "slug": self.slug, "name": self.name, "status": self.status,
                "repo": self.repo, "slack_team_id": self.slack_team_id}


_TENANT_COLUMNS = "id, slug, name, status, repo, slack_team_id"


def _tenant(row) -> Tenant | None:
    if row is None:
        return None
    return Tenant(id=str(row[0]), slug=row[1], name=row[2], status=row[3], repo=row[4],
                  slack_team_id=row[5])


def _one(conn, where: str, value) -> Tenant | None:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_TENANT_COLUMNS} FROM tenants WHERE {where} = %s", (value,))
        return _tenant(cur.fetchone())


def tenant_by_id(conn, tenant_id) -> Tenant | None:
    try:
        return _one(conn, "id", str(uuid.UUID(str(tenant_id))))
    except ValueError:
        return None


def tenant_by_slug(conn, slug: str) -> Tenant | None:
    return _one(conn, "slug", str(slug or "").strip().lower())


def tenant_by_repo(conn, repo: str) -> Tenant | None:
    return _one(conn, "repo", str(repo or ""))


def tenant_by_team(conn, team_id: str) -> Tenant | None:
    return _one(conn, "slack_team_id", str(team_id or ""))


def list_tenants(conn, *, statuses=tenancy.STATUSES) -> list[Tenant]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_TENANT_COLUMNS} FROM tenants WHERE status = ANY(%s) ORDER BY slug",
                    (list(statuses),))
        return [_tenant(row) for row in cur.fetchall()]


def resolve_tenant(conn, slug: str | None) -> Tenant:
    """The workspace a single-tenant process acts for: the one named, or the only active one."""
    if slug:
        found = tenant_by_slug(conn, slug)
        if found is None or found.status == "suspended":
            raise PlatformRefused(f"no usable workspace named {slug!r}")
        return found
    active = list_tenants(conn, statuses=("active",))
    if len(active) != 1:
        raise PlatformRefused("name the workspace with --tenant or $STIGMERGY_TENANT")
    return active[0]


def check_slug(value) -> str:
    slug = str(value or "").strip().lower()
    if not tenancy.SLUG_RE.fullmatch(slug) or slug in tenancy.RESERVED_SLUGS:
        raise PlatformRefused("a workspace name is 2 to 32 lowercase letters, digits, or hyphens, "
                              "starting with a letter")
    return slug


def create_tenant(conn, *, slug: str, name: str, repo: str, actor: str,
                  status: str = "provisioning", slack_team_id: str | None = None) -> Tenant:
    """Register a tenant and seed its roster. Its repository is created by the worker."""
    slug = check_slug(slug)
    name = " ".join(str(name or "").split())
    if not name or len(name) > MAX_NAME_CHARS:
        raise PlatformRefused("a name of at most 120 characters is required")
    if not _REPO_RE.fullmatch(str(repo or "")):
        raise PlatformRefused("the repository must be owner/name")
    if status not in tenancy.STATUSES:
        raise PlatformRefused("unknown tenant status")
    tenant_id = str(uuid.uuid4())
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("INSERT INTO tenants (id, slug, name, status, repo, slack_team_id, created_by) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (tenant_id, slug, name, status, repo, slack_team_id, actor))
        if cur.rowcount != 1:
            raise PlatformRefused("that workspace name or repository is already taken")
        with tenancy.tenant_scope(conn, tenant_id):
            roster.seed_tenant(conn)
    return tenant_by_id(conn, tenant_id)


def set_tenant_status(conn, tenant_id, status: str) -> Tenant:
    if status not in tenancy.STATUSES:
        raise PlatformRefused("unknown tenant status")
    with conn.cursor() as cur:
        cur.execute("UPDATE tenants SET status = %s WHERE id = %s", (status, str(tenant_id)))
        if cur.rowcount != 1:
            raise PlatformRefused("that workspace does not exist")
    return tenant_by_id(conn, tenant_id)


# ── operators ─────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class OperatorSession:
    subject: str
    display_name: str
    token_id: str
    expires_at: dt.datetime
    acting_tenant: str | None


def _subject(value) -> str:
    subject = str(value or "").strip().lower()
    if not _EMAIL_RE.fullmatch(subject) or len(subject) > 254:
        raise PlatformRefused("the operator must be an email address")
    return subject


def operator_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM platform_operators WHERE disabled_at IS NULL")
        return cur.fetchone()[0]


def _issue(conn, subject: str, label: str, *, actor: str) -> roster.TokenGrant:
    token = roster.TOKEN_PREFIX + secrets.token_urlsafe(roster.TOKEN_BYTES)
    token_id = str(uuid.uuid4())
    prefix = token[: len(roster.TOKEN_PREFIX) + 6]
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(days=OPERATOR_TOKEN_DAYS)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO platform_tokens "
                    "(id, subject, label, prefix, sha256, created_by, expires_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (token_id, subject, label, prefix, hash_token(token), actor, expires))
    return roster.TokenGrant(token=token, id=token_id, subject=subject, label=label, prefix=prefix,
                             expires_at=expires.isoformat())


def bootstrap_operator(conn, subject: str, display_name: str, *, actor: str) -> roster.TokenGrant:
    """Create the first operator and its token, only while no operator exists."""
    subject = _subject(subject)
    display_name = " ".join(str(display_name or "").split())
    if not display_name or len(display_name) > MAX_NAME_CHARS:
        raise PlatformRefused("a display name of at most 120 characters is required")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('stigmergy.platform.operators'))")
        if operator_count(conn) > 0:
            raise PlatformRefused("an operator already exists")
        cur.execute("INSERT INTO platform_operators (subject, display_name, created_by) "
                    "VALUES (%s, %s, %s)", (subject, display_name, actor))
        return _issue(conn, subject, "bootstrap", actor=actor)


def mint_operator_token(conn, subject: str, *, actor: str) -> roster.TokenGrant:
    """A new token for an existing enabled operator who lost theirs."""
    subject = _subject(subject)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM platform_operators WHERE subject = %s AND disabled_at IS NULL",
                    (subject,))
        if cur.fetchone() is None:
            raise PlatformRefused("that operator does not exist")
    return _issue(conn, subject, "minted", actor=actor)


def operator_for_token(conn, token: str | None) -> tuple[str, str, str]:
    """`(subject, display name, token id)` of the enabled operator holding a live token."""
    if not token:
        raise IdentityError(roster.UNKNOWN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT o.subject, o.display_name, t.id FROM platform_tokens t "
            "JOIN platform_operators o ON o.subject = t.subject "
            "WHERE t.sha256 = %s AND t.revoked_at IS NULL "
            "AND (t.expires_at IS NULL OR t.expires_at > now()) AND o.disabled_at IS NULL",
            (hash_token(token),),
        )
        row = cur.fetchone()
        if row is None:
            raise IdentityError(roster.UNKNOWN)
        cur.execute("UPDATE platform_tokens SET last_used_at = now() WHERE id = %s "
                    "AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')",
                    (row[2],))
    return row[0], row[1], str(row[2])


def open_operator_session(conn, token: str) -> tuple[str, OperatorSession]:
    subject, display_name, token_id = operator_for_token(conn, token)
    value = secrets.token_urlsafe(roster.SESSION_BYTES)
    expires = dt.datetime.now(dt.UTC) + roster.SESSION_ABSOLUTE
    with conn.cursor() as cur:
        cur.execute("DELETE FROM platform_sessions WHERE expires_at < now() - %s "
                    "OR revoked_at < now() - %s", (roster.SESSION_ABSOLUTE, roster.SESSION_ABSOLUTE))
        cur.execute("INSERT INTO platform_sessions (sha256, subject, token_id, expires_at) "
                    "VALUES (%s, %s, %s, %s)", (hash_token(value), subject, token_id, expires))
    return value, OperatorSession(subject=subject, display_name=display_name, token_id=token_id,
                                  expires_at=expires, acting_tenant=None)


def operator_session(conn, value: str | None) -> OperatorSession:
    """The operator behind a live session; expired, idle, revoked, and unknown read alike."""
    if not value:
        raise IdentityError(roster.UNKNOWN)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT o.subject, o.display_name, s.token_id, s.expires_at, s.acting_tenant_id "
            "FROM platform_sessions s JOIN platform_operators o ON o.subject = s.subject "
            "JOIN platform_tokens t ON t.id = s.token_id "
            "WHERE s.sha256 = %s AND s.revoked_at IS NULL AND s.expires_at > now() "
            "AND s.last_seen_at > now() - %s AND o.disabled_at IS NULL "
            "AND t.revoked_at IS NULL AND (t.expires_at IS NULL OR t.expires_at > now())",
            (hash_token(value), roster.SESSION_IDLE),
        )
        row = cur.fetchone()
        if row is None:
            raise IdentityError(roster.UNKNOWN)
        cur.execute("UPDATE platform_sessions SET last_seen_at = now() WHERE sha256 = %s "
                    "AND last_seen_at < now() - interval '1 minute'", (hash_token(value),))
    return OperatorSession(subject=row[0], display_name=row[1], token_id=str(row[2]),
                           expires_at=row[3], acting_tenant=str(row[4]) if row[4] else None)


def set_acting_tenant(conn, value: str, tenant_id: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE platform_sessions SET acting_tenant_id = %s WHERE sha256 = %s "
                    "AND revoked_at IS NULL", (tenant_id, hash_token(value)))


def close_operator_session(conn, value: str | None) -> None:
    if not value:
        return
    with conn.cursor() as cur:
        cur.execute("UPDATE platform_sessions SET revoked_at = now() WHERE sha256 = %s "
                    "AND revoked_at IS NULL", (hash_token(value),))


def operator_principal(conn, subject: str | None, tenant_id: str) -> Principal | None:
    """An enabled operator acting in `tenant_id`, or None. The connection must already act for
    that tenant: the operator's groups are every layer the tenant has."""
    if not subject:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT subject, display_name FROM platform_operators "
                    "WHERE subject = %s AND disabled_at IS NULL", (str(subject).strip().lower(),))
        row = cur.fetchone()
    if row is None:
        return None
    if tenancy.current_tenant(conn) != str(tenant_id):
        raise tenancy.TenancyError("an operator principal is built inside its tenant")
    groups = sorted(roster.known_groups(conn) | {UNRESTRICTED_GROUP})
    return Principal(subject=row[0], display_name=row[1], groups=tuple(groups),
                     default_audience=(layers.PUBLIC,), kind="person", is_admin=True,
                     tenant=str(tenant_id), operator=True)


# ── access requests ───────────────────────────────────────────────────────────────────────────
def record_access_request(conn, *, email: str, company: str = "", note: str = "") -> None:
    """Store one request; the same answer whether or not the email was seen before."""
    email = str(email or "").strip().lower()
    if not _EMAIL_RE.fullmatch(email) or len(email) > 254:
        raise PlatformRefused("a valid email address is required")
    company = " ".join(str(company or "").split())[:MAX_COMPANY_CHARS]
    note = " ".join(str(note or "").split())[:MAX_NOTE_CHARS]
    with conn.cursor() as cur:
        cur.execute("INSERT INTO access_requests (id, email, company, note) VALUES (%s, %s, %s, %s)",
                    (str(uuid.uuid4()), email, company, note))


# A request is kept until it is answered and for a while after, never longer than a year.
ACCESS_REQUEST_RETENTION = ("(status = 'handled' AND handled_at < now() - interval '90 days') "
                            "OR created_at < now() - interval '365 days'")


def purge_access_requests(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('access_requests') IS NOT NULL")
        if not cur.fetchone()[0]:
            return 0
        cur.execute(f"DELETE FROM access_requests WHERE {ACCESS_REQUEST_RETENTION}")
        return cur.rowcount


def list_access_requests(conn, *, limit: int = 200) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, email, company, note, status, created_at, handled_by, handled_at "
                    "FROM access_requests ORDER BY created_at DESC LIMIT %s",
                    (max(1, min(int(limit), 500)),))
        rows = cur.fetchall()
    return [{"id": str(r[0]), "email": r[1], "company": r[2], "note": r[3], "status": r[4],
             "created_at": _iso(r[5]), "handled_by": r[6], "handled_at": _iso(r[7])} for r in rows]


def set_access_request_status(conn, request_id: str, status: str, *, actor: str) -> None:
    if status not in ACCESS_STATUSES:
        raise PlatformRefused("unknown request status")
    try:
        request_uuid = str(uuid.UUID(str(request_id)))
    except ValueError as error:
        raise PlatformRefused("that request does not exist") from error
    with conn.cursor() as cur:
        cur.execute("UPDATE access_requests SET status = %s, handled_by = %s, handled_at = "
                    "CASE WHEN %s = 'handled' THEN now() ELSE NULL END WHERE id = %s",
                    (status, actor if status == "handled" else "", status, request_uuid))
        if cur.rowcount != 1:
            raise PlatformRefused("that request does not exist")


# ── platform audit ────────────────────────────────────────────────────────────────────────────
def record_action(conn, *, actor: str, action: str, outcome: str, tenant_slug: str = "",
                  args: dict | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO platform_actions (actor, action, tenant_slug, args, outcome) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (actor, action, tenant_slug, Jsonb(args or {}), outcome))


def recent_actions(conn, *, limit: int = 50) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, ts, actor, action, tenant_slug, args, outcome FROM platform_actions "
                    "ORDER BY ts DESC, id DESC LIMIT %s", (max(1, min(int(limit), 500)),))
        rows = cur.fetchall()
    return [{"id": r[0], "ts": _iso(r[1]), "actor": r[2], "action": r[3], "tenant": r[4],
             "args": r[5], "outcome": r[6]} for r in rows]


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None
