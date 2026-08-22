"""Identity resolution — fail-closed on every path: the audience resolver, and the per-request
token store (`hash_token`, `load_token_store`, `resolve_email_for_token`)."""
import json

import pytest

from stigmergy.server.errors import IdentityError
from stigmergy.server.identity import (
    audiences_from_text,
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


def test_brain_admins_membership_resolves_to_unrestricted(tmp_path):
    """The one unrestricted spelling (ADR 045 D7): a GROUP, not a sigil, because the identity
    provider that replaces this file has groups and has no sigils."""
    path = _write(tmp_path, {"steward": ["brain-admins"]})
    assert resolve_audiences(path, "steward") is None


def test_brain_admins_beside_other_groups_still_resolves_to_unrestricted(tmp_path):
    path = _write(tmp_path, {"steward": ["finance", "brain-admins"]})
    assert resolve_audiences(path, "steward") is None


def test_scoped_list_resolves_to_tuple(tmp_path):
    path = _write(tmp_path, {"ana": ["finance"], "bob": ["sales", "leadership"]})
    assert resolve_audiences(path, "ana") == ("finance",)
    assert resolve_audiences(path, "bob") == ("sales", "leadership")


def test_no_groups_at_all_reads_open_pages_and_nothing_else(tmp_path):
    """A PRINCIPAL with no groups is not the `acl: []` of a PAGE (which means nobody): it is an
    authenticated reader who holds nothing, so `visible()` shows it every page carrying no label
    and no other. ADR 045 D9 keeps the two facts apart, and this is the one that is about a
    person."""
    path = _write(tmp_path, {"newcomer": []})
    assert resolve_audiences(path, "newcomer") == ()


def test_a_repeated_group_is_normalized_away(tmp_path):
    path = _write(tmp_path, {"ana": ["finance", " finance ", "sales"]})
    assert resolve_audiences(path, "ana") == ("finance", "sales")


# ── the two retired spellings: refused by name, with the line to write instead ─────────────────
# "A message containing a command is an executable promise": each refusal below names a
# replacement, and its twin RUNS that replacement.

def test_the_star_spelling_is_refused_and_names_the_line_to_write(tmp_path):
    path = _write(tmp_path, {"steward": "*"})
    with pytest.raises(IdentityError, match=r'write \["brain-admins"\] instead'):
        resolve_audiences(path, "steward")


def test_the_line_the_star_refusal_names_actually_resolves(tmp_path):
    """The promise, run: what the refusal above tells an operator to write is what works."""
    path = _write(tmp_path, {"steward": ["brain-admins"]})
    assert resolve_audiences(path, "steward") is None


def test_the_bare_label_spelling_is_refused_and_names_the_line_to_write(tmp_path):
    path = _write(tmp_path, {"ana": "finance"})
    with pytest.raises(IdentityError, match=r'write \["finance"\] instead'):
        resolve_audiences(path, "ana")


def test_the_line_the_bare_label_refusal_names_actually_resolves(tmp_path):
    path = _write(tmp_path, {"ana": ["finance"]})
    assert resolve_audiences(path, "ana") == ("finance",)


# ── `all` is a reserved word, because open is the ABSENCE of a label ───────────────────────────
def test_the_reserved_group_all_is_refused(tmp_path):
    """A page labelled `[all]` would be restricted to whoever holds a group called `all` — the
    opposite of what anybody writing it means. ADR 045 D7."""
    path = _write(tmp_path, {"ana": ["all"]})
    with pytest.raises(IdentityError, match="reserved group 'all'"):
        resolve_audiences(path, "ana")


def test_a_group_named_like_all_but_not_all_is_fine(tmp_path):
    """The benign twin: the reservation is one exact name, not a substring match on it."""
    path = _write(tmp_path, {"ana": ["all-hands", "allies"]})
    assert resolve_audiences(path, "ana") == ("all-hands", "allies")


# ── the whole file is validated, not only the entry looked up ──────────────────────────────────
def test_a_malformed_NEIGHBOUR_refuses_the_lookup_too(tmp_path):
    """An access-scoping file the server cannot make sense of must never answer for the entry that
    happened to parse. Before ADR 045 each reader validated only the value it wanted."""
    path = _write(tmp_path, {"ana": ["finance"], "bob": {"nested": True}})
    with pytest.raises(IdentityError, match="malformed group list"):
        resolve_audiences(path, "ana")


def test_a_comment_key_is_dropped_rather_than_parsed_as_a_principal(tmp_path):
    """`_`-prefixed keys let an operator say which channel `C0BL6QH7AQN` is, in the file itself.
    Dropped, not exempted: looking the comment up is an `unknown` refusal like any other."""
    path = _write(tmp_path, {"_comment": "the roster", "ana": ["finance"]})
    assert resolve_audiences(path, "ana") == ("finance",)
    with pytest.raises(IdentityError, match="unknown identity"):
        resolve_audiences(path, "_comment")


def test_no_identity_given_fails_closed(tmp_path):
    path = _write(tmp_path, {"steward": ["brain-admins"]})
    with pytest.raises(IdentityError, match="no identity given"):
        resolve_audiences(path, None)


def test_unknown_identity_fails_closed_and_names_known(tmp_path):
    path = _write(tmp_path, {"steward": ["brain-admins"], "ana": ["finance"]})
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
    with pytest.raises(IdentityError, match="malformed"):
        resolve_audiences(path, "steward")


def test_non_object_json_fails_closed(tmp_path):
    path = _write(tmp_path, ["steward", "ana"])
    with pytest.raises(IdentityError, match="malformed"):
        resolve_audiences(path, "steward")


def test_malformed_audience_value_fails_closed(tmp_path):
    # an identity mapped to something that is not a list (here: a bool) must never silently open
    # — fail closed with an actionable message.
    path = _write(tmp_path, {"steward": True})
    with pytest.raises(IdentityError, match="malformed group list"):
        resolve_audiences(path, "steward")


def test_null_audience_value_fails_closed(tmp_path):
    path = _write(tmp_path, {"steward": None})
    with pytest.raises(IdentityError, match="malformed group list"):
        resolve_audiences(path, "steward")


def test_a_group_name_that_is_not_a_string_fails_closed(tmp_path):
    path = _write(tmp_path, {"steward": ["finance", 7]})
    with pytest.raises(IdentityError, match="not a string"):
        resolve_audiences(path, "steward")


def test_a_group_name_carrying_a_comma_fails_closed(tmp_path):
    """A label list is CSV-serialized on at least one road, so one comma inside a name would
    silently become two groups at enforcement time."""
    path = _write(tmp_path, {"steward": ["finance,leadership"]})
    with pytest.raises(IdentityError, match="invalid group"):
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


# ── the text road: the snapshot's own parse, one function under the file road ─────────────────
def test_audiences_from_text_resolves_exactly_as_the_file_road_does(tmp_path):
    """One parse under both roads, asserted as behaviour: whatever the file road answers, the
    text road answers for the same bytes — scope, unrestricted and nobody alike."""
    data = {"steward": ["brain-admins"], "ana": ["finance"], "ghost-scope": []}
    path = _write(tmp_path, data)
    text = json.dumps(data)

    for who in data:
        assert audiences_from_text(text, who, origin="snapshot") == resolve_audiences(path, who)


def test_an_EMPTY_snapshot_text_resolves_nobody_never_everybody():
    """**The trap the `is not None` fallback order sets, closed.** `store.read_ops_file` returns
    `""` for a snapshot row holding empty text — falsy, and a truthiness-based fallback would
    silently hand resolution to the baked FILE, which is exactly the stale roster the snapshot
    exists to replace. The empty text reaches this parser and fails CLOSED: malformed JSON, an
    `IdentityError`, nobody resolved."""
    with pytest.raises(IdentityError, match="malformed"):
        audiences_from_text("", "steward", origin="snapshot")


def test_a_snapshot_of_garbage_fails_closed_with_the_snapshot_named():
    with pytest.raises(IdentityError, match="snapshot"):
        audiences_from_text("{not json", "steward", origin="snapshot")


def test_an_unknown_identity_in_the_snapshot_fails_closed():
    with pytest.raises(IdentityError, match="unknown identity"):
        audiences_from_text('{"ana": ["finance"]}', "ghost@example.com", origin="snapshot")
