"""The removal ledger's contract: the DDL and the vocabularies.

Idempotent DDL behind `capture.schema.startup_ddl_lock`, the shared advisory lock — the same
posture `gardener/schema.py` takes, and for the same reason: two processes may start at once.

**One row per page removal that LANDED**, written by `librarian.processing` once the commit is
pushed. That is the whole of what this table is for now, and the reason it exists beside the
capture row that asked for the removal: the capture is purged with the retention window, and what
left the corpus is not something a deployment may forget. A removal that was refused changed
nothing and is answered on its own capture.

**A removal is applied or it is not; nothing waits.** The person decided at the door, the worker
performed it, and this records what it did — the pages that went, the pages that were rewritten so
they would stop pointing at them, and the diff of both, because nobody read that prose before it
landed.

**Rows this version can no longer write still live here.** A real database holds `edits`,
`entity-body` and `entity-alias` rows from the elective repair loop, and `pending`/`approved`/
`rejected` statuses from before it applied without asking. `KINDS` and `STATUSES` below are the
vocabulary a ROW MAY CARRY — history included — while `WRITABLE_KIND` and `WRITABLE_STATUS` are
what this version inserts. Narrowing the CHECK to what code writes would refuse the rows already
there: `ALTER TABLE ... ADD CONSTRAINT ... CHECK` validates the existing table, and the whole DDL
sequence would abort on every start.
"""
from stigmergy.capture.schema import startup_ddl_lock

# ── kind — what a row DID to the corpus ──────────────────────────────────────────────────────
# `delete` is the one kind anything writes: one removal performs two different actions — pages
# removed, pages rewritten to stop pointing at them — so its ops carry `delete-page`/`scrub-page`
# (`DELETE_OP_NAMES`) and a reader can tell which is which.
KIND_DELETE = "delete"
WRITABLE_KIND = KIND_DELETE

# The three the elective repair loop wrote before it was measured against `docs/DESIGN.md` §2 and
# removed: an additive edit, a drafted entity body, a merge of two registry entries. Named rather
# than dropped, because they are the `kind` of rows a deployed database still holds and the CHECK
# below is asserted against those rows.
RETIRED_KINDS = ("edits", "entity-body", "entity-alias")
KINDS = (KIND_DELETE, *RETIRED_KINDS)

# ── status — what happened, and the whole vocabulary a row may carry ─────────────────────────
# `applied` landed a commit, and it is the only one this version writes. `failed` and `skipped`
# are the elective loop's — a repair derived and refused, and a finding no repair could answer.
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
WRITABLE_STATUS = STATUS_APPLIED
STATUSES = (STATUS_APPLIED, STATUS_FAILED, STATUS_SKIPPED)

# The CHECK constraints are the vocabularies above, spelled for SQL. `repr`, not
# `capture.schema.sql_literals`: that helper SORTS, and a CHECK's definition string is committed
# to databases in DECLARATION order — sorting would change it. Safe only because both
# vocabularies are lowercase identifiers with no quote or backslash.
_KIND_SQL_LIST = ", ".join(repr(k) for k in KINDS)
_STATUS_SQL_LIST = ", ".join(repr(s) for s in STATUSES)

_REPAIRS_DDL = f"""
CREATE TABLE IF NOT EXISTS repairs (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id BIGINT NOT NULL DEFAULT 0,
    finding_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    finding_subjects JSONB NOT NULL DEFAULT '[]'::jsonb,
    kind TEXT NOT NULL DEFAULT '{KIND_DELETE}' CHECK (kind IN ({_KIND_SQL_LIST})),
    target_paths JSONB NOT NULL DEFAULT '[]'::jsonb,
    ops JSONB NOT NULL DEFAULT '[]'::jsonb,
    rationale TEXT NOT NULL DEFAULT '',
    content_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ({_STATUS_SQL_LIST})),
    applied_commit TEXT NOT NULL DEFAULT '',
    diff TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL DEFAULT ''
)
"""

# The rename, and the reason it is a rename rather than a fresh table beside the old one: the rows
# in `repair_proposals` are the record of every change this deployment ever landed outside a
# filing, and a new table would leave that history in a table nothing reads. Runs BEFORE the
# `CREATE TABLE IF NOT EXISTS` above, so a database that has the old name arrives at the new one
# with its rows, and a fresh database skips it entirely.
_RENAME_FROM_PROPOSALS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'repair_proposals' AND relkind = 'r')
       AND NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'repairs' AND relkind = 'r') THEN
        ALTER TABLE repair_proposals RENAME TO repairs;
    END IF;
