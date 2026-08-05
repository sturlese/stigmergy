"""The deterministic skeleton: member set, staleness hash, timeline, backlinks."""
import os
import subprocess

from stigmergy.index import corpus
from stigmergy.views import skeleton
from tests.views.conftest import build_repo

_FINANCE_NOTE = ('---\ntype: note\ntitle: "Finance Note"\nacl: [finance]\ntags: [note]\n'
                 'created: "2026-07-01"\nupdated: "2026-07-01"\nstatus: developing\n---\n\n'
                 '# Finance Note\n\nSomething about [[Acme Corp]].\n')

_GIT_ENV = {"GIT_AUTHOR_NAME": "Test Steward", "GIT_AUTHOR_EMAIL": "steward@example.com",
           "GIT_COMMITTER_NAME": "Test Steward", "GIT_COMMITTER_EMAIL": "steward@example.com"}


def _add_finance_note(clone: str) -> None:
    """A page OUTSIDE the member zone's `entity:` set that wikilinks the entity's own page and
    carries its own restrictive `acl` — a governed, non-member backlink source."""
    notes_dir = os.path.join(clone, "wiki", "notes")
    os.makedirs(notes_dir, exist_ok=True)
    with open(os.path.join(notes_dir, "finance-note.md"), "w") as f:
        f.write(_FINANCE_NOTE)
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add finance note"], cwd=clone, check=True,
                   env={**os.environ, **_GIT_ENV})
    subprocess.run(["git", "push", "-q"], cwd=clone, check=True)


def test_members_of_reads_both_zones_and_excludes_views_zone(tmp_path):
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=2)
    members = skeleton.members_of(clone, "acme-corp")
    # the entity page itself + 2 decisions = 3 members, sorted by path
    assert [m.path for m in members] == sorted(m.path for m in members)
    assert len(members) == 3
    assert {m.type for m in members} == {"entity", "decision"}


def test_members_of_ignores_pages_anchored_to_a_different_entity(tmp_path):
    remote, clone = build_repo(str(tmp_path / "git"), entity_id="acme-corp")
    assert skeleton.members_of(clone, "some-other-id") == []


def test_timeline_ordered_by_as_of(tmp_path):
    """The timeline is ordered by `as_of`, newest first."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=3)
    members = skeleton.members_of(clone, "acme-corp")
    ordered = skeleton.timeline_order(members)
    dated = [m for m in ordered if m.as_of]
    assert dated == sorted(dated, key=lambda m: m.as_of, reverse=True)
    # the entity page (no as_of) sorts after every dated member
    undated_positions = [i for i, m in enumerate(ordered) if not m.as_of]
    dated_positions = [i for i, m in enumerate(ordered) if m.as_of]
    assert all(u > d for u in undated_positions for d in dated_positions)


def test_skeleton_is_byte_identical_across_two_runs(tmp_path):
    """Two runs over an unchanged corpus produce byte-identical skeleton sections."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=5)
    members_a = skeleton.members_of(clone, "acme-corp")
    members_b = skeleton.members_of(clone, "acme-corp")
    assert skeleton.render_timeline(members_a) == skeleton.render_timeline(members_b)
    entity_page = skeleton.entity_own_page(members_a)
    backlinks_a = skeleton.backlinks_of(clone, entity_page)
    backlinks_b = skeleton.backlinks_of(clone, entity_page)
    assert (skeleton.render_backlinks(backlinks_a, entity_title="Acme Corp")
           == skeleton.render_backlinks(backlinks_b, entity_title="Acme Corp"))
    assert skeleton.member_hash(members_a) == skeleton.member_hash(members_b)


def test_timeline_caps_with_stated_truncation(tmp_path):
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=15)
    members = skeleton.members_of(clone, "acme-corp")
    rendered = skeleton.render_timeline(members, cap=10)
    assert "showing the 10 most recent" in rendered
    assert "older not shown" in rendered
    assert rendered.count("\n- ") == 10   # never a SILENT cap: exactly `cap` bullets rendered


def test_timeline_empty_state():
    assert skeleton.render_timeline([]) == "No anchored pages."


def test_backlinks_resolve_to_the_entity_own_page_only(tmp_path):
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=2)
    members = skeleton.members_of(clone, "acme-corp")
    entity_page = skeleton.entity_own_page(members)
    assert entity_page is not None and entity_page.type == "entity"
    backlinks = skeleton.backlinks_of(clone, entity_page)
    # both decision pages wikilink [[Acme Corp]] — both are backlinks
    assert len(backlinks) == 2
    assert entity_page.path not in [r.path for r in backlinks]   # never links to itself


