"""Safe provenance locators."""

from __future__ import annotations

import urllib.parse


def without_capability(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        return value
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("provenance URL is invalid")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("provenance URL is invalid") from error
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.rstrip(".").lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    if port is not None and port != default_port:
        host = f"{host}:{port}"
    return urllib.parse.urlunsplit((scheme, host, parsed.path or "/", "", ""))
