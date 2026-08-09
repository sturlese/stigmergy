"""`entities.remote.mint_via_clone` — the server-driven mint's own seam (ADR 030 D3), unit-level:
credential resolution (`_authenticated_url`) and the clone/cleanup orchestration, both driven
without a real GitHub App and without a real network call — `githubapp.installation_token`'s own
`opener=` seam is what makes the real thing keyless (`tests/librarian/test_githubapp.py`); this
suite instead monkeypatches `installation_token` itself, because `mint_via_clone` does not thread
an `opener` through (there is exactly one caller, and it is not a place that needs one).

The end-to-end mint (a real bare remote, a real push, the registry regenerated) is proven in
`tests/server/test_review.py` — the one real caller. What is missing there, and what this file
closes, is the credential math no server-side test exercises without a real `https://` remote and
a real App: the tokenized URL's exact shape, the half-configured-App refusal, and that a failed
mint still cleans up its own throwaway clone.
"""
import os

import pytest

from stigmergy.entities import cli as entities_cli
from stigmergy.entities import generator, remote
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
    assert "misconfigured" in str(excinfo.value)


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
    assert "HTTP 401" in str(excinfo.value)


def test_embeds_a_minted_token_into_the_url_in_the_shape_gitcmd_scrubs(monkeypatch):
    """The token-in-URL shape this function builds is EXACTLY what `gitcmd._scrub` already
    redacts from any error message — proven directly against the real regex rather than by
    inspection, so the two halves of this defense cannot silently drift apart."""
    monkeypatch.setattr(remote.githubapp, "installation_token", lambda env: "ghs_stubtoken123")

    url = remote._authenticated_url("https://github.com/acme/knowledge.git", FULL_CREDENTIAL)

    assert url == "https://x-access-token:ghs_stubtoken123@github.com/acme/knowledge.git"
    assert gitcmd._TOKEN_IN_URL.search(url), "not the user:pass@ shape _scrub matches"
    assert "ghs_stubtoken123" not in gitcmd._scrub(url)


# ── mint_via_clone: the clone/cleanup orchestration, mint() itself stubbed out ──────────────────
def test_mint_via_clone_cleans_up_the_temp_clone_even_when_mint_raises(tmp_path, monkeypatch):
    from tests.librarian import support
    env = support.build_repo(str(tmp_path / "git"))
    captured = {}

    def boom(repo, **kwargs):
        captured["repo"] = repo
        captured["author"] = kwargs["author"]
        assert os.path.isdir(repo)
        # the App's identity is configured on the clone BEFORE mint() is called (needed for the
        # bounded rebase-and-retry's own `git rebase`, which a fresh temp clone otherwise has no
        # committer identity for at all).
        name = gitcmd.run("config", "user.name", cwd=repo).stdout.strip()
        email = gitcmd.run("config", "user.email", cwd=repo).stdout.strip()
        assert (name, email) == kwargs["author"]
        raise EntityError("boom from mint()")

    monkeypatch.setattr(remote.mint_lib, "mint", boom)

    with pytest.raises(EntityError, match="boom from mint"):
        remote.mint_via_clone(env.bare, "main", None, entity_id="acme-two", name="Acme Two",
                              entity_type="organization", today="2026-01-01",
                              approved_by="steward@example.com")

    assert captured["author"] == (
        "stigmergy-librarian", "stigmergy-librarian@users.noreply.github.com")
    assert not os.path.exists(captured["repo"]), (
        "the TemporaryDirectory must be gone even though mint() raised")


def test_mint_via_clone_needs_no_credential_against_a_local_bare_remote(tmp_path, monkeypatch):
    """The property the pg suite (`tests/server/test_review.py`) relies on for every mint it
    proves for real: `credential=None` against a local path never even asks `githubapp` whether
    it is configured."""
    from tests.librarian import support
    env = support.build_repo(str(tmp_path / "git"))

    def fail_if_called(*a, **k):
        raise AssertionError("githubapp.configured must not be consulted for a non-https remote")
    monkeypatch.setattr(remote.githubapp, "configured", fail_if_called)
    monkeypatch.setattr(remote.mint_lib, "mint", lambda repo, **kwargs: {"stub": True})

    result = remote.mint_via_clone(env.bare, "main", None, entity_id="acme-two", name="Acme Two",
                                   entity_type="organization", today="2026-01-01",
                                   approved_by="steward@example.com")

    assert result == {"stub": True}


