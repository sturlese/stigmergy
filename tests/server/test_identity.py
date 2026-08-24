import json

import pytest

from stigmergy.server.errors import IdentityError
from stigmergy.server.identity import (
    audiences_from_text,
    channel_map_from_text,
    default_path,
    group_map_from_text,
    hash_token,
    known_groups_from_text,
    load_token_store,
    principal_from_text,
    resolve_audiences,
    resolve_default_audience,
    resolve_email_for_token,
)


def principal(display_name="Ana", groups=None, default=None):
    return {
        "display_name": display_name,
        "groups": groups if groups is not None else ["finance"],
        "default_audience": default if default is not None else ["finance"],
    }


def write_identities(tmp_path, data):
    path = tmp_path / "identities.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return str(path)


def test_principal_contract_carries_display_name_groups_and_capture_default(tmp_path):
    path = write_identities(tmp_path, {"ana": principal()})

    resolved = principal_from_text(json.dumps({"ana": principal()}), "ana", origin="test")

    assert resolved.display_name == "Ana"
    assert resolve_audiences(path, "ana") == ("finance",)
    assert resolve_default_audience(path, "ana") == ("finance",)


def test_master_group_is_the_only_unrestricted_reader(tmp_path):
    value = principal("Master", ["finance", "brain-admins"], ["finance"])
    path = write_identities(tmp_path, {"master": value})
    assert resolve_audiences(path, "master") is None


def test_organization_wide_capture_default_is_explicit_null(tmp_path):
    value = {
        "display_name": "Master",
        "groups": ["brain-admins"],
        "default_audience": None,
    }
    path = write_identities(tmp_path, {"master": value})
    assert resolve_default_audience(path, "master") is None


def test_scoped_default_must_be_held_by_the_principal(tmp_path):
    path = write_identities(tmp_path, {
        "ana": principal(groups=["finance"], default=["leadership"]),
    })
    with pytest.raises(IdentityError, match="defaults to a group"):
        resolve_audiences(path, "ana")


@pytest.mark.parametrize("value", [
    {"groups": ["finance"], "default_audience": ["finance"]},
    {"display_name": "Ana", "default_audience": ["finance"]},
    {"display_name": "Ana", "groups": ["finance"]},
    ["finance"],
])
def test_principal_requires_exact_current_fields(tmp_path, value):
    path = write_identities(tmp_path, {"ana": value})
    with pytest.raises(IdentityError, match="must define"):
        resolve_audiences(path, "ana")


@pytest.mark.parametrize("groups", [
    ["all"],
    ["*"],
    ["Brain-Admins"],
    ["finance", "finance"],
    ["finance,leadership"],
    ["x" * 65],
    [7],
])
def test_invalid_group_names_fail_closed(tmp_path, groups):
    path = write_identities(tmp_path, {"ana": principal(groups=groups)})
    with pytest.raises(IdentityError):
        resolve_audiences(path, "ana")


def test_too_many_groups_fail_closed(tmp_path):
    path = write_identities(tmp_path, {"ana": principal(groups=[f"g{i}" for i in range(33)])})
    with pytest.raises(IdentityError, match="32-group"):
        resolve_audiences(path, "ana")


def test_comment_entries_are_prose_and_not_principals(tmp_path):
    data = {"_comment": "Roster purpose", "ana": principal()}
    path = write_identities(tmp_path, data)
    assert resolve_audiences(path, "ana") == ("finance",)
    with pytest.raises(IdentityError, match="not configured"):
        resolve_audiences(path, "_comment")


def test_duplicate_or_case_colliding_identity_keys_fail_closed(tmp_path):
    for value in (
        '{"ana": {"display_name":"A","groups":[],"default_audience":null},'
        '"ana": {"display_name":"B","groups":[],"default_audience":null}}',
        '{"Ana": {"display_name":"A","groups":[],"default_audience":null},'
        '"ana": {"display_name":"B","groups":[],"default_audience":null}}',
    ):
        path = write_identities(tmp_path, value)
        with pytest.raises(IdentityError, match="duplicate keys"):
            resolve_audiences(path, "ana")


def test_missing_malformed_and_unknown_identity_fail_closed(tmp_path):
    with pytest.raises(IdentityError):
        resolve_audiences("", "ana")
    with pytest.raises(IdentityError):
        resolve_audiences(str(tmp_path / "missing.json"), "ana")
    with pytest.raises(IdentityError):
        audiences_from_text("{bad", "ana", origin="snapshot")
    with pytest.raises(IdentityError):
        audiences_from_text(json.dumps({"ana": principal()}), "unknown", origin="snapshot")


def test_known_groups_collects_the_configured_vocabulary():
    text = json.dumps({
        "ana": principal(groups=["finance", "leadership"], default=["finance"]),
        "master": {
            "display_name": "Master",
            "groups": ["brain-admins"],
            "default_audience": None,
        },
    })
    assert known_groups_from_text(text, origin="test") == {
        "finance", "leadership", "brain-admins"
    }


def test_channel_map_requires_explicit_private_groups_or_null():
    parsed = channel_map_from_text(
        json.dumps({"C_PUBLIC": None, "C_FIN": ["finance"]}), origin="channels"
    )
    assert parsed == {"C_PUBLIC": None, "C_FIN": ("finance",)}
    for invalid in ({"C": []}, {"C": ["brain-admins"]}, {"C": "finance"}):
        with pytest.raises(IdentityError):
            channel_map_from_text(json.dumps(invalid), origin="channels")


def test_external_group_maps_never_grant_unrestricted_access():
    assert group_map_from_text(
        json.dumps({"team-a": ["finance"]}), origin="groups", subject="team"
    ) == {"team-a": ("finance",)}
    with pytest.raises(IdentityError):
        group_map_from_text(
            json.dumps({"team-a": ["brain-admins"]}), origin="groups", subject="team"
        )


def test_default_path_uses_the_ops_contract():
    assert default_path("/repo").endswith("/repo/ops/identities.json")
    assert default_path(None) == ""


def test_token_hash_is_deterministic_sha256():
    digest = hash_token("token")
    assert len(digest) == 64
    assert digest == hash_token("token")
    assert digest != hash_token("other")


def test_token_store_prefers_inline_json_and_validates_digests(tmp_path):
    digest = hash_token("token")
    file_path = tmp_path / "tokens.json"
    file_path.write_text(json.dumps({hash_token("file"): "file@example.com"}), encoding="utf-8")
    assert load_token_store(json.dumps({digest: "ana@example.com"}), str(file_path)) == {
        digest: "ana@example.com"
    }
    assert load_token_store(None, str(file_path)) == {
        hash_token("file"): "file@example.com"
    }
    for value in ('{"not-a-digest":"ana@example.com"}', "[]", "{bad"):
        with pytest.raises(IdentityError):
            load_token_store(value, None)


def test_bearer_token_resolution_does_not_enumerate_identities():
    store = {hash_token("real"): "ana@example.com"}
    assert resolve_email_for_token(store, "real") == "ana@example.com"
    with pytest.raises(IdentityError) as error:
        resolve_email_for_token(store, "wrong")
    assert "ana@example.com" not in str(error.value)
