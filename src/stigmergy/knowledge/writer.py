from __future__ import annotations

import datetime as dt
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from stigmergy.capture import ops, references, schema
from stigmergy.capture.extraction import extract_capture
from stigmergy.capture.source import render_source, source_path
from stigmergy.changes.model import ChangeRecord
from stigmergy.changes.store import get_change_by_commit, record_change
from stigmergy.entities.model import load_entities
from stigmergy.entities.service import (
    EntityOperationError,
    ProposalResolution,
    apply_proposals,
    delete_entity,
    merge_entities,
    remove_source_claims,
    resolve_reference,
)
from stigmergy.kernel.deadline import hard_deadline
from stigmergy.kernel.normalize import resolution_key
from stigmergy.knowledge import contradictions
from stigmergy.knowledge.context import actor_scope, filing_context, render_context
from stigmergy.knowledge.lint import Violation, check
from stigmergy.knowledge.pages import PageContractError, page_path, parse_page, render_page
from stigmergy.knowledge.plan import FilingPlan, PageMutation
from stigmergy.knowledge.planner import Planner
from stigmergy.knowledge.repair import repair_deterministic
from stigmergy.knowledge.sources import (
    SourceContractError,
    read_recompile_source,
    recompile_source_preflight,
    source_envelope,
)
from stigmergy.knowledge.trust import WRITER_EMAIL as AUTHOR_EMAIL
from stigmergy.knowledge.trust import WRITER_NAME as AUTHOR_NAME
from stigmergy.knowledge.write_guard import (
    WriteContext,
    WriteRefused,
    allow_create,
    allow_existing,
    allow_explicit_master,
)
from stigmergy.librarian import config, gitcmd

log = logging.getLogger(__name__)

WRITER_LOCK_KEY = int.from_bytes(b"KNOWWRIT", "big", signed=True)


class KnowledgeWriteError(RuntimeError):
    retryable = False


class WriterBusy(KnowledgeWriteError):
    retryable = True


class WriterDeadline(KnowledgeWriteError):
    retryable = True


class GateRefused(KnowledgeWriteError):
    pass


class CorpusUnavailable(GateRefused):
    retryable = True


@dataclass(frozen=True)
class WriterDeps:
    settings: object
    evidence: object
    planner: Planner
    repo: str


@dataclass(frozen=True)
class WriteResult:
    commit_sha: str
    change_id: str | None
    source_path: str = ""
    extraction: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)


def process(conn, item: dict, deps: WriterDeps) -> WriteResult:
    budget_s = config.operation_budget_s(timeout_s=deps.settings.timeout_s)
    with hard_deadline(
        budget_s,
        lambda: WriterDeadline("knowledge operation exceeded its queue lease budget"),
    ):
        return _process_with_lock(conn, item, deps)


def _process_with_lock(conn, item: dict, deps: WriterDeps) -> WriteResult:
    with ops.try_advisory_lock(conn, WRITER_LOCK_KEY) as acquired:
        if not acquired:
            raise WriterBusy("another knowledge write is active")
        base = gitcmd.base_ref(deps.repo, deps.settings.branch)
        recovered = _recover(conn, item, deps, base.sha)
        if recovered:
            return recovered
        if item["operation"] == schema.CAPTURE:
            return _capture(conn, item, deps, base)
        if item["operation"] == schema.DELETE:
            return _delete(conn, item, deps, base)
        if item["operation"] == schema.ENTITY:
            return _entity(conn, item, deps, base)
        if item["operation"] == schema.GARDEN:
            request = schema.parse_garden(item["request"])
            if request.mode == "recompile":
                return _recompile(conn, deps, base, request=request)
            return _garden(
                conn,
                deps,
                base,
                operation_id=str(request.operation_id),
                actor=request.actor.subject,
            )
        raise KnowledgeWriteError("unknown writer operation")


def _garden(
    conn,
    deps: WriterDeps,
    base: gitcmd.BaseRef,
    *,
    operation_id: str,
    actor: str,
) -> WriteResult:
    with (
        ops.job_run(conn, "garden", base_commit_sha=base.sha) as run,
        gitcmd.ephemeral_worktree(
            deps.repo,
            base.sha,
            root=deps.settings.worktree_root,
        ) as worktree,
    ):
        before = check(worktree)
        changed = repair_deterministic(worktree)
        after = check(worktree)
        model_requests = 0
        model_changed: dict[str, str] = {}
        if after:
            repair_run = deps.planner.repair(worktree=worktree, violations=after)
            model_requests = repair_run.model_requests
            model_changed = _apply_repair_plan(worktree, after, repair_run.plan)
            if model_changed:
                repair_deterministic(worktree)
            after = check(worktree)
        run.stats.update(
            detected=len(before),
            fixed=max(0, len(before) - len(after)),
            clean=not after,
            model_requests=model_requests,
        )
        if after:
            raise GateRefused(_violation_summary(after))
        entries = gitcmd.diff_entries(worktree)
        if not entries:
            run.head_commit_sha = base.sha
            run.stats["final_violations"] = 0
            report = {**run.stats, "commit_sha": "", "change_id": None}
            return WriteResult(commit_sha="", change_id=None, report=report)
        _gate_diff(entries, trigger="garden")
        _gate_garden_acl(deps.repo, base.sha, worktree, entries)
        commit, change = _commit_and_record(
            conn,
            deps,
            worktree=worktree,
            base_sha=base.sha,
            entries=entries,
            item_id=operation_id,
            trigger="garden",
            actor=actor,
            summary=f"Repaired {len(set(changed) | set(model_changed))} corpus path(s)",
            reasons={
                **{path: "Deterministic corpus repair" for path in changed},
                **model_changed,
            },
            job_run_id=str(run.id),
        )
        run.head_commit_sha = commit
        run.stats.update(
            commit_sha=commit,
            change_id=str(change.id),
            final_violations=0,
            clean=True,
        )
        return WriteResult(commit_sha=commit, change_id=str(change.id), report=dict(run.stats))


