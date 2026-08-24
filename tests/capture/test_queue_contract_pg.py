import concurrent.futures
import datetime as dt
import hashlib
import uuid
from types import SimpleNamespace

import pytest

from stigmergy.capture import queue, schema, uploads
from stigmergy.capture.errors import (
    CaptureError,
    EvidenceError,
    QueueStateError,
    SubmissionRejected,
    UploadError,
)
from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.capture.service import CaptureService
from stigmergy.index import store as index_store
from stigmergy.server.identity import Principal
from stigmergy.server.service import BrainService
from tests import testdb

ALICE = schema.Actor(subject="alice@example.com", display_name="Alice")
CAPTURED_AT = dt.datetime(2026, 8, 24, 12, tzinfo=dt.UTC)


def _capture(conn, evidence, *, text="knowledge", key=None):
    return CaptureService(conn, evidence).capture_text(
        actor=ALICE,
        audience=("engineering",),
        adapter="mcp",
        text=text,
        idempotency_key=key or str(uuid.uuid4()),
        captured_at=CAPTURED_AT,
    )


def test_capture_queue_stores_an_envelope_without_text_payload(clean_queue):
    evidence = MemoryEvidenceStore()
    receipt = _capture(clean_queue, evidence, text="exact source", key="one")

    assert receipt["status"] == schema.QUEUED
    assert receipt["created"] is True
    assert receipt["request"]["artifacts"][0]["sha256"] == hashlib.sha256(
        b"exact source"
    ).hexdigest()
    assert "exact source" not in str(receipt["request"])
    with clean_queue.cursor() as cursor:
        cursor.execute(
            "SELECT operation, request ? 'kind', request ? 'material', request ? 'payload' "
            "FROM capture_queue WHERE id = %s",
            (receipt["id"],),
        )
        assert cursor.fetchone() == (schema.CAPTURE, False, False, False)


def test_idempotent_retry_returns_the_original_capture(clean_queue):
    evidence = MemoryEvidenceStore()
    first = _capture(clean_queue, evidence, text="same", key="stable-request")
    second = _capture(clean_queue, evidence, text="same", key="stable-request")

    assert first["id"] == second["id"]
    assert first["created"] is True
    assert second["created"] is False
    with clean_queue.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM capture_queue")
        assert cursor.fetchone()[0] == 1


def test_reusing_an_idempotency_key_for_different_bytes_is_rejected(clean_queue):
    evidence = MemoryEvidenceStore()
    _capture(clean_queue, evidence, text="first", key="collision")
    before = dict(evidence.objects)

    with pytest.raises(SubmissionRejected, match="different request"):
        _capture(clean_queue, evidence, text="second", key="collision")

    assert evidence.objects == before


def test_idempotent_retry_does_not_require_deleted_evidence(clean_queue):
    evidence = MemoryEvidenceStore()
    first = _capture(clean_queue, evidence, text="same", key="evidence-gone")
    evidence.objects.clear()

    second = _capture(clean_queue, evidence, text="same", key="evidence-gone")

    assert second["id"] == first["id"]
    assert second["created"] is False
    assert evidence.objects == {}


def test_equal_content_with_distinct_keys_creates_distinct_captures(clean_queue):
    evidence = MemoryEvidenceStore()
    first = _capture(clean_queue, evidence, text="same", key="first")
    second = _capture(clean_queue, evidence, text="same", key="second")

    assert first["id"] != second["id"]
    assert (
        first["request"]["artifacts"][0]["blob_ref"]
        == second["request"]["artifacts"][0]["blob_ref"]
    )
    assert len(evidence.objects) == 1


def test_claiming_is_exactly_once_under_parallel_workers(clean_queue):
    evidence = MemoryEvidenceStore()
    for index in range(12):
        _capture(clean_queue, evidence, text=f"item {index}", key=f"parallel-{index}")

    def claim(_):
        connection = index_store.connect(testdb.dsn())
        try:
            item = queue.claim_next(connection)
            return item["id"] if item else None
        finally:
            connection.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(claim, range(16)))
    ids = [item_id for item_id in claimed if item_id]

    assert len(ids) == 12
    assert len(set(ids)) == 12


