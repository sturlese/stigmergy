"""`gardener_findings`: insert one run's findings, and read them back. Pure persistence — never
composes report text (`report.py`) and never decides what a check found (`checks.py`); this
module's whole job is turning a finding dict into a row and back.

**Read-back after insert, deliberately, not a re-use of the in-memory list `checks.py` built.**
`run.py` re-fetches via `findings_for_run` once its `job_runs` row (and this run's findings) are
committed, so `--json` and the printed report render exactly what is now durably true — the same
"the preview and the action share one predicate" discipline `capture.retention.purge`'s dry-run
already applies, one level over: what a reader sees is never allowed to drift from what is stored.
"""
from stigmergy.gardener.schema import JOB_NAME, SOURCE_DETERMINISTIC

_INSERT_FINDING = """
INSERT INTO gardener_findings
    (run_id, check_slug, severity, source, subject, detail, suggested_action, model_id)
VALUES (%(run_id)s, %(check)s, %(severity)s, %(source)s, %(subject)s, %(detail)s,
        %(suggested_action)s, %(model_id)s)
"""


def insert_findings(conn, run_id: int, findings: list[dict]) -> None:
    """Persist one run's findings. Exactly seven keys are read off each finding dict — `check`,
    `severity`, `source`, `subject`, `detail`, `suggested_action`, `model_id` — plus the `run_id`
    argument; any `_notice_*` key carrying the SLA notice's own wording is deliberately NOT among
    them, so those keys never survive a round trip through the table. `model_id` defaults to `''`
    for a deterministic finding — the same "empty is a real, honest state" posture `source` has."""
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
    """Every finding this run persisted, in insertion order — `report.py` applies its own
    severity/slug/subject sort at RENDER time, never baked in here, so `--json`'s row order and
    the printed report's grouping can each choose their own from the same read.

    **`check_slug AS check` is deliberately not done in SQL.** `CHECK` is a reserved SQL keyword
    (Postgres and the SQL standard) — usable as a column ALIAS only quoted (`AS "check"`), and
    quoting-dependent correctness is exactly the kind of thing a typo turns into a bug. Renamed
    here, in Python, once.

    **`model_id` is always present in the returned dict, `''` for a deterministic finding** — the
    same "an absent field would read as something false" reasoning `report.render_json`'s own
    `suggested_action` already applies, one column over: `report._source_tag` reads
    `finding.get("model_id", "?")`, and a KeyError there would be a worse failure mode than a
    harmless empty string nothing ever prints (deterministic findings never reach that branch at
    all — `_source_tag` only reads `model_id` when `source == SOURCE_MODEL`)."""
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
    """The most recent completed run for `job` (default `'gardener'`), or `None` when none has
    ever completed — read via the existing `job_runs (job, started_at DESC)` index, the SAME
    lookup `gardener.sweep.previous_run_watermark` makes for its own, narrower purpose (that one
    also reads the sweep's private sample-rotation offset out of `stats`; this is the general read
    `stigmergy.digest`'s corpus-health section needs: the latest gardener run's findings summarized
    by check + severity, naming the run date).

    **`status IN ('ok', 'partial')`, not `= 'ok'` alone.** A `'partial'` run
    (`gardener.run.run_gardener`'s own status when the model sweep failed but the deterministic
    checks still committed) is exactly as trustworthy a source of FINDINGS as an `'ok'` one — the
    findings themselves were computed and persisted before the sweep pass ever ran (module
    docstring). Excluding `'partial'` here would make a sweep outage ALSO blank out the digest's
    corpus-health section for that run — a second, independent honesty failure on top of the one
    the `'partial'` status exists to prevent at the watermark. Deliberately NOT the same predicate
    `gardener.sweep.previous_run_watermark` uses (that one stays `'ok'`-only on purpose — see
    `capture.ops`'s module docstring for why the two readers of this same column disagree).

    `stigmergy.digest` is why this is public rather than another module's private helper: the
    layering grants `digest` an edge into the findings store — this module — precisely so a run's
    findings are read back through the ONE place that already knows how (`findings_for_run`,
    immediately above), never a second, independently-written `job_runs` query in that package."""
    with conn.cursor() as cur:
        cur.execute(_LATEST_COMPLETED_RUN, (job,))
        row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "started_at": row[1], "finished_at": row[2], "stats": row[3] or {}}
