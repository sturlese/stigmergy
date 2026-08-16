"""Orchestration: staleness, --force, removal, refusals, one commit per entity, job_runs. Real
git throughout — never fake what you are claiming to prove."""
import asyncio
import os
import pathlib

import pytest

from stigmergy.kernel.registry import Registry
from stigmergy.views import regenerate, skeleton
from tests.views.conftest import (
    FakeConn,
    build_repo,
    git,
    registry_of,
    remote_files,
    remote_log,
)


def test_write_produces_the_skeleton_sections(repo):
    remote, clone = repo
    registry = registry_of()
    outcome = asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry))
    assert outcome.action == "written"
    assert "views/acme-corp.md" in remote_files(remote)
    page = (open(os.path.join(clone, "views", "acme-corp.md")).read())
    assert "## Timeline" in page and "## Backlinks" in page
    assert "## Current facts" not in page   # there is no facts store, so no section for one
    assert "## Synthesis" in page


def test_unchanged_member_set_is_an_honest_no_op(repo):
    remote, clone = repo
    registry = registry_of()
    asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry))
    log_before = remote_log(remote)
    mtime_before = os.path.getmtime(os.path.join(clone, "views", "acme-corp.md"))

    outcome2 = asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry))
    assert outcome2.action == "unchanged"
    assert outcome2.commit is None
    assert remote_log(remote) == log_before                       # nothing committed
    assert os.path.getmtime(os.path.join(clone, "views", "acme-corp.md")) == mtime_before


def test_force_bypasses_the_staleness_check(repo):
    remote, clone = repo
    registry = registry_of()
    asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry))
    log_before = remote_log(remote)
    outcome2 = asyncio.run(regenerate.regenerate_entity(
        clone, "acme-corp", registry=registry, force=True))
    assert outcome2.action == "written"
    assert remote_log(remote) != log_before                       # --force DID write again


def test_last_member_vanishing_removes_the_view(tmp_path):
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    registry = registry_of()
    asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry))
    assert "views/acme-corp.md" in remote_files(remote)

    for f in os.listdir(os.path.join(clone, "wiki", "decisions")):
        os.remove(os.path.join(clone, "wiki", "decisions", f))
    os.remove(os.path.join(clone, "wiki", "entities", "Acme Corp.md"))
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "remove members"], cwd=clone, check=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "Test Steward",
                        "GIT_AUTHOR_EMAIL": "steward@example.com",
                        "GIT_COMMITTER_NAME": "Test Steward",
                        "GIT_COMMITTER_EMAIL": "steward@example.com"})
    subprocess.run(["git", "push", "-q"], cwd=clone, check=True)

    outcome = asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry))
    assert outcome.action == "removed"
    assert "views/acme-corp.md" not in remote_files(remote)
    assert not os.path.exists(os.path.join(clone, "views", "acme-corp.md"))


def test_refuses_an_unknown_entity_id(repo):
    remote, clone = repo
    outcome = asyncio.run(regenerate.regenerate_entity(clone, "not-registered", registry=registry_of()))
    assert outcome.action == "refused-unknown-entity"


def test_refuses_a_registered_entity_with_no_members(tmp_path):
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=0)
    # give the registry a SECOND entity with no page at all
    registry = registry_of()
    registry.entities["ghost-co"] = {"name": "Ghost Co", "type": "organization", "aliases": []}
    outcome = asyncio.run(regenerate.regenerate_entity(clone, "ghost-co", registry=registry))
    assert outcome.action == "refused-no-members"


