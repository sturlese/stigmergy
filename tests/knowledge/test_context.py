import datetime as dt
import hashlib
import os
from pathlib import Path
from uuid import UUID

from stigmergy.capture import schema
from stigmergy.capture.extraction import ExtractedArtifact, ExtractionResult
from stigmergy.capture.source import render_source
from stigmergy.knowledge import context
from stigmergy.knowledge.pages import render_page


def _page(root, title, acl, body, *, sources=()):
    relative = f"wiki/notes/{title}.md"
    path = Path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_page(
            path=relative,
            role="note",
            title=title,
            body=f"# {title}\n\n{body}",
            acl=acl,
            sources=sources,
            created=dt.date(2026, 8, 24),
            updated=dt.date(2026, 8, 24),
        ),
        encoding="utf-8",
    )


def _source(root, source_id, acl, title, body):
    relative = f"sources/2026/08/{source_id}.md"
    path = Path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = body.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    artifact = schema.ArtifactRef(
        blob_ref=schema.content_ref(digest),
        sha256=digest,
        bytes=len(data),
        media_type=schema.MEDIA_TEXT,
        original_name="source.txt",
    )
    envelope = schema.CaptureEnvelope(
        capture_id=UUID(source_id),
        idempotency_key=f"source-{source_id}",
        actor=schema.Actor(subject="marc", display_name="Marc"),
        audience=acl,
        origin=schema.Origin(
            adapter="mcp",
            captured_at=dt.datetime(2026, 8, 24, 9, tzinfo=dt.UTC),
            title=title,
        ),
        artifacts=(artifact,),
    )
    extracted = ExtractedArtifact(
        original=artifact,
        readable_ref=artifact.blob_ref,
        readable_sha256=digest,
        readable_bytes=len(data),
        result=ExtractionResult(
            text=body,
            media_type=schema.MEDIA_TEXT,
            extractor="utf8",
        ),
    )
    path.write_text(
        render_source(envelope, (extracted,)),
        encoding="utf-8",
    )
    return relative


def test_context_excludes_visible_page_that_cannot_flow_to_capture(tmp_path):
    _page(tmp_path, "Finance plan", ("finance",), "Launch alpha on Friday.")

    result = context.filing_context(
        str(tmp_path),
        source_text="Alpha launch moved to Monday.",
        capture_acl=("finance", "leadership"),
        actor_groups=frozenset({"finance", "leadership"}),
    )

    assert result["candidates"] == []


def test_context_includes_broader_page_without_allowing_a_restricted_update(tmp_path):
    _page(tmp_path, "Launch plan", None, "Launch alpha on Friday.")

    result = context.filing_context(
        str(tmp_path),
        source_text="Alpha launch moved to Monday.",
        capture_acl=("finance",),
        actor_groups=frozenset({"finance"}),
    )

    assert result["candidates"][0]["path"] == "wiki/notes/Launch plan.md"
    assert result["candidates"][0]["capture_may_update"] is False
    assert "Launch alpha on Friday" in result["candidates"][0]["body"]


def test_context_excludes_pages_the_actor_cannot_read(tmp_path):
    _page(tmp_path, "Finance plan", ("finance",), "Launch alpha on Friday.")

    result = context.filing_context(
        str(tmp_path),
        source_text="Alpha launch moved to Monday.",
        capture_acl=("engineering",),
        actor_groups=frozenset({"engineering"}),
    )

    assert result["candidates"] == []


def test_context_pairs_relevant_claims_with_exact_visible_source_paths(tmp_path):
    relevant = _source(
        tmp_path,
        "00000000-0000-4000-8000-000000000001",
        None,
        "Alpha owner source",
        "Lena Costa owns the Alpha security review.",
    )
    unrelated = _source(
        tmp_path,
        "00000000-0000-4000-8000-000000000002",
        None,
        "Treasury source",
        "The treasury buffer is EUR 42,000.",
    )
    hidden = _source(
        tmp_path,
        "00000000-0000-4000-8000-000000000003",
        ("leadership",),
        "Restricted Alpha source",
        "A private Alpha security review note.",
    )
    _page(
        tmp_path,
        "Alpha plan",
        None,
        "Lena Costa owns the Alpha security review.",
        sources=(relevant, unrelated, hidden),
    )

    result = context.filing_context(
        str(tmp_path),
        source_text="Mateo Ruiz now owns the Alpha security review.",
        capture_acl=None,
        actor_groups=frozenset({"engineering"}),
    )

    assert [item["path"] for item in result["source_evidence"]] == [relevant]
    assert "Lena Costa owns the Alpha security review" in result["source_evidence"][0]["body"]


