"""Two suite runs must not share the test database — and the guard that says so must be armed.

`tests/testdb.py` already refuses a DSN naming anything but `stigmergy_test`. That answers "which
database" and says nothing about "how many of us", and the second question has the sharper failure
mode: `stigmergy_test` is one shared mutable fixture whose Postgres fixtures `DELETE FROM
capture_queue` at setup, so two concurrent runs delete each other's rows mid-flight. What a
reviewer then sees is not a collision anyone would recognise — it is tens of `LeaseLostError` and
"submission N does not exist" failures scattered across unrelated suites, a different count every
run. The cost is not the red run; it is that a red stops meaning anything.

**The load-bearing test here is the first one.** It does not check that `require_sole_test_run`
exists or that advisory locks work — it asks Postgres, from a foreign connection, whether THIS
run's lock is held right now. That is the only assertion that would go red if the guard were
wired up wrong, called too late, or silently swallowed at startup, and it costs one query.
"""
import pathlib

import pytest

from stigmergy.capture import schema as capture_schema
from stigmergy.slack import app as slack_app
from tests import testdb

# A key of this file's own, one above the run lock's. Every test below that needs to exercise the
# REFUSAL uses it rather than `RUN_LOCK_KEY`, because the only way to make the real key claimable
# again would be to release this run's own lock — putting a hole in the guard under test, for the
# duration of the test that proves the guard works.
PRIVATE_KEY = testdb.RUN_LOCK_KEY + 1


@pytest.fixture()
def foreign_conn():
    """A second connection to the test database, standing in for a second suite run.

    Closed unconditionally: an advisory lock lives as long as its session, so a leaked connection
    here would leave `PRIVATE_KEY` held for the rest of the run and turn the tests below into
    order-dependent ones.
    """
    conn = testdb.connect_or_skip("run-lock")
    try:
        yield conn
    finally:
        conn.close()


