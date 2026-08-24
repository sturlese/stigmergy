"""Bearer-token and host checks for the master backoffice."""
import hmac

from stigmergy.server.identity import hash_token

_LOCALHOST_NAMES = frozenset({"localhost", "127.0.0.1", "[::1]"})


def token_matches(token_hash: str, presented: str | None) -> bool:
    """Compare a presented token with its configured digest in constant time."""
    if not token_hash or not presented:
        return False
    return hmac.compare_digest(hash_token(presented), token_hash)


def bearer_token(headers: list[tuple[bytes, bytes]]) -> str | None:
    """Return one well-formed bearer token and reject duplicate headers."""
    values = [v for k, v in headers or [] if k.lower() == b"authorization"]
    if len(values) != 1:
        return None
    raw = values[0].decode("latin-1")
    scheme, _, rest = raw.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return rest.strip() or None


def host_allowed(headers: list[tuple[bytes, bytes]], public_hosts: list[str]) -> bool:
    """Apply the configured public-host allowlist while permitting localhost."""
    if not public_hosts:
        return True
    values = [v for k, v in headers or [] if k.lower() == b"host"]
    if len(values) != 1:
        return False
    host = values[0].decode("latin-1").strip().lower()
    bare = host.rsplit(":", 1)[0] if ":" in host and not host.startswith("[") else host
    if host.startswith("["):
        bare = host.split("]", 1)[0] + "]"
    if bare in _LOCALHOST_NAMES:
        return True
    return any(host in (h.lower(), f"{h.lower()}:443") for h in public_hosts)
