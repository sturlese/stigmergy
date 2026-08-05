"""`stigmergy-librarian status`, driven through `cli.main(argv)` against real Postgres.

Three things it reports and one thing it must never do:

- **queue depth**, in `stigmergy-queue`'s byte-identical depth line;
- **the item in flight**, including whether its lease looks stale against the CONFIGURED visibility
  timeout — the difference between an operator seeing a live worker and a dead one;
- **capture->filed p50/p95**, or the "not enough data yet" framing below `latency.MIN_SAMPLES`;
- and it must **write nothing**: an operator reaching for `status` is often doing so because
  something is already wrong, and a status command that claimed, swept or failed a row would be
  changing the thing it was asked to describe.

Needs no MinIO and no gitleaks: `status` builds no `Deps`, runs no `startup_checks` and never
reaches the agent. That is itself part of the contract (see `_cmd_status`) and the first test
asserts it.
"""
import json

from stigmergy.capture import cli as queue_cli
from stigmergy.capture import latency, queue, schema
from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.index import store
from stigmergy.librarian import cli, config
from tests import testdb

SUBMITTER = "status.tester@stigmergy.test"


def _run(capsys, *argv):
    exit_code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return exit_code, out, err


def _argv() -> list[str]:
    return ["--dsn", testdb.dsn()]


def _submit(conn, material: str) -> dict:
    return queue.submit(conn, MemoryEvidenceStore(), kind="raw", material=material, hints=None,
                        submitted_by=SUBMITTER)


def _file_n(conn, n: int) -> None:
    for i in range(n):
        _submit(conn, f"a capture that gets filed, number {i}")
        item = queue.claim_next(conn, visibility_timeout_s=300)
        queue.finish(conn, item["id"], status=schema.FILED, expected_attempts=item["attempts"],
                     result_ref=f"wiki/notes/Filed {i}.md@abc{i}")


def _conn():
    return store.connect(testdb.dsn())


# ── it needs nothing but the database ─────────────────────────────────────────────────────────────
def test_status_runs_against_an_empty_queue_with_no_repo_no_gitleaks_and_no_evidence_store(
        capsys, clean_queue):
    """No `--repo`, nothing stubbed. An operator reaching for `status` because the librarian is
    misconfigured must still get an answer — a status command that refused until the config was
    valid would be useless exactly when it is needed."""
    exit_code, out, err = _run(capsys, *_argv(), "status")

    assert exit_code == 0
    assert err == ""
    assert out.splitlines()[0] == "queue: empty"
    assert "in flight: nothing claimed" in out


def test_status_writes_nothing(capsys, clean_queue):
    """The whole point of a read-only surface, asserted rather than assumed: a queued row is still
    queued and a stranded claim is NOT swept — `status` reports staleness, it does not repair it."""
    queued = _submit(clean_queue, "a row that must stay queued")
    stranded = _submit(clean_queue, "a row that must stay claimed")
    queue.claim_next(clean_queue, visibility_timeout_s=300)          # `queued` — oldest first
    queue.claim_next(clean_queue, visibility_timeout_s=300)          # `stranded`
    with clean_queue.cursor() as cur:
        cur.execute("UPDATE capture_queue SET status = %s, claimed_at = NULL WHERE id = %s",
                    (schema.QUEUED, queued["id"]))
        cur.execute("UPDATE capture_queue SET claimed_at = now() - interval '2 hours'"
                    " WHERE id = %s", (stranded["id"],))
    before = queue.counts_by_status(clean_queue)

    exit_code, out, _ = _run(capsys, *_argv(), "status")

    assert exit_code == 0
    assert "LEASE EXPIRED" in out
    assert queue.counts_by_status(clean_queue) == before
    assert queue.get_submission_trace(clean_queue, stranded["id"])["status"] == schema.CLAIMED
    assert queue.get_submission_trace(clean_queue, stranded["id"])["attempts"] == 1


def test_status_creates_no_schema_and_says_so_when_there_is_none(capsys, monkeypatch):
    """"Writes nothing" includes DDL, and it did not. `_connect` used to call
    `ensure_capture_schema` for every subcommand, so the command documented three times over as
    read-only created tables and indexes and required DDL privileges — and on a read-only role it
    failed with the generic "cannot reach the queue database", the exact misdiagnosis `_cmd_status`
    exists to avoid.

    Driven against a schema-less database rather than by inspecting privileges: an empty schema is
    what a read-only role's connection effectively looks like from here, and the observable property
    is the same — no tables are created and the message names the real cause.
    """
    conn = _conn()
    # DROP then CREATE, not `CREATE IF NOT EXISTS`: this test's whole assertion is a table COUNT, so
    # it has to start from a schema it knows is empty and leave nothing behind for the next run.
    # Anything less makes it order-dependent on itself.
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS status_no_ddl CASCADE")
        cur.execute("CREATE SCHEMA status_no_ddl")
    conn.commit()
    # A search_path with no `public` in it: every capture table is invisible, so `status` is asked to
    # read a database whose schema it has not created.
    monkeypatch.setenv("PGOPTIONS", "-c search_path=status_no_ddl")
    try:
        exit_code, out, err = _run(capsys, *_argv(), "status")
    finally:
        monkeypatch.delenv("PGOPTIONS", raising=False)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = 'status_no_ddl'")
            created = cur.fetchone()[0]
            cur.execute("DROP SCHEMA IF EXISTS status_no_ddl CASCADE")
        conn.commit()
        conn.close()

    assert exit_code == cli.EXIT_CONFIG
    assert "no schema" in err
    assert "cannot reach the queue database" not in err
    assert created == 0, "`status` executed DDL against a database it was asked only to read"


