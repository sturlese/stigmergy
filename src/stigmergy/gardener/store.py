"""`gardener_findings`: insert one run's findings, read them back. Pure persistence — composes no
text, decides nothing. `run.py` re-fetches after commit so the report renders what is durably
true, never the in-memory list.
"""
from stigmergy.gardener.schema import JOB_NAME, SOURCE_DETERMINISTIC

_INSERT_FINDING = """
INSERT INTO gardener_findings
    (run_id, check_slug, severity, source, subject, detail, suggested_action, model_id)
VALUES (%(run_id)s, %(check)s, %(severity)s, %(source)s, %(subject)s, %(detail)s,
        %(suggested_action)s, %(model_id)s)
"""


def insert_findings(conn, run_id: int, findings: list[dict]) -> None:
    """Persist one run's findings — exactly seven keys per dict; the `_notice_*` keys are
    deliberately not among them, so they never survive a round trip. `model_id` defaults to
    `''`."""
    with conn.cursor() as cur:
        for f in findings:
            cur.execute(_INSERT_FINDING, {
                "run_id": run_id, "check": f["check"], "severity": f["severity"],
                "source": f.get("source", SOURCE_DETERMINISTIC),
                "subject": f.get("subject", ""), "detail": f.get("detail", ""),
                "suggested_action": f.get("suggested_action", ""),
                "model_id": f.get("model_id", ""),
            })


_FINDINGS_FOR_RUN = """
SELECT id, run_id, check_slug, severity, source, subject, detail, suggested_action, created_at,
       model_id
FROM gardener_findings WHERE run_id = %s ORDER BY id
"""


def findings_for_run(conn, run_id: int) -> list[dict]:
    """Every finding this run persisted, in insertion order — `report.py` sorts at render time.
    `check_slug` -> `"check"` is renamed here in Python, not by a quoted SQL alias (`CHECK` is a
    reserved keyword). `model_id` is always present, `''` for a deterministic finding."""
    with conn.cursor() as cur:
        cur.execute(_FINDINGS_FOR_RUN, (run_id,))
        rows = cur.fetchall()
    return [
        {"id": r[0], "run_id": r[1], "check": r[2], "severity": r[3], "source": r[4],
         "subject": r[5], "detail": r[6], "suggested_action": r[7], "created_at": r[8],
         "model_id": r[9]}
        for r in rows
    ]


_LATEST_COMPLETED_RUN = """
SELECT id, started_at, finished_at, stats FROM job_runs
WHERE job = %s AND status IN ('ok', 'partial')
ORDER BY started_at DESC LIMIT 1
"""


def latest_completed_run(conn, *, job: str = JOB_NAME) -> dict | None:
    """The most recent completed run, or `None`. Public for `stigmergy.digest`'s corpus-health
    read.

    `status IN ('ok', 'partial')`, not `'ok'` alone: a `'partial'` run's FINDINGS are exactly as
    trustworthy — they committed before the sweep ever ran. Deliberately wider than
    `sweep.previous_run_watermark`'s `'ok'`-only predicate; the two readers of this column
    disagree on purpose."""
    with conn.cursor() as cur:
        cur.execute(_LATEST_COMPLETED_RUN, (job,))
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "started_at": row[1], "finished_at": row[2], "stats": row[3] or {}}
