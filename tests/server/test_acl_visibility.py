"""The two-sided ACL truth table (page acl × client audiences) — exhaustive, including the
unrestricted client and the fail-closed malformed case."""
import logging

import pytest

from stigmergy.server.acl import visible

UNRESTRICTED = None            # a client with no audience scope sees everything (non-malformed)
NOBODY = set()                 # a client with an empty scope: only unlabeled (open) content
FINANCE = {"finance"}
ENG = {"eng"}


# ── page carries NO acl (NULL) → open to everyone ──────────────────────────────────────────────
@pytest.mark.parametrize("audiences", [UNRESTRICTED, NOBODY, FINANCE, ENG])
def test_no_acl_is_open_to_everyone(audiences):
    assert visible(None, audiences) is True


# ── page carries an EMPTY acl ({} / "") → nobody, but unrestricted still sees it ────────────────
@pytest.mark.parametrize("empty", [[], ""])
def test_empty_acl_hidden_from_scoped_visible_to_unrestricted(empty):
    assert visible(empty, UNRESTRICTED) is True
    assert visible(empty, NOBODY) is False
    assert visible(empty, FINANCE) is False
    assert visible(empty, ENG) is False


# ── page carries labels → intersect; unrestricted sees all ─────────────────────────────────────
def test_scoped_acl_intersects_audiences():
    assert visible(["finance"], FINANCE) is True
    assert visible(["finance"], ENG) is False
    assert visible(["finance", "leadership"], FINANCE) is True
    assert visible(["finance"], UNRESTRICTED) is True
    assert visible(["finance"], NOBODY) is False


def test_comma_separated_string_shape_is_accepted():
    # an acl may arrive serialized as a comma-separated string rather than a list; the same rule
    # must read both, with no second code path deciding access
    assert visible("sales,leadership", {"sales"}) is True
    assert visible("sales,leadership", ENG) is False
    assert visible("finance", UNRESTRICTED) is True


# ── malformed stored acl → hidden from EVERYONE (even unrestricted), logged loudly ─────────────
# includes lists carrying non-string elements: str()-coercing them would forge a garbage label
# ([{"nested": 1}] → "{'nested': 1}") that an unrestricted client would then "see".
@pytest.mark.parametrize("bad", [{"nested": 1}, True, 42, 3.14, [{"nested": 1}], [None]])
def test_malformed_acl_hidden_even_from_unrestricted(bad, caplog):
    with caplog.at_level(logging.WARNING):
        assert visible(bad, UNRESTRICTED) is False
        assert visible(bad, FINANCE) is False
    assert any("fail-closed" in r.message for r in caplog.records)
