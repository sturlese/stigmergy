from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from stigmergy.capture import ops, queue, references, schema
from stigmergy.capture.errors import QueueStateError
from stigmergy.capture.extraction import extract_capture
from stigmergy.capture.source import render_source, source_path
from stigmergy.changes.model import ChangeRecord
from stigmergy.changes.store import get_change_by_commit, record_change
from stigmergy.entities.service import (
    EntityOperationError,
    entity_pages,
    merge_entities,
    name_index,
    rename_entity,
    rewrite_links,
)
from stigmergy.kernel import layers
from stigmergy.kernel.deadline import hard_deadline
from stigmergy.kernel.normalize import resolution_key
from stigmergy.knowledge import aliases, contradictions, layered
from stigmergy.knowledge.context import (
    actor_scope,
    filing_context,
    render_context,
)
from stigmergy.knowledge.lint import Violation, check
from stigmergy.knowledge.pages import (
    KNOWLEDGE_FOLDERS,
    PageContractError,
    page_path,
    parse_page,
    path_role,
    render_like,
    render_page,
    title_stem,
)
from stigmergy.knowledge.plan import FilingPlan, PageMutation
from stigmergy.knowledge.planner import Planner
from stigmergy.knowledge.relationships import source_attributions
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
    is_broader,
    may_link,
)
from stigmergy.librarian import gitcmd
from stigmergy.server import roster

log = logging.getLogger(__name__)

WRITER_LOCK_KEY = int.from_bytes(b"KNOWWRIT", "big", signed=True)
MAINTENANCE_REPAIR_MAX_REQUESTS = 2
GARDEN_ALIAS_CANDIDATES = 32
GARDEN_ALIAS_WINDOW_S = 60.0


class KnowledgeWriteError(RuntimeError):
    retryable = False


class WriterBusy(KnowledgeWriteError):
    retryable = True


# The alias step stops this long before the capture budget ends, so projection and commit still fit.
ALIAS_BUDGET_RESERVE_S = 60.0


class WriterDeadline(KnowledgeWriteError):
    pass


class GateRefused(KnowledgeWriteError):
    pass


class ExistingSourceBlockNotPreserved(KnowledgeWriteError):
    code = "existing-source-block-not-preserved"

    def __init__(self, path: str = "", lost: tuple[str, ...] = ()) -> None:
        super().__init__(self.code)
        self.path = path
        self.lost = lost

    def correction(self) -> dict[str, str]:
        """What the correction request must keep: the page's own cited lines, never source text."""
        return {"path": self.path, "code": self.code,
                "message": "Keep these existing source-backed lines verbatim and in order, adding "
                           "the new source's lines around them:\n" + "\n".join(self.lost)}


class CorpusUnavailable(GateRefused):
    retryable = True


# A reasoning model can write a whole plan in its reasoning and return the summary alone; an empty
# plan gets the bounded correction, which may return the same empty plan when nothing is warranted.
EMPTY_PLAN = {"path": "plan", "code": "empty-plan",
              "message": "The plan changes no page. Return the pages this source warrants, or the same "
                         "empty plan when it adds nothing, with the summary saying why."}


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
    return _process_with_lock(conn, item, deps)


def _remaining_operation_budget_s(
    item: dict,
    *,
    now: dt.datetime | None = None,
) -> float:
    raw_deadline = item.get("budget_deadline_at")
    if not raw_deadline:
        raise WriterDeadline("capture budget has no persisted deadline")
    try:
        deadline = dt.datetime.fromisoformat(str(raw_deadline))
    except ValueError as error:
        raise WriterDeadline("capture budget has an invalid persisted deadline") from error
    if deadline.tzinfo is None:
        raise WriterDeadline("capture budget deadline has no timezone")
    current = now or dt.datetime.now(dt.UTC)
    return max(0.0, (deadline - current).total_seconds())


def _process_with_lock(conn, item: dict, deps: WriterDeps) -> WriteResult:
    with ops.try_advisory_lock(conn, WRITER_LOCK_KEY) as acquired:
        if not acquired:
            raise WriterBusy("another knowledge write is active")
        with hard_deadline(
            deps.settings.timeout_s,
            lambda: WriterDeadline("commit reconciliation exceeded its bounded budget"),
        ):
            base = gitcmd.base_ref(deps.repo, deps.settings.branch)
            recovered = _recover(conn, item, deps, base.sha)
        if recovered:
            return recovered
        if int(item["attempts"]) > queue.DEFAULT_MAX_ATTEMPTS:
            raise WriterDeadline("recovery-only lease found no landed commit")
        budget_s = _remaining_operation_budget_s(
            item,
        )
        with hard_deadline(
            budget_s,
            lambda: WriterDeadline("knowledge operation exhausted its capture budget"),
        ):
            if item["operation"] == schema.CAPTURE:
                return _capture(conn, item, deps, base)
            if item["operation"] == schema.DELETE:
                return _delete(conn, item, deps, base)
            if item["operation"] == schema.ENTITY:
                return _entity(conn, item, deps, base)
            if item["operation"] == schema.GARDEN:
                request = schema.parse_garden(item["request"])
                if request.mode == "recompile":
                    return _recompile(conn, item, deps, base, request=request)
                return _garden(
                    conn,
                    item,
                    deps,
                    base,
                    operation_id=str(request.operation_id),
                    actor=request.actor.subject,
                )
            raise KnowledgeWriteError("unknown writer operation")


def _merge_in_layer(root: str, paths: tuple[str, ...], *, at: dt.datetime, evidence) -> str:
    """Fold entity pages of one layer together; links elsewhere follow where they resolved to them."""
    located = [layers.split_path(path) for path in paths]
    owners = {layer for layer, _vpath in located}
    if len(owners) != 1:
        raise EntityOperationError("only entity pages of one layer can be merged")
    layer = owners.pop()
    base = str(layered.layer_root(root, layer))
    canonical = merge_entities(
        base,
        tuple(vpath for _layer, vpath in located),
        at=at,
        evidence=evidence,
        source_roots=(base, root) if layer != layers.PUBLIC else (root,),
    )
    if layer == layers.PUBLIC:
        moved = {Path(vpath).stem: Path(canonical).stem for _layer, vpath in located if vpath != canonical}
        for other in layered.layer_names(root)[1:]:
            own = {Path(vpath).stem.casefold() for vpath in layered.knowledge_pages(root, other)}
            rewrite_links(str(layered.layer_root(root, other)),
                          {old: new for old, new in moved.items() if old.casefold() not in own})
    return layers.physical_path(layer, canonical)


def _rename_everywhere(root: str, path: str, new_title: str, *, at: dt.datetime) -> str:
    """Rename every layer's page for one virtual entity path in one change."""
    vpath = layers.virtual_path(path)
    owners = [layer for layer in layered.layer_names(root)
              if _path(root, layers.physical_path(layer, vpath)).is_file()]
    if not owners:
        raise EntityOperationError("entity page does not exist")
    renamed = vpath
    for layer in owners:
        renamed = rename_entity(str(layered.layer_root(root, layer)), vpath, new_title, at=at, rewrite=False)
    if renamed != vpath:
        for layer in layered.layer_names(root):
            rewrite_links(str(layered.layer_root(root, layer)), {Path(vpath).stem: Path(renamed).stem})
    return renamed


