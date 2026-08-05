"""Non-fixture test support for the gardener suite: the shared Postgres connection seam
(`tests/capture/conftest.py`'s own posture, several packages over), a throwaway `--repo` tree
(registry + views, plain files — the gardener never writes, so it needs none of
`tests/views/conftest.py`'s real-git-remote machinery), and the raw-SQL seeding helpers every
`tests/gardener/*_pg.py` file needs and none of them should reinvent.

Deliberately a plain module, not a `conftest.py` — the same reasoning `tests/librarian/support.py`
gives for itself: fixtures are per-package pytest wiring, this is plain code any file can import.
"""
import json
import os
import uuid

import yaml

from stigmergy.capture import ops as capture_ops
from stigmergy.capture import queue as capture_queue
from stigmergy.capture import schema as capture_schema
from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.gardener import schema as gardener_schema
from stigmergy.gardener.schema import JOB_NAME, ensure_gardener_schema
from stigmergy.index import build as index_build
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server import review
from tests import testdb

STEWARD = "steward@example.com"


def connect_or_skip():
    conn = testdb.connect_or_skip("gardener")
    capture_schema.ensure_capture_schema(conn)   # capture_queue, job_runs — the filing-window
                                                 # checks + every run's own job_runs row
    review.ensure_review_schema(conn)              # review_decisions
    ensure_gardener_schema(conn)                 # gardener_findings
    return conn


