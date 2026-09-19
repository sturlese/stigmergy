import subprocess
from pathlib import Path

from stigmergy.capture import evidence, schema
from stigmergy.capture.service import CaptureService
from stigmergy.changes.store import list_changes
from stigmergy.knowledge.plan import FilingPlan
from stigmergy.knowledge.planner import ScriptedPlanner
from stigmergy.knowledge.writer import WriterDeps
from stigmergy.librarian import config, worker


def _commit(repo: Path, message: str) -> None:
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


def _advance_remote(repo: Path, root: Path, mutate) -> Path:
    remote = root / "skill-contract-remote.git"
    clone = root / "skill-contract-remote"
    subprocess.run(["git", "init", "--bare", "-q", remote], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=repo, check=True)
    subprocess.run(["git", "clone", "-q", remote, clone], check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=clone, check=True)
    mutate(clone)
    _commit(clone, "advance remote fixture")
    subprocess.run(["git", "push", "-q", "origin", "main"], cwd=clone, check=True)
    return remote


class CountingPlanner(ScriptedPlanner):
    def __init__(self):
        super().__init__(FilingPlan(summary="Archive the immutable source only"))
        self.calls = 0

    def plan(self, **kwargs):
        self.calls += 1
        return super().plan(**kwargs)


def _enqueue_capture(conn, store, key: str) -> None:
    CaptureService(conn, store).capture_text(
        actor=schema.Actor(subject="marc", display_name="Marc"),
        audience=None,
        adapter="mcp",
        text="A durable source awaiting filing.",
        idempotency_key=key,
    )


def _process(conn, repo: Path, store, planner):
    return worker.process_next(
        conn,
        WriterDeps(
            config.Settings(repo=str(repo), branch="main", backend="scripted"),
            store,
            planner,
            str(repo),
        ),
    )


def test_remote_skill_change_is_rejected_before_planning_or_writing(clean_queue, target_repo, tmp_path):
    store = evidence.MemoryEvidenceStore()
    planner = CountingPlanner()
    remote = _advance_remote(
        target_repo,
        tmp_path,
        lambda clone: (clone / ".claude/skills/librarian/SKILL.md").write_text(
            "untrusted skill\n", encoding="utf-8"
        ),
    )
    remote_before = subprocess.check_output(
        ["git", "--git-dir", str(remote), "rev-parse", "main"], text=True
    ).strip()
    local_before = subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip()
    sources_before = subprocess.check_output(
        ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main", "sources"],
        text=True,
    )
    _enqueue_capture(clean_queue, store, "skill-contract-rejected")

    item, outcome = _process(clean_queue, target_repo, store, planner)

    assert outcome.status == schema.FAILED
    assert item["error_category"] == "KnowledgeContractError"
    assert planner.calls == 0
    assert list_changes(clean_queue) == []
    assert subprocess.check_output(
        ["git", "--git-dir", str(remote), "rev-parse", "main"], text=True
    ).strip() == remote_before
    assert subprocess.check_output(
        ["git", "rev-parse", "main"], cwd=target_repo, text=True
    ).strip() == local_before
    assert subprocess.check_output(
        ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main", "sources"],
        text=True,
    ) == sources_before


def test_remote_data_only_advance_with_matching_skill_is_accepted(clean_queue, target_repo, tmp_path):
    store = evidence.MemoryEvidenceStore()
    planner = CountingPlanner()
    remote = _advance_remote(
        target_repo,
        tmp_path,
        lambda clone: (clone / "ops/data-only.txt").write_text("ordinary data\n", encoding="utf-8"),
    )
    remote_base = subprocess.check_output(
        ["git", "--git-dir", str(remote), "rev-parse", "main"], text=True
    ).strip()
    _enqueue_capture(clean_queue, store, "skill-contract-data-only")

    item, outcome = _process(clean_queue, target_repo, store, planner)

    remote_after = subprocess.check_output(
        ["git", "--git-dir", str(remote), "rev-parse", "main"], text=True
    ).strip()
    assert outcome.status == schema.LANDED
    assert planner.calls == 1
    assert len(list_changes(clean_queue)) == 1
    assert remote_after == item["commit_sha"]
    assert subprocess.check_output(
        ["git", "--git-dir", str(remote), "rev-parse", f"{remote_after}^"], text=True
    ).strip() == remote_base
