from pathlib import Path

import pytest

from stigmergy.index import corpus
from stigmergy.index.errors import StigmergyIndexError

FIXTURE = str(Path(__file__).parent / "fixtures" / "repo")


def _wiki_text(*, role="note", title="Example", status="developing", body="Useful body"):
    folder = "notes" if role == "note" else "concepts"
    path = f"wiki/{folder}/{title}.md"
    text = (
        "---\n"
        "id: page_00000000-0000-4000-8000-000000000001\n"
        f"type: {role}\n"
        f"title: {title}\n"
        f"status: {status}\n"
        "created: '2026-01-01'\n"
        "updated: '2026-01-02'\n"
        "acl: null\n"
        "entity: []\n"
        "sources: []\n"
        "---\n\n"
        f"# {title}\n\n{body}\n"
    )
    return path, text


def test_loads_only_canonical_searchable_roles():
    rows = corpus.load_pages(FIXTURE)
    assert len(rows) == 10
    assert {row.type for row in rows} == {"note", "concept", "source"}
    assert not any(row.path.startswith("wiki/entities/") for row in rows)
    assert not any(row.path.startswith(("ops/", "meta/", "datasets/")) for row in rows)


def test_page_row_preserves_canonical_metadata():
    path, text = _wiki_text(body="Body with [[Support refunds]].")
    row = corpus.page_row(path, "wiki", text)
    assert row.page_id == "page_00000000-0000-4000-8000-000000000001"
    assert row.type == "note"
    assert row.status == "developing"
    assert row.updated == "2026-01-02"
    assert row.acl is None
    assert row.links == ["support refunds"]
    assert row.content_hash.startswith("sha256:")


def test_entity_anchors_remain_plural_and_ordered():
    path, text = _wiki_text()
    text = text.replace(
        "entity: []",
        "entity:\n- ent_a\n- ent_b\n- ent_a",
    )
    assert corpus.page_row(path, "wiki", text).entity == ["ent_a", "ent_b"]


def test_restricted_acl_is_preserved():
    path, text = _wiki_text()
    text = text.replace("acl: null", "acl:\n- finance")
    assert corpus.page_row(path, "wiki", text).acl == ["finance"]


@pytest.mark.parametrize("acl", ["[]", "finance", "[finance, 2]", "false"])
def test_invalid_acl_fails_closed(acl):
    path, text = _wiki_text()
    text = text.replace("acl: null", f"acl: {acl}")
    with pytest.raises(corpus.CorpusContractError, match="acl"):
        corpus.page_row(path, "wiki", text)


def test_corpus_contract_error_is_an_index_domain_error_not_a_generic_value_error():
    path, text = _wiki_text()
    text = text.replace("acl: null", "acl: []")

    with pytest.raises(StigmergyIndexError) as exc_info:
        corpus.page_row(path, "wiki", text)

    assert isinstance(exc_info.value, corpus.CorpusContractError)
    assert not isinstance(exc_info.value, ValueError)


@pytest.mark.parametrize("role", ["meeting", "document", "page", "view", "raw"])
def test_removed_roles_are_rejected(role):
    path, text = _wiki_text()
    text = text.replace("type: note", f"type: {role}")
    with pytest.raises(corpus.CorpusContractError, match="must have type note"):
        corpus.page_row(path, "wiki", text)


def test_noncanonical_paths_are_not_indexable():
    assert not corpus.is_indexable_page("wiki/entities/ent_x.md")
    assert not corpus.is_indexable_page("wiki/playbooks/Support.md")
    assert not corpus.is_indexable_page("sources/general/input.md")
    assert corpus.is_indexable_page("wiki/notes/Support.md")
    assert corpus.is_indexable_page(
        "sources/2026/08/00000000-0000-4000-8000-000000000001.md"
    )


def test_link_graph_resolves_paths_and_counts_inlinks():
    rows = {row.path: row for row in corpus.load_pages(FIXTURE)}
    refund = rows["wiki/notes/Refund policy.md"]
    support = rows["wiki/concepts/Support refunds.md"]
    assert refund.links == ["wiki/concepts/Support refunds.md"]
    assert support.inlinks == 1


def test_links_inside_code_are_not_edges():
    assert corpus.link_targets("`[[inline]]`\n```md\n[[fenced]]\n```\n[[real]]") == ["real"]


def test_dotted_link_names_are_not_truncated():
    assert corpus.link_targets("[[Booking.com]] [[Guide.md|guide]]") == ["booking.com", "guide"]


def test_frontmatter_parser_accepts_bom_and_crlf():
    metadata, body, malformed = corpus.split_frontmatter_checked(
        "\ufeff---\r\ntitle: Example\r\n---\r\nbody\r\n"
    )
    assert metadata == {"title": "Example"}
    assert body == "body\r\n"
    assert malformed is False


def test_unreadable_acl_frontmatter_is_marked_malformed():
    metadata, _body, malformed = corpus.split_frontmatter_checked(
        "---\nacl: [finance\n\n# Body\n"
    )
    assert metadata == {}
    assert malformed is True


def test_plain_markdown_is_not_misclassified_as_malformed_frontmatter():
    metadata, body, malformed = corpus.split_frontmatter_checked("# Body\n\nacl: is prose\n")
    assert metadata == {}
    assert body.startswith("# Body")
    assert malformed is False


def test_content_hash_changes_with_searchable_content():
    path, first = _wiki_text(body="First")
    _, second = _wiki_text(body="Second")
    assert corpus.page_row(path, "wiki", first).content_hash != corpus.page_row(
        path, "wiki", second
    ).content_hash
