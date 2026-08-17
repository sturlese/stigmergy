"""Fixtures for the repair suite: a real Postgres connection with every schema this package reads
or writes already ensured, `CLEAN_LLM` pinned to the offline double, and a real knowledge repo.

`clean_llm` is autouse and unconditional: this package builds a model-backed agent, and a machine
with `CLEAN_LLM=openai` in its environment would otherwise turn an offline suite into one that
needs a key and spends money. The suite is keyless by construction, and this is what makes that
true here rather than assumed.
"""
import pytest

from tests.repair import support


@pytest.fixture(autouse=True)
def clean_llm(monkeypatch):
    monkeypatch.setenv("CLEAN_LLM", "fake")


@pytest.fixture()
def conn():
    c = support.connect_or_skip()
    support.clean(c)
    yield c
    c.close()


@pytest.fixture()
def repo_env(tmp_path):
    """A bare remote plus a clone of the fixture knowledge repo, carrying the proposer's skill."""
    return support.build_repo(tmp_path)


@pytest.fixture()
def settings(repo_env):
    from stigmergy.repair.settings import RepairSettings
    return RepairSettings(repo=repo_env.repo)


@pytest.fixture()
def require_gitleaks():
    """Skip on a laptop with no gitleaks; FAIL in CI — the same posture
    `tests/entities/conftest.py` and `tests/librarian/conftest.py` take, and the reason is this
    package's own: the apply path's secrets veto is one of two defenses this suite exists to
    prove, and a plain `skipif` would let it stop running inside a green CI run without anybody
    noticing."""
    from tests import testdb
    from tests.librarian import support as librarian_support

    if librarian_support.gitleaks_available():
        return
    if testdb.required():
        pytest.fail("$STIGMERGY_TEST_DSN is set (CI mode) but gitleaks is not on PATH — refusing "
                    "to skip the repair apply-path secrets gate silently. Install gitleaks BEFORE "
                    "the test step (see .github/workflows/ci.yml).")
    pytest.skip("gitleaks not on PATH (brew install gitleaks) — the apply path's secrets gate "
                "cannot be exercised without it")
