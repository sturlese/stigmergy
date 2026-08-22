"""`librarian.page`: placement policy (the three creatable types vs. the full page vocabulary) and
the server-owned frontmatter stamp — pure functions, no I/O.
"""
import pytest

from stigmergy.librarian import page as page_policy


# ── classify_page_type: the shared base every placement question goes through ──────────────────
@pytest.mark.parametrize("creatable_type", sorted(page_policy.FAST_LANE_TYPES))
def test_every_fast_lane_type_is_known_creatable_and_has_a_folder(creatable_type):
    policy = page_policy.classify_page_type(creatable_type)
    assert policy.known is True
    assert policy.creatable is True
    assert policy.folder == page_policy.FOLDER_BY_TYPE[creatable_type]
    assert policy.reason == ""


@pytest.mark.parametrize("governed_type", sorted(page_policy.ALL_PAGE_TYPES
                                                 - page_policy.FAST_LANE_TYPES))
def test_every_governed_type_is_known_but_not_creatable_with_a_reason(governed_type):
    policy = page_policy.classify_page_type(governed_type)
    assert policy.known is True
    assert policy.creatable is False
    assert policy.folder == ""
    assert policy.reason != ""


def test_an_unknown_type_is_neither_known_nor_creatable():
    policy = page_policy.classify_page_type("wizardry")
    assert policy.known is False
    assert policy.creatable is False
    assert "not a page type this brain has" in policy.reason


def test_classify_page_type_normalizes_case_and_whitespace():
    assert page_policy.classify_page_type("  Note ").creatable is True
    assert page_policy.classify_page_type("DECISION").folder == "wiki/decisions"


def test_classify_page_type_of_empty_or_none_is_unknown():
    assert page_policy.classify_page_type("").known is False
    assert page_policy.classify_page_type(None).known is False


# ── ensure_creatable / folder_for: the write guard the zone gate calls ─────────────────────────
def test_ensure_creatable_raises_value_error_naming_the_reason_for_a_governed_type():
    with pytest.raises(ValueError, match="birth fold"):
        page_policy.ensure_creatable("entity")


def test_folder_for_a_creatable_type_matches_the_table():
    # `concept` is a fast-lane type with a folder of its own, which is what this checks
    assert page_policy.folder_for("concept") == "wiki/concepts"


def test_folder_for_a_governed_type_raises():
    # `entity` is a governed type — a name that is not a type at all would make this pass for the
    # wrong reason
    with pytest.raises(ValueError):
        page_policy.folder_for("entity")


# ── type_for_folder: the inverse lookup the zone gate uses ─────────────────────────────────────
def test_type_for_folder_resolves_every_fast_lane_folder_back_to_its_type():
    for page_type, folder in page_policy.FOLDER_BY_TYPE.items():
        assert page_policy.type_for_folder(f"{folder}/Some Page.md") == page_type


def test_type_for_folder_is_empty_for_a_folder_outside_the_fast_lane():
    assert page_policy.type_for_folder("ops/acl.json") == ""
    assert page_policy.type_for_folder("wiki/entities/Acme.md") == ""


# ── frontmatter: split ───────────────────────────────────────────────────────────────────────
def test_split_frontmatter_separates_the_block_from_the_rest():
    front, rest = page_policy.split_frontmatter("---\ntype: note\n---\n\nbody text\n")
    assert front == "type: note"
    assert rest == "\nbody text\n"     # the regex consumes the closing `---` and ONE newline


def test_split_frontmatter_with_no_leading_block_returns_the_whole_text_as_rest():
    front, rest = page_policy.split_frontmatter("just a body, no frontmatter\n")
    assert front == ""
    assert rest == "just a body, no frontmatter\n"


