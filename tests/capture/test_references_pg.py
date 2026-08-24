import datetime as dt
import hashlib
import uuid
from types import SimpleNamespace

import pytest

from stigmergy.capture import evidence, queue, references, schema, uploads
from stigmergy.capture.extraction import ExtractedArtifact, ExtractionResult
from stigmergy.librarian import worker


def _artifact(store, data: bytes) -> schema.ArtifactRef:
    digest = hashlib.sha256(data).hexdigest()
    return schema.ArtifactRef(
        blob_ref=store.put(data),
        sha256=digest,
        bytes=len(data),
        media_type=schema.MEDIA_TEXT,
    )


def _capture(store, data: bytes, key: str):
    artifact = _artifact(store, data)
    envelope = schema.CaptureEnvelope(
        idempotency_key=key,
        actor=schema.Actor(subject="alice", display_name="Alice"),
        audience=None,
        origin=schema.Origin(adapter="mcp", captured_at=dt.datetime.now(dt.UTC)),
        artifacts=(artifact,),
    )
    extracted = ExtractedArtifact(
        original=artifact,
        readable_ref=artifact.blob_ref,
        readable_sha256=artifact.sha256,
        readable_bytes=artifact.bytes,
        result=ExtractionResult(
            text=data.decode(),
            media_type=schema.MEDIA_TEXT,
            extractor="utf8",
        ),
    )
    return envelope, (extracted,)


def test_shared_object_is_collected_only_after_its_last_source_is_released(clean_queue):
    store = evidence.MemoryEvidenceStore()
    first, first_extraction = _capture(store, b"shared", "first")
    second, second_extraction = _capture(store, b"shared", "second")
    references.record_capture(clean_queue, first, first_extraction, "sources/2026/08/first.md")
    references.record_capture(clean_queue, second, second_extraction, "sources/2026/08/second.md")

    assert references.release_sources(clean_queue, {"sources/2026/08/first.md"}) == set()
    garbage = references.release_sources(clean_queue, {"sources/2026/08/second.md"})

    assert garbage == {first.artifacts[0].blob_ref}


@pytest.mark.parametrize("status", [schema.QUEUED, schema.FAILED])
def test_unlanded_capture_keeps_shared_original_live(clean_queue, status):
    store = evidence.MemoryEvidenceStore()
    landed, extracted = _capture(store, b"shared pending bytes", "landed")
    pending, _ = _capture(store, b"shared pending bytes", f"pending-{status}")
    references.record_capture(clean_queue, landed, extracted, "sources/2026/08/landed.md")
    receipt = queue.enqueue_capture(clean_queue, pending)
    if status == schema.FAILED:
        with clean_queue.cursor() as cursor:
            cursor.execute(
                "UPDATE capture_queue SET status = %s, finished_at = now() WHERE id = %s",
                (schema.FAILED, receipt["id"]),
            )

    assert references.release_sources(clean_queue, {"sources/2026/08/landed.md"}) == set()


def test_expired_unconsumed_upload_is_removed_with_its_orphan_object(clean_queue):
    store = evidence.MemoryEvidenceStore()
    data = b"abandoned upload"
    digest = hashlib.sha256(data).hexdigest()
    key = store.put(data)
    upload = uploads.create_upload(
        clean_queue,
        store,
        actor="alice",
        idempotency_key=str(uuid.uuid4()),
        sha256=digest,
        bytes=len(data),
        media_type=schema.MEDIA_TEXT,
    )
    with clean_queue.cursor() as cursor:
        cursor.execute(
            "UPDATE upload_sessions SET expires_at = now() - interval '1 second' WHERE id = %s",
            (upload["upload_id"],),
        )

    assert uploads.purge_expired(clean_queue, store) == 1
    assert not store.exists(key)


def test_expired_upload_cleanup_preserves_an_object_used_by_a_landed_capture(clean_queue):
    store = evidence.MemoryEvidenceStore()
    envelope, extracted = _capture(store, b"still live", "live")
    references.record_capture(clean_queue, envelope, extracted, "sources/2026/08/live.md")
    artifact = envelope.artifacts[0]
    upload = uploads.create_upload(
        clean_queue,
        store,
        actor="alice",
        idempotency_key=str(uuid.uuid4()),
        sha256=artifact.sha256,
        bytes=artifact.bytes,
        media_type=artifact.media_type,
    )
    with clean_queue.cursor() as cursor:
        cursor.execute(
            "UPDATE upload_sessions SET expires_at = now() - interval '1 second' WHERE id = %s",
            (upload["upload_id"],),
        )

    assert uploads.purge_expired(clean_queue, store) == 1
    assert store.exists(artifact.blob_ref)


def test_busy_worker_still_queues_garden_and_deletes_expired_staging_upload(clean_queue, monkeypatch):
    store = evidence.MemoryEvidenceStore()
    data = b"abandoned staging object"
    digest = hashlib.sha256(data).hexdigest()
    upload = uploads.create_upload(
        clean_queue,
        store,
        actor="alice",
        idempotency_key=str(uuid.uuid4()),
        sha256=digest,
        bytes=len(data),
        media_type=schema.MEDIA_TEXT,
    )
    staging = uploads.staging_ref(upload["upload_id"])
    store.objects[staging] = data
    with clean_queue.cursor() as cursor:
        cursor.execute(
            "UPDATE upload_sessions SET expires_at = now() - interval '1 second' WHERE id = %s",
            (upload["upload_id"],),
        )

    settings = SimpleNamespace(
        visibility_timeout_s=600,
        max_attempts=3,
        poll_interval_s=0.01,
        garden_at="05:07",
    )
    loop = worker.Worker(
        clean_queue,
        SimpleNamespace(settings=settings, evidence=store),
        utcnow=lambda: dt.datetime(2026, 8, 24, 5, 8, tzinfo=dt.UTC),
        monotonic=lambda: 1_000.0,
        on_output=lambda _line: None,
    )
    calls = 0

    def continuous_backlog(_conn, _deps):
        nonlocal calls
        calls += 1
        if calls == 3:
            loop.stopping = True
        return (
            {"id": "00000000-0000-4000-8000-000000000001"},
            worker.ProcessOutcome(status=schema.LANDED, report={}),
        )

    monkeypatch.setattr(worker, "process_next", continuous_backlog)

    assert loop.run() == 3
    assert not store.exists(staging)
    with clean_queue.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM upload_sessions")
        assert cursor.fetchone()[0] == 0
        cursor.execute("SELECT operation, request->>'idempotency_key' FROM capture_queue")
        assert cursor.fetchall() == [(schema.GARDEN, "garden:scheduled:2026-08-24")]
