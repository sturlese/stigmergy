"""The findings store's contract: the DDL and the vocabulary.

Idempotent DDL behind `capture.schema.startup_ddl_lock`, the shared advisory lock. `check` is a
reserved SQL keyword, so the column is `check_slug`; the Python key stays `"check"` and
`check_slug` never leaks past the one INSERT/SELECT that names it. Watermarks live in
`job_runs.stats`, not in a table of their own.
"""
from stigmergy.capture.schema import startup_ddl_lock

JOB_NAME = "gardener"

# ── severity ──────────────────────────────────────────────────────────────────────────────────
SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_SLA = "sla"
SEVERITIES = (SEVERITY_INFO, SEVERITY_WARN, SEVERITY_SLA)
# Report order: SLA first, then WARN, then INFO — worst news first.
SEVERITY_ORDER = (SEVERITY_SLA, SEVERITY_WARN, SEVERITY_INFO)

# ── source — the column's CHECK constraint accepts exactly these two ─────────────────────────
SOURCE_DETERMINISTIC = "deterministic"
SOURCE_MODEL = "model"
SOURCES = (SOURCE_DETERMINISTIC, SOURCE_MODEL)

# The deterministic checks' bound on corpus-derived text in `detail`/`suggested_action`.
MAX_DETAIL_CHARS = 500

# The model sweep's TIGHTER bound on `detail` (rationale + excerpt combined). Enforced twice:
# `_validate` rejects an oversized excerpt, and the composed string is hard-clamped regardless —
# the clamp is what actually guarantees the column bound.
MAX_MODEL_DETAIL_CHARS = 200

# The two CHECK constraints are the vocabularies above, spelled for SQL — a value the code can
# produce and the column would reject is the drift these constants exist to make impossible.
_SEVERITY_SQL_LIST = ", ".join(repr(s) for s in SEVERITIES)
_SOURCE_SQL_LIST = ", ".join(repr(s) for s in SOURCES)

_GARDENER_FINDINGS_DDL = f"""
CREATE TABLE IF NOT EXISTS gardener_findings (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL,
    check_slug TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ({_SEVERITY_SQL_LIST})),
    source TEXT NOT NULL DEFAULT 'deterministic' CHECK (source IN ({_SOURCE_SQL_LIST})),
    subject TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    suggested_action TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
# `ADD COLUMN IF NOT EXISTS` because `CREATE TABLE IF NOT EXISTS` never adds a column to a table
# that already exists; the DEFAULT fills pre-existing rows, so no backfill is needed.
_GARDENER_FINDINGS_MODEL_ID_COLUMN = (
    "ALTER TABLE gardener_findings ADD COLUMN IF NOT EXISTS model_id TEXT NOT NULL DEFAULT ''"
)
_GARDENER_FINDINGS_RUN_INDEX = (
    "CREATE INDEX IF NOT EXISTS gardener_findings_run_idx ON gardener_findings (run_id)"
)

_ALL_DDL = (_GARDENER_FINDINGS_DDL, _GARDENER_FINDINGS_MODEL_ID_COLUMN, _GARDENER_FINDINGS_RUN_INDEX)


def ensure_gardener_schema(conn) -> None:
    """Idempotent DDL for `gardener_findings` — safe on every startup and from two processes at
    once (the shared `startup_ddl_lock`)."""
    with startup_ddl_lock(conn) as cur:
        for statement in _ALL_DDL:
            cur.execute(statement)
