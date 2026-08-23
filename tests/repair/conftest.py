"""Fixtures for the removal suite: a real Postgres connection with every schema this package reads
or writes already ensured, and `CLEAN_LLM` pinned to the offline double.

`clean_llm` is autouse and unconditional: this package builds a model-backed agent (the sweep
writer), and a machine with `CLEAN_LLM=openai` in its environment would otherwise turn an offline
suite into one that needs a key and spends money. The suite is keyless by construction, and this is
what makes that true here rather than assumed.
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
