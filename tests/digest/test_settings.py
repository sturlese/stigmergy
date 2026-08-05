"""`digest.settings`: the window default is env-tunable with a sane default, and a malformed value
fails loud with an actionable message — never a raw traceback. The channel/token env NAMES are
re-exported from `gardener.settings`, never re-declared — pinned here too, so an accidental
re-declaration is caught locally."""
import pytest

from stigmergy.digest import settings
from stigmergy.gardener import settings as gardener_settings
from stigmergy.server.errors import StartupError

ALL_ENV_NAMES = [settings.WINDOW_DAYS_ENV, settings.DIGEST_CHANNEL_ID_ENV]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for env_name in ALL_ENV_NAMES:
        monkeypatch.delenv(env_name, raising=False)


def test_channel_and_token_env_names_are_reexported_not_redeclared():
    """ONE name, imported — never a second, independently-spelled literal."""
    assert settings.DIGEST_CHANNEL_ID_ENV is gardener_settings.DIGEST_CHANNEL_ID_ENV
    assert settings.SLACK_BOT_TOKEN_ENV is gardener_settings.SLACK_BOT_TOKEN_ENV


def test_all_defaults_with_nothing_set():
    s = settings.DigestSettings.from_args()
    assert s.digest_channel_id == ""
    assert s.window_days == settings.DEFAULT_WINDOW_DAYS == 7


def test_from_args_accepts_no_argument_at_all():
    """No flag overrides either of these — `from_args` must work with nothing passed, the shape
    `cli.py` actually calls it with."""
    settings.DigestSettings.from_args()


def test_digest_channel_id_reads_the_env_var(monkeypatch):
    monkeypatch.setenv(settings.DIGEST_CHANNEL_ID_ENV, "C0123456789")
    assert settings.DigestSettings.from_args().digest_channel_id == "C0123456789"


def test_window_days_reads_the_env_var(monkeypatch):
    monkeypatch.setenv(settings.WINDOW_DAYS_ENV, "14")
    assert settings.DigestSettings.from_args().window_days == 14


@pytest.mark.parametrize("raw", ["not-a-number", "3.5", ""])
def test_window_days_bad_values(monkeypatch, raw):
    if raw == "":
        # Empty is treated as unset (the same posture every other int setting in this codebase
        # takes) — not itself a malformed-value case.
        monkeypatch.setenv(settings.WINDOW_DAYS_ENV, raw)
        assert settings.DigestSettings.from_args().window_days == settings.DEFAULT_WINDOW_DAYS
        return
    monkeypatch.setenv(settings.WINDOW_DAYS_ENV, raw)
    with pytest.raises(StartupError, match=settings.WINDOW_DAYS_ENV):
        settings.DigestSettings.from_args()


@pytest.mark.parametrize("raw", ["0", "-1", "-30"])
def test_window_days_refuses_zero_or_negative(monkeypatch, raw):
    monkeypatch.setenv(settings.WINDOW_DAYS_ENV, raw)
    with pytest.raises(StartupError, match="positive"):
        settings.DigestSettings.from_args()


def test_settings_is_frozen():
    s = settings.DigestSettings()
    with pytest.raises((AttributeError, TypeError)):
        s.window_days = 99
