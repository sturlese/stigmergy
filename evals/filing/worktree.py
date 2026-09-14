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
from stigmergy.entities import service as entity_service
from stigmergy.entities.model import registry_bytes
from stigmergy.knowledge import contradictions
from stigmergy.knowledge.context import filing_context, render_context
from stigmergy.knowledge.lint import check
from stigmergy.knowledge.pages import PageContractError, page_path, parse_page, render_page
from stigmergy.knowledge.plan import FilingPlan
from stigmergy.knowledge.repair import repair_deterministic
from stigmergy.knowledge.write_guard import WriteContext, WriteRefused
from stigmergy.knowledge.writer import (
    GateRefused,
    KnowledgeWriteError,
    _apply_filing_plan,
    _apply_repair_plan,
    _capture_repair_files,
    _restore_mutable,
    _snapshot_mutable,
    requires_semantic_revision,
)


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


def apply_with_production_repair(
    worktree: EvaluationWorktree,
    plan,
    planner,
    *,
    planning_model_requests: int,
    max_turns: int,
    return_plan: bool = False,
):
    """Exercise the production filing gate and its single bounded model-repair path in memory."""
    root = worktree.root
    context = filing_context(
        root,
        source_text=worktree.source_text,
        capture_acl=worktree.envelope.audience,
        actor_groups=None,
    )
    rendered_context = render_context(context)
    reasons = {}
    snapshot = _snapshot_mutable(root)
    plan_invalid = False
    plan_rejection = ""
    repair_model_requests = 0
    semantic_repair_count = 0
    semantic_revision_required = requires_semantic_revision(plan, context)
    semantic_revision_attempted = False
    semantic_revision_applied = False
    semantic_revision_model_requests = 0
    repair_rejection = None
    repair_mutation_shape = []
    active_plan = plan
    if semantic_revision_required:
        remaining_requests = max(0, int(max_turns) - int(planning_model_requests))
        if remaining_requests < 1:
            plan_invalid = True
            plan_rejection = "semantic-revision-budget-exhausted"
        else:
            semantic_revision_attempted = True
            try:
                revision_run = planner.revise(
                    worktree=root,
                    envelope=worktree.envelope,
                    source_path=worktree.source_path,
                    source_text=worktree.source_text,
                    context=rendered_context,
                    draft=plan,
                    max_requests=remaining_requests,
                )
            except Exception:
                plan_invalid = True
                plan_rejection = "semantic-revision-failed"
            else:
                semantic_revision_model_requests = int(revision_run.model_requests)
                if (
                    semantic_revision_model_requests > remaining_requests
                    or not isinstance(revision_run.plan, FilingPlan)
                ):
                    plan_invalid = True
                    plan_rejection = "semantic-revision-failed"
                else:
                    active_plan = revision_run.plan
                    semantic_revision_applied = True
    if not plan_invalid:
        try:
            _apply_filing_plan(
                root,
                active_plan,
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
            repair_deterministic(root)
        except (
            KnowledgeWriteError,
            PageContractError,
            entity_service.EntityOperationError,
            WriteRefused,
            contradictions.ContradictionContractError,
        ) as error:
            plan_invalid = True
            plan_rejection = error.__class__.__name__

    editorial_paths = frozenset(
        path for path in reasons if path.startswith(("wiki/notes/", "wiki/concepts/"))
    )
    violations = check(root, editorial_paths=editorial_paths) if not plan_invalid else ()
    if violations and not semantic_revision_required:
        try:
            repair_files = _capture_repair_files(
                root,
                violations=violations,
                editorial_paths=editorial_paths,
                snapshot=snapshot,
                context=WriteContext(None, worktree.envelope.audience, unrestricted=True),
            )
            repair_run = planner.repair(
                worktree=root,
                violations=violations,
                files=repair_files,
                source_path=worktree.source_path,
                source_text=worktree.source_text,
                context=rendered_context,
                max_requests=max(0, int(max_turns) - int(planning_model_requests)),
            )
            repair_model_requests = int(repair_run.model_requests)
            semantic_repair_count = int(repair_model_requests > 0)
            repair_mutation_shape = _repair_shape(repair_run.plan)
            repaired = _apply_repair_plan(
                root,
                violations,
                repair_run.plan,
                authorized_files=repair_files,
                context=WriteContext(None, worktree.envelope.audience, unrestricted=True),
                existing_paths=frozenset(snapshot),
            )
            if repaired:
                reasons.update(repaired)
                repair_deterministic(root)
            violations = check(root, editorial_paths=editorial_paths)
        except GateRefused as error:
            plan_invalid = True
            plan_rejection = "invalid-repair-plan"
            repair_rejection = _safe_repair_rejection(error)
        else:
            if violations:
                plan_invalid = True
                plan_rejection = "unrepaired-gate-violations"
    elif violations:
        plan_invalid = True
        plan_rejection = "unrepaired-gate-violations"

    if plan_invalid:
        _restore_mutable(root, snapshot)
    result = {
        "passed": not plan_invalid and not violations,
        "violations": [{"path": item.path, "code": item.code} for item in violations],
        "changed_paths": sorted(reasons),
        "plan_rejection": plan_rejection or None,
        "repair_model_requests": repair_model_requests,
        "semantic_repair_count": semantic_repair_count,
        "semantic_revision_required": semantic_revision_required,
        "semantic_revision_attempted": semantic_revision_attempted,
        "semantic_revision_applied": semantic_revision_applied,
        "semantic_revision_model_requests": semantic_revision_model_requests,
        "repair_rejection": repair_rejection,
        "repair_mutation_shape": repair_mutation_shape,
    }
    return (result, active_plan) if return_plan else result


def effective_plan(worktree: EvaluationWorktree, plan):
    """Return the submitted plan with bodies read from the gated temporary worktree."""
    root = Path(worktree.root)
    mutations = []
    for mutation in plan.mutations:
        if mutation.action == "delete":
            mutations.append(mutation)
            continue
        relative = mutation.path if mutation.action == "update" else page_path(mutation.role, mutation.title)
        target = root / str(relative)
        if not target.is_file():
            mutations.append(mutation)
            continue
        page = parse_page(str(relative), target.read_text(encoding="utf-8"))
        mutations.append(mutation.model_copy(update={"body": page.body}))
    return plan.model_copy(update={"mutations": tuple(mutations)})


def _repair_shape(plan) -> list[dict[str, int | str]]:
    """Expose no repair body while retaining enough shape to diagnose a gate refusal."""
    return [
        {
            "target_kind": _repair_target_kind(mutation.path),
            "body_bytes": len(mutation.body.encode("utf-8")),
            "reason_bytes": len(mutation.reason.encode("utf-8")),
        }
        for mutation in plan.mutations
    ]


def _repair_target_kind(path: str) -> str:
    if path.startswith("wiki/concepts/"):
        return "concept"
    if path.startswith("wiki/notes/"):
        return "note"
    if path.startswith("wiki/entities/"):
        return "entity"
    if path.startswith("sources/"):
        return "source"
    return "other"


_SAFE_REPAIR_REJECTIONS = frozenset(
    {
        "repair includes a path outside this capture's visible graph changes",
        "repair may only target capture knowledge pages",
        "repair target is outside the capture audience",
        "repair target is missing",
        "repair target exceeds its byte limit",
        "model repair targeted a path outside its violations",
        "model repair targeted a path outside this capture's authorization",
        "model repair targeted a missing path",
        "capture repair is missing its authorization context",
        "model repair is outside the capture audience",
        "repair target has no preservable page metadata",
        "repair target changed after authorization",
    }
)


def _safe_repair_rejection(error: GateRefused) -> str:
    message = str(error)
    return message if message in _SAFE_REPAIR_REJECTIONS else error.__class__.__name__


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
