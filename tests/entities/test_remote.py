"""`entities.remote.decide_via_clone` — the server-driven decision's own seam (ADR 030 D3), unit-level:
credential resolution (`_authenticated_url`) and the clone/cleanup orchestration, both driven
without a real GitHub App and without a real network call — `githubapp.installation_token`'s own
`opener=` seam is what makes the real thing keyless (`tests/librarian/test_githubapp.py`); this
suite instead monkeypatches `installation_token` itself, because `decide_via_clone` does not thread
an `opener` through (there is exactly one caller, and it is not a place that needs one).

The end-to-end mint (a real bare remote, a real push, the registry regenerated) is proven in
`tests/server/test_review.py` — the one real caller. What is missing there, and what this file
closes, is the credential math no server-side test exercises without a real `https://` remote and
a real App: the tokenized URL's exact shape, the half-configured-App refusal, and that a failed
mint still cleans up its own throwaway clone.
"""
import logging
import os

import pytest

from stigmergy.entities import remote
from stigmergy.entities.errors import CapabilityUnavailableError, EntityError
from stigmergy.librarian import gitcmd
from stigmergy.librarian.errors import LibrarianConfigError
from tests.entities import conftest as fx

FULL_CREDENTIAL = {
    remote.githubapp.APP_ID_ENV: "123456",
    remote.githubapp.INSTALLATION_ID_ENV: "987654",
    remote.githubapp.PRIVATE_KEY_ENV: "not-a-real-pem-but-configured() never reads it",
}


# ── _authenticated_url ───────────────────────────────────────────────────────────────────────
def test_refuses_when_no_repo_url_is_configured_at_all():
    with pytest.raises(CapabilityUnavailableError, match="STIGMERGY_LIBRARIAN_REPO_URL"):
        remote._authenticated_url("", None)


@pytest.mark.parametrize("url", ["/tmp/some/bare.git", "git://localhost/repo.git",
                                 "file:///tmp/bare.git"])
def test_a_non_https_url_is_returned_unchanged_and_needs_no_credential(url):
    """The test seam ADR 030 asks for directly: a plain path or `git://` remote (every real test
    in this suite, and the composition's own bare remote) authenticates nothing, so `credential`
    is never even inspected — `None` is accepted without touching `githubapp` at all."""
    assert remote._authenticated_url(url, None) == url


def test_https_with_no_credential_at_all_is_refused_naming_the_capability():
    with pytest.raises(CapabilityUnavailableError, match="GitHub App credential"):
        remote._authenticated_url("https://github.com/acme/knowledge.git", None)


def test_https_with_an_empty_credential_mapping_is_refused_the_same_way():
    with pytest.raises(CapabilityUnavailableError, match="GitHub App credential"):
        remote._authenticated_url("https://github.com/acme/knowledge.git", {})


def test_a_half_configured_app_is_a_plain_entity_error_not_capability_unavailable():
    """Distinct from "absent" (`CapabilityUnavailableError`, the ADR 030 D3 posture): one or two
    of the three env vars present means somebody meant to configure this and got it wrong.
    `githubapp.configured` itself raises `LibrarianConfigError` for exactly this shape; this
    module must not let that foreign exception type escape its own boundary (module docstring:
    "raises only entities.errors.EntityError, or a subclass")."""
    partial = {remote.githubapp.APP_ID_ENV: "123456"}
    with pytest.raises(EntityError) as excinfo:
        remote._authenticated_url("https://github.com/acme/knowledge.git", partial)
    assert not isinstance(excinfo.value, CapabilityUnavailableError)
    assert str(excinfo.value) == remote.APP_MISCONFIGURED_MESSAGE


