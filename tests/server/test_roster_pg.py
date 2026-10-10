"""The Postgres roster: people, groups, tokens, sessions, and channels, with the rules that keep the
backoffice administrable and every credential revocable on the next request."""
import datetime as dt

import pytest

from stigmergy.server import roster
from stigmergy.server.errors import IdentityError
from stigmergy.server.identity import UNRESTRICTED_GROUP, Principal
from tests.roster import reset_roster
from tests.server.conftest import connect_or_skip


@pytest.fixture
def conn():
    connection = connect_or_skip()
    reset_roster(connection)
    roster.create_group(connection, "finance", "Money", actor="test")
    roster.create_group(connection, "product", "", actor="test")
    yield connection
    reset_roster(connection)
    connection.close()


def _admin(conn, subject="ana@example.com"):
    return roster.create_principal(conn, {"subject": subject, "display_name": "Ana", "kind": "person",
                                          "is_admin": True, "groups": [UNRESTRICTED_GROUP],
                                          "default_audience": ["public"]}, actor="test")


def _member(conn, subject="bob@example.com", groups=("finance",), **extra):
    return roster.create_principal(conn, {"subject": subject, "display_name": "Bob",
                                          "groups": list(groups), "default_audience": ["public"],
                                          **extra}, actor="test")


def test_a_token_resolves_its_principal_until_revoked(conn):
    _admin(conn)
    bob = _member(conn)
    grant = roster.issue_token(conn, bob.subject, "laptop", actor="ana@example.com")
    assert grant.token.startswith("stg_") and len(grant.token) == 47
    who, token_id = roster.principal_for_token(conn, grant.token)
    assert (who.subject, who.audiences, token_id) == ("bob@example.com", ("finance",), grant.id)
    roster.revoke_token(conn, grant.id)
    with pytest.raises(IdentityError, match=roster.UNKNOWN):
        roster.principal_for_token(conn, grant.token)


def test_an_expired_token_and_a_disabled_person_read_like_an_unknown_token(conn):
    _admin(conn)
    bob = _member(conn)
    grant = roster.issue_token(conn, bob.subject, "ci", actor="t",
                               expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1))
    with conn.cursor() as cur:
        cur.execute("UPDATE roster_tokens SET expires_at = now() - interval '1 second'")
    errors = []
    for token in (grant.token, "stg_" + "x" * 43):
        with pytest.raises(IdentityError) as error:
            roster.principal_for_token(conn, token)
        errors.append(str(error.value))
    live = roster.issue_token(conn, bob.subject, "second", actor="t")
    roster.set_disabled(conn, bob.subject, True, actor="t")
    with pytest.raises(IdentityError) as error:
        roster.principal_for_token(conn, live.token)
    errors.append(str(error.value))
    assert len(set(errors)) == 1


def test_a_group_change_applies_on_the_next_lookup(conn):
    _admin(conn)
    bob = _member(conn)
    grant = roster.issue_token(conn, bob.subject, "laptop", actor="t")
    roster.update_principal(conn, bob.subject, {"display_name": "Bob", "groups": ["product"],
                                                "default_audience": ["public"]}, actor="t")
    assert roster.principal_for_token(conn, grant.token)[0].audiences == ("product",)


def test_validation_refuses_unknown_groups_and_foreign_defaults(conn):
    with pytest.raises(roster.RosterRefused, match="does not exist"):
        _member(conn, groups=("legal",))
    with pytest.raises(roster.RosterRefused, match="default layer must be"):
        _member(conn, default_audience=["product"])
    with pytest.raises(roster.RosterRefused, match="default layer is required"):
        _member(conn, default_audience=None)
    with pytest.raises(roster.RosterRefused, match="default layer is required"):
        _member(conn, default_audience=["finance", "product"], groups=("finance", "product"))
    assert _member(conn, subject="carla@example.com", default_audience=["finance"]).default_layer == "finance"
    with pytest.raises(roster.RosterRefused, match="only a person"):
        _member(conn, kind="agent", is_admin=True)
    with pytest.raises(roster.RosterRefused, match="email"):
        _member(conn, subject="not-an-email")


