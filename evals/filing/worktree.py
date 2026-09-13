"""Ephemeral, source-grounded filing worktrees for real-model semantic evaluations."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from stigmergy.capture import schema
from stigmergy.capture.extraction import ExtractedArtifact, ExtractionResult
from stigmergy.capture.source import render_source
from stigmergy.entities.model import registry_bytes
from stigmergy.knowledge.context import filing_context, render_context
from stigmergy.knowledge.lint import check
from stigmergy.knowledge.pages import page_path, render_page
from stigmergy.knowledge.write_guard import WriteContext
from stigmergy.knowledge.writer import _apply_filing_plan


@dataclass(frozen=True)
class EvaluationWorktree:
    root: str
    envelope: schema.CaptureEnvelope
    source_path: str
    source_text: str
    context: str


@contextlib.contextmanager
def prepared(case: dict, source_text: str, *, template: str):
    """Materialize the exact source and declared initial graph without touching a repository."""
    with tempfile.TemporaryDirectory(prefix="stigmergy-filing-eval-") as temporary:
        root = Path(temporary, "repo")
        shutil.copytree(template, root, dirs_exist_ok=True)
        registry = root / "ops" / "entity-registry.json"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_bytes(registry_bytes({}))
        envelope, source_path, rendered_source = _source(case, source_text)
        source_file = root / source_path
        source_file.parent.mkdir(parents=True, exist_ok=True)
        source_file.write_text(rendered_source, encoding="utf-8")
        _seed_pages(root, case, source_path)
        context = render_context(
            filing_context(
                str(root),
                source_text=rendered_source,
                capture_acl=envelope.audience,
                actor_groups=None,
            )
        )
        yield EvaluationWorktree(
            root=str(root),
            envelope=envelope,
            source_path=source_path,
            source_text=rendered_source,
            context=context,
        )


def apply_and_gate(worktree: EvaluationWorktree, plan) -> dict:
    """Use the writer's plan application and gates, never an eval-only approximation."""
    root = worktree.root
    root_path = Path(root)
    context = filing_context(
        root,
        source_text=worktree.source_text,
        capture_acl=worktree.envelope.audience,
        actor_groups=None,
    )
    reasons = {}
    try:
        _apply_filing_plan(
            root,
            plan,
            context=WriteContext(None, worktree.envelope.audience, unrestricted=True),
            envelope=worktree.envelope,
            relative_source=worktree.source_path,
            readable_artifacts=(worktree.source_text,),
            reasons=reasons,
            visible_entities=tuple(context["entities"]),
            visible_entity_ids=frozenset(item["id"] for item in context["entities"]),
            allowed_contradiction_sources=frozenset(
                {worktree.source_path, *(item["path"] for item in context["source_evidence"])}
            ),
        )
        editorial = frozenset(
            path.relative_to(root).as_posix()
            for folder in ("wiki/notes", "wiki/concepts")
            for path in (root_path / folder).glob("*.md")
        )
        violations = check(root, editorial_paths=editorial)
        return {
            "passed": not violations,
            "violations": [
                {"path": item.path, "code": item.code} for item in violations
            ],
            "changed_paths": sorted(reasons),
        }
    except Exception as error:  # The artifact records only the safe class, never model/source text.
        return {
            "passed": False,
            "violations": [{"path": "plan", "code": error.__class__.__name__}],
            "changed_paths": sorted(reasons),
        }


def _source(case: dict, source_text: str) -> tuple[schema.CaptureEnvelope, str, str]:
    raw = source_text.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    source_path = str(case["source_path"])
    capture_id = source_path.rsplit("/", 1)[-1].removesuffix(".md")
    artifact = schema.ArtifactRef(
        blob_ref=schema.content_ref(digest),
        sha256=digest,
        bytes=len(raw),
        media_type=schema.MEDIA_MARKDOWN,
    )
    envelope = schema.CaptureEnvelope(
        capture_id=capture_id,
        idempotency_key=f"filing-eval:{capture_id}",
        actor=schema.Actor(subject="filing-eval", display_name="Filing evaluation"),
        audience=None,
        origin=schema.Origin(
            adapter="mcp",
            captured_at=dt.datetime(2026, 9, 12, 14, 36, 31, tzinfo=dt.UTC),
            title=case["source_title"],
        ),
        artifacts=(artifact,),
    )
    extraction = ExtractedArtifact(
        original=artifact,
        readable_ref=schema.content_ref(digest),
        readable_sha256=digest,
        readable_bytes=len(raw),
        result=ExtractionResult(
            text=source_text,
            media_type=schema.MEDIA_MARKDOWN,
            extractor="filing-eval",
        ),
    )
    return envelope, source_path, render_source(envelope, (extraction,))


def _seed_pages(root: Path, case: dict, source_path: str) -> None:
    for item in case.get("seed_pages", ()):
        role = item["role"]
        title = item["title"]
        path = page_path(role, title)
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            render_page(
                path=path,
                role=role,
                title=title,
                body=item["body"].replace("{source_path}", source_path),
                acl=None,
                sources=(source_path,),
                status="developing",
                page_id=item.get("id"),
                created=dt.date(2026, 9, 1),
                updated=dt.date(2026, 9, 1),
            ),
            encoding="utf-8",
        )