def test_context_rejects_source_references_outside_the_canonical_corpus(tmp_path):
    secret = tmp_path.parent / "secret.md"
    secret.write_text("Alpha security review belongs to an unrelated file.", encoding="utf-8")
    _page(
        tmp_path,
        "Alpha plan",
        None,
        "The Alpha security review has an owner.",
        sources=("sources/../../secret.md",),
    )

    result = context.filing_context(
        str(tmp_path),
        source_text="The Alpha security review changed owner.",
        capture_acl=None,
        actor_groups=None,
    )

    assert result["source_evidence"] == []


def test_context_marks_oversized_source_evidence_as_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "MAX_CONTEXT_SOURCE_BYTES", 24)
    monkeypatch.setattr(context, "MAX_CONTEXT_SOURCE_FILE_BYTES", 256 * 1024)
    source = _source(
        tmp_path,
        "00000000-0000-4000-8000-000000000004",
        None,
        "Alpha source",
        "The Alpha security review has a deliberately oversized source body.",
    )
    _page(
        tmp_path,
        "Alpha plan",
        None,
        "The Alpha security review has an owner.",
        sources=(source,),
    )

    result = context.filing_context(
        str(tmp_path),
        source_text="The Alpha security review changed owner.",
        capture_acl=None,
        actor_groups=None,
    )

    assert result["source_evidence"] == []
    assert result["truncated"] is True


def test_context_rejects_a_source_file_symlink(tmp_path):
    outside = tmp_path.parent / "outside-source.md"
    valid = _source(
        tmp_path,
        "00000000-0000-4000-8000-000000000005",
        None,
        "Alpha source",
        "The Alpha security review belongs to Lena.",
    )
    valid_path = tmp_path / valid
    outside.write_bytes(valid_path.read_bytes())
    valid_path.unlink()
    os.symlink(outside, valid_path)
    _page(
        tmp_path,
        "Alpha plan",
        None,
        "The Alpha security review has an owner.",
        sources=(valid,),
    )

    result = context.filing_context(
        str(tmp_path),
        source_text="The Alpha security review changed owner.",
        capture_acl=None,
        actor_groups=None,
    )

    assert result["source_evidence"] == []


def test_context_rejects_a_source_directory_symlink(tmp_path):
    external = tmp_path.parent / "external-sources"
    source = _source(
        external,
        "00000000-0000-4000-8000-000000000006",
        None,
        "Alpha source",
        "The Alpha security review belongs to Lena.",
    )
    (tmp_path / "sources").parent.mkdir(parents=True, exist_ok=True)
    os.symlink(external / "sources", tmp_path / "sources")
    _page(
        tmp_path,
        "Alpha plan",
        None,
        "The Alpha security review has an owner.",
        sources=(source,),
    )

    result = context.filing_context(
        str(tmp_path),
        source_text="The Alpha security review changed owner.",
        capture_acl=None,
        actor_groups=None,
    )

    assert result["source_evidence"] == []


def test_context_rejects_a_source_with_invalid_artifacts(tmp_path):
    source = _source(
        tmp_path,
        "00000000-0000-4000-8000-000000000007",
        None,
        "Alpha source",
        "The Alpha security review belongs to Lena.",
    )
    path = tmp_path / source
    path.write_text(
        path.read_text(encoding="utf-8").replace("artifacts:\n", "artifacts: []\n", 1),
        encoding="utf-8",
    )
    _page(
        tmp_path,
        "Alpha plan",
        None,
        "The Alpha security review has an owner.",
        sources=(source,),
    )

    result = context.filing_context(
        str(tmp_path),
        source_text="The Alpha security review changed owner.",
        capture_acl=None,
        actor_groups=None,
    )

    assert result["source_evidence"] == []


