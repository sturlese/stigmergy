"""Refile one workspace's knowledge into a fresh workspace from the same captures, then hand its
people over.

A contract change that reshapes existing pages (a new page role, a new skill) reaches an existing
workspace only by filing its captures again. The operator creates the target workspace in the
master console, exports the source, imports into the target, lets the writer file every capture,
compares the two graphs, and then moves the source's credentials to the target:

  python -m stigmergy.ops.replay_workspace --operator EMAIL export --workspace SOURCE --out FILE
  python -m stigmergy.ops.replay_workspace --operator EMAIL import --workspace TARGET --file FILE \\
      --confirm SOURCE:TARGET [--order IDS]
  python -m stigmergy.ops.replay_workspace --operator EMAIL handover --from SOURCE --to TARGET \\
      --confirm SOURCE:TARGET [--accept-unlanded N]

Export and import each act for exactly one workspace. Export leaves out every capture whose source
a person deleted. Import copies the layers and people (never tokens) into a target that holds
nothing else yet, and queues each exported capture once, in order, under an id derived from the
target and the original id, with its original actor, layer, origin, and artifact references: the
evidence objects are content-addressed and already stored. Running it again queues nothing twice.
Handover moves the live tokens and sessions of the people both workspaces share to the target once
every exported capture has landed there, and suspends the source. These are the one sanctioned
copy and credential move between workspaces.
"""
from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path

from stigmergy.capture import queue, schema
from stigmergy.index import store
from stigmergy.kernel import tenancy
from stigmergy.ops import erase_workspace
from stigmergy.server import platform, roster

_NAMESPACE = uuid.UUID("6f1c2d9e-3b7a-4c51-9e0f-2a8d4b6c7e10")
_KEY_PREFIX = "replay-"


class ReplayRefused(ValueError):
    """A replay step that may not run; the message is safe to show."""


def _workspace(conn, slug: str) -> platform.Tenant:
    found = platform.tenant_by_slug(conn, slug)
    if found is None:
        raise ReplayRefused(f"no workspace named {slug!r}")
    return found


def _operator(conn, subject: str) -> str:
    with tenancy.platform_scope(conn), conn.cursor() as cur:
        cur.execute("SELECT 1 FROM platform_operators WHERE subject = %s AND disabled_at IS NULL",
                    (str(subject or "").strip().lower(),))
        if cur.fetchone() is None:
            raise ReplayRefused("name an enabled platform operator with --operator")
    return str(subject).strip().lower()


def _no_slack(source: platform.Tenant) -> None:
    if source.slack_team_id:
        raise ReplayRefused("the source workspace owns the deployment's Slack app; move it first")


def export(conn, slug: str, *, operator: str) -> dict:
    """The source's layers, people, and landed captures whose sources nobody deleted."""
    source = _workspace(conn, slug)
    _no_slack(source)
    with tenancy.tenant_scope(conn, source.id), conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM capture_queue WHERE status IN ('queued', 'processing')")
        if cur.fetchone()[0]:
            raise ReplayRefused("the source still has captures waiting to be filed")
        groups = roster.list_groups(conn)
        principals = [p for p in roster.list_principals(conn) if not p["disabled"]]
        cur.execute(
            "SELECT q.id, q.request FROM capture_queue q WHERE q.operation = %s AND q.status = 'landed' "
            "AND NOT EXISTS (SELECT 1 FROM capture_artifacts a WHERE a.capture_id = q.id AND NOT a.live) "
            "ORDER BY q.created_at, q.id", (schema.CAPTURE,))
        captures = [{"id": str(row[0]), "request": row[1]} for row in cur.fetchall()]
        cur.execute(
            "SELECT count(*) FROM capture_queue q WHERE q.operation = %s AND q.status = 'landed' "
            "AND EXISTS (SELECT 1 FROM capture_artifacts a WHERE a.capture_id = q.id AND NOT a.live)",
            (schema.CAPTURE,))
        deleted = cur.fetchone()[0]
    with tenancy.platform_scope(conn):
        platform.record_action(conn, actor=operator, action="tenant.replay_export", tenant_slug=source.slug,
                               outcome="ok", args={"captures": len(captures), "deleted_sources": deleted})
    return {"workspace": source.slug, "workspace_id": source.id,
            "groups": [{"name": g["name"], "description": g.get("description", "")} for g in groups],
            "principals": [{key: p.get(key) for key in ("subject", "display_name", "kind", "is_admin", "groups",
                                                        "default_audience")} for p in principals],
            "captures": captures, "excluded": {"deleted_sources": deleted}}


