"""AdminSettings + the auth primitives — every refusal beside its benign twin (house doctrine:
a test that only proves a gate fires measures sensitivity, never specificity)."""
import pytest

from stigmergy.admin import auth
from stigmergy.admin.settings import ACTOR_ENV, TOKEN_HASH_ENV, AdminSettings
from stigmergy.server.errors import StartupError
from stigmergy.server.identity import hash_token


def test_unset_env_means_not_configured():
    settings = AdminSettings.from_env({})
    assert not settings.configured()
    assert settings.actor == "admin-console"


def test_the_console_holds_no_credential_for_another_service():
    """The console's whole credential surface is its own token hash, and that is a property worth
    a test rather than an observation: it used to carry a fine-grained GitHub PAT with Actions
    read+write, so that a browser could dispatch the nightly crons. ADR 044 moved those passes
    into the librarian worker, and the PAT went with them — a token that can start a workflow in
    somebody's repository is not a credential to keep for a page that now only reads rows.

    Written over the resolved settings rather than over the source file, so a re-added field fails
    here whatever it is named."""
    settings = AdminSettings.from_env({TOKEN_HASH_ENV: hash_token("t"),
                                       "STIGMERGY_ADMIN_GITHUB_TOKEN": "ghp_x",
                                       "STIGMERGY_ADMIN_GITHUB_REPO": "acme/brain"})
    carried = {name: value for name, value in vars(settings).items()
               if isinstance(value, str) and value in ("ghp_x", "acme/brain")}
    assert not carried, (
        f"AdminSettings picked up {sorted(carried)} from the environment — the console's only "
        f"credential is its own token hash")


def test_a_real_hash_configures_the_console():
    digest = hash_token("some-token")
    settings = AdminSettings.from_env({TOKEN_HASH_ENV: digest, ACTOR_ENV: "steward"})
    assert settings.configured()
    assert settings.token_hash == digest
    assert settings.actor == "steward"


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
