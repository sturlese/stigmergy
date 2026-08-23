"""Non-fixture test support for the removal suite: the Postgres seam with the ledger this package
writes, and the rule every refusal published to a person is held to.

**Real git, real Postgres, real gates** — a double stands in for the model and for nothing else. A
faked diff would prove nothing about `gate_body_rewrite`, and a faked gitleaks nothing about the
secrets veto.

A plain module rather than a `conftest.py`, the same reasoning `tests/librarian/support.py` gives
for itself: fixtures are per-package pytest wiring, this is plain code any file can import.
"""
import json
import os
import re

from stigmergy.capture import schema as capture_schema
from stigmergy.librarian import gitcmd
from stigmergy.repair import brief
from stigmergy.repair.schema import ensure_repair_schema
from tests import testdb
from tests.librarian import support as librarian_support


def connect_or_skip():
    conn = testdb.connect_or_skip("repair")
    capture_schema.ensure_capture_schema(conn)      # capture_queue, job_runs
    ensure_repair_schema(conn)                      # repairs — this package's own ledger
    return conn


def clean(conn) -> None:
    """Empty every table this suite's own writes could have touched — test isolation only, the
    same posture every sibling suite takes."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM repairs")
        cur.execute("DELETE FROM capture_queue")
        cur.execute("DELETE FROM job_runs")


# ── the knowledge repo the removal road is exercised against ─────────────────────────────────
# The `removal-sweep` skill, as a FIXTURE. The real one is versioned in the knowledge repo and
# read at the base commit the removal runs against (`brief.SKILL_RELPATH`), which is the whole
# point of the design — so the suite carries its own, deliberately short, and never a frozen copy
# of the real one: what the code owes is the READ path and the refusal when it is absent, and
# pinning the brain repo's prose here would be pinning somebody else's editorial decisions.
FIXTURE_SKILL = """---
name: removal-sweep
description: fixture stand-in for the knowledge repo's own procedure
---

# removal-sweep (test fixture)

Write the pages a removal leaves behind. Reconcile the references and change nothing else.
"""

BRANCH = "main"


def build_repo(tmp_path, *, with_skill: bool = True):
    """A bare remote plus a clone of the fixture knowledge repo, with the sweep writer's skill
    committed into it. `with_skill=False` is the fixture for the named config refusal."""
    env = librarian_support.build_repo(str(tmp_path / "git"))
    if with_skill:
        write_skill(env.repo)
        librarian_support.commit_and_push(env.repo, "test: add the removal-sweep skill")
    return env


def write_skill(repo: str, text: str = FIXTURE_SKILL) -> str:
    path = brief.skill_path(repo)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


# ── what actually landed: the bare remote, read as git ───────────────────────────────────────
# Never the working checkout. The worker writes in a detached worktree and pushes; the checkout
# `repo_env.repo` names is left on whatever commit the fixture last committed, so a test asserting
# against it would be reading the corpus BEFORE the removal rather than after it.
def remote_head(bare: str, ref: str = BRANCH) -> str:
    return librarian_support.branch_sha(bare, ref)


def remote_page(bare: str, path: str, ref: str = BRANCH) -> str:
    return librarian_support.read_filed_page(bare, ref, path)


def remote_paths(bare: str, ref: str = BRANCH) -> list[str]:
    """Every path the remote's tree holds — `-z`, because a page name carries spaces routinely."""
    out = gitcmd.run("ls-tree", "-r", "-z", "--name-only", ref, cwd=bare).stdout
    return [path for path in out.split("\0") if path]


def commit_message(bare: str, ref: str = BRANCH) -> str:
    return gitcmd.run("log", "-1", "--format=%B", ref, cwd=bare).stdout


def commit_author(bare: str, ref: str = BRANCH) -> str:
    return gitcmd.run("log", "-1", "--format=%an <%ae>", ref, cwd=bare).stdout.strip()


def commit_count(bare: str, ref: str = BRANCH) -> int:
    return len(gitcmd.run("log", "--format=%H", ref, cwd=bare).stdout.split())


# ── a page worth removing, and pages that mention it ──────────────────────────────────────────
# Deliberately NO `entity:` declaration on any of them, exactly as the fixture repo's own
# hand-authored pages have none: an anchor would make every removal test depend on the entity
# registry as well, and `gate_contract` would then surface a registry finding on any page a sweep
# touched.
DOOMED_STEM = "Superseded Renewal Memo"
DOOMED_PAGE = f"wiki/notes/{DOOMED_STEM}.md"