def _recompile(conn, deps: WriterDeps, base: gitcmd.BaseRef, *, request: schema.GardenRequest) -> WriteResult:
    """Rebuild derived knowledge from immutable sources in one master-only commit."""
    with (
        ops.job_run(conn, "recompile", base_commit_sha=base.sha) as run,
        gitcmd.ephemeral_worktree(
            deps.repo,
            base.sha,
            root=deps.settings.worktree_root,
        ) as worktree,
    ):
        groups, unrestricted = actor_scope(worktree, request.actor.subject)
        allow_explicit_master(WriteContext(groups, None, unrestricted))
        report = _recompile_derived(
            worktree,
            deps,
            rationale=request.rationale,
        )
        entries = gitcmd.diff_entries(worktree)
        if not entries:
            run.head_commit_sha = base.sha
            report.update(commit_sha="", change_id=None)
            run.stats.update(report)
            return WriteResult(commit_sha="", change_id=None, report=report)
        _gate_diff(entries, trigger="garden")
        reasons = {
            entry.path: "Recompiled derived knowledge from immutable sources"
            for entry in entries
        }
        commit, change = _commit_and_record(
            conn,
            deps,
            worktree=worktree,
            base_sha=base.sha,
            entries=entries,
            item_id=str(request.operation_id),
            trigger="garden",
            actor=request.actor.subject,
            summary=(
                f"Recompiled derived knowledge from {report['source_count']} immutable source(s)"
            ),
            reasons=reasons,
            job_run_id=str(run.id),
        )
        run.head_commit_sha = commit
        report.update(commit_sha=commit, change_id=str(change.id))
        run.stats.update(report)
        return WriteResult(commit_sha=commit, change_id=str(change.id), report=report)


def _recompile_derived(
    worktree: str,
    deps: WriterDeps,
    *,
    rationale: str,
) -> dict:
    """Compile all immutable sources into a fresh derived graph without committing.

    The caller owns an ephemeral worktree, so any failure abandons every derived mutation. Entity
    records remain while sources are refiled, which reuses valid opaque identities. Page anchors
    are not an identity liveness signal: source-backed records remain until an explicit lifecycle
    operation removes them.
    """
    before_entities = set(load_entities(worktree))
    previous_pages = {
        path: _path(worktree, path).read_text(encoding="utf-8")
        for path in _derived_paths(worktree)
    }
    source_paths = tuple(
        path.relative_to(worktree).as_posix()
        for path in sorted(Path(worktree, "sources").glob("*/*/*.md"))
    )
    if not source_paths:
        raise GateRefused("recompile requires at least one immutable source")
    try:
        recompile_source_preflight(worktree, source_paths)
    except SourceContractError as error:
        raise GateRefused(f"recompile source preflight failed: {error}") from error
    _clear_derived_pages(worktree)
    model_requests = 0
    gate_violations = 0
    for relative_source in source_paths:
        try:
            source = read_recompile_source(worktree, relative_source)
        except (OSError, UnicodeError, SourceContractError) as error:
            raise GateRefused(f"recompile source changed after preflight: {relative_source}") from error
        relative_source = source.path
        envelope = source_envelope(source)
        safe_context = filing_context(
            worktree,
            source_text=source.text,
            capture_acl=envelope.audience,
            actor_groups=None,
        )
        rendered_context = render_context(safe_context)
        plan_run = deps.planner.plan(
            worktree=worktree,
            envelope=envelope,
            source_path=relative_source,
            source_text=source.text,
            context=rendered_context,
        )
        model_requests += plan_run.model_requests
        plan = _recompile_identity_reuse(worktree, plan_run.plan)
        try:
            _apply_filing_plan(
                worktree,
                plan,
                context=WriteContext(None, envelope.audience, unrestricted=True),
                envelope=envelope,
                relative_source=relative_source,
                readable_artifacts=(source.body,),
                reasons={},
                visible_entities=tuple(safe_context["entities"]),
                # The master-only compiler may preserve an opaque identity even if its former
                # claim was outside this source's normal filing context. It still requires an
                # exact existing preferred claim and compatible entity type below.
                visible_entity_ids=frozenset(load_entities(worktree)),
                allowed_contradiction_sources=frozenset(
                    {relative_source, *(item["path"] for item in safe_context["source_evidence"])}
                ),
            )
            repair_deterministic(worktree)
            violations = check(worktree, editorial_paths=_derived_paths(worktree))
            if violations:
                gate_violations += len(violations)
                raise GateRefused(_violation_summary(violations))
        except (
            KnowledgeWriteError,
            PageContractError,
            EntityOperationError,
            WriteRefused,
            contradictions.ContradictionContractError,
        ) as error:
            raise GateRefused(f"recompile rejected source {relative_source}") from error
    _restore_recompiled_page_identity(worktree, previous_pages)
    final_violations = check(worktree)
    if final_violations:
        gate_violations += len(final_violations)
        raise GateRefused(_violation_summary(final_violations))
    after_entities = set(load_entities(worktree))
    entries = gitcmd.diff_entries(worktree)
    return {
        "operation": "recompile",
        "rationale": rationale,
        "source_count": len(source_paths),
        "page_mutations": len(entries),
        "retained_entities": len(before_entities & after_entities),
        # A valid source-backed identity may currently have no page anchor. Recompilation preserves
        # it; an operator must use the explicit entity lifecycle path to remove an orphan.
        "removed_entities": 0,
        "created_entities": len(after_entities - before_entities),
        "model_requests": model_requests,
        "link_health": {"violations": gate_violations, "clean": True},
        "idempotent": not entries,
    }


def _clear_derived_pages(root: str) -> None:
    for folder in ("wiki/notes", "wiki/concepts"):
        path = Path(root, folder)
        if not path.is_dir():
            continue
        for page in path.glob("*.md"):
            page.unlink()