END $$
"""

# `CREATE TABLE IF NOT EXISTS` never adds a column to a table that already exists, so every column
# this version introduced is added explicitly — a renamed `repair_proposals` has none of them.
_REPAIRS_COLUMNS = (
    "ALTER TABLE repairs ADD COLUMN IF NOT EXISTS finding_subjects JSONB NOT NULL "
    "DEFAULT '[]'::jsonb",
    "ALTER TABLE repairs ADD COLUMN IF NOT EXISTS diff TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE repairs ADD COLUMN IF NOT EXISTS reason TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE repairs ALTER COLUMN content_key SET DEFAULT ''",
)

# What the three retired statuses become. A row that was waiting on somebody when this shipped was
# never applied and never refused: it is a change that did not happen, which is what `skipped`
# means. The reason says so rather than leaving a status a reader has to remember the history of.
# Idempotent by construction — after the first run no row matches.
# **Dropped BEFORE the migration below, and this ordering is the whole of a production defect.**
# `ALTER TABLE ... RENAME` carries a table's CONSTRAINTS with it, so an upgraded database still has
# `repair_proposals_status_check` — which permits `pending|approved|rejected|applied|failed` and
# does NOT permit `skipped`. The migration's own UPDATE therefore violated the constraint that was
# still standing, before the swap that replaces it ever ran: the whole DDL sequence aborted with
# CheckViolation, the server exited 2 on every start, and the app crash-looped. It shipped green
# because the test covering the migration dropped this constraint first — constructing the one
# state in which the sequence works, which is not the state an upgrade starts from.
#
# So the swap is three steps, not two: drop the old vocabulary, write the new value, add the new
# vocabulary. It cannot be one atomic drop-and-add, because an UPDATE has to happen in between.
# The window where the column is unconstrained is inside `startup_ddl_lock` and closes two
# statements later.
_DROP_LEGACY_STATUS_CHECK = (
    "ALTER TABLE repairs DROP CONSTRAINT IF EXISTS repair_proposals_status_check"
)

_MIGRATE_RETIRED_STATUSES = f"""
UPDATE repairs
   SET status = '{STATUS_SKIPPED}',
       reason = CASE WHEN reason <> '' THEN reason
                     ELSE 'this repair was waiting on a person when repairs began applying '
                          'themselves; it was never applied' END
 WHERE status IN ('pending', 'approved', 'rejected')
"""

# The columns of the decision that no longer happens. Dropped rather than left empty: a column
# nothing writes is a column a reader assumes something writes.
_DROP_DECISION_COLUMNS = (
    "ALTER TABLE repairs DROP COLUMN IF EXISTS decided_by",
    "ALTER TABLE repairs DROP COLUMN IF EXISTS decided_at",
    "ALTER TABLE repairs DROP COLUMN IF EXISTS notes",
)

# ONE row per content key, over the rows that have one. Partial, because the elective loop's
# `skipped` rows had no ops to key on and several of them would otherwise collide on the empty
# string — which is also what lets a removal, keyed by nothing, be recorded as often as somebody
# asks for one.
_REPAIRS_CONTENT_KEY_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS repairs_content_key_idx "
    "ON repairs (content_key) WHERE content_key <> ''"
)
# The old partial index, whose predicate names a status that no longer exists. Dropped by name:
# left in place it would keep enforcing uniqueness over rows nothing can create, and a reader
# would find an index for a lifecycle the code has forgotten.
_DROP_PENDING_KEY_INDEX = "DROP INDEX IF EXISTS repair_proposals_pending_key_idx"
_REPAIRS_STATUS_INDEX = (
    "CREATE INDEX IF NOT EXISTS repairs_status_idx ON repairs (status, id)"
)

# The name Postgres itself gives the inline column CHECKs above, spelled out so the swaps below can
# reach a constraint on a table created — or renamed — before this version.
KIND_CHECK_NAME = "repairs_kind_check"
STATUS_CHECK_NAME = "repairs_status_check"

# The two non-additive migrations, and `capture.schema`'s `_CAPTURE_QUEUE_STATUS_CHECK` verbatim in
# shape because the problem is identical: `CREATE TABLE IF NOT EXISTS` never touches a table that
# already exists, so a vocabulary changed here and nowhere else would be refused by every deployed
# database — an IntegrityError on the first row of the new shape, in production, at night.
#
# ONE `DO` statement each, never a DROP-then-ADD pair: as two statements the table is briefly
# unconstrained, every process start takes an ACCESS EXCLUSIVE lock, and two concurrent starters
# race into `DuplicateObject`. The guard skips the swap once the existing definition already names
# every value (`quote_literal`, so one name cannot match inside another).
#
# The renamed table brings `repair_proposals_kind_check`/`_status_check` with it, so each swap also
# drops the old name — otherwise a row would have to satisfy both the old vocabulary and the new.
def _check_swap(column: str, name: str, old_name: str, values: str) -> str:
    return f"""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'repairs'::regclass
          AND c.conname = '{name}'
          AND (SELECT bool_and(pg_get_constraintdef(c.oid) LIKE '%' || quote_literal(v) || '%')
               FROM unnest(ARRAY[{values}]) AS v)
    ) THEN
        ALTER TABLE repairs DROP CONSTRAINT IF EXISTS {old_name};
        ALTER TABLE repairs DROP CONSTRAINT IF EXISTS {name};
        ALTER TABLE repairs ADD CONSTRAINT {name} CHECK ({column} IN ({values}));
    END IF;
