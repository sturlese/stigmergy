import datetime as dt
import io
import json
import subprocess
import zipfile

import pymupdf
import pytest

from stigmergy.capture import evidence, queue, schema
from stigmergy.capture.errors import ArtifactRejected, QueueStateError
from stigmergy.capture.fetch import FetchedArtifact
from stigmergy.capture.schema import Actor
from stigmergy.capture.service import CaptureService
from stigmergy.capture.source import source_path
from stigmergy.changes.store import list_changes
from stigmergy.index.corpus import split_frontmatter_checked
from stigmergy.knowledge import contradictions
from stigmergy.knowledge.pages import parse_page, render_page
from stigmergy.knowledge.plan import (
    ContradictionClaim,
    ContradictionProposal,
    FilingPlan,
    PageMutation,
)
from stigmergy.knowledge.planner import ScriptedPlanner
from stigmergy.knowledge.writer import WriterDeps
from stigmergy.librarian import config, worker


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
        WriterDeps(settings, store, ScriptedPlanner(plan), str(repo)),
    )
    return receipt, item, outcome


def test_one_capture_lands_source_wiki_and_change_in_one_commit(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    material = "The support rotation changes to weekly on 1 September."
    receipt = CaptureService(clean_queue, store).capture_text(
        actor=Actor(subject="marc", display_name="Marc"),
        audience=None,
        adapter="mcp",
        text=material,
        idempotency_key="writer-e2e",
        title="Support rotation",
    )
    plan = FilingPlan(
        summary="Recorded the weekly support rotation",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Support rotation",
                body="# Support rotation\n\nThe support rotation changes weekly on 1 September.",
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
        actor=Actor(subject="marc", display_name="Marc"),
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
        actor=Actor(subject="marc", display_name="Marc"),
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


def test_invalid_wiki_plan_lands_the_source_without_partial_mutations(
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
        summary="Renamed the target",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/notes/Target.md",
                title="Renamed target",
                body="# Renamed target\n\nUpdated target knowledge.",
                reason="The source uses the new title",
            ),
        ),
    )

    _receipt, item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="marc", display_name="Marc"),
        audience=None,
        key="invalid-cross-page-plan",
        text="The current target has a new title.",
        plan=plan,
    )

    tree = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "main"],
        cwd=target_repo,
        text=True,
    ).splitlines()
    assert outcome.status == schema.LANDED
    assert item["report"]["plan_rejected"] is True
    assert item["report"]["wiki_changes"] == 0
    assert "wiki/notes/Target.md" in tree
    assert "wiki/notes/Renamed target.md" not in tree
    assert [change.page_role for change in list_changes(clean_queue)[0].manifest] == [
        "source"
    ]


def test_pasted_transcript_archives_exact_input_and_files_only_its_conclusion(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    transcript = (
        "Alice: We will move the support rotation to weekly on 1 September.\n"
        "Bob: Agreed. I will update the rota."
    )
    plan = FilingPlan(
        summary="Recorded the weekly support rotation decision",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Weekly support rotation",
                body=(
                    "# Weekly support rotation\n\n"
                    "The team agreed to move the support rotation to weekly on 1 September."
                ),
                reason="The transcript establishes the decision",
            ),
        ),
    )

    receipt, item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="alice", display_name="Alice"),
        audience=("engineering",),
        key="pasted-transcript",
        text=transcript,
        plan=plan,
    )

    request = queue.get_submission_trace(clean_queue, receipt["id"])["request"]
    artifact = schema.parse_capture(request).artifacts[0]
    source_text = subprocess.check_output(
        ["git", "show", f"main:{item['source_path']}"], cwd=target_repo, text=True
    )
    note_text = subprocess.check_output(
        ["git", "show", "main:wiki/notes/Weekly support rotation.md"],
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
        actor=Actor(subject="marc", display_name="Marc"),
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
            actor=Actor(subject="marc", display_name="Marc"),
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
            actor=Actor(subject="marc", display_name="Marc"),
            audience=None,
            adapter="mcp",
            artifact_values=((unsafe.getvalue(), None, None, None),),
            idempotency_key="unsafe-container",
        )

    monkeypatch.setattr(schema, "MAX_ARTIFACT_BYTES", 4)
    with pytest.raises(ArtifactRejected, match="size limit"):
        service.capture_bytes(
            actor=Actor(subject="marc", display_name="Marc"),
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
    actor = Actor(subject="marc", display_name="Marc")
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


def test_exact_proposed_entity_name_anchors_a_mutated_page_when_omitted_from_mutation(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    plan = FilingPlan(
        summary="Recorded the Northstar Research renewal",
        entities=(
            {
                "name": "Northstar Research",
                "entity_type": "organization",
            },
        ),
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Northstar Research renewal",
                body=(
                    "# Northstar Research renewal\n\n"
                    "Northstar Research approved the annual renewal."
                ),
                reason="The source identifies the organization and its renewal",
            ),
        ),
    )

    _receipt, _item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="alice", display_name="Alice"),
        audience=None,
        key="implicit-entity-anchor",
        text="Northstar Research approved the annual renewal.",
        plan=plan,
    )

    assert outcome.status == schema.LANDED
    registry = json.loads(
        subprocess.check_output(
            ["git", "show", "main:ops/entity-registry.json"],
            cwd=target_repo,
            text=True,
        )
    )
    entity_id = next(iter(registry["entities"]))
    page = parse_page(
        "wiki/notes/Northstar Research renewal.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Northstar Research renewal.md"],
            cwd=target_repo,
            text=True,
        ),
    )

    assert page.entities == (entity_id,)


