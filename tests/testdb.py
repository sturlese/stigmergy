"""The suite's own database, and the refusal that keeps every Postgres fixture off any other one.

A `make test` in one terminal once deleted three real captures from the local dogfood in another,
mid-demo, and left fixture rows in their place. The mechanism was not exotic: the Postgres
fixtures truncate `capture_queue`, `job_runs` and `ingest_errors` at setup, and they resolved the
SAME DSN the dogfood MCP servers serve. Everything else in that database could be rebuilt from
git — the index is a disposable cache — but a queued capture exists NOWHERE else until the
librarian files it.

So the separation is structural, not a convention someone remembers:

  * the composition creates a SECOND database beside `stigmergy` (`scripts/postgres-init/`);
  * the suites resolve their DSN here, from a variable a running brain never reads;
  * `require_test_database()` runs BEFORE any connection is opened and RAISES when the resolved
    DSN names anything else — the dogfood, staging, a colleague's box. There is no override flag:
    an escape hatch is a rule someone remembers, and remembering is what failed.

The SECOND guard here answers a different question — not "which database", but "how many of us".
`stigmergy_test` is one shared mutable fixture, and the Postgres fixtures `DELETE FROM
capture_queue` at setup. Two `make test` runs against it delete each other's rows mid-flight, and
the symptom is not a clear collision: it is `LeaseLostError`, "submission N does not exist", tens
of failures scattered across suites that have nothing to do with each other, and a different count
on every run. That is worse than a plain failure, because it teaches a reviewer to distrust a red.
`require_sole_test_run()` takes a session-level advisory lock on the way in, so the second run is
refused by name in one line instead of corrupting the first.

Named `testdb.py` rather than `test_db.py` on purpose — pytest collects `test_*.py`, and this is
support code, not a suite. For the same reason nothing exported here is named `test_*` either.
"""
import os

import psycopg
import pytest
from psycopg import conninfo as _conninfo

from stigmergy.index import store

# Deliberately NOT `$STIGMERGY_INDEX_DSN`. That variable is what a RUNNING brain reads
# (`stigmergy.index.store.dsn()`), which makes it precisely the variable an operator's gitignored
# `.env` sets — and the Makefile's `-include .env` + `export` hands that file to every target,
# `make test` included. Reusing it for the suite is how a test run reached the dogfood in the
# first place, and it is a live path to staging too. This mirrors the discipline the Makefile
# already applies in the other direction (`STAGING_DSN` is deliberately not `STIGMERGY_INDEX_DSN`
# "so 'make test' can never point at staging"): the suite's database gets a name production never
# reads, and production keeps a name the suite never reads.
DSN_ENV = "STIGMERGY_TEST_DSN"

DATABASE = "stigmergy_test"
DSN_DEFAULT = f"postgresql://stigmergy:stigmergy@localhost:54321/{DATABASE}"


# The advisory-lock key `require_sole_test_run` claims for the whole run. Spelled the way the two
# keys in `src/` already are (`capture.schema`'s `SYNCDDL`, `slack.app`'s `SYNSLCK`) so a reader
# who has met one recognises this one — and so the value is derivable rather than a magic number
# somebody would be tempted to "tidy".
#
# The house rule those two state is what makes the spelling load-bearing: advisory locks live in
# ONE flat namespace per database, so two features sharing a key interfere for no reason and the
# symptom is a hang, not a name collision. `test_run_lock.py` asserts this key differs from both.
# Non-negative by construction, which `_advisory_lock_holder` needs: it splits the key into the
# `classid`/`objid` pair `pg_locks` stores it as, and a negative value would not survive the trip.
RUN_LOCK_KEY = int.from_bytes(b"TESTRUN", "big")


class WrongDatabase(RuntimeError):
    """A Postgres fixture was pointed at something that is not the test database."""


class ConcurrentTestRun(RuntimeError):
    """A second suite run tried to start against the test database while one was already in it."""


def describe(conninfo: str) -> str:
    """`host:port/dbname` — never the DSN itself. A DSN carries a password and this string lands
    in error messages and CI logs."""
    try:
        parsed = _conninfo.conninfo_to_dict(conninfo)
    except Exception:  # noqa: BLE001 — an unparseable DSN is still something we must name safely
        return "<unparseable DSN>"
    return f"{parsed.get('host', '?')}:{parsed.get('port', '?')}/{parsed.get('dbname') or '<default>'}"


