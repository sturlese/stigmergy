import socket

import pytest
from urllib3.exceptions import (
    ConnectTimeoutError,
    NewConnectionError,
    ProtocolError,
    ReadTimeoutError,
    SSLError,
)

from stigmergy.capture import errors
from stigmergy.capture.errors import FetchRejected
from stigmergy.capture.fetch import fetch_public, resolve_url


def _resolver(*addresses):
    def resolve(host, port, type):
        assert type == socket.SOCK_STREAM
        return [
            (socket.AF_INET6 if ":" in address else socket.AF_INET, type, 6, "", (address, port))
            for address in addresses
        ]

    return resolve


class _Response:
    def __init__(self, status=200, headers=None, chunks=()):
        self.status = status
        self.headers = headers or {}
        self._chunks = chunks
        self.released = False

    def stream(self, chunk_size):
        assert chunk_size > 0
        return iter(self._chunks)

    def release_conn(self):
        self.released = True


class _StreamingFailureResponse(_Response):
    def stream(self, chunk_size):
        assert chunk_size > 0
        yield b"partial"
        raise OSError("stream failed for signature=secret")


class _Urllib3StreamingFailureResponse(_Response):
    def __init__(self, error):
        super().__init__()
        self.error = error

    def stream(self, chunk_size):
        assert chunk_size > 0
        yield b"partial"
        raise self.error


class _MissingFetchUnavailable(Exception):
    """Makes each red case explicit until production provides the domain error."""


def _raise(error):
    def fail(*_args, **_kwargs):
        raise error

    return fail


def _fetch_unavailable_type():
    return getattr(errors, "FetchUnavailable", _MissingFetchUnavailable)


_STREAMING_FAILURE_RESPONSE = _StreamingFailureResponse()


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "::1",
        "fe80::1",
    ],
)
def test_private_and_metadata_addresses_are_blocked(address):
    with pytest.raises(FetchRejected, match="non-public"):
        resolve_url("https://files.example/report.pdf", resolver=_resolver(address))


def test_a_mixed_public_private_dns_answer_is_rejected():
    with pytest.raises(FetchRejected, match="non-public"):
        resolve_url(
            "https://files.example/report.pdf",
            resolver=_resolver("93.184.216.34", "127.0.0.1"),
        )


def test_fetch_connects_to_the_validated_address_and_preserves_host():
    seen = []
    response = _Response(
        headers={"content-type": "application/pdf", "content-length": "8"},
        chunks=(b"%PDF-1.7",),
    )

    def request(resolved):
        seen.append(resolved)
        return response

    result = fetch_public(
        "https://files.example/report.pdf",
        resolver=_resolver("93.184.216.34"),
        requester=request,
    )

    assert result.data == b"%PDF-1.7"
    assert result.final_url == "https://files.example/report.pdf"
    assert seen[0].ip == "93.184.216.34"
    assert seen[0].host_header == "files.example"
    assert response.released is True


def test_fetch_uses_a_capability_query_but_does_not_return_it_as_provenance():
    seen = []
    response = _Response(chunks=(b"report",))

    result = fetch_public(
        "https://files.example/report.pdf?signature=secret#viewer",
        resolver=_resolver("93.184.216.34"),
        requester=lambda resolved: seen.append(resolved) or response,
    )

    assert seen[0].target == "/report.pdf?signature=secret"
    assert result.final_url == "https://files.example/report.pdf"


def test_every_redirect_is_resolved_and_revalidated():
    responses = iter(
        [
            _Response(status=302, headers={"location": "http://127.0.0.1/secret"}),
        ]
    )

    with pytest.raises(FetchRejected, match="non-public"):
        fetch_public(
            "https://files.example/start",
            resolver=lambda host, port, type: (
                _resolver("93.184.216.34")(host, port, type)
                if host == "files.example"
                else _resolver("127.0.0.1")(host, port, type)
            ),
            requester=lambda resolved: next(responses),
        )


def test_streaming_limit_is_enforced_without_trusting_content_length():
    response = _Response(chunks=(b"a" * 6, b"b" * 6))
    with pytest.raises(FetchRejected, match="exceeds"):
        fetch_public(
            "https://files.example/data",
            resolver=_resolver("93.184.216.34"),
            requester=lambda resolved: response,
            max_bytes=10,
        )


@pytest.mark.parametrize(
    ("resolver", "requester", "response"),
    [
        pytest.param(
            _raise(OSError("resolver unavailable for signature=secret")),
            lambda _resolved: pytest.fail("requester must not run after DNS failure"),
            None,
            id="dns-oserror",
        ),
        pytest.param(
            _resolver("93.184.216.34"),
            _raise(TimeoutError("request timed out for signature=secret")),
            None,
            id="request-timeout",
        ),
        pytest.param(
            _resolver("93.184.216.34"),
            lambda _resolved: _STREAMING_FAILURE_RESPONSE,
            _STREAMING_FAILURE_RESPONSE,
            id="stream-oserror",
        ),
    ],
)
def test_transient_transport_failures_are_retryable_and_safe(resolver, requester, response):
    with pytest.raises(_fetch_unavailable_type()) as raised:
        fetch_public(
            "https://files.example/report.pdf?signature=secret",
            resolver=resolver,
            requester=requester,
        )

    assert str(raised.value) == "public URL is temporarily unavailable"
    assert len(str(raised.value)) <= 80
    assert "files.example" not in str(raised.value)
    assert "signature" not in str(raised.value)
    assert "secret" not in str(raised.value)
    if response is not None:
        assert response.released is True


@pytest.mark.parametrize(
    ("stage", "error"),
    [
        pytest.param(
            "request",
            ConnectTimeoutError(None, "https://files.example", "connect signature=secret"),
            id="connect-timeout",
        ),
        pytest.param(
            "request",
            NewConnectionError(None, "connect signature=secret"),
            id="new-connection",
        ),
        pytest.param(
            "stream",
            ReadTimeoutError(None, "https://files.example", "read signature=secret"),
            id="read-timeout",
        ),
        pytest.param(
            "stream",
            ProtocolError("stream", OSError("protocol signature=secret")),
            id="protocol-error",
        ),
        pytest.param(
            "request",
            SSLError("TLS certificate signature=secret"),
            id="tls-request",
        ),
        pytest.param(
            "stream",
            SSLError("TLS certificate signature=secret"),
            id="tls-stream",
        ),
    ],
)
def test_urllib3_transport_failures_are_retryable_and_safe(stage, error):
    response = None
    if stage == "request":
        requester = _raise(error)
    else:
        response = _Urllib3StreamingFailureResponse(error)

        def requester(_resolved):
            return response

    with pytest.raises(_fetch_unavailable_type()) as raised:
        fetch_public(
            "https://files.example/report.pdf?signature=secret",
            resolver=_resolver("93.184.216.34"),
            requester=requester,
        )

    assert raised.value.category == "fetch_unavailable"
    assert str(raised.value) == "public URL is temporarily unavailable"
    assert raised.value.__cause__ is error
    if response is not None:
        assert response.released is True
