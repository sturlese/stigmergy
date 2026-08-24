"""Repository-wide isolation for databases, credentials, models, and child processes."""

import os

import pytest

from stigmergy.index import store
from stigmergy.librarian import githubapp
from tests import childwatch, testdb


def pytest_configure(config) -> None:
    """Pin the test DSN and refuse concurrent suites before collection."""
    os.environ[store.DSN_ENV] = testdb.dsn()
    testdb.require_sole_test_run()


_LIBRARIAN_APP_ENV = (
    githubapp.APP_ID_ENV,
    githubapp.INSTALLATION_ID_ENV,
    githubapp.PRIVATE_KEY_ENV,
    githubapp.PRIVATE_KEY_FILE_ENV,
    githubapp.APP_LOGIN_ENV,
)


@pytest.fixture(autouse=True)
def no_real_github_app_anywhere(monkeypatch):
    """Prevent tests from using ambient GitHub App credentials."""
    for name in _LIBRARIAN_APP_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_real_llm_anywhere(monkeypatch):
    """Prevent tests from selecting a paid LLM through ambient configuration."""
    monkeypatch.setenv("CLEAN_LLM", "fake")


@pytest.fixture(autouse=True)
def no_spawned_child_outlives_its_test():
    """Fail the test that leaks a child process and reap it immediately."""
    childwatch.forget()
    yield
    strays = childwatch.reap(childwatch.strays())
    childwatch.forget()
    assert not strays, (
        f"a child process outlived its test and was killed: {'; '.join(strays)}. "
        "Wrap the spawn in `try:` / `finally:` and stop it there."
    )
