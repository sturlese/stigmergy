"""Runtime configuration for `stigmergy-gardener`: the corpus-health thresholds, env-tunable per
the `Settings.from_args` convention — modules never read the environment at import time;
`GardenerSettings.from_args` is the ONE place env fallbacks are consulted. All thresholds are
settings, never CLI flags, so no flag overrides any of these — `from_args` still takes `args` for
the same reason `server.settings.Settings.from_args` does even though several of its own fields
never read one: the convention is the seam, not a promise every field uses it.

Deliberately does NOT carry `dsn`/`repo` — those are connection/location arguments `cli.py` reads
directly off `args` (mirroring `views/cli.py`'s own posture: a plain `args.repo`, never folded
into a settings object), not tunable behaviour. `GardenerSettings` is exactly the tunable surface:
five corpus-health thresholds, the digest channel, and the model sweep's own model and sample size.

`model` (`STIGMERGY_GARDENER_MODEL`) is a plain string, read like `digest_channel_id` — no format to
validate, just a name threaded to `stigmergy.kernel.llm.build_processor`'s own `model_name`
parameter. Unlike `views.synthesis`, which passes no `model_name` at all and therefore rides the
shared `CLEAN_MODEL`, the gardener's sweep gets its OWN concrete cheap-class default
(`DEFAULT_GARDENER_MODEL`) so it never silently follows whatever the shared model happens to be
configured to: for this subsystem, the model is configuration. `sweep_sample`
(`STIGMERGY_GARDENER_SWEEP_SAMPLE`) is one more int threshold, validated by the SAME `_int_setting`
every other count-shaped threshold already uses, and is therefore folded into
`tests/test_architecture.py`'s threshold-literal-ban scan alongside the other five.
`schema.MAX_MODEL_DETAIL_CHARS` and `sweep.MAX_SWEEP_SUBJECT_PAGES` are NOT: those are fixed
bounds, never tunable, the same non-settings posture `schema.MAX_DETAIL_CHARS` already has.

**The threshold-literal ban is grep-asserted** (`tests/test_architecture.py`, mirroring the
fence-literal ban): no threshold literal may appear outside this module's own constants/defaults,
so a hardcoded comparison that silently bypasses `GardenerSettings` — and is therefore unreachable
by any env override — fails the suite. Every env var name is owned here next to its `DEFAULT_*`,
the same shape `digest.settings.WINDOW_DAYS_ENV`/`DEFAULT_WINDOW_DAYS` takes one package over.

**The digest channel is shared with `stigmergy.digest`, deliberately.** The SLA notice this package
posts and the digest's own broadcast go to the SAME place: one channel, one place to look. So
`STIGMERGY_DIGEST_CHANNEL_ID` is declared here and `digest.settings` imports the constant rather
than re-declaring the literal — each side imports the name, never a second independently-spelled
copy of it.
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
# A cheap-class default: same family as `stigmergy.kernel.llm.DEFAULT_MODEL` ("gpt-5.4"), one
# reasoning tier down. The sweep is a bounded editorial pass over a batch of pages, not a
# synthesis task, so the cheaper tier is the honest starting point rather than a claim that no
# heavier model would ever be worth it — `$STIGMERGY_GARDENER_MODEL` is how an operator disagrees.
DEFAULT_GARDENER_MODEL = "gpt-5.4-mini"

SWEEP_SAMPLE_ENV = "STIGMERGY_GARDENER_SWEEP_SAMPLE"
DEFAULT_SWEEP_SAMPLE = 10

# ── the Slack bot token — hand-mirrored, not imported. `stigmergy.slack.settings.BOT_TOKEN_ENV`
# carries the identical value, but that module also pulls in the whole `server.settings` surface
# to reach it (`SlackSettings.server: Settings`) — gardener's own declared Slack edge is
# `stigmergy.slack.gateway` alone (this package's own `__init__.py`). Same trade-off
# `capture.schema.MAX_HINT_CHARS` already makes for `server.service.MAX_ARG_CHARS`: mirrored by
# VALUE, not by import, and if this value ever moves, this comment is the pointer to the other.
SLACK_BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"


def _int_setting(env_name: str, default: int) -> int:
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
            f"${env_name}={value} must be a positive integer — a zero or negative day/window "
            f"count makes every page/filing instantly past threshold. Unset it to use the "
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
    # Empty is a real, honest state (matching `server.settings.Settings.knowledge_repo`'s own
    # posture): most runs have no `sla` finding and never touch Slack at all. Required only at the
    # moment a notice actually needs to post — see `notice.py::require_channel`.
    digest_channel_id: str = ""
    # The model sweep's own model and sample size — see the module docstring for why `model` gets
    # a concrete default rather than deferring to the shared `CLEAN_MODEL`.
    model: str = DEFAULT_GARDENER_MODEL
    sweep_sample: int = DEFAULT_SWEEP_SAMPLE

    @classmethod
    def from_args(cls, args=None) -> "GardenerSettings":
        """`args` is accepted, not consulted — these are env-tunable only, and no flag overrides
        any of them. Kept for the convention's own sake (`server.settings.Settings.from_args`'s
        shape), and so a future flag can be added here without a rename."""
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
