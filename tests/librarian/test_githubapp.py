"""The `stigmergy-librarian` GitHub App identity: `configured()`'s all-or-nothing guard, JWT-signed
installation token minting (driven end to end through the injectable `opener=`, real RS256
signing included — spec module docstring: "a test can drive the whole path... against a stub
instead of GitHub"), the push URL shape, and the commit identity.

No network is ever reached here: `installation_token`'s `opener` seam is exactly what makes that
true (`githubapp.py`'s own docstring), so this suite needs no key and no connectivity.
"""
import base64
import json

import pytest

from stigmergy.librarian import githubapp
from stigmergy.librarian.errors import LibrarianConfigError


def _generate_test_key() -> str:
    """A real, freshly generated RSA private key in PEM form — `pyjwt`'s RS256 encoder needs a
    real key, and a placeholder string would only prove the HTTP plumbing, not the signing."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


FULL_ENV = {
    githubapp.APP_ID_ENV: "123456",
    githubapp.INSTALLATION_ID_ENV: "987654",
    githubapp.PRIVATE_KEY_ENV: _generate_test_key(),
}


# ── configured(): all or nothing ────────────────────────────────────────────────────────────
def test_configured_is_false_with_no_env_at_all():
    assert githubapp.configured({}) is False


# ── no test run pushes to the real remote, through this variable either ────────────────────────
def test_the_process_environment_never_configures_the_app_during_a_test_run():
    """`configured()` with NO argument reads `os.environ`, and that is the call `processing._file`
    makes to decide whether to push to `github.com/<slug>` instead of the fixtures' bare remote.

    The Makefile's `-include .env` + `export` hands every target the operator's gitignored
    credentials, so an operator whose real App id, installation id and private key sit in that
    file gets a `make test` that mints real installation tokens and pushes fixture commits to the
    company's knowledge repo — real writes, out of a test run. The autouse fixture in
    `conftest.py` clears the four variables; this asserts the property that fixture exists for,
    rather than trusting that it is still wired up somewhere.
    """
    assert githubapp.configured() is False


def test_the_commit_identity_falls_back_to_the_unnumbered_app_login_with_no_app_configured():
    """The consequence for the fixtures: an unconfigured run commits as the plain app login and
    pushes to `origin`, which in every test is a local bare repo."""
    name, email = githubapp.identity()
    assert name == githubapp.APP_LOGIN_DEFAULT
    assert email.endswith("@users.noreply.github.com")
    assert "[bot]" not in name


def test_configured_is_true_with_every_field_present():
    assert githubapp.configured(FULL_ENV) is True


@pytest.mark.parametrize("missing", [githubapp.APP_ID_ENV, githubapp.INSTALLATION_ID_ENV,
                                     githubapp.PRIVATE_KEY_ENV])
def test_configured_raises_loudly_on_a_half_configured_app_never_silently_falls_back(missing):
    partial = {k: v for k, v in FULL_ENV.items() if k != missing}
    with pytest.raises(LibrarianConfigError, match="half-configured"):
        githubapp.configured(partial)


def test_configured_accepts_the_private_key_file_variant_in_place_of_the_inline_one(tmp_path):
    key_path = tmp_path / "key.pem"
    key_path.write_text(FULL_ENV[githubapp.PRIVATE_KEY_ENV], encoding="utf-8")
    env = {githubapp.APP_ID_ENV: FULL_ENV[githubapp.APP_ID_ENV],
          githubapp.INSTALLATION_ID_ENV: FULL_ENV[githubapp.INSTALLATION_ID_ENV],
          githubapp.PRIVATE_KEY_FILE_ENV: str(key_path)}
    assert githubapp.configured(env) is True


# ── installation_token(): the whole path, including real RS256 signing, via a stub opener ──────
class _StubResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_installation_token_signs_a_real_jwt_and_returns_the_minted_token():
    captured = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        return _StubResponse({"token": "ghs_mintedtoken123"})

    token = githubapp.installation_token(FULL_ENV, opener=opener)

    assert token == "ghs_mintedtoken123"
    assert captured["url"] == (
        f"https://api.github.com/app/installations/"
        f"{FULL_ENV[githubapp.INSTALLATION_ID_ENV]}/access_tokens")
    assert captured["auth"].startswith("Bearer ")
    # the bearer credential is a real, well-formed JWT (three dot-separated segments) — proof the
    # RS256 signing step actually ran rather than being stubbed out alongside the HTTP call.
    assert captured["auth"].split(" ", 1)[1].count(".") == 2


def test_installation_token_raises_when_the_app_is_not_configured_at_all():
    with pytest.raises(LibrarianConfigError, match="not configured"):
        githubapp.installation_token({}, opener=lambda *a, **k: None)


def test_installation_token_reduces_an_http_error_to_a_safe_message():
    import urllib.error

    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Bad credentials", {}, None)

    with pytest.raises(LibrarianConfigError, match="401"):
        githubapp.installation_token(FULL_ENV, opener=opener)


def test_installation_token_raises_when_github_returns_no_token_field():
    def opener(request, timeout):
        return _StubResponse({"unexpected": "shape"})

    with pytest.raises(LibrarianConfigError, match="no token"):
        githubapp.installation_token(FULL_ENV, opener=opener)


# ── the push URL and the push credential: never on disk, and never in argv either ──────────────
def test_push_url_carries_no_credential_at_all():
    """This used to assert the URL embedded the token (`https://x-access-token:<token>@github.com/
    ...`) and `gitcmd.push` passed it as argv. argv is world-readable: the token was observable in
    `ps -Ao args` for the whole push, on a machine that also runs other agents (reproduced). The
    URL is plain and the credential travels in the child's environment instead — so this asserts
    the ABSENCE of what it used to require."""
    url = githubapp.push_url("acme/knowledge")
    assert url == "https://github.com/acme/knowledge.git"
    assert "@" not in url and "token" not in url


def test_push_config_carries_the_token_in_a_scoped_git_config_env_triple():
    config = githubapp.push_config("ghs_abc123", "acme/knowledge")
    assert config["GIT_CONFIG_COUNT"] == "1"
    # scoped to the ONE url, never a global `http.extraheader` that a redirect could carry
    # elsewhere
    assert config["GIT_CONFIG_KEY_0"] == (
        "http.https://github.com/acme/knowledge.git.extraheader")
    assert config["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    # the token is present, but base64'd inside a header value rather than in a URL — and this
    # dict reaches git through `env=`, so it never becomes an argument
    decoded = base64.b64decode(config["GIT_CONFIG_VALUE_0"].split()[-1]).decode()
    assert decoded == "x-access-token:ghs_abc123"
    assert "ghs_abc123" not in config["GIT_CONFIG_KEY_0"]


# ── commit identity: the App's own convention when configured, a fallback otherwise ────────────
def test_identity_uses_the_bot_convention_when_the_app_id_is_set():
    name, email = githubapp.identity(FULL_ENV)
    assert name == "stigmergy-librarian[bot]"
    assert email == f"{FULL_ENV[githubapp.APP_ID_ENV]}+stigmergy-librarian[bot]@users.noreply.github.com"


def test_identity_falls_back_to_the_plain_login_with_no_app_id():
    name, email = githubapp.identity({})
    assert name == "stigmergy-librarian"
    assert email == "stigmergy-librarian@users.noreply.github.com"


# ── the slug is the DEPLOYMENT's, not this software's ──────────────────────────────────────────
# The failure this pins, which is silent in every direction that matters: an App named anything
# other than the default still mints tokens, still pushes, and still returns 200 — the commits
# simply stop rendering as the App, and the knowledge repo's own authorship check rejects every
# one of them, because a check against forged authorship necessarily pins ONE identity. Nothing
# fails at the seam where the mistake was made. It fails one repository over.
def test_the_app_slug_comes_from_the_environment_when_the_deployment_names_its_own():
    env = {**FULL_ENV, githubapp.APP_LOGIN_ENV: "acme-librarian"}
    name, email = githubapp.identity(env)
    assert name == "acme-librarian[bot]"
    assert email == "123456+acme-librarian[bot]@users.noreply.github.com"
    assert githubapp.app_login(env) == "acme-librarian"


def test_the_configured_slug_also_governs_the_unnumbered_fallback():
    """Both legs of `identity`, so a deployment cannot be right when its App is reachable and
    wrong when it is not — the fallback is what a local operator run commits as."""
    name, email = githubapp.identity({githubapp.APP_LOGIN_ENV: "acme-librarian"})
    assert (name, email) == ("acme-librarian", "acme-librarian@users.noreply.github.com")


@pytest.mark.parametrize("value", ["", None])
def test_an_absent_or_empty_slug_falls_back_to_the_default_rather_than_an_empty_login(value):
    """An empty string is what a `fly secrets set STIGMERGY_LIBRARIAN_APP_LOGIN=""` leaves behind,
    and `[bot]@users.noreply.github.com` with no name in front is a commit author nothing can
    attribute — worse than the default, which is at least a real shape."""
    env = {**FULL_ENV} if value is None else {**FULL_ENV, githubapp.APP_LOGIN_ENV: value}
    assert githubapp.app_login(env) == githubapp.APP_LOGIN_DEFAULT
    assert githubapp.identity(env)[0] == f"{githubapp.APP_LOGIN_DEFAULT}[bot]"