# ── stamp_server_fields, at the pure-function level ─────────────────────────────────────────────
DRAFT_WITH_FORGED_FIELDS = (
    "---\n"
    "type: note\n"
    'title: "A page"\n'
    "status: canonical\n"
    "created: 2026-01-01\n"
    "updated: 2026-01-01\n"
    "tags: [note]\n"
    'related: ["[[Acme Corp]]"]\n'
    "sources: []\n"
    "submitted_by: someone.else@example.com\n"
    "verification: verified\n"
    'acl: ["leadership"]\n'
    'content_hash: "sha256:deadbeef"\n'
    "owner: someone.else\n"
    # Findings cycle 1, 4.1: a QUOTED spelling of two more server-owned keys, the exact shape
    # `_KEY_RE` used to be blind to (`'status':` single-quoted, `"entity":` double-quoted). Widened
    # so both are stripped exactly like their bare siblings — see `_match_key`'s own docstring.
    '"entity": ["evil"]\n'
    "'status': canonical\n"
    "---\n\n# A page\n\nbody text\n")


def test_stamp_server_fields_forces_status_to_developing_regardless_of_the_draft():
    stamped = page_policy.stamp_server_fields(
        DRAFT_WITH_FORGED_FIELDS, submitted_by="real@example.com",
        acl=None, as_of="2026-07-26")
    assert "status: developing" in stamped
    assert "status: canonical" not in stamped


def test_stamp_server_fields_replaces_every_server_owned_key_and_strips_owner():
    stamped = page_policy.stamp_server_fields(
        DRAFT_WITH_FORGED_FIELDS, submitted_by="real@example.com",
        acl=None, as_of="2026-07-26")
    assert "someone.else@example.com" not in stamped
    assert "submitted_by: real@example.com" in stamped
    # `verification` stays in SERVER_OWNED_KEYS precisely so a forged one is STRIPPED, and nothing
    # re-stamps it — no verdict exists to write.
    assert "verification:" not in stamped
    assert "leadership" not in stamped
    assert "deadbeef" not in stamped
    assert "owner:" not in stamped
    assert "as_of: 2026-07-26" in stamped


def test_stamp_server_fields_preserves_non_server_owned_lines_untouched():
    stamped = page_policy.stamp_server_fields(
        DRAFT_WITH_FORGED_FIELDS, submitted_by="real@example.com",
        acl=None, as_of="2026-07-26")
    assert 'related: ["[[Acme Corp]]"]' in stamped
    assert "tags: [note]" in stamped
    assert '# A page' in stamped
    assert "body text" in stamped


def test_stamp_server_fields_with_acl_none_omits_the_acl_line_entirely():
    stamped = page_policy.stamp_server_fields(
        "---\ntype: note\ntitle: \"x\"\ntags: []\nrelated: []\nsources: []\n---\n\nbody\n",
        submitted_by="a@example.com", acl=None, as_of="2026-07-26")
    assert "acl:" not in stamped


def test_stamp_server_fields_with_acl_labels_writes_a_yaml_list():
    stamped = page_policy.stamp_server_fields(
        "---\ntype: note\ntitle: \"x\"\ntags: []\nrelated: []\nsources: []\n---\n\nbody\n",
        submitted_by="a@example.com", acl=["finance", "leadership"],
        as_of="2026-07-26")
    assert 'acl: ["finance", "leadership"]' in stamped


# ── entity: a server-owned key, always written (even empty) ────────────────────────────────────
def test_entity_is_a_server_owned_key():
    assert "entity" in page_policy.SERVER_OWNED_KEYS


def test_stamp_server_fields_writes_the_resolved_entity_list():
    stamped = page_policy.stamp_server_fields(
        DRAFT_WITH_FORGED_FIELDS, submitted_by="real@example.com",
        acl=None, as_of="2026-07-26", entity=["borealis-dynamics"])
    assert 'entity: ["borealis-dynamics"]' in stamped


def test_stamp_server_fields_writes_entity_empty_list_for_company_wide_and_never_omits_it():
    """Criterion 7: a company-wide outcome is filed with `entity: []` — present, not absent, so it
    is distinguishable from a pre-contract page with no `entity` key at all."""
    stamped = page_policy.stamp_server_fields(
        DRAFT_WITH_FORGED_FIELDS, submitted_by="real@example.com",
        acl=None, as_of="2026-07-26", entity=[])
    assert "entity: []" in stamped


