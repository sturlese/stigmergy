"""`stigmergy.slack.identity` — Slack profile email -> `ops/identities.json`, fail closed. Every
defense gets a benign twin: an unmapped user refused AND a mapped one served, in this same file.
"""
import asyncio
import json

import pytest

from stigmergy.slack.gateway import FakeSlackGateway
from stigmergy.slack.identity import (
    ForeignTeam,
    NoAccess,
    Resolved,
    TransientFailure,
    UsersInfoCache,
    is_configured_workspace,
    is_ignorable_event,
    resolve_slack_identity,
)

TEAM = "T1"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def identities_path(tmp_path):
    path = tmp_path / "identities.json"
    path.write_text(json.dumps({"steward@example.com": ["brain-admins"], "ana@example.com": ["finance"]}))
    return str(path)


def _resolve(gw, cache, identities_path, *, slack_user_id, event_team_id=TEAM,
            configured_team_id=TEAM):
    return _run(resolve_slack_identity(gw, cache, identities_path=identities_path,
                                       configured_team_id=configured_team_id,
                                       event_team_id=event_team_id, slack_user_id=slack_user_id))


# ── is_ignorable_event: bot/app/workflow/self, before identity is even attempted ─────────────────
def test_a_bot_message_is_ignorable():
    assert is_ignorable_event({"bot_id": "B1", "user": "U1"}, bot_user_id="UBOT")


def test_an_app_message_is_ignorable():
    assert is_ignorable_event({"app_id": "A1", "user": "U1"}, bot_user_id="UBOT")


def test_a_bot_message_subtype_is_ignorable():
    assert is_ignorable_event({"subtype": "bot_message", "user": "U1"}, bot_user_id="UBOT")


def test_the_bots_own_message_is_ignorable():
    assert is_ignorable_event({"user": "UBOT"}, bot_user_id="UBOT")


def test_an_event_with_no_user_at_all_is_ignorable():
    assert is_ignorable_event({}, bot_user_id="UBOT")


def test_an_ordinary_human_event_is_not_ignorable():
    assert not is_ignorable_event({"user": "U1"}, bot_user_id="UBOT")


# ── the benign twin: mapped vs unmapped ────────────────────────────────────────────────────────
def test_a_mapped_email_resolves_to_its_audiences(identities_path):
    gw = FakeSlackGateway()
    gw.seed_user("U_ANA", "ana@example.com")
    result = _resolve(gw, UsersInfoCache(), identities_path, slack_user_id="U_ANA")
    assert result == Resolved(email="ana@example.com", audiences=frozenset({"finance"}))


def test_an_unrestricted_identity_resolves_to_none_audiences(identities_path):
    gw = FakeSlackGateway()
    gw.seed_user("U_STEWARD", "steward@example.com")
    result = _resolve(gw, UsersInfoCache(), identities_path, slack_user_id="U_STEWARD")
    assert result == Resolved(email="steward@example.com", audiences=None)


def test_a_scoped_and_an_unrestricted_identity_get_DIFFERENT_results_over_the_same_lookup(identities_path):
    gw = FakeSlackGateway()
    gw.seed_user("U_ANA", "ana@example.com")
    gw.seed_user("U_STEWARD", "steward@example.com")
    cache = UsersInfoCache()
    ana = _resolve(gw, cache, identities_path, slack_user_id="U_ANA")
    steward = _resolve(gw, cache, identities_path, slack_user_id="U_STEWARD")
    assert ana.audiences != steward.audiences


def test_no_email_set_on_the_slack_profile_is_no_access(identities_path):
    gw = FakeSlackGateway()
    gw.seed_user("U_NOBODY", None)
    assert _resolve(gw, UsersInfoCache(), identities_path, slack_user_id="U_NOBODY") == NoAccess()


def test_an_email_the_identities_file_does_not_recognize_is_no_access(identities_path):
    gw = FakeSlackGateway()
    gw.seed_user("U_STRANGER", "stranger@example.com")
    assert _resolve(gw, UsersInfoCache(), identities_path, slack_user_id="U_STRANGER") == NoAccess()


def test_an_empty_identities_path_is_also_no_access_not_an_exception(identities_path):
    gw = FakeSlackGateway()
    gw.seed_user("U_X", "x@example.com")
    assert _resolve(gw, UsersInfoCache(), "", slack_user_id="U_X") == NoAccess()


# ── ruling 1: a transient API failure is NOT an unmapped user ────────────────────────────────────
def test_a_transient_users_info_failure_is_distinguished_from_no_access(identities_path):
    gw = FakeSlackGateway()
    gw.fail_users_info.add("U_FLAKY")
    result = _resolve(gw, UsersInfoCache(), identities_path, slack_user_id="U_FLAKY")
    assert isinstance(result, TransientFailure)
    assert not isinstance(result, NoAccess)