# ── the depth line is `stigmergy-queue`'s, not a second dialect ─────────────────────────────────────
def test_the_depth_line_is_byte_identical_to_stigmergy_queues(capsys, clean_queue):
    _submit(clean_queue, "one queued capture")
    _file_n(clean_queue, 1)

    _, out, _ = _run(capsys, *_argv(), "status")

    expected = queue_cli.depth_line(queue.counts_by_status(clean_queue))
    assert out.splitlines()[0] == expected
    assert f"{schema.QUEUED}=1" in expected and f"{schema.FILED}=1" in expected


# ── the in-flight line, and the stale-lease verdict ───────────────────────────────────────────────
def test_a_fresh_claim_is_reported_as_within_its_lease(capsys, clean_queue):
    ack = _submit(clean_queue, "an item a live worker is working on")
    queue.claim_next(clean_queue, visibility_timeout_s=config.DEFAULT_VISIBILITY_TIMEOUT_S)

    _, out, _ = _run(capsys, *_argv(), "status")

    line = next(line for line in out.splitlines() if line.startswith("in flight:"))
    assert f"#{ack['id']}" in line and SUBMITTER in line and "attempts=1" in line
    assert "within its lease" in out
    assert "LEASE EXPIRED" not in out
    # no recovery command offered for a healthy claim: `--visibility-timeout 0` would pull the row
    # out from under a worker that is mid-item, and both would then file the capture
    assert queue_cli.RECLAIM_NOW not in out


def test_a_stale_claim_is_named_stale_with_the_arithmetic_and_the_recovery_command(capsys,
                                                                                  clean_queue):
    """The verdict AND the two numbers it came from. "looks stale" that cannot be checked is an
    assertion an operator has to trust; with the age and the lease beside it they can see why."""
    ack = _submit(clean_queue, "an item whose worker died")
    queue.claim_next(clean_queue, visibility_timeout_s=config.DEFAULT_VISIBILITY_TIMEOUT_S)
    with clean_queue.cursor() as cur:
        cur.execute("UPDATE capture_queue SET claimed_at = now() - interval '2 hours' WHERE id = %s",
                    (ack["id"],))

    _, out, _ = _run(capsys, *_argv(), "status")

    assert "LEASE EXPIRED" in out
    # the measured age, in `stigmergy-queue`'s duration format ...
    assert "held 72" in out and "of 900s (15 min)" in out
    # ... the configured lease, in the worker's own configured-value format ...
    assert f"{config.DEFAULT_VISIBILITY_TIMEOUT_S}s (15 min)" in out
    # ... and the one command that gets it back now, from the shared constant
    assert queue_cli.RECLAIM_NOW in out


def test_the_verdict_for_a_row_with_every_delivery_burned_matches_what_the_sweep_does(
        capsys, clean_queue):
    """A message containing a command is an executable promise: the printed verdict is checked
    against the sweep's real behaviour rather than against itself.

    `queue.release_expired` SPLITS the expired set — a row at `attempts >= max_attempts` is finished
    as `failed` with an `ingest_errors` row and is NOT returned to the queue. The verdict promised a
    requeue unconditionally, so it was most wrong about the most common genuinely-stuck case (a third
    delivery that died), and the `RECLAIM_NOW` line under it would have failed the row too.
    """
    max_attempts = 3
    ack = _submit(clean_queue, "an item that has burned every delivery")
    queue.claim_next(clean_queue, visibility_timeout_s=300, max_attempts=max_attempts)
    with clean_queue.cursor() as cur:
        cur.execute("UPDATE capture_queue SET attempts = %s, "
                    "claimed_at = now() - interval '2 hours' WHERE id = %s",
                    (max_attempts, ack["id"]))

    _, out, _ = _run(capsys, *_argv(), "status", "--max-attempts", str(max_attempts))

    assert "LEASE EXPIRED and every delivery is burned" in out
    assert f"({max_attempts}/{max_attempts})" in out
    assert "FAILS this row" in out
    assert "returns it to the queue" not in out
    # and no recovery command, because reclaiming this row fails it rather than recovering it
    assert queue_cli.RECLAIM_NOW not in out

    # THE cross-check: run the very sweep the verdict described and confirm it did that.
    swept = queue.release_expired(clean_queue, visibility_timeout_s=300,
                                  max_attempts=max_attempts)
    assert swept["failed"] == 1 and swept["released"] == 0
    assert queue.get_submission_trace(clean_queue, ack["id"])["status"] == schema.FAILED


