"""`entities.birth` — resolve-before-mint, and the page it renders.

Pure over `(Registry, existing_pages)` (module docstring: "the whole of resolve-before-mint is
testable without git and the CLI cannot accidentally check against a different registry than the
one the commit will regenerate from"), so nothing here touches a filesystem or a database.
"""
import pytest
import yaml

from stigmergy.entities import birth, generator
from stigmergy.entities.errors import CollisionError, EntityError
from stigmergy.kernel.registry import Registry

_SEED_ENTITIES = (
    generator.PageEntity(canonical_id="acme", name="Acme", entity_type="organization",
                        aliases=("Acme Corp",), relpath="wiki/entities/Acme.md"),
    generator.PageEntity(canonical_id="jordan-reyes", name="Jordan Reyes",
                        entity_type="person", aliases=("Jordan Reyes Gaya",),
                        relpath="wiki/entities/Jordan Reyes.md"),
)


def _registry() -> Registry:
    """Built through `generator.registry_of` — the reader's own indexing code (module docstring:
    "populated by exactly the code that populates it on load"), never a hand-rolled `by_alias`."""
    return generator.registry_of(_SEED_ENTITIES)


TEMPLATE = """---
type: entity
title: "<Entity Name>"
status: developing        # seed|developing|mature|canonical (canonical requires `owner`)
entity_type: organization # person|organization|product|tool|repository|place|project
role: ""
aliases: []
created: <YYYY-MM-DD>
updated: <YYYY-MM-DD>
tags: [entity]
entity: []  # THIS PAGE'S OWN registry id, once minted — e.g. entity: ["<own-id>"]
related: []
sources: []
---

# <Entity Name>

## What / Who

<One clear paragraph: what this entity is and why it's in the brain.>
"""


_BODY = birth.prepare_body(summary="A fictional conglomerate the tests mint pages for.")


def _render(template, proposal, **kwargs):
    """`birth.render_page` with a body supplied: a page is never born without its What / Who, so
    every test not ABOUT the body says one and gets on with what it is testing."""
    kwargs.setdefault("body", _BODY)
    return birth.render_page(template, proposal, **kwargs)


def _prepare(**kwargs):
    kwargs.setdefault("registry", _registry())
    kwargs.setdefault("existing_pages", ())
    return birth.prepare(**kwargs)


# ── resolve-before-mint refuses a colliding id/name/alias, naming the mechanism ──────────────────
def test_a_name_that_already_resolves_is_refused_and_names_the_registered_entry():
    """The refusal names the MECHANISM — which entry it collided with, not just "collision"."""
    with pytest.raises(CollisionError) as exc:
        _prepare(canonical_id="acme-corp", name="Acme Corp", entity_type="organization")
    message = str(exc.value)
    assert "Acme Corp" in message
    assert "acme" in message              # the entry it resolves to
    assert "name: Acme" in message
    assert "type: organization" in message


def test_the_mint_gate_still_folds_a_legal_form_that_filing_no_longer_folds():
    """**The half of issue #77 that did NOT move, pinned so it cannot move by accident.**

    `kernel.normalize` split one key into two: filing resolves through `canonical_id`, which folds
    accents, case and punctuation and deliberately not a legal form, because deciding that
    `Acme S.L.` is `Acme` is a claim about the world and belongs to the agent. This gate asks the
    opposite question and keeps the coarse fold, because its failure direction is opposite too — a
    false negative here mints a SECOND identity for one company and nothing ever reconciles them,
    while a false positive costs a steward one refusal they can read and argue with.

    So the same string has two different, both-correct answers, and this is where that is stated:
    the registry does not RESOLVE `Acme S.L.` and still REFUSES to mint it.
    """
    registry = _registry()
    assert registry.canonical_id("Acme S.L.") is None, (
        "filing must not fold a legal form any more — that is the judgment this issue moved")

    with pytest.raises(CollisionError) as exc:
        _prepare(canonical_id=generator.canonical_id_for("Acme S.L."), name="Acme S.L.",
                 entity_type="organization", registry=registry)

    assert "acme" in str(exc.value)