def test_a_legitimate_mapped_user_still_resolves_after_the_api_recovers(identities_path):
    """The benign twin of the transient-failure test: the SAME already-mapped person, once the API
    is healthy again, is served normally — not punished by having been asked once during an
    outage."""
    gw = FakeSlackGateway()
    gw.seed_user("U_ANA", "ana@example.com")
    cache = UsersInfoCache()
    gw.fail_users_info.add("U_ANA")
    assert isinstance(_resolve(gw, cache, identities_path, slack_user_id="U_ANA"), TransientFailure)
    gw.fail_users_info.discard("U_ANA")
    result = _resolve(gw, cache, identities_path, slack_user_id="U_ANA")
    assert result == Resolved(email="ana@example.com", audiences=frozenset({"finance"}))


# ── ruling 2: cross-workspace mentions are silent ────────────────────────────────────────────────
def test_a_foreign_team_id_is_a_distinct_silent_outcome(identities_path):
    gw = FakeSlackGateway()
    gw.seed_user("U_STRANGER", "stranger@other-workspace.com")
    result = _resolve(gw, UsersInfoCache(), identities_path, slack_user_id="U_STRANGER",
                      event_team_id="T_OTHER", configured_team_id=TEAM)
    assert result == ForeignTeam("T_OTHER")


# ── risk 3: the cache never remembers a NEGATIVE result ──────────────────────────────────────────
def test_the_ttl_cache_never_short_circuits_a_user_who_becomes_mapped(identities_path):
    """Risk 3, in full: a fresh hire's Slack profile has no email yet, so nothing is cached (a
    negative result is never stored) — their VERY NEXT question, once the profile (and
    `identities.json`) catch up, resolves correctly instead of being served a stale "no email"
    from the cache."""
    gw = FakeSlackGateway()
    gw.seed_user("U_NEW", None)   # no email yet
    cache = UsersInfoCache()
    assert _resolve(gw, cache, identities_path, slack_user_id="U_NEW") == NoAccess()
    assert cache.get(TEAM, "U_NEW") is None   # the negative result was never cached

    gw.seed_user("U_NEW", "ana@example.com")   # the profile gets an email already IN identities.json
    result = _resolve(gw, cache, identities_path, slack_user_id="U_NEW")
    assert result == Resolved(email="ana@example.com", audiences=frozenset({"finance"}))
    assert cache.get(TEAM, "U_NEW") == "ana@example.com"   # NOW the positive result is cached


def test_the_ttl_cache_stores_only_the_positive_lookup_and_expires(identities_path):
    gw = FakeSlackGateway()
    gw.seed_user("U_ANA", "ana@example.com")
    clock = {"t": 0.0}
    cache = UsersInfoCache(ttl_seconds=10, clock=lambda: clock["t"])
    _resolve(gw, cache, identities_path, slack_user_id="U_ANA")
    assert cache.get(TEAM, "U_ANA") == "ana@example.com"
    clock["t"] = 11.0
    assert cache.get(TEAM, "U_ANA") is None   # expired


def test_the_cache_never_stores_an_empty_email():
    cache = UsersInfoCache()
    cache.put(TEAM, "U1", "")
    assert cache.get(TEAM, "U1") is None


# ── keyed on (team_id, slack_user_id), not slack_user_id alone ─────────────────────────────────
def test_the_cache_never_leaks_an_email_across_teams_for_the_same_slack_user_id():
    """A Slack user id is only meaningful within its own workspace's id space — a cache keyed on
    the id alone would let a lookup cached under one team answer for another team's identically-
    numbered user. Defense in depth: this bot serves one configured workspace today, but the cache
    itself must not assume that forever."""
    cache = UsersInfoCache()
    cache.put("T1", "U1", "ana@team1.example.com")
    cache.put("T2", "U1", "bob@team2.example.com")
    assert cache.get("T1", "U1") == "ana@team1.example.com"
    assert cache.get("T2", "U1") == "bob@team2.example.com"
    assert cache.get("T3", "U1") is None   # a third, never-seen team gets nothing


def test_the_cache_is_bounded_and_evicts_the_oldest_entry_on_insert():
    cache = UsersInfoCache(max_entries=2)
    cache.put("T1", "U1", "one@example.com")
    cache.put("T1", "U2", "two@example.com")
    cache.put("T1", "U3", "three@example.com")   # over the bound — the oldest (U1) is evicted
    assert cache.get("T1", "U1") is None
    assert cache.get("T1", "U2") == "two@example.com"
    assert cache.get("T1", "U3") == "three@example.com"
    assert len(cache._entries) == 2


