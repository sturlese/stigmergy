import asyncio
import datetime as dt
import hashlib
import json
import subprocess
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pymupdf

from stigmergy.capture import evidence, queue, schema, uploads
from stigmergy.capture.service import CaptureService
from stigmergy.capture.source import source_path
from stigmergy.changes.store import list_changes
from stigmergy.index import build, corpus
from stigmergy.index import health as index_health
from stigmergy.index import store as index_store
from stigmergy.index.backends.embedder import build_embedder
from stigmergy.knowledge import contradictions
from stigmergy.knowledge.pages import parse_page
from stigmergy.knowledge.plan import (
    ContradictionClaim,
    ContradictionProposal,
    EntityProposal,
    FilingPlan,
    PageMutation,
)
from stigmergy.knowledge.planner import ScriptedPlanner
from stigmergy.knowledge.writer import WriterDeps
from stigmergy.librarian import config, gitcmd, worker
from stigmergy.server import webhook
from stigmergy.server.identity import Principal
from stigmergy.server.mcp_server import build_mcp as build_server_mcp
from stigmergy.server.service import BrainService
from stigmergy.slack.snapshot import (
    SlackSnapshot,
    SnapshotMessage,
    canonical_bytes,
    timestamp_from_slack,
)
from tests.capture.conftest import clean_queue, conn
from tests.knowledge.conftest import target_repo

__all__ = ["clean_queue", "conn", "target_repo"]


def _git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _digital_pdf(text: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 100), text, fontsize=14)
    data = document.tobytes()
    document.close()
    return data


def _scanned_pdf(text: str) -> bytes:
    digital = pymupdf.open(stream=_digital_pdf(text), filetype="pdf")
    image = digital[0].get_pixmap(dpi=200, alpha=False).tobytes("png")
    digital.close()
    document = pymupdf.open()
    page = document.new_page()
    page.insert_image(page.rect, stream=image)
    data = document.tobytes()
    document.close()
    return data


def _process(conn, repo, evidence_store, plan=None):
    settings = config.Settings(repo=str(repo), branch="main", backend="scripted")
    return worker.process_next(
        conn,
        WriterDeps(
            settings,
            evidence_store,
            ScriptedPlanner(plan or FilingPlan(summary="Archived immutable evidence")),
            str(repo),
        ),
    )


def _main_text(repo, path):
    return _git(repo, "show", f"main:{path}") + "\n"


class _Response:
    def __init__(self, text: str):
        self._body = text.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _opener(checkout: Path):
    def open_request(request, timeout=30):
        del timeout
        parsed = urllib.parse.urlparse(request.full_url)
        path = urllib.parse.unquote(parsed.path.split("/contents/", 1)[1])
        return _Response((checkout / path).read_text(encoding="utf-8"))

    return open_request


def _search_state(conn):
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT path, content_hash, type, status, entity, acl, sources, links, inlinks "
            "FROM pages_index ORDER BY path"
        )
        return cursor.fetchall()