def test_an_alias_that_collides_with_a_different_entity_is_refused():
    with pytest.raises(CollisionError, match="an alias"):
        _prepare(canonical_id="globex", name="Globex", entity_type="organization",
                aliases=["Jordan Reyes"])


def test_an_id_that_collides_is_refused_even_when_the_name_does_not():
    """Ordered name -> id -> aliases (module docstring): an id colliding via a DIFFERENT name is
    the case where `--id` really is the problem, and the message says so.

    Constructed so the two matchers genuinely disagree: `slugify` (the id) and `normalize` (the
    name-collision matcher) fold differently — `slugify` drops `!` entirely while `normalize`
    leaves it as a literal character — so `"Acme!!!"` slugs to the taken id `"acme"` while its own
    `normalize` key (`"acme!!!"`) resolves to nothing registered. The name check is silent and the
    id check is the one that fires, exactly the case the module docstring names.
    """
    name = "Acme!!!"
    assert generator.canonical_id_for(name) == "acme"          # the id this name WOULD take
    with pytest.raises(CollisionError, match="--id"):
        _prepare(canonical_id="acme", name=name, entity_type="organization")


def test_a_page_on_disk_but_not_in_the_registry_is_a_collision_too():
    """The `northwind-slides.md` next to `Northwind Group.md` class: a page can exist while the
    registry does not know it (drift), and that is a collision, not a free slot."""
    with pytest.raises(CollisionError, match="drift"):
        _prepare(canonical_id="zenith", name="Zenith", entity_type="organization",
                existing_pages=["wiki/entities/Zenith.md"])


def test_the_file_collision_check_is_case_insensitive():
    with pytest.raises(CollisionError):
        _prepare(canonical_id="zenith", name="ZENITH", entity_type="organization",
                existing_pages=["wiki/entities/zenith.md"])


# ── the benign twin: a genuinely new entity passes ───────────────────────────────────────────────
def test_a_genuinely_new_entity_is_accepted():
    proposal = _prepare(canonical_id="globex", name="Globex", entity_type="organization",
                        aliases=["Globex Corporation"])
    assert proposal.canonical_id == "globex"
    assert proposal.name == "Globex"
    assert proposal.aliases == ("Globex Corporation",)


def test_a_name_similar_but_not_colliding_passes():
    """`northwind-slides` vs `Northwind Group`-shaped near-miss: distinct after normalization."""
    proposal = _prepare(canonical_id="meridian-ventures", name="Meridian Ventures",
                        entity_type="organization")
    assert proposal.canonical_id == "meridian-ventures"


# ── --id must be the derived slug, never free-form (generator.canonical_id_for) ──────────────────
def test_a_mismatched_id_is_refused_and_states_the_correct_one():
    with pytest.raises(EntityError, match="globex"):
        _prepare(canonical_id="not-the-right-slug", name="Globex", entity_type="organization")


# ── name/role/alias/type validation ───────────────────────────────────────────────────────────────
def test_an_unknown_entity_type_is_refused():
    with pytest.raises(EntityError, match="organization"):
        _prepare(canonical_id="globex", name="Globex", entity_type="spaceship")


@pytest.mark.parametrize("entity_type", birth.ENTITY_TYPES)
def test_every_declared_entity_type_is_accepted(entity_type):
    proposal = _prepare(canonical_id="globex", name="Globex", entity_type=entity_type)
    assert proposal.entity_type == entity_type


def test_an_empty_name_is_refused():
    with pytest.raises(EntityError, match="empty"):
        _prepare(canonical_id="x", name="   ", entity_type="organization")


def test_a_name_over_the_character_ceiling_is_refused():
    with pytest.raises(EntityError, match=str(birth.MAX_NAME_CHARS)):
        _prepare(canonical_id="x", name="x" * (birth.MAX_NAME_CHARS + 1), entity_type="organization")


