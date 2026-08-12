"""Domain refusals for `stigmergy-digest`: library code raises, `cli.py` maps to a clean stderr
line and a non-zero exit — no traceback reaches an operator's terminal."""


class DigestError(RuntimeError):
    """A caller-facing refusal: a malformed `--since`, or a missing channel/token on a REAL post.
    Never raised for a `--dry-run` — previewing without posting is exactly the escape hatch a
    missing channel or token leaves an operator."""
