"""One normalized acquisition service for all capture adapters."""

from __future__ import annotations

import datetime as dt
import uuid

from stigmergy.capture import artifacts, fetch, queue, schema
from stigmergy.capture import evidence as evidence_module
from stigmergy.capture.errors import ArtifactRejected, SubmissionRejected
from stigmergy.capture.provenance import without_capability

_CAPTURE_NAMESPACE = uuid.UUID("22078940-2d32-4c83-877f-541a7f24cbb1")


class CaptureService:
    def __init__(self, conn, evidence) -> None:
        self.conn = conn
        self.evidence = evidence

    def capture_text(
        self,
        *,
        actor: schema.Actor,
        audience: tuple[str, ...] | None,
        adapter: str,
        text: str,
        idempotency_key: str,
        title: str | None = None,
        occurred_at: dt.datetime | dt.date | None = None,
        captured_at: dt.datetime | None = None,
        locator: str | None = None,
        intent: schema.CaptureIntent | None = None,
        acquisition: schema.AcquisitionProvenance | None = None,
    ) -> dict:
        if not isinstance(text, str) or not text:
            raise SubmissionRejected("text is required")
        return self.capture_bytes(
            actor=actor,
            audience=audience,
            adapter=adapter,
            artifact_values=((text.encode("utf-8"), schema.MEDIA_TEXT, None, None),),
            idempotency_key=idempotency_key,
            title=title,
            occurred_at=occurred_at,
            captured_at=captured_at,
            locator=locator,
            intent=intent,
            acquisition=acquisition,
        )

    def capture_bytes(
        self,
        *,
        actor: schema.Actor,
        audience: tuple[str, ...] | None,
        adapter: str,
        artifact_values: tuple[tuple[bytes, str | None, str | None, str | None], ...],
        idempotency_key: str,
        title: str | None = None,
        occurred_at: dt.datetime | dt.date | None = None,
        captured_at: dt.datetime | None = None,
        locator: str | None = None,
        participants: tuple[schema.Participant, ...] = (),
        intent: schema.CaptureIntent | None = None,
        acquisition: schema.AcquisitionProvenance | None = None,
    ) -> dict:
        if not 1 <= len(artifact_values) <= schema.MAX_ARTIFACTS:
            raise ArtifactRejected(
                f"capture requires between 1 and {schema.MAX_ARTIFACTS} artifacts"
            )
        if sum(len(value[0]) for value in artifact_values) > schema.MAX_CAPTURE_BYTES:
            raise ArtifactRejected("artifacts exceed the capture-wide byte limit")
        normalized = []
        for data, declared_type, original_name, source_url in artifact_values:
            if not data:
                raise ArtifactRejected("capture artifacts cannot be empty")
            if len(data) > schema.MAX_ARTIFACT_BYTES:
                raise ArtifactRejected("capture artifact exceeds the size limit")
            media_type = artifacts.detect_media(
                data,
                declared=declared_type,
                original_name=original_name,
            )
            normalized.append((data, media_type, original_name, source_url))

        references = tuple(
            schema.ArtifactRef(
                blob_ref=schema.content_ref(evidence_module.sha256(data)),
                sha256=evidence_module.sha256(data),
                bytes=len(data),
                media_type=media_type,
                original_name=original_name,
                source_url=source_url,
            )
            for data, media_type, original_name, source_url in normalized
        )
        captured_at = captured_at or dt.datetime.now(dt.UTC)
        envelope = self._envelope(
            actor=actor,
            audience=audience,
            adapter=adapter,
            references=references,
            idempotency_key=idempotency_key,
            title=title,
            occurred_at=occurred_at,
            captured_at=captured_at,
            locator=locator,
            participants=participants,
            intent=intent,
            acquisition=acquisition,
        )
        existing = queue.find_capture(self.conn, envelope)
        if existing is not None:
            return existing

        for (data, _media_type, _original_name, _source_url), reference in zip(
            normalized, references, strict=True
        ):
            key = self.evidence.put(data)
            if key != reference.blob_ref:
                raise SubmissionRejected("evidence store returned an invalid content reference")
        return self._queue_envelope(envelope)

    def capture_references(
        self,
        *,
        actor: schema.Actor,
        audience: tuple[str, ...] | None,
        adapter: str,
        references: tuple[schema.ArtifactRef, ...],
        idempotency_key: str,
        title: str | None = None,
        occurred_at: dt.datetime | dt.date | None = None,
        captured_at: dt.datetime | None = None,
        locator: str | None = None,
        participants: tuple[schema.Participant, ...] = (),
        intent: schema.CaptureIntent | None = None,
        acquisition: schema.AcquisitionProvenance | None = None,
    ) -> dict:
        envelope = self._envelope(
            actor=actor,
            audience=audience,
            adapter=adapter,
            references=references,
            idempotency_key=idempotency_key,
            title=title,
            occurred_at=occurred_at,
            captured_at=captured_at or dt.datetime.now(dt.UTC),
            locator=locator,
            participants=participants,
            intent=intent,
            acquisition=acquisition,
        )
        return self._queue_envelope(envelope)

    def _queue_envelope(self, envelope: schema.CaptureEnvelope) -> dict:
        existing = queue.find_capture(self.conn, envelope)
        if existing is not None:
            return existing
        for artifact in envelope.artifacts:
            if not self.evidence.verify(
                artifact.blob_ref,
                digest=artifact.sha256,
                size=artifact.bytes,
            ):
                raise SubmissionRejected("artifact reference could not be verified")
        return queue.enqueue_capture(self.conn, envelope)

    @staticmethod
    def _envelope(
        *,
        actor: schema.Actor,
        audience: tuple[str, ...] | None,
        adapter: str,
        references: tuple[schema.ArtifactRef, ...],
        idempotency_key: str,
        title: str | None,
        occurred_at: dt.datetime | dt.date | None,
        captured_at: dt.datetime,
        locator: str | None,
        participants: tuple[schema.Participant, ...],
        intent: schema.CaptureIntent | None,
        acquisition: schema.AcquisitionProvenance | None,
    ) -> schema.CaptureEnvelope:
        capture_id = uuid.uuid5(
            _CAPTURE_NAMESPACE,
            f"{actor.subject}\0{idempotency_key}",
        )
        return schema.CaptureEnvelope(
            capture_id=capture_id,
            idempotency_key=idempotency_key,
            actor=actor,
            audience=audience,
            origin=schema.Origin(
                adapter=adapter,
                captured_at=captured_at,
                occurred_at=occurred_at,
                title=title,
                locator=locator,
                participants=participants,
                acquisition=acquisition,
            ),
            artifacts=references,
            intent=intent or schema.CaptureIntent(),
        )

    def capture_public_url(
        self,
        *,
        actor: schema.Actor,
        audience: tuple[str, ...] | None,
        adapter: str,
        url: str,
        idempotency_key: str,
        title: str | None = None,
        occurred_at: dt.datetime | dt.date | None = None,
        captured_at: dt.datetime | None = None,
        intent: schema.CaptureIntent | None = None,
        resolver=None,
        requester=None,
    ) -> dict:
        options = {}
        if resolver is not None:
            options["resolver"] = resolver
        if requester is not None:
            options["requester"] = requester
        original_url = without_capability(url)
        acquired = fetch.fetch_public(url, **options)
        acquired_at = dt.datetime.now(dt.UTC)
        return self.capture_bytes(
            actor=actor,
            audience=audience,
            adapter=adapter,
            artifact_values=(
                (
                    acquired.data,
                    acquired.response_media_type or None,
                    None,
                    acquired.final_url,
                ),
            ),
            idempotency_key=idempotency_key,
            title=title,
            occurred_at=occurred_at,
            captured_at=captured_at,
            locator=acquired.final_url,
            intent=intent,
            acquisition=schema.AcquisitionProvenance(
                original_url=original_url,
                final_url=acquired.final_url,
                acquired_at=acquired_at,
            ),
        )