def write_note(env, title: str, *, related=(), body: str = "", push: bool = True) -> str:
    """One hand-authored `wiki/notes/` page with the `related:` list a test needs.

    The body is padded past the contract linter's thirty-line floor the same way the fixture repo's
    own pages are: a `size` warning is only a note, but a fixture that trips one teaches a reader
    to skim the gate output, which is where a real veto hides.
    """
    front = ["type: note", f'title: "{title}"', "status: developing", "created: 2026-02-01",
             "updated: 2026-02-01", "tags: [note]",
             f"related: {json.dumps([f'[[{name}]]' for name in related], ensure_ascii=False)}",
             "sources: []"]
    filler = "\n".join(f"- line {n} of the padding this page carries so the contract linter's "
                       f"thirty-line floor is met without a warning." for n in range(1, 26))
    relpath = f"wiki/notes/{title}.md"
    path = os.path.join(env.repo, *relpath.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = body or f"# {title}\n\n## What it says\n\n{filler}\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write("---\n" + "\n".join(front) + "\n---\n\n" + text)
    if push:
        librarian_support.commit_and_push(env.repo, f"test: add {title}")
    return relpath


def seed_deletion_corpus(env) -> dict[str, str]:
    """The shape every removal test needs: one doomed page and three that mention it three
    different ways — a `related:` entry beside a surviving one, a body wikilink, and a page whose
    ONLY reference is a `related:` entry (so its scrub removes a line and adds none).

    Returns `{label: relpath}` so a test names the page by what it is FOR.
    """
    doomed = write_note(env, DOOMED_STEM, push=False)
    write_note(env, "Keeps A Link", related=[DOOMED_STEM, "Existing Note"], push=False)
    write_note(env, "Mentions It In Prose", push=False,
               body=f"# Mentions It In Prose\n\n## What it says\n\nThe broker agreed, as "
                    f"[[{DOOMED_STEM}]] records, and the volumes held.\n\n"
                    + "\n".join(f"- padding line {n} so the linter's floor is met."
                                for n in range(1, 26)) + "\n")
    write_note(env, "Only A Related Entry", related=[DOOMED_STEM], push=False)
    librarian_support.commit_and_push(env.repo, "test: seed the deletion corpus")
    return {"doomed": doomed,
            "keeps_a_link": "wiki/notes/Keeps A Link.md",
            "in_prose": "wiki/notes/Mentions It In Prose.md",
            "only_related": "wiki/notes/Only A Related Entry.md"}


def stem(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".md")


def page_text(repo: str, path: str) -> str:
    with open(os.path.join(repo, path), encoding="utf-8") as f:
        return f.read()


# ── what a refusal published to a PERSON may say ──────────────────────────────────────────────
# The rule every refusal this package composes is held to, shared by the suites that publish one
# (`tests/server/test_delete_pages_pg.py`, `tests/librarian/test_delete_processing_pg.py`): a
# removal's refusal travels verbatim into the report whoever asked reads back.
#
# Strictly stronger than the `/tmp` · `/private/` · `/Users/` roots the issue named: any token
# STARTING with `/` is an absolute path on some host. A repo-relative path
# (`ops/templates/entity.md`) and a spaced-out alternative (`list_entities / describe_entity`) are
# both deliberately outside it — they name nothing about the host.
_ABSOLUTE_PATH = re.compile(r"(?:^|[\s(\[\"'`])/\S")


def assert_person_facing(message: str) -> None:
    """Raise unless `message` is safe to publish — over MCP, on the console, or in a stored row."""
    leak = _ABSOLUTE_PATH.search(message)
    assert not leak, (
        f"a server-door refusal named an absolute path ({message[leak.start():leak.start() + 60]!r} "
        f"…) — the person reading this over MCP has no filesystem to find it on, and it is the "
        f"server host's temp directory. Log it with `exc_info=True` and map the type to a written "
        f"sentence:\n  {message}")
    assert "git -C" not in message, (
        f"a server-door refusal told a person to run a git command — `git -C` names a clone that "
        f"exists only inside the server process and is deleted before anyone reads this:\n  "
        f"{message}")
