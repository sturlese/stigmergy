"""`repairs` persistence. Pure — composes no prose, decides nothing, authorizes nothing.

ONE write, and it is a fact about something that already happened: `record_applied`, after a
removal's commit landed. There is no state machine to guard — a row is written once and never
transitions.

The reads are what the rest of the table is for. A deployed database still holds the elective
repair loop's rows under three kinds this version can no longer write (`schema.RETIRED_KINDS`) and
two statuses it no longer records, so every reader here returns whatever a row carries and lets the
surface say what it is. Narrowing a query to `kind = 'delete'` would hide the history the table was
kept for.
"""
from psycopg.types.json import Jsonb

from stigmergy.repair.schema import KIND_DELETE, STATUS_APPLIED, STATUSES

# Every column a reader needs, in one place: three queries return the same shape, and a row that
# means one thing on the console and another in a job report is exactly the drift this avoids.
_COLUMNS = ("id, created_at, run_id, finding_ids, kind, target_paths, ops, rationale, "
            "content_key, status, applied_commit, diff, reason, error, model_id, "
            "finding_subjects")


def _row(r) -> dict:
    return {"id": r[0], "created_at": r[1], "run_id": r[2], "finding_ids": list(r[3] or []),
            "kind": r[4], "target_paths": list(r[5] or []), "ops": list(r[6] or []),
            "rationale": r[7], "content_key": r[8], "status": r[9], "applied_commit": r[10],
            "diff": r[11], "reason": r[12], "error": r[13], "model_id": r[14],
            "finding_subjects": [list(g or []) for g in (r[15] or [])]}


_INSERT = """
INSERT INTO repairs (kind, target_paths, ops, rationale, model_id, status, applied_commit, diff)
VALUES (%(kind)s, %(target_paths)s, %(ops)s, %(rationale)s, %(model_id)s, %(status)s,
        %(commit)s, %(diff)s)
RETURNING id
"""


def record_applied(conn, *, target_paths, ops, rationale: str, commit: str, diff: str,
                   kind: str = KIND_DELETE, model_id: str = "") -> int:
    """A removal that landed. Returns its id.

    `diff` is stored because nobody read the change before it was pushed: it is the reading, and a
    console that only listed paths would be showing a summary of prose a model wrote. The capture
    that asked for the removal carries the same reading per page — and is purged with the retention
    window, which is why this row exists beside it.

    `rationale` is the reason the person gave, already scanned for secrets by the flow that read it.

    **No content key.** The column and its unique index belong to the elective loop, whose whole
    problem was not deriving the same repair twice; a removal is decided by a person every time and
    is remembered by nothing, so it is written with none and two removals of the same pages cannot
    collide on an index.
    """
    with conn.cursor() as cur:
        cur.execute(_INSERT, {
            "kind": kind,
            "target_paths": Jsonb([str(p) for p in (target_paths or ())]),
            "ops": Jsonb([dict(o) for o in (ops or ())]),
            "rationale": rationale or "", "model_id": model_id or "",
            "status": STATUS_APPLIED, "commit": commit or "", "diff": diff or "",
        })
        return cur.fetchone()[0]


def counts_by_status(conn) -> dict[str, int]:
    """How many rows sit in each status, over the WHOLE table — every declared status present,
    zero included. The one aggregate a surface may draw a part-to-whole from: counting a bounded
    page of rows instead silently understates history the moment the page fills."""
    with conn.cursor() as cur:
        cur.execute("SELECT status, count(*) FROM repairs GROUP BY status")
        counted = dict(cur.fetchall())
    return {status: int(counted.get(status, 0)) for status in STATUSES}


_RECENT = f"SELECT {_COLUMNS} FROM repairs ORDER BY id DESC LIMIT %s"
_RECENT_OF_STATUS = f"SELECT {_COLUMNS} FROM repairs WHERE status = %s ORDER BY id DESC LIMIT %s"


def recent(conn, limit: int = 20, *, status: str | None = None) -> list[dict]:
    """The last `limit` rows, newest first — every outcome, or one of them.

    Newest first and never oldest: this table only grows, and what a reader wants from it is what
    left the corpus most recently. The whole-table counts above are where a part-to-whole comes
    from.
    """
    with conn.cursor() as cur:
        if status is None:
            cur.execute(_RECENT, (max(int(limit), 1),))
        else:
            cur.execute(_RECENT_OF_STATUS, (status, max(int(limit), 1)))
        return [_row(r) for r in cur.fetchall()]


_ONE = f"SELECT {_COLUMNS} FROM repairs WHERE id = %s"


def repair(conn, repair_id: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(_ONE, (repair_id,))
        row = cur.fetchone()
    return _row(row) if row else None