def test_proposed_entity_name_substrings_do_not_anchor_mutated_pages(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    plan = FilingPlan(
        summary="Recorded AcmeCorp renewal",
        entities=(({"name": "Acme", "entity_type": "organization"}),),
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="AcmeCorp renewal",
                body="# AcmeCorp renewal\n\nAcmeCorp approved the annual renewal.",
                reason="The source records the AcmeCorp decision",
            ),
        ),
    )

    _receipt, _item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="alice", display_name="Alice"),
        audience=None,
        key="entity-substring-does-not-anchor",
        text="AcmeCorp approved the annual renewal.",
        plan=plan,
    )

    assert outcome.status == schema.LANDED
    page = parse_page(
        "wiki/notes/AcmeCorp renewal.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/AcmeCorp renewal.md"],
            cwd=target_repo,
            text=True,
        ),
    )
    assert page.entities == ()


def test_overlapping_proposed_names_anchor_only_the_longest_matching_name(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    plan = FilingPlan(
        summary="Recorded entity approvals",
        entities=(
            {"name": "Acme", "entity_type": "organization"},
            {"name": "Acme Inc", "entity_type": "organization"},
            {"name": "Pinecone Labs", "entity_type": "organization"},
        ),
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Acme Inc approval",
                body="# Acme Inc approval\n\nAcme Inc approved the renewal.",
                reason="The source names Acme Inc exactly",
            ),
            PageMutation(
                action="create",
                role="note",
                title="Acme and Pinecone approval",
                body=(
                    "# Acme and Pinecone approval\n\nAcme and Pinecone Labs "
                    "approved the renewal."
                ),
                reason="The source names two distinct organizations",
            ),
        ),
    )

    _receipt, _item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="alice", display_name="Alice"),
        audience=None,
        key="longest-proposed-entity-anchor",
        text="Acme Inc and Pinecone Labs approved the renewal.",
        plan=plan,
    )

    assert outcome.status == schema.LANDED
    registry = json.loads(
        subprocess.check_output(
            ["git", "show", "main:ops/entity-registry.json"],
            cwd=target_repo,
            text=True,
        )
    )
    entity_ids = {
        claim["value"]: entity_id
        for entity_id, record in registry["entities"].items()
        for claim in record["claims"]
    }
    overlapping = parse_page(
        "wiki/notes/Acme Inc approval.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Acme Inc approval.md"],
            cwd=target_repo,
            text=True,
        ),
    )
    distinct = parse_page(
        "wiki/notes/Acme and Pinecone approval.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Acme and Pinecone approval.md"],
            cwd=target_repo,
            text=True,
        ),
    )

    assert overlapping.entities == (entity_ids["Acme Inc"],)
    assert distinct.entities == (entity_ids["Acme"], entity_ids["Pinecone Labs"])


