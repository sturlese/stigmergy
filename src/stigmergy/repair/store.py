"""`repair_proposals` persistence. Pure — composes no prose, decides nothing, authorizes nothing.

Every write is a state TRANSITION guarded in SQL rather than read-then-write in Python: two
stewards clicking Approve at the same moment must not both get a proposal to apply, and a
`WHERE status = ...` in the UPDATE is what makes the loser see zero rows and be told so. The
callers turn that into `ProposalStateError`; this module only reports the row count.
"""
from psycopg.types.json import Jsonb

from stigmergy.repair.schema import (
    DECIDABLE,
    KIND_EDITS,
    STATUS_APPLIED,
    STATUS_APPROVED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUSES,
)

# Every column a reader needs, in one place: three queries return the same shape, and a row that
# means one thing in the CLI and another in the review lane is exactly the drift this avoids.
_COLUMNS = ("id, created_at, run_id, finding_ids, kind, target_paths, ops, rationale, "
            "content_key, status, decided_by, decided_at, notes, applied_commit, error, "
            "model_id, finding_subjects")


def _row(r) -> dict:
    return {"id": r[0], "created_at": r[1], "run_id": r[2], "finding_ids": list(r[3] or []),
            "kind": r[4], "target_paths": list(r[5] or []), "ops": list(r[6] or []),
            "rationale": r[7], "content_key": r[8], "status": r[9], "decided_by": r[10],
            "decided_at": r[11], "notes": r[12], "applied_commit": r[13], "error": r[14],
            "model_id": r[15], "finding_subjects": [list(g or []) for g in (r[16] or [])]}


_INSERT_PROPOSAL = """
INSERT INTO repair_proposals
    (run_id, finding_ids, kind, target_paths, ops, rationale, content_key, model_id,
     finding_subjects, status, decided_by, decided_at)
VALUES (%(run_id)s, %(finding_ids)s, %(kind)s, %(target_paths)s, %(ops)s, %(rationale)s,
        %(content_key)s, %(model_id)s, %(finding_subjects)s, %(status)s, %(decided_by)s,
        CASE WHEN %(decided_by)s = '' THEN NULL ELSE now() END)
RETURNING id
"""


def insert_proposal(conn, *, run_id: int, finding_ids, target_paths, ops, rationale: str,
                    content_key: str, kind: str = KIND_EDITS, model_id: str = "",
                    finding_subjects=(), status: str = STATUS_PENDING,
                    decided_by: str = "") -> int:
    """One proposal. Returns its id.

    `status`/`decided_by` are how the ACT road records a deletion a person made at an
    authenticated door (ADR 043 D2): the row is born `approved` in their name and is applied in
    the same call, so it is never listed as pending and no second person is asked a question the
    first already answered. Every other road takes the defaults and waits in the inbox — which is
    the whole distinction: a proposal a MODEL initiated overnight has nobody behind it yet.

    `target_paths` is stored SEPARATELY from `ops` even though it is derivable from them, and that
    redundancy is the point: `remote.apply_via_clone` cross-checks the diff it produced against
    this column, so an `ops` blob that disagrees with it cannot reach `main`. That is the whole of
    the property — two stored facts kept consistent, not a defense against a writer who can edit
    both.

    `finding_subjects` is what the findings NAMED, one sorted list per finding answered — the other
    half of the dismissal memory, and not derivable from anything else here (`schema.py`).
    """
    with conn.cursor() as cur:
        cur.execute(_INSERT_PROPOSAL, {
            "run_id": int(run_id or 0),
            "finding_ids": Jsonb([int(i) for i in (finding_ids or ())]),
            "kind": kind,
            "target_paths": Jsonb([str(p) for p in (target_paths or ())]),
            "ops": Jsonb([dict(o) for o in (ops or ())]),
            "rationale": rationale or "", "content_key": content_key, "model_id": model_id or "",
            "finding_subjects": Jsonb([[str(p) for p in group]
                                       for group in (finding_subjects or ()) if group]),
            "status": status, "decided_by": decided_by or "",
        })
        return cur.fetchone()[0]


_PENDING_PROPOSALS = f"""
SELECT {_COLUMNS} FROM repair_proposals WHERE status = '{STATUS_PENDING}' ORDER BY id
"""


def pending_proposals(conn, limit: int | None = None) -> list[dict]:
    """Every proposal waiting on a steward, oldest first — or the first `limit` of them.

    The bound is the CALLER's to apply, because they do not share one: the propose pass needs the
    whole set to skip against, and a request-scoped reader needs its own ceiling. Oldest first
    either way, so a bounded read is the front of the queue and not an arbitrary slice.
    """
    with conn.cursor() as cur:
        if limit is None:
            cur.execute(_PENDING_PROPOSALS)
        else:
            cur.execute(_PENDING_PROPOSALS + " LIMIT %s", (max(int(limit), 0),))
        return [_row(r) for r in cur.fetchall()]


