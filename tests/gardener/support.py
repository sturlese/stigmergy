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
from stigmergy.gardener.schema import JOB_NAME, ensure_gardener_schema
from stigmergy.index import build as index_build
from stigmergy.index import store as index_store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server import review
from tests import testdb

STEWARD = "steward@example.com"


def connect_or_skip():
    conn = testdb.connect_or_skip("gardener")
    capture_schema.ensure_capture_schema(conn)   # capture_queue, job_runs — the filing-window
                                                 # checks + every run's own job_runs row
    review.ensure_repair_schema(conn)
    ensure_gardener_schema(conn)                 # gardener_findings
    return conn


def clean(conn) -> None:
    """Empty every table this suite's own writes could have touched — test isolation only, the
    same posture sibling suites take. `pages_index` is
    NOT listed: `index.build.rebuild` drops and recreates it on every call, so a test that needs
    it calls `rebuild_index` fresh rather than relying on this to have cleared a previous one."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM gardener_findings")
        cur.execute("DELETE FROM capture_queue")
        cur.execute("DELETE FROM job_runs")
        cur.execute("DELETE FROM repairs")


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


# ── the two entity bodies the empty-body pass exists to tell apart ─────────────────────────────
# NEITHER carries an angle-marked placeholder line, so `entity-placeholder-body` is blind to both
# and no other deterministic check reads a body at all. That is the gap `model-empty-entity-body`
# was added for, and these two fixtures are what "it works" and "it is not noise" mean.
EMPTY_ENTITY_BODY_TEXT = "Cofers is a company we work with."


def empty_entity_body(title: str = "Cofers") -> str:
    """A body somebody typed in thirty seconds: one sentence that would read identically with any
    other company's name in it. The reported case."""
    return f"# {title}\n\n{EMPTY_ENTITY_BODY_TEXT}\n"


def written_entity_body(title: str = "Cofers") -> str:
    """The BENIGN TWIN, and the one that matters: five specific facts, each wikilinked to the page
    that states it, plus a connections section. A check that flags this has bounced a steward's
    real work, which is how a corpus-health check turns into noise nobody reads."""
    return f"""# {title}

## What / Who

{title} is the payments processor behind the checkout flow, onboarded in March 2026 after the
[[Payments Vendor Review]] compared three candidates.

## Facts

- The contract floor is $15k/month, agreed in [[{title} Contract Terms]].
- Settlement runs T+2 for EU cards and T+3 elsewhere ([[{title} Settlement Windows]]).
- Their integration lead is Petra Halden, named in [[{title} Kickoff Notes]].
- The 2026 renewal date is 2027-03-01 ([[{title} Contract Terms]]).
- Two incidents in Q2 2026 were both DNS, not payments ([[{title} Incident Log]]).

## Connections

Replaced the previous processor described in [[Payments Vendor Review]], and feeds the reconciliation
job documented in [[Finance Reconciliation]].
"""


def write_registry(root: str, entities: dict) -> None:
    """`entities`: `{id: {"name": ..., "type": ..., "aliases": [...]}}` — `ops/entity-registry.json`,
    `kernel.registry.load_registry`'s own contract."""
    path = os.path.join(root, "ops", "entity-registry.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"entities": entities}, f)


def write_view(root: str, entity_id: str, *, member_hash: str, backlink_hash: str) -> str:
    """A minimal `views/<id>.md` carrying only the frontmatter fields `list_stale_entities` reads
    — real view files carry far more, but the staleness check touches these two.

    BOTH are required, and that is the point of the signature: since #85 staleness is a PAIR
    (member set, rendered backlinks), and a fixture that wrote only half of it would describe a
    view no generator produces — one that reads as permanently stale, which is what a benign twin
    must never be built on."""
    return write_page(root, "views", f"{entity_id}.md",
                      frontmatter={"type": "view", "member_hash": member_hash,
                                   "backlink_hash": backlink_hash},
                      body=f"# {entity_id}\n")


def rebuild_index(conn, repo: str):
    """`pages_index`, for real, from `repo`'s own files — `index.build.rebuild` drops and
    recreates the table, so this IS the isolation between tests that need it. Exercise the real
    builder over a real repo, never hand-crafted rows a parsing bug could silently disagree
    with.

    The access-file snapshots the rebuild reconciles from this repo are cleared on the way out:
    in a database every suite shares they would silently switch a later suite's file-road
    identity or channel resolution onto this repo's copies (arrange, never inherit)."""
    stats = index_build.rebuild(conn, repo, build_embedder("fake"))
    for relpath in (index_store.IDENTITIES_RELPATH, index_store.SLACK_CHANNELS_RELPATH):
        index_store.clear_ops_file(conn, relpath)
    return stats


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
