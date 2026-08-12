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
import ipaddress
import logging
import os
import socket

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


# ── the two halves have to belong to the same deployment ──────────────────────────────────────
# The queue and the evidence plane are configured independently, and a drop with the evidence
# group unset puts the row in the cloud and the bytes on the operator's laptop — the deployed
# worker then dies on `NoSuchKey` after the material has been consumed (the repo's own `.env`
# carries the bucket under `R2_*` names, so sourcing it looks like configuration and is not).
# Deliberately ONE combination, not a general consistency check: a remote database with a
# loopback evidence endpoint is never right, while the mirror case (local database, remote
# bucket) is odd but works — a guard that fires on the merely unusual gets disabled.
_LOOPBACK_HOSTS = ("localhost", "0.0.0.0", "[::1]", "::1")


def host_of(endpoint_url: str) -> str:
    """The host of an S3 ENDPOINT URL, lowercased, port and path stripped. Not for DSNs: those
    are `index.store.host_of_dsn`'s job through libpq's parser, because the keyword form carries
    a password that string surgery here would hand straight to a printed sentence."""
    value = (endpoint_url or "").strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0].split("?", 1)[0]
    if "@" in value:
        value = value.rsplit("@", 1)[1]
    if value.startswith("["):                       # bracketed IPv6, port outside the brackets
        return value.split("]", 1)[0] + "]"
    return value.rsplit(":", 1)[0] if ":" in value else value


def is_loopback_host(host: str) -> bool:
    """Does this host name the machine asking? Literal spellings first, then a real address test
    (`ipaddress`) — a `startswith("127.")` string test misses the 127/8 and IPv6 spellings and
    classifies `127.evil.com` as local."""
    value = (host or "").strip().lower().rstrip(".")
    if value in _LOOPBACK_HOSTS:
        return True
    literal = value.strip("[]")
    try:
        return ipaddress.ip_address(literal).is_loopback
    except ValueError:
        pass
    # `ipaddress` refuses abbreviated IPv4 (`127.1`, decimal/octal/hex forms) that are WORKING
    # local endpoints; reading them as remote would admit the cloud-queue/laptop-evidence pair.
    # `inet_aton` accepts exactly that classic family and still rejects a hostname, so
    # `127.evil.com` stays remote.
    try:
        return ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(literal))).is_loopback
    except OSError:
        return False


def is_loopback(endpoint_url: str) -> bool:
    """`is_loopback_host` over an endpoint URL."""
    return is_loopback_host(host_of(endpoint_url))


def split_stores_reason(*, db_host: str, endpoint_url: str) -> str:
    """`""` when the queue and the evidence plane can belong to the same deployment, and the
    sentence explaining the refusal when they provably cannot.

    Takes a HOST, never a DSN — no parsing of a credential-bearing string this would then
    interpolate into a message. Fails open in both directions: an empty `db_host` or a
    unix-socket directory can only be local, and only a positively-remote host against a
    positively-loopback endpoint is refused.
    """
    if not db_host or db_host.startswith("/") or is_loopback_host(db_host):
        return ""
    if not is_loopback(endpoint_url):
        return ""
    return (
        f"the queue is at {db_host} but evidence would upload to {host_of(endpoint_url)} — a "
        f"remote worker can never read a store on this machine, so this capture would fail with "
        f"NoSuchKey seconds after it is claimed.\n"
        f"  export the deployment's own evidence group before dropping:\n"
        f"    {ENDPOINT_ENV} {BUCKET_ENV} {ACCESS_KEY_ENV} {SECRET_KEY_ENV}\n"
        f"  (the repo's .env keeps these under R2_* names for `make r2-smoke`, so sourcing it "
        f"does NOT set them)\n"
        f"  or pass --allow-split-stores if you really mean to split them.")


def content_key(data: bytes) -> str:
    """The content address of `data`. Pure: no store, no network — a caller can compute the key a
    submission WILL have (and a verifier can recompute it from the bytes it read back)."""
    digest = hashlib.sha256(data).hexdigest()
    return f"{KEY_PREFIX}/{digest[:2]}/{digest[2:4]}/{digest}"


class MemoryEvidenceStore:
    """The offline double: same surface, a dict instead of a bucket."""

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
