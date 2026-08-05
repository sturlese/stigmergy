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


def test_default_path_joins_ops_slack_channels_json_under_the_repo():
    assert default_path("/repo").endswith("ops/slack-channels.json")


def test_default_path_is_empty_with_no_repo():
    assert default_path(None) == ""
