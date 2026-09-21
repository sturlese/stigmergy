import datetime as dt
import json
import subprocess

from stigmergy.capture import evidence, ops, queue, schema
from stigmergy.changes.store import list_changes
from stigmergy.entities.model import EntityRecord, new_name_claim
from stigmergy.entities.service import write_records
from stigmergy.knowledge import writer as knowledge_writer
from stigmergy.knowledge.lint import check
from stigmergy.knowledge.pages import parse_page, render_page
from stigmergy.knowledge.plan import RepairMutation, RepairPlan
from stigmergy.knowledge.planner import ScriptedPlanner
from stigmergy.knowledge.writer import WriterDeps
from stigmergy.librarian import config, worker


def _commit(repo, message):
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            message,
        ],
        cwd=repo,
        check=True,
    )


def _enqueue_garden(conn, key):
    return queue.enqueue_garden(
        conn,
        schema.GardenRequest(
            idempotency_key=key,
            actor=schema.Actor(
                subject="system:garden",
                display_name="Stigmergy Gardener",
            ),
            rationale="Autonomous corpus health run",
        ),
    )


def _deps(repo, store, planner=None):
    settings = config.Settings(repo=str(repo), branch="main", backend="scripted")
    return WriterDeps(
        settings,
        store,
        planner or ScriptedPlanner(),
        str(repo),
    )


def _broken_page(repo, *, acl=None):
    path = repo / "wiki" / "notes" / "Broken links.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_page(
            path="wiki/notes/Broken links.md",
            role="note",
            title="Broken links",
            body="# Broken links\n\nSee [[Missing page]].",
            acl=acl,
            created=dt.date(2026, 8, 24),
            updated=dt.date(2026, 8, 24),
        ),
        encoding="utf-8",
    )
    _commit(repo, "add repair fixture")
    return path


def test_linter_is_pure_and_deterministic_for_a_fixed_tree(target_repo):
    _broken_page(target_repo)

    first = check(str(target_repo))
    second = check(str(target_repo))

    assert first == second
    assert [item.code for item in first] == ["dead-link"]


