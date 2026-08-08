"""The evidence plane: an S3-compatible, content-addressed archive of every capture's raw material.

Why it exists: the librarian reads a capture's material from HERE, not from the queue row
(`processing` resolves `blob_refs` through this store), and the raw material has to outlive the
row. Retention NULLs `payload`/`hints` 30 days after a row goes terminal, and git must never hold
the conversation in the first place — git cannot delete. So the raw text gets a home with its own
lifecycle, addressed by what it IS rather than by who sent it:

    key = sha256/<first two hex>/<next two hex>/<full sha256 hex>

Content addressing buys three things for free: identical material submitted twice occupies
exactly ONE object while still producing two queue rows (dedup against the graph is the
librarian's judgment, not a storage side effect); the key is verifiable (re-hash the bytes and
compare); and the two-level fan-out keeps any single prefix listing small.

Backends: MinIO locally (docker-compose, defaults below), an R2 bucket in staging. Same code,
same protocol — only the four environment values differ, which is why `boto3` is a dependency at
all (`scripts/r2_smoke.py` proves the credentials against the real bucket independently of this
writer).

**Errors are reduced to a class name on the way out.** The exceptions boto3 raises embed the
endpoint URL, the bucket name and the access key id, none of which may reach the wire; the real
cause is logged server-side. `MemoryEvidenceStore` is the offline double the fast test suite uses
(same `put/get/exists` surface) — production ships its own fake here for the same reason
`stigmergy.index.backends.fake_embedder` does: a test double that hand-rolls the key scheme would
prove the double, not the store.
"""
import hashlib
import ipaddress
import logging
import os
import socket

from stigmergy.capture.errors import EvidenceError

log = logging.getLogger(__name__)

KEY_PREFIX = "sha256"

# Local defaults match the `minio` service in docker-compose.yml, exactly as
# `stigmergy.index.store.DSN_DEFAULT` matches the `postgres` service: `make db-up` and a submit
# work with zero configuration, and staging overrides all four with real R2 values (Fly secrets;
# see docs/reference/operator-runbook.md). Nothing here is a secret — `minioadmin` is the compose
# file's own value, the same posture as the `stigmergy:stigmergy` DSN.
ENDPOINT_ENV = "STIGMERGY_EVIDENCE_ENDPOINT"
BUCKET_ENV = "STIGMERGY_EVIDENCE_BUCKET"
ACCESS_KEY_ENV = "STIGMERGY_EVIDENCE_ACCESS_KEY_ID"
SECRET_KEY_ENV = "STIGMERGY_EVIDENCE_SECRET_ACCESS_KEY"

ENDPOINT_DEFAULT = "http://127.0.0.1:9000"

BUCKET_DEFAULT = "stigmergy-evidence"
ACCESS_KEY_DEFAULT = "minioadmin"
SECRET_KEY_DEFAULT = "minioadmin"

# **These bound a STALL, not a slow upload.** `put`/`get` are reached from `brain_submit`, which
# the MCP SDK invokes as a SYNC tool body — directly on the event loop, with no threadpool. So a
# degraded object store does not slow one caller's submit: it freezes the single process serving
# every other identity. boto3's defaults (60 s connect, 60 s read, retrying) make that minutes.
#
# botocore reads `max_attempts` as RETRIES and resolves it to `total_max_attempts = RETRIES + 1`,
# so the bound is the PRODUCT, not any one number. Written as the arithmetic because three
# constants nobody multiplies is how a 30-second bound quietly becomes a three-minute one.
CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 10
RETRIES = 1
WORST_CASE_STALL_S = (RETRIES + 1) * (CONNECT_TIMEOUT_S + READ_TIMEOUT_S)

_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound", "NoSuchBucket"}


# ── the two halves have to belong to the same deployment ──────────────────────────────────────
# The queue and the evidence plane are configured INDEPENDENTLY (`$STIGMERGY_INDEX_DSN` here, the
# four `STIGMERGY_EVIDENCE_*` above), and nothing used to check they name the same world. A drop
# against staging with the evidence group unset uploaded the bytes to the operator's own laptop
# and put the row in Fly; the deployed worker then looked for a key that was never there and the
# capture died 8 seconds later with `NoSuchKey`. Queue row in the cloud, evidence on a laptop:
# guaranteed failure, discovered only after the material had been consumed.
#
# The mistake is easy and silent, which is why this is a guard and not a paragraph: the repo's own
# `.env` carries the bucket under `R2_*` names (for `make r2-smoke`) while the code reads
# `STIGMERGY_EVIDENCE_*`, so `set -a; source .env` LOOKS like it configured the deployment's store
# and instead leaves it pointing at whatever local MinIO that file names.
#
# Deliberately ONE combination, not a general consistency check: a remote database with a
# loopback evidence endpoint is never right, because the deployed worker structurally cannot
# reach a loopback address on somebody else's machine. The mirror case (a local database with a
# remote bucket) is odd but works, so it is not refused — a guard that fires on the merely
# unusual gets disabled.
_LOOPBACK_HOSTS = ("localhost", "0.0.0.0", "[::1]", "::1")