def test_the_last_administrator_cannot_be_removed_in_any_way(conn):
    ana = _admin(conn)
    grant = roster.issue_token(conn, ana.subject, "only", actor="t")
    for action in (
        lambda: roster.update_principal(conn, ana.subject, {"is_admin": False}, actor="t"),
        lambda: roster.set_disabled(conn, ana.subject, True, actor="t"),
        lambda: roster.delete_principal(conn, ana.subject),
    ):
        with pytest.raises(roster.RosterRefused, match="last administrator"):
            action()
    _admin(conn, "carla@example.com")
    roster.revoke_token(conn, grant.id)
    roster.set_disabled(conn, ana.subject, True, actor="t")


def test_a_session_ends_with_sign_out_token_revocation_disabling_or_idleness(conn):
    _admin(conn)
    bob = _member(conn)
    for end in ("close", "revoke", "disable", "idle"):
        roster.set_disabled(conn, bob.subject, False, actor="t")
        grant = roster.issue_token(conn, bob.subject, end, actor="t")
        value, session = roster.open_session(conn, grant.token)
        assert roster.session_principal(conn, value).principal.subject == bob.subject
        if end == "close":
            roster.close_session(conn, value)
        elif end == "revoke":
            roster.revoke_token(conn, grant.id)
        elif end == "disable":
            roster.set_disabled(conn, bob.subject, True, actor="t")
        else:
            with conn.cursor() as cur:
                cur.execute("UPDATE roster_sessions SET last_seen_at = now() - interval '9 hours'")
        with pytest.raises(IdentityError, match=roster.UNKNOWN):
            roster.session_principal(conn, value)


def test_a_session_value_is_stored_only_as_a_digest(conn):
    _admin(conn)
    grant = roster.issue_token(conn, "ana@example.com", "laptop", actor="t")
    value, _session = roster.open_session(conn, grant.token)
    with conn.cursor() as cur:
        cur.execute("SELECT sha256 FROM roster_sessions")
        stored = [row[0] for row in cur.fetchall()]
        cur.execute("SELECT sha256 FROM roster_tokens")
        tokens = [row[0] for row in cur.fetchall()]
    assert value not in stored and grant.token not in tokens
    assert all(len(digest) == 64 for digest in stored + tokens)


def test_a_group_in_use_cannot_be_deleted_and_the_unrestricted_group_never(conn):
    _indexed(conn)
    _member(conn)
    with pytest.raises(roster.RosterRefused, match="in use"):
        roster.delete_group(conn, "finance")
    with pytest.raises(roster.RosterRefused, match="unrestricted"):
        roster.delete_group(conn, UNRESTRICTED_GROUP)
    roster.delete_group(conn, "product")


def test_channels_map_to_known_groups_or_organization_wide(conn):
    roster.set_channel(conn, "c0123456789", ["finance"], actor="t")
    roster.set_channel(conn, "C0999999999", None, actor="t")
    assert roster.channel_map(conn) == {"C0123456789": ("finance",), "C0999999999": None}
    with pytest.raises(roster.RosterRefused):
        roster.set_channel(conn, "C0123456789", [UNRESTRICTED_GROUP], actor="t")
    with pytest.raises(roster.RosterRefused):
        roster.delete_group(conn, "finance")


def test_the_import_loads_people_channels_and_token_digests(conn):
    principals = {
        "Ana@Example.com": Principal("Ana@Example.com", "Ana", (UNRESTRICTED_GROUP,), None),
        "bot@example.com": Principal("bot@example.com", "Bot", ("research",), None),
    }
    legacy = "legacy-plain-token"
    from stigmergy.server.identity import hash_token
    report = roster.import_roster(conn, principals=principals, channels={"C0123456789": ("research",)},
                                  token_store={hash_token(legacy): "ana@example.com"},
                                  admins={"ana@example.com"}, agents={"bot@example.com"}, actor="import")
    assert report == {"principals": 2, "tokens": 1, "channels": 1, "groups": 2}
    who, _ = roster.principal_for_token(conn, legacy)
    assert (who.subject, who.is_admin, who.kind) == ("ana@example.com", True, "person")
    assert roster.principal(conn, "bot@example.com").kind == "agent"
    assert "research" in roster.known_groups(conn)