def test_a_redelivered_lease_fences_the_stale_worker(clean_queue):
    evidence = MemoryEvidenceStore()
    receipt = _capture(clean_queue, evidence, key="lease")
    first = queue.claim_next(clean_queue, visibility_timeout_s=0)
    queue.release_expired(clean_queue, visibility_timeout_s=0)
    second = queue.claim_next(clean_queue)

    assert first["attempts"] == 1
    assert second["attempts"] == 2
    with pytest.raises(QueueStateError, match="redelivered"):
        queue.finish_landed(
            clean_queue,
            receipt["id"],
            expected_attempts=first["attempts"],
            source_path="sources/2026/08/x.md",
            commit_sha="a" * 40,
            change_id=uuid.uuid4(),
        )


def test_landed_transition_records_commit_source_and_change(clean_queue):
    evidence = MemoryEvidenceStore()
    receipt = _capture(clean_queue, evidence, key="land")
    claimed = queue.claim_next(clean_queue)
    change_id = uuid.uuid4()

    landed = queue.finish_landed(
        clean_queue,
        receipt["id"],
        expected_attempts=claimed["attempts"],
        source_path=f"sources/2026/08/{receipt['id']}.md",
        commit_sha="b" * 40,
        change_id=change_id,
        extraction={"artifacts": 1},
        report={"summary": "Learned one decision"},
    )

    assert landed["status"] == schema.LANDED
    assert landed["commit_sha"] == "b" * 40
    assert landed["change_id"] == str(change_id)
    assert landed["extraction"] == {"artifacts": 1}


def test_retryable_failures_back_off_then_terminally_fail(clean_queue):
    evidence = MemoryEvidenceStore()
    receipt = _capture(clean_queue, evidence, key="retry")
    claimed = queue.claim_next(clean_queue)

    retry = queue.fail_or_retry(
        clean_queue,
        receipt["id"],
        expected_attempts=claimed["attempts"],
        category="transient",
        error="temporary",
        retryable=True,
    )
    assert retry["status"] == schema.QUEUED

    with clean_queue.cursor() as cursor:
        cursor.execute(
            "UPDATE capture_queue SET next_attempt_at = now() WHERE id = %s",
            (receipt["id"],),
        )
    claimed = queue.claim_next(clean_queue)
    failed = queue.fail_or_retry(
        clean_queue,
        receipt["id"],
        expected_attempts=claimed["attempts"],
        category="invalid_artifact",
        error="corrupt",
        retryable=False,
    )
    assert failed["status"] == schema.FAILED


def test_verified_upload_session_returns_an_artifact_reference(clean_queue):
    evidence = MemoryEvidenceStore()
    data = b"uploaded bytes"
    digest = hashlib.sha256(data).hexdigest()
    created = uploads.create_upload(
        clean_queue,
        evidence,
        actor=ALICE.subject,
        idempotency_key="upload-1",
        sha256=digest,
        bytes=len(data),
        media_type=schema.MEDIA_TEXT,
        original_name="notes.txt",
    )
    evidence.objects[uploads.staging_ref(created["upload_id"])] = data

    artifact = uploads.finalize_upload(
        clean_queue,
        evidence,
        actor=ALICE.subject,
        upload_id=created["upload_id"],
    )

    assert artifact.sha256 == digest
    assert artifact.original_name == "notes.txt"


def test_existing_content_object_cannot_satisfy_a_new_upload_session(clean_queue):
    evidence = MemoryEvidenceStore()
    data = b"restricted bytes already held by the service"
    digest = hashlib.sha256(data).hexdigest()
    evidence.put(data)
    created = uploads.create_upload(
        clean_queue,
        evidence,
        actor="another-user@example.com",
        idempotency_key="proof-of-possession",
        sha256=digest,
        bytes=len(data),
        media_type=schema.MEDIA_TEXT,
    )

    with pytest.raises(UploadError):
        uploads.finalize_upload(
            clean_queue,
            evidence,
            actor="another-user@example.com",
            upload_id=created["upload_id"],
        )