# ── AUDIT M3: the governance trailer cannot be forged ──────────────────────────────────────────
# ADR 030 D1 makes `Approved-by:` half of how `git log` answers "who approved this identity", and
# the knowledge repo's authorship check reads it. MCP and Slack pass a resolved identity, but the
# CONSOLE passes a free-text actor by design (D2 — attribution), so a newline there would inject
# arbitrary lines into the commit message: a second, forged `Approved-by:` among them. `name` was
# already collapsed one field over; the trailer had no equivalent until it became a record.
def test_a_newline_in_the_approver_cannot_forge_a_second_trailer(tmp_path, monkeypatch):
    from tests.librarian import support
    env = support.build_repo(str(tmp_path / "git"))
    seen = {}
    monkeypatch.setattr(remote.mint_lib, "mint",
                        lambda repo, **kw: seen.update(kw) or {"stub": True})
    remote.mint_via_clone(
        env.bare, "main", {}, entity_id="acme", name="Acme", entity_type="organization",
        today="2026-08-05", approved_by="steward@example.com\nApproved-by: mallory@evil.example")

    # git parses a trailer only at the START of a line, so the property that matters is that the
    # value stays on ONE line: the forged text survives as visible content of the single real
    # trailer, never as a second trailer git (or the knowledge repo's authorship check) would read.
    trailer = seen["trailer"]
    assert "\n" not in trailer
    assert len([line for line in trailer.splitlines() if line.startswith("Approved-by:")]) == 1
    assert "mallory@evil.example" in trailer      # kept and visible, never smuggled


def test_an_empty_approver_is_refused_rather_than_committed_blank(tmp_path, monkeypatch):
    from tests.librarian import support
    env = support.build_repo(str(tmp_path / "git"))
    monkeypatch.setattr(remote.mint_lib, "mint", lambda repo, **kw: {"stub": True})
    for blank in ("", "   ", "\n\t"):
        with pytest.raises(EntityError, match="approver"):
            remote.mint_via_clone(env.bare, "main", {}, entity_id="acme", name="Acme",
                                  entity_type="organization", today="2026-08-05",
                                  approved_by=blank)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# CROSS-DOOR CONSISTENCY (ADR 030 D4): "a divergence between the two doors is the defect class
