import json

import pytest

from stigmergy.server.controls import (
    IDENTITIES_PATH,
    REGISTRY_PATH,
    SLACK_CHANNELS_PATH,
    ControlError,
    validate_texts,
)


def _texts(*, channels=None):
    return {
        IDENTITIES_PATH: json.dumps(
            {
                "master": {
                    "display_name": "Master",
                    "groups": ["brain-admins", "finance"],
                    "default_audience": None,
                }
            }
        ),
        REGISTRY_PATH: '{"version":1,"entities":{},"redirects":{}}',
        SLACK_CHANNELS_PATH: json.dumps(channels or {}),
    }


def test_control_set_accepts_open_and_known_group_channels():
    result = validate_texts(_texts(channels={"C1": None, "C2": ["finance"]}))

    assert result.slack_channels == {"C1": None, "C2": ("finance",)}


def test_control_set_rejects_groups_absent_from_the_identity_vocabulary():
    with pytest.raises(ControlError, match="unknown group"):
        validate_texts(_texts(channels={"C1": ["legal"]}))


def test_control_set_rejects_an_empty_identity_roster():
    values = _texts()
    values[IDENTITIES_PATH] = "{}"

    with pytest.raises(ControlError, match="no principals"):
        validate_texts(values)