def test_a_half_configured_app_does_not_echo_the_librarian_exceptions_own_text(caplog):
    """OLD BEHAVIOUR: the message was `f"the librarian GitHub App is misconfigured: {ex}"`, so a
    `librarian` exception's own text reached a steward over MCP verbatim — twelve lines below this
    module's rule forbidding exactly that splice. `githubapp` names the private-key FILE PATH in
    one of the `LibrarianConfigError`s reachable here, which made this a server-side filesystem
    disclosure to anyone able to trigger it.

    MOVED, not lost, is the property: the operator still gets the detail, in the log.
    """
    partial = {remote.githubapp.APP_ID_ENV: "123456"}
    with caplog.at_level(logging.ERROR, logger=remote.log.name), pytest.raises(EntityError) as ex:
        remote._authenticated_url("https://github.com/acme/knowledge.git", partial)

    told_the_steward = str(ex.value)
    assert remote.githubapp.PRIVATE_KEY_FILE_ENV not in told_the_steward
    assert "LibrarianConfigError" not in told_the_steward
    assert caplog.records and caplog.records[-1].exc_info, (
        "the detail was dropped rather than moved — an operator now has nothing to debug with")


def test_a_credential_that_cannot_mint_a_token_is_a_plain_entity_error(monkeypatch):
    """The App IS configured but GitHub refuses the token exchange (a revoked installation, a
    rotated key) — an operational fault, not an absent capability, so this is NOT
    `CapabilityUnavailableError` either: the fix is "check the App", not "configure one"."""
    def boom(env):
        raise LibrarianConfigError("GitHub refused an installation token (HTTP 401)")
    monkeypatch.setattr(remote.githubapp, "installation_token", boom)

    with pytest.raises(EntityError) as excinfo:
        remote._authenticated_url("https://github.com/acme/knowledge.git", FULL_CREDENTIAL)
    assert not isinstance(excinfo.value, CapabilityUnavailableError)
    assert str(excinfo.value) == remote.CREDENTIAL_FAULT_MESSAGE


def test_a_token_exchange_fault_moves_githubs_own_words_to_the_log(monkeypatch, caplog):
    """The second half of the same defect. OLD BEHAVIOUR: `f"...: {ex}"` put whatever the token
    exchange said — GitHub's HTTP body included — in front of a steward.

    The benign twin for both is `test_..._is_a_plain_entity_error` above: the steward is still
    told the mint failed and that it was not their approval's fault, which is the only part they
    can act on. A refusal that says nothing at all would be the other way to fail this.
    """
    def boom(env):
        raise LibrarianConfigError("GitHub refused an installation token (HTTP 401)")
    monkeypatch.setattr(remote.githubapp, "installation_token", boom)

    with caplog.at_level(logging.ERROR, logger=remote.log.name), pytest.raises(EntityError) as ex:
        remote._authenticated_url("https://github.com/acme/knowledge.git", FULL_CREDENTIAL)

    assert "HTTP 401" not in str(ex.value)
    assert "HTTP 401" in caplog.text, "the operator lost the one detail that names the fault"


def test_embeds_a_minted_token_into_the_url_in_the_shape_gitcmd_scrubs(monkeypatch):
    """The token-in-URL shape this function builds is EXACTLY what `gitcmd._scrub` already
    redacts from any error message — proven directly against the real regex rather than by
    inspection, so the two halves of this defense cannot silently drift apart."""
    monkeypatch.setattr(remote.githubapp, "installation_token", lambda env: "ghs_stubtoken123")

    url = remote._authenticated_url("https://github.com/acme/knowledge.git", FULL_CREDENTIAL)

    assert url == "https://x-access-token:ghs_stubtoken123@github.com/acme/knowledge.git"
    assert gitcmd._TOKEN_IN_URL.search(url), "not the user:pass@ shape _scrub matches"
    assert "ghs_stubtoken123" not in gitcmd._scrub(url)


