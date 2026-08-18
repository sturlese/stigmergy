"""The sweep's population now comes from PAGE FRONTMATTER, and that is a new trust boundary.

Before #76 the only population that reached `staleness.view_relpath` was the set of view files
already on disk — every one of them written by this system, so every one of them a well-formed
entity id. `list_sweep_entities` changed that: `skeleton.all_anchored_entity_ids` reads `entity:`
off pages, `corpus.entity_list` normalizes but does NOT validate (it drops bools and nested
containers, and keeps every non-empty string verbatim), and a page is something a human can hand
edit in the knowledge repo, an agent can propose and a repair can apply.

So the question this file exists to answer is not "does `view_relpath` refuse a bad id" — it does,
and `tests/views/test_regenerate.py` already pins that — but **what a refused id does to the
PASS**. A convergence guarantee that one hand-edited page can switch off is not a guarantee.

`views/README.md` is the same boundary from the other side: a hand-written page sitting beside the
generated files, whose stem is not an entity id. `staleness.existing_view_ids` already filters it
out with that reasoning stated in its own docstring — pinned here as behaviour rather than left as
a comment, because the sweep is the caller that would take the crash.
"""
import asyncio
import json
import os

import pytest

from stigmergy.views import regenerate, skeleton, staleness
from tests.views.conftest import FakeConn, build_repo, git, registry_of, remote_files

_COMMIT_ENV = {"GIT_AUTHOR_NAME": "Hand Editor", "GIT_AUTHOR_EMAIL": "hand@example.com",
               "GIT_COMMITTER_NAME": "Hand Editor", "GIT_COMMITTER_EMAIL": "hand@example.com"}

# Three spellings of the same accident, only the first of which needs an attacker:
#
#   - a display name typed where an id belongs is what a human writes by hand, every time;
#   - an underscore is what anyone who has met a slug generator writes;
#   - `../../evil` is the traversal `view_relpath`'s assertion was actually written for.
#
# All three are `entity:` values `corpus.entity_list` keeps verbatim and `view_relpath` refuses.
POISONED_IDS = ["Acme Corp", "acme_corp", "../../evil"]


def _commit_all(clone: str, message: str) -> None:
    git("add", "--all", cwd=clone)
    git("commit", "--quiet", "-m", message, cwd=clone, env=_COMMIT_ENV)
    git("push", "--quiet", cwd=clone)


def _write_page_anchored_to(clone: str, name: str, entity_ids: list[str]) -> None:
    """A page whose `entity:` says exactly what it is told to say — the shape a hand edit, an
    applied repair or a filing agent's slip all leave behind."""
    ids = ", ".join(f'"{e}"' for e in entity_ids)
    with open(os.path.join(clone, "wiki", "decisions", f"{name}.md"), "w") as f:
        f.write(f'---\ntype: decision\ntitle: "{name}"\nentity: [{ids}]\nas_of: "2026-08-01"\n'
                f'created: "2026-08-01"\nupdated: "2026-08-01"\nstatus: developing\n'
                f'tags: [decision]\n---\n\n# {name}\n\nA page somebody wrote by hand.\n')


def _sweep(clone, conn, registry, **kw):
    return asyncio.run(regenerate.sweep(clone, conn, registry=registry, **kw))


# ── an unusable id in the population ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("poisoned", POISONED_IDS)
def test_one_hand_edited_entity_id_must_not_stop_the_whole_sweep(tmp_path, poisoned):
    """**The convergence guarantee under a page nobody validated.**

    #76 exists to make "a view is never stale, whatever wrote the corpus" a property of STATE
    rather than of somebody remembering a call site. A single page carrying an `entity:` value
    `view_relpath` refuses must therefore cost that page's own id and nothing else: `acme-corp`
    still has an anchored page, still has no view, and must still get one.

    Today it does not — the refusal escapes `regenerate_entity`, escapes `run`'s per-entity loop
    (which catches only `KeyboardInterrupt`), and aborts the pass. Two of the three ids above sort
    BEFORE `acme-corp`, so the pass dies on its first entity and converges nothing at all; the
    worker then swallows the fault, logs it, and repeats the identical failure every interval —
    silently, forever, because a swallowed fault has no operator-facing surface.

    This is a production defect, reported and not fixed here. The seam already exists and the
    developer already applied exactly this reasoning one function over:
    `staleness.existing_view_ids` filters a stem `view_relpath` would refuse precisely so it
    cannot "take the caller down". The frontmatter-sourced half of the union got no such filter.

    The SECOND assertion is the benign twin of the fix, stated in advance so the defect is not
    fixed into silence: filtering the population would converge the pass again and make the bad
    page invisible — no view, no finding, no line anywhere — and a steward who anchored a page to
    `Acme Corp` instead of `acme-corp` would wait forever for a rollup nobody is building.
    Whatever shape the fix takes (a per-entity refusal outcome, a `skip_reasons` entry, a gardener
    finding), the id has to appear in what the run reports; asserted against the whole reported
    blob rather than one named key, because this is a contract about VISIBILITY and pinning it to
    a field would dictate the implementation.
    """
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    _write_page_anchored_to(clone, "hand-edited", [poisoned])
    _commit_all(clone, "chore: a page a human wrote by hand")

    result = _sweep(clone, FakeConn(), registry_of())

    assert "views/acme-corp.md" in remote_files(remote), (
        f"one page declaring entity: [{poisoned!r}] stopped the whole convergence pass — "
        f"acme-corp has an anchored page, no view, and was never reached")
    assert result.stats["written"] == 1

    reported = json.dumps(result.stats) + json.dumps(
        [o.entity_id for o in result.outcomes]) + json.dumps(result.skip_reasons)
    assert poisoned in reported, (
        f"the sweep converged but said nothing about entity: [{poisoned!r}] — an id no view can "
        f"ever be built for must not vanish into a green run")


