"""The proposal store's contract: the DDL and the vocabularies.

Idempotent DDL behind `capture.schema.startup_ddl_lock`, the shared advisory lock — the same
posture `gardener/schema.py` takes, and for the same reason: two processes may start at once.

The one design decision worth stating here rather than in prose elsewhere: **a REJECTED row is
the dismissal memory.** `content_key` identifies a proposal by what it would DO, not by which
finding suggested it, and the proposer skips a key held by a pending, approved, rejected or
applied row. "Reviewed and declined" therefore finally exists as a durable fact, and a steward who
says no once is not asked the same question again the next night. The UNIQUE index is narrower on
purpose (pending only): re-proposing after a rejection is a decision a human makes, and the index
must not turn it into a database error.

`failed` is the one status the memory does NOT hold, and the asymmetry is the point: a rejection is
a human saying no, while a failed apply is a human having said YES to something that then hit a
gate, a race or a fault. The row stays as the operator-visible record; the SKIP does not, or the
one repair a steward actively wanted would be the one the loop can never offer again.
"""
import hashlib

from stigmergy.capture.schema import startup_ddl_lock

JOB_NAME = "repair-propose"

# ── kind — what a proposal WOULD DO ──────────────────────────────────────────────────────────
# `edits` is the librarian's three additive declared-edit shapes, applied by `edits.apply_declared`
# and proven additive by `gate_body_rewrite`. `entity-body` is the one kind that REPLACES text, and
# it is a separate kind rather than a fourth op precisely because it is a different question for
# the gates: it may only touch a page in the entity zone, only below that page's own H1, and only
# with the apply telling `GateContext.body_rewrite_allowed` which path was authorized (ADR 039,
# "entity-body: the second kind").
#
# The string doubles as the OP name inside such a proposal's single op, deliberately: one
# vocabulary word for one shape means `ops_preview.kinds` in the review lane names the thing a
# steward is being asked about without a second lookup table.
KIND_EDITS = "edits"
KIND_ENTITY_BODY = "entity-body"
KINDS = (KIND_EDITS, KIND_ENTITY_BODY)

# ── status — the lifecycle. `failed` is terminal for the ROW, never for the finding: a steward
# may propose again, and the `error` column says what went wrong. An approved proposal whose
# apply failed does NOT revert to pending; a silent revert would hide that a gate refused. ─────
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_APPLIED, STATUS_FAILED)

# The two verdicts a steward may return. `applied`/`failed` are outcomes code writes, never
# something a human hands in.
DECIDABLE = (STATUS_APPROVED, STATUS_REJECTED)

# The CHECK constraints are the vocabularies above, spelled for SQL. `repr`, not
# `capture.schema.sql_literals`: that helper SORTS, and a CHECK's definition string is committed
# to databases in DECLARATION order — sorting would change it. Safe only because both
# vocabularies are lowercase identifiers with no quote or backslash.
_KIND_SQL_LIST = ", ".join(repr(k) for k in KINDS)
_STATUS_SQL_LIST = ", ".join(repr(s) for s in STATUSES)

_REPAIR_PROPOSALS_DDL = f"""
CREATE TABLE IF NOT EXISTS repair_proposals (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    run_id BIGINT NOT NULL DEFAULT 0,
    finding_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    kind TEXT NOT NULL DEFAULT '{KIND_EDITS}' CHECK (kind IN ({_KIND_SQL_LIST})),
    target_paths JSONB NOT NULL,
    ops JSONB NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    content_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '{STATUS_PENDING}' CHECK (status IN ({_STATUS_SQL_LIST})),
    decided_by TEXT NOT NULL DEFAULT '',
    decided_at TIMESTAMPTZ,
    notes TEXT NOT NULL DEFAULT '',
    applied_commit TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL DEFAULT '',
    finding_subjects JSONB NOT NULL DEFAULT '[]'::jsonb
)
"""

# WHAT THE FINDINGS NAMED, as against `target_paths` (what the answer would EDIT). The two are
# routinely different — an `orphan-page` finding names the page nothing links to, and the repair
# edits the page that ought to link to it — so a dismissal memory keyed only on `target_paths`
# recognised neither that shape nor a one-sided answer to a two-page finding, and sent the same
# declined repair to the model every night under each new finding id.
#
# A LIST OF LISTS, one entry per finding answered, and never their union: a proposal answering two
# findings has to dismiss BOTH, while a union would dismiss only a hypothetical third finding
# naming all of those pages at once, which is not a finding anything produces.
#
# `ADD COLUMN IF NOT EXISTS` for the reason `gardener/schema.py` states: `CREATE TABLE IF NOT
# EXISTS` never adds a column to a table that already exists, and the `'[]'` default fills every
# pre-existing row — a proposal stored before this column reads as "named no subject", which falls
# back to the `target_paths` half exactly as it did before.
_REPAIR_PROPOSALS_FINDING_SUBJECTS_COLUMN = (
    "ALTER TABLE repair_proposals ADD COLUMN IF NOT EXISTS finding_subjects JSONB NOT NULL "
    "DEFAULT '[]'::jsonb"
)

# One PENDING proposal per content key — a second propose run that re-derives the same edit is a
# no-op, not a second question. Deliberately NOT unique across every status: the dismissal memory
# is enforced in `proposer.py` (which skips a key with any prior row) so a human who decides to
# re-propose after a rejection meets a decision, never a constraint violation.
_REPAIR_PROPOSALS_PENDING_KEY_INDEX = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS repair_proposals_pending_key_idx "
    f"ON repair_proposals (content_key) WHERE status IN ('{STATUS_PENDING}')"
)
_REPAIR_PROPOSALS_STATUS_INDEX = (
    "CREATE INDEX IF NOT EXISTS repair_proposals_status_idx ON repair_proposals (status, id)"
)

