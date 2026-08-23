"""Domain refusals for `stigmergy-gardener`: library code raises, `cli.py` maps to a clean stderr
line and a non-zero exit — no traceback reaches an operator's terminal."""


class GardenerError(RuntimeError):
    """A caller-facing refusal about a precondition for RUNNING the tool at all: a bad `--repo`
    or a bad setting."""
