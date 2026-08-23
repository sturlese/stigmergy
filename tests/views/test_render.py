"""Page shape: frontmatter, the withheld state, the intersection ACL rule.

A view carries no `verification:` verdict and no "Current facts" section: nothing computes a
verdict, and there is no facts store to draw a facts section from. The withheld state has exactly
one road — the bounded agent ran out of budget before a draft existed.
"""
from stigmergy.views import render, skeleton


def _members(acls=(None, None)):
    return [skeleton.Member(path=f"wiki/decisions/d{i}.md", title=f"D{i}", type="decision",
                            as_of="2026-07-2" + str(i), superseded_by="", acl=acl,
                            content_hash=f"h{i}")
           for i, acl in enumerate(acls)]


def _render(members, shipped=True):
    return render.render("acme-corp", "Acme Corp", members, member_hash="deadbeef",
                         backlink_hash="feedface", timeline_md="tl", backlinks_md="bl",
                         synthesis_body="## Status\nAll good.", shipped=shipped)


def test_entity_frontmatter_is_a_list_not_a_bare_string():
    """`entity: [<id>]`, matching every other page type — the earlier generator's bare-string
    dialect is the concrete rewiring point this test pins."""
    page = _render(_members())
    assert "entity: [acme-corp]" in page
    assert "entity: acme-corp\n" not in page   # the legacy bare-string form must not reappear


def test_frontmatter_carries_the_view_fields_and_no_verdict():
    page = _render(_members())
    fm = page.split("---")[1]
    for field in ("type: view", 'title: "Acme Corp — view"', "tags: [view]", "tier: 3",
                 "members: 2"):
        assert field in fm
    # BOTH staleness signals, one per feed the page renders — a view missing `backlink_hash:` is
    # read as stale forever after (#85), so its absence here would be a defect, not a detail.
    assert "content_hash:" in fm and "generated_at:" in fm
    assert 'member_hash: "deadbeef"' in fm and 'backlink_hash: "feedface"' in fm
    # nothing computes a verdict, so the page must not claim one
    assert "verification:" not in fm


def test_a_shipped_synthesis_renders_under_a_caption_that_states_what_it_is():
    page = _render(_members(), shipped=True)
    assert "## Synthesis" in page
    assert "Written by an agent from the pages above" in page
    # State the fact, never the implication: the caption must not imply a check nothing runs.
    # It says the source pages ARE the check.
    assert "not machine-verified" in page
    assert "All good." in page


def test_withheld_synthesis_never_ships_prose_and_says_why():
    """A synthesis that never finished ships the skeleton plus the explicit withheld state, never
    prose under a heading that promises one. The reason is a BUDGET — the verification road went
    with the verifier — and the copy says so rather than claiming a verdict nothing computed."""
    page = _render(_members(), shipped=False)
    assert "## Synthesis" in page                        # never omitted
    assert "ran out of budget" in page
    assert "did not pass verification" not in page        # the removed claim must not survive
    assert "All good." not in page                        # the unfinished prose never ships
    assert "attempted again\nautomatically the next time" in page


def test_a_view_carries_NO_acl_line_whatever_its_members_are_labelled():
    """The audience-from-the-door change. A view is the OPEN rollup: `skeleton.members_of` has already dropped every
    member that may not be rendered onto an open page, so there is nothing left on this page to
    restrict — and a page with no `acl:` is the contract's spelling of open.

    The renderer is asked with LABELLED members anyway, because that is the shape that used to
    produce a label and the one a regression would reintroduce it from."""
    for acls in ((["a"], None), (["a"], ["a", "b"]), (["a"], ["b"]), (None, None)):
        page = _render(_members(acls=acls))
        front = page.split("---\n\n")[0]
        assert "acl:" not in front, f"members={acls} produced a labelled view:\n{front}"


def test_the_collapse_this_replaced_is_what_a_label_here_would_mean():
    """**The red proof, kept as the argument.** The rule was the INTERSECTION of the members'
    audiences. It never widened access — that half was right, and its own sabotage twin (a union)
    genuinely failed. What it did instead was COLLAPSE: two members with disjoint labels produced
    `acl: []`, which is *nobody*, so one leadership-only note anchored to a popular entity deleted
    that entity's view for everyone — while the timeline went on naming every member's path and
    title.

    This recomputes the retired rule on the fixture that made it collapse, so the reason the line
    is gone stays legible next to its absence rather than only in a commit message."""
    def retired_intersection(member_acls):
        sets = [set(a) for a in member_acls if a is not None]
        return None if not sets else sorted(set.intersection(*sets))

    assert retired_intersection([["a"], ["b"]]) == []      # nobody: the availability failure
    assert "acl:" not in _render(_members(acls=(["a"], ["b"]))).split("---\n\n")[0]