# ── decide_via_clone: the clone/cleanup orchestration, the decision itself stubbed out ─────────
def test_decide_via_clone_cleans_up_the_temp_clone_even_when_the_decision_raises(tmp_path, monkeypatch):
    from tests.librarian import support
    env = support.build_repo(str(tmp_path / "git"))
    captured = {}

    def boom(repo, **kwargs):
        captured["repo"] = repo
        captured["author"] = kwargs["author"]
        assert os.path.isdir(repo)
        # the App's identity is configured on the clone BEFORE the decision runs (needed for the
        # bounded rebase-and-retry's own `git rebase`, which a fresh temp clone otherwise has no
        # committer identity for at all).
        name = gitcmd.run("config", "user.name", cwd=repo).stdout.strip()
        email = gitcmd.run("config", "user.email", cwd=repo).stdout.strip()
        assert (name, email) == kwargs["author"]
        raise EntityError("boom from the decision")

    monkeypatch.setattr(remote.decide, "apply", boom)

    with pytest.raises(EntityError, match="boom from the decision"):
        remote.decide_via_clone(env.bare, "main", None, action=lambda repo: None,
                                decided_by="steward@example.com")

    assert captured["author"] == (
        "stigmergy-librarian", "stigmergy-librarian@users.noreply.github.com")
    assert not os.path.exists(captured["repo"]), (
        "the TemporaryDirectory must be gone even though the decision raised")


def test_decide_via_clone_needs_no_credential_against_a_local_bare_remote(tmp_path, monkeypatch):
    """The property the pg suite (`tests/server/test_review.py`) relies on for every decision it
    proves for real: `credential=None` against a local path never even asks `githubapp` whether
    it is configured."""
    from tests.librarian import support
    env = support.build_repo(str(tmp_path / "git"))

    def fail_if_called(*a, **k):
        raise AssertionError("githubapp.configured must not be consulted for a non-https remote")
    monkeypatch.setattr(remote.githubapp, "configured", fail_if_called)
    monkeypatch.setattr(remote.decide, "apply", lambda repo, **kwargs: {"stub": True})

    result = remote.decide_via_clone(env.bare, "main", None, action=lambda repo: None,
                                     decided_by="steward@example.com")

    assert result == {"stub": True}


# ── AUDIT M3: the governance trailer cannot be forged ──────────────────────────────────────────
# ADR 030 D1 makes the trailer half of how `git log` answers "who decided this identity", and the
# knowledge repo's authorship check reads it. MCP and Slack pass a resolved identity, but the
# CONSOLE passes a free-text actor by design (D2 — attribution), so a newline there would inject
# arbitrary lines into the commit message: a second, forged trailer among them.
def test_a_newline_in_the_decider_cannot_forge_a_second_trailer(tmp_path, monkeypatch):
    from tests.librarian import support
    env = support.build_repo(str(tmp_path / "git"))
    seen = {}
    monkeypatch.setattr(remote.decide, "apply",
                        lambda repo, **kw: seen.update(kw) or {"stub": True})
    remote.decide_via_clone(
        env.bare, "main", {}, action=lambda repo: None,
        decided_by="steward@example.com\nDecided-by: mallory@evil.example")

    # git parses a trailer only at the START of a line, so the property that matters is that the
    # value stays on ONE line: the forged text survives as visible content of the single real
    # trailer, never as a second trailer git (or the knowledge repo's authorship check) would read.
    trailer = seen["trailer"]
    assert "\n" not in trailer
    assert len([line for line in trailer.splitlines() if line.startswith("Decided-by:")]) == 1
    assert "mallory@evil.example" in trailer      # kept and visible, never smuggled


def test_an_empty_decider_is_refused_rather_than_committed_blank(tmp_path, monkeypatch):
    from tests.librarian import support
    env = support.build_repo(str(tmp_path / "git"))
    monkeypatch.setattr(remote.decide, "apply", lambda repo, **kw: {"stub": True})
    for blank in ("", "   ", "\n\t"):
        with pytest.raises(EntityError, match="approver"):
            remote.decide_via_clone(env.bare, "main", {}, action=lambda repo: None,
                                    decided_by=blank)