def test_stamp_server_fields_deletes_a_capture_declared_entity_value(tmp_path=None):
    """Criterion 4: a capture whose material declares `entity: ["some-other-entity"]` is filed
    with the entity list the ANCHORING OUTCOME verified, and the declared value appears nowhere on
    the page."""
    drafted = ('---\ntype: note\ntitle: "x"\ntags: []\nrelated: []\nsources: []\n'
              'entity: ["some-other-entity"]\n---\n\nbody\n')
    stamped = page_policy.stamp_server_fields(
        drafted, submitted_by="real@example.com", acl=None,
        as_of="2026-07-26", entity=["borealis-dynamics"])
    assert "some-other-entity" not in stamped
    assert 'entity: ["borealis-dynamics"]' in stamped


def test_stamp_server_fields_strips_a_quoted_entity_key_forgery():
    """Findings cycle 1, 4.1: a capture-drafted `"entity": ["evil"]` (a QUOTED key) used to survive
    `_strip_keys`'s bare-only matcher and land NEXT TO the server's own `entity:` line — containment
    then rested on `yaml.safe_load`'s last-key-wins reading, an incidental property nothing
    asserted. `_match_key` now reads the quoted spelling too, so it is stripped exactly like a bare
    `entity:` forgery would be."""
    stamped = page_policy.stamp_server_fields(
        DRAFT_WITH_FORGED_FIELDS, submitted_by="real@example.com",
        acl=None, as_of="2026-07-26", entity=["borealis-dynamics"])
    assert "evil" not in stamped
    assert 'entity: ["borealis-dynamics"]' in stamped
    # exactly ONE `entity` declaration survives, not the forged one beside the server's own —
    # the duplicate-declaration post-condition (`gates.gate_frontmatter`) is the second, independent
    # backstop for this same property, tested separately in test_gates_unit.py.
    assert stamped.count("entity") == 1


# ── case and homoglyph spellings must strip like the real key ──────────────────────────────────
# `Entity:` and `еntity:` (Cyrillic е, U+0435) are genuinely DIFFERENT strings to a real YAML
# parser too — they are not "duplicates" `duplicate_top_level_keys` can catch, so the ONLY defense
# is that `_strip_keys` removes them before the page is ever committed. These are two of five
# known bypasses; the other three (YAML explicit-key syntax, a hex-escaped quoted key, a BOM) are
# covered at the gate level in test_gates_unit.py, because those parse to the exact
# string "entity" to PyYAML and so ARE real duplicates the parser-based backstop catches.
def test_stamp_server_fields_strips_a_mixed_case_entity_key_forgery():
    drafted = ('---\ntype: note\ntitle: "x"\ntags: []\nrelated: []\nsources: []\n'
              'Entity: ["evil"]\n---\n\nbody\n')
    stamped = page_policy.stamp_server_fields(
        drafted, submitted_by="real@example.com", acl=None,
        as_of="2026-07-26", entity=["borealis-dynamics"])
    assert "evil" not in stamped
    assert 'entity: ["borealis-dynamics"]' in stamped


def test_stamp_server_fields_strips_a_homoglyph_entity_key_forgery():
    """`еntity:` — the first character is Cyrillic е (U+0435), not Latin e (U+0065)."""
    drafted = ('---\ntype: note\ntitle: "x"\ntags: []\nrelated: []\nsources: []\n'
              'еntity: ["evil"]\n---\n\nbody\n')
    stamped = page_policy.stamp_server_fields(
        drafted, submitted_by="real@example.com", acl=None,
        as_of="2026-07-26", entity=["borealis-dynamics"])
    assert "evil" not in stamped
    assert 'entity: ["borealis-dynamics"]' in stamped


def test_normalize_key_folds_case_and_the_cyrillic_homoglyph_onto_the_ascii_spelling():
    assert page_policy.normalize_key("Entity") == page_policy.normalize_key("entity")
    assert page_policy.normalize_key("еntity") == page_policy.normalize_key("entity")