def _derived_paths(root: str) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for folder in ("wiki/notes", "wiki/concepts")
        for path in Path(root, folder).glob("*.md")
    }


def _restore_recompiled_page_identity(root: str, previous_pages: dict[str, str]) -> None:
    """Keep durable page IDs/dates stable when the rebuilt page has the same logical path."""
    for path in _derived_paths(root) & set(previous_pages):
        before = parse_page(path, previous_pages[path])
        after = parse_page(path, _path(root, path).read_text(encoding="utf-8"))
        if before.role != after.role or before.title != after.title:
            continue
        _path(root, path).write_text(
            render_page(
                path=after.path,
                role=after.role,
                title=after.title,
                body=after.body,
                acl=after.acl,
                entities=after.entities,
                sources=after.sources,
                status=after.status,
                page_id=before.page_id,
                created=before.created,
                updated=after.updated,
            ),
            encoding="utf-8",
        )


def _recompile_identity_reuse(root: str, plan: FilingPlan) -> FilingPlan:
    """Attach exact, type-compatible existing identities to a recompilation plan.

    This is preservation rather than entity inference: no names are extracted or invented, and an
    ambiguous match remains a model decision. It gives an otherwise equivalent rebuilt graph its
    prior opaque IDs without weakening ordinary capture identity rules.
    """
    records = load_entities(root)
    proposals = []
    for proposal in plan.entities:
        if proposal.same_as or (proposal.external_namespace and proposal.external_id):
            proposals.append(proposal)
            continue
        matches = [
            entity_id
            for entity_id, record in records.items()
            if record.entity_type == proposal.entity_type
            and sum(
                claim.kind == "preferred" and claim.normalized == resolution_key(proposal.name)
                for claim in record.claims
            ) == 1
        ]
        proposals.append(
            proposal.model_copy(update={"same_as": matches[0]})
            if len(matches) == 1
            else proposal
        )
    return plan.model_copy(update={"entities": tuple(proposals)})


def _capture(conn, item: dict, deps: WriterDeps, base: gitcmd.BaseRef) -> WriteResult:
    envelope = schema.parse_capture(item["request"])
    extracted = extract_capture(
        deps.evidence,
        envelope,
        ocr_model=deps.settings.ocr_model,
    )
    relative_source = source_path(envelope)
    source_text = render_source(envelope, extracted)
    trigger = "contradiction_resolution" if envelope.intent.resolution_of else "capture"

    with gitcmd.ephemeral_worktree(
        deps.repo,
        base.sha,
        root=deps.settings.worktree_root,
    ) as worktree:
        _write_new(worktree, relative_source, source_text)
        groups, unrestricted = actor_scope(worktree, envelope.actor.subject)
        context = WriteContext(
            actor_groups=groups,
            content_acl=envelope.audience,
            unrestricted=unrestricted,
        )
        safe_context = filing_context(
            worktree,
            source_text=source_text,
            capture_acl=envelope.audience,
            actor_groups=groups,
        )
        visible_entities = tuple(safe_context["entities"])
        visible_entity_ids = frozenset(
            item["id"] for item in visible_entities
        )
        allowed_contradiction_sources = frozenset(
            {
                relative_source,
                *(item["path"] for item in safe_context["source_evidence"]),
            }
        )
        rendered_context = render_context(safe_context)
        plan_run = deps.planner.plan(
            worktree=worktree,
            envelope=envelope,
            source_path=relative_source,
            source_text=source_text,
            context=rendered_context,
        )
        reasons = {relative_source: "Archived immutable readable evidence"}
        baseline = check(worktree)
        if baseline:
            raise CorpusUnavailable(_violation_summary(baseline))
        snapshot = _snapshot_mutable(worktree)
        plan_invalid = False
        plan_rejection = ""
        plan_skipped: list[str] = []
        model_requests = plan_run.model_requests
        try:
            _apply_filing_plan(
                worktree,
                plan_run.plan,
                context=context,
                envelope=envelope,
                relative_source=relative_source,
                readable_artifacts=tuple(item.result.text for item in extracted),
                reasons=reasons,
                visible_entities=visible_entities,
                visible_entity_ids=visible_entity_ids,
                allowed_contradiction_sources=allowed_contradiction_sources,
                skipped=plan_skipped,
            )
            repair_deterministic(worktree)
        except (
            KnowledgeWriteError,
            PageContractError,
            EntityOperationError,
            WriteRefused,
            contradictions.ContradictionContractError,
        ) as error:
            plan_invalid = True
            plan_rejection = str(error)
        editorial_paths = frozenset(
            path for path in reasons if path.startswith(("wiki/notes/", "wiki/concepts/"))
        )
        violations = check(worktree, editorial_paths=editorial_paths) if not plan_invalid else ()
        if violations:
            try:
                repair_files = _capture_repair_files(
                    worktree,
                    violations=violations,
                    editorial_paths=editorial_paths,
                    snapshot=snapshot,
                    context=context,
                )
                repair_run = deps.planner.repair(
                    worktree=worktree,
                    violations=violations,
                    files=repair_files,
                    source_path=relative_source,
                    source_text=source_text,
                    context=rendered_context,
                    max_requests=max(0, int(deps.settings.max_turns) - model_requests),
                )
                model_requests += repair_run.model_requests
                repaired = _apply_repair_plan(
                    worktree,
                    violations,
                    repair_run.plan,
                    authorized_files=repair_files,
                    context=context,
                    existing_paths=frozenset(snapshot),
                )
                if repaired:
                    reasons.update(repaired)
                    repair_deterministic(worktree)
                violations = check(worktree, editorial_paths=editorial_paths)
            except GateRefused:
                plan_invalid = True
                plan_rejection = "knowledge gates found invalid-repair-plan"
            else:
                if violations:
                    plan_invalid = True
                    # Codes only: a violation's path names a page, and the report is read by the submitter.
                    codes = ", ".join(sorted({item.code for item in violations}))
                    plan_rejection = f"knowledge gates found {codes}"
        if plan_invalid:
            # Gate messages are fixed sentences, never source or model text.
            log.warning("filing plan rejected for %s: %s", envelope.capture_id, plan_rejection)
            _restore_mutable(worktree, snapshot)
            reasons = {relative_source: "Archived immutable readable evidence"}
            fallback = check(worktree)
            if fallback:
                raise GateRefused(_violation_summary(fallback))
        entries = gitcmd.diff_entries(worktree)
        _gate_diff(entries, trigger=trigger, expected_source=relative_source)
        wiki_changes = sum(1 for entry in entries if entry.path.startswith("wiki/"))
        summary = (
            "Archived source without wiki changes"
            if plan_invalid or not wiki_changes
            else (
                f"Applied {wiki_changes} wiki change(s); "
                f"skipped {len(plan_skipped)} plan operation(s)"
            )
        )
        commit, change = _commit_and_record(
            conn,
            deps,
            worktree=worktree,
            base_sha=base.sha,
            entries=entries,
            item_id=str(envelope.capture_id),
            trigger=trigger,
            actor=envelope.actor.subject,
            summary=summary,
            reasons=reasons,
            capture_id=str(envelope.capture_id),
        )
        references.record_capture(conn, envelope, extracted, relative_source)
    return WriteResult(
        commit_sha=commit,
        change_id=str(change.id),
        source_path=relative_source,
        extraction={
            "artifacts": [artifact.metadata() for artifact in extracted],
        },
        report={
            "summary": summary,
            "wiki_changes": wiki_changes,
            "model_requests": model_requests,
            "plan_rejected": plan_invalid,
            "plan_rejection": plan_rejection,
            # Empty when the plan was rejected outright: nothing of it was kept to drop from.
            "plan_skipped": [] if plan_invalid else plan_skipped,
        },
    )