@pytest.mark.parametrize("bad_char", ['/', '\\', ':', '*', '?', '"', '<', '>', '|', '[', ']', '#'])
def test_a_name_carrying_a_forbidden_character_is_refused_and_names_it(bad_char):
    with pytest.raises(EntityError, match="cannot appear"):
        _prepare(canonical_id="x", name=f"Globex{bad_char}Corp", entity_type="organization")


def test_a_name_starting_with_a_dot_is_refused():
    with pytest.raises(EntityError, match="hidden file"):
        _prepare(canonical_id="x", name=".Globex", entity_type="organization")


def test_a_refusal_consequence_carrying_a_literal_brace_still_refuses_cleanly():
    """A brace in a consequence template must stay a refusal, never become a KeyError.
    OLD BEHAVIOUR: `consequence.format(found=found)` raised KeyError on any brace that was
    not `{found}`, turning a governance-path refusal into an unanticipated fault."""
    with pytest.raises(EntityError, match="cannot appear"):
        birth._refuse_forbidden('a/b', birth._FORBIDDEN_IN_NAME, subject="the name",
                                consequence='which cannot appear in a JSON example like {"a": 1}')


def test_the_refusal_consequence_found_placeholder_still_substitutes():
    """The benign twin: the one sanctioned `{found}` placeholder keeps working."""
    with pytest.raises(EntityError, match=r"repeats \'/\'"):
        birth._refuse_forbidden('a/b', birth._FORBIDDEN_IN_NAME, subject="the name",
                                consequence="which repeats {found} here")


# ── control characters: invisible in every field a steward reads them in ────────────────────────
# OLD BEHAVIOUR: they reached a signed commit. `" ".join(value.split())` folds only the characters
# Python calls whitespace, and `\x01` is not one; `_FORBIDDEN_IN_NAME` lists none of them either.
# The admin console happened to be safe because `admin/service.py::_traced_fields` cleans string
# values on the way out — a DISPLAY defence on ONE surface, not the gate. Slack and MCP had none:
# a capture naming an organization `"\x01 Acme"` parked, the doorbell offered a mint modal already
# filled in with it, and a steward accepting the default minted a page whose title and FILENAME
# carried a character nobody can type.
@pytest.mark.parametrize("bad_char", ["\x00", "\x01", "\x08", "\x1a", "\x7f", "\x9b"])
def test_a_name_carrying_a_control_character_is_refused_and_names_its_code_point(bad_char):
    with pytest.raises(EntityError, match="control characters") as caught:
        _prepare(canonical_id="x", name=f"{bad_char} Globex", entity_type="organization")
    assert f"U+{ord(bad_char):04X}" in str(caught.value), (
        "the refusal has to name the code point — the character is invisible, so a message "
        "quoting it back shows the operator an empty pair of quotes")


@pytest.mark.parametrize("field", ["alias", "role"])
def test_a_control_character_is_refused_in_an_alias_and_in_a_role_too(field):
    """An alias is a wikilink target and a registry key, so it carries the name's whole problem.
    `role` is only a sentence — held to this rule anyway, because the question is not what the
    character breaks syntactically, it is whether a steward can see what they are approving."""
    kwargs = {"aliases": ["Glob\x01ex Corp"]} if field == "alias" else {"role": "the \x01 boss"}
    with pytest.raises(EntityError, match="control characters"):
        _prepare(canonical_id="globex", name="Globex", entity_type="organization", **kwargs)


def test_a_stripped_name_is_never_minted_in_place_of_the_refusal():
    """REFUSE, never strip, and this is the assertion that keeps it that way.

    Silently dropping the character would mint `Globex` from a value a steward read as `Globex`
    and approved — indistinguishable to them, and the artefact is a filename in a signed commit.
    A future "be helpful, just clean it" edit passes every other test in this file.
    """
    with pytest.raises(EntityError):
        _prepare(canonical_id="globex", name="Glob\x01ex", entity_type="organization")