def replay_id(target_id: str, original_id: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"{target_id}:{original_id}")


def import_(conn, slug: str, exported: dict, *, operator: str, confirm: str,
            order: list[str] | None = None) -> dict:
    """Copy layers and people into a fresh target and queue each exported capture once, in order."""
    if confirm != f"{exported['workspace']}:{slug}":
        raise ReplayRefused(f"confirm with exactly {exported['workspace']}:{slug}")
    target = _workspace(conn, slug)
    if target.status != "active":
        raise ReplayRefused("the target workspace is not active")
    if target.id == exported["workspace_id"]:
        raise ReplayRefused("the target must be a different workspace")
    captures = {item["id"]: item for item in exported["captures"]}
    sequence = order if order is not None else list(captures)
    if len(sequence) != len(set(sequence)) or set(sequence) != set(captures):
        raise ReplayRefused("the order must list every exported capture exactly once")
    people = {p["subject"] for p in exported["principals"]}
    envelopes = []
    for original in sequence:
        envelope = schema.parse_capture(captures[original]["request"])
        if envelope.actor.subject.lower() not in people:
            raise ReplayRefused("an exported capture's author is not an exported person")
        envelopes.append(envelope.model_copy(update={"capture_id": replay_id(target.id, original),
                                                     "idempotency_key": f"{_KEY_PREFIX}{original}"}))
    keys = {envelope.idempotency_key for envelope in envelopes}
    queued = existing = 0
    with tenancy.tenant_scope(conn, target.id):
        with conn.cursor() as cur:
            cur.execute("SELECT idempotency_key FROM capture_queue")
            if {row[0] for row in cur.fetchall()} - keys:
                raise ReplayRefused("the target already holds work of its own; import only into a fresh workspace")
        if {p["subject"] for p in roster.list_principals(conn)} - people:
            raise ReplayRefused("the target has people the source lacks; import only into a fresh workspace")
        known = roster.known_groups(conn)
        for group in exported["groups"]:
            if group["name"] not in known:
                roster.create_group(conn, group["name"], group["description"], actor=operator)
        present = {p["subject"] for p in roster.list_principals(conn)}
        for who in exported["principals"]:
            fields = {key: who[key] for key in ("display_name", "kind", "is_admin", "groups", "default_audience")}
            if who["subject"] in present:
                roster.update_principal(conn, who["subject"], fields, actor=operator)
            else:
                roster.create_principal(conn, {"subject": who["subject"], **fields}, actor=operator)
        for envelope in envelopes:
            row = queue.enqueue_capture(conn, envelope)
            queued, existing = (queued + 1, existing) if row.get("created") else (queued, existing + 1)
    with tenancy.platform_scope(conn):
        platform.record_action(conn, actor=operator, action="tenant.replay_import", tenant_slug=target.slug,
                               outcome="ok", args={"from": exported["workspace"], "queued": queued})
    return {"workspace": target.slug, "queued": queued, "already_queued": existing}