def test_a_stale_row_with_deliveries_left_still_promises_the_requeue_it_gets(capsys, clean_queue):
    """The benign twin of the case above, cross-checked the same way: below the ceiling the sweep
    really does return the row, so the requeue verdict and the recovery command are both correct and
    must not have been suppressed along with the exhausted case."""
    max_attempts = 3
    ack = _submit(clean_queue, "an item whose worker died with deliveries left")
    queue.claim_next(clean_queue, visibility_timeout_s=300, max_attempts=max_attempts)
    with clean_queue.cursor() as cur:
        cur.execute("UPDATE capture_queue SET claimed_at = now() - interval '2 hours' WHERE id = %s",
                    (ack["id"],))

    _, out, _ = _run(capsys, *_argv(), "status", "--max-attempts", str(max_attempts))

    assert "returns it to the queue with an attempt burned" in out
    assert "every delivery is burned" not in out
    assert queue_cli.RECLAIM_NOW in out

    swept = queue.release_expired(clean_queue, visibility_timeout_s=300,
                                  max_attempts=max_attempts)
    assert swept["released"] == 1 and swept["failed"] == 0
    assert queue.get_submission_trace(clean_queue, ack["id"])["status"] == schema.QUEUED


def test_the_stale_verdict_uses_the_visibility_timeout_on_the_command_line(capsys, clean_queue):
    """The mirror of the defaults rule: the DEFAULT is exercised above, and here an explicit flag
    has to change the verdict — otherwise the number printed and the number compared are not the
    same."""
    ack = _submit(clean_queue, "an item held for ten minutes")
    queue.claim_next(clean_queue, visibility_timeout_s=config.DEFAULT_VISIBILITY_TIMEOUT_S)
    with clean_queue.cursor() as cur:
        cur.execute("UPDATE capture_queue SET claimed_at = now() - interval '10 minutes'"
                    " WHERE id = %s", (ack["id"],))

    _, default_out, _ = _run(capsys, *_argv(), "status")
    _, tight_out, _ = _run(capsys, *_argv(), "status", "--visibility-timeout", "60")

    assert "within its lease" in default_out          # 600s held, 900s lease
    assert "LEASE EXPIRED" in tight_out               # 600s held, 60s lease


# ── the latency measurement, and its refusal to answer early ────────────────────────────────────
def test_below_the_minimum_status_prints_the_not_enough_data_framing(capsys, clean_queue):
    """Three samples cannot produce a p95; printing one to a decimal place would read as a
    measurement. Deliberately three, which is about what a hand-drained walk actually produces."""
    _file_n(clean_queue, 3)

    _, out, _ = _run(capsys, *_argv(), "status")

    assert "not enough data yet" in out
    assert "3 filed captures so far" in out
    assert f"{latency.MIN_SAMPLES} needed" in out
    assert "p50=" not in out


def test_at_the_minimum_status_reports_p50_and_p95(capsys, clean_queue):
    _file_n(clean_queue, latency.MIN_SAMPLES)

    _, out, _ = _run(capsys, *_argv(), "status")

    assert "p50=" in out and "p95=" in out
    assert f"over {latency.MIN_SAMPLES} filed captures" in out
    assert "not enough data" not in out


def test_an_empty_queue_reports_zero_samples_rather_than_omitting_the_line(capsys, clean_queue):
    """Silence is not an outcome — the same rule anchoring is held to: an absent latency line is
    indistinguishable from a latency line that failed to render."""
    _, out, _ = _run(capsys, *_argv(), "status")
    assert "capture->filed latency:" in out
    assert "0 filed captures so far" in out


# ── --json ────────────────────────────────────────────────────────────────────────────────────────
def test_json_output_leads_with_the_machine_readable_object(capsys, clean_queue):
    ack = _submit(clean_queue, "an in-flight item, as JSON")
    queue.claim_next(clean_queue, visibility_timeout_s=config.DEFAULT_VISIBILITY_TIMEOUT_S)
    _file_n(clean_queue, 2)

    exit_code, out, _ = _run(capsys, *_argv(), "--json", "status")

    assert exit_code == 0
    payload, _ = json.JSONDecoder().raw_decode(out)
    assert payload["counts"][schema.CLAIMED] == 1
    assert payload["visibility_timeout_s"] == config.DEFAULT_VISIBILITY_TIMEOUT_S
    assert payload["in_flight"][0]["id"] == ack["id"]
    assert payload["in_flight"][0]["lease_expired"] is False
    assert payload["latency"]["samples"] == 2
    assert payload["latency"]["enough_data"] is False
    assert payload["latency"]["p50_ms"] is None
    # prose stays out of a --json stdout entirely
    assert "in flight:" not in out and "queue:" not in out