# ── the benign twin, which this gate needs more than most: it stands in front of real work ──────
@pytest.mark.parametrize("name", [
    "Peña-Rodríguez S.L.",          # the issue's own example: accents and a hyphen
    "Café Zürich",
    "O'Brien & Sons",
    "Møller-Maersk",
    "Ação Digital, Lda.",
    "北京科技",                       # nothing in this rule is about ASCII
    "Globex  Corp",                 # collapsed to one space by the normalizer, not refused
])
def test_ordinary_names_a_steward_legitimately_types_still_mint(name):
    """A test that only proves a gate fires measures its sensitivity and never its specificity.

    Every name here is one somebody's real company or colleague is called. They also cover the
    characters most likely to be caught by a range written by hand instead of asked as a Unicode
    category: accented letters, a right single quote, a slashed O, CJK.
    """
    collapsed = " ".join(name.split())
    proposal = _prepare(canonical_id=generator.canonical_id_for(collapsed), name=name,
                        entity_type="organization")
    assert proposal.name == collapsed


def test_a_tab_and_a_newline_are_still_collapsed_rather_than_refused():
    """The other half of specificity. Tab and newline ARE control characters by category, and a
    name pasted out of a document routinely carries them — so the check runs AFTER the whitespace
    collapse that already folds them into single spaces, and only the invisible remainder reaches
    it. Refusing a pasted tab would make this gate bounce ordinary work every day."""
    proposal = _prepare(canonical_id="globex-corp", name="Globex\tCorp\n",
                        entity_type="organization")
    assert proposal.name == "Globex Corp"


def test_more_than_the_alias_ceiling_is_refused():
    aliases = [f"Alias {i}" for i in range(birth.MAX_ALIASES + 1)]
    with pytest.raises(EntityError, match="aliases"):
        _prepare(canonical_id="globex", name="Globex", entity_type="organization", aliases=aliases)


def test_an_alias_equal_to_the_name_is_silently_dropped_not_refused():
    """A self-alias is redundant, not hostile — `_clean_aliases`'s own doctrine."""
    proposal = _prepare(canonical_id="globex", name="Globex", entity_type="organization",
                        aliases=["Globex"])
    assert proposal.aliases == ()


def test_duplicate_aliases_collapse_to_one_order_preserving():
    proposal = _prepare(canonical_id="globex", name="Globex", entity_type="organization",
                        aliases=["Globex Corp", "Globex Corp", "GX"])
    assert proposal.aliases == ("Globex Corp", "GX")


@pytest.mark.parametrize("bad_char", ['"', '\\'])
def test_a_role_carrying_a_yaml_unsafe_character_is_refused(bad_char):
    with pytest.raises(EntityError, match="role"):
        _prepare(canonical_id="globex", name="Globex", entity_type="organization",
                role=f'CEO{bad_char}boss')


def test_a_role_with_an_ordinary_single_quote_and_colon_is_accepted():
    """The benign twin of the role refusal above — `--role "CEO: 'the boss'"` is a plausible
    human sentence and must not be refused just because it carries punctuation `_yaml_str` escapes
    correctly."""
    proposal = _prepare(canonical_id="globex", name="Globex", entity_type="organization",
                        role="CEO: 'the boss'")
    assert proposal.role == "CEO: 'the boss'"


def test_an_invalid_today_is_refused():
    with pytest.raises(EntityError, match="YYYY-MM-DD"):
        _render(TEMPLATE, _prepare(canonical_id="globex", name="Globex",
                                            entity_type="organization"), today="07/27/2026")


