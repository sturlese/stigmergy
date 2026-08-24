"""`stigmergy.slack.channels` — `ops/slack-channels.json`.

At least one test per surface runs on the DEFAULTS: the empty-audience default is the interesting
case here, asserted on a channel NOT in the config file rather than on one that is.
"""
import json

import pytest

from stigmergy.server.errors import IdentityError
from stigmergy.slack.channels import channel_audiences, default_path


@pytest.fixture()
def channels_path(tmp_path):
    path = tmp_path / "slack-channels.json"
    path.write_text(json.dumps({"C_FINANCE": ["finance"]}))
    return str(path)


def test_a_listed_channel_gets_its_configured_labels(channels_path):
    assert channel_audiences(channels_path, "C_FINANCE") == {"finance"}


def test_an_unlisted_channel_defaults_to_the_empty_set_not_none(channels_path):
    """The interesting default: a channel NOT in the file at all."""
    result = channel_audiences(channels_path, "C_RANDOM")
    assert result == set()
    assert result is not None


def test_no_channels_file_configured_at_all_also_defaults_to_the_empty_set():
    assert channel_audiences("", "C_ANY") == set()


def test_a_nonexistent_channels_file_path_also_defaults_to_the_empty_set(tmp_path):
    missing = str(tmp_path / "does-not-exist.json")
    assert channel_audiences(missing, "C_ANY") == set()


def test_a_malformed_channels_file_fails_closed(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json at all")
    with pytest.raises(IdentityError):
        channel_audiences(str(path), "C_ANY")


def test_a_channel_value_that_is_not_a_list_of_strings_fails_closed(channels_path, tmp_path):
    path = tmp_path / "bad2.json"
    path.write_text(json.dumps({"C_X": "finance"}))   # a bare string, not a list
    with pytest.raises(IdentityError):
        channel_audiences(str(path), "C_X")


def test_the_reserved_group_all_is_refused_here_too(tmp_path):
    """One grammar for both control files: the roster's rules are this map's rules,
    so a reserved name cannot be legal in one file and refused in the other."""
    path = tmp_path / "reserved.json"
    path.write_text(json.dumps({"C_X": ["all"]}))
    with pytest.raises(IdentityError, match="invalid group name"):
        channel_audiences(str(path), "C_X")


def test_a_malformed_NEIGHBOUR_channel_refuses_the_lookup_too(tmp_path):
    """A scoping file the server cannot make sense of never answers for the entry that happened to
    parse — the posture the roster already had, now this file's too."""
    path = tmp_path / "neighbour.json"
    path.write_text(json.dumps({"C_OK": ["finance"], "C_BAD": 7}))
    with pytest.raises(IdentityError, match="must be a list of group names"):
        channel_audiences(str(path), "C_OK")


def test_a_comment_key_names_a_channel_without_becoming_one(tmp_path):
    """What the `_` convention is FOR here: a raw channel id says nothing to a human reading the
    file, so the file gets to say which one it is."""
    path = tmp_path / "commented.json"
    path.write_text(json.dumps({"_C_FINANCE": "#finance, the numbers channel",
                                "C_FINANCE": ["finance"]}))
    assert channel_audiences(str(path), "C_FINANCE") == {"finance"}
    assert channel_audiences(str(path), "_C_FINANCE") == set()


def test_a_channel_is_NEVER_unrestricted_even_listed_as_brain_admins(tmp_path):
    same_bytes = json.dumps({"C_OPS": ["brain-admins"]})
    path = tmp_path / "admins.json"
    path.write_text(same_bytes)

    with pytest.raises(IdentityError, match="cannot use the master group"):
        channel_audiences(str(path), "C_OPS")


def test_default_path_joins_ops_slack_channels_json_under_the_repo():
    assert default_path("/repo").endswith("ops/slack-channels.json")


def test_default_path_is_empty_with_no_repo():
    assert default_path(None) == ""


# ── the text road, and the live chooser over a real snapshot ───────────────────────────────────
def test_channel_audiences_from_text_answers_exactly_as_the_file_road_does(channels_path):
    """One parse under both roads: whatever the file road answers, the text road answers for the
    same bytes — a listed channel's labels and the unlisted channel's empty set alike."""
    text = json.dumps({"C_FINANCE": ["finance"]})
    from stigmergy.slack.channels import channel_audiences_from_text

    assert channel_audiences_from_text(text, "C_FINANCE", origin="snapshot") == {"finance"}
    assert channel_audiences_from_text(text, "C_ELSE", origin="snapshot") == set()


def test_an_empty_snapshot_text_fails_closed_rather_than_reading_as_unscoped():
    """On the snapshot road "no scoping declared" is spelled `{}` — a committed statement — never
    bytes that failed to arrive. An empty text is malformed and raises, the same posture the
    identity roster takes."""
    from stigmergy.slack.channels import channel_audiences_from_text

    with pytest.raises(IdentityError):
        channel_audiences_from_text("", "C_FINANCE", origin="snapshot")


def test_channel_audiences_live_prefers_the_snapshot_and_falls_back_to_the_file(channels_path):
    """The deployed resolution, against a REAL snapshot row: a channel scoped by a pushed edit is
    scoped within seconds on a group that holds no checkout, and a database with no snapshot
    leaves the file road byte-for-byte as it was."""
    from stigmergy.index import store as index_store
    from stigmergy.slack.channels import channel_audiences_live
    from tests import testdb

    conn = testdb.connect_or_skip("slack")
    try:
        index_store.clear_ops_file(conn, index_store.SLACK_CHANNELS_RELPATH)
        assert channel_audiences_live(conn, channels_path, "C_FINANCE") == {"finance"}, (
            "no snapshot: the file road answers")

        index_store.write_ops_file(conn, index_store.SLACK_CHANNELS_RELPATH,
                                   json.dumps({"C_FINANCE": ["finance", "leadership"]}),
                                   "pushed-sha")
        assert channel_audiences_live(conn, channels_path, "C_FINANCE") == {
            "finance", "leadership"}, "a snapshot present: the pushed scope wins over the file"
    finally:
        index_store.clear_ops_file(conn, index_store.SLACK_CHANNELS_RELPATH)
        conn.close()
