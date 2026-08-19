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


def test_acl_is_the_intersection_and_an_open_member_does_not_narrow():
    members = _members(acls=(["a"], None))            # one labelled, one open
    page = _render(members)
    assert "acl: [a]" in page                          # open member contributes neutrally


def test_acl_narrows_to_the_shared_label_only():
    members = _members(acls=(["a"], ["a", "b"]))       # [a] ∩ [a,b] = [a]
    page = _render(members)
    assert "acl: [a]" in page
    assert "acl: [a, b]" not in page


def test_disjoint_labels_yield_an_empty_restrictive_acl():
    members = _members(acls=(["a"], ["b"]))            # empty intersection: restrictive, not open
    page = _render(members)
    assert "acl: []" in page


def test_all_open_members_yield_no_acl_field_at_all():
    members = _members(acls=(None, None))
    page = _render(members)
    assert "acl:" not in page.split("---\n\n")[0] + "---"


def test_sabotage_the_intersection_and_watch_the_test_above_fail():
    """Before trusting a check, ask whether it can go red, and prove it. This calls the SABOTAGED
    (union) rule directly and asserts it produces the WRONG, widened answer that `view_acl`'s real
    intersection must never produce — showing the control can fail for the reason it exists, not
    merely that the real code happens to pass."""
    def sabotaged_union(member_acls):
        sets = [set(a) for a in member_acls if a is not None]
        if not sets:
            return None
        return sorted(set.union(*sets))               # the bug this rule exists to prevent

    member_acls = [["a"], ["b"]]                        # disjoint: correct answer is []
    correct = _view_acl(member_acls)
    wrong = sabotaged_union(member_acls)
    assert correct == []                                # restrictive by construction
    assert wrong == ["a", "b"]                           # the union WIDENS access — the exact bug
    assert correct != wrong, (
        "the intersection and the sabotaged union produced the SAME answer on a disjoint-label "
        "fixture — this fixture no longer distinguishes the two rules and must be strengthened")


def _view_acl(member_acls):
    from stigmergy.kernel.acl import view_acl
    return view_acl(member_acls)
