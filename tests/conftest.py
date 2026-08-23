"""Root conftest: pin the whole run to the test database before a single test module is imported.

`$STIGMERGY_INDEX_DSN` is what a RUNNING brain reads, and these suites drive a lot of real runtime
that resolves it for itself — the `stigmergy-server` subprocess spawned in `tests/server/conftest.py`,
the `stigmergy-queue` CLI in `tests/capture/test_cli.py`, `build_http_app`'s own connection. Setting
it here, once, before collection puts every one of those paths on `stigmergy_test` without thirty
call sites having to remember to.

It is overwritten UNCONDITIONALLY, whatever the environment brought in. The Makefile `-include`s a
gitignored `.env` and `export`s it into every target, so an operator who keeps `STIGMERGY_INDEX_DSN`
there for the dogfood — or for staging — would otherwise hand it straight to `make test`. The
value assigned is `testdb.dsn()`, which is guarded, so this can only ever pin the run to the test
database.

This is the convenience half. The load-bearing half is `tests.testdb.require_test_database`, which
refuses at the connection seam regardless of how a DSN arrived.

**The second autouse fixture below is the same doctrine, one credential over.** A run once
surfaced where one test file was missing the librarian's own `no_real_github_app` guard (a
fixture that had been copy-pasted, correctly, into `tests/librarian/conftest.py` and several
`tests/server/`/`tests/slack/` files individually) and, for that one file, `review.propose()`
minted a REAL GitHub App installation token and pushed over the real network during an ordinary
test run — surfacing as a confusing git conflict rather than an alarm. "One file remembering is
not a control": `no_real_github_app_anywhere` below closes the same class of gap structurally, at
the root of the whole suite, so no future test file — in ANY package, whether or not its authors
know the per-file convention exists — can reintroduce it by omission.
"""
import os

import pytest

from stigmergy.index import store
from stigmergy.librarian import githubapp
from tests import childwatch, testdb


def pytest_configure(config) -> None:
    """Runs once, before collection — so both of these are in place before any test module is
    imported, and before any fixture has truncated anything.

    `require_sole_test_run` is here rather than at the connection seam deliberately. The damage
    two concurrent runs do is not done by CONNECTING, it is done by running at all, so the honest
    place to refuse is the earliest point where a run is known to exist — and refusing here yields
    one legible line instead of the same message repeated by every Postgres fixture in the tree.
    """
    os.environ[store.DSN_ENV] = testdb.dsn()
    testdb.require_sole_test_run()


# The App's five environment variables — same tuple `tests/librarian/conftest.py::
# no_real_github_app` clears for its own package. Duplicated here deliberately rather than
# imported from there: the whole point is that THIS fixture must not depend on that file, or on
# any other file, continuing to exist or to be wired up correctly.
#
# `APP_LOGIN_ENV` belongs here for a reason the other four do not make obvious: it is not a
# credential, so it looks harmless, and it is the one an operator whose App is not named the
# default MUST set. The moment they do, an `.env` naming their slug reaches every test asserting
# the bot's commit identity, and five of them fail on a machine where nothing is wrong. Found
# exactly that way.
_LIBRARIAN_APP_ENV = (githubapp.APP_ID_ENV, githubapp.INSTALLATION_ID_ENV,
                      githubapp.PRIVATE_KEY_ENV, githubapp.PRIVATE_KEY_FILE_ENV,
                      githubapp.APP_LOGIN_ENV)