def test_backlinks_empty_state():
    assert skeleton.render_backlinks([], entity_title="Acme Corp") == \
        "Nothing links to this entity's own page yet."


def test_member_set_going_empty_is_detectable(tmp_path):
    """An entity whose last member vanishes loses its view — this is the pure building block
    `regenerate.regenerate_entity` acts on; proven end-to-end in test_regenerate.py."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    assert skeleton.members_of(clone, "acme-corp") != []
    import os
    for name in os.listdir(os.path.join(clone, "wiki", "decisions")):
        os.remove(os.path.join(clone, "wiki", "decisions", name))
    os.remove(os.path.join(clone, "wiki", "entities", "Acme Corp.md"))
    assert skeleton.members_of(clone, "acme-corp") == []


def test_member_hash_changes_when_a_member_gains_superseded_by(tmp_path):
    """A frontmatter-only change (no body edit) still moves the hash — the strengthening beyond
    the bare (id, content_hash, path) triple, documented in `skeleton.member_hash`."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    members = skeleton.members_of(clone, "acme-corp")
    h1 = skeleton.member_hash(members)
    import dataclasses
    members2 = [dataclasses.replace(m, superseded_by="somewhere-else.md") if m.type == "decision"
               else m for m in members]
    assert skeleton.member_hash(members2) != h1


# ── backlinks are a GOVERNED but NON-MEMBER feed ────────────────────────────────────────────────
def test_backlinks_are_filtered_to_the_views_own_audience_both_ways(tmp_path):
    """A backlink's own `acl` never participates in `view_acl`'s intersection (a backlink must
    never NARROW the view), but a restricted backlink must still never RENDER on a view whose
    audience it does not cover — the existence/leak rule, applied to one more feed."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    _add_finance_note(clone)
    members = skeleton.members_of(clone, "acme-corp")
    entity_page = skeleton.entity_own_page(members)

    # open view: the finance-scoped backlink must NOT render — its own author restricted it,
    # and an open view is visible to unrestricted clients who never agreed to `finance`.
    open_backlinks = {r.path for r in skeleton.backlinks_of(clone, entity_page, view_acl=None)}
    assert "wiki/notes/finance-note.md" not in open_backlinks

    # a view narrowed to a set the row's own acl COVERS (subset): visible.
    finance_backlinks = {r.path for r in
                         skeleton.backlinks_of(clone, entity_page, view_acl=["finance"])}
    assert "wiki/notes/finance-note.md" in finance_backlinks

    # a view narrowed to a DIFFERENT label the row's acl does not cover: still not visible.
    sales_backlinks = {r.path for r in
                       skeleton.backlinks_of(clone, entity_page, view_acl=["sales"])}
    assert "wiki/notes/finance-note.md" not in sales_backlinks


def test_sabotage_no_filter_would_leak_a_restricted_backlink(tmp_path):
    """Before trusting a check, ask whether it can go red, and prove it: the unfiltered behaviour
    — every wikilinking page, regardless of its own acl — really does leak the finance-scoped
    note's title and path onto an open view, and the real, filtered code must disagree with it on
    this exact fixture."""
    remote, clone = build_repo(str(tmp_path / "git"), n_decisions=1)
    _add_finance_note(clone)
    members = skeleton.members_of(clone, "acme-corp")
    entity_page = skeleton.entity_own_page(members)

    def _sabotaged_backlinks_of(repo, page):
        """The real `backlinks_of` minus its ACL filter — it mirrors that function's `r.links`
        resolution (resolved paths, not stems; see its own docstring) so this twin's only
        difference from production is the ONE thing this test is proving matters."""
        rows = corpus.load_pages(repo)
        return sorted((r for r in rows if page.path in r.links and r.path != page.path),
                     key=lambda r: r.path)

    filtered = {r.path for r in skeleton.backlinks_of(clone, entity_page, view_acl=None)}
    sabotaged = {r.path for r in _sabotaged_backlinks_of(clone, entity_page)}
    assert "wiki/notes/finance-note.md" not in filtered
    assert "wiki/notes/finance-note.md" in sabotaged
    assert filtered != sabotaged, (
        "the filtered and sabotaged backlink sets are identical on this fixture — strengthen it")