# ── render_page fills the template and preserves its body ────────────────────────────────────────
def test_render_page_fills_every_declared_field():
    proposal = _prepare(canonical_id="globex", name="Globex", entity_type="organization",
                        aliases=["Globex Corp"], role="a fictional conglomerate")
    page = _render(TEMPLATE, proposal, today="2026-07-27")
    front = yaml.safe_load(page.split("---")[1])
    assert front["title"] == "Globex"
    assert front["entity_type"] == "organization"
    assert front["aliases"] == ["Globex Corp"]
    assert front["role"] == "a fictional conglomerate"
    assert front["status"] == "developing"
    # a BARE (unquoted) YAML date, deliberately (`_clean_today`'s docstring: quoting it would
    # silently change its type against every hand-authored page) — PyYAML reads it back as a
    # `datetime.date`, not a string.
    import datetime
    assert front["created"] == front["updated"] == datetime.date(2026, 7, 27)
    assert "# Globex" in page
    assert "A fictional conglomerate the tests mint pages for." in page   # the body is written
    assert "<One clear paragraph" not in page


# ── a minted entity page anchors to ITSELF ───────────────────────────────────────────────────────
def test_a_minted_entity_page_anchors_to_its_own_registry_id():
    """The template ships `entity: []` with a comment promising the page's own id "once minted",
    and `views.skeleton.entity_own_page`'s docstring states that governed birth mints exactly one
    such page "self-anchored (`entity: [<id>]`)" — but `render_page`'s field dict used to carry no
    `entity` key at all, so what landed was the template's literal `[]`.

    It is not a cosmetic omission. Under the anchor contract, `entity: []` inside `wiki/**` means
    "a checked, explicit company-wide declaration" — so a minted entity page did not merely fail
    to say what it was about, it said it was about the whole company."""
    proposal = _prepare(canonical_id="globex", name="Globex", entity_type="organization")
    page = _render(TEMPLATE, proposal, today="2026-07-27")
    front = yaml.safe_load(page.split("---")[1])
    assert front["entity"] == ["globex"] == [proposal.canonical_id]


def test_the_self_anchor_is_the_DERIVED_id_the_registry_will_regenerate():
    """The anchor must be the id `generator.canonical_id_for` derives from the NAME — the same one
    the next `stigmergy-entities regenerate` will produce from the page. Anchoring to anything else
    would put an id on the page that the registry does not contain, and `gate_anchoring` would
    then refuse every later page trying to anchor through it. Uses a multi-word name so the
    derived slug is visibly not the name."""
    proposal = _prepare(canonical_id="globex-industries", name="Globex Industries",
                        entity_type="organization")
    front = yaml.safe_load(_render(TEMPLATE, proposal, today="2026-07-27")
                           .split("---")[1])
    assert front["entity"] == ["globex-industries"] == [proposal.canonical_id]


def test_the_self_anchor_is_written_through_the_yaml_escaper_like_every_other_authored_value():
    """This module's own rule (`render_page`'s docstring): "Every value that came from a human
    goes through `_yaml_str`, none through an f-string." The canonical id is derived from a
    steward-authored name, so it is one of those values — pinned here rather than left to the
    reviewer to notice, because the id is the LAST field added to that dict and the next one will
    be copied from it."""
    page = _render(TEMPLATE, _prepare(canonical_id="globex", name="Globex",
                                                entity_type="organization"), today="2026-07-27")
    assert 'entity: ["globex"]' in page


def test_render_page_refuses_a_template_with_no_frontmatter():
    with pytest.raises(EntityError, match="frontmatter"):
        _render("# just a heading\n", _prepare(canonical_id="globex", name="Globex",
                                                         entity_type="organization"),
                          today="2026-07-27")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# Aliases interpolated into YAML unescaped used to smuggle a second alias past the collision
# gate. Two layers, tested independently (module docstring: "the repair is two layers that do
# not share a mechanism").
# ══════════════════════════════════════════════════════════════════════════════════════════════
YAML_BREAK_ALIAS = 'x", "Jordan Reyes'


