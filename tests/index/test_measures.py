"""Graph measures over a fixture repository, per layer and in total (spec retrieval-and-curation §3.4,
BQ-12). Rows come from the real corpus parser, so links are resolved exactly as the index stores
them."""
import dataclasses

import pytest

from stigmergy.index import corpus, measures
from tests.server.conftest import write_page

SOURCE_A = "sources/2026/03/30000000-0000-4000-8000-000000000001.md"
SOURCE_B = "sources/2026/04/30000000-0000-4000-8000-000000000002.md"
SOURCE_F = "sources/2026/05/30000000-0000-4000-8000-000000000003.md"
ACME = "wiki/entities/Acme.md"
DANA = "wiki/entities/Dana.md"
LONE = "wiki/entities/Lone Ranger.md"
VAULT = "wiki/entities/Vault.md"
KICKOFF = "wiki/notes/Kickoff.md"
REVIEW = "wiki/notes/Review.md"
PRICING = "wiki/concepts/Pricing.md"
LEDGER = "wiki/notes/Ledger.md"


@pytest.fixture(scope="module")
def rows(tmp_path_factory):
    repo = str(tmp_path_factory.mktemp("measures") / "repo")
    write_page(repo, SOURCE_A, {"title": "A"}, "Evidence A.")
    write_page(repo, SOURCE_B, {"title": "B"}, "Evidence B.")
    write_page(repo, SOURCE_F, {"title": "F", "acl": ["finance"]}, "Evidence F.")
    write_page(repo, ACME, {"title": "Acme", "aliases": ["Acme Corp"], "sources": [SOURCE_A, SOURCE_B]},
               "# Acme\n\n## What / Who\nA customer. (Source: `" + SOURCE_A + "`)\n\n## Facts\n"
               "- 2026-03-01: signed. (Source: `" + SOURCE_A + "`)\n- 2026-04-01: renewed. (Source: `"
               + SOURCE_B + "`)\n\n## Connections\n- [[Kickoff]] - where it started")
    write_page(repo, DANA, {"title": "Dana", "entity_type": "person", "sources": [SOURCE_A]},
               "# Dana\n\n## What / Who\nBuyer at [[Acme]].\n\n## Facts\n- 2026-03-01: joined. (Source: `"
               + SOURCE_A + "`)")
    write_page(repo, LONE, {"title": "Lone Ranger", "entity_type": "person"},
               "# Lone Ranger\n\n## What / Who\nNobody links here.")
    write_page(repo, KICKOFF, {"title": "Kickoff", "sources": [SOURCE_A]},
               "# Kickoff\n\nThe kickoff with [[Acme]] and [[Dana]]. (Source: `" + SOURCE_A + "`)\n\n"
               "It cites [[Missing Page]] too.")
    write_page(repo, REVIEW, {"title": "Review", "sources": [SOURCE_B]},
               "# Review\n\nThe review with [[Acme]].\n\nUncited line.\n\n- One finding.\n- Another.\n\n"
               "(Source: `" + SOURCE_B + "`)\n\n## Entities\n- [[Acme]] - reviewed")
    write_page(repo, PRICING, {"title": "Pricing"}, "# Pricing\n\nPer seat, see [[Kickoff]].")
    write_page(repo, VAULT, {"title": "Vault", "acl": ["finance"], "sources": [SOURCE_F]},
               "# Vault\n\n## Facts\n- 2026-05-01: escrow. (Source: `" + SOURCE_F + "`)")
    write_page(repo, LEDGER, {"title": "Ledger", "acl": ["finance"], "sources": [SOURCE_F]},
               "# Ledger\n\nThe [[Acme]] invoice and the [[Vault]] escrow. (Source: `" + SOURCE_F + "`)")
    # The finance layer's own part of the public entity page, fused for finance readers.
    write_page(repo, ACME, {"title": "Acme", "aliases": ["Acme Holdings"], "acl": ["finance"],
                            "sources": [SOURCE_F]},
               "# Acme\n\n## Facts\n- 2026-05-02: owes the invoice. (Source: `" + SOURCE_F + "`)")
    return [dataclasses.asdict(row) for row in corpus.load_pages(repo)]


def test_scopes_are_the_brain_public_and_every_layer_that_holds_a_row(rows):
    assert measures.scopes_of(rows) == ["brain", "public", "finance"]


def test_public_measures_follow_their_definitions(rows):
    result = measures.compute(rows, "public")
    assert result["measures"] == {
        "pages": 8,
        "pages_by_type": {"note": 2, "concept": 1, "entity": 3, "project": 0, "source": 2},
        # Acme 1, Dana 1, Lone 0, Kickoff 2, Review 1, Pricing 1 distinct targets over six pages.
        "links_per_page": 1.0,
        "dead_links": 1,
        "orphan_entities": 1,
        "multi_source_entities": 1,
        # Paragraphs and lists outside Entities and Connections: Acme 2, Dana 2, Lone 1, Kickoff 2,
        # Review 3, Pricing 1; Acme 2, Dana 1, Kickoff 1, and Review's cited list 1 cite a source.
        "cited_paragraphs": round(5 / 11, 4),
        "entities_without_aliases": 2,
        "thin_entities": 2,
        "entities": 3,
    }
    assert [hub["path"] for hub in result["hubs"]] == [ACME, KICKOFF, DANA]
    assert result["hubs"][0]["inbound"] == 3
    assert [item["path"] for item in result["lists"]["orphan_entities"]] == [LONE]
    assert [(item["path"], item["facts"]) for item in result["lists"]["thin_entities"]] == [(DANA, 1), (LONE, 0)]
    assert [item["path"] for item in result["lists"]["entities_without_aliases"]] == [DANA, LONE]


def test_a_layer_scope_fuses_its_rows_with_public_and_the_brain_holds_everything(rows):
    finance = measures.compute(rows, "finance")["measures"]
    brain = measures.compute(rows, "brain")["measures"]
    assert finance == brain
    assert brain["pages_by_type"] == {"note": 3, "concept": 1, "entity": 4, "project": 0, "source": 3}
    assert brain["multi_source_entities"] == 1          # Acme: A, B and F fused
    assert brain["thin_entities"] == 3                  # Acme now has three facts; Dana, Lone, Vault
    acme = next(hub for hub in measures.compute(rows, "brain")["hubs"] if hub["path"] == ACME)
    assert acme["inbound"] == 4 and acme["layers"] == ["public", "finance"]


def test_the_constellation_holds_the_hubs_their_neighbours_and_the_orphans(rows):
    constellation = measures.compute(rows, "public")["constellation"]
    nodes = {node["path"]: node for node in constellation["nodes"]}
    assert set(nodes) == {ACME, DANA, LONE, KICKOFF, REVIEW, PRICING}
    assert nodes[LONE]["orphan"] is True and nodes[ACME]["orphan"] is False
    assert {"from": ACME, "to": KICKOFF} in constellation["edges"]
    assert all(edge["from"] in nodes and edge["to"] in nodes for edge in constellation["edges"])


def test_paragraphs_follow_headings_without_blank_lines_and_fold_only_within_a_section():
    source = "(Source: `" + SOURCE_A + "`)"
    assert measures._paragraphs(f"{source}\n\nA claim.") == [False]
    assert measures._paragraphs("A claim.\n## Entities\n- [[Acme]] - customer") == [False]
    assert measures._paragraphs(f"A claim.\n\n## Facts\n{source}") == [False]
    assert measures._paragraphs(f"- one\n- two\n\n{source}") == [True]