def handover(conn, source_slug: str, target_slug: str, *, operator: str, confirm: str,
             accept_unlanded: int = 0) -> dict:
    """Move the live tokens and sessions of the people both workspaces share to the target, once
    every capture the source could export has landed there, and suspend the source."""
    if confirm != f"{source_slug}:{target_slug}":
        raise ReplayRefused(f"confirm with exactly {source_slug}:{target_slug}")
    source, target = _workspace(conn, source_slug), _workspace(conn, target_slug)
    if source.id == target.id:
        raise ReplayRefused("the source and target must differ")
    _no_slack(source)
    with tenancy.platform_scope(conn), conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT id, status FROM tenants WHERE id IN (%s, %s) ORDER BY id FOR UPDATE",
                    (source.id, target.id))
        status = {str(row[0]): row[1] for row in cur.fetchall()}
        if status.get(target.id) != "active":
            raise ReplayRefused("the target workspace is not active")
        if any(erase_workspace.is_open(conn, workspace) for workspace in (source.id, target.id)):
            raise ReplayRefused("a workspace in this handover is being deleted")
        cur.execute("SELECT count(*) FROM capture_queue WHERE tenant_id = %s AND status IN ('queued', 'processing')",
                    (target.id,))
        if cur.fetchone()[0]:
            raise ReplayRefused("the target is still filing; hand over once its queue is empty")
        cur.execute(
            "SELECT count(*) FROM capture_queue s WHERE s.tenant_id = %(src)s AND s.operation = %(op)s "
            "AND s.status = 'landed' AND s.idempotency_key NOT LIKE %(prefix)s "
            "AND NOT EXISTS (SELECT 1 FROM capture_artifacts a WHERE a.capture_id = s.id AND NOT a.live) "
            "AND NOT EXISTS (SELECT 1 FROM capture_queue t WHERE t.tenant_id = %(dst)s AND t.status = 'landed' "
            "AND t.submitted_by = s.submitted_by AND t.idempotency_key = %(bare)s || s.id::text)",
            {"src": source.id, "dst": target.id, "op": schema.CAPTURE, "prefix": f"{_KEY_PREFIX}%",
             "bare": _KEY_PREFIX})
        unlanded = cur.fetchone()[0]
        if unlanded != accept_unlanded:
            raise ReplayRefused(f"{unlanded} of the source's captures have not landed in the target; "
                                f"pass --accept-unlanded {unlanded} to hand over without them")
        cur.execute(
            "UPDATE roster_tokens t SET tenant_id = %(dst)s WHERE t.tenant_id = %(src)s AND t.revoked_at IS NULL "
            "AND (t.expires_at IS NULL OR t.expires_at > now()) "
            "AND EXISTS (SELECT 1 FROM roster_principals p WHERE p.tenant_id = %(src)s AND p.subject = t.subject "
            "AND p.disabled_at IS NULL) "
            "AND EXISTS (SELECT 1 FROM roster_principals p WHERE p.tenant_id = %(dst)s AND p.subject = t.subject "
            "AND p.disabled_at IS NULL) RETURNING t.id",
            {"src": source.id, "dst": target.id})
        moved = [row[0] for row in cur.fetchall()]
        if not moved:
            raise ReplayRefused("no live token holder of the source is a person of the target workspace")
        cur.execute("UPDATE roster_sessions SET tenant_id = %s WHERE token_id = ANY(%s)", (target.id, moved))
        sessions = cur.rowcount
        cur.execute("SELECT count(*) FROM roster_tokens WHERE tenant_id = %s AND revoked_at IS NULL", (source.id,))
        left = cur.fetchone()[0]
        platform.set_tenant_status(conn, source.id, "suspended")
        platform.record_action(conn, actor=operator, action="tenant.handover", tenant_slug=target.slug,
                               outcome="ok", args={"from": source.slug, "tokens": len(moved), "left": left,
                                                   "unlanded": unlanded})
    return {"from": source.slug, "to": target.slug, "tokens": len(moved), "sessions": sessions,
            "left_behind": left, "unlanded": unlanded, "suspended": source.slug}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stigmergy.ops.replay_workspace")
    parser.add_argument("--dsn", default=os.environ.get(store.DSN_ENV, ""))
    parser.add_argument("--operator", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    out = commands.add_parser("export")
    out.add_argument("--workspace", required=True)
    out.add_argument("--out", required=True)
    into = commands.add_parser("import")
    into.add_argument("--workspace", required=True)
    into.add_argument("--file", required=True)
    into.add_argument("--confirm", required=True)
    into.add_argument("--order", help="a JSON list of every exported capture id, in the order to file them")
    move = commands.add_parser("handover")
    move.add_argument("--from", dest="source", required=True)
    move.add_argument("--to", dest="target", required=True)
    move.add_argument("--confirm", required=True)
    move.add_argument("--accept-unlanded", type=int, default=0)
    args = parser.parse_args(argv)
    with store.connect(args.dsn) as conn:
        operator = _operator(conn, args.operator)
        if args.command == "export":
            exported = export(conn, args.workspace, operator=operator)
            Path(args.out).write_text(json.dumps(exported, ensure_ascii=False))
            Path(args.out).chmod(0o600)
            result = {"workspace": exported["workspace"], "captures": len(exported["captures"]),
                      "principals": len(exported["principals"]), "groups": len(exported["groups"]),
                      **exported["excluded"]}
        elif args.command == "import":
            exported = json.loads(Path(args.file).read_text())
            order = json.loads(Path(args.order).read_text()) if args.order else None
            result = import_(conn, args.workspace, exported, operator=operator, confirm=args.confirm, order=order)
        else:
            result = handover(conn, args.source, args.target, operator=operator, confirm=args.confirm,
                              accept_unlanded=args.accept_unlanded)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