def _delete(conn, item: dict, deps: WriterDeps, base: gitcmd.BaseRef) -> WriteResult:
    request = schema.parse_delete(item["request"])
    with gitcmd.ephemeral_worktree(
        deps.repo,
        base.sha,
        root=deps.settings.worktree_root,
    ) as worktree:
        groups, unrestricted = actor_scope(worktree, request.actor.subject)
        context = WriteContext(groups, None, unrestricted)
        allow_explicit_master(context)
        missing = [path for path in request.paths if not _path(worktree, path).is_file()]
        if missing:
            raise KnowledgeWriteError("one or more requested paths do not exist")
        reasons = {path: request.rationale for path in request.paths}
        for relative in request.paths:
            _path(worktree, relative).unlink()
        reasons.update(_sweep_deleted_references(worktree, set(request.paths)))
        repair_deterministic(worktree)
        violations = check(worktree)
        if violations:
            raise GateRefused(_violation_summary(violations))
        entries = gitcmd.diff_entries(worktree)
        _gate_diff(entries, trigger="delete", deleted_sources=set(request.paths))
        commit, change = _commit_and_record(
            conn,
            deps,
            worktree=worktree,
            base_sha=base.sha,
            entries=entries,
            item_id=str(request.operation_id),
            trigger="delete",
            actor=request.actor.subject,
            summary=f"Deleted {len(request.paths)} knowledge path(s)",
            reasons=reasons,
        )
        _release_deleted_evidence(conn, deps.evidence, set(request.paths))
    return WriteResult(
        commit_sha=commit,
        change_id=str(change.id),
        report={"summary": f"Deleted {len(request.paths)} knowledge path(s)"},
    )


def _entity(conn, item: dict, deps: WriterDeps, base: gitcmd.BaseRef) -> WriteResult:
    request = schema.parse_entity_operation(item["request"])
    with gitcmd.ephemeral_worktree(
        deps.repo,
        base.sha,
        root=deps.settings.worktree_root,
    ) as worktree:
        groups, unrestricted = actor_scope(worktree, request.actor.subject)
        allow_explicit_master(WriteContext(groups, None, unrestricted))
        if request.action == "merge":
            if request.evidence is None:
                raise GateRefused("merge evidence is missing")
            canonical = merge_entities(
                worktree,
                request.entity_ids,
                at=dt.datetime.now(dt.UTC),
                evidence=request.evidence,
            )
            summary = f"Merged {len(request.entity_ids)} identities into {canonical}"
            reason = f"Verified by {request.evidence.label()}. {request.rationale}"
        else:
            delete_entity(worktree, request.entity_ids[0])
            summary = f"Deleted identity {request.entity_ids[0]}"
            reason = request.rationale
        repair_deterministic(worktree)
        violations = check(worktree)
        if violations:
            raise GateRefused(_violation_summary(violations))
        entries = gitcmd.diff_entries(worktree)
        _gate_diff(entries, trigger="entity")
        reasons = {entry.path: reason for entry in entries}
        commit, change = _commit_and_record(
            conn,
            deps,
            worktree=worktree,
            base_sha=base.sha,
            entries=entries,
            item_id=str(request.operation_id),
            trigger="entity",
            actor=request.actor.subject,
            summary=summary,
            reasons=reasons,
        )
    return WriteResult(
        commit_sha=commit,
        change_id=str(change.id),
        report={"summary": summary, "reason": reason},
    )


