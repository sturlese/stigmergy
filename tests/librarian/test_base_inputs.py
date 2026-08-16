"""`acl_rules`/the registry loader/the linter read `ops/acl.json`, `ops/entity-registry.json` and
`.claude/tools/stigmergy_lint.py` **at the base commit** (`gitcmd.BaseRef.sha`), never off the
working tree — in every mode, including a local laptop run where a working-tree read used to be a
convenience.

Pure git, no Postgres, no worker loop: `base_inputs.read_at`/`load_acl`/`load_registry`/
`linter_at`/`check_linter_at` take a `(repo, base)` pair, so the property — "an uncommitted edit
does not change what these functions return" — is provable with two commits and a dirty working
tree, without a claim, a double agent or a database. Mirrors the skill-at-base test
(`worker._check_skill_at`) this module's own docstring names as the precedent it is symmetric with.
"""
import os
import subprocess

import pytest

from stigmergy.librarian import base_inputs, config, gitcmd
from stigmergy.librarian.errors import LibrarianConfigError

ACL_V1 = '{"default": [], "rules": [{"path": "wiki/**", "acl": []}]}'
ACL_V2_NARROWED = '{"default": [], "rules": [{"path": "wiki/**", "acl": ["leadership"]}]}'

REGISTRY_V1 = '{"entities": {"acme": {"name": "Acme", "type": "organization", "aliases": []}}}'
REGISTRY_V2 = ('{"entities": {"acme": {"name": "Acme", "type": "organization", "aliases": []}, '
              '"globex": {"name": "Globex", "type": "organization", "aliases": []}}}')

