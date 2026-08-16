"""The withheld-material rule (`_MATERIAL_WITHHELD`) used to be applied to `query_submissions`'s
listing query and not to `get_submission_trace` — so a capture's own reply, withheld from
`stigmergy-queue list`/`brain_submissions`, still came back through `stigmergy-queue
show`/`stigmergy-entities show` (both built on `get_submission_trace`) and through
`queue.filed_latencies_ms`-adjacent code that reads the same trace. Both paths now read ONE shared
SQL expression (`queue.py`'s own docstring: "the rule itself is one expression, used by both
paths, so they cannot drift again").

**The highest-value shape here is a parametrized test that walks BOTH read paths over the SAME
row** — that is what would have caught the asymmetry when it was introduced: a test that only ever
calls one of the two functions cannot see the other one disagreeing with it.
"""
import pytest
from psycopg.types.json import Jsonb

from stigmergy.capture import dispositions, queue, schema
from stigmergy.capture.evidence import MemoryEvidenceStore

ALICE = "alice@example.com"
STEWARD = "steward"

REPLY = "the secret rotation key is sk-live-abcdef1234567890"


def _submit_and_ask(conn) -> dict:
    evidence = MemoryEvidenceStore()
    ack = queue.submit(conn, evidence, kind="raw", material="A memo about Acme Corp.\n",
                      hints=None, submitted_by=ALICE)
    claimed = queue.claim_next(conn)
    queue.finish(conn, ack["id"], status=schema.NEEDS_INPUT, expected_attempts=claimed["attempts"],
                error="which entity is this about?")
    return ack


def _reply_directly(conn, submission_id: int, answer: str) -> None:
    """Set the row's `reply` the way `server.service.BrainService._reply` ultimately does — a
    plain UPDATE, since this suite is about the QUEUE read paths and does not need the full
    identity/audit machinery `BrainService` wraps around the same write."""
    with conn.cursor() as cur:
        cur.execute("UPDATE capture_queue SET reply = %s, status = %s WHERE id = %s",
                   (answer, schema.QUEUED, submission_id))


def _park_as_secret(conn, submission_id: int) -> None:
    """The shape `report.rejected_secret` writes — reused here as the minimal fields
    `_MATERIAL_WITHHELD` actually reads (`status`, `report->>'reason_code'`), matching a row that
    was ASKED, ANSWERED, and only then refused because the gates found a secret in the drafted
    page — the exact path the asymmetry was reachable through."""
    claimed = queue.claim_next(conn)
    assert claimed["id"] == submission_id
    report = {schema.REASON_CODE_KEY: schema.REASON_SECRET}
    queue.finish(conn, submission_id, status=schema.REJECTED,
                expected_attempts=claimed["attempts"], error="a seeded secret was matched",
                report=report)


@pytest.mark.parametrize("read_path", ["query_submissions", "get_submission_trace"])
def test_a_withheld_rows_reply_never_crosses_either_read_path(clean_queue, read_path):
    """The attack this closes: a submitter answers a question with material that later gets the
    row refused for a secret — the reply itself is exactly the kind of thing that must not travel
    once the row is withheld, and BOTH read paths must agree it does not."""
    ack = _submit_and_ask(clean_queue)
    _reply_directly(clean_queue, ack["id"], REPLY)
    _park_as_secret(clean_queue, ack["id"])

    if read_path == "query_submissions":
        [row] = queue.query_submissions(clean_queue, submitter=ALICE, statuses=[schema.REJECTED])
    else:
        row = queue.get_submission_trace(clean_queue, ack["id"])

    assert row["withheld_reason"] == schema.WITHHELD_MATERIAL_NOTE
    assert row["reply"] == ""
    assert REPLY not in str(row)     # belt and braces: the secret is nowhere in the shaped row