def _apply_filing_plan(
    root: str,
    plan: FilingPlan,
    *,
    context: WriteContext,
    envelope: schema.CaptureEnvelope,
    relative_source: str,
    readable_artifacts: tuple[str, ...],
    reasons: dict[str, str],
    visible_entities: tuple[dict, ...],
    visible_entity_ids: frozenset[str],
    allowed_contradiction_sources: frozenset[str],
    skipped: list[str] | None = None,
) -> None:
    """Apply one derived graph plan atomically, or reject it as a whole.

    The immutable source is written before this function. Every graph mutation, entity proposal,
    anchor, contradiction, and resolution is therefore all-or-nothing with respect to that source.
    """
    if envelope.intent.resolution_of:
        allow_explicit_master(context)
    for proposal in plan.contradictions:
        proposed_sources = {claim.source for claim in proposal.claims}
        if not proposed_sources <= allowed_contradiction_sources:
            raise KnowledgeWriteError(
                "contradiction cites evidence outside the supplied filing context"
            )
    proposal_resolution = ProposalResolution.empty()
    if plan.entities:
        proposal_resolution = apply_proposals(
            root,
            plan.entities,
            acl=envelope.audience,
            source=relative_source,
            readable_artifacts=readable_artifacts,
            actor=envelope.actor.subject,
            at=envelope.origin.captured_at,
            allowed_same_as=visible_entity_ids,
        )
    updated_pages: set[str] = set()
    for index, mutation in enumerate(plan.mutations):
        try:
            changed_path = _apply_page_mutation(
                root,
                mutation,
                context=context,
                source=relative_source,
                proposal_resolution=proposal_resolution,
                at=envelope.origin.captured_at.date(),
                reasons=reasons,
                visible_entity_ids=visible_entity_ids,
            )
        except (KnowledgeWriteError, PageContractError, WriteRefused) as error:
            raise KnowledgeWriteError(f"planned mutation {index} could not be applied") from error
        if mutation.action == "update" and changed_path:
            updated_pages.add(changed_path)
    for index, proposal in enumerate(plan.contradictions):
        target = _path(root, proposal.page_path)
        if not target.is_file():
            raise KnowledgeWriteError(f"planned contradiction {index} targets no page")
        original = target.read_text(encoding="utf-8")
        page = parse_page(proposal.page_path, original)
        try:
            allow_existing(context, page.acl)
        except WriteRefused as error:
            raise KnowledgeWriteError(f"planned contradiction {index} is not authorized") from error
        before = set(check(root))
        try:
            record = contradictions.from_proposal(proposal)
            candidate = render_page(
                path=page.path,
                role=page.role,
                title=page.title,
                body=contradictions.append(page.body, record),
                acl=page.acl,
                entities=page.entities,
                sources=tuple(
                    dict.fromkeys((*page.sources, *(claim.source for claim in record.claims)))
                ),
                status=page.status,
                page_id=page.page_id,
                created=page.created,
                updated=envelope.origin.captured_at.date(),
            )
        except (contradictions.ContradictionContractError, PageContractError) as error:
            raise KnowledgeWriteError(f"planned contradiction {index} is invalid") from error
        target.write_text(candidate, encoding="utf-8")
        introduced = set(check(root)) - before
        if introduced:
            target.write_text(original, encoding="utf-8")
            raise KnowledgeWriteError(f"planned contradiction {index} failed knowledge gates")
        reasons[proposal.page_path] = proposal.explanation
    if plan.resolved_contradictions:
        expected = envelope.intent.resolution_of
        if not expected or tuple(plan.resolved_contradictions) != (expected,):
            raise KnowledgeWriteError("only the contradiction named by the capture can be resolved")
        try:
            removed = _remove_contradiction(
                root,
                expected,
                at=envelope.origin.captured_at.date(),
                context=context,
                resolution_source=relative_source,
                updated_pages=frozenset(updated_pages),
            )
        except KnowledgeWriteError as error:
            raise KnowledgeWriteError("planned contradiction resolution could not be applied") from error
        reasons.update({path: envelope.intent.rationale or "Resolved contradiction" for path in removed})


def _note_skip(skipped: list[str] | None, entry: str) -> None:
    log.warning("filing plan item dropped: %s", entry)
    if skipped is not None:
        skipped.append(entry)


def _apply_page_mutation(
    root: str,
    mutation: PageMutation,
    *,
    context: WriteContext,
    source: str,
    proposal_resolution: ProposalResolution,
    at: dt.date,
    reasons: dict[str, str],
    visible_entity_ids: frozenset[str],
) -> str | None:
    records = load_entities(root)
    if mutation.action == "create":
        target_path = page_path(mutation.role or "", mutation.title or "")
        allow_create(context, context.content_acl)
        entities = (
            _resolve_entities(
                records,
                mutation.entities,
                proposal_resolution,
                visible_entity_ids,
            )
            if mutation.entities is not None
            else ()
        )
        body = mutation.body or ""
        try:
            if contradictions.parse_all(body):
                raise KnowledgeWriteError(
                    "contradictions must use the structured contradiction plan"
                )
        except contradictions.ContradictionContractError as error:
            raise KnowledgeWriteError(
                "planned page body has invalid contradiction markers"
            ) from error
        text = render_page(
            path=target_path,
            role=mutation.role or "",
            title=mutation.title or "",
            body=body,
            acl=context.content_acl,
            entities=entities,
            sources=(source,),
            status=mutation.status or "developing",
            created=at,
            updated=at,
        )
        _write_new(root, target_path, text)
        reasons[target_path] = mutation.reason
        return target_path

    target_path = mutation.path or ""
    target = _path(root, target_path)
    if not target.is_file():
        raise KnowledgeWriteError("planned page does not exist")
    page = parse_page(target_path, target.read_text(encoding="utf-8"))
    allow_existing(context, page.acl)
    if mutation.action == "delete":
        if contradictions.parse_all(page.body):
            raise KnowledgeWriteError(
                "a capture cannot delete a page with unresolved contradictions"
            )
        target.unlink()
        reasons[target_path] = mutation.reason
        return None

    title = mutation.title or page.title
    destination = page_path(page.role, title)
    entities = (
        page.entities
        if mutation.entities is None
        else _resolve_entities(
            records,
            mutation.entities,
            proposal_resolution,
            visible_entity_ids,
        )
    )
    body = _preserve_contradictions(page.body, mutation.body or "")
    rendered = render_page(
        path=destination,
        role=page.role,
        title=title,
        body=body,
        acl=page.acl,
        entities=entities,
        sources=tuple(dict.fromkeys((*page.sources, source))),
        status=mutation.status or page.status,
        page_id=page.page_id,
        created=page.created,
        updated=at,
    )
    if destination != target_path:
        if _path(root, destination).exists():
            raise KnowledgeWriteError("renamed page destination already exists")
        target.unlink()
        _write_new(root, destination, rendered)
        reasons[target_path] = mutation.reason
    else:
        target.write_text(rendered, encoding="utf-8")
    reasons[destination] = mutation.reason
    return destination


