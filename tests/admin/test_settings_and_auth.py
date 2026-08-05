"""AdminSettings + the auth primitives — every refusal beside its benign twin (house doctrine:
a test that only proves a gate fires measures sensitivity, never specificity)."""
import pytest

from stigmergy.admin import auth
from stigmergy.admin.settings import (
    ACTOR_ENV,
    GITHUB_REPO_ENV,
    GITHUB_TOKEN_ENV,
    TOKEN_HASH_ENV,
    AdminSettings,
)
from stigmergy.server.errors import StartupError
from stigmergy.server.identity import hash_token


def test_unset_env_means_not_configured():
    settings = AdminSettings.from_env({})
    assert not settings.configured()
    assert not settings.github_configured()
    assert settings.actor == "admin-console"
    # No default repository, deliberately: a guessed `<owner>/<repo>` would point one operator's
    # cron buttons at somebody else's repository. Unset is unconfigured.
    assert settings.github_repo == ""


def test_a_github_token_without_a_repository_is_not_configured():
    """Both halves or neither. A token with no repository builds `/repos//actions/...` and fails
    at the API, where the operator cannot see why — so it fails here instead."""
    settings = AdminSettings.from_env({GITHUB_TOKEN_ENV: "ghp_x"})
    assert not settings.github_configured()
    settings = AdminSettings.from_env({GITHUB_TOKEN_ENV: "ghp_x", GITHUB_REPO_ENV: "acme/brain"})
    assert settings.github_configured()


def test_a_real_hash_configures_the_console():
    digest = hash_token("some-token")
    settings = AdminSettings.from_env({TOKEN_HASH_ENV: digest, ACTOR_ENV: "steward",
                                       GITHUB_TOKEN_ENV: "ghp_x", GITHUB_REPO_ENV: "a/b"})
    assert settings.configured() and settings.github_configured()
    assert settings.token_hash == digest
    assert settings.actor == "steward"
    assert settings.github_repo == "a/b"


def test_a_malformed_hash_refuses_at_startup_not_silently():
    """Fail closed AND loudly — a console no token can ever open is worse than no console."""
    with pytest.raises(StartupError, match="stigmergy-admin-token"):
        AdminSettings.from_env({TOKEN_HASH_ENV: "not-a-sha256"})


def test_uppercase_hex_is_normalized_not_refused():
    digest = hash_token("t").upper()
    assert AdminSettings.from_env({TOKEN_HASH_ENV: digest}).token_hash == digest.lower()


# ── token ─────────────────────────────────────────────────────────────────────────────────────
def test_the_right_token_matches_and_the_wrong_one_does_not():
    digest = hash_token("secret-token")
    assert auth.token_matches(digest, "secret-token") is True          # the benign twin
    assert auth.token_matches(digest, "wrong") is False
    assert auth.token_matches(digest, "") is False
    assert auth.token_matches(digest, None) is False


def test_an_empty_configured_hash_matches_nothing_at_all():
    assert auth.token_matches("", "anything") is False


def test_bearer_extraction_accepts_any_scheme_case_and_refuses_smuggling():
    assert auth.bearer_token([(b"authorization", b"Bearer tok")]) == "tok"
    assert auth.bearer_token([(b"authorization", b"bEaReR tok")]) == "tok"     # RFC 9110 §11.1
    assert auth.bearer_token([(b"authorization", b"Basic dXNlcg==")]) is None
    assert auth.bearer_token([(b"authorization", b"Bearer ")]) is None
    assert auth.bearer_token([]) is None
    # Two Authorization headers are adversarial-shaped and refused outright — the MCP
    # middleware's own rule, mirrored, never resolved to whichever value happened to win.
    assert auth.bearer_token(
        [(b"authorization", b"Bearer a"), (b"authorization", b"Bearer b")]) is None


# ── host ──────────────────────────────────────────────────────────────────────────────────────
def test_no_public_host_configured_means_every_host_passes():
    assert auth.host_allowed([(b"host", b"whatever.example")], []) is True


def test_localhost_spellings_pass_on_any_port():
    hosts = ["brain.example.com"]
    for value in (b"localhost:8080", b"127.0.0.1:9999", b"localhost", b"[::1]:8080"):
        assert auth.host_allowed([(b"host", value)], hosts) is True, value


def test_the_configured_public_host_passes_bare_and_on_443_and_foreign_hosts_do_not():
    hosts = ["brain.example.com"]
    assert auth.host_allowed([(b"host", b"brain.example.com")], hosts) is True
    assert auth.host_allowed([(b"host", b"brain.example.com:443")], hosts) is True
    assert auth.host_allowed([(b"host", b"evil.example")], hosts) is False
    assert auth.host_allowed([(b"host", b"brain.example.com.evil.example")],
                             hosts) is False
    assert auth.host_allowed([], hosts) is False
    assert auth.host_allowed([(b"host", b"a"), (b"host", b"b")], hosts) is False