def database_of(conninfo: str) -> str:
    """The database a DSN names, '' when it names none (libpq would then default it to the user
    name — fail closed and treat that as "not the test database")."""
    try:
        return str(_conninfo.conninfo_to_dict(conninfo).get("dbname") or "")
    except Exception:  # noqa: BLE001 — unparseable means unknown means refuse
        return ""


def require_test_database(conninfo: str) -> str:
    """THE guard. Returns `conninfo` unchanged when it names the test database; raises otherwise.

    Called on every DSN a fixture resolves, however it arrived — env var, built-in default, or an
    explicit override handed to a harness — because "which of these three paths did we check"
    is exactly the kind of question that gets one of them wrong.
    """
    found = database_of(conninfo)
    if found != DATABASE:
        raise WrongDatabase(
            f"refusing to point a Postgres fixture at {describe(conninfo)}: the suites TRUNCATE "
            f"capture_queue, job_runs, ingest_errors and audit_log at setup, and "
            f"{found or '<no database>'!r} is not the test database ({DATABASE!r}). "
            f"A queued capture exists nowhere else until the librarian files it — this refusal is "
            f"what keeps `make test` from deleting one. Run `make db-up` to create {DATABASE}, or "
            f"set ${DSN_ENV} to a DSN naming it (default: {DSN_DEFAULT}).")
    return conninfo


def dsn() -> str:
    """The DSN every Postgres-backed suite runs against — guarded on the way out, so even an
    explicitly configured `${DSN_ENV}` cannot aim the suite at the dogfood."""
    return require_test_database(os.environ.get(DSN_ENV, DSN_DEFAULT))


def required() -> bool:
    """True when this run MUST reach a real Postgres, making an unreachable one a FAILURE rather
    than a skip. CI sets `$STIGMERGY_TEST_DSN` explicitly; a laptop with no docker sets nothing and
    skips cleanly. The signal used to be `$STIGMERGY_INDEX_DSN` being set — that variable says
    nothing about the suite any more, so the signal moved with the DSN."""
    return bool(os.environ.get(DSN_ENV))


def _with_timeout(conninfo: str) -> str:
    return conninfo + ("?connect_timeout=2" if "?" not in conninfo else "")


def _server_is_up(conninfo: str) -> bool:
    """Is Postgres answering on this host/port at all, on the maintenance database?

    Separates "no stack on this machine" (skip, so `make test` stays green without docker) from
    "the stack is up but `stigmergy_test` was never created" (fail, with the fix). Without the
    distinction, a container started before the init script existed would silently drop every
    Postgres suite out of a green run — a green suite that catches nothing.
    """
    try:
        params = _conninfo.conninfo_to_dict(conninfo)
        params["dbname"] = "postgres"
        params["connect_timeout"] = 2
        psycopg.connect(_conninfo.make_conninfo(**params)).close()
        return True
    except Exception:  # noqa: BLE001 — any failure here means: no server to talk to
        return False


def _advisory_lock_holder(cur, key: int) -> str:
    """`pid N, connected since T` for whoever holds `key`, or `''` when it cannot be attributed.

    A bigint advisory key is stored SPLIT in `pg_locks` — the high 32 bits in `classid`, the low 32
    in `objid`, `objsubid = 1`. Reassembling the key rather than listing every advisory lock keeps
    the answer about THIS one, which is the difference between "another suite run is in there" and
    "something, somewhere, holds a lock". Attribution is best-effort by design: a refusal that
    cannot name the holder is still a correct refusal, so every failure here degrades to `''`.
    """
    try:
        cur.execute(
            "SELECT l.pid, a.backend_start FROM pg_locks l "
            "LEFT JOIN pg_stat_activity a ON a.pid = l.pid "
            "WHERE l.locktype = 'advisory' AND l.granted "
            "  AND l.classid = %s AND l.objid = %s AND l.objsubid = 1",
            (key >> 32, key & 0xFFFFFFFF))
        row = cur.fetchone()
    except psycopg.Error:
        return ""
    if not row:
        return ""
    since = f", connected since {row[1]:%H:%M:%S}" if row[1] else ""
    return f"pid {row[0]}{since}"