def test_presigned_upload_is_promoted_from_staging_without_exposing_the_content_key(
    clean_queue,
):
    evidence = MemoryEvidenceStore()
    data = b"private drive bytes"
    digest = hashlib.sha256(data).hexdigest()
    created = uploads.create_upload(
        clean_queue,
        evidence,
        actor=ALICE.subject,
        idempotency_key="upload-staged",
        sha256=digest,
        bytes=len(data),
        media_type=schema.MEDIA_TEXT,
    )
    staging_ref = f"uploads/{created['upload_id']}"
    assert staging_ref in created["upload_url"]
    assert schema.content_ref(digest) not in created["upload_url"]
    evidence.objects[staging_ref] = data

    artifact = uploads.finalize_upload(
        clean_queue,
        evidence,
        actor=ALICE.subject,
        upload_id=created["upload_id"],
    )
    evidence.objects[staging_ref] = b"late overwrite through an unexpired URL"

    assert artifact.blob_ref == schema.content_ref(digest)
    assert evidence.get(artifact.blob_ref) == data


def test_verified_upload_retry_does_not_issue_another_write_url(clean_queue):
    evidence = MemoryEvidenceStore()
    data = b"already verified"
    digest = hashlib.sha256(data).hexdigest()
    created = uploads.create_upload(
        clean_queue,
        evidence,
        actor=ALICE.subject,
        idempotency_key="upload-idempotent",
        sha256=digest,
        bytes=len(data),
        media_type=schema.MEDIA_TEXT,
    )
    evidence.objects[uploads.staging_ref(created["upload_id"])] = data
    uploads.finalize_upload(
        clean_queue,
        evidence,
        actor=ALICE.subject,
        upload_id=created["upload_id"],
    )

    retried = uploads.create_upload(
        clean_queue,
        evidence,
        actor=ALICE.subject,
        idempotency_key="upload-idempotent",
        sha256=digest,
        bytes=len(data),
        media_type=schema.MEDIA_TEXT,
    )

    assert retried["upload_id"] == created["upload_id"]
    assert retried["upload_url"] is None


def test_failed_multi_upload_transaction_keeps_verified_staging_retriable(clean_queue):
    evidence = MemoryEvidenceStore()
    first_data = b"first upload"
    second_data = b"second upload"
    sessions = [
        uploads.create_upload(
            clean_queue,
            evidence,
            actor=ALICE.subject,
            idempotency_key=f"transactional-upload-{position}",
            sha256=hashlib.sha256(data).hexdigest(),
            bytes=len(data),
            media_type=schema.MEDIA_TEXT,
        )
        for position, data in enumerate((first_data, second_data), start=1)
    ]
    first_staging = uploads.staging_ref(sessions[0]["upload_id"])
    second_staging = uploads.staging_ref(sessions[1]["upload_id"])
    evidence.objects[first_staging] = first_data

    with pytest.raises(UploadError, match="unavailable"), clean_queue.transaction():
        uploads.finalize_upload(
            clean_queue,
            evidence,
            actor=ALICE.subject,
            upload_id=sessions[0]["upload_id"],
        )
        uploads.finalize_upload(
            clean_queue,
            evidence,
            actor=ALICE.subject,
            upload_id=sessions[1]["upload_id"],
        )

    assert evidence.get(first_staging) == first_data
    evidence.objects[second_staging] = second_data
    with clean_queue.transaction():
        artifacts = [
            uploads.finalize_upload(
                clean_queue,
                evidence,
                actor=ALICE.subject,
                upload_id=session["upload_id"],
            )
            for session in sessions
        ]
    assert [artifact.sha256 for artifact in artifacts] == [
        hashlib.sha256(first_data).hexdigest(),
        hashlib.sha256(second_data).hexdigest(),
    ]