def test_a_librarian_fault_after_the_clone_is_renamed_into_this_packages_vocabulary(
        tmp_path, monkeypatch):
    """OLD BEHAVIOUR: it escaped as `librarian.errors.LibrarianConfigError`, and the module
    docstring's "This module's public seam raises only `entities.errors.EntityError`" was false for
    everything after the clone.

    `gates.ensure_scanner` runs on this exact path (`guard.refuse_secrets` scans what the commit
    will carry) and raises `LibrarianConfigError` when gitleaks is absent. The MCP server is a
    different process from the librarian worker, so a server host without the scanner is the
    ordinary deployment — and `server.review._mint_entity_proposal` catches
    `EntityCapabilityUnavailableError` and `EntityError` only, so that fault came out of a
    steward's Approve as an unmapped exception rather than a refusal naming what to install.
    `GitError` from the push had the identical shape.
    """
    from tests.librarian import support
    env = support.build_repo(str(tmp_path / "git"))

    def scanner_missing(repo, **kwargs):
        raise LibrarianConfigError(
            "the secret scanner '/opt/operator/bin/gitleaks' is not runnable (FileNotFoundError)")

    monkeypatch.setattr(remote.decide, "apply", scanner_missing)

    with pytest.raises(EntityError) as caught:
        remote.decide_via_clone(env.bare, "main", None, action=lambda repo: None,
                                decided_by="steward@example.com")

    assert str(caught.value) == remote.MINT_FAULT_MESSAGE
    # …and the librarian fault's own text does NOT ride along. `server.review` turns this into a
    # `ReviewError` "echoed verbatim over MCP", and `librarian/index.md` forbids putting a
    # filesystem path — or any `str(exception)` — on the wire for a mid-run fault. Both of this
    # seam's real faults carry one: git names the throwaway clone's absolute path, and
    # `ensure_scanner` interpolates the operator-supplied `$STIGMERGY_GITLEAKS_BIN`.
    assert "/opt/operator/bin/gitleaks" not in str(caught.value)
    assert caught.value.__cause__ is not None, "the cause is kept for the log, not for the wire"


def test_an_entity_error_from_the_decision_is_not_rewrapped_by_that_rename(tmp_path, monkeypatch):
    """The benign twin: the rename above catches `LibrarianError`, which `EntityError` is not, so
    a refusal this package words for the STEWARD — a decision on a name nothing proposed, a
    collision, both clean by construction — still reaches the caller as itself and keeps its own
    sentence. What the ladder above it maps is the four types whose sentences are worded for a
    terminal instead (issue #57); `EntityError` itself is deliberately not an arm."""
    from tests.librarian import support
    env = support.build_repo(str(tmp_path / "git"))

    def refuse(repo, **kwargs):
        raise EntityError("--name contains a character which cannot appear in an entity name")

    monkeypatch.setattr(remote.decide, "apply", refuse)

    with pytest.raises(EntityError, match="cannot appear in an entity name"):
        remote.decide_via_clone(env.bare, "main", None, action=lambda repo: None,
                                decided_by="steward@example.com")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# issue #57 — every sentence this module publishes is publishable.
#
# `server.review` echoes an `EntityError` raised here to a steward over MCP verbatim, so a refusal
# composed in this module is wire copy, not a log line. The two properties below are what makes
# that safe, asserted over the constants themselves so a NEW sentence cannot be added without
# meeting them. What reaches the wire in practice is proven at the door
# (`tests/server/test_review.py`); this is the text's own contract.
# ══════════════════════════════════════════════════════════════════════════════════════════════
# DERIVED, never listed: a refusal sentence added to this module joins both properties below by
# existing. A hand-written list is how a constant gets added, missed, and shipped with a path in it
# — and every list in this repo that was maintained by hand eventually stopped being maintained.
ALL_REFUSAL_MESSAGES = sorted(n for n in dir(remote) if n.endswith("_MESSAGE"))

# The one NAMED exception to the property below, and the reason it is named rather than derived:
# these two refuse BEFORE a clone exists at all, so "Nothing was pushed" would be a smaller claim
# than the truth. They say "Nothing was written" instead. Anything else this module ever publishes
# is reachable once a clone has been attempted, and has to say what state was left behind.
REFUSED_BEFORE_ANY_CLONE = {"APP_MISCONFIGURED_MESSAGE", "CREDENTIAL_FAULT_MESSAGE"}

AFTER_A_CLONE_WAS_ATTEMPTED = [n for n in ALL_REFUSAL_MESSAGES if n not in REFUSED_BEFORE_ANY_CLONE]

