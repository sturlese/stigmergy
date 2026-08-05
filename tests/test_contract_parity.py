"""The two cross-package contracts that are implemented TWICE, pinned where they actually run.

They lived in an eval harness once, behind a capability probe for a top-level `answer` package a
consolidation had already folded away. The probe was False on every run, so the check its own
comment called *"the one harness that imports both sides"* never actually executed — while the
scorecard reported green over its absence. Nothing proved the two halves agreed for a long
stretch, and during it two ACL dialects got written for one system. That is why these run in the
suite, on every `make test`, rather than in a harness nobody invokes: a check that stops running
must be impossible to miss.

A third contract used to be pinned here — two hand-written SQL paths over one facts store — and
has no sides left to disagree, the store and both of its readers being gone. What remains is the
ACL contract, still implemented twice (`kernel.acl.visible` on the curation path,
`server.acl.visible` on the serving one), plus the `[]`-stays-`[]` invariant below.
"""
import pytest

from stigmergy.kernel.acl import visible as kernel_visible
from stigmergy.server.acl import visible as serving_visible

_WELL_FORMED = [
    (None, None),
    (None, {"eng"}),
    ([], None),
    ([], {"eng"}),
    (["sales"], None),
    (["sales"], {"sales"}),
    (["sales"], {"eng"}),
    (["sales", "leadership"], {"eng", "leadership"}),
    (["sales", "leadership"], {"eng"}),
]


@pytest.mark.parametrize(("acl", "audiences"), _WELL_FORMED)
def test_the_two_visible_implementations_agree_on_every_well_formed_case(acl, audiences):
    """`kernel.acl.visible` and `server.acl.visible` are hand-mirrored halves of one rule, living
    in packages that share no code on purpose — packages that share no code talk through files,
    never through imports. Nothing imports one from the other, so only a test that calls both can
    prove they still say the same thing."""
    assert kernel_visible(acl, audiences) == serving_visible(acl, audiences)


@pytest.mark.parametrize(("acl", "audiences"), _WELL_FORMED)
def test_the_serving_half_reads_the_csv_shape_identically_to_the_list_shape(acl, audiences):
    """The index holds `acl` as a Postgres `text[]`; the serving half ALSO normalizes a bare CSV
    string, a shape no live caller produces any more but which must keep failing closed rather
    than crashing if a stored `acl` ever arrives that way. One enforcement point takes both, and
    a divergence between the two shapes would scope the same content two different ways."""
    csv = None if acl is None else ",".join(acl)
    assert serving_visible(csv, audiences) == serving_visible(acl, audiences)


@pytest.mark.parametrize("malformed", [{"a": 1}, True, 7, [None], [{"x": 1}], object()])
def test_the_two_halves_diverge_on_malformed_values_and_that_divergence_is_deliberate(malformed):
    """The ONE case where they are meant to disagree, pinned so it cannot drift into an accident.

    `server/acl.py` states it outright: a stored value we cannot parse must never resolve to "open"
    at the point access is decided, *not even for an unrestricted client*. `kernel.acl`'s half is a
    curation-time helper with no such duty. Asserting the divergence is what keeps someone from
    "fixing" the serving half into agreement and quietly turning a fail-closed into a fail-open."""
    assert serving_visible(malformed, None) is False
    assert serving_visible(malformed, {"eng"}) is False


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The `[]`-stays-`[]` invariant, pinned. Load-bearing, and unguarded for far too long.
#
# A view's own `acl` is the INTERSECTION of its members' audiences (`view_acl`,
# "restrictive by construction, never silently open"), and when that intersection is empty the
# view is written with a literal `acl: []` rather than the key being omitted. `acl: []` means
# NOBODY at every serving reader (`server.acl.visible`'s own truth table), so an empty
# intersection produces a view that is visible to no scoped client — which is the correct,
# fail-closed answer for a rollup over pages that share no audience.
#
# Two independent implementations have to agree for that to hold:
#
#   * `views.render` must WRITE `acl: []` and not omit the key;
#   * `index.corpus._acl_labels` must READ `acl: []` back as `[]` and not as `None`.
#
# If EITHER ever collapsed empty to open, a view carrying `acl: []` would index as open — and a
# view's body renders its members' titles and its Backlinks section lists their paths, so the
# failure discloses every restricted backlink to every reader. Neither side had a test holding it,
# which is precisely how a two-sided invariant dies in one of its halves.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_the_render_side_writes_an_empty_acl_rather_than_omitting_the_key():
    from stigmergy.kernel.acl import view_acl
    # Two members sharing NO audience: the intersection is empty.
    assert view_acl([["finance"], ["sales"]]) == []
    # ...and an open corpus is a different answer, which is what makes the empty case meaningful.
    assert view_acl([None, None]) is None


def test_the_read_side_reads_an_empty_acl_back_as_nobody_never_as_open():
    from stigmergy.index.corpus import _acl_labels
    assert _acl_labels({"acl": []}) == [], (
        "`acl: []` read back as anything but `[]` — if this became `None`, a view over "
        "members sharing no audience would index as OPEN and disclose every restricted backlink")
    assert _acl_labels({}) is None                  # absent = open, the OTHER value
    assert _acl_labels({"acl": None}) is None       # explicit null = open


def test_the_two_sides_compose_a_view_over_disjoint_members_is_visible_to_nobody():
    """The invariant end to end, over the values rather than the prose: render's output shape fed
    to the reader, then to the one enforcement point. This is the assertion that would have caught
    a collapse in EITHER half, which is why it exists beside the two unit checks and not instead
    of them."""
    from stigmergy.index.corpus import _acl_labels
    from stigmergy.kernel.acl import view_acl
    from stigmergy.server.acl import visible

    acl = view_acl([["finance"], ["sales"]])     # disjoint members -> []
    assert acl == []
    rendered = {"acl": acl}                          # what `views.render` puts in frontmatter
    stored = _acl_labels(rendered)                   # what the index stores
    assert visible(stored, {"finance"}) is False
    assert visible(stored, {"sales"}) is False
    assert visible(stored, set()) is False
    # Unrestricted still sees it — `visible`'s documented truth table, not an accident: an operator
    # with full read is inside the trust boundary, and hiding it from them would hide the fact that
    # a view came out unshareable at all.
    assert visible(stored, None) is True