def test_one_token_opens_both_mcp_and_the_backoffice(conn):
    _admin(conn)
    bob = _member(conn)
    grant = roster.issue_token(conn, bob.subject, "laptop", actor="t")

    assert roster.principal_for_token(conn, grant.token)[0].subject == bob.subject
    assert roster.open_session(conn, grant.token)[1].principal.subject == bob.subject


def test_an_agent_token_opens_mcp_and_a_member_session(conn):
    _member(conn, subject="bot@example.com", kind="agent")
    grant = roster.issue_token(conn, "bot@example.com", "runner", actor="t")

    assert roster.principal_for_token(conn, grant.token)[0].kind == "agent"
    value, session = roster.open_session(conn, grant.token)
    assert session.principal.kind == "agent" and not session.principal.is_admin
    assert roster.session_principal(conn, value).principal.subject == "bot@example.com"


def test_a_partial_update_keeps_what_it_omits_and_an_agent_keeps_a_member_session(conn):
    _admin(conn)
    bob = _member(conn)
    grant = roster.issue_token(conn, bob.subject, "browser", actor="t")
    value, _session = roster.open_session(conn, grant.token)

    assert roster.update_principal(conn, bob.subject, {"display_name": "Robert"},
                                   actor="t").kind == "person"
    roster.update_principal(conn, bob.subject, {"kind": "agent"}, actor="t")

    session = roster.session_principal(conn, value)
    assert (session.principal.kind, session.principal.is_admin) == ("agent", False)
    assert roster.principal_for_token(conn, grant.token)[0].kind == "agent"


