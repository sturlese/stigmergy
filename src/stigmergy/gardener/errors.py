"""Domain refusals for `stigmergy-gardener` — same posture as `stigmergy.views.errors.ViewError`:
library code raises, `cli.py` maps to a clean stderr line and a non-zero exit, no traceback ever
reaches an operator's terminal."""


class GardenerError(RuntimeError):
    """A clean, caller-facing refusal: a bad `--repo` (not a directory, or missing the paths a
    check needs), a bad setting, or an `sla` finding this run cannot post a notice for (no Slack
    bot token or no configured channel) — the "no command runs itself" checks never raise this;
    only genuine preconditions for RUNNING the tool at all do."""


class SweepGarbage(RuntimeError):
    """The model editorial sweep produced nothing usable — not even after its one retry.

    A SIBLING of `GardenerError` rather than a subclass of it: `GardenerError`'s own docstring
    reserves that class for preconditions on running the tool at all, and a sweep whose output
    failed validation twice is a run-level OUTCOME, not a precondition.

    Raised by `stigmergy.gardener.sweep.run_sweep` and caught inside
    `stigmergy.gardener.run.run_gardener`, never propagated to the CLI: the whole point of catching
    it there is that the deterministic findings computed in the SAME run must still commit."""