def test_one_commit_per_entity_in_a_batch(tmp_path):
    """One commit per entity, never one commit for the whole sweep."""
    remote, clone = build_repo(str(tmp_path / "git"), entity_id="acme-corp",
                               entity_name="Acme Corp", n_decisions=1)
    # add a second, independently anchored entity into the same clone
    import subprocess
    with open(os.path.join(clone, "wiki", "entities", "Globex.md"), "w") as f:
        f.write('---\ntype: entity\ntitle: "Globex"\nentity_type: organization\n'
               'entity: [globex]\ntags: [entity]\ncreated: "2026-07-01"\n'
               'updated: "2026-07-01"\nstatus: developing\n---\n\n# Globex\n')
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add globex"], cwd=clone, check=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "Test Steward",
                        "GIT_AUTHOR_EMAIL": "steward@example.com",
                        "GIT_COMMITTER_NAME": "Test Steward",
                        "GIT_COMMITTER_EMAIL": "steward@example.com"})
    subprocess.run(["git", "push", "-q"], cwd=clone, check=True)

    registry = registry_of()
    registry.entities["globex"] = {"name": "Globex", "type": "organization", "aliases": []}

    conn = FakeConn()
    result = asyncio.run(regenerate.run(clone, conn, ["acme-corp", "globex"], registry=registry))
    assert result.stats["written"] == 2
    log = remote_log(remote)
    # two distinct commits, one per entity — not one combined commit
    assert log.count("chore(views):") == 2
    assert conn.executed, "job_runs write was never attempted"


def test_list_stale_and_all_populations(repo):
    remote, clone = repo
    registry = registry_of()
    assert regenerate.list_all_anchored_entities(clone) == ["acme-corp"]
    assert regenerate.list_stale_entities(clone) == []     # no view exists yet -> not "stale"
    asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry))
    assert regenerate.list_stale_entities(clone) == []     # freshly written -> not stale


# ── a view orphaned by DE-REGISTRATION ──────────────────────────────────────────────────────────
def test_a_deregistered_entitys_view_is_removed_not_refused_forever(repo):
    """`regenerate_entity` used to refuse an unknown entity id BEFORE the no-members removal path
    ever ran, so a view whose entity was later removed from the registry entirely (distinct from
    "the last member vanishes while the entity is still registered") stayed committed, indexed and
    searchable forever: refused every time via `--entity`, and listed stale every run via
    `--stale` with no way to act on it."""
    remote, clone = repo
    registry = registry_of()
    asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry))
    assert "views/acme-corp.md" in remote_files(remote)

    deregistered = Registry()   # the SAME entity id, now absent from the registry entirely
    outcome = asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=deregistered))
    assert outcome.action == "removed"
    assert "views/acme-corp.md" not in remote_files(remote)
    assert not os.path.exists(os.path.join(clone, "views", "acme-corp.md"))

    # a SECOND run against the still-de-registered entity must not regress to the old refusal —
    # the view is already gone, so this is the ordinary "no view, unregistered" refusal,
    # never a silent no-op that could mask the removal not having happened.
    again = asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=deregistered))
    assert again.action == "refused-unknown-entity"


def test_a_removal_carries_which_of_its_two_causes_it_was(tmp_path):
    """**Old behaviour: `RegenOutcome.message` was `""` for every removal**, so the only surface
    that told a steward WHY a view had gone was `views/cli.py`, which hardcoded one sentence ("the
    last page anchored to it is gone") and printed it down both roads — telling the steward of a
    just-de-registered entity that its pages had vanished. They had not; the entity had. Two
    different facts, two different next actions, and the two call sites already knew which was
    which (their commit messages say so).

    Asserted on the CAUSE words, not on the full sentence: this pins that the outcome distinguishes
    the two roads, without freezing an operator-facing wording that is free to be improved.
    """
    # road 1: the entity was de-registered, its pages untouched
    remote, clone = build_repo(str(tmp_path / "dereg"))
    asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry_of()))
    deregistered = asyncio.run(
        regenerate.regenerate_entity(clone, "acme-corp", registry=Registry()))
    assert deregistered.action == "removed"
    assert "de-registered" in deregistered.message
    assert "no anchored pages remain" not in deregistered.message

    # road 2: the entity is still registered, its last anchored page went away
    remote2, clone2 = build_repo(str(tmp_path / "members"), n_decisions=1)
    registry = registry_of()
    asyncio.run(regenerate.regenerate_entity(clone2, "acme-corp", registry=registry))
    os.remove(os.path.join(clone2, "wiki", "decisions", "decision-1.md"))
    os.remove(os.path.join(clone2, "wiki", "entities", "Acme Corp.md"))
    git("add", "--all", cwd=clone2)
    git("commit", "--quiet", "-m", "chore: remove every member", cwd=clone2,
       env={"GIT_AUTHOR_NAME": "Test Steward", "GIT_AUTHOR_EMAIL": "steward@example.com",
            "GIT_COMMITTER_NAME": "Test Steward", "GIT_COMMITTER_EMAIL": "steward@example.com"})
    git("push", "--quiet", cwd=clone2)

    gone = asyncio.run(regenerate.regenerate_entity(clone2, "acme-corp", registry=registry))
    assert gone.action == "removed"
    assert "no anchored pages remain" in gone.message
    assert "de-registered" not in gone.message


