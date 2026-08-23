"""The findings store's contract: the DDL and the vocabulary.

Idempotent DDL behind `capture.schema.startup_ddl_lock`, the shared advisory lock. `check` is a
reserved SQL keyword, so the column is `check_slug`; the Python key stays `"check"` and
`check_slug` never leaks past the one INSERT/SELECT that names it. Watermarks live in
`job_runs.stats`, not in a table of their own.
"""
from stigmergy.capture.schema import startup_ddl_lock

JOB_NAME = "gardener"

# ── severity ──────────────────────────────────────────────────────────────────────────────────
# A closed, ordered vocabulary, and every reader of it — `report.py`'s grouped sections,
# `digest.render`/`digest.sections`, the admin console's chips — spells it off these names.
SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITIES = (SEVERITY_INFO, SEVERITY_WARN)
# Report order: WARN, then INFO — worst news first.
SEVERITY_ORDER = (SEVERITY_WARN, SEVERITY_INFO)

# ── source — the column's CHECK constraint accepts exactly these two ─────────────────────────
# Every finding a run writes now is `deterministic`: the model passes that were the only producer
# of `model` are retired. `SOURCE_MODEL` stays, and stays in `SOURCES`, for the rows they already
# wrote — a deployed `gardener_findings` holds them, and this is what says what such a row means
# rather than leaving a reader to guess. Keeping it in the CHECK also keeps a fresh database's
# constraint identical to a deployed one's, which `CREATE TABLE IF NOT EXISTS` could not have
# reconciled afterwards in either direction.
SOURCE_DETERMINISTIC = "deterministic"
SOURCE_MODEL = "model"          # historical only — nothing produces it any more
SOURCES = (SOURCE_DETERMINISTIC, SOURCE_MODEL)

# The deterministic checks' bound on corpus-derived text in `detail`/`suggested_action`.
MAX_DETAIL_CHARS = 500

# The two CHECK constraints are the vocabularies above, spelled for SQL — a value the code can
# produce and the column would reject is the drift these constants exist to make impossible.
# One direction only: `CREATE TABLE IF NOT EXISTS` never narrows a constraint on a table that
# already exists, so an already-deployed database keeps whatever vocabulary it was created with.
# That is safe while the change is a REMOVAL (nothing can write the retired value any more) and is
# the reason ADDING one needs a migration rather than an edit here.
# `repr`, not `capture.schema.sql_literals`: that helper SORTS, and these CHECKs are already
# committed to databases in DECLARATION order — sorting would change the constraint's definition
# string. Safe only because both vocabularies are lowercase identifiers with no quote or backslash.
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
# that already exists; the DEFAULT fills pre-existing rows, so no backfill is needed. It is `''`
# on every row a run writes now — only a retired model pass ever named a model — and the column
# stays because dropping it would destroy the record of which model made the findings it made.
_GARDENER_FINDINGS_MODEL_ID_COLUMN = (
    "ALTER TABLE gardener_findings ADD COLUMN IF NOT EXISTS model_id TEXT NOT NULL DEFAULT ''"
)
# `subject` is the DISPLAY string — a report line, comma-joined when a finding names several
# pages. `subjects` is the same fact as DATA, so a consumer that has to act on the pages reads a
# list instead of re-splitting prose that was never a parseable format.
# Added the same additive way as `model_id`: the `'[]'` default fills every pre-existing row, so
# no backfill is needed and a finding filed before this column existed reads as "names no page",
# which is exactly what its `subject` could be recovered as anyway.
_GARDENER_FINDINGS_SUBJECTS_COLUMN = (
    "ALTER TABLE gardener_findings ADD COLUMN IF NOT EXISTS subjects JSONB NOT NULL "
    "DEFAULT '[]'::jsonb"
)
_GARDENER_FINDINGS_RUN_INDEX = (
    "CREATE INDEX IF NOT EXISTS gardener_findings_run_idx ON gardener_findings (run_id)"
)

_ALL_DDL = (_GARDENER_FINDINGS_DDL, _GARDENER_FINDINGS_MODEL_ID_COLUMN,
            _GARDENER_FINDINGS_SUBJECTS_COLUMN, _GARDENER_FINDINGS_RUN_INDEX)


def ensure_gardener_schema(conn) -> None:
    """Idempotent DDL for `gardener_findings` — safe on every startup and from two processes at
    once (the shared `startup_ddl_lock`)."""
    with startup_ddl_lock(conn) as cur:
        for statement in _ALL_DDL:
            cur.execute(statement)