END $$
"""


_REPAIRS_KIND_CHECK = _check_swap("kind", KIND_CHECK_NAME, "repair_proposals_kind_check",
                                  _KIND_SQL_LIST)
_REPAIRS_STATUS_CHECK = _check_swap("status", STATUS_CHECK_NAME, "repair_proposals_status_check",
                                    _STATUS_SQL_LIST)

# Order is load-bearing, and in BOTH directions — which is what the first version of it got wrong.
# Rename, create, columns, then DROP THE LEGACY STATUS CHECK, then the status migration, then the
# CHECK swaps. A swap run before the migration would refuse the rows the migration exists to fix;
# a migration run before the legacy drop is refused by the constraint the rename brought with it.
_ALL_DDL = (_RENAME_FROM_PROPOSALS, _REPAIRS_DDL, *_REPAIRS_COLUMNS,
            _DROP_LEGACY_STATUS_CHECK, _MIGRATE_RETIRED_STATUSES,
            *_DROP_DECISION_COLUMNS, _REPAIRS_KIND_CHECK, _REPAIRS_STATUS_CHECK,
            _DROP_PENDING_KEY_INDEX, _REPAIRS_CONTENT_KEY_INDEX, _REPAIRS_STATUS_INDEX)


def ensure_repair_schema(conn) -> None:
    """Idempotent DDL for `repairs` — safe on every startup and from two processes at once (the
    shared `startup_ddl_lock`), and safe on a database that still has `repair_proposals`."""
    with startup_ddl_lock(conn) as cur:
        for statement in _ALL_DDL:
            cur.execute(statement)


# ── the op record: the stored shape of ONE act inside a removal ──────────────────────────────
# The stored key is `op`, not `kind`, so a persisted op is never mistaken for the row's own `kind`
# by anything reading JSON.
OP_KIND_KEY = "op"
# The `delete` kind's two op names and two shapes. A removal names only the page; a scrub carries
# the bytes it was computed FROM (so "the corpus moved" is a fact rather than a guess) and the bytes
# it would write (so the apply's recomputation has something to byte-compare against).
#
# The NAMES live here rather than in `repair.deletion` because this module is the bottom of this
# package: it imports nothing of its own.
DELETE_OP_NAME = "delete-page"
SCRUB_OP_NAME = "scrub-page"
DELETE_OP_FIELDS = (OP_KIND_KEY, "path")
SCRUB_OP_FIELDS = (OP_KIND_KEY, "path", "expected_before_hash", "planned_after")
# The GROUP, beside the names: three surfaces have to know "every op a removal performs" — the
# validator, the console's op cleaner and the plan — and each rebuilding the tuple is how a third
# op reaches a reader rendered as something else. `tests/test_architecture.py` pins the console's
# table against this one.
DELETE_OP_NAMES = (DELETE_OP_NAME, SCRUB_OP_NAME)


def target_paths(ops) -> list[str]:
    """The pages a removal touched, deduplicated and sorted — the stored column beside `ops`, and
    what a reader scans before opening the diff."""
    return sorted({str(o.get("path", "")) for o in (ops or ()) if str(o.get("path", ""))})