def test_layer_1_the_validator_refuses_the_yaml_breaking_alias():
    """`_clean_aliases` refuses any alias carrying a character in `_FORBIDDEN_IN_NAME` — `"` among
    them — so the attack never reaches a registered collision at all: it is refused before the
    collision gate is even asked, for a reason a steward can read (a name/alias character rule),
    which is the correct refusal — and it names the mechanism that fired."""
    with pytest.raises(EntityError, match=r"cannot appear"):
        _prepare(canonical_id="globex", name="Globex", entity_type="organization",
                aliases=[YAML_BREAK_ALIAS])


def test_layer_2_render_page_escapes_a_hostile_alias_constructed_directly():
    """Bypass the validator entirely — construct `Proposal` directly — and prove the SECOND,
    independent layer: `render_page` must render ONE alias that reads back as ONE alias,
    round-tripped through `yaml.safe_load`, never smuggling the registered name back in.
    """
    smuggled = birth.Proposal(canonical_id="globex", name="Globex", entity_type="organization",
                              aliases=(YAML_BREAK_ALIAS,), role="")
    page = _render(TEMPLATE, smuggled, today="2026-07-27")
    front = yaml.safe_load(page.split("---")[1])
    assert front["aliases"] == [YAML_BREAK_ALIAS]
    assert "Jordan Reyes" not in front["aliases"]


@pytest.mark.parametrize("hostile_role", ['CEO, "the boss"', "path\\to\\somewhere"])
def test_layer_2_render_page_escapes_a_hostile_role_constructed_directly(hostile_role):
    smuggled = birth.Proposal(canonical_id="globex", name="Globex", entity_type="organization",
                              aliases=(), role=hostile_role)
    page = _render(TEMPLATE, smuggled, today="2026-07-27")
    front = yaml.safe_load(page.split("---")[1])
    assert front["role"] == hostile_role


def test_layer_2_a_control_character_in_a_directly_constructed_alias_still_parses():
    """`_yaml_str` escapes control bytes too (an ESC one paste away from a terminal) — a page
    minted from a hostile value must stay PARSEABLE, not merely "not the wrong value"."""
    smuggled = birth.Proposal(canonical_id="globex", name="Globex", entity_type="organization",
                              aliases=("Globex\x1b[31m",), role="")
    page = _render(TEMPLATE, smuggled, today="2026-07-27")
    front = yaml.safe_load(page.split("---")[1])       # must not raise
    assert front["aliases"] == ["Globex\x1b[31m"]


# ── benign twins for the YAML-escaping layer: ordinary punctuation a steward legitimately types ──
@pytest.mark.parametrize("name,aliases,role", [
    ("Globex", ["Globex Corporation"], ""),
    ("Globex", [], "CEO, 'the boss'"),
    ("Café Zürich", ["Café Zürich Ltd"], ""),
])
def test_ordinary_punctuation_and_accents_round_trip_through_render_page(name, aliases, role):
    proposal = _prepare(canonical_id=generator.canonical_id_for(name), name=name,
                        entity_type="organization", aliases=aliases, role=role,
                        existing_pages=())
    page = _render(TEMPLATE, proposal, today="2026-07-27")
    front = yaml.safe_load(page.split("---")[1])
    assert front["title"] == name
    assert front["aliases"] == list(proposal.aliases)
    assert front["role"] == role


# ── recheck() re-asks the SAME gate against a moved registry ─────────────────────────────────────
def test_recheck_refuses_when_the_registry_moved_underneath_the_first_pass():
    proposal = _prepare(canonical_id="globex", name="Globex", entity_type="organization")
    moved = generator.registry_of((*_SEED_ENTITIES,
                                   generator.PageEntity(canonical_id="globex", name="Globex",
                                                       entity_type="organization", aliases=(),
                                                       relpath="wiki/entities/Globex.md")))
    with pytest.raises(CollisionError):
        birth.recheck(proposal, registry=moved, existing_pages=())


