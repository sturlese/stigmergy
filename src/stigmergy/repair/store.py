"""`repairs` persistence. Pure — composes no prose, decides nothing, authorizes nothing.

Three writes, one per outcome, and every one of them is a fact about something that already
happened: `record_applied` after a commit landed, `record_failed` after a refusal, `record_skipped`
for a repair that was never derived. There is no state machine left to guard — a row is written
once and never transitions — so the concurrency question this module used to answer (two people
approving at the same moment) is gone with the approving.

What survives from it is the one guarded write that matters: `content_key` is UNIQUE over every row
that has one, so two workers deriving the same repair in the same minute cannot both apply it. The
loser sees the conflict and is told, rather than pushing a second commit for the same edit.
"""
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

from stigmergy.repair.schema import (
    KIND_EDITS,
    STATUS_APPLIED,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUSES,
    page_set_key,
)

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
INSERT INTO repairs
    (run_id, finding_ids, kind, target_paths, ops, rationale, content_key, model_id,
     finding_subjects, status, applied_commit, diff, reason, error)
VALUES (%(run_id)s, %(finding_ids)s, %(kind)s, %(target_paths)s, %(ops)s, %(rationale)s,
        %(content_key)s, %(model_id)s, %(finding_subjects)s, %(status)s, %(commit)s, %(diff)s,
        %(reason)s, %(error)s)
RETURNING id
"""


class ContentKeyTaken(RuntimeError):
    """This exact repair is already in the ledger. Raised rather than returned, because every
    caller's next move is the same: abandon this one and go on to the next finding."""


def _insert(conn, *, status: str, run_id: int, finding_ids, target_paths, ops, rationale: str,
            content_key: str, kind: str, model_id: str, finding_subjects, commit: str, diff: str,
            reason: str, error: str) -> int:
    with conn.cursor() as cur:
        try:
            cur.execute(_INSERT, {
                "run_id": int(run_id or 0),
                "finding_ids": Jsonb([int(i) for i in (finding_ids or ())]),
                "kind": kind,
                "target_paths": Jsonb([str(p) for p in (target_paths or ())]),
                "ops": Jsonb([dict(o) for o in (ops or ())]),
                "rationale": rationale or "", "content_key": content_key or "",
                "model_id": model_id or "",
                "finding_subjects": Jsonb([[str(p) for p in group]
                                           for group in (finding_subjects or ()) if group]),
                "status": status, "commit": commit or "", "diff": diff or "",
                "reason": reason or "", "error": error or "",
            })
            return cur.fetchone()[0]
        except pg_errors.UniqueViolation as ex:
            raise ContentKeyTaken(content_key) from ex


def record_applied(conn, *, run_id: int, finding_ids, target_paths, ops, rationale: str,
                   content_key: str, commit: str, diff: str, kind: str = KIND_EDITS,
                   model_id: str = "", finding_subjects=()) -> int:
    """A repair that landed. Returns its id.

    `diff` is stored because nobody read the change before it was pushed: it is the reading, and a
    console that only listed paths would be showing a summary of prose a model wrote (ADR 043 D5,
    generalised by ADR 044 to every repair).

    `target_paths` is stored SEPARATELY from `ops` even though it is derivable from them, and that
    redundancy is the point: the apply cross-checks the diff it produced against this column, so an
    `ops` blob that disagrees with it cannot reach `main`. Two stored facts kept consistent — never
    a defense against a writer who can edit both.

    `finding_subjects` is what the findings NAMED, one sorted list per finding answered — the other
    half of the memory, and not derivable from anything else here (`schema.py`).
    """
    return _insert(conn, status=STATUS_APPLIED, run_id=run_id, finding_ids=finding_ids,
                   target_paths=target_paths, ops=ops, rationale=rationale,
                   content_key=content_key, kind=kind, model_id=model_id,
                   finding_subjects=finding_subjects, commit=commit, diff=diff, reason="",
                   error="")


def record_failed(conn, *, run_id: int, finding_ids, target_paths, ops, rationale: str,
                  content_key: str, error: str, kind: str = KIND_EDITS, model_id: str = "",
                  finding_subjects=()) -> int:
    """A repair that was derived and then refused — by its own validator, by a gate, or by a fault.

    The row is the operator-facing record, and its key is normally remembered like any other: the
    loop does not retry a repair a gate already refused. That is why `error` has to say WHICH gate,
    in words an operator can act on — it is the whole of what anyone will ever know about why this
    finding stopped being answered.

    **An empty `content_key` is the exception, and it is deliberate**: a refusal that is about the
    TREE rather than about the repair (`errors.CorpusMovedError`) is not a verdict on anything, and
    remembering it would retire a finding because two repairs collided. The row still exists; only
    the memory declines to hold it.
    """
    return _insert(conn, status=STATUS_FAILED, run_id=run_id, finding_ids=finding_ids,
                   target_paths=target_paths, ops=ops, rationale=rationale,
                   content_key=content_key, kind=kind, model_id=model_id,
                   finding_subjects=finding_subjects, commit="", diff="", reason="", error=error)