def test_separate_short_and_long_proposed_name_mentions_both_anchor(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    plan = FilingPlan(
        summary="Recorded Acme approvals",
        entities=(
            {"name": "Acme", "entity_type": "organization"},
            {"name": "Acme Inc", "entity_type": "organization"},
        ),
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Acme approvals",
                body=(
                    "# Acme approvals\n\nAcme Inc approved its renewal, while Acme "
                    "approved a separate renewal."
                ),
                reason="The source separately identifies Acme Inc and Acme",
            ),
        ),
    )

    _receipt, _item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="alice", display_name="Alice"),
        audience=None,
        key="separate-short-long-entity-anchor",
        text="Acme Inc and Acme approved separate renewals.",
        plan=plan,
    )

    assert outcome.status == schema.LANDED
    registry = json.loads(
        subprocess.check_output(
            ["git", "show", "main:ops/entity-registry.json"],
            cwd=target_repo,
            text=True,
        )
    )
    entity_ids = {
        claim["value"]: entity_id
        for entity_id, record in registry["entities"].items()
        for claim in record["claims"]
    }
    page = parse_page(
        "wiki/notes/Acme approvals.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Acme approvals.md"],
            cwd=target_repo,
            text=True,
        ),
    )

    assert page.entities == (entity_ids["Acme"], entity_ids["Acme Inc"])


def test_equivalent_proposed_name_spans_with_distinct_ids_do_not_auto_anchor(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    plan = FilingPlan(
        summary="Recorded ambiguous Acme identity evidence",
        entities=(
            {"name": "Acme Inc", "entity_type": "organization"},
            {"name": "Acme, Inc.", "entity_type": "organization"},
        ),
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Ambiguous Acme approval",
                body="# Ambiguous Acme approval\n\nAcme Inc approved the renewal.",
                reason="The source does not disambiguate equivalent proposed identities",
            ),
        ),
    )

    _receipt, _item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="alice", display_name="Alice"),
        audience=None,
        key="ambiguous-equivalent-entity-anchor",
        text="Acme Inc approved the renewal.",
        plan=plan,
    )

    assert outcome.status == schema.LANDED
    registry = json.loads(
        subprocess.check_output(
            ["git", "show", "main:ops/entity-registry.json"],
            cwd=target_repo,
            text=True,
        )
    )
    assert len(registry["entities"]) == 2
    page = parse_page(
        "wiki/notes/Ambiguous Acme approval.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Ambiguous Acme approval.md"],
            cwd=target_repo,
            text=True,
        ),
    )

    assert page.entities == ()


def test_omitted_update_entities_retains_existing_anchors_and_adds_exact_proposals(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="alice", display_name="Alice")
    initial = FilingPlan(
        summary="Recorded Legacy Systems renewal",
        entities=(({"name": "Legacy Systems", "entity_type": "organization"}),),
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Renewal record",
                body="# Renewal record\n\nLegacy Systems approved the current renewal.",
                entities=("Legacy Systems",),
                reason="The source identifies the existing organization",
            ),
        ),
    )
    _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="existing-entity-anchor",
        text="Legacy Systems approved the current renewal.",
        plan=initial,
    )
    update = FilingPlan(
        summary="Recorded Northstar Research renewal",
        entities=(({"name": "Northstar Research", "entity_type": "organization"}),),
        mutations=(
            PageMutation(
                action="update",
                path="wiki/notes/Renewal record.md",
                body=(
                    "# Renewal record\n\nLegacy Systems remains the account holder. "
                    "Northstar Research approved the annual renewal."
                ),
                reason="The source identifies the additional organization",
            ),
        ),
    )
    _receipt, _item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="implicit-update-entity-anchor",
        text="Northstar Research approved the annual renewal.",
        plan=update,
    )

    assert outcome.status == schema.LANDED
    registry = json.loads(
        subprocess.check_output(
            ["git", "show", "main:ops/entity-registry.json"],
            cwd=target_repo,
            text=True,
        )
    )
    entity_ids = {
        claim["value"]: entity_id
        for entity_id, record in registry["entities"].items()
        for claim in record["claims"]
    }
    page = parse_page(
        "wiki/notes/Renewal record.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Renewal record.md"],
            cwd=target_repo,
            text=True,
        ),
    )
    assert page.entities == (entity_ids["Legacy Systems"], entity_ids["Northstar Research"])