def test_stamp_server_fields_strips_a_single_quoted_status_key_forgery():
    stamped = page_policy.stamp_server_fields(
        DRAFT_WITH_FORGED_FIELDS, submitted_by="real@example.com",
        acl=None, as_of="2026-07-26")
    assert "'status'" not in stamped
    assert "status: canonical" not in stamped
    assert "status: developing" in stamped


def test_stamp_server_fields_entity_defaults_to_an_empty_list():
    stamped = page_policy.stamp_server_fields(
        DRAFT_WITH_FORGED_FIELDS, submitted_by="real@example.com",
        acl=None, as_of="2026-07-26")
    assert "entity: []" in stamped


def test_stamp_server_fields_on_a_page_with_no_frontmatter_still_stamps_something_honest():
    """No leading `---` block at all: the linter will reject the page for missing required
    fields (page.py's own docstring: "the honest outcome") — the stamp itself must not crash."""
    stamped = page_policy.stamp_server_fields(
        "just a body with no frontmatter block\n", submitted_by="a@example.com", acl=None, as_of="2026-07-26")
    assert stamped.startswith("---\n")
    assert "submitted_by: a@example.com" in stamped
    assert "just a body with no frontmatter block" in stamped


# ── page names: UTF-8 survives, the two impossible characters do not ───────────────────────────
# The defect this closes shipped to git: a title sanitizer spelled as an ASCII whitelist turned
# "Zürich" into "Z rich" in the filename, the H1, the `title` field AND the commit subject, and
# three pages on the real `main` still carry it. The direction of the fix is what these assert:
# keep the characters, and REFUSE the ones that genuinely cannot be in a filename.
@pytest.mark.parametrize("name", [
    "Zürich Review with Meridian Partners.md",
    "Müller Retrospective.md",
    "Åkerlund & Co Renewal.md",
    "設計会議.md",
    "Q3 Revenue — 2026.md",
])
def test_an_accented_or_non_ascii_page_name_is_perfectly_filable(name):
    assert page_policy.unnameable_reason(name) == ""


@pytest.mark.parametrize("name", ["a\x00b.md", "a\x1bb.md", "sub/dir.md", "back\\slash.md"])
def test_a_name_carrying_a_path_separator_or_a_control_byte_is_refused_not_approximated(name):
    reason = page_policy.unnameable_reason(name)
    assert reason
    assert "path separator" in reason or "control character" in reason


def test_an_empty_page_name_is_refused():
    assert page_policy.unnameable_reason("") != ""


# ── the additive edits code performs from the agent's declaration ──────────────────────────────
FLOW_PAGE = """---
type: note
title: "Existing"
related: ["[[Acme Corp]]"]
tags: [note]
---

# Existing

The body a human wrote, which nothing here may touch.
"""

BLOCK_PAGE = """---
type: note
related:
  - "[[Acme Corp]]"
tags: [note]
---

# Existing

Body.
"""

NO_RELATED_PAGE = """---
type: note
tags: [note]
---

# Existing

Body.
"""


def test_related_links_reads_the_flow_spelling_every_real_page_uses():
    assert page_policy.related_links(FLOW_PAGE) == ["[[Acme Corp]]"]


def test_related_links_reads_the_block_spelling_too():
    assert page_policy.related_links(BLOCK_PAGE) == ["[[Acme Corp]]"]


def test_related_links_of_a_page_with_no_related_field_is_empty():
    assert page_policy.related_links(NO_RELATED_PAGE) == []


def test_with_related_link_adds_to_a_flow_list_and_keeps_every_existing_link():
    out, changed = page_policy.with_related_link(FLOW_PAGE, "New Page")
    assert changed is True
    assert page_policy.related_links(out) == ["[[Acme Corp]]", "[[New Page]]"]
    assert "The body a human wrote" in out


def test_with_related_link_inserts_a_block_item_without_touching_the_existing_one():
    out, changed = page_policy.with_related_link(BLOCK_PAGE, "New Page")
    assert changed is True
    assert page_policy.related_links(out) == ["[[Acme Corp]]", "[[New Page]]"]
    # a pure insertion: every line the page already had survives byte for byte
    for line in BLOCK_PAGE.splitlines():
        assert line in out.splitlines()


