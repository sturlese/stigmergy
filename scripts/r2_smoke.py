#!/usr/bin/env python
"""Verify put, get, and delete against a configured S3-compatible evidence bucket.

Env (all required — fails closed with a clear message on any missing one, never a traceback):
    R2_ENDPOINT_URL       e.g. https://<account-id>.r2.cloudflarestorage.com
    R2_ACCESS_KEY_ID
    R2_SECRET_ACCESS_KEY
    R2_BUCKET

Run:
    .venv/bin/python scripts/r2_smoke.py
"""
import os
import sys
import uuid


def _require_env() -> dict[str, str]:
    names = ["R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]
    values = {n: os.environ.get(n) for n in names}
    missing = [n for n, v in values.items() if not v]
    if missing:
        print(f"r2-smoke: missing env var(s): {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)
    return values


def run() -> int:
    env = _require_env()
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        print("r2-smoke: boto3 is required (pip install -e '.[dev]')", file=sys.stderr)
        return 2

    client = boto3.client(
        "s3",
        endpoint_url=env["R2_ENDPOINT_URL"],
        aws_access_key_id=env["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
        region_name="auto",   # R2's convention — the endpoint URL carries the real routing
    )
    bucket = env["R2_BUCKET"]
    key = f"stigmergy-r2-smoke/{uuid.uuid4().hex}.txt"
    body = b"stigmergy R2 smoke check\n"

    try:
        client.put_object(Bucket=bucket, Key=key, Body=body)
        got = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        if got != body:
            print(f"r2-smoke: FAILED — round-tripped body mismatch for {bucket}/{key}",
                  file=sys.stderr)
            return 1
        client.delete_object(Bucket=bucket, Key=key)
    except (BotoCoreError, ClientError) as ex:
        print(f"r2-smoke: FAILED — {ex.__class__.__name__}", file=sys.stderr)
        return 1

    print(f"r2-smoke: OK — put+get+delete round-tripped {bucket}/{key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
