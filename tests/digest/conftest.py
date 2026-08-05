"""Fixtures for the digest suite: a real Postgres connection with every schema this package reads
or writes already ensured, and a throwaway `--repo` directory for the `ops/slack-channels.json`
fixtures. Real Postgres, never a faked query.
"""
import os

import pytest

from tests.digest import support


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
