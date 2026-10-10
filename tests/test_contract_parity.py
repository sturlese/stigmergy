import datetime as dt

import pytest

from stigmergy.knowledge.pages import PageContractError, parse_page, render_page
from stigmergy.server.acl import visible


@pytest.mark.parametrize(
    ("acl", "audiences"),
    [
        (None, None),
        (None, {"eng"}),
        ([], None),
        ([], {"eng"}),
        (["sales"], None),
        (["sales"], {"sales"}),
        (["sales"], {"eng"}),
        (["sales", "leadership"], {"eng", "leadership"}),
        (["sales", "leadership"], {"eng"}),
    ],
)
def test_visibility_reads_csv_and_list_storage_shapes_identically(acl, audiences):
    csv = None if acl is None else ",".join(acl)
    assert visible(csv, audiences) == visible(acl, audiences)


@pytest.mark.parametrize("malformed", [{"a": 1}, True, 7, [None], [{"x": 1}], object()])
def test_unparseable_stored_acl_fails_closed(malformed):
    assert visible(malformed, None) is False
    assert visible(malformed, {"eng"}) is False


@pytest.mark.parametrize("acl", [None, ("finance",)])
def test_page_visibility_round_trips_through_the_canonical_contract(acl):
    text = render_page(
        path="wiki/notes/Plan.md",
        role="note",
        title="Plan",
        body="# Plan\n\nCurrent plan.",
        acl=acl,
        created=dt.date(2026, 8, 24),
        updated=dt.date(2026, 8, 24),
    )

    assert parse_page("wiki/notes/Plan.md", text).acl == acl


def test_empty_page_acl_is_not_a_valid_visibility_state():
    text = render_page(
        path="wiki/notes/Plan.md",
        role="note",
        title="Plan",
        body="# Plan\n\nCurrent plan.",
        acl=(),
        created=dt.date(2026, 8, 24),
        updated=dt.date(2026, 8, 24),
    )

    with pytest.raises(PageContractError, match="acl cannot be empty"):
        parse_page("wiki/notes/Plan.md", text)