def test_a_non_ascii_stem_lands_in_the_page_verbatim_never_as_an_escape():
    """`_yaml_list` used `json.dumps` with its `ensure_ascii` DEFAULT, so a stem like
    `sesión-de-planificación` was written as `sesi\\u00f3n-…` — valid JSON, but the contract
    linter resolves `[[…]]` targets literally, so every non-ASCII backlink it wrote was read as a
    dead link and the whole capture refused. A Spanish corpus hits this on the first accent."""
    out, changed = page_policy.with_related_link(FLOW_PAGE, "sesión-de-planificación")
    assert changed is True
    assert "[[sesión-de-planificación]]" in out
    assert "\\u" not in out

    stamped = page_policy.stamp_server_fields(
        FLOW_PAGE, as_of="2026-08-17", submitted_by="marc@example.com",
        entity=["maría-lópez"], acl=["team:año-fiscal"])
    assert "maría-lópez" in stamped
    assert "año-fiscal" in stamped
    assert "\\u" not in stamped


def test_with_related_link_appends_the_field_when_the_page_declares_none():
    out, changed = page_policy.with_related_link(NO_RELATED_PAGE, "New Page")
    assert changed is True
    assert page_policy.related_links(out) == ["[[New Page]]"]
    assert "tags: [note]" in out


def test_with_related_link_is_idempotent_when_the_link_is_already_there():
    once, _ = page_policy.with_related_link(FLOW_PAGE, "New Page")
    twice, changed = page_policy.with_related_link(once, "New Page")
    assert changed is False
    assert twice == once


def test_with_related_link_never_removes_a_body_line():
    out, _ = page_policy.with_related_link(FLOW_PAGE, "New Page")
    body_before = page_policy.split_frontmatter(FLOW_PAGE)[1]
    assert page_policy.split_frontmatter(out)[1] == body_before


@pytest.mark.parametrize("kind,marker", [("overlap", "[!NOTE]"),
                                         ("contradiction", "[!WARNING]")])
def test_with_callout_only_appends_and_names_the_other_page(kind, marker):
    out = page_policy.with_callout(FLOW_PAGE, kind=kind, name="New Page", note="same ground")
    assert out.startswith(FLOW_PAGE.rstrip("\n"))          # nothing before it changed at all
    assert marker in out
    assert "[[New Page]]" in out
    assert "same ground" in out


def test_with_callout_collapses_a_multiline_note_so_the_quote_block_stays_one_line():
    out = page_policy.with_callout(FLOW_PAGE, kind="overlap", name="New Page",
                                   note="line one\nline two")
    callout = [line for line in out.splitlines() if line.startswith("> ") and "[!" not in line]
    assert callout == ["> line one line two"]


# ── related_links_from_line: the input to the gate's additive proof ────────────────────────────
def test_related_links_from_line_parses_a_flow_value():
    assert page_policy.related_links_from_line('related: ["[[A]]", "[[B]]"]') == ["[[A]]", "[[B]]"]


def test_related_links_from_line_reads_an_empty_list_as_empty_not_as_unknown():
    assert page_policy.related_links_from_line("related: []") == []


@pytest.mark.parametrize("line", [
    "title: x",                     # a different key
    "  related: [\"[[A]]\"]",       # indented: not a top-level field
    "related:",                     # opens a block list; this line alone declares nothing
    "related: not-a-list",
])
def test_related_links_from_line_returns_none_for_anything_it_cannot_establish(line):
    """`None` and `[]` must not be confused: an unparseable before-value read as an empty one would
    turn "I cannot tell what was lost" into "nothing was lost", which is the whole question
    `gate_body_rewrite` is asking."""
    assert page_policy.related_links_from_line(line) is None



# ── add_source_citation: the synthesis cites its verbatim source ───────────────────────────────
CITABLE_PAGE = ('---\ntype: note\ntitle: "X"\nsources: []\n---\n\n# X\n\nbody\n')