def test_explicit_entity_lists_do_not_add_matching_proposed_entities(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="alice", display_name="Alice")
    plan = FilingPlan(
        summary="Recorded Northstar Research and Pinecone Labs evidence",
        entities=(
            {"name": "Northstar Research", "entity_type": "organization"},
            {"name": "Pinecone Labs", "entity_type": "organization"},
        ),
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Listed entity page",
                body=(
                    "# Listed entity page\n\nNorthstar Research and Pinecone Labs "
                    "approved the renewal."
                ),
                entities=("Northstar Research",),
                reason="The source retains only the explicit selected entity",
            ),
            PageMutation(
                action="create",
                role="note",
                title="Empty entity page",
                body=(
                    "# Empty entity page\n\nNorthstar Research and Pinecone Labs "
                    "approved the renewal."
                ),
                entities=(),
                reason="The source intentionally carries no entity anchor",
            ),
        ),
    )
    _receipt, _item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="explicit-entity-lists-win",
        text="Northstar Research and Pinecone Labs approved the renewal.",
        plan=plan,
    )

    assert outcome.status == schema.LANDED
    registry = json.loads(
        subprocess.check_output(
            ["git", "show", "main:ops/entity-registry.json"],
            cwd=target_repo,
            text=True,
        )
    )
    entity_ids = {
        claim["value"]: entity_id
        for entity_id, record in registry["entities"].items()
        for claim in record["claims"]
    }
    listed = parse_page(
        "wiki/notes/Listed entity page.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Listed entity page.md"],
            cwd=target_repo,
            text=True,
        ),
    )
    empty = parse_page(
        "wiki/notes/Empty entity page.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Empty entity page.md"],
            cwd=target_repo,
            text=True,
        ),
    )

    assert listed.entities == (entity_ids["Northstar Research"],)
    assert empty.entities == ()


def test_guessed_hidden_page_and_entity_cannot_be_affected(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    bob = Actor(subject="bob", display_name="Bob")
    hidden_plan = FilingPlan(
        summary="Recorded a private finance relationship",
        entities=(
            {
                "name": "Stealth Holdings",
                "entity_type": "organization",
            },
        ),
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Private finance plan",
                body="# Private finance plan\n\nRestricted facts.",
                entities=("Stealth Holdings",),
                reason="Finance needs the restricted plan",
            ),
        ),
    )
    _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=bob,
        audience=("finance",),
        key="hidden-seed",
        text="Stealth Holdings has a restricted finance plan.",
        plan=hidden_plan,
    )
    registry = json.loads(
        subprocess.check_output(
            ["git", "show", "main:ops/entity-registry.json"],
            cwd=target_repo,
            text=True,
        )
    )
    hidden_id = next(iter(registry["entities"]))
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
        actor=Actor(subject="alice", display_name="Alice"),
        audience=("engineering",),
        key="guess-hidden-page",
        text="Try the guessed page path.",
        plan=guessed_page_plan,
    )
    assert page_outcome.status == schema.LANDED
    assert page_item["report"]["wiki_changes"] == 0

    guessed_entity_plan = FilingPlan(
        summary="Attempted an unauthorized entity anchor",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Guessed identity",
                body="# Guessed identity\n\nA guessed reference.",
                entities=(hidden_id,),
                reason="Guessed entity identifier",
            ),
        ),
    )
    _receipt, entity_item, entity_outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="alice", display_name="Alice"),
        audience=("engineering",),
        key="guess-hidden-entity",
        text="Try a guessed entity identifier.",
        plan=guessed_entity_plan,
    )

    assert entity_outcome.status == schema.LANDED
    assert entity_item["report"]["wiki_changes"] == 0
    assert subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip() != landed_head
    assert len(list_changes(clean_queue)) == landed_changes + 2