# The name Postgres itself gives the inline column CHECK above, spelled out so the swap below can
# reach the constraint on a table created before this constant existed.
KIND_CHECK_NAME = "repair_proposals_kind_check"

# The one non-additive migration, and `capture.schema`'s `_CAPTURE_QUEUE_STATUS_CHECK` verbatim in
# shape because the problem is identical: `CREATE TABLE IF NOT EXISTS` never touches a table that
# already exists, so a KIND added to `KINDS` and to nothing else would be refused by every deployed
# database — an IntegrityError on the first proposal of the new kind, in production, at night.
#
# ONE `DO` statement, never a DROP-then-ADD pair: as two statements the table is briefly
# unconstrained, every process start takes an ACCESS EXCLUSIVE lock, and two concurrent starters
# race into `DuplicateObject`. The guard skips the swap once the existing definition already names
# every kind (`quote_literal`, so one name cannot match inside another).
_REPAIR_PROPOSALS_KIND_CHECK = f"""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conrelid = 'repair_proposals'::regclass
          AND c.conname = '{KIND_CHECK_NAME}'
          AND (SELECT bool_and(pg_get_constraintdef(c.oid) LIKE '%' || quote_literal(k) || '%')
               FROM unnest(ARRAY[{_KIND_SQL_LIST}]) AS k)
    ) THEN
        ALTER TABLE repair_proposals DROP CONSTRAINT IF EXISTS {KIND_CHECK_NAME};
        ALTER TABLE repair_proposals ADD CONSTRAINT {KIND_CHECK_NAME}
            CHECK (kind IN ({_KIND_SQL_LIST}));
    END IF;
END $$
"""

_ALL_DDL = (_REPAIR_PROPOSALS_DDL, _REPAIR_PROPOSALS_FINDING_SUBJECTS_COLUMN,
            _REPAIR_PROPOSALS_KIND_CHECK, _REPAIR_PROPOSALS_PENDING_KEY_INDEX,
            _REPAIR_PROPOSALS_STATUS_INDEX)


def ensure_repair_schema(conn) -> None:
    """Idempotent DDL for `repair_proposals` — safe on every startup and from two processes at
    once (the shared `startup_ddl_lock`)."""
    with startup_ddl_lock(conn) as cur:
        for statement in _ALL_DDL:
            cur.execute(statement)


# ── the op record: the stored shape of ONE edit, and what identifies a proposal ───────────────
# Pure string and dict work, with no import of its own beyond hashlib, because BOTH ends of the
# loop need it: the proposer (which loads a model stack) and `remote.apply_via_clone` (which the
# review lane calls inside the MCP server process, and which must not drag one in). Living here
# rather than in `proposer.py` is what keeps the server's import graph honest.
#
# The stored key is `op`, not `kind`, so a persisted proposal is never mistaken for a librarian
# outcome's `edits` entry by anything reading JSON — `declared_edits` is the ONE translation
# between the two vocabularies, and both validations run on its output, so propose time and apply
# time cannot come to judge different things.
OP_KIND_KEY = "op"
# The stored op shape, PER KIND — every reader of an op (the CLI preview, the console's cleaner,
# the review lane's `ops_preview`) is shaped by which of these it is looking at, so the two are
# named rather than left implicit in four separate reshapes. `entity-body` carries PROSE where the
# additive kinds carry a link and a note, and a reader that assumed one shape for both showed a
# steward an empty cell where the draft should have been.
EDIT_OP_FIELDS = (OP_KIND_KEY, "path", "link", "note")
ENTITY_BODY_OP_FIELDS = (OP_KIND_KEY, "path", "body_markdown", "role")


def declared_edits(ops) -> list[dict]:
    """Stored ops -> the `edits.validate`/`edits.apply_declared` declaration shape."""
    return [{"kind": str(o.get(OP_KIND_KEY, "")), "path": str(o.get("path", "")),
             "link": str(o.get("link", "")), "note": str(o.get("note", ""))}
            for o in (ops or ())]


def target_paths(ops) -> list[str]:
    """The pages a proposal would touch, deduplicated and sorted — the second stored fact
    `remote.apply_via_clone` cross-checks the produced diff against."""
    return sorted({str(o.get("path", "")) for o in (ops or ()) if str(o.get("path", ""))})


def content_key(ops, *, kind: str = KIND_EDITS) -> str:
    """What identifies a proposal: WHAT IT WOULD DO, never which finding suggested it.

    Order-independent (the op lines are sorted) so two runs that derive the same repair in a
    different order collide as they should, and `note` is deliberately EXCLUDED: two proposals
    that add the same callout to the same page with differently-worded sentences are the same
    question asked twice, and a steward who declined it once should not meet a rephrasing of it
    tomorrow.

    The same exclusion does the same work for the other kind, and it matters more there: an
    `entity-body` op has no `link`, so its key is `kind + path` and the DRAFTED BODY is not part of
    it. **A re-drafted body is the same question** — a steward who read a draft for a page and
    decided it needs writing by a person is not asked again tomorrow with the prose rearranged.
    """
    body = "|".join(sorted(f"{o.get(OP_KIND_KEY, '')}:{o.get('path', '')}:{o.get('link', '')}"
                           for o in (ops or ())))
    return hashlib.sha256(f"{kind}|{body}".encode()).hexdigest()
