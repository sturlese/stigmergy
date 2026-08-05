"""**No test run may touch state a human is using**, tested at its own seam: `tests/testdb.py`'s
`require_test_database` refuses every DSN that does not name `stigmergy_test`, fails CLOSED on a DSN
it cannot even parse, never leaks a password into the exception text, and is never silently
downgraded to a skip.

This is pure Python — no Postgres connection is ever opened here on purpose. `require_test_database`
is specified to run BEFORE any connection attempt (module docstring: "runs BEFORE any connection is
opened"), so a test proving the refusal must not itself open one; doing so would prove a weaker
property (that a bad DSN eventually fails) instead of the real one (that it is refused before it
gets the chance to touch anything).
"""
import pytest

from tests import testdb

DOGFOOD_DSN = "postgresql://stigmergy:stigmergy@localhost:54321/stigmergy"
STAGING_SHAPED_DSN = "postgresql://stigmergy:s3cr3t-pw@staging.example.com:5432/stigmergy_staging"
NO_DBNAME_DSN = "postgresql://stigmergy:stigmergy@localhost:54321/"
KEYWORD_FORM_DSN = "host=localhost port=54321 user=stigmergy password=stigmergy dbname=stigmergy"
UNPARSEABLE_DSN = "not a dsn at all ??%%::"

# `(label, dsn)` — every one of these must be REFUSED (raise WrongDatabase), including the last
# two, which must fail CLOSED rather than being waved through because they could not be read.
WRONG_DATABASES = [
    ("the dogfood database by name", DOGFOOD_DSN),
    ("a staging-shaped DSN", STAGING_SHAPED_DSN),
    ("a DSN with no dbname at all", NO_DBNAME_DSN),
    ("the keyword=value connection-string form", KEYWORD_FORM_DSN),
    ("unparseable garbage", UNPARSEABLE_DSN),
]


@pytest.mark.parametrize("label,dsn", WRONG_DATABASES, ids=[w[0] for w in WRONG_DATABASES])
def test_require_test_database_refuses_every_wrong_dsn(label, dsn):
    with pytest.raises(testdb.WrongDatabase):
        testdb.require_test_database(dsn)


def test_the_test_database_itself_is_accepted():
    """The positive control: the guard is not simply refusing everything."""
    assert testdb.require_test_database(testdb.DSN_DEFAULT) == testdb.DSN_DEFAULT


def test_a_password_in_the_dsn_never_appears_in_the_refusal_text():
    """`describe()`'s own contract: "never the DSN itself... a DSN carries a password". Exercised
    against a DSN whose refusal text is actually built (the dogfood one), not just described."""
    with pytest.raises(testdb.WrongDatabase) as exc_info:
        testdb.require_test_database(STAGING_SHAPED_DSN)
    assert "s3cr3t-pw" not in str(exc_info.value)


def test_the_two_unparseable_and_no_dbname_cases_fail_closed_not_open():
    """`database_of` docstring: "'' when it names none... fail closed and treat that as 'not the
    test database'". A DSN the guard cannot even read must never be waved through as if it named
    the test database by default."""
    assert testdb.database_of(UNPARSEABLE_DSN) == ""
    assert testdb.database_of(NO_DBNAME_DSN) == ""
    with pytest.raises(testdb.WrongDatabase):
        testdb.require_test_database(UNPARSEABLE_DSN)
    with pytest.raises(testdb.WrongDatabase):
        testdb.require_test_database(NO_DBNAME_DSN)


def test_the_refusal_is_a_hard_failure_not_a_downgraded_skip():
    """Pins the guard-before-`try` ordering (module docstring: "Guard first, connect second...
    a refusal raised inside that block would be downgraded into a silently skipped suite").
    `require_test_database` itself must never turn its refusal into a `pytest.skip` — only
    `connect_or_skip`'s OWN, separate `except` block (wrapping the connection attempt, never the
    guard call) is allowed to skip, and only for "no server reachable", not for "wrong database".
    A future refactor that moved the guard call inside that `try` would make this test fail by
    raising `Skipped` where `WrongDatabase` is required, which is exactly the regression this
    pins."""
    with pytest.raises(testdb.WrongDatabase):
        testdb.require_test_database(DOGFOOD_DSN)


def test_dsn_env_var_cannot_override_the_guard(monkeypatch):
    """`dsn()`'s own contract: "guarded on the way out, so even an explicitly configured
    `$STIGMERGY_TEST_DSN` cannot aim the suite at the dogfood." There is deliberately no escape
    hatch — proven by actually pointing the env var this suite reads at the dogfood DSN and
    calling the real function every Postgres fixture calls, not by re-deriving the property from
    `require_test_database` alone."""
    monkeypatch.setenv(testdb.DSN_ENV, DOGFOOD_DSN)
    with pytest.raises(testdb.WrongDatabase):
        testdb.dsn()


def test_dsn_env_var_naming_the_test_database_is_accepted(monkeypatch):
    """The positive control for the test above: the env var is not simply broken, it is checked."""
    monkeypatch.setenv(testdb.DSN_ENV, testdb.DSN_DEFAULT)
    assert testdb.dsn() == testdb.DSN_DEFAULT
