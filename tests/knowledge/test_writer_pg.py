import datetime as dt
import io
import json
import re
import subprocess
import zipfile
from pathlib import Path

import pymupdf
import pytest

from stigmergy.capture import evidence, queue, schema
from stigmergy.capture.errors import ArtifactRejected, QueueStateError
from stigmergy.capture.fetch import FetchedArtifact
from stigmergy.capture.schema import Actor
from stigmergy.capture.service import CaptureService
from stigmergy.capture.source import source_path
from stigmergy.changes.errors import ChangeError
from stigmergy.changes.store import list_changes
from stigmergy.index import build
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.index.corpus import split_frontmatter_checked
from stigmergy.knowledge import contradictions, writer
from stigmergy.knowledge import sources as knowledge_sources
from stigmergy.knowledge.lint import check
from stigmergy.knowledge.pages import parse_page, render_page
from stigmergy.knowledge.plan import (
    ContradictionClaim,
    ContradictionProposal,
    FilingPlan,
    PageMutation,
    RepairMutation,
    RepairPlan,
)
from stigmergy.knowledge.planner import PlanRun, ScriptedPlanner
from stigmergy.knowledge.write_guard import WriteContext
from stigmergy.knowledge.writer import KnowledgeWriteError, WriterDeps
from stigmergy.librarian import config, worker
from stigmergy.server.service import BrainService
from stigmergy.server.settings import Settings as ServerSettings

_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


class EditorialFixturePlanner(ScriptedPlanner):
    """Explicit compatibility fixture for pre-editorial successful writer scenarios only."""

    def plan(self, **kwargs) -> PlanRun:
        run = super().plan(**kwargs)
        plan = run.plan
        if not isinstance(plan, FilingPlan):
            return run
        source = str(kwargs["source_path"])
        context = json.loads(str(kwargs["context"]))
        prior_sources = {
            candidate["path"]: tuple(candidate.get("sources", ()))
            for candidate in context.get("candidates", ())
        }
        mutations = tuple(
            _editorial_fixture_mutation(
                mutation,
                source=source,
                prior_sources=prior_sources.get(mutation.path or "", ()),
            )
            for mutation in plan.mutations
        )
        return PlanRun(plan.model_copy(update={"mutations": mutations}), run.model_requests)


def _editorial_fixture_mutation(
    mutation: PageMutation,
    *,
    source: str,
    prior_sources: tuple[str, ...],
) -> PageMutation:
    if mutation.action not in {"create", "update"} or not mutation.body:
        return mutation
    title = mutation.title or Path(mutation.path or "").stem
    body_lines = mutation.body.strip().splitlines()
    if body_lines and body_lines[0].startswith("# "):
        body_lines[0] = f"# {title}"
    else:
        body_lines.insert(0, f"# {title}")
    body = "\n".join(body_lines).strip()
    prose = " ".join(body_lines[1:]).strip()
    if len(re.sub(r"[^A-Za-z0-9]", "", prose)) < 32:
        body += (
            f"\n\n{mutation.reason.rstrip('.')}. "
            "This conclusion remains useful as durable operating knowledge."
        )
    if source not in body:
        body += f"\n\n(Source: `{source}`)"
    for prior_source in prior_sources:
        if prior_source not in body:
            body += f"\n\n(Source: `{prior_source}`)"
    return mutation.model_copy(update={"body": body})


def _process_capture(
    conn,
    repo,
    store,
    *,
    actor: Actor,
    audience: tuple[str, ...] | None,
    key: str,
    text: str,
    plan: FilingPlan,
    planner: ScriptedPlanner | None = None,
    editorial: bool = False,
):
    receipt = CaptureService(conn, store).capture_text(
        actor=actor,
        audience=audience,
        adapter="mcp",
        text=text,
        idempotency_key=key,
    )
    settings = config.Settings(repo=str(repo), branch="main", backend="scripted")
    item, outcome = worker.process_next(
        conn,
        WriterDeps(
            settings,
            store,
            planner or (EditorialFixturePlanner(plan) if editorial else ScriptedPlanner(plan)),
            str(repo),
        ),
    )
    return receipt, item, outcome


def _lint_main(repo) -> tuple:
    """Lint the landed branch: the worker advances `main` without touching this checkout."""
    subprocess.run(["git", "reset", "-q", "--hard", "main"], cwd=repo, check=True)
    return check(str(repo))


def _tree(repo) -> list[str]:
    return subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "main"], cwd=repo, text=True
    ).splitlines()


def _show(repo, path: str) -> str:
    return subprocess.check_output(["git", "show", f"main:{path}"], cwd=repo, text=True)


def test_resolution_plan_without_capture_resolution_intent_is_refused(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    receipt = CaptureService(clean_queue, store).capture_text(
        actor=Actor(subject="morgan", display_name="Morgan"),
        audience=None,
        adapter="admin",
        text="A purported contradiction resolution.",
        idempotency_key="resolution-without-intent",
    )
    envelope = schema.parse_capture(receipt["request"])

    with pytest.raises(KnowledgeWriteError, match="only the contradiction named"):
        writer._apply_filing_plan(
            str(target_repo),
            FilingPlan(
                summary="Resolved an existing contradiction",
                resolved_contradictions=("con_00000000-0000-4000-8000-000000000001",),
            ),
            context=WriteContext(actor_groups=None, content_acl=None, unrestricted=True),
            envelope=envelope,
            relative_source="sources/2026/08/00000000-0000-4000-8000-000000000001.md",
            reasons={},
            allowed_contradiction_sources=frozenset(),
        )


def test_rejected_plan_records_why_in_the_capture_report(clean_queue, target_repo):
    """The reason a plan was refused is operator evidence: without it a refused plan and a source
    with no durable conclusion look identical in the backoffice."""
    store = evidence.MemoryEvidenceStore()
    plan = FilingPlan(
        summary="Resolved a contradiction this capture never named",
        resolved_contradictions=("con_00000000-0000-4000-8000-000000000001",),
    )

    _receipt, item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="morgan", display_name="Morgan"),
        audience=None,
        key="rejected-plan-reason",
        text="A plain capture whose plan overreaches.",
        plan=plan,
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["plan_rejected"] is True
    assert item["report"]["plan_rejection"] == "filing correction failed"
    # The submitter reads the summary; a rejected plan must not read like a source with nothing new.
    assert item["report"]["summary"] == "Archived source without wiki changes: the filing plan was rejected"


def test_missing_required_plan_items_reject_the_entire_derived_graph(clean_queue, target_repo):
    """A source can land, but no mutation or contradiction may silently disappear."""
    store = evidence.MemoryEvidenceStore()
    receipt = CaptureService(clean_queue, store).capture_text(
        actor=Actor(subject="morgan", display_name="Morgan"),
        audience=None,
        adapter="mcp",
        text="A capture whose plan names a page that does not exist.",
        idempotency_key="skipped-plan-items",
    )
    own_source = source_path(schema.parse_capture(receipt["request"]))
    plan = FilingPlan(
        summary="Touches a page that is not there",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/notes/Missing page.md",
                body="# Missing page\n\nNew knowledge.",
                reason="The source updates a page that was never written",
            ),
        ),
        contradictions=(
            ContradictionProposal(
                page_path="wiki/notes/Missing page.md",
                explanation="Two claims disagree.",
                claims=(
                    ContradictionClaim(text="It is annual.", source=own_source, date="2026-08-20"),
                    ContradictionClaim(text="It is monthly.", source=own_source, date="2026-08-21"),
                ),
            ),
        ),
    )
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    item, outcome = worker.process_next(
        clean_queue,
        WriterDeps(settings, store, ScriptedPlanner(plan), str(target_repo)),
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["plan_rejected"] is True
    assert item["report"]["wiki_changes"] == 0
    assert item["report"]["plan_skipped"] == []


def test_failed_plan_rolls_back_entity_page_creation(clean_queue, target_repo):
    """An entity page is ordinary derived knowledge: it lands with its plan or not at all."""
    store = evidence.MemoryEvidenceStore()
    plan = FilingPlan(
        summary="Attempted an atomic entity-backed conclusion",
        mutations=(
            PageMutation(
                action="create",
                role="entity",
                title="Atomic fixture organization",
                entity_type="organization",
                body="# Atomic fixture organization\n\nThis entity page must never land on its own.",
                reason="The plan pairs the entity page with an impossible update",
            ),
            PageMutation(
                action="update",
                path="wiki/notes/Missing page.md",
                body="# Missing page\n\nThe plan deliberately updates a page that does not exist.",
                reason="The plan deliberately contains an impossible mutation",
            ),
        ),
    )

    _receipt, item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="morgan", display_name="Morgan"),
        audience=None,
        key="atomic-entity-rollback",
        text="The entity-backed conclusion must be atomic.",
        plan=plan,
        editorial=True,
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["plan_rejected"] is True
    assert item["report"]["wiki_changes"] == 0
    assert "wiki/entities/Atomic fixture organization.md" not in _tree(target_repo)


def test_one_capture_lands_source_wiki_and_change_in_one_commit(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    material = "The support rotation changes to weekly on 1 September."
    receipt = CaptureService(clean_queue, store).capture_text(
        actor=Actor(subject="morgan", display_name="Morgan"),
        audience=None,
        adapter="mcp",
        text=material,
        idempotency_key="writer-e2e",
        title="Support rotation",
    )
    source = source_path(schema.parse_capture(receipt["request"]))
    plan = FilingPlan(
        summary="Recorded the weekly support rotation",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Support rotation",
                    body=("# Support rotation\n\nThe support rotation changes weekly on 1 September. "
                          f"(Source: `{source}`)"),
                reason="The source establishes the new operating cadence",
            ),
        ),
    )
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    deps = WriterDeps(settings, store, ScriptedPlanner(plan), str(target_repo))

    item, outcome = worker.process_next(clean_queue, deps)

    assert outcome.status == "landed"
    source_text = subprocess.check_output(
        ["git", "show", f"main:{item['source_path']}"], cwd=target_repo, text=True
    )
    note_text = subprocess.check_output(
        ["git", "show", "main:wiki/notes/Support rotation.md"],
        cwd=target_repo,
        text=True,
    )
    assert material in source_text
    assert "The support rotation changes weekly" in note_text
    commits = subprocess.check_output(
        ["git", "rev-list", "--count", "main"], cwd=target_repo, text=True
    ).strip()
    assert commits == "2"
    changes = list_changes(clean_queue)
    assert len(changes) == 1
    assert changes[0].commit_sha == item["commit_sha"]
    assert {entry.page_role for entry in changes[0].manifest} == {"source", "note"}

    duplicate = CaptureService(clean_queue, store).capture_text(
        actor=Actor(subject="morgan", display_name="Morgan"),
        audience=None,
        adapter="mcp",
        text=material,
        idempotency_key="writer-e2e",
        title="Support rotation",
    )
    assert duplicate["id"] == receipt["id"]
    assert duplicate["status"] == "landed"
    assert subprocess.check_output(
        ["git", "rev-list", "--count", "main"], cwd=target_repo, text=True
    ).strip() == "2"


def test_title_form_wikilink_lands_as_the_canonical_page_link(clean_queue, target_repo):
    """A link written with the page title resolves to the filename the title was sanitized into."""
    store = evidence.MemoryEvidenceStore()
    meeting = "Reunión: Pre Quillstack — 2026-03-03"
    plan = FilingPlan(
        summary="Recorded the pre-alignment meeting and its weekly follow-up",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title=meeting,
                body=f"# {meeting}\n\nThe partners aligned on the diligence questions before the call.",
                reason="The source records a dated pre-alignment meeting",
            ),
            PageMutation(
                action="create",
                role="note",
                title="Seguimiento semanal de Quillstack",
                body=(
                    "# Seguimiento semanal de Quillstack\n\n"
                    f"The weekly follow-up continues the questions agreed in [[{meeting}]]."
                ),
                reason="The source sets a weekly follow-up cadence",
            ),
        ),
    )

    _receipt, item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="morgan", display_name="Morgan"),
        audience=None,
        key="title-form-link",
        text="Pre-alignment meeting before the Quillstack call; follow up weekly.",
        plan=plan,
        editorial=True,
    )

    assert outcome.status == "landed"
    assert outcome.report["plan_rejected"] is False
    measure = outcome.report["filing_context"]
    assert set(measure) == {"bytes", "budget_bytes", "catalog", "candidates", "evidence", "trimmed"}
    assert 0 < measure["bytes"] <= measure["budget_bytes"] and measure["trimmed"] == 0
    follow_up = subprocess.check_output(
        ["git", "show", "main:wiki/notes/Seguimiento semanal de Quillstack.md"],
        cwd=target_repo,
        text=True,
    )
    assert f"[[Reunión- Pre Quillstack — 2026-03-03|{meeting}]]" in follow_up