def test_strong_external_id_reuses_hidden_identity_without_model_or_receipt_leak(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    hidden_plan = FilingPlan(
        summary="Recorded the private account",
        entities=(
            {
                "name": "Secret Finance Account",
                "entity_type": "organization",
                "external_namespace": "crm",
                "external_id": "account-7",
            },
        ),
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Private account",
                body="# Private account\n\nRestricted finance facts.",
                entities=("Secret Finance Account",),
                reason="The source identifies the private account",
            ),
        ),
    )
    _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="bob", display_name="Bob"),
        audience=("finance",),
        key="hidden-external-id",
        text="The private CRM account is account-7.",
        plan=hidden_plan,
    )
    hidden_registry = json.loads(
        subprocess.check_output(
            ["git", "show", "main:ops/entity-registry.json"],
            cwd=target_repo,
            text=True,
        )
    )
    hidden_id = next(iter(hidden_registry["entities"]))

    visible_plan = FilingPlan(
        summary="Recorded the engineering vendor",
        entities=(
            {
                "name": "Engineering Vendor",
                "entity_type": "organization",
                "external_namespace": "crm",
                "external_id": "account-7",
            },
        ),
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Engineering vendor",
                body="# Engineering vendor\n\nThe vendor supports engineering.",
                entities=("Engineering Vendor",),
                reason="The source identifies the engineering vendor",
            ),
        ),
    )

    class RecordingPlanner(ScriptedPlanner):
        context = ""

        def plan(self, **kwargs):
            self.context = kwargs["context"]
            return super().plan(**kwargs)

    planner = RecordingPlanner(visible_plan)
    receipt = CaptureService(clean_queue, store).capture_text(
        actor=Actor(subject="alice", display_name="Alice"),
        audience=("engineering",),
        adapter="mcp",
        text="The engineering vendor uses CRM account-7.",
        idempotency_key="visible-external-id",
    )
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    item, outcome = worker.process_next(
        clean_queue,
        WriterDeps(settings, store, planner, str(target_repo)),
    )

    registry = json.loads(
        subprocess.check_output(
            ["git", "show", "main:ops/entity-registry.json"],
            cwd=target_repo,
            text=True,
        )
    )
    assert outcome.status == schema.LANDED
    assert set(registry["entities"]) == {hidden_id}
    assert "Secret Finance Account" not in planner.context
    assert hidden_id not in planner.context
    assert "Secret Finance Account" not in json.dumps(receipt)
    assert "finance" not in json.dumps(receipt)
    assert item["report"]["wiki_changes"] == 2


def test_restricted_input_cannot_rewrite_open_page_but_can_create_companion(
    clean_queue, target_repo
):
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
        actor=Actor(subject="marc", display_name="Marc"),
        audience=None,
        key="open-policy",
        text="This policy is organization-wide.",
        plan=open_plan,
    )
    open_before = subprocess.check_output(
        ["git", "show", "main:wiki/notes/Public policy.md"],
        cwd=target_repo,
    )

    unsafe_plan = FilingPlan(
        summary="Attempted to mix restricted evidence into the public policy",
        mutations=(
            PageMutation(
                action="update",
                path="wiki/notes/Public policy.md",
                body="# Public policy\n\nRestricted finance detail.",
                reason="Unsafe widening",
            ),
        ),
    )
    _receipt, unsafe_item, unsafe_outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="bob", display_name="Bob"),
        audience=("finance",),
        key="unsafe-open-update",
        text="Restricted finance detail.",
        plan=unsafe_plan,
    )
    assert unsafe_outcome.status == schema.LANDED
    assert unsafe_item["report"]["wiki_changes"] == 0

    companion_plan = FilingPlan(
        summary="Recorded the restricted companion",
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Finance policy detail",
                body="# Finance policy detail\n\nRestricted finance detail.",
                reason="Restricted evidence belongs in a restricted page",
            ),
        ),
    )
    _receipt, _item, outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=Actor(subject="bob", display_name="Bob"),
        audience=("finance",),
        key="safe-companion",
        text="Restricted finance detail for a companion page.",
        plan=companion_plan,
    )

    assert outcome.status == schema.LANDED
    assert subprocess.check_output(
        ["git", "show", "main:wiki/notes/Public policy.md"], cwd=target_repo
    ) == open_before
    companion = subprocess.check_output(
        ["git", "show", "main:wiki/notes/Finance policy detail.md"],
        cwd=target_repo,
        text=True,
    )
    assert parse_page("wiki/notes/Finance policy detail.md", companion).acl == (
        "finance",
    )


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


def test_explicit_delete_sweeps_page_and_source_references_in_one_commit(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="marc", display_name="Marc")
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