@pytest.mark.parametrize("read_path", ["query_submissions", "get_submission_trace"])
def test_the_benign_twin_an_ordinary_parked_rows_reply_survives_on_both_paths(clean_queue, read_path):
    """The benign twin: the rule must not have started withholding replies that were never
    refused at all — a steward reading a merely-parked row still needs to see the answer."""
    ack = _submit_and_ask(clean_queue)
    _reply_directly(clean_queue, ack["id"], "it's about Acme Corp")
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.TRIAGE,
                expected_attempts=claimed["attempts"], error="still cannot resolve it")

    if read_path == "query_submissions":
        [row] = queue.query_submissions(clean_queue, submitter=ALICE, statuses=[schema.TRIAGE])
    else:
        row = queue.get_submission_trace(clean_queue, ack["id"])

    assert row["withheld_reason"] == ""
    assert row["reply"] == "it's about Acme Corp"


def test_the_two_read_paths_agree_on_the_same_row_not_only_on_the_same_shape(clean_queue):
    """The asymmetry's own shape, pinned directly: query BOTH paths for the identical row and
    require every shared field — above all `reply` and `withheld_reason` — to say the same thing.
    A future regression that fixes one path and not the other fails HERE even if each path's own
    isolated test (above) happens to still pass for unrelated reasons."""
    ack = _submit_and_ask(clean_queue)
    _reply_directly(clean_queue, ack["id"], REPLY)
    _park_as_secret(clean_queue, ack["id"])

    [listed] = queue.query_submissions(clean_queue, submitter=ALICE, statuses=[schema.REJECTED])
    trace = queue.get_submission_trace(clean_queue, ack["id"])

    for field in ("reply", "withheld_reason", "status", "id"):
        assert listed[field] == trace[field], (
            f"{field!r} disagrees between query_submissions ({listed[field]!r}) and "
            f"get_submission_trace ({trace[field]!r}) for the SAME row")


def test_a_stewards_rejection_reply_is_not_withheld_on_either_path(clean_queue):
    """The other side of the asymmetry, using the REAL disposition path (`capture.dispositions`)
    rather than a hand-built report: a steward's own `reject` carries `reason_code=steward`, which
    is NOT in `WITHHELD_REASONS` — a reply must stay visible after a human's judgment call, on
    both read paths, exactly as it does after a merely-parked row."""
    ack = _submit_and_ask(clean_queue)
    _reply_directly(clean_queue, ack["id"], "it's about Acme Corp")
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.TRIAGE,
                expected_attempts=claimed["attempts"], error="still triage")
    dispositions.reject(clean_queue, ack["id"], actor=STEWARD, reason="not brain material")

    [listed] = queue.query_submissions(clean_queue, submitter=ALICE, statuses=[schema.REJECTED])
    trace = queue.get_submission_trace(clean_queue, ack["id"])
    assert listed["withheld_reason"] == trace["withheld_reason"] == ""
    assert listed["reply"] == trace["reply"] == "it's about Acme Corp"




@pytest.mark.parametrize("read_path", ["query_submissions", "get_submission_trace"])
def test_a_parked_row_whose_report_is_a_json_scalar_is_still_served(clean_queue, read_path):
    """OLD BEHAVIOUR: `AttributeError: 'str' object has no attribute 'get'` — BOTH read paths
    500ed on the row instead of serving it.

    `report` is JSONB, and JSONB holds scalars as happily as objects. `schema._reason_flagged`
    guarded only the falsy case (`(report or {}).get(...)`), so a truthy scalar sailed past it and
    blew up in the one function both paths route their sentence through. A single row written in
    an unexpected shape therefore took out `stigmergy-queue show`, `brain_submissions` and the
    admin console's list — the SQL mirror (`report ->> 'reason_code'`) had no such problem, so the
    two halves of one rule disagreed exactly where a row was hardest to look at.

    Walking both paths over the SAME row is this file's own shape: a fix to one and not the other
    fails here.
    """
    ack = _submit_and_ask(clean_queue)
    with clean_queue.cursor() as cur:
        cur.execute("UPDATE capture_queue SET status = %s, report = %s WHERE id = %s",
                    (schema.TRIAGE, Jsonb("a report stored as a bare string"), ack["id"]))

    if read_path == "query_submissions":
        [row] = queue.query_submissions(clean_queue, submitter=ALICE, statuses=[schema.TRIAGE])
    else:
        row = queue.get_submission_trace(clean_queue, ack["id"])

    assert row["id"] == ack["id"]
    assert row["status"] == schema.TRIAGE
    assert row["withheld_reason"] == ""      # a scalar report names no reason code


