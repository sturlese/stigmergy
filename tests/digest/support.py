"""Non-fixture test support for the digest suite. Reuses `tests.gardener.support` for the shared
connection/schema bootstrap and the corpus/queue fixtures — the SAME tables this package reads —
rather than re-deriving any of it; adds only what is specific to the digest: the Slack channels
file, the filed reports that name what a capture introduced, and gardener
`job_runs`+`gardener_findings` fixtures
shaped for the digest's OWN corpus-health read.

Deliberately a plain module, not a `conftest.py` — the same reasoning `tests/gardener/support.py`
gives for itself: fixtures are per-package pytest wiring, this is plain code any file can import.
"""
from stigmergy.capture import ops as capture_ops
from stigmergy.gardener.schema import JOB_NAME as GARDENER_JOB_NAME
from tests.gardener import support as gardener_support

STEWARD = gardener_support.STEWARD

connect_or_skip = gardener_support.connect_or_skip
clean = gardener_support.clean
write_page = gardener_support.write_page
rebuild_index = gardener_support.rebuild_index
seed_filed_capture = gardener_support.seed_filed_capture
unique_claim = gardener_support.unique_claim


# ── pages_index rows with and without an ACL label — the pair the broadcast-scoping tests need.
# Both go through the SAME real-file + `rebuild_index` path every other fixture page uses, never a
# hand-crafted row a parsing bug could silently disagree with ───────────────────────────────────
def write_labelled_page(root: str, relpath: str, *, title: str, acl: list) -> str:
    return write_page(root, "wiki", relpath,
                      frontmatter={"type": "note", "title": title, "entity": [],
                                  "status": "developing",
                                  "updated": "2026-07-01", "acl": acl})


def unlabelled_page(root: str, relpath: str, *, title: str) -> str:
    return write_page(root, "wiki", relpath,
                      frontmatter={"type": "note", "title": title, "entity": [],
                                  "status": "developing", "updated": "2026-07-01"})


# ── entities born — counted off the filings that introduced them ─────────────────────
def seed_entity_births(conn, *, count: int = 1, finished_days_ago: int = 0,
                       result_ref: str = "wiki/notes/x.md@abc1234") -> int:
    """A FILED capture whose report names the identities that capture introduced — the shape
    `librarian.report.filed` produces (`entities_born`), and the only place a birth is recorded
    since the capture-is-the-approval change: an entity is born when a capture introduces it, so the digest counts the
    filings rather than a second table that could disagree with the commits."""
    born = [{"id": f"e{n}", "name": f"Entity {n}", "type": "organization",
             "confirmed_by": STEWARD} for n in range(count)]
    return seed_filed_capture(conn, result_ref=result_ref, finished_days_ago=finished_days_ago,
                              report={"entities_born": born})


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


# ── repairs applied — the third corpus delta ────────────────────────────────────────
def seed_applied_repair(conn, *, kind: str = "edits", created_days_ago: int = 1,
                        content_key: str = "") -> int:
    """One `applied` row in the repair ledger, aged like every other fixture here.

    Written through `repair.store` rather than by hand: the digest counts a column that store owns,
    and a hand-built INSERT would keep passing after the column moved. `content_key` is unique per
    row by default because the ledger's index says it must be.

    Backdated by a day by DEFAULT, and that is not decoration: every window in this suite is
    captured at import time, so a row stamped `now()` lands after the upper bound and reads as a
    row outside the window — which is indistinguishable from a broken query."""
    import uuid

    from stigmergy.repair import store as repair_store

    repair_id = repair_store.record_applied(
        conn, run_id=0, finding_ids=[], target_paths=["wiki/notes/x.md"],
        ops=[{"op": "backlink", "path": "wiki/notes/x.md", "link": "Y", "note": ""}],
        rationale="a fixture repair", content_key=content_key or f"key-{uuid.uuid4().hex}",
        commit="cafebabe", diff="--- a\n+++ b\n", kind=kind)
    if created_days_ago:
        with conn.cursor() as cur:
            cur.execute("UPDATE repairs SET created_at = now() - make_interval(days => %s) "
                        "WHERE id = %s", (created_days_ago, repair_id))
    return repair_id
