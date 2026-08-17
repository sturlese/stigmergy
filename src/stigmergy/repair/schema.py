"""The proposal store's contract: the DDL and the vocabularies.

Idempotent DDL behind `capture.schema.startup_ddl_lock`, the shared advisory lock — the same
posture `gardener/schema.py` takes, and for the same reason: two processes may start at once.

The one design decision worth stating here rather than in prose elsewhere: **a REJECTED row is
the dismissal memory.** `content_key` identifies a proposal by what it would DO, not by which
finding suggested it, and the proposer skips a key that has ANY prior row — pending, rejected or
applied. "Reviewed and declined" therefore finally exists as a durable fact, and a steward who
says no once is not asked the same question again the next night. The UNIQUE index is narrower on
purpose (pending only): re-proposing after a rejection is a decision a human makes, and the index
must not turn it into a database error.
"""
import hashlib

from stigmergy.capture.schema import startup_ddl_lock

JOB_NAME = "repair-propose"

# ── kind — what a proposal WOULD DO. v1 has exactly one; PR-6/7 extend the tuple ──────────────
KIND_EDITS = "edits"
KINDS = (KIND_EDITS,)

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
    model_id TEXT NOT NULL DEFAULT ''
)
"""

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

_ALL_DDL = (_REPAIR_PROPOSALS_DDL, _REPAIR_PROPOSALS_PENDING_KEY_INDEX,
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
OP_FIELDS = (OP_KIND_KEY, "path", "link", "note")


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
    """
    body = "|".join(sorted(f"{o.get(OP_KIND_KEY, '')}:{o.get('path', '')}:{o.get('link', '')}"
                           for o in (ops or ())))
    return hashlib.sha256(f"{kind}|{body}".encode()).hexdigest()
