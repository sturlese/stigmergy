"""Private content-addressed evidence storage."""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass

from stigmergy.capture.errors import EvidenceError
from stigmergy.capture.schema import content_ref

log = logging.getLogger(__name__)

ENDPOINT_ENV = "STIGMERGY_EVIDENCE_ENDPOINT"
BUCKET_ENV = "STIGMERGY_EVIDENCE_BUCKET"
ACCESS_KEY_ENV = "STIGMERGY_EVIDENCE_ACCESS_KEY_ID"
SECRET_KEY_ENV = "STIGMERGY_EVIDENCE_SECRET_ACCESS_KEY"

ENDPOINT_DEFAULT = "http://127.0.0.1:9000"
BUCKET_DEFAULT = "stigmergy-evidence"
ACCESS_KEY_DEFAULT = "minioadmin"
SECRET_KEY_DEFAULT = "minioadmin"

CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 20
RETRIES = 1
PRESIGN_TTL_S = 300
READ_CHUNK_BYTES = 1024 * 1024
_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound", "NoSuchBucket"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_key(data: bytes) -> str:
    return content_ref(sha256(data))


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    bytes: int


class MemoryEvidenceStore:
    bucket = "memory"
    endpoint_url = "memory://evidence"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, data: bytes) -> str:
        key = content_key(data)
        self.objects.setdefault(key, bytes(data))
        return key

    def get(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError as error:
            raise EvidenceError("evidence object not found") from error

    def get_limited(self, key: str, *, max_bytes: int) -> bytes:
        data = self.get(key)
        if len(data) > max_bytes:
            raise EvidenceError("evidence object exceeds the read limit")
        return data

    def head(self, key: str) -> ObjectInfo:
        return ObjectInfo(key=key, bytes=len(self.get(key)))

    def exists(self, key: str) -> bool:
        return key in self.objects

    def delete(self, key: str) -> bool:
        return self.objects.pop(key, None) is not None

    def verify(self, key: str, *, digest: str, size: int) -> bool:
        if key != content_ref(digest) or self.head(key).bytes != size:
            return False
        return sha256(self.get_limited(key, max_bytes=size)) == digest

    def presign_put(
        self,
        key: str,
        *,
        bytes: int,
        expires_s: int = PRESIGN_TTL_S,
    ) -> str:
        return f"memory://upload/{key}?bytes={int(bytes)}&expires={int(expires_s)}"


class S3EvidenceStore:
    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        region: str = "auto",
        client=None,
    ):
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
                region_name=self.region,
                config=Config(
                    connect_timeout=CONNECT_TIMEOUT_S,
                    read_timeout=READ_TIMEOUT_S,
                    retries={"max_attempts": RETRIES, "mode": "standard"},
                    signature_version="s3v4",
                ),
            )
        return self._client

    def put(self, data: bytes) -> str:
        key = content_key(data)
        if not self.exists(key):
            self._call(
                "put",
                key,
                lambda: self.client().put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=data,
                    ContentType="application/octet-stream",
                ),
            )
        return key

    def get(self, key: str) -> bytes:
        return self._call(
            "get",
            key,
            lambda: self.client().get_object(Bucket=self.bucket, Key=key)["Body"].read(),
        )

    def get_limited(self, key: str, *, max_bytes: int) -> bytes:
        response = self._call(
            "get",
            key,
            lambda: self.client().get_object(Bucket=self.bucket, Key=key),
        )
        body = response["Body"]
        chunks = []
        total = 0
        try:
            while True:
                chunk = body.read(READ_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise EvidenceError("evidence object exceeds the read limit")
                chunks.append(chunk)
        except EvidenceError:
            raise
        except Exception as error:
            return self._fail("get", key, error)
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        return b"".join(chunks)

    def head(self, key: str) -> ObjectInfo:
        response = self._call(
            "head", key, lambda: self.client().head_object(Bucket=self.bucket, Key=key)
        )
        return ObjectInfo(key=key, bytes=int(response["ContentLength"]))

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client().head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code in _NOT_FOUND_CODES:
                return False
            return self._fail("head", key, error)
        except Exception as error:
            return self._fail("head", key, error)

    def delete(self, key: str) -> bool:
        existed = self.exists(key)
        if existed:
            self._call(
                "delete",
                key,
                lambda: self.client().delete_object(Bucket=self.bucket, Key=key),
            )
        return existed

    def verify(self, key: str, *, digest: str, size: int) -> bool:
        if key != content_ref(digest):
            return False
        info = self.head(key)
        if info.bytes != size:
            return False
        return sha256(self.get_limited(key, max_bytes=size)) == digest

    def presign_put(
        self,
        key: str,
        *,
        bytes: int,
        expires_s: int = PRESIGN_TTL_S,
    ) -> str:
        return self._call(
            "presign",
            key,
            lambda: self.client().generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self.bucket,
                    "Key": key,
                    "ContentType": "application/octet-stream",
                    "ContentLength": int(bytes),
                },
                ExpiresIn=max(1, min(int(expires_s), PRESIGN_TTL_S)),
            ),
        )

    def _call(self, operation: str, key: str, function):
        try:
            return function()
        except Exception as error:
            return self._fail(operation, key, error)

    def _fail(self, operation: str, key: str, error: Exception):
        log.error(
            "evidence operation failed",
            extra={
                "operation": operation,
                "object_suffix": key[-12:],
                "error_class": error.__class__.__name__,
            },
        )
        raise EvidenceError(f"evidence store unavailable ({error.__class__.__name__})") from error


def store_from_env(env: dict | None = None) -> S3EvidenceStore:
    values = os.environ if env is None else env
    return S3EvidenceStore(
        endpoint_url=values.get(ENDPOINT_ENV) or ENDPOINT_DEFAULT,
        bucket=values.get(BUCKET_ENV) or BUCKET_DEFAULT,
        access_key_id=values.get(ACCESS_KEY_ENV) or ACCESS_KEY_DEFAULT,
        secret_access_key=values.get(SECRET_KEY_ENV) or SECRET_KEY_DEFAULT,
    )