def _holds(conn, key: int) -> bool:
    """Can `conn` take `key`? Releases it again when it could, so asking does not answer."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
        got = cur.fetchone()[0]
        if got:
            cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
    return not got


# ── the guard is armed, for this very run ────────────────────────────────────────────────────────
def test_this_run_holds_the_run_lock_so_a_second_run_would_be_refused(foreign_conn):
    """The one assertion that fails if the guard is present but not working.

    Everything else in this file exercises `_RunLock` in isolation and would stay green if
    `pytest_configure` had stopped calling it, if the call had moved somewhere collection never
    reaches, or if the claim had failed and been swallowed. This asks the database instead: a
    connection that is not this run's cannot take `RUN_LOCK_KEY`, which is true only because this
    run took it before the first fixture ran.
    """
    assert _holds(foreign_conn, testdb.RUN_LOCK_KEY), (
        f"nothing holds advisory lock {testdb.RUN_LOCK_KEY} on {testdb.DATABASE} — this run did "
        f"not claim it, so a second `make test` would start straight into this database and the "
        f"two would delete each other's queue rows. Check that tests/conftest.py::pytest_configure "
        f"still calls testdb.require_sole_test_run().")


# ── the refusal ──────────────────────────────────────────────────────────────────────────────────
def test_a_second_run_is_refused_by_name_and_told_what_it_would_have_broken(foreign_conn):
    """The refusal has to be readable by someone who has not read this file. A steward-facing
    message would say "lock held"; an operator staring at a suite that will not start needs to
    know WHICH database, that their other run is unharmed, and that the failures they were about
    to spend an afternoon on were never about their change."""
    with foreign_conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (PRIVATE_KEY,))
        assert cur.fetchone()[0], "the stand-in run could not take its own key"

    lock = testdb._RunLock()
    with pytest.raises(testdb.ConcurrentTestRun) as caught:
        lock.claim(testdb.dsn(), PRIVATE_KEY)

    message = str(caught.value)
    assert testdb.DATABASE in message
    assert "Nothing was truncated" in message, "the refusal must say the other run is unharmed"
    assert "LeaseLostError" in message, (
        "the refusal must name the symptom, or the next person meets it without the diagnosis")


def test_the_refusal_names_the_backend_that_is_holding_the_database(foreign_conn):
    """Attribution, so "another run" is checkable rather than something to take on faith — the
    pid is enough to see whether it is a live `make test` or something forgotten in a shell."""
    with foreign_conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (PRIVATE_KEY,))
        cur.fetchone()
        cur.execute("SELECT pg_backend_pid()")
        holder_pid = cur.fetchone()[0]

    with pytest.raises(testdb.ConcurrentTestRun) as caught:
        testdb._RunLock().claim(testdb.dsn(), PRIVATE_KEY)

    assert f"pid {holder_pid}" in str(caught.value)


def test_a_refused_run_does_not_itself_become_a_holder(foreign_conn):
    """A refusal that left its own connection open would be a slow leak with a nasty shape: the
    refused run keeps a session on the database it was told to stay out of, and — once the first
    run finishes — a THIRD run is refused by a process that is not running any tests."""
    with foreign_conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (PRIVATE_KEY,))
        cur.fetchone()

    lock = testdb._RunLock()
    with pytest.raises(testdb.ConcurrentTestRun):
        lock.claim(testdb.dsn(), PRIVATE_KEY)

    assert lock.conn is None, "the refused claim kept its connection"


# ── the benign twin ──────────────────────────────────────────────────────────────────────────────
def test_a_lone_run_claims_the_database_and_re_claiming_it_is_free(foreign_conn):
    """The specificity half. A guard that only ever fires is a guard nobody can run the suite
    behind, and this one stands between every developer and `make test`.

    The second `claim` is not a repetition: it is the property that makes the guard safe to call
    from anywhere, including a harness that enters Postgres by another door. A guard that refused
    its OWN run the second time it was asked would be indistinguishable, from the outside, from
    the bug it exists to catch.
    """
    lock = testdb._RunLock()
    try:
        lock.claim(testdb.dsn(), PRIVATE_KEY)
        assert lock.conn is not None, "a lone run was not given the database"
        held = lock.conn

        lock.claim(testdb.dsn(), PRIVATE_KEY)          # asked again, mid-run
        assert lock.conn is held, "re-claiming opened a second connection"

        assert _holds(foreign_conn, PRIVATE_KEY), "the claim did not actually take the lock"
    finally:
        if lock.conn is not None:
            lock.conn.close()


# ── order, and the case where there is nothing to ask ────────────────────────────────────────────
def test_the_wrong_database_is_refused_before_a_connection_is_opened():
    """`require_test_database` runs FIRST inside the claim. Reversed, a run pointed at the dogfood
    would open a session on it to discover it should not have — and the whole point of that guard
    is that nothing is opened at all."""
    dogfood = testdb.DSN_DEFAULT.replace(f"/{testdb.DATABASE}", "/stigmergy")
    with pytest.raises(testdb.WrongDatabase):
        testdb.require_sole_test_run(dogfood)


def test_no_server_at_all_is_silent_rather_than_a_failure():
    """A laptop with no docker must still run the keyless suites. "There is no Postgres here" is
    `connect_or_skip`'s story to tell — it is the one that knows to skip on a laptop and to FAIL
    in CI — so this guard says nothing and lets that distinction stay in one place."""
    unreachable = f"postgresql://stigmergy:stigmergy@localhost:1/{testdb.DATABASE}"
    testdb.require_sole_test_run(unreachable)          # no raise, no skip, no output


def test_the_run_lock_key_collides_with_neither_key_the_shipped_code_takes():
    """Advisory locks share ONE flat namespace per database — the rule `slack/app.py` states where
    it picks its own key, and the reason it picked a distinct one.

    Two of these keys are taken by code a developer may well be running against `stigmergy_test`
    while the suite starts: the startup-DDL lock, which every entry point takes, and the
    Slack singleton. Reusing either would make the suite and that process wait on each other, and
    a hang names nothing — it does not read as a key collision, it reads as a flaky test.

    Compared by VALUE, deliberately. A test that grepped for the literal would go green the moment
    someone renamed a constant, and the collision it exists to prevent is between numbers.
    """
    taken = {"capture.schema startup DDL": capture_schema._STARTUP_DDL_LOCK_KEY,
             "slack.app singleton": slack_app._SINGLETON_LOCK_KEY}
    clashes = [name for name, key in taken.items() if key == testdb.RUN_LOCK_KEY]
    assert not clashes, (
        f"the suite's run lock ({testdb.RUN_LOCK_KEY}) is the same key as {clashes} — the suite "
        f"and that feature will block on each other, and it will surface as a test that hangs")


def test_every_advisory_lock_in_the_shipped_code_is_one_this_file_knows_about():
    """The pruning half, and the reason the test above cannot rot quietly.

    A third feature taking an advisory lock would collide with this one exactly as easily, and the
    comparison above would keep passing while knowing nothing about it. Naming the modules allowed
    to take a lock turns the next one into a decision — the same shape as
    `tests/test_deploy_defaults.py`'s declared subdirectory set.
    """
    src = pathlib.Path(__file__).resolve().parents[1] / "src"
    holders = sorted(str(p.relative_to(src)) for p in src.rglob("*.py")
                     if "pg_advisory_lock" in (t := p.read_text()) or "pg_try_advisory_lock" in t)
    assert holders == ["stigmergy/capture/schema.py", "stigmergy/slack/app.py"], (
        f"the set of modules taking a Postgres advisory lock changed to {holders}. Add the new "
        f"key to the comparison in the test above — it shares one namespace with the suite's run "
        f"lock, and a collision surfaces as a hang rather than as an error.")