def test_recheck_passes_when_nothing_relevant_moved():
    proposal = _prepare(canonical_id="globex", name="Globex", entity_type="organization")
    birth.recheck(proposal, registry=_registry(), existing_pages=())   # must not raise


# ── the lifecycle on the page: a proposal is `approved_by: ""`, an approval names a person ───────
def test_a_page_rendered_with_no_approver_is_a_proposal_the_generator_reads_as_such():
    """The librarian's page: `approved_by` PRESENT and EMPTY. Read back through the generator's own
    vocabulary rather than a string check, so the writer and the reader are pinned to each other."""
    proposal = _prepare(canonical_id="globex", name="Globex", entity_type="organization")
    page = _render(TEMPLATE, proposal, today="2026-07-27")
    front = yaml.safe_load(page.split("---")[1])
    assert front[generator.APPROVED_BY_KEY] == ""
    assert f'{generator.APPROVED_BY_KEY}: ""' in page


def test_a_page_rendered_with_an_approver_is_approved_by_that_person():
    """A steward's own `create` IS the approval, and the field says who."""
    proposal = _prepare(canonical_id="globex", name="Globex", entity_type="organization")
    page = _render(TEMPLATE, proposal, today="2026-07-27", approved_by="Test Steward")
    assert yaml.safe_load(page.split("---")[1])[generator.APPROVED_BY_KEY] == "Test Steward"


def test_the_approver_is_written_through_the_yaml_escaper_and_refused_on_control_characters():
    proposal = _prepare(canonical_id="globex", name="Globex", entity_type="organization")
    page = _render(TEMPLATE, proposal, today="2026-07-27", approved_by='Ann "A" Lee')
    assert yaml.safe_load(page.split("---")[1])[generator.APPROVED_BY_KEY] == 'Ann "A" Lee'
    with pytest.raises(EntityError, match=r"U\+001B"):
        _render(TEMPLATE, proposal, today="2026-07-27", approved_by="Ann\x1bLee")


# ── the body: the librarian fills every section it can; a steward's stub keeps the template ─────
def test_render_page_fills_the_template_sections_from_a_prepared_body():
    proposal = _prepare(canonical_id="globex", name="Globex", entity_type="organization")
    body = birth.prepare_body(
        summary="Globex is a fictional conglomerate that the brain tracks as a client.",
        facts=["Signed a reporting pilot in August 2026", "Headquartered in Springfield"],
        connections=["[[Globex Reporting Pilot]] — the note that introduced it"])
    page = _render(TEMPLATE, proposal, today="2026-07-27", body=body,
                             related=["Globex Reporting Pilot"])
    front, _, rest = page.partition("\n---\n")
    assert "<One clear paragraph" not in page       # the stub was replaced, not kept beside
    assert "Globex is a fictional conglomerate" in rest
    assert "- Signed a reporting pilot in August 2026" in rest
    assert "- Headquartered in Springfield" in rest
    assert "## Facts" in rest and "## Connections" in rest   # headings the TEMPLATE lacked were added
    assert "- [[Globex Reporting Pilot]] — the note that introduced it" in rest
    assert yaml.safe_load(front.split("---")[1])["related"] == ["[[Globex Reporting Pilot]]"]
    assert "\n\n\n" not in page


def test_prepare_body_bounds_and_cleans_every_field():
    body = birth.prepare_body(summary="  two\n  lines  ", facts=["a", "a", " ", "b"],
                              connections=[])
    assert body.summary == "two lines"
    assert body.facts == ("a", "b")                  # de-duplicated, blanks dropped, collapsed
    assert len(birth.prepare_body(summary="x" * 5000).summary) == birth.MAX_SUMMARY_CHARS
    with pytest.raises(EntityError, match="max"):
        birth.prepare_body(facts=[f"fact {i}" for i in range(birth.MAX_FACTS + 1)])
    with pytest.raises(EntityError, match=r"U\+0007"):
        birth.prepare_body(facts=["a\x07fact"])