def test_context_rejects_a_source_with_invalid_provenance(tmp_path):
    source = _source(
        tmp_path,
        "00000000-0000-4000-8000-000000000011",
        None,
        "Alpha source",
        "The Alpha security review belongs to Lena.",
    )
    path = tmp_path / source
    path.write_text(
        path.read_text(encoding="utf-8").replace("origin: mcp", "origin: unknown", 1),
        encoding="utf-8",
    )
    _page(
        tmp_path,
        "Alpha plan",
        None,
        "The Alpha security review has an owner.",
        sources=(source,),
    )

    result = context.filing_context(
        str(tmp_path),
        source_text="The Alpha security review changed owner.",
        capture_acl=None,
        actor_groups=None,
    )

    assert result["source_evidence"] == []


def test_context_rejects_unsupported_source_frontmatter(tmp_path):
    source = _source(
        tmp_path,
        "00000000-0000-4000-8000-000000000012",
        None,
        "Alpha source",
        "The Alpha security review belongs to Lena.",
    )
    path = tmp_path / source
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "type: source\n",
            "type: source\nprivate_hint: secret\n",
            1,
        ),
        encoding="utf-8",
    )
    _page(
        tmp_path,
        "Alpha plan",
        None,
        "The Alpha security review has an owner.",
        sources=(source,),
    )

    result = context.filing_context(
        str(tmp_path),
        source_text="The Alpha security review changed owner.",
        capture_acl=None,
        actor_groups=None,
    )

    assert result["source_evidence"] == []


def test_context_rejects_a_source_whose_date_does_not_match_its_path(tmp_path):
    source = _source(
        tmp_path,
        "00000000-0000-4000-8000-000000000008",
        None,
        "Alpha source",
        "The Alpha security review belongs to Lena.",
    )
    path = tmp_path / source
    path.write_text(
        path.read_text(encoding="utf-8").replace("2026-08-24", "2026-09-24", 1),
        encoding="utf-8",
    )
    _page(
        tmp_path,
        "Alpha plan",
        None,
        "The Alpha security review has an owner.",
        sources=(source,),
    )

    result = context.filing_context(
        str(tmp_path),
        source_text="The Alpha security review changed owner.",
        capture_acl=None,
        actor_groups=None,
    )

    assert result["source_evidence"] == []


def test_context_rejects_a_source_whose_id_does_not_match_its_path(tmp_path):
    source_id = "00000000-0000-4000-8000-000000000009"
    source = _source(
        tmp_path,
        source_id,
        None,
        "Alpha source",
        "The Alpha security review belongs to Lena.",
    )
    path = tmp_path / source
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            f"id: {source_id}",
            "id: 00000000-0000-4000-8000-999999999999",
            1,
        ),
        encoding="utf-8",
    )
    _page(
        tmp_path,
        "Alpha plan",
        None,
        "The Alpha security review has an owner.",
        sources=(source,),
    )

    result = context.filing_context(
        str(tmp_path),
        source_text="The Alpha security review changed owner.",
        capture_acl=None,
        actor_groups=None,
    )

    assert result["source_evidence"] == []


def test_context_rejects_a_non_regular_source(tmp_path):
    source = "sources/2026/08/00000000-0000-4000-8000-000000000010.md"
    path = tmp_path / source
    path.parent.mkdir(parents=True)
    os.mkfifo(path)
    _page(
        tmp_path,
        "Alpha plan",
        None,
        "The Alpha security review has an owner.",
        sources=(source,),
    )

    result = context.filing_context(
        str(tmp_path),
        source_text="The Alpha security review changed owner.",
        capture_acl=None,
        actor_groups=None,
    )

    assert result["source_evidence"] == []


