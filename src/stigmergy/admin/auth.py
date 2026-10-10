"""Backoffice credentials: the session cookie and the CSRF contract."""

SESSION_COOKIE = "stigmergy_session"
CSRF_HEADER = b"x-stigmergy-csrf"
_LOCALHOST_NAMES = frozenset({"localhost", "127.0.0.1", "[::1]"})


def _values(headers, name: bytes) -> list[str]:
    return [v.decode("latin-1") for k, v in headers or [] if k.lower() == name]


def session_value(headers: list[tuple[bytes, bytes]]) -> str | None:
    """The one session cookie the request carries; a repeated cookie counts as none."""
    found = [
        value.strip()
        for raw in _values(headers, b"cookie")
        for key, _, value in (part.strip().partition("=") for part in raw.split(";"))
        if key == SESSION_COOKIE
    ]
    return found[0] if len(found) == 1 and found[0] else None


def session_cookie(value: str, *, max_age: int) -> str:
    return (f"{SESSION_COOKIE}={value}; Path=/admin; Max-Age={max_age}; HttpOnly; Secure; "
            "SameSite=Strict")


def cleared_session_cookie() -> str:
    return f"{SESSION_COOKIE}=; Path=/admin; Max-Age=0; HttpOnly; Secure; SameSite=Strict"


def _bare_host(host: str) -> str:
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if ":" in host else host


def host_allowed(headers: list[tuple[bytes, bytes]], public_hosts: list[str]) -> bool:
    """Apply the configured public-host allowlist while permitting localhost."""
    if not public_hosts:
        return True
    values = _values(headers, b"host")
    if len(values) != 1:
        return False
    host = values[0].strip().lower()
    if _bare_host(host) in _LOCALHOST_NAMES:
        return True
    return any(host in (h.lower(), f"{h.lower()}:443") for h in public_hosts)


def same_origin_request(headers: list[tuple[bytes, bytes]], public_hosts: list[str]) -> bool:
    """A state change needs the page's anti-CSRF header and an Origin naming this very host."""
    if _values(headers, CSRF_HEADER) != ["1"]:
        return False
    origins, hosts = _values(headers, b"origin"), _values(headers, b"host")
    if len(origins) != 1 or len(hosts) != 1:
        return False
    host = hosts[0].strip().lower()
    allowed = {f"https://{host}"}
    if not public_hosts or _bare_host(host) in _LOCALHOST_NAMES:
        allowed.add(f"http://{host}")
    if host.endswith(":443"):
        allowed.add(f"https://{host[:-4]}")
    return origins[0].strip().lower() in allowed