# ── the full state matrix ───────────────────────────────────────────────────────────────────────
# Withhold in `queued` and `claimed`; show in `needs_input`, `triage`, `filed`, `rejected`,
# `resolved`. `failed` stays withheld too, as an accepted residual (a run that failed before the
# gate leaves unscanned material). `query_submissions` carries `excerpt`; `get_submission_trace`
# does not (it has no excerpt field at all — only `reply`), so each path is asserted on the fields
# it actually has, and `withheld_reason`/`status` (common to both) are asserted on every case.
def test_a_queued_row_withholds_with_the_pending_note_not_the_secret_one(clean_queue):
    """The central rule: a `queued` row must NOT reuse `WITHHELD_MATERIAL_NOTE` — that sentence
    says the capture was refused as a secrets/PII match, which is false and needlessly alarming
    about an ordinary, unscanned, possibly entirely benign capture."""
    evidence = MemoryEvidenceStore()
    ack = queue.submit(clean_queue, evidence, kind="raw", material="an ordinary memo",
                      hints=None, submitted_by=ALICE)

    [listed] = queue.query_submissions(clean_queue, submitter=ALICE, statuses=[schema.QUEUED])
    trace = queue.get_submission_trace(clean_queue, ack["id"])

    assert listed["status"] == trace["status"] == schema.QUEUED
    assert listed["excerpt"] == ""
    assert listed["withheld_reason"] == trace["withheld_reason"] == schema.WITHHELD_PENDING_NOTE
    assert listed["withheld_reason"] != schema.WITHHELD_MATERIAL_NOTE


def test_a_claimed_row_withholds_with_the_pending_note_too(clean_queue):
    evidence = MemoryEvidenceStore()
    ack = queue.submit(clean_queue, evidence, kind="raw", material="an ordinary memo",
                      hints=None, submitted_by=ALICE)
    queue.claim_next(clean_queue)

    [listed] = queue.query_submissions(clean_queue, submitter=ALICE, statuses=[schema.CLAIMED])
    trace = queue.get_submission_trace(clean_queue, ack["id"])

    assert listed["status"] == trace["status"] == schema.CLAIMED
    assert listed["excerpt"] == ""
    assert listed["withheld_reason"] == trace["withheld_reason"] == schema.WITHHELD_PENDING_NOTE


@pytest.mark.parametrize("status,finish_kwargs", [
    (schema.NEEDS_INPUT, {"error": "which entity?"}),
    (schema.TRIAGE, {"error": "triage"}),
    (schema.FILED, {"result_ref": "wiki/x.md"}),
])
def test_gate_has_run_states_show_the_excerpt(clean_queue, status, finish_kwargs):
    """`needs_input`/`triage` are exactly the states a submitter/steward must read the
    material to act on it — the secrets/PII gate has already run by the time a row leaves
    `claimed` into any of these. `filed` is the ordinary happy path, unaffected."""
    evidence = MemoryEvidenceStore()
    ack = queue.submit(clean_queue, evidence, kind="raw", material="an ordinary memo",
                      hints=None, submitted_by=ALICE)
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=status, expected_attempts=claimed["attempts"],
                **finish_kwargs)

    [listed] = queue.query_submissions(clean_queue, submitter=ALICE, statuses=[status])
    trace = queue.get_submission_trace(clean_queue, ack["id"])

    assert listed["status"] == trace["status"] == status
    assert listed["excerpt"] == "an ordinary memo"
    assert listed["withheld_reason"] == trace["withheld_reason"] == ""


