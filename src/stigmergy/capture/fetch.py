"""Bounded public HTTP fetching with DNS and redirect validation."""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from dataclasses import dataclass

from stigmergy.capture import schema
from stigmergy.capture.errors import FetchRejected
from stigmergy.capture.provenance import without_capability

MAX_REDIRECTS = 5
CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 20
CHUNK_BYTES = 64 * 1024
USER_AGENT = "Stigmergy/1 public-artifact-fetcher"
REDIRECTS = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class ResolvedURL:
    url: str
    scheme: str
    hostname: str
    port: int
    ip: str
    target: str
    host_header: str


@dataclass(frozen=True)
class FetchedArtifact:
    data: bytes
    final_url: str
    response_media_type: str


def resolve_url(url: str, *, resolver=socket.getaddrinfo) -> ResolvedURL:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as error:
        raise FetchRejected("URL is invalid") from error
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise FetchRejected("only HTTP and HTTPS URLs are supported")
    if not parsed.hostname or parsed.username or parsed.password:
        raise FetchRejected("URL must have a public host and no embedded credentials")
    hostname = parsed.hostname.rstrip(".").lower()
    try:
        answers = resolver(hostname, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise FetchRejected("URL host could not be resolved") from error
    addresses = sorted({answer[4][0] for answer in answers})
    if not addresses:
        raise FetchRejected("URL host has no address")
    if any(not _public_address(address) for address in addresses):
        raise FetchRejected("URL resolves to a non-public address")
    ip = addresses[0]
    target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    default_port = 443 if scheme == "https" else 80
    host_header = hostname if port == default_port else f"{hostname}:{port}"
    return ResolvedURL(
        url=urllib.parse.urlunsplit(
            (scheme, parsed.netloc, parsed.path or "/", parsed.query, "")
        ),
        scheme=scheme,
        hostname=hostname,
        port=port,
        ip=ip,
        target=target,
        host_header=host_header,
    )


def _public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def fetch_public(
    url: str,
    *,
    resolver=socket.getaddrinfo,
    requester=None,
    max_bytes: int = schema.MAX_ARTIFACT_BYTES,
) -> FetchedArtifact:
    request = requester or _request
    current = url
    for redirect_count in range(MAX_REDIRECTS + 1):
        resolved = resolve_url(current, resolver=resolver)
        response = request(resolved)
        try:
            if response.status in REDIRECTS:
                if redirect_count >= MAX_REDIRECTS:
                    raise FetchRejected("URL has too many redirects")
                location = response.headers.get("location", "")
                if not location:
                    raise FetchRejected("URL redirect has no destination")
                current = urllib.parse.urljoin(resolved.url, location)
                continue
            if response.status < 200 or response.status >= 300:
                raise FetchRejected(f"URL returned HTTP {response.status}")
            length = response.headers.get("content-length")
            if length:
                try:
                    if int(length) > max_bytes:
                        raise FetchRejected("URL response exceeds the artifact limit")
                except ValueError as error:
                    raise FetchRejected("URL returned an invalid content length") from error
            chunks = []
            total = 0
            for chunk in response.stream(CHUNK_BYTES):
                total += len(chunk)
                if total > max_bytes:
                    raise FetchRejected("URL response exceeds the artifact limit")
                chunks.append(chunk)
            media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            return FetchedArtifact(
                data=b"".join(chunks),
                final_url=without_capability(resolved.url),
                response_media_type=media_type,
            )
        finally:
            response.release_conn()
    raise FetchRejected("URL has too many redirects")


def _request(resolved: ResolvedURL):
    import urllib3

    timeout = urllib3.Timeout(connect=CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S)
    headers = {
        "Host": resolved.host_header,
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    }
    if resolved.scheme == "https":
        pool = urllib3.HTTPSConnectionPool(
            resolved.ip,
            port=resolved.port,
            assert_hostname=resolved.hostname,
            server_hostname=resolved.hostname,
            cert_reqs="CERT_REQUIRED",
            timeout=timeout,
            maxsize=1,
        )
    else:
        pool = urllib3.HTTPConnectionPool(
            resolved.ip,
            port=resolved.port,
            timeout=timeout,
            maxsize=1,
        )
    return pool.urlopen(
        "GET",
        resolved.target,
        headers=headers,
        preload_content=False,
        redirect=False,
        retries=False,
    )