def test_a_related_page_name_is_held_to_the_name_rules():
    proposal = _prepare(canonical_id="globex", name="Globex", entity_type="organization")
    with pytest.raises(EntityError, match="wikilink"):
        _render(TEMPLATE, proposal, today="2026-07-27", related=["a|b"])


# ── the template's own maintainer notes stay in the template ──────────────────────────────────
_TEMPLATE_WITH_NOTE = TEMPLATE.replace(
    "# <Entity Name>",
    "<!-- Placeholders here are PLAIN TEXT, unlike the agent-facing templates: this note is for\n"
    "     whoever edits THIS FILE, and a reader of a filed page has no use for it. -->\n\n"
    "# <Entity Name>")


def test_the_templates_html_comments_never_reach_the_rendered_page():
    """`ops/templates/entity.md` carries a note addressed to whoever edits the template. It was
    copied verbatim into every page the template rendered, so all nineteen entity pages in the
    real brain opened with a paragraph explaining how the template works — including the ones the
    librarian filled. A comment in the template is a note to its maintainer; the page a person
    reads carries none."""
    proposal = _prepare(canonical_id="globex", name="Globex", entity_type="organization")

    page = _render(_TEMPLATE_WITH_NOTE, proposal, today="2026-07-27")

    assert "<!--" not in page and "-->" not in page
    assert "whoever edits THIS FILE" not in page


def test_the_body_the_comment_sat_in_survives_intact():
    """The benign twin: stripping the note must not take the page with it. Every heading and every
    stub the template declares still arrives, and so does the entity's own name."""
    proposal = _prepare(canonical_id="globex", name="Globex", entity_type="organization")

    body = birth.prepare_body(summary="Globex makes everything.", facts=["Founded in 1989."],
                              connections=["[[Acme Corp]] — a rival"])
    page = _render(_TEMPLATE_WITH_NOTE, proposal, today="2026-07-27", body=body)
    plain = _render(TEMPLATE, proposal, today="2026-07-27", body=body)

    assert page == plain, "the note was the only difference between the two templates"
    assert "# Globex" in page
    for heading in ("## What / Who", "## Facts", "## Connections"):
        assert heading in page


def test_a_page_with_nothing_said_about_the_entity_is_not_written_at_all():
    """Twelve of the first brain's nineteen entity pages were born with the template's stubs for a
    body — `<One clear paragraph: ...>` — because the hand doors rendered the template with no
    account of the entity. GitHub hid the stubs as HTML, the index ranked the pages as knowledge,
    and `ask` had nothing to say about them. The render refuses: an entity is born written, or
    not at all."""
    proposal = _prepare(canonical_id="globex", name="Globex", entity_type="organization")
    with pytest.raises(EntityError, match="nothing said about it"):
        birth.render_page(TEMPLATE, proposal, today="2026-07-27")
    with pytest.raises(EntityError, match="nothing said about it"):
        birth.render_page(TEMPLATE, proposal, today="2026-07-27", body=birth.prepare_body())


def test_a_section_given_no_lines_is_dropped_heading_and_stub_together():
    """A body with a summary and no facts must not carry `- <fact, and the page it came from ...>`:
    a reader takes it for a fact, and the gardener has to find it later to say there is none. The
    heading goes with the stub; the sections that have content keep their place."""
    proposal = _prepare(canonical_id="globex", name="Globex", entity_type="organization")
    page = _render(TEMPLATE, proposal, today="2026-07-27",
                   body=birth.prepare_body(summary="Globex makes everything.",
                                           connections=["[[Acme Corp]] — a rival"]))
    body = page.split("---", 2)[2]
    assert "## What / Who" in body and "Globex makes everything." in body
    assert "## Connections" in body and "[[Acme Corp]] — a rival" in body
    assert "## Facts" not in body
    assert "<" not in body.replace("<!--", ""), "no placeholder survives on a page that lands"
