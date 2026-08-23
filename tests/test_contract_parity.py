"""The cross-package contracts implemented TWICE, pinned where they actually run.

They lived in an eval harness once, behind a capability probe for a top-level `answer` package a
consolidation had already folded away. The probe was False on every run, so the check its own
comment called *"the one harness that imports both sides"* never actually executed — while the
scorecard reported green over its absence. That is why these run in the suite, on every
`make test`: a check that stops running must be impossible to miss.

**Two contracts have since stopped having two sides, and both are recorded rather than quietly
dropped.** The first was two hand-written SQL paths over one facts store; the store and both of
its readers are gone. The second was `visible()` — hand-mirrored in `kernel.acl` and
`server.acl`, and pinned here against drift. A pre-publication audit found the kernel half had NO
production caller at all: not the kernel itself, not one package, only this file. It was also the
FAIL-OPEN half (`True` for a malformed value), it sat in the one module every package may import,
and its docstring called itself "the one visibility rule" — so the likeliest way it would ever get
a caller was somebody reaching for an ACL predicate and picking that one. It was deleted, which
closes the drift this file was pinning by removing the second side rather than by watching it.

What remains here is the `[]`-stays-`[]` invariant, which genuinely has two sides that must agree.
"""
import pytest

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


def test_there_is_exactly_one_visible_implementation_to_disagree_with():
    """The deletion, asserted rather than remembered. `stigmergy.kernel` is importable from every
    package by construction, so a second `visible` there is a fail-open predicate one autocomplete
    away from becoming an enforcement point. If one is ever added back, this is the check that says
    so — and adding it back means restoring the parity test that used to live here."""
    from stigmergy.kernel import acl as kernel_acl
    assert not hasattr(kernel_acl, "visible"), (
        "a second `visible()` is back in stigmergy.kernel.acl — `server.acl.visible` is the ONE "
        "place read access is decided (SECURITY.md, CLAUDE.md's invariant table). If this one is "
        "deliberate, it needs a name that cannot be mistaken for the enforcement predicate AND a "
        "parity test against the serving half.")


@pytest.mark.parametrize(("acl", "audiences"), _WELL_FORMED)
def test_the_serving_half_reads_the_csv_shape_identically_to_the_list_shape(acl, audiences):
    """The index holds `acl` as a Postgres `text[]`; the serving half ALSO normalizes a bare CSV
    string, a shape no live caller produces any more but which must keep failing closed rather
    than crashing if a stored `acl` ever arrives that way. One enforcement point takes both, and
    a divergence between the two shapes would scope the same content two different ways."""
    csv = None if acl is None else ",".join(acl)
    assert serving_visible(csv, audiences) == serving_visible(acl, audiences)


@pytest.mark.parametrize("malformed", [{"a": 1}, True, 7, [None], [{"x": 1}], object()])
def test_a_stored_value_that_cannot_be_parsed_fails_closed_for_every_client(malformed):
    """`server/acl.py` states it outright: a stored value we cannot parse must never resolve to
    "open" at the point access is decided, *not even for an unrestricted client*.

    This used to assert a deliberate DIVERGENCE from the kernel half, which returned `True` here.
    That half is gone; the property it was contrasted against is the one that mattered, so it is
    asserted on its own terms now."""
    assert serving_visible(malformed, None) is False
    assert serving_visible(malformed, {"eng"}) is False