# this decision exists to prevent" — stated in the ADR's prose, never previously stated as a single
# assertion comparing what the two doors actually produce. Every existing suite (this file,
# `tests/server/test_review.py`, `tests/slack/test_review.py`, `tests/admin/test_service_pg.py`,
# `tests/entities/test_cli.py`) inspects only ITS OWN remote — none compares one door's artifact to
# another's byte for byte. This is that comparison: the SAME identity metadata, minted through the
# server seam (`entities.remote.mint_via_clone` — what `server.review`/`slack.review`/
# `admin.service` all call) and through the CLI seam (`entities.cli.main`, a steward's own
# terminal), against two INDEPENDENT bare remotes seeded byte-identically.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_the_server_seam_and_the_cli_seam_mint_equivalent_artifacts(tmp_path, require_gitleaks):
    """Same page path and byte-identical page body, byte-identical registry (both remotes started
    from the same seed and gained the same one entity — `derive_registry` never embeds an absolute
    path or a timestamp, so this is a meaningful comparison rather than one doomed to differ by
    construction), the same conventional-commit SUBJECT/BODY, and exactly one new commit on each
    side. The only two properties allowed to differ are named by D1 itself — the commit author (App
    vs. the steward's own git identity) and the `Approved-by:` trailer paragraph a server-driven
    mint appends and the CLI does not — and this test asserts the difference explicitly rather than
    merely not checking it.

    `submission_id=None` on the server-seam call, matching the CLI's `create` (no queue row): both
    sides then render `birth.commit_message`'s "Created by a steward." body line, so an
    `approve`-vs-`create` wording difference is not a confound this comparison has to explain away.
    """
    server_remote, _server_seed_clone = fx.build_repo(str(tmp_path / "door-a"))
    cli_remote, cli_clone = fx.build_repo(str(tmp_path / "door-b"))
    assert fx.remote_files(server_remote) == fx.remote_files(cli_remote), (
        "the two seed remotes must start byte-identical for this comparison to mean anything")

    name, entity_type, aliases, role, today = (
        "Globex Robotics", "organization", ["Globex", "Globex Robotics Inc"],
        "a robotics manufacturer", "2026-07-27")
    entity_id = generator.canonical_id_for(name)
    approver = "steward@example.com"

    server_result = remote.mint_via_clone(
        server_remote, "main", None, entity_id=entity_id, name=name, entity_type=entity_type,
        aliases=aliases, role=role, today=today, submission_id=None, approved_by=approver)
    server_commit = server_result["commit"]

    rc = entities_cli.main([
        "--repo", cli_clone, "--branch", "main", "create",
        "--id", entity_id, "--name", name, "--type", entity_type,
        "--aliases", ", ".join(aliases), "--role", role, "--today", today])
    assert rc == 0
    cli_commit = gitcmd.run("rev-parse", "main", cwd=cli_remote).stdout.strip()

    # ── same page path, byte-identical page content ─────────────────────────────────────────────
    page_relpath = f"wiki/entities/{name}.md"
    server_page = gitcmd.run("show", f"{server_commit}:{page_relpath}", cwd=server_remote).stdout
    cli_page = gitcmd.run("show", f"{cli_commit}:{page_relpath}", cwd=cli_remote).stdout
    assert server_page == cli_page

    # ── same tree shape: both remotes started identical and gained the same one file ────────────
    server_files = fx.remote_files(server_remote)
    cli_files = fx.remote_files(cli_remote)
    assert page_relpath in server_files and page_relpath in cli_files
    assert server_files == cli_files

    # ── byte-identical registry (the derived view is a pure function of the pages) ───────────────
    server_registry = fx.remote_registry(server_remote)
    cli_registry = fx.remote_registry(cli_remote)
    assert server_registry == cli_registry
    assert server_registry["entities"][entity_id]["name"] == name

    # ── exactly ONE new commit landed on each side (the seed commit + this ONE mint commit) ──────
    server_log = gitcmd.run("log", "--oneline", "main", cwd=server_remote).stdout.strip()
    cli_log = gitcmd.run("log", "--oneline", "main", cwd=cli_remote).stdout.strip()
    assert len(server_log.splitlines()) == len(cli_log.splitlines()) == 2

    # ── same commit-message SHAPE, modulo the trailer (D1's own, named exception) ────────────────
    trailer_line = f"Approved-by: {approver}"
    server_message = gitcmd.run("log", "-1", "--format=%B", server_commit, cwd=server_remote).stdout
    cli_message = gitcmd.run("log", "-1", "--format=%B", cli_commit, cwd=cli_remote).stdout
    subject = f"feat(entity): add {name}\n\nCreated by a steward.\n\n"
    assert server_message.startswith(subject)
    assert cli_message.startswith(subject)
    assert trailer_line in server_message
    assert trailer_line not in cli_message
    before_trailer, _, _ = server_message.partition(f"\n{trailer_line}\n")
    assert before_trailer.rstrip("\n") == cli_message.rstrip("\n")

    # ── the one property D1 says must NOT match: the author (App vs. the steward) ────────────────
    server_author = gitcmd.run("log", "-1", "--format=%an <%ae>", server_commit,
                               cwd=server_remote).stdout.strip()
    cli_author = gitcmd.run("log", "-1", "--format=%an <%ae>", cli_commit,
                            cwd=cli_remote).stdout.strip()
    assert server_author == "stigmergy-librarian <stigmergy-librarian@users.noreply.github.com>"
    assert cli_author == f"{fx.STEWARD_NAME} <{fx.STEWARD_EMAIL}>"
    assert server_author != cli_author, (
        "D1: the App writes, the human is named in a trailer — the author line is how a reader "
        "of git log tells which door a mint came through, and it must not converge")


def test_a_librarian_fault_after_the_clone_is_renamed_into_this_packages_vocabulary(
        tmp_path, monkeypatch):
    """OLD BEHAVIOUR: it escaped as `librarian.errors.LibrarianConfigError`, and the module
    docstring's "This module's public seam raises only `entities.errors.EntityError`" was false for
    everything after the clone.

    `gates.ensure_scanner` runs on this exact path (`mint._refuse_secrets` scans what the commit
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
        raise LibrarianConfigError("the secret scanner 'gitleaks' is not runnable (FileNotFound)")

    monkeypatch.setattr(remote.mint_lib, "mint", scanner_missing)

    with pytest.raises(EntityError, match="secret scanner"):
        remote.mint_via_clone(env.bare, "main", None, entity_id="acme-two", name="Acme Two",
                              entity_type="organization", today="2026-01-01",
                              approved_by="steward@example.com")


def test_an_entity_error_from_the_mint_is_not_rewrapped_by_that_rename(tmp_path, monkeypatch):
    """The benign twin: the rename above catches `LibrarianError`, which `EntityError` is not, so
    every refusal this package already words for a human (a collision, a lost push race, a secret
    in the role text) still reaches the caller as itself and keeps its own sentence."""
    from stigmergy.entities.errors import CollisionError
    from tests.librarian import support
    env = support.build_repo(str(tmp_path / "git"))

    def collide(repo, **kwargs):
        raise CollisionError("that identity already resolves to Acme Corp")

    monkeypatch.setattr(remote.mint_lib, "mint", collide)

    with pytest.raises(CollisionError, match="already resolves"):
        remote.mint_via_clone(env.bare, "main", None, entity_id="acme-two", name="Acme Two",
                              entity_type="organization", today="2026-01-01",
                              approved_by="steward@example.com")
