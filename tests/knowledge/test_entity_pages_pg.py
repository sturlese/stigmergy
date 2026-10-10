"""Entities are librarian-owned pages and the wikilink is their only relation."""

import subprocess

from stigmergy.capture import evidence, queue, schema
from stigmergy.capture.schema import Actor
from stigmergy.capture.service import CaptureService
from stigmergy.knowledge import writer
from stigmergy.knowledge.lint import check
from stigmergy.knowledge.pages import parse_page
from stigmergy.knowledge.plan import FilingPlan, PageMutation
from stigmergy.knowledge.planner import ScriptedPlanner
from stigmergy.knowledge.writer import WriterDeps
from stigmergy.librarian import config, worker

MORGAN = Actor(subject="morgan", display_name="Morgan")
ALICE = Actor(subject="alice", display_name="Alice")


def _capture(conn, repo, store, *, actor, audience, key, text, plan):
    receipt = CaptureService(conn, store).capture_text(
        actor=actor, audience=audience, adapter="mcp", text=text, idempotency_key=key
    )
    settings = config.Settings(repo=str(repo), branch="main", backend="scripted")
    item, outcome = worker.process_next(conn, WriterDeps(settings, store, ScriptedPlanner(plan), str(repo)))
    return receipt, item, outcome


def _show(repo, path: str) -> str:
    return subprocess.check_output(["git", "show", f"main:{path}"], cwd=repo, text=True)


def _tracked(repo) -> set[str]:
    listing = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", "main"], cwd=repo, text=True)
    return set(listing.splitlines())


def _note(title: str, body: str) -> PageMutation:
    return PageMutation(action="create", role="note", title=title, body=body, reason="test note")


def _entity(title: str, body: str, *, entity_type="organization", aliases=None) -> PageMutation:
    return PageMutation(
        action="create",
        role="entity",
        title=title,
        body=body,
        entity_type=entity_type,
        aliases=aliases,
        reason="test entity",
    )


def _filing_source(item) -> str:
    return item["source_path"]