def test_public_url_lands_sanitized_original_and_final_provenance(
    clean_queue, target_repo, monkeypatch
):
    store = evidence.MemoryEvidenceStore()
    monkeypatch.setattr(
        "stigmergy.capture.service.fetch.fetch_public",
        lambda _url, **_options: FetchedArtifact(
            data=b"Redirected public report",
            final_url="https://cdn.example/reports/current.txt",
            response_media_type=schema.MEDIA_TEXT,
        ),
    )
    receipt = CaptureService(clean_queue, store).capture_public_url(
        actor=Actor(subject="morgan", display_name="Morgan"),
        audience=None,
        adapter="mcp",
        url="https://files.example/latest?signature=secret#viewer",
        idempotency_key="public-url-provenance",
    )
    item, outcome = worker.process_next(
        clean_queue,
        WriterDeps(
            config.Settings(
                repo=str(target_repo), branch="main", backend="scripted"
            ),
            store,
            ScriptedPlanner(FilingPlan(summary="Archived the public report")),
            str(target_repo),
        ),
    )

    queued = queue.get_submission_trace(clean_queue, receipt["id"])["request"]
    source_text = subprocess.check_output(
        ["git", "show", f"main:{item['source_path']}"],
        cwd=target_repo,
        text=True,
    )
    metadata, _body, malformed = split_frontmatter_checked(source_text)

    assert outcome.status == schema.LANDED
    assert malformed is False
    assert queued["origin"]["acquisition"]["original_url"] == (
        "https://files.example/latest"
    )
    assert metadata["acquisition"]["original_url"] == "https://files.example/latest"
    assert metadata["acquisition"]["final_url"] == (
        "https://cdn.example/reports/current.txt"
    )
    assert "secret" not in json.dumps(queued)
    assert "secret" not in source_text


def test_update_ignores_model_supplied_role_and_title(
    clean_queue, target_repo
):
    for title, body in (
        ("Target", "# Target\n\nCurrent target knowledge."),
        ("Reference", "# Reference\n\nSee [[Target]]."),
    ):
        path = target_repo / "wiki" / "notes" / f"{title}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_page(
                path=f"wiki/notes/{title}.md",
                role="note",
                title=title,
                body=body,
                acl=None,
                created=dt.date(2026, 8, 24),
                updated=dt.date(2026, 8, 24),
            ),
            encoding="utf-8",
        )
    subprocess.run(["git", "add", "."], cwd=target_repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "seed linked pages",
        ],
        cwd=target_repo,
        check=True,
    )
    store = evidence.MemoryEvidenceStore()
    plan = FilingPlan(
        summary="Updated the target",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/notes/Target.md",
                role="concept",
                title="Renamed target",
                body=(
                    "# Target\n\nThe target now records the current operating decision "
                    "with sufficient supporting detail."
                ),
                reason="The source updates the target",
            ),
        ),
    )

    class CurrentTargetPlanner(ScriptedPlanner):
        def plan(self, **kwargs) -> PlanRun:
            run = super().plan(**kwargs)
            mutation = run.plan.mutations[0]
            body = f"{mutation.body}\n\n(Source: `{kwargs['source_path']}`)"
            return PlanRun(
                run.plan.model_copy(
                    update={"mutations": (mutation.model_copy(update={"body": body}),)}
                ),
                run.model_requests,
            )

    _receipt, item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="morgan", display_name="Morgan"),
        audience=None,
        key="update-ignores-model-create-fields",
        text="The current target changed.",
        plan=plan,
        planner=CurrentTargetPlanner(plan),
    )

    tree = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "main"],
        cwd=target_repo,
        text=True,
    ).splitlines()
    assert outcome.status == schema.LANDED
    assert item["report"]["plan_rejected"] is False
    assert item["report"]["wiki_changes"] == 1
    assert "wiki/notes/Target.md" in tree
    assert "wiki/notes/Renamed target.md" not in tree
    page = parse_page(
        "wiki/notes/Target.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Target.md"], cwd=target_repo, text=True
        ),
    )
    assert (page.role, page.title) == ("note", "Target")
    assert [change.page_role for change in list_changes(clean_queue)[0].manifest] == ["source", "note"]


def test_pasted_transcript_archives_exact_input_and_files_only_its_conclusion(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    transcript = (
        "Alice: We will move the support rotation to weekly on 1 September.\n"
        "Bob: Agreed. I will update the rota."
    )
    receipt = CaptureService(clean_queue, store).capture_text(
        actor=Actor(subject="alice", display_name="Alice"),
        audience=("engineering",),
        adapter="mcp",
        text=transcript,
        idempotency_key="pasted-transcript",
    )
    source = source_path(schema.parse_capture(receipt["request"]))
    plan = FilingPlan(
        summary="Recorded the weekly support rotation decision",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Weekly support rotation",
                    body=(
                        "# Weekly support rotation\n\n"
                        "The team agreed to move the support rotation to weekly on 1 September. "
                        f"(Source: `{source}`)"
                    ),
                reason="The transcript establishes the decision",
            ),
        ),
    )

    item, outcome = worker.process_next(
        clean_queue,
        WriterDeps(config.Settings(repo=str(target_repo), branch="main", backend="scripted"), store,
                   EditorialFixturePlanner(plan), str(target_repo)),
    )

    request = queue.get_submission_trace(clean_queue, receipt["id"])["request"]
    artifact = schema.parse_capture(request).artifacts[0]
    source_text = subprocess.check_output(
        ["git", "show", f"main:{item['source_path']}"], cwd=target_repo, text=True
    )
    note_text = subprocess.check_output(
        ["git", "show", "main:layers/engineering/wiki/notes/Weekly support rotation.md"],
        cwd=target_repo,
        text=True,
    )

    assert outcome.status == schema.LANDED
    assert store.get(artifact.blob_ref) == transcript.encode("utf-8")
    assert source_text.endswith(transcript + "\n")
    assert "move the support rotation to weekly" in note_text
    assert "Alice:" not in note_text and "Bob:" not in note_text


def test_conclusions_only_submission_archives_only_the_supplied_synthesis(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    synthesis = "Decision: the launch remains on 15 October, owned by the product team."
    plan = FilingPlan(
        summary="Recorded the launch decision",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Launch date",
                body="# Launch date\n\nThe launch remains on 15 October and is owned by product.",
                reason="The submitted synthesis establishes the current launch decision",
            ),
        ),
    )

    receipt, item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="alice", display_name="Alice"),
        audience=("engineering",),
        key="conversation-synthesis",
        text=synthesis,
        plan=plan,
        editorial=True,
    )

    request = queue.get_submission_trace(clean_queue, receipt["id"])["request"]
    envelope = schema.parse_capture(request)
    source_text = subprocess.check_output(
        ["git", "show", f"main:{item['source_path']}"], cwd=target_repo, text=True
    )

    assert outcome.status == schema.LANDED
    assert store.get(envelope.artifacts[0].blob_ref) == synthesis.encode("utf-8")
    assert source_text.endswith(synthesis + "\n")
    assert envelope.origin.acquisition is None
    assert "transcript" not in source_text.casefold()
    assert "conversation history" not in source_text.casefold()


def _encrypted_pdf() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 100), "Protected text")
    data = document.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="reader-secret",
    )
    document.close()
    return data


def _corrupt_docx() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types><Override ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/></Types>',
        )
        archive.writestr("word/document.xml", "<not-valid-xml")
    return output.getvalue()


@pytest.mark.parametrize(
    ("data", "media_type"),
    [
        (b"%PDF-1.7\nthis is not a valid PDF", schema.MEDIA_PDF),
        (_encrypted_pdf(), schema.MEDIA_PDF),
        (_corrupt_docx(), schema.MEDIA_DOCX),
    ],
)
def test_invalid_queued_artifact_fails_typed_without_a_git_commit(
    clean_queue, target_repo, data, media_type
):
    store = evidence.MemoryEvidenceStore()
    CaptureService(clean_queue, store).capture_bytes(
        actor=Actor(subject="morgan", display_name="Morgan"),
        audience=None,
        adapter="mcp",
        artifact_values=((data, media_type, None, None),),
        idempotency_key=f"invalid-{media_type}-{len(data)}",
    )
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    before = subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip()

    item, outcome = worker.process_next(
        clean_queue,
        WriterDeps(settings, store, ScriptedPlanner(FilingPlan(summary="unused")), str(target_repo)),
    )

    assert outcome.status == schema.FAILED
    assert item["error_category"] == "invalid_artifact"
    assert "secret" not in item["error"].lower()
    assert subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip() == before


@pytest.mark.parametrize(
    ("data", "declared", "expected"),
    [
        (b"plain text", schema.MEDIA_PDF, "does not match"),
        (b"PK\x03\x04broken", None, "corrupt container"),
        (b"\x80\x81\x82unsupported", None, "unsupported"),
    ],
)
def test_rejected_acquisition_is_typed_and_never_queues_or_commits(
    clean_queue, target_repo, data, declared, expected
):
    store = evidence.MemoryEvidenceStore()
    before = subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip()

    with pytest.raises(ArtifactRejected, match=expected):
        CaptureService(clean_queue, store).capture_bytes(
            actor=Actor(subject="morgan", display_name="Morgan"),
            audience=None,
            adapter="mcp",
            artifact_values=((data, declared, None, None),),
            idempotency_key=f"rejected-{len(data)}",
        )

    with clean_queue.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM capture_queue")
        assert cursor.fetchone()[0] == 0
    assert subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip() == before


def test_unsafe_container_and_oversize_are_typed_before_queueing(
    clean_queue, target_repo, monkeypatch
):
    unsafe = io.BytesIO()
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../escape.xml", "x")
    service = CaptureService(clean_queue, evidence.MemoryEvidenceStore())

    with pytest.raises(ArtifactRejected, match="unsafe path"):
        service.capture_bytes(
            actor=Actor(subject="morgan", display_name="Morgan"),
            audience=None,
            adapter="mcp",
            artifact_values=((unsafe.getvalue(), None, None, None),),
            idempotency_key="unsafe-container",
        )

    monkeypatch.setattr(schema, "MAX_ARTIFACT_BYTES", 4)
    with pytest.raises(ArtifactRejected, match="size limit"):
        service.capture_bytes(
            actor=Actor(subject="morgan", display_name="Morgan"),
            audience=None,
            adapter="mcp",
            artifact_values=((b"12345", None, None, None),),
            idempotency_key="oversize",
        )

    with clean_queue.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM capture_queue")
        assert cursor.fetchone()[0] == 0
    assert subprocess.check_output(
        ["git", "rev-list", "--count", "main"], cwd=target_repo, text=True
    ).strip() == "1"


def test_capture_can_create_rewrite_consolidate_and_delete_in_one_atomic_commit(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="morgan", display_name="Morgan")
    first_plan = FilingPlan(
        summary="Established the initial operating pages",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Primary plan",
                body="# Primary plan\n\nInitial plan.",
                reason="The initial plan is durable",
            ),
            PageMutation(
                action="create",
                role="note",
                title="Duplicate plan",
                body="# Duplicate plan\n\nDuplicate details.",
                reason="The source initially separates this detail",
            ),
            PageMutation(
                action="create",
                role="concept",
                title="Temporary idea",
                body="# Temporary idea\n\nA temporary explanation.",
                reason="The source introduces the concept",
            ),
        ),
    )
    first, first_item, first_outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="initial-pages",
        text="Initial plan, duplicate detail, and temporary idea.",
        plan=first_plan,
        editorial=True,
    )
    assert first_outcome.status == schema.LANDED
    first_source = first_item["source_path"]
    first_source_bytes = subprocess.check_output(
        ["git", "show", f"main:{first_source}"], cwd=target_repo
    )

    second_plan = FilingPlan(
        summary="Consolidated the plan and removed obsolete material",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/notes/Primary plan.md",
                body="# Primary plan\n\nThe consolidated current plan.",
                status="mature",
                reason="The new source consolidates the current plan",
            ),
            PageMutation(
                action="delete",
                path="wiki/notes/Duplicate plan.md",
                reason="Its durable content is now in the primary plan",
            ),
            PageMutation(
                action="delete",
                path="wiki/concepts/Temporary idea.md",
                reason="The new evidence retracts the temporary idea",
            ),
            PageMutation(
                action="create",
                role="concept",
                title="Operating cadence",
                body="# Operating cadence\n\nUse a weekly review.",
                reason="The cadence is reusable explanatory knowledge",
            ),
        ),
    )
    _second, second_item, second_outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="consolidate-pages",
        text="Consolidate the plan, retract the temporary idea, and review weekly.",
        plan=second_plan,
        editorial=True,
    )

    assert second_outcome.status == schema.LANDED
    assert subprocess.check_output(
        ["git", "rev-list", "--count", "main"], cwd=target_repo, text=True
    ).strip() == "3"
    assert subprocess.check_output(
        ["git", "show", f"main:{first_source}"], cwd=target_repo
    ) == first_source_bytes
    primary = subprocess.check_output(
        ["git", "show", "main:wiki/notes/Primary plan.md"],
        cwd=target_repo,
        text=True,
    )
    assert "consolidated current plan" in primary
    assert parse_page("wiki/notes/Primary plan.md", primary).status == "mature"
    tree = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "main"],
        cwd=target_repo,
        text=True,
    ).splitlines()
    assert "wiki/notes/Duplicate plan.md" not in tree
    assert "wiki/concepts/Temporary idea.md" not in tree
    assert "wiki/concepts/Operating cadence.md" in tree
    change = list_changes(clean_queue)[0]
    assert change.commit_sha == second_item["commit_sha"]
    assert {entry.action for entry in change.manifest} == {
        "created",
        "updated",
        "deleted",
    }


