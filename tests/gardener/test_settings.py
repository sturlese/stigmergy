"""`gardener.settings`: every threshold is env-tunable with a sane default, and a malformed value
fails loud with an actionable message — never a raw traceback."""
import dataclasses

import pytest

from stigmergy.gardener import settings
from stigmergy.server.errors import StartupError

# Two entries went with the checks that read them — `CANON_UNTOUCHED_DAYS` (the stale-canon
# check) and `CONTRADICTION_SLA_DAYS` (both SLA arms). A threshold nothing reads is not a setting,
# it is a knob wired to nothing.
INT_SETTINGS = [
    (settings.AGING_SEED_DAYS_ENV, settings.DEFAULT_AGING_SEED_DAYS, "aging_seed_days"),
    (settings.CONCENTRATION_WINDOW_ENV, settings.DEFAULT_CONCENTRATION_WINDOW,
     "concentration_window"),
    (settings.COMPANY_WINDOW_ENV, settings.DEFAULT_COMPANY_WINDOW, "company_window"),
    # the sweep's own sample size — validated by the SAME `int_setting` every other count-shaped
    # threshold above already uses, so it rides the identical parametrized suite below rather than
    # needing its own copy of every case.
    (settings.SWEEP_SAMPLE_ENV, settings.DEFAULT_SWEEP_SAMPLE, "sweep_sample"),
]

SHARE_SETTINGS = [
    (settings.CONCENTRATION_SHARE_ENV, settings.DEFAULT_CONCENTRATION_SHARE,
     "concentration_share"),
    (settings.COMPANY_SHARE_ENV, settings.DEFAULT_COMPANY_SHARE, "company_share"),
]

ALL_ENV_NAMES = [env for env, _default, _field in INT_SETTINGS + SHARE_SETTINGS] + [
    settings.DIGEST_CHANNEL_ID_ENV, settings.MODEL_ENV]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every gardener setting starts unset — a leftover from the operator's own shell (or from a
    previous parametrized case) must never leak into the next assertion."""
    for env_name in ALL_ENV_NAMES:
        monkeypatch.delenv(env_name, raising=False)


def test_all_defaults_with_nothing_set():
    s = settings.GardenerSettings.from_args()
    assert s.aging_seed_days == settings.DEFAULT_AGING_SEED_DAYS == 30
    assert s.concentration_window == settings.DEFAULT_CONCENTRATION_WINDOW == 30
    assert s.concentration_share == settings.DEFAULT_CONCENTRATION_SHARE == 0.6
    assert s.company_window == settings.DEFAULT_COMPANY_WINDOW == 20
    assert s.company_share == settings.DEFAULT_COMPANY_SHARE == 0.3
    assert s.digest_channel_id == ""
    assert s.model == settings.DEFAULT_GARDENER_MODEL == "gpt-5.6-luna"
    assert s.sweep_sample == settings.DEFAULT_SWEEP_SAMPLE == 10


def test_from_args_accepts_no_argument_at_all():
    """No flag overrides any of these seven — `from_args` must work with nothing passed, the
    shape `cli.py` actually calls it with."""
    assert settings.GardenerSettings.from_args() == settings.GardenerSettings.from_args(None)


@pytest.mark.parametrize("env_name,default,field", INT_SETTINGS, ids=[f for _, _, f in INT_SETTINGS])
def test_int_setting_reads_a_valid_override(monkeypatch, env_name, default, field):
    monkeypatch.setenv(env_name, "45")
    s = settings.GardenerSettings.from_args()
    assert getattr(s, field) == 45
    assert getattr(s, field) != default


@pytest.mark.parametrize("env_name,default,field", INT_SETTINGS, ids=[f for _, _, f in INT_SETTINGS])
def test_int_setting_rejects_non_numeric(monkeypatch, env_name, default, field):
    monkeypatch.setenv(env_name, "not-a-number")
    with pytest.raises(StartupError, match=env_name):
        settings.GardenerSettings.from_args()


@pytest.mark.parametrize("env_name,default,field", INT_SETTINGS, ids=[f for _, _, f in INT_SETTINGS])
@pytest.mark.parametrize("bad_value", ["0", "-5"])
def test_int_setting_rejects_zero_or_negative(monkeypatch, env_name, default, field, bad_value):
    monkeypatch.setenv(env_name, bad_value)
    with pytest.raises(StartupError, match="positive"):
        settings.GardenerSettings.from_args()


@pytest.mark.parametrize("env_name,default,field", SHARE_SETTINGS,
                        ids=[f for _, _, f in SHARE_SETTINGS])
def test_share_setting_reads_a_valid_override(monkeypatch, env_name, default, field):
    monkeypatch.setenv(env_name, "0.75")
    s = settings.GardenerSettings.from_args()
    assert getattr(s, field) == 0.75
    assert getattr(s, field) != default


@pytest.mark.parametrize("env_name,default,field", SHARE_SETTINGS,
                        ids=[f for _, _, f in SHARE_SETTINGS])
def test_share_setting_rejects_non_numeric(monkeypatch, env_name, default, field):
    monkeypatch.setenv(env_name, "not-a-number")
    with pytest.raises(StartupError, match=env_name):
        settings.GardenerSettings.from_args()


@pytest.mark.parametrize("env_name,default,field", SHARE_SETTINGS,
                        ids=[f for _, _, f in SHARE_SETTINGS])
@pytest.mark.parametrize("bad_value", ["0", "1.5", "-0.1"])
def test_share_setting_rejects_out_of_range(monkeypatch, env_name, default, field, bad_value):
    monkeypatch.setenv(env_name, bad_value)
    with pytest.raises(StartupError, match=r"\(0, 1\]"):
        settings.GardenerSettings.from_args()


def test_share_setting_accepts_the_upper_bound_exactly(monkeypatch):
    monkeypatch.setenv(settings.CONCENTRATION_SHARE_ENV, "1")
    s = settings.GardenerSettings.from_args()
    assert s.concentration_share == 1.0


def test_digest_channel_id_reads_env(monkeypatch):
    monkeypatch.setenv(settings.DIGEST_CHANNEL_ID_ENV, "C0123456789")
    s = settings.GardenerSettings.from_args()
    assert s.digest_channel_id == "C0123456789"


# ── the sweep's own model: a plain string with no format to validate, so it takes
# `digest_channel_id`'s posture rather than the int/share settings' ─────────────────────────────
def test_model_reads_env(monkeypatch):
    monkeypatch.setenv(settings.MODEL_ENV, "anthropic:claude-haiku-4-5")
    s = settings.GardenerSettings.from_args()
    assert s.model == "anthropic:claude-haiku-4-5"


def test_model_falls_back_to_the_cheap_class_default_when_unset(monkeypatch):
    monkeypatch.delenv(settings.MODEL_ENV, raising=False)
    s = settings.GardenerSettings.from_args()
    assert s.model == settings.DEFAULT_GARDENER_MODEL


def test_model_falls_back_to_the_default_on_an_empty_string_too(monkeypatch):
    """`os.environ.get(...) or DEFAULT` (not a bare `.get(..., DEFAULT)`) — an explicitly EMPTY
    env value must not win over the default, the same "empty is not a real override" posture
    `_int_setting`/`_share_setting` already apply for `raw is None or raw == ''`."""
    monkeypatch.setenv(settings.MODEL_ENV, "")
    s = settings.GardenerSettings.from_args()
    assert s.model == settings.DEFAULT_GARDENER_MODEL


def test_settings_is_frozen():
    s = settings.GardenerSettings.from_args()
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.aging_seed_days = 1
