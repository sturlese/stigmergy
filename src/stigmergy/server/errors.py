"""Domain errors of the server subsystem.

Like `stigmergy.index.errors`, library code raises these instead of `SystemExit`: the console
entry point (`mcp_server.main`) maps them to a clean stderr line and a non-zero exit code — no
traceback ever reaches an operator's terminal.
"""


class StigmergyServerError(RuntimeError):
    """Base class for server startup/domain errors."""


class IdentityError(StigmergyServerError):
    """Fail-closed identity resolution: no identity, unknown identity, or an unreadable/malformed
    identities file. The server never starts without a resolved audience scope."""


class StartupError(StigmergyServerError):
    """A precondition for serving is missing (e.g. the real embedder is configured but
    OPENAI_API_KEY is absent). Actionable, non-fatal-to-the-shell — the CLI reports and exits."""


class CapabilityUnavailableError(StigmergyServerError):
    """A tool needs a capability this process was started WITHOUT, and says which one.

    `stigmergy-server` must start with no `OPENAI_API_KEY`, so that capture does not depend on the
    read path's quota, key rotation or provider outage. The write tools and
    `read_page` need no embedder and work; `search_brain` and `ask` genuinely cannot, and they
    refuse with this — a named missing capability rather than a `StartupError` that took the whole
    server down, and rather than a class-name-only failure that tells a caller nothing.

    Distinct from `StartupError` on purpose: that one means the server must not run at all. This
    one means the server is running and one capability is absent, which is a different sentence and
    a different exit code (none — nothing exits).

    Safe to return verbatim over HTTP: every message in this family names an environment
    variable, a tool name and a capability, never a DSN, a path, a key or any captured content.
    """


class RateLimitError(StigmergyServerError):
    """Fail-closed rate limiting: an identity exceeded its per-minute token bucket (the shared
    overall bucket, or the stricter `ask` bucket). The message names no other identity and carries
    no internal detail, so it is safe to return to an HTTP or stdio caller verbatim."""
