"""Fixtures for the Slack transport suite: a real Postgres connection (capture + slack DDL
ensured), the fake embedder, `MemoryEvidenceStore` for the write path, and a `FakeSlackGateway` per
test — same skip-vs-fail posture as `tests/capture/conftest.py`/`tests/server/conftest.py`.
"""
import json
import os

import pytest

from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.index import build
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server.settings import Settings
from stigmergy.slack.context import SlackContext
from stigmergy.slack.gateway import FakeSlackGateway
from stigmergy.slack.settings import SlackSettings, no_link_resolver
from stigmergy.slack.store import ensure_write_path_schema
from stigmergy.index import store as index_store

from tests import testdb
from tests.server.conftest import Fixture


def connect_or_skip():
    conn = testdb.connect_or_skip("slack")
    ensure_write_path_schema(conn)
    # Earlier suites' rebuilds may have cached THEIR fixture repos' access files; this suite's
    # file-road tests must not inherit them (arrange, never inherit — the freshness doctrine).
    for relpath in (index_store.IDENTITIES_RELPATH, index_store.SLACK_CHANNELS_RELPATH):
        index_store.clear_ops_file(conn, relpath)
    return conn


@pytest.fixture(scope="module")
def conn():
    c = connect_or_skip()
    yield c
    c.close()


@pytest.fixture()
def clean_tables(conn):
    """Each test gets empty `capture_queue`/`slack_submissions`/operational tables — mirrors
    `tests/capture/conftest.py::clean_queue`, extended to this module's own table."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM slack_submissions")
        cur.execute("DELETE FROM capture_queue")
        cur.execute("DELETE FROM job_runs")
        cur.execute("DELETE FROM ingest_errors")
    return conn


TEAM_ID = "T_STIGMERGY"
FINANCE_CHANNEL = "C_FINANCE"     # scoped to ["finance"] in slack-channels.json
UNLISTED_CHANNEL = "C_UNLISTED"   # not in slack-channels.json — the empty-set default


@pytest.fixture(scope="session")
def fixture(tmp_path_factory) -> Fixture:
    """The shared knowledge-repo fixture, plus `ops/slack-channels.json` for channel scoping."""
    fx = Fixture(str(tmp_path_factory.mktemp("slack-brain")))
    channels_path = os.path.join(fx.repo, "ops", "slack-channels.json")
    with open(channels_path, "w", encoding="utf-8") as f:
        json.dump({FINANCE_CHANNEL: ["finance"]}, f)
    fx.channels_path = channels_path
    return fx


@pytest.fixture(scope="module")
def indexed(fixture):
    conn = connect_or_skip()
    build.rebuild(conn, fixture.repo, build_embedder("fake"))
    yield conn, fixture
    conn.close()


def build_context(fixture: Fixture, conn, *, gateway=None) -> SlackContext:
    server_settings = Settings(identities_path=fixture.identities_path,
                               dsn=testdb.dsn(), embedder="fake", llm="fake")
    settings = SlackSettings(app_token="xapp-test", bot_token="xoxb-test", team_id=TEAM_ID,
                             channels_path=fixture.channels_path, server=server_settings)
    return SlackContext(settings=settings, gateway=gateway or FakeSlackGateway(), conn=conn,
                        embedder=build_embedder("fake"), evidence=MemoryEvidenceStore(),
                        link_resolver=no_link_resolver)
