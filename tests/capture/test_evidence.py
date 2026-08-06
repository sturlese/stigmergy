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


def test_s3_store_failure_is_logged_server_side_with_the_full_detail(caplog):
    """The detail that must never reach the wire is exactly what the operator NEEDS server-side — proven
    the same way `tests/server/test_audit.py` proves the audit writer's swallow-and-log split."""
    store = _store_with_boom("real cause: DNS resolution failed for 10.0.0.1")
    with caplog.at_level(logging.ERROR), pytest.raises(EvidenceError):
        store.put(b"material")
    assert any("10.0.0.1" in r.message or "evidence store" in r.message for r in caplog.records)
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


# ── the queue and the evidence plane have to belong to the same deployment ─────────────────────
# Live on staging: a drop queued against the staging database while uploading the bytes to the
# operator's own MinIO, because the two halves are configured by INDEPENDENT env vars and nothing
# checked they named the same world. The capture died 8s later with NoSuchKey — queue row in the
# cloud, evidence on a laptop.
@pytest.mark.parametrize("endpoint", [
    "http://localhost:9000", "http://127.0.0.1:9000", "http://127.0.0.2:9000",
    "http://0.0.0.0:9000", "http://[::1]:9000", "https://LOCALHOST:9000",
])
def test_a_remote_queue_with_a_loopback_evidence_store_is_refused(endpoint):
    reason = evidence.split_stores_reason(
        db_host="db.abcdef.supabase.co", endpoint_url=endpoint)
    assert reason
    assert "db.abcdef.supabase.co" in reason
    assert "--allow-split-stores" in reason
    assert evidence.ENDPOINT_ENV in reason          # names what to export, not just what is wrong


@pytest.mark.parametrize("db_host,endpoint", [
    # the compose default: both halves on this machine — the everyday local path
    ("localhost", "http://127.0.0.1:9000"),
    # both remote: the deployment's own shape
    ("db.abcdef.supabase.co", "https://acc.r2.cloudflarestorage.com"),
    # a LOCAL queue with a remote bucket: odd, but it works — a guard that fires on the merely
    # unusual gets disabled, so this one deliberately does not fire
    ("localhost", "https://acc.r2.cloudflarestorage.com"),
])
def test_every_workable_combination_is_left_alone(db_host, endpoint):
    assert evidence.split_stores_reason(db_host=db_host, endpoint_url=endpoint) == ""


@pytest.mark.parametrize("db_host", ["", "/var/run/postgresql", "localhost", "127.0.0.2", "::1",
                                     "0:0:0:0:0:0:0:1", "LOCALHOST."])
def test_a_host_that_is_not_positively_remote_never_refuses(db_host):
    """The rule, applied where it actually bites: this predicate REFUSES when the queue looks
    remote, so anything it cannot positively read as remote — PG* defaults, a unix socket, an
    unreadable connstring — must pass. Failing closed here would block the everyday local drop
    and, worse, tell the operator to go export R2 credentials to fix it."""
    assert evidence.split_stores_reason(db_host=db_host,
                                        endpoint_url="http://127.0.0.1:9000") == ""


@pytest.mark.parametrize("endpoint", ["http://[0:0:0:0:0:0:0:1]:9000", "http://localhost.:9000",
                                      "http://127.255.255.254:9000"])
def test_loopback_is_an_address_test_not_a_string_prefix(endpoint):
    assert evidence.is_loopback(endpoint)


def test_a_host_that_merely_starts_with_127_is_not_loopback():
    """The benign twin of the address test: `127.evil.com` is a perfectly ordinary remote name,
    and a string-prefix check would have silently disabled the guard for it."""
    assert not evidence.is_loopback_host("127.evil.com")


def test_host_of_strips_credentials_ports_and_paths():
    assert evidence.host_of("https://acc.r2.cloudflarestorage.com/bucket") == (
        "acc.r2.cloudflarestorage.com")
    assert evidence.host_of("http://[::1]:9000") == "[::1]"
