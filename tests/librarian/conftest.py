"""Fixtures for the librarian suite: a real Postgres connection (via `tests.testdb`, the guard
that refuses any DSN but the test database's), an empty `capture_queue` per test, and a real git
repo + bare remote wired to `processing.Deps` for the double agent (`support.py`).

**Every processing-level test needs a real secrets scanner.** `processing.process_item` scans the
MATERIAL for secrets before the agent ever runs (module docstring: "the secrets scan runs over
the material and not only over the diff"), so `require_gitleaks` gates the whole processing suite
the same way `connect_or_skip` gates it on Postgres — skip cleanly with neither installed, never a
silent pass that would make the secrets gate look tested when it was never run.
"""
import pytest

from stigmergy.capture import evidence as evidence_plane
from stigmergy.capture import schema
from stigmergy.librarian import githubapp, pricing, pydantic_backend
from tests import testdb
from tests.librarian import support

# The App's five environment variables. Cleared for every test in this package — see below.
# `APP_LOGIN_ENV` is in the group because it decides the identity commits are AUTHORED by: an
# operator whose App is not named the default sets it, and without this their `.env` reaches the
# tests that assert that identity.
_APP_ENV = (githubapp.APP_ID_ENV, githubapp.INSTALLATION_ID_ENV,
            githubapp.PRIVATE_KEY_ENV, githubapp.PRIVATE_KEY_FILE_ENV,
            githubapp.APP_LOGIN_ENV)

# Every provider key the filing backend can authenticate with — `worker._check_pydantic_backend`'s
# own table, never a retyped list, so a fourth provider is cleared the day it is added. The tuple
# it replaced named the RETIRED backend's CLI credentials; the doctrine below is why it was not
# simply deleted with them.
_AGENT_CREDENTIAL_ENV = tuple(sorted(pydantic_backend.PROVIDER_KEY_ENV.values()))


@pytest.fixture(autouse=True)
def no_real_github_app(monkeypatch):
    """**No test run may touch state a human is using** — including state that is not in Postgres.

    Closing that for the database alone leaves it reachable through a different variable.
    `githubapp.configured()` reads `os.environ`; the Makefile's `-include .env` + `export` hands
    every target the operator's gitignored credentials; so an operator whose real App id,
    installation id and private key sit in that file gets a `make test` that mints real
    installation tokens and pushes the fixtures' commits to the live knowledge repo — real writes
    to a company's knowledge base out of a test run.

    `processing._file` is the reachable path: it asks `githubapp.configured()` and, when the answer
    is yes, pushes to `githubapp.push_url(slug)` instead of to the fixtures' own bare remote. The
    fix is structural and belongs here rather than in the Makefile, for the same reason
    `tests.testdb` refuses a non-test DSN rather than trusting a naming convention: the property
    must hold whatever an operator keeps in their environment.

    Autouse and unconditional. `test_githubapp.py` drives every App code path by passing an
    explicit `env` dict (that is what `configured(env)` and `installation_token(env)` take), so
    clearing the process environment removes nothing from the App's own coverage.
    """
    for name in _APP_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_ambient_agent_credential(monkeypatch, tmp_path):
    """The same doctrine as `no_real_github_app`, applied to the filing agent's credential.

    `worker.startup_checks` refuses a real-backend run whose provider key is missing, so that a
    missing export is caught at start-up instead of several steps into a run. That refusal is only
    testable if whether a credential is present is decided by the TEST and not by the operator's
    gitignored `.env`, which the Makefile exports into `make test` — otherwise the positive cases
    pass on a developer machine and the negative cases pass in CI, and neither machine runs both.

    Cleared unconditionally; the tests that need a credential set one explicitly (`stub_startup`).
    Nothing here can reach a real API: no test in this package runs a real backend against a real
    provider, and every structured run is driven through an injected offline model.

    **The variables this clears changed with the backend, and the fixture did not.** It used to
    clear the retired Claude Code CLI's three credentials plus its gateway variable, and to point
    `$CLAUDE_CONFIG_DIR` at a path inside `tmp_path` that was deliberately never created — because
    that CLI could also authenticate from a stored interactive login, which exists on a developer
    machine and not in CI. There is no such second channel now: a provider key is a variable that is
    either exported or not, so clearing the variables IS the whole of it.
    """
    for name in _AGENT_CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    # The drive flow's vision fallback keys on GEMINI_API_KEY at call time
    # (`processing._with_vision_fallback`, mirroring `converters.vision_extract`'s own read).
    # Cleared for the same reason as the agent credential: whether vision "exists" is the
    # TEST's decision, never the operator's ambient .env — and no test in this package may
    # ever reach the real Gemini API. Tests that want the fallback set a fake key explicitly.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def no_ambient_pricing_override(monkeypatch):
    """The third variable the same doctrine applies to: `$STIGMERGY_LIBRARIAN_PRICING`.

    `pricing._override()` reads it at CALL time and merges it over the seeded table per id, so an
    operator who has priced a model in their gitignored `.env` — which `make` exports into `make
    test` — changes what `priced_models()` answers, what the unpriced refusal LISTS, and, if they
    happened to price the id a test picked as unpriced, whether that refusal fires at all. That is
    the same laptop-versus-CI asymmetry `no_ambient_agent_credential` exists to close, one variable
    over: the positive cases would pass on the developer's machine and the negative cases in CI,
    and neither machine would run both.

    Cleared unconditionally; the tests that need an override set one explicitly with `monkeypatch`.
    """
    monkeypatch.delenv(pricing.PRICING_ENV, raising=False)