@pytest.fixture(autouse=True)
def no_real_github_app_anywhere(monkeypatch):
    """**No test run may touch state a human is using** — repo-wide, not package-by-package.

    `githubapp.configured()` reads `os.environ`; the Makefile's `-include .env` + `export` hands
    every target the operator's gitignored credentials; and `processing._file` asks
    `githubapp.configured()` to decide whether to push to the REAL `github.com/<slug>` instead of
    a test fixture's own bare remote. Before this fixture existed at the ROOT of the suite, that
    property depended on every package that could reach `processing._file` — or anything else
    that reads these variables — independently remembering to clear them, and at least one file
    did not. Autouse, at `tests/conftest.py`, means every test in the whole tree gets this
    regardless of which package it lives in or whether its author knew the convention existed.

    Every test that needs an EXPLICIT App configuration (`test_githubapp.py`, `no_real_github_app`
    in `tests/librarian/conftest.py`) passes its own `env` dict rather than relying on
    `os.environ` — that is what `configured(env)`/`installation_token(env)` take an argument for
    — so clearing the process environment here removes nothing from the App's own test coverage.
    """
    for name in _LIBRARIAN_APP_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_real_llm_anywhere(monkeypatch):
    """**No test run may make a real, paid, non-deterministic call to an LLM provider** — the
    exact same doctrine as `no_real_github_app_anywhere` above, one credential over. Found while
    investigating a full-suite-only failure in `tests/librarian/test_meeting_processing_pg.py`:

    `stigmergy.kernel.settings.resolve_backend` reads `$CLEAN_LLM` (default `"openai"`
    — the REAL backend) at call time, every time, and a package-scoped fixture that pins it to
    `"fake"` protects only its own package. A filing hook gave `tests/librarian/` a code path that
    reached an LLM-backed agent no test in that package had ever pinned.
    With a real `$OPENAI_API_KEY` in the environment (the Makefile's `-include .env` +
    `export`, the SAME mechanism `no_real_github_app_anywhere`'s own docstring names) and real
    network access, that hook made a genuine OpenAI call mid-test-run: reproduced directly —
    exporting a real key turned `tests/librarian/test_meeting_processing_pg.py::
    test_two_resolvable_entities_file_atomically_with_the_meeting_1to1_link_contract`'s silent,
    ~1s pass (no key -> `RuntimeError` -> the hook's own best-effort catch -> no second commit)
    into a ~20s run that legitimately regenerated a view and pushed a SECOND commit on top of
    the meeting's — the exact "ambient state" the test was failing on in the full suite only. Not
    test ORDER: whether the key/network happen to be live in the process, which correlates with
    `make test` (full suite, `.env` exported) far more than with a narrow ad hoc `pytest` run.

    "One file remembering is not a control" (that fixture's own words) applies identically here:
    pinned to `fake` (never `fake-flawed`, which deliberately misbehaves once) at the ROOT of the
    suite, so no future test file — in ANY package — can reintroduce a real, costly, non-
    deterministic network call by omission. A test that wants the real backend, the flawed fake,
    or no backend at all overrides this with its own `monkeypatch.setenv`/`.delenv` in the test
    body or a more specific fixture (`tests/kernel/test_settings.py`'s own
    `monkeypatch.delenv("CLEAN_LLM", ...)` for the true-default case, `tests/repair/test_sweep.py`'s
    `fake-flawed` override) — the same composition `no_real_github_app`/`test_githubapp.py`'s
    explicit `env` dicts already rely on for the App credential one fixture up.
    """
    monkeypatch.setenv("CLEAN_LLM", "fake")


@pytest.fixture(autouse=True)
def no_real_vision_anywhere(monkeypatch):
    """The same doctrine, one seam over: the drive flow's vision-OCR fallback goes live off
    ambient configuration alone. Before the two-form VISION_MODEL a stray call needed
    GEMINI_API_KEY; now a provider-prefixed VISION_MODEL plus that provider's key — both
    plausible in the `.env` that `make test` exports — is enough for any test in any package
    that reaches `librarian.processing._with_vision_fallback` to make a real, paid OCR call.
    One package's conftest remembering was not a control (its own words, two fixtures up), so
    the clearing lives HERE. A test that wants the fallback sets its own configuration
    explicitly."""
    monkeypatch.delenv("VISION_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def no_spawned_child_outlives_its_test():
    """**No test may leave a process running behind it** — the third reading of the same doctrine,
    one resource over from the two fixtures above.

    A leaked child is worse than a leaked variable, because it does not fail the test that leaked
    it. A surviving `stigmergy-librarian` or `stigmergy-queue` keeps claiming rows from
    `capture_queue` while some LATER test's fixture truncates the table under it, and the failures
    that produces (`LeaseLostError`, "submission N does not exist") land in whatever package
    happened to be running at the time, name no culprit, and move on the next run. That is the
    inverse of a useful red, and this is the fixture that makes the leak fail where it happened.

    Autouse and repo-wide, again for the reason `tests/childwatch.py` spells out: the suites that
    spawn real processes live in `tests/librarian/` and `tests/capture/` today, and "which
    packages spawn" is not a fact worth encoding anywhere.
    """
    childwatch.forget()
    yield
    strays = childwatch.reap(childwatch.strays())
    childwatch.forget()          # even on failure — the next test must not inherit this one's mess
    assert not strays, (
        f"a child process outlived its test and is still running: {'; '.join(strays)}. It has been "
        f"killed, but until this test reaps its own children the suite will keep producing "
        f"LeaseLostError and 'submission N does not exist' failures in unrelated packages, "
        f"because the survivor claims queue rows a later fixture is truncating. Wrap the spawn in "
        f"`try:` / `finally:` and kill it there.")
