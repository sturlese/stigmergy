"""Recreate a non-production Stigmergy environment from an empty baseline."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from psycopg import sql

from stigmergy.admin.schema import ensure_admin_schema
from stigmergy.capture import schema, uploads
from stigmergy.changes.store import ensure_change_schema
from stigmergy.entities.model import registry_bytes
from stigmergy.index import store
from stigmergy.server.audit import ensure_audit_table
from stigmergy.slack.store import ensure_slack_schema

ENVIRONMENTS = frozenset({"test", "staging"})
KNOWLEDGE_ZONES = ("wiki", "sources")
EMPTY_ZONES = (
    "wiki/notes",
    "wiki/concepts",
    "wiki/entities",
    "sources",
)
CONTROL_PATHS = (
    ".claude",
    ".github",
    "ops/identities.json",
    "ops/slack-channels.json",
    "ops/templates",
)


class ResetRefused(RuntimeError):
    pass


def confirmation_token(
    *, environment: str, database: str, bucket: str, repository: str | Path
) -> str:
    return ":".join(
        (
            environment,
            database,
            bucket,
            str(Path(repository).expanduser().resolve()),
        )
    )


def validate_target(
    conn,
    *,
    environment: str,
    database: str,
    bucket: str,
    repository: str | Path,
    confirmation: str,
) -> None:
    if environment not in ENVIRONMENTS:
        raise ResetRefused("only test and staging environments can be reset")
    if not database or not bucket:
        raise ResetRefused("database and evidence bucket must be named explicitly")
    with conn.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        actual_database = cursor.fetchone()[0]
    if actual_database != database:
        raise ResetRefused("connected database does not match the declared target")
    expected = confirmation_token(
        environment=environment,
        database=database,
        bucket=bucket,
        repository=repository,
    )
    if confirmation != expected:
        raise ResetRefused("confirmation does not exactly match the reset target")


def recreate_database(
    conn,
    *,
    embedding_dim: int,
    embedding_model: str,
    fts_config: str = "english",
) -> None:
    with conn.transaction(), conn.cursor() as cursor:
        cursor.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        )
        tables = [row[0] for row in cursor.fetchall()]
        if tables:
            cursor.execute(
                sql.SQL("DROP TABLE {} CASCADE").format(
                    sql.SQL(", ").join(sql.Identifier(table) for table in tables)
                )
            )

    schema.ensure_capture_schema(conn)
    uploads.ensure_upload_schema(conn)
    ensure_change_schema(conn)
    ensure_admin_schema(conn)
    ensure_audit_table(conn)
    ensure_slack_schema(conn)
    store.init_schema(
        conn,
        dim=embedding_dim,
        model=embedding_model,
        fts_config=fts_config,
        host="reset",
    )
    store.ensure_webhook_dedupe_table(conn)


def clear_evidence(evidence) -> int:
    if hasattr(evidence, "objects"):
        count = len(evidence.objects)
        evidence.objects.clear()
        return count

    client = evidence.client()
    continuation = None
    deleted = 0
    while True:
        arguments = {"Bucket": evidence.bucket, "MaxKeys": 1000}
        if continuation:
            arguments["ContinuationToken"] = continuation
        response = client.list_objects_v2(**arguments)
        keys = [item["Key"] for item in response.get("Contents", ())]
        if keys:
            client.delete_objects(
                Bucket=evidence.bucket,
                Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True},
            )
            deleted += len(keys)
        if not response.get("IsTruncated"):
            return deleted
        continuation = response.get("NextContinuationToken")
        if not continuation:
            raise RuntimeError("evidence listing did not provide a continuation token")


def recreate_repository(
    repository: str | Path,
    *,
    control_source: str | Path | None = None,
) -> None:
    target = Path(repository).expanduser().resolve()
    source = Path(control_source).expanduser().resolve() if control_source else target
    if target == Path(target.anchor) or len(target.parts) < 3:
        raise ResetRefused("repository target is too broad")
    if source != target and not source.is_dir():
        raise ResetRefused("control source does not exist")

    preserved: dict[str, bytes] = {}
    for relpath in CONTROL_PATHS:
        item = source / relpath
        if item.is_file():
            preserved[relpath] = item.read_bytes()

    target.mkdir(parents=True, exist_ok=True)
    for zone in KNOWLEDGE_ZONES:
        path = target / zone
        if path.exists():
            shutil.rmtree(path)
    registry = target / "ops" / "entity-registry.json"
    if registry.exists():
        registry.unlink()

    if source != target:
        for relpath in CONTROL_PATHS:
            item = source / relpath
            destination = target / relpath
            if item.is_dir():
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.copytree(item, destination)

    for relpath, data in preserved.items():
        destination = target / relpath
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    for relpath in EMPTY_ZONES:
        directory = target / relpath
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".gitkeep").write_text("", encoding="utf-8")
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_bytes(registry_bytes({}))


def reset_environment(
    conn,
    evidence,
    *,
    environment: str,
    database: str,
    bucket: str,
    repository: str | Path,
    confirmation: str,
    embedding_dim: int,
    embedding_model: str,
    fts_config: str = "english",
    control_source: str | Path | None = None,
) -> dict:
    if getattr(evidence, "bucket", None) != bucket:
        raise ResetRefused("configured evidence store does not match the declared bucket")
    validate_target(
        conn,
        environment=environment,
        database=database,
        bucket=bucket,
        repository=repository,
        confirmation=confirmation,
    )
    recreate_database(
        conn,
        embedding_dim=embedding_dim,
        embedding_model=embedding_model,
        fts_config=fts_config,
    )
    deleted_objects = clear_evidence(evidence)
    recreate_repository(repository, control_source=control_source)
    return {"deleted_objects": deleted_objects, "status": "reset"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m stigmergy.ops.reset")
    parser.add_argument("--environment", choices=sorted(ENVIRONMENTS), required=True)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--control-source")
    parser.add_argument("--embedding-dim", type=int, required=True)
    parser.add_argument("--embedding-model", required=True)
    parser.add_argument("--fts-config", default="english")
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)

    from stigmergy.capture.evidence import store_from_env

    with store.connect(args.dsn) as conn:
        result = reset_environment(
            conn,
            store_from_env(),
            environment=args.environment,
            database=args.database,
            bucket=args.bucket,
            repository=args.repo,
            confirmation=args.confirm,
            embedding_dim=args.embedding_dim,
            embedding_model=args.embedding_model,
            fts_config=args.fts_config,
            control_source=args.control_source,
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
