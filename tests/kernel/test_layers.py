"""`kernel.layers`: layer names, the virtual/physical path mapping, and body fusion."""
import pytest

from stigmergy.kernel import layers

PUBLIC_PAGE = """# Acme

## What / Who
Acme is a robotics company. (Source: `sources/2026/09/11111111-1111-4111-8111-111111111111.md`)

## Facts
- 2026-09-21: Acme raised a seed round. (Source: `sources/2026/09/11111111-1111-4111-8111-111111111111.md`)

## Connections
- [[Dana]] - founder
"""

LAYER_PAGE = """# Acme

## Facts
- 2026-09-10: The deals team met Acme. (Source: `sources/2026/09/22222222-2222-4222-8222-222222222222.md`)

## Connections
- [[Dana]] - met the team
- [[Lumen]] - competitor
"""


def test_layer_names_exclude_the_root_and_reserved_words():
    assert layers.is_layer_name("deals")
    assert layers.is_layer_name("people-ops")
    for name in ("public", "brain-admins", "layers", "wiki", "sources", "Deals", "d", "9lives", "a_b"):
        assert not layers.is_layer_name(name)


def test_an_audience_names_exactly_one_layer():
    assert layers.layer_of_acl(None) == "public"
    assert layers.layer_of_acl(("deals",)) == "deals"
    assert layers.acl_of_layer("public") is None
    assert layers.acl_of_layer("deals") == ("deals",)
    for acl in ((), ("deals", "people"), ("public",), ("brain-admins",)):
        with pytest.raises(layers.LayerError):
            layers.layer_of_acl(acl)


def test_public_lives_at_the_root_and_layers_under_their_folder():
    assert layers.physical_path("public", "wiki/entities/Acme.md") == "wiki/entities/Acme.md"
    assert layers.physical_path("deals", "wiki/entities/Acme.md") == "layers/deals/wiki/entities/Acme.md"
    assert layers.split_path("layers/deals/sources/2026/09/x.md") == ("deals", "sources/2026/09/x.md")
    assert layers.split_path("wiki/notes/N.md") == ("public", "wiki/notes/N.md")
    for bad in ("layers/public/wiki/x.md", "layers/Deals/wiki/x.md", "layers/deals"):
        with pytest.raises(layers.LayerError):
            layers.split_path(bad)


def test_merging_for_the_librarian_puts_public_units_first_under_shared_headings():
    merged = layers.merge_for_writing(PUBLIC_PAGE, LAYER_PAGE, layer="deals")
    assert merged.count("# Acme\n") == 1
    facts = merged.split("## Facts", 1)[1].split("##", 1)[0]
    assert facts.index("Acme raised a seed round") < facts.index("The deals team met Acme")
    assert merged.count("[[Dana]] - founder") == 1
    assert "[[Lumen]]" in merged
    assert "[layer:" not in merged


def test_splitting_keeps_only_what_the_public_page_does_not_hold():
    merged = layers.merge_for_writing(PUBLIC_PAGE, LAYER_PAGE, layer="deals")
    extended = merged.replace(
        "- [[Lumen]] - competitor", "- [[Lumen]] - competitor\n- [[Orbit]] - partner")
    part = layers.split_from_public(extended, PUBLIC_PAGE)
    assert "The deals team met Acme" in part and "[[Orbit]]" in part
    assert "Acme raised a seed round" not in part and "Acme is a robotics company" not in part
    assert "## What / Who" not in part
    assert part.startswith("# Acme\n")


def test_merge_then_split_round_trips_the_layer_part():
    merged = layers.merge_for_writing(PUBLIC_PAGE, LAYER_PAGE, layer="deals")
    again = layers.merge_for_writing(PUBLIC_PAGE, layers.split_from_public(merged, PUBLIC_PAGE), layer="deals")
    assert layers.unit_key(again) == layers.unit_key(merged)


def test_splitting_an_unchanged_public_page_leaves_nothing():
    assert layers.split_from_public(PUBLIC_PAGE, PUBLIC_PAGE) == ""