def record_skipped(conn, *, run_id: int, finding_ids, reason: str, kind: str = KIND_EDITS,
                   finding_subjects=(), model_id: str = "") -> int:
    """A finding that produced no repair: no kind could express it, a ceiling bound, the model
    declined. Carries NO content key — there is nothing to key on — so it is remembered by nothing
    and the next run is free to try again once the corpus has moved.
    """
    return _insert(conn, status=STATUS_SKIPPED, run_id=run_id, finding_ids=finding_ids,
                   target_paths=(), ops=(), rationale="", content_key="", kind=kind,
                   model_id=model_id, finding_subjects=finding_subjects, commit="", diff="",
                   reason=reason, error="")


def counts_by_status(conn) -> dict[str, int]:
    """How many repairs sit in each status, over the WHOLE table — every declared status present,
    zero included. The one aggregate a surface may draw a part-to-whole from: counting a bounded
    page of rows instead silently understates history the moment the page fills."""
    with conn.cursor() as cur:
        cur.execute("SELECT status, count(*) FROM repairs GROUP BY status")
        counted = dict(cur.fetchall())
    return {status: int(counted.get(status, 0)) for status in STATUSES}


_RECENT = f"SELECT {_COLUMNS} FROM repairs ORDER BY id DESC LIMIT %s"
_RECENT_OF_STATUS = f"SELECT {_COLUMNS} FROM repairs WHERE status = %s ORDER BY id DESC LIMIT %s"


def recent(conn, limit: int = 20, *, status: str | None = None) -> list[dict]:
    """The last `limit` repairs, newest first — every outcome, or one of them.

    Newest first and never oldest: this table only grows, and what a reader wants from it is what
    the last pass did. The whole-table counts above are where a part-to-whole comes from.
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


_KNOWN_CONTENT_KEYS = "SELECT DISTINCT content_key FROM repairs WHERE content_key <> ''"


def known_content_keys(conn) -> set[str]:
    """Every content key this table holds — the memory the derivation skips against, and it holds
    for every outcome that produced one.

    `applied` is obvious: doing it twice would be a second commit for an edit already in the repo,
    and it is what makes a repair somebody REVERTED in git stay reverted rather than coming back
    the next night. `failed` is the deliberate one: a repair the gates refused is not retried, so a
    finding whose only expressible answer is refused stops being answered — the `error` column is
    where an operator finds out why, and the finding stays in the gardener's report either way.

    Two rows carry no key and are therefore held by nothing: a `skipped` one, which never became a
    repair, and a `failed` one whose refusal was about the corpus having MOVED under it
    (`errors.CorpusMovedError`) — that one is a race, not an answer, and the next pass derives it
    again.
    """
    with conn.cursor() as cur:
        cur.execute(_KNOWN_CONTENT_KEYS)
        return {r[0] for r in cur.fetchall()}


_ANSWERED_FINDINGS = ("SELECT finding_ids, finding_subjects, target_paths FROM repairs "
                      "WHERE content_key <> ''")


def answered_findings(conn) -> tuple[set[int], set[str]]:
    """`(finding ids, page-set keys)` this ledger has already answered — the memory consulted
    BEFORE a model is asked anything, so a finding that already produced a repair costs nothing to
    skip.

    TWO EXACT RULES, deliberately not one fuzzy one. A finding answered by ID is the same finding
    (a second pass over the same gardener run). A finding naming exactly the pages some row stands
    for is the same repair rediscovered under a new id in a later run. Anything looser — "any page
    in common" — would suppress a legitimate second repair on a page that already has one, and an
    over-eager skip is invisible while a missed one only costs a model call that `content_key`
    then throws away.

    "Stands for" is TWO page sets per row, and it needs both: `finding_subjects` (what each
    answered finding NAMED) and `target_paths` (what the answer EDITED). They are routinely
    different — an `orphan-page` finding names the page nothing links to and the repair edits the
    page that ought to link to it — and matching only the second is why that shape went back to the
    model every night after being answered.

    Rows with no content key are skipped: a `skipped` row means nothing was derived, and the next
    run must be free to try.
    """
    ids: set[int] = set()
    page_sets: set[str] = set()
    with conn.cursor() as cur:
        cur.execute(_ANSWERED_FINDINGS)
        for finding_ids, subjects, target_paths in cur.fetchall():
            ids.update(int(i) for i in (finding_ids or ()))
            page_sets.add(page_set_key(target_paths))
            for group in (subjects or ()):
                page_sets.add(page_set_key(group))
    page_sets.discard(page_set_key(()))
    return ids, page_sets
