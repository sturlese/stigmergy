"""`kernel.acl` — the audience vocabulary: what two label lists mean when they meet.

`flows_into` is the load-bearing one and is asked in six places (ADR 045 D3/D5), so its truth
table is pinned here rather than inferred from any one caller. `view_acl` is on its way out with
D5 and keeps its own test until it goes.

What this file used to hold — `load_acl_config`, `resolve_acl` and the four matchers — went with
the path resolver (ADR 045 D2): a capture's audience is the door's decision, carried on its queue
row, so there is nothing left to resolve from a config file.
"""
from stigmergy.kernel.acl import flows_into, view_acl


# ── flows_into: may this CONTENT be written into a page with THAT label ────────────────────────
def test_open_content_flows_anywhere():
    """Content with no label is already readable by everyone; nothing it lands on can widen it."""
    assert flows_into(None, None) is True
    assert flows_into(None, ["finance"]) is True
    assert flows_into(None, []) is True


def test_nothing_labelled_flows_into_an_open_page():
    """The fail-closed direction, and the one an accidental default has to land on: an open page
    reaches everybody, so labelled material rendered there reaches an audience its author
    restricted it from."""
    assert flows_into(["finance"], None) is False
    assert flows_into([], None) is False


def test_a_page_admits_content_whose_groups_CONTAIN_its_own():
    """Containment, not intersection — everyone who can read the page can already read the
    source."""
    assert flows_into(["finance"], ["finance"]) is True
    assert flows_into(["finance", "leadership"], ["finance"]) is True


def test_sharing_one_group_is_NOT_enough():
    """The difference from `visible()`, which is the reason these are two functions. A PERSON
    needs to share one label with a page. An AUDIENCE needs to be contained by it: `leadership`
    readers of the target page would otherwise inherit `finance`-only material."""
    assert flows_into(["finance"], ["finance", "leadership"]) is False


def test_nobody_content_flows_only_into_a_nobody_page():
    """`[]` is a real value meaning nobody, never "open" — the collapse ADR 045 D9 ends."""
    assert flows_into([], []) is True
    assert flows_into([], ["finance"]) is False
    assert flows_into(["finance"], []) is True     # every group of `[]` is trivially in `finance`


def test_order_and_duplicates_do_not_change_the_answer():
    assert flows_into(["leadership", "finance", "finance"], ["finance", "leadership"]) is True


# ── view_acl: the members' intersection (retired by D5, in the next phase) ─────────────────────
def test_view_acl_is_intersection():
    assert view_acl([None, None]) is None                       # all open -> open
    assert view_acl([["a", "b"], ["b", "c"]]) == ["b"]           # intersection, never union
    assert view_acl([["a"], ["b"]]) == []                        # disjoint -> nobody, not open
    assert view_acl([None, ["a"]]) == ["a"]                      # an open member does not widen
    assert view_acl([]) is None
