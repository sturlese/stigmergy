"""Non-fixture test support for the repair suite: the Postgres seam with every schema this
package reads or writes, a real knowledge repo (bare remote + clone) with the proposer's own skill
in it, and the finding-seeding helpers every `test_*_pg.py` file needs.

**Real git, real Postgres, real gates, real gitleaks** — the double stands in for the model and
for nothing else. A faked diff would prove nothing about `gate_body_rewrite`, a faked gitleaks
nothing about the secrets veto, and both are exactly the properties this package exists to keep.

A plain module rather than a `conftest.py`, the same reasoning `tests/librarian/support.py` gives
for itself: fixtures are per-package pytest wiring, this is plain code any file can import.
"""
import os

from stigmergy.capture import ops as capture_ops
from stigmergy.capture import schema as capture_schema
from stigmergy.gardener import checks as gardener_checks
from stigmergy.gardener import schema as gardener_schema
from stigmergy.gardener import store as gardener_store
from stigmergy.gardener import sweep as gardener_sweep
from stigmergy.repair import proposer
from stigmergy.repair.schema import ensure_repair_schema
from tests import testdb
from tests.librarian import support as librarian_support

# Pages the fixture knowledge repo (`tests/librarian/fixtures/repo/`) already carries. Named here
# so a test reads as the scenario it is rather than as a path soup, and so a fixture rename breaks
# in one place.
NOTE_A = "wiki/notes/Existing Note.md"
NOTE_B = "wiki/notes/Café Zürich Renewal.md"
DECISION = "wiki/decisions/a-decision-from-a-previous-meeting.md"

STEWARD = "steward@example.com"

# The repair-proposer skill, as a FIXTURE. The real one is versioned in the knowledge repo and
# read at run time (`proposer.SKILL_RELPATH`), which is the whole point of the design — so the
# suite carries its own, deliberately short, and never a frozen copy of the real one: what the
# code owes is the READ path and the refusal when it is absent, and pinning the brain repo's prose
# here would be pinning somebody else's editorial decisions.
FIXTURE_SKILL = """---
name: repair-proposer
description: fixture stand-in for the knowledge repo's own procedure
---

# repair-proposer (test fixture)

Propose the smallest additive repair for a finding, or nothing at all. Read the pages before you
propose anything about them. A finding is a hint, not a verdict.
"""


def connect_or_skip():
    conn = testdb.connect_or_skip("repair")
    capture_schema.ensure_capture_schema(conn)      # capture_queue, job_runs
    gardener_schema.ensure_gardener_schema(conn)    # gardener_findings — the proposer's input
    ensure_repair_schema(conn)                      # repair_proposals — this package's own table
    return conn


def clean(conn) -> None:
    """Empty every table this suite's own writes could have touched — test isolation only, the
    same posture every sibling suite takes."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM repair_proposals")
        cur.execute("DELETE FROM gardener_findings")
        cur.execute("DELETE FROM capture_queue")
        cur.execute("DELETE FROM job_runs")


def build_repo(tmp_path, *, with_skill: bool = True):
    """A bare remote plus a clone of the fixture knowledge repo, with the proposer's skill
    committed into it. `with_skill=False` is the fixture for the named config refusal."""
    env = librarian_support.build_repo(str(tmp_path / "git"))
    if with_skill:
        write_skill(env.repo)
        librarian_support.commit_and_push(env.repo, "test: add the repair-proposer skill")
    return env


def write_skill(repo: str, text: str = FIXTURE_SKILL) -> str:
    path = proposer.skill_path(repo)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


# ── the proposer's input: a completed gardener run and its findings ───────────────────────────
def seed_gardener_run(conn, *, status: str = "ok") -> int:
    """A `job_runs` row for `job='gardener'` — what `store.latest_completed_run` reads. Seeded
    directly rather than by running a whole gardener pass, exactly as `tests.gardener.support`
    seeds its own."""
    return capture_ops.record_job_run(conn, gardener_schema.JOB_NAME, status=status, stats={})


def seed_finding(conn, run_id: int, *, check: str, subjects: list[str], detail: str = "",
                 severity: str = gardener_schema.SEVERITY_WARN) -> int:
    """One `gardener_findings` row, through the REAL assembler and the REAL writer — so a test
    exercises the same `subjects` round trip the gardener itself produces, never a hand-crafted
    row a marshalling bug could silently disagree with. Returns the finding's id."""
    finding = gardener_checks.build_finding(
        check=check, severity=severity, subject=", ".join(subjects), subjects=subjects,
        detail=detail or f"fixture finding about {', '.join(subjects)}",
        suggested_action="no command — this is a fixture",
        source=gardener_schema.SOURCE_MODEL, model_id="fixture-model")
    before = {row["id"] for row in gardener_store.findings_for_run(conn, run_id)}
    gardener_store.insert_findings(conn, run_id, [finding])
    after = gardener_store.findings_for_run(conn, run_id)
    return next(row["id"] for row in after if row["id"] not in before)


def seed_unlinked_mention(conn, run_id: int, pages=(NOTE_A, NOTE_B)) -> int:
    """The finding the offline double answers with ONE backlink op."""
    return seed_finding(conn, run_id, check=gardener_sweep.CHECK_MODEL_UNLINKED_MENTION,
                        subjects=list(pages))


def seed_contradiction(conn, run_id: int, pages=(NOTE_A, DECISION)) -> int:
    """The finding the offline double answers with the callout PAIR, one op per side."""
    return seed_finding(conn, run_id, check=gardener_sweep.CHECK_MODEL_CONTRADICTION,
                        subjects=list(pages))


def stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".md")


def page_text(repo: str, path: str) -> str:
    with open(os.path.join(repo, path), encoding="utf-8") as f:
        return f.read()
