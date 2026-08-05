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


class WrongDatabase(RuntimeError):
    """A Postgres fixture was pointed at something that is not the test database."""


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