def test_source_only_capture_is_a_landed_auditable_commit(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    plan = FilingPlan(summary="Archived evidence with no durable conclusion")

    _receipt, item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="alice", display_name="Alice"),
        audience=("engineering",),
        key="source-only",
        text="A transient observation with no durable conclusion.",
        plan=plan,
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["wiki_changes"] == 0
    change = list_changes(clean_queue)[0]
    assert [(entry.page_role, entry.action) for entry in change.manifest] == [
        ("source", "created")
    ]


class RecordingEditorialPlanner(EditorialFixturePlanner):
    """Keeps the rendered filing context the writer handed to the model."""

    context = ""

    def plan(self, **kwargs):
        self.context = kwargs["context"]
        return super().plan(**kwargs)


def test_entity_page_created_by_a_capture_is_linkable_from_a_later_note(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    alice = Actor(subject="alice", display_name="Alice")
    seed = FilingPlan(
        summary="Recorded the Northstar Research organization",
        mutations=(
            PageMutation(
                action="create",
                role="entity",
                title="Northstar Research",
                entity_type="organization",
                aliases=("Northstar",),
                body="# Northstar Research\n\nA research organization that renews its contract annually.",
                reason="The source identifies the organization",
            ),
        ),
    )
    _receipt, seed_item, seed_outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=alice,
        audience=None,
        key="entity-page-seed",
        text="Northstar Research renews annually.",
        plan=seed,
        editorial=True,
    )
    assert seed_outcome.status == schema.LANDED
    assert seed_item["report"]["plan_skipped"] == []
    entity_path = "wiki/entities/Northstar Research.md"
    entity = parse_page(entity_path, _show(target_repo, entity_path))
    assert (entity.role, entity.entity_type, entity.aliases) == ("entity", "organization", ("Northstar",))

    follow_up = FilingPlan(
        summary="Recorded the renewal",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Northstar renewal",
                body="# Northstar renewal\n\n[[Northstar Research]] approved the annual renewal.",
                reason="The source records the renewal",
            ),
        ),
    )
    planner = RecordingEditorialPlanner(follow_up)
    _receipt, item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=alice,
        audience=None,
        key="entity-page-link",
        text="Northstar approved the annual renewal.",
        plan=follow_up,
        planner=planner,
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["plan_rejected"] is False
    assert item["report"]["plan_skipped"] == []
    assert item["report"]["wiki_changes"] == 1
    catalog = {entry["path"]: entry for entry in json.loads(planner.context)["catalog"]}
    assert catalog["wiki/entities/Northstar Research.md"]["aliases"] == ["Northstar"]
    note = parse_page("wiki/notes/Northstar renewal.md", _show(target_repo, "wiki/notes/Northstar renewal.md"))
    assert "[[Northstar Research]] approved the annual renewal." in note.body
    assert _lint_main(target_repo) == ()


def test_a_project_page_lands_and_one_named_like_an_entity_is_skipped(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    plan = FilingPlan(
        summary="Recorded the supplier and its rollout",
        mutations=(
            PageMutation(
                action="create",
                role="entity",
                title="Velorum Signal Works",
                entity_type="organization",
                body="# Velorum Signal Works\n\nA calibration supplier.\n\n## Connections\n\n"
                     "- [[Velorum calibration rollout]] - the rollout it supplies",
                reason="The source identifies the supplier",
            ),
            PageMutation(
                action="create",
                role="project",
                title="Velorum calibration rollout",
                body="# Velorum calibration rollout\n\n## Goal\n\nRoll the calibration schedule out to every site."
                     "\n\n## People and organizations\n\n- [[Velorum Signal Works]] - supplier",
                reason="The source establishes an initiative that continues beyond it",
            ),
            PageMutation(
                action="create",
                role="project",
                title="Velorum Signal Works",
                body="# Velorum Signal Works\n\n## Goal\n\nThe same supplier filed again as a project.",
                reason="The plan repeats the supplier's name as a project",
            ),
        ),
    )

    _receipt, item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="alice", display_name="Alice"),
        audience=None,
        key="project-name-collision",
        text="Velorum Signal Works will roll the calibration schedule out to every site.",
        plan=plan,
        editorial=True,
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["plan_skipped"] == [
        "project page 'Velorum Signal Works' was not created: its name is already claimed"]
    tree = _tree(target_repo)
    assert "wiki/projects/Velorum calibration rollout.md" in tree
    assert "wiki/projects/Velorum Signal Works.md" not in tree
    project = parse_page("wiki/projects/Velorum calibration rollout.md",
                         _show(target_repo, "wiki/projects/Velorum calibration rollout.md"))
    assert project.role == "project" and project.entity_type is None
    assert "[[Velorum Signal Works]] - supplier" in project.body
    assert _lint_main(target_repo) == ()


def test_a_name_another_kind_of_page_holds_is_contested_and_an_alias_it_holds_is_left_off(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    first = FilingPlan(summary="Recorded the rollout", mutations=(PageMutation(
        action="create", role="project", title="Lumen rollout",
        body="# Lumen rollout\n\n## Goal\n\nRoll Lumen out to every team.", reason="An initiative"),))
    _process_capture(clean_queue, target_repo, store, actor=Actor(subject="alice", display_name="Alice"),
                     audience=None, key="rollout-1", text="Lumen will roll out to every team.", plan=first,
                     editorial=True)
    second = FilingPlan(summary="Recorded the review and the vendor", mutations=(
        PageMutation(action="create", role="note", title="Lumen rollout",
                     body="# Lumen rollout\n\nThe rollout was reviewed.", reason="The review"),
        PageMutation(action="create", role="entity", title="Lumen Labs", entity_type="organization",
                     aliases=("Lumen rollout", "Lumen"),
                     body="# Lumen Labs\n\nThe vendor. See [[Lumen rollout]].", reason="The vendor"),
    ))

    _receipt, item, outcome = _process_capture(
        clean_queue, target_repo, store, actor=Actor(subject="alice", display_name="Alice"), audience=None,
        key="rollout-2", text="Lumen Labs reviewed the Lumen rollout.", plan=second, editorial=True)

    assert outcome.status == schema.LANDED
    assert item["report"]["plan_skipped"][:2] == [
        "note page 'Lumen rollout' was not created: its name is already claimed",
        "alias 'Lumen rollout' of 'Lumen Labs' was not added: another page has that name",
    ]
    vendor = parse_page("wiki/entities/Lumen Labs.md", _show(target_repo, "wiki/entities/Lumen Labs.md"))
    assert vendor.aliases == ("Lumen",)
    assert "See Lumen rollout." in vendor.body
    assert _lint_main(target_repo) == ()


def test_entity_create_colliding_within_the_plan_is_skipped_and_the_rest_lands(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    plan = FilingPlan(
        summary="Recorded one supplier under two names",
        mutations=(
            PageMutation(
                action="create",
                role="entity",
                title="Velorum Signal Works",
                entity_type="organization",
                aliases=("VSW",),
                body="# Velorum Signal Works\n\nA calibration supplier known as VSW.",
                reason="The source identifies the supplier",
            ),
            PageMutation(
                action="create",
                role="entity",
                title="VSW",
                entity_type="organization",
                body="# VSW\n\nThe same supplier filed again under its acronym.",
                reason="The plan repeats the supplier under a claimed alias",
            ),
            PageMutation(
                action="create",
                role="note",
                title="Calibration approval",
                body="# Calibration approval\n\n[[Velorum Signal Works|VSW]] approved the calibration schedule.",
                reason="The source records the approval",
            ),
        ),
    )

    _receipt, item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="alice", display_name="Alice"),
        audience=None,
        key="entity-alias-collision",
        text="Velorum Signal Works, also called VSW, approved the schedule.",
        plan=plan,
        editorial=True,
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["plan_rejected"] is False
    assert item["report"]["plan_skipped"] == ["entity page 'VSW' was not created: its name is already claimed"]
    assert item["report"]["wiki_changes"] == 2
    tree = _tree(target_repo)
    assert "wiki/entities/Velorum Signal Works.md" in tree
    assert "wiki/entities/VSW.md" not in tree
    assert "[[Velorum Signal Works|VSW]] approved" in _show(target_repo, "wiki/notes/Calibration approval.md")
    assert _lint_main(target_repo) == ()


def test_describe_entity_returns_the_entity_page_and_only_the_pages_linking_it(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    plan = FilingPlan(
        summary="Recorded two otherwise similar Northstar renewal pages",
        mutations=(
            PageMutation(
                action="create",
                role="entity",
                title="Northstar Research",
                entity_type="organization",
                aliases=("Northstar",),
                body="# Northstar Research\n\nA research organization with an annual renewal.",
                reason="The source identifies the organization",
            ),
            PageMutation(
                action="create",
                role="note",
                title="Northstar explicit link",
                body="# Northstar explicit link\n\n[[Northstar Research]] approved the renewal.",
                reason="The source establishes a deliberate Northstar relationship",
            ),
            PageMutation(
                action="create",
                role="note",
                title="Northstar omitted link",
                body="# Northstar omitted link\n\nNorthstar Research approved the renewal.",
                reason="The source records a passing Northstar mention without a link",
            ),
        ),
    )

    _receipt, _item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="alice", display_name="Alice"),
        audience=None,
        key="explicit-entity-link-indexing",
        text="Northstar Research approved the renewal.",
        plan=plan,
        editorial=True,
    )

    assert outcome.status == schema.LANDED
    build.rebuild(clean_queue, str(target_repo), build_embedder("fake"))
    service = BrainService(
        ServerSettings(identity="morgan"),
        clean_queue,
        build_embedder("fake"),
        audiences=None,
        identity="morgan",
    )

    result = service.describe_entity("Northstar")

    assert result["found"] is True
    assert result["entity"]["path"] == "wiki/entities/Northstar Research.md"
    assert result["entity"]["aliases"] == ["Northstar"]
    assert [item["path"] for item in result["knowledge"]] == [
        "wiki/notes/Northstar explicit link.md"
    ]
    assert service.entity_vocabulary() == {
        "count": 1,
        "entities": [
            {
                "path": "wiki/entities/Northstar Research.md",
                "title": "Northstar Research",
                "entity_type": "organization",
                "aliases": ["Northstar"],
                "layers": ["public"],
            }
        ],
    }


def test_guessed_hidden_page_and_entity_cannot_be_affected_or_leaked(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    bob = Actor(subject="bob", display_name="Bob")
    alice = Actor(subject="alice", display_name="Alice")
    hidden_plan = FilingPlan(
        summary="Recorded a private finance relationship",
        mutations=(
            PageMutation(
                action="create",
                role="entity",
                title="Stealth Holdings",
                entity_type="organization",
                aliases=("Stealth",),
                body="# Stealth Holdings\n\nA restricted finance counterparty.",
                reason="Finance needs the restricted counterparty",
            ),
            PageMutation(
                action="create",
                role="note",
                title="Private finance plan",
                body="# Private finance plan\n\n[[Stealth Holdings]] holds the restricted plan.",
                reason="Finance needs the restricted plan",
            ),
        ),
    )
    _receipt, _item, hidden_outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=bob,
        audience=("finance",),
        key="hidden-seed",
        text="Stealth Holdings has a restricted finance plan.",
        plan=hidden_plan,
        editorial=True,
    )
    assert hidden_outcome.status == schema.LANDED
    landed_head = subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip()
    landed_changes = len(list_changes(clean_queue))

    guessed_page_plan = FilingPlan(
        summary="Attempted an unauthorized rewrite",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/notes/Private finance plan.md",
                body="# Private finance plan\n\nLeaked rewrite.",
                reason="Guessed path",
            ),
        ),
    )
    _receipt, page_item, page_outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=alice,
        audience=("engineering",),
        key="guess-hidden-page",
        text="Try the guessed page path.",
        plan=guessed_page_plan,
    )
    assert page_outcome.status == schema.LANDED
    assert page_item["report"]["wiki_changes"] == 0

    guessed_link_plan = FilingPlan(
        summary="Attempted a link to a hidden entity page",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Guessed identity",
                body="# Guessed identity\n\nEngineering also works with [[Stealth Holdings]] on the plan.",
                reason="Guessed entity page",
            ),
        ),
    )
    planner = RecordingEditorialPlanner(guessed_link_plan)
    _receipt, link_item, link_outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=alice,
        audience=("engineering",),
        key="guess-hidden-entity",
        text="Try a guessed entity link about Stealth.",
        plan=guessed_link_plan,
        planner=planner,
    )

    assert link_outcome.status == schema.LANDED
    assert link_item["report"]["plan_rejected"] is False
    assert link_item["report"]["plan_skipped"] == [
        "unlinked unresolved wikilink 'Stealth Holdings' in wiki/notes/Guessed identity.md"
    ]
    guessed = parse_page("wiki/notes/Guessed identity.md",
                         _show(target_repo, "layers/engineering/wiki/notes/Guessed identity.md"))
    assert guessed.acl == ("engineering",)
    assert "[[" not in guessed.body
    assert "works with Stealth Holdings on the plan" in guessed.body
    assert "Stealth Holdings" not in planner.context
    assert "wiki/entities/Stealth Holdings.md" not in planner.context
    assert _show(target_repo, "layers/finance/wiki/notes/Private finance plan.md") == _show(
        target_repo, "layers/finance/wiki/notes/Private finance plan.md"
    )
    assert subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip() != landed_head
    assert len(list_changes(clean_queue)) == landed_changes + 2
    assert _lint_main(target_repo) == ()