def test_concurrent_demotions_never_remove_every_administrator(conn):
    import threading

    _admin(conn, "ana@example.com")
    _admin(conn, "carla@example.com")
    barrier = threading.Barrier(2)
    outcomes = []

    def demote(subject):
        other = connect_or_skip()
        try:
            barrier.wait()
            roster.update_principal(other, subject, {"is_admin": False}, actor="t")
            outcomes.append("ok")
        except roster.RosterRefused:
            outcomes.append("refused")
        finally:
            other.close()

    threads = [threading.Thread(target=demote, args=(s,))
               for s in ("ana@example.com", "carla@example.com")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["ok", "refused"]
    assert roster.admin_count(conn) == 1


def test_the_import_runs_once_and_validates_like_an_edit(conn):
    principals = {"ana@example.com": Principal("ana@example.com", "Ana", (UNRESTRICTED_GROUP,), None)}
    roster.import_roster(conn, principals=principals, channels={}, token_store={},
                         admins={"ana@example.com"}, agents=set(), actor="import")
    with pytest.raises(roster.RosterRefused, match="only once"):
        roster.import_roster(conn, principals=principals, channels={}, token_store={},
                             admins={"ana@example.com"}, agents=set(), actor="import")
    reset_roster(conn)
    for bad in (
        {"legacy-name": Principal("legacy-name", "L", (UNRESTRICTED_GROUP,), None)},
        {"ana@example.com": Principal("ana@example.com", "Ana", (UNRESTRICTED_GROUP,),
                                      (UNRESTRICTED_GROUP,))},
    ):
        with pytest.raises(roster.RosterRefused):
            roster.import_roster(conn, principals=bad, channels={}, token_store={},
                                 admins=set(bad), agents=set(), actor="import")
        assert roster.admin_count(conn) == 0


def test_a_group_deleted_concurrently_never_leaves_a_dangling_membership(conn):
    import threading

    _indexed(conn)
    roster.create_group(conn, "ephemeral", "", actor="t")
    barrier = threading.Barrier(2)

    def run(action):
        other = connect_or_skip()
        try:
            barrier.wait()
            action(other)
        except roster.RosterRefused:
            pass
        finally:
            other.close()

    threads = [
        threading.Thread(target=run, args=(lambda c: roster.delete_group(c, "ephemeral"),)),
        threading.Thread(target=run, args=(lambda c: _member(c, groups=("ephemeral",)),)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM roster_principals p WHERE EXISTS (SELECT 1 FROM "
                    "unnest(p.groups) g WHERE g NOT IN (SELECT name FROM roster_groups))")
        assert cur.fetchone()[0] == 0


def test_a_layer_description_is_long_enough_to_route_by(conn):
    roster.create_group(conn, "routing-rule", "x" * roster.MAX_DESCRIPTION_CHARS, actor="test")
    assert roster.MAX_DESCRIPTION_CHARS >= 1000
    with pytest.raises(roster.RosterRefused, match="too long"):
        roster.update_group(conn, "routing-rule", "x" * (roster.MAX_DESCRIPTION_CHARS + 1))
    roster.delete_group(conn, "routing-rule")


def test_groups_are_layers_and_a_channel_files_into_one_of_them(conn):
    for name in ("Deals", "public", "brain-admins", "layers", "x", "a_b"):
        with pytest.raises(roster.RosterRefused, match="layer name"):
            roster.create_group(conn, name, "", actor="test")
    roster.create_group(conn, "people-ops", "Hiring", actor="test")
    with pytest.raises(roster.RosterRefused, match="exactly one layer"):
        roster.set_channel(conn, "C0123456789", ["finance", "product"], actor="test")
    roster.set_channel(conn, "C0123456789", ["public"], actor="test")
    roster.set_channel(conn, "C0987654321", ["people-ops"], actor="test")
    assert roster.channel_map(conn) == {"C0123456789": None, "C0987654321": ("people-ops",)}


def _indexed(conn):
    """An index that has converged on the repository and holds nothing."""
    from stigmergy.index import health, store

    store.init_schema(conn, dim=256, model="fake-hashed-bow-256", fts_config="english")
    health.record_full_rebuild(conn, "a" * 40, 0)


def test_a_layer_is_deleted_only_when_nothing_can_still_hold_it(conn):
    from stigmergy.capture import evidence
    from stigmergy.capture.schema import Actor, ensure_capture_schema
    from stigmergy.capture.service import CaptureService
    from stigmergy.changes.store import ensure_change_schema
    from stigmergy.index import health

    ensure_change_schema(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM knowledge_changes WHERE commit_sha = %s", ("c" * 40,))
        cur.execute("DROP TABLE IF EXISTS page_fragments")
        cur.execute("DROP TABLE IF EXISTS pages_index")
    with pytest.raises(roster.RosterRefused, match="index"):
        roster.delete_group(conn, "product")
    _indexed(conn)
    health.mark_dirty(conn, "b" * 40)
    with pytest.raises(roster.RosterRefused, match="index"):
        roster.delete_group(conn, "product")
    health.record_full_rebuild(conn, "b" * 40, 0)
    ensure_capture_schema(conn)
    receipt = CaptureService(conn, evidence.MemoryEvidenceStore()).capture_text(
        actor=Actor(subject="bob@example.com", display_name="Bob"), audience=("product",),
        adapter="mcp", text="A queued product note.", idempotency_key="queued-in-product")
    with pytest.raises(roster.RosterRefused, match="capture"):
        roster.delete_group(conn, "product")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM capture_queue WHERE id = %s", (receipt["id"],))
    with conn.cursor() as cur:
        cur.execute("INSERT INTO knowledge_changes (id, trigger, actor, parent_commit_sha, commit_sha, summary, "
                    "manifest, exact_patch_ref, exact_patch_sha256, exact_patch_bytes, created_at) VALUES "
                    "(gen_random_uuid(), 'capture', 'bob@example.com', %s, %s, 'Filed', '[]', 'ref', %s, 0, "
                    "now() + interval '1 minute')", ("b" * 40, "c" * 40, "d" * 64))
    with pytest.raises(roster.RosterRefused, match="index"):
        roster.delete_group(conn, "product")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM knowledge_changes WHERE commit_sha = %s", ("c" * 40,))

    roster.delete_group(conn, "product")
    assert "product" not in roster.known_groups(conn)
