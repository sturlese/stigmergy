"""Ephemeral, source-grounded filing worktrees for real-model semantic evaluations."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from stigmergy.capture import schema
from stigmergy.capture.extraction import ExtractedArtifact, ExtractionResult
from stigmergy.capture.source import render_source
from stigmergy.knowledge import contradictions
from stigmergy.knowledge.context import filing_context, render_context
from stigmergy.knowledge.lint import check
from stigmergy.knowledge.pages import (
    KNOWLEDGE_FOLDERS,
    PageContractError,
    page_path,
    parse_page,
    path_role,
    render_page,
)
from stigmergy.knowledge.plan import FilingPlan
from stigmergy.knowledge.write_guard import WriteContext, WriteRefused
from stigmergy.knowledge.writer import (
    EMPTY_PLAN,
    ExistingSourceBlockNotPreserved,
    KnowledgeWriteError,
    _apply_filing_plan,
    _filing_failure_payload,
    _restore_mutable,
    _snapshot_mutable,
)


@dataclass(frozen=True)
class EvaluationWorktree:
    root: str
    envelope: schema.CaptureEnvelope
    source_path: str
    source_text: str
    context: str
    initial_worktree_manifest_sha256: str


def initial_graph_manifest(template: str | Path) -> str:
    """Return the content-addressed manifest of a versioned initial-graph template."""
    return _tree_manifest(Path(template))


def _tree_manifest(root: Path) -> str:
    if not root.is_dir():
        raise ValueError(f"evaluation template is not a directory: {root}")
    entries = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        if path.is_symlink() or path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"evaluation template contains a non-file entry: {relative}")
        entries.append(
            {
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return hashlib.sha256(
        json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@contextlib.contextmanager
def prepared(case: dict, source_text: str, *, template: str):
    """Materialize the exact source and declared initial graph without touching a repository."""
    with tempfile.TemporaryDirectory(prefix="stigmergy-filing-eval-") as temporary:
        root = Path(temporary, "repo")
        shutil.copytree(template, root, dirs_exist_ok=True)
        (root / "ops").mkdir(parents=True, exist_ok=True)
        envelope, source_path, rendered_source = _source(case, source_text)
        source_file = root / source_path
        source_file.parent.mkdir(parents=True, exist_ok=True)
        source_file.write_text(rendered_source, encoding="utf-8")
        _seed_pages(root, case)
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
            initial_worktree_manifest_sha256=_tree_manifest(root),
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
            reasons=reasons,
            allowed_contradiction_sources=frozenset(
                {worktree.source_path, *(item["path"] for item in context["source_evidence"])}
            ),
            drop_broader=not worktree.envelope.intent.resolution_of,
        )
        editorial = frozenset(
            path.relative_to(root).as_posix()
            for folder in KNOWLEDGE_FOLDERS
            for path in (root_path / folder).glob("*.md")
        )
        violations = check(root, editorial_paths=editorial)
        return {
            "passed": not violations,
            "violations": [{"path": item.path, "code": item.code} for item in violations],
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
    contract_failures = ()
    semantic_revision_required = False
    semantic_revision_attempted = False
    semantic_revision_applied = False
    semantic_revision_model_requests = 0
    semantic_revision_telemetry = None
    active_plan = plan
    if not isinstance(active_plan, FilingPlan):
        plan_invalid = True
        plan_rejection = "wrong-plan-type"
    if not plan_invalid:
        try:
            _apply_filing_plan(
                root,
                active_plan,
                context=WriteContext(None, worktree.envelope.audience, unrestricted=True),
                envelope=worktree.envelope,
                relative_source=worktree.source_path,
                reasons=reasons,
                allowed_contradiction_sources=frozenset(
                    {worktree.source_path, *(item["path"] for item in context["source_evidence"])}
                ),
                drop_broader=not worktree.envelope.intent.resolution_of,
            )
        except (
            KnowledgeWriteError,
            PageContractError,
            WriteRefused,
            contradictions.ContradictionContractError,
        ) as error:
            plan_invalid = True
            plan_rejection = (
                ExistingSourceBlockNotPreserved.code
                if isinstance(error, ExistingSourceBlockNotPreserved)
                else error.__class__.__name__
            )
            contract_failures = (
                error.correction() if isinstance(error, ExistingSourceBlockNotPreserved) else {
                    "path": "plan",
                    "code": error.__class__.__name__,
                    "message": str(error),
                },
            )
    if not plan_invalid and not active_plan.mutations and not active_plan.contradictions:
        plan_invalid = True
        plan_rejection = EMPTY_PLAN["code"]
        contract_failures = (EMPTY_PLAN,)

    editorial_paths = frozenset(path for path in reasons if path_role(path))
    violations = check(root, editorial_paths=editorial_paths) if not plan_invalid else ()
    if plan_invalid or violations:
        semantic_revision_required = True
        remaining_requests = max(0, int(max_turns) - int(planning_model_requests))
        if remaining_requests < 1:
            plan_invalid = True
            plan_rejection = "filing-correction-budget-exhausted"
        else:
            failure_payload = (
                *contract_failures,
                *_filing_failure_payload(
                    active_plan,
                    violations,
                    source_path=worktree.source_path,
                ),
            )
            _restore_mutable(root, snapshot)
            reasons = {}
            plan_invalid = False
            semantic_revision_attempted = True
            try:
                revision_run = planner.revise(
                    worktree=root,
                    envelope=worktree.envelope,
                    source_path=worktree.source_path,
                    source_text=worktree.source_text,
                    context=rendered_context,
                    draft=active_plan,
                    violations=failure_payload,
                    max_requests=remaining_requests,
                )
                semantic_revision_model_requests = int(revision_run.model_requests)
                semantic_revision_telemetry = revision_run.telemetry
                active_plan = revision_run.plan
                _apply_filing_plan(
                    root,
                    active_plan,
                    context=WriteContext(None, worktree.envelope.audience, unrestricted=True),
                    envelope=worktree.envelope,
                    relative_source=worktree.source_path,
                    reasons=reasons,
                    allowed_contradiction_sources=frozenset(
                        {
                            worktree.source_path,
                            *(item["path"] for item in context["source_evidence"]),
                        }
                    ),
                    drop_broader=not worktree.envelope.intent.resolution_of,
                )
                editorial_paths = frozenset(path for path in reasons if path_role(path))
                violations = check(root, editorial_paths=editorial_paths)
            except ExistingSourceBlockNotPreserved as lossy:
                # As the writer does: file the corrected plan without each update that still drops
                # cited lines.
                dropped: list[str] = []
                try:
                    while lossy is not None:
                        dropped.append(lossy.path)
                        active_plan = active_plan.model_copy(update={"mutations": tuple(
                            m for m in active_plan.mutations if not (m.action == "update" and m.path in dropped))})
                        _restore_mutable(root, snapshot)
                        reasons = {}
                        lossy = None
                        try:
                            _apply_filing_plan(
                                root,
                                active_plan,
                                context=WriteContext(None, worktree.envelope.audience, unrestricted=True),
                                envelope=worktree.envelope,
                                relative_source=worktree.source_path,
                                reasons=reasons,
                                allowed_contradiction_sources=frozenset(
                                    {worktree.source_path, *(item["path"] for item in context["source_evidence"])}
                                ),
                                skipped=[],
                                drop_broader=not worktree.envelope.intent.resolution_of,
                            )
                        except ExistingSourceBlockNotPreserved as again:
                            lossy = again
                    editorial_paths = frozenset(path for path in reasons if path_role(path))
                    violations = check(root, editorial_paths=editorial_paths)
                    if violations or not active_plan.mutations:
                        plan_invalid = True
                        plan_rejection = ExistingSourceBlockNotPreserved.code
                    else:
                        semantic_revision_applied = True
                except Exception:
                    plan_invalid = True
                    plan_rejection = "filing-correction-failed"
            except Exception:
                plan_invalid = True
                plan_rejection = "filing-correction-failed"
            else:
                semantic_revision_applied = not violations
                if violations:
                    plan_invalid = True
                    plan_rejection = "uncorrected-gate-violations"

    if plan_invalid:
        _restore_mutable(root, snapshot)
        reasons = {}
    result = {
        "passed": not plan_invalid and not violations,
        "violations": [{"path": item.path, "code": item.code} for item in violations],
        "changed_paths": sorted(reasons),
        "plan_rejection": plan_rejection or None,
        "semantic_revision_required": semantic_revision_required,
        "semantic_revision_attempted": semantic_revision_attempted,
        "semantic_revision_applied": semantic_revision_applied,
        "semantic_revision_model_requests": semantic_revision_model_requests,
        "semantic_revision_telemetry": semantic_revision_telemetry,
    }
    if return_plan:
        return result, active_plan
    return result


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
        audience=(tuple(case["audience"]) if case.get("audience") is not None else None),
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


SEED_SOURCE_PATH = "sources/2026/09/00000000-0000-4000-8000-000000000100.md"


def _seed_pages(root: Path, case: dict) -> None:
    """Seeded pages are prior knowledge: they cite their own earlier source, never the capture
    under evaluation, so preserving them cannot read as misattribution to that capture."""
    seeds = tuple(case.get("seed_pages", ()))
    if not seeds:
        return
    prior = {
        "source_path": SEED_SOURCE_PATH,
        "source_title": "Prior seeded knowledge",
        "audience": case.get("audience"),
    }
    prior_text = "\n\n".join(re.sub(r"\s*\(Source: `\{source_path\}`\)", "", item["body"]) for item in seeds)
    _envelope, _path, rendered = _source(prior, prior_text)
    seed_source = root / SEED_SOURCE_PATH
    seed_source.parent.mkdir(parents=True, exist_ok=True)
    seed_source.write_text(rendered, encoding="utf-8")
    for item in seeds:
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
                body=item["body"].replace("{source_path}", SEED_SOURCE_PATH),
                acl=(tuple(item["audience"]) if item.get("audience") is not None else None),
                sources=(SEED_SOURCE_PATH,),
                status="developing",
                page_id=item.get("id"),
                created=dt.date(2026, 9, 1),
                updated=dt.date(2026, 9, 1),
                entity_type=item.get("entity_type"),
                aliases=tuple(item.get("aliases", ())),
            ),
            encoding="utf-8",
        )
