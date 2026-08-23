"""`kernel.acl` — the audience vocabulary: what two label lists mean when they meet.

`flows_into` is the whole of this module now, and it is asked in six places (the audience-from-the-door change), so
its truth table is pinned here rather than inferred from any one caller.

What this file used to hold went with two decisions. `load_acl_config`, `resolve_acl` and the four
matchers went with the path resolver (D2): a capture's audience is the door's decision, carried on
its queue row, so there is nothing to resolve from a config file. `view_acl` went with D5: a view
carries no label, so there is no second label to compute.
"""
from stigmergy.kernel.acl import flows_into


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
    """`[]` is a real value meaning nobody, never "open" — the collapse the audience-from-the-door change ends."""
    assert flows_into([], []) is True
    assert flows_into([], ["finance"]) is False
    assert flows_into(["finance"], []) is True     # every group of `[]` is trivially in `finance`


def test_order_and_duplicates_do_not_change_the_answer():
    assert flows_into(["leadership", "finance", "finance"], ["finance", "leadership"]) is True