def test_source_deletion_reconciles_entity_claims_and_removes_empty_identities(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="marc", display_name="Marc")
    first_plan = FilingPlan(
        summary="Created an identified account",
        entities=({"name": "Acme Legacy", "entity_type": "organization"},),
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Acme account",
                body="# Acme account\n\nThe account knowledge remains substantive.",
                entities=("Acme Legacy",),
                reason="The source identifies the account",
            ),
        ),
    )
    _first, first_item, first_outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="entity-source-first",
        text="Acme Legacy is the account name.",
        plan=first_plan,
    )
    assert first_outcome.status == schema.LANDED
    first_source = first_item["source_path"]
    first_registry = json.loads(
        subprocess.check_output(
            ["git", "show", "main:ops/entity-registry.json"],
            cwd=target_repo,
            text=True,
        )
    )
    entity_id = next(iter(first_registry["entities"]))

    second_plan = FilingPlan(
        summary="Renamed the identified account",
        entities=(
            {
                "name": "Acme Systems",
                "entity_type": "organization",
                "same_as": "Acme Legacy",
            },
        ),
        mutations=(
            PageMutation(
                action="update",
                path="wiki/notes/Acme account.md",
                body="# Acme account\n\nThe account knowledge remains substantive after the rename.",
                entities=("Acme Systems",),
                reason="The source establishes the new account name",
            ),
        ),
    )
    _second, second_item, second_outcome = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="entity-source-second",
        text="Acme Legacy is now called Acme Systems.",
        plan=second_plan,
    )
    assert second_outcome.status == schema.LANDED
    second_source = second_item["source_path"]
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    deps = WriterDeps(settings, store, ScriptedPlanner(), str(target_repo))

    queue.enqueue_delete(
        clean_queue,
        schema.DeleteRequest(
            idempotency_key="delete-entity-name-source",
            actor=actor,
            paths=(second_source,),
            rationale="Remove the evidence for the renamed identity",
        ),
    )
    _deleted_name, delete_name_outcome = worker.process_next(clean_queue, deps)

    assert delete_name_outcome.status == schema.LANDED
    after_name_delete = json.loads(
        subprocess.check_output(
            ["git", "show", "main:ops/entity-registry.json"],
            cwd=target_repo,
            text=True,
        )
    )["entities"][entity_id]
    assert [(claim["value"], claim["kind"]) for claim in after_name_delete["claims"]] == [
        ("Acme Legacy", "preferred")
    ]
    assert all(claim["source"] != second_source for claim in after_name_delete["claims"])

    queue.enqueue_delete(
        clean_queue,
        schema.DeleteRequest(
            idempotency_key="delete-last-entity-source",
            actor=actor,
            paths=(first_source,),
            rationale="Remove the final evidence for the identity",
        ),
    )
    _deleted_last, delete_last_outcome = worker.process_next(clean_queue, deps)

    assert delete_last_outcome.status == schema.LANDED
    final_registry = json.loads(
        subprocess.check_output(
            ["git", "show", "main:ops/entity-registry.json"],
            cwd=target_repo,
            text=True,
        )
    )
    assert final_registry == {"entities": {}, "redirects": {}, "version": 1}
    final_page = parse_page(
        "wiki/notes/Acme account.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Acme account.md"],
            cwd=target_repo,
            text=True,
        ),
    )
    assert final_page.entities == ()
    assert "account knowledge remains substantive" in final_page.body


