"""`kernel.normalize` — the two keys, and the line between them.

`kernel/index.md` has carried a standing debt for as long as this module has existed: *"`normalize`
carries the legal-suffix table entity identity depends on; a change there needs a suite added with
it."* Issue #77 is that change, so this is that suite.

**What it is actually about.** One function used to answer two questions, and the answers pull in
opposite directions:

* *"which registered entity does this text MEAN?"* — asked at filing time, where a false positive
  anchors a page to the wrong entity SILENTLY and corrupts a timeline nobody will re-read;
* *"would this NEW name be confused with one we already have?"* — asked at the mint gate, where a
  false NEGATIVE lets a duplicate identity through and the refusal falls CLOSED onto a human.

So the folds are split: `resolution_key` folds only what is not a judgment (accents, case,
punctuation), and `normalize` keeps the legal-suffix table on top of it for the gate that needs it.
Every test below is written against a spelling that discriminates the two, because a case both keys
answer the same way proves nothing about the split.
"""
import pytest

from stigmergy.kernel.normalize import normalize, resolution_key, slugify


# ── what BOTH keys fold, because none of it is a claim about the world ─────────────────────────
@pytest.mark.parametrize("spelling", [
    "Cofers",
    "COFERS",
    "  cofers  ",
    "Cofers.",
    "(Cofers)",
    'Cofers"',
    "Cofers/",
    "Côfers",
])
def test_case_accents_punctuation_and_spacing_are_the_same_name_under_both_keys(spelling):
    """A keyboard, a locale and a copy-paste are not evidence about identity. Both keys fold them,
    and a resolver that did not would park a capture about a registered customer over an accent."""
    assert resolution_key(spelling) == "cofers"
    assert normalize(spelling) == "cofers"


def test_an_empty_or_whitespace_only_name_folds_to_nothing_under_both_keys():
    """`""` is what `registry.index_entity` refuses to key on — a blank alias that produced a real
    key would resolve every name that folded to nothing."""
    for spelling in ("", "   ", "\n", "..."):
        assert resolution_key(spelling) == ""
        assert normalize(spelling) == ""


# ── what only the COLLISION key folds ──────────────────────────────────────────────────────────
# Each of these is a real spelling `Registry.canonical_id` used to resolve and deliberately no
# longer does. They are the whole subject of issue #77's "the suffix list is the DEFECT".
@pytest.mark.parametrize("spelling", [
    "Cofers SL",
    "Cofers S.L.",
    "Cofers, S.L.",
    "Cofers Inc",
    "COFERS LTD",
    "Cofers Limited",
    "Cofers Corp",
    "Cofers Co",
    "Cofers GmbH",
    "Cofers B.V.",
    "Cofers S.L.U.",
    "Cofers Sociedad Limitada",
])
def test_a_legal_form_is_folded_by_the_collision_key_and_kept_by_the_resolution_key(spelling):
    """OLD BEHAVIOUR: one function stripped these, so `canonical_id("Cofers SL")` answered `cofers`
    at FILING time — code deciding, with no human anywhere, that two names denote one company.

    It is right by accident and wrong in silence: `co`, `corp`, `limited` and `sa` are all in the
    table, so the day `Cofers` and `Cofers Co` are two real organizations — or an entity is
    genuinely named `Limited` — code merges them and nothing anywhere says so. That judgment is the
    agent's now, fenced by a resolved id having to EXIST and by uncertainty parking.

    The collision key is UNCHANGED and this pins that too: at the mint gate the same fold refuses a
    second `Cofers SL` beside a registered `Cofers`, to a human, which is the direction that fails
    safe.
    """
    assert normalize(spelling) == "cofers"
    assert resolution_key(spelling) != "cofers"


def test_the_collision_key_strips_a_stack_of_suffixes_not_only_the_last_one():
    """`S.L.` after `Inc` is still one company as far as the mint gate is concerned — the loop runs
    to a fixed point, which is what makes the gate hard to walk past by appending a second form."""
    assert normalize("Cofers Inc S.L.") == "cofers"


@pytest.mark.parametrize("spelling,expected", [
    ("Cofers Holdings", "cofers holdings"),
    ("Cofers Group", "cofers group"),
    ("Cofers España", "cofers espana"),
    ("Cofers (formerly Nubelo)", "cofers formerly nubelo"),
    ("Cofers Legal", "cofers legal"),
])
def test_a_qualifier_is_folded_by_NEITHER_key(spelling, expected):
    """The cases the table never covered, and the reason a longer table is not the fix. A qualifier,
    a former name and a regional variant are claims about the world; `Cofers Legal` is the one that
    shows why guessing is unsafe, because it is a genuinely different organization sharing a prefix.

    Both keys agree here, and the agreement is the point: the mint gate must NOT refuse
    `Cofers Legal` as a collision with `Cofers`, and filing must not resolve it to `cofers` either.
    """
    assert resolution_key(spelling) == normalize(spelling) == expected


# ── the third key, which answers a third question ───────────────────────────────────────────────
def test_slugify_is_an_id_and_not_a_matcher():
    """`entities.generator.canonical_id_for` uses `slugify` and says why in its own docstring: an
    id is a stable file-safe handle, so using the coarse matcher for it would give `Acme Corp` the
    id `acme` and make two genuinely different entities fight over one slot. Pinned here beside the
    two matchers so the three-way distinction is readable in one place."""
    assert slugify("Cofers Corp") == "cofers-corp"
    assert normalize("Cofers Corp") == "cofers"
    assert resolution_key("Cofers Corp") == "cofers corp"


def test_slugify_never_answers_with_an_empty_handle():
    """A page whose title folds to nothing still needs a filename — `x` is the documented floor,
    and an empty id would collide with every other empty one."""
    assert slugify("···") == "x"
    assert len(slugify("a" * 200)) == 60
