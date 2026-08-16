"""Runtime configuration for `stigmergy-gardener` — env-tunable settings, never CLI flags.
`GardenerSettings.from_args` is the ONE place the environment is consulted; modules never read it
at import time. `dsn`/`repo` are deliberately not here — connection/location arguments, not
tunable behaviour.

The threshold-literal ban is grep-asserted (`tests/test_architecture.py`): no threshold literal
may appear outside this module, so a hardcoded comparison unreachable by any env override fails
the suite. The digest channel is declared HERE and `digest.settings` imports the constant —
one channel, one spelling.
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


# ── the digest channel — `digest.settings` imports THIS name, never a second literal ──────────
DIGEST_CHANNEL_ID_ENV = "STIGMERGY_DIGEST_CHANNEL_ID"

# ── the model sweep's own configuration ───────────────────────────────────────────────────────
MODEL_ENV = "STIGMERGY_GARDENER_MODEL"
# A cheap-class default of its own, so the sweep never silently rides the shared `CLEAN_MODEL`;
# `$STIGMERGY_GARDENER_MODEL` is how an operator disagrees.
DEFAULT_GARDENER_MODEL = "gpt-5.6-luna"

SWEEP_SAMPLE_ENV = "STIGMERGY_GARDENER_SWEEP_SAMPLE"
DEFAULT_SWEEP_SAMPLE = 10

# Hand-mirrored from `stigmergy.slack.settings.BOT_TOKEN_ENV`, not imported — importing it would
# pull the whole `server.settings` surface in; if that value ever moves, move this with it.
SLACK_BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"


# What a zero or negative value would do to THIS package's arithmetic. `digest.settings` shares the
# validator and passes its own, because one sentence general enough for both would say nothing an
# operator could act on.
_POSITIVE_COUNT_WHY = ("a zero or negative day/window count makes every page/filing instantly past "
                       "threshold.")


def _int_setting(env_name: str, default: int, *, why: str = _POSITIVE_COUNT_WHY) -> int:
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
    # Empty is a real, honest state: most runs have no `sla` finding and never touch Slack.
    # Required only when a notice actually posts (`notice.require_channel`).
    digest_channel_id: str = ""
    model: str = DEFAULT_GARDENER_MODEL
    sweep_sample: int = DEFAULT_SWEEP_SAMPLE

    @classmethod
    def from_args(cls, args=None) -> "GardenerSettings":
        """`args` is accepted, not consulted — env-tunable only; kept for the convention's
        shape."""
        return cls(
            aging_seed_days=_int_setting(AGING_SEED_DAYS_ENV, DEFAULT_AGING_SEED_DAYS),
            concentration_window=_int_setting(CONCENTRATION_WINDOW_ENV,
                                              DEFAULT_CONCENTRATION_WINDOW),
            concentration_share=_share_setting(CONCENTRATION_SHARE_ENV,
                                               DEFAULT_CONCENTRATION_SHARE),
            company_window=_int_setting(COMPANY_WINDOW_ENV, DEFAULT_COMPANY_WINDOW),
            company_share=_share_setting(COMPANY_SHARE_ENV, DEFAULT_COMPANY_SHARE),
            digest_channel_id=os.environ.get(DIGEST_CHANNEL_ID_ENV, ""),
            model=os.environ.get(MODEL_ENV) or DEFAULT_GARDENER_MODEL,
            sweep_sample=_int_setting(SWEEP_SAMPLE_ENV, DEFAULT_SWEEP_SAMPLE),
        )