# ── job_runs must not lie by omission on partial failure ────────────────────────────────────────
def test_a_keyboardinterrupt_mid_batch_still_records_the_entities_already_done(tmp_path,
                                                                              monkeypatch):
    """`ops.job_run`'s own `except Exception` cannot see a `KeyboardInterrupt` (a `BaseException`),
    so an operator's Ctrl-C mid-batch used to leave `job_runs` with NO row at all for a run that
    had already pushed real commits — a job-history trail lying by omission. `regenerate.run`'s
    own `except KeyboardInterrupt` closes that gap explicitly."""
    import subprocess

    remote, clone = build_repo(str(tmp_path / "git"), entity_id="acme-corp",
                               entity_name="Acme Corp", n_decisions=1)
    with open(os.path.join(clone, "wiki", "entities", "Globex.md"), "w") as f:
        f.write('---\ntype: entity\ntitle: "Globex"\nentity_type: organization\n'
               'entity: [globex]\ntags: [entity]\ncreated: "2026-07-01"\n'
               'updated: "2026-07-01"\nstatus: developing\n---\n\n# Globex\n')
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add globex"], cwd=clone, check=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "Test Steward",
                        "GIT_AUTHOR_EMAIL": "steward@example.com",
                        "GIT_COMMITTER_NAME": "Test Steward",
                        "GIT_COMMITTER_EMAIL": "steward@example.com"})
    subprocess.run(["git", "push", "-q"], cwd=clone, check=True)

    registry = registry_of()
    registry.entities["globex"] = {"name": "Globex", "type": "organization", "aliases": []}

    orig = regenerate.regenerate_entity
    calls = {"n": 0}

    async def _flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt()
        return await orig(*a, **kw)

    monkeypatch.setattr(regenerate, "regenerate_entity", _flaky)
    conn = FakeConn()
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(regenerate.run(clone, conn, ["acme-corp", "globex"], registry=registry))

    assert conn.executed, "no job_runs write was attempted for the interrupted batch"
    _sql, params = conn.executed[-1]
    job, status, stats, error = params
    assert status == "error"
    assert error == "KeyboardInterrupt"
    assert stats.obj["written"] == 1    # acme-corp's commit already landed before the Ctrl-C
    assert stats.obj["checked"] == 1    # never claims the full batch of 2 was "checked"


# ── the commit verb, the atomic writer, the path shape ──────────────────────────────────────────
def test_first_ever_write_commits_as_write_not_regenerate(repo):
    remote, clone = repo
    registry = registry_of()
    outcome = asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry))
    assert outcome.action == "written"
    log = remote_log(remote)
    assert "write acme-corp" in log
    assert "regenerate acme-corp" not in log

    # a SECOND write over a changed member set commits as "regenerate", never "write" again
    with open(os.path.join(clone, "wiki", "decisions", "decision-3.md"), "w") as f:
        f.write('---\ntype: decision\ntitle: "Decision 3"\nentity: [acme-corp]\n'
               'as_of: "2026-07-25"\ncreated: "2026-07-25"\nupdated: "2026-07-25"\n'
               'status: developing\ntags: [decision]\n---\n\n# Decision 3\n')
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add decision 3"], cwd=clone, check=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "Test Steward",
                        "GIT_AUTHOR_EMAIL": "steward@example.com",
                        "GIT_COMMITTER_NAME": "Test Steward",
                        "GIT_COMMITTER_EMAIL": "steward@example.com"})
    subprocess.run(["git", "push", "-q"], cwd=clone, check=True)
    asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry))
    assert "regenerate acme-corp" in remote_log(remote)