def test_a_resolved_row_shows_the_excerpt_a_human_already_looked(clean_queue):
    """A `resolved` row was looked at by a HUMAN directly — a different, equally valid route
    to "somebody has looked at what is sitting there"."""
    ack = _submit_and_ask(clean_queue)     # parks as needs_input
    dispositions.resolve(clean_queue, ack["id"], actor=STEWARD, note="folded in by hand")

    [listed] = queue.query_submissions(clean_queue, submitter=ALICE, statuses=[schema.RESOLVED])
    trace = queue.get_submission_trace(clean_queue, ack["id"])

    assert listed["status"] == trace["status"] == schema.RESOLVED
    assert listed["withheld_reason"] == trace["withheld_reason"] == ""


def test_a_failed_row_stays_withheld_with_its_own_distinct_sentence(clean_queue):
    """The accepted residual: a run that failed before reaching the gate leaves genuinely
    unscanned material behind, and — unlike `queued`/`claimed` — there is no automatic next pass
    that will look at it. Withheld, with a sentence that does not falsely promise it will
    reappear "as soon as the librarian has looked at it"."""
    evidence = MemoryEvidenceStore()
    ack = queue.submit(clean_queue, evidence, kind="raw", material="an ordinary memo",
                      hints=None, submitted_by=ALICE)
    claimed = queue.claim_next(clean_queue)
    queue.finish(clean_queue, ack["id"], status=schema.FAILED, expected_attempts=claimed["attempts"],
                error="the worker crashed mid-item")

    [listed] = queue.query_submissions(clean_queue, submitter=ALICE, statuses=[schema.FAILED])
    trace = queue.get_submission_trace(clean_queue, ack["id"])

    assert listed["status"] == trace["status"] == schema.FAILED
    assert listed["excerpt"] == ""
    assert listed["withheld_reason"] == trace["withheld_reason"] == schema.WITHHELD_UNSCANNED_NOTE
    assert listed["withheld_reason"] not in (schema.WITHHELD_PENDING_NOTE, schema.WITHHELD_MATERIAL_NOTE)


# ── the drift test for the one predicate written twice ──────────────────────────────────────────
# `schema._reason_flagged` (Python, evaluated from an already-fetched `report` dict) and
# `queue._REASON_FLAGGED_SQL` (evaluated IN POSTGRES, before a withheld value ever crosses the
# wire) implement the SAME predicate twice, by design (`queue.py`'s own comment above
# `_REASON_FLAGGED_SQL`: "the Python mirror of exactly this expression"). Two implementations of
# one predicate is exactly the shape that drifts silently — this is the test that would catch it,
# across the full status x reason_code matrix, not just the handful of cases the other tests in
# this file happen to construct rows for.
def _sql_reason_flagged(conn, status: str, report: dict | None) -> bool:
    """Evaluate `queue._REASON_FLAGGED_SQL` directly against literal `(status, report)` values —
    no `capture_queue` row needed, since the expression only ever reads those two columns."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT ({queue._REASON_FLAGGED_SQL}) FROM "
            "(SELECT %(status)s::text AS status, %(report)s::jsonb AS report) AS t",
            {"status": status, "report": None if report is None else Jsonb(report)})
        return cur.fetchone()[0]


_REASON_CODE_MATRIX = (
    None,                                    # no report at all (row predates `report`)
    {},                                       # report present, no `reason_code` key
    {schema.REASON_CODE_KEY: None},          # explicit null reason_code
    {schema.REASON_CODE_KEY: schema.REASON_SECRET},
    {schema.REASON_CODE_KEY: schema.REASON_PII},
    {schema.REASON_CODE_KEY: schema.REASON_DUPLICATE},   # a REJECTION_REASONS member, not withheld
    {schema.REASON_CODE_KEY: "some-unknown-future-code"},
)


def test_reason_flagged_python_and_sql_agree_across_the_full_status_x_reason_code_matrix(clean_queue):
    for status in schema.STATUSES:
        for report in _REASON_CODE_MATRIX:
            python_result = schema._reason_flagged(status, report)
            sql_result = _sql_reason_flagged(clean_queue, status, report)
            assert python_result == sql_result, (
                f"schema._reason_flagged and queue._REASON_FLAGGED_SQL disagree for "
                f"status={status!r} report={report!r}: python={python_result!r} sql={sql_result!r}")
