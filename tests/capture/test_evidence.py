"""`stigmergy.capture.evidence` — the content-addressed evidence plane.

Two tiers: MinIO in compose for the evidence tests, an in-memory store double for the unit tests
so the fast suite stays fast.

- `content_key`/`MemoryEvidenceStore`: pure, keyless, no I/O — the fast suite.
- `S3EvidenceStore` against the REAL `minio` compose service: the object-COUNT assertion needs a
  real bucket — a double that hand-rolled `put`/`exists` would prove the double, not the store.
  Skips cleanly without MinIO reachable.

The redaction path (`_fail`: never a bucket/endpoint/credential/raw key on the wire) is proven
with an injected stub `client` — the boundary under test is "does `EvidenceError`'s message stay
value-free", not "does boto3 raise a ClientError", so a stub standing in for the NETWORK boundary
is the right double here (not a mock of internal logic — `S3EvidenceStore` IS the external-service
adapter, and `client=` is its own documented injection seam).
"""
import logging

import pytest

from stigmergy.capture import evidence
from stigmergy.capture.errors import EvidenceError
from stigmergy.capture.evidence import MemoryEvidenceStore, S3EvidenceStore, content_key
from tests.capture.conftest import minio_or_skip, unique_material


# ── content_key: pure, verifiable, no store ─────────────────────────────────────────────────────
def test_content_key_matches_the_documented_scheme():
    import hashlib
    data = b"hello brain"
    digest = hashlib.sha256(data).hexdigest()
    assert content_key(data) == f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"


def test_content_key_is_deterministic_and_collision_sensitive():
    assert content_key(b"same bytes") == content_key(b"same bytes")
    assert content_key(b"same bytes") != content_key(b"different bytes")


# ── MemoryEvidenceStore: the offline double, same surface as the real store ────────────────────
def test_memory_store_put_then_get_roundtrips():
    store = MemoryEvidenceStore()
    key = store.put(b"raw capture material")
    assert store.get(key) == b"raw capture material"
    assert store.exists(key) is True


def test_memory_store_identical_bytes_produce_one_object():
    """The property this plane exists for: identical material -> exactly one object."""
    store = MemoryEvidenceStore()
    key1 = store.put(b"same capture")
    key2 = store.put(b"same capture")
    assert key1 == key2
    assert len(store.objects) == 1


def test_memory_store_get_of_a_missing_key_raises_evidence_error():
    store = MemoryEvidenceStore()
    with pytest.raises(EvidenceError):
        store.get("sha256/00/00/nonexistent")


def test_memory_store_exists_is_false_for_a_key_never_put():
    store = MemoryEvidenceStore()
    assert store.exists("sha256/00/00/nonexistent") is False


def test_presigned_upload_binds_the_declared_content_length():
    class PresignClient:
        def __init__(self):
            self.params = None

        def generate_presigned_url(self, _operation, *, Params, ExpiresIn):
            self.params = {"Params": Params, "ExpiresIn": ExpiresIn}
            return "https://upload.example/object"

    client = PresignClient()
    store = S3EvidenceStore(
        endpoint_url="https://objects.example",
        bucket="evidence",
        access_key_id="key",
        secret_access_key="secret",
        client=client,
    )
    key = content_key(b"declared bytes")

    url = store.presign_put(key, bytes=14)

    assert url == "https://upload.example/object"
    assert client.params["Params"]["ContentLength"] == 14


# ── S3EvidenceStore: every network failure crossing the boundary is class-name-only ─────────────
class _BoomClient:
    """Stands in for the boto3 client the network boundary actually is: every call raises an
    exception whose message embeds exactly what must never reach the wire (a realistic boto3
    ClientError shape does this too — this stub is simpler and just as sufficient for proving the
    REDACTION, which is `S3EvidenceStore`'s own job, not boto3's)."""

    def __init__(self, message: str):
        self._message = message

    def put_object(self, **kwargs):
        raise RuntimeError(self._message)

    def get_object(self, **kwargs):
        raise RuntimeError(self._message)

    def head_object(self, **kwargs):
        raise RuntimeError(self._message)


def _store_with_boom(message: str) -> S3EvidenceStore:
    return S3EvidenceStore(endpoint_url="http://10.0.0.1:9000", bucket="secret-bucket",
                           access_key_id="AKIA_REAL_KEY", secret_access_key="real-secret",
                           client=_BoomClient(message))


def test_s3_store_put_failure_redacts_endpoint_bucket_and_credentials_from_the_wire_message():
    store = _store_with_boom("connection to 10.0.0.1:9000 refused, key AKIA_REAL_KEY invalid")
    with pytest.raises(EvidenceError) as exc_info:
        store.put(b"material")
    message = str(exc_info.value)
    assert "10.0.0.1" not in message
    assert "secret-bucket" not in message
    assert "AKIA_REAL_KEY" not in message
    assert message == "evidence store unavailable (RuntimeError)"   # the class name and nothing else


def test_s3_store_get_failure_is_also_redacted():
    store = _store_with_boom("bucket secret-bucket has no such key")
    with pytest.raises(EvidenceError, match=r"evidence store unavailable \(RuntimeError\)"):
        store.get("sha256/ab/cd/somehash")