def test_reading_orders_dated_facts_and_marks_layer_units():
    fused = layers.fuse_for_reading([("public", PUBLIC_PAGE), ("deals", LAYER_PAGE)])
    facts = fused.split("## Facts", 1)[1].split("##", 1)[0]
    assert facts.index("2026-09-10") < facts.index("2026-09-21")
    assert "- [layer:deals] 2026-09-10: The deals team met Acme." in fused
    assert "- 2026-09-21: Acme raised a seed round." in fused
    connections = fused.split("## Connections", 1)[1]
    assert connections.count("[[Dana]]") == 1
    assert "[layer:deals] [[Lumen]]" in connections


def test_reading_keeps_a_newest_first_section_newest_first():
    project = ("# Lumen rollout\n\n## Current state\n"
               "- 2026-09-30: Two of five pilot teams onboarded. (Source: `sources/2026/09/b.md`)\n"
               "- 2026-09-08: The pilot started with five teams. (Source: `sources/2026/09/a.md`)\n")
    for fused in (layers.fuse_for_reading([("deals", project)]),
                  layers.fuse_for_reading([("public", project), ("deals", project.replace("Two of five", "Three"))])):
        state = fused.split("## Current state", 1)[1]
        assert state.index("2026-09-30") < state.index("2026-09-08")


def test_a_contradiction_callout_is_one_unit():
    body = (
        "# Page\n\nLead paragraph.\n\n"
        "<!-- stigmergy-contradiction-start:con_1 -->\n> [!WARNING] x\n> - **Claim:** a\n"
        "<!-- stigmergy-contradiction-data:abc -->\n<!-- stigmergy-contradiction-end:con_1 -->\n"
    )
    parsed = layers.parse_body(body)
    kinds = [unit.kind for section in parsed.sections for unit in section.units]
    assert kinds == ["paragraph", "callout"]
    assert layers.render_body(parsed) == body


def test_list_items_keep_their_lazy_attribution_line():
    body = "# N\n\n## Topic\n- First.\n- Second.\n(Source: `sources/2026/09/a.md`)\n"
    units = layers.parse_body(body).sections[0].units
    assert [unit.text for unit in units] == ["- First.", "- Second.\n(Source: `sources/2026/09/a.md`)"]
    assert layers.render_body(layers.parse_body(body)) == body


def test_fusing_rows_unions_links_sources_and_aliases():
    rows = [
        {"vpath": "wiki/entities/Acme.md", "layer": "deals", "title": "Acme", "aliases": ["ACME Inc"],
         "links": ["wiki/entities/Lumen.md"], "sources": ["sources/b.md"], "updated": "2026-09-22",
         "entity_type": "organization", "body": LAYER_PAGE},
        {"vpath": "wiki/entities/Acme.md", "layer": "public", "title": "Acme", "aliases": ["Acme Robotics"],
         "links": ["wiki/entities/Dana.md"], "sources": ["sources/a.md"], "updated": "2026-09-21",
         "entity_type": "organization", "body": PUBLIC_PAGE},
    ]
    fused = layers.fuse_rows(rows)
    assert fused["path"] == "wiki/entities/Acme.md"
    assert fused["layers"] == ["public", "deals"]
    assert fused["aliases"] == ["Acme Robotics", "ACME Inc"]
    assert fused["links"] == ["wiki/entities/Dana.md", "wiki/entities/Lumen.md"]
    assert fused["sources"] == ["sources/a.md", "sources/b.md"]
    assert fused["updated"] == "2026-09-22"


def test_a_marker_written_in_stored_text_is_not_read_as_a_layer():
    fused = layers.fuse_for_reading([
        ("public", "# Acme\n\n## Facts\n- [layer:deals] Acme builds robots.\n"),
        ("deals", "# Acme\n\n## Facts\n- Acme pays yearly.\n"),
    ])

    assert [match.group("layer") for match in layers.MARKER_RE.finditer(fused)
            if fused[match.start() - 1] != "\\"] == ["deals"]
    assert "- \\[layer:deals] Acme builds robots." in fused


def test_a_marker_written_on_a_public_only_page_is_escaped_too():
    fused = layers.fuse_rows([{"vpath": "wiki/entities/Acme.md", "layer": "public",
                               "body": "# Acme\n\n- [layer:deals] Acme builds robots.\n"}])

    assert "- \\[layer:deals] Acme builds robots." in fused["body"]
    assert layers.strip_markers(fused["body"]) == "# Acme\n\n- Acme builds robots.\n"
