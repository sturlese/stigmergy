"""The findings store's contract: the DDL and the vocabulary.

Same DDL ownership posture as `capture.schema` — one idempotent `CREATE TABLE IF NOT EXISTS` run
at startup, behind `capture.schema.startup_ddl_lock` (the SAME advisory lock every other
startup-DDL run in this database takes; `server/review.py::ensure_review_schema` is the closest
sibling and this mirrors it).

**`check` is a reserved SQL keyword** (Postgres, and the SQL standard), so the column is named
`check_slug` — never bare `check`, which would need quoting at every single call site and is
exactly the kind of friction that grows a typo. The field name survives at the PYTHON boundary:
every finding dict this package builds, persists and renders uses the key `"check"` (see
`store.py`), and `check_slug` never leaks past the one INSERT/SELECT that names it.

**Watermarks live in `job_runs.stats`, not in a table of their own.** It fits: `gardener`'s own
run stats already carry counts per check/severity, and `stigmergy.digest` reads the latest COMPLETED
run for `job='gardener'` via `job_runs`'s own `(job, started_at DESC)` index — no second table
needed.
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

# ── source: the deterministic checks write `deterministic`, the model editorial sweep writes
# `model`, and the column's own CHECK constraint accepts exactly those two ─────────────────────
SOURCE_DETERMINISTIC = "deterministic"
SOURCE_MODEL = "model"
SOURCES = (SOURCE_DETERMINISTIC, SOURCE_MODEL)

# Bound on any free text this package interpolates into `detail`/`suggested_action` from
# corpus-derived values (a metric name, a page title) — never user-supplied prose, but bounded on
# the same "bounded text" instinct `capture.schema.MAX_HINT_CHARS` applies everywhere in this
# codebase. This is the deterministic checks' bound; the model sweep gets its own, tighter one
# immediately below.
MAX_DETAIL_CHARS = 500

# The model sweep's own, TIGHTER bound on `detail` — the rationale + excerpt a model produces,
# combined, never the deterministic checks' 500-char allowance above. Enforced twice:
# `gardener.sweep._validate` rejects an oversized excerpt from the model (a named, retriable
# validation failure), and the composed `detail` string is hard-clamped to this width regardless
# (`stigmergy.text.clamp`) — the second guard is what actually GUARANTEES the column never exceeds
# it, independent of what the model's own two fields add up to.
MAX_MODEL_DETAIL_CHARS = 200

_GARDENER_FINDINGS_DDL = """
CREATE TABLE IF NOT EXISTS gardener_findings (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL,
    check_slug TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warn', 'sla')),
    source TEXT NOT NULL DEFAULT 'deterministic' CHECK (source IN ('deterministic', 'model')),
    subject TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    suggested_action TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
# A sweep finding persists `source='model'` plus the id of the model that produced it. Additive,
# via the SAME `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` convention `capture/schema.py`
# (`report`/`asked_at`/`trace`/...) already uses to grow a table that shipped before a column did:
# idempotent, and safe against a database that already holds `gardener_findings` without this
# column (`CREATE TABLE IF NOT EXISTS` alone would never add it — that form only guards table
# CREATION, not a column on a table that already exists). The DEFAULT fills every pre-existing
# row, so no separate backfill is needed.
_GARDENER_FINDINGS_MODEL_ID_COLUMN = (
    "ALTER TABLE gardener_findings ADD COLUMN IF NOT EXISTS model_id TEXT NOT NULL DEFAULT ''"
)
# The digest's health section and the report's own re-print both read "every finding for run N" —
# a plain index on the foreign key, not a composite one: this table is never large enough (one
# run's worth of findings) to need ordering baked into the index itself.
_GARDENER_FINDINGS_RUN_INDEX = (
    "CREATE INDEX IF NOT EXISTS gardener_findings_run_idx ON gardener_findings (run_id)"
)

_ALL_DDL = (_GARDENER_FINDINGS_DDL, _GARDENER_FINDINGS_MODEL_ID_COLUMN, _GARDENER_FINDINGS_RUN_INDEX)


def ensure_gardener_schema(conn) -> None:
    """Idempotent DDL for `gardener_findings` — safe on every startup and safe from two processes
    at once (the same `startup_ddl_lock` `capture.schema.ensure_capture_schema` and
    `server.review.ensure_review_schema` both take on this connection)."""
    with startup_ddl_lock(conn) as cur:
        for statement in _ALL_DDL:
            cur.execute(statement)