def test_context_stops_before_reading_beyond_the_reference_cap(tmp_path, monkeypatch):
    sources = tuple(
        _source(
            tmp_path,
            f"00000000-0000-4000-8000-{index:012d}",
            None,
            f"Alpha source {index}",
            f"The Alpha security review source {index} belongs to Lena.",
        )
        for index in range(1, 7)
    )
    _page(
        tmp_path,
        "Alpha plan",
        None,
        "The Alpha security review has an owner.",
        sources=sources,
    )
    monkeypatch.setattr(context, "MAX_CONTEXT_SOURCE_REFERENCES", 3)
    original = context.source_file_size
    seen = []

    def measured(root, relative):
        seen.append(relative)
        return original(root, relative)

    monkeypatch.setattr(context, "source_file_size", measured)

    result = context.filing_context(
        str(tmp_path),
        source_text="The Alpha security review changed owner.",
        capture_acl=None,
        actor_groups=None,
    )

    assert seen == list(sources[:3])
    assert result["truncated"] is True


def test_context_stops_before_reading_beyond_the_aggregate_byte_cap(
    tmp_path, monkeypatch
):
    sources = tuple(
        _source(
            tmp_path,
            f"00000000-0000-4000-8000-{index:012d}",
            None,
            f"Alpha source {index}",
            f"The Alpha security review source {index} belongs to Lena.",
        )
        for index in range(20, 23)
    )
    _page(
        tmp_path,
        "Alpha plan",
        None,
        "The Alpha security review has an owner.",
        sources=sources,
    )
    first_size = (tmp_path / sources[0]).stat().st_size
    monkeypatch.setattr(context, "MAX_CONTEXT_SOURCE_READ_BYTES", first_size)
    original = context.read_source
    seen = []

    def measured(root, relative, *, max_bytes):
        seen.append(relative)
        return original(root, relative, max_bytes=max_bytes)

    monkeypatch.setattr(context, "read_source", measured)

    result = context.filing_context(
        str(tmp_path),
        source_text="The Alpha security review changed owner.",
        capture_acl=None,
        actor_groups=None,
    )

    assert seen == [sources[0]]
    assert result["truncated"] is True


def test_context_is_trimmed_before_rendering(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "MAX_PLANNER_CONTEXT_BYTES", 500)
    _page(tmp_path, "Large plan", None, "alpha " * 300)

    result = context.filing_context(
        str(tmp_path),
        source_text="alpha",
        capture_acl=None,
        actor_groups=None,
    )

    assert result["truncated"] is True
    assert len(context.render_context(result).encode("utf-8")) <= 500


def test_context_lists_each_candidates_filed_contradictions_not_its_prose(tmp_path):
    from stigmergy.knowledge import contradictions
    from stigmergy.knowledge.plan import ContradictionClaim, ContradictionProposal

    first = _source(
        tmp_path,
        "00000000-0000-4000-8000-000000000001",
        None,
        "Vendor brief",
        "The term is 12 months.",
    )
    second = _source(
        tmp_path,
        "00000000-0000-4000-8000-000000000002",
        None,
        "Board minutes",
        "The term is 24 months.",
    )
    record = contradictions.from_proposal(
        ContradictionProposal(
            page_path="wiki/notes/Marked.md",
            explanation="Two drafts disagree on the term.",
            claims=(
                ContradictionClaim(text="The term is 12 months.", source=first, date="2026-08-14"),
                ContradictionClaim(text="The term is 24 months.", source=second, date="2026-08-18"),
            ),
        )
    )
    _page(
        tmp_path,
        "Marked",
        None,
        contradictions.append("Both term drafts are recorded here.", record),
        sources=(first, second),
    )
    _page(
        tmp_path,
        "Prose only",
        None,
        "## Contradiction\n\nThe term is 12 months in one draft and 24 months in the other.",
        sources=(first, second),
    )

    result = context.filing_context(
        str(tmp_path),
        source_text="The term drafts disagree: 12 months versus 24 months.",
        capture_acl=None,
        actor_groups=None,
    )

    by_path = {item["path"]: item for item in result["candidates"]}
    assert by_path["wiki/notes/Marked.md"]["filed_contradictions"] == [
        {
            "id": record.contradiction_id,
            "explanation": "Two drafts disagree on the term.",
            "claims": [
                {"text": "The term is 12 months.", "source": first, "date": "2026-08-14"},
                {"text": "The term is 24 months.", "source": second, "date": "2026-08-18"},
            ],
        }
    ]
    assert by_path["wiki/notes/Prose only.md"]["filed_contradictions"] == []
