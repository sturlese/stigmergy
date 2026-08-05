"""Fixtures for the views suite: a throwaway knowledge repo (real git bare remote + a steward's
clone), seeded with an entity and a few anchored pages. Same "real git, always" posture as
`tests/entities/conftest.py` — never fake what you are claiming to prove, so every test that
exercises `views.writer` works against an actual `git init --bare` remote, never a faked diff.
"""
import os
import subprocess

import pytest

from stigmergy.kernel.registry import Registry

STEWARD_NAME = "Test Steward"
STEWARD_EMAIL = "steward@example.com"
_COMMIT_ENV = {"GIT_AUTHOR_NAME": STEWARD_NAME, "GIT_AUTHOR_EMAIL": STEWARD_EMAIL,
              "GIT_COMMITTER_NAME": STEWARD_NAME, "GIT_COMMITTER_EMAIL": STEWARD_EMAIL}


def git(*args, cwd, check=True, env=None):
    full_env = {**os.environ, **env} if env else None
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=check, env=full_env)


def entity_page(name: str, entity_id: str) -> str:
    return (f'---\ntype: entity\ntitle: "{name}"\nentity_type: organization\nrole: ""\n'
           f'status: developing\naliases: []\nentity: [{entity_id}]\ncreated: 2026-07-01\n'
           f'updated: 2026-07-01\ntags: [entity]\nrelated: []\nsources: []\n---\n\n# {name}\n\n'
           f'## Facts\n')


def decision_page(title: str, entity_id: str, *, as_of: str, mentions_entity_page: str,
                  acl: list | None = None) -> str:
    """`wiki/decisions/` is an AUTHORED zone (not `sources/`/`views/`), so the frozen
    linter's `REQUIRED_FIELDS` — not the machine-page `MACHINE_REQUIRED_FIELDS` group — applies:
    `created`/`updated`/`status` are mandatory, same as `entity_page` below already carries.
    `created`/`updated` reuse `as_of` (the decision's own date is the only date this fixture
    otherwise tracks) rather than inventing a second, unrelated date the tests never assert on."""
    acl_line = f"acl: [{', '.join(acl)}]\n" if acl is not None else ""
    return (f'---\ntype: decision\ntitle: "{title}"\nentity: [{entity_id}]\nas_of: "{as_of}"\n'
           f'created: "{as_of}"\nupdated: "{as_of}"\nstatus: developing\n'
           f'{acl_line}tags: [decision]\n---\n\n# {title}\n\nSomething happened with '
           f'[[{mentions_entity_page}]].\n')


def build_repo(root: str, *, entity_id: str = "acme-corp", entity_name: str = "Acme Corp",
              n_decisions: int = 2, decision_acls: list | None = None):
    """`(remote, clone)`: a fresh bare remote plus a steward's clone, seeded with one registered
    entity, its own entity page (self-anchored) and `n_decisions` decision pages anchored to it,
    each linking back to the entity page (so Backlinks has something to find), committed and
    pushed once."""
    remote = os.path.join(root, "remote.git")
    clone = os.path.join(root, "clone")
    os.makedirs(remote, exist_ok=True)
    git("init", "--bare", "--quiet", "--initial-branch=main", remote, cwd=root)

    os.makedirs(os.path.join(clone, "wiki", "entities"), exist_ok=True)
    os.makedirs(os.path.join(clone, "wiki", "decisions"), exist_ok=True)
    os.makedirs(os.path.join(clone, "ops"), exist_ok=True)
    git("init", "--quiet", "--initial-branch=main", clone, cwd=root)
    git("config", "user.name", STEWARD_NAME, cwd=clone)
    git("config", "user.email", STEWARD_EMAIL, cwd=clone)

    with open(os.path.join(clone, "wiki", "entities", f"{entity_name}.md"), "w") as f:
        f.write(entity_page(entity_name, entity_id))

    for i in range(n_decisions):
        acl = decision_acls[i] if decision_acls else None
        title = f"Decision {i + 1}"
        with open(os.path.join(clone, "wiki", "decisions", f"decision-{i + 1}.md"), "w") as f:
            f.write(decision_page(title, entity_id, as_of=f"2026-07-{20 + i:02d}",
                                  mentions_entity_page=entity_name, acl=acl))

    git("add", "--all", cwd=clone)
    git("commit", "--quiet", "-m", "chore: seed the fixture knowledge repo", cwd=clone,
       env=_COMMIT_ENV)
    git("remote", "add", "origin", remote, cwd=clone)
    git("push", "--quiet", "-u", "origin", "main", cwd=clone)
    return remote, clone


def registry_of(entity_id: str = "acme-corp", entity_name: str = "Acme Corp") -> Registry:
    reg = Registry()
    reg.entities[entity_id] = {"name": entity_name, "type": "organization", "aliases": []}
    reg.by_alias[entity_id] = entity_id
    reg.by_alias[entity_name.lower()] = entity_id
    return reg


def remote_log(remote: str, ref: str = "main") -> str:
    return git("log", "--oneline", ref, cwd=remote).stdout


def remote_files(remote: str, ref: str = "main") -> list[str]:
    return git("ls-tree", "-r", "--name-only", ref, cwd=remote).stdout.splitlines()


@pytest.fixture()
def repo(tmp_path):
    """`(remote, clone)` — see `build_repo`. Fresh per test, 2 decisions, no ACL."""
    return build_repo(str(tmp_path / "git"))


@pytest.fixture(autouse=True)
def _fake_llm(monkeypatch):
    """Every views test runs the bounded agent offline (`CLEAN_LLM=fake`) unless a test
    overrides it (the ACL/sabotage tests need no agent at all; the synthesis tests override to
    `fake-flawed` where the hallucination path is exercised)."""
    monkeypatch.setenv("CLEAN_LLM", "fake")


class FakeConn:
    """A `job_runs` write needs a real Postgres `conn` in the live system
    (`capture.ops.record_job_run`); this suite is otherwise entirely offline, so this double lets
    `ops.job_run`'s context manager — and a direct `ops.record_job_run` call, the
    `KeyboardInterrupt` path — run their `try/except`/yield contract without one. It records every
    attempted write instead of touching a database: good enough to assert "a job_runs write was
    attempted, with this shape", not to assert what Postgres actually stored, which belongs to a
    `_pg.py` suite. Shared by `test_regenerate.py` and `test_cli.py` rather than duplicated."""

    def __init__(self):
        self.executed = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def fetchone(self):
        return (1,)