def test_add_source_citation_fills_an_empty_sources_list():
    text, values = page_policy.add_source_citation(CITABLE_PAGE, "acme-thread")
    assert values == ["[[acme-thread]]"]
    assert 'sources: ["[[acme-thread]]"]' in text


def test_add_source_citation_preserves_the_agents_own_citations():
    drafted = CITABLE_PAGE.replace('sources: []', 'sources: ["[[other-page]]"]')
    text, values = page_policy.add_source_citation(drafted, "acme-thread")
    assert values == ["[[other-page]]", "[[acme-thread]]"]
    assert 'sources: ["[[other-page]]", "[[acme-thread]]"]' in text


def test_add_source_citation_is_idempotent():
    once, _ = page_policy.add_source_citation(CITABLE_PAGE, "acme-thread")
    twice, values = page_policy.add_source_citation(once, "acme-thread")
    assert values == ["[[acme-thread]]"]
    assert twice.count("[[acme-thread]]") == 1


def test_add_source_citation_reads_a_block_style_list_and_rewrites_it_flow_style():
    """A real YAML parser reads the drafted value (both styles), and the rewrite lands in the
    flow style every template uses — no orphan `- item` continuation lines left behind."""
    drafted = CITABLE_PAGE.replace('sources: []', 'sources:\n  - "[[other-page]]"')
    text, values = page_policy.add_source_citation(drafted, "acme-thread")
    assert values == ["[[other-page]]", "[[acme-thread]]"]
    assert '- "[[other-page]]"' not in text


def test_add_source_citation_adds_the_line_when_the_draft_has_none():
    drafted = ('---\ntype: note\ntitle: "X"\n---\n\n# X\n\nbody\n')
    text, values = page_policy.add_source_citation(drafted, "acme-thread")
    assert values == ["[[acme-thread]]"]
    assert 'sources: ["[[acme-thread]]"]' in text


# ── the shared frontmatter line editors (`repair.entity_alias` and `entities.decide` both write
# through these; one opinion about block sequences, re-cased keys and where a new line lands) ────
_ENTITY_PAGE = ('---\ntype: entity\ntitle: "Globex"\naliases: []\nrelated:\n  - "[[A]]"\n'
                'entity: ["globex"]\n---\n\n# Globex\n')


def test_front_and_tail_round_trips_byte_for_byte():
    front, tail = page_policy.front_and_tail(_ENTITY_PAGE)
    assert front[0] == "type: entity"
    assert page_policy.rebuild(front, tail) == _ENTITY_PAGE


def test_front_and_tail_refuses_a_page_with_no_block():
    with pytest.raises(ValueError, match="no `---` frontmatter block"):
        page_policy.front_and_tail("# Just a body\n")


def test_list_field_values_reads_flow_and_block_shapes():
    front, _ = page_policy.front_and_tail(_ENTITY_PAGE)
    assert page_policy.list_field_values(front, "entity") == ["globex"]
    assert page_policy.list_field_values(front, "related") == ["[[A]]"]
    assert page_policy.list_field_values(front, "aliases") == []
    assert page_policy.list_field_values(front, "nope") == []


def test_with_list_field_rewrites_in_place_and_appends_an_absent_field():
    front, tail = page_policy.front_and_tail(_ENTITY_PAGE)
    rewritten = page_policy.with_list_field(front, "related", ["[[A]]", "[[B]]"])
    assert rewritten[3] == 'related: ["[[A]]", "[[B]]"]'     # the block collapsed onto ONE line
    assert len(rewritten) == len(front) - 1
    appended = page_policy.with_list_field(front, "proposed_aliases", ["GX"])
    assert appended[-1] == 'proposed_aliases: ["GX"]' and appended[:-1] == front


def test_with_scalar_field_rewrites_in_place_and_appends_an_absent_field():
    front, _ = page_policy.front_and_tail(_ENTITY_PAGE)
    assert page_policy.with_scalar_field(front, "title", "Globex Corp")[1] == 'title: "Globex Corp"'
    assert page_policy.with_scalar_field(front, "approved_by", "marc")[-1] == 'approved_by: "marc"'
