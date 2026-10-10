"""Fixtures for the Slack transport suite: a real Postgres connection (capture + slack DDL
ensured), the fake embedder, `MemoryEvidenceStore` for the write path, and a `FakeSlackGateway` per
test — same skip-vs-fail posture as `tests/capture/conftest.py`/`tests/server/conftest.py`.
"""
import pytest

from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.index import build
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.server.settings import Settings
from stigmergy.slack.context import SlackContext
from stigmergy.slack.gateway import FakeSlackGateway
from stigmergy.slack.settings import SlackSettings, no_link_resolver
from stigmergy.slack.store import ensure_write_path_schema
from tests import testdb
from tests.roster import seed_roster
from tests.server.conftest import Fixture


def connect_or_skip():
    conn = testdb.connect_or_skip("slack")
    ensure_write_path_schema(conn)
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
        cur.execute("DELETE FROM capture_artifacts")
        cur.execute("DELETE FROM slack_submissions")
        cur.execute("DELETE FROM capture_queue")
        cur.execute("DELETE FROM job_runs")
    return conn


TEAM_ID = "T_STIGMERGY"
FINANCE_CHANNEL = "C_FINANCE"     # scoped to ["finance"] in the roster's channel map
PUBLIC_CHANNEL = "C_PUBLIC"
UNLISTED_CHANNEL = "C_UNLISTED"   # not mapped — captures nothing, answers unscoped
CHANNELS = {FINANCE_CHANNEL: ["finance"], PUBLIC_CHANNEL: None}


def seed_slack_roster(conn, fixture: Fixture) -> dict[str, str]:
    return seed_roster(conn, fixture.IDENTITIES, channels=CHANNELS)


@pytest.fixture(scope="session")
def fixture(tmp_path_factory) -> Fixture:
    return Fixture(str(tmp_path_factory.mktemp("slack-brain")))


@pytest.fixture(scope="module")
def indexed(fixture):
    conn = connect_or_skip()
    build.rebuild(conn, fixture.repo, build_embedder("fake"))
    seed_slack_roster(conn, fixture)
    yield conn, fixture
    conn.close()


@pytest.fixture()
def roster_seeded(conn, fixture):
    seed_slack_roster(conn, fixture)
    return conn


def build_context(fixture: Fixture, conn, *, gateway=None) -> SlackContext:
    server_settings = Settings(dsn=testdb.dsn(), embedder="fake", llm="fake")
    settings = SlackSettings(app_token="xapp-test", bot_token="xoxb-test", team_id=TEAM_ID,
                             server=server_settings)
    return SlackContext(settings=settings, gateway=gateway or FakeSlackGateway(), conn=conn,
                        embedder=build_embedder("fake"), evidence=MemoryEvidenceStore(),
                        link_resolver=no_link_resolver)
