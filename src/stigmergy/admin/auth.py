"""Admin auth primitives — small and pure.

Token: bearer -> sha256 -> constant-time compare against the ONE configured hash; no store, one
credential, revoked by changing one secret; failures are a generic 401, never a reason. Host:
mirrors the MCP transport's DNS-rebinding allowlist — defense in depth only, since a bearer token
carries no ambient credential a rebinding could ride.
"""
import hmac

from stigmergy.server.identity import hash_token

_LOCALHOST_NAMES = frozenset({"localhost", "127.0.0.1", "[::1]"})


def token_matches(token_hash: str, presented: str | None) -> bool:
    """True iff `presented` hashes to the configured digest; an empty configured hash matches
    nothing (belt and braces — the console is not built in that state)."""
    if not token_hash or not presented:
        return False
    return hmac.compare_digest(hash_token(presented), token_hash)


def bearer_token(headers: list[tuple[bytes, bytes]]) -> str | None:
    """The presented bearer token, or None on anything malformed — including a request smuggling
    two `Authorization` headers, refused outright, never dict()-collapsed to whichever won."""
    values = [v for k, v in headers or [] if k.lower() == b"authorization"]
    if len(values) != 1:
        return None
    raw = values[0].decode("latin-1")
    scheme, _, rest = raw.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return rest.strip() or None


def host_allowed(headers: list[tuple[bytes, bytes]], public_hosts: list[str]) -> bool:
    """Mirror of the transport's allowlist: with no public host configured every host passes
    (local dev unchanged); configured, the header must be a localhost spelling (any port) or a
    configured host bare or on `:443`."""
    if not public_hosts:
        return True
    values = [v for k, v in headers or [] if k.lower() == b"host"]
    if len(values) != 1:
        return False
    host = values[0].decode("latin-1").strip().lower()
    bare = host.rsplit(":", 1)[0] if ":" in host and not host.startswith("[") else host
    if host.startswith("["):   # [::1]:8080 → [::1]
        bare = host.split("]", 1)[0] + "]"
    if bare in _LOCALHOST_NAMES:
        return True
    return any(host in (h.lower(), f"{h.lower()}:443") for h in public_hosts)