def _garden(
    conn,
    item: dict,
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
        changed: tuple[str, ...] = ()
        model_requests = 0
        model_changed: dict[str, str] = {}
        if before:
            for layer in layered.layer_names(worktree):
                requests, repaired = _garden_layer(worktree, layer, deps)
                model_requests += requests
                model_changed.update(repaired)
        after = check(worktree)
        run.stats.update(
            detected=len(before),
            fixed=max(0, len(before) - len(after)),
            clean=not after,
            model_requests=model_requests,
        )
        if after:
            raise GateRefused(_violation_summary(after))
        alias_reasons = _garden_aliases(worktree, item, deps)
        run.stats["aliases_recorded"] = len(alias_reasons)
        if alias_reasons and (violations := check(worktree)):
            raise GateRefused(_violation_summary(violations))
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
            expected_attempts=item["attempts"],
            expected_lease_started_at=item["lease_started_at"],
            trigger="garden",
            actor=actor,
            summary=f"Repaired {len(set(changed) | set(model_changed) | set(alias_reasons))} corpus path(s)",
            reasons={
                **{path: "Deterministic corpus repair" for path in changed},
                **model_changed,
                **alias_reasons,
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


def _garden_layer(worktree: str, layer: str, deps: WriterDeps) -> tuple[int, dict[str, str]]:
    """Repair one layer's view and store the repairs in that layer."""
    with _open_view(worktree, layer, deps) as view:
        tolerated, found = _view_baseline(view.root, layer)
        if not found:
            return 0, {}
        repair_files, repair_context = _garden_repair_files(view.root, found)
        repair_run = deps.planner.repair(
            worktree=view.root,
            violations=found,
            files=repair_files,
            source_path="",
            source_text="",
            context=repair_context,
            max_requests=min(MAINTENANCE_REPAIR_MAX_REQUESTS, max(0, int(deps.settings.max_turns))),
        )
        changed = _apply_repair_plan(
            view.root,
            found,
            repair_run.plan,
            authorized_files=repair_files,
            context=WriteContext(actor_groups=None, content_acl=None, unrestricted=True),
            existing_paths=frozenset(repair_files),
        )
        requests = int(repair_run.model_requests)
        if _introduced(check(view.root), tolerated):
            # A repair that leaves the layer dirty stores nothing; the repository check reports it.
            return requests, {}
        try:
            layered.project(view, at=dt.datetime.now(dt.UTC).date(), strict=True, skipped=[])
        except layered.LayerProjectionError as error:
            raise GateRefused(str(error)) from error
        return requests, {layers.physical_path(layer, path): reason for path, reason in changed.items()}


def _garden_aliases(worktree: str, item: dict, deps: WriterDeps) -> dict[str, str]:
    """Record, layer by layer on a clean corpus, the other names its pages already link entities by;
    `{physical path: reason}` for the entity pages stored. Every link counts, so the candidates
    rotate by day: a variant the classifier refused cannot hold the cap from newer ones."""
    window = min(GARDEN_ALIAS_WINDOW_S, _remaining_operation_budget_s(item) - ALIAS_BUDGET_RESERVE_S)
    deadline = time.monotonic() + window
    context = WriteContext(actor_groups=None, content_acl=None, unrestricted=True)
    today = dt.datetime.now(dt.UTC).date()
    reasons: dict[str, str] = {}
    for layer in layered.layer_names(worktree):
        window_s = deadline - time.monotonic()
        if window_s <= 0:
            break
        with _open_view(worktree, layer, deps) as view:
            tolerated, found = _view_baseline(view.root, layer)
            if found:
                continue
            written = {f"{folder}/{path.name}": None for folder in KNOWLEDGE_FOLDERS
                       for path in sorted(Path(view.root, folder).glob("*.md"))}
            recorded: dict[str, str] = {}
            _record_aliases(view.root, view, recorded, tolerated, written, context=context, at=today,
                            window_s=window_s, max_candidates=GARDEN_ALIAS_CANDIDATES,
                            start=today.toordinal() * GARDEN_ALIAS_CANDIDATES)
            if not recorded:
                continue
            try:
                projected = layered.project(view, at=today, strict=True, skipped=[])
            except layered.LayerProjectionError as error:
                raise GateRefused(str(error)) from error
            reasons.update({path: "Recorded another name a page links this entity by" for path in projected})
    return reasons


def _garden_repair_files(root: str, violations: tuple[Violation, ...]) -> tuple[dict[str, str], str]:
    """Authorize maintenance repair targets and their already-declared provenance."""
    paths = {violation.path for violation in violations}
    if not paths or not all(path_role(path) for path in paths):
        raise GateRefused("garden repair may only target violated knowledge pages")
    files = {}
    allowed_sources_by_path = {}
    for path in sorted(paths):
        target = _path(root, path)
        if not target.is_file():
            raise GateRefused("garden repair targeted a missing path")
        try:
            page = parse_page(path, target.read_text(encoding="utf-8"))
        except PageContractError as error:
            raise GateRefused("garden repair target has no preservable page metadata") from error
        files[path] = page.body
        allowed_sources_by_path[path] = sorted(page.sources)
    context = json.dumps(
        {
            "maintenance": True,
            "allowed_sources_by_path": allowed_sources_by_path,
        },
        sort_keys=True,
    )
    return files, context


def _recompile(
    conn,
    item: dict,
    deps: WriterDeps,
    base: gitcmd.BaseRef,
    *,
    request: schema.GardenRequest,
) -> WriteResult:
    """Rebuild derived knowledge from immutable sources in one master-only commit."""
    with (
        ops.job_run(conn, "recompile", base_commit_sha=base.sha) as run,
        gitcmd.ephemeral_worktree(
            deps.repo,
            base.sha,
            root=deps.settings.worktree_root,
        ) as worktree,
    ):
        groups, unrestricted = actor_scope(conn, request.actor.subject)
        allow_explicit_master(WriteContext(groups, None, unrestricted))
        report = _recompile_layers(worktree, deps, rationale=request.rationale)
        entries = gitcmd.diff_entries(worktree)
        if not entries:
            run.head_commit_sha = base.sha
            report.update(commit_sha="", change_id=None)
            run.stats.update(report)
            return WriteResult(commit_sha="", change_id=None, report=report)
        _gate_diff(entries, trigger="garden")
        reasons = {entry.path: "Recompiled derived knowledge from immutable sources" for entry in entries}
        commit, change = _commit_and_record(
            conn,
            deps,
            worktree=worktree,
            base_sha=base.sha,
            entries=entries,
            item_id=str(request.operation_id),
            expected_attempts=item["attempts"],
            expected_lease_started_at=item["lease_started_at"],
            trigger="garden",
            actor=request.actor.subject,
            summary=(f"Recompiled derived knowledge from {report['source_count']} immutable source(s)"),
            reasons=reasons,
            job_run_id=str(run.id),
        )
        run.head_commit_sha = commit
        report.update(commit_sha=commit, change_id=str(change.id))
        run.stats.update(report)
        return WriteResult(commit_sha=commit, change_id=str(change.id), report=report)


_RECOMPILE_COUNTS = (
    "source_count",
    "page_mutations",
    "authorized_page_removals",
    "model_requests",
    "planning_model_requests",
    "semantic_revision_model_requests",
    "semantic_revision_count",
)


def _recompile_layers(worktree: str, deps: WriterDeps, *, rationale: str) -> dict:
    """Recompile `public`, then each layer on top of the recompiled public pages."""
    total = dict.fromkeys(_RECOMPILE_COUNTS, 0)
    violations = 0
    for layer in layered.layer_names(worktree):
        base = layered.layer_root(worktree, layer)
        sources = tuple(path.relative_to(base).as_posix() for path in sorted(Path(base, "sources").glob("*/*/*.md")))
        previous = {vpath: text for vpath, (_page, text) in layered.knowledge_pages(worktree, layer).items()}
        if layer != layers.PUBLIC:
            if not sources and not previous:
                continue
            for vpath in previous:
                _path(worktree, layers.physical_path(layer, vpath)).unlink()
        elif not sources:
            raise GateRefused("recompile requires at least one immutable source")
        with _open_view(worktree, layer, deps) as view:
            keep = frozenset() if layer == layers.PUBLIC else frozenset(view.before)
            report = _recompile_derived(
                view.root, deps, rationale=rationale, source_paths=sources, previous_pages=previous, keep=keep)
            layered.project(view, at=dt.datetime.now(dt.UTC).date(), strict=False, skipped=[])
        for key in _RECOMPILE_COUNTS:
            total[key] += int(report[key])
        violations += int(report["link_health"]["violations"])
    entries = gitcmd.diff_entries(worktree)
    return {
        "operation": "recompile",
        "rationale": rationale,
        **total,
        "page_mutations": len(entries),
        "link_health": {"violations": violations, "clean": True},
        "idempotent": not entries,
    }


def _recompile_derived(
    worktree: str,
    deps: WriterDeps,
    *,
    rationale: str,
    source_paths: tuple[str, ...],
    previous_pages: dict[str, str],
    keep: frozenset[str] = frozenset(),
) -> dict:
    """Compile a view's sources into fresh derived pages without committing.

    `keep` names pages of more open layers the view shows and this compilation must not clear.
    The caller owns an ephemeral worktree, so any failure abandons every derived mutation.
    """
    previous_page_records = {path: parse_page(path, text) for path, text in previous_pages.items()}
    if not source_paths:
        raise GateRefused("recompile requires at least one immutable source")
    try:
        recompile_source_preflight(worktree, source_paths)
    except SourceContractError as error:
        raise GateRefused(f"recompile source preflight failed: {error}") from error
    _clear_derived_pages(worktree, keep=keep)
    model_requests = 0
    planning_model_requests = 0
    semantic_revision_model_requests = 0
    semantic_revision_count = 0
    gate_violations = 0
    tombstone_authorizations: dict[str, set[str]] = {}
    for relative_source in source_paths:
        try:
            source = read_recompile_source(worktree, relative_source)
        except (OSError, UnicodeError, SourceContractError) as error:
            raise GateRefused(f"recompile source changed after preflight: {relative_source}") from error
        relative_source = source.path
        envelope = source_envelope(source)
        write_context = WriteContext(None, envelope.audience, unrestricted=True)
        prior_pages = _recompile_prior_pages(
            previous_page_records,
            source=relative_source,
            context=write_context,
        )
        safe_context = filing_context(
            worktree,
            source_text=source.text,
            capture_acl=envelope.audience,
            actor_groups=None,
        )
        safe_context["recompile"] = {
            "prior_pages": [_recompile_prior_page_context(prior_pages[path]) for path in sorted(prior_pages)],
        }
        try:
            rendered_context = render_context(safe_context)
        except ValueError as error:
            raise GateRefused("recompile prior-page context exceeds its safe limit") from error
        plan_run = deps.planner.plan(
            worktree=worktree,
            envelope=envelope,
            source_path=relative_source,
            source_text=source.text,
            context=rendered_context,
        )
        planning_requests = int(plan_run.model_requests)
        planning_model_requests += planning_requests
        model_requests += planning_requests
        plan = plan_run.plan
        if not isinstance(plan, FilingPlan):
            raise GateRefused(f"recompile rejected source {relative_source}")
        snapshot = _snapshot_mutable(worktree)
        corrected = False
        while True:
            violations: tuple[Violation, ...] = ()
            failure_payload: tuple[dict[str, str], ...] = ()
            try:
                effective_plan, tombstones = _prepare_recompile_plan(
                    plan,
                    prior_pages=prior_pages,
                )
                _apply_filing_plan(
                    worktree,
                    effective_plan,
                    context=write_context,
                    envelope=envelope,
                    relative_source=relative_source,
                    reasons={},
                    allowed_contradiction_sources=frozenset(
                        {
                            relative_source,
                            *(item["path"] for item in safe_context["source_evidence"]),
                        }
                    ),
                )
                violations = tuple(
                    item for item in check(worktree, editorial_paths=frozenset(_derived_paths(worktree) - keep))
                    if not (keep and item.code in layered.CROSS_LAYER_CODES)
                )
                if not violations:
                    for path in tombstones:
                        tombstone_authorizations.setdefault(path, set()).add(relative_source)
                    break
                failure_payload = _filing_failure_payload(
                    plan,
                    violations,
                    source_path=relative_source,
                )
            except (
                KnowledgeWriteError,
                PageContractError,
                WriteRefused,
                contradictions.ContradictionContractError,
            ) as error:
                failure_payload = (
                    {
                        "path": "plan",
                        "code": error.__class__.__name__,
                        "message": str(error),
                    },
                )
            remaining_requests = max(0, int(deps.settings.max_turns) - planning_requests)
            if corrected or remaining_requests < 1:
                gate_violations += len(violations)
                raise GateRefused(f"recompile rejected source {relative_source}")
            _restore_mutable(worktree, snapshot)
            try:
                revision_run = deps.planner.revise(
                    worktree=worktree,
                    envelope=envelope,
                    source_path=relative_source,
                    source_text=source.text,
                    context=rendered_context,
                    draft=plan,
                    violations=failure_payload,
                    max_requests=remaining_requests,
                )
            except Exception as error:
                raise GateRefused(f"recompile rejected source {relative_source}") from error
            revision_requests = int(revision_run.model_requests)
            if revision_requests > remaining_requests or not isinstance(revision_run.plan, FilingPlan):
                raise GateRefused(f"recompile rejected source {relative_source}")
            model_requests += revision_requests
            semantic_revision_model_requests += revision_requests
            semantic_revision_count += int(revision_requests > 0)
            plan = revision_run.plan
            corrected = True
    _restore_recompiled_page_identity(
        worktree, {path: text for path, text in previous_pages.items() if path not in keep})
    authorized_page_removals = _gate_recompile_disappearance(
        previous_page_records,
        final_paths=_derived_paths(worktree),
        tombstone_authorizations=tombstone_authorizations,
    )
    final_violations = tuple(
        item for item in check(worktree) if not (keep and item.code in layered.CROSS_LAYER_CODES))
    if final_violations:
        gate_violations += len(final_violations)
        raise GateRefused(_violation_summary(final_violations))
    return {
        "operation": "recompile",
        "rationale": rationale,
        "source_count": len(source_paths),
        "page_mutations": 0,
        "authorized_page_removals": authorized_page_removals,
        "model_requests": model_requests,
        "planning_model_requests": planning_model_requests,
        "semantic_revision_model_requests": semantic_revision_model_requests,
        "semantic_revision_count": semantic_revision_count,
        "link_health": {"violations": gate_violations, "clean": True},
    }


def _clear_derived_pages(root: str, *, keep: frozenset[str] = frozenset()) -> None:
    for folder in KNOWLEDGE_FOLDERS:
        path = Path(root, folder)
        if not path.is_dir():
            continue
        for page in path.glob("*.md"):
            if page.relative_to(root).as_posix() not in keep:
                page.unlink()


def _derived_paths(root: str) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for folder in KNOWLEDGE_FOLDERS
        for path in Path(root, folder).glob("*.md")
    }


def _recompile_prior_pages(previous_pages: dict, *, source: str, context: WriteContext) -> dict:
    """Return every prior page backed by this source, or fail before exposing unsafe content."""
    result = {}
    for path, page in previous_pages.items():
        if source not in page.sources:
            continue
        try:
            allow_existing(context, page.acl)
        except WriteRefused as error:
            raise GateRefused("recompile prior page is outside its backing source audience") from error
        result[path] = page
    return result


def _recompile_prior_page_context(page) -> dict:
    return {
        "path": page.path,
        "title": page.title,
        "type": page.role,
        "status": page.status,
        "entity_type": page.entity_type,
        "aliases": list(page.aliases),
        "sources": list(page.sources),
        "body": page.body,
    }


def _prepare_recompile_plan(plan: FilingPlan, *, prior_pages: dict) -> tuple[FilingPlan, frozenset[str]]:
    """Require an explicit model disposition for every prior source-backed page."""
    delete_paths = [mutation.path or "" for mutation in plan.mutations if mutation.action == "delete"]
    tombstones = frozenset(delete_paths)
    if len(tombstones) != len(delete_paths):
        raise GateRefused("recompile plan repeats a prior-page tombstone")
    unknown = tombstones - set(prior_pages)
    if unknown:
        raise GateRefused("recompile plan tombstones a page not backed by this source")
    retained = set()
    try:
        for mutation in plan.mutations:
            if mutation.action == "create":
                retained.add(page_path(mutation.role or "", mutation.title or ""))
            elif mutation.action == "update":
                retained.add(mutation.path or "")
    except PageContractError as error:
        raise GateRefused("recompile plan has an invalid page target") from error
    if tombstones & retained:
        raise GateRefused("recompile plan cannot tombstone and recreate the same page")
    for path in tombstones:
        try:
            if contradictions.parse_all(prior_pages[path].body):
                raise GateRefused("recompile cannot tombstone a page with unresolved contradictions")
        except contradictions.ContradictionContractError as error:
            raise GateRefused("recompile prior-page contradiction data is invalid") from error
    if set(prior_pages) - retained - tombstones:
        raise GateRefused("recompile plan omitted prior derived knowledge without an explicit tombstone")
    if not tombstones:
        return plan, tombstones
    return (
        plan.model_copy(
            update={"mutations": tuple(mutation for mutation in plan.mutations if mutation.action != "delete")}
        ),
        tombstones,
    )


def _gate_recompile_disappearance(
    previous_pages: dict,
    *,
    final_paths: set[str],
    tombstone_authorizations: dict[str, set[str]],
) -> int:
    """A vanished page requires an explicit tombstone from every backing source."""
    removed = 0
    for path, page in previous_pages.items():
        if path in final_paths:
            continue
        backing_sources = set(page.sources)
        if not backing_sources or tombstone_authorizations.get(path, set()) != backing_sources:
            raise GateRefused("recompile cannot remove a page without every backing source's authorization")
        removed += 1
    return removed


def _restore_recompiled_page_identity(root: str, previous_pages: dict[str, str]) -> None:
    """Keep durable page IDs/dates stable when the rebuilt page has the same logical path, and an
    entity's prior aliases that no other entity page now claims."""
    claimed = name_index(entity_pages(root))
    for path in _derived_paths(root) & set(previous_pages):
        before = parse_page(path, previous_pages[path])
        after = parse_page(path, _path(root, path).read_text(encoding="utf-8"))
        if before.role != after.role or before.title != after.title:
            continue
        kept = list(after.aliases)
        keys = set(after.name_keys())
        for alias in before.aliases if after.role == "entity" else ():
            key = resolution_key(alias)
            if key and key not in keys and set(claimed.get(key, ())) <= {path}:
                kept.append(alias)
                keys.add(key)
        _path(root, path).write_text(
            render_like(after, page_id=before.page_id, created=before.created, aliases=tuple(kept)),
            encoding="utf-8",
        )


def _capture(conn, item: dict, deps: WriterDeps, base: gitcmd.BaseRef) -> WriteResult:
    capture_started = time.perf_counter()
    latency_ms: dict[str, float] = {}
    model_telemetry: dict[str, dict] = {}
    context_measure: dict = {}
    alias_report: list[dict] = []
    envelope = schema.parse_capture(item["request"])
    retry = int(item["attempts"]) > 1
    layer = layers.layer_of_acl(envelope.audience)
    # Membership is checked again at filing: the author may have left the layer since queueing.
    author = roster.principal(conn, envelope.actor.subject)
    if layer not in author.publishable_layers:
        raise GateRefused("the capture's author can no longer file in its layer")
    phase_started = time.perf_counter()
    extracted = extract_capture(
        deps.evidence,
        envelope,
        ocr_model=deps.settings.ocr_model,
    )
    queue.record_extraction(conn, item["id"], {"artifacts": [a.metadata() for a in extracted]})
    latency_ms["extraction"] = round((time.perf_counter() - phase_started) * 1000, 2)
    phase_started = time.perf_counter()
    relative_source = source_path(envelope)
    source_text = render_source(envelope, extracted)
    physical_source = layers.physical_path(layer, relative_source)
    # The librarian files every capture as if it were organization-wide; the layer is applied
    # when its result is stored.
    open_envelope = envelope if envelope.audience is None else envelope.model_copy(update={"audience": None})
    open_source_text = source_text if envelope.audience is None else render_source(open_envelope, extracted)
    trigger = "contradiction_resolution" if envelope.intent.resolution_of else "capture"
    latency_ms["source_render"] = round((time.perf_counter() - phase_started) * 1000, 2)

    phase_started = time.perf_counter()
    with gitcmd.ephemeral_worktree(
        deps.repo,
        base.sha,
        root=deps.settings.worktree_root,
    ) as worktree, contextlib.ExitStack() as scratch:
        latency_ms["worktree_setup"] = round((time.perf_counter() - phase_started) * 1000, 2)
        phase_started = time.perf_counter()
        corpus_baseline = set(check(worktree))
        view = scratch.enter_context(_open_view(worktree, layer, deps))
        tree = view.root
        _write_new(worktree, physical_source, source_text)
        _write_new(tree, relative_source, open_source_text)
        context = WriteContext(actor_groups=None, content_acl=None, unrestricted=author.unrestricted)
        safe_context = filing_context(
            tree,
            source_text=open_source_text,
            capture_acl=None,
            actor_groups=None,
            measure=context_measure,
        )
        allowed_contradiction_sources = frozenset(
            {
                relative_source,
                *(item["path"] for item in safe_context["source_evidence"]),
            }
        )
        rendered_context = render_context(safe_context)
        context_measure["bytes"] = len(rendered_context.encode("utf-8"))
        latency_ms["context"] = round((time.perf_counter() - phase_started) * 1000, 2)
        phase_started = time.perf_counter()
        plan_run = deps.planner.plan(
            worktree=tree,
            envelope=open_envelope,
            source_path=relative_source,
            source_text=open_source_text,
            context=rendered_context,
            retry=retry,
        )
        latency_ms["planning"] = round((time.perf_counter() - phase_started) * 1000, 2)
        model_telemetry["planning"] = plan_run.telemetry
        phase_started = time.perf_counter()
        reasons = {relative_source: "Archived immutable readable evidence"}
        tolerated, blocking = _view_baseline(tree, layer)
        if blocking:
            raise CorpusUnavailable(_violation_summary(blocking))
        snapshot = _snapshot_mutable(tree)
        plan_invalid = False
        plan_rejection = ""
        contract_failures: tuple[dict[str, str], ...] = ()
        plan_skipped: list[str] = []
        planning_model_requests = int(plan_run.model_requests)
        semantic_revision_model_requests = 0
        repair_model_requests = 0
        semantic_revision_required = False
        semantic_revision_attempted = False
        semantic_revision_applied = False
        plan = plan_run.plan
        model_requests = planning_model_requests
        if not isinstance(plan, FilingPlan):
            plan_invalid = True
            plan_rejection = "filing planner returned the wrong plan type"
        if not plan_invalid:
            try:
                _apply_filing_plan(
                    tree,
                    plan,
                    context=context,
                    envelope=open_envelope,
                    relative_source=relative_source,
                    reasons=reasons,
                    allowed_contradiction_sources=allowed_contradiction_sources,
                    skipped=plan_skipped,
                    drop_broader=not envelope.intent.resolution_of,
                )
            except (
                KnowledgeWriteError,
                PageContractError,
                WriteRefused,
                contradictions.ContradictionContractError,
            ) as error:
                plan_invalid = True
                plan_rejection = str(error)
                contract_failures = (
                    error.correction() if isinstance(error, ExistingSourceBlockNotPreserved) else {
                        "path": "plan",
                        "code": error.__class__.__name__,
                        "message": str(error),
                    },
                )
        if not plan_invalid and not plan.mutations and not plan.contradictions:
            plan_invalid = True
            plan_rejection = EMPTY_PLAN["code"]
            contract_failures = (EMPTY_PLAN,)
        editorial_paths = frozenset(path for path in reasons if path_role(path))
        violations = _introduced(check(tree, editorial_paths=editorial_paths), tolerated) if not plan_invalid else ()
        if plan_invalid or violations:
            semantic_revision_required = True
            remaining_requests = max(0, int(deps.settings.max_turns) - model_requests)
            if remaining_requests < 1:
                plan_invalid = True
                plan_rejection = "filing correction budget exhausted"
            else:
                failure_payload = (
                    *contract_failures,
                    *_filing_failure_payload(
                        plan,
                        violations,
                        source_path=relative_source,
                    ),
                )
                _restore_mutable(tree, snapshot)
                reasons = {relative_source: "Archived immutable readable evidence"}
                plan_skipped = []
                plan_invalid = False
                semantic_revision_attempted = True
                try:
                    revision_run = deps.planner.revise(
                        worktree=tree,
                        envelope=open_envelope,
                        source_path=relative_source,
                        source_text=open_source_text,
                        context=rendered_context,
                        draft=plan,
                        violations=failure_payload,
                        max_requests=remaining_requests,
                        retry=retry,
                    )
                    semantic_revision_model_requests = int(revision_run.model_requests)
                    model_requests += semantic_revision_model_requests
                    model_telemetry["semantic_revision"] = revision_run.telemetry
                    if not isinstance(revision_run.plan, FilingPlan):
                        raise GateRefused("filing correction returned the wrong plan type")
                    plan = revision_run.plan
                    _apply_filing_plan(
                        tree,
                        plan,
                        context=context,
                        envelope=open_envelope,
                        relative_source=relative_source,
                        reasons=reasons,
                        allowed_contradiction_sources=allowed_contradiction_sources,
                        skipped=plan_skipped,
                        drop_broader=not envelope.intent.resolution_of,
                    )
                    editorial_paths = frozenset(path for path in reasons if path_role(path))
                    violations = _introduced(check(tree, editorial_paths=editorial_paths), tolerated)
                except ExistingSourceBlockNotPreserved as lossy:
                    # The rest of the corrected plan is sound: file it without each update that
                    # would still drop cited lines, rather than archive the source with nothing.
                    dropped: list[str] = []
                    while lossy is not None:
                        dropped.append(lossy.path)
                        plan = plan.model_copy(update={"mutations": tuple(
                            m for m in plan.mutations if not (m.action == "update" and m.path in dropped))})
                        _restore_mutable(tree, snapshot)
                        reasons = {relative_source: "Archived immutable readable evidence"}
                        plan_skipped = [f"update of {path} was not applied: it would drop cited lines"
                                        for path in dropped]
                        lossy = None
                        try:
                            _apply_filing_plan(
                                tree,
                                plan,
                                context=context,
                                envelope=open_envelope,
                                relative_source=relative_source,
                                reasons=reasons,
                                allowed_contradiction_sources=allowed_contradiction_sources,
                                skipped=plan_skipped,
                                drop_broader=not envelope.intent.resolution_of,
                            )
                        except ExistingSourceBlockNotPreserved as again:
                            lossy = again
                        except (
                            KnowledgeWriteError,
                            PageContractError,
                            WriteRefused,
                            contradictions.ContradictionContractError,
                        ):
                            plan_invalid = True
                            plan_rejection = "filing correction failed"
                    if not plan_invalid:
                        editorial_paths = frozenset(path for path in reasons if path_role(path))
                        violations = _introduced(check(tree, editorial_paths=editorial_paths), tolerated)
                        if violations or not plan.mutations:
                            plan_invalid = True
                            plan_rejection = ExistingSourceBlockNotPreserved.code
                except (
                    KnowledgeWriteError,
                    PageContractError,
                    WriteRefused,
                    contradictions.ContradictionContractError,
                ):
                    plan_invalid = True
                    plan_rejection = "filing correction failed"
                else:
                    semantic_revision_applied = not violations
                    if violations:
                        plan_invalid = True
                        codes = ", ".join(sorted({item.code for item in violations}))
                        plan_rejection = f"knowledge gates found {codes}"
        if plan_invalid:
            # Gate messages are fixed sentences, never source or model text.
            log.warning("filing plan rejected for %s: %s", envelope.capture_id, plan_rejection)
            _restore_mutable(tree, snapshot)
            reasons = {relative_source: "Archived immutable readable evidence"}
            fallback = _introduced(check(tree), tolerated)
            if fallback:
                raise GateRefused(_violation_summary(fallback))
        else:
            window_s = min(aliases.WINDOW_S, _remaining_operation_budget_s(item) - ALIAS_BUDGET_RESERVE_S)
            written = {path: snapshot[path].decode("utf-8") if path in snapshot else None
                       for path in sorted(reasons) if path_role(path)}
            alias_report = (_record_aliases(tree, view, reasons, tolerated, written, context=context,
                                            at=envelope.origin.captured_at.date(), window_s=window_s)
                            if window_s > 0 else [{"decision": "skipped", "reason": "budget"}])
        latency_ms["plan_apply_and_gates"] = round(
            (time.perf_counter() - phase_started) * 1000,
            2,
        )
        phase_started = time.perf_counter()
        try:
            projected = layered.project(
                view,
                at=envelope.origin.captured_at.date(),
                strict=bool(envelope.intent.resolution_of),
                skipped=plan_skipped,
            )
        except layered.LayerProjectionError as error:
            raise GateRefused(str(error)) from error
        swept = _sweep_other_layers(worktree, layer, projected)
        reasons = {layers.physical_path(layer, path): reason for path, reason in reasons.items()}
        reasons.update({path: "Removed references to deleted knowledge" for path in swept})
        introduced = _introduced(check(worktree), corpus_baseline)
        if introduced:
            raise GateRefused(_violation_summary(introduced))
        entries = gitcmd.diff_entries(worktree)
        _gate_diff(entries, trigger=trigger, expected_source=physical_source, layer=layer, swept=swept)
        # The submitter's report counts its own layer only; sweeps elsewhere are not theirs to see.
        wiki_changes = sum(1 for entry in entries if entry.path not in swept
                           and layers.virtual_path(entry.path).startswith("wiki/"))
        summary = (
            "Archived source without wiki changes: the filing plan was rejected"
            if plan_invalid
            else "Archived source without wiki changes"
            if not wiki_changes
            else (f"Applied {wiki_changes} wiki change(s); skipped {len(plan_skipped)} plan operation(s)")
        )
        commit, change = _commit_and_record(
            conn,
            deps,
            worktree=worktree,
            base_sha=base.sha,
            entries=entries,
            item_id=str(envelope.capture_id),
            expected_attempts=item["attempts"],
            expected_lease_started_at=item["lease_started_at"],
            trigger=trigger,
            actor=envelope.actor.subject,
            summary=summary,
            reasons=reasons,
            capture_id=str(envelope.capture_id),
        )
        latency_ms["commit_publish"] = round((time.perf_counter() - phase_started) * 1000, 2)
        phase_started = time.perf_counter()
        references.record_capture(conn, envelope, extracted, physical_source)
        latency_ms["reference_record"] = round((time.perf_counter() - phase_started) * 1000, 2)
        cleanup_started = time.perf_counter()
    latency_ms["worktree_cleanup"] = round((time.perf_counter() - cleanup_started) * 1000, 2)
    latency_ms["total"] = round((time.perf_counter() - capture_started) * 1000, 2)
    log.info(
        "capture latency capture=%s latency_ms=%s model_telemetry=%s filing_context=%s",
        envelope.capture_id,
        json.dumps(latency_ms, sort_keys=True),
        json.dumps(model_telemetry, sort_keys=True),
        json.dumps(context_measure, sort_keys=True),
    )
    return WriteResult(
        commit_sha=commit,
        change_id=str(change.id),
        source_path=physical_source,
        extraction={
            "artifacts": [artifact.metadata() for artifact in extracted],
        },
        report={
            "summary": summary,
            "wiki_changes": wiki_changes,
            "model_requests": model_requests,
            "planning_model_requests": planning_model_requests,
            "semantic_revision_required": semantic_revision_required,
            "semantic_revision_attempted": semantic_revision_attempted,
            "semantic_revision_applied": semantic_revision_applied,
            "semantic_revision_model_requests": semantic_revision_model_requests,
            "repair_model_requests": repair_model_requests,
            "latency_ms": latency_ms,
            "model_telemetry": model_telemetry,
            "filing_context": context_measure,
            "aliases": alias_report,
            "plan_rejected": plan_invalid,
            "plan_rejection": plan_rejection,
            # Empty when the plan was rejected outright: nothing of it was kept to drop from.
            "plan_skipped": [] if plan_invalid else plan_skipped,
        },
    )


def _record_aliases(tree: str, view, reasons: dict[str, str], tolerated, written: dict[str, str | None], *,
                    context: WriteContext, at, window_s: float,
                    max_candidates: int = aliases.MAX_CANDIDATES, start: int = 0) -> list[dict]:
    """Write the aliases the classifier accepts for the `[[Entity|variant]]` links of `written`
    (`{path: its text before, or None when every link counts}`), and undo them all if they would
    introduce a gate violation. A deadline or lease abort propagates."""
    try:
        report, before = aliases.record(
            tree, written, link_names=_knowledge_link_stems(tree, (), context),
            eligible=lambda path: layered.stores_whole(view, path), at=at, window_s=window_s,
            path_of=lambda relative: _path(tree, relative), max_candidates=max_candidates, start=start)
    except (OSError, UnicodeError, PageContractError) as error:
        log.warning("alias step wrote nothing (%s)", error.__class__.__name__)
        return [{"decision": "skipped", "reason": error.__class__.__name__}]
    if before and _introduced(check(tree), tolerated):
        for path, text in before.items():
            _path(tree, path).write_text(text, encoding="utf-8")
        return [{**entry, "decision": "reverted"} if entry["decision"] == "accepted" else entry
                for entry in report]
    for path in before:
        reasons.setdefault(path, "Recorded another name a page links this entity by")
    return report


def _filing_failure_payload(
    plan: FilingPlan,
    violations: tuple[Violation, ...],
    *,
    source_path: str,
) -> tuple[dict[str, str], ...]:
    """Expose mechanical gate failures to the correction request."""
    return tuple({"path": item.path, "code": item.code, "message": item.message} for item in violations)


def _delete(conn, item: dict, deps: WriterDeps, base: gitcmd.BaseRef) -> WriteResult:
    request = schema.parse_delete(item["request"])
    with gitcmd.ephemeral_worktree(
        deps.repo,
        base.sha,
        root=deps.settings.worktree_root,
    ) as worktree:
        groups, unrestricted = actor_scope(conn, request.actor.subject)
        context = WriteContext(groups, None, unrestricted)
        allow_explicit_master(context)
        paths = _deletion_targets(worktree, request.paths)
        baseline = set(check(worktree))
        reasons = {path: request.rationale for path in paths}
        for relative in paths:
            _path(worktree, relative).unlink()
        reasons.update(_sweep_deleted_references(worktree, set(paths)))
        violations = _introduced(check(worktree), baseline)
        if violations:
            raise GateRefused(_violation_summary(violations))
        entries = gitcmd.diff_entries(worktree)
        _gate_diff(entries, trigger="delete", deleted_sources=set(paths))
        commit, change = _commit_and_record(
            conn,
            deps,
            worktree=worktree,
            base_sha=base.sha,
            entries=entries,
            item_id=str(request.operation_id),
            expected_attempts=item["attempts"],
            expected_lease_started_at=item["lease_started_at"],
            trigger="delete",
            actor=request.actor.subject,
            summary=f"Deleted {len(paths)} knowledge path(s)",
            reasons=reasons,
        )
        _release_deleted_evidence(conn, deps.evidence, set(paths))
    return WriteResult(
        commit_sha=commit,
        change_id=str(change.id),
        report={"summary": f"Deleted {len(paths)} knowledge path(s)"},
    )


def _deletion_targets(root: str, requested: tuple[str, ...]) -> list[str]:
    """A path names one page, as an entity operation's does: `layers/<layer>/…` that layer's, any
    other path the public page."""
    if not all(_path(root, path).is_file() for path in requested):
        raise KnowledgeWriteError("one or more requested paths do not exist")
    return list(dict.fromkeys(requested))


def _entity(conn, item: dict, deps: WriterDeps, base: gitcmd.BaseRef) -> WriteResult:
    request = schema.parse_entity_operation(item["request"])
    with gitcmd.ephemeral_worktree(
        deps.repo,
        base.sha,
        root=deps.settings.worktree_root,
    ) as worktree:
        groups, unrestricted = actor_scope(conn, request.actor.subject)
        allow_explicit_master(WriteContext(groups, None, unrestricted))
        at = dt.datetime.now(dt.UTC)
        reasons: dict[str, str] = {}
        baseline = set(check(worktree))
        try:
            if request.action == "merge":
                if request.evidence is None:
                    raise GateRefused("merge evidence is missing")
                canonical = _merge_in_layer(worktree, request.entities, at=at, evidence=request.evidence)
                summary = f"Merged {len(request.entities)} entity pages into {canonical}"
                reason = f"Verified by {request.evidence.label()}. {request.rationale}"
            elif request.action == "rename":
                renamed = _rename_everywhere(worktree, request.entities[0], request.new_title or "", at=at)
                summary = f"Renamed {request.entities[0]} to {renamed}"
                reason = request.rationale
            else:
                target_path = request.entities[0]
                target = _path(worktree, target_path)
                if not target.is_file():
                    raise GateRefused("entity page does not exist")
                target.unlink()
                reasons.update(_sweep_deleted_references(worktree, {target_path}))
                summary = f"Deleted entity page {target_path}"
                reason = request.rationale
        except EntityOperationError as error:
            raise GateRefused(str(error)) from error
        violations = _introduced(check(worktree), baseline)
        if violations:
            raise GateRefused(_violation_summary(violations))
        entries = gitcmd.diff_entries(worktree)
        _gate_diff(entries, trigger="entity")
        for entry in entries:
            reasons.setdefault(entry.path, reason)
        commit, change = _commit_and_record(
            conn,
            deps,
            worktree=worktree,
            base_sha=base.sha,
            entries=entries,
            item_id=str(request.operation_id),
            expected_attempts=item["attempts"],
            expected_lease_started_at=item["lease_started_at"],
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
    reasons: dict[str, str],
    allowed_contradiction_sources: frozenset[str],
    skipped: list[str] | None = None,
    drop_broader: bool = False,
) -> None:
    """Apply one derived graph plan, or reject it as a whole.

    The immutable source is written before this function. Every page mutation, link, contradiction,
    and resolution is therefore all-or-nothing with respect to that source, except for the
    degradations recorded in `skipped`: a wikilink that resolves to no linkable page becomes plain
    text, a page whose name another page already answers to is not created, and, with `drop_broader`,
    an update, delete, or contradiction aimed at a page the actor can read but whose audience is
    broader than the capture's is not applied. Without `drop_broader` such an operation rejects the
    plan, which resolutions and recompiles rely on.
    """
    if envelope.intent.resolution_of:
        allow_explicit_master(context)
    for proposal in plan.contradictions:
        proposed_sources = {claim.source for claim in proposal.claims}
        if not proposed_sources <= allowed_contradiction_sources:
            raise KnowledgeWriteError("contradiction cites evidence outside the supplied filing context")
    skipped = skipped if skipped is not None else []
    contested: set[str] = set()
    mutations = _skip_claimed_names(root, plan.mutations, skipped=skipped, contested=contested)
    updated_pages: set[str] = set()
    link_stems = _knowledge_link_stems(root, mutations, context)
    link_stems.update(dict.fromkeys(contested))
    for index, mutation in enumerate(mutations):
        if (drop_broader and mutation.action in ("update", "delete")
                and _broader_page(root, mutation.path, context)):
            verb = "update to" if mutation.action == "update" else "delete of"
            skipped.append(f"{verb} {mutation.path} was not applied: {layered.NARROWER}")
            continue
        if mutation.body:
            body, unlinked = _resolve_links(_canonical_links(mutation.body, link_stems), link_stems)
            target = mutation.path or page_path(mutation.role or "", mutation.title or "")
            skipped.extend(f"unlinked unresolved wikilink {name!r} in {target}" for name in unlinked)
            mutation = mutation.model_copy(update={"body": body})
        try:
            changed_path = _apply_page_mutation(
                root,
                mutation,
                context=context,
                source=relative_source,
                at=envelope.origin.captured_at.date(),
                reasons=reasons,
            )
        except ExistingSourceBlockNotPreserved:
            raise
        except (KnowledgeWriteError, PageContractError, WriteRefused) as error:
            raise KnowledgeWriteError(f"planned mutation {index} could not be applied") from error
        if mutation.action == "update" and changed_path:
            updated_pages.add(changed_path)
    for index, proposal in enumerate(plan.contradictions):
        if drop_broader and _broader_page(root, proposal.page_path, context):
            skipped.append(f"contradiction on {proposal.page_path} was not recorded: {layered.NARROWER}")
            continue
        target = _path(root, proposal.page_path)
        unplaceable = f"planned contradiction {index} targets no page this capture may affect"
        if not target.is_file():
            raise KnowledgeWriteError(unplaceable)
        original = target.read_text(encoding="utf-8")
        page = parse_page(proposal.page_path, original)
        try:
            allow_existing(context, page.acl)
        except WriteRefused as error:
            raise KnowledgeWriteError(unplaceable) from error
        before = set(check(root))
        try:
            record = contradictions.from_proposal(proposal)
            candidate = render_like(
                page,
                body=contradictions.append(page.body, record),
                sources=tuple(dict.fromkeys((*page.sources, *(claim.source for claim in record.claims)))),
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




def _broader_page(root: str, relative: str | None, context: WriteContext) -> bool:
    """Whether `relative` names an existing page this write may read but must not change."""
    if not relative:
        return False
    try:
        target = _path(root, relative)
    except KnowledgeWriteError:
        return False
    if not target.is_file():
        return False
    try:
        page = parse_page(relative, target.read_text(encoding="utf-8"))
    except PageContractError:
        return False
    return is_broader(context, page.acl)


def _knowledge_link_stems(
    root: str, mutations: tuple[PageMutation, ...], context: WriteContext
) -> dict[str, str | None]:
    """Casefolded names of pages this write may address, mapped to their stem.

    A page answers to its stem and, for an entity, to its aliases; `None` marks an ambiguous name.
    """
    names: list[tuple[str, str]] = []
    for folder in KNOWLEDGE_FOLDERS:
        for path in Path(root, folder).glob("*.md"):
            page = parse_page(path.relative_to(root).as_posix(), path.read_text(encoding="utf-8"))
            if may_link(context, page.acl):
                names.append((path.stem, path.stem))
                names.extend((title_stem(alias), path.stem) for alias in page.aliases)
    for mutation in mutations:
        if mutation.action == "create":
            try:
                stem = Path(page_path(mutation.role or "", mutation.title or "")).stem
            except PageContractError:
                continue
            names.append((stem, stem))
            names.extend((title_stem(alias), stem) for alias in mutation.aliases or ())
    resolved: dict[str, str | None] = {}
    for name, stem in names:
        key = name.casefold()
        if key:
            resolved[key] = stem if resolved.get(key, stem) == stem else None
    return resolved


def _canonical_links(body: str, stems: dict[str, str | None]) -> str:
    """Point a title-form wikilink at the filename its title was sanitized into."""

    def replace(match):
        target, separator, label = match.group(1).partition("|")
        name, anchor_mark, anchor = target.partition("#")
        cleaned = name.strip().removesuffix(".md")
        canonical = stems.get(cleaned.casefold()) or stems.get(title_stem(cleaned).casefold())
        if not canonical or canonical == cleaned:
            return match.group(0)
        embed = "!" if match.group(0).startswith("!") else ""
        shown = label if separator else cleaned
        return f"{embed}[[{canonical}{anchor_mark}{anchor}|{shown}]]"

    return _WIKILINK_RE.sub(replace, body)


def _resolve_links(body: str, stems: dict[str, str | None]) -> tuple[str, list[str]]:
    """Keep wikilinks that resolve to exactly one linkable page; the rest become their shown text."""
    unresolved: list[str] = []

    def replace(match):
        target, separator, label = match.group(1).partition("|")
        cleaned = target.split("#", 1)[0].strip().removesuffix(".md")
        if stems.get(cleaned.casefold()) == cleaned:
            return match.group(0)
        unresolved.append(cleaned)
        return label.strip() if separator else cleaned

    return _WIKILINK_RE.sub(replace, body), unresolved


def _skip_claimed_names(
    root: str,
    mutations: tuple[PageMutation, ...],
    *,
    skipped: list[str],
    contested: set[str],
) -> tuple[PageMutation, ...]:
    """Drop creations whose name another page already answers to, in the corpus or in this plan.

    A wikilink resolves by name across every folder, and an entity also answers to its aliases.
    Entity names are the identities' names: a creation named like an entity is not made, and links
    of that name keep reaching the entity. A note, concept, or project named like another such page
    in a different folder is a different subject, so its name joins `contested` and links to it read
    as plain text. A new entity's alias that another kind of page is named is left off. A second
    page at the same path is left to the write itself, which refuses it.
    """
    entity_names = set(name_index(entity_pages(root)))
    folders = {path.stem.casefold(): folder for folder in KNOWLEDGE_FOLDERS
               for path in Path(root, folder).glob("*.md")}
    kept = []
    for mutation in mutations:
        if mutation.action == "create":
            try:
                folder, filename = page_path(mutation.role or "", mutation.title or "").rsplit("/", 1)
            except PageContractError:
                kept.append(mutation)
                continue
            stem = filename.removesuffix(".md").casefold()
            aliases = tuple(mutation.aliases or ()) if mutation.role == "entity" else ()
            keys = {resolution_key(value) for value in (mutation.title or "", *aliases)}
            if keys & entity_names:
                skipped.append(f"{mutation.role} page {mutation.title!r} was not created: its name is already claimed")
                continue
            if folders.get(stem, folder) != folder:
                skipped.append(f"{mutation.role} page {mutation.title!r} was not created: its name is already claimed")
                contested.add(stem)
                continue
            taken = [alias for alias in aliases if folders.get(title_stem(alias).casefold(), folder) != folder]
            if taken:
                skipped.extend(f"alias {alias!r} of {mutation.title!r} was not added: another page has that name"
                               for alias in taken)
                aliases = tuple(alias for alias in aliases if alias not in taken)
                mutation = mutation.model_copy(update={"aliases": aliases})
                keys = {resolution_key(value) for value in (mutation.title or "", *aliases)}
            if mutation.role == "entity":
                entity_names |= keys
            for name in (stem, *(title_stem(alias).casefold() for alias in aliases)):
                folders.setdefault(name, folder)
        kept.append(mutation)
    return tuple(kept)


def _apply_page_mutation(
    root: str,
    mutation: PageMutation,
    *,
    context: WriteContext,
    source: str,
    at: dt.date,
    reasons: dict[str, str],
) -> str | None:
    if mutation.action == "create":
        target_path = page_path(mutation.role or "", mutation.title or "")
        allow_create(context, context.content_acl)
        body = mutation.body or ""
        try:
            if contradictions.parse_all(body):
                raise KnowledgeWriteError("contradictions must use the structured contradiction plan")
        except contradictions.ContradictionContractError as error:
            raise KnowledgeWriteError("planned page body has invalid contradiction markers") from error
        text = render_page(
            path=target_path,
            role=mutation.role or "",
            title=mutation.title or "",
            body=body,
            acl=context.content_acl,
            sources=(source,),
            status=mutation.status or "developing",
            created=at,
            updated=at,
            entity_type=mutation.entity_type,
            aliases=tuple(mutation.aliases or ()),
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
            raise KnowledgeWriteError("a capture cannot delete a page with unresolved contradictions")
        target.unlink()
        reasons[target_path] = mutation.reason
        return None

    # An update is path-addressed. Model-supplied create metadata cannot rename or retype it.
    if page.role != "entity" and (mutation.entity_type or mutation.aliases):
        raise KnowledgeWriteError("only entity pages carry an entity type or aliases")
    body = _preserve_contradictions(page.body, mutation.body or "")
    lost = _lost_source_backed_lines(page.body, body)
    if lost:
        raise ExistingSourceBlockNotPreserved(target_path, lost)
    missing_attributions = source_attributions(page.body) - source_attributions(body)
    if missing_attributions:
        raise KnowledgeWriteError("planned update drops existing local source attribution")
    rendered = render_like(
        page,
        body=body,
        sources=tuple(dict.fromkeys((*page.sources, source))),
        status=mutation.status or page.status,
        updated=at,
        aliases=page.aliases if mutation.aliases is None else tuple(mutation.aliases),
    )
    target.write_text(rendered, encoding="utf-8")
    reasons[target_path] = mutation.reason
    return target_path


def _source_backed_blocks(body: str) -> tuple[str, ...]:
    """Return exact prose blocks whose evidence must survive additive capture updates."""
    return tuple(block.strip() for block in re.split(r"\n\s*\n", body) if source_attributions(block))


def _lost_source_backed_lines(existing: str, proposed: str) -> tuple[str, ...]:
    """The lines of the first source-backed block `proposed` loses or rewords. Each line must survive
    in order, so a dated line inserted into a cited timeline keeps the evidence around it."""
    kept = layers.unit_key(proposed)
    for block in _source_backed_blocks(existing):
        at = 0
        for line in block.splitlines():
            key = layers.unit_key(line)
            if not key:
                continue
            found = kept.find(key, at)
            if found < 0:
                return tuple(line.strip() for line in block.splitlines() if line.strip())
            at = found + len(key)
    return ()


def _preserve_contradictions(existing: str, proposed: str) -> str:
    try:
        current = {item.record.contradiction_id: item.record for item in contradictions.parse_all(existing)}
        candidate = {item.record.contradiction_id: item.record for item in contradictions.parse_all(proposed)}
    except contradictions.ContradictionContractError as error:
        raise KnowledgeWriteError("planned page body has invalid contradiction markers") from error
    if set(candidate) - set(current):
        raise KnowledgeWriteError("contradictions must use the structured contradiction plan")
    if any(candidate[item_id] != record for item_id, record in current.items() if item_id in candidate):
        raise KnowledgeWriteError("existing contradiction claims cannot be rewritten in a page mutation")
    result = proposed
    for item_id, record in current.items():
        if item_id not in candidate:
            result = contradictions.append(result, record)
    return result


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
    for folder in KNOWLEDGE_FOLDERS:
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
            render_like(
                page,
                body=body,
                sources=tuple(dict.fromkeys((*page.sources, resolution_source))),
                updated=at,
            ),
            encoding="utf-8",
        )
    return tuple(relative for _path, relative, _page, _body in candidates)


def _sweep_deleted_references(root: str, deleted: set[str], *, layers_only: bool = False) -> dict[str, str]:
    """Remove what pointed at deleted physical paths from every page that can no longer resolve it:
    wikilinks become plain text, and units, contradictions, and declarations citing a deleted
    source go. A page left with nothing but its heading is deleted in turn."""
    reasons: dict[str, str] = {}
    at = dt.datetime.now(dt.UTC)
    pending = set(deleted)
    removed = set(deleted)
    while pending:
        gone = [layers.split_path(path) for path in pending]
        pending = set()
        for layer in layered.layer_names(root):
            if layers_only and layer == layers.PUBLIC:
                continue
            scope = {layer, layers.PUBLIC}
            pages = {
                layers.physical_path(each, vpath): page
                for each in dict.fromkeys((layer, layers.PUBLIC))
                for vpath, (page, _text) in layered.knowledge_pages(root, each).items()
            }
            living = {Path(path).stem.casefold() for path in pages}
            dead_stems = {Path(vpath).stem.casefold() for owner, vpath in gone if owner in scope} - living
            dead_sources = {vpath for owner, vpath in gone if owner in scope and vpath.startswith("sources/")}
            for vpath, (page, _text) in layered.knowledge_pages(root, layer).items():
                physical = layers.physical_path(layer, vpath)
                body = _remove_wikilinks(page.body, dead_stems)
                for located in reversed(contradictions.parse_all(body)):
                    if any(claim.source in dead_sources for claim in located.record.claims):
                        body, _removed = contradictions.remove(body, located.record.contradiction_id)
                body = _remove_citing_units(body, dead_sources)
                sources = tuple(source for source in page.sources if source not in dead_sources)
                if body == page.body and sources == page.sources:
                    continue
                target = _path(root, physical)
                if not _has_units(body):
                    target.unlink()
                    pending.add(physical)
                    removed.add(physical)
                    reasons[physical] = "Removed knowledge whose every source was deleted"
                    continue
                target.write_text(render_like(page, body=body, sources=sources, updated=at.date()), encoding="utf-8")
                reasons[physical] = "Removed references to deleted knowledge"
    return {path: reason for path, reason in reasons.items() if path not in deleted}


def _remove_citing_units(body: str, sources: set[str]) -> str:
    if not sources or not any(source in body for source in sources):
        return body
    parsed = layers.parse_body(body)
    for section in parsed.sections:
        section.units = [unit for unit in section.units
                         if unit.kind == "callout" or not any(source in unit.text for source in sources)]
    return layers.render_body(parsed).strip()


def _has_units(body: str) -> bool:
    return any(section.units for section in layers.parse_body(body).sections)


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
    layer: str | None = None,
    swept: frozenset[str] = frozenset(),
) -> None:
    """A write touches only knowledge paths; a capture only its own layer, except the link sweep a
    deleted public page leaves in other layers."""
    if not entries:
        raise GateRefused("writer produced no change")
    deleted_sources = deleted_sources or set()
    for entry in entries:
        if not entry.is_regular_file:
            raise GateRefused("writer changed a non-regular file")
        try:
            entry_layer, vpath = layers.split_path(entry.path)
        except layers.LayerError as error:
            raise GateRefused("writer changed a path outside the knowledge contract") from error
        if not (path_role(vpath) or vpath.startswith("sources/")):
            raise GateRefused("writer changed a path outside the knowledge contract")
        if layer is not None and entry_layer != layer and not (entry.path in swept and entry.status == "M"):
            raise GateRefused("writer changed a path outside the capture's layer")
        if vpath.startswith("sources/"):
            if trigger in {"capture", "contradiction_resolution"}:
                if entry.path != expected_source or entry.status != "A":
                    raise GateRefused("capture attempted to mutate an existing source")
            elif trigger == "delete":
                if entry.path not in deleted_sources or entry.status != "D":
                    raise GateRefused("delete attempted an undeclared source mutation")
            else:
                raise GateRefused("maintenance attempted to mutate a source")


@contextmanager
def _open_view(worktree: str, layer: str, deps: WriterDeps):
    root = gitcmd.scratch_path(deps.repo, deps.settings.worktree_root)
    try:
        view = layered.build_view(worktree, layer, root=root)
    except (PageContractError, layered.LayerProjectionError, OSError, UnicodeError) as error:
        raise CorpusUnavailable("the layer's knowledge could not be read") from error
    try:
        yield view
    finally:
        layered.discard(view)


def _view_baseline(root: str, layer: str) -> tuple[frozenset[Violation], tuple[Violation, ...]]:
    """Violations a view may carry without blocking a write, and the ones that block it. Only a
    restricted layer's view can hold name conflicts a public write introduced unseen."""
    found = check(root)
    tolerated = frozenset(
        item for item in found if layer != layers.PUBLIC and item.code in layered.CROSS_LAYER_CODES)
    return tolerated, tuple(item for item in found if item not in tolerated)


def _introduced(violations, baseline) -> tuple[Violation, ...]:
    return tuple(item for item in violations if item not in baseline)


def _sweep_other_layers(worktree: str, layer: str, projected: dict[str, str]) -> frozenset[str]:
    """Degrade links in other layers that pointed at public pages this write deleted."""
    if layer != layers.PUBLIC:
        return frozenset()
    deleted = {path for path, change in projected.items() if change == "deleted"}
    if not deleted:
        return frozenset()
    return frozenset(_sweep_deleted_references(worktree, deleted, layers_only=True))


def _commit_and_record(
    conn,
    deps: WriterDeps,
    *,
    worktree: str,
    base_sha: str,
    entries,
    item_id: str,
    expected_attempts: int,
    expected_lease_started_at: str,
    trigger: str,
    actor: str,
    summary: str,
    reasons: dict[str, str],
    capture_id: str | None = None,
    job_run_id: str | None = None,
) -> tuple[str, ChangeRecord]:
    safe_summary = _safe_summary(summary)
    message = f"feat(knowledge): {safe_summary[:68]}\n\nStigmergy-Operation: {item_id}\nStigmergy-Trigger: {trigger}\n"
    if not queue.holds_lease(
        conn,
        item_id,
        expected_attempts=expected_attempts,
        expected_lease_started_at=expected_lease_started_at,
    ):
        raise QueueStateError("processing lease was redelivered before publication")
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
        relative_source = layers.physical_path(layers.layer_of_acl(envelope.audience), source_path(envelope))
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
    sources = {path for path in paths if layers.virtual_path(path).startswith("sources/")}
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
    if not all(path_role(path) for path in paths):
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
        try:
            files[path] = parse_page(path, text).body
        except PageContractError as error:
            raise GateRefused("repair target has no preservable page metadata") from error
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
    allowed = {violation.path for violation in violations if path_role(violation.path)}
    changed = {}
    for mutation in plan.mutations:
        if mutation.path not in allowed:
            raise GateRefused("model repair targeted a path outside its violations")
        if authorized_files is not None and mutation.path not in authorized_files:
            raise GateRefused("model repair targeted a path outside this capture's authorization")
        target = _path(root, mutation.path)
        if not target.is_file():
            raise GateRefused("model repair targeted a missing path")
        try:
            before = parse_page(mutation.path, target.read_text(encoding="utf-8"))
        except PageContractError as error:
            raise GateRefused("repair target has no preservable page metadata") from error
        if authorized_files is not None:
            if before.body != authorized_files[mutation.path]:
                raise GateRefused("repair target changed after authorization")
            if context is None:
                raise GateRefused("capture repair is missing its authorization context")
            try:
                if mutation.path in existing_paths:
                    allow_existing(context, before.acl)
                else:
                    allow_create(context, before.acl)
            except WriteRefused as error:
                raise GateRefused("model repair is outside the capture audience") from error
        target.write_text(render_like(before, body=mutation.body), encoding="utf-8")
        changed[mutation.path] = mutation.reason
    return changed


def _gate_garden_acl(repo: str, base_sha: str, worktree: str, entries) -> None:
    for entry in entries:
        vpath = layers.virtual_path(entry.path)
        if entry.status != "M" or not path_role(vpath):
            continue
        before = parse_page(vpath, gitcmd.show(repo, base_sha, entry.path))
        after = parse_page(
            vpath,
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


def _snapshot_mutable(root: str) -> dict[str, bytes]:
    snapshot = {}
    for folder in KNOWLEDGE_FOLDERS:
        base = _path(root, folder)
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            snapshot[path.relative_to(root).as_posix()] = path.read_bytes()
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