def test_clean_garden_run_records_no_change_and_no_commit(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    _enqueue_garden(clean_queue, "garden-clean")
    before = subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip()

    item, outcome = worker.process_next(clean_queue, _deps(target_repo, store))

    assert outcome.status == schema.LANDED
    assert item["commit_sha"] == ""
    assert item["change_id"] is None
    assert item["report"] == {
        "detected": 0,
        "fixed": 0,
        "clean": True,
        "model_requests": 0,
        "final_violations": 0,
        "commit_sha": "",
        "change_id": None,
    }
    assert subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip() == before
    assert list_changes(clean_queue) == []
    assert ops.latest_run(clean_queue, "garden")["status"] == ops.SUCCEEDED


def test_model_repair_lands_one_commit_and_reruns_linter(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    path = _broken_page(target_repo)
    fixed = "# Broken links\n\nThe page is self-contained."
    class RecordingPlanner(ScriptedPlanner):
        repair_kwargs = None

        def repair(
            self,
            *,
            worktree,
            violations,
            files,
            source_path,
            source_text,
            context,
            max_requests,
        ):
            self.repair_kwargs = {
                "worktree": worktree,
                "violations": violations,
                "files": files,
                "source_path": source_path,
                "source_text": source_text,
                "context": context,
                "max_requests": max_requests,
            }
            return super().repair(
                worktree=worktree,
                violations=violations,
                files=files,
                source_path=source_path,
                source_text=source_text,
                context=context,
                max_requests=max_requests,
            )

    planner = RecordingPlanner(
        repair_plan=RepairPlan(
            summary="Removed the dead link",
            mutations=(
                RepairMutation(
                    path="wiki/notes/Broken links.md",
                    body=fixed,
                    reason="Removed a link with no target",
                ),
            ),
        )
    )
    _enqueue_garden(clean_queue, "garden-model-repair")
    before_count = int(
        subprocess.check_output(
            ["git", "rev-list", "--count", "main"],
            cwd=target_repo,
            text=True,
        ).strip()
    )

    item, outcome = worker.process_next(
        clean_queue,
        _deps(target_repo, store, planner),
    )

    assert outcome.status == schema.LANDED
    assert int(
        subprocess.check_output(
            ["git", "rev-list", "--count", "main"],
            cwd=target_repo,
            text=True,
        ).strip()
    ) == before_count + 1
    assert "Missing page" not in subprocess.check_output(
        ["git", "show", "main:wiki/notes/Broken links.md"],
        cwd=target_repo,
        text=True,
    )
    assert item["report"]["clean"] is True
    assert item["report"]["final_violations"] == 0
    assert item["report"]["detected"] == 1
    assert item["report"]["fixed"] == 1
    assert planner.repair_kwargs is not None
    assert planner.repair_kwargs["source_path"] == ""
    assert planner.repair_kwargs["source_text"] == ""
    assert planner.repair_kwargs["max_requests"] == 2
    assert planner.repair_kwargs["files"] == {
        "wiki/notes/Broken links.md": (
            "# Broken links\n\nSee [[Missing page]]."
        )
    }
    assert '"maintenance": true' in planner.repair_kwargs["context"]
    assert '"allowed_sources_by_path"' in planner.repair_kwargs["context"]
    change = list_changes(clean_queue)[0]
    assert change.trigger == "garden"
    assert change.commit_sha == item["commit_sha"]
    assert ops.latest_run(clean_queue, "garden")["head_commit_sha"] == item["commit_sha"]
    assert path.is_file()


def test_garden_runs_every_lint_gate_before_advancing_the_branch(
    clean_queue, target_repo, monkeypatch
):
    store = evidence.MemoryEvidenceStore()
    _broken_page(target_repo)
    fixed = "# Broken links\n\nThe page is self-contained."
    planner = ScriptedPlanner(
        repair_plan=RepairPlan(
            summary="Removed the dead link",
            mutations=(
                RepairMutation(
                    path="wiki/notes/Broken links.md",
                    body=fixed,
                    reason="Removed a link with no target",
                ),
            ),
        )
    )
    base = subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip()
    real_check = knowledge_writer.check
    checked_heads = []

    def check_before_advance(path):
        head = subprocess.check_output(
            ["git", "rev-parse", "main"], cwd=target_repo, text=True
        ).strip()
        checked_heads.append(head)
        assert head == base
        return real_check(path)

    monkeypatch.setattr(knowledge_writer, "check", check_before_advance)
    _enqueue_garden(clean_queue, "garden-precommit-gates")

    item, outcome = worker.process_next(
        clean_queue,
        _deps(target_repo, store, planner),
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["clean"] is True
    assert checked_heads == [base, base, base]


def test_garden_recovers_commit_after_change_record_failure(
    clean_queue, target_repo, monkeypatch
):
    store = evidence.MemoryEvidenceStore()
    _broken_page(target_repo)
    fixed = "# Broken links\n\nThe page is self-contained."
    planner = ScriptedPlanner(
        repair_plan=RepairPlan(
            summary="Removed the dead link",
            mutations=(
                RepairMutation(
                    path="wiki/notes/Broken links.md",
                    body=fixed,
                    reason="Removed a link with no target",
                ),
            ),
        )
    )
    _enqueue_garden(clean_queue, "garden-change-record-recovery")
    original = knowledge_writer.record_change
    monkeypatch.setattr(
        knowledge_writer,
        "record_change",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("unavailable")),
    )

    first, first_outcome = worker.process_next(
        clean_queue,
        _deps(target_repo, store, planner),
    )

    assert first_outcome.status == schema.QUEUED
    assert list_changes(clean_queue) == []
    assert ops.latest_run(clean_queue, "garden")["status"] == ops.FAILED
    monkeypatch.setattr(knowledge_writer, "record_change", original)
    with clean_queue.cursor() as cursor:
        cursor.execute(
            "UPDATE capture_queue SET next_attempt_at = now() WHERE id = %s",
            (first["id"],),
        )

    item, outcome = worker.process_next(
        clean_queue,
        _deps(target_repo, store, planner),
    )

    assert outcome.status == schema.LANDED
    assert item["report"]["reconciled"] is True
    assert item["report"]["clean"] is True
    changes = list_changes(clean_queue)
    assert len(changes) == 1
    run = ops.latest_run(clean_queue, "garden", successful=True)
    assert run["status"] == ops.SUCCEEDED
    assert run["head_commit_sha"] == item["commit_sha"]
    assert str(changes[0].job_run_id) == run["id"]


def test_failed_repair_candidate_lands_nothing_and_records_safe_failure(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    _broken_page(target_repo)
    _enqueue_garden(clean_queue, "garden-failed")
    before = subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip()

    item, outcome = worker.process_next(clean_queue, _deps(target_repo, store))

    assert outcome.status == schema.FAILED
    assert item["error_category"] == "GateRefused"
    assert subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip() == before
    assert list_changes(clean_queue) == []
    run = ops.latest_run(clean_queue, "garden")
    assert run["status"] == ops.FAILED
    assert run["error"] == "job failed"
    assert "Missing page" not in json.dumps(run)


def test_model_repair_preserves_visibility(clean_queue, target_repo):
    store = evidence.MemoryEvidenceStore()
    path = _broken_page(target_repo, acl=("finance",))
    before = parse_page(
        "wiki/notes/Broken links.md", path.read_text(encoding="utf-8")
    )
    planner = ScriptedPlanner(
        repair_plan=RepairPlan(
            summary="Repaired the broken link",
            mutations=(
                RepairMutation(
                    path="wiki/notes/Broken links.md",
                    body="# Broken links\n\nThe page is self-contained.",
                    reason="Removed the dead link without changing visibility",
                ),
            ),
        )
    )
    _enqueue_garden(clean_queue, "garden-acl-refusal")
    item, outcome = worker.process_next(
        clean_queue,
        _deps(target_repo, store, planner),
    )

    assert outcome.status == schema.LANDED
    page = parse_page(
        "wiki/notes/Broken links.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Broken links.md"],
            cwd=target_repo,
            text=True,
        ),
    )
    assert page.acl == ("finance",)
    assert page.role == before.role
    assert page.title == before.title
    assert page.entities == before.entities
    assert page.sources == before.sources
    assert page.status == before.status
    assert page.page_id == before.page_id
    assert page.created == before.created
    assert page.updated == before.updated


def test_registry_and_absorbed_anchor_are_repaired_deterministically(
    clean_queue, target_repo
):
    store = evidence.MemoryEvidenceStore()
    now = dt.datetime(2026, 8, 24, tzinfo=dt.UTC)
    canonical = "ent_11111111-1111-4111-8111-111111111111"
    absorbed = "ent_22222222-2222-4222-8222-222222222222"
    record = EntityRecord(
        entity_id=canonical,
        path_slug="acme",
        entity_type="organization",
        created_at=now,
        updated_at=now,
        claims=(
            new_name_claim(
                "Acme",
                kind="preferred",
                acl=None,
                source="sources/2026/08/11111111-1111-4111-8111-111111111111.md",
                actor="marc",
                introduced_at=now,
            ),
        ),
        absorbed_ids=(absorbed,),
    )
    source_path = (
        target_repo
        / "sources"
        / "2026"
        / "08"
        / "11111111-1111-4111-8111-111111111111.md"
    )
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(
        "---\n"
        "id: 11111111-1111-4111-8111-111111111111\n"
        "type: source\n"
        "submitted_by: marc\n"
        "acl: null\n"
            "captured_at: '2026-08-24T00:00:00+00:00'\n"
            "origin: mcp\n"
            "participants: []\n"
            "artifacts:\n"
        "- sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "  bytes: 1\n"
        "  media_type: text/plain\n"
        "  readable_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            "  extractor: text\n"
            "  extractor_version: '1'\n"
            "  ocr_pages: []\n"
            "---\n\n# Captured source\n\nAcme.\n",
        encoding="utf-8",
    )
    write_records(str(target_repo), {canonical: record})
    note_path = target_repo / "wiki" / "notes" / "Acme.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(
        render_page(
            path="wiki/notes/Acme.md",
            role="note",
            title="Acme",
            body="# Acme\n\nCurrent knowledge.",
            acl=None,
            entities=(absorbed,),
            created=now.date(),
            updated=now.date(),
        ),
        encoding="utf-8",
    )
    (target_repo / "ops" / "entity-registry.json").write_text(
        '{"entities":{},"redirects":{},"version":1}\n',
        encoding="utf-8",
    )
    _commit(target_repo, "add deterministic drift")
    assert {item.code for item in check(str(target_repo))} == {
        "absorbed-anchor",
        "registry-drift",
    }
    _enqueue_garden(clean_queue, "garden-deterministic")

    item, outcome = worker.process_next(clean_queue, _deps(target_repo, store))

    assert outcome.status == schema.LANDED
    repaired = parse_page(
        "wiki/notes/Acme.md",
        subprocess.check_output(
            ["git", "show", "main:wiki/notes/Acme.md"],
            cwd=target_repo,
            text=True,
        ),
    )
    assert repaired.entities == (canonical,)
    registry = json.loads(
        subprocess.check_output(
            ["git", "show", "main:ops/entity-registry.json"],
            cwd=target_repo,
            text=True,
        )
    )
    assert registry["redirects"] == {absorbed: canonical}
    assert item["report"]["model_requests"] == 0
    assert item["report"]["clean"] is True
