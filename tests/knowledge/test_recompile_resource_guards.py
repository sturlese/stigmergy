"""Resource and authority regressions for derived-knowledge recompilation."""

from __future__ import annotations

import datetime as dt
import gc
import hashlib
import subprocess
import tracemalloc
import uuid
from pathlib import Path

from stigmergy.capture import evidence, queue, schema
from stigmergy.capture.extraction import ExtractedArtifact, ExtractionResult
from stigmergy.capture.source import (
    MAX_CAPTURE_RENDERED_SOURCE_BYTES,
    render_source,
)
from stigmergy.knowledge import sources, writer
from stigmergy.knowledge.plan import FilingPlan
from stigmergy.knowledge.planner import ScriptedPlanner
from stigmergy.knowledge.writer import WriterDeps
from stigmergy.librarian import config, worker


def _source(root: Path, capture_id: uuid.UUID, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    artifact = schema.ArtifactRef(
        blob_ref=schema.content_ref(digest),
        sha256=digest,
        bytes=len(text.encode("utf-8")),
        media_type=schema.MEDIA_MARKDOWN,
    )
    envelope = schema.CaptureEnvelope(
        capture_id=capture_id,
        idempotency_key=f"source-{capture_id}",
        actor=schema.Actor(subject="marc", display_name="Marc"),
        audience=None,
        origin=schema.Origin(
            adapter="mcp",
            captured_at=dt.datetime(2026, 9, 12, tzinfo=dt.UTC),
            title="Source",
        ),
        artifacts=(artifact,),
    )
    extracted = ExtractedArtifact(
        original=artifact,
        readable_ref=artifact.blob_ref,
        readable_sha256=digest,
        readable_bytes=artifact.bytes,
        result=ExtractionResult(text=text, media_type=schema.MEDIA_MARKDOWN, extractor="test"),
    )
    relative = f"sources/2026/09/{capture_id}.md"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_source(envelope, (extracted,)), encoding="utf-8")
    return relative


def test_recompile_accepts_a_maximum_readable_payload_plus_rendered_framing(tmp_path):
    payload = "x" * (2 * 1024 * 1024)
    relative = _source(tmp_path, uuid.UUID("00000000-0000-4000-8000-000000000001"), payload)

    rendered = (tmp_path / relative).read_bytes()
    assert len(rendered) <= MAX_CAPTURE_RENDERED_SOURCE_BYTES
    assert sources.recompile_source_preflight(str(tmp_path), (relative,)) is None
    assert payload in sources.read_recompile_source(str(tmp_path), relative).body


def test_recompile_preflight_releases_each_source_instead_of_retaining_a_large_corpus(tmp_path):
    paths = tuple(
        _source(tmp_path, uuid.UUID(int=index + 1), "x" * (128 * 1024))
        for index in range(40)
    )
    gc.collect()
    tracemalloc.start()
    try:
        sources.recompile_source_preflight(str(tmp_path), paths)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Retaining the old tuple would keep roughly 5 MiB of decoded source text alive; streaming
    # stays well below that corpus-size slope even with YAML parser allocations.
    assert peak < 3_000_000


def test_non_master_recompile_is_refused_before_the_compiler_reads_or_mutates(clean_queue, target_repo, monkeypatch):
    called = False

    def reject_if_called(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("non-master request reached the compiler")

    monkeypatch.setattr(writer, "_recompile_derived", reject_if_called)
    before = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=target_repo, text=True
    ).strip()
    queue.enqueue_garden(
        clean_queue,
        schema.GardenRequest(
            idempotency_key="non-master-recompile",
            actor=schema.Actor(subject="alice", display_name="Alice"),
            rationale="Must not be permitted outside master scope.",
            mode="recompile",
        ),
    )

    _item, outcome = worker.process_next(
        clean_queue,
        WriterDeps(
            config.Settings(repo=str(target_repo), branch="main", backend="scripted"),
            evidence.MemoryEvidenceStore(),
            ScriptedPlanner(FilingPlan(summary="Unused by refused request")),
            str(target_repo),
        ),
    )

    assert outcome.status == schema.FAILED
    assert called is False
    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=target_repo, text=True
    ).strip() == before