def connect_or_skip():
    conn = testdb.connect_or_skip("librarian")
    schema.ensure_capture_schema(conn)
    return conn


@pytest.fixture(scope="module")
def conn():
    c = connect_or_skip()
    yield c
    c.close()


@pytest.fixture()
def clean_queue(conn):
    """Each test gets an empty `capture_queue`/`job_runs`/`ingest_errors` — same isolation posture
    as `tests/capture/conftest.py::clean_queue`. Safe to wipe: `conn` can only ever be the test
    database, `tests.testdb` raises before it opens anything else."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM capture_queue")
        cur.execute("DELETE FROM job_runs")
        cur.execute("DELETE FROM ingest_errors")
    return conn


@pytest.fixture()
def require_gitleaks():
    """Skip on a laptop with no gitleaks; FAIL in CI.

    Same fail-loud-in-CI posture as `testdb.required()` and `minio_or_skip`, and for a sharper
    reason than either: this gates the whole processing suite, so an absent binary silently removed
    the entire secrets-gate test surface from a green run. `ensure_scanner` says a secrets gate that
    silently passes is worse than no gate — a secrets gate whose TESTS silently skip is the same
    sentence one level up.
    """
    if support.gitleaks_available():
        return
    if testdb.required():
        pytest.fail("$STIGMERGY_TEST_DSN is set (CI mode) but gitleaks is not on PATH — refusing to "
                    "skip the librarian processing suites silently. Install gitleaks BEFORE the "
                    "test step (see .github/workflows/ci.yml).")
    pytest.skip("gitleaks not on PATH (brew install gitleaks / see docs/reference/"
                "operator-runbook.md) — the secrets gate cannot run without it")


@pytest.fixture()
def rig(tmp_path, require_gitleaks):
    """A fresh `(RepoEnv, Deps)` pair: a real git repo + bare remote seeded from
    `fixtures/repo/`, and `processing.Deps` wired to the offline double. One per test — nothing
    here is shared or reused across tests."""
    return support.build_rig(tmp_path)


def minio_or_skip() -> evidence_plane.S3EvidenceStore:
    """A real `S3EvidenceStore` against the compose `minio` service — same posture as
    `tests/capture/conftest.py::minio_or_skip`. Needed only by tests that cross a real PROCESS
    boundary (`test_worker_signals.py`): `MemoryEvidenceStore` is an in-process dict, invisible
    to a subprocess that builds its own `Deps`, so those tests need the one evidence store that
    is actually shared between two independent processes."""
    st = evidence_plane.store_from_env()
    try:
        st.client().list_buckets()
    except Exception as ex:  # noqa: BLE001 — any failure here means: no local MinIO
        if testdb.required():
            pytest.fail(f"${testdb.DSN_ENV} is set (CI mode) but MinIO at {st.endpoint_url} is "
                        f"unreachable — refusing to skip the librarian suites silently: {ex}")
        pytest.skip(f"no MinIO at {st.endpoint_url} (docker compose up -d --wait): {ex}")
    return st


@pytest.fixture()
def require_minio():
    return minio_or_skip()