def test_layer_capture_extends_a_public_page_only_inside_its_layer(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    open_plan = FilingPlan(
        summary="Recorded the public policy",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Public policy",
                body="# Public policy\n\nOrganization-wide policy.",
                reason="The policy is organization-wide",
            ),
        ),
    )
    _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="morgan", display_name="Morgan"),
        audience=None,
        key="open-policy",
        text="This policy is organization-wide.",
        plan=open_plan,
        editorial=True,
    )
    open_before = subprocess.check_output(
        ["git", "show", "main:wiki/notes/Public policy.md"],
        cwd=target_repo,
        text=True,
    )
    public_body = parse_page("wiki/notes/Public policy.md", open_before).body

    rewrite_plan = FilingPlan(
        summary="Attempted to replace the public policy",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/notes/Public policy.md",
                body="# Public policy\n\nRestricted finance detail.",
                reason="Unsafe replacement",
            ),
        ),
    )
    _receipt, rewrite_item, rewrite_outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="bob", display_name="Bob"),
        audience=("finance",),
        key="unsafe-open-update",
        text="Restricted finance detail.",
        plan=rewrite_plan,
    )
    assert rewrite_outcome.status == schema.LANDED
    assert rewrite_item["report"]["wiki_changes"] == 0
    assert rewrite_item["report"]["plan_rejected"] is True

    mixed_plan = FilingPlan(
        summary="Recorded the restricted detail and extended the policy",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Finance budget detail",
                body="# Finance budget detail\n\nRestricted budget detail. See [[Public policy]].",
                reason="Restricted evidence belongs in a restricted page",
            ),
            PageMutation(
                action="update",
                path="wiki/notes/Public policy.md",
                body=f"{public_body}\n\nThe finance team funds the policy from the restricted budget.",
                reason="The finance layer knows how the policy is funded",
            ),
        ),
    )
    _receipt, mixed_item, mixed_outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="bob", display_name="Bob"),
        audience=("finance",),
        key="mixed-restricted",
        text="Restricted budget detail.",
        plan=mixed_plan,
        editorial=True,
    )
    assert mixed_outcome.status == schema.LANDED
    assert mixed_item["report"]["wiki_changes"] == 2
    assert subprocess.check_output(
        ["git", "show", "main:wiki/notes/Public policy.md"], cwd=target_repo, text=True
    ) == open_before
    companion = subprocess.check_output(
        ["git", "show", "main:layers/finance/wiki/notes/Public policy.md"], cwd=target_repo, text=True)
    companion_page = parse_page("wiki/notes/Public policy.md", companion)
    assert companion_page.acl == ("finance",)
    assert "funds the policy from the restricted budget" in companion_page.body
    assert "Organization-wide policy." not in companion_page.body
    mixed_note = subprocess.check_output(
        ["git", "show", "main:layers/finance/wiki/notes/Finance budget detail.md"], cwd=target_repo, text=True)
    assert "[[Public policy]]" in mixed_note
    assert parse_page("wiki/notes/Finance budget detail.md", mixed_note).acl == ("finance",)
    assert check(str(target_repo)) == ()


def test_invalid_filing_candidate_lands_only_immutable_evidence(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    before = subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip()
    invalid_plan = FilingPlan(
        summary="Attempted an invalid page",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Broken page",
                body="# Broken page\n\nThis points to [[Missing page]].",
                reason="Invalid link fixture",
            ),
        ),
    )

    _receipt, item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="alice", display_name="Alice"),
        audience=("engineering",),
        key="gate-failure",
        text="Create a broken link.",
        plan=invalid_plan,
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["plan_rejected"] is True
    assert item["report"]["wiki_changes"] == 0
    after = subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip()
    assert after != before
    assert subprocess.run(
        ["git", "cat-file", "-e", "main:wiki/notes/Broken page.md"],
        cwd=target_repo,
        check=False,
    ).returncode != 0
    changes = list_changes(clean_queue)
    assert len(changes) == 1
    assert [entry.page_role for entry in changes[0].manifest] == ["source"]


def test_repair_writer_reconstructs_the_authorized_page_metadata(tmp_path):
    path = "wiki/concepts/Repair boundary.md"
    target = tmp_path.joinpath(*path.split("/"))
    target.parent.mkdir(parents=True)
    target.write_text(
        render_page(
            path=path,
            role="concept",
            title="Repair boundary",
            body="# Repair boundary\n\nThe original body.",
            acl=("engineering",),
            sources=("sources/2026/09/repair-boundary.md",),
            status="evergreen",
            page_id="page_repair_boundary",
            created=dt.date(2026, 9, 1),
            updated=dt.date(2026, 9, 2),
        ),
        encoding="utf-8",
    )
    before = parse_page(path, target.read_text(encoding="utf-8"))

    changed = writer._apply_repair_plan(
        str(tmp_path),
        (writer.Violation(path, "placeholder-knowledge-body", "repair it"),),
        RepairPlan(
            summary="Replaced the incomplete body.",
            mutations=(
                RepairMutation(
                    path=path,
                    body="# Repair boundary\n\nThe repaired body.",
                    reason="Restored the durable conclusion.",
                ),
            ),
        ),
        authorized_files={path: before.body},
        context=WriteContext(None, ("engineering",), unrestricted=True),
        existing_paths=frozenset({path}),
    )

    after = parse_page(path, target.read_text(encoding="utf-8"))
    assert changed == {path: "Restored the durable conclusion."}
    assert after.body == "# Repair boundary\n\nThe repaired body."
    assert (
        after.role,
        after.title,
        after.acl,
        after.entity_type,
        after.aliases,
        after.sources,
        after.status,
        after.page_id,
        after.created,
        after.updated,
    ) == (
        before.role,
        before.title,
        before.acl,
        before.entity_type,
        before.aliases,
        before.sources,
        before.status,
        before.page_id,
        before.created,
        before.updated,
    )


def test_capture_repair_refuses_an_out_of_scope_violation_before_reading_files(
    clean_queue, target_repo
):
    hidden = "wiki/notes/Finance repair target.md"
    target = target_repo / hidden
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_page(
            path=hidden,
            role="note",
            title="Finance repair target",
            body="# Finance repair target\n\nRestricted evidence.",
            acl=("finance",),
            created=dt.date(2026, 8, 24),
            updated=dt.date(2026, 8, 24),
        ),
        encoding="utf-8",
    )
    before = {hidden: target.read_bytes()}
    context = WriteContext(
        actor_groups=frozenset({"engineering"}),
        content_acl=("engineering",),
        unrestricted=False,
    )

    with pytest.raises(writer.GateRefused, match="outside this capture"):
        writer._capture_repair_files(
            str(target_repo),
            violations=(writer.Violation(hidden, "placeholder-knowledge-body", "repair it"),),
            editorial_paths=frozenset({"wiki/notes/Visible repair target.md"}),
            snapshot=before,
            context=context,
        )


def test_master_recompile_preserves_sources_reuses_entity_page_identity_and_is_idempotent(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    entity_path = "wiki/entities/Acme.md"
    seed_plan = FilingPlan(
        summary="Recorded the Acme organization",
        mutations=(
            PageMutation(
                action="create",
                role="entity",
                title="Acme",
                entity_type="organization",
                body="# Acme\n\nAcme is an organization. Acme owns an operating decision.",
                reason="The source identifies Acme",
            ),
        ),
    )
    recompiled_plan = FilingPlan(
        summary="Recompiled the source into richer entity knowledge",
        mutations=(
            PageMutation(
                action="create",
                role="entity",
                title="Acme",
                entity_type="organization",
                aliases=("Acme CRM account",),
                body=(
                    "# Acme\n\nAcme is the CRM organization for account account-7. "
                    "Acme owns a durable operating decision."
                ),
                reason="The source identifies Acme and its CRM account",
            ),
        ),
    )
    receipt, _item, outcome = _process_capture(
        clean_queue, target_repo, store,
        actor=Actor(subject="morgan", display_name="Morgan"), audience=None,
        key="recompile-seed", text="Acme CRM account account-7 owns a durable operating decision.", plan=seed_plan,
        editorial=True,
    )
    assert outcome.status == schema.LANDED
    source = source_path(schema.parse_capture(receipt["request"]))
    source_before = _show(target_repo, source)
    entity_before = parse_page(entity_path, _show(target_repo, entity_path))
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    queue.enqueue_garden(
        clean_queue,
        schema.GardenRequest(
            idempotency_key="recompile-derived-knowledge",
            actor=Actor(subject="morgan", display_name="Morgan"),
            rationale="Apply the current graph compiler to immutable evidence", mode="recompile",
        ),
    )

    item, outcome = worker.process_next(
        clean_queue,
        WriterDeps(settings, store, EditorialFixturePlanner(recompiled_plan), str(target_repo)),
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["operation"] == "recompile"
    assert item["report"]["source_count"] == 1
    assert not {key for key in item["report"] if key.endswith("_entities")}
    assert source_before == _show(target_repo, source)
    entity_after = parse_page(entity_path, _show(target_repo, entity_path))
    assert (entity_after.page_id, entity_after.created) == (entity_before.page_id, entity_before.created)
    assert entity_after.sources == (source,)
    assert entity_after.aliases == ("Acme CRM account",)
    assert "Acme is the CRM organization for account account-7." in entity_after.body
    assert "Acme owns a durable operating decision." in entity_after.body
    assert "Acme is an organization." not in entity_after.body
    assert "Acme owns an operating decision." not in entity_after.body

    queue.enqueue_garden(
        clean_queue,
        schema.GardenRequest(
            idempotency_key="recompile-derived-knowledge-again",
            actor=Actor(subject="morgan", display_name="Morgan"),
            rationale="Verify deterministic recompilation", mode="recompile",
        ),
    )
    repeated, repeated_outcome = worker.process_next(
        clean_queue,
        WriterDeps(settings, store, EditorialFixturePlanner(recompiled_plan), str(target_repo)),
    )

    assert repeated_outcome.status == schema.LANDED
    assert repeated["report"]["idempotent"] is True
    assert repeated["commit_sha"] == ""


def test_master_recompile_rejects_empty_plan_that_would_delete_sole_derived_page(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    page_path = "wiki/notes/Recompile preservation.md"
    seed = FilingPlan(
        summary="Recorded durable knowledge",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Recompile preservation",
                body=(
                    "# Recompile preservation\n\n"
                    "The operating decision remains durable knowledge."
                ),
                reason="The source records a durable operating decision",
            ),
        ),
    )
    receipt, _item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="morgan", display_name="Morgan"),
        audience=None,
        key="recompile-preservation-seed",
        text="The operating decision remains durable knowledge.",
        plan=seed,
        editorial=True,
    )
    assert outcome.status == schema.LANDED
    source = source_path(schema.parse_capture(receipt["request"]))
    before = subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip()
    page_before = subprocess.check_output(
        ["git", "show", f"main:{page_path}"], cwd=target_repo, text=True
    )

    queue.enqueue_garden(
        clean_queue,
        schema.GardenRequest(
            idempotency_key="recompile-preservation-empty",
            actor=Actor(subject="morgan", display_name="Morgan"),
            rationale="Recompile without silently dropping durable knowledge",
            mode="recompile",
        ),
    )
    _item, failed = worker.process_next(
        clean_queue,
        WriterDeps(
            config.Settings(repo=str(target_repo), branch="main", backend="scripted"),
            store,
            ScriptedPlanner(FilingPlan(summary="Found no new durable knowledge")),
            str(target_repo),
        ),
    )

    assert failed.status == schema.FAILED
    assert subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip() == before
    assert subprocess.check_output(
        ["git", "show", f"main:{page_path}"], cwd=target_repo, text=True
    ) == page_before
    assert subprocess.run(
        ["git", "cat-file", "-e", f"main:{source}"],
        cwd=target_repo,
        check=False,
    ).returncode == 0