def test_s3_store_exists_swallows_a_not_found_client_error_as_false():
    """`exists()` distinguishes a real not-found (`ClientError` with a 404-shaped code) from every
    other failure — only the former becomes `False`; everything else still raises, redacted."""
    from botocore.exceptions import ClientError
    client = _BoomClient("unused")
    client.head_object = lambda **kw: (_ for _ in ()).throw(
        ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"))
    store = S3EvidenceStore(endpoint_url="http://10.0.0.1:9000", bucket="b", access_key_id="k",
                            secret_access_key="s", client=client)
    assert store.exists("sha256/ab/cd/x") is False


def test_s3_store_failure_logs_the_operation_without_secret_values(caplog):
    store = _store_with_boom(
        "real cause: DNS resolution failed for 10.0.0.1, bucket secret-bucket, "
        "key AKIA_REAL_KEY"
    )
    with caplog.at_level(logging.ERROR), pytest.raises(EvidenceError):
        store.put(b"material")
    assert any("evidence operation failed" in r.message for r in caplog.records)
    assert "10.0.0.1" not in caplog.text
    assert "secret-bucket" not in caplog.text
    assert "AKIA_REAL_KEY" not in caplog.text
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_store_from_env_uses_an_injected_mapping_and_never_touches_the_real_process_environment(
        monkeypatch):
    """`store_from_env`'s `env=` parameter is the seam that makes it safe for a LIBRARY function
    to read configuration without reading the process environment directly at import time (see
    `tests/test_architecture.py`'s capture module-scope check): passing an explicit mapping must
    win outright, even when the real process environment sets conflicting values."""
    import os

    from stigmergy.capture import evidence as evidence_plane
    monkeypatch.setenv(evidence_plane.ENDPOINT_ENV, "http://real-process-env:9000")
    injected = {evidence_plane.ENDPOINT_ENV: "http://injected:9000",
               evidence_plane.BUCKET_ENV: "injected-bucket"}
    store = evidence_plane.store_from_env(injected)
    assert store.endpoint_url == "http://injected:9000"
    assert store.bucket == "injected-bucket"
    assert os.environ[evidence_plane.ENDPOINT_ENV] == "http://real-process-env:9000"   # untouched


def test_store_from_env_falls_back_to_documented_defaults_with_an_empty_mapping():
    from stigmergy.capture import evidence as evidence_plane
    store = evidence_plane.store_from_env({})
    assert store.endpoint_url == evidence_plane.ENDPOINT_DEFAULT
    assert store.bucket == evidence_plane.BUCKET_DEFAULT


# ── real MinIO (the object-count assertion needs a real bucket) ─────────────────────────────────
def test_real_minio_put_get_and_content_addressed_dedup():
    store = minio_or_skip()
    material = unique_material("evidence-real").encode("utf-8")
    key = store.put(material)
    assert key == content_key(material)
    assert store.exists(key) is True
    assert store.get(key) == material


def test_real_minio_identical_material_is_exactly_one_object_not_two():
    """Against the real bucket: two `put`s of the SAME bytes must not double the
    object count — proven by actually listing the bucket, not by trusting the key alone."""
    store = minio_or_skip()
    material = unique_material("evidence-dedup").encode("utf-8")
    key1 = store.put(material)
    key2 = store.put(material)
    assert key1 == key2
    client = store.client()
    listing = client.list_objects_v2(Bucket=store.bucket, Prefix=key1)
    assert listing.get("KeyCount", len(listing.get("Contents", []))) == 1


def test_real_minio_head_object_of_a_never_written_key_is_false():
    store = minio_or_skip()
    bogus = content_key(unique_material("never-written").encode("utf-8"))
    assert store.exists(bogus) is False


def test_the_s3_client_bounds_how_long_a_degraded_store_can_stall_the_process():
    """boto3's defaults are 60 s connect, 60 s read and a retrying mode — which is a bound on ONE
    caller's patience everywhere else, and a bound on the WHOLE SERVER here.

    `put`/`get` are reached from `brain_submit`, which the MCP SDK invokes as a sync tool body:
    directly on the event loop, no threadpool. So an unreachable object store does not slow one
    submit, it freezes the single process serving every other identity for minutes. Found by a
    pre-publication audit; the numbers are the bound, so they are asserted rather than described.
    """
    store = evidence.S3EvidenceStore(endpoint_url="http://127.0.0.1:9", bucket="b",
                                     access_key_id="k", secret_access_key="s")
    config = store.client().meta.config          # constructing the client does no I/O
    assert config.connect_timeout == evidence.CONNECT_TIMEOUT_S == 5
    assert config.read_timeout == evidence.READ_TIMEOUT_S == 20
    # botocore reads `max_attempts` as RETRIES and resolves it to `total_max_attempts`. The bound
    # that matters is the PRODUCT, so it is what gets asserted: a caller cannot stall the shared
    # process for longer than this, whatever the store is doing.
    assert config.retries["total_max_attempts"] == evidence.RETRIES + 1 == 2
    assert evidence.CONNECT_TIMEOUT_S + evidence.READ_TIMEOUT_S <= 30