LINTER_V1 = "#!/usr/bin/env python3\nVERSION = 1\n"
LINTER_V2 = "#!/usr/bin/env python3\nVERSION = 2\n"


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _write(repo: str, relpath: str, text: str) -> None:
    path = os.path.join(repo, *relpath.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


@pytest.fixture()
def two_commit_repo(tmp_path):
    """A plain (non-bare) repo with TWO commits: `sha1` carries the V1 inputs, `sha2` narrows the
    ACL and adds an entity. Returns `(repo_path, sha1, sha2)`. No bare remote at all — `BaseRef` is
    constructed by hand below, so nothing here depends on `git fetch` semantics."""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git("init", "--quiet", "-b", "main", repo, cwd=str(tmp_path))
    _git("config", "user.name", "fixture", cwd=repo)
    _git("config", "user.email", "fixture@example.com", cwd=repo)

    _write(repo, config.ACL_RELPATH, ACL_V1)
    _write(repo, config.REGISTRY_RELPATH, REGISTRY_V1)
    _write(repo, config.LINTER_RELPATH, LINTER_V1)
    _git("add", "-A", cwd=repo)
    _git("commit", "--quiet", "-m", "v1", cwd=repo)
    sha1 = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    _write(repo, config.ACL_RELPATH, ACL_V2_NARROWED)
    _write(repo, config.REGISTRY_RELPATH, REGISTRY_V2)
    _write(repo, config.LINTER_RELPATH, LINTER_V2)
    _git("add", "-A", cwd=repo)
    _git("commit", "--quiet", "-m", "v2: narrow ACL, add Globex, bump linter", cwd=repo)
    sha2 = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    return repo, sha1, sha2


def _base(sha: str) -> gitcmd.BaseRef:
    """A `BaseRef` built by hand — `base_inputs` only ever reads `.sha`, so nothing here needs a
    real `origin/<branch>` to point at."""
    return gitcmd.BaseRef(sha=sha, ref="main", remote=False)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The core property: an UNCOMMITTED working-tree edit changes nothing a run at a fixed base sees.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_an_uncommitted_acl_edit_does_not_change_what_load_acl_reads(two_commit_repo):
    repo, sha1, _sha2 = two_commit_repo
    _write(repo, config.ACL_RELPATH, ACL_V2_NARROWED)     # dirty the working tree, UNCOMMITTED

    resolved = base_inputs.load_acl(repo, _base(sha1))
    # V1 has no audience restriction at all; the (uncommitted) V2 narrows to "leadership" — if the
    # working tree were consulted, this would come back scoped.
    from stigmergy.librarian import acl_rules
    labels = acl_rules.resolve(resolved, "wiki/notes/Anything.md")
    assert labels in (None, [])


def test_an_uncommitted_registry_edit_does_not_change_what_load_registry_reads(two_commit_repo):
    repo, sha1, _sha2 = two_commit_repo
    _write(repo, config.REGISTRY_RELPATH, REGISTRY_V2)    # dirty the working tree, UNCOMMITTED

    registry = base_inputs.load_registry(repo, _base(sha1))
    assert registry.canonical_id("Globex") is None        # V2-only entity must not be visible
    assert registry.canonical_id("Acme") == "acme"


def test_an_uncommitted_linter_edit_does_not_change_what_linter_at_materializes(two_commit_repo):
    repo, sha1, _sha2 = two_commit_repo
    _write(repo, config.LINTER_RELPATH, LINTER_V2)        # dirty the working tree, UNCOMMITTED

    with base_inputs.linter_at(repo, _base(sha1)) as path, open(path, encoding="utf-8") as f:
        materialized = f.read()
    assert materialized == LINTER_V1
    assert "VERSION = 2" not in materialized


# ── the SECOND commit's inputs really are reachable — proving the base moved, not merely that ────
# ── V1 stuck around by coincidence ────────────────────────────────────────────────────────────
def test_reading_at_the_second_commit_sees_the_narrowed_acl_and_the_new_entity(two_commit_repo):
    """The per-item re-read's own property, at the level `base_inputs` is pure enough to prove
    without a
    worker: two commits, the second narrowing `ops/acl.json`, and the read that resolves `base` to
    the second sha sees the SECOND commit's audiences — not because a long-lived process re-reads
    a file on disk, but because it is handed a NEW `base.sha` each time (exactly what
    `processing.process_item`'s per-item `gitcmd.base_ref(...)` call supplies)."""
    repo, _sha1, sha2 = two_commit_repo
    from stigmergy.librarian import acl_rules

    resolved = base_inputs.load_acl(repo, _base(sha2))
    assert acl_rules.resolve(resolved, "wiki/notes/Anything.md") == ["leadership"]

    registry = base_inputs.load_registry(repo, _base(sha2))
    assert registry.canonical_id("Globex") == "globex"

    with base_inputs.linter_at(repo, _base(sha2)) as path, open(path, encoding="utf-8") as f:
        assert "VERSION = 2" in f.read()


def test_the_same_process_reading_first_base_one_then_base_two_sees_each_commits_own_values(
        two_commit_repo):
    """No restart, one process, two `base_inputs` calls in a row — the shape `deps = dataclasses.
    replace(deps, ...)` takes per item in `processing.process_item`, proven directly: reading at
    `sha1` then `sha2` from the SAME repo path, with nothing rebuilt in between, gives two
    genuinely different answers."""
    repo, sha1, sha2 = two_commit_repo
    from stigmergy.librarian import acl_rules

    first = acl_rules.resolve(base_inputs.load_acl(repo, _base(sha1)),
                              "wiki/notes/Anything.md")
    second = acl_rules.resolve(base_inputs.load_acl(repo, _base(sha2)),
                              "wiki/notes/Anything.md")
    assert first in (None, [])
    assert second == ["leadership"]


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Missing keeps its meaning in every case (module docstring)
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_a_commit_with_no_acl_file_is_an_open_corpus(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git("init", "--quiet", "-b", "main", repo, cwd=str(tmp_path))
    _git("config", "user.name", "fixture", cwd=repo)
    _git("config", "user.email", "fixture@example.com", cwd=repo)
    _write(repo, "README.md", "x\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "--quiet", "-m", "no acl.json at all", cwd=repo)
    sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    assert base_inputs.load_acl(repo, _base(sha)) is None


def test_a_commit_with_no_registry_file_is_an_empty_registry(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git("init", "--quiet", "-b", "main", repo, cwd=str(tmp_path))
    _git("config", "user.name", "fixture", cwd=repo)
    _git("config", "user.email", "fixture@example.com", cwd=repo)
    _write(repo, "README.md", "x\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "--quiet", "-m", "no registry at all", cwd=repo)
    sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    registry = base_inputs.load_registry(repo, _base(sha))
    assert registry.entities == {}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# `ops/stewards.json`, read at the base commit like the three inputs above.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _committed_repo(tmp_path, relpath: str | None, content: str | None) -> tuple[str, str]:
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git("init", "--quiet", "-b", "main", repo, cwd=str(tmp_path))
    _git("config", "user.name", "fixture", cwd=repo)
    _git("config", "user.email", "fixture@example.com", cwd=repo)
    _write(repo, "README.md", "x\n")
    if relpath is not None:
        _write(repo, relpath, content)
    _git("add", "-A", cwd=repo)
    _git("commit", "--quiet", "-m", "seed", cwd=repo)
    sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    return repo, sha


def test_a_commit_with_no_stewards_file_resolves_to_an_empty_map(tmp_path):
    repo, sha = _committed_repo(tmp_path, None, None)
    assert base_inputs.load_stewards(repo, _base(sha)) == {}


def test_load_stewards_reads_the_scope_to_emails_map(tmp_path):
    content = '{"*": ["steward@example.com"], "wiki/finance/": ["ana@example.com"]}'
    repo, sha = _committed_repo(tmp_path, config.STEWARDS_RELPATH, content)
    stewards = base_inputs.load_stewards(repo, _base(sha))
    assert stewards["*"] == ["steward@example.com"]
    assert stewards["wiki/finance/"] == ["ana@example.com"]


def test_load_stewards_raises_on_malformed_json(tmp_path):
    repo, sha = _committed_repo(tmp_path, config.STEWARDS_RELPATH, "{not json")
    with pytest.raises(LibrarianConfigError, match="not valid JSON"):
        base_inputs.load_stewards(repo, _base(sha))


def test_load_stewards_raises_when_not_an_object(tmp_path):
    repo, sha = _committed_repo(tmp_path, config.STEWARDS_RELPATH, '["not", "a", "map"]')
    with pytest.raises(LibrarianConfigError, match="must be an object"):
        base_inputs.load_stewards(repo, _base(sha))


def test_an_uncommitted_stewards_edit_does_not_change_what_load_stewards_reads(tmp_path):
    content_v1 = '{"*": ["steward@example.com"]}'
    repo, sha1 = _committed_repo(tmp_path, config.STEWARDS_RELPATH, content_v1)
    _write(repo, config.STEWARDS_RELPATH, '{"*": ["someone-else@example.com"]}')  # UNCOMMITTED

    stewards = base_inputs.load_stewards(repo, _base(sha1))
    assert stewards["*"] == ["steward@example.com"]


def test_a_commit_with_no_linter_refuses_loudly_naming_the_locator(two_commit_repo):
    repo, sha1, _sha2 = two_commit_repo
    _git("rm", "--quiet", config.LINTER_RELPATH, cwd=repo)
    _git("commit", "--quiet", "-m", "drop the linter", cwd=repo)
    sha_no_linter = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    with pytest.raises(LibrarianConfigError, match="not in the commit"):
        base_inputs.check_linter_at(repo, _base(sha_no_linter))
    with pytest.raises(LibrarianConfigError, match="not in the commit"), \
            base_inputs.linter_at(repo, _base(sha_no_linter)):
        pass

    # benign twin: the earlier commit (which HAD the linter) is unaffected by the later removal
    base_inputs.check_linter_at(repo, _base(sha1))    # must not raise


def test_a_malformed_acl_at_base_is_refused_with_the_locator_naming_the_commit(two_commit_repo):
    repo, sha1, _sha2 = two_commit_repo
    _write(repo, config.ACL_RELPATH, "not json at all")
    _git("add", "-A", cwd=repo)
    _git("commit", "--quiet", "-m", "break the acl config", cwd=repo)
    sha_broken = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    with pytest.raises(LibrarianConfigError, match=sha_broken[:12]):
        base_inputs.load_acl(repo, _base(sha_broken))
    # benign twin: sha1's own (valid) ACL is unaffected
    base_inputs.load_acl(repo, _base(sha1))   # must not raise


def test_a_stewards_file_that_is_not_utf8_is_unreadable_not_a_raw_decode_error(tmp_path):
    """An unreadable stewards map is a NAMED config refusal, whatever makes it unreadable.
    OLD BEHAVIOUR: non-UTF-8 bytes raised `UnicodeDecodeError` — a `ValueError`, not an
    `OSError` — so it escaped this loader's own promise and, downstream, escaped
    `server.review.is_steward`'s fail-closed catch, swallowing a steward's click in silence."""
    path = tmp_path / "stewards.json"
    path.write_bytes(b'{"*": ["ana@example.com"]}\xff\xfe')
    with pytest.raises(LibrarianConfigError, match="could not be read"):
        base_inputs.load_stewards_file(str(path))