def test_json_output_carries_the_percentiles_once_there_are_enough(capsys, clean_queue):
    _file_n(clean_queue, latency.MIN_SAMPLES)

    _, out, _ = _run(capsys, *_argv(), "--json", "status")

    payload, _ = json.JSONDecoder().raw_decode(out)
    assert payload["latency"]["enough_data"] is True
    assert payload["latency"]["p50_ms"] is not None
    assert payload["latency"]["p95_ms"] is not None


# ── the derived visibility timeout, reported through --json (issue #30's executable promise) ────
def test_json_status_reports_the_visibility_timeout_derived_from_the_agent_timeout_env_var(
        capsys, clean_queue, monkeypatch):
    """The other half of issue #30's fix. `stigmergy-queue reclaim`'s no-flag refusal used to point an
    operator at `$STIGMERGY_LIBRARIAN_VISIBILITY_TIMEOUT` — a variable this repo reads nowhere — and
    now names the real source (`$STIGMERGY_LIBRARIAN_TIMEOUT_S`) plus the command that answers it:
    `stigmergy-librarian status --json`'s `visibility_timeout_s` field. A message containing a command
    is an executable promise, so this runs the named command in the environment the message claims
    determines the number.

    `STIGMERGY_LIBRARIAN_TIMEOUT_S=600` is not an arbitrary probe value — it is the deployed worker's
    own budget (`docs/reference/operator-runbook.md`, `fly.toml`), which is exactly the case the
    dead variable was silently wrong for: the class default (900) is not what a staging operator's
    worker actually holds. `tests/librarian/test_config.py::
    test_a_raised_agent_timeout_raises_the_derived_visibility_with_it` already pins the arithmetic
    at `Settings.from_args`; this pins that the SAME resolved number reaches the JSON surface the
    refusal now names — the wiring from env var to `--json` output, not the formula.
    """
    monkeypatch.setenv("STIGMERGY_LIBRARIAN_TIMEOUT_S", "600")

    exit_code, out, err = _run(capsys, *_argv(), "--json", "status")

    assert exit_code == 0
    payload, _ = json.JSONDecoder().raw_decode(out)
    # 2 agent attempts * 600s + 120s gate budget + 180s headroom — staging's derived lease
    # (config.minimum_visibility_timeout_s(600) + config.VISIBILITY_HEADROOM_S), not the 900s
    # class default a bare "$STIGMERGY_LIBRARIAN_VISIBILITY_TIMEOUT, default 900" would have implied.
    assert payload["visibility_timeout_s"] == 1500
    assert payload["visibility_timeout_s"] != config.DEFAULT_VISIBILITY_TIMEOUT_S


# ── the config carry-over, at the surface an operator touches ────────────────────────────────────
def test_an_explicit_visibility_timeout_of_zero_is_refused_out_loud_not_silently_replaced(
        capsys, clean_queue):
    """**The carry-over defect, at the CLI.** `Settings.from_args` resolved flags with `args.x or
    default`, so `--visibility-timeout 0` was falsy, silently became 900, and the run then quoted 900
    back at the operator. `status` is the cheapest surface to prove the fix on: the zero now survives
    resolution and `worker.startup_checks`... does not run here, so what proves it is the value the
    command actually used — the JSON says 0, not 900."""
    exit_code, out, err = _run(capsys, *_argv(), "--json", "status", "--visibility-timeout", "0")

    assert exit_code == 0
    payload, _ = json.JSONDecoder().raw_decode(out)
    assert payload["visibility_timeout_s"] == 0
    assert payload["visibility_timeout_s"] != config.DEFAULT_VISIBILITY_TIMEOUT_S


def test_a_zero_visibility_timeout_makes_every_claim_read_as_stale_which_is_the_honest_answer(
        capsys, clean_queue):
    """The consequence of honoring it, stated: with a zero lease, a claim made this instant is
    already expired. That is what the operator asked for, and seeing it is how they learn the flag
    took effect."""
    _submit(clean_queue, "claimed a moment ago")
    queue.claim_next(clean_queue, visibility_timeout_s=config.DEFAULT_VISIBILITY_TIMEOUT_S)

    _, out, _ = _run(capsys, *_argv(), "status", "--visibility-timeout", "0")

    assert "LEASE EXPIRED" in out