def test_clean_system_flow_converges_from_capture_to_full_index(
    clean_queue, target_repo, tmp_path_factory, monkeypatch
):
    evidence_store = evidence.MemoryEvidenceStore()
    actor = schema.Actor(subject="marc", display_name="Marc")
    capture = CaptureService(clean_queue, evidence_store)
    captured_at = dt.datetime(2026, 8, 24, 10, tzinfo=dt.UTC)
    source_blobs = {}

    first = capture.capture_text(
        actor=actor,
        audience=None,
        adapter="mcp",
        text=(
            "Northstar Labs and Northstar Research are duplicate CRM identities. "
            "The signed renewal states an annual cadence."
        ),
        idempotency_key="system-text",
        captured_at=captured_at,
    )
    first_source = source_path(schema.parse_capture(first["request"]))
    first_item, first_outcome = _process(
        clean_queue,
        target_repo,
        evidence_store,
        FilingPlan(
            summary="Recorded the annual renewal and two CRM identities",
            entities=(
                EntityProposal(name="Northstar Labs", entity_type="organization"),
                EntityProposal(name="Northstar Research", entity_type="organization"),
            ),
            mutations=(
                PageMutation(
                    action="create",
                    role="note",
                    title="Northstar renewal",
                    body="# Northstar renewal\n\nThe signed renewal states an annual cadence.",
                    entities=("Northstar Labs", "Northstar Research"),
                    reason="The source establishes the initial renewal cadence",
                ),
            ),
        ),
    )
    assert first_outcome.status == schema.LANDED
    source_blobs[first_source] = _git(
        target_repo, "rev-parse", f"{first_item['commit_sha']}:{first_source}"
    )

    digital_bytes = _digital_pdf(
        "The current signed schedule says the Northstar renewal cadence is monthly."
    )
    second = capture.capture_bytes(
        actor=actor,
        audience=None,
        adapter="mcp",
        artifact_values=((digital_bytes, schema.MEDIA_PDF, "schedule.pdf", None),),
        idempotency_key="system-digital-pdf",
        captured_at=captured_at + dt.timedelta(minutes=1),
    )
    second_source = source_path(schema.parse_capture(second["request"]))
    second_item, second_outcome = _process(
        clean_queue,
        target_repo,
        evidence_store,
        FilingPlan(
            summary="Preserved conflicting renewal schedules",
            mutations=(
                PageMutation(
                    action="update",
                    path="wiki/notes/Northstar renewal.md",
                    body=(
                        "# Northstar renewal\n\n"
                        "The signed renewal and the current schedule disagree on cadence."
                    ),
                    reason="The current signed schedule conflicts with the renewal",
                ),
            ),
            contradictions=(
                ContradictionProposal(
                    page_path="wiki/notes/Northstar renewal.md",
                    explanation="Two signed sources state incompatible renewal cadences.",
                    claims=(
                        ContradictionClaim(
                            text="The renewal cadence is annual.",
                            source=first_source,
                            date="2026-08-24",
                        ),
                        ContradictionClaim(
                            text="The renewal cadence is monthly.",
                            source=second_source,
                            date="2026-08-24",
                        ),
                    ),
                ),
            ),
        ),
    )
    assert second_outcome.status == schema.LANDED
    assert second_item["extraction"]["artifacts"][0]["ocr_pages"] == []
    assert second_item["extraction"]["artifacts"][0]["decisions"] == [
        "page 1: digital text"
    ]
    source_blobs[second_source] = _git(
        target_repo, "rev-parse", f"{second_item['commit_sha']}:{second_source}"
    )

    capture.capture_bytes(
        actor=actor,
        audience=None,
        adapter="mcp",
        artifact_values=(
            (
                _scanned_pdf("Scanned board approval for the Northstar account"),
                schema.MEDIA_PDF,
                "board-scan.pdf",
                None,
            ),
        ),
        idempotency_key="system-scanned-pdf",
        captured_at=captured_at + dt.timedelta(minutes=2),
    )
    scanned_item, scanned_outcome = _process(
        clean_queue, target_repo, evidence_store
    )
    assert scanned_outcome.status == schema.LANDED
    assert scanned_item["extraction"]["artifacts"][0]["ocr_pages"] == [1]
    source_blobs[scanned_item["source_path"]] = _git(
        target_repo,
        "rev-parse",
        f"{scanned_item['commit_sha']}:{scanned_item['source_path']}",
    )

    principal = Principal(
        subject=actor.subject,
        display_name=actor.display_name,
        groups=("brain-admins",),
        default_audience=None,
    )
    bridge = BrainService(
        SimpleNamespace(identities_path="unused"),
        clean_queue,
        None,
        audiences=None,
        identity=actor.subject,
        evidence=evidence_store,
        principal=principal,
    )
    drive_bytes = _digital_pdf("Private Drive board pack confirms the account owner is Marc.")
    drive_digest = hashlib.sha256(drive_bytes).hexdigest()
    drive_upload = bridge.create_upload(
        idempotency_key="system-drive-upload",
        sha256=drive_digest,
        bytes=len(drive_bytes),
        media_type=schema.MEDIA_PDF,
        original_name="board-pack.pdf",
        source_url="https://drive.google.com/file/d/private-fixture/view",
    )
    evidence_store.objects[uploads.staging_ref(drive_upload["upload_id"])] = drive_bytes
    drive_receipt = bridge.finalize_upload_capture(
        upload_ids=[drive_upload["upload_id"]],
        idempotency_key="system-drive-capture",
        title="Private board pack",
        locator="https://drive.google.com/file/d/private-fixture/view",
        acquisition={
            "original_url": (
                "https://docs.google.com/document/d/private-fixture/edit"
            ),
            "final_url": "https://drive.google.com/file/d/private-fixture/view",
            "drive_file_id": "private-fixture",
            "drive_media_type": schema.MEDIA_PDF,
            "acquired_at": (captured_at + dt.timedelta(minutes=3)).isoformat(),
        },
    )
    drive_row = queue.get_submission_trace(clean_queue, drive_receipt["id"])
    drive_item, drive_outcome = _process(
        clean_queue, target_repo, evidence_store
    )
    drive_envelope = schema.parse_capture(drive_row["request"])
    assert drive_outcome.status == schema.LANDED
    assert drive_envelope.artifacts[0].source_url.startswith("https://drive.google.com/")
    assert drive_envelope.origin.acquisition is not None
    assert drive_envelope.origin.acquisition.drive_file_id == "private-fixture"
    assert evidence_store.get(drive_envelope.artifacts[0].blob_ref) == drive_bytes
    drive_source_text = _main_text(target_repo, drive_item["source_path"])
    drive_source_metadata, _body, malformed = corpus.split_frontmatter_checked(
        drive_source_text
    )
    assert malformed is False
    assert drive_source_metadata["acquisition"]["drive_file_id"] == "private-fixture"
    assert drive_source_metadata["acquisition"]["original_url"] == (
        "https://docs.google.com/document/d/private-fixture/edit"
    )
    source_blobs[drive_item["source_path"]] = _git(
        target_repo,
        "rev-parse",
        f"{drive_item['commit_sha']}:{drive_item['source_path']}",
    )

    slack_bytes = canonical_bytes(
        SlackSnapshot(
            team_id="T1",
            channel_id="C1",
            channel_name="product",
            thread_ts="1787565600.000001",
            permalink="https://example.slack.com/thread",
            messages=(
                SnapshotMessage(
                    order=1,
                    ts="1787565600.000001",
                    occurred_at=timestamp_from_slack("1787565600.000001"),
                    user_id="U1",
                    speaker="Marc",
                    text="The channel scratch note can be removed after filing.",
                    permalink="https://example.slack.com/thread",
                ),
            ),
        )
    )
    capture.capture_bytes(
        actor=actor,
        audience=None,
        adapter="slack",
        artifact_values=(
            (slack_bytes, schema.MEDIA_SLACK, "thread.json", "https://example.slack.com/thread"),
        ),
        idempotency_key="system-slack",
        captured_at=captured_at + dt.timedelta(minutes=4),
    )
    slack_item, slack_outcome = _process(
        clean_queue,
        target_repo,
        evidence_store,
        FilingPlan(
            summary="Recorded the temporary channel note",
            mutations=(
                PageMutation(
                    action="create",
                    role="note",
                    title="Channel scratch",
                    body="# Channel scratch\n\nThis note can be removed after filing.",
                    reason="The Slack thread requests a temporary note",
                ),
            ),
        ),
    )
    assert slack_outcome.status == schema.LANDED
    source_blobs[slack_item["source_path"]] = _git(
        target_repo,
        "rev-parse",
        f"{slack_item['commit_sha']}:{slack_item['source_path']}",
    )

    current_page = parse_page(
        "wiki/notes/Northstar renewal.md",
        _main_text(target_repo, "wiki/notes/Northstar renewal.md"),
    )
    contradiction_id = contradictions.parse_all(current_page.body)[0].record.contradiction_id
    capture.capture_text(
        actor=actor,
        audience=None,
        adapter="admin",
        text="The countersigned amendment confirms that the annual cadence controls.",
        idempotency_key="system-resolution",
        captured_at=captured_at + dt.timedelta(minutes=5),
        intent=schema.CaptureIntent(
            resolution_of=contradiction_id,
            rationale="The countersigned amendment has controlling authority.",
        ),
    )
    resolution_item, resolution_outcome = _process(
        clean_queue,
        target_repo,
        evidence_store,
        FilingPlan(
            summary="Resolved the renewal cadence",
            mutations=(
                PageMutation(
                    action="update",
                    path="wiki/notes/Northstar renewal.md",
                    body=(
                        "# Northstar renewal\n\n"
                        "The annual cadence controls under the countersigned amendment."
                    ),
                    reason="The amendment resolves the conflicting schedules",
                ),
            ),
            resolved_contradictions=(contradiction_id,),
        ),
    )
    assert resolution_outcome.status == schema.LANDED
    source_blobs[resolution_item["source_path"]] = _git(
        target_repo,
        "rev-parse",
        f"{resolution_item['commit_sha']}:{resolution_item['source_path']}",
    )
    assert contradictions.parse_all(
        parse_page(
            "wiki/notes/Northstar renewal.md",
            _main_text(target_repo, "wiki/notes/Northstar renewal.md"),
        ).body
    ) == ()

    registry = json.loads(_main_text(target_repo, "ops/entity-registry.json"))
    entity_ids = tuple(sorted(registry["entities"]))
    queue.enqueue_entity_operation(
        clean_queue,
        schema.EntityOperationRequest(
            idempotency_key="system-entity-merge",
            actor=actor,
            action="merge",
            entity_ids=entity_ids,
            rationale="The source identifies the CRM records as duplicates.",
            evidence=schema.EntityMergeEvidence(
                source_assertions=(
                    schema.SourceMergeAssertion(
                        path=first_source,
                        assertion=(
                            "Northstar Labs and Northstar Research are duplicate CRM identities."
                        ),
                    ),
                ),
            ),
        ),
    )
    merge_item, merge_outcome = _process(clean_queue, target_repo, evidence_store)
    assert merge_outcome.status == schema.LANDED
    assert merge_item["change_id"]

    queue.enqueue_delete(
        clean_queue,
        schema.DeleteRequest(
            idempotency_key="system-delete",
            actor=actor,
            paths=("wiki/notes/Channel scratch.md",),
            rationale="The temporary note has served its purpose.",
        ),
    )
    delete_item, delete_outcome = _process(clean_queue, target_repo, evidence_store)
    assert delete_outcome.status == schema.LANDED
    assert delete_item["change_id"]

    queue.enqueue_garden(
        clean_queue,
        schema.GardenRequest(
            idempotency_key="system-garden",
            actor=schema.Actor(
                subject="system:garden",
                display_name="Stigmergy Gardener",
            ),
            rationale="Scheduled autonomous corpus health run",
        ),
    )
    garden_item, garden_outcome = _process(clean_queue, target_repo, evidence_store)
    assert garden_outcome.status == schema.LANDED
    assert garden_item["report"]["clean"] is True

    head = _git(target_repo, "rev-parse", "main")
    for path, blob in source_blobs.items():
        assert _git(target_repo, "rev-parse", f"main:{path}") == blob
    assert "wiki/notes/Channel scratch.md" not in _git(
        target_repo, "ls-tree", "-r", "--name-only", "main"
    ).splitlines()

    fake_embedder = build_embedder("fake")
    index_store.init_schema(
        clean_queue,
        dim=256,
        model=fake_embedder.model,
        fts_config="english",
        host=fake_embedder.host,
    )
    index_store.create_search_indexes(clean_queue)
    index_health.record_full_rebuild(clean_queue, "", 0)
    for relpath in index_store.OPS_FILE_RELPATHS:
        index_store.clear_ops_file(clean_queue, relpath)

    checkout_root = tmp_path_factory.mktemp("clean-system-index")
    monkeypatch.setattr(
        "stigmergy.librarian.githubapp.installation_token",
        lambda: "fixture-token",
    )
    with gitcmd.ephemeral_worktree(
        str(target_repo), head, root=str(checkout_root)
    ) as checkout:
        checkout_path = Path(checkout)
        indexable = sorted(
            path.relative_to(checkout_path).as_posix()
            for zone in corpus.ZONES
            for path in (checkout_path / zone).rglob("*.md")
            if corpus.is_indexable_page(path.relative_to(checkout_path).as_posix())
        )
        incremental = webhook.process_push(
            clean_queue,
            fake_embedder,
            {
                "ref": "refs/heads/main",
                "before": "",
                "after": head,
                "repository": {"full_name": "fixture/brain"},
                "commits": [
                    {"added": indexable, "modified": [], "removed": []}
                ],
            },
            webhook.WebhookSettings(
                secret="fixture",
                repo="fixture/brain",
                branch="main",
                file_cap=100,
            ),
            opener=_opener(checkout_path),
        )
        incremental_state = _search_state(clean_queue)
        incremental_hits = BrainService(
            SimpleNamespace(
                entity_registry_path=str(checkout_path / "ops/entity-registry.json")
            ),
            clean_queue,
            fake_embedder,
            audiences=None,
        ).search("Northstar renewal")["hits"]

        rebuilt = build.rebuild(clean_queue, checkout, fake_embedder)
        rebuilt_state = _search_state(clean_queue)
        rebuilt_hits = BrainService(
            SimpleNamespace(
                entity_registry_path=str(checkout_path / "ops/entity-registry.json")
            ),
            clean_queue,
            fake_embedder,
            audiences=None,
        ).search("Northstar renewal")["hits"]

    assert incremental["upserted"] == len(indexable)
    assert incremental_state == rebuilt_state
    assert [hit["path"] for hit in incremental_hits] == [
        hit["path"] for hit in rebuilt_hits
    ]
    assert rebuilt["commit_sha"] == head
    assert len(list_changes(clean_queue)) == 8
    assert {change.trigger for change in list_changes(clean_queue)} == {
        "capture",
        "contradiction_resolution",
        "entity",
        "delete",
    }


