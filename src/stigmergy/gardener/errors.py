"""Domain refusals for `stigmergy-gardener`: library code raises, `cli.py` maps to a clean stderr
line and a non-zero exit — no traceback reaches an operator's terminal."""


class GardenerError(RuntimeError):
    """A caller-facing refusal about a precondition for RUNNING the tool at all: a bad `--repo`
    or a bad setting."""


class SweepGarbage(RuntimeError):
    """The model sweep produced nothing usable, even after its one retry — a run-level outcome,
    deliberately a sibling of `GardenerError`, not a subclass. Raised by `sweep.run_sweep`,
    caught in `run.run_gardener` so the same run's deterministic findings still commit."""
