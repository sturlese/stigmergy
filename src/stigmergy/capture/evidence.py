"""The evidence plane: an S3-compatible, content-addressed archive of every capture's raw
material.

The librarian reads a capture's material from HERE, not from the queue row, and the material
must outlive the row: retention NULLs `payload`/`hints`, and git must never hold the
conversation (git cannot delete). Keys:

    key = sha256/<first two hex>/<next two hex>/<full sha256 hex>

Content addressing: identical material occupies ONE object while still producing two queue rows
(dedup against the graph is the librarian's judgment, not a storage side effect); the key is
verifiable by re-hashing; the fan-out keeps prefix listings small. Backends: MinIO locally, R2
in staging — same code, only the four environment values differ.

Errors are reduced to a class name on the way out: boto3's exceptions embed the endpoint,
bucket and access key id, none of which may reach the wire. `MemoryEvidenceStore` is the
offline double — a double that hand-rolled the key scheme would prove the double, not the store.
"""
import hashlib
import logging
import os

from stigmergy.capture.errors import EvidenceError

log = logging.getLogger(__name__)

KEY_PREFIX = "sha256"

# Local defaults match the `minio` service in docker-compose.yml, so a submit works with zero
# configuration; staging overrides all four. Nothing here is a secret — `minioadmin` is the
# compose file's own value.
ENDPOINT_ENV = "STIGMERGY_EVIDENCE_ENDPOINT"
BUCKET_ENV = "STIGMERGY_EVIDENCE_BUCKET"
ACCESS_KEY_ENV = "STIGMERGY_EVIDENCE_ACCESS_KEY_ID"
SECRET_KEY_ENV = "STIGMERGY_EVIDENCE_SECRET_ACCESS_KEY"

ENDPOINT_DEFAULT = "http://127.0.0.1:9000"

BUCKET_DEFAULT = "stigmergy-evidence"
ACCESS_KEY_DEFAULT = "minioadmin"
SECRET_KEY_DEFAULT = "minioadmin"

# These bound a STALL, not a slow upload: `put`/`get` run as a SYNC tool body on the server's
# event loop, so a degraded store freezes the single process serving every identity (boto3's
# defaults make that minutes). botocore reads `max_attempts` as RETRIES (+1), so the bound is
# the PRODUCT — written as the arithmetic because three constants nobody multiplies is how a
# 30-second bound quietly becomes a three-minute one.
CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 10
RETRIES = 1
WORST_CASE_STALL_S = (RETRIES + 1) * (CONNECT_TIMEOUT_S + READ_TIMEOUT_S)

_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound", "NoSuchBucket"}


def content_key(data: bytes) -> str:
    """The content address of `data`. Pure: no store, no network — a caller can compute the key a
    submission WILL have (and a verifier can recompute it from the bytes it read back)."""
    digest = hashlib.sha256(data).hexdigest()
    return f"{KEY_PREFIX}/{digest[:2]}/{digest[2:4]}/{digest}"


class MemoryEvidenceStore:
    """The offline double: same surface, a dict instead of a bucket.

    `bucket`/`endpoint_url` are part of that surface, not decoration: a report names the store a
    capture's bytes went to, so a double without them can only be driven through code paths that
    never say where anything was stored.
    """

    bucket = "memory"
    # Loopback-shaped on purpose: the double stands in for the LOCAL store, so the drop doors'
    # split-stores guard must read it exactly as it reads MinIO.
    endpoint_url = "http://127.0.0.1:9000"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, data: bytes) -> str:
        key = content_key(data)
        self.objects.setdefault(key, data)   # identical bytes -> one object, like the real store
        return key

    def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise EvidenceError("evidence object not found")
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects


class S3EvidenceStore:
    """The real store over any S3-compatible endpoint (MinIO, R2).

    The boto3 client is built lazily and cached: constructing this object does no I/O, so a
    server whose bucket is unreachable still serves every READ tool — only a submit fails, with
    a clean error rather than a startup crash. `client` is injectable for tests.
    """

    def __init__(self, *, endpoint_url: str, bucket: str, access_key_id: str,
                 secret_access_key: str, region: str = "auto", client=None):
        self.endpoint_url = endpoint_url
        self.bucket = bucket
        self.region = region
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._client = client

    def client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config
            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self._access_key_id,
                aws_secret_access_key=self._secret_access_key,
                region_name=self.region,   # R2's convention: the endpoint carries the routing
                config=Config(connect_timeout=CONNECT_TIMEOUT_S, read_timeout=READ_TIMEOUT_S,
                              retries={"max_attempts": RETRIES, "mode": "standard"}),
            )
        return self._client

    def put(self, data: bytes) -> str:
        """Archive `data` and return its content key. Skips the upload when the object is already
        there — identical material submitted twice costs one round trip, not one more copy."""
        key = content_key(data)
        if self.exists(key):
            return key
        self._guard("put", key, lambda: self.client().put_object(
            Bucket=self.bucket, Key=key, Body=data))
        return key

    def get(self, key: str) -> bytes:
        return self._guard("get", key,
                           lambda: self.client().get_object(
                               Bucket=self.bucket, Key=key)["Body"].read())

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError
        try:
            self.client().head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as ex:
            if str(ex.response.get("Error", {}).get("Code", "")) in _NOT_FOUND_CODES:
                return False
            return self._fail("head", key, ex)
        except Exception as ex:  # noqa: BLE001 — BotoCoreError et al: same redaction
            return self._fail("head", key, ex)

    def _guard(self, op: str, key: str, call):
        try:
            return call()
        except Exception as ex:  # noqa: BLE001 — every boto exception embeds endpoint/bucket/key id
            return self._fail(op, key, ex)

    def _fail(self, op: str, key: str, ex: Exception):
        # Full detail in the server-side log; class name only on the wire — never `str(ex)`,
        # which carries the bucket, endpoint and credentials.
        log.error("evidence store %s failed (bucket=%s endpoint=%s key=%s)",
                  op, self.bucket, self.endpoint_url, key, exc_info=True)
        raise EvidenceError(f"evidence store unavailable ({ex.__class__.__name__})") from ex


def store_from_env(env: dict | None = None) -> S3EvidenceStore:
    """Build the configured store. `env` is injectable, and this is a function called from the
    entry point — modules never read the environment at import."""
    env = os.environ if env is None else env
    return S3EvidenceStore(
        endpoint_url=env.get(ENDPOINT_ENV) or ENDPOINT_DEFAULT,
        bucket=env.get(BUCKET_ENV) or BUCKET_DEFAULT,
        access_key_id=env.get(ACCESS_KEY_ENV) or ACCESS_KEY_DEFAULT,
        secret_access_key=env.get(SECRET_KEY_ENV) or SECRET_KEY_DEFAULT,
    )