class _RunLock:
    """One suite run's claim on the test database, held as a session-level advisory lock.

    The connection is kept for the process's whole life ON PURPOSE and never closed: a
    session-level advisory lock is released when its SESSION ends, so the lock lasts exactly as
    long as this object's connection. Letting process exit release it covers the cases no
    `finally` would have — `SIGKILL`, an OOM kill, a crashed interpreter — which is why there is
    no stale-lock story to tell here and no override flag to forget.

    `_RUN_LOCK` below is the real one. The guard's own tests build their own instance with a
    private key, because releasing the real lock in order to exercise the refusal would open a
    hole in the very guard under test.
    """

    def __init__(self) -> None:
        self.conn: psycopg.Connection | None = None

    def claim(self, conninfo: str, key: int) -> None:
        # The database guard runs FIRST, ahead of the "already claimed" shortcut. Ordered the
        # other way — as this was written — a run that had claimed the test database would then
        # accept ANY later DSN in silence, including the dogfood's, because the shortcut returned
        # before anything was checked. Nothing exercised that path in production, and the test for
        # this ordering is what found it.
        conninfo = require_test_database(conninfo)   # raises before anything is opened
        if self.conn is not None:
            return                       # this run already holds it — idempotent, and free
        try:
            conn = store.connect(_with_timeout(conninfo))
        except psycopg.Error:
            return    # no server here. `connect_or_skip` owns that story, with its skip/fail
        try:          # distinction intact; telling it twice is how two voices drift apart.
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
                if cur.fetchone()[0]:
                    self.conn = conn     # held from here until the process dies
                    return
                holder = _advisory_lock_holder(cur, key)
        except BaseException:
            conn.close()
            raise
        conn.close()
        raise ConcurrentTestRun(
            f"refusing to start a second suite run against {describe(conninfo)}: another run "
            f"already holds this database ({holder or 'holder not attributable'}). "
            f"{DATABASE!r} is ONE shared database and the Postgres fixtures DELETE FROM "
            f"capture_queue, job_runs and ingest_errors at setup, so two runs delete each other's "
            f"rows mid-flight. The symptom is not a collision anyone would recognise: it is tens "
            f"of LeaseLostError and 'submission N does not exist' failures, in a different place "
            f"and a different count on every run, across suites that have nothing to do with each "
            f"other or with the change under test. Wait for the other run to finish, or stop it, "
            f"then start this one again. Nothing was truncated — this run stopped before its "
            f"first fixture.")


_RUN_LOCK = _RunLock()


def require_sole_test_run(conninfo: str | None = None) -> None:
    """THE SECOND guard: refuse to start when another suite run already holds the test database.

    Called once from `tests/conftest.py::pytest_configure`, before collection, so a doomed run
    produces ONE refusal at the top of the output instead of the same message repeated by every
    Postgres fixture in the tree — and so it stops before it has truncated anything belonging to
    the run already in there.

    Silent when there is no server to ask. "No postgres on this machine" is `connect_or_skip`'s
    story, and it has a skip/fail distinction this has no business restating.
    """
    _RUN_LOCK.claim(dsn() if conninfo is None else conninfo, RUN_LOCK_KEY)


def connect_or_skip(suite: str) -> psycopg.Connection:
    """The ONE place a Postgres-backed suite opens its connection (`tests/capture`, `tests/server`,
    `tests/index` all arrive here). `suite` only names the caller in the diagnostics.

    Guard first, connect second, and deliberately so: the connect below is wrapped in a broad
    `except` that turns "no database here" into a skip, and a refusal raised inside that block
    would be downgraded into a silently skipped suite — the precise failure this module exists to
    prevent.
    """
    conninfo = dsn()                     # raises WrongDatabase before anything is opened
    try:
        return store.connect(_with_timeout(conninfo))
    except Exception as ex:  # noqa: BLE001 — any connection failure means: no test database here
        if required():
            pytest.fail(f"${DSN_ENV} is set (CI mode) but {describe(conninfo)} is unreachable — "
                        f"refusing to skip the {suite} suites silently: {ex}")
        if _server_is_up(conninfo):
            pytest.fail(f"postgres is up but database {DATABASE!r} does not exist, so the {suite} "
                        f"suites would silently skip inside a green run. Recreate the composition "
                        f"(`make db-down && make db-up`) — an init script creates it on a fresh "
                        f"start, and a container older than that script never ran it: {ex}")
        pytest.skip(f"no postgres at {describe(conninfo)} (docker compose up -d --wait): {ex}")