# ── the display-name map: the SAME properties as the email map, on its own third store ─────────
def test_the_display_name_cache_stores_only_the_positive_lookup_and_expires():
    clock = {"t": 0.0}
    cache = UsersInfoCache(ttl_seconds=10, clock=lambda: clock["t"])
    cache.put_display_name("T1", "U_ANA", "Ana")
    assert cache.get_display_name("T1", "U_ANA") == "Ana"
    clock["t"] = 11.0
    assert cache.get_display_name("T1", "U_ANA") is None   # expired


def test_the_display_name_cache_never_stores_an_empty_name():
    cache = UsersInfoCache()
    cache.put_display_name("T1", "U1", "")
    assert cache.get_display_name("T1", "U1") is None


def test_the_display_name_cache_is_keyed_on_team_and_user_not_user_alone():
    cache = UsersInfoCache()
    cache.put_display_name("T1", "U1", "Ana (Team 1)")
    cache.put_display_name("T2", "U1", "Bob (Team 2)")
    assert cache.get_display_name("T1", "U1") == "Ana (Team 1)"
    assert cache.get_display_name("T2", "U1") == "Bob (Team 2)"
    assert cache.get_display_name("T3", "U1") is None


def test_the_display_name_cache_is_bounded_and_evicts_the_oldest_entry_on_insert():
    cache = UsersInfoCache(max_entries=2)
    cache.put_display_name("T1", "U1", "One")
    cache.put_display_name("T1", "U2", "Two")
    cache.put_display_name("T1", "U3", "Three")   # over the bound — the oldest (U1) is evicted
    assert cache.get_display_name("T1", "U1") is None
    assert cache.get_display_name("T1", "U2") == "Two"
    assert cache.get_display_name("T1", "U3") == "Three"


def test_the_display_name_cache_is_a_separate_bound_from_the_email_cache():
    """A THIRD map, not a bigger cached value on `_entries` — filling it must not evict, or be
    evicted by, the email lookups `resolve_slack_identity` depends on."""
    cache = UsersInfoCache(max_entries=1)
    cache.put("T1", "U_EMAIL", "email@example.com")
    cache.put_display_name("T1", "U_NAME", "Some Name")
    assert cache.get("T1", "U_EMAIL") == "email@example.com"
    assert cache.get_display_name("T1", "U_NAME") == "Some Name"


def test_resolve_slack_identity_populates_the_display_name_cache_as_a_side_effect(identities_path):
    """The zero-extra-cost path this cache shape exists to buy: the SAME `users.info` response
    that resolves the email also fills the display-name map, so `capture._display_name`'s later
    lookup for this identical (team, user) never needs a second `users.info` call."""
    gw = FakeSlackGateway()
    gw.seed_user("U_ANA", "ana@example.com", display_name="Ana")
    cache = UsersInfoCache()
    _resolve(gw, cache, identities_path, slack_user_id="U_ANA")
    assert cache.get_display_name(TEAM, "U_ANA") == "Ana"


def test_resolve_slack_identity_never_caches_an_empty_display_name(identities_path):
    gw = FakeSlackGateway()
    gw.seed_user("U_ANA", "ana@example.com")   # no display_name seeded
    cache = UsersInfoCache()
    _resolve(gw, cache, identities_path, slack_user_id="U_ANA")
    assert cache.get_display_name(TEAM, "U_ANA") is None


# ── is_configured_workspace: the cheap synchronous half of the fail-closed workspace check ─────
def test_is_configured_workspace_true_when_the_event_team_matches():
    assert is_configured_workspace("T1", "T1") is True


def test_is_configured_workspace_false_when_the_event_team_differs():
    assert is_configured_workspace("T_OTHER", "T1") is False


def test_is_configured_workspace_fails_closed_on_an_absent_event_team():
    assert is_configured_workspace("", "T1") is False
    assert is_configured_workspace(None, "T1") is False


# ── an absent event team_id fails CLOSED, not open ─────────────────────────────────────────────
def test_an_absent_event_team_id_fails_closed_not_open(identities_path):
    """The old guard (`if configured_team_id and event_team_id and event_team_id !=
    configured_team_id`) skipped the check ENTIRELY when `event_team_id` was falsy — an
    Enterprise Grid org-wide install (or any caller that failed to source one) would then be
    treated as the configured workspace by default. A missing team fact must be untrusted, the
    same as a mismatched one."""
    gw = FakeSlackGateway()
    gw.seed_user("U_X", "steward@example.com")   # a REGISTERED identity — proves this isn't NoAccess
    result = _resolve(gw, UsersInfoCache(), identities_path, slack_user_id="U_X", event_team_id="")
    assert isinstance(result, ForeignTeam)
    assert not isinstance(result, Resolved)


def test_a_none_event_team_id_also_fails_closed(identities_path):
    gw = FakeSlackGateway()
    gw.seed_user("U_X", "steward@example.com")
    result = _resolve(gw, UsersInfoCache(), identities_path, slack_user_id="U_X",
                      event_team_id=None)
    assert isinstance(result, ForeignTeam)