def _preserve_contradictions(existing: str, proposed: str) -> str:
    try:
        current = {
            item.record.contradiction_id: item.record
            for item in contradictions.parse_all(existing)
        }
        candidate = {
            item.record.contradiction_id: item.record
            for item in contradictions.parse_all(proposed)
        }
    except contradictions.ContradictionContractError as error:
        raise KnowledgeWriteError(
            "planned page body has invalid contradiction markers"
        ) from error
    if set(candidate) - set(current):
        raise KnowledgeWriteError(
            "contradictions must use the structured contradiction plan"
        )
    if any(candidate[item_id] != record for item_id, record in current.items() if item_id in candidate):
        raise KnowledgeWriteError(
            "existing contradiction claims cannot be rewritten in a page mutation"
        )
    result = proposed
    for item_id, record in current.items():
        if item_id not in candidate:
            result = contradictions.append(result, record)
    return result


def _resolve_entities(
    records,
    values,
    proposal_resolution: ProposalResolution,
    visible_entity_ids: frozenset[str],
) -> tuple[str, ...]:
    visible_records = {
        entity_id: record
        for entity_id, record in records.items()
        if entity_id in visible_entity_ids
    }
    result = []
    for value in values:
        candidates = proposal_resolution.candidates(value)
        resolved = proposal_resolution.resolve(value) if candidates else None
        if not candidates:
            resolved = resolve_reference(visible_records, value)
        if not resolved:
            raise KnowledgeWriteError("entity reference is not unambiguous")
        result.append(resolved)
    return tuple(dict.fromkeys(result))


def _remove_contradiction(
    root: str,
    contradiction_id: str,
    *,
    at: dt.date,
    context: WriteContext,
    resolution_source: str,
    updated_pages: frozenset[str],
) -> tuple[str, ...]:
    candidates = []
    for folder in ("wiki/notes", "wiki/concepts"):
        base = _path(root, folder)
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.md")):
            relative = path.relative_to(root).as_posix()
            page = parse_page(relative, path.read_text(encoding="utf-8"))
            try:
                allow_existing(context, page.acl)
            except WriteRefused:
                continue
            body, removed = contradictions.remove(page.body, contradiction_id)
            if removed:
                candidates.append((path, relative, page, body))
    if any(relative not in updated_pages for _path, relative, _page, _body in candidates):
        raise KnowledgeWriteError("target page requires an accepted update")
    for path, _relative, page, body in candidates:
        path.write_text(
            render_page(
                path=page.path,
                role=page.role,
                title=page.title,
                body=body,
                acl=page.acl,
                entities=page.entities,
                sources=tuple(dict.fromkeys((*page.sources, resolution_source))),
                status=page.status,
                page_id=page.page_id,
                created=page.created,
                updated=at,
            ),
            encoding="utf-8",
        )
    return tuple(relative for _path, relative, _page, _body in candidates)


def _sweep_deleted_references(root: str, deleted: set[str]) -> dict[str, str]:
    reasons = {}
    deleted_stems = {Path(path).stem.casefold() for path in deleted}
    deleted_sources = {path for path in deleted if path.startswith("sources/")}
    at = dt.datetime.now(dt.UTC)
    for folder in ("wiki/notes", "wiki/concepts"):
        base = _path(root, folder)
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.md")):
            relative = path.relative_to(root).as_posix()
            if relative in deleted:
                continue
            page = parse_page(relative, path.read_text(encoding="utf-8"))
            body = _remove_wikilinks(page.body, deleted_stems)
            for located in reversed(contradictions.parse_all(body)):
                if any(claim.source in deleted_sources for claim in located.record.claims):
                    body, _removed = contradictions.remove(body, located.record.contradiction_id)
            sources = tuple(source for source in page.sources if source not in deleted_sources)
            if body == page.body and sources == page.sources:
                continue
            path.write_text(
                render_page(
                    path=page.path,
                    role=page.role,
                    title=page.title,
                    body=body,
                    acl=page.acl,
                    entities=page.entities,
                    sources=sources,
                    status=page.status,
                    page_id=page.page_id,
                    created=page.created,
                    updated=at.date(),
                ),
                encoding="utf-8",
            )
            reasons[relative] = "Removed references to deleted knowledge"
    for path in remove_source_claims(root, deleted_sources, at=at):
        reasons.setdefault(path, "Removed identity provenance from deleted evidence")
    return reasons


_WIKILINK_RE = re.compile(r"!?\[\[([^\[\]]+?)\]\]")


def _remove_wikilinks(text: str, stems: set[str]) -> str:
    def replace(match):
        target, separator, label = match.group(1).partition("|")
        stem = Path(target.split("#", 1)[0].removesuffix(".md")).name.casefold()
        if stem not in stems:
            return match.group(0)
        return label.strip() if separator else target.strip()

    return _WIKILINK_RE.sub(replace, text)