def test_master_recompile_accepts_explicit_tombstone_for_prior_derived_page(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    page_path = "wiki/notes/Obsolete recompile knowledge.md"
    seed = FilingPlan(
        summary="Recorded knowledge that later becomes obsolete",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Obsolete recompile knowledge",
                body="# Obsolete recompile knowledge\n\nThe old procedure remains in force.",
                reason="The source records the old procedure",
            ),
        ),
    )
    receipt, _item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="morgan", display_name="Morgan"),
        audience=None,
        key="recompile-tombstone-seed",
        text="The old procedure remains in force.",
        plan=seed,
        editorial=True,
    )
    assert outcome.status == schema.LANDED
    source = source_path(schema.parse_capture(receipt["request"]))
    plan = FilingPlan(
        summary="Retired obsolete knowledge explicitly",
        mutations=(
            PageMutation(
                action="delete",
                path=page_path,
                reason="The source no longer supports a durable current procedure",
            ),
        ),
    )
    queue.enqueue_garden(
        clean_queue,
        schema.GardenRequest(
            idempotency_key="recompile-tombstone",
            actor=Actor(subject="morgan", display_name="Morgan"),
            rationale="Retire obsolete derived knowledge explicitly",
            mode="recompile",
        ),
    )

    item, recompiled = worker.process_next(
        clean_queue,
        WriterDeps(
            config.Settings(repo=str(target_repo), branch="main", backend="scripted"),
            store,
            ScriptedPlanner(plan),
            str(target_repo),
        ),
    )

    assert recompiled.status == schema.LANDED
    assert item["report"]["authorized_page_removals"] == 1
    assert subprocess.run(
        ["git", "cat-file", "-e", f"main:{page_path}"],
        cwd=target_repo,
        check=False,
    ).returncode != 0
    assert subprocess.run(
        ["git", "cat-file", "-e", f"main:{source}"],
        cwd=target_repo,
        check=False,
    ).returncode == 0


def test_master_recompile_requires_tombstone_when_consolidating_prior_page(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    old_path = "wiki/notes/Legacy operating guide.md"
    seed = FilingPlan(
        summary="Recorded the legacy guide",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Legacy operating guide",
                body="# Legacy operating guide\n\nThe guide records the durable operating method.",
                reason="The source records the operating method",
            ),
        ),
    )
    _receipt, _item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="morgan", display_name="Morgan"),
        audience=None,
        key="recompile-consolidation-seed",
        text="The guide records the durable operating method.",
        plan=seed,
        editorial=True,
    )
    assert outcome.status == schema.LANDED
    plan = FilingPlan(
        summary="Consolidated the legacy guide into the current method",
        mutations=(
            PageMutation(
                action="create",
                role="concept",
                title="Current operating method",
                body="# Current operating method\n\nThe durable operating method is consolidated here.",
                reason="This page is the durable replacement",
            ),
            PageMutation(
                action="delete",
                path=old_path,
                reason="Consolidated into wiki/concepts/Current operating method.md",
            ),
        ),
    )
    queue.enqueue_garden(
        clean_queue,
        schema.GardenRequest(
            idempotency_key="recompile-consolidation",
            actor=Actor(subject="morgan", display_name="Morgan"),
            rationale="Consolidate prior derived knowledge explicitly",
            mode="recompile",
        ),
    )

    item, recompiled = worker.process_next(
        clean_queue,
        WriterDeps(
            config.Settings(repo=str(target_repo), branch="main", backend="scripted"),
            store,
            EditorialFixturePlanner(plan),
            str(target_repo),
        ),
    )

    assert recompiled.status == schema.LANDED
    assert item["report"]["authorized_page_removals"] == 1
    tree = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "main"], cwd=target_repo, text=True
    ).splitlines()
    assert old_path not in tree
    assert "wiki/concepts/Current operating method.md" in tree


def test_master_recompile_allows_empty_plan_for_source_without_prior_derived_page(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    empty = FilingPlan(summary="The source has no durable graph contribution")
    _receipt, _item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="morgan", display_name="Morgan"),
        audience=None,
        key="recompile-source-only-seed",
        text="A transient observation with no durable conclusion.",
        plan=empty,
    )
    assert outcome.status == schema.LANDED
    before = subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip()
    queue.enqueue_garden(
        clean_queue,
        schema.GardenRequest(
            idempotency_key="recompile-source-only-empty",
            actor=Actor(subject="morgan", display_name="Morgan"),
            rationale="Keep a source-only capture source-only",
            mode="recompile",
        ),
    )

    item, recompiled = worker.process_next(
        clean_queue,
        WriterDeps(
            config.Settings(repo=str(target_repo), branch="main", backend="scripted"),
            store,
            ScriptedPlanner(empty),
            str(target_repo),
        ),
    )

    assert recompiled.status == schema.LANDED
    assert item["commit_sha"] == ""
    assert item["report"]["idempotent"] is True
    assert item["report"]["authorized_page_removals"] == 0
    assert subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip() == before