def test_entity_rename_merge_and_delete_use_atomic_writer_operations(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="marc", display_name="Marc")
    duplicate_assertion = (
        "Northstar Labs and Northstar Research are duplicate CRM identities."
    )
    first_plan = FilingPlan(
        summary="Created two identified organizations",
        entities=(
            {
                "name": "Northstar Labs",
                "entity_type": "organization",
                "external_namespace": "crm",
                "external_id": "crm-101",
            },
            {
                "name": "Northstar Research",
                "entity_type": "organization",
                "external_namespace": "crm",
                "external_id": "crm-202",
            },
        ),
        mutations=(
            PageMutation(
                action="create",
                role="note",
                title="Northstar account",
                body="# Northstar account\n\nThe account remains substantive.",
                entities=("Northstar Labs", "Northstar Research"),
                reason="The source describes both records",
            ),
        ),
    )
    _, first_item, _ = _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="entity-pair",
        text=duplicate_assertion,
        plan=first_plan,
    )
    registry = json.loads(
        subprocess.check_output(
            ["git", "show", "main:ops/entity-registry.json"],
            cwd=target_repo,
            text=True,
        )
    )
    first_id = next(
        entity_id
        for entity_id, record in registry["entities"].items()
        if any(claim["value"] == "Northstar Labs" for claim in record["claims"])
    )
    second_id = next(
        entity_id
        for entity_id, record in registry["entities"].items()
        if any(claim["value"] == "Northstar Research" for claim in record["claims"])
    )

    rename_plan = FilingPlan(
        summary="Renamed Northstar Labs",
        entities=(
            {
                "name": "Northstar Systems",
                "entity_type": "organization",
                "same_as": "Northstar Labs",
            },
        ),
        mutations=(
            PageMutation(
                action="update",
                path="wiki/notes/Northstar account.md",
                body="# Northstar account\n\nThe account remains substantive after the rename.",
                entities=("Northstar Systems", "Northstar Research"),
                reason="The source establishes the preferred name",
            ),
        ),
    )
    _process_capture(
        clean_queue,
        target_repo,
        store,
        actor=actor,
        audience=None,
        key="entity-rename",
        text="Northstar Labs is now called Northstar Systems.",
        plan=rename_plan,
    )
    renamed = json.loads(
        subprocess.check_output(
            ["git", "show", "main:ops/entity-registry.json"],
            cwd=target_repo,
            text=True,
        )
    )["entities"]
    assert set(renamed) == {first_id, second_id}
    assert {claim["value"]: claim["kind"] for claim in renamed[first_id]["claims"]} == {
        "Northstar Labs": "alias",
        "Northstar Systems": "preferred",
    }

    merge_request = schema.EntityOperationRequest(
        idempotency_key="merge-northstar",
        actor=actor,
        action="merge",
        entity_ids=(second_id, first_id),
        rationale="CRM evidence confirms the duplicate identity",
        evidence=schema.EntityMergeEvidence(
            source_assertions=(
                schema.SourceMergeAssertion(
                    path=first_item["source_path"],
                    assertion=duplicate_assertion,
                ),
            ),
        ),
    )
    queue.enqueue_entity_operation(clean_queue, merge_request)
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    before_merge = int(
        subprocess.check_output(
            ["git", "rev-list", "--count", "main"],
            cwd=target_repo,
            text=True,
        ).strip()
    )
    merged_item, merged_outcome = worker.process_next(
        clean_queue,
        WriterDeps(settings, store, ScriptedPlanner(), str(target_repo)),
    )

    assert merged_outcome.status == schema.LANDED
    merged_registry = json.loads(
        subprocess.check_output(
            ["git", "show", "main:ops/entity-registry.json"],
            cwd=target_repo,
            text=True,
        )
    )
    merged = merged_registry["entities"]
    canonical = next(iter(merged))
    absorbed = second_id if canonical == first_id else first_id
    assert merged[canonical]["absorbed_ids"] == [absorbed]
    assert merged_registry["redirects"] == {absorbed: canonical}
    page_after_merge = parse_page(
        "wiki/notes/Northstar account.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Northstar account.md"],
            cwd=target_repo,
            text=True,
        ),
    )
    assert page_after_merge.entities == (canonical,)
    assert int(
        subprocess.check_output(
            ["git", "rev-list", "--count", "main"],
            cwd=target_repo,
            text=True,
        ).strip()
    ) == before_merge + 1
    assert list_changes(clean_queue)[0].commit_sha == merged_item["commit_sha"]

    delete_request = schema.EntityOperationRequest(
        idempotency_key="delete-northstar",
        actor=actor,
        action="delete",
        entity_ids=(canonical,),
        rationale="The identity must be removed while retaining substantive knowledge",
    )
    queue.enqueue_entity_operation(clean_queue, delete_request)
    deleted_item, deleted_outcome = worker.process_next(
        clean_queue,
        WriterDeps(settings, store, ScriptedPlanner(), str(target_repo)),
    )

    assert deleted_outcome.status == schema.LANDED
    deleted_registry = json.loads(
        subprocess.check_output(
            ["git", "show", "main:ops/entity-registry.json"],
            cwd=target_repo,
            text=True,
        )
    )
    assert deleted_registry == {"entities": {}, "redirects": {}, "version": 1}
    page_after_delete = parse_page(
        "wiki/notes/Northstar account.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Northstar account.md"],
            cwd=target_repo,
            text=True,
        ),
    )
    assert page_after_delete.entities == ()
    assert "account remains substantive" in page_after_delete.body
    assert list_changes(clean_queue)[0].commit_sha == deleted_item["commit_sha"]