def clean(conn) -> None:
    """Empty every table this suite's own writes could have touched — test isolation only, the
    same posture sibling suites take. `pages_index` is
    NOT listed: `index.build.rebuild` drops and recreates it on every call, so a test that needs
    it calls `rebuild_index` fresh rather than relying on this to have cleared a previous one."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM gardener_findings")
        cur.execute("DELETE FROM review_decisions")
        cur.execute("DELETE FROM capture_queue")
        cur.execute("DELETE FROM job_runs")


def unique_material(label: str = "gardener") -> str:
    return f"{label} {uuid.uuid4()}\n"


def unique_claim(label: str = "candidate") -> str:
    return f"{label} claim {uuid.uuid4()}"


# ── job_runs fixtures: the sweep's own watermark, `sweep.previous_run_watermark` ────────────────
def seed_gardener_job_run(conn, *, stats: dict | None = None, status: str = "ok",
                          started_days_ago: int = 0) -> int:
    """A `job_runs` row for `job='gardener'`, backdated via SQL like `seed_filed_capture` — the
    real row shape `sweep.previous_run_watermark` reads (`started_at`, `status`, `stats`), without
    running an entire gardener pass just to get one into existence."""
    run_id = capture_ops.record_job_run(conn, JOB_NAME, status=status, stats=stats or {})
    if started_days_ago:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE job_runs SET started_at = now() - make_interval(days => %s) WHERE id = %s",
                (started_days_ago, run_id))
    return run_id


# ── the --repo tree: registry + pages, no git ────────────────────────────────────────────────
def write_page(root: str, zone: str, relpath: str, *, frontmatter: dict, body: str = "") -> str:
    """One page file under `root/<zone>/<relpath>`, real YAML frontmatter (`yaml.safe_dump`
    handles every value shape correctly — lists, bare strings, empty lists — rather than a
    hand-built line-by-line writer that would only be as correct as its own test coverage).
    Returns the zone-relative path (`index.corpus.PageRow.path`'s own convention: `zone/rest`)."""
    full = os.path.join(root, zone, relpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    fm_text = yaml.safe_dump(frontmatter, default_flow_style=False, sort_keys=False).strip()
    with open(full, "w", encoding="utf-8") as f:
        f.write(f"---\n{fm_text}\n---\n\n{body}")
    return f"{zone}/{relpath}".replace(os.sep, "/")


def write_registry(root: str, entities: dict) -> None:
    """`entities`: `{id: {"name": ..., "type": ..., "aliases": [...]}}` — `ops/entity-registry.json`,
    `kernel.registry.load_registry`'s own contract."""
    path = os.path.join(root, "ops", "entity-registry.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"entities": entities}, f)


def write_view(root: str, entity_id: str, *, member_hash: str) -> str:
    """A minimal `views/<id>.md` carrying only the one frontmatter field `list_stale_entities`
    reads (`member_hash`) — real view files carry far more, but the staleness check touches
    only this one."""
    return write_page(root, "views", f"{entity_id}.md",
                      frontmatter={"type": "view", "member_hash": member_hash},
                      body=f"# {entity_id}\n")


# ── pages_index rows carrying an ACL label. These live here, not in `tests.digest.support`,
# because the SLA notice's own channel-scoping tests need them too, one package over;
# `tests.digest.support` re-exports both names unchanged for its own call sites ─────────────────
def write_labelled_page(root: str, relpath: str, *, title: str, acl: list) -> str:
    """A `zone='wiki'` page with a real `acl:` frontmatter label. Written through the SAME
    real-file + `rebuild_index` path every other gardener/digest fixture page uses — never a
    hand-crafted row a parsing bug could silently disagree with."""
    return write_page(root, "wiki", relpath,
                      frontmatter={"type": "note", "title": title, "entity": [],
                                  "status": "developing",
                                  "updated": "2026-07-01", "acl": acl})


def unlabelled_page(root: str, relpath: str, *, title: str) -> str:
    return write_page(root, "wiki", relpath,
                      frontmatter={"type": "note", "title": title, "entity": [],
                                  "status": "developing", "updated": "2026-07-01"})


# ── ops/slack-channels.json — here rather than in `tests.digest.support` for the identical
# reason as the two functions immediately above ─────────────────────────────────────────────────
def write_channels_file(root: str, mapping: dict) -> str:
    """`ops/slack-channels.json` — `{channel_id: [audience labels]}`, `slack.channels.
    channel_audiences`'s own contract."""
    path = os.path.join(root, "ops", "slack-channels.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mapping, f)
    return path


def write_malformed_channels_file(root: str) -> str:
    """`ops/slack-channels.json` that fails to parse at all — `slack.channels.channel_audiences`'s
    own `IdentityError` case: the fixture the gardener's own tests need to prove a run survives
    this file when it has nothing to post, and fails cleanly (report intact, `notice_error` set)
    when it does."""
    path = os.path.join(root, "ops", "slack-channels.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("not json at all")
    return path


def rebuild_index(conn, repo: str):
    """`pages_index`, for real, from `repo`'s own files — `index.build.rebuild` drops and
    recreates the table, so this IS the isolation between tests that need it. Exercise the real
    builder over a real repo, never hand-crafted rows a parsing bug could silently disagree
    with."""
    return index_build.rebuild(conn, repo, build_embedder("fake"))


# ── capture_queue fixtures: "the last N filings", the population the windowed checks share, and
# the same rows `stigmergy.digest`'s "pages filed"/"corrections filed" read, `report` included ─────
def seed_filed_capture(conn, *, result_ref: str, finished_days_ago: int = 0,
                       submitted_by: str = STEWARD, report: dict | None = None) -> int:
    """A `capture_queue` row already `status='filed'`, backdated via SQL — the clock-injection
    pattern `tests/capture/test_retention_pg.py::_terminal_row` already establishes one package
    over (a real `now() - make_interval(...)`, never a wall-clock sleep).

    `report` is optional (`None` leaves the row's `report` unset): a caller building a
    meeting-style fixture passes `{"filed_meeting": {...}}`, the exact shape
    `librarian.report.filed_meeting` produces (`stigmergy.digest.sections._filed_page_paths`'s own
    documented contract) — never fabricated ad hoc at more than this one seeding point."""
    ack = capture_queue.submit(conn, MemoryEvidenceStore(), kind="raw",
                               material=unique_material(), hints={"title": "t"},
                               submitted_by=submitted_by)
    claimed = capture_queue.claim_next(conn)
    capture_queue.finish(conn, ack["id"], status=capture_schema.FILED,
                        expected_attempts=claimed["attempts"], result_ref=result_ref,
                        report=report)
    if finished_days_ago:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE capture_queue SET finished_at = now() - make_interval(days => %s) "
                "WHERE id = %s", (finished_days_ago, ack["id"]))
    return ack["id"]


# ── an `sla`-severity finding, injected ───────────────────────────────────────────────────────
def force_one_sla_finding(monkeypatch, *, subject="wiki/notes/x.md"):
    """Make the next `run_gardener` pass produce exactly one `sla` finding, by appending it to
    `check_orphans`' own return.

    **Stated plainly, because a check that stops running must be impossible to miss: NO live
    check produces an `sla` finding.** Both arms of the contradiction SLA — the only producers
    there have ever been — went with the canon lane. The NOTICE mechanism is severity-driven and
    generic, so it survives and is still worth testing; what these tests cannot do is reach it
    through a real check. They inject one instead, and say so, rather than quietly asserting
    nothing. The first check that genuinely needs to escalate wakes this path.
    """
    from stigmergy.gardener import checks as checks_module
    real = checks_module.check_orphans

    def _with_sla(conn):
        return [*real(conn), checks_module.build_finding(
            check="injected-sla", severity=gardener_schema.SEVERITY_SLA, subject=subject,
            detail=f"an injected escalation about {subject} — see support.force_one_sla_finding",
            suggested_action="nothing; this finding exists to exercise the notice path")]

    monkeypatch.setattr(checks_module, "check_orphans", _with_sla)