def test_recompile_records_one_garden_ledger_row_under_previous_trigger_constraint(
    clean_queue, target_repo, monkeypatch
):
    def derive_one_page(worktree, _deps, *, rationale):
        path = Path(worktree, "wiki", "notes", "Recompiled evidence.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_page(
                path="wiki/notes/Recompiled evidence.md",
                role="note",
                title="Recompiled evidence",
                body="# Recompiled evidence\n\nThis is regenerated derived knowledge.",
                acl=None,
                created=dt.date(2026, 8, 24),
                updated=dt.date(2026, 8, 24),
            ),
            encoding="utf-8",
        )
        return {
            "operation": "recompile",
            "rationale": rationale,
            "source_count": 1,
            "page_mutations": 1,
            "model_requests": 0,
            "link_health": {"violations": 0, "clean": True},
            "idempotent": False,
        }

    monkeypatch.setattr(writer, "_recompile_layers", derive_one_page)
    with clean_queue.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE knowledge_changes DROP CONSTRAINT knowledge_changes_trigger_check"
        )
        cursor.execute(
            """
            ALTER TABLE knowledge_changes
            ADD CONSTRAINT knowledge_changes_trigger_check
            CHECK (trigger IN ('capture', 'garden', 'delete', 'contradiction_resolution', 'entity'))
            """
        )
        cursor.execute(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'knowledge_changes'::regclass
              AND conname = 'knowledge_changes_trigger_check'
            """
        )
        trigger_constraint = cursor.fetchone()[0]
    assert "'garden'" in trigger_constraint
    assert "recompile" not in trigger_constraint

    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    deps = WriterDeps(settings, evidence.MemoryEvidenceStore(), ScriptedPlanner(FilingPlan(
        summary="Unused recompile planner fixture"
    )), str(target_repo))
    request = schema.GardenRequest(
        idempotency_key="recompile-ledger-compatibility",
        actor=Actor(subject="morgan", display_name="Morgan"),
        rationale="Record one compiled change under the historical ledger constraint",
        mode="recompile",
    )
    queue.enqueue_garden(clean_queue, request)
    item = queue.claim_next(
        clean_queue,
        operation_budget_s=config.operation_budget_s(timeout_s=settings.timeout_s),
    )
    commits_before = int(
        subprocess.check_output(
            ["git", "rev-list", "--count", "main"], cwd=target_repo, text=True
        ).strip()
    )

    result = writer._recompile(
        clean_queue,
        item,
        deps,
        writer.gitcmd.base_ref(str(target_repo), "main"),
        request=request,
    )

    assert result.report["operation"] == "recompile"
    assert int(
        subprocess.check_output(
            ["git", "rev-list", "--count", "main"], cwd=target_repo, text=True
        ).strip()
    ) == commits_before + 1
    changes = [change for change in list_changes(clean_queue) if change.commit_sha == result.commit_sha]
    assert len(changes) == 1
    assert changes[0].trigger == "garden"


def test_master_recompile_refuses_an_oversized_source_before_any_derived_mutation(
    clean_queue, target_repo, monkeypatch
):
    store = evidence.MemoryEvidenceStore()
    valid = FilingPlan(
        summary="Recorded a durable conclusion",
        mutations=(
            PageMutation(
                action="create", role="note", title="Stable recompile baseline",
                body="# Stable recompile baseline\n\nThe source records a durable operating conclusion.",
                reason="Baseline.",
            ),
        ),
    )
    _receipt, _item, outcome = _process_capture(
        clean_queue, target_repo, store,
        actor=Actor(subject="morgan", display_name="Morgan"), audience=None,
        key="recompile-rollback-seed", text="The source records a durable operating conclusion.",
        plan=valid, editorial=True,
    )
    assert outcome.status == schema.LANDED
    before = subprocess.check_output(["git", "rev-parse", "main"], cwd=target_repo, text=True).strip()
    monkeypatch.setattr(knowledge_sources, "MAX_CAPTURE_RENDERED_SOURCE_BYTES", 1)
    queue.enqueue_garden(
        clean_queue,
        schema.GardenRequest(
            idempotency_key="recompile-rollback", actor=Actor(subject="morgan", display_name="Morgan"),
            rationale="Reject an oversized source before a complete rebuild", mode="recompile",
        ),
    )

    _item, failed = worker.process_next(
        clean_queue,
        WriterDeps(
            config.Settings(repo=str(target_repo), branch="main", backend="scripted"),
            store, EditorialFixturePlanner(valid), str(target_repo),
        ),
    )

    assert failed.status == schema.FAILED
    assert subprocess.check_output(["git", "rev-parse", "main"], cwd=target_repo, text=True).strip() == before


def test_crash_after_commit_reconciles_without_a_second_commit(
    clean_queue, target_repo, monkeypatch
):
    store = evidence.MemoryEvidenceStore()
    CaptureService(clean_queue, store).capture_text(
        actor=Actor(subject="alice", display_name="Alice"),
        audience=("engineering",),
        adapter="mcp",
        text="A durable crash-recovery decision.",
        idempotency_key="crash-recovery",
    )
    plan = FilingPlan(
        summary="Recorded crash recovery",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Crash recovery",
                body="# Crash recovery\n\nThe decision survives acknowledgement loss.",
                reason="The decision is durable",
            ),
        ),
    )
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    deps = WriterDeps(settings, store, ScriptedPlanner(plan), str(target_repo))
    original_finish = queue.finish_landed
    monkeypatch.setattr(
        queue,
        "finish_landed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            QueueStateError("simulated acknowledgement loss")
        ),
    )

    with pytest.raises(QueueStateError, match="acknowledgement loss"):
        worker.process_next(clean_queue, deps)

    assert subprocess.check_output(
        ["git", "rev-list", "--count", "main"], cwd=target_repo, text=True
    ).strip() == "2"
    assert len(list_changes(clean_queue)) == 1
    monkeypatch.setattr(queue, "finish_landed", original_finish)
    with clean_queue.cursor() as cursor:
        cursor.execute(
            "UPDATE capture_queue SET status = 'queued', processing_started_at = NULL, "
            "next_attempt_at = now() WHERE idempotency_key = 'crash-recovery'"
        )

    item, outcome = worker.process_next(clean_queue, deps)

    assert outcome.status == schema.LANDED
    assert item["report"]["reconciled"] is True
    assert subprocess.check_output(
        ["git", "rev-list", "--count", "main"], cwd=target_repo, text=True
    ).strip() == "2"
    assert len(list_changes(clean_queue)) == 1


def test_change_record_failure_after_commit_retries_and_reconciles_once(
    clean_queue, target_repo, monkeypatch
):
    store = evidence.MemoryEvidenceStore()
    CaptureService(clean_queue, store).capture_text(
        actor=Actor(subject="alice", display_name="Alice"),
        audience=("engineering",),
        adapter="mcp",
        text="A durable change-ledger recovery decision.",
        idempotency_key="change-record-recovery",
    )
    plan = FilingPlan(
        summary="Recorded change-ledger recovery",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Change ledger recovery",
                body="# Change ledger recovery\n\nThe commit remains recoverable.",
                reason="The source establishes the recovery invariant",
            ),
        ),
    )
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    deps = WriterDeps(settings, store, ScriptedPlanner(plan), str(target_repo))
    remote = target_repo.parent / "writer-remote.git"
    subprocess.run(["git", "init", "--bare", "-q", remote], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=target_repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=target_repo, check=True)
    original_record_change = writer.record_change
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ChangeError("git could not construct the change record")
        return original_record_change(*args, **kwargs)

    monkeypatch.setattr(writer, "record_change", fail_once)

    first, first_outcome = worker.process_next(clean_queue, deps)

    assert first_outcome.status == schema.QUEUED
    assert first["error_category"] == "ChangeError"
    assert subprocess.check_output(
        ["git", "--git-dir", str(remote), "rev-list", "--count", "main"], text=True
    ).strip() == "2"
    assert list_changes(clean_queue) == []
    with clean_queue.cursor() as cursor:
        cursor.execute(
            "UPDATE capture_queue SET next_attempt_at = now() WHERE id = %s",
            (first["id"],),
        )

    item, outcome = worker.process_next(clean_queue, deps)

    assert outcome.status == schema.LANDED
    assert item["report"]["reconciled"] is True
    assert subprocess.check_output(
        ["git", "--git-dir", str(remote), "rev-list", "--count", "main"], text=True
    ).strip() == "2"
    changes = list_changes(clean_queue)
    assert len(changes) == 1
    assert changes[0].commit_sha == item["commit_sha"]


def test_explicit_delete_sweeps_page_and_source_references_in_one_commit(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="morgan", display_name="Morgan")
    seed_plan = FilingPlan(
        summary="Created linked knowledge for deletion",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Delete target",
                body="# Delete target\n\nObsolete detail.",
                reason="Deletion fixture target",
            ),
            PageMutation(
                action="create",
                role="note",
                title="Keep page",
                body="# Keep page\n\nSee [[Delete target]] for obsolete detail.",
                reason="Deletion fixture referrer",
            ),
        ),
    )
    receipt, item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="delete-seed",
        text="Obsolete detail with a retained referrer.",
        plan=seed_plan,
        editorial=True,
    )
    assert outcome.status == schema.LANDED
    source = item["source_path"]
    original_ref = receipt["request"]["artifacts"][0]["blob_ref"]
    before_count = int(
        subprocess.check_output(
            ["git", "rev-list", "--count", "main"],
            cwd=target_repo,
            text=True,
        ).strip()
    )
    request = schema.DeleteRequest(
        idempotency_key="delete-page-and-source",
        actor=actor,
        paths=("wiki/notes/Delete target.md", source),
        rationale="The evidence and obsolete page must be removed",
    )
    queue.enqueue_delete(clean_queue, request)
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")

    deleted, delete_outcome = worker.process_next(
        clean_queue,
        WriterDeps(settings, store, ScriptedPlanner(), str(target_repo)),
    )

    assert delete_outcome.status == schema.LANDED
    assert int(
        subprocess.check_output(
            ["git", "rev-list", "--count", "main"],
            cwd=target_repo,
            text=True,
        ).strip()
    ) == before_count + 1
    tree = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "main"],
        cwd=target_repo,
        text=True,
    ).splitlines()
    assert "wiki/notes/Delete target.md" not in tree
    assert source not in tree
    kept_text = subprocess.check_output(
        ["git", "show", "main:wiki/notes/Keep page.md"],
        cwd=target_repo,
        text=True,
    )
    kept = parse_page("wiki/notes/Keep page.md", kept_text)
    assert "[[Delete target]]" not in kept.body
    assert source not in kept.sources
    assert original_ref not in store.objects
    change = list_changes(clean_queue)[0]
    assert change.trigger == "delete"
    assert change.commit_sha == deleted["commit_sha"]


def test_explicit_delete_of_an_entity_page_sweeps_its_links_to_prose(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="morgan", display_name="Morgan")
    seed_plan = FilingPlan(
        summary="Created an entity page and a note linking it",
        mutations=(
            PageMutation(
                action="create",
                role="entity",
                title="Acme Legacy",
                entity_type="organization",
                body="# Acme Legacy\n\nThe legacy account entity.",
                reason="The source identifies the account",
            ),
            PageMutation(
                action="create",
                role="note",
                title="Acme account",
                body="# Acme account\n\nThe [[Acme Legacy]] account knowledge remains substantive.",
                reason="The source describes the account",
            ),
        ),
    )
    _receipt, seed_item, seed_outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="entity-delete-seed",
        text="Acme Legacy is the account name.",
        plan=seed_plan,
        editorial=True,
    )
    assert seed_outcome.status == schema.LANDED
    source = seed_item["source_path"]
    queue.enqueue_delete(
        clean_queue,
        schema.DeleteRequest(
            idempotency_key="delete-entity-page",
            actor=actor,
            paths=("wiki/entities/Acme Legacy.md",),
            rationale="The identity is not durable",
        ),
    )
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")

    deleted, delete_outcome = worker.process_next(
        clean_queue,
        WriterDeps(settings, store, ScriptedPlanner(), str(target_repo)),
    )

    assert delete_outcome.status == schema.LANDED
    tree = _tree(target_repo)
    assert "wiki/entities/Acme Legacy.md" not in tree
    assert source in tree
    note = parse_page("wiki/notes/Acme account.md", _show(target_repo, "wiki/notes/Acme account.md"))
    assert "[[" not in note.body
    assert "The Acme Legacy account knowledge remains substantive." in note.body
    assert note.sources == (source,)
    change = list_changes(clean_queue)[0]
    assert change.trigger == "delete"
    assert change.commit_sha == deleted["commit_sha"]
    assert _lint_main(target_repo) == ()


def test_entity_rename_merge_and_delete_use_atomic_writer_operations(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="morgan", display_name="Morgan")
    duplicate_assertion = (
        "Northstar Labs and Northstar Research are duplicate CRM identities."
    )
    seed_plan = FilingPlan(
        summary="Created two identified organizations",
        mutations=(
            PageMutation(
                action="create",
                role="entity",
                title="Northstar Labs",
                entity_type="organization",
                aliases=("NL",),
                body="# Northstar Labs\n\nThe laboratory arm of the Northstar account.",
                reason="The source identifies the laboratory",
            ),
            PageMutation(
                action="create",
                role="entity",
                title="Northstar Research",
                entity_type="organization",
                body="# Northstar Research\n\nThe research arm of the Northstar account.",
                reason="The source identifies the research arm",
            ),
            PageMutation(
                action="create",
                role="note",
                title="Northstar account",
                body=(
                    "# Northstar account\n\n"
                    "[[Northstar Labs]] and [[Northstar Research]] remain substantive accounts."
                ),
                reason="The source describes both records",
            ),
        ),
    )
    _receipt, seed_item, seed_outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="entity-pair",
        text=duplicate_assertion,
        plan=seed_plan,
        editorial=True,
    )
    assert seed_outcome.status == schema.LANDED
    source = seed_item["source_path"]
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")

    def run(request):
        queue.enqueue_entity_operation(clean_queue, request)
        return worker.process_next(
            clean_queue,
            WriterDeps(settings, store, ScriptedPlanner(), str(target_repo)),
        )

    def commits():
        return int(
            subprocess.check_output(
                ["git", "rev-list", "--count", "main"], cwd=target_repo, text=True
            ).strip()
        )

    before = commits()
    renamed_item, renamed_outcome = run(
        schema.EntityOperationRequest(
            idempotency_key="rename-northstar",
            actor=actor,
            action="rename",
            entities=("wiki/entities/Northstar Labs.md",),
            new_title="Northstar Systems",
            rationale="The laboratory renamed itself",
        )
    )

    assert renamed_outcome.status == schema.LANDED
    assert commits() == before + 1
    tree = _tree(target_repo)
    assert "wiki/entities/Northstar Labs.md" not in tree
    assert "wiki/entities/Northstar Systems.md" in tree
    renamed = parse_page("wiki/entities/Northstar Systems.md", _show(target_repo, "wiki/entities/Northstar Systems.md"))
    assert (renamed.entity_type, renamed.aliases) == ("organization", ("NL", "Northstar Labs"))
    assert "[[Northstar Systems|Northstar Labs]] and [[Northstar Research]]" in _show(
        target_repo, "wiki/notes/Northstar account.md"
    )
    change = list_changes(clean_queue)[0]
    assert change.trigger == "entity"
    assert change.commit_sha == renamed_item["commit_sha"]

    refused_item, refused_outcome = run(
        schema.EntityOperationRequest(
            idempotency_key="rename-onto-claimed-name",
            actor=actor,
            action="rename",
            entities=("wiki/entities/Northstar Research.md",),
            new_title="Northstar Labs",
            rationale="Reuse a name another entity page still answers to",
        )
    )

    assert refused_outcome.status == schema.FAILED
    assert refused_item["error_category"] == "GateRefused"
    assert commits() == before + 1

    merged_item, merged_outcome = run(
        schema.EntityOperationRequest(
            idempotency_key="merge-northstar",
            actor=actor,
            action="merge",
            entities=("wiki/entities/Northstar Systems.md", "wiki/entities/Northstar Research.md"),
            rationale="CRM evidence confirms the duplicate identity",
            evidence=schema.EntityMergeEvidence(
                source_assertions=(
                    schema.SourceMergeAssertion(path=source, assertion=duplicate_assertion),
                ),
            ),
        )
    )

    assert merged_outcome.status == schema.LANDED
    assert commits() == before + 2
    assert "wiki/entities/Northstar Research.md" not in _tree(target_repo)
    merged = parse_page("wiki/entities/Northstar Systems.md", _show(target_repo, "wiki/entities/Northstar Systems.md"))
    assert merged.aliases == ("NL", "Northstar Labs", "Northstar Research")
    assert merged.sources == (source,)
    assert "## Formerly Northstar Research" in merged.body
    assert "The research arm of the Northstar account." in merged.body
    account = _show(target_repo, "wiki/notes/Northstar account.md")
    assert "[[Northstar Systems|Northstar Labs]] and [[Northstar Systems|Northstar Research]]" in account
    assert merged_item["report"]["reason"].startswith(f"Verified by source assertion {source}.")
    assert list_changes(clean_queue)[0].commit_sha == merged_item["commit_sha"]
    assert _lint_main(target_repo) == ()

    deleted_item, deleted_outcome = run(
        schema.EntityOperationRequest(
            idempotency_key="delete-northstar",
            actor=actor,
            action="delete",
            entities=("wiki/entities/Northstar Systems.md",),
            rationale="The identity must be removed while retaining substantive knowledge",
        )
    )

    assert deleted_outcome.status == schema.LANDED
    assert commits() == before + 3
    assert "wiki/entities/Northstar Systems.md" not in _tree(target_repo)
    account_path = "wiki/notes/Northstar account.md"
    page_after_delete = parse_page(account_path, _show(target_repo, account_path))
    assert "[[" not in page_after_delete.body
    assert "Northstar Labs and Northstar Research remain substantive accounts." in page_after_delete.body
    assert list_changes(clean_queue)[0].commit_sha == deleted_item["commit_sha"]
    assert _lint_main(target_repo) == ()


def test_contradiction_and_resolution_are_ordinary_atomic_captures(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    service = CaptureService(clean_queue, store)
    actor = Actor(subject="morgan", display_name="Morgan")
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")

    first = service.capture_text(
        actor=actor,
        audience=None,
        adapter="mcp",
        text="The signed agreement says the renewal is annual.",
        idempotency_key="contradiction-first",
        captured_at=dt.datetime(2026, 8, 20, tzinfo=dt.UTC),
    )
    first_source = source_path(schema.parse_capture(first["request"]))
    first_plan = FilingPlan(
        summary="Recorded the annual renewal claim",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Renewal cadence",
                body="# Renewal cadence\n\nThe signed agreement states an annual renewal.",
                reason="The signed agreement establishes the initial cadence",
            ),
        ),
    )
    worker.process_next(
        clean_queue,
            WriterDeps(settings, store, EditorialFixturePlanner(first_plan), str(target_repo)),
    )

    second = service.capture_text(
        actor=actor,
        audience=None,
        adapter="mcp",
        text="The current billing schedule says the renewal is monthly.",
        idempotency_key="contradiction-second",
        captured_at=dt.datetime(2026, 8, 21, tzinfo=dt.UTC),
    )
    second_source = source_path(schema.parse_capture(second["request"]))
    second_plan = FilingPlan(
        summary="Preserved conflicting renewal evidence",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/notes/Renewal cadence.md",
                body="# Renewal cadence\n\nThe signed agreement and billing schedule disagree.",
                reason="The current sources conflict",
            ),
        ),
        contradictions=(
            ContradictionProposal(
                page_path="wiki/notes/Renewal cadence.md",
                explanation="Two current sources state incompatible renewal cadences.",
                claims=(
                    ContradictionClaim(
                        text="The renewal is annual.",
                        source=first_source,
                        date="2026-08-20",
                    ),
                    ContradictionClaim(
                        text="The renewal is monthly.",
                        source=second_source,
                        date="2026-08-21",
                    ),
                ),
            ),
        ),
    )
    item, outcome = worker.process_next(
        clean_queue,
            WriterDeps(settings, store, EditorialFixturePlanner(second_plan), str(target_repo)),
    )

    assert outcome.status == schema.LANDED
    page_text = subprocess.check_output(
        ["git", "show", "main:wiki/notes/Renewal cadence.md"],
        cwd=target_repo,
        text=True,
    )
    page = parse_page("wiki/notes/Renewal cadence.md", page_text)
    located = contradictions.parse_all(page.body)
    assert page.updated == dt.date(2026, 8, 21)
    assert set(page.sources) == {first_source, second_source}
    assert len(located) == 1
    assert item["report"]["plan_rejected"] is False
    assert item["report"]["plan_rejection"] == ""
    contradiction_id = located[0].record.contradiction_id

    resolution_plan = FilingPlan(
        summary="Resolved the renewal contradiction with signed evidence",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/notes/Renewal cadence.md",
                body="# Renewal cadence\n\nThe renewal is annual under the signed agreement.",
                reason="New signed evidence confirms the annual cadence",
            ),
        ),
        resolved_contradictions=(contradiction_id,),
    )
    service.capture_text(
        actor=actor,
        audience=None,
        adapter="admin",
        text="Signed confirmation: the renewal is annual.",
        idempotency_key="contradiction-resolution",
        captured_at=dt.datetime(2026, 8, 22, tzinfo=dt.UTC),
        intent=schema.CaptureIntent(
            resolution_of=contradiction_id,
            rationale="A signed confirmation settles the cadence.",
        ),
    )
    resolved_item, resolved_outcome = worker.process_next(
        clean_queue,
            WriterDeps(settings, store, EditorialFixturePlanner(resolution_plan), str(target_repo)),
    )

    assert resolved_outcome.status == schema.LANDED
    resolved_text = subprocess.check_output(
        ["git", "show", "main:wiki/notes/Renewal cadence.md"],
        cwd=target_repo,
        text=True,
    )
    resolved_page = parse_page("wiki/notes/Renewal cadence.md", resolved_text)
    assert resolved_page.updated == dt.date(2026, 8, 22)
    assert contradictions.parse_all(resolved_page.body) == ()
    changes = list_changes(clean_queue)
    assert {change.trigger for change in changes} == {"capture", "contradiction_resolution"}
    assert item["change_id"] and resolved_item["change_id"]


def test_resolution_without_a_prose_mutation_preserves_the_visible_marker(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    service = CaptureService(clean_queue, store)
    master = Actor(subject="morgan", display_name="Morgan")
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")

    seed = service.capture_text(
        actor=master,
        audience=None,
        adapter="mcp",
        text="Two public policies state incompatible retention periods.",
        idempotency_key="resolution-without-prose-seed",
        captured_at=dt.datetime(2026, 8, 20, tzinfo=dt.UTC),
    )
    seed_source = source_path(schema.parse_capture(seed["request"]))
    worker.process_next(
        clean_queue,
        WriterDeps(
            settings,
            store,
            EditorialFixturePlanner(
                FilingPlan(
                    summary="Preserved a public retention contradiction",
                    mutations=(
                        PageMutation(
                            action="create",
                            role="note",
                            title="Public retention",
                            body="# Public retention\n\nThe policies disagree.",
                            reason="Both policies remain credible",
                        ),
                    ),
                    contradictions=(
                        ContradictionProposal(
                            page_path="wiki/notes/Public retention.md",
                            explanation="Two public policies disagree.",
                            claims=(
                                ContradictionClaim(
                                    text="Retention is 30 days.",
                                    source=seed_source,
                                    date="2026-08-20",
                                ),
                                ContradictionClaim(
                                    text="Retention is 60 days.",
                                    source=seed_source,
                                    date="2026-08-20",
                                ),
                            ),
                        ),
                    ),
                )
            ),
            str(target_repo),
        ),
    )
    before = parse_page(
        "wiki/notes/Public retention.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Public retention.md"],
            cwd=target_repo,
            text=True,
        ),
    )
    contradiction_id = contradictions.parse_all(before.body)[0].record.contradiction_id

    service.capture_text(
        actor=master,
        audience=None,
        adapter="admin",
        text="The signed public policy confirms that 30 days controls.",
        idempotency_key="resolution-without-prose",
        captured_at=dt.datetime(2026, 8, 22, tzinfo=dt.UTC),
        intent=schema.CaptureIntent(
            resolution_of=contradiction_id,
            rationale="The signed public policy controls.",
        ),
    )
    item, outcome = worker.process_next(
        clean_queue,
        WriterDeps(
            settings,
            store,
            ScriptedPlanner(
                FilingPlan(
                    summary="Resolved the public retention contradiction",
                    resolved_contradictions=(contradiction_id,),
                )
            ),
            str(target_repo),
        ),
    )

    after = parse_page(
        "wiki/notes/Public retention.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Public retention.md"],
            cwd=target_repo,
            text=True,
        ),
    )
    assert outcome.status == schema.LANDED
    assert item["report"]["wiki_changes"] == 0
    assert item["report"]["plan_rejected"] is True
    assert item["report"]["plan_skipped"] == []
    assert len(contradictions.parse_all(after.body)) == 1
    assert after.sources == (seed_source,)


def test_invalid_contradiction_proposal_cannot_block_a_valid_capture(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="morgan", display_name="Morgan")
    _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="contradiction-target",
        text="The account currently renews annually.",
        plan=FilingPlan(
            summary="Recorded the renewal cadence",
            mutations=(
                PageMutation(
                    action="create",
                    role="note",
                    title="Account renewal",
                    body="# Account renewal\n\nThe account currently renews annually.",
                    reason="The source establishes the cadence",
                ),
            ),
        ),
        editorial=True,
    )
    receipt = CaptureService(clean_queue, store).capture_text(
        actor=actor,
        audience=None,
        adapter="mcp",
        text="A second observation was submitted.",
        idempotency_key="invalid-contradiction-source",
    )
    current_source = source_path(schema.parse_capture(receipt["request"]))
    missing_source = "sources/2026/08/ffffffff-ffff-4fff-8fff-ffffffffffff.md"
    plan = FilingPlan(
        summary="Archived evidence without a supportable contradiction",
        contradictions=(
            ContradictionProposal(
                page_path="wiki/notes/Account renewal.md",
                explanation="The proposed contradiction cites missing evidence.",
                claims=(
                    ContradictionClaim(
                        text="The account renews annually.",
                        source=current_source,
                    ),
                    ContradictionClaim(
                        text="The account renews monthly.",
                        source=missing_source,
                    ),
                ),
            ),
        ),
    )
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")

    item, outcome = worker.process_next(
        clean_queue,
        WriterDeps(settings, store, ScriptedPlanner(plan), str(target_repo)),
    )

    assert outcome.status == schema.LANDED
    assert item["source_path"] == current_source
    page = parse_page(
        "wiki/notes/Account renewal.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Account renewal.md"],
            cwd=target_repo,
            text=True,
        ),
    )
    assert contradictions.parse_all(page.body) == ()


def test_contradiction_cannot_cite_an_existing_source_not_supplied_to_the_planner(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="morgan", display_name="Morgan")
    first_receipt, _first_item, first_outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="bounded-contradiction-first",
        text="The signed renewal agreement says the renewal is annual.",
        plan=FilingPlan(
            summary="Recorded the signed renewal cadence",
            mutations=(
                PageMutation(
                    action="create",
                    role="note",
                    title="Bounded renewal provenance",
                    body=(
                        "# Bounded renewal provenance\n\n"
                        "The signed renewal agreement states an annual renewal."
                    ),
                    reason="The agreement establishes the renewal cadence",
                ),
            ),
        ),
        editorial=True,
    )
    first_source = source_path(schema.parse_capture(first_receipt["request"]))
    unrelated_receipt, _unrelated_item, unrelated_outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="bounded-contradiction-unrelated",
        text="The lunch menu contains a seasonal soup.",
        plan=FilingPlan(summary="Archived an unrelated source"),
    )
    unrelated_source = source_path(schema.parse_capture(unrelated_receipt["request"]))
    service = CaptureService(clean_queue, store)
    current_receipt = service.capture_text(
        actor=actor,
        audience=None,
        adapter="mcp",
        text="The current renewal billing schedule says the renewal is monthly.",
        idempotency_key="bounded-contradiction-current",
    )
    current_source = source_path(schema.parse_capture(current_receipt["request"]))
    plan = FilingPlan(
        summary="Attempted to cite evidence outside the supplied context",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/notes/Bounded renewal provenance.md",
                body=(
                    "# Bounded renewal provenance\n\n"
                    "This mutation must be rolled back."
                ),
                reason="The sources appear to conflict",
            ),
        ),
        contradictions=(
            ContradictionProposal(
                page_path="wiki/notes/Bounded renewal provenance.md",
                explanation="The proposed claims cite unrelated evidence.",
                claims=(
                    ContradictionClaim(
                        text="The renewal is monthly.",
                        source=current_source,
                    ),
                    ContradictionClaim(
                        text="The renewal is annual.",
                        source=unrelated_source,
                    ),
                ),
            ),
        ),
    )
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")

    item, outcome = worker.process_next(
        clean_queue,
        WriterDeps(settings, store, ScriptedPlanner(plan), str(target_repo)),
    )

    page = parse_page(
        "wiki/notes/Bounded renewal provenance.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Bounded renewal provenance.md"],
            cwd=target_repo,
            text=True,
        ),
    )
    assert first_outcome.status == schema.LANDED
    assert unrelated_outcome.status == schema.LANDED
    assert outcome.status == schema.LANDED
    assert item["report"]["plan_rejected"] is True
    assert item["report"]["wiki_changes"] == 0
    assert first_source in page.sources
    assert "annual renewal" in page.body
    assert "must be rolled back" not in page.body
    assert contradictions.parse_all(page.body) == ()


def test_restricted_contradiction_is_kept_only_on_a_safe_companion_page(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    master = Actor(subject="morgan", display_name="Morgan")
    bob = Actor(subject="bob", display_name="Bob")
    service = CaptureService(clean_queue, store)
    first = service.capture_text(
        actor=master,
        audience=None,
        adapter="mcp",
        text="The public schedule says renewal is annual.",
        idempotency_key="acl-contradiction-open",
        captured_at=dt.datetime(2026, 8, 20, tzinfo=dt.UTC),
    )
    first_source = source_path(schema.parse_capture(first["request"]))
    first_plan = FilingPlan(
        summary="Recorded the public renewal schedule",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Renewal schedule",
                body="# Renewal schedule\n\nThe public schedule says renewal is annual.",
                reason="The public schedule establishes the current claim",
            ),
        ),
    )
    worker.process_next(
        clean_queue,
        WriterDeps(settings, store, EditorialFixturePlanner(first_plan), str(target_repo)),
    )
    public_before = subprocess.check_output(
        ["git", "show", "main:wiki/notes/Renewal schedule.md"],
        cwd=target_repo,
    )

    unsafe = service.capture_text(
        actor=bob,
        audience=("finance",),
        adapter="mcp",
        text="A restricted billing schedule says renewal is monthly.",
        idempotency_key="acl-contradiction-unsafe",
        captured_at=dt.datetime(2026, 8, 21, tzinfo=dt.UTC),
    )
    unsafe_source = source_path(schema.parse_capture(unsafe["request"]))
    unsafe_plan = FilingPlan(
        summary="Attempted an unsafe contradiction marker",
        contradictions=(
            ContradictionProposal(
                page_path="wiki/notes/Renewal schedule.md",
                explanation="The schedules disagree.",
                claims=(
                    ContradictionClaim(
                        text="Renewal is annual.", source=first_source, date="2026-08-20"
                    ),
                    ContradictionClaim(
                        text="Renewal is monthly.", source=unsafe_source, date="2026-08-21"
                    ),
                ),
            ),
        ),
    )
    unsafe_item, unsafe_outcome = worker.process_next(
        clean_queue,
        WriterDeps(settings, store, ScriptedPlanner(unsafe_plan), str(target_repo)),
    )
    assert unsafe_outcome.status == schema.LANDED
    assert unsafe_item["report"]["wiki_changes"] == 1
    assert subprocess.check_output(
        ["git", "show", "main:wiki/notes/Renewal schedule.md"],
        cwd=target_repo,
    ) == public_before
    finance_part = parse_page("wiki/notes/Renewal schedule.md", subprocess.check_output(
        ["git", "show", "main:layers/finance/wiki/notes/Renewal schedule.md"], cwd=target_repo, text=True))
    assert finance_part.acl == ("finance",)
    assert len(contradictions.parse_all(finance_part.body)) == 1

    safe = service.capture_text(
        actor=bob,
        audience=("finance",),
        adapter="mcp",
        text="A restricted billing schedule says renewal is monthly.",
        idempotency_key="acl-contradiction-safe",
        captured_at=dt.datetime(2026, 8, 21, 1, tzinfo=dt.UTC),
    )
    safe_source = source_path(schema.parse_capture(safe["request"]))
    safe_plan = FilingPlan(
        summary="Preserved the contradiction within finance",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Finance renewal discrepancy",
                body="# Finance renewal discrepancy\n\nThe schedules disagree.",
                reason="The restricted evidence requires a restricted companion",
            ),
        ),
        contradictions=(
            ContradictionProposal(
                page_path="wiki/notes/Finance renewal discrepancy.md",
                explanation="The public and finance schedules disagree.",
                claims=(
                    ContradictionClaim(
                        text="Renewal is annual.", source=first_source, date="2026-08-20"
                    ),
                    ContradictionClaim(
                        text="Renewal is monthly.", source=safe_source, date="2026-08-21"
                    ),
                ),
            ),
        ),
    )
    safe_item, safe_outcome = worker.process_next(
        clean_queue,
        WriterDeps(settings, store, EditorialFixturePlanner(safe_plan), str(target_repo)),
    )

    assert safe_outcome.status == schema.LANDED
    companion_text = subprocess.check_output(
        ["git", "show", "main:layers/finance/wiki/notes/Finance renewal discrepancy.md"],
        cwd=target_repo,
        text=True,
    )
    companion = parse_page("wiki/notes/Finance renewal discrepancy.md", companion_text)
    assert companion.acl == ("finance",)
    assert len(contradictions.parse_all(companion.body)) == 1
    assert safe_item["commit_sha"]


def test_only_the_master_can_remove_a_matching_scoped_contradiction(
    clean_queue, target_repo
):
    _join(clean_queue, "morgan", "finance")
    store = evidence.MemoryEvidenceStore()
    service = CaptureService(clean_queue, store)
    master = Actor(subject="morgan", display_name="Morgan")
    bob = Actor(subject="bob", display_name="Bob")
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    seed = service.capture_text(
        actor=master,
        adapter="mcp",
        audience=("finance",),
        text="Two finance schedules disagree about the notice period.",
        idempotency_key="finance-resolution-seed",
        captured_at=dt.datetime(2026, 8, 20, tzinfo=dt.UTC),
    )
    public_source = source_path(schema.parse_capture(seed["request"]))
    proposal = ContradictionProposal(
        page_path="wiki/notes/Finance notice period.md",
        explanation="Two finance schedules disagree.",
        claims=(
            ContradictionClaim(
                text="Notice is 30 days.", source=public_source, date="2026-08-20"
            ),
            ContradictionClaim(
                text="Notice is 60 days.", source=public_source, date="2026-08-20"
            ),
        ),
    )
    worker.process_next(
        clean_queue,
        WriterDeps(
            settings,
            store,
            EditorialFixturePlanner(
                FilingPlan(
                    summary="Preserved finance contradictions",
                    mutations=(
                        PageMutation(
                            action="create",
                            role="note",
                            title="Finance notice period",
                            body="# Finance notice period\n\nThe schedules disagree.",
                            reason="Both claims remain credible",
                        ),
                    ),
                    contradictions=(
                        proposal,
                        ContradictionProposal(
                            page_path="wiki/notes/Finance notice period.md",
                            explanation="A separate finance schedule disagreement remains.",
                            claims=(
                                ContradictionClaim(
                                    text="Notice is 45 days.", source=public_source, date="2026-08-20"
                                ),
                                ContradictionClaim(
                                    text="Notice is 75 days.", source=public_source, date="2026-08-20"
                                ),
                            ),
                        ),
                    ),
                )
            ),
            str(target_repo),
        ),
    )
    before = parse_page(
        "wiki/notes/Finance notice period.md",
        subprocess.check_output(
            ["git", "show", "main:layers/finance/wiki/notes/Finance notice period.md"],
            cwd=target_repo,
            text=True,
        ),
    )
    records = contradictions.parse_all(before.body)
    contradiction_id = records[0].record.contradiction_id
    retained_id = records[1].record.contradiction_id

    scoped_receipt = service.capture_text(
        actor=bob,
        audience=("finance",),
        adapter="admin",
        text="A finance email proposes one notice period.",
        idempotency_key="scoped-finance-resolution",
        captured_at=dt.datetime(2026, 8, 21, tzinfo=dt.UTC),
        intent=schema.CaptureIntent(
            resolution_of=contradiction_id,
            rationale="The finance email was submitted as resolution evidence.",
        ),
    )
    scoped_source = source_path(schema.parse_capture(scoped_receipt["request"]))
    item, outcome = worker.process_next(
        clean_queue,
        WriterDeps(
            settings,
            store,
            ScriptedPlanner(
                FilingPlan(
                    summary="Performed unrelated non-master work while resolving",
                    mutations=(
                        PageMutation(
                            action="create",
                            role="entity",
                            title="Scoped resolution entity",
                            entity_type="organization",
                            body="# Scoped resolution entity\n\nThis entity page must not be written.",
                            reason="The non-master plan attempted an unrelated entity page",
                        ),
                        PageMutation(
                            action="create",
                            role="note",
                            title="Scoped resolution side effect",
                            body="# Scoped resolution side effect\n\nThis must not be written.",
                            reason="The non-master plan attempted an unrelated mutation",
                        ),
                    ),
                    contradictions=(
                        ContradictionProposal(
                            page_path="wiki/notes/Finance notice period.md",
                            explanation="This must not append another contradiction.",
                            claims=(
                                ContradictionClaim(
                                    text="Notice is 30 days.", source=scoped_source, date="2026-08-21"
                                ),
                                ContradictionClaim(
                                    text="Notice is 60 days.", source=scoped_source, date="2026-08-21"
                                ),
                            ),
                        ),
                    ),
                    resolved_contradictions=(contradiction_id,),
                )
            ),
            str(target_repo),
        ),
    )
    after = parse_page(
        "wiki/notes/Finance notice period.md",
        subprocess.check_output(
            ["git", "show", "main:layers/finance/wiki/notes/Finance notice period.md"],
            cwd=target_repo,
            text=True,
        ),
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["wiki_changes"] == 0
    assert item["report"]["plan_rejected"] is True
    assert item["report"]["plan_skipped"] == []
    changed_paths = subprocess.check_output(
        ["git", "show", "--format=", "--name-only", item["commit_sha"]],
        cwd=target_repo,
        text=True,
    ).splitlines()
    assert changed_paths == [f"layers/finance/{scoped_source}"]
    assert "layers/finance/wiki/entities/Scoped resolution entity.md" not in _tree(target_repo)
    assert {
        record.record.contradiction_id for record in contradictions.parse_all(after.body)
    } == {contradiction_id, retained_id}

    service.capture_text(
        actor=master,
        audience=("finance",),
        adapter="admin",
        text="The master submitted controlling finance evidence.",
        idempotency_key="master-finance-resolution",
        captured_at=dt.datetime(2026, 8, 22, tzinfo=dt.UTC),
        intent=schema.CaptureIntent(
            resolution_of=contradiction_id,
            rationale="The master selected the controlling finance evidence.",
        ),
    )
    master_item, master_outcome = worker.process_next(
        clean_queue,
        WriterDeps(
            settings,
            store,
                EditorialFixturePlanner(
                    FilingPlan(
                        summary="Resolved the targeted finance contradiction",
                        mutations=(
                            PageMutation(
                                action="update",
                                path="wiki/notes/Finance notice period.md",
                                body=(
                                    "# Finance notice period\n\n"
                                    "The controlling finance evidence sets the notice period. "
                                    "A separate schedule disagreement remains unresolved."
                                ),
                                reason="Recorded the controlling finance evidence",
                            ),
                        ),
                        resolved_contradictions=(contradiction_id,),
                    )
                ),
            str(target_repo),
        ),
    )
    resolved = parse_page(
        "wiki/notes/Finance notice period.md",
        subprocess.check_output(
            ["git", "show", "main:layers/finance/wiki/notes/Finance notice period.md"],
            cwd=target_repo,
            text=True,
        ),
    )

    assert master_outcome.status == schema.LANDED
    assert master_item["report"]["wiki_changes"] == 1
    assert [record.record.contradiction_id for record in contradictions.parse_all(resolved.body)] == [
        retained_id,
    ]


def test_unsupported_resolution_lands_as_evidence_and_preserves_uncertainty(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    service = CaptureService(clean_queue, store)
    actor = Actor(subject="morgan", display_name="Morgan")
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    seed = service.capture_text(
        actor=actor,
        audience=None,
        adapter="mcp",
        text="Two contracts disagree about the notice period.",
        idempotency_key="unresolved-seed",
        captured_at=dt.datetime(2026, 8, 20, tzinfo=dt.UTC),
    )
    source = source_path(schema.parse_capture(seed["request"]))
    proposal = ContradictionProposal(
        page_path="wiki/notes/Notice period.md",
        explanation="Two current contracts disagree.",
        claims=(
            ContradictionClaim(text="Notice is 30 days.", source=source, date="2026-08-20"),
            ContradictionClaim(text="Notice is 60 days.", source=source, date="2026-08-20"),
        ),
    )
    seed_plan = FilingPlan(
        summary="Preserved the notice-period contradiction",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Notice period",
                body="# Notice period\n\nThe current contracts disagree.",
                reason="Both claims remain supportable",
            ),
        ),
        contradictions=(proposal,),
    )
    worker.process_next(
        clean_queue,
        WriterDeps(settings, store, EditorialFixturePlanner(seed_plan), str(target_repo)),
    )
    page_text = subprocess.check_output(
        ["git", "show", "main:wiki/notes/Notice period.md"],
        cwd=target_repo,
        text=True,
    )
    contradiction_id = contradictions.parse_all(
        parse_page("wiki/notes/Notice period.md", page_text).body
    )[0].record.contradiction_id

    service.capture_text(
        actor=actor,
        audience=None,
        adapter="admin",
        text="The new email repeats both periods and does not settle which controls.",
        idempotency_key="unsupported-resolution",
        captured_at=dt.datetime(2026, 8, 22, tzinfo=dt.UTC),
        intent=schema.CaptureIntent(
            resolution_of=contradiction_id,
            rationale="The email was submitted for evaluation.",
        ),
    )
    item, outcome = worker.process_next(
        clean_queue,
        WriterDeps(
            settings,
            store,
            ScriptedPlanner(
                FilingPlan(
                    summary="Archived evidence that does not resolve the contradiction",
                )
            ),
            str(target_repo),
        ),
    )

    assert outcome.status == schema.LANDED
    assert list_changes(clean_queue)[0].trigger == "contradiction_resolution"
    assert item["report"]["wiki_changes"] == 0
    after = subprocess.check_output(
        ["git", "show", "main:wiki/notes/Notice period.md"],
        cwd=target_repo,
        text=True,
    )
    assert after == page_text


def test_a_narrower_resolution_of_an_open_contradiction_is_rejected_whole(clean_queue, target_repo):
    _join(clean_queue, "morgan", "finance")
    store = evidence.MemoryEvidenceStore()
    service = CaptureService(clean_queue, store)
    master = Actor(subject="morgan", display_name="Morgan")
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    seed = service.capture_text(
        actor=master, adapter="mcp", audience=None,
        text="Two public schedules disagree about the notice period.",
        idempotency_key="open-resolution-seed", captured_at=dt.datetime(2026, 8, 20, tzinfo=dt.UTC))
    open_source = source_path(schema.parse_capture(seed["request"]))
    worker.process_next(clean_queue, WriterDeps(settings, store, EditorialFixturePlanner(FilingPlan(
        summary="Preserved the open contradiction",
        mutations=(PageMutation(action="create", role="note", title="Open notice period",
                                body="# Open notice period\n\nThe schedules disagree.",
                                reason="Both claims remain credible"),),
        contradictions=(ContradictionProposal(
            page_path="wiki/notes/Open notice period.md", explanation="Two schedules disagree.",
            claims=(ContradictionClaim(text="Notice is 30 days.", source=open_source, date="2026-08-20"),
                    ContradictionClaim(text="Notice is 60 days.", source=open_source, date="2026-08-20"))),),
    )), str(target_repo)))
    before = subprocess.check_output(["git", "show", "main:wiki/notes/Open notice period.md"],
                                     cwd=target_repo)
    contradiction_id = contradictions.parse_all(
        parse_page("wiki/notes/Open notice period.md", before.decode()).body)[0].record.contradiction_id

    narrow = service.capture_text(
        actor=master, adapter="mcp", audience=("finance",),
        text="A finance email settles the notice period at 30 days.",
        idempotency_key="narrow-resolution", captured_at=dt.datetime(2026, 8, 21, tzinfo=dt.UTC),
        intent=schema.CaptureIntent(resolution_of=contradiction_id, rationale="Finance email"))
    item, outcome = worker.process_next(clean_queue, WriterDeps(settings, store, ScriptedPlanner(FilingPlan(
        summary="Resolved the contradiction and noted the email",
        mutations=(
            PageMutation(action="create", role="note", title="Finance notice email",
                         body="# Finance notice email\n\nThe email settles notice at 30 days.",
                         reason="Record the email"),
            PageMutation(action="update", path="wiki/notes/Open notice period.md",
                         body="# Open notice period\n\nNotice is 30 days.", reason="Resolve"),
        ),
        resolved_contradictions=(contradiction_id,),
    )), str(target_repo)))

    assert outcome.status == schema.LANDED
    assert item["report"]["plan_rejected"] is True
    assert item["report"]["wiki_changes"] == 0
    assert subprocess.check_output(["git", "show", "main:wiki/notes/Open notice period.md"],
                                   cwd=target_repo) == before
    del narrow


def _join(conn, subject: str, layer: str) -> None:
    """Membership is the right to publish: an administrator files in a layer only as a member."""
    with conn.cursor() as cur:
        cur.execute("UPDATE roster_principals SET groups = array_append(groups, %s) WHERE subject = %s",
                    (layer, subject))
