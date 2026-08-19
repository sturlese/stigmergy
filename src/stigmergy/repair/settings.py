"""Runtime configuration for `stigmergy-repair` — env-tunable, read in ONE place.

`RepairSettings.from_env` is the only function in this package that consults the environment, and
modules never read it at import time (`tests/test_architecture.py`). `dsn` is deliberately not
here: a connection argument, not tunable behaviour.

The bounds below are the proposer's blast radius, and they belong to code rather than to the
skill: a brief can be argued with and a constant cannot. `MAX_OPS_PER_PROPOSAL` is what keeps one
approval from being a corpus-wide rewrite — a steward approves ONE proposal, and this is how much
one proposal is allowed to be.
"""
import os
from dataclasses import dataclass

from stigmergy.librarian import config as librarian_config
from stigmergy.server.errors import StartupError

MODEL_ENV = "STIGMERGY_REPAIR_MODEL"

# The librarian's own default, deliberately: the proposer reads pages and writes an edit
# declaration in exactly the vocabulary the filing agent already uses, so a deployment that has
# settled on a model for one has settled on it for the other. `$STIGMERGY_REPAIR_MODEL` is how an
# operator disagrees.
DEFAULT_REPAIR_MODEL = librarian_config.DEFAULT_MODEL

MAX_OPS_ENV = "STIGMERGY_REPAIR_MAX_OPS"
DEFAULT_MAX_OPS_PER_PROPOSAL = 6

# How many findings go to the model in ONE call. A batch is the unit of LOSS: a call that spends
# its usage budget mid-work is skipped whole, so every finding in it waits for the next night. At 8
# that cost eight findings a lapse, and on the first real corpus it cost the additive road every
# proposal it had (issue #75). The proposer's budget scales with this number
# (`proposer.batch_limits`), so raising it buys the model more room rather than starving it — what
# it also buys is a bigger crater when one call lapses.
BATCH_SIZE_ENV = "STIGMERGY_REPAIR_BATCH"
DEFAULT_BATCH_SIZE = 3
# The hard ceiling on that knob, because it MULTIPLIES a per-call model budget by six
# (`proposer.batch_limits`): of every count setting in the two packages, this is the one whose
# blast radius is a bill rather than a prompt, and it was the one with no maximum. Sized far above
# any sane batch — a lapse at 32 costs 32 findings their night — and far below what a typo'd
# extra digit would buy.
MAX_BATCH_SIZE = 32

# What ONE run may put in front of stewards. `MAX_OPS_PER_PROPOSAL` bounds one approval; this
# bounds how many approvals a night can ask for, which is the other half of the same argument: a
# gardener run that suddenly reports four hundred findings would otherwise cost four hundred model
# calls and produce an inbox nobody reads. The surplus is not lost — it is proposed by the next
# run, once these have been decided.
MAX_PROPOSALS_ENV = "STIGMERGY_REPAIR_MAX_PROPOSALS"
DEFAULT_MAX_PROPOSALS_PER_RUN = 20

# How much ONE approval may be, measured in the bytes its stored plan carries. Shared by the TWO
# kinds whose ops hold whole PAGES — a `delete` sweep carries every page it would rewrite, and an
# `entity-alias` merge carries every page it would re-anchor, both in full so the apply can
# recompute the plan and byte-compare it — so the natural bound for either is a size rather than a
# count of ops. Around thirty average pages at the default, which is already more of a corpus
# change than one Approve button should stand for.
MAX_PLAN_BYTES_ENV = "STIGMERGY_REPAIR_MAX_PLAN_BYTES"
DEFAULT_MAX_PLAN_BYTES = 100_000

# What a zero or negative value would do to THIS package's arithmetic — the sentence
# `gardener.settings.int_setting` interpolates, written for the bounds it guards here.
_POSITIVE_COUNT_WHY = ("a zero or negative bound would either refuse every proposal or send an "
                       "empty batch to the model.")


def _int_setting(env_name: str, default: int, *, maximum: int | None = None) -> int:
    """`gardener.settings.int_setting`'s rules, spelled here rather than imported: importing it
    would put a `stigmergy.gardener.settings` edge on this package for one validator, and this
    package already reaches the gardener for findings only. Two callers, one shape — the parity
    test in `tests/repair/test_settings_parity.py` is what keeps a rule from landing on one copy
    only, which is exactly how this one's `maximum` arrived late."""
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
            f"${env_name}={value} must be a positive integer — {_POSITIVE_COUNT_WHY} Unset it to "
            f"use the default ({default}) or set it to a positive integer.")
    if maximum is not None and value > maximum:
        raise StartupError(
            f"${env_name}={value} is above the maximum of {maximum} — unset it to use the default "
            f"({default}) or set it to a whole number between 1 and {maximum}.")
    return value


@dataclass(frozen=True)
class RepairSettings:
    """`repo` is WHERE the proposer reads from — the same checkout every other tool here is
    pointed at, resolved through the shared `librarian_config.repo_path`. It is paired with
    `is_repo_checkout` because the proposer reads the entity registry, the pages and the skill at
    a real clone's HEAD, and a bare directory of markdown would silently answer every question
    with a different corpus than the one the apply will commit against."""

    repo: str = ""
    model: str = DEFAULT_REPAIR_MODEL
    max_ops_per_proposal: int = DEFAULT_MAX_OPS_PER_PROPOSAL
    batch_size: int = DEFAULT_BATCH_SIZE
    max_proposals_per_run: int = DEFAULT_MAX_PROPOSALS_PER_RUN
    max_plan_bytes: int = DEFAULT_MAX_PLAN_BYTES

    @classmethod
    def from_env(cls, args=None) -> "RepairSettings":
        """`args` supplies `--repo` only; everything else is env-tunable, the convention
        `GardenerSettings.from_args` already sets."""
        return cls(
            repo=librarian_config.repo_path(getattr(args, "repo", None) or ""),
            model=os.environ.get(MODEL_ENV) or DEFAULT_REPAIR_MODEL,
            max_ops_per_proposal=_int_setting(MAX_OPS_ENV, DEFAULT_MAX_OPS_PER_PROPOSAL),
            batch_size=_int_setting(BATCH_SIZE_ENV, DEFAULT_BATCH_SIZE,
                                    maximum=MAX_BATCH_SIZE),
            max_proposals_per_run=_int_setting(MAX_PROPOSALS_ENV, DEFAULT_MAX_PROPOSALS_PER_RUN),
            max_plan_bytes=_int_setting(MAX_PLAN_BYTES_ENV, DEFAULT_MAX_PLAN_BYTES),
        )
