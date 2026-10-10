"""The workspace every evaluation runner indexes and reads in, beside any real tenant."""
from stigmergy.kernel import tenancy
from stigmergy.server import platform

EVAL_TENANT = "00000000-0000-4000-8000-0000000000e1"
EVAL_SLUG = "evals"


def bind(conn) -> str:
    """Register the evaluation workspace once and act for it on `conn`."""
    platform.ensure_platform_schema(conn)
    with tenancy.platform_scope(conn), conn.cursor() as cur:
        cur.execute("INSERT INTO tenants (id, slug, name, status, repo) VALUES (%s, %s, %s, 'active', "
                    "%s) ON CONFLICT DO NOTHING",
                    (EVAL_TENANT, EVAL_SLUG, "Evaluations", "stigmergy-evals/evals-brain"))
    tenancy.set_tenant(conn, EVAL_TENANT)
    return EVAL_TENANT
