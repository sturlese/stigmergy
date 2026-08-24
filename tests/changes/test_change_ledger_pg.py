import subprocess

from stigmergy.capture.evidence import MemoryEvidenceStore
from stigmergy.changes import diff
from stigmergy.changes import store as change_store
from stigmergy.knowledge import contradictions
from stigmergy.knowledge.contradictions import Contradiction
from stigmergy.knowledge.plan import ContradictionClaim


def _git(repo, *args):
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "brain"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Stigmergy")
    _git(repo, "config", "user.email", "writer@example.com")
    note = repo / "wiki" / "notes" / "Plan.md"
    note.parent.mkdir(parents=True)
    note.write_text("old\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    parent = _git(repo, "rev-parse", "HEAD")
    note.write_text("new\nsecond\n")
    source = repo / "sources" / "2026" / "08" / "capture.md"
    source.parent.mkdir(parents=True)
    source.write_text("source\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "capture")
    commit = _git(repo, "rev-parse", "HEAD")
    return repo, parent, commit


def test_manifest_is_deterministic_and_includes_hashes_and_counts(tmp_path):
    repo, parent, commit = _repo(tmp_path)

    manifest = diff.build_manifest(
        str(repo),
        parent,
        commit,
        reasons={"wiki/notes/Plan.md": "Updated the current plan"},
        default_reason="Archived the source",
    )

    assert [item.path for item in manifest] == [
        "sources/2026/08/capture.md",
        "wiki/notes/Plan.md",
    ]
    assert manifest[0].action == "created"
    assert manifest[0].page_role == "source"
    assert manifest[1].action == "updated"
    assert manifest[1].additions == 2
    assert manifest[1].deletions == 1
    assert manifest[1].before_sha256
    assert manifest[1].after_sha256
    assert manifest[1].contradictions_added == ()
    assert manifest[1].contradictions_resolved == ()


def test_manifest_records_exact_contradiction_ids_added_and_resolved(tmp_path):
    repo = tmp_path / "brain"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Stigmergy")
    _git(repo, "config", "user.email", "writer@example.com")
    path = repo / "wiki" / "notes" / "Plan.md"
    path.parent.mkdir(parents=True)
    first = Contradiction(
        contradiction_id="con_00000000-0000-4000-8000-000000000001",
        explanation="Two sources disagree.",
        claims=(
            ContradictionClaim(text="Launch Monday", source="sources/a.md"),
            ContradictionClaim(text="Launch Tuesday", source="sources/b.md"),
        ),
    )
    second = Contradiction(
        contradiction_id="con_00000000-0000-4000-8000-000000000002",
        explanation="Two sources disagree again.",
        claims=(
            ContradictionClaim(text="Launch early", source="sources/c.md"),
            ContradictionClaim(text="Launch late", source="sources/d.md"),
        ),
    )
    path.write_text(f"# Plan\n\n{contradictions.render(first)}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    parent = _git(repo, "rev-parse", "HEAD")
    path.write_text(f"# Plan\n\n{contradictions.render(second)}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "replace contradiction")
    commit = _git(repo, "rev-parse", "HEAD")

    manifest = diff.build_manifest(
        str(repo),
        parent,
        commit,
        default_reason="Updated the plan",
    )

    assert manifest[0].contradictions_added == (second.contradiction_id,)
    assert manifest[0].contradictions_resolved == (first.contradiction_id,)


def test_manifest_records_a_rename_as_delete_and_create(tmp_path):
    repo = tmp_path / "brain"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Stigmergy")
    _git(repo, "config", "user.email", "writer@example.com")
    old = repo / "wiki" / "notes" / "Old.md"
    old.parent.mkdir(parents=True)
    old.write_text("same bytes\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    parent = _git(repo, "rev-parse", "HEAD")
    new = old.with_name("New.md")
    old.rename(new)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "rename")
    commit = _git(repo, "rev-parse", "HEAD")

    patch = diff.exact_patch(str(repo), parent, commit).decode()
    manifest = diff.build_manifest(
        str(repo),
        parent,
        commit,
        default_reason="Renamed the page",
    )

    assert {(item.action, item.path) for item in manifest} == {
        ("deleted", "wiki/notes/Old.md"),
        ("created", "wiki/notes/New.md"),
    }
    assert set(diff.path_patches(patch)) == {
        "wiki/notes/Old.md",
        "wiki/notes/New.md",
    }


def test_change_record_is_unique_per_commit_and_patch_is_reconstructable(
    clean_queue, tmp_path
):
    repo, parent, commit = _repo(tmp_path)
    evidence = MemoryEvidenceStore()

    first = change_store.record_change(
        clean_queue,
        evidence,
        repo=str(repo),
        trigger="capture",
        actor="alice@example.com",
        parent_commit_sha=parent,
        commit_sha=commit,
        summary="Learned the launch plan",
    )
    second = change_store.record_change(
        clean_queue,
        evidence,
        repo=str(repo),
        trigger="capture",
        actor="alice@example.com",
        parent_commit_sha=parent,
        commit_sha=commit,
        summary="Learned the launch plan",
    )

    assert first.id == second.id
    assert first.commit_sha == commit
    assert first.exact_patch_bytes > 0
    cached = change_store.load_exact_patch(first, evidence, repo=str(repo))
    evidence.delete(first.exact_patch_ref)
    reconstructed = change_store.load_exact_patch(first, evidence, repo=str(repo))
    assert reconstructed == cached

    with clean_queue.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM knowledge_changes")
        assert cursor.fetchone()[0] == 1


def test_patch_is_split_by_unicode_paths_with_spaces():
    patch = (
        'diff --git "a/wiki/notes/Café plan.md" "b/wiki/notes/Café plan.md"\n'
        "index 1111111..2222222 100644\n"
        '--- "a/wiki/notes/Café plan.md"\n'
        '+++ "b/wiki/notes/Café plan.md"\n'
        "@@ -1 +1 @@\n-old\n+new\n"
        "diff --git a/sources/item.md b/sources/item.md\n"
        "new file mode 100644\n+source\n"
    )

    parts = diff.path_patches(patch)

    assert set(parts) == {"wiki/notes/Café plan.md", "sources/item.md"}
    assert "-old" in parts["wiki/notes/Café plan.md"]
    assert "+source" in parts["sources/item.md"]


def test_patch_is_split_when_git_leaves_spaced_paths_unquoted():
    patch = (
        "diff --git a/wiki/notes/Café plan.md b/wiki/notes/Café plan.md\n"
        "new file mode 100644\n"
        "+plan\n"
    )

    parts = diff.path_patches(patch)

    assert set(parts) == {"wiki/notes/Café plan.md"}
