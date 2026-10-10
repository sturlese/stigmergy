"""Slack channel audiences come from the Postgres roster on every event."""
import pytest

from stigmergy.server import roster
from stigmergy.server.errors import IdentityError
from stigmergy.slack.channels import channel_audiences_live, channel_scope_for_capture
from tests import testdb
from tests.roster import seed_roster


@pytest.fixture()
def conn():
    conn = testdb.connect_or_skip("slack-channels")
    seed_roster(conn, {}, channels={"C0FINANCE1": ["finance"], "C0GENERAL1": None})
    yield conn
    conn.close()


def test_a_mapped_channel_scopes_answers_and_captures_to_its_groups(conn):
    assert channel_audiences_live(conn, "C0FINANCE1") == {"finance"}
    assert channel_scope_for_capture(conn, "C0FINANCE1") == ("finance",)


def test_an_organization_wide_channel_captures_for_everyone_and_scopes_answers_to_nothing(conn):
    assert channel_scope_for_capture(conn, "C0GENERAL1") is None
    assert channel_audiences_live(conn, "C0GENERAL1") == set()


def test_an_unmapped_channel_captures_nothing(conn):
    assert channel_audiences_live(conn, "C0RANDOM01") == set()
    with pytest.raises(IdentityError):
        channel_scope_for_capture(conn, "C0RANDOM01")


def test_a_channel_change_takes_effect_on_the_next_event(conn):
    roster.set_channel(conn, "C0RANDOM01", ["finance"], actor="test")
    assert channel_scope_for_capture(conn, "C0RANDOM01") == ("finance",)
    roster.delete_channel(conn, "C0FINANCE1")
    with pytest.raises(IdentityError):
        channel_scope_for_capture(conn, "C0FINANCE1")


def test_a_channel_can_never_capture_for_the_unrestricted_group(conn):
    with pytest.raises(roster.RosterRefused):
        roster.set_channel(conn, "C0OPS00001", ["brain-admins"], actor="test")
