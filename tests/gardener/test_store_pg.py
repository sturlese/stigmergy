"""`gardener.store`: `gardener_findings` insert + read-back. Pure persistence — no check logic,
no rendering, synthetic finding dicts over a real table. The slugs below are deliberately
arbitrary: this table stores whatever a check hands it and validates no vocabulary."""
from stigmergy.gardener import schema, store
from tests.gardener import support


def _finding(check="orphan-page", severity=schema.SEVERITY_WARN, subject="wiki/x.md",
            **extra):
    f = {"check": check, "severity": severity, "source": schema.SOURCE_DETERMINISTIC,
        "subject": subject, "detail": "some detail", "suggested_action": "some action"}
    f.update(extra)
    return f


def test_insert_and_read_back_round_trips_every_field(conn):
    store.insert_findings(conn, 128, [_finding(
        check="aging-seed", severity="warn", subject="wiki/x.md",
        detail="seed, updated 2026-03-02, 151 days ago (threshold 90)",
        suggested_action="no command runs itself")])

    rows = store.findings_for_run(conn, 128)

    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == 128
    assert row["check"] == "aging-seed"
    assert row["severity"] == "warn"
    assert row["source"] == "deterministic"
    assert row["subject"] == "wiki/x.md"
    assert row["detail"] == "seed, updated 2026-03-02, 151 days ago (threshold 90)"
    assert row["suggested_action"] == "no command runs itself"
    assert row["model_id"] == ""   # empty, never null, for a deterministic finding
    assert isinstance(row["id"], int)
    assert row["created_at"] is not None


# ── source/model_id: the columns a retired model pass wrote into ────────────────────────────────
def test_a_row_a_retired_model_pass_wrote_still_reads_back_as_what_it_is(conn):
    """Nothing produces `source='model'` any more — the gardener's model passes are gone — but a
    deployed `gardener_findings` holds rows that do, and this store is what an operator, the admin
    console and `--json` read them through. The `source` CHECK constraint must still ACCEPT the
    value, and the row must come back labelled `model` rather than silently relabelled
    `deterministic`. This is the whole reason `schema.SOURCE_MODEL` is still declared."""
    store.insert_findings(conn, 500, [_finding(
        check="model-contradiction", severity="warn", source=schema.SOURCE_MODEL,
        subject="wiki/a.md, wiki/b.md", model_id="gpt-5.4-mini")])

    row = store.findings_for_run(conn, 500)[0]

    assert row["source"] == "model"
    assert row["model_id"] == "gpt-5.4-mini"


def test_a_deterministic_finding_never_needs_model_id_supplied(conn):
    """`model_id` defaults to `''` when the finding dict omits it entirely — every deterministic
    check in this package builds its finding through `checks.build_finding`, which never sets
    this key, and must not be required to."""
    store.insert_findings(conn, 501, [_finding(check="orphan-page")])

    row = store.findings_for_run(conn, 501)[0]

    assert row["model_id"] == ""


def test_insert_many_findings_preserves_each_one(conn):
    findings = [_finding(check=f"check-{i}", subject=f"x{i}.md") for i in range(5)]
    store.insert_findings(conn, 200, findings)

    rows = store.findings_for_run(conn, 200)

    assert sorted(r["check"] for r in rows) == sorted(f["check"] for f in findings)


def test_findings_for_run_is_scoped_to_the_run(conn):
    store.insert_findings(conn, 1, [_finding(check="run-one")])
    store.insert_findings(conn, 2, [_finding(check="run-two")])

    assert [r["check"] for r in store.findings_for_run(conn, 1)] == ["run-one"]
    assert [r["check"] for r in store.findings_for_run(conn, 2)] == ["run-two"]


def test_findings_for_run_on_a_run_with_no_findings_is_empty(conn):
    assert store.findings_for_run(conn, 999) == []


def test_insert_findings_defaults_missing_optional_fields_to_empty_string(conn):
    minimal = {"check": "dead-vocabulary", "severity": "info", "source": "deterministic"}
    store.insert_findings(conn, 300, [minimal])

    row = store.findings_for_run(conn, 300)[0]
    assert row["subject"] == ""
    assert row["detail"] == ""
    assert row["suggested_action"] == ""


# ── latest_completed_run — 'ok' and 'partial' both count as completed ───────────────────────────
def test_latest_completed_run_is_none_when_nothing_has_ever_run(conn):
    assert store.latest_completed_run(conn) is None


def test_latest_completed_run_reads_the_most_recent_ok_run(conn):
    support.seed_gardener_job_run(conn, status="ok", stats={"tag": "older"}, started_days_ago=5)
    newer_id = support.seed_gardener_job_run(conn, status="ok", stats={"tag": "newer"},
                                             started_days_ago=1)

    run = store.latest_completed_run(conn)

    assert run["id"] == newer_id
    assert run["stats"] == {"tag": "newer"}


def test_latest_completed_run_also_reads_a_partial_run(conn):
    """`'partial'` is now a HISTORICAL status: it meant a gardener model pass had failed while the
    deterministic findings committed anyway, and no run written today can be one. The predicate
    still accepts it because a deployed `job_runs` holds such rows — narrowing to `'ok'` would
    blank the digest and the console's gardener page on every deployment whose last completed run
    predates this change, until the next nightly pass."""
    run_id = support.seed_gardener_job_run(
        conn, status="partial", stats={"sweep": {"error": "SweepGarbage"}})   # a real old row

    run = store.latest_completed_run(conn)

    assert run["id"] == run_id


def test_latest_completed_run_ignores_an_error_run(conn):
    support.seed_gardener_job_run(conn, status="error", stats={})

    assert store.latest_completed_run(conn) is None


def test_latest_completed_run_picks_the_most_recent_among_ok_and_partial(conn):
    """Proves the `status IN ('ok', 'partial')` predicate orders correctly ACROSS the two
    statuses, not merely within one of them — a more recent (historical) 'partial' run must win
    over an older 'ok' one."""
    support.seed_gardener_job_run(conn, status="ok", stats={"tag": "older-ok"},
                                  started_days_ago=5)
    newer_id = support.seed_gardener_job_run(conn, status="partial", stats={"tag": "newer-partial"},
                                             started_days_ago=1)

    run = store.latest_completed_run(conn)

    assert run["id"] == newer_id


def test_insert_findings_persists_the_named_columns_only(conn):
    """`checks.build_finding`'s `**extra` lets a pass hang its own working keys off a finding, and
    the table has no column for them — so they must never survive the round trip and be mistaken
    for stored facts. `subjects` joined the persisted set when the repair loop needed the subject
    pages as DATA rather than as a comma-joined report line; an unnamed key still does not."""
    f = _finding(check="model-contradiction", severity="warn",
                _working_wording="a pass's own copy", _working_paths=["wiki/x.md"])
    store.insert_findings(conn, 400, [f])

    row = store.findings_for_run(conn, 400)[0]
    assert set(row) == {"id", "run_id", "check", "severity", "source", "subject", "detail",
                        "suggested_action", "created_at", "model_id", "subjects"}