# ── the repo-parse → Member.acl → committed page chain, end to end. `build_repo` accepted
# `decision_acls` long before any test passed it, so the whole ACL suite ran on hand-built
# `Member` objects — never on `acl:` frontmatter genuinely PARSED off disk through
# `corpus.load_pages`, which is what a real capture leaves behind ─────────────────────────────
def test_the_intersection_survives_the_real_repo_parse_end_to_end(tmp_path):
    """Two decision pages carrying REAL `acl:` frontmatter on disk (one `[a]`, one `[a, b]`), plus
    the entity's own open (label-free) page — `decision_acls[i]` is threaded through `build_repo`
    into `decision_page`'s own YAML, committed and pushed like any other fixture page, so
    `skeleton.members_of` must read `Member.acl` off the genuinely parsed frontmatter
    (`index.corpus.load_pages` → `_acl_labels`), not off a value a test constructed by hand. The
    real regeneration path, unmodified, must still land the intersection (`[a]`) on the COMMITTED
    page — closing the coverage gap `test_render.py`'s ACL tests leave open: they prove the
    intersection MATH against hand-built `Member`s, never that a real page's `acl:` line reaches
    that math in the first place."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=2,
                               decision_acls=[["a"], ["a", "b"]])
    registry = registry_of()

    members = skeleton.members_of(clone, "acme-corp")
    by_title = {m.title: m.acl for m in members}
    # the real parse actually produced these, off disk — not asserted from the fixture's own input
    assert by_title["Decision 1"] == ["a"]
    assert by_title["Decision 2"] == ["a", "b"]
    assert by_title["Acme Corp"] is None                    # the entity page itself is open

    outcome = asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry))
    assert outcome.action == "written"
    assert outcome.acl == ["a"]                              # [a] ∩ [a,b], the open member neutral

    page = open(os.path.join(clone, "views", "acme-corp.md")).read()
    assert "acl: [a]" in page
    assert "acl: [a, b]" not in page


def test_disjoint_real_acls_land_a_restrictive_empty_intersection_on_the_committed_page(tmp_path):
    """The empty-intersection half of the same chain: two decision pages with DISJOINT real `acl:`
    frontmatter must land `acl: []` — restrictive by construction — on the committed view, never
    an omitted `acl:` field, which would read as open to `acl_rules`/the server seam."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=2,
                               decision_acls=[["a"], ["b"]])
    registry = registry_of()

    outcome = asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry))
    assert outcome.acl == []

    page = open(os.path.join(clone, "views", "acme-corp.md")).read()
    assert "acl: []" in page


