"""Runtime configuration for `stigmergy-gardener` — env-tunable settings, never CLI flags.
Every setting here is a threshold a deterministic check measures against; the gardener asks no
model, so it reads no model name and holds no model budget.
`GardenerSettings.from_args` is the ONE place the environment is consulted; modules never read it
at import time. `dsn`/`repo` are deliberately not here — connection/location arguments, not
tunable behaviour.

The threshold-literal ban is grep-asserted (`tests/test_architecture.py`): no threshold literal
may appear outside this module, so a hardcoded comparison unreachable by any env override fails
the suite.
"""
import os
from dataclasses import dataclass

from stigmergy.server.errors import StartupError

# ── the corpus-health thresholds ──────────────────────────────────────────────────────────────

AGING_SEED_DAYS_ENV = "STIGMERGY_GARDENER_AGING_SEED_DAYS"
DEFAULT_AGING_SEED_DAYS = 30

CONCENTRATION_WINDOW_ENV = "STIGMERGY_GARDENER_CONCENTRATION_WINDOW"
DEFAULT_CONCENTRATION_WINDOW = 30

CONCENTRATION_SHARE_ENV = "STIGMERGY_GARDENER_CONCENTRATION_SHARE"
DEFAULT_CONCENTRATION_SHARE = 0.6

COMPANY_WINDOW_ENV = "STIGMERGY_GARDENER_COMPANY_WINDOW"
DEFAULT_COMPANY_WINDOW = 20

COMPANY_SHARE_ENV = "STIGMERGY_GARDENER_COMPANY_SHARE"
DEFAULT_COMPANY_SHARE = 0.3


# What a zero or negative value would do to THIS package's arithmetic. Callers pass their own
# `why`, because one sentence general enough for every count would say nothing an operator could
# act on.
_POSITIVE_COUNT_WHY = ("a zero or negative day/window count makes every page/filing instantly past "
                       "threshold.")


def int_setting(env_name: str, default: int, *, why: str = _POSITIVE_COUNT_WHY,
                maximum: int | None = None) -> int:
    """A positive whole number from the environment, or `default`. `maximum`, when a setting has
    one, is refused at startup rather than absorbed: a count with a floor and no ceiling is only
    half-validated.

    No setting in THIS module passes `maximum` any more — the one that did bounded a retired model
    pass's batch. It stays because this validator has a second, declared copy in
    `repair.settings._int_setting`, whose settings do use it, and `tests/repair/test_settings_parity.py`
    drives the same rules through both: the rule lives here, so dropping the parameter here would
    leave the twin enforcing something its stated original no longer knows about."""
    raw = os.environ.get(env_name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise StartupError(
            f"${env_name}={raw!r} is not a valid integer — unset it to use the default "
            f"({default}) or set it to a positive whole number.") from None
    if value <= 0:
        raise StartupError(
            f"${env_name}={value} must be a positive integer — {why} Unset it to use the "
            f"default ({default}) or set it to a positive integer.")
    if maximum is not None and value > maximum:
        raise StartupError(
            f"${env_name}={value} is above the maximum of {maximum} — unset it to use the default "
            f"({default}) or set it to a whole number between 1 and {maximum}.")
    return value


def _share_setting(env_name: str, default: float) -> float:
    raw = os.environ.get(env_name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise StartupError(
            f"${env_name}={raw!r} is not a valid number — unset it to use the default "
            f"({default}) or set it to a share between 0 and 1.") from None
    if not (0 < value <= 1):
        raise StartupError(
            f"${env_name}={value} must be a share in (0, 1] — unset it to use the default "
            f"({default}) or set it to a fraction like 0.6.")
    return value


@dataclass(frozen=True)
class GardenerSettings:
    aging_seed_days: int = DEFAULT_AGING_SEED_DAYS
    concentration_window: int = DEFAULT_CONCENTRATION_WINDOW
    concentration_share: float = DEFAULT_CONCENTRATION_SHARE
    company_window: int = DEFAULT_COMPANY_WINDOW
    company_share: float = DEFAULT_COMPANY_SHARE

    @classmethod
    def from_args(cls, args=None) -> "GardenerSettings":
        """`args` is accepted, not consulted — env-tunable only; kept for the convention's
        shape."""
        return cls(
            aging_seed_days=int_setting(AGING_SEED_DAYS_ENV, DEFAULT_AGING_SEED_DAYS),
            concentration_window=int_setting(CONCENTRATION_WINDOW_ENV,
                                              DEFAULT_CONCENTRATION_WINDOW),
            concentration_share=_share_setting(CONCENTRATION_SHARE_ENV,
                                               DEFAULT_CONCENTRATION_SHARE),
            company_window=int_setting(COMPANY_WINDOW_ENV, DEFAULT_COMPANY_WINDOW),
            company_share=_share_setting(COMPANY_SHARE_ENV, DEFAULT_COMPANY_SHARE),
        )
