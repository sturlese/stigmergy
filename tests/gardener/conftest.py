"""Fixtures for the gardener suite: a real Postgres connection with every schema this package's
checks read or write already ensured — real Postgres, never a faked query, on the same reasoning
that a faked git proves nothing about the property being claimed — plus a throwaway `--repo`
directory for the registry/view-staleness checks.
"""
import os

import pytest

from tests.gardener import support


@pytest.fixture()
def conn():
    c = support.connect_or_skip()
    support.clean(c)
    yield c
    c.close()


@pytest.fixture()
def repo(tmp_path) -> str:
    path = str(tmp_path / "repo")
    os.makedirs(path, exist_ok=True)
    return path