def test_upload_finalization_rejects_wrong_bytes(clean_queue):
    evidence = MemoryEvidenceStore()
    expected = b"expected"
    digest = hashlib.sha256(expected).hexdigest()
    created = uploads.create_upload(
        clean_queue,
        evidence,
        actor=ALICE.subject,
        idempotency_key="upload-bad",
        sha256=digest,
        bytes=len(expected),
        media_type=schema.MEDIA_TEXT,
    )
    evidence.objects[uploads.staging_ref(created["upload_id"])] = b"tampered"

    with pytest.raises(Exception, match="do not match"):
        uploads.finalize_upload(
            clean_queue,
            evidence,
            actor=ALICE.subject,
            upload_id=created["upload_id"],
        )


def test_upload_finalization_heads_then_reads_with_a_bounded_limit(clean_queue):
    class RaceEvidence:
        def __init__(self, data):
            self.data = data
            self.calls = []

        def presign_put(self, _key, **_kwargs):
            return "memory://upload"

        def head(self, key):
            self.calls.append(("head", key))
            return type("Info", (), {"bytes": len(self.data) - 1})()

        def get_limited(self, key, *, max_bytes):
            self.calls.append(("get_limited", key, max_bytes))
            raise EvidenceError("object changed after HEAD")

        def put(self, data):
            return schema.content_ref(hashlib.sha256(data).hexdigest())

    expected = b"declared"
    evidence = RaceEvidence(expected + b"!")
    created = uploads.create_upload(
        clean_queue,
        evidence,
        actor=ALICE.subject,
        idempotency_key="head-get-race",
        sha256=hashlib.sha256(expected).hexdigest(),
        bytes=len(expected),
        media_type=schema.MEDIA_TEXT,
    )

    with pytest.raises(UploadError, match="unavailable"):
        uploads.finalize_upload(
            clean_queue,
            evidence,
            actor=ALICE.subject,
            upload_id=created["upload_id"],
        )

    assert evidence.calls == [
        ("head", uploads.staging_ref(created["upload_id"])),
        ("get_limited", uploads.staging_ref(created["upload_id"]), len(expected)),
    ]


def test_multi_upload_capture_rejects_declared_aggregate_before_evidence_access(
    clean_queue, monkeypatch
):
    class Evidence:
        def __init__(self):
            self.accesses = []

        def presign_put(self, _key, **_kwargs):
            return "memory://upload"

        def head(self, key):
            self.accesses.append(("head", key))
            raise AssertionError("aggregate validation must precede evidence access")

        def get_limited(self, key, *, max_bytes):
            self.accesses.append(("get_limited", key, max_bytes))
            raise AssertionError("aggregate validation must precede evidence access")

        def put(self, data):
            self.accesses.append(("put", data))
            raise AssertionError("aggregate validation must precede evidence access")

    monkeypatch.setattr(schema, "MAX_CAPTURE_BYTES", 10)
    evidence = Evidence()
    principal = Principal(
        subject=ALICE.subject,
        display_name=ALICE.display_name,
        groups=("brain-admins",),
        default_audience=None,
    )
    service = BrainService(
        SimpleNamespace(identities_path="unused"),
        clean_queue,
        None,
        audiences=None,
        identity=principal.subject,
        evidence=evidence,
        principal=principal,
    )
    upload_ids = []
    for position, data in enumerate((b"abcdef", b"ghijk"), start=1):
        created = service.create_upload(
            idempotency_key=f"aggregate-declared-{position}",
            sha256=hashlib.sha256(data).hexdigest(),
            bytes=len(data),
            media_type=schema.MEDIA_TEXT,
        )
        upload_ids.append(created["upload_id"])

    with pytest.raises(CaptureError):
        service.finalize_upload_capture(
            upload_ids=upload_ids,
            idempotency_key="aggregate-declared-capture",
        )

    assert evidence.accesses == []