def counts_by_status(conn) -> dict[str, int]:
    """How many proposals sit in each status, over the WHOLE table — every declared status
    present, zero included. The one aggregate a surface may draw a part-to-whole from: counting a
    bounded page of rows instead silently understates history the moment the page fills."""
    with conn.cursor() as cur:
        cur.execute("SELECT status, count(*) FROM repair_proposals GROUP BY status")
        counted = dict(cur.fetchall())
    return {status: int(counted.get(status, 0)) for status in STATUSES}


_RECENT_DECIDED = f"""
SELECT {_COLUMNS} FROM repair_proposals WHERE status <> '{STATUS_PENDING}'
ORDER BY decided_at DESC NULLS LAST, id DESC LIMIT %s
"""


def recent_decided(conn, limit: int = 20) -> list[dict]:
    """The last `limit` proposals that are no longer pending — approved, rejected, applied or
    failed. A rejected row IS the dismissal memory, so this is where a steward sees that a
    proposal was reviewed and declined rather than never made."""
    with conn.cursor() as cur:
        cur.execute(_RECENT_DECIDED, (max(int(limit), 1),))
        return [_row(r) for r in cur.fetchall()]


_PROPOSAL = f"SELECT {_COLUMNS} FROM repair_proposals WHERE id = %s"


def proposal(conn, proposal_id: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(_PROPOSAL, (proposal_id,))
        row = cur.fetchone()
    return _row(row) if row else None


_MARK_DECIDED = f"""
UPDATE repair_proposals
SET status = %(status)s, decided_by = %(decided_by)s, decided_at = now(), notes = %(notes)s
WHERE id = %(id)s AND status = '{STATUS_PENDING}'
"""


def mark_decided(conn, proposal_id: int, *, status: str, decided_by: str,
                 notes: str = "") -> bool:
    """Record a steward's verdict. `False` when the row was not pending any more — the race a
    second Approve loses, reported rather than papered over.

    `status` is checked against `DECIDABLE` rather than left to the column's CHECK: `applied` and
    `failed` are legal VALUES and illegal VERDICTS, so the constraint would happily let a caller
    stamp a proposal as applied without a commit ever having been made. The two vocabularies are
    not the same vocabulary, and this is the only place that difference can be enforced.
    """
    if status not in DECIDABLE:
        raise ValueError(
            f"{status!r} is not a verdict a steward can return ({', '.join(DECIDABLE)}) — "
            f"`applied` and `failed` are outcomes code records after an apply, never decisions")
    with conn.cursor() as cur:
        cur.execute(_MARK_DECIDED, {"id": proposal_id, "status": status,
                                    "decided_by": decided_by or "", "notes": notes or ""})
        return cur.rowcount == 1


_MARK_APPLIED = f"""
UPDATE repair_proposals SET status = '{STATUS_APPLIED}', applied_commit = %(commit)s, error = ''
WHERE id = %(id)s AND status = '{STATUS_APPROVED}'
"""


def mark_applied(conn, proposal_id: int, commit: str) -> bool:
    """The commit landed. Only an APPROVED row may become applied — nothing reaches this state
    without having passed through a human."""
    with conn.cursor() as cur:
        cur.execute(_MARK_APPLIED, {"id": proposal_id, "commit": commit or ""})
        return cur.rowcount == 1


_MARK_FAILED = f"""
UPDATE repair_proposals SET status = '{STATUS_FAILED}', error = %(error)s WHERE id = %(id)s
"""


def mark_failed(conn, proposal_id: int, error: str) -> bool:
    """The apply refused or faulted. Deliberately not guarded on the previous status: this is the
    honest record of "we tried and it did not land", and a row that has already moved on is not a
    reason to lose it. The approved status is NOT restored — a failed apply stays operator-visible
    until somebody proposes again."""
    with conn.cursor() as cur:
        cur.execute(_MARK_FAILED, {"id": proposal_id, "error": error or ""})
        return cur.rowcount == 1


_KNOWN_CONTENT_KEYS = f"""
SELECT DISTINCT content_key FROM repair_proposals WHERE status <> '{STATUS_FAILED}'
"""


def known_content_keys(conn) -> set[str]:
    """Every content key this table holds that is NOT a failed apply — the dismissal memory the
    proposer skips against. Rejected keys are in here on purpose (`schema.py`'s docstring): a
    steward who declined a repair once is not asked again by the next night's run.

    `failed` is excluded, and that exclusion is the whole difference between a memory and a
    graveyard. `rejected` is a human saying no; `pending`/`approved` are in flight; a FAILED row is
    a human having said YES to something that then hit a gate, a race or a fault. Remembering it
    as a dismissal would make the one repair a steward actively wanted the one repair the loop can
    never offer again. The row itself stays — it is the operator-visible record of the failure.
    """
    with conn.cursor() as cur:
        cur.execute(_KNOWN_CONTENT_KEYS)
        return {r[0] for r in cur.fetchall()}