def test_mcp_delete_lands_one_commit_sweeps_references_and_leaves_search(
    clean_queue, target_repo, tmp_path_factory, monkeypatch
):
    evidence_store = evidence.MemoryEvidenceStore()
    actor = schema.Actor(subject="marc", display_name="Marc")
    capture = CaptureService(clean_queue, evidence_store)
    capture.capture_text(
        actor=actor,
        audience=None,
        adapter="mcp",
        text="A temporary operating note and its reference holder.",
        idempotency_key="delete-fixture",
    )
    _item, outcome = _process(
        clean_queue,
        target_repo,
        evidence_store,
        FilingPlan(
            summary="Created a temporary linked note",
            mutations=(
                PageMutation(
                    action="create",
                    role="note",
                    title="Temporary operating note",
                    body="# Temporary operating note\n\nEphemeral procedure delta.",
                    reason="Recorded the temporary procedure",
                ),
                PageMutation(
                    action="create",
                    role="note",
                    title="Reference holder",
                    body="# Reference holder\n\nSee [[Temporary operating note]].",
                    reason="Recorded the related reference",
                ),
            ),
        ),
    )
    assert outcome.status == schema.LANDED

    principal = Principal(
        subject=actor.subject,
        display_name=actor.display_name,
        groups=("brain-admins",),
        default_audience=None,
    )
    service = BrainService(
        SimpleNamespace(identities_path=str(target_repo / "ops" / "identities.json")),
        clean_queue,
        None,
        audiences=None,
        identity=actor.subject,
        evidence=evidence_store,
        principal=principal,
    )
    mcp = build_server_mcp(service)
    before = int(_git(target_repo, "rev-list", "--count", "main"))
    blocks, _ = asyncio.run(
        mcp.call_tool(
            "brain_delete",
            {
                "paths": ["wiki/notes/Temporary operating note.md"],
                "why": "The temporary procedure is no longer current.",
            },
        )
    )
    receipt = json.loads(blocks[0].text)
    assert receipt["status"] == schema.QUEUED

    deleted_item, deleted_outcome = _process(clean_queue, target_repo, evidence_store)
    assert deleted_outcome.status == schema.LANDED
    assert int(_git(target_repo, "rev-list", "--count", "main")) == before + 1
    assert deleted_item["change_id"]
    assert list_changes(clean_queue)[0].trigger == "delete"
    paths = _git(target_repo, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert "wiki/notes/Temporary operating note.md" not in paths
    assert "[[Temporary operating note]]" not in _main_text(
        target_repo, "wiki/notes/Reference holder.md"
    )

    fake_embedder = build_embedder("fake")
    head = _git(target_repo, "rev-parse", "main")
    monkeypatch.setattr(
        "stigmergy.librarian.githubapp.installation_token",
        lambda: "fixture-token",
    )
    checkout_root = tmp_path_factory.mktemp("delete-search-index")
    with gitcmd.ephemeral_worktree(
        str(target_repo), head, root=str(checkout_root)
    ) as checkout:
        build.rebuild(clean_queue, checkout, fake_embedder)
        hits = BrainService(
            SimpleNamespace(
                entity_registry_path=str(Path(checkout) / "ops" / "entity-registry.json")
            ),
            clean_queue,
            fake_embedder,
            audiences=None,
        ).search("ephemeral procedure delta")["hits"]
    assert "wiki/notes/Temporary operating note.md" not in {
        hit["path"] for hit in hits
    }