def test_capture_lands_note_and_entity_pages_linked_both_ways(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    plan = FilingPlan(
        summary="A meeting with Tessera",
        mutations=(
            _note(
                "Tessera meeting",
                "# Tessera meeting\n\nOn 3 March 2026 [[Tessera]] presented to [[Harbor Fund]]. "
                "(Source: `{source}`)\n\n## Entities\n- [[Tessera]] - presenting company\n- [[Harbor Fund]] - investor",
            ),
            _entity(
                "Tessera",
                "# Tessera\n\n## What / Who\nEdtech company that met [[Harbor Fund]]. (Source: `{source}`)\n\n"
                "## Connections\n- [[Tessera meeting]] - first meeting",
                aliases=("Tessera Learning",),
            ),
            _entity(
                "Harbor Fund",
                "# Harbor Fund\n\n## What / Who\nVenture fund that met [[Tessera]]. (Source: `{source}`)\n\n"
                "## Connections\n- [[Tessera meeting]] - first meeting",
            ),
        ),
    )
    receipt = CaptureService(clean_queue, store).capture_text(
        actor=MORGAN, audience=None, adapter="mcp", text="Tessera met Harbor Fund.", idempotency_key="tessera"
    )
    source = writer.source_path(schema.parse_capture(receipt["request"]))
    plan = plan.model_copy(
        update={
            "mutations": tuple(
                m.model_copy(update={"body": m.body.replace("{source}", source)}) for m in plan.mutations
            )
        }
    )
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    item, outcome = worker.process_next(
        clean_queue, WriterDeps(settings, store, ScriptedPlanner(plan), str(target_repo))
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["plan_rejected"] is False, item["report"]
    assert item["report"]["plan_skipped"] == []
    tracked = _tracked(target_repo)
    assert {"wiki/notes/Tessera meeting.md", "wiki/entities/Tessera.md", "wiki/entities/Harbor Fund.md"} <= tracked
    tessera = parse_page("wiki/entities/Tessera.md", _show(target_repo, "wiki/entities/Tessera.md"))
    assert tessera.entity_type == "organization"
    assert tessera.aliases == ("Tessera Learning",)
    assert tessera.sources == (source,)
    assert "[[Harbor Fund]]" in tessera.body and "[[Tessera meeting]]" in tessera.body
    note = parse_page("wiki/notes/Tessera meeting.md", _show(target_repo, "wiki/notes/Tessera meeting.md"))
    assert note.body.count("[[Tessera]]") == 2
    assert check(str(target_repo)) == ()


def test_unresolved_links_degrade_to_prose_and_are_reported(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    receipt = CaptureService(clean_queue, store).capture_text(
        actor=MORGAN, audience=None, adapter="mcp", text="A note about nothing.", idempotency_key="dead"
    )
    source = writer.source_path(schema.parse_capture(receipt["request"]))
    plan = FilingPlan(
        summary="A note with a dead link",
        mutations=(
            _note(
                "Dead link note",
                "# Dead link note\n\nThis mentions [[Nowhere Page|somewhere]] and [[Also Missing]]. "
                f"(Source: `{source}`)",
            ),
        ),
    )
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    item, outcome = worker.process_next(
        clean_queue, WriterDeps(settings, store, ScriptedPlanner(plan), str(target_repo))
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["plan_rejected"] is False, item["report"]
    assert item["report"]["wiki_changes"] == 1
    assert sorted(item["report"]["plan_skipped"]) == [
        "unlinked unresolved wikilink 'Also Missing' in wiki/notes/Dead link note.md",
        "unlinked unresolved wikilink 'Nowhere Page' in wiki/notes/Dead link note.md",
    ]
    body = _show(target_repo, "wiki/notes/Dead link note.md")
    assert "This mentions somewhere and Also Missing." in body
    assert "[[" not in body


def test_entity_creation_with_a_claimed_name_is_skipped_and_alias_links_reach_the_existing_page(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    first = CaptureService(clean_queue, store).capture_text(
        actor=MORGAN, audience=None, adapter="mcp", text="Quillstack raises.", idempotency_key="quill-1"
    )
    first_source = writer.source_path(schema.parse_capture(first["request"]))
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    worker.process_next(
        clean_queue,
        WriterDeps(
            settings,
            store,
            ScriptedPlanner(
                FilingPlan(
                    summary="Quillstack",
                    mutations=(
                        _entity(
                            "Quillstack",
                            f"# Quillstack\n\n## What / Who\nInference startup. (Source: `{first_source}`)",
                            aliases=("Quill Stack",),
                        ),
                    ),
                )
            ),
            str(target_repo),
        ),
    )
    second = CaptureService(clean_queue, store).capture_text(
        actor=MORGAN, audience=None, adapter="mcp", text="Quill Stack again.", idempotency_key="quill-2"
    )
    second_source = writer.source_path(schema.parse_capture(second["request"]))
    item, outcome = worker.process_next(
        clean_queue,
        WriterDeps(
            settings,
            store,
            ScriptedPlanner(
                FilingPlan(
                    summary="Duplicate Quill Stack",
                    mutations=(
                        _entity("Quill Stack", f"# Quill Stack\n\nDuplicate. (Source: `{second_source}`)"),
                        _note(
                            "Quillstack follow-up",
                            f"# Quillstack follow-up\n\nA second meeting with [[Quillstack]] and [[Quill Stack]]. "
                            f"(Source: `{second_source}`)",
                        ),
                    ),
                )
            ),
            str(target_repo),
        ),
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["plan_rejected"] is False, item["report"]
    assert item["report"]["plan_skipped"] == [
        "entity page 'Quill Stack' was not created: its name is already claimed",
    ]
    tracked = _tracked(target_repo)
    assert "wiki/entities/Quill Stack.md" not in tracked
    body = _show(target_repo, "wiki/notes/Quillstack follow-up.md")
    assert "[[Quillstack]] and [[Quillstack|Quill Stack]]." in body
    assert check(str(target_repo)) == ()


def test_open_capture_cannot_link_a_restricted_entity_page(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    restricted = CaptureService(clean_queue, store).capture_text(
        actor=ALICE, audience=("engineering",), adapter="mcp", text="Vault Corp supplies engineering.",
        idempotency_key="restricted-supplier"
    )
    restricted_source = writer.source_path(schema.parse_capture(restricted["request"]))
    worker.process_next(
        clean_queue,
        WriterDeps(
            settings,
            store,
            ScriptedPlanner(
                FilingPlan(
                    summary="Vault",
                    mutations=(
                        _entity(
                            "Vault Corp", f"# Vault Corp\n\nA restricted supplier. (Source: `{restricted_source}`)"
                        ),
                    ),
                )
            ),
            str(target_repo),
        ),
    )
    assert parse_page(
        "wiki/entities/Vault Corp.md", _show(target_repo, "layers/engineering/wiki/entities/Vault Corp.md")
    ).acl == ("engineering",)

    open_capture = CaptureService(clean_queue, store).capture_text(
        actor=MORGAN, audience=None, adapter="mcp", text="Everyone knows Vault Corp.", idempotency_key="open"
    )
    open_source = writer.source_path(schema.parse_capture(open_capture["request"]))
    item, outcome = worker.process_next(
        clean_queue,
        WriterDeps(
            settings,
            store,
            ScriptedPlanner(
                FilingPlan(
                    summary="Open note",
                    mutations=(
                        _note("Open note", f"# Open note\n\nWe talked about [[Vault Corp]]. (Source: `{open_source}`)"),
                    ),
                )
            ),
            str(target_repo),
        ),
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["plan_rejected"] is False, item["report"]
    assert item["report"]["plan_skipped"] == ["unlinked unresolved wikilink 'Vault Corp' in wiki/notes/Open note.md"]
    assert "[[" not in _show(target_repo, "wiki/notes/Open note.md")
    assert check(str(target_repo)) == ()


def test_master_rename_merge_and_delete_rewrite_links_atomically(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    receipt = CaptureService(clean_queue, store).capture_text(
        actor=MORGAN,
        audience=None,
        adapter="mcp",
        text="Northstar Labs and Northstar Research are duplicate CRM identities.",
        idempotency_key="northstar",
    )
    source = writer.source_path(schema.parse_capture(receipt["request"]))
    worker.process_next(
        clean_queue,
        WriterDeps(
            settings,
            store,
            ScriptedPlanner(
                FilingPlan(
                    summary="Northstar",
                    mutations=(
                        _entity("Northstar Labs", f"# Northstar Labs\n\nA lab. (Source: `{source}`)"),
                        _entity("Northstar Research", f"# Northstar Research\n\nA research arm. (Source: `{source}`)"),
                        _note(
                            "Northstar account",
                            f"# Northstar account\n\n[[Northstar Labs]] and [[Northstar Research]] are accounts. "
                            f"(Source: `{source}`)",
                        ),
                    ),
                )
            ),
            str(target_repo),
        ),
    )

    def run(request):
        queue.enqueue_entity_operation(clean_queue, request)
        return worker.process_next(
            clean_queue, WriterDeps(settings, store, ScriptedPlanner(), str(target_repo))
        )

    _item, outcome = run(
        schema.EntityOperationRequest(
            idempotency_key="rename",
            actor=MORGAN,
            action="rename",
            entities=("wiki/entities/Northstar Labs.md",),
            new_title="Northstar Systems",
            rationale="The company renamed itself",
        )
    )
    assert outcome.status == schema.LANDED
    renamed = parse_page("wiki/entities/Northstar Systems.md", _show(target_repo, "wiki/entities/Northstar Systems.md"))
    assert renamed.aliases == ("Northstar Labs",)
    assert "wiki/entities/Northstar Labs.md" not in _tracked(target_repo)
    assert "[[Northstar Systems|Northstar Labs]]" in _show(target_repo, "wiki/notes/Northstar account.md")

    _item, outcome = run(
        schema.EntityOperationRequest(
            idempotency_key="merge",
            actor=MORGAN,
            action="merge",
            entities=("wiki/entities/Northstar Systems.md", "wiki/entities/Northstar Research.md"),
            rationale="CRM evidence confirms the duplicate identity",
            evidence=schema.EntityMergeEvidence(
                source_assertions=(
                    schema.SourceMergeAssertion(
                        path=source,
                        assertion="Northstar Labs and Northstar Research are duplicate CRM identities.",
                    ),
                )
            ),
        )
    )
    assert outcome.status == schema.LANDED
    tracked = _tracked(target_repo)
    assert "wiki/entities/Northstar Research.md" not in tracked
    merged = parse_page("wiki/entities/Northstar Systems.md", _show(target_repo, "wiki/entities/Northstar Systems.md"))
    assert set(merged.aliases) == {"Northstar Labs", "Northstar Research"}
    assert "## Formerly Northstar Research" in merged.body and "A research arm." in merged.body
    account = _show(target_repo, "wiki/notes/Northstar account.md")
    assert "[[Northstar Systems|Northstar Research]]" in account
    assert check(str(target_repo)) == ()

    _item, outcome = run(
        schema.EntityOperationRequest(
            idempotency_key="delete",
            actor=MORGAN,
            action="delete",
            entities=("wiki/entities/Northstar Systems.md",),
            rationale="Not a durable identity",
        )
    )
    assert outcome.status == schema.LANDED
    assert "wiki/entities/Northstar Systems.md" not in _tracked(target_repo)
    account = _show(target_repo, "wiki/notes/Northstar account.md")
    assert "[[" not in account and "Northstar Labs and Northstar Research are accounts." in account
    assert check(str(target_repo)) == ()


def test_entity_update_preserves_or_replaces_aliases(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    settings = config.Settings(repo=str(target_repo), branch="main", backend="scripted")
    first = CaptureService(clean_queue, store).capture_text(
        actor=MORGAN, audience=None, adapter="mcp", text="Acme.", idempotency_key="acme-1"
    )
    first_source = writer.source_path(schema.parse_capture(first["request"]))
    worker.process_next(
        clean_queue,
        WriterDeps(
            settings,
            store,
            ScriptedPlanner(
                FilingPlan(
                    summary="Acme",
                    mutations=(
                        _entity("Acme", f"# Acme\n\nA company. (Source: `{first_source}`)", aliases=("ACME Inc",)),
                    ),
                )
            ),
            str(target_repo),
        ),
    )
    second = CaptureService(clean_queue, store).capture_text(
        actor=MORGAN, audience=None, adapter="mcp", text="Acme again.", idempotency_key="acme-2"
    )
    second_source = writer.source_path(schema.parse_capture(second["request"]))
    item, outcome = worker.process_next(
        clean_queue,
        WriterDeps(
            settings,
            store,
            ScriptedPlanner(
                FilingPlan(
                    summary="Acme update",
                    mutations=(
                        PageMutation(
                            action="update",
                            path="wiki/entities/Acme.md",
                            body=f"# Acme\n\nA company. (Source: `{first_source}`)\n\n"
                            f"## Facts\n- Raised a round in 2026. (Source: `{second_source}`)",
                            reason="new fact",
                        ),
                    ),
                )
            ),
            str(target_repo),
        ),
    )
    assert outcome.status == schema.LANDED and item["report"]["plan_rejected"] is False, item["report"]
    page = parse_page("wiki/entities/Acme.md", _show(target_repo, "wiki/entities/Acme.md"))
    assert page.aliases == ("ACME Inc",)
    assert page.sources == (first_source, second_source)