def host_of(endpoint_url: str) -> str:
    """The host of an S3 ENDPOINT URL, lowercased, port and path stripped. Deliberately not a URL
    parser and deliberately not for DSNs: a DSN's host is `index.store.host_of_dsn`'s job, through
    libpq's own parser, because the keyword form carries a password that string surgery here would
    hand straight to a printed sentence."""
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
    """Does this host name the machine asking? Literal spellings first, then a REAL address test
    (`ipaddress`) so the whole 127/8, `::1`, `0:0:0:0:0:0:0:1`, `::ffff:127.0.0.1` and the octal/
    decimal/hex spellings of 127.0.0.1 all resolve correctly — a `startswith("127.")` string test
    both misses those and classifies `127.evil.com` as local."""
    value = (host or "").strip().lower().rstrip(".")
    if value in _LOOPBACK_HOSTS:
        return True
    literal = value.strip("[]")
    try:
        return ipaddress.ip_address(literal).is_loopback
    except ValueError:
        pass
    # `ipaddress` is deliberately STRICT: it takes full dotted-quad IPv4 and nothing else, so it
    # refuses `127.1`, `2130706433`, `0x7f000001` and `017700000001` — every abbreviated spelling
    # the docstring above promises. `socket.getaddrinfo` resolves all four to 127.0.0.1, so each
    # one is a WORKING local endpoint, and reading them as REMOTE is the direction that hurts:
    # `split_stores_reason` then admits the cloud-queue/laptop-evidence pair it exists to refuse.
    # `inet_aton` accepts exactly this classic family and still rejects a HOSTNAME, so
    # `127.evil.com` stays remote — the case the string test this replaced got wrong.
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

    Takes a HOST, never a DSN: this module owns the S3 endpoint and has no business parsing a
    credential-bearing string it would then interpolate into a message.

    **Fails open in both directions.** A `db_host` that is empty (PG* defaults, an unreadable
    connstring) or a unix-socket directory names no remote machine, so it can only be local —
    refusing there would block the everyday local drop and, worse, tell the operator to go export
    R2 credentials. Only a host we can positively read as remote, against an endpoint we can
    positively read as loopback, is refused.
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
    """The offline double: same surface, a dict instead of a bucket. Keeps the fast suite fast
    and keyless, and gives any caller an injectable store."""

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

    The boto3 client is built lazily and cached: constructing this object does no I/O, so
    `build_service` can wire one unconditionally and a server whose bucket is unreachable still
    serves every READ tool — only a submit fails, and it fails with a clean error rather than a
    startup crash. `client` is injectable for tests that want to drive a stub without boto3.
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
        # Server-side log: the full detail, including the bucket and endpoint the operator needs.
        # Wire-side message: the class name only — never the bucket, endpoint, credentials or the
        # key, and never `str(ex)`, which carries all of them.
        log.error("evidence store %s failed (bucket=%s endpoint=%s key=%s)",
                  op, self.bucket, self.endpoint_url, key, exc_info=True)
        raise EvidenceError(f"evidence store unavailable ({ex.__class__.__name__})") from ex


def store_from_env(env: dict | None = None) -> S3EvidenceStore:
    """Build the configured store. `env` is injectable so a caller can pass an explicit mapping
    instead of the process environment (the modules-never-read-the-environment-at-import rule:
    this is a function, called from the entry point, exactly like `Settings.from_args`)."""
    env = os.environ if env is None else env
    return S3EvidenceStore(
        endpoint_url=env.get(ENDPOINT_ENV) or ENDPOINT_DEFAULT,
        bucket=env.get(BUCKET_ENV) or BUCKET_DEFAULT,
        access_key_id=env.get(ACCESS_KEY_ENV) or ACCESS_KEY_DEFAULT,
        secret_access_key=env.get(SECRET_KEY_ENV) or SECRET_KEY_DEFAULT,
    )
