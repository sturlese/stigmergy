"""Domain refusals for `stigmergy-digest` — same posture as `stigmergy.gardener.errors.GardenerError`/
`stigmergy.views.errors.ViewError`: library code raises, `cli.py` maps to a clean stderr line
and a non-zero exit, no traceback ever reaches an operator's terminal."""


class DigestError(RuntimeError):
    """A clean, caller-facing refusal: a malformed `--since`, no configured Slack channel when a
    REAL post is attempted, or no Slack bot token when a REAL post is attempted. Never raised for
    the precondition a `--dry-run` needs — that surface is deliberately reachable with neither the
    channel nor the token configured, since previewing the body without posting is exactly the
    escape hatch a missing channel or token leaves an operator."""