def test_contradiction_and_resolution_are_ordinary_atomic_captures(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    service = CaptureService(clean_queue, store)
    actor = Actor(subject="marc", display_name="Marc")
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
        WriterDeps(settings, store, ScriptedPlanner(first_plan), str(target_repo)),
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
        WriterDeps(settings, store, ScriptedPlanner(second_plan), str(target_repo)),
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
        WriterDeps(settings, store, ScriptedPlanner(resolution_plan), str(target_repo)),
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


def test_invalid_contradiction_proposal_cannot_block_a_valid_capture(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    actor = Actor(subject="marc", display_name="Marc")
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


def test_restricted_contradiction_is_kept_only_on_a_safe_companion_page(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    master = Actor(subject="marc", display_name="Marc")
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
        WriterDeps(settings, store, ScriptedPlanner(first_plan), str(target_repo)),
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
    assert unsafe_item["report"]["wiki_changes"] == 0
    assert subprocess.check_output(
        ["git", "show", "main:wiki/notes/Renewal schedule.md"],
        cwd=target_repo,
    ) == public_before

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
        WriterDeps(settings, store, ScriptedPlanner(safe_plan), str(target_repo)),
    )

    assert safe_outcome.status == schema.LANDED
    companion_text = subprocess.check_output(
        ["git", "show", "main:wiki/notes/Finance renewal discrepancy.md"],
        cwd=target_repo,
        text=True,
    )
    companion = parse_page("wiki/notes/Finance renewal discrepancy.md", companion_text)
    assert companion.acl == ("finance",)
    assert len(contradictions.parse_all(companion.body)) == 1
    assert safe_item["commit_sha"]


def test_restricted_resolution_cannot_remove_a_public_contradiction(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    service = CaptureService(clean_queue, store)
    master = Actor(subject="marc", display_name="Marc")
    bob = Actor(subject="bob", display_name="Bob")
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    seed = service.capture_text(
        actor=master,
        audience=None,
        adapter="mcp",
        text="Two public schedules disagree about the notice period.",
        idempotency_key="public-resolution-seed",
        captured_at=dt.datetime(2026, 8, 20, tzinfo=dt.UTC),
    )
    public_source = source_path(schema.parse_capture(seed["request"]))
    proposal = ContradictionProposal(
        page_path="wiki/notes/Public notice period.md",
        explanation="Two public schedules disagree.",
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
            ScriptedPlanner(
                FilingPlan(
                    summary="Preserved a public contradiction",
                    mutations=(
                        PageMutation(
                            action="create",
                            role="note",
                            title="Public notice period",
                            body="# Public notice period\n\nThe schedules disagree.",
                            reason="Both claims remain credible",
                        ),
                    ),
                    contradictions=(proposal,),
                )
            ),
            str(target_repo),
        ),
    )
    before = parse_page(
        "wiki/notes/Public notice period.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Public notice period.md"],
            cwd=target_repo,
            text=True,
        ),
    )
    contradiction_id = contradictions.parse_all(before.body)[0].record.contradiction_id

    service.capture_text(
        actor=bob,
        audience=("finance",),
        adapter="admin",
        text="A finance-only email proposes one notice period.",
        idempotency_key="restricted-public-resolution",
        captured_at=dt.datetime(2026, 8, 21, tzinfo=dt.UTC),
        intent=schema.CaptureIntent(
            resolution_of=contradiction_id,
            rationale="The finance email was submitted as resolution evidence.",
        ),
    )
    item, outcome = worker.process_next(
        clean_queue,
        WriterDeps(
            settings,
            store,
            ScriptedPlanner(
                FilingPlan(
                    summary="Archived restricted resolution evidence",
                    resolved_contradictions=(contradiction_id,),
                )
            ),
            str(target_repo),
        ),
    )
    after = parse_page(
        "wiki/notes/Public notice period.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Public notice period.md"],
            cwd=target_repo,
            text=True,
        ),
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["wiki_changes"] == 0
    assert contradictions.parse_all(after.body)[0].record.contradiction_id == contradiction_id


def test_unsupported_resolution_lands_as_evidence_and_preserves_uncertainty(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    service = CaptureService(clean_queue, store)
    actor = Actor(subject="marc", display_name="Marc")
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
        WriterDeps(settings, store, ScriptedPlanner(seed_plan), str(target_repo)),
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
                    resolved_contradictions=(contradiction_id,),
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
