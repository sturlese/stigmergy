"""Non-fixture test support for the digest suite. Reuses `tests.gardener.support` for the shared
connection/schema bootstrap and the corpus/queue fixtures — the SAME tables this package reads —
rather than re-deriving any of it; adds only what is specific to the digest: the Slack channels
file, review_decisions (governed-birth) rows, and gardener `job_runs`+`gardener_findings` fixtures
shaped for the digest's OWN corpus-health read.

Deliberately a plain module, not a `conftest.py` — the same reasoning `tests/gardener/support.py`
gives for itself: fixtures are per-package pytest wiring, this is plain code any file can import.
"""
from stigmergy.capture import ops as capture_ops
from stigmergy.gardener.schema import JOB_NAME as GARDENER_JOB_NAME
from stigmergy.server import review
from tests.gardener import support as gardener_support

STEWARD = gardener_support.STEWARD

connect_or_skip = gardener_support.connect_or_skip
clean = gardener_support.clean
write_page = gardener_support.write_page
rebuild_index = gardener_support.rebuild_index
seed_filed_capture = gardener_support.seed_filed_capture
unique_claim = gardener_support.unique_claim
# These three live in `tests.gardener.support` — the SLA notice's own channel-scoping tests need
# them too, one package over — and are re-exported here unchanged for this package's call sites.
write_labelled_page = gardener_support.write_labelled_page
unlabelled_page = gardener_support.unlabelled_page
write_channels_file = gardener_support.write_channels_file


# ── review_decisions: entities born — the governed-birth log ────────────────────────────────────
def seed_entity_approval(conn, *, created_days_ago: int = 0, item_id: str = "1",
                         verdict: str = review.APPROVE) -> int:
    """One `review_decisions` row shaped exactly like `server.review._decide_entity_proposal`'s own
    single INSERT — `item_kind=KIND_ENTITY_PROPOSAL`, `item_id` the capture_queue id (as text,
    never the entity id — `stigmergy.digest.sections`'s own module docstring explains why this
    governed-birth log is a COUNT source, never a names source)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO review_decisions (item_kind, item_id, verdict, actor, notes, extra) "
            "VALUES (%s, %s, %s, %s, '', NULL) RETURNING id",
            (review.KIND_ENTITY_PROPOSAL, item_id, verdict, STEWARD))
        decision_id = cur.fetchone()[0]
    if created_days_ago:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE review_decisions SET created_at = now() - make_interval(days => %s) "
                "WHERE id = %s", (created_days_ago, decision_id))
    return decision_id


# ── gardener job_runs + gardener_findings: the corpus-health source ─────────────────────────────
def seed_gardener_run(conn, *, findings: list[dict] | None = None,
                      finished_days_ago: int = 0, status: str = "ok",
                      extra_stats: dict | None = None) -> int:
    """A completed `job='gardener'` run plus its findings, independent of whatever a live
    `gardener.run.run_gardener` call in the SAME test would itself produce — the digest's own
    corpus-health read (`gardener.store.latest_completed_run`/`findings_for_run`) only cares that
    the ROW shape is real, not that a live sweep produced it. These tests are about the digest's
    OWN read/render/window logic, not about re-proving what the gardener's own suite covers, so
    the check slugs here are arbitrary: nothing on this path validates a vocabulary.

    `extra_stats` is merged on top of the base `{"findings_total": ...}` dict — e.g.
    `{"sweep": {"error": "SweepGarbage"}}`, the one shape `sections.gather_corpus_health`'s own
    `sweep_incomplete` flag reads. `None` leaves the stats without it."""
    run_id = capture_ops.record_job_run(
        conn, GARDENER_JOB_NAME, status=status,
        stats={"findings_total": len(findings or []), **(extra_stats or {})})
    if finished_days_ago:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE job_runs SET started_at = now() - make_interval(days => %s), "
                "finished_at = now() - make_interval(days => %s) WHERE id = %s",
                (finished_days_ago, finished_days_ago, run_id))
    if findings:
        with conn.cursor() as cur:
            for f in findings:
                cur.execute(
                    "INSERT INTO gardener_findings (run_id, check_slug, severity, source, "
                    "subject, detail, suggested_action, model_id) "
                    "VALUES (%(run_id)s, %(check)s, %(severity)s, %(source)s, %(subject)s, "
                    "%(detail)s, %(suggested_action)s, %(model_id)s)",
                    {"run_id": run_id, "check": f["check"], "severity": f["severity"],
                     "source": f.get("source", "deterministic"), "subject": f.get("subject", ""),
                     "detail": f.get("detail", ""),
                     "suggested_action": f.get("suggested_action", ""),
                     "model_id": f.get("model_id", "")})
    return run_id