def test_a_page_anchored_to_both_a_good_and_an_unusable_id_still_refreshes_the_good_one(tmp_path):
    """The commonest real shape: one page, two `entity:` entries, one of them a typo. The typo
    must not cost the correct id its rollup — and the page IS a member of `acme-corp`, so the
    entity's view has to contain it."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    _write_page_anchored_to(clone, "half-typo", ["acme-corp", "Acme Corp"])
    _commit_all(clone, "chore: an anchor list with a typo in it")

    _sweep(clone, FakeConn(), registry_of())

    assert "views/acme-corp.md" in remote_files(remote)
    assert "half-typo" in open(os.path.join(clone, "views", "acme-corp.md")).read()


def test_the_unusable_id_never_reaches_a_path_outside_views(tmp_path):
    """The defense itself, and it holds: whatever the pass does with `../../evil`, nothing is
    written outside `views/`. This is the specificity twin of the two tests above — they say the
    refusal is too LOUD, this one says it is not too quiet.

    The pass no longer dies on the id (that was the defect the two tests above pin), so what the
    refusal LOOKS like is asserted here rather than left as `pytest.raises(Exception)` — a bare
    `raises` would now fail for the right reason and hide the claim this test is actually about.
    """
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    _write_page_anchored_to(clone, "traversal", ["../../evil"])
    _commit_all(clone, "chore: a traversal attempt in an entity anchor")

    result = _sweep(clone, FakeConn(), registry_of())

    refusals = [o for o in result.outcomes if o.action.startswith("refused")]
    assert [o.entity_id for o in refusals] == ["../../evil"]
    assert result.stats["refused"] == 1
    escaped = os.path.abspath(os.path.join(clone, "..", "..", "evil.md"))
    assert not os.path.exists(escaped)
    assert not os.path.exists(os.path.join(clone, "evil.md"))
    assert [f for f in remote_files(remote) if not f.startswith(("wiki/", "views/"))] == []


# ── a hand-written page beside the generated ones ──────────────────────────────────────────────
def _write_views_readme(clone: str, *, body: str) -> str:
    d = os.path.join(clone, "views")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "README.md")
    with open(path, "w") as f:
        f.write(body)
    return path


def test_a_hand_written_views_readme_never_reaches_the_union_population(tmp_path):
    """`README` is not an entity id, and `view_relpath` would refuse it. It reaches the union
    through `list_stale_entities`, which iterates the `.md` files in `views/` — so the stem filter
    in `existing_view_ids` is what stands between a hand-written index page and a pass that dies
    on the letter R."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    _write_views_readme(clone, body="# Views\n\nGenerated. Do not edit by hand.\n")
    _commit_all(clone, "docs: a hand-written index beside the generated views")

    assert staleness.existing_view_ids(clone) == set()
    assert "README" not in staleness.list_sweep_entities(clone)
    assert staleness.list_sweep_entities(clone) == ["acme-corp"]


def test_a_views_readme_that_declares_an_entity_anchor_is_still_not_a_member(tmp_path):
    """The nastier spelling: a README with real frontmatter that anchors itself. `views/` is
    deliberately outside `skeleton.MEMBER_ZONES` (a view declares `entity:` too, and counting it
    would re-stale every view on every write), so this page is not a member and does not enter the
    population through `all_anchored_entity_ids` either — and it must not change `acme-corp`'s
    member hash."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    _write_views_readme(clone, body=(
        '---\ntype: note\ntitle: "Views"\nentity: ["acme-corp", "README"]\ncreated: "2026-08-01"\n'
        'updated: "2026-08-01"\nstatus: developing\ntags: [note]\n---\n\n# Views\n\nAn index.\n'))
    _commit_all(clone, "docs: an anchored README beside the generated views")

    assert staleness.list_sweep_entities(clone) == ["acme-corp"]
    members = [m.path for m in skeleton.members_of(clone, "acme-corp")]
    assert "views/README.md" not in members


def test_a_sweep_converges_around_a_hand_written_readme_and_leaves_it_alone(tmp_path):
    """The benign twin for the filter: the pass still does its job with the README present, and
    the README survives it. A filter that also deleted the page it excluded would be a worse bug
    than the crash it prevents."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    readme = _write_views_readme(clone, body="# Views\n\nGenerated. Do not edit by hand.\n")
    _commit_all(clone, "docs: a hand-written index beside the generated views")

    result = _sweep(clone, FakeConn(), registry_of())

    assert result.stats["written"] == 1
    assert "views/acme-corp.md" in remote_files(remote)
    assert "views/README.md" in remote_files(remote)
    assert open(readme).read().startswith("# Views")
