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

# How many CHANGED pages one editorial sweep may carry — the bound the pass's own population
# never had (issue #101): "changed since the watermark" is unbounded, and on a first run or after
# a cron outage it is the whole corpus, in one prompt, whose failure freezes the watermark that
# would shrink it. The overflow is never lost — it joins the unchanged pool, where the rotating
# sample reaches it — so this bounds how fast the changed stream is prioritized, not whether a
# page is ever judged. Sized well above an ordinary night's filings.
SWEEP_CHANGED_CEILING_ENV = "STIGMERGY_GARDENER_SWEEP_CHANGED_CEILING"
DEFAULT_SWEEP_CHANGED_CEILING = 30

# ── the empty-body pass's own two bounds ─────────────────────────────────────────────────────
# Deliberately NOT a sample size: that pass covers its whole population (entity pages are a
# bounded set), so these two bound the model SPEND over it rather than choosing which pages are
# looked at. The batch is how many entity bodies ride one call; the ceiling is how many pages one
# run may judge at all, and when it binds the run records what it deferred instead of truncating
# in silence.
EMPTY_BODY_BATCH_ENV = "STIGMERGY_GARDENER_EMPTY_BODY_BATCH"
DEFAULT_EMPTY_BODY_BATCH = 8
# The batch is the ONLY thing standing between the whole entity-page population and a single model
# call, so it is the one count here with a ceiling of its own: a floor alone would let `=100000`
# put every body in one prompt, which is the failure batching exists to prevent. The run ceiling
# needs no such bound — raising it adds calls, never enlarges one. This figure is a blast-radius
# bound, deliberately well above any batch worth setting, not a recommendation: it exists to make
# the catastrophic value impossible, and an operator who wants 8 or 20 is choosing on other
# grounds entirely.
MAX_EMPTY_BODY_BATCH = 64

EMPTY_BODY_CEILING_ENV = "STIGMERGY_GARDENER_EMPTY_BODY_CEILING"
DEFAULT_EMPTY_BODY_CEILING = 150

# ── the duplicate-identity pass's ONE bound ──────────────────────────────────────────────────
# One bound rather than the pair above, and the absence of a batch size is the decision: that pass
# asks whether TWO registry entries are one entity, and a pair whose halves fell in different
# batches is invisible to every batch. So its population rides ONE prompt and the only thing to
# bound is how large that population may be — `sweep.MAX_DUPLICATE_ENTITY_PROMPT_CHARS` bounds what
# each entry contributes to it. Lower than the empty-body ceiling for the same arithmetic reason:
# every entry is co-present in one call rather than spread over batches of eight.
DUPLICATE_ENTITY_CEILING_ENV = "STIGMERGY_GARDENER_DUPLICATE_ENTITY_CEILING"
DEFAULT_DUPLICATE_ENTITY_CEILING = 120

# Hand-mirrored from `stigmergy.slack.settings.BOT_TOKEN_ENV`, not imported — importing it would
# pull the whole `server.settings` surface in; if that value ever moves, move this with it.
SLACK_BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"


# What a zero or negative value would do to THIS package's arithmetic. `digest.settings` shares the
# validator and passes its own, because one sentence general enough for both would say nothing an
# operator could act on.
_POSITIVE_COUNT_WHY = ("a zero or negative day/window count makes every page/filing instantly past "
                       "threshold.")

# The empty-body pass's own sentence: its two counts are not thresholds a page is measured against
# but bounds on how much of the population gets judged, and zero would silently disable a whole
# model pass while every run still reported success.
_EMPTY_BODY_COUNT_WHY = ("a zero or negative batch size or run ceiling means no entity page is "
                         "ever judged for an empty body, and the run would say nothing was wrong.")

# The duplicate-identity pass's own sentence. Its ceiling is not a threshold either, and zero
# there is worse than a disabled pass: a run would report no duplicate identities for a registry
# nothing compared.
_DUPLICATE_ENTITY_COUNT_WHY = (
    "a zero or negative run ceiling means no registered entity is ever compared against another, "
    "and the run would say the registry holds no duplicate identity.")


def int_setting(env_name: str, default: int, *, why: str = _POSITIVE_COUNT_WHY,
                maximum: int | None = None) -> int:
    """A positive whole number from the environment, or `default`. `maximum`, when a setting has
    one, is refused at startup rather than absorbed: a count with a floor and no ceiling is only
    half-validated, and the settings that need one say why at their call site."""
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
    # Empty is a real, honest state: most runs have no `sla` finding and never touch Slack.
    # Required only when a notice actually posts (`notice.require_channel`).
    digest_channel_id: str = ""
    model: str = DEFAULT_GARDENER_MODEL
    sweep_sample: int = DEFAULT_SWEEP_SAMPLE
    sweep_changed_ceiling: int = DEFAULT_SWEEP_CHANGED_CEILING
    empty_body_batch: int = DEFAULT_EMPTY_BODY_BATCH
    empty_body_ceiling: int = DEFAULT_EMPTY_BODY_CEILING
    duplicate_entity_ceiling: int = DEFAULT_DUPLICATE_ENTITY_CEILING

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
            digest_channel_id=os.environ.get(DIGEST_CHANNEL_ID_ENV, ""),
            model=os.environ.get(MODEL_ENV) or DEFAULT_GARDENER_MODEL,
            sweep_sample=int_setting(SWEEP_SAMPLE_ENV, DEFAULT_SWEEP_SAMPLE),
            sweep_changed_ceiling=int_setting(SWEEP_CHANGED_CEILING_ENV,
                                              DEFAULT_SWEEP_CHANGED_CEILING),
            empty_body_batch=int_setting(EMPTY_BODY_BATCH_ENV, DEFAULT_EMPTY_BODY_BATCH,
                                         why=_EMPTY_BODY_COUNT_WHY,
                                         maximum=MAX_EMPTY_BODY_BATCH),
            empty_body_ceiling=int_setting(EMPTY_BODY_CEILING_ENV, DEFAULT_EMPTY_BODY_CEILING,
                                           why=_EMPTY_BODY_COUNT_WHY),
            duplicate_entity_ceiling=int_setting(DUPLICATE_ENTITY_CEILING_ENV,
                                                 DEFAULT_DUPLICATE_ENTITY_CEILING,
                                                 why=_DUPLICATE_ENTITY_COUNT_WHY),
        )