# The FIVE the post-clone ladder maps a refusal TYPE onto — the only place a type appears in this
# file, so the mapping's own shape is pinned beside the text it produces.
MAPPED_MINT_MESSAGES = {
    "CloneStateError": remote.CLONE_STATE_FAULT_MESSAGE,
    "TemplateMissingError": remote.TEMPLATE_MISSING_MESSAGE,
    "CollisionRaceError": remote.COLLISION_RACE_MESSAGE,
    "PushRaceError": remote.PUSH_RACE_MESSAGE,
    "LibrarianError": remote.MINT_FAULT_MESSAGE,
}


@pytest.mark.parametrize("constant", ALL_REFUSAL_MESSAGES)
def test_no_refusal_this_module_publishes_names_a_path_or_a_runnable_git_command(constant):
    """The sweep, over EVERY constant — the ladder's four, the clone leg's, the git/config fault's
    and both credential faults. `entities` writes for an operator standing in a clone (`clone.py`
    and `mint.py` both hand out `git -C <path> …`) and this module is the ONE place those refusals
    are re-worded for somebody who holds neither. A path here is the server host's own temp
    directory, and it is deleted before the message is read."""
    fx.assert_steward_facing(getattr(remote, constant))


@pytest.mark.parametrize("constant", AFTER_A_CLONE_WAS_ATTEMPTED)
def test_every_refusal_reachable_after_a_clone_says_nothing_was_pushed(constant):
    """The one fact a steward needs before any other: their approval did not half-land.

    A COMPLETE partition with the test below, derived from the module rather than from a list of
    the arms — the clone leg's own refusal was an inline literal until this change and would have
    sat outside a hand-picked set forever. A refusal that is silent about state sends a steward to
    look for a commit that is not there, or leaves them afraid to approve again."""
    assert "Nothing was pushed" in getattr(remote, constant)


@pytest.mark.parametrize("constant", sorted(REFUSED_BEFORE_ANY_CLONE))
def test_the_two_credential_faults_say_nothing_was_written_instead(constant):
    """The other half of the partition, and the benign twin for the rule above: a named exception
    that is honest rather than a gap. These fire before `decide_via_clone` has cloned anything, so
    "Nothing was pushed" would understate it — nothing was written at all. Asserted in both
    directions, so widening the rule to "every constant says Nothing was pushed" cannot be done by
    quietly editing these two."""
    assert "Nothing was written" in getattr(remote, constant)
    assert "Nothing was pushed" not in getattr(remote, constant)


def test_the_partition_covers_every_constant_and_overlaps_nowhere():
    """The test on the two tests above. Both are parametrized over derived lists, and a derivation
    that silently produced an EMPTY list would leave a parametrized test that runs zero cases and
    reports green — the permanently-green test this repo's doctrine calls worse than no test."""
    assert set(AFTER_A_CLONE_WAS_ATTEMPTED) | REFUSED_BEFORE_ANY_CLONE == set(ALL_REFUSAL_MESSAGES)
    assert not set(AFTER_A_CLONE_WAS_ATTEMPTED) & REFUSED_BEFORE_ANY_CLONE
    assert len(AFTER_A_CLONE_WAS_ATTEMPTED) >= len(MAPPED_MINT_MESSAGES) + 1, (
        "the post-clone set no longer covers the five mapped sentences plus the clone leg's own")
    assert set(ALL_REFUSAL_MESSAGES) >= REFUSED_BEFORE_ANY_CLONE, (
        "a named exception outlived the constant it exempted — prune it")


def test_the_mapped_mint_refusals_are_five_distinct_sentences():
    """The ladder's specificity: 'approve again', 'commit the template first' and 'ask whoever runs
    this deployment' are three different instructions, and collapsing any two of them onto one
    constant would keep every assertion above green while telling a steward the wrong thing."""
    assert len(set(MAPPED_MINT_MESSAGES.values())) == len(MAPPED_MINT_MESSAGES) == 5
    published = [getattr(remote, name) for name in ALL_REFUSAL_MESSAGES]
    for constant in MAPPED_MINT_MESSAGES.values():
        assert constant in published, (
            "a mapped sentence stopped being a module constant — the sweep above no longer covers "
            "it, and nothing else checks it for a path")