def _gate_diff(
    entries,
    *,
    trigger: str,
    expected_source: str = "",
    deleted_sources: set[str] | None = None,
) -> None:
    if not entries:
        raise GateRefused("writer produced no change")
    deleted_sources = deleted_sources or set()
    for entry in entries:
        if not entry.is_regular_file:
            raise GateRefused("writer changed a non-regular file")
        allowed = (
            entry.path.startswith(("wiki/notes/", "wiki/concepts/", "wiki/entities/", "sources/"))
            or entry.path == "ops/entity-registry.json"
        )
        if not allowed:
            raise GateRefused("writer changed a path outside the knowledge contract")
        if entry.path.startswith("sources/"):
            if trigger in {"capture", "contradiction_resolution"}:
                if entry.path != expected_source or entry.status != "A":
                    raise GateRefused("capture attempted to mutate an existing source")
            elif trigger == "delete":
                if entry.path not in deleted_sources or entry.status != "D":
                    raise GateRefused("delete attempted an undeclared source mutation")
            else:
                raise GateRefused("maintenance attempted to mutate a source")


def _commit_and_record(
    conn,
    deps: WriterDeps,
    *,
    worktree: str,
    base_sha: str,
    entries,
    item_id: str,
    trigger: str,
    actor: str,
    summary: str,
    reasons: dict[str, str],
    capture_id: str | None = None,
    job_run_id: str | None = None,
) -> tuple[str, ChangeRecord]:
    safe_summary = _safe_summary(summary)
    message = f"feat(knowledge): {safe_summary[:68]}\n\nStigmergy-Operation: {item_id}\nStigmergy-Trigger: {trigger}\n"
    commit = gitcmd.commit(
        worktree,
        message=message,
        author_name=AUTHOR_NAME,
        author_email=AUTHOR_EMAIL,
        gated_entries=entries,
    )
    commit = _advance(deps, worktree, commit, base_sha)
    parent = gitcmd.run("rev-parse", f"{commit}^", cwd=deps.repo).stdout.strip()
    change = record_change(
        conn,
        deps.evidence,
        repo=deps.repo,
        trigger=trigger,
        actor=actor,
        parent_commit_sha=parent,
        commit_sha=commit,
        summary=safe_summary,
        reasons=reasons,
        capture_id=capture_id,
        job_run_id=job_run_id,
    )
    return commit, change


def _advance(deps: WriterDeps, worktree: str, commit: str, parent: str) -> str:
    if gitcmd.origin_url(deps.repo):
        return gitcmd.push(
            worktree,
            branch=deps.settings.branch,
        )
    gitcmd.run(
        "update-ref",
        f"refs/heads/{deps.settings.branch}",
        commit,
        parent,
        cwd=deps.repo,
    )
    return commit


def _recover(conn, item: dict, deps: WriterDeps, head: str) -> WriteResult | None:
    operation_id = str(item["id"])
    process = gitcmd.run(
        "log",
        "--format=%H",
        "--fixed-strings",
        f"--grep=Stigmergy-Operation: {operation_id}",
        "-1",
        head,
        cwd=deps.repo,
        check=False,
    )
    commit = process.stdout.strip()
    if not commit:
        return None
    change = get_change_by_commit(conn, commit)
    request = item["request"]
    trigger = item["operation"]
    capture_id = None
    relative_source = ""
    if trigger == schema.CAPTURE:
        envelope = schema.parse_capture(request)
        capture_id = str(envelope.capture_id)
        relative_source = source_path(envelope)
        trigger = "contradiction_resolution" if envelope.intent.resolution_of else "capture"
    parent = gitcmd.run("rev-parse", f"{commit}^", cwd=deps.repo).stdout.strip()
    if item["operation"] == schema.GARDEN:
        garden_request = schema.parse_garden(request)
        change, report = _recover_garden(
            conn,
            deps,
            change=change,
            commit=commit,
            parent=parent,
            actor=item["submitted_by"],
            job_name="recompile" if garden_request.mode == "recompile" else "garden",
            trigger="garden",
        )
    elif change is None:
        change = record_change(
            conn,
            deps.evidence,
            repo=deps.repo,
            trigger=trigger,
            actor=item["submitted_by"],
            parent_commit_sha=parent,
            commit_sha=commit,
            summary="Recovered a landed knowledge operation",
            capture_id=capture_id,
        )
        report = {"summary": change.summary, "reconciled": True}
    else:
        report = {"summary": change.summary, "reconciled": True}
    if item["operation"] == schema.CAPTURE:
        envelope = schema.parse_capture(request)
        extracted = extract_capture(
            deps.evidence,
            envelope,
            ocr_model=deps.settings.ocr_model,
        )
        references.record_capture(conn, envelope, extracted, relative_source)
    elif item["operation"] == schema.DELETE:
        deletion = schema.parse_delete(request)
        _release_deleted_evidence(conn, deps.evidence, set(deletion.paths))
    return WriteResult(
        commit_sha=commit,
        change_id=str(change.id),
        source_path=relative_source,
        report=report,
    )


def _recover_garden(
    conn,
    deps: WriterDeps,
    *,
    change: ChangeRecord | None,
    commit: str,
    parent: str,
    actor: str,
    job_name: str = "garden",
    trigger: str = "garden",
) -> tuple[ChangeRecord, dict]:
    stats = {
        "reconciled": True,
        "commit_sha": commit,
        "final_violations": 0,
        "clean": True,
    }
    if change is None:
        with ops.job_run(conn, job_name, base_commit_sha=parent) as run:
            change = record_change(
                conn,
                deps.evidence,
                repo=deps.repo,
                trigger=trigger,
                actor=actor,
                parent_commit_sha=parent,
                commit_sha=commit,
                summary="Recovered a landed knowledge operation",
                job_run_id=str(run.id),
            )
            run.head_commit_sha = commit
            run.stats.update(stats, change_id=str(change.id))
    elif change.job_run_id:
        ops.reconcile_job(
            conn,
            change.job_run_id,
            head_commit_sha=commit,
            stats={**stats, "change_id": str(change.id)},
        )
    return change, {**stats, "change_id": str(change.id)}


def _release_deleted_evidence(conn, evidence, paths: set[str]) -> None:
    sources = {path for path in paths if path.startswith("sources/")}
    for reference in references.release_sources(conn, sources):
        evidence.delete(reference)