# ══════════════════════════════════════════════════════════════════════════════════════════════
# The `[]`-stays-`[]` invariant, pinned. Load-bearing, and unguarded for far too long.
#
# `acl: []` means NOBODY at every serving reader (`server.acl.visible`'s own truth table), and
# `acl:` ABSENT means open. They are two values, not two spellings of one, and the audience-from-the-door change makes
# that a corpus-wide rule after the librarian's old resolver spent years translating a resolved
# empty list back into "no line at all" — so the one spelling `ops/acl.json` used to restrict
# meant its opposite once stamped.
#
# Two independent implementations have to agree for the rule to hold:
#
#   * `page.stamp_server_fields` must WRITE `acl: []` and not omit the key;
#   * `index.corpus._acl_labels` must READ `acl: []` back as `[]` and not as `None`.
#
# If EITHER collapsed empty to open, a page the writer meant for nobody would index as visible to
# everyone. Nothing DERIVES `[]` today — the one thing that ever did was a stored per-entity
# rollup, computing its audience as the intersection over members that shared none, and both the
# rollup and the derivation are gone. The rule is pinned here anyway: a value that means the
# opposite of its neighbour is worth holding BEFORE something derives one again, which is exactly
# the order in which it was got wrong the first time.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_the_retired_acl_symbols_stay_retired():
    """The same posture `test_contract_parity` already takes for `kernel.acl.visible` and
    `Settings.acl_path`: a deleted access-control function is one autocomplete away from being
    reintroduced, and the reintroduced one would not be wired to anything that tests it.

    `resolve_acl`/`load_acl_config` derived a label from a path, before the audience came from the
    door; `view_acl` collapsed a derived rollup to nobody; `all_visible` never had a caller."""
    from stigmergy.kernel import acl as kernel_acl
    from stigmergy.server import acl as server_acl

    for name in ("resolve_acl", "load_acl_config", "load_acl_config_text", "view_acl"):
        assert not hasattr(kernel_acl, name), (
            f"kernel.acl.{name} is back — it was deleted when the audience came from the door, "
            f"and a second way to answer an access question is a second thing that can answer it "
            f"differently")
    assert not hasattr(server_acl, "all_visible"), (
        "server.acl.all_visible is back — it had no caller, and a predicate nothing calls is a "
        "predicate nothing tests")


def test_the_write_side_writes_an_empty_acl_rather_than_omitting_the_key():
    """The two spellings must stay two values on the WRITE side, or the read side's truth table
    below is decorative. The producer this pins is the stamper every filed page goes through —
    the only one left, now that nothing derives an audience from other pages."""
    from stigmergy.librarian.page import stamp_server_fields

    page = "---\ntype: note\ntitle: t\n---\n\n# t\n"
    nobody = stamp_server_fields(page, submitted_by="a@b", acl=[], as_of="2026-08-22")
    assert "acl: []" in nobody, nobody
    # ...and open is a DIFFERENT answer — an omitted line — which is what makes `[]` meaningful.
    opened = stamp_server_fields(page, submitted_by="a@b", acl=None, as_of="2026-08-22")
    assert "acl:" not in opened, opened


def test_the_read_side_reads_an_empty_acl_back_as_nobody_never_as_open():
    from stigmergy.index.corpus import _acl_labels
    assert _acl_labels({"acl": []}) == [], (
        "`acl: []` read back as anything but `[]` — if this became `None`, a page written for "
        "nobody would index as OPEN and be served to everyone")
    assert _acl_labels({}) is None                  # absent = open, the OTHER value
    assert _acl_labels({"acl": None}) is None       # explicit null = open


def test_the_two_sides_compose_a_page_stamped_at_nobody_is_visible_to_nobody():
    """The invariant end to end, over the values rather than the prose: the stamper's output fed
    to the reader, then to the one enforcement point. This is the assertion that would have caught
    a collapse in EITHER half, which is why it exists beside the two unit checks and not instead
    of them."""
    import yaml

    from stigmergy.index.corpus import _acl_labels
    from stigmergy.librarian.page import split_frontmatter, stamp_server_fields
    from stigmergy.server.acl import visible

    page = stamp_server_fields("---\ntype: note\ntitle: t\n---\n\n# t\n",
                               submitted_by="a@b", acl=[], as_of="2026-08-22")
    front, _body = split_frontmatter(page)
    stored = _acl_labels(yaml.safe_load(front))      # what the index stores
    assert stored == []
    assert visible(stored, {"finance"}) is False
    assert visible(stored, {"sales"}) is False
    assert visible(stored, set()) is False
    # Unrestricted still sees it — `visible`'s documented truth table, not an accident: an operator
    # with full read is inside the trust boundary, and hiding it from them would hide the fact that
    # a page came out unshareable at all.
    assert visible(stored, None) is True
