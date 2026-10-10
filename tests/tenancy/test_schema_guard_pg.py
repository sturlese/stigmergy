"""TEN-01: every tenant table carries its tenant, forces the one isolation policy, and keys every
unique index by tenant; a table added later without them fails here."""
from stigmergy.admin.schema import ensure_admin_schema
from stigmergy.index import measures, store
from stigmergy.kernel import tenancy
from stigmergy.server import usage
from stigmergy.server.audit import ensure_audit_table
from stigmergy.slack.store import ensure_slack_schema

PLATFORM_TABLES = {"tenants", "platform_operators", "platform_tokens", "platform_sessions",
                   "platform_actions", "access_requests", "index_meta", "worker_heartbeat",
                   "webhook_deliveries", "workspace_erasures"}
# Unique keys that need no tenant: random identifiers and credential digests are unique across the
# platform by construction.
GLOBAL_KEYS = {("id",), ("sha256",), ("capture_id", "position")}


def _all_schemas(conn) -> None:
    usage.ensure_schema(conn)
    ensure_audit_table(conn)
    ensure_admin_schema(conn)
    ensure_slack_schema(conn)
    measures.ensure_schema(conn)
    store.ensure_webhook_dedupe_table(conn)
    meta = store.read_meta(conn)
    store.init_schema(conn, dim=(meta or {}).get("dim") or 256, model=(meta or {}).get("model") or "fake",
                      fts_config="english")


def test_every_tenant_table_is_isolated_by_policy(conn):
    _all_schemas(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = current_schema() AND c.relkind = 'r'")
        tables = {name: (secured, forced) for name, secured, forced in cur.fetchall()}
        cur.execute("SELECT table_name, data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND column_name = 'tenant_id'")
        columns = {name: (kind, nullable) for name, kind, nullable in cur.fetchall()}
        cur.execute("SELECT tablename, policyname, qual, with_check FROM pg_policies "
                    "WHERE schemaname = current_schema()")
        policies: dict[str, list] = {}
        for table, name, qual, check in cur.fetchall():
            policies.setdefault(table, []).append((name, qual, check))
    tenant_tables = sorted(set(tables) - PLATFORM_TABLES)
    assert {"roster_principals", "capture_queue", "pages_index", "page_fragments",
            "embedding_cache", "knowledge_changes", "audit_log", "model_usage"} <= set(tenant_tables)
    for table in tenant_tables:
        assert columns.get(table) == ("uuid", "NO"), table
        assert tables[table] == (True, True), table
        assert [name for name, _qual, _check in policies.get(table, [])] == [tenancy.POLICY], table
        (_name, qual, check), = policies[table]
        assert "app.tenant_id" in qual and "app.scope" in qual, table
        assert qual == check, table
    for table in PLATFORM_TABLES & set(tables):
        assert table not in columns, table


def test_every_unique_index_of_a_tenant_table_includes_the_tenant(conn):
    _all_schemas(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT t.relname, i.relname,
                   array(SELECT a.attname FROM unnest(x.indkey) WITH ORDINALITY AS k(attnum, ord)
                         JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
                         ORDER BY k.ord)
            FROM pg_index x
            JOIN pg_class t ON t.oid = x.indrelid
            JOIN pg_class i ON i.oid = x.indexrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = current_schema() AND x.indisunique""")
        unique = cur.fetchall()
    offenders = [(table, index, columns) for table, index, columns in unique
                 if table not in PLATFORM_TABLES and "tenant_id" not in columns
                 and tuple(columns) not in GLOBAL_KEYS]
    assert offenders == []