def _capture_repair_files(
    root: str,
    *,
    violations: tuple[Violation, ...],
    editorial_paths: frozenset[str],
    snapshot: dict[str, bytes],
    context: WriteContext,
) -> dict[str, str]:
    """Authorize every repair target before reading any candidate body for the model."""
    paths = frozenset(violation.path for violation in violations)
    if not paths or not paths <= editorial_paths:
        raise GateRefused("repair includes a path outside this capture's visible graph changes")
    if not all(path.startswith(("wiki/notes/", "wiki/concepts/")) for path in paths):
        raise GateRefused("repair may only target capture knowledge pages")
    for path in paths:
        if path in snapshot:
            before = parse_page(path, snapshot[path].decode("utf-8"))
            try:
                allow_existing(context, before.acl)
            except WriteRefused as error:
                raise GateRefused("repair target is outside the capture audience") from error
        else:
            try:
                allow_create(context, context.content_acl)
            except WriteRefused as error:
                raise GateRefused("repair target is outside the capture audience") from error
    files = {}
    for path in sorted(paths):
        target = _path(root, path)
        if not target.is_file():
            raise GateRefused("repair target is missing")
        text = target.read_text(encoding="utf-8")
        if len(text.encode("utf-8")) > 100_000:
            raise GateRefused("repair target exceeds its byte limit")
        files[path] = text
    return files


def _apply_repair_plan(
    root: str,
    violations: tuple[Violation, ...],
    plan,
    *,
    authorized_files: dict[str, str] | None = None,
    context: WriteContext | None = None,
    existing_paths: frozenset[str] = frozenset(),
) -> dict[str, str]:
    allowed = {
        violation.path
        for violation in violations
        if violation.path.startswith(("wiki/notes/", "wiki/concepts/"))
    }
    changed = {}
    for mutation in plan.mutations:
        if mutation.path not in allowed:
            raise GateRefused("model repair targeted a path outside its violations")
        if authorized_files is not None and mutation.path not in authorized_files:
            raise GateRefused("model repair targeted a path outside this capture's authorization")
        target = _path(root, mutation.path)
        if not target.is_file():
            raise GateRefused("model repair targeted a missing path")
        if authorized_files is not None:
            before = parse_page(mutation.path, authorized_files[mutation.path])
            if context is None:
                raise GateRefused("capture repair is missing its authorization context")
            try:
                if mutation.path in existing_paths:
                    allow_existing(context, before.acl)
                else:
                    allow_create(context, before.acl)
            except WriteRefused as error:
                raise GateRefused("model repair is outside the capture audience") from error
            after = parse_page(mutation.path, mutation.text)
            if (
                after.role != before.role
                or after.title != before.title
                or after.acl != before.acl
                or after.entities != before.entities
                or after.sources != before.sources
                or after.status != before.status
                or after.page_id != before.page_id
                or after.created != before.created
                or after.updated != before.updated
            ):
                raise GateRefused("model repair may change only the authorized page body")
        target.write_text(mutation.text, encoding="utf-8")
        changed[mutation.path] = mutation.reason
    return changed


def _gate_garden_acl(repo: str, base_sha: str, worktree: str, entries) -> None:
    for entry in entries:
        if entry.status != "M" or not entry.path.startswith(("wiki/notes/", "wiki/concepts/")):
            continue
        before = parse_page(entry.path, gitcmd.show(repo, base_sha, entry.path))
        after = parse_page(
            entry.path,
            _path(worktree, entry.path).read_text(encoding="utf-8"),
        )
        if before.acl != after.acl:
            raise GateRefused("gardening cannot change page visibility")


def _write_new(root: str, relative: str, text: str) -> None:
    target = _path(root, relative)
    if target.exists():
        raise KnowledgeWriteError("writer target already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


_MUTABLE_FOLDERS = ("wiki/notes", "wiki/concepts", "wiki/entities")
_MUTABLE_FILES = ("ops/entity-registry.json",)


def _snapshot_mutable(root: str) -> dict[str, bytes]:
    snapshot = {}
    for folder in _MUTABLE_FOLDERS:
        base = _path(root, folder)
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            snapshot[path.relative_to(root).as_posix()] = path.read_bytes()
    for relative in _MUTABLE_FILES:
        path = _path(root, relative)
        if path.is_file():
            snapshot[relative] = path.read_bytes()
    return snapshot


def _restore_mutable(root: str, snapshot: dict[str, bytes]) -> None:
    current = _snapshot_mutable(root)
    for relative in sorted(set(current) - set(snapshot)):
        _path(root, relative).unlink()
    for relative, data in snapshot.items():
        path = _path(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _path(root: str, relative: str) -> Path:
    normalized = relative.replace("\\", "/")
    if normalized.startswith("/") or ".." in normalized.split("/"):
        raise KnowledgeWriteError("writer path is invalid")
    target = Path(root, *normalized.split("/"))
    root_real = os.path.realpath(root)
    parent_real = os.path.realpath(target.parent)
    if os.path.commonpath((root_real, parent_real)) != root_real:
        raise KnowledgeWriteError("writer path escapes the worktree")
    if target.is_symlink():
        raise KnowledgeWriteError("writer target cannot be a symlink")
    return target


def _safe_summary(value: str) -> str:
    text = " ".join(str(value).split())
    text = re.sub(
        r"(?i)\b(stigmergy-operation|stigmergy-trigger|submitted-by|co-authored-by)\s*:",
        "",
        text,
    )
    return text[:500] or "Updated team knowledge"


def _violation_summary(violations: tuple[Violation, ...]) -> str:
    shown = "; ".join(f"{item.path}: {item.code}" for item in violations[:8])
    suffix = f"; {len(violations) - 8} more" if len(violations) > 8 else ""
    return f"knowledge gates found {shown}{suffix}"
