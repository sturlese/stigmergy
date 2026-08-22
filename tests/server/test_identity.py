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


# ── the retired sigil, refused as a VALUE and as a NAME ───────────────────────────────────────
def test_the_sigil_wrapped_in_a_list_is_refused_by_name(tmp_path):
    """The migration foot-gun: an operator told to "write a list" wraps the old sigil. It would
    otherwise resolve to a group nobody can hold — the admin silently loses unrestricted access,
    and at the door files pages nobody can read."""
    path = _write(tmp_path, {"steward": ["*"]})
    with pytest.raises(IdentityError, match="retired unrestricted sigil"):
        resolve_audiences(path, "steward")


def test_a_case_variant_of_the_unrestricted_group_is_refused_not_folded(tmp_path):
    """Refused rather than accepted case-insensitively: folding would WIDEN on a typo, and this is
    the one label whose typo grants the whole corpus. Refusing is the fail-closed direction and it
    says which spelling to write."""
    path = _write(tmp_path, {"steward": ["Brain-Admins"]})
    with pytest.raises(IdentityError, match="differs only in case"):
        resolve_audiences(path, "steward")


def test_the_reservation_is_casefolded(tmp_path):
    """`All` is the same intention as `all` with a different shift key."""
    path = _write(tmp_path, {"ana": ["All"]})
    with pytest.raises(IdentityError, match="reserved group"):
        resolve_audiences(path, "ana")


# ── the group-name grammar: narrow, because these names reach page frontmatter ────────────────
def test_a_group_name_carrying_a_newline_is_refused(tmp_path):
    """A group name is stamped into YAML frontmatter as an access label and, since ADR 045 D2, can
    arrive from a model through `brain_submit(audience=…)`. A newline there is a page-contract
    injection, and no legitimate group name needs one."""
    path = _write(tmp_path, {"ana": ["fin\nance"]})
    with pytest.raises(IdentityError, match="invalid group"):
        resolve_audiences(path, "ana")


def test_a_group_name_over_the_length_ceiling_is_refused(tmp_path):
    path = _write(tmp_path, {"ana": ["a" * 65]})
    with pytest.raises(IdentityError, match="invalid group"):
        resolve_audiences(path, "ana")


def test_too_many_groups_is_refused(tmp_path):
    path = _write(tmp_path, {"ana": [f"g{n}" for n in range(33)]})
    with pytest.raises(IdentityError, match="over the ceiling"):
        resolve_audiences(path, "ana")


def test_the_benign_twin_ordinary_group_names_pass_the_grammar(tmp_path):
    """The specificity half of every rule above: the shapes a real roster uses must all resolve.
    A rule that has never let anything through has been measured for sensitivity only."""
    path = _write(tmp_path, {"ana": ["finance", "sales-emea", "eng.platform", "team_42", "g1"]})
    assert resolve_audiences(path, "ana") == (
        "finance", "sales-emea", "eng.platform", "team_42", "g1")


# ── a refusal that names a replacement RUNS that replacement ──────────────────────────────────
def test_a_bare_label_that_could_not_be_a_group_is_not_suggested_back(tmp_path):
    """The naive message answered `"finance,sales"` with `write ["finance,sales"]`, which the
    comma rule then refuses — a message containing a command is an executable promise, and that
    one could not be kept. The suggestion is checked before it is offered."""
    path = _write(tmp_path, {"ana": "finance,sales"})
    with pytest.raises(IdentityError) as caught:
        resolve_audiences(path, "ana")
    assert "write [" not in str(caught.value), str(caught.value)


def test_a_bare_label_that_COULD_be_a_group_still_gets_its_suggestion(tmp_path):
    """The benign twin: the suggestion is withheld only when it would not work."""
    path = _write(tmp_path, {"ana": "finance"})
    with pytest.raises(IdentityError, match=r'write \["finance"\] instead'):
        resolve_audiences(path, "ana")


# ── the whole file is validated, not only the entry looked up ──────────────────────────────────
def test_a_repeated_principal_is_refused_rather_than_last_win(tmp_path):
    """`json.loads` keeps the last occurrence silently. A diff appending a wide line far from an
    existing narrow one reads, in review, as one added line beside an unchanged one — and the push
    webhook makes it live within seconds."""
    path = _write(tmp_path, '{"ana": ["finance"], "ana": ["brain-admins"]}')
    with pytest.raises(IdentityError, match="appears more than once"):
        resolve_audiences(path, "ana")


def test_two_principals_differing_only_in_case_are_refused(tmp_path):
    """Lookups are exact, so these are two principals that look identical in review and grant
    differently — the file's smuggling surface."""
    path = _write(tmp_path, '{"Ana@x.com": ["finance"], "ana@x.com": ["brain-admins"]}')
    with pytest.raises(IdentityError, match="differ only in case"):
        resolve_audiences(path, "ana@x.com")


def test_the_benign_twin_two_genuinely_different_principals_resolve(tmp_path):
    path = _write(tmp_path, {"ana@x.com": ["finance"], "bob@x.com": ["brain-admins"]})
    assert resolve_audiences(path, "ana@x.com") == ("finance",)
    assert resolve_audiences(path, "bob@x.com") is None


def test_a_comment_key_whose_value_is_a_group_list_is_refused(tmp_path):
    """The ambiguous case: somebody whose entry silently does nothing. `_marc@example.com` is a
    valid email and `stigmergy-issue-token` will issue for it, so "no principal begins with `_`"
    is a rule this file states rather than a fact about the world."""
    path = _write(tmp_path, {"_marc@example.com": ["finance"], "ana": ["finance"]})
    with pytest.raises(IdentityError, match="marks a COMMENT"):
        resolve_audiences(path, "ana")



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