# ── the regression test for a real defect: `skeleton.render_timeline`/`render_backlinks` now
# build their wikilinks from `Path(...).stem` instead of `.title` ───────────────────────────────
def test_the_timeline_and_backlinks_sections_link_by_file_stem_not_by_title(tmp_path):
    """`skeleton.render_timeline`/`render_backlinks` used to build wikilinks from the
    MEMBER's TITLE (`[[{m.title}]]`), but every wikilink resolver in this codebase —
    `index.corpus.link_targets` (`Path(t).stem.lower()`) and the real contract linter's own
    `by_name` index — resolves a wikilink by the target's FILE STEM, not its title. Titles and
    filename stems genuinely diverge in this system by design: `librarian.double`'s own decision
    pages (mirroring what a real agent drafts) title a page 'Q3 sync — decision 1' while filing it
    at 'wiki/decisions/<slug>-decision-1.md' — title and stem share no words at all. Both
    sections now build their wikilink from `Path(m.path).stem`/`Path(r.path).stem`, the same
    convention every other consumer of these pages already reads wikilinks by.

    Reproduction: a real regenerated view, scanned by the REAL contract linter (the frozen
    copy `tests/librarian` carries, which `test_frozen_linter.py` keeps in sync with the
    knowledge repo's own), over a member whose title (`Decision 1`) and filename stem
    (`decision-1`) diverge exactly the way a real meeting-filed decision page's do — run with
    `--strict` so the real exit-code contract is what is asserted, not merely the JSON
    findings."""
    import json
    import shutil
    import subprocess

    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    registry = registry_of()
    asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry))

    # the linter needs a committed registry to resolve `entity: [acme-corp]` — irrelevant to the
    # defect itself, just a piece of the fixture `tests/views/conftest.py` does not write.
    with open(os.path.join(clone, "ops", "entity-registry.json"), "w") as f:
        json.dump({"entities": {"acme-corp": {"name": "Acme Corp", "type": "organization",
                                              "aliases": []}}}, f)

    linter_src = (pathlib.Path(__file__).parents[1] / "librarian" / "fixtures" / "repo"
                 / ".claude" / "tools" / "stigmergy_lint.py")
    dest_dir = os.path.join(clone, ".claude", "tools")
    os.makedirs(dest_dir, exist_ok=True)
    shutil.copy(linter_src, os.path.join(dest_dir, "stigmergy_lint.py"))

    proc = subprocess.run(
        ["python3", os.path.join(dest_dir, "stigmergy_lint.py"), "--repo", clone, "--strict",
         "--json"], capture_output=True, text=True)
    report = json.loads(proc.stdout)
    dead_links = [f for f in report.get("findings", [])
                 if f.get("file") == "views/acme-corp.md" and f.get("check") == "dead_links"]

    assert proc.returncode == 0, (
        f"stigmergy_lint.py --strict refused the regenerated view: {dead_links}")
    assert dead_links == []


def test_view_relpath_refuses_a_malformed_entity_id():
    from stigmergy.views.errors import ViewError

    with pytest.raises(ViewError, match="lowercase letters, digits and hyphens"):
        regenerate.view_relpath("../../etc/passwd")
    with pytest.raises(ViewError):
        regenerate.view_relpath("Acme Corp")
    assert regenerate.view_relpath("acme-corp") == "views/acme-corp.md"


def test_a_view_never_lists_itself_among_its_own_backlinks(tmp_path):
    """OLD BEHAVIOUR: from the SECOND regeneration onward, the rollup cited itself.

    `backlinks_of` scans every indexed zone including `views/` — correctly, because another
    entity's view may legitimately wikilink this entity's page. But it excluded only the entity
    page itself. A view's Timeline renders one bullet per member and the `type: entity` page is
    always a member, so the view always links the entity page; and its `acl` equals `view_acl` by
    construction, so it always passed the visibility filter.

    The result was a rollup asserting itself as evidence that something points at the entity, with
    the "N page(s) link to X's own entity page" count one too high.
    """
    _remote, clone = build_repo(str(tmp_path / "git"))
    registry = registry_of()

    asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry))
    asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry, force=True))

    view = (pathlib.Path(clone) / "views" / "acme-corp.md").read_text(encoding="utf-8")
    backlinks = view.split("## Backlinks")[1]
    assert "views/acme-corp.md" not in backlinks, backlinks
    assert "[[acme-corp]]" not in backlinks, backlinks


def test_a_real_backlink_is_still_listed(tmp_path):
    """The benign twin: excluding the view itself must not empty the section. A genuine page
    linking the entity page still appears — that is what the feed is for."""
    _remote, clone = build_repo(str(tmp_path / "git"))

    asyncio.run(regenerate.regenerate_entity(clone, "acme-corp", registry=registry_of()))

    view = (pathlib.Path(clone) / "views" / "acme-corp.md").read_text(encoding="utf-8")
    backlinks = view.split("## Backlinks")[1]
    assert "decision" in backlinks, backlinks
