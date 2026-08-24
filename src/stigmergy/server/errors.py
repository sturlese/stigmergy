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
    OPENROUTER_API_KEY is absent). Actionable, non-fatal-to-the-shell — the CLI reports and exits."""


class CapabilityUnavailableError(StigmergyServerError):
    """A tool needs a capability this process was started WITHOUT, and says which one — the
    server keeps running (capture must not depend on the read path's key/quota), unlike
    `StartupError`. Safe to return verbatim over HTTP: messages name an env var, a tool and a
    capability, never a DSN, a path, a key or any captured content."""


class RegistryError(StigmergyServerError):
    """The entity registry file exists but could not be read as one. A TYPE for confidentiality:
    the loader's own `ValueError` names the registry PATH (written for operators), and
    `search_brain` echoes `ValueError` verbatim — so the path-bearing message must arrive as a
    different type. Raised at the service's call sites, never inside the loader."""


class RateLimitError(StigmergyServerError):
    """Fail-closed rate limiting: an identity exceeded its per-minute token bucket (the shared
    overall bucket, or the stricter `ask` bucket). The message names no other identity and carries
    no internal detail, so it is safe to return to an HTTP or stdio caller verbatim."""
