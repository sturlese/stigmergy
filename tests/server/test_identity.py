"""Identity resolution — fail-closed on every path: the audience resolver, and the per-request
token store (`hash_token`, `load_token_store`, `resolve_email_for_token`)."""
import json

import pytest

from stigmergy.server.errors import IdentityError
from stigmergy.server.identity import (
    default_path,
    hash_token,
    load_token_store,
    resolve_audiences,
    resolve_email_for_token,
)


def _write(tmp_path, data) -> str:
    p = tmp_path / "identities.json"
    p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return str(p)


def test_unrestricted_star_resolves_to_none(tmp_path):
    path = _write(tmp_path, {"steward": "*"})
    assert resolve_audiences(path, "steward") is None


def test_scoped_list_resolves_to_tuple(tmp_path):
    path = _write(tmp_path, {"ana": ["finance"], "bob": ["sales", "leadership"]})
    assert resolve_audiences(path, "ana") == ("finance",)
    assert resolve_audiences(path, "bob") == ("sales", "leadership")


def test_bare_label_is_a_one_audience_scope(tmp_path):
    path = _write(tmp_path, {"ana": "finance"})
    assert resolve_audiences(path, "ana") == ("finance",)


def test_empty_list_is_a_nobody_scope(tmp_path):
    path = _write(tmp_path, {"nobody": []})
    assert resolve_audiences(path, "nobody") == ()      # sees only unlabeled (open) content


def test_no_identity_given_fails_closed(tmp_path):
    path = _write(tmp_path, {"steward": "*"})
    with pytest.raises(IdentityError, match="no identity given"):
        resolve_audiences(path, None)


def test_unknown_identity_fails_closed_and_names_known(tmp_path):
    path = _write(tmp_path, {"steward": "*", "ana": ["finance"]})
    with pytest.raises(IdentityError, match="unknown identity 'ghost'"):
        resolve_audiences(path, "ghost")


def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(IdentityError, match="not found"):
        resolve_audiences(str(tmp_path / "nope.json"), "steward")


def test_no_file_configured_fails_closed():
    with pytest.raises(IdentityError, match="no identities file configured"):
        resolve_audiences("", "steward")


def test_malformed_json_fails_closed(tmp_path):
    path = _write(tmp_path, "{not valid json")
    with pytest.raises(IdentityError, match="unreadable or malformed"):
        resolve_audiences(path, "steward")


def test_non_object_json_fails_closed(tmp_path):
    path = _write(tmp_path, ["steward", "ana"])
    with pytest.raises(IdentityError, match="malformed"):
        resolve_audiences(path, "steward")


def test_malformed_audience_value_fails_closed(tmp_path):
    # an identity mapped to something that is neither "*" nor a list (here: a bool) must never
    # silently open — fail closed with an actionable message.
    path = _write(tmp_path, {"steward": True})
    with pytest.raises(IdentityError, match="malformed audience value"):
        resolve_audiences(path, "steward")


def test_null_audience_value_fails_closed(tmp_path):
    path = _write(tmp_path, {"steward": None})
    with pytest.raises(IdentityError, match="malformed audience value"):
        resolve_audiences(path, "steward")


def test_default_path_from_repo():
    assert default_path("/repo").endswith("/repo/ops/identities.json")
    assert default_path(None) == ""


# ── hash_token / load_token_store / resolve_email_for_token ────────────────────────────────────
# `tests/server/test_transport_http.py` exercises this trio end to end THROUGH the HTTP transport,
# but always with an already-built `dict` token store — `load_token_store`'s OWN fail-closed
# loading logic (the missing-store and malformed-store paths, and the sha256 hashing the whole
# lookup depends on) is otherwise never called by any test. These are pure unit tests: no DB, no
# HTTP.
def test_hash_token_is_sha256_hex_and_deterministic():
    digest = hash_token("a-plaintext-token")
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
    assert hash_token("a-plaintext-token") == digest         # deterministic
    assert hash_token("a-different-token") != digest


def test_load_token_store_prefers_inline_json_over_a_file_path(tmp_path):
    file_path = tmp_path / "tokens.json"
    file_path.write_text(json.dumps({"fromfile": "file@example.com"}), encoding="utf-8")
    store = load_token_store('{"fromenv": "env@example.com"}', str(file_path))
    assert store == {"fromenv": "env@example.com"}           # inline JSON wins, file never read


def test_load_token_store_reads_the_file_path_when_inline_is_absent(tmp_path):
    file_path = tmp_path / "tokens.json"
    file_path.write_text(json.dumps({"abc123": "ana@example.com"}), encoding="utf-8")
    assert load_token_store(None, str(file_path)) == {"abc123": "ana@example.com"}


def test_load_token_store_neither_configured_fails_closed():
    with pytest.raises(IdentityError, match="no token store configured"):
        load_token_store(None, None)


def test_load_token_store_missing_file_fails_closed(tmp_path):
    with pytest.raises(IdentityError, match="token store file not found"):
        load_token_store(None, str(tmp_path / "nope.json"))


def test_load_token_store_malformed_inline_json_fails_closed():
    with pytest.raises(IdentityError, match="token store malformed"):
        load_token_store("{not valid json", None)


def test_load_token_store_malformed_file_json_fails_closed(tmp_path):
    file_path = tmp_path / "tokens.json"
    file_path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(IdentityError, match="token store malformed"):
        load_token_store(None, str(file_path))


def test_load_token_store_non_object_json_fails_closed():
    with pytest.raises(IdentityError, match="expected"):
        load_token_store('["a", "b"]', None)


@pytest.mark.parametrize("bad_store", ['{"abc123": 42}', '{"abc123": null}', '{"abc123": [1]}'])
def test_load_token_store_non_string_values_fail_closed(bad_store):
    """Every value must be a string (an email) — a non-string value (int/null/list) is a shape we
    cannot trust, so it fails closed rather than being silently coerced or ignored."""
    with pytest.raises(IdentityError, match="token store malformed"):
        load_token_store(bad_store, None)


def test_load_token_store_success_shape():
    store = load_token_store('{"abc123": "ana@example.com"}', None)
    assert store == {"abc123": "ana@example.com"}


def test_resolve_email_for_token_no_token_fails_closed():
    with pytest.raises(IdentityError, match="no bearer token presented"):
        resolve_email_for_token({}, None)
    with pytest.raises(IdentityError, match="no bearer token presented"):
        resolve_email_for_token({}, "")


def test_resolve_email_for_token_unrecognized_hash_fails_closed_and_never_enumerates():
    """Unlike `resolve_audiences`'s unknown-identity message, this must NEVER name a known email
    or count — the HTTP middleware relies on this to keep the 401 body generic."""
    store = {hash_token("real-token"): "ana@example.com"}
    with pytest.raises(IdentityError, match="token not recognized") as exc_info:
        resolve_email_for_token(store, "wrong-token")
    assert "ana@example.com" not in str(exc_info.value)


def test_resolve_email_for_token_success():
    store = {hash_token("real-token"): "ana@example.com"}
    assert resolve_email_for_token(store, "real-token") == "ana@example.com"
